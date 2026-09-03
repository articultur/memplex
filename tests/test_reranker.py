"""Test Reranker: 6-dimensional weighted scoring, each dimension's behaviour,
and CrossEncoderReranker degradation paths.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import sys
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from memplex.models import SearchResult, SourceType
from memplex.retrieval.reranker import (
    CrossEncoderReranker,
    Reranker,
    cosine_similarity,
)

# ── Stub helpers ──────────────────────────────────────────────────────


class _FakeEmbedder:
    """Bag-of-words embedder over a tiny fixed vocab (deterministic)."""

    def __init__(self, vocab=("alpha", "beta", "gamma")):
        self._vocab = list(vocab)
        self.embed_query_calls = 0

    def _vec(self, text):
        words = set(text.lower().split())
        return [1.0 if w in words else 0.0 for w in self._vocab]

    def embed(self, text):
        return self._vec(text)

    def embed_query(self, text):
        """Query-side encode that must not touch document statistics."""
        self.embed_query_calls += 1
        return self._vec(text)


class _LegacyEmbedder:
    """Embedder without embed_query (sentence-transformers style)."""

    def __init__(self):
        self.embed_calls = 0

    def embed(self, text):
        self.embed_calls += 1
        return [1.0, 0.0]


def _sr(
    func_id,
    score=0.5,
    summary="",
    source_type=SourceType.CODE,
    updated_at=None,
    vector_cache=None,
):
    return SearchResult(
        func_id=func_id,
        name=func_id,
        domain="",
        relevance_score=score,
        summary=summary or func_id,
        source_type=source_type,
        updated_at=updated_at,
        vector_cache=vector_cache,
    )


def _weights(**overrides):
    w = {
        "raw_relevance": 0.0,
        "semantic_similarity": 0.0,
        "recency_decay": 0.0,
        "source_authority": 0.0,
        "frequency": 0.0,
    }
    w.update(overrides)
    return w


# ── cosine_similarity ─────────────────────────────────────────────────


def test_cosine_similarity_basic():
    assert abs(cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-6
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) < 1e-6


# ── 6-dimensional weighted scoring ────────────────────────────────────


def test_weighted_sum_decides_ranking():
    """Same candidates, different weights -> different winner."""
    now = datetime.now(UTC)
    old = now - timedelta(days=365)
    a = _sr("a", score=0.9, summary="alpha", updated_at=old)  # high raw, old
    b = _sr("b", score=0.1, summary="beta", updated_at=now)  # low raw, fresh

    raw_first = Reranker(_FakeEmbedder(), weights=_weights(raw_relevance=1.0))
    assert raw_first.rerank("q", [a, b])[0].func_id == "a"

    recency_first = Reranker(_FakeEmbedder(), weights=_weights(recency_decay=1.0))
    assert recency_first.rerank("q", [a, b])[0].func_id == "b"


def test_default_weights_sum_to_one():
    r = Reranker(_FakeEmbedder())
    assert abs(sum(r.weights.values()) - 1.0) < 1e-9
    assert set(r.weights) == {
        "raw_relevance",
        "semantic_similarity",
        "recency_decay",
        "source_authority",
        "frequency",
        "confidence",
    }


def test_rerank_respects_top_k_and_empty_input():
    r = Reranker(_FakeEmbedder())
    assert r.rerank("q", []) == []
    results = [_sr(f"f{i}", score=i / 10) for i in range(5)]
    assert len(r.rerank("q", results, top_k=2)) == 2


# ── recency decay ─────────────────────────────────────────────────────


def test_recency_decay_values():
    now = datetime.now(UTC)
    # _recency_decay is an instance method since the configurable half-life.
    r = Reranker(_FakeEmbedder())
    assert r._recency_decay(None) == 0.5
    assert r._recency_decay("not-a-date") == 0.5
    # Today -> ~1.0
    assert r._recency_decay(now) > 0.99
    # exp(-30/60) ~= 0.607 (docstring previously claimed 0.5 -- regression)
    thirty_days = now - timedelta(days=30)
    assert abs(r._recency_decay(thirty_days) - 0.6065) < 0.01
    # ISO strings accepted; naive datetimes treated as UTC
    assert r._recency_decay(thirty_days.isoformat()) > 0.5
    assert r._recency_decay(thirty_days.replace(tzinfo=None)) > 0.5


def test_recency_dimension_prefers_newer_result():
    now = datetime.now(UTC)
    fresh = _sr("fresh", updated_at=now)
    stale = _sr("stale", updated_at=now - timedelta(days=120))
    r = Reranker(_FakeEmbedder(), weights=_weights(recency_decay=1.0))
    assert r.rerank("q", [stale, fresh])[0].func_id == "fresh"


# ── source authority ──────────────────────────────────────────────────


def test_source_authority_orders_requirement_over_wiki():
    req = _sr("req", source_type=SourceType.REQUIREMENT)
    wiki = _sr("wiki", source_type=SourceType.WIKI)
    r = Reranker(_FakeEmbedder(), weights=_weights(source_authority=1.0))
    assert r.rerank("q", [wiki, req])[0].func_id == "req"


# ── frequency scoring ─────────────────────────────────────────────────


def test_frequency_score_grows_with_access_count():
    hot = SimpleNamespace(access_count=100, last_accessed_at=datetime.now(UTC))
    warm = SimpleNamespace(access_count=5, last_accessed_at=datetime.now(UTC))
    cold = SimpleNamespace(access_count=0, last_accessed_at=None)
    assert (
        Reranker._frequency_score(hot)
        > Reranker._frequency_score(warm)
        > Reranker._frequency_score(cold)
    )


def test_frequency_absence_of_access_evidence_is_neutral():
    """A present-but-never-accessed node must not be punished for absence:
    neutral 0.5, the same convention as the confidence dimension.

    Regression: the old formula scored never-accessed nodes 0.12 while a
    single recent retrieval scored ~0.49, so any document an earlier query
    happened to touch permanently outranked never-recalled memories
    (~0.4 swing at weight 0.10 -- a rich-get-richer prior that decided
    rankings, observed flipping the longmemeval knowledge-update case).
    """
    never = SimpleNamespace(access_count=0, last_accessed_at=None)
    once = SimpleNamespace(access_count=1, last_accessed_at=datetime.now(UTC))
    assert Reranker._frequency_score(never) == 0.5
    # A single recent access sits in the neutral band, not on a cliff
    # above it; only accumulated counts lift the dimension.
    once_score = Reranker._frequency_score(once)
    assert pytest.approx(0.49, abs=0.02) == once_score
    assert abs(once_score - 0.5) < 0.02


def test_frequency_dimension_uses_storage_access_count():
    now = datetime.now(UTC)
    storage = SimpleNamespace(
        get=lambda fid: SimpleNamespace(
            access_count=100 if fid == "hot" else 0,
            last_accessed_at=now if fid == "hot" else None,
        )
    )
    hot = _sr("hot")
    cold = _sr("cold")
    r = Reranker(_FakeEmbedder(), weights=_weights(frequency=1.0), storage=storage)
    assert r.rerank("q", [cold, hot])[0].func_id == "hot"


def test_frequency_defaults_to_neutral_when_storage_missing_or_failing():
    failing = SimpleNamespace(get=lambda fid: (_ for _ in ()).throw(RuntimeError("db down")))
    r = Reranker(_FakeEmbedder(), weights=_weights(frequency=1.0), storage=failing)
    out = r.rerank("q", [_sr("x")])
    assert [x.func_id for x in out] == ["x"]  # no crash, neutral 0.5


# ── semantic similarity ───────────────────────────────────────────────


def test_semantic_dimension_discriminates_by_result_vector():
    """Regression: semantic score must compare query vs the RESULT's own
    vector (previously every hit cached the query vector -> constant 1.0)."""
    match = _sr("match", summary="alpha")
    miss = _sr("miss", summary="gamma")
    r = Reranker(_FakeEmbedder(), weights=_weights(semantic_similarity=1.0))
    ranked = r.rerank("alpha", [miss, match])
    assert ranked[0].func_id == "match"


def test_semantic_dimension_uses_vector_cache_as_result_vector():
    # cache encodes "alpha"-like vs "gamma"-like vectors explicitly
    match = _sr("match", vector_cache=[1.0, 0.0, 0.0])
    miss = _sr("miss", vector_cache=[0.0, 0.0, 1.0])
    r = Reranker(_FakeEmbedder(), weights=_weights(semantic_similarity=1.0))
    ranked = r.rerank("alpha", [miss, match], query_vector=[1.0, 0.0, 0.0])
    assert ranked[0].func_id == "match"


def test_query_side_embedding_uses_embed_query_when_available():
    embedder = _FakeEmbedder()
    r = Reranker(embedder)
    r.rerank("alpha", [_sr("x", summary="beta")])
    # Query + uncached result summary both go through the non-mutating path.
    assert embedder.embed_query_calls == 2


def test_query_side_embedding_falls_back_to_embed():
    embedder = _LegacyEmbedder()
    r = Reranker(embedder)
    r.rerank("q", [_sr("x")])
    assert embedder.embed_calls == 2


# ── CrossEncoderReranker ──────────────────────────────────────────────


def test_cross_encoder_disabled_returns_input_unchanged():
    results = [_sr("a"), _sr("b")]
    cross = CrossEncoderReranker(enabled=False)
    assert cross.rerank("q", results) is results


def test_cross_encoder_missing_dependency_degrades_gracefully(monkeypatch):
    """sentence-transformers absent -> disabled + passthrough, no crash."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    results = [_sr("a", score=0.1), _sr("b", score=0.9)]
    cross = CrossEncoderReranker(enabled=True)
    out = cross.rerank("q", results)
    assert cross.enabled is False
    assert out is results  # input order/scores untouched


def test_cross_encoder_rescores_and_updates_relevance():
    class _FakeModel:
        def predict(self, pairs):
            # Higher score for the pair containing "beta"
            return [2.0 if "beta" in p[1] else 0.5 for p in pairs]

    cross = CrossEncoderReranker(enabled=True)
    cross._model = _FakeModel()
    low = _sr("low", score=0.9, summary="alpha")
    high = _sr("high", score=0.1, summary="beta")
    out = cross.rerank("q", [low, high])
    assert [x.func_id for x in out] == ["high", "low"]
    assert out[0].relevance_score == 2.0
    assert out[1].relevance_score == 0.5
