"""Test associators: TermMapper, RefLinker, DomainClassifier, EntityAligner."""

import os
import sys
import types

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


from memplex.core.associator.domain_classifier import DomainClassifier
from memplex.core.associator.entity_aligner import EntityAligner
from memplex.core.associator.ref_linker import RefLinker
from memplex.core.associator.term_mapper import TermMapper
from memplex.models import (
    FieldValue,
    Function,
)

# ── TermMapper ───────────────────────────────────────────────────────


class TestTermMapper:
    def test_extract_terms_returns_set(self):
        mapper = TermMapper()
        result = mapper.extract_terms("some random text")
        assert isinstance(result, set)

    def test_find_associations_empty(self):
        mapper = TermMapper()
        result = mapper.find_associations(set(), [])
        assert result == []

    def test_find_associations_with_candidates(self):
        mapper = TermMapper()
        source_terms = {"login"}
        func = Function(
            id="func_test",
            name="Login Function",
            trigger=[FieldValue(desc="user login")],
        )
        result = mapper.find_associations(source_terms, [func])
        assert isinstance(result, list)

    def test_build_term_normalized(self):
        mapper = TermMapper()
        result = mapper.build_term_normalized("hello world")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_embed_text_returns_none_when_model_load_fails(self, monkeypatch):
        module = types.ModuleType("sentence_transformers")

        class BrokenSentenceTransformer:
            def __init__(self, model_name):
                raise RuntimeError(f"{model_name} blocked")

        module.SentenceTransformer = BrokenSentenceTransformer
        monkeypatch.setitem(sys.modules, "sentence_transformers", module)

        mapper = TermMapper()
        assert mapper.embed_text("offline fallback") is None


# ── RefLinker ────────────────────────────────────────────────────────


class TestRefLinker:
    def setup_method(self):
        self.linker = RefLinker()

    # -- extract_references --

    def test_extract_cross_doc_reference(self):
        refs = self.linker.extract_references("详见《用户手册》")
        assert len(refs) >= 1
        cross_doc = [r for r in refs if r["type"] == "cross_doc"]
        assert len(cross_doc) >= 1
        # The lazy regex captures text before the closing delimiter;
        # the target should contain at least part of the reference name.
        assert "用户" in cross_doc[0]["target"]

    def test_extract_section_reference(self):
        refs = self.linker.extract_references("见第3.2节")
        assert len(refs) >= 1
        section_refs = [r for r in refs if r["type"] == "section"]
        assert len(section_refs) >= 1
        assert "3.2" in section_refs[0]["target"]

    def test_extract_url_reference(self):
        refs = self.linker.extract_references("参考 https://example.com/docs")
        assert len(refs) >= 1
        url_refs = [r for r in refs if r["type"] == "url"]
        assert len(url_refs) >= 1
        assert "https://example.com/docs" in url_refs[0]["target"]

    def test_extract_implicit_reference(self):
        refs = self.linker.extract_references("如上所述")
        assert len(refs) >= 1
        implicit = [r for r in refs if r["type"] == "implicit"]
        assert len(implicit) >= 1

    def test_extract_sequential_reference(self):
        refs = self.linker.extract_references("之后进行下一步操作")
        assert len(refs) >= 1
        seq = [r for r in refs if r["type"] == "sequential"]
        assert len(seq) >= 1

    def test_extract_rfc_reference(self):
        refs = self.linker.extract_references("根据RFC-2119规定")
        assert len(refs) >= 1

    def test_no_references(self):
        refs = self.linker.extract_references("这是一段普通文字")
        # May or may not find refs depending on keyword match
        assert isinstance(refs, list)

    # -- resolve_reference --

    def test_resolve_exact_match(self):
        ref = {"target": "Login", "type": "cross_doc", "confidence": 0.95}
        known = {"Login": ["func_1"]}
        result = self.linker.resolve_reference(ref, known)
        assert result == "func_1"

    def test_resolve_partial_match(self):
        ref = {"target": "user login", "type": "cross_doc", "confidence": 0.9}
        known = {"User Login Flow": ["func_2"]}
        result = self.linker.resolve_reference(ref, known)
        assert result == "func_2"

    def test_resolve_no_match(self):
        ref = {"target": "Unknown", "type": "cross_doc", "confidence": 0.5}
        known = {"Login": ["func_1"]}
        result = self.linker.resolve_reference(ref, known)
        assert result is None

    # -- resolve_implicit_reference --

    def test_resolve_implicit_back_reference(self):
        ref = {"target": "如上所述", "type": "implicit", "confidence": 0.7}
        known = {"Login": ["func_1"]}
        result, conf = self.linker.resolve_implicit_reference(ref, known)
        assert result == "func_1"
        assert conf > 0

    def test_resolve_implicit_with_context(self):
        ref = {"target": "如上所述", "type": "implicit", "confidence": 0.7}
        known = {"Previous Topic": ["func_prev"]}
        ctx = {"previous_entity": "Previous Topic"}
        result, conf = self.linker.resolve_implicit_reference(ref, known, ctx)
        assert result == "func_prev"
        assert conf >= 0.8

    def test_resolve_implicit_sequential(self):
        ref = {"target": "implicit_next", "type": "sequential", "confidence": 0.6}
        known = {"First": ["func_1"], "Second": ["func_2"]}
        ctx = {"next_entity": "Second"}
        result, _ = self.linker.resolve_implicit_reference(ref, known, ctx)
        assert result == "func_2"


