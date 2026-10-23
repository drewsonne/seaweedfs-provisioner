"""Policy JSON, client factories and error mapping in common."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import common
import kopf
import pytest
from botocore.exceptions import ClientError


def _err(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


def _statement(buckets: list[str], access: str) -> dict:
    return json.loads(common.bucket_policy(buckets, access))["Statement"][0]


def test_policy_read_write_one_bucket() -> None:
    st = _statement(["a"], "read-write")

    assert st["Resource"] == ["arn:aws:s3:::a", "arn:aws:s3:::a/*"]
    assert {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"} <= set(st["Action"])
    assert "s3:ListBucketMultipartUploads" in st["Action"]


def test_policy_two_buckets() -> None:
    st = _statement(["a", "b"], "read-write")

    assert st["Resource"] == [
        "arn:aws:s3:::a",
        "arn:aws:s3:::a/*",
        "arn:aws:s3:::b",
        "arn:aws:s3:::b/*",
    ]


def test_policy_read_only() -> None:
    assert _statement(["a"], "read-only")["Action"] == ["s3:Get*", "s3:List*"]


@pytest.mark.parametrize(
    ("factory", "service"),
    [
        (common.s3_client, "s3"),
        (common.iam_client, "iam"),
    ],
)
def test_client_uses_env_endpoint(factory, service) -> None:  # noqa: ANN001
    with patch.object(common.boto3, "client") as boto:
        factory()

    args, kwargs = boto.call_args
    assert args == (service,)
    assert kwargs["endpoint_url"] == "http://seaweedfs.test:8333"
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["config"].s3["addressing_style"] == "path"


def test_bucket_exists_false_on_not_found() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _err("404")

    assert common.bucket_exists(client, "a") is False


def test_client_error_becomes_temporary_error() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _err("InternalError")

    with pytest.raises(kopf.TemporaryError):
        common.bucket_exists(client, "a")


def test_ensure_bucket_creates_when_missing() -> None:
    client = MagicMock()
    client.head_bucket.side_effect = _err("NoSuchBucket")

    common.ensure_bucket(client, "a", MagicMock())

    client.create_bucket.assert_called_once_with(Bucket="a")


def test_versioning_roundtrip() -> None:
    client = MagicMock()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}

    common.set_bucket_versioning(client, "a", enabled=True)

    assert common.get_bucket_versioning_enabled(client, "a") is True
    client.put_bucket_versioning.assert_called_once_with(
        Bucket="a", VersioningConfiguration={"Status": "Enabled"}
    )
