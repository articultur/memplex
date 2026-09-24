# 设计提案：provenance 信任级 + 原生文本权威层（2026-09-24）

> 状态：提案（待用户确认后升 ADR-013 并入 ADR-012 Phase B schema）。
> 来源：capability-directions 报告 F2/F3 + [保真审计](fidelity-audit-2026-09.md)
> 的 P0 发现。**两个问题必须在同一个 schema 窗口定形**：信任级要挂在
> 节点上，原话层要成为节点——ADR-012 Phase B 权威翻转之前是唯一
> 零迁移成本的时机。

## 1. 动机（三条证据汇合）

1. **投毒面**（F2）：`llm/injection_guard.py` 是检测计数，不保证"不可信
   来源经合并/巩固不被放大成看似可信的用户历史"（provenance
   laundering，PPMF）。四宿主 recall-before-turn 恰是"编排提示优先"结构。
2. **保真缺口**（C1 审计）：产品持久层是全抽取派生层，段落原话不落盘，
   `source_paragraphs` 悬空。评测 0.916 依赖 harness 侧原文，产品
   orchestrated 探针 0.797——存储面缺"含答案文本的单元"。
3. **时机**：ADR-012 Phase A 已落地，Phase B 翻转前改 schema 零迁移；
   provenance 标签若不同步进 peer-mesh 契约，对等节点间会漂移。

## 2. 信任级模型

### 2.1 分层（写入时判定，节点一等公民字段 `trust_tier`）

| tier | 值 | 来源 | 判定规则 |
|---|---|---|---|
| `user_direct` | 4 | 用户在交互中的直述 | write_text 且 source_type 标记为 user |
| `session_derived` | 3 | 会话内抽取/摘要（assistant 产物） | 抽取管线默认产出 |
| `external_web` | 2 | 外部内容（网页、工具输出、抓取） | source_type=web/tool |
| `agent_inferred` | 1 | agent 推断/合成（枢纽、跨会话结论） | EntityHubs / improve 产物 |

缺省（旧数据/未标注）= `session_derived`（3）：向后兼容，不诬赖旧记忆
也不抬高。

### 2.2 权威不放大规则（核心不变量）

- **合并取 min**：dedup/巩固合并时，幸存节点 trust_tier = min(参与者)。
  不可信内容不能借合并洗白（PPMF 的 provenance laundering 防线）。
- **派生取 min**：摘要/枢纽/wiki 等派生视图的 tier = min(源集合)——
  与既有 ACL 血缘（bind_derivation_lineage 的"创建时最严钳制"）同构，
  实现上可复用同一派生遍历。
- **检索呈现**：SearchResult 携带 tier；编排层可用"低于阈值的记忆
  不得进入高权限工具调用"（本期只透出字段，绑定授权留后续）。

## 3. 原生文本权威层

- `write_text` 的段落原文持久化为新 typed 类别 `paragraph`
  （`raw_text`、source 元数据、`trust_tier`、`created_at`）；抽取管线的
  `ParagraphCollection` 在提交时随 typed 节点一同落盘。
- 现有 `source_paragraphs` 由悬空 id 变实指针（id 命名不变，
  如 `text:para_001`）。
- **检索面**：paragraph 节点参与向量 + FTS 检索——把 J 分 harness 已
  验证的"含答案文本单元"能力交还给产品路径（对等探针 0.797 →
  预期向 harness 0.8045+ 收敛，作为 Stage 1 的量化验收）。
- **压缩边界**：paragraph 不参与 prune（权威层只增；显式删除/TTL 除外）。
  容量对策：段落级 hash 去重；远期 archive 仍保留指针目标不删。
- 与 compaction 的关系（C1 审计 P1）：FieldValue/prune 删除后原话
  可回溯——自然满足。

## 4. Schema 变更清单

| 层 | 变更 |
|---|---|
| lite pair | `_raw_memory` 新增 `"paragraphs"` 区；四类节点新增 `trust_tier`（缺省 3） |
| SQLite v2 | `memories.kind` 增 `'paragraph'`（payload 含 raw_text/trust_tier）；shadow diff 工具同步扩展 |
| PostgreSQL | `paragraphs` 表 + 节点表 `trust_tier` 列（v2 迁移，`storage/migrations/_constants.py` 常量先行） |
| peer-mesh | MeshObject payload 带 trust_tier；对象分治冲突表加"合并取 min"规则；`SyncNodeType` 增 `PARAGRAPH`（复用现有 upsert/delete 操作，**不新增同步方法**，契约测试扩展枚举即可） |

peer-mesh 对齐成本量化：契约测试 3 文件扩展 + 冲突表一规则，无新方法
（避开 17 方法锁步扩容）。

## 5. 评测与验收

1. **A2 红队基线先行**（不依赖本 schema 实现）：间接注入 → 摘要污染 →
   跨会话存活的 ASR 基线数字，为防御提供靶子。
2. Stage 1 落地后重跑红队：ASR 下降幅度 = 防御收益（合并取 min +
   检索层 tier 透出）。
3. 保真收益：产品 `query(orchestrated=True)` 对等重跑（0.797 基线
   在账），paragraph 层上线后收敛目标 ≥0.8045。

## 6. 阶段

- **Stage 1**（schema 冻结后）：lite 段落持久化 + trust_tier 字段 +
  实指针 + 检索接入（含 shadow diff 扩展与契约测试）。
- **Stage 2**：peer-mesh 契约扩展 + 合并取 min 规则进 dedup/派生遍历。
- **Stage 3**：A2 红队防御重测 + 保真对等重跑，产出 E1 证据。

## 7. 风险

- 段落全量持久化的容量增长（benchmark 50k turn 级语料 ×2 存储）——
  接受：权威层不压缩是 F3 的明确结论；去重 + 远期 archive 缓解。
- 信任级判定依赖 source_type 标注质量——write_text 调用方（四宿主）
  需正确标注；缺省 3 保证不炸。
- 检索面新增高召回单元可能挤占 top-k 槽位（v13 槽位教训）——
  paragraph 与抽取节点同池排序，靠既有融合归一化处理，验收项 3 覆盖。
