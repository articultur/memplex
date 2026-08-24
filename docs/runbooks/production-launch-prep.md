# 生产上线准备 Runbook（2026-08-24 本地全量演练版）

本 runbook 记录在本机（macOS arm64、无系统 PostgreSQL）对 G002–G009 全门禁的
端到端演练结果与生产执行顺序。所有"本地已验证"条目均附真实命令与输出；
生产部署仍必须提交自身绑定部署身份的签名证据（门禁 fail-closed，不可豁免）。

## 0. 基线（演练前 readiness 输出）

| 检查 | 演练前 | 说明 |
|---|---|---|
| production_profile | fail | profile=development |
| production_storage | fail | backend=lite，生产需 postgres + 双 DSN |
| principal_tenant_acl (G002) | fail | principal registry missing |
| G003–G009 | blocked | 无任何签名证据 |

## 1. 环境前置（本地演练踩过的全部坑）

1. **pgserver 仅支持 cp311/cp312**：项目默认 .venv 是 3.13，PG 相关验证必须用
   独立 3.12 环境：
   `UV_PROJECT_ENVIRONMENT=/tmp/memplex-pg-venv uv sync --locked --python 3.12 --extra dev --extra pgtest`
   （CI 的 test-postgres/security 任务同用 3.12，见 ci.yml 注释）
2. **macOS `/tmp` 是指向 `/private/tmp` 的 symlink**：证据写入器逐组件
   `O_NOFOLLOW` 打开父目录，经 symlink 路径一律拒绝。所有 `--evidence-output`、
   `--workdir` 必须用 `/private/tmp/...` 真实路径（Linux 生产无此问题）。
3. **G009 `--workdir` 必须预先存在**且非 symlink（`ValueError("workdir invalid")`
   被 fail-closed 吞掉，排查时注意）。
4. **迁移/备份只接受已解析 TCP DSN**：Unix socket 连接被拒绝
   （target identity 合同）。pgserver 默认 socket 启动，需改用其捆绑二进制
   手动 `initdb` + `pg_ctl -o "-c listen_addresses='127.0.0.1'"` 启动。
5. **备份执行器经 PATH 查找 `pg_dump`/`pg_restore`**（client/server major 必须一致）：
   `export PATH="<pgserver>/pginstall/bin:$PATH"`。
6. **各门签名密钥格式不同**（混用即 `*_signing_key_invalid`）：
   - G005 `MEMPLEX_BACKUP_HMAC_KEY`：**canonical base64 32B**
   - G006 `MEMPLEX_OPERATIONS_HMAC_KEY`：**canonical base64 32B**
   - G009 `MEMPLEX_CAPACITY_CHAOS_HMAC_KEY`：**64 hex**
   - G003/G004 `MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY`：canonical base64 32B + `MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID`
   - G007 `MEMPLEX_RELEASE_EVIDENCE_KEY`：64 hex
   - G008 `MEMPLEX_HOST_LIFECYCLE_HMAC_KEY`：64 hex
   - 备份还需 `MEMPLEX_BACKUP_KEY_ID`（key rotation 标识）
7. **后台命令经管道时 `$?` 是 tail 的退出码**：verifier 失败会被掩盖，检查输出本体。

## 2. 各门演练结果（本地，2026-08-24）

### G004 可靠同步 — ✅ 原始验证通过
```
verify_g004_reliable_sync.py → 1 passed in 11.96s
100,001 events / 101 pages × 1000 / dead_letters=0 / duplicate_receipts=0
outbox_count=100001, PostgreSQL 16.2 (pgserver), Python 3.12.14
```

### G003/G004 回归面 — ✅ PG 套件通过
```
7 文件（CI 同款清单）: 728 passed, 2 skipped in 99.75s
（2 个 skip 为 pgvector 依赖——pgserver 不含 pgvector；CI 容器补齐）
```

### G009 容量 chaos — ✅ 全量合同规模闭环
```
100,000 functions / 1,000,000 edges / 60s soak / 9 并发
2,857,196 ops（超越工程基准 2,399,619），error_rate=0.0
RTO=0.0016s（合同 ≤30s），verified=true
```
注意：`MEMPLEX_CAPACITY_CHAOS_HMAC_KEY` 缺失时全部 chaos 通过后才在签名步
失败——先配密钥再跑。

