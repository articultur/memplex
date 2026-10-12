"""Lite V3 authority ledger, source CAS, and protected source sessions.

Publication writers remain disabled. Physical payload locators bind
row identity and ACL fields before raw IDs are projected to logical handles.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from memplex.auth import AuthorizationContext
from memplex.source_codec import (
    _NAMESPACE_IDENTITY_FIELDS,
    _PROVENANCE_IDENTITY_FIELDS,
    _ROOT_IDENTITY_FIELDS,
    _canonical,
    _project,
    decode_envelope,
    decode_ledger,
    encode_envelope,
    encode_ledger,
    output_digest,
    semantic_digest,
)
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
    SourceBusyError,
    SourceClass,
    SourceContractError,
    SourceHead,
    SourceIntegrityError,
    SourceKey,
    SourceKind,
    SourceLedger,
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
)
from memplex.source_validation import finalize_at, validate_sources
from memplex.storage.lite.durability import (
    LiteDurability,
    LitePair,
    LiteStorageIntegrityError,
    _canonical_json,
    _check_read_deadline,
    _empty_sync_state,
)
from memplex.storage.lite.single_writer import SingleWriterQueue, single_writer_enabled
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceAdmission,
    SourceCapability,
    SourceReadSession,
    SourceValidationHooks,
    _check_admission,
)

if TYPE_CHECKING:
    from memplex.storage.lite.store import LiteMemoryStore

_COLLECTIONS = {SourceKind.RAW_PARAGRAPH: "paragraphs", SourceKind.FUNCTION: "functions",
                SourceKind.FACT: "facts", SourceKind.PREFERENCE: "preferences",
                SourceKind.OBSERVATION: "observations"}
_ACL_FIELDS = ("tenant_id", "workspace_id", "owner_subject_id", "visibility", "owner",
               "agent_id", "session_id", "origin_session", "provenance")


def _require(condition: bool) -> None:
    if not condition:
        raise SourceIntegrityError()


def _payload_locator(key: SourceKey, payload: Mapping[str, object]) -> str:
    """Shared writer/reader physical binding; never infer absent persisted identity."""
    _require(type(payload.get("id")) is str and bool(payload["id"]))
    _require(payload.get("tenant_id") == key.tenant_id and payload.get("workspace_id") == key.workspace_id)
    _require(type(payload.get("owner_subject_id")) is str and bool(payload["owner_subject_id"]))
    _require(payload.get("visibility") in {"user", "workspace", "session"})
    if key.kind is not SourceKind.RAW_PARAGRAPH:
        _require(payload["id"] == key.logical_id and payload.get("memory_type") == key.kind.value)
    namespace = payload.get("namespace", {})
    _require(isinstance(namespace, Mapping))
    assert isinstance(namespace, Mapping)
    aliases = {alias: namespace[alias] for alias in
               ("memplex_tenant_id", "memplex_workspace_id", "memplex_subject_id") if alias in namespace}
    for alias, field in (("memplex_tenant_id", "tenant_id"), ("memplex_workspace_id", "workspace_id"),
                         ("memplex_subject_id", "owner_subject_id")):
        _require(alias not in aliases or aliases[alias] == payload.get(field))
    acl = {field: payload[field] for field in _ACL_FIELDS if field in payload}
    acl["namespace"] = aliases
    return _canonical_json({"id": payload["id"], "kind": key.kind.value,
                            "authority_id": key.authority_id, "tenant_id": key.tenant_id,
                            "workspace_id": key.workspace_id,
                            "acl_digest": hashlib.sha256(_canonical_json(acl)).hexdigest()}).decode()


def _canonical_payload(head: SourceHead, payload: Mapping[str, object]) -> SourceSnapshot:
    _require(head.payload_locator == _payload_locator(head.ref.key, payload))
    canonical = copy.deepcopy(dict(payload))
    canonical["id"] = head.ref.key.logical_id
    _require(semantic_digest(canonical, input_context=head.input_context, root_refs=head.root_refs)
             == head.ref.semantic_digest)
    return SourceSnapshot(head, canonical)


def _check_publication(snapshot: SourceSnapshot, ledger: SourceLedger) -> None:
    head, payload = snapshot.head, snapshot.payload
    namespace = payload.get("namespace", {})
    _require(isinstance(namespace, Mapping))
    assert isinstance(namespace, Mapping)
    carrier = namespace.get(CARRIER_KEY)
    if head.source_class is not SourceClass.DERIVED_V1:
        _require(carrier is None and not head.ref.key.logical_id.startswith(PUBLICATION_PREFIX))
        return
    _require(head.ref.key.kind is SourceKind.FACT and type(carrier) is str)
    assert isinstance(carrier, str)
    envelope = decode_envelope(carrier)
    publication = ledger.publications.get(envelope.publication_id)
    target = ledger.targets.get(envelope.derivation_key)
    _require(publication is not None and target is not None)
    assert publication is not None and target is not None
    _require(head.ref.key.logical_id == publication.publication_id == envelope.publication_id)
    _require(publication.state is PublicationState.ACTIVE and target.state is SourceState.LIVE)
    _require(publication.target_token == target.token
             and target.token.incarnation == envelope.target_incarnation
             and target.token.generation == envelope.target_generation
             and target.token.current_publication == envelope.publication_id)
    _require(publication.target_key == envelope.derivation_key
             and envelope.authority_id == ledger.authority.scope.authority_id
             and publication.algorithm_version == envelope.algorithm_version
             and publication.output_digest == envelope.output_digest == output_digest(payload)
             and publication.envelope_digest == hashlib.sha256(encode_envelope(envelope).encode()).hexdigest())
    refs = {ref.key: ref for ref in envelope.required_premises + envelope.authorization_dependencies}
    _require(set(publication.source_refs) == set(refs.values()))


_T = TypeVar("_T")


def _within_deadline(items: Iterable[_T], deadline_ns: int | None) -> Iterator[_T]:
    """Stop between traversal units, without claiming to interrupt an atomic operation."""
    for item in items:
        _check_read_deadline(deadline_ns)
        yield item
        _check_read_deadline(deadline_ns)


def reconcile_memory(memory: Mapping[str, Any], *, deadline_ns: int | None = None) -> tuple[SourceLedger, dict[SourceKey, SourceSnapshot]]:
    """Fail closed on orphan rows, headers, carriers, IDs, or publication bodies."""
    _check_read_deadline(deadline_ns)
    ledger = decode_ledger(memory["source_validity"])
    _check_read_deadline(deadline_ns)
    rows: dict[tuple[SourceKind, str], dict[str, object]] = {}
    physical_ids: set[str] = set()
    for kind, collection in _within_deadline(_COLLECTIONS.items(), deadline_ns):
        for row in _within_deadline(memory.get(collection, []), deadline_ns):
            _require(type(row) is dict and type(row.get("id")) is str and bool(row["id"]))
            _require(row["id"] not in physical_ids)
            physical_ids.add(row["id"])
            rows[(kind, row["id"])] = row
    snapshots: dict[SourceKey, SourceSnapshot] = {}
    used: set[tuple[SourceKind, str]] = set()
    for key, head in _within_deadline(ledger.sources.items(), deadline_ns):
        if head.state is SourceState.DELETED:
            _require((key.kind, key.logical_id) not in rows)
            snapshots[key] = SourceSnapshot(head, {})
            continue
        locator = json.loads(head.payload_locator)
        _require(type(locator) is dict and type(locator.get("id")) is str)
        address = (key.kind, locator["id"])
        _require(address in rows and address not in used)
        used.add(address)
        snapshot = _canonical_payload(head, rows[address])
        _check_read_deadline(deadline_ns)
        _check_publication(snapshot, ledger)
        snapshots[key] = snapshot
    _require(used == set(rows))
    active = {p.publication_id for p in _within_deadline(ledger.publications.values(), deadline_ns) if p.state is PublicationState.ACTIVE}
    bodies = {key.logical_id for key, snapshot in _within_deadline(snapshots.items(), deadline_ns)
              if snapshot.head.source_class is SourceClass.DERIVED_V1 and snapshot.head.state is SourceState.LIVE}
    _require(active == bodies)
    # Physical query IDs and logical handles must have one unambiguous meaning.
    logical_owners = {key.logical_id: key for key in _within_deadline(ledger.sources, deadline_ns)}
    for key, snapshot in _within_deadline(snapshots.items(), deadline_ns):
        if snapshot.head.state is not SourceState.DELETED:
            physical_id = json.loads(snapshot.head.payload_locator)["id"]
            _require(physical_id not in logical_owners or logical_owners[physical_id] == key)
    _check_read_deadline(deadline_ns)
    return ledger, snapshots


def validate_source_memory(memory: Mapping[str, Any], *, deadline_ns: int | None = None) -> None:
    """The durability loader must enforce V3 even without a model decoder."""
    try:
        if memory.get("schema_version") == 3:
            reconcile_memory(memory, deadline_ns=deadline_ns)
            return
        for collection in _within_deadline(_COLLECTIONS.values(), deadline_ns):
            for row in _within_deadline(memory.get(collection, []), deadline_ns):
                if isinstance(row, dict):
                    namespace = row.get("namespace", {})
                    if (isinstance(namespace, dict) and CARRIER_KEY in namespace
                            or str(row.get("id", "")).startswith(PUBLICATION_PREFIX)):
                        raise SourceIntegrityError()
    except SourceBusyError:
        raise
    except (SourceContractError, KeyError, TypeError, ValueError) as exc:
        raise LiteStorageIntegrityError("source-validity authority is quarantined") from exc


class _LiteReadSession:
    def __init__(self, pair: LitePair, deadline_ns: int) -> None:
        _check_read_deadline(deadline_ns)
        # The protected loader has reconciled this exact detached pair. Build
        # lookup indexes only; do not re-hash/reconcile the whole bank again.
        self._ledger = decode_ledger(pair.memory["source_validity"])
        self._deadline_ns = deadline_ns
        self._active = True
        self._ids: dict[str, SourceKey] = {}
        self._rows: dict[tuple[SourceKind, str], Mapping[str, object]] = {}
        for kind, collection in _within_deadline(_COLLECTIONS.items(), deadline_ns):
            for row in _within_deadline(pair.memory.get(collection, []), deadline_ns):
                self._rows[(kind, row["id"])] = row
        for key, head in _within_deadline(self._ledger.sources.items(), deadline_ns):
            self._ids[key.logical_id] = key
            if head.state is not SourceState.DELETED:
                self._ids[json.loads(head.payload_locator)["id"]] = key
        self._check()

    def _check(self) -> None:
        if not self._active:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        if time.monotonic_ns() >= self._deadline_ns:
            raise SourceBusyError()

    def authority(self) -> AuthorityState:
        self._check()
        return self._ledger.authority

    def resolve_id(self, memory_id: str) -> SourceKey | None:
        self._check()
        return self._ids.get(memory_id)

    def source(self, key: SourceKey) -> SourceSnapshot | None:
        self._check()
        head = self._ledger.sources.get(key)
        if head is None:
            return None
        if head.state is SourceState.DELETED:
            return SourceSnapshot(head, {})
        row = self._rows[(key.kind, json.loads(head.payload_locator)["id"])]
        snapshot = _canonical_payload(head, row)
        self._check()
        return snapshot

    def target(self, derivation_key: str) -> TargetHead | None:
        self._check()
        return self._ledger.targets.get(derivation_key)

    def publication(self, publication_id: str) -> PublicationRecord | None:
        self._check()
        return self._ledger.publications.get(publication_id)

    def utc_now(self) -> datetime:
        self._check()
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        self._check()
        return time.monotonic_ns()


@contextmanager
def _read_lock(durability: LiteDurability, deadline_ns: int) -> Iterator[None]:
    """Bound lock acquisition, sharing the normal writer's process/flock locks."""
    with durability.writer_lock_until(deadline_ns):
        yield


