"""Shared helpers for the seaweedfs-provisioner operator."""

from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING, Any

import boto3
import kopf
import kubernetes
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Sequence

SEAWEEDFS_ENDPOINT = os.environ["SEAWEEDFS_ENDPOINT"]
SEAWEEDFS_ACCESS_KEY = os.environ["SEAWEEDFS_ACCESS_KEY"]
SEAWEEDFS_SECRET_KEY = os.environ["SEAWEEDFS_SECRET_KEY"]
SEAWEEDFS_SECURE = os.environ.get("SEAWEEDFS_SECURE", "false").lower() == "true"

CRD_GROUP = "seaweedfs.sonne.zone"
CRD_VERSION = "v1"

# Endpoint exposed to consumers (written into K8s secrets)
ENDPOINT_URL = f"{'https' if SEAWEEDFS_SECURE else 'http'}://{SEAWEEDFS_ENDPOINT}"

_NOT_FOUND_CODES = frozenset({"NoSuchEntity", "NoSuchBucket", "404", "NotFound"})

RW_ACTIONS = [
    "s3:GetObject",
    "s3:PutObject",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:GetBucketLocation",
    "s3:AbortMultipartUpload",
]
RO_ACTIONS = ["s3:Get*", "s3:List*"]


# ---------------------------------------------------------------------------
# Client factories (S3 and IAM share the S3 port)
# ---------------------------------------------------------------------------


def _client(service: str) -> Any:
    return boto3.client(
        service,
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=SEAWEEDFS_ACCESS_KEY,
        aws_secret_access_key=SEAWEEDFS_SECRET_KEY,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


def s3_client() -> Any:
    """Return a boto3 S3 client for bucket operations."""
    return _client("s3")


def iam_client() -> Any:
    """Return a boto3 IAM client for user, key and policy operations."""
    return _client("iam")


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def error_code(exc: ClientError) -> str:
    """Return the S3/IAM error code of a ClientError."""
    return exc.response.get("Error", {}).get("Code", "")


def is_not_found(exc: ClientError) -> bool:
    """Return True for NoSuchEntity / NoSuchBucket style errors."""
    return error_code(exc) in _NOT_FOUND_CODES


def temporary(action: str, exc: ClientError) -> kopf.TemporaryError:
    """Wrap a ClientError so kopf retries the handler."""
    return kopf.TemporaryError(f"SeaweedFS error {action}: {exc}", delay=30)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def bucket_policy(buckets: Sequence[str], access: str) -> str:
    """Return the inline IAM policy JSON limiting a user to the given buckets."""
    actions = RO_ACTIONS if access == "read-only" else RW_ACTIONS
    resources = [
        arn for b in buckets for arn in (f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*")
    ]
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": actions, "Resource": resources}
            ],
        }
    )


# ---------------------------------------------------------------------------
# Kubernetes Secret helpers
# ---------------------------------------------------------------------------


def get_existing_secret_data(
    namespace: str,
    secret_name: str,
) -> dict[str, str] | None:
    """Read all decoded key/value pairs from an existing Kubernetes Secret.

    Returns None if the secret does not exist.  Raises TemporaryError on
    unexpected Kubernetes API failures.
    """
    v1 = kubernetes.client.CoreV1Api()
    try:
        secret = v1.read_namespaced_secret(name=secret_name, namespace=namespace)
        return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            return None
        raise kopf.TemporaryError(
            f"Failed reading secret {namespace}/{secret_name}: {exc}",
            delay=15,
        ) from exc


def ensure_secret(
    namespace: str,
    secret_name: str,
    body: kopf.Body,
    data: dict[str, str],
    logger: kopf.Logger,
) -> None:
    """Create or update a Kubernetes Secret with the supplied data.

    Skips the patch if the secret already exists with identical values to
    avoid churning the secret's resourceVersion.

    Owner references are only set when the CR and secret share the same
    namespace; cross-namespace owner refs are not permitted by Kubernetes.
    """
    v1 = kubernetes.client.CoreV1Api()
    secret_body = kubernetes.client.V1Secret(
        metadata=kubernetes.client.V1ObjectMeta(name=secret_name),
        string_data=data,
    )
    cr_namespace = body["metadata"]["namespace"]
    if namespace == cr_namespace:
        kopf.adopt(secret_body)

    try:
        v1.create_namespaced_secret(namespace=namespace, body=secret_body)
        logger.info("Created secret %s/%s", namespace, secret_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 409:
            existing = v1.read_namespaced_secret(name=secret_name, namespace=namespace)
            existing_data = {
                k: base64.b64decode(v).decode()
                for k, v in (existing.data or {}).items()
            }
            if existing_data == data:
                logger.debug(
                    "Secret %s/%s already up-to-date, skipping patch",
                    namespace,
                    secret_name,
                )
                return
            v1.patch_namespaced_secret(
                name=secret_name,
                namespace=namespace,
                body={"stringData": data},
            )
            logger.info("Updated existing secret %s/%s", namespace, secret_name)
        else:
            raise kopf.TemporaryError(
                f"Kubernetes API error creating secret "
                f"{namespace}/{secret_name}: {exc}",
                delay=15,
            ) from exc


def delete_secret(
    namespace: str,
    secret_name: str,
    logger: kopf.Logger,
) -> None:
    """Delete a Kubernetes Secret, silently ignoring 404."""
    v1 = kubernetes.client.CoreV1Api()
    try:
        v1.delete_namespaced_secret(name=secret_name, namespace=namespace)
        logger.info("Deleted secret %s/%s", namespace, secret_name)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            logger.debug("Secret %s/%s already absent", namespace, secret_name)
        else:
            raise kopf.TemporaryError(
                f"Failed deleting secret {namespace}/{secret_name}: {exc}",
                delay=15,
            ) from exc


# ---------------------------------------------------------------------------
# Bucket helpers
# ---------------------------------------------------------------------------


def bucket_exists(client: Any, name: str) -> bool:
    """Return True if the bucket exists."""
    try:
        client.head_bucket(Bucket=name)
    except ClientError as exc:
        if is_not_found(exc):
            return False
        raise temporary(f"checking bucket {name!r}", exc) from exc
    return True


def ensure_bucket(client: Any, name: str, logger: kopf.Logger) -> None:
    """Create the bucket if it does not exist."""
    if bucket_exists(client, name):
        logger.debug("Bucket %r already exists", name)
        return
    try:
        client.create_bucket(Bucket=name)
    except ClientError as exc:
        raise temporary(f"creating bucket {name!r}", exc) from exc
    logger.info("Created bucket %r", name)


def set_bucket_versioning(
    client: Any,
    name: str,
    enabled: bool,  # noqa: FBT001
) -> None:
    """Enable or suspend versioning on a bucket."""
    status = "Enabled" if enabled else "Suspended"
    try:
        client.put_bucket_versioning(
            Bucket=name, VersioningConfiguration={"Status": status}
        )
    except ClientError as exc:
        raise temporary(f"setting versioning on {name!r}", exc) from exc


def get_bucket_versioning_enabled(client: Any, name: str) -> bool:
    """Return True if versioning is currently enabled on the bucket."""
    try:
        resp = client.get_bucket_versioning(Bucket=name)
    except ClientError as exc:
        raise temporary(f"reading versioning for {name!r}", exc) from exc
    return resp.get("Status") == "Enabled"
