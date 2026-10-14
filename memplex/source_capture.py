"""Committed capture inputs and publication requests; no model or bootstrap authority."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from memplex.auth import AuthorizationContext, bind_node_identity
from memplex.authorization import is_own_node_visible, source_authorization_node
from memplex.config import LLMConfig
from memplex.factual_lineage import source_fingerprint
from memplex.llm.factual_capture import Evidence, FactCandidate
from memplex.llm.injection_guard import IndirectInjectionGuard
from memplex.models import ExtractedData, Fact, SourceDocument, SourceType
from memplex.models.paragraph import persisted_paragraph_id
from memplex.privacy import strip_private_tags
from memplex.source_codec import _canonical, _decode_value, output_digest
from memplex.source_contract import (
    ALGORITHM_VERSION,
    PUBLICATION_PREFIX,
    CaptureManifest,
    DerivationEnvelope,
    ExpectedAbsent,
    PublishCommand,
    RejectionCode,
    SourceClass,
    SourceContractError,
    SourceKey,
    SourceKind,
    SourceMutation,
    SourceOperation,
    SourceReceipt,
    SourceSnapshot,
    SourceState,
    TargetToken,
    ValidationLimits,
)
from memplex.source_validation import finalize_at, validate_sources
from memplex.storage.source_versions import SourceReadSession, SourceVersionStore


def _require(condition: bool, code: RejectionCode = RejectionCode.MALFORMED) -> None:
    if not condition:
        raise SourceContractError(code)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _wire(value: object) -> Any:
    return json.loads(_canonical(value))


def capture_deadline() -> int:
    return time.monotonic_ns() + 250_000_000


def capture_source_safe(snapshot: SourceSnapshot) -> bool:
    return snapshot.payload.get("unsafe") is not True and not IndirectInjectionGuard.is_suspected(source_authorization_node(snapshot))


def capture_source_acl(snapshot: SourceSnapshot, authorization: AuthorizationContext) -> bool:
    return is_own_node_visible(source_authorization_node(snapshot), authorization)


@dataclass(frozen=True, slots=True)
class FrozenCaptureInput:
    manifest: CaptureManifest
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class _PreparedCapture:
    raw: tuple[tuple[str, dict[str, object]], ...]
    typed: tuple[Any, ...]
    parameters: Mapping[str, str]


def prepare_capture_sources(extracted: ExtractedData, source: SourceDocument, *,
                            authorization: AuthorizationContext, visibility: str,
                            trust_tier: int, config: LLMConfig) -> _PreparedCapture:
    """Prepare new identities in memory. No value here is a commit receipt."""
    capture_id = str(uuid.uuid4())
    parameters = {
        "capture_id": capture_id, "author_role": source.author_role or "unknown",
        "source_type": source.source_type.value, "source_hint": source.type,
        "algorithm_version": ALGORITHM_VERSION,
        "model_configuration_digest": _digest({name: getattr(config, name, None) for name in (
            "provider", "model", "max_input_length", "factual_capture_max_facts", "factual_capture_timeout_seconds",
        )}),
    }
    if source.reference_datetime is not None:
        parameters["reference_datetime"] = source.reference_datetime.isoformat()
    receipt: dict[str, object] = {"contract_version": 1, "capture_id": capture_id, "complete": False,
                                "acknowledgement": "incomplete", "phase": "preparation", "sources": [],
                                "publications": [], "input_parameters": dict(parameters), "expected_sources": 0}
    extracted.source_contract_receipt = receipt
    _require(os.environ.get("MEMPLEX_RAW_PARAGRAPH_LAYER", "1") not in {"0", "false", "False"},
             RejectionCode.UNSUPPORTED_CAPABILITY)
    try:
        cap = int(os.environ.get("MEMPLEX_TRUST_TIER2_CAP", "0"))
    except ValueError:
        cap = 0
    _require(cap <= 0, RejectionCode.UNSUPPORTED_CAPABILITY)
    raw: list[tuple[str, dict[str, object]]] = []
    mapping: dict[str, str] = {}
    identity = bind_node_identity(Fact(), authorization, visibility=visibility).to_dict()
    for paragraph in extracted.paragraphs:
        _require(type(paragraph.id) is str and paragraph.id not in mapping)
        original = paragraph.raw_text
        _require(type(original) is str)
        text = strip_private_tags(original)
        _require(bool(text.strip()), RejectionCode.MISSING)
        handle = "raw_source_v1_" + uuid.uuid4().hex
        # The extractor's old reference is mapped exactly once before commit.
        # All subsequent work uses this explicit map and actual backend receipts.
        mapping[paragraph.id] = handle
        mapping[persisted_paragraph_id(source.type, paragraph.id, original.strip())] = handle
        payload = {name: identity[name] for name in (
            "tenant_id", "owner_subject_id", "owner", "workspace_id", "visibility", "origin_session", "namespace", "provenance",
        )}
        payload.update(id=handle, raw_text=text, trust_tier=trust_tier,
                       created_at=datetime.now(UTC).isoformat(), source=paragraph.source,
                       observation_count=1, last_observed_at=datetime.now(UTC).isoformat())
        raw.append((paragraph.id, payload))
    typed = (*extracted.facts, *extracted.preferences, *extracted.functions)
    _require(bool(raw), RejectionCode.MISSING)
    _require(len(raw) + len(typed) <= 64, RejectionCode.BUDGET_EXHAUSTED)
    _require(sum(len(cast(str, row["raw_text"])) for _, row in raw) <= config.max_input_length,
             RejectionCode.BUDGET_EXHAUSTED)
    for node in typed:
        _require(node.memory_type in {"fact", "preference", "function"})
        _require(bool(node.source_paragraphs) and all(ref in mapping for ref in node.source_paragraphs),
                 RejectionCode.INVALID_UPSTREAM)
        node.id = node.memory_type + "_capture_v1_" + uuid.uuid4().hex
        node.source_paragraphs = list(dict.fromkeys(mapping[ref] for ref in node.source_paragraphs))
        bind_node_identity(node, authorization, visibility=visibility)
        node.provenance.update(capture_input="factual_capture_v1", author_role=parameters["author_role"])
    extracted.graph.edges = []
    receipt["expected_sources"] = len(raw) + len(typed)
    return _PreparedCapture(tuple(raw), typed, parameters)


def persist_capture_sources(extracted: ExtractedData, prepared: _PreparedCapture, *,
                            backend: SourceVersionStore, authorization: AuthorizationContext) -> bool:
    """Acknowledge each real raw, typed, then Function commit; never fake rollback."""
    receipt = extracted.source_contract_receipt
    assert receipt is not None
    acknowledged = cast(list[dict[str, object]], receipt["sources"])
    raw_refs = {}
    try:
        with backend.read_fence(authorization, deadline_ns=capture_deadline()) as session:
            scope = session.authority().scope
        inputs = [(SourceKind.RAW_PARAGRAPH, original, payload, ()) for original, payload in prepared.raw]
        for kind, original, payload, raw_roots in inputs:
            receipt["phase"] = "raw"
            key = SourceKey(scope.authority_id, scope.tenant_id, scope.workspace_id, kind, cast(str, payload["id"]))
            command = SourceMutation(key, ExpectedAbsent.ABSENT, SourceOperation.CREATE, payload,
                                     SourceClass.CAPTURED_INPUT, prepared.parameters, raw_roots)
            committed = backend.mutate_source(authorization, command, deadline_ns=capture_deadline())
            _require(type(committed) is SourceReceipt and committed.state is SourceState.LIVE)
            acknowledged.append({"input_id": original, "payload_id": payload["id"], "receipt": _wire(committed)})
            raw_refs[key.logical_id] = committed.ref
        for node in prepared.typed:
            receipt["phase"] = "function" if node.memory_type == "function" else "typed"
            key = SourceKey(scope.authority_id, scope.tenant_id, scope.workspace_id, SourceKind(node.memory_type), node.id)
            roots = tuple(raw_refs[ref] for ref in node.source_paragraphs)
            command = SourceMutation(key, ExpectedAbsent.ABSENT, SourceOperation.CREATE, node.to_dict(),
                                     SourceClass.CAPTURED_INPUT, prepared.parameters, roots)
            committed = backend.mutate_source(authorization, command, deadline_ns=capture_deadline())
            _require(type(committed) is SourceReceipt and committed.state is SourceState.LIVE)
            acknowledged.append({"input_id": node.id, "payload_id": node.id, "receipt": _wire(committed)})
    except Exception:  # noqa: BLE001 - a lost reply cannot prove either failure or a successful receipt
        receipt["acknowledgement"] = "unknown"
        return False
    receipt.update(complete=True, acknowledgement="complete", phase="sources_committed")
    return True


def freeze_capture_inputs(extracted: ExtractedData, source: SourceDocument, *, session: SourceReadSession,
                          authorization: AuthorizationContext, algorithm_version: str,
                          input_limit: int) -> FrozenCaptureInput:
    """Fix exact committed evidence and its complete closure within the caller's fence."""
    receipt = extracted.source_contract_receipt
    _require(type(receipt) is dict and receipt.get("complete") is True, RejectionCode.MISSING)
    assert receipt is not None
    entries = receipt.get("sources")
    parameters = receipt.get("input_parameters")
    _require(type(entries) is list and bool(entries) and len(entries) == receipt.get("expected_sources"))
    _require(type(parameters) is dict and algorithm_version == ALGORITHM_VERSION)
    assert isinstance(entries, list) and isinstance(parameters, dict)
    _require(parameters.get("author_role") == (source.author_role or "unknown")
             and parameters.get("reference_datetime") == (source.reference_datetime.isoformat() if source.reference_datetime else None),
             RejectionCode.SOURCE_CHANGED)
    authority = session.authority()
    _require(algorithm_version in authority.policy.algorithm_versions, RejectionCode.UNSUPPORTED_CAPABILITY)
    raw_ids: list[str] = []
    expected = []
    for entry in entries:
        _require(type(entry) is dict and set(entry) == {"input_id", "payload_id", "receipt"})
        committed = cast(SourceReceipt, _decode_value(SourceReceipt, entry["receipt"]))
        current = session.source(committed.ref.key)
        _require(committed.state is SourceState.LIVE and current is not None, RejectionCode.MISSING)
        assert current is not None
        _require(current.head.ref == committed.ref and current.head.last_commit_seq == committed.commit_seq,
                 RejectionCode.SOURCE_CHANGED)
        _require(current.head.source_class is SourceClass.CAPTURED_INPUT
                 and dict(current.head.input_context) == parameters
                 and parameters.get("capture_id") == receipt.get("capture_id"), RejectionCode.INVALID_UPSTREAM)
        _require(json.loads(current.head.payload_locator)["id"] == entry["payload_id"], RejectionCode.MALFORMED)
        expected.append(committed.ref)
        if committed.ref.key.kind is SourceKind.RAW_PARAGRAPH:
            raw_ids.append(committed.ref.key.logical_id)
    _require(len({ref.key for ref in expected}) == len(expected))
    result = validate_sources(session, [ref.key.logical_id for ref in expected], authorization=authorization,
                              own_acl=capture_source_acl, safe_for_model=capture_source_safe,
                              limits=ValidationLimits(), deadline_ns=session.monotonic_ns() + 250_000_000,
                              allow_captured_inputs=True)
    _require(not result.rejected, next(iter(result.rejected.values()), RejectionCode.MISSING))
    refs = tuple(sorted((snapshot.head.ref for snapshot in result.sources.values()), key=lambda ref: _canonical(ref.key)))
    _require(bool(raw_ids) and len(refs) <= 64, RejectionCode.BUDGET_EXHAUSTED)
    by_id = {snapshot.head.ref.key.logical_id: snapshot for snapshot in result.sources.values()}
    evidence = tuple(Evidence(memory_id, cast(str, by_id[memory_id].payload["raw_text"])) for memory_id in raw_ids)
    _require(type(input_limit) is int and input_limit > 0
             and sum(len(item.text) for item in evidence) <= input_limit, RejectionCode.BUDGET_EXHAUSTED)
    finalized = finalize_at(result, at=session.utc_now())
    _require(len(finalized.accepted) == len(expected), RejectionCode.EXPIRED)
    fixed_parameters = {**parameters, "source_snapshots": _canonical({
        memory_id: source_fingerprint(source_authorization_node(snapshot)) for memory_id, snapshot in by_id.items()
    })}
    manifest = CaptureManifest(cast(str, receipt["capture_id"]), authority.scope.authority_id,
                               authority.policy.revision, authority.commit_seq, refs, fixed_parameters,
                               _digest({"evidence": evidence, "parameters": fixed_parameters}), algorithm_version)
    return FrozenCaptureInput(manifest, evidence)


