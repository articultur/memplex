"""Task and compaction types."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class BackgroundTask(Enum):
    EXTRACT_DOCUMENT = "extract_document"
    BUILD_INDEX = "build_index"
    COMPILE_WIKI = "compile_wiki"
    REFRESH_VECTOR = "refresh_vector"
    COMPACTION = "compaction"


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    task_id: str
    task_type: BackgroundTask
    status: TaskStatus
    created_at: datetime
    completed_at: datetime | None = None
    payload: dict | None = None
    result: Any = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    next_attempt_at: datetime | None = None
    lease_until: datetime | None = None
    lease_id: str | None = None
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerDrainResult:
    drained: bool
    completed: int
    pending: int
    leased: int
    dead_letters: int
    deadline_exceeded: bool

    def __post_init__(self) -> None:
        if type(self.drained) is not bool or type(self.deadline_exceeded) is not bool:
            raise TypeError("worker drain flags must be exact bools")
        for name in ("completed", "pending", "leased", "dead_letters"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact int")

    def to_dict(self) -> dict[str, object]:
        return {
            "drained": self.drained,
            "completed": self.completed,
            "pending": self.pending,
            "leased": self.leased,
            "dead_letters": self.dead_letters,
            "deadline_exceeded": self.deadline_exceeded,
        }


class CompactionScope(Enum):
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


@dataclass
class CompactionStageResult:
    stage: str
    processed: int
    removed: int
    merged: int
    duration_ms: int
    abort: bool = False


@dataclass
class CompactionResult:
    total_processed: int
    total_removed: int
    total_merged: int
    duration_ms: int
    stages: list[CompactionStageResult] = field(default_factory=list)
    skipped: bool = False
