"""Privacy helpers shared across write paths.

The ``<private>...</private>`` tag lets operators mark content that must
never be stored as memory. The hook runner has always stripped these tags
before capture; the service write path now does the same so the redaction
applies to every adapter (CLI write, HTTP /memories, MCP memory_add,
corpus indexer), not only the Claude Code hook runner.

Usage::

    from memplex.privacy import strip_private_tags
    cleaned = strip_private_tags(raw_text)
"""

from __future__ import annotations

import re

# Non-greedy so two separate <private> blocks are stripped independently.
# DOTALL so a block may span newlines.
_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def strip_private_tags(text: str) -> str:
    """Remove every ``<private>...</private>`` block from *text*.

    Returns the text unchanged when no tags are present. The closing tag
    is required; an unclosed ``<private>`` is left intact (treated as
    literal text) so a malformed tag never silently drops trailing
    content.
    """
    if not text or "<private>" not in text.lower():
        return text
    return _PRIVATE_TAG_RE.sub("", text)
