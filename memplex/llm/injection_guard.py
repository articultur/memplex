"""Indirect prompt injection guard for memory recall contexts."""

from __future__ import annotations

import logging
import re
from collections import deque
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

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
    INJECTION_PATTERNS: ClassVar[list[str]] = [        # --- Direct override (English) ---
        r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",
        r"disregard\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",
        r"forget\s+(all\s+)?(the\s+)?(above|previous|prior)\s+(instructions?|rules?|context)",
        r"disregard\s+(everything|all)\s+(above|before|prior)",
        r"override\s+(your\s+)?(system\s+)?(prompt|instructions?)",
        # --- Direct override (Chinese) ---
        r"忽略(之前|前面|上面|此前|以上)的?(所有|全部|一切)?(指令|指示|规则|内容|上下文)",
        r"忽略系统提示",
        r"disregard\s+上一条",  # bilingual mix
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

    _compiled: ClassVar[list[re.Pattern]] = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
    # Trust level mapping: source_type value -> trust level label
    TRUST_LEVELS: ClassVar[dict[str, str]] = {        "requirement": "HIGH",
        "meeting": "MEDIUM",
        "code": "MEDIUM",
        "wiki": "LOW",
    }
    MAX_MODEL_VISIBLE_STRINGS = 4096
    MAX_MODEL_VISIBLE_CHARS = 1_000_000

    @classmethod
    def scan(cls, content: str) -> bool:
        """Scan content for suspected injection attacks.

        Returns
        -------
        True if the content is suspected to contain an injection payload;
        the caller should discard or isolate the memory entry.
        """
        if not isinstance(content, str):
            # The caller supplied data that cannot be scanned.  It must not
            # enter a model context merely because a backend returned an
            # unexpected type.
            return True
        try:
            return any(pattern.search(content) for pattern in cls._compiled)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.warning("injection scanner failed closed: %s", exc)
            return True

    @classmethod
    def is_suspected(cls, node: object, *, fallback_text: str = "") -> bool:
        """Apply the same risk decision to every typed memory node.

        Function's historical ``attributes`` marker remains honoured, while
        Fact, Preference, and Observation rely on their typed textual fields.
        If a malformed node prevents field extraction, its result summary is
        still scanned; only that uncertain node is withheld when no safe
        fallback is available.
        """
        try:
            attrs = getattr(node, "attributes", {}) or {}
            if isinstance(attrs, dict) and attrs.get("memplex_injection_suspected") == "true":
                return True
            return cls.scan(cls._extract_model_visible_text(node))
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.warning("injection node inspection failed: %s", exc)
            if fallback_text:
                return cls.scan(fallback_text)
            return True

    @classmethod
    def wrap_for_context(
        cls,
        memories: list[SearchResult],
        store: Any,
        risk_registry: InjectionRiskRegistry | None = None,
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
            if _result_is_injection_suspected(r, store, risk_registry=risk_registry):
                continue
            try:
                func = store.get(r.func_id) if store else None
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                # A safe summary must not turn a lookup hiccup into a leak;
                # skip only this unresolved entry and continue with the rest.
                logger.warning("context wrapper lookup failed for %s: %s", r.func_id, exc)
                continue
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
        store: Any,
        risk_registry: InjectionRiskRegistry | None = None,
    ) -> str:
        """Filter out injection-suspected memories, then wrap the rest.

        Memories that trigger the injection scanner are logged as warnings
        and excluded from the output.
        """
        return cls.wrap_for_context(memories, store, risk_registry=risk_registry)

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

    @classmethod
    def _extract_model_visible_text(cls, node: object) -> str:
        """Collect every bounded string that a typed-node API may serialize.

        The historical field-specific extractor remains available for callers
        that need that exact projection.  Security decisions use the complete
        serialized surface so an attack cannot hide in ``name``, provenance,
        source paragraphs, graph evidence, or a future typed string field.
        """
        try:
            to_dict = getattr(node, "to_dict", None)
            root = to_dict() if callable(to_dict) else vars(node)
            strings: list[str] = []
            total_chars = 0
            seen: set[int] = set()

            def visit(value: object, depth: int = 0) -> None:
                nonlocal total_chars
                if depth > 10:
                    raise ValueError("model-visible node nesting exceeds scan limit")
                if isinstance(value, str):
                    if len(strings) >= cls.MAX_MODEL_VISIBLE_STRINGS:
                        raise ValueError("model-visible node has too many strings")
                    total_chars += len(value)
                    if total_chars > cls.MAX_MODEL_VISIBLE_CHARS:
                        raise ValueError("model-visible node text exceeds scan limit")
                    strings.append(value)
                    return
                if value is None or isinstance(value, (bool, int, float, bytes, Enum)):
                    return
                identifier = id(value)
                if identifier in seen:
                    return
                seen.add(identifier)
                if isinstance(value, dict):
                    if len(value) > cls.MAX_MODEL_VISIBLE_STRINGS:
                        raise ValueError("model-visible mapping exceeds scan limit")
                    for key, item in value.items():
                        visit(key, depth + 1)
                        visit(item, depth + 1)
                    return
                if isinstance(value, (list, tuple, set, frozenset)):
                    if len(value) > cls.MAX_MODEL_VISIBLE_STRINGS:
                        raise ValueError("model-visible collection exceeds scan limit")
                    for item in value:
                        visit(item, depth + 1)
                    return
                try:
                    attributes = vars(value)
                except TypeError:
                    return
                if len(attributes) > cls.MAX_MODEL_VISIBLE_STRINGS:
                    raise ValueError("model-visible attributes exceed scan limit")
                for key, item in attributes.items():
                    visit(key, depth + 1)
                    visit(item, depth + 1)

            visit(root)
            return "\n".join(strings)
        except Exception as exc:
            raise ValueError("model-visible node cannot be inspected safely") from exc


class InjectionScanCounter:
    """Date-bucketed counter for injection-suspected scan detections.

    Only the current day's count is ever reported (the health endpoint), so
    the counter prunes older date keys to stay bounded. Extracted from
    ``MemplexService`` so the service does not own low-level counter state;
    the service delegates to this collaborator.
    """

    __slots__ = ("_counts",)

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def prune(self, today: str) -> None:
        """Drop non-current date keys; cheap no-op when already current.

        The map is keyed by ``YYYY-MM-DD`` and only today's count is ever
        read, so older keys are dead weight.
        """
        if len(self._counts) > 1 or (self._counts and today not in self._counts):
            self._counts = {k: v for k, v in self._counts.items() if k == today}

    def increment(self, today: str) -> None:
        """Record one injection-suspected detection on the current day."""
        self._counts[today] = self._counts.get(today, 0) + 1

    def count(self, today: str) -> int:
        """Return the current day's detection count (0 when none recorded)."""
        return self._counts.get(today, 0)


class InjectionRiskRegistry:
    """Bounded in-process cache of node ids already judged unsafe.

    Typed models deliberately have no new serialized marker fields.  The
    registry preserves a risk decision across read surfaces in one service
    lifetime, while every read still re-scans durable node text after restart.
    """

    __slots__ = ("_ids", "_order")
    MAX_ENTRIES = 4096

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._order: deque[str] = deque()

    def contains(self, node_id: str) -> bool:
        return bool(node_id) and node_id in self._ids

    def mark(self, node_id: str) -> bool:
        """Remember *node_id* and return whether it was newly recorded."""
        if not node_id or node_id in self._ids:
            return False
        if len(self._order) >= self.MAX_ENTRIES:
            self._ids.discard(self._order.popleft())
        self._order.append(node_id)
        self._ids.add(node_id)
        return True


def _result_is_injection_suspected(
    result: SearchResult,
    store: Any,
    *,
    risk_registry: InjectionRiskRegistry | None = None,
) -> bool:
    """Return whether one retrieved result must be withheld from a model."""
    if risk_registry is not None and risk_registry.contains(result.func_id):
        return True
    try:
        node = store.get(result.func_id) if store else None
    except Exception as exc:  # noqa: BLE001 - logged degradation path
        logger.warning("injection filter: store.get failed for %s: %s", result.func_id, exc)
        node = None
    suspected = IndirectInjectionGuard.is_suspected(
        node,
        fallback_text=getattr(result, "summary", "") or "",
    ) if node is not None else IndirectInjectionGuard.scan(getattr(result, "summary", "") or "")
    if suspected and risk_registry is not None:
        risk_registry.mark(result.func_id)
    return suspected


def drop_injection_suspected(
    results: list[SearchResult],
    store: Any,
    *,
    risk_registry: InjectionRiskRegistry | None = None,
) -> list[SearchResult]:
    """Drop flagged or content-suspected results for every typed memory.

    A failed lookup does not erase unrelated safe results: its already
    retrieved summary is scanned and only a suspicious summary is withheld.
    """
    kept: list[SearchResult] = []
    for r in results:
        if not _result_is_injection_suspected(r, store, risk_registry=risk_registry):
            kept.append(r)
    return kept
