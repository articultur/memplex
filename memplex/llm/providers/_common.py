"""Shared helpers for LLM provider implementations.

The Anthropic and local (OpenAI-compatible) providers differ only in
their SDK transport; response parsing and intent mapping are identical
and live here to avoid drift between the two copies.
"""

import json
import logging
import re

from memplex.models import IntentType

logger = logging.getLogger(__name__)

_INTENT_MAPPING = {
    "search": IntentType.IMMEDIATE,
    "understand": IntentType.SYNTHESIS,
    "compare": IntentType.RELATION,
    "relation": IntentType.RELATION,
}


def parse_json_response(text: str) -> dict:
    """Best-effort JSON extraction from LLM output."""
    text = text.strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to extract JSON block from markdown code fence
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Try to find first { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    logger.warning("Failed to parse JSON from LLM response, returning empty dict")
    return {}


def classify_intent_prompt(query: str) -> str:
    """Build the shared intent-classification prompt."""
    return (
        "Classify the intent of the following query. "
        'Respond with a JSON object: {"intent": "search|understand|compare|relation"}'
        f"\n\nQuery: {query}"
    )


def intent_from_result(result: dict) -> IntentType:
    """Map a parsed classification response to an IntentType."""
    return _INTENT_MAPPING.get(result.get("intent", "search"), IntentType.IMMEDIATE)
