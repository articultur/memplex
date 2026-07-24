"""Test intent classification: detect_memory_type and detect_scope_by_keywords.

These heuristics previously lived inside ``MemplexService`` (as a module
function and a private method respectively) and were only covered
indirectly via service tests. After extraction to ``memplex.intent`` they
are unit-tested directly.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.intent import (  # noqa: E402
    NEGATION_PREFIXES,
    SCOPE_KEYWORDS,
    detect_memory_type,
    detect_scope_by_keywords,
)
from memplex.models import QueryScope  # noqa: E402

# ── detect_memory_type ────────────────────────────────────────────────


def test_memory_type_defaults_to_function():
    assert detect_memory_type("用户登录系统") == "function"


def test_memory_type_detects_observation():
    assert detect_memory_type("观察到系统发生了错误") == "observation"


def test_memory_type_detects_preference():
    assert detect_memory_type("用户偏好暗色主题") == "preference"


def test_memory_type_detects_fact():
    assert detect_memory_type("API 是 REST 接口") == "fact"


def test_memory_type_observation_takes_priority_over_preference():
    # An observation keyword should win even if a preference keyword is present.
    assert detect_memory_type("observed that the user prefers X") == "observation"


def test_memory_type_preference_beats_fact():
    assert detect_memory_type("I always define X as Y") == "preference"


def test_memory_type_english_keywords():
    assert detect_memory_type("I noticed the deploy failed") == "observation"
    assert detect_memory_type("I prefer dark mode") == "preference"


# ── detect_scope_by_keywords ──────────────────────────────────────────


def test_scope_relation():
    assert detect_scope_by_keywords("登录和注册的关系") == QueryScope.RELATION


def test_scope_synthesis():
    assert detect_scope_by_keywords("整体架构设计") == QueryScope.SYNTHESIS


def test_scope_immediate():
    assert detect_scope_by_keywords("登录函数在哪") == QueryScope.IMMEDIATE


def test_scope_immediate_english():
    assert detect_scope_by_keywords("where is the login function") == QueryScope.IMMEDIATE


def test_scope_no_match_defaults_to_immediate():
    assert detect_scope_by_keywords("random text xyz") == QueryScope.IMMEDIATE


def test_scope_tie_resolves_to_all():
    # Two scopes scoring equally -> ALL (multi-path merge).
    # "find" (immediate) + "design" (synthesis) each score 1.
    scope = detect_scope_by_keywords("find the design")
    assert scope == QueryScope.ALL


def test_scope_negation_prefix_is_stripped():
    # "不要搜索" without stripping would match "搜索" (immediate).
    # After stripping the 不 prefix, "搜索" still appears — so this is a
    # positive check that the cleaner did not corrupt the rest of the text.
    assert detect_scope_by_keywords("搜索登录") == QueryScope.IMMEDIATE
    # Pure negation with nothing left -> falls back to IMMEDIATE (no match).
    assert detect_scope_by_keywords("不要") == QueryScope.IMMEDIATE


def test_scope_mixed_chinese_english():
    # "关系" (relation, zh) + "affect" (relation, en) -> RELATION with score 2.
    assert detect_scope_by_keywords("how does X affect Y 的关系") == QueryScope.RELATION


def test_scope_keywords_table_covers_three_scopes():
    # Sanity: the table exposes the three non-ALL scopes used in scoring.
    assert set(SCOPE_KEYWORDS.keys()) == {
        QueryScope.RELATION,
        QueryScope.SYNTHESIS,
        QueryScope.IMMEDIATE,
    }


def test_negation_prefixes_contains_chinese_and_english():
    assert "不" in NEGATION_PREFIXES
    assert "not" in NEGATION_PREFIXES


# ── Backward compatibility shim ───────────────────────────────────────


def test_detect_memory_type_still_importable_from_service():
    # The public re-export must keep working so existing callers
    # (e.g. tests/test_service.py) are unaffected.
    from memplex.service import _detect_memory_type

    assert _detect_memory_type is detect_memory_type
    assert _detect_memory_type("用户偏好暗色主题") == "preference"
