"""Structured logging configuration for Memplex.

Provides an opt-in JSON formatter so daemon-style surfaces (MCP server,
HTTP API) emit machine-parseable logs suitable for log correlation and
field-level filtering. Existing ``logging.getLogger(__name__)`` call sites
are unchanged; only the root handler formatting is swapped when the
operator opts in via the ``MEMPLEX_LOG_JSON`` environment variable.

Usage::

    from memplex.logging_config import configure_logging
    configure_logging()  # call once at process entry (CLI main, MCP run)

Operator controls:
- ``MEMPLEX_LOG_JSON=1`` -- emit one JSON object per log record.
- ``MEMPLEX_LOG_LEVEL`` -- DEBUG/INFO/WARNING (default INFO).
- Default (no env) -- unchanged stdlib human-readable formatting.

The JSON schema is stable for log pipelines::

    {
        "timestamp": "2026-07-25T10:00:00.123456+00:00",
        "level": "INFO",
        "name": "memplex.service",
        "message": "...",
        ...optional extra fields from record.__dict__...
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

# Reserved attributes that should not leak into the JSON "extra" payload.
_STDLOG_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|bearer|cursor|dsn|password|secret|token)",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"([?&](?:authorization|api[_-]?key|cursor|password|secret|token)=)[^&\s]+",
    re.IGNORECASE,
)
_BEARER_SECRET = re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE)
_AUTH_HEADER_SECRET = re.compile(
    r"(\bAuthorization\s*:\s*)(?!Bearer\s)[^\s,;]+", re.IGNORECASE
)
_URI_CREDENTIALS = re.compile(
    r"(\b[a-z][a-z0-9+.-]*://)[^/@\s]+(?::[^@\s]*)?@",
    re.IGNORECASE,
)


def _redact_text(value: str) -> str:
    value = _QUERY_SECRET.sub(rf"\1{_REDACTED}", value)
    value = _BEARER_SECRET.sub(rf"\1{_REDACTED}", value)
    value = _AUTH_HEADER_SECRET.sub(rf"\1{_REDACTED}", value)
    return _URI_CREDENTIALS.sub(rf"\1{_REDACTED}@", value)


def _redact_value(value, *, key: str = ""):
    if key and _SENSITIVE_KEY.search(key):
        return _REDACTED
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            item_key: _redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


class _SensitiveDataFilter(logging.Filter):
    """Redact common credentials/cursors before any formatter sees them."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_text(record.msg)
        if isinstance(record.args, dict):
            record.args = _redact_value(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_value(item) for item in record.args)
        for key in tuple(record.__dict__):
            if key.startswith("_") or key in _STDLOG_ATTRS:
                continue
            record.__dict__[key] = _redact_value(record.__dict__[key], key=key)
        return True


def install_sensitive_data_filters() -> None:
    """Attach redaction to already-configured daemon/access handlers.

    Uvicorn installs its access handlers independently of the root logger.
    Calling this from the application lifespan covers those handlers after
    Uvicorn's logging configuration has run, including cursor-bearing query
    strings in access-log argument tuples.
    """
    loggers = (logging.getLogger(), logging.getLogger("uvicorn.access"))
    for target in loggers:
        for handler in target.handlers:
            if not any(isinstance(item, _SensitiveDataFilter) for item in handler.filters):
                handler.addFilter(_SensitiveDataFilter())


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        # ISO-8601, timezone-aware timestamp.
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload = {
            "timestamp": timestamp,
            "level": record.levelname,
            "name": record.name,
            "message": _redact_text(record.getMessage()),
        }
        # Carry non-standard record attributes as extra fields (structured
        # logging via logger.info("...", extra={"k": v})). Skip private and
        # reserved stdlib keys.
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STDLOG_ATTRS:
                continue
            value = _redact_value(value, key=key)
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = _redact_text(
                self.formatException(record.exc_info)
            )
        return json.dumps(payload, ensure_ascii=False, default=str)


def _truthy(value: Optional[str]) -> bool:
    return bool(value) and value.lower() in ("1", "true", "yes", "on")


def configure_logging(json_mode: Optional[bool] = None, level: Optional[str] = None) -> None:
    """Configure the root logger.

    Parameters
    ----------
    json_mode:
        When ``None`` (default), the mode is read from the
        ``MEMPLEX_LOG_JSON`` environment variable. Pass an explicit bool
        to override the env var (useful in tests).
    level:
        Fallback log-level name (e.g. from ``MemplexConfig.logging.level``).
        The ``MEMPLEX_LOG_LEVEL`` environment variable takes precedence;
        when neither is set the level defaults to ``INFO``.

    Idempotent in intent: re-configuring replaces the root logger's
    handlers rather than appending, so calling it more than once does not
    duplicate log lines.
    """
    if json_mode is None:
        json_mode = _truthy(os.environ.get("MEMPLEX_LOG_JSON"))

    level_name = (os.environ.get("MEMPLEX_LOG_LEVEL") or (level or "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    # Clear any handlers from a previous configure/basicConfig call so we
    # do not stack formatters on repeat invocations.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.addFilter(_SensitiveDataFilter())
    if json_mode:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    install_sensitive_data_filters()
