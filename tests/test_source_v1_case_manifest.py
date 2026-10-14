"""Frozen W09 input/oracle inventory; fixture absence is the original RED."""
from __future__ import annotations

import json
from itertools import product
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "source_v1"
CHANGES = ("content", "delete", "auth", "expiry", "upstream", "incarnation")
SURFACES = ("query", "get", "context", "hot", "prefetch")
IDENTITIES = ("scope", "subject", "workspace", "tenant")


def test_manifest_exact_120_plus_40_and_no_empty_oracles():
    paths = tuple(FIXTURES / name for name in ("base_cases.json", "adversarial_cases.json"))
    assert all(path.is_file() for path in paths), "W09 frozen case fixtures are missing"
    from tests.source_v1_case_support import load_case_manifest

    base, adversarial = (load_case_manifest(path) for path in paths)
    expected = {f"B-{change}-{surface}-{identity}" for change, surface, identity in product(
        CHANGES, SURFACES, IDENTITIES)}
    assert len(base) == 120 and {case.case_id for case in base} == expected
    assert {(case.change, case.surface, case.identity) for case in base} == set(product(
        CHANGES, SURFACES, IDENTITIES))
    assert all(case.case_id == f"B-{case.change}-{case.surface}-{case.identity}" for case in base)
    assert len(adversarial) == 40 and {case.case_id for case in adversarial} == {
        f"A{number:02}" for number in range(1, 41)}
    cases = base + adversarial
    assert len({case.case_id for case in cases}) == 160
    for case in cases:
        assert case.control_id == "U" and "U" in case.allowed_ids
        assert len(case.forbidden_fragments) >= 3
        assert all(type(fragment) is str and fragment for fragment in case.forbidden_fragments)
        assert case.expected_reason and case.expected_reason != "any"
    for path in paths:
        document = json.loads(path.read_text())
        assert document.get("backend_reason_overrides") == {"lite": {}, "postgres": {"subject": "missing"}}
        assert document["backends"] == {
            "lite": "SUPPORTED", "postgres": "SUPPORTED", "sqlite": "DENY",
            "wrapper": "DENY", "sync": "DENY", "legacy_reader": "DENY"}
        for row in document["cases"]:
            assert "expected" not in row and "any" not in row.values()
            assert row["timeline"] and row["principal"] and row["scope"]
            assert row["failure_phase"] in {"validation", "publication", "scenario"}


@pytest.mark.parametrize("mutation", ["duplicate", "empty_control", "empty_fragments", "any", "unknown", "dimension", "backend_reason"])
def test_loader_rejects_ambiguous_or_empty_oracles(tmp_path, mutation):
    assert (FIXTURES / "base_cases.json").is_file(), "W09 frozen case fixtures are missing"
    from tests.source_v1_case_support import load_case_manifest

    document = json.loads((FIXTURES / "base_cases.json").read_text())
    row = document["cases"][0]
    if mutation == "duplicate":
        document["cases"].append(dict(row))
    elif mutation == "empty_control":
        row["allowed_ids"] = []
    elif mutation == "empty_fragments":
        row["forbidden_fragments"] = [""]
    elif mutation == "backend_reason":
        document.setdefault("backend_reason_overrides", {"lite": {}, "postgres": {}})["postgres"]["subject"] = "any"
    elif mutation == "dimension":
        row["change"] = "invented"
    elif mutation == "any":
        row["expected_reason"] = "any"
    else:
        row["expected"] = "anything"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_case_manifest(path)