def prepare_publication(candidate: FactCandidate, manifest: CaptureManifest, *,
                        expected_target: TargetToken | ExpectedAbsent, authorization: AuthorizationContext,
                        visibility: str, request_id: str) -> PublishCommand:
    """Bind a validated low-trust candidate to every frozen input, never its own subset."""
    _require(type(candidate) is FactCandidate and bool(candidate.evidence))
    raw_ids = {ref.key.logical_id for ref in manifest.input_refs if ref.key.kind is SourceKind.RAW_PARAGRAPH}
    _require(all(citation.paragraph_id in raw_ids for citation in candidate.evidence), RejectionCode.MISSING)
    first = manifest.input_refs[0].key
    _require((first.tenant_id, first.workspace_id) == (authorization.principal.tenant_id, authorization.workspace_id),
             RejectionCode.DENIED)
    key = _digest({"scope": [first.authority_id, first.tenant_id, first.workspace_id], "producer": "factual_capture",
                   "claim": [" ".join(value.casefold().split()) for value in (candidate.subject, candidate.predicate, candidate.object)],
                   "sources": tuple(ref.key for ref in manifest.input_refs)})
    publication_id = PUBLICATION_PREFIX + uuid.uuid4().hex
    node = Fact(id=publication_id, name=f"{candidate.subject} {candidate.predicate} {candidate.object}",
                subject=candidate.subject, predicate=candidate.predicate, object_=candidate.object,
                source_type=SourceType(manifest.input_parameters["source_type"]),
                source_paragraphs=list(dict.fromkeys(c.paragraph_id for c in candidate.evidence)),
                valid_from=candidate.valid_from, trust_tier=1)
    bind_node_identity(node, authorization, visibility=visibility)
    node.namespace.update(memplex_derivation="v1", memplex_source_refs=",".join(ref.key.logical_id for ref in manifest.input_refs))
    node.provenance.update(extraction="factual_capture_v1", authority="agent_inferred",
                           author_role=manifest.input_parameters["author_role"],
                           source_snapshots=manifest.input_parameters["source_snapshots"],
                           evidence=_canonical([{"paragraph_id": c.paragraph_id, "quote": c.quote} for c in candidate.evidence]))
    if manifest.input_parameters.get("reference_datetime"):
        node.provenance["reference_datetime"] = manifest.input_parameters["reference_datetime"]
    _require(not IndirectInjectionGuard.is_suspected(node), RejectionCode.UNSAFE)
    output = node.to_dict()
    envelope = DerivationEnvelope(1, "factual_capture", manifest.algorithm_version, key, publication_id,
                                  str(uuid.uuid4()), 1, manifest.authority_id, manifest.policy_revision,
                                  manifest.input_refs, (), manifest.input_refs, output_digest(output), 1)
    return PublishCommand(request_id, key, expected_target, manifest, envelope, output)
