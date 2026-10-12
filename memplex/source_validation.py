"""Pure V1 dependency proofs over a caller-owned stable authority read fence.

No store, model, or network access is opened here. The supplied session must
already be admitted by its trusted backend; T3 owns that capability boundary.
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from memplex.auth import AuthorizationContext
from memplex.source_codec import decode_envelope, encode_envelope, output_digest, semantic_digest
from memplex.source_contract import (
    CARRIER_KEY,
    CONTRACT_VERSION,
    PUBLICATION_PREFIX,
    AuthorityState,
    DerivationEnvelope,
    FinalRead,
    PublicationRecord,
    PublicationState,
    RejectionCode,
    SourceClass,
    SourceContractError,
    SourceKey,
    SourceKind,
    SourceRef,
    SourceSnapshot,
    SourceState,
    TargetHead,
    TargetToken,
    ValidationLimits,
    ValidationResult,
)

if TYPE_CHECKING:
    from memplex.storage.source_versions import SourceReadSession


@dataclass(frozen=True, slots=True)
class _Visit:
    key: SourceKey
    expected: SourceRef | None
    depth: int
    exit: bool = False


@dataclass(slots=True)
class _Root:
    memory_id: str
    snapshot: SourceSnapshot | None = None
    stack: list[_Visit] = field(default_factory=list)
    path: set[SourceKey] = field(default_factory=set)
    closure: dict[SourceKey, SourceRef] = field(default_factory=dict)
    heights: dict[SourceKey, int] = field(default_factory=dict)
    dependencies: dict[SourceKey, tuple[SourceRef, ...]] = field(default_factory=dict)


def _require(condition: bool, code: RejectionCode = RejectionCode.MALFORMED) -> None:
    if not condition:
        raise SourceContractError(code)


def _scope(key: SourceKey) -> tuple[str, str, str]:
    return key.authority_id, key.tenant_id, key.workspace_id


def _check_ref(expected: SourceRef | None, actual: SourceRef) -> None:
    if expected is None:
        return
    _require(expected.key == actual.key)
    _require(expected.token.incarnation == actual.token.incarnation, RejectionCode.SOURCE_CHANGED)
    _require(
        expected.token.content_revision == actual.token.content_revision,
        RejectionCode.SOURCE_CHANGED,
    )
    _require(expected.token.auth_revision == actual.token.auth_revision, RejectionCode.AUTH_CHANGED)
    _require(
        expected.digest_version == actual.digest_version
        and expected.semantic_digest == actual.semantic_digest,
        RejectionCode.SOURCE_CHANGED,
    )


def _identity(
    snapshot: SourceSnapshot, authority: AuthorityState, context: AuthorizationContext
) -> None:
    """Persisted identity is mandatory before the legacy own-ACL hook."""
    key, payload = snapshot.head.ref.key, snapshot.payload
    scope = authority.scope
    _require(
        _scope(key) == (scope.authority_id, scope.tenant_id, scope.workspace_id),
        RejectionCode.UNSUPPORTED_SEMANTICS,
    )
    _require(
        context.principal.tenant_id == key.tenant_id and context.workspace_id == key.workspace_id,
        RejectionCode.DENIED,
    )
    _require(
        payload.get("tenant_id") == key.tenant_id
        and payload.get("workspace_id") == key.workspace_id,
        RejectionCode.DENIED,
    )
    _require(payload.get("id") == key.logical_id)
    if key.kind is not SourceKind.RAW_PARAGRAPH:
        _require(payload.get("memory_type") == key.kind.value)
    namespace = payload.get("namespace", {})
    _require(isinstance(namespace, Mapping))
    assert isinstance(namespace, Mapping)
    # A serializer compatibility alias cannot contradict the persisted columns.
    for alias, field_name in (
        ("memplex_tenant_id", "tenant_id"),
        ("memplex_workspace_id", "workspace_id"),
        ("memplex_subject_id", "owner_subject_id"),
    ):
        if alias in namespace:
            _require(namespace[alias] == payload.get(field_name), RejectionCode.DENIED)


def _lifecycle_current(snapshot: SourceSnapshot) -> None:
    """Reject existing non-time lifecycle markers without changing legacy rules."""
    payload = snapshot.payload
    _require(
        not any(
            payload.get(field)
            for field in ("context_historical", "historical", "superseded", "premise_superseded")
        ),
        RejectionCode.INVALID_UPSTREAM,
    )
    namespace = payload.get("namespace", {})
    _require(isinstance(namespace, Mapping))
    assert isinstance(namespace, Mapping)
    _require(not namespace.get("premise_superseded"), RejectionCode.INVALID_UPSTREAM)


def _state(snapshot: SourceSnapshot) -> None:
    state = snapshot.head.state
    if state is SourceState.DELETED:
        raise SourceContractError(RejectionCode.DELETED)
    if state is not SourceState.LIVE:
        raise SourceContractError(RejectionCode.QUARANTINED)
    _require(
        snapshot.head.source_class is not SourceClass.LEGACY_UNVERIFIED,
        RejectionCode.INVALID_UPSTREAM,
    )
    _lifecycle_current(snapshot)


def _carrier(snapshot: SourceSnapshot, limits: ValidationLimits) -> DerivationEnvelope:
    namespace = snapshot.payload.get("namespace", {})
    _require(isinstance(namespace, Mapping))
    assert isinstance(namespace, Mapping)
    value = namespace.get(CARRIER_KEY)
    _require(type(value) is str)
    assert isinstance(value, str)
    _require(
        len(value.encode("utf-8")) <= limits.max_envelope_bytes, RejectionCode.BUDGET_EXHAUSTED
    )
    return decode_envelope(value)


def _publication(
    session: SourceReadSession,
    snapshot: SourceSnapshot,
    authority: AuthorityState,
    limits: ValidationLimits,
) -> DerivationEnvelope:
    envelope = _carrier(snapshot, limits)
    _require(envelope.publication_id == snapshot.head.ref.key.logical_id)
    _require(snapshot.head.ref.key.kind is SourceKind.FACT)
    _require(
        envelope.authority_id == authority.scope.authority_id, RejectionCode.UNSUPPORTED_SEMANTICS
    )
    _require(envelope.policy_revision == authority.policy.revision, RejectionCode.AUTH_CHANGED)
    _require(
        envelope.algorithm_version in authority.policy.algorithm_versions,
        RejectionCode.UNSUPPORTED_CAPABILITY,
    )
    record = session.publication(envelope.publication_id)
    _require(record is not None, RejectionCode.MISSING)
    assert record is not None
    _require(type(record) is PublicationRecord)
    _require(record.state is PublicationState.ACTIVE, RejectionCode.NOT_CURRENT_PUBLICATION)
    expected = TargetToken(
        envelope.target_incarnation, envelope.target_generation, envelope.publication_id
    )
    target = session.target(envelope.derivation_key)
    _require(
        target is not None and target.state is SourceState.LIVE and target.token == expected,
        RejectionCode.NOT_CURRENT_PUBLICATION,
    )
    assert target is not None
    _require(type(target) is TargetHead and target.derivation_key == envelope.derivation_key)
    _require(
        record.publication_id == envelope.publication_id
        and record.target_key == envelope.derivation_key
        and record.target_token == expected
    )
    _require(
        record.envelope_digest
        == hashlib.sha256(encode_envelope(envelope).encode("utf-8")).hexdigest()
    )
    _require(record.output_digest == envelope.output_digest == output_digest(snapshot.payload))
    _require(
        record.source_refs == envelope.required_premises
        and record.algorithm_version == envelope.algorithm_version
    )
    _require(
        record.commit_seq <= authority.commit_seq and target.last_commit_seq <= authority.commit_seq
    )
    return envelope


def _dependencies(
    session: SourceReadSession,
    snapshot: SourceSnapshot,
    authority: AuthorityState,
    limits: ValidationLimits,
) -> tuple[SourceRef, ...]:
    head = snapshot.head
    refs = head.root_refs
    if head.source_class is SourceClass.DERIVED_V1:
        envelope = _publication(session, snapshot, authority, limits)
        # Both frozen sets are checked independently even though V1 requires equality.
        _require(
            len(envelope.required_premises) <= limits.max_direct_refs
            and len(envelope.authorization_dependencies) <= limits.max_direct_refs,
            RejectionCode.BUDGET_EXHAUSTED,
        )
        refs = envelope.required_premises + envelope.authorization_dependencies + refs
    else:
        namespace = snapshot.payload.get("namespace", {})
        _require(isinstance(namespace, Mapping))
        assert isinstance(namespace, Mapping)
        _require(
            CARRIER_KEY not in namespace
            and not head.ref.key.logical_id.startswith(PUBLICATION_PREFIX)
        )
    _require(len(head.root_refs) <= limits.max_direct_refs, RejectionCode.BUDGET_EXHAUSTED)
    if (
        head.source_class is SourceClass.CAPTURED_INPUT
        and head.ref.key.kind is not SourceKind.RAW_PARAGRAPH
    ):
        _require(
            head.ref.key.kind in {SourceKind.FUNCTION, SourceKind.FACT, SourceKind.PREFERENCE}
            and bool(head.root_refs)
            and all(ref.key.kind is SourceKind.RAW_PARAGRAPH for ref in head.root_refs),
            RejectionCode.INVALID_UPSTREAM,
        )
    return refs


@dataclass(slots=True)
class _Validation:
    session: SourceReadSession
    authorization: AuthorizationContext
    own_acl: Callable[[SourceSnapshot, AuthorizationContext], bool]
    safe_for_model: Callable[[SourceSnapshot], bool]
    limits: ValidationLimits
    authority: AuthorityState
    end_ns: int
    allow_captured_inputs: bool
    cache: dict[SourceKey, SourceSnapshot | RejectionCode] = field(default_factory=dict)
    reads: int = 0

    def check_time(self) -> None:
        _require(self.session.monotonic_ns() < self.end_ns, RejectionCode.BUDGET_EXHAUSTED)

    def read(self, key: SourceKey) -> SourceSnapshot:
        if key not in self.cache:
            _require(self.reads < self.limits.max_unique_reads, RejectionCode.BUDGET_EXHAUSTED)
            self.reads += 1
            try:
                value = self.session.source(key)
                if value is None:
                    self.cache[key] = RejectionCode.MISSING
                else:
                    # Only an exact, correctly bound DTO may enter the snapshot
                    # cache. Malformed entries must become per-key rejections.
                    _require(type(value) is SourceSnapshot and value.head.ref.key == key)
                    self.cache[key] = value
            except SourceContractError as exc:
                self.cache[key] = exc.code
            except Exception:  # noqa: BLE001 - authoritative read errors fail closed per root
                self.cache[key] = RejectionCode.LOOKUP_ERROR
        cached = self.cache[key]
        if isinstance(cached, RejectionCode):
            raise SourceContractError(cached)
        _require(type(cached) is SourceSnapshot and cached.head.ref.key == key)
        return cached

    def step(self, root: _Root) -> None:
        self.check_time()
        visit = root.stack.pop()
        if visit.exit:
            self.finish_node(root, visit)
            return
        key = visit.key
        scope = self.authority.scope
        _require(
            _scope(key) == (scope.authority_id, scope.tenant_id, scope.workspace_id),
            RejectionCode.UNSUPPORTED_SEMANTICS,
        )
        _require(key not in root.path, RejectionCode.CYCLE)
        _require(visit.depth <= self.limits.max_depth, RejectionCode.BUDGET_EXHAUSTED)
        if key not in root.closure:
            _require(
                len(root.closure) < self.limits.max_nodes_per_root, RejectionCode.BUDGET_EXHAUSTED
            )
        snapshot = self.read(key)
        self.check_time()
        _check_ref(visit.expected, snapshot.head.ref)
        root.closure[key] = snapshot.head.ref
        if key in root.heights:
            _require(
                visit.depth + root.heights[key] - 1 <= self.limits.max_depth,
                RejectionCode.BUDGET_EXHAUSTED,
            )
            return
        _state(snapshot)
        _identity(snapshot, self.authority, self.authorization)
        _require(snapshot.head.last_commit_seq <= self.authority.commit_seq)
        _require(self.own_acl(snapshot, self.authorization), RejectionCode.DENIED)
        _require(self.safe_for_model(snapshot), RejectionCode.UNSAFE)
        if visit.depth == 1:
            root.snapshot = snapshot
            _require(
                self.allow_captured_inputs
                or snapshot.head.source_class is not SourceClass.CAPTURED_INPUT
                or key.kind is SourceKind.RAW_PARAGRAPH,
                RejectionCode.CAPTURED_INPUT_ONLY,
            )
        refs = _dependencies(self.session, snapshot, self.authority, self.limits)
        root.dependencies[key] = refs
        root.path.add(key)
        root.stack.append(_Visit(key, visit.expected, visit.depth, exit=True))
        root.stack.extend(_Visit(ref.key, ref, visit.depth + 1) for ref in reversed(refs))
        self.check_time()

    def finish_node(self, root: _Root, visit: _Visit) -> None:
        snapshot = self.read(visit.key)
        _require(
            snapshot.head.ref.semantic_digest
            == semantic_digest(
                snapshot.payload,
                input_context=snapshot.head.input_context,
                root_refs=snapshot.head.root_refs,
            )
        )
        root.heights[visit.key] = 1 + max(
            (root.heights[ref.key] for ref in root.dependencies[visit.key]), default=0
        )
        root.path.remove(visit.key)
        self.check_time()


def validate_sources(
    session: SourceReadSession,
    roots: Sequence[str],
    *,
    authorization: AuthorizationContext,
    own_acl: Callable[[SourceSnapshot, AuthorizationContext], bool],
    safe_for_model: Callable[[SourceSnapshot], bool],
    limits: ValidationLimits,
    deadline_ns: int,
    allow_captured_inputs: bool = False,
) -> ValidationResult:
    """Round-robin proofs: root counts as one closure node and depth starts at 1."""
    accepted: dict[str, SourceSnapshot] = {}
    rejected: dict[str, RejectionCode] = {}
    dependencies: dict[str, tuple[SourceRef, ...]] = {}
    if len(roots) > limits.max_roots:
        return ValidationResult({}, dict.fromkeys(roots, RejectionCode.BUDGET_EXHAUSTED), {}, {})
    try:
        _require(type(deadline_ns) is int and type(allow_captured_inputs) is bool)
        start = session.monotonic_ns()
        authority = session.authority()
        _require(
            type(authority) is AuthorityState and authority.contract_version == CONTRACT_VERSION,
            RejectionCode.UNSUPPORTED_CAPABILITY,
        )
        _require(authority.state is SourceState.LIVE, RejectionCode.QUARANTINED)
        state = _Validation(
            session,
            authorization,
            own_acl,
            safe_for_model,
            limits,
            authority,
            min(deadline_ns, start + limits.max_fence_ms * 1_000_000),
            allow_captured_inputs,
        )
    except SourceContractError as exc:
        return ValidationResult({}, dict.fromkeys(roots, exc.code), {}, {})
    except Exception:  # noqa: BLE001 - authority failure invalidates this stable view
        return ValidationResult({}, dict.fromkeys(roots, RejectionCode.LOOKUP_ERROR), {}, {})
    pending: deque[_Root] = deque()
    for memory_id in dict.fromkeys(roots):
        try:
            state.check_time()
            key = session.resolve_id(memory_id)
            _require(key is not None, RejectionCode.MISSING)
            assert key is not None
            _require(type(key) is SourceKey and key.logical_id == memory_id)
            pending.append(_Root(memory_id, stack=[_Visit(key, None, 1)]))
        except SourceContractError as exc:
            rejected[memory_id] = exc.code
        except Exception:  # noqa: BLE001 - resolution errors fail closed per root
            rejected[memory_id] = RejectionCode.LOOKUP_ERROR
    while pending:
        root = pending.popleft()
        try:
            state.step(root)
            if root.stack:
                pending.append(root)
            else:
                assert root.snapshot is not None
                accepted[root.memory_id] = root.snapshot
                dependencies[root.memory_id] = tuple(root.closure.values())
        except SourceContractError as exc:
            rejected[root.memory_id] = exc.code
        except Exception:  # noqa: BLE001 - callbacks and authoritative metadata fail closed
            rejected[root.memory_id] = RejectionCode.LOOKUP_ERROR
    sources = {
        key: value for key, value in state.cache.items() if isinstance(value, SourceSnapshot)
    }
    return ValidationResult(accepted, rejected, dependencies, sources)


def _timestamp(value: object) -> datetime:
    _require(type(value) is str)
    assert isinstance(value, str)
    try:
        at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceContractError() from exc
    _require(at.tzinfo is not None and at.utcoffset() == timedelta(0))
    return at


def _current_at(snapshot: SourceSnapshot, at: datetime) -> None:
    payload = snapshot.payload
    _lifecycle_current(snapshot)
    for field_name in ("valid_until", "expires_at", "invalid_at"):
        value = payload.get(field_name)
        if value is not None:
            _require(at < _timestamp(value), RejectionCode.EXPIRED)
    value = payload.get("valid_from")
    if value is not None:
        _require(_timestamp(value) <= at, RejectionCode.INVALID_UPSTREAM)


def finalize_at(result: ValidationResult, *, at: datetime) -> FinalRead:
    """Recheck all proven dependency times at the caller's single final UTC L."""
    _require(type(at) is datetime and at.tzinfo is not None and at.utcoffset() == timedelta(0))
    accepted: dict[str, SourceSnapshot] = {}
    dropped = Counter(code.value for code in result.rejected.values())
    for memory_id, snapshot in result.accepted.items():
        try:
            refs = result.dependency_refs.get(memory_id)
            _require(refs is not None and snapshot.head.ref in refs)
            assert refs is not None
            for ref in refs:
                source = result.sources.get(ref.key)
                _require(source is not None, RejectionCode.MISSING)
                assert source is not None
                _check_ref(ref, source.head.ref)
                _current_at(source, at)
            accepted[memory_id] = snapshot
        except SourceContractError as exc:
            dropped[exc.code.value] += 1
    return FinalRead(at, accepted, dict(dropped))
