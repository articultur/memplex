# Known issues & retained low-severity findings（2026-10-24 建档）

本文件是仓库级的已知问题清单：评审/修复中保留的低危项与尚未开工的大块遗留。
低危保留项的原文未随工作流报告完整保存，此处收录的是**可考证的实质内容**
（来源：修复员自报风险、终审披露、能力合同表），不是逐字评审记录。

## 低危保留项（已确认存在、暂不处理）

1. **热层 user 可见性条目被 workspace 预检分区**：working-memory 层内 user 条目
   先按 workspace 分区再做 owner 检查，比持久层的 user 分支（租户内全可见，
   仅 owner 限定）更严。属 fail-closed 收紧而非放宽；持久层语义未动。
2. **直调 `MemplexService.write` 传未知 visibility 字符串会整体抛 ValueError**
   （fail-closed）而非旧静默改 tenant：仓库内所有调用方经 runtime 层白名单
   校验不受影响，直调方（如有）会遇到新异常——是有意的行为变更。
3. **本地单机兼容分支**：service 默认写入（local-development）与 local-process
   runtime 共享 `tenant:local` 热层分区，该分支仅在 local scope 生效并有测试
   钉死（`test_real_tenant_runtime_never_takes_compatibility_branch`）。
4. **retrieval 判定仍是 top-24 gold-session 覆盖代理指标**：gap_forensics 的
   full-coverage 是口径修正，不是新增真值来源；counting_intent 为关键词启发式
   （99/133 判计数），无人工抽检。
5. **基线矩阵单次运行、无方差口径**；orchestrated 组合臂在本地无增强 LLM 时
   退化（default≡orchestrated 的零差只反映退化生效，不可外推）。
6. **Mimosa normal 深度静态扫描 43 findings 未逐条复核**
   （scanId `scan-2026-10-24T17-56-02`，seal `sha256:3e220cf…`，
   static-only 边界；历次深扫画像为已知误报家族 + 占位符，但本次未复核）。
7. **`scripts/` 目录 23 处 ruff 欠账**：不在 CI lint 门禁面（门禁只覆盖
   `memplex tests`），属既有脚本的历史欠账，与近期提交无关。

## 大块遗留（需独立决策或战役，未开工）

- **发版**：141+ 提交未发布，两个安全修复（热记忆词表、段落 ACL 身份）不在
  任何 release。版本九处声明 bump + tag + push 需用户明确授权。
- **ADR-012 Phase B/C 验收缺口**：crash 测试为 monkeypatch 替换（非真实
  kill -9/事务中/WAL checkpoint 矩阵）、30 分钟死锁 soak 未跑、全量 rw 语义
  回归不全；"单一大库 5908 会话吞吐塌缩 ≥7 倍"的根治在此（短期以每题新库
  runner 模式绕开）。
- **PG parity 关闭**：段落不进检索（Phase B 窗口）、无 trust 惩罚/配额、
  `vector_search` 只查 functions——红队结论不可跨后端迁移
  （见 `docs/capability-parity-2026-10.md` 表 B）。
- **打分 harness（P3 协议）**：检索级基线无答案正确率维度；需 answerer/judge
  API 预算。
- **四宿主真实安装矩阵**：受网络窗口限制未跑；MCP 2026-07-28 双时代兼容未做。
- **legacy LWW 与 durable sync 双轨语义未收敛**（P2）。
- **PG 段落本地同步发射**：入站 apply 已支持（ADR-013 Stage 3），本地捕获/
  快照发射等 Phase-B 触发器迁移（快照读依赖触发器写入的 entity versions）。
- **ADR-012 死锁未根除（P0，2026-10-24 soak 实锤）**：30 分钟 soak
  5 进程中 worker 0 于 ~42 分钟工作中途挂死（纯 CPU、非 MPS；
  详见 `docs/evidence/adr012-fault-matrix/manifest.md` §2）。B4 单写者
  队列的"构造性消灭"声明在长尺度不成立；根因未定位（无栈样本，
  watchdog 先杀了进程）。真实 kill -9 崩溃矩阵与 rw 语义回归已达标
  （§1/§3）；Phase C 不应开启直至死锁归零。
- **rw 全量套件的 170 个 JSON 前提失败**：`test_lite_durability.py`
  (160) / `test_lite_sync_repository.py` (8) / `test_backup.py` (1) /
  `test_sync.py` e2e (2，含 shadow 模式 2) 在全局 rw 下失败——
  测试手工构造 memory.json/journal 测 JSON 持久化协议，rw 下该协议
  被 SQLite 权威取代（B2 既定语义，非回归）。rw 正确性由专项测试
  背书（见 manifest §3）；若要 Phase C，这批测试需参数化双模式。

## 维护规则

- 每项落地后从本文件移除并在对应提交里注明；新保留项入档时注明来源与日期。
