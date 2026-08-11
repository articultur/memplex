"""Tests for the PostgreSQL memory backend (R1).

No live PostgreSQL is required: these tests cover (a) Function <-> JSONB
serialization round-trip, (b) SQL construction via a mock psycopg2
connection, and (c) the create_store factory routing for the postgres
backend.
"""

import inspect
import json
import os
import sys
import threading
from contextlib import contextmanager
from hashlib import sha256

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.models import (  # noqa: E402
    FieldValue,
    Function,
    Observation,
    SourceDocument,
    SourceType,
)
from memplex.storage.migrations import MigrationIntegrityError, PostgresTargetIdentity  # noqa: E402
from memplex.storage.migrations.runner import (  # noqa: E402
    VectorCapabilityRequest,
    VectorCapabilityStatus,
)
from memplex.storage.postgres import (  # noqa: E402
    FunctionWriteBusy,
    PostgresMemoryStore,
    _func_from_json,
    _func_to_json,
    _function_write_lock_key,
    _obs_to_json,
)
from memplex.sync_repository import SyncCapturePolicy

# ── Serialization round-trip ─────────────────────────────────────────


def _sample_func(fid="pg-1", name="login"):
    return Function(
        id=fid,
        name=name,
        name_normalized=name.lower(),
        domain="auth",
        confidence=0.9,
        source_type=SourceType.CODE,
        trigger=[
            FieldValue(desc="user logs in", sources=["t"], source_method="manual", weight=1.0)
        ],
        action=[FieldValue(desc="call auth()", sources=["t"], source_method="manual", weight=1.0)],
        attributes={"ns": "test"},
    )


def _authorization(*, tenant="tenant-a", subject="alice"):
    from memplex.auth import AuthorizationContext, Principal

    return AuthorizationContext(
        principal=Principal(
            tenant_id=tenant,
            subject_id=subject,
            roles=frozenset({"member"}),
            authentication_id=f"credential-{subject}",
        ),
        workspace_id="shared-workspace",
        agent_id="http",
        session_id=f"session-{subject}",
        request_id=f"request-{subject}",
    )


def test_func_json_roundtrip_preserves_fields():
    f = _sample_func()
    data = _func_to_json(f)
    # JSONB-safe (serialisable).
    s = json.dumps(data)
    restored = _func_from_json(json.loads(s))
    assert restored.id == f.id
    assert restored.name == f.name
    assert restored.domain == f.domain
    assert restored.source_type == SourceType.CODE
    assert [fv.desc for fv in restored.trigger] == ["user logs in"]
    assert restored.attributes == {"ns": "test"}


def test_func_to_json_includes_search_text_fields():
    f = _sample_func()
    data = _func_to_json(f)
    assert "trigger_text" in data
    assert "user logs in" in data["trigger_text"]
    assert "action_text" in data


def test_func_from_json_tolerates_missing_fields():
    restored = _func_from_json({"id": "x", "name": "n"})
    assert restored.id == "x"
    assert restored.source_type == SourceType.WIKI  # default
    assert restored.trigger == []


def test_func_from_json_bad_source_type_falls_back_to_wiki():
    restored = _func_from_json({"id": "x", "source_type": "not-a-real-type"})
    assert restored.source_type == SourceType.WIKI


# ── Mock-connection fixture ──────────────────────────────────────────


class _MockCursor:
    def __init__(self):
        self.executed = []  # list of (sql, params)
        self._result = []
        self._fetchone_val = None

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if "set_config('memplex.tenant_id'" in sql:
            self._probe_tenant = params[0]

    def fetchone(self):
        if not self.executed:
            return self._fetchone_val
        sql, params = self.executed[-1]
        if any("memplex_sync_local_identity" in str(param) for param in params):
            return (False,)
        if any(
            "memplex_configure_sync_local_identity" in str(param)
            for param in params
        ):
            return (False,)
        if "pg_catalog.current_database()" in sql:
            return getattr(self, "_target_row", ("fake", "public", None, None))
        if "role.rolsuper" in sql:
            return ("fake", "fake", False, False)
        if "SELECT current_user, session_user" in sql:
            return ("fake", "fake")
        if getattr(self, "_force_returning_none", False) and "RETURNING" in sql:
            return None
        if "RETURNING source, target, edge_type" in sql:
            return tuple(params[:3])
        if "RETURNING id" in sql:
            if len(params) > 1 and isinstance(params[0], str) and params[0].startswith("memplex-readiness-"):
                return (params[1],)
            if len(params) > 1 and isinstance(params[0], str) and params[0].startswith("{"):
                return (params[-1],)
            return (params[0],)
        if "has_schema_privilege" in sql:
            return (True,) * 8
        if "attribute.atttypid" in sql and "embedding" in sql:
            return (1, 4, "public", "vector")
        if "has_sequence_privilege" in sql or "has_type_privilege" in sql:
            return (True,)
        if "has_function_privilege" in sql:
            return (True,)
        if "memplex_sync_local_identity" in sql:
            return (False,)
        if "memplex_configure_sync_local_identity" in sql:
            return (False,)
        if "session_user = current_user" in sql:
            return (True,) * 5
        if "SELECT %s::" in sql:
            return ("[]",)
        if "RETURNING memory_id" in sql:
            return (params[1] if "WHERE tenant_id" in sql else params[0],)
        if "RETURNING source" in sql:
            return (params[1],)
        if "SELECT id FROM" in sql or "SELECT memory_id FROM" in sql:
            if "-other" in getattr(self, "_probe_tenant", ""):
                return None
            return (params[-1],)
        if "SELECT source FROM memplex_edges" in sql:
            return (params[1],)
        return self._fetchone_val

    def fetchall(self):
        if self.executed and "SELECT table_name, has_table_privilege" in self.executed[-1][0]:
            return [
                ("memplex_sync_batches", True),
                ("memplex_sync_cursors", True),
                ("memplex_sync_deliveries", True),
                ("memplex_sync_entity_versions", True),
                ("memplex_sync_inbox", True),
                ("memplex_sync_outbox", True),
                ("memplex_sync_snapshot_items", True),
                ("memplex_sync_snapshots", True),
                ("memplex_sync_stream_state", True),
                ("memplex_sync_targets", True),
            ]
        if self.executed and "DELETE FROM memplex_functions" in self.executed[-1][0]:
            params = self.executed[-1][1]
            return [(params[-2],), (params[-1],)]
        return self._result

    def close(self):
        pass


class _MockConn:
    def __init__(self):
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self._cursor = _MockCursor()

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _PoolMockCursor(_MockCursor):
    def __init__(self):
        super().__init__()
        self.closed = 0

    def close(self):
        self.closed += 1


class _PoolMockConnection:
    def __init__(self):
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self.cursor_instance = _PoolMockCursor()

    @property
    def _cursor(self):
        return self.cursor_instance

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _PoolMock:
    def __init__(self):
        self.connection = _PoolMockConnection()
        self.put_calls = []
        self.closeall_calls = 0

    def getconn(self):
        return self.connection

    def putconn(self, connection):
        self.put_calls.append(connection)

    def closeall(self):
        self.closeall_calls += 1


def _test_pool_manager():
    from memplex.storage.pool import PostgresPoolManager

    return PostgresPoolManager("dbname=fake", pool=_PoolMock())


def _test_target():
    return PostgresTargetIdentity(
        database="fake", schema="public", server_address=None, server_port=None
    )


def _test_runner(*, target=None, apply=None, ensure=None, verify=None):
    """Build an exact runner for the private resource-construction seam."""
    from memplex.storage.migrations import PostgresMigrationRunner
    from memplex.storage.migrations.runner import VectorCapabilityStatus

    runner = PostgresMigrationRunner("dbname=fake")
    runner.inspect_target = lambda: _test_target() if target is None else target
    from memplex.storage.migrations.runner import PostgresApplicationPrincipal
    runner.inspect_application_principal = lambda **_kwargs: PostgresApplicationPrincipal("fake", "fake")
    if apply is None:
        runner.apply = lambda *, expected_target=None, application_acl=None: None
    else:
        runner.apply = lambda *, expected_target=None, application_acl=None: apply(
            expected_target=expected_target
        )
    if ensure is None:
        runner.ensure_vector_capability = lambda _request, _profile, *, expected_target=None, application_acl=None: VectorCapabilityStatus(
            state="disabled", dim=0
        )
    else:
        runner.ensure_vector_capability = lambda request, profile, *, expected_target=None, application_acl=None: ensure(
            request, profile, expected_target=expected_target
        )
    if verify is None:
        def default_verify(request, _profile, *, expected_target=None, application_acl=None):
            if request.policy == "disabled":
                return VectorCapabilityStatus(state="disabled", dim=0)
            return VectorCapabilityStatus(
                state="ready",
                dim=request.dim,
                parameter_digest=sha256(f"pgvector:{request.dim}".encode("ascii")).hexdigest(),
            )

        runner.verify_storage_readiness = default_verify
    else:
        runner.verify_storage_readiness = lambda request, profile, *, expected_target=None, application_acl=None: verify(
            request, profile, expected_target=expected_target
        )
    return runner


def _install_resource_runner_sequence(
    monkeypatch,
    *,
    application_target=None,
    migration_target=None,
    apply=None,
    ensure=None,
    verify=None,
):
    """Replace only the module-private runner constructor for one test.

    This is a unit-test I/O seam, not a public readiness collaborator.
    Production code always obtains fresh exact runners from its own DSNs.
    """
    from memplex.storage import pool as pool_module

    runners = [
        _test_runner(target=application_target),
        _test_runner(target=migration_target, apply=apply, ensure=ensure),
        _test_runner(verify=verify),
    ]

    def factory(_dsn):
        if runners:
            return runners.pop(0)
        return _test_runner()

    monkeypatch.setattr(pool_module, "_new_migration_runner", factory)


@pytest.fixture(autouse=True)
def _mock_resource_runner_construction(monkeypatch):
    """Keep resource tests database-free through the private test seam."""
    _install_resource_runner_sequence(monkeypatch)


def _test_ready_pool(*, dim: int = 0, pool=None):
    """Issue a real readiness seal over the deterministic mock pool."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    policy = "disabled" if dim == 0 else "best_effort"
    request = VectorCapabilityRequest(dim=dim, policy=policy)
    raw_pool = pool or _PoolMock()
    resources = PostgresStorageResources(
        dsn="dbname=fake",
        pool_factory=lambda *_args: raw_pool,
    )
    resources.ensure_ready(request, "development")
    # Readiness deliberately exercised the candidate connection.  Product
    # SQL assertions below start from the published-pool baseline.
    raw_pool.connection._cursor.executed.clear()
    return resources.ready_pool


def test_pool_capacity_configuration_is_exact_and_diagnostic_only():
    """Capacity bounds reject bool-like inputs and remain safe diagnostics."""
    from memplex.storage.pool import PostgresPoolManager

    for minimum, maximum in ((True, 1), (1, True), (1.0, 1), (1, 1.0)):
        with pytest.raises(TypeError, match="bounds"):
            PostgresPoolManager(
                "dbname=fake",
                min_connections=minimum,
                max_connections=maximum,
                pool=_PoolMock(),
            )

    manager = PostgresPoolManager(
        "dbname=fake", min_connections=2, max_connections=3, pool=_PoolMock()
    )
    assert manager.min_connections == 2
    assert manager.max_connections == 3
    assert manager.business_lease_high_watermark == 0


def test_pool_capacity_limits_live_leases_and_keeps_high_watermark_after_close():
    """A capacity waiter cannot become a lease until a real lease returns."""
    from memplex.storage.pool import PostgresPoolManager

    class _DistinctConnectionPool(_PoolMock):
        def __init__(self):
            super().__init__()
            self.connections = [_PoolMockConnection() for _ in range(3)]
            self.get_calls = 0

        def getconn(self):
            connection = self.connections[self.get_calls]
            self.get_calls += 1
            return connection

    raw_pool = _DistinctConnectionPool()
    manager = PostgresPoolManager(
        "dbname=fake", min_connections=1, max_connections=2, pool=raw_pool
    )
    first = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    second = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    assert manager.business_lease_count == 2
    assert manager.business_lease_high_watermark == 2

    borrowed: list[object] = []
    borrow_errors: list[BaseException] = []
    started = threading.Event()

    def wait_for_capacity():
        started.set()
        try:
            borrowed.append(
                manager.read_cursor(lambda _cursor, _context: None, _authorization())
            )
        except BaseException as exc:
            borrow_errors.append(exc)

    waiter = threading.Thread(target=wait_for_capacity)
    waiter.start()
    assert started.wait(timeout=1)
    waiter.join(timeout=0.05)
    assert waiter.is_alive()
    assert raw_pool.get_calls == 2
    assert manager.business_lease_count == 2
    assert manager.business_lease_high_watermark == 2

    first.close()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert borrow_errors == []
    assert len(borrowed) == 1
    assert manager.business_lease_count == 2
    assert manager.business_lease_high_watermark == 2

    second.close()
    borrowed[0].close()
    assert manager.business_lease_count == 0
    assert manager.close() is True
    assert manager.business_lease_high_watermark == 2


def test_pool_failed_target_borrow_never_advances_capacity_watermark():
    """Only a validated, published business lease contributes to the metric."""
    from memplex.storage.migrations import MigrationIntegrityError
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    raw_pool.connection.cursor_instance._target_row = ("other", "public", None, None)
    manager = PostgresPoolManager(
        "dbname=fake", pool=raw_pool, expected_target=_test_target()
    )

    with pytest.raises(MigrationIntegrityError, match="pool target"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0
    with manager._condition:
        assert not manager._borrow_queue
        assert manager._waiting_borrowers == 0
        assert not manager._checked_out


def test_pool_fault_keeps_the_last_valid_capacity_high_watermark():
    """A later target fault must not erase already-observed valid demand."""
    from memplex.storage.migrations import MigrationIntegrityError
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager(
        "dbname=fake", pool=raw_pool, expected_target=_test_target()
    )
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    cursor.close()
    assert manager.business_lease_high_watermark == 1

    raw_pool.connection.cursor_instance._target_row = ("other", "public", None, None)
    with pytest.raises(MigrationIntegrityError, match="pool target"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())
    assert manager.business_lease_high_watermark == 1


def test_resources_expose_non_sensitive_pool_capacity_diagnostics():
    """Resources expose only aggregate pool limits and history, never their DSN."""
    from memplex.storage.pool import PostgresStorageResources

    resources = PostgresStorageResources(
        dsn="postgresql://secret@example.invalid/private",
        pool_factory=lambda *_args: _PoolMock(),
    )
    assert resources.pool_max_connections == 0
    assert resources.pool_high_watermark == 0

    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    assert resources.pool_max_connections == 8
    # Resource readiness borrows only for probes.  It is not product traffic.
    assert resources.pool_high_watermark == 0
    resources.close()
    assert resources.pool_high_watermark == 0


def test_pool_scope_binding_failure_never_publishes_business_demand():
    """A checked-out connection becomes business demand only after scope binding."""
    from memplex.storage.pool import PostgresPoolManager

    manager = PostgresPoolManager("dbname=fake", pool=_PoolMock())
    with pytest.raises(RuntimeError, match="scope bind failed"):
        manager.read_cursor(
            lambda _cursor, _context: (_ for _ in ()).throw(RuntimeError("scope bind failed")),
            _authorization(),
        )
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0


@pytest.mark.parametrize("failure_point", ("getconn", "cursor"))
def test_pool_pre_publish_driver_failures_never_raise_business_watermark(failure_point):
    """Driver acquisition and cursor setup are capacity work, not business work."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    if failure_point == "getconn":
        raw_pool.getconn = lambda: (_ for _ in ()).throw(RuntimeError("getconn failed"))
    else:
        raw_pool.connection.cursor = lambda: (_ for _ in ()).throw(RuntimeError("cursor failed"))
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)

    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0
    with manager._condition:
        assert not manager._borrow_queue
        assert manager._waiting_borrowers == 0
        assert not manager._checked_out


def test_pool_fifo_capacity_waiters_and_driver_peak_are_bounded():
    """Queued callers acquire one capacity slot in arrival order, never in a herd."""
    from memplex.storage.pool import PostgresPoolManager

    class _FifoPool(_PoolMock):
        def __init__(self):
            super().__init__()
            self.connections = [_PoolMockConnection() for _ in range(4)]
            self.get_calls = 0
            self.checked_out = 0
            self.peak_checked_out = 0

        def getconn(self):
            connection = self.connections[self.get_calls]
            self.get_calls += 1
            self.checked_out += 1
            self.peak_checked_out = max(self.peak_checked_out, self.checked_out)
            return connection

        def putconn(self, connection):
            self.checked_out -= 1
            super().putconn(connection)

    raw_pool = _FifoPool()
    manager = PostgresPoolManager(
        "dbname=fake", min_connections=1, max_connections=1, pool=raw_pool
    )
    first = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    acquisition_order: list[int] = []
    failures: list[BaseException] = []
    threads: list[threading.Thread] = []

    def wait_for_slot(index: int) -> None:
        try:
            cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
            acquisition_order.append(index)
            cursor.close()
        except BaseException as exc:
            failures.append(exc)

    for index in range(3):
        thread = threading.Thread(target=wait_for_slot, args=(index,))
        thread.start()
        threads.append(thread)
        for _ in range(100):
            with manager._condition:
                queued = len(manager._borrow_queue)
            if queued == index + 1:
                break
            threading.Event().wait(0.005)
        assert queued == index + 1

    first.close()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert failures == []
    assert acquisition_order == [0, 1, 2]
    assert raw_pool.peak_checked_out == 1
    assert manager.business_lease_high_watermark == 1


