"""JSON log formatting."""

from __future__ import annotations

import json
import logging
import sys

from logs import JsonFormatter, configure_logging


def _record(k8s_ref: dict[str, str] | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        "x.y", logging.INFO, __file__, 1, "hi %s", ("there",), None
    )
    if k8s_ref:
        setattr(record, "k8s_ref", k8s_ref)  # noqa: B010
    return record


def test_record_is_one_json_line_with_workgraph_keys() -> None:
    line = JsonFormatter().format(_record())
    assert "\n" not in line
    assert set(json.loads(line)) == {"time", "level", "logger", "message"}
    assert json.loads(line)["message"] == "hi there"


def _boom() -> None:
    msg = "boom"
    raise ValueError(msg)


def test_exc_included_with_exc_info() -> None:
    record = _record()
    try:
        _boom()
    except ValueError:
        record.exc_info = sys.exc_info()
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exc"]


def test_object_from_k8s_ref() -> None:
    ref = {"namespace": "ns", "name": "n"}
    assert json.loads(JsonFormatter().format(_record(ref)))["object"] == "ns/n"
    ref = {"name": "n"}
    assert json.loads(JsonFormatter().format(_record(ref)))["object"] == "n"


def test_configure_logging_replaces_root_handlers() -> None:
    root = logging.getLogger()
    saved = root.handlers[:]
    try:
        root.handlers[:] = [logging.StreamHandler(), logging.StreamHandler()]
        configure_logging()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers[:] = saved
