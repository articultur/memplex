"""LLM enhancement manager: coordinates all LLM-augmented pipeline nodes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from memplex.models import EnhancedQuery

if TYPE_CHECKING:
    from memplex.config import LLMConfig
    from memplex.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


class LLMEnhancer:
    """Unified LLM enhancement manager.

    Orchestrates the LLM-augmented query pipeline nodes with per-feature
    configuration switches.  When a feature is disabled, a rule-based
    fallback is used instead.

    Parameters
    ----------
    llm_provider:
        An LLMProvider implementation to delegate calls to.
    config:
        LLMConfig controlling which enhancements are active.
    """

    def __init__(self, llm_provider: LLMProvider, config: LLMConfig) -> None:
        self.llm = llm_provider
        self.config = config

    # -- LLM Enhancement 2: Query Enhancement ---------------------------

    async def enhance_query(self, query: str) -> EnhancedQuery:
        """Use LLM to understand and expand a user query."""
        from memplex.llm.sanitizer import LLMPromptSanitizer

        if not self.config.query_enhancement:
            return EnhancedQuery(original=query, expanded=[query], intent="search")

        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction="Analyze the user query intent. Return intent type, "
            "expanded queries, and related concepts",
            user_input=query,
            output_schema={
                "intent": "search|understand|compare|relation",
                "expanded_queries": ["str"],
                "related_concepts": ["str"],
            },
            max_length=self.config.max_input_length,
        )
        result = await self.llm.complete_json(prompt)
        return EnhancedQuery(
            original=query,
            expanded=result.get("expanded_queries", [query]),
            intent=result.get("intent", "search"),
        )

    # -- LLM Enhancement 2.5: HyDE --------------------------------------

    async def enhance_query_hyde_text(self, query: str) -> str:
        """Generate a hypothetical answer text for HyDE embedding.

        On failure, returns the original query so the main
        pipeline is never blocked.
        """
        from memplex.llm.sanitizer import LLMPromptSanitizer

        if not self.config.query_enhancement:
            return query

        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction="Assume a memory entry fully answers the user query. "
            "Describe the core content of that memory in 2-3 sentences",
            user_input=query,
            output_schema={"hypothetical_memory": "str"},
            max_length=self.config.max_input_length,
        )
        try:
            result = await self.llm.complete_json(prompt)
            return result.get("hypothetical_memory", query)
        except Exception as exc:
            logger.debug("HyDE enhancement failed, returning original query: %s", exc)
            return query

    # -- LLM Enhancement 4: Observation compression ----------------------

    async def compress_observation(self, content: str, max_length: int = 500) -> str:
        """Compress a captured observation into a compact summary.

        Consumer: the agent-runtime capture path calls this before storing
        long tool output / conversation text, claude-mem style.

        With a real LLM provider, *content* is compressed to at most
        *max_length* characters, preserving the key facts (what was done,
        which files/decisions are involved, and the outcome).  Without an
        LLM (rule-based provider), when the feature is disabled, or when
        the LLM call fails, a rule-based head/tail truncation is used so
        the capture path is never blocked.
        """
        if len(content) <= max_length:
            return content

        from memplex.llm.providers.rule_based import RuleBasedProvider

        llm_available = (
            self.config.observation_compression and not isinstance(self.llm, RuleBasedProvider)
        )
        if llm_available:
            try:
                from memplex.llm.sanitizer import LLMPromptSanitizer

                prompt = LLMPromptSanitizer.build_structured_prompt(
                    instruction="Compress this captured observation into a compact summary "
                    f"of at most {max_length} characters. Preserve the key facts: what was "
                    "done, which files or decisions are involved, and the outcome",
                    user_input=content,
                    output_schema={"compressed": "str"},
                    max_length=self.config.max_input_length,
                )
                result = await self.llm.complete_json(prompt)
                compressed = result.get("compressed", "")
                if compressed:
                    return compressed[:max_length]
                logger.debug("Observation compression returned empty, using rule truncation")
            except Exception as exc:
                logger.debug(
                    "Observation compression failed, using rule-based truncation: %s", exc
                )

        return self._rule_truncate(content, max_length)

    # -- LLM Enhancement 5: Factual capture (retain-style) ----------------

    async def factualize(self, text: str, max_facts: int = 8) -> list[str]:
        """Extract self-contained, temporally-normalised facts from *text*.

        Hindsight-``retain()``-style capture: resolves pronouns/coreferences
        to explicit subjects, converts relative time expressions ("last
        week", "yesterday") into absolute dates against *reference_date*,
        and returns each fact as one standalone sentence. The prompt pins
        JSON output; malformed results fall back to an empty list rather
        than blocking the capture path.

        With a rule-based provider (no LLM configured) this returns ``[]``;
        callers keep their existing extraction as the source of truth.
        """
        from memplex.llm.providers.rule_based import RuleBasedProvider

        if isinstance(self.llm, RuleBasedProvider) or not text.strip():
            return []
        try:
            from memplex.llm.sanitizer import LLMPromptSanitizer

            prompt = LLMPromptSanitizer.build_structured_prompt(
                instruction=(
                    "Extract at most "
                    f"{max_facts} self-contained facts from the text. Rules: "
                    "(1) resolve every pronoun or coreference to the explicit "
                    "subject it refers to; (2) normalise relative time "
                    "expressions to absolute ISO dates using the reference "
                    "date; (3) each fact must be a single standalone sentence "
                    "understandable without any other context; (4) skip "
                    "opinions, filler, and questions."
                ),
                user_input=text,
                output_schema={"facts": ["str"]},
                max_length=self.config.max_input_length,
            )
            result = await self.llm.complete_json(prompt)
            facts = result.get("facts", [])
            if not isinstance(facts, list):
                return []
            cleaned = [str(f).strip() for f in facts if isinstance(f, str) and str(f).strip()]
            return cleaned[:max_facts]
        except Exception as exc:
            logger.debug("Factual capture failed, returning no facts: %s", exc)
            return []

    @staticmethod
    def _rule_truncate(content: str, max_length: int) -> str:
        """Rule-based fallback: keep head and tail halves with an omission marker."""
        marker = "\n...\n"
        if max_length <= len(marker):
            return content[:max_length]
        head = (max_length - len(marker)) // 2
        tail = max_length - len(marker) - head
        return content[:head] + marker + content[len(content) - tail :]
