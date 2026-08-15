# Memplex

Memplex is a **multi-agent** long-term memory layer for AI agents. It gives
Codex, Claude Code, OpenClaw, Hermes, and similar agents the same closed
loop: recall useful memory before a turn, capture what happened after the
turn, and compact old context.

By default Memplex runs **single-machine** (`~/.memplex/memory.json`)；默认本地配置仍是
**single-machine Beta / Developer Preview**，不等同 HA 或工业级生产部署。Memplex 已具备
证据门控的工业部署能力：只有生产 PostgreSQL 拓扑逐项提交并通过全部当前机器门禁时，
`readiness --strict` 才会报告 `ready / industrial`。生产支持合同和逐项机器门禁见
[`docs/production-readiness.md`](docs/production-readiness.md)，可运行
`memplex --output json readiness --strict` 检查；未为当前部署提交完整、有效证据时仍为
`not_ready`，不得把仓库测试通过等同于部署已就绪。
生产租户隔离以统一 `principal` 为边界：server registry 只保存 `token_sha256`，客户端
原始 secret 仅使用 `MEMPLEX_PRINCIPAL_TOKEN`；详见
[`docs/production-readiness.md`](docs/production-readiness.md)。迁移、可靠 outbox、灾备、
SLO、供应链、四宿主 E2E 与容量故障注入均有独立证据门，任一缺失都会保持 fail-closed。
Multi-machine sharing is **opt-in**: point one or more nodes at a central
Memplex HTTP server with `MEMPLEX_REMOTE_URL`, and memories sync
(write-push + on-demand pull) across machines. See
[Multi-Node Sharing](#multi-node-sharing) below.

Working-memory tier (opt-in): with `MEMPLEX_WORKING_MEMORY_ENABLED=true`,
recent typed captures live in a TTL hot-context store and are prepended to
every agent recall (`[WORKING MEMORY]` prefix) before retrieval runs.

Bi-temporal fact history (Zep-style): contradicted facts are stamped
`invalid_at` and retained — `memplex agent`-scoped listing supports `as_of`
point-in-time queries. `memplex improve` runs proactive maintenance
(dedupe/expire/reindex), and opt-in sleep-time compute
(`MEMPLEX_SLEEP_TIME_ENABLED=true`) reruns it during idle windows while
precomputing `[SLEEP-TIME]` association inferences into the working-memory
tier.

Background compaction is automatic: the Claude Code hook loop compacts on
Stop, and writes on any path trigger compaction once the corpus crosses the
configured warn threshold. `memplex compact` remains available for manual
runs.

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
`~/.local/share/memplex/agent-venv`, installs `memplex==3.3.0`, detects local
agent config directories, and registers Memplex into the selected hosts. It uses
`uv` when available and falls back to `python -m venv` plus `pip`.

OpenClaw 入口对宿主有 `peerDependencies` 兼容约束：`openclaw >= 2026.5.17`。

Python-first users can skip npm:

```bash
uv tool install memplex==3.3.0
memplex setup --agent all --project-path "$PWD"
```

版本绑定的 npm 安装：

```bash
npx memplex@3.3.0 setup --agent all --project-path "$PWD"
```

## What Gets Installed

| Agent | Integration | Installed shape |
| --- | --- | --- |
| Codex / Claude Code | MCP / host-native wrapper | Codex: managed marketplace + plugin cache; Claude Code: registered and enabled local marketplace + lifecycle hooks |
| OpenClaw | JS plugin + bundled Python bridge | `extensions/memplex` JS 插件 + `python -m memplex.adapters.openclaw_plugin` + `plugins.slots.memory = "memplex"` |
| Hermes | official MemoryProvider wrapper | provider descriptor and `plugins/memplex`（使用固定官方源码与真实 CLI 验证） |

All installers are reversible and refuse to overwrite unmanaged existing
Memplex entries.

The root `marketplace.json` is the **publishing** descriptor. For Claude
Code, setup creates a host-native local marketplace at
`plugins/marketplaces/articultur/.claude-plugin/marketplace.json`, copies
the versioned plugin into Claude's cache, and updates `settings.json`,
`known_marketplaces.json`, and `installed_plugins.json`. Uninstall restores
the exact prior files when unchanged; if the user edited them afterwards,
it removes only the Memplex-owned keys.

## Verify

任何主机/宿主升级（含插件、包、配置漂移）后需重新执行 host matrix 与健康检查：

- `memplex --output json doctor --agent <agent>`
- `memplex --output json agent status --agent all`

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
- **Structured observation categories**: captured turns are classified as
  `bugfix` / `decision` / `change` / `discovery` / `note` and browsable via
  `memplex observations` and the MCP `memory_observations` tool.
- **Visible token costs**: search results, full reads, and agent recall
  annotate `est_tokens` / `tokens_used` / `max_tokens` so agents can budget
  context progressively.
- **Optional LLM observation compression**: long captured turns are
  compressed before storage (rule-based fallback otherwise; disable with
  `MEMPLEX_LLM_OBSERVATION_COMPRESSION=false`).
- **3-layer retrieval**: SQLite FTS5/BM25+trigram search, timeline, get.
- **5-dim reranking**: raw relevance, semantic similarity, recency, source
  authority, frequency.
- **5-stage compaction**: extract, dedup, summarize, prune, archive.
- **Wiki layer**: full-text/vector retrieval plus graph-aware synthesis.
- **Namespacing**: user, session, project path, and storage path isolation.

### Benchmarks

An evaluation harness (source checkout only) runs Memplex against LoCoMo,
NQ, TriviaQA, PopQA, HotpotQA, and a memory-specific suite. Offline
synthetic baseline (lite backend, TF-IDF embeddings, no LLM): 100% fact /
preference / observation retention on the memory suite, recall@10 = 1.0 on
synthetic LoCoMo and PopQA. These are synthetic offline numbers — not
real-distribution performance. See
[Benchmarks](docs/benchmarks.md) for methodology, full metric tables, and
reproduction commands.

## Docs

- [Getting Started](docs/getting-started.md): install, verify, doctor,
  recall explain, scope, inbox, corpus, policy, report, uninstall, and
  troubleshoot.
- [Explainer](docs/explainer.md): what Memplex is and how the memory loop works.
- [Architecture](docs/architecture.md): module map, split-module re-export
  contracts, ordered-circular-import rules, and the sync lockstep ABC.
- [Mutation Testing](docs/mutation-testing.md): pilot baseline, how to run,
  and the equivalence argument for surviving mutants.
- [Security Scan Triage](docs/security-triage.md): sealed deep-scan verdicts
  per finding category, with rerun instructions.
- [Agent Integration Loop](docs/agent-integration.md): adapter contracts for
  Codex, Claude Code, OpenClaw, and Hermes.
- [Benchmarks](docs/benchmarks.md): evaluation methodology, metric
  definitions, offline synthetic baselines, and reproduction commands.
- [Release Automation](docs/release-automation.md): 可复现构建、OIDC trusted
  publishing、attestation 与不可变摘要门禁。

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

The default memory backend is a JSON-backed `LiteMemoryStore` at
`~/.memplex/memory.json` (override with `MEMPLEX_STORAGE_PATH` or
`config.yaml`). All data is held in memory and flushed to JSON on every
write. A native **PostgreSQL backend** is also implemented: install
`memplex[postgres]` and set `MEMPLEX_STORAGE_BACKEND=postgres` to store
memory in JSONB columns with native tsvector full-text search; setting
`MEMPLEX_PGVECTOR_DIM` additionally enables pgvector semantic search
(hybrid tsv + vector cosine via RRF).

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
- **Pull-on-demand by default**, with two opt-in accelerators: background
  auto-pull via `MEMPLEX_SYNC_PULL_INTERVAL` and SSE push notifications
  (`/sync/events`) for near-real-time updates.
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
FTS5, auto-pull, pgvector, P2P mesh) is shipped, and the Wiki layer and
the Fact/Preference memory types are wired into the default
capture/retrieval paths. Further plans are tracked in GitHub issues.

