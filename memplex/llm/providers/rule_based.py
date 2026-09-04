"""Rule-based LLM provider: zero-dependency fallback implementation."""

from typing import ClassVar

from memplex.models import IntentType


class RuleBasedProvider:
    """Pure keyword-rule based LLM provider.

    Zero external dependencies. Used as the final fallback when no real
    LLM provider is available.
    """

    # Intent classification keyword mapping
    _INTENT_KEYWORDS: ClassVar[dict[str, list[str]]] = {        "understand": [
            "what is",
            "what are",
            "explain",
            "how does",
            "how do",
            "describe",
            "define",
            "tell me about",
            "什么是",
            "解释",
            "描述",
            "如何",
            "怎么",
        ],
        "compare": [
            "compare",
            "difference",
            "versus",
            "vs",
            "contrast",
            "比较",
            "对比",
            "区别",
            "不同",
        ],
        "relation": [
            "related",
            "connection",
            "linked",
            "between",
            "关联",
            "关系",
            "联系",
            "连接",
        ],
    }

    async def classify_intent(self, query: str, context: dict | None = None) -> IntentType:
        """Classify intent using keyword matching."""
        q = query.lower()
        for intent_name, keywords in self._INTENT_KEYWORDS.items():
            for kw in keywords:
                if kw in q:
                    mapping = {
                        "understand": IntentType.SYNTHESIS,
                        "compare": IntentType.RELATION,
                        "relation": IntentType.RELATION,
                    }
                    return mapping.get(intent_name, IntentType.IMMEDIATE)
        return IntentType.IMMEDIATE

    async def summarize(self, content: str, max_tokens: int = 256) -> str:
        """Truncate content as a trivial summary."""
        if len(content) <= max_tokens:
            return content
        return content[:max_tokens]

    async def extract_structured(self, prompt: str, schema: dict) -> dict:
        """Return empty dict -- no structured extraction without an LLM."""
        return {}

    async def generate_hypothetical(self, query: str) -> str:
        """Return query unchanged -- no HyDE without an LLM."""
        return query

    async def complete(self, prompt: str) -> str:
        """Return empty string -- no completion without an LLM."""
        return ""

    async def complete_json(self, prompt: str) -> dict:
        """Return empty dict -- no JSON completion without an LLM."""
        return {}
