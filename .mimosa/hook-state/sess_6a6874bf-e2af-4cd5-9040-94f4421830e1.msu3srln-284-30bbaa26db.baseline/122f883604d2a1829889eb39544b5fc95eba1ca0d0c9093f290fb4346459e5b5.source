"""LLMWikiGenerator -- LLM-augmented wiki page generation.

Uses the LLM to produce enriched Entity Pages, summaries, concept pages,
and cross-reference suggestions.  All LLM calls go through
``LLMPromptSanitizer.build_structured_prompt`` for safe input handling.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, List

from memplex.llm.sanitizer import LLMPromptSanitizer
from memplex.models import (
    Function,
    WikiPage,
)

if TYPE_CHECKING:
    from memplex.llm.enhancer import LLMEnhancer

logger = logging.getLogger(__name__)

# Default maximum input length for prompt sanitization
DEFAULT_MAX_INPUT_LENGTH = 10000


class LLMWikiGenerator:
    """LLM-enhanced wiki page generator.

    Uses LLM understanding to produce higher-quality Wiki content than
    rule-based compilation alone.

    Parameters
    ----------
    llm_enhancer:
        The :class:`LLMEnhancer` instance used for LLM calls.
    sep:
        Separator string appended after sections injected into generated
        pages (e.g. the LLM cross-reference block) to delimit them.
    max_input_length:
        Maximum character length for sanitized inputs sent to the LLM.
    """

    def __init__(
        self,
        llm_enhancer: LLMEnhancer,
        sep: str = "---",
        max_input_length: int = DEFAULT_MAX_INPUT_LENGTH,
    ) -> None:
        self._llm = llm_enhancer
        self.sep = sep
        self.max_input_length = max_input_length

    # ── Public API ────────────────────────────────────────────────────

    async def generate_entity_page(self, func: Function) -> str:
        """Use LLM to generate an enhanced Entity Page for a Function.

        The LLM receives structured Function data and returns a natural
        language wiki page covering: one-sentence summary, trigger
        conditions, execution flow, and related functions.
        """
        func_data = {
            "name": func.name,
            "domain": func.domain or "uncategorized",
            "trigger": [fv.desc for fv in func.trigger],
            "condition": [fv.desc for fv in func.condition],
            "action": [fv.desc for fv in func.action],
            "benefit": [fv.desc for fv in func.benefit],
        }

        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction=(
                "为以下函数生成一份清晰的 Wiki 页面，包含："
                "1.一句话概括 2.触发条件与前置条件 "
                "3.执行流程自然语言描述 4.关联函数（如有）"
            ),
            user_input=json.dumps(func_data, ensure_ascii=False),
            max_length=self.max_input_length,
        )
        return await self._llm.llm.complete(prompt)

    async def generate_summary(self, functions: List[Function]) -> str:
        """Use LLM to generate a domain summary from multiple Functions.

        Produces a concept-level overview including core responsibilities,
        functional components, collaboration patterns, and key workflows.
        """
        funcs_data = [{"name": f.name, "action": [fv.desc for fv in f.action]} for f in functions]

        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction=(
                "分析以下函数列表，生成简洁领域摘要，包含："
                "1.核心职责 2.主要功能组件 3.协作关系 4.关键业务流程"
            ),
            user_input=json.dumps(funcs_data, ensure_ascii=False),
            max_length=self.max_input_length,
        )
        return await self._llm.llm.complete(prompt)

    async def generate_concept_page(
        self,
        domain: str,
        functions: List[Function],
    ) -> str:
        """Generate a concept page that aggregates Functions by domain.

        Parameters
        ----------
        domain:
            The domain / topic label for this concept page.
        functions:
            Functions belonging to this domain.
        """
        funcs_summary = [
            {
                "name": f.name,
                "trigger": [fv.desc for fv in f.trigger[:2]],
                "action": [fv.desc for fv in f.action[:2]],
            }
            for f in functions[:20]
        ]

        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction=(
                f'为领域 "{domain}" 生成一份概念聚合页面，包含：'
                "1.领域概述 2.核心功能列表 3.功能间协作关系 4.业务流程图描述"
            ),
            user_input=json.dumps(funcs_summary, ensure_ascii=False),
            max_length=self.max_input_length,
        )
        return await self._llm.llm.complete(prompt)

    async def update_cross_references(
        self,
        pages: List[WikiPage],
    ) -> List[WikiPage]:
        """Use LLM to discover and update cross-references across pages.

        For each page, the LLM analyses the content and suggests relevant
        ``[[wikilinks]]`` to other pages in the corpus.

        Parameters
        ----------
        pages:
            All wiki pages to analyse for cross-references.

        Returns
        -------
        Updated list of WikiPage objects with enriched cross-references.
        """
        if not pages:
            return pages

        # Build a lightweight summary index for the LLM
        page_summaries: list[dict] = []
        for p in pages:
            # Extract first non-heading, non-blank line as summary
            summary = ""
            for line in p.content.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
                    summary = stripped[:200]
                    break
            page_summaries.append({"id": p.page_id, "summary": summary})

        updated: List[WikiPage] = []
        for page in pages:
            try:
                candidates = [s for s in page_summaries if s["id"] != page.page_id][:20]

                prompt = LLMPromptSanitizer.build_structured_prompt(
                    instruction=(
                        "分析当前 Wiki 页面，从候选页面中选择相关页面，"
                        "返回相关页面的 ID 列表和关联理由。"
                        '格式：{"related": [{"id": "str", "reason": "str"}]}'
                    ),
                    user_input=json.dumps(
                        {
                            "current_page": {
                                "id": page.page_id,
                                "summary": page.content[:500],
                            },
                            "candidates": candidates,
                        },
                        ensure_ascii=False,
                    ),
                    output_schema={
                        "related": [{"id": "str", "reason": "str"}],
                    },
                    max_length=self.max_input_length,
                )
                result = await self._llm.llm.complete_json(prompt)

                # Inject cross-references into page content
                related = result.get("related", [])
                if related:
                    link_lines = [
                        f"- [[{r.get('id', '')}]] -- {r.get('reason', '')}"
                        for r in related
                        if r.get("id")
                    ]
                    if link_lines:
                        cross_ref_block = (
                            "\n## Cross-References (LLM)\n"
                            + "\n".join(link_lines)
                            + f"\n{self.sep}\n"
                        )
                        new_content = page.content.rstrip() + cross_ref_block
                        updated.append(
                            WikiPage(
                                page_id=page.page_id,
                                content=new_content,
                                metadata=page.metadata,
                            )
                        )
                        continue
            except Exception:
                logger.warning(
                    "Cross-reference generation failed for %s, keeping original",
                    page.page_id,
                    exc_info=True,
                )

            updated.append(page)

        return updated

    async def generate_community_page(
        self,
        community_funcs: List[Function],
        community_id: int,
    ) -> dict:
        """Generate a Concept Page for a GraphRAG-detected community.

        At most 20 functions are sent to the LLM to avoid token overflow.

        Parameters
        ----------
        community_funcs:
            Functions belonging to this community.
        community_id:
            Numeric identifier for the community.

        Returns
        -------
        Parsed JSON dict from the LLM response.
        """
        func_summaries = [
            f"{f.name}: {', '.join(fv.desc for fv in f.action[:1])}" for f in community_funcs[:20]
        ]
        safe_text = LLMPromptSanitizer.sanitize(
            "\n".join(func_summaries),
            self.max_input_length,
        )

        prompt = LLMPromptSanitizer.build_structured_prompt(
            instruction="分析以下功能节点的聚类，识别共同主题并生成简洁社区摘要",
            user_input=safe_text,
            output_schema={
                "community_theme": "str",
                "core_functions": ["str"],
                "summary": "str",
                "key_relationships": ["str"],
            },
        )
        return await self._llm.llm.complete_json(prompt)
