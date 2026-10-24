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
- **Store-parity recall ACL**: every service-captured entry carries the
  capturing principal's real tenant, subject, workspace, session, agent,
  and visibility. Recall callers must re-check entries (see
  ``adapters.agent_runtime.hot_entry_visible``): tenant equality first —
  an entry authored under one real tenant never crosses to another —
  then workspace / user visibility, with session entries bound to owner
  + session + capturing agent (mirroring the store ACL's
  origin_session/source_agent rule), and unknown visibility denied. The
  single cross-tenant exception is the service's local-development
  compatibility tier inside the mapped ``tenant:local`` scope
  (single-machine development seamlessness, V3 design). Entries without
  tenant provenance (``tenant_id is None`` — sleep-time precompute,
  direct tier users) stay scope-partitioned as before.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Hot-tier visibility vocabulary, aligned with the node model the host
# runtimes write ({user, workspace, session} — see
# adapters.agent_runtime.MEMORY_VISIBILITIES) plus explicit tenant-wide
# "tenant" for entries that deliberately address the whole scope partition.
# Unknown values are rejected fail-closed by :meth:`WorkingMemory.add`;
# the legacy "private" spelling is accepted as an alias for "user".
WORKING_MEMORY_VISIBILITIES = frozenset({"user", "workspace", "session", "tenant"})
_LEGACY_PRIVATE_ALIAS = "private"


def normalise_working_memory_visibility(visibility: str) -> str:
    """Map *visibility* onto the hot-tier vocabulary, fail-closed.

    Raises ``ValueError`` for unknown values instead of silently widening
    them (the historical behaviour coerced host "user"/"session" writes to
    tenant-wide visibility — a privilege-escalation surface).
    """

    selected = str(visibility or "").strip().lower()
    if selected == _LEGACY_PRIVATE_ALIAS:
        selected = "user"
    if selected not in WORKING_MEMORY_VISIBILITIES:
        supported = ", ".join(sorted(WORKING_MEMORY_VISIBILITIES))
        raise ValueError(
            f"Unsupported working-memory visibility {visibility!r}. Supported: {supported}"
        )
    return selected


@dataclass
class WorkingMemoryEntry:
    """One hot-context item."""

    content: str
    category: str = "note"
    pinned: bool = False
    expires_at: float | None = None  # monotonic deadline; None = pinned-only
    created_at: float = field(default_factory=time.monotonic)
    # ACL provenance (V4): the writer's visibility contract. Entries
    # inherit the visibility of their write; recall re-checks them so a
    # workspace-, user-, or session-restricted capture cannot reach a
    # broader audience through the hot tier than through the store.
    owner_subject_id: str | None = None
    # Capturing principal's REAL tenant id (before any scope mapping).
    # Recall enforces tenant equality (store-boundary parity); ``None``
    # marks entries without tenant provenance (sleep-time precompute,
    # direct tier users), which stay scope-partitioned as before.
    tenant_id: str | None = None
    workspace_id: str | None = None
    session_id: str | None = None
    # Capturing agent id (AuthorizationContext.agent_id of the write).
    # Session-visibility recall additionally requires agent equality,
    # mirroring the store ACL's origin_session/source_agent rule; entries
    # without an agent stamp are denied to any named agent.
    agent_id: str | None = None
    visibility: str = "tenant"  # user | workspace | session | tenant


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
        tenant_id: str | None = None,
        owner_subject_id: str | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        visibility: str = "tenant",
    ) -> None:
        """Add or refresh one entry; evicts the oldest unpinned entry at cap.

        ``scope`` partitions the tier per principal (V3 fix); entries added
        under one scope are invisible to ``recall_context`` calls with a
        different scope. The ACL sextuple (tenant/owner/workspace/session/
        agent/visibility) records the write's visibility contract for
        recall-time re-checking (V4): ``tenant_id`` is the capturing
        principal's REAL tenant and ``agent_id`` the capturing agent, so
        recall can enforce store-boundary parity. Unknown visibility
        values raise ``ValueError`` (fail-closed); the legacy ``"private"``
        spelling maps onto ``"user"``.
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
            tenant_id=tenant_id or None,
            owner_subject_id=owner_subject_id or None,
            workspace_id=workspace_id or None,
            session_id=session_id or None,
            agent_id=agent_id or None,
            visibility=normalise_working_memory_visibility(visibility),
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

    def remove_by_suffix(self, memory_id: str) -> int:
        """Drop every entry whose bare key (sans ``scope::`` prefix) is the
        node id or its function-mirrored ``fn:{id}`` form.

        The invalidation hook for main-store mutations (delete/update):
        capture keys are the node id (fact/preference) or ``fn:{id}``
        (function) under tenant scopes, so removing both spellings across
        every scope closes the consistency gap where a deleted memory
        kept surfacing until TTL. Returns the number removed.
        """
        removed = 0
        with self._lock:
            stale = [
                key
                for key in self._entries
                if key.endswith((memory_id, f"::{memory_id}", f"::fn:{memory_id}"))
            ]
            for key in stale:
                self._entries.pop(key, None)
                removed += 1
        return removed

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

    def recall_context(
        self,
        limit: int = 8,
        scope: str | None = None,
        acl: Callable[[WorkingMemoryEntry], bool] | None = None,
    ) -> list[str]:
        """Live entries for *scope*, most-recent first, as context lines.

        Scope-filtered: a recall under scope A never returns entries pinned
        under scope B (V3 fix). Unscoped recall sees only unscoped entries.
        ``acl`` (V4) re-checks each entry against the RECALLING principal:
        entries whose write was workspace-, user-, or session-restricted
        stay inside that boundary even though the scope (tenant) is
        broader.
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
            if acl is not None:
                scoped = [e for e in scoped if acl(e)]
            return [e.content for e in scoped[: max(0, limit)]]

    def __len__(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._entries)
