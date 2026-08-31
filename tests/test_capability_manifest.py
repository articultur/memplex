"""Fail-closed contract for the auditable capability knowledge map."""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/capabilities.json"

TOP_LEVEL_FIELDS = {
    "schema_version",
    "manifest_id",
    "validation_scope",
    "supported_hosts",
    "claims",
    "limitations",
    "capabilities",
}
CLAIM_FIELDS = {"current_test_status", "industrial_readiness"}
CAPABILITY_FIELDS = {
    "id",
    "title",
    "summary",
    "evidence_level",
    "evidence_basis",
    "limitations",
    "mechanism_ref",
    "code_refs",
    "test_refs",
}
REQUIRED_CAPABILITY_IDS = {
    "module-boundaries",
    "typed-memory-model",
    "capture-write-path",
    "recall-retrieval-path",
    "temporal-facts",
    "bounded-graph-expansion",
    "principal-tenant-authorization",
    "sync-convergence",
    "backup-restore",
    "operations-observability",
    "reproducible-supply-chain",
    "four-host-lifecycle",
}
EVIDENCE_BASES = {
    "design_only",
    "repository_static",
    "historical_reference",
    "current_reproducible_evidence",
}
OVERCLAIM_PATTERNS = (
    re.compile(r"\b(?:industrial|production)(?:\s+\w+){0,3}\s+ready\b"),
    re.compile(r"\bready\b(?:\s+\w+){0,3}\s+(?:industrial|production)\b"),
    re.compile(
        r"\bcurrent(?:\s+\w+){0,3}\s+test(?:\s+\w+){0,3}\s+pass\b"
    ),
    re.compile(r"\btest(?:\s+\w+){0,3}\s+current(?:\s+\w+){0,3}\s+pass\b"),
    re.compile(r"\btest(?:\s+\w+){0,2}\s+pass(?:\s+\w+){0,4}\s+current\b"),
)
WORD_NORMALIZATIONS = {
    "currently": "current",
    "industrially": "industrial",
    "passed": "pass",
    "passes": "pass",
    "passing": "pass",
    "readiness": "ready",
    "tests": "test",
}
CAPABILITY_REQUIRED_CODE_PATHS = {
    "module-boundaries": frozenset({"memplex/service.py"}),
    "typed-memory-model": frozenset({"memplex/models/memory.py"}),
    "capture-write-path": frozenset({"memplex/service.py"}),
    "recall-retrieval-path": frozenset(
        {"memplex/service.py", "memplex/retrieval/multi_path.py"}
    ),
    "temporal-facts": frozenset(
        {"memplex/models/memory.py", "memplex/temporal.py", "memplex/service.py"}
    ),
    "bounded-graph-expansion": frozenset({"memplex/retrieval/multi_path.py"}),
    "principal-tenant-authorization": frozenset({"memplex/authorization.py"}),
    "sync-convergence": frozenset({"memplex/sync_repository.py", "memplex/sync.py"}),
    "backup-restore": frozenset({"memplex/backup.py"}),
    "operations-observability": frozenset({"memplex/operations.py"}),
    "reproducible-supply-chain": frozenset({"scripts/build_release_artifacts.py"}),
    "four-host-lifecycle": frozenset(
        {
            "memplex/host_lifecycle.py",
            "memplex/adapters/agent_installer.py",
            "memplex/adapters/runtime_status.py",
        }
    ),
}
CAPABILITY_REQUIRED_TEST_PATHS = {
    "module-boundaries": frozenset(
        {
            "tests/test_dependency_boundaries.py",
            "tests/test_authorization_gate.py",
            "tests/test_operations.py",
        }
    ),
    "typed-memory-model": frozenset({"tests/test_models.py"}),
    "capture-write-path": frozenset(
        {
            "tests/test_agent_runtime.py",
            "tests/test_service.py",
            "tests/test_storage.py",
            "tests/test_observation_pipeline.py",
        }
    ),
    "recall-retrieval-path": frozenset(
        {"tests/test_service.py", "tests/test_multi_path.py"}
    ),
    "temporal-facts": frozenset({"tests/test_temporal_facts.py"}),
    "bounded-graph-expansion": frozenset({"tests/test_multi_path.py"}),
    "principal-tenant-authorization": frozenset(
        {"tests/test_authorization_context.py", "tests/test_authorization_gate.py"}
    ),
    "sync-convergence": frozenset(
        {"tests/test_sync_repository_contract.py", "tests/test_sync_dispatcher.py"}
    ),
    "backup-restore": frozenset({"tests/test_backup.py"}),
    "operations-observability": frozenset(
        {"tests/test_operations.py", "tests/test_operations_evidence.py"}
    ),
    "reproducible-supply-chain": frozenset({"tests/test_reproducible_release.py"}),
    "four-host-lifecycle": frozenset(
        {
            "tests/test_agent_installer_registry.py",
            "tests/test_runtime_status.py",
            "tests/test_host_lifecycle_evidence.py",
            "tests/test_agent_host_matrix.py",
        }
    ),
}
LINE_REF = re.compile(
    r"^(?P<path>[^:#]+?)(?::(?P<colon_start>[1-9]\d*)-(?P<colon_end>[1-9]\d*)"
    r"|#L(?P<hash_start>[1-9]\d*)-L(?P<hash_end>[1-9]\d*))$"
)
MECHANISM_REF = re.compile(r"^(?P<path>[^#]+\.md)#(?P<anchor>[a-z0-9]+(?:-[a-z0-9]+)*)$")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssertionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_manifest() -> dict[str, Any]:
    payload = json.loads(
        MANIFEST.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
    )
    assert type(payload) is dict, "docs/capabilities.json must contain a JSON object"
    return payload


