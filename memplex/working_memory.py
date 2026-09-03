"""Working memory: TTL-bounded hot context automatically injected on recall.

Mnemosyne-style "working memory" tier. Recent writes (and explicitly pinned
entries) live in a bounded in-process store; every :meth:`recall` call
prepends the live entries as ready-to-use context so the most recent turns
are available before any retrieval path runs.

Design boundaries
-----------------
- **In-process, per-service**: working memory is a latency tier, not a
  durable store — it is not synced and never persisted. Durability remains
  the job of the ordinary write path (which still runs for every capture).
- **Bounded**: a max-entry cap plus per-entry TTL; expired entries are
  dropped lazily on access, so the tier cannot grow without bound.
- **Opt-in injection**: disabled by default (``working_memory.enabled``);
  when enabled, ``recall_context()`` returns the live entries for callers
  to prepend. The retrieval pipeline itself is untouched.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class WorkingMemoryEntry:
    """One hot-context item."""

    content: str
    category: str = "note"
    pinned: bool = False
    expires_at: float | None = None  # monotonic deadline; None = pinned-only
    created_at: float = field(default_factory=time.monotonic)


class WorkingMemory:
    """Thread-safe TTL hot-context store with a hard entry cap.

    Multi-principal safety (V3 fix): entries are keyed under an optional
    ``scope`` prefix so one principal's hot context never leaks into
    another's recall. The service pins capture-side entries under the
    capturing principal's scope and reads with the requesting context's
    scope; a missing scope (local development) shares the single default
    tier, preserving the original single-tenant behaviour.
    """

    def __init__(self, max_entries: int = 64, default_ttl_seconds: float = 900.0) -> None:
        self._max_entries = max(1, int(max_entries))
        self._default_ttl = float(default_ttl_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, WorkingMemoryEntry] = {}

    # ── Capture ─────────────────────────────────────────────────────

    @staticmethod
    def _scoped_key(key: str, scope: str | None) -> str:
        """Namespacer: ``scope:key`` (or bare key when scope is None)."""
        if not scope:
            return key
        return f"{scope}::{key}"

    def add(
        self,
        key: str,
        content: str,
        *,
        category: str = "note",
        ttl_seconds: float | None = None,
        pinned: bool = False,
        scope: str | None = None,
    ) -> None:
        """Add or refresh one entry; evicts the oldest unpinned entry at cap.

        ``scope`` partitions the tier per principal (V3 fix); entries added
        under one scope are invisible to ``recall_context`` calls with a
        different scope.
        """
        if not key or not content:
            return
        key = self._scoped_key(key, scope)
        ttl = self._default_ttl if ttl_seconds is None else float(ttl_seconds)
        entry = WorkingMemoryEntry(
            content=content,
            category=category,
            pinned=pinned,
            expires_at=None if pinned else time.monotonic() + max(0.0, ttl),
        )
        with self._lock:
            if key in self._entries:
                self._entries[key] = entry
                return
            if len(self._entries) >= self._max_entries:
                unpinned = [k for k, e in self._entries.items() if not e.pinned]
                if unpinned:
                    self._entries.pop(unpinned[0], None)  # FIFO among unpinned
            self._entries[key] = entry

    def pin(self, key: str) -> bool:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            entry.pinned = True
            entry.expires_at = None
            return True

    def unpin(self, key: str, ttl_seconds: float | None = None) -> bool:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            entry.pinned = False
            ttl = self._default_ttl if ttl_seconds is None else float(ttl_seconds)
            entry.expires_at = time.monotonic() + max(0.0, ttl)
            return True

    def remove(self, key: str) -> bool:
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ── Recall ───────────────────────────────────────────────────────

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.expires_at is not None and entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)

    def recall_context(self, limit: int = 8, scope: str | None = None) -> list[str]:
        """Live entries for *scope*, most-recent first, as context lines.

        Scope-filtered: a recall under scope A never returns entries pinned
        under scope B (V3 fix). Unscoped recall sees only unscoped entries.
        """
        prefix = f"{scope}::" if scope else ""
        with self._lock:
            self._prune_locked()
            ordered = sorted(self._entries.values(), key=lambda e: e.created_at, reverse=True)
            scoped = [
                entry
                for key, entry in self._entries.items()
                if key.startswith(prefix)
                and ("::" not in key[len(prefix):])  # no deeper nesting leak
            ] if scope else [
                entry for key, entry in self._entries.items() if "::" not in key
            ]
            scoped.sort(key=lambda e: e.created_at, reverse=True)
            return [e.content for e in scoped[: max(0, limit)]]

    def __len__(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._entries)
