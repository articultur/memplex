"""Cross-host visibility contract tests for ``AgentMemoryRuntime``."""

import json
from pathlib import Path

import pytest

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.config import MemplexConfig
from memplex.models import QueryResult, QueryScope
from memplex.service import MemplexService


def _make_service(tmp_path: Path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "memory.json")
    return MemplexService(config=cfg)


def test_model_controlled_search_budget_and_fanout_are_hard_capped(tmp_path, monkeypatch):
    """A model request must not turn runtime over-fetch into unbounded work."""

    service = _make_service(tmp_path)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="bounded-session",
        project_path=tmp_path,
    )
    calls = []

    def query(**kwargs):
        calls.append(kwargs)
        return QueryResult(
            results=[],
            scope=QueryScope.IMMEDIATE,
            latency_ms=0,
            max_tokens=kwargs["max_tokens"],
        )

    monkeypatch.setattr(service, "query", query)

    runtime.search_memories("bounded", top_k=1_000_000, max_tokens=10_000_000)
    runtime.search_memories("bounded", top_k=1, max_tokens=0)

    assert calls[0]["top_k"] == 500
    assert calls[0]["max_tokens"] == 32_000
    assert calls[1]["top_k"] == 21
    assert calls[1]["max_tokens"] == 1


def test_workspace_memory_is_shared_across_agents_and_sessions(tmp_path):
    """Removing workspace sharing or restoring agent/session read gates must fail."""

    project = tmp_path / "workspace"
    project.mkdir()
    service = _make_service(tmp_path)
    claude = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=project,
    )
    codex = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="codex-session",
        project_path=project,
    )

    claude.after_response(
        user_message="Remember cross-host-workspace-token for this project.",
        assistant_message="Captured.",
    )

    recalled = codex.before_prompt("cross-host-workspace-token")

    assert "cross-host-workspace-token" in recalled.context


def test_session_memory_stays_private_to_its_source_agent_and_session(tmp_path):
    """Dropping either source-agent or source-session checks must leak this token."""

    project = tmp_path / "workspace"
    project.mkdir()
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="private-session",
        project_path=project,
    )
    same_session = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="private-session",
        project_path=project,
    )
    other_session = AgentMemoryRuntime(
        service=service,
        agent="hermes",
        user_id="alice",
        session_id="other-session",
        project_path=project,
    )

    writer.after_response(
        user_message="Remember session-private-token only in this conversation.",
        assistant_message="Captured.",
        metadata={"memplex_visibility": "session"},
    )

    assert "session-private-token" in same_session.before_prompt("session-private-token").context
    assert (
        "session-private-token" not in other_session.before_prompt("session-private-token").context
    )


def test_user_memory_is_shared_across_workspaces_for_the_same_user(tmp_path):
    """Keeping the project gate on user-visible memory must hide this token."""

    project_a = tmp_path / "workspace-a"
    project_b = tmp_path / "workspace-b"
    project_a.mkdir()
    project_b.mkdir()
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="openclaw",
        user_id="alice",
        session_id="openclaw-session",
        project_path=project_a,
    )
    reader = AgentMemoryRuntime(
        service=service,
        agent="hermes",
        user_id="alice",
        session_id="hermes-session",
        project_path=project_b,
    )

    writer.after_response(
        user_message="Remember user-wide-interoperability-token for Alice.",
        assistant_message="Captured.",
        metadata={"memplex_visibility": "user"},
    )

    assert (
        "user-wide-interoperability-token"
        in reader.before_prompt("user-wide-interoperability-token").context
    )


def test_workspace_identity_canonicalizes_symlinked_project_paths(tmp_path):
    """Comparing raw project-path spellings must split one physical workspace."""

    project = tmp_path / "workspace"
    project.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(project, target_is_directory=True)
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=project,
    )
    reader = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="codex-session",
        project_path=alias,
    )

    writer.after_response(
        user_message="Remember canonical-workspace-token through either path.",
        assistant_message="Captured.",
    )

    assert "canonical-workspace-token" in reader.before_prompt("canonical-workspace-token").context


