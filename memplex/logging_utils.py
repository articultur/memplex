"""Structured logging utilities for Memplex.

Provides JSON-formatted logging with trace_id propagation across
HTTP requests and MCP tool calls.

Optional dependency: Python's built-in json module is always available,
so this module never fails to import.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Context variable for thread/async-safe trace_id propagation
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)

# ── Trace ID helpers ────────────────────────────────────────────────


def generate_trace_id() -> str:
    """Generate a new unique trace ID."""
    return uuid.uuid4().hex[:16]


def get_trace_id() -> Optional[str]:
    """Get the current trace ID from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: Optional[str]) -> None:
    """Set the trace ID in the current context."""
    trace_id_var.set(trace_id)


# ── JSON Formatter ───────────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Python logging formatter that outputs JSON with structured fields.

    Output format::
        {
            "timestamp": "2024-01-01T12:00:00.000Z",
            "level": "INFO",
            "trace_id": "abc123def456",
            "service": "memplex",
            "component": "module.path",
            "event": "log message",
            "extra": {...}
        }

    Fields:
        - timestamp: ISO 8601 format with UTC timezone
        - level: Log level name (DEBUG, INFO, WARNING, ERROR)
        - trace_id: Current trace ID (from contextvar), or null
        - service: Fixed to "memplex"
        - component: Logger name (typically __name__)
        - event: The formatted log message
        - extra: Any additional structured data passed via 'extra' kwarg
    """

    def __init__(
        self,
        service: str = "memplex",
        include_extra: bool = True,
    ) -> None:
        super().__init__()
        self.service = service
        self.include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as a JSON string."""
        trace_id = get_trace_id()

        # Build base record
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "trace_id": trace_id,
            "service": self.service,
            "component": record.name,
            "event": record.getMessage(),
        }

        # Add extra fields if present and enabled
        if self.include_extra:
            extra_fields = {
                key: value
                for key, value in record.__dict__.items()
                if key
                not in (
                    "name",
                    "msg",
                    "args",
                    "created",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "module",
                    "msecs",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "stack_info",
                    "thread",
                    "threadName",
                    "exc_info",
                    "exc_text",
                    "message",
                    "taskName",
                    "trace_id",
                )
            }
            if extra_fields:
                entry["extra"] = extra_fields

        # Add exception info if present
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str, ensure_ascii=False)


class PlainTextFormatter(logging.Formatter):
    """Fallback plain-text formatter that still includes trace_id when available."""

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record with trace_id prefix when available."""
        trace_id = get_trace_id()
        if trace_id is not None:
            record.message = f"[{trace_id}] {record.getMessage()}"
        else:
            record.message = record.getMessage()
        return super().format(record)


# ── Structured Logger ──────────────────────────────────────────────


class StructuredLogger:
    """Logger wrapper that emits JSON logs with trace_id propagation.

    Wraps a standard ``logging.Logger`` and enhances it with:
    - JSON output (when enabled)
    - Automatic trace_id inclusion from context
    - Structured extra fields

    Usage::

        logger = StructuredLogger(logging.getLogger(__name__))

        set_trace_id("req-123")
        logger.info("Processing request")  # JSON: {"event": "Processing request", "trace_id": "req-123", ...}

        # Async context (FastAPI middleware sets trace_id automatically)
        async def handler():
            set_trace_id(request.headers.get("X-Trace-Id"))
            logger.info("Handler started")
    """

    def __init__(
        self,
        logger: logging.Logger,
        json_format: bool = True,
        service: str = "memplex",
    ) -> None:
        self._logger = logger
        self._json_format = json_format
        self._service = service
        self._setup_handler()

    def _setup_handler(self) -> None:
        """Configure the handler with appropriate formatter."""
        handler = logging.StreamHandler(sys.stderr)
        if self._json_format:
            handler.setFormatter(JSONFormatter(service=self._service))
        else:
            handler.setFormatter(
                PlainTextFormatter(
                    fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
        self._logger.addHandler(handler)
        self._logger.propagate = False

    def _log(
        self,
        level: int,
        msg: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Internal log method that preserves trace_id in context."""
        # Ensure trace_id is captured at log time (not just at emit time)
        trace_id = get_trace_id()
        if trace_id is not None:
            kwargs.setdefault("extra", {})["trace_id"] = trace_id
        self._logger.log(level, msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("exc_info", True)
        self._log(logging.ERROR, msg, *args, **kwargs)


# ── Setup helpers ────────────────────────────────────────────────────


def setup_logging(
    json_format: bool = True,
    level: str = "INFO",
    service: str = "memplex",
) -> None:
    """Configure the root logger with structured logging.

    Parameters
    ----------
    json_format:
        If True, use JSON formatter; otherwise plain text.
    level:
        Log level (DEBUG, INFO, WARNING, ERROR).
    service:
        Service name for log entries.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplication
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(JSONFormatter(service=service))
    else:
        handler.setFormatter(
            PlainTextFormatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root_logger.addHandler(handler)
