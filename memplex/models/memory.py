"""Memory node types: MemoryNode base + Function, Fact, Preference, Observation."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional

from .graph import GraphEdge, domain_node_id
from .misc import FieldValue, validate_domain, validate_func_id
from .source import SourceType

if TYPE_CHECKING:
    from memplex.sync_protocol import SyncNodeType

# Valid Observation.category values.  Plain string constants (no Literal
# precedent in the codebase); ``note`` is the default / fallback bucket.
# Consumed by ``memplex.intent.classify_observation`` and the optional
# ``MemoryStore.list_observations(category=...)`` filter.
OBSERVATION_CATEGORIES = ("bugfix", "decision", "change", "discovery", "note")
DEFAULT_OBSERVATION_CATEGORY = "note"
SYNCABLE_MEMORY_TYPES = ("function", "fact", "preference", "observation")


def sync_node_type_for_memory(memory: "MemoryNode") -> "SyncNodeType":
    """Return the frozen sync node type for a durable memory object.

    The protocol intentionally keeps the entity-key codec independent of
    model classes.  This small adapter is the only model-side mapping, so a
    storage implementation cannot silently invent a fifth ordinary node type.
    """
    from memplex.sync_protocol import SyncNodeType

    if not isinstance(memory, MemoryNode):
        raise TypeError("syncable memory must be a MemoryNode")
    if memory.memory_type not in SYNCABLE_MEMORY_TYPES:
        raise ValueError("memory type is not syncable")
    return SyncNodeType(memory.memory_type)


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
    tenant_id: Optional[str] = None
    owner_subject_id: Optional[str] = None
    workspace_id: Optional[str] = None
    visibility: Optional[str] = None
    provenance: Dict[str, str] = field(default_factory=dict)
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
    namespace: Dict[str, str] = field(default_factory=dict)

    def _base_to_dict(self) -> Dict[str, Any]:
        """Serialize the MemoryNode base fields shared by all memory types."""
        return {
            "id": self.id,
            "memory_type": self.memory_type,
            "name": self.name,
            "domain": self.domain,
            "confidence": self.confidence,
            "source_type": (
                self.source_type.value
                if isinstance(self.source_type, SourceType)
                else self.source_type
            ),
            "owner": self.owner,
            "tenant_id": self.tenant_id,
            "owner_subject_id": self.owner_subject_id,
            "workspace_id": self.workspace_id,
            "visibility": self.visibility,
            "provenance": dict(self.provenance),
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "origin_session": self.origin_session,
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "source_paragraphs": list(self.source_paragraphs),
            "needs_review": self.needs_review,
            "needs_review_until": self.needs_review_until,
            "content_hash": self.content_hash,
            "namespace": dict(self.namespace),
        }

    @staticmethod
    def _base_from_dict(d: Dict[str, Any]) -> Dict[str, Any]:
        """Build constructor kwargs for MemoryNode base fields from a dict."""
        source_type = d.get("source_type", "wiki")
        if isinstance(source_type, str):
            try:
                source_type = SourceType(source_type)
            except ValueError:
                source_type = SourceType.WIKI
        return {
            "id": d.get("id", ""),
            "name": d.get("name", ""),
            "domain": d.get("domain"),
            "confidence": d.get("confidence", 1.0),
            "source_type": source_type,
            "owner": d.get("owner"),
            "tenant_id": d.get("tenant_id"),
            "owner_subject_id": d.get("owner_subject_id"),
            "workspace_id": d.get("workspace_id"),
            "visibility": d.get("visibility"),
            "provenance": dict(d.get("provenance", {})),
            "version": d.get("version", 1),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "origin_session": d.get("origin_session"),
            "access_count": d.get("access_count", 0),
            "last_accessed_at": d.get("last_accessed_at"),
            "source_paragraphs": list(d.get("source_paragraphs", [])),
            "needs_review": d.get("needs_review", False),
            "needs_review_until": d.get("needs_review_until"),
            "content_hash": d.get("content_hash"),
            "namespace": dict(d.get("namespace", {})),
        }


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
        validate_domain(self.domain)
        if not self.created_at:
            from datetime import datetime, timezone

            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> Dict[str, Any]:
        """Standard serialization covering every field.

        This is the convergence target for the per-backend serializers
        (lite / postgres / http_api), which historically drifted on
        ``needs_review_until`` / ``priority_from_source`` /
        ``source_authority`` and on FieldValue sub-fields.
        """
        d = self._base_to_dict()
        d.update(
            {
                "name_normalized": self.name_normalized,
                "trigger": [fv.to_dict() for fv in self.trigger],
                "condition": [fv.to_dict() for fv in self.condition],
                "action": [fv.to_dict() for fv in self.action],
                "benefit": [fv.to_dict() for fv in self.benefit],
                "attributes": dict(self.attributes),
                "cross_references": list(self.cross_references),
                "priority_from_source": self.priority_from_source,
                "source_authority": self.source_authority,
            }
        )
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Function":
        """Inverse of :meth:`to_dict`; tolerant of missing keys."""
        kwargs = cls._base_from_dict(d)
        kwargs.update(
            {
                "memory_type": d.get("memory_type", "function"),
                "name_normalized": d.get("name_normalized", ""),
                "trigger": [FieldValue.from_dict(fv) for fv in d.get("trigger", [])],
                "condition": [FieldValue.from_dict(fv) for fv in d.get("condition", [])],
                "action": [FieldValue.from_dict(fv) for fv in d.get("action", [])],
                "benefit": [FieldValue.from_dict(fv) for fv in d.get("benefit", [])],
                "attributes": dict(d.get("attributes", {})),
                "cross_references": list(d.get("cross_references", [])),
                "priority_from_source": d.get("priority_from_source"),
                "source_authority": d.get("source_authority"),
            }
        )
        return cls(**kwargs)


def validate_belongs_to_edges(functions, edges) -> None:
    """Validate the shared virtual-domain edge contract.

    ``BELONGS_TO`` is the one graph relation that intentionally targets a
    virtual node. Its source must still resolve to a durable Function in the
    supplied complete graph and its target must use GraphBuilder's exact
    domain-node mapping. Other edge referential-integrity policy is outside
    this narrow helper.
    """
    by_id: Dict[str, Function] = {}
    for function in functions:
        if not isinstance(function, Function):
            raise ValueError("BELONGS_TO source 必须是 Function")
        validate_func_id(function.id)
        validate_domain(function.domain)
        by_id[function.id] = function
    for edge in edges:
        if not isinstance(edge, GraphEdge):
            raise ValueError("BELONGS_TO edge 必须是 GraphEdge")
        if edge.edge_type != "BELONGS_TO":
            continue
        source = by_id.get(edge.source)
        if source is None:
            raise ValueError("BELONGS_TO source 必须是持久 Function")
        domain = validate_domain(source.domain)
        if not domain or edge.target != domain_node_id(domain):
            raise ValueError("BELONGS_TO target 必须匹配 source domain")


@dataclass
class Fact(MemoryNode):
    """Declarative memory: subject → predicate → object."""

    memory_type: str = "fact"
    subject: str = ""
    predicate: str = ""
    object_: str = ""
    valid_until: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Standard serialization covering every field (base + fact).

        The ``object_`` field is serialized under the key ``"object"``
        (trailing underscore is a Python keyword workaround, not part of
        the external JSON contract).
        """
        d = self._base_to_dict()
        d.update(
            {
                "subject": self.subject,
                "predicate": self.predicate,
                "object": self.object_,
                "valid_until": self.valid_until,
            }
        )
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Fact":
        """Inverse of :meth:`to_dict`; tolerant of missing keys.

        Accepts both ``"object"`` (canonical, emitted by
        :meth:`to_dict`) and the legacy/ Python-side ``"object_"`` key.
        """
        kwargs = cls._base_from_dict(d)
        kwargs.update(
            {
                "memory_type": d.get("memory_type", "fact"),
                "subject": d.get("subject", ""),
                "predicate": d.get("predicate", ""),
                "object_": d.get("object", d.get("object_", "")),
                "valid_until": d.get("valid_until"),
            }
        )
        return cls(**kwargs)


