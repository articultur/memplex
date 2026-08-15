"""Test build_query_explanation: pure dict-to-dict trace translation.

This module was previously a private staticmethod on ``MemplexService``
with zero direct test coverage. After extraction to
``memplex.query_explainer`` it is unit-tested across every branch.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.query_explainer import build_query_explanation  # noqa: E402

# ── Base shape ────────────────────────────────────────────────────────


def test_none_trace_returns_none():
    assert build_query_explanation(None) is None


def test_empty_trace_still_emits_full_schema():
    """A trace with no stages still returns the documented schema keys."""
    out = build_query_explanation({"query": "q", "top_k": 5, "max_tokens": 100})
    assert out["schema_version"] == 1
    assert out["query"] == "q"
    # Fixed boundaries copy must always be present (product-facing contract).
    assert out["boundaries"]["scope"] == "Visibility metadata only; not an ACL engine."
    assert out["boundaries"]["remote_embedding"] == "Remote embeddings are opt-in and not required."
    # Empty containers initialised, never missing.
    assert out["retrieval"] == {"paths": []}
    assert out["filters"] == []
    assert out["ranking"] == {}
    assert out["selection"] == {}
    assert out["results"] == []
    # Budget defaults before token_budget stage runs.
    assert out["budget"] == {
        "top_k": 5,
        "max_tokens": 100,
        "tokens_used": 0,
        "truncated": False,
    }


def test_does_not_mutate_input_trace():
    """The transform must be pure: caller's trace stays intact."""
    trace = {
        "query": "q",
        "top_k": 3,
        "max_tokens": 50,
        "stages": [
            {"stage": "rag_search", "status": "ok", "candidates": 2},
        ],
        "embedding": {"enabled": True},
        "final_results": [{"id": "x"}],
    }
    snapshot = {
        "query": "q",
        "top_k": 3,
        "max_tokens": 50,
        "stages": [{"stage": "rag_search", "status": "ok", "candidates": 2}],
        "embedding": {"enabled": True},
        "final_results": [{"id": "x"}],
    }
    out = build_query_explanation(trace)
    # Mutating the output must not bleed back into the input.
    out["retrieval"]["paths"].append("tampered")
    out["filters"].append("tampered")
    assert trace == snapshot, "input trace was mutated"


# ── Per-stage branches ────────────────────────────────────────────────


def test_search_stages_become_named_paths():
    trace = {
        "stages": [
            {"stage": "rag_search", "status": "ok", "candidates": 3},
            {"stage": "wiki_search", "status": "ok", "candidates": 1},
            {"stage": "graph_search", "status": "failed", "candidates": 0},
        ],
    }
    paths = build_query_explanation(trace)["retrieval"]["paths"]
    assert paths == [
        {"name": "rag", "status": "ok", "candidates": 3},
        {"name": "wiki", "status": "ok", "candidates": 1},
        {"name": "graph", "status": "failed", "candidates": 0},
    ]


def test_merge_deduplicate_stage_sets_merged_candidates():
    trace = {"stages": [{"stage": "merge_deduplicate", "candidates": 7}]}
    out = build_query_explanation(trace)
    assert out["retrieval"]["merged_candidates"] == 7


def test_namespace_filter_stage_appends_filter():
    trace = {
        "stages": [
            {
                "stage": "namespace_filter",
                "before": 5,
                "after": 2,
                "boundary": "exact-match metadata filter; not an ACL engine",
            },
        ],
    }
    out = build_query_explanation(trace)
    assert out["filters"] == [
        {
            "type": "namespace",
            "before": 5,
            "after": 2,
            "boundary": "exact-match metadata filter; not an ACL engine",
        }
    ]


def test_rerank_stage_populates_semantic_rerank():
    trace = {
        "stages": [
            {
                "stage": "rerank",
                "before": 10,
                "after": 6,
                "weights": {"raw_relevance": 0.25, "semantic_similarity": 0.30},
            },
        ],
    }
    out = build_query_explanation(trace)
    assert out["ranking"]["semantic_rerank"] == {
        "before": 10,
        "after": 6,
        "weights": {"raw_relevance": 0.25, "semantic_similarity": 0.30},
    }


def test_cross_encoder_rerank_stage_populates_cross_encoder():
    trace = {
        "stages": [
            {
                "stage": "cross_encoder_rerank",
                "before": 6,
                "after": 4,
                "model": "BAAI/bge-reranker-v2-m3",
            },
        ],
    }
    out = build_query_explanation(trace)
    assert out["ranking"]["cross_encoder"] == {
        "before": 6,
        "after": 4,
        "model": "BAAI/bge-reranker-v2-m3",
    }


def test_top_k_stage_sets_selection_limits():
    trace = {"stages": [{"stage": "top_k", "limit": 10, "after": 8}]}
    out = build_query_explanation(trace)
    assert out["selection"]["top_k_limit"] == 10
    assert out["selection"]["after_top_k"] == 8


