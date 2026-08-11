"""Wave-2 wiring tests: structured Observations through the capture path
and the service / MCP / CLI observation retrieval surfaces."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from memplex.adapters import cli
from memplex.config import MemplexConfig
from memplex.models import Observation
from memplex.service import MemplexService


def _make_service(tmp_path: Path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    return MemplexService(config=cfg)


def _make_runtime(service, **kwargs) -> "object":
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    defaults = {"agent": "codex", "user_id": "user-1", "session_id": "session-1"}
    defaults.update(kwargs)
    return AgentMemoryRuntime(service=service, **defaults)


# ── capture path: Observation nodes with structured categories ────────


def test_capture_turn_persists_categorized_observation(tmp_path):
    service = _make_service(tmp_path / "memory.json")
    runtime = _make_runtime(service)

    runtime.after_response(
        user_message="The login endpoint crashed; I fixed the bug.",
        assistant_message="Confirmed the fix and added a regression test.",
    )

    observations = service.list_observations()
    assert len(observations) == 1
    obs = observations[0]
    assert obs.event == "agent_turn"
    assert obs.category == "bugfix"
    assert obs.actor == "codex"
    # Namespace stamping matches the rest of the capture path.
    assert obs.owner == "user-1"
    assert obs.origin_session == "session-1"
    assert obs.observed_at is not None
    assert "fixed the bug" in obs.context


def test_capture_turn_tool_name_takes_category_priority(tmp_path):
    service = _make_service(tmp_path / "memory.json")
    runtime = _make_runtime(service)

    # Neutral text would classify as "note"; the Edit tool forces "change".
    runtime.after_response(
        user_message="[Edit] updated settings.yaml",
        assistant_message="Observed Claude Code tool use.",
        metadata={"tool_name": "Edit", "tool_input": {"file_path": "settings.yaml"}},
    )

    observations = service.list_observations()
    assert len(observations) == 1
    assert observations[0].category == "change"


def test_capture_turn_bash_error_observation_classifies_bugfix(tmp_path):
    service = _make_service(tmp_path / "memory.json")
    runtime = _make_runtime(service)

    runtime.after_response(
        user_message="[Bash] pytest failed with a traceback",
        assistant_message="Observed Claude Code tool use.",
        metadata={"tool_name": "Bash"},
    )

    observations = service.list_observations(category="bugfix")
    assert len(observations) == 1


def test_capture_observation_never_breaks_after_response(tmp_path, monkeypatch, caplog):
    """A store without add_observation must not interrupt the hook contract."""
    service = _make_service(tmp_path / "memory.json")
    runtime = _make_runtime(service)

    monkeypatch.delattr(type(service.store), "add_observation", raising=False)
    monkeypatch.setattr(
        type(service.store),
        "add_observation",
        lambda self, obs: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False,
    )

    with caplog.at_level(logging.WARNING, logger="memplex.adapters.agent_runtime"):
        runtime.after_response(
            user_message="I prefer concise Chinese status updates.",
            assistant_message="Understood.",
        )
    assert "observation capture skipped: boom" in caplog.text
    # The extraction pipeline still captured the turn despite the
    # add_observation failure above.
    recalled = runtime.before_prompt("How should status updates be written?")
    assert "concise Chinese status updates" in recalled.context


# ── LLM observation compression on the capture path ───────────────────


class _RecordingEnhancer:
    """Fake LLMEnhancer: records compress_observation calls."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail = fail

    async def compress_observation(self, content: str, max_length: int = 500) -> str:
        self.calls.append((content, max_length))
        if self.fail:
            raise RuntimeError("llm down")
        return "compressed: turn summary"


def test_long_turn_is_compressed_via_llm(tmp_path, monkeypatch):
    service = _make_service(tmp_path / "memory.json")
    fake = _RecordingEnhancer()
    monkeypatch.setattr(service, "_llm", fake)
    runtime = _make_runtime(service)

    long_text = "step completed successfully. " * 100  # > 500 chars
    runtime.after_response(user_message=long_text, assistant_message="noted.")

    assert fake.calls, "compress_observation should be invoked for long content"
    observations = service.list_observations()
    assert observations[0].context == "compressed: turn summary"


