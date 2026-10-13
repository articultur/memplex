"""Internal PostgreSQL source fence/CAS. Deployment and legacy routing stay off.

Only source-aware transactions participate in this intermediate lock protocol.
Resource-issued admission/hooks are mandatory; publication belongs to Task 7.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from memplex.auth import AuthorizationContext
from memplex.source_codec import (
    _NAMESPACE_IDENTITY_FIELDS,
    _PROVENANCE_IDENTITY_FIELDS,
    _ROOT_IDENTITY_FIELDS,
    _canonical,
    _decode_value,
    _project,
    _wire,
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
    PublishCommand,
    RejectionCode,
    SourceBusyError,
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
    _uuid,
)
from memplex.source_validation import finalize_at, validate_sources
from memplex.storage.migrations import PostgresTargetIdentity
from memplex.storage.pool import _remaining_transaction_ms as _remaining_ms
from memplex.storage.pool import _target_key, validate_ready_postgres_pool
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceAdmission,
    SourceReadSession,
    SourceValidationHooks,
)

if TYPE_CHECKING:
    from memplex.storage.postgres import PostgresMemoryStore

_TABLES = {SourceKind.RAW_PARAGRAPH: "memplex_paragraphs", SourceKind.FUNCTION: "memplex_functions",
           SourceKind.FACT: "memplex_facts", SourceKind.PREFERENCE: "memplex_preferences",
           SourceKind.OBSERVATION: "memplex_observations"}
_IDENTITY_COLUMNS = "tenant_id, owner_subject, workspace, visibility, source_agent, source_session"
_HEAD_COLUMNS = ("authority_id::text, kind, logical_id, incarnation::text, content_revision, auth_revision, "
                 "state, source_class, payload_locator, digest_version, semantic_digest, last_commit_seq, "
                 "head_data, " + _IDENTITY_COLUMNS)


def _source_lock_key(target: PostgresTargetIdentity, authority_id: str) -> int:
    _target_key(target)
    _uuid(authority_id)
    # Advisory locks already live in a server/database namespace. Transport
    # metadata differs for Unix/TCP aliases and must not split one authority.
    data = b"memplex/source-validity/v1\0" + _canonical([target.database, target.schema, authority_id]).encode()
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "big", signed=True)


def _admit_current_policy(context: AuthorizationContext, policy: PolicySnapshot) -> None:
    principal = context.principal
    current = (principal.authentication_id, principal.tenant_id, principal.subject_id,
               context.workspace_id, context.agent_id, principal.roles)
    if not any(current == (entry.authentication_id, entry.tenant_id, entry.subject_id,
                           entry.workspace_id, entry.agent_id, entry.roles) for entry in policy.entries):
        raise SourceContractError(RejectionCode.DENIED)


def _integrity(condition: bool) -> None:
    if not condition:
        raise SourceIntegrityError()


def _advance(value: int) -> int:
    if value >= MAX_REVISION:
        raise SourceIntegrityError()
    return value + 1


def _scope_sql(alias: str = "") -> str:
    from memplex.storage.postgres import _acl_scope_sql

    column = f"{alias}." if alias else ""
    return column + "workspace = current_setting('memplex.workspace_id', true) AND " + _acl_scope_sql(alias)


def _identity(payload: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(payload.get(key) for key in ("tenant_id", "owner_subject_id", "workspace_id",
                                            "visibility", "agent_id", "session_id"))


def _acl_projection(payload: Mapping[str, object], *, audit: bool = True) -> str:
    result = {key: payload[key] for key in _ROOT_IDENTITY_FIELDS - {"id"} if key in payload}
    for container, fields in (("namespace", _NAMESPACE_IDENTITY_FIELDS),
                              ("provenance", _PROVENANCE_IDENTITY_FIELDS)):
        values = payload.get(container, {})
        if isinstance(values, Mapping):
            result[container] = {key: values[key] for key in fields if key in values
                                 and (audit or container != "provenance" or key != "request_id")}
    return _canonical(result)


def _locator(key: SourceKey, payload: Mapping[str, object]) -> str:
    _integrity(type(payload.get("id")) is str and bool(payload["id"]))
    _integrity(payload.get("tenant_id") == key.tenant_id and payload.get("workspace_id") == key.workspace_id)
    if key.kind is not SourceKind.RAW_PARAGRAPH:
        _integrity(payload["id"] == key.logical_id and payload.get("memory_type") == key.kind.value)
    return _canonical({"id": payload["id"], "kind": key.kind.value,
                       "authority_id": key.authority_id, "tenant_id": key.tenant_id,
                       "workspace_id": key.workspace_id,
                       "acl_digest": hashlib.sha256(_acl_projection(payload).encode()).hexdigest()})


def _bound_payload(command: SourceMutation, authorization: AuthorizationContext) -> dict[str, Any] | None:
    if command.payload is None:
        return None
    payload = json.loads(_canonical(command.payload))
    # PG uses the logical handle as its physical row ID for every kind.
    # This keeps the authority-wide unique ID constraint sufficient even
    # when RLS hides another principal's source.
    if payload.get("id") != command.key.logical_id:
        raise SourceContractError(RejectionCode.MALFORMED)
    visibility = payload.get("visibility", "workspace")
    if visibility not in {"workspace", "user", "session"} or (
            visibility == "session" and not authorization.session_id):
        raise SourceContractError(RejectionCode.DENIED)
    namespace = payload.get("namespace", {})
    if not isinstance(namespace, dict) or CARRIER_KEY in namespace:
        raise SourceContractError(RejectionCode.UNSUPPORTED_SEMANTICS)
    payload.update(tenant_id=authorization.principal.tenant_id, workspace_id=authorization.workspace_id,
                   owner_subject_id=authorization.principal.subject_id, owner=authorization.principal.subject_id,
                   visibility=visibility, agent_id=authorization.agent_id, session_id=authorization.session_id,
                   origin_session=authorization.session_id or None)
    payload["namespace"] = {**namespace, "memplex_tenant_id": authorization.principal.tenant_id,
                            "memplex_workspace_id": authorization.workspace_id,
                            "memplex_subject_id": authorization.principal.subject_id}
    from memplex.factual_lineage import capture_audit_provenance

    audit = capture_audit_provenance(SimpleNamespace(provenance=payload.get("provenance")))
    payload["provenance"] = {**audit, **authorization.provenance, "agent_id": authorization.agent_id,
                             "session_id": authorization.session_id,
                             "authentication_id": authorization.principal.authentication_id or "",
                             "request_id": authorization.request_id}
    if command.key.kind is SourceKind.FUNCTION:
        from memplex.models import Function
        from memplex.storage.postgres import _func_to_json

        search = _func_to_json(Function.from_dict(payload))
        payload.update(trigger_text=search["trigger_text"], action_text=search["action_text"])
    return payload


class _PostgresReadSession:
    def __init__(self, cursor: Any, scope: AuthorityScope, deadline_ns: int) -> None:
        self._cursor, self._scope, self._deadline_ns = cursor, scope, deadline_ns
        self._active = True
        self._linearized_at: datetime | None = None
        self._authority: AuthorityState | None = None

    def _check(self) -> None:
        if not self._active:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        _remaining_ms(self._deadline_ns)

    def _execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        self._check()
        if self._linearized_at is not None:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        self._cursor.execute(sql, parameters)
        self._check()

    def authority(self) -> AuthorityState:
        self._check()
        if self._authority is None:
            self._execute("SELECT state, policy_revision, commit_seq, policy, contract_version "
                          "FROM memplex_source_authorities WHERE authority_id=%s AND tenant_id=%s AND workspace=%s",
                          (self._scope.authority_id, self._scope.tenant_id, self._scope.workspace_id))
            row = self._cursor.fetchone()
            if row is None:
                raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
            policy = cast(PolicySnapshot, _decode_value(PolicySnapshot, row[3]))
            _integrity(policy.revision == row[1])
            self._authority = AuthorityState(self._scope, SourceState(row[0]), row[4], policy, row[2])
        return self._authority

    def resolve_id(self, memory_id: str) -> SourceKey | None:
        self._execute("SELECT kind, logical_id FROM memplex_source_heads "
                      f"WHERE authority_id=%s AND logical_id=%s AND {_scope_sql()}",
                      (self._scope.authority_id, memory_id))
        rows = self._cursor.fetchall()
        if len(rows) > 1:
            raise SourceContractError(RejectionCode.MALFORMED)
        if not rows:
            return None
        return SourceKey(self._scope.authority_id, self._scope.tenant_id, self._scope.workspace_id,
                         SourceKind(rows[0][0]), rows[0][1])

    def _head(self, key: SourceKey) -> tuple[SourceHead, tuple[object, ...]] | None:
        if (key.authority_id, key.tenant_id, key.workspace_id) != (
                self._scope.authority_id, self._scope.tenant_id, self._scope.workspace_id):
            raise SourceContractError(RejectionCode.DENIED)
        self._execute(f"SELECT {_HEAD_COLUMNS} FROM memplex_source_heads "
                      f"WHERE authority_id=%s AND kind=%s AND logical_id=%s AND {_scope_sql()}",
                      (key.authority_id, key.kind.value, key.logical_id))
        row = self._cursor.fetchone()
        if row is None:
            return None
        _integrity((row[0], row[1], row[2]) == (key.authority_id, key.kind.value, key.logical_id))
        _integrity(type(row[12]) is dict and set(row[12]) == {"input_context", "root_refs"})
        head = _decode_value(SourceHead, {
            "ref": {"key": _wire(key), "token": {"incarnation": row[3], "content_revision": row[4],
                                                   "auth_revision": row[5]},
                    "digest_version": row[9], "semantic_digest": row[10]},
            "state": row[6], "source_class": row[7], "payload_locator": row[8], "last_commit_seq": row[11],
            **row[12],
        })
        _integrity(head.last_commit_seq <= self.authority().commit_seq)
        return head, tuple(row[13:])

    def source(self, key: SourceKey) -> SourceSnapshot | None:
        found = self._head(key)
        if found is None:
            return None
        head, identity = found
        if head.state is SourceState.DELETED:
            return SourceSnapshot(head, {})
        locator = json.loads(head.payload_locator)
        self._execute(f"SELECT id, data, {_IDENTITY_COLUMNS} FROM {_TABLES[key.kind]} "
                      f"WHERE id=%s AND {_scope_sql()}", (locator["id"],))
        row = self._cursor.fetchone()
        _integrity(row is not None)
        assert row is not None
        payload = json.loads(_canonical(row[1]))
        _integrity(row[0] == payload.get("id") and tuple(row[2:]) == identity == _identity(payload))
        _integrity(head.payload_locator == _locator(key, payload))
        payload["id"] = key.logical_id
        _integrity(semantic_digest(payload, input_context=head.input_context, root_refs=head.root_refs)
                   == head.ref.semantic_digest)
        return SourceSnapshot(head, payload)

    def _record(self, kind: str, key: str, cls: type[Any]) -> Any:
        self._execute("SELECT data FROM memplex_derivation_records "
                      f"WHERE authority_id=%s AND record_kind=%s AND record_key=%s AND {_scope_sql()}",
                      (self._scope.authority_id, kind, key))
        row = self._cursor.fetchone()
        if row is None:
            return None
        data = dict(row[0])
        _integrity(type(data.pop("contract_version", None)) is int and row[0]["contract_version"] == 1)
        value = _decode_value(cls, data)
        _integrity((value.derivation_key if kind == "target" else value.publication_id) == key)
        _integrity((value.last_commit_seq if kind == "target" else value.commit_seq) <= self.authority().commit_seq)
        return value

    def target(self, derivation_key: str) -> TargetHead | None:
        return cast(TargetHead | None, self._record("target", derivation_key, TargetHead))

    def publication(self, publication_id: str) -> PublicationRecord | None:
        return cast(PublicationRecord | None, self._record("publication", publication_id, PublicationRecord))

    def utc_now(self) -> datetime:
        self._check()
        if self._linearized_at is None:
            self._execute("SELECT clock_timestamp()")
            self._linearized_at = self._cursor.fetchone()[0]
            _integrity(isinstance(self._linearized_at, datetime) and self._linearized_at.tzinfo is not None)
            self._linearized_at = self._linearized_at.astimezone(UTC)
        return self._linearized_at

    def monotonic_ns(self) -> int:
        self._check()
        return time.monotonic_ns()


def _compare_source(current: SourceSnapshot | None, command: SourceMutation) -> None:
    if current is None:
        if command.expected is not ExpectedAbsent.ABSENT:
            raise SourceContractError(RejectionCode.MISSING)
        return
    head = current.head
    if command.expected is ExpectedAbsent.ABSENT:
        raise SourceContractError(RejectionCode.DELETED if head.state is SourceState.DELETED else RejectionCode.SOURCE_CHANGED)
    if (command.expected.incarnation, command.expected.content_revision) != (
            head.ref.token.incarnation, head.ref.token.content_revision):
        raise SourceContractError(RejectionCode.SOURCE_CHANGED)
    if command.expected.auth_revision != head.ref.token.auth_revision:
        raise SourceContractError(RejectionCode.AUTH_CHANGED)
    wanted = SourceState.DELETED if command.operation is SourceOperation.RECREATE else SourceState.LIVE
    if head.state is not wanted:
        raise SourceContractError(RejectionCode.DELETED if head.state is SourceState.DELETED else RejectionCode.QUARANTINED)
    if head.source_class is not command.source_class:
        raise SourceContractError(RejectionCode.UNSUPPORTED_SEMANTICS)


def _mutation_head(command: SourceMutation, current: SourceSnapshot | None,
                   payload: Mapping[str, object] | None, commit_seq: int) -> SourceHead:
    if command.operation is SourceOperation.DELETE:
        assert current is not None
        token = replace(current.head.ref.token, content_revision=_advance(current.head.ref.token.content_revision))
        return SourceHead(replace(current.head.ref, token=token), SourceState.DELETED,
                          command.source_class, "", commit_seq, {}, ())
    assert payload is not None
    locator = _locator(command.key, payload)
    canonical = {**payload, "id": command.key.logical_id}
    digest = semantic_digest(canonical, input_context=command.input_context, root_refs=command.root_refs)
    if current is None or command.operation is SourceOperation.RECREATE:
        token = SourceToken(str(uuid.uuid4()), 1, 1)
    else:
        content_changed = (_canonical(_project(current.payload, output=False)) != _canonical(_project(canonical, output=False))
                           or current.head.input_context != command.input_context or current.head.root_refs != command.root_refs
                           or json.loads(current.head.payload_locator)["id"] != payload["id"])
        auth_changed = _acl_projection(current.payload, audit=False) != _acl_projection(payload, audit=False)
        old = current.head.ref.token
        token = SourceToken(old.incarnation, _advance(old.content_revision) if content_changed else old.content_revision,
                            _advance(old.auth_revision) if auth_changed else old.auth_revision)
    return SourceHead(SourceRef(command.key, token, 1, digest), SourceState.LIVE,
                      command.source_class, locator, commit_seq, command.input_context, command.root_refs)


class PostgresSourceVersionStore:
    def __init__(self, owner: PostgresMemoryStore, *, resource: object) -> None:
        from memplex.storage.postgres import PostgresMemoryStore
        from memplex.storage.postgres_resources import PostgresStorageResources

        if (type(owner) is not PostgresMemoryStore or type(resource) is not PostgresStorageResources
                or resource.ready_pool is not owner._ready_pool):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        validate_ready_postgres_pool(owner._ready_pool)
        self._owner, self._resource = owner, resource
        self._hooks: SourceValidationHooks | None = None
        self._scope: AuthorityScope | None = None

    def _install_hooks(self, hooks: SourceValidationHooks, *, scope: AuthorityScope, issuer: object) -> None:
        if issuer is not _RESOURCE_ISSUER or type(hooks) is not SourceValidationHooks or type(scope) is not AuthorityScope:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        self._hooks, self._scope = hooks, scope

    def _preflight(self, authorization: AuthorizationContext, deadline_ns: int) -> AuthorityScope:
        if (type(self) is not PostgresSourceVersionStore or self._hooks is None or self._scope is None or type(authorization) is not AuthorizationContext
                or type(deadline_ns) is not int or self._owner._sync_capture_policy.mode != "off"):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        validate_ready_postgres_pool(self._owner._ready_pool)
        if (authorization.principal.tenant_id, authorization.workspace_id) != (self._scope.tenant_id, self._scope.workspace_id):
            raise SourceContractError(RejectionCode.DENIED)
        _remaining_ms(deadline_ns)
        return self._scope

    @contextmanager
    def _transaction(self, authorization: AuthorizationContext, deadline_ns: int, *, shared: bool) -> Iterator[_PostgresReadSession]:
        scope = self._preflight(authorization, deadline_ns)
        try:
            with self._owner._pool_manager.transaction(self._owner._bind_transaction_scope, authorization,
                                                       deadline_ns=deadline_ns) as (_, cursor):
                session = _PostgresReadSession(cursor, scope, deadline_ns)
                try:
                    lock = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
                    session._execute(f"SELECT {lock}(%s)", (_source_lock_key(self._owner._ready_pool.target, scope.authority_id),))
                    yield session
                    session._check()
                finally:
                    session._active = False
        except BaseException as exc:
            if getattr(exc, "pgcode", None) == "23505":
                raise SourceContractError(RejectionCode.MALFORMED) from exc
            if getattr(exc, "pgcode", None) in {"55P03", "57014", "40P01", "40001"}:
                raise SourceBusyError() from exc
            raise

    def initialize_authority(self, scope: AuthorityScope, policy: PolicySnapshot, *, admission: SourceAdmission) -> AuthorityState:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def _admit(self, session: _PostgresReadSession, authorization: AuthorizationContext) -> AuthorityState:
        authority = session.authority()
        if authority.scope != self._scope or authority.state is not SourceState.LIVE:
            raise SourceContractError(RejectionCode.QUARANTINED)
        _admit_current_policy(authorization, authority.policy)
        return authority

    @contextmanager
    def read_fence(self, authorization: AuthorizationContext, *, deadline_ns: int) -> Iterator[SourceReadSession]:
        if type(deadline_ns) is not int:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        end = min(deadline_ns, time.monotonic_ns() + 250_000_000)
        with self._transaction(authorization, end, shared=True) as session:
            self._admit(session, authorization)
            yield session

    def _check_source_inputs(self, session: _PostgresReadSession, authorization: AuthorizationContext,
                             command: SourceMutation) -> None:
        assert self._hooks is not None
        if (command.source_class is SourceClass.CAPTURED_INPUT and command.key.kind is not SourceKind.RAW_PARAGRAPH
                and (command.key.kind not in {SourceKind.FUNCTION, SourceKind.FACT, SourceKind.PREFERENCE}
                     or not command.root_refs or any(ref.key.kind is not SourceKind.RAW_PARAGRAPH for ref in command.root_refs))):
            raise SourceContractError(RejectionCode.INVALID_UPSTREAM)
        result = validate_sources(session, [ref.key.logical_id for ref in command.root_refs], authorization=authorization,
                                  own_acl=self._hooks.own_acl, safe_for_model=self._hooks.safe_for_model,
                                  limits=ValidationLimits(max_nodes_per_root=127, max_depth=15, max_unique_reads=4999),
                                  deadline_ns=session._deadline_ns, allow_captured_inputs=True)
        if result.rejected:
            raise SourceContractError(next(iter(result.rejected.values())))
        if command.key in result.sources:
            raise SourceContractError(RejectionCode.CYCLE)
        if len(result.sources) > 127:
            raise SourceContractError(RejectionCode.BUDGET_EXHAUSTED)
        for ref in command.root_refs:
            if result.accepted.get(ref.key.logical_id) is None or result.accepted[ref.key.logical_id].head.ref != ref:
                raise SourceContractError(RejectionCode.SOURCE_CHANGED)
        # Mutation still has writes to perform: this clock is a validation
        # time, not the final read linearization point that seals read sessions.
        session._execute("SELECT clock_timestamp()")
        if len(finalize_at(result, at=session._cursor.fetchone()[0]).accepted) != len(result.accepted):
            raise SourceContractError(RejectionCode.EXPIRED)

    def mutate_source(self, authorization: AuthorizationContext, command: SourceMutation, *, deadline_ns: int) -> SourceReceipt:
        if type(command) is not SourceMutation:
            raise SourceContractError(RejectionCode.MALFORMED)
        if command.source_class not in {SourceClass.DIRECT_INPUT, SourceClass.CAPTURED_INPUT} or command.key.logical_id.startswith(PUBLICATION_PREFIX):
            raise SourceContractError(RejectionCode.UNSUPPORTED_SEMANTICS)
        with self._transaction(authorization, deadline_ns, shared=False) as session:
            authority = self._admit(session, authorization)
            current = session.source(command.key)
            assert self._hooks is not None
            if current is not None and current.head.state is SourceState.LIVE and not self._hooks.own_acl(current, authorization):
                raise SourceContractError(RejectionCode.DENIED)
            _compare_source(current, command)
            payload = _bound_payload(command, authorization)
            head = _mutation_head(command, current, payload, _advance(authority.commit_seq))
            if payload is not None:
                if not self._hooks.own_acl(SourceSnapshot(head, {**payload, "id": command.key.logical_id}), authorization):
                    raise SourceContractError(RejectionCode.DENIED)
                self._check_source_inputs(session, authorization, command)
            self._write_source(session, authorization, command, current, head, payload)
            receipt = SourceReceipt(head.ref, head.state, head.last_commit_seq)
        return receipt

    def _write_source(self, session: _PostgresReadSession, authorization: AuthorizationContext,
                      command: SourceMutation, current: SourceSnapshot | None, head: SourceHead,
                      payload: dict[str, Any] | None) -> None:
        key = command.key
        if key.kind is SourceKind.FUNCTION:
            from memplex.storage.postgres import _function_write_lock_key

            session._execute("SELECT pg_advisory_xact_lock(%s)",
                             (_function_write_lock_key(self._owner._ready_pool.target, key.tenant_id),))
        replacing = current is not None and current.head.state is not SourceState.DELETED
        if replacing:
            assert current is not None
            physical_id = json.loads(current.head.payload_locator)["id"]
            if payload is None:
                session._execute(f"DELETE FROM {_TABLES[key.kind]} WHERE id=%s AND {_scope_sql()} RETURNING id", (physical_id,))
            else:
                extra = ""
                if key.kind is SourceKind.FUNCTION:
                    session._execute("SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute "
                                     "WHERE attrelid='memplex_functions'::regclass AND attname='embedding' "
                                     "AND attnum>0 AND NOT attisdropped)")
                    if session._cursor.fetchone()[0]:
                        extra = ", embedding=NULL"
                session._execute(f"UPDATE {_TABLES[key.kind]} SET data=%s::jsonb, "
                                 "owner_subject=%s,workspace=%s,visibility=%s,source_agent=%s,source_session=%s" + extra +
                                 f" WHERE id=%s AND {_scope_sql()} RETURNING id",
                                 (_canonical(payload), *_identity(payload)[1:], physical_id))
            _integrity(session._cursor.fetchone() == (physical_id,))
        elif payload is not None:
            # An INSERT cannot silently adopt an unregistered existing payload.
            timestamp_column = "created_at" if key.kind is SourceKind.OBSERVATION else "updated_at"
            session._execute(f"INSERT INTO {_TABLES[key.kind]} (id,data,{timestamp_column},{_IDENTITY_COLUMNS}) "
                             "VALUES (%s,%s::jsonb,clock_timestamp(),%s,%s,%s,%s,%s,%s)",
                             (payload["id"], _canonical(payload), *_identity(payload)))
        if payload is None:
            assert current is not None
            visibility = current.payload.get("visibility", "workspace")
        else:
            visibility = payload["visibility"]
        identity = (authorization.principal.tenant_id, authorization.principal.subject_id, authorization.workspace_id,
                    visibility,
                    authorization.agent_id, authorization.session_id)
        head_data = _canonical({"input_context": head.input_context, "root_refs": head.root_refs})
        session._execute("INSERT INTO memplex_source_heads "
                         "(authority_id,kind,logical_id,incarnation,content_revision,auth_revision,state,source_class,"
                         "payload_locator,digest_version,semantic_digest,last_commit_seq,head_data," + _IDENTITY_COLUMNS + ") "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s) "
                         "ON CONFLICT (authority_id,kind,logical_id) DO UPDATE SET "
                         "incarnation=EXCLUDED.incarnation,content_revision=EXCLUDED.content_revision,auth_revision=EXCLUDED.auth_revision,"
                         "state=EXCLUDED.state,source_class=EXCLUDED.source_class,payload_locator=EXCLUDED.payload_locator,"
                         "semantic_digest=EXCLUDED.semantic_digest,last_commit_seq=EXCLUDED.last_commit_seq,head_data=EXCLUDED.head_data,"
                         "owner_subject=EXCLUDED.owner_subject,visibility=EXCLUDED.visibility,source_agent=EXCLUDED.source_agent,"
                         "source_session=EXCLUDED.source_session "
                         f"WHERE {_scope_sql('memplex_source_heads')} RETURNING logical_id",
                         (key.authority_id, key.kind.value, key.logical_id, head.ref.token.incarnation,
                          head.ref.token.content_revision, head.ref.token.auth_revision, head.state.value,
                          head.source_class.value, head.payload_locator, head.ref.semantic_digest,
                          head.last_commit_seq, head_data, *identity))
        _integrity(session._cursor.fetchone() == (key.logical_id,))
        session._execute("UPDATE memplex_source_authorities SET commit_seq=%s "
                         "WHERE authority_id=%s AND tenant_id=%s AND workspace=%s AND commit_seq=%s RETURNING commit_seq",
                         (head.last_commit_seq, key.authority_id, key.tenant_id, key.workspace_id, session.authority().commit_seq))
        _integrity(session._cursor.fetchone() == (head.last_commit_seq,))

    def publish_if_current(self, authorization: AuthorizationContext, command: PublishCommand, *, deadline_ns: int) -> PublicationReceipt:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def delete_target(self, authorization: AuthorizationContext, derivation_key: str, expected: TargetToken, *, deadline_ns: int) -> TargetHead:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)

    def recreate_target(self, authorization: AuthorizationContext, command: PublishCommand, expected_deleted: TargetToken, *, deadline_ns: int) -> PublicationReceipt:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)


class _AuthorizedPostgresSources:
    """Bind every invocation explicitly, including delayed context-manager entry."""
    def __init__(self, backend: PostgresSourceVersionStore, context: AuthorizationContext) -> None:
        if type(backend) is not PostgresSourceVersionStore or type(context) is not AuthorizationContext:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        self._backend, self._context = backend, context

    def initialize_authority(self, scope: AuthorityScope, policy: PolicySnapshot, *, admission: SourceAdmission) -> AuthorityState:
        return self._backend.initialize_authority(scope, policy, admission=admission)

    def _check(self, authorization: AuthorizationContext) -> None:
        if authorization != self._context:
            raise SourceContractError(RejectionCode.DENIED)

    def read_fence(self, authorization: AuthorizationContext, *, deadline_ns: int) -> Any:
        self._check(authorization)
        return self._backend.read_fence(self._context, deadline_ns=deadline_ns)

    def mutate_source(self, authorization: AuthorizationContext, command: SourceMutation, *, deadline_ns: int) -> SourceReceipt:
        self._check(authorization)
        return self._backend.mutate_source(self._context, command, deadline_ns=deadline_ns)

    def publish_if_current(self, authorization: AuthorizationContext, command: PublishCommand, *, deadline_ns: int) -> PublicationReceipt:
        self._check(authorization)
        return self._backend.publish_if_current(self._context, command, deadline_ns=deadline_ns)

    def delete_target(self, authorization: AuthorizationContext, derivation_key: str, expected: TargetToken, *, deadline_ns: int) -> TargetHead:
        self._check(authorization)
        return self._backend.delete_target(self._context, derivation_key, expected, deadline_ns=deadline_ns)

    def recreate_target(self, authorization: AuthorizationContext, command: PublishCommand, expected_deleted: TargetToken, *, deadline_ns: int) -> PublicationReceipt:
        self._check(authorization)
        return self._backend.recreate_target(self._context, command, expected_deleted, deadline_ns=deadline_ns)
