"""Real MCP capture must recall content, without promoting the transport envelope."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _stdio(tmp_path, calls, *, session="first", user="alice", workspace="project"):
    """Each invocation is a new server, with only synthetic local identity/state."""
    home = tmp_path / "home"
    project = tmp_path / workspace
    home.mkdir(exist_ok=True)
    project.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(tmp_path / "memory"),
        "MEMPLEX_EMBEDDING_MODEL": "tfidf",
        "MEMPLEX_LLM_PROVIDER": "rule-based",
        "MEMPLEX_LLM_FALLBACK_CHAIN": "rule-based",
        "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
        "MEMPLEX_LLM_OBSERVATION_COMPRESSION": "false",
        "MEMPLEX_LLM_FACTUAL_CAPTURE": "false",
        "MEMPLEX_EMBEDDING_HYDE_ENABLED": "false",
        "MEMPLEX_SYNC_ENABLED": "false",
        "MEMPLEX_SLEEP_TIME_ENABLED": "false",
        "MEMPLEX_WIKI_ENABLED": "false",
        "MEMPLEX_AGENT_ID": "codex",
        "MEMPLEX_USER_ID": user,
        "MEMPLEX_SESSION_ID": session,
        "MEMPLEX_PROJECT_ROOT": str(project),
    }
    # Windows needs these OS paths, never the caller's Memplex/credential settings.
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    messages = [{"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 0}]
    messages.extend(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
            "id": index,
        }
        for index, (name, args) in enumerate(calls, start=1)
    )
    result = subprocess.run(
        [sys.executable, "-m", "memplex.adapters.mcp_server"],
        input="\n".join(json.dumps(message) for message in messages) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=project,
        timeout=30,
        check=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [response["id"] for response in responses] == list(range(len(messages)))
    parsed = []
    for response in responses[1:]:
        assert "error" not in response, response
        assert not response["result"].get("isError"), response
        parsed.append(json.loads(response["result"]["content"][0]["text"]))
    return parsed


def _capture(user, assistant="Recorded.", **metadata):
    return ("memory_turn_end", {
        "user_message": user,
        "assistant_message": assistant,
        "metadata": metadata,
    })


def _recall(prompt="What is the Atlas parcel routing code?"):
    return ("memory_turn_begin", {"prompt": prompt})


def test_captured_fact_is_recalled_immediately_over_stdio(tmp_path):
    _, recalled = _stdio(tmp_path, [
        _capture("The Atlas parcel routing code is AMBER-7382."),
        _recall(),
    ])

    assert "AMBER-7382" in recalled["context"]
    assert "trust=LOW" in recalled["context"]
    assert "Observation from agent conversation" not in recalled["context"]


def test_captured_fact_survives_restart_and_fresh_session(tmp_path):
    _stdio(tmp_path, [_capture("The Atlas parcel routing code is AMBER-7382.")])

    recalled, = _stdio(tmp_path, [_recall()], session="fresh")

    assert "AMBER-7382" in recalled["context"]


def test_distinct_captured_facts_and_speakers_do_not_replace_each_other(tmp_path):
    _stdio(tmp_path, [
        _capture(
            "The Atlas parcel routing code is AMBER-7382.",
            "The Cedar storage label is VIOLET-9264.",
        ),
        _capture("The Birch meeting label is COPPER-4156."),
    ])

    atlas, cedar, birch = _stdio(tmp_path, [
        _recall(),
        _recall("What is the Cedar storage label?"),
        _recall("What is the Birch meeting label?"),
    ], session="fresh")

    assert "AMBER-7382" in atlas["context"]
    assert "VIOLET-9264" in cedar["context"]
    assert "COPPER-4156" in birch["context"]


@pytest.mark.parametrize("other", [
    {"session": "other"},
    {"workspace": "other"},
    {"user": "bob"},
])
def test_session_capture_is_not_recalled_outside_its_scope(tmp_path, other):
    _stdio(tmp_path, [_capture(
        "The Atlas parcel routing code is AMBER-7382.",
        memplex_visibility="session",
    )])
    own, = _stdio(tmp_path, [_recall()])
    inaccessible, = _stdio(tmp_path, [_recall()], **other)

    assert "AMBER-7382" in own["context"]
    assert "AMBER-7382" not in inaccessible["context"]


def test_captured_fact_update_and_delete_are_authoritative_after_restart(tmp_path):
    _stdio(tmp_path, [_capture("The Atlas parcel routing code is AMBER-7382.")])
    _, recalled, facts = _stdio(tmp_path, [
        _capture("The Atlas parcel routing code is INDIGO-6528."),
        _recall(),
        ("memory_facts", {}),
    ])
    assert "INDIGO-6528" in recalled["context"]
    assert "AMBER-7382" not in recalled["context"]
    current = next(fact for fact in facts["facts"] if fact["object"] == "INDIGO-6528")

    _stdio(tmp_path, [("memory_delete", {"memory_id": current["id"]})])
    deleted, = _stdio(tmp_path, [_recall()], session="fresh")

    assert "INDIGO-6528" not in deleted["context"]
    assert "AMBER-7382" not in deleted["context"]


def test_capture_metadata_does_not_become_a_recalled_fact(tmp_path):
    _stdio(tmp_path, [_capture(
        "The Atlas parcel routing code is AMBER-7382.",
        note="The secret metadata label is ENVELOPE-8391.",
    )])
    recalled, = _stdio(tmp_path, [_recall("What is the secret metadata label?")])

    assert "ENVELOPE-8391" not in recalled["context"]


@pytest.mark.parametrize("other", [
    {"session": "other"},
    {"workspace": "other"},
    {"user": "bob"},
])
@pytest.mark.parametrize("second_code", ["AMBER-7382", "INDIGO-6528"])
def test_capture_in_another_scope_cannot_overwrite_or_invalidate_first(
    tmp_path, other, second_code,
):
    _stdio(tmp_path, [_capture(
        "The Atlas parcel routing code is AMBER-7382.",
        memplex_visibility="session",
    )])
    _stdio(tmp_path, [_capture(
        f"The Atlas parcel routing code is {second_code}.",
        memplex_visibility="session",
    )], **other)

    first, = _stdio(tmp_path, [_recall()])
    second, = _stdio(tmp_path, [_recall()], **other)

    assert "AMBER-7382" in first["context"]
    assert "INDIGO-6528" not in first["context"]
    assert second_code in second["context"]


def test_session_capture_cannot_replace_workspace_capture(tmp_path):
    _stdio(tmp_path, [_capture("The Atlas parcel routing code is AMBER-7382.")])
    _stdio(tmp_path, [_capture(
        "The Atlas parcel routing code is INDIGO-6528.",
        memplex_visibility="session",
    )])

    fresh, = _stdio(tmp_path, [_recall()], session="fresh")

    assert "AMBER-7382" in fresh["context"]
    assert "INDIGO-6528" not in fresh["context"]


@pytest.mark.parametrize("text", [
    "Remember the SAPPHIRE-4821 workspace marker.",
    "I prefer SAPPHIRE-4821 status updates.",
])
@pytest.mark.parametrize("other", [
    {"session": "other"},
    {"workspace": "other"},
    {"user": "bob"},
])
def test_identical_nonfact_captures_keep_both_scopes(tmp_path, text, other):
    capture = _capture(text, memplex_visibility="session")
    _stdio(tmp_path, [capture])
    _stdio(tmp_path, [capture], **other)

    first, = _stdio(tmp_path, [_recall("What is the SAPPHIRE marker preference?")])
    second, = _stdio(tmp_path, [_recall("What is the SAPPHIRE marker preference?")], **other)

    assert "SAPPHIRE-4821" in first["context"]
    assert "SAPPHIRE-4821" in second["context"]


def test_direct_same_scope_correction_supersedes_captured_fact(tmp_path):
    _stdio(tmp_path, [_capture("The Atlas parcel routing code is AMBER-7382.")])
    _, recalled = _stdio(tmp_path, [
        ("memory_add", {"content": "The Atlas parcel routing code is INDIGO-6528."}),
        _recall(),
    ])

    assert "INDIGO-6528" in recalled["context"]
    assert "AMBER-7382" not in recalled["context"]


@pytest.mark.parametrize("capture_first", [True, False])
def test_captured_and_direct_facts_cannot_invalidate_another_user(tmp_path, capture_first):
    first = ("memory_add", {"content": "The Atlas parcel routing code is AMBER-7382."})
    second = ("memory_add", {"content": "The Atlas parcel routing code is INDIGO-6528."})
    if capture_first:
        first = _capture("The Atlas parcel routing code is AMBER-7382.")
    else:
        second = _capture("The Atlas parcel routing code is INDIGO-6528.")
    _stdio(tmp_path, [first])
    _stdio(tmp_path, [second], user="bob")

    alice, = _stdio(tmp_path, [_recall()])
    bob, = _stdio(tmp_path, [_recall()], user="bob")

    assert "AMBER-7382" in alice["context"]
    assert "INDIGO-6528" not in alice["context"]
    assert "INDIGO-6528" in bob["context"]
    assert "AMBER-7382" not in bob["context"]


def test_capture_retains_structured_observation_separately(tmp_path):
    user = "The Atlas parcel routing code is AMBER-7382."
    assistant = "The Cedar storage label is VIOLET-9264."
    _, observations = _stdio(tmp_path, [
        _capture(user, assistant),
        ("memory_observations", {}),
    ])

    assert observations["total"] == 1
    observation = observations["observations"][0]
    assert observation["actor"] == "codex"
    assert observation["event"] == "agent_turn"
    assert observation["summary"] == f"User: {user}\nAssistant: {assistant}"
