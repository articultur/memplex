# G007 / G008 / G009 最终交付报告

日期：2026-08-30。结论：**最终本地验收通过；独立代码审查 APPROVE，架构审查 CLEAR。**
这证明指定工作树的修复和本地质量门闭环，**不代表 benchmark-qualified 或 production-ready**，也不是发布许可。

## 范围与来源

对象为 `fix/review-swave` 的未提交工作树，HEAD 为
`ef9aa8f4f224fbe9ddcc9de5de603fbbf11352b2`，比较基线 `main` 为
`9ec2a2ac6231b2359d9539d8b5c24348fb9e9678`。最终验收冻结了 **395 个项目文件**，
其中 **13 个 tracked 修改、33 个 untracked 项目文件**；验收前后摘要一致。
冻结 inventory SHA-256：`775f565d83598394de2c16d7a919fa94ffaa176b25efa4a93d823a7401ec43f9`。
这个摘要不等于 E1 包创建时的 diff 摘要，也不包含随后新增的本报告。

本文汇总已有实现与验收，不增加功能。公共叙事依据仓库内的
[定位](positioning.md)、[机制地图](capability-mechanisms.md)、[架构](architecture.md)、
[真实 CLI 工作流](guides/real-value-cli.md)和[当前 benchmark](current-worktree-benchmark.md)。
[G001 基线](open-source-benchmark-baseline.md)的 **49.5/100** 是历史时点评分；
本次没有重评分，也不沿用其“完整测试未知”的旧状态作为最终验收状态。

