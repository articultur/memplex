# Agent 集成闭环

> 生产身份说明（中文优先）：Codex、Claude Code、OpenClaw、Hermes，以及 HTTP、CLI、
> MCP、Sync 必须收敛到同一份已认证 `principal`。服务端 registry 仅保存
> `token_sha256`；客户端原始 secret 仅由 `MEMPLEX_PRINCIPAL_TOKEN` 注入。不要把
> `user_id`、`owner`、workspace 或 session 作为模型可写的身份声明。

Memplex now exposes a shared agent runtime for Codex, Claude Code, OpenClaw,
Hermes, and similar tool-using agents. The goal is a closed production-to-
consumption memory loop rather than manual memory entry.

## Shared Loop

All agent adapters should map their platform lifecycle into three operations:

1. **Recall before the model turn**
   - Call `memory_turn_begin` over MCP, or `memplex agent recall`.
   - Inject the returned `context` into the prompt as data.
   - The context is wrapped by `IndirectInjectionGuard` before injection.

2. **Capture after the model turn**
   - Call `memory_turn_end` over MCP, or `memplex agent capture`.
   - Pass the user message and assistant response.
   - Memplex writes the turn as observation input through `MemplexService`.

3. **Consolidate in the background**
   - Use existing compaction and background worker paths for dedup, summarize,
     prune, archive, vector refresh, and wiki compilation.
   - Hermes-style adapters can pass `next_prompt_hint` to `memory_turn_end` so
     Memplex prefetches context for the next turn.

## 集成形态与实现形式

当前实现的集成形态：

- **Codex / Claude Code**：均为宿主原生包装入口（Codex 通过 `plugins/marketplaces/memplex` 与 hooks，Claude Code 通过
  `plugins/marketplaces/articultur` 插件），并共享同一 MCP / hook 插件形态与生命周期契约。
- **OpenClaw**：原生 JS 插件入口（`extensions/memplex`）+ `plugins.slots.memory = "memplex"`，
  `index.js` 再桥接 `python -m memplex.adapters.openclaw_plugin`。该入口为 JS 包装层 + bundled
  Python 进程桥接；对宿主版本有 `peerDependencies` 约束（`openclaw >= 2026.5.17`）。
- **Hermes**：官方 Hermes `MemoryProvider` 子类/Provider wrapper（`plugins/memplex`），
  通过 `~/.hermes/config.yaml` 将 `memory.provider` 设为 `memplex` 以选择该 provider。

## 身份、工作区、可见性语义（跨主机互操作）

1. `principal`：生产身份由服务端 `MEMPLEX_PRINCIPALS_JSON` registry 中的 digest
   匹配建立；registry 只保留 `token_sha256`，客户端原始 secret 只能来自
   `MEMPLEX_PRINCIPAL_TOKEN`。HTTP、CLI、MCP、Sync 和四宿主必须使用这个同一合同。
   payload、工具参数、`owner` 或环境污染均不能升级或替换它。
2. `user_id` / `project_path`：受管安装身份优先于 payload 与可覆盖环境变量；宿主事件
   的工作区路径会标准化为 `workspace_id`。这只保护受管进程不受普通输入覆盖；
   `local-process` 仅限 development 的同 UID 本机便利，不是多租户或同 UID 对手边界。
3. `session_id`：从可信宿主事件上下文提取（Codex/OpenClaw/Hermes 均支持运行时注入）。
4. `visibility`：默认 `workspace`，支持 `session`、`workspace`、`user`。
   - `session`：同 tenant + subject + workspace + 同一 `session_id` + 同一 agent。
   - `workspace`：同 tenant + 同一 `workspace_id`；允许该租户内不同 subject
     共享工作区记忆。共享 workspace 的写操作仍会把 `owner` 与 provenance 规范化为
     当前可信 principal，调用方不能伪造写入者。
   - `user`：同 tenant + subject。
   - 不含 tenant provenance 的 legacy 记录在生产环境必须 fail-closed；不能作为
     跨租户、跨 workspace 或跨 session 的兼容回退。

