# ADR-012 Phase B fault-matrix acceptance (2026-10-24)

本 manifest 记录清单第 10 项（真实故障矩阵）三项验收的执行证据。

## 1. 真实 kill -9 崩溃矩阵（replaces monkeypatched crash test）

- **交付**：`tests/test_rw_crash_matrix.py`（4 测试）+ `scripts/rw_crash_child.py`
  （spawn-context 子进程 runner；提交 `e123c0b`）。
- **方法**：multiprocessing spawn 子进程在 rw 权威下运行真实写循环，
  SIGKILL 于三个崩溃点：
  - **mid-transaction**（burst 模式：写入在途，单写者队列排入
    BEGIN IMMEDIATE 事务）；
  - **mid-WAL-checkpoint**（checkpoint 模式：持续写压使默认
    autocheckpoint 反复触发 WAL 检查点；写上限 10_000 保证 kill 必然
    落在循环中途而非跑完后）；
  - **mid-write-queue**（queued 模式：200 写 deferred flood 批次在
    单写者队列排空中被杀；两个测试覆盖批内不同时点）。
- **断言**：FRESH 进程（read 权威）重开——不抛
  LiteStorageIntegrityError；已提交记录存活（WAL 回放）；段落行
  whole-or-absent（torn 计数为 0）；rows > 0（kill 前确有提交，
  排除空跑假阳）。
- **前身测试的定位**：`test_rw_authority_crash_rolls_back` 的
  monkeypatch 替换 `AuthoritativeWriter.commit` 只覆盖 Python 层回滚
  契约（仍在跑）；真实矩阵补上进程级/SQLite 崩溃语义。
- **诚实边界**：kill 时机为定时（2.5–7.5 秒），不保证精确命中
  "checkpoint 指令执行中的微秒窗口"——覆盖的是"检查点邻域的高压
  崩溃"，这是实际部署会遇到的形态。

## 2. 30 分钟死锁 soak

- **工具**：`scripts/deadlock_stress.py`（5 独立进程、CPU bge-m3、
  orchestrated 查询、watchdog 1900s、挂死=死锁签名）。
- **首跑校准**（90 轮）：verdict **pass**，5/5 exit 0、0 挂死、
  406 秒——负载不足 30 分钟。
- **正式跑**（430 轮 × 5 进程 ≈30 分钟负载，watchdog 1900s）：
  **verdict FAIL — 真实死锁复现**。Worker 0 挂死
  （`hung=true`，被 watchdog 终止）；worker 1-4 全部干净跑完
  （各 3440 写 / 2150 查询 / exit 0）。
- **挂死定性（排除假阳）**：worker 0 的临时库
  `stress-0-jvwu9r13/s.sqlite3/` 代际 **7244**、最后写入
  **2026-10-24T20:12:07Z**（挂死判定前 ~4 分钟）——它工作了
  ~42 分钟后**中途挂死**，不是启动期卡死（known 形态之一）也
  不是残留结果文件（`worker-0.json` 的 written=720 是首轮校准
  跑的残留，本次跑 worker 0 的真实进度在库里而非结果文件——
  结果文件只在子进程正常退出时写）。
- **结论**：B4 单写者队列的"构造性消灭 worker↔主线程死锁"
  声明在 35 分钟尺度 **未成立**：1/5 进程复发。ADR-012 验收门槛
  "超时阻塞 = 0" **未达标**；Phase C（默认 SQLite 权威）不应
  开启。栈采样不可得（watchdog 已终止进程；后续复现应先
  `sample <pid> 5` 再杀）。
- **取证复跑 #1**（430 轮 × 5，2026-10-24 取证协议版，24 分钟）：
  verdict **pass**，5/5 干净退出（各 3440 写 / 2150 查询）——
  **未复现**。与"概率性复发"一致（上次 1/5 进程于 ~42 分钟）；
  一次未复现不是修复证明，延长窗口复跑进行中。
- **取证协议已入库**（ff13bdd）：子进程 faulthandler.enable() +
  挂死时父进程先 SIGABRT（全线程 Python 栈落 stderr 日志）再 kill。
- **已知背景**：v13.1（2026-09-22）三次复现的根因当时定位为
  MPS 嵌入并发；首跑（2026-10-24 §2）是纯 CPU——**CPU 下也存在**
  该死锁类，根因需重新定位（候选：worker 线程与服务线程的锁序、
  EmbeddingService RLock 与单写者队列的交互）。

## 3. rw 语义全量回归

- **方法**：`MEMPLEX_LITE_SQLITE_AUTHORITY=rw` 全局环境下分片跑
  全量 lite 套件（排除两个自声明模式的权威专项文件）。
- **结果**：**3217 通过 / 170 失败**；失败按文件聚合：
  `test_lite_durability.py` 160 + `test_lite_sync_repository.py` 8 +
  `test_backup.py` 1 + `test_sync.py` 2（含 shadow/authority 自身
  模式下的 2 个）。
- **失败定性（非回归）**：全部为 **JSON pair 前提类**——这些测试
  手工构造 memory.json/journal 文件并断言 JSON 持久化协议行为；
  rw 模式下该协议被 SQLite 权威取代（JSON 退为每 8 代快照，B2
  既定设计）。抽检确认：`test_journal_transition_rejects_reused_
  transaction_id` 失败于 "DID NOT RAISE"（rw 下根本不走 journal
  路径）；`test_lite_restore_rejects_tampered_artifact` 失败于
  memory.json 不存在（首代未到快照点）。
- **与 B2 记录一致**：2026-09-26 d2fbcd1 已入档"全局 rw 跑全量套件
  会 170 失败（JSON 前提测试假设被旗标翻转）；rw 正确性由专项测试
  背书"——本次回归数字与之吻合，无新增失败类。
- **rw 正确性背书面**：shadow/safety 专项（18 过，自身模式）、
  真实崩溃矩阵（4 过）、rw 环境下的常规语义面（136 过：single
  writer/段落生命周期/段落身份/原文层/service/working memory/
  derivation lineage 全部）。

## Evidence level / runner / date

- **Evidence level**: E1（本地确定性执行，逐项命令与日志在会话记录）。
- **Runner**: pytest + scripts/deadlock_stress.py + scripts/rw_crash_child.py。
- **Date**: 2026-10-24。
