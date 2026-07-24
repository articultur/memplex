"""Anthropic LLM provider implementation."""

import json
import logging

from memplex.models import IntentType

logger = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:
    raise ImportError(
        "The 'anthropic' package is required for AnthropicProvider. "
        "Install it with: pip install anthropic"
    )


class AnthropicProvider:
    """LLM provider backed by the Anthropic SDK (Claude).

    Parameters
    ----------
    api_key:
        Anthropic API key.
    model:
        Model identifier (default: claude-sonnet-4-6).
    max_tokens:
        Default maximum tokens for completions.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    # -- helpers --------------------------------------------------------

    async def _raw_complete(self, prompt: str, max_tokens: int | None = None) -> str:
        """Send a single-turn user message and return the assistant text."""
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens or self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    async def _raw_complete_json(self, prompt: str) -> dict:
        """Complete with JSON response expectation and parse the result."""
        text = await self._raw_complete(prompt)
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Best-effort JSON extraction from LLM output."""
        text = text.strip()
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try to extract JSON block from markdown code fence
        import re

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

    # -- LLMProvider interface ------------------------------------------

    async def classify_intent(self, query: str, context: dict | None = None) -> IntentType:
        """Classify user query intent using Claude."""
        result = await self.complete_json(
            f"Classify the intent of the following query. "
            f'Respond with a JSON object: {{"intent": "search|understand|compare|relation"}}\n\nQuery: {query}'
        )
        intent_str = result.get("intent", "search")
        mapping = {
            "search": IntentType.IMMEDIATE,
            "understand": IntentType.SYNTHESIS,
            "compare": IntentType.RELATION,
            "relation": IntentType.RELATION,
        }
        return mapping.get(intent_str, IntentType.IMMEDIATE)

    async def summarize(self, content: str, max_tokens: int = 256) -> str:
        """Summarize content using Claude."""
        prompt = f"Summarize the following content concisely in at most {max_tokens} tokens:\n\n{content}"
        return await self._raw_complete(prompt, max_tokens=max_tokens)

    async def extract_structured(self, prompt: str, schema: dict) -> dict:
        """Extract structured data according to a JSON schema."""
        full_prompt = (
            f"{prompt}\n\n"
            f"Respond with valid JSON matching this schema:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        return await self._raw_complete_json(full_prompt)

    async def generate_hypothetical(self, query: str) -> str:
        """Generate a hypothetical answer for HyDE."""
        prompt = (
            f"Given the query below, write a brief hypothetical answer (2-3 sentences) "
            f"as if a comprehensive knowledge base entry existed for it.\n\nQuery: {query}"
        )
        return await self._raw_complete(prompt, max_tokens=256)

    async def complete(self, prompt: str) -> str:
        """General-purpose text completion."""
        return await self._raw_complete(prompt)

    async def complete_json(self, prompt: str) -> dict:
        """Complete and parse response as JSON."""
        return await self._raw_complete_json(prompt)
