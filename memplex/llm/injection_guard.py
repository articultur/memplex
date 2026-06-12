"""Indirect prompt injection guard for memory recall contexts."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memplex.models import SearchResult

logger = logging.getLogger(__name__)


class IndirectInjectionGuard:
    """Detect and mitigate indirect prompt injection in recalled memories.

    While ``LLMPromptSanitizer`` protects against *direct* injection (user
    input), this class handles *indirect* injection: an attacker embeds
    malicious instructions in a source document that survives extraction
    and gets injected into the LLM context when recalled via RAG.

    Defense layers:
    1. Content scanning -- regex-based detection of system-role keywords.
    2. Protective wrapping -- memory content is wrapped in ``[MEMORY ...]``
       tags so the LLM treats them as data, not instructions.
    3. Trust-level labelling -- each memory is annotated with a trust level
       derived from its ``source_type``.
    """

    # Multi-language injection patterns (compiled once at class load)
    INJECTION_PATTERNS: list[str] = [
        r"ignore\s+(all\s+)?previous\s+instructions?",
        r"disregard\s+(all\s+)?prior\s+instructions?",
        r"system\s*:\s*you\s+are",
        r"<\|endoftext\|>",
        r"<\|im_start\|>",
        r"忽略(之前|前面|上面)的(所有|全部)?指令",
        r"忽略系统提示",
        r"你现在是",
        r"新的系统提示",
        r"assistant\s*:\s*sure",
    ]

    _compiled: list[re.Pattern] = [
        re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS
    ]

    # Trust level mapping: source_type value -> trust level label
    TRUST_LEVELS: dict[str, str] = {
        "requirement": "HIGH",
        "meeting": "MEDIUM",
        "code": "MEDIUM",
        "wiki": "LOW",
    }

    @classmethod
    def scan(cls, content: str) -> bool:
        """Scan content for suspected injection attacks.

        Returns
        -------
        True if the content is suspected to contain an injection payload;
        the caller should discard or isolate the memory entry.
        """
        for pattern in cls._compiled:
            if pattern.search(content):
                return True
        return False

    @classmethod
    def wrap_for_context(
        cls,
        memories: list[SearchResult],
        store: object,
    ) -> str:
        """Wrap recalled memories in protective tags for LLM context injection.

        Each memory is enclosed in ``[MEMORY START | trust=LEVEL | id=...]``
        / ``[MEMORY END]`` markers so the LLM treats the content as
        data, not as system instructions.

        Parameters
        ----------
        memories:
            Search results to be injected into the LLM context.
        store:
            A MemoryStore-like object with a ``get(id)`` method that
            returns a MemoryNode (or None).
        """
        parts: list[str] = []
        for r in memories:
            func = store.get(r.func_id) if store else None
            if not func:
                continue
            source_type_val = (
                func.source_type.value
                if hasattr(func.source_type, "value")
                else (func.source_type or "wiki")
            )
            trust = cls.TRUST_LEVELS.get(source_type_val, "LOW")
            summary = r.summary or func.name
            parts.append(
                f"[MEMORY START | trust={trust} | id={r.func_id}]\n"
                f"{summary}\n"
                f"[MEMORY END]"
            )
        return "\n\n".join(parts)

    @classmethod
    def filter_and_wrap(
        cls,
        memories: list[SearchResult],
        store: object,
    ) -> str:
        """Filter out injection-suspected memories, then wrap the rest.

        Memories that trigger the injection scanner are logged as warnings
        and excluded from the output.
        """
        safe: list[SearchResult] = []
        for r in memories:
            func = store.get(r.func_id) if store else None
            if func:
                memory_type = getattr(func, "memory_type", "function")
                text = cls._extract_scan_text(func, memory_type)
                if cls.scan(text):
                    logger.warning(
                        "Indirect injection detected in memory %s (type=%s), skipped.",
                        r.func_id,
                        memory_type,
                    )
                    continue
            safe.append(r)
        return cls.wrap_for_context(safe, store)

    @classmethod
    def _extract_scan_text(cls, func: object, memory_type: str) -> str:
        """Extract the relevant text fields for injection scanning by memory type."""
        if memory_type == "function":
            return " ".join(
                fv.desc
                for role in ("trigger", "condition", "action", "benefit")
                for fv in getattr(func, role, [])
            )
        if memory_type == "fact":
            return " ".join(
                filter(
                    None,
                    [
                        getattr(func, "subject", ""),
                        getattr(func, "predicate", ""),
                        getattr(func, "object_", ""),
                    ],
                )
            )
        if memory_type == "preference":
            return " ".join(
                filter(
                    None,
                    [
                        getattr(func, "aspect", ""),
                        getattr(func, "preference", ""),
                    ],
                )
            )
        if memory_type == "observation":
            return " ".join(
                filter(
                    None,
                    [
                        getattr(func, "event", ""),
                        getattr(func, "context", ""),
                    ],
                )
            )
        # Unknown type: scan all string attributes
        return " ".join(str(v) for v in vars(func).values() if isinstance(v, str))
