"""Unified metrics framework for memplex benchmarks.

Provides standard IR metrics (Precision@K, Recall@K, MRR, NDCG) and
generation metrics (BLEU, ROUGE-L, F1, Exact Match), plus memory-specific
metrics for evaluating Memplex behavior.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from benchmarks.base import BenchmarkSample
from memplex.service import MemplexService

logger = logging.getLogger(__name__)


# ── Retrieval Metrics ──────────────────────────────────────────────────────────


def precision_at_k(
    retrieved: List[str],
    expected: List[str],
    k: int,
) -> float:
    """Compute Precision@K.

    Args:
        retrieved: List of retrieved item IDs (ordered by rank).
        expected: List of ground-truth relevant item IDs.
        k: Consider only top-k retrieved items.

    Returns:
        Fraction of top-k results that are relevant (0.0 to 1.0).
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if not expected:
        return 0.0

    top_k = retrieved[:k]
    relevant = sum(1 for item in top_k if item in set(expected))
    return relevant / k


def recall_at_k(
    retrieved: List[str],
    expected: List[str],
    k: int,
) -> float:
    """Compute Recall@K.

    Args:
        retrieved: List of retrieved item IDs (ordered by rank).
        expected: List of ground-truth relevant item IDs.
        k: Consider only top-k retrieved items.

    Returns:
        Fraction of relevant items found in top-k (0.0 to 1.0).
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if not expected:
        return 0.0

    top_k = retrieved[:k]
    expected_set = set(expected)
    relevant = sum(1 for item in top_k if item in expected_set)
    return relevant / len(expected_set)


def mrr(retrieved_ids: List[str], expected_ids: List[str]) -> float:
    """Compute Mean Reciprocal Rank (MRR).

    MRR = 1 / rank_of_first_relevant_item.
    Returns 0.0 if no relevant item is found.

    Args:
        retrieved_ids: List of retrieved item IDs (ordered by rank).
        expected_ids: List of ground-truth relevant item IDs.

    Returns:
        Reciprocal rank of first relevant item (0.0 to 1.0).
    """
    if not expected_ids:
        return 0.0

    expected_set = set(expected_ids)
    for rank, item_id in enumerate(retrieved_ids, start=1):
        if item_id in expected_set:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    retrieved: List[str],
    expected: List[str],
    k: int,
    relevance_fn: Optional[callable] = None,
) -> float:
    """Compute Normalized Discounted Cumulative Gain at K.

    Args:
        retrieved: List of retrieved item IDs (ordered by rank).
        expected: List of ground-truth relevant item IDs.
        k: Consider only top-k results.
        relevance_fn: Optional function(item_id) -> relevance score (0, 1, or higher).
                      Defaults to binary relevance (1 if in expected, 0 otherwise).

    Returns:
        NDCG@K score (0.0 to 1.0).
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    if not retrieved:
        return 0.0

    expected_set = set(expected)
    relevance_fn = relevance_fn or (lambda item_id: 1.0 if item_id in expected_set else 0.0)

    def dcg(items: List[str]) -> float:
        result = 0.0
        for i, item_id in enumerate(items[:k], start=1):
            rel = relevance_fn(item_id)
            result += rel / _log2(i + 1)
        return result

    top_k = retrieved[:k]
    dcg_val = dcg(top_k)

    # Ideal DCG: relevant items at the top
    ideal_order = [item_id for item_id in retrieved if item_id in expected_set]
    ideal_order.extend(item_id for item_id in retrieved if item_id not in expected_set)
    idcg_val = dcg(ideal_order[:k])

    if idcg_val == 0.0:
        return 0.0
    return dcg_val / idcg_val


def _log2(x: float) -> float:
    """Safe log2 that handles edge cases."""
    import math

    if x <= 1:
        return 0.0
    return math.log2(x)


# ── Generation Metrics ────────────────────────────────────────────────────────


def bleu(prediction: str, reference: str, n: int = 4) -> float:
    """Compute sentence-level BLEU-N score.

    Args:
        prediction: Predicted text.
        reference: Ground-truth reference text.
        n: Maximum n-gram order (default 4 for BLEU-4).

    Returns:
        BLEU score (0.0 to 1.0).
    """
    import math

    pred_tokens = prediction.lower().split()
    ref_tokens = reference.lower().split()

    if not pred_tokens or not ref_tokens:
        return 0.0

    # Compute n-gram precisions
    precisions = []
    for n_gram in range(1, min(n + 1, 5)):
        pred_ngrams = _get_ngrams(pred_tokens, n_gram)
        ref_ngrams = _get_ngrams(ref_tokens, n_gram)

        if not pred_ngrams:
            precisions.append(0.0)
            continue

        matches = sum(1 for ng in pred_ngrams if ng in ref_ngrams)
        total = len(pred_ngrams)
        precisions.append(matches / total if total > 0 else 0.0)

    # BLEU uses geometric mean of precisions
    valid_precisions = [p for p in precisions if p > 0]
    if not valid_precisions:
        return 0.0

    geo_mean = math.exp(sum(math.log(p) for p in valid_precisions) / len(valid_precisions))

    # Brevity penalty
    bp = (
        1.0
        if len(pred_tokens) >= len(ref_tokens)
        else math.exp(1 - len(ref_tokens) / len(pred_tokens))
    )

    return bp * geo_mean


