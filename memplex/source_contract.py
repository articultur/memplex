"""Immutable wire contract for the opt-in source validity V1 boundary."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TypeVar, cast
from uuid import UUID

CONTRACT_VERSION = 1
DIGEST_VERSION = 1
MAX_REVISION = 2**63 - 1
CARRIER_KEY = "memplex_derivation_contract"
PUBLICATION_PREFIX = "fact_derived_v1_"
ALGORITHM_VERSION = "factual_capture/source-validity-v1"
ALGORITHM_VERSIONS = frozenset({ALGORITHM_VERSION})
PRODUCER = "factual_capture"


class SourceKind(StrEnum):
    RAW_PARAGRAPH = "raw_paragraph"
    FUNCTION = "function"
    FACT = "fact"
    PREFERENCE = "preference"
    OBSERVATION = "observation"


class SourceState(StrEnum):
    LIVE = "live"
    DELETED = "deleted"
    QUARANTINED = "quarantined"


class SourceClass(StrEnum):
    CAPTURED_INPUT = "captured_input"
    DIRECT_INPUT = "direct_input"
    DERIVED_V1 = "derived_v1"
    LEGACY_UNVERIFIED = "legacy_unverified"


class PublicationState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    QUARANTINED = "quarantined"


class SourceOperation(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    DELETE = "delete"
    RECREATE = "recreate"


class ExpectedAbsent(StrEnum):
    ABSENT = "absent"


class RejectionCode(StrEnum):
    SOURCE_CHANGED = "source_changed"
    AUTH_CHANGED = "auth_changed"
    DELETED = "deleted"
    MISSING = "missing"
    DENIED = "denied"
    EXPIRED = "expired"
    INVALID_UPSTREAM = "invalid_upstream"
    UNSAFE = "unsafe"
    MALFORMED = "malformed"
    CYCLE = "cycle"
    BUDGET_EXHAUSTED = "budget_exhausted"
    LOOKUP_ERROR = "lookup_error"
    UNSUPPORTED_SEMANTICS = "unsupported_semantics"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PUBLICATION_CONFLICT = "publication_conflict"
    QUARANTINED = "quarantined"
    NOT_CURRENT_PUBLICATION = "not_current_publication"
    CAPTURED_INPUT_ONLY = "captured_input_only"


class SourceContractError(ValueError):
    def __init__(self, code: RejectionCode = RejectionCode.MALFORMED) -> None:
        self.code = code
        super().__init__(code.value)


class SourceBusyError(SourceContractError):
    pass


class SourceIntegrityError(SourceContractError):
    pass


_K = TypeVar("_K")
_V = TypeVar("_V")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _check(condition: bool, code: RejectionCode = RejectionCode.MALFORMED) -> None:
    if not condition:
        raise SourceContractError(code)


def _text(value: object, *, empty: bool = False) -> None:
    _check(type(value) is str)
    _check(empty or bool(value))
    try:
        cast(str, value).encode("utf-8")
    except UnicodeError as exc:
        raise SourceContractError() from exc


def _integer(value: object, *, minimum: int = 1, maximum: int = MAX_REVISION) -> None:
    _check(type(value) is int and minimum <= cast(int, value) <= maximum)


def _uuid(value: object) -> None:
    _text(value)
    try:
        parsed = UUID(cast(str, value))
    except ValueError as exc:
        raise SourceContractError() from exc
    _check(parsed.version == 4 and str(parsed) == value)


def _digest(value: object) -> None:
    _text(value)
    _check(_HEX_DIGEST.fullmatch(cast(str, value)) is not None)


def _publication(value: object) -> None:
    _text(value)
    raw = cast(str, value)
    _check(raw.startswith(PUBLICATION_PREFIX))
    token = raw[len(PUBLICATION_PREFIX) :]
    _check(len(token) == 32)
    try:
        parsed = UUID(hex=token)
    except ValueError as exc:
        raise SourceContractError() from exc
    _check(parsed.version == 4 and parsed.hex == token)


def _version(value: object) -> None:
    _integer(value)
    _check(value == 1, RejectionCode.UNSUPPORTED_CAPABILITY)


def _utc(value: object) -> None:
    _check(type(value) is datetime)
    at = cast(datetime, value)
    _check(at.tzinfo is not None and at.utcoffset() == timedelta(0))


def _freeze_json(value: object, active: frozenset[int] = frozenset()) -> object:
    """Detach JSON trees, preserve array order, and reject ambiguous values."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        _text(value, empty=True)
        return value
    if type(value) is float:
        _check(math.isfinite(cast(float, value)))
        return value
    _check(id(value) not in active)
    active = active | {id(value)}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            _text(key, empty=True)
            result[cast(str, key)] = _freeze_json(item, active)
        return MappingProxyType(result)
    if type(value) in (list, tuple):
        return tuple(_freeze_json(item, active) for item in cast(tuple[object, ...], value))
    raise SourceContractError()


