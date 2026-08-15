"""LLM provider implementations."""

from memplex.llm.providers.rule_based import RuleBasedProvider

# Anthropic and Local providers are optional -- they require external
# packages that may not be installed.  Import them lazily or guard with
# try/except at the call site.

__all__ = [
    "RuleBasedProvider",
]


def __getattr__(name: str):
    """Lazy-load optional providers that depend on external packages."""
    if name == "AnthropicProvider":
        from memplex.llm.providers.anthropic import AnthropicProvider

        return AnthropicProvider
    if name == "LocalProvider":
        from memplex.llm.providers.local import LocalProvider

        return LocalProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
