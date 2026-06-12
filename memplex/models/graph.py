"""Graph types: EdgeType, GraphEdge, GraphData."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


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
    evidence: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None


@dataclass
class GraphData:
    nodes: list = field(default_factory=list)  # List[MemoryNode]
    edges: List[GraphEdge] = field(default_factory=list)
