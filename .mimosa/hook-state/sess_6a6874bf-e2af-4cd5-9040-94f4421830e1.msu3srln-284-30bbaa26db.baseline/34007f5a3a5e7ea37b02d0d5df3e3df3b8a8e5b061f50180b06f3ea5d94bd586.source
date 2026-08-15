"""Local LLM provider using OpenAI-compatible API (Ollama / LM Studio)."""

import json
import logging

from memplex.llm.providers._common import (
    classify_intent_prompt,
    intent_from_result,
    parse_json_response,
)
from memplex.models import IntentType

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - exercised through constructor contract
    AsyncOpenAI = None


class LocalProvider:
    """LLM provider backed by an OpenAI-compatible local API.

    Works with Ollama, LM Studio, vLLM, and any server that exposes
    the ``/v1/chat/completions`` endpoint.

    Parameters
    ----------
    endpoint:
        Base URL of the OpenAI-compatible API.
    model:
        Model identifier served by the local endpoint.
    max_tokens:
        Default maximum tokens for completions.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:11434/v1",
        model: str = "qwen2.5",
        max_tokens: int = 1024,
        *,
        client=None,
    ) -> None:
        if client is None:
            if AsyncOpenAI is None:
                raise ImportError(
                    "The 'openai' package is required for LocalProvider. "
                    "Install it with: pip install openai"
                )
            client = AsyncOpenAI(base_url=endpoint, api_key="not-needed")
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    # -- helpers --------------------------------------------------------

    async def _raw_complete(self, prompt: str, max_tokens: int | None = None) -> str:
        """Send a single-turn chat completion request."""
        resp = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens or self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""

    async def _raw_complete_json(self, prompt: str) -> dict:
        """Complete with JSON response expectation and parse the result."""
        text = await self._raw_complete(prompt)
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Best-effort JSON extraction from LLM output."""
        return parse_json_response(text)

    # -- LLMProvider interface ------------------------------------------

    async def classify_intent(self, query: str, context: dict | None = None) -> IntentType:
        """Classify user query intent using local LLM."""
        result = await self.complete_json(classify_intent_prompt(query))
        return intent_from_result(result)

    async def summarize(self, content: str, max_tokens: int = 256) -> str:
        """Summarize content using local LLM."""
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
