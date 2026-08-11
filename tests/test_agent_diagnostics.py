"""Read-only diagnostics for the four supported agent hosts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memplex.adapters import cli
from memplex.adapters.agent_installer import (
    inspect_agent_installation,
    install_agent,
    uninstall_agent,
)
from memplex.adapters.agent_runtime import get_agent_manifest
from memplex.config import MemplexConfig
from memplex.product import run_agent_diagnostics, run_doctor, scope_explain, scope_preview
from memplex.service import MemplexService


@pytest.mark.parametrize("agent", ["codex", "claude-code", "openclaw", "hermes"])
def test_installation_diagnostics_report_managed_selected_host(tmp_path: Path, agent: str):
    root = tmp_path / agent
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    install_agent(
        agent,
        target_dir=root,
        user_id="alice",
        project_path=workspace,
    )

    status = inspect_agent_installation(agent, target_dir=root)

    assert status["selected_host"] == agent
    assert status["status"] == "healthy"
    assert status["install_state"] == {
        "installed": True,
        "selected": True,
        "managed": True,
        "reinstall_needed": False,
    }
    assert status["drift_reasons"] == []
    assert Path(status["paths"]["root"]) == root
    assert status["identity"]["user_id"] == "alice"
    assert status["identity"]["project_path"] == str(workspace.resolve())
    assert status["identity"]["source"].endswith("memplex-agent.json")


def test_installation_diagnostics_detect_config_drift(tmp_path: Path):
    root = tmp_path / "openclaw"
    install_agent(
        "openclaw",
        target_dir=root,
        user_id="alice",
        project_path=tmp_path,
    )
    config = root / "openclaw.json"
    config.write_text(
        config.read_text().replace('"memory": "memplex"', '"memory": "other"'),
        encoding="utf-8",
    )

    status = inspect_agent_installation("openclaw", target_dir=root)

    assert status["status"] == "drifted"
    assert status["install_state"]["installed"] is True
    assert status["install_state"]["managed"] is True
    assert status["install_state"]["selected"] is False
    assert status["install_state"]["reinstall_needed"] is True
    assert "memory provider is not selected" in status["drift_reasons"]


def test_claude_uninstall_preserves_unmanaged_marketplace(tmp_path: Path):
    market = tmp_path / "plugins" / "marketplaces" / "articultur"
    market.mkdir(parents=True)
    sentinel = market / "user-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")

    uninstall_agent("claude-code", target_dir=tmp_path)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_claude_install_refuses_unmanaged_marketplace(tmp_path: Path):
    market = tmp_path / "plugins" / "marketplaces" / "articultur"
    market.mkdir(parents=True)
    (market / "marketplace.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="not managed by Memplex"):
        install_agent(
            "claude-code",
            target_dir=tmp_path,
            user_id="alice",
            project_path=tmp_path,
        )


def test_manifest_and_scope_expose_shared_memory_contract(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    manifest = get_agent_manifest("codex")
    scope = scope_explain(
        agent="codex",
        user_id="alice",
        session_id="session-1",
        project_path=str(workspace),
        storage_namespace="store-1",
    )

    assert manifest["schema_version"] == 1
    assert manifest["memory_contract"]["default_visibility"] == "workspace"
    assert manifest["memory_contract"]["supported_visibilities"] == [
        "session",
        "user",
        "workspace",
    ]
    assert scope["schema_version"] == 1
    assert scope["identity"]["workspace_id"] == str(workspace.resolve())
    assert scope["visibility"]["default"] == "workspace"
    assert [item["memplex_visibility"] for item in scope["read_namespace_filters"][:3]] == [
        "session",
        "workspace",
        "user",
    ]
    assert scope["write_namespace"]["memplex_source_agent"] == "codex"
    assert scope["write_namespace"]["memplex_source_session_id"] == "session-1"


def test_scope_preview_uses_or_semantics_for_read_visibility(tmp_path: Path):
    scope = scope_explain(
        agent="codex",
        user_id="alice",
        session_id="session-1",
        project_path=str(tmp_path),
        storage_namespace="store-1",
    )
    functions = [
        SimpleNamespace(
            id=f"memory-{index}",
            name=f"memory-{index}",
            memory_type="function",
            domain="test",
            attributes=dict(branch),
        )
        for index, branch in enumerate(scope["read_namespace_filters"][:3])
    ]
    functions.append(
        SimpleNamespace(
            id="foreign",
            name="foreign",
            memory_type="function",
            domain="test",
            attributes={
                **scope["read_namespace_filters"][1],
                "memplex_user_id": "bob",
            },
        )
    )
    service = SimpleNamespace(store=SimpleNamespace(list_functions=lambda *, limit: functions))

    preview = scope_preview(service, scope["read_namespace_filters"])

    assert preview["filter_mode"] == "or"
    assert "total_functions" not in preview
    assert preview["matched_in_scan"] == 3
    assert {item["id"] for item in preview["sample"]} == {
        "memory-0",
        "memory-1",
        "memory-2",
    }


def test_agent_diagnostics_resolve_installed_identity_and_doctor_surfaces_it(tmp_path: Path):
    root = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_agent(
        "codex",
        target_dir=root,
        user_id="alice",
        project_path=workspace,
    )
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "memory")
    service = MemplexService(config=config)

    try:
        diagnostics = run_agent_diagnostics(
            service,
            agent="codex",
            target_dir=root,
            session_id="session-1",
        )
        doctor = run_doctor(
            service,
            agent="codex",
            target_dir=root,
            session_id="session-1",
        )
    finally:
        service.stop()

    assert diagnostics["selected_host"] == "codex"
    assert diagnostics["identity"]["user_id"] == "alice"
    assert diagnostics["identity"]["source"] == "installed"
    assert diagnostics["workspace"]["workspace_id"] == str(workspace.resolve())
    assert diagnostics["visibility"]["effective"] == "workspace"
    assert diagnostics["install_state"]["status"] == "healthy"
    checks = {item["name"]: item for item in doctor["checks"]}
    assert checks["agent_manifest"]["details"]["hook_events"]
    assert checks["memory_scope_contract"]["details"]["identity"]["user_id"] == "alice"
    assert checks["agent_installation"]["details"]["status"] == "healthy"


def test_doctor_reports_missing_host_as_warning_without_failing_service(tmp_path: Path):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "memory")
    service = MemplexService(config=config)
    try:
        doctor = run_doctor(
            service,
            agent="openclaw",
            target_dir=tmp_path / "not-installed",
            user_id="alice",
            project_path=tmp_path,
        )
    finally:
        service.stop()

    checks = {item["name"]: item for item in doctor["checks"]}
    assert doctor["status"] == "pass"
    assert checks["agent_installation"]["status"] == "warning"
    assert checks["agent_installation"]["details"]["status"] == "not_installed"


def test_agent_status_cli_exposes_single_read_only_snapshot(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    root = tmp_path / "hermes"
    install_agent(
        "hermes",
        target_dir=root,
        user_id="alice",
        project_path=tmp_path,
    )
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "memory")
    service = MemplexService(config=config)
    monkeypatch.setattr(cli, "_make_service", lambda _path=None: service)

    result = cli.cmd_agent(
        SimpleNamespace(
            agent_command="status",
            agent="hermes",
            target_dir=str(root),
            user_id=None,
            session_id="session-1",
            project_path=None,
            config=None,
            output="json",
        )
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_host"] == "hermes"
    assert payload["install_state"]["status"] == "healthy"
    assert payload["identity"]["user_id"] == "alice"


def test_agent_status_all_returns_partial_reports_on_one_host_failure(
    monkeypatch,
    capsys,
):
    import memplex.product as product

    stopped = []
    fake_service = SimpleNamespace(stop=lambda: stopped.append(True))
    monkeypatch.setattr(cli, "_make_service", lambda _path=None: fake_service)

    def diagnose(_service, *, agent, **_kwargs):
        if agent == "hermes":
            raise RuntimeError("broken provider config")
        return {"selected_host": agent, "status": "ok"}

    monkeypatch.setattr(product, "run_agent_diagnostics", diagnose)
    result = cli.cmd_agent(
        SimpleNamespace(
            agent_command="status",
            agent="all",
            target_dir=None,
            user_id=None,
            session_id="default",
            project_path=None,
            config=None,
            output="json",
        )
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["codex"]["status"] == "ok"
    assert payload["hermes"] == {
        "schema_version": 1,
        "selected_host": "hermes",
        "status": "error",
        "error": "broken provider config",
    }
    assert stopped == [True]


def test_agent_status_all_rejects_one_shared_target_root(capsys):
    result = cli.cmd_agent(
        SimpleNamespace(
            agent_command="status",
            agent="all",
            target_dir="/ambiguous/root",
            config=None,
            output="json",
        )
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "four different host roots" in payload["error"]
