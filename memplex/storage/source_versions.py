"""Internal source-version protocols and resource-owned, non-wire capabilities.

The issuer is deliberately private. Resource wiring is a later task; no public
constructor, namespace, HTTP claim, wrapper, or decoded ledger grants a seal.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, NoReturn, Protocol

from memplex.auth import AuthorizationContext
from memplex.source_contract import (
    CARRIER_KEY,
    MAX_REVISION,
    PUBLICATION_PREFIX,
    AuthorityScope,
    AuthorityState,
    ExpectedAbsent,
    PolicySnapshot,
    PublicationReceipt,
    PublicationRecord,
    PublicationState,
    PublishCommand,
    RejectionCode,
    SourceClass,
    SourceContractError,
    SourceHead,
    SourceIntegrityError,
    SourceKey,
    SourceKind,
    SourceMutation,
    SourceOperation,
    SourceReceipt,
    SourceRef,
    SourceSnapshot,
    SourceState,
    SourceToken,
    TargetHead,
    TargetToken,
    ValidationLimits,
    ValidationResult,
)


class SourceReadSession(Protocol):
    """A detached source view valid only while its backend fence is held."""

    def authority(self) -> AuthorityState: ...
    def resolve_id(self, memory_id: str) -> SourceKey | None: ...
    def source(self, key: SourceKey) -> SourceSnapshot | None: ...
    def target(self, derivation_key: str) -> TargetHead | None: ...
    def publication(self, publication_id: str) -> PublicationRecord | None: ...
    def utc_now(self) -> datetime: ...
    def monotonic_ns(self) -> int: ...


class SourceVersionStore(Protocol):
    def replace_policy(self, expected_revision: int, policy: PolicySnapshot,
                       authority: PolicyUpdateAuthority) -> PolicySnapshot: ...
    def initialize_authority(self, scope: AuthorityScope, policy: PolicySnapshot, *,
                             admission: SourceAdmission) -> AuthorityState: ...
    def read_fence(self, authorization: AuthorizationContext, *,
                   deadline_ns: int) -> AbstractContextManager[SourceReadSession]: ...
    def mutate_source(self, authorization: AuthorizationContext, command: SourceMutation, *,
                      deadline_ns: int) -> SourceReceipt: ...
    def publish_if_current(self, authorization: AuthorizationContext, command: PublishCommand, *,
                           deadline_ns: int) -> PublicationReceipt: ...
    def delete_target(self, authorization: AuthorizationContext, derivation_key: str,
                      expected: TargetToken, *, deadline_ns: int) -> TargetHead: ...
    def recreate_target(self, authorization: AuthorizationContext, command: PublishCommand,
                        expected_deleted: TargetToken, *, deadline_ns: int) -> PublicationReceipt: ...


@dataclass(frozen=True, slots=True)
class SourceValidationHooks:
    own_acl: Callable[[SourceSnapshot, AuthorizationContext], bool]
    safe_for_model: Callable[[SourceSnapshot], bool]

    def __post_init__(self) -> None:
        if not callable(self.own_acl) or not callable(self.safe_for_model):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)


_RESOURCE_ISSUER = object()


class _ResourceSeal:
    __slots__ = ("_issuer", "_resource")
    _issuer: object
    _resource: object

    def __init__(self, *, issuer: object = None, resource: object = None) -> None:
        if issuer is not _RESOURCE_ISSUER or resource is None:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_resource", resource)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("source capability is read-only")

    def __reduce__(self) -> NoReturn:
        raise TypeError("source capability cannot be serialized")


class SourceAdmission(_ResourceSeal):
    __slots__ = ("_hooks", "_scope")
    _scope: AuthorityScope
    _hooks: SourceValidationHooks

    def __init__(self, *, issuer: object = None, resource: object = None,
                 scope: AuthorityScope | None = None,
                 hooks: SourceValidationHooks | None = None) -> None:
        super().__init__(issuer=issuer, resource=resource)
        if type(scope) is not AuthorityScope or type(hooks) is not SourceValidationHooks:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_hooks", hooks)


class PolicyUpdateAuthority(_ResourceSeal):
    __slots__ = ("_context",)
    _context: AuthorizationContext

    def __init__(self, *, issuer: object = None, resource: object = None,
                 context: AuthorizationContext | None = None) -> None:
        super().__init__(issuer=issuer, resource=resource)
        if type(context) is not AuthorizationContext:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "_context", context)


def _issue_policy_authority(resource: object, context: AuthorizationContext, *,
                            issuer: object) -> PolicyUpdateAuthority:
    return PolicyUpdateAuthority(issuer=issuer, resource=resource, context=context)


def _check_policy_update(authority: PolicyUpdateAuthority, resource: object,
                         expected_revision: int, policy: PolicySnapshot) -> None:
    if (type(authority) is not PolicyUpdateAuthority or authority._issuer is not _RESOURCE_ISSUER
            or authority._resource is not resource):
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
    if (type(expected_revision) is not int or not 1 <= expected_revision < MAX_REVISION
            or type(policy) is not PolicySnapshot or policy.revision != expected_revision + 1):
        raise SourceContractError(RejectionCode.AUTH_CHANGED)


_SOURCE_WRITE: ContextVar[tuple[object, str] | None] = ContextVar("source_write", default=None)


@contextmanager
def _source_write_scope(resource: object, *, issuer: object, access_only: bool = False) -> Iterator[None]:
    """Non-wire, resource-local write admission owned by reviewed internal paths."""
    if issuer is not _RESOURCE_ISSUER:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
    token = _SOURCE_WRITE.set((resource, "access" if access_only else "source"))
    try:
        yield
    finally:
        _SOURCE_WRITE.reset(token)


def _source_write_mode(resource: object) -> str | None:
    active = _SOURCE_WRITE.get()
    return active[1] if active is not None and active[0] is resource else None



class SourceCapability(_ResourceSeal):
    __slots__ = ("_backend", "_contract_version", "_scope", "_state")
    _backend: Literal["lite_json", "postgres"]
    _scope: AuthorityScope
    _contract_version: int
    _state: SourceState

    def __init__(self, *, issuer: object, resource: object,
                 backend: Literal["lite_json", "postgres"], scope: AuthorityScope,
                 contract_version: int, state: SourceState) -> None:
        super().__init__(issuer=issuer, resource=resource)
        if backend not in {"lite_json", "postgres"} or type(scope) is not AuthorityScope:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        if type(contract_version) is not int or contract_version != 1 or type(state) is not SourceState:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_contract_version", contract_version)
        object.__setattr__(self, "_state", state)

    @property
    def backend(self) -> Literal["lite_json", "postgres"]:
        return self._backend

    @property
    def scope(self) -> AuthorityScope:
        return self._scope

    @property
    def contract_version(self) -> int:
        return self._contract_version

    @property
    def state(self) -> SourceState:
        return self._state


def _issue_admission(resource: object, scope: AuthorityScope, hooks: SourceValidationHooks, *,
                     issuer: object) -> SourceAdmission:
    return SourceAdmission(issuer=issuer, resource=resource, scope=scope, hooks=hooks)


def _check_admission(admission: SourceAdmission, resource: object, scope: AuthorityScope) -> None:
    if (type(admission) is not SourceAdmission
            or admission._issuer is not _RESOURCE_ISSUER
            or admission._resource is not resource or admission._scope != scope
            or type(admission._hooks) is not SourceValidationHooks):
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)


def _raw_retirements(session: SourceReadSession, current: SourceSnapshot | None,
                     command: SourceMutation) -> tuple[SourceMutation, ...]:
    """Carry typed historical/superseded effects into raw heads in one decision."""
    if current is None or command.key.kind is SourceKind.RAW_PARAGRAPH or command.payload is None:
        return ()
    attributes = command.payload.get("attributes", {})
    before = current.payload.get("attributes", {})
    namespace = command.payload.get("namespace", {})
    old_namespace = current.payload.get("namespace", {})
    role_edit = (isinstance(attributes, Mapping) and isinstance(before, Mapping)
                 and bool(attributes.get("memplex_role_updates"))
                 and attributes.get("memplex_role_updates") != before.get("memplex_role_updates"))
    superseded = (isinstance(namespace, Mapping) and isinstance(old_namespace, Mapping)
                  and bool(namespace.get("premise_superseded"))
                  and namespace.get("premise_superseded") != old_namespace.get("premise_superseded"))
    if not (role_edit or superseded):
        return ()
    values = current.payload.get("source_paragraphs", ())
    if not isinstance(values, (tuple, list)) or any(type(value) is not str for value in values):
        raise SourceContractError(RejectionCode.MALFORMED)
    source_ids = set(values)
    source_ids.update(ref.key.logical_id for ref in current.head.root_refs if ref.key.kind is SourceKind.RAW_PARAGRAPH)
    result = []
    for identifier in sorted(source_ids):
        key = session.resolve_id(identifier)
        raw = session.source(key) if key is not None else None
        if raw is None or raw.head.ref.key.kind is not SourceKind.RAW_PARAGRAPH or raw.head.state is not SourceState.LIVE:
            raise SourceContractError(RejectionCode.INVALID_UPSTREAM)
        payload = dict(raw.payload)
        payload["premise_superseded"] = "typed:" + command.key.logical_id
        result.append(SourceMutation(raw.head.ref.key, raw.head.ref.token, SourceOperation.REPLACE,
                                     payload, raw.head.source_class, raw.head.input_context, raw.head.root_refs))
    return tuple(result)


def reject_reserved_publication(memory_id: object, namespace: object) -> None:
    """Ordinary writers cannot mint or strip a source-derived publication."""
    if (isinstance(memory_id, str) and memory_id.startswith(PUBLICATION_PREFIX)
            or isinstance(namespace, Mapping) and CARRIER_KEY in namespace):
        raise SourceContractError(RejectionCode.UNSUPPORTED_SEMANTICS)


def validate_publish_command(command: PublishCommand) -> None:
    """Structural checks only; this never grants permission to persist."""
    from memplex.source_codec import encode_envelope, output_digest

    if type(command) is not PublishCommand:
        raise SourceContractError(RejectionCode.MALFORMED)
    encoded = encode_envelope(command.envelope)
    if len(encoded.encode()) > 65536:
        raise SourceContractError(RejectionCode.BUDGET_EXHAUSTED)
    if command.output.get("memory_type") != "fact" or type(command.output.get("trust_tier")) is not int or command.output.get("trust_tier") != 1:
        raise SourceContractError(RejectionCode.UNSUPPORTED_SEMANTICS)
    namespace = command.output.get("namespace", {})
    if not isinstance(namespace, Mapping) or namespace.get(CARRIER_KEY, encoded) != encoded:
        raise SourceContractError(RejectionCode.MALFORMED)
    if output_digest(command.output) != command.envelope.output_digest:
        raise SourceContractError(RejectionCode.MALFORMED)


def _publication_digest(value: object) -> str:
    from memplex.source_codec import _canonical

    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _publication_request_digest(command: PublishCommand, context: AuthorizationContext,
                                expected_deleted: TargetToken | None) -> str:
    return _publication_digest({"command": command, "recreate": expected_deleted,
                                "principal": context.principal, "workspace": context.workspace_id,
                                "agent": context.agent_id, "session": context.session_id})


def _admit_publication(session: SourceReadSession, context: AuthorizationContext,
                       scope: AuthorityScope) -> AuthorityState:
    authority = session.authority()
    principal = context.principal
    if (authority.scope != scope or authority.state is not SourceState.LIVE
            or (principal.tenant_id, context.workspace_id) != (scope.tenant_id, scope.workspace_id)):
        raise SourceContractError(RejectionCode.DENIED)
    from memplex.source_policy import admit_principal

    if not admit_principal(context, authority.policy):
        raise SourceContractError(RejectionCode.DENIED)
    return authority


def _compare_target(current: TargetHead | None, command: PublishCommand,
                    expected_deleted: TargetToken | None) -> None:
    if current is None:
        if command.expected_target is not ExpectedAbsent.ABSENT or expected_deleted is not None:
            raise SourceContractError(RejectionCode.PUBLICATION_CONFLICT)
        return
    state = SourceState.DELETED if expected_deleted is not None else SourceState.LIVE
    if (current.state is not state or command.expected_target != current.token
            or expected_deleted is not None and expected_deleted != current.token
            or current.last_commit_seq > command.manifest.observed_commit_seq):
        raise SourceContractError(RejectionCode.PUBLICATION_CONFLICT)


def _publication_replay(record: PublicationRecord | None, digest: str) -> PublicationReceipt | None:
    if record is None:
        return None
    if record.request_digest != digest:
        raise SourceContractError(RejectionCode.PUBLICATION_CONFLICT)
    # A historical receipt reports an already committed operation; no body or
    # target is resurrected, and current reads still perform full validation.
    return PublicationReceipt(record.publication_id, record.target_token, record.commit_seq, True)


def _make_publication(command: PublishCommand, authority: AuthorityState,
                      context: AuthorizationContext, current: TargetHead | None,
                      digest: str, expected_deleted: TargetToken | None) -> tuple[TargetHead, PublicationRecord, SourceSnapshot]:
    from memplex.auth import bind_node_identity
    from memplex.factual_lineage import capture_audit_provenance
    from memplex.models import Fact
    from memplex.source_codec import _canonical, encode_envelope, output_digest, semantic_digest

    manifest, scope = command.manifest, authority.scope
    if manifest.authority_id != scope.authority_id:
        raise SourceContractError(RejectionCode.DENIED)
    if manifest.policy_revision != authority.policy.revision:
        raise SourceContractError(RejectionCode.AUTH_CHANGED)
    if manifest.algorithm_version not in authority.policy.algorithm_versions:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
    if manifest.observed_commit_seq > authority.commit_seq:
        raise SourceContractError(RejectionCode.MALFORMED)
    _compare_target(current, command, expected_deleted)
    if authority.commit_seq >= MAX_REVISION or (current is not None and expected_deleted is None
                                               and current.token.generation >= MAX_REVISION):
        raise SourceIntegrityError()
    publication_id = PUBLICATION_PREFIX + uuid.uuid4().hex
    incarnation = str(uuid.uuid4()) if current is None or expected_deleted is not None else current.token.incarnation
    generation = 1 if current is None or expected_deleted is not None else current.token.generation + 1
    token = TargetToken(incarnation, generation, publication_id)
    target = TargetHead(command.derivation_key, token, SourceState.LIVE, authority.commit_seq + 1)
    payload: dict[str, Any] = json.loads(_canonical(command.output))
    node = Fact.from_dict(payload)
    audit = capture_audit_provenance(node)
    visibility = payload.get("visibility", "workspace")
    if visibility not in {"workspace", "user", "session"} or visibility == "session" and not context.session_id:
        raise SourceContractError(RejectionCode.DENIED)
    bind_node_identity(node, context, visibility=visibility, reject_conflicts=False)
    bound = node.to_dict()
    for field in ("tenant_id", "workspace_id", "owner_subject_id", "owner", "origin_session", "namespace", "provenance"):
        payload[field] = bound[field]
    payload.update(id=publication_id, visibility=visibility)
    payload["provenance"].update(audit)
    envelope = replace(command.envelope, publication_id=publication_id, target_incarnation=incarnation,
                       target_generation=generation, output_digest=output_digest(payload))
    payload["namespace"][CARRIER_KEY] = encode_envelope(envelope)
    record = PublicationRecord(publication_id, command.derivation_key, token, PublicationState.ACTIVE,
                               hashlib.sha256(encode_envelope(envelope).encode()).hexdigest(), envelope.output_digest,
                               manifest.input_refs, manifest.algorithm_version, command.request_id, digest,
                               _publication_digest(manifest), manifest.observed_commit_seq, target.last_commit_seq)
    key = SourceKey(scope.authority_id, scope.tenant_id, scope.workspace_id, SourceKind.FACT, publication_id)
    ref = SourceRef(key, SourceToken(str(uuid.uuid4()), 1, 1), 1,
                    semantic_digest(payload, input_context={}, root_refs=()))
    head = SourceHead(ref, SourceState.LIVE, SourceClass.DERIVED_V1, "pending", target.last_commit_seq, {}, ())
    return target, record, SourceSnapshot(head, payload)


class _PublicationView:
    """Tentative root over the held backend snapshot; never exposed as a receipt."""
    def __init__(self, session: SourceReadSession, target: TargetHead,
                 record: PublicationRecord, snapshot: SourceSnapshot) -> None:
        self._session, self._target, self._record, self._snapshot = session, target, record, snapshot

    def authority(self) -> AuthorityState:
        return replace(self._session.authority(), commit_seq=self._record.commit_seq)

    def resolve_id(self, memory_id: str) -> SourceKey | None:
        return self._snapshot.head.ref.key if memory_id == self._record.publication_id else self._session.resolve_id(memory_id)

    def source(self, key: SourceKey) -> SourceSnapshot | None:
        return self._snapshot if key == self._snapshot.head.ref.key else self._session.source(key)

    def target(self, derivation_key: str) -> TargetHead | None:
        return self._target if derivation_key == self._target.derivation_key else self._session.target(derivation_key)

    def publication(self, publication_id: str) -> PublicationRecord | None:
        return self._record if publication_id == self._record.publication_id else self._session.publication(publication_id)

    def utc_now(self) -> datetime:
        return self._session.utc_now()

    def monotonic_ns(self) -> int:
        return self._session.monotonic_ns()


def _check_publication_inputs(session: SourceReadSession, context: AuthorizationContext,
                              hooks: SourceValidationHooks, target: TargetHead,
                              record: PublicationRecord, snapshot: SourceSnapshot,
                              deadline_ns: int) -> ValidationResult:
    from memplex.source_validation import validate_sources

    view = _PublicationView(session, target, record, snapshot)
    result = validate_sources(view, [record.publication_id], authorization=context,
                              own_acl=hooks.own_acl, safe_for_model=hooks.safe_for_model,
                              limits=ValidationLimits(), deadline_ns=deadline_ns, allow_captured_inputs=True)
    if result.rejected:
        raise SourceContractError(next(iter(result.rejected.values())))
    return result


def _publication_tombstone(snapshot: SourceSnapshot, commit_seq: int) -> SourceHead:
    old = snapshot.head
    if old.state is not SourceState.LIVE or old.source_class is not SourceClass.DERIVED_V1:
        raise SourceIntegrityError()
    if old.ref.token.content_revision >= MAX_REVISION:
        raise SourceIntegrityError()
    ref = replace(old.ref, token=replace(old.ref.token, content_revision=old.ref.token.content_revision + 1))
    return SourceHead(ref, SourceState.DELETED, SourceClass.DERIVED_V1, "", commit_seq, {}, ())
