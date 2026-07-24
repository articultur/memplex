"""Test memplex/worker.py: TaskStore persistence and BackgroundWorker lifecycle.

Previously zero direct coverage (test evaluation #1). TaskStore is driven
against a real tmp_path file; BackgroundWorker is exercised via its
public lifecycle (start/submit/status/stop) and its dead-letter counter.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from memplex.models import BackgroundTask, TaskInfo, TaskStatus  # noqa: E402
from memplex.worker import BackgroundWorker, TaskStore  # noqa: E402


def _info(tid="t1", status=TaskStatus.PENDING, payload=None):
    """Build a TaskInfo with the correct field name (task_id, not id)."""
    return TaskInfo(
        task_id=tid,
        task_type=BackgroundTask.BUILD_INDEX,
        status=status,
        created_at=datetime.now(),
        payload=payload or {},
    )


# ── TaskStore ────────────────────────────────────────────────────────


def test_task_store_save_and_get_roundtrip(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    store.save(_info(tid="t1", status=TaskStatus.PENDING, payload={"func_id": "f1"}))
    got = store.get("t1")
    assert got is not None
    assert got.task_id == "t1"
    assert got.status == TaskStatus.PENDING
    assert got.payload == {"func_id": "f1"}


def test_task_store_get_missing_returns_none(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    assert store.get("nope") is None


def test_task_store_persists_across_instances(tmp_path):
    """A new TaskStore on the same path reloads saved tasks."""
    path = tmp_path / "tasks.json"
    TaskStore(path).save(_info(tid="persist-1", status=TaskStatus.RUNNING))
    s2 = TaskStore(path)  # reload
    got = s2.get("persist-1")
    assert got is not None
    assert got.status == TaskStatus.RUNNING


def test_task_store_list_by_status(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    store.save(_info(tid="a", status=TaskStatus.RUNNING))
    store.save(_info(tid="b", status=TaskStatus.FAILED))
    store.save(_info(tid="c", status=TaskStatus.RUNNING))
    running = store.list_by_status(TaskStatus.RUNNING)
    assert {t.task_id for t in running} == {"a", "c"}
    failed = store.list_by_status(TaskStatus.FAILED)
    assert {t.task_id for t in failed} == {"b"}


# ── BackgroundWorker lifecycle ───────────────────────────────────────


def test_worker_starts_and_stops_cleanly(tmp_path):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    assert worker._running is False
    worker.start()
    assert worker._running is True
    worker.stop(timeout=5.0)
    assert worker._running is False


def test_worker_start_is_idempotent(tmp_path):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    worker.start()
    worker.start()  # second start must not spawn another thread / not raise
    assert worker._running is True
    worker.stop(timeout=5.0)


def test_worker_submit_accepts_task_without_crashing(tmp_path):
    """BUILD_INDEX with no compaction pipeline is best-effort; submit must
    accept the task and the worker stays healthy."""
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    worker.start()
    worker.submit(BackgroundTask.BUILD_INDEX, {"func_id": "f1"})
    assert worker._running is True
    worker.stop(timeout=5.0)


def test_worker_dead_letters_pending_counts_failed_tasks(tmp_path):
    """Failed tasks persisted in the TaskStore are reported as dead letters."""
    path = tmp_path / "tasks.json"
    TaskStore(path).save(_info(tid="dead1", status=TaskStatus.FAILED))
    worker = BackgroundWorker(storage_path=path)
    assert worker.dead_letters_pending() == 1


def test_worker_last_compaction_starts_none(tmp_path):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    assert worker.last_compaction is None


def test_worker_get_status_raises_for_unknown_task(tmp_path):
    """get_status raises KeyError (not silent None) for an unknown task id."""
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    with pytest.raises(KeyError):
        worker.get_status("does-not-exist")
