"""L1: Paragraph and Sentence models."""

from dataclasses import dataclass, field
from hashlib import sha256


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


def persisted_paragraph_id(source_hint: str, para_id: str, raw_text: str) -> str:
    """Deterministic storage id for a raw paragraph (ADR-013 Stage 2).

    Extractor paragraph ids repeat across writes (``para_001`` in every
    document), so the durable id namespaces by source hint and pins the
    verbatim text with a short content digest (collision-safe naming,
    not a cryptographic guarantee). Identical text written twice maps
    to one row, and ``source_paragraphs`` references stay resolvable
    because the node builders reference this same id.
    """
    digest = sha256(raw_text.encode("utf-8")).hexdigest()[:8]
    return f"{source_hint}:{para_id}:{digest}"


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
