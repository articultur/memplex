"""Evidence-linked factual capture: offline contract, never model quality evidence."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import pytest

from memplex.config import LLMConfig, MemplexConfig
from memplex.llm.enhancer import LLMEnhancer
from memplex.models import SourceDocument
from memplex.service import MemplexService


class FakeProvider:
    def __init__(self, payload=None):
        self.payload = payload
        self.prompts = []

    async def complete_json(self, prompt):
        self.prompts.append(json.loads(prompt))
        if callable(self.payload):
            return self.payload(json.loads(prompt))
        return self.payload


def payload_for(prompt):
    evidence = prompt["evidence"][0]
    return {"facts": [{
        "subject": "Alice", "predicate": "uses", "object": "PostgreSQL",
        "evidence": [{"paragraph_id": evidence["paragraph_id"],
                      "quote": "Alice uses PostgreSQL."}],
    }, {
        "subject": "Bob", "predicate": "uses", "object": "SQLite",
        "evidence": [{"paragraph_id": evidence["paragraph_id"],
                      "quote": "Bob uses SQLite."}],
    }]}


def service(tmp_path, payload=payload_for):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.embedding.model = "tfidf"
    cfg.llm.provider = "rule-based"
    cfg.llm.query_enhancement = False
    cfg.llm.factual_capture = True
    cfg.embedding.hyde_enabled = False
    cfg.wiki.enabled = False
    cfg.sleep_time.enabled = False
    svc = MemplexService(config=cfg)
    svc._llm = LLMEnhancer(FakeProvider(payload), cfg.llm)
    return svc


def test_write_keeps_original_and_creates_two_evidence_linked_facts(tmp_path):
    svc = service(tmp_path)
    doc = SourceDocument(type="text", content="Alice uses PostgreSQL. Bob uses SQLite.",
                         content_hash="caller-owned")
    try:
        result = svc.write(doc)
        assert doc.content == "Alice uses PostgreSQL. Bob uses SQLite."
        assert doc.content_hash == "caller-owned"
        assert [p.raw_text for p in result.paragraphs] == [doc.content]
        derived = [f for f in result.facts if f.provenance.get("extraction") == "factual_capture_v1"]
        assert [(f.subject, f.predicate, f.object_) for f in derived] == [
            ("Alice", "uses", "PostgreSQL"), ("Bob", "uses", "SQLite")]
        assert all(f.trust_tier == 1 and f.namespace["memplex_derivation"] for f in derived)
        assert all(f.source_paragraphs and f.valid_from is None for f in derived)
        assert result.factual_capture["status"] == "success"
        assert result.factual_capture["accepted"] == 2
        for fact in derived:
            assert svc.get(fact.id) is not None
            raw_id = fact.source_paragraphs[0]
            assert svc.store.read_context_nodes([raw_id])[raw_id]["raw_text"] == doc.content
    finally:
        svc.stop()


def test_private_text_never_reaches_extractor_or_changes_callers_document(tmp_path):
    svc = service(tmp_path, {"facts": []})
    doc = SourceDocument(type="text", content="Public. <private>Hidden.</private>")
    try:
        result = svc.write(doc)
        assert doc.content.endswith("</private>")
        assert "Hidden" not in json.dumps(svc._llm.llm.prompts)
        assert all("Hidden" not in p.raw_text for p in result.paragraphs)
        assert result.factual_capture["status"] == "abstained"
    finally:
        svc.stop()


@pytest.mark.parametrize("payload", [{}, {"facts": "wrong"}, {"facts": ["legacy sentence"]},
                                      {"facts": [], "tenant_id": "forged"}])
def test_invalid_is_not_empty_success(tmp_path, payload):
    svc = service(tmp_path, payload)
    try:
        result = svc.write_text("The database is SQLite.")
        assert result.factual_capture["status"] == "invalid"
        assert not any(f.trust_tier == 1 for f in result.facts)
        assert any(f.object_ == "SQLite" for f in result.facts)
    finally:
        svc.stop()


def test_flag_off_never_calls_provider(tmp_path):
    svc = service(tmp_path)
    svc._config.llm.factual_capture = False
    try:
        result = svc.write_text("The database is SQLite.")
        assert svc._llm.llm.prompts == []
        assert result.factual_capture is None
    finally:
        svc.stop()


def test_async_deadline_cancels_and_cannot_write_late(tmp_path):
    svc = service(tmp_path)
    cancelled = []

    class Slow:
        async def complete_json(self, prompt):
            try:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return payload_for(json.loads(prompt))

    svc._config.llm.factual_capture_timeout_seconds = 0.04
    svc._llm.llm = Slow()
    try:
        start = time.monotonic()
        result = svc.write_text("Alice uses PostgreSQL. Bob uses SQLite.")
        assert time.monotonic() - start < 0.2
        assert result.factual_capture["status"] == "timeout"
        time.sleep(0.35)
        assert cancelled
        assert not any(f.trust_tier == 1 for f in svc.store.list_facts())
    finally:
        svc.stop()


def test_runtime_supplies_trusted_reference_and_separates_speakers(tmp_path):
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    svc = service(tmp_path, {"facts": []})
    runtime = AgentMemoryRuntime(service=svc)
    try:
        runtime.capture_turn("Alice uses PostgreSQL.", "Bob uses SQLite.")
        prompts = svc._llm.llm.prompts
        assert len(prompts) == 2
        assert [p["author_role"] for p in prompts] == ["user", "assistant"]
        assert prompts[0]["reference_datetime"] == prompts[1]["reference_datetime"]
        assert datetime.fromisoformat(prompts[0]["reference_datetime"]).utcoffset() is not None
        assert "Bob" not in json.dumps(prompts[0]["evidence"])
        assert "Alice" not in json.dumps(prompts[1]["evidence"])
        assistant_nodes = [n for n in svc.store.list_facts() if "Bob" in n.subject or "Bob" in n.object_]
        assert all(n.trust_tier == 1 for n in assistant_nodes)
    finally:
        svc.stop()


def test_file_source_resolution_is_preserved(tmp_path):
    path = tmp_path / "input.md"
    path.write_text("Alice uses PostgreSQL. Bob uses SQLite.")
    svc = service(tmp_path / "store")
    try:
        result = svc.write(SourceDocument(type="file", source_path=str(path)))
        assert result.paragraphs
        assert result.factual_capture["accepted"] == 2
        originals = [f for f in result.facts if f.provenance.get("extraction") != "factual_capture_v1"]
        assert all(f.trust_tier == 3 for f in originals)
    finally:
        svc.stop()


def test_assistant_rule_fact_cannot_supersede_user_assertion(tmp_path):
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    svc = service(tmp_path, {"facts": []})
    try:
        AgentMemoryRuntime(service=svc).capture_turn("The database is PostgreSQL.", "The database is SQLite.")
        original = next(f for f in svc.store.list_facts() if f.object_ == "PostgreSQL")
        assert original.invalid_at is None
    finally:
        svc.stop()


def test_equal_speaker_text_preserves_both_original_assertions(tmp_path):
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    svc = service(tmp_path, {"facts": []})
    try:
        text = "The database is PostgreSQL."
        AgentMemoryRuntime(service=svc).capture_turn(text, text)
        originals = [f for f in svc.store.list_facts() if f.object_ == "PostgreSQL"]
        assert len(originals) == 2
        assert {f.trust_tier for f in originals} == {1, 4}
        assert len({f.source_paragraphs[0] for f in originals}) == 2
        assert all(f.invalid_at is None for f in originals)
    finally:
        svc.stop()


def test_derived_facts_read_committed_originals(tmp_path, monkeypatch):
    """Derivation sees the committed original typed and raw evidence phases."""
    from memplex.models import ExtractedData, Fact, Paragraph
    from memplex.models.paragraph import persisted_paragraph_id

    monkeypatch.delenv("MEMPLEX_LITE_TYPED_BATCH", raising=False)
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_AUTHORITY", raising=False)
    text = "Alice uses PostgreSQL. Bob uses SQLite."
    raw_id = persisted_paragraph_id("text", "raw", text)
    observed = []
    svc = service(tmp_path)
    commits = []
    original_commit = svc.store._durability.commit_locked

    def commit(base, target, **kwargs):
        commits.append(target)
        return original_commit(base, target, **kwargs)

    def payload(prompt):
        original_facts = svc.store.list_facts()
        assert len(original_facts) == 2
        assert all(fact.valid_from for fact in original_facts)
        evidence_id = prompt["evidence"][0]["paragraph_id"]
        rows = svc.store.read_context_nodes([evidence_id, *(fact.id for fact in original_facts)])
        assert len(rows) == 3
        assert rows[evidence_id]["raw_text"] == text
        assert svc.store.generation == 2
        observed.append(evidence_id)
        return payload_for(prompt)

    monkeypatch.setattr(svc.store._durability, "commit_locked", commit)
    monkeypatch.setattr(svc._engine, "extract", lambda _source: ExtractedData(
        facts=[
            Fact(id="original-alice", subject="Alice", predicate="uses", object_="PostgreSQL", source_paragraphs=[raw_id]),
            Fact(id="original-bob", subject="Bob", predicate="uses", object_="SQLite", source_paragraphs=[raw_id]),
        ],
        paragraphs=[Paragraph(id="raw", source="text", section="1", raw_text=text)],
    ))
    svc._llm.llm = FakeProvider(payload)
    try:
        result = svc.write(SourceDocument(type="text", content=text))
        assert len(observed) == 1
        assert len(commits) == 3
        assert [len(target.memory["facts"]) for target in commits] == [2, 2, 4]
        assert [len(target.memory.get("paragraphs", [])) for target in commits] == [0, 1, 1]
        assert result.factual_capture["status"] == "success"
        assert result.factual_capture["accepted"] == 2
        derived = [fact for fact in result.facts if fact.provenance.get("extraction") == "factual_capture_v1"]
        assert len(derived) == 2
        assert all(fact.valid_from is None for fact in derived)
        assert all(svc.store.get_fact(fact.id) is not None for fact in derived)
    finally:
        svc.stop()
