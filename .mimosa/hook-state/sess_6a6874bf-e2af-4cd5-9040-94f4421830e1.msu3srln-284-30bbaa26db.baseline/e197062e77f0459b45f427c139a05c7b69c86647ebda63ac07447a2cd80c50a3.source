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
    classify_observation,
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


# ── fact keyword word-boundary (regression) ─────────────────────────


def test_fact_keyword_is_not_matched_inside_words():
    # Regression: substring matching classified any text containing
    # "this"/"history"/"list" as fact because of the embedded "is".
    assert detect_memory_type("check this history list") == "function"
    assert detect_memory_type("review the historic listings") == "function"


def test_fact_keyword_is_matched_as_word():
    assert detect_memory_type("The sky is blue") == "fact"
    assert detect_memory_type("they are deployed") == "fact"


def test_fact_keyword_chinese_unaffected():
    # CJK keeps substring matching (no word boundaries in Chinese).
    assert detect_memory_type("查询历史数据") == "function"
    assert detect_memory_type("这个事实很重要") == "fact"
    assert detect_memory_type("同步意味着一致性") == "fact"


# ── classify_observation ──────────────────────────────────────────────


def test_classify_observation_bugfix_english():
    assert classify_observation("fixed the login error") == "bugfix"
    assert classify_observation("an exception occurred in the parser") == "bugfix"
    assert classify_observation("patched the bug in retry logic") == "bugfix"


def test_classify_observation_bugfix_chinese():
    assert classify_observation("修复了登录接口的报错") == "bugfix"
    assert classify_observation("定位到空指针异常") == "bugfix"


def test_classify_observation_decision_english():
    assert classify_observation("decided to adopt Postgres for storage") == "decision"
    assert classify_observation("we chose option B") == "decision"


def test_classify_observation_decision_chinese():
    assert classify_observation("决定采用 SQLite 作为缓存层") == "decision"
    assert classify_observation("完成数据库选型") == "decision"


def test_classify_observation_change_english():
    assert classify_observation("created the migration script") == "change"
    assert classify_observation("deleted the deprecated endpoint") == "change"


def test_classify_observation_change_chinese():
    assert classify_observation("修改了配置文件的超时时间") == "change"
    assert classify_observation("新增了用户表索引") == "change"
    assert classify_observation("删除了冗余日志") == "change"


def test_classify_observation_discovery_english():
    assert classify_observation("found the root cause in the cache layer") == "discovery"
    assert classify_observation("discovered an undocumented flag") == "discovery"


def test_classify_observation_discovery_chinese():
    assert classify_observation("发现缓存层有一个隐藏开关") == "discovery"
    assert classify_observation("定位到问题出在重试逻辑") == "discovery"


def test_classify_observation_fallback_note():
    assert classify_observation("deployed v2 to production") == "note"
    assert classify_observation("随便记录一条运行状态") == "note"


def test_classify_observation_tool_name_write_is_change():
    assert classify_observation("anything at all", tool_name="Write") == "change"
    assert classify_observation("deployed v2", tool_name="Edit") == "change"
    assert classify_observation("refactored module", tool_name="MultiEdit") == "change"


def test_classify_observation_tool_name_bash_error_is_bugfix():
    assert classify_observation("pytest run failed with error code 1", tool_name="Bash") == "bugfix"
    # Bash without error keywords falls through to text classification.
    assert classify_observation("ran the migration script", tool_name="Bash") == "note"


def test_classify_observation_tool_name_beats_text():
    # Write tool wins even when the text itself looks like a discovery.
    assert classify_observation("found the right config", tool_name="Write") == "change"


def test_classify_observation_bugfix_beats_change_keyword():
    # "fixed" (bugfix) outranks "created" (change) in precedence order.
    assert classify_observation("fixed the bug and created a regression test") == "bugfix"
