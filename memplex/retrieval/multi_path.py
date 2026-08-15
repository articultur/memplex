"""MultiPathRetriever -- parallel multi-path memory retrieval.

Runs the three retrieval paths used by :class:`MemplexService.query` and
merges their results:

1. ``rag_search``    -- vector + FTS hybrid with FTS fallback.
2. ``wiki_search``   -- dual-index search over compiled wiki pages when a
   wiki searcher is injected, otherwise FTS passthrough.
3. ``graph_search``  -- vector-seeded 1-hop graph traversal.

Each path only requires a :class:`MemoryStore`-like object (an optional
:class:`EmbeddingService` may be added to pre-fill result vectors), so the
retriever is unit-testable with a stub store.

Usage::

    from memplex.retrieval.multi_path import MultiPathRetriever

    retriever = MultiPathRetriever(store)
    rag_hits = retriever.rag_search("login function", top_k=10)
    merged = MultiPathRetriever.merge_multi_path([rag_hits, wiki_hits])
"""

from __future__ import annotations

import logging
from datetime import datetime
from itertools import islice
from typing import TYPE_CHECKING, Dict, List, Optional

from memplex.models import SearchResult

if TYPE_CHECKING:
    from memplex.retrieval.embedding import EmbeddingService, Vector
    from memplex.storage.base import MemoryStore

logger = logging.getLogger(__name__)

# Relation-type edges traversed by ``graph_search`` (mirrors the strong
# relationship types used for community detection in ``memplex.wiki``).
_RELATION_EDGE_TYPES = ["REFERENCES", "DEPENDS_ON", "IMPLEMENTS", "ASSOCIATED_WITH"]


