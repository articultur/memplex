"""Real-value coverage for the agent capture and recall CLI workflow."""

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


def test_agent_capture_is_recalled_by_an_independent_later_process(
    tmp_path: Path,
) -> None:
    lookup_phrase = "g004 independent process durable lookup"
    canary = f"g004-agent-recall-{uuid4().hex}"
    identity = [
        "--agent",
        "codex",
        "--user-id",
        "g004-agent-user",
        "--session-id",
        "g004-agent-session",
        "--project-path",
        str(tmp_path / "project"),
    ]
    env = {
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(tmp_path / "store"),
    }

    captured = run_cli(
        [
            MEMPLEX,
            "--output",
            "json",
            "agent",
            "capture",
            *identity,
            "--user-message",
            f"For {lookup_phrase}, remember the unique value {canary}.",
            "--assistant-message",
            "The canary was captured.",
        ],
        env=env,
    )
    _assert_success(captured)

    recalled = run_cli(
        [
            MEMPLEX,
            "--output",
            "json",
            "agent",
            "recall",
            *identity,
            lookup_phrase,
        ],
        env=env,
    )
    _assert_success(recalled)

    assert canary not in recalled.args, (
        "Recall argv unexpectedly contains the canary, so query echo could "
        "satisfy the durability assertion\n" + process_diagnostic(recalled)
    )
    payload = parse_json_stdout(recalled)
    serialized_payload = json.dumps(payload, ensure_ascii=False)
    assert canary in serialized_payload, (
        f"CLI recall JSON missing canary {canary!r}\n"
        + process_diagnostic(recalled)
    )


def test_agent_capture_requires_assistant_message(tmp_path: Path) -> None:
    completed = run_cli(
        [
            MEMPLEX,
            "--output",
            "json",
            "agent",
            "capture",
            "--agent",
            "codex",
            "--user-id",
            "g004-validation-user",
            "--session-id",
            "g004-validation-session",
            "--project-path",
            str(tmp_path / "project"),
            "--user-message",
            "This command deliberately omits the required response.",
        ],
        env={"MEMPLEX_STORAGE_PATH": str(tmp_path / "store")},
    )

    diagnostic = process_diagnostic(completed)
    assert completed.returncode != 0, (
        "CLI unexpectedly accepted a missing required argument\n" + diagnostic
    )
    assert "--assistant-message" in completed.stderr, (
        "CLI parser error did not identify the missing argument\n" + diagnostic
    )
    assert "required" in completed.stderr.lower(), (
        "CLI parser error did not report required-argument validation\n" + diagnostic
    )
