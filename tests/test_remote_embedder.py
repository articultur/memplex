"""RemoteEmbedder (embedding-as-a-service) contract tests, fully offline.

The remote backend must: honor OpenAI-compatible /embeddings semantics
(index-ordered responses), validate dimensions fail-closed, chunk batch
requests, send the bearer header, and never silently degrade.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

from memplex.retrieval.embedding import EmbeddingService, RemoteEmbedder

DIM = 4


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequests:
    """Records posts; returns scripted responses in shuffled order."""

    def __init__(self, dimension=DIM, status=200):
        self.calls: list[dict] = []
        self.dimension = dimension
        self.status = status

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        inputs = json["input"]
        # shuffle the data list to prove index-ordering matters
        data = [
            {"index": i, "embedding": [float(i)] * self.dimension}
            for i in range(len(inputs))
        ][::-1]
        return _FakeResponse({"data": data}, status=self.status)


def _service(fake, **kw):
    embedder = RemoteEmbedder(
        base_url="https://emb.example/v1",
        model="test-model",
        dimension=DIM,
        api_key="secret",
        **kw,
    )
    embedder._requests = fake
    return embedder


def test_encode_restores_index_order():
    fake = _FakeRequests()
    vec = _service(fake).encode("hello")
    assert vec == [0.0] * DIM
    assert fake.calls[0]["url"] == "https://emb.example/v1/embeddings"
    assert fake.calls[0]["json"] == {"model": "test-model", "input": ["hello"]}
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer secret"


def test_encode_query_batch_single_request_shuffled_order():
    fake = _FakeRequests()
    vectors = _service(fake).encode_query_batch(["a", "b", "c"])
    assert len(fake.calls) == 1, "query batch is one server-side request"
    assert vectors == [[0.0] * DIM, [1.0] * DIM, [2.0] * DIM]


def test_encode_batch_chunks_by_batch_size():
    fake = _FakeRequests()
    vectors = _service(fake).encode_batch(["t"] * 7, batch_size=3)
    assert [len(c["json"]["input"]) for c in fake.calls] == [3, 3, 1]
    assert len(vectors) == 7


def test_dimension_mismatch_fails_closed():
    fake = _FakeRequests(dimension=DIM + 1)
    with pytest.raises(ValueError, match="dimension"):
        _service(fake).encode("hello")


def test_http_error_propagates():
    fake = _FakeRequests(status=500)
    with pytest.raises(RuntimeError, match="500"):
        _service(fake).encode("hello")


def test_service_requires_remote_url_and_model():
    with pytest.raises(ValueError, match="remote_url"):
        EmbeddingService(model="remote", dimension=DIM)
    with pytest.raises(ValueError, match="remote_model"):
        EmbeddingService(model="remote", dimension=DIM, remote_url="https://x/v1")


def test_service_dispatches_to_remote(monkeypatch):
    fake = _FakeRequests()
    svc = EmbeddingService(
        model="remote",
        dimension=DIM,
        remote_url="https://emb.example/v1",
        remote_model="test-model",
        remote_api_key="k",
    )
    svc._embedder._requests = fake
    assert svc.embed("q") == [0.0] * DIM
    assert svc.embed_query("q") == [0.0] * DIM
