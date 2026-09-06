# Memplex 生产运行 Runbook

本 runbook 适用于 `deployment.profile=production`、PostgreSQL backend、独立 application / migration / inbound 身份的部署。任何步骤都不得把 DSN、token、principal registry、业务 payload 或原始数据库异常复制到工单、告警注释或聊天记录。

## 启动与探针

- `GET /health/live`：只证明进程事件循环仍能响应，固定返回 `200 {schema_version:1,status:live}`；它不证明数据库、worker 或同步可用。
- `GET /health/ready`：只有 runtime lifecycle 为 `ready` 且 storage resource 保持 READY 时返回 200；starting、draining、stopped、faulted 一律 503。
- 编排平台应把 liveness 与 readiness 分开配置。readiness 连续失败时先停止流量，不要通过重启循环掩盖数据库或权限故障。
- `/health` 是认证后的诊断兼容面，不用于 orchestration；输出不包含 storage path 或异常原文。

## readiness unavailable

1. 确认 `/health/live` 是否仍为 200。
2. 若 live=200、ready=503，停止新流量并查看固定 lifecycle/storage 状态。
3. 检查 PostgreSQL application/migration/inbound 目标、ACL、RLS、连接池和 migration status；不要在业务进程内自动 GRANT 或 apply 未审迁移。
4. 如果 lifecycle 已进入 `faulted`，该进程不会重新发布 ready；修复依赖后滚动替换实例。

## error budget burn

1. 对照 `memplex_http_requests_total{status_class="5xx"}` 和 request histogram，确认不是 4xx 客户端错误。
2. 同时查看 pool saturation、worker/sync backlog 与 DLQ；不要按 tenant、subject 或 memory id 扩展 Prometheus label。
3. 在错误率回落前暂停扩张流量；如需回滚，使用上一份已验证发布工件，G007 完成前不得声称供应链回滚已工业化。

## latency

1. 使用固定 histogram 计算 p95；指标 endpoint 不执行全表/全图扫描。
2. 先排查 pool leases/max、PostgreSQL slow query、worker backlog 和远端同步，再调整实例数。
3. 不直接提高 pool/queue 上限来掩盖阻塞；所有容量更改需重新执行 G009 容量门禁。

## worker backlog

1. 查看 `memplex_worker_pending`、`memplex_worker_leased`、`memplex_worker_dead_letters`。
2. 确认 worker lifecycle 正常、lease 会到期恢复、任务未被 admission hard limit 拒绝。
3. DLQ 只通过现有显式 replay 接口处理；记录固定错误码，不复制任务 payload。

## sync backlog

1. 查看 `memplex_sync_pending`、`memplex_sync_leased`、`memplex_sync_dead_letters`。
2. 检查 peer HTTPS、认证 principal、no-echo 与远端 inbox 幂等状态。
3. 优先 drain/replay 单个稳定 target/event identity；不要把 URL 或凭据写入日志。

## dead letters

1. 暂停相关自动流量，确认失败是 terminal 还是 lease/retry 可恢复。
2. 使用 `memplex sync dlq list` 与显式 replay；worker DLQ 按任务管理接口处理。
3. 连续失败应升级处理，不能通过删除 DLQ 伪造健康。

## pool saturation

1. 比较 `memplex_pool_business_leases`、`memplex_pool_high_watermark` 与 `memplex_pool_max_connections`。
2. 检查请求是否释放 cursor/transaction，确认 close/fault 没有 token 泄漏。
3. 若 PostgreSQL 已达连接上限，先降 admission/扩实例，再评估数据库容量；不要让 application role 获得 admin 权限。

## shutdown deadline

1. 收到 SIGTERM 后 readiness 必须立即 503；liveness 保持 200 直到进程退出。
2. 新业务请求应固定 503；已 admission 请求在 `request_drain_timeout_seconds` 内完成。
3. 随后依次 drain sync、worker 并关闭 pool。`memplex_shutdown_deadline_exceeded_total` 增长必须告警。
4. deadline exceeded 时不要把进程重新置 ready；依赖 durable lease/outbox 由替换实例恢复。

