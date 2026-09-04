"""Test memplex/worker.py: TaskStore persistence and BackgroundWorker lifecycle.

Previously zero direct coverage (test evaluation #1). TaskStore is driven
against a real tmp_path file; BackgroundWorker is exercised via its
public lifecycle (start/submit/status/stop) and its dead-letter counter.
"""

import multiprocessing
import os
import threading
from pathlib import Path
from queue import Full

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import UTC, datetime, timedelta, timezone

import pytest

from memplex.models import BackgroundTask, TaskInfo, TaskStatus
from memplex.worker import (
    BackgroundWorker,
    TaskStore,
    TaskStoreIntegrityError,
    WorkerQueueFull,
)


def _info(tid="t1", status=TaskStatus.PENDING, payload=None):
    """Build a TaskInfo with the correct field name (task_id, not id)."""
    return TaskInfo(
        task_id=tid,
        task_type=BackgroundTask.BUILD_INDEX,
        status=status,
        created_at=datetime.now(UTC),
        payload=payload or {},
    )


def _concurrent_worker_admission_probe(
    path,
    operation,
    task_id,
    start_barrier,
    counted_barrier,
    results,
):
    """Synchronize the old split count/save path to make its race deterministic."""
    worker = BackgroundWorker(storage_path=Path(path), queue_capacity=1)
    real_count = worker._task_store.count_by_status

    def _synchronized_count(*statuses):
        count = real_count(*statuses)
        counted_barrier.wait(timeout=5.0)
        return count

    worker._task_store.count_by_status = _synchronized_count
    start_barrier.wait(timeout=5.0)
    try:
        if operation == "submit":
            worker.submit(BackgroundTask.BUILD_INDEX, {"task_id": task_id})
            results.put("accepted")
        else:
            results.put("accepted" if worker.replay_failed(task_id) else "ineligible")
    except WorkerQueueFull:
        results.put("full")


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


def test_task_store_two_instances_merge_under_file_lock(tmp_path):
    path = tmp_path / "tasks.json"
    first = TaskStore(path)
    second = TaskStore(path)

    first.save(_info(tid="from-first"))
    second.save(_info(tid="from-second"))

    reopened = TaskStore(path)
    assert {item.task_id for item in reopened.list_by_status(TaskStatus.PENDING)} == {
        "from-first",
        "from-second",
    }


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
    from memplex.storage.lite.store import LiteMemoryStore

    store = LiteMemoryStore(path=tmp_path / "memory.json")
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json", store=store)
    worker.start()
    worker.submit(BackgroundTask.BUILD_INDEX, {"func_id": "f1"})
    assert worker._running is True
    worker.stop(timeout=5.0)


def test_worker_queue_capacity_rejects_without_partial_task(tmp_path):
    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json",
        queue_capacity=1,
    )
    first = worker.submit(BackgroundTask.BUILD_INDEX, {"n": 1})

    with pytest.raises(WorkerQueueFull, match="worker_queue_full"):
        worker.submit(BackgroundTask.BUILD_INDEX, {"n": 2})

    assert worker.get_status(first) is TaskStatus.PENDING
    assert len(worker._task_store.list_by_status(TaskStatus.PENDING)) == 1


@pytest.mark.parametrize("operation", ("submit", "replay"))
def test_worker_capacity_admission_is_atomic_across_processes(tmp_path, operation):
    """Removing the store-level capacity reservation must let both processes win."""
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "tasks.json"
    if operation == "replay":
        TaskStore(path).save(_info(tid="dead-a", status=TaskStatus.FAILED))
        TaskStore(path).save(_info(tid="dead-b", status=TaskStatus.FAILED))
        task_ids = ("dead-a", "dead-b")
    else:
        task_ids = ("submit-a", "submit-b")
    start_barrier = ctx.Barrier(2)
    counted_barrier = ctx.Barrier(2)
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_concurrent_worker_admission_probe,
            args=(
                str(path),
                operation,
                task_id,
                start_barrier,
                counted_barrier,
                results,
            ),
        )
        for task_id in task_ids
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10.0)
        assert process.exitcode == 0

    outcomes = sorted(results.get(timeout=1.0) for _ in processes)
    assert outcomes == ["accepted", "full"]
    assert TaskStore(path).count_by_status(TaskStatus.PENDING, TaskStatus.RUNNING) == 1


