"""Single-writer queue for the lite store (ADR-012 Phase B B4).

All store mutations execute on one dedicated writer thread; embedding,
extraction, and graph computation stay on the caller thread and only the
final row mutation enters the queue. This constructively ends the
worker-vs-main-thread durability deadlock class (v13.1: two threads
entering the same write path from different lock orders): with a single
writer there is no interleaving to order incorrectly.

Reads never enter the queue (WAL readers do not block the writer).
Reentrant submissions from the writer thread itself run inline, so a
mutation that triggers another mutation cannot deadlock the queue.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def single_writer_enabled() -> bool:
    """Kill-switch: MEMPLEX_LITE_SINGLE_WRITER=0 restores direct mutation."""
    return os.environ.get("MEMPLEX_LITE_SINGLE_WRITER", "1") not in {"0", "false", "False"}


class _Job:
    __slots__ = ("done", "error", "fn", "result")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class SingleWriterQueue:
    """One dedicated mutation thread; submit() blocks for the result."""

    def __init__(self) -> None:
        self._queue: queue.Queue[_Job] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._writer_ident: int | None = None
        self._closed = False

    def _ensure_thread(self) -> None:
        with self._start_lock:
            if self._closed:
                raise RuntimeError("single-writer queue is closed")
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="lite-single-writer", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        self._writer_ident = threading.get_ident()
        while True:
            job = self._queue.get()
            if job is None:  # shutdown sentinel
                self._queue.task_done()
                return
            try:
                job.result = job.fn()
            except BaseException as exc:  # noqa: BLE001 - propagated to submitter
                job.error = exc
            finally:
                job.done.set()
                self._queue.task_done()

    def submit(self, fn: Callable[[], Any]) -> Any:
        """Run *fn* on the writer thread and return its result.

        Raises whatever *fn* raised, on the calling thread. Submissions
        from the writer thread itself execute inline (reentrancy).
        """
        if self._writer_ident is not None and threading.get_ident() == self._writer_ident:
            return fn()
        self._ensure_thread()
        job = _Job(fn)
        self._queue.put(job)
        job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result

    def drain(self, timeout: float | None = None) -> None:
        """Wait for queued jobs to finish; the queue stays usable."""
        self._queue.join()

    def close(self, drain: bool = True) -> None:
        """Stop the writer; queued jobs finish first when *drain*.

        Use only when the owning store will never mutate again (process
        teardown). Service stop() uses drain() instead so the CLI's
        service-reuse pattern keeps working.
        """
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if drain:
            self._queue.join()
        self._queue.put(None)
        if thread is not None:
            thread.join(timeout=30)

    @property
    def depth(self) -> int:
        return self._queue.qsize()
