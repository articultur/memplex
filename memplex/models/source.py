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
    content: str | None = None
    source_path: str | None = None
    content_hash: str | None = None
    url: str | None = None
    vision: dict | None = None
    source_type: SourceType = SourceType.WIKI
