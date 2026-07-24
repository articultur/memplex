"""GraphBuilder -- construct graph edges from Function nodes.

Detects REFERENCES, DEPENDS_ON, CONFLICTS_WITH, ASSOCIATED_WITH, and
BELONGS_TO edges by analysing cross-references, name patterns, and
domain membership.

Works with :class:`MemoryStore` for persistence, unlike the legacy
``merger/graph_builder.py`` which was single-run only.

Usage::

    builder = GraphBuilder(store, config)
    edges = builder.process(func, existing_graph)
    builder.build_from_batch(functions)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Set

from memplex.models import (
    EdgeType,
    Function,
    GraphData,
    GraphEdge,
)

if TYPE_CHECKING:
    from memplex.config import GraphConfig, MemplexConfig
    from memplex.storage.base import MemoryStore

logger = logging.getLogger(__name__)


class GraphBuilder:
    """Build graph edges for Function nodes.

    Parameters
    ----------
    store:
        Active :class:`MemoryStore` backend (used for name lookups).
    config:
        Optional :class:`MemplexConfig` (reads ``graph`` sub-config).
    """

    def __init__(
        self,
        store: MemoryStore,
        config: Optional[MemplexConfig] = None,
    ) -> None:
        self._store = store
        self._config = config
        self._graph_config: Optional[GraphConfig] = config.graph if config else None

    # ── Public API ──────────────────────────────────────────────────

    def process(
        self,
        func: Function,
        existing_graph: Optional[GraphData] = None,
    ) -> List[GraphEdge]:
        """Detect and return edges for a single Function.

        Parameters
        ----------
        func:
            The Function to analyse.
        existing_graph:
            Current graph state (used to avoid duplicate edges and
            to look up neighbour nodes).  If ``None``, edges are
            computed from scratch.
        """
        edges: List[GraphEdge] = []
        existing_set = self._edge_set(existing_graph)

        # 1. REFERENCES -- from cross_references field
        for ref in func.cross_references:
            target_id = ref.get("target_id", "") if isinstance(ref, dict) else ""
            target_name = ref.get("target", "") if isinstance(ref, dict) else str(ref)
            if target_id:
                edge = self._make_edge(
                    source=func.id,
                    target=target_id,
                    edge_type=EdgeType.REFERENCES.value,
                    evidence=[f"cross_reference from {func.name}"],
                )
            elif target_name:
                resolved = self._resolve_by_name(target_name)
                if resolved:
                    edge = self._make_edge(
                        source=func.id,
                        target=resolved,
                        edge_type=EdgeType.REFERENCES.value,
                        evidence=[f"cross_reference: {func.name} -> {target_name}"],
                    )
                else:
                    continue
            else:
                continue

            if self._edge_key(edge) not in existing_set:
                edges.append(edge)
                existing_set.add(self._edge_key(edge))

        # 2. DEPENDS_ON -- from action field references
        all_funcs = self._get_all_funcs()
        for other in all_funcs:
            if other.id == func.id:
                continue
            if self._has_name_reference(func, other):
                key = (func.id, other.id, EdgeType.DEPENDS_ON.value)
                if key not in existing_set:
                    edges.append(
                        self._make_edge(
                            source=func.id,
                            target=other.id,
                            edge_type=EdgeType.DEPENDS_ON.value,
                            evidence=[f"{func.name} references {other.name}"],
                        )
                    )
                    existing_set.add(key)

        # 3. CONFLICTS_WITH -- same domain, overlapping trigger/action
        for other in all_funcs:
            if other.id == func.id:
                continue
            if self._detect_conflict(func, other):
                key = (func.id, other.id, EdgeType.CONFLICTS_WITH.value)
                rev_key = (other.id, func.id, EdgeType.CONFLICTS_WITH.value)
                if key not in existing_set and rev_key not in existing_set:
                    edges.append(
                        self._make_edge(
                            source=func.id,
                            target=other.id,
                            edge_type=EdgeType.CONFLICTS_WITH.value,
                            evidence=[
                                f"conflicting definitions in domain {func.domain or 'unknown'}"
                            ],
                        )
                    )
                    existing_set.add(key)

        # 4. BELONGS_TO -- domain membership
        if func.domain:
            domain_id = f"domain_{func.domain.replace(' ', '_').lower()}"
            key = (func.id, domain_id, EdgeType.BELONGS_TO.value)
            if key not in existing_set:
                edges.append(
                    self._make_edge(
                        source=func.id,
                        target=domain_id,
                        edge_type=EdgeType.BELONGS_TO.value,
                        evidence=[f"{func.name} belongs to {func.domain}"],
                    )
                )
                existing_set.add(key)

        # 5. ASSOCIATED_WITH -- shared domain with other functions
        if func.domain:
            for other in all_funcs:
                if other.id == func.id:
                    continue
                if other.domain == func.domain:
                    key = (func.id, other.id, EdgeType.ASSOCIATED_WITH.value)
                    rev_key = (other.id, func.id, EdgeType.ASSOCIATED_WITH.value)
                    if key not in existing_set and rev_key not in existing_set:
                        edges.append(
                            self._make_edge(
                                source=func.id,
                                target=other.id,
                                edge_type=EdgeType.ASSOCIATED_WITH.value,
                                weight=0.5,
                                evidence=[f"shared domain: {func.domain}"],
                            )
                        )
                        existing_set.add(key)

        return edges

    def build_from_batch(
        self,
        funcs: List[Function],
    ) -> List[GraphEdge]:
        """Build edges for a batch of Functions.

        The graph is built incrementally: each Function sees edges
        from previously processed Functions in the same batch.
        """
        all_edges: List[GraphEdge] = []
        accumulated_graph = GraphData(nodes=[], edges=[])

        for func in funcs:
            accumulated_graph.nodes.append(func)
            new_edges = self.process(func, accumulated_graph)
            all_edges.extend(new_edges)
            accumulated_graph.edges.extend(new_edges)

        return all_edges

    # ── Edge detection helpers ──────────────────────────────────────

    def _has_name_reference(self, source: Function, target: Function) -> bool:
        """Check if *source* mentions *target*'s name in its action field."""
        target_name = target.name.lower()
        if not target_name:
            return False
        for fv in source.action:
            if target_name in fv.desc.lower():
                return True
        for fv in source.trigger:
            if target_name in fv.desc.lower():
                return True
        return False

    def _detect_conflict(self, a: Function, b: Function) -> bool:
        """Detect if two Functions in the same domain conflict.

        Conflict heuristic:
        - Same domain (non-empty)
        - Overlapping trigger descriptions (substring match)
        """
        if not a.domain or a.domain != b.domain:
            return False
        a_triggers = {fv.desc.lower() for fv in a.trigger}
        b_triggers = {fv.desc.lower() for fv in b.trigger}
        return bool(a_triggers & b_triggers)

    # ── Utility helpers ─────────────────────────────────────────────

    @staticmethod
    def _make_edge(
        source: str,
        target: str,
        edge_type: str,
        weight: float = 1.0,
        evidence: Optional[List[str]] = None,
    ) -> GraphEdge:
        return GraphEdge(
            source=source,
            target=target,
            edge_type=edge_type,
            weight=weight,
            evidence=evidence or [],
            created_at=datetime.now(),
        )

    @staticmethod
    def _edge_key(edge: GraphEdge) -> tuple:
        return (edge.source, edge.target, edge.edge_type)

    @staticmethod
    def _edge_set(graph: Optional[GraphData]) -> Set[tuple]:
        if graph is None:
            return set()
        return {(e.source, e.target, e.edge_type) for e in graph.edges}

    def _resolve_by_name(self, name: str) -> Optional[str]:
        """Look up a Function ID by its name via the store."""
        try:
            funcs = self._store.list_functions(limit=100000)
            for f in funcs:
                if f.name == name:
                    return f.id
        except Exception:
            pass
        return None

    def _get_all_funcs(self) -> List[Function]:
        """Retrieve all stored Functions (cached per build batch)."""
        if not hasattr(self, "_funcs_cache"):
            try:
                self._funcs_cache = self._store.list_functions(limit=100000)
            except Exception:
                self._funcs_cache = []
        return self._funcs_cache

    def invalidate_cache(self) -> None:
        """Clear the internal function list cache."""
        if hasattr(self, "_funcs_cache"):
            del self._funcs_cache


