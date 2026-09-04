"""G014 PostgreSQL durable background-task 的真实数据库测试。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timezone

import pytest

from memplex.models import BackgroundTask, TaskInfo, TaskStatus
from memplex.storage.migrations import PostgresMigrationRunner, discover_migrations
from memplex.storage.migrations.runner import MigrationIntegrityError, VectorCapabilityRequest
from memplex.storage.postgres_resources import PostgresStorageResources
from memplex.storage.postgres_tasks import PostgresTaskRepository
from memplex.worker import BackgroundWorker


@pytest.fixture
def postgres_task_repository(pg_function_dsn: str):
    resources = PostgresStorageResources(pg_function_dsn)
    resources.ensure_ready(
        VectorCapabilityRequest(dim=0, policy="disabled"),
        "development",
    )
    try:
        yield PostgresTaskRepository(ready_pool=resources.ready_pool)
    finally:
        resources.close()


def _task(task_id: str, *, max_retries: int = 1) -> TaskInfo:
    future = datetime(2099, 1, 1, tzinfo=UTC)
    return TaskInfo(
        task_id=task_id,
        task_type=BackgroundTask.BUILD_INDEX,
        status=TaskStatus.PENDING,
        created_at=future,
        payload={"task_id": task_id},
        max_retries=max_retries,
        next_attempt_at=future,
    )


def test_0006_is_packaged_and_catalogue_verified(pg_function_dsn: str) -> None:
    assert [(item.version, item.name) for item in discover_migrations()][-1] == (
        6,
        "background_tasks",
    )

    runner = PostgresMigrationRunner(pg_function_dsn)
    applied = runner.apply()

    assert applied.state == "ready"
    assert applied.current_version == 6
    assert runner.plan().state == "ready"


def test_0006_catalogue_drift_fails_closed(pg_function_dsn: str) -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    runner = PostgresMigrationRunner(pg_function_dsn)
    runner.apply()
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "DROP INDEX memplex_background_tasks_due_idx; "
            "CREATE INDEX memplex_background_tasks_due_idx "
            "ON memplex_background_tasks (task_id) WHERE status='pending'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        runner.plan()


def test_postgres_admission_uses_database_clock(
    postgres_task_repository: PostgresTaskRepository,
) -> None:
    stored = postgres_task_repository.admit_pending(_task("db-clock"), capacity=10)

    assert stored.created_at.year != 2099
    assert stored.next_attempt_at == stored.created_at
    claimed = postgres_task_repository.claim_due(limit=1, lease_seconds=30)
    assert [item.task_id for item in claimed] == ["db-clock"]


def test_background_worker_executes_through_postgres_repository(
    postgres_task_repository: PostgresTaskRepository,
) -> None:
    worker = BackgroundWorker(task_repository=postgres_task_repository)
    worker._dispatch = lambda *_args: {"status": "completed"}

    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {"source": "postgres"})

    assert worker.run_once() is True
    stored = postgres_task_repository.get(task_id)
    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED
    assert stored.result == {"status": "completed"}


def test_claim_due_skips_a_row_locked_by_another_instance(
    pg_function_dsn: str,
    postgres_task_repository: PostgresTaskRepository,
) -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    postgres_task_repository.admit_pending(_task("a-locked"), capacity=10)
    postgres_task_repository.admit_pending(_task("b-claimable"), capacity=10)

    blocker = psycopg2.connect(pg_function_dsn)
    try:
        cursor = blocker.cursor()
        cursor.execute(
            "SELECT task_id FROM memplex_background_tasks "
            "WHERE task_id='a-locked' FOR UPDATE"
        )
        started = time.monotonic()
        claimed = postgres_task_repository.claim_due(limit=1, lease_seconds=30)
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert elapsed < 1.0
    assert [item.task_id for item in claimed] == ["b-claimable"]


def test_two_instances_claim_distinct_rows_without_duplicate_lease(
    postgres_task_repository: PostgresTaskRepository,
) -> None:
    second_instance = PostgresTaskRepository(
        ready_pool=postgres_task_repository._ready_pool
    )
    postgres_task_repository.admit_pending(_task("multi-a"), capacity=10)
    postgres_task_repository.admit_pending(_task("multi-b"), capacity=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = tuple(
            executor.map(
                lambda repository: repository.claim_due(
                    limit=1, lease_seconds=30
                )[0],
                (postgres_task_repository, second_instance),
            )
        )

    assert {item.task_id for item in claims} == {"multi-a", "multi-b"}
    assert len({item.lease_id for item in claims}) == 2


def test_expired_lease_is_reclaimed_and_old_token_cannot_complete_or_retry(
    pg_function_dsn: str,
    postgres_task_repository: PostgresTaskRepository,
) -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    postgres_task_repository.admit_pending(_task("fenced"), capacity=10)
    old = postgres_task_repository.claim_due(limit=1, lease_seconds=30)[0]

    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE memplex_background_tasks "
            "SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE task_id='fenced'"
        )
        connection.commit()
    finally:
        connection.close()

    fresh = postgres_task_repository.claim_due(limit=1, lease_seconds=30)[0]
    assert fresh.lease_id != old.lease_id
    assert postgres_task_repository.complete(
        old.task_id, old.lease_id, {"stale": True}
    ) is None
    assert postgres_task_repository.fail(
        old.task_id,
        old.lease_id,
        error_code="stale_failure",
        retry_delay_seconds=0,
    ) is None

    completed = postgres_task_repository.complete(
        fresh.task_id, fresh.lease_id, {"fresh": True}
    )
    assert completed is not None
    assert completed.status is TaskStatus.COMPLETED
    assert completed.result == {"fresh": True}


def test_postgres_retry_dead_letter_and_replay_are_durable(
    pg_function_dsn: str,
    postgres_task_repository: PostgresTaskRepository,
) -> None:
    psycopg2 = pytest.importorskip("psycopg2")
    postgres_task_repository.admit_pending(
        _task("postgres-dead-letter", max_retries=1), capacity=10
    )
    first = postgres_task_repository.claim_due(limit=1, lease_seconds=30)[0]
    retry = postgres_task_repository.fail(
        first.task_id,
        first.lease_id,
        error_code="first_failure",
        retry_delay_seconds=60,
    )
    assert retry is not None
    assert retry.status is TaskStatus.PENDING
    assert retry.retry_count == 1

    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE memplex_background_tasks SET next_attempt_at=clock_timestamp() "
            "WHERE task_id='postgres-dead-letter'"
        )
        connection.commit()
    finally:
        connection.close()

    second = postgres_task_repository.claim_due(limit=1, lease_seconds=30)[0]
    dead = postgres_task_repository.fail(
        second.task_id,
        second.lease_id,
        error_code="second_failure",
        retry_delay_seconds=60,
    )
    assert dead is not None
    assert dead.status is TaskStatus.FAILED
    assert dead.retry_count == 2

    replayed = postgres_task_repository.replay_failed_atomic(
        "postgres-dead-letter", capacity=10
    )
    assert replayed is not None
    assert replayed.task_id == "postgres-dead-letter"
    assert replayed.status is TaskStatus.PENDING
    assert replayed.retry_count == 0
