"""Check bucket-limited keys against a real SeaweedFS (SEAWEEDFS_IT=1)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from unittest import mock

import boto3
import common
import pytest
import users
from botocore.config import Config
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SEAWEEDFS_IT") != "1", reason="needs SEAWEEDFS_IT=1"
    ),
]


def _s3(access_key: str, secret_key: str) -> Any:  # noqa: ANN401
    return boto3.client(
        "s3",
        endpoint_url=common.ENDPOINT_URL,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )


def _provision(name: str, bucket: str, access: str) -> tuple[str, str]:
    """Run the real user upsert, with the Kubernetes Secret helpers stubbed."""
    written: dict[str, str] = {}
    spec = {"bucket": bucket, "access": access, "secretName": f"{name}-s3"}
    with (
        mock.patch.object(users, "get_existing_secret_data", return_value=None),
        mock.patch.object(
            users,
            "ensure_secret",
            side_effect=lambda _ns, _n, _b, data, _l: written.update(data),
        ),
    ):
        users._upsert_user(spec, {}, name, "default", mock.Mock())
    return written["access_key"], written["secret_key"]


def _denied(call: Callable[[], object]) -> bool:
    try:
        call()
    except ClientError as exc:
        return exc.response["Error"]["Code"] in {"AccessDenied", "403"}
    return False


def test_keys_are_limited_to_their_bucket() -> None:
    admin = common.s3_client()
    for bucket in ("it-bucket-a", "it-bucket-b"):
        common.ensure_bucket(admin, bucket, mock.Mock())
        admin.put_object(Bucket=bucket, Key="seed", Body=b"seed")

    rw_key = _provision("it-rw", "it-bucket-a", "read-write")
    rw = _s3(*rw_key)
    rw.put_object(Bucket="it-bucket-a", Key="k", Body=b"hello")
    assert rw.get_object(Bucket="it-bucket-a", Key="k")["Body"].read() == b"hello"
    keys = [o["Key"] for o in rw.list_objects_v2(Bucket="it-bucket-a")["Contents"]]
    assert "k" in keys
    assert _denied(lambda: rw.put_object(Bucket="it-bucket-b", Key="k", Body=b"x"))
    assert _denied(lambda: rw.get_object(Bucket="it-bucket-b", Key="seed"))

    ro = _s3(*_provision("it-ro", "it-bucket-a", "read-only"))
    assert ro.get_object(Bucket="it-bucket-a", Key="k")["Body"].read() == b"hello"
    assert _denied(lambda: ro.put_object(Bucket="it-bucket-a", Key="n", Body=b"x"))

    iam = common.iam_client()
    for name in ("it-rw", "it-ro"):
        for key in iam.list_access_keys(UserName=name)["AccessKeyMetadata"]:
            iam.delete_access_key(UserName=name, AccessKeyId=key["AccessKeyId"])
        iam.delete_user_policy(UserName=name, PolicyName=users.POLICY_NAME)
        iam.delete_user(UserName=name)
    with pytest.raises(ClientError):
        rw.get_object(Bucket="it-bucket-a", Key="k")
