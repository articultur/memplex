"""Direct handler tests for memplex/adapters/cli.py.

Invokes cmd_* handlers via types.SimpleNamespace (no subprocess) to cover
the gaps left by tests/test_hooks.py and tests/test_agent_hot_paths.py
(which only exercise a subset via subprocess). Uses monkeypatch on
``_make_service`` to point every service-based handler at a tmp_path
lite store.
"""

import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.adapters import cli  # noqa: E402
from memplex.config import MemplexConfig  # noqa: E402
from memplex.service import MemplexService  # noqa: E402


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A lite service on tmp_path, wired into cli._make_service."""
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)

    def _factory(_config_path=None):
        return svc

    monkeypatch.setattr(cli, "_make_service", _factory)
    yield svc
    svc.stop()


def _ns(**kw):
    """Build a SimpleNamespace with sensible CLI defaults."""
    defaults = {"output": "json", "config": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _out(capsys):
    return capsys.readouterr().out


# ── write + query round trip ─────────────────────────────────────────


def test_cmd_write_text_then_query(service, capsys):
    rc = cli.cmd_write(_ns(text="cli-handler-canary: a recorded fact."))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["function_ids"] or payload.get("functions")

    rc = cli.cmd_query(_ns(text="cli-handler-canary", top_k=5, max_tokens=4000, explain=False))
    assert rc == 0
    q = json.loads(_out(capsys))
    assert q["total"] >= 1


def test_cmd_query_explain_emits_explanation(service, capsys):
    cli.cmd_write(_ns(text="explain-canary: structured memory."))
    capsys.readouterr()  # discard write output
    rc = cli.cmd_query(_ns(text="explain-canary", top_k=5, max_tokens=4000, explain=True))
    assert rc == 0
    q = json.loads(_out(capsys))
    assert q["explanation"] is not None
    assert q["explanation"]["schema_version"] == 1


# ── token cost exposure (progressive disclosure) ─────────────────────


def test_cmd_query_exposes_token_costs(service, capsys):
    """Each result is annotated with est_tokens and the summary line
    carries tokens_used / max_tokens / truncated."""
    cli.cmd_write(_ns(text="token-cost-canary: tokens are first-class product info."))
    capsys.readouterr()  # discard write output
    rc = cli.cmd_query(_ns(text="token-cost-canary", top_k=5, max_tokens=4000, explain=False))
    assert rc == 0
    q = json.loads(_out(capsys))
    assert q["total"] >= 1
    assert q["tokens_used"] >= 1
    assert q["max_tokens"] == 4000
    assert q["truncated"] is False
    assert all(item["est_tokens"] >= 1 for item in q["results"])
    # Per-result estimates sum to the reported budget usage.
    assert sum(item["est_tokens"] for item in q["results"]) == q["tokens_used"]


def test_cmd_query_table_mode_shows_token_costs(service, capsys):
    cli.cmd_write(_ns(text="token-table-canary: table mode shows token costs."))
    capsys.readouterr()  # discard write output
    rc = cli.cmd_query(
        _ns(output="table", text="token-table-canary", top_k=5, max_tokens=4000, explain=False)
    )
    assert rc == 0
    out = _out(capsys)
    assert "tokens_used" in out
    assert "max_tokens" in out
    assert "truncated" in out
    assert "est_tokens" in out


def test_cmd_write_with_no_content_errors(service, capsys):
    """No --text/--file/--url -> stderr + rc=1."""
    rc = cli.cmd_write(_ns(text=None, file=None, url=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert err  # something printed to stderr


# ── get / delete ─────────────────────────────────────────────────────


def test_cmd_get_existing_memory(service, capsys):
    cli.cmd_write(_ns(text="get-canary: fetch me"))
    capsys.readouterr()  # discard write output
    funcs = service.store.list_functions(limit=100)
    fid = funcs[0].id
    rc = cli.cmd_get(_ns(memory_id=fid))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["id"] == fid


def test_cmd_get_missing_returns_error_code(service, capsys):
    rc = cli.cmd_get(_ns(memory_id="does-not-exist"))
    assert rc == 1


def test_cmd_delete_removes_memory(service, capsys):
    cli.cmd_write(_ns(text="delete-canary: temp"))
    capsys.readouterr()  # discard write output
    fid = service.store.list_functions(limit=1)[0].id
    rc = cli.cmd_delete(_ns(memory_id=fid))
    assert rc == 0
    assert service.store.get(fid) is None


# ── feedback / pending ───────────────────────────────────────────────


def test_cmd_feedback_then_pending(service, capsys):
    cli.cmd_write(_ns(text="feedback-canary: review me"))
    capsys.readouterr()  # discard write output
    fid = service.store.list_functions(limit=1)[0].id
    rc = cli.cmd_feedback(_ns(memory_id=fid, role="trigger", index=0, verdict="wrong"))
    assert rc == 0
    capsys.readouterr()  # discard feedback output
    rc = cli.cmd_pending(_ns())
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["total"] >= 1


# ── compact ──────────────────────────────────────────────────────────


def test_cmd_compact_runs(service, capsys):
    rc = cli.cmd_compact(_ns(scope="project"))
    assert rc == 0


# ── health / stats ───────────────────────────────────────────────────


def test_cmd_health(service, capsys):
    service.start()
    try:
        rc = cli.cmd_health(_ns())
        assert rc == 0
        payload = json.loads(_out(capsys))
        assert payload["backend"] == "lite"
        assert "functions_total" in payload
    finally:
        service.stop()


def test_cmd_health_strict_exit_codes(service, capsys, monkeypatch):
    """--strict exits 1 unless status is healthy; default keeps warning -> 0."""
    monkeypatch.setattr(service, "health", lambda: {"status": "warning", "backend": "lite"})
    assert cli.cmd_health(_ns()) == 0
    _out(capsys)
    assert cli.cmd_health(_ns(strict=True)) == 1
    _out(capsys)

    monkeypatch.setattr(service, "health", lambda: {"status": "healthy", "backend": "lite"})
    assert cli.cmd_health(_ns(strict=True)) == 0
    _out(capsys)

    monkeypatch.setattr(service, "health", lambda: {"status": "error", "backend": "lite"})
    assert cli.cmd_health(_ns()) == 1
    _out(capsys)


def test_cmd_stats(service, capsys):
    rc = cli.cmd_stats(_ns())
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["storage_backend"] == "lite"


# ── doctor / policy / report ─────────────────────────────────────────


def test_cmd_doctor(service, capsys):
    rc = cli.cmd_doctor(_ns(agent="codex", profile=None, smoke=False, fix=False))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["status"] in {"pass", "fail"}
    assert any(c["name"] == "service_health" for c in payload["checks"])


def test_cmd_policy(service, capsys):
    rc = cli.cmd_policy(_ns(agent="codex"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["agent"] == "codex"
    assert "embedding" in payload


def test_cmd_report(service, capsys):
    rc = cli.cmd_report(_ns(agent="codex"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert "health" in payload
    assert "lifecycle" in payload


# ── scope ────────────────────────────────────────────────────────────


def test_cmd_scope_list(service, capsys):
    rc = cli.cmd_scope(_ns(scope_command="list"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert "scopes" in payload


def test_cmd_scope_explain(service, capsys):
    rc = cli.cmd_scope(
        _ns(
            scope_command="explain",
            agent="codex",
            user_id="alice",
            session_id="s1",
            project_path=None,
        )
    )
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["namespace_filter"]["memplex_user_id"] == "alice"


def test_cmd_scope_preview(service, capsys):
    rc = cli.cmd_scope(
        _ns(
            scope_command="preview",
            agent="codex",
            user_id=None,
            session_id="default",
            project_path=None,
            limit=5,
        )
    )
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["boundary"].startswith("Preview only")
    assert "total_functions" not in payload
    assert payload["scanned_functions"] >= 0
    assert payload["matched_in_scan"] >= 0


# ── inbox ────────────────────────────────────────────────────────────


def test_cmd_inbox_list(service, capsys):
    cli.cmd_write(_ns(text="inbox-canary: for review"))
    capsys.readouterr()  # discard write output
    fid = service.store.list_functions(limit=1)[0].id
    cli.cmd_feedback(_ns(memory_id=fid, role="trigger", index=0, verdict="wrong"))
    capsys.readouterr()  # discard feedback output
    rc = cli.cmd_inbox(_ns(inbox_command="list", limit=100))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["total"] >= 1


def test_cmd_inbox_show_missing(service, capsys):
    rc = cli.cmd_inbox(_ns(inbox_command="show", memory_id="nope"))
    assert rc == 1


# ── corpus ───────────────────────────────────────────────────────────


def test_cmd_corpus_preview(tmp_path, capsys):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "guide.md").write_text("corpus-canary: canonical doc.", encoding="utf-8")
    manifest = root / "m.toml"
    manifest.write_text(
        "\n".join(["[corpus]", 'name = "docs"', 'include = ["*.md"]']),
        encoding="utf-8",
    )
    rc = cli.cmd_corpus(_ns(corpus_command="preview", manifest=str(manifest), limit=10))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["included_count"] >= 1


def test_cmd_corpus_index_then_recall(service, tmp_path, capsys):
    """corpus index writes canonical memories; corpus recall finds them."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "guide.md").write_text("corpus-recall-canary: canonical doc.", encoding="utf-8")
    manifest = root / "m.toml"
    manifest.write_text(
        "\n".join(["[corpus]", 'name = "docs"', 'include = ["*.md"]']),
        encoding="utf-8",
    )
    # Index
    rc = cli.cmd_corpus(_ns(corpus_command="index", manifest=str(manifest), dry_run=False))
    assert rc == 0
    capsys.readouterr()  # discard index output
    # Recall
    rc = cli.cmd_corpus(
        _ns(corpus_command="recall", query="corpus-recall-canary", top_k=5, max_tokens=4000)
    )
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["total"] >= 1


