"""EmbeddingService -- vector embedding generation, storage and refresh.

Supports an offline default embedder, optional HuggingFace-backed models,
optional local ONNX models, configurable dimension, batch size, and Contextual
Retrieval (Anthropic's document-context prefix injection).

Embedding strategies::

    NAME_ONLY    -- function name only
    NAME_DOMAIN  -- name + domain
    FULL         -- name + trigger + action + benefit
    SEMANTIC     -- concise semantic summary for search

Usage::

    svc = EmbeddingService(model="default", storage=store, vector_store=vs)
    vector = svc.embed("some text")
    svc.refresh_all()
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Optional

from memplex.models import Function, RefreshResult

if TYPE_CHECKING:
    from memplex.storage.base import MemoryStore
    from memplex.storage.vector import VectorStore as VectorStoreProtocol

logger = logging.getLogger(__name__)

# Type alias for embedding vectors
Vector = list[float]


# ── Embedding strategies ────────────────────────────────────────────


class EmbeddingStrategy(Enum):
    """Controls how a Function is converted to embeddable text."""

    NAME_ONLY = "name"
    NAME_DOMAIN = "name_domain"
    FULL = "full"
    SEMANTIC = "semantic"


# ── Embedder backends ───────────────────────────────────────────────


class _SentenceTransformerEmbedder:
    """Wraps ``sentence_transformers.SentenceTransformer``."""

    def __init__(self, model_name: str, dimension: int) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        self.dimension = dimension

    def encode(self, text: str) -> Vector:
        return self._model.encode([text])[0].tolist()

    def encode_batch(self, texts: list[str], batch_size: int = 32) -> list[Vector]:
        embeddings = self._model.encode(texts, batch_size=batch_size)
        return [e.tolist() for e in embeddings]


class _SimpleTFIDFEmbedder:
    """Local embedder used for offline/default operation.

    Uses a TF-IDF-inspired bag-of-words representation.  The dimension
    is fixed to the number of unique words seen so far (padded / truncated
    to *dimension*).
    """

    def __init__(self, dimension: int = 384) -> None:
        self.dimension = dimension
        self._vocab: dict = {}  # word -> index
        self._idf: dict = {}  # word -> idf score
        self._doc_count: int = 0

    def encode(self, text: str) -> Vector:
        """Encode a document, updating vocab/idf corpus statistics."""
        return self._encode(text, update_stats=True)

    def encode_query(self, text: str) -> Vector:
        """Encode a query WITHOUT updating vocab/idf statistics.

        Query-time encoding must not mutate the corpus statistics, otherwise
        stored document vectors would drift with query history.
        """
        return self._encode(text, update_stats=False)

    def _encode(self, text: str, update_stats: bool) -> Vector:
        words = text.lower().split()
        if not words:
            return [0.0] * self.dimension

        if update_stats:
            # Update vocabulary (document path only)
            self._doc_count += 1
            unique_words = set(words)
            for w in unique_words:
                if w not in self._vocab:
                    self._vocab[w] = len(self._vocab)
                self._idf[w] = self._idf.get(w, 0) + 1

        # TF-IDF vector
        vec = [0.0] * self.dimension
        tf = {}
        for w in words:
            tf[w] = tf.get(w, 0) + 1

        import math

        for w, count in tf.items():
            idx = self._vocab.get(w, -1) % self.dimension
            idf = self._doc_count / (self._idf.get(w, 1) + 1)
            vec[idx] = (count / len(words)) * math.log(idf + 1)

        # L2 normalize
        norm = sum(x * x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def encode_batch(self, texts: list[str], batch_size: int = 32) -> list[Vector]:
        return [self.encode(t) for t in texts]


class _LocalONNXEmbedder:
    """Offline ONNX embedding backend for locally cached models."""

    def __init__(
        self,
        model_path: str,
        dimension: int,
        tokenizer_path: str | None = None,
        max_length: int = 256,
    ) -> None:
        import numpy as np
        import onnxruntime as ort  # type: ignore
        from tokenizers import Tokenizer  # type: ignore

        resolved_model = Path(model_path).expanduser()
        if not resolved_model.exists():
            raise FileNotFoundError(f"Local ONNX model not found: {resolved_model}")

        resolved_tokenizer = (
            Path(tokenizer_path).expanduser()
            if tokenizer_path
            else resolved_model.parent / "tokenizer.json"
        )
        if not resolved_tokenizer.exists():
            raise FileNotFoundError(f"Local ONNX tokenizer not found: {resolved_tokenizer}")

        self.model_path = str(resolved_model)
        self.dimension = dimension
        self.max_length = max(1, max_length)
        self._np = np
        self._tokenizer = Tokenizer.from_file(str(resolved_tokenizer))
        self._session = ort.InferenceSession(
            str(resolved_model),
            providers=["CPUExecutionProvider"],
        )
        self._input_names = [item.name for item in self._session.get_inputs()]

    def encode(self, text: str) -> Vector:
        inputs = self._encode_inputs(text)
        outputs = self._session.run(None, inputs)
        return self._normalize_output(outputs[0], inputs.get("attention_mask"))

    def encode_batch(self, texts: list[str], batch_size: int = 32) -> list[Vector]:
        return [self.encode(text) for text in texts]

    def _encode_inputs(self, text: str) -> dict:
        encoding = self._tokenizer.encode(text)
        ids = list(getattr(encoding, "ids", []) or [])[: self.max_length]
        if not ids:
            ids = [0]
        attention_mask = [1] * len(ids)

        inputs = {}
        if "input_ids" in self._input_names:
            inputs["input_ids"] = self._np.array([ids], dtype=self._np.int64)
        if "attention_mask" in self._input_names:
            inputs["attention_mask"] = self._np.array(
                [attention_mask],
                dtype=self._np.int64,
            )
        if "token_type_ids" in self._input_names:
            inputs["token_type_ids"] = self._np.zeros(
                (1, len(ids)),
                dtype=self._np.int64,
            )
        if not inputs:
            inputs[self._input_names[0]] = self._np.array([ids], dtype=self._np.int64)
        return inputs

    def _normalize_output(self, output, attention_mask=None) -> Vector:
        arr = self._np.asarray(output, dtype=float)
        if arr.ndim == 3:
            token_embeddings = arr[0]
            if attention_mask is not None:
                mask = self._np.asarray(attention_mask[0], dtype=float)[:, None]
                denom = max(float(mask.sum()), 1.0)
                vec = (token_embeddings * mask).sum(axis=0) / denom
            else:
                vec = token_embeddings.mean(axis=0)
        elif arr.ndim == 2:
            vec = arr[0]
        else:
            vec = arr.reshape(-1)

        values = [float(value) for value in vec.tolist()]
        if len(values) < self.dimension:
            values.extend([0.0] * (self.dimension - len(values)))
        elif len(values) > self.dimension:
            values = values[: self.dimension]

        norm = sum(value * value for value in values) ** 0.5
        if norm > 0:
            values = [value / norm for value in values]
        return values


# ── EmbeddingService ─────────────────────────────────────────────────


class EmbeddingService:
    """Vector embedding service.

    Parameters
    ----------
    model:
        Embedding model name. ``"default"`` uses local retrieval and local
        embeddings, and auto-enables local ONNX only when
        ``MEMPLEX_LOCAL_ONNX_MODEL`` points at an existing local model.
        ``"tfidf"``, ``"offline"``, and ``"lite"`` force local TF-IDF
        embeddings. Use ``"local-onnx"``, ``"local-onnx:<path>``, or
        ``"onnx:<path>"`` for a local ONNX model. Use ``"minilm"``,
        ``"bge-m3"``, ``"bge-small"``, a raw sentence-transformers model id, or
        ``"hf:<model-id>"`` to opt into HuggingFace-backed embeddings.
    dimension:
        Embedding vector dimension.
    storage:
        Optional :class:`MemoryStore` for ``refresh`` / ``refresh_all``.
    vector_store:
        Optional :class:`VectorStore` for upsert operations.
    batch_size:
        Default batch size for :meth:`embed_batch` (wired from
        ``config.embedding.batch_size`` by the service layer).
    contextual_retrieval:
        Default for :meth:`embed_function`'s *use_contextual* (wired
        from ``config.embedding.contextual_retrieval`` by the service
        layer).
    """

    _OFFLINE_MODELS: ClassVar[set[str]] = {"default", "tfidf", "offline", "lite", "local"}
    _HF_PREFIX = "hf:"
    _LOCAL_ONNX_MODELS: ClassVar[set[str]] = {"local-onnx", "onnx"}
    _LOCAL_ONNX_PREFIXES = ("local-onnx:", "onnx:")
    _MODEL_MAP: ClassVar[dict[str, str]] = {
        "minilm": "sentence-transformers/all-MiniLM-L6-v2",
        "bge-m3": "BAAI/bge-m3",
        "bge-small": "BAAI/bge-small-en-v1.5",
    }

    def __init__(
        self,
        model: str = "default",
        dimension: int = 384,
        storage: MemoryStore | None = None,
        vector_store: VectorStoreProtocol | None = None,
        batch_size: int = 32,
        contextual_retrieval: bool = True,
    ) -> None:
        self.model = model
        self.dimension = dimension
        self.storage = storage
        self.vector_store = vector_store
        self.batch_size = batch_size
        self.contextual_retrieval = contextual_retrieval
        self._embedder = self._create_embedder(model, dimension)

    # ── Public API ──────────────────────────────────────────────────

    def embed(self, text: str) -> Vector:
        """Generate an embedding vector for a single text."""
        return self._embedder.encode(text)

    def embed_query(self, text: str) -> Vector:
        """Embed a query-time text without mutating corpus statistics.

        Backends with internal statistics (TF-IDF) expose ``encode_query``
        so that document vectors do not drift with query history; other
        backends fall back to the regular ``encode``.
        """
        encode_query = getattr(self._embedder, "encode_query", None)
        if callable(encode_query):
            return encode_query(text)
        return self._embedder.encode(text)

    def embed_batch(self, texts: list[str], batch_size: int | None = None) -> list[Vector]:
        """Batch generate embedding vectors.

        *batch_size* defaults to the service-level default configured at
        construction (``self.batch_size``) when not given explicitly.
        """
        if batch_size is None:
            batch_size = self.batch_size
        return self._embedder.encode_batch(texts, batch_size=batch_size)

    def embed_function(
        self,
        func: Function,
        source: object | None = None,
        use_contextual: bool | None = None,
    ) -> Vector:
        """Generate an embedding for a Function.

        When *source* is available and *use_contextual* is True, a document
        context prefix is prepended (Contextual Retrieval). *use_contextual*
        defaults to the configured ``self.contextual_retrieval`` when not
        given explicitly.
        """
        if use_contextual is None:
            use_contextual = self.contextual_retrieval
        content = self.function_to_text(func)
        if use_contextual and source is not None:
            origin = (
                getattr(source, "url", None) or str(getattr(source, "source_path", "")) or "unknown"
            )
            content = f"[文档: {origin} | 领域: {func.domain or '未分类'}] {content}"
        return self.embed(content)

    def function_to_text(
        self,
        func: Function,
        strategy: EmbeddingStrategy = EmbeddingStrategy.FULL,
    ) -> str:
        """Convert a Function to embeddable text per *strategy*."""
        if strategy == EmbeddingStrategy.NAME_ONLY:
            return func.name

        if strategy == EmbeddingStrategy.NAME_DOMAIN:
            return f"{func.name} {func.domain or ''}"

        if strategy == EmbeddingStrategy.SEMANTIC:
            parts = [func.name, func.domain or ""]
            if func.trigger:
                parts.append(f"触发: {'; '.join(fv.desc for fv in func.trigger[:2])}")
            if func.action:
                parts.append(f"动作: {'; '.join(fv.desc for fv in func.action[:2])}")
            return " ".join(parts)

        # FULL
        parts = [func.name, func.domain or ""]
        for fv in func.trigger:
            parts.append(fv.desc)
        for fv in func.action:
            parts.append(fv.desc)
        for fv in func.benefit:
            parts.append(fv.desc)
        return " ".join(parts)

    def refresh(self, func_id: str) -> None:
        """Re-embed a single Function and upsert into the vector store."""
        if self.storage is None or self.vector_store is None:
            logger.warning("Cannot refresh: storage or vector_store not configured")
            return
        func = self.storage.get(func_id)
        if func is None:
            logger.warning("Function %s not found for refresh", func_id)
            return
        vector = self.embed_function(func)
        self.vector_store.upsert(func_id, vector)

    def refresh_all(self, batch_size: int = 100) -> RefreshResult:
        """Re-embed all Functions in batches and upsert into the vector store.

        Used after model or strategy changes.
        """
        if self.storage is None or self.vector_store is None:
            logger.warning("Cannot refresh_all: storage or vector_store not configured")
            return RefreshResult(total=0, refreshed=0)

        refreshed = 0
        offset = 0

        while True:
            batch = self.storage.list_functions(offset=offset, limit=batch_size)
            if not batch:
                break

            texts = [self.function_to_text(f) for f in batch]
            vectors = self.embed_batch(texts)
            self.vector_store.upsert_batch({f.id: v for f, v in zip(batch, vectors)})
            refreshed += len(batch)
            offset += batch_size

        return RefreshResult(total=refreshed, refreshed=refreshed)

    # ── Private ─────────────────────────────────────────────────────

    def _create_embedder(self, model: str, dimension: int):
        """Create the appropriate embedder backend."""
        model_key = (model or "default").strip()
        model_lookup_key = model_key.lower()

        onnx_model_path, onnx_explicit = self._resolve_local_onnx_model_path(
            model_key,
            model_lookup_key,
        )
        if onnx_model_path:
            try:
                return _LocalONNXEmbedder(
                    model_path=onnx_model_path,
                    tokenizer_path=os.getenv("MEMPLEX_LOCAL_ONNX_TOKENIZER"),
                    max_length=self._local_onnx_max_length(),
                    dimension=dimension,
                )
            except Exception as exc:
                if onnx_explicit:
                    raise RuntimeError(
                        f"Failed to load explicit local ONNX embedding model "
                        f"{onnx_model_path}: {exc}"
                    ) from exc
                logger.warning(
                    "Failed to load local ONNX embedding model %s: %s. "
                    "Falling back to TF-IDF embedder",
                    onnx_model_path,
                    exc,
                )
                return _SimpleTFIDFEmbedder(dimension=dimension)

        if onnx_explicit:
            raise ValueError(
                "Local ONNX embedding requested but no model path was provided. "
                "Set MEMPLEX_LOCAL_ONNX_MODEL or use "
                "MEMPLEX_EMBEDDING_MODEL=local-onnx:/path/to/model.onnx."
            )

        if model_lookup_key in self._OFFLINE_MODELS:
            logger.debug("Using local TF-IDF embedder for embedding model %s", model_key)
            return _SimpleTFIDFEmbedder(dimension=dimension)

        if model_lookup_key.startswith(self._HF_PREFIX):
            model_name = model_key[len(self._HF_PREFIX) :]
        else:
            model_name = self._MODEL_MAP.get(model_lookup_key, model_key)

        try:
            return _SentenceTransformerEmbedder(model_name, dimension)
        except ImportError:
            logger.info("sentence-transformers not available, falling back to TF-IDF embedder")
            return _SimpleTFIDFEmbedder(dimension=dimension)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.warning(
                "Failed to load sentence-transformers model %s: %s. "
                "Falling back to TF-IDF embedder",
                model_name,
                exc,
            )
            return _SimpleTFIDFEmbedder(dimension=dimension)

    @classmethod
    def _resolve_local_onnx_model_path(
        cls,
        model_key: str,
        model_lookup_key: str,
    ) -> tuple[str, bool]:
        """Resolve a local ONNX model path from model string or environment."""
        for prefix in cls._LOCAL_ONNX_PREFIXES:
            if model_lookup_key.startswith(prefix):
                return model_key.split(":", 1)[1].strip(), True

        if model_lookup_key in cls._LOCAL_ONNX_MODELS:
            return os.getenv("MEMPLEX_LOCAL_ONNX_MODEL", "").strip(), True

        if model_lookup_key == "default":
            return os.getenv("MEMPLEX_LOCAL_ONNX_MODEL", "").strip(), False

        return "", False

    @staticmethod
    def _local_onnx_max_length() -> int:
        """Return local ONNX tokenizer max length from environment."""
        try:
            return int(os.getenv("MEMPLEX_LOCAL_ONNX_MAX_LENGTH", "256"))
        except ValueError:
            return 256
