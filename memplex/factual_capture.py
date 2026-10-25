"""Materialize factual candidates using only committed original evidence.

No PostgreSQL schema changes: existing raw rows and typed derivation lineage retain
ownership of deletion, revocation, restart and read-time authorization.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from memplex.auth import AuthorizationContext, bind_node_identity
from memplex.authorization import AuthorizationGate, _RawParagraphView
from memplex.capture_identity import capture_source_hint, scope_captured_data
from memplex.config import LLMConfig
from memplex.factual_lineage import source_fingerprint, source_is_current
from memplex.llm.factual_capture import (
    Evidence,
    FactCandidate,
    FactualCaptureResult,
)
from memplex.models import ExtractedData, Fact, SourceDocument
from memplex.models.paragraph import persisted_paragraph_id


def scope_evidence(
    extracted: ExtractedData, source: SourceDocument, context: AuthorizationContext, *, visibility: str,
) -> SourceDocument:
    """Rekey raw references only after file/URL/clipboard acquisition completes."""
    scoped = replace(source, type=capture_source_hint(context) + "/" + source.type
                     + "/" + (source.author_role or "unknown") + "/" + visibility)
    mapping = {}
    for paragraph in extracted.paragraphs:
        text = (paragraph.raw_text or "").strip()
        mapping[persisted_paragraph_id(source.type, paragraph.id, text)] = persisted_paragraph_id(
            scoped.type, paragraph.id, text,
        )
    # The same words from two speakers must not upsert the same typed row.
    # Use the evidence scope as input identity before ordinary capture rekeying.
    nodes = {id(n): n for n in [*extracted.functions, *extracted.facts,
                               *extracted.preferences, *extracted.graph.nodes]}
    identities = {node.id: node.memory_type + "_evidence_" + hashlib.sha256(
        (scoped.type + "\0" + node.id).encode()).hexdigest()[:32] for node in nodes.values()}
    scope_digest = hashlib.sha256(scoped.type.encode()).hexdigest()[:16]
    for node in nodes.values():
        node.source_paragraphs = [mapping.get(value, value) for value in node.source_paragraphs]
        node.id = identities[node.id]
        if node.memory_type == "function":
            node.name_normalized = f"evidence:{scope_digest}:{node.name_normalized or node.name}"
    for edge in extracted.graph.edges:
        edge.source = identities.get(edge.source, edge.source)
        edge.target = identities.get(edge.target, edge.target)
    return scoped


def _evidence(extracted: ExtractedData, source: SourceDocument, limit: int) -> tuple[Evidence, ...]:
    result = []
    remaining = limit
    for paragraph in extracted.paragraphs:
        from memplex.privacy import strip_private_tags

        raw = (paragraph.raw_text or "").strip()
        text = strip_private_tags(raw)
        if not text or len(text) > remaining:
            continue  # never truncate an evidence unit into a fabricated source
        paragraph_id = persisted_paragraph_id(source.type, paragraph.id, raw)
        result.append(Evidence(paragraph_id, text))
        remaining -= len(text)
    return tuple(result)


def _sources(
    candidate: FactCandidate, extracted: ExtractedData, store: Any,
    authorization: AuthorizationGate, context: AuthorizationContext,
) -> list[Any]:
    raw_ids = tuple(dict.fromkeys(c.paragraph_id for c in candidate.evidence))
    typed_ids = tuple(n.id for n in [*extracted.functions, *extracted.facts, *extracted.preferences]
                      if set(n.source_paragraphs) & set(raw_ids))
    reader = getattr(store, "read_context_nodes", None)
    if not callable(reader):
        return []
    rows = reader((*raw_ids, *typed_ids))
    sources = []
    for source_id in (*raw_ids, *typed_ids):
        row = rows.get(source_id)
        node = _RawParagraphView(row) if isinstance(row, dict) else row
        if node is None or node.id != source_id or not authorization.is_node_visible(node, context):
            return []
        if source_id in raw_ids:
            for citation in candidate.evidence:
                if citation.paragraph_id == source_id and citation.quote not in getattr(node, "raw_text", ""):
                    return []
        if not source_is_current(node):
            return []
        sources.append(node)
    return sources


def _materialize(
    candidate: FactCandidate, sources: list[Any], source: SourceDocument,
    context: AuthorizationContext, authorization: AuthorizationGate, visibility: str,
) -> Fact:
    content = json.dumps({
        "subject": candidate.subject, "predicate": candidate.predicate, "object": candidate.object,
        "evidence": [(c.paragraph_id, c.quote) for c in candidate.evidence],
        "valid_from": candidate.valid_from,
    }, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(content.encode()).hexdigest()
    now = datetime.now(UTC).isoformat()
    node = Fact(
        id="fact_derived_" + digest[:32], name=f"{candidate.subject} {candidate.predicate} {candidate.object}",
        subject=candidate.subject, predicate=candidate.predicate, object_=candidate.object,
        source_paragraphs=list(dict.fromkeys(c.paragraph_id for c in candidate.evidence)),
        source_type=source.source_type, content_hash=digest, created_at=now, updated_at=now,
        valid_from=candidate.valid_from, trust_tier=1,
    )
    bind_node_identity(node, context, visibility=visibility)
    authorization.bind_derivation_lineage(node, sources)
    node.provenance.update({
        "extraction": "factual_capture_v1", "authority": "agent_inferred",
        "author_role": source.author_role or "unknown",
        "source_snapshots": json.dumps({n.id: source_fingerprint(n) for n in sources}, sort_keys=True),
        "evidence": json.dumps([{"paragraph_id": c.paragraph_id, "quote": c.quote}
                                for c in candidate.evidence], ensure_ascii=False),
    })
    if source.reference_datetime is not None:
        node.provenance["reference_datetime"] = source.reference_datetime.isoformat()
    scope_captured_data(ExtractedData(facts=[node]))
    return node


def capture_derived_facts(
    extracted: ExtractedData, source: SourceDocument, context: AuthorizationContext, *,
    store: Any, authorization: AuthorizationGate, enhancer: Any, config: LLMConfig, visibility: str,
) -> tuple[list[Fact], dict[str, Any]]:
    """Fail closed to an operational receipt; raw/rule writes remain intact."""
    if enhancer is None:
        return [], FactualCaptureResult("unavailable").receipt()
    evidence = _evidence(extracted, source, config.max_input_length)
    if not evidence:
        return [], FactualCaptureResult("unavailable").receipt()
    outcome = enhancer.factualize_sync(
        evidence, reference_datetime=source.reference_datetime, author_role=source.author_role,
    )
    derived = []
    try:
        for candidate in outcome.candidates:
            sources = _sources(candidate, extracted, store, authorization, context)
            if not sources:
                return [], {**outcome.receipt(), "status": "source_unavailable"}
            derived.append(_materialize(candidate, sources, source, context, authorization, visibility))
    except Exception:  # noqa: BLE001 - missing/malformed source never grants authority
        return [], {**outcome.receipt(), "status": "source_unavailable"}
    return derived, outcome.receipt()
