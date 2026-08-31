# Memplex 开源基准与证据基线（G001）

## 范围与结论

- 审计对象：`fix/review-swave`
- 固定源码：`ef9aa8f4f224fbe9ddcc9de5de603fbbf11352b2`
- 外部资料获取日期：**2026-08-29**
- 范围：开源可理解性、真实用户价值、架构与数据模型、检索、时间与多跳、租户与安全、同步与持久性、可观测性、宿主集成、可复现性、DX/运维和治理。

当前结论是：Memplex 有显著的工程亮点和较完整的证据门设计，但**尚未达到本报告定义的开源基准资格**。保守临时得分为 **49.5/100**；这不是产品排名，也不表示当前测试全绿、当前工业就绪或优于任何竞品。

主要停止条件如下：公开安装版本与源码声明不一致；当前 SHA 没有完整 Lite/真实 PostgreSQL/四宿主检查证明；G005–G009 缺少当前签名证据；benchmark 原始输出未作为不可变公共制品发布。新增 strict runner 已禁止无提示使用公开数据失败后的 synthetic fallback，但本次环境缺少公开数据依赖与缓存，所以仍只有 dirty worktree 上的 E1 aggregate-only synthetic smoke，不是当前 clean-SHA、公开数据或生产证明。

本文是绑定上述分支、源码与日期的**时点快照**，其中测试状态、版本、数量和评分都可能过期，不应作为持续更新的产品能力清单。稳定的能力 ID、机制边界、代码/测试定位和限制见 [Capability mechanisms](capability-mechanisms.md)；机器消费入口见 [`capabilities.json`](capabilities.json)。两者只做仓库静态映射，同样不声明当前测试通过或工业就绪。

## 证据阶梯

本报告按以下等级评价每项能力：

| 等级 | 要求 |
|---:|---|
| 0 | 没有证据。 |
| 1 | 只有声明、设计或演示。 |
| 2 | 有文档和可运行 happy path，但缺少充分负向或跨环境证明。 |
| 3 | 有固定版本的自动化 E2E、负向测试和明确边界。 |
| 4 | 有第三方可独立复跑的不可变原始制品，并覆盖故障、对抗条件和运行环境。 |

“存在代码”“存在测试”“本次当前验证通过”和“公开可独立复现”是四种不同结论，不得互相替代。历史日志、历史 G009 数值、未签名 JSON、被忽略的本机 JSONL 和单元测试均不能提升到等级 4。

## 当前验证边界

独立测试审计在固定 SHA 上记录：

