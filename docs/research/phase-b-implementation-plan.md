# ADR-012 Phase B 实现蓝图（2026-09-26）

> 状态：开工蓝图（文件级序列，供实现直接消费）。前置状态：Phase A
> 影子写已落地（d24aea3）并积累两轮全量 shadow 套件零失败
> （3,868 过 × 2，含 diff-EQUAL 门禁）；ADR-013 的 trust_tier 与
> paragraphs 已进 pair schema——**schema 冻结条件满足**，Phase B
> 迁移窗口开启。保真靶处置（选项 A/B）不阻塞本线：两条分支下
> Phase B 都是不可动摇的主线。

## 实现序列（按提交粒度，每步全门禁 + 可回退）

### B1. SQLite 读取权威（读翻转，写仍 JSON）— 先立读路径

- `storage/lite/sqlite_v2.py`：新增 `AuthoritativeReader`——从
  `shadow_v2.sqlite3` 构造 `LitePair`（memories 四类 + paragraphs 按
  kind 反序列化、change_log 事件流、meta.generation）；与
  `_decode_pair` 同构的校验（键集/类型/digest）。
- `storage/lite/durability.py`：`MEMPLEX_LITE_SQLITE_AUTHORITY=read`
  时 `_load_authoritative_locked` 先试 SQLite（缺库/版本不符回退
  JSON 并 warning）。
- 验收：`test_authority_read_reconstructs_pair`（shadow 写入 → 重开
  读权威 → resident 与 JSON 加载逐节点相等）；shadow diff 在
  read-authority 模式下仍 EQUAL。
- 风险：低（只动读路径，flag 默认关）。

### B2. SQLite 写权威 + JSON 快照导出（写翻转）

- `sqlite_v2.py`：`AuthoritativeWriter`——B1 reader 复用 + 增量行
  mutation（upsert/delete by id、change_log append、meta 代际推进）
  取代 Phase A 的 replace-all；事务 = `BEGIN IMMEDIATE ... COMMIT`
  + `synchronous=FULL`。
- `store.py`：`_commit_current_state` 在
  `MEMPLEX_LITE_SQLITE_AUTHORITY=rw` 下走 SQLite 主事务，JSON pair
  每 N 代（默认 8）快照导出（沿用现 journal 原子写序列）。
- 回退：flag 回 `json` = 现行路径原样保留一个 release。
- 验收：§5 六项门槛的本地预演——10k 文档播种 ≥85 docs/s、崩溃
  注入（事务中 kill -9 重开一致）、迁移等价（JSON→SQLite 全量
  diff 100%）、全量 lite + shadow 双套件零回归。
- 风险：中（写路径核心）；deferred_commit 批语义映射为单事务，
  成功前缀语义需要在事务回滚边界重新证明。

### B3. PG paragraphs 表 + 同窗迁移

- `storage/migrations/_constants.py` 常量先行（AGENTS 规矩），新增
  `paragraphs` 表迁移；`postgres.py` 持久化接入 service.write 的
  duck-typed 挂点（现 no-op 点）。
- 验收：PG 套件 + 17 方法契约零回归（不新增方法，paragraph 走
  既有 upsert 面）。

### B4. 单写者队列（构造性消灭双线程 durability 死锁）

- 写 mutations 进专职写者线程的队列；embedding/抽取在队列外。
- 验收：§5 死锁项——双线程压力（主线程批量写 + worker 轮询 +
  并发读）30 分钟零阻塞；v13.1 复现场景归零。
- 注：B2 的 RLock 串行化已缓解（df1d46d），B4 是构造性收尾。

## 明确顺序依赖

B1 → B2 →（B3、B4 可并行 B2 之后）；shadow 模式在 B2 后转为
"读 JSON 写双库"的对拍模式直至 Phase C 移除。

## 验收门槛映射（ADR-012 §5）

| 门槛 | 蓝图步 |
|---|---|
| ≥85 docs/s @100k | B2（O(batch) 行插入） |
| 死锁 30min 零阻塞 | B4 |
| 崩溃注入零丢失 | B2 |
| 迁移 100% 等价 | B1+B2（diff 工具已备） |
| 双套件零回归 | 每步 |
| content_hash 篡改检出 | B1 reader 校验 |
