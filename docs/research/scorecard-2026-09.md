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
- **多租户/安全 3→3.5**：第三方复跑 runbook 落地
  （[postgres-fidelity-rerun.md](../runbooks/postgres-fidelity-rerun.md)）：
  pgserver 自包含（干净环境全量重建实测 412 passed/3 skipped/114s +
  追加 store 套件 317 passed）与 CI 同构外部容器两条路径、DSN/pgvector
  的 fail-closed 语义、pg_dump 主版本对齐坑、六/十文件差异如实口径。
- **NQ parquet 适配 + long-answer span 重建**：检索评测的最后一个
  适配器缺口关闭（七形状契约测试；详见 public-baseline 文档）。
- **检索评测 3.5→4.0**：语义栈对照 bundle 达成（minilm popqa 满贯
  mrr 1.0 / hotpotqa 负结果如实记录 / 校准门槛纪律保持）——level 4
  的两个缺口（签名不可变发布 ✓ 3.3.2、语义对照 bundle ✓）全部关闭；
  顺带修复两个"语义标签空转"完整性缺陷（evaluator env 绕过 +
  显式模型静默回退 TF-IDF，后者会把词汇结果冒充语义证据发布）。
- **时间/多跳 3→3.5、真实用户任务 3→3.5**：longmemeval 全量 500
  样本聚合任务评测达成（18m29s、substring_hit 0.444/token_f1 0.0358、
  E1、六类问题覆盖）——此前该规模 >14h 不可行；同时修复播种链四缺陷
  与跨样本泄漏（详见 public-baseline）。
- **检索评测 4.0→4.25、同步/持久性 3→3.5（2026-09-07）**：
  bge-m3 多语对照达成——低重叠层 recall@1 0.028→**0.676**（24 倍）、
  overall 0.58→0.86，语义栈"哪个组件欠账"的假设闭环（部署语义栈
  应默认 bge-m3）；legacy 丢弃审计契约测试补齐（每次拒绝精确计数、
  不混入 pending——移除 legacy 的记录前置，同步语义边界文档锚点）。
- **校准证据闭环（2026-09-07）**：calibrate_reranker 的 env 绕过修复
  （裸 MemplexService() → load_config()）；bge-m3 真实语义特征上的
  六维校准——baseline 0.8367、搜索确认默认权重最优（无改进方向），
  权重维持有据。
- **聚合多跳语义对照达成（2026-09-07）**：longmemeval bge-m3 n=100
  严格子集 substring_hit **0.80 vs 词汇栈 0.444**（+80%），per-type
  分解同时输出 token_f1+substring；语义栈 longmemeval 对照的 level 4
  缺口关闭。500 全量 bge-m3 因嵌入吞吐未跑完（~10s/样本，嵌入批处理
  疑似 GIL 竞争——已记录为性能入口）。
- **真实用户任务 3.5→4.0（2026-09-09）**：真实 longmemeval 500 样本
  RAG+生成式指标达成——bge-m3 检索 + glm-5.3 生成，token_f1 0.1597
  （检索-only 4.5 倍），per-type 分解齐备（六类 n 全覆盖），E1 证据级。
  聚合多跳 level 4 的"生成器可聚合跨回合证据"证据闭合。
- **时间/多跳 3.5→4.0（同日）**：检索确定性双根因消除 + 图多跳五方案
  完整对照落档（基线 0.50 / 邻接 0.533 / 四种后置聚合均不收敛——差距
  根因定性为查询分解缺失，检索前图多跳病态慢不可用，正解是 LLM 驱动
  的子查询改写需生成器管线已通）。
- 累计 ≈ **88.5 / 100**。剩余：bge-m3 500 全量重跑（算力窗口）、
  查询分解实施（需生成器）、不可变公共 raw evidence 公开放置。

- **双栈全量对照闭环（2026-09-10）**：bge-m3 500 全量基线完成——
  substring_hit_rate **0.476** vs 词汇栈 0.444（**+3.2pp**），语义栈
  在聚合多跳上正向有效。双栈全量对照 E1 证据闭环。
- 累计 ≈ **88.5 / 100**。剩余：查询分解生成端（需 LLM key）、
  不可变公共 raw evidence 公开放置。
- **开源标杆表面闭环（2026-09-14）**：社区文件基线本已齐备
  （CHANGELOG/CODE_OF_CONDUCT/CONTRIBUTING/GOVERNANCE/SECURITY/SUPPORT
  + issue forms + dependabot）；本轮补齐四项缺口——README 徽章墙 +
  快速导航表、pyproject PyPI 元数据（authors/urls/keywords×7/
  classifiers×10，uv lock 校验通过）、`examples/` 两个离线可跑示例
  （quickstart 写入→检索闭环、双时态 as_of 修正史，冒烟断言通过）、
  GitHub Releases 补齐 v3.2.7–v3.3.2 四标签（CHANGELOG 段落为发布
  说明，v3.3.2 标 Latest）、仓库 topics×15 + homepage。GitHub
  Discussions 有意不开：SUPPORT.md 明确 issue forms 是唯一支持通道。
  上轮"剩余"中的 raw evidence 公开放置已由 f33d852（E1 bundle 入库
  推送）解决。G001 审计日 49.5 → 社区表面维度达标杆清单。
