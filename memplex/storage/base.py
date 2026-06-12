"""MemoryStore abstract base class -- unified data access layer.

Pure CRUD + basic retrieval.  No orchestration logic (that belongs in
MemplexService).  Every concrete backend (Lite, Standard, Enterprise)
implements this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from memplex.models import (
    BatchResult,
    ChangelogEvent,
    Function,
    GraphData,
    MergeResult,
    Observation,
    SearchFilters,
    SearchResult,
    SourceDocument,
)


class MemoryStore(ABC):
    """Pure data access layer -- CRUD + basic retrieval, no orchestration."""

    # ── Write operations ────────────────────────────────────────────

    @abstractmethod
    def add(self, func: Function, source: SourceDocument) -> None:
        """Add a Function.  If a Function with the same *name_normalized*
        already exists, its FieldValues are merged instead.

        Concurrency (Lite backend): single-threaded, optimistic lock skipped.
        """

    @abstractmethod
    def add_batch(
        self,
        funcs: List[Function],
        sources: List[SourceDocument],
    ) -> BatchResult:
        """Batch add.  Calls :meth:`add` per item; single-item failure does
        **not** abort the rest.  Failed items are recorded in
        ``BatchResult.failed_items``.
        """

    @abstractmethod
    def add_observation(self, observation: Observation) -> None:
        """Persist an Observation event."""

    @abstractmethod
    def increment_access(self, func_id: str) -> None:
        """Atomically increment ``access_count`` and update
        ``last_accessed_at``.  Must not depend on a prior ``get()`` to
        avoid read-modify-write races.
        """

    # ── Retrieval ───────────────────────────────────────────────────

    @abstractmethod
    def vector_search(self, text: str, top_k: int = 5) -> List[SearchResult]:
        """Semantic / vector similarity search."""

    @abstractmethod
    def fts_search(self, text: str, top_k: int = 10) -> List[SearchResult]:
        """Full-text / keyword search."""

    @abstractmethod
    def filter(self, filters: SearchFilters) -> List[Function]:
        """Structured filter over stored Functions."""

    # ── Read operations ─────────────────────────────────────────────

    @abstractmethod
    def get(self, func_id: str) -> Optional[Function]:
        """Retrieve a single Function by ID, or ``None``."""

    @abstractmethod
    def get_neighbors(
        self,
        func_id: str,
        edge_types: Optional[List[str]] = None,
        max_hops: int = 1,
    ) -> List[Function]:
        """Return neighbour Functions reachable within *max_hops* edges,
        optionally restricted to *edge_types*.
        """

    @abstractmethod
    def get_graph(
        self,
        func_ids: Optional[List[str]] = None,
    ) -> GraphData:
        """Return sub-graph.  If *func_ids* is ``None`` the full graph is
        returned.
        """

    @abstractmethod
    def get_timeline(
        self,
        func_id: str,
        limit: int = 20,
    ) -> List[ChangelogEvent]:
        """Return recent ChangelogEvents for a Function."""

    @abstractmethod
    def list_functions(
        self,
        offset: int = 0,
        limit: int = 1000,
        owner: Optional[str] = None,
    ) -> List[Function]:
        """Paginated listing, optionally filtered by *owner*."""

    # ── Delete / merge / clear ──────────────────────────────────────

    @abstractmethod
    def delete(self, func_id: str) -> None:
        """Soft-delete a Function and its associated graph edges."""

    @abstractmethod
    def merge(self, sub_graph: GraphData) -> MergeResult:
        """Incrementally fuse *sub_graph* into the main graph.  Existing
        nodes are merged; new ones are inserted.
        """

    @abstractmethod
    def clear(self) -> None:
        """Remove all stored data (Functions, edges, changelog)."""
