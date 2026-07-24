"""Memory-specific benchmark metrics.

These metrics evaluate memplex's unique memory capabilities rather than
RAG-style context retrieval.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List


def fact_retention_rate(
    query_result_ids: List[str],
    expected_ids: List[str],
    top_k: int,
) -> float:
    """What fraction of expected facts were retrieved in top_k?"""
    if not expected_ids:
        return 0.0
    found = sum(1 for eid in expected_ids if eid in query_result_ids[:top_k])
    return found / len(expected_ids)


def recency_ndcg(
    query_result_ids: List[str],
    items_by_recency: List[str],  # Most recent first
    top_k: int,
) -> float:
    """NDCG score for recency ranking.

    Perfect score when newer items appear before older items.
    """
    if not items_by_recency:
        return 0.0

    # Relevance = position in recency list (0 = most recent = highest relevance)
    def relevance(item_id: str) -> float:
        if item_id not in items_by_recency:
            return 0.0
        # Invert position so most recent has highest relevance
        pos = items_by_recency.index(item_id)
        return 1.0 / math.log2(pos + 2)

    # DCG
    dcg = sum(relevance(item_id) for item_id in query_result_ids[:top_k])

    # IDCG (ideal DCG)
    ideal_order = items_by_recency[:top_k]
    idcg = sum(relevance(item_id) for item_id in ideal_order)

    if idcg == 0:
        return 0.0
    return dcg / idcg


def memory_decay_curve(
    query_result_ids: List[str],
    item_id: str,
    time_steps: List[int],  # Hours since creation for each time step
    retrieval_times: List[int],  # When (in time steps) item was retrieved
) -> Dict[str, float]:
    """Measure how retrieval quality degrades over time.

    Returns per-time-step retention scores.
    """
    scores: Dict[str, float] = {}

    for step in time_steps:
        key = f"retention_at_{step}h"
        # Item should be retrieved at its creation time step
        expected_retrieval_step = time_steps[0]  # First time step
        if step < expected_retrieval_step:
            scores[key] = 1.0  # Future item shouldn't exist yet
        elif step == expected_retrieval_step:
            scores[key] = 1.0 if item_id in query_result_ids else 0.0
        else:
            # Older items should still be retrievable (no decay expected for facts)
            scores[key] = 1.0 if item_id in query_result_ids else 0.0

    return scores


def graph_connectivity(
    retrieved_ids: List[str],
    source_id: str,
    target_id: str,
    get_neighbors_fn: Any,  # Callable that takes func_id and returns neighbors
    max_hops: int = 2,
) -> float:
    """Can we traverse from source to target through graph connectivity?

    Returns 1.0 if path exists within max_hops, 0.0 otherwise.
    """
    if source_id not in retrieved_ids:
        return 0.0
    if source_id == target_id:
        return 1.0

    visited = {source_id}
    frontier = {source_id}

    for _ in range(max_hops):
        next_frontier = set()
        for fid in frontier:
            try:
                neighbors = get_neighbors_fn(fid)
                for neighbor in neighbors:
                    if neighbor.id == target_id:
                        return 1.0
                    if neighbor.id not in visited:
                        visited.add(neighbor.id)
                        next_frontier.add(neighbor.id)
            except Exception:
                pass
        frontier = next_frontier
        if not frontier:
            break

    return 0.0


def multi_hop_recall(
    retrieved_ids: List[str],
    required_hops: List[List[str]],  # List of ID sets per hop level
    top_k: int,
) -> Dict[str, float]:
    """Measure recall across multiple hop levels.

    required_hops[0] = IDs needed from first hop
    required_hops[1] = IDs needed from second hop
    etc.
    """
    scores: Dict[str, float] = {}

    for hop_level, required_ids in enumerate(required_hops):
        if not required_ids:
            continue

        retrieved_in_hop = [rid for rid in retrieved_ids[:top_k] if rid in required_ids]
        recall_key = f"hop_{hop_level}_recall"
        scores[recall_key] = len(retrieved_in_hop) / len(required_ids)

        # Overall multi-hop recall
        all_required = set().union(*[set(ids) for ids in required_hops])
        all_retrieved = set(retrieved_ids[:top_k])
        scores["multihop_recall"] = len(all_retrieved & all_required) / len(all_required)

    return scores
