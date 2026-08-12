# 生产支持合同与工业验收门

Memplex 默认本地配置仍是 `Developer Preview / single-machine Beta`。项目已经完成
G001-G009 工业化工程验收，但这不自动使任意部署成为生产就绪：只有生产 PostgreSQL
拓扑为当前版本提交全部有效机器证据，并由 `memplex readiness --strict` 同时验证通过时，
该部署才报告 `ready / industrial`；缺失、过期或无效证据一律保持 `not_ready`。

## 支持拓扑

- 生产：一个或多个 Memplex 服务进程，共享 PostgreSQL 持久层。
- 本地开发与测试：Lite，仅支持单进程；不承诺多线程、多进程或多机写入安全。
- 四宿主：Codex、Claude Code、OpenClaw、Hermes 通过同一受认证服务共享工作区记忆。

生产配置必须显式设置：

```yaml
deployment:
  profile: production
storage:
  backend: postgres
  # 应用运行身份：仅业务表所需的最小权限，不能是表 owner、superuser 或 BYPASSRLS。
  path: postgresql://memplex_app@db.example/memplex
  # 迁移身份：仅用于受控 DDL/迁移验证；不要复用给应用连接池。
  migration_dsn: postgresql://memplex_migrator@db.example/memplex
```

在每个受管 schema 上，operator 必须先移除默认 PUBLIC schema 权限，再只向 application
role 授予 `USAGE`；`CREATE` 不得授予应用身份。仅真实 schema owner 的固有 entry 可以保留；
任何非 owner 的 `pg_database_owner` grant 也会被拒绝：

```sql
REVOKE USAGE, CREATE ON SCHEMA <memplex_schema> FROM PUBLIC;
GRANT USAGE ON SCHEMA <memplex_schema> TO memplex_app;
```

也可通过环境变量配置，环境变量优先于 YAML：

```bash
export MEMPLEX_STORAGE_PATH='postgresql://memplex_app@db.example/memplex'
export MEMPLEX_STORAGE_MIGRATION_DSN='postgresql://memplex_migrator@db.example/memplex'
```

生产启动会在建池/DDL 前要求两条 DSN 均为可解析、无前后空白的精确 PostgreSQL DSN，
并拒绝相同 canonical 连接身份；连接后还会比较真实 `current_user/session_user`，即使使用不同
connection options 伪装同一 role 也会 fail-closed。application 与 migration principal 必须完全分离。

若 `deployment.profile=production` 仍选择 Lite、缺少/无效/复用任一 DSN、YAML 解析失败、`PUBLIC` 获得
受管 schema 的任何权限，或
`storage` 出现未知字段（例如 `migration_dns`），服务会在创建任何连接前拒绝启动。错误、
readiness 证据与配置对象表示不会回显 DSN、密码或查询参数；应通过部署平台的 secret 注入
上述值，而非日志、命令行或诊断输出。

## G002：Principal、租户与身份合同

生产环境的 HTTP、CLI、MCP、Sync，以及 Codex、Claude Code、OpenClaw、Hermes 四个
宿主，必须使用同一个 server-trusted `principal` 合同。身份不是模型工具参数、请求
payload、`owner` 字段或宿主事件中可伪造声明的推导结果。

服务端通过 `MEMPLEX_PRINCIPALS_JSON` 配置非空 credential registry。每个条目至少包含
`credential_id`、`token_sha256`、`tenant_id`、`subject_id`、`workspace_id`；可选
`agent_id` 和 `roles`。registry **只保存 token 的 SHA-256 十六进制摘要，不保存原始
secret**。原始 secret 只能由客户端进程的 `MEMPLEX_PRINCIPAL_TOKEN` 提供，不能写入
配置文件、memory、日志、诊断报告或安装器输出。

示例中的摘要是占位符，不能当作可用凭据：

```bash
export MEMPLEX_PRINCIPALS_JSON='[
  {"credential_id":"ci-a","token_sha256":"<64-hex-sha256>",
   "tenant_id":"tenant-a","subject_id":"alice","workspace_id":"workspace-a",
   "agent_id":"codex","roles":["memory-user"]}
]'
export MEMPLEX_PRINCIPAL_TOKEN='<raw-secret-only-in-client-environment>'
```

