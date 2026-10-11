"""Transform-only TF-IDF must project only the fitted vocabulary."""

from concurrent.futures import ThreadPoolExecutor
from math import isfinite, log, sqrt
from threading import Event, Thread

import pytest

from memplex.auth import AuthorizationContext, Principal, bind_node_identity
from memplex.config import MemplexConfig
from memplex.models import Fact
from memplex.retrieval.embedding import EmbeddingService
from memplex.retrieval.reranker import cosine_similarity
from memplex.service import MemplexService
from memplex.storage.lite.vector_index import VectorSearchIndex, _batch_cosine, _cosine


def _statistics(service):
    backend = service._embedder
    return dict(backend._vocab), dict(backend._idf), backend._doc_count


@pytest.mark.parametrize("dimension", [1, 3, 8])
def test_disjoint_oov_transforms_have_zero_vector_and_cosine(dimension):
    service = EmbeddingService(model="tfidf", dimension=dimension)
    service.embed("anchor")
    before = _statistics(service)

    query = service.embed_query("quartz")
    document = service.embed_query_batch(["zircon"])[0]

    assert query == document == [0.0] * dimension
    assert _cosine(query, document) == 0.0
    assert _batch_cosine(query, {"document": document}) == [("document", 0.0)]
    assert cosine_similarity(query, document) == 0.0
    assert _statistics(service) == before


@pytest.mark.parametrize("text", ["alpha beta beta quartz", "quartz alpha beta beta", "alpha quartz beta beta quartz"])
def test_unknown_terms_cannot_overwrite_known_last_coordinate(text):
    service = EmbeddingService(model="tfidf", dimension=2)
    # Separate singleton documents make the vocabulary order deterministic.
    service.embed("alpha")
    service.embed("beta")
    before = _statistics(service)
    known = service.embed_query("alpha beta beta")

    assert known == pytest.approx([1 / sqrt(5), 2 / sqrt(5)])
    assert service.embed_query(text) == pytest.approx(known)
    assert service.embed_query_batch([text])[0] == pytest.approx(known)
    assert _statistics(service) == before


@pytest.mark.parametrize("documents", [[], ["", "   "]])
def test_empty_corpus_and_empty_text_keep_zero_vectors(documents):
    service = EmbeddingService(model="tfidf", dimension=3)
    assert service.embed_batch(documents) == [[0.0] * 3 for _ in documents]
    before = _statistics(service)

    assert service.embed_query("quartz") == [0.0] * 3
    assert service.embed_query_batch(["", "   ", "quartz"]) == [[0.0] * 3] * 3
    assert service.embed_query_batch([]) == []
    assert _statistics(service) == before == ({}, {}, 0)


def test_document_admission_and_known_modulo_overwrite_are_preserved():
    service = EmbeddingService(model="tfidf", dimension=2)
    assert service.embed("alpha") == [1.0, 0.0]
    assert service.embed("beta") == [0.0, 1.0]
    assert service.embed("gamma") == [1.0, 0.0]
    before = _statistics(service)

    # The separate known-token collision policy remains overwrite, not sum.
    assert service.embed_query("alpha alpha gamma beta") == pytest.approx([1 / sqrt(2)] * 2)
    assert service.embed_query("gamma alpha alpha beta") == pytest.approx([2 / sqrt(5), 1 / sqrt(5)])
    assert _statistics(service) == before

    vector = service.embed("alpha gamma beta")
    gamma_weight, beta_weight = log(4 / 3 + 1), log(4 / 3 + 1)
    norm = sqrt(gamma_weight**2 + beta_weight**2)
    assert vector == pytest.approx([gamma_weight / norm, beta_weight / norm])
    assert _statistics(service) == ({"alpha": 0, "beta": 1, "gamma": 2}, {"alpha": 2, "beta": 2, "gamma": 2}, 4)


def test_vocabulary_growth_recomputes_queries_and_existing_cache_reset_refreshes_docs():
    service = EmbeddingService(model="tfidf", dimension=3)
    service.embed("anchor")
    index = VectorSearchIndex()
    index.set_embedder(service)
    documents = [("document", "quartz")]
    before = _statistics(service)

    assert index.search(documents, "quartz", top_k=1) == [("document", 0.0)]
    cached = index.cached_vector("document", "quartz")
    assert cached == [0.0] * 3
    assert _statistics(service) == before

    service.embed("quartz")
    grown = _statistics(service)
    assert service.embed_query("quartz") == [0.0, 1.0, 0.0]
    assert service.embed_query_batch(["quartz"])[0] == [0.0, 1.0, 0.0]
    # Existing cache contracts deliberately require an explicit reset.
    assert index.search(documents, "quartz", top_k=1) == [("document", 0.0)]
    assert index.cached_vector("document", "quartz") is cached

    index.set_embedder(service)
    assert index.cached_vector("document", "quartz") is None
    assert index.search(documents, "quartz", top_k=1) == [("document", 1.0)]
    assert _statistics(service) == grown


