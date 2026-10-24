# claude-mem 15.0.2 对比研究笔记（2026-10）

## 状态与口径

- 撰写日：**2026-10-23**（Asia/Shanghai）。**性质：内部研究笔记，含内部观点，不构成任何公开宣称**。
  对外比较仍受 [positioning.md](../positioning.md) 的边界与
  [memory-landscape-2026-09.md](memory-landscape-2026-09.md) 的禁止性口径约束。
- 对比锚点：**claude-mem@15.0.2** = [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem)
  commit [`e7173346f16bb3ab28cb4de11a63822895217d4c`](https://github.com/thedotmack/claude-mem/commit/e7173346f16bb3ab28cb4de11a63822895217d4c)
  （2026-10-23，Apache-2.0，open-core 边界以
  [docs/ip-boundary.md @ e717334](https://github.com/thedotmack/claude-mem/blob/e7173346f16bb3ab28cb4de11a63822895217d4c/docs/ip-boundary.md)
  为准）。npm 包页：[claude-mem](https://www.npmjs.com/package/claude-mem)。
- 事实来源声明：**claude-mem 侧全部事实来自此前调研中已核实的材料，本笔记撰写时未做联网重新核实**；
  每条事实附来源链接——能 pin 的用该 commit 的 GitHub 永久链接
  （[源码树快照 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)），
  其余用 issue/release 链接。Memplex 侧代码引用为撰写当日本地核对（命令记录见
  [附录](#附录引用粒度与本次核对记录)）。
- 证据等级图例：
  - 【官方声明】= claude-mem 官方文档 / 官方 skill 文档 / 发布物 / 项目自测中的说法（含官方估算）；
  - 【源码证实】= 前期调研在其源码中证实的行为（锚定上表 commit）；
  - 【维护者确认】= 维护者在 issue 中确认；
  - 【单用户报告】= 单个用户的 issue 报告，**不外推**为普遍结论；
  - 【前期材料】= 来自前期调研已核实材料的 claude-mem 侧事实：本笔记撰写时未联网复核，
    且前期材料未细分其原始证据类型（官方文档 / 源码 / issue）；引用强度低于【官方声明】
    与【源码证实】；
  - 【内部判断】= 本笔记观点，非公开宣称。

## 结论先行

1. **三形态不可混称**（§1.2）：v15.0.2 的 claude-mem 是三个部署形态的集合——本地 worker
   （SQLite/FTS5 + 可选 Chroma）、独立 server **Beta**（PostgreSQL + Valkey/BullMQ）、
   cmem.ai Cloud Sync。任何"claude-mem 是/不是 X"的表述必须先声明谈的是哪个形态；
   9 月笔记的部分结论失效，正是因为把本地形态当成了全貌。
2. **渐进披露是它最值得借鉴的机制**（§2.1）：官方 skill 文档把检索规定为
   `search → timeline → get_observations`、最后才 `get_tool_uses` 的分层流程，先过滤后取详情；
   官方 token 口径为索引条约 50-100 tokens、完整 observation 约 500-1000 tokens。
   "**约 10x 节省**"是**官方估算**，引用时必须如此标注，不得当作独立测量。
3. **server Beta 的可靠性工程超出"插件"预期**（§2.3）：agent event 与 generation outbox 同一
   PG 事务提交、发布失败留 `queued_only` 待 reconciliation、generation response 用
   `lockedBy/lockedAtToken/attempts` 做 attempt ownership fencing、迟到响应返回 `superseded`
   不重复写入。与 Memplex worker 已有的 lease/重试/死信同构，补 generation 幂等与 fencing 即可对齐。
4. **明确不照搬**（§4）：RAM-only 生成队列（[#4580](https://github.com/thedotmack/claude-mem/issues/4580)
   已暴露其恢复语义不足）、SQLite+Chroma 双库无跨库事务、loopback 信任 + 全项目共享 token 的
   授权边界、每工具调用一次付费 observer 的成本结构、telemetry 默认开启 opt-out 的隐私姿态。
5. **Memplex 的差异化组合未被动摇**（【内部判断】）：typed 节点 / 双时态 / 租户授权 / durable sync
   契约仍是对手未覆盖的组合；v15 的 team/project 作用域 API key 出现在 **server Beta 层**，
   缩小了"无租户"差距，但 OSS 侧边界由
   [ip-boundary.md](https://github.com/thedotmack/claude-mem/blob/e7173346f16bb3ab28cb4de11a63822895217d4c/docs/ip-boundary.md)
   决定。
6. 借鉴优先级（【内部判断】）：① 渐进披露默认化（紧凑索引为默认 + 批量详情 + 按需原文）；
   ② 缓存与预算感知的上下文装配；③ generation 幂等与 attempt fencing；④
   人工确认状态（"agent done ≠ human checked"）。

## 1. 版本与形态识别

### 1.1 版本锚点

| 项 | 值 | 来源 |
| --- | --- | --- |
| npm 包 | `claude-mem@15.0.2` | [npm: claude-mem](https://www.npmjs.com/package/claude-mem) |
| 对应 commit | [`e7173346f16bb3ab28cb4de11a63822895217d4c`](https://github.com/thedotmack/claude-mem/commit/e7173346f16bb3ab28cb4de11a63822895217d4c)（2026-10-23） | commit 永久链接 |
| 许可 | Apache-2.0（open-core，边界见其 [docs/ip-boundary.md](https://github.com/thedotmack/claude-mem/blob/e7173346f16bb3ab28cb4de11a63822895217d4c/docs/ip-boundary.md)） | 【官方声明】 |
| 撰写时 npm registry 最新版 | 15.0.2（本次撰写执行 `npm view claude-mem version` 输出 `15.0.2`，仅元数据确认，非事实重核） | 本地命令输出 |

npm 版本 ↔ commit 映射依据：沿用**前期调研材料核定的对应关系**（npm 15.0.2 的发布源码即
commit `e717334`，2026-10-23）。本笔记撰写时未联网复核该映射——未查 release tag、亦未读
该 commit 的 `package.json`；上表 `npm view` 输出仅证明 registry 最新版为 15.0.2，
**不构成映射证明**。

### 1.2 三种形态（【源码证实】，[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)）

| 形态 | 存储/依赖 | 定位与备注 |
| --- | --- | --- |
| 本地 worker | SQLite/FTS5 + 可选 Chroma | 默认形态；生成队列 RAM-only（§2.4）、双库无跨库事务（§2.5） |
| 独立 server（**Beta**） | PostgreSQL + Valkey/BullMQ | 团队/多客户端部署；outbox 与 attempt fencing（§2.3）；team/project 作用域 API key 与只读 MCP（§2.6） |
| cmem.ai Cloud Sync | 托管服务 | 云同步路径（托管） |

【内部判断】此前任何把 claude-mem 单独概括为"SQLite + Chroma 的本地插件"的表述自 v15 起不再成立；
server Beta 虽是 Beta，其可靠性设计已显著高于本地形态。

### 1.3 宿主覆盖

- claude-mem：其 CLI help 列出 **19 个 IDE 标识**【源码证实】
  （[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)）。
- Memplex：安装器支持 **4 个宿主**——`codex` / `claude-code` / `openclaw` / `hermes`
  （`memplex/adapters/agent_installer.py:437`；`memory_agent_manifest` 的 agent 枚举同此四值，
  `memplex/adapters/mcp_server.py:338`）。
- 【内部判断】宿主数量上 claude-mem 远多于 Memplex，且差距仍在扩大。Memplex 不应追宿主数量，
  价值主张在"每宿主的记忆质量 + 授权边界 + 跨宿主 sync 契约"；宿主矩阵的宽度差异在公开对比中
  应主动披露而非回避。

## 2. 机制对比

### 总览对比表

| 机制面 | claude-mem@15.0.2 | Memplex（本次本地核对） |
| --- | --- | --- |
| 检索分层 | 官方 skill 规定 search→timeline→get_observations→最后 get_tool_uses；先过滤后详情【官方声明】 | `memory_search`（索引式，带 `est_tokens`）→ `memory_get`（单条详情）→ `memory_observations`（事件流）；changelog timeline 已有；分层流程未写进宿主操作规程 |
| 预算 | ContextBudget 10k 字符 + 固定降级顺序 + 整项移除 + `overBudget` 显式标记【源码证实】 | 检索层 `max_tokens`（默认 4000/上限 32000）+ 按相关度贪心整项截断 + payload 报告 `tokens_used/max_tokens/truncated` |
| 写路径可靠性 | server：outbox 同事务 + `queued_only` reconciliation + attempt fencing + `superseded`【源码证实】 | worker：lease（lease_id/lease_until）、指数退避重试、死信（`max_attempts` 默认 3） |
| 生成队列 | 本地 RAM-only，崩溃恢复靠 transcript 重放【源码证实】 | 持久化任务队列（任务状态落库，含 lease/retry/dead-letter 字段） |
| 投影一致性 | SQLite（当前内容）与 Chroma（搜索投影）无跨库事务，靠 backfill【源码证实】 | 索引在主库内（SQLite FTS5 / PG pgvector），无双库投影问题 |
| 多租户/授权 | server Beta：team/project 作用域 API key + 只读 MCP【官方声明/源码证实】 | tenant/owner/workspace/visibility/grants + 受信 MCP 身份强制 |
| 宿主数 | CLI help 列 19 个 IDE 标识【源码证实】 | 4 宿主（codex/claude-code/openclaw/hermes） |

### 2.1 渐进披露检索

【官方声明】（官方 skill 文档；见
[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)。
旁证：本机 Cursor 插件缓存中的 `mem-search/SKILL.md` 与该流程一致，缓存版本未逐一比对，仅作旁证）：

- 规定检索顺序：`search(query)`（带 ID 的索引）→ `timeline(anchor=ID)`（邻近上下文）→
  `get_observations(ids=[...])`（只对过滤后的 ID 取完整详情）→ **最后才**
  `get_tool_uses(ids=[...])`（原始 `tool_input`/`tool_response`，官方称 last resort——未压缩，
  每条可达数千 tokens）。
- 原则：**先过滤后取详情**；ID 过滤前不得倾倒完整 observation。
- 官方 token 口径：**索引条约 50-100 tokens**，**完整 observation 约 500-1000 tokens**。
- "**约 10x 节省**"是**官方估算**【官方声明】，不是独立测量；任何引用必须带此标注。

Memplex 现有落点（本次本地核对）：

- `memory_search` 已是索引式：每条结果带 `est_tokens`（工具描述自报约 50-100 tokens/条），
  payload 报告 `tokens_used / max_tokens / truncated`（`memplex/adapters/mcp_server.py:59`；
  字段定义 `memplex/models/search.py:56-58`）。
- `memory_get` 单条完整详情（工具描述自报约 500-1000 tokens，响应含 `est_tokens` 字段），
  描述已写明 "Use AFTER memory_search"（`memplex/adapters/mcp_server.py:114`）。
- 量级口径说明：以上两个区间均为 **Memplex 工具描述文案的自报口径**——描述文本本身写有
  该量级（`mcp_server.py:59` "…per result (~50-100 tokens each)"、`mcp_server.py:114`
  "…(~500-1000 tokens; …)"，均为本次本地 grep 核对），本笔记**未做本地 token 测量**；
  该量级与 claude-mem 官方区间（本节上方）数值一致，属文案层同构，不构成独立测量值。
- `memory_observations` 每条带 `est_tokens`，支持按 category/子串过滤
  （`memplex/adapters/mcp_server.py:276`）。
- changelog timeline 已有：`ChangelogStore.get_timeline`
  （`memplex/storage/changelog.py:60`），经 service（`memplex/service.py:1515`）与
  HTTP API（`memplex/adapters/http_api.py:1020`）暴露。

差距（【内部判断】）：Memplex 的工具描述里也写了 "(10x token savings)"
（`memplex/adapters/mcp_server.py:59`）——它同样是**自报口径**，与 claude-mem 的官方估算一样
都不是测量值，公开材料中须同等标注。更实质的差距是：claude-mem 用**官方 skill 文档**把
分层检索流程硬性写进宿主的操作规程（含"get_tool_uses 是 last resort"的禁令），Memplex 的
分层用法只存在于工具描述文本中，未形成等价的操作规程与"按需原文"层。

### 2.2 ContextBudget：10k 字符预算与降级顺序

【源码证实】（前期调研，[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)）：

- 预算：**10k 字符**。
- 降级顺序（逐级放弃更贵的内容）：先弃**完整 narrative** → **上次会话摘要** →
  **上次回复** → **会话数减半** → **观察数减半**。
- 移除粒度：**整项移除，不中途截断**（避免半句进入上下文）。
- 逐级降级后仍超限：返回 **`overBudget` 标记**，不静默输出超预算内容。

Memplex 现有落点（本次本地核对）：检索层已有整项截断语义——`_apply_token_budget`
（`memplex/query_pipeline.py:507-537`）按相关度顺序贪心保留**整条**结果（不做条内截断），
截断发生时显式置 `truncated`（`memplex/models/search.py:58`）；预算由调用方经 `max_tokens`
控制（默认 4000，上限 32000，`memplex/adapters/mcp_server.py:74-76`）。

【内部判断】差异在层次：Memplex 的预算作用在**检索结果列表**，claude-mem 的 ContextBudget
作用在**注入上下文的装配**（narrative/摘要/回复/观察是装配成分）。Memplex 的注入装配在
`AgentMemoryRuntime.before_prompt`（`memplex/adapters/agent_runtime.py:539`），目前没有
对应的多级降级阶梯与显式超限标记。借鉴方向见 §3.2。

### 2.3 server Beta：outbox 与 attempt fencing

【源码证实】（前期调研，[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)）：

- agent event 与 generation outbox 在**同一 PostgreSQL 事务**内提交——事件落库与"待生成"
  记录原子化，不存在"事件已存、生成任务丢失"的中间态。
- 队列发布失败时记录保留 **`queued_only`** 状态，等待 **reconciliation** 补发——发布失败不丢数据。
- generation response 以 **`lockedBy` / `lockedAtToken` / `attempts`** 做 **attempt ownership
  fencing**：只有持有当前 attempt 所权的 worker 能写入结果。
- **迟到响应返回 `superseded`，不重复写入**。

对照 Memplex（本次本地核对）：worker 已有 lease 机制——`lease_id`/`lease_until` 的生成与
原子校验（`memplex/worker.py:419-421`；`_lease_matches` 校验非当前 lease 的完成写入，
`memplex/worker.py:449`）、重试与指数退避（`memplex/worker.py:518-524`、`:1009`）、
死信（`dead_letters_pending` `memplex/worker.py:735`、`worker_task_dead_lettered` `:1023`，
`max_attempts` 默认 3，`:666-670`）。

【内部判断】Memplex 的任务级 ownership（lease 校验）已对齐"迟到结果不得覆盖"的语义；
缺口在 **generation（observation 生成）侧的幂等与 fencing 字段**——即以 attempt 所权
（lockedBy/lockedAtToken 式）显式标记生成结果的归属，而不是仅靠任务 lease 间接保证。
对齐方式见 §3.3。

### 2.4 本地生成队列：RAM-only 及其来龙去脉

现状行为【源码证实】（前期调研，[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)）：

- 本地形态的生成队列是 **RAM-only**：排队状态在 worker 进程内存中，不落盘。
- 崩溃恢复语义：依赖 **transcript 重放**——重启后从宿主 transcript 重新生成丢失内容，
  而不是从队列恢复。
- 已知后果见 §5 [#4580](https://github.com/thedotmack/claude-mem/issues/4580)
  【维护者确认】：重启丢失 RAM 队列内未生成的 observation。

移除成因【官方声明】（来源类型未细分）：旧持久队列因**有状态非确定性 reducer 在重放时
产生重复与 retry storm** 而被移除。这是项目自述的历史成因，经前期调研材料转述——树快照
只能证明现状（RAM-only + transcript 重放），证明不了"为何移除"；该成因的具体来源
（commit message / issue / 官方文档）未在本次材料中固定，本笔记不臆造链接，引用强度
低于上文【源码证实】。

【内部判断】"删持久队列换确定性、把恢复外包给 transcript 重放"是一个自洽但激进的取舍：
它用**重放成本 + 数据丢失窗口**换取不再重放出重复任务。Memplex 的持久化队列
（lease/retry/dead-letter 落库）没有该丢失窗口，不应反向模仿。

### 2.5 SQLite 与 Chroma：无跨库事务

【源码证实】（前期调研，[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)）：

- 本地形态中 SQLite 与 Chroma 之间**没有跨库事务**。
- 职责切分：**SQLite 存当前内容**，**Chroma 是搜索投影**。
- 一致性策略：靠 **backfill** 补齐投影，不追求双写原子性。

【内部判断】"当前内容库 + 可重建投影 + backfill"是清晰的最终一致性取舍，但把一致性责任
转给了运维（投影损坏 → 重建，见 §5 [#3202](https://github.com/thedotmack/claude-mem/issues/3202)）。
Memplex 的索引在主库内（SQLite FTS5 / PG pgvector），无双库投影问题；未来语义向量层落地时
应保持"投影可全量重建、主库为唯一事实源"的属性，而不是引入第二个事实源。

### 2.6 server Beta：作用域 API key 与只读 MCP

【官方声明/源码证实】（前期调研，[源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)）：

- server Beta 提供 **team / project 作用域的 API key**。
- 提供**只读 MCP** 面。

【内部判断】这是 v15 相对 v13（9 月笔记"无租户模型"结论）的最大变化：租户作用域出现在
server Beta 层。它是否覆盖 open-core 的 OSS 侧、边界划在哪里，以
[docs/ip-boundary.md @ e717334](https://github.com/thedotmack/claude-mem/blob/e7173346f16bb3ab28cb4de11a63822895217d4c/docs/ip-boundary.md)
的口径为准。Memplex 的对照优势在于：作用域模型（tenant/owner/workspace/visibility/grants）
覆盖**全部形态**并下沉到存储层（PG RLS），且 Memplex 的 MCP 身份是受信强制
（owner 等输入已标 deprecated、以受信身份为准，`memplex/adapters/mcp_server.py:229-232`），
不依赖共享 token。

## 3. 值得借鉴清单及 Memplex 现有落点

| # | 借鉴点 | claude-mem v15 依据 | Memplex 现有落点（本次核对） | 建议方向 |
| --- | --- | --- | --- | --- |
| 1 | 渐进披露 | 官方 skill 规定分层检索与 token 口径（§2.1）【官方声明】 | `memory_search`/`memory_get`/`memory_observations` 均带 `est_tokens`；changelog timeline 已有 | 紧凑索引为默认 + 批量详情 + 按需原文 |
| 2 | 缓存与预算感知的上下文装配 | ContextBudget 降级顺序/整项移除/`overBudget`（§2.2）【源码证实】 | 检索层整项截断已有；装配层（`before_prompt`）无降级阶梯 | 装配层引入固定降级顺序与显式超限标记 |
| 3 | PG outbox / attempt fencing | 同事务 outbox、`queued_only`、`lockedBy/lockedAtToken/attempts`、`superseded`（§2.3）【源码证实】 | worker 已有 lease/重试/死信 | 补 generation 幂等与 attempt fencing |
| 4 | 人工确认状态（"agent done ≠ human checked"） | My Work 将"agent 完成"与"人已确认"区分为不同状态【官方声明】 | `memory_pending_reviews`（`mcp_server.py:224`）+ `memory_resolve`（`mcp_server.py:244`）已有"待评审→裁决"流 | 把该状态显式映射进宿主工作流与注入策略 |

分节说明：

1. **渐进披露**（内部判断）：Memplex 的工具面已具备分层检索的全部构件，缺的是三件事——
   把流程写成宿主侧操作规程（对位 claude-mem 的官方 skill 文档）、把"批量取详情"
   （一次 get 多个已过滤 ID）固化为推荐用法、为"按需原文"设显式的最后手段层。现有
   `memory_agent_manifest`（`mcp_server.py:338`）是承载该规程的现成通道。
2. **上下文装配**（内部判断）：ContextBudget 的可移植点不是"10k 字符"这个数，而是
   三个设计约束——固定降级顺序（先弃贵的）、整项移除（不中途截断）、超限显式标记
   （不静默）。Memplex 检索层已满足后两者（§2.2），装配层（`before_prompt`，
   `agent_runtime.py:539`）补齐第一项与标记即可。
3. **outbox/fencing**（内部判断）：Memplex 不需要照搬 PG outbox 的表结构，需要对齐的是
   语义：generation 结果带 attempt 所权标记，迟到/过期 attempt 的写入被拒绝或判
   `superseded`。现有 lease 校验（`worker.py:449`）是任务级的，generation 侧幂等
   以字段级显式化后，重放/重试路径的行为将完全可审计。
4. **人工确认状态**（内部判断）：claude-mem 的 My Work 把"agent 宣称完成"与"人已检查"
   分开，防止 agent 自评直接变成共享事实。Memplex 的 pending reviews / resolve 流
   语义相近，差距在于它尚未作为**记忆生效路径**的一等状态呈现（哪些节点必须过人审
   才进入注入/共享），值得在注入过滤与 sync 传播中显式化。

## 4. 明确不照搬清单

| # | 不照搬项 | claude-mem 侧事实依据 | 理由 |
| --- | --- | --- | --- |
| 1 | **RAM-only 生成队列** | [#4580](https://github.com/thedotmack/claude-mem/issues/4580)【维护者确认】 | 恢复语义不足：崩溃丢失队列内未生成内容。Memplex 的持久化任务队列（lease/retry/dead-letter 落库）是正确形态，不回退。 |
| 2 | **SQLite + Chroma 双库无跨库事务** | [#3202](https://github.com/thedotmack/claude-mem/issues/3202)【issue 记录】 | 双事实源 + backfill 把一致性责任转给运维，损坏场景已实际发生。Memplex 索引在主库内；未来向量层也应保持"主库唯一事实源、投影可全量重建"。 |
| 3 | **loopback 信任 + 全项目共享 token 作为授权边界** | 【前期材料】（出处未细分，见下注） | loopback 只能证明"来自本机"，共享 token 不能区分调用者身份；以它做授权边界等于"同机即同权"。Memplex 的授权模型（tenant/owner/workspace/visibility/grants + 受信 MCP 身份强制，`mcp_server.py:229-232`）不以此为边界。 |
| 4 | **每工具调用一次付费 observer 的成本结构** | 【前期材料】（出处未细分，见下注）；[#5023](https://github.com/thedotmack/claude-mem/issues/5023)【单用户报告】仅支撑"token 放大风险"半句 | 成本随工具调用次数线性增长；且把 observer 绑定付费服务与 Memplex 本地优先原则冲突。 |
| 5 | **telemetry 默认开启、opt-out 的隐私姿态** | 【前期材料】（出处未细分，见下注） | 默认外发 + 用户须主动退出，与 Memplex "本地优先、最小外发"的姿态相反。此条是姿态取舍，不针对具体缺陷。 |

下注：第 3-5 行的 claude-mem 侧事实均**沿用前期调研已核实材料**，本笔记撰写时未联网复核，
且前期材料未细分其原始证据类型（官方文档 / 源码 / issue），故标【前期材料】而非更强的等级；
本笔记不为其臆造链接。#5023 只支撑"成本放大风险"这一半句，不支撑"每工具调用一次"的
结构描述本身。

## 5. 已知运维教训（附 issue 链接与证据等级）

| Issue | 现象与教训 | 证据等级 |
| --- | --- | --- |
| [#4580](https://github.com/thedotmack/claude-mem/issues/4580) | 重启丢失 RAM 队列内未生成的 observation——RAM-only 队列恢复语义不足的直接证据（§2.4） | 【维护者确认根因】 |
| [#5023](https://github.com/thedotmack/claude-mem/issues/5023) | observer token 放大——成本结构风险（§4-4）。**单用户报告，不外推为普遍结论** | 【单用户报告】 |
| [#3857](https://github.com/thedotmack/claude-mem/issues/3857) | manifest 与 bundle 版本不一致导致 worker kill loop——发布物内部一致性是硬约束（Memplex 对应物：G008 契约文件集 hash 进 readiness proof） | 【issue 记录】（报告方等级未在本次材料中细分） |
| [#3202](https://github.com/thedotmack/claude-mem/issues/3202) | Chroma 损坏引发无限重试与内存膨胀（issue 口径称后续版本已加检测/重建）——投影库必须有损坏检测与全量重建路径 | 【issue 记录】 |

**File Read Gate 澄清**：其 A/B 实验是**文件读取成本评估**（项目自测：大文件省约 30-38%、
小文件约 0）【官方声明/项目自测】；**不是记忆召回质量评估**。任何关于召回质量或 token
效率的对比引用，不得混用该数字——它度量的是"读文件"的成本，不是"回忆忆"的质量。

## 6. 对 memory-landscape-2026-09.md 的勘误声明

[memory-landscape-2026-09.md](memory-landscape-2026-09.md) 是 **2026-09-04 时点、
claude-mem v13.24.0 的快照**，作为历史材料保留。其中关于 claude-mem 的以下表述
对 **v15.0.2 已过时**，新对比一律以本笔记为准：

1. **"无租户模型"**——v15.0.2 的 server Beta 已提供 team/project 作用域 API key 与只读 MCP
   （§2.6）。修正只到 server Beta 层；OSS 侧边界以
   [ip-boundary.md](https://github.com/thedotmack/claude-mem/blob/e7173346f16bb3ab28cb4de11a63822895217d4c/docs/ip-boundary.md)
   为准。
2. **"仅 SQLite+Chroma"**——v15.0.2 存在三形态：本地 worker（SQLite/FTS5 + 可选 Chroma）、
   server Beta（PostgreSQL + Valkey/BullMQ）、cmem.ai Cloud Sync（§1.2）。
3. **"覆盖式（无点时历史）"的单句口径**——不再沿用。v15.0.2 材料中与之相关的机制描述见
   §2.5（SQLite 存当前内容 + Chroma 搜索投影 + backfill）与 §2.4（崩溃恢复靠 transcript
   重放），但二者是**内容组织与恢复语义**，不构成时态维度（点时历史 / bitemporal）的
   对比结论——本笔记 §2 未包含该维度的重查。定性判断以结论先行第 5 条为准
   （typed / 双时态 / 授权 / sync 组合仍是 Memplex 差异化面，【内部判断】）；时态维度的
   精确对比须另行专项核查，在此之前不得引用旧文的单句判断，也不得将本笔记引申为
   v15.0.2 的时态结论。

该文其余部分（分层图景、基准诚信危机、Codex 专项核查、对 Memplex 的战略启示）不受本勘误影响。

## 附录：引用粒度与本次核对记录

引用粒度：

- claude-mem 侧：仅 `docs/ip-boundary.md` 与 4 个 issue 在前期材料中有明确路径/编号，
  pin 到 commit blob / issue 永久链接；其余源码级事实统一 pin 到
  [源码树 @ e717334](https://github.com/thedotmack/claude-mem/tree/e7173346f16bb3ab28cb4de11a63822895217d4c)。
  前期调研材料未固定 claude-mem 仓内具体文件路径，本笔记**不臆造路径**。
- 撰写过程中在本机 Cursor 插件缓存
  （`~/.cursor/plugins/cache/claude-mem/claude-mem-cursor/fa8ab09f…/skills/mem-search/SKILL.md`）
  读到与官方描述一致的分层检索工作流，作 §2.1 旁证；缓存版本未与 15.0.2 逐一比对，
  不作为独立来源。
- 本次仅执行过一次 registry 元数据查询：`npm view claude-mem version` → `15.0.2`。该输出
  仅证明 registry 最新版为 15.0.2，**不构成 npm 版本与 commit 的映射证明**；映射依据见
  §1.1（沿用前期调研材料核定）。除此之外**未做任何联网核实**；claude-mem 事实全部沿用
  既有调研已核实材料。

Memplex 侧本次核对的命令与位置（均为撰写当日执行）：

- `grep -n '"name":' memplex/adapters/mcp_server.py` → `memory_search`(:58)、
  `memory_get`(:113)、`memory_pending_reviews`(:224)、`memory_resolve`(:244)、
  `memory_observations`(:275)、`memory_agent_manifest`(:338)。
- `grep -n "est_tokens" memplex/adapters/mcp_server.py` → :59、:114、:276（及 :567/:599/:761）。
- Read `memplex/query_pipeline.py:507-537` → `_apply_token_budget`（按相关度贪心整项截断）。
- `grep -n "tokens_used" memplex/models/search.py` → :56-58（`tokens_used/max_tokens/truncated`）。
- `grep -n "lease\|retry\|dead_letter\|attempt" memplex/worker.py` → lease 生成/校验 :419-421/:449、
  重试 :518-524、`dead_letters_pending` :735、退避 :1009、`worker_task_dead_lettered` :1023、
  `max_attempts` 默认 3 :666-670。
- `grep -rn "get_timeline" memplex` → `storage/changelog.py:60`、`service.py:1515`、
  `http_api.py:1020`。
- `grep -n "def before_prompt\|def capture_turn\|class AgentMemoryRuntime" memplex/adapters/agent_runtime.py` →
  `AgentMemoryRuntime`(:394)、`before_prompt`(:539)、`capture_turn`(:571)。
- `grep -n "codex\|claude-code\|openclaw\|hermes" memplex/adapters/agent_installer.py` → :437 四宿主。
- 未运行测试套件/构建：本变更为纯新增文档，无代码改动。

## 禁止性口径

不得据本文宣称：

- Memplex 优于 claude-mem 或任何对比项目（不宣称 winner / 性能 / 精度 / token 效率优势）；
- "**约 10x 节省**"（claude-mem 官方估算）与 File Read Gate 的"大文件省约 30-38%"
  （项目自测）是独立测量事实——两者均须标注为**对方官方自报口径**；
- #5023 的 token 放大为普遍行为（单用户报告）；
- server Beta 的租户能力覆盖 OSS 侧（以 ip-boundary.md 为准）；
- 本文任何结论可替代共享协议下的同任务横评。
