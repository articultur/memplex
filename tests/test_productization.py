"""Productized Memplex operator surfaces."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from memplex.adapters.mcp_server import MCPServer
from memplex.config import MemplexConfig
from memplex.product import run_doctor
from memplex.service import MemplexService


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
    }


def _run_memplex(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "memplex", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_query_explain_returns_retrieval_trace(tmp_path):
    env = _offline_env(tmp_path)
    canary = "product-query-explain-token"

    write = _run_memplex(
        ["--output", "json", "write", "--text", f"{canary}: explain should show stages."],
        env=env,
    )
    assert write.returncode == 0, write.stderr

    query = _run_memplex(
        ["--output", "json", "recall", canary, "--explain"],
        env=env,
    )
    assert query.returncode == 0, query.stderr
    payload = json.loads(query.stdout)
    assert payload["total"] >= 1
    assert payload["explanation"]["query"] == canary
    assert payload["explanation"]["schema_version"] == 1
    assert payload["explanation"]["budget"]["tokens_used"] >= 0
    assert "after_token_budget" in payload["explanation"]["selection"]


def test_doctor_profile_smoke_is_offline_and_productized(tmp_path):
    env = _offline_env(tmp_path)
    result = _run_memplex(
        ["--output", "json", "doctor", "--agent", "codex", "--profile", "local", "--smoke"],
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert payload["profile"]["name"] == "local"
    assert {check["name"] for check in payload["checks"]} >= {
        "service_health",
        "agent_manifest",
        "setup_profile",
        "capture_recall_smoke",
    }


def test_scope_explain_and_preview_are_visibility_only(tmp_path):
    env = _offline_env(tmp_path)
    explain = _run_memplex(
        [
            "--output",
            "json",
            "scope",
            "explain",
            "--agent",
            "codex",
            "--user-id",
            "alice",
            "--session-id",
            "s1",
            "--project-path",
            "/repo/a",
        ],
        env=env,
    )
    assert explain.returncode == 0, explain.stderr
    payload = json.loads(explain.stdout)
    assert "not an ACL" in payload["scope_boundary"]
    assert payload["namespace_filter"]["memplex_user_id"] == "alice"

    preview = _run_memplex(
        ["--output", "json", "scope", "preview", "--agent", "codex"],
        env=env,
    )
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["boundary"].startswith("Preview only")


def test_inbox_wraps_pending_reviews(tmp_path):
    env = _offline_env(tmp_path)
    write = _run_memplex(
        ["--output", "json", "write", "--text", "inbox-token: review this memory."],
        env=env,
    )
    assert write.returncode == 0, write.stderr
    memory_id = json.loads(write.stdout)["function_ids"][0]

    feedback = _run_memplex(
        [
            "--output",
            "json",
            "feedback",
            memory_id,
            "--role",
            "trigger",
            "--index",
            "0",
            "--verdict",
            "wrong",
        ],
        env=env,
    )
    assert feedback.returncode == 0, feedback.stderr

    inbox = _run_memplex(["--output", "json", "inbox", "list"], env=env)
    assert inbox.returncode == 0, inbox.stderr
    payload = json.loads(inbox.stdout)
    assert payload["total"] >= 1
    assert payload["reviews"][0]["memory_id"] == memory_id


def test_corpus_manifest_preview_index_and_recall_are_bounded(tmp_path):
    env = _offline_env(tmp_path)
    root = tmp_path / "corpus-root"
    (root / "docs").mkdir(parents=True)
    (root / ".codex" / "memories").mkdir(parents=True)
    (root / "nested" / ".codex").mkdir(parents=True)
    (root / "nested" / ".agents").mkdir(parents=True)
    (root / "nested" / ".claude").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text(
        "canonical-corpus-token: this should be indexed.\n",
        encoding="utf-8",
    )
    (root / ".codex" / "memories" / "private.md").write_text(
        "private-corpus-token: this must stay denied.\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("ROOT_SECRET=1\n", encoding="utf-8")
    (root / "token.txt").write_text("root token must stay denied.\n", encoding="utf-8")
    (root / "nested" / ".codex" / "private.md").write_text("nested codex\n", encoding="utf-8")
    (root / "nested" / ".agents" / "private.md").write_text("nested agents\n", encoding="utf-8")
    (root / "nested" / ".claude" / "private.md").write_text("nested claude\n", encoding="utf-8")
    manifest = root / "memplex-corpus.toml"
    manifest.write_text(
        "\n".join(
            [
                "[corpus]",
                'name = "docs"',
                'scope = "project"',
                'include = ["*", "**/*"]',
            ]
        ),
        encoding="utf-8",
    )

    preview = _run_memplex(
        ["--output", "json", "corpus", "preview", "--manifest", str(manifest)],
        env=env,
    )
    assert preview.returncode == 0, preview.stderr
    preview_payload = json.loads(preview.stdout)
    assert preview_payload["included_count"] >= 1
    assert preview_payload["denied_count"] >= 6
    included = {item["path"] for item in preview_payload["included"]}
    assert "docs/guide.md" in included
    denied = {item["path"] for item in preview_payload["denied"]}
    assert {
        ".codex/memories/private.md",
        ".env",
        "token.txt",
        "nested/.codex/private.md",
        "nested/.agents/private.md",
        "nested/.claude/private.md",
    } <= denied

    index = _run_memplex(
        ["--output", "json", "corpus", "index", "--manifest", str(manifest)],
        env=env,
    )
    assert index.returncode == 0, index.stderr
    assert json.loads(index.stdout)["indexed_count"] >= 1

    recall = _run_memplex(
        ["--output", "json", "corpus", "recall", "canonical-corpus-token"],
        env=env,
    )
    assert recall.returncode == 0, recall.stderr
    recall_payload = json.loads(recall.stdout)
    assert recall_payload["total"] >= 1
    source_paths = {result["source_path"] for result in recall_payload["results"]}
    assert "docs/guide.md" in source_paths
    assert not any(path.startswith(".codex/") for path in source_paths)
    assert ".env" not in source_paths
    assert "token.txt" not in source_paths
    assert all(
        result["id"] in {item["id"] for item in recall_payload["results"]}
        for result in recall_payload["explanation"]["results"]
    )


def test_corpus_recall_explanation_does_not_leak_non_corpus_results(tmp_path):
    env = _offline_env(tmp_path)
    root = tmp_path / "corpus-root"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text(
        "shared-product-token: corpus memory.\n",
        encoding="utf-8",
    )
    manifest = root / "memplex-corpus.toml"
    manifest.write_text(
        "\n".join(
            [
                "[corpus]",
                'name = "docs"',
                'scope = "project"',
                'include = ["docs/*.md"]',
            ]
        ),
        encoding="utf-8",
    )

    non_corpus = _run_memplex(
        [
            "--output",
            "json",
            "write",
            "--text",
            "shared-product-token: non-corpus memory must not appear in corpus explanation.",
        ],
        env=env,
    )
    assert non_corpus.returncode == 0, non_corpus.stderr
    non_corpus_id = json.loads(non_corpus.stdout)["function_ids"][0]

    index = _run_memplex(
        ["--output", "json", "corpus", "index", "--manifest", str(manifest)],
        env=env,
    )
    assert index.returncode == 0, index.stderr

    recall = _run_memplex(
        ["--output", "json", "corpus", "recall", "shared-product-token"],
        env=env,
    )
    assert recall.returncode == 0, recall.stderr
    payload = json.loads(recall.stdout)
    result_ids = {item["id"] for item in payload["results"]}
    explanation_ids = {item["id"] for item in payload["explanation"]["results"]}
    assert non_corpus_id not in result_ids
    assert non_corpus_id not in explanation_ids


def test_policy_show_report_and_setup_profile_surfaces(tmp_path):
    env = _offline_env(tmp_path)

    policy = _run_memplex(["--output", "json", "policy", "show"], env=env)
    assert policy.returncode == 0, policy.stderr
    policy_payload = json.loads(policy.stdout)
    assert policy_payload["embedding"]["remote_default"] is False
    assert "not an ACL" in policy_payload["scope_boundary"]

    report = _run_memplex(["--output", "json", "report"], env=env)
    assert report.returncode == 0, report.stderr
    report_payload = json.loads(report.stdout)
    assert "health" in report_payload
    assert report_payload["lifecycle"]["boundary"].startswith("Derived labels only")

    setup = _run_memplex(
        [
            "--output",
            "json",
            "setup",
            "--agent",
            "codex",
            "--target-dir",
            str(tmp_path / "codex"),
            "--dry-run",
            "--profile",
            "privacy",
        ],
        env=env,
    )
    assert setup.returncode == 0, setup.stderr
    setup_payload = json.loads(setup.stdout)
    assert setup_payload["profile"]["name"] == "privacy"
    assert setup_payload["result"][0]["agent"] == "codex"


def test_mcp_product_tools_are_available(tmp_path):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "store")
    cfg.llm.semantic_extraction = False
    cfg.llm.query_enhancement = False
    cfg.llm.conflict_resolution = False
    cfg.llm.summarization = False
    cfg.llm.reranking = False

    server = MCPServer(config=cfg)
    tools = server._handle_tools_list({})["tools"]
    names = {tool["name"] for tool in tools}
    assert {
        "memory_doctor",
        "memory_scope_explain",
        "memory_policy_show",
    } <= names

    try:
        doctor = server._tool_memory_doctor({"agent": "codex", "smoke": True})
        assert doctor["status"] == "pass"
        scope = server._tool_memory_scope_explain({"agent": "codex", "preview": True})
        assert "namespace_filter" in scope
        policy = server._tool_memory_policy_show({"agent": "codex"})
        assert policy["embedding"]["remote_default"] is False
    finally:
        if server._service is not None:
            server._service.stop()


def test_doctor_smoke_cleans_canary_when_query_fails(tmp_path, monkeypatch):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "store")
    cfg.llm.semantic_extraction = False
    cfg.llm.query_enhancement = False
    cfg.llm.conflict_resolution = False
    cfg.llm.summarization = False
    cfg.llm.reranking = False
    service = MemplexService(config=cfg)

    def fail_query(*args, **kwargs):
        raise RuntimeError("forced query failure")

    monkeypatch.setattr(service, "query", fail_query)
    try:
        report = run_doctor(service, agent="codex", smoke=True)
        assert report["status"] == "fail"
        funcs = service.store.list_functions(limit=100)
        assert all("memplex-doctor-smoke-token" not in func.name for func in funcs)
    finally:
        service.stop()