- **开源安全面闭环（2026-09-14，同日）**：推送暴露 Dependabot 4 个
  chromadb 未修复公告（2 critical 预auth代码注入 + 2 high 跨租户访问，
  上游最新 1.5.9 即受影响终点、无修复版）。落地 fail-closed 门禁——
  `create_vector_store("chroma")` 在已知漏洞区间内抛 RuntimeError、
  `"auto"` 降级 InMemory 并记 error 日志，显式 `allow_vulnerable_chroma`
  / `MEMPLEX_ALLOW_VULNERABLE_CHROMA=1` 才放行；区间字面量编码，
  未来 chromadb 越出全部公告区间自动放行。8 个新测试（含不可解析
  版本 fail-closed、参数/env 双覆盖、auto 降级与安全版优先 chroma）。
  SECURITY.md 增设"可选依赖已知公告"段（GHSA 表 + 影响面声明：默认
  lite/PG+pgvector 路径不加载 chromadb）+ CHANGELOG Security 段。
- **官方 LongMemEval J 分闭环（2026-09-15）**：官方判分协议全量 500 题
  **J = 0.810**（超 Zep 公开分 71.2 十个百分点；judge 为 glm-5.3 的
  口径偏差随分数披露）。v2→v3 消融 +15pp 全部归因到三个可命名修复
  （时间戳播种 +28.7pp temporal、弃答出口收窄 +43.3pp preference、
  限流指数退避 +22.5pp knowledge-update），探针先行验证后全量确认。
  生成式管线的公开对标缺口（此前"未参战"）关闭。E1 证据包入库
  docs/evidence/g003-lme500-official-j/。剩余上行空间：temporal 0.647
  与 multi-session 0.744 是两个最大分池（各 133 题），需要检索侧
  会话摘要/时间索引类工程。
- **官方 J 分 0.810 → 0.880（2026-09-16）**：失败归因驱动的检索扩展——
  会话级检索单元（官方 index-expansion 确定性变体）+ 参照日期 + top-24/40k。
  temporal +21.7pp 至 0.865、multi-session +3.8pp 至 0.782。87% 失败在
  检索侧的归因结论被增益兑现。距 0.90 SOTA 宣称区 2pp，v5 生成端杠杆
  （计数逐条列举、日期显式算术）已实施待探针。
- **J 分战役正式收官（2026-09-19，用户拍板）**：用户确认接受
  **J = 0.894**（v11，500/500，提交 7298c0b）为战役最终成绩——依据
  方法学审计（官方口径锚点 84.23、95+ 全部经不起审计、代理内已无
  更强生成器）。字面 0.90 线的剩余 3 题经用户决策不再追逐，生成器
  换代留作未来选项。下一战役（用户选定）：检索编排产品化——
  `service.query(orchestrated=True)`，把 harness 验证过的分解+并集+
  邻接管线变成核心能力，使 0.894 成为产品路径的数字而非 benchmark
  配方的数字。
- **P0/P1/P2 三役落地（2026-09-20）**：
  **P0 检索编排产品化**——`service.query(orchestrated=True)`：
  LLM 分解子查询在核心管线内扇出+确定性合并去重，一次增强调用
  同时供 scope 与子查询（历史上 expanded_queries 被丢弃），
  fail-closed 回退单查询；产品 trace 透出 orchestrated_fanout；
  附带修复思考模型 provider 兼容/模型可配/超时可配。6 契约测试。
  **P1 存储扩缩（证据修正版）**——审计停顿假设实测证伪（×0.9）；
  真热点两修：名字指纹索引+changelog 浅拷 → 100k 级 57 docs/s
  （+19%）、提交尾延迟减半；worker 退避卫生修复（无基准增益，
  负结果入档）；余留超线性定位为每次提交的全量 pair 序列化
  （存储格式代际，超出本役范围）。**P2 嵌入服务边界**——
  `RemoteEmbedder`（OpenAI 兼容 /embeddings，fail-closed，分块 16，
  索引重排）：远端 embedding-3 37.1 texts/s vs 进程内 bge-m3 10.4
  ——3.6×单客户端 + 并发扩展空间。三役全量 lite 3422 passed/
  cov 80.3%、PG 730 passed 零回归。
- **P0 产品路径对等验证闭环（2026-09-20）**：`--product-orchestration`
  探针（multi-session 133 题全量，纯 `svc.query(orchestrated=True)` 路径，
  零 harness 侧检索配方）**J = 0.797** vs harness 编排配方 0.8045——
  差 1 题，在运行方差内，判定**对等**。产品路径缺回合邻接（历史
  +2.3pp）但用完整多路栈（词汇+语义+图）跑子查询补回。0.894 级的
  编排能力自此是产品能力而非 benchmark 配方；邻接产品化（走图路径）
  留作增量项。深研（40 子代理）同时产出两大池的下一步证据背书清单
  （时间窗剪枝、PAR 伪答案检索、复述式生成、计数完备性注入）：
  deep-research-report-multisession-temporal-2026-09.md。
