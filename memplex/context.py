"""Bounded context assembly from current, authorized source snapshots.

The caller supplies candidates in priority order (hot first, then retrieval
rank). This leaf owns projection, current safety, complete wrappers and the
final character-based estimate. It never imports storage or host adapters.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from threading import Lock
from types import MappingProxyType
from typing import Any, Literal
from unicodedata import category

from memplex.llm.injection_guard import IndirectInjectionGuard, InjectionRiskRegistry
from memplex.models import (
    Fact,
    Function,
    Observation,
    Preference,
    QueryResult,
    SearchResult,
    SourceType,
)
from memplex.temporal import is_valid_at

# Matches the existing model-facing retrieval candidate ceiling without a
# forbidden dependency on adapters. The service separately bounds lineage.
MAX_CONTEXT_CANDIDATES = 500


@dataclass(frozen=True)
class ContextCandidate:
    """A source reference, never a cached text or authorization copy."""

    memory_id: str
    origin: Literal["hot", "retrieval"]


@dataclass(frozen=True)
class ContextCacheKey:
    """Actual authorization identity and normalized query, without display defaults."""

    storage_namespace: str
    tenant_id: str
    subject_id: str
    workspace_id: str | None
    agent_id: str | None
    session_id: str | None
    normalized_query: str


def _valid_cached_candidates(value: Any) -> bool:
    """Reject historic rendered payloads and incompatible/body-bearing records."""
    return (
        type(value) is tuple and len(value) <= MAX_CONTEXT_CANDIDATES
        and all(
            type(candidate) is ContextCandidate
            and type(candidate.memory_id) is str
            and type(candidate.origin) is str
            and candidate.origin in {"hot", "retrieval"}
            for candidate in value
        )
    )


class ContextCandidateCache:
    """Service-local bounded FIFO of source references, never rendered context.

    Replacing a key preserves its original FIFO age; pop consumes it. Input
    traversal/validation happens before locking. No source lookup, rendering,
    database or model work occurs under the cache lock.
    """

    def __init__(self, max_entries: int = 64) -> None:
        self._max_entries = max(1, int(max_entries))
        self._lock = Lock()
        self._entries: dict[ContextCacheKey, tuple[ContextCandidate, ...]] = {}

    def put(self, key: ContextCacheKey, candidates: Sequence[ContextCandidate]) -> None:
        # islice bounds traversal even for a lazy/unbounded sequence; never
        # first materialize the whole input or call its potentially costly len.
        bounded = tuple(islice(candidates, MAX_CONTEXT_CANDIDATES))
        valid = _valid_cached_candidates(bounded)
        with self._lock:
            if not valid:
                self._entries.pop(key, None)
                return
            self._entries[key] = bounded
            while len(self._entries) > self._max_entries:
                del self._entries[next(iter(self._entries))]

    def pop(self, key: ContextCacheKey) -> tuple[ContextCandidate, ...] | None:
        with self._lock:
            candidates = self._entries.pop(key, None)
        return candidates if _valid_cached_candidates(candidates) else None

    def invalidate(self, memory_id: str | None = None) -> None:
        with self._lock:
            if memory_id is None:
                self._entries.clear()
                return
            for key, candidates in tuple(self._entries.items()):
                if not _valid_cached_candidates(candidates) or any(
                    candidate.memory_id == memory_id for candidate in candidates
                ):
                    del self._entries[key]


@dataclass(frozen=True)
class ContextFragment:
    """One accepted current snapshot projection, including its exact wrapper."""

    memory_id: str
    name: str
    domain: str
    # Historic raw sources may retain an unknown string (still LOW trust).
    source_type: Any
    context: str
    tokens_used: int


@dataclass(frozen=True)
class ContextAssembly:
    """Final model context; diagnostics contain aggregate reasons only."""

    context: str
    memory_ids: tuple[str, ...]
    tokens_used: int
    truncated: bool
    dropped: Mapping[str, int]
    fragments: tuple[ContextFragment, ...] = ()


def estimate_context_tokens(text: str) -> int:
    """Estimate characters, including all wrappers; not a model tokenizer."""
    return len(text) // 4 + 1 if text else 0


def _join_text(values: Sequence[Any], separator: str = " ") -> str:
    """Project only text, rejecting malformed model-visible fields."""
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            raise TypeError("context text fields must be strings")
        if value.strip():
            parts.append(value.strip())
    return separator.join(parts)


def current_node_text(node: Any) -> str:
    """Project the supported node's current body, excluding private metadata."""
    if isinstance(node, Function):
        fields = [node.name, node.domain]
        fields.extend(
            value.desc
            for role in (node.trigger, node.condition, node.action, node.benefit)
            for value in role if value.status == "active"
        )
        return _join_text(fields, "\n")
    if isinstance(node, Fact):
        return _join_text([node.subject, node.predicate, node.object_]) or _join_text([node.name])
    if isinstance(node, Preference):
        return _join_text([node.aspect, node.preference]) or _join_text([node.name])
    if isinstance(node, Observation):
        return _join_text([node.event, node.context]) or _join_text([node.name])
    if getattr(node, "memory_type", None) == "paragraph":
        if getattr(node, "context_historical", False):
            return ""
        return _join_text([node.raw_text])
    return ""


