"""Test MultiPathRetriever: the three retrieval paths, merge, and namespace filter.

Previously these lived as five private methods on ``MemplexService`` with
zero direct test coverage. After extraction to
``memplex.retrieval.multi_path.MultiPathRetriever`` they are driven here
via a stub store so each path is exercised in isolation.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import datetime
from types import SimpleNamespace

from memplex.models import SearchResult  # noqa: E402
from memplex.retrieval.multi_path import MultiPathRetriever  # noqa: E402

# ── Stub helpers ──────────────────────────────────────────────────────


def _sr(func_id: str, score: float = 0.5, name: str = "") -> SearchResult:
    """Build a SearchResult with only the fields merge/rerank care about."""
    return SearchResult(
        func_id=func_id,
        name=name or func_id,
        domain="",
        relevance_score=score,
        summary=name or func_id,
    )


def _node(
    nid: str,
    *,
    name: str = "",
    domain: str = "",
    created_at="2024-01-01T00:00:00",
    updated_at="2024-01-02T00:00:00",
    origin_session: str = "",
):
    """Build a neighbor node (duck-typed MemoryNode)."""
    return SimpleNamespace(
        id=nid,
        name=name or nid,
        domain=domain,
        created_at=created_at,
        updated_at=updated_at,
        origin_session=origin_session,
    )


class _StubStore:
    """Minimal MemoryStore stub: only the methods MultiPathRetriever calls."""

    def __init__(
        self,
        *,
        vector_hits=None,
        fts_hits=None,
        neighbors_by_id=None,
        funcs_by_id=None,
        vector_throws=False,
    ):
        self._vector_hits = vector_hits or []
        self._fts_hits = fts_hits or []
        self._neighbors_by_id = neighbors_by_id or {}
        self._funcs_by_id = funcs_by_id or {}
        self._vector_throws = vector_throws
        # Call counters help assertions.
        self.vector_calls = 0
        self.fts_calls = 0
        self.get_calls = 0
        self.last_edge_types = None
        self.vector_top_ks = []
        self.neighbor_limits = []

    def vector_search(self, text, top_k):
        self.vector_calls += 1
        self.vector_top_ks.append(top_k)
        if self._vector_throws:
            raise RuntimeError("vector store down")
        return list(self._vector_hits)

    def fts_search(self, text, top_k):
        self.fts_calls += 1
        return list(self._fts_hits)

    def get_neighbors(self, func_id, edge_types=None, max_hops=1, limit=None):
        self.last_edge_types = edge_types
        self.neighbor_limits.append(limit)
        neighbors = list(self._neighbors_by_id.get(func_id, []))
        return neighbors if limit is None else neighbors[:limit]

    def get(self, memory_id):
        self.get_calls += 1
        return self._funcs_by_id.get(memory_id)


class _StubEmbeddingService:
    """Deterministic per-text embeddings so semantic similarity can vary."""

    def embed(self, text):
        return self.embed_query(text)

    def embed_query(self, text):
        # Distinct, text-dependent vector per summary.
        return [float((hash(text) >> i) & 0xFF) / 255.0 for i in range(4)]


# ── rag_search ────────────────────────────────────────────────────────


def test_rag_search_uses_vector_results_when_available():
    hits = [_sr("a", 0.9), _sr("b", 0.7)]
    store = _StubStore(vector_hits=hits)
    r = MultiPathRetriever(store).rag_search("q", top_k=5)
    assert [x.func_id for x in r] == ["a", "b"]
    assert store.vector_calls == 1
    # Vector hit -> FTS must NOT be consulted.
    assert store.fts_calls == 0


def test_rag_search_falls_back_to_fts_when_vector_empty():
    fts_hits = [_sr("c", 0.6)]
    store = _StubStore(vector_hits=[], fts_hits=fts_hits)
    r = MultiPathRetriever(store).rag_search("q", top_k=5)
    assert [x.func_id for x in r] == ["c"]
    assert store.fts_calls == 1


def test_rag_search_prefills_each_hits_own_vector_not_the_query_vector():
    """Regression: vector_cache must hold the RESULT's vector, not the query's.

    Previously the query vector was stored on every hit, which made the
    Reranker's semantic dimension a constant ``cos(q, q) == 1.0``.
    """
    hits = [_sr("a", name="alpha"), _sr("b", name="beta")]
    store = _StubStore(vector_hits=hits)
    embedder = _StubEmbeddingService()
    qv = [0.1, 0.2, 0.3]
    r = MultiPathRetriever(store, embedder).rag_search("q", top_k=5, query_vector=qv)
    assert all(x.vector_cache is not qv for x in r)
    # Each hit carries the embedding of its OWN summary.
    assert r[0].vector_cache == embedder.embed_query("alpha")
    assert r[1].vector_cache == embedder.embed_query("beta")
    # Distinct summaries -> distinct vectors -> semantic dimension can vary.
    assert r[0].vector_cache != r[1].vector_cache


def test_rag_search_leaves_vector_cache_none_without_embedding_service():
    hits = [_sr("a")]
    store = _StubStore(vector_hits=hits)
    r = MultiPathRetriever(store).rag_search("q", top_k=5, query_vector=[0.1])
    # No embedding service -> nothing stuffed into vector_cache (the
    # Reranker embeds result summaries itself downstream).
    assert r[0].vector_cache is None


# ── wiki_search ───────────────────────────────────────────────────────


def test_wiki_search_passes_through_to_fts():
    fts_hits = [_sr("w1"), _sr("w2")]
    store = _StubStore(fts_hits=fts_hits)
    r = MultiPathRetriever(store).wiki_search("q", top_k=3)
    assert [x.func_id for x in r] == ["w1", "w2"]


# ── graph_search ──────────────────────────────────────────────────────


def test_graph_search_returns_empty_when_no_seeds():
    store = _StubStore(vector_hits=[], fts_hits=[])
    assert MultiPathRetriever(store).graph_search("q", top_k=5) == []


def test_graph_search_zero_budget_performs_no_store_reads():
    store = _StubStore(vector_hits=[_sr("seed")])

    assert MultiPathRetriever(store).graph_search("q", top_k=0) == []
    assert store.vector_calls == 0
    assert store.fts_calls == 0
    assert store.neighbor_limits == []


def test_graph_search_expands_one_hop_neighbors():
    seed = _sr("seed", 0.8)
    neighbor = _node("nbr1", name="neighbor one")
    store = _StubStore(vector_hits=[seed], neighbors_by_id={"seed": [neighbor]})
    r = MultiPathRetriever(store).graph_search("q", top_k=5)
    ids = {x.func_id for x in r}
    assert ids == {"seed", "nbr1"}


def test_graph_search_falls_back_to_fts_for_seeds_when_vector_empty():
    fts_seed = _sr("fts_seed", 0.5)
    nbr = _node("nbr")
    store = _StubStore(vector_hits=[], fts_hits=[fts_seed], neighbors_by_id={"fts_seed": [nbr]})
    r = MultiPathRetriever(store).graph_search("q", top_k=5)
    assert {x.func_id for x in r} == {"fts_seed", "nbr"}


def test_graph_search_converts_iso_string_timestamps_to_datetime():
    seed = _sr("seed")
    nbr = _node("nbr", created_at="2024-03-01T10:00:00", updated_at="2024-03-02T11:00:00")
    store = _StubStore(vector_hits=[seed], neighbors_by_id={"seed": [nbr]})
    r = MultiPathRetriever(store).graph_search("q", top_k=5)
    neighbor_result = next(x for x in r if x.func_id == "nbr")
    assert isinstance(neighbor_result.created_at, datetime)
    assert isinstance(neighbor_result.updated_at, datetime)


def test_graph_search_passes_through_datetime_timestamps_unchanged():
    seed = _sr("seed")
    ts = datetime(2024, 5, 1)
    nbr = _node("nbr", created_at=ts, updated_at=ts)
    store = _StubStore(vector_hits=[seed], neighbors_by_id={"seed": [nbr]})
    r = MultiPathRetriever(store).graph_search("q", top_k=5)
    neighbor_result = next(x for x in r if x.func_id == "nbr")
    assert neighbor_result.created_at is ts


def test_graph_search_skips_seed_when_get_neighbors_raises():
    seed1 = _sr("s1")
    seed2 = _sr("s2")
    store = _StubStore(
        vector_hits=[seed1, seed2],
        neighbors_by_id={"s1": RuntimeError("boom"), "s2": [_node("n2")]},
    )
    # get_neighbors is called via the stub; simulate by raising.
    # Wrap to raise on s1.
    orig = store.get_neighbors

    def maybe_raise(func_id, edge_types=None, max_hops=1, limit=None):
        v = orig(func_id, edge_types=edge_types, max_hops=max_hops, limit=limit)
        if isinstance(v, Exception):
            raise v
        return v

    store.get_neighbors = maybe_raise
    r = MultiPathRetriever(store).graph_search("q", top_k=5)
    ids = {x.func_id for x in r}
    # s1 still appears (it was added before the neighbour call) but its
    # neighbours do not; s2's neighbour n2 is present.
    assert "s1" in ids
    assert "s2" in ids
    assert "n2" in ids


def test_graph_search_passes_relation_edge_types_to_store():
    """get_neighbors must receive the relation-type filter promised by the
    docstring (previously edge_types was never passed)."""
    seed = _sr("seed")
    store = _StubStore(vector_hits=[seed], neighbors_by_id={"seed": [_node("n")]})
    MultiPathRetriever(store).graph_search("q", top_k=5)
    assert store.last_edge_types == ["REFERENCES", "DEPENDS_ON", "IMPLEMENTS", "ASSOCIATED_WITH"]


def test_graph_search_rejects_store_without_bounded_neighbor_api():
    """An unbounded legacy neighbor API must fail closed for this path."""
    seed = _sr("seed")
    store = _StubStore(vector_hits=[seed], neighbors_by_id={"seed": [_node("n")]})

    def legacy_get_neighbors(func_id, max_hops=1):
        return [_node("n")]

    store.get_neighbors = legacy_get_neighbors
    r = MultiPathRetriever(store).graph_search("q", top_k=5)
    assert {x.func_id for x in r} == {"seed"}


def test_graph_search_prefills_seed_with_its_own_vector():
    seed = _sr("seed", 0.8, name="seed summary")
    store = _StubStore(vector_hits=[seed], neighbors_by_id={"seed": []})
    embedder = _StubEmbeddingService()
    qv = [0.9, 0.9]
    r = MultiPathRetriever(store, embedder).graph_search("q", top_k=5, query_vector=qv)
    seed_result = next(x for x in r if x.func_id == "seed")
    assert seed_result.vector_cache is not qv
    assert seed_result.vector_cache == embedder.embed_query("seed summary")


def test_graph_search_respects_top_k():
    seed = _sr("seed")
    neighbors = [_node(f"n{i}") for i in range(10)]
    store = _StubStore(vector_hits=[seed], neighbors_by_id={"seed": neighbors})
    r = MultiPathRetriever(store).graph_search("q", top_k=3)
    assert len(r) <= 3


def test_graph_search_pushes_one_budget_into_seed_and_neighbor_reads():
    seeds = [_sr(f"s{i}") for i in range(3)]
    neighbors = {
        seed.func_id: [_node(f"{seed.func_id}-n{i}") for i in range(100)]
        for seed in seeds
    }
    for budget in range(1, 6):
        store = _StubStore(vector_hits=seeds, neighbors_by_id=neighbors)

        result = MultiPathRetriever(store).graph_search("q", top_k=budget)

        assert len(result) <= budget
        assert len(store.vector_top_ks) == 1
        assert sum(store.neighbor_limits) + store.vector_top_ks[0] == budget
        assert all(limit is not None and limit >= 0 for limit in store.neighbor_limits)

    sparse_store = _StubStore(
        vector_hits=[seeds[0]],
        neighbors_by_id={seeds[0].func_id: neighbors[seeds[0].func_id]},
    )
    MultiPathRetriever(sparse_store).graph_search("q", top_k=5)
    assert sparse_store.vector_top_ks[0] + sum(sparse_store.neighbor_limits) == 5


def test_graph_search_deduplicates_neighbor_seen_across_seeds():
    s1 = _sr("s1")
    s2 = _sr("s2")
    shared = _node("shared")
    store = _StubStore(
        vector_hits=[s1, s2],
        neighbors_by_id={"s1": [shared], "s2": [shared]},
    )
    r = MultiPathRetriever(store).graph_search("q", top_k=20)
    assert sum(1 for x in r if x.func_id == "shared") == 1


# ── filter_by_namespace ───────────────────────────────────────────────


def _func(attrs=None):
    return SimpleNamespace(attributes=attrs or {})


def test_filter_by_namespace_keeps_matching_results():
    r1, r2 = _sr("a"), _sr("b")
    store = _StubStore(funcs_by_id={"a": _func({"agent": "codex"}), "b": _func({"agent": "other"})})
    out = MultiPathRetriever(store).filter_by_namespace([r1, r2], {"agent": "codex"})
    assert [x.func_id for x in out] == ["a"]


def test_filter_by_namespace_requires_all_keys_to_match():
    r1 = _sr("a")
    store = _StubStore(funcs_by_id={"a": _func({"agent": "codex", "user": "alice"})})
    out = MultiPathRetriever(store).filter_by_namespace([r1], {"agent": "codex", "user": "bob"})
    assert out == []


def test_filter_by_namespace_skips_results_with_missing_function():
    r1, r2 = _sr("present"), _sr("missing")
    store = _StubStore(funcs_by_id={"present": _func({"k": "v"})})
    out = MultiPathRetriever(store).filter_by_namespace([r1, r2], {"k": "v"})
    assert [x.func_id for x in out] == ["present"]


def test_filter_by_namespace_handles_func_with_no_attributes():
    r1 = _sr("a")
    store = _StubStore(funcs_by_id={"a": SimpleNamespace()})  # no .attributes
    out = MultiPathRetriever(store).filter_by_namespace([r1], {"k": "v"})
    # No attributes -> nothing matches -> dropped (no crash).
    assert out == []


class _BulkStore(_StubStore):
    """Store exposing a bulk get_many (what the N+1 fix should prefer)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.get_many_calls = 0

    def get_many(self, func_ids):
        self.get_many_calls += 1
        self.last_get_many_ids = list(func_ids)
        return {fid: self._funcs_by_id.get(fid) for fid in func_ids}

    def get(self, memory_id):  # per-id path must NOT be used when bulk works
        raise AssertionError("per-id get() called despite get_many")