## 代理互通与内存共享（Claude Code / Codex / OpenClaw / Hermes）

已落地的主机绑定关系：

- **Claude Code**：原生 Claude Code 插件（`plugins/marketplaces/articultur`）。
- **Codex**：原生插件 + MCP + hooks + skills（`plugins/marketplaces/memplex`）。
- **OpenClaw**：宿主原生 JS 入口（`extensions/memplex`），运行时通过
  `python -m memplex.adapters.openclaw_plugin` 桥接到 Python，绑定
  `plugins.slots.memory = "memplex"`；这是 bridge-backed 集成，不是无版本边界的
  OpenClaw 内部 ABI。
- **Hermes**：官方 `MemoryProvider` ABI 的 provider wrapper
  （`plugins/memory/memplex`），内部桥接 Memplex runtime，并通过
  `~/.hermes/config.yaml` 的 `memory.provider: memplex` 激活；这同样属于
  bridge-backed 集成。

### 一键安装 / 卸载

```bash
memplex agent install --agent all --user-id alice --project-path "$PWD"
memplex agent uninstall --agent openclaw
```

只读核对当前选中的宿主、安装路径、托管状态、身份、规范化工作区和可见性：

```bash
memplex --output json agent status --agent all
memplex --output json doctor --agent codex --target-dir "$CODEX_HOME"
memplex --output json scope explain --agent codex --user-id alice --project-path "$PWD"
```

