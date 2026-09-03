"""Real-value coverage for the top-level Lite CLI workflow."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

from tests.g004_cli_runner import parse_json_stdout, process_diagnostic, run_cli

MEMPLEX = ".venv/bin/memplex"


def _assert_success(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode == 0, (
        "CLI command failed\n" + process_diagnostic(completed)
    )


def _assert_json_contains(
    completed: subprocess.CompletedProcess[str],
    *canaries: str,
) -> None:
    payload = parse_json_stdout(completed)
    json_text = json.dumps(payload, ensure_ascii=False)
    missing = [canary for canary in canaries if canary not in json_text]
    assert not missing, (
        f"CLI JSON missing canaries: {missing!r}\n"
        + process_diagnostic(completed)
    )


def _find_json_string_starting_with(
    completed: subprocess.CompletedProcess[str],
    prefix: str,
) -> str:
    pending = [parse_json_stdout(completed)]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and value.startswith(prefix):
            return value
    raise AssertionError(
        f"CLI JSON has no string starting with {prefix!r}\n"
        + process_diagnostic(completed)
    )


def test_write_is_recalled_by_a_later_process(tmp_path: Path) -> None:
    value = f"g004-lite-recall-{uuid4().hex}"
    env = {"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")}

    written = run_cli(
        [MEMPLEX, "--output", "json", "write", "--text", value],
        env=env,
    )
    _assert_success(written)

    recalled = run_cli(
        [MEMPLEX, "--output", "json", "recall", value],
        env=env,
    )

    _assert_success(recalled)
    _assert_json_contains(recalled, value)


def test_write_is_discovered_by_query_in_a_later_process(tmp_path: Path) -> None:
    value = f"g004-lite-query-{uuid4().hex}"
    env = {"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")}

    written = run_cli(
        [MEMPLEX, "--output", "json", "write", "--text", value],
        env=env,
    )
    _assert_success(written)

    discovered = run_cli(
        [MEMPLEX, "--output", "json", "query", value],
        env=env,
    )

    _assert_success(discovered)
    _assert_json_contains(discovered, value)


def test_scope_list_reports_existing_visibility_canary(tmp_path: Path) -> None:
    env = {"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")}

    completed = run_cli(
        [MEMPLEX, "--output", "json", "scope", "list"],
        env=env,
    )

    _assert_success(completed)
    _assert_json_contains(completed, "global")


def test_written_memory_can_be_shared_with_an_agent_in_a_later_process(
    tmp_path: Path,
) -> None:
    value = f"g004-lite-share-{uuid4().hex}"
    env = {"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")}

    written = run_cli(
        [MEMPLEX, "--output", "json", "write", "--text", value],
        env=env,
    )

    _assert_success(written)
    memory_id = _find_json_string_starting_with(written, "func_")

    shared = run_cli(
        [MEMPLEX, "--output", "json", "share", memory_id, "--agent", "codex"],
        env=env,
    )

    _assert_success(shared)
    _assert_json_contains(shared, memory_id, "codex")
