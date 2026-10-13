"""Verified inbound executor unit surface: exact type gate + SQL/parse contract."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timezone
from typing import ClassVar

import pytest

import memplex.storage.pool as _pool_module
from memplex.storage.inbound import InboundSyncExecutor
from memplex.storage.migrations import (
    IngressAclContract,
    MigrationIntegrityError,
    PostgresApplicationPrincipal,
    PostgresTargetIdentity,
)
from memplex.storage.migrations.runner import VectorCapabilityRequest, VectorCapabilityStatus
from memplex.sync_ingress import ValidatedIngressBatch, validate_ingress_batch
from memplex.sync_protocol import (
    SyncBatch,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncScope,
    SyncVersion,
)


def _batch() -> SyncBatch:
    event_id = "123e4567-e89b-42d3-a456-426614174401"
    origin = "remote-inbound"
    event = SyncEvent(
        1,
        event_id,
        origin,
        SyncNodeType.FUNCTION,
        SyncEntityKey.node("fn-inbound"),
        SyncOperation.UPSERT,
        str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=UTC), origin, event_id)),
        SyncScope("tenant", "owner", "workspace", "user", None, None),
        {"id": "fn-inbound"},
    )
    return SyncBatch(
        1,
        "123e4567-e89b-42d3-a456-426614174402",
        origin,
        (event,),
    )


def _sample_raw() -> tuple[bytes, str]:
    event = _batch()
    return event.canonical_bytes, event.request_digest


def _two_event_batch() -> tuple[bytes, str]:
    first = _batch().events[0]
    second = SyncEvent(
        1,
        "123e4567-e89b-42d3-a456-426614174403",
        "remote-inbound",
        SyncNodeType.FUNCTION,
        SyncEntityKey.node("fn-inbound-2"),
        SyncOperation.UPSERT,
        str(
            SyncVersion.create(
                datetime(2026, 8, 12, tzinfo=UTC),
                "remote-inbound",
                "123e4567-e89b-42d3-a456-426614174403",
            )
        ),
        SyncScope("tenant", "owner", "workspace", "user", None, None),
        {"id": "fn-inbound-2"},
    )
    composite = SyncBatch(
        1,
        "123e4567-e89b-42d3-a456-426614174404",
        "remote-inbound",
        (first, second),
    )
    return composite.canonical_bytes, composite.request_digest


class _MockCursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


class _MockTransaction:
    def __init__(self, row):
        self.connection = _MockConnection()
        self.cursor = _MockCursor(row)
        self.cursor_entered = 0
        self.cursor_exited = 0

    def __enter__(self):
        self.cursor_entered += 1
        return self.connection, self.cursor

    def __exit__(self, exc_type, exc, tb):
        self.cursor_exited += 1
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()


class _MockConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _MockManager:
    def __init__(self, row):
        self.tx = _MockTransaction(row)
        self.calls = 0

    def transaction(self):
        self.calls += 1
        return self.tx


def _result_payload(
    *, batch: ValidatedIngressBatch, outcomes: tuple[str, ...] | None = None
) -> dict[str, object]:
    outcomes = outcomes or tuple("accepted" for _ in batch.batch.events)
    if len(outcomes) != len(batch.batch.events):
        raise AssertionError("outcomes must align with event count")

    return {
        "accepted": sum(item == "accepted" for item in outcomes),
        "duplicate": sum(item == "duplicate" for item in outcomes),
        "conflict": sum(item == "rejected_conflict" for item in outcomes),
        "receipts": [
            {
                "event_id": batch.batch.events[idx].event_id,
                "outcome": outcomes[idx],
                **({"stream_seq": idx + 1} if outcomes[idx] == "accepted" else {}),
            }
            for idx in range(len(outcomes))
        ],
    }


def _result_row(payload_or_row: object):
    if type(payload_or_row) is tuple:
        return payload_or_row
    return (payload_or_row,)


def test_apply_accepts_only_exact_validated_ingress_batch() -> None:
    batch = validate_ingress_batch(*_sample_raw())
    manager = _MockManager(_result_row(_result_payload(batch=batch)))
    executor = InboundSyncExecutor(manager)
    assert executor.apply(batch).outcome == "accepted"

    other = batch.canonical_bytes
    with pytest.raises(TypeError):
        InboundSyncExecutor(manager).apply(other)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        InboundSyncExecutor(manager).apply(batch.batch)  # type: ignore[arg-type]

    class _DuckBatch:
        canonical_bytes = batch.canonical_bytes
        request_digest = batch.request_digest

    with pytest.raises(TypeError):
        InboundSyncExecutor(manager).apply(_DuckBatch())  # type: ignore[arg-type]

    class _SubBatch(ValidatedIngressBatch):
        pass

    sub = object.__new__(_SubBatch)
    object.__setattr__(sub, "batch", batch.batch)
    object.__setattr__(sub, "canonical_bytes", batch.canonical_bytes)
    object.__setattr__(sub, "request_digest", batch.request_digest)
    with pytest.raises(TypeError):
        InboundSyncExecutor(manager).apply(sub)  # type: ignore[arg-type]


def test_apply_executes_exact_sql_once_with_verified_bytes_and_digest() -> None:
    batch = validate_ingress_batch(*_sample_raw())
    manager = _MockManager(_result_row(_result_payload(batch=batch)))
    executor = InboundSyncExecutor(manager)

    result = executor.apply(batch)

    assert result.batch_id == batch.batch.batch_id
    assert result.request_digest == batch.request_digest
    assert result.outcome == "accepted"
    assert manager.tx.cursor.calls == [
        ("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest)),
    ]
    assert manager.tx.cursor_entered == 1
    assert manager.tx.cursor_exited == 1
    assert manager.tx.connection.commits == 1
    assert manager.tx.connection.rollbacks == 0


def test_apply_calls_callable_transaction_context() -> None:
    batch = validate_ingress_batch(*_sample_raw())
    tx = _MockTransaction(_result_row(_result_payload(batch=batch)))

    def factory():
        return tx

    executor = InboundSyncExecutor(factory)
    executor.apply(batch)
    assert tx.cursor_entered == 1
    assert tx.cursor_exited == 1
    assert tx.connection.commits == 1
    assert tx.connection.rollbacks == 0


def test_apply_rollback_when_row_shape_is_invalid() -> None:
    batch = validate_ingress_batch(*_sample_raw())
    manager = _MockManager("not-a-row")
    executor = InboundSyncExecutor(manager)

    with pytest.raises(TypeError):
        executor.apply(batch)

    assert manager.tx.connection.commits == 0
    assert manager.tx.connection.rollbacks == 1


def test_apply_fail_closed_on_unknown_or_weak_shape() -> None:
    batch = validate_ingress_batch(*_sample_raw())

    missing_key = _result_payload(batch=batch)
    missing_key.pop("receipts")
    with pytest.raises(ValueError):
        InboundSyncExecutor(_MockManager(_result_row(missing_key))).apply(batch)

    weak_receipts = _result_payload(batch=batch)
    weak_receipts["receipts"] = tuple(weak_receipts["receipts"])
    with pytest.raises(TypeError):
        InboundSyncExecutor(_MockManager(_result_row(weak_receipts))).apply(batch)


def test_apply_rejects_out_of_order_or_count_mismatch_receipts() -> None:
    batch = validate_ingress_batch(*_two_event_batch())
    # duplicate receipt order check is explicit and exact.
    payload = _result_payload(
        batch=batch,
        outcomes=("duplicate", "accepted"),
    )
    payload["accepted"] = 0
    payload["duplicate"] = 1
    payload["conflict"] = 0
    payload["receipts"] = [
        {"event_id": batch.batch.events[1].event_id, "outcome": "duplicate"},
        {"event_id": batch.batch.events[0].event_id, "outcome": "accepted", "stream_seq": 2},
    ]
    with pytest.raises(ValueError):
        InboundSyncExecutor(_MockManager(_result_row(payload))).apply(batch)

    payload2 = {
        **_result_payload(batch=batch),
        "accepted": 0,
        "duplicate": 0,
        "conflict": 0,
    }
    payload2["receipts"] = [{"event_id": batch.batch.events[0].event_id, "outcome": "accepted", "stream_seq": 10}]
    with pytest.raises(ValueError):
        InboundSyncExecutor(_MockManager(_result_row(payload2))).apply(batch)

    payload3 = {
        **_result_payload(batch=batch, outcomes=("accepted", "accepted")),
        "receipts": [
            {"event_id": batch.batch.events[1].event_id, "outcome": "accepted", "stream_seq": 9},
            {"event_id": batch.batch.events[0].event_id, "outcome": "accepted", "stream_seq": 10},
        ],
    }
    with pytest.raises(ValueError):
        InboundSyncExecutor(_MockManager(_result_row(payload3))).apply(batch)


def test_apply_rejects_accepted_receipt_shape_and_rejects_unknown_outcome() -> None:
    batch = validate_ingress_batch(*_sample_raw())

    payload = _result_payload(batch=batch)
    payload["receipts"] = [{"event_id": batch.batch.events[0].event_id, "outcome": "accepted"}]
    with pytest.raises(ValueError):
        InboundSyncExecutor(_MockManager(_result_row(payload))).apply(batch)

    payload = _result_payload(batch=batch)
    payload["receipts"] = [{"event_id": batch.batch.events[0].event_id, "outcome": "unknown"}]
    payload["accepted"] = 1
    payload["duplicate"] = 0
    payload["conflict"] = 0
    with pytest.raises(ValueError):
        InboundSyncExecutor(_MockManager(_result_row(payload))).apply(batch)


def test_apply_rejects_invalid_stream_seq() -> None:
    batch = validate_ingress_batch(*_sample_raw())
    payload = _result_payload(batch=batch)
    payload["receipts"] = [{"event_id": batch.batch.events[0].event_id, "outcome": "accepted", "stream_seq": 0}]
    with pytest.raises(TypeError):
        InboundSyncExecutor(_MockManager(_result_row(payload))).apply(batch)


class _SyncTestRunner:
    def __init__(
        self,
        *,
        target: PostgresTargetIdentity,
        principal: PostgresApplicationPrincipal,
        status: VectorCapabilityStatus,
    ) -> None:
        self.target = target
        self.principal = principal
        self.status = status
        self.calls: list[str] = []

    def inspect_target(self) -> PostgresTargetIdentity:
        self.calls.append("inspect_target")
        return self.target

    def inspect_application_principal(self, **_kwargs) -> PostgresApplicationPrincipal:
        self.calls.append("inspect_application_principal")
        return self.principal

    def apply(self, **_kwargs) -> None:
        self.calls.append("apply")

    def ensure_vector_capability(self, *args, **_kwargs) -> VectorCapabilityStatus:
        self.calls.append("ensure_vector_capability")
        return self.status

    def verify_storage_readiness(
        self,
        _request: VectorCapabilityRequest,
        _profile: str,
        **_kwargs,
    ) -> VectorCapabilityStatus:
        self.calls.append("verify_storage_readiness")
        return self.status


class _SyncTestPoolManager:
    init_calls: ClassVar[list[object]] = []
    close_calls: ClassVar[list[str]] = []
    actual_role_by_dsn: ClassVar[dict[str, PostgresApplicationPrincipal]] = {}
    init_blocker: threading.Event | None = None
    init_started: threading.Event | None = None

    def __init__(
        self,
        dsn: str,
        *,
        expected_target: PostgresTargetIdentity | None = None,
        expected_application_principal: PostgresApplicationPrincipal | None = None,
        **_kwargs,
    ) -> None:
        self.dsn = dsn
        self.expected_target = self._expected_target = expected_target
        self.expected_application_principal = self._expected_application_principal = expected_application_principal
        self.transaction_calls = 0
        self._on_closed = _kwargs.get("on_closed")
        self._on_fault = _kwargs.get("on_fault")
        init_kwargs = dict(_kwargs)
        if "on_closed" in init_kwargs:
            init_kwargs["on_closed"] = self._on_closed is not None
        if "on_fault" in init_kwargs:
            init_kwargs["on_fault"] = self._on_fault is not None
        _SyncTestPoolManager.init_calls.append(init_kwargs)
        if _SyncTestPoolManager.init_started is not None:
            _SyncTestPoolManager.init_started.set()
        if _SyncTestPoolManager.init_blocker is not None:
            assert _SyncTestPoolManager.init_blocker.wait(timeout=1)

    def verify_target(self, expected_target: PostgresTargetIdentity) -> None:
        if self.expected_target is not None and self.expected_target != expected_target:
            raise MigrationIntegrityError("PostgreSQL pool target identity does not match expected target")

    def verify_application_access(self, **_kwargs) -> None:
        return None

    def inspect_application_role(self) -> PostgresApplicationPrincipal:
        if self.dsn not in _SyncTestPoolManager.actual_role_by_dsn:
            return (
                self.expected_application_principal
                if self.expected_application_principal is not None
                else PostgresApplicationPrincipal("app", "app")
            )
        return _SyncTestPoolManager.actual_role_by_dsn[self.dsn]

    def transaction(self, *_args, **_kwargs):
        self.transaction_calls += 1
        return _MockTransaction(_result_row({"accepted": 1, "duplicate": 0, "conflict": 0, "receipts": []}))

    def close(self, wait: bool = True) -> bool:
        _SyncTestPoolManager.close_calls.append(f"close:{self.dsn}")
        if self._on_closed is not None:
            self._on_closed(None if wait else RuntimeError("close requested"))
        return True

    def trigger_fault(self, error: BaseException) -> None:
        if self._on_fault is not None:
            self._on_fault(error)


def _install_runner_sequence(monkeypatch, runners: list[_SyncTestRunner]) -> None:
    def factory(_dsn: str) -> _SyncTestRunner:
        if not runners:
            raise AssertionError("unexpected migration-runner allocation")
        return runners.pop(0)

    monkeypatch.setattr(_pool_module, "_new_migration_runner", factory)


def test_sync_resources_rejects_distinct_dsn_requirement() -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    with pytest.raises(ValueError, match="distinct"):
        PostgresSyncStorageResources(
            app_dsn="postgresql://app",
            migration_dsn="postgresql://app",
            inbound_dsn="postgresql://inbound",
        )


def test_sync_resources_status_rejects_non_ready_state() -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )

    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.status


def _make_sync_ready_runners(shared_target: PostgresTargetIdentity) -> tuple[
    _SyncTestRunner,
    _SyncTestRunner,
    _SyncTestRunner,
    _SyncTestRunner,
]:
    in_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("principal", "principal"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    app_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    migration_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    verify_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    return in_runner, app_runner, migration_runner, verify_runner


def test_sync_resources_ready_properties_return_without_fault(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner, app_runner, migration_runner, verify_runner = _make_sync_ready_runners(
        shared_target,
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal(
        "app",
        "app",
    )

    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    status = resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    assert status.state == "disabled"

    assert resources.state == "READY"
    assert resources.ready_pool is not None
    assert resources.executor is not None
    assert resources.status is status
    assert _SyncTestPoolManager.close_calls == []


def test_sync_resources_no_partial_publish_when_targets_mismatch(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    app_target = PostgresTargetIdentity("app-db", "public", "127.0.0.1", 5432)
    inbound_target = PostgresTargetIdentity("inbound-db", "public", "127.0.0.1", 5432)
    in_runner = _SyncTestRunner(
        target=inbound_target,
        principal=PostgresApplicationPrincipal("inbound", "inbound"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    app_runner = _SyncTestRunner(
        target=app_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    migration_runner = _SyncTestRunner(
        target=app_target,
        principal=PostgresApplicationPrincipal("app-role", "app-session"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    verify_runner = _SyncTestRunner(
        target=app_target,
        principal=PostgresApplicationPrincipal("app-role", "app-session"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal(
        "app",
        "app",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "inbound",
        "inbound",
    )

    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    with pytest.raises(MigrationIntegrityError, match="match application target"):
        resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")

    assert resources.state == "FAULTED"
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.ready_pool
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.executor
    assert _SyncTestPoolManager.close_calls == [
        "close:postgresql://app",
    ]


def test_sync_resources_closes_inbound_then_app_when_second_phase_fails(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    expected_principal = PostgresApplicationPrincipal("expected", "expected")
    actual_principal = PostgresApplicationPrincipal("actual", "actual")
    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)

    in_runner = _SyncTestRunner(
        target=shared_target,
        principal=expected_principal,
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    app_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    migration_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    verify_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    observed_ingress_roles: list[str] = []

    original_resources = _pool_module.PostgresStorageResources

    class _ObservedStorageResources(original_resources):
        def __init__(self, *_args, **_kwargs):
            ingress_acl = _kwargs.get("ingress_acl")
            if ingress_acl is not None:
                observed_ingress_roles.append(ingress_acl.role)
            super().__init__(* _args, **_kwargs)

    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = actual_principal
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal("app", "app")

    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)
    monkeypatch.setattr(_pool_module, "PostgresStorageResources", _ObservedStorageResources)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    with pytest.raises(MigrationIntegrityError, match="principal"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
        )

    assert resources.state == "FAULTED"
    assert observed_ingress_roles == ["expected"]
    assert _SyncTestPoolManager.close_calls == [
        "close:postgresql://inbound",
        "close:postgresql://app",
    ]


def test_sync_resources_rejects_optional_ingress_acl_argument() -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    with pytest.raises(TypeError, match="unexpected"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
            ingress_acl=IngressAclContract("expected"),  # type: ignore[arg-type]
        )


def test_sync_resources_fault_callback_revoke_ready_and_close_peer(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("principal", "principal"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    app_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    migration_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    verify_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal("app", "app")
    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    inbound_manager = resources._inbound_manager
    assert inbound_manager is not None
    inbound_manager.trigger_fault(RuntimeError("test fault"))

    assert resources.state == "FAULTED"
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.ready_pool
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.executor


def test_sync_resources_app_resource_faults_revoke_coordinator_and_close_inbound(
    monkeypatch,
) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner, app_runner, migration_runner, verify_runner = _make_sync_ready_runners(
        shared_target,
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal(
        "app",
        "app",
    )
    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")

    app_resources = resources._app_resources
    assert app_resources is not None
    app_resources.close()

    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.ready_pool
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.executor
    assert resources.state == "FAULTED"
    assert "close:postgresql://inbound" in _SyncTestPoolManager.close_calls


def test_sync_resources_held_executor_rechecks_app_authority_before_transaction(
    monkeypatch,
) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner, app_runner, migration_runner, verify_runner = _make_sync_ready_runners(
        shared_target,
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = (
        PostgresApplicationPrincipal("principal", "principal")
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = (
        PostgresApplicationPrincipal("app", "app")
    )
    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    resources.ensure_ready(
        VectorCapabilityRequest(dim=0, policy="disabled"),
        "development",
    )
    executor = resources.executor
    inbound_manager = resources._inbound_manager
    app_resources = resources._app_resources
    assert inbound_manager is not None
    assert app_resources is not None

    app_resources.close()
    batch = validate_ingress_batch(*_sample_raw())
    with pytest.raises(RuntimeError, match="not ready"):
        executor.apply(batch)

    assert inbound_manager.transaction_calls == 0
    assert resources.state == "FAULTED"


def test_sync_resources_close_faulted_state_does_not_reclose_inbound(
    monkeypatch,
) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner, app_runner, migration_runner, verify_runner = _make_sync_ready_runners(
        shared_target,
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal("app", "app")
    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")

    app_resources = resources._app_resources
    assert app_resources is not None
    app_resources.close()
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.status
    assert resources.state == "FAULTED"
    _SyncTestPoolManager.close_calls.clear()

    with pytest.raises(RuntimeError, match="faulted"):
        resources.close()
    assert _SyncTestPoolManager.close_calls == []

def test_sync_resources_close_wait_false_returns_nonblocking(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner, app_runner, migration_runner, verify_runner = _make_sync_ready_runners(
        shared_target,
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal("app", "app")

    class _NoCallbackManager(_SyncTestPoolManager):
        def close(self, wait: bool = True) -> bool:
            _SyncTestPoolManager.close_calls.append(f"close:{self.dsn}")
            return wait

    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _NoCallbackManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")

    assert resources.close(wait=False) is False
    assert resources.state == "CLOSING"
    assert resources.close(wait=True) is True
    assert resources.state == "CLOSED"


def test_sync_resources_inbound_close_callback_closes_app_resources_wait_false(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    original_resources = _pool_module.PostgresStorageResources
    close_calls: list[bool] = []

    class _ObservedStorageResources(original_resources):
        def close(self, wait: bool = True):
            close_calls.append(wait)
            return super().close(wait=wait)

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner, app_runner, migration_runner, verify_runner = _make_sync_ready_runners(
        shared_target,
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal("app", "app")

    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)
    monkeypatch.setattr(_pool_module, "PostgresStorageResources", _ObservedStorageResources)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")

    inbound_manager = resources._inbound_manager
    assert inbound_manager is not None
    _SyncTestPoolManager.close_calls.clear()
    inbound_manager.close(wait=False)

    assert resources.state == "FAULTED"
    assert close_calls == [False]
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.ready_pool
    assert set(_SyncTestPoolManager.close_calls) == {
        "close:postgresql://inbound",
        "close:postgresql://app",
    }


def test_sync_resources_property_refresh_rejects_when_app_resources_faulted(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("principal", "principal"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    app_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    migration_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    verify_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal("app", "app")
    _SyncTestPoolManager.close_calls.clear()
    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    app_resources = resources._app_resources
    assert app_resources is not None
    app_resources.close()

    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.ready_pool
    with pytest.raises(RuntimeError, match="not ready"):
        _ = resources.status
    assert resources.state == "FAULTED"


def test_sync_resources_init_race_with_close_converges_to_closed(monkeypatch) -> None:
    from memplex.storage.pool import PostgresSyncStorageResources

    shared_target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    in_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("principal", "principal"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    app_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    migration_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    verify_runner = _SyncTestRunner(
        target=shared_target,
        principal=PostgresApplicationPrincipal("app", "app"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    _install_runner_sequence(
        monkeypatch,
        [in_runner, app_runner, migration_runner, verify_runner],
    )
    blocker = threading.Event()
    init_started = threading.Event()
    _SyncTestPoolManager.init_blocker = blocker
    _SyncTestPoolManager.init_started = init_started
    _SyncTestPoolManager.close_calls.clear()
    _SyncTestPoolManager.init_calls.clear()
    _SyncTestPoolManager.actual_role_by_dsn.clear()
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://inbound"] = PostgresApplicationPrincipal(
        "principal",
        "principal",
    )
    _SyncTestPoolManager.actual_role_by_dsn["postgresql://app"] = PostgresApplicationPrincipal("app", "app")
    monkeypatch.setattr(_pool_module, "PostgresPoolManager", _SyncTestPoolManager)

    resources = PostgresSyncStorageResources(
        app_dsn="postgresql://app",
        migration_dsn="postgresql://migration",
        inbound_dsn="postgresql://inbound",
    )
    errors: list[BaseException] = []

    def ensure() -> None:
        try:
            resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
        except BaseException as exc:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
            errors.append(exc)

    worker = threading.Thread(target=ensure)
    worker.start()
    assert init_started.wait(timeout=1)
    assert resources.close(wait=False) is False
    blocker.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert resources.state == "CLOSED"


@pytest.fixture
def fixed_ingress_backend(monkeypatch):
    """Real resource/manager protocol with a driver double, not a native SQL proof."""
    from memplex.storage.pool import PostgresSyncStorageResources

    target = PostgresTargetIdentity("sync-db", "public", "127.0.0.1", 5432)
    _install_runner_sequence(monkeypatch, list(_make_sync_ready_runners(target)))
    pools = []

    class Cursor:
        def __init__(self, connection):
            self.connection = connection
            self.row = None
            self.closed = False

        def execute(self, sql, parameters=()):
            connection = self.connection
            connection.calls.append((sql, parameters))
            connection.status = 2
            if sql == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED":
                connection.isolation = "read committed"
            elif "pg_catalog.current_database()" in sql:
                self.row = ("sync-db", "public", "127.0.0.1", 5432)
            elif sql == "SELECT current_user, session_user":
                self.row = (connection.role, connection.role)
            elif "SELECT current_user, session_user" in sql:
                self.row = (connection.role, connection.role, False, False)
            elif "FROM memplex_source_authorities" in sql:
                raise PermissionError("execute-only ingress cannot select source authorities")
            elif sql == "SELECT memplex_sync_apply_inbound(%s,%s)":
                assert connection.isolation == "read committed"
                self.row = connection.result
            else:
                raise AssertionError(f"unexpected protocol SQL: {sql}")

        def fetchone(self):
            return self.row

        def close(self):
            self.closed = True

    class Connection:
        autocommit = False
        closed = False

        def __init__(self, role):
            self.role = role
            self.status = 0
            self.isolation = "repeatable read"
            self.calls = []
            self.cursors = []
            self.commits = self.rollbacks = 0
            self.result = ({"accepted": 1, "duplicate": 0, "conflict": 0, "receipts": [
                {"event_id": _batch().events[0].event_id, "outcome": "accepted", "stream_seq": 1},
            ]},)

        def get_transaction_status(self):
            return self.status

        def cursor(self):
            cursor = Cursor(self)
            self.cursors.append(cursor)
            return cursor

        def commit(self):
            self.commits += 1
            self.status = 0
            self.isolation = "repeatable read"

        def rollback(self):
            self.rollbacks += 1
            self.status = 0
            self.isolation = "repeatable read"

    class DriverPool:
        def __init__(self, minimum, maximum, dsn):
            self.connections = [Connection("principal" if "inbound" in dsn else "app") for _ in range(2)]
            self.available = list(self.connections)
            self.put_calls = []
            pools.append(self)

        def getconn(self):
            return self.available.pop()

        def putconn(self, connection):
            assert connection not in self.available
            self.put_calls.append(connection)
            self.available.append(connection)

        def closeall(self):
            for connection in self.connections:
                connection.closed = True

    original_init = _pool_module.PostgresPoolManager.__init__

    def initialize_manager(manager, dsn, **kwargs):
        kwargs["pool"] = DriverPool(kwargs.get("min_connections", 1), kwargs.get("max_connections", 8), dsn)
        original_init(manager, dsn, **kwargs)

    monkeypatch.setattr(_pool_module.PostgresPoolManager, "__init__", initialize_manager)
    # The separately tested catalogue/ACL readiness is modeled by the runners.
    monkeypatch.setattr(_pool_module.PostgresPoolManager, "verify_application_access", lambda *args, **kwargs: None)
    resources = PostgresSyncStorageResources("postgresql://app", "postgresql://migration", "postgresql://inbound")
    resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
    driver = pools[-1]
    for connection in driver.connections:
        connection.calls.clear()
    try:
        yield resources, driver
    finally:
        resources.close()


def test_ready_execute_only_ingress_uses_fixed_operation_without_relation_probe(fixed_ingress_backend):
    resources, driver = fixed_ingress_backend
    batch = validate_ingress_batch(*_sample_raw())
    result = resources.executor.apply(batch)
    assert result.receipts[0].outcome == "accepted"
    calls = next(connection.calls for connection in driver.connections if connection.calls)
    assert calls[0][0] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert not any("FROM memplex_source_authorities" in sql for sql, _ in calls)
    assert calls[-1] == ("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest))
    assert resources._inbound_manager.business_lease_count == 0
    assert all(connection.status == 0 for connection in driver.connections)


@pytest.mark.parametrize("interruption", [None, ValueError, KeyboardInterrupt, SystemExit, BaseException])
def test_fixed_ingress_capability_is_spent_and_never_inherited(fixed_ingress_backend, interruption):
    import pickle
    from contextlib import nullcontext

    from memplex.auth import local_development_context
    from memplex.storage.pool import _inbound_transaction

    resources, driver = fixed_ingress_backend
    manager = resources._inbound_manager
    batch = validate_ingress_batch(*_sample_raw())
    transaction = _inbound_transaction(resources._ingress_authority, batch)
    capability = transaction._ingress_admission
    with pytest.raises(TypeError):
        pickle.dumps(capability)
    outer = nullcontext() if interruption is None else pytest.raises(interruption)
    with outer, transaction as (connection, cursor):
        assert capability._consumed and capability._transaction is None and capability._connection is None
        assert not hasattr(cursor, "connection") and not hasattr(cursor, "executemany")
        with pytest.raises(RuntimeError, match="bound operation"):
            cursor.execute("UPDATE memplex_facts SET data='{}'")
        with pytest.raises(RuntimeError, match="bound operation"):
            cursor.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (b"other", batch.request_digest))
        with pytest.raises(PermissionError, match="execute-only"), manager.transaction(lambda *args: None, local_development_context()):
            pytest.fail("ordinary nested transaction inherited ingress admission")
        other = next(item for item in driver.connections if item is not connection)
        assert any("FROM memplex_source_authorities" in sql for sql, _ in other.calls)
        cursor.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest))
        assert cursor.fetchone()[0]["accepted"] == 1
        with pytest.raises(RuntimeError):
            cursor.fetchone()
        with pytest.raises(RuntimeError):
            cursor.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest))
        if interruption is not None:
            raise interruption("cancel the fixed operation")
    assert manager.business_lease_count == 0
    assert all(item.status == 0 for item in driver.connections)
    assert all(cursor.closed for item in driver.connections for cursor in item.cursors)
    with pytest.raises(RuntimeError, match="invalid ingress"), transaction:
        pytest.fail("spent transaction reentered")
    with pytest.raises(PermissionError), manager.transaction(lambda *args: None, local_development_context()):
        pytest.fail("ordinary post-exit transaction inherited ingress admission")
    assert not any(sql.startswith("UPDATE ") for item in driver.connections for sql, _ in item.calls)


@pytest.mark.parametrize("mismatch", ["transaction", "connection", "cursor", "resource", "manager"])
def test_ingress_admission_binds_actual_transaction_and_connection(fixed_ingress_backend, mismatch):
    from memplex.storage.pool import _inbound_transaction

    resources, driver = fixed_ingress_backend
    batch = validate_ingress_batch(*_sample_raw())
    transaction = _inbound_transaction(resources._ingress_authority, batch)
    other = _inbound_transaction(resources._ingress_authority, batch)
    capability = transaction._ingress_admission
    capability.claim(transaction)
    connection = driver.connections[0]
    cursor = connection.cursor()
    transaction._connection, transaction._cursor = connection, cursor
    capability._connection = connection
    authority = resources._ingress_authority
    saved_manager = resources._inbound_manager
    if mismatch == "resource":
        resources._ingress_authority = object()
    if mismatch == "manager":
        resources._inbound_manager = object()
    try:
        with pytest.raises(RuntimeError, match="ingress"):
            capability.consume(other if mismatch == "transaction" else transaction,
                               driver.connections[1] if mismatch == "connection" else connection,
                               connection.cursor() if mismatch == "cursor" else cursor)
        assert capability._consumed and capability._transaction is None and capability._connection is None
        with pytest.raises(RuntimeError):
            capability.consume(transaction, connection, cursor)
    finally:
        resources._ingress_authority = authority
        resources._inbound_manager = saved_manager
        cursor.close()


@pytest.mark.parametrize("interruption", [ValueError, KeyboardInterrupt, SystemExit])
def test_fixed_ingress_setup_cancel_spends_ticket_and_returns_lease(fixed_ingress_backend, monkeypatch, interruption):
    from memplex.storage.pool import _inbound_transaction

    resources, driver = fixed_ingress_backend
    transaction = _inbound_transaction(resources._ingress_authority, validate_ingress_batch(*_sample_raw()))
    connection = driver.available[-1]
    make_cursor = connection.cursor
    def failing_cursor():
        cursor = make_cursor()
        execute = cursor.execute
        def failing_execute(sql, parameters=()):
            if sql == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED":
                raise interruption("setup cancelled")
            return execute(sql, parameters)
        cursor.execute = failing_execute
        return cursor
    monkeypatch.setattr(connection, "cursor", failing_cursor)
    with pytest.raises(interruption), transaction:
        pytest.fail("cancelled setup entered")
    capability = transaction._ingress_admission
    assert capability._claimed and capability._transaction is None and capability._connection is None
    assert resources._inbound_manager.business_lease_count == 0
    assert len(driver.available) == 2
    with pytest.raises(RuntimeError), transaction:
        pytest.fail("cancelled transaction reentered")


def test_fixed_ingress_parse_error_rolls_back_before_commit(fixed_ingress_backend):
    resources, driver = fixed_ingress_backend
    connection = driver.available[-1]
    before = connection.commits
    connection.result = ({"malformed": True},)
    with pytest.raises(ValueError, match="malformed payload"):
        resources.executor.apply(validate_ingress_batch(*_sample_raw()))
    assert connection.commits == before and connection.rollbacks > 0
    assert connection.status == 0 and len(driver.available) == 2


def test_fake_ingress_ticket_and_issuer_cannot_select_fixed_operation(fixed_ingress_backend):
    from types import SimpleNamespace

    from memplex.auth import local_development_context
    from memplex.storage.pool import _IngressOperationAuthority

    resources, driver = fixed_ingress_backend
    with pytest.raises(RuntimeError):
        _IngressOperationAuthority(resources, resources._inbound_manager, issuer=object())
    transaction = resources._inbound_manager.transaction(lambda *args: None, local_development_context())
    transaction._ingress_admission = SimpleNamespace(claim=lambda *args: pytest.fail("forged ticket called"))
    with pytest.raises(RuntimeError), transaction:
        pytest.fail("forged ingress entered")
    assert len(driver.available) == 2


def test_fixed_ingress_revoked_before_claim_spends_without_borrow(fixed_ingress_backend):
    from memplex.storage.pool import _inbound_transaction

    resources, driver = fixed_ingress_backend
    transaction = _inbound_transaction(resources._ingress_authority, validate_ingress_batch(*_sample_raw()))
    resources.close()
    before = [list(connection.calls) for connection in driver.connections]
    with pytest.raises(RuntimeError), transaction:
        pytest.fail("revoked ingress entered")
    assert transaction._ingress_admission._claimed
    assert transaction._ingress_admission._transaction is None
    assert transaction._ingress_admission._connection is None
    assert [connection.calls for connection in driver.connections] == before


def test_fixed_ingress_fixture_needs_no_optional_driver(monkeypatch):
    """Construction and the real admission path work with driver imports denied."""
    import builtins
    import sys

    original_import = builtins.__import__
    original_init = _pool_module.PostgresPoolManager.__init__
    original_verify = _pool_module.PostgresPoolManager.verify_application_access

    def without_driver(name, *args, **kwargs):
        if name == "psycopg2" or name.startswith("psycopg2."):
            raise ModuleNotFoundError("optional psycopg2 unavailable at fixture boundary", name=name)
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as local:
        local.setattr(builtins, "__import__", without_driver)
        for name in tuple(sys.modules):
            if name == "psycopg2" or name.startswith("psycopg2."):
                local.setitem(sys.modules, name, None)
        with pytest.raises(ModuleNotFoundError, match="optional psycopg2 unavailable"):
            __import__("psycopg2.pool")
        fixture = fixed_ingress_backend.__wrapped__(local)
        backend = next(fixture)
        try:
            assert type(backend[0]._inbound_manager) is _pool_module.PostgresPoolManager
            test_ready_execute_only_ingress_uses_fixed_operation_without_relation_probe(backend)
        finally:
            fixture.close()
        assert all(connection.closed for connection in backend[1].connections)
    assert builtins.__import__ is original_import
    assert _pool_module.PostgresPoolManager.__init__ is original_init
    assert _pool_module.PostgresPoolManager.verify_application_access is original_verify
