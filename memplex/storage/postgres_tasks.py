"""PostgreSQL durable repository for at-least-once background tasks."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from memplex.auth import AuthorizationContext, Principal
from memplex.models import BackgroundTask, TaskInfo, TaskStatus
from memplex.storage.pool import ReadyPostgresPool, validate_ready_postgres_pool
from memplex.task_repository import TaskRepository, WorkerQueueFull

_TASK_CONTEXT = AuthorizationContext(
    principal=Principal(
        tenant_id="memplex-system",
        subject_id="background-worker",
        roles=frozenset({"background-worker"}),
    ),
    workspace_id="memplex-system",
    agent_id="background-worker",
)

_ROW_COLUMNS = """
    task_id, task_type, status, created_at, completed_at, payload, result,
    error, retry_count, max_retries, next_attempt_at, lease_until, lease_id,
    last_error_code
"""


def _json_value(value: Any) -> Any:
    """Return an exact JSON value suitable for a JSONB parameter."""
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if type(value) in {list, tuple}:
        return [_json_value(item) for item in value]
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be exact strings")
            result[key] = _json_value(item)
        return result
    raise TypeError(f"Object of type {type(value)} is not JSON serializable")


def _json_text(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class PostgresTaskRepository(TaskRepository):
    """Database-clocked, lease-fenced task repository.

    Claims use ``FOR UPDATE SKIP LOCKED`` so independent service instances can
    make progress without serialising behind another instance's selected row.
    Fencing prevents stale workers from publishing a completion or retry, but
    it does not make arbitrary handler side effects exactly once.
    """

    def __init__(self, *, ready_pool: ReadyPostgresPool) -> None:
        sealed = validate_ready_postgres_pool(ready_pool)
        self._ready_pool = sealed
        self._pool_manager = sealed.manager

    @staticmethod
    def _bind_task_scope(_cursor: Any, _context: AuthorizationContext) -> None:
        # The task catalogue is service-global and intentionally has no RLS.
        # The pool has already verified the exact application principal and
        # target before this no-op binder is invoked.
        return None

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        with self._pool_manager.transaction(
            self._bind_task_scope, _TASK_CONTEXT
        ) as (_connection, cursor):
            yield cursor

    @staticmethod
    def _positive_int(value: object, name: str) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive exact int")
        return value

    @staticmethod
    def _task_id(value: object) -> str:
        if type(value) is not str or not value:
            raise ValueError("task_id must be a non-empty exact str")
        return value

    @staticmethod
    def _row_to_info(row: tuple[Any, ...]) -> TaskInfo:
        if row is None or len(row) != 14:
            raise RuntimeError("PostgreSQL task row is malformed")
        return TaskInfo(
            task_id=str(row[0]),
            task_type=BackgroundTask(str(row[1])),
            status=TaskStatus(str(row[2])),
            created_at=row[3],
            completed_at=row[4],
            payload=row[5],
            result=row[6],
            error=row[7],
            retry_count=int(row[8]),
            max_retries=int(row[9]),
            next_attempt_at=row[10],
            lease_until=row[11],
            lease_id=row[12],
            last_error_code=row[13],
        )

    @staticmethod
    def _lock_capacity(cursor: Any) -> None:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended('memplex-background-task-capacity', 0))"
        )

    @staticmethod
    def _active_count(cursor: Any) -> int:
        cursor.execute(
            "SELECT count(*) FROM memplex_background_tasks "
            "WHERE status IN ('pending','running')"
        )
        row = cursor.fetchone()
        return int(row[0])

    def admit_pending(self, info: TaskInfo, *, capacity: int) -> TaskInfo:
        self._positive_int(capacity, "capacity")
        if type(info) is not TaskInfo or info.status is not TaskStatus.PENDING:
            raise TypeError("info must be an exact pending TaskInfo")
        self._task_id(info.task_id)
        if type(info.max_retries) is not int or info.max_retries < 0:
            raise ValueError("max_retries must be a non-negative exact int")
        payload = {} if info.payload is None else info.payload
        if type(payload) is not dict:
            raise TypeError("task payload must be an exact dict")
        with self._transaction() as cursor:
            self._lock_capacity(cursor)
            if self._active_count(cursor) >= capacity:
                raise WorkerQueueFull("worker_queue_full")
            cursor.execute(
                f"""
                WITH authoritative_now AS (
                    SELECT clock_timestamp() AS value
                )
                INSERT INTO memplex_background_tasks
                    (task_id, task_type, status, payload, retry_count,
                     max_retries, created_at, next_attempt_at)
                SELECT %s, %s, 'pending', %s::jsonb, 0, %s,
                       authoritative_now.value, authoritative_now.value
                FROM authoritative_now
                RETURNING {_ROW_COLUMNS}
                """,
                (
                    info.task_id,
                    info.task_type.value,
                    _json_text(payload),
                    info.max_retries,
                ),
            )
            return self._row_to_info(cursor.fetchone())

    def get(self, task_id: str) -> TaskInfo | None:
        self._task_id(task_id)
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT {_ROW_COLUMNS} FROM memplex_background_tasks WHERE task_id=%s",
                (task_id,),
            )
            row = cursor.fetchone()
            return None if row is None else self._row_to_info(row)

    def list_by_status(self, *statuses: TaskStatus) -> list[TaskInfo]:
        if not statuses:
            return []
        if any(type(status) is not TaskStatus for status in statuses):
            raise TypeError("statuses must be exact TaskStatus values")
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT {_ROW_COLUMNS} FROM memplex_background_tasks "
                "WHERE status = ANY(%s) ORDER BY created_at, task_id",
                ([status.value for status in statuses],),
            )
            return [self._row_to_info(row) for row in cursor.fetchall()]

    def count_by_status(self, *statuses: TaskStatus) -> int:
        if not statuses:
            return 0
        if any(type(status) is not TaskStatus for status in statuses):
            raise TypeError("statuses must be exact TaskStatus values")
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT count(*) FROM memplex_background_tasks WHERE status = ANY(%s)",
                ([status.value for status in statuses],),
            )
            return int(cursor.fetchone()[0])

    def replay_failed_atomic(
        self,
        task_id: str,
        *,
        capacity: int,
        now: datetime | None = None,
    ) -> TaskInfo | None:
        del now  # PostgreSQL time is authoritative.
        self._task_id(task_id)
        self._positive_int(capacity, "capacity")
        with self._transaction() as cursor:
            self._lock_capacity(cursor)
            cursor.execute(
                "SELECT status FROM memplex_background_tasks "
                "WHERE task_id=%s FOR UPDATE",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None or row[0] != TaskStatus.FAILED.value:
                return None
            if self._active_count(cursor) >= capacity:
                raise WorkerQueueFull("worker_queue_full")
            cursor.execute(
                f"""
                UPDATE memplex_background_tasks
                SET status='pending', retry_count=0, completed_at=NULL,
                    result=NULL, error=NULL, last_error_code=NULL,
                    next_attempt_at=clock_timestamp(), lease_until=NULL,
                    lease_id=NULL
                WHERE task_id=%s AND status='failed'
                RETURNING {_ROW_COLUMNS}
                """,
                (task_id,),
            )
            replayed = cursor.fetchone()
            return None if replayed is None else self._row_to_info(replayed)

    def cancel_pending(self, task_id: str) -> bool:
        self._task_id(task_id)
        with self._transaction() as cursor:
            cursor.execute(
                "UPDATE memplex_background_tasks "
                "SET status='cancelled', next_attempt_at=NULL "
                "WHERE task_id=%s AND status='pending' RETURNING task_id",
                (task_id,),
            )
            return cursor.fetchone() is not None

    def due_task_ids(
        self, now: datetime | None = None, *, limit: int
    ) -> list[str]:
        del now  # PostgreSQL time is authoritative.
        self._positive_int(limit, "limit")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT task_id
                FROM memplex_background_tasks
                WHERE (status='pending' AND next_attempt_at <= clock_timestamp())
                   OR (status='running' AND lease_until <= clock_timestamp())
                ORDER BY COALESCE(next_attempt_at, lease_until), created_at, task_id
                LIMIT %s
                """,
                (limit,),
            )
            return [str(row[0]) for row in cursor.fetchall()]

    def claim(
        self,
        task_id: str,
        now: datetime | None = None,
        *,
        lease_seconds: int,
    ) -> TaskInfo | None:
        del now  # PostgreSQL time is authoritative.
        self._task_id(task_id)
        self._positive_int(lease_seconds, "lease_seconds")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT task_id
                FROM memplex_background_tasks
                WHERE task_id=%s
                  AND ((status='pending' AND next_attempt_at <= clock_timestamp())
                    OR (status='running' AND lease_until <= clock_timestamp()))
                FOR UPDATE SKIP LOCKED
                """,
                (task_id,),
            )
            if cursor.fetchone() is None:
                return None
            return self._claim_locked(cursor, task_id, lease_seconds)

    @staticmethod
    def _claim_locked(cursor: Any, task_id: str, lease_seconds: int) -> TaskInfo:
        lease_id = uuid.uuid4().hex
        cursor.execute(
            f"""
            UPDATE memplex_background_tasks
            SET status='running', lease_id=%s,
                lease_until=clock_timestamp() + (%s * interval '1 second'),
                next_attempt_at=NULL
            WHERE task_id=%s
            RETURNING {_ROW_COLUMNS}
            """,
            (lease_id, lease_seconds, task_id),
        )
        return PostgresTaskRepository._row_to_info(cursor.fetchone())

    def claim_due(
        self,
        *,
        limit: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> list[TaskInfo]:
        del now  # PostgreSQL time is authoritative.
        self._positive_int(limit, "limit")
        self._positive_int(lease_seconds, "lease_seconds")
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT task_id
                FROM memplex_background_tasks
                WHERE (status='pending' AND next_attempt_at <= clock_timestamp())
                   OR (status='running' AND lease_until <= clock_timestamp())
                ORDER BY COALESCE(next_attempt_at, lease_until), created_at, task_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (limit,),
            )
            return [
                self._claim_locked(cursor, str(row[0]), lease_seconds)
                for row in cursor.fetchall()
            ]

    def complete(
        self,
        task_id: str,
        lease_id: str,
        result: Any,
        *,
        now: datetime | None = None,
    ) -> TaskInfo | None:
        del now  # PostgreSQL time is authoritative.
        self._task_id(task_id)
        if type(lease_id) is not str or not lease_id:
            raise ValueError("lease_id must be a non-empty exact str")
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                UPDATE memplex_background_tasks
                SET status='completed', completed_at=clock_timestamp(),
                    result=%s::jsonb, error=NULL, last_error_code=NULL,
                    next_attempt_at=NULL, lease_until=NULL, lease_id=NULL
                WHERE task_id=%s AND status='running' AND lease_id=%s
                  AND lease_until > clock_timestamp()
                RETURNING {_ROW_COLUMNS}
                """,
                (_json_text(result), task_id, lease_id),
            )
            row = cursor.fetchone()
            return None if row is None else self._row_to_info(row)

    def fail(
        self,
        task_id: str,
        lease_id: str,
        *,
        error_code: str,
        retry_delay_seconds: int,
        now: datetime | None = None,
    ) -> TaskInfo | None:
        del now  # PostgreSQL time is authoritative.
        self._task_id(task_id)
        if type(lease_id) is not str or not lease_id:
            raise ValueError("lease_id must be a non-empty exact str")
        if type(error_code) is not str or not error_code:
            raise ValueError("error_code must be a non-empty exact str")
        if type(retry_delay_seconds) is not int or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be a non-negative exact int")
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                UPDATE memplex_background_tasks
                SET retry_count=retry_count + 1,
                    status=CASE WHEN retry_count + 1 <= max_retries
                                THEN 'pending' ELSE 'failed' END,
                    next_attempt_at=CASE WHEN retry_count + 1 <= max_retries
                        THEN clock_timestamp() + (%s * interval '1 second')
                        ELSE NULL END,
                    result=NULL, error=%s, last_error_code=%s,
                    lease_until=NULL, lease_id=NULL
                WHERE task_id=%s AND status='running' AND lease_id=%s
                  AND lease_until > clock_timestamp()
                RETURNING {_ROW_COLUMNS}
                """,
                (
                    retry_delay_seconds,
                    error_code,
                    error_code,
                    task_id,
                    lease_id,
                ),
            )
            row = cursor.fetchone()
            return None if row is None else self._row_to_info(row)

    def status_counts(self) -> dict[TaskStatus, int]:
        counts = {status: 0 for status in TaskStatus}
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT status, count(*) FROM memplex_background_tasks GROUP BY status"
            )
            for status, count in cursor.fetchall():
                counts[TaskStatus(str(status))] = int(count)
        return counts
