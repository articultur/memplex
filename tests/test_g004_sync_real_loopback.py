"""Real loopback coverage for the public reliable-sync CLI lifecycle."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.g004_cli_runner import (
    parse_json_stdout,
    process_diagnostic,
    reserve_loopback_listener,
    run_cli,
    running_process,
    wait_for_http_ready,
)


MEMPLEX = ".venv/bin/memplex"


def _assert_success(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert completed.returncode == 0, (
        "CLI command failed\n" + process_diagnostic(completed)
    )
    payload = parse_json_stdout(completed)
    assert isinstance(payload, dict), (
        "CLI JSON must be an object\n" + process_diagnostic(completed)
    )
    return payload


def test_public_sync_cli_converges_two_real_loopback_peers(tmp_path: Path) -> None:
    try:
        importlib.import_module("uvicorn")
    except ImportError:
        pytest.fail("required test dependency unavailable: uvicorn", pytrace=False)

    listener = reserve_loopback_listener()
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    source_path = tmp_path / "source.json"
    destination_path = tmp_path / "destination.json"
    signing_secret = "g004-loopback-cursor-signing-secret"
    source_token = "g004-source-token"
    destination_token = "g004-destination-token"
    principals = json.dumps(
        [
            {
                "credential_id": "g004-source",
                "token_sha256": hashlib.sha256(source_token.encode()).hexdigest(),
                "tenant_id": "g004-loopback-tenant",
                "subject_id": "source-node",
                "workspace_id": "g004-loopback-workspace",
                "agent_id": "",
            },
            {
                "credential_id": "g004-destination",
                "token_sha256": hashlib.sha256(destination_token.encode()).hexdigest(),
                "tenant_id": "g004-loopback-tenant",
                "subject_id": "destination-node",
                "workspace_id": "g004-loopback-workspace",
                "agent_id": "",
            },
        ]
    )
    source_env = {
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(source_path),
        "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
        "MEMPLEX_SYNC_ENABLED": "true",
        "MEMPLEX_SYNC_NODE_ID": "source-node",
        "MEMPLEX_SYNC_CURSOR_SIGNING_KEY_ID": "g004-loopback-key",
        "MEMPLEX_SYNC_CURSOR_SIGNING_SECRET": signing_secret,
        "MEMPLEX_SYNC_TARGETS_JSON": json.dumps({"central-node": base_url}),
        "MEMPLEX_PRINCIPALS_JSON": principals,
        "MEMPLEX_PRINCIPAL_TOKEN": source_token,
        "MEMPLEX_SESSION_ID": "g004-source-session",
    }
    destination_env = {
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(destination_path),
        "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
        "MEMPLEX_SYNC_ENABLED": "true",
        "MEMPLEX_SYNC_NODE_ID": "destination-node",
        "MEMPLEX_SYNC_CURSOR_SIGNING_KEY_ID": "g004-loopback-key",
        "MEMPLEX_SYNC_CURSOR_SIGNING_SECRET": signing_secret,
        "MEMPLEX_SYNC_TARGETS_JSON": json.dumps({"central-node": base_url}),
        "MEMPLEX_PRINCIPALS_JSON": principals,
        "MEMPLEX_PRINCIPAL_TOKEN": destination_token,
        "MEMPLEX_SESSION_ID": "g004-destination-session",
    }

    source_status = _assert_success(
        run_cli([MEMPLEX, "--output", "json", "sync", "status"], env=source_env)
    )
    destination_status = _assert_success(
        run_cli(
            [MEMPLEX, "--output", "json", "sync", "status"],
            env=destination_env,
        )
    )
    assert source_status["status"] == "active"
    assert destination_status["status"] == "active"

    server_env = {
        **source_env,
        "MEMPLEX_SYNC_NODE_ID": "central-node",
        "MEMPLEX_SYNC_TARGETS_JSON": "{}",
    }
    server_command = [
        sys.executable,
        "-m",
        "uvicorn",
        "memplex.adapters.http_api:create_app",
        "--factory",
        "--fd",
        str(listener.fileno()),
        "--log-level",
        "warning",
        "--no-access-log",
    ]
    with listener, running_process(
        server_command,
        cwd=Path(__file__).resolve().parents[1],
        env=server_env,
        pass_fds=(listener.fileno(),),
    ) as process:
        wait_for_http_ready(base_url + "/health/ready", process)

        canary = f"g004-sync-loopback-{uuid4().hex}"
        _assert_success(
            run_cli(
                [MEMPLEX, "--output", "json", "write", "--text", canary],
                env=source_env,
            )
        )

        pulled = run_cli(
            [
                MEMPLEX,
                "--output",
                "json",
                "sync",
                "pull",
                "--target",
                "central-node",
            ],
            env=destination_env,
        )
        _assert_success(pulled)

        drained = _assert_success(
            run_cli(
                [
                    MEMPLEX,
                    "--output",
                    "json",
                    "sync",
                    "drain",
                    "--timeout",
                    "5",
                ],
                env=source_env,
            )
        )
        assert drained["drained"] is True

        recalled = run_cli(
            [MEMPLEX, "--output", "json", "recall", canary],
            env=destination_env,
        )
        recall_payload = _assert_success(recalled)
        assert canary in json.dumps(recall_payload, ensure_ascii=False), (
            "destination recall JSON missing generated canary\n"
            + process_diagnostic(recalled)
        )

        listed = run_cli(
            [
                MEMPLEX,
                "--output",
                "json",
                "sync",
                "dlq",
                "list",
                "--limit",
                "7",
            ],
            env=destination_env,
        )
        assert _assert_success(listed)["items"] == []

        absent_event_id = str(uuid4())
        replayed = run_cli(
            [
                MEMPLEX,
                "--output",
                "json",
                "sync",
                "dlq",
                "replay",
                "--target",
                "central-node",
                "--event-id",
                absent_event_id,
            ],
            env=destination_env,
        )
        assert replayed.returncode != 0, (
            "empty-DLQ replay unexpectedly succeeded\n"
            + process_diagnostic(replayed)
        )
        assert parse_json_stdout(replayed) == {
            "replayed": False,
            "target_id": "central-node",
            "event_id": absent_event_id,
        }
