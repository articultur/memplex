"""Feedback types: FeedbackVerdict, MemoryFeedback, PendingReview."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

from memplex.models.misc import FieldValue


class FeedbackVerdict(Enum):
    CORRECT = "correct"
    WRONG = "wrong"
    ALTERNATIVE = "alternative"


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


@dataclass
class PendingReview:
    memory_id: str
    field_role: str
    conflicting_values: List[FieldValue] = field(default_factory=list)
    detected_at: Optional[datetime] = None
    source: str = ""
