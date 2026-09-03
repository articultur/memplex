"""Tests for the paraphrase-robustness benchmark: dataset integrity,
distractor loading, recall computation, and a small lite-backend smoke run."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import json

import pytest

from benchmarks.paraphrase_data import (
    DATASET_VERSION,
    FACTS,
    OVERLAP_LEVELS,
    QUERIES,
    fact_by_id,
    queries_by_overlap,
)
from benchmarks.paraphrase_eval import (
    compute_recall,
    load_distractors,
    run_queries,
    seed_documents,
)


@pytest.fixture
def service(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "store")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    yield svc
    svc.stop()


class TestDatasetIntegrity:
    def test_version_is_set(self):
        assert DATASET_VERSION

    def test_fact_count_in_required_range(self):
        assert 20 <= len(FACTS) <= 30

    def test_fact_ids_unique(self):
        ids = [f["id"] for f in FACTS]
        assert len(ids) == len(set(ids))

    def test_each_fact_has_3_to_4_queries(self):
        for fact in FACTS:
            n = sum(1 for q in QUERIES if q["fact_id"] == fact["id"])
            assert 3 <= n <= 4, f"{fact['id']} has {n} queries"

    def test_every_query_references_known_fact(self):
        for query in QUERIES:
            assert fact_by_id(query["fact_id"])["id"] == query["fact_id"]

    def test_overlap_levels_valid_and_all_present(self):
        assert {q["overlap"] for q in QUERIES} == set(OVERLAP_LEVELS)
        for level in OVERLAP_LEVELS:
            assert queries_by_overlap(level), level

    def test_each_fact_spans_high_to_low_overlap(self):
        """Every fact must exercise both ends of the lexical-overlap range."""
        for fact in FACTS:
            levels = {q["overlap"] for q in QUERIES if q["fact_id"] == fact["id"]}
            assert {"high", "low"} <= levels, fact["id"]

    def test_query_ids_unique_and_texts_nonempty(self):
        ids = [q["id"] for q in QUERIES]
        assert len(ids) == len(set(ids))
        assert all(q["text"].strip() for q in QUERIES)

    def test_queries_by_overlap_rejects_unknown_level(self):
        with pytest.raises(ValueError):
            queries_by_overlap("verbatim")


class TestDistractorLoading:
    def _write_popqa(self, tmp_path, entries):
        path = tmp_path / "popqa.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        return path

    def test_loads_up_to_limit(self, tmp_path):
        entries = [
            {"subject": f"Subject {i}", "question": f"What is thing {i}?", "object": "x"}
            for i in range(10)
        ]
        docs = load_distractors(self._write_popqa(tmp_path, entries), FACTS, limit=7)
        assert len(docs) == 7
        assert [d["id"] for d in docs] == [f"distractor_{i:04d}" for i in range(1, 8)]

    def test_skips_subject_colliding_with_fact_text(self, tmp_path):
        entries = [
            {"subject": "Eiffel Tower", "question": "How tall is it?", "object": "330 m"},
            {"subject": "Zebra", "question": "What genus is the zebra?", "object": "Equus"},
        ]
        docs = load_distractors(self._write_popqa(tmp_path, entries), FACTS, limit=5)
        assert [d["id"] for d in docs] == ["distractor_0001"]
        assert "zebra" in docs[0]["text"].lower()

    def test_skips_question_mentioning_fact_subject(self, tmp_path):
        entries = [
            {"subject": "France", "question": "What is the capital of France?", "object": "Paris"},
            {"subject": "Zebra", "question": "What genus is the zebra?", "object": "Equus"},
        ]
        docs = load_distractors(self._write_popqa(tmp_path, entries), FACTS, limit=5)
        assert len(docs) == 1
        assert "zebra" in docs[0]["text"].lower()


class TestComputeRecall:
    def test_overall_and_stratified(self):
        records = [
            {"fact_id": "a", "overlap": "high", "retrieved_ids": ["a", "b"]},
            {"fact_id": "b", "overlap": "high", "retrieved_ids": ["x", "b"]},
            {"fact_id": "c", "overlap": "low", "retrieved_ids": ["x", "y"]},
        ]
        metrics = compute_recall(records, ks=(1, 2))
        assert metrics["overall"] == {"recall@1": round(1 / 3, 4), "recall@2": round(2 / 3, 4)}
        assert metrics["by_overlap"]["high"] == {"n": 2, "recall@1": 0.5, "recall@2": 1.0}
        assert metrics["by_overlap"]["low"] == {"n": 1, "recall@1": 0.0, "recall@2": 0.0}

    def test_empty_records(self):
        metrics = compute_recall([], ks=(1,))
        assert metrics["overall"] == {"recall@1": 0.0}
        assert metrics["n"] == 0


class TestLiteEndToEnd:
    def test_verbatim_query_recalls_seeded_fact(self, service):
        """A near-verbatim query must surface its fact at rank 1 on lite."""
        docs = [
            {"id": "parafact_t1", "text": "The Eiffel Tower was completed in 1889."},
            {"id": "parafact_t2", "text": "The Amazon River flows through Brazil."},
            {"id": "parafact_t3", "text": "Gold has the chemical symbol Au."},
        ]
        assert seed_documents(service, docs) == 3
        queries = [
            {
                "id": "parafact_t1_q1",
                "fact_id": "parafact_t1",
                "text": "When was the Eiffel Tower completed?",
                "overlap": "high",
            }
        ]
        run = run_queries(service, queries, top_k=3)
        record = run["records"][0]
        assert record["retrieved_ids"][0] == "parafact_t1"
        metrics = compute_recall(run["records"], ks=(1,))
        assert metrics["overall"]["recall@1"] == 1.0