### G005 备份恢复 — ✅ 演练闭环 + verifier verified
```
backup: 141,182 bytes (schema g005_rehearsal, migration v6)
DROP SCHEMA（模拟灾难）→ pg_restore --single-transaction 0.10s
drill: closing=true, RPO=0.003s, RTO=0.113s, PITR ready（需 WAL archive 参数）
verify_g005_backup_restore.py → status=verified
```
**但发现 P1 缺陷，见 §3。**

### G006 运维 SLO — ✅ 演练闭环 + verifier verified
```
生产 profile uvicorn（postgres backend + principal 注册表 + 双 DSN）
1,620 请求 / 412s（计数请求 1,080 —— 探针端点不计入 SLO 样本）
SIGTERM → 优雅排空（exit -15 或 0 均接受），shutdown_drained=true
availability=1.0, error_rate=0.0, p95=0.55ms（目标 ≤250ms）
verify_g006_operations_slo.py → verified=true, industrial_gate_closing=true
```
两个演练踩坑（生产注意）：
- **报告写入以 deployment 绑定为前提**：缺 `MEMPLEX_DEPLOYMENT_ID` /
  `MEMPLEX_SOURCE_SHA256` / `MEMPLEX_ARTIFACT_SHA256` /
  `MEMPLEX_TARGET_IDENTITY_SHA256` 任一，SIGTERM 后**根本不写报告**（fail-closed）。
- **探针端点不计入 request_count**：门禁要求计数 ≥1,000（
  `MINIMUM_REQUEST_SAMPLES`），驱动总量需 ≥1.5×（本演练 1,620→1,080）。
- 证据时效 900s：drill 结束后 15min 内必须完成 verify。

### G007 供应链 — ✅ 构建链本地验证
- `release/build-tools.txt` 已从 setuptools 80.9.0/wheel 0.45.1 升级到
  **setuptools 83.0.0 / wheel 0.48.0 / packaging 26.3**（hash 与 uv.lock 对齐；
  修复了 pyproject `>=83.0` 声明与锁定 drift，及 wheel 0.48 新增 packaging
  运行时依赖在 `--no-index --require-hashes` 下必然失败的隐患）
- CI 两步已本地复现：`pip download --only-binary --require-hashes` ✓；
  `pip install --no-index --find-links --require-hashes` ✓
- 用新后端离线构建 sdist+wheel 成功；`test_release_workflows +
  test_reproducible_release` 21 passed
- 正式证据仍须 CI 双构建字节比对 + OIDC attestation

### G008 四宿主 — ⚠️ 本机不可行
本机仅有 claude CLI，缺 codex/openclaw/hermes。必须在
`[self-hosted, macOS, memplex-g008-real-host]` runner 执行（workflow 已钉死）。

## 3. 发现的阻塞缺陷

### P1（已修复）：`storage backup drill/restore` CLI 路径鸡生蛋缺陷

**现象**（修复前）：按文档执行灾难演练——`DROP SCHEMA <memplex-schema>` 后运行
`memplex storage backup drill --target-schema <memplex-schema>`——必然失败。

**机制**：
1. CLI 每次调用都经 `_build_backup_command_context` 重建上下文，其中
   `inspect_target()` 要求 DSN search_path 指向的 schema 的
   `current_schema()` 非 NULL——schema 已 DROP 时直接
   `MigrationIntegrityError("PostgreSQL target identity cannot be inspected")`。
2. 而 `PostgresBackupExecutor.restore()` 要求 target schema **不存在**
   （`postgres_restore_target_exists`）且 `target_schema == DSN schema`。
3. 两者在同一 CLI 调用内不可同时成立。即使给 search_path 加 fallback schema，
   `inspect_application_principal` 的 ACL 精确比对也会对缺失 schema 失败。

**为何测试未发现**：`test_postgres_backup_integration` 直连 executor
（DROP 前构造、DROP 后 restore，绕过 CLI 上下文）；`test_cli_backup` 的
drill 用 mock 上下文。真实 DROP 后的 CLI 路径从未被覆盖。

