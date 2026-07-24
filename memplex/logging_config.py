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


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        # ISO-8601, timezone-aware timestamp.
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload = {
            "timestamp": timestamp,
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # Carry non-standard record attributes as extra fields (structured
        # logging via logger.info("...", extra={"k": v})). Skip private and
        # reserved stdlib keys.
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STDLOG_ATTRS:
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _truthy(value: Optional[str]) -> bool:
    return bool(value) and value.lower() in ("1", "true", "yes", "on")


def configure_logging(json_mode: Optional[bool] = None) -> None:
    """Configure the root logger.

    Parameters
    ----------
    json_mode:
        When ``None`` (default), the mode is read from the
        ``MEMPLEX_LOG_JSON`` environment variable. Pass an explicit bool
        to override the env var (useful in tests).

    Idempotent in intent: re-configuring replaces the root logger's
    handlers rather than appending, so calling it more than once does not
    duplicate log lines.
    """
    if json_mode is None:
        json_mode = _truthy(os.environ.get("MEMPLEX_LOG_JSON"))

    level_name = (os.environ.get("MEMPLEX_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    # Clear any handlers from a previous configure/basicConfig call so we
    # do not stack formatters on repeat invocations.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    if json_mode:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
