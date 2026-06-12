"""Confidence calculation based on extraction quality signals."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memplex.models.paragraph import Paragraph


class ConfidenceCalculator:
    """Calculates confidence scores based on extraction quality signals."""

    SOURCE_BASE = {
        "text": 0.95,
        "markdown": 0.95,
        "pdf": 0.90,
        "docx": 0.90,
        "image": 0.85,
        "vision": 0.80,
        "url": 0.90,
    }

    SOURCE_ALIASES = {
        "clipboard": "text",
        "file:": "file",
    }

    def calculate_paragraph_confidence(
        self, para: "Paragraph", source_hint: str = "text"
    ) -> float:
        """
        Calculate confidence for a paragraph -> Function conversion.

        Args:
            para: The paragraph to evaluate
            source_hint: Hint about source type

        Returns:
            Confidence score between 0.0 and 1.0
        """
        base = self._get_base_confidence(source_hint)

        adjustments = []

        if para.sentences:
            sent_count = len(para.sentences)
            if 2 <= sent_count <= 10:
                adjustments.append(0.02)
            elif sent_count > 10:
                adjustments.append(0.01)
        else:
            adjustments.append(-0.05)

        if para.section:
            adjustments.append(0.03)

        text_len = len(para.raw_text) if para.raw_text else 0
        if text_len < 10:
            adjustments.append(-0.05)
        elif text_len >= 50:
            adjustments.append(0.02)

        roles = [s.role for s in para.sentences] if para.sentences else []
        field_count = sum(
            1 for r in roles if r in ("trigger", "condition", "action", "result")
        )
        if field_count >= 3:
            adjustments.append(0.05)
        elif field_count == 1:
            adjustments.append(-0.02)

        unique_roles = set(roles)
        if "trigger" in unique_roles and "action" in unique_roles:
            adjustments.append(0.03)
        if "condition" in unique_roles and "action" in unique_roles:
            adjustments.append(0.02)

        confidence = base + sum(adjustments)
        return max(0.5, min(0.99, confidence))

    def calculate_vision_confidence(
        self, page_type: str, component_count: int
    ) -> float:
        """
        Calculate confidence for Vision-derived functions.

        Args:
            page_type: Type of page
            component_count: Number of UI components detected

        Returns:
            Confidence score between 0.0 and 1.0
        """
        base = self.SOURCE_BASE["vision"]

        adjustments = []

        if page_type and page_type not in ("Unknown", "Other"):
            adjustments.append(0.05)
        else:
            adjustments.append(-0.05)

        if component_count == 0:
            adjustments.append(-0.10)
        elif component_count <= 10:
            adjustments.append(0.03)
        elif component_count > 20:
            adjustments.append(-0.02)

        confidence = base + sum(adjustments)
        return max(0.5, min(0.95, confidence))

    def _get_base_confidence(self, source_hint: str) -> float:
        """Get base confidence for a source hint."""
        hint_lower = source_hint.lower()

        for key, val in self.SOURCE_BASE.items():
            if key in hint_lower:
                return val

        for alias, canonical in self.SOURCE_ALIASES.items():
            if alias in hint_lower:
                return self.SOURCE_BASE.get(canonical, 0.9)

        return 0.9