class _SnapshotLookup:
    """The guard can only re-read the already-resolved current snapshot."""

    def __init__(self, nodes: Mapping[str, Any]) -> None:
        self._nodes = nodes

    def get(self, memory_id: str) -> Any:
        return self._nodes.get(memory_id)


def _candidate_ids(
    candidates: Sequence[ContextCandidate], dropped: Counter[str],
) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        memory_id = candidate.memory_id
        if memory_id in seen:
            dropped["duplicate"] += 1
        elif len(ids) >= MAX_CONTEXT_CANDIDATES:
            dropped["candidate_limit"] += 1
        else:
            seen.add(memory_id)
            ids.append(memory_id)
    return ids


def _current_fragment(
    memory_id: str,
    node: Any,
    *,
    allow: Callable[[Any], bool],
    now: datetime,
    lookup: _SnapshotLookup,
    risk_registry: InjectionRiskRegistry | None,
) -> tuple[ContextFragment | None, str]:
    """Return a complete wrapper or one internal drop reason for this node."""
    try:
        if not memory_id or node is None or getattr(node, "id", None) != memory_id:
            return None, "missing"
        if not allow(node):
            return None, "denied"
        if isinstance(node, Fact) and not is_valid_at(node, now):
            return None, "expired"
        text = current_node_text(node)
        if not text:
            return None, "empty"
        name = _join_text([getattr(node, "name", "")])
        domain = _join_text([getattr(node, "domain", "")])
        source_type = getattr(node, "source_type", "wiki")
        if not isinstance(source_type, (SourceType, str)):
            raise TypeError("context source metadata must be immutable text or SourceType")
        # Public search also exposes these current metadata fields. Apply the
        # same framing/scanner boundary to that complete projected surface.
        projected = _join_text([
            name, domain, text,
            source_type.value if isinstance(source_type, SourceType) else source_type,
        ], "\n")
        # The existing scanner handles instruction attacks; also protect its
        # trusted framing from literal markers and header/control characters.
        if (any(char in "[]|\"'" or category(char) in {"Cc", "Cf", "Zl", "Zp"}
                for char in memory_id)
                or "[MEMORY START" in projected or "[MEMORY END]" in projected):
            return None, "unsafe"
        # Projection joins fields differently from the node's serialized
        # surface. Scan the exact model-visible body as well as retaining
        # the guard's complete-node and risk-registry decision below.
        if IndirectInjectionGuard.scan(projected):
            if risk_registry is not None:
                risk_registry.mark(memory_id)
            return None, "unsafe"
        result = SearchResult(
            func_id=memory_id, name="", domain="", relevance_score=0.0, summary=text,
        )
        fragment = IndirectInjectionGuard.filter_and_wrap(
            [result], lookup, risk_registry=risk_registry,
        )
        if not fragment:
            return None, "unsafe"
        return ContextFragment(
            memory_id=memory_id,
            name=name, domain=domain,
            source_type=source_type,
            context=fragment, tokens_used=estimate_context_tokens(fragment),
        ), ""
    except Exception:  # noqa: BLE001 - one malformed or unreadable node fails closed
        return None, "lookup_error"


