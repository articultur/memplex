"""Lazy in-memory embedding cache and cosine search for the lite backend.

The lite backend's semantic retrieval leg mirrors the postgres
tsvector + pgvector hybrid: documents are embedded once, cached by
content hash, and matched to the query embedding by cosine similarity.
Vectors live in memory only -- like the FTS5 sidecar this is an
acceleration cache over the authoritative JSON pair, rebuilt lazily
(and re-embedded) after restarts, never a source of truth. Swapping the
embedding model via a fresh ``set_embedder`` call drops the cache so
stale vectors from another model can never leak into rankings.

Cosine is computed in pure Python on purpose: the store module is pinned
in the mypy gate while numpy's stubs are excluded there (see the
``[tool.mypy]`` notes in pyproject), and corpora on the lite backend are
dev-scale by its documented boundary -- the postgres backend owns
scale-out vector search via pgvector.
"""

from __future__ import annotations

import logging
import math
from hashlib import sha1
from typing import Any

logger = logging.getLogger(__name__)

Vector = list[float]


def _cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two equal-length vectors, clipped to [0, 1].

    Negative similarities (dissimilar text under some embedding models)
    clip to 0 so they never contribute to leg fusion; zero vectors score
    0 rather than dividing by ~0.
    """
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    similarity = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    return max(0.0, similarity)


class VectorSearchIndex:
    """Embedding cache plus cosine search over ``(id, text)`` documents.

    The index holds no document corpus: callers pass the candidate list
    per search (the store's authoritative in-memory nodes), and the cache
    avoids re-embedding unchanged texts across queries. Cache entries are
    keyed by document id and validated by the text's SHA-1, so edits
    re-embed while unchanged documents reuse their vector.
    """

    def __init__(self) -> None:
        self._embedder: Any | None = None
        # func_id -> (text sha1, vector)
        self._cache: dict[str, tuple[str, Vector]] = {}

    @property
    def enabled(self) -> bool:
        """True when an embedder has been injected and is usable."""
        return self._embedder is not None

    def set_embedder(self, embedder: Any) -> None:
        """Inject (or replace) the embedder and drop any cached vectors.

        Replacing the embedder invalidates the cache unconditionally:
        vectors from a previous model or dimension must never mix with
        the new one.
        """
        self._embedder = embedder
        self._cache = {}

    def search(
        self,
        documents: list[tuple[str, str]],
        query_text: str,
        top_k: int,
    ) -> list[tuple[str, float]]:
        """Return ``(id, cosine)`` pairs for *query_text*, best first.

        Missing or changed documents are embedded on demand (batched),
        so the first query on a fresh corpus pays the backfill once and
        later queries are cache-only. Equal scores tie-break by id for
        deterministic output. Embedder failures degrade to an empty leg
        (the caller's lexical results stand) instead of failing search.
        """
        if self._embedder is None or top_k <= 0 or not documents:
            return []

        query_vector = self._embed_query(query_text)
        if not query_vector:
            return []

        vectors = self._vectors_for(documents)
        scored: list[tuple[str, float]] = []
        for doc_id, vector in vectors.items():
            if len(vector) != len(query_vector):
                continue
            scored.append((doc_id, _cosine(query_vector, vector)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:top_k]

    # ── Internals ────────────────────────────────────────────────────

    def _embed_query(self, text: str) -> Vector:
        """Embed query text without mutating corpus-derived statistics.

        Uses ``embed_query`` when the embedder provides it (the TF-IDF
        backend needs the transform-only path) and falls back to
        ``embed`` otherwise.
        """
        embedder = self._embedder
        assert embedder is not None  # guarded by ``enabled`` / search()
        embed_query = getattr(embedder, "embed_query", None)
        try:
            if callable(embed_query):
                return list(embed_query(text))
            return list(embedder.embed(text))
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("vector leg: query embedding failed: %s", exc)
            return []

    def _vectors_for(self, documents: list[tuple[str, str]]) -> dict[str, Vector]:
        """Resolve vectors for all documents, embedding misses on demand.

        Document embedding goes through the transform-only ``embed_query``
        path on backends that distinguish the two: query-time backfill
        must never mutate embedder corpus statistics (the TF-IDF backend
        pins this contract), and stateless semantic models treat both
        paths identically.
        """
        resolved: dict[str, Vector] = {}
        misses: list[tuple[str, str]] = []
        for doc_id, text in documents:
            digest = sha1(text.encode("utf-8")).hexdigest()
            cached = self._cache.get(doc_id)
            if cached is not None and cached[0] == digest:
                resolved[doc_id] = cached[1]
            else:
                misses.append((doc_id, text))

        for doc_id, text in misses:
            vector = self._embed_transform_only(text)
            if not vector:
                continue
            digest = sha1(text.encode("utf-8")).hexdigest()
            self._cache[doc_id] = (digest, vector)
            resolved[doc_id] = vector
        return resolved

    def _embed_transform_only(self, text: str) -> Vector:
        """Embed one text without mutating embedder corpus statistics."""
        embedder = self._embedder
        assert embedder is not None
        embed_query = getattr(embedder, "embed_query", None)
        try:
            if callable(embed_query):
                return list(embed_query(text))
            return list(embedder.embed(text))
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("vector leg: document embedding failed: %s", exc)
            return []
