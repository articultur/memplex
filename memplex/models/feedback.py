"""Feedback types: FeedbackVerdict, MemoryFeedback, PendingReview."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from memplex.models.misc import FieldValue


class FeedbackVerdict(Enum):
    CORRECT = "correct"
    WRONG = "wrong"
    ALTERNATIVE = "alternative"


def _to_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to naive UTC.

    Lite/SQLite feedback stores produce naive timestamps while the
    Postgres backend (TIMESTAMPTZ via asyncpg) yields tz-aware ones;
    mixing the two raises ``TypeError`` on comparison/sort.  Normalizing
    at the model boundary keeps every store backend comparable.
    """
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass
class MemoryFeedback:
    memory_id: str
    field_role: str
    value_index: int
    verdict: FeedbackVerdict
    reason: Optional[str] = None
    source: str = "user"
    timestamp: datetime = field(default_factory=datetime.now)
    owner: Optional[str] = None
    feedback_type: str = "field_value"
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    needs_review: bool = True
    needs_review_until: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None
    # Feedback has the same identity boundary as the memory it reviews.  The
    # fields remain optional for backwards-compatible loading of historic JSON
    # and SQLite rows; authenticated stores fill them before persistence.
    tenant_id: Optional[str] = None
    owner_subject_id: Optional[str] = None
    workspace_id: Optional[str] = None
    visibility: str = "workspace"
    provenance: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.timestamp = _to_naive(self.timestamp)
        self.needs_review_until = _to_naive(self.needs_review_until)
        self.resolved_at = _to_naive(self.resolved_at)
        if not isinstance(self.visibility, str) or not self.visibility.strip():
            raise ValueError("visibility must be a non-empty string")
        self.visibility = self.visibility.strip()
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping")
        # Do not retain a caller-owned mutable mapping after validation.
        self.provenance = dict(self.provenance)


@dataclass
class PendingReview:
    memory_id: str
    field_role: str
    conflicting_values: List[FieldValue] = field(default_factory=list)
    detected_at: Optional[datetime] = None
    source: str = ""
