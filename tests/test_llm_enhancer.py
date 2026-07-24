"""Direct tests for memplex/llm/enhancer.py static helpers + rule-based path.

The LLM-dependent methods need a provider stub; here we cover the pure
staticmethods (_rule_based_extract, _authority_based_resolve,
_parse_resolution) and LLMEnhancer construction with a stub provider,
which were the bulk of the uncovered lines at 29%.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.config import LLMConfig  # noqa: E402
from memplex.llm.enhancer import LLMEnhancer  # noqa: E402
from memplex.models import FieldValue, Function, SourceType  # noqa: E402

# ── _rule_based_extract ──────────────────────────────────────────────


def test_rule_based_extract_splits_sentences():
    out = LLMEnhancer._rule_based_extract("first. second. third.", "trigger")
    assert len(out) == 3
    assert all(isinstance(fv, FieldValue) for fv in out)
    assert [fv.desc for fv in out] == ["first", "second", "third"]


def test_rule_based_extract_caps_at_five_sentences():
    para = ". ".join(f"s{i}" for i in range(10))
    out = LLMEnhancer._rule_based_extract(para, "action")
    assert len(out) == 5


def test_rule_based_extract_empty_returns_empty():
    assert LLMEnhancer._rule_based_extract("", "trigger") == []


def test_rule_based_extract_field_weights_are_low():
    out = LLMEnhancer._rule_based_extract("one.", "trigger")
    assert out[0].weight == 0.5
    assert out[0].source_method == "rule_based"


# ── _authority_based_resolve ─────────────────────────────────────────


def _func(source_type=SourceType.WIKI):
    return Function(
        id="f",
        name="n",
        name_normalized="n",
        source_type=source_type,
    )


def test_authority_resolution_prefers_higher_source():
    requirement = _func(SourceType.REQUIREMENT)
    wiki = _func(SourceType.WIKI)
    # func1 (requirement, priority 4) vs func2 (wiki, priority 1) -> keep_v1
    result = LLMEnhancer._authority_based_resolve(requirement, wiki)
    assert result["decision"] == "keep_v1"


def test_authority_resolution_lower_source_keeps_v2():
    wiki = _func(SourceType.WIKI)
    code = _func(SourceType.CODE)
    result = LLMEnhancer._authority_based_resolve(wiki, code)  # 1 vs 2
    assert result["decision"] == "keep_v2"


def test_authority_resolution_tie_keeps_v1():
    a = _func(SourceType.CODE)
    b = _func(SourceType.CODE)
    result = LLMEnhancer._authority_based_resolve(a, b)
    assert result["decision"] == "keep_v1"


# ── _parse_resolution ────────────────────────────────────────────────


def test_parse_resolution_defaults():
    out = LLMEnhancer._parse_resolution({})
    assert out["decision"] == "keep_v1"
    assert out["reasoning"] == ""
    assert out["merged_function"] == {}


def test_parse_resolution_preserves_values():
    out = LLMEnhancer._parse_resolution(
        {"decision": "keep_v2", "reasoning": "because", "merged_function": {"x": 1}}
    )
    assert out["decision"] == "keep_v2"
    assert out["reasoning"] == "because"
    assert out["merged_function"] == {"x": 1}


# ── Construction with a stub provider ───────────────────────────────


class _StubProvider:
    """Minimal LLMProvider stub: every method returns a benign default."""

    async def classify_intent(self, text):
        from memplex.models import IntentType

        return IntentType.IMMEDIATE

    async def enhance_query(self, text):
        from types import SimpleNamespace

        return SimpleNamespace(intent="search", expanded_query=text, hyde_text=None)

    async def enhance_query_hyde_text(self, text):
        return text

    async def extract_structured(self, paragraph, role):
        return []

    async def summarize(self, text):
        return text

    async def resolve_conflict(self, func1, func2):
        return {"decision": "keep_v1", "reasoning": "stub", "merged_function": {}}

    async def rerank(self, query, results):
        return results

    async def complete(self, prompt, **kw):
        return ""

    async def complete_json(self, prompt, **kw):
        return {}


def test_enhancer_constructs_with_stub_provider():
    cfg = LLMConfig()
    cfg.semantic_extraction = False
    cfg.query_enhancement = False
    cfg.conflict_resolution = False
    cfg.summarization = False
    cfg.reranking = False
    enhancer = LLMEnhancer(llm_provider=_StubProvider(), config=cfg)
    assert enhancer is not None
    assert enhancer.config is cfg