- `uv lock --check`、Ruff、import-linter（132 files / 356 dependencies）以及 mypy 的 23 文件边界通过；mypy 文件集合见 [`pyproject.toml`](../pyproject.toml#L95-L130)。
- Full Lite suite 在约 91% 时被中断，且中断前至少出现一个失败。因此**当前完整通过数、失败数和覆盖率均未知**。
- GitHub Actions 处于禁用状态，当前 SHA 有 **0 个 current check runs**；仓库中的 workflow 设计不能替代实际运行结果。
- 真实 PostgreSQL、pgvector、G005–G009 当前签名证据和当前四宿主运行均未证明。
- 不得将历史 G009 容量/chaos 数字描述为当前结果。

因此，本报告只承认静态门禁的有限记录，不声明“当前测试通过”或“当前 industrial ready”。

## 当前快照 inventory

以下为固定 SHA 的 tracked inventory；类别可能按审计口径分组或存在包含关系，数字只描述仓库表面，不证明正确性、覆盖率、可维护性或产品质量。

| 表面 | tracked 数量 | 当前证据状态与缺失原因 |
|---|---:|---|
| `memplex/` 文件 | 156 | 有实现与架构契约；当前 full Lite、真实 PostgreSQL 和真实宿主验证未闭环。 |
| Python modules | 133 | typed model、service、retrieval、ACL、sync、backup、operations 等代码可读；大模块与 source-layout contracts 增加变更风险。 |
| `test_*.py` | 111 | tracked tests 文件共 115；自动化面较广，但 full Lite 在 91% 后中断且已有至少 1 个失败，最终数量与覆盖率未知。 |
| `benchmarks/` | 11 | 有 harness、synthetic 数据与诚实边界；缺当前 SHA 的真实 dataset manifest、raw query traces、硬件/配置和不可变制品。 |
| `docs/` | 24 | 架构、生产门和评测文档可用；存在版本、HTTP/HTTPS、CI/security、curation、storage wording 与治理入口漂移。 |
| `scripts/` | 15 | 有发布、证据校验和运维脚本；缺当前环境实际执行并签名绑定的结果。 |
| workflows | 3 | workflow 形状存在；Actions 已禁用，当前 SHA 为 0 current checks。 |
| release/README/CHANGELOG/pyproject/npm/marketplace 组 | 12 | 发布与安装材料存在；公共 registry 为 3.2.7、源码/文档为未发布 3.3.0，当前 release bundle 与 registry digest 未闭环。 |
| 真实 runtime evidence | — | 当前无真实 PostgreSQL+pgvector、G005–G009 签名制品、容量/恢复或真实四宿主证明；历史记录不代替当前证据。 |

## 可核验用户任务集

这些任务是资格评测的最小用户价值表面；目标等级沿用本报告 E0–E4 证据阶梯。

| 用户任务 | 最小输入/环境 | 预期可观测输出或失败 | 目标等级 |
|---|---|---|---:|
| Local capture → recall | Lite 新目录；通过普通 `write()` 写入带来源的 Function/Fact/Preference，或通过独立授权的 `add_observation()` 写入 Observation，再按自然语言查询 | 返回正确 typed node、provenance、排序/命中 trace；Observation 必须经过独立授权绑定路径；未命中必须显式失败，不以空内容伪装成功 | E3 |
| Fact correction + `as_of` | 同一 subject/predicate 的旧值、新值及两个查询时间点 | 当前查询返回新值；历史 `as_of` 返回旧值；superseded 行保留且时间边界可审计 | E4 |
| Cross-agent shared knowledge with scope | 两个 agent identity、workspace grant 与 private/workspace 两类 memory | 获授权 agent 只读到共享范围；private/越权对象不可见且拒绝原因稳定 | E4 |
| Two-tenant negative isolation | 真实 PostgreSQL+RLS、tenant A/B、伪造或缺失 context | A/B 正向读写成功；跨租户、缺 identity、弱化 RLS 尝试 fail closed 且无数据泄漏 | E4 |
| Offline/duplicate/out-of-order sync convergence | 两副本、断网窗口、重复与乱序事件、重启 | 恢复后 durable inbox/outbox 收敛到相同状态；无 gap、重复副作用或越权；无法收敛时给出可恢复错误 | E4 |
| Signed backup → verify → restore | 真实 PostgreSQL 数据、HMAC key、隔离恢复 schema | backup digest/signature 可验证；tamper 被拒绝；restore readback、catalogue/ACL、RPO/RTO 与源摘要一致 | E4 |
| Health/ready/drain/SLO observation | 运行服务、认证探针、负载与 drain 信号 | health/ready 状态转换、排空结果、低基数指标、延迟/error-budget 与签名报告均可观察；秘密不回显 | E4 |
| Four-host install/status/capture/recall/uninstall | 同一固定 release bundle；Codex、Claude Code、OpenClaw、Hermes 真实宿主 | 每宿主安装、状态、capture、recall、卸载均成功并恢复原配置；任一宿主缺失或摘要漂移则总门失败 | E4 |

## 能力清单与本地证据

| 能力 | 代码/测试证据 | 当前边界 |
|---|---|---|
| 四类 typed memory | `MemoryNode` 包含 tenant、owner、workspace、visibility、provenance、version 和 knowledge tier；具体类型为 Function、Fact、Preference、Observation。见 [`memory.py`](../memplex/models/memory.py#L42-L73)、[`memory.py`](../memplex/models/memory.py#L154-L169)、[`memory.py`](../memplex/models/memory.py#L255-L270) 和 [`memory.py`](../memplex/models/memory.py#L315-L362)。 | 模型设计强；当前完整跨后端回归未证明。 |
| Service ingest | 普通 `write()` 统一授权并持久化 Function/Fact/Preference 与 graph edges；Observation 不走普通写入，而由独立授权的 `add_observation()` 绑定身份、扫描并持久化。见 [`service.py`](../memplex/service.py#L1291-L1408) 和 [`service.py`](../memplex/service.py#L1689-L1707)。 | 模块很大；完整失败/恢复面未由当前全量结果封口。 |
| 多路径检索 | query 包含 intent、并行多路径、合并去重、授权/namespace/owner 过滤、rerank、注入过滤、访问计数、`top_k` 和 token budget。见 [`service.py`](../memplex/service.py#L817-L1067) 与 [`multi_path.py`](../memplex/retrieval/multi_path.py#L35-L215)。 | 当前 graph 路径只做有界 seed + **一跳邻居扩展**，不能称通用 multihop reasoning。 |
| 双时态事实 | Fact 保留 `valid_from`/`invalid_at`；`list_facts(as_of=...)` 提供时间点查询。见 [`memory.py`](../memplex/models/memory.py#L256-L270)、[`service.py`](../memplex/service.py#L1599-L1646) 和 [`test_temporal_facts.py`](../tests/test_temporal_facts.py#L84-L132)。 | 时间语义有自动化覆盖；多跳聚合仍是独立缺口。 |
| 授权与 PostgreSQL 租户隔离 | `AuthorizationGate` 每次调用派生 request-scoped facade，生产缺少 context 时 fail closed；PostgreSQL 负责 tenant predicates 与 RLS。见 [`authorization.py`](../memplex/authorization.py#L70-L122)。 | Lite 是单机开发存储，不是针对同一 OS 用户、root、磁盘读取或恶意本机进程的安全边界。 |
| 同步协议 | Lite/Postgres 继承同一 17 方法 `AbstractSyncRepository`，契约测试固定方法集合和签名。见 [`sync_repository.py`](../memplex/sync_repository.py#L238-L329) 和 [`architecture.md`](architecture.md#L135-L141)。 | durable outbox 与 legacy best-effort HTTP sync 并存；legacy 队列可丢弃任务，语义需要明确收敛。见 [`sync.py`](../memplex/sync.py#L482-L580)。 |
| 备份与恢复 | 有签名 manifest、HMAC key、artifact writer 和 verify surface。见 [`backup.py`](../memplex/backup.py#L101-L236)、[`backup.py`](../memplex/backup.py#L803-L919) 与 [`backup.py`](../memplex/backup.py#L1028-L1031)。 | 当前真实 PostgreSQL restore/drill 和签名 G005 证据未证明。 |
| 运维/SLO | 有签名 operations evidence、原子报告写入、有限低基数指标及对应负向测试。见 [`operations.py`](../memplex/operations.py#L599-L690)、[`test_operations.py`](../tests/test_operations.py#L138-L170) 和 [`test_operations_evidence.py`](../tests/test_operations_evidence.py#L286-L309)。 | 当前真实观测窗口、流量、排空和签名 G006 报告未证明。 |
| 可确定发布 | 构建固定 locale、hash seed、时区和 epoch，归一化 wheel/sdist/npm，生成 SBOM、checksums 和 canonical manifest。见 [`build_release_artifacts.py`](../scripts/build_release_artifacts.py#L200-L296) 与 [`test_reproducible_release.py`](../tests/test_reproducible_release.py#L1-L110)。 | 当前公开 registry 仍是 3.2.7，而源码声明 3.3.0；没有当前不可变 benchmark bundle。 |
| 四宿主生命周期 | workflow 从待发布 artifact 安装 Codex、Claude Code、OpenClaw、Hermes，前后检查 runtime sidecar，并生成部署绑定签名 evidence。见 [`g008-real-host-lifecycle.yml`](../.github/workflows/g008-real-host-lifecycle.yml#L192-L313)。 | 这是强设计，不是当前运行证明；当前四宿主结果缺失。 |

## Benchmark 证据质量与 G003 当前状态

当前 benchmark 文档主动声明 synthetic、小样本和非真实分布边界，这是正确做法，见 [`benchmarks.md`](benchmarks.md)。新增 [current worktree report](current-worktree-benchmark.md) 与 strict runner 已产出一个四文件 canonical bundle：它记录 base commit、dirty-state/diff digest、配置、环境、7 个数据集的摘要与样本 ID 摘要、56 条 aggregate 结果、覆盖状态和 bundle checksums。现有 artifact 可从 [`manifest.json`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/manifest.json)、[`datasets.json`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/datasets.json)、[`results.jsonl`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/results.jsonl) 与 [`checksums.sha256`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/checksums.sha256) 复核。

G005 已判定旧 artifact 的 `hop_precision@1=1.3333` 为无效指标，而非对其作事后解释：旧公式把单个检索槽位中命中的两个 supporting hops 当成两个 relevant slots。修正后 precision 按 relevant retrieved slots / `k` 计算，recall 保留 unique supporting-hop coverage，重生成值为 `hop_precision@1=1.0`。bundle 创建与校验也会依据显式 normalized-metric schema 拒绝非有限值和 `[0,1]` 外值，即使攻击者同步重算 checksums；合法无界指标不受该区间约束。

G008 最终审查发现旧 manifest 的全局 `warm=true` 与 LongMemEval 实际 `warm=false` 调用不符；该全局声明已失效，现已重新执行并以 `config.warm_by_dataset` 记录每个数据集的真实模式。空结果、缺失/错误类型 provenance、URL query/fragment 与 libpq 凭据，以及 synthetic raw-complete 豁免均加入 create/verify 回归检查。新的精确数值与摘要见 [G008 修正说明](current-worktree-benchmark.md#g008-correction-and-invalidation)。这些修正不提升 E1 等级：仍为 dirty-worktree、synthetic、unsigned、aggregate-only，未证明生产或 clean-SHA 能力。

这只把 G003 从“没有绑定 bundle”推进到 **E1 aggregate-only synthetic smoke**。它以 `ef9aa8f4f224fbe9ddcc9de5de603fbbf11352b2` 为 base commit，但 `source.dirty=true`，不能代表该 clean SHA；只保留 aggregate `BenchmarkResult`，没有逐 query/candidate/rank/failure raw trace；覆盖仅 `retrieval` 与 `temporal_multihop` 为 `passed`，ACL、sync、latency/capacity、recovery、host integration 五个维度均为 `not_measured`。checksums 可发现文件损坏或 bundle 内部不一致，但没有外部签名，无法防止攻击者同时重写 payload 与 checksums，也不证明制品发布者身份。

strict runner 只允许显式 `--synthetic`，不会在公开数据不可用时静默 fallback。本次环境因公开数据依赖与缓存均不存在，未执行公开数据集；这是一项明确的 unavailable 状态，而不是 synthetic 成绩的公开数据替代品。仍缺少：

- 每条 query、候选、排序分数、最终命中和失败原因；
- 当前 clean-SHA 的 public-dataset 结果、公开不可变下载地址及独立签名；
- ACL、同步、延迟/容量、恢复和真实宿主集成的测量结果。

历史通用 loader 的 fallback 风险仍是历史审计发现；G003 strict runner 已通过强制显式 synthetic 路径避免该混淆。LongMemEval 本次仍只有 3 个 synthetic questions，aggregate `answer_hit_rate=0.3333`（保留 bundle 的已下线指标名；现行 runner 以 `token_f1` 为主指标，`answer_hit_rate` 的单向后继仅作辅助诊断 `substring_hit_rate`），其中 multi-hop 为 `0.0000`；这不证明真实 LongMemEval 能力。loader 现已兼容官方 `xiaowu0162/longmemeval-cleaned` schema（`answer` 单字符串 + `haystack_sessions`/`haystack_dates`），但尚未取得公开数据集的运行结果。

## 2026 临时评分卡

类别权重合计为 **100**；每行按 `weight × level / 4` 计算，保守临时总分为 **49.5/100**。

| 类别 | 权重 | 等级 | 原始得分 | 理由 |
|---|---:|---:|---:|---|
| 真实用户任务 | 8 | 2 | 4.00 | 有源码 happy path；公开版本和当前 E2E 证据不足。 |
| 架构/数据模型 | 8 | 3 | 6.00 | typed model、service 边界、架构契约和负向测试较强。 |
| 检索评测 | 14 | 2 | 7.00 | 有 harness 和 synthetic 结果；无不可变真实数据原始制品。 |
| 时间/多跳 | 12 | 2 | 6.00 | 双时态较强；graph retrieval 仅有界一跳，aggregation 失败被固定。 |
| 多租户/安全 | 12 | 2 | 6.00 | 授权/RLS 设计强；当前真实 PG 和 OS-adversary 证明不足。 |
| 同步/持久性 | 12 | 2 | 6.00 | 17 方法 durable contract；当前真实故障运行缺失，legacy 语义并存。 |
| 可观测性 | 8 | 2 | 4.00 | 有低基数指标和证据模型；无当前生产形观测制品。 |
| 集成 | 6 | 2 | 3.00 | 四宿主 workflow 完整；没有当前四宿主运行。 |
| 可复现性 | 10 | 2 | 5.00 | release bundle 确定性设计强；benchmark raw evidence 不可公开复核。 |
| DX/运维 | 5 | 1 | 1.25 | 文档丰富，但公共稳定版 3.2.7 与未发布源码 3.3.0 不一致且导航分散。 |
| 治理 | 5 | 1 | 1.25 | 有 MIT 与基础模板；贡献、安全披露、治理和支持入口不完整。 |
| **合计** | **100** |  | **49.50/100** | 保守临时分；不代表 benchmark qualification。 |

资格线要求：得分至少 75；核心维度均至少等级 3；其他类别不得低于等级 2；存在不可变公共 raw evidence。Memplex 当前四项均未满足，因此只能标记为 **not benchmark-qualified yet**。

## 优先缺口

### P0：阻断资格

1. 在 clean SHA 上发布 public-dataset bundle，补齐逐 query trace、独立签名和不可变下载地址；strict runner 已禁止无提示 fallback，但当前只有 dirty-worktree E1 synthetic aggregate bundle。
2. 修复并完整重跑 Lite suite，报告真实 pass/fail/skip/coverage；恢复 Actions，并为当前 SHA 产生可查 check runs。
3. 在固定 PostgreSQL + pgvector 环境重跑当前集成面，生成当前 G005–G009 和四宿主签名证据；历史数值不得复用。
4. 对齐 PyPI/npm/源码版本，让新用户能从公开制品复现被评分的确切 SHA。

### P1：核心能力不足

1. 用真实 LongMemEval、LoCoMo、MemoryAgentBench、MemoryBench 固定任务集；公布成功、失败和成本，不只公布 aggregate。
2. 为真正的多跳检索/聚合增加两跳以上、时间冲突、更新、删除和不可回答负向任务；当前一跳实现必须准确命名。
3. 增加跨租户伪造、RLS 弱化、恶意 payload、网络分区、重复/乱序、进程崩溃、磁盘故障和 restore tamper 的公开复跑包。
4. 明确 durable outbox 与 legacy best-effort 的产品边界、迁移路径和丢失语义。

### P2：维护性与开源 DX

1. 对 `service.py`、CLI、HTTP adapter、Postgres store 和 Lite store 等大型模块开展证据驱动的模块化与复杂度评审，不把“拆分”预设为更优；必须保留 ordered imports、live-module routing、sync lockstep 和 G008 contract files 等架构不变量。只有 impact 证据表明收益大于迁移风险，且契约、负向与回归测试覆盖拆分边界时才实施拆分。现有约束见 [`architecture.md`](architecture.md#L64-L141)。
2. 增加贡献指南、安全披露、治理、支持政策、版本化示例和统一 docs index。
3. 将 benchmark manifest schema、复跑环境和结果校验器纳入 CI，并对证据 schema 做兼容性版本控制。

## Bright spots

- 数据模型不是字符串拼装：tenant、provenance、visibility、version 和 typed nodes 都是一等字段。
- 双时态 supersession 保留历史而非覆盖，`as_of` 有明确读面和测试。
- PostgreSQL 授权采用 immutable context + request-scoped facade，避免共享 principal 状态污染并发请求。
- Lite/Postgres 同步通过抽象方法集合锁步，而不是只靠文档约定。
- 备份、SLO、release 和四宿主都采用 fail-closed、摘要/签名和部署绑定思路。
- Benchmark 文档公开承认 synthetic、小样本和 multi-hop 失败，没有把 smoke 数字包装成真实分布性能。

## 禁止性声明

在完成资格线前，公开材料不得声称：

- “当前所有测试通过”、给出当前 full-suite 数量或当前覆盖率；
- “当前 industrial ready”或“G005–G009/四宿主当前已通过”；
- “支持通用 multihop reasoning”；当前检索只证明有界一跳扩展；
- “Lite 可抵御本机/OS adversary”或等价安全保证；
- “真实数据 benchmark 已运行”，除非结果 manifest 证明未 synthetic fallback；
- 将历史 G009、历史 CI、unsigned JSONL 或本机临时文件描述为当前证据；
- “优于 Mem0、Graphiti、Letta、LangMem”或任何 competitor winner 结论。

## G002–G007 完成条件

| Gate | 本基线要求的完成证据 |
|---|---|
| G002 架构与机制知识地图 | 形成易导航的架构、机制与 capability 文档，覆盖模块边界、核心模型及关键链路，并逐项以代码和测试交叉验证。 |
| G003 可复现评测与当前 SHA 基线 | **部分完成（E1）**：已有 dirty-worktree synthetic aggregate bundle、绑定 manifest、数据摘要、结果摘要、覆盖盲点与校验命令；仍缺 clean-SHA public datasets、raw traces、独立签名，以及 ACL、同步、延迟/容量、恢复和宿主集成证明。 |
| G004 真实价值演示与开发者体验 | 提供可执行的 quickstart、examples、运维、故障排查和贡献者路径，并以自动化 smoke/E2E 证明真实流程，而非只依赖截图或 mock。 |
| G005 高价值缺口修复 | 依据证据选择并修复最高价值的正确性、安全性、可用性或证据链缺口；符号修改前执行 impact，以 TDD 和相应真实后端/宿主验证闭环，并保持全部架构不变量。 |
| G006 可信定位与开源标杆叙事 | 所有定位与比较均绑定权威来源、项目/源码版本、日期、口径差异和 caveat；性能与质量仅引用当前 SHA 可复现结果，不作无证据的 winner 或性能主张。 |
| G007 旗舰质量终审与证据包 | 运行全部仓库门禁；触发 storage/sync/migration 范围时运行真实 PostgreSQL+pgvector 门；对 changed files 执行 ai-slop-cleaner 后复验，完成架构不变量审计，并取得独立 code-reviewer `APPROVE` 与 architect `CLEAR`。 |

G002–G007 必须以 [`.omx/ultragoal/goals.json`](../.omx/ultragoal/goals.json) 的当前目标为准；全部完成仍不自动取得本评分卡的 benchmark qualification，资格线及不可变公共 raw evidence 条件仍须独立满足。

## 外部参考基线

以下 primary sources 于 **2026-08-29** 获取，只用于构建任务和证据维度，不据此宣称竞品赢家：

- [LongMemEval](https://github.com/xiaowu0162/longmemeval)
- [LoCoMo](https://github.com/snap-research/locomo)
- [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench)
- [MemoryBench](https://github.com/THUIR/MemoryBench)
- [Mem0](https://github.com/mem0ai/mem0)
- [Graphiti](https://github.com/getzep/graphiti)
- [Letta](https://github.com/letta-ai/letta)
- [LangMem](https://github.com/langchain-ai/langmem)

本报告不做 competitor winner 判断。只有在相同任务、相同数据版本、相同预算、公开原始 trace 和独立复跑条件下，横向排名才有意义。
