"""Vector store abstraction -- migrated from the legacy ``storage/`` package.

Provides a ``VectorStore`` Protocol plus two implementations:

* ``InMemoryVectorStore`` -- bag-of-words cosine similarity, zero
  external dependencies.
* ``ChromaVectorStore`` -- ChromaDB + sentence-transformers for production
  quality embeddings.

Usage::

    from memplex.storage.vector import create_vector_store

    vs = create_vector_store("auto")   # ChromaDB if available, else InMemory
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Re-export the list[float] alias for convenience
Vector = list[float]


@dataclass
class VectorSearchResult:
    """A single vector search hit."""

    id: str
    score: float
    text: str


# ── Protocol ────────────────────────────────────────────────────────


@runtime_checkable
class VectorStore(Protocol):
    """Minimal vector store interface."""

    def add(self, id: str, text: str, metadata: dict | None = None) -> None: ...

    def upsert(self, id: str, vector: Vector, text: str = "") -> None: ...

    def upsert_batch(self, items: dict[str, Vector]) -> None: ...

    def search(
        self, query: str, top_k: int = 5, query_vector: Vector | None = None
    ) -> list[VectorSearchResult]: ...

    def delete(self, id: str) -> None: ...

    def clear(self) -> None: ...


# ── InMemory implementation ─────────────────────────────────────────


class InMemoryVectorStore:
    """In-memory bag-of-words cosine similarity store.

    Zero external dependencies.  Suitable for Lite backend and testing.
    """

    def __init__(self) -> None:
        self._vectors: dict[str, tuple] = {}  # id -> (text, embedding)
        self._stored_vectors: dict[str, Vector] = {}  # id -> pre-stored vector
        self._all_words: set = set()

    def add(self, id: str, text: str, metadata: dict | None = None) -> None:
        words = self._get_words(text)
        self._all_words.update(words)
        embedding = self._encode_with_vocab(text, words)
        self._vectors[id] = (text, embedding)
        self._stored_vectors.pop(id, None)

    def upsert(self, id: str, vector: Vector, text: str = "") -> None:
        self._stored_vectors[id] = vector
        self._vectors[id] = (text, [0])  # placeholder text embedding

    def upsert_batch(self, items: dict[str, Vector]) -> None:
        for id, vector in items.items():
            self.upsert(id, vector)

    def search(
        self,
        query: str,
        top_k: int = 5,
        query_vector: Vector | None = None,
    ) -> list[VectorSearchResult]:
        if query_vector is not None:
            return self._search_by_vector(query_vector, top_k)
        query_emb = self._encode_with_vocab(query, self._get_words(query))
        return self._search_by_embedding(query_emb, top_k)

    def _search_by_vector(self, query_vec: Vector, top_k: int) -> list[VectorSearchResult]:
        scores: list = []
        for vid, vec in self._stored_vectors.items():
            score = self._cosine(query_vec, vec)
            text = self._vectors.get(vid, ("", None))[0]
            scores.append((vid, score, text))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [VectorSearchResult(id=s[0], score=s[1], text=s[2]) for s in scores[:top_k]]

    def _search_by_embedding(self, query_emb: list, top_k: int) -> list[VectorSearchResult]:
        scores: list = []
        for vid, (text, emb) in self._vectors.items():
            score = self._cosine(query_emb, emb)
            scores.append((vid, score, text))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [VectorSearchResult(id=s[0], score=s[1], text=s[2]) for s in scores[:top_k]]

    def delete(self, id: str) -> None:
        self._vectors.pop(id, None)
        self._stored_vectors.pop(id, None)

    def clear(self) -> None:
        self._vectors.clear()
        self._stored_vectors.clear()
        self._all_words.clear()

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _get_words(text: str) -> set:
        text_lower = text.lower()
        if any("一" <= c <= "鿿" for c in text):
            return set(text_lower)
        return set(text_lower.split())

    def _encode_with_vocab(self, text: str, words: set) -> list:
        if not self._all_words:
            return [0]
        return [1 if w in words else 0 for w in sorted(self._all_words)]

    @staticmethod
    def _cosine(a: list, b: list) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        return dot / (norm_a * norm_b + 1e-10)


# ── ChromaDB implementation ─────────────────────────────────────────

try:
    import chromadb  # type: ignore
    from chromadb.config import Settings  # type: ignore

    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False
    chromadb = None


# Known-unpatched chromadb advisories (Dependabot, checked 2026-09-14).
# Every released version inside these ranges is affected and upstream has
# shipped no fix yet -- the latest release (1.5.9) is the affected ceiling
# -- so construction fails closed unless the caller explicitly accepts the
# risk. Ranges are encoded literally so a future chromadb outside all of
# them clears the gate without a code change.
_CHROMA_ADVISORIES: tuple[tuple[str, tuple[int, int, int], tuple[int, int, int]], ...] = (
    ("GHSA-36p7-vc44-83pf", (0, 4, 17), (1, 5, 9)),  # critical: code injection
    ("GHSA-f4j7-r4q5-qw2c", (1, 0, 0), (1, 5, 9)),  # critical: pre-auth code injection
    ("GHSA-2wm9-hf6c-p5cr", (0, 4, 17), (1, 5, 9)),  # high: cross-tenant data access
    ("GHSA-xph7-9rjv-w5fr", (0, 5, 0), (1, 5, 9)),  # high: RBAC not scoped to tenant/db/collection
)


def _chroma_version_tuple() -> tuple[int, int, int] | None:
    """Installed chromadb version as a comparable tuple, None if unknowable."""
    if not _CHROMA_AVAILABLE or chromadb is None:
        return None
    raw = str(getattr(chromadb, "__version__", "") or "")
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        digits = re.match(r"\d+", piece.strip())
        parts.append(int(digits.group()) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def _chroma_advisory_hits() -> list[str]:
    """GHSA ids whose known-vulnerable range covers the installed chromadb.

    An importable but unversionable chromadb cannot be proven outside any
    range, so it fails closed (all advisories reported).
    """
    version = _chroma_version_tuple()
    if version is None:
        return [advisory for advisory, _, _ in _CHROMA_ADVISORIES]
    return [
        advisory
        for advisory, low, high in _CHROMA_ADVISORIES
        if low <= version <= high
    ]


def _chroma_risk_accepted(allow: bool) -> bool:
    return allow or os.environ.get("MEMPLEX_ALLOW_VULNERABLE_CHROMA") == "1"


class ChromaVectorStore:
    """ChromaDB-backed vector store with sentence-transformers embeddings."""

    def __init__(
        self,
        collection_name: str = "functions",
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        if not _CHROMA_AVAILABLE:
            raise ImportError("chromadb not installed: pip install chromadb sentence-transformers")
        self.client = chromadb.Client(Settings(anonymized_telemetry=False))
        self.collection = self.client.get_or_create_collection(name=collection_name)
        self._embedding_model = embedding_model
        self._model = None

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._embedding_model)
        return self._model

    def add(self, id: str, text: str, metadata: dict | None = None) -> None:
        embedding = self._get_model().encode([text])[0]
        self.collection.upsert(
            ids=[id],
            embeddings=[embedding.tolist()],
            documents=[text],
            metadatas=[metadata or {}],
        )

    def upsert(self, id: str, vector: Vector, text: str = "") -> None:
        self.collection.upsert(
            ids=[id],
            embeddings=[vector if isinstance(vector, list) else list(vector)],
            documents=[text],
            metadatas=[{}],
        )

    def upsert_batch(self, items: dict[str, Vector]) -> None:
        ids = list(items.keys())
        vectors = [v if isinstance(v, list) else list(v) for v in items.values()]
        self.collection.upsert(
            ids=ids,
            embeddings=vectors,
            documents=[""] * len(ids),
            metadatas=[{}] * len(ids),
        )

    def search(
        self,
        query: str,
        top_k: int = 5,
        query_vector: Vector | None = None,
    ) -> list[VectorSearchResult]:
        if query_vector is not None:
            q_emb = query_vector if isinstance(query_vector, list) else list(query_vector)
        else:
            q_emb = self._get_model().encode([query])[0].tolist()
        results = self.collection.query(query_embeddings=[q_emb], n_results=top_k)
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        documents = results.get("documents", [[]])[0]
        return [
            VectorSearchResult(id=ids[i], score=float(distances[i]), text=documents[i])
            for i in range(len(ids))
        ]

    def delete(self, id: str) -> None:
        self.collection.delete(ids=[id])

    def clear(self) -> None:
        self.collection.delete(where={})


# ── Factory ──────────────────────────────────────────────────────────


def create_vector_store(
    backend: str = "auto",
    *,
    allow_vulnerable_chroma: bool = False,
) -> VectorStore:
    """Create a vector store by backend name.

    Parameters
    ----------
    backend:
        ``"inmemory"`` | ``"chroma"`` | ``"auto"`` (default).
        ``"auto"`` prefers ChromaDB and falls back to InMemory.
        ``"chroma"`` without chromadb installed raises ``ImportError``.
    allow_vulnerable_chroma:
        Explicitly accept the known-unpatched chromadb advisories
        (``_CHROMA_ADVISORIES``; no fixed upstream release exists yet)
        and construct the ChromaDB backend anyway. Without this (or the
        ``MEMPLEX_ALLOW_VULNERABLE_CHROMA=1`` env var), ``"chroma"``
        raises ``RuntimeError`` and ``"auto"`` degrades to InMemory with
        an error log. Default deployments (lite/PostgreSQL paths) never
        touch chromadb and are unaffected.

    """
    if backend == "chroma":
        if not _CHROMA_AVAILABLE:
            raise ImportError(
                "chromadb is not installed; install it with: "
                "pip install chromadb sentence-transformers "
                "(or use backend='inmemory')."
            )
        hits = _chroma_advisory_hits()
        if hits and not _chroma_risk_accepted(allow_vulnerable_chroma):
            raise RuntimeError(
                "installed chromadb is inside known-unpatched advisory "
                f"ranges: {', '.join(hits)}. No fixed upstream release "
                "exists yet. Use backend='inmemory' (default deployments "
                "are unaffected), or pass allow_vulnerable_chroma=True / "
                "set MEMPLEX_ALLOW_VULNERABLE_CHROMA=1 to explicitly "
                "accept the risk."
            )
        return ChromaVectorStore()
    if backend == "inmemory":
        return InMemoryVectorStore()
    if backend == "auto":
        if _CHROMA_AVAILABLE:
            hits = _chroma_advisory_hits()
            if hits and not _chroma_risk_accepted(allow_vulnerable_chroma):
                logger.error(
                    "chromadb is installed but inside known-unpatched "
                    "advisory ranges (%s); degrading 'auto' to "
                    "InMemoryVectorStore. Set MEMPLEX_ALLOW_VULNERABLE_CHROMA=1 "
                    "to override.",
                    ", ".join(hits),
                )
                return InMemoryVectorStore()
            return ChromaVectorStore()
        return InMemoryVectorStore()
    raise ValueError(f"Unknown vector store backend: {backend!r}")
