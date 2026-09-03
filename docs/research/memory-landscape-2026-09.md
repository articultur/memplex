# 业界记忆项目全景调研与深度对比（2026-09）

## 状态与口径

- 调研执行日：**2026-09-04**（Asia/Shanghai）。外部信息来源为各项目官方仓库/文档，
  交叉第三方分析与独立审计；全部外部链接与访问口径见[来源清单](#外部来源清单)。
- 本文性质：**内部研究文档**。它扩展 [positioning.md](../positioning.md)（2026-08-30
  口径）的对比范围，但**不修改该指南的公开边界与声明**，也不构成
  [open-source-benchmark-baseline.md](../open-source-benchmark-baseline.md)（G001）定义的
  任何资格评分或重评分。
- 对比对象分三层：**独立记忆层**（Mem0、Graphiti/Zep、Cognee、MemOS）、
  **agent 运行时/框架自带记忆**（Letta、LangMem/LangGraph）、
  **宿主集成记忆层**（claude-mem/Grok Mem——与 Memplex 同层）、
  **宿主原生记忆**（OpenAI Codex Memories、Anthropic Claude memory tool）。
- 沿用 positioning.md 的 apples-to-oranges 边界：产品层次不同不能按功能计数排名；
  OSS 与托管产品分开评价；同名术语（"memory"、"temporal"、"graph"）语义不同；
  厂商自报基准分数一律标注为厂商声明。本文**不宣称任何 winner**。

## 分层图景

| 层 | 项目 | 一句话定位 |
| --- | --- | --- |
| 独立记忆 SDK/平台 | Mem0、Graphiti(Zep)、Cognee、MemOS | 应用开发者嵌入的记忆基础设施 |
| agent 运行时/框架 | Letta(MemGPT)、LangMem/LangGraph | 记忆是运行时或工作流框架的原语 |
| 宿主集成记忆层 | **claude-mem/Grok Mem**、**Memplex** | 通过 hook/插件给多个编码 agent 宿主加跨会话记忆 |
| 宿主原生 | Codex Memories、Claude memory tool | 宿主自带的免费记忆，能力边界由宿主方定义 |
| 研究前沿 | SAGE、LightMem、LongMemEval-V2 | 图记忆自演化、轻量记忆、新一代评测 |

Memplex 与 claude-mem 是**同层唯二的多宿主记忆层**，两者都覆盖 Claude Code、
Codex、OpenClaw 三个宿主，因此 claude-mem 是结构上最直接的对照对象
（专项对比见[下文](#5-claude-mem-专项对比)）。

## 总览对比表

| 系统 | 层次 | 数据模型 | 时态/修正 | 检索 | 多租户/授权 | 同步/共享 | 存储 | 许可/热度（访问日） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Memplex** | 宿主集成多 agent 记忆层 | 四类 typed 节点（Function/Fact/Preference/Observation）+ scope/provenance/version/tier | **双时态**：superseded 保留 + `as_of` 点时查询 | 多路径（RAG/wiki/graph）+ 全局预算 + 注入过滤；**默认纯词汇栈**（语义缺口已量化） | **显式 tenant/owner/workspace/visibility/grants + PG RLS** | 17 方法 durable sync 契约（Lite/PG lockstep）+ 签名备份 | SQLite（FTS5）/ PostgreSQL+pgvector | 本地未发布 3.3.0；不宣称资格 |
| [Mem0](https://github.com/mem0ai/mem0) | 记忆 SDK + 自托管服务 + 平台 | 抽取式记忆条目 + Mem0g 图变体（实体节点+关系边） | ADD/UPDATE/DELETE/NOOP 冲突解决操作 | 可插拔（向量库+reranker）；2026-04 新算法**单次检索、无 agentic 循环**（厂商声明） | OSS 层无 Memplex 级授权证明 | user/agent/session scope；平台层另有 | 可插拔向量库 | Apache-2.0；SDK v2.0.19（2026-08-30 核对） |
| [Graphiti](https://github.com/getzep/graphiti) / Zep | 时态知识图框架（+托管） | Episodes → 实体+带时间边的 Context Graph | **双时态**（事件时间/摄取时间），失效边保留历史 | 时间/全文/语义/图四路 | 框架层无；Zep 企业层有 | Zep 托管服务 | Neo4j 等图库 | v0.29.3（2026-08-30 核对）；20k+ stars（厂商页） |
| [Letta](https://github.com/letta-ai/letta) (ex-MemGPT) | 有状态 agent 运行时 | **MemFS**：git 版本化 markdown 记忆文件系统 | git 历史（版本化，非语义化双时态） | `system/` 全量入上下文 + 按需发现；语义检索需可选工具 | 无显式租户模型 | git 天然可同步 | 本地/git | Apache-2.0；Letta Code v0.31.6（2026-08-30 核对） |
| [LangMem](https://www.langchain.com/blog/langmem-sdk-launch)/LangGraph | 工作流框架记忆原语 | episodic/semantic/procedural + Store 命名空间 | checkpoint 时间旅行（工作流层） | Store 可选语义索引 | 应用自定义 | 跨线程 Store（应用定义） | 可插拔 | LangGraph 1.2.11（2026-08-30 核对） |
| [Cognee](https://github.com/topoteretes/cognee) | 记忆引擎/管线 | 任意格式 ingest → 自托管知识图（ECL 管线） | 图演化 | 图+向量+关系三路 | 无显式租户模型 | 无内置 durable sync 契约 | 图库+向量库 | OSS |
| [MemOS](https://github.com/MemTensor/MemOS) | 研究型"记忆操作系统" | **MemCube**：明文/激活(KV-cache)/参数(权重)统一抽象、可互转 | 调度演化 | 意图感知预取调度 | MemCube 元数据含权限 | 跨会话 | 多种 | [论文 2507.03724](https://arxiv.org/abs/2507.03724)（2025-07）；工程成熟度低 |
| [claude-mem / Grok Mem](https://github.com/thedotmack/claude-mem) | **多宿主编码 agent 记忆层** | 会话/观察/摘要（AI 压缩产物），非 typed 语义模型 | 覆盖式（压缩演进） | **SQLite FTS5 + Chroma 向量混合**；MCP 三层检索（search/timeline/get_observations） | 无租户模型；`<private>` 标签做隐私排除 | cmem.ai 云同步（托管默认开启） | SQLite + ChromaDB | Apache-2.0；**93.1k stars**；v13.24.0（2026-09-03） |
| [Codex Memories](https://mem0.ai/blog/how-memory-works-in-codex-cli)（原生） | CLI/IDE 宿主原生 | 本地 markdown（summary/raw/skills） | 覆盖式 + 30 天老化 | **grep**（启动读 summary，agent 再 grep MEMORY.md） | 无（单机单用户） | **无跨机同步、无团队共享** | 本地文件 | Apache-2.0（`codex-rs/memories` crate） |
| [Claude memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)（原生） | API/CLI 宿主原生 | 客户端目录记忆文件（6 个命令） | 文件覆盖 | 文件读取（客户端实现） | 无 | [团队记忆](https://claude.com/blog/memory)在平台层 | 客户端文件 | 平台功能非开源（2025-09 beta） |

## 关键维度深度分析

### 1. 写路径与 token 成本（2026 年主战场）

Mem0 于 2026-04 发布[单次检索、无 agentic 循环的新算法](https://mem0.ai/blog/state-of-ai-agent-memory-2026)，
把 token 效率作为核心卖点；Codex Memories 用两阶段后台固化（闲置约 6 小时后、下次启动时执行）
把成本移到闲置时间；Letta 用 [sleep-time compute](https://arxiv.org/html/2504.13171v1)
（论文声称每查询最多约 5 倍测试时成本降低）做同类事。claude-mem 的路线是
AI 压缩 + 渐进披露（MCP 三层检索先过滤再取详情，自称约 10 倍 token 节省——厂商声明）。

Memplex 的 lite 写路径 fast path（1000-doc seeding ≤ baseline 的 34%，commit `38e8f8a`）
落在同一行业方向上，但注意成本轴不同：上述项目优化的是"每条记忆的 LLM token 成本"，
该提交优化的是"存储提交路径"。两条轴都需要，语义检索轴见下条。

### 2. 检索架构（语义是入场券，grep 是宿主的特权）

所有独立记忆层（Mem0/Graphiti/Cognee/LangMem）默认向量语义检索；claude-mem 在
SQLite FTS5 之上加 Chroma 向量做混合检索。唯一用 grep 的是 Codex 原生记忆——
因为它是宿主，可以把检索质量成本转嫁给用户。Memplex 默认是纯词汇栈
（TF-IDF embedder + FTS5/BM25），本仓库 2026-09-01 的 paraphrase 基准
（本地 `.memplex/benchmarks/paraphrase_baseline.json`，25 facts/100 queries/200 distractors）
量化了缺口：整体 recall@1/5/10 = 0.58/0.64/0.67，其中**低词汇重叠查询坍缩到
0.027/0.081/0.162**（高重叠为 0.88/0.96/0.96）。这是与所有独立记忆层和 claude-mem
对比时唯一的硬能力缺口，也是当前最高优先级待办。

### 3. 时态语义（Memplex 强项，与最强对手同构）

Memplex 的双时态 Fact（superseded 保留 + `as_of` 点时查询）与 Graphiti 的双时态
知识图是同一设计哲学；比 Mem0 的操作式冲突解决（ADD/UPDATE/DELETE/NOOP）更体系化；
claude-mem、Codex、Claude memory tool 均为覆盖式更新，无点时历史。注意边界：
Memplex 的图检索目前是**有界一跳扩展**，不支持通用多跳推理（沿用 positioning.md 口径）。

### 4. 多租户与授权（几乎无对手的差异化）

对比范围内，**没有一家独立记忆层或宿主记忆层在 OSS 层提供 Memplex 级的
tenant/owner/workspace/visibility/grants + PG RLS 模型**。Mem0 的 scope 是
user/agent/session 三键而非授权体系；claude-mem 无租户模型（`<private>` 标签是
隐私排除，不是授权边界）；Graphiti/Cognee/Letta 框架层基本没有。Memplex 的瓶颈
在部署级证明缺失（G001 P0 项），不在模型设计。

### 5. claude-mem 专项对比

claude-mem（已更名 Grok Mem，npm 包仍为 `claude-mem`）是与 Memplex **结构上最直接
的对照**：同为 hook 驱动的多宿主记忆层，且同样覆盖 Claude Code、Codex、OpenClaw
（另支持 Gemini、Copilot、OpenCode、Antigravity、Grok Bot，共 8 个宿主）。

| 面 | claude-mem | Memplex |
| --- | --- | --- |
| 集成机制 | 5 个生命周期 hook（SessionStart/UserPromptSubmit/PostToolUse/Stop/SessionEnd）+ 本地 worker HTTP API + web 查看器 | 宿主适配器 + 共享 recall/capture 运行时（`AgentMemoryRuntime.before_prompt`/`capture_turn`） |
| 记忆模型 | 会话/观察/摘要的 AI 压缩产物——**记录 agent 做了什么** | 四类 typed 语义节点——**记录世界是什么**（事实/偏好/函数/观察）+ provenance/version/tier |
| 存储 | SQLite（FTS5）+ ChromaDB 向量 | SQLite（FTS5）/ PostgreSQL+pgvector 双后端 |
| 检索 | 混合语义+关键词；MCP 三层渐进披露 | 多路径+预算+注入过滤；默认纯词汇（语义缺口待补） |
| 时态 | 覆盖式 | 双时态 `as_of` |
| 授权/租户 | 无；`<private>` 标签 | tenant/owner/workspace/visibility/grants + RLS |
| 同步 | cmem.ai 云同步（托管路径） | 17 方法 durable sync 契约 + 签名备份 |
| 商业形态 | **默认引导托管 CMEM Pro**（30 天试用后转付费；需 `--provider` 或 `CLAUDE_MEM_ONLINE_OPTIN=false` 显式退出登录）；创建者公开背书 CMEM 加密代币 | 本地优先，无托管，无代币 |
| 社区 | 93.1k stars / 8.2k forks / 2,525 commits；v13.24.0（2026-09-03），发版极快 | 本地仓库，未发布 3.3.0 |

判断（内部观点，非公开宣称）：claude-mem 验证了"多宿主 hook 记忆层"这个市场
真实存在且规模不小；它的优势在社区热度、混合检索和开箱即用；它的边界在
记忆是无类型的压缩观察流、无租户授权模型、无点时时态，以及默认托管+代币的
商业姿态带来的治理信任问题。Memplex 的差异化恰好压在后者缺失的三件事上
（typed/时态/授权），但**必须先补齐语义检索**才谈得上与它的检索能力同台。

### 6. 宿主原生记忆的双刃剑

Codex Memories（Apache-2.0 开源，见[专项核查](#专项核查codex-记忆系统的开源状态)）
与 [Claude memory tool](https://claude.com/blog/context-management)（2025-09 beta；
2026-05 [Code with Claude 大会](https://www.youtube.com/watch?v=YPOgIvC_RwA)展示
memory & dreaming 方向）正在把"单机单用户本地记忆"变成宿主免费赠品。两者共同的
明确边界——无跨机同步、无团队共享、无租户模型、检索弱（grep/文件读取）——
正是 Memplex 四宿主契约、sync 契约与授权模型的目标空间。positioning.md 的定位
判断（"多 agent 长期记忆层 for 本地宿主"）被本轮调研验证。

### 7. 基准诚信危机与证据生态

本轮调研最大的发现：领域公开数字目前普遍不可信。

- [Penfield Labs 审计 LoCoMo](https://dev.to/penfieldlabs/we-audited-locomo-64-of-the-answer-key-is-wrong-and-the-judge-accepts-up-to-63-of-intentionally-33lg)：
  **6.4% 答案键错误**；LLM judge 接受**高达 63% 的故意错误答案**；56% 的分类分解有问题。
- [Zep 与 Mem0 的 LoCoMo 罗生门](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/)
  （2025-05 起）：Zep 自报 84% → 修订 75.14%；Mem0 复测称 Zep 实为
  [58.44%](https://github.com/getzep/zep-papers/issues/5)；数字至今不可调和。
- [MemPalace 100% 造假](https://vectorize.io/articles/mempalace-benchmarks)（2025 末）：
  top_k=50 对 19–32 条的会话近似全量检索，绕过检索本身。
- 行业应对：Mem0 推 [BEAM](https://mem0.ai/research) 并自报 LoCoMo 92.5 /
  LongMemEval 94.4（厂商声明）；[LongMemEval-V2](https://arxiv.org/html/2605.12493v1)
  （2026-05）主张评测"获得经验"而非 QA；EgoMemBench、LifeMenBench、ConvoMem、
  Mnemoverse 及 [Hindsight 的基准宣言](https://vectorize.io/articles/langchain-memory-alternatives)涌现。

Memplex 的严格证据纪律（E1 分级、拒绝 synthetic 冒充公开数据、禁止 winner 声明、
[current-worktree-benchmark.md](../current-worktree-benchmark.md) 的显式边界）与
后丑剧时代的行业情绪同频。**在 clean SHA 上跑一次诚实的公开数据集基线**
（G001 P0-1）是当前性价比最高的信誉投资。

## 专项核查：Codex 记忆系统的开源状态

（应仓库维护者问题沉淀，2026-09-04 核查）

- [openai/codex](https://github.com/openai/codex) 整仓库 **Apache-2.0** 开源，
  121.2k stars（2026-09-04 访问）。记忆子系统实现代码在仓库内：
  `codex-rs/` Rust 工作区含专门 **`memories` crate**（相邻 `rollout`、`history`、
  `skills` 等）。
- 官方 Memories 功能（2026 preview）：本地 `~/.codex/memories/` markdown 存储
  （`memory_summary.md`、`MEMORY.md`、`raw_memories.md`、`skills/*/SKILL.md`）；
  会话闲置约 6 小时才具资格，下次启动两阶段后台固化（模型一抽取候选 → 模型二
  合并写入）；写入前密钥脱敏；读/写独立开关；256 rollouts 上限 + 30 天老化。
  架构细节依据 [Mem0 拆解](https://mem0.ai/blog/how-memory-works-in-codex-cli)（2026-05）。
- AGENTS.md（静态指令层，32KiB 上限、三级发现、`AGENTS.override.md`）规范已捐给
  Linux Foundation Agentic AI Foundation——**规范开源，非代码**。
- 边界：开源的是 CLI/IDE 本地记忆子系统；ChatGPT 云端工作区记忆不是这套；
  无跨机同步、无团队共享、不支持手编；上线时 EEA/UK/瑞士不可用。
- 对 Memplex 的意义：`codex-rs/memories` 的行为规格（闲置阈值、两阶段、markdown
  布局）是公开可读的，Codex 宿主适配器可精确互操作；同时 Codex 原生记忆的
  边界（无同步/共享/租户）是 Memplex 适配层的价值主张。

## 对 Memplex 的战略启示

1. **语义检索是入场券不是加分项**（对应待办第 1 项）：补齐前与任何独立记忆层
   及 claude-mem 都不可比；补齐后，多路径+预算+注入过滤的管线设计反而是优势。
2. **诚实的公开基准是最便宜的信誉资产**（对应 G001 P0-1）：行业数字全面破产的
   当下，严格 runner + 公开 per-query trace + 签名制品正是各家喊得多、真做的少。
3. **租户/授权 + 跨宿主共享是护城河叙事**：原生记忆与 claude-mem 都不做的三件事
   （跨宿主团队共享、租户边界、durable sync）恰好压在 Memplex 的既有契约上；
   优先补部署级证明（G001 P0-2/3）。
4. **多跳继续诚实命名**：除 Graphiti 外各方也没有真正的通用多跳；"有界一跳"
   的命名纪律与行业审计方向一致，不构成相对劣势。
5. **claude-mem 是最近的坐标系**：跟踪其混合检索与宿主扩展节奏，把对比维度
   收敛在 typed/时态/授权/本地自主性（无托管默认、无代币）上。

## 与既有文档的关系

- [positioning.md](../positioning.md)（2026-08-30）仍是公开定位与边界的权威文档；
  本文是其研究侧扩展，不修改其声明。
- [open-source-benchmark-baseline.md](../open-source-benchmark-baseline.md)（G001，
  49.5/100 not qualified）的评分与停止条件不受本文影响；本文不重评分。
- 待办优先级引用自 G001 §优先缺口与 2026-09-04 的仓库待办盘点（paraphrase 基准
  量化、lite fast path 已落地等）。

## 禁止性口径

不得据本文宣称：Memplex 优于任何对比项目（含 winner/性能/精度/token 效率主张）；
Memplex 已 benchmark-qualified 或 production-ready；任何厂商自报分数为独立事实；
"支持通用多跳推理"；本文对比可替代共享协议下的同任务横评。任何未来对外比较
主张须满足 positioning.md 结尾要求的共享协议、不可变原始制品与独立复跑条件。

## 外部来源清单

访问日期均为 **2026-09-04**（positioning.md 内版本号为 2026-08-30 核对）。

- Mem0：[GitHub](https://github.com/mem0ai/mem0) ·
  [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) ·
  [research/BEAM 自报分数](https://mem0.ai/research) ·
  [How Memory Works in Codex CLI](https://mem0.ai/blog/how-memory-works-in-codex-cli)
- Graphiti/Zep：[GitHub](https://github.com/getzep/graphiti) ·
  [Zep 论文 arXiv:2501.13956](https://arxiv.org/abs/2501.13956) ·
  [Zep vs Mem0 之争](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/) ·
  [Mem0 反驳 issue](https://github.com/getzep/zep-papers/issues/5)
- Letta：[官网](https://www.letta.com/) · [GitHub](https://github.com/letta-ai/letta) ·
  [MemFS 文档](https://docs.letta.com/concepts/memfs) ·
  [Sleep-time compute 论文](https://arxiv.org/html/2504.13171v1)
- LangMem/LangGraph：[LangMem SDK 发布](https://www.langchain.com/blog/langmem-sdk-launch)
- Cognee：[GitHub](https://github.com/topoteretes/cognee) ·
  [ECL 管线说明](https://www.cognee.ai/how-cognee-builds-ai-memory)
- MemOS：[GitHub](https://github.com/MemTensor/MemOS) ·
  [论文 arXiv:2507.03724](https://arxiv.org/abs/2507.03724) · [OpenMem](https://memos.openmem.net/)
- claude-mem/Grok Mem：[GitHub](https://github.com/thedotmack/claude-mem) ·
  [releases（v13.24.0，2026-09-03）](https://github.com/thedotmack/claude-mem/releases) ·
  [Termdock 概览](https://www.termdock.com/blog/claude-mem-persistent-memory-claude-code)
- OpenAI Codex：[openai/codex](https://github.com/openai/codex)（Apache-2.0，121.2k stars）
- Anthropic Claude：[memory tool 文档](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) ·
  [context management](https://claude.com/blog/context-management) ·
  [团队记忆](https://claude.com/blog/memory) ·
  [Code with Claude 2026：memory & dreaming](https://www.youtube.com/watch?v=YPOgIvC_RwA)
- 基准审计与新基准：[Penfield Labs LoCoMo 审计](https://dev.to/penfieldlabs/we-audited-locomo-64-of-the-answer-key-is-wrong-and-the-judge-accepts-up-to-63-of-intentionally-33lg) ·
  [MemPalace 事件](https://vectorize.io/articles/mempalace-benchmarks) ·
  [LongMemEval-V2 arXiv:2605.12493](https://arxiv.org/html/2605.12493v1) ·
  [2026 框架横评](https://vectorize.io/articles/best-ai-agent-memory-systems) ·
  [Mem0 vs Zep](https://vectorize.io/articles/mem0-vs-zep)