生产中 credential registry 缺失、为空或格式非法时，认证入口必须 fail-closed；不得退回
共享 API key、匿名 `default` 身份或 payload 中的 `owner`。受管安装身份优先于环境变量和
hook/tool payload；它是防止同一受管进程被普通输入覆盖的边界，并不是抵御同 UID 的本机
恶意进程的安全边界。`local-process` 身份只适用于 development 且同 UID 的本机开发，不能
作为多租户或对手模型下的隔离承诺。

可见性必须精确绑定到已认证 principal：`user` 是同 tenant + subject，`workspace` 是同
tenant + workspace，`session` 还要求同 agent + session。没有 tenant provenance 的 legacy
记录在生产读取、更新、同步和反馈中一律 fail-closed；不可把兼容读取当作跨租户回退。

## 机器可读门禁

```bash
memplex --output json readiness
memplex --output json readiness --strict
```

普通命令用于审计，未就绪时仍返回报告；`--strict` 只有全部必需门禁通过才返回 0。
当前 `principal_tenant_acl` 门禁在且仅在 `production + postgres + 可解析的非空
MEMPLEX_PRINCIPALS_JSON` 时为 `pass`；格式错误会作为不泄露配置内容的 `fail` 报告。
这只证明 G002 的部署合同已满足；整体状态仍取决于其余全部机器门禁。

G003 迁移/存储完整性与 G004 可靠同步/背压的工程验收不再被当作任意部署的静态 `pass`。
两个门禁都必须提交当前部署的签名 evidence；缺失时为 `blocked`，报告存在但过期、篡改、
gate 互换、key id 或下列任一绑定不一致时为 `fail`：

```bash
export MEMPLEX_DEPLOYMENT_ID='prod-cn-1-20260812'
export MEMPLEX_SOURCE_SHA256='<64 lowercase hex>'
export MEMPLEX_ARTIFACT_SHA256='<64 lowercase hex>'
export MEMPLEX_TARGET_IDENTITY_SHA256='<64 lowercase hex>'
export MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID='industrial-2026-08'
export MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY='<canonical-base64-32-byte-secret>'
export MEMPLEX_G003_STORAGE_REPORT='/secure/evidence/g003-storage.json'
export MEMPLEX_G004_SYNC_REPORT='/secure/evidence/g004-sync.json'
```

同一组 source、artifact、deployment 和 target identity 也是 G006/G008 当前部署验证的权威输入；
readiness 不从 Git 工作树、文件路径或历史测试计数推断它们。G003/G004 evidence 自生成起最多有效
15 分钟，必须由持有 attestation key 的受控验证流水线在独立 verifier 已通过后签发。本机 unsigned
迁移诊断或 G004 原始 backlog JSON 不能直接关闭工业门禁。

受控流水线可在独立 verifier 已经返回成功后，对其原始结果字节签名：

```json
{
  "schema_version": 1,
  "gate_id": "schema_migrations_atomicity",
  "verifier_id": "memplex-g003-storage-integrity-v1",
  "verifier_contract_sha256": "<current fixed contract sha256>",
  "status": "passed",
  "memplex_version": "<installed version>",
  "source_sha256": "<same as MEMPLEX_SOURCE_SHA256>",
  "artifact_sha256": "<same as MEMPLEX_ARTIFACT_SHA256>",
  "deployment_id": "<same as MEMPLEX_DEPLOYMENT_ID>",
  "target_identity_sha256": "<same as MEMPLEX_TARGET_IDENTITY_SHA256>",
  "started_at": "2026-08-13T00:00:00.000000Z",
  "completed_at": "2026-08-13T00:05:00.000000Z",
  "checks": [
    {"id": "least_privilege_application_acl", "status": "passed", "evidence_sha256": "<64 hex>"}
  ]
}
```

上例仅展示字段形状；`checks` 必须完整包含下述 gate 的全部固定检查，顺序、数量及 digest 都由
签名工具严格验证，不能只提供示例中的一个元素。