### 共享身份/工作区/可见性语义

核心字段为 `user_id`、`session_id`、`project_path`。

- `session`：同一 `user_id` + `session_id` + `agent`。
- `workspace`：同一 `project_path`。
- `user`：同一 `user_id`。

查询顺序为 `session -> workspace -> user -> legacy`。

Legacy typed 记录首次通过 `owner + origin_session` 兼容命中时，会立即迁移并绑定
当前 workspace；迁移失败则拒绝返回，公共 explain 也会在运行时授权后重建结果投影，
不会残留被拒绝记录的 ID/名称/评分。迁移后不会继续跨 workspace 走 legacy 分支。
模型可控的检索/诊断也有运行时硬上限：搜索最多返回 100 项，所有激活检索路径
共享最多 500 项候选总预算，token budget 最多 32,000；observation/待审项最多
返回或扫描 1,000 项，scope preview 最多扫描 1,000 项，且不伪装成全库总数。
Graph 路径把自己的份额继续拆成 seed 与一跳邻居额度：Lite 使用维护中的邻接索引，
PostgreSQL 使用双向索引查询并在 Function join 前 `LIMIT`，不会先读取整张稠密图再切片。
这里的 candidate 指进入 graph traversal 的 Function 候选，不宣称约束底层 FTS
posting 或向量距离计算的内部工作单元。

Hermes ABI smoke 固定到 `v2026.8.3`（tag commit
`7de39e700d2c329e15d32eb0b96e2f7cdd9fbdb2`）中最后由
`3c27eb6234bf91b8ceee9e9071591b31e9b148cb` 修订的
[`agent/memory_provider.py`](https://github.com/NousResearch/hermes-agent/blob/3c27eb6234bf91b8ceee9e9071591b31e9b148cb/agent/memory_provider.py)，
上游文件 SHA-256 为
`678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd`。
这些字段也随 `agent manifest --agent hermes` 输出，便于机器审计；它仍不是本机
Hermes CLI installed-host proof。

原生插件启动 MCP 时会把受管身份写入 `MEMPLEX_*` 环境变量；MCP 身份完全来自
进程/受管配置，模型工具参数不能选择或覆盖 `agent/user/session/project`。手工启动
且没有受管环境时，使用 OS 用户、MCP 进程会话和当前工作目录。检索、写入、
按 ID 修改/删除、反馈、待审项与 observation
列表统一执行同一用户/工作区可见性校验。Codex turn state 也按
`user_id + project_path + session_id` 联合隔离。

### 可恢复安装行为

- **Codex**：仅移除由 Memplex 标记管理的 TOML 区块，保留用户无关内容。
- **OpenClaw**：有安装状态哈希时，支持精确回滚；若配置被外部修改，优先保留用户改动并仅做最小回退（移除 `memplex` 入口/钩子绑定）。
- **Hermes**：有安装状态哈希时，支持精确回滚；若配置被外部修改，恢复或移除 `memory.provider` 其余内容保留。
- **单宿主事务**：Codex、Claude Code、OpenClaw、Hermes 任一安装步骤失败，
  都会恢复该宿主的安装前内容与权限，并移除本次新建的托管文件。
- **跨宿主事务**：`memplex agent install --agent all` 会在首个写入前快照四宿主的
  明确托管路径；任一后续宿主失败时精确恢复整次调用前的文件、目录、符号链接与
  完整权限位，
  包括原本已安装的宿主，而不是将其普通卸载。

### 核验命令

```bash
codex plugin list --json  # 某些版本可能是 `codex plugins list`
OPENCLAW_HOME=/tmp/openclaw-home OPENCLAW_CONFIG_PATH=$OPENCLAW_HOME/openclaw.json openclaw plugins inspect memplex --runtime --json
hermes memory status
```

若当前机器未安装 `hermes` 命令，最后一条会报错，请使用 `which hermes`
确认环境；这不阻塞官方 ABI/source smoke，但会阻塞 Hermes installed-host proof，
因此不能据此宣称 Hermes 本机运行链路已通过或达到工业级部署条件。

## License

MIT
