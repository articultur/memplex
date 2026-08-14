"""Tests for retain()-style factual capture (LLMEnhancer.factualize + write wiring)."""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.config import LLMConfig  # noqa: E402
from memplex.llm.enhancer import LLMEnhancer  # noqa: E402
from memplex.llm.providers.rule_based import RuleBasedProvider  # noqa: E402


class _FakeProvider:
    """Provider returning a canned facts payload."""

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    async def complete_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.payload


def _enhancer(provider) -> LLMEnhancer:
    return LLMEnhancer(llm_provider=provider, config=LLMConfig())


def test_factualize_resolves_and_normalises():
    provider = _FakeProvider(
        {
            "facts": [
                "Alice adopted Python for data analysis on 2025-03-01.",
                "  Bob switched to Neovim on 2025-05-02.  ",
                "",
                42,
            ]
        }
    )
    facts = asyncio.run(_enhancer(provider).factualize("she uses it... he switched last month"))
    assert facts == [
        "Alice adopted Python for data analysis on 2025-03-01.",
        "Bob switched to Neovim on 2025-05-02.",
    ]
    # The prompt pins coreference + temporal normalisation + sentence rules
    assert "coreference" in provider.prompts[0] or "pronoun" in provider.prompts[0]
    assert "absolute ISO dates" in provider.prompts[0]


def test_factualize_caps_max_facts():
    provider = _FakeProvider({"facts": [f"fact {i}" for i in range(20)]})
    facts = asyncio.run(_enhancer(provider).factualize("text", max_facts=3))
    assert facts == ["fact 0", "fact 1", "fact 2"]


def test_factualize_rule_based_provider_returns_empty():
    enhancer = _enhancer(RuleBasedProvider())
    assert asyncio.run(enhancer.factualize("any text")) == []


def test_factualize_provider_failure_is_non_blocking():
    class _Boom:
        async def complete_json(self, prompt, **kwargs):
            raise RuntimeError("provider down")

    assert asyncio.run(_enhancer(_Boom()).factualize("text")) == []


def test_factualize_malformed_payload_returns_empty():
    assert asyncio.run(_enhancer(_FakeProvider({"facts": "not-a-list"})).factualize("t")) == []
    assert asyncio.run(_enhancer(_FakeProvider({})).factualize("t")) == []


def test_write_augmentation_flag(tmp_path, monkeypatch):
    """service.write appends extracted facts only when factual_capture=True."""
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    captured = []

    class _RecordingEnhancer:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def factualize(self, content):
            captured.append(content)
            return ["Carol completed 5 books by 2025-03-31."]

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.llm.query_enhancement = False

    svc = MemplexService(config=cfg)
    svc._llm = _RecordingEnhancer(svc._llm)
    svc.start()
    try:
        from memplex.models import SourceDocument, SourceType

        doc = SourceDocument(
            type="conversation",
            content="I finished 2 books early March and 3 more late March.",
            source_type=SourceType.MEETING,
        )
        # Flag off (default): no capture call
        svc.write(doc)
        assert captured == []
        # Flag on: facts appended to capture content
        cfg.llm.factual_capture = True
        svc.write(doc)
        assert len(captured) == 1
    finally:
        svc.stop()
