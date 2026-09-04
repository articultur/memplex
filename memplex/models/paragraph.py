"""L1: Paragraph and Sentence models."""

from dataclasses import dataclass, field


@dataclass
class Sentence:
    id: str
    text: str
    role: str  # trigger, condition, action, result


@dataclass
class SentenceRelation:
    from_id: str
    to_id: str
    type: str  # if_then, cause_effect, etc.


@dataclass
class Paragraph:
    id: str
    source: str  # "filename.md#3.2.1"
    section: str
    raw_text: str
    semantic_unit: bool = True
    sentences: list[Sentence] = field(default_factory=list)
    sentence_relations: list[SentenceRelation] = field(default_factory=list)
    confidence: float = 1.0
    needs_review: bool = False


@dataclass
class ParagraphCollection:
    paragraphs: list[Paragraph] = field(default_factory=list)

    def add(self, paragraph: Paragraph) -> None:
        self.paragraphs.append(paragraph)

    def get_by_id(self, para_id: str) -> Paragraph | None:
        for p in self.paragraphs:
            if p.id in (f"para_{para_id}", para_id):
                return p
        return None
