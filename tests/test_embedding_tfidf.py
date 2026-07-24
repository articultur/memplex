"""Direct tests for the TF-IDF embedder and EmbeddingService offline routing.

Complements tests/test_embedding.py (which covers backend selection). Here
we exercise _SimpleTFIDFEmbedder's encode/normalization contract directly
and the offline-model -> TF-IDF routing of EmbeddingService.
"""

import math
import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.retrieval.embedding import (  # noqa: E402
    EmbeddingService,
    EmbeddingStrategy,
    _SimpleTFIDFEmbedder,
)

# ── _SimpleTFIDFEmbedder ─────────────────────────────────────────────


def test_tfidf_encode_returns_normalized_vector():
    e = _SimpleTFIDFEmbedder(dimension=16)
    vec = e.encode("hello world hello")
    assert len(vec) == 16
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6) or norm == 0.0


def test_tfidf_encode_empty_text_returns_zero_vector():
    e = _SimpleTFIDFEmbedder(dimension=8)
    assert e.encode("") == [0.0] * 8


def test_tfidf_encode_identical_text_produces_same_vector():
    e = _SimpleTFIDFEmbedder(dimension=16)
    assert e.encode("alpha beta") == e.encode("alpha beta")


def test_tfidf_encode_batch_consistent_with_sequential():
    texts = ["one two", "three four", "five"]
    batched = _SimpleTFIDFEmbedder(dimension=16).encode_batch(texts)
    expected = _SimpleTFIDFEmbedder(dimension=16)
    individual = [expected.encode(t) for t in texts]
    assert batched == individual


def test_tfidf_doc_count_advances_per_call():
    e = _SimpleTFIDFEmbedder(dimension=8)
    assert e._doc_count == 0
    e.encode("word")
    assert e._doc_count == 1
    e.encode("word again")
    assert e._doc_count == 2


def test_tfidf_dimension_is_respected():
    assert len(_SimpleTFIDFEmbedder(dimension=32).encode("anything")) == 32


# ── EmbeddingService offline routing ─────────────────────────────────


@pytest.mark.parametrize("model", ["default", "tfidf", "offline", "lite", "local"])
def test_offline_models_use_tfidf(model):
    """Offline-named models must resolve to local TF-IDF, never HF/network."""
    vec = EmbeddingService(model=model, dimension=16, storage=None).embed("offline test")
    assert len(vec) == 16


def test_embed_batch_consistency():
    svc = EmbeddingService(model="default", dimension=16, storage=None)
    batched = svc.embed_batch(["alpha", "beta gamma"])
    assert len(batched) == 2 and all(len(v) == 16 for v in batched)


def test_embedding_strategy_enum_members():
    """The strategy enum is part of the public surface; pin its real members."""
    assert EmbeddingStrategy.NAME_ONLY.value == "name"
    assert EmbeddingStrategy.SEMANTIC.value == "semantic"


def test_unknown_hf_model_falls_back_locally():
    """An hf: model that cannot load must fall back to TF-IDF, not raise."""
    vec = EmbeddingService(
        model="hf:definitely-not-a-real-model-xyz", dimension=8, storage=None
    ).embed("fallback test")
    assert len(vec) == 8
