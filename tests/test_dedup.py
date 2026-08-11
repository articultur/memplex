"""Direct tests for memplex/retrieval/dedup.py (was 15% coverage).

MemoryDeduplicator is fully decoupled from storage; it takes a stub
embedder (anything with embed_batch). Covers exact dedup, semantic dedup
(numpy + pure-python fallback), the static helpers, and strategy routing.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.models import DedupResult, FieldValue, Function  # noqa: E402
from memplex.retrieval.dedup import DedupStrategy, MemoryDeduplicator  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────


def _mem(mid, name="m", desc="content", domain="x"):
    return Function(
        id=mid,
        name=name,
        name_normalized=name.lower(),
        domain=domain,
        trigger=[FieldValue(desc=desc, sources=["t"], source_method="manual", weight=1.0)],
    )


class _StubEmbedder:
    """Returns a fixed vector per distinct text so duplicates collide."""

    def __init__(self):
        self._table = {}

    def embed_batch(self, texts):
        out = []
        for t in texts:
            if t not in self._table:
                # New text -> a fresh orthogonal-ish vector.
                idx = len(self._table)
                v = [0.0] * 16
                v[idx % 16] = 1.0
                self._table[t] = v
            out.append(self._table[t])
        return out


# ── Exact dedup ──────────────────────────────────────────────────────


def test_exact_dedup_removes_identical_content():
    d = MemoryDeduplicator(_StubEmbedder(), strategy=DedupStrategy.EXACT)
    a = _mem("a", desc="same text")
    b = _mem("b", desc="same text")  # identical content hash
    result = d.deduplicate([a, b])
    assert isinstance(result, DedupResult)
    assert result.original_count == 2
    assert result.exact_removed == 1
    assert len(result.deduplicated) == 1


def test_exact_dedup_keeps_distinct_content():
    d = MemoryDeduplicator(_StubEmbedder(), strategy=DedupStrategy.EXACT)
    result = d.deduplicate([_mem("a", desc="alpha"), _mem("b", desc="beta")])
    assert result.exact_removed == 0
    assert len(result.deduplicated) == 2


def test_empty_input_returns_empty_result():
    d = MemoryDeduplicator(_StubEmbedder())
    result = d.deduplicate([])
    assert result.original_count == 0
    assert result.deduplicated == []


# ── Semantic dedup (numpy path runs by default when numpy present) ───


def test_semantic_dedup_removes_near_duplicates():
    """With a stub embedder returning identical vectors for identical text,
    semantic dedup collapses them."""
    d = MemoryDeduplicator(_StubEmbedder(), strategy=DedupStrategy.SEMANTIC, threshold=0.95)
    # Same text -> same embedding -> cosine 1.0 >= threshold -> deduped.
    a = _mem("a", desc="near dup text")
    b = _mem("b", desc="near dup text")
    result = d.deduplicate([a, b])
    assert result.semantic_removed >= 1
    assert len(result.deduplicated) == 1


def test_semantic_dedup_keeps_orthogonal_embeddings():
    d = MemoryDeduplicator(_StubEmbedder(), strategy=DedupStrategy.SEMANTIC, threshold=0.95)
    # Distinct texts -> distinct orthogonal vectors -> cosine 0 < threshold.
    result = d.deduplicate([_mem("a", desc="alpha"), _mem("b", desc="beta")])
    assert len(result.deduplicated) == 2


# ── Both strategy ────────────────────────────────────────────────────


def test_both_strategy_runs_exact_then_semantic():
    d = MemoryDeduplicator(_StubEmbedder(), strategy=DedupStrategy.BOTH)
    a = _mem("a", desc="same")
    b = _mem("b", desc="same")
    result = d.deduplicate([a, b, _mem("c", desc="unique")])
    # exact removes one of a/b; c survives both stages.
    assert len(result.deduplicated) == 2
    assert result.original_count == 3


# ── Pure-python fallback path (force by calling internal directly) ───


def test_semantic_dedup_fallback_pure_python():
    """Directly exercise the no-numpy fallback with hand-built embeddings."""
    d = MemoryDeduplicator(_StubEmbedder())
    mems = [_mem("a", desc="x"), _mem("b", desc="x")]
    # Identical embeddings -> deduped.
    embeddings = [[1.0, 0.0], [1.0, 0.0]]
    kept = d._semantic_dedup_fallback(mems, embeddings)
    assert len(kept) == 1


def test_semantic_dedup_fallback_keeps_orthogonal():
    d = MemoryDeduplicator(_StubEmbedder())
    mems = [_mem("a", desc="x"), _mem("b", desc="y")]
    embeddings = [[1.0, 0.0], [0.0, 1.0]]
    kept = d._semantic_dedup_fallback(mems, embeddings)
    assert len(kept) == 2


# ── Static helpers ───────────────────────────────────────────────────


def test_choose_better_prefers_more_recent():
    from datetime import datetime, timedelta

    now = datetime.now()
    older = _mem("a", desc="x")
    older.updated_at = now - timedelta(days=5)
    newer = _mem("b", desc="x")
    newer.updated_at = now
    # _choose_better returns the preferred one.
    chosen = MemoryDeduplicator._choose_better(older, newer)
    assert chosen is newer


def test_cosine_sim_static():
    assert MemoryDeduplicator._cosine_sim([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert MemoryDeduplicator._cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_content_hash_is_stable_for_same_memory():
    d = MemoryDeduplicator(_StubEmbedder())  # _content_hash is an instance method
    m = _mem("a", desc="hash me")
    assert d._content_hash(m) == d._content_hash(m)


def test_memory_to_text_joins_fields():
    m = _mem("a", name="Login", desc="authenticate user")
    text = MemoryDeduplicator._memory_to_text(m)
    assert "Login" in text
    assert "authenticate user" in text


def test_merge_memories_combines_fields():
    a = _mem("a", desc="alpha")
    b = _mem("b", desc="beta")
    merged = MemoryDeduplicator._merge_memories([a, b])
    descs = {fv.desc for fv in merged.trigger}
    assert {"alpha", "beta"} <= descs


# ── FAISS opt-out (compaction.dedup_use_faiss wiring) ────────────────


def test_use_faiss_true_uses_faiss_backend_when_available(monkeypatch):
    import sys
    import types

    calls = []
    monkeypatch.setitem(sys.modules, "faiss", types.ModuleType("faiss"))
    monkeypatch.setattr(
        MemoryDeduplicator,
        "_semantic_dedup_faiss",
        lambda self, memories: calls.append(len(memories)) or list(memories),
    )
    d = MemoryDeduplicator(_StubEmbedder(), use_faiss=True)
    d._semantic_dedup([_mem("a"), _mem("b")])
    assert calls == [2]


def test_use_faiss_false_skips_faiss_backend(monkeypatch):
    import sys
    import types

    calls = []
    monkeypatch.setitem(sys.modules, "faiss", types.ModuleType("faiss"))
    monkeypatch.setattr(
        MemoryDeduplicator,
        "_semantic_dedup_faiss",
        lambda self, memories: calls.append(len(memories)) or list(memories),
    )
    d = MemoryDeduplicator(_StubEmbedder(), use_faiss=False)
    result = d._semantic_dedup([_mem("a", desc="alpha"), _mem("b", desc="beta")])
    assert calls == []
    assert len(result) == 2