def test_worker_queue_publication_failure_keeps_durable_admission(
    tmp_path, monkeypatch
):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    worker._dispatch = lambda *_: {"status": "completed"}
    put_nowait = worker._queue.put_nowait

    def _full(_task_id):
        raise Full

    monkeypatch.setattr(worker._queue, "put_nowait", _full)
    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {"n": 1})

    assert worker.get_status(task_id) is TaskStatus.PENDING
    assert worker.persisted_pending_count() == 1
    assert worker.queue_depth == 0
    monkeypatch.setattr(worker._queue, "put_nowait", put_nowait)
    assert worker.run_once() is True
    assert worker.get_status(task_id) is TaskStatus.COMPLETED


def test_worker_replay_queue_hint_failure_keeps_durable_replay(tmp_path, monkeypatch):
    path = tmp_path / "tasks.json"
    TaskStore(path).save(_info(tid="dead-hint", status=TaskStatus.FAILED))
    worker = BackgroundWorker(storage_path=path)
    worker._dispatch = lambda *_: {"status": "completed"}
    put_nowait = worker._queue.put_nowait

    def _full(_task_id):
        raise Full

    monkeypatch.setattr(worker._queue, "put_nowait", _full)
    assert worker.replay_failed("dead-hint") is True
    assert worker.get_status("dead-hint") is TaskStatus.PENDING
    monkeypatch.setattr(worker._queue, "put_nowait", put_nowait)
    assert worker.run_once() is True
    assert worker.get_status("dead-hint") is TaskStatus.COMPLETED


def test_worker_retry_survives_restart_without_timer(tmp_path):
    path = tmp_path / "tasks.json"
    now = datetime(2026, 8, 11, tzinfo=UTC)
    first = BackgroundWorker(storage_path=path, clock=lambda: now)
    task_id = first.submit(BackgroundTask.BUILD_INDEX, {})
    first._dispatch = lambda *_: (_ for _ in ()).throw(RuntimeError("offline"))

    assert first.run_once() is True
    retry = first._task_store.get(task_id)
    assert retry is not None
    assert retry.status is TaskStatus.PENDING
    assert retry.next_attempt_at == now + timedelta(seconds=2)

    second = BackgroundWorker(
        storage_path=path,
        clock=lambda: now + timedelta(seconds=2),
    )
    second._dispatch = lambda *_: {"status": "completed"}
    assert second.run_once() is True
    assert second.get_status(task_id) is TaskStatus.COMPLETED


def test_worker_persists_completion_before_callback(tmp_path):
    path = tmp_path / "tasks.json"
    worker = BackgroundWorker(storage_path=path)
    observed: list[TaskStatus] = []
    task_id = ""

    def _callback(_result):
        info = TaskStore(path).get(task_id)
        assert info is not None
        observed.append(info.status)

    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {}, callback=_callback)
    worker._dispatch = lambda *_: {"status": "completed"}

    assert worker.run_once() is True
    assert observed == [TaskStatus.COMPLETED]


def test_worker_does_not_publish_callback_when_completion_commit_fails(
    tmp_path, monkeypatch
):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    callbacks: list[object] = []
    worker.submit(
        BackgroundTask.BUILD_INDEX,
        {},
        callback=lambda result: callbacks.append(result),
    )
    worker._dispatch = lambda *_: {"status": "completed"}
    def _fail_completed(task_id, lease_id, result, *, now=None):
        raise TaskStoreIntegrityError("injected completion commit failure")

    monkeypatch.setattr(worker._task_store, "complete", _fail_completed)
    with pytest.raises(TaskStoreIntegrityError, match="completion commit failure"):
        worker.run_once()

    assert callbacks == []