@pytest.mark.parametrize("batch", [False, True])
def test_query_api_reenters_local_model_lock_and_waits_for_other_thread(batch):
    service = EmbeddingService(model="tfidf", dimension=3)
    service.embed("anchor")
    before = _statistics(service)
    rejected_attempt, finished = Event(), Event()
    results, errors = [], []
    model_lock = service._model_lock

    class ObservedLock:
        def __enter__(self):
            if not model_lock.acquire(blocking=False):
                # This event records actual contention on the existing RLock.
                rejected_attempt.set()
                model_lock.acquire()
            return self

        def __exit__(self, *exc_info):
            model_lock.release()

    service._model_lock = ObservedLock()

    def query():
        return service.embed_query_batch(["quartz"])[0] if batch else service.embed_query("quartz")

    def worker():
        try:
            results.append(query())
        except Exception as exc:  # noqa: BLE001 - thread errors are asserted below
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=worker, daemon=True)
    try:
        with model_lock:
            # Batch/single callers on the owning thread must reenter the RLock.
            assert query() == [0.0] * 3
            thread.start()
            assert rejected_attempt.wait(timeout=2), "query bypassed the held model lock"
            assert not finished.is_set()
    finally:
        # The context releases the real lock on every assertion path.
        if thread.ident is not None:
            thread.join(timeout=2)

    assert not thread.is_alive(), "query did not resume after model lock release"
    assert not errors
    assert results == [[0.0] * 3]
    assert _statistics(service) == before


def test_concurrent_query_and_document_calls_do_not_admit_unknown_queries():
    service = EmbeddingService(model="tfidf", dimension=3)
    service.embed("anchor")

    def worker(index):
        service.embed(f"document-{index}")
        return service.embed_query_batch(["quartz", "zircon"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        vectors = list(pool.map(worker, range(12)))

    assert vectors == [[[0.0] * 3] * 2] * 12
    vocab, frequencies, count = _statistics(service)
    assert set(vocab) == {"anchor", *(f"document-{i}" for i in range(12))}
    assert set(frequencies.values()) == {1}
    assert count == 13


def test_public_query_keeps_lexical_positive_without_false_oov_semantic_hits(tmp_path):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    config.embedding.model = "tfidf"
    config.embedding.dimension = 3
    config.embedding.hyde_enabled = False
    service = MemplexService(config=config)
    service._embedding_service.embed("anchor")
    service.store.add_fact(Fact(id="lexical", name="orchard", subject="orchard", predicate="is", object_="verdant"))
    service.store.add_fact(Fact(id="unrelated", name="cobalt", subject="cobalt", predicate="is", object_="metallic"))
    before = _statistics(service._embedding_service)

    first = service.query("orchard", top_k=2, max_tokens=0)
    second = service.query("orchard", top_k=2, max_tokens=0)

    assert [hit.func_id for hit in first.results] == ["lexical"]
    assert [hit.func_id for hit in second.results] == ["lexical"]
    assert all(isfinite(hit.relevance_score) and hit.relevance_score > 0.0 for hit in first.results)
    assert service._embedding_service.embed_query("orchard") == [0.0] * 3
    assert _statistics(service._embedding_service) == before


def test_public_zero_vector_query_keeps_authorized_positive_and_blocks_other_tenant(tmp_path):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    config.embedding.model = "tfidf"
    config.embedding.dimension = 3
    config.embedding.hyde_enabled = False
    service = MemplexService(config=config)
    service._embedding_service.embed("anchor")
    alice = AuthorizationContext(principal=Principal(tenant_id="tenant-a", subject_id="alice"), workspace_id="workspace")
    bob = AuthorizationContext(principal=Principal(tenant_id="tenant-b", subject_id="bob"), workspace_id="workspace")
    for node_id, context in [("alice-orchard", alice), ("bob-orchard", bob)]:
        fact = Fact(id=node_id, name="orchard", subject="orchard", predicate="is", object_="verdant")
        bind_node_identity(fact, context)
        service.store.add_fact(fact)

    result = service.query("orchard", top_k=2, max_tokens=0, authorization=alice)

    assert [hit.func_id for hit in result.results] == ["alice-orchard"]
    assert service._embedding_service.embed_query("orchard") == [0.0] * 3
