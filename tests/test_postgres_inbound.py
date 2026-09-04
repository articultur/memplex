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
        self.expected_target = expected_target
        self.expected_application_principal = expected_application_principal
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
