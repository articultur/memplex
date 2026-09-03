"""Function builder -- L1 Paragraphs -> L2 Functions.

Pure transform extracted from :class:`CoreEngine` so the paragraph-to-
Function conversion (with multi-value field classification and content-
hash IDs) is independently testable and reusable.

Usage::

    from memplex.processing.function_builder import build_functions_from_paragraphs

    functions = build_functions_from_paragraphs(paragraphs, source)
"""

from __future__ import annotations

import hashlib
import re

from memplex.models import FieldValue, Function, SourceDocument
from memplex.models.paragraph import ParagraphCollection

# ── Helpers ───────────────────────────────────────────────────────────


def normalize_name(name: str) -> str:
    """Generate name_normalized from a display name.

    Rules (per spec SS1.3):
    1. lowercase
    2. strip whitespace
    3. collapse consecutive whitespace to single space
    4. remove punctuation (keep letters, digits, CJK, spaces, underscores, hyphens)
    """
    normalized = name.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9一-鿿 _-]", "", normalized)
    return normalized


# ── Builder ───────────────────────────────────────────────────────────


def build_functions_from_paragraphs(
    paragraphs: ParagraphCollection,
    source: SourceDocument,
) -> list[Function]:
    """Convert L1 Paragraphs to L2 Functions with multi-value fields.

    For each paragraph:
    - generate a stable ``func_<sha256[:16]>`` id from the raw text
    - classify sentences into trigger/condition/action/benefit FieldValues
      by their role; unstructured text is sentence-split and routed to
      trigger (first) then action (rest)
    - stamp ``content_hash`` for downstream dedup
    """
    functions: list[Function] = []
    source_id = source.type

    for para in paragraphs.paragraphs:
        if not para.raw_text or not para.raw_text.strip():
            continue

        # Generate stable ID from content hash
        content_hash = hashlib.sha256(para.raw_text.encode()).hexdigest()[:16]
        func_id = f"func_{content_hash}"

        name = para.section if para.section else para.raw_text[:50]
        name_normalized = normalize_name(para.section if para.section else para.raw_text[:50])

        # Classify FieldValues from sentences
        triggers: list[FieldValue] = []
        conditions: list[FieldValue] = []
        actions: list[FieldValue] = []
        benefits: list[FieldValue] = []

        for sent in para.sentences:
            fv = FieldValue(
                desc=sent.text,
                sources=[f"{source_id}:{para.id}"],
                source_method="rule_based",
                weight=0.7,
            )
            if sent.role == "trigger":
                triggers.append(fv)
            elif sent.role == "condition":
                conditions.append(fv)
            elif sent.role in ("action",):
                actions.append(fv)
            elif sent.role == "result":
                benefits.append(fv)
            else:
                # "statement" (or any unknown role) -> put as action by default.
                # Previously this branch had an if/else whose two arms were
                # identical (always append to actions); collapsed to one call.
                actions.append(fv)

        # If no structured sentences, use raw text as trigger/action
        if not triggers and not actions and para.raw_text:
            sentences_text = [
                s.strip() for s in re.split(r"[。.!?！？]", para.raw_text) if s.strip()
            ]
            if sentences_text:
                triggers.append(
                    FieldValue(
                        desc=sentences_text[0],
                        sources=[f"{source_id}:{para.id}"],
                        source_method="rule_based",
                        weight=0.7,
                    )
                )
                for s in sentences_text[1:]:
                    actions.append(
                        FieldValue(
                            desc=s,
                            sources=[f"{source_id}:{para.id}"],
                            source_method="rule_based",
                            weight=0.7,
                        )
                    )
            else:
                triggers.append(
                    FieldValue(
                        desc=para.raw_text,
                        sources=[f"{source_id}:{para.id}"],
                        source_method="rule_based",
                        weight=0.7,
                    )
                )

        func = Function(
            id=func_id,
            name=name,
            name_normalized=name_normalized,
            trigger=triggers,
            condition=conditions,
            action=actions,
            benefit=benefits,
            source_paragraphs=[para.id],
            source_type=source.source_type,
            content_hash=hashlib.sha256(para.raw_text.encode()).hexdigest(),
        )
        functions.append(func)

    return functions
