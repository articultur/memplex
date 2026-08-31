"""Search types: QueryScope, SearchResult, SearchFilters, QueryResult."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from memplex.models.source import SourceType


class QueryScope(Enum):
    IMMEDIATE = "immediate"
    SYNTHESIS = "synthesis"
    RELATION = "relation"
    ALL = "all"


@dataclass
class SearchResult:
    func_id: str
    name: str
    domain: str
    relevance_score: float
    summary: str
    source_type: SourceType = SourceType.WIKI
    # Producers pass ISO strings (Lite/Postgres stores) or datetimes
    # (multi_path fusion); the field is a pass-through for display/JSON.
    created_at: str | datetime | None = None
    updated_at: str | datetime | None = None
    origin: str = ""
    vector_cache: Any = None
    token_estimate: int = 0
    graph_context: Optional[Dict] = None


@dataclass
class SearchFilters:
    domain: Optional[List[str]] = None
    source_type: Optional[List[SourceType]] = None
    confidence_min: Optional[float] = None
    updated_after: Optional[datetime] = None
    updated_before: Optional[datetime] = None
    needs_review: Optional[bool] = None
    owner: Optional[str] = None


@dataclass
class QueryResult:
    results: List[SearchResult]
    scope: QueryScope
    latency_ms: int
    tokens_used: int = 0
    max_tokens: int = 0
    truncated: bool = False
    explanation: Optional[Dict[str, Any]] = None