@pytest.mark.parametrize("terminal", ("close", "fault"))
def test_pool_terminal_state_wakes_capacity_waiters_without_leaking_tickets(terminal):
    """Close and fault release FIFO waiters instead of leaving shutdown blocked."""
    from memplex.storage.pool import PostgresPoolManager

    manager = PostgresPoolManager(
        "dbname=fake", min_connections=1, max_connections=1, pool=_PoolMock()
    )
    first = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    failures: list[BaseException] = []

    def wait_for_slot() -> None:
        try:
            manager.read_cursor(lambda _cursor, _context: None, _authorization())
        except BaseException as exc:
            failures.append(exc)

    waiter = threading.Thread(target=wait_for_slot)
    waiter.start()
    for _ in range(100):
        with manager._condition:
            queued = len(manager._borrow_queue)
        if queued == 1:
            break
        threading.Event().wait(0.005)
    assert queued == 1

    if terminal == "close":
        assert manager.close(wait=False) is False
    else:
        manager._mark_fault(RuntimeError("simulated pool fault"))
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    with manager._condition:
        assert not manager._borrow_queue
        assert manager._waiting_borrowers == 0

    first.close()


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_pool_interrupted_fifo_waiter_removes_its_ticket_and_capacity_reservation(
    monkeypatch, interrupt_type
):
    """BaseException during Condition.wait cannot strand FIFO capacity state."""
    from memplex.storage.pool import PostgresPoolManager

    manager = PostgresPoolManager(
        "dbname=fake", min_connections=1, max_connections=1, pool=_PoolMock()
    )
    first = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    original_wait = manager._condition.wait

    def interrupt_wait(*_args, **_kwargs):
        raise interrupt_type("simulated wait interruption")

    monkeypatch.setattr(manager._condition, "wait", interrupt_wait)
    with pytest.raises(interrupt_type, match="simulated wait interruption"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())
    with manager._condition:
        assert not manager._borrow_queue
        assert manager._waiting_borrowers == 0

    monkeypatch.setattr(manager._condition, "wait", original_wait)
    first.close()
    replacement = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    replacement.close()
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize("notify_number", (1, 2))
def test_pool_reservation_handoff_interrupt_leaks_no_capacity(
    monkeypatch, interrupt_type, notify_number
):
    """Notify failure at slot hand-off or checkout registration unwinds state."""
    from memplex.storage.pool import PostgresPoolManager

    manager = PostgresPoolManager(
        "dbname=fake", min_connections=1, max_connections=1, pool=_PoolMock()
    )
    original_notify = manager._condition.notify_all
    calls = 0

    def interrupt_once():
        nonlocal calls
        calls += 1
        if calls == notify_number:
            raise interrupt_type("simulated handoff interruption")
        original_notify()

    monkeypatch.setattr(manager._condition, "notify_all", interrupt_once)
    with pytest.raises(interrupt_type, match="simulated handoff interruption"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())
    with manager._condition:
        assert not manager._borrow_queue
        assert manager._waiting_borrowers == 0
        assert not manager._checked_out
        assert manager.business_lease_count == 0

    monkeypatch.setattr(manager._condition, "notify_all", original_notify)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    cursor.close()
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_published_but_unreturned_handle_is_reclaimed_on_context_exception(interrupt_type):
    """A published lease has no peak/history effect until context hand-off succeeds."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    with pytest.raises(interrupt_type, match="before handle handoff"):
        with manager._borrow_capacity_reservation() as reservation:
            connection = reservation.borrow()
            reservation.publish(connection)
            raise interrupt_type("before handle handoff")
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0
    with manager._condition:
        assert not manager._checked_out
        assert manager._waiting_borrowers == 0
        assert not manager._borrow_queue
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_read_cursor_return_boundary_interrupt_reclaims_published_handle(interrupt_type):
    """The actual `return wrapped` boundary cannot leak an unreachable cursor."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    source, start_line = inspect.getsourcelines(PostgresPoolManager.read_cursor)
    target_line = start_line + next(
        index for index, line in enumerate(source) if "return wrapped" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is PostgresPoolManager.read_cursor.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated read return interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated read return interruption"):
            manager.read_cursor(lambda _cursor, _context: None, _authorization())
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0
    with manager._condition:
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_transaction_yield_boundary_interrupt_reclaims_unhanded_lease(interrupt_type):
    """A trace interruption before generator hand-off leaves no business demand."""
    from memplex.storage.pool import PostgresPoolManager, _TransactionContext

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    source, start_line = inspect.getsourcelines(_TransactionContext.__enter__)
    target_line = start_line + next(
        index for index, line in enumerate(source) if "return connection, cursor" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is _TransactionContext.__enter__.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated transaction yield interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated transaction yield interruption"):
            with manager.transaction(lambda _cursor, _context: None, _authorization()):
                raise AssertionError("must not enter transaction body")
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.connection.cursor_instance.closed == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0
    with manager._condition:
        assert not manager._checked_out
    assert manager.close(wait=True) is True


def test_overlapping_transactions_publish_live_count_and_peak_demand():
    """Concurrent transaction bodies contribute to live and historical demand."""
    from memplex.storage.pool import PostgresPoolManager

    class _DistinctPool(_PoolMock):
        def __init__(self):
            super().__init__()
            self.connections = [_PoolMockConnection(), _PoolMockConnection()]
            self.index = 0

        def getconn(self):
            connection = self.connections[self.index]
            self.index += 1
            return connection

    manager = PostgresPoolManager(
        "dbname=fake", min_connections=1, max_connections=2, pool=_DistinctPool()
    )
    entered = threading.Barrier(3)
    release = threading.Event()

    def transact() -> None:
        with manager.transaction(lambda _cursor, _context: None, _authorization()):
            entered.wait(timeout=1)
            assert release.wait(timeout=1)

    threads = [threading.Thread(target=transact) for _ in range(2)]
    for thread in threads:
        thread.start()
    entered.wait(timeout=1)
    assert manager.business_lease_count == 2
    assert manager.business_lease_high_watermark == 2
    release.set()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 2


def test_transaction_body_error_remains_in_historical_peak_demand():
    """A product error after entry must not erase observed transaction demand."""
    from memplex.storage.pool import PostgresPoolManager

    manager = PostgresPoolManager("dbname=fake", pool=_PoolMock())
    with pytest.raises(RuntimeError, match="body error"):
        with manager.transaction(lambda _cursor, _context: None, _authorization()):
            assert manager.business_lease_count == 1
            raise RuntimeError("body error")
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 1


@pytest.mark.parametrize("body_error", (KeyboardInterrupt, SystemExit, BaseException))
def test_transaction_body_baseexception_remains_in_historical_peak_demand(body_error):
    """Explicit context exit distinguishes delivered BaseException from enter interruption."""
    from memplex.storage.pool import PostgresPoolManager

    manager = PostgresPoolManager("dbname=fake", pool=_PoolMock())
    with pytest.raises(body_error, match="body interruption"):
        with manager.transaction(lambda _cursor, _context: None, _authorization()):
            assert manager.business_lease_count == 1
            raise body_error("body interruption")
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 1


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize(
    "target_statement",
    ("reservation_context.__enter__()", "self._reservation = reservation"),
)
def test_transaction_enter_interrupt_releases_local_reservation(
    interrupt_type, target_statement
):
    """Trace interruption before transaction ownership is published cannot leak a slot."""
    from memplex.storage.pool import PostgresPoolManager, _TransactionContext

    manager = PostgresPoolManager("dbname=fake", pool=_PoolMock())
    transaction = manager.transaction(lambda _cursor, _context: None, _authorization())
    source, start_line = inspect.getsourcelines(_TransactionContext.__enter__)
    target_line = start_line + next(
        index for index, line in enumerate(source) if target_statement in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is _TransactionContext.__enter__.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated transaction enter interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated transaction enter interruption"):
            transaction.__enter__()
    finally:
        sys.settrace(previous)
    assert injected
    with manager._condition:
        assert not manager._lease_records
        assert manager._waiting_borrowers == 0
        assert not manager._borrow_queue
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    cursor.close()
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_reservation_enter_return_interrupt_releases_reserved_slot(interrupt_type):
    """The lower-level context cleans itself before Python can skip `__exit__`."""
    from memplex.storage.pool import PostgresPoolManager, _BorrowCapacityReservation

    manager = PostgresPoolManager("dbname=fake", pool=_PoolMock())
    reservation = _BorrowCapacityReservation(manager)
    source, start_line = inspect.getsourcelines(_BorrowCapacityReservation.__enter__)
    target_line = start_line + next(
        index for index, line in enumerate(source) if "return self" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is _BorrowCapacityReservation.__enter__.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated reservation return interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated reservation return interruption"):
            reservation.__enter__()
    finally:
        sys.settrace(previous)
    assert injected
    with manager._condition:
        assert not manager._lease_records
        assert manager._waiting_borrowers == 0
        assert not manager._borrow_queue
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("body_error", (ValueError, KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize("cleanup_stage", ("handoff", "rollback", "cursor", "release"))
def test_transaction_body_primary_survives_every_cleanup_failure(
    body_error, cleanup_stage
):
    """Once body entry is known, cleanup faults never replace its BaseException."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    transaction = manager.transaction(lambda _cursor, _context: None, _authorization())
    with pytest.raises(body_error, match="body primary"):
        with transaction:
            if cleanup_stage == "handoff":
                manager.commit_publish = lambda _token: (_ for _ in ()).throw(
                    RuntimeError("handoff cleanup failed")
                )
            elif cleanup_stage == "rollback":
                raw_pool.connection.rollback = lambda: (_ for _ in ()).throw(
                    RuntimeError("rollback cleanup failed")
                )
            elif cleanup_stage == "cursor":
                raw_pool.connection.cursor_instance.close = lambda: (_ for _ in ()).throw(
                    RuntimeError("cursor cleanup failed")
                )
            else:
                assert transaction._reservation is not None
                transaction._reservation.release = lambda: (_ for _ in ()).throw(
                    RuntimeError("release cleanup failed")
                )
            raise body_error("body primary")


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize("target_statement", ("self._connection = connection", "self._cursor = cursor"))
def test_transaction_enter_owner_assignment_interrupt_cleans_local_resources(
    interrupt_type, target_statement
):
    """Locals remain cleanup authority until transaction ownership is assigned."""
    from memplex.storage.pool import PostgresPoolManager, _TransactionContext

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    transaction = manager.transaction(lambda _cursor, _context: None, _authorization())
    source, start_line = inspect.getsourcelines(_TransactionContext.__enter__)
    target_line = start_line + next(
        index for index, line in enumerate(source) if target_statement in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is _TransactionContext.__enter__.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated owner assignment interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated owner assignment interruption"):
            transaction.__enter__()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.connection.cursor_instance.closed == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    with manager._condition:
        assert not manager._lease_records
        assert not manager._checked_out
    replacement = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    replacement.close()
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_manager_returning_token_is_claimable_after_pre_put_interrupt(interrupt_type):
    """An interrupted RETURNING transition is retried without duplicate putconn."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    source, start_line = inspect.getsourcelines(PostgresPoolManager.release)
    target_line = start_line + next(
        index for index, line in enumerate(source) if "if rollback:" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is PostgresPoolManager.release.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated pre-put interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated pre-put interruption"):
            cursor.close()
    finally:
        sys.settrace(previous)
    assert injected
    cursor.close()
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    with manager._condition:
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize(
    ("code", "target_statement"),
    (
        ("borrow", "self._connection = connection"),
        ("borrow", "self._manager._attach_borrowed_connection(self._token, connection)"),
        ("attach", "record.connection = connection"),
    ),
)
def test_getconn_handoff_interrupt_returns_local_connection_once(
    interrupt_type, code, target_statement
):
    """A post-getconn interruption gives the manager enough ownership to return it."""
    from memplex.storage.pool import PostgresPoolManager, _BorrowCapacityReservation

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    reservation = _BorrowCapacityReservation(manager)
    reservation.__enter__()
    target = (
        _BorrowCapacityReservation.borrow
        if code == "borrow"
        else PostgresPoolManager._attach_borrowed_connection
    )
    source, start_line = inspect.getsourcelines(target)
    target_line = start_line + next(
        index for index, line in enumerate(source) if target_statement in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated getconn handoff interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated getconn handoff interruption"):
            reservation.borrow()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    with manager._condition:
        assert not manager._lease_records
        assert not manager._borrow_queue
        assert manager._waiting_borrowers == 0
        assert not manager._checked_out
    replacement = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    replacement.close()
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_putconn_call_line_interrupt_keeps_token_claimable(interrupt_type):
    """A trace before the atomic put-attempt line has not called the driver yet."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    source, start_line = inspect.getsourcelines(PostgresPoolManager.release)
    target_line = start_line + next(
        index for index, line in enumerate(source) if "put_attempted = True;" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is PostgresPoolManager.release.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated putconn call-line interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated putconn call-line interruption"):
            cursor.close()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.put_calls == []
    with manager._condition:
        assert len(manager._lease_records) == 1
        assert manager._checked_out == {id(raw_pool.connection)}
    cursor.close()
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize(
    "target_statement",
    (
        "self._close_pre_enter_cursor(cursor)",
        "if reservation_context is not None:",
    ),
)
def test_transaction_enter_primary_survives_cleanup_call_interrupt(
    interrupt_type, target_statement
):
    """A secondary cleanup interruption cannot strand a failed bind lease."""
    from memplex.storage.pool import PostgresPoolManager, _TransactionContext

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    transaction = manager.transaction(
        lambda _cursor, _context: (_ for _ in ()).throw(ValueError("bind primary")),
        _authorization(),
    )
    source, start_line = inspect.getsourcelines(_TransactionContext.__enter__)
    target_line = start_line + next(
        index for index, line in enumerate(source) if target_statement in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is _TransactionContext.__enter__.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated enter cleanup interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(ValueError, match="bind primary"):
            transaction.__enter__()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.connection.cursor_instance.closed == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    with manager._condition:
        assert not manager._lease_records
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_borrow_primary_survives_release_call_interrupt(interrupt_type):
    """Verification failure remains primary when its first release call is interrupted."""
    from memplex.storage.pool import PostgresPoolManager, _BorrowCapacityReservation

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    manager._verify_borrowed_connection_target = lambda _connection: (_ for _ in ()).throw(
        ValueError("verify primary")
    )
    reservation = _BorrowCapacityReservation(manager)
    reservation.__enter__()
    source, start_line = inspect.getsourcelines(_BorrowCapacityReservation.borrow)
    target_line = start_line + next(
        index
        for index, line in enumerate(source)
        if "self._manager.release(self._token, fallback_connection=connection)" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is _BorrowCapacityReservation.borrow.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated borrow release interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(ValueError, match="verify primary"):
            reservation.borrow()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    with manager._condition:
        assert not manager._lease_records
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize("phase", ("cursor_gate", "nested_exit"))
def test_transaction_body_primary_survives_finally_phase_interrupt(interrupt_type, phase):
    """Every finally phase still reaches a manager release without masking the body."""
    from memplex.storage.pool import PostgresPoolManager, _TransactionContext

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    transaction = manager.transaction(lambda _cursor, _context: None, _authorization())
    target = (
        _TransactionContext._close_transaction_cursor
        if phase == "cursor_gate"
        else _TransactionContext.__exit__
    )
    source, start_line = inspect.getsourcelines(target)
    statement = (
        "if self._cursor is not None"
        if phase == "cursor_gate"
        else "self._reservation_context.__exit__(None, None, None)"
    )
    target_line = start_line + next(
        index for index, line in enumerate(source) if statement in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated transaction finally interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(ValueError, match="body primary"):
            with transaction:
                raise ValueError("body primary")
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.connection.cursor_instance.closed == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    with manager._condition:
        assert not manager._lease_records
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize("scenario", ("body", "commit"))
def test_reservation_exit_release_interrupt_keeps_published_token_reclaimable(
    interrupt_type, scenario
):
    """Both reservation exit branches retry cleanup after a release-line interruption."""
    from memplex.storage.pool import PostgresPoolManager, _BorrowCapacityReservation

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    source, start_line = inspect.getsourcelines(_BorrowCapacityReservation.__exit__)
    release_lines = [
        start_line + index
        for index, line in enumerate(source)
        if "self._manager.release(self._token)" in line
    ]
    target_line = release_lines[0 if scenario == "commit" else 2]
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is _BorrowCapacityReservation.__exit__.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated reservation exit release interruption")
        return tracer

    def use_reservation() -> None:
        with _BorrowCapacityReservation(manager) as reservation:
            connection = reservation.borrow()
            reservation.publish(connection)
            if scenario == "body":
                raise ValueError("body primary")

    if scenario == "commit":
        manager.commit_publish = lambda _token: (_ for _ in ()).throw(
            ValueError("commit primary")
        )
        message = "commit primary"
    else:
        message = "body primary"
    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(ValueError, match=message):
            use_reservation()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    with manager._condition:
        assert not manager._lease_records
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_fetch_primary_survives_close_call_interrupt_and_reclaims_lease(interrupt_type):
    """A failed cursor fetch has a second close path if the first is interrupted."""
    from memplex.storage.pool import PooledReadCursor, PostgresPoolManager

    raw_pool = _PoolMock()
    raw_pool.connection.cursor_instance.fetchone = lambda: (_ for _ in ()).throw(
        ValueError("fetch primary")
    )
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    source, start_line = inspect.getsourcelines(PooledReadCursor._fetch)
    target_line = start_line + next(
        index for index, line in enumerate(source) if "self.close()" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is PooledReadCursor._fetch.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated fetch close interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(ValueError, match="fetch primary"):
            cursor.fetchone()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.cursor_instance.closed == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    with manager._condition:
        assert not manager._lease_records
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize(
    ("path", "expected_error"),
    (
        ("read", ValueError),
        ("target", ValueError),
        ("role", MigrationIntegrityError),
        ("access", MigrationIntegrityError),
    ),
)
def test_internal_reservation_cleanup_phase_interrupt_preserves_public_primary(
    monkeypatch, interrupt_type, path, expected_error
):
    """Every reservation consumer retries local cleanup before releasing capacity."""
    from memplex.storage import pool as pool_module
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    if path == "target":
        monkeypatch.setattr(
            pool_module,
            "inspect_postgres_connection_target",
            lambda _connection, _cursor: (_ for _ in ()).throw(ValueError("target primary")),
        )
    elif path == "role":
        raw_pool.connection.cursor_instance.execute = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("role primary")
        )
    elif path == "access":
        manager._probe_application_access = lambda *_args: (_ for _ in ()).throw(
            ValueError("access primary")
        )

    source, start_line = inspect.getsourcelines(
        PostgresPoolManager._cleanup_reservation_local_resources
    )
    target_line = start_line + next(
        index for index, line in enumerate(source) if "if not state.rolled_back:" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code
            is PostgresPoolManager._cleanup_reservation_local_resources.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated reservation cleanup phase interruption")
        return tracer

    def invoke() -> None:
        if path == "read":
            manager.read_cursor(
                lambda _cursor, _context: (_ for _ in ()).throw(ValueError("read primary")),
                _authorization(),
            )
        elif path == "target":
            manager.inspect_target()
        elif path == "role":
            manager.inspect_application_role()
        else:
            manager.verify_application_access(
                target=_test_target(), profile="development", vector_dim=0
            )

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(expected_error):
            invoke()
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.connection.cursor_instance.closed == 1
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0
    with manager._condition:
        assert not manager._lease_records
        assert not manager._checked_out
    assert manager.close(wait=True) is True


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
def test_commit_publish_interrupt_rolls_back_unhanded_high_watermark(interrupt_type):
    """An interruption after HWM assignment cannot retain an unreturned peak."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    source, start_line = inspect.getsourcelines(PostgresPoolManager.commit_publish)
    target_line = start_line + next(
        index
        for index, line in enumerate(source)
        if "self._business_lease_high_watermark < previous_high_watermark" in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is PostgresPoolManager.commit_publish.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated commit publish interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated commit publish interruption"):
            manager.read_cursor(lambda _cursor, _context: None, _authorization())
    finally:
        sys.settrace(previous)
    assert injected
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    assert manager.business_lease_high_watermark == 0
    with manager._condition:
        assert not manager._checked_out
    assert manager.close(wait=True) is True


def test_putconn_failure_is_faulted_and_never_retries_the_same_connection():
    """A driver return error clears accounting once and lets closeall converge."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    calls = 0

    def put_fails(connection):
        nonlocal calls
        calls += 1
        raw_pool.put_calls.append(connection)
        raise RuntimeError("put failed")

    raw_pool.putconn = put_fails
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    with pytest.raises(RuntimeError, match="put failed"):
        cursor.close()
    cursor.close()
    assert calls == 1
    assert manager.business_lease_count == 0
    with manager._condition:
        assert not manager._checked_out
        assert not manager._lease_records
    assert manager.close(wait=True) is True
    assert raw_pool.closeall_calls == 1
    with pytest.raises(RuntimeError, match="faulted"):
        manager.close(wait=True)


def test_putconn_fault_callback_converges_to_closeall_after_returning_cleanup():
    """A resource-style non-waiting fault close finalizes after putconn fails."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    holder: dict[str, PostgresPoolManager] = {}

    def put_fails(connection):
        raw_pool.put_calls.append(connection)
        raise RuntimeError("put failed")

    def close_on_fault(_error):
        holder["manager"].close(wait=False)

    raw_pool.putconn = put_fails
    manager = PostgresPoolManager(
        "dbname=fake", pool=raw_pool, on_fault=close_on_fault
    )
    holder["manager"] = manager
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    with pytest.raises(RuntimeError, match="put failed"):
        cursor.close()
    assert raw_pool.put_calls == [raw_pool.connection]
    assert raw_pool.closeall_calls == 1
    assert manager.closed is True
    with manager._condition:
        assert not manager._checked_out
        assert not manager._lease_records


@pytest.mark.parametrize("interrupt_type", (KeyboardInterrupt, SystemExit, BaseException))
@pytest.mark.parametrize(
    "target_statement",
    (
        "cleanup_error: BaseException | None = None",
        "self._rolled_back = True",
        "self._cursor_closed = True",
        "self._returned = True",
    ),
)
def test_pooled_cursor_close_is_retryable_after_line_interrupt(
    interrupt_type, target_statement
):
    """No trace interruption can turn an unreturned lease into a false close."""
    from memplex.storage.pool import PooledReadCursor, PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=raw_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    source, start_line = inspect.getsourcelines(PooledReadCursor.close)
    target_line = start_line + next(
        index for index, line in enumerate(source) if target_statement in line
    )
    injected = False

    def tracer(frame, event, _arg):
        nonlocal injected
        if (
            not injected
            and event == "line"
            and frame.f_code is PooledReadCursor.close.__code__
            and frame.f_lineno == target_line
        ):
            injected = True
            raise interrupt_type("simulated cursor close interruption")
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        with pytest.raises(interrupt_type, match="simulated cursor close interruption"):
            cursor.close()
    finally:
        sys.settrace(previous)
    assert injected
    cursor.close()
    assert raw_pool.put_calls == [raw_pool.connection]
    assert manager.business_lease_count == 0
    with manager._condition:
        assert not manager._checked_out
        assert manager._waiting_borrowers == 0
    assert manager.close(wait=True) is True


def test_application_probe_uses_savepoints_for_negative_rls_with_check_writes():
    """Production proof must recover failed cross-tenant INSERTs before sealing."""
    from memplex.storage.pool import PostgresPoolManager

    class _RlsCursor(_MockCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if (
                "INSERT INTO memplex_functions" in sql
                or "INSERT INTO feedback" in sql
            ) and "-other" in getattr(self, "_probe_tenant", ""):
                raise PermissionError("new row violates row-level security policy")

    cursor = _RlsCursor()
    PostgresPoolManager._probe_application_access(
        cursor, _test_target(), "production", 0
    )

    statements = [str(sql) for sql, _params in cursor.executed]
    for name in ("function", "feedback"):
        savepoint = f"SAVEPOINT memplex_probe_{name}_rls"
        rollback = f"ROLLBACK TO SAVEPOINT memplex_probe_{name}_rls"
        release = f"RELEASE SAVEPOINT memplex_probe_{name}_rls"
        assert statements.index(savepoint) < statements.index(rollback) < statements.index(release)
    assert sum("set_config('memplex.tenant_id'" in item for item in statements) >= 5


def test_resources_rejects_public_runner_keyword_without_initializing_or_pooling():
    """Readiness never accepts caller-owned executable migration capability."""
    from memplex.storage.pool import PostgresStorageResources

    raw_pool = _PoolMock()
    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: raw_pool
    )

    with pytest.raises(TypeError, match="runner"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
            runner=_test_runner(),
        )

    assert resources.state == "NEW"
    assert resources.pool_created is False
    assert raw_pool.closeall_calls == 0


def test_resources_verify_application_access_runs_in_development(monkeypatch):
    """Development readiness still proves role access against required business tables."""
    import memplex.storage.pool as _pool_module
    from memplex.storage.pool import PostgresStorageResources

    calls: list[tuple[PostgresTargetIdentity, str, int]] = []
    original_verify = _pool_module.PostgresPoolManager.verify_application_access

    def _capture(self, *args, **kwargs):  # pylint: disable=unused-argument
        calls.append((kwargs["target"], kwargs["profile"], kwargs["vector_dim"]))
        return original_verify(self, *args, **kwargs)

    monkeypatch.setattr(_pool_module.PostgresPoolManager, "verify_application_access", _capture)

    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: _PoolMock()
    )
    status = resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    assert status.state == "disabled"
    assert calls == [(_test_target(), "development", 0)]


@pytest.mark.parametrize("dsn", ("", None, 42))
def test_resources_require_nonempty_exact_application_and_migration_dsns(dsn):
    """Readiness owns DSN data rather than accepting loose constructor input."""
    from memplex.storage.pool import PostgresStorageResources

    with pytest.raises(TypeError, match="DSN"):
        PostgresStorageResources(dsn=dsn)
    if dsn is not None:
        with pytest.raises(TypeError, match="DSN"):
            PostgresStorageResources(dsn="dbname=fake", migration_dsn=dsn)


def test_resources_rejects_noop_mutation_when_independent_readback_is_not_ready(monkeypatch):
    """A provisional disabled apply cannot publish without catalogue readback."""
    from memplex.storage.migrations import MigrationIntegrityError
    from memplex.storage.pool import PostgresStorageResources

    mutation_calls: list[str] = []
    raw_pool = _PoolMock()
    _install_resource_runner_sequence(
        monkeypatch,
        apply=lambda *, expected_target=None: mutation_calls.append("apply"),
        verify=lambda _request, _profile, *, expected_target=None: (
            _ for _ in ()
        ).throw(MigrationIntegrityError("PostgreSQL storage catalogue is not ready")),
    )
    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: raw_pool
    )

    with pytest.raises(MigrationIntegrityError, match="catalogue is not ready"):
        resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")

    assert mutation_calls == ["apply"]
    assert resources.state == "FAULTED"
    assert resources.pool_created is False
    # Catalogue readback now precedes candidate-pool construction: an
    # unverified schema must not create a pool side effect.
    assert raw_pool.closeall_calls == 0


def test_resources_uses_readback_not_provisional_vector_status(monkeypatch):
    """Only the fresh verifier, not a mutator's claimed ready status, determines the seal."""
    from memplex.storage.pool import PostgresStorageResources

    raw_pool = _PoolMock()
    _install_resource_runner_sequence(
        monkeypatch,
        ensure=lambda _request, _profile, *, expected_target=None: VectorCapabilityStatus(
            state="ready",
            dim=8,
            parameter_digest=sha256(b"pgvector:8").hexdigest(),
        ),
        verify=lambda request, _profile, *, expected_target=None: VectorCapabilityStatus(
            state="degraded", dim=request.dim
        ),
    )
    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: raw_pool
    )

    status = resources.ensure_ready(
        VectorCapabilityRequest(dim=8, policy="best_effort"), "development"
    )

    assert status == VectorCapabilityStatus(state="degraded", dim=8)
    assert resources.ready_pool.effective_dim == 0


def test_pooled_read_cursor_returns_once_on_fetch_error():
    """A cursor fetch failure must roll back and return precisely one lease."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()
    cursor = PostgresPoolManager("dbname=fake", pool=mock_pool).read_cursor(
        lambda _cursor, _context: None,
        _authorization(),
    )
    mock_pool.connection.cursor_instance.fetchall = lambda: (_ for _ in ()).throw(RuntimeError("fetch"))
    with pytest.raises(RuntimeError, match="fetch"):
        cursor.fetchall()
    cursor.close()
    assert mock_pool.connection.rollbacks == 1
    assert mock_pool.put_calls == [mock_pool.connection]
    assert mock_pool.connection.cursor_instance.closed == 1


def test_pooled_read_cursor_cleans_up_in_transaction_order():
    """Read leases roll back before closing the cursor and returning the connection."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()
    events: list[str] = []
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())

    mock_pool.connection.rollback = lambda: events.append("rollback")
    mock_pool.connection.cursor_instance.close = lambda: events.append("cursor.close")
    mock_pool.putconn = lambda _connection: events.append("putconn")
    cursor.close()

    assert events == ["rollback", "cursor.close", "putconn"]


def test_postgres_store_uses_injected_pool_for_reads():
    """Memory reads borrow an already-ready shared pool rather than a private connection."""
    mock_pool = _PoolMock()
    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(pool=mock_pool),
    )
    mock_pool.put_calls.clear()
    mock_pool.connection.rollbacks = 0
    store.get("missing")
    assert mock_pool.put_calls == [mock_pool.connection]
    assert mock_pool.connection.rollbacks == 1


def test_read_execute_preserves_primary_error_when_cursor_cleanup_fails(pg_store):
    """Read execution failure must survive a failing cursor cleanup."""
    store, conn = pg_store

    calls = 0

    def execute_fails(_sql, _params=()):
        nonlocal calls
        if "pg_catalog.current_database()" in _sql:
            conn._cursor.executed.append((_sql, _params))
            return
        if "role.rolsuper" in _sql:
            conn._cursor.executed.append((_sql, _params))
            conn._cursor._fetchone_val = ("fake", "fake", False, False)
            return
        calls += 1
        if calls > 1:  # The first call binds AuthorizationContext.
            raise ValueError("application read failed")

    close_calls = 0

    def close_fails():
        nonlocal close_calls
        close_calls += 1
        if close_calls > 1:  # The target gate closes its own probe cursor first.
            raise RuntimeError("cursor cleanup failed")

    conn._cursor.execute = execute_fails
    conn._cursor.close = close_fails
    with pytest.raises(ValueError, match="application read failed"):
        store._execute("SELECT broken", commit=False)


def test_transaction_cleanup_failure_is_reported_after_successful_work():
    """A successful body is not reported as clean when cursor cleanup fails."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)

    def close_fails():
        raise RuntimeError("cursor close failed")

    mock_pool.connection.cursor_instance.close = close_fails
    with pytest.raises(RuntimeError, match="cursor close failed"):
        with manager.transaction(lambda _cursor, _context: None, _authorization()):
            pass

    assert mock_pool.put_calls == [mock_pool.connection]
    with pytest.raises(RuntimeError, match="closed"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())


def test_read_bind_failure_preserves_primary_error_when_cleanup_also_fails():
    """Rollback/return failures must never replace the scope-bind failure."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)

    def bind_fails(_cursor, _context):
        raise ValueError("bind failed")

    def rollback_fails():
        raise RuntimeError("rollback failed")

    def put_fails(_connection):
        raise RuntimeError("put failed")

    mock_pool.connection.rollback = rollback_fails
    mock_pool.putconn = put_fails
    with pytest.raises(ValueError, match="bind failed"):
        manager.read_cursor(bind_fails, _authorization())

    with pytest.raises(RuntimeError, match="closed"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())


def test_read_bind_failure_rolls_back_closes_cursor_and_returns_once():
    """Scope-bind errors must release every acquired read resource in order."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()
    events: list[str] = []
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)
    mock_pool.connection.rollback = lambda: events.append("rollback")
    mock_pool.connection.cursor_instance.close = lambda: events.append("cursor.close")
    mock_pool.putconn = lambda _connection: events.append("putconn")

    with pytest.raises(ValueError, match="bind failed"):
        manager.read_cursor(
            lambda _cursor, _context: (_ for _ in ()).throw(ValueError("bind failed")),
            _authorization(),
        )

    assert events == ["rollback", "cursor.close", "putconn"]


def test_closeall_failure_is_terminal_and_not_reported_as_clean_on_retry():
    """A failed physical close remains observable to every later close caller."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()

    def closeall_fails():
        mock_pool.closeall_calls += 1
        raise RuntimeError("closeall failed")

    mock_pool.closeall = closeall_fails
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)
    with pytest.raises(RuntimeError, match="closeall failed"):
        manager.close()
    with pytest.raises(RuntimeError, match="closeall failed"):
        manager.close()
    assert mock_pool.closeall_calls == 1


def test_close_wait_false_blocks_new_leases_then_finalizes_on_last_return():
    """Non-waiting shutdown must close physically exactly once after return."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())

    assert manager.close(wait=False) is False
    assert mock_pool.closeall_calls == 0
    with pytest.raises(RuntimeError, match="closed"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())

    assert cursor.fetchone() is None
    assert mock_pool.closeall_calls == 1
    assert manager.close() is True
    assert mock_pool.closeall_calls == 1


def test_concurrent_close_waiters_share_one_physical_close():
    """Waiting shutdown callers coordinate rather than issuing two closeall calls."""
    from memplex.storage.pool import PostgresPoolManager

    mock_pool = _PoolMock()
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    results: list[bool] = []
    started = threading.Barrier(3)

    def close_pool():
        started.wait(timeout=1)
        results.append(manager.close(wait=True))

    first = threading.Thread(target=close_pool)
    second = threading.Thread(target=close_pool)
    first.start()
    second.start()
    started.wait(timeout=1)
    cursor.close()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(results) == [True, True]
    assert mock_pool.closeall_calls == 1


def test_close_waits_for_an_inflight_borrower_before_closeall():
    """A blocked getconn borrower counts as live until it returns the connection."""
    from memplex.storage.pool import PostgresPoolManager

    class _BlockingPool(_PoolMock):
        def __init__(self):
            super().__init__()
            self.borrow_started = threading.Event()
            self.allow_borrow = threading.Event()

        def getconn(self):
            self.borrow_started.set()
            assert self.allow_borrow.wait(timeout=1)
            return self.connection

    mock_pool = _BlockingPool()
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)
    borrower_error: list[BaseException] = []
    close_results: list[bool] = []
    close_started = threading.Event()

    def borrow():
        try:
            manager.read_cursor(lambda _cursor, _context: None, _authorization())
        except BaseException as exc:
            borrower_error.append(exc)

    borrower = threading.Thread(target=borrow)
    borrower.start()
    assert mock_pool.borrow_started.wait(timeout=1)
    def close_pool():
        close_started.set()
        close_results.append(manager.close())

    closer = threading.Thread(target=close_pool)
    closer.start()
    assert close_started.wait(timeout=1)
    assert closer.is_alive()

    mock_pool.allow_borrow.set()
    borrower.join(timeout=1)
    closer.join(timeout=1)

    assert not borrower.is_alive() and not closer.is_alive()
    assert len(borrower_error) == 1
    assert isinstance(borrower_error[0], RuntimeError)
    assert close_results == [True]
    assert mock_pool.put_calls == [mock_pool.connection]
    assert mock_pool.closeall_calls == 1


def test_close_waits_for_blocked_putconn_before_physical_close():
    """A returning lease remains live until ``putconn`` has completed."""
    from memplex.storage.pool import PostgresPoolManager

    class _BlockingReturnPool(_PoolMock):
        def __init__(self):
            super().__init__()
            self.put_started = threading.Event()
            self.allow_put = threading.Event()

        def putconn(self, connection):
            self.put_started.set()
            assert self.allow_put.wait(timeout=1)
            super().putconn(connection)

    mock_pool = _BlockingReturnPool()
    manager = PostgresPoolManager("dbname=fake", pool=mock_pool)
    cursor = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    returner = threading.Thread(target=cursor.close)
    returner.start()
    assert mock_pool.put_started.wait(timeout=1)

    close_results: list[bool] = []
    close_started = threading.Event()

    def close_pool():
        close_started.set()
        close_results.append(manager.close())

    closer = threading.Thread(target=close_pool)
    closer.start()
    assert close_started.wait(timeout=1)
    assert closer.is_alive()
    assert mock_pool.closeall_calls == 0

    mock_pool.allow_put.set()
    returner.join(timeout=1)
    closer.join(timeout=1)

    assert not returner.is_alive() and not closer.is_alive()
    assert close_results == [True]
    assert mock_pool.put_calls == [mock_pool.connection]
    assert mock_pool.closeall_calls == 1


def test_resources_close_orphaned_raw_pool_if_manager_construction_fails(monkeypatch):
    """Factory success must not leak a raw pool if manager publication fails."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    raw_pool = _PoolMock()

    class _BrokenManager:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("manager construction failed")

    monkeypatch.setattr("memplex.storage.pool.PostgresPoolManager", _BrokenManager)
    resources = PostgresStorageResources(
        dsn="dbname=fake",
        pool_factory=lambda *_args: raw_pool,
    )
    with pytest.raises(RuntimeError, match="manager construction failed"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
        )
    assert raw_pool.closeall_calls == 1
    assert resources.pool_created is False


def test_resources_factory_failure_is_faulted_and_rejects_later_ensure():
    """A pool factory failure is terminal and retains its initialization identity."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    attempts = 0
    def factory(*_args):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("factory transient failure")

    resources = PostgresStorageResources(dsn="dbname=fake", pool_factory=factory)
    request = VectorCapabilityRequest(dim=0, policy="disabled")
    with pytest.raises(RuntimeError, match="factory transient failure"):
        resources.ensure_ready(request, "development")
    assert attempts == 1
    assert resources.state == "FAULTED"
    with pytest.raises(RuntimeError, match="faulted"):
        resources.ensure_ready(request, "development")


def test_ready_postgres_pool_seal_is_immutable_after_issuance():
    """No caller may replace the validated manager, status, or vector dimension."""
    ready_pool = _test_ready_pool(dim=4)
    with pytest.raises(AttributeError):
        ready_pool.manager = _test_pool_manager()
    with pytest.raises(AttributeError):
        ready_pool.status = None
    with pytest.raises(AttributeError):
        ready_pool.effective_dim = 0


def _assert_all_business_entrypoints_reject(seal):
    """All public store/factory constructors share the same seal gate."""
    from memplex.storage import create_store
    from memplex.storage.feedback import PostgresFeedbackStore, create_feedback_store

    constructors = (
        lambda: PostgresMemoryStore(dsn="dbname=fake", ready_pool=seal),
        lambda: PostgresFeedbackStore(dsn="dbname=fake", ready_pool=seal),
        lambda: create_store(
            backend="postgres", path="dbname=fake", ready_pool=seal
        ),
        lambda: create_feedback_store(
            backend="postgres", dsn="dbname=fake", ready_pool=seal
        ),
    )
    for construct in constructors:
        with pytest.raises(TypeError, match="resource-issued ReadyPostgresPool"):
            construct()


def test_all_business_entrypoints_reject_forged_unpublished_and_tampered_seals():
    """``isinstance`` is not authority: each construction boundary must verify it."""
    from memplex.storage import pool as pool_module
    from memplex.storage.pool import ReadyPostgresPool

    class _SealSubclass(ReadyPostgresPool):
        pass

    subclass_forge = object.__new__(_SealSubclass)
    exact_forge = object.__new__(ReadyPostgresPool)
    request = VectorCapabilityRequest(dim=0, policy="disabled")
    status = VectorCapabilityStatus(state="disabled", dim=0)
    private_issuer_forge = ReadyPostgresPool(
        manager=_test_pool_manager(),
        request=request,
        status=status,
        effective_dim=0,
        target=_test_target(),
        issuer=pool_module._READY_POOL_ISSUER,
    )
    tampered_published = _test_ready_pool()
    object.__setattr__(tampered_published, "effective_dim", 99)

    for forged in (
        subclass_forge,
        exact_forge,
        private_issuer_forge,
        tampered_published,
    ):
        _assert_all_business_entrypoints_reject(forged)


def test_all_business_entrypoints_reject_a_revoked_ready_seal():
    """Direct manager close/fault revokes the authority for later construction."""
    ready_pool = _test_ready_pool()
    ready_pool.manager.close()
    _assert_all_business_entrypoints_reject(ready_pool)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("manager", lambda: _test_pool_manager()),
        (
            "request",
            lambda: VectorCapabilityRequest(dim=0, policy="disabled"),
        ),
        (
            "status",
            lambda: VectorCapabilityStatus(state="disabled", dim=0),
        ),
        ("effective_dim", lambda: 99),
        (
            "target",
            lambda: PostgresTargetIdentity(
                database="other", schema="public", server_address=None, server_port=None
            ),
        ),
    ),
)
def test_all_business_entrypoints_reject_every_tampered_authority_field(
    field, replacement
):
    """The registry checks each snapshotted field, not only the seal object type."""
    ready_pool = _test_ready_pool()
    object.__setattr__(ready_pool, field, replacement())
    _assert_all_business_entrypoints_reject(ready_pool)


def test_published_seal_authorizes_all_four_normal_public_entrypoints():
    """The authority registry rejects forgeries without narrowing normal construction."""
    from memplex.storage import create_store
    from memplex.storage.feedback import PostgresFeedbackStore, create_feedback_store

    ready_pool = _test_ready_pool()
    assert isinstance(PostgresMemoryStore(dsn="dbname=fake", ready_pool=ready_pool), PostgresMemoryStore)
    assert isinstance(PostgresFeedbackStore(dsn="dbname=fake", ready_pool=ready_pool), PostgresFeedbackStore)
    assert create_store(backend="postgres", path="dbname=fake", ready_pool=ready_pool)
    assert create_feedback_store(
        backend="postgres", dsn="dbname=fake", ready_pool=ready_pool
    )
    ready_pool.manager.close()


def test_direct_manager_fault_revokes_the_published_seal_and_closes_pool():
    """Fault callbacks revoke authority and make physical close self-completing."""
    raw_pool = _PoolMock()
    ready_pool = _test_ready_pool(pool=raw_pool)

    ready_pool.manager._mark_fault(RuntimeError("forced pool fault"))

    _assert_all_business_entrypoints_reject(ready_pool)
    assert raw_pool.closeall_calls == 1


def test_resources_reject_duck_runner_keyword_before_state_or_catalogue_mutation():
    """No caller-owned runner, even a duck, reaches resource readiness."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    calls: list[str] = []

    class _DuckRunner:
        def inspect_target(self):
            calls.append("inspect")
            return _test_target()

        def apply(self, *, expected_target=None):
            calls.append("apply")

        def ensure_vector_capability(self, _request, _profile, *, expected_target=None):
            calls.append("capability")
            raise AssertionError("duck runner must not reach capability")

    factory_calls = 0

    def pool_factory(*_args):
        nonlocal factory_calls
        factory_calls += 1
        return _PoolMock()

    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=pool_factory
    )
    with pytest.raises(TypeError, match="runner"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
            runner=_DuckRunner(),
        )
    assert calls == []
    assert factory_calls == 0
    assert resources.state == "NEW"


def test_each_business_lease_rechecks_target_before_scope_or_application_sql():
    """A pool connection that switches A->B after readiness cannot execute store SQL."""
    from memplex.storage.migrations import MigrationIntegrityError

    raw_pool = _PoolMock()
    ready_pool = _test_ready_pool(pool=raw_pool)
    store = PostgresMemoryStore(dsn="dbname=fake", ready_pool=ready_pool)
    raw_pool.connection.cursor_instance.executed.clear()
    raw_pool.put_calls.clear()
    raw_pool.connection.cursor_instance._target_row = ("other", "public", None, None)

    with pytest.raises(MigrationIntegrityError, match="pool target"):
        store.get("must-not-query")

    statements = [sql for sql, _params in raw_pool.connection.cursor_instance.executed]
    assert statements
    assert all("pg_catalog.current_database()" in statement for statement in statements)
    assert raw_pool.put_calls == [raw_pool.connection]
    with pytest.raises(RuntimeError, match="closed"):
        store.get("still-faulted")


def test_target_bound_verify_rolls_back_probe_before_returning_connection():
    """A successful publication probe cannot return an idle transaction to the pool."""
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    manager = PostgresPoolManager(
        "dbname=fake", pool=raw_pool, expected_target=_test_target()
    )
    manager.verify_target(_test_target())

    assert raw_pool.connection.rollbacks == 1
    assert raw_pool.connection.cursor_instance.closed == 1
    assert raw_pool.put_calls == [raw_pool.connection]


def test_target_fault_eventually_closes_raw_pool_after_unleased_cleanup():
    """A per-lease A->B target failure faults, returns and physically closes one pool."""
    from memplex.storage.migrations import MigrationIntegrityError

    raw_pool = _PoolMock()
    ready_pool = _test_ready_pool(pool=raw_pool)
    raw_pool.put_calls.clear()
    raw_pool.connection.cursor_instance._target_row = ("other", "public", None, None)
    store = PostgresMemoryStore(dsn="dbname=fake", ready_pool=ready_pool)

    with pytest.raises(MigrationIntegrityError, match="pool target"):
        store.get("must-close")

    assert raw_pool.put_calls == [raw_pool.connection]
    assert raw_pool.closeall_calls == 1
    with pytest.raises(RuntimeError, match="faulted"):
        ready_pool.manager.close()


@pytest.mark.parametrize("second_lease", ("read", "transaction"))
def test_each_target_bound_lease_rechecks_a_then_b_before_bind_or_sql(second_lease):
    """Both read and transaction entries reject a later pool target B."""
    from memplex.storage.migrations import MigrationIntegrityError

    raw_pool = _PoolMock()
    ready_pool = _test_ready_pool(pool=raw_pool)
    manager = ready_pool.manager

    first = manager.read_cursor(lambda _cursor, _context: None, _authorization())
    first.close()
    raw_pool.connection.cursor_instance.executed.clear()
    raw_pool.put_calls.clear()
    raw_pool.connection.cursor_instance._target_row = ("other", "public", None, None)

    with pytest.raises(MigrationIntegrityError, match="pool target"):
        if second_lease == "read":
            manager.read_cursor(lambda _cursor, _context: None, _authorization())
        else:
            with manager.transaction(lambda _cursor, _context: None, _authorization()):
                raise AssertionError("target gate must fail before transaction body")

    statements = [sql for sql, _params in raw_pool.connection.cursor_instance.executed]
    assert len(statements) == 1
    assert "pg_catalog.current_database()" in statements[0]
    assert raw_pool.put_calls == [raw_pool.connection]
    assert raw_pool.closeall_calls == 1


def test_target_probe_cleanup_preserves_identity_failure_and_returns_connection():
    """Probe rollback/cursor cleanup failures cannot replace the target mismatch."""
    from memplex.storage.migrations import MigrationIntegrityError
    from memplex.storage.pool import PostgresPoolManager

    raw_pool = _PoolMock()
    raw_pool.connection.cursor_instance._target_row = ("other", "public", None, None)
    events: list[str] = []

    def rollback_fails():
        events.append("rollback")
        raise RuntimeError("rollback cleanup failed")

    def close_fails():
        events.append("cursor.close")
        raise RuntimeError("cursor cleanup failed")

    def put_connection(connection):
        events.append("putconn")
        raw_pool.put_calls.append(connection)

    raw_pool.connection.rollback = rollback_fails
    raw_pool.connection.cursor_instance.close = close_fails
    raw_pool.putconn = put_connection
    manager = PostgresPoolManager(
        "dbname=fake", pool=raw_pool, expected_target=_test_target()
    )

    with pytest.raises(MigrationIntegrityError, match="pool target"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())

    assert events == ["cursor.close", "rollback", "putconn"]
    assert raw_pool.put_calls == [raw_pool.connection]
    with pytest.raises(RuntimeError, match="closed"):
        manager.read_cursor(lambda _cursor, _context: None, _authorization())


def test_close_waits_for_target_probe_before_publish_or_physical_close():
    """An Event-held target probe remains an in-flight borrower during close."""
    from memplex.storage.pool import PostgresPoolManager

    class _ProbeBlockingPool(_PoolMock):
        def __init__(self):
            super().__init__()
            self.probe_started = threading.Event()
            self.release_probe = threading.Event()
            self.block_probe = False
            original_execute = self.connection.cursor_instance.execute

            def execute(sql, params=()):
                if self.block_probe and "pg_catalog.current_database()" in sql:
                    self.probe_started.set()
                    assert self.release_probe.wait(timeout=1)
                return original_execute(sql, params)

            self.connection.cursor_instance.execute = execute

    raw_pool = _ProbeBlockingPool()
    manager = PostgresPoolManager(
        "dbname=fake", pool=raw_pool, expected_target=_test_target()
    )
    raw_pool.block_probe = True
    borrower_errors: list[BaseException] = []
    close_results: list[bool] = []

    def borrow():
        try:
            manager.read_cursor(lambda _cursor, _context: None, _authorization())
        except BaseException as exc:
            borrower_errors.append(exc)

    borrower = threading.Thread(target=borrow)
    borrower.start()
    assert raw_pool.probe_started.wait(timeout=1)

    closer = threading.Thread(target=lambda: close_results.append(manager.close()))
    closer.start()
    assert closer.is_alive()
    assert raw_pool.closeall_calls == 0

    raw_pool.release_probe.set()
    borrower.join(timeout=1)
    closer.join(timeout=1)

    assert not borrower.is_alive() and not closer.is_alive()
    assert len(borrower_errors) == 1
    assert isinstance(borrower_errors[0], RuntimeError)
    assert close_results == [True]
    assert raw_pool.put_calls == [raw_pool.connection]
    assert raw_pool.closeall_calls == 1


def test_resources_reject_structural_vector_status_before_pool_creation(monkeypatch):
    """A duck-typed status cannot forge the runner's capability result."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    class _ForgedStatus:
        state = "disabled"
        dim = 0
        parameter_digest = None

    factory_calls = 0

    def pool_factory(*_args):
        nonlocal factory_calls
        factory_calls += 1
        return _PoolMock()

    resources = PostgresStorageResources(dsn="dbname=fake", pool_factory=pool_factory)
    _install_resource_runner_sequence(
        monkeypatch,
        verify=lambda _request, _profile, *, expected_target=None: _ForgedStatus(),
    )
    with pytest.raises(ValueError, match="VectorCapabilityStatus"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
        )
    assert factory_calls == 0
    assert resources.state == "FAULTED"


def test_resources_staged_cleanup_failure_is_faulted_not_cleanly_closed(monkeypatch):
    """A staged pool close error is terminal even when shutdown won the race."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    manager_entered = threading.Event()
    allow_manager = threading.Event()

    class _BlockingManager:
        def __init__(self, *_args, **_kwargs):
            manager_entered.set()
            assert allow_manager.wait(timeout=1)

        def close(self, *, wait=True):
            raise RuntimeError("staged close failed")

        def verify_target(self, _target):
            return None

    monkeypatch.setattr("memplex.storage.pool.PostgresPoolManager", _BlockingManager)
    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: _PoolMock()
    )
    errors: list[BaseException] = []

    def ensure():
        try:
                resources.ensure_ready(
                    VectorCapabilityRequest(dim=0, policy="disabled"),
                    "development",
                )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=ensure)
    worker.start()
    assert manager_entered.wait(timeout=1)
    assert resources.close(wait=False) is False
    allow_manager.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert resources.state == "FAULTED"
    with pytest.raises(RuntimeError, match="faulted"):
        resources.close(wait=True)


