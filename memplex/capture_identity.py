"""Identity keys for newly captured conversations, independent of their text.

Capture markers select stricter combination rules, never additional authority.
Existing unmarked memories retain their historical identity and merge behavior.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from memplex.models import ExtractedData, Function, MemoryNode

if TYPE_CHECKING:
    from memplex.auth import AuthorizationContext
    from memplex.models import GraphEdge

_CAPTURE_PREFIXES = ("func_capture_v1_", "fact_capture_v1_", "pref_capture_v1_")


def is_captured(node: MemoryNode) -> bool:
    """Recognize the persisted capture format without trusting editable metadata."""
    return node.id.startswith(_CAPTURE_PREFIXES)


def capture_scope(node: MemoryNode) -> tuple[str, ...] | None:
    """Return canonical overwrite scope; missing identity fails closed.

    User memories follow their owner across workspaces. Workspace captures
    deduplicate across sessions/hosts for the same owner. Session captures
    additionally require the originating host and session. Namespace strings
    are compatibility projections, and cannot supply missing authority here.
    """
    visibility = node.visibility
    if not isinstance(visibility, str) or visibility not in {"user", "workspace", "session"}:
        return None
    values = [node.tenant_id, node.owner_subject_id, visibility]
    if visibility in {"workspace", "session"}:
        values.append(node.workspace_id)
    if visibility == "session":
        if not isinstance(node.provenance, dict):
            return None
        values.extend([node.provenance.get("agent_id"), node.origin_session])
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    return tuple(value for value in values if isinstance(value, str))


def capture_scopes_match(left: MemoryNode, right: MemoryNode) -> bool:
    """Guard captured/direct supersession without changing ordinary pairs."""
    if not is_captured(left) and not is_captured(right):
        return True
    scope = capture_scope(left)
    return scope is not None and scope == capture_scope(right)


def _digest(values: tuple[str, ...]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_source_hint(context: AuthorizationContext) -> str:
    """Separate raw rows by their full write identity, including each session.

    PostgreSQL raw rows retain the first writer's ACL columns on conflict.
    Their physical identity must therefore be finer than workspace/user
    typed-memory deduplication scope. No principal data is embedded in text.
    """
    key = (
        "capture-raw-v1", context.principal.tenant_id, context.principal.subject_id,
        context.workspace_id, context.agent_id or "", context.session_id,
    )
    return "observation/capture-v1/" + _digest(key)


def scope_captured_data(extracted: ExtractedData) -> None:
    """Rekey already-authorized capture nodes and their internal graph edges."""
    nodes: dict[int, MemoryNode] = {}
    for node in [
        *extracted.functions, *extracted.facts, *extracted.preferences, *extracted.graph.nodes,
    ]:
        nodes[id(node)] = node
    mapping: dict[str, str] = {}
    scopes: dict[int, str] = {}
    prefixes = {"function": "func", "fact": "fact", "preference": "pref"}
    for identity, node in nodes.items():
        scope = capture_scope(node)
        if scope is None:
            raise ValueError("captured memory requires complete canonical identity")
        if isinstance(node, Function):
            # PG Function merge keeps a non-workspace/normalized-name row's
            # original writer. Use a finer physical key than logical user or
            # workspace merge scope, so a new writer cannot hit that row.
            scope = (
                *scope, node.workspace_id or "", node.provenance.get("agent_id") or "",
                node.origin_session or "",
            )
        scopes[identity] = _digest(("capture-scope-v1", *scope))
        prefix = prefixes[node.memory_type]
        mapping[node.id] = f"{prefix}_capture_v1_" + _digest((*scope, node.id))[:32]
    for identity, node in nodes.items():
        node.id = mapping[node.id]
        if isinstance(node, Function):
            # PG's normalized-name uniqueness is broader than personal
            # workspace capture identity. Keep the display name untouched.
            node.name_normalized = (
                f"capture-v1:{scopes[identity]}:{node.name_normalized or node.name}"
            )
    edges: dict[tuple[str, str, str], GraphEdge] = {}
    for edge in extracted.graph.edges:
        edge.source = mapping.get(edge.source, edge.source)
        edge.target = mapping.get(edge.target, edge.target)
        # GraphBuilder can relate an unscoped input to its already captured
        # alias. Rebinding must not turn that relation into a self-loop.
        if edge.source != edge.target:
            edges.setdefault((edge.source, edge.target, edge.edge_type), edge)
    extracted.graph.edges = list(edges.values())
