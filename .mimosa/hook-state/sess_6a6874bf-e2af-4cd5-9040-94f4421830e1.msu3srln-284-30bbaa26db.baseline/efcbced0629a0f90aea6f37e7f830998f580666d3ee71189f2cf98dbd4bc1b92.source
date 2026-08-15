"""Test memplex/processing/merger/confidence_calculator.py.

Previously zero coverage (Wave 1 fix-list item 9). Covers per-source
base confidence, alias resolution, signal-based adjustments, vision
confidence, and the clamping bounds.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.models.paragraph import Paragraph, Sentence  # noqa: E402
from memplex.processing.merger.confidence_calculator import ConfidenceCalculator  # noqa: E402


def _para(sentences=None, section="1.2", raw_text="x" * 60):
    return Paragraph(
        id="p1",
        source="doc.md#1.2",
        section=section,
        raw_text=raw_text,
        sentences=sentences if sentences is not None else [Sentence(id="s1", text="t", role="action")],
    )


# ── Per-source base confidence ───────────────────────────────────────


@pytest.mark.parametrize(
    "hint,expected_base",
    [
        ("text", 0.95),
        ("markdown", 0.95),
        ("pdf", 0.90),
        ("docx", 0.90),
        ("image", 0.85),
        ("vision", 0.80),
        ("url", 0.90),
    ],
)
def test_base_confidence_by_source_type(hint, expected_base):
    calc = ConfidenceCalculator()
    # A paragraph with zero adjustments: no sentences (-0.05), section
    # present (+0.03), long text (+0.02), no roles -> net 0.00.
    para = _para(sentences=[], section="1.2", raw_text="x" * 60)
    assert calc.calculate_paragraph_confidence(para, source_hint=hint) == pytest.approx(
        expected_base
    )


def test_unknown_source_hint_defaults_to_0_9():
    calc = ConfidenceCalculator()
    para = _para(sentences=[], section="1.2", raw_text="x" * 60)
    assert calc.calculate_paragraph_confidence(para, source_hint="carrier-pigeon") == pytest.approx(
        0.9
    )


def test_clipboard_alias_maps_to_text_base():
    calc = ConfidenceCalculator()
    para = _para(sentences=[], section="1.2", raw_text="x" * 60)
    assert calc.calculate_paragraph_confidence(para, source_hint="clipboard") == pytest.approx(0.95)


# ── Signal adjustments ───────────────────────────────────────────────


def test_sentence_count_adjustments():
    calc = ConfidenceCalculator()
    base_para = _para(sentences=[], section="", raw_text="x" * 60)  # only +0.02 len adj
    base = calc.calculate_paragraph_confidence(base_para)

    two_to_ten = _para(
        sentences=[Sentence(id=f"s{i}", text="t", role="other") for i in range(3)],
        section="",
        raw_text="x" * 60,
    )
    many = _para(
        sentences=[Sentence(id=f"s{i}", text="t", role="other") for i in range(12)],
        section="",
        raw_text="x" * 60,
    )
    assert calc.calculate_paragraph_confidence(two_to_ten) > base
    assert calc.calculate_paragraph_confidence(many) > base
    # 2-10 sentences (+0.02) beats >10 sentences (+0.01)
    assert calc.calculate_paragraph_confidence(two_to_ten) > calc.calculate_paragraph_confidence(
        many
    )


def test_short_text_penalised_and_section_rewarded():
    calc = ConfidenceCalculator()
    short = _para(sentences=[], section="", raw_text="tiny")
    long_with_section = _para(sentences=[], section="3.1", raw_text="x" * 60)
    assert calc.calculate_paragraph_confidence(long_with_section) > calc.calculate_paragraph_confidence(
        short
    )


def test_role_coverage_adjustments():
    calc = ConfidenceCalculator()
    rich = _para(
        sentences=[
            Sentence(id="s1", text="a", role="trigger"),
            Sentence(id="s2", text="b", role="condition"),
            Sentence(id="s3", text="c", role="action"),
        ],
        section="",
        raw_text="x" * 60,
    )
    poor = _para(
        sentences=[Sentence(id="s1", text="a", role="trigger")],
        section="",
        raw_text="x" * 60,
    )
    assert calc.calculate_paragraph_confidence(rich) > calc.calculate_paragraph_confidence(poor)


def test_paragraph_confidence_clamped_to_range():
    calc = ConfidenceCalculator()
    worst = _para(sentences=[], section="", raw_text="")
    best = _para(
        sentences=[
            Sentence(id="s1", text="a", role="trigger"),
            Sentence(id="s2", text="b", role="condition"),
            Sentence(id="s3", text="c", role="action"),
        ],
        section="1.1",
        raw_text="x" * 60,
    )
    assert calc.calculate_paragraph_confidence(worst, source_hint="vision") >= 0.5
    assert calc.calculate_paragraph_confidence(best, source_hint="text") <= 0.99


# ── Vision confidence ────────────────────────────────────────────────


def test_vision_confidence_known_page_type_beats_unknown():
    calc = ConfidenceCalculator()
    known = calc.calculate_vision_confidence(page_type="Dashboard", component_count=5)
    unknown = calc.calculate_vision_confidence(page_type="Unknown", component_count=5)
    assert known > unknown


def test_vision_confidence_component_count_adjustments():
    calc = ConfidenceCalculator()
    none = calc.calculate_vision_confidence(page_type="Dashboard", component_count=0)
    some = calc.calculate_vision_confidence(page_type="Dashboard", component_count=5)
    many = calc.calculate_vision_confidence(page_type="Dashboard", component_count=25)
    assert some > none
    assert some > many


def test_vision_confidence_clamped():
    calc = ConfidenceCalculator()
    assert 0.5 <= calc.calculate_vision_confidence("Unknown", 0)
    assert calc.calculate_vision_confidence("Dashboard", 5) <= 0.95