def test_worker_recovers_legacy_running_task_without_lease(tmp_path):
    path = tmp_path / "tasks.json"
    TaskStore(path).save(_info(tid="legacy-running", status=TaskStatus.RUNNING))
    worker = BackgroundWorker(storage_path=path)
    worker._dispatch = lambda *_: {"status": "completed"}

    assert worker.run_once() is True
    assert worker.get_status("legacy-running") is TaskStatus.COMPLETED


def test_worker_stop_returns_machine_readable_pending_state(tmp_path):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    worker.submit(BackgroundTask.BUILD_INDEX, {})

    result = worker.stop(timeout=0.01)

    assert result.drained is True
    assert result.pending == 1
    assert result.leased == 0
    assert result.dead_letters == 0
    assert result.deadline_exceeded is False


def test_worker_loop_refuses_new_claim_after_stop_gate_closes(tmp_path):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {})
    worker._running = False
    worker._dispatch = lambda *_: pytest.fail("stopped worker claimed new work")

    assert worker._run_once(require_running=True) is False
    assert worker.get_status(task_id) is TaskStatus.PENDING


def test_worker_stop_deadline_leaves_active_lease_recoverable(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json",
        lease_seconds=1,
    )

    def _blocked(*_args):
        entered.set()
        release.wait(timeout=2.0)
        return {"status": "completed"}

    worker._dispatch = _blocked
    worker.start()
    worker.submit(BackgroundTask.BUILD_INDEX, {})
    assert entered.wait(timeout=1.0)

    first = worker.stop(timeout=0.01)

    assert first.drained is False
    assert first.leased == 1
    assert first.deadline_exceeded is True
    release.set()
    assert worker._worker_thread is not None
    worker._worker_thread.join(timeout=2.0)
    second = worker.stop(timeout=0.1)
    assert second.drained is True
    assert second.leased == 0
    assert second.completed == 1


