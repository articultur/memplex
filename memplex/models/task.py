"""Task and compaction types."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional


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
    completed_at: Optional[datetime] = None
    payload: Optional[dict] = None
    result: Any = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3


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
    stages: List[CompactionStageResult] = field(default_factory=list)
    skipped: bool = False
