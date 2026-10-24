"""Single-writer queue reentrancy guard (deadlock trap closure).

A thread already inside a _with_writer_lock locked call on the store must
NOT have further wrapped methods queued to the single-writer thread: the
queued job re-acquires durability.writer_lock on the WRITER thread and
blocks against the caller's hold (RLock is not cross-thread reentrant) -
a deterministic deadlock. The guard runs such nested calls inline.

The trap proof: hold writer_lock on this thread (as the seven manual
lock sites do), then call a wrapped method from a helper thread... no -
the real trap is calling a WRAPPED method while THIS thread holds the
lock directly. Demonstrated with the writer_lock context manager plus a
wrapped call that must complete instead of hanging.
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.models import Function, SourceDocument, SourceType
from memplex.storage.lite.store import LiteMemoryStore


def _store(tmp_path) -> LiteMemoryStore:
    return LiteMemoryStore(tmp_path / "memory.json")


def test_wrapped_call_inside_manual_lock_hold_completes(tmp_path):
    """The deterministic trap: holding writer_lock on this thread while
    calling a wrapped method must run inline (guard) rather than queue a
    job that can never acquire the lock. Without the guard this hangs
    forever; the bounded join makes a regression a failure, not a hang
    that trips the suite timeout."""
    store = _store(tmp_path)
    store.add(
        Function(id="f1", name="probe", name_normalized="probe"),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    result: list[str] = []

    def nested_call():
        # Wrapped (@_with_writer_lock) method invoked while THIS thread
        # holds the durability lock directly - the seven manual lock
        # sites' shape.
        with store._durability.writer_lock():
            store._writer_call_depth.depth = 1  # simulate locked-call context
            try:
                store.delete_paragraph("nonexistent")
                result.append("returned")
            finally:
                store._writer_call_depth.depth = 0

    t = threading.Thread(target=nested_call)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), (
        "wrapped call under a manual lock hold hung - reentrancy guard "
        "regressed (this is the deterministic deadlock shape)"
    )
    assert result == ["returned"]


def test_guard_depth_is_thread_local(tmp_path):
    """Depth tracking on one thread must not make unrelated threads skip
    the queue (the queue's serialization is the B4 contract)."""
    store = _store(tmp_path)
    store.add(
        Function(id="f2", name="probe2", name_normalized="probe2"),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    # Set depth on the main thread; a helper thread (depth 0) must still
    # go through the queue path and complete.
    store._writer_call_depth.depth = 3
    try:
        done = threading.Event()

        def other_thread_call():
            store.delete_paragraph("nonexistent")
            done.set()

        t = threading.Thread(target=other_thread_call)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "unrelated-thread wrapped call hung"
        assert done.is_set()
    finally:
        store._writer_call_depth.depth = 0
