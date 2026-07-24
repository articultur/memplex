"""LLM Provider protocol definition."""

from typing import Protocol, runtime_checkable

from memplex.models import IntentType


@runtime_checkable
class LLMProvider(Protocol):
    """LLM Provider standard protocol.

    All LLM provider implementations must satisfy this protocol.
    Used for intent classification, summarization, structured extraction,
    HyDE generation, and general-purpose completion.
    """

    async def classify_intent(self, query: str, context: dict | None = None) -> IntentType:
        """Classify user query intent.

        Returns one of: IMMEDIATE, SYNTHESIS, RELATION, ALL.
        """
        ...

    async def summarize(self, content: str, max_tokens: int = 256) -> str:
        """Summarize content into a concise representation."""
        ...

    async def extract_structured(self, prompt: str, schema: dict) -> dict:
        """Extract structured data according to the provided JSON schema."""
        ...

    async def generate_hypothetical(self, query: str) -> str:
        """Generate a hypothetical answer for HyDE (Hypothetical Document Embeddings)."""
        ...

    async def complete(self, prompt: str) -> str:
        """General-purpose text completion."""
        ...

    async def complete_json(self, prompt: str) -> dict:
        """Complete and parse response as JSON."""
        ...


def create_provider(
    provider: str = "auto",
    *,
    anthropic_api_key: str | None = None,
    anthropic_model: str = "claude-sonnet-4-6",
    local_endpoint: str = "http://localhost:11434/v1",
    local_model: str = "qwen2.5",
    fallback_chain: list[str] | None = None,
) -> LLMProvider:
    """Factory: create an LLM provider instance by name.

    Parameters
    ----------
    provider:
        One of "auto", "anthropic", "local", "rule-based".
    anthropic_api_key:
        Anthropic API key (required for anthropic provider).
    anthropic_model:
        Model name for Anthropic.
    local_endpoint:
        OpenAI-compatible endpoint URL (e.g., Ollama / LM Studio).
    local_model:
        Model name for the local provider.
    fallback_chain:
        Ordered list of provider names for FallbackChain.
        Defaults to ["anthropic", "local", "rule-based"] when *provider*
        is "auto".

    Returns
    -------
    An object satisfying the LLMProvider protocol.
    """
    if provider == "auto":
        chain = fallback_chain or ["anthropic", "local", "rule-based"]
        return create_provider(
            provider=None,
            anthropic_api_key=anthropic_api_key,
            anthropic_model=anthropic_model,
            local_endpoint=local_endpoint,
            local_model=local_model,
            fallback_chain=chain,
        )

    # If provider is None, build a FallbackChain from fallback_chain list.
    if provider is None:
        from memplex.llm.fallback_chain import FallbackChain

        chain_names = fallback_chain or ["anthropic", "local", "rule-based"]
        providers = []
        for name in chain_names:
            try:
                p = create_provider(
                    provider=name,
                    anthropic_api_key=anthropic_api_key,
                    anthropic_model=anthropic_model,
                    local_endpoint=local_endpoint,
                    local_model=local_model,
                )
                providers.append(p)
            except Exception:
                continue
        return FallbackChain(providers)

    if provider == "anthropic":
        from memplex.llm.providers.anthropic import AnthropicProvider

        if not anthropic_api_key:
            raise ValueError("anthropic_api_key is required for the Anthropic provider")
        return AnthropicProvider(api_key=anthropic_api_key, model=anthropic_model)

    if provider == "local":
        from memplex.llm.providers.local import LocalProvider

        return LocalProvider(endpoint=local_endpoint, model=local_model)

    if provider == "rule-based":
        from memplex.llm.providers.rule_based import RuleBasedProvider

        return RuleBasedProvider()

    raise ValueError(
        f"Unknown provider: {provider!r}. Choose from: auto, anthropic, local, rule-based"
    )
