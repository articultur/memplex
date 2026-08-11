"""MemoryDeduplicator -- exact + semantic deduplication for Compaction.

Dedup strategies (by scale, automatic fallback)::

    1. FAISS IndexFlatIP  -- exact inner-product (cosine) search,
                             cross-domain, requires faiss-cpu
    2. NumPy matrix       -- O(n*d), grouped by domain, requires numpy
    3. Pure Python        -- O(n^2), zero dependencies, Lite fallback

Usage::

    dedup = MemoryDeduplicator(embedding_service)
    result = dedup.deduplicate(memories)
    print(result.exact_removed, result.semantic_removed)
"""

from __future__ import annotations

import copy
import hashlib
import logging
from enum import Enum
from typing import Dict, List

from memplex.models import DedupResult, Memory
from memplex.retrieval.embedding import EmbeddingService

logger = logging.getLogger(__name__)


# ── Dedup strategy ────────────────────────────────────────────────────


class DedupStrategy(Enum):
    """Deduplication strategy."""

    EXACT = "exact"  # exact content hash match
    SEMANTIC = "semantic"  # embedding cosine similarity
    BOTH = "both"  # exact first, then semantic


# ── MemoryDeduplicator ────────────────────────────────────────────────


class MemoryDeduplicator:
    """Memory deduplication cleaner.

    Used in the Compaction Pipeline's Dedup stage.

    Parameters
    ----------
    embedding_service:
        Used to generate embeddings for semantic dedup.
    strategy:
        Which dedup strategy to apply.
    threshold:
        Cosine similarity threshold for semantic dedup (default 0.95).
    chunk_threshold:
        Max memories per NumPy chunk before domain-based splitting.
    """

    def __init__(
        self,
        embedding_service: EmbeddingService,
        strategy: DedupStrategy = DedupStrategy.BOTH,
        threshold: float = 0.95,
        chunk_threshold: int = 20000,
        use_faiss: bool = True,
    ) -> None:
        self.embedder = embedding_service
        self.strategy = strategy
        self.threshold = threshold
        self.chunk_threshold = chunk_threshold
        self.use_faiss = use_faiss

        # Counters set at the start of each ``deduplicate`` call
        self._original_count: int = 0
        self._exact_removed: int = 0
        self._semantic_removed: int = 0

    # ── Public API ──────────────────────────────────────────────────

    def deduplicate(self, memories: List[Memory]) -> DedupResult:
        """Run dedup and return cleaned memories with statistics."""
        self._original_count = len(memories)
        self._exact_removed = 0
        self._semantic_removed = 0

        if self.strategy in (DedupStrategy.EXACT, DedupStrategy.BOTH):
            memories = self._exact_dedup(memories)

        if self.strategy in (DedupStrategy.SEMANTIC, DedupStrategy.BOTH):
            memories = self._semantic_dedup(memories)

        return DedupResult(
            original_count=self._original_count,
            final_count=len(memories),
            exact_removed=self._exact_removed,
            semantic_removed=self._semantic_removed,
            deduplicated=memories,
        )

    # ── Exact dedup ─────────────────────────────────────────────────

    def _exact_dedup(self, memories: List[Memory]) -> List[Memory]:
        """Remove exact duplicates by content hash.

        When two memories share the same hash, the *better* one is kept
        (see :meth:`_choose_better`).
        """
        seen: Dict[str, Memory] = {}
        for m in memories:
            key = self._content_hash(m)
            if key not in seen:
                seen[key] = m
            else:
                seen[key] = self._choose_better(seen[key], m)
                self._exact_removed += 1
        return list(seen.values())

    @staticmethod
    def _choose_better(a: Memory, b: Memory) -> Memory:
        """Pick the higher-quality memory to keep.

        Priority: newer *updated_at* > more populated fields
        (trigger/condition/action/benefit).
        """
        # updated_at comparison (they may be str or datetime)
        a_updated = str(getattr(a, "updated_at", "") or "")
        b_updated = str(getattr(b, "updated_at", "") or "")
        if a_updated > b_updated:
            return a
        if b_updated > a_updated:
            return b

        # field completeness
        a_fields = sum(
            1 for role in ("trigger", "condition", "action", "benefit") if getattr(a, role, [])
        )
        b_fields = sum(
            1 for role in ("trigger", "condition", "action", "benefit") if getattr(b, role, [])
        )
        if a_fields >= b_fields:
            return a
        return b

    # ── Semantic dedup (dispatcher) ─────────────────────────────────

    def _semantic_dedup(self, memories: List[Memory]) -> List[Memory]:
        """Semantic dedup with automatic backend selection.

        Routing:
        1. FAISS IndexFlatIP exact search (if *faiss* is importable)
           -- global, cross-domain.
        2. NumPy matrix (if *numpy* is importable, n <= chunk_threshold)
           -- grouped by domain when n > chunk_threshold.
        3. Pure-Python pairwise -- O(n^2), zero dependencies.
        """
        # 1. FAISS (best, unless disabled via ``use_faiss``)
        if self.use_faiss:
            try:
                import faiss  # noqa: F401 -- check availability

                return self._semantic_dedup_faiss(memories)
            except ImportError:
                pass

        # 2. NumPy (good for medium scale)
        if len(memories) > self.chunk_threshold:
            # Split by domain to keep memory bounded
            grouped: Dict[str, List[Memory]] = {}
            for m in memories:
                domain = getattr(m, "domain", "unknown") or "unknown"
                grouped.setdefault(domain, []).append(m)
            result: List[Memory] = []
            for group in grouped.values():
                result.extend(self._semantic_dedup_chunk(group))
            return result

        return self._semantic_dedup_chunk(memories)

    # ── FAISS ANN dedup ─────────────────────────────────────────────

    def _semantic_dedup_faiss(self, memories: List[Memory]) -> List[Memory]:
        """FAISS IndexFlatIP with Union-Find clustering.

        Steps:
        1. Batch embed + L2-normalise (inner product == cosine).
        2. Build IndexFlatIP, search top-K=5 neighbours.
        3. Union-Find groups memories above *threshold*.
        4. Merge each cluster into one representative memory.
        """
        import faiss  # type: ignore
        import numpy as np  # type: ignore

        if not memories:
            return []

        texts = [self._memory_to_text(m) for m in memories]
        raw_vecs = np.array(self.embedder.embed_batch(texts), dtype="float32")

        # Normalise to unit vectors so IP == cosine
        norms = np.linalg.norm(raw_vecs, axis=1, keepdims=True)
        vecs = raw_vecs / (norms + 1e-8)

        index = faiss.IndexFlatIP(vecs.shape[1])
        index.add(vecs)

        k = min(6, len(memories))  # top-1 is self; real neighbours = k-1
        distances, indices = index.search(vecs, k)

        # Union-Find
        parent = list(range(len(memories)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            parent[find(x)] = find(y)

        for i in range(len(memories)):
            for j_idx in range(1, k):
                j = int(indices[i][j_idx])
                if j < 0 or j == i:
                    continue
                if float(distances[i][j_idx]) > self.threshold:
                    union(i, j)

        # Collect clusters
        clusters: Dict[int, List[int]] = {}
        for i in range(len(memories)):
            root = find(i)
            clusters.setdefault(root, []).append(i)

        result: List[Memory] = []
        for idxs in clusters.values():
            cluster_mems = [memories[i] for i in idxs]
            merged = self._merge_memories(cluster_mems)
            self._semantic_removed += len(idxs) - 1
            result.append(merged)

        return result

    # ── NumPy matrix dedup ──────────────────────────────────────────

    def _semantic_dedup_chunk(self, memories: List[Memory]) -> List[Memory]:
        """Dedup one chunk using NumPy cosine similarity matrix."""
        if not memories:
            return []

        texts = [self._memory_to_text(m) for m in memories]
        embeddings = self.embedder.embed_batch(texts)

        # Try numpy; fall back to pure Python
        try:
            import numpy as np  # type: ignore
        except ImportError:
            return self._semantic_dedup_fallback(memories, embeddings)

        emb_matrix = np.array(embeddings, dtype="float64")
        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        emb_normalized = emb_matrix / (norms + 1e-8)
        sim_matrix = emb_normalized @ emb_normalized.T

        result: List[Memory] = []
        used: set = set()

        for i, m in enumerate(memories):
            mid = m.id
            if mid in used:
                continue
            similar = [m]
            used.add(mid)

            for j in range(i + 1, len(memories)):
                mjid = memories[j].id
                if mjid in used:
                    continue
                if sim_matrix[i][j] > self.threshold:
                    similar.append(memories[j])
                    used.add(mjid)
                    self._semantic_removed += 1

            merged = self._merge_memories(similar)
            result.append(merged)

        return result

    # ── Pure-Python fallback dedup ──────────────────────────────────

    def _semantic_dedup_fallback(
        self, memories: List[Memory], embeddings: List[list]
    ) -> List[Memory]:
        """O(n^2) pairwise cosine similarity, zero dependencies."""
        result: List[Memory] = []
        used: set = set()

        for i, m in enumerate(memories):
            mid = m.id
            if mid in used:
                continue
            similar = [m]
            used.add(mid)

            for j in range(i + 1, len(memories)):
                mjid = memories[j].id
                if mjid in used:
                    continue
                sim = self._cosine_sim(embeddings[i], embeddings[j])
                if sim > self.threshold:
                    similar.append(memories[j])
                    used.add(mjid)
                    self._semantic_removed += 1

            result.append(self._merge_memories(similar))
        return result

    # ── Merge helpers ───────────────────────────────────────────────

    @staticmethod
    def _merge_memories(memories: List[Memory]) -> Memory:
        """Merge a list of similar memories into one.

        Strategy:
        - Keep the newest *updated_at* as the base (deep-copy to avoid
          mutating live objects in MemoryStore).
        - Merge *source_paragraphs* and role field-values, deduplicating
          by ``desc`` string.
        """
        if len(memories) == 1:
            return memories[0]

        base = copy.deepcopy(max(memories, key=lambda m: str(getattr(m, "updated_at", "") or "")))

        for m in memories:
            if m.id == base.id:
                continue
            # Merge source_paragraphs
            for sp in getattr(m, "source_paragraphs", []):
                if sp not in base.source_paragraphs:
                    base.source_paragraphs.append(sp)
            # Merge role field-values
            for role in ("trigger", "condition", "action", "benefit"):
                existing_descs = {fv.desc for fv in getattr(base, role, [])}
                for fv in getattr(m, role, []):
                    if fv.desc not in existing_descs:
                        getattr(base, role).append(fv)
                        existing_descs.add(fv.desc)

        return base

    # ── Utility ─────────────────────────────────────────────────────

    @staticmethod
    def _cosine_sim(a: list, b: list) -> float:
        """Pure-Python cosine similarity (no numpy)."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        return dot / (norm_a * norm_b + 1e-8)

    def _content_hash(self, memory: Memory) -> str:
        """SHA-256 of the memory's text representation."""
        content = self._memory_to_text(memory)
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _memory_to_text(memory: Memory) -> str:
        """Flatten a memory into a single text for comparison."""
        parts = [memory.name]
        for role in ("trigger", "condition", "action", "benefit"):
            for fv in getattr(memory, role, []):
                parts.append(fv.desc)
        return " ".join(parts)
