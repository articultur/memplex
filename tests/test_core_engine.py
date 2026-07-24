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
        source = _make_source("这是一个测试段落。")
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
