"""Chain-of-responsibility fallback for LLM providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from memplex.models import IntentType

if TYPE_CHECKING:
    from memplex.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


class FallbackChain:
    """Try providers in order; first success wins, final fallback to RuleBasedProvider.

    Parameters
    ----------
    providers:
        Ordered list of LLMProvider implementations.  They are tried
        sequentially; the first one that returns without raising wins.
    """

    def __init__(self, providers: list[LLMProvider] | None = None) -> None:
        self._providers: list[LLMProvider] = providers or []

    def _fallback(self) -> LLMProvider:
        """Lazily create a RuleBasedProvider as the ultimate fallback."""
        from memplex.llm.providers.rule_based import RuleBasedProvider

        return RuleBasedProvider()

    # -- LLMProvider interface ------------------------------------------

    async def classify_intent(self, query: str, context: dict | None = None) -> IntentType:
        errors: list[str] = []
        for p in self._providers:
            try:
                return await p.classify_intent(query, context)
            except Exception as exc:
                errors.append(f"{p.__class__.__name__}: {exc}")
                logger.debug("classify_intent fallback: %s", errors[-1])
        return await self._fallback().classify_intent(query, context)

    async def summarize(self, content: str, max_tokens: int = 256) -> str:
        for p in self._providers:
            try:
                return await p.summarize(content, max_tokens)
            except Exception as exc:
                logger.debug("summarize fallback: %s: %s", p.__class__.__name__, exc)
        return content[:max_tokens]

    async def extract_structured(self, prompt: str, schema: dict) -> dict:
        for p in self._providers:
            try:
                return await p.extract_structured(prompt, schema)
            except Exception as exc:
                logger.debug("extract_structured fallback: %s: %s", p.__class__.__name__, exc)
        return {}

    async def generate_hypothetical(self, query: str) -> str:
        for p in self._providers:
            try:
                return await p.generate_hypothetical(query)
            except Exception as exc:
                logger.debug("generate_hypothetical fallback: %s: %s", p.__class__.__name__, exc)
        return query

    async def complete(self, prompt: str) -> str:
        for p in self._providers:
            try:
                return await p.complete(prompt)
            except Exception as exc:
                logger.debug("complete fallback: %s: %s", p.__class__.__name__, exc)
        return ""

    async def complete_json(self, prompt: str) -> dict:
        for p in self._providers:
            try:
                return await p.complete_json(prompt)
            except Exception as exc:
                logger.debug("complete_json fallback: %s: %s", p.__class__.__name__, exc)
        return {}