def assemble_context(
    candidates: Sequence[ContextCandidate],
    *,
    resolve: Callable[[Sequence[str]], Mapping[str, Any]],
    allow: Callable[[Any], bool],
    max_tokens: int,
    now: datetime,
    risk_registry: InjectionRiskRegistry | None = None,
) -> ContextAssembly:
    """Resolve once, render current sources, and admit only whole fragments.

    Resolver failures propagate: a whole-service storage error is never
    disguised as a successful cached recall. Individual malformed nodes are
    omitted. Oversized fragments do not prevent later smaller ones fitting.
    """
    if max_tokens < 0:
        raise ValueError("max_tokens must be non-negative")
    dropped: Counter[str] = Counter()
    ids = _candidate_ids(candidates, dropped)
    nodes = dict(resolve(ids)) if ids else {}
    lookup = _SnapshotLookup(nodes)
    context = ""
    visible: list[str] = []
    fragments: list[ContextFragment] = []
    for memory_id in ids:
        fragment, reason = _current_fragment(
            memory_id, nodes.get(memory_id), allow=allow, now=now,
            lookup=lookup, risk_registry=risk_registry,
        )
        if fragment is None:
            dropped[reason] += 1
            continue
        proposed = f"{context}\n\n{fragment.context}" if context else fragment.context
        if estimate_context_tokens(proposed) > max_tokens:
            dropped["budget"] += 1
            continue
        context = proposed
        visible.append(memory_id)
        fragments.append(fragment)
    return ContextAssembly(
        context=context, memory_ids=tuple(visible),
        tokens_used=estimate_context_tokens(context),
        truncated=bool(dropped["budget"] or dropped["candidate_limit"]),
        dropped=MappingProxyType(dict(dropped)), fragments=tuple(fragments),
    )


def project_context_results(
    result: QueryResult, assembled: ContextAssembly, *, selected_top_k: int,
) -> QueryResult:
    """Replace ranked bodies/metadata with accepted fragments, retaining scores."""
    scores: dict[str, float] = {}
    for candidate in result.results:
        scores.setdefault(candidate.func_id, candidate.relevance_score)
    result.results = [
        SearchResult(
            func_id=item.memory_id, name=item.name, domain=item.domain,
            relevance_score=scores[item.memory_id], summary=item.context,
            source_type=item.source_type, token_estimate=item.tokens_used,
        )
        for item in assembled.fragments
    ]
    result.tokens_used = assembled.tokens_used
    result.truncated = result.truncated or assembled.truncated
    redact_context_explanation(result, selected_top_k=selected_top_k)
    return result


def redact_context_explanation(result: QueryResult, *, selected_top_k: int) -> None:
    """Expose record metadata only for accepted current fragments.

    Aggregate retrieval diagnostics remain separate from the memory budget.
    No rejected identity can survive only in the public explanation.
    """
    explanation = result.explanation
    if not isinstance(explanation, dict):
        return
    visible_ids = {item.func_id for item in result.results}
    explanation["results"] = [
        {
            "id": item.func_id,
            "name": item.name,
            "score": item.relevance_score,
            "domain": item.domain,
            "token_estimate": item.token_estimate,
            "source_type": getattr(item.source_type, "value", str(item.source_type)),
        }
        for item in result.results
    ]
    # Per-path candidate refs carry record ids and must be scrubbed to
    # the same authorized set -- a denied record cannot survive there.
    retrieval = explanation.get("retrieval")
    if isinstance(retrieval, dict):
        for path in retrieval.get("paths") or []:
            if not isinstance(path, dict):
                continue
            refs = path.get("candidate_refs")
            if isinstance(refs, list):
                path["candidate_refs"] = [
                    ref
                    for ref in refs
                    if isinstance(ref, dict) and ref.get("id") in visible_ids
                ]
    budget = explanation.get("budget")
    if isinstance(budget, dict):
        budget["max_tokens"] = result.max_tokens
        budget["tokens_used"] = result.tokens_used
        budget["truncated"] = result.truncated
    selection = explanation.get("selection")
    if isinstance(selection, dict):
        selection["public_top_k_limit"] = selected_top_k
        selection["after_runtime_authorization"] = len(result.results)
        selection["after_token_budget"] = len(result.results)
    boundaries = explanation.get("boundaries")
    if isinstance(boundaries, dict):
        boundaries["runtime_authorization"] = (
            "Record metadata is projected only after current source and caller authorization."
        )

