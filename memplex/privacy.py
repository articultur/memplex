"""Privacy helpers shared across write paths.

The ``<private>...</private>`` tag lets operators mark content that must
never be stored as memory. The hook runner has always stripped these tags
before capture; the service write path now does the same so the redaction
applies to every adapter (CLI write, HTTP /memories, MCP memory_add,
corpus indexer), not only the Claude Code hook runner.

Unclosed-tag semantics: redaction is fail-open. An opening ``<private>``
without a matching ``</private>`` is treated as literal text and kept, so
a malformed tag never silently drops trailing content. Because that means
the trailing content is stored *unredacted*, the function logs a warning
(``privacy_unclosed_private_tag``) whenever it keeps an unclosed tag.

Usage::

    from memplex.privacy import strip_private_tags
    cleaned = strip_private_tags(raw_text)
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Non-greedy so two separate <private> blocks are stripped independently.
# DOTALL so a block may span newlines.
_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def strip_private_tags(text: str) -> str:
    """Remove every ``<private>...</private>`` block from *text*.

    Returns the text unchanged when no tags are present. The closing tag
    is required; an unclosed ``<private>`` is left intact (treated as
    literal text) so a malformed tag never silently drops trailing
    content. That fail-open keep may leave sensitive text unredacted, so
    it is reported via a ``privacy_unclosed_private_tag`` warning.
    """
    if not text or "<private>" not in text.lower():
        return text
    cleaned = _PRIVATE_TAG_RE.sub("", text)
    if "<private>" in cleaned.lower():
        logger.warning(
            "privacy_unclosed_private_tag: unclosed <private> tag kept as "
            "literal text; trailing content is NOT redacted"
        )
    return cleaned
