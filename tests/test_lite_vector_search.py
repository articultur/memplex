"""Tests for the lite backend's semantic vector search leg.

The vector leg mirrors the postgres tsvector+pgvector hybrid: it lights
up only when an embedder is injected, embeds corpus documents lazily,
and fuses with the lexical FTS leg on a per-leg normalized scale. These
tests pin the behavioural contract with a deterministic topic-axis stub
embedder -- no network, no model download.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from pathlib import Path
from typing import ClassVar

import pytest

from memplex.models import (
    Fact,
    Function,
    SourceDocument,
    SourceType,
)
from memplex.storage.lite.store import LiteMemoryStore

# ── Helpers ──────────────────────────────────────────────────────────


class _TopicEmbedder:
    """Deterministic keyword->axis embedder.

    ``cat``/``feline`` share axis 0 and ``dog``/``canine`` share axis 1,
    so a query with zero lexical overlap with a document ("cat" vs "The
    feline sleeps") still lands on its vector -- the property a real
    semantic model provides and the lexical stack cannot.
    """

    AXES: ClassVar[dict[str, int]] = {"cat": 0, "feline": 0, "dog": 1, "canine": 1}

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    def embed(self, text: str) -> list[float]:
        return self.embed_query(text)

    def embed_query(self, text: str) -> list[float]:
        self.embedded_texts.append(text)
        vector = [0.0, 0.0, 0.0]
        for keyword, axis in self.AXES.items():
            if keyword in text.lower():
                vector[axis] = 1.0
        if vector[0] == 0.0 and vector[1] == 0.0:
            vector[2] = 1.0
        return vector

    def embed_batch(self, texts, batch_size=32):
        return [self.embed_query(t) for t in texts]


class _ExplodingEmbedder:
    def embed(self, text):
        raise RuntimeError("embedding backend down")

    def embed_query(self, text):
        raise RuntimeError("embedding backend down")


def _make_store(tmp_path: Path) -> LiteMemoryStore:
    return LiteMemoryStore(path=tmp_path / "memory.json")


def _make_source() -> SourceDocument:
    return SourceDocument(type="text", source_type=SourceType.WIKI)


def _make_func(func_id: str, name: str) -> Function:
    return Function(
        id=func_id,
        name=name,
        name_normalized=name.lower().replace(" ", "_"),
        source_type=SourceType.WIKI,
    )


def _seed_corpus(store: LiteMemoryStore) -> None:
    store.add(_make_func("feline", "The feline sleeps on the sofa"), _make_source())
    store.add(_make_func("canine", "The canine barks at the mail carrier"), _make_source())
    store.add(_make_func("quantum", "Quantum tunneling through the barrier"), _make_source())


# ── Semantic gap closure ─────────────────────────────────────────────


def test_vector_leg_finds_semantically_related_doc_without_lexical_overlap(tmp_path):
    """A query sharing no content token with the answer document must
    still retrieve it once an embedder is injected -- the exact gap the
    paraphrase benchmark quantified (low-overlap recall@1 0.027)."""
    store = _make_store(tmp_path)
    _seed_corpus(store)
    store.set_embedder(_TopicEmbedder())

    hits = store.vector_search("cat", top_k=3)

    assert hits
    assert hits[0].func_id == "feline"
    assert hits[0].relevance_score > 0.0


def test_vector_leg_off_without_embedder_keeps_fts_behaviour(tmp_path):
    """No embedder injected -> vector_search is byte-for-byte the FTS
    path: no vector work, identical results and ordering."""
    store = _make_store(tmp_path)
    _seed_corpus(store)

    fts_hits = store.fts_search("feline", top_k=3)
    vector_hits = store.vector_search("feline", top_k=3)

    assert [h.func_id for h in vector_hits] == [h.func_id for h in fts_hits]
    assert [h.relevance_score for h in vector_hits] == [
        h.relevance_score for h in fts_hits
    ]


def test_vector_leg_includes_facts_and_preferences(tmp_path):
    """Typed memories participate in the semantic leg via the same text
    projection the pure-Python BM25 path uses."""
    store = _make_store(tmp_path)
    store.add_fact(
        Fact(
            id="fact-feline",
            name="feline fact",
            subject="sofa",
            predicate="hosts",
            object_="a feline",
            source_type=SourceType.WIKI,
        )
    )
    store.set_embedder(_TopicEmbedder())

    hits = store.vector_search("cat", top_k=3)

    assert "fact-feline" in [h.func_id for h in hits]


# ── Fusion semantics ─────────────────────────────────────────────────


def test_fusion_normalizes_legs_and_takes_best_contribution(tmp_path):
    """Legs fuse on each leg's own maximum scale: the lexical leg's best
    hit and the vector leg's best hit both reach 1.0 and tie-break by
    func_id; a doc strong in either leg is not buried by the other leg's
    score magnitudes."""
    store = _make_store(tmp_path)
    _seed_corpus(store)
    store.set_embedder(_TopicEmbedder())

    hits = store.vector_search("feline cat", top_k=3)

    by_id = {h.func_id: h.relevance_score for h in hits}
    # Both legs rank "feline" first -> fused 1.0 on both legs' scales.
    assert by_id["feline"] == pytest.approx(1.0)
    # Deterministic ordering for equal scores.
    assert [h.func_id for h in hits] == sorted(
        by_id, key=lambda fid: (-by_id[fid], fid)
    )


def test_vector_leg_degrades_gracefully_when_embedder_fails(tmp_path):
    """A failing embedder must never fail search: the lexical results
    stand and the query returns exactly the FTS path's output."""
    store = _make_store(tmp_path)
    _seed_corpus(store)
    store.set_embedder(_ExplodingEmbedder())

    fts_hits = store.fts_search("feline", top_k=3)
    hits = store.vector_search("feline", top_k=3)

    assert [h.func_id for h in hits] == [h.func_id for h in fts_hits]


# ── Cache lifecycle ──────────────────────────────────────────────────


def test_vector_cache_reuses_unchanged_documents(tmp_path):
    """Unchanged documents are embedded once across queries (the first
    query pays the backfill; later queries are cache-only)."""
    store = _make_store(tmp_path)
    _seed_corpus(store)
    embedder = _TopicEmbedder()
    store.set_embedder(embedder)

    store.vector_search("cat", top_k=3)
    backfill_embeds = len(embedder.embedded_texts)
    assert backfill_embeds >= 3  # corpus backfill happened

    embedder.embedded_texts.clear()
    store.vector_search("dog", top_k=3)
    assert embedder.embedded_texts == ["dog"]  # query only


def test_vector_cache_reembeds_changed_documents(tmp_path):
    """Editing a document invalidates its cache entry by content hash."""
    store = _make_store(tmp_path)
    store.add(_make_func("doc", "The canine barks"), _make_source())
    embedder = _TopicEmbedder()
    store.set_embedder(embedder)
    store.vector_search("dog", top_k=1)

    embedder.embedded_texts.clear()
    store.add(_make_func("doc", "The feline sleeps now"), _make_source())
    store.vector_search("dog", top_k=1)

    assert any("feline sleeps" in t for t in embedder.embedded_texts)


def test_replacing_embedder_drops_cached_vectors(tmp_path):
    """set_embedder is also the model-swap path: stale vectors from the
    previous model must never survive into the new one's rankings."""
    store = _make_store(tmp_path)
    _seed_corpus(store)
    first = _TopicEmbedder()
    store.set_embedder(first)
    store.vector_search("cat", top_k=3)

    second = _TopicEmbedder()
    store.set_embedder(second)
    hits = store.vector_search("cat", top_k=3)

    assert hits[0].func_id == "feline"
    # The second embedder re-embedded the corpus from scratch.
    assert len(second.embedded_texts) >= 3
