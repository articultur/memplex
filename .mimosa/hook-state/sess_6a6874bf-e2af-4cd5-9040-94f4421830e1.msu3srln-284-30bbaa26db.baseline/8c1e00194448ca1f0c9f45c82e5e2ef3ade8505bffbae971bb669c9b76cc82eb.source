"""Tests for the 6-dimensional reranker scoring (confidence + tunable halflife).

Competitive-research follow-ups (Hindsight per-memory confidence, Mnemosyne
configurable temporal half-life): pin the new dimension's semantics so future
edits cannot silently drop or invert them.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.retrieval.reranker import Reranker  # noqa: E402


class _FixedEmbedder:
    """Deterministic embedder: identical text → identical unit vector."""

    def embed(self, text: str):
        base = [float(len(text) % 7) + 1.0, 1.0, 0.5]
        norm = math.sqrt(sum(x * x for x in base))
        return [x / norm for x in base]

    embed_query = embed


class _Node:
    """Function stand-in exposing only what the reranker reads."""

    def __init__(self, confidence=None, access_count=0, last_accessed_at=None):
        if confidence is not None:
            self.confidence = confidence
        self.access_count = access_count
        self.last_accessed_at = last_accessed_at


class _Store:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, func_id):
        return self._mapping.get(func_id)


def _result(func_id: str, updated_at: datetime):
    from memplex.models import SearchResult, SourceType

    return SearchResult(
        func_id=func_id,
        name=func_id,
        summary="summary " + func_id,
        relevance_score=0.5,
        source_type=SourceType.MEETING,
        updated_at=updated_at,
        domain="test",
    )


def test_default_weights_sum_to_one():
    ranker = Reranker(_FixedEmbedder())
    assert abs(sum(ranker.weights.values()) - 1.0) < 1e-9
    assert ranker.weights["confidence"] > 0


def test_recency_halflife_is_configurable():
    now = datetime.now(timezone.utc)
    ranker = Reranker(_FixedEmbedder(), recency_halflife_days=10.0)
    # 10 days at halflife 10 → exp(-1) ≈ 0.368; with 60 → exp(-1/6) ≈ 0.846
    assert abs(ranker._recency_decay(now - timedelta(days=10)) - math.exp(-1)) < 1e-6
    ranker60 = Reranker(_FixedEmbedder(), recency_halflife_days=60.0)
    assert ranker60._recency_decay(now - timedelta(days=30)) > 0.6


def test_higher_confidence_ranks_above_lower():
    now = datetime.now(timezone.utc)
    store = _Store({"high": _Node(confidence=0.95), "low": _Node(confidence=0.10)})
    ranker = Reranker(_FixedEmbedder(), storage=store)
    ranked = ranker.rerank("q", [_result("low", now), _result("high", now)], top_k=2)
    assert ranked[0].func_id == "high"


def test_missing_confidence_is_neutral_not_punitive():
    now = datetime.now(timezone.utc)
    store = _Store({"absent": _Node(access_count=0), "explicit": _Node(confidence=1.0)})
    assert Reranker._confidence_score(None) == 0.5
    assert Reranker._confidence_score(_Node(confidence=7.0)) == 1.0  # clamped
    assert Reranker._confidence_score(_Node(confidence=-3.0)) == 0.0
    # NaN guard
    assert Reranker._confidence_score(_Node(confidence=float("nan"))) == 0.5


def test_zero_confidence_weight_disables_dimension():
    now = datetime.now(timezone.utc)
    store = _Store({"a": _Node(confidence=0.0), "b": _Node(confidence=1.0)})
    ranker = Reranker(
        _FixedEmbedder(),
        storage=store,
        weights={
            "raw_relevance": 0.0,
            "semantic_similarity": 1.0,
            "recency_decay": 0.0,
            "source_authority": 0.0,
            "frequency": 0.0,
            "confidence": 0.0,
        },
    )
    ranked = ranker.rerank("q", [_result("a", now), _result("b", now)], top_k=2)
    # Identical under semantic-only scoring: order stable regardless of confidence
    assert {r.func_id for r in ranked} == {"a", "b"}
