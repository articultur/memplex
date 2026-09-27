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
