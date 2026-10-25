"""CLI principal handling must be explicit, tenant-bound, and fail closed."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from memplex.adapters import cli
from memplex.config import MemplexConfig
from memplex.service import MemplexService


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _principals() -> str:
    return json.dumps(
        [
            {
                "credential_id": "cli-alice",
                "token_sha256": _digest("cli-token-alice"),
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "workspace_id": "workspace-a",
                "agent_id": "cli",
                "roles": ["member"],
            },
            {
                "credential_id": "cli-bob",
                "token_sha256": _digest("cli-token-bob"),
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "workspace_id": "workspace-b",
                "roles": ["member"],
            },
        ]
    )


def _ns(**values):
    defaults = {"config": None, "output": "json"}
    defaults.update(values)
    return SimpleNamespace(**defaults)


@pytest.fixture
def service(tmp_path, monkeypatch):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "memories")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    monkeypatch.setattr(cli, "_make_service", lambda _config_path=None: svc)
    yield svc
    svc.stop()


def test_cli_registry_identity_is_stamped_and_forged_owner_cannot_expand_scope(
    service, monkeypatch, capsys
):
    """A CLI command gets identity only from its env-held credential."""
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", _principals())
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "cli-token-alice")

    assert cli.cmd_write(_ns(text="cli-principal-canary", owner="bob")) == 0
    capsys.readouterr()
    memory = service.store.list_functions(limit=1)[0]
    assert memory.tenant_id == "tenant-a"
    assert memory.owner_subject_id == "alice"
    assert memory.workspace_id == "workspace-a"
    assert memory.provenance["transport"] == "cli"
    assert memory.provenance["agent_id"] == "cli"

    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "cli-token-bob")
    assert cli.cmd_query(
        _ns(
            text="cli-principal-canary",
            top_k=10,
            max_tokens=4000,
            explain=False,
            owner="alice",
        )
    ) == 0
    assert json.loads(capsys.readouterr().out)["results"] == []

    assert cli.cmd_get(_ns(memory_id=memory.id)) == 1
    assert "Memory not found" in capsys.readouterr().err
    assert cli.main(["delete", memory.id]) == 1
    assert "Memory not found" in capsys.readouterr().err
    assert service.store.get(memory.id) is not None

    # Compaction mutates a shared store internally and currently has no
    # per-principal service API, so the CLI must fail closed rather than run
    # it with Bob's authenticated process identity but no tenant scope.
    assert cli.main(["compact"]) == 1
    assert "principal-scoped" in capsys.readouterr().err


@pytest.mark.parametrize("token", [None, "not-in-registry"])
def test_production_cli_rejects_missing_or_invalid_registry_credential_before_service_creation(
    monkeypatch, capsys, token
):
    """Production never falls back to a shared secret or local identity."""
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", _principals())
    monkeypatch.setenv("MEMPLEX_API_KEY", "legacy-shared-secret")
    if token is None:
        monkeypatch.delenv("MEMPLEX_PRINCIPAL_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", token)

    created = []
    monkeypatch.setattr(cli, "_make_service", lambda _config_path=None: created.append(True))

    assert cli.main(["write", "--text", "must-not-persist"]) == 1
    assert created == []
    assert "principal" in capsys.readouterr().err.lower()


def test_production_cli_requires_registry_even_when_legacy_shared_secret_exists(monkeypatch, capsys):
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")
    monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    monkeypatch.setenv("MEMPLEX_API_KEY", "legacy-shared-secret")

    created = []
    monkeypatch.setattr(cli, "_make_service", lambda _config_path=None: created.append(True))

    assert cli.main(["query", "must-not-read"]) == 1
    assert created == []
    assert "principal registry" in capsys.readouterr().err.lower()


def test_production_cli_sync_pull_uses_the_principal_scoped_sync_facade(monkeypatch, capsys):
    """Sync pull must never call the raw SyncableStore in production."""
    from memplex.sync import SyncableStore

    class StrictSyncStore(SyncableStore):
        def __init__(self):
            self.raw_pull_calls = 0
            self.contexts = []

        def authorized(self, context):
            self.contexts.append(context)
            class ScopedPull:
                def pull_incremental(self):
                    return {
                        "status": "pulled",
                        "tenant": context.principal.tenant_id,
                        "canonicalized_by": "trusted-context",
                    }

            return ScopedPull()

        def pull_incremental(self):  # pragma: no cover - the assertion is the contract
            self.raw_pull_calls += 1
            raise AssertionError("raw sync pull bypassed the principal facade")

    class FakeService:
        def __init__(self, store):
            self.store = store

        def stop(self, **_kwargs):
            pass

    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", _principals())
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "cli-token-alice")
    monkeypatch.setenv("MEMPLEX_REMOTE_URL", "https://sync.example.test")
    store = StrictSyncStore()
    monkeypatch.setattr(cli, "_make_service", lambda _config_path=None: FakeService(store))

    assert cli.main(["--output", "json", "sync", "pull"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tenant"] == "tenant-a"
    assert payload["canonicalized_by"] == "trusted-context"
    assert store.raw_pull_calls == 0
    assert len(store.contexts) == 1
    assert store.contexts[0].principal.tenant_id == "tenant-a"
    assert store.contexts[0].agent_id == "cli"


@pytest.mark.parametrize("agent", ["codex", "claude-code", "openclaw", "hermes"])
def test_agent_cli_development_preserves_local_process_scope(tmp_path, monkeypatch, capsys, agent):
    """CLI-generated development fallback must not erase local adapter scope."""
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path / "cli-memory"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    monkeypatch.delenv("MEMPLEX_PRINCIPAL_TOKEN", raising=False)
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    project = str(tmp_path / "project-a")
    identity = [
        "--agent", agent, "--user-id", "alice", "--session-id", "session-a",
        "--project-path", project,
    ]
    assert cli.main([
        "--output", "json", "agent", "capture", *identity,
        "--user-message", "Remember cli-development-scope-canary.",
        "--assistant-message", "Captured.",
    ]) == 0
    capsys.readouterr()
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "cli-memory")
    service = MemplexService(config=config)
    try:
        memories = service.store.list_functions(limit=10)
        assert memories
        assert all(memory.owner_subject_id == "alice" for memory in memories)
        assert all(memory.workspace_id == project for memory in memories)
        assert all(memory.origin_session == "session-a" for memory in memories)
        assert all(memory.provenance["agent_id"] == agent for memory in memories)
    finally:
        service.stop()
    assert cli.main([
        "--output", "json", "agent", "recall", *identity, "cli-development-scope-canary",
    ]) == 0
    assert "cli-development-scope-canary" in json.loads(capsys.readouterr().out)["context"]
    for user, workspace in [("alice", str(tmp_path / "project-b")), ("bob", project)]:
        assert cli.main([
            "--output", "json", "agent", "recall", "--agent", agent,
            "--user-id", user, "--session-id", "session-a", "--project-path", workspace,
            "cli-development-scope-canary",
        ]) == 0
        assert "cli-development-scope-canary" not in json.loads(capsys.readouterr().out)["context"]


@pytest.mark.parametrize("registry_agent", ["", "codex"])
def test_agent_cli_registry_scope_cannot_be_replaced_by_local_arguments(
    tmp_path, monkeypatch, capsys, registry_agent
):
    """The development fallback fix must preserve real env-bound identity."""
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path / "registry-memory"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEMPLEX_SESSION_ID", "trusted-session")
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "cli-registry-control")
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", json.dumps([{
        "credential_id": "cli-registry-control", "token_sha256": _digest("cli-registry-control"),
        "tenant_id": "trusted-tenant", "subject_id": "trusted-user",
        "workspace_id": "trusted-workspace", "agent_id": registry_agent, "roles": ["host"],
    }]))
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    identity = [
        "--agent", "codex", "--user-id", "forged-user", "--session-id", "forged-session",
        "--project-path", str(tmp_path / "forged-project"),
    ]
    assert cli.main([
        "--output", "json", "agent", "capture", *identity,
        "--user-message", "Remember cli-registry-scope-control.",
        "--assistant-message", "Captured.",
    ]) == 0
    capsys.readouterr()
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "registry-memory")
    service = MemplexService(config=config)
    try:
        memories = service.store.list_functions(limit=10)
        assert memories
        assert all(memory.tenant_id == "trusted-tenant" for memory in memories)
        assert all(memory.owner_subject_id == "trusted-user" for memory in memories)
        assert all(memory.workspace_id == "trusted-workspace" for memory in memories)
        assert all(memory.origin_session == "trusted-session" for memory in memories)
        assert all(memory.provenance["agent_id"] == "codex" for memory in memories)
    finally:
        service.stop()
    assert cli.main([
        "--output", "json", "agent", "recall", *identity, "cli-registry-scope-control",
    ]) == 0
    assert "cli-registry-scope-control" in json.loads(capsys.readouterr().out)["context"]


def test_agent_runtime_explicit_development_context_is_not_cli_fallback(service):
    """An actual explicit compatibility context stays intact in the runtime."""
    from memplex.adapters.agent_runtime import AgentMemoryRuntime
    from memplex.auth import local_development_context

    context = local_development_context()
    runtime = AgentMemoryRuntime(
        service=service, agent="codex", user_id="ignored-user", session_id="ignored-session",
        project_path="/ignored-project", authorization=context,
    )
    assert runtime.authorization_context is context
    assert runtime.user_id == context.principal.subject_id
    assert runtime.project_path == context.workspace_id
    assert runtime.session_id == context.session_id


def test_cli_development_sentinel_has_factory_only_trust_provenance(monkeypatch):
    """A registry claiming similar identity fields is not the CLI fallback."""
    from memplex.authorization import AuthorizationGate

    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "development")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    monkeypatch.delenv("MEMPLEX_PRINCIPAL_TOKEN", raising=False)
    fallback = cli._cli_authorization(agent_id="codex")
    assert AuthorizationGate.is_local_development_context(fallback)
    assert fallback.provenance["trust_boundary"] == "local-development"
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", json.dumps([{
        "credential_id": "similar-fields", "token_sha256": _digest("similar-fields-token"),
        "tenant_id": "local", "subject_id": "local-development",
        "workspace_id": "local-development", "roles": ["local-development"],
    }]))
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "similar-fields-token")
    registry = cli._cli_authorization(agent_id="codex")
    assert registry.principal.tenant_id == fallback.principal.tenant_id
    assert registry.principal.subject_id == fallback.principal.subject_id
    assert registry.workspace_id == fallback.workspace_id
    assert registry.provenance["identity_source"] == "principal-registry"
    assert registry.provenance["transport"] == "cli"
    assert not AuthorizationGate.is_local_development_context(registry)


@pytest.mark.parametrize(
    "profile,registry,token",
    [
        ("production", None, None),
        ("production", _principals(), None),
        ("production", _principals(), "not-in-registry"),
        ("development", _principals(), None),
        ("development", _principals(), "not-in-registry"),
        ("development", _principals(), "cli-token-alice"),
    ],
)
def test_agent_cli_invalid_principal_never_reaches_local_fallback(
    monkeypatch, capsys, profile, registry, token
):
    """A missing/invalid credential or host mismatch cannot select CLI fields."""
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", profile)
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")
    if registry is None:
        monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    else:
        monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", registry)
    if token is None:
        monkeypatch.delenv("MEMPLEX_PRINCIPAL_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", token)
    created = []
    monkeypatch.setattr(cli, "_make_service", lambda _config_path=None: created.append(True))
    assert cli.main([
        "--output", "json", "agent", "capture", "--agent", "codex",
        "--user-id", "forged-user", "--project-path", "/forged-project",
        "--user-message", "Must not persist.", "--assistant-message", "Denied.",
    ]) == 1
    assert created == []
    assert "principal" in capsys.readouterr().err.lower()