```bash
python scripts/sign_industrial_gate_evidence.py \
  --gate-id schema_migrations_atomicity \
  --run-result /secure/results/g003-verifier.json \
  --output "$MEMPLEX_G003_STORAGE_REPORT" \
  --key-id "$MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID"

python scripts/sign_industrial_gate_evidence.py \
  --gate-id durable_sync_backpressure \
  --run-result /secure/results/g004-verifier.json \
  --output "$MEMPLEX_G004_SYNC_REPORT" \
  --key-id "$MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID"
```

签名工具会先严格解析独立 verifier result：必须是 schema v1、`status=passed`、gate 与当前
source/artifact/deployment/target/version 完全一致、固定 verifier contract digest 匹配、结果在 15 分钟内，
且该 gate 的全部必需检查按固定顺序为 `passed` 并携带各自 evidence SHA-256；任意 failed、缺项、extra、
未来 schema、重复 JSON key 或跨部署结果都不会被包装成通过证据。签名仍不替代 verifier 自身执行，持有
attestation key 的流水线负责保证各 evidence digest 指向受控、不可变的原始运行产物。输入、输出及其祖先
符号链接、非普通文件、超限结果或输入输出同 inode 都会 fail-closed，公开输出不含路径、摘要、部署身份或密钥。

G003 固定检查为 migration plan/status/apply、storage crash atomicity、least-privilege application ACL、
PostgreSQL storage regression 和 packaged migration discovery；G004 固定检查为 durable outbox/inbox、
bounded pagination/backpressure、idempotent replay、typed tombstone propagation 和 PostgreSQL sync regression。
verifier result 中的 `verifier_id` 与 `verifier_contract_sha256` 必须由当前脚本合同生成，不能由自由文本替代。

G005 已提供签名 PostgreSQL 逻辑备份、无覆盖恢复、PITR 前提检查和实测 RPO/RTO 演练工具；但
`backup_restore_dr` 默认仍为 `blocked`。只有同时配置一份通过 HMAC 验证的 PostgreSQL backup
artifact 和与其 backup id、payload digest、key id 匹配的签名演练报告时，该门禁才会变为
`pass`。缺失证据保持 `blocked`，配置了证据但签名、摘要、PITR 或阈值无效则为 `fail`；报告不回显
工件路径、密钥或解析异常。

G005 当前工程验收已经完成：本地组合 `672 passed, 1 warning`，真实 PostgreSQL 八文件门禁
`839 passed, 3 skipped`，包含 `100001` 条 outbox 的恢复与 identity sequence 保真，以及“payload
损坏但 manifest 重新签名”仍整事务拒绝恢复；wheel 隔离 discovery/CLI `6 passed`。两条 fresh
独立审查均为 `APPROVE / CLEAR`，未发现 P0/P1。这里的“工程通过”不等于任意部署已完成演练；
部署仍必须提交自身的签名 artifact 与签名 drill report 才能让动态门禁变绿。

G006-G009 的工程验收也已完成，但每个生产部署仍必须提交自身的当前签名证据。因此即使某个
部署提供了有效 G005 证据，也不得用单项通过替代其他门禁；只有全部门禁同时通过才可报告
`ready / industrial`。

## 生产探针、指标与 SLO 证据（G006）

G006 提供分离的匿名低信息探针：`/health/live` 只证明进程可响应，`/health/ready` 只在 runtime 与
storage 均可接收业务时返回 200。进入 shutdown 后 readiness 立即 503、新业务固定 503，已 admission
请求在有界 deadline 内完成，再 drain durable sync、worker 和 PostgreSQL pool。认证 `/health` 仅作兼容
诊断，不包含 storage path 或异常原文。

`/metrics` 使用固定 method/status-class 标签，不包含 tenant、subject、workspace、agent、memory id、
URL、path 或异常文本，也不会调用昂贵的全图/全表 health scan。发布包内置 Prometheus 告警规则；可用
以下 data-only 命令检查：

```bash
memplex --output json operations alerts-check
memplex --output json operations status
```

生产进程应配置上述四项统一部署绑定、canonical base64 32-byte HMAC key、非秘密 key id 与一个
安全的 evidence 输出文件：

