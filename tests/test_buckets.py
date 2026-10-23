"""SeaweedfsBucket handlers against a mocked S3 client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import buckets
import kopf
import pytest
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Callable


def _err(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, op)


def _client(*, exists: bool, versioned: bool = False) -> MagicMock:
    client = MagicMock()
    if not exists:
        client.head_bucket.side_effect = _err("404", "HeadBucket")
    client.get_bucket_versioning.return_value = (
        {"Status": "Enabled"} if versioned else {}
    )
    return client


def _run(handler: Callable[..., Any], client: MagicMock, spec: dict) -> kopf.Patch:
    patch_ = kopf.Patch()
    with patch.object(buckets, "s3_client", return_value=client):
        handler(spec=spec, name="b", logger=MagicMock(), patch=patch_)
    return patch_


def test_create_makes_bucket_and_sets_ready() -> None:
    client = _client(exists=False)

    patch_ = _run(buckets.create_fn, client, {})

    client.create_bucket.assert_called_once_with(Bucket="b")
    client.put_bucket_versioning.assert_not_called()
    assert patch_.status["ready"] is True


def test_create_on_existing_bucket_is_noop() -> None:
    client = _client(exists=True)

    patch_ = _run(buckets.create_fn, client, {})

    client.create_bucket.assert_not_called()
    client.put_bucket_versioning.assert_not_called()
    assert patch_.status["ready"] is True


def test_update_toggles_versioning() -> None:
    client = _client(exists=True)

    patch_ = _run(buckets.update_fn, client, {"versioning": True})

    client.put_bucket_versioning.assert_called_once_with(
        Bucket="b", VersioningConfiguration={"Status": "Enabled"}
    )
    assert patch_.status["ready"] is True


def test_update_suspends_versioning() -> None:
    client = _client(exists=True, versioned=True)

    _run(buckets.update_fn, client, {"versioning": False})

    client.put_bucket_versioning.assert_called_once_with(
        Bucket="b", VersioningConfiguration={"Status": "Suspended"}
    )


def test_resume_sets_ready() -> None:
    patch_ = _run(buckets.resume_fn, _client(exists=True), {})

    assert patch_.status["ready"] is True


def test_unsupported_versioning_is_permanent_error() -> None:
    client = _client(exists=True)
    client.put_bucket_versioning.side_effect = _err("NotImplemented")

    with pytest.raises(kopf.PermanentError, match="cannot enable versioning"):
        _run(buckets.create_fn, client, {"versioning": True})


def test_delete_makes_no_s3_call() -> None:
    with patch.object(buckets, "s3_client") as factory:
        buckets.delete_fn(name="b", logger=MagicMock())

    factory.assert_not_called()


def test_drift_recreates_missing_bucket() -> None:
    client = _client(exists=False)

    with patch.object(buckets, "s3_client", return_value=client):
        result = buckets.check_drift(spec={}, name="b", logger=MagicMock())

    client.create_bucket.assert_called_once_with(Bucket="b")
    assert result is not None
    assert result["driftReason"] == "missing"


def test_drift_none_when_in_sync() -> None:
    with patch.object(buckets, "s3_client", return_value=_client(exists=True)):
        assert buckets.check_drift(spec={}, name="b", logger=MagicMock()) is None