class MultiPathRetriever:
    """Run the three retrieval paths and merge their results.

    Parameters
    ----------
    store:
        A :class:`MemoryStore`-like object providing ``vector_search``,
        ``fts_search``, ``get_neighbors``, and ``get`` (optionally a bulk
        ``get_many``). The retriever holds no other required collaborator,
        which keeps it straightforward to test in isolation.
    embedding_service:
        Optional :class:`EmbeddingService`. When given, each hit's
        ``vector_cache`` is pre-filled with the hit's own embedding so the
        downstream Reranker can skip re-embedding.
    wiki_searcher:
        Optional duck-typed wiki searcher exposing ``search(text, top_k)``
        and returning :class:`SearchResult` objects (e.g.
        :class:`memplex.wiki.search.DualIndexSearch`).  When given,
        :meth:`wiki_search` runs a real dual-index (FTS + vector) search
        over compiled wiki pages; without it the path passes through to
        ``store.fts_search``.
    """

    def __init__(
        self,
        store: "MemoryStore",
        embedding_service: Optional["EmbeddingService"] = None,
        wiki_searcher: Optional[object] = None,
    ) -> None:
        self._store = store
        self._embedding_service = embedding_service
        self._wiki_searcher = wiki_searcher

    # ── Retrieval paths ──────────────────────────────────────────────

    def rag_search(
        self,
        text: str,
        top_k: int,
        query_vector: Optional["Vector"] = None,
    ) -> List[SearchResult]:
        """RAG vector + FTS hybrid search with FTS fallback.

        Pre-fills ``vector_cache`` on each hit with the hit's OWN embedding
        (when an embedding service is configured) so the downstream Reranker
        can compute a genuine query-vs-result cosine similarity.  The
        *query_vector* parameter is retained for call-site compatibility and
        no longer stored on results -- storing it made the semantic
        dimension a constant ``cos(q, q) == 1.0``.
        """
        results = self._store.vector_search(text, top_k)
        # FTS fallback when vector search returns nothing
        if not results:
            results = self._store.fts_search(text, top_k)
        # Pre-fill vector_cache with each hit's own vector for Reranker reuse
        self._prefill_result_vectors(results)
        return results

    def wiki_search(self, text: str, top_k: int) -> List[SearchResult]:
        """Wiki layer: search over compiled wiki pages.

        When a ``wiki_searcher`` was injected (e.g.
        :class:`memplex.wiki.search.DualIndexSearch`), runs its real
        dual-index (FTS + vector, RRF-merged) search and returns the
        mapped :class:`SearchResult` hits.  Otherwise falls back to
        ``store.fts_search``.
        """
        if self._wiki_searcher is not None:
            try:
                return list(self._wiki_searcher.search(text, top_k))
            except Exception as exc:
                logger.debug("wiki_search: searcher failed, falling back to FTS: %s", exc)
        return self._store.fts_search(text, top_k)

    def graph_search(
        self,
        text: str,
        top_k: int,
        query_vector: Optional["Vector"] = None,
    ) -> List[SearchResult]:
        """Incremental graph traversal search.

        1. Spend part of ``top_k`` on at most three seed Functions.
        2. Divide the remaining budget across bounded 1-hop reads.
        3. Filter to relation-type edges.

        The *query_vector* parameter is retained for call-site
        compatibility; seeds get their own embedding in ``vector_cache``
        (see :meth:`rag_search`).
        """
        budget = max(0, int(top_k))
        if budget == 0:
            return []
        seed_limit = min(3, max(1, (budget + 1) // 2))
        seed_results = list(
            islice(self._store.vector_search(text, top_k=seed_limit), seed_limit)
        )
        if not seed_results:
            seed_results = list(
                islice(self._store.fts_search(text, top_k=seed_limit), seed_limit)
            )
        if not seed_results:
            return []

        results: List[SearchResult] = []
        seen: set = set()
        unique_seeds: List[SearchResult] = []
        for seed in seed_results:
            if seed.func_id in seen:
                continue
            seen.add(seed.func_id)
            self._prefill_result_vectors([seed])
            results.append(seed)
            unique_seeds.append(seed)

        # Reserve the full requested seed allowance even when the backend
        # returns fewer hits: seed search may still have spent that work.
        neighbor_budget = max(0, budget - seed_limit)
        if not unique_seeds or neighbor_budget == 0:
            return results[:budget]
        per_seed, remainder = divmod(neighbor_budget, len(unique_seeds))

        for index, seed in enumerate(unique_seeds):
            quota = per_seed + (1 if index < remainder else 0)
            if quota == 0:
                continue
            try:
                try:
                    neighbors = self._store.get_neighbors(
                        seed.func_id,
                        edge_types=_RELATION_EDGE_TYPES,
                        max_hops=1,
                        limit=quota,
                    )
                except TypeError:
                    # A legacy store may omit edge_types, but it must still
                    # accept the hard ``limit``. Never fall back to an
                    # unbounded neighbour read.
                    neighbors = self._store.get_neighbors(
                        seed.func_id,
                        max_hops=1,
                        limit=quota,
                    )
            except Exception as exc:
                logger.debug("graph_search: get_neighbors failed for %s: %s", seed.func_id, exc)
                continue
            for neighbor in islice(neighbors, quota):
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

        return results[:budget]

    # ── Post-retrieval helpers ───────────────────────────────────────

    def _prefill_result_vectors(self, results: List[SearchResult]) -> None:
        """Fill ``vector_cache`` with each result's OWN embedding.

        Skipped when no embedding service is configured; the Reranker then
        embeds result summaries itself.
        """
        if self._embedding_service is None:
            return
        for r in results:
            if r.vector_cache is None:
                r.vector_cache = self._embed_query_text(r.summary)

    def _embed_query_text(self, text: str) -> "Vector":
        """Embed query-time text without polluting TF-IDF corpus statistics."""
        embed_query = getattr(self._embedding_service, "embed_query", None)
        if callable(embed_query):
            return embed_query(text)
        return self._embedding_service.embed(text)

    def _load_functions(self, func_ids: List[str]) -> Dict[str, object]:
        """Load Functions by id, preferring a single bulk call.

        Uses the store's ``get_many`` when available (avoids the N+1
        per-result ``get``); otherwise falls back to per-id ``get`` over
        de-duplicated ids.
        """
        unique_ids = list(dict.fromkeys(func_ids))
        get_many = getattr(self._store, "get_many", None)
        if callable(get_many):
            try:
                loaded = get_many(unique_ids)
            except Exception as exc:
                logger.debug(
                    "filter_by_namespace: bulk get_many failed (%s); "
                    "falling back to per-id get",
                    exc,
                )
            else:
                if isinstance(loaded, dict):
                    return loaded
                return {f.id: f for f in loaded if f is not None}
        return {fid: self._store.get(fid) for fid in unique_ids}

    def _get_typed_node(self, node_id: str) -> Optional[object]:
        """Resolve a Fact/Preference node via the store's optional typed
        APIs (duck-typed ``get_fact`` / ``get_preference``)."""
        for getter_name in ("get_fact", "get_preference"):
            getter = getattr(self._store, getter_name, None)
            if not callable(getter):
                continue
            try:
                node = getter(node_id)
            except Exception as exc:
                logger.debug(
                    "filter_by_namespace: typed lookup via %s failed for %s: %s",
                    getter_name,
                    node_id,
                    exc,
                )
                node = None
            if node is not None:
                return node
        return None

    def filter_by_namespace(
        self,
        results: List[SearchResult],
        namespace_filter: Dict[str, Optional[str]] | List[Dict[str, Optional[str]]],
    ) -> List[SearchResult]:
        """Keep only results whose stored metadata matches a namespace.

        A list of filters is an OR expression; keys inside each filter are
        exact-match AND conditions. ``None`` matches a missing key, which
        lets callers express safe legacy fallbacks without also matching
        newly-versioned records.

        Functions match on their namespace plus ``attributes`` maps.
        Fact/Preference nodes resolve through the optional typed store APIs
        and use their persisted base ``namespace`` projection. Legacy typed
        nodes fall back only to owner/session fields and must match the
        explicit legacy-typed filter marker; unverifiable keys never widen
        visibility.
        """
        if not results:
            return []
        filters = [namespace_filter] if isinstance(namespace_filter, dict) else namespace_filter

        def matches(metadata: Dict[str, object]) -> bool:
            return any(
                all(metadata.get(key) == value for key, value in candidate.items())
                for candidate in filters
            )

        funcs_by_id = self._load_functions([r.func_id for r in results])
        filtered: List[SearchResult] = []
        for result in results:
            func = funcs_by_id.get(result.func_id)
            if func is None:
                typed = self._get_typed_node(result.func_id)
                if typed is None:
                    continue
                projection = dict(getattr(typed, "namespace", {}) or {})
                projection.setdefault("memplex_user_id", getattr(typed, "owner", None))
                projection.setdefault(
                    "memplex_session_id", getattr(typed, "origin_session", None)
                )
                if not getattr(typed, "namespace", None):
                    projection["memplex_legacy_typed"] = "true"
                if matches(projection):
                    filtered.append(result)
                continue
            attrs = dict(getattr(func, "namespace", {}) or {})
            attrs.update(getattr(func, "attributes", {}) or {})
            # S2/S4 fix: project typed identity/ACL fields into the
            # filterable metadata so domain-bound and team-tier filters
            # match even when the write path ran before the namespace
            # projection landed (or via direct store.add).
            if getattr(func, "domain", None):
                attrs.setdefault("domain", func.domain)
            if getattr(func, "knowledge_tier", None):
                attrs.setdefault("knowledge_tier", func.knowledge_tier)
            if getattr(func, "visibility", None):
                attrs.setdefault("memplex_visibility", func.visibility)
            if getattr(func, "workspace_id", None):
                attrs.setdefault("memplex_workspace_id", func.workspace_id)
            if getattr(func, "tenant_id", None):
                attrs.setdefault("memplex_tenant_id", func.tenant_id)
            if getattr(func, "owner_subject_id", None):
                attrs.setdefault("memplex_subject_id", func.owner_subject_id)
            if matches(attrs):
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