## 备份与恢复联动

- operations 告警不能替代 G005。执行恢复前先验证签名 artifact；目标 schema 必须不存在。
- 恢复后运行 exact migration/readiness 与签名 G005 drill verifier，再逐步恢复流量。

## SLO 证据

HTTP 进程可在 shutdown 时通过 `MEMPLEX_G006_REPORT_OUTPUT` 写入一份原子替换的 schema v2 签名 report；签名 key 来自 `MEMPLEX_OPERATIONS_HMAC_KEY`，key id 来自 `operations.report_key_id`。发布系统还必须显式注入当前部署绑定：`MEMPLEX_DEPLOYMENT_ID`、`MEMPLEX_SOURCE_SHA256`、`MEMPLEX_ARTIFACT_SHA256` 与 `MEMPLEX_TARGET_IDENTITY_SHA256`。任一项缺失或不是规范 SHA-256 时，HTTP 进程 fail closed：不写 report，只记录不含输入值的固定告警事件。报告同时包含 `generated_at`；只有观测窗口不少于 5 分钟、请求不少于 1000、latency samples 不少于 128、所有阈值和完整 drain 均通过，才会设置 `industrial_gate_closing=true`。

```bash
memplex --output json operations alerts-check
memplex --output json operations verify-report /secure/evidence/g006-operations.json
python scripts/verify_g006_operations_slo.py --report /secure/evidence/g006-operations.json
export MEMPLEX_G006_OPERATIONS_REPORT=/secure/evidence/g006-operations.json
memplex --output json readiness --strict
```

`operations verify-report`、独立脚本与 readiness 都按当前统一部署绑定和
`operations.report_key_id` 验证报告，并拒绝超过 15 分钟、短窗口、少样本或跨部署证据；公开输出不会
回显报告路径、部署标识、摘要或签名 key。

G001-G009 工程验收已经完成，但任一部署缺少当前有效的 G007-G009 或其他机器证据时，
整体仍为 `not_ready`；只有全部门禁同时通过才报告 `ready / industrial`。

### 本地生成一份合格签名 G006 报告（2026-09-06 实测 runbook）

一份能通过 `verify_readiness` 的报告必须同时满足硬阈值：
**观察窗口 ≥ 300 秒、请求 ≥ 1000、延迟样本 ≥ 128、可用性 ≥ 0.999、
错误率 ≤ 0.001、p95 ≤ 250ms、优雅排空且未超停机期限**。步骤：

1. 环境变量（全部必填）：
   `MEMPLEX_OPERATIONS_HMAC_KEY`（base64 of 32 bytes，签名与验证必须同一把）、
   `MEMPLEX_SOURCE_SHA256` / `MEMPLEX_ARTIFACT_SHA256` /
   `MEMPLEX_TARGET_IDENTITY_SHA256`（各 64 hex，部署绑定）、
   `MEMPLEX_DEPLOYMENT_ID`、`MEMPLEX_OPERATIONS_REPORT_KEY_ID`
   （非空；与 config `operations.report_key_id` 一致）、
   `MEMPLEX_G006_REPORT_OUTPUT`（**输出路径的每一级都不能是符号链接**——
   `_open_pinned_parent` 带 `O_NOFOLLOW`，macOS 的 `/tmp` 会被拒绝，
   用 `/private/tmp/...`）。
2. 进程内起 uvicorn（`create_app()`），就绪门控后**单连接顺序**打满
   1600+ 个请求（并发压测会把本机回环 p95 推到 500ms+ 而触发 SLO 拒绝），
   保持窗口 310 秒后 `should_exit = True` 优雅停机——报告在停机钩子里写出。
3. 同一环境（同一把 HMAC key）下
   `python scripts/verify_g006_operations_slo.py --report <path>`。

2026-09-06 实测产出：`report_id 0a53692b…`，1661 请求、p95 5.77ms、
可用性 1.0，签名/binding/alert-rules 哈希全部通过（本地审计证据，
按部署绑定归档，不入库）。