```bash
export MEMPLEX_OPERATIONS_HMAC_KEY='<canonical-base64-32-byte-secret>'
export MEMPLEX_G006_REPORT_OUTPUT='/secure/evidence/g006-operations.json'
# YAML: operations.report_key_id: ops-2026-08
```

进程完成至少 5 分钟、1000 个请求且含至少 128 个 latency samples 的真实窗口并优雅退出后，
离线验证签名、当前部署绑定、key id、15 分钟 freshness、阈值、shutdown drain 和告警规则 digest：

```bash
memplex --output json operations verify-report /secure/evidence/g006-operations.json
python scripts/verify_g006_operations_slo.py --report /secure/evidence/g006-operations.json
export MEMPLEX_G006_OPERATIONS_REPORT='/secure/evidence/g006-operations.json'
memplex --output json readiness --strict
```

没有报告时 `operations_slo=blocked`；报告存在但签名、freshness、样本、部署绑定、key id、阈值、
drain 或 alert digest 无效时为 `fail`。
详细响应步骤见 `docs/runbooks/production-operations.md`。G006 工程通过不替代 G007-G009 的部署证据。

## 可复现发布供应链证据（G007）

G007 的构建脚本会在固定 `SOURCE_DATE_EPOCH`、不同 umask 下重复构建 wheel、sdist 与 npm
tgz，并生成 exact manifest、SHA-256 checksums 与 CycloneDX SBOM。发布工作流只消费一次构建的
release bundle；PyPI 发布成功后才允许 npm 发布。registry 中同版本不存在才发布，摘要完全一致时
幂等结束，摘要冲突或 registry/auth/network 异常一律 fail closed。发布使用 GitHub OIDC trusted
publishing，不配置长期 PyPI/npm 写 token。

生产 readiness 不信任 tag、CI 绿灯、unsigned 本地报告或文件路径本身。部署必须提供当前版本的
immutable bundle、签名 evidence 和 32-byte HMAC key（64 位十六进制）：

```bash
export MEMPLEX_G007_RELEASE_BUNDLE='/secure/release/memplex-3.3.0'
export MEMPLEX_G007_RELEASE_EVIDENCE='/secure/evidence/g007-release.json'
export MEMPLEX_RELEASE_EVIDENCE_KEY='<64-hex-secret>'
memplex --output json readiness --strict
```

验证器固定目录成员、内外层 archive 成员、时间戳、uid/gid/mode、大小上限、摘要、SBOM、版本和
必需插件资产；symlink、特殊文件、私有路径、credential-bearing DSN、private key 或重签后的恶意
额外成员仍会被拒绝。没有证据时 `release_supply_chain=blocked`；配置不完整或验证失败为 `fail`；
只有签名且与当前安装版本完全绑定的 bundle 才为 `pass`。详细发布与事故恢复流程见
`docs/release-automation.md` 和 `docs/runbooks/release-rollback.md`。

G007 工程验收包含 Python 3.11/3.12/3.13 的 wheel fresh install/reinstall/uninstall、sdist 安装、
Node 22 npm tgz install/reinstall/failure rollback/uninstall，以及完整仓库回归。它只关闭发布供应链
工程门；部署仍须提供 G008 四宿主生命周期和 G009 容量/chaos/soak 等全部当前证据。

## 四宿主真实生命周期证据（G008）

G008 验证器必须在隔离 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`、OpenClaw profile 与
`HERMES_HOME` 中运行真实宿主 CLI。Hermes CLI 必须来自固定官方 revision
`3c27eb6234bf91b8ceee9e9071591b31e9b148cb`，并核对官方
`agent/memory_provider.py` SHA-256。验证成功后输出签名 evidence：

```bash
export MEMPLEX_HOST_LIFECYCLE_HMAC_KEY='<64-hex-characters>'
export MEMPLEX_HOST_LIFECYCLE_KEY_ID='g008-prod-2026-08'
python scripts/verify_g008_host_lifecycle.py \
  --codex-cli /path/to/codex \
  --claude-cli /path/to/claude \
  --openclaw-cli /path/to/openclaw \
  --hermes-cli /isolated/hermes-venv/bin/hermes \
  --hermes-source-root /isolated/hermes-source \
  --deployment-id "$MEMPLEX_DEPLOYMENT_ID" \
  --source-sha256 "$MEMPLEX_SOURCE_SHA256" \
  --artifact-sha256 "$MEMPLEX_ARTIFACT_SHA256" \
  --target-identity-sha256 "$MEMPLEX_TARGET_IDENTITY_SHA256" \
  --key-id "$MEMPLEX_HOST_LIFECYCLE_KEY_ID" \
  --evidence-output /secure/evidence/g008-hosts.json

