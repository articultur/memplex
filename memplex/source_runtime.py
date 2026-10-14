"""Service-owned current V1 reads; validation and pure projection share a fence."""
from __future__ import annotations

import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

from memplex.authorization import is_own_node_visible, source_authorization_node
from memplex.context import (
    ContextAssembly,
    ContextCandidate,
    assemble_context,
    project_context_results,
)
from memplex.source_contract import (
    FinalRead,
    RejectionCode,
    SourceBusyError,
    SourceContractError,
    SourceKind,
    ValidationLimits,
    ValidationResult,
)
from memplex.source_validation import finalize_at, validate_sources

if TYPE_CHECKING:
    from memplex.auth import AuthorizationContext
    from memplex.models import MemoryNode, PendingReview, QueryResult
    from memplex.service import MemplexService

T = TypeVar("T")


def _project_read(
    service: MemplexService, memory_ids: Sequence[str], *, authorization: AuthorizationContext,
    deadline_ns: int, project: Callable[[FinalRead], T],
) -> T:
    limits = ValidationLimits()
    hard_end = min(deadline_ns, time.monotonic_ns() + limits.max_fence_ms * 1_000_000)
    backend = service._v1_source_store(authorization)
    if backend is None:
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
    validated: ValidationResult | None = None
    phase, time_checked, projection_fixed = "fence", 0, False
    try:
        with backend.read_fence(authorization, deadline_ns=hard_end) as session:
            remaining = hard_end - time.monotonic_ns()
            if remaining <= 0:
                raise SourceBusyError()
            # Reserve one tenth of the remaining SAME hard budget for the
            # final clock, expiry checks and rendering. Never renew the fence.
            soft_end = hard_end - max(1, remaining // 10)
            phase = "validation"
            validated = validate_sources(
                session, memory_ids, authorization=authorization,
                own_acl=lambda snapshot, context: is_own_node_visible(source_authorization_node(snapshot), context),
                safe_for_model=lambda snapshot: service.is_safe_for_model(source_authorization_node(snapshot)),
                limits=limits, deadline_ns=soft_end,
            )
            phase = "final_time"
            if time.monotonic_ns() >= hard_end:
                raise SourceBusyError()
            # L is sampled only AFTER validation, inside the original fence.
            # Only detached expiry checks and pure projection may follow it.
            final = finalize_at(validated, at=session.utc_now())
            time_checked = len(validated.accepted)
            phase = "projection"
            result = project(final)
            if time.monotonic_ns() >= hard_end:
                raise SourceBusyError()
            projection_fixed, phase = True, "release"
        return result
    except Exception as exc:
        # Preserve the original failure with body-free proof-stage counts.
        proven = len(validated.accepted) if validated is not None else 0
        exc.add_note(f"source_read_phase={phase}; source_proven={proven}; "
                     f"source_unproven={len(set(memory_ids)) - proven}; time_checked={time_checked}; "
                     f"projection_fixed={int(projection_fixed)}")
        raise


def _render(
    service: MemplexService, candidates: Sequence[ContextCandidate], final: FinalRead, *,
    runtime_filter: Callable[[Any], bool], max_tokens: int,
) -> ContextAssembly:
    nodes = {memory_id: source_authorization_node(snapshot) for memory_id, snapshot in final.accepted.items()}
    assembled = assemble_context(
        candidates, resolve=lambda _ids: nodes, allow=runtime_filter,
        max_tokens=max_tokens, now=final.linearized_at, risk_registry=service._injection_risks,
    )
    dropped = Counter(assembled.dropped)
    # The renderer's absent nodes have already been diagnosed by the proof.
    dropped["missing"] = max(0, dropped["missing"] - sum(final.dropped.values()))
    dropped.update(final.dropped)
    return replace(assembled, dropped=MappingProxyType(dict(+dropped)),
                   truncated=assembled.truncated or bool(dropped["budget_exhausted"]))


def assemble_v1_context(
    service: MemplexService, candidates: Sequence[ContextCandidate], *,
    authorization: AuthorizationContext, runtime_filter: Callable[[Any], bool],
    max_tokens: int, deadline_ns: int,
) -> ContextAssembly:
    """Render accepted current sources before releasing their authority fence."""
    return _project_read(
        service, tuple(dict.fromkeys(candidate.memory_id for candidate in candidates)),
        authorization=authorization, deadline_ns=deadline_ns,
        project=lambda final: _render(service, candidates, final, runtime_filter=runtime_filter, max_tokens=max_tokens),
    )


def project_v1_results(
    service: MemplexService, result: QueryResult, *, authorization: AuthorizationContext,
    top_k: int, max_tokens: int, deadline_ns: int,
) -> QueryResult:
    """Keep candidate order/scores, replacing every body-bearing field at L."""
    if max_tokens < 0:
        raise ValueError("max_tokens must be non-negative")
    candidates = tuple(ContextCandidate(item.func_id, "retrieval") for item in result.results[:max(0, top_k)])

    def project(final: FinalRead) -> QueryResult:
        assembled = _render(service, candidates, final, runtime_filter=lambda _node: True,
                            max_tokens=max_tokens or sys.maxsize)
        # An explanation can contain arbitrary cached bodies at any depth.
        # Rebuild its allowed current result fields from an empty object.
        current = replace(result, explanation={} if result.explanation is not None else None,
                          max_tokens=max_tokens)
        current = project_context_results(current, assembled, selected_top_k=top_k)
        current.results = [replace(item, trust_tier=source_authorization_node(
            final.accepted[item.func_id]).trust_tier) for item in current.results]
        return current

    return _project_read(service, tuple(dict.fromkeys(item.memory_id for item in candidates)),
                         authorization=authorization, deadline_ns=deadline_ns, project=project)


def resolve_v1_nodes(
    service: MemplexService, memory_ids: Sequence[str], *, authorization: AuthorizationContext,
    deadline_ns: int,
) -> Mapping[str, MemoryNode | dict[str, object]]:
    """Detach only current authorized nodes; never use a caller or cached body."""
    def project(final: FinalRead) -> Mapping[str, MemoryNode | dict[str, object]]:
        nodes: dict[str, MemoryNode | dict[str, object]] = {}
        for memory_id, snapshot in final.accepted.items():
            node = source_authorization_node(snapshot)
            nodes[memory_id] = node.to_dict() if snapshot.head.ref.key.kind is SourceKind.RAW_PARAGRAPH else node
        return MappingProxyType(nodes)

    return _project_read(service, tuple(dict.fromkeys(memory_ids)), authorization=authorization,
                         deadline_ns=deadline_ns, project=project)


def source_timeline(
    service: MemplexService, memory_id: str, *, authorization: AuthorizationContext, deadline_ns: int,
) -> list[dict[str, object]]:
    """Current, authorized audit metadata; no legacy before/after body history."""
    def project(final: FinalRead) -> list[dict[str, object]]:
        snapshot = final.accepted.get(memory_id)
        if snapshot is None:
            return []
        head, token = snapshot.head, snapshot.head.ref.token
        return [{"state": head.state.value, "incarnation": token.incarnation,
                 "content_revision": token.content_revision, "auth_revision": token.auth_revision,
                 "commit_seq": head.last_commit_seq}]

    return _project_read(service, (memory_id,), authorization=authorization, deadline_ns=deadline_ns, project=project)


def project_v1_reviews(
    service: MemplexService, reviews: Sequence[PendingReview], *, authorization: AuthorizationContext,
    owner: str | None, limit: int, deadline_ns: int,
    runtime_filter: Callable[[Any], bool] | None = None,
) -> list[PendingReview]:
    """Rebuild pending review values from current nodes, never saved conflicts."""
    from memplex.models import FieldValue, PendingReview

    def project(final: FinalRead) -> list[PendingReview]:
        projected: list[PendingReview] = []
        for review in reviews:
            snapshot = final.accepted.get(review.memory_id)
            if snapshot is None or len(projected) >= max(0, limit):
                continue
            node = source_authorization_node(snapshot)
            if runtime_filter is not None and not runtime_filter(node):
                continue
            if owner is not None and getattr(node, "owner", None) != owner:
                continue
            # Field names are fixed model schema, not saved feedback text.
            if review.field_role not in {"trigger", "condition", "action", "benefit",
                                         "subject", "predicate", "object", "aspect", "preference",
                                         "event", "context"}:
                continue
            values = getattr(node, review.field_role, None)
            current = [value for value in values if isinstance(value, FieldValue)] if isinstance(values, list) else []
            projected.append(PendingReview(review.memory_id, review.field_role, current, source="current"))
        return projected

    return _project_read(service, tuple(dict.fromkeys(review.memory_id for review in reviews)),
                         authorization=authorization, deadline_ns=deadline_ns, project=project)
