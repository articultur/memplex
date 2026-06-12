"""Miscellaneous types: FieldValue, ChangelogEvent, MergeResult, and others."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from memplex.models.graph import GraphData

# ── Function ID validation ──────────────────────────────────────

MAX_FUNC_ID_LENGTH = 128
_FUNC_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_func_id(func_id: str) -> str:
    if len(func_id) > MAX_FUNC_ID_LENGTH:
        raise ValueError(f"Function ID 过长: {len(func_id)} > {MAX_FUNC_ID_LENGTH}")
    if not _FUNC_ID_PATTERN.fullmatch(func_id):
        raise ValueError(f"Function ID 含非法字符: {func_id!r}")
    return func_id


# ── FieldValue (multi-value field entry) ────────────────────────


@dataclass
class FieldValue:
    desc: str
    sources: List[str] = field(default_factory=list)
    source_method: str = "rule_based"  # rule_based | llm_semantic | manual
    weight: float = 1.0
    observation: Optional[float] = None
    created_at: Optional[datetime] = None
    status: str = "active"  # active | deprecated | disputed


# ── Auxiliary types ─────────────────────────────────────────────


@dataclass
class ExtractedData:
    functions: list = field(default_factory=list)  # List[MemoryNode]
    graph: GraphData = field(default_factory=GraphData)
    delta: bool = False


@dataclass
class MergeResult:
    merged: bool
    new_functions: int = 0
    updated_functions: int = 0
    new_conflicts: int = 0
    new_edges: int = 0


@dataclass
class BatchResult:
    total: int = 0
    succeeded: int = 0
    failed_items: List[Dict] = field(default_factory=list)


@dataclass
class ParagraphDelta:
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)


@dataclass
class DedupResult:
    original_count: int
    final_count: int
    exact_removed: int
    semantic_removed: int
    deduplicated: list = field(default_factory=list)


@dataclass
class RefreshResult:
    total: int
    refreshed: int


@dataclass
class ValidationResult:
    valid: bool
    issue: Optional[str] = None
    truncated_content: Optional[str] = None


@dataclass
class UpdateResult:
    memory_id: str
    role: str
    old_value: Optional[str] = None
    new_value: str = ""
    version: int = 0
    success: bool = False
    error: Optional[str] = None


@dataclass
class StorageStats:
    total_functions: int
    total_edges: int
    total_observations: int
    storage_size_mb: float
    last_compaction: Optional[datetime] = None


# ── Changelog types ─────────────────────────────────────────────


@dataclass
class ChangelogEvent:
    func_id: str
    timestamp: datetime
    event_type: str  # created | updated | merged | field_added
    description: str
    source: str
    actor: str  # user | ai | system


# ── LLM types ──────────────────────────────────────────────────


class IntentType(Enum):
    IMMEDIATE = "immediate"
    SYNTHESIS = "synthesis"
    RELATION = "relation"
    ALL = "all"


@dataclass
class EnhancedQuery:
    original: str
    expanded: List[str] = field(default_factory=list)
    intent: str = "search"


@dataclass
class Summary:
    key_points: List[str] = field(default_factory=list)
    patterns: List[str] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)


# ── Incremental types ──────────────────────────────────────────


@dataclass
class IncrementalState:
    source_id: str
    last_hash: Optional[str] = None
    last_paragraphs: List[str] = field(default_factory=list)
    processed_at: Optional[datetime] = None


# ── Wiki types ─────────────────────────────────────────────────


@dataclass
class WikiPage:
    page_id: str
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass
class WikiIndex:
    pages: List[WikiPage] = field(default_factory=list)
    total: int = 0


@dataclass
class LintIssue:
    page_id: str
    severity: str  # error | warning
    message: str


@dataclass
class LintResult:
    total_pages: int
    issues: List[LintIssue] = field(default_factory=list)
    passed: bool = True
