# Sync 语义边界：durable outbox 与 legacy best-effort（G001 P1-4）

日期：2026-09-04。本文回答 G001 优先缺口 P1-4：「明确 durable outbox 与
legacy best-effort 的产品边界、迁移路径和丢失语义」。

## 两条同步通路的边界

| 面 | durable 同步（`storage/postgres_sync.py`、`storage/lite/sync_repository.py`） | legacy HTTP 同步（`sync.py`、`sync_dispatcher.py`） |
| --- | --- | --- |
| 一致性承诺 | **at-least-once + 幂等应用**：事件经 durable inbox/outbox 持久化，重复与乱序收敛到相同状态（17 方法 `AbstractSyncRepository` 锁步契约） | **best-effort**：内存队列派发，进程退出或队列满可**丢弃任务**，无跨重启投递承诺 |
| 丢失语义 | 不丢失：事件先落库后派发；消费端按依赖序应用（edge tombstone → node tombstone → upsert） | 可丢失：`sync.py` 的 legacy 队列在背压/关闭时丢弃；丢弃不可审计 |
| 排序保证 | 单次发布内按依赖序提交（lite `_commit_sync_changes`）；跨发布按序号收敛 | 无跨发布顺序保证 |
| 适用场景 | 生产、多副本、需要审计与恢复的部署 | 单机开发回环演示（G004 的 loopback 即此通路） |

## 迁移路径

1. **现状**：两条通路并存；生产 profile 下 durable 是缺省（配置层
   `sync.enabled + targets` 走 dispatcher，durable 由 PG 后端的 sync
   repository 承载）。legacy 仅为开发回环与历史 API 兼容保留。
2. **建议演进**（未实施，属产品决策）：在 `sync_dispatcher` 中将
   legacy 队列标记 deprecated（warning 日志），把 HTTP 回环演示切到
   durable 通道的本地模式；下个大版本移除 legacy 队列的丢弃路径。
3. **不可混称**：文档与对外叙述不得把 legacy 回环演示描述为
   "durable sync"；G001 禁止性声明持续有效。

## 验证锚点

- durable：`tests/test_sync_repository_contract.py`（15 项 PG 全绿），
  `tests/test_lite_sync_repository.py`（合并/清除/依赖序回归），
  `tests/test_g004_sync_real_loopback.py`（真实 TCP 回环）。
- 丢失语义的负向面：legacy 丢弃路径目前**无**专门的丢弃审计测试——
  这就是它必须保持 best-effort 标签的原因；补审计测试属于移除
  legacy 前置条件。