def test_resources_wait_false_converges_to_closed_after_last_pool_lease_returns():
    """The manager's autonomous final close advances resources out of CLOSING."""
    from memplex.storage.pool import PostgresStorageResources

    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: _PoolMock()
    )
    from memplex.storage.migrations.runner import VectorCapabilityRequest

    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    cursor = resources.pool_manager.read_cursor(
        lambda _cursor, _context: None, _authorization()
    )
    assert resources.close(wait=False) is False
    cursor.close()
    with resources._condition:
        assert resources._condition.wait_for(
            lambda: resources._state.value == "CLOSED", timeout=1
        )
    assert resources.state == "CLOSED"


def test_resources_reject_inconsistent_vector_status_before_pool_creation(monkeypatch):
    """A runner status/digest mismatch is faulted before business pool creation."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    factory_calls = 0

    def pool_factory(*_args):
        nonlocal factory_calls
        factory_calls += 1
        return _PoolMock()

    resources = PostgresStorageResources(dsn="dbname=fake", pool_factory=pool_factory)
    _install_resource_runner_sequence(
        monkeypatch,
        verify=lambda _request, _profile, *, expected_target=None: VectorCapabilityStatus(
            state="ready", dim=8, parameter_digest="wrong-digest"
        ),
    )
    with pytest.raises(ValueError, match="digest"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=8, policy="best_effort"),
            "development",
        )

    assert factory_calls == 0
    assert resources.state == "FAULTED"


def test_resources_rejects_migration_target_mismatch_before_apply_or_pool(monkeypatch):
    """Admin migration target B cannot authorize application target A."""
    from memplex.storage.migrations import MigrationIntegrityError, PostgresTargetIdentity
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    target_b = PostgresTargetIdentity(
        database="other", schema="public", server_address=None, server_port=None
    )
    calls: list[str] = []

    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: _PoolMock()
    )
    _install_resource_runner_sequence(
        monkeypatch,
        migration_target=target_b,
        apply=lambda *, expected_target=None: calls.append("apply"),
        ensure=lambda _request, _profile, *, expected_target=None: (
            calls.append("capability"),
            VectorCapabilityStatus(state="disabled", dim=0),
        )[1],
    )
    with pytest.raises(MigrationIntegrityError, match="migration target"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
        )
    assert calls == []
    assert resources.pool_created is False
    assert resources.business_lease_count == 0
    assert resources.state == "FAULTED"


def test_resources_rejects_pool_target_mismatch_and_does_not_publish_seal(monkeypatch):
    """A business pool resolving to B is closed before any seal or lease escapes."""
    from memplex.storage.migrations import MigrationIntegrityError
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    raw_pool = _PoolMock()
    raw_pool.connection.cursor_instance._target_row = (
        "other",
        "public",
        None,
        None,
    )
    target_calls: list[object] = []

    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: raw_pool
    )
    _install_resource_runner_sequence(
        monkeypatch,
        apply=lambda *, expected_target=None: target_calls.append(expected_target),
    )
    with pytest.raises(MigrationIntegrityError, match="pool target"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
        )
    assert target_calls == [_test_target()]
    assert raw_pool.closeall_calls == 1
    assert resources.pool_created is False
    assert resources.business_lease_count == 0
    assert resources.state == "FAULTED"
    with pytest.raises(RuntimeError, match="not ready"):
        resources.ready_pool


def test_resources_rejects_non_exact_target_identity_before_initialization():
    """A target subclass or structural lookalike cannot influence the gate."""
    from memplex.storage.migrations import PostgresTargetIdentity
    from memplex.storage.pool import PostgresStorageResources

    class _TargetSubclass(PostgresTargetIdentity):
        pass

    with pytest.raises(TypeError, match="exact PostgresTargetIdentity"):
        PostgresStorageResources(
            dsn="dbname=fake",
            expected_target=_TargetSubclass(
                database="fake", schema="public", server_address=None, server_port=None
            ),
        )


def test_resources_fault_when_its_manager_is_closed_outside_the_owner():
    """A published seal cannot remain READY over a directly closed manager."""
    from memplex.storage.pool import PostgresStorageResources

    resources = PostgresStorageResources(
        dsn="dbname=fake", pool_factory=lambda *_args: _PoolMock()
    )

    from memplex.storage.migrations.runner import VectorCapabilityRequest

    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    resources.pool_manager.close()
    assert resources.state == "FAULTED"
    with pytest.raises(RuntimeError, match="not ready"):
        resources.ready_pool


def test_resources_close_during_initializing_cancels_and_closes_staged_pool():
    """Closing during readiness prevents publication and closes the staged raw pool."""
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources

    factory_entered = threading.Event()
    allow_factory = threading.Event()
    raw_pool = _PoolMock()

    def pool_factory(*_args):
        factory_entered.set()
        assert allow_factory.wait(timeout=1)
        return raw_pool

    resources = PostgresStorageResources(dsn="dbname=fake", pool_factory=pool_factory)
    ensure_error: list[BaseException] = []

    def ensure():
        try:
                resources.ensure_ready(
                    VectorCapabilityRequest(dim=8, policy="best_effort"),
                    "development",
                )
        except BaseException as exc:
            ensure_error.append(exc)

    worker = threading.Thread(target=ensure)
    worker.start()
    assert factory_entered.wait(timeout=1)
    assert resources.close(wait=False) is False
    assert resources.state == "CLOSING"
    allow_factory.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert len(ensure_error) == 1
    assert isinstance(ensure_error[0], RuntimeError)
    assert raw_pool.closeall_calls == 1
    assert resources.state == "CLOSED"
    with pytest.raises(RuntimeError, match="closed"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=8, policy="best_effort"),
            "development",
        )


def test_business_store_rejects_raw_pool_manager_without_ready_seal():
    """Only a resource-issued ReadyPostgresPool may construct a business store."""
    with pytest.raises(TypeError, match="ReadyPostgresPool"):
        PostgresMemoryStore(dsn="dbname=fake", pool_manager=_test_pool_manager())


@pytest.mark.parametrize("func_ids", (None, ["graph-a"]))
def test_get_graph_preserves_full_and_bidirectional_node_contract(pg_store, monkeypatch, func_ids):
    """Reintroducing list caps or source-only filtering must break graph parity."""
    store, _conn = pg_store
    graph_a = _sample_func("graph-a", "graph a")
    graph_b = _sample_func("graph-b", "graph b")
    executed: list[tuple[str, tuple]] = []

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

        def close(self):
            return None

    def _execute(sql, params=(), *, commit=False):
        assert commit is False
        executed.append((sql, params))
        if "FROM memplex_edges" in sql:
            return _Cursor(
                [
                    ("graph-b", "graph-a", "REFERENCES", 1.0, [], None),
                    ("graph-a", "graph-b", "REFERENCES", 1.0, [], None),
                ]
            )
        assert "FROM memplex_functions" in sql
        rows = [(_func_to_json(graph_a),)]
        if func_ids is None:
            rows.append((_func_to_json(graph_b),))
        return _Cursor(rows)

    monkeypatch.setattr(store, "_execute", _execute)
    monkeypatch.setattr(
        store,
        "list_functions",
        lambda *args, **kwargs: pytest.fail("get_graph must not use a capped listing"),
    )

    graph = store.get_graph(func_ids=func_ids)

    expected_nodes = {"graph-a", "graph-b"} if func_ids is None else {"graph-a"}
    assert {node.id for node in graph.nodes} == expected_nodes
    assert {(edge.source, edge.target) for edge in graph.edges} == {
        ("graph-b", "graph-a"),
        ("graph-a", "graph-b"),
    }
    if func_ids is not None:
        edge_sql, edge_params = executed[0]
        assert "source = ANY(%s) OR target = ANY(%s)" in edge_sql
        assert edge_params == (func_ids, func_ids)


def test_feedback_business_store_rejects_raw_pool_manager_without_ready_seal():
    """Feedback shares the same sealed ownership boundary as memory."""
    from memplex.storage.feedback import PostgresFeedbackStore, create_feedback_store

    with pytest.raises(TypeError, match="ReadyPostgresPool"):
        PostgresFeedbackStore(dsn="dbname=fake", pool_manager=_test_pool_manager())
    with pytest.raises(TypeError, match="ReadyPostgresPool"):
        create_feedback_store(
            backend="postgres", dsn="dbname=fake", pool_manager=_test_pool_manager()
        )


@pytest.fixture
def pg_store(monkeypatch):
    """A PostgresMemoryStore with a mock connection (no real DB)."""
    mock_conn = _MockConn()

    pool = _PoolMock()
    pool.connection = mock_conn
    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(pool=pool),
    )
    mock_conn._cursor.executed.clear()
    return store, mock_conn


# ── Write operations (SQL + params verified) ─────────────────────────


def test_add_executes_upsert_sql(pg_store):
    store, conn = pg_store
    store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    sqls = [s for s, _ in conn._cursor.executed]
    upserts = [s for s in sqls if "INSERT INTO memplex_functions" in s]
    assert len(upserts) == 1
    assert "ON CONFLICT" in upserts[0]
    # Params of the upsert carry the func id first.
    upsert_params = next(
        p for s, p in conn._cursor.executed if "INSERT INTO memplex_functions" in s
    )
    assert upsert_params[0] == "pg-1"  # func id


def test_function_write_lock_key_has_stable_golden_vectors():
    from memplex.storage.migrations import PostgresTargetIdentity

    public = _test_target()
    other_schema = PostgresTargetIdentity(
        database="fake", schema="other", server_address=None, server_port=None
    )
    assert _function_write_lock_key(public, "tenant-a") == 7510571994098344188
    assert _function_write_lock_key(public, "tenant-a") == _function_write_lock_key(
        public, "tenant-a"
    )
    assert _function_write_lock_key(other_schema, "tenant-a") == 8293794513030357488
    assert _function_write_lock_key(public, "tenant-a") != _function_write_lock_key(
        public, "tenant-b"
    )


def test_add_acquires_tenant_lock_before_first_function_business_sql(pg_store):
    store, conn = pg_store
    store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    sqls = [sql for sql, _params in conn._cursor.executed]
    scope_idx = next(i for i, sql in enumerate(sqls) if "set_config('memplex.tenant_id'" in sql)
    timeout_idx = next(i for i, sql in enumerate(sqls) if "SET LOCAL lock_timeout" in sql)
    lock_idx = next(i for i, sql in enumerate(sqls) if "pg_advisory_xact_lock" in sql)
    function_idx = next(i for i, sql in enumerate(sqls) if "memplex_functions" in sql)
    assert scope_idx < timeout_idx < lock_idx < function_idx


def test_advisory_lock_timeout_rolls_back_and_maps_to_retryable_busy(pg_store, monkeypatch):
    store, conn = pg_store

    class LockUnavailable(Exception):
        pgcode = "55P03"

    monkeypatch.setattr(
        store,
        "_acquire_function_write_lock",
        lambda _cur, _context: (_ for _ in ()).throw(LockUnavailable()),
    )
    before_rollbacks = conn.rollbacks
    with pytest.raises(FunctionWriteBusy, match="retry"):
        store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    assert conn.rollbacks == before_rollbacks + 1


def test_all_function_and_edge_writer_callsites_use_the_tenant_lock(pg_store, monkeypatch):
    """A future public Function/edge writer must not bypass the lock entrypoint."""
    from memplex.models import GraphData

    store, _conn = pg_store
    operations = []
    original = store._acquire_function_write_lock

    def record_lock(cur, context):
        operations.append(context.principal.tenant_id)
        return original(cur, context)

    monkeypatch.setattr(store, "_acquire_function_write_lock", record_lock)
    store.add(_sample_func("lock-add", "Lock Add"), SourceDocument(type="text", source_type=SourceType.WIKI))
    store.merge(GraphData(nodes=[], edges=[]))
    store.delete("lock-missing")
    store.clear()
    store.increment_access("lock-missing")
    store.increment_access_batch(["lock-missing"])
    result = store.add_batch(
        [_sample_func("lock-batch", "Lock Batch")],
        [SourceDocument(type="text", source_type=SourceType.WIKI)],
    )
    assert result.succeeded == 1
    # add_batch intentionally remains per-item atomic, so its one item takes
    # a separate public add lock rather than joining a batch-wide transaction.
    assert len(operations) == 7


def test_add_rechecks_mutated_reserved_id_before_transaction_writes(pg_store):
    store, conn = pg_store
    func = _sample_func()
    func.id = "domain_auth"

    with pytest.raises(ValueError, match="保留"):
        store.add(func, SourceDocument(type="text", source_type=SourceType.WIKI))
    assert conn._cursor.executed == []


def test_add_rechecks_mutated_non_string_domain_before_transaction_writes(pg_store):
    store, conn = pg_store
    func = _sample_func()
    func.domain = []

    with pytest.raises(ValueError, match="domain"):
        store.add(func, SourceDocument(type="text", source_type=SourceType.WIKI))
    assert conn._cursor.executed == []


def test_merge_rechecks_mutated_reserved_id_before_edge_or_counter_write(pg_store):
    from memplex.models import GraphData, GraphEdge

    store, conn = pg_store
    func = _sample_func("pg-valid", "Source")
    func.id = "domain_auth"
    with pytest.raises(ValueError, match="保留"):
        store.merge(
            GraphData(
                nodes=[func],
                edges=[GraphEdge("domain_auth", "pg-target", "REFERENCES")],
            )
        )
    assert not any("INSERT INTO memplex_edges" in sql for sql, _ in conn._cursor.executed)


def test_merge_rejects_duck_node_before_transaction_writes(pg_store):
    from memplex.models import GraphData, GraphEdge

    class DuckFunction:
        id = "pg-duck"
        name = "Duck"

    store, conn = pg_store
    with pytest.raises(ValueError, match="Function"):
        store.merge(
            GraphData(
                nodes=[DuckFunction()],
                edges=[GraphEdge("pg-duck", "pg-target", "REFERENCES")],
            )
        )
    assert conn._cursor.executed == []


def test_add_commits_once_for_function_and_changelog(pg_store):
    """Task 4 RED: a public add may not commit the row before its audit row."""
    store, conn = pg_store
    store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    assert conn.commits == 1


def test_acl_filtered_function_upsert_never_reports_success(pg_store):
    store, conn = pg_store
    conn._cursor._force_returning_none = True
    with pytest.raises(RuntimeError, match="authorized row"):
        store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    assert conn.commits == 0


def test_delete_executes_delete_sql(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = ("pg-1", json.dumps(_func_to_json(_sample_func())))
    store.delete("pg-1")
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_functions" in s for s in sqls)
    assert not any("DELETE FROM memplex_edges" in s for s in sqls)


def test_existing_source_domain_edge_uses_shared_domain_identifier(pg_store):
    from memplex.models import EdgeType, GraphData, GraphEdge, domain_node_id

    store, conn = pg_store
    source = _sample_func("pg-domain-source", "Domain",)
    source.domain = "  A  B "
    conn._cursor._fetchone_val = (
        source.id,
        json.dumps(_func_to_json(source)),
    )
    result = store.merge(
        GraphData(
            nodes=[],
            edges=[
                GraphEdge(
                    source.id,
                    domain_node_id(source.domain),
                    EdgeType.BELONGS_TO.value,
                )
            ],
        )
    )
    assert result.new_edges == 0
    assert not any(
        "SELECT id, data" in sql and domain_node_id(source.domain) in params
        for sql, params in conn._cursor.executed
    )


def test_malformed_domain_target_is_rejected_when_source_is_existing(pg_store, monkeypatch):
    from memplex.models import EdgeType, GraphData, GraphEdge

    store, _conn = pg_store
    source = _sample_func("pg-domain-malformed", "Domain")
    source.domain = "  A  B "
    identity = store._row_identity_values(store._authorization_context(), source)
    monkeypatch.setattr(
        store,
        "_locked_function_by_id",
        lambda _cur, func_id, _context: (source, identity)
        if func_id == source.id
        else None,
    )
    with pytest.raises(RuntimeError, match="authorized row"):
        store.merge(
            GraphData(
                nodes=[],
                edges=[
                    GraphEdge(
                        source.id,
                        "domain_a_b",
                        EdgeType.BELONGS_TO.value,
                    )
                ],
            )
        )


def test_belongs_to_cannot_target_an_existing_ordinary_function(pg_store, monkeypatch):
    from memplex.models import EdgeType, GraphData, GraphEdge

    store, conn = pg_store
    source = _sample_func("pg-domain-existing-source", "Domain")
    source.domain = "  A  B "
    target = _sample_func("pg-domain-existing-target", "Ordinary")
    identity = store._row_identity_values(store._authorization_context(), source)
    monkeypatch.setattr(
        store,
        "_locked_function_by_id",
        lambda _cur, func_id, _context: (source, identity)
        if func_id == source.id
        else (target, identity)
        if func_id == target.id
        else None,
    )
    with pytest.raises(RuntimeError, match="authorized row"):
        store.merge(
            GraphData(
                nodes=[],
                edges=[GraphEdge(source.id, target.id, EdgeType.BELONGS_TO.value)],
            )
        )
    assert not any("INSERT INTO memplex_edges" in sql for sql, _ in conn._cursor.executed)


def test_belongs_to_cannot_target_an_incoming_ordinary_function(pg_store, monkeypatch):
    from memplex.models import EdgeType, GraphData, GraphEdge

    store, conn = pg_store
    source = _sample_func("pg-domain-incoming-source", "Domain")
    source.domain = "  A  B "
    target = _sample_func("pg-domain-incoming-target", "Ordinary")
    identity = store._row_identity_values(store._authorization_context(), source)
    monkeypatch.setattr(
        store,
        "_locked_function_by_id",
        lambda _cur, func_id, _context: (source, identity)
        if func_id == source.id
        else None,
    )
    with pytest.raises(RuntimeError, match="authorized row"):
        store.merge(
            GraphData(
                nodes=[target],
                edges=[GraphEdge(source.id, target.id, EdgeType.BELONGS_TO.value)],
            )
        )
    assert not any("INSERT INTO memplex_edges" in sql for sql, _ in conn._cursor.executed)


def test_increment_access_executes_update(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = (
        "pg-1",
        json.dumps(_func_to_json(_sample_func("pg-1"))),
    )
    store.increment_access("pg-1")
    sql = conn._cursor.executed[-1][0]
    assert "UPDATE memplex_functions" in sql


def test_increment_access_batch_commits_once(pg_store):
    store, conn = pg_store
    function_a = _sample_func("a")
    function_b = _sample_func("b")
    function_c = _sample_func("c")
    identity = store._row_identity_values(store._authorization_context(), function_a)
    functions = {"a": function_a, "b": function_b, "c": function_c}

    def _locked(_cur, func_id, _context):
        if func_id not in functions:
            return None
        return (functions[func_id], identity)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_locked_function_by_id", _locked)
        store.increment_access_batch(["a", "b", "c"])
    # Batch path uses one transaction -> one commit.
    assert conn.commits == 1
    update_count = sum(1 for s, _ in conn._cursor.executed if "UPDATE" in s)
    assert update_count == 3


def test_increment_access_batch_rolls_back_the_whole_batch_on_late_failure(pg_store, monkeypatch):
    store, conn = pg_store
    before_rollbacks = conn.rollbacks
    original_execute = conn._cursor.execute
    conn._cursor._fetchone_val = (
        "task4-a",
        json.dumps(_func_to_json(_sample_func("task4-a"))),
    )
    updates = 0

    def fail_second_update(sql, params=()):
        nonlocal updates
        original_execute(sql, params)
        if "UPDATE memplex_functions" in sql:
            updates += 1
            if updates == 2:
                raise RuntimeError("task4 access batch fault")

    monkeypatch.setattr(conn._cursor, "execute", fail_second_update)
    with pytest.raises(RuntimeError, match="task4 access batch fault"):
        store.increment_access_batch(["task4-a", "task4-b"])
    assert conn.commits == 0
    assert conn.rollbacks == before_rollbacks + 1


def test_add_observation_executes_insert(pg_store):
    store, conn = pg_store
    from memplex.models import Observation

    store.add_observation(
        Observation(
            id="obs-1",
            name="x",
            event="deploy",
            context="3am",
            confidence=0.5,
            source_type=SourceType.WIKI,
        )
    )
    sql = next(
        (sql for sql, _ in conn._cursor.executed if "INSERT INTO memplex_observations" in sql),
    )
    assert "INSERT INTO memplex_observations" in sql


def test_obs_to_json_includes_category():
    from memplex.models import Observation

    data = _obs_to_json(Observation(id="obs-2", name="y", event="e", category="bugfix"))
    assert data["category"] == "bugfix"
    json.dumps(data)  # JSONB-safe


def test_obs_to_json_defaults_category_to_note():
    from memplex.models import Observation

    data = _obs_to_json(Observation(id="obs-3", name="y", event="e"))
    assert data["category"] == "note"


def test_list_observations_queries_jsonb(pg_store):
    from memplex.models import Observation

    store, conn = pg_store
    conn._cursor._result = [
        (json.dumps(Observation(id="o1", name="A", event="e", category="bugfix").to_dict()),),
        # Row predating the category key -> deserializes as "note".
        (json.dumps({"id": "o2", "memory_type": "observation", "event": "old"}),),
    ]
    observations = store.list_observations(offset=0, limit=10)
    assert [o.id for o in observations] == ["o1", "o2"]
    assert observations[0].category == "bugfix"
    assert observations[1].category == "note"
    sql = conn._cursor.executed[-1][0]
    assert "FROM memplex_observations" in sql
    assert "WHERE" not in sql  # no filters -> no WHERE clause


def test_list_observations_pushes_category_and_owner_into_sql(pg_store):
    store, conn = pg_store
    conn._cursor._result = []
    store.list_observations(category="decision", owner="alice", offset=5, limit=10)
    sql, params = next(
        (sql, params)
        for sql, params in conn._cursor.executed
        if "FROM memplex_observations" in sql
    )
    assert "data->>'category' = %s" in sql
    assert "data->>'owner' = %s" in sql
    assert params == ("decision", "alice", 5, 10)


# ── Read operations (mock results) ───────────────────────────────────


def test_get_returns_function_when_found(pg_store):
    store, conn = pg_store
    f = _sample_func()
    conn._cursor._fetchone_val = (json.dumps(_func_to_json(f)),)
    got = store.get("pg-1")
    assert got is not None
    assert got.id == "pg-1"


def test_get_returns_none_when_missing(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = None
    assert store.get("missing") is None


def test_read_parse_failure_releases_transaction_lock_for_next_thread(pg_store):
    store, conn = pg_store
    conn._cursor._result = [("not-json",)]
    with pytest.raises(json.JSONDecodeError):
        store.list_functions()

    conn._cursor._result = [(json.dumps(_func_to_json(_sample_func())),)]
    finished = threading.Event()
    result = []

    def read_after_failure():
        try:
            result.extend(store.list_functions())
        finally:
            finished.set()

    thread = threading.Thread(target=read_after_failure, daemon=True)
    thread.start()
    completed_without_manual_unlock = finished.wait(timeout=0.5)
    if not completed_without_manual_unlock:
        # RED cleanup: the pre-fix lock is owned by this test thread. Release
        # it so the daemon can exit instead of contaminating the test process.
        store._transaction_lock.release()
        finished.wait(timeout=1)
    thread.join(timeout=1)

    assert completed_without_manual_unlock is True
    assert result and result[0].id == "pg-1"


def test_vector_search_uses_tsquery(pg_store):
    store, conn = pg_store
    f = _sample_func()
    conn._cursor._result = [("pg-1", json.dumps(_func_to_json(f)), 0.9)]
    results = store.vector_search("login", top_k=5)
    assert len(results) == 1
    assert results[0].func_id == "pg-1"
    sql = conn._cursor.executed[-1][0]
    assert "plainto_tsquery" in sql


def test_list_functions_paginates(pg_store):
    store, conn = pg_store
    f1 = _sample_func("pg-a")
    f2 = _sample_func("pg-b")
    conn._cursor._result = [(json.dumps(_func_to_json(f1)),), (json.dumps(_func_to_json(f2)),)]
    funcs = store.list_functions(offset=0, limit=10)
    assert len(funcs) == 2
    sql = conn._cursor.executed[-1][0]
    assert "OFFSET" in sql and "LIMIT" in sql


def test_clear_deletes_all_tables(pg_store):
    store, conn = pg_store
    store.clear()
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_functions" in s for s in sqls)
    assert any("DELETE FROM memplex_edges" in s for s in sqls)
    assert any("DELETE FROM memplex_observations" in s for s in sqls)


# ── Factory routing ──────────────────────────────────────────────────


def test_factory_postgres_backend_returns_postgres_store(monkeypatch):
    from memplex.storage import create_store

    store = create_store("postgres", path="dbname=fake", ready_pool=_test_ready_pool())
    assert isinstance(store, PostgresMemoryStore)


def test_factory_postgres_accepts_inbound_executor_and_forwards_to_store():
    from memplex.storage import create_store
    from memplex.storage.inbound import InboundSyncExecutor

    @contextmanager
    def _tx():
        yield (None, None)

    executor = InboundSyncExecutor(_tx)
    store = create_store(
        "postgres",
        path="dbname=fake",
        ready_pool=_test_ready_pool(),
        inbound_executor=executor,
    )
    assert isinstance(store, PostgresMemoryStore)
    assert store._inbound_executor is executor


def test_factory_postgres_forwards_exact_required_sync_capture_policy():
    from memplex.storage import create_store

    policy = SyncCapturePolicy("required", local_node_id="local-node-1")
    store = create_store(
        "postgres",
        path="dbname=fake",
        ready_pool=_test_ready_pool(),
        sync_capture_policy=policy,
        sync_max_attempts=13,
        sync_snapshot_ttl_seconds=901,
        sync_max_snapshot_items=1001,
        sync_max_active_snapshots_per_tenant=3,
        sync_max_active_snapshots_per_remote=2,
        sync_snapshot_create_timeout_seconds=31,
    )

    assert isinstance(store, PostgresMemoryStore)
    assert store._sync_capture_policy is policy
    assert store._sync_repository._max_attempts == 13
    assert store._sync_repository._snapshot_ttl_seconds == 901
    assert store._sync_repository._max_snapshot_items == 1001
    assert store._sync_repository._max_active_snapshots_per_tenant == 3
    assert store._sync_repository._max_active_snapshots_per_remote == 2
    assert store._sync_repository._snapshot_create_timeout_seconds == 31


def test_required_sync_capture_constructs_postgres_sync_repository():
    from memplex.storage.postgres_sync import PostgresSyncRepository

    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(),
        sync_capture_policy=SyncCapturePolicy(
            "required", local_node_id="local-node-1"
        ),
        sync_max_attempts=11,
        sync_snapshot_ttl_seconds=902,
        sync_max_snapshot_items=1002,
        sync_max_active_snapshots_per_tenant=4,
        sync_max_active_snapshots_per_remote=3,
        sync_snapshot_create_timeout_seconds=32,
    )

    assert type(store._sync_repository) is PostgresSyncRepository
    assert store._sync_repository._max_attempts == 11
    assert store._sync_repository._snapshot_ttl_seconds == 902
    assert store._sync_repository._max_snapshot_items == 1002
    assert store._sync_repository._max_active_snapshots_per_tenant == 4
    assert store._sync_repository._max_active_snapshots_per_remote == 3
    assert store._sync_repository._snapshot_create_timeout_seconds == 32


def test_sync_repository_methods_delegate_through_authorized_facade(monkeypatch):
    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(),
        require_authorization=True,
        sync_capture_policy=SyncCapturePolicy(
            "required", local_node_id="local-node-1"
        ),
    )
    calls = []

    def _status():
        calls.append(store._authorization_context().principal.tenant_id)
        return "status"

    monkeypatch.setattr(store._sync_repository, "sync_status", _status)
    context = _authorization(
        tenant="tenant-sync-delegate", subject="subject-sync-delegate"
    )

    assert store.authorized(context).sync_status() == "status"
    assert calls == ["tenant-sync-delegate"]


def test_sync_repository_is_unavailable_when_capture_is_off():
    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(),
    )

    with pytest.raises(RuntimeError, match="sync repository is not enabled"):
        store.sync_status()


def test_postgres_store_rejects_non_exact_inbound_executor():
    class _DuckExecutor:
        def transaction(self, *_args, **_kwargs):
            raise AssertionError

    with pytest.raises(TypeError, match="inbound_executor must be an InboundSyncExecutor"):
        PostgresMemoryStore(
            dsn="dbname=fake",
            ready_pool=_test_ready_pool(),
            inbound_executor=_DuckExecutor(),
        )


class _ExactPolicy(SyncCapturePolicy):
    pass


def test_postgres_store_rejects_non_exact_sync_capture_policy():
    with pytest.raises(TypeError, match="sync_capture_policy must be an exact SyncCapturePolicy"):
        PostgresMemoryStore(
            dsn="dbname=fake",
            ready_pool=_test_ready_pool(),
            sync_capture_policy=_ExactPolicy("off"),  # noqa: E501
        )


def test_sync_capture_policy_rejects_non_str_mode_and_invalid_local_node():
    class _StrChild(str):
        pass

    with pytest.raises(TypeError, match="exact type str"):
        SyncCapturePolicy(_StrChild("required"))
    with pytest.raises(TypeError, match="exact type str"):
        SyncCapturePolicy("required", local_node_id=_StrChild("node"))


def test_sync_capture_policy_required_enforces_nonempty_stripped_local_node_id():
    with pytest.raises(ValueError, match="non-empty when mode is required"):
        SyncCapturePolicy("required", local_node_id="")
    with pytest.raises(ValueError, match="after strip"):
        SyncCapturePolicy("required", local_node_id="   ")
    policy = SyncCapturePolicy("required", local_node_id="  node-id  ")
    assert policy.local_node_id == "node-id"


def test_factory_postgres_without_dsn_raises():
    from memplex.storage import create_store

    with pytest.raises(ValueError, match="DSN"):
        create_store("postgres")


def test_factory_unknown_backend_still_raises():
    from memplex.storage import create_store

    with pytest.raises(ValueError):
        create_store("not-a-backend")


# ── Construction lazy (no psycopg2 needed to import/construct) ───────


def test_postgres_store_has_no_private_connection():
    """A ready store retains only its injected shared pool manager."""
    store = PostgresMemoryStore(dsn="dbname=fake", ready_pool=_test_ready_pool())
    assert store._dsn == "dbname=fake"
    assert not hasattr(store, "_conn")


def test_production_postgres_store_rejects_unscoped_calls_and_binds_set_local(
    monkeypatch,
):
    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(),
        require_authorization=True,
    )
    mock_conn = store._pool_manager._pool.connection
    mock_conn._cursor.executed.clear()

    with pytest.raises(PermissionError, match="authorization context"):
        store.get("pg-1")

    mock_conn._cursor._fetchone_val = (json.dumps(_func_to_json(_sample_func())),)
    scoped = store.authorized(_authorization())
    assert scoped.get("pg-1").id == "pg-1"

    sql_and_params = mock_conn._cursor.executed
    context_calls = [
        (sql, params) for sql, params in sql_and_params if "set_config('memplex.tenant_id'" in sql
    ]
    assert context_calls
    assert "tenant-a" in context_calls[0][1]
    assert any("FROM memplex_functions" in sql for sql, _ in sql_and_params)


def test_authorized_store_canonicalizes_payload_identity_to_active_context(pg_store):
    store, conn = pg_store
    node = _sample_func("canonicalized-workspace")
    node.visibility = "workspace"
    node.tenant_id = "forged-tenant"
    node.owner_subject_id = "alice"
    node.owner = "alice"
    node.workspace_id = "forged-workspace"
    node.namespace = {
        "memplex_tenant_id": "forged-tenant",
        "memplex_subject_id": "alice",
        "memplex_workspace_id": "forged-workspace",
    }
    node.provenance = {
        "agent_id": "forged-agent",
        "authentication_id": "forged-credential",
        "request_id": "forged-request",
        "session_id": "forged-session",
    }

    context = _authorization(tenant="tenant-a", subject="bob")
    store.authorized(context).add(
        node,
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )

    assert node.tenant_id == "tenant-a"
    assert node.owner_subject_id == "bob"
    assert node.owner == "bob"
    assert node.workspace_id == "shared-workspace"
    assert node.namespace["memplex_tenant_id"] == "tenant-a"
    assert node.namespace["memplex_subject_id"] == "bob"
    assert node.namespace["memplex_workspace_id"] == "shared-workspace"
    assert node.provenance == {
        "agent_id": "http",
        "authentication_id": "credential-bob",
        "request_id": "request-bob",
        "session_id": "session-bob",
    }

    upsert_params = next(
        params
        for sql, params in conn._cursor.executed
        if "INSERT INTO memplex_functions" in sql
    )
    assert upsert_params[-6:] == (
        "tenant-a",
        "bob",
        "shared-workspace",
        "workspace",
        "http",
        "session-bob",
    )


def test_session_visibility_rejects_empty_agent_or_session_before_sql(monkeypatch):
    from memplex.auth import AuthorizationContext, Principal

    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(),
        require_authorization=True,
    )
    mock_conn = store._pool_manager._pool.connection
    mock_conn._cursor.executed.clear()
    incomplete = AuthorizationContext(
        principal=Principal(tenant_id="tenant-a", subject_id="alice"),
        workspace_id="shared-workspace",
    )
    node = _sample_func("session-node")
    node.visibility = "session"

    with pytest.raises(PermissionError, match="agent_id.*session_id"):
        store.authorized(incomplete).add(
            node,
            SourceDocument(type="text", source_type=SourceType.WIKI),
        )
    assert mock_conn._cursor.executed == []


def test_postgres_store_has_no_dynamic_schema_method():
    store = PostgresMemoryStore(dsn="dbname=fake", ready_pool=_test_ready_pool())
    assert not hasattr(store, "_ensure_schema")


# ── pgvector hybrid search ───────────────────────────────────────────


class _StubEmbedder:
    """Fixed-dimension embedder for pgvector tests."""

    def __init__(self, dim=4):
        self.dim = dim

    def embed(self, text):
        # Deterministic vector derived from text length so identical texts collide.
        base = [0.0] * self.dim
        for i, ch in enumerate(text[: self.dim]):
            base[i] = float(ord(ch) % 10) / 10.0
        return base


def test_pgvector_add_writes_embedding_when_enabled(monkeypatch):
    store = PostgresMemoryStore(
        dsn="dbname=fake", ready_pool=_test_ready_pool(dim=4), embedder=_StubEmbedder(4)
    )
    mock_conn = store._pool_manager._pool.connection
    store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    # The functions INSERT should reference the embedding column.
    upserts = [
        (s, p)
        for s, p in mock_conn._cursor.executed
        if "INSERT INTO memplex_functions" in s
    ]
    assert len(upserts) == 1
    assert "embedding" in upserts[0][0]
    assert upserts[0][1][3] is not None  # the embedding literal


def test_pgvector_add_skips_embedding_when_no_embedder(monkeypatch):
    store = PostgresMemoryStore(
        dsn="dbname=fake", ready_pool=_test_ready_pool(dim=4), embedder=None
    )
    mock_conn = store._pool_manager._pool.connection
    store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    upserts = [s for s, _ in mock_conn._cursor.executed if "INSERT INTO memplex_functions" in s]
    assert len(upserts) == 1
    assert "embedding" not in upserts[0]  # plain INSERT, no vector column


def test_pgvector_vector_dim_zero_disables_vector_search(monkeypatch):
    """vector_dim=0 -> vector_search is tsvector-only (no pgvector leg)."""
    store = PostgresMemoryStore(dsn="dbname=fake", ready_pool=_test_ready_pool())
    mock_conn = store._pool_manager._pool.connection
    f = _sample_func()
    mock_conn._cursor._result = [("pg-1", json.dumps(_func_to_json(f)), 0.9)]
    results = store.vector_search("login", top_k=5)
    assert len(results) == 1
    # Scope bind plus only one business SQL (the tsv leg) -- no vector leg.
    assert sum("FROM memplex_functions" in sql for sql, _ in mock_conn._cursor.executed) == 1


def test_rrf_merge_fuses_both_legs():
    """RRF gives a doc found in both legs a higher fused score."""
    row_a = ("a", json.dumps({"name": "a", "trigger_text": "x"}), 0.9)
    row_b = ("b", json.dumps({"name": "b", "trigger_text": "y"}), 0.8)
    vec_a = ("a", json.dumps({"name": "a", "trigger_text": "x"}), 0.7)
    merged = PostgresMemoryStore._rrf_merge([row_a, row_b], [vec_a], top_k=5)
    # 'a' appears in both legs -> higher RRF score -> ranked first.
    assert merged[0].func_id == "a"
    assert merged[0].relevance_score > merged[1].relevance_score


def test_rrf_merge_empty_legs_returns_empty():
    assert PostgresMemoryStore._rrf_merge([], [], top_k=5) == []


def test_rrf_merge_single_leg_works():
    row = ("x", json.dumps({"name": "x", "trigger_text": "t"}), 0.5)
    merged = PostgresMemoryStore._rrf_merge([row], [], top_k=5)
    assert len(merged) == 1
    assert merged[0].func_id == "x"


def test_pgvector_dim_is_explicit_not_re_read_from_env(monkeypatch):
    """The service resolves env once; a store accepts only that effective value."""
    monkeypatch.setenv("MEMPLEX_PGVECTOR_DIM", "8")
    store = PostgresMemoryStore(dsn="dbname=fake", ready_pool=_test_ready_pool())
    assert store._vector_dim == 0


def test_pgvector_embed_text_returns_literal_string(monkeypatch):
    store = PostgresMemoryStore(
        dsn="dbname=fake", ready_pool=_test_ready_pool(dim=4), embedder=_StubEmbedder(4)
    )
    vec_str = store._embed_text(_sample_func())
    assert vec_str is not None
    assert vec_str.startswith("[") and vec_str.endswith("]")
    assert len(json.loads(vec_str)) == 4


# ── Regression: serialization must not drop fields ───────────────────


def test_field_value_roundtrip_preserves_observation_created_at_status():
    """_fv_to_json previously dropped observation/created_at/status."""
    from datetime import datetime, timezone

    from memplex.storage.postgres import _fv_from_json, _fv_to_json

    fv = FieldValue(
        desc="step",
        sources=["s1"],
        source_method="llm_semantic",
        weight=0.7,
        observation=0.42,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        status="disputed",
    )
    restored = _fv_from_json(json.loads(json.dumps(_fv_to_json(fv))))
    assert restored.observation == 0.42
    assert restored.created_at == fv.created_at
    assert restored.status == "disputed"


def test_func_roundtrip_preserves_review_and_priority_fields():
    """_func_to_json/_func_from_json previously dropped needs_review_until,
    priority_from_source and source_authority."""
    f = _sample_func()
    f.needs_review_until = "2026-02-01T00:00:00"
    f.priority_from_source = "high"
    f.source_authority = "official-docs"
    restored = _func_from_json(json.loads(json.dumps(_func_to_json(f))))
    assert restored.needs_review_until == "2026-02-01T00:00:00"
    assert restored.priority_from_source == "high"
    assert restored.source_authority == "official-docs"


def test_observation_serialization_preserves_identity_fields():
    """add_observation previously persisted only id/event/context, losing
    name/domain/actor/observed_at/memory_type."""
    from memplex.models import Observation
    from memplex.storage.postgres import _obs_to_json

    obs = Observation(
        id="obs-rt",
        name="deploy failed",
        domain="ops",
        event="deploy",
        context="3am rollback",
        observed_at="2026-03-01T03:00:00",
        actor="agent-7",
    )
    data = json.loads(json.dumps(_obs_to_json(obs)))
    assert data["memory_type"] == "observation"
    assert data["name"] == "deploy failed"
    assert data["domain"] == "ops"
    assert data["actor"] == "agent-7"
    assert data["observed_at"] == "2026-03-01T03:00:00"
    assert data["event"] == "deploy"


def test_add_observation_persists_full_payload(pg_store):
    from memplex.models import Observation

    store, conn = pg_store
    store.add_observation(
        Observation(id="obs-2", name="n", domain="d", event="e", actor="bot")
    )
    sql, params = next(
        (sql, params)
        for sql, params in conn._cursor.executed
        if "INSERT INTO memplex_observations" in sql
    )
    assert "INSERT INTO memplex_observations" in sql
    payload = json.loads(params[1])
    assert payload["name"] == "n"
    assert payload["domain"] == "d"
    assert payload["actor"] == "bot"


def _required_capture_store(pool: _PoolMock | None = None) -> tuple[PostgresMemoryStore, _MockConn]:
    pool = pool or _PoolMock()
    ready_pool = _test_ready_pool(pool=pool)
    store = PostgresMemoryStore(
        dsn="dbname=fake",
        ready_pool=ready_pool,
        sync_capture_policy=SyncCapturePolicy("required", local_node_id="unit-local-node"),
    )
    pool.connection._cursor.executed.clear()
    return store, pool.connection


def test_required_sync_capture_binds_observation_payload_during_upsert():
    from memplex.models import Observation
    from memplex.sync_protocol import SyncEntityKey

    store, conn = _required_capture_store()
    observation = Observation(
        id="obs-1",
        name="Observe",
        domain="ops",
        event="deploy",
        actor="agent",
        context="ci",
    )
    store.add_observation(observation)

    sqls = [s for s, _ in conn._cursor.executed]
    sync_idx = next(
        i for i, s in enumerate(sqls) if "set_config('memplex.sync_capture'" in s
    )
    scope_idx = next(
        i for i, s in enumerate(sqls) if "set_config('memplex.tenant_id'" in s
    )
    insert_idx = next(
        i for i, s in enumerate(sqls) if "INSERT INTO memplex_observations" in s
    )
    assert scope_idx < sync_idx < insert_idx

    _, _, _, event_id, version_key, entity_key, sync_payload = next(
        p for s, p in conn._cursor.executed if "set_config('memplex.sync_capture'" in s
    )
    assert event_id
    assert version_key.startswith("v1:")
    assert entity_key == str(SyncEntityKey.node("obs-1"))
    assert json.loads(sync_payload) == _obs_to_json(observation)

    insert_payload = json.loads(
        [p for s, p in conn._cursor.executed if "INSERT INTO memplex_observations" in s][0][1]
    )
    assert json.loads(sync_payload) == insert_payload


@pytest.mark.parametrize(
    "method,table",
    (
        ("add_fact", "memplex_facts"),
        ("add_preference", "memplex_preferences"),
    ),
)
def test_required_sync_capture_upsert_payload_matches_persisted_json(method, table):
    from memplex.models import Fact, Preference

    store, conn = _required_capture_store()

    node = (
        Fact(id="typed-1", name="Fact", subject="s", predicate="is", object_="o")
        if method == "add_fact"
        else Preference(
            id="typed-1", name="Pref", aspect="theme", preference="dark"
        )
    )
    getattr(store, method)(node)

    sync_payload = next(
        p for s, p in conn._cursor.executed if "set_config('memplex.sync_capture'" in s
    )[6]
    row_payload = next(p for s, p in conn._cursor.executed if f"INSERT INTO {table}" in s)[1]
    assert json.loads(sync_payload) == json.loads(row_payload)


@pytest.mark.parametrize(
    "method,statement",
    (
        ("delete_fact", "DELETE FROM memplex_facts"),
        ("delete_preference", "DELETE FROM memplex_preferences"),
        ("delete_observation", "DELETE FROM memplex_observations"),
    ),
)
def test_required_sync_capture_binds_empty_payload_for_tombstones(method, statement):
    store, conn = _required_capture_store()
    getattr(store, method)("typed-1")

    sync_idx = next(
        i for i, (s, _p) in enumerate(conn._cursor.executed)
        if "set_config('memplex.sync_capture'" in s
    )
    sync_payload = conn._cursor.executed[sync_idx][1][6]
    delete_idx = next(
        i for i, (s, _p) in enumerate(conn._cursor.executed)
        if statement in s and "RETURNING" in s
    )
    assert sync_payload == ""
    assert sync_idx < delete_idx


def test_required_sync_capture_increment_access_binds_full_payload_before_full_row_update():
    from memplex.sync_protocol import SyncEntityKey

    store, conn = _required_capture_store()
    function = _sample_func("inc-full", "Increment")
    conn._cursor._fetchone_val = (
        function.id,
        json.dumps(_func_to_json(function)),
    )

    store.increment_access(function.id)

    sync_idxs = [
        i for i, (s, _p) in enumerate(conn._cursor.executed)
        if "set_config('memplex.sync_capture'" in s
    ]
    update_idxs = [
        i for i, (s, _p) in enumerate(conn._cursor.executed)
        if "UPDATE memplex_functions" in s and "RETURNING id" in s
    ]
    assert len(sync_idxs) == 1
    assert len(update_idxs) == 1
    assert sync_idxs[0] < update_idxs[0]

    sync_params = conn._cursor.executed[sync_idxs[0]][1]
    update_params = conn._cursor.executed[update_idxs[0]][1]
    sync_payload = json.loads(sync_params[6])
    update_payload = json.loads(update_params[0])
    assert sync_params[5] == str(SyncEntityKey.node(function.id))
    assert sync_payload == update_payload
    assert sync_payload["access_count"] == function.access_count + 1
    assert sync_payload["last_accessed_at"] is not None


def test_required_sync_capture_increment_access_batch_publishes_separate_access_payloads_for_duplicates():
    from memplex.sync_protocol import SyncEntityKey

    store, conn = _required_capture_store()

    base = _sample_func("inc-batch", "Increment Batch")
    identity = store._row_identity_values(store._authorization_context(), base)

    def _locked(_cur, func_id, _context):
        if func_id != base.id:
            return None
        return (base, identity)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_locked_function_by_id", _locked)
        store.increment_access_batch([base.id, "missing", base.id])

    sync_calls = [
        p for s, p in conn._cursor.executed if "set_config('memplex.sync_capture'" in s
    ]
    assert len(sync_calls) == 2
    payloads = [json.loads(params[6]) for params in sync_calls]
    assert payloads[0]["access_count"] == 1
    assert payloads[1]["access_count"] == 2

    for p in payloads:
        assert p["id"] == base.id
    assert sync_calls[0][5] == str(SyncEntityKey.node(base.id))

    update_calls = [
        p for s, p in conn._cursor.executed
        if "UPDATE memplex_functions" in s and "RETURNING id" in s
    ]
    assert len(update_calls) == 2
    assert all(json.loads(params[0])["id"] == base.id for params in update_calls)
    assert all(
        json.loads(update_calls[i][0])["access_count"]
        == payloads[i]["access_count"]
        for i in (0, 1)
    )


def test_required_sync_capture_clear_deletes_entities_in_expected_order_with_returning():
    from memplex.sync_protocol import SyncEntityKey

    store, conn = _required_capture_store()
    function = _sample_func("clear-full", "Clear")
    conn._cursor._fetchone_val = (
        function.id,
        json.dumps(_func_to_json(function)),
    )

    def _fetchall():
        sql, _params = conn._cursor.executed[-1]
        if "SELECT source, target, edge_type FROM memplex_edges" in sql:
            return [("clear-full", "edge-a", "DERIVED"), ("edge-b", "clear-full", "SUPPORTS")]
        if "SELECT id FROM memplex_functions" in sql:
            return [(function.id,),]
        if "SELECT id FROM memplex_observations" in sql:
            return [(function.id,),]
        if "SELECT id FROM memplex_facts" in sql:
            return [(function.id,),]
        if "SELECT id FROM memplex_preferences" in sql:
            return [(function.id,),]
        return []

    original_fetchall = conn._cursor.fetchall

    conn._cursor._result = []
    conn._cursor.fetchall = _fetchall
    try:
        store.clear()
    finally:
        conn._cursor.fetchall = original_fetchall

    sync_calls = [
        (i, p)
        for i, (s, p) in enumerate(conn._cursor.executed)
        if "set_config('memplex.sync_capture'" in s
    ]
    sqls = [s for s, _ in conn._cursor.executed]
    edge_sync_idxs = [idx for idx in [i for i, _ in sync_calls][:2]]
    edge_delete_idxs = [
        i for i, s in enumerate(sqls) if "DELETE FROM memplex_edges" in s and "RETURNING" in s
    ]
    function_sync_idx = sync_calls[2][0]
    function_delete_idx = next(
        i for i, s in enumerate(sqls)
        if "DELETE FROM memplex_functions" in s and "RETURNING" in s
    )
    typed_sync_idxs = [
        i
        for i, s in enumerate(sqls)
        if "set_config('memplex.sync_capture'" in s
        and i not in edge_sync_idxs
        and i >= function_sync_idx + 1
    ]
    typed_delete_idxs = [
        i
        for i, s in enumerate(sqls)
        if "DELETE FROM memplex_" in s
        and " RETURNING" in s
        and "memplex_functions" not in s
        and "memplex_edges" not in s
    ]

    assert len(sync_calls) == 6
    function_sync_params = sync_calls[2][1]
    assert function_sync_params[5] == str(SyncEntityKey.node(function.id))
    assert edge_sync_idxs[0] < edge_delete_idxs[0] < edge_sync_idxs[1] < edge_delete_idxs[1]
    assert edge_delete_idxs[1] < function_delete_idx
    assert edge_delete_idxs == sorted(edge_delete_idxs)
    assert len(typed_delete_idxs) == 3
    assert typed_delete_idxs == sorted(typed_delete_idxs)
    assert typed_delete_idxs[0] > function_delete_idx
    assert function_sync_idx < function_delete_idx
    assert len(typed_sync_idxs) == 3
    assert all("set_config('memplex.sync_capture'" in sqls[i] for i in typed_sync_idxs)
    assert all("RETURNING id" in sqls[i] for i in typed_delete_idxs)
    typed_keys = [conn._cursor.executed[i][1][5] for i in typed_sync_idxs]
    assert typed_keys == [
        str(SyncEntityKey.node(function.id)),
        str(SyncEntityKey.node(function.id)),
        str(SyncEntityKey.node(function.id)),
    ]
    assert typed_sync_idxs[0] > function_delete_idx
    assert sync_calls[0][1][5] == str(
        SyncEntityKey.edge("clear-full", "edge-a", "DERIVED")
    )
    assert sync_calls[1][1][5] == str(
        SyncEntityKey.edge("edge-b", "clear-full", "SUPPORTS")
    )
    assert all(sync_calls[idx][1][6] == "" for idx in range(2, 6))
    assert typed_sync_idxs[0] < typed_sync_idxs[1] < typed_sync_idxs[2]
    assert typed_delete_idxs[0] < typed_delete_idxs[1] < typed_delete_idxs[2]
    assert any("DELETE FROM memplex_changelog" in s for s in sqls)


def test_required_sync_capture_add_function_binds_canonical_payload_before_upsert():
    from memplex.models import SourceDocument

    store, conn = _required_capture_store()
    function = _sample_func("func-1", "Capture")
    store.add(function, SourceDocument(type="text", source_type=SourceType.WIKI))

    sqls = [s for s, _ in conn._cursor.executed]
    sync_idx = next(
        i for i, s in enumerate(sqls) if "set_config('memplex.sync_capture'" in s
    )
    scope_idx = next(
        i for i, s in enumerate(sqls) if "set_config('memplex.tenant_id'" in s
    )
    upsert_idx = next(
        i for i, s in enumerate(sqls) if "INSERT INTO memplex_functions" in s
    )
    assert scope_idx < sync_idx < upsert_idx

    sync_payload = next(
        p for s, p in conn._cursor.executed if "set_config('memplex.sync_capture'" in s
    )[6]
    row_payload = next(p for s, p in conn._cursor.executed if "INSERT INTO memplex_functions" in s)[1]
    assert json.loads(sync_payload) == _func_to_json(function)
    assert row_payload == json.dumps(_func_to_json(function))


def test_required_sync_capture_merge_same_name_updates_emit_unique_event_per_function_mutation():
    from memplex.models import GraphData

    store, conn = _required_capture_store()
    canonical = _sample_func("func-canon", "Merge")
    identity = store._row_identity_values(store._authorization_context(), canonical)
    store._write_identity(canonical)

    def patched_locked(_cur, _func_id, _context):
        return canonical, identity

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_locked_function_by_id", patched_locked)
        patch.setattr(
            store,
            "_normalized_function",
            lambda *_args, **_kwargs: (canonical, identity),
        )
        store.merge(
            GraphData(
                nodes=[
                    _sample_func("merge-in-1", "Merge"),
                    _sample_func("merge-in-2", "Merge"),
                ],
                edges=[],
            )
        )

    sync_calls = [p for s, p in conn._cursor.executed if "set_config('memplex.sync_capture'" in s]
    assert len(sync_calls) == 2
    assert sync_calls[0][3] != sync_calls[1][3]
    upserts = [
        p for s, p in conn._cursor.executed if "INSERT INTO memplex_functions" in s
    ]
    assert len(upserts) == 2
    upserts = [p for s, p in conn._cursor.executed if "INSERT INTO memplex_functions" in s]
    assert len(upserts) == 2


def test_required_sync_capture_edge_only_merge_binds_full_edge_payload_before_upsert():
    from datetime import datetime, timezone

    from memplex.models import GraphData, GraphEdge
    from memplex.sync_protocol import SyncEntityKey

    store, conn = _required_capture_store()
    source = _sample_func("edge-source", "Source")
    target = _sample_func("edge-target", "Target")
    identity = store._row_identity_values(store._authorization_context(), source)
    created_at = datetime(2026, 8, 11, 1, 2, 3, 456789, tzinfo=timezone.utc)
    edge = GraphEdge(
        source=source.id,
        target=target.id,
        edge_type="REFERENCES",
        weight=0.75,
        evidence=["proof", "证据"],
        created_at=created_at,
    )

    def locked(_cur, func_id, _context):
        return (source if func_id == source.id else target, identity)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_require_visible_function_endpoint", locked)
        store.merge(GraphData(nodes=[], edges=[edge]))

    sync_calls = [
        (index, params)
        for index, (sql, params) in enumerate(conn._cursor.executed)
        if "set_config('memplex.sync_capture'" in sql
    ]
    assert len(sync_calls) == 1
    sync_index, sync_params = sync_calls[0]
    edge_insert_index = next(
        index
        for index, (sql, _params) in enumerate(conn._cursor.executed)
        if "INSERT INTO memplex_edges" in sql
    )
    edge_insert_sql = conn._cursor.executed[edge_insert_index][0]
    assert sync_index < edge_insert_index
    assert "created_at = EXCLUDED.created_at" in edge_insert_sql
    assert sync_params[5] == str(
        SyncEntityKey.edge(source.id, target.id, edge.edge_type)
    )
    assert json.loads(sync_params[6]) == {
        "created_at": "2026-08-11T01:02:03.456789Z",
        "evidence": ["proof", "证据"],
        "weight": 0.75,
    }


def test_required_sync_capture_delete_function_tombstones_edges_before_function_with_unique_edge_keys():
    from memplex.sync_protocol import SyncEntityKey

    store, conn = _required_capture_store()
    function = _sample_func("func-delete", "Delete")
    function_key = str(SyncEntityKey.node(function.id))
    conn._cursor._fetchone_val = (function.id, json.dumps(_func_to_json(function)))
    conn._cursor._result = [
        ("func-delete", "leaf", "DERIVED"),
        ("hub", "func-delete", "TRIGGERS"),
    ]
    store.delete("func-delete")

    sqls = [s for s, _ in conn._cursor.executed]
    edge_lock_idx = next(
        i
        for i, s in enumerate(sqls)
        if "FROM memplex_edges" in s and "FOR UPDATE" in s
    )
    sync_calls = [i for i, s in enumerate(sqls) if "set_config('memplex.sync_capture'" in s]
    edge_deletes = [
        i for i, s in enumerate(sqls) if "DELETE FROM memplex_edges" in s and "RETURNING" in s
    ]
    function_delete_idx = next(
        i for i, s in enumerate(sqls) if "DELETE FROM memplex_functions" in s
    )
    sync_calls_with_params = [
        params for sql, params in conn._cursor.executed if "set_config('memplex.sync_capture'" in sql
    ]
    sync_entities = [params[5] for params in sync_calls_with_params]

    assert len(sync_calls) == 3
    assert edge_lock_idx < sync_calls[0] < edge_deletes[0] < sync_calls[1] < edge_deletes[1] < sync_calls[2] < function_delete_idx

    expected_edges = {
        str(SyncEntityKey.edge("func-delete", "leaf", "DERIVED")),
        str(SyncEntityKey.edge("hub", "func-delete", "TRIGGERS")),
    }
    assert sync_entities[0] in expected_edges
    assert sync_entities[1] in expected_edges
    assert sync_entities[0] != sync_entities[1]
    assert sync_entities[2] == function_key
    assert all(p[6] == "" for p in sync_calls_with_params)


def test_required_sync_capture_delete_noop_function_no_sync_capture():
    store, conn = _required_capture_store()
    store.delete("missing-function")
    sync_calls = [s for s, _p in conn._cursor.executed if "set_config('memplex.sync_capture'" in s]
    assert sync_calls == []


def test_required_sync_capture_off_mode_sets_no_sync_gucs(pg_store):
    store, conn = pg_store
    store.add_fact(_sample_fact())
    assert not any("memplex.sync_capture" in s for s, _ in conn._cursor.executed)


# ── Regression: merge() must return a real MergeResult ───────────────


def _make_graph():
    from memplex.models import GraphData, GraphEdge

    node = _sample_func("pg-m1", "merge-node")
    target = _sample_func("pg-m2", "merge-target")
    edge = GraphEdge(source="pg-m1", target="pg-m2", edge_type="REFERENCES")
    return GraphData(nodes=[node, target], edges=[edge])


def test_merge_returns_merge_result_with_real_fields(pg_store):
    """merge() used to build MergeResult with non-existent kwargs ->
    TypeError on every call."""
    from memplex.models import MergeResult

    store, conn = pg_store
    conn._cursor._fetchone_val = None  # nothing pre-exists -> all new
    result = store.merge(_make_graph())
    assert isinstance(result, MergeResult)
    assert result.merged is True
    assert result.new_functions == 2
    assert result.updated_functions == 0
    assert result.new_edges == 1


def test_merge_counts_existing_nodes_as_updated(pg_store):
    store, conn = pg_store
    existing = _sample_func("pg-m1", "merge-node")
    # The Task 4 canonical-row lock reads the complete stored function before
    # it decides whether the graph node is an update.
    conn._cursor._fetchone_val = ("pg-m1", json.dumps(_func_to_json(existing)))
    result = store.merge(_make_graph())
    assert result.new_functions == 0
    assert result.updated_functions == 1
    assert result.new_edges == 0


def test_merge_locks_nodes_and_writes_edges_in_stable_order(pg_store):
    """Reverse graph payloads must acquire the same lock/write order.

    This is the mock-level lock-order proof for the real-PostgreSQL 40P01
    regression: callers can submit B then A and B→A then A→B, but the store
    must process A before B everywhere that can take row locks.
    """
    from memplex.models import GraphData, GraphEdge

    store, conn = pg_store
    node_b = _sample_func("pg-sort-b", "Zulu")
    node_a = _sample_func("pg-sort-a", "Alpha")
    result = store.merge(
        GraphData(
            nodes=[node_b, node_a],
            edges=[
                GraphEdge("pg-sort-b", "pg-sort-a", "REFERENCES"),
                GraphEdge("pg-sort-a", "pg-sort-b", "REFERENCES"),
            ],
        )
    )

    lock_ids = [
        params[-1]
        for sql, params in conn._cursor.executed
        if "FROM memplex_functions" in sql and "id = %s FOR UPDATE" in sql
    ]
    edge_inserts = [
        params[:3]
        for sql, params in conn._cursor.executed
        if "INSERT INTO memplex_edges" in sql
    ]
    assert result.new_functions == 2
    assert lock_ids == ["pg-sort-a", "pg-sort-b"]
    assert edge_inserts == [
        ("pg-sort-a", "pg-sort-b", "REFERENCES"),
        ("pg-sort-b", "pg-sort-a", "REFERENCES"),
    ]


def test_merge_prelocks_external_reverse_edge_endpoints_by_id(pg_store):
    """Existing A/B endpoints are locked once in canonical ID order."""
    from memplex.models import GraphData, GraphEdge

    store, conn = pg_store
    node_a = _sample_func("pg-external-a", "Alpha")
    node_b = _sample_func("pg-external-b", "Beta")
    identity = store._row_identity_values(store._authorization_context(), node_a)

    def locked(_cur, func_id, _context):
        return (node_a if func_id == node_a.id else node_b, identity)

    # Keep the real SQL method so the assertion observes FOR UPDATE ordering.
    # The cursor only needs to return the durable representation for the two
    # endpoint locks; its generic mock returns None for graph edge existence.
    responses = {
        node_a.id: (node_a.id, json.dumps(_func_to_json(node_a))),
        node_b.id: (node_b.id, json.dumps(_func_to_json(node_b))),
    }
    original_fetchone = conn._cursor.fetchone

    def fetchone():
        sql, params = conn._cursor.executed[-1]
        if "FROM memplex_functions" in sql and "id = %s FOR UPDATE" in sql:
            return responses.get(params[-1])
        return original_fetchone()

    conn._cursor.fetchone = fetchone
    result = store.merge(
        GraphData(
            nodes=[],
            edges=[
                GraphEdge(node_b.id, node_a.id, "REFERENCES"),
                GraphEdge(node_a.id, node_b.id, "REFERENCES"),
            ],
        )
    )
    lock_ids = [
        params[-1]
        for sql, params in conn._cursor.executed
        if "FROM memplex_functions" in sql and "id = %s FOR UPDATE" in sql
    ]
    assert result.new_edges == 2
    assert lock_ids == [node_a.id, node_b.id]


# ── Regression: changelog must be written, not just read ─────────────


def test_add_writes_created_changelog_entry(pg_store):
    store, conn = pg_store
    store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    entries = [
        (s, p) for s, p in conn._cursor.executed if "INSERT INTO memplex_changelog" in s
    ]
    assert len(entries) == 1
    params = entries[0][1]
    assert params[0] == "pg-1"  # func_id
    assert params[2] == "created"  # event_type


def test_delete_writes_changelog_entry(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = ("pg-1", json.dumps(_func_to_json(_sample_func())))
    store.delete("pg-1")
    entries = [
        (s, p) for s, p in conn._cursor.executed if "INSERT INTO memplex_changelog" in s
    ]
    assert len(entries) >= 1
    assert any(row[1][2] == "deleted" for row in entries)
    assert entries[0][1][2] == "deleted"


# ── Regression: add() must merge by name_normalized (base contract) ──


def test_add_merges_into_existing_same_name_function(pg_store):
    store, conn = pg_store
    existing = _sample_func("pg-existing", "login")
    existing.trigger = [FieldValue(desc="old trigger", sources=["t"], source_method="manual")]
    existing.version = 3
    # The name-lookup SELECT finds the existing row.
    conn._cursor._fetchone_val = ("pg-existing", json.dumps(_func_to_json(existing)))

    incoming = _sample_func("pg-new-id", "login")
    incoming.trigger = [FieldValue(desc="new trigger", sources=["t"], source_method="manual")]
    store.add(incoming, SourceDocument(type="text", source_type=SourceType.WIKI))

    upserts = [
        (s, p) for s, p in conn._cursor.executed if "INSERT INTO memplex_functions" in s
    ]
    assert len(upserts) == 1
    params = upserts[0][1]
    assert params[0] == "pg-existing"  # merged into the stored row, not the new id
    data = json.loads(params[1])
    descs = {fv["desc"] for fv in data["trigger"]}
    assert descs == {"old trigger", "new trigger"}
    assert data["version"] == 4  # version bumped on merge

    entries = [
        (s, p) for s, p in conn._cursor.executed if "INSERT INTO memplex_changelog" in s
    ]
    assert entries[0][1][0] == "pg-existing"
    assert entries[0][1][2] == "updated"


# ── Regression: add_batch follows the base signature ─────────────────


def test_add_batch_returns_batch_result(pg_store):
    from memplex.models import BatchResult

    store, conn = pg_store
    funcs = [_sample_func("pg-b1", "alpha"), _sample_func("pg-b2", "beta")]
    sources = [
        SourceDocument(type="text", source_type=SourceType.WIKI),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    ]
    result = store.add_batch(funcs, sources)
    assert isinstance(result, BatchResult)
    assert result.total == 2
    assert result.succeeded == 2
    assert result.failed_items == []


def test_add_batch_isolates_single_item_failure(pg_store, monkeypatch):
    store, _ = pg_store
    calls = []

    def flaky_add(func, source):
        calls.append(func.id)
        if func.id == "pg-bad":
            raise RuntimeError("boom")

    monkeypatch.setattr(store, "add", flaky_add)
    funcs = [_sample_func("pg-ok", "ok"), _sample_func("pg-bad", "bad")]
    sources = [SourceDocument(type="text", source_type=SourceType.WIKI)] * 2
    result = store.add_batch(funcs, sources)
    assert result.succeeded == 1
    assert len(result.failed_items) == 1
    assert result.failed_items[0]["func_id"] == "pg-bad"
    assert "boom" in result.failed_items[0]["error"]
    assert calls == ["pg-ok", "pg-bad"]  # failure did not abort the batch


def test_add_batch_writes_embeddings_via_add(monkeypatch):
    store = PostgresMemoryStore(
        dsn="dbname=fake", ready_pool=_test_ready_pool(dim=4), embedder=_StubEmbedder(4)
    )
    mock_conn = store._pool_manager._pool.connection
    store.add_batch(
        [_sample_func("pg-be", "embedme")],
        [SourceDocument(type="text", source_type=SourceType.WIKI)],
    )
    upserts = [s for s, _ in mock_conn._cursor.executed if "INSERT INTO memplex_functions" in s]
    assert any("embedding" in s for s in upserts)


# ── Regression: get_neighbors depth/edge_types/bidirectional ─────────


def test_get_neighbors_max_hops_zero_returns_empty(pg_store):
    store, conn = pg_store
    assert store.get_neighbors("pg-1", max_hops=0) == []
    assert conn._cursor.executed == []  # no query issued


def test_get_neighbors_zero_limit_returns_empty_without_query(pg_store):
    store, conn = pg_store
    assert store.get_neighbors("pg-1", max_hops=1, limit=0) == []
    assert conn._cursor.executed == []


def test_get_neighbors_sql_is_bidirectional_and_depth_limited(pg_store):
    store, conn = pg_store
    conn._cursor._result = []
    store.get_neighbors("pg-1", max_hops=2)
    sql, params = conn._cursor.executed[-1]
    assert "WITH RECURSIVE" in sql
    # Bidirectional: both directions of each edge are traversed.
    assert "e.source = %s OR e.target = %s" in sql
    # Depth limit + cycle guard.
    assert "h.depth < %s" in sql
    assert "= ANY(h.path)" in sql
    assert params[0] == "pg-1"
    assert 2 in params  # max_hops bound


def test_get_neighbors_edge_types_filter(pg_store):
    store, conn = pg_store
    conn._cursor._result = []
    store.get_neighbors("pg-1", edge_types=["REFERENCES"], max_hops=1)
    sql, params = conn._cursor.executed[-1]
    assert sql.count("edge_type = ANY(%s)") == 2  # anchor + recursive leg
    assert params.count(["REFERENCES"]) == 2


def test_get_neighbors_pushes_limit_before_function_join(pg_store):
    store, conn = pg_store
    conn._cursor._result = []

    store.get_neighbors("pg-1", edge_types=["REFERENCES"], max_hops=1, limit=7)

    sql, params = conn._cursor.executed[-1]
    assert "WITH bounded_neighbors" in sql
    assert "UNION ALL" in sql
    assert sql.index("LIMIT %s") < sql.index("JOIN bounded_neighbors")
    assert params[-1] == 7


# ── Regression: filter() honours every SearchFilters field ───────────


def test_filter_supports_all_search_filters_fields(pg_store):
    from datetime import datetime, timezone

    from memplex.models import SearchFilters

    store, conn = pg_store
    conn._cursor._result = []
    filters = SearchFilters(
        domain=["auth"],
        source_type=[SourceType.CODE],
        confidence_min=0.5,
        updated_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_before=datetime(2026, 2, 1, tzinfo=timezone.utc),
        needs_review=True,
        owner="alice",
    )
    store.filter(filters)
    sql, params = conn._cursor.executed[-1]
    assert "data->>'domain' = ANY(%s)" in sql
    assert "data->>'source_type' = ANY(%s)" in sql
    assert "(data->>'confidence')::float >= %s" in sql
    assert "data->>'updated_at' >= %s" in sql
    assert "data->>'updated_at' <= %s" in sql
    assert "(data->>'needs_review')::boolean = %s" in sql
    assert "data->>'owner' = %s" in sql
    assert ["auth"] in params
    assert [["code"]] == [p for p in params if p == ["code"]]
    assert 0.5 in params
    assert True in params
    assert "alice" in params


def test_filter_no_criteria_selects_all(pg_store):
    from memplex.models import SearchFilters

    store, conn = pg_store
    conn._cursor._result = []
    store.filter(SearchFilters())
    sql, params = conn._cursor.executed[-1]
    assert "WHERE" not in sql
    assert params == ()


# ── Regression: PostgresFeedbackStore is synchronous ─────────────────


def _make_feedback(memory_id="mem-1"):
    from memplex.models import FeedbackVerdict, MemoryFeedback

    return MemoryFeedback(
        memory_id=memory_id,
        field_role="trigger",
        value_index=0,
        verdict=FeedbackVerdict.WRONG,
        reason="outdated",
    )


@pytest.fixture
def pg_feedback_store():
    from memplex.storage.feedback import PostgresFeedbackStore

    mock_conn = _MockConn()
    pool = _PoolMock()
    pool.connection = mock_conn
    store = PostgresFeedbackStore(
        dsn="dbname=fake",
        ready_pool=_test_ready_pool(pool=pool),
    )
    return store, mock_conn


def test_postgres_feedback_store_methods_are_synchronous():
    """The FeedbackStore protocol is synchronous; the old asyncpg-based
    implementation returned coroutines that service.py never awaited."""
    import inspect

    from memplex.storage.feedback import PostgresFeedbackStore

    for name in ("record", "get_pending", "resolve", "get_history", "clear"):
        assert not inspect.iscoroutinefunction(getattr(PostgresFeedbackStore, name)), name


def test_feedback_read_execute_preserves_primary_error_when_cleanup_fails(
    pg_feedback_store, monkeypatch
):
    """Feedback reads must not replace their application error during cleanup."""
    store, _conn = pg_feedback_store

    class _FailingCursor:
        def execute(self, *_args, **_kwargs):
            raise ValueError("feedback read failed")

        def close(self):
            raise RuntimeError("feedback cursor cleanup failed")

    monkeypatch.setattr(
        store._pool_manager,
        "read_cursor",
        lambda *_args, **_kwargs: _FailingCursor(),
    )
    with pytest.raises(ValueError, match="feedback read failed"):
        store.get_history("mem-1")


def test_postgres_feedback_record_inserts(pg_feedback_store):
    store, conn = pg_feedback_store
    result = store.record(_make_feedback())
    assert result is None  # sync call, not a coroutine
    sql, params = conn._cursor.executed[-1]
    assert "INSERT INTO feedback" in sql
    assert params[0] == "mem-1"
    assert params[3] == "wrong"  # verdict serialized to its value
    assert conn.commits == 1


def test_postgres_feedback_get_pending(pg_feedback_store):
    from datetime import datetime

    store, conn = pg_feedback_store
    conn._cursor._result = [("mem-1", "trigger", "user", datetime(2026, 1, 1))]
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0].memory_id == "mem-1"
    assert pending[0].field_role == "trigger"


def test_postgres_feedback_resolve_updates(pg_feedback_store):
    store, conn = pg_feedback_store
    store.resolve("mem-1", "trigger", "accepted new value")
    sql, params = conn._cursor.executed[-1]
    assert "UPDATE feedback" in sql
    assert params == ("accepted new value", "mem-1", "trigger")


def test_postgres_feedback_get_history_maps_rows(pg_feedback_store):
    from datetime import datetime

    store, conn = pg_feedback_store
    conn._cursor._result = [
        (
            "mem-1",
            "trigger",
            0,
            "correct",
            "ok",
            "user",
            datetime(2026, 1, 1),
            None,
            "field_value",
            None,
            None,
            False,
            None,
            None,
            None,
        )
    ]
    history = store.get_history("mem-1")
    assert len(history) == 1
    assert history[0].memory_id == "mem-1"
    assert history[0].verdict.value == "correct"
    assert history[0].needs_review is False


def test_postgres_feedback_clear_deletes(pg_feedback_store):
    store, conn = pg_feedback_store
    store.clear()
    sql = conn._cursor.executed[-1][0]
    assert "DELETE FROM feedback" in sql


@pytest.mark.parametrize("operation", ("record", "resolve", "clear"))
def test_task4_feedback_public_writes_commit_once(pg_feedback_store, operation):
    store, conn = pg_feedback_store
    if operation == "record":
        store.record(_make_feedback("task4-feedback"))
    elif operation == "resolve":
        store.resolve("task4-feedback", "trigger", "accepted")
    else:
        store.clear()
    assert conn.commits == 1


# ── Fact / Preference tables (mock connection) ───────────────────────


def _sample_fact(fid="fact-1"):
    from memplex.models import Fact

    return Fact(
        id=fid,
        name="API fact",
        subject="API",
        predicate="is",
        object_="REST interface",
        domain="arch",
        source_type=SourceType.WIKI,
    )


def _sample_preference(pid="pref-1"):
    from memplex.models import Preference

    return Preference(
        id=pid,
        name="UI theme",
        aspect="theme",
        preference="dark mode",
        subject_id="user-1",
        source_type=SourceType.WIKI,
    )


def test_add_fact_executes_upsert_and_changelog(pg_store):
    store, conn = pg_store
    store.add_fact(_sample_fact())
    sqls = [s for s, _ in conn._cursor.executed]
    upserts = [s for s in sqls if "INSERT INTO memplex_facts" in s]
    assert len(upserts) == 1
    assert "ON CONFLICT" in upserts[0]
    # Changelog entry recorded for the fact id.
    changelogs = [p for s, p in conn._cursor.executed if "INSERT INTO memplex_changelog" in s]
    assert changelogs and changelogs[0][0] == "fact-1"


def test_add_preference_executes_upsert(pg_store):
    store, conn = pg_store
    store.add_preference(_sample_preference())
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("INSERT INTO memplex_preferences" in s for s in sqls)


def test_get_fact_deserializes_row(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = (json.dumps(_sample_fact().to_dict()),)
    got = store.get_fact("fact-1")
    assert got is not None
    assert got.id == "fact-1"
    assert got.object_ == "REST interface"
    sql = conn._cursor.executed[-1][0]
    assert "FROM memplex_facts" in sql


def test_get_fact_returns_none_when_missing(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = None
    assert store.get_fact("missing") is None


def test_get_preference_deserializes_row(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = (json.dumps(_sample_preference().to_dict()),)
    got = store.get_preference("pref-1")
    assert got is not None
    assert got.preference == "dark mode"


def test_get_observation_deserializes_row(pg_store):
    from memplex.models import Observation

    store, conn = pg_store
    observation = Observation(id="obs-1", event="deployed", category="change")
    conn._cursor._fetchone_val = (json.dumps(observation.to_dict()),)

    got = store.get_observation("obs-1")

    assert got is not None
    assert got.id == "obs-1"
    assert got.category == "change"
    assert "FROM memplex_observations" in conn._cursor.executed[-1][0]


def test_list_facts_paginates(pg_store):
    store, conn = pg_store
    conn._cursor._result = [
        (json.dumps(_sample_fact("fact-a").to_dict()),),
        (json.dumps(_sample_fact("fact-b").to_dict()),),
    ]
    facts = store.list_facts(offset=0, limit=10)
    assert [f.id for f in facts] == ["fact-a", "fact-b"]
    sql = conn._cursor.executed[-1][0]
    assert "FROM memplex_facts" in sql and "OFFSET" in sql and "LIMIT" in sql


def test_list_facts_owner_filter(pg_store):
    store, conn = pg_store
    conn._cursor._result = []
    store.list_facts(owner="alice")
    sql, params = conn._cursor.executed[-1]
    assert "data->>'owner'" in sql
    assert params[0] == "alice"


def test_list_preferences_paginates(pg_store):
    store, conn = pg_store
    conn._cursor._result = [(json.dumps(_sample_preference().to_dict()),)]
    prefs = store.list_preferences(offset=0, limit=10)
    assert len(prefs) == 1
    assert prefs[0].subject_id == "user-1"


def test_delete_fact_executes_delete_and_changelog(pg_store):
    store, conn = pg_store
    store.delete_fact("fact-1")
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_facts" in s for s in sqls)
    assert any("INSERT INTO memplex_changelog" in s for s in sqls)


def test_delete_preference_executes_delete(pg_store):
    store, conn = pg_store
    store.delete_preference("pref-1")
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_preferences" in s for s in sqls)


def test_delete_observation_executes_delete(pg_store):
    store, conn = pg_store
    store.delete_observation("obs-1")
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_observations" in s for s in sqls)


def test_clear_deletes_fact_and_preference_tables(pg_store):
    store, conn = pg_store
    store.clear()
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_facts" in s for s in sqls)
    assert any("DELETE FROM memplex_preferences" in s for s in sqls)


@pytest.mark.parametrize(
    "operation",
    (
        "delete",
        "clear",
        "merge",
        "add_fact",
        "add_preference",
        "add_observation",
        "delete_fact",
        "delete_preference",
        "delete_observation",
    ),
)
def test_task4_public_writes_commit_once(pg_store, operation):
    """Every Task 4 public write owns exactly one lease/commit boundary."""
    store, conn = pg_store
    operations = {
        "delete": lambda: store.delete("task4-function"),
        "clear": store.clear,
        "merge": lambda: store.merge(_make_graph()),
        "add_fact": lambda: store.add_fact(_sample_fact("task4-fact")),
        "add_preference": lambda: store.add_preference(
            _sample_preference("task4-preference")
        ),
        "delete_fact": lambda: store.delete_fact("task4-fact"),
        "delete_preference": lambda: store.delete_preference("task4-preference"),
        "delete_observation": lambda: store.delete_observation("task4-observation"),
        "add_observation": lambda: store.add_observation(
            Observation(id="task4-observation", name="o", domain="ops", event="e")
        ),
    }
    operations[operation]()
    assert conn.commits == 1


def test_fact_json_roundtrip_via_model():
    """Fact.to_dict output is JSONB-safe and round-trips via from_dict."""
    f = _sample_fact()
    s = json.dumps(f.to_dict())
    restored = type(f).from_dict(json.loads(s))
    assert restored.id == f.id
    assert restored.subject == f.subject
    assert restored.object_ == f.object_


def test_postgres_merge_field_values_enforces_max_values_per_field():
    """Postgres merge mirrors the lite cap (Function.MAX_VALUES_PER_FIELD)."""
    from memplex.models import FieldValue, Function
    from memplex.storage.postgres import _merge_field_values

    existing = [FieldValue(desc=f"existing-{i}") for i in range(Function.MAX_VALUES_PER_FIELD)]
    incoming = [FieldValue(desc=f"incoming-{i}") for i in range(5)]
    merged = _merge_field_values(existing, incoming)
    assert len(merged) == Function.MAX_VALUES_PER_FIELD
    assert merged[-1].desc == f"existing-{Function.MAX_VALUES_PER_FIELD - 1}"