G007 是终审故事；本轮追加的 G008/G009 分别处理证据契约与 URI 脱敏阻塞。
它们与产品原有的 G008 四宿主生命周期门、G009 容量门同名但不是同一验收对象。
报告编写时读取的目标账本仍为 G007/G008 `review_blocked`、G009 `in_progress`；
本交付提供解除阻塞的证据，**不修改 goals/ledger、不代替总验收签署**。
目标、原始日志及工具审查记录的来源集中列于[本机审计证据](#local-audit-evidence)。

## 改善、机制与亮点

Memplex 的框架边界是“本地宿主集成的多 agent 长期记忆层”：host/transport adapters
调用 `MemplexService`，再委派授权、检索、存储、同步和运维；domain/storage 不反向依赖 adapters。
本轮把静态能力地图、可执行用户流程、严格证据包和独立终审连成可核查的交付链。

| 表面 | 机制与本轮改善 | 仍须保留的边界 |
| --- | --- | --- |
| 数据与历史 | [四类 typed memory](capability-mechanisms.md#typed-memory-model)携带身份、作用域、来源和版本；[双时态 Fact](capability-mechanisms.md#temporal-facts)保留 superseded 值及 `as_of` 历史。 | Observation 经独立授权写入，不属于普通 write 提取事务；事实历史不是任意图时序推理。 |
| Capture / recall | [多路径检索](capability-mechanisms.md#recall-retrieval-path)共享候选预算，经授权、重排、注入过滤和 token 截断；[真实 CLI 流程](guides/real-value-cli.md)覆盖持久化、agent capture/recall、scope/share、回环同步及临时 PG 恢复。 | 图检索仅有界一跳；agent CLI 不等于四个真实宿主安装成功；回环不等于 WAN/HA。 |
| 隔离与持久性 | [不可变授权上下文](capability-mechanisms.md#principal-tenant-authorization)与 request-scoped PG facade；[17 方法同步契约](capability-mechanisms.md#sync-convergence)、签名备份和原子恢复边界。 | Lite 不是恶意本机/OS 安全边界；legacy best-effort sync 与 durable sync 不可混称。 |
| 运维与交付 | [运维证据](capability-mechanisms.md#operations-observability)、[可确定构建](capability-mechanisms.md#reproducible-supply-chain)、[四宿主摘要契约](capability-mechanisms.md#four-host-lifecycle)各有显式门限；定位、贡献、安全、治理和支持入口已补齐。 | 机制存在不等于部署 SLO、公开制品或新鲜四宿主签名证据成立；不作竞品优胜排名。 |
| 证据可信度 | 严格 create/verify schema、归一化指标范围、真实 warm 配置、整标量脱敏和重算 checksum 对抗测试，避免错误证据通过。 | E1 仍为 synthetic、dirty-worktree、unsigned、aggregate-only；校验不认证发布者。 |

治理入口：[贡献](../CONTRIBUTING.md)、[安全披露](../SECURITY.md)、[治理](../GOVERNANCE.md)、
[支持](../SUPPORT.md)、[行为准则](../CODE_OF_CONDUCT.md)。这些是可用入口，不是运营成熟度认证。

<a id="final-gates"></a>

## 最终验收：精确计数与环境分区

以下是 G009 final verifier 保留的同一次验收记录，全部退出码为 **0**；
不是把早期 G007/G008 或多个聚焦运行累加成全量成绩。原始 argv、环境、时间、stdout/stderr、
JUnit 和覆盖率数据库见[本机证据 V](#local-audit-evidence)。文档编写阶段另行复验了四项静态门、
E1 和 Actions 只读状态；未重跑两套完整测试，未覆盖其原始记录。

| 门 | 最终结果 | V 中的原始证据 |
| --- | --- | --- |
| Lock | `uv lock --check` 通过，171 packages | `lock.json` / `lock.log` |
| Ruff | `.venv/bin/ruff check memplex tests benchmarks scripts` 通过，覆盖仓库要求的 memplex/tests 范围 | `ruff.json` / `ruff.log` |
| 六边形导入契约 | `.venv/bin/lint-imports`：132 files、356 dependencies；1 kept、0 broken | `imports.json` / `imports.log` |
| 类型边界 | `.venv/bin/mypy`：权威 pyproject 文件集 23 files，无问题 | `mypy.json` / `mypy.log` |
| 完整 Lite | **3263 passed, 403 skipped, 236 subtests passed；80.16%；204.34s**；119 warnings，无失败/错误 | `lite.json` / `lite.log` / `lite.xml` / `lite.coverage` |
| 真实 PostgreSQL + pgvector | **422 passed, 2 N/A；104.49s**；pytest 原文为 `2 skipped`，无失败/错误 | `pg.json` / `pg.log` / `pg.xml` / `pg_gate.py` |
| E1 复验 | 当前校验器返回 `evidence_level=E1` | `e1.json` / `e1.log` |
| Diff / 变更影响 | `git diff --check` 通过；对 main 的 GitNexus 比较为 36 files、106 symbols、19 processes，**CRITICAL** 影响分类 | `diff.json` / `diff.log`、`gitnexus.json` / `gitnexus.log` |
| Actions 状态 | 只读 GET 返回 `enabled=false`；未启用或触发 workflow | `actions.json` / `actions.log` |

CRITICAL 是累计变更的影响范围，不是失败测试；不能因门禁通过就把它描述为低风险。
GitNexus 的 tracked 比较范围也不等于包含 untracked 文件的 46 文件审查范围。

Lite 使用 Python **3.13.15**、Node **24.19.0**，设置 Lite backend 并清除继承的 PG DSN/required 标志。
**403 跳过是后端/环境分区，不是 403 个通过**：其中 **402** 因 Lite 环境未安装 pgserver，
另 **1** 因官方 Hermes CLI/source 不可用。402 的模块分布为 PG integration 376、sync PG 9、
backup 6、sync contract 1、G014 task repository 8、CI PG contract 1、capacity/chaos 1。
两个 G004 PG-only 模块在该 Lite 条件下不收集，其 **9 项**在专门 PG 门执行。
Lite 的 3666 个顶层 case = 3263 + 403；JUnit 的 3902 含另列的 236 个通过 subtests，
不能当作 3902 个独立顶层测试。覆盖率门为 68%，实际 80.16%；26543 statements、5267 missed。
119 warnings 包含 datetime 弃用、Starlette/httpx 和两项 SQLite 未关闭资源警告，未隐去。

PG 使用独立 `.venv-pgcheck`：Python **3.11.16**、PostgreSQL **16.2**、pgvector **0.6.2**，
临时服务器提供显式 DSN 与 `MEMPLEX_REQUIRE_PGVECTOR=1`，uvicorn/FastAPI 可用。
八文件选择依次为 `test_postgres_integration`、`test_postgres_backup_integration`、
`test_sync_postgres_integration`、`test_sync_repository_contract`、`test_g014_postgres_task_repository`、
`test_ci_postgres_contract`、`test_g004_postgres_backup_real_value`、`test_g004_postgres_probe_isolation`
（均在 `tests/`，扩展名 `.py`）；case 数依次 **376、6、9、15、8、1、3、6**，共 **424**。
这是本地八文件门，不宣称与 Actions job 的全部选择或容器版本相同。

仅两项 N/A：`test_required_vector_unavailable_rolls_back_migrations` 和
`test_best_effort_vector_unavailable_degrades_without_capability_row`，原因都是该 PG build
**提供 pgvector**，无法在此环境执行“扩展不可用”分支；不是缺依赖导致的失败掩盖。
真实双 uvicorn 进程共享 PG inbox/business state，以及成功/失败探针、完整备份生命周期
在 vector 未安装/已安装两种条件下的 **6 项目录保全测试均执行通过**。
服务已停止、临时根已移除；capacity/chaos 的 PG 跳过项不在这八文件门内，仍是残余缺口。
两套测试有时间重叠；204.34s / 104.49s 是 pytest 时长，不是性能基准，亦不可与独立聚焦次数相加。

<a id="strict-e1"></a>

## Strict E1：保留结果与纠正口径

公共复核入口是四文件包：[manifest](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/manifest.json)、
[datasets](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/datasets.json)、
[results](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/results.jsonl)、
[checksums](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/checksums.sha256)。
**7 个 synthetic 数据集、56 条 aggregate rows**；G008 于 `2026-08-30T00:32:22.083487Z`
重新生成，G009 只用修复后的 verifier 复验，并未重新生成 benchmark。

- 创建时 base SHA 为上述 HEAD，`source.dirty=true`；diff digest 为
  `f9ca1657ca3b39b7e301b8eb4142ba2787587d897232df826f120239d805554e`。
- 环境为 Darwin/arm64、Python 3.13.15、Lite；配置为 synthetic、all、top_k=10、seed=17；
  uv.lock digest 为 `148d12dc7710af9f7d1d684599c5bf1e03cbc1c999e3c27eca36fde7149880cc`。
- `warm_by_dataset.longmemeval=false`，其余 hotpotqa、locomo、memory_benchmark、nq、popqa、triviaqa 均为 true。
  旧全局 `warm=true` 声明已作废，不是对旧运行换标签。
- `retrieval`、`temporal_multihop` 仅表示已产生对应 synthetic 聚合结果；
  `acl`、`sync`、`latency_capacity`、`recovery`、`host_integration` 五项仍为 `not_measured`。
  独立功能测试不补写这个包的未测维度。
- `raw.status=null` 且附非空缺失原因，synthetic 也不能豁免；没有逐 query/candidate/rank/failure traces。
  同目录未签名 checksum 只能检查内部完整性；schema 拒绝无效内容，不阻止恶意方重写一整套合法假数据。

| 指标 | 纠正后的保留值 | 解释 |
| --- | --- | --- |
| HotpotQA retrieval `hop_precision@1` | **1.0**，samples=3，latency_ms=55 | 旧 **1.3333** 无效。按 relevant retrieved slots / k 计 precision；recall 仍按 unique supporting hops 计。 |
| LoCoMo recency `recency_accuracy` | **0.6667**，samples=3，latency_ms=51 | 新运行取代旧 0.5，不宣称质量改善。 |
| PopQA retrieval `mrr` | **0.7**，samples=5，latency_ms=53 | 新运行取代旧 0.6333，不保证仅凭 seed 可逐位重现。 |
| LongMemEval overall `answer_hit_rate` | **0.3333**，samples=3；两条记录 latency_ms=53 / 52 | 保留原有重复聚合行，不合并伪造新样本量。该指标已随 runner 改版下线：现行行为 `longmemeval_answer_quality`，主指标 `token_f1`，`answer_hit_rate` 的单向后继仅作辅助诊断 `substring_hit_rate`；保留值按旧口径照录。 |
| LongMemEval multi-hop `answer_hit_rate` | **0.0**，samples=1；两条记录 latency_ms=53 / 52 | 失败同样保留，不能推导通用多跳能力。现行 runner 以 `token_f1` 报分类型结果，口径见上行。 |

完整 56 行、其他指标及分母见[精确聚合表](current-worktree-benchmark.md#exact-aggregate-results)。
三个检索小集 hotpotqa/locomo/longmemeval 各 3 条，nq/popqa/triviaqa 各 5 条；
memory_benchmark 的 1 条嵌入记录仅作 provenance 占位，实际分母是 **50 facts、4 preferences、5 observations**。
没有运行公开数据集，strict runner 不会静默回退为 synthetic。该包不代表 clean SHA、公开发布版或生产分布。

## 审查驱动的修复

| 问题 | 最终修复与回归证据 |
| --- | --- |
| 空/不完整结果、缺失或错误类型 provenance 在重算 checksum 后通过 | [共享严格校验](../benchmarks/evidence.py#L415)约束非空结果、必需字段、类型、摘要和时间；[证据契约测试](../tests/test_benchmark_evidence.py)覆盖 create/verify 两入口，阻断 bool/int 等价绕过。 |
| 归一化指标越界 | [HotpotQA 实现](../benchmarks/popqa_hotpot.py)修正槽位分母；[范围回归](../tests/test_benchmark_evidence.py#L394)在创建、重算 checksum 校验时拒绝非有限值和已知 normalized metric 的区间外值，合法无界指标不强套区间。 |
| URL/libpq 凭据漏检；初次 URI 修复仍留下尾部或斜杠歧义 | [最终整标量脱敏](../benchmarks/evidence.py#L259)跨空白、换行、斜杠识别 scheme 后的 `@` / `?` / `#`，命中即整段替换；[171 项 URI 回归](../tests/test_benchmark_evidence.py#L915)覆盖 config、独立 argv、inline argv 与伪造校验和。早期部分替换 PASS 已撤回。 |
| synthetic raw-complete 豁免、错误 warm 声明 | [raw 统一约束](../benchmarks/evidence.py#L455)要求 null+原因；[实际调用参数](../scripts/run_g003_benchmark.py#L163)与 manifest 共用 warm map，[回归](../tests/test_g003_benchmark_runner.py#L450)核对每个数据集。 |
| PG 前置探针提交 CREATE EXTENSION，污染外部 DSN | [探针](../tests/test_g004_postgres_backup_real_value.py#L74)成功/失败均 rollback；[真实目录比较](../tests/test_g004_postgres_probe_isolation.py#L62)逐项检查 extension、schema/owner/ACL、role 的前后相等。CI 选择和 pin 同步更新。 |
| Sync 事件依赖顺序错误 | [单次提交前排序](../memplex/storage/lite/store.py#L804)保证删边→删节点→写节点→写边；[空目的端 merge/clear 回归](../tests/test_lite_sync_repository.py#L653)与真实回环流程验证。 |

审查未发现保留包实际泄漏真实秘密；攻击输入使用虚构凭据。整段脱敏会损失无害 query/fragment
或含 `@` 的诊断信息，这是接受的 fail-closed 取舍，普通无凭据端点仍保留。
最后 ai-slop-cleaner 在修复后 **PASS / no-op**，仅针对 G009 两文件增量，未扩大成全工作树清理认证。

<a id="architecture-invariants"></a>

## 八项架构不变量

下表逐项为 **proved**，意思是指定快照有实现、执行证据及独立架构审查支持，不是永久安全保证。
前五项源自 [AGENTS.md](../AGENTS.md) 与 [architecture.md](architecture.md)；
后三项另受目标 brief/goals 的证据、安全及范围约束，见[本机审计来源](#local-audit-evidence)。
E/A 指下节两位独立审查者；A 的最终 CLEAR 沿用先前全树审查，并限定复查 G009。

| 不变量 | 实现证据 | 执行/测试证据 | 独立审查证据 |
| --- | --- | --- | --- |
| 1. Ordered migration imports | [runner 定义](../memplex/storage/migrations/runner.py#L180)先于四组重导出（760/821/939/949 行），不能把依赖常量移到重导出后。 | [fresh-import permutations](../tests/test_dependency_boundaries.py#L19)：24 项；该模块共 26 项在最终 Lite 通过。 | A：定义顺序与兼容 facade 保持，proved。 |
| 2. Live-module routing | [postgres_resources](../memplex/storage/postgres_resources.py#L336)运行时经 `_pool.X` 取 manager/runner。 | [monkeypatch 回归](../tests/test_postgres_store.py#L2221)及两种 pool/resource 导入顺序在最终 Lite 通过。 | A：动态解析边界保持，proved。 |
| 3. G008 七文件 lifecycle contract | [共享契约](../memplex/host_lifecycle.py#L159)包含 agent_installer、install_transaction、agent_assets、agent_runtime、managed_identity、runtime_status、_shared 七个 `.py`，纳入全部四宿主摘要。 | [独立 mutation manifest](../tests/test_host_lifecycle_evidence.py#L112)逐文件变更必须使对应摘要变化；模块 9 项在最终 Lite 通过。 | A：文件集与 mutation 证明保持，proved；不等于真实宿主签名证据齐全。 |
| 4. 17-method sync lockstep | [Protocol / ABC](../memplex/sync_repository.py#L170)、[Lite](../memplex/storage/lite/sync_repository.py#L50)、[PG](../memplex/storage/postgres_sync.py#L42)同步实现相同操作。 | [集合、签名、具体类契约](../tests/test_sync_repository_contract.py#L148)：最终 PG 15/15 通过；Lite 的 PG 行为项按环境跳过。 | A：17 方法集合/签名/后端锁步保持，proved。 |
| 5. CI pin | [workflow](../.github/workflows/ci.yml)与 [release-workflow pins](../tests/test_release_workflows.py#L342)同时包含两个 G004 PG 模块及 Lite 排除项，mypy 文件集也受 pin。 | 最终 Lite 的 `test_release_workflows` 17/17 通过。 | E/A：PG isolation wiring 与 CI pin 保持，proved；未声称托管 CI 执行。 |
| 6. Strict E1 schema / redaction / raw / warm | [共享 schema](../benchmarks/evidence.py#L415)、[整段脱敏](../benchmarks/evidence.py#L259)、[raw](../benchmarks/evidence.py#L455)、[warm](../scripts/run_g003_benchmark.py#L163)。 | 最终 Lite 的 evidence/runner/benchmark **323+31+34=388** 项通过，其中 URI **171** 项；保留包复验 E1。 | E：72 接受/99 拒绝及 66 checksum 攻击；A：33/33 整段脱敏、99/99 拒绝、99/99 接受，恢复 CLEAR，proved。 |
| 7. Sync dependency order | [一次发布](../memplex/storage/lite/store.py#L804)：edge tombstone → node tombstone → node upsert → edge upsert。 | [merge/clear 回归](../tests/test_lite_sync_repository.py#L653)所在模块 25 项及[真实回环](../tests/test_g004_sync_real_loopback.py)1 项在最终 Lite 通过。 | A：依赖顺序与单次提交保持，proved；E 先前全树审查无剩余项。 |
| 8. Actions disabled | 仓库托管设置 `enabled=false`，不是从 workflow 文件推断；无启用或 dispatch。 | V 的只读 GET 响应及本次编写阶段只读 GET 均为 false；这是状态检查，不是 pytest。 | A：此前 GET 证据保留；最终 V 再核对，proved；不是 Actions-green。 |

### 原始 brief 的两项补充约束

以下实现边界未变，执行证据已具备；父任务已提供同一 architect 的 bounded addendum：
**两项均 CLEAR**。补充结论按父任务提供的审查证据记录，不冒称本报告另行发起了审查。

| 不变量 | 实现与执行证明 | 补充审查状态 |
| --- | --- | --- |
| 9. Hexagonal architecture | [分层约束](architecture.md#machine-enforced-gates-ci)及 [pyproject 契约](../pyproject.toml#L183)禁止 domain/storage 依赖 adapters；边界文件与冻结的 395 文件摘要一致。最终及编写阶段 lint-imports 均为 132 files / 356 dependencies、1 kept / 0 broken。 | A 补充 CLEAR：当前 memplex diff 仅 Lite sync 顺序，无 adapter 依赖或分层边界移动，契约未变；proved。 |
| 10. Tenant authorization | [上下文授权](../memplex/authorization.py#L97)和[写入身份绑定](../memplex/authorization.py#L194)、[PG transaction-local identity](../memplex/storage/postgres.py#L494)、[RLS FORCE/USING/WITH CHECK](../memplex/storage/migrations/0002_principal_acl.sql#L161)未变。最终 Lite 的 [context](../tests/test_authorization_context.py)、[gate](../tests/test_authorization_gate.py)、[authorized service](../tests/test_authorized_service.py#L90)、[HTTP tenant ACL](../tests/test_http_tenant_acl.py)与最终真实 PG 的 [ACL/RLS integration](../tests/test_postgres_integration.py#L6188)均纳入执行；PG 仅两项 vector-unavailable N/A，不涉及 ACL/RLS。 | A 补充 CLEAR：授权边界未变，真实 ACL/RLS 执行证明保持；proved，不扩张为生产部署认证。 |

<a id="independent-review"></a>

## 独立审查来源与结论

本报告通过 `read_thread` 读取以下 agent 的**最终工具返回**，没有用 final verifier
转述的 parent 状态替代独立审查证据，也没有发起新审查。

- **E — Einstein，code-reviewer**：`01a05020-d7f5-7d43-9b86-3303cc867dca`，最终 **APPROVE**。
  初次 **46 文件全树审阅**加 G009 **两文件修复复审**（同时读两份报告）；**0 剩余问题**。
  独立执行 **388 focused / 171 URI**；真实 CLI **72 接受、99 拒绝**；
  额外 **22 种对抗变体、66 次重算 checksum 攻击全部拒绝，5 个安全端点保留**。
  **395 个项目文件哈希不变**。该 lane 未重跑完整 Lite/PG，也不替代 architect。
- **A — Parfit，architect**：`01a05020-d77d-7093-b382-6821b38c00ea`，最终 **CLEAR**。
  独立只读验证 **33/33 整标量脱敏、99/99 原始 manifests 拒绝、99/99 脱敏 manifests 接受、
  4/4 安全端点保留**；八项不变量均 proved。先前 PG rollback/schema 关注项已解除。
  其余不变量沿用先前全树证据，最终复查限定于 G009；全量门禁由 V 单独证明。
  父任务随后提供同 agent 的有界补充：原始 brief 的六边形架构与租户授权两项均 **CLEAR**，具体证据见上表。

E 的 CLI case 数、额外攻击矩阵、A 的内存检查和完整套件属于不同口径，不相加充当更多独立测试。
**不声称这两位审查者审阅过本报告或随附 final-quality-gate.json。**

## 残余外部资格缺口与停止条件

本地 gate 通过解决了旧基线的本地执行缺口，但以下仍未证明：

- clean-SHA、公开数据集、逐样本 raw traces、第三方复跑、独立签名及不可变公共 benchmark 制品；
  当前 E1 的五个未测维度不能升级，历史 49.5/100 不自动加分。
- 四个真实宿主及部署绑定的新鲜签名证明；官方 Hermes runtime 在此环境仍不可用。
- 生产形容量/chaos、跨网络分区与 HA、部署授权/RLS、SLO/drain 观测、RPO/RTO；
  临时 PG 测试和回环同步都不能代替这些外部资格证明。
- 公开 registry 制品与当前源码/版本/digest 的对应，以及当前托管 CI 执行结果。
  **Actions 保持禁用**，本次无权启用；本地验证不是托管 check run。

本次停止于两份交付文件生成与文档验证。**无 README/代码/测试改动，无 commit/push、release
或目标账本写入**；保留现有 dirty worktree。外部资格须由后续明确授权、独立运行和制品补齐，
不能把本地故事的关闭写成项目 benchmark-qualified 或 production-ready。

<a id="local-audit-evidence"></a>

## 本机审计证据（Git 忽略，不是公共制品）

下列路径仅供拥有该工作区的审计者使用；普通公开叙事以此前仓库内文档、实现、测试和 E1 包为入口。
`.omx/`、`.superpowers/` 被 Git 忽略，公开读者不应被要求拥有这些本机文件。

| 编号 | 本机证据 | 用途 |
| --- | --- | --- |
| S | [brief](../.omx/ultragoal/brief.md)、[goals](../.omx/ultragoal/goals.json)、[ledger](../.omx/ultragoal/ledger.jsonl) | 原始约束与 G007→G008→G009 审查阻塞轨迹；未回写状态。 |
| I | [G009 implementation](../.superpowers/sdd/2026-08-30-g009-uri-redaction/implementation-report.md) | 初次方案撤回、最终整标量修复、TDD 及 CLI 证据。 |
| C | [G009 cleaner](../.superpowers/sdd/2026-08-30-g009-uri-redaction/ai-slop-cleaner-report.md) | 最终增量清理 PASS/no-op 与两文件哈希；不指代早期失效评估。 |
| P | [G008 PG probe](../.superpowers/sdd/2026-08-30-g008-review-fixes/pg-probe-report.md) | rollback、六种 catalogue before/after 证明及同步 CI pin。 |
| V | [G009 final verification](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-verification.md)、[原始证据目录](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/) | 本报告精确全量结果的权威出处；argv/cwd/env/UTC 时间与完整输出。 |
| V1 | [Lite log](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/lite.log)、[Lite JUnit](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/lite.xml)、[PG log](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/pg.log)、[PG JUnit](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/pg.xml) | 3263/403/236/80.16%/204.34s 与 422/2/104.49s 原始成绩。 |
| V2 | [计数与分区](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/results.log)、[provenance check](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/provenance-check.log)、[snapshot](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/snapshot-before.json)、[Actions GET](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-evidence/actions.log) | JUnit 分组、395 项目及 45 历史/输入文件未变、托管禁用状态。 |
| Q | [final-quality-gate.json](../.superpowers/sdd/2026-08-30-g009-uri-redaction/final-quality-gate.json) | 本报告的机器可读门禁索引，独立审查 agent ID 和八项证明；不是外部签名。 |

历史失败没有被删除或改写：旧 G008 PG 包装器在 pytest 成功后追加过度的整套 catalogue 断言而失败，
不能把它改记为 PASS。最终 PG 门以真实 pytest 退出码和探针局部目录断言为准，允许 legacy suite
在一次性数据库留下预期角色。一次 provenance 元数据同名冲突保留原失败，修正后的只读检查通过，
没有为此重跑测试。一个 77824-byte coverage shard 仅移入可恢复目录，aggregate coverage 保留。
这些诊断和恢复细节均在 V，既不计作产品失败，也不隐去审计历史。
