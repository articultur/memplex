"""build_query_explanation -- product-facing retrieval trace translator.

Converts the internal query ``trace`` dict assembled by
:meth:`MemplexService.query` into the stable, documented product schema
returned under ``QueryResult.explanation`` (surfaced by ``recall --explain``,
``corpus recall``, and the ``memory_*`` MCP tools).

This is a pure dict-in / dict-out transform with no dependency on the
service or storage layer, so it is unit-tested directly.

Usage::

    from memplex.query_explainer import build_query_explanation

    explanation = build_query_explanation(trace)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Public entry point ───────────────────────────────────────────────


def build_query_explanation(
    trace: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Convert an internal query trace into a stable product schema.

    Parameters
    ----------
    trace:
        The trace dict built inside :meth:`MemplexService.query` when
        ``explain=True``. May be ``None`` (when explanation was not
        requested), in which case ``None`` is returned.

    Returns
    -------
    dict or None
        A schema-versioned explanation with the keys ``schema_version``,
        ``query``, ``scope``, ``budget``, ``embedding``, ``retrieval``,
        ``filters``, ``ranking``, ``selection``, ``boundaries``, and
        ``results``. Returns ``None`` when *trace* is ``None``.

    The function never mutates *trace*; each output key is built fresh.
    """
    if trace is None:
        return None

    paths: List[Dict[str, Any]] = []
    filters: List[Dict[str, Any]] = []
    ranking: Dict[str, Any] = {}
    retrieval: Dict[str, Any] = {"paths": paths}
    selection: Dict[str, Any] = {}
    budget: Dict[str, Any] = {
        "top_k": trace.get("top_k"),
        "max_tokens": trace.get("max_tokens"),
        "tokens_used": 0,
        "truncated": False,
    }

    for stage in trace.get("stages", []):
        name = stage.get("stage", "")
        if name.endswith("_search"):
            path = {
                "name": name[: -len("_search")],
                "status": stage.get("status"),
                "candidates": stage.get("candidates", 0),
            }
            if stage.get("candidate_budget") is not None:
                path["candidate_budget"] = stage["candidate_budget"]
            paths.append(path)
        elif name == "merge_deduplicate":
            retrieval["merged_candidates"] = stage.get("candidates", 0)
            if stage.get("candidate_budget") is not None:
                retrieval["candidate_budget"] = stage["candidate_budget"]
        elif name == "namespace_filter":
            filters.append(
                {
                    "type": "namespace",
                    "before": stage.get("before", 0),
                    "after": stage.get("after", 0),
                    "boundary": stage.get("boundary"),
                }
            )
        elif name == "owner_filter":
            filters.append(
                {
                    "type": "owner",
                    "owner": stage.get("owner"),
                    "before": stage.get("before", 0),
                    "after": stage.get("after", 0),
                    "boundary": stage.get("boundary"),
                }
            )
        elif name == "injection_filter":
            filters.append(
                {
                    "type": "injection",
                    "before": stage.get("before", 0),
                    "after": stage.get("after", 0),
                    "boundary": stage.get("boundary"),
                }
            )
        elif name == "rerank":
            ranking["semantic_rerank"] = {
                "before": stage.get("before", 0),
                "after": stage.get("after", 0),
                "weights": stage.get("weights", {}),
            }
        elif name == "cross_encoder_rerank":
            ranking["cross_encoder"] = {
                "before": stage.get("before", 0),
                "after": stage.get("after", 0),
                "model": stage.get("model"),
            }
        elif name == "top_k":
            selection["top_k_limit"] = stage.get("limit")
            selection["after_top_k"] = stage.get("after", 0)
        elif name == "token_budget":
            budget["tokens_used"] = stage.get("tokens_used", 0)
            budget["truncated"] = stage.get("truncated", False)
            # The stage records the effective budget cap; prefer it over the
            # top-level trace value when present.
            if stage.get("max_tokens") is not None:
                budget["max_tokens"] = stage["max_tokens"]
            selection["after_token_budget"] = stage.get("after", 0)

    return {
        "schema_version": 1,
        "query": trace.get("query"),
        "scope": trace.get("scope"),
        "budget": budget,
        "embedding": trace.get("embedding", {}),
        "retrieval": retrieval,
        "filters": filters,
        "ranking": ranking,
        "selection": selection,
        "boundaries": {
            "scope": "Visibility metadata only; not an ACL engine.",
            "remote_embedding": "Remote embeddings are opt-in and not required.",
        },
        "results": trace.get("final_results", []),
    }