def _advance(revision: int) -> int:
    if revision >= MAX_REVISION:
        raise SourceIntegrityError(RejectionCode.QUARANTINED)
    return revision + 1


def _acl_projection(payload: Mapping[str, object]) -> str:
    identity = {key: payload[key] for key in _ROOT_IDENTITY_FIELDS - {"id"} if key in payload}
    for container, fields in (("namespace", _NAMESPACE_IDENTITY_FIELDS),
                              ("provenance", _PROVENANCE_IDENTITY_FIELDS)):
        values = payload.get(container, {})
        if isinstance(values, Mapping):
            identity[container] = {key: values[key] for key in fields if key in values}
    return _canonical(identity)


def _compare_source(current: SourceSnapshot | None, command: SourceMutation) -> None:
    if current is None:
        if command.expected is not ExpectedAbsent.ABSENT:
            raise SourceContractError(RejectionCode.MISSING)
        return
    head = current.head
    if command.expected is ExpectedAbsent.ABSENT:
        raise SourceContractError(RejectionCode.DELETED if head.state is SourceState.DELETED
                                  else RejectionCode.SOURCE_CHANGED)
    expected, actual = command.expected, head.ref.token
    if (expected.incarnation != actual.incarnation
            or expected.content_revision != actual.content_revision):
        raise SourceContractError(RejectionCode.SOURCE_CHANGED)
    if expected.auth_revision != actual.auth_revision:
        raise SourceContractError(RejectionCode.AUTH_CHANGED)
    wanted = SourceState.DELETED if command.operation is SourceOperation.RECREATE else SourceState.LIVE
    if head.state is not wanted:
        raise SourceContractError(RejectionCode.DELETED if head.state is SourceState.DELETED
                                  else RejectionCode.QUARANTINED)
    if head.source_class is not command.source_class:
        raise SourceContractError(RejectionCode.UNSUPPORTED_SEMANTICS)