**演练旁证**：直连 executor + 显式 `ApplicationAclContract("memplex_app")`
的演练完整闭环（restore 0.10s、drill closing=true、verifier verified），
证明备份/恢复机制本身无恙，缺陷在 CLI 编排层。

**已实施的修复**（2026-08-25）：
- `runner.py` 新增 `inspect_postgres_restore_connection_target`：严格检查优先；
  仅当 `current_schema()` 为 NULL（灾后）时，回退到连接的**单条目**
  search_path 解析 pinned schema（拒绝 `$user`/多条目/空值，引号与大小写
  折叠遵循服务器语义）。配套 `PostgresMigrationRunner.inspect_restore_target`
  与 `inspect_restore_application_principal`。
- CLI：仅 `restore`/`drill` 以 `allow_missing_schema=True` 构建上下文；
  `create`/`pitr-status` 保持严格合同。identity 四元组
  (address, port, database, schema) 与 ACL 精确比对全部保留——容错只放宽
  "schema 当前存在"这一条。
- 回归测试：search_path 解析单测 + CLI 接线单测 + 真实 PG 端到端
  （backup → DROP SCHEMA → restore → DROP SCHEMA → drill，全程 CLI）。
另注意：executor 直连时若漏传 `application_acl`，restore readback 的
`schema_fingerprint` 不含 ACL 归一化，会把带生产授权的 schema 判为
"unrecognised legacy schema"——readback 必须始终携带 ACL 合同（CLI 路径
已内建）。

### P2（已修复）：release/build-tools.txt 构建后端 drift
见 §2 G007。setuptools 80.9.0 低于 pyproject 声明的 >=83.0（PYSEC-2026-3447）。

### P3（低危）：admin.html 部分字段未 escape
`operations_assets/admin.html` render() 中 memory_type/updated_at 未过
escapeHtml（id 形状受系统约束，detail 用 textContent 安全）。

## 4. 生产执行顺序（签署证据 → readiness 转绿）

前置：production profile + postgres + 双 DSN（application/migration 不同 role）+
PUBLIC schema 权限回收 + `_APPLICATION_ACL` 精确授权（本 runbook 演练 SQL
可直接复用：七档表权限分组 + 2 sequence USAGE + 5 函数 EXECUTE）+
`MEMPLEX_PRINCIPALS_JSON`（digest-only）+ 各门密钥注入平台 secret。

1. CI 发版：tag `v*` → release workflow（双构建字节比对 → attest → G008 →
   PyPI → npm）→ 得 G007 证据
2. 部署目标初始化：migrations status/plan → apply --dry-run → apply →
   ACL remediation（GRANT 后 status=ready）
3. 生成部署绑定：`MEMPLEX_DEPLOYMENT_ID` / `SOURCE_SHA256` /
   `ARTIFACT_SHA256` / `TARGET_IDENTITY_SHA256`
4. G003：PG 迁移/存储验证 → sign_industrial_gate_evidence（15min 有效）
5. G004：verify_g004_reliable_sync → 签名（15min）
6. G005：backup create → drill（P1 已修复，CLI 直连可用）→ verify_g005
7. G006：真实 uvicorn 跑 ≥5min/≥1000req/≥128 samples → SIGTERM drain →
   verify_g006（绑 deployment）
8. G008：self-hosted runner 全量（24h 有效）
9. G009：verify_g009_capacity_chaos 全量 → evidence
10. `memplex readiness --strict` 全绿 → 开流量；探针/告警按
    deploy/prometheus/memplex-alerts.yml 接入

## 5. 复跑入口（本地演练资产）

- 3.12 PG 环境：`/tmp/memplex-pg-venv`（uv sync 可重建）
- G005 演练脚本：`/private/tmp/g005-setup.sh`（initdb+迁移+授权+备份）
- G006 演练脚本：`/private/tmp/g006-drill.py`（uvicorn 起服+压测+SIGTERM）
- 各门原始报告：`/private/tmp/g004-report.json`、`/private/tmp/g009-evidence.json`、
  `/private/tmp/g005-drill-report.json`、`/private/tmp/g006-report.json`、
  `/private/tmp/pg-suite-junit.xml`
- 演练密钥均一次性生成、仅存 /tmp，生产密钥必须经平台 secret 管理重新生成
