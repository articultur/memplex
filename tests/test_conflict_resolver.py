"""Test memplex/processing/merger/conflict_resolver.py.

Previously zero coverage (Wave 1 fix-list item 9). Covers conflict
detection on condition comparison, value preservation, human-review
marking, and resolution application.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

from memplex.models import FieldValue, Function
from memplex.processing.merger.conflict_resolver import Conflict, ConflictResolver


def _func(fid, name, conditions, authority=None, paragraphs=None):
    return Function(
        id=fid,
        name=name,
        name_normalized=name.strip().lower(),
        condition=[FieldValue(desc=c) for c in conditions],
        source_authority=authority,
        source_paragraphs=paragraphs or [],
    )


# ── detect_conflicts ─────────────────────────────────────────────────


def test_detects_conflict_when_conditions_differ():
    resolver = ConflictResolver()
    f1 = _func("a", "Deploy", ["linux only"], authority="docs", paragraphs=["p1"])
    f2 = _func("b", "deploy", ["windows only"], authority="blog", paragraphs=["p2"])

    conflicts = resolver.detect_conflicts([f1, f2])

    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.type == "field_value"
    assert c.field == "condition"
    assert c.severity == "medium"
    assert c.needs_human is True
    assert c.resolved is False
    contents = {v["content"] for v in c.values}
    assert contents == {"linux only", "windows only"}
    authorities = {v["authority"] for v in c.values}
    assert authorities == {"docs", "blog"}
    sources = {v["source"] for v in c.values}
    assert sources == {"p1", "p2"}


def test_no_conflict_when_conditions_identical():
    resolver = ConflictResolver()
    f1 = _func("a", "Deploy", ["linux only"])
    f2 = _func("b", "deploy", ["linux only"])
    assert resolver.detect_conflicts([f1, f2]) == []


def test_no_conflict_when_either_side_has_no_conditions():
    resolver = ConflictResolver()
    f1 = _func("a", "Deploy", ["linux only"])
    f2 = _func("b", "deploy", [])
    assert resolver.detect_conflicts([f1, f2]) == []


def test_no_conflict_across_different_names():
    resolver = ConflictResolver()
    f1 = _func("a", "Deploy", ["linux only"])
    f2 = _func("b", "Rollback", ["windows only"])
    assert resolver.detect_conflicts([f1, f2]) == []


def test_conflict_ids_are_unique_across_pairs():
    resolver = ConflictResolver()
    funcs = [
        _func("a", "Deploy", ["v1"]),
        _func("b", "deploy", ["v2"]),
        _func("c", "deploy", ["v3"]),
    ]
    conflicts = resolver.detect_conflicts(funcs)
    assert len(conflicts) == 3  # all three pairs conflict
    assert len({c.id for c in conflicts}) == 3


def test_missing_authority_and_source_fall_back_to_unknown():
    resolver = ConflictResolver()
    f1 = _func("a", "Deploy", ["v1"])
    f2 = _func("b", "deploy", ["v2"])
    (c,) = resolver.detect_conflicts([f1, f2])
    assert all(v["authority"] == "unknown" for v in c.values)
    assert all(v["source"] == "unknown" for v in c.values)


# ── value access / human review / resolution ─────────────────────────


def _conflict(resolved=False, final=None):
    return Conflict(
        id="conflict_001",
        type="field_value",
        severity="medium",
        field="condition",
        values=[
            {"source": "p1", "content": "linux only", "authority": "docs"},
            {"source": "p2", "content": "windows only", "authority": "blog"},
        ],
        resolved=resolved,
        final_value=final,
    )


def test_get_all_values():
    resolver = ConflictResolver()
    assert resolver.get_all_values(_conflict()) == ["linux only", "windows only"]
    empty = _conflict()
    empty.values = []
    assert resolver.get_all_values(empty) == []


def test_mark_for_human_review():
    resolver = ConflictResolver()
    c = _conflict(resolved=True, final="linux only")
    resolver.mark_for_human_review(c)
    assert c.needs_human is True
    assert c.resolved is False


def test_apply_resolution_valid_value():
    resolver = ConflictResolver()
    c = _conflict()
    resolver.apply_resolution(c, "linux only")
    assert c.resolved is True
    assert c.final_value == "linux only"
    assert c.needs_human is False


def test_apply_resolution_rejects_unknown_value():
    resolver = ConflictResolver()
    c = _conflict()
    with pytest.raises(ValueError):
        resolver.apply_resolution(c, "mac only")


def test_resolve_conflicts_partitions_and_marks_unresolved():
    resolver = ConflictResolver()
    done = _conflict(resolved=True, final="linux only")
    done.needs_human = False
    pending = _conflict()
    unresolved, resolved = resolver.resolve_conflicts([done, pending])
    assert resolved == [done]
    assert unresolved == [pending]
    assert pending.needs_human is True
