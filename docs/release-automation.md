# 发布自动化

Memplex 的 Python wheel/sdist 与 npm 包由 [`.github/workflows/release.yml`](../.github/workflows/release.yml) 在同一次发布运行中构建、校验、验签并发布。发布流程不接受长期 PyPI/npm 写入 token。

## 发布前平台配置

仓库管理员必须在外部平台完成以下一次性配置：

1. 在 PyPI 为 `memplex` 配置 GitHub Actions Trusted Publisher，绑定本仓库、`release.yml` 和 `pypi` environment。
2. 在 npm 为 `memplex` 配置 Trusted Publisher，绑定本仓库、`release.yml` 和 `npm` environment。
3. 在 GitHub 创建受保护的 `pypi` 与 `npm` environment；按组织策略配置审批人和 tag 保护。
4. 不要配置 `NPM_TOKEN`、`PYPI_TOKEN` 或 `NODE_AUTH_TOKEN`。工作流只使用短期 OIDC 身份。

## 发布合同

正常发布只能从与所有版本元数据完全一致的不可变 tag 启动：

```bash
git tag v3.3.0
git push origin v3.3.0
```

工作流将：

1. 使用带 SHA-256 的固定 build backend wheel，断网解析并在不同 umask 下构建两次。
2. 对两次 wheel、sdist、npm tgz、SBOM、checksums 与 manifest 做逐字节比较。
3. 只上传一次 release bundle；后续 provenance、SBOM attestation、PyPI 和 npm 发布都消费同一组文件。
4. 通过 GitHub OIDC 生成 artifact provenance 与 SBOM attestation。
5. 发布前读取 registry 中的同版本摘要：不存在才发布；完全一致则幂等结束；不同摘要固定报 `digest conflict` 并停止。
6. npm registry 查询仅在明确的 `E404` 时视为未发布。网络、认证或 registry 异常一律 fail closed。

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