def test_worker_dead_letter_replay_preserves_task_identity(tmp_path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    failed = _info(tid="dead-replay", status=TaskStatus.FAILED)
    failed.error = "task_failed"
    failed.last_error_code = "task_failed"
    store.save(failed)
    worker = BackgroundWorker(storage_path=path)
    worker._dispatch = lambda *_: {"status": "completed"}

    assert worker.replay_failed("dead-replay") is True
    assert worker.run_once() is True
    assert worker.get_status("dead-replay") is TaskStatus.COMPLETED
    assert worker.replay_failed("dead-replay") is False


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


# ── Persistence robustness (Wave 1 fixes) ────────────────────────────


def test_task_store_save_is_atomic_and_survives_serialisation_failure(tmp_path):
    """A payload that json cannot serialise must NOT truncate tasks.json."""
    import json as _json

    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(_info(tid="good", status=TaskStatus.PENDING, payload={"func_id": "f1"}))
    good_bytes = path.read_bytes()
    assert good_bytes  # pre-condition: file written

    # sets are not JSON serialisable -> commit fails before touching disk and
    # the caller receives a fixed integrity error.
    with pytest.raises(TaskStoreIntegrityError, match="not serializable"):
        store.save(_info(tid="bad", status=TaskStatus.PENDING, payload={"oops": {1, 2, 3}}))

    assert path.read_bytes() == good_bytes
    on_disk = _json.loads(path.read_text(encoding="utf-8"))
    assert set(on_disk["tasks"].keys()) == {"good"}


@pytest.mark.parametrize(
    "mutate",
    (
        lambda info: setattr(info, "retry_count", True),
        lambda info: setattr(info, "payload", {1: "silently-stringified"}),
        lambda info: setattr(info, "result", float("nan")),
    ),
)
def test_task_store_rejects_weak_or_noncanonical_state_before_commit(
    tmp_path, mutate
):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(_info(tid="good"))
    old_bytes = path.read_bytes()
    bad = _info(tid="bad")
    mutate(bad)

    with pytest.raises(TaskStoreIntegrityError):
        store.save(bad)

    assert path.read_bytes() == old_bytes
    assert store.get("bad") is None


def test_task_store_serialises_dataclass_results(tmp_path):
    """dataclass results (e.g. CompactionResult from COMPACTION) round-trip."""
    import json as _json

    from memplex.models import CompactionResult, CompactionStageResult

    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    info = _info(tid="c1", status=TaskStatus.COMPLETED)
    info.result = CompactionResult(
        total_processed=3,
        total_removed=1,
        total_merged=1,
        duration_ms=12,
        stages=[
            CompactionStageResult(stage="dedup", processed=3, removed=1, merged=1, duration_ms=5)
        ],
    )
    store.save(info)

    on_disk = _json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["tasks"]["c1"]["result"]["total_removed"] == 1
    assert on_disk["tasks"]["c1"]["result"]["stages"][0]["stage"] == "dedup"


def test_task_store_load_rejects_corrupt_file_without_mutation(tmp_path):
    """A corrupt tasks.json fails closed and remains byte-for-byte intact."""
    path = tmp_path / "tasks.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(TaskStoreIntegrityError, match="task store is invalid"):
        TaskStore(path)

    assert path.read_text(encoding="utf-8") == "{not valid json"
    assert list(tmp_path.glob("tasks.json.corrupt-*")) == []


def test_task_store_pre_replace_fsync_failure_preserves_old_state_and_recovers(
    tmp_path, monkeypatch
):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(_info(tid="old"))
    old_bytes = path.read_bytes()
    real_fsync = os.fsync
    fail_once = True

    def _fail_first_fsync(fd):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("injected pre-replace fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fail_first_fsync)
    with pytest.raises(TaskStoreIntegrityError, match="commit failed"):
        store.save(_info(tid="new"))

    assert path.read_bytes() == old_bytes
    assert store.get("old") is not None
    store.save(_info(tid="after-recovery"))
    assert TaskStore(path).get("after-recovery") is not None


def test_task_store_temp_creation_failure_is_fixed_and_non_mutating(
    tmp_path, monkeypatch
):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(_info(tid="old"))
    old_bytes = path.read_bytes()

    def _fail_tempfile(*_args, **_kwargs):
        raise OSError("injected temp creation failure")

    monkeypatch.setattr("memplex.worker.tempfile.mkstemp", _fail_tempfile)
    with pytest.raises(TaskStoreIntegrityError, match="commit failed"):
        store.save(_info(tid="new"))

    assert path.read_bytes() == old_bytes
    assert store.get("old") is not None
    assert store.get("new") is None


def test_task_store_post_replace_directory_fsync_failure_poisons_until_reopen(
    tmp_path, monkeypatch
):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(_info(tid="old"))
    real_fsync = os.fsync
    fsync_calls = 0

    def _fail_directory_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fail_directory_fsync)
    with pytest.raises(TaskStoreIntegrityError, match="commit failed"):
        store.save(_info(tid="new"))

    with pytest.raises(TaskStoreIntegrityError, match="requires reopen"):
        store.get("old")
    reopened = TaskStore(path)
    assert reopened.get("old") is not None
    assert reopened.get("new") is not None


# ── cancel() honoured by the execution loop ──────────────────────────


def test_cancelled_task_is_skipped_by_execute(tmp_path):
    """_execute_task must not run a task whose status is CANCELLED."""
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {"func_id": "f1"})
    assert worker.cancel(task_id) is True

    worker._execute_task({"id": task_id, "task": BackgroundTask.BUILD_INDEX, "payload": {}})

    assert worker.get_status(task_id) == TaskStatus.CANCELLED


def test_cancel_pending_returns_true_and_running_returns_false(tmp_path):
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json")
    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {})
    assert worker.cancel(task_id) is True
    # Already cancelled -> no longer cancellable
    assert worker.cancel(task_id) is False
    assert worker.cancel("missing") is False


# ── Task handlers (Wave 1: stubs replaced) ───────────────────────────


