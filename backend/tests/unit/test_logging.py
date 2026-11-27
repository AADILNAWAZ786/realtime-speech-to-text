from __future__ import annotations

import json
import logging

from app.core.logging import JsonFormatter, RequestIdFilter, request_id_ctx


def test_json_formatter_includes_request_id_and_extras() -> None:
    token = request_id_ctx.set("req-42")
    try:
        record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "hello %s", ("x",), None)
        record.user_id = "u-1"
        RequestIdFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_ctx.reset(token)

    assert payload["message"] == "hello x"
    assert payload["level"] == "INFO"
    assert payload["request_id"] == "req-42"
    assert payload["user_id"] == "u-1"
    assert "timestamp" in payload


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "app.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]