def _payload(value: object) -> Mapping[str, object]:
    _check(isinstance(value, Mapping))
    try:
        return cast(Mapping[str, object], _freeze_json(value))
    except RecursionError as exc:
        raise SourceContractError() from exc


def _freeze_strings(value: object) -> Mapping[str, str]:
    result = _payload(value)
    for key, item in result.items():
        _text(key)
        _text(item, empty=True)
    return cast(Mapping[str, str], result)


def _mapping(value: Mapping[_K, _V], key_type: type[_K], value_type: type[_V]) -> Mapping[_K, _V]:
    _check(isinstance(value, Mapping))
    result = dict(value)
    for key, item in result.items():
        _check(type(key) is key_type and type(item) is value_type)
        if key_type is str:
            _text(key)
    return MappingProxyType(result)


def _scope(key: SourceKey) -> tuple[str, str, str]:
    return key.authority_id, key.tenant_id, key.workspace_id


def _same_scope(refs: tuple[SourceRef, ...], scope: tuple[str, str, str] | None = None) -> None:
    if refs:
        expected = scope if scope is not None else _scope(refs[0].key)
        _check(
            all(_scope(ref.key) == expected for ref in refs), RejectionCode.UNSUPPORTED_SEMANTICS
        )


def _refs(value: object, *, nonempty: bool = False) -> tuple[SourceRef, ...]:
    _check(type(value) is tuple)
    refs = cast(tuple[SourceRef, ...], value)
    _check(len(refs) <= 64, RejectionCode.BUDGET_EXHAUSTED)
    _check(not nonempty or bool(refs))
    _check(all(type(ref) is SourceRef for ref in refs))
    _check(len({ref.key for ref in refs}) == len(refs))
    _same_scope(refs)
    return tuple(
        sorted(refs, key=lambda ref: (*_scope(ref.key), ref.key.kind.value, ref.key.logical_id))
    )


@dataclass(frozen=True, slots=True)
class AuthorityScope:
    authority_id: str
    tenant_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        _uuid(self.authority_id)
        _text(self.tenant_id)
        _text(self.workspace_id)


@dataclass(frozen=True, slots=True)
class SourceKey:
    authority_id: str
    tenant_id: str
    workspace_id: str
    kind: SourceKind
    logical_id: str

    def __post_init__(self) -> None:
        _uuid(self.authority_id)
        _text(self.tenant_id)
        _text(self.workspace_id)
        _check(type(self.kind) is SourceKind)
        _text(self.logical_id)


@dataclass(frozen=True, slots=True)
class SourceToken:
    incarnation: str
    content_revision: int
    auth_revision: int

    def __post_init__(self) -> None:
        _uuid(self.incarnation)
        _integer(self.content_revision)
        _integer(self.auth_revision)


@dataclass(frozen=True, slots=True)
class SourceRef:
    key: SourceKey
    token: SourceToken
    digest_version: int
    semantic_digest: str

    def __post_init__(self) -> None:
        _check(type(self.key) is SourceKey and type(self.token) is SourceToken)
        _version(self.digest_version)
        _digest(self.semantic_digest)


@dataclass(frozen=True, slots=True)
class SourceHead:
    ref: SourceRef
    state: SourceState
    source_class: SourceClass
    payload_locator: str
    last_commit_seq: int
    input_context: Mapping[str, str]
    root_refs: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        _check(type(self.ref) is SourceRef)
        _check(type(self.state) is SourceState and type(self.source_class) is SourceClass)
        _text(self.payload_locator, empty=self.state is SourceState.DELETED)
        _integer(self.last_commit_seq)
        object.__setattr__(self, "input_context", _freeze_strings(self.input_context))
        object.__setattr__(self, "root_refs", _refs(self.root_refs))
        _same_scope(self.root_refs, _scope(self.ref.key))
        if self.state is SourceState.DELETED:
            _check(not self.payload_locator and not self.input_context and not self.root_refs)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    head: SourceHead
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _check(type(self.head) is SourceHead)
        object.__setattr__(self, "payload", _payload(self.payload))


