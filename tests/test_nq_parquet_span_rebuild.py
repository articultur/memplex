"""Contract tests for the NQ parquet-shape long-answer span rebuild."""

from __future__ import annotations

from benchmarks.nq_trivia import (
    NQDataset,
    NQTriviaDataset,
    _extract_nq_question_text,
    _extract_nq_short_answer,
    _rebuild_nq_long_answer,
)


def _nq_parquet_row(*, no_answer: bool = False, with_short: bool = True) -> dict:
    long_start = -1 if no_answer else 1
    long_end = -1 if no_answer else 7
    return {
        "id": "12345",
        "document": {
            "title": "JWT",
            "url": "https://example.com/jwt",
            "tokens": {
                "token": [
                    "<Html>", "JSON", "Web", "Tokens", "are", "signed", ".",
                    "</body>",
                ],
                "is_html": [True, False, False, False, False, False, False, True],
                "start_byte": [0, 6, 11, 15, 22, 26, 33, 34],
                "end_byte": [6, 11, 15, 22, 26, 33, 34, 42],
            },
        },
        "question": {"text": ["who", "signs", "JWTs", "?"], "tokens": {"token": ["who", "signs", "JWTs", "?"]}},
        "annotations": {
            "yes": [-1],
            "no_answer": [1 if no_answer else 0],
            "long_answer": {
                "candidate_index": [-1 if no_answer else 0],
                "start_token": [long_start],
                "end_token": [long_end],
                "yes_no_answer": [-1],
            },
            "short_answers": [
                (
                    {"start_token": [5, 6], "end_token": [6, 7], "is_html": [False, False]}
                    if with_short
                    else {"start_token": [], "end_token": [], "is_html": []}
                )
            ],
        },
        "long_answer_candidates": [{"start_token": 1, "end_token": 7, "top_level": True}],
    }


def test_question_text_extracted_from_parquet_struct() -> None:
    assert _extract_nq_question_text(_nq_parquet_row()) == "who signs JWTs ?"
    assert _extract_nq_question_text({"question": "plain string"}) == "plain string"


def test_long_answer_rebuilt_html_filtered() -> None:
    passage = _rebuild_nq_long_answer(_nq_parquet_row())
    assert passage == "JSON Web Tokens are signed ."


def test_long_answer_no_answer_annotation_yields_empty() -> None:
    assert _rebuild_nq_long_answer(_nq_parquet_row(no_answer=True)) == ""


def test_short_answer_span_rebuilt() -> None:
    assert _extract_nq_short_answer(_nq_parquet_row()) == "signed"


def test_parquet_row_parses_into_seeded_sample_with_evidence() -> None:
    dataset = NQTriviaDataset(dataset_name="natural_questions")
    sample = dataset._parse_sample(_nq_parquet_row())
    assert sample is not None
    assert sample.id == "nq_12345"
    assert sample.query == "who signs JWTs ?"
    # short answer wins as the match target...
    assert "signed" in sample.metadata["aliases"]
    # ...while the rebuilt long-answer passage seeds the store.
    assert "JSON Web Tokens are signed" in sample.metadata["context"]


def test_parquet_row_without_short_answer_uses_long_passage_as_target() -> None:
    row = _nq_parquet_row(with_short=False)
    sample = NQTriviaDataset(dataset_name="natural_questions")._parse_sample(row)
    assert sample is not None
    assert "json web tokens are signed" in sample.metadata["aliases"]
    assert "JSON Web Tokens are signed" in sample.metadata["context"]


def test_nq_dataset_class_uses_the_parser() -> None:
    assert NQDataset().dataset_name == "natural_questions"
