"""Lite V3 authority ledger and protected read-only source sessions.

This adapter has no source/publication writers. Physical payload locators bind
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
from datetime import UTC, datetime
from typing import Any, TypeVar

from memplex.auth import AuthorizationContext
from memplex.source_codec import (
    decode_envelope,
    decode_ledger,
    encode_envelope,
    encode_ledger,
    output_digest,
    semantic_digest,
)
from memplex.source_contract import (
    CARRIER_KEY,
    PUBLICATION_PREFIX,
    AuthorityScope,
    AuthorityState,
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
    SourceReceipt,
    SourceSnapshot,
    SourceState,
    TargetHead,
    TargetToken,
)
from memplex.storage.lite.durability import (
    LiteDurability,
    LitePair,
    LiteStorageIntegrityError,
    _canonical_json,
    _check_read_deadline,
    _empty_sync_state,
    _load_fcntl,
    _process_lock_for,
)
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceAdmission,
    SourceCapability,
    SourceReadSession,
    SourceValidationHooks,
    _check_admission,
)

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
    remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
    if remaining <= 0 or durability._poisoned or getattr(durability._local, "depth", False):
        raise SourceBusyError()
    lock = _process_lock_for(durability._memory_path)
    if not lock.acquire(timeout=remaining):
        raise SourceBusyError()
    fcntl = _load_fcntl()
    try:
        with durability._lock_path.open("a+b") as handle:
            while True:
                if time.monotonic_ns() >= deadline_ns:
                    raise SourceBusyError()
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(min(0.001, max(0, (deadline_ns - time.monotonic_ns()) / 1_000_000_000)))
            try:
                durability._local.depth = 1
                yield
            finally:
                durability._local.depth = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


class LiteSourceVersionStore:
    """Read-only internal adapter. Source and publication writers remain disabled."""

    def __init__(self, durability: LiteDurability) -> None:
        if type(durability) is not LiteDurability:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
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
            self._durability.commit_locked(base, target, base_verified=True)
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
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def publish_if_current(self, authorization: AuthorizationContext, command: PublishCommand, *,
                           deadline_ns: int) -> PublicationReceipt:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def delete_target(self, authorization: AuthorizationContext, derivation_key: str,
                      expected: TargetToken, *, deadline_ns: int) -> TargetHead:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def recreate_target(self, authorization: AuthorizationContext, command: PublishCommand,
                        expected_deleted: TargetToken, *, deadline_ns: int) -> PublicationReceipt:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
