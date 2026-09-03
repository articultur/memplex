"""Miscellaneous types: FieldValue, ChangelogEvent, MergeResult, and others."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from memplex.models.graph import GraphData

# ── Function ID validation ──────────────────────────────────────

MAX_FUNC_ID_LENGTH = 128
_FUNC_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_func_id(func_id: str) -> str:
    if not isinstance(func_id, str):
        raise ValueError("Function ID 必须是字符串")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    # ``domain_`` is GraphBuilder's virtual-node namespace.  It must never
    # become durable Function state, otherwise a graph edge can be confused
    # with a real memory row.  This is deliberately case-sensitive to retain
    # compatibility with pre-existing upper-case IDs.
    if func_id.startswith("domain_"):
        raise ValueError(f"Function ID 使用了保留的 domain_ 命名空间: {func_id!r}")
    if len(func_id) > MAX_FUNC_ID_LENGTH:
        raise ValueError(f"Function ID 过长: {len(func_id)} > {MAX_FUNC_ID_LENGTH}")
    if not _FUNC_ID_PATTERN.fullmatch(func_id):
        raise ValueError(f"Function ID 含非法字符: {func_id!r}")
    return func_id


def validate_domain(domain: object) -> str | None:
    """Validate the persisted Function domain contract."""
    if domain is None:
        return None
    if type(domain) is not str:
        raise ValueError("Function domain 必须是字符串或 None")
    return domain


# ── FieldValue (multi-value field entry) ────────────────────────


@dataclass
class FieldValue:
    desc: str
    sources: list[str] = field(default_factory=list)
    source_method: str = "rule_based"  # rule_based | llm_semantic | manual
    weight: float = 1.0
    observation: float | None = None
    created_at: datetime | None = None
    status: str = "active"  # active | deprecated | disputed

    def to_dict(self) -> dict:
        """Standard serialization covering every field.

        This is the convergence target for the per-backend serializers
        (lite / postgres / http_api), which historically drifted on
        ``observation`` / ``created_at`` / ``status``.
        """
        return {
            "desc": self.desc,
            "sources": list(self.sources),
            "source_method": self.source_method,
            "weight": self.weight,
            "observation": self.observation,
            "created_at": (
                self.created_at.isoformat()
                if isinstance(self.created_at, datetime)
                else self.created_at
            ),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FieldValue":
        """Inverse of :meth:`to_dict`; tolerant of missing keys."""
        created_at = d.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return cls(
            desc=d.get("desc", ""),
            sources=list(d.get("sources", [])),
            source_method=d.get("source_method", "rule_based"),
            weight=d.get("weight", 1.0),
            observation=d.get("observation"),
            created_at=created_at,
            status=d.get("status", "active"),
        )


# ── Auxiliary types ─────────────────────────────────────────────


@dataclass
class ExtractedData:
    functions: list = field(default_factory=list)  # List[MemoryNode]
    graph: GraphData = field(default_factory=GraphData)
    delta: bool = False
    # Fact / Preference nodes produced by intent classification of
    # fact/preference paragraphs (CoreEngine.extract). Default empty so
    # existing constructors stay source-compatible.
    facts: list = field(default_factory=list)  # List[Fact]
    preferences: list = field(default_factory=list)  # List[Preference]


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
    failed_items: list[dict] = field(default_factory=list)


@dataclass
class ParagraphDelta:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)


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
    issue: str | None = None
    truncated_content: str | None = None


@dataclass
class UpdateResult:
    memory_id: str
    role: str
    old_value: str | None = None
    new_value: str = ""
    version: int = 0
    success: bool = False
    error: str | None = None


@dataclass
class StorageStats:
    total_functions: int
    total_edges: int
    total_observations: int
    storage_size_mb: float
    last_compaction: datetime | None = None


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
    expanded: list[str] = field(default_factory=list)
    intent: str = "search"


@dataclass
class Summary:
    key_points: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)


# ── Incremental types ──────────────────────────────────────────


@dataclass
class IncrementalState:
    source_id: str
    last_hash: str | None = None
    last_paragraphs: list[str] = field(default_factory=list)
    processed_at: datetime | None = None


# ── Wiki types ─────────────────────────────────────────────────


@dataclass
class WikiPage:
    page_id: str
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass
class WikiIndex:
    pages: list[WikiPage] = field(default_factory=list)
    total: int = 0


@dataclass
class LintIssue:
    page_id: str
    severity: str  # error | warning
    message: str


@dataclass
class LintResult:
    total_pages: int
    issues: list[LintIssue] = field(default_factory=list)
    passed: bool = True
