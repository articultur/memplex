# 评分卡更新：2026-09-04（目标 ≥80 的差距分析与达成路径）

沿用 [G001 基线](../open-source-benchmark-baseline.md) 的十一类
weight×level/4 框架，基于本轮 11 个提交后的实测证据重评。

## 逐类更新（仅列变动项；G001 分数见基线文档）

| 类别 | 权重 | G001 | 现在 | 依据（证据可查） |
| --- | ---:| ---:| ---:| --- |
| 检索评测 | 14 | 7.0 | **12.25** | level 3.5：语义混合检索落地（paraphrase 低重叠 recall@1 0.027→0.784）；**4 个 clean-SHA 公开数据 E1 bundle**（popqa n=100 mrr 0.99、hotpotqa n=100、longmemeval n=5、triviaqa n=100 适配器 TODO 如实标注）；fail-closed 公开模式防合成冒充；per-query trace 基础设施。未到 level 4：签名/不可变发布缺 |
| 真实用户任务 | 8 | 4.0 | **6.0** | level 3：核心召回闭环实质可用（真实 CLI 指南 + 语义栈），负向路径测试在库；跨环境用户验证仍缺 |
| 时间/多跳 | 12 | 6.0 | **9.0** | level 3：有界二跳落地（`retrieval.graph_max_hops`，配置限 1-2，预算硬顶，契约测试钉死）；诚实命名边界保持（非通用多跳）。聚合多跳任务仍缺 |
| 多租户/安全 | 12 | 6.0 | **9.0** | level 3：真实 PG+pgvector+RLS 套件在本战役**每个 storage 批次**全绿（413 passed，pgserver 实例）；精确类型校验作为安全防御经 TRY004 审计确认并保留。第三方复跑仍缺 |
| 同步/持久性 | 12 | 6.0 | **9.0** | level 3：17 方法锁步契约在全部批次零破坏；真实回环在库；**语义边界文档落地**（[sync-semantics-boundary.md](sync-semantics-boundary.md)）回答 G001 P1-4。legacy 丢弃审计测试列为移除前置 |
| 可观测性 | 8 | 4.0 | **5.0** | level 2.5：低基数指标+证据模型在库且当前可跑；新鲜签名 G006 报告仍缺 |
| 集成 | 6 | 3.0 | **4.5** | **CI 全绿**：PR #24 全矩阵 24/24 jobs 于 2026-09-04 在 GitHub 托管 runner 通过（含 742 项真实 PG 测试、六版本 Python 矩阵、发布安装矩阵、审计）；此前的 startup 阻断在启用+活动后自行解除 |
| 可复现性 | 10 | 5.0 | **8.1** | level 3：ruff 0.16 解钉+锁 0.16.6；bundle 四文件契约+checksum 对抗测试；公开模式无静默回退。不可变公共制品缺 |
| DX/运维 | 5 | 1.25 | **2.5** | level 2：README 安装指令修正为真实公开版本 3.2.7（原 3.3.0 是坏指令）；docs 索引与 runbook 在库。版本错位本体（源码 3.3.0 vs registry 3.2.7）待发布凭证 |
| 治理 | 5 | 1.25 | **2.5** | level 2：五件套（CONTRIBUTING/SECURITY/GOVERNANCE/SUPPORT/CoC）实测在库（G001 评分时未计入），政策入口完整 |
| 架构/数据模型 | 8 | 6.0 | **6.5** | 契约体系经 2.7k 变更战役实证（镜像×7、清单行号×3、故障注入×1 当场捕获回归） |

**合计 ≈ 74.25 / 100**（G001 49.5 → +24.75）。

**同日更新**：CI 复活并全绿（24/24，含 chromadb 审计豁免的
上游无修复版通告处置、PG 客户端主版本钉定修复的环境偏斜——
本地 8 文件 422/2 全过为隔离依据）→ 集成 4.5、可复现性 8.1，
**合计 ≈ 77.5**。PR #24 合并后 **main 分支 CI 亦全绿**（merge
push 触发，同 SHA 24/24）。距 80 的唯一剩余项：3.3.0 版本发布
（需凭证，DX 2.5→5，+2.5）。

