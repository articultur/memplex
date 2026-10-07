"""Offline guards for the opt-in, real-provider memory lifecycle runner."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="module")
def runner():
    path = Path(__file__).resolve().parents[1] / "scripts/run_live_memory_e2e.py"
    assert path.exists(), "The bounded live E2E runner must exist"
    spec = importlib.util.spec_from_file_location("memplex_live_runner", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def response(*, text="UNKNOWN", model="glm-5.3", stop_reason="end_turn"):
    return {"model": model, "stop_reason": stop_reason,
            "content": [{"type": "thinking", "thinking": "not an answer"},
                        {"type": "text", "text": text}],
            "usage": {"input_tokens": 42, "output_tokens": 12}}


def test_transport_fixed_destination_model_and_no_secret_in_trace(runner):
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, json=response())

    client = runner.LiveModel("private-test-value", transport=httpx.MockTransport(reply))
    assert client.complete("reader", "Neutral query") == "UNKNOWN"
    request, = seen
    assert str(request.url) == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    body = json.loads(request.content)
    assert body["model"] == "glm-5.3"
    assert body["max_tokens"] == runner.MAX_OUTPUT_TOKENS
    assert body.get("thinking", {}).get("type") != "disabled"
    assert body["messages"] == [{"role": "user", "content": "Neutral query"}]
    assert "private-test-value" not in json.dumps(client.trace)
    assert "not an answer" not in json.dumps(client.trace)


@pytest.mark.parametrize("payload", [
    response(text=""), response(model="different-model"),
    response(stop_reason="max_tokens"), {"content": [], "usage": {}},
])
def test_transport_rejects_incomplete_or_wrong_model_without_retry(runner, payload):
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    client = runner.LiveModel("private-test-value", transport=httpx.MockTransport(reply))
    with pytest.raises(runner.RunError):
        client.complete("reader", "Neutral query")
    assert len(seen) == 1


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_transport_fails_closed_and_does_not_expose_error_body(runner, status):
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(status, text="private-test-value", headers={"location": "https://example.com"})

    client = runner.LiveModel("private-test-value", transport=httpx.MockTransport(reply))
    with pytest.raises(runner.RunError) as error:
        client.complete("reader", "Neutral query")
    assert len(seen) == 1
    assert "private-test-value" not in str(error.value)
    assert "private-test-value" not in json.dumps(client.trace)


def test_budget_rejects_an_extra_call_before_transport(runner):
    seen = []

    def reply(request):
        seen.append(request)
        return httpx.Response(200, json=response())

    client = runner.LiveModel("test-value", transport=httpx.MockTransport(reply))
    client.calls = runner.MAX_CALLS
    with pytest.raises(runner.RunError, match="call_budget"):
        client.complete("reader", "Neutral query")
    assert seen == []


def test_prompt_limit_rejects_before_transport(runner):
    client = runner.LiveModel("test-value", transport=httpx.MockTransport(lambda _: pytest.fail("network")))
    with pytest.raises(runner.RunError, match="prompt_budget"):
        client.complete("reader", "x" * (runner.MAX_PROMPT_CHARS + 1))


def test_reader_prompt_contains_only_question_and_retrieved_context(runner):
    prompt = runner.reader_prompt("Which parcel code?", "untrusted context")
    assert "Which parcel code?" in prompt
    assert "untrusted context" in prompt
    assert "UNKNOWN" in prompt
    assert "expected" not in prompt.lower()


def test_child_env_excludes_ambient_credentials(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_LIVE_API_KEY", "private-test-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-test-value")
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", "/production")
    env = runner.child_env(tmp_path, runner.Scope())
    assert all("private-test-value" not in value for value in env.values())
    assert env["MEMPLEX_STORAGE_PATH"].startswith(str(tmp_path))
    assert env["MEMPLEX_LLM_PROVIDER"] == "rule-based"


def test_offline_full_protocol_uses_real_mcp_and_blind_fresh_readers(runner, tmp_path):
    class OfflineBoundary:
        def __init__(self):
            self.trace = []
            self.calls = 0

        def complete(self, phase, prompt):
            self.calls += 1
            if phase == "capture":
                text = "ACK"
            else:
                context = prompt.split("MEMORY CONTEXT:\n", 1)[1]
                question = prompt.split("QUESTION:\n", 1)[1].split("\nMEMORY CONTEXT:", 1)[0]
                subject = next(word for word in ("Atlas", "Kestrel", "Juniper") if word in question)
                relevant = "\n".join(line for line in context.splitlines() if subject in line)
                markers = re.findall(r"MX[A-Z]+-[A-F0-9]{12}", relevant)
                text = markers[0] if markers else "UNKNOWN"
            self.trace.append({"phase": phase, "prompt": prompt, "text": text})
            return text

    model = OfflineBoundary()
    report = runner.run_suite(tmp_path, model)
    assert report["status"] == "passed", report["cases"]
    assert len(report["cases"]) == 12
    assert model.calls == runner.MAX_CALLS == 17
    assert all(row["passed"] for row in report["cases"])
    assert all("expected" not in row["prompt"].lower()
               for row in model.trace if row["phase"] == "reader")


def test_correct_model_answer_cannot_mask_stale_context(runner, tmp_path):
    suite = runner.Suite(tmp_path, None)
    suite.case("stale", "old-value new-value", "new-value", "new-value", forbidden=("old-value",))
    suite.case("readback", "new-value", "new-value", "new-value", checks=(False,))
    assert not any(row["passed"] for row in suite.cases)


def test_manual_workflow_guards_and_secret_step_scope():
    import yaml

    path = Path(__file__).resolve().parents[1] / ".github/workflows/live-memory-e2e.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert list(workflow["on"]) == ["workflow_dispatch"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["live-memory"]
    for guard in ("github.actor == github.repository_owner", "github.triggering_actor == github.repository_owner",
                  "inputs.reviewed_sha == github.sha", "inputs.confirm_live"):
        assert guard in job["if"]
    secret_steps = [step for step in job["steps"] if "MEMPLEX_LIVE_API_KEY" in json.dumps(step)]
    assert len(secret_steps) == 1
    assert secret_steps[0]["name"] == "Run bounded synthetic live cases"
    assert secret_steps[0]["env"]["MEMPLEX_LIVE_API_KEY"] == "${{ secrets.MEMPLEX_LIVE_API_KEY }}"
    assert job["steps"][0]["with"]["persist-credentials"] == "false"
    assert job["steps"][-1]["with"]["path"] == "artifacts/live-memory-e2e.json"


def test_reviewed_commit_rejects_tracked_and_untracked_changes(runner, tmp_path):
    import subprocess

    assert hasattr(runner, "verified_commit"), "Review must bind the actual clean worktree"

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True, stderr=subprocess.DEVNULL).strip()

    git("init")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Synthetic Fixture")
    script = tmp_path / "scripts/run_live_memory_e2e.py"
    script.parent.mkdir()
    script.write_text("approved fixture\n")
    git("add", ".")
    git("commit", "-m", "test: create synthetic review fixture")
    sha = git("rev-parse", "HEAD")
    assert runner.verified_commit(sha, tmp_path) == sha
    with pytest.raises(runner.RunError, match="reviewed_sha"):
        runner.verified_commit("0" * 40, tmp_path)
    script.write_text("unreviewed change\n")
    with pytest.raises(runner.RunError, match="dirty_worktree"):
        runner.verified_commit(sha, tmp_path)
    script.write_text("approved fixture\n")
    (tmp_path / "unreviewed.py").write_text("new code\n")
    with pytest.raises(runner.RunError, match="dirty_worktree"):
        runner.verified_commit(sha, tmp_path)


def test_rejected_completion_keeps_valid_billing_usage(runner):
    client = runner.LiveModel("test-value", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=response(stop_reason="max_tokens")),
    ))
    with pytest.raises(runner.RunError):
        client.complete("reader", "Neutral query")
    assert client.trace[0].get("usage") == {"input_tokens": 42, "output_tokens": 12}
