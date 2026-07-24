"""Intent classification -- memory type and query scope heuristics.

Pure, dependency-free heuristics used by :class:`MemplexService`:

- :func:`detect_memory_type` classifies raw text into one of the four
  memory types (``function`` | ``fact`` | ``preference`` | ``observation``).
- :func:`detect_scope_by_keywords` maps a query string to a
  :class:`QueryScope` via multi-label keyword scoring (the keyword
  fallback half of ``MemplexService._detect_scope``; the LLM half stays
  on the service because it needs the LLM enhancer and event-loop
  juggling).

Both functions are extracted as module-level pure functions so they can
be unit-tested directly without instantiating a service.

Usage::

    from memplex.intent import detect_memory_type, detect_scope_by_keywords
    from memplex.models import QueryScope

    mt = detect_memory_type("用户偏好暗色主题")        # -> "preference"
    scope = detect_scope_by_keywords("整体架构设计")   # -> QueryScope.SYNTHESIS
"""

from __future__ import annotations

import logging

from memplex.models import QueryScope

logger = logging.getLogger(__name__)


# ── Memory type classification ────────────────────────────────────────


def detect_memory_type(text: str) -> str:
    """Heuristic: classify text into a memory type.

    Returns one of ``"function"`` | ``"fact"`` | ``"preference"`` |
    ``"observation"``.
    """
    text_lower = text.lower()

    # Observation patterns
    obs_keywords = [
        "observe",
        "observed",
        "noticed",
        "happened",
        "occurred",
        "事件",
        "观察",
        "发生",
        "记录",
    ]
    if any(k in text_lower for k in obs_keywords):
        return "observation"

    # Preference patterns
    pref_keywords = [
        "prefer",
        "like",
        "dislike",
        "want",
        "always",
        "never",
        "喜欢",
        "偏好",
        "讨厌",
        "倾向",
        "总是",
        "从不",
    ]
    if any(k in text_lower for k in pref_keywords):
        return "preference"

    # Fact patterns
    fact_keywords = [
        "is",
        "are",
        "means",
        "defined as",
        "refers to",
        "是",
        "意味着",
        "定义为",
        "指的是",
        "事实",
    ]
    if any(k in text_lower for k in fact_keywords):
        return "fact"

    # Default: function (procedural / action-oriented)
    return "function"


# ── Query scope classification (keyword fallback) ─────────────────────


# Negation / stop prefixes stripped before keyword scoring so that
# "不要搜索" does not falsely trigger the "search" immediate keyword.
NEGATION_PREFIXES = [
    "不",
    "没有",
    "没",
    "非",
    "不是",
    "un",
    "not",
    "no ",
    "non-",
]

# Multi-label keyword table. Highest score wins; ties resolve to ALL
# (multi-path merge). Exposed as a module constant for testability.
SCOPE_KEYWORDS: dict[QueryScope, list[str]] = {
    QueryScope.RELATION: [
        "影响",
        "依赖",
        "调用",
        "关系",
        "哪些",
        "affect",
        "depend",
        "call",
        "relation",
        "impact",
    ],
    QueryScope.SYNTHESIS: [
        "设计",
        "架构",
        "概述",
        "整体",
        "概念",
        "原理",
        "design",
        "architecture",
        "overview",
        "concept",
        "how does",
    ],
    QueryScope.IMMEDIATE: [
        "在哪",
        "定义",
        "是什么",
        "查找",
        "搜索",
        "where",
        "define",
        "what is",
        "find",
        "search",
    ],
}


def detect_scope_by_keywords(text: str) -> QueryScope:
    """Map a query string to a :class:`QueryScope` via keyword scoring.

    Multi-label scoring: each scope accumulates one point per matched
    keyword. The highest-scoring scope wins. Ties resolve to
    :attr:`QueryScope.ALL` (multi-path merge). When no keyword matches,
    falls back to :attr:`QueryScope.IMMEDIATE`.

    This is the keyword fallback half of
    :meth:`MemplexService._detect_scope`; the LLM-driven half remains on
    the service.
    """
    text_lower = text.lower()
    cleaned = text_lower
    for neg in NEGATION_PREFIXES:
        cleaned = cleaned.replace(neg, " ")

    scores = {
        scope: sum(1 for k in kw if k in cleaned)
        for scope, kw in SCOPE_KEYWORDS.items()
    }
    max_score = max(scores.values())
    if max_score == 0:
        return QueryScope.IMMEDIATE
    top_scopes = [s for s, v in scores.items() if v == max_score]
    return QueryScope.ALL if len(top_scopes) > 1 else top_scopes[0]