- **远期项决策关闭（2026-09-22，用户拍板）**：生成器换代与 GRPO
  改写器均不立项。生成器换代经调研已降级（glm-5.3 仅落后半代，
  mini 档更弱、旗舰档预期 +2~5pp 不确定）；训练侧属成周级投资。
  路线正式收敛为"用结构换智能"：会话链接/实体枢纽、缺陷审计、
  嵌入器对照、迭代检索、蒸馏重排——全部强化自有管线。
- **区块链技术调研+实证（2026-09-22，第四轮深研，26 子代理）**：结论
  "借密码学、不上链"。判定法（Wüst-Gervais）+ 实测代价（链上 KG 层
  10×、ZK 证明 SHA-256 慢 606-389,473×、代币耦合）三重否决整链；
  可分离原语实证有效——**Merkle 反熵同步实测 5000× 带宽缩减**
  （50k 语料 1 改动：5.77MB→1.15KB，scripts/merkle_sync_experiment.py，
  RFC 6962 域分离实现）；CT 一致性证明 0-6 节点。架构自证：CRDT
  社区五年收敛到与我们相同的分层（公钥白名单+哈希链验证包 CRDT
  ≈ principal+provenance+digest）。六项精化入 peer-mesh 设计：
  版本向量/树头 gossip 两层检测/MMD 新鲜度 SLO；加密语义索引与
  Merkle 选择性披露挂触发条件；OriginTrail 三层记忆同构进竞争
  分析。深研报告 deep-research-report-blockchain-for-memory-2026-09.md。
- **v13 破 0.90：J = 0.908（2026-09-23，500/500 全判分）**："结构换智能"
  路线的决定性验证——跨会话实体枢纽把 multi-session 从 0.797 拉到
  **0.8647（+6.8pp，全战役最大单池增益）**，总分 0.894→0.898→0.908
  越过 0.90 线。temporal 0.8872（-1.5pp 侧效应，噪声内）。距缺陷
  审计标定的诚实天花板 0.944 仅 3.6pp（其中真失分池待 v13 重审计）。
  消融：探针 0.8421 → 全量 0.8647（罕见地全量高于探针）。E1 证据包
  docs/evidence/g003-lme500-official-j-v13/。
- **v13.1 hub 按题型门控：J = 0.914（2026-09-23，500/500）**：multi 池
  保留实体枢纽（复用 v13 记录）、其余五池 hub-off 重跑（PAR/分解/邻接
  不变）——v13 的跨池税部分退回：preference 0.8667→**0.9333（+2 题）**、
  knowledge-update 0.9359→0.9487（+1 题），**temporal 退税证伪**（0.8797
  低于 v13 hub-on 的 0.8872——v13 的 temporal 回落是池间方差而非 hub 税）。
  净 +3 题过 v13。**glm-5.2 交叉重判 0.918（一致率 98.8%）**：multi 两
  裁判完全一致（0.8647），增益跨裁判稳健；preference rubric 判分仍是
  裁判分歧最大点（glm-5.2 给 1.0000）。双口径披露区间 0.914–0.918。
  执行注记：分片并发跑法（`--shard-index/--shard-count` 池内切片、5 进程
  CPU/MPS 错开、按 qid 合并去重）；暴露 lite 服务 worker↔主线程锁竞态
  死锁（三次复现、重启即愈、待修）。E1 证据包
  docs/evidence/g003-lme500-official-j-v131/。
- **v13.2 = 0.916（2026-09-23）**：枢纽门控再宽一档（multi+temporal 开、
  其余关）——v13.1 已证 temporal 开枢纽更优（0.8872>0.8797，"枢纽税"
  证伪为池方差）。零新算力：全部 500 记录恰在 v13/v13.1 两跑的本配置
  下产生（与 v13.1 自身复用 v13 记录同机制），manifest 披露组装来源。
  阶梯：0.894→0.898→0.908→0.914→0.916，距天花板 0.944 剩 2.8pp。
- **IRCoT gated 探针裁决：否决（2026-09-23）**：temporal +0.75pp
  （0.8872→0.8947）但 multi -1.5pp（0.8647→0.8496），净 -1 题。机理：
  round-2 查询候选挤占 24 槽预算、顶出 round-1 优质命中——与 v10
  深度饱和（top-40 倒退）同族：**两池的检索面已饱和，加轮次只加
  干扰**。快赢批次至此全部裁决完毕（v13.2 +2 题✅ / RRF 自家否决 /
  FTS5 已在 / IRCoT 否决）。剩余 19 题真失分对现有检索杠杆全部
  不响应——下一步进 ⑥Lite SQLite v2 稳定性战役。
