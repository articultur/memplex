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
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Thread, Timer
from typing import Any, Callable, Dict, List, Optional

from memplex.compaction import CompactionPipeline, CompactionScope
from memplex.models import BackgroundTask, TaskInfo, TaskStatus

logger = logging.getLogger(__name__)


def _generate_uuid() -> str:
    return uuid.uuid4().hex


def _json_serializer(obj: Any) -> Any:
    """JSON serializer for non-standard types."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


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
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ── Persistence ─────────────────────────────────────────────────

    def _load(self) -> None:
        """Load tasks from disk (no-op if file does not exist)."""
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._tasks = data.get("tasks", {})
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load task store from %s: %s", self._path, exc)
                self._tasks = {}

    def _save(self) -> None:
        """Flush tasks to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump({"tasks": self._tasks}, fh, default=_json_serializer, indent=2)
        except OSError as exc:
            logger.error("Failed to persist task store to %s: %s", self._path, exc)

    # ── CRUD ────────────────────────────────────────────────────────

    def save(self, info: TaskInfo) -> None:
        """Save or update a TaskInfo."""
        self._tasks[info.task_id] = self._info_to_dict(info)
        self._save()

    def get(self, task_id: str) -> Optional[TaskInfo]:
        """Retrieve a TaskInfo by ID, or ``None``."""
        data = self._tasks.get(task_id)
        if data is None:
            return None
        return self._dict_to_info(data)

    def list_by_status(self, *statuses: TaskStatus) -> List[TaskInfo]:
        """Return all tasks matching any of the given statuses."""
        status_values = {s.value for s in statuses}
        result: List[TaskInfo] = []
        for data in self._tasks.values():
            if data.get("status") in status_values:
                result.append(self._dict_to_info(data))
        return result

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
        }

    @staticmethod
    def _dict_to_info(data: Dict[str, Any]) -> TaskInfo:
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
    ) -> None:
        self._task_store = TaskStore(storage_path)
        self._queue: Queue = Queue()
        self._running: bool = False
        self._worker_thread: Optional[Thread] = None
        self._compaction_pipeline = compaction_pipeline
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

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background worker daemon thread."""
        if self._running:
            return
        self._running = True
        self._worker_thread = Thread(target=self._run_loop, daemon=True)
        self._worker_thread.start()
        logger.info("BackgroundWorker started")

    def stop(self, timeout: float = 30.0) -> None:
        """Gracefully stop the worker, waiting up to *timeout* seconds."""
        self._running = False
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning("BackgroundWorker did not stop within %.1fs", timeout)
        logger.info("BackgroundWorker stopped")

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
        task_id = _generate_uuid()
        info = TaskInfo(
            task_id=task_id,
            task_type=task,
            status=TaskStatus.PENDING,
            created_at=datetime.now(),
            payload=payload,
        )
        self._task_store.save(info)
        self._queue.put({"id": task_id, "task": task, "payload": payload, "callback": callback})
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

    # ── Worker loop ─────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main loop: dequeue tasks and execute them."""
        while self._running:
            try:
                task = self._queue.get(timeout=5.0)
                self._execute_task(task)
            except Empty:
                continue
            except Exception as exc:
                logger.error("Unexpected error in worker loop: %s", exc, exc_info=True)

    def _execute_task(self, task: dict) -> None:
        """Execute a single task with retry support.

        Steps:
        1. Update status to RUNNING.
        2. Dispatch to the correct handler.
        3. On success: COMPLETED + optional callback.
        4. On failure: retry with exponential backoff (Timer), or mark FAILED.
        """
        task_id = task["id"]
        task_type = task["task"]
        payload = task.get("payload", {})

        info = self._task_store.get(task_id)
        if info is None:
            return

        info.status = TaskStatus.RUNNING
        self._task_store.save(info)

        try:
            result = self._dispatch(task_type, payload)
            info.status = TaskStatus.COMPLETED
            info.result = result
            info.completed_at = datetime.now()

            callback = task.get("callback")
            if callback is not None:
                try:
                    callback(result)
                except Exception as cb_exc:
                    logger.warning("Callback error for task %s: %s", task_id, cb_exc)

        except Exception as exc:
            if info.retry_count < info.max_retries:
                info.retry_count += 1
                info.status = TaskStatus.PENDING
                self._task_store.save(info)
                delay = min(2**info.retry_count, 30)  # exponential backoff, max 30s
                logger.info(
                    "Retrying task %s (%s) in %ds (attempt %d/%d)",
                    task_id,
                    task_type.value,
                    delay,
                    info.retry_count,
                    info.max_retries,
                )
                Timer(
                    delay,
                    self._queue.put,
                    args=[{"id": task_id, "task": task_type, "payload": payload}],
                ).start()
                return
            info.status = TaskStatus.FAILED
            info.error = str(exc)
            logger.error("Task %s failed permanently: %s", task_id, exc)

        finally:
            self._task_store.save(info)

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

    def _handle_extract(self, payload: dict) -> dict:
        """Handle EXTRACT_DOCUMENT tasks.

        Stub: concrete extraction logic is wired by the application layer
        (MemplexService or a plugin).  This method provides the scaffolding.
        """
        logger.info("Extract document: %s", payload.get("source_id", "<unknown>"))
        return {"status": "completed", "extracted": True}

    def _handle_build_index(self, payload: dict) -> dict:
        """Handle BUILD_INDEX tasks."""
        logger.info("Build index: %s", payload.get("func_id", "<batch>"))
        return {"status": "completed", "indexed": True}

    def _handle_compile_wiki(self, payload: dict) -> dict:
        """Handle COMPILE_WIKI tasks."""
        logger.info("Compile wiki: %s", payload.get("domain", "<all>"))
        return {"status": "completed", "compiled": True}

    def _handle_refresh_vector(self, payload: dict) -> dict:
        """Handle REFRESH_VECTOR tasks."""
        logger.info("Refresh vector: %s", payload.get("func_id", "<batch>"))
        return {"status": "completed", "refreshed": True}

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
        """Re-queue tasks that were pending or running when the process died."""
        pending = self._task_store.list_by_status(TaskStatus.PENDING, TaskStatus.RUNNING)
        for t in pending:
            if t.status == TaskStatus.RUNNING:
                # Only re-queue RUNNING tasks that have exceeded a 1-hour timeout
                elapsed = (datetime.now() - t.created_at).total_seconds()
                if elapsed > 3600:
                    t.status = TaskStatus.PENDING
                    self._task_store.save(t)
                    self._queue.put(
                        {
                            "id": t.task_id,
                            "task": t.task_type,
                            "payload": t.payload or {},
                        }
                    )
                else:
                    logger.warning(
                        "Task %s (%s) was RUNNING before shutdown; "
                        "skipping re-queue (not timed out)",
                        t.task_id,
                        t.task_type.value,
                    )
            else:
                self._queue.put(
                    {
                        "id": t.task_id,
                        "task": t.task_type,
                        "payload": t.payload or {},
                    }
                )
