"""E2E tests for Memplex integration layer: hooks, MCP server, CLI, skills."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from memplex.adapters.claude_skill import generate_hook_sh, generate_skill_md
from memplex.adapters.mcp_server import MCPServer
from memplex.config import MemplexConfig
from memplex.service import MemplexService

# ── Fixtures ─────────────────────────────────────────────────────────


def _make_service(tmp_path: Path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    return MemplexService(config=cfg)


@pytest.fixture
def service(tmp_path):
    return _make_service(tmp_path)


@pytest.fixture
def mcp_server(tmp_path):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    return MCPServer(config=cfg)


HOOK_RUNNER = str(Path(__file__).resolve().parent.parent / "plugin" / "scripts" / "hook-runner.py")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ═════════════════════════════════════════════════════════════════════
# Hook Runner E2E
# ═════════════════════════════════════════════════════════════════════


class TestHookRunner:
    """Test plugin/scripts/hook-runner.py end-to-end."""

    def _run_hook(
        self,
        command: str,
        extra_args: list = None,
        stdin_data: str = None,
        env: dict = None,
    ):
        args = [sys.executable, HOOK_RUNNER, command, *(extra_args or [])]
        test_env = {
            **os.environ,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_PLUGIN_ROOT": str(Path(__file__).resolve().parent.parent / "plugin"),
            "MEMPLEX_PROJECT_ROOT": str(PROJECT_ROOT),
        }
        if env:
            test_env.update(env)
        result = subprocess.run(
            args,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=30,
            env=test_env,
        )
        return result

    def test_session_start_exits_zero(self):
        r = self._run_hook("session-start")
        assert r.returncode == 0, f"stderr: {r.stderr}"

    def test_setup_reports_installed_version(self):
        r = self._run_hook("setup")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "v3.2.7" in r.stdout
        assert "vunknown" not in r.stdout

    def test_session_start_outputs_context(self):
        r = self._run_hook("session-start")
        # Should print something to stdout (either context or "no memories")
        assert r.stdout.strip(), "session-start produced no output"

    def test_session_stop_exits_zero(self):
        r = self._run_hook("session-stop")
        assert r.returncode == 0, f"stderr: {r.stderr}"

    def test_session_stop_outputs_stats(self, tmp_path):
        r = self._run_hook(
            "session-stop",
            env={
                "HOME": str(tmp_path / "home"),
                "MEMPLEX_STORAGE_PATH": str(tmp_path / "session-stop-memory"),
            },
        )
        assert "[Memplex]" in r.stdout

    def test_observation_exits_zero(self):
        stdin_data = '{"tool_name":"Write","tool_input":{"file_path":"/tmp/test.py"}}'
        r = self._run_hook("observation", ["Write", "test-session-obs"], stdin_data=stdin_data)
        assert r.returncode == 0, f"stderr: {r.stderr}"

    def test_observation_empty_stdin_exits_zero(self):
        r = self._run_hook("observation", ["Write", "test-session-obs"])
        assert r.returncode == 0, f"stderr: {r.stderr}"

    def test_observation_parses_nested_tool_payload(self, tmp_path):
        storage_path = tmp_path / "hook-memory"
        rate_file = tmp_path / "rate"
        env = {
            "MEMPLEX_STORAGE_PATH": str(storage_path),
            "MEMPLEX_OBS_RATE_FILE": str(rate_file),
            "MEMPLEX_USER_ID": "hook-user",
            "MEMPLEX_SESSION_ID": "nested-session",
        }
        stdin_data = json.dumps(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/nested-hook-token.py"},
            }
        )
        r = self._run_hook("observation", stdin_data=stdin_data, env=env)
        assert r.returncode == 0, f"stderr: {r.stderr}"

        recall = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "agent",
                "recall",
                "--agent",
                "claude-code",
                "--user-id",
                "hook-user",
                "--session-id",
                "nested-session",
                "--project-path",
                str(PROJECT_ROOT),
                "nested-hook-token",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "MEMPLEX_STORAGE_BACKEND": "lite",
                "MEMPLEX_STORAGE_PATH": str(storage_path),
            },
        )
        assert recall.returncode == 0, recall.stderr
        assert "nested-hook-token" in json.loads(recall.stdout)["context"]

    def test_file_context_parses_nested_tool_payload(self, tmp_path):
        storage_path = tmp_path / "hook-file-memory"
        env = {
            **os.environ,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": str(storage_path),
        }
        capture = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "agent",
                "capture",
                "--agent",
                "claude-code",
                "--user-id",
                "hook-user",
                "--session-id",
                "file-session",
                "--project-path",
                str(PROJECT_ROOT),
                "--user-message",
                "Remember nested-file-context-token for nested.py.",
                "--assistant-message",
                "Captured.",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert capture.returncode == 0, capture.stderr

        stdin_data = json.dumps({"tool_input": {"file_path": str(tmp_path / "nested.py")}})
        r = self._run_hook(
            "file-context",
            stdin_data=stdin_data,
            env={
                "MEMPLEX_STORAGE_PATH": str(storage_path),
                "MEMPLEX_USER_ID": "hook-user",
                "MEMPLEX_SESSION_ID": "file-session",
            },
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "nested-file-context-token" in r.stdout

    def test_summarize_runs_compaction(self, tmp_path):
        r = self._run_hook(
            "summarize",
            env={
                "HOME": str(tmp_path / "home"),
                "MEMPLEX_STORAGE_PATH": str(tmp_path / "summary-memory"),
            },
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "Compaction:" in r.stdout

    def test_unknown_command_exits_nonzero(self):
        r = self._run_hook("nonexistent-command")
        assert r.returncode != 0

    def test_no_args_shows_usage(self):
        r = subprocess.run(
            [sys.executable, HOOK_RUNNER],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "MEMPLEX_PROJECT_ROOT": str(PROJECT_ROOT)},
        )
        assert r.returncode != 0
        assert "Usage" in r.stderr


# ═════════════════════════════════════════════════════════════════════
# MCP Server E2E
# ═════════════════════════════════════════════════════════════════════


class TestMCPServerProtocol:
    """Test MCP server JSON-RPC protocol handling."""

    def test_initialize_returns_protocol_info(self, mcp_server):
        result = mcp_server._handle_initialize({})
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "memplex"
        assert result["serverInfo"]["version"] == "3.2.7"
        assert "tools" in result["capabilities"]

    def test_tools_list_returns_definitions(self, mcp_server):
        result = mcp_server._handle_tools_list({})
        tools = result["tools"]
        names = [t["name"] for t in tools]
        assert "memory_search" in names
        assert "memory_add" in names
        assert "memory_get" in names
        assert "memory_delete" in names
        assert "memory_feedback" in names
        assert "memory_pending_reviews" in names
        assert "memory_resolve" in names
        assert "memory_update" in names
        assert "memory_health" in names
        assert "memory_agent_manifest" in names
        assert "memory_turn_begin" in names
        assert "memory_turn_end" in names
        assert len(tools) >= 12

    def test_tools_have_required_schema(self, mcp_server):
        tools = mcp_server._handle_tools_list({})["tools"]
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_search_tool_emphasizes_filter_first(self, mcp_server):
        tools = mcp_server._handle_tools_list({})["tools"]
        search_tool = next(t for t in tools if t["name"] == "memory_search")
        assert (
            "ALWAYS" in search_tool["description"] or "filter" in search_tool["description"].lower()
        )

    def test_get_tool_emphasizes_after_search(self, mcp_server):
        tools = mcp_server._handle_tools_list({})["tools"]
        get_tool = next(t for t in tools if t["name"] == "memory_get")
        assert "AFTER" in get_tool["description"] or "search" in get_tool["description"].lower()


class TestMCPServerTools:
    """Test MCP server tool implementations with real service."""

    def test_search_returns_results(self, mcp_server):
        mcp_server._ensure_service()
        result = mcp_server._tool_memory_search({"query": "test", "top_k": 5})
        assert "total" in result
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_add_and_get_memory(self, mcp_server):
        mcp_server._ensure_service()
        add_result = mcp_server._tool_memory_add(
            {
                "content": "测试记忆：当用户点击保存时，系统自动持久化数据",
                "source_type": "text",
            }
        )
        assert add_result["functions_extracted"] >= 1
        func_id = add_result["function_ids"][0]

        get_result = mcp_server._tool_memory_get({"memory_id": func_id})
        assert "error" not in get_result
        assert get_result["id"] == func_id

    def test_get_nonexistent_memory(self, mcp_server):
        mcp_server._ensure_service()
        result = mcp_server._tool_memory_get({"memory_id": "nonexistent_id"})
        assert "error" in result

    def test_add_and_delete_memory(self, mcp_server):
        mcp_server._ensure_service()
        add_result = mcp_server._tool_memory_add(
            {
                "content": "待删除记忆：临时测试内容",
                "source_type": "text",
            }
        )
        func_id = add_result["function_ids"][0]

        del_result = mcp_server._tool_memory_delete({"memory_id": func_id})
        assert del_result["status"] == "deleted"
        assert del_result["id"] == func_id

    def test_health_check(self, mcp_server):
        mcp_server._ensure_service()
        result = mcp_server._tool_memory_health({})
        assert "status" in result

    def test_pending_reviews(self, mcp_server):
        mcp_server._ensure_service()
        result = mcp_server._tool_memory_pending_reviews({"limit": 50})
        assert "total" in result
        assert "reviews" in result

    def test_agent_manifest_tool(self, mcp_server):
        mcp_server._ensure_service()
        result = mcp_server._tool_memory_agent_manifest({"agent": "openclaw"})
        assert result["name"] == "openclaw"
        assert result["config"]["plugins"]["slots"]["memory"] == "memplex"

    def test_turn_tools_capture_and_recall(self, mcp_server):
        mcp_server._ensure_service()
        mcp_server._tool_memory_turn_end(
            {
                "agent": "codex",
                "user_id": "user-1",
                "session_id": "session-1",
                "project_path": "/repo/a",
                "user_message": "Remember that Codex should use automatic memory capture.",
                "assistant_message": "Captured.",
            }
        )
        result = mcp_server._tool_memory_turn_begin(
            {
                "agent": "codex",
                "user_id": "user-1",
                "session_id": "session-1",
                "project_path": "/repo/a",
                "prompt": "What should Codex use?",
            }
        )
        assert "automatic memory capture" in result["context"]

    def test_hermes_turn_end_prefetches_next_turn(self, mcp_server):
        mcp_server._ensure_service()
        mcp_server._tool_memory_turn_end(
            {
                "agent": "hermes",
                "user_id": "user-1",
                "session_id": "session-1",
                "project_path": "/repo/a",
                "user_message": "Hermes should prefetch Memplex context.",
                "assistant_message": "Captured.",
                "next_prompt_hint": "What should Hermes prefetch?",
            }
        )
        result = mcp_server._tool_memory_turn_begin(
            {
                "agent": "hermes",
                "user_id": "user-1",
                "session_id": "session-1",
                "project_path": "/repo/a",
                "prompt": "What should Hermes prefetch?",
            }
        )
        assert result["source"] == "prefetch"
        assert "Memplex context" in result["context"]

    def test_mcp_turn_tools_scope_by_project_path(self, mcp_server):
        mcp_server._ensure_service()
        mcp_server._tool_memory_turn_end(
            {
                "agent": "codex",
                "user_id": "user-1",
                "session_id": "session-1",
                "project_path": "/repo/a",
                "user_message": "Remember mcp-project-scope-token for project A.",
                "assistant_message": "Captured.",
            }
        )
        result = mcp_server._tool_memory_turn_begin(
            {
                "agent": "codex",
                "user_id": "user-1",
                "session_id": "session-1",
                "project_path": "/repo/b",
                "prompt": "mcp-project-scope-token",
            }
        )
        assert "mcp-project-scope-token" not in result["context"]

    def test_request_routing_initialize(self, mcp_server):
        response = mcp_server._handle_request(
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {},
                "id": 1,
            }
        )
        assert response["id"] == 1
        assert "result" in response

    def test_request_routing_unknown_method(self, mcp_server):
        response = mcp_server._handle_request(
            {
                "jsonrpc": "2.0",
                "method": "nonexistent",
                "params": {},
                "id": 2,
            }
        )
        assert "error" in response
        assert response["error"]["code"] == -32601

    def test_request_routing_ping(self, mcp_server):
        response = mcp_server._handle_request(
            {
                "jsonrpc": "2.0",
                "method": "ping",
                "params": {},
                "id": 3,
            }
        )
        assert "result" in response

    def test_json_rpc_roundtrip_via_subprocess(self, tmp_path):
        """Full JSON-RPC roundtrip through stdin/stdout."""
        cfg_path = tmp_path / "memplex.yaml"
        cfg_path.write_text("storage:\n  backend: lite\n  path: '%s'" % str(tmp_path))

        init_msg = json.dumps({"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1})
        search_msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "memory_search",
                    "arguments": {"query": "test", "top_k": 3},
                },
                "id": 2,
            }
        )
        stdin_data = init_msg + "\n" + search_msg + "\n"

        result = subprocess.run(
            [sys.executable, "-m", "memplex.adapters.mcp_server"],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "MEMPLEX_STORAGE_BACKEND": "lite",
                "MEMPLEX_CONFIG": str(cfg_path),
            },
        )
        assert result.returncode == 0

        lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        assert len(lines) == 2

        init_resp = json.loads(lines[0])
        assert init_resp["result"]["serverInfo"]["name"] == "memplex"

        search_resp = json.loads(lines[1])
        assert (
            "total" in search_resp["result"]["content"][0]["text"]
            or "results" in search_resp["result"]["content"][0]["text"]
        )


# ═════════════════════════════════════════════════════════════════════
# CLI E2E
# ═════════════════════════════════════════════════════════════════════


class TestCLI:
    """Test CLI commands via subprocess."""

    def _run_cli(self, args: list, stdin_data: str = None):
        return subprocess.run(
            [sys.executable, "-m", "memplex", *args],
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "MEMPLEX_STORAGE_BACKEND": "lite",
                "MEMPLEX_STORAGE_PATH": tempfile.mkdtemp(prefix="memplex-cli-"),
            },
        )

    def test_health_command(self):
        r = self._run_cli(["health"])
        assert r.returncode == 0
        assert "status" in r.stdout

    def test_stats_command(self):
        r = self._run_cli(["stats"])
        assert "total_functions" in r.stdout

    def test_query_command(self):
        r = self._run_cli(["query", "test"])
        # query always returns output (may be empty results)
        assert r.returncode == 0

    def test_write_text_command(self):
        r = self._run_cli(["write", "--text", "CLI写入测试：当系统启动时，加载配置文件"])
        assert r.returncode == 0
        assert (
            "function" in r.stdout.lower() or "extracted" in r.stdout.lower() or "func_" in r.stdout
        )

    def test_json_output(self):
        r = self._run_cli(["--output", "json", "stats"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "total_functions" in data

    def test_agent_manifest_json_output(self):
        r = self._run_cli(["--output", "json", "agent", "manifest", "--agent", "hermes"])
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["name"] == "hermes"
        assert data["config"]["memory"]["provider"] == "memplex"

    def test_agent_capture_then_recall_across_cli_processes(self):
        storage_path = tempfile.mkdtemp(prefix="memplex-cli-loop-")
        env = {
            **os.environ,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": storage_path,
        }
        capture = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "agent",
                "capture",
                "--agent",
                "codex",
                "--user-id",
                "cli-user",
                "--session-id",
                "cli-session",
                "--project-path",
                "/repo/a",
                "--user-message",
                "Remember cli-closed-loop-token for later.",
                "--assistant-message",
                "Captured.",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert capture.returncode == 0, capture.stderr

        recall = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "agent",
                "recall",
                "--agent",
                "codex",
                "--user-id",
                "cli-user",
                "--session-id",
                "cli-session",
                "--project-path",
                "/repo/a",
                "cli-closed-loop-token",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert recall.returncode == 0, recall.stderr
        data = json.loads(recall.stdout)
        assert "cli-closed-loop-token" in data["context"]

    def test_agent_recall_scopes_by_cli_project_path(self):
        storage_path = tempfile.mkdtemp(prefix="memplex-cli-project-")
        env = {
            **os.environ,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": storage_path,
        }
        capture = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "agent",
                "capture",
                "--agent",
                "codex",
                "--user-id",
                "cli-user",
                "--session-id",
                "cli-session",
                "--project-path",
                "/repo/a",
                "--user-message",
                "Remember cli-project-scope-token for project A.",
                "--assistant-message",
                "Captured.",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert capture.returncode == 0, capture.stderr

        recall = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "agent",
                "recall",
                "--agent",
                "codex",
                "--user-id",
                "cli-user",
                "--session-id",
                "cli-session",
                "--project-path",
                "/repo/b",
                "cli-project-scope-token",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert recall.returncode == 0, recall.stderr
        data = json.loads(recall.stdout)
        assert "cli-project-scope-token" not in data["context"]

    def test_agent_install_openclaw_writes_and_uninstalls_config(self, tmp_path):
        target = tmp_path / "openclaw"
        install = self._run_cli(
            [
                "--output",
                "json",
                "agent",
                "install",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
                "--user-id",
                "alice",
                "--project-path",
                "/repo/a",
            ]
        )
        assert install.returncode == 0, install.stderr
        data = json.loads(install.stdout)
        assert data[0]["status"] == "installed"

        config = json.loads((target / "openclaw.json").read_text())
        assert config["plugins"]["slots"]["memory"] == "memplex"
        assert config["plugins"]["entries"]["memplex"]["config"]["userId"] == "alice"
        assert config["plugins"]["entries"]["memplex"]["config"]["projectPath"] == "/repo/a"
        plugin = json.loads((target / "extensions" / "memplex" / "plugin.json").read_text())
        openclaw_plugin = json.loads(
            (target / "extensions" / "memplex" / "openclaw.plugin.json").read_text()
        )
        assert plugin["slots"] == ["memory"]
        assert openclaw_plugin["kind"] == "memory"
        assert {hook["event"] for hook in plugin["hooks"]} == {
            "triage",
            "recall",
            "dream",
        }
        assert (target / "extensions" / "memplex" / "hooks" / "recall.py").exists()

        uninstall = self._run_cli(
            [
                "--output",
                "json",
                "agent",
                "uninstall",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        config = json.loads((target / "openclaw.json").read_text())
        assert config["plugins"]["slots"].get("memory") != "memplex"
        assert "memplex" not in config["plugins"].get("entries", {})
        assert not (target / "extensions" / "memplex").exists()

    def test_agent_install_openclaw_restores_previous_memory_slot(self, tmp_path):
        target = tmp_path / "openclaw"
        target.mkdir()
        (target / "openclaw.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "slots": {"memory": "existing-memory"},
                        "entries": {"existing-memory": {"enabled": True}},
                    }
                }
            )
        )

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 0, install.stderr
        config = json.loads((target / "openclaw.json").read_text())
        assert config["plugins"]["slots"]["memory"] == "memplex"
        assert (
            config["plugins"]["entries"]["memplex"]["config"]["managed"]["previousMemorySlot"]
            == "existing-memory"
        )

        uninstall = self._run_cli(
            [
                "agent",
                "uninstall",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        config = json.loads((target / "openclaw.json").read_text())
        assert config["plugins"]["slots"]["memory"] == "existing-memory"
        assert "existing-memory" in config["plugins"]["entries"]

    def test_agent_install_openclaw_refuses_unmanaged_memplex_entry(self, tmp_path):
        target = tmp_path / "openclaw"
        target.mkdir()
        config_path = target / "openclaw.json"
        original = {
            "plugins": {
                "slots": {"memory": "custom-memory"},
                "entries": {"memplex": {"enabled": True, "config": {"mode": "custom"}}},
            }
        }
        config_path.write_text(json.dumps(original, indent=2))

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 1
        assert "unmanaged memplex plugin entry" in install.stderr
        assert json.loads(config_path.read_text()) == original

        uninstall = self._run_cli(
            [
                "agent",
                "uninstall",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        assert json.loads(config_path.read_text()) == original

    def test_agent_install_openclaw_refuses_unmanaged_extension(self, tmp_path):
        target = tmp_path / "openclaw"
        extension_dir = target / "extensions" / "memplex"
        extension_dir.mkdir(parents=True)
        custom_manifest = {"id": "memplex", "provider": "custom"}
        (extension_dir / "plugin.json").write_text(json.dumps(custom_manifest))

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 1
        assert "extensions/memplex already exists" in install.stderr
        assert json.loads((extension_dir / "plugin.json").read_text()) == custom_manifest

    def test_agent_uninstall_openclaw_preserves_existing_allow_entry(self, tmp_path):
        target = tmp_path / "openclaw"
        target.mkdir()
        config_path = target / "openclaw.json"
        config_path.write_text(json.dumps({"plugins": {"allow": ["memplex"]}}))

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 0, install.stderr

        uninstall = self._run_cli(
            [
                "agent",
                "uninstall",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        config = json.loads(config_path.read_text())
        assert config["plugins"]["allow"] == ["memplex"]
        assert "memplex" not in config["plugins"].get("entries", {})
        assert config["plugins"].get("slots", {}).get("memory") != "memplex"

    def test_agent_uninstall_openclaw_noop_preserves_unmanaged_jsonc(self, tmp_path):
        target = tmp_path / "openclaw"
        target.mkdir()
        config_path = target / "openclaw.json"
        original = (
            "{\n"
            "  // user-managed config\n"
            '  "plugins": {\n'
            '    "slots": {"memory": "custom-memory",},\n'
            "  },\n"
            "}\n"
        )
        config_path.write_text(original)

        uninstall = self._run_cli(
            [
                "agent",
                "uninstall",
                "--agent",
                "openclaw",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        assert config_path.read_text() == original

    def test_agent_install_codex_managed_block_is_reversible(self, tmp_path):
        target = tmp_path / "codex"
        target.mkdir()
        config_path = target / "config.toml"
        config_path.write_text('model = "gpt-5.5"\n')

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "codex",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 0, install.stderr
        text = config_path.read_text()
        assert 'model = "gpt-5.5"' in text
        assert "[mcp_servers.memplex]" in text
        assert "memplex managed agent integration" in text

        uninstall = self._run_cli(
            [
                "agent",
                "uninstall",
                "--agent",
                "codex",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        text = config_path.read_text()
        assert 'model = "gpt-5.5"' in text
        assert "[mcp_servers.memplex]" not in text

    def test_agent_install_codex_refuses_unmanaged_memplex_table(self, tmp_path):
        target = tmp_path / "codex"
        target.mkdir()
        config_path = target / "config.toml"
        original = '[mcp_servers.memplex]\ncommand = "custom"\n'
        config_path.write_text(original)

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "codex",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 1
        assert "unmanaged [mcp_servers.memplex]" in install.stderr
        assert config_path.read_text() == original

    def test_agent_install_hermes_writes_and_removes_provider(self, tmp_path):
        target = tmp_path / "hermes"
        install = self._run_cli(
            [
                "--output",
                "json",
                "agent",
                "install",
                "--agent",
                "hermes",
                "--target-dir",
                str(target),
                "--user-id",
                "hermes-user",
                "--project-path",
                "/repo/a",
            ]
        )
        assert install.returncode == 0, install.stderr
        provider_path = target / "memory-providers" / "memplex.json"
        provider = json.loads(provider_path.read_text())
        assert provider["provider"] == "memplex"
        assert provider["prefetch"] is True
        assert provider["project_path"] == "/repo/a"
        plugin_dir = target / "plugins" / "memory" / "memplex"
        assert (plugin_dir / "plugin.yaml").exists()
        assert (plugin_dir / "__init__.py").exists()

        uninstall = self._run_cli(
            [
                "agent",
                "uninstall",
                "--agent",
                "hermes",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        assert not provider_path.exists()
        assert not plugin_dir.exists()

    def test_agent_install_hermes_refuses_unmanaged_provider(self, tmp_path):
        target = tmp_path / "hermes"
        provider_dir = target / "memory-providers"
        provider_dir.mkdir(parents=True)
        provider_path = provider_dir / "memplex.json"
        original_provider = {"name": "memplex", "provider": "custom"}
        provider_path.write_text(json.dumps(original_provider, indent=2))
        plugin_dir = target / "plugins" / "memory" / "memplex"
        plugin_dir.mkdir(parents=True)
        plugin_marker = plugin_dir / "README.md"
        plugin_marker.write_text("custom hermes provider\n")

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "hermes",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 1
        assert "memory-providers/memplex.json already exists" in install.stderr
        assert json.loads(provider_path.read_text()) == original_provider

        uninstall = self._run_cli(
            [
                "agent",
                "uninstall",
                "--agent",
                "hermes",
                "--target-dir",
                str(target),
            ]
        )
        assert uninstall.returncode == 0, uninstall.stderr
        assert provider_path.exists()
        assert plugin_marker.exists()

    def test_agent_install_all_rolls_back_partial_install_on_failure(self, tmp_path):
        target = tmp_path / "agents"
        provider_dir = target / "memory-providers"
        provider_dir.mkdir(parents=True)
        provider_path = provider_dir / "memplex.json"
        provider_path.write_text(json.dumps({"name": "memplex", "provider": "custom"}))

        install = self._run_cli(
            [
                "agent",
                "install",
                "--agent",
                "all",
                "--target-dir",
                str(target),
            ]
        )
        assert install.returncode == 1
        assert "Failed to install hermes" in install.stderr
        assert "Rolled back installed agents" in install.stderr

        codex_config = target / "config.toml"
        if codex_config.exists():
            assert "[mcp_servers.memplex]" not in codex_config.read_text()
        assert not (target / "plugins" / "marketplaces" / "articultur").exists()
        openclaw_config = target / "openclaw.json"
        if openclaw_config.exists():
            config = json.loads(openclaw_config.read_text())
            assert config.get("plugins", {}).get("slots", {}).get("memory") != "memplex"
            assert "memplex" not in config.get("plugins", {}).get("entries", {})
        assert not (target / "extensions" / "memplex").exists()
        assert provider_path.exists()

    def test_agent_install_all_dry_run_lists_each_agent(self, tmp_path):
        r = self._run_cli(
            [
                "--output",
                "json",
                "agent",
                "install",
                "--agent",
                "all",
                "--target-dir",
                str(tmp_path / "agents"),
                "--dry-run",
            ]
        )
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert {item["agent"] for item in data} == {
            "codex",
            "claude-code",
            "openclaw",
            "hermes",
        }
        assert all(item["status"] == "planned" for item in data)

    def test_get_nonexistent_id(self):
        r = self._run_cli(["get", "nonexistent_id_xyz"])
        # Should not crash — output may be error message or empty
        assert r.returncode in (0, 1)

    def test_setup_command(self, tmp_path):
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "setup",
                "--agent",
                "claude-code",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data[0]["agent"] == "claude-code"
        assert data[0]["status"] == "installed"

        market_dir = tmp_path / "plugins" / "marketplaces" / "articultur"
        assert (market_dir / "marketplace.json").exists()
        assert (market_dir / "plugin" / ".claude-plugin" / "plugin.json").exists()
        assert (market_dir / "plugin" / "hooks" / "hooks.json").exists()
        assert (market_dir / "plugin" / ".mcp.json").exists()
        assert (market_dir / "plugin" / "skills" / "mem-search" / "SKILL.md").exists()
        assert not (market_dir / "plugin" / "scripts" / "__pycache__").exists()

    def test_unsetup_command(self, tmp_path):
        # Setup first
        subprocess.run(
            [sys.executable, "-m", "memplex", "setup", "--agent", "claude-code"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},
        )
        # Then unsetup
        r = subprocess.run(
            [sys.executable, "-m", "memplex", "unsetup"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},
        )
        assert r.returncode == 0
        assert "Memplex plugin uninstalled" in r.stdout
        market_dir = tmp_path / "plugins" / "marketplaces" / "articultur"
        assert not market_dir.exists()

    def test_setup_uninstall_flag(self, tmp_path):
        subprocess.run(
            [sys.executable, "-m", "memplex", "setup", "--agent", "claude-code"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},
        )
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "memplex",
                "--output",
                "json",
                "setup",
                "--uninstall",
                "--agent",
                "claude-code",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data[0]["agent"] == "claude-code"
        assert data[0]["status"] == "uninstalled"

    def test_unsetup_when_not_installed(self, tmp_path):
        r = subprocess.run(
            [sys.executable, "-m", "memplex", "unsetup"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},
        )
        assert r.returncode == 0


# ═════════════════════════════════════════════════════════════════════
# Skill Generation
# ═════════════════════════════════════════════════════════════════════


class TestSkillGeneration:
    """Test SKILL.md and hook.sh generators."""

    def test_generate_skill_md_has_yaml_frontmatter(self):
        content = generate_skill_md()
        assert content.startswith("---")
        assert "name:" in content
        assert "description:" in content
        assert content.count("---") >= 2

    def test_generate_skill_md_contains_mcp_tools(self):
        content = generate_skill_md()
        assert "memory_search" in content
        assert "memory_get" in content
        assert "memory_add" in content

    def test_generate_skill_md_contains_3_layer_workflow(self):
        content = generate_skill_md()
        assert "Search" in content
        assert "Filter" in content
        assert "Fetch" in content

    def test_generate_skill_md_write_to_file(self, tmp_path):
        out = str(tmp_path / "SKILL.md")
        content = generate_skill_md(output_path=out)
        assert Path(out).exists()
        assert Path(out).read_text() == content

    def test_generate_hook_sh_is_bash_script(self):
        content = generate_hook_sh()
        assert content.startswith("#!/usr/bin/env bash")

    def test_generate_hook_sh_has_rate_limit(self):
        content = generate_hook_sh()
        assert "RATE_FILE" in content
        assert "30" in content

    def test_generate_hook_sh_strips_private_tags(self):
        content = generate_hook_sh()
        assert "memplex" in content

    def test_generate_hook_sh_write_to_file(self, tmp_path):
        out = str(tmp_path / "hook.sh")
        content = generate_hook_sh(output_path=out)
        assert Path(out).exists()
        assert Path(out).stat().st_mode & 0o111  # executable


# ═════════════════════════════════════════════════════════════════════
# Plugin Config Validation
# ═════════════════════════════════════════════════════════════════════


class TestPluginConfig:
    """Test plugin manifest and config files are valid."""

    def test_plugin_json_valid(self):
        data = json.loads(
            Path(PROJECT_ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text()
        )
        assert data["name"] == "memplex"
        assert data["version"] == "3.2.7"
        assert "repository" in data

    def test_hooks_json_valid(self):
        data = json.loads(Path(PROJECT_ROOT / "plugin" / "hooks" / "hooks.json").read_text())
        assert "hooks" in data
        assert "SessionStart" in data["hooks"]
        assert "PostToolUse" in data["hooks"]
        assert "Stop" in data["hooks"]

    def test_hooks_json_has_timeout(self):
        data = json.loads(Path(PROJECT_ROOT / "plugin" / "hooks" / "hooks.json").read_text())
        for hook_group in data["hooks"]["SessionStart"]:
            for hook in hook_group["hooks"]:
                assert "timeout" in hook
                assert hook["timeout"] > 0

    def test_mcp_json_valid(self):
        data = json.loads(Path(PROJECT_ROOT / "plugin" / ".mcp.json").read_text())
        assert "mcpServers" in data
        assert "memplex" in data["mcpServers"]
        assert data["mcpServers"]["memplex"]["type"] == "stdio"

    def test_skill_files_exist(self):
        skills_dir = PROJECT_ROOT / "plugin" / "skills"
        for skill_name in ["mem-search", "mem-write", "mem-explore", "mem-manage"]:
            skill_file = skills_dir / skill_name / "SKILL.md"
            assert skill_file.exists(), f"Missing skill: {skill_name}"

    def test_skill_files_have_frontmatter(self):
        skills_dir = PROJECT_ROOT / "plugin" / "skills"
        for skill_name in ["mem-search", "mem-write", "mem-explore", "mem-manage"]:
            content = (skills_dir / skill_name / "SKILL.md").read_text()
            assert content.startswith("---"), f"{skill_name} missing YAML frontmatter"
            assert "name:" in content
            assert "description:" in content
