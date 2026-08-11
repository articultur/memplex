"""MCP tools must enforce the same identity and visibility contract as agent runtimes."""

from __future__ import annotations

import getpass
import json

import pytest

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.adapters.mcp_server import MCPServer
from memplex.config import MemplexConfig


def _server(tmp_path) -> MCPServer:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "memory.json")
    server = MCPServer(config=config)
    server._ensure_service()
    return server


def _identity(monkeypatch, *, user: str, project, session: str = "shared-session") -> None:
    monkeypatch.setenv("MEMPLEX_AGENT_ID", "codex")
    monkeypatch.setenv("MEMPLEX_USER_ID", user)
    monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", str(project))
    monkeypatch.setenv("MEMPLEX_SESSION_ID", session)


def _add(server: MCPServer, token: str) -> str:
    result = server._tool_memory_add({"content": f"Remember {token} for this workspace."})
    return result["function_ids"][0]


def test_unmanaged_mcp_runtime_uses_os_user_instead_of_shared_default(tmp_path, monkeypatch):
    server = _server(tmp_path)
    for name in (
        "MEMPLEX_AGENT_ID",
        "MEMPLEX_USER_ID",
        "MEMPLEX_PROJECT_ROOT",
        "MEMPLEX_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    runtime = server._agent_runtime({})

    assert runtime.user_id == getpass.getuser()
    assert runtime.user_id != "default"


def test_mcp_tool_schemas_publish_model_facing_hard_limits(tmp_path):
    server = _server(tmp_path)
    tools = {tool["name"]: tool for tool in server._handle_tools_list({})["tools"]}

    search = tools["memory_search"]["inputSchema"]["properties"]
    pending = tools["memory_pending_reviews"]["inputSchema"]["properties"]
    observations = tools["memory_observations"]["inputSchema"]["properties"]
    turn_begin = tools["memory_turn_begin"]["inputSchema"]["properties"]
    turn_end = tools["memory_turn_end"]["inputSchema"]["properties"]
    scope = tools["memory_scope_explain"]["inputSchema"]["properties"]

    assert search["top_k"]["maximum"] == 100
    assert search["max_tokens"]["maximum"] == 32_000
    assert pending["limit"]["maximum"] == 1_000
    assert observations["limit"]["maximum"] == 1_000
    assert turn_begin["top_k"]["maximum"] == 100
    assert turn_begin["token_budget"]["maximum"] == 32_000
    identity_fields = {"agent", "user_id", "session_id", "project_path"}
    assert identity_fields.isdisjoint(turn_begin)
    assert identity_fields.isdisjoint(turn_end)
    assert identity_fields.isdisjoint(scope)


def test_raw_search_and_get_cannot_cross_mcp_identity_or_workspace(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    _identity(monkeypatch, user="alice", project=workspace_a)
    alice_id = _add(server, "mcp-alice-boundary-token")
    _identity(monkeypatch, user="bob", project=workspace_b)
    bob_id = _add(server, "mcp-bob-boundary-token")

    result = server._tool_memory_search({"query": "boundary-token", "top_k": 20})
    returned_ids = {item["id"] for item in result["results"]}
    assert bob_id in returned_ids
    assert alice_id not in returned_ids
    assert "error" in server._tool_memory_get({"memory_id": alice_id})


@pytest.mark.parametrize("operation", ["update", "delete", "feedback", "resolve"])
def test_id_mutations_reject_another_mcp_identity(tmp_path, monkeypatch, operation):
    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _identity(monkeypatch, user="alice", project=workspace)
    alice_id = _add(server, f"mcp-{operation}-owner-token")
    _identity(monkeypatch, user="bob", project=workspace)

    calls = {
        "update": lambda: server._tool_memory_update(
            {"memory_id": alice_id, "role": "action", "new_value": "tampered"}
        ),
        "delete": lambda: server._tool_memory_delete({"memory_id": alice_id}),
        "feedback": lambda: server._tool_memory_feedback(
            {
                "memory_id": alice_id,
                "role": "action",
                "index": 0,
                "verdict": "wrong",
            }
        ),
        "resolve": lambda: server._tool_memory_resolve(
            {"memory_id": alice_id, "field_role": "action", "action": "reject"}
        ),
    }
    with pytest.raises(PermissionError, match="not found or inaccessible"):
        calls[operation]()


def test_pending_reviews_are_filtered_by_accessible_memory(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ids = {}
    for user in ("alice", "bob"):
        _identity(monkeypatch, user=user, project=workspace)
        memory_id = _add(server, f"mcp-{user}-review-token")
        ids[user] = memory_id
        server._tool_memory_feedback(
            {
                "memory_id": memory_id,
                "role": "action",
                "index": 0,
                "verdict": "wrong",
            }
        )

    _identity(monkeypatch, user="alice", project=workspace)
    result = server._tool_memory_pending_reviews({"limit": 50})
    assert result["total"] == 1
    assert result["reviews"][0]["memory_id"] == ids["alice"]


def test_pending_review_scan_and_result_limit_are_hard_capped(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _identity(monkeypatch, user="alice", project=workspace)
    calls = []

    def get_pending_reviews(*, limit):
        calls.append(limit)
        return []

    monkeypatch.setattr(server._service, "get_pending_reviews", get_pending_reviews)

    result = server._tool_memory_pending_reviews({"limit": 10_000_000})

    assert result == {"total": 0, "reviews": []}
    assert calls == [1_000]


def test_observations_are_filtered_by_workspace(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    for project, token in ((workspace_a, "obs-a-token"), (workspace_b, "obs-b-token")):
        _identity(monkeypatch, user="alice", project=project)
        server._tool_memory_turn_end(
            {
                "user_message": f"Remember {token}.",
                "assistant_message": "Captured.",
            }
        )

    _identity(monkeypatch, user="alice", project=workspace_a)
    result = server._tool_memory_observations({"limit": 50})
    summaries = "\n".join(item["summary"] for item in result["observations"])
    assert "obs-a-token" in summaries
    assert "obs-b-token" not in summaries


def test_observation_backend_failure_is_not_reported_as_an_empty_success(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _identity(monkeypatch, user="alice", project=workspace)

    def fail_listing(**_kwargs):
        raise RuntimeError("observation backend unavailable")

    monkeypatch.setattr(server._service.store, "list_observations", fail_listing)
    with pytest.raises(RuntimeError, match="observation backend unavailable"):
        server._tool_memory_observations({"limit": 10})


def test_observation_scan_and_result_limit_are_hard_capped(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _identity(monkeypatch, user="alice", project=workspace)
    calls = []

    def list_observations(**kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(server._service.store, "list_observations", list_observations)

    result = server._tool_memory_observations({"limit": 10_000_000})

    assert result == {"total": 0, "observations": []}
    assert calls == [
        {
            "offset": 0,
            "limit": 1_000,
            "category": None,
            "owner": "alice",
        }
    ]


def test_installed_environment_identity_cannot_be_overridden_by_tool_arguments(
    tmp_path, monkeypatch
):
    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    AgentMemoryRuntime(
        service=server._service,
        agent="codex",
        user_id="bob",
        session_id="bob-session",
        project_path=workspace,
    ).after_response("mcp-trusted-env-token", "Captured for Bob.")
    _identity(monkeypatch, user="alice", project=workspace, session="alice-session")

    result = server._tool_memory_turn_begin(
        {
            "agent": "codex",
            "user_id": "bob",
            "session_id": "bob-session",
            "project_path": str(workspace),
            "prompt": "mcp-trusted-env-token",
        }
    )

    assert "mcp-trusted-env-token" not in result["context"]


def test_unmanaged_mcp_identity_arguments_cannot_select_another_scope(
    tmp_path, monkeypatch
):
    """Tool arguments are data, never an unmanaged MCP identity source."""

    server = _server(tmp_path)
    victim_workspace = tmp_path / "victim-workspace"
    victim_workspace.mkdir()
    victim = AgentMemoryRuntime(
        service=server._service,
        agent="codex",
        user_id="victim-user",
        session_id="victim-session",
        project_path=victim_workspace,
    )
    victim.after_response("mcp-unmanaged-victim-token", "Captured for victim.")
    for name in (
        "MEMPLEX_AGENT_ID",
        "MEMPLEX_USER_ID",
        "MEMPLEX_PROJECT_ROOT",
        "MEMPLEX_SESSION_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    forged_identity = {
        "agent": "codex",
        "user_id": "victim-user",
        "session_id": "victim-session",
        "project_path": str(victim_workspace),
    }
    recalled = server._tool_memory_turn_begin(
        {**forged_identity, "prompt": "mcp-unmanaged-victim-token"}
    )
    server._tool_memory_turn_end(
        {
            **forged_identity,
            "user_message": "Remember mcp-unmanaged-forged-write-token.",
            "assistant_message": "Captured.",
        }
    )

    assert "mcp-unmanaged-victim-token" not in recalled["context"]
    assert "mcp-unmanaged-forged-write-token" not in victim.before_prompt(
        "mcp-unmanaged-forged-write-token"
    ).context


def test_mcp_explain_redacts_legacy_record_when_migration_fails(
    tmp_path, monkeypatch
):
    """MCP explain must expose the same authorized records as search results."""

    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _identity(monkeypatch, user="alice", project=workspace, session="legacy-session")
    token = "mcp-legacy-explanation-failure-token"
    server._agent_runtime({}).after_response(
        f"I prefer {token} responses.",
        "Captured.",
    )
    stored = server._service.store.list_preferences(owner="alice")[0]
    stored.namespace = {}
    server._service.store.add_preference(stored)

    def fail_migration(*_args, **_kwargs):
        raise RuntimeError("namespace persistence unavailable")

    monkeypatch.setattr(server._service, "annotate_memories", fail_migration)

    payload = server._tool_memory_search(
        {"query": token, "top_k": 10, "explain": True}
    )

    assert payload["total"] == 0
    assert payload["results"] == []
    assert payload["explanation"]["results"] == []
    assert stored.id not in json.dumps(payload, sort_keys=True)


def test_scope_preview_uses_trusted_identity_without_global_corpus_count(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    _identity(monkeypatch, user="alice", project=workspace_a)
    alice_id = _add(server, "mcp-alice-preview-token")
    _identity(monkeypatch, user="bob", project=workspace_b)
    bob_id = _add(server, "mcp-bob-preview-token")

    _identity(monkeypatch, user="alice", project=workspace_a)
    result = server._tool_memory_scope_explain(
        {
            "agent": "codex",
            "user_id": "bob",
            "project_path": str(workspace_b),
            "preview": True,
        }
    )

    assert result["identity"]["user_id"] == "alice"
    assert "total_functions" not in result["preview"]
    sample_ids = {item["id"] for item in result["preview"]["sample"]}
    assert alice_id in sample_ids
    assert bob_id not in sample_ids


def test_scope_preview_scan_is_hard_capped(tmp_path, monkeypatch):
    server = _server(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _identity(monkeypatch, user="alice", project=workspace)
    calls = []

    def list_functions(*, limit):
        calls.append(limit)
        return []

    monkeypatch.setattr(server._service.store, "list_functions", list_functions)

    result = server._tool_memory_scope_explain({"preview": True})

    assert calls == [1_000]
    assert result["preview"]["scan_limit"] == 1_000
    assert result["preview"]["scanned_functions"] == 0
