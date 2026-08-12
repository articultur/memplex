"""Durable background-task repository contract.

The contract provides lease fencing for an **at-least-once** runner.  A handler
may execute more than once after a process pause or lease expiry; only the
holder of the current ``lease_id`` may publish completion or schedule retry.
Callers must therefore keep handlers idempotent where externally observable
side effects matter.  This module deliberately does not promise handler-level
exactly-once execution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from memplex.models import TaskInfo, TaskStatus


class WorkerQueueFull(RuntimeError):
    """Raised when durable non-terminal worker admission is exhausted."""


class TaskRepository(ABC):
    """Persistence boundary used by the at-least-once background runner."""

    @abstractmethod
    def admit_pending(self, info: TaskInfo, *, capacity: int) -> TaskInfo:
        """Atomically reserve capacity and persist one pending task."""

    @abstractmethod
    def get(self, task_id: str) -> TaskInfo | None:
        """Return one task snapshot."""

    @abstractmethod
    def list_by_status(self, *statuses: TaskStatus) -> list[TaskInfo]:
        """Return task snapshots in the requested states."""

    def count_by_status(self, *statuses: TaskStatus) -> int:
        return len(self.list_by_status(*statuses))

    @abstractmethod
    def replay_failed_atomic(
        self,
        task_id: str,
        *,
        capacity: int,
        now: datetime | None = None,
    ) -> TaskInfo | None:
        """Reset one dead letter while atomically reserving capacity."""

    @abstractmethod
    def cancel_pending(self, task_id: str) -> bool:
        """Cancel only a still-pending task."""

    @abstractmethod
    def due_task_ids(
        self, now: datetime | None = None, *, limit: int
    ) -> list[str]:
        """Return bounded due-work hints; claiming remains authoritative."""

    @abstractmethod
    def claim(
        self,
        task_id: str,
        now: datetime | None = None,
        *,
        lease_seconds: int,
    ) -> TaskInfo | None:
        """Claim one exact due task using a new fencing token."""

    @abstractmethod
    def claim_due(
        self,
        *,
        limit: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> list[TaskInfo]:
        """Atomically claim up to ``limit`` due tasks."""

    @abstractmethod
    def complete(
        self,
        task_id: str,
        lease_id: str,
        result: Any,
        *,
        now: datetime | None = None,
    ) -> TaskInfo | None:
        """Publish success only while ``lease_id`` is current and unexpired."""

    @abstractmethod
    def fail(
        self,
        task_id: str,
        lease_id: str,
        *,
        error_code: str,
        retry_delay_seconds: int,
        now: datetime | None = None,
    ) -> TaskInfo | None:
        """Fence a failure into retry or terminal dead-letter state."""

    @abstractmethod
    def status_counts(self) -> dict[TaskStatus, int]:
        """Return exact durable counts for every status."""