def test_compression_failure_falls_back_and_capture_continues(tmp_path, monkeypatch):
    service = _make_service(tmp_path / "memory.json")
    fake = _RecordingEnhancer(fail=True)
    monkeypatch.setattr(service, "_llm", fake)
    runtime = _make_runtime(service)

    long_text = "worked on the release checklist. " * 100
    runtime.after_response(user_message=long_text, assistant_message="done.")

    assert fake.calls, "compression was attempted"
    observations = service.list_observations()
    assert len(observations) == 1
    # Rule-based head/tail truncation kept the capture bounded.
    assert len(observations[0].context) <= 500


def test_short_turn_is_not_compressed(tmp_path, monkeypatch):
    service = _make_service(tmp_path / "memory.json")
    fake = _RecordingEnhancer()
    monkeypatch.setattr(service, "_llm", fake)
    runtime = _make_runtime(service)

    runtime.after_response(user_message="hi", assistant_message="hello")

    assert fake.calls == []
    observations = service.list_observations()
    assert "hi" in observations[0].context


# ── service.list_observations namespace filtering ─────────────────────


def test_service_list_observations_owner_and_category_filters(tmp_path):
    service = _make_service(tmp_path / "memory.json")
    for obs_id, owner, category in (
        ("obs_a", "user-1", "bugfix"),
        ("obs_b", "user-1", "decision"),
        ("obs_c", "user-2", "bugfix"),
    ):
        service.store.add_observation(
            Observation(id=obs_id, event=obs_id, category=category, owner=owner)
        )

    assert {o.id for o in service.list_observations()} == {"obs_a", "obs_b", "obs_c"}
    assert {o.id for o in service.list_observations(owner="user-1")} == {"obs_a", "obs_b"}
    assert {o.id for o in service.list_observations(category="bugfix")} == {"obs_a", "obs_c"}
    filtered = service.list_observations(owner="user-1", category="bugfix")
    assert [o.id for o in filtered] == ["obs_a"]


# ── MCP memory_observations tool ──────────────────────────────────────


def _make_mcp_server(tmp_path):
    from memplex.adapters.mcp_server import MCPServer

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    server = MCPServer(config=cfg)
    server._ensure_service()
    return server


def _add_scoped_mcp_observation(server, observation: Observation) -> None:
    runtime = server._agent_runtime({})
    observation.owner = runtime.user_id
    observation.origin_session = runtime.session_id
    observation.namespace = runtime._namespace_metadata()
    server._service.store.add_observation(observation)


def test_memory_observations_tool_registered():
    from memplex.adapters.mcp_server import _TOOL_DEFINITIONS, MCPServer

    names = {t["name"] for t in _TOOL_DEFINITIONS}
    assert "memory_observations" in names
    assert "memory_observations" in MCPServer._tool_handlers


def test_memory_observations_tool_lists_with_est_tokens(tmp_path):
    server = _make_mcp_server(tmp_path / "memory.json")
    _add_scoped_mcp_observation(
        server,
        Observation(
            id="obs_1",
            event="agent_turn",
            context="User: fixed the crash\nAssistant: verified",
            category="bugfix",
            actor="codex",
        ),
    )
    _add_scoped_mcp_observation(
        server,
        Observation(id="obs_2", event="agent_turn", context="chose sqlite", category="decision"),
    )

    payload = server._tool_memory_observations({})
    assert payload["total"] == 2
    item = next(i for i in payload["observations"] if i["id"] == "obs_1")
    assert item["category"] == "bugfix"
    assert item["event"] == "agent_turn"
    assert item["est_tokens"] >= 1
    assert "fixed the crash" in item["summary"]


def test_memory_observations_tool_category_and_query_filters(tmp_path):
    server = _make_mcp_server(tmp_path / "memory.json")
    _add_scoped_mcp_observation(
        server,
        Observation(id="obs_1", event="agent_turn", context="fixed the crash", category="bugfix"),
    )
    _add_scoped_mcp_observation(
        server,
        Observation(id="obs_2", event="agent_turn", context="chose sqlite", category="decision"),
    )

    by_category = server._tool_memory_observations({"category": "decision"})
    assert [i["id"] for i in by_category["observations"]] == ["obs_2"]

    by_query = server._tool_memory_observations({"query": "crash"})
    assert [i["id"] for i in by_query["observations"]] == ["obs_1"]