def test_filter_by_namespace_uses_bulk_get_many_once():
    """Regression for the N+1: one bulk call instead of one get per result."""
    results = [_sr("a"), _sr("b"), _sr("a")]  # duplicate id included
    store = _BulkStore(
        funcs_by_id={"a": _func({"agent": "codex"}), "b": _func({"agent": "other"})}
    )
    out = MultiPathRetriever(store).filter_by_namespace(results, {"agent": "codex"})
    assert [x.func_id for x in out] == ["a", "a"]
    assert store.get_many_calls == 1
    # De-duplicated ids in the bulk request.
    assert store.last_get_many_ids == ["a", "b"]


def test_filter_by_namespace_falls_back_to_get_when_get_many_raises():
    class _FlakyBulkStore(_BulkStore):
        def get_many(self, func_ids):
            raise RuntimeError("bulk down")

    store = _FlakyBulkStore(funcs_by_id={"a": _func({"k": "v"})})
    # Restore per-id get (parent disables it).
    store.get = _StubStore.get.__get__(store)
    out = MultiPathRetriever(store).filter_by_namespace([_sr("a")], {"k": "v"})
    assert [x.func_id for x in out] == ["a"]


# ── merge_multi_path ──────────────────────────────────────────────────


def test_merge_dedups_keeping_highest_score():
    a_low = _sr("x", 0.3)
    a_high = _sr("x", 0.9)
    out = MultiPathRetriever.merge_multi_path([[a_low], [a_high]])
    assert len(out) == 1
    assert out[0].relevance_score == 0.9


