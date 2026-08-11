"""Offline and release-surface robustness gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from memplex.adapters.mcp_server import MCPServer
from memplex.config import MemplexConfig
from memplex.retrieval import embedding
from memplex.service import MemplexService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _offline_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(tmp_path / "store"),
        "MEMPLEX_EMBEDDING_MODEL": "default",
        "MEMPLEX_LLM_SEMANTIC_EXTRACTION": "false",
        "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
        "MEMPLEX_LLM_CONFLICT_RESOLUTION": "false",
        "MEMPLEX_LLM_SUMMARIZATION": "false",
        "MEMPLEX_LLM_RERANKING": "false",
        "HF_HOME": str(tmp_path / "hf-home"),
        "SENTENCE_TRANSFORMERS_HOME": str(tmp_path / "sentence-transformers-home"),
    }


def _run_memplex(
    args: list[str],
    *,
    env: dict[str, str],
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "memplex", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _combined_output(*results: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(f"{result.stdout}\n{result.stderr}" for result in results).lower()


def test_cli_default_offline_write_query_does_not_touch_huggingface(tmp_path):
    env = _offline_env(tmp_path)
    canary = "offline-e2e-bm25-token"

    write = _run_memplex(
        [
            "--output",
            "json",
            "write",
            "--text",
            f"{canary}: local BM25 retrieval keeps Mainland offline recall working.",
        ],
        env=env,
    )
    assert write.returncode == 0, write.stderr
    assert json.loads(write.stdout)["functions_extracted"] >= 1

    query = _run_memplex(
        [
            "--output",
            "json",
            "query",
            f"Mainland offline recall {canary}",
        ],
        env=env,
    )
    assert query.returncode == 0, query.stderr
    payload = json.loads(query.stdout)
    assert payload["total"] >= 1
    assert any(canary in result["summary"] for result in payload["results"])
    assert (tmp_path / "store" / "memory.json.fts5.db").exists()

    output = _combined_output(write, query)
    assert "huggingface.co" not in output
    assert "sentence-transformers" not in output


def test_agent_capture_recall_closed_loop_works_offline(tmp_path):
    env = _offline_env(tmp_path)
    canary = "offline-agent-loop-token"

    capture = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "capture",
            "--agent",
            "hermes",
            "--user-id",
            "offline-user",
            "--session-id",
            "offline-session",
            "--project-path",
            str(PROJECT_ROOT),
            "--user-message",
            f"Remember {canary} for offline agent recall.",
            "--assistant-message",
            "Captured.",
        ],
        env=env,
    )
    assert capture.returncode == 0, capture.stderr
    assert json.loads(capture.stdout)["status"] == "captured"

    recall = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "recall",
            "--agent",
            "hermes",
            "--user-id",
            "offline-user",
            "--session-id",
            "offline-session",
            "--project-path",
            str(PROJECT_ROOT),
            canary,
        ],
        env=env,
    )
    assert recall.returncode == 0, recall.stderr
    payload = json.loads(recall.stdout)
    assert canary in payload["context"]

    output = _combined_output(capture, recall)
    assert "huggingface.co" not in output
    assert "sentence-transformers" not in output


def test_explicit_huggingface_failure_falls_back_through_service(monkeypatch, tmp_path):
    def fail_if_loaded(model_name: str, dimension: int):
        raise RuntimeError(f"{model_name} unavailable in offline test")

    monkeypatch.setattr(embedding, "_SentenceTransformerEmbedder", fail_if_loaded)

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "store")
    cfg.embedding.model = "bge-m3"
    cfg.embedding.dimension = 16
    cfg.llm.query_enhancement = False

    service = MemplexService(config=cfg)
    try:
        service.write_text("explicit-hf-fallback-token: offline fallback still writes and recalls.")
        result = service.query("explicit-hf-fallback-token")
    finally:
        service.stop()

    assert result.results
    assert "explicit-hf-fallback-token" in result.results[0].summary


def test_release_version_surfaces_stay_in_sync_for_fresh_installs():
    version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]["version"]

    npm_package = json.loads((PROJECT_ROOT / "npm" / "memplex" / "package.json").read_text())
    assert npm_package["version"] == version

    installer = (PROJECT_ROOT / "scripts" / "install-agent.sh").read_text()
    assert f'DEFAULT_PACKAGE="memplex=={version}"' in installer
    assert "MEMPLEX_PACKAGE" not in installer
    assert "--package" not in installer
    assert "--from" not in installer

    npm_bin = (PROJECT_ROOT / "npm" / "memplex" / "bin" / "memplex.js").read_text()
    assert "install-agent.sh" in npm_bin
    assert "raw.githubusercontent.com" not in npm_bin

    source_plugin = json.loads(
        (PROJECT_ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    packaged_plugin = json.loads(
        (PROJECT_ROOT / "memplex" / "_plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert source_plugin["version"] == version
    assert packaged_plugin["version"] == version

    initialize = MCPServer()._handle_initialize({})
    assert initialize["serverInfo"]["version"] == version