export MEMPLEX_G008_HOST_LIFECYCLE_REPORT=/secure/evidence/g008-hosts.json
```

`--evidence-output` 的父目录必须预先建立，且从文件系统根到父目录的整条路径不得包含
符号链接；验证器用 pinned directory fd 和 `O_NOFOLLOW` 原子写入，拒绝路径重定向。
evidence 自生成起仅有效 24 小时，并拒绝超过 5 分钟的未来时间；宿主升级、重启、配置
变更或有效期届满后必须重新运行验证器。

verifier 通过 JUnit 精确核对每个必需 node；任一 node skipped、xfailed、失败或未运行都拒绝签发。
签发前 verifier 与 evidence 创建器会在执行该验证的宿主上重新读取每个实际 CLI，确认它是常规可执行
文件且 SHA-256 与 proof 一致。每宿主 evidence 记录当时的 CLI 路径/二进制摘要/版本、隔离根摘要、
required-node manifest 与 JUnit 摘要。部署侧 readiness 会重新验证签名、预期 key id、统一部署绑定、
当前 Memplex 版本、四个宿主集合、proof schema、生成时间、当前打包 integration contract 摘要和固定
Hermes 来源；它不会重新读取签发宿主的 `cli_path`，因此验证产物可以被安全地交付给另一台部署机器。
缺文件、弱 schema、篡改、旧版本、
旧插件摘要或缺少任一宿主都会使 `four_host_e2e` 保持 `blocked/fail`，且报告不会输出
CLI 路径、临时 profile 或 HMAC key。G008 通过不能替代 G009；两者及其他门禁必须同时有效。

## 生产规模容量与 Chaos 证据（G009）

G009 是工业成熟度的最后强制门。验证器会在独立 PostgreSQL schema 中实际建立至少
100,000 个 Function 与 1,000,000 条 Edge，运行不少于 60 秒的并发 read/write/sync
workload，记录吞吐、p50/p95/p99、错误率、RSS、queue depth 与 outbox age，并执行
database disconnect、network fault、disk write failure、TERM、KILL 与 duplicate-delivery
故障矩阵。故障前后 authoritative Function/Edge 摘要必须一致，RPO 必须为 0，RTO 不得
超过 30 秒。

当前生产拓扑不使用 Redis，因此 evidence 只接受绑定
`redis_not_in_supported_topology` 的 `not_applicable`，不得伪称执行不存在的 Redis 数据路径。

```bash
export MEMPLEX_CAPACITY_CHAOS_HMAC_KEY='<64-hex-secret>'
mkdir -p /secure/g009-work /secure/evidence
python scripts/verify_g009_capacity_chaos.py \
  --dsn "$MEMPLEX_STORAGE_MIGRATION_DSN" \
  --workdir /secure/g009-work \
  --evidence-output /secure/evidence/g009-capacity-chaos.json

python scripts/verify_g009_capacity_chaos_evidence.py \
  --report /secure/evidence/g009-capacity-chaos.json
