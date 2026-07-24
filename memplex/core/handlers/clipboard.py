"""Handle text paste input."""

from typing import List, Tuple


class ClipboardHandler:
    """Handles pasted text content."""

    def parse(self, content: str) -> List[Tuple[str, str]]:
        """
        Parse pasted content.

        Returns:
            List of (content_type, content) tuples.
            content_type: "markdown", "text"
        """
        if not content or not content.strip():
            return []

        content = content.strip()

        if self._is_markdown(content):
            return [("markdown", content)]
        else:
            return [("text", content)]

    def _is_markdown(self, content: str) -> bool:
        """Simple markdown detection."""
        markdown_indicators = ["#", "```", "- ", "* ", "[ ]", "**", "__", "```"]
        return any(content.startswith(ind) or f"\n{ind}" in content for ind in markdown_indicators)
