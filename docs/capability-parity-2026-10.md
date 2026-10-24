# 能力合同对照表（2026-10）

本文回答两个问题：**(A)** 稳定版 v3.3.2 与当前未发布代码之间，能力合同变了什么；
**(B)** 同一份当前代码里，Lite 与 PostgreSQL 两个后端各自兑现了哪些能力。
每一条状态都直接取自代码 / 配置默认值 / 提交，依据列给出锚点（文件、环境开关、
提交号）。

## 口径

- **v3.3.2** = git tag `v3.3.2`（2026-09-05 发布）。
- **当前未发布** = 工作树（2026-10-24）：HEAD `7b64f87`（2026-10-02）加三组
  **尚未提交**的修复——热记忆 ACL/词表（`memplex/auth.py`、`working_memory.py`、
  `service.py`、`adapters/agent_runtime.py`）、gap_forensics 测量修订
  （`scripts/gap_forensics.py`）及其证据（`docs/evidence/product-gap-forensics/`
  rev1 文件）。表 A 中仅这些行标注"工作树未提交"。
- 状态词：**在**（代码存在且默认路径可达）/ **默认开** / **默认关**（需显式开关）/
  **实验**（有代码与测试，无产品接线）/ **不在**。
- `pyproject.toml` 的 `version` 仍是 `3.3.2`——所有"当前"能力都未随任何
  release 发布。

## 表 A：stable v3.3.2 vs 当前未发布

