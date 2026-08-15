"""GraphRAG community detection using the Louvain algorithm.

Detects communities (clusters) in the knowledge graph and generates
concept pages for each community.  Falls back to domain-based grouping
when python-louvain / networkx are not available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

from memplex.models import (
    GraphData,
    WikiPage,
)

if TYPE_CHECKING:
    from memplex.storage.base import MemoryStore

logger = logging.getLogger(__name__)

# Only use strong-relationship edge types for community detection
STRONG_EDGE_TYPES = frozenset(
    {
        "REFERENCES",
        "DEPENDS_ON",
        "IMPLEMENTS",
        "ASSOCIATED_WITH",
    }
)

DEFAULT_MIN_COMMUNITY_SIZE = 3


@dataclass
class Community:
    """A detected community of function nodes."""

    community_id: int
    node_ids: List[str] = field(default_factory=list)
    theme: str = ""

    @property
    def size(self) -> int:
        return len(self.node_ids)


class GraphCommunityDetector:
    """Louvain-based graph community detector.

    Detects communities (clusters) in the knowledge graph using the
    Louvain algorithm.  Falls back to domain-based grouping when
    ``python-louvain`` or ``networkx`` are not installed.

    Parameters
    ----------
    min_community_size:
        Minimum number of nodes for a community to be reported.
        Communities below this threshold are discarded as noise.
    """

    def __init__(
        self,
        min_community_size: int = DEFAULT_MIN_COMMUNITY_SIZE,
    ) -> None:
        self.min_community_size = min_community_size

    def detect_communities(
        self,
        graph: GraphData,
        min_size: Optional[int] = None,
    ) -> List[Community]:
        """Detect communities in the graph.

        Parameters
        ----------
        graph:
            The graph data containing nodes and edges.
        min_size:
            Override minimum community size for this call.

        Returns
        -------
        List of Community objects, each with a list of node IDs.
        """
        threshold = min_size if min_size is not None else self.min_community_size

        try:
            import community as community_louvain  # type: ignore
            import networkx as nx  # type: ignore
        except ImportError:
            logger.info("python-louvain/networkx not available, falling back to domain grouping")
            return self._fallback_domain_grouping(graph, threshold)

        # Build networkx graph with strong edges only
        G = nx.Graph()
        for node in graph.nodes:
            node_id = node.id if hasattr(node, "id") else str(node)
            G.add_node(node_id)
        for edge in graph.edges:
            if edge.edge_type in STRONG_EDGE_TYPES:
                G.add_edge(edge.source, edge.target, weight=edge.weight)

        if G.number_of_edges() == 0:
            logger.info("No strong edges found, falling back to domain grouping")
            return self._fallback_domain_grouping(graph, threshold)

        partition = community_louvain.best_partition(G)
        groups: Dict[int, List[str]] = {}
        for node_id, comm_id in partition.items():
            groups.setdefault(comm_id, []).append(node_id)

        communities: List[Community] = []
        for comm_id, node_ids in groups.items():
            if len(node_ids) >= threshold:
                communities.append(
                    Community(
                        community_id=comm_id,
                        node_ids=node_ids,
                    )
                )

        return communities

    def generate_concept_pages(
        self,
        communities: List[Community],
        store: MemoryStore,
    ) -> List[WikiPage]:
        """Generate concept pages for detected communities.

        Each community gets a concept page listing its member functions.

        Parameters
        ----------
        communities:
            Detected communities.
        store:
            MemoryStore for looking up Function details.

        Returns
        -------
        List of WikiPage objects for the communities.
        """
        pages: List[WikiPage] = []

        for community in communities:
            lines: list[str] = [
                f"# Community {community.community_id}",
                "",
            ]
            if community.theme:
                lines.append(f"**Theme:** {community.theme}")
                lines.append("")

            lines.append(f"**Members:** {len(community.node_ids)} functions")
            lines.append("")

            lines.append("## Functions")
            lines.append("")

            for node_id in community.node_ids:
                func = store.get(node_id)
                if func:
                    lines.append(
                        f"- [[{func.id}]] -- "
                        f"{func.domain or 'uncategorized'} "
                        f"(confidence: {func.confidence:.2f})"
                    )
                else:
                    lines.append(f"- [[{node_id}]]")

            lines.append("")

            page_id = f"community_{community.community_id}"
            content = "\n".join(lines)

            pages.append(
                WikiPage(
                    page_id=page_id,
                    content=content,
                    metadata={
                        "type": "community",
                        "community_id": community.community_id,
                        "member_count": len(community.node_ids),
                        "theme": community.theme,
                    },
                )
            )

        return pages

    # ── Private helpers ───────────────────────────────────────────────

    def _fallback_domain_grouping(
        self,
        graph: GraphData,
        min_size: int,
    ) -> List[Community]:
        """Degraded community detection: group nodes by domain field."""
        groups: Dict[str, List[str]] = {}
        for node in graph.nodes:
            node_id = node.id if hasattr(node, "id") else str(node)
            domain = getattr(node, "domain", None) or "uncategorized"
            groups.setdefault(domain, []).append(node_id)

        communities: List[Community] = []
        for idx, (domain, node_ids) in enumerate(sorted(groups.items())):
            if len(node_ids) >= min_size:
                communities.append(
                    Community(
                        community_id=idx,
                        node_ids=node_ids,
                        theme=domain,
                    )
                )
        return communities
