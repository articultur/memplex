"""Strict, test-only W09 fixture DTOs; expected values never come from a store."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LifecycleCase:
    case_id: str
    change: str
    surface: str
    identity: str
    allowed_ids: tuple[str, ...]
    forbidden_fragments: tuple[str, ...]
    expected_reason: str
    control_id: str


_FIELDS = frozenset(LifecycleCase.__dataclass_fields__)
_ANNOTATIONS = frozenset({"timeline", "principal", "scope", "failure_phase", "scenario",
                          "allowed_behavior", "forbidden_behavior", "execution", "owner_task"})
_REASONS = frozenset({"source_changed", "auth_changed", "deleted", "missing", "denied", "expired",
                     "invalid_upstream", "cycle", "budget_exhausted", "lookup_error", "malformed",
                     "unsupported_semantics", "unsupported_capability", "quarantined",
                     "publication_conflict", "not_current_publication", "captured_input_only", "valid"})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def load_case_manifest(path: Path) -> tuple[LifecycleCase, ...]:
    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if type(document) is not dict or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported case manifest schema")
    if document.get("backend_reason_overrides") != {"lite": {}, "postgres": {"subject": "missing"}}:
        raise ValueError("backend reasons must match the exact reviewed native concealment map")
    rows = document.get("cases")
    if type(rows) is not list or not rows:
        raise ValueError("case manifest must contain explicit cases")
    result = []
    seen = set()
    for row in rows:
        if type(row) is not dict or not _FIELDS <= row.keys() or row.keys() - _FIELDS - _ANNOTATIONS:
            raise ValueError("unknown or missing case field")
        for field in _FIELDS - {"allowed_ids", "forbidden_fragments"}:
            if type(row[field]) is not str or not row[field]:
                raise ValueError("case fields must be nonempty strings")
        for field in ("allowed_ids", "forbidden_fragments"):
            values = row[field]
            if type(values) is not list or not values or any(type(v) is not str or not v for v in values):
                raise ValueError("oracles must be explicit nonempty string arrays")
            if len(set(values)) != len(values):
                raise ValueError("duplicate oracle")
        if row["control_id"] != "U" or "U" not in row["allowed_ids"]:
            raise ValueError("independent U must have a positive allowed oracle")
        if len(row["forbidden_fragments"]) < 3 or row["expected_reason"] not in _REASONS:
            raise ValueError("body, citation, metadata and fixed reason are required")
        if row["case_id"].startswith("B-") and row["case_id"] != (
            f'B-{row["change"]}-{row["surface"]}-{row["identity"]}'):
            raise ValueError("base case dimensions contradict its frozen ID")
        if row["case_id"] in seen:
            raise ValueError("duplicate case ID")
        seen.add(row["case_id"])
        result.append(LifecycleCase(**{field: tuple(row[field]) if field in {
            "allowed_ids", "forbidden_fragments"} else row[field] for field in _FIELDS}))
    return tuple(result)