| 能力 | v3.3.2（2026-09-05） | 当前未发布 | 依据 |
|---|---|---|---|
| 热记忆 tier（working memory） | 在，默认关（`working_memory.enabled=False`）。已有 V3 按 principal scope 分区；条目**没有**可见性字段 | 在，默认关。写侧继承写入可见性 + 召回侧 `recall_context(acl=...)` 逐条复查（HEAD）；租户相等性、session/agent 绑定与 `hot_entry_visible` 判定为**工作树未提交** | `memplex/config.py`（`WorkingMemoryConfig.enabled`）；`memplex/working_memory.py`；`memplex/adapters/agent_runtime.py::hot_entry_visible`（未提交） |
| 热记忆可见性词表 | 不在（无词表概念，条目无 visibility） | 在，默认路径生效。HEAD 词表 `{tenant, workspace, private}`，未知值**静默放宽为 tenant**（提权面）；工作树统一为 `{user, workspace, session, tenant}`（与宿主节点模型 `MEMORY_VISIBILITIES` 对齐），legacy `private` 仅作 `user` 别名，未知值 `ValueError` fail-closed（未提交） | HEAD `working_memory.py`（`else "tenant"` 强制转换）；工作树 `normalise_working_memory_visibility` + `WORKING_MEMORY_VISIBILITIES` |
| local-process 热条目 scope | 不适用（无此修复） | 在（**工作树未提交**）：`hot_tier_tenant()` 把 `local-process-*` 适配租户映射到 `local` 开发租户，写/召回解析到同一分区；真实租户仍按自身分区 | `memplex/auth.py::hot_tier_tenant`、`memplex/service.py`（capture 侧 scope 映射），均未提交 |
| raw paragraph 原文层（ADR-013 Stage 2） | 不在（无 `memplex_paragraphs`、无段落检索） | 在，默认开（写路径持久化段落 + 检索融合）。`MEMPLEX_PARAGRAPH_FUSION` 默认 `fallback`（段落只补 top-k 空位）；`mixed`/`primary`/`off` 可选。Lite 有 BM25 段落检索腿；PG 侧 B3 仅存储（见表 B） | 提交 `62b3a67`（lite Stage 2）、`b8e4235`（PG B3 + migration 0007）；`memplex/storage/lite/store.py::_paragraph_fusion_mode`（默认 fallback） |
| provenance trust tiers（ADR-013 Stage 1） | 不在 | 在，默认开。节点携带 `trust_tier`（user_direct=4 > session_derived=3 > external_web=2 > agent_inferred=1）；检索惩罚 `MEMPLEX_TRUST_PENALTY` 默认 0.5（tier<=2 乘 0.5）；可选用量配额/写侧 cap（见表 B，均 Lite-only） | 提交 `a29c6b8`；`memplex/storage/lite/store.py::_apply_trust_penalty`；`docs/evidence/trust-quota-bounded-occupancy/manifest.md` |
| orchestrated query | 不在（`retrieval.orchestrated` 配置键不存在） | 在，默认关。`MemplexService.query(orchestrated=True)` 按调用开启（LLM 分解子查询→多路检索→共享 merge/rerank，失败降级单查询）；PAR 腿（假设答案再检索）`MEMPLEX_ORCHESTRATED_PAR` 默认 1、仅在 orchestrated 时参与 | 提交 `7298c0b`/`5fd8070`（产品化）、`6f15ddb`（PAR 腿）；`memplex/config.py`（`orchestrated: bool = False`）；`memplex/service.py::_pseudo_answer_sub_query` |
| premise resolution（ADR-013 B1） | 不在 | 在，实验/维护通道。`resolve_premises()` LLM 维护 pass 判定"后句取代前句"，败者打 `premise_superseded` 戳；Lite 检索腿过滤该戳（默认路径生效）。pass 本身由脚本/维护调用触发（`scripts/stale_memory_eval.py`），未接入自动产品回路 | 提交 `1f60721`；`memplex/premise_resolution.py`；`memplex/storage/lite/store.py::_premise_resolution` |
| consolidation（ADR-013 F3，episodic→sustained） | 不在 | 在，默认关（`MEMPLEX_CONSOLIDATION=1`）。挂在 sleep-time 回路（sleep-time 本身默认关）；规则式离线晋级 + 遗忘，不碰查询路径；Lite-only（见表 B） | 提交 `cf50cae`、`d1bc02e`（v2 + tier2 cap）；`memplex/consolidation.py` 头注；`memplex/sleep_time.py::_consolidate_memory` |
| sleep-time 计算通道 | 在，默认关（`sleep_time.enabled=False`）：improve + 推理预计算 | 在，默认关。同一回路新增 consolidation（F3）维护通道 | `memplex/config.py`（`SleepTimeConfig.enabled`）；`memplex/sleep_time.py::run_once` |
| entity hubs（v13 配方产品移植） | 不在 | 在，默认关（`MEMPLEX_ENTITY_HUBS=1`）。维护通道：从段落层推断会话，为跨会话实体物化可检索枚举 Function；Lite-only | 提交 `c043e0e`；`memplex/entity_hubs.py` 头注 |
| SQLite authority rw 模式（ADR-012） | 不在（JSON pair 是唯一权威持久化；无 `sqlite_v2.py`） | 在，默认关。`MEMPLEX_LITE_SQLITE_SHADOW=1` 双写影子库（Phase A）；`MEMPLEX_LITE_SQLITE_AUTHORITY=read|rw` 读/写权威（Phase B0-B4，rw 下 JSON 退为 N 代快照导出）。默认不设 = JSON pair 仍是权威。ADR-012 Status 段仍停留在 Phase A 暴露期（文档滞后于代码） | 提交 `d24aea3`/`7899f44`/`d2fbcd1`/`675a66c`；`memplex/storage/lite/sqlite_v2.py`（两个 flag 读取）；`memplex/storage/lite/store.py::_sqlite_write_authority` |
| peer-mesh 对等协调 | 不在 | 在，实验。Phase 1 纯协议核（版本向量/Merkle 反熵/reconcile）、Phase 2 HTTP gossip transport、Phase 3 窄交换（按 id 清单）；仅 contract tests（`tests/test_peer_mesh*.py`），无任何产品接线（包内零 import） | 提交 `be6619d`/`27af8d4`/`5b7e1d2`；`memplex/peer_mesh.py`、`memplex/peer_mesh_transport.py` |
| gap_forensics 归因工具 | 不在 | 在。2026-10-02 首跑（`e95787c`，证据 manifest 归因为 context 11/17）；2026-10-23 测量修订（**工作树未提交**）修复两处缺陷后重跑：归因反转为 **retrieval 15/17**（counting 题按全覆盖判定）、context 1、answerer 1；旧 stage_counts 与 0.8796 上界**冻结**（manifest 标注不得用于决策） | `scripts/gap_forensics.py`；`docs/evidence/product-gap-forensics/manifest.json`（`measurement_revision` 段）+ `summary-rev1.json`（未提交） |
| raw paragraph ACL 身份绑定 | 不适用（v3.3.2 无段落层） | 在（**工作树未提交，2026-10-24**）：段落行携带写方 tenant/subject/workspace/visibility（扁平字段 + `memplex_*` namespace 副本，首写绑定）；检索/`service.get`/runtime namespace 过滤三层均按该身份判定——认证 runtime 召回自己的段落，跨租户/跨主体隔离，legacy 无身份行 fail-closed 退 local-development；段落视图 `to_dict` 进入读侧注入扫描 | `storage/lite/store.py::persist_paragraphs`、`authorization.py::_RawParagraphView`、`retrieval/multi_path.py::filter_by_namespace`（均未提交）；`tests/test_raw_paragraph_identity.py` |
| 检索级基线矩阵 + Raw-RAG 对照 | 不在 | 在（**工作树未提交，2026-10-24**）：`scripts/product_retrieval_baseline.py` 本地无 API，同库对比 default/orchestrated/parity/Raw-RAG 四组合的会话级 any/full 覆盖（计数题分层） | `scripts/product_retrieval_baseline.py`；`benchmarks/results/product-retrieval-baseline/`（本地，gitignored） |

## 表 B：Lite vs PostgreSQL parity（同一份当前代码）