@dataclass(frozen=True, slots=True)
class TargetToken:
    incarnation: str
    generation: int
    current_publication: str

    def __post_init__(self) -> None:
        _uuid(self.incarnation)
        _integer(self.generation)
        _publication(self.current_publication)


@dataclass(frozen=True, slots=True)
class TargetHead:
    derivation_key: str
    token: TargetToken
    state: SourceState
    last_commit_seq: int

    def __post_init__(self) -> None:
        _digest(self.derivation_key)
        _check(type(self.token) is TargetToken and type(self.state) is SourceState)
        _integer(self.last_commit_seq)


@dataclass(frozen=True, slots=True)
class DerivationEnvelope:
    contract_version: int
    producer: str
    algorithm_version: str
    derivation_key: str
    publication_id: str
    target_incarnation: str
    target_generation: int
    authority_id: str
    policy_revision: int
    required_premises: tuple[SourceRef, ...]
    alternative_support_groups: tuple[object, ...]
    authorization_dependencies: tuple[SourceRef, ...]
    output_digest: str
    output_digest_version: int

    def __post_init__(self) -> None:
        _version(self.contract_version)
        _version(self.output_digest_version)
        _text(self.producer)
        _check(self.producer == PRODUCER, RejectionCode.UNSUPPORTED_CAPABILITY)
        _text(self.algorithm_version)
        _check(self.algorithm_version in ALGORITHM_VERSIONS, RejectionCode.UNSUPPORTED_CAPABILITY)
        _digest(self.derivation_key)
        _publication(self.publication_id)
        _uuid(self.target_incarnation)
        _uuid(self.authority_id)
        _integer(self.target_generation)
        _integer(self.policy_revision)
        _digest(self.output_digest)
        _check(type(self.alternative_support_groups) is tuple)
        _check(not self.alternative_support_groups, RejectionCode.UNSUPPORTED_SEMANTICS)
        object.__setattr__(self, "required_premises", _refs(self.required_premises, nonempty=True))
        object.__setattr__(
            self,
            "authorization_dependencies",
            _refs(self.authorization_dependencies, nonempty=True),
        )
        _check(
            self.required_premises == self.authorization_dependencies,
            RejectionCode.UNSUPPORTED_SEMANTICS,
        )
        _check(
            all(ref.key.authority_id == self.authority_id for ref in self.required_premises),
            RejectionCode.UNSUPPORTED_SEMANTICS,
        )


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_id: str
    target_key: str
    target_token: TargetToken
    state: PublicationState
    envelope_digest: str
    output_digest: str
    source_refs: tuple[SourceRef, ...]
    algorithm_version: str
    request_id: str
    request_digest: str
    manifest_digest: str
    observed_commit_seq: int
    commit_seq: int

    def __post_init__(self) -> None:
        _publication(self.publication_id)
        _digest(self.target_key)
        _check(type(self.target_token) is TargetToken and type(self.state) is PublicationState)
        _check(self.target_token.current_publication == self.publication_id)
        for digest in (
            self.envelope_digest,
            self.output_digest,
            self.request_digest,
            self.manifest_digest,
        ):
            _digest(digest)
        _uuid(self.request_id)
        _text(self.algorithm_version)
        _check(self.algorithm_version in ALGORITHM_VERSIONS, RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "source_refs", _refs(self.source_refs, nonempty=True))
        _integer(self.observed_commit_seq)
        _integer(self.commit_seq)
        _check(self.observed_commit_seq <= self.commit_seq)


@dataclass(frozen=True, slots=True)
class SourceMutation:
    key: SourceKey
    expected: SourceToken | ExpectedAbsent
    operation: SourceOperation
    payload: Mapping[str, object] | None
    source_class: SourceClass
    input_context: Mapping[str, str]
    root_refs: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        _check(type(self.key) is SourceKey and type(self.operation) is SourceOperation)
        _check(type(self.source_class) is SourceClass)
        if self.operation is SourceOperation.CREATE:
            _check(self.expected is ExpectedAbsent.ABSENT)
        else:
            _check(type(self.expected) is SourceToken)
        if self.operation is SourceOperation.DELETE:
            _check(self.payload is None)
        else:
            object.__setattr__(self, "payload", _payload(self.payload))
        object.__setattr__(self, "input_context", _freeze_strings(self.input_context))
        object.__setattr__(self, "root_refs", _refs(self.root_refs))
        _same_scope(self.root_refs, _scope(self.key))


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    ref: SourceRef
    state: SourceState
    commit_seq: int

    def __post_init__(self) -> None:
        _check(type(self.ref) is SourceRef and type(self.state) is SourceState)
        _integer(self.commit_seq)


