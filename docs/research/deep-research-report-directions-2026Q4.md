# 深度调研报告：能力增量方向（2026 Q4）

> 承接三篇前作：[memory-landscape-2026-09.md](memory-landscape-2026-09.md)（全景）、
> [scorecard-2026-09.md](scorecard-2026-09.md)（已裁决）、
> [deep-research-report-capability-directions-2026-09.md](deep-research-report-capability-directions-2026-09.md)（2026-09 增量）。
> 本报告只研究**delta**：2026 Q3-Q4 新发布且未被已有裁决覆盖的增量方向。
> 执行时点：2026-09-28。系统状态基线：官方判分 0.916-0.918（天花板 0.944）、
> 产品 parity 0.7669（CPU 口径）、红队 ASR 0.20、B1 resolved 2/25、
> ADR-012 Phase B 全部落地、provenance 信任级 + 段落原话层 + 单写者队列已上线。

## 问题

对于上述状态的 Memplex，2026 Q3-Q4 的研究与业界发布中，还有哪些值得探索和优化的新方向？

## 执行摘要

四条最值得投入的新方向：**①立即审计 compaction 管线的治理约束保留**（1,323 episodes 的规模化证据显示压缩静默删除治理约束、违规 0%→30%+，有免训练修复，且我们是唯一有 compaction+信任级+锁定三层防线可直接验证的系统）；**②加性信任权重的本地复现与 quota-based 重设计**（Quantify Labs 报告加性 provenance 权重无可用的有效设置——直接挑战 trust_tier 的设计假设，需本地复现后决策）；**③离线巩固循环**（点状记忆→持续型记忆的语义时间晋升 + 算法遗忘，多篇 2026 预印本报告最高 +12.2%，与 typed 双时态架构天然契合，但全部未经同行评审）；**④第二战场评估**（年尺度多源 LifeBench 型负载与非陈述性记忆，顶级系统仅 55.2%，旧基准已饱和）。

## 白话结论（供本文件独立成篇）

- **最便宜且最相关的一步**：审计我们自己的 compaction 管线是否静默删除治理/信任约束。我们有 compaction + 信任级 + 锁定三层防线，是唯一能本地复现 governance decay 的系统——验证成本最低。
- **最尖锐的设计挑战**：Quantify Labs 的报告说加性 provenance 信任权重"没有有效的设置"——这直接打在 trust_tier 的加权检索设计上。需要本地复现；若成立，quota-based 有界占用是候选替代。
- **最有产出的中期方向**：离线巩固循环（episodic→sustained 晋升 + 算法遗忘）。与 typed 双时态架构天然契合，可只用规则/启发式实现（剥离 Auto-Dreamer 的 GRPO 组件以避开已否决的 RL 方向）。
- **第二战场评估**：年尺度多源（LifeBench 型）与非陈述性记忆是当前顶级系统都不行的空白域，但是否值得开第二战线需要先测自有负载的契合度。
- **边界确认**：长上下文不均匀退化 + 成本边界（~10 轮后记忆更便宜）继续为外部记忆路线提供必要性支撑——投入方向应集中在 persona 驱动多轮等优势负载。

## 发现（7 条，按优先级）

### F1. Governance Decay：compaction 静默删除治理约束 【置信度：高】

1,323 episodes、七个模型族：压缩后违规率 0%→平均 30%（部分模型 59%）；因果机制确认（约束存活于摘要则违规 0%，被删则 38%）；Compaction-Eviction Attack 的优化注入击败所有受评模型；Constraint Pinning 训练免费修复至 0%（其自建基准）。

**对 Memplex 的含义**：立即审计自有 compaction 管线的治理/信任约束保留率——我们有 compaction + trust_tier + 锁定三层防线，是唯一能本地复现 decay 的系统；验证成本最低。注意 0% 修复仅限其自建 benchmark。

