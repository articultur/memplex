# Memplex

Memplex is a **multi-agent** long-term memory layer for AI agents. It gives
Codex, Claude Code, OpenClaw, Hermes, and similar agents the same closed
loop: recall useful memory before a turn, capture what happened after the
turn, and compact old context.

By default Memplex runs **single-machine** (memory in `~/.memplex/memory.json`).
Multi-machine sharing is **opt-in**: point one or more nodes at a central
Memplex HTTP server with `MEMPLEX_REMOTE_URL`, and memories sync
(write-push + on-demand pull) across machines. See
[Multi-Node Sharing](#multi-node-sharing) below.

Background compaction is automatic for the Claude Code hook loop and manual
(`memplex compact`) elsewhere.

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

## Multi-Node Sharing

Memplex can share one memory pool across multiple machines. The model is
**central server + local cache**: one host runs the Memplex HTTP API as
the source of truth; every other node keeps a local cache and pushes
writes / pulls updates on demand.

Quick start:

```bash
# On the central host (the server):
MEMPLEX_API_KEY=shared-secret \
  uvicorn 'memplex.adapters.http_api:app' --host 0.0.0.0 --port 8900

# On each node (the clients):
export MEMPLEX_REMOTE_URL=http://central.host:8900
export MEMPLEX_REMOTE_API_KEY=shared-secret
memplex sync pull      # fetch the latest from the server
memplex write --text "note from this machine"   # auto-pushes to server
memplex sync status    # show remote config + last-pull timestamp
```

How it works:

- **Writes are local-first.** `write` / `write_text` / `update_memory`
  commit to the local cache, then best-effort push to the server's
  `/sync/push`. If the server is unreachable the local write still
  succeeds (offline-capable); the push is retried implicitly on the next
  write.
- **Reads stay local** (fast, offline). To see the latest from other
  nodes, run `memplex sync pull` (or call `store.pull_incremental()`)
  before querying. Pull applies **last-write-wins** by `updated_at` and
  replicates deletions via tombstones.
- **No real-time subscription.** Sync is pull-on-demand by design --
  exactly the "fetch when needed" shape. Background auto-pull is roadmap.
- **Auth** reuses the existing `MEMPLEX_API_KEY` / `MEMPLEX_BEARER_TOKEN`
  (constant-time compared on the server).

Conflict policy: last-write-wins by `updated_at`. For memory-style data
(append-mostly, rare concurrent edits of the same record) this is
sufficient; richer CRDT-style merge is roadmap.

## Agent Loop Automation

The recall/capture/compact loop runs with different levels of automation
depending on the agent:

| Agent | Automation | How |
|---|---|---|
| **Claude Code** | Fully automatic | Hooks fire on UserPromptSubmit (recall), Stop (capture+compact), PostToolUse (observation). Zero user action needed after `setup`. |
| **Codex** | Agent-driven | MCP server registered with `memory_turn_begin` / `memory_turn_end` tools. The agent calls them per turn; they are NOT auto-fired by Codex. |
| **OpenClaw** | Hook-dependent | Extension hook files installed; auto-fires if OpenClaw invokes its hook contract. |
| **Hermes** | Provider-driven | `sync_turn` / `prefetch` in the memory provider plugin; auto-fires if Hermes calls them. |

For Codex/OpenClaw/Hermes, the agent runtime must cooperate by calling the
provided hooks/tools. If your agent does not call them automatically,
consider setting `MEMPLEX_SYNC_PULL_INTERVAL` for periodic background sync.

## Embedding Dimension Notes

The default embedding model (`MEMPLEX_EMBEDDING_MODEL=default`) uses a
local TF-IDF embedder whose vector dimension tracks the vocabulary size,
capped at the configured `dimension` (default 384). For most use cases
this is sufficient. If you switch to a sentence-transformers model
(`minilm`, `bge-m3`, `hf:...`), the dimension must match the model's
output size (e.g. `minilm` = 384, `bge-m3` = 1024). Set
`MEMPLEX_EMBEDDING_DIMENSION` to match.

## Scope & Roadmap

What Memplex **is today**:

- Local recall-before-turn / capture-after-turn loop for Codex, Claude
  Code, OpenClaw, and Hermes.
- Local-first retrieval: SQLite FTS5/BM25 + trigram (incremental upsert),
  pure-Python fallback, optional local ONNX/HF embeddings (never required).
- Automatic closed loop for Claude Code (via hooks); MCP tools for Codex
  and others (agent-driven).
- **Multi-machine sharing** via central server + local cache
  (`MEMPLEX_REMOTE_URL`, opt-in, LWW sync), **or P2P mesh** between peer
  nodes (`MEMPLEX_PEERS=url1,url2,...`). Optional background auto-pull
  via `MEMPLEX_SYNC_PULL_INTERVAL`.
- **Multiple storage backends**: `lite` (JSON, default) and `postgres`
  (JSONB + native tsvector full-text search, requires `memplex[postgres]`).
  The Postgres backend optionally supports **pgvector semantic search**
  (hybrid tsv + vector cosine via RRF) when `MEMPLEX_PGVECTOR_DIM` is set.
- **Scheduled background compaction**: writes trigger compaction when the
  corpus crosses `warn_threshold` (configurable); no longer manual-only.
- **Incremental FTS5 indexing**: the SQLite sidecar upserts/deletes only
  changed rows after the first build (O(changes) per write, not O(N)).
- `<private>` redaction and indirect-injection scanning on every write
  path (`write`, `write_text`, `update_memory`).

The original roadmap (native Postgres, scheduled compaction, incremental
FTS5, auto-pull, pgvector, P2P mesh) is now fully shipped. There is no
outstanding roadmap -- future work will be tracked in GitHub issues.

## License

MIT
