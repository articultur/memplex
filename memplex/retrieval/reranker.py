"""Reranker -- multi-dimensional result re-ranking + optional CrossEncoder.

Two-stage retrieval architecture::

    Stage 1 (bi-encoder, fast):
        Reranker scores candidates across 6 dimensions and returns top-K.

    Stage 2 (cross-encoder, precise, optional):
        CrossEncoderReranker re-scores the top-K with a jointly-encoded model
        for significantly higher accuracy on ambiguous queries.

Usage::

    reranker = Reranker(embedding_service)
    ranked = reranker.rerank("query text", search_results, top_k=10)

    # Optional stage 2
    cross = CrossEncoderReranker(enabled=True)
    ranked = cross.rerank("query text", ranked)
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


from typing import TYPE_CHECKING, Dict, List, Optional

from memplex.models import SearchResult, SourceType
from memplex.retrieval.embedding import EmbeddingService, Vector

if TYPE_CHECKING:
    from memplex.models import Function
    from memplex.storage.base import MemoryStore

logger = logging.getLogger(__name__)


# ── Helper ────────────────────────────────────────────────────────────


def cosine_similarity(a: Vector, b: Vector) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    return dot / (norm_a * norm_b + 1e-8)


# ── 6-dimensional Reranker ────────────────────────────────────────────


class Reranker:
    """Multi-path retrieval result re-ranker.

    Scoring dimensions and default weights::

        raw_relevance       0.25  -- original score from each retrieval path
        semantic_similarity 0.30  -- cosine(query_vec, result_vec)
        recency_decay       0.15  -- exponential decay (~0.61 at 30 days)
        source_authority    0.10  -- requirement > meeting > code > wiki
        frequency           0.10  -- log-scaled access count * recency
        confidence          0.10  -- extraction-quality score persisted on the
                                     node (clamped [0, 1], neutral 0.5 if absent)

    Parameters
    ----------
    embedding_service:
        Provides ``embed()`` for computing semantic similarity.
    weights:
        Optional custom dimension weights (must sum to ~1.0).
    storage:
        Optional :class:`MemoryStore` for reading *access_count*.
    """

    _SOURCE_WEIGHTS: Dict[SourceType, float] = {
        SourceType.REQUIREMENT: 1.0,
        SourceType.MEETING: 0.8,
        SourceType.CODE: 0.6,
        SourceType.WIKI: 0.4,
    }

    def __init__(
        self,
        embedding_service: EmbeddingService,
        weights: Optional[Dict[str, float]] = None,
        storage: Optional["MemoryStore"] = None,
        recency_halflife_days: float = 60.0,
    ) -> None:
        self.embedder = embedding_service
        self.storage = storage
        self.weights = weights or {
            "raw_relevance": 0.25,
            "semantic_similarity": 0.30,
            "recency_decay": 0.15,
            "source_authority": 0.10,
            "frequency": 0.10,
            "confidence": 0.10,
        }
        # Exponential recency half-life in days (Mnemosyne-style knob):
        # score = exp(-days_since_update / halflife).
        self.recency_halflife_days = max(1e-6, float(recency_halflife_days))

    # ── Public API ──────────────────────────────────────────────────

    def rerank(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int = 10,
        query_vector: Optional[Vector] = None,
    ) -> List[SearchResult]:
        """Re-rank *results* using the 6-dimensional scoring model.

        Parameters
        ----------
        query:
            Original query text.
        results:
            Candidate results from multi-path retrieval.
        top_k:
            Maximum number of results to return.
        query_vector:
            Pre-computed query embedding (avoids re-embedding).
        """
        if not results:
            return []

        if query_vector is None:
            query_vector = self._embed_query_text(query)

        # Pre-load all Function nodes in one batch (or per-id fallback),
        # avoiding a per-result storage round-trip that compounds on
        # backends with O(N) reads (2026-08 review).
        node_map: dict[str, "Function"] = {}
        if self.storage is not None:
            unique_ids = list({r.func_id for r in results})
            get_many = getattr(self.storage, "get_many", None)
            if callable(get_many):
                try:
                    node_map = {k: v for k, v in get_many(unique_ids).items() if v is not None}
                except Exception as exc:
                    logger.debug("rerank: batch get_many failed (%s); falling back", exc)
                    node_map = {}
            for missing_id in unique_ids:
                if missing_id not in node_map:
                    try:
                        node = self.storage.get(missing_id)
                        if node is not None:
                            node_map[missing_id] = node
                    except Exception as exc:
                        logger.debug("rerank: storage.get failed for %s: %s", missing_id, exc)

        scored: list[tuple[float, SearchResult]] = []

        for r in results:
            # 1. Raw relevance from the retrieval path
            raw_score = r.relevance_score

            # 2. Semantic similarity (reuse vector_cache when available)
            if r.vector_cache is not None:
                result_vector = r.vector_cache
            else:
                result_vector = self._embed_query_text(r.summary)
            semantic_score = cosine_similarity(query_vector, result_vector)

            # 3. Recency decay
            recency_score = self._recency_decay(r.updated_at)

            # 4. Source authority
            source_weight = self._source_weight(r.source_type)

            # 5. Frequency (access count * recency of last access)
            func = node_map.get(r.func_id)
            frequency_score = self._frequency_score(func) if func else 0.5

            # 6. Confidence: extraction-quality score persisted on the node
            #    (Hindsight-style per-memory belief strength).
            confidence_score = self._confidence_score(func)

            # Weighted sum
            final_score = (
                raw_score * self.weights["raw_relevance"]
                + semantic_score * self.weights["semantic_similarity"]
                + recency_score * self.weights["recency_decay"]
                + source_weight * self.weights["source_authority"]
                + frequency_score * self.weights["frequency"]
                + confidence_score * self.weights.get("confidence", 0.0)
            )

            scored.append((final_score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]

    # ── Dimension scorers ───────────────────────────────────────────

    def _embed_query_text(self, text: str) -> Vector:
        """Embed query-time text without polluting TF-IDF corpus statistics.

        Uses ``embed_query`` when the embedding service provides it (TF-IDF
        backend), otherwise falls back to ``embed``.
        """
        embed_query = getattr(self.embedder, "embed_query", None)
        if callable(embed_query):
            return embed_query(text)
        return self.embedder.embed(text)

    def _recency_decay(self, updated_at: Optional[datetime] | str) -> float:
        """Exponential time decay, range (0, 1], 0.5 at ``halflife * ln 2`` days.

        ``score = exp(-days_since_update / halflife)`` where the half-life is
        configurable (``reranker.recency_halflife_days``, default 60 →
        ~0.61 at 30 days).
        """
        if updated_at is None:
            return 0.5
        # Handle both datetime objects and ISO strings
        if isinstance(updated_at, str):
            try:
                updated_at = datetime.fromisoformat(updated_at)
            except (ValueError, TypeError):
                return 0.5
        days_since = max(0, (datetime.now(timezone.utc) - _ensure_aware(updated_at)).days)
        return min(1.0, math.exp(-days_since / self.recency_halflife_days))

    @staticmethod
    def _confidence_score(func: Optional["Function"]) -> float:
        """Extraction-quality confidence persisted on the node, clamped [0, 1].

        Falls back to a neutral 0.5 when the node (or its confidence field)
        is unavailable, so the dimension never dominates by absence.
        """
        confidence = getattr(func, "confidence", None)
        if confidence is None:
            return 0.5
        try:
            value = float(confidence)
        except (TypeError, ValueError):
            return 0.5
        if value != value:  # NaN guard
            return 0.5
        return min(1.0, max(0.0, value))

    def _source_weight(self, source_type: SourceType) -> float:
        """Authority weight by source type.

        requirement=1.0 > meeting=0.8 > code=0.6 > wiki=0.4.
        """
        return self._SOURCE_WEIGHTS.get(source_type, 0.5)

    @staticmethod
    def _frequency_score(func: "Function") -> float:
        """Access-frequency score combining count and recency.

        ``freq = log(1+count) / log(1+100)`` normalised to [0, 1].
        Combined with a last-access recency factor: 60% freq + 40% recency.
        """
        access_count = getattr(func, "access_count", 0)
        last_accessed = getattr(func, "last_accessed_at", None)

        # Frequency factor: log-scaled, normalised
        freq = math.log1p(access_count) / math.log1p(100)

        # Recency of last access
        if last_accessed is not None:
            if isinstance(last_accessed, str):
                try:
                    last_accessed = datetime.fromisoformat(last_accessed)
                except (ValueError, TypeError):
                    last_accessed = None
            if last_accessed is not None:
                days = max(0, (datetime.now(timezone.utc) - _ensure_aware(last_accessed)).days)
                recency = min(1.0, math.exp(-days / 60))
            else:
                recency = 0.3
        else:
            recency = 0.3

        return freq * 0.6 + recency * 0.4


# ── CrossEncoderReranker (stage 2) ────────────────────────────────────


class CrossEncoderReranker:
    """Cross-encoder precision re-ranker (stage 2 of two-stage retrieval).

    Uses a jointly-encoded model (e.g. BGE-reranker-v2-m3) for significantly
    higher accuracy than bi-encoder cosine similarity.  Only runs on the
    top-K candidates from the bi-encoder stage, so latency impact is minimal.

    The model is **lazily loaded** on first use to avoid blocking startup.

    Parameters
    ----------
    model_name:
        HuggingFace model identifier.
    enabled:
        Master switch.  When *False*, :meth:`rerank` returns input unchanged.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        enabled: bool = False,
    ) -> None:
        self.model_name = model_name
        self.enabled = enabled
        self._model = None  # lazy-loaded

    def _load_model(self) -> None:
        """Load the cross-encoder model on first call."""
        if self._model is not None:
            return
        try:
            from sentence_transformers import CrossEncoder  # type: ignore

            self._model = CrossEncoder(self.model_name)
            logger.info("CrossEncoder loaded: %s", self.model_name)
        except ImportError:
            logger.warning(
                "CrossEncoder unavailable (pip install sentence-transformers); "
                "skipping precision re-ranking"
            )
            self.enabled = False
        except Exception as exc:
            logger.warning("Failed to load CrossEncoder %s: %s", self.model_name, exc)
            self.enabled = False

    # ── Public API ──────────────────────────────────────────────────

    def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        """Re-score *results* with the cross-encoder.

        Returns results sorted by cross-encoder score (descending).
        When the model is unavailable or disabled, returns input unchanged.
        """
        if not self.enabled or not results:
            return results

        self._load_model()
        if self._model is None:
            return results

        pairs = [(query, r.summary) for r in results]
        scores = self._model.predict(pairs)
        for r, score in zip(results, scores):
            r.relevance_score = float(score)

        results.sort(key=lambda x: x.relevance_score, reverse=True)
        return results
