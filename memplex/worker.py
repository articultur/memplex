"""BackgroundWorker -- async task processor for long-running operations.

Runs tasks (index builds, wiki compilation, vector refresh, compaction)
on a daemon thread so the main session is never blocked.

Task state is persisted to a lightweight JSON file so that pending/running
tasks survive process crashes and can be recovered on restart.

Usage::

    worker = BackgroundWorker()
    worker.start()
    task_id = worker.submit(BackgroundTask.BUILD_INDEX, {"func_id": "abc"})
    ...
    status = worker.get_status(task_id)
    worker.stop()
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread
from typing import Any, Callable, Dict, List, Optional

from memplex.compaction import CompactionPipeline, CompactionScope
from memplex.models import BackgroundTask, TaskInfo, TaskStatus, WorkerDrainResult

logger = logging.getLogger(__name__)


class WorkerQueueFull(RuntimeError):
    """Raised when durable non-terminal worker admission is exhausted."""


class TaskStoreIntegrityError(RuntimeError):
    """Raised when durable worker state cannot be decoded or committed."""


def _generate_uuid() -> str:
    return uuid.uuid4().hex


def _normalise_json_value(value: Any) -> Any:
    """Return a detached exact-JSON value without coercing mapping keys."""
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
        return _normalise_json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _normalise_json_value(asdict(value))
    if type(value) in {list, tuple}:
        return [_normalise_json_value(item) for item in value]
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("JSON object keys must be exact strings")
            result[key] = _normalise_json_value(item)
        return result
    raise TypeError(f"Object of type {type(value)} is not JSON serializable")


# ── TaskStore: lightweight JSON persistence ────────────────────────────


class TaskStore:
    """Persist task state to a single JSON file.

    File format::

        {
            "tasks": {
                "<task_id>": { ... TaskInfo dict ... },
                ...
            }
        }
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()
        self._poisoned = False
        self._load()

    def _assert_healthy(self) -> None:
        if self._poisoned:
            raise TaskStoreIntegrityError("worker task store requires reopen")

    # ── Persistence ─────────────────────────────────────────────────

    @contextmanager
    def _disk_lock(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        with open(self._lock_path, "r+b") as lock_file:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except ImportError as exc:  # pragma: no cover - production is POSIX
                raise TaskStoreIntegrityError("worker file locking is unavailable") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_tasks_file(self) -> Dict[str, Dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if type(data) is not dict or set(data) != {"tasks"}:
                raise ValueError("invalid worker task store root")
            tasks = data["tasks"]
            if type(tasks) is not dict or any(
                type(task_id) is not str or not task_id or type(item) is not dict
                for task_id, item in tasks.items()
            ):
                raise ValueError("invalid worker task store tasks")
            for task_id, item in tasks.items():
                info = self._dict_to_info(item)
                if info.task_id != task_id:
                    raise ValueError("worker task identity mismatch")
            return tasks
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            raise TaskStoreIntegrityError("worker task store is invalid") from exc

    def _load(self) -> None:
        """Load exact durable state, preserving corrupt bytes in place."""
        with self._disk_lock():
            self._tasks = self._read_tasks_file()

    def _save_candidate(
        self, candidate: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Durably replace the complete task state or raise without publication."""
        self._assert_healthy()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            root = _normalise_json_value({"tasks": candidate})
            normalised = root["tasks"]
            for task_id, item in normalised.items():
                info = self._dict_to_info(item)
                if info.task_id != task_id:
                    raise ValueError("worker task identity mismatch")
            serialized = json.dumps(
                root,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except TypeError as exc:
            raise TaskStoreIntegrityError("worker task state is not serializable") from exc
        except ValueError as exc:
            raise TaskStoreIntegrityError("worker task state is invalid") from exc
        tmp_fd: int | None = None
        tmp_path: str | None = None
        replaced = False
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            fh = os.fdopen(tmp_fd, "w", encoding="utf-8")
            tmp_fd = None
            with fh:
                fh.write(serialized)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
            replaced = True
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException as exc:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if replaced:
                self._poisoned = True
            if isinstance(exc, OSError):
                raise TaskStoreIntegrityError("worker task state commit failed") from exc
            raise
        return normalised

    # ── CRUD ────────────────────────────────────────────────────────

    def save(self, info: TaskInfo) -> None:
        """Save or update a TaskInfo."""
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            candidate = copy.deepcopy(self._tasks)
            candidate[info.task_id] = self._info_to_dict(info)
            self._tasks = self._save_candidate(candidate)

    def admit_pending(self, info: TaskInfo, *, capacity: int) -> None:
        """Atomically reserve durable capacity and persist one new pending task."""
        if type(capacity) is not int:
            raise TypeError("capacity must be an exact int")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            if info.task_id in self._tasks:
                raise TaskStoreIntegrityError("worker task identity already exists")
            active = sum(
                item.get("status")
                in {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}
                for item in self._tasks.values()
            )
            if active >= capacity:
                raise WorkerQueueFull("worker_queue_full")
            candidate = copy.deepcopy(self._tasks)
            candidate[info.task_id] = self._info_to_dict(info)
            self._tasks = self._save_candidate(candidate)

    def replay_failed_atomic(
        self,
        task_id: str,
        *,
        capacity: int,
        now: datetime,
    ) -> Optional[TaskInfo]:
        """Atomically reserve capacity while resetting one durable dead letter."""
        if type(capacity) is not int:
            raise TypeError("capacity must be an exact int")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            data = self._tasks.get(task_id)
            if data is None or data.get("status") != TaskStatus.FAILED.value:
                return None
            active = sum(
                item.get("status")
                in {TaskStatus.PENDING.value, TaskStatus.RUNNING.value}
                for item in self._tasks.values()
            )
            if active >= capacity:
                raise WorkerQueueFull("worker_queue_full")
            info = self._dict_to_info(copy.deepcopy(data))
            info.status = TaskStatus.PENDING
            info.retry_count = 0
            info.completed_at = None
            info.error = None
            info.last_error_code = None
            info.lease_until = None
            info.next_attempt_at = now
            candidate = copy.deepcopy(self._tasks)
            candidate[task_id] = self._info_to_dict(info)
            self._tasks = self._save_candidate(candidate)
            return copy.deepcopy(info)

    def delete(self, task_id: str) -> None:
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            if task_id not in self._tasks:
                return
            candidate = copy.deepcopy(self._tasks)
            del candidate[task_id]
            self._tasks = self._save_candidate(candidate)

    def get(self, task_id: str) -> Optional[TaskInfo]:
        """Retrieve a TaskInfo by ID, or ``None``."""
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            data = self._tasks.get(task_id)
            if data is None:
                return None
            return self._dict_to_info(copy.deepcopy(data))

    def list_by_status(self, *statuses: TaskStatus) -> List[TaskInfo]:
        """Return all tasks matching any of the given statuses."""
        status_values = {s.value for s in statuses}
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            result: List[TaskInfo] = []
            for data in self._tasks.values():
                if data.get("status") in status_values:
                    result.append(self._dict_to_info(copy.deepcopy(data)))
            return result

    def count_by_status(self, *statuses: TaskStatus) -> int:
        return len(self.list_by_status(*statuses))

    def due_task_ids(self, now: datetime, *, limit: int) -> list[str]:
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            due: list[tuple[datetime, str]] = []
            for task_id, data in self._tasks.items():
                status = TaskStatus(data["status"])
                if status is TaskStatus.RUNNING:
                    lease_raw = data.get("lease_until")
                    lease_until = (
                        datetime.fromisoformat(lease_raw) if lease_raw else None
                    )
                    if lease_until is not None and lease_until > now:
                        continue
                elif status is not TaskStatus.PENDING:
                    continue
                due_raw = data.get("next_attempt_at")
                due_at = datetime.fromisoformat(due_raw) if due_raw else now
                if due_at <= now:
                    due.append((due_at, task_id))
            due.sort(key=lambda item: (item[0], item[1]))
            return [task_id for _due_at, task_id in due[:limit]]

    def claim(self, task_id: str, now: datetime, *, lease_seconds: int) -> Optional[TaskInfo]:
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            data = self._tasks.get(task_id)
            if data is None:
                return None
            status = TaskStatus(data["status"])
            if status is TaskStatus.RUNNING:
                lease_raw = data.get("lease_until")
                lease_until = datetime.fromisoformat(lease_raw) if lease_raw else None
                if lease_until is not None and lease_until > now:
                    return None
            elif status is not TaskStatus.PENDING:
                return None
            due_raw = data.get("next_attempt_at")
            if due_raw and datetime.fromisoformat(due_raw) > now:
                return None
            info = self._dict_to_info(copy.deepcopy(data))
            info.status = TaskStatus.RUNNING
            info.lease_until = now + timedelta(seconds=lease_seconds)
            candidate = copy.deepcopy(self._tasks)
            candidate[task_id] = self._info_to_dict(info)
            self._tasks = self._save_candidate(candidate)
            return info

    def status_counts(self) -> dict[TaskStatus, int]:
        with self._lock, self._disk_lock():
            self._assert_healthy()
            self._tasks = self._read_tasks_file()
            counts = {status: 0 for status in TaskStatus}
            for data in self._tasks.values():
                counts[TaskStatus(data["status"])] += 1
            return counts

    # ── Serialisation helpers ───────────────────────────────────────

    @staticmethod
    def _info_to_dict(info: TaskInfo) -> Dict[str, Any]:
        return {
            "task_id": info.task_id,
            "task_type": info.task_type.value,
            "status": info.status.value,
            "created_at": info.created_at.isoformat() if info.created_at else None,
            "completed_at": info.completed_at.isoformat() if info.completed_at else None,
            "payload": info.payload,
            "result": info.result,
            "error": info.error,
            "retry_count": info.retry_count,
            "max_retries": info.max_retries,
            "next_attempt_at": (
                info.next_attempt_at.isoformat() if info.next_attempt_at else None
            ),
            "lease_until": info.lease_until.isoformat() if info.lease_until else None,
            "last_error_code": info.last_error_code,
        }

    @staticmethod
    def _dict_to_info(data: Dict[str, Any]) -> TaskInfo:
        required = {
            "task_id",
            "task_type",
            "status",
            "created_at",
            "completed_at",
            "payload",
            "result",
            "error",
            "retry_count",
            "max_retries",
        }
        optional = {"next_attempt_at", "lease_until", "last_error_code"}
        if type(data) is not dict or not required <= set(data) <= required | optional:
            raise ValueError("invalid worker task record schema")
        if type(data["task_id"]) is not str or not data["task_id"]:
            raise ValueError("invalid worker task id")
        if data["payload"] is not None and type(data["payload"]) is not dict:
            raise ValueError("invalid worker task payload")
        for name in ("retry_count", "max_retries"):
            if type(data[name]) is not int or data[name] < 0:
                raise ValueError("invalid worker task retry state")
        for name in ("error", "last_error_code"):
            if data.get(name) is not None and type(data[name]) is not str:
                raise ValueError("invalid worker task error state")
        return TaskInfo(
            task_id=data["task_id"],
            task_type=BackgroundTask(data["task_type"]),
            status=TaskStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else datetime.now(),
            completed_at=datetime.fromisoformat(data["completed_at"])
            if data.get("completed_at")
            else None,
            payload=data.get("payload"),
            result=data.get("result"),
            error=data.get("error"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            next_attempt_at=(
                datetime.fromisoformat(data["next_attempt_at"])
                if data.get("next_attempt_at")
                else None
            ),
            lease_until=(
                datetime.fromisoformat(data["lease_until"])
                if data.get("lease_until")
                else None
            ),
            last_error_code=data.get("last_error_code"),
        )


# ── BackgroundWorker ──────────────────────────────────────────────────


class BackgroundWorker:
    """Background task processor.

    Parameters
    ----------
    storage_path:
        Path to the JSON file used for task persistence.
        Defaults to ``~/.memplex/tasks.json``.
    """

    def __init__(
        self,
        storage_path: Path = Path("~/.memplex/tasks.json").expanduser(),
        compaction_pipeline: Optional["CompactionPipeline"] = None,
        store: Optional[Any] = None,
        engine: Optional[Any] = None,
        embedding_service: Optional[Any] = None,
        config: Optional[Any] = None,
        *,
        queue_capacity: int | None = None,
        claim_size: int | None = None,
        max_attempts: int | None = None,
        lease_seconds: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        worker_config = getattr(config, "worker", None)
        queue_capacity = (
            queue_capacity
            if queue_capacity is not None
            else getattr(worker_config, "queue_capacity", 1000)
        )
        claim_size = (
            claim_size if claim_size is not None else getattr(worker_config, "claim_size", 32)
        )
        max_attempts = (
            max_attempts
            if max_attempts is not None
            else getattr(worker_config, "max_attempts", 3)
        )
        lease_seconds = (
            lease_seconds
            if lease_seconds is not None
            else getattr(worker_config, "lease_seconds", 60)
        )
        for name, value in (
            ("queue_capacity", queue_capacity),
            ("claim_size", claim_size),
            ("max_attempts", max_attempts),
            ("lease_seconds", lease_seconds),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be an exact int")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self._task_store = TaskStore(storage_path)
        self._queue_capacity = queue_capacity
        self._claim_size = min(claim_size, queue_capacity)
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._queue: Queue[str] = Queue(maxsize=queue_capacity)
        self._queued_ids: set[str] = set()
        self._state_lock = RLock()
        self._wake = Event()
        self._accepting = True
        self._running: bool = False
        self._worker_thread: Optional[Thread] = None
        self._active_task_id: str | None = None
        self._completed_since_start = 0
        self._callbacks: dict[str, Callable] = {}
        self._compaction_pipeline = compaction_pipeline
        self._store = store
        self._engine = engine
        self._embedding_service = embedding_service
        self._config = config
        self._last_compaction: Optional[datetime] = None
        self._recover_pending_tasks()

    @property
    def queue_depth(self) -> int:
        """Return the number of tasks currently in the queue."""
        return self._queue.qsize()

    @property
    def last_compaction(self) -> Optional[datetime]:
        """Return the timestamp of the last compaction run, or None."""
        return self._last_compaction

    def dead_letters_pending(self) -> int:
        """Return the count of failed tasks that may need manual intervention."""
        return len(self._task_store.list_by_status(TaskStatus.FAILED))

    def persisted_pending_count(self) -> int:
        """Return durable pending plus leased work, independent of queue hints."""
        return self._task_store.count_by_status(TaskStatus.PENDING, TaskStatus.RUNNING)

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background worker daemon thread."""
        with self._state_lock:
            if self._running:
                return
            self._accepting = True
            self._running = True
            self._wake.clear()
            self._worker_thread = Thread(target=self._run_loop, daemon=True)
            self._worker_thread.start()
        logger.info("BackgroundWorker started")

    def stop(self, timeout: float = 30.0) -> WorkerDrainResult:
        """Stop admission and return an exact durable drain snapshot."""
        if (
            type(timeout) not in {int, float}
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("timeout must be a non-negative finite number")
        deadline = time.monotonic() + float(timeout)
        with self._state_lock:
            self._accepting = False
            self._running = False
            self._wake.set()
            thread = self._worker_thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        deadline_exceeded = bool(thread is not None and thread.is_alive())
        while True:
            try:
                task_id = self._queue.get_nowait()
            except Empty:
                break
            else:
                with self._state_lock:
                    self._queued_ids.discard(task_id)
                self._queue.task_done()
        counts = self._task_store.status_counts()
        leased = counts[TaskStatus.RUNNING]
        result = WorkerDrainResult(
            drained=leased == 0,
            completed=self._completed_since_start,
            pending=counts[TaskStatus.PENDING],
            leased=leased,
            dead_letters=counts[TaskStatus.FAILED],
            deadline_exceeded=deadline_exceeded,
        )
        logger.info("BackgroundWorker stopped")
        return result

    # ── Public API ──────────────────────────────────────────────────

    def submit(
        self,
        task: BackgroundTask,
        payload: dict,
        callback: Optional[Callable] = None,
    ) -> str:
        """Submit a background task, returning the ``task_id``.

        Parameters
        ----------
        task:
            Type of background task.
        payload:
            Arbitrary data forwarded to the task handler.
        callback:
            Optional function called with the handler's result on success.
        """
        if type(task) is not BackgroundTask:
            raise TypeError("task must be an exact BackgroundTask")
        if type(payload) is not dict:
            raise TypeError("payload must be an exact dict")
        if callback is not None and not callable(callback):
            raise TypeError("callback must be callable")
        with self._state_lock:
            if not self._accepting:
                raise RuntimeError("worker_not_accepting")
            task_id = _generate_uuid()
            now = self._clock()
            info = TaskInfo(
                task_id=task_id,
                task_type=task,
                status=TaskStatus.PENDING,
                created_at=now,
                payload=copy.deepcopy(payload),
                max_retries=max(0, self._max_attempts - 1),
                next_attempt_at=now,
            )
            self._task_store.admit_pending(info, capacity=self._queue_capacity)
            try:
                self._queue.put_nowait(task_id)
            except Full as exc:
                self._task_store.delete(task_id)
                raise WorkerQueueFull("worker_queue_full") from exc
            self._queued_ids.add(task_id)
            if callback is not None:
                self._callbacks[task_id] = callback
            self._wake.set()
        logger.debug("Submitted task %s (%s)", task_id, task.value)
        return task_id

    def get_status(self, task_id: str) -> TaskStatus:
        """Return the current status of a task."""
        info = self._task_store.get(task_id)
        if info is None:
            raise KeyError(f"Task {task_id!r} not found")
        return info.status

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending task.

        Returns ``True`` if the task was successfully cancelled,
        ``False`` if it was already running/completed.
        """
        info = self._task_store.get(task_id)
        if info is None:
            return False
        if info.status in (TaskStatus.PENDING,):
            info.status = TaskStatus.CANCELLED
            self._task_store.save(info)
            return True
        return False

    def replay_failed(self, task_id: str) -> bool:
        """Reset one durable dead letter to immediately due pending state."""
        with self._state_lock:
            if not self._accepting:
                raise RuntimeError("worker_not_accepting")
            info = self._task_store.replay_failed_atomic(
                task_id,
                capacity=self._queue_capacity,
                now=self._clock(),
            )
            if info is None:
                return False
            try:
                self._queue.put_nowait(task_id)
            except Full as exc:
                info.status = TaskStatus.FAILED
                info.next_attempt_at = None
                self._task_store.save(info)
                raise WorkerQueueFull("worker_queue_full") from exc
            self._queued_ids.add(task_id)
            self._wake.set()
            return True

    # ── Worker loop ─────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main loop: dequeue tasks and execute them."""
        while True:
            with self._state_lock:
                if not self._running:
                    return
            try:
                if not self._run_once(require_running=True):
                    self._wake.wait(timeout=0.1)
                    self._wake.clear()
            except BaseException:
                logger.error("worker_loop_failed")

    def _fill_due_queue(self) -> None:
        now = self._clock()
        with self._state_lock:
            free = self._queue_capacity - self._queue.qsize()
            if free <= 0:
                return
            due_ids = self._task_store.due_task_ids(now, limit=self._queue_capacity)
            admitted = 0
            for task_id in due_ids:
                if task_id in self._queued_ids or task_id == self._active_task_id:
                    continue
                try:
                    self._queue.put_nowait(task_id)
                except Full:
                    return
                self._queued_ids.add(task_id)
                admitted += 1
                if admitted >= min(self._claim_size, free):
                    return

    def run_once(self) -> bool:
        """Claim and execute at most one durable due task."""
        return self._run_once(require_running=False)

    def _run_once(self, *, require_running: bool) -> bool:
        """Execute one task, optionally refusing claims after stop begins."""
        self._fill_due_queue()
        with self._state_lock:
            if require_running and not self._running:
                return False
            try:
                task_id = self._queue.get_nowait()
            except Empty:
                return False
            self._queued_ids.discard(task_id)
            self._active_task_id = task_id
        try:
            info = self._task_store.claim(
                task_id,
                self._clock(),
                lease_seconds=self._lease_seconds,
            )
            if info is None:
                return False
            self._execute_claimed(info)
            return True
        finally:
            with self._state_lock:
                self._active_task_id = None
            self._queue.task_done()

    def _execute_task(self, task: dict) -> None:
        """Execute a single task with retry support.

        Steps:
        1. Update status to RUNNING.
        2. Dispatch to the correct handler.
        3. On success: COMPLETED + optional callback.
        4. On failure: persist a due time for retry, or mark FAILED.
        """
        task_id = task["id"]
        info = self._task_store.claim(
            task_id,
            self._clock(),
            lease_seconds=self._lease_seconds,
        )
        if info is not None:
            self._execute_claimed(info)

    def _execute_claimed(self, info: TaskInfo) -> None:
        task_id = info.task_id
        task_type = info.task_type
        payload = info.payload or {}
        completed_result: Any = None
        completed = False

        try:
            completed_result = self._dispatch(task_type, payload)
            info.status = TaskStatus.COMPLETED
            info.result = completed_result
            info.completed_at = self._clock()
            info.lease_until = None
            info.next_attempt_at = None
            info.last_error_code = None
            info.error = None
            completed = True

        except BaseException:
            info.retry_count += 1
            info.result = None
            info.lease_until = None
            info.last_error_code = "task_failed"
            info.error = "task_failed"
            if info.retry_count <= info.max_retries:
                info.status = TaskStatus.PENDING
                delay = min(2**info.retry_count, 30)  # exponential backoff, max 30s
                info.next_attempt_at = self._clock() + timedelta(seconds=delay)
                logger.info("worker_task_retry_scheduled")
            else:
                info.status = TaskStatus.FAILED
                info.next_attempt_at = None
                self._callbacks.pop(task_id, None)
                logger.error("worker_task_dead_lettered")

        self._task_store.save(info)
        self._wake.set()
        if not completed:
            return
        self._completed_since_start += 1
        callback = self._callbacks.pop(task_id, None)
        if callback is not None:
            try:
                callback(completed_result)
            except Exception:
                logger.warning("worker_callback_failed")

    # ── Dispatch & task handlers ────────────────────────────────────

    def _dispatch(self, task_type: BackgroundTask, payload: dict) -> Any:
        """Route a task to its handler."""
        handlers = {
            BackgroundTask.EXTRACT_DOCUMENT: self._handle_extract,
            BackgroundTask.BUILD_INDEX: self._handle_build_index,
            BackgroundTask.COMPILE_WIKI: self._handle_compile_wiki,
            BackgroundTask.REFRESH_VECTOR: self._handle_refresh_vector,
            BackgroundTask.COMPACTION: self._handle_compaction,
        }
        handler = handlers.get(task_type)
        if handler is None:
            raise ValueError(f"Unknown task type: {task_type}")
        return handler(payload)

    def _resolve_store(self) -> Any:
        """Return the injected store, lazily creating the default Lite store."""
        if self._store is None:
            from memplex.storage.lite.store import LiteMemoryStore

            self._store = LiteMemoryStore()
        return self._store

    def _resolve_config(self) -> Any:
        """Return the injected config, lazily loading the default one."""
        if self._config is None:
            from memplex.config import load_config

            self._config = load_config()
        return self._config

    def _handle_extract(self, payload: dict) -> dict:
        """Handle EXTRACT_DOCUMENT tasks: run CoreEngine extraction.

        The payload describes the source document (``type``, ``content``,
        ``source_path`` or ``url``).  Persistence of the extracted
        Functions is the application layer's job; this handler reports
        extraction counts.
        """
        from memplex.core.engine import CoreEngine
        from memplex.models import SourceDocument

        source = SourceDocument(
            type=payload.get("type", "text"),
            content=payload.get("content"),
            source_path=payload.get("source_path"),
            url=payload.get("url"),
            content_hash=payload.get("content_hash"),
        )
        engine = self._engine
        if engine is None:
            try:
                engine = CoreEngine(store=self._resolve_store())
            except Exception:
                engine = CoreEngine(store=None)
            self._engine = engine
        extracted = engine.extract(source)
        func_count = len(getattr(extracted, "functions", []) or [])
        edge_count = len(getattr(getattr(extracted, "graph", None), "edges", []) or [])
        logger.info(
            "Extract document %s: %d functions, %d edges",
            payload.get("source_id", "<unknown>"),
            func_count,
            edge_count,
        )
        return {"status": "completed", "extracted": func_count, "edges": edge_count}

    def _handle_build_index(self, payload: dict) -> dict:
        """Handle BUILD_INDEX tasks: force-rebuild the FTS sidecar index.

        The Lite backend keeps a SQLite FTS5 sidecar next to the JSON
        store; :meth:`SQLiteFTSIndex.rebuild` resets its per-function
        signature cache and re-indexes every function.  Backends without
        a rebuildable sidecar are skipped gracefully.
        """
        store = self._resolve_store()
        fts = getattr(store, "_fts_index", None)
        rebuild = getattr(fts, "rebuild", None)
        if not callable(rebuild):
            logger.info("Build index: store has no rebuildable FTS sidecar; skipping")
            return {"status": "skipped", "reason": "no_fts_sidecar", "indexed": 0}
        rebuild()
        indexed = len(getattr(store, "_functions", {}) or {})
        logger.info("Build index: rebuilt FTS sidecar for %d functions", indexed)
        return {"status": "completed", "indexed": indexed}

    def _handle_compile_wiki(self, payload: dict) -> dict:
        """Handle COMPILE_WIKI tasks: compile the store into wiki pages.

        Runs :class:`memplex.wiki.compiler.WikiCompiler.compile_all` against
        the worker's store and writes every page to disk.  The wiki output
        directory comes from ``wiki.dir`` in the config (overridable per
        task via the payload's ``wiki_dir``).  When ``wiki.enabled`` is
        false the task is skipped gracefully.
        """
        from memplex.wiki.compiler import WikiCompiler

        config = self._resolve_config()
        wiki_config = getattr(config, "wiki", None)
        if wiki_config is not None and not wiki_config.enabled:
            logger.info("Compile wiki: wiki layer disabled in config; skipping")
            return {"status": "skipped", "reason": "wiki_disabled", "pages": 0}

        wiki_dir_raw = payload.get("wiki_dir") or (
            wiki_config.dir if wiki_config is not None else "~/.memplex/wiki"
        )
        wiki_dir = Path(wiki_dir_raw).expanduser()

        compiler = WikiCompiler(
            store=self._resolve_store(),
            wiki_dir=wiki_dir,
            graph_config=getattr(config, "graph", None),
        )
        pages = compiler.compile_all()
        for page in pages:
            if page.page_id == "index":
                compiler.write_index(page)
            else:
                compiler.write_page(page)

        logger.info("Compile wiki: wrote %d pages to %s", len(pages), wiki_dir)
        return {"status": "completed", "pages": len(pages), "wiki_dir": str(wiki_dir)}

    def _handle_refresh_vector(self, payload: dict) -> dict:
        """Handle REFRESH_VECTOR tasks via EmbeddingService.refresh*.

        With a ``func_id`` in the payload only that Function is
        re-embedded; otherwise all Functions are refreshed in batches.
        """
        from memplex.retrieval.embedding import EmbeddingService
        from memplex.storage.vector import InMemoryVectorStore

        service = self._embedding_service
        if service is None:
            service = EmbeddingService(
                storage=self._resolve_store(),
                vector_store=InMemoryVectorStore(),
            )
            self._embedding_service = service

        func_id = payload.get("func_id")
        if func_id:
            service.refresh(func_id)
            logger.info("Refresh vector: refreshed %s", func_id)
            return {"status": "completed", "refreshed": 1}
        result = service.refresh_all()
        logger.info("Refresh vector: refreshed %d functions", result.refreshed)
        return {"status": "completed", "refreshed": result.refreshed}

    def _handle_compaction(self, payload: dict) -> dict:
        """Handle COMPACTION tasks.

        When a CompactionPipeline is injected at init time, this handler
        delegates to it.  Otherwise falls back to a stub.
        """
        trigger = payload.get("trigger", "manual")
        scope = payload.get("scope", "global")
        logger.info("Compaction triggered: trigger=%s scope=%s", trigger, scope)
        if self._compaction_pipeline is not None:
            compaction_scope = CompactionScope(scope)
            return asyncio.run(self._compaction_pipeline.run(compaction_scope))
        return {"status": "completed", "trigger": trigger, "scope": scope}

    # ── Recovery ────────────────────────────────────────────────────

    def _recover_pending_tasks(self) -> None:
        """Populate only the bounded ready queue; durable state is authoritative."""
        self._fill_due_queue()