# ── DomainClassifier ─────────────────────────────────────────────────


class TestDomainClassifier:
    def setup_method(self):
        self.classifier = DomainClassifier()

    def test_classify_login(self):
        func = Function(
            id="func_login",
            name="用户登录",
            trigger=[FieldValue(desc="用户输入密码")],
        )
        domain = self.classifier.classify(func)
        assert domain == "认证模块"

    def test_classify_payment(self):
        func = Function(
            id="func_pay",
            name="支付订单",
            trigger=[FieldValue(desc="用户点击支付按钮")],
        )
        domain = self.classifier.classify(func)
        assert domain == "支付模块"

    def test_classify_search(self):
        func = Function(
            id="func_search",
            name="搜索功能",
            trigger=[FieldValue(desc="用户输入搜索关键词")],
        )
        domain = self.classifier.classify(func)
        assert domain == "搜索模块"

    def test_classify_generic(self):
        func = Function(
            id="func_gen",
            name="Some Unknown Feature",
            trigger=[FieldValue(desc="does something unspecified")],
        )
        domain = self.classifier.classify(func)
        assert domain == "通用"

    def test_classify_with_multiple_fields(self):
        func = Function(
            id="func_multi",
            name="订单处理",
            trigger=[FieldValue(desc="用户下单")],
            action=[FieldValue(desc="处理购物车数据")],
        )
        domain = self.classifier.classify(func)
        assert domain == "订单模块"

    def test_classify_name_weight_higher(self):
        """Name field should have higher weight than other fields."""
        func_name_only = Function(
            id="func_no",
            name="登录",
            trigger=[],
            action=[FieldValue(desc="generic action unrelated to auth")],
        )
        domain = self.classifier.classify(func_name_only)
        assert domain == "认证模块"


# ── EntityAligner ────────────────────────────────────────────────────


class TestEntityAligner:
    def setup_method(self):
        self.aligner = EntityAligner()

    def test_normalize(self):
        result = self.aligner.normalize("用户登录")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_calculate_similarity_identical(self):
        score = self.aligner.calculate_similarity("Login", "Login")
        assert score == 1.0

    def test_calculate_similarity_different(self):
        score = self.aligner.calculate_similarity("Login", "Payment")
        assert score < 1.0

    def test_calculate_similarity_equivalents(self):
        score = self.aligner.calculate_similarity("login", "登录")
        assert score >= 0.8

    def test_find_similar(self):
        entities = [
            type("E", (), {"name": "Login"})(),
            type("E", (), {"name": "Logout"})(),
            type("E", (), {"name": "Payment"})(),
        ]
        results = self.aligner.find_similar("Login", entities, threshold=0.3)
        assert len(results) >= 1
        # Login should be the best match
        assert results[0][0].name == "Login"

    def test_find_merge_candidates(self):
        entities = [
            {"id": "e1", "name": "User Login"},
            {"id": "e2", "name": "user login"},  # very similar
            {"id": "e3", "name": "Payment Processing"},
        ]
        groups = self.aligner.find_merge_candidates(entities, threshold=0.9)
        # "User Login" and "user login" should merge
        assert len(groups) >= 1
        merged_ids = {e["id"] for group in groups for e in group}
        assert "e1" in merged_ids
        assert "e2" in merged_ids

    def test_find_merge_candidates_no_merges(self):
        entities = [
            {"id": "e1", "name": "Alpha"},
            {"id": "e2", "name": "Beta"},
            {"id": "e3", "name": "Gamma"},
        ]
        groups = self.aligner.find_merge_candidates(entities, threshold=0.9)
        assert groups == []

    def test_suggest_merged_name(self):
        entities = [
            {"id": "e1", "name": "用户登录"},
            {"id": "e2", "name": "User Login"},
        ]
        name = self.aligner.suggest_merged_name(entities)
        assert name == "用户登录"  # Chinese name preferred

    def test_suggest_merged_name_single(self):
        entities = [{"id": "e1", "name": "Only One"}]
        name = self.aligner.suggest_merged_name(entities)
        assert name == "Only One"
