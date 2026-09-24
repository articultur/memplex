# 保真审计：原生文本在 Memplex 存储层中的地位（2026-09-24）

> 对应 [capability-directions 报告](../../deep-research-report-capability-directions-2026-09.md) F3
> （原生保真 vs 压缩张力）的落地审计。结论：**报告推断"lite pair + JSON journal
> 已是原生权威"不成立**——产品持久层是全抽取派生层，原生文本只存在于
> benchmark harness 侧。

## 审计问题

派生视图（compaction / wiki / 摘要）能否回溯到原话？原生 turn/段落文本
是否作为权威层持久化？

## 逐层证据

### 1. 段落原话：不持久化（P0 缺口）

- `Paragraph.raw_text`（`memplex/models/paragraph.py:25`）只在抽取管线
  内存中存在：`core/engine.py:397` `_extract_paragraphs` 构造、消费后丢弃。
- lite 持久对（`storage/lite/store.py` `_raw_memory`）只序列化四类 typed
  节点（Function/Fact/Preference/Observation）+ edges + sync 状态；
  PostgreSQL 后端同样无段落表（`storage/postgres.py` 无 raw_text 写入）。
- **产品与评测的保真度鸿沟**：LongMemEval J=0.916 的 harness 用数据集
  侧会话原文做检索单元；产品 `query(orchestrated=True)` 检索的是抽取
  节点。对等探针 0.797 vs harness 0.8045 的差距根源之一在此。
  v13 教训"槽位有限时排名好的单元 ≠ 含答案文本的单元"（摘要三设计
  全否决）在存储面的对应物就是：我们根本没有"含答案文本的单元"可排。

### 2. source_paragraphs：悬空指针（dedup 侧处理正确）

- `Function.source_paragraphs: list[str]`（`models/memory.py:66`）存段落
  id（如 `text:para_001`），但被引用的段落对象不落盘——引用无法解析。
- dedup 合并**保真正确**：`retrieval/dedup.py:340-342` 合并幸存者时对
  source_paragraphs 做并集去重；`compaction.py:_sync_merged_fields` 同步
  写回。指针不丢，但指向虚空。
- wiki 编译把指针渲染为展示文本（`wiki/compiler.py:109-110`
  `**Source Paragraphs:** [...]`），无解析路径。

### 3. compaction 五段管线的删除面

| 阶段 | 行为 | 保真影响 |
|---|---|---|
| extract | no-op 计数（应用层接线 LLM） | 无 |
| dedup | 语义重复真删（delete_ids），幸存者合并字段+指针并集 | 被删节点的 FieldValue 句丢失；指针并集保住 |
| summarize | 超限 FieldValue 标 `deprecated`（按 weight×observation 排序） | 低分抽取句进入待删 |
| prune | 删 deprecated FieldValue；整节点按 confidence/age/access 删除 | **FieldValue.desc 是最接近原话的持久单元，删除即永久丢失**（原话无处可查） |
| archive | 完整 Function 体 JSON 到 `~/.memplex/archive/` 后软删 | 可回溯到节点，仍非原话 |

### 4. 评测口径对照

benchmark 侧的原生检索单元（session fulltexts、a11y 投影）来自数据集
文件（`scripts/run_lme_official_j.py` collect_context），不经 store——
**评测测不出产品存储的这个缺口**，只有产品路径受影响。

## 结论与修复方向

1. **P0**：原生段落文本应成为持久权威层（write_text 时原文落盘，新
   节点类型或 sidecar），派生节点经 source_paragraphs 实指针指向它。
   这与 ADR-012 Phase B 的 schema 决策窗口重合——**应与 provenance/
   信任级 schema（A1）同窗设计**，一次定形。
2. **P1**：prune/archive 对含 FieldValue 的删除应受"原话可回溯"约束
   （原话层落地后自然满足）。
3. 指针并集、archive 全体序列化等既有行为正确，无需改动。

## 关联

- ADR-012（Lite SQLite v2 Phase B schema 窗口）
- F3（压缩张力）、F10（流式评测——本缺口正是离线/产品口径差异的实例）
- v13 负结果档案：摘要三设计否决（`docs/evidence/g003-lme500-official-j-v8`
  系列 manifest）