@dataclass
class Preference(MemoryNode):
    """User/agent preference memory."""

    memory_type: str = "preference"
    aspect: str = ""
    preference: str = ""
    subject_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Standard serialization covering every field (base + preference)."""
        d = self._base_to_dict()
        d.update(
            {
                "aspect": self.aspect,
                "preference": self.preference,
                "subject_id": self.subject_id,
            }
        )
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Preference":
        """Inverse of :meth:`to_dict`; tolerant of missing keys."""
        kwargs = cls._base_from_dict(d)
        kwargs.update(
            {
                "memory_type": d.get("memory_type", "preference"),
                "aspect": d.get("aspect", ""),
                "preference": d.get("preference", ""),
                "subject_id": d.get("subject_id"),
            }
        )
        return cls(**kwargs)


@dataclass
class Observation(MemoryNode):
    """Runtime observation event memory."""

    memory_type: str = "observation"
    event: str = ""
    context: str = ""
    observed_at: Optional[str] = None
    actor: str = "system"
    # Structured category (see OBSERVATION_CATEGORIES): bugfix | decision |
    # change | discovery | note.  Defaults to "note"; older serialized data
    # without the key loads as "note" via from_dict.
    category: str = DEFAULT_OBSERVATION_CATEGORY

    def to_dict(self) -> Dict[str, Any]:
        """Standard serialization covering every field (base + observation)."""
        d = self._base_to_dict()
        d.update(
            {
                "event": self.event,
                "context": self.context,
                "observed_at": self.observed_at,
                "actor": self.actor,
                "category": self.category,
            }
        )
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Observation":
        """Inverse of :meth:`to_dict`; tolerant of missing keys."""
        kwargs = cls._base_from_dict(d)
        kwargs.update(
            {
                "memory_type": d.get("memory_type", "observation"),
                "event": d.get("event", ""),
                "context": d.get("context", ""),
                "observed_at": d.get("observed_at"),
                "actor": d.get("actor", "system"),
                "category": d.get("category", DEFAULT_OBSERVATION_CATEGORY),
            }
        )
        return cls(**kwargs)


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
