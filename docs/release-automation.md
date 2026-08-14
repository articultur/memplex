# 发布自动化

Memplex 的 Python wheel/sdist 与 npm 包由 [`.github/workflows/release.yml`](../.github/workflows/release.yml) 在同一次发布运行中构建、校验、验签并发布。发布流程不接受长期 PyPI/npm 写入 token。

## 发布前平台配置

仓库管理员必须在外部平台完成以下一次性配置：

1. 在 PyPI 为 `memplex` 配置 GitHub Actions Trusted Publisher，绑定本仓库、`release.yml` 和 `pypi` environment。
2. 在 npm 为 `memplex` 配置 Trusted Publisher，绑定本仓库、`release.yml` 和 `npm` environment。
3. 在 GitHub 创建受保护的 `pypi`、`npm` 与 `g008-real-host` environment；按组织策略配置审批人和 tag 保护。
4. 为 `g008-real-host` 配置仅该 environment 可见的 `MEMPLEX_HOST_LIFECYCLE_HMAC_KEY`、
   `MEMPLEX_G008_HERMES_SOURCE_ROOT`、`MEMPLEX_G008_DEPLOYMENT_ID`、
   `MEMPLEX_G008_SOURCE_SHA256` 与 `MEMPLEX_G008_TARGET_IDENTITY_SHA256`。两个摘要必须是
   当前实际部署的 64 位小写 SHA-256；不可用普通 CI 变量或测试报告替代。artifact 摘要不得配置为
   secret：它必须直接来自本次 `build` job 上传 `memplex-release-bundle` 时产生的
   `artifact-digest` output。
5. 配置一台受控的 macOS self-hosted runner，并给它**唯一**标签
   `memplex-g008-real-host`。该机须预装真实的 `codex`、`claude`、`openclaw`、`hermes`
   CLI，以及固定 Hermes revision `3c27eb6234bf91b8ceee9e9071591b31e9b148cb` 的源码根。
   不要把 GitHub-hosted macOS、普通 CI runner 或模拟 CLI 加入该标签。
6. 不要配置 `NPM_TOKEN`、`PYPI_TOKEN` 或 `NODE_AUTH_TOKEN`。工作流只使用短期 OIDC 身份。

## 发布合同

正常发布只能从与所有版本元数据完全一致的不可变 tag 启动：

```bash
git tag v3.3.0
git push origin v3.3.0
```

工作流将：

1. 使用带 SHA-256 的固定 build backend wheel，断网解析并在不同 umask 下构建两次。
2. 对两次 wheel、sdist、npm tgz、SBOM、checksums 与 manifest 做逐字节比较。
3. 只上传一次 release bundle；后续 provenance、SBOM attestation、G008、PyPI 和 npm 发布都消费
   同一组文件。G008 下载该上游 artifact，逐项验证 manifest 记录的大小和 SHA-256，并要求恰好一个
   wheel 与一个 npm tgz。
4. 通过 GitHub OIDC 生成 artifact provenance 与 SBOM attestation。
5. 在 PyPI 前调用 `G008 Real Host Lifecycle` 的受保护 self-hosted macOS job。它用真实四宿主
   CLI、固定 Hermes 官方源码、真实 CLI JUnit required nodes 和当前部署绑定生成签名 evidence。
   它先在 fresh venv 中从已校验 wheel 安装 Memplex，所有安装、状态查询、宿主 launcher 与 verifier
   都使用该 artifact 环境；在 verifier
   前后会对四个宿主分别执行带显式 `--target-dir` 的 `memplex --output json agent status`，每个
   `runtime_status.state` 都必须为 `healthy`。发布 job 只能在该 job 成功后运行。
6. 发布前读取 registry 中的同版本摘要：不存在才发布；完全一致则幂等结束；不同摘要固定报 `digest conflict` 并停止。
7. npm registry 查询仅在明确的 `E404` 时视为未发布。网络、认证或 registry 异常一律 fail closed。

## G008 受保护检查

将 `G008 Real Host Lifecycle / g008-real-host-lifecycle` 设为 release/tag 规则所需检查。该检查
没有条件跳过路径：缺少带 `memplex-g008-real-host` 标签的 runner 时工作流会保持 pending；runner
上线后若任一 CLI、固定 Hermes 源码、required node、部署绑定值或 HMAC secret 缺失/无效，则 job
以失败结束。普通 CI 的 macOS matrix、跳过的 pytest、手工日志与未签名 JSON 都不能代替此检查。

签名 evidence 只保留在受控 runner 的临时工作区供该 job 校验，**不得**上传为普通 Actions artifact，
也不得用未签名 JSON 代替部署证据。任一四宿主 runtime sidecar 报告 `degraded`（包括
`state_unreadable`）或缺失状态字段，前/后检查都会 fail closed；这项运行态观察是对真实 host proof
的补充，不能替代固定源码、required nodes 或部署绑定验证。目标部署仍须按
[`生产 readiness 合同`](production-readiness.md) 重新验证，不能把 release job 成功等同于目标部署已就绪。

`tests/test_agent_host_matrix.py` 只提供确定性、身份与工作区隔离的 **unit-only** 合同覆盖，不能作为真实宿主 proof，
也不会进入签名 evidence 的 required-node manifest 或 real-host selected suite。
G008 真实宿主证据必须在同一个新建临时根下分别安装四个宿主，通过实际 CLI 启动路径，并在 verifier
前后对每个宿主使用显式 `--target-dir` 查询该临时根；任何默认用户根上的既有安装都不计入证据。

## 本地验证

本地只构建和验证，不发布：

```bash
epoch="$(git show -s --format=%ct HEAD)"
python scripts/build_release_artifacts.py \
  --source . --output /tmp/memplex-release \
  --tag v3.3.0 --source-date-epoch "$epoch" --allow-dirty

python scripts/verify_g007_supply_chain.py \
  --bundle /tmp/memplex-release \
  --evidence /path/to/signed-release-evidence.json
```

ZIP/wheel 时间戳不能早于 1980 年；因此本地命令与发布 workflow 都使用当前提交时间，不能把
Unix epoch `0` 当作可重现构建时间。

生产 readiness 只接受与当前 bundle 摘要绑定、签名有效的 release evidence。tag、CI 绿灯、测试数量或 unsigned 本地报告都不能关闭该门禁。

## 禁止事项

- 不得覆盖、重打或删除已经发布的版本来模拟回滚。
- 不得从 `main` 下载脚本后直接交给 shell 执行。
- 不得在工作流、仓库变量、文档或日志中保存 registry 写 token。
- 不得在 publish job 中重新构建 artifacts。
- 不得在 registry 查询失败时假设版本不存在。

发生发布事故时执行 [发布回滚 runbook](runbooks/release-rollback.md)。
