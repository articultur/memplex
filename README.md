# Memplex

Memplex is a **single-machine, multi-agent** long-term memory layer for local
AI agents. It gives Codex, Claude Code, OpenClaw, Hermes, and similar local
agents on **the same host** the same closed loop: recall useful memory before
a turn, capture what happened after the turn, and compact old context.

> **Scope note (read before relying on this).** Memory is stored locally in
> `~/.memplex/memory.json` (or a project-local path). Memplex does **not**
> sync across machines, networks, or remote users out of the box -- "shared"
> here means *shared between the agents on one machine*, not multi-device or
> multi-site replication. Remote/shared backends are on the roadmap (see
> [Scope & Roadmap](#scope--roadmap) below). Background compaction is
> automatic for the Claude Code hook loop and manual (`memplex compact`)
> elsewhere.

## Install

No source checkout is required.

```bash
npx memplex setup
```

Install into a specific agent:

```bash
npx memplex setup --agent codex --project-path "$PWD"
npx memplex setup --agent claude-code --project-path "$PWD"
npx memplex setup --agent openclaw --project-path "$PWD"
npx memplex setup --agent hermes --project-path "$PWD"
```

Install every supported local agent:

```bash
npx memplex setup --agent all --project-path "$PWD"
```

Uninstall is symmetrical:

```bash
npx memplex uninstall --agent all
```

The npm wrapper creates a persistent Python environment at
`~/.local/share/memplex/agent-venv`, installs `memplex==3.2.7`, detects local
agent config directories, and registers Memplex into the selected hosts. It uses
`uv` when available and falls back to `python -m venv` plus `pip`.

Python-first users can skip npm:

```bash
uv tool install memplex==3.2.7
memplex setup --agent all --project-path "$PWD"
```

Shell-only fallback:

```bash
curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | bash
```

## What Gets Installed

| Agent | Integration | Installed shape |
| --- | --- | --- |
| Codex | MCP server | marker-bounded `mcp_servers.memplex` config |
| Claude Code | plugin + lifecycle hooks + MCP | packaged plugin and hook runner |
| OpenClaw | memory plugin slot | `plugins.slots.memory = "memplex"` plus extension files |
| Hermes | memory provider plugin | provider descriptor and `plugins/memory/memplex` |

All installers are reversible and refuse to overwrite unmanaged existing
Memplex entries.

## Verify

List supported profiles:

```bash
memplex --output json agent list
```

Run a local closed-loop smoke:

```bash
memplex --output json agent capture \
  --agent codex \
  --user-message "Use lite storage for local development." \
  --assistant-message "Recorded."

memplex --output json agent recall \
  --agent codex \
  "What storage should local development use?"
```

For a no-write preview:

```bash
npx memplex setup --agent all --project-path "$PWD" --dry-run
```

## Offline And Mainland China

Memplex's default local retrieval uses a SQLite FTS5 sidecar index with
`bm25()` ranking plus generated trigram tokens for Chinese, code symbols,
paths, and short memory fragments. If SQLite FTS5 is unavailable, it falls back
to pure-Python local BM25/trigram matching. The agent hot path does not need
HuggingFace, so `npx memplex setup`, capture, recall, MCP tools, and hooks
continue to work when HuggingFace is blocked or unavailable.

The embedding fallback is also local. To force that path explicitly:

```bash
MEMPLEX_EMBEDDING_MODEL=tfidf memplex query "local memory"
```

To enhance semantic recall with a local model and no network downloads:

```bash
python -m pip install "memplex[local-onnx]"
export MEMPLEX_LOCAL_ONNX_MODEL=/models/bge-small/model.onnx
export MEMPLEX_LOCAL_ONNX_TOKENIZER=/models/bge-small/tokenizer.json
memplex query "local semantic memory"
```

`MEMPLEX_EMBEDDING_MODEL=local-onnx` or
`MEMPLEX_EMBEDDING_MODEL=local-onnx:/models/bge-small/model.onnx` opts in
explicitly and reports configuration errors. With the default auto-enhancement
path, a missing local ONNX runtime or model keeps the SQLite
FTS5/BM25+trigram retrieval path alive and falls back to local embedding.

Opt into HuggingFace only when the environment can reach it or the model is
already cached:

```bash
MEMPLEX_EMBEDDING_MODEL=minilm memplex query "semantic memory"
MEMPLEX_EMBEDDING_MODEL=bge-m3 memplex query "中文记忆"
MEMPLEX_EMBEDDING_MODEL=hf:BAAI/bge-m3 memplex query "中文记忆"
```

If your organization provides an approved HuggingFace mirror, configure it in
the Python/HuggingFace environment before enabling those models. Otherwise keep
the default local retrieval and embedding path for agent reliability.

## Core Features

- **Automatic agent loop**: pre-turn recall, post-turn capture, background
  consolidation.
- **4 memory types**: Function, Fact, Preference, Observation.
- **3-layer retrieval**: SQLite FTS5/BM25+trigram search, timeline, get.
- **5-dim reranking**: raw relevance, semantic similarity, recency, source
  authority, frequency.
- **5-stage compaction**: extract, dedup, summarize, prune, archive.
- **Wiki layer**: full-text/vector retrieval plus graph-aware synthesis.
- **Namespacing**: user, session, project path, and storage path isolation.

## Docs

- [Getting Started](docs/getting-started.md): install, verify, doctor,
  recall explain, scope, inbox, corpus, policy, report, uninstall, and
  troubleshoot.
- [Explainer](docs/explainer.md): what Memplex is and how the memory loop works.
- [Agent Integration Loop](docs/agent-integration.md): adapter contracts for
  Codex, Claude Code, OpenClaw, and Hermes.
- [Release Automation](docs/release-automation.md): npm token handling and
  automated npm publishing.

## From Source

```bash
git clone https://github.com/articultur/memplex.git
cd memplex
pip install -e .
```

## CLI Basics

```bash
memplex write --text "Python list comprehensions are faster than loops"
memplex query "python performance"
memplex compact
memplex health
```

## Storage And Privacy

The default and currently implemented memory backend is a JSON-backed
`LiteMemoryStore` at `~/.memplex/memory.json` (override with
`MEMPLEX_STORAGE_PATH` or `config.yaml`). All data is held in memory and
flushed to JSON on every write. The feedback store has an optional
asyncpg/Postgres backend; the main memory store does not yet have a
remote backend (see Scope & Roadmap).

Content wrapped in `<private>...</private>` is stripped before storage on
every write path (CLI/HTTP/MCP/corpus).

## Scope & Roadmap

What Memplex **is today** (single-machine, multi-agent, local):

- Local recall-before-turn / capture-after-turn loop for Codex, Claude
  Code, OpenClaw, and Hermes on one host.
- Local-first retrieval: SQLite FTS5/BM25 + trigram, pure-Python fallback,
  optional local ONNX/HF embeddings (never required).
- Automatic closed loop for Claude Code (via hooks); MCP tools for Codex
  and others (agent-driven).
- `<private>` redaction and indirect-injection scanning on every write
  path (`write`, `write_text`, `update_memory`).

What Memplex **is not yet** (tracked as roadmap, not currently shipped):

- **Cross-machine / multi-site sharing.** No sync, replication, or remote
  memory backend for the main store. Multiple machines cannot share one
  memory. A remote/shared backend is the largest roadmap item.
- **Remote/enterprise memory backends.** `standard` and `enterprise`
  backends are reserved names; only `lite` is implemented.
- **Scheduled background compaction.** `memplex compact` is manual.
  Claude Code's `Stop` hook triggers compaction automatically; all other
  surfaces require the user/agent to run it.
- **Incremental FTS5 indexing.** The FTS5 sidecar rebuilds fully on the
  first query after any write (cached afterwards). Fine up to ~10k
  memories; incremental upsert is roadmap for larger corpora.

## License

MIT
