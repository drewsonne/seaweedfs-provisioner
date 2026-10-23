"""Kopf handlers for the SeaweedfsUser CRD."""

from __future__ import annotations

from typing import Any

import kopf
from botocore.exceptions import ClientError
from common import (
    CRD_GROUP,
    CRD_VERSION,
    ENDPOINT_URL,
    bucket_policy,
    delete_secret,
    ensure_secret,
    get_existing_secret_data,
    iam_client,
    is_not_found,
    temporary,
)

POLICY_NAME = "seaweedfs-provisioner"


def _buckets(spec: kopf.Spec) -> list[str]:
    return [spec["bucket"]] if "bucket" in spec else list(spec["buckets"])


def _ensure_user(iam: Any, name: str, logger: kopf.Logger) -> None:
    try:
        iam.get_user(UserName=name)
    except ClientError as exc:
        if not is_not_found(exc):
            raise temporary(f"checking user {name!r}", exc) from exc
        try:
            iam.create_user(UserName=name)
        except ClientError as exc2:
            raise temporary(f"creating user {name!r}", exc2) from exc2
        logger.info("Created SeaweedFS user %r", name)


def _ensure_key(
    iam: Any, name: str, existing: dict[str, str] | None, logger: kopf.Logger
) -> tuple[str, str]:
    """Return (access_key, secret_key), reusing the Secret's key if still valid."""
    try:
        live = {
            k["AccessKeyId"]
            for k in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]
        }
        have = (existing or {}).get("access_key", "")
        secret = (existing or {}).get("secret_key", "")
        if have in live and secret:
            return have, secret
        created = iam.create_access_key(UserName=name)["AccessKey"]
        for stale in live:
            iam.delete_access_key(UserName=name, AccessKeyId=stale)
    except ClientError as exc:
        raise temporary(f"managing access key for {name!r}", exc) from exc
    logger.info("Created access key for SeaweedFS user %r", name)
    return created["AccessKeyId"], created["SecretAccessKey"]


def _upsert_user(
    spec: Any,
    body: Any,
    name: str,
    namespace: str,
    logger: Any,
) -> None:
    secret_name: str = spec["secretName"]
    secret_ns: str = spec.get("secretNamespace", namespace)
    iam = iam_client()

    _ensure_user(iam, name, logger)
    access_key, secret_key = _ensure_key(
        iam, name, get_existing_secret_data(secret_ns, secret_name), logger
    )
    try:
        iam.put_user_policy(
            UserName=name,
            PolicyName=POLICY_NAME,
            PolicyDocument=bucket_policy(
                _buckets(spec), spec.get("access", "read-write")
            ),
        )
    except ClientError as exc:
        raise temporary(f"putting policy for {name!r}", exc) from exc

    ensure_secret(
        secret_ns,
        secret_name,
        body,
        {"access_key": access_key, "secret_key": secret_key, "endpoint": ENDPOINT_URL},
        logger,
    )


def _reconcile(
    spec: Any,
    body: Any,
    name: str,
    namespace: str | None,
    logger: Any,
    patch: Any,
) -> None:
    if namespace is None:
        msg = "SeaweedfsUser must be namespace-scoped"
        raise kopf.PermanentError(msg)
    _upsert_user(spec, body, name, namespace, logger)
    patch.status["ready"] = True


def _drop_old_secret(old: Any, spec: Any, namespace: str | None, logger: Any) -> None:
    """Delete the previous Secret when secretName/secretNamespace changed."""
    if namespace is None or not old.get("secretName"):
        return
    old_target = (old.get("secretNamespace", namespace), old["secretName"])
    if old_target != (spec.get("secretNamespace", namespace), spec["secretName"]):
        delete_secret(*old_target, logger)


@kopf.on.create(
    CRD_GROUP, CRD_VERSION, "seaweedfsusers", retries=5, backoff=30, timeout=300
)
def create_fn(
    spec: kopf.Spec,
    body: kopf.Body,
    name: str,
    namespace: str | None,
    logger: kopf.Logger,
    patch: kopf.Patch,
    **_: Any,
) -> None:
    _reconcile(spec, body, name, namespace, logger, patch)


@kopf.on.resume(CRD_GROUP, CRD_VERSION, "seaweedfsusers")
def resume_fn(
    spec: kopf.Spec,
    body: kopf.Body,
    name: str,
    namespace: str | None,
    logger: kopf.Logger,
    patch: kopf.Patch,
    **_: Any,
) -> None:
    _reconcile(spec, body, name, namespace, logger, patch)


@kopf.on.update(
    CRD_GROUP, CRD_VERSION, "seaweedfsusers", field="spec", retries=3, backoff=15
)
def update_fn(
    spec: kopf.Spec,
    old: Any,
    body: kopf.Body,
    name: str,
    namespace: str | None,
    logger: kopf.Logger,
    patch: kopf.Patch,
    **_: Any,
) -> None:
    _reconcile(spec, body, name, namespace, logger, patch)
    _drop_old_secret(old, spec, namespace, logger)


def _tolerate_missing(action: str, call: Any, **kwargs: str) -> None:
    try:
        call(**kwargs)
    except ClientError as exc:
        if not is_not_found(exc):
            raise temporary(action, exc) from exc


@kopf.on.delete(
    CRD_GROUP, CRD_VERSION, "seaweedfsusers", retries=3, backoff=15, timeout=120
)
def delete_fn(
    spec: kopf.Spec,
    name: str,
    namespace: str | None,
    logger: kopf.Logger,
    **_: Any,
) -> None:
    """Remove the user's keys, policy, user and Secret. Buckets are never touched."""
    iam = iam_client()
    try:
        keys = iam.list_access_keys(UserName=name)["AccessKeyMetadata"]
    except ClientError as exc:
        if not is_not_found(exc):
            raise temporary(f"listing keys for {name!r}", exc) from exc
        keys = []
    for key in keys:
        _tolerate_missing(
            f"deleting key for {name!r}",
            iam.delete_access_key,
            UserName=name,
            AccessKeyId=key["AccessKeyId"],
        )
    _tolerate_missing(
        f"deleting policy for {name!r}",
        iam.delete_user_policy,
        UserName=name,
        PolicyName=POLICY_NAME,
    )
    _tolerate_missing(f"deleting user {name!r}", iam.delete_user, UserName=name)
    logger.info("Deleted SeaweedFS user %r", name)
    if namespace is not None:
        delete_secret(
            spec.get("secretNamespace", namespace), spec["secretName"], logger
        )


@kopf.timer(
    CRD_GROUP,
    CRD_VERSION,
    "seaweedfsusers",
    interval=300,
    initial_delay=60,
    idle=30,
)
def check_drift(
    spec: kopf.Spec,
    body: kopf.Body,
    name: str,
    namespace: str | None,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any] | None:
    """Re-run the upsert if the SeaweedFS user went missing."""
    if namespace is None:
        return None
    try:
        iam_client().get_user(UserName=name)
    except ClientError as exc:
        if not is_not_found(exc):
            raise temporary(f"checking user {name!r}", exc) from exc
        logger.warning("Drift detected: user %r is missing, recreating", name)
        _upsert_user(spec, body, name, namespace, logger)
        return {"ready": True, "drift": True, "driftReason": "missing"}
    return None