def test_merge_combines_across_lists():
    out = MultiPathRetriever.merge_multi_path([[_sr("a", 0.5)], [_sr("b", 0.7)]])
    assert [x.func_id for x in out] == ["b", "a"]  # sorted desc


def test_merge_sorts_descending_by_relevance():
    out = MultiPathRetriever.merge_multi_path([[_sr("a", 0.1), _sr("b", 0.9), _sr("c", 0.5)]])
    scores = [x.relevance_score for x in out]
    assert scores == sorted(scores, reverse=True)


def test_merge_empty_input_returns_empty():
    assert MultiPathRetriever.merge_multi_path([]) == []
    assert MultiPathRetriever.merge_multi_path([[], []]) == []


def test_merge_within_list_dedup_also_keeps_highest():
    a1 = _sr("a", 0.2)
    a2 = _sr("a", 0.8)
    out = MultiPathRetriever.merge_multi_path([[a1, a2]])
    assert len(out) == 1
    assert out[0].relevance_score == 0.8


# ── wiki_search with injected wiki searcher (Wave 2a) ──────────────────


class _StubWikiSearcher:
    """Duck-typed wiki searcher (DualIndexSearch-shaped)."""

    def __init__(self, hits=None, raises=False):
        self._hits = hits or []
        self._raises = raises
        self.calls = []

    def search(self, text, top_k=10):
        self.calls.append((text, top_k))
        if self._raises:
            raise RuntimeError("wiki index down")
        return list(self._hits)[:top_k]


