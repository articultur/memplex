"""Direct tests for memplex/llm/enhancer.py construction + LLM-path behaviour.

The dead methods (semantic_extract_trigger / resolve_conflict / summarize
and their private helpers) were removed in Wave 2b -- no production caller
existed (see CHANGELOG "Removed"). What remains under test here is
LLMEnhancer construction with a stub provider and the query-enhancement
path, which is wired into MemplexService intent detection / HyDE.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import json  # noqa: E402

import pytest  # noqa: E402

from memplex.config import LLMConfig  # noqa: E402
from memplex.llm.enhancer import LLMEnhancer  # noqa: E402

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

    async def rerank(self, query, results):
        return results

    async def complete(self, prompt, **kw):
        return ""

    async def complete_json(self, prompt, **kw):
        return {}


def test_enhancer_constructs_with_stub_provider():
    cfg = LLMConfig()
    cfg.query_enhancement = False
    enhancer = LLMEnhancer(llm_provider=_StubProvider(), config=cfg)
    assert enhancer is not None
    assert enhancer.config is cfg


# ── LLM-path behaviour (stub provider) ──────────────────────────────


class _RecordingProvider:
    """Captures prompts and returns canned complete_json payloads."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.prompts: list = []

    async def complete_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.payload


@pytest.mark.asyncio
async def test_enhance_query_respects_max_input_length_config():
    """llm.max_input_length must replace the sanitizer's hard-coded 10000."""
    cfg = LLMConfig()
    cfg.max_input_length = 10
    provider = _RecordingProvider({"intent": "search", "expanded_queries": ["q"]})
    enhancer = LLMEnhancer(llm_provider=provider, config=cfg)
    await enhancer.enhance_query("a" * 100)
    user_input = json.loads(provider.prompts[0])["user_input"]
    assert user_input.endswith("...(truncated)")
    assert len(user_input) == len("a" * 10 + "...(truncated)")


@pytest.mark.asyncio
async def test_enhance_query_disabled_returns_rule_based_default():
    """query_enhancement=False -> no LLM call, intent defaults to search."""
    provider = _RecordingProvider({})
    cfg = LLMConfig()
    cfg.query_enhancement = False
    enhancer = LLMEnhancer(llm_provider=provider, config=cfg)
    out = await enhancer.enhance_query("where is the login function")
    assert out.intent == "search"
    assert out.expanded == ["where is the login function"]
    assert provider.prompts == []  # no LLM round-trip


@pytest.mark.asyncio
async def test_hyde_falls_back_to_original_query_on_failure():
    """HyDE must never block the main pipeline: provider failure -> original."""

    class _BoomProvider:
        async def complete_json(self, prompt, **kw):
            raise RuntimeError("provider down")

    enhancer = LLMEnhancer(llm_provider=_BoomProvider(), config=LLMConfig())
    assert await enhancer.enhance_query_hyde_text("original query") == "original query"


# ── Observation compression (Feature 4) ─────────────────────────────


@pytest.mark.asyncio
async def test_compress_observation_calls_llm_and_passes_through():
    """LLM path: the provider's compressed summary is returned as-is."""
    provider = _RecordingProvider({"compressed": "fixed login bug in auth.py"})
    enhancer = LLMEnhancer(llm_provider=provider, config=LLMConfig())
    out = await enhancer.compress_observation("x" * 1000)
    assert out == "fixed login bug in auth.py"
    assert len(provider.prompts) == 1
    parsed = json.loads(provider.prompts[0])
    assert "x" * 100 in parsed["user_input"]
    assert parsed["output_format"] == {"compressed": "str"}


@pytest.mark.asyncio
async def test_compress_observation_falls_back_on_provider_error():
    """Provider failure must never block capture: rule-based head/tail truncation."""

    class _BoomProvider:
        async def complete_json(self, prompt, **kw):
            raise RuntimeError("provider down")

    enhancer = LLMEnhancer(llm_provider=_BoomProvider(), config=LLMConfig())
    content = "head" + "m" * 1000 + "tail"
    out = await enhancer.compress_observation(content, max_length=100)
    assert len(out) <= 100
    assert out.startswith("head")
    assert out.endswith("tail")
    assert "..." in out


@pytest.mark.asyncio
async def test_compress_observation_falls_back_on_empty_llm_result():
    """An LLM result without a "compressed" key degrades to rule truncation."""
    provider = _RecordingProvider({})  # FallbackChain end-of-chain behaviour
    enhancer = LLMEnhancer(llm_provider=provider, config=LLMConfig())
    out = await enhancer.compress_observation("y" * 1000, max_length=100)
    assert len(out) <= 100
    assert "..." in out


@pytest.mark.asyncio
async def test_compress_observation_rule_based_provider_uses_rules():
    """RuleBasedProvider = no LLM: direct rule truncation, no LLM round-trip."""
    from memplex.llm.providers.rule_based import RuleBasedProvider

    enhancer = LLMEnhancer(llm_provider=RuleBasedProvider(), config=LLMConfig())
    content = "a" * 500 + "b" * 500
    out = await enhancer.compress_observation(content, max_length=100)
    assert len(out) <= 100
    assert out.startswith("a")
    assert out.endswith("b")


@pytest.mark.asyncio
async def test_compress_observation_short_content_returned_unchanged():
    """Content already within max_length needs no compression (no LLM call)."""
    provider = _RecordingProvider({"compressed": "should not be used"})
    enhancer = LLMEnhancer(llm_provider=provider, config=LLMConfig())
    out = await enhancer.compress_observation("short", max_length=500)
    assert out == "short"
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_compress_observation_disabled_by_config_uses_rules():
    """llm.observation_compression=False -> no LLM call, rule-based truncation."""
    provider = _RecordingProvider({"compressed": "unused"})
    cfg = LLMConfig()
    cfg.observation_compression = False
    enhancer = LLMEnhancer(llm_provider=provider, config=cfg)
    out = await enhancer.compress_observation("y" * 1000, max_length=100)
    assert len(out) <= 100
    assert "..." in out
    assert provider.prompts == []


@pytest.mark.asyncio
async def test_compress_observation_enforces_max_length_on_llm_output():
    """Even a verbose LLM response is hard-truncated to max_length."""
    provider = _RecordingProvider({"compressed": "z" * 5000})
    enhancer = LLMEnhancer(llm_provider=provider, config=LLMConfig())
    out = await enhancer.compress_observation("w" * 1000, max_length=200)
    assert out == "z" * 200


@pytest.mark.asyncio
async def test_compress_observation_respects_max_input_length_config():
    """llm.max_input_length caps the content embedded into the prompt."""
    cfg = LLMConfig()
    cfg.max_input_length = 10
    provider = _RecordingProvider({"compressed": "ok"})
    enhancer = LLMEnhancer(llm_provider=provider, config=cfg)
    await enhancer.compress_observation("a" * 1000)
    user_input = json.loads(provider.prompts[0])["user_input"]
    assert user_input.endswith("...(truncated)")
    assert len(user_input) == len("a" * 10 + "...(truncated)")