def _mutation_head(command: SourceMutation, current: SourceSnapshot | None,
                   payload: Mapping[str, object] | None, commit_seq: int) -> SourceHead:
    if command.operation is SourceOperation.DELETE:
        assert current is not None
        token = replace(current.head.ref.token,
                        content_revision=_advance(current.head.ref.token.content_revision))
        return SourceHead(replace(current.head.ref, token=token), SourceState.DELETED,
                          command.source_class, "", commit_seq, {}, ())
    assert payload is not None
    locator = _payload_locator(command.key, payload)
    canonical = {**payload, "id": command.key.logical_id}
    digest = semantic_digest(canonical, input_context=command.input_context, root_refs=command.root_refs)
    if current is None or command.operation is SourceOperation.RECREATE:
        token = SourceToken(str(uuid.uuid4()), 1, 1)
    else:
        content_changed = (_canonical(_project(current.payload, output=False))
                           != _canonical(_project(canonical, output=False))
                           or current.head.input_context != command.input_context
                           or current.head.root_refs != command.root_refs
                           or json.loads(current.head.payload_locator)["id"] != payload["id"])
        auth_changed = _acl_projection(current.payload) != _acl_projection(payload)
        old = current.head.ref.token
        token = SourceToken(old.incarnation,
                            _advance(old.content_revision) if content_changed else old.content_revision,
                            _advance(old.auth_revision) if auth_changed else old.auth_revision)
    ref = SourceRef(command.key, token, 1, digest)
    return SourceHead(ref, SourceState.LIVE, command.source_class, locator, commit_seq,
                      command.input_context, command.root_refs)


