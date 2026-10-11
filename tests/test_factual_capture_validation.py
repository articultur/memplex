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


@pytest.mark.parametrize("schedule", ["started", "delayed-start"])
def test_async_entry_point_is_bounded_when_provider_suppresses_cancellation(monkeypatch, schedule):
    from contextlib import suppress
    from contextvars import ContextVar
    from threading import Condition, Event, Thread, current_thread

    from memplex.llm import factual_capture

    budget, watchdog = 0.02, 5.0
    provider_entered, provider_release, startup_release, caller_returned = (
        Event(), Event(), Event(), Event()
    )
    if schedule == "started":
        startup_release.set()
    provider_state, cancellations, workers, outcomes, errors, waits, done_events, admissions, releases = (
        [], [], [], [], [], [], [], [], []
    )
    cancelled = Condition()
    owned_capture = ContextVar("test_factual_capture_owner", default=False)
    capture_slots = factual_capture._CAPTURE_SLOTS
    # Wait for one available permit during fixture setup, then restore it.
    # The actual helper still performs its unchanged nonblocking admission.
    assert capture_slots.acquire(timeout=watchdog), "fixture could not obtain one capture slot"
    capture_slots.release()

    class ObservedSlots:
        def acquire(self, *args, **kwargs):
            admitted = capture_slots.acquire(*args, **kwargs)
            if owned_capture.get():
                admissions.append((current_thread(), admitted))
            return admitted

        def release(self, *args, **kwargs):
            capture_slots.release(*args, **kwargs)
            if current_thread() in workers:
                releases.append(current_thread())

    class Uncooperative:
        async def complete_json(self, prompt):
            loop = asyncio.get_running_loop()
            released = loop.create_future()
            provider_state.append((loop, released, asyncio.current_task()))
            provider_entered.set()
            while not provider_release.is_set():
                try:
                    await asyncio.shield(released)
                except asyncio.CancelledError:
                    with cancelled:
                        cancellations.append("suppressed")
                        cancelled.notify_all()
            return {"facts": []}

    class ObservedDone:
        def __init__(self):
            self.event = Event()
            done_events.append(self.event)

        def set(self):
            self.event.set()

        def wait(self, timeout):
            if schedule == "started":
                assert provider_entered.wait(watchdog), "provider did not enter before bounded wait"
            waits.append(timeout)
            # Observe the real helper's argument and delegate with that exact budget.
            return self.event.wait(timeout)

    def capture_done():
        return ObservedDone() if owned_capture.get() else Event()

    def capture_worker(*, target, **kwargs):
        if not owned_capture.get():
            return Thread(target=target, **kwargs)

        def controlled_target():
            startup_release.wait()
            target()
        worker = Thread(target=controlled_target, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(factual_capture, "Event", capture_done)
    monkeypatch.setattr(factual_capture, "Thread", capture_worker)
    monkeypatch.setattr(factual_capture, "_CAPTURE_SLOTS", ObservedSlots())
    enhancer = LLMEnhancer(Uncooperative(), LLMConfig(factual_capture_timeout_seconds=budget))

    def call_public_entry():
        token = owned_capture.set(True)
        try:
            outcomes.append(asyncio.run(enhancer.factualize(EVIDENCE)))
        except Exception as exc:  # noqa: BLE001 - preserve caller failures for foreground assertions
            errors.append(exc)
        finally:
            owned_capture.reset(token)
            caller_returned.set()

    caller = Thread(target=call_public_entry, name="test-factual-capture-caller", daemon=True)
    returned_outcomes = None
    caller.start()
    try:
        assert caller_returned.wait(watchdog), "caller coupled its return to the unfinished worker"
        assert not errors
        assert waits == [budget], "bounded Event.wait must receive exactly 20 ms"
        assert len(outcomes) == len(workers) == len(done_events) == 1
        outcome, = outcomes
        assert outcome.status == "timeout" and not outcome.attempts_complete
        # Preserve timeout/liveness evidence before opening either cleanup gate.
        returned_outcomes = tuple(outcomes)
        assert workers[0].is_alive() and not done_events[0].is_set()
        assert [admitted for _, admitted in admissions] == [True]
        assert not releases, "unfinished owned worker released its actual capture slot"
        if schedule == "delayed-start":
            assert not provider_entered.is_set() and not provider_state
            assert not startup_release.is_set()
        else:
            assert provider_entered.is_set() and not provider_release.is_set()
            loop, _, task = provider_state[0]
            # Repeated cancellations must not complete the gated provider.
            for _ in range(2):
                with cancelled:
                    previous = len(cancellations)
                    loop.call_soon_threadsafe(task.cancel)
                    assert cancelled.wait_for(
                        lambda previous=previous: len(cancellations) > previous, watchdog
                    )
                assert workers[0].is_alive() and not done_events[0].is_set()
    finally:
        startup_release.set()
        entered = provider_entered.wait(watchdog)
        provider_release.set()
        if provider_state:
            loop, released, _ = provider_state[0]

            def release_provider():
                if not released.done():
                    released.set_result(None)
            # A failed liveness assertion may mean the worker already closed its loop.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(release_provider)
        caller.join(watchdog)
        for worker in workers:
            worker.join(watchdog)
        assert entered, "provider never entered, including during delayed-start cleanup"
        assert not caller.is_alive(), "caller leaked after gate release"
        assert workers and all(not worker.is_alive() for worker in workers), "capture worker leaked"
        assert done_events and all(done.is_set() for done in done_events)
        assert releases == workers, "owned worker did not release its actual capture slot"
    assert tuple(outcomes) == returned_outcomes, "cleanup replaced the caller's timeout outcome"


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