def test_wiki_search_uses_injected_searcher():
    hits = [_sr("w1", 0.9), _sr("w2", 0.8)]
    searcher = _StubWikiSearcher(hits=hits)
    store = _StubStore(fts_hits=[_sr("fts")])
    r = MultiPathRetriever(store, wiki_searcher=searcher).wiki_search("login", top_k=5)
    assert [x.func_id for x in r] == ["w1", "w2"]
    # The injected searcher handled the query; store FTS was NOT consulted.
    assert searcher.calls == [("login", 5)]
    assert store.fts_calls == 0


def test_wiki_search_falls_back_to_fts_when_searcher_raises():
    searcher = _StubWikiSearcher(raises=True)
    store = _StubStore(fts_hits=[_sr("fts_fallback")])
    r = MultiPathRetriever(store, wiki_searcher=searcher).wiki_search("q", top_k=3)
    assert [x.func_id for x in r] == ["fts_fallback"]
    assert store.fts_calls == 1


def test_wiki_search_without_searcher_keeps_fts_passthrough():
    """Default behaviour unchanged: no wiki_searcher -> store.fts_search."""
    store = _StubStore(fts_hits=[_sr("w1")])
    r = MultiPathRetriever(store).wiki_search("q", top_k=3)
    assert [x.func_id for x in r] == ["w1"]
    assert store.fts_calls == 1


def test_wiki_search_with_real_dual_index_search(tmp_path):
    """Integration: a real DualIndexSearch over compiled wiki pages serves
    wiki_search end-to-end."""
    from memplex.models import WikiPage
    from memplex.wiki.search import DualIndexSearch

    class _Embedder:
        def embed(self, text):
            return [float(len(text)) % 7, 1.0]

    searcher = DualIndexSearch(wiki_dir=tmp_path, embedding_service=_Embedder())
    searcher.add_page(WikiPage(page_id="p1", content="login flow with oauth tokens"))
    searcher.add_page(WikiPage(page_id="p2", content="database migration runbook"))

    store = _StubStore()
    r = MultiPathRetriever(store, wiki_searcher=searcher).wiki_search("oauth", top_k=5)
    # FTS hit ranks first via RRF; store FTS was not consulted.
    assert r[0].func_id == "p1"
    assert store.fts_calls == 0
