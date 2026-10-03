"""Bounded hot source references and an independent legacy text container.

Source references retain lifecycle data only. The service registers them
only after a current committed read, and context consumers must resolve the
current source rather than trusting any cached content or authorization.
Legacy add/recall_context remain usable as a local string container; both
containers share the same TTL pruning and hard capacity limit.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


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
    # workspace- or user-restricted capture cannot reach a broader
    # audience through the hot tier than through the store.
    owner_subject_id: str | None = None
    workspace_id: str | None = None
    visibility: str = "tenant"  # tenant | workspace | private


@dataclass
class WorkingMemoryReference:
    """A source ID and its hot-candidate lifecycle, never text or ACLs."""

    memory_id: str
    ttl_seconds: float
    pinned: bool
    expires_at: float | None
    insertion_sequence: int


class WorkingMemory:
    """Thread-safe TTL candidates with one hard cap across both containers.

    Reference keys are structured (storage namespace, tenant, memory ID)
    tuples, independent of the legacy string container's scope syntax.
    A pin suspends TTL expiry only; it never bypasses capacity or grants
    permission to output a reference's source.
    """

    def __init__(self, max_entries: int = 64, default_ttl_seconds: float = 900.0) -> None:
        self._max_entries = max(1, int(max_entries))
        self._default_ttl = float(default_ttl_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, WorkingMemoryEntry] = {}
        self._entry_sequences: dict[str, int] = {}
        self._references: dict[tuple[str, str, str], WorkingMemoryReference] = {}
        self._insertion_sequence = 0

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
        owner_subject_id: str | None = None,
        workspace_id: str | None = None,
        visibility: str = "tenant",
    ) -> None:
        """Add or refresh one entry; evicts the oldest unpinned entry at cap.

        ``scope`` partitions the tier per principal (V3 fix); entries added
        under one scope are invisible to ``recall_context`` calls with a
        different scope. The ACL triple (owner/workspace/visibility)
        records the write's visibility contract for recall-time
        re-checking (V4).
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
            owner_subject_id=owner_subject_id or None,
            workspace_id=workspace_id or None,
            visibility=visibility if visibility in {"tenant", "workspace", "private"} else "tenant",
        )
        with self._lock:
            self._prune_locked()
            if key not in self._entries and not self._make_room_locked():
                return
            self._entries[key] = entry
            self._entry_sequences[key] = self._next_sequence_locked()

    def _next_sequence_locked(self) -> int:
        self._insertion_sequence += 1
        return self._insertion_sequence

    def _make_room_locked(self) -> bool:
        """Evict the oldest unpinned item from either container, if needed."""
        if len(self._entries) + len(self._references) < self._max_entries:
            return True
        oldest_legacy = min(
            (key for key, entry in self._entries.items() if not entry.pinned),
            key=lambda key: self._entry_sequences[key],
            default=None,
        )
        oldest_reference = min(
            (key for key, entry in self._references.items() if not entry.pinned),
            key=lambda key: self._references[key].insertion_sequence,
            default=None,
        )
        if oldest_legacy is None and oldest_reference is None:
            return False
        if oldest_reference is not None and (
            oldest_legacy is None
            or self._references[oldest_reference].insertion_sequence
            < self._entry_sequences[oldest_legacy]
        ):
            self._references.pop(oldest_reference, None)
        elif oldest_legacy is not None:
            self._entries.pop(oldest_legacy, None)
            self._entry_sequences.pop(oldest_legacy, None)
        return True

    def add_reference(
        self,
        memory_id: str,
        *,
        storage_namespace: str,
        tenant_id: str,
        pinned: bool = False,
        ttl_seconds: float | None = None,
    ) -> bool:
        """Add/refresh a source candidate; reject growth when all are pinned."""
        if not memory_id or not storage_namespace or not tenant_id:
            return False
        key = (storage_namespace, tenant_id, memory_id)
        ttl = max(0.0, self._default_ttl if ttl_seconds is None else float(ttl_seconds))
        with self._lock:
            self._prune_locked()
            if key not in self._references and not self._make_room_locked():
                return False
            self._references[key] = WorkingMemoryReference(
                memory_id=memory_id,
                ttl_seconds=ttl,
                pinned=pinned,
                expires_at=None if pinned else time.monotonic() + ttl,
                insertion_sequence=self._next_sequence_locked(),
            )
            return True

    def recall_references(
        self, *, storage_namespace: str, tenant_id: str, limit: int = 8,
    ) -> tuple[str, ...]:
        """Return live source IDs in newest-insertion order for one scope."""
        with self._lock:
            self._prune_locked()
            references = [
                entry for key, entry in self._references.items()
                if key[:2] == (storage_namespace, tenant_id)
            ]
            references.sort(key=lambda entry: entry.insertion_sequence, reverse=True)
            return tuple(entry.memory_id for entry in references[:max(0, limit)])

    def remove_reference(
        self, memory_id: str, *, storage_namespace: str, tenant_id: str,
    ) -> bool:
        with self._lock:
            return self._references.pop((storage_namespace, tenant_id, memory_id), None) is not None

    def set_reference_pinned(
        self,
        memory_id: str,
        *,
        storage_namespace: str,
        tenant_id: str,
        pinned: bool,
        ttl_seconds: float | None = None,
    ) -> bool:
        """Suspend expiry, or restart the retained/requested TTL from now."""
        with self._lock:
            self._prune_locked()
            entry = self._references.get((storage_namespace, tenant_id, memory_id))
            if entry is None:
                return False
            if ttl_seconds is not None:
                entry.ttl_seconds = max(0.0, float(ttl_seconds))
            entry.pinned = pinned
            entry.expires_at = None if pinned else time.monotonic() + entry.ttl_seconds
            return True

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
            self._entry_sequences.pop(key, None)
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._entry_sequences.clear()
            self._references.clear()

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
            self._entry_sequences.pop(key, None)
        expired_references = [
            key for key, entry in self._references.items()
            if entry.expires_at is not None and entry.expires_at <= now
        ]
        for reference_key in expired_references:
            self._references.pop(reference_key, None)

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
        entries whose write was workspace- or user-restricted stay inside
        that boundary even though the scope (tenant) is broader.
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
            return len(self._entries) + len(self._references)
