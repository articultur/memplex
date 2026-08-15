"""Lockstep contract test for the two sync repository backends.

``LiteSyncRepository`` and ``PostgresSyncRepository`` are kept in lockstep by
hand. Both now inherit ``AbstractSyncRepository``; this test pins that neither
backend can silently drop or rename one of the shared atomic sync operations.
A backend that forgets a method fails ABC instantiation, and this test makes
that failure explicit in CI rather than surfacing only at runtime.
"""

import copy
import inspect
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.auth import AuthorizationContext, Principal  # noqa: E402
from memplex.storage.lite.store import LiteMemoryStore  # noqa: E402
from memplex.storage.lite.sync_repository import LiteSyncRepository  # noqa: E402
from memplex.storage.postgres_sync import PostgresSyncRepository  # noqa: E402
from memplex.sync_protocol import (  # noqa: E402
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncPage,
    SyncScope,
    SyncStreamItem,
    SyncVersion,
)
from memplex.sync_repository import (  # noqa: E402
    AbstractSyncRepository,
    SyncCapturePolicy,
    SyncRepository,
    validate_incoming_page,
)

# The concrete contract every sync backend must satisfy.
_EXPECTED_METHODS = {
    "sync_page",
    "sync_create_snapshot",
    "sync_snapshot_page",
    "sync_apply_batch",
    "sync_apply_page",
    "sync_register_target",
    "sync_claim",
    "sync_ack",
    "sync_ack_batch",
    "sync_fail",
    "sync_dead_letter",
    "sync_replay_dead_letter",
    "sync_list_dead_letters",
    "sync_set_target_enabled",
    "sync_compact",
    "sync_status",
    "sync_dispatch_status",
}


def _event(index: int, *, tenant_id: str = "tenant-a") -> SyncEvent:
    event_id = str(uuid.UUID(int=index))
    return SyncEvent(
        1,
        event_id,
        "remote-a",
        SyncNodeType.FUNCTION,
        SyncEntityKey.node(f"function-{index}"),
        SyncOperation.UPSERT,
        str(
            SyncVersion.create(
                datetime(2026, 8, 12, tzinfo=timezone.utc),
                "remote-a",
                event_id,
            )
        ),
        SyncScope(tenant_id, "owner-a", None, "user", None, None),
        {"id": f"function-{index}"},
    )


def _page(*events: SyncEvent) -> SyncPage:
    return SyncPage(
        tuple(SyncStreamItem(index + 1, event) for index, event in enumerate(events)),
        len(events),
        len(events),
        False,
    )


def _public_sync_methods(repository_type: type) -> dict[str, object]:
    return {
        name: member
        for name, member in repository_type.__dict__.items()
        if name.startswith("sync_") and callable(member)
    }


def _lite_repository(tmp_path: Path) -> LiteSyncRepository:
    store = LiteMemoryStore(
        path=tmp_path / "memory.json",
        sync_capture_policy=SyncCapturePolicy("required", "lite-local"),
    )
    repository = store._sync_repository
    assert isinstance(repository, LiteSyncRepository)
    return repository


class _PostgresTransactionProbe:
    def __init__(self) -> None:
        self.transaction_entries = 0

    @contextmanager
    def transaction(self, bind_scope, context) -> Iterator[tuple[None, None]]:
        del bind_scope, context
        self.transaction_entries += 1
        yield None, None


class _PostgresPreflightStore:
    def __init__(self) -> None:
        self._pool_manager = _PostgresTransactionProbe()
        self._sync_capture_policy = SyncCapturePolicy("required", "postgres-local")

    @staticmethod
    def _authorization_context() -> AuthorizationContext:
        return AuthorizationContext(
            principal=Principal(
                tenant_id="tenant-a",
                subject_id="subject-a",
                roles=frozenset({"member"}),
                authentication_id="credential-a",
            ),
            workspace_id="workspace-a",
            request_id="request-a",
        )

    @staticmethod
    def _bind_transaction_scope(cursor, context) -> None:
        del cursor, context


def test_abstract_repository_declares_the_full_contract():
    assert _EXPECTED_METHODS == AbstractSyncRepository.__abstractmethods__


