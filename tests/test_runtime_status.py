"""Host-runtime degraded-state persistence contract."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from pathlib import Path
from threading import BrokenBarrierError
from unittest.mock import Mock

import pytest

from memplex.adapters import runtime_status as runtime_status_module
from memplex.adapters.runtime_status import (
    clear_runtime_status_on_success,
    read_runtime_status,
    record_runtime_failure,
    runtime_status_path,
)


def _record_failure_after_barrier(
    path: str,
    operation: str,
    barrier: multiprocessing.synchronize.Barrier,
) -> None:
    """Force two unlocked readers to observe the same pre-write snapshot."""
    original_read = runtime_status_module._read_valid_payload

    def synchronized_read(status_path, *, agent):
        payload = original_read(status_path, agent=agent)
        try:
            barrier.wait()
        except BrokenBarrierError:
            pass
        return payload

    runtime_status_module._read_valid_payload = synchronized_read
    record_runtime_failure(path, agent="codex", operation=operation)


def _clear_capture_after_recall_write(
    path: str,
    capture_read: multiprocessing.synchronize.Event,
    recall_written: multiprocessing.synchronize.Event,
) -> None:
    """Reproduce the stale capture-success unlink from the unlocked implementation."""
    original_read = runtime_status_module._read_valid_payload

    def delayed_read(status_path, *, agent):
        payload = original_read(status_path, agent=agent)
        capture_read.set()
        recall_written.wait(timeout=0.75)
        return payload

    runtime_status_module._read_valid_payload = delayed_read
    clear_runtime_status_on_success(
        path,
        agent="codex",
        operation="capture",
        completed=True,
    )


def _record_recall_after_capture_read(
    path: str,
    capture_read: multiprocessing.synchronize.Event,
    recall_written: multiprocessing.synchronize.Event,
) -> None:
    if not capture_read.wait(timeout=5):
        raise RuntimeError("capture reader did not reach the controlled interleave")
    record_runtime_failure(path, agent="codex", operation="recall")
    recall_written.set()


def _join_processes(*processes: multiprocessing.Process) -> None:
    failure: BaseException | None = None
    try:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
                pytest.fail(f"runtime-status child process hung: {process.name}")
            assert process.exitcode == 0
    except BaseException as exc:  # noqa: BLE001 -- preserve the first failure after owned-child cleanup
        failure = exc
    finally:
        for process in processes:
            if process.pid is None:
                continue
            reaped = False
            for stop in (process.terminate, process.kill):
                try:
                    if process.is_alive():
                        stop()
                except BaseException as exc:  # noqa: BLE001 -- retry this child and retain the first failure
                    if failure is None:
                        failure = exc
                try:
                    process.join(timeout=2)
                    reaped = not process.is_alive()
                except BaseException as exc:  # noqa: BLE001 -- kill/reap remains independent of terminate/join
                    if failure is None:
                        failure = exc
                if reaped:
                    break
            if not reaped:
                message = f"runtime-status child not reaped: {process.name}"
                if failure is None:
                    failure = AssertionError(message)
                else:
                    failure.add_note(message)
    if failure is not None:
        raise failure


@pytest.mark.parametrize("first_failure", ["timeout", "nonzero"])
@pytest.mark.parametrize("needs_kill", [False, True])
def test_join_processes_reaps_later_owned_child_after_failure(first_failure, needs_kill):
    first = Mock(spec=multiprocessing.Process, pid=101, exitcode=1)
    later = Mock(spec=multiprocessing.Process, pid=102, exitcode=None)
    first.name = "first"
    later.name = "later"
    first.is_alive.return_value = first_failure == "timeout"
    first.terminate.side_effect = lambda: setattr(first.is_alive, "return_value", False)
    later.is_alive.return_value = True
    if not needs_kill:
        later.terminate.side_effect = lambda: setattr(later.is_alive, "return_value", False)
    later.kill.side_effect = lambda: setattr(later.is_alive, "return_value", False)

    error = pytest.fail.Exception if first_failure == "timeout" else AssertionError
    with pytest.raises(error) as caught:
        _join_processes(first, later)

    if first_failure == "timeout":
        assert str(caught.value) == "runtime-status child process hung: first"
    assert not later.is_alive()
    later.terminate.assert_called_once_with()
    later.join.assert_called()
    assert later.kill.called is needs_kill


@pytest.mark.parametrize("cleanup_operation", ["terminate", "join"])
@pytest.mark.parametrize("cleanup_error", [KeyboardInterrupt, OSError])
def test_join_processes_retries_interrupted_owned_child(cleanup_operation, cleanup_error):
    primary = AssertionError("original runtime failure")
    first = Mock(spec=multiprocessing.Process, pid=101, exitcode=1)
    interrupted = Mock(spec=multiprocessing.Process, pid=102, exitcode=None)
    later = Mock(spec=multiprocessing.Process, pid=103, exitcode=None)
    unstarted = Mock(spec=multiprocessing.Process, pid=None)
    first.name, interrupted.name, later.name = "first", "interrupted", "later"
    first.join.side_effect = [primary, None]
    first.is_alive.return_value = False
    interrupted.is_alive.return_value = True
    getattr(interrupted, cleanup_operation).side_effect = [cleanup_error("cleanup fault"), None]
    interrupted.kill.side_effect = lambda: setattr(interrupted.is_alive, "return_value", False)
    later.is_alive.return_value = True
    later.terminate.side_effect = lambda: setattr(later.is_alive, "return_value", False)

    with pytest.raises(AssertionError) as caught:
        _join_processes(first, interrupted, later, unstarted)

    assert caught.value is primary
    assert not interrupted.is_alive()
    assert not later.is_alive()
    interrupted.kill.assert_called_once_with()
    interrupted.join.assert_called_with(timeout=2)
    later.join.assert_called_with(timeout=2)
    assert not unstarted.mock_calls
    assert all(call.kwargs["timeout"] in {2, 10} for call in first.join.call_args_list)


def test_join_processes_reports_persistent_unreaped_child():
    primary = AssertionError("original runtime failure")
    first = Mock(spec=multiprocessing.Process, pid=101, exitcode=1)
    unreaped = Mock(spec=multiprocessing.Process, pid=102, exitcode=None)
    later = Mock(spec=multiprocessing.Process, pid=103, exitcode=None)
    first.name, unreaped.name, later.name = "first", "unreaped", "later"
    first.join.side_effect = [primary, None]
    first.is_alive.return_value = False
    unreaped.is_alive.return_value = True
    unreaped.terminate.side_effect = OSError("terminate failed")
    unreaped.kill.side_effect = OSError("kill failed")
    later.is_alive.return_value = True
    later.terminate.side_effect = lambda: setattr(later.is_alive, "return_value", False)

    with pytest.raises(AssertionError) as caught:
        _join_processes(first, unreaped, later)

    assert caught.value is primary
    assert "runtime-status child not reaped: unreaped" in getattr(caught.value, "__notes__", [])
    unreaped.kill.assert_called_once_with()
    unreaped.join.assert_called_with(timeout=2)
    assert not later.is_alive()


def _read_status_in_child(path: str, results: multiprocessing.Queue) -> None:
    results.put(read_runtime_status(path, agent="codex"))


def test_runtime_failure_persists_a_redacted_degraded_state_atomically(tmp_path):
    """Replacing a host exception with raw details must not leak into its sidecar."""
    path = runtime_status_path(tmp_path)

    record_runtime_failure(
        path,
        agent="codex",
        operation="recall",
        error=RuntimeError("Bearer super-secret-token at /Users/alice/private.sqlite"),
    )

    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload == {
        "agent": "codex",
        "failure_code": "runtime_operation_failed",
        "pending_operations": ["recall"],
        "schema_version": 2,
        "state": "degraded",
    }
    assert "super-secret-token" not in raw
    assert "/Users/alice" not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lock_path = runtime_status_module._runtime_status_lock_path(path)
    assert stat.S_ISREG(lock_path.stat().st_mode)
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    assert read_runtime_status(path, agent="codex") == {
        "reason": "runtime_operation_failed",
        "state": "degraded",
    }


def test_only_a_real_success_of_the_same_operation_clears_degraded_state(tmp_path):
    """A successful recall must not hide a failed capture, and a no-op clears nothing."""
    path = runtime_status_path(tmp_path)
    record_runtime_failure(path, agent="claude-code", operation="capture")

    assert (
        clear_runtime_status_on_success(
            path, agent="claude-code", operation="recall", completed=True
        )
        is False
    )
    assert (
        clear_runtime_status_on_success(
            path, agent="claude-code", operation="capture", completed=False
        )
        is False
    )
    assert read_runtime_status(path, agent="claude-code")["state"] == "degraded"

    assert (
        clear_runtime_status_on_success(
            path, agent="claude-code", operation="capture", completed=True
        )
        is True
    )
    assert not path.exists()
    assert read_runtime_status(path, agent="claude-code") == {
        "reason": None,
        "state": "healthy",
    }


def test_success_clears_only_its_operation_when_multiple_failures_are_pending(tmp_path):
    """A recall recovery must leave an earlier capture outage visible to operators."""
    path = runtime_status_path(tmp_path)

    record_runtime_failure(path, agent="codex", operation="capture")
    record_runtime_failure(path, agent="codex", operation="recall")

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "agent": "codex",
        "failure_code": "runtime_operation_failed",
        "pending_operations": ["capture", "recall"],
        "schema_version": 2,
        "state": "degraded",
    }
    assert (
        clear_runtime_status_on_success(path, agent="codex", operation="recall", completed=True)
        is True
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "agent": "codex",
        "failure_code": "runtime_operation_failed",
        "pending_operations": ["capture"],
        "schema_version": 2,
        "state": "degraded",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_runtime_status(path, agent="codex") == {
        "reason": "runtime_operation_failed",
        "state": "degraded",
    }
    assert (
        clear_runtime_status_on_success(path, agent="codex", operation="capture", completed=True)
        is True
    )
    assert not path.exists()


def test_concurrent_failures_preserve_both_pending_operations(tmp_path):
    """Two failures reading one snapshot must merge instead of last-writer winning."""
    path = runtime_status_path(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2, timeout=0.75)
    capture = context.Process(
        target=_record_failure_after_barrier,
        args=(str(path), "capture", barrier),
    )
    recall = context.Process(
        target=_record_failure_after_barrier,
        args=(str(path), "recall", barrier),
    )

    capture.start()
    recall.start()
    _join_processes(capture, recall)

    assert json.loads(path.read_text(encoding="utf-8"))["pending_operations"] == [
        "capture",
        "recall",
    ]


def test_capture_success_cannot_erase_an_interleaved_recall_failure(tmp_path):
    """A stale capture success must not unlink a concurrently recorded recall outage."""
    path = runtime_status_path(tmp_path)
    record_runtime_failure(path, agent="codex", operation="capture")
    context = multiprocessing.get_context("spawn")
    capture_read = context.Event()
    recall_written = context.Event()
    clear_capture = context.Process(
        target=_clear_capture_after_recall_write,
        args=(str(path), capture_read, recall_written),
    )
    record_recall = context.Process(
        target=_record_recall_after_capture_read,
        args=(str(path), capture_read, recall_written),
    )

    clear_capture.start()
    record_recall.start()
    _join_processes(clear_capture, record_recall)

    assert json.loads(path.read_text(encoding="utf-8"))["pending_operations"] == ["recall"]
    assert read_runtime_status(path, agent="codex") == {
        "reason": "runtime_operation_failed",
        "state": "degraded",
    }


def test_legacy_single_operation_sidecar_fails_closed(tmp_path):
    """A v1 sidecar cannot be cleared or presented as healthy after the schema upgrade."""
    path = runtime_status_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent": "codex",
                "failure_code": "runtime_operation_failed",
                "operation": "recall",
                "schema_version": 1,
                "state": "degraded",
            }
        ),
        encoding="utf-8",
    )

    assert (
        clear_runtime_status_on_success(path, agent="codex", operation="recall", completed=True)
        is False
    )
    assert read_runtime_status(path, agent="codex") == {
        "reason": "state_unreadable",
        "state": "degraded",
    }


def test_unreadable_or_invalid_sidecar_fails_closed_as_degraded(tmp_path):
    """A broken sidecar must not be mistaken for a healthy host runtime."""
    path = runtime_status_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"state":"healthy"', encoding="utf-8")

    assert read_runtime_status(path, agent="hermes") == {
        "reason": "state_unreadable",
        "state": "degraded",
    }

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "degraded",
                "agent": "other-host",
                "operation": "capture",
                "failure_code": "runtime_operation_failed",
            }
        ),
        encoding="utf-8",
    )
    assert read_runtime_status(path, agent="hermes") == {
        "reason": "state_unreadable",
        "state": "degraded",
    }


@pytest.mark.parametrize("invalid_kind", ["fifo", "symlink"])
def test_non_regular_status_sidecar_fails_closed_without_blocking(tmp_path, invalid_kind):
    """Status reads must reject FIFOs and symlinks before opening their contents."""
    path = runtime_status_path(tmp_path)
    if invalid_kind == "fifo":
        os.mkfifo(path)
    else:
        target = Path(tmp_path) / "attacker-controlled-status"
        target.write_text('{"state":"healthy"}', encoding="utf-8")
        path.symlink_to(target)
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    reader = context.Process(target=_read_status_in_child, args=(str(path), results))

    reader.start()
    _join_processes(reader)

    assert results.get(timeout=1) == {
        "reason": "state_unreadable",
        "state": "degraded",
    }


def test_lock_failure_is_redacted_and_all_public_operations_fail_closed(tmp_path, monkeypatch):
    """Lock diagnostics must not expose OS paths, credentials, or raw exceptions."""
    path = runtime_status_path(tmp_path)
    record_runtime_failure(path, agent="codex", operation="recall")

    def fail_lock(_descriptor, _operation):
        raise OSError("Bearer lock-secret at /Users/alice/private")

    monkeypatch.setattr(runtime_status_module.fcntl, "flock", fail_lock)

    with pytest.raises(RuntimeError) as raised:
        record_runtime_failure(
            path,
            agent="codex",
            operation="capture",
            error=RuntimeError("operation-secret"),
        )
    assert str(raised.value) == "runtime status lock unavailable"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert (
        clear_runtime_status_on_success(
            path,
            agent="codex",
            operation="recall",
            completed=True,
        )
        is False
    )
    assert read_runtime_status(path, agent="codex") == {
        "reason": "state_unreadable",
        "state": "degraded",
    }


@pytest.mark.parametrize("invalid_kind", ["fifo", "symlink"])
def test_non_regular_lock_sidecar_is_rejected_without_blocking(tmp_path, invalid_kind):
    """A FIFO or symlink lock target cannot enter the runtime-status lock domain."""
    path = runtime_status_path(tmp_path)
    lock_path = runtime_status_module._runtime_status_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if invalid_kind == "fifo":
        os.mkfifo(lock_path)
    else:
        target = Path(tmp_path) / "attacker-controlled-lock"
        target.write_text("", encoding="utf-8")
        lock_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="^runtime status lock unavailable$"):
        record_runtime_failure(path, agent="codex", operation="capture")
    assert read_runtime_status(path, agent="codex") == {
        "reason": "state_unreadable",
        "state": "degraded",
    }
