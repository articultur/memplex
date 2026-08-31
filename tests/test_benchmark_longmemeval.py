"""Tests for the LongMemEval benchmark module (loader / scoring / runner).

Synthetic-corpus e2e runs through the real service. Score expectations
encode the honest failure mode: single-hop and knowledge-update questions
retrieve answer-bearing evidence (positive token-F1, substring diagnostic
hits), the aggregation multi-hop question does not. Token-F1 over
concatenated retrieval snippets is far below 1.0 by construction — the
prediction is evidence text, not a generated answer.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

from benchmarks.base import BenchmarkRunnerFactory
from benchmarks.loader import download_dataset
from benchmarks.longmemeval import (
    LongMemEvalDataset,
    LongMemEvalRunner,
    answer_hit,
)


def test_registered_in_factory():
    assert "longmemeval" in BenchmarkRunnerFactory.available_datasets()
    assert "longmemeval" in BenchmarkRunnerFactory.available_datasets()


@pytest.mark.parametrize(
    "predicted,gold,expected",
    [
        ("The user prefers Neovim now", ["Neovim"], True),
        ("completely unrelated text", ["Neovim"], False),
        ("2 + 3 = 5 total", ["5"], True),
        ("Answer: Paris, France", ["paris"], True),  # normalisation
        ("", ["anything"], False),
        ("something", [""], False),  # empty gold never hits
        # One-directional: a short prediction contained in a longer gold is
        # NOT a hit (the old bidirectional check overestimated).
        ("Neovim", ["The user prefers Neovim now"], False),
    ],
)
def test_answer_hit_normalisation(predicted, gold, expected):
    assert answer_hit(predicted, gold) is expected


def test_synthetic_generation_and_load(tmp_path):
    path = download_dataset("longmemeval", output_dir=str(tmp_path), force_synthetic=True)
    samples = LongMemEvalDataset().load(str(path))
    assert len(samples) == 3
    types = {s.metadata["question_type"] for s in samples}
    assert {"single-hop-user", "multi-hop", "knowledge-update"} <= types
    for sample in samples:
        assert sample.query
        assert sample.metadata["answers"]
        assert sample.metadata["session_history"]


def test_loader_rejects_bad_format(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a list"}')
    with pytest.raises(ValueError):
        LongMemEvalDataset().load(str(bad))


def test_loader_parses_official_schema(tmp_path):
    """Official releases use `answer` + haystack_sessions; auto-detected."""
    official = [
        {
            "question_id": "q001",
            "question_type": "single-session-user",
            "question": "Which instrument did the user start learning?",
            "answer": "The user started learning the guitar.",
            "question_date": "2023/4/10 14:00",
            "haystack_session_ids": ["s1", "s2"],
            "haystack_dates": ["2023/4/1 10:00", "2023/4/5 11:00"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "I just bought a guitar.", "has_answer": True},
                    {"role": "assistant", "content": "That is a great choice!"},
                ],
                [{"role": "user", "content": "I practised chords all evening."}],
            ],
            "answer_session_ids": ["s1"],
        },
        {
            "question_id": "q002_abs",
            "question_type": "multi-session",
            "question": "Which two hobbies does the user combine on Sundays?",
            "answer": "Cycling and photography.",
            "question_date": "2023/5/2 9:30",
            "haystack_session_ids": ["s3"],
            "haystack_dates": ["2023/4/30 8:00"],
            "haystack_sessions": [
                [{"role": "user", "content": "Sunday ride, then photos at the lake."}],
            ],
            "answer_session_ids": ["s3"],
        },
    ]
    path = tmp_path / "official.json"
    path.write_text(json.dumps(official), encoding="utf-8")

    dataset = LongMemEvalDataset()
    samples = dataset.load(str(path))

    # question_id gives stable sample ids; abstention entries load normally.
    assert [s.id for s in samples] == ["longmemeval-q001", "longmemeval-q002_abs"]
    first = samples[0]
    assert first.query == "Which instrument did the user start learning?"
    assert first.metadata["answers"] == ["The user started learning the guitar."]
    assert first.metadata["question_type"] == "single-session-user"
    assert first.metadata["question_date"] == "2023/4/10 14:00"
    assert first.metadata["evidence_session_ids"] == ["s1"]
    # The per-session haystack is flattened into one turn-level history.
    history = first.metadata["session_history"]
    assert [turn["role"] for turn in history] == ["user", "assistant", "user"]
    assert history[0]["content"] == "I just bought a guitar."
    assert len(dataset.to_memories(first)) == 3


def test_loader_autodetects_per_entry_and_skips_answerless(tmp_path):
    """Format detection is per entry; an empty official answer is malformed."""
    mixed = [
        {
            "question": "What is the user's current preferred editor?",
            "question_type": "knowledge-update",
            "answers": ["Neovim"],
            "question_date": "2025/5/2 15:30",
            "session_history": [{"role": "user", "content": "I now prefer Neovim."}],
        },
        {
            "question_id": "q003",
            "question_type": "single-session-user",
            "question": "Entry without a usable gold answer",
            "answer": "",
            "question_date": "2023/6/1 12:00",
            "haystack_sessions": [[{"role": "user", "content": "nothing here"}]],
            "answer_session_ids": [],
        },
    ]
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(mixed), encoding="utf-8")

    samples = LongMemEvalDataset().load(str(path))

    assert len(samples) == 1
    assert samples[0].metadata["answers"] == ["Neovim"]


def test_runner_end_to_end_scores(tmp_path):
    import tempfile

    os.environ["MEMPLEX_STORAGE_PATH"] = tempfile.mkdtemp()
    from memplex.service import MemplexService

    path = download_dataset("longmemeval", output_dir=str(tmp_path), force_synthetic=True)
    samples = LongMemEvalDataset().load(str(path))
    svc = MemplexService()
    svc.start()
    try:
        results = LongMemEvalRunner().run_retrieval(svc, samples, top_k=5)
    finally:
        svc.stop()

    by_metric = {(r.dataset, r.metric): r for r in results}
    overall_f1 = by_metric[("longmemeval", "token_f1")]
    overall_em = by_metric[("longmemeval", "exact_match")]
    overall_hit = by_metric[("longmemeval", "substring_hit_rate")]
    assert overall_f1.samples == 3
    # Primary metric: token-F1 over retrieved evidence. Answerable questions
    # score above zero; the aggregation multi-hop question does not.
    assert 0.0 < overall_f1.value < 1.0
    # EM compares full normalised strings; concatenated snippets never equal
    # a short gold answer — pinned at 0 for honesty.
    assert overall_em.value == 0.0
    # Auxiliary diagnostic keeps the old substring signal (one-directional).
    assert overall_hit.value == pytest.approx(2 / 3, abs=1e-3)
    assert by_metric[("longmemeval::single-hop-user", "token_f1")].value > 0.0
    assert by_metric[("longmemeval::knowledge-update", "token_f1")].value > 0.0
    assert by_metric[("longmemeval::multi-hop", "token_f1")].value == 0.0
    # Latency is a float-millisecond mean with p50/p99 percentiles attached.
    assert isinstance(overall_f1.latency_ms, float)
    assert overall_f1.latency_ms > 0
    assert overall_f1.latency_p50_ms is not None
    assert overall_f1.latency_p99_ms is not None
    assert overall_f1.latency_p50_ms <= overall_f1.latency_p99_ms