def _capabilities(payload: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities = payload.get("capabilities")
    assert type(capabilities) is list, "capabilities must be a JSON array"
    assert all(type(item) is dict for item in capabilities), (
        "every capability must be a JSON object"
    )
    return capabilities


def _safe_repository_path(value: str) -> Path:
    assert type(value) is str and value, "reference path must be a non-empty string"
    assert "\\" not in value, f"reference must use POSIX separators: {value}"
    relative = PurePosixPath(value)
    assert not relative.is_absolute(), f"reference must be relative: {value}"
    assert ".." not in relative.parts, f"reference must not escape the repository: {value}"
    path = ROOT.joinpath(*relative.parts).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError:
        raise AssertionError(f"reference must not escape the repository: {value}") from None
    assert path.is_file(), f"referenced file does not exist: {value}"
    return path


def _parse_line_ref(value: Any) -> tuple[str, int, int]:
    assert type(value) is str, "line reference must be a string"
    match = LINE_REF.fullmatch(value)
    assert match is not None, f"reference must include a line range: {value}"
    start = int(match.group("colon_start") or match.group("hash_start"))
    end = int(match.group("colon_end") or match.group("hash_end"))
    assert start <= end, f"reference line range is reversed: {value}"
    return match.group("path"), start, end


def _assert_valid_line_ref(value: Any) -> None:
    relative, start, end = _parse_line_ref(value)
    path = _safe_repository_path(relative)
    line_count = len(path.read_text(encoding="utf-8").splitlines())
    assert end <= line_count, f"reference exceeds {relative} line count {line_count}: {value}"


def _iter_string_values(value: Any, location: str = "$") -> list[tuple[str, str]]:
    strings: list[tuple[str, str]] = []
    if type(value) is str:
        strings.append((location, value))
    elif type(value) is dict:
        for key, child in value.items():
            strings.extend(_iter_string_values(child, f"{location}.{key}"))
    elif type(value) is list:
        for index, child in enumerate(value):
            strings.extend(_iter_string_values(child, f"{location}[{index}]"))
    return strings


def _assert_no_overclaim_phrases(payload: dict[str, Any]) -> None:
    for location, value in _iter_string_values(payload):
        words = re.sub(r"[\W_]+", " ", value.casefold()).split()
        normalized = " ".join(WORD_NORMALIZATIONS.get(word, word) for word in words)
        for pattern in OVERCLAIM_PATTERNS:
            match = pattern.search(normalized)
            if match is not None:
                preceding_words = normalized[: match.start()].split()[-8:]
                if any(word in {"no", "not", "without"} for word in preceding_words):
                    continue
            assert match is None, (
                f"overclaim phrase {pattern.pattern!r} at {location}: {value!r}"
            )


def _assert_capability_has_semantic_paths(capability: dict[str, Any]) -> None:
    capability_id = capability.get("id")
    required_code_paths = CAPABILITY_REQUIRED_CODE_PATHS.get(capability_id)
    required_test_paths = CAPABILITY_REQUIRED_TEST_PATHS.get(capability_id)
    assert required_code_paths is not None and required_test_paths is not None, (
        f"missing path contract for capability: {capability_id}"
    )
    code_paths = {
        _parse_line_ref(reference)[0] for reference in capability.get("code_refs", [])
    }
    test_paths = {
        _parse_line_ref(reference)[0] for reference in capability.get("test_refs", [])
    }
    assert code_paths == required_code_paths, (
        f"{capability_id} code_refs paths must exactly equal "
        f"{sorted(required_code_paths)}; got {sorted(code_paths)}"
    )
    assert test_paths == required_test_paths, (
        f"{capability_id} test_refs paths must exactly equal "
        f"{sorted(required_test_paths)}; got {sorted(test_paths)}"
    )


def _assert_static_capability_evidence(capability: dict[str, Any]) -> None:
    capability_id = capability.get("id")
    assert capability.get("evidence_level") == 2, (
        f"{capability_id} evidence_level must be exactly 2"
    )
    assert capability.get("evidence_basis") == "repository_static", (
        f"{capability_id} evidence_basis must be repository_static"
    )


def _assert_code_reference_path(relative: str) -> None:
    assert relative.startswith(("memplex/", "scripts/")), (
        f"code reference must be under memplex/ or scripts/: {relative}"
    )


def _assert_range_overlaps_test_function(path: Path, start: int, end: int) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    test_ranges = [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    assert any(start <= test_end and test_start <= end for test_start, test_end in test_ranges), (
        f"test reference must overlap an AST test_* function, not imports/setup: "
        f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{start}-{end}"
    )


def _capability_by_id(
    capabilities: list[dict[str, Any]], capability_id: str
) -> dict[str, Any]:
    matches = [item for item in capabilities if item.get("id") == capability_id]
    assert len(matches) == 1, f"expected exactly one capability {capability_id}"
    return matches[0]


def _code_ref_ranges(
    capability: dict[str, Any], relative: str
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for reference in capability.get("code_refs", []):
        ref_path, start, end = _parse_line_ref(reference)
        if ref_path == relative:
            ranges.append((start, end))
    return ranges


def _assert_refs_cover_range(
    capability: dict[str, Any], relative: str, required_start: int, required_end: int
) -> None:
    ranges = sorted(_code_ref_ranges(capability, relative))
    cursor = required_start
    for start, end in ranges:
        if end < cursor:
            continue
        if start > cursor:
            break
        cursor = max(cursor, end + 1)
        if cursor > required_end:
            return
    raise AssertionError(
        f"{capability.get('id')} code_refs must cover "
        f"{relative}:{required_start}-{required_end}"
    )


def _assert_refs_include_class_definitions(
    capability: dict[str, Any], relative: str, required_names: set[str]
) -> None:
    path = _safe_repository_path(relative)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = {
        node.name: node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name in required_names
    }
    assert set(definitions) == required_names, (
        f"could not find required class definitions in {relative}: "
        f"{sorted(required_names - set(definitions))}"
    )
    ranges = _code_ref_ranges(capability, relative)
    missing = sorted(
        name
        for name, line in definitions.items()
        if not any(start <= line <= end for start, end in ranges)
    )
    assert not missing, (
        f"{capability.get('id')} code_refs must include definitions for: "
        f"{', '.join(missing)}"
    )


def _github_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading is None:
            continue
        anchor = heading.group(1).strip().lower()
        anchor = re.sub(r"[^\w\- ]", "", anchor, flags=re.UNICODE)
        anchor = re.sub(r"[\s\-]+", "-", anchor).strip("-")
        if anchor:
            anchors.add(anchor)
    return anchors


def test_capability_manifest_exists() -> None:
    assert MANIFEST.is_file(), "docs/capabilities.json is required for the G002 knowledge map"


@unittest.skipUnless(MANIFEST.is_file(), "requires docs/capabilities.json")
class CapabilityManifestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = _load_manifest()

    def test_uses_schema_version_one(self) -> None:
        self.assertIs(type(self.payload.get("schema_version")), int)
        self.assertEqual(self.payload["schema_version"], 1)

    def test_identifies_the_memplex_capability_manifest(self) -> None:
        self.assertEqual(self.payload.get("manifest_id"), "memplex-capabilities")

    def test_limits_validation_to_static_repository_mapping(self) -> None:
        self.assertEqual(
            self.payload.get("validation_scope"),
            "static_repository_mapping",
        )

    def test_has_exact_top_level_fields(self) -> None:
        self.assertEqual(set(self.payload), TOP_LEVEL_FIELDS)

    def test_supports_exactly_the_four_documented_hosts(self) -> None:
        self.assertEqual(
            self.payload.get("supported_hosts"),
            ["claude-code", "codex", "openclaw", "hermes"],
        )

    def test_does_not_assert_current_test_or_industrial_readiness(self) -> None:
        claims = self.payload.get("claims")
        self.assertIs(type(claims), dict)
        self.assertEqual(set(claims), CLAIM_FIELDS)
        self.assertEqual(
            claims,
            {
                "current_test_status": "not_asserted",
                "industrial_readiness": "not_asserted",
            },
        )

    def test_recursively_rejects_overclaim_phrases(self) -> None:
        _assert_no_overclaim_phrases(self.payload)

    def test_records_nonempty_manifest_limitations(self) -> None:
        limitations = self.payload.get("limitations")
        self.assertIs(type(limitations), list)
        self.assertTrue(limitations)
        self.assertTrue(all(type(item) is str and item.strip() for item in limitations))

    def test_contains_exactly_the_required_capability_ids(self) -> None:
        ids = [item.get("id") for item in _capabilities(self.payload)]
        self.assertEqual(set(ids), REQUIRED_CAPABILITY_IDS)
        self.assertEqual(len(ids), len(REQUIRED_CAPABILITY_IDS))

    def test_capability_ids_are_unique_strings(self) -> None:
        ids = [item.get("id") for item in _capabilities(self.payload)]
        self.assertTrue(all(type(item) is str and item for item in ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_capability_has_exact_fields(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                self.assertEqual(set(capability), CAPABILITY_FIELDS)

    def test_each_capability_has_valid_scalar_field_types(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                for field in ("id", "title", "summary", "mechanism_ref"):
                    self.assertIs(type(capability.get(field)), str)
                    self.assertTrue(capability[field].strip())
                self.assertIs(type(capability.get("evidence_level")), int)

    def test_each_capability_has_exactly_static_level_two_evidence(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                _assert_static_capability_evidence(capability)

    def test_each_capability_references_semantically_relevant_paths(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                _assert_capability_has_semantic_paths(capability)

    def test_each_capability_records_nonempty_limitations(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                limitations = capability.get("limitations")
                self.assertIs(type(limitations), list)
                self.assertTrue(limitations)
                self.assertTrue(
                    all(type(item) is str and item.strip() for item in limitations)
                )

    def test_each_capability_has_safe_existing_code_references_with_valid_ranges(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                refs = capability.get("code_refs")
                self.assertIs(type(refs), list)
                self.assertTrue(refs)
                for reference in refs:
                    relative, _, _ = _parse_line_ref(reference)
                    _assert_code_reference_path(relative)
                    _assert_valid_line_ref(reference)

    def test_each_capability_has_safe_existing_test_references_with_valid_ranges(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                refs = capability.get("test_refs")
                self.assertIs(type(refs), list)
                self.assertTrue(refs)
                for reference in refs:
                    relative, start, end = _parse_line_ref(reference)
                    self.assertRegex(relative, r"^tests/test_[^/]+\.py$")
                    _assert_valid_line_ref(reference)
                    _assert_range_overlaps_test_function(
                        _safe_repository_path(relative), start, end
                    )

    def test_typed_memory_model_references_all_typed_class_definitions(self) -> None:
        capability = _capability_by_id(
            _capabilities(self.payload), "typed-memory-model"
        )
        _assert_refs_include_class_definitions(
            capability,
            "memplex/models/memory.py",
            {"MemoryNode", "Function", "Fact", "Preference", "Observation"},
        )

    def test_module_boundaries_references_service_composition(self) -> None:
        capability = _capability_by_id(
            _capabilities(self.payload), "module-boundaries"
        )
        _assert_refs_cover_range(capability, "memplex/service.py", 292, 369)

    def test_recall_references_complete_query_implementation(self) -> None:
        capability = _capability_by_id(
            _capabilities(self.payload), "recall-retrieval-path"
        )
        _assert_refs_cover_range(capability, "memplex/service.py", 859, 1067)

    def test_backup_references_writer_publication_after_line_841(self) -> None:
        capability = _capability_by_id(_capabilities(self.payload), "backup-restore")
        _assert_refs_cover_range(capability, "memplex/backup.py", 841, 919)

    def test_each_capability_points_to_its_existing_mechanism_anchor(self) -> None:
        for capability in _capabilities(self.payload):
            with self.subTest(capability=capability.get("id")):
                reference = capability.get("mechanism_ref")
                self.assertIs(type(reference), str)
                match = MECHANISM_REF.fullmatch(reference)
                self.assertIsNotNone(match, f"invalid mechanism reference: {reference}")
                assert match is not None
                relative = match.group("path")
                self.assertEqual(relative, "docs/capability-mechanisms.md")
                self.assertEqual(match.group("anchor"), capability["id"])
                path = _safe_repository_path(relative)
                self.assertIn(match.group("anchor"), _github_anchors(path))


class CapabilityManifestValidatorMutationTests(unittest.TestCase):
    def test_overclaim_equivalents_are_rejected_after_word_normalization(self) -> None:
        variants = (
            "all tests pass in current checkout",
            "tests currently pass",
            "industrially ready",
            "ready for industrial production",
            "production readiness",
        )

        for value in variants:
            with self.subTest(value=value), self.assertRaisesRegex(
                AssertionError, "overclaim phrase"
            ):
                _assert_no_overclaim_phrases({"summary": value})

    def test_nested_industrial_ready_claim_is_rejected(self) -> None:
        payload = {"capabilities": [{"limitations": ["Industrial-ready today"]}]}

        with self.assertRaisesRegex(AssertionError, "overclaim phrase"):
            _assert_no_overclaim_phrases(payload)

    def test_nested_production_ready_claim_is_rejected_case_insensitively(self) -> None:
        payload = {"capabilities": [{"summary": "PRODUCTION READY"}]}

        with self.assertRaisesRegex(AssertionError, "overclaim phrase"):
            _assert_no_overclaim_phrases(payload)

    def test_nested_current_tests_pass_claim_is_rejected(self) -> None:
        payload = {"claims": {"detail": ["current tests pass"]}}

        with self.assertRaisesRegex(AssertionError, "overclaim phrase"):
            _assert_no_overclaim_phrases(payload)

    def test_readiness_near_industrial_production_is_rejected_after_normalization(self) -> None:
        payload = {
            "summary": "READY, for Industrial Production in every deployment"
        }

        with self.assertRaisesRegex(AssertionError, "overclaim phrase"):
            _assert_no_overclaim_phrases(payload)

    def test_current_test_pass_inflections_are_rejected_after_normalization(self) -> None:
        variants = ("CURRENT_TESTS_PASSED", "Current-tests-passing", "current tests pass")

        for value in variants:
            with self.subTest(value=value), self.assertRaisesRegex(
                AssertionError, "overclaim phrase"
            ):
                _assert_no_overclaim_phrases({"summary": value})

    def test_supply_chain_code_path_swap_to_service_is_rejected(self) -> None:
        capability = {
            "id": "reproducible-supply-chain",
            "code_refs": ["memplex/service.py:1-2"],
            "test_refs": ["tests/test_reproducible_release.py:66-112"],
        }

        with self.assertRaisesRegex(AssertionError, "code_refs"):
            _assert_capability_has_semantic_paths(capability)

    def test_supply_chain_test_path_swap_to_service_test_is_rejected(self) -> None:
        capability = {
            "id": "reproducible-supply-chain",
            "code_refs": ["scripts/build_release_artifacts.py:200-296"],
            "test_refs": ["tests/test_service.py:83-102"],
        }

        with self.assertRaisesRegex(AssertionError, "test_refs"):
            _assert_capability_has_semantic_paths(capability)

    def test_swapping_all_refs_between_any_capability_pair_is_rejected(self) -> None:
        capabilities = _capabilities(_load_manifest())

        for left, right in combinations(capabilities, 2):
            for recipient, donor in ((left, right), (right, left)):
                mutated = {
                    **recipient,
                    "code_refs": donor["code_refs"],
                    "test_refs": donor["test_refs"],
                }
                with (
                    self.subTest(recipient=recipient["id"], donor=donor["id"]),
                    self.assertRaisesRegex(AssertionError, "(?:code|test)_refs"),
                ):
                    _assert_capability_has_semantic_paths(mutated)

    def test_non_level_two_evidence_is_rejected(self) -> None:
        capability = {
            "id": "example",
            "evidence_level": 3,
            "evidence_basis": "repository_static",
        }

        with self.assertRaisesRegex(AssertionError, "exactly 2"):
            _assert_static_capability_evidence(capability)

    def test_non_static_evidence_basis_is_rejected(self) -> None:
        capability = {
            "id": "example",
            "evidence_level": 2,
            "evidence_basis": "current_reproducible_evidence",
        }

        with self.assertRaisesRegex(AssertionError, "repository_static"):
            _assert_static_capability_evidence(capability)

    def test_code_reference_outside_allowed_roots_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssertionError, "under memplex/ or scripts/"):
            _assert_code_reference_path("tests/test_service.py")

    def test_import_only_test_range_is_rejected(self) -> None:
        source = "import unittest\n\ndef test_expected_behavior():\n    assert True\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test_example.py"
            path.write_text(source, encoding="utf-8")

            with self.assertRaisesRegex(AssertionError, "AST test_\\* function"):
                _assert_range_overlaps_test_function(path, 1, 1)

    def test_incomplete_required_range_is_rejected(self) -> None:
        capability = {
            "id": "recall-retrieval-path",
            "code_refs": ["memplex/service.py:859-900"],
        }

        with self.assertRaisesRegex(AssertionError, "859-1067"):
            _assert_refs_cover_range(capability, "memplex/service.py", 859, 1067)
