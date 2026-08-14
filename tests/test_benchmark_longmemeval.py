"""Tests for the LongMemEval benchmark module (loader / scoring / runner).

Synthetic-corpus e2e runs through the real service; hit-rate expectations
encode the honest failure mode: single-hop and knowledge-update questions
are answerable by retrieval, the aggregation multi-hop question is not.
"""

from __future__ import annotations

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


def test_runner_end_to_end_hit_rates(tmp_path):
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

    by_dataset = {r.dataset: r for r in results}
    overall = by_dataset["longmemeval"]
    assert overall.samples == 3
    assert overall.value == pytest.approx(2 / 3, abs=1e-3)
    assert by_dataset["longmemeval::single-hop-user"].value == 1.0
    assert by_dataset["longmemeval::knowledge-update"].value == 1.0
    # Aggregation multi-hop is NOT retrieval-answerable: pinned as 0.
    assert by_dataset["longmemeval::multi-hop"].value == 0.0