def _source_target(base: LitePair, ledger: SourceLedger, command: SourceMutation,
                   current: SourceSnapshot | None, head: SourceHead,
                   payload: Mapping[str, object] | None) -> LitePair:
    memory = copy.deepcopy(base.memory)
    collection = _COLLECTIONS[command.key.kind]
    if current is not None and current.head.state is not SourceState.DELETED:
        physical_id = json.loads(current.head.payload_locator)["id"]
        memory[collection] = [row for row in memory[collection] if row["id"] != physical_id]
    if payload is not None:
        memory.setdefault(collection, []).append(dict(payload))
    if command.operation is SourceOperation.DELETE:
        memory["edges"] = [edge for edge in memory["edges"]
                           if command.key.logical_id not in {edge["source"], edge["target"]}]
    authority = replace(ledger.authority, commit_seq=head.last_commit_seq)
    memory["source_validity"] = encode_ledger(replace(ledger, authority=authority,
                                                    sources={**ledger.sources, command.key: head}))
    changelog = copy.deepcopy(base.changelog)
    changelog.append({"func_id": command.key.logical_id, "timestamp": datetime.now(UTC).isoformat(),
                      "event_type": command.operation.value, "description": "Source " + command.operation.value,
                      "source": "", "actor": "source-validity-v1"})
    return LitePair(memory, changelog, base.generation + 1, uuid.uuid4().hex)