def test_handle_build_index_rebuilds_fts_sidecar(tmp_path):
    """BUILD_INDEX drives a real FTS sidecar rebuild on the lite store."""
    from memplex.models import Function, SourceDocument
    from memplex.storage.lite.store import LiteMemoryStore

    store = LiteMemoryStore(path=tmp_path / "memory.json")
    store.add(
        Function(id="f1", name="deploy app", name_normalized="deploy app"),
        SourceDocument(type="text", content="deploy"),
    )

    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json", store=store)
    result = worker._handle_build_index({})
    assert result["status"] == "completed"
    assert result["indexed"] == 1
    # The sidecar now serves real hits.
    hits = store.fts_search("deploy", top_k=5)
    assert any(h.func_id == "f1" for h in hits)


def test_handle_build_index_uses_public_rebuild(tmp_path):
    """BUILD_INDEX delegates to the sidecar's public rebuild() method
    instead of poking private signature-cache members."""

    class _StubFTS:
        def __init__(self):
            self.calls = 0

        def rebuild(self):
            self.calls += 1

    class _StubStore:
        def __init__(self):
            self._fts_index = _StubFTS()
            self._functions = {"f1": object()}

    store = _StubStore()
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json", store=store)
    result = worker._handle_build_index({})
    assert result["status"] == "completed"
    assert result["indexed"] == 1
    assert store._fts_index.calls == 1


def test_fts_rebuild_recovers_deleted_sidecar(tmp_path):
    """rebuild() re-indexes every function even when the signature cache
    looks current (e.g. the sidecar DB file was deleted externally)."""
    from memplex.models import Function, SourceDocument
    from memplex.storage.lite.store import LiteMemoryStore

    store = LiteMemoryStore(path=tmp_path / "memory.json")
    store.add(
        Function(id="f1", name="rebuild canary", name_normalized="rebuild canary"),
        SourceDocument(type="text", content="rebuild"),
    )
    assert any(h.func_id == "f1" for h in store.fts_search("rebuild", top_k=5))

    # Simulate external deletion of the sidecar while the cache says "current".
    sidecar = (tmp_path / "memory.json").with_name("memory.json.fts5.db")
    assert sidecar.exists()
    sidecar.unlink()

    store.rebuild_search_index()
    hits = store.fts_search("rebuild", top_k=5)
    assert any(h.func_id == "f1" for h in hits)


def test_handle_build_index_skips_store_without_sidecar(tmp_path):
    class _NoFTSStore:
        pass

    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json", store=_NoFTSStore())
    result = worker._handle_build_index({})
    assert result["status"] == "skipped"


def test_handle_extract_runs_core_engine(tmp_path):
    """EXTRACT_DOCUMENT goes through CoreEngine and reports real counts."""
    worker = BackgroundWorker(storage_path=tmp_path / "tasks.json", engine=None)
    result = worker._handle_extract({"type": "text", "content": "触发: 保存. 动作: 写入磁盘."})
    assert result["status"] == "completed"
    assert isinstance(result["extracted"], int)
    assert result["extracted"] >= 0


def test_handle_refresh_vector_refreshes_single_function(tmp_path):
    """REFRESH_VECTOR delegates to EmbeddingService.refresh for a func_id."""
    from memplex.models import Function, SourceDocument
    from memplex.retrieval.embedding import EmbeddingService
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.storage.vector import InMemoryVectorStore

    store = LiteMemoryStore(path=tmp_path / "memory.json")
    store.add(
        Function(id="f1", name="deploy app", name_normalized="deploy app"),
        SourceDocument(type="text", content="deploy"),
    )
    vector_store = InMemoryVectorStore()
    service = EmbeddingService(model="tfidf", storage=store, vector_store=vector_store)

    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json", store=store, embedding_service=service
    )
    result = worker._handle_refresh_vector({"func_id": "f1"})
    assert result == {"status": "completed", "refreshed": 1}
    assert "f1" in vector_store._stored_vectors


