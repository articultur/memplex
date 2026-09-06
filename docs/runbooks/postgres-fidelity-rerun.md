# PostgreSQL CI-fidelity 第三方复跑 Runbook（多租户/安全维度）

目的：让任何第三方在干净机器上，从零复现 Memplex 真实 PostgreSQL 后端
（含 pgvector、RLS 行级安全、真实 catalogue/ACL 探测、备份/恢复环回）
的全部验收测试，不需要访问本仓库维护者的任何私有环境。

两条等价路径：**A. pgserver 自包含**（零外部服务，推荐首次复跑）与
**B. 外部 PostgreSQL**（与 GitHub Actions `test-postgres` job 完全同构）。

## 前置

- 仓库 checkout（任一 commit；建议与被评审 PR 同 SHA）。
- Python 3.11+ 与 `uv`；`uv sync --locked --extra pgtest`（或等价
  的依赖安装——`pyproject.toml` 的 `pgtest` extra 已含 pgserver 与
  psycopg2 二进制轮子集合）。
- 路径 B 另需：PostgreSQL 16 服务端（带 `vector` 扩展可用）、
  **与服务器主版本一致的 `pg_dump`/`pg_restore` 客户端工具**
  （G004 备份套件在客户端/服务器主版本偏斜时会失败——这是探测到的
  真实约束，不是 flake；CI 通过 pin `postgresql-client-16` 解决）。

## 路径 A：pgserver 自包含复跑（约 95 秒）

`tests/conftest.py` 的 session fixture 在**未设置** `MEMPLEX_TEST_POSTGRES_DSN`
时会用 [pgserver](https://pypi.org/project/pgserver/) 引导一个私有真实
PostgreSQL（真实进程、真实文件系统，非 mock），每个测试拿到独立 schema。
因此复跑只需：

```bash
# 一次性环境（pgserver 需要 Python 3.11 运行时）
uv venv .venv-pgcheck --python 3.11
uv pip install --python .venv-pgcheck/bin/python -e ".[pgtest]" pytest

.venv-pgcheck/bin/python -m pytest \
  tests/test_postgres_integration.py \
  tests/test_postgres_backup_integration.py \
  tests/test_sync_postgres_integration.py \
  tests/test_sync_repository_contract.py \
  tests/test_g014_postgres_task_repository.py \
  tests/test_ci_postgres_contract.py \
  -q
```

预期（2026-09-06 干净环境全量重建实测，Apple Silicon / Python 3.11.16）：
**412 passed, 3 skipped, ~114s**。skip 是显式环境探测跳过（缺外部条件
时 fail-closed 标注，不会静默变绿）。

### 六文件 vs CI 十文件的差异（如实口径）

CI 的 `test-postgres` job 额外运行 `tests/test_postgres_store.py`、
`tests/test_g004_postgres_backup_real_value.py`、
`tests/test_g004_postgres_probe_isolation.py`。后两个要求外部服务容器
语义（pg_dump/pg_restore 与服务器主版本对齐），在 pgserver 引导路径下
按环境探测跳过；如需全量对齐请走路径 B。`test_postgres_store.py` 可在
路径 A 直接追加（2026-09-06 实测 317 passed / 9s）。

## 路径 B：外部 PostgreSQL（与 CI 完全同构）

CI 的服务容器是 `pgvector/pgvector:0.8.6-pg16`（digest-pinned）。复跑
等价配置：

```bash
docker run -d --name memplex-pg \
  -e POSTGRES_DB=memplex -e POSTGRES_USER=postgres \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 5432:5432 pgvector/pgvector:0.8.6-pg16

export MEMPLEX_TEST_POSTGRES_DSN='postgresql://postgres:postgres@localhost:5432/memplex'
export MEMPLEX_REQUIRE_PGVECTOR=1

uv run pytest \
  tests/test_ci_postgres_contract.py \
  tests/test_postgres_integration.py \
  tests/test_postgres_backup_integration.py \
  tests/test_sync_postgres_integration.py \
  tests/test_sync_repository_contract.py \
  tests/test_postgres_store.py \
  tests/test_g014_postgres_task_repository.py \
  tests/test_g004_postgres_backup_real_value.py \
  tests/test_g004_postgres_probe_isolation.py \
  -v --timeout=120
```

两个环境变量的语义：

- `MEMPLEX_TEST_POSTGRES_DSN`：非空时 conftest 直接使用该服务器并保留
  每测试独立 schema fixture（`public` 永远不是测试的草稿区）。
- `MEMPLEX_REQUIRE_PGVECTOR=1`：测试前置探针会执行
  `CREATE EXTENSION IF NOT EXISTS vector` 并跑一次 `<=>` 余弦距离
  断言（随后 rollback，不在外部库上留下任何安装痕迹）；失败即报
  "PostgreSQL prerequisite unavailable"，**fail-closed，不回退假绿**。

## 这套套件覆盖了什么（多租户/安全声明的证据范围）

- pgvector 语义检索的真实 SQL 与余弦排序；
- 多租户 RLS 隔离与 ACL 目录（真实 catalogue 探测，非模拟）；
- 17 方法同步仓库锁步契约（双后端同一抽象）；
- G014 PostgreSQL 任务仓库；
- 备份/恢复环回与 G004 真实备份值探测（路径 B）。

## 复跑失败时的第一排查清单

1. `pg_dump --version` / `pg_restore --version` 与服务器 major 是否一致
   （G004 备份套件的常见假失败源）；
2. `MEMPLEX_REQUIRE_PGVECTOR=1` 但目标库无 `vector` 扩展可用；
3. pgserver 路径误设了 `MEMPLEX_TEST_POSTGRES_DSN`（会切换到外部模式）；
4. Python 运行时不是 3.11（pgserver 兼容面）。
