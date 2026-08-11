# 发布回滚 Runbook

## 原则

PyPI 与 npm 版本是不可变制品。回滚不是覆盖旧版本，而是阻止问题版本继续被默认安装，并把用户恢复到最后一个已验证版本。

## 触发条件

- registry 中的摘要与 release manifest 不一致；
- artifact provenance、SBOM attestation 或签名 evidence 无法验证；
- 新版本安装、启动、卸载或四宿主生命周期验证失败；
- 发布后发现安全或数据完整性 P0/P1。

## 操作步骤

1. 停止后续 environment 审批，不重新运行 publish job。
2. 保存失败 tag、workflow run id、release manifest、checksums、SBOM 与 attestation 验证结果；不得记录 token 或 DSN。
3. 在 PyPI yank 问题版本；在 npm deprecate 问题版本，并写明最后一个已验证版本。不得删除并重发同一版本。
4. 对最后一个已验证版本重新执行 provenance、SBOM、manifest 与 registry 摘要核对。
5. 在隔离 HOME/venv/npm prefix 中安装最后一个已验证版本，验证 CLI、插件资产、卸载和重新安装。
6. 发布一个递增的新修复版本；从新的受保护 tag 运行完整 `release.yml`。
7. 修复版本通过后更新事件记录、根因、影响范围和防回归测试。

## 用户恢复命令

Python：

```bash
uv tool install --force memplex==<last-known-good>
```

npm：

```bash
npx memplex@<last-known-good> setup --agent <host> --project-path "$PWD"
```

## 验收

- registry 旧版本摘要未变化；
- 问题版本已 yank/deprecate，未被覆盖；
- last-known-good 与修复版本的 provenance、SBOM、manifest、checksums 全部可验证；
- 隔离安装、卸载、重新安装和 readiness evidence 均通过；
- 事件记录中没有 secret、DSN、本机路径或业务 payload。
