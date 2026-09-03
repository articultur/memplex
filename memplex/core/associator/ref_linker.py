"""Cross-document reference extraction and linking."""

import re
from typing import ClassVar


class RefLinker:
    """Extracts and resolves cross-document references."""

    # Reference patterns
    CROSS_DOC_PATTERNS: ClassVar[list[str]] = [
        r"详见[《\"]?(.+?)[》文档手册]",
        r"参见[《\"]?(.+?)[》\]]",
        r"[《\"]?(.+?)[》\]]\s*[第见]?\s*([0-9.]+)[章]?",
        r"如上所述",
        r"如前所述",
        r"前述",
        r"同上述([A-Za-z0-9_一-龥]+)",
        r"同下述([A-Za-z0-9_一-龥]+)",
        r"同前述([A-Za-z0-9_一-龥]+)",
        r"参见([A-Za-z0-9_一-龥-]+)",
        r"依据([A-Za-z0-9_一-龥-]+)",
        r"按照([A-Za-z0-9_一-龥-]+)",
        r"符合([A-Za-z0-9_一-龥-]+)",
        r"满足([A-Za-z0-9_一-龥-]+)",
        r"参照([A-Za-z0-9_一-龥-]+)",
        r"根据([A-Za-z0-9_一-龥-]+)",
        r"RFC-?(\d+)",
    ]

    SECTION_PATTERNS: ClassVar[list[str]] = [
        r"见第?([0-9.]+)节?",
        r"如图?([0-9]+(?:\.[0-9]+)?)",
        r"参考第?([0-9.]+)节",
        r"第([一二三四五六七八九十零]+)章",
    ]

    CN_DIGIT_MAP: ClassVar[dict[str, int]] = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "零": 0,
    }

    URL_PATTERN = r"https?://[^\s<>\"]+"

    SEQUENTIAL_PATTERNS: ClassVar[list[str]] = [
        r"之后",
        r"随后",
        r"接下来",
    ]

    BACK_REFERENCE_PATTERNS: ClassVar[list[str]] = [
        "如上所述",
        "如前所述",
        "前述",
    ]

    def extract_references(self, text: str) -> list[dict]:
        """
        Extract all types of references from text.

        Returns:
            List of reference dicts with type, target, and confidence
        """
        references = []

        for pattern in self.CROSS_DOC_PATTERNS:
            for match in re.finditer(pattern, text):
                target = (
                    match.group(1).strip()
                    if match.lastindex and match.group(1)
                    else match.group(0).strip()
                )
                ref_type = "implicit" if target in self.BACK_REFERENCE_PATTERNS else "cross_doc"
                references.append(
                    {
                        "type": ref_type,
                        "target": target,
                        "confidence": 0.95 if ref_type == "cross_doc" else 0.7,
                        "match": match.group(0),
                    }
                )

        for pattern in self.SEQUENTIAL_PATTERNS:
            for match in re.finditer(pattern, text):
                references.append(
                    {
                        "type": "sequential",
                        "target": "implicit_next",
                        "confidence": 0.6,
                        "match": match.group(0),
                    }
                )

        for pattern in self.SECTION_PATTERNS:
            for match in re.finditer(pattern, text):
                section = match.group(1)
                if re.match(r"^[一-鿿]+$", section):
                    section_num = 0
                    if "十" in section:
                        parts = section.split("十")
                        if parts[0] == "":
                            section_num = 10
                        else:
                            section_num = self.CN_DIGIT_MAP.get(parts[0], 0) * 10
                        if len(parts) > 1 and parts[1]:
                            section_num += self.CN_DIGIT_MAP.get(parts[1], 0)
                    else:
                        section_num = self.CN_DIGIT_MAP.get(section, 0)
                    target = f"section_{section_num}"
                else:
                    target = f"section_{section}"
                references.append(
                    {
                        "type": "section",
                        "target": target,
                        "confidence": 0.9,
                        "match": match.group(0),
                    }
                )

        for match in re.finditer(self.URL_PATTERN, text):
            references.append(
                {
                    "type": "url",
                    "target": match.group(0),
                    "confidence": 0.85,
                    "match": match.group(0),
                }
            )

        return references

    def resolve_reference(self, ref: dict, known_entities: dict[str, list[str]]) -> str | None:
        """Resolve reference to entity ID."""
        target = ref["target"]

        if target in known_entities:
            return known_entities[target][0]

        target_lower = target.lower()
        for name, ids in known_entities.items():
            if target_lower in name.lower() or name.lower() in target_lower:
                return ids[0]

        return None

    def resolve_implicit_reference(
        self,
        ref: dict,
        known_entities: dict[str, list[str]],
        context: dict | None = None,
    ) -> tuple[str | None, float]:
        """Resolve implicit reference to entity ID with confidence score."""
        target = ref.get("target", "")
        ref_type = ref.get("type", "")
        confidence = ref.get("confidence", 0.5)

        if target in self.BACK_REFERENCE_PATTERNS:
            if context and "previous_entity" in context:
                prev = context["previous_entity"]
                if prev in known_entities:
                    return known_entities[prev][0], 0.85
            if known_entities:
                first_key = next(iter(known_entities))
                return known_entities[first_key][0], 0.6
            return None, 0.0

        rfc_match = re.match(r"RFC-?(\d+)", target, re.IGNORECASE)
        if rfc_match:
            rfc_num = rfc_match.group(1)
            for name, ids in known_entities.items():
                name_lower = name.lower()
                if (
                    f"rfc_{rfc_num}" in name_lower
                    or f"rfc {rfc_num}" in name_lower
                    or f"rfc-{rfc_num}" in name_lower
                ):
                    return ids[0], 0.9
            return None, 0.0

        if target.startswith("同") and len(target) > 1:
            suffix = target[1:]
            best_match = None
            best_score = 0.0
            for name, ids in known_entities.items():
                if suffix in name:
                    score = len(suffix) / max(len(name), 1)
                    if score > best_score:
                        best_score = score
                        best_match = ids[0]
            if best_match:
                return best_match, min(0.5 + best_score * 0.4, 0.85)
            return None, 0.0

        if ref_type == "sequential" and target == "implicit_next":
            if context and "next_entity" in context:
                next_ent = context["next_entity"]
                if next_ent in known_entities:
                    return known_entities[next_ent][0], 0.8
            if len(known_entities) > 1:
                keys = list(known_entities.keys())
                return known_entities[keys[1]][0], 0.5
            return None, 0.0

        if ref_type == "implicit":
            target_lower = target.lower()
            for name, ids in known_entities.items():
                if target_lower in name.lower() or name.lower() in target_lower:
                    return ids[0], 0.7

        return None, confidence
