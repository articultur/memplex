"""Sparse retrieval-leg contract tests (bge-m3 lexical weights).

Covers: sparse_search ranking/normalization/caching on a fake sparse
embedder, fail-closed behaviour when the embedder lacks the sparse API,
the store-level leg wiring behind MEMPLEX_SPARSE_LEG, and the FTS5
field-weights knob (default legacy, weighted ranking, parse fail-closed).
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.storage.lite.search_index import _fts_field_weights
from memplex.storage.lite.vector_index import VectorSearchIndex


class _FakeSparseEmbedder:
    """Deterministic sparse backend: token -> weight dicts."""

    def __init__(self) -> None:
        self.corpus_calls = 0

    def embed_sparse_batch(self, texts, batch_size=None):
        self.corpus_calls += 1
        table = {
            "aloe drink": {"aloe": 0.8, "drink": 0.3, "water": 0.2},
            "orchid mist": {"orchid": 0.9, "mist": 0.4},
            "bus schedule": {"bus": 0.7, "schedule": 0.5},
        }
        out = []
        for t in texts:
            if t in table:
                out.append(table[t])
            elif "aloe" in t and "wednesday" in t:
                out.append({"aloe": 0.6, "wednesday": 0.5, "water": 0.1})
            else:
                out.append({})
        return out


def test_sparse_search_ranks_shared_tokens():
    index = VectorSearchIndex()
    index.set_embedder(_FakeSparseEmbedder())
    docs = [
        ("d1", "aloe drink"),
        ("d2", "orchid mist"),
        ("d3", "bus schedule"),
    ]
    hits = index.sparse_search(docs, "aloe watering on wednesday", top_k=3)
    assert hits and hits[0][0] == "d1", "the aloe doc must win the aloe query"
    assert all(score <= 1.0 for _, score in hits), "scores normalize per query"
    assert not any(doc_id == "d3" for doc_id, _ in hits), "no shared tokens"


def test_sparse_search_caches_corpus():
    index = VectorSearchIndex()
    embedder = _FakeSparseEmbedder()
    index.set_embedder(embedder)
    docs = [("d1", "aloe drink")]
    index.sparse_search(docs, "aloe", top_k=1)
    first = embedder.corpus_calls
    index.sparse_search(docs, "drink", top_k=1)
    assert embedder.corpus_calls == first + 1, (
        "only the new query is embedded; the corpus hit the cache"
    )


def test_sparse_search_fails_closed_without_api():
    class DenseOnly:
        def encode(self, text):
            return [0.0]

    index = VectorSearchIndex()
    index.set_embedder(DenseOnly())
    assert index.sparse_search([("d1", "text")], "query", top_k=1) == []


def test_store_sparse_leg_fuses(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_SPARSE_LEG", "1")
    from memplex.models import Fact, SourceType
    from memplex.storage.lite.store import LiteMemoryStore

    store = LiteMemoryStore(path=tmp_path / "m.json")
    store.add_fact(
        Fact(
            id="aloe_fact",
            subject="user",
            predicate="waters",
            object_="the aloe drink",
            source_type=SourceType.WIKI,
            trust_tier=4,
        )
    )
    store.set_embedder(_FakeSparseEmbedder())
    hits = store.vector_search("aloe watering", top_k=3)
    assert hits, "the sparse leg must surface the shared-token fact"
    assert hits[0].func_id == "aloe_fact"


def test_fts_field_weights_default_and_parse():
    assert _fts_field_weights() == (1.0, 1.0, 1.0)
    os.environ["MEMPLEX_FTS_FIELD_WEIGHTS"] = "8,2,1"
    try:
        assert _fts_field_weights() == (8.0, 2.0, 1.0)
    finally:
        del os.environ["MEMPLEX_FTS_FIELD_WEIGHTS"]
    os.environ["MEMPLEX_FTS_FIELD_WEIGHTS"] = "not-a-float"
    try:
        assert _fts_field_weights() == (1.0, 1.0, 1.0), (
            "parse failure fails closed to the legacy default"
        )
    finally:
        del os.environ["MEMPLEX_FTS_FIELD_WEIGHTS"]
    os.environ["MEMPLEX_FTS_FIELD_WEIGHTS"] = "1,2"
    try:
        assert _fts_field_weights() == (1.0, 1.0, 1.0), (
            "wrong arity fails closed to the legacy default"
        )
    finally:
        del os.environ["MEMPLEX_FTS_FIELD_WEIGHTS"]