def test_memory_observations_query_filter_applies_before_limit(tmp_path):
    """The query substring filter must run before limit truncation:
    matches beyond the first `limit` store rows must not be dropped."""
    server = _make_mcp_server(tmp_path / "memory.json")
    for i in range(3):
        _add_scoped_mcp_observation(
            server,
            Observation(
                id=f"obs_skip_{i}",
                event="agent_turn",
                context="unrelated chatter",
                category="note",
            ),
        )
    for i in range(2):
        _add_scoped_mcp_observation(
            server,
            Observation(
                id=f"obs_hit_{i}",
                event="agent_turn",
                context="fixed the crash",
                category="bugfix",
            ),
        )

    payload = server._tool_memory_observations({"query": "crash", "limit": 2})
    assert [i["id"] for i in payload["observations"]] == ["obs_hit_0", "obs_hit_1"]
    assert payload["total"] == 2


# ── CLI observations subcommand ───────────────────────────────────────


def test_cmd_observations_json_output(tmp_path, monkeypatch, capsys):
    service = _make_service(tmp_path / "memory.json")
    service.store.add_observation(
        Observation(id="obs_1", event="agent_turn", context="fixed the crash", category="bugfix")
    )
    service.store.add_observation(
        Observation(id="obs_2", event="agent_turn", context="chose sqlite", category="decision")
    )
    monkeypatch.setattr(cli, "_make_service", lambda _config_path=None: service)

    rc = cli.cmd_observations(SimpleNamespace(output="json", config=None, category=None, limit=100))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 2
    first = payload["observations"][0]
    assert first["category"] in {"bugfix", "decision"}
    assert first["est_tokens"] >= 1

    rc = cli.cmd_observations(
        SimpleNamespace(output="json", config=None, category="bugfix", limit=100)
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert [o["id"] for o in payload["observations"]] == ["obs_1"]


def test_cmd_observations_table_output_shows_category_and_tokens(tmp_path, monkeypatch, capsys):
    service = _make_service(tmp_path / "memory.json")
    service.store.add_observation(
        Observation(id="obs_1", event="agent_turn", context="fixed the crash", category="bugfix")
    )
    monkeypatch.setattr(cli, "_make_service", lambda _config_path=None: service)

    rc = cli.cmd_observations(
        SimpleNamespace(output="table", config=None, category=None, limit=100)
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "category: bugfix" in out
    assert "est_tokens:" in out


def test_observations_subcommand_dispatches():
    parser = cli.build_parser()
    args = parser.parse_args(["observations", "--category", "note", "--limit", "5"])
    assert args.command == "observations"
    assert args.category == "note"
    assert args.limit == 5


# ── agent recall token exposure ───────────────────────────────────────


def test_agent_recall_output_exposes_est_tokens_and_tokens_used(tmp_path):
    service = _make_service(tmp_path / "memory.json")
    runtime = _make_runtime(service)
    runtime.after_response(
        user_message="I prefer concise Chinese status updates.",
        assistant_message="Understood.",
    )

    recalled = runtime.before_prompt("How should status updates be written?")

    assert recalled.context
    assert recalled.est_tokens == len(recalled.context) // 4 + 1
    assert recalled.tokens_used >= 0
    # The CLI prints recalled.__dict__; both fields must be present there.
    assert "est_tokens" in recalled.__dict__
    assert "tokens_used" in recalled.__dict__


# ── wiki observation page category ────────────────────────────────────


def test_compile_observation_page_includes_category(tmp_path):
    from memplex.wiki.compiler import WikiCompiler

    class _StubStore:
        def list_observations(self, limit=100):
            return []

    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    obs = Observation(id="obs_1", event="deploy failed", category="bugfix")
    page = compiler.compile_observation(obs)
    assert "**Category:** bugfix" in page.content


# ── hook-runner copies stay in sync ───────────────────────────────────


def test_hook_runner_copies_are_byte_identical():
    project_root = Path(__file__).resolve().parent.parent
    source = project_root / "plugin" / "scripts" / "hook-runner.py"
    packaged = project_root / "memplex" / "_plugin" / "scripts" / "hook-runner.py"
    assert source.read_bytes() == packaged.read_bytes()
    # cmd_observation must forward tool_name into after_response metadata so
    # the capture path can classify with tool-name priority.
    text = source.read_text(encoding="utf-8")
    assert '"tool_name": tool_name' in text