def _get_ngrams(tokens: List[str], n: int) -> List[tuple]:
    """Extract n-grams from token list."""
    if n <= 0 or n > len(tokens):
        return []
    return list(zip(*[tokens[i:] for i in range(n)]))


def rouge_l(prediction: str, reference: str) -> float:
    """Compute ROUGE-L (Longest Common Subsequence) F1 score.

    ROUGE-L measures the longest common subsequence between prediction
    and reference, computing precision, recall, and F1.

    Args:
        prediction: Predicted text.
        reference: Ground-truth reference text.

    Returns:
        ROUGE-L F1 score (0.0 to 1.0).
    """
    pred_tokens = prediction.lower().split()
    ref_tokens = reference.lower().split()

    if not pred_tokens or not ref_tokens:
        return 0.0

    lcs_length = _lcs_length(pred_tokens, ref_tokens)

    precision = lcs_length / len(pred_tokens) if pred_tokens else 0.0
    recall = lcs_length / len(ref_tokens) if ref_tokens else 0.0

    if precision + recall == 0.0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _lcs_length(a: List[str], b: List[str]) -> int:
    """Compute length of longest common subsequence using dynamic programming."""
    m, n = len(a), len(b)
    # Space-optimized DP
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, [0] * (n + 1)

    return prev[n]


def f1_score(prediction: str, reference: str) -> float:
    """Compute token-level F1 score between prediction and reference.

    Args:
        prediction: Predicted text.
        reference: Ground-truth reference text.

    Returns:
        F1 score (0.0 to 1.0).
    """
    pred_tokens = set(prediction.lower().split())
    ref_tokens = set(reference.lower().split())

    if not pred_tokens and not ref_tokens:
        return 0.0
    if not pred_tokens or not ref_tokens:
        return 0.0

    tp = len(pred_tokens & ref_tokens)
    precision = tp / len(pred_tokens)
    recall = tp / len(ref_tokens)

    if precision + recall == 0.0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def exact_match(prediction: str, reference: str) -> float:
    """Check exact string equality (case-sensitive).

    Args:
        prediction: Predicted text.
        reference: Ground-truth reference text.

    Returns:
        1.0 if strings are exactly equal, 0.0 otherwise.
    """
    return 1.0 if prediction.strip() == reference.strip() else 0.0


def answer_contains(prediction: str, reference: str) -> float:
    """Check if prediction contains the reference answer (for QA).

    Useful when the retrieved summary should contain the answer string.

    Args:
        prediction: Retrieved summary text.
        reference: Expected answer text.

    Returns:
        1.0 if reference is contained in prediction, 0.0 otherwise.
    """
    return 1.0 if reference.strip() in prediction else 0.0


# ── Memory-Specific Metrics ────────────────────────────────────────────────────


