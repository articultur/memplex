"""Test memplex/processing/function_builder.py: L1 Paragraphs -> L2 Functions.

Previously only exercised transitively via CoreEngine.extract (test #1
finding: zero direct coverage). Covers field classification, ID hashing,
unstructured-text fallback, and the role-routing ladder (incl. the
statement/default branch whose dead if/else was just collapsed).
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from types import SimpleNamespace

from memplex.models import SourceDocument, SourceType
from memplex.models.paragraph import ParagraphCollection
from memplex.processing.function_builder import (
    build_functions_from_paragraphs,
    normalize_name,
)


def _sentence(text, role):
    return SimpleNamespace(text=text, role=role)


def _paragraph(raw_text, sentences=None, section=None, pid="p1"):
    return SimpleNamespace(
        id=pid,
        raw_text=raw_text,
        section=section,
        sentences=sentences or [],
    )


def _source():
    return SourceDocument(type="text", content="x", source_type=SourceType.WIKI)


# ── normalize_name ───────────────────────────────────────────────────


def test_normalize_name_lowercases_and_strips_punctuation():
    assert normalize_name("Hello, World!") == "hello world"
    assert normalize_name("  Multi   Space  ") == "multi space"


def test_normalize_name_keeps_cjk_and_alphanumerics():
    assert normalize_name("登录函数 login_01") == "登录函数 login_01"


# ── empty / skip behaviour ───────────────────────────────────────────


def test_empty_paragraphs_yields_empty_function_list():
    paras = ParagraphCollection(paragraphs=[])
    assert build_functions_from_paragraphs(paras, _source()) == []


def test_whitespace_only_paragraph_is_skipped():
    paras = ParagraphCollection(paragraphs=[_paragraph("   \n\t  ")])
    assert build_functions_from_paragraphs(paras, _source()) == []


# ── role routing ─────────────────────────────────────────────────────


def test_structured_sentences_route_to_correct_fields():
    para = _paragraph(
        "body",
        sentences=[
            _sentence("when X", "trigger"),
            _sentence("if Y", "condition"),
            _sentence("do Z", "action"),
            _sentence("so W", "result"),
        ],
    )
    funcs = build_functions_from_paragraphs(ParagraphCollection([para]), _source())
    assert len(funcs) == 1
    f = funcs[0]
    assert [fv.desc for fv in f.trigger] == ["when X"]
    assert [fv.desc for fv in f.condition] == ["if Y"]
    assert [fv.desc for fv in f.action] == ["do Z"]
    assert [fv.desc for fv in f.benefit] == ["so W"]


def test_statement_role_routes_to_action():
    """The collapsed default branch (was dead if/else) must still go to action."""
    para = _paragraph("body", sentences=[_sentence("just a statement", "statement")])
    f = build_functions_from_paragraphs(ParagraphCollection([para]), _source())[0]
    assert [fv.desc for fv in f.action] == ["just a statement"]


def test_unknown_role_routes_to_action():
    para = _paragraph("body", sentences=[_sentence("mystery", "totally-unknown")])
    f = build_functions_from_paragraphs(ParagraphCollection([para]), _source())[0]
    assert [fv.desc for fv in f.action] == ["mystery"]


# ── unstructured-text fallback ───────────────────────────────────────


def test_unstructured_text_splits_to_trigger_then_actions():
    """No structured sentences -> sentence-split, first=trigger, rest=actions."""
    para = _paragraph("First sentence. Second one! Third?")
    f = build_functions_from_paragraphs(ParagraphCollection([para]), _source())[0]
    assert len(f.trigger) == 1
    assert f.trigger[0].desc == "First sentence"
    assert [fv.desc for fv in f.action] == ["Second one", "Third"]


def test_unstructured_single_sentence_becomes_trigger():
    para = _paragraph("Only sentence here.")
    f = build_functions_from_paragraphs(ParagraphCollection([para]), _source())[0]
    assert len(f.trigger) == 1
    assert f.action == []


# ── ID and hash stability ────────────────────────────────────────────


def test_id_is_stable_for_same_content():
    para1 = _paragraph("identical body text")
    para2 = _paragraph("identical body text")
    f1 = build_functions_from_paragraphs(ParagraphCollection([para1]), _source())[0]
    f2 = build_functions_from_paragraphs(ParagraphCollection([para2]), _source())[0]
    assert f1.id == f2.id
    assert f1.id.startswith("func_")


def test_content_hash_populated():
    para = _paragraph("some body text")
    f = build_functions_from_paragraphs(ParagraphCollection([para]), _source())[0]
    assert f.content_hash
    assert isinstance(f.content_hash, str)


def test_section_used_as_name_when_present():
    para = _paragraph("body text", section="My Section Title")
    f = build_functions_from_paragraphs(ParagraphCollection([para]), _source())[0]
    assert f.name == "My Section Title"
