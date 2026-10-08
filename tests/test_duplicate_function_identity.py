"""Repeated source occurrences share content identity without losing evidence."""

from dataclasses import replace
from hashlib import sha256

import pytest

from memplex.auth import AuthorizationContext, MemoryNotFoundError, Principal
from memplex.config import MemplexConfig
from memplex.core.engine import CoreEngine
from memplex.models import FieldValue, Function, SourceDocument
from memplex.models.paragraph import persisted_paragraph_id
from memplex.service import MemplexService

TEXT = "**Cost:**\n\nOne price.\n\n**Cost:**\n\nAnother price."
HEADING_ID = f"func_{sha256(b'**Cost:**').hexdigest()[:16]}"


def test_repeated_heading_has_one_stable_function_and_every_raw_occurrence():
    extracted = CoreEngine().extract(SourceDocument(type="text", content=TEXT))
    ids = [node.id for node in extracted.functions]
    assert len(ids) == len(set(ids)) == 3
    heading, = [node for node in extracted.functions if node.id == HEADING_ID]
    assert heading.source_paragraphs == [
        persisted_paragraph_id("text", "para_001", "**Cost:**"),
        persisted_paragraph_id("text", "para_003", "**Cost:**"),
    ]
    assert [paragraph.raw_text for paragraph in extracted.paragraphs] == TEXT.split("\n\n")
    assert [node.id for node in extracted.graph.nodes] == ids


def test_repeated_heading_retains_each_field_value_source():
    extracted = CoreEngine().extract(SourceDocument(type="text", content=TEXT))
    heading = next(node for node in extracted.functions if node.id == HEADING_ID)
    value, = heading.action
    assert value.desc == "**Cost:**"
    assert value.sources == ["text:para_001", "text:para_003"]
    assert value.source_method == "rule_based"


def test_repeated_content_across_batch_coalesces_identity_and_sources():
    extracted = CoreEngine().extract_batch([
        SourceDocument(type="text", content="**Cost:**"),
        SourceDocument(type="text", content="Introduction.\n\n**Cost:**"),
    ])
    headings = [node for node in extracted.functions if node.id == HEADING_ID]
    assert len(headings) == 1
    assert headings[0].action[0].sources == ["text:para_001", "text:para_002"]
    assert len(headings[0].source_paragraphs) == 2


def test_repeated_conflicting_occurrences_keep_both_distinct_meanings():
    alpha = "## Access\nIf code alpha, execute sync."
    beta = "## Access\nIf code beta, execute sync."
    extracted = CoreEngine().extract(SourceDocument(
        type="text", content=f"{alpha}\n\n{beta}\n\n{alpha}",
    ))
    assert len(extracted.functions) == 2
    assert len({node.id for node in extracted.functions}) == 2
    assert all(node.needs_review for node in extracted.functions)
    repeated = next(node for node in extracted.functions if "alpha" in node.condition[0].desc)
    assert len(repeated.source_paragraphs) == 2
    assert repeated.condition[0].sources == ["text:para_001", "text:para_003"]


def test_exact_duplicates_do_not_duplicate_fuzzy_merge_canonical():
    first = "## Flow\nExecute step one."
    second = "## Flow\nExecute step two."
    extracted = CoreEngine().extract(SourceDocument(
        type="text", content=f"{first}\n\n{second}\n\n{first}",
    ))
    node, = extracted.functions
    assert node.id == f"func_{sha256(first.encode()).hexdigest()[:16]}"
    assert len(node.source_paragraphs) == 3
    value = next(value for value in node.action if value.desc == "Execute step one.")
    assert value.sources == ["text:para_001", "text:para_003"]


@pytest.mark.parametrize("content_hash", [None, "", "different", "a" * 64])
def test_duplicate_id_without_matching_full_hash_fails_closed(content_hash):
    original = Function(id=HEADING_ID, name="Cost", content_hash=sha256(b"**Cost:**").hexdigest())
    collision = Function(id=HEADING_ID, name="Cost", content_hash=content_hash)
    with pytest.raises(ValueError, match="duplicate Function id"):
        CoreEngine()._deduplicate_functions([original, collision])


def test_equal_truncated_hash_is_not_sufficient_identity_evidence():
    nodes = [Function(id=HEADING_ID, name="Cost", content_hash="65fe3d8e73e06ae9") for _ in range(2)]
    with pytest.raises(ValueError, match="duplicate Function id"):
        CoreEngine()._deduplicate_functions(nodes)


def test_late_collision_rejects_batch_before_combining_any_evidence():
    digest = sha256(b"**Cost:**").hexdigest()
    first = Function(id=HEADING_ID, name="Cost", content_hash=digest, source_paragraphs=["p1"])
    repeat = Function(id=HEADING_ID, name="Cost", content_hash=digest, source_paragraphs=["p2"])
    collision = Function(id=HEADING_ID, name="Cost", content_hash="a" * 64)
    with pytest.raises(ValueError, match="duplicate Function id"):
        CoreEngine()._deduplicate_functions([first, repeat, collision])
    assert first.source_paragraphs == ["p1"]


