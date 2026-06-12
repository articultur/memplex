"""Memory node types: MemoryNode base + Function, Fact, Preference, Observation."""

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

from .misc import FieldValue, validate_func_id
from .source import SourceType


@dataclass(kw_only=True)
class MemoryNode:
    """Abstract base for all memory types."""

    id: str = ""
    memory_type: str = ""  # function | fact | preference | observation
    name: str = ""
    domain: Optional[str] = None
    confidence: float = 1.0
    source_type: SourceType = SourceType.WIKI
    owner: Optional[str] = None
    version: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    origin_session: Optional[str] = None
    access_count: int = 0
    last_accessed_at: Optional[str] = None
    source_paragraphs: List[str] = field(default_factory=list)
    needs_review: bool = False
    needs_review_until: Optional[str] = None
    content_hash: Optional[str] = None


@dataclass
class Function(MemoryNode):
    """Procedural memory: actions/flows/interfaces with trigger/condition/action/benefit."""

    memory_type: str = "function"
    trigger: List[FieldValue] = field(default_factory=list)
    condition: List[FieldValue] = field(default_factory=list)
    action: List[FieldValue] = field(default_factory=list)
    benefit: List[FieldValue] = field(default_factory=list)
    name_normalized: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    cross_references: List[Dict] = field(default_factory=list)
    priority_from_source: Optional[str] = None
    source_authority: Optional[str] = None

    MAX_VALUES_PER_FIELD: ClassVar[int] = 20

    def __post_init__(self):
        validate_func_id(self.id)
        if not self.created_at:
            from datetime import datetime, timezone

            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.updated_at:
            from datetime import datetime

            self.updated_at = self.created_at


@dataclass
class Fact(MemoryNode):
    """Declarative memory: subject → predicate → object."""

    memory_type: str = "fact"
    subject: str = ""
    predicate: str = ""
    object_: str = ""
    valid_until: Optional[str] = None


@dataclass
class Preference(MemoryNode):
    """User/agent preference memory."""

    memory_type: str = "preference"
    aspect: str = ""
    preference: str = ""
    subject_id: Optional[str] = None


@dataclass
class Observation(MemoryNode):
    """Runtime observation event memory."""

    memory_type: str = "observation"
    event: str = ""
    context: str = ""
    observed_at: Optional[str] = None
    actor: str = "system"


# Type alias: Memory = MemoryNode (emphasizes role in compaction pipeline)
Memory = MemoryNode


def create_memory_node(memory_type: str, **kwargs) -> MemoryNode:
    """Factory: create the correct MemoryNode subclass by type string."""
    cls_map = {
        "function": Function,
        "fact": Fact,
        "preference": Preference,
        "observation": Observation,
    }
    cls = cls_map.get(memory_type)
    if not cls:
        raise ValueError(f"Unknown memory_type: {memory_type!r}")
    return cls(**kwargs)