export MEMPLEX_G009_CAPACITY_CHAOS_REPORT=/secure/evidence/g009-capacity-chaos.json
```

readiness 重新核验 exact schema、HMAC、当前 Memplex 版本、冻结合同摘要、24 小时 freshness、
规模、所有 workload/SLO、全故障矩阵、RPO/RTO 与最终数据摘要。报告路径、DSN、密钥和原始
异常不会进入公开输出。缩小规模 smoke、unsigned JSON、测试数量或手工日志都不能关闭
`capacity_chaos`。

G009 已完成真实工业参数运行、全仓回归、wheel 隔离验证、anti-slop cleanup，以及独立
code/security 与 architecture/reliability 双审。工程基准在 10 核 arm64、16GiB、Python
3.11.15、PostgreSQL 16.2 上建立 100,000 Function / 1,000,000 Edge，持续 60.0026 秒完成
2,399,619 次操作且错误率为 0；read/write/sync p99 分别为 0.523/0.716/0.688ms，RPO=0，
RTO=0.0021 秒。该结果证明实现满足冻结合同，不替代目标部署重新生成的签名 evidence。

## PostgreSQL 备份、恢复与灾备演练（G005）

备份签名密钥必须是 canonical base64 编码的 32-byte secret，只能通过环境注入；配置与报告只记录
非秘密 `key_id`：

```bash
export MEMPLEX_BACKUP_HMAC_KEY='<canonical-base64-32-byte-secret>'

# 创建和离线验证签名工件。输出不会包含 DSN、密码或工件路径。
memplex --output json storage backup create --destination /secure/backup-root
memplex --output json storage backup verify /secure/backup-root/<backup-id>

# 恢复只允许同名且当前不存在的 schema；不会自动 drop、rename 或覆盖。
memplex --output json storage backup restore /secure/backup-root/<backup-id> \
  --target-schema <memplex-schema>

# 只读检查 WAL/archive 前提，并执行一次真实恢复演练。
memplex --output json storage backup pitr-status
memplex --output json storage backup drill \
  --artifact /secure/backup-root/<backup-id> --target-schema <memplex-schema> \
  > /secure/evidence/g005-drill.json
```

PostgreSQL 备份固定使用 schema-scoped custom-format `pg_dump`；client/server major 必须一致。
发布采用 payload/manifest 文件 fsync、临时目录 fsync、kernel no-replace rename、父目录 fsync，并把
源文件、目标 root、验证与恢复对象固定到 fd/inode，防止 symlink、目录替换和 verify 后路径重绑。
恢复固定使用 `pg_restore --single-transaction --exit-on-error`，目标 schema 已存在时在调用
`pg_restore` 前拒绝；成功后重新检查 migration ledger、catalogue、application/ingress ACL 与 target。

Lite snapshot/restore 只允许 `deployment.profile=development`，不能作为生产 G005 证据。生产部署在
完成真实演练后，将签名工件与签名报告路径注入 readiness；路径本身不是证据，内容必须重新验签：

```bash
export MEMPLEX_G005_BACKUP_ARTIFACT='/secure/backup-root/<backup-id>'
export MEMPLEX_G005_DRILL_REPORT='/secure/evidence/g005-drill.json'
memplex --output json readiness --strict

# 可在独立验收节点执行同一固定验证边界。
python scripts/verify_g005_backup_restore.py \
  --artifact "$MEMPLEX_G005_BACKUP_ARTIFACT" \
  --drill-report "$MEMPLEX_G005_DRILL_REPORT"
```

启用生产同步时，一个 Memplex 服务进程只服务一个 tenant：principal registry 可以包含该 tenant 的
多个 subject/workspace/remote agent，但只要出现第二个 tenant，dispatcher 会在注册 target 或启动网络
任务前 fail-closed。多租户部署必须按 tenant 拆分服务进程；当前版本不提供一个高权限进程跨 tenant
扫描 outbox 的 trusted executor，也不得给普通 application principal 扩权来模拟该能力。

## PostgreSQL 迁移维护窗口（G003 诊断面）

迁移命令只适用于已经配置了独立 application DSN 与 migration DSN 的 PostgreSQL 部署；它们
不会构造 `MemplexService`，因此不会因服务启动路径触发额外的存储初始化。请在维护窗口先做
只读检查，再决定是否执行写入：

```bash
# 只读：检查已解析的 application/migration 目标、应用身份 ACL 和 ledger/catalog 状态。
memplex --output json storage migration status
memplex --output json storage migration plan
memplex --output json storage migration apply --dry-run