@dataclass(frozen=True, slots=True)
class CaptureManifest:
    capture_id: str
    authority_id: str
    policy_revision: int
    observed_commit_seq: int
    input_refs: tuple[SourceRef, ...]
    input_parameters: Mapping[str, str]
    input_digest: str
    algorithm_version: str

    def __post_init__(self) -> None:
        _uuid(self.capture_id)
        _uuid(self.authority_id)
        _integer(self.policy_revision)
        _integer(self.observed_commit_seq)
        _digest(self.input_digest)
        _text(self.algorithm_version)
        _check(self.algorithm_version in ALGORITHM_VERSIONS, RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "input_refs", _refs(self.input_refs, nonempty=True))
        _check(
            all(ref.key.authority_id == self.authority_id for ref in self.input_refs),
            RejectionCode.UNSUPPORTED_SEMANTICS,
        )
        object.__setattr__(self, "input_parameters", _freeze_strings(self.input_parameters))


@dataclass(frozen=True, slots=True)
class PublishCommand:
    request_id: str
    derivation_key: str
    expected_target: TargetToken | ExpectedAbsent
    manifest: CaptureManifest
    envelope: DerivationEnvelope
    output: Mapping[str, object]

    def __post_init__(self) -> None:
        _uuid(self.request_id)
        _digest(self.derivation_key)
        _check(
            type(self.expected_target) is TargetToken
            or self.expected_target is ExpectedAbsent.ABSENT
        )
        _check(type(self.manifest) is CaptureManifest and type(self.envelope) is DerivationEnvelope)
        _check(self.derivation_key == self.envelope.derivation_key)
        _check(self.manifest.authority_id == self.envelope.authority_id)
        _check(self.manifest.policy_revision == self.envelope.policy_revision)
        _check(self.manifest.algorithm_version == self.envelope.algorithm_version)
        _check(self.manifest.input_refs == self.envelope.required_premises)
        object.__setattr__(self, "output", _payload(self.output))


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    publication_id: str
    target: TargetToken
    commit_seq: int
    replayed: bool

    def __post_init__(self) -> None:
        _publication(self.publication_id)
        _check(type(self.target) is TargetToken)
        _check(self.target.current_publication == self.publication_id)
        _integer(self.commit_seq)
        _check(type(self.replayed) is bool)


@dataclass(frozen=True, slots=True)
class ValidationLimits:
    max_roots: int = 500
    max_envelope_bytes: int = 65536
    max_direct_refs: int = 64
    max_nodes_per_root: int = 128
    max_depth: int = 16
    max_unique_reads: int = 5000
    max_fence_ms: int = 250

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.max_roots, 500),
            (self.max_envelope_bytes, 65536),
            (self.max_direct_refs, 64),
            (self.max_nodes_per_root, 128),
            (self.max_depth, 16),
            (self.max_unique_reads, 5000),
            (self.max_fence_ms, 250),
        ):
            _integer(value, maximum=maximum)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    accepted: Mapping[str, SourceSnapshot]
    rejected: Mapping[str, RejectionCode]
    dependency_refs: Mapping[str, tuple[SourceRef, ...]]
    sources: Mapping[SourceKey, SourceSnapshot]

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted", _mapping(self.accepted, str, SourceSnapshot))
        object.__setattr__(self, "rejected", _mapping(self.rejected, str, RejectionCode))
        _check(not self.accepted.keys() & self.rejected.keys())
        _check(isinstance(self.dependency_refs, Mapping))
        dependencies: dict[str, tuple[SourceRef, ...]] = {}
        for key, refs in self.dependency_refs.items():
            _text(key)
            _check(type(refs) is tuple and all(type(ref) is SourceRef for ref in refs))
            dependencies[key] = tuple(refs)
        object.__setattr__(self, "dependency_refs", MappingProxyType(dependencies))
        object.__setattr__(self, "sources", _mapping(self.sources, SourceKey, SourceSnapshot))
        _check(all(key == snapshot.head.ref.key for key, snapshot in self.sources.items()))


