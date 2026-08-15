"""Source types: SourceType, SourceDocument."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SourceType(Enum):
    REQUIREMENT = "requirement"
    MEETING = "meeting"
    CODE = "code"
    WIKI = "wiki"


@dataclass
class SourceDocument:
    type: str  # text | file | url | clipboard
    content: Optional[str] = None
    source_path: Optional[str] = None
    content_hash: Optional[str] = None
    url: Optional[str] = None
    vision: Optional[dict] = None
    source_type: SourceType = SourceType.WIKI