# 唯一会写入的命令：先执行只读 preflight，完成受控 mutation 后使用新只读连接 readback。
memplex --output json storage migration apply
```

`status`、`plan` 和 `apply --dry-run` 不会创建或写入 migration ledger，也不会调用同步
push/pull，并且它们以 strict application ACL 检查报告 readiness。`apply` 不会在 DDL 之前运行
该 strict ACL 检查：首次建 schema/ledger 时 application role 尚未获得 grant 是预期状态。它直接
进入 runner 的 advisory-lock 受控结构 preflight/mutation；提交后重新检查 application target 与
direct-login principal 是否仍与初始绑定一致，再用新的严格只读 `status`/ACL readback 决定是否成功。
若已提交 DDL/ledger 但 ACL/readback 仍未就绪，命令以非零和固定
`migration_committed_acl_remediation_required` 结束，operator 必须人工配置已批准的最小权限后执行
`status`；命令绝不自动 `GRANT`。命令输出仅包含状态、版本和 migration 名称；不会输出 DSN、token、
SQL、绑定参数、业务 payload 或数据库驱动原始异常。

runner 在提交前必须先完成 cursor cleanup；其失败会 rollback，不能留下 ledger。commit 一旦成功，
connection close 只是 best-effort，不能把已提交的 migration result 改写为失败。即使 `apply` 因为
commit/connection I/O 异常而不能直接确认结果，CLI 仍会重新绑定 application target/principal 并执行
独立 strict readback：readback 为 `ready` 即按已确认结果成功；明确仍为 `upgrade_required` 时返回
`migration_failed`；其余无法确认的状态只返回
`migration_outcome_requires_readback`，要求先执行 `status`，不得重复写入。

为避免两个 Unix-socket cluster 在 database/schema 相同的情况下被误判为同一目标，maintenance
command 只接受 exact `PostgresTargetIdentity` 的非空已解析 TCP server、port、database、schema；
缺 server/port、partial 值、lookalike 或 subclass 一律拒绝。application 身份也必须是 exact
`PostgresApplicationPrincipal`，且 `role` 与 `session_role` 都是相同的非空字符串；高权连接通过
`SET ROLE` 冒充 application role 会被拒绝。该检查在 production 与 development 均适用，错误只给出
固定 code 和 remediation。

`migration_verification_report(store)` 是用于本机排障的机器可读诊断，它明确标记
`signed: false` 与 `local_diagnostic_only: true`。它不构成签名证明，不能关闭
`schema_migrations_atomicity` 或任何工业级门禁。

### G003 当前本地集成证据（非工业 attestation）

2026-08-11 的候选验收在每个真实 PostgreSQL 用例中创建独立 schema，并在 cleanup 时执行
`DROP SCHEMA ... CASCADE`；测试确认 migration 前 schema 为空、migration ledger 连续、
`public` 不承载 `memplex_%` 受管表。真实 PostgreSQL 全套为 `648 passed, 2 skipped`，
Lite/storage/CLI/readiness/sync 全套为 `368 passed`；运行时为 Python 3.11.15、psycopg2
2.9.12、PostgreSQL 16.2。另有 16-worker 多租户 RLS/pool 检查，验证跨 tenant 不可读且
business lease high-watermark 不超过配置上限。

pool 的 manager-authoritative token state-machine 已完成 fresh reviewer 审查；两个 PostgreSQL
skip 是 pgvector extension 不可用的环境分支。这些是工程归档与待签发 verifier result，不会单独使
任一当前部署的 `schema_migrations_atomicity` 变为 `pass`；该门仍要求前述 15 分钟内、绑定当前
source/artifact/deployment/target/key id 的签名 evidence。

Lite 仍是单进程本地开发/测试存储，不是生产恢复方案；不得使用 Lite 文件、Lite journal 或
Lite 测试结果替代 PostgreSQL 迁移、备份恢复和灾备演练证据。

不得将 Lite 单机测试、mock PostgreSQL、配置文件生成或宿主 fixture 视为这些门禁的
替代证据。每个门禁必须记录可重复执行的命令、真实依赖版本、数据规模和结果工件。
