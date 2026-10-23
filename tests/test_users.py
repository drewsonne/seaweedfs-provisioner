"""SeaweedfsUser reconcile: user, key, policy and Secret."""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock

import kopf
import pytest
import users
from botocore.exceptions import ClientError

LOGGER = logging.getLogger("test")
BODY = {"metadata": {"namespace": "ns"}}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict:
    iam = MagicMock()
    iam.get_user.return_value = {}
    iam.list_access_keys.return_value = {"AccessKeyMetadata": []}
    iam.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AK1", "SecretAccessKey": "SK1"}
    }
    existing = MagicMock(return_value=None)
    ensure = MagicMock()
    monkeypatch.setattr(users, "iam_client", lambda: iam)
    monkeypatch.setattr(users, "get_existing_secret_data", existing)
    monkeypatch.setattr(users, "ensure_secret", ensure)
    return {"iam": iam, "existing": existing, "ensure": ensure}


def _spec(**kw: object) -> dict:
    return {"bucket": "a", "secretName": "s", **kw}


def _policy_resources(iam: MagicMock) -> list[str]:
    doc = iam.put_user_policy.call_args.kwargs["PolicyDocument"]
    return json.loads(doc)["Statement"][0]["Resource"]


def test_new_user_creates_user_key_policy_and_secret(env: dict) -> None:
    env["iam"].get_user.side_effect = ClientError(
        {"Error": {"Code": "NoSuchEntity"}}, "GetUser"
    )

    users._upsert_user(_spec(), BODY, "bob", "ns", LOGGER)

    env["iam"].create_user.assert_called_once_with(UserName="bob")
    env["iam"].put_user_policy.assert_called_once()
    ns, name, _, data, _ = env["ensure"].call_args.args
    assert (ns, name) == ("ns", "s")
    assert data == {
        "access_key": "AK1",
        "secret_key": "SK1",
        "endpoint": users.ENDPOINT_URL,
    }


def test_second_reconcile_reuses_key(env: dict) -> None:
    env["existing"].return_value = {"access_key": "AK0", "secret_key": "SK0"}
    env["iam"].list_access_keys.return_value = {
        "AccessKeyMetadata": [{"AccessKeyId": "AK0"}]
    }

    users._upsert_user(_spec(), BODY, "bob", "ns", LOGGER)

    env["iam"].create_access_key.assert_not_called()
    assert env["ensure"].call_args.args[3]["access_key"] == "AK0"


def test_lost_key_is_replaced_and_others_deleted(env: dict) -> None:
    env["existing"].return_value = {"access_key": "gone", "secret_key": "x"}
    env["iam"].list_access_keys.return_value = {
        "AccessKeyMetadata": [{"AccessKeyId": "other"}]
    }

    users._upsert_user(_spec(), BODY, "bob", "ns", LOGGER)

    env["iam"].delete_access_key.assert_called_once_with(
        UserName="bob", AccessKeyId="other"
    )
    assert env["ensure"].call_args.args[3]["access_key"] == "AK1"


def test_policy_names_only_declared_buckets(env: dict) -> None:
    spec = {"buckets": ["x", "y"], "secretName": "s"}
    users._upsert_user(spec, BODY, "b", "ns", LOGGER)

    assert _policy_resources(env["iam"]) == [
        "arn:aws:s3:::x",
        "arn:aws:s3:::x/*",
        "arn:aws:s3:::y",
        "arn:aws:s3:::y/*",
    ]


def test_read_only_rewrites_policy(env: dict) -> None:
    users._upsert_user(_spec(access="read-only"), BODY, "b", "ns", LOGGER)

    doc = env["iam"].put_user_policy.call_args.kwargs["PolicyDocument"]
    assert json.loads(doc)["Statement"][0]["Action"] == ["s3:Get*", "s3:List*"]


def test_bucket_change_rewrites_policy(env: dict) -> None:
    users._upsert_user(_spec(), BODY, "b", "ns", LOGGER)
    users._upsert_user(_spec(bucket="c"), BODY, "b", "ns", LOGGER)

    assert _policy_resources(env["iam"])[0] == "arn:aws:s3:::c"
    assert env["iam"].put_user_policy.call_count == len("ab")


def test_cluster_scoped_is_permanent_error() -> None:
    with pytest.raises(kopf.PermanentError):
        users._reconcile(_spec(), BODY, "b", None, LOGGER, MagicMock())


@pytest.mark.usefixtures("env")
def test_secret_rename_deletes_old_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    delete = MagicMock()
    monkeypatch.setattr(users, "delete_secret", delete)

    users._drop_old_secret(
        _spec(secretName="old"), _spec(secretName="new"), "ns", LOGGER
    )

    delete.assert_called_once_with("ns", "old", LOGGER)


MISSING = ClientError({"Error": {"Code": "NoSuchEntity"}}, "Op")


def _delete(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    delete = MagicMock()
    monkeypatch.setattr(users, "delete_secret", delete)
    fn: Any = users.delete_fn
    fn(spec=_spec(), name="bob", namespace="ns", logger=LOGGER)
    return delete


def test_delete_removes_keys_policy_user_then_secret(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    env["iam"].list_access_keys.return_value = {
        "AccessKeyMetadata": [{"AccessKeyId": "A"}, {"AccessKeyId": "B"}]
    }
    order = MagicMock()
    order.attach_mock(env["iam"].delete_access_key, "key")
    order.attach_mock(env["iam"].delete_user_policy, "policy")
    order.attach_mock(env["iam"].delete_user, "user")

    delete = _delete(monkeypatch)

    assert [c[0] for c in order.mock_calls] == ["key", "key", "policy", "user"]
    delete.assert_called_once_with("ns", "s", LOGGER)


def test_delete_of_absent_user_succeeds(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    env["iam"].list_access_keys.side_effect = MISSING
    env["iam"].delete_user_policy.side_effect = MISSING
    env["iam"].delete_user.side_effect = MISSING

    delete = _delete(monkeypatch)

    delete.assert_called_once()


@pytest.mark.usefixtures("env")
def test_delete_never_calls_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    monkeypatch.setattr("common.s3_client", s3, raising=False)
    _delete(monkeypatch)
    s3.assert_not_called()
    assert not hasattr(users, "s3_client")


def test_drift_recreates_missing_user(env: dict) -> None:
    env["iam"].get_user.side_effect = MISSING
    fn: Any = users.check_drift

    result = fn(spec=_spec(), body=BODY, name="bob", namespace="ns", logger=LOGGER)

    env["iam"].create_user.assert_called_once_with(UserName="bob")
    assert result["driftReason"] == "missing"


def test_drift_none_when_user_present(env: dict) -> None:
    fn: Any = users.check_drift
    assert (
        fn(spec=_spec(), body=BODY, name="bob", namespace="ns", logger=LOGGER) is None
    )
    env["iam"].create_user.assert_not_called()