回放与查询按 `session -> workspace -> user` 顺序聚合命中；生产不会把无 tenant
provenance 的 legacy 数据加入回退链路。

上述身份与可见性合同不代表工业交付已完成。HTTP batch 原子性与连接池仍属于 G003，
可靠 outbox/背压仍属于 G004；四宿主真实环境 E2E 仍是 G008 门禁。请以
`memplex --output json readiness --strict` 的当前输出为准，而非把本机 hook smoke 当作
生产证明。

所有 MCP 数据入口都复用同一作用域合同：`search/add/get/update/delete/feedback/`
`pending_reviews/resolve/observations` 不再直通全局 store；按 ID 的不可访问记录与
不存在记录使用同一失败语义，避免泄露对象是否存在。`scope preview` 也只展示
当前可信身份可见的样本，不返回全库总数。Codex 的临时 turn state 以
`user_id + project_path + session_id` 联合分区，读取时再次校验三项身份。

模型可控的运行时预算在 handler 之外仍会再次硬限制：搜索返回最多 100 项，
所有激活检索路径共享最多 500 项候选总预算，token budget 最多 32,000；待审项
与 observation 最多返回/扫描 1,000 项，`scope preview` 最多扫描 1,000 个
Function，输出只使用 `scanned_functions` / `matched_in_scan`，不声称全库总数。
Graph 路径将所得 candidate budget 进一步分配给 seed 和各 seed 的一跳邻居；Lite
通过内存邻接索引限量遍历，PostgreSQL 通过 source/target 索引和 join 前 SQL
`LIMIT` 限量读取。没有 bounded-neighbor ABI 的第三方 store 会安全降级为只返回
seed，不会退回无界邻居扫描。
该 candidate 口径是进入 graph traversal 的 Function 候选；底层 FTS posting、
向量距离计算等索引内部工作单元不在此计数口径内。
JSON Schema 中的 `maximum` 只是宿主提示，runtime/service clamp 才是不可绕过的
执行边界。

## Supported Agent Profiles

The portable profile registry is implemented in
`memplex.adapters.agent_runtime`.

| Agent | Integration modes | Memory behavior |
| --- | --- | --- |
| Codex | MCP, CLI, hooks | auto recall, auto capture, background consolidation |
| Claude Code | plugin, MCP, lifecycle hooks, CLI | lifecycle prompt recall/tool observation plus MCP tools |
| OpenClaw | plugin slot, CLI, MCP | triage, recall, dream shape; memory slot config |
| Hermes | memory provider, CLI, MCP | sync after response plus zero-latency prefetch |

Discover profiles:

```bash
memplex --output json agent list
memplex --output json agent manifest --agent openclaw
memplex --output json agent status --agent openclaw
```

`manifest` 输出静态集成契约；`status` 是只读的本机诊断快照，会同时显示
宿主选择、身份来源、规范化 `workspace_id`、有效可见性、关键配置路径、
托管标记、缺失文件和配置漂移。`doctor` 会复用同一快照，并把未安装或
漂移显示为 warning，而不会擅自修复配置：

```bash
memplex --output json doctor --agent openclaw --target-dir "$OPENCLAW_CONFIG_DIR"
memplex --output json scope explain --agent openclaw --user-id alice --project-path "$PWD"
```

`agent status --agent all` 会分别使用四个宿主的默认目录或对应环境变量；
因为单个 `--target-dir` 无法表达四个不同根目录，组合使用会被明确拒绝。

主机边界与升级后复核（开发边界）：

- **升级要求**：每次主机升级或主机配置变更后，必须重新执行
  `memplex --output json doctor --agent <agent>` 与 `agent status --agent all`，并对齐
  host matrix 与托管路径漂移，避免把历史成功状态当作当前可用状态。