def test_cmd_corpus_index_dry_run(service, tmp_path, capsys):
    """Dry-run index previews without writing."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "g.md").write_text("dry-run-canary content", encoding="utf-8")
    manifest = root / "m.toml"
    manifest.write_text("\n".join(["[corpus]", 'name="d"', 'include=["*.md"]']), encoding="utf-8")
    rc = cli.cmd_corpus(_ns(corpus_command="index", manifest=str(manifest), dry_run=True))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["status"] == "dry_run"


# ── agent list / manifest (no service needed) ────────────────────────


def test_cmd_agent_list(capsys):
    rc = cli.cmd_agent(_ns(agent_command="list"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert isinstance(payload, dict)
    assert "codex" in payload


def test_cmd_agent_manifest(capsys):
    rc = cli.cmd_agent(_ns(agent_command="manifest", agent="codex"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["name"] == "codex"


def test_cmd_agent_manifest_all(capsys):
    """Regression: --agent all was advertised in the help text but crashed
    with ValueError from get_agent_manifest('all')."""
    rc = cli.cmd_agent(_ns(agent_command="manifest", agent="all"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert {"codex", "claude-code", "openclaw", "hermes"} <= set(payload)


# ── table output mode smoke ──────────────────────────────────────────


def test_cmd_stats_table_mode(service, capsys):
    rc = cli.cmd_stats(_ns(output="table"))
    assert rc == 0
    out = _out(capsys)
    assert "storage_backend" in out


# ── build_parser exercises every subcommand registration ─────────────


def test_build_parser_registers_all_subcommands():
    """Calling build_parser() executes every _add_*_parsers helper, covering
    the parser-construction code that handlers never reach. Asserts the full
    command surface exists so a broken/missing subparser is caught."""
    parser = cli.build_parser()
    # Parse '--help'-style introspection: collect registered subcommands.
    help_text = parser.format_help()
    expected_commands = [
        "query",
        "recall",
        "write",
        "get",
        "delete",
        "feedback",
        "pending",
        "compact",
        "health",
        "stats",
        "doctor",
        "scope",
        "policy",
        "inbox",
        "corpus",
        "report",
        "agent",
        "setup",
        "install",
        "stepup",
        "uninstall",
        "unsetup",
        "sync",
        "benchmark",
    ]
    for cmd in expected_commands:
        assert cmd in help_text, f"subcommand {cmd!r} missing from parser help"


def test_build_parser_global_options_present():
    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "--config" in help_text
    assert "--output" in help_text


def test_build_parser_query_accepts_explain_flag():
    """The query subparser must wire --explain (regression for the explain
    surface that several other tests depend on)."""
    args = cli.build_parser().parse_args(["query", "x", "--explain", "--top-k", "3"])
    assert args.explain is True
    assert args.top_k == 3
    assert args.text == "x"


def test_build_parser_corpus_subcommands():
    args = cli.build_parser().parse_args(["corpus", "preview", "--manifest", "m.toml"])
    assert args.corpus_command == "preview"
    assert args.manifest == "m.toml"


def test_build_parser_agent_subcommands():
    args = cli.build_parser().parse_args(["agent", "list"])
    assert args.agent_command == "list"


def test_build_parser_sync_subcommands():
    args = cli.build_parser().parse_args(["sync", "pull"])
    assert args.sync_command == "pull"
    args = cli.build_parser().parse_args(["sync", "status"])
    assert args.sync_command == "status"
    args = cli.build_parser().parse_args(["sync", "drain", "--timeout", "2"])
    assert args.sync_command == "drain"
    assert args.timeout == 2
    args = cli.build_parser().parse_args(
        ["sync", "dlq", "replay", "--target", "remote-a", "--event-id", "event-a"]
    )
    assert args.dlq_command == "replay"


def test_cmd_sync_status_reports_disabled_without_remote(service, capsys):
    """Without MEMPLEX_REMOTE_URL, sync status clearly says disabled."""
    rc = cli.cmd_sync(_ns(sync_command="status"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["status"] == "disabled"
    assert "MEMPLEX_REMOTE_URL" in payload["reason"]


def test_cmd_sync_pull_reports_disabled_without_remote(service, capsys):
    rc = cli.cmd_sync(_ns(sync_command="pull"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["status"] == "disabled"


def test_cmd_sync_status_uses_durable_dispatcher_without_transport_details(
    service, capsys
):
    from memplex.sync_protocol import SyncStatus

    service._sync_dispatcher = SimpleNamespace(
        running=False,
        status=lambda: SyncStatus(2, 1, 3, 0, 4),
    )

    rc = cli.cmd_sync(_ns(sync_command="status"))

    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload == {
        "status": "active",
        "pending": 2,
        "leased": 1,
        "delivered": 3,
        "disabled_targets": 0,
        "dead_letters": 4,
    }
    assert "url" not in json.dumps(payload).lower()


def test_cmd_legacy_sync_status_never_echoes_remote_url(service, capsys, monkeypatch):
    from memplex.sync import RemoteSyncConfig, SyncableStore

    monkeypatch.setenv("MEMPLEX_REMOTE_URL", "https://sync.example.test/private")
    service.store = SyncableStore(service.store, config=RemoteSyncConfig())

    rc = cli.cmd_sync(_ns(sync_command="status"))

    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["status"] == "active"
    assert payload["remote_configured"] is True
    assert payload["target_count"] == 1
    assert "url" not in json.dumps(payload).lower()
    assert "sync.example.test" not in json.dumps(payload)


def test_cmd_sync_drain_and_dlq_replay_use_stable_target_identity(
    service, capsys
):
    from memplex.sync_protocol import SyncDrainResult, SyncStatus
    from memplex.sync_repository import SyncDeadLetterEntry

    calls = []

    class Dispatcher:
        running = False

        def drain(self, deadline):
            calls.append(("drain", deadline))
            return SyncDrainResult(True, 1, 0, 0, 0, False)

        def stop(self, deadline):
            calls.append(("stop", deadline))
            return SyncDrainResult(True, 1, 0, 0, 0, False)

        def replay(self, target_id, event_id):
            calls.append(("replay", target_id, event_id))
            return True

        def list_dead_letters(self, *, limit):
            calls.append(("list", limit))
            return [
                SyncDeadLetterEntry(
                    "remote-a",
                    "123e4567-e89b-42d3-a456-426614174000",
                    2,
                    "remote_batch_rejected",
                )
            ]

        def status(self):
            return SyncStatus(0, 0, 1, 0, 0)

    service._sync_dispatcher = Dispatcher()
    assert cli.cmd_sync(_ns(sync_command="drain", timeout=2.0)) == 0
    assert json.loads(_out(capsys))["drained"] is True
    assert ("drain", 2.0) in calls

    # Use a fresh lifecycle state because each CLI invocation owns and closes
    # its service in production.
    service._service_stop_state = "open"
    service._service_stop_result = None
    assert (
        cli.cmd_sync(
            _ns(sync_command="dlq", dlq_command="list", limit=5)
        )
        == 0
    )
    assert json.loads(_out(capsys))["items"][0]["error_code"] == (
        "remote_batch_rejected"
    )
    assert ("list", 5) in calls

    service._service_stop_state = "open"
    service._service_stop_result = None
    assert (
        cli.cmd_sync(
            _ns(
                sync_command="dlq",
                dlq_command="replay",
                target="remote-a",
                event_id="123e4567-e89b-42d3-a456-426614174000",
            )
        )
        == 0
    )
    payload = json.loads(_out(capsys))
    assert payload["replayed"] is True
    assert (
        "replay",
        "remote-a",
        "123e4567-e89b-42d3-a456-426614174000",
    ) in calls


# ── benchmark (lazy import of the source-only benchmarks package) ────


def test_build_parser_benchmark_subcommands():
    args = cli.build_parser().parse_args(["benchmark", "list"])
    assert args.benchmark_command == "list"
    args = cli.build_parser().parse_args(
        [
            "benchmark",
            "run",
            "--dataset",
            "locomo",
            "--synthetic",
            "--top-k",
            "5",
            "--output",
            "out.jsonl",
        ]
    )
    assert args.benchmark_command == "run"
    assert args.dataset == "locomo"
    assert args.synthetic is True
    assert args.top_k == 5
    assert args.benchmark_output == "out.jsonl"


def test_benchmark_run_output_flag_does_not_clash_with_global_output():
    """`benchmark run --output PATH` uses its own dest so the global
    `--output json|table` format flag keeps working on the same namespace."""
    args = cli.build_parser().parse_args(
        ["--output", "json", "benchmark", "run", "--dataset", "locomo"]
    )
    assert args.output == "json"  # global format flag survives
    assert args.benchmark_output == ".memplex/benchmarks/results.jsonl"


def test_cmd_benchmark_list(capsys):
    rc = cli.cmd_benchmark(_ns(benchmark_command="list"))
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["total"] >= 1
    assert "locomo" in payload["datasets"]
    assert "memory_benchmark" in payload["datasets"]


def test_cmd_benchmark_run_passes_args(monkeypatch, capsys):
    """cmd_benchmark must forward CLI flags to run_benchmark_command."""
    import benchmarks.benchmark_cli as bench_cli
    from benchmarks.base import BenchmarkResult

    captured = {}

    def _fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "locomo": [
                BenchmarkResult(
                    name="b", dataset="locomo", metric="mrr", value=0.5,
                    latency_ms=0, samples=1,
                )
            ]
        }

    monkeypatch.setattr(bench_cli, "run_benchmark_command", _fake_run)
    rc = cli.cmd_benchmark(
        _ns(
            benchmark_command="run",
            dataset="locomo",
            synthetic=True,
            top_k=7,
            benchmark_output="results/my.jsonl",
        )
    )
    assert rc == 0
    assert captured["dataset"] == "locomo"
    assert captured["retrieval_k"] == 7
    assert captured["output"] == "results/my.jsonl"
    assert captured["force_synthetic"] is True
    assert captured["path"] is None
    payload = json.loads(_out(capsys))
    assert payload["status"] == "ok"
    assert payload["output"] == "results/my.jsonl"
    assert payload["results"]["locomo"]["mrr"] == 0.5


def test_cmd_benchmark_run_defaults(monkeypatch, capsys):
    import benchmarks.benchmark_cli as bench_cli

    captured = {}
    monkeypatch.setattr(
        bench_cli, "run_benchmark_command", lambda **kw: captured.update(kw) or {}
    )
    rc = cli.cmd_benchmark(
        _ns(
            benchmark_command="run",
            dataset="all",
            synthetic=False,
            top_k=10,
            benchmark_output=".memplex/benchmarks/results.jsonl",
        )
    )
    assert rc == 0
    assert captured["force_synthetic"] is False
    assert captured["output"] == ".memplex/benchmarks/results.jsonl"


def test_cmd_benchmark_missing_package_reports_source_only(monkeypatch, capsys):
    """Without the source-only benchmarks package: clear stderr + rc=1."""
    monkeypatch.setitem(sys.modules, "benchmarks", None)
    monkeypatch.setitem(sys.modules, "benchmarks.base", None)
    rc = cli.cmd_benchmark(_ns(benchmark_command="list"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "仅源码可用" in err
    assert "source checkout" in err


def test_cmd_benchmark_unknown_action(capsys):
    rc = cli.cmd_benchmark(_ns(benchmark_command="bogus"))
    assert rc == 1


# ── setup profile wiring ──────────────────────────────────────────────


def test_cmd_setup_applies_profile(service, capsys):
    """cmd_setup must call apply_profile and merge applied/declarative
    into the output (previously the profile was only displayed)."""
    rc = cli.cmd_setup(
        _ns(
            command="setup",
            agent="codex",
            profile="max-recall",
            dry_run=True,
            uninstall=False,
            target_dir=None,
            user_id=None,
            project_path=None,
        )
    )
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert payload["profile"]["name"] == "max-recall"
    assert payload["applied"]["retrieval.default_max_tokens"] == 4000
    assert payload["declarative"]["recommended_top_k"] == 10
    assert "result" in payload


def test_cmd_setup_without_profile_keeps_plain_output(service, capsys):
    rc = cli.cmd_setup(
        _ns(
            command="setup",
            agent="codex",
            profile=None,
            dry_run=True,
            uninstall=False,
            target_dir=None,
            user_id=None,
            project_path=None,
        )
    )
    assert rc == 0
    payload = json.loads(_out(capsys))
    assert "profile" not in payload
    assert "applied" not in payload


# ── exit codes / top-level error surface ─────────────────────────────


def test_main_unknown_command_is_usage_error_2(capsys):
    """argparse usage errors exit 2 (argparse's own convention)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["no-such-command"])
    assert excinfo.value.code == 2


def test_main_handler_error_prints_one_line_without_verbose(monkeypatch, capsys):
    """Runtime errors: rc=1 with a single-line message, no traceback."""

    def _boom(_args):
        raise RuntimeError("cli-main-quiet-canary")

    monkeypatch.setattr(cli, "cmd_health", _boom)
    monkeypatch.delenv("MEMPLEX_DEBUG", raising=False)
    rc = cli.main(["health"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cli-main-quiet-canary" in err
    assert "Traceback" not in err


def test_main_handler_error_prints_traceback_with_verbose(monkeypatch, capsys):
    """--verbose surfaces the full traceback for the same failure."""

    def _boom(_args):
        raise RuntimeError("cli-main-verbose-canary")

    monkeypatch.setattr(cli, "cmd_health", _boom)
    monkeypatch.delenv("MEMPLEX_DEBUG", raising=False)
    rc = cli.main(["--verbose", "health"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "cli-main-verbose-canary" in err


def test_main_handler_error_prints_traceback_with_memplex_debug(monkeypatch, capsys):
    """MEMPLEX_DEBUG is the env equivalent of --verbose."""

    def _boom(_args):
        raise RuntimeError("cli-main-debug-canary")

    monkeypatch.setattr(cli, "cmd_health", _boom)
    monkeypatch.setenv("MEMPLEX_DEBUG", "1")
    rc = cli.main(["health"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "cli-main-debug-canary" in err


def test_stepup_help_marks_it_as_install_alias(capsys):
    """The stepup alias stays, but --help must say what it is."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0
    out = _out(capsys)
    assert "stepup" in out
    assert "Alias for 'install'" in out
