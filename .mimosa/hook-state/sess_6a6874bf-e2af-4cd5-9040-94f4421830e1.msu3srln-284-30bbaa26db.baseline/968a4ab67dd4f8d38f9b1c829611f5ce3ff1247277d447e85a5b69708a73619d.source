"""Durable, low-information health state for host runtime adapters.

Host hook contracts deliberately swallow runtime exceptions so a memory outage
does not interrupt the host.  This sidecar preserves that outage for operators
without retaining exception text, user content, paths, or credentials.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

RUNTIME_STATUS_FILENAME = ".memplex-runtime-status.json"
RUNTIME_STATUS_SCHEMA_VERSION = 2
_SUPPORTED_AGENTS = frozenset({"codex", "claude-code", "hermes", "openclaw"})
_SUPPORTED_OPERATIONS = frozenset(
    {
        "capture",
        "recall",
        "prefetch",
        "session_start",
        "summarize",
    }
)
_FAILURE_CODE = "runtime_operation_failed"


class _RuntimeStatusLockError(RuntimeError):
    """Expose one redacted failure for every lock setup/acquisition error."""


def runtime_status_path(root: str | Path) -> Path:
    """Return the stable, host-local sidecar path below one host root."""

    return Path(root).expanduser().resolve(strict=False) / RUNTIME_STATUS_FILENAME


def record_runtime_failure(
    path: str | Path,
    *,
    agent: str,
    operation: str,
    error: BaseException | None = None,
) -> None:
    """Persist a redacted degraded state after a real host runtime failure.

    ``error`` is accepted only so callers can record the failure at their
    exception boundary.  Its text and type are intentionally never persisted.
    """

    del error
    selected_agent = _validate_agent(agent)
    selected_operation = _validate_operation(operation)
    status_path = Path(path)
    with _runtime_status_lock(status_path, exclusive=True):
        current = _read_valid_payload(status_path, agent=selected_agent)
        pending_operations = set(current["pending_operations"]) if current is not None else set()
        if current is None:
            legacy = _read_legacy_v1_payload(status_path, agent=selected_agent)
            if legacy is not None:
                pending_operations.add(legacy["operation"])
        pending_operations.add(selected_operation)
        payload = _degraded_payload(selected_agent, pending_operations)
        _write_json_atomic(status_path, payload)


def clear_runtime_status_on_success(
    path: str | Path,
    *,
    agent: str,
    operation: str,
    completed: bool,
) -> bool:
    """Clear only the matching persisted failure after a real success.

    Hook no-ops must call this with ``completed=False`` (or not call it at
    all), so they cannot accidentally make a degraded host look healthy.
    """

    if not completed:
        return False
    selected_agent = _validate_agent(agent)
    selected_operation = _validate_operation(operation)
    status_path = Path(path)
    try:
        with _runtime_status_lock(status_path, exclusive=True):
            payload = _read_valid_payload(status_path, agent=selected_agent)
            if payload is None or selected_operation not in payload["pending_operations"]:
                return False
            remaining_operations = set(payload["pending_operations"])
            remaining_operations.remove(selected_operation)
            if remaining_operations:
                _write_json_atomic(
                    status_path,
                    _degraded_payload(selected_agent, remaining_operations),
                )
            else:
                status_path.unlink()
                _fsync_directory(status_path.parent)
    except (OSError, _RuntimeStatusLockError):
        return False
    return True


def read_runtime_status(path: str | Path, *, agent: str) -> dict[str, str | None]:
    """Return a fail-closed redacted health projection for status consumers."""

    selected_agent = _validate_agent(agent)
    status_path = Path(path)
    try:
        with _runtime_status_lock(status_path, exclusive=False):
            try:
                status_path.lstat()
            except FileNotFoundError:
                return {"reason": None, "state": "healthy"}
            except OSError:
                return {"reason": "state_unreadable", "state": "degraded"}
            payload = _read_valid_payload(status_path, agent=selected_agent)
            if payload is None:
                return {"reason": "state_unreadable", "state": "degraded"}
    except _RuntimeStatusLockError:
        return {"reason": "state_unreadable", "state": "degraded"}
    return {"reason": _FAILURE_CODE, "state": "degraded"}


def _runtime_status_lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


@contextmanager
def _runtime_status_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Lock every sidecar read/modify/write on one persistent regular file."""

    lock_path = _runtime_status_lock_path(path)
    descriptor: int | None = None
    lock_failed = False
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("runtime status lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except (OSError, ValueError):
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        lock_failed = True
    if lock_failed:
        raise _RuntimeStatusLockError("runtime status lock unavailable")
    assert descriptor is not None
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _validate_agent(agent: str) -> str:
    selected = str(agent or "").strip().lower()
    if selected not in _SUPPORTED_AGENTS:
        raise ValueError(f"unsupported runtime-status agent: {agent!r}")
    return selected


def _validate_operation(operation: str) -> str:
    selected = str(operation or "").strip().lower()
    if selected not in _SUPPORTED_OPERATIONS:
        raise ValueError(f"unsupported runtime-status operation: {operation!r}")
    return selected


def _read_valid_payload(path: Path, *, agent: str) -> dict[str, Any] | None:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "agent",
        "failure_code",
        "pending_operations",
        "schema_version",
        "state",
    }:
        return None
    if (
        value.get("schema_version") != RUNTIME_STATUS_SCHEMA_VERSION
        or value.get("state") != "degraded"
        or value.get("agent") != agent
        or value.get("failure_code") != _FAILURE_CODE
    ):
        return None
    pending_operations = value.get("pending_operations")
    if (
        not isinstance(pending_operations, list)
        or not pending_operations
        or any(type(operation) is not str for operation in pending_operations)
    ):
        return None
    try:
        validated_operations = [_validate_operation(operation) for operation in pending_operations]
    except ValueError:
        return None
    if pending_operations != validated_operations or pending_operations != sorted(
        set(pending_operations)
    ):
        return None
    return {**value, "pending_operations": validated_operations}


def _read_legacy_v1_payload(path: Path, *, agent: str) -> dict[str, str] | None:
    """Read only the previous exact payload shape for lossless failure migration."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "agent",
        "failure_code",
        "operation",
        "schema_version",
        "state",
    }:
        return None
    if (
        value.get("schema_version") != 1
        or value.get("state") != "degraded"
        or value.get("agent") != agent
        or value.get("failure_code") != _FAILURE_CODE
    ):
        return None
    try:
        operation = _validate_operation(value["operation"])
    except (TypeError, ValueError):
        return None
    return {"operation": operation}


def _degraded_payload(agent: str, pending_operations: set[str]) -> dict[str, Any]:
    """Build the exact redacted v2 sidecar payload."""

    if not pending_operations:
        raise ValueError("degraded runtime status requires a pending operation")
    return {
        "agent": agent,
        "failure_code": _FAILURE_CODE,
        "pending_operations": sorted(pending_operations),
        "schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
        "state": "degraded",
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
