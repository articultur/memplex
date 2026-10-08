"""ADR-012 Phase B B4: single-writer queue contract tests.

Covers: submission serialization (one mutation at a time), reentrant
inline execution, exception propagation to the caller, close draining,
and the store-level integration - concurrent writer threads plus a
querying reader thread running without deadlock while mutations land
durably.
"""

import os
import threading
import time

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from pathlib import Path

import pytest

from memplex.storage.lite.single_writer import (
    SingleWriterQueue,
    single_writer_enabled,
)


def test_submissions_serialize():
    q = SingleWriterQueue()
    try:
        active = 0
        peak = 0
        lock = threading.Lock()

        def job(i):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return i * 2

        results = [q.submit(lambda i=i: job(i)) for i in range(8)]
        assert results == [i * 2 for i in range(8)]
        assert peak == 1, "mutations must never overlap"
    finally:
        q.close()


def test_reentrancy_runs_inline():
    q = SingleWriterQueue()
    try:
        def outer():
            # From inside the writer thread, submit must run inline
            # (same thread ident) or this would deadlock.
            return q.submit(lambda: "inner")

        assert q.submit(outer) == "inner"
    finally:
        q.close()


def test_exception_propagates_to_caller():
    q = SingleWriterQueue()
    try:
        with pytest.raises(ValueError, match="boom"):
            q.submit(lambda: (_ for _ in ()).throw(ValueError("boom")))
        # Queue survives the failure and serves the next job.
        assert q.submit(lambda: "ok") == "ok"
    finally:
        q.close()


def test_close_drains_pending_jobs():
    q = SingleWriterQueue()
    executed = []

    def slow(i):
        time.sleep(0.02)
        executed.append(i)

    for i in range(5):
        # enqueue without waiting: run the puts from a helper thread
        pass
    # submit one directly first to start the writer
    q.submit(lambda: executed.append(-1))
    th = threading.Thread(
        target=lambda: [q.submit(lambda i=i: slow(i)) for i in range(5)]
    )
    th.start()
    th.join()
    q.close(drain=True)
    assert sorted(executed) == [-1, 0, 1, 2, 3, 4]


def test_flag_defaults_on():
    assert single_writer_enabled() is True


def test_store_mutations_through_queue_with_concurrent_reader(tmp_path):
    """Store-level: writer threads mutate through the single writer while
    a reader queries; no deadlock, all writes durable at reopen."""
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()

    stop = threading.Event()
    errors: list[BaseException] = []

    def writer_thread(n: int) -> None:
        try:
            for i in range(15):
                svc.write_text(
                    f"Writer {n} record {i}: the ledger notes entry {n}-{i}.",
                    source_type="text",
                )
        except BaseException as exc:  # noqa: BLE001 - collected by assertion
            errors.append(exc)

    def reader_thread() -> None:
        try:
            while not stop.is_set():
                svc.query("ledger notes entry", top_k=5, orchestrated=False, explain=False)
                time.sleep(0.01)
        except BaseException as exc:  # noqa: BLE001 - collected by assertion
            errors.append(exc)

    readers = [threading.Thread(target=reader_thread) for _ in range(2)]
    writers = [threading.Thread(target=writer_thread, args=(n,)) for n in range(3)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(timeout=120)
        assert not t.is_alive(), "writer thread deadlocked"
    stop.set()
    for t in readers:
        t.join(timeout=30)
        assert not t.is_alive(), "reader thread deadlocked"
    assert not errors, f"concurrent errors: {errors}"
    svc.stop()

    # Everything written must be durable.
    svc = MemplexService(config=config)
    svc.start()
    try:
        paragraphs = svc.store._paragraphs
        assert any("Writer 2 record 14" in r["raw_text"] for r in paragraphs.values())
    finally:
        svc.stop()


@pytest.mark.parametrize("queue_flag", ["1", "0"], ids=["queue-on", "queue-off"])
def test_fact_write_uses_writer_boundary_and_is_durable_on_return(tmp_path, monkeypatch, queue_flag):
    """Removing add_fact's decorator loses its execution and lock boundary."""
    from memplex.models import Fact
    from memplex.storage.lite.store import LiteMemoryStore

    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", queue_flag)
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", "")
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    reloaded = None
    calls = []
    original_reload = store._reload_for_mutation

    def observe_reload(*, force=False):
        calls.append((threading.get_ident(), getattr(store._durability._local, "depth", 0)))
        original_reload(force=force)

    monkeypatch.setattr(store, "_reload_for_mutation", observe_reload)
    caller_ident = threading.get_ident()
    try:
        store.add_fact(Fact(id="durable-fact", subject="alice", predicate="likes", object_="tea"))
        assert len(calls) == 1
        execution_ident, lock_depth = calls[0]
        assert lock_depth > 0, "Fact reload ran outside the writer lock"
        if queue_flag == "1":
            writer = store._durability._single_writer
            assert execution_ident == writer._writer_ident
            assert execution_ident != caller_ident
        else:
            assert execution_ident == caller_ident
            assert getattr(store._durability, "_single_writer", None) is None
        reloaded = LiteMemoryStore(path)
        assert reloaded.get_fact("durable-fact").object_ == "tea"
    finally:
        for memory in (store, reloaded):
            if memory is not None:
                writer = getattr(memory._durability, "_single_writer", None)
                if writer is not None:
                    writer.close()


def test_fact_write_reenters_existing_writer_queue_under_lock(tmp_path, monkeypatch):
    """A decorated Fact write must run inline when its writer already owns the lock."""
    from memplex.models import Fact
    from memplex.storage.lite.store import LiteMemoryStore

    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", "1")
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", "")
    store = LiteMemoryStore(tmp_path / "memory.json")
    writer = SingleWriterQueue()
    monkeypatch.setattr(store._durability, "_single_writer", writer, raising=False)
    finished = threading.Event()
    errors = []

    def write_under_lock():
        with store._durability.writer_lock():
            store.add_fact(Fact(id="reentrant-fact", subject="alice", predicate="likes", object_="tea"))
            return store.get_fact("reentrant-fact").object_

    results = []

    def submit():
        try:
            results.append(writer.submit(write_under_lock))
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            errors.append(exc)
        finally:
            finished.set()

    caller = threading.Thread(target=submit, daemon=True)
    caller.start()
    try:
        assert finished.wait(5), "Fact write deadlocked while reentering the writer queue"
        caller.join(5)
        assert not caller.is_alive()
        assert not errors, f"Reentrant Fact write errors: {errors}"
        assert results == ["tea"]
    finally:
        # A failed bounded deadlock assertion must not block again in close.
        if finished.is_set():
            writer.close()