def test_out_of_workspace_recall_does_not_touch_access_counters(tmp_path):
    """Filtering only after ``service.query`` must not mutate hidden memories."""

    project_a = tmp_path / "workspace-a"
    project_b = tmp_path / "workspace-b"
    project_a.mkdir()
    project_b.mkdir()
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=project_a,
    )
    reader = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="codex-session",
        project_path=project_b,
    )
    writer.after_response(
        user_message="Remember untouched-workspace-counter-token in workspace A.",
        assistant_message="Captured.",
    )
    stored = service.store.list_functions(limit=1, owner="alice")[0]
    before = stored.access_count

    recalled = reader.before_prompt("untouched-workspace-counter-token")

    assert "untouched-workspace-counter-token" not in recalled.context
    assert service.store.get(stored.id).access_count == before


def test_legacy_agent_session_namespace_remains_readable_in_its_original_context(tmp_path):
    """Requiring only the new visibility fields must not orphan old captures."""

    project = tmp_path / "workspace"
    project.mkdir()
    service = _make_service(tmp_path)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=project,
    )
    runtime.after_response(
        user_message="Remember legacy-namespace-compatibility-token.",
        assistant_message="Captured.",
    )
    stored = service.store.list_functions(limit=1, owner="alice")[0]
    stored.attributes = {
        "memplex_agent": "claude-code",
        "memplex_user_id": "alice",
        "memplex_session_id": "legacy-session",
        "memplex_project_path": str(project.resolve()),
        "memplex_storage_namespace": service.storage_namespace(),
    }
    service.store.replace_function(stored)

    recalled = runtime.before_prompt("legacy-namespace-compatibility-token")

    assert "legacy-namespace-compatibility-token" in recalled.context


def test_typed_preference_respects_workspace_visibility(tmp_path):
    """Treating typed-node namespace keys as unverifiable must not leak projects."""

    project_a = tmp_path / "workspace-a"
    project_b = tmp_path / "workspace-b"
    project_a.mkdir()
    project_b.mkdir()
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=project_a,
    )
    writer.after_response(
        user_message="I prefer typed-workspace-isolation-token responses.",
        assistant_message="Captured.",
    )
    assert service.store.list_preferences(owner="alice")
    reloaded_service = _make_service(tmp_path)
    reader = AgentMemoryRuntime(
        service=reloaded_service,
        agent="codex",
        user_id="alice",
        session_id="codex-session",
        project_path=project_b,
    )

    recalled = reader.before_prompt("typed-workspace-isolation-token")

    assert "typed-workspace-isolation-token" not in recalled.context


def test_typed_preference_is_shared_across_agents_in_one_workspace(tmp_path):
    """Losing typed-node namespace persistence must hide this shared preference."""

    project = tmp_path / "workspace"
    project.mkdir()
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=project,
    )
    writer.after_response(
        user_message="I prefer typed-cross-host-sharing-token responses.",
        assistant_message="Captured.",
    )
    reloaded_service = _make_service(tmp_path)
    reader = AgentMemoryRuntime(
        service=reloaded_service,
        agent="hermes",
        user_id="alice",
        session_id="hermes-session",
        project_path=project,
    )

    recalled = reader.before_prompt("typed-cross-host-sharing-token")

    assert "typed-cross-host-sharing-token" in recalled.context


