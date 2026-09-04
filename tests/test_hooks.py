"""E2E tests for Memplex integration layer: hooks, MCP server, CLI, skills."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from memplex.adapters.claude_skill import generate_hook_sh, generate_skill_md
from memplex.adapters.mcp_server import MCPServer
from memplex.adapters.runtime_status import read_runtime_status, runtime_status_path
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


def _claude_plugin_root(host_root: Path) -> Path:
    plugin_root = host_root / "plugins" / "marketplaces" / "articultur" / "plugin"
    plugin_root.mkdir(parents=True, exist_ok=True)
    return plugin_root


def _claude_identity(host_root: Path, project_path: Path | str, *, user_id: str) -> dict:
    host_root.mkdir(parents=True, exist_ok=True)
    return {
        "agent": "claude-code",
        "user_id": user_id,
        "project_path": str(Path(project_path).resolve(strict=False)),
        "python": sys.executable,
        "source_root": str(PROJECT_ROOT),
        "host_root": str(host_root.resolve()),
        "managed": {
            "by": "memplex",
            "installer": "memplex",
            "schema_version": 1,
        },
    }


# ═════════════════════════════════════════════════════════════════════
# Hook Runner E2E
# ═════════════════════════════════════════════════════════════════════


class TestHookRunner:
    """Test plugin/scripts/hook-runner.py end-to-end."""

    def _run_hook(
        self,
        command: str,
        extra_args: list | None = None,
        stdin_data: str | None = None,
        env: dict | None = None,
    ):
        args = [sys.executable, HOOK_RUNNER, command, *(extra_args or [])]
        isolated_root = Path(tempfile.mkdtemp(prefix="memplex-hook-"))
        test_env = {
            **os.environ,
            "HOME": str(isolated_root / "home"),
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": str(isolated_root / "memory"),
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
            env=test_env,check=False
        
        )
        return result

    def test_session_start_exits_zero(self):
        r = self._run_hook("session-start")
        assert r.returncode == 0, f"stderr: {r.stderr}"

    def test_setup_reports_installed_version(self):
        r = self._run_hook("setup")
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "v3.3.0" in r.stdout
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
            },check=False
        
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
            env=env,check=False
        
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
            env={**os.environ, "MEMPLEX_PROJECT_ROOT": str(PROJECT_ROOT)},check=False
        
        )
        assert r.returncode != 0
        assert "Usage" in r.stderr


# ═════════════════════════════════════════════════════════════════════
# Hook Runner Internals (white-box)
# ═════════════════════════════════════════════════════════════════════


def _load_hook_runner():
    """Import plugin/scripts/hook-runner.py as a module for white-box tests."""
    spec = importlib.util.spec_from_file_location("memplex_hook_runner", HOOK_RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_claude_real_prompt_failure_persists_degraded_host_runtime_state(tmp_path, monkeypatch):
    """A non-blocking Claude hook failure still leaves an operator-visible state."""
    hr = _load_hook_runner()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(hr, "_read_stdin_json", lambda: {"prompt": "remember status"})

    def fail_runtime(*_args, **_kwargs):
        raise RuntimeError("Bearer claude-secret-must-not-persist")

    monkeypatch.setattr(hr, "_init_runtime", fail_runtime)
    with pytest.raises(SystemExit) as exited:
        hr.cmd_prompt_submit()

    assert exited.value.code == 0
    assert read_runtime_status(runtime_status_path(tmp_path), agent="claude-code") == {
        "reason": "runtime_operation_failed",
        "state": "degraded",
    }


class TestHookRunnerInternals:
    """Regression tests for hook-runner.py helper functions."""

    def test_find_plugin_root_prefers_highest_semver(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        cache = tmp_path / "claude" / "plugins" / "cache" / "articultur" / "memplex"
        for version in ("3.2.7", "3.10.0"):
            scripts = cache / version / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "hook-runner.py").write_text("# stub\n")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.delenv("MEMPLEX_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)

        # Lexicographic order would pick 3.2.7; semver order must pick 3.10.0
        assert hr._find_plugin_root() == cache / "3.10.0"

    def test_find_plugin_root_skips_non_version_dirs(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        cache = tmp_path / "claude" / "plugins" / "cache" / "articultur" / "memplex"
        for name in ("latest", "3.2.7"):
            scripts = cache / name / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "hook-runner.py").write_text("# stub\n")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.delenv("MEMPLEX_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)

        assert hr._find_plugin_root() == cache / "3.2.7"

    def test_find_plugin_root_accepts_official_claude_plugin_root(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        monkeypatch.delenv("MEMPLEX_PLUGIN_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)

        assert hr._find_plugin_root() == plugin_root

    def test_rate_file_defaults_are_project_scoped(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        monkeypatch.delenv("MEMPLEX_OBS_RATE_FILE", raising=False)
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/alpha")
        rate_a = hr._rate_file()
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/beta")
        rate_b = hr._rate_file()

        assert rate_a != rate_b
        assert rate_a.name.startswith(".memplex_last_obs_")

    def test_rate_file_env_override_wins(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        override = tmp_path / "custom-rate"
        monkeypatch.setenv("MEMPLEX_OBS_RATE_FILE", str(override))
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/alpha")

        assert hr._rate_file() == override

    def test_managed_claude_identity_is_runtime_fallback(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        claude_root = tmp_path / "claude"
        plugin_root = _claude_plugin_root(claude_root)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (plugin_root / "memplex-agent.json").write_text(
            json.dumps(_claude_identity(claude_root, workspace, user_id="alice")),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPLEX_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("MEMPLEX_USER_ID", raising=False)
        monkeypatch.delenv("MEMPLEX_PROJECT_ROOT", raising=False)
        monkeypatch.setenv("USER", "different-system-user")

        assert hr._user_id() == "alice"
        assert hr._project_path() == str(workspace)

    def test_claude_managed_identity_cannot_be_overridden_by_payload_or_env(
        self,
        tmp_path,
        monkeypatch,
    ):
        hr = _load_hook_runner()
        claude_root = tmp_path / "claude"
        plugin_root = _claude_plugin_root(claude_root)
        (plugin_root / "memplex-agent.json").write_text(
            json.dumps(
                _claude_identity(
                    claude_root,
                    "/installed/project",
                    user_id="installed-user",
                )
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPLEX_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("MEMPLEX_USER_ID", raising=False)
        monkeypatch.delenv("MEMPLEX_PROJECT_ROOT", raising=False)

        payload = {
            "user_id": "payload-user",
            "cwd": "/payload/project",
            "session_id": "payload-session",
        }
        assert hr._user_id(payload) == "installed-user"
        assert hr._project_path(payload) == "/installed/project"
        assert hr._session_id(data=payload) == "payload-session"

        monkeypatch.setenv("MEMPLEX_USER_ID", "env-user")
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/env/project")
        monkeypatch.setenv("MEMPLEX_SESSION_ID", "env-session")
        assert hr._user_id(payload) == "installed-user"
        assert hr._project_path(payload) == "/installed/project"
        assert hr._session_id(data=payload) == "env-session"

    def test_claude_runtime_adapter_rejects_identity_for_another_host_root(
        self,
        tmp_path,
        monkeypatch,
    ):
        hr = _load_hook_runner()
        claude_root = tmp_path / "host-a"
        other_root = tmp_path / "host-b"
        other_root.mkdir()
        plugin_root = _claude_plugin_root(claude_root)
        identity = _claude_identity(claude_root, tmp_path, user_id="alice")
        identity["host_root"] = str(other_root.resolve())
        (plugin_root / "memplex-agent.json").write_text(
            json.dumps(identity), encoding="utf-8"
        )
        monkeypatch.setenv("MEMPLEX_PLUGIN_ROOT", str(plugin_root))

        with pytest.raises(ValueError, match="host_root.*reinstall required|reinstall required.*host_root"):
            hr._managed_identity()

    def test_claude_mcp_launcher_forces_managed_scope_and_preserves_host_session(
        self,
        tmp_path,
        monkeypatch,
    ):
        hr = _load_hook_runner()
        from memplex.adapters.mcp_server import MCPServer

        claude_root = tmp_path / "claude"
        plugin_root = _claude_plugin_root(claude_root)
        workspace = tmp_path / "managed-workspace"
        attacker_workspace = tmp_path / "attacker-workspace"
        workspace.mkdir()
        attacker_workspace.mkdir()
        (plugin_root / "memplex-agent.json").write_text(
            json.dumps(_claude_identity(claude_root, workspace, user_id="managed-alice")),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPLEX_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setenv("MEMPLEX_AGENT_ID", "attacker-agent")
        monkeypatch.setenv("MEMPLEX_USER_ID", "attacker-user")
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", str(attacker_workspace))
        monkeypatch.setenv("MEMPLEX_SESSION_ID", "host-trusted-session")
        captured: dict[str, str] = {}

        def capture_run(_self):
            captured.update(
                {
                    key: os.environ[key]
                    for key in (
                        "MEMPLEX_AGENT_ID",
                        "MEMPLEX_USER_ID",
                        "MEMPLEX_PROJECT_ROOT",
                        "MEMPLEX_SESSION_ID",
                    )
                }
            )

        monkeypatch.setattr(MCPServer, "run", capture_run)
        hr.cmd_mcp()

        assert captured == {
            "MEMPLEX_AGENT_ID": "claude-code",
            "MEMPLEX_USER_ID": "managed-alice",
            "MEMPLEX_PROJECT_ROOT": str(workspace.resolve()),
            "MEMPLEX_SESSION_ID": "host-trusted-session",
        }

    def test_claude_mcp_launcher_generates_process_stable_session_when_missing(
        self,
        tmp_path,
        monkeypatch,
    ):
        hr = _load_hook_runner()
        from memplex.adapters.mcp_server import MCPServer

        claude_root = tmp_path / "claude"
        plugin_root = _claude_plugin_root(claude_root)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (plugin_root / "memplex-agent.json").write_text(
            json.dumps(_claude_identity(claude_root, workspace, user_id="managed-alice")),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPLEX_PLUGIN_ROOT", str(plugin_root))
        # Register restoration points before the launcher installs its
        # managed identity and process-local fallback session.
        monkeypatch.setenv("MEMPLEX_AGENT_ID", "")
        monkeypatch.setenv("MEMPLEX_USER_ID", "")
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "")
        monkeypatch.setenv("MEMPLEX_SESSION_ID", "")
        sessions: list[str] = []
        monkeypatch.setattr(MCPServer, "run", lambda _self: sessions.append(os.environ["MEMPLEX_SESSION_ID"]))

        hr.cmd_mcp()
        hr.cmd_mcp()

        assert sessions == [f"claude-code-{os.getpid()}", f"claude-code-{os.getpid()}"]

    def test_claude_cache_hook_falls_back_to_marketplace_identity(
        self,
        tmp_path,
        monkeypatch,
    ):
        hr = _load_hook_runner()
        cached_plugin = tmp_path / "cache-plugin"
        cached_plugin.mkdir()
        marketplace_plugin = (
            tmp_path / "claude" / "plugins" / "marketplaces" / "articultur" / "plugin"
        )
        marketplace_plugin.mkdir(parents=True)
        (marketplace_plugin / "memplex-agent.json").write_text(
            json.dumps(
                _claude_identity(
                    tmp_path / "claude",
                    "/shared/project",
                    user_id="alice",
                )
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPLEX_PLUGIN_ROOT", str(cached_plugin))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.delenv("MEMPLEX_USER_ID", raising=False)
        monkeypatch.delenv("MEMPLEX_PROJECT_ROOT", raising=False)

        assert hr._user_id() == "alice"
        assert hr._project_path() == "/shared/project"

    def test_official_claude_project_dir_is_runtime_fallback(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/official/project")
        monkeypatch.delenv("MEMPLEX_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("MEMPLEX_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)

        assert hr._project_path() == "/official/project"

    def test_managed_identity_is_mirrored_to_claude_plugin_data(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        claude_root = tmp_path / "claude"
        plugin_root = _claude_plugin_root(claude_root)
        plugin_data = tmp_path / "plugin-data"
        identity = _claude_identity(claude_root, "/shared/project", user_id="alice")
        (plugin_root / "memplex-agent.json").write_text(json.dumps(identity), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.delenv("MEMPLEX_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)

        assert hr._managed_identity() == identity
        persisted = json.loads((plugin_data / "memplex-agent.json").read_text())
        assert persisted == identity
        assert (plugin_data / "memplex-agent.json").stat().st_mode & 0o777 == 0o600

    def test_claude_plugin_data_identity_survives_plugin_update(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        plugin_data = tmp_path / "plugin-data"
        plugin_data.mkdir()
        claude_root = tmp_path / "claude"
        identity = _claude_identity(claude_root, "/shared/project", user_id="alice")
        (plugin_data / "memplex-agent.json").write_text(json.dumps(identity), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.delenv("MEMPLEX_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("PLUGIN_ROOT", raising=False)

        assert hr._managed_identity() == identity
        assert hr._user_id() == "alice"

    def test_ensure_memplex_importable_does_not_pollute_sys_path(self):
        hr = _load_hook_runner()
        before = list(sys.path)
        hr._ensure_memplex_importable()
        assert sys.path == before

    def test_sanitize_payload_strips_private_tags_recursively(self):
        hr = _load_hook_runner()
        payload = {
            "command": "echo <private>secret</private> ok",
            "nested": {"items": ["a <private>hidden</private> b", 42]},
            "untouched": None,
        }
        cleaned = hr._sanitize_payload(payload)
        assert cleaned["command"] == "echo  ok"
        assert cleaned["nested"]["items"] == ["a  b", 42]
        assert cleaned["untouched"] is None

    def test_obs_rate_state_roundtrip_and_legacy_float(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        rate = tmp_path / "rate"
        monkeypatch.setenv("MEMPLEX_OBS_RATE_FILE", str(rate))

        assert hr._read_obs_rate_state() == (0.0, "")
        hr._write_obs_rate_state("key-1")
        ts, key = hr._read_obs_rate_state()
        assert ts > 0
        assert key == "key-1"

        # Legacy plain-float format (timestamp only) still parses
        rate.write_text("123.5")
        assert hr._read_obs_rate_state() == (123.5, "")
        rate.write_text("garbage")
        assert hr._read_obs_rate_state() == (0.0, "")

    def test_obs_rate_state_write_failure_is_visible(self, tmp_path, monkeypatch, capsys):
        hr = _load_hook_runner()
        monkeypatch.setenv("MEMPLEX_OBS_RATE_FILE", str(tmp_path))

        hr._write_obs_rate_state("event-key")

        assert "observation rate state skipped" in capsys.readouterr().err

    def test_observation_dedups_consecutive_identical_events(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        calls = []

        class FakeRuntime:
            def after_response(self, user_message, assistant_message, metadata):
                calls.append(user_message)

        payload = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/dup.py"}}
        monkeypatch.setattr(hr, "_init_runtime", lambda session_id="", data=None: FakeRuntime())
        monkeypatch.setattr(hr, "_read_stdin_json", lambda: dict(payload))
        rate = tmp_path / "rate"
        monkeypatch.setenv("MEMPLEX_OBS_RATE_FILE", str(rate))

        with pytest.raises(SystemExit) as exc:
            hr.cmd_observation("Read", "s1")
        assert exc.value.code == 0
        assert calls == ["[Read] Read: /tmp/dup.py"]

        # Cooldown over (ts=0) but identical event: dropped by dedup
        from memplex.core.hooks.policy import tool_event_key

        rate.write_text(json.dumps({"ts": 0, "key": tool_event_key("Read", payload["tool_input"])}))
        with pytest.raises(SystemExit):
            hr.cmd_observation("Read", "s1")
        assert len(calls) == 1

        # A different event after the cooldown is captured
        other = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/other.py"}}
        monkeypatch.setattr(hr, "_read_stdin_json", lambda: dict(other))
        with pytest.raises(SystemExit):
            hr.cmd_observation("Read", "s1")
        assert calls == ["[Read] Read: /tmp/dup.py", "[Read] Read: /tmp/other.py"]

    def test_observation_skips_empty_payload(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        calls = []

        class FakeRuntime:
            def after_response(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(hr, "_init_runtime", lambda session_id="", data=None: FakeRuntime())
        monkeypatch.setattr(
            hr, "_read_stdin_json", lambda: {"tool_name": "Write", "tool_input": {}}
        )
        monkeypatch.setenv("MEMPLEX_OBS_RATE_FILE", str(tmp_path / "rate"))

        with pytest.raises(SystemExit) as exc:
            hr.cmd_observation("Write", "s1")
        assert exc.value.code == 0
        assert calls == []

    def test_observation_strips_private_tags_from_metadata(self, tmp_path, monkeypatch):
        hr = _load_hook_runner()
        captured = {}

        class FakeRuntime:
            def after_response(self, user_message, assistant_message, metadata):
                captured["user_message"] = user_message
                captured["metadata"] = metadata

        monkeypatch.setattr(hr, "_init_runtime", lambda session_id="", data=None: FakeRuntime())
        monkeypatch.setattr(
            hr,
            "_read_stdin_json",
            lambda: {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "deploy <private>token-123</private> now",
                    "note": "key <private>api-key-xyz</private> here",
                },
            },
        )
        monkeypatch.setenv("MEMPLEX_OBS_RATE_FILE", str(tmp_path / "rate"))

        with pytest.raises(SystemExit) as exc:
            hr.cmd_observation("Bash", "session-1")
        assert exc.value.code == 0

        serialized = json.dumps(captured)
        assert "token-123" not in serialized
        assert "api-key-xyz" not in serialized
        assert captured["metadata"]["tool_name"] == "Bash"
        assert "deploy" in captured["metadata"]["tool_input"]["command"]

    def test_session_start_query_uses_project_keywords(self, monkeypatch):
        hr = _load_hook_runner()
        queries = []

        class Recalled:
            context = ""

        class FakeRuntime:
            def before_prompt(self, query):
                queries.append(query)
                return Recalled()

        monkeypatch.setattr(hr, "_init_runtime", lambda data=None: FakeRuntime())
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/widget")
        monkeypatch.delenv("MEMPLEX_SESSION_QUERY", raising=False)

        with pytest.raises(SystemExit):
            hr.cmd_session_start()

        assert queries, "session-start never recalled"
        assert "widget" in queries[0]
        assert not queries[0].startswith("session start")

    def test_session_start_query_env_override(self, monkeypatch):
        hr = _load_hook_runner()
        monkeypatch.setenv("MEMPLEX_SESSION_QUERY", "custom onboarding query")
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/widget")
        assert hr._session_start_query() == "custom onboarding query"


# ═════════════════════════════════════════════════════════════════════
# MCP Server E2E
# ═════════════════════════════════════════════════════════════════════


class TestMCPServerProtocol:
    """Test MCP server JSON-RPC protocol handling."""

    def test_initialize_returns_protocol_info(self, mcp_server):
        result = mcp_server._handle_initialize({})
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "memplex"
        assert result["serverInfo"]["version"] == "3.3.0"
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

    def test_hermes_turn_end_prefetches_next_turn(self, mcp_server, monkeypatch):
        mcp_server._ensure_service()
        monkeypatch.setenv("MEMPLEX_AGENT_ID", "hermes")
        monkeypatch.setenv("MEMPLEX_USER_ID", "user-1")
        monkeypatch.setenv("MEMPLEX_SESSION_ID", "session-1")
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/a")
        mcp_server._tool_memory_turn_end(
            {
                "user_message": "Hermes should prefetch Memplex context.",
                "assistant_message": "Captured.",
                "next_prompt_hint": "What should Hermes prefetch?",
            }
        )
        result = mcp_server._tool_memory_turn_begin(
            {
                "prompt": "What should Hermes prefetch?",
            }
        )
        assert result["source"] == "prefetch"
        assert "Memplex context" in result["context"]

    def test_mcp_turn_tools_scope_by_project_path(self, mcp_server, monkeypatch):
        mcp_server._ensure_service()
        monkeypatch.setenv("MEMPLEX_AGENT_ID", "codex")
        monkeypatch.setenv("MEMPLEX_USER_ID", "user-1")
        monkeypatch.setenv("MEMPLEX_SESSION_ID", "session-1")
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/a")
        mcp_server._tool_memory_turn_end(
            {
                "user_message": "Remember mcp-project-scope-token for project A.",
                "assistant_message": "Captured.",
            }
        )
        monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "/repo/b")
        result = mcp_server._tool_memory_turn_begin(
            {
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
        cfg_path.write_text(f"storage:\n  backend: lite\n  path: '{tmp_path}'")

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
                "HOME": str(tmp_path / "home"),
                "MEMPLEX_STORAGE_BACKEND": "lite",
                "MEMPLEX_STORAGE_PATH": str(tmp_path / "memory"),
                "MEMPLEX_CONFIG": str(cfg_path),
            },check=False
        
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

    def _run_cli(self, args: list, stdin_data: str | None = None):
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
            },check=False
        
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
            env=env,check=False
        
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
            env=env,check=False
        
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
            env=env,check=False
        
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
            env=env,check=False
        
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
        assert plugin == openclaw_plugin
        assert openclaw_plugin["kind"] == "memory"
        assert openclaw_plugin["activation"]["onStartup"] is True
        assert set(openclaw_plugin["contracts"]["tools"]) == {
            "memory_recall",
            "memory_store",
        }
        assert (target / "extensions" / "memplex" / "index.js").exists()
        assert (target / "extensions" / "memplex" / "package.json").exists()

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
        plugins = config.get("plugins", {})
        assert plugins.get("slots", {}).get("memory") != "memplex"
        assert "memplex" not in plugins.get("entries", {})
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
        assert "[marketplaces.memplex]" in text
        assert '[plugins."memplex@memplex"]' in text
        assert "memplex managed agent integration" in text
        assert (target / "plugins" / "marketplaces" / "memplex" / "plugin").exists()

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
        assert "[marketplaces.memplex]" not in text
        assert '[plugins."memplex@memplex"]' not in text
        assert not (target / "plugins" / "marketplaces" / "memplex").exists()
        assert not (target / "plugins" / "cache" / "memplex").exists()

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
        provider_path = target / "memplex.json"
        provider = json.loads(provider_path.read_text())
        assert provider["provider"] == "memplex"
        assert provider["prefetch"] is True
        assert provider["project_path"] == "/repo/a"
        assert "provider: memplex" in (target / "config.yaml").read_text()
        plugin_dir = target / "plugins" / "memplex"
        assert (plugin_dir / "plugin.yaml").exists()
        assert (plugin_dir / "__init__.py").exists()
        assert (plugin_dir / "memplex-agent.json").exists()

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
        assert not (target / "config.yaml").exists()

    def test_agent_install_hermes_refuses_unmanaged_provider(self, tmp_path):
        target = tmp_path / "hermes"
        target.mkdir(parents=True)
        provider_path = target / "memplex.json"
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
        assert "Hermes memplex.json already exists" in install.stderr
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
        target.mkdir(parents=True)
        provider_path = target / "memplex.json"
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
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},check=False
        
        )
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data[0]["agent"] == "claude-code"
        assert data[0]["status"] == "installed"

        market_dir = tmp_path / "plugins" / "marketplaces" / "articultur"
        assert (market_dir / ".claude-plugin" / "marketplace.json").exists()
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
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},check=False
        
        )
        # Then unsetup
        r = subprocess.run(
            [sys.executable, "-m", "memplex", "unsetup"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},check=False
        
        )
        assert r.returncode == 0
        assert "Memplex plugin uninstalled" in r.stdout
        market_dir = tmp_path / "plugins" / "marketplaces" / "articultur"
        assert not market_dir.exists()
        assert not (tmp_path / "settings.json").exists()
        assert not (tmp_path / "plugins" / "known_marketplaces.json").exists()
        assert not (tmp_path / "plugins" / "installed_plugins.json").exists()
        assert not (tmp_path / "plugins" / "cache" / "articultur" / "memplex").exists()

    def test_setup_uninstall_flag(self, tmp_path):
        subprocess.run(
            [sys.executable, "-m", "memplex", "setup", "--agent", "claude-code"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},check=False
        
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
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},check=False
        
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
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(tmp_path)},check=False
        
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

    def test_generate_hook_sh_reads_stdin_json_contract(self):
        """The generated hook must follow the real Claude Code PostToolUse
        contract (JSON on stdin), not the drifted env-var contract."""
        content = generate_hook_sh()
        assert "STDIN_JSON" in content
        assert "tool_name" in content
        assert "tool_input" in content
        assert "session_id" in content
        # Drifted contract: these env vars are never provided by Claude Code
        assert "MEMPLEX_TOOL_NAME" not in content
        assert "MEMPLEX_TOOL_INPUT" not in content

    def test_generate_hook_sh_is_non_blocking(self):
        content = generate_hook_sh()
        assert content.rstrip().endswith("exit 0")
        assert "set -euo" not in content

    def test_generate_hook_sh_has_rate_limit(self):
        content = generate_hook_sh()
        assert "RATE_FILE" in content
        assert "30" in content

    def test_generate_hook_sh_strips_private_tags(self):
        content = generate_hook_sh()
        assert "<private>" in content
        assert "memplex" in content

    def test_generate_hook_sh_write_to_file(self, tmp_path):
        out = str(tmp_path / "hook.sh")
        generate_hook_sh(output_path=out)
        assert Path(out).exists()
        assert Path(out).stat().st_mode & 0o111  # executable

    def test_generated_hook_sh_runs_with_stdin_json(self, tmp_path):
        """Functional: feed a PostToolUse payload on stdin; always exit 0."""
        hook = tmp_path / "hook.sh"
        generate_hook_sh(output_path=str(hook))
        rate = tmp_path / "rate"
        env = {
            **os.environ,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_OBS_RATE_FILE": str(rate),
        }
        payload = json.dumps(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/x <private>secret</private> y.py"},
                "session_id": "s1",
            }
        )
        first = subprocess.run(
            ["bash", str(hook)], input=payload, capture_output=True, text=True, timeout=30, env=env, check=False
        
        )
        assert first.returncode == 0, first.stderr
        assert rate.exists(), "rate-limit timestamp was not written"

        # Second call within the cooldown is skipped but still exits 0
        second = subprocess.run(
            ["bash", str(hook)], input=payload, capture_output=True, text=True, timeout=30, env=env, check=False
        
        )
        assert second.returncode == 0, second.stderr

        # Malformed stdin never breaks the hook contract
        third = subprocess.run(
            ["bash", str(hook)],
            input="not json",
            capture_output=True,
            text=True,
            timeout=30,
            env=env,check=False
        
        )
        assert third.returncode == 0, third.stderr


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
        assert data["version"] == "3.3.0"
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

    def test_hooks_use_fixed_claude_launcher_instead_of_inline_shell_discovery(self):
        data = json.loads(Path(PROJECT_ROOT / "plugin" / "hooks" / "hooks.json").read_text())
        commands = [
            hook["command"]
            for groups in data["hooks"].values()
            for group in groups
            for hook in group["hooks"]
        ]
        assert commands
        assert all("claude-hook.sh" in command for command in commands)
        assert all("_P=$(" not in command for command in commands)

    def test_claude_hooks_use_official_root_and_launcher_uses_its_own_directory(self):
        data = json.loads(Path(PROJECT_ROOT / "plugin" / "hooks" / "hooks.json").read_text())
        commands = [
            hook["command"]
            for groups in data["hooks"].values()
            for group in groups
            for hook in group["hooks"]
        ]
        assert all(
            command.startswith('bash "${CLAUDE_PLUGIN_ROOT}/scripts/claude-hook.sh" ')
            for command in commands
        )

        launcher = (PROJECT_ROOT / "plugin" / "scripts" / "claude-hook.sh").read_text()
        assert "BASH_SOURCE[0]" in launcher
        assert "CLAUDE_CONFIG_DIR" in launcher
        assert "memplex-agent.json" in launcher

    def test_mcp_json_valid(self):
        data = json.loads(Path(PROJECT_ROOT / "plugin" / ".mcp.json").read_text())
        assert "mcpServers" in data
        assert "memplex" in data["mcpServers"]
        assert data["mcpServers"]["memplex"]["type"] == "stdio"

    def test_mcp_json_avoids_hardcoded_python(self):
        data = json.loads(Path(PROJECT_ROOT / "plugin" / ".mcp.json").read_text())
        server = data["mcpServers"]["memplex"]
        assert server["command"] != "python"
        wrapper_ref = " ".join(server.get("args", []))
        assert "mcp-server.sh" in wrapper_ref

    def test_mcp_server_wrapper_requires_managed_identity(self):
        wrapper = PROJECT_ROOT / "plugin" / "scripts" / "mcp-server.sh"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "memplex-agent.json" in content
        assert "reinstall required" in content
        assert "command -v python3" not in content

    def test_mcp_server_wrapper_serves_mcp_over_stdio(self, tmp_path):
        claude_root = tmp_path / "claude-root"
        plugin_root = _claude_plugin_root(claude_root)
        shutil.copytree(PROJECT_ROOT / "plugin", plugin_root, dirs_exist_ok=True)
        (plugin_root / "memplex-agent.json").write_text(
            json.dumps(_claude_identity(claude_root, PROJECT_ROOT, user_id="wrapper-test"))
        )
        wrapper = plugin_root / "scripts" / "mcp-server.sh"
        init_msg = json.dumps({"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1})
        result = subprocess.run(
            ["bash", str(wrapper)],
            input=init_msg + "\n",
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "MEMPLEX_PLUGIN_ROOT": str(plugin_root),
                "MEMPLEX_STORAGE_BACKEND": "lite",
                "MEMPLEX_STORAGE_PATH": str(tmp_path / "wrapper-memory"),
            },check=False
        
        )
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout.strip().splitlines()[0])
        assert response["result"]["serverInfo"]["name"] == "memplex"

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
