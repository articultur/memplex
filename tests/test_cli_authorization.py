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
