"""Kopf handlers for the SeaweedfsBucket CRD."""

from __future__ import annotations

from typing import Any

import kopf
from botocore.exceptions import ClientError
from common import (
    CRD_GROUP,
    CRD_VERSION,
    bucket_exists,
    ensure_bucket,
    error_code,
    get_bucket_versioning_enabled,
    s3_client,
    set_bucket_versioning,
)

PLURAL = "seaweedfsbuckets"
_UNSUPPORTED_CODES = frozenset({"NotImplemented", "XNotImplemented"})


def _upsert_bucket(spec: kopf.Spec, name: str, logger: kopf.Logger) -> None:
    """Ensure the bucket exists and its versioning matches the spec."""
    versioning = bool(spec.get("versioning", False))
    client = s3_client()
    ensure_bucket(client, name, logger)
    if get_bucket_versioning_enabled(client, name) == versioning:
        return
    try:
        set_bucket_versioning(client, name, versioning)
    except kopf.TemporaryError as exc:
        cause = exc.__cause__
        if (
            versioning
            and isinstance(cause, ClientError)
            and error_code(cause) in _UNSUPPORTED_CODES
        ):
            msg = f"SeaweedFS cannot enable versioning on bucket {name!r}: {cause}"
            raise kopf.PermanentError(msg) from cause
        raise


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _reconcile(
    spec: kopf.Spec, name: str, logger: kopf.Logger, patch: kopf.Patch
) -> dict[str, Any]:
    _upsert_bucket(spec, name, logger)
    patch.status["ready"] = True
    return {"bucket": name, "ready": True}


@kopf.on.create(CRD_GROUP, CRD_VERSION, PLURAL, retries=5, backoff=30, timeout=300)
def create_fn(
    spec: kopf.Spec,
    name: str,
    logger: kopf.Logger,
    patch: kopf.Patch,
    **_: Any,
) -> dict[str, Any]:
    return _reconcile(spec, name, logger, patch)


@kopf.on.resume(CRD_GROUP, CRD_VERSION, PLURAL)
def resume_fn(
    spec: kopf.Spec,
    name: str,
    logger: kopf.Logger,
    patch: kopf.Patch,
    **_: Any,
) -> dict[str, Any]:
    return _reconcile(spec, name, logger, patch)


@kopf.on.update(CRD_GROUP, CRD_VERSION, PLURAL, field="spec", retries=3, backoff=15)
def update_fn(
    spec: kopf.Spec,
    name: str,
    logger: kopf.Logger,
    patch: kopf.Patch,
    **_: Any,
) -> dict[str, Any]:
    return _reconcile(spec, name, logger, patch)


@kopf.on.delete(CRD_GROUP, CRD_VERSION, PLURAL, retries=3, backoff=15, timeout=120)
def delete_fn(name: str, logger: kopf.Logger, **_: Any) -> None:
    """Log only: the bucket and its objects are intentionally kept."""
    logger.info(
        "SeaweedfsBucket %r deleted; SeaweedFS bucket preserved to prevent data loss",
        name,
    )


@kopf.timer(CRD_GROUP, CRD_VERSION, PLURAL, interval=300, initial_delay=60, idle=30)
def check_drift(
    spec: kopf.Spec,
    name: str,
    logger: kopf.Logger,
    **_: Any,
) -> dict[str, Any] | None:
    """Recreate the bucket if it went missing, or fix versioning drift."""
    client = s3_client()
    if not bucket_exists(client, name):
        logger.warning("Drift detected: bucket %r is missing, recreating", name)
        _upsert_bucket(spec, name, logger)
        return {"bucket": name, "ready": True, "drift": True, "driftReason": "missing"}

    if get_bucket_versioning_enabled(client, name) != bool(
        spec.get("versioning", False)
    ):
        logger.warning("Drift detected: bucket %r versioning differs, fixing", name)
        _upsert_bucket(spec, name, logger)
        return {
            "bucket": name,
            "ready": True,
            "drift": True,
            "driftReason": "config mismatch",
        }
    return None