@pytest.mark.parametrize("role", ["trigger", "condition", "action", "benefit"])
def test_repeated_field_values_union_sources_without_changing_metadata(role):
    digest = sha256(b"**Cost:**").hexdigest()
    first = Function(id=HEADING_ID, name="First section", content_hash=digest)
    repeat = Function(id=HEADING_ID, name="Second section", content_hash=digest, needs_review=True)
    setattr(first, role, [FieldValue("Cost", sources=["first"], source_method="rule_based", weight=0.7)])
    setattr(repeat, role, [FieldValue("Cost", sources=["second", "first"], source_method="rule_based", weight=0.7)])
    node, = CoreEngine()._deduplicate_functions([first, repeat])
    value, = getattr(node, role)
    assert node.name == "First section" and node.needs_review
    assert value.sources == ["first", "second"]
    assert (value.source_method, value.weight, value.status) == ("rule_based", 0.7, "active")


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_second_occurrence_derived_fact_tracks_coalesced_function(tmp_path, mutation):
    from tests.test_factual_capture import service as factual_service

    text = "Atlas routing uses AMBER-7382."

    def payload(prompt):
        evidence = prompt["evidence"][2]
        assert evidence["text"] == text
        return {"facts": [{
            "subject": "Atlas routing", "predicate": "uses", "object": "AMBER-7382",
            "evidence": [{"paragraph_id": evidence["paragraph_id"], "quote": text}],
        }]}

    service = factual_service(tmp_path, payload)
    try:
        result = service.write(SourceDocument(type="text", content=f"{text}\n\nOverview.\n\n{text}"))
        assert result.factual_capture["status"] == "success"
        derived, = result.facts
        original, = [node for node in result.functions if node.name == text]
        assert len(original.source_paragraphs) == 2
        assert derived.source_paragraphs == [original.source_paragraphs[1]]
        assert set(derived.namespace["memplex_source_refs"].split(",")) == {
            original.id, original.source_paragraphs[1],
        }
        assert service.get(derived.id) is not None
        if mutation == "edit":
            assert service.update_memory(original.id, "action", "Atlas routing uses BLUE-92.").success
        else:
            service.delete(original.id)
        assert service.get(derived.id) is None
    finally:
        service.stop()


def _context(**scope):
    return AuthorizationContext(
        Principal(tenant_id="duplicate-team", subject_id="alice"),
        **({"workspace_id": "project", "agent_id": "codex", "session_id": "s1"} | scope),
    )


@pytest.mark.parametrize("factual_capture", [False, True])
def test_public_write_replay_restart_edit_delete_keep_identity_contract(
    tmp_path, factual_capture,
):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    config.embedding.model = "tfidf"
    config.embedding.hyde_enabled = False
    config.llm.provider = "rule-based"
    config.llm.fallback_chain = ["rule-based"]
    config.llm.query_enhancement = False
    config.llm.factual_capture = factual_capture
    config.wiki.enabled = False
    config.sleep_time.enabled = False
    service = MemplexService(config=config)
    source = SourceDocument(type="text", content=TEXT, author_role="user")
    context = _context()
    try:
        first = service.write(source, authorization=context, visibility="session")
        second = service.write(source, authorization=context, visibility="session")
        assert source.content == TEXT
        assert [node.id for node in first.functions] == [node.id for node in second.functions]
        assert len(service.store.list_functions()) == 3
        heading, = [node for node in first.functions if node.name == "**Cost:**"]
        before = service.get(heading.id, authorization=context)
        assert before is not None and len(before.source_paragraphs) == 2
        assert before.action[0].sources == heading.action[0].sources
        service.stop()
        service = MemplexService(config=config)
        reloaded = service.get(heading.id, authorization=context)
        assert reloaded is not None
        assert reloaded.source_paragraphs == before.source_paragraphs
        assert reloaded.action[0].sources == before.action[0].sources
        hidden_contexts = [
            _context(session_id="other"), _context(workspace_id="other"),
            replace(context, principal=Principal(tenant_id="other", subject_id="alice")),
            replace(context, principal=Principal(tenant_id="duplicate-team", subject_id="bob")),
        ]
        for hidden in hidden_contexts:
            assert service.get(heading.id, authorization=hidden) is None
            with pytest.raises(MemoryNotFoundError):
                service.update_memory(heading.id, "action", "tampered", authorization=hidden)
            with pytest.raises(MemoryNotFoundError):
                service.delete(heading.id, authorization=hidden)
        assert service.update_memory(heading.id, "action", "Updated cost.", authorization=context).success
        updated = service.get(heading.id, authorization=context)
        assert updated.id == before.id
        assert updated.source_paragraphs == before.source_paragraphs
        assert updated.provenance == before.provenance
        assert [(value.desc, value.status) for value in updated.action] == [
            ("Updated cost.", "active"), ("**Cost:**", "deprecated"),
        ]
        assert updated.action[1].sources == before.action[0].sources
        service.delete(heading.id, authorization=context)
        assert service.get(heading.id, authorization=context) is None
        assert len(service.store.list_functions()) == 2
    finally:
        service.stop()
