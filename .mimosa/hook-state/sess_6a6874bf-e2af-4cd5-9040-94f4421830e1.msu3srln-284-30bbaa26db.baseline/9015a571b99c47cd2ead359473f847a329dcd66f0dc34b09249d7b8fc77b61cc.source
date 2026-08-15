"""Direct tests for memplex/storage/vector.py (was 34% coverage).

InMemoryVectorStore is fully standalone (TF-IDF, no chromadb). Covers
add/upsert/search/delete/clear, CJK tokenization, empty-vocab edge case,
cosine, and the create_vector_store factory routing (including the
chroma-absent fallback).
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.storage.vector import (  # noqa: E402
    InMemoryVectorStore,
    VectorSearchResult,
    VectorStore,
    create_vector_store,
)

# ── InMemoryVectorStore add + search ─────────────────────────────────


def test_add_then_search_finds_by_keyword():
    store = InMemoryVectorStore()
    store.add("a", "login authentication handler")
    store.add("b", "database connection pool")
    results = store.search("login", top_k=5)
    ids = {r.id for r in results}
    assert "a" in ids


def test_search_empty_store_returns_empty():
    store = InMemoryVectorStore()
    assert store.search("anything", top_k=5) == []


def test_search_top_k_limits_results():
    store = InMemoryVectorStore()
    for i in range(10):
        store.add(f"id{i}", "shared keyword content")
    results = store.search("shared keyword", top_k=3)
    assert len(results) <= 3


def test_search_returns_vector_search_result_type():
    store = InMemoryVectorStore()
    store.add("a", "memory text")
    results = store.search("memory", top_k=1)
    assert results
    assert isinstance(results[0], VectorSearchResult)
    assert hasattr(results[0], "score")


# ── upsert path (caller-supplied vector) ─────────────────────────────


def test_upsert_then_search_by_vector():
    store = InMemoryVectorStore()
    store.upsert("a", [1.0, 0.0, 0.0], text="vec-memory")
    results = store.search("ignored-text", top_k=5, query_vector=[1.0, 0.0, 0.0])
    ids = {r.id for r in results}
    assert "a" in ids


def test_upsert_batch_adds_multiple():
    store = InMemoryVectorStore()
    store.upsert_batch({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    results = store.search("x", top_k=5, query_vector=[1.0, 0.0])
    assert "a" in {r.id for r in results}


def test_upsert_without_add_has_zero_text_embedding():
    """After upsert only, TF-IDF embedding is [0] placeholder; text search
    returns nothing useful, but vector search still works."""
    store = InMemoryVectorStore()
    store.upsert("a", [1.0, 0.0])
    # text search against the [0] placeholder yields no real hit
    text_results = store.search("anything", top_k=5)
    assert all(r.score == pytest.approx(0.0) for r in text_results)


# ── delete / clear ───────────────────────────────────────────────────


def test_delete_removes_entry():
    store = InMemoryVectorStore()
    store.add("a", "keep me")
    store.delete("a")
    assert store.search("keep", top_k=5) == []


def test_delete_missing_id_is_noop():
    store = InMemoryVectorStore()
    store.delete("never-added")  # must not raise


def test_clear_empties_store():
    store = InMemoryVectorStore()
    store.add("a", "x")
    store.add("b", "y")
    store.clear()
    assert store.search("x", top_k=5) == []


# ── CJK tokenization branch ──────────────────────────────────────────


def test_get_words_splits_cjk_into_chars():
    words = InMemoryVectorStore._get_words("你好世界")
    assert words == {"你", "好", "世", "界"}


def test_get_words_whitespace_splits_ascii():
    words = InMemoryVectorStore._get_words("hello world")
    assert words == {"hello", "world"}


def test_get_words_mixed_cjk_and_ascii_uses_char_split():
    """When any CJK char is present, the whole string is char-split
    (documented simplification: ASCII words are NOT preserved as tokens
    in mixed text). Pin this so a future change is intentional."""
    words = InMemoryVectorStore._get_words("login 登录")
    assert "登" in words and "录" in words
    assert "l" in words and "o" in words


# ── cosine static ────────────────────────────────────────────────────


def test_cosine_static():
    assert InMemoryVectorStore._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert InMemoryVectorStore._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # zero vector -> 0 (epsilon guard, no division by zero)
    assert InMemoryVectorStore._cosine([0.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)


# ── empty-vocab encoding edge case ───────────────────────────────────


def test_encode_with_empty_vocab_returns_zero_vector():
    store = InMemoryVectorStore()  # no adds -> empty _all_words vocab
    vec = store._encode_with_vocab("anything", {"x"})
    assert vec == [0]


def test_encode_with_vocab_against_known_vocab():
    store = InMemoryVectorStore()
    store._all_words = {"alpha", "beta", "gamma"}
    vec = store._encode_with_vocab("ignore", {"alpha": True})
    # sorted vocab -> [alpha=1, beta=0, gamma=0]
    assert vec == [1, 0, 0]


# ── create_vector_store factory ──────────────────────────────────────


def test_create_vector_store_inmemory_explicit():
    s = create_vector_store("inmemory")
    assert isinstance(s, InMemoryVectorStore)


def test_create_vector_store_auto_falls_back_to_inmemory_when_chroma_absent():
    """chromadb is not installed in this env, so 'auto' -> InMemory."""
    s = create_vector_store("auto")
    assert isinstance(s, InMemoryVectorStore)


def test_create_vector_store_unknown_backend_raises():
    with pytest.raises(ValueError):
        create_vector_store("not-a-backend")


def test_create_vector_store_chroma_unavailable_raises():
    """Requesting chroma when it is absent must raise a clear ImportError
    naming the missing dependency (not silently fall back, and not a
    misleading 'unknown backend' ValueError)."""
    with pytest.raises(ImportError, match="chromadb"):
        create_vector_store("chroma")


# ── VectorStore protocol conformance ─────────────────────────────────


def test_inmemory_vector_store_satisfies_protocol():
    """InMemoryVectorStore implements the VectorStore runtime-checkable Protocol."""
    assert isinstance(InMemoryVectorStore(), VectorStore)
