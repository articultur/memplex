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
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Re-export the list[float] alias for convenience
Vector = List[float]


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

    def add(self, id: str, text: str, metadata: Optional[dict] = None) -> None: ...

    def upsert(self, id: str, vector: Vector, text: str = "") -> None: ...

    def upsert_batch(self, items: Dict[str, Vector]) -> None: ...

    def search(
        self, query: str, top_k: int = 5, query_vector: Optional[Vector] = None
    ) -> List[VectorSearchResult]: ...

    def delete(self, id: str) -> None: ...

    def clear(self) -> None: ...


# ── InMemory implementation ─────────────────────────────────────────


class InMemoryVectorStore:
    """In-memory bag-of-words cosine similarity store.

    Zero external dependencies.  Suitable for Lite backend and testing.
    """

    def __init__(self) -> None:
        self._vectors: Dict[str, tuple] = {}  # id -> (text, embedding)
        self._stored_vectors: Dict[str, Vector] = {}  # id -> pre-stored vector
        self._all_words: set = set()

    def add(self, id: str, text: str, metadata: Optional[dict] = None) -> None:
        words = self._get_words(text)
        self._all_words.update(words)
        embedding = self._encode_with_vocab(text, words)
        self._vectors[id] = (text, embedding)
        self._stored_vectors.pop(id, None)

    def upsert(self, id: str, vector: Vector, text: str = "") -> None:
        self._stored_vectors[id] = vector
        self._vectors[id] = (text, [0])  # placeholder text embedding

    def upsert_batch(self, items: Dict[str, Vector]) -> None:
        for id, vector in items.items():
            self.upsert(id, vector)

    def search(
        self,
        query: str,
        top_k: int = 5,
        query_vector: Optional[Vector] = None,
    ) -> List[VectorSearchResult]:
        if query_vector is not None:
            return self._search_by_vector(query_vector, top_k)
        query_emb = self._encode_with_vocab(query, self._get_words(query))
        return self._search_by_embedding(query_emb, top_k)

    def _search_by_vector(self, query_vec: Vector, top_k: int) -> List[VectorSearchResult]:
        scores: list = []
        for vid, vec in self._stored_vectors.items():
            score = self._cosine(query_vec, vec)
            text = self._vectors.get(vid, ("", None))[0]
            scores.append((vid, score, text))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [VectorSearchResult(id=s[0], score=s[1], text=s[2]) for s in scores[:top_k]]

    def _search_by_embedding(self, query_emb: list, top_k: int) -> List[VectorSearchResult]:
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
            return set(list(text_lower))
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
    chromadb = None  # type: ignore[assignment]


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

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._model = SentenceTransformer(self._embedding_model)
        return self._model

    def add(self, id: str, text: str, metadata: Optional[dict] = None) -> None:
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

    def upsert_batch(self, items: Dict[str, Vector]) -> None:
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
        query_vector: Optional[Vector] = None,
    ) -> List[VectorSearchResult]:
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


def create_vector_store(backend: str = "auto") -> VectorStore:
    """Create a vector store by backend name.

    Parameters
    ----------
    backend:
        ``"inmemory"`` | ``"chroma"`` | ``"auto"`` (default).
        ``"auto"`` prefers ChromaDB and falls back to InMemory.
        ``"chroma"`` without chromadb installed raises ``ImportError``.
    """
    if backend == "chroma":
        if not _CHROMA_AVAILABLE:
            raise ImportError(
                "chromadb is not installed; install it with: "
                "pip install chromadb sentence-transformers "
                "(or use backend='inmemory')."
            )
        return ChromaVectorStore()
    if backend == "inmemory":
        return InMemoryVectorStore()
    if backend == "auto":
        if _CHROMA_AVAILABLE:
            return ChromaVectorStore()
        return InMemoryVectorStore()
    raise ValueError(f"Unknown vector store backend: {backend!r}")
