# Memplex Explainer

Memplex is a memory system for local AI agents. It is built for agents that do
real work over many sessions and need a durable way to remember decisions,
preferences, project facts, and observations without asking the user to type
manual memory entries each time.

## The Problem

Most coding agents can read the current prompt and files, but they forget useful
context once a session ends. Users then repeat the same instructions:

- which project conventions matter;
- which decisions were already made;
- which local tools or paths are important;
- which preferences should be applied automatically;
- what happened in earlier agent runs.

Memplex turns those repeated facts into a shared memory layer that multiple
agents can consume.

## The Closed Loop

Memplex is designed around a production-to-consumption loop:

1. **Recall before a turn**
   The agent asks Memplex for relevant context before the model responds.

2. **Capture after a turn**
   The agent sends the completed user/assistant turn to Memplex. Memplex stores
   it as observation input and extracts structured memory.

3. **Consolidate in the background**
   Compaction deduplicates, summarizes, prunes, archives, and prepares memory
   for future retrieval.

The user should not need to manually record every memory. Agent adapters own the
hot path.

## What Memplex Stores

Memplex stores four memory types:

| Type | Meaning | Example |
| --- | --- | --- |
| Function | procedural knowledge | "When publishing, build from a clean archive." |
| Fact | declarative project knowledge | "The package name is `memplex`." |
| Preference | user or agent preference | "Use concise Chinese status updates." |
| Observation | runtime event | "Codex installed Memplex into temp config and MCP initialized." |

These are extracted from text, files, URLs, and agent conversations through the
same service boundary.

## Retrieval Model

Memplex retrieves memory in layers:

- **Search** finds candidate memories.
- **Rerank** scores results with relevance, semantic similarity, recency, source
  authority, and access frequency.
- **Get** fetches exact memory details only when needed.
- **Injection guard** wraps recalled context as data before an agent uses it.

This keeps prompt context smaller than dumping all memory into every turn.

## Supported Agent Shapes

Memplex supports several common local-agent integration styles:

| Agent | Shape | Memory path |
| --- | --- | --- |
| Codex | MCP server config | pre-turn tools and portable MCP memory calls |
| Claude Code | plugin, hooks, MCP | lifecycle recall, observation capture, packaged plugin |
| OpenClaw | memory plugin slot | triage, recall, dream-style memory lifecycle |
| Hermes | memory provider plugin | sync after response and optional next-turn prefetch |

The adapters use one shared runtime in `memplex.adapters.agent_runtime`, so the
behavior is consistent across hosts.

## Namespacing

Agent memory is scoped by:

- user id;
- session id;
- project path;
- storage path;
- agent id.

This prevents one user, project, or temporary store from accidentally consuming
another namespace's memory.

## Installation Philosophy

Memplex should be installable without cloning the repository:

```bash
npx memplex setup
```

The npm command installs the Python runtime into a persistent venv and writes
host-specific config. For Python-first users:

```bash
uv tool install memplex==3.2.7
memplex setup
```

Both paths lead to the same installer and the same agent runtime.

## Safety Model

Memplex installers are intended to be reversible:

- generated config uses managed markers or managed metadata;
- unmanaged existing `memplex` entries are refused instead of overwritten;
- `memplex uninstall` removes only Memplex-managed entries;
- dry-run mode shows planned files and commands before writing.

Runtime safety focuses on namespace isolation, private-tag filtering, and
wrapping recalled memory as untrusted data before prompt injection.

## Where To Go Next

- Use [Getting Started](getting-started.md) for installation and verification.
- Use [Agent Integration Loop](agent-integration.md) when adding or reviewing an
  adapter.
