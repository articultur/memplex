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
    Fact,
    Function,
    GraphData,
    MergeResult,
    Observation,
    Preference,
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

    # ── Fact / Preference (OPTIONAL extension points) ───────────────
    # These are *optional* capabilities: the WikiCompiler duck-types
    # ``list_facts`` / ``list_preferences`` (hasattr/getattr), and the
    # service layer feature-checks before persisting extracted
    # Fact/Preference nodes.  Backends that do not support these memory
    # types may simply inherit the defaults below.

    def add_fact(self, fact: Fact) -> None:
        """OPTIONAL: persist a Fact node (upsert by ``fact.id``).

        Default raises :class:`NotImplementedError`; backends supporting
        declarative memory override this.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support Fact storage")

    def add_preference(self, preference: Preference) -> None:
        """OPTIONAL: persist a Preference node (upsert by id).

        Default raises :class:`NotImplementedError`; backends supporting
        preference memory override this.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support Preference storage")

    def get_fact(self, fact_id: str) -> Optional[Fact]:
        """OPTIONAL: retrieve a single Fact by ID, or ``None``.

        Default returns ``None`` (backend has no Fact storage).
        """
        return None

    def get_preference(self, preference_id: str) -> Optional[Preference]:
        """OPTIONAL: retrieve a single Preference by ID, or ``None``.

        Default returns ``None`` (backend has no Preference storage).
        """
        return None

    def list_facts(
        self,
        offset: int = 0,
        limit: int = 1000,
        owner: Optional[str] = None,
    ) -> List[Fact]:
        """OPTIONAL: paginated Fact listing, optionally filtered by *owner*.

        Default returns ``[]`` so duck-typed callers (e.g. WikiCompiler)
        degrade gracefully on backends without Fact storage.
        """
        return []

    def list_preferences(
        self,
        offset: int = 0,
        limit: int = 1000,
        owner: Optional[str] = None,
    ) -> List[Preference]:
        """OPTIONAL: paginated Preference listing, optionally by *owner*.

        Default returns ``[]`` (same reasoning as :meth:`list_facts`).
        """
        return []

    def list_observations(
        self,
        offset: int = 0,
        limit: int = 1000,
        category: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> List[Observation]:
        """OPTIONAL: paginated Observation listing, optionally filtered by
        *category* (see ``OBSERVATION_CATEGORIES``) and/or *owner*.

        Default returns ``[]`` so duck-typed callers (e.g. WikiCompiler,
        which already probes ``list_observations``) degrade gracefully on
        backends without Observation listing support.
        """
        return []

    def get_observation(self, observation_id: str) -> Optional[Observation]:
        """OPTIONAL: retrieve one Observation by ID, or ``None``."""
        return None

    def delete_fact(self, fact_id: str) -> None:
        """OPTIONAL: delete a Fact by ID.  Default is a no-op."""
        return None

    def delete_preference(self, preference_id: str) -> None:
        """OPTIONAL: delete a Preference by ID.  Default is a no-op."""
        return None

    def delete_observation(self, observation_id: str) -> None:
        """OPTIONAL: delete an Observation by ID. Default is a no-op."""
        return None

    @abstractmethod
    def increment_access(self, func_id: str) -> None:
        """Atomically increment ``access_count`` and update
        ``last_accessed_at``.  Must not depend on a prior ``get()`` to
        avoid read-modify-write races.
        """

    def increment_access_batch(self, func_ids) -> None:
        """Increment access_count for many funcs in a single persistence pass.

        Default implementation loops the single-func primitive so every
        backend gets correct behaviour for free. Backends whose
        ``increment_access`` pays a full-store persistence cost (e.g. the
        lite JSON store) MUST override this to persist once for the whole
        batch -- calling the single-func version N times would trigger N
        full-store rewrites, which is the pathological amplifier that
        makes query cost O(results x store_size).
        """
        for func_id in func_ids:
            self.increment_access(func_id)

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
        limit: Optional[int] = None,
    ) -> List[Function]:
        """Return neighbour Functions reachable within *max_hops* edges,
        optionally restricted to *edge_types* and capped in the storage
        query by *limit*. ``None`` preserves the historical unbounded API.
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

    def count_functions(self) -> int:
        """Total Function count, without materializing any node.

        Backends should override with a cheaper implementation (SQL COUNT
        or len(self._functions)); the ABC default paginates through
        list_functions as a correctness-preserving fallback.
        """
        total = 0
        while True:
            page = self.list_functions(offset=total, limit=10_000)
            count = len(page)
            total += count
            if count < 10_000:
                return total

    def list_changes_since(
        self, since: Optional[str] = None, limit: int = 100000
    ) -> List[Function]:
        """Return Functions with updated_at > *since* (incremental sync query).

        Default implementation falls back to list_functions + Python filter.
        Backends with query capabilities (e.g. Postgres) override this to
        push the filter into the database (WHERE updated_at > ?) so the
        server does not load the entire store on every /sync/changes call.
        """
        funcs = self.list_functions(limit=limit)
        if since is None:
            return funcs
        return [f for f in funcs if (f.updated_at or "") > since]

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
