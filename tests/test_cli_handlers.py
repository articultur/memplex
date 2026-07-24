"""Direct handler tests for memplex/adapters/cli.py.

Invokes cmd_* handlers via types.SimpleNamespace (no subprocess) to cover
the gaps left by tests/test_hooks.py and tests/test_agent_hot_paths.py
(which only exercise a subset via subprocess). Uses monkeypatch on
``_make_service`` to point every service-based handler at a tmp_path
lite store.
"""

import json
import os
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
    cfg.llm.semantic_extraction = False
    cfg.llm.query_enhancement = False
    cfg.llm.conflict_resolution = False
    cfg.llm.summarization = False
    cfg.llm.reranking = False
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


# ── table output mode smoke ──────────────────────────────────────────


def test_cmd_stats_table_mode(service, capsys):
    rc = cli.cmd_stats(_ns(output="table"))
    assert rc == 0
    out = _out(capsys)
    assert "storage_backend" in out
