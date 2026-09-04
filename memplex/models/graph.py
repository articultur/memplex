"""Graph types: EdgeType, GraphEdge, GraphData."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


def domain_node_id(domain: str) -> str:
    """Return GraphBuilder's stable virtual-domain node identifier.

    This is intentionally a mechanical legacy mapping: callers decide
    whether a domain is present, then spaces (and only ASCII spaces) become
    underscores and the complete string lowercases.  Do not strip/collapse
    whitespace here because existing persisted graph edges depend on those
    exact identifiers.
    """
    if type(domain) is not str:
        raise ValueError("Function domain 必须是字符串")
    return "domain_" + domain.replace(" ", "_").lower()


class EdgeType(Enum):
    REFERENCES = "REFERENCES"
    ASSOCIATED_WITH = "ASSOCIATED_WITH"
    SEMANTIC_SIMILAR = "SEMANTIC_SIMILAR"
    BELONGS_TO = "BELONGS_TO"
    IMPLEMENTS = "IMPLEMENTS"
    DEPENDS_ON = "DEPENDS_ON"
    CONFLICTS_WITH = "CONFLICTS_WITH"
    EVOLVED_FROM = "EVOLVED_FROM"


@dataclass
class GraphEdge:
    source: str
    target: str
    edge_type: str
    weight: float = 1.0
    evidence: list[str] = field(default_factory=list)
    created_at: datetime | None = None


@dataclass
class GraphData:
    nodes: list = field(default_factory=list)  # List[MemoryNode]
    edges: list[GraphEdge] = field(default_factory=list)