def test_legacy_typed_preference_remains_visible_in_its_original_session(tmp_path, caplog):
    """A legacy typed node is recalled once, then locked to that workspace."""

    project = tmp_path / "workspace"
    other_project = tmp_path / "other-workspace"
    project.mkdir()
    other_project.mkdir()
    service = _make_service(tmp_path)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=project,
    )
    runtime.after_response(
        user_message="I prefer legacy-typed-compatibility-token responses.",
        assistant_message="Captured.",
    )
    stored = service.store.list_preferences(owner="alice")[0]
    stored.namespace = {}
    service.store.add_preference(stored)
    reloaded_service = _make_service(tmp_path)
    reader = AgentMemoryRuntime(
        service=reloaded_service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=project,
    )

    recalled = reader.before_prompt("legacy-typed-compatibility-token")

    assert "legacy-typed-compatibility-token" in recalled.context
    assert "legacy typed memory accepted without workspace provenance" in caplog.text
    migrated = reloaded_service.get(stored.id)
    assert migrated.namespace["memplex_visibility"] == "workspace"
    assert migrated.namespace["memplex_workspace_id"] == str(project.resolve())
    assert migrated.namespace["memplex_storage_namespace"] == service.storage_namespace()

    other_reader = AgentMemoryRuntime(
        service=reloaded_service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=other_project,
    )
    isolated = other_reader.before_prompt("legacy-typed-compatibility-token")
    assert "legacy-typed-compatibility-token" not in isolated.context


def test_legacy_typed_migration_failure_fails_closed(tmp_path, monkeypatch, caplog):
    """A compatibility read must not escape before workspace provenance persists."""

    project = tmp_path / "workspace"
    project.mkdir()
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=project,
    )
    writer.after_response(
        user_message="I prefer legacy-migration-failure-token responses.",
        assistant_message="Captured.",
    )
    stored = service.store.list_preferences(owner="alice")[0]
    stored.namespace = {}
    service.store.add_preference(stored)

    reloaded_service = _make_service(tmp_path)
    reader = AgentMemoryRuntime(
        service=reloaded_service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=project,
    )

    def fail_migration(*_args, **_kwargs):
        raise RuntimeError("namespace persistence unavailable")

    monkeypatch.setattr(reloaded_service, "annotate_memories", fail_migration)

    recalled = reader.before_prompt("legacy-migration-failure-token")

    assert "legacy-migration-failure-token" not in recalled.context
    assert reloaded_service.get(stored.id).namespace == {}
    assert "legacy typed memory migration failed closed" in caplog.text


def test_legacy_typed_migration_failure_redacts_search_explanation(
    tmp_path, monkeypatch
):
    """Denied compatibility records must not survive in the public trace."""

    project = tmp_path / "workspace"
    project.mkdir()
    service = _make_service(tmp_path)
    writer = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=project,
    )
    token = "legacy-explanation-failure-token"
    writer.after_response(
        user_message=f"I prefer {token} responses.",
        assistant_message="Captured.",
    )
    stored = service.store.list_preferences(owner="alice")[0]
    stored.namespace = {}
    service.store.add_preference(stored)

    reloaded_service = _make_service(tmp_path)
    reader = AgentMemoryRuntime(
        service=reloaded_service,
        agent="claude-code",
        user_id="alice",
        session_id="legacy-session",
        project_path=project,
    )

    def fail_migration(*_args, **_kwargs):
        raise RuntimeError("namespace persistence unavailable")

    monkeypatch.setattr(reloaded_service, "annotate_memories", fail_migration)

    result = reader.search_memories(token, top_k=10, explain=True)

    assert result.results == []
    assert result.tokens_used == 0
    assert result.explanation["results"] == []
    serialized = json.dumps(result.explanation, sort_keys=True)
    assert stored.id not in serialized


def test_invalid_visibility_is_rejected_before_any_memory_is_written(tmp_path):
    """Validating after ``write_text`` must not leave an unscoped orphan behind."""

    project = tmp_path / "workspace"
    project.mkdir()
    service = _make_service(tmp_path)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="codex-session",
        project_path=project,
    )

    with pytest.raises(ValueError, match="Unsupported memory visibility"):
        runtime.after_response(
            user_message="I prefer invalid-visibility-orphan-token responses.",
            assistant_message="Captured.",
            metadata={"memplex_visibility": "team"},
        )

    assert service.store.list_functions(limit=100) == []
    assert service.store.list_preferences(limit=100) == []