@dataclass(frozen=True, slots=True)
class FinalRead:
    linearized_at: datetime
    accepted: Mapping[str, SourceSnapshot]
    dropped: Mapping[str, int]

    def __post_init__(self) -> None:
        _utc(self.linearized_at)
        object.__setattr__(self, "accepted", _mapping(self.accepted, str, SourceSnapshot))
        dropped = _mapping(self.dropped, str, int)
        for count in dropped.values():
            _integer(count, minimum=0)
        object.__setattr__(self, "dropped", dropped)


@dataclass(frozen=True, slots=True)
class PolicyEntry:
    authentication_id: str
    tenant_id: str
    subject_id: str
    workspace_id: str
    agent_id: str
    roles: frozenset[str]

    def __post_init__(self) -> None:
        for value in (
            self.authentication_id,
            self.tenant_id,
            self.subject_id,
            self.workspace_id,
            self.agent_id,
        ):
            _text(value)
        _check(type(self.roles) is frozenset)
        for role in self.roles:
            _text(role)


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    revision: int
    entries: tuple[PolicyEntry, ...]
    algorithm_versions: frozenset[str]

    def __post_init__(self) -> None:
        _integer(self.revision)
        _check(
            type(self.entries) is tuple
            and all(type(entry) is PolicyEntry for entry in self.entries)
        )
        _check(len({entry.authentication_id for entry in self.entries}) == len(self.entries))
        object.__setattr__(
            self, "entries", tuple(sorted(self.entries, key=lambda entry: entry.authentication_id))
        )
        _check(type(self.algorithm_versions) is frozenset)
        for version in self.algorithm_versions:
            _text(version)
            _check(version in ALGORITHM_VERSIONS, RejectionCode.UNSUPPORTED_CAPABILITY)


@dataclass(frozen=True, slots=True)
class AuthorityState:
    scope: AuthorityScope
    state: SourceState
    contract_version: int
    policy: PolicySnapshot
    commit_seq: int

    def __post_init__(self) -> None:
        _check(type(self.scope) is AuthorityScope and type(self.state) is SourceState)
        _version(self.contract_version)
        _check(type(self.policy) is PolicySnapshot)
        _integer(self.commit_seq)
        _check(
            all(
                entry.tenant_id == self.scope.tenant_id
                and entry.workspace_id == self.scope.workspace_id
                for entry in self.policy.entries
            ),
            RejectionCode.UNSUPPORTED_SEMANTICS,
        )


@dataclass(frozen=True, slots=True)
class SourceLedger:
    authority: AuthorityState
    sources: Mapping[SourceKey, SourceHead]
    targets: Mapping[str, TargetHead]
    publications: Mapping[str, PublicationRecord]

    def __post_init__(self) -> None:
        _check(type(self.authority) is AuthorityState)
        object.__setattr__(self, "sources", _mapping(self.sources, SourceKey, SourceHead))
        object.__setattr__(self, "targets", _mapping(self.targets, str, TargetHead))
        object.__setattr__(
            self, "publications", _mapping(self.publications, str, PublicationRecord)
        )
        scope = (
            self.authority.scope.authority_id,
            self.authority.scope.tenant_id,
            self.authority.scope.workspace_id,
        )
        ids: set[str] = set()
        for key, head in self.sources.items():
            _check(key == head.ref.key and _scope(key) == scope)
            _check(key.logical_id not in ids)
            ids.add(key.logical_id)
            _check(head.last_commit_seq <= self.authority.commit_seq)
        requests: set[str] = set()
        for publication_id, publication in self.publications.items():
            _check(publication_id == publication.publication_id)
            _same_scope(publication.source_refs, scope)
            _check(publication.commit_seq <= self.authority.commit_seq)
            _check(publication.request_id not in requests)
            requests.add(publication.request_id)
            _check(publication.target_key in self.targets)
            target = self.targets[publication.target_key]
            if publication.state is PublicationState.ACTIVE:
                _check(
                    target.state is SourceState.LIVE and target.token == publication.target_token
                )
        for target_key, target in self.targets.items():
            _check(
                target_key == target.derivation_key
                and target.last_commit_seq <= self.authority.commit_seq
            )
            current = self.publications.get(target.token.current_publication)
            _check(current is not None)
            _check(
                current is not None
                and current.target_key == target_key
                and current.target_token.incarnation == target.token.incarnation
                and current.target_token.generation <= target.token.generation
            )
            if target.state is SourceState.LIVE:
                _check(
                    current is not None
                    and current.state is PublicationState.ACTIVE
                    and current.target_token == target.token
                )