class MemoryMetrics:
    """Memplex-specific metrics layered on top of standard IR metrics.

    These metrics evaluate memplex-specific behavior such as recency ordering
    and fact retention across compaction runs.
    """

    @staticmethod
    def recency_accuracy(
        service: MemplexService,
        sample: BenchmarkSample,
    ) -> float:
        """Measure how accurately memplex retrieves the most recent memory for a query.

        Compares access_count ordering vs temporal ordering of ground-truth
        memories to evaluate whether recent memories are ranked appropriately.

        Args:
            service: MemplexService instance to evaluate.
            sample: BenchmarkSample with expected_ids containing ground-truth.

        Returns:
            Accuracy score (0.0 to 1.0): fraction of ground-truth memories
            that appear in correct temporal order relative to each other.
        """
        if len(sample.expected_ids) < 2:
            return 1.0 if sample.expected_ids else 0.0

        # Get memory details to check access patterns
        memories = []
        for mem_id in sample.expected_ids:
            try:
                mem = service.get(mem_id)
                if mem:
                    memories.append(mem)
            except Exception as e:
                logger.debug("Could not retrieve memory %s: %s", mem_id, e)
                continue

        if len(memories) < 2:
            return 0.0

        # Compare temporal order with access_count order
        # Ideally, newer memories should have higher access counts
        temporal_order = sorted(memories, key=lambda m: getattr(m, "created_at", "") or "")
        access_order = sorted(
            memories, key=lambda m: getattr(m, "access_count", 0) or 0, reverse=True
        )

        # Score: how many pairs are in correct relative order
        correct_pairs = 0
        total_pairs = 0
        for i in range(len(temporal_order)):
            for j in range(i + 1, len(temporal_order)):
                total_pairs += 1
                # temporal_order[i] should come before temporal_order[j]
                # Check if access_order respects this
                id_i = getattr(temporal_order[i], "id", None)
                id_j = getattr(temporal_order[j], "id", None)
                pos_i = next(
                    (k for k, m in enumerate(access_order) if getattr(m, "id", None) == id_i),
                    -1,
                )
                pos_j = next(
                    (k for k, m in enumerate(access_order) if getattr(m, "id", None) == id_j),
                    -1,
                )
                if pos_i != -1 and pos_j != -1 and pos_i < pos_j:
                    correct_pairs += 1

        return correct_pairs / total_pairs if total_pairs > 0 else 0.0

    @staticmethod
    def fact_retention(
        service: MemplexService,
        sample: BenchmarkSample,
    ) -> float:
        """Measure what fraction of Facts are retained and retrievable.

        Evaluates whether subject→predicate→object triples remain accessible
        after compaction runs.

        Args:
            service: MemplexService instance to evaluate.
            sample: BenchmarkSample with expected_ids containing Fact memories.

        Returns:
            Retention score (0.0 to 1.0): fraction of expected facts that
            can still be retrieved.
        """
        if not sample.expected_ids:
            return 0.0

        retained = 0
        total = len(sample.expected_ids)

        for fact_id in sample.expected_ids:
            try:
                mem = service.get(fact_id)
                if mem is not None and getattr(mem, "id", None) == fact_id:
                    retained += 1
            except Exception as e:
                logger.debug("Could not retrieve fact %s: %s", fact_id, e)
                continue

        return retained / total if total > 0 else 0.0

    @staticmethod
    def graph_connectivity(
        service: MemplexService,
        sample: BenchmarkSample,
    ) -> float:
        """Measure graph connectivity for multi-hop reasoning.

        Evaluates whether memory nodes maintain proper edges for multi-hop
        queries like those in HotpotQA.

        Args:
            service: MemplexService instance to evaluate.
            sample: BenchmarkSample with metadata containing required edges.

        Returns:
            Connectivity score (0.0 to 1.0): fraction of required edges
            that exist between relevant memories.
        """
        required_edges = sample.metadata.get("required_edges", [])
        if not required_edges:
            return 1.0

        # required_edges format: [(source_id, target_id), ...]
        connected = 0
        total = len(required_edges)

        try:
            store = service.store if hasattr(service, "store") else None
            if not store:
                return 0.0

            for source_id, target_id in required_edges:
                neighbors = (
                    store.get_neighbors(source_id) if hasattr(store, "get_neighbors") else []
                )
                neighbor_ids = [getattr(n, "id", None) for n in neighbors]
                if target_id in neighbor_ids:
                    connected += 1
        except Exception as e:
            logger.debug("Error checking graph connectivity: %s", e)
            return 0.0

        return connected / total if total > 0 else 0.0

    @staticmethod
    def retrieval_latency_p95(
        service: MemplexService,
        samples: List[BenchmarkSample],
    ) -> float:
        """Compute 95th percentile retrieval latency.

        Args:
            service: MemplexService instance to evaluate.
            samples: List of samples to run for timing.

        Returns:
            P95 latency in milliseconds.
        """
        latencies = []
        for sample in samples:
            try:
                import time

                start = time.perf_counter()
                service.query(sample.query, top_k=10)
                elapsed = (time.perf_counter() - start) * 1000
                latencies.append(elapsed)
            except Exception as e:
                logger.debug("Query failed for sample %s: %s", sample.id, e)
                continue

        if not latencies:
            return 0.0

        sorted_latencies = sorted(latencies)
        idx = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[min(idx, len(sorted_latencies) - 1)]


# ── Aggregation Helpers ────────────────────────────────────────────────────────


def aggregate_metrics(results: List[float]) -> Dict[str, float]:
    """Aggregate a list of per-sample metric values.

    Args:
        results: List of metric values (0.0 to 1.0).

    Returns:
        Dict with keys: mean, median, std, min, max, count.
    """
    import statistics

    if not results:
        return {
            "mean": 0.0,
            "median": 0.0,
            "std": 0.0,
            "min": 0.0,
            "max": 0.0,
            "count": 0,
        }

    return {
        "mean": statistics.mean(results),
        "median": statistics.median(results),
        "std": statistics.stdev(results) if len(results) > 1 else 0.0,
        "min": min(results),
        "max": max(results),
        "count": len(results),
    }