@pytest.mark.parametrize(
    "implementation",
    [AbstractSyncRepository, LiteSyncRepository, PostgresSyncRepository],
)
def test_every_public_repository_method_has_the_exact_protocol_signature(
    implementation: type,
) -> None:
    protocol_methods = _public_sync_methods(SyncRepository)

    assert set(protocol_methods) == _EXPECTED_METHODS
    assert set(_public_sync_methods(implementation)) == _EXPECTED_METHODS
    for method_name, protocol_method in protocol_methods.items():
        assert inspect.signature(getattr(implementation, method_name)) == inspect.signature(
            protocol_method
        ), method_name


def test_lite_repository_is_a_concrete_sync_repository():
    assert issubclass(LiteSyncRepository, AbstractSyncRepository)
    assert issubclass(LiteSyncRepository, SyncRepository)  # runtime-checkable Protocol
    # No remaining abstract methods ⇒ the backend implements every operation.
    assert LiteSyncRepository.__abstractmethods__ == frozenset()


def test_postgres_repository_is_a_concrete_sync_repository():
    assert issubclass(PostgresSyncRepository, AbstractSyncRepository)
    assert issubclass(PostgresSyncRepository, SyncRepository)
    assert PostgresSyncRepository.__abstractmethods__ == frozenset()


def test_lite_rejects_an_invalid_page_before_opening_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _lite_repository(tmp_path)
    page = _page(_event(1), _event(2, tenant_id="tenant-b"))
    mutation_entries = 0

    @contextmanager
    def counted_mutation() -> Iterator[None]:
        nonlocal mutation_entries
        mutation_entries += 1
        yield

    monkeypatch.setattr(repository, "_mutation", counted_mutation)
    before = copy.deepcopy(repository._state)

    with pytest.raises(ValueError, match="tenant"):
        repository.sync_apply_page("remote-a", page)

    assert mutation_entries == 0
    assert repository._state == before
    assert not (tmp_path / "memory.json").exists()


def test_postgres_rejects_an_invalid_page_before_opening_transaction() -> None:
    store = _PostgresPreflightStore()
    repository = PostgresSyncRepository(store)
    page = _page(_event(1), _event(2, tenant_id="tenant-b"))

    with pytest.raises(ValueError, match="tenant"):
        repository.sync_apply_page("remote-a", page)

    assert store._pool_manager.transaction_entries == 0


def test_real_postgres_contract_environment_is_explicit(pg_server_dsn: str) -> None:
    """Keep real-PostgreSQL availability visible instead of claiming fake-DB proof."""
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    connection = psycopg2.connect(pg_server_dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT current_setting('server_version_num')::int")
            assert cursor.fetchone()[0] >= 120000
        finally:
            cursor.close()
    finally:
        connection.close()


def test_validate_incoming_page_returns_events_for_one_tenant():
    first = _event(1)
    second = _event(2)

    assert validate_incoming_page(_page(first, second), tenant_id="tenant-a") == (
        first,
        second,
    )


def test_validate_incoming_page_rejects_cross_tenant_before_storage():
    with pytest.raises(ValueError, match="tenant"):
        validate_incoming_page(
            _page(_event(1), _event(2, tenant_id="tenant-b")),
            tenant_id="tenant-a",
        )


def test_validate_incoming_page_rejects_duplicate_origin_and_event_id():
    event = _event(1)
    with pytest.raises(ValueError, match="duplicate"):
        validate_incoming_page(_page(event, event), tenant_id="tenant-a")


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("snapshot_seq", 1),
        ("next_after_seq", 1),
        ("next_after_seq", 3),
    ],
)
def test_validate_incoming_page_rejects_forged_sequence_continuation(
    attribute: str, value: int
):
    page = _page(_event(1), _event(2))
    if value == 3:
        object.__setattr__(page, "has_more", True)
    object.__setattr__(page, attribute, value)

    with pytest.raises(ValueError, match="(?:stream items|cursor|continue)"):
        validate_incoming_page(page, tenant_id="tenant-a")
