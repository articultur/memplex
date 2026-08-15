"""Embedding backend selection and fallback tests."""

import os
import sys
import types

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.retrieval import embedding
from memplex.retrieval.embedding import EmbeddingService


class _FakeSentenceTransformerEmbedder:
    seen_model_names: list[str] = []

    def __init__(self, model_name: str, dimension: int) -> None:
        self.seen_model_names.append(model_name)
        self.dimension = dimension

    def encode(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (self.dimension - 1)

    def encode_batch(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        return [self.encode(text) for text in texts]


class _BrokenSentenceTransformerEmbedder:
    def __init__(self, model_name: str, dimension: int) -> None:
        raise RuntimeError(f"{model_name} unavailable")


def _install_fake_onnx_runtime(monkeypatch):
    captured: dict[str, object] = {}

    class FakeInput:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeSession:
        def __init__(self, model_path: str, providers: list[str]) -> None:
            captured["model_path"] = model_path
            captured["providers"] = providers

        def get_inputs(self):
            return [FakeInput("input_ids"), FakeInput("attention_mask")]

        def run(self, output_names, inputs):
            import numpy as np

            captured["input_ids"] = inputs["input_ids"].tolist()
            return [np.array([[1.0, 2.0, 2.0]])]

    class FakeTokenizer:
        @classmethod
        def from_file(cls, path: str):
            captured["tokenizer_path"] = path
            return cls()

        def encode(self, text: str):
            captured["encoded_text"] = text
            return types.SimpleNamespace(ids=[101, 42, 102])

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.InferenceSession = FakeSession
    fake_tokenizers = types.ModuleType("tokenizers")
    fake_tokenizers.Tokenizer = FakeTokenizer
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tokenizers)
    return captured


def test_default_embedding_model_is_offline(monkeypatch):
    def fail_if_called(model_name: str, dimension: int):
        raise AssertionError(f"unexpected HuggingFace model load: {model_name}")

    monkeypatch.setattr(embedding, "_SentenceTransformerEmbedder", fail_if_called)

    service = EmbeddingService(model="default", dimension=8)
    vector = service.embed("offline memory works in mainland china")

    assert len(vector) == 8
    assert any(value != 0.0 for value in vector)


def test_offline_aliases_use_tfidf(monkeypatch):
    def fail_if_called(model_name: str, dimension: int):
        raise AssertionError(f"unexpected HuggingFace model load: {model_name}")

    monkeypatch.setattr(embedding, "_SentenceTransformerEmbedder", fail_if_called)

    for model in ["tfidf", "offline", "lite", "local"]:
        service = EmbeddingService(model=model, dimension=6)
        assert len(service.embed("local fallback")) == 6


def test_explicit_hf_models_opt_into_sentence_transformers(monkeypatch):
    _FakeSentenceTransformerEmbedder.seen_model_names = []
    monkeypatch.setattr(
        embedding,
        "_SentenceTransformerEmbedder",
        _FakeSentenceTransformerEmbedder,
    )

    minilm = EmbeddingService(model="minilm", dimension=4)
    custom = EmbeddingService(model="hf:BAAI/bge-m3", dimension=4)

    assert minilm.embed("semantic recall") == [1.0, 0.0, 0.0, 0.0]
    assert custom.embed("semantic recall") == [1.0, 0.0, 0.0, 0.0]
    assert _FakeSentenceTransformerEmbedder.seen_model_names == [
        "sentence-transformers/all-MiniLM-L6-v2",
        "BAAI/bge-m3",
    ]


def test_hf_model_load_failure_falls_back_to_tfidf(monkeypatch):
    monkeypatch.setattr(
        embedding,
        "_SentenceTransformerEmbedder",
        _BrokenSentenceTransformerEmbedder,
    )

    service = EmbeddingService(model="bge-m3", dimension=5)
    vector = service.embed("huggingface blocked fallback")

    assert len(vector) == 5
    assert any(value != 0.0 for value in vector)


def test_default_auto_enables_local_onnx_when_model_env_is_set(monkeypatch, tmp_path):
    def fail_if_called(model_name: str, dimension: int):
        raise AssertionError(f"unexpected HuggingFace model load: {model_name}")

    model_path = tmp_path / "model.onnx"
    tokenizer_path = tmp_path / "tokenizer.json"
    model_path.write_bytes(b"fake")
    tokenizer_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MEMPLEX_LOCAL_ONNX_MODEL", str(model_path))
    monkeypatch.setenv("MEMPLEX_LOCAL_ONNX_TOKENIZER", str(tokenizer_path))
    monkeypatch.setattr(embedding, "_SentenceTransformerEmbedder", fail_if_called)
    captured = _install_fake_onnx_runtime(monkeypatch)

    service = EmbeddingService(model="default", dimension=4)
    vector = service.embed("semantic local recall")

    assert len(vector) == 4
    assert vector[-1] == 0.0
    assert any(value != 0.0 for value in vector)
    assert captured["model_path"] == str(model_path)
    assert captured["tokenizer_path"] == str(tokenizer_path)
    assert captured["encoded_text"] == "semantic local recall"


def test_default_local_onnx_load_failure_falls_back_to_tfidf(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPLEX_LOCAL_ONNX_MODEL", str(tmp_path / "missing.onnx"))

    service = EmbeddingService(model="default", dimension=6)
    vector = service.embed("missing local onnx default fallback")

    assert len(vector) == 6
    assert any(value != 0.0 for value in vector)


def test_explicit_local_onnx_without_model_path_raises(monkeypatch):
    monkeypatch.delenv("MEMPLEX_LOCAL_ONNX_MODEL", raising=False)

    try:
        EmbeddingService(model="local-onnx", dimension=6)
    except ValueError as exc:
        assert "no model path was provided" in str(exc)
    else:
        raise AssertionError("explicit local-onnx requires a model path")


def test_explicit_local_onnx_missing_model_raises(tmp_path):
    missing_model = tmp_path / "missing.onnx"
    service = EmbeddingService(
        model="tfidf",
        dimension=6,
    )
    assert len(service.embed("sanity local fallback")) == 6

    try:
        EmbeddingService(model=f"local-onnx:{missing_model}", dimension=6)
    except RuntimeError as exc:
        assert "Failed to load explicit local ONNX embedding model" in str(exc)
    else:
        raise AssertionError("explicit local-onnx misconfiguration must surface")


# ── TF-IDF query pollution (regression) ────────────────────────────────


def test_tfidf_encode_query_does_not_update_corpus_statistics():
    """Regression: query-time encoding must not mutate vocab/idf/doc_count,
    otherwise stored document vectors drift with query history."""
    embedder = embedding._SimpleTFIDFEmbedder(dimension=8)

    embedder.encode("apple banana")  # document path: stats updated
    vocab_after_fit = dict(embedder._vocab)
    idf_after_fit = dict(embedder._idf)
    doc_count_after_fit = embedder._doc_count
    doc_vector_before = embedder.encode_query("apple banana")

    embedder.encode_query("apple cherry")  # query path: unseen word included

    assert embedder._vocab == vocab_after_fit
    assert embedder._idf == idf_after_fit
    assert embedder._doc_count == doc_count_after_fit
    # Transform-only document vectors are stable across query history.
    assert embedder.encode_query("apple banana") == doc_vector_before


def test_tfidf_encode_query_returns_vector_for_unseen_words():
    embedder = embedding._SimpleTFIDFEmbedder(dimension=8)
    vector = embedder.encode_query("totally unseen words")
    assert len(vector) == 8
    assert embedder.encode_query("") == [0.0] * 8


def test_embedding_service_embed_query_uses_non_mutating_path():
    service = EmbeddingService(model="tfidf", dimension=8)
    service.embed("apple banana")  # fit on a document
    stats_before = (
        dict(service._embedder._vocab),
        dict(service._embedder._idf),
        service._embedder._doc_count,
    )
    vector = service.embed_query("apple cherry")
    assert len(vector) == 8
    stats_after = (
        dict(service._embedder._vocab),
        dict(service._embedder._idf),
        service._embedder._doc_count,
    )
    assert stats_before == stats_after


def test_embedding_service_embed_query_falls_back_to_encode(monkeypatch):
    class _NoQueryEmbedder:
        def __init__(self):
            self.encoded = []

        def encode(self, text):
            self.encoded.append(text)
            return [1.0]

    service = EmbeddingService(model="tfidf", dimension=1)
    stub = _NoQueryEmbedder()
    service._embedder = stub
    assert service.embed_query("q") == [1.0]
    assert stub.encoded == ["q"]