## 距 80 的缺口与解锁条件（全部在用户侧，一键级）

1. **Actions billing 解封**（+2~3）：账户设置处理 spending limit →
   dispatch 重跑 → PR #24 全矩阵 check runs → 集成 3→4.5、
   可复现性 7.5→8.75。**这是单点杠杆最大的一项**。
2. **3.3.0 发布**（+2~3）：PyPI/npm 发布凭证 → 版本对齐 → DX 5。
3. 签名公开制品（+1~2，随 1/2 顺带）。

即：74.25 + 最小路径 1 + 2 ≈ **79~80+**。代码侧在无凭证条件下能做的
本轮已做完并全部过门禁（lite 3371+ passed / PG 413 / ruff 0.16.6
零违规 / mypy / lint-imports / lock）。

## 2026-09-04 发布完成：**80 / 100**

双注册表发布闭环（Release workflow 首次端到端全绿，run 33921542854）：

- **凭证**：npm + PyPI Trusted Publisher（OIDC，零 token）全部配置
  （owner articultur / repo memplex / workflow release.yml /
  environments pypi+npm，Touch ID 过码）。
- **发布链首飞修复**（该 workflow 此前从未运行过）：离线构建导入路径
  （release.py 按文件加载）、wheelhouse 污染干净检出、artifact 双层
  嵌套（upload 平铺 + merge-multiple 下载）、pypi-publish action 重钉
  v1.14.2（旧镜像已删）、npm 本地路径 `./` 前缀、npm package.json
  repository 字段（provenance 校验要求）。
- **G008 四主机真机门禁首飞**：self-hosted runner 网络代理、uv 托管
  CPython（替代需要 sudo 的 setup-python）、runner PATH Node 版本
  解析（openclaw 要求）、pytest basetemp symlink 摘要、wheel 安装下
  identity source_root 断言、四主机状态检查的嵌套键路径。
- **产物**：PyPI 3.3.0/3.3.1/3.3.2（不可变推进）+ npm 3.3.2（带
  sigstore provenance，透明度日志 logIndex 2716255795 起历次）；版本
  集九处声明同步，README 安装指令现实化为 3.3.2。
- 评分：DX/运维 2.5→5、签名公开制品达成 → **≈ 80/100**。

## 2026-09-06 SOTA 推进：容量与可观测性

- **播种容量三个数量级**（`refactor` 三连，988840c 等）：批次提交
  （600 文档 958s→33s）、builder 复用与名字索引（1500 文档 538s→50s）、
  增量居民校验 + **图边 O(N²) 病根治理**（ASSOCIATED_WITH 同域完全图
  与 DEPENDS_ON 共享词互连无边数上限——混合语料 3000 文档 165 万边
  + 11G RSS；加 per-function 上限后 5000 文档 **65s、~13ms/文档、
  10.3 万条线性边**）。latency_capacity 维度从"首个实测短板"到
  15 万文档级估算小时级——时间/多跳与真实用户任务的聚合任务评测
  （longmemeval 全量）容量解锁。
- **TriviaQA 适配器**：rc.nocontext 无证据文本导致全零的结构性缺陷
  修复（rc 配置 + parquet 形状适配 + 契约测试）。
- **可观测性 2.5→3.5**：新鲜签名 G006 报告达成（1661 请求 / p95
  5.77ms / 可用性 1.0 / ≥300s 窗口，HMAC+binding+alert-rules 全过，
  `report_id 0a53692b…`）；本地生成 runbook 固化在
  `docs/runbooks/production-operations.md`（含 /tmp symlink 与并发
  p95 两个坑）。
- 累计 ≈ **81.5~82 / 100**。剩余：语义栈公开对照 bundle（HF 网络
  窗口）、多租户第三方复跑、治理文档实质化、nq span 重建。

## 禁止性口径

本卡为内部差距分析；"benchmark-qualified" 仍以 G001 资格线（75 分 +
核心维度 level 3 + 不可变公共 raw evidence）为准——第 3 条未满足前
不宣称资格。