def test_handle_refresh_vector_refresh_all(tmp_path):
    from memplex.models import Function, SourceDocument
    from memplex.retrieval.embedding import EmbeddingService
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.storage.vector import InMemoryVectorStore

    store = LiteMemoryStore(path=tmp_path / "memory.json")
    for fid in ("f1", "f2"):
        store.add(
            Function(id=fid, name=f"func {fid}", name_normalized=f"func {fid}"),
            SourceDocument(type="text", content=fid),
        )
    service = EmbeddingService(
        model="tfidf", storage=store, vector_store=InMemoryVectorStore()
    )
    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json", store=store, embedding_service=service
    )
    result = worker._handle_refresh_vector({})
    assert result == {"status": "completed", "refreshed": 2}


# ── COMPILE_WIKI handler (Wave 2a: real WikiCompiler wiring) ──────────


def _wiki_config(wiki_dir, enabled=True):
    from memplex.config import MemplexConfig

    cfg = MemplexConfig()
    cfg.wiki.dir = str(wiki_dir)
    cfg.wiki.enabled = enabled
    return cfg


def _store_with_function(tmp_path, fid="func_1"):
    from memplex.models import Function, SourceDocument
    from memplex.storage.lite.store import LiteMemoryStore

    store = LiteMemoryStore(path=tmp_path / "memory.json")
    store.add(
        Function(id=fid, name="deploy app", name_normalized="deploy app", domain="ops"),
        SourceDocument(type="text", content="deploy"),
    )
    return store


def test_handle_compile_wiki_writes_real_pages(tmp_path):
    """COMPILE_WIKI compiles the store and writes pages to the configured
    wiki directory (config.wiki.dir)."""
    wiki_dir = tmp_path / "wiki"
    store = _store_with_function(tmp_path)
    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json",
        store=store,
        config=_wiki_config(wiki_dir),
    )

    result = worker._handle_compile_wiki({})

    assert result["status"] == "completed"
    assert result["pages"] >= 3  # entity + domain aggregate + index
    assert result["wiki_dir"] == str(wiki_dir)
    # Index page and the entity page really exist on disk.
    assert (wiki_dir / "index.md").exists()
    entity_pages = list((wiki_dir / "entities").glob("*.md"))
    assert (wiki_dir / "entities" / "func_1.md") in entity_pages
    assert (wiki_dir / "entities" / "domain_ops.md") in entity_pages
    assert "deploy app" in (wiki_dir / "entities" / "func_1.md").read_text(encoding="utf-8")


def test_handle_compile_wiki_skips_when_wiki_disabled(tmp_path):
    """wiki.enabled=false -> graceful skip, nothing written."""
    wiki_dir = tmp_path / "wiki"
    store = _store_with_function(tmp_path)
    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json",
        store=store,
        config=_wiki_config(wiki_dir, enabled=False),
    )

    result = worker._handle_compile_wiki({})

    assert result == {"status": "skipped", "reason": "wiki_disabled", "pages": 0}
    assert not wiki_dir.exists()


def test_handle_compile_wiki_payload_wiki_dir_overrides_config(tmp_path):
    store = _store_with_function(tmp_path)
    override_dir = tmp_path / "wiki_override"
    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json",
        store=store,
        config=_wiki_config(tmp_path / "wiki_configured"),
    )

    result = worker._handle_compile_wiki({"wiki_dir": str(override_dir)})

    assert result["status"] == "completed"
    assert (override_dir / "index.md").exists()
    assert not (tmp_path / "wiki_configured").exists()


def test_handle_compile_wiki_via_submit_roundtrip(tmp_path):
    """End-to-end through the worker queue: task completes and pages land
    in the configured wiki directory."""
    import time

    wiki_dir = tmp_path / "wiki"
    store = _store_with_function(tmp_path)
    worker = BackgroundWorker(
        storage_path=tmp_path / "tasks.json",
        store=store,
        config=_wiki_config(wiki_dir),
    )
    worker.start()
    try:
        task_id = worker.submit(BackgroundTask.COMPILE_WIKI, {})
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if worker.get_status(task_id) in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)
        assert worker.get_status(task_id) == TaskStatus.COMPLETED
        assert (wiki_dir / "index.md").exists()
    finally:
        worker.stop(timeout=5.0)
