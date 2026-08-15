"""Host runtimes must project trusted principal identity into the service boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.config import MemplexConfig
from memplex.service import MemplexService


def _registry(*, agent_id: str = "", token: str = "shared-production-token") -> str:
    return json.dumps(
        [
            {
                "credential_id": "shared-host-credential",
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "tenant_id": "tenant-production",
                "subject_id": "alice-production",
                "workspace_id": "workspace-production",
                "agent_id": agent_id,
                "roles": ["host"],
            }
        ]
    )


def _service(tmp_path: Path) -> MemplexService:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "store")
    config.llm.query_enhancement = False
    return MemplexService(config=config)


def test_runtime_persists_principal_fields_before_first_store_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _service(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="session-a",
        project_path=workspace,
    )
    observed = []
    original_merge = service.store.merge

    def merge(graph):
        observed.extend(
            (
                node.tenant_id,
                node.owner_subject_id,
                node.workspace_id,
                node.visibility,
                node.provenance.get("agent_id"),
            )
            for node in graph.nodes
        )
        return original_merge(graph)

    monkeypatch.setattr(service.store, "merge", merge)

    result = runtime.write_text(
        "Remember the principal boundary canary.",
        visibility="session",
    )
    memory_id = result.functions[0].id

    assert observed == [
        (
            runtime.authorization_context.principal.tenant_id,
            "alice",
            str(workspace.resolve()),
            "session",
            "codex",
        )
    ]
    stored = service.get(memory_id, authorization=runtime.authorization_context)
    assert stored is not None
    assert stored.owner == "alice"
    assert stored.origin_session == "session-a"


def test_runtime_tenant_is_shared_across_hosts_for_one_user_but_not_other_users(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="codex-session",
        project_path=workspace,
    )
    claude = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=workspace,
    )
    bob = AgentMemoryRuntime(
        service=service,
        agent="openclaw",
        user_id="bob",
        session_id="bob-session",
        project_path=workspace,
    )

    assert codex.authorization_context.principal.tenant_id == (
        claude.authorization_context.principal.tenant_id
    )
    assert codex.authorization_context.principal.tenant_id != (
        bob.authorization_context.principal.tenant_id
    )

    memory_id = codex.write_text("Remember cross-host principal sharing.").functions[0].id
    assert claude.get_accessible_memory(memory_id) is not None
    assert bob.get_accessible_memory(memory_id) is None


def test_explicit_trusted_authorization_cannot_be_replaced_by_runtime_user_arguments(
    tmp_path: Path,
) -> None:
    from memplex.auth import AuthorizationContext, Principal

    service = _service(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trusted = AuthorizationContext(
        principal=Principal(
            tenant_id="tenant-managed",
            subject_id="managed-alice",
            roles=frozenset({"host"}),
            authentication_id="managed-install",
        ),
        workspace_id=str(workspace.resolve()),
        agent_id="codex",
        session_id="managed-session",
        request_id="managed-request",
        provenance={"trust_boundary": "managed-host"},
    )

    runtime = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="attacker",
        session_id="attacker-session",
        project_path=tmp_path / "attacker-workspace",
        authorization=trusted,
    )
    memory_id = runtime.write_text("Remember managed identity wins.").functions[0].id
    stored = service.store.get(memory_id)

    assert runtime.user_id == "managed-alice"
    assert runtime.session_id == "managed-session"
    assert runtime.project_path == str(workspace.resolve())
    assert stored.tenant_id == "tenant-managed"
    assert stored.owner_subject_id == "managed-alice"
    assert stored.provenance["authentication_id"] == "managed-install"


@pytest.mark.parametrize("agent", ["codex", "claude-code", "openclaw", "hermes"])
def test_registry_wildcard_credential_projects_one_production_principal_across_hosts(
    monkeypatch,
    agent,
) -> None:
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", _registry())
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "shared-production-token")
    service = SimpleNamespace(
        _config=SimpleNamespace(deployment=SimpleNamespace(profile="production")),
        store=SimpleNamespace(),
    )

    runtime = AgentMemoryRuntime(
        service=service,
        agent=agent,
        user_id="untrusted-local-user",
        session_id=f"{agent}-trusted-session",
        project_path="/untrusted/local/project",
    )

    context = runtime.authorization_context
    assert context.principal.tenant_id == "tenant-production"
    assert context.principal.subject_id == "alice-production"
    assert context.workspace_id == "workspace-production"
    assert context.agent_id == agent
    assert context.session_id == f"{agent}-trusted-session"
    assert runtime.user_id == "alice-production"
    assert runtime.project_path == "workspace-production"


@pytest.mark.parametrize("token", [None, "foreign-token"])
def test_registry_never_falls_back_to_local_process_for_missing_or_invalid_token(
    monkeypatch,
    token,
) -> None:
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", _registry(agent_id="codex"))
    if token is None:
        monkeypatch.delenv("MEMPLEX_PRINCIPAL_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", token)
    service = SimpleNamespace(
        _config=SimpleNamespace(deployment=SimpleNamespace(profile="development")),
        store=SimpleNamespace(),
    )

    with pytest.raises(PermissionError, match="MEMPLEX_PRINCIPAL_TOKEN"):
        AgentMemoryRuntime(service=service, agent="codex", user_id="alice")


def test_registry_host_binding_rejects_another_agent(monkeypatch) -> None:
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", _registry(agent_id="codex"))
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "shared-production-token")
    service = SimpleNamespace(
        _config=SimpleNamespace(deployment=SimpleNamespace(profile="development")),
        store=SimpleNamespace(),
    )

    with pytest.raises(PermissionError, match="agent"):
        AgentMemoryRuntime(service=service, agent="hermes", user_id="alice")


@pytest.mark.parametrize(
    ("profile", "remote_active"),
    [("production", False), ("development", True)],
)
def test_production_or_active_remote_runtime_requires_registry(
    monkeypatch,
    profile,
    remote_active,
) -> None:
    monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    monkeypatch.delenv("MEMPLEX_PRINCIPAL_TOKEN", raising=False)
    store = SimpleNamespace(
        _config=SimpleNamespace(active=True)
    ) if remote_active else SimpleNamespace()
    service = SimpleNamespace(
        _config=SimpleNamespace(deployment=SimpleNamespace(profile=profile)),
        store=store,
    )

    with pytest.raises(PermissionError, match="principal registry"):
        AgentMemoryRuntime(service=service, agent="codex", user_id="alice")
