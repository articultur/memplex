"""Source-aware PostgreSQL transactions; native lock proofs require external PG.

Local protocol/error probes do not certify PostgreSQL locking. Ordinary legacy
writers are outside this intermediate adapter's concurrency contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import replace

import pytest

from memplex.auth import AuthorizationContext, Principal
from memplex.source_contract import (
    ALGORITHM_VERSION,
    AuthorityScope,
    PolicyEntry,
    PolicySnapshot,
    SourceBusyError,
    SourceContractError,
)
from memplex.storage.migrations import PostgresTargetIdentity
from memplex.storage.postgres_sources import _admit_current_policy, _remaining_ms, _source_lock_key

SCOPE = AuthorityScope("11111111-1111-4111-8111-111111111111", "source-tenant", "source-workspace")
CONTEXT = AuthorizationContext(
    Principal(SCOPE.tenant_id, "alice", frozenset({"member"}), "credential-alice"),
    SCOPE.workspace_id, agent_id="source-agent", session_id="session-alice",
)
POLICY = PolicySnapshot(1, (PolicyEntry("credential-alice", SCOPE.tenant_id, "alice",
                                      SCOPE.workspace_id, "source-agent", frozenset({"member"})),),
                        frozenset({ALGORITHM_VERSION}))
NATIVE = pytest.mark.skipif(not os.environ.get("MEMPLEX_TEST_POSTGRES_DSN"),
                           reason="native source-lock proof requires external PostgreSQL; local startup route closed")


def test_source_lock_key_has_stable_domain_and_authoritative_target():
    target = PostgresTargetIdentity("db", "schema", "127.0.0.1", 5432)
    data = b"memplex/source-validity/v1\0" + json.dumps(
        ["db", "schema", SCOPE.authority_id],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()
    assert _source_lock_key(target, SCOPE.authority_id) == int.from_bytes(hashlib.sha256(data).digest()[:8], "big", signed=True)
    assert _source_lock_key(replace(target, schema="other"), SCOPE.authority_id) != _source_lock_key(target, SCOPE.authority_id)
    assert _source_lock_key(replace(target, server_address=None, server_port=None), SCOPE.authority_id) == _source_lock_key(target, SCOPE.authority_id)
    assert _source_lock_key(target, "22222222-2222-4222-8222-222222222222") != _source_lock_key(target, SCOPE.authority_id)
    assert _source_lock_key(replace(target, database="ab", schema="c"), SCOPE.authority_id) != _source_lock_key(replace(target, database="a", schema="bc"), SCOPE.authority_id)
    with pytest.raises(TypeError):
        _source_lock_key(object(), SCOPE.authority_id)
    with pytest.raises(SourceContractError):
        _source_lock_key(target, "unverified-authority")


def test_deadline_never_disables_postgres_timeout():
    with pytest.raises(SourceBusyError):
        _remaining_ms(time.monotonic_ns() - 1)
    assert 1 <= _remaining_ms(time.monotonic_ns() + 20_000_000) <= 20


@pytest.mark.parametrize("field,value", [
    ("authentication_id", "other"), ("subject_id", "bob"),
    ("tenant_id", "other"), ("roles", frozenset({"admin"})),
])
def test_current_policy_rejects_changed_principal(field, value):
    _admit_current_policy(CONTEXT, POLICY)
    changed = replace(CONTEXT, principal=replace(CONTEXT.principal, **{field: value}))
    with pytest.raises(SourceContractError) as caught:
        _admit_current_policy(changed, POLICY)
    assert caught.value.code == "denied"


def _command(*, value="one", expected=None, operation=None, kind=None, name="source"):
    from memplex.models import Fact
    from memplex.source_contract import (
        ExpectedAbsent,
        SourceClass,
        SourceKey,
        SourceKind,
        SourceMutation,
        SourceOperation,
    )

    operation = operation or SourceOperation.CREATE
    payload = None if operation is SourceOperation.DELETE else Fact(
        id=name, subject="item", predicate="is", object_=value,
        tenant_id=SCOPE.tenant_id, workspace_id=SCOPE.workspace_id,
        owner_subject_id="alice", visibility="workspace",
    ).to_dict()
    return SourceMutation(SourceKey(SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id,
                                    kind or SourceKind.FACT, name),
                          expected if expected is not None else ExpectedAbsent.ABSENT,
                          operation, payload, SourceClass.DIRECT_INPUT, {}, ())


def _mutate(backend, command, context=CONTEXT):
    return backend.mutate_source(context, command, deadline_ns=time.monotonic_ns() + 2_000_000_000)


def _fence(backend, context=CONTEXT):
    return backend.read_fence(context, deadline_ns=time.monotonic_ns() + 250_000_000)


@pytest.fixture
def source_backend(pg_function_dsn, monkeypatch):
    """A real migrated schema with explicit test-only bootstrap and SQL tracing."""
    import psycopg2
    from psycopg2.extensions import connection, cursor

    from memplex.source_codec import _canonical
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.source_versions import _RESOURCE_ISSUER, SourceValidationHooks

    trace = []

    class TraceCursor(cursor):
        def execute(self, sql, variables=None):
            trace.append(("start", self.connection.get_backend_pid(), str(sql), variables))
            result = super().execute(sql, variables)
            trace.append(("finish", self.connection.get_backend_pid(), str(sql), variables))
            return result

    class TraceConnection(connection):
        def cursor(self, *args, **kwargs):
            return super().cursor(*args, cursor_factory=TraceCursor, **kwargs)

        def commit(self):
            super().commit()
            trace.append(("commit", self.get_backend_pid(), "", None))

        def rollback(self):
            super().rollback()
            trace.append(("rollback", self.get_backend_pid(), "", None))

    resources = PostgresStorageResources._for_source_validity(pg_function_dsn, issuer=_RESOURCE_ISSUER, capacity=2)
    connect = psycopg2.connect

    def traced_connect(*args, **kwargs):
        kwargs.setdefault("connection_factory", TraceConnection)
        return connect(*args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(psycopg2, "connect", traced_connect)
            resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
        assert len(resources.pool_manager._pool._pool) == 2
        store = PostgresMemoryStore(pg_function_dsn, ready_pool=resources.ready_pool, require_authorization=True)
        assert store.source_versions() is None
        hooks = SourceValidationHooks(
            lambda snapshot, context: snapshot.payload.get("tenant_id") == context.principal.tenant_id,
            lambda snapshot: snapshot.payload.get("unsafe") is not True,
        )
        admission = resources._install_source_versions(store, SCOPE, hooks, issuer=_RESOURCE_ISSUER)
        # Fixture bootstrap is explicit SQL, never a production activation path.
        from contextlib import closing

        with closing(psycopg2.connect(pg_function_dsn)) as admin, admin, admin.cursor() as cur:
            cur.execute("INSERT INTO memplex_source_authorities VALUES (%s,%s,%s,'live',1,1,%s::jsonb,1)",
                        (SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id, _canonical(POLICY)))
        backend = store.authorized(CONTEXT).source_versions()
        trace.clear()
        yield store, backend, resources, trace, pg_function_dsn, admission
    finally:
        if getattr(resources, "_test_expected_fault", False):
            assert store._pool_manager._pool.closed
            with pytest.raises(RuntimeError, match="faulted"):
                resources.close()
        else:
            resources.close()


@NATIVE
def test_one_connection_snapshot_after_lock(source_backend, monkeypatch):
    store, backend, _, trace, _, _ = source_backend
    receipt = _mutate(backend, _command())
    trace.clear()
    monkeypatch.setattr(store, "_execute", lambda *args, **kwargs: pytest.fail("ordinary getter opened a transaction"))
    with _fence(backend) as session:
        assert session.resolve_id("source") == receipt.ref.key
        snapshot = session.source(receipt.ref.key)
        assert snapshot.head.ref == receipt.ref
        assert session.authority().commit_seq == receipt.commit_seq
        session._execute("SELECT txid_current()")
        transaction_id = session._cursor.fetchone()[0]
        session.source(receipt.ref.key)
        session._execute("SELECT txid_current()")
        assert session._cursor.fetchone()[0] == transaction_id
        assert session.utc_now() == session.utc_now()
        with pytest.raises(SourceContractError):
            session.source(receipt.ref.key)
    assert len({row[1] for row in trace}) == 1
    assert sum(row[0] == "commit" for row in trace) == 1
    statements = [row[2] for row in trace if row[0] == "finish"]
    assert statements[0].startswith("SET LOCAL lock_timeout")
    assert statements[0].index("SET LOCAL statement_timeout") < statements[0].index("SET TRANSACTION")
    assert statements[0].endswith("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    lock = next(i for i, sql in enumerate(statements) if "pg_advisory_xact_lock_shared" in sql)
    metadata = [i for i, sql in enumerate(statements) if "pg_catalog.current_database()" in sql or "role.rolsuper" in sql]
    assert len(metadata) == 2 and 0 < min(metadata) <= max(metadata) < lock
    reads = [i for i, sql in enumerate(statements) if "FROM memplex_source_" in sql or "FROM memplex_facts" in sql]
    assert reads and min(reads) > lock
    with pytest.raises(SourceContractError):
        session.authority()


@NATIVE
def test_pg_source_cas_and_rollback(source_backend, monkeypatch):
    from memplex.source_contract import SourceOperation, SourceState

    store, backend, _, trace, _, _ = source_backend
    first = _mutate(backend, _command())
    second = _mutate(backend, _command(value="two", expected=first.ref.token, operation=SourceOperation.REPLACE))
    assert second.ref.token.content_revision == 2
    with pytest.raises(SourceContractError) as stale:
        _mutate(backend, _command(value="stale", expected=first.ref.token, operation=SourceOperation.REPLACE))
    assert stale.value.code == "source_changed"
    internal = store.source_versions()
    original = internal._write_source

    def fail_after_writes(*args):
        original(*args)
        raise RuntimeError("injected before commit")

    with monkeypatch.context() as patch:
        patch.setattr(internal, "_write_source", fail_after_writes)
        with pytest.raises(RuntimeError, match="injected"):
            _mutate(backend, _command(value="rollback", expected=second.ref.token, operation=SourceOperation.REPLACE))
    with _fence(backend) as session:
        assert session.source(second.ref.key).payload["object"] == "two"
        assert session.authority().commit_seq == second.commit_seq
    deleted = _mutate(backend, _command(expected=second.ref.token, operation=SourceOperation.DELETE))
    assert deleted.state is SourceState.DELETED
    with pytest.raises(SourceContractError):
        _mutate(backend, _command())
    recreated = _mutate(backend, _command(expected=deleted.ref.token, operation=SourceOperation.RECREATE))
    assert recreated.ref.token.incarnation != first.ref.token.incarnation
    assert any(row[0] == "rollback" for row in trace)


@NATIVE
def test_pg_post_commit_new_reader(source_backend, monkeypatch):
    """C < R on distinct physical connections before the writer releases its lease."""
    import threading

    from memplex.source_contract import SourceOperation

    store, backend, _, _, _, _ = source_backend
    old = _mutate(backend, _command())
    committed, release_writer = threading.Event(), threading.Event()
    writer_pids, receipts, failures = [], [], []
    connection_class = type(store._pool_manager._pool._pool[0])
    commit = connection_class.commit

    def writer():
        try:
            receipts.append(_mutate(backend, _command(value="current", expected=old.ref.token,
                                                       operation=SourceOperation.REPLACE)))
        except BaseException as exc:  # noqa: BLE001 - propagate peer failure to its owning test
            failures.append(exc)

    writer_thread = threading.Thread(target=writer)

    def held_commit(connection):
        commit(connection)  # The real PostgreSQL commit precedes the barrier.
        if threading.current_thread() is writer_thread:
            writer_pids.append(connection.get_backend_pid())
            committed.set()
            assert release_writer.wait(2)

    with monkeypatch.context() as patch:
        patch.setattr(connection_class, "commit", held_commit)
        writer_thread.start()
        try:
            assert committed.wait(2)
            assert not receipts
            with _fence(backend) as session:
                reader_pid = session._cursor.connection.get_backend_pid()
                snapshot = session.source(old.ref.key)
                assert reader_pid != writer_pids[0]
                assert snapshot.payload["object"] == "current"
                assert snapshot.head.ref.token.content_revision == 2
                session.utc_now()
        finally:
            release_writer.set()
            writer_thread.join(3)
    assert not writer_thread.is_alive() and not failures
    assert len(receipts) == 1 and snapshot.head.ref == receipts[0].ref


@NATIVE
def test_no_scope_leak_between_sessions(source_backend):
    store, backend, resources, _, _, _ = source_backend
    receipt = _mutate(backend, _command())
    later = replace(CONTEXT, session_id="another-session")
    delayed = _fence(backend)
    other = store.authorized(later).source_versions()
    with pytest.raises(SourceContractError), other.read_fence(CONTEXT, deadline_ns=time.monotonic_ns() + 100_000_000):
        pass
    with delayed as session:
        assert session.source(receipt.ref.key) is not None
    with resources.pool_manager.transaction(store._bind_transaction_scope, later) as (_, cursor):
        cursor.execute("SELECT current_setting('memplex.session_id'), current_setting('transaction_isolation'), "
                       "current_setting('lock_timeout'), current_setting('statement_timeout')")
        row = cursor.fetchone()
        assert row[0] == "another-session"
        assert row[2:] == ("0", "0")
    with resources.pool_manager.transaction(lambda cursor, context: None, later) as (_, cursor):
        cursor.execute("SELECT current_setting('memplex.session_id', true)")
        assert cursor.fetchone()[0] in ("", None)


@NATIVE
def test_reader_started_before_commit_waits_then_observes_it(source_backend, monkeypatch):
    """R < C < L, with an actual second connection waiting for the writer."""
    import threading

    from memplex.source_contract import SourceOperation

    store, backend, _, trace, _, _ = source_backend
    old = _mutate(backend, _command())
    before_write, release_writer, reader_started = threading.Event(), threading.Event(), threading.Event()
    from memplex.storage.postgres_sources import _PostgresReadSession

    original_execute = _PostgresReadSession._execute

    def observed_execute(session, sql, parameters=()):
        if "pg_advisory_xact_lock_shared" in sql:
            reader_started.set()
        return original_execute(session, sql, parameters)
    results, failures = [], []
    internal = store.source_versions()
    original = internal._write_source

    def paused(*args):
        before_write.set()
        assert release_writer.wait(2)
        original(*args)

    def writer():
        try:
            _mutate(backend, _command(value="after", expected=old.ref.token, operation=SourceOperation.REPLACE))
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures to the owning test
            failures.append(exc)

    def reader():
        try:
            with _fence(backend) as session:
                results.append((session.source(old.ref.key), session.utc_now()))
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures to the owning test
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(internal, "_write_source", paused)
        patch.setattr(_PostgresReadSession, "_execute", observed_execute)
        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)
        writer_thread.start()
        try:
            assert before_write.wait(2)
            reader_thread.start()
            assert reader_started.wait(2)
        finally:
            release_writer.set()
            writer_thread.join(3)
            if reader_thread.ident is not None:
                reader_thread.join(3)
    assert not writer_thread.is_alive() and not reader_thread.is_alive()
    assert not failures
    assert results[0][0].payload["object"] == "after"
    assert len({row[1] for row in trace if "pg_advisory_xact_lock" in row[2]}) >= 2


@NATIVE
def test_linearized_reader_blocks_writer_until_exit(source_backend):
    """L < C: fixed old snapshot is allowed while writer waits on real PG."""
    import threading

    import psycopg2

    from memplex.source_contract import SourceOperation

    store, backend, _, _, dsn, _ = source_backend
    lock_key = _source_lock_key(store._ready_pool.target, SCOPE.authority_id)
    old = _mutate(backend, _command())
    completed, errors = threading.Event(), []

    def writer():
        try:
            _mutate(backend, _command(value="later", expected=old.ref.token, operation=SourceOperation.REPLACE))
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures to the owning test
            errors.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(target=writer)
    from contextlib import closing

    with closing(psycopg2.connect(dsn)) as observer, observer, observer.cursor() as cursor:
        try:
            with _fence(backend) as session:
                fixed = session.source(old.ref.key)
                session.utc_now()
                thread.start()
                until = time.monotonic() + .15
                waiting = False
                while time.monotonic() < until:
                    cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                                   "AND classid=%s::oid AND objid=%s::oid AND objsubid=1)",
                                   ((lock_key >> 32) & 0xffffffff, lock_key & 0xffffffff))
                    if cursor.fetchone()[0]:
                        waiting = True
                        break
                assert waiting and not completed.is_set()
        finally:
            if thread.ident is not None:
                thread.join(3)
    assert not thread.is_alive() and not errors
    assert fixed.payload["object"] == "one"
    with _fence(backend) as session:
        assert session.source(old.ref.key).payload["object"] == "later"


@pytest.fixture
def protocol_backend(monkeypatch):
    """Pinned driver bookkeeping with fake connections, never a native lock proof."""
    psycopg2 = pytest.importorskip("psycopg2")
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.source_versions import _RESOURCE_ISSUER, SourceValidationHooks
    from tests.test_postgres_store import (
        _install_resource_runner_sequence,
        _PoolMockConnection,
        _PoolMockCursor,
    )

    _install_resource_runner_sequence(monkeypatch)
    connections = []

    class Cursor(_PoolMockCursor):
        rowcount = 1
        description = None

        def __init__(self, connection):
            super().__init__()
            self.connection = connection

        def execute(self, query, parameters=None):
            self.connection.status = 2
            return super().execute(query, () if parameters is None else parameters)

    class Connection(_PoolMockConnection):
        def __init__(self):
            super().__init__()
            self.closed, self.status, self.close_calls = False, 0, 0
            self.cursor_instance = Cursor(self)

        def get_transaction_status(self):
            return self.status

        def commit(self):
            super().commit()
            self.status = 0

        def rollback(self):
            super().rollback()
            self.status = 0

        def close(self):
            self.closed = True
            self.close_calls += 1

    def connect(*args, **kwargs):
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(psycopg2, "connect", connect)
    resources = PostgresStorageResources._for_source_validity("dbname=fake", issuer=_RESOURCE_ISSUER, capacity=2)
    resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
    pool = resources.pool_manager._pool
    pool.connection, pool.connections = connections[-1], connections
    pool.closeall_calls, pool.put_calls = 0, []
    closeall, putconn = pool.closeall, pool.putconn

    def close_counted():
        pool.closeall_calls += 1
        closeall()

    def put_counted(connection):
        pool.put_calls.append(connection)
        putconn(connection)

    monkeypatch.setattr(pool, "closeall", close_counted)
    monkeypatch.setattr(pool, "putconn", put_counted)
    pool.expected_fault = False
    store = PostgresMemoryStore("dbname=fake", ready_pool=resources.ready_pool, require_authorization=True)
    try:
        yield resources, store, pool, _RESOURCE_ISSUER, SourceValidationHooks(lambda snapshot, auth: True, lambda snapshot: True)
    finally:
        if pool.expected_fault:
            assert pool.closeall_calls == 1
            with pytest.raises(RuntimeError, match="faulted"):
                resources.close()
        else:
            resources.close()


def test_capability_requires_sealed_resource_installation(protocol_backend):
    from memplex.storage.postgres_sources import PostgresSourceVersionStore

    resources, store, _, issuer, hooks = protocol_backend
    assert store.source_versions() is None
    assert store.authorized(CONTEXT).source_versions() is None
    with pytest.raises(SourceContractError):
        resources._install_source_versions(store, SCOPE, hooks, issuer=object())
    assert store.source_versions() is None
    uninstalled = PostgresSourceVersionStore(store, resource=resources)
    with pytest.raises(SourceContractError), _fence(uninstalled):
        pass
    admission = resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    with pytest.raises(SourceContractError):
        store.authorized(CONTEXT).source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    with pytest.raises(SourceContractError):
        resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)


@pytest.mark.parametrize("phase", ["isolation", "timeouts", "identity", "binding"])
def test_source_setup_failure_rolls_back_and_releases(protocol_backend, monkeypatch, phase):
    resources, store, pool, _, _ = protocol_backend
    manager = resources.pool_manager
    cursor = pool.connection._cursor
    original = cursor.execute
    original_identity = manager._verify_borrowed_connection_target
    original_bind = store._bind_transaction_scope
    before_commits, before_rollbacks = pool.connection.commits, pool.connection.rollbacks

    def execute(sql, params=()):
        if ((phase == "isolation" and "SET TRANSACTION" in sql)
                or (phase == "timeouts" and "SET LOCAL lock_timeout" in sql)):
            raise RuntimeError("setup failure")
        return original(sql, params)

    def identity(connection, *, deadline_ns=None):
        if phase == "identity":
            raise RuntimeError("setup failure")
        original_identity(connection, deadline_ns=deadline_ns)

    def binding(cur, context):
        if phase == "binding":
            raise RuntimeError("setup failure")
        original_bind(cur, context)

    with monkeypatch.context() as patch:
        patch.setattr(cursor, "execute", execute)
        patch.setattr(manager, "_verify_borrowed_connection_target", identity)
        with pytest.raises(RuntimeError, match="setup failure"), manager.transaction(
                binding, CONTEXT, deadline_ns=time.monotonic_ns() + 100_000_000):
            pytest.fail("failed setup must not yield")
    assert manager.business_lease_count == 0
    assert not manager._checked_out and not manager._borrow_queue and manager._waiting_borrowers == 0
    assert pool.connection.commits == before_commits and pool.connection.rollbacks > before_rollbacks
    cursor.executed.clear()
    with manager.transaction(original_bind, CONTEXT):
        pass
    assert not any("SET TRANSACTION" in sql or "lock_timeout" in sql for sql, _ in cursor.executed)


def test_capacity_deadline_unwinds_only_its_own_ticket(protocol_backend):
    from memplex.storage.pool import _BorrowCapacityReservation

    resources, _, _, _, _ = protocol_backend
    manager = resources.pool_manager
    held = []
    try:
        for _ in range(manager.max_connections):
            reservation = _BorrowCapacityReservation(manager)
            reservation.__enter__()
            held.append(reservation)
        with pytest.raises(SourceBusyError), manager.transaction(lambda *args: None, CONTEXT,
                                                                 deadline_ns=time.monotonic_ns() + 10_000_000):
            pytest.fail("pool had no capacity")
        assert not manager._borrow_queue
        assert manager._waiting_borrowers == len(held)
    finally:
        for reservation in held:
            reservation.__exit__(None, None, None)
    assert manager._waiting_borrowers == 0


def test_source_transaction_commit_failure_cannot_return_receipt(protocol_backend, monkeypatch):
    resources, store, pool, issuer, hooks = protocol_backend
    resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    adapter = store.source_versions()
    manager = resources.pool_manager
    pool.expected_fault = True
    with monkeypatch.context() as patch:
        patch.setattr(pool.connection, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"), adapter._transaction(
                CONTEXT, time.monotonic_ns() + 100_000_000, shared=False):
            pass
    assert manager.business_lease_count == 0
    assert resources.state == "FAULTED"


@NATIVE
def test_native_lock_timeout_does_not_retry_or_read_sources(source_backend):
    import psycopg2

    store, backend, _, trace, dsn, _ = source_backend
    blocker = psycopg2.connect(dsn)
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_source_lock_key(store._ready_pool.target, SCOPE.authority_id),))
        trace.clear()
        with pytest.raises(SourceBusyError), backend.read_fence(CONTEXT, deadline_ns=time.monotonic_ns() + 30_000_000):
            pytest.fail("exclusive writer owns the source lock")
        assert sum(row[0] == "start" and "pg_advisory_xact_lock_shared" in row[2] for row in trace) == 1
        assert not any("FROM memplex_source_" in row[2] for row in trace)
    finally:
        blocker.rollback()
        blocker.close()
    with _fence(backend) as session:
        assert session.authority().scope == SCOPE


@NATIVE
def test_native_source_identity_and_current_policy(source_backend):
    import psycopg2

    from memplex.source_codec import _canonical

    store, backend, _, _, dsn, _ = source_backend
    receipt = _mutate(backend, _command())
    other = replace(CONTEXT, principal=replace(CONTEXT.principal, subject_id="mallory"))
    with pytest.raises(SourceContractError), _fence(store.authorized(other).source_versions(), other):
        pass
    connection = psycopg2.connect(dsn)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_source_lock_key(store._ready_pool.target, SCOPE.authority_id),))
            cursor.execute("UPDATE memplex_source_authorities SET policy_revision=2,policy=%s::jsonb,commit_seq=commit_seq+1",
                           (_canonical(replace(POLICY, revision=2, entries=())),))
    finally:
        connection.close()
    with pytest.raises(SourceContractError) as revoked, _fence(backend):
        pass
    assert revoked.value.code == "denied"
    assert receipt.ref.token.auth_revision == 1


@NATIVE
def test_native_cross_kind_collision_rolls_back_payload(source_backend):
    import psycopg2

    from memplex.source_contract import SourceKind

    _, backend, _, _, dsn, _ = source_backend
    _mutate(backend, _command())
    raw = _command(kind=SourceKind.RAW_PARAGRAPH)
    with pytest.raises(SourceContractError) as conflict:
        _mutate(backend, raw)
    assert conflict.value.code == "malformed"
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM memplex_paragraphs")
            assert cursor.fetchone() == (0,)
    finally:
        connection.close()


def test_source_transaction_rejects_autocommit_before_lock(protocol_backend):
    resources, store, pool, _, _ = protocol_backend
    pool.connection.autocommit = True
    pool.connection._cursor.executed.clear()
    with pytest.raises(SourceContractError), resources.pool_manager.transaction(
            store._bind_transaction_scope, CONTEXT, deadline_ns=time.monotonic_ns() + 100_000_000):
        pytest.fail("autocommit cannot hold a transaction advisory lock")
    assert not pool.connection._cursor.executed
    assert resources.pool_manager.business_lease_count == 0
    pool.connection.autocommit = False


def _all_kind_command(kind):
    from memplex.models import Fact, Function, Observation, Preference
    from memplex.source_contract import SourceKind

    name = "source_" + kind
    identity = {
        "tenant_id": CONTEXT.principal.tenant_id, "workspace_id": CONTEXT.workspace_id,
        "owner_subject_id": CONTEXT.principal.subject_id, "owner": CONTEXT.principal.subject_id,
        "visibility": "workspace", "origin_session": CONTEXT.session_id,
    }
    payload = {"id": name, "raw_text": "raw input", "trust_tier": 1, **identity} if kind == "raw_paragraph" else {
        "function": Function, "fact": Fact, "preference": Preference, "observation": Observation,
    }[kind](id=name, name="source", **identity).to_dict()
    payload.update(agent_id=CONTEXT.agent_id, session_id=CONTEXT.session_id)
    return replace(_command(kind=SourceKind(kind), name=name), payload=payload)


@pytest.mark.parametrize("kind", ["raw_paragraph", "function", "fact", "preference", "observation"])
def test_all_kind_fixture_passes_authenticated_payload_binding(kind):
    from memplex.storage.postgres_sources import _bound_payload

    command = _all_kind_command(kind)
    payload = _bound_payload(command, CONTEXT)
    assert payload is not None
    expected = {
        "tenant_id": CONTEXT.principal.tenant_id, "workspace_id": CONTEXT.workspace_id,
        "owner_subject_id": CONTEXT.principal.subject_id, "owner": CONTEXT.principal.subject_id,
        "visibility": "workspace", "agent_id": CONTEXT.agent_id, "session_id": CONTEXT.session_id,
        "origin_session": CONTEXT.session_id,
    }
    assert {field: command.payload[field] for field in expected} == expected
    assert {field: payload[field] for field in expected} == expected
    assert payload["namespace"] == {
        "memplex_tenant_id": CONTEXT.principal.tenant_id,
        "memplex_workspace_id": CONTEXT.workspace_id,
        "memplex_subject_id": CONTEXT.principal.subject_id,
    }
    assert payload["provenance"]["authentication_id"] == CONTEXT.principal.authentication_id
    assert payload["provenance"]["agent_id"] == CONTEXT.agent_id
    assert payload["provenance"]["session_id"] == CONTEXT.session_id
    assert payload["provenance"]["request_id"] == CONTEXT.request_id


@pytest.mark.parametrize("kind", ["raw_paragraph", "function", "fact", "preference", "observation"])
@pytest.mark.parametrize("visibility", [None, "public"])
def test_all_kind_payload_binding_rejects_explicit_invalid_visibility(kind, visibility):
    from memplex.storage.postgres_sources import _bound_payload

    command = _all_kind_command(kind)
    command = replace(command, payload={**command.payload, "visibility": visibility})
    with pytest.raises(SourceContractError) as caught:
        _bound_payload(command, CONTEXT)
    assert caught.value.code == "denied"
    assert command.payload["visibility"] == visibility


@pytest.mark.parametrize("kind", ["raw_paragraph", "function", "fact", "preference", "observation"])
@NATIVE
def test_native_all_source_kinds_roundtrip_and_atomic_delete(source_backend, kind):
    from memplex.source_contract import SourceOperation

    _, backend, _, _, _, _ = source_backend
    command = _all_kind_command(kind)
    name, payload = command.key.logical_id, command.payload
    receipt = _mutate(backend, command)
    with _fence(backend) as session:
        snapshot = session.source(receipt.ref.key)
        assert snapshot.payload["id"] == name
        assert snapshot.head.ref == receipt.ref
    changed = replace(command, expected=receipt.ref.token, operation=SourceOperation.REPLACE,
                      payload={**payload, "name": "changed"})
    replacement = _mutate(backend, changed)
    assert replacement.ref.token.content_revision == 2
    deleted = _mutate(backend, replace(changed, expected=replacement.ref.token, operation=SourceOperation.DELETE, payload=None))
    with _fence(backend) as session:
        assert session.resolve_id(name) == deleted.ref.key
        assert not session.source(deleted.ref.key).payload


@NATIVE
def test_native_effective_rls_and_identity_binding(source_backend, monkeypatch):
    import uuid

    import psycopg2
    from psycopg2 import sql

    from memplex.storage.source_versions import _RESOURCE_ISSUER

    store, backend, _, _, dsn, _ = source_backend
    connection = psycopg2.connect(dsn)
    role = "source_reader_" + uuid.uuid4().hex
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_schema()")
            schema = cursor.fetchone()[0]
            cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON memplex_source_authorities, memplex_source_heads, "
                                   "memplex_derivation_records TO {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON memplex_facts TO {}").format(sql.Identifier(role)))
        bind = store._bind_transaction_scope

        def app_role(cursor, context):
            bind(cursor, context)
            cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))

        with monkeypatch.context() as patch:
            patch.setattr(store, "_bind_transaction_scope", app_role)
            command = _command()
            command = replace(command, payload={**command.payload, "owner_subject_id": "forged", "tenant_id": "forged"})
            receipt = _mutate(backend, command)
            with _fence(backend) as session:
                payload = session.source(receipt.ref.key).payload
                assert payload["tenant_id"] == SCOPE.tenant_id and payload["owner_subject_id"] == "alice"
            internal = store.source_versions()
            internal._install_hooks(internal._hooks, scope=replace(SCOPE, tenant_id="other"), issuer=_RESOURCE_ISSUER)
            other = replace(CONTEXT, principal=replace(CONTEXT.principal, tenant_id="other"))
            with pytest.raises(SourceContractError), _fence(store.authorized(other).source_versions(), other):
                pass
    finally:
        connection.rollback()
        with connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        connection.close()


@pytest.mark.parametrize("change", ["content", "acl", "operational", "context", "delete", "recreate", "overflow"])
def test_source_head_protocol_versions(change):
    from memplex.source_contract import (
        MAX_REVISION,
        SourceIntegrityError,
        SourceOperation,
        SourceSnapshot,
        SourceState,
    )
    from memplex.storage.postgres_sources import _bound_payload, _compare_source, _mutation_head

    original = _command()
    payload = _bound_payload(original, CONTEXT)
    head = _mutation_head(original, None, payload, 2)
    snapshot = SourceSnapshot(head, payload)
    command = replace(original, expected=head.ref.token, operation=SourceOperation.REPLACE)
    next_payload = dict(payload)
    if change == "content":
        next_payload["object"] = "changed"
    elif change == "acl":
        next_payload["visibility"] = "user"
    elif change == "operational":
        next_payload["access_count"] = 42
    elif change == "context":
        command = replace(command, input_context={"author_role": "user"})
    elif change == "delete":
        command = replace(command, operation=SourceOperation.DELETE, payload=None)
        next_payload = None
    elif change == "recreate":
        snapshot = replace(snapshot, head=replace(head, state=SourceState.DELETED, payload_locator="", input_context={}, root_refs=()))
        command = replace(command, operation=SourceOperation.RECREATE)
    else:
        exhausted = replace(head.ref.token, content_revision=MAX_REVISION)
        snapshot = replace(snapshot, head=replace(head, ref=replace(head.ref, token=exhausted)))
        command = replace(command, expected=exhausted)
        next_payload["object"] = "overflow"
    _compare_source(snapshot, command)
    if change == "overflow":
        with pytest.raises(SourceIntegrityError):
            _mutation_head(command, snapshot, next_payload, 3)
        return
    result = _mutation_head(command, snapshot, next_payload, 3)
    if change == "recreate":
        assert result.ref.token.incarnation != head.ref.token.incarnation
    else:
        assert result.ref.token.incarnation == head.ref.token.incarnation
        assert result.ref.token.content_revision == (2 if change in {"content", "context", "delete"} else 1)
        assert result.ref.token.auth_revision == (2 if change == "acl" else 1)
    if change == "delete":
        assert result.state is SourceState.DELETED and result.payload_locator == ""


@pytest.mark.parametrize("tamper", [None, "payload", "row_identity", "head", "expired", "closed"])
def test_detached_session_checks_persisted_binding_and_lifetime(tamper):
    from datetime import UTC, datetime

    from memplex.source_codec import _wire
    from memplex.source_contract import SourceIntegrityError
    from memplex.storage.postgres_sources import (
        _bound_payload,
        _identity,
        _mutation_head,
        _PostgresReadSession,
    )

    command = _command()
    payload = _bound_payload(command, CONTEXT)
    head = _mutation_head(command, None, payload, 2)

    class Cursor:
        query = ""

        def execute(self, sql, args=()):
            self.query = sql

        def fetchone(self):
            if "FROM memplex_source_authorities" in self.query:
                return ("live", 1, 2, _wire(POLICY), 1)
            if "FROM memplex_source_heads" in self.query:
                return (SCOPE.authority_id, "fact", "source", head.ref.token.incarnation, 1, 1, "live", "direct_input",
                        head.payload_locator, 1, "0" * 64 if tamper == "head" else head.ref.semantic_digest, 2,
                        {"input_context": {}, "root_refs": []}, *_identity(payload))
            if "clock_timestamp" in self.query:
                return (datetime.now(UTC),)
            body = dict(payload)
            if tamper == "payload":
                body["object"] = "unrecorded change"
            identity = list(_identity(payload))
            if tamper == "row_identity":
                identity[1] = "mallory"
            return ("source", body, *identity)

    session = _PostgresReadSession(Cursor(), SCOPE, time.monotonic_ns() + 100_000_000)
    if tamper == "expired":
        session._deadline_ns = time.monotonic_ns() - 1
        with pytest.raises(SourceBusyError):
            session.source(command.key)
    elif tamper == "closed":
        session._active = False
        with pytest.raises(SourceContractError):
            session.source(command.key)
    elif tamper is not None:
        with pytest.raises(SourceIntegrityError):
            session.source(command.key)
    else:
        snapshot = session.source(command.key)
        payload["object"] = "mutated outside the session"
        assert snapshot.payload["object"] == "one"
        session.utc_now()
        with pytest.raises(SourceContractError):
            session.source(command.key)


def test_current_policy_compares_workspace_and_agent():
    for context in (replace(CONTEXT, workspace_id="other"), replace(CONTEXT, agent_id="other")):
        with pytest.raises(SourceContractError):
            _admit_current_policy(context, POLICY)


@NATIVE
def test_native_revision_projection_and_missing_input_rollback(source_backend):
    from memplex.source_contract import SourceOperation

    _, backend, _, _, _, _ = source_backend
    command = _command()
    first = _mutate(backend, command)
    operational = replace(command, expected=first.ref.token, operation=SourceOperation.REPLACE,
                          payload={**command.payload, "access_count": 42})
    second = _mutate(backend, operational)
    assert second.ref.token == first.ref.token
    authorization = replace(operational, expected=second.ref.token, payload={**operational.payload, "visibility": "user"})
    third = _mutate(backend, authorization)
    assert third.ref.token.content_revision == 1 and third.ref.token.auth_revision == 2
    missing = replace(third.ref, key=replace(third.ref.key, logical_id="missing"))
    with pytest.raises(SourceContractError):
        _mutate(backend, replace(authorization, expected=third.ref.token, root_refs=(missing,)))
    with _fence(backend) as session:
        assert session.authority().commit_seq == third.commit_seq
        assert session.source(third.ref.key).head.ref == third.ref


@NATIVE
def test_native_commit_exception_does_not_publish_source_receipt(source_backend, monkeypatch):
    import psycopg2

    from memplex.source_contract import SourceOperation

    store, backend, resources, _, dsn, _ = source_backend
    first = _mutate(backend, _command())
    connection_class = type(store._pool_manager._pool._pool[0])

    def fail_commit(connection):
        raise RuntimeError("injected native commit failure")

    resources._test_expected_fault = True
    with monkeypatch.context() as patch:
        patch.setattr(connection_class, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="native commit failure"):
            _mutate(backend, _command(value="uncommitted", expected=first.ref.token, operation=SourceOperation.REPLACE))
    assert store._pool_manager.business_lease_count == 0
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT data->>'object' FROM memplex_facts WHERE id='source'")
            assert cursor.fetchone() == ("one",)
            cursor.execute("SELECT content_revision,last_commit_seq FROM memplex_source_heads WHERE logical_id='source'")
            assert cursor.fetchone() == (1, first.commit_seq)
    finally:
        connection.close()


@pytest.mark.parametrize("change", ["same", "operational", "acl"])
def test_request_correlation_does_not_churn_source_authorization(change):
    from memplex.source_contract import SourceOperation, SourceSnapshot
    from memplex.storage.postgres_sources import _bound_payload, _mutation_head

    command = _command()
    first = _bound_payload(command, replace(CONTEXT, request_id="request-a"))
    head = _mutation_head(command, None, first, 2)
    current = SourceSnapshot(head, first)
    command = replace(command, operation=SourceOperation.REPLACE, expected=head.ref.token)
    later = _bound_payload(command, replace(CONTEXT, request_id="request-b"))
    if change == "operational":
        later["access_count"] = 42
    elif change == "acl":
        later["visibility"] = "user"
    result = _mutation_head(command, current, later, 3)
    assert result.ref.token.content_revision == 1
    assert result.ref.token.auth_revision == (2 if change == "acl" else 1)
    assert later["provenance"]["request_id"] == "request-b"
    assert result.payload_locator != head.payload_locator  # Audit binding still changes.


def test_default_pool_cannot_install_source_capability(monkeypatch):
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.source_versions import _RESOURCE_ISSUER, SourceValidationHooks
    from tests.test_postgres_store import _install_resource_runner_sequence, _PoolMock

    _install_resource_runner_sequence(monkeypatch)
    pool = _PoolMock()
    resources = PostgresStorageResources("dbname=fake", pool_factory=lambda *args: pool)
    try:
        resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
        store = PostgresMemoryStore("dbname=fake", ready_pool=resources.ready_pool)
        hooks = SourceValidationHooks(lambda *args: True, lambda *args: True)
        with pytest.raises(SourceContractError):
            resources._install_source_versions(store, SCOPE, hooks, issuer=_RESOURCE_ISSUER)
        assert store.source_versions() is None
        assert resources.pool_manager.min_connections == 1
        assert resources.pool_manager.max_connections == 8
        with resources.pool_manager.transaction(lambda *args: None, CONTEXT):
            pass
    finally:
        resources.close()


def test_source_setup_sends_timeouts_before_isolation_in_one_execute(monkeypatch):
    from memplex.storage import pool as pool_module
    from tests.test_postgres_store import _PoolMockConnection

    connection = _PoolMockConnection()
    now = 1_000_000_000
    monkeypatch.setattr(pool_module.time, "monotonic_ns", lambda: now)
    pool_module._source_transaction_setup(connection, now + 50_000_000)
    queries = connection._cursor.executed
    assert len(queries) == 1
    statement, _ = queries[0]
    assert statement.index("SET LOCAL lock_timeout") < statement.index("SET TRANSACTION")
    assert statement.index("SET LOCAL statement_timeout") < statement.index("SET TRANSACTION")
    assert "50ms" in statement and "SELECT set_config" not in statement


@pytest.mark.parametrize("phase", ["target", "principal"])
@pytest.mark.parametrize("kind", ["local", "55P03", "57014"])
def test_source_budget_cancellation_keeps_resource_ready(protocol_backend, monkeypatch, phase, kind):
    resources, store, pool, _, _ = protocol_backend
    manager = resources.pool_manager
    connection = pool.connection
    cursor = connection._cursor
    execute = cursor.execute
    marker = "pg_catalog.current_database()" if phase == "target" else "role.rolsuper"

    class BudgetCancellation(Exception):
        pgcode = kind

    failure = SourceBusyError() if kind == "local" else BudgetCancellation()

    def cancel(sql, parameters=()):
        if marker in sql:
            connection.status = 3
            raise failure
        return execute(sql, parameters)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(cursor, "execute", cancel)
            with pytest.raises(type(failure)), manager.transaction(
                    store._bind_transaction_scope, CONTEXT,
                    deadline_ns=time.monotonic_ns() + 100_000_000):
                pytest.fail("cancelled metadata query yielded a transaction")
        assert resources.state == "READY"
        assert manager.business_lease_count == 0 and not manager._checked_out
        with manager.transaction(store._bind_transaction_scope, CONTEXT):
            pass
    finally:
        pool.expected_fault = resources.state == "FAULTED"


def test_source_driver_lock_timeout_has_no_late_checkout(protocol_backend):
    import threading

    resources, _, pool, _, _ = protocol_backend
    held, release, expired = threading.Event(), threading.Event(), threading.Event()

    def hold_driver_lock():
        with pool._lock:
            held.set()
            if not release.wait(2):
                expired.set()

    peer = threading.Thread(target=hold_driver_lock)
    peer.start()
    try:
        assert held.wait(1)
        with pytest.raises(SourceBusyError), resources.pool_manager.transaction(
                lambda *args: None, CONTEXT, deadline_ns=time.monotonic_ns() + 10_000_000):
            pytest.fail("driver lock was held")
        assert not expired.is_set() and peer.is_alive()
        assert not pool._used and len(pool._pool) == 2
        assert not resources.pool_manager._lease_records
    finally:
        release.set()
        peer.join(3)
    assert not peer.is_alive()
    with resources.pool_manager.transaction(lambda *args: None, CONTEXT):
        pass
    assert len(pool.connections) == 2 and not pool._used


@pytest.mark.parametrize("route", ["transaction", "read_cursor", "target", "principal", "access"])
@pytest.mark.parametrize("loss", ["closed", "unknown"])
def test_source_inventory_loss_faults_every_borrower_without_reconnect(protocol_backend, route, loss):
    resources, store, pool, _, _ = protocol_backend
    manager = resources.pool_manager
    target = resources.ready_pool.target
    connection = pool._pool[0]
    if loss == "closed":
        connection.closed = True
    else:
        connection.status = 4
    pool.expected_fault = True
    with pytest.raises(RuntimeError, match="inventory"):
        if route == "transaction":
            with manager.transaction(store._bind_transaction_scope, CONTEXT):
                pytest.fail("depleted source pool yielded")
        elif route == "read_cursor":
            manager.read_cursor(store._bind_transaction_scope, CONTEXT)
        elif route == "target":
            manager.inspect_target()
        elif route == "principal":
            manager.inspect_application_role()
        else:
            manager.verify_application_access(target=target, profile="development", vector_dim=0)
    assert resources.state == "FAULTED" and len(pool.connections) == 2
    assert not manager._lease_records and not manager._checked_out
    with pytest.raises(RuntimeError, match="replacement"):
        pool._connect()
    assert len(pool.connections) == 2


def test_source_installation_requires_quiescent_fixed_inventory(protocol_backend):
    resources, store, pool, issuer, hooks = protocol_backend
    with resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT):
        with pytest.raises(SourceContractError):
            resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
        assert store.source_versions() is None
    resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    assert store.source_versions() is not None
    assert len(pool.connections) == 2


def test_source_partial_provisioning_closes_owned_connections(monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    from memplex.storage import pool as pool_module

    calls = []

    class Connection:
        closed = 0

        def close(self):
            self.closed += 1

    first = Connection()

    def connect(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("second connection failed")
        return first

    monkeypatch.setattr(psycopg2, "connect", connect)
    with pytest.raises(RuntimeError, match="second connection failed"):
        pool_module._new_source_ready_pool(2, "dbname=fake")
    assert len(calls) == 2 and first.closed == 1


def test_source_setup_cursor_cleanup_failure_is_terminal(protocol_backend, monkeypatch):
    resources, _, pool, _, _ = protocol_backend
    manager = resources.pool_manager
    cursor = pool.connection._cursor
    close = cursor.close
    failed = False

    def fail_once():
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("setup cursor close failed")
        close()

    monkeypatch.setattr(cursor, "close", fail_once)
    try:
        with pytest.raises(RuntimeError, match="setup cursor close failed"), resources.pool_manager.transaction(
                lambda *args: None, CONTEXT, deadline_ns=time.monotonic_ns() + 100_000_000):
            pytest.fail("setup close failure yielded")
        assert resources.state == "FAULTED"
        assert not manager._lease_records
    finally:
        pool.expected_fault = resources.state == "FAULTED"


def test_source_deadline_covers_metadata_scope_and_body(protocol_backend, monkeypatch):
    import re

    from memplex.storage import pool as pool_module

    resources, store, pool, _, _ = protocol_backend
    now = [1_000_000_000]
    monkeypatch.setattr(pool_module.time, "monotonic_ns", lambda: now[0])
    cursor = pool.connection._cursor
    execute = cursor.execute
    parameters = {"value": 23}

    def consume_budget(query, values=None):
        result = execute(query, values)
        now[0] += 10_000_000
        return result

    cursor.executed.clear()
    monkeypatch.setattr(cursor, "execute", consume_budget)
    with resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT,
                                            deadline_ns=now[0] + 100_000_000) as (_, bounded):
        bounded.execute("SELECT %(value)s", parameters)
    statements = [query for query, _ in cursor.executed]
    budgets = [int(re.search(r"lock_timeout = '(\d+)ms'", query)[1]) for query in statements]
    assert budgets == list(range(100, 100 - 10 * len(statements), -10))
    assert all("SET LOCAL statement_timeout" in query for query in statements)
    assert "SET TRANSACTION" in statements[0]
    assert "pg_catalog.current_database()" in statements[1]
    assert "role.rolsuper" in statements[2]
    assert any("set_config('memplex.tenant_id'" in query for query in statements[3:-1])
    assert cursor.executed[-1][1] is parameters


def test_deadline_cursor_preserves_composable_parameters_and_final_results(monkeypatch):
    sql = pytest.importorskip("psycopg2.sql")
    from memplex.storage import pool as pool_module

    class Cursor:
        name = None
        rowcount = 7
        description = ("value",)
        connection = object()

        def __init__(self):
            self.executed, self.closed = [], False

        def execute(self, query, parameters):
            self.executed.append((query, parameters))

        def fetchone(self):
            return (23,)

        def fetchall(self):
            return [(23,)]

        def close(self):
            self.closed = True

    now = [1_000_000_000]
    monkeypatch.setattr(pool_module.time, "monotonic_ns", lambda: now[0])
    raw = Cursor()
    bounded = pool_module._SourceDeadlineCursor(raw, now[0] + 50_000_000)
    query, parameters = sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier("role")), {"value": 23}
    bounded.execute(query, parameters)
    composed, supplied = raw.executed[0]
    assert isinstance(composed, sql.Composed) and composed.seq[1:] == query.seq
    assert supplied is parameters and "50ms" in composed.seq[0].string
    assert bounded.fetchone() == (23,) and bounded.fetchall() == [(23,)]
    assert bounded.rowcount == 7 and bounded.description == ("value",)
    assert bounded.connection is raw.connection
    for unsupported in (b"SELECT 1", object()):
        with pytest.raises(SourceContractError):
            bounded.execute(unsupported)
    for operation in ("callproc", "executemany"):
        with pytest.raises(AttributeError):
            getattr(bounded, operation)
    now[0] += 50_000_000
    with pytest.raises(SourceBusyError):
        bounded.execute("SELECT 1")
    assert len(raw.executed) == 1
    bounded.close()
    assert raw.closed


def test_source_deadline_expiry_after_execute_prevents_next_sql(protocol_backend, monkeypatch):
    from memplex.storage import pool as pool_module

    resources, _, pool, _, _ = protocol_backend
    now = [1_000_000_000]
    monkeypatch.setattr(pool_module.time, "monotonic_ns", lambda: now[0])
    cursor = pool.connection._cursor
    execute = cursor.execute

    def exhaust(query, values=None):
        execute(query, values)
        now[0] += 100_000_000

    cursor.executed.clear()
    monkeypatch.setattr(cursor, "execute", exhaust)
    with pytest.raises(SourceBusyError), resources.pool_manager.transaction(
            lambda *args: pytest.fail("expired setup bound scope"), CONTEXT, deadline_ns=now[0] + 50_000_000):
        pytest.fail("expired setup yielded")
    assert len(cursor.executed) == 1 and "SET TRANSACTION" in cursor.executed[0][0]
    assert not pool._used and resources.state == "READY"


@pytest.mark.parametrize("phase", ["target", "principal"])
def test_source_metadata_expiry_rolls_back_without_fault(protocol_backend, monkeypatch, phase):
    from memplex.storage import pool as pool_module

    resources, store, pool, _, _ = protocol_backend
    manager, connection = resources.pool_manager, pool.connection
    cursor, now = connection._cursor, [1_000_000_000]
    execute = cursor.execute
    cursor.executed.clear()
    marker = "pg_catalog.current_database()" if phase == "target" else "role.rolsuper"
    monkeypatch.setattr(pool_module.time, "monotonic_ns", lambda: now[0])

    def consume(query, parameters=None):
        result = execute(query, parameters)
        if marker in query:
            now[0] += 100_000_000
        return result

    with monkeypatch.context() as patch:
        patch.setattr(cursor, "execute", consume)
        with pytest.raises(SourceBusyError), manager.transaction(
                store._bind_transaction_scope, CONTEXT, deadline_ns=now[0] + 50_000_000):
            pytest.fail("expired metadata setup yielded")
    assert resources.state == "READY" and not manager._lease_records and not pool._used
    assert not any("memplex.tenant_id" in query for query, _ in cursor.executed)
    with manager.transaction(store._bind_transaction_scope, CONTEXT):
        pass


@pytest.mark.parametrize("failure_kind", ["target", "principal", "rollback", "return", "lost", "legacy_cancel"])
def test_source_healthy_budget_exception_does_not_weaken_faults(protocol_backend, monkeypatch, failure_kind):
    from memplex.storage.migrations import MigrationIntegrityError

    resources, store, pool, _, _ = protocol_backend
    manager, connection = resources.pool_manager, pool.connection
    cursor = connection._cursor
    execute, fetchone = cursor.execute, cursor.fetchone
    marker = "pg_catalog.current_database()" if failure_kind == "target" else "role.rolsuper"

    class Cancelled(Exception):
        pgcode = "57014"

    def run(query, parameters=None):
        if failure_kind in {"rollback", "return", "lost", "legacy_cancel"} and "role.rolsuper" in query:
            connection.status = 3
            if failure_kind == "lost":
                connection.closed = True
            raise Cancelled("cancelled")
        return execute(query, parameters)

    def fetch():
        if failure_kind in {"target", "principal"} and marker in cursor.executed[-1][0]:
            return (None, None, "wrong", "wrong") if failure_kind == "target" else ("wrong", "wrong", False, False)
        return fetchone()

    def fail_cleanup(*args):
        raise RuntimeError("cleanup failed")

    pool.expected_fault = True
    with monkeypatch.context() as patch:
        patch.setattr(cursor, "execute", run)
        patch.setattr(cursor, "fetchone", fetch)
        if failure_kind == "rollback":
            patch.setattr(connection, "rollback", fail_cleanup)
        if failure_kind == "return":
            patch.setattr(pool, "putconn", fail_cleanup)
        deadline = None if failure_kind == "legacy_cancel" else time.monotonic_ns() + 100_000_000
        expected = MigrationIntegrityError if failure_kind in {"target", "principal"} else Cancelled
        with pytest.raises(expected), manager.transaction(store._bind_transaction_scope, CONTEXT, deadline_ns=deadline):
            pytest.fail("failed verification yielded")
    assert resources.state == "FAULTED"
    assert not manager._lease_records and not manager._checked_out and manager.business_lease_count == 0
    assert len(pool.connections) == 2


def test_fixed_pool_discard_closes_outside_lock_without_driver_rollback(protocol_backend, monkeypatch):
    from memplex.storage.pool import _SourcePoolFault

    resources, _, pool, _, _ = protocol_backend
    connection = pool.getconn()
    connection.status = 3
    close = connection.close
    checked = []

    def close_unlocked():
        acquired = pool._lock.acquire(blocking=False)
        checked.append(acquired)
        if acquired:
            pool._lock.release()
        close()

    monkeypatch.setattr(connection, "close", close_unlocked)
    monkeypatch.setattr(connection, "rollback", lambda: pytest.fail("driver performed rollback"))
    with pytest.raises(_SourcePoolFault, match="without cleanup"):
        pool.putconn(connection)
    assert checked == [True] and not pool._used and len(pool.connections) == 2
    pool.expected_fault = True
    with pytest.raises(_SourcePoolFault), resources.pool_manager.transaction(lambda *args: None, CONTEXT):
        pass
    assert resources.state == "FAULTED"


@NATIVE
def test_native_request_id_is_audit_only_and_payload_remains_bound(source_backend):
    from memplex.source_contract import SourceOperation

    store, _, _, _, _, _ = source_backend
    first_context = replace(CONTEXT, request_id="request-one")
    second_context = replace(CONTEXT, request_id="request-two")
    first_backend = store.authorized(first_context).source_versions()
    second_backend = store.authorized(second_context).source_versions()
    first = _mutate(first_backend, _command(), first_context)
    command = _command(expected=first.ref.token, operation=SourceOperation.REPLACE)
    second = _mutate(second_backend, command, second_context)
    assert second.ref.token == first.ref.token and second.commit_seq > first.commit_seq
    with _fence(second_backend, second_context) as session:
        snapshot = session.source(second.ref.key)
        assert snapshot.head.ref == second.ref
        assert snapshot.payload["provenance"]["request_id"] == "request-two"