class LiteSourceVersionStore:
    """Internal source transactions; publication and runtime activation stay disabled."""

    def __init__(self, durability: LiteDurability, *, owner: LiteMemoryStore | None = None) -> None:
        if type(durability) is not LiteDurability:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        if owner is not None:
            from memplex.storage.lite.store import LiteMemoryStore

            if type(owner) is not LiteMemoryStore or owner._durability is not durability:
                raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        self._owner = owner
        self._durability = durability
        self._hooks: SourceValidationHooks | None = None
        self._scope: AuthorityScope | None = None

    def _install_hooks(self, hooks: SourceValidationHooks, *, scope: AuthorityScope, issuer: object) -> None:
        if (issuer is not _RESOURCE_ISSUER or type(hooks) is not SourceValidationHooks
                or type(scope) is not AuthorityScope):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        self._hooks = hooks
        self._scope = scope

    def initialize_authority(self, scope: AuthorityScope, policy: PolicySnapshot, *,
                             admission: SourceAdmission) -> AuthorityState:
        _check_admission(admission, self._durability, scope)
        if os.environ.get("MEMPLEX_LITE_SQLITE_AUTHORITY", ""):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        with self._durability.writer_lock():
            # The official alternate durable authority is not a rebuildable
            # FTS/vector cache. Even an incomplete prior authority is not new.
            sqlite_path = self._durability._memory_path.parent / "shadow_v2.sqlite3"
            if any(path.exists() for path in (sqlite_path, sqlite_path.with_name(sqlite_path.name + "-wal"),
                                             sqlite_path.with_name(sqlite_path.name + "-shm"))):
                raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
            base = self._durability._load_authoritative_locked()
            if (base.generation != 0 or base.transaction_id != "empty" or base.changelog
                    or any(base.memory.get(name) for name in (*_COLLECTIONS.values(), "edges"))
                    or base.memory.get("sync") != _empty_sync_state()):
                raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
            authority = AuthorityState(scope, SourceState.LIVE, 1, policy, 1)
            memory = copy.deepcopy(base.memory)
            memory.update(schema_version=3, source_validity=encode_ledger(SourceLedger(authority, {}, {}, {})))
            memory.setdefault("paragraphs", [])
            target = LitePair(memory, [], 1, uuid.uuid4().hex)
            committed = self._durability.commit_locked(base, target, base_verified=True)
            if self._owner is not None:
                self._owner._publish_pair(committed)
        self._install_hooks(admission._hooks, scope=scope, issuer=_RESOURCE_ISSUER)
        return authority

    @contextmanager
    def read_fence(self, authorization: AuthorizationContext, *,
                   deadline_ns: int) -> Iterator[SourceReadSession]:
        if (self._hooks is None or type(authorization) is not AuthorizationContext
                or type(deadline_ns) is not int or os.environ.get("MEMPLEX_LITE_SQLITE_AUTHORITY", "")):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        end = min(deadline_ns, time.monotonic_ns() + 250_000_000)
        with _read_lock(self._durability, end):
            pair = self._durability._load_authoritative_locked(deadline_ns=end)
            _check_read_deadline(end)
            if pair.memory.get("schema_version") != 3:
                raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
            session = _LiteReadSession(pair, end)
            try:
                authority = session.authority()
                if (authority.scope != self._scope or authority.state is not SourceState.LIVE
                        or authorization.principal.tenant_id != authority.scope.tenant_id
                        or authorization.workspace_id != authority.scope.workspace_id):
                    raise SourceContractError(RejectionCode.DENIED)
                yield session
            finally:
                session._active = False

    def mutate_source(self, authorization: AuthorizationContext, command: SourceMutation, *,
                      deadline_ns: int) -> SourceReceipt:
        if (self._hooks is None or self._owner is None or type(authorization) is not AuthorizationContext
                or type(command) is not SourceMutation or type(deadline_ns) is not int
                or os.environ.get("MEMPLEX_LITE_SQLITE_AUTHORITY", "")
                or self._owner._sync_repository._capture_policy.mode != "off"):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        if (command.source_class not in {SourceClass.DIRECT_INPUT, SourceClass.CAPTURED_INPUT}
                or command.key.logical_id.startswith(PUBLICATION_PREFIX)):
            raise SourceContractError(RejectionCode.UNSUPPORTED_SEMANTICS)
        if single_writer_enabled():
            writer = getattr(self._durability, "_single_writer", None)
            if writer is None:
                writer = SingleWriterQueue()
                self._durability._single_writer = writer
            return writer.submit_until(lambda: self._mutate_locked(authorization, command, deadline_ns), deadline_ns)
        return self._mutate_locked(authorization, command, deadline_ns)

    def _check_source_inputs(self, session: _LiteReadSession, authorization: AuthorizationContext,
                             command: SourceMutation, deadline_ns: int) -> None:
        assert self._hooks is not None
        if (command.source_class is SourceClass.CAPTURED_INPUT
                and command.key.kind is not SourceKind.RAW_PARAGRAPH
                and (command.key.kind not in {SourceKind.FUNCTION, SourceKind.FACT, SourceKind.PREFERENCE}
                     or not command.root_refs
                     or any(ref.key.kind is not SourceKind.RAW_PARAGRAPH for ref in command.root_refs))):
            raise SourceContractError(RejectionCode.INVALID_UPSTREAM)
        roots = [ref.key.logical_id for ref in command.root_refs]
        result = validate_sources(session, roots, authorization=authorization,
                                  own_acl=self._hooks.own_acl, safe_for_model=self._hooks.safe_for_model,
                                  limits=ValidationLimits(max_nodes_per_root=127, max_depth=15,
                                                          max_unique_reads=4999),
                                  deadline_ns=deadline_ns, allow_captured_inputs=True)
        if result.rejected:
            raise SourceContractError(next(iter(result.rejected.values())))
        # The new source is one additional root above every declared input.
        # Validate dependencies without forbidding revocation of this source's
        # own safety/lifecycle state; a dependency back to it is still a cycle.
        if command.key in result.sources:
            raise SourceContractError(RejectionCode.CYCLE)
        if len(result.sources) > 127:
            raise SourceContractError(RejectionCode.BUDGET_EXHAUSTED)
        for ref in command.root_refs:
            snapshot = result.accepted.get(ref.key.logical_id)
            if snapshot is None or snapshot.head.ref != ref:
                raise SourceContractError(RejectionCode.SOURCE_CHANGED)
        if len(finalize_at(result, at=session.utc_now()).accepted) != len(result.accepted):
            raise SourceContractError(RejectionCode.EXPIRED)

    def _mutate_locked(self, authorization: AuthorizationContext, command: SourceMutation,
                       deadline_ns: int) -> SourceReceipt:
        assert self._hooks is not None and self._owner is not None
        with self._durability.writer_lock_until(deadline_ns):
            base = self._durability._load_authoritative_locked(deadline_ns=deadline_ns)
            if base.memory.get("schema_version") != 3:
                raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
            session = _LiteReadSession(base, deadline_ns)
            try:
                ledger = session._ledger
                scope = ledger.authority.scope
                if (scope != self._scope or ledger.authority.state is not SourceState.LIVE
                        or (command.key.authority_id, command.key.tenant_id, command.key.workspace_id)
                        != (scope.authority_id, scope.tenant_id, scope.workspace_id)
                        or authorization.principal.tenant_id != scope.tenant_id
                        or authorization.workspace_id != scope.workspace_id):
                    raise SourceContractError(RejectionCode.DENIED)
                if any(key.logical_id == command.key.logical_id and key != command.key for key in ledger.sources):
                    raise SourceContractError(RejectionCode.MALFORMED)
                current = session.source(command.key)
                if (current is not None and current.head.state is SourceState.LIVE
                        and not self._hooks.own_acl(current, authorization)):
                    raise SourceContractError(RejectionCode.DENIED)
                _compare_source(current, command)
                payload = None if command.payload is None else json.loads(_canonical(command.payload))
                head = _mutation_head(command, current, payload, _advance(ledger.authority.commit_seq))
                if payload is not None:
                    if not self._hooks.own_acl(SourceSnapshot(head, payload), authorization):
                        raise SourceContractError(RejectionCode.DENIED)
                    self._check_source_inputs(session, authorization, command, deadline_ns)
                target = _source_target(base, ledger, command, current, head, payload)
                _check_read_deadline(deadline_ns)
                committed = self._durability.commit_locked(base, target, base_verified=True, deadline_ns=deadline_ns)
                self._owner._publish_pair(committed)
                return SourceReceipt(head.ref, head.state, head.last_commit_seq)
            except BaseException:
                self._owner._pair_fingerprint = None
                self._owner._committed_pair = None
                self._owner._committed_record = None
                raise
            finally:
                session._active = False

    def publish_if_current(self, authorization: AuthorizationContext, command: PublishCommand, *,
                           deadline_ns: int) -> PublicationReceipt:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def delete_target(self, authorization: AuthorizationContext, derivation_key: str,
                      expected: TargetToken, *, deadline_ns: int) -> TargetHead:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def recreate_target(self, authorization: AuthorizationContext, command: PublishCommand,
                        expected_deleted: TargetToken, *, deadline_ns: int) -> PublicationReceipt:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
