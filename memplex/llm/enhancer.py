"""LLM enhancement manager: coordinates all LLM-augmented pipeline nodes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, List

from memplex.models import (
    EnhancedQuery,
    FieldValue,
    Function,
    Summary,
)

if TYPE_CHECKING:
    from memplex.config import LLMConfig
    from memplex.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


class LLMEnhancer:
    """Unified LLM enhancement manager.

    Orchestrates all LLM-augmented pipeline nodes with per-feature
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

    # -- LLM Enhancement 1: Semantic Extraction -------------------------

    async def semantic_extract_trigger(self, paragraph: str) -> List[FieldValue]:
        """Use LLM to semantically extract trigger conditions from a paragraph."""
        from memplex.llm.sanitizer import LLMPromptSanitizer

        if not self.config.semantic_extraction:
            return self._rule_based_extract(paragraph, "trigger")

        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction="Extract trigger conditions from the following paragraph, "
            "focusing on user intent rather than simple keyword matching",
            user_input=paragraph,
            output_schema={"triggers": [{"desc": "str", "confidence": "float(0-1)"}]},
        )
        result = await self.llm.complete_json(prompt)
        return [
            FieldValue(
                desc=r["desc"],
                sources=["llm_semantic"],
                source_method="llm_semantic",
                weight=r.get("weight", 0.8),
                observation=r.get("confidence", 1.0),
                created_at=datetime.utcnow(),
            )
            for r in result.get("triggers", [])
        ]

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

        On failure, silently returns the original query so the main
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
        )
        try:
            result = await self.llm.complete_json(prompt)
            return result.get("hypothetical_memory", query)
        except Exception:
            return query

    # -- LLM Enhancement 3: Conflict Resolution -------------------------

    async def resolve_conflict(self, func1: Function, func2: Function) -> dict:
        """Use LLM to analyze two conflicting Function versions and propose a merge."""
        from memplex.llm.sanitizer import LLMPromptSanitizer

        if not self.config.conflict_resolution:
            return self._authority_based_resolve(func1, func2)

        conflict_data = {
            "v1": {
                "trigger": [fv.desc for fv in func1.trigger],
                "condition": [fv.desc for fv in func1.condition],
            },
            "v2": {
                "trigger": [fv.desc for fv in func2.trigger],
                "condition": [fv.desc for fv in func2.condition],
            },
        }
        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction="Analyze two conflicting function versions and decide how to merge",
            user_input=__import__("json").dumps(conflict_data, ensure_ascii=False),
            output_schema={
                "decision": "keep_v1|keep_v2|merge",
                "reasoning": "str",
                "merged_function": {},
            },
        )
        result = await self.llm.complete_json(prompt)
        return self._parse_resolution(result)

    # -- LLM Enhancement 4: Summarization --------------------------------

    async def summarize(self, memories: list) -> Summary:
        """Generate a summary from a list of MemoryNode objects."""
        from memplex.llm.sanitizer import LLMPromptSanitizer

        if not self.config.summarization:
            return Summary(
                key_points=[m.name for m in memories],
                patterns=[],
                changes=[],
            )

        # Only send structured fields, not raw free text (reduces injection risk)
        summaries = [
            f"{m.name}: {', '.join(fv.desc for fv in getattr(m, 'action', []))}"
            for m in memories
        ]
        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction="Extract key information from the following memories "
            "and generate a concise summary",
            user_input="\n".join(summaries),
            output_schema={
                "key_points": ["str"],
                "patterns": ["str"],
                "changes": ["str"],
            },
        )
        result = await self.llm.complete_json(prompt)
        return Summary(
            key_points=result.get("key_points", []),
            patterns=result.get("patterns", []),
            changes=result.get("changes", []),
        )

    # -- Private helpers -------------------------------------------------

    @staticmethod
    def _rule_based_extract(paragraph: str, role: str) -> List[FieldValue]:
        """Trivial rule-based extraction when LLM is disabled."""
        sentences = [s.strip() for s in paragraph.split(".") if s.strip()]
        return [
            FieldValue(
                desc=s,
                sources=["rule_based"],
                source_method="rule_based",
                weight=0.5,
            )
            for s in sentences[:5]
        ]

    @staticmethod
    def _authority_based_resolve(func1: Function, func2: Function) -> dict:
        """Fallback conflict resolution based on source authority."""
        priority = {"requirement": 4, "meeting": 3, "code": 2, "wiki": 1}
        p1 = priority.get(func1.source_type.value if func1.source_type else "wiki", 1)
        p2 = priority.get(func2.source_type.value if func2.source_type else "wiki", 1)
        if p1 >= p2:
            return {"decision": "keep_v1", "reasoning": "higher source authority"}
        return {"decision": "keep_v2", "reasoning": "higher source authority"}

    @staticmethod
    def _parse_resolution(result: dict) -> dict:
        """Normalize the LLM conflict resolution response."""
        return {
            "decision": result.get("decision", "keep_v1"),
            "reasoning": result.get("reasoning", ""),
            "merged_function": result.get("merged_function", {}),
        }
