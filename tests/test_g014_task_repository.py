"""G014 后台任务 repository 的跨后端合同测试。

这些测试刻意把 handler 语义限定为 at-least-once：lease 过期后同一 handler
可能再次执行，可靠性来自 lease_id fencing，而不是虚假的 exactly-once 声明。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from memplex.models import BackgroundTask, TaskInfo, TaskStatus
from memplex.task_repository import TaskRepository
from memplex.worker import BackgroundWorker, TaskStore


def _task(task_id: str, *, max_retries: int = 1) -> TaskInfo:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    return TaskInfo(
        task_id=task_id,
        task_type=BackgroundTask.BUILD_INDEX,
        status=TaskStatus.PENDING,
        created_at=now,
        payload={"task_id": task_id},
        max_retries=max_retries,
        next_attempt_at=now,
    )


def test_lite_task_store_is_the_compatibility_task_repository(tmp_path) -> None:
    repository = TaskStore(tmp_path / "tasks.json")

    assert isinstance(repository, TaskRepository)


def test_postgres_application_acl_includes_exact_task_table_rights() -> None:
    from memplex.storage.migrations.runner import (
        _APPLICATION_ACL,
        _APPLICATION_ACL_TABLES,
    )

    assert "memplex_background_tasks" in _APPLICATION_ACL_TABLES
    assert _APPLICATION_ACL["memplex_background_tasks"] == frozenset(
        {"SELECT", "INSERT", "UPDATE", "DELETE"}
    )


def test_lite_repository_rejects_stale_lease_completion_and_retry(tmp_path) -> None:
    repository = TaskStore(tmp_path / "tasks.json")
    first_now = datetime(2026, 8, 13, tzinfo=UTC)
    repository.admit_pending(_task("leased"), capacity=10)

    first = repository.claim_due(limit=1, lease_seconds=5, now=first_now)[0]
    second = repository.claim_due(
        limit=1,
        lease_seconds=5,
        now=first_now + timedelta(seconds=6),
    )[0]

    assert first.lease_id
    assert second.lease_id
    assert first.lease_id != second.lease_id
    assert (
        repository.complete(
            first.task_id,
            first.lease_id,
            {"winner": "stale"},
            now=first_now + timedelta(seconds=6),
        )
        is None
    )
    assert (
        repository.fail(
            first.task_id,
            first.lease_id,
            error_code="stale_failure",
            retry_delay_seconds=0,
            now=first_now + timedelta(seconds=6),
        )
        is None
    )

    completed = repository.complete(
        second.task_id,
        second.lease_id,
        {"winner": "fresh"},
        now=first_now + timedelta(seconds=7),
    )
    assert completed is not None
    assert completed.status is TaskStatus.COMPLETED
    assert completed.result == {"winner": "fresh"}


def test_worker_accepts_an_injected_repository_and_fences_completion(tmp_path) -> None:
    repository = TaskStore(tmp_path / "tasks.json")
    now = datetime(2026, 8, 13, tzinfo=UTC)
    worker = BackgroundWorker(
        storage_path=tmp_path / "unused.json",
        task_repository=repository,
        clock=lambda: now,
    )
    worker._dispatch = lambda *_args: {"status": "completed"}

    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {})

    assert worker.task_repository is repository
    assert worker.run_once() is True
    assert repository.get(task_id).status is TaskStatus.COMPLETED


def test_retry_transitions_to_dead_letter_then_replays_with_same_identity(tmp_path) -> None:
    repository = TaskStore(tmp_path / "tasks.json")
    now = datetime(2026, 8, 13, tzinfo=UTC)
    repository.admit_pending(_task("dead-letter", max_retries=1), capacity=10)

    first = repository.claim_due(limit=1, lease_seconds=30, now=now)[0]
    retry = repository.fail(
        first.task_id,
        first.lease_id,
        error_code="handler_failed",
        retry_delay_seconds=2,
        now=now,
    )
    assert retry is not None
    assert retry.status is TaskStatus.PENDING
    assert retry.retry_count == 1
    assert retry.next_attempt_at == now + timedelta(seconds=2)

    second = repository.claim_due(
        limit=1,
        lease_seconds=30,
        now=now + timedelta(seconds=2),
    )[0]
    dead = repository.fail(
        second.task_id,
        second.lease_id,
        error_code="handler_failed_again",
        retry_delay_seconds=4,
        now=now + timedelta(seconds=2),
    )
    assert dead is not None
    assert dead.status is TaskStatus.FAILED
    assert dead.retry_count == 2
    assert dead.next_attempt_at is None

    replayed = repository.replay_failed_atomic(
        "dead-letter", capacity=10, now=now + timedelta(seconds=3)
    )
    assert replayed is not None
    assert replayed.task_id == "dead-letter"
    assert replayed.status is TaskStatus.PENDING
    assert replayed.retry_count == 0