- **Hermes 本机验真边界**：当前环境若无 `hermes` 命令，不可声称
  `Hermes` 已安装在本机可执行链路完成验真；仅可记录对官方精确
  `MemoryProvider` ABI/source 的 smoke 级验证（provider 形态与字段兼容）。
  当前机器可追溯锚点为 Hermes Agent `v2026.8.3`（tag commit
  `7de39e700d2c329e15d32eb0b96e2f7cdd9fbdb2`）、源码 revision
  `3c27eb6234bf91b8ceee9e9071591b31e9b148cb` 与
  [`agent/memory_provider.py`](https://github.com/NousResearch/hermes-agent/blob/3c27eb6234bf91b8ceee9e9071591b31e9b148cb/agent/memory_provider.py)
  SHA-256 `678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd`；
  同一机器可读合同由 `agent manifest --agent hermes` 输出。

Install or uninstall an agent host with one command:

```bash
memplex agent install --agent codex
memplex agent install --agent claude-code
memplex agent install --agent openclaw --user-id alice --project-path /path/to/project
memplex agent install --agent hermes --user-id alice --project-path /path/to/project
memplex agent install --agent all --user-id alice --project-path /path/to/project

memplex agent uninstall --agent openclaw
memplex agent uninstall --agent all
```

### Without A Checkout

Users do not need to download this repository. The hosted installer creates a
persistent Memplex Python environment and registers Memplex into a detected
local agent host. The recommended public entrypoint is the npm CLI:

```bash
npx memplex setup
```

Pass an agent when the user wants a specific host:

```bash
npx memplex setup --agent codex --project-path "$PWD"
npx memplex setup --agent claude-code --project-path "$PWD"
npx memplex setup --agent openclaw --project-path "$PWD"
npx memplex setup --agent hermes --project-path "$PWD"
npx memplex setup --agent all --project-path "$PWD"
```

Uninstall is symmetrical:

```bash
npx memplex uninstall --agent all
```

The `npx` package follows the same convention as CLIs such as
`npx shadcn@latest init` and `npx create-next-app@latest`: the package name is
the product name, the first command is the setup action, and the installer owns
the host-specific details.

Python-first users can install a persistent command and then run the same setup
verb:

```bash
uv tool install memplex==3.3.0
memplex setup
```

使用发布包内、版本绑定的安装器：

```bash
npx memplex@3.3.0 setup
```

Auto-detection checks local config directories and commands for Codex, Claude
Code, OpenClaw, and Hermes. If the user knows the target, pass it explicitly:

```bash
npx memplex@3.3.0 setup --agent codex --project-path "$PWD"
npx memplex@3.3.0 setup --agent claude-code --project-path "$PWD"
npx memplex@3.3.0 setup --agent openclaw --project-path "$PWD"
npx memplex@3.3.0 setup --agent hermes --project-path "$PWD"
```

一次事务配置全部受支持宿主：

```bash
npx memplex@3.3.0 setup --agent all --project-path "$PWD"
```

Direct `uv` is supported through the script. The installer deliberately creates
a persistent venv instead of using one-shot `uvx`, because generated agent
configs must keep importing Memplex after the install command exits:

```bash
MEMPLEX_AGENT=auto \
MEMPLEX_PROJECT_PATH="$PWD" \
bash scripts/install-agent.sh
```

The installers are deliberately reversible:

- **Codex**：在 `$CODEX_HOME/config.toml` 或 `~/.codex/config.toml` 注入
  marker-bounded 的 MCP 区块。撤销时会从配置里移除该区块，并清理
  `plugins/marketplaces/memplex` 及缓存。
- **Claude Code**：把插件注册到
  `$CLAUDE_CONFIG_DIR/plugins/marketplaces/articultur`。撤销时会移除该
  marketplace。
- **OpenClaw**：写入
  `$OPENCLAW_CONFIG_DIR/openclaw.json` 的 `plugins.slots.memory = "memplex"`
  与 `plugins.entries.memplex`，并写入 `$OPENCLAW_CONFIG_DIR/extensions/memplex`
  的双 manifest + Node 桥接脚本。可恢复行为：
  - 如果当前配置未被外部手工改动（与安装时哈希一致），卸载会恢复
    `openclaw.json` 的 `originalText`。
  - 如果被改动过，卸载只做最小回退：清理 `memory` 插件槽位与 `memplex` entry。
  - 受管理标记约束，未被 Memplex 管理的条目不会被覆盖。
- **Hermes**：写入
  `~/.hermes/config.yaml`（或 `$HERMES_CONFIG_DIR/config.yaml`）将
  `memory.provider` 设为 `memplex`，并生成 `memplex.json` 与
  `plugins/memplex`。可恢复行为：
  - 如果配置哈希一致，卸载会完整回写安装前 `config.yaml` 内容与权限。
  - 如果哈希不一致，卸载会回退 `memory.provider` 到安装前值或移除 provider
    键；其余用户改动保留。
  - 受管理标记约束，未被 Memplex 管理的 provider/plugin 不会被覆盖。
- 每个单宿主安装本身是事务性的：写入前只快照该宿主的明确托管路径；任一步
  失败都会恢复原文件内容/权限，并删除仅由本次尝试创建的托管目录。
- `memplex agent install --agent all` 额外提供跨宿主事务：若中途安装失败，
  顶层事务会恢复四宿主所有明确托管路径的调用前内容、目录、符号链接与完整权限
  位；调用前已安装的宿主会回到原状态，不会被普通卸载。

## 一键安装与卸载

```bash
memplex agent install --agent codex --user-id alice --project-path "$PWD"
memplex agent install --agent claude-code --user-id alice --project-path "$PWD"
memplex agent install --agent openclaw --user-id alice --project-path "$PWD"
memplex agent install --agent hermes --user-id alice --project-path "$PWD"

memplex agent install --agent all --user-id alice --project-path "$PWD"

memplex agent uninstall --agent openclaw
memplex agent uninstall --agent all
```

## 安装后验证命令（含已知不可用项）

```bash
# Codex
codex plugin list --json

# OpenClaw（隔离 profile）
OPENCLAW_HOME=/tmp/openclaw-home \
OPENCLAW_CONFIG_PATH=$OPENCLAW_HOME/openclaw.json \
openclaw plugins inspect memplex --runtime --json

# Hermes（可能未安装；先确认命令是否可用）
hermes memory status
# 若返回 `command not found`，说明本机未安装 Hermes CLI：
# 只能视为 CLI 不可达，不可直接声称「已成功安装」；需改为验证
# 官方 MemoryProvider ABI/source 与 provider 注册配置一致。
```

Use the closed loop from shell:

```bash
memplex --output json agent recall --agent codex "What did we decide about storage?"
memplex --output json agent capture \
  --agent codex \
  --user-message "Use lite storage for local dev." \
  --assistant-message "Recorded the local storage decision."
```

The shell CLI is process-per-call, so it persists captured turns and recalls them
through live search on the next invocation. Hermes-style zero-latency prefetch is
available when the adapter uses a long-lived MCP/server process; the prefetch
cache is scoped by storage path, project path, agent, user, and session.

Use the closed loop from MCP:

- `memory_agent_manifest`
- `memory_turn_begin`
- `memory_turn_end`
- existing memory CRUD/search/feedback tools

Claude Code's packaged plugin already has lifecycle hooks for prompt-time recall
and post-tool observation collection. `memory_turn_begin` / `memory_turn_end`
are the portable MCP turn contract for clients that can pass a complete
user/assistant turn; lifecycle-only clients can keep using the hook runner while
sharing the same `MemplexService` and storage.

## External Patterns Reflected Here

- Mem0's Claude Code integration combines MCP tools with lifecycle hooks that
  recall memories before prompts and capture learnings at lifecycle points.
- Mem0's OpenClaw integration names the core loop as triage, recall, and dream;
  Memplex uses the same shape in the OpenClaw manifest and installer.
- Mem0's Hermes integration uses background sync after a response and prefetch
  for the next turn; Memplex exposes `next_prompt_hint` for that path.
- LangGraph distinguishes short-term thread state from long-term namespaced
  memory, and separates hot-path writes from background writes. Memplex profiles
  carry `user_id` and `session_id`, while capture can run after the turn.

## Design Alignment

This implements the adapter-layer requirements for the public integration
surface:

- adapters share `MemplexService`;
- hooks/tools perform protocol conversion only;
- memory production is automatic through lifecycle capture;
- memory consumption is automatic through pre-turn recall;
- compaction remains the background "dream" path.