def test_token_budget_stage_updates_budget_and_selection():
    trace = {
        "stages": [
            {"stage": "token_budget", "tokens_used": 1200, "truncated": True, "after": 5},
        ],
    }
    out = build_query_explanation(trace)
    assert out["budget"]["tokens_used"] == 1200
    assert out["budget"]["truncated"] is True
    assert out["selection"]["after_token_budget"] == 5


def test_token_budget_stage_max_tokens_overrides_top_level():
    """The stage records the effective budget cap; it must win over the
    top-level trace value when present."""
    trace = {
        "max_tokens": 4000,
        "stages": [
            {
                "stage": "token_budget",
                "max_tokens": 1500,
                "tokens_used": 900,
                "truncated": True,
                "after": 3,
            },
        ],
    }
    out = build_query_explanation(trace)
    assert out["budget"]["max_tokens"] == 1500
    assert out["budget"]["tokens_used"] == 900


def test_token_budget_stage_without_max_tokens_keeps_top_level():
    trace = {
        "max_tokens": 4000,
        "stages": [
            {"stage": "token_budget", "tokens_used": 100, "truncated": False, "after": 1},
        ],
    }
    out = build_query_explanation(trace)
    assert out["budget"]["max_tokens"] == 4000


def test_unknown_stage_name_is_silently_ignored():
    """Forward-compat: unknown stages must not crash the translator."""
    trace = {"stages": [{"stage": "future_stage", "any": "thing"}]}
    out = build_query_explanation(trace)
    assert out["retrieval"]["paths"] == []
    assert out["filters"] == []
    assert out["ranking"] == {}


# ── Pass-through keys ─────────────────────────────────────────────────


def test_embedding_and_final_results_pass_through():
    embedding = {"enabled": True, "model": "default", "query_vector_available": True}
    final = [{"id": "f1", "name": "Login", "score": 0.9}]
    trace = {
        "query": "login",
        "scope": "immediate",
        "embedding": embedding,
        "final_results": final,
        "stages": [],
    }
    out = build_query_explanation(trace)
    assert out["embedding"] == embedding
    assert out["results"] == final
    assert out["scope"] == "immediate"


def test_missing_embedding_defaults_to_empty_dict():
    trace = {"stages": []}
    assert build_query_explanation(trace)["embedding"] == {}


def test_missing_final_results_defaults_to_empty_list():
    trace = {"stages": []}
    assert build_query_explanation(trace)["results"] == []


# ── Integration-style: a full realistic trace ─────────────────────────


def test_full_realistic_trace_assembles_correctly():
    """End-to-end shape check mirroring what MemplexService.query builds."""
    trace = {
        "query": "登录函数在哪",
        "top_k": 10,
        "max_tokens": 4000,
        "scope": "immediate",
        "namespace_filter": {"memplex_agent": "codex"},
        "embedding": {
            "enabled": True,
            "model": "default",
            "hyde_enabled": True,
            "query_vector_available": True,
        },
        "reranker": {"enabled": True, "cross_encoder_enabled": False},
        "stages": [
            {"stage": "rag_search", "status": "ok", "candidates": 8},
            {"stage": "graph_search", "status": "ok", "candidates": 3},
            {"stage": "merge_deduplicate", "candidates": 9},
            {
                "stage": "namespace_filter",
                "before": 9,
                "after": 9,
                "boundary": "exact-match metadata filter; not an ACL engine",
            },
            {
                "stage": "rerank",
                "before": 9,
                "after": 20,
                "weights": {"raw_relevance": 0.25, "semantic_similarity": 0.30},
            },
            {"stage": "top_k", "limit": 10, "after": 10},
            {
                "stage": "token_budget",
                "tokens_used": 3500,
                "truncated": False,
                "after": 9,
            },
        ],
        "final_results": [
            {"id": "func_1", "name": "login", "score": 0.95, "token_estimate": 400},
        ],
    }
    out = build_query_explanation(trace)

    assert out["schema_version"] == 1
    assert out["query"] == "登录函数在哪"
    assert out["scope"] == "immediate"

    # retrieval
    assert len(out["retrieval"]["paths"]) == 2
    assert out["retrieval"]["merged_candidates"] == 9

    # filters
    assert len(out["filters"]) == 1
    assert out["filters"][0]["type"] == "namespace"

    # ranking
    assert out["ranking"]["semantic_rerank"]["after"] == 20

    # selection
    assert out["selection"]["top_k_limit"] == 10
    assert out["selection"]["after_top_k"] == 10
    assert out["selection"]["after_token_budget"] == 9

    # budget
    assert out["budget"]["tokens_used"] == 3500
    assert out["budget"]["truncated"] is False

    # results passthrough
    assert out["results"][0]["name"] == "login"
