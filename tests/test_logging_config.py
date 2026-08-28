"""Test memplex/logging_config.py: structured JSON logging.

Multi-angle evaluation finding (ops #3 FAIL): no structured logging.
This covers the JsonFormatter and the env-driven configure_logging()
switch so daemon surfaces (MCP, HTTP) can emit machine-parseable logs.
"""

import io
import json
import logging
import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.logging_config import (  # noqa: E402
    JsonFormatter,
    configure_logging,
    install_sensitive_data_filters,
)


def _make_record(name="memplex.service", level=logging.INFO, msg="hello", **extra):
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


# ── JsonFormatter ────────────────────────────────────────────────────


def test_json_formatter_emits_parseable_object():
    out = JsonFormatter().format(_make_record(msg="recalled 3 memories"))
    parsed = json.loads(out)
    assert parsed["message"] == "recalled 3 memories"
    assert parsed["level"] == "INFO"
    assert parsed["name"] == "memplex.service"
    assert "timestamp" in parsed


def test_json_formatter_carries_extra_fields():
    out = JsonFormatter().format(_make_record(msg="op", agent="codex", latency_ms=42))
    parsed = json.loads(out)
    assert parsed["agent"] == "codex"
    assert parsed["latency_ms"] == 42


def test_json_formatter_excludes_stdlib_attrs():
    out = JsonFormatter().format(_make_record(msg="x"))
    parsed = json.loads(out)
    # Standard LogRecord internals must not pollute the payload.
    for reserved in ("pathname", "filename", "lineno", "funcName", "thread", "process"):
        assert reserved not in parsed, f"{reserved} leaked into JSON payload"


def test_json_formatter_includes_exception_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    parsed = json.loads(JsonFormatter().format(record))
    assert "exception" in parsed
    assert "ValueError" in parsed["exception"]
    assert "boom" in parsed["exception"]


def test_json_formatter_handles_non_json_extra_value():
    """Non-serialisable extra values are repr'd rather than crashing."""
    out = JsonFormatter().format(_make_record(msg="x", obj=object()))
    parsed = json.loads(out)  # must still parse
    assert "obj" in parsed


def test_json_formatter_redacts_sync_cursor_authorization_and_dsn():
    record = _make_record(
        msg=(
            "GET /sync/v1/changes?cursor=signed-secret-cursor "
            "Authorization: Bearer bearer-secret "
            "postgresql://app:db-password@db.example/memplex"
        ),
        cursor="extra-secret-cursor",
        request_digest="a" * 64,
    )
    rendered = JsonFormatter().format(record)

    assert "signed-secret-cursor" not in rendered
    assert "bearer-secret" not in rendered
    assert "db-password" not in rendered
    assert "extra-secret-cursor" not in rendered
    assert "[REDACTED]" in rendered
    assert "a" * 64 in rendered


def test_json_formatter_timestamp_is_iso8601():
    import re

    parsed = json.loads(JsonFormatter().format(_make_record(msg="x")))
    # ISO-8601 with timezone offset, e.g. 2026-07-25T10:00:00.123456+00:00
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", parsed["timestamp"])


# ── configure_logging ────────────────────────────────────────────────


def _reset_root():
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_configure_logging_default_is_human_readable(caplog):
    _reset_root()
    configure_logging(json_mode=False)
    root = logging.getLogger()
    assert root.handlers
    formatter = root.handlers[0].formatter
    assert not isinstance(formatter, JsonFormatter)


def test_configure_logging_json_mode_installs_json_formatter():
    _reset_root()
    configure_logging(json_mode=True)
    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, JsonFormatter)


def test_configure_logging_is_idempotent_no_duplicate_handlers():
    _reset_root()
    configure_logging(json_mode=True)
    configure_logging(json_mode=True)
    configure_logging(json_mode=False)
    root = logging.getLogger()
    # Re-configuring replaces; must not stack handlers.
    assert len(root.handlers) == 1


def test_configure_logging_respects_level_env(monkeypatch):
    _reset_root()
    monkeypatch.setenv("MEMPLEX_LOG_LEVEL", "DEBUG")
    configure_logging(json_mode=False)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_explicit_level_is_the_fallback(monkeypatch):
    _reset_root()
    monkeypatch.delenv("MEMPLEX_LOG_LEVEL", raising=False)
    configure_logging(json_mode=False, level="WARNING")
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_env_level_beats_explicit_level(monkeypatch):
    _reset_root()
    monkeypatch.setenv("MEMPLEX_LOG_LEVEL", "ERROR")
    configure_logging(json_mode=False, level="DEBUG")
    assert logging.getLogger().level == logging.ERROR


def test_configure_logging_reads_json_env(monkeypatch):
    _reset_root()
    monkeypatch.setenv("MEMPLEX_LOG_JSON", "1")
    configure_logging()  # json_mode=None -> reads env
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)


def test_end_to_end_json_log_is_parseable(caplog):
    """A real logger.info call under JSON mode produces parseable JSON on the wire."""
    _reset_root()
    configure_logging(json_mode=True)
    buf = io.StringIO()
    logging.getLogger().handlers[0].stream = buf
    logging.getLogger("memplex.test").info("real message", extra={"k": "v"})
    line = buf.getvalue().strip()
    parsed = json.loads(line)
    assert parsed["message"] == "real message"
    assert parsed["k"] == "v"


def test_uvicorn_access_handler_redacts_cursor_after_server_configures_logging():
    access = logging.getLogger("uvicorn.access")
    previous_handlers = list(access.handlers)
    previous_propagate = access.propagate
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        access.handlers = [handler]
        access.propagate = False
        install_sensitive_data_filters()
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1",
            "GET",
            "/sync/v1/changes?cursor=signed-access-secret",
            "1.1",
            200,
        )
        rendered = buffer.getvalue()
        assert "signed-access-secret" not in rendered
        assert "cursor=[REDACTED]" in rendered
    finally:
        access.handlers = previous_handlers
        access.propagate = previous_propagate
