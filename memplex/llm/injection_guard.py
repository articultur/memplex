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

    # Multi-language injection patterns (compiled once at class load).
    # Organized by family; each family closes a known bypass class. This is
    # a defense-in-depth tripwire, not a complete barrier -- the read path
    # also drops flagged memories (service.query) and wraps the rest in
    # [MEMORY ...] trust tags (filter_and_wrap).
    INJECTION_PATTERNS: list[str] = [
        # --- Direct override (English) ---
        r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",
        r"disregard\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",
        r"forget\s+(all\s+)?(the\s+)?(above|previous|prior)\s+(instructions?|rules?|context)",
        r"disregard\s+(everything|all)\s+(above|before|prior)",
        r"override\s+(your\s+)?(system\s+)?(prompt|instructions?)",
        # --- Direct override (Chinese) ---
        r"忽略(之前|前面|上面|此前|以上)的?(所有|全部|一切)?(指令|指示|规则|内容|上下文)",
        r"忽略系统提示",
        r" disregard 上一条",  # bilingual mix
        r"你现在是",
        r"新的系统提示",
        r"请(忽略|无视)(以上|之前|前面|上述)",
        # --- Role hijack / persona rewrite ---
        r"system\s*:\s*you\s+are",
        r"act\s+as\s+(?:if\s+you\s+were\s+)?(?:a\s+|an\s+)?(?:different|new|unrestricted)\s+(?:ai|assistant|persona|character|model)",
        r"pretend\s+(?:to\s+be|you\s+are)\s+(?:a|an\s+)?\s*(?:different|new|unrestricted)",
        r"from\s+now\s+on\s+you\s+(are|will\s+be|act\s+as)",
        r"你的新身份",
        r"扮演(一个|新的)?(不同的|无限制的)?(角色|助手|AI)",
        # --- Special-token boundary injection (multi-format) ---
        r"<\|endoftext\|>",
        r"<\|im_start\|>",
        r"<\|system\|>",
        r"<\|assistant\|>",
        r"\[/?system\]",
        r"\[/?INST\]",
        r"<</?SYS>>",
        # --- Pre-filled assistant turn / jailbreak scaffolding ---
        r"assistant\s*:\s*sure",
        r"assistant\s*:\s*ok(ay)?[,\.]\s*(i\s+)?(will|can|do)",
        r"chatgpt\s*:\s*sure",
        # --- Tool / capability abuse disguised as memory ---
        r"(execute|run|eval)\s+(the\s+)?following\s+(command|code|python)",
        r"sudo\s+(rm|chmod|chown|cat|cp|mv)\s+",
        # --- Exfiltration prompts ---
        r"(reveal|show|print|repeat)\s+(the\s+)?(system\s+)?(prompt|instructions?|rules?)",
        r"(输出|显示|打印)(你的)?(系统提示|系统指令|初始指令|隐藏规则)",
    ]

    _compiled: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

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
                f"[MEMORY START | trust={trust} | id={r.func_id}]\n{summary}\n[MEMORY END]"
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