| 能力 | Lite | PostgreSQL | 判定 |
|---|---|---|---|
| sync 仓储操作集 | 实现 `AbstractSyncRepository` 全部 17 个抽象方法 | 同左，逐方法签名锁步 | **parity**（`tests/test_sync_repository_contract.py` 钉死 17 方法集合与两实现） |
| 行级安全（RLS） | 无数据库层 RLS；租户/主体/工作区/会话隔离由应用层执行（AuthorizationGate + scope 前缀 + 热记忆 ACL） | `ENABLE/FORCE ROW LEVEL SECURITY` + 逐表 `<table>_scope` policy（含 0007 段落表的 user/workspace/session 四要素合同） | **PG-only**（Lite 语义等价、执行点在进程内） |
| paragraph 持久化 | 在（写路径权威层，content-addressed 去重，`_paragraphs`） | 在（`memplex_paragraphs` 表，migration 0007；`persist_paragraphs` + `get_paragraph` 按 id 读取；行不参与 prune） | **parity**（存储合同一致） |
| paragraph 检索 | 在：BM25 段落腿 + 四档融合（`MEMPLEX_PARAGRAPH_FUSION` 默认 fallback 补位、primary 为基准口径配方） | 不在：检索集成明确标注 Stage 3（migration 0007 注释 "retrieval integration follow in Stage 3"）；问句检索不会命中原文段 | **Lite-only / PG-deferred** |
| paragraph 同步 | —（`SyncNodeType` 尚无 `PARAGRAPH` 成员） | —（同左；postgres.py 注释 "paragraphs over the wire is Stage 3 (SyncNodeType extension)"） | **计划中（Stage 3）** |
| trust 惩罚 / 配额 / cap | `MEMPLEX_TRUST_PENALTY`（默认 0.5）、`MEMPLEX_TRUST_QUOTA`（默认关）、`MEMPLEX_TRUST_TIER2_CAP`（默认关）全部生效于检索尾部/写路径 | 无任何对应机制（manifest 明示 "the postgres backend has no trust-weight mechanism to mirror"） | **Lite-only** |
| vector_search 覆盖节点类型 | Function（FTS5 BM25+trigram 与向量腿融合）+ Fact/Preference（纯 Python BM25 腿）+ Paragraph（BM25 腿按融合档参与）；可选 `MEMPLEX_SPARSE_LEG=1` bge-m3 稀疏腿 | 仅 `memplex_functions`（tsvector 全文 + pgvector 余弦，RRF 融合）；Fact/Preference/Paragraph 不进任何检索腿 | **Lite-only**（PG 检索面更窄） |
| compaction（5 阶段管线） | 在（经 `MemoryStore` ABC，作用于抽取节点） | 在（同左，后端无关） | **parity**；语义注记：raw paragraph 不参与 prune（治理衰减审计：约束经 compaction 后在库与检索面均存活） |
| consolidation（F3 晋级/遗忘） | 在（默认关；需要 `store._paragraphs`） | no-op：PG 侧无 `_paragraphs`，pass 返回 "no paragraph layer exposed" 报告而非报错 | **Lite-only** |
| entity hubs 维护通道 | 在（默认关；需要段落层推断会话） | no-op（同样依赖 `_paragraphs`） | **Lite-only** |
| premise 段落戳过滤 | 在（检索腿过滤 `premise_superseded`） | 维护 pass 可基于 `list_*` API 运行于两后端；段落侧戳（`_stamp_paragraphs`）仅 Lite 生效 | **parity（节点级）/ Lite-only（段落级）** |

### 已知差异的语义影响

1. **PG 无 trust 惩罚/配额**：低信任（tier<=2，如外部网页内容）在 PG 检索排序中
   不受抑制、不占位受限。防投毒纵深（惩罚 + 配额 + 写侧 cap + premise 过滤）
   目前只在 Lite 兑现；PG 部署的对应边界只剩 RLS 租户隔离与写路径本身。
2. **PG 无 paragraph 检索**：`source_paragraphs` 引用在 PG 上只能按 id 精确取回
   （`get_paragraph`），问句式召回不会命中原文段。基准上段落主导融合带来的
  召回差距（parity 探针 0.376→0.767，提交 `b9dbe15`）在 PG 后端不可复现；
   同一批数据"Lite 可检索、PG 检索不到"。
3. **PG 检索只覆盖 Function 表**：Fact/Preference 在 PG 上不经全文/语义检索命中，
   只能走精确 API（`list_facts`/bi-temporal 读）。跨后端迁移时召回面会收窄。
4. **RLS 执行点不同**：PG 在数据库层强制（绕过应用的直连读也被拦），Lite 在
   进程内（AuthorizationGate）。可见性四要素（tenant/user/workspace/session +
   source_agent）的语义合同两端一致（0007 policy 与 `hot_entry_visible` 对齐），
   但"谁执行"不同决定了威胁模型不同。
5. **paragraph 不进同步事件**（Stage 3 之前）：Lite↔PG 同步拓扑里原文层不复制，
   段落数据只存在于写入侧后端；依赖段落层的维护通道（consolidation、entity
   hubs）在同步对端自然退化为 no-op。
6. **consolidation/entity hubs 是 Lite-only**：PG 部署没有 episodic→sustained
   晋级与算法性遗忘，也没有跨会话实体枢纽；sleep-time 在 PG 下这些步骤返回
   no-op 报告（记录、不报错）。
