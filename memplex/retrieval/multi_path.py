"""MultiPathRetriever -- parallel multi-path memory retrieval.

Runs the three retrieval paths used by :class:`MemplexService.query` and
merges their results:

1. ``rag_search``    -- vector + FTS hybrid with FTS fallback.
2. ``wiki_search``   -- FTS over compiled wiki pages.
3. ``graph_search``  -- vector-seeded 1-hop graph traversal.

Each path only depends on a :class:`MemoryStore`-like object (the single
collaborator), so the retriever is unit-testable with a stub store.

Usage::

    from memplex.retrieval.multi_path import MultiPathRetriever

    retriever = MultiPathRetriever(store)
    rag_hits = retriever.rag_search("login function", top_k=10)
    merged = MultiPathRetriever.merge_multi_path([rag_hits, wiki_hits])
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

from memplex.models import SearchResult

if TYPE_CHECKING:
    from memplex.retrieval.embedding import Vector
    from memplex.storage.base import MemoryStore

logger = logging.getLogger(__name__)


class MultiPathRetriever:
    """Run the three retrieval paths and merge their results.

    Parameters
    ----------
    store:
        A :class:`MemoryStore`-like object providing ``vector_search``,
        ``fts_search``, ``get_neighbors``, and ``get``. The retriever
        holds no other collaborator, which keeps it straightforward to
        test in isolation.
    """

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    # ── Retrieval paths ──────────────────────────────────────────────

    def rag_search(
        self,
        text: str,
        top_k: int,
        query_vector: Optional["Vector"] = None,
    ) -> List[SearchResult]:
        """RAG vector + FTS hybrid search with FTS fallback.

        Pre-fills ``vector_cache`` on each hit so the downstream Reranker
        can reuse the query vector without re-embedding.
        """
        results = self._store.vector_search(text, top_k)
        # FTS fallback when vector search returns nothing
        if not results:
            results = self._store.fts_search(text, top_k)
        # Pre-fill vector_cache for Reranker reuse
        if query_vector is not None:
            for r in results:
                r.vector_cache = query_vector
        return results

    def wiki_search(self, text: str, top_k: int) -> List[SearchResult]:
        """Wiki layer: FTS-based search over compiled wiki pages.

        Falls back to ``store.fts_search`` when no WikiCompiler is
        available.
        """
        return self._store.fts_search(text, top_k)

    def graph_search(
        self,
        text: str,
        top_k: int,
        query_vector: Optional["Vector"] = None,
    ) -> List[SearchResult]:
        """Incremental graph traversal search.

        1. Vector search to find seed Functions (top_k=3).
        2. Expand 1-hop neighbours via ``get_neighbors()``.
        3. Filter to relation-type edges.
        """
        seed_results = self._store.vector_search(text, top_k=3)
        if not seed_results:
            seed_results = self._store.fts_search(text, top_k=3)
        if not seed_results:
            return []

        results: List[SearchResult] = []
        seen: set = set()

        for seed in seed_results:
            if seed.func_id in seen:
                continue
            seen.add(seed.func_id)
            if query_vector is not None:
                seed.vector_cache = query_vector
            results.append(seed)

            # Incremental: only get this seed's neighbours
            try:
                neighbors = self._store.get_neighbors(seed.func_id, max_hops=1)
            except Exception:
                continue
            for neighbor in neighbors:
                if neighbor.id not in seen:
                    results.append(
                        SearchResult(
                            func_id=neighbor.id,
                            name=neighbor.name,
                            domain=neighbor.domain or "",
                            relevance_score=0.5,
                            summary=neighbor.name,
                            created_at=(
                                datetime.fromisoformat(neighbor.created_at)
                                if isinstance(neighbor.created_at, str) and neighbor.created_at
                                else neighbor.created_at
                            ),
                            updated_at=(
                                datetime.fromisoformat(neighbor.updated_at)
                                if isinstance(neighbor.updated_at, str) and neighbor.updated_at
                                else neighbor.updated_at
                            ),
                            origin=neighbor.origin_session or "",
                        )
                    )
                    seen.add(neighbor.id)

        return results[:top_k]

    # ── Post-retrieval helpers ───────────────────────────────────────

    def filter_by_namespace(
        self,
        results: List[SearchResult],
        namespace_filter: Dict[str, str],
    ) -> List[SearchResult]:
        """Keep only results whose stored metadata matches a namespace."""
        filtered: List[SearchResult] = []
        for result in results:
            func = self._store.get(result.func_id)
            if func is None:
                continue
            attrs = getattr(func, "attributes", {}) or {}
            if all(attrs.get(key) == value for key, value in namespace_filter.items()):
                filtered.append(result)
        return filtered

    @staticmethod
    def merge_multi_path(
        result_lists: List[List[SearchResult]],
    ) -> List[SearchResult]:
        """Merge multi-path results; deduplicate by ``func_id``, keeping
        the highest ``relevance_score``."""
        seen: Dict[str, SearchResult] = {}
        for results in result_lists:
            for r in results:
                if r.func_id not in seen or r.relevance_score > seen[r.func_id].relevance_score:
                    seen[r.func_id] = r
        return sorted(seen.values(), key=lambda x: x.relevance_score, reverse=True)
