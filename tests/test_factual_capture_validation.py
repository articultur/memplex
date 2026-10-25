"""Strict evidence/date/provider contract. All completions are offline doubles."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from memplex.config import LLMConfig
from memplex.llm.enhancer import LLMEnhancer
from memplex.llm.factual_capture import Evidence, validate_payload
from memplex.llm.fallback_chain import FallbackChain
from memplex.llm.providers.rule_based import RuleBasedProvider
from tests.test_factual_capture import FakeProvider

EVIDENCE = (Evidence("raw-1", "Alice moved to Paris yesterday. Bob uses SQLite."),)


def fact(**changes):
    candidate = {"subject": "Alice", "predicate": "moved to", "object": "Paris",
                 "evidence": [{"paragraph_id": "raw-1", "quote": "Alice moved to Paris yesterday."}]}
    candidate.update(changes)
    return {"facts": [candidate]}


@pytest.mark.parametrize("changes", [
    {"tenant_id": "forged"}, {"subject": 123}, {"object": "x" * 2001},
    {"evidence": []}, {"evidence": [{"paragraph_id": "other", "quote": "Alice"}]},
    {"evidence": [{"paragraph_id": "raw-1", "quote": "private missing phrase"}]},
    {"evidence": [{"paragraph_id": "raw-1", "quote": ""}]},
    {"valid_from": "2020-01-01"},
])
def test_rejects_invalid_candidate(changes):
    with pytest.raises(ValueError):
        validate_payload(fact(**changes), EVIDENCE, reference_datetime=None, max_facts=8)


@pytest.mark.parametrize("reference,expected", [("2026-10-07", "2026-10-06"), ("2026-01-01", "2025-12-31")])
def test_relative_date_requires_correct_explicit_reference(reference, expected):
    candidates = validate_payload(fact(valid_from=expected), EVIDENCE,
                                  reference_datetime=datetime.fromisoformat(reference).replace(tzinfo=UTC),
                                  max_facts=8)
    assert candidates[0].valid_from == expected + "T00:00:00+00:00"


def test_unknown_date_stays_unknown():
    candidates = validate_payload(fact(), EVIDENCE, reference_datetime=None, max_facts=8)
    assert candidates[0].valid_from is None


def test_fallback_validates_each_attempt_and_reports_invalid():
    bad, good = FakeProvider({}), FakeProvider(fact())
    enhancer = LLMEnhancer(FallbackChain([bad, good]), LLMConfig())
    result = asyncio.run(enhancer.factualize(EVIDENCE))
    assert result.status == "success" and result.fallback_used
    assert [attempt.status for attempt in result.attempts] == ["invalid", "success"]
    assert len(good.prompts) == 1


def test_rule_based_is_unavailable_not_abstention():
    result = asyncio.run(LLMEnhancer(RuleBasedProvider(), LLMConfig()).factualize(EVIDENCE))
    assert result.status == "unavailable"


def test_exception_is_failure_not_abstention():
    class Broken:
        async def complete_json(self, prompt):
            raise RuntimeError("not persisted or echoed")
    result = asyncio.run(LLMEnhancer(Broken(), LLMConfig()).factualize(EVIDENCE))
    assert result.status == "provider_failure"
    assert "not persisted" not in str(result)


@pytest.mark.parametrize("kwargs", [
    {"factual_capture_timeout_seconds": 0}, {"factual_capture_timeout_seconds": float("nan")},
    {"factual_capture_timeout_seconds": True}, {"factual_capture_max_facts": 0},
    {"factual_capture_max_facts": True}, {"factual_capture_max_facts": 65},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        LLMConfig(**kwargs)


def test_generated_object_must_be_present_in_cited_evidence():
    with pytest.raises(ValueError):
        validate_payload(fact(object="London"), EVIDENCE, reference_datetime=None, max_facts=8)


@pytest.mark.parametrize("provider_name", ["local", "anthropic"])
def test_builtin_factual_transport_is_strict_and_disables_retries(provider_name):
    from types import SimpleNamespace

    from memplex.llm.providers.anthropic import AnthropicProvider
    from memplex.llm.providers.local import LocalProvider

    calls = []

    class Client:
        def with_options(self, **kwargs):
            calls.append(kwargs)
            return self

        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="not JSON"))],
                content=[SimpleNamespace(type="text", text="not JSON")],
            )
    client = Client()
    client.chat = SimpleNamespace(completions=client)
    client.messages = client
    provider = (LocalProvider(client=client) if provider_name == "local"
                else AnthropicProvider(api_key="test-only", client=client))
    good = FakeProvider(fact())
    result = asyncio.run(LLMEnhancer(FallbackChain([provider, good]), LLMConfig()).factualize(EVIDENCE))
    assert result.status == "success" and result.fallback_used
    assert result.attempts[0].status == "invalid"
    assert calls == [{"timeout": 10.0, "max_retries": 0}]


def test_raw_lineage_lookup_uses_committed_projection_on_postgres_shaped_store():
    from memplex.authorization import _TypedNodeLookup

    class Store:
        def get(self, node_id):
            return None

        def read_context_nodes(self, ids):
            return {"raw": {"id": "raw", "raw_text": "Original", "tenant_id": "tenant"}}
    node = _TypedNodeLookup(Store()).get("raw")
    assert node is not None and node.id == "raw" and node.tenant_id == "tenant"


def test_raw_lineage_lookup_rejects_aliased_projection():
    from memplex.authorization import _TypedNodeLookup

    class Store:
        def get(self, node_id):
            return None

        def read_context_nodes(self, ids):
            return {"raw": {"id": "different", "raw_text": "Not the requested evidence"}}
    assert _TypedNodeLookup(Store()).get("raw") is None


def test_async_entry_point_is_bounded_when_provider_suppresses_cancellation():
    import time

    class Uncooperative:
        async def complete_json(self, prompt):
            try:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)
            return {"facts": []}
    enhancer = LLMEnhancer(Uncooperative(), LLMConfig(factual_capture_timeout_seconds=0.02))
    start = time.monotonic()
    outcome = asyncio.run(enhancer.factualize(EVIDENCE))
    assert outcome.status == "timeout"
    assert time.monotonic() - start < 0.15


@pytest.mark.parametrize("name,value", [("TIMEOUT_SECONDS", "inf"), ("MAX_FACTS", "1000000")])
def test_environment_cannot_bypass_capture_bounds(tmp_path, monkeypatch, name, value):
    from memplex.config import load_config

    monkeypatch.setenv("MEMPLEX_LLM_FACTUAL_CAPTURE_" + name, value)
    with pytest.raises(ValueError):
        load_config(str(tmp_path / "missing.yaml"))


def test_factual_candidates_render_low_even_from_requirement_source(tmp_path):
    from memplex.llm.injection_guard import IndirectInjectionGuard
    from memplex.models import SearchResult, SourceDocument, SourceType
    from tests.test_factual_capture import service

    svc = service(tmp_path)
    try:
        result = svc.write(SourceDocument(type="text", content="Alice uses PostgreSQL. Bob uses SQLite.",
                                          source_type=SourceType.REQUIREMENT))
        fact = next(f for f in result.facts if f.trust_tier == 1)
        wrapped = IndirectInjectionGuard.wrap_for_context(
            [SearchResult(func_id=fact.id, name=fact.name, domain="", relevance_score=1, summary=fact.name)],
            svc._typed_lookup_for(svc._require_authorization(None)),
        )
        assert "trust=LOW" in wrapped and "trust=HIGH" not in wrapped
        assert fact.source_type == SourceType.REQUIREMENT
    finally:
        svc.stop()


def test_lite_raw_replay_never_adopts_legacy_identity(tmp_path):
    from memplex.auth import AuthorizationContext, Principal
    from memplex.models import Paragraph
    from memplex.storage.lite.store import LiteMemoryStore

    store = LiteMemoryStore(tmp_path / "legacy.json")
    paragraph = Paragraph(id="p1", source="text", section="", raw_text="Original")
    store.persist_paragraphs([paragraph], trust_tier=3, source_hint="legacy")
    context = AuthorizationContext(Principal(tenant_id="tenant", subject_id="user"), "workspace",
                                   agent_id="agent", session_id="session")
    store.persist_paragraphs([paragraph], trust_tier=4, source_hint="legacy", authorization=context)
    row, = store._paragraphs.values()
    assert "tenant_id" not in row


def test_outer_timeout_does_not_claim_no_fallback():
    class Slow:
        async def complete_json(self, prompt):
            await asyncio.sleep(0.3)
            return {"facts": []}
    enhancer = LLMEnhancer(FallbackChain([FakeProvider({}), Slow()]),
                           LLMConfig(factual_capture_timeout_seconds=0.02))
    result = asyncio.run(enhancer.factualize(EVIDENCE))
    receipt = result.receipt()
    assert receipt["status"] == "timeout"
    assert receipt["fallback_used"] is None or receipt["fallback_used"] is True
    if receipt["fallback_used"] is None:
        assert receipt["attempts_complete"] is False
