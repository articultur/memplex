"""Sleep-time compute (Letta-style idle-time maintenance and precompute).

A daemon that waits for sustained idle (background worker queue empty for
``idle_grace_seconds``) and then, once per ``interval_seconds``:

1. Runs the :meth:`~memplex.service.MemplexService.improve` maintenance
   pass (fact dedupe / expiry / index rebuild).
2. **Precomputes inferences** for the hottest memories: for the top-K
   functions by ``access_count``, resolves their graph neighbourhood and
   pins compact association summaries into the working-memory tier as
   ``[SLEEP-TIME]`` entries — so the next recall surfaces rehearsed
   associations without touching the retrieval pipeline at query time.

Off by default (``sleep_time.enabled``); ``run_once()`` is synchronous and
side-effect-reported for testability. The daemon never raises into the
host: every phase is fail-soft with a debug trace.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SLEEP_PREFIX = "[SLEEP-TIME]"


class SleepTimeAgent:
    """Idle-triggered maintenance + inference precompute daemon."""

    def __init__(
        self,
        service: Any,
        *,
        interval_seconds: float = 3600.0,
        idle_grace_seconds: float = 300.0,
        precompute_top_k: int = 20,
    ) -> None:
        self._service = service
        self._interval = max(1.0, float(interval_seconds))
        self._idle_grace = max(0.0, float(idle_grace_seconds))
        self._top_k = max(1, int(precompute_top_k))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_report: Dict[str, Any] = {}

    # ── Public lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="memplex-sleep-time", daemon=True
        )
        self._thread.start()
        logger.info("SleepTimeAgent started (interval=%ss)", self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    # ── Idle detection ───────────────────────────────────────────────

    def _idle_since(self) -> float:
        """Monotonic timestamp since which the worker queue has been empty."""
        try:
            if self._service._worker.queue_depth > 0:
                return -1.0
        except Exception:
            return -1.0
        return time.monotonic()  # caller pairs this with a grace window

    def _run_loop(self) -> None:
        idle_mark: Optional[float] = None
        while not self._stop_event.wait(1.0):
            try:
                if self._service._worker.queue_depth > 0:
                    idle_mark = None
                    continue
                now = time.monotonic()
                if idle_mark is None:
                    idle_mark = now
                if now - idle_mark < self._idle_grace:
                    continue
                # Sustained idle reached: run one pass, then back off.
                self.run_once()
                idle_mark = None
                self._stop_event.wait(self._interval)
            except Exception as exc:
                logger.debug("sleep-time pass failed (will retry): %s", exc)
                idle_mark = None
                self._stop_event.wait(self._interval)

    # ── The pass itself (synchronous, testable) ──────────────────────

    def run_once(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"improved": {}, "pinned_inferences": 0}
        try:
            report["improved"] = self._service.improve()
        except Exception as exc:
            logger.debug("sleep-time improve failed: %s", exc)
        try:
            report["pinned_inferences"] = self._precompute_inferences()
        except Exception as exc:
            logger.debug("sleep-time inference precompute failed: %s", exc)
        self.last_report = report
        return report

    def _precompute_inferences(self) -> int:
        working_memory = getattr(self._service, "_working_memory", None)
        if working_memory is None:
            return 0
        # V3 fix: scan through the authorization gate's store facade (the
        # local-development principal), never the raw base store —
        # production nodes outside the local tenant are filtered before
        # their names can reach the hot-context tier.
        from memplex.auth import local_development_context

        context = local_development_context()
        store = self._service._store_for(context)
        try:
            functions: List = list(store.list_functions(limit=100000))
        except Exception as exc:
            logger.debug("sleep-time list_functions failed: %s", exc)
            return 0
        functions.sort(
            key=lambda f: getattr(f, "access_count", 0) or 0, reverse=True
        )
        hot = [f for f in functions if (getattr(f, "access_count", 0) or 0) > 0][
            : self._top_k
        ]
        pinned = 0
        for func in hot:
            neighbours = self._neighbour_names(store, func.id)
            if not neighbours:
                continue
            summary = (
                f"{_SLEEP_PREFIX} '{func.name}' is graph-adjacent to: "
                + ", ".join(neighbours[:5])
            )
            working_memory.add(
                f"sleep:{func.id}",
                summary,
                category="note",
                pinned=False,
                # Scope to the local-development workspace (V3 fix):
                # sleep-time inferences stay inside their workspace's
                # hot-context tier and never leak cross-workspace.
                scope="tenant:local",
            )
            pinned += 1
        return pinned

    @staticmethod
    def _neighbour_names(store: Any, func_id: str) -> List[str]:
        """One-hop graph neighbours of *func_id* via the store's graph API.

        The store contract is ``get_graph(func_ids: Optional[List[str]])``
        returning the induced sub-graph (requested nodes + connecting
        edges); neighbours come from the edge endpoints.
        """
        try:
            data = store.get_graph([func_id])
        except Exception:
            return []
        names: List[str] = []
        for edge in getattr(data, "edges", None) or []:
            source = getattr(edge, "source", None)
            target = getattr(edge, "target", None)
            other = target if source == func_id else source if target == func_id else None
            if other is None:
                continue
            neighbour = store.get(other) if hasattr(store, "get") else None
            name = getattr(neighbour, "name", None) or other
            names.append(str(name))
        return names[:8]
