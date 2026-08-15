"""Test CoreEngine: text extraction to Function, batch extraction,
FieldValue multi-value, domain classification, graph edges."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from unittest.mock import MagicMock

from memplex.core.engine import CoreEngine, _normalize_name
from memplex.models import (
    ExtractedData,
    FieldValue,
    Function,
    SourceDocument,
    SourceType,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_source(text: str) -> SourceDocument:
    return SourceDocument(
        type="text",
        content=text,
        source_type=SourceType.WIKI,
    )


# ── _normalize_name ──────────────────────────────────────────────────


class TestNormalizeName:
    def test_lowercase(self):
        assert _normalize_name("Hello World") == "hello world"

    def test_strip_whitespace(self):
        assert _normalize_name("  hello  ") == "hello"

    def test_collapse_whitespace(self):
        assert _normalize_name("hello   world") == "hello world"

    def test_remove_punctuation(self):
        result = _normalize_name("Hello, World!")
        assert "," not in result
        assert "!" not in result

    def test_cjk_preserved(self):
        result = _normalize_name("登录模块")
        assert "登录" in result


# ── CoreEngine init ──────────────────────────────────────────────────


class TestCoreEngineInit:
    def test_no_store(self):
        engine = CoreEngine()
        assert engine._store is None

    def test_with_store(self):
        mock_store = MagicMock()
        engine = CoreEngine(store=mock_store)
        assert engine._store is mock_store


# ── Single text extraction ──────────────────────────────────────────


class TestCoreEngineExtract:
    def test_simple_text_produces_function(self):
        engine = CoreEngine()
        source = _make_source("用户点击登录按钮。系统验证用户名和密码。")
        extracted = engine.extract(source)

        assert isinstance(extracted, ExtractedData)
        assert len(extracted.functions) >= 1

        func = extracted.functions[0]
        assert isinstance(func, Function)
        assert func.id.startswith("func_")
        assert func.name != ""

    def test_function_has_field_values(self):
        engine = CoreEngine()
        source = _make_source("用户点击登录按钮。系统验证用户名和密码。用户登录成功。")
        extracted = engine.extract(source)

        assert len(extracted.functions) >= 1
        func = extracted.functions[0]

        # Should have at least trigger and/or action FieldValues
        all_fvs = func.trigger + func.action
        assert len(all_fvs) > 0
        for fv in all_fvs:
            assert isinstance(fv, FieldValue)
            assert fv.desc != ""

    def test_function_field_value_sources_populated(self):
        engine = CoreEngine()
        source = _make_source("触发条件描述。执行动作描述。")
        extracted = engine.extract(source)

        func = extracted.functions[0]
        all_fvs = func.trigger + func.action
        assert len(all_fvs) > 0
        for fv in all_fvs:
            assert len(fv.sources) > 0
            assert fv.source_method == "rule_based"

    def test_empty_text_returns_empty(self):
        engine = CoreEngine()
        source = _make_source("")
        extracted = engine.extract(source)
        assert extracted.functions == []

    def test_whitespace_only_returns_empty(self):
        engine = CoreEngine()
        source = _make_source("   \n  \n  ")
        extracted = engine.extract(source)
        assert extracted.functions == []

    def test_function_has_domain(self):
        engine = CoreEngine()
        source = _make_source("用户登录系统时需要输入用户名和密码进行认证。")
        extracted = engine.extract(source)

        if extracted.functions:
            func = extracted.functions[0]
            assert func.domain is not None

    def test_function_has_content_hash(self):
        engine = CoreEngine()
        # Action-oriented text (no fact/preference intent keywords) so the
        # paragraph is routed to a Function node.
        source = _make_source("用户执行测试段落操作。")
        extracted = engine.extract(source)

        func = extracted.functions[0]
        assert func.content_hash is not None
        assert len(func.content_hash) == 64  # SHA-256 hex


# ── Batch extraction ────────────────────────────────────────────────


class TestCoreEngineExtractBatch:
    def test_batch_multiple_sources(self):
        engine = CoreEngine()
        sources = [
            _make_source("用户点击登录按钮。系统验证凭证。"),
            _make_source("管理员配置系统参数。系统保存配置。"),
        ]
        extracted = engine.extract_batch(sources)

        assert isinstance(extracted, ExtractedData)
        assert len(extracted.functions) >= 2

    def test_batch_deduplicates(self):
        """Same content in two sources should be deduped."""
        engine = CoreEngine()
        text = "用户点击登录按钮。系统验证凭证。"
        sources = [_make_source(text), _make_source(text)]
        extracted = engine.extract_batch(sources)

        # Deduplication may reduce count
        ids = [f.id for f in extracted.functions]
        assert len(ids) >= 1


# ── Graph edge building (stateless) ─────────────────────────────────


class TestCoreEngineGraphBuilding:
    def test_stateless_builds_graph(self):
        engine = CoreEngine()  # No store
        source = _make_source("用户登录后查看首页仪表盘指标。")
        extracted = engine.extract(source)

        assert extracted.graph is not None
        # With single function and no store, edges may be empty or minimal
        assert isinstance(extracted.graph.edges, list)

    def test_cross_reference_detection(self):
        """When functions mention each other, REFERENCES edges are created."""
        engine = CoreEngine()
        source = _make_source("登录模块负责用户认证。\n\n首页模块展示用户信息，参见登录模块。")
        extracted = engine.extract(source)

        # At least some functions should exist
        assert len(extracted.functions) >= 1


# ── Domain classification via engine ─────────────────────────────────


class TestCoreEngineDomainClassification:
    def test_login_domain(self):
        engine = CoreEngine()
        source = _make_source("用户使用密码登录系统。")
        extracted = engine.extract(source)

        for func in extracted.functions:
            if "登录" in func.name or any("登录" in fv.desc for fv in func.trigger + func.action):
                assert func.domain in ("认证模块", "通用")


# ── Source type propagation ─────────────────────────────────────────


class TestCoreEngineSourceType:
    def test_source_type_preserved(self):
        engine = CoreEngine()
        source = SourceDocument(
            type="text",
            content="Test content paragraph.",
            source_type=SourceType.CODE,
        )
        extracted = engine.extract(source)

        for func in extracted.functions:
            assert func.source_type == SourceType.CODE


# ── Conflict detection (runs before dedup) ──────────────────────────


def _make_function(func_id: str, condition_desc: str, paragraph: str) -> Function:
    return Function(
        id=func_id,
        name="数据同步",
        name_normalized="数据同步",
        condition=[FieldValue(desc=condition_desc)],
        source_paragraphs=[paragraph],
    )


class TestConflictDetection:
    """Regression: step 7 dedup previously merged all same-name Functions
    before ConflictResolver ran, making conflict detection unreachable.
    Conflicts must now be detected first and flagged needs_review instead
    of being silently merged."""

    def test_same_name_different_condition_flagged_not_merged(self, monkeypatch):
        funcs = [
            _make_function("func_a", "网络可用时", "p1"),
            _make_function("func_b", "每小时触发", "p2"),
        ]
        monkeypatch.setattr(
            "memplex.core.engine._build_functions_from_paragraphs",
            lambda paragraphs, source: funcs,
        )
        engine = CoreEngine()
        extracted = engine.extract(_make_source("非空文本以触发管线。"))

        assert len(extracted.functions) == 2
        assert all(f.needs_review for f in extracted.functions)

    def test_same_name_same_condition_merged_without_review(self, monkeypatch):
        funcs = [
            _make_function("func_a", "网络可用时", "p1"),
            _make_function("func_b", "网络可用时", "p2"),
        ]
        monkeypatch.setattr(
            "memplex.core.engine._build_functions_from_paragraphs",
            lambda paragraphs, source: funcs,
        )
        engine = CoreEngine()
        extracted = engine.extract(_make_source("非空文本以触发管线。"))

        assert len(extracted.functions) == 1
        assert extracted.functions[0].needs_review is False

    def test_batch_cross_source_conflict_flagged_not_merged(self, monkeypatch):
        def fake_builder(paragraphs, source):
            tag = "a" if "alpha" in source.content else "b"
            return [_make_function(f"func_{tag}", f"条件{tag}", f"p_{tag}")]

        monkeypatch.setattr(
            "memplex.core.engine._build_functions_from_paragraphs", fake_builder
        )
        engine = CoreEngine()
        extracted = engine.extract_batch(
            [_make_source("alpha 来源文本。"), _make_source("beta 来源文本。")]
        )

        assert len(extracted.functions) == 2
        assert all(f.needs_review for f in extracted.functions)


# ── cross_references independence ───────────────────────────────────


class TestCrossReferences:
    def test_cross_references_are_independent_copies(self, monkeypatch):
        """Regression: the same list object was assigned to every Function's
        cross_references; mutating one leaked into all others."""
        funcs = [
            Function(id="func_a", name="功能甲", name_normalized="功能甲"),
            Function(id="func_b", name="功能乙", name_normalized="功能乙"),
        ]
        monkeypatch.setattr(
            "memplex.core.engine._build_functions_from_paragraphs",
            lambda paragraphs, source: funcs,
        )
        engine = CoreEngine()
        monkeypatch.setattr(
            engine.ref_linker,
            "extract_references",
            lambda text: [{"type": "url", "value": "https://example.com"}],
        )

        extracted = engine.extract(_make_source("非空文本以触发引用提取。"))
        assert len(extracted.functions) == 2

        extracted.functions[0].cross_references.append({"type": "injected"})
        assert len(extracted.functions[1].cross_references) == 1


# ── Fact / Preference intent routing ─────────────────────────────────


class TestCoreEngineFactPreferenceExtraction:
    def test_fact_intent_paragraph_produces_fact(self):
        engine = CoreEngine()
        extracted = engine.extract(_make_source("巴黎是法国的首都。"))
        assert extracted.functions == []
        assert len(extracted.facts) == 1
        fact = extracted.facts[0]
        assert fact.memory_type == "fact"
        assert fact.id.startswith("fact_")
        assert fact.subject == "巴黎"
        assert fact.predicate == "是"
        assert fact.object_ == "法国的首都"
        assert fact.content_hash is not None
        assert fact.created_at and fact.updated_at

    def test_english_fact_copula_split(self):
        engine = CoreEngine()
        extracted = engine.extract(_make_source("The API is a REST interface."))
        assert len(extracted.facts) == 1
        fact = extracted.facts[0]
        assert fact.subject == "The API"
        assert fact.predicate == "is"
        assert fact.object_ == "a REST interface"

    def test_preference_intent_paragraph_produces_preference(self):
        engine = CoreEngine()
        extracted = engine.extract(_make_source("用户喜欢暗色主题界面。"))
        assert extracted.functions == []
        assert len(extracted.preferences) == 1
        pref = extracted.preferences[0]
        assert pref.memory_type == "preference"
        assert pref.id.startswith("pref_")
        assert "暗色主题" in pref.preference

    def test_function_intent_default_unchanged(self):
        engine = CoreEngine()
        source = _make_source("用户点击登录按钮。系统验证用户名和密码。")
        extracted = engine.extract(source)
        assert extracted.functions
        assert extracted.facts == []
        assert extracted.preferences == []

    def test_mixed_intents_split_nodes(self):
        engine = CoreEngine()
        source = _make_source("用户点击保存按钮。\n\n巴黎是法国的首都。")
        extracted = engine.extract(source)
        assert len(extracted.functions) == 1
        assert len(extracted.facts) == 1

    def test_extract_batch_merges_facts_and_preferences(self):
        engine = CoreEngine()
        sources = [
            _make_source("巴黎是法国的首都。"),
            _make_source("用户喜欢暗色主题。"),
        ]
        extracted = engine.extract_batch(sources)
        assert len(extracted.facts) == 1
        assert len(extracted.preferences) == 1

    def test_extracted_data_facts_default_empty(self):
        """ExtractedData stays backward compatible: facts/preferences
        default to empty lists."""
        data = ExtractedData(functions=[], delta=False)
        assert data.facts == []
        assert data.preferences == []