- 来源：[arXiv:2606.22528](https://arxiv.org/abs/2606.22528)（单作者 preprint v2，2:0 通过）

### F2. 加性信任权重失效与 quota-based 重设计 【置信度：中（需本地复现）】

无 payload 投毒：内容筛选拒绝 0/360（"虚假性不是文本可扫描属性"）；1.2% 中毒占比使 0.85→0.30；provenance 加性信任权重 w=0.15 无效（p=0.80）、w=0.35 仅因排除性起效——**没有可用的加性设置**；提出 quota-based 有界占用但承认未实现；主张 utility-retained 协议 + FPR 强制报告。

**对 Memplex 的含义**：用自有 trust_tier 数据复现"加性权重无有效设置"；若成立，评估 quota-based 有界占用在单写者写路径上的可行性；按 MPBench taxonomy（4 写入通道×9 攻击模式）扩充红队回归集。注意：单作者行业预印本、quota 设计未实现。

- 来源：[arXiv:2608.21230](https://arxiv.org/html/2608.21230)（2:0）、[A-MemGuard ICML 2026](https://icml.cc/virtual/2026/poster/61006)（共识验证+双重记忆，ASR -95%+，2:0）、[arXiv:2606.04329](https://arxiv.org/html/2606.04329v1)（taxonomy，2:0）

### F3. 离线巩固循环：episodic→sustained 晋升 + 算法遗忘 【置信度：中】

TSM：按语义时间轴组织点状记忆并将时间连续且语义相关的信息巩固为持续型记忆，LongMemEval/LoCoMo 最高 +12.2% 绝对提升（CAS preprint v2，2:0，证据最强）。Auto-Dreamer：GRPO 训练的离线巩固器，ScienceWorld +7 分、记忆库小 12×、跨环境泛化（UIUC/UCSD，2:0 通过但未经同行评审——**其 GRPO 组件与已否决的 RL 方向冲突，只应借鉴离线巩固架构本身**）。SCM：睡眠阶段巩固 + 算法遗忘（单作者研究预览，最弱源，2:0 通过但带保留）。Sleep-time Compute（Letta）为概念先声。

**对 Memplex 的含义**：与 typed 双时态记忆的 episodic→semantic 晋升天然契合；可用规则/启发式实现巩固循环而不碰 RL；不改动在线检索路径（0.7669 的 primary 融合保持不变）。0.7669→0.944 天花板约 0.18 的差距中，巩固类预计能吃掉的部分需按自身差距重新测算（论文的 +12.2% 是在未达天花板的系统上测得）。

- 来源：[arXiv:2601.07468](https://arxiv.org/abs/2601.07468)（2:0）、[arXiv:2605.20616](https://arxiv.org/abs/2605.20616)（2:0）、[arXiv:2604.20943](https://arxiv.org/abs/2604.20943)（2:0 带保留）

### F4. 新一代基准揭示未解问题域 【置信度：高】

LifeBench：年尺度（3.66M tokens/用户、10 用户、2,003 问）、多源数字痕迹（通话/短信/日历/照片/健康记录）、独有非陈述性记忆任务（习惯/技能/情绪/偏好，429 问）；顶级系统 MemOS 仅 55.2%、Hindsight 40.99%（后者在 LoCoMo/LongMemEval ~90%）——旧基准饱和论据。Agent-native 评测（12 系统×5 工作负载）：无单一架构全面占优、检索随时间距离退化、**局部维护优于全局重组**（互证单写者+主动整理）、**原始内容保留优于摘要**（互证段落原话层）。EvoMemBench 自进化两轴。LongMemEval-V2 被 ICML 2026 正式发表——在线口径确认为社区共识。

**对 Memplex 的含义**：非陈述性记忆（习惯/技能/情绪/偏好）是 typed 体系未覆盖的记忆类型，构成潜在第二战场；系统级评测的多项结论与现有设计互证。

- 来源：[arXiv:2603.03781](https://arxiv.org/html/2603.03781v1)（2:0）、[arXiv:2606.24775](https://arxiv.org/html/2606.24775v1)（2:0）、[arXiv:2605.18421](https://arxiv.org/html/2605.18421v2)（2:0）

### F5. 上下文工程杠杆 【置信度：中】

ACE（Stanford，ICLR 2026）：context 当演化 playbook（生成/反思/整理三模块），既有方法的 brevity bias 与 context collapse 两种失败模式由结构化增量更新防住，+10.6% agents/+8.6% finance；适用系统提示与 agent memory。写时路由（记忆落在哪层/tier/注入点在写入时决策）与独立 context-manager 模型（CoMem）为相邻方向。

**对 Memplex 的含义**：ACE 的增量更新防 collapse 论点与段落原话层的 append 优先设计互证；写时路由与现有 typed/tier 结构兼容度高——把层级决策前移到写入侧是编排查询的自然延伸。注意：+10.6% 为对强基线的平均提升，落地收益需在自有管线实测。

- 来源：[arXiv:2510.04618](https://arxiv.org/abs/2510.04618)（ICLR 2026，2:0）、[arXiv:2608.22215](https://arxiv.org/html/2608.22215v1)、[arXiv:2605.30842](https://arxiv.org/html/2605.30842)

### F6. MATM 群体级轨迹记忆 【置信度：低-中（证据单薄）】

agent 轨迹的群体级存储/检索（生产者-消费者、无需显式协调），ALFWorld/WebArena 上提升表现减少步数。与已搁置的"跨 agent 语义层"不同——复用的是过程性轨迹而非语义合并——可作为 peer-mesh 之上的增量，但采纳前需划清轨迹级复用与语义合并的边界。

- 来源：[arXiv:2606.19911](https://arxiv.org/abs/2606.19911)（2:0，预印本无同行评审）

### F7. Context Rot 与成本-性能边界（边界条件） 【置信度：高】

18 模型上性能随输入长度不均匀退化（Chroma 系统研究，2:0）；NIAH 高估长上下文；LongMemEval 上聚焦提示远优于完整长提示；成本边界：100k 上下文下事实记忆约 10 轮后更便宜（2:0，注意 PersonaMemv2 上 LC GPT-5-mini 仍以 7.3pp 优于 Mem0——"记忆优势负载"应读为成本优势场景）。2026 年新模型的退化更平缓但未解决（独立佐证）。

**对 Memplex 的含义**：边界条件——把资源集中在 persona 驱动多轮等优势负载；为"哪些负载不值得投入记忆"给出排除标准。

- 来源：[Chroma Context Rot](https://www.trychroma.com/research/context-rot)（2:0×2）、[arXiv:2603.04814](https://arxiv.org/abs/2603.04814)（2:0 带精度保留）

## 被驳回的主张

无（本轮 18 条 canonical claims 全部 2:0 通过，无 tiebreak、无 killed、无 unverified）。

## 保留意见（Caveats）

- **预印本密集**：巩固类全部为 2026 预印本未经同行评审；SCM 为单作者研究预览（最弱源）；安全方向最尖锐结论来自单作者行业预印本且其 quota 方案未实现。
- **跨基准不可比**：+12.2%、55.2%、12× 等自报数字跨基准不可直接比较；巩固类收益需按自身 0.7669→0.944 差距重新测算。
- **GRPO 边界**：Auto-Dreamer 的巩固器依赖 GRPO 训练——借鉴架构时必须剥离 RL 组件（已否决方向的边界）。
- **时效**：2026 上半年文献密集，判断建议 3-6 个月后复核。
- **MPS 设备**：本机 MPS 后端第四次 wedge，设备对齐重跑在 MPS 稳定前不可行；CPU 带 0.7669 为正式读数。

## 开放问题

1. 自有 compaction 管线中治理/信任约束的实际保留率是多少？本地复现 governance decay 后，Constraint Pinning 的免训练等价物（约束重注入/固定）是否足够？
2. 用自有 trust_tier 数据能否复现"加性 provenance 权重无有效设置"？若成立，quota-based 有界占用在单写者写路径上如何实现？
3. 0.7669→0.944 差距中，TSM 式离线巩固预计能吃掉多少？年尺度/非陈述性负载（LifeBench 型）是否值得作为第二战场？
4. MATM 轨迹级群体复用是否落在已搁置的"跨 agent 语义层"决定之外？挂接在 peer-mesh 之上还是旁边？

## 来源清单（11 个抓取来源）

| 来源 | 质量 | 角度 | 主张数 |
| --- | --- | --- | ---: |
| [arXiv:2605.20616](https://arxiv.org/abs/2605.20616) Auto-Dreamer | primary | 巩固/遗忘 | 5 |
| [arXiv:2604.20943](https://arxiv.org/abs/2604.20943) SCM | primary(弱) | 巩固/遗忘 | 5 |
| [arXiv:2601.07468](https://arxiv.org/abs/2601.07468) TSM | primary | 巩固/遗忘 | 5 |
| [arXiv:2603.03781](https://arxiv.org/html/2603.03781v1) LifeBench | primary | 评测 | 5 |
| [arXiv:2606.24775](https://arxiv.org/html/2606.24775v1) Agent-Native 评测 | primary | 评测 | 5 |
| [arXiv:2605.18421](https://arxiv.org/html/2605.18421v2) EvoMemBench | primary | 评测 | — |
| [arXiv:2510.04618](https://arxiv.org/abs/2510.04618) ACE | primary(ICLR) | 上下文工程 | 5 |
| [arXiv:2606.22528](https://arxiv.org/abs/2606.22528) Governance Decay | primary(preprint) | 上下文工程/安全 | 5 |
| [arXiv:2608.21230](https://arxiv.org/html/2608.21230) Utility Under Attack | primary(弱) | 安全 | 5 |
| [ICML 2026 A-MemGuard](https://icml.cc/virtual/2026/poster/61006) | primary(ICML) | 安全 | 5 |
| [arXiv:2606.19911](https://arxiv.org/abs/2606.19911) MATM | primary | 多 agent | 5 |
| [Chroma Context Rot](https://www.trychroma.com/research/context-rot) | primary(行业) | 反向视角 | 5 |
| [arXiv:2603.04814](https://arxiv.org/abs/2603.04814) 成本-性能边界 | primary | 反向视角 | 5 |

验证期补充：MPBench、A-MemGuard arXiv 版、MemSentry (2026-09)、Sleep-time Compute、MemoryAgentBench、awesome-agent-memory。

## 统计行

`6 angles / 11 sourcesFetched / ~48 claimsExtracted / 18 canonicalClaims / 18 confirmed·0 killed·0 unverified / 17 agentCalls`

流程：六角度并行检索 → 去重配额 → 逐源提取 → 语义合并为 18 条 canonical → 每条 2 名独立核查员（A 忠实性/来源强度、B 反证/时效）→ 全部 2:0 通过（无 tiebreak）。全部核查员结论 JSON 回传逐字段校验。