# ── Rule-based fallback (no store) ───────────────────────────────────


def build_edges_rule_based(functions: List[Function]) -> List[GraphEdge]:
    """Simple rule-based edge detection when no store is available.

    Detects REFERENCES edges from ``func.cross_references`` (matching by
    name/name_normalized) and ASSOCIATED_WITH edges from shared domains.
    Used by :class:`CoreEngine._build_graph` as the fallback when the
    store-aware :class:`GraphBuilder` cannot run (no store available).
    """
    edges: List[GraphEdge] = []
    seen: set = set()

    for func in functions:
        # REFERENCES from cross-references
        for ref in func.cross_references:
            target = ref.get("target", "")
            if not target:
                continue
            # Find matching function by name
            for other in functions:
                if other.id == func.id:
                    continue
                if target.lower() in other.name.lower() or target.lower() in other.name_normalized:
                    key = (func.id, other.id, "REFERENCES")
                    if key not in seen:
                        seen.add(key)
                        edges.append(
                            GraphEdge(
                                source=func.id,
                                target=other.id,
                                edge_type="REFERENCES",
                                weight=1.0,
                                evidence=[f"cross-reference: {func.name} -> {other.name}"],
                                created_at=datetime.now(),
                            )
                        )

        # ASSOCIATED_WITH: shared domain
        if func.domain:
            for other in functions:
                if other.id == func.id:
                    continue
                if other.domain == func.domain:
                    key = (func.id, other.id, "ASSOCIATED_WITH")
                    rev_key = (other.id, func.id, "ASSOCIATED_WITH")
                    if key not in seen and rev_key not in seen:
                        seen.add(key)
                        edges.append(
                            GraphEdge(
                                source=func.id,
                                target=other.id,
                                edge_type="ASSOCIATED_WITH",
                                weight=0.5,
                                evidence=[f"shared domain: {func.domain}"],
                                created_at=datetime.now(),
                            )
                        )

    return edges
