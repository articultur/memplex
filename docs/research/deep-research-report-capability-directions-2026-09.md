# 深度调研报告：长期记忆系统能力增量方向（2026-09-24）

> 承接 [memory-landscape-2026-09.md](memory-landscape-2026-09.md)（2026-09-04 全景调研）与
> [scorecard-2026-09.md](scorecard-2026-09.md) 的已裁决事项（蒸馏 NO-GO / IRCoT 否决 /
> RRF 否决 / 生成器换代与 GRPO 不立项 / 上链否决 / 多模态 parked），本报告只研究**增量**：
> 已有强检索/授权/同步之外的**新能力方向**。

## 问题

对于一个 2026 年末已相当强的 agent 长期记忆系统（typed 双时态记忆、混合词汇+语义+图检索 +
LLM 编排查询分解 + 实体枢纽、多租户授权、peer-mesh durable 同步、闲置期维护），还有哪些来自
研究与业界的新能力方向能实质提升其长期记忆能力（而非既有检索的增量调参）？

## 执行摘要

研究和业界共识指向的最大增量不是继续调检索参数，而是四类新能力：

1. **让系统知道记忆何时已经失效并敢于推翻旧结论**——全领域当前最弱环节（最强模型判断已存记忆
   是否仍有效的正确率约 55%，六款主流记忆系统频繁复用无效记忆），需要把形式化信念修正、来源
   权威追踪和审慎剪枝做进记忆本体。
2. **把智能从查询端搬到写入端**：写入时生成情境前缀与未来场景索引、用强化学习训练记忆管理
   策略，让维护从手写启发式变成可学习、可验证的闭环。
3. **记忆读写通道是新攻击面**：投毒写入可跨会话潜伏并被优先执行，召回行为本身会泄露"记没记过
   某事"，需要来源权威分级与召回侧信道防御这类结构性防护。
4. **压缩有真实代价**：抽取和摘要会抹掉原话与表面形式（同等预算下抽取式图记忆反而不如平坦向量
   检索），原生文本应作权威层、抽象层级只作派生视图；精确时间戳与时间关系推理仍是所有方法共同
   短板。

多数方向证据方向一致但幅度存疑（厂商自报或单篇预印本）；新能力应在线上流式口径下严格 A/B
再上线。

## 白话结论（与聊天层一致，供本文件独立成篇）

- 检索侧接近饱和（J=0.916 距审计天花板 0.944 剩 2.8pp、19 题不响应检索杠杆）之后，本轮核验
  过的增量几乎全部在检索之外：遗忘与失效、信任与溯源、原文保真、时间推理、写入端富化。
- 遗忘/失效检测是文献中反复独立复现的最大缺口，且与双时态记录正交：`as_of` 记录了"系统何时
  改写"，但世界变化造成的语义失效不自带时间戳。
- 防御层（投毒/溯源洗白/成员推断）与多租户授权是互补而非替代关系：现有注入扫描属于检测计数，
  不构成"来源权威不被合并改写放大"的保证。
- 写入端富化（情境前缀、前瞻索引）与既有 sleep_time 守护进程天然契合，是迁移成本最低的方向。
- 每个方向都带幅度限定：独立复现普遍比自报数字温和。

## 发现（10 项，按置信度与优先级）

### F1. 陈旧/失效记忆的检测与信念更新是领域最大开放缺口 【置信度：高】

最强前沿模型在 STALE 基准上判断"已存记忆是否仍有效"仅 **55.2%**（400 专家校验冲突场景 /
1200 查询）；agent **取回了更新证据却不据此行动**——失分在"对已检索证据的状态感知推理"而非
检索本身。三个可分离维度：状态消解（State Resolution）、前提抵抗（Premise Resistance）、隐式
策略适应（Implicit Policy Adaptation）；核心失败模式是**隐式冲突**（后来的观察使早先记忆失效
但没有任何显式否定）。Memora 基准（ACL 2026 Findings）独立收敛：六款记忆 agent（A-Mem /
LangMem / Mem-0 / MemoBase / MemoryOS / Nemori）频繁复用无效记忆、相对裸 LLM 仅边际增益；
**64% 的推荐类错误源于未遗忘的过时记忆**；随时间跨度恶化（FAMA 惩罚 18.2→29.5，周→季）。
形式化路径已被证明可行：Kumiho 证明属性图记忆操作满足 **AGM 信念修正公理 K\*2–K\*6 与
Hansson 信念基公理**（Relevance / Core-Retainment；命题 7.1–7.7 + 49 场景自动合规套件
100% 通过）。审慎剪枝方向：单次剪除 27,021 节点图的 9.8% 节点 / 9.5% 字节后 token F1
不变（+0.001，CI [-0.015, +0.016]），总体判定正确率损失上限约 3.8 点（总体 95% CI；见
R1 的转述勘误）。

**对 Memplex 的含义**：J=0.916 不测量这件事。编排层（`service.query(orchestrated=True)`）已有
分解/枢纽/PAR 三条腿，"前提抵抗 + 依据新证据推翻旧记忆"是第四条腿的自然候选；AGM 公理化与
`temporal.py` 的 supersede 语义同构，可作 `improve.py` 维护策略的形式化验收标准。

- 来源：[arXiv:2605.06527](https://arxiv.org/abs/2605.06527)（STALE）、
  [arXiv:2604.20006](https://arxiv.org/abs/2604.20006)（Memora）、
  [arXiv:2603.17244](https://arxiv.org/abs/2603.17244)（Kumiho/AGM）、
  [arXiv:2608.28978](https://arxiv.org/abs/2608.28978)（剪枝，修正后口径）
- 验证：stale-detection / invalid-reuse / error-attribution / agm-belief-revision 四项均双票通过
  （STALE 另有独立跟进 [arXiv:2608.01619](https://arxiv.org/abs/2608.01619) 佐证缺口持续存在）

### F2. 记忆投毒与来源权威防御层 【置信度：高】

记忆读写通道是结构性新攻击面：MPBench（6 攻击类 / 4 写入通道 / 9 结构漏洞）显示**写入/检索越
积极的 agent 越易被利用**（平均 ASR ≈50.46%；MemPoison 未防御口径 ~98%；MemSecBench 跨会话
持久化 84.2%），且**既有提示注入防御不覆盖记忆投毒**。Unit 42 真实 PoC（2025-10，Amazon
Bedrock）：网页间接注入在**会话摘要时**无声污染长期记忆、跨会话存活、在编排提示中**优先于用户
输入**、后续触发静默外泄（AWS 确认并发布防御指引）。PPMF 论文命名了**溯源洗白（provenance
laundering）**：合并/巩固时不可信外部观察被改写成"看似可信的用户历史"，提示过滤/净化器/工具
守卫均不保证来源权威不被放大；其方案（平台维护的 provenance + 与记忆权威等级绑定的工具调用
授权）在自评中把 ASR 1.000 降到零未授权高危动作。MINJA 系防御：复合信任 I/O 审核 + 信任感知
检索（时间衰减 + 模式过滤）；预存合法记忆显著稀释注入效果；信任阈值校准困难（设错即全拦或漏防）。

**对 Memplex 的含义**：`llm/injection_guard.py` 是检测计数，不是权威不放大保证；四宿主
recall-before-turn 恰是"编排提示优先"结构的镜像。provenance/信任级若成为 schema 一等公民，
**必须纳入 peer-mesh 同步契约，否则权威标签在对等节点间漂移**——ADR-012 Lite SQLite v2 尚在
Phase A，现在是决定 schema 的时机。

- 来源：[arXiv:2606.04329](https://arxiv.org/abs/2606.04329)（MPBench）、
  [Unit 42](https://unit42.paloaltonetworks.com/indirect-prompt-injection-poisons-ai-longterm-memory/)、
  [arXiv:2607.29167](https://arxiv.org/abs/2607.29167)（PPMF）、
  [arXiv:2503.03704](https://arxiv.org/abs/2503.03704)（MINJA 正典）+
  [arXiv:2601.05504](https://arxiv.org/abs/2601.05504)（攻防后续）
- 验证：poisoning-tension / unit42-poc / provenance-firewall / minja-defense 四项均双票通过；
  PPMF 的 1.000→0 为作者自评（未独立复现）；MINJA 编号之争已核实（两篇皆真，2601.05504 为
  引用正典数字的后续论文）

### F3. 原生保真 vs 压缩的张力 【置信度：高】

有损压缩/抽取会丢表面形式。同等检索预算下，抽取式 KG 记忆在 LongMemEval 上 token F1
**0.417 vs 平坦向量 0.468**（CI [-0.085, -0.016]），对"先前 assistant 原话"的精确召回从
0.911 掉到 **0.607**（实体抽取抹掉表面形式；作者自限：结论适用于抽取式管线而非图记忆一般
情形；Zep 自家消融"纯图逊于混合"方向一致）。LifeDialBench（ACL 2026 Findings，真实自我中心
视频 EgoMem + 模拟社区 LifeMem，在线流式协议）：RAG/A-Mem/MemOS/Mem0 四系统中**复杂系统
未能胜过简单 RAG**，原生文本保持显著优于摘要式，**压缩程度与损失正相关**（保留 ~62% token 的
MemOS 优于 ~35% 的 Mem0）。限定：RAG 优势在年级跨度收窄（Fig 4）。

**对 Memplex 的含义**：原生 turn/会话文本应作权威层（lite pair + JSON journal 已是），wiki/
摘要/图边作派生视图且**必须可回溯到原话**——与 compaction 5 段管线的张力需要在 v2 存储格式
里显式回答（保留原文引用指针）。

- 来源：[arXiv:2608.28978](https://arxiv.org/abs/2608.28978)、
  [arXiv:2604.11182](https://arxiv.org/abs/2604.11182)
- 验证：graph-vs-flat / raw-fidelity 双票通过（后者逐字核至全文表格）

### F4. 时间 grounding 是全方法公共瓶颈 【置信度：高】

精确时间戳 grounding 与时间关系推理（多跳事件链、先后/包含关系）是 LifeDialBench 全部受测
方法的一致最弱项；LongMemEval 与 LoCoMo 的独立分析报告同类结论。**这与存储是否双时态正交**：
`as_of` 过滤不等于时间关系求解。

**对 Memplex 的含义**：v13.x 的 temporal 池 0.8872 与此吻合；下一步杠杆是编排层的时间约束
求解/事件链遍历（查询分解的自然扩展），而非检索参数。

- 来源：[arXiv:2604.11182](https://arxiv.org/abs/2604.11182) + LongMemEval/LoCoMo 独立佐证
- 验证：双票通过

### F5. 写入时富化（contextual retrieval + 前瞻索引）【置信度：中】

写入管道做一次性 LLM 富化而非查询时补救：为每条记忆生成 50–100 token 情境前缀再做嵌入与
BM25 索引——Anthropic 自报 top-20 检索失败率 **-35%（仅嵌入）/ -49%（+情境 BM25）/
-67%（+重排）**，一次性成本 $1.02/M tokens（提示缓存下）；ECIR 2025 独立复现**方向成立但
幅度温和**（强嵌入器下 +0.005–0.012 nDCG@5）。前瞻索引（prospective indexing）：写入时生成
并索引"未来场景蕴含查询"，论文报告消除 >6 个月准确率断崖（37.5%→84.4%）；该思路有
docTTTTTquery 与 Letta sleep-time 两条独立谱系佐证；教训：未过滤的生成查询会幻觉化并损害
检索（Doc2Query--）。

**对 Memplex 的含义**：与 sleep_time 守护进程天然契合（闲置期批量富化），RemoteEmbedder 边界
已就绪；写入吞吐依赖 Lite SQLite v2 增益（两战役汇合点）。

- 来源：[Anthropic](https://www.anthropic.com/news/contextual-retrieval)、
  [arXiv:2504.19754](https://arxiv.org/abs/2504.19754)（ECIR 复现）、
  [arXiv:2603.17244](https://arxiv.org/abs/2603.17244)
- 验证：双票通过；幅度证据分层（厂商自报 vs 独立复现）已标注

### F6. 召回通道的隐私侧信道 【置信度：中】

MRMMIA：通过多次召回探测推断某条交互/事实/偏好**是否存在于 agent 记忆库**（成员推断；黑/
灰/白盒均可行、优于既有 MIA 基线）。ACL 与加密不覆盖"召回行为本身"。RAG-MIA 谱系
（SIGSAC 2025）独立佐证。能力方向：召回速率/重复探测限制、响应一致性最小化、记忆单元级
membership 防御。

- 来源：[arXiv:2605.27825](https://arxiv.org/abs/2605.27825)
- 验证：双票通过（单主源 + 谱系佐证）

### F7. 学习式记忆管理（RL memory management）【置信度：中】

Mem-α（~83 引用）：以全交互历史上的下游 QA 准确率为奖励，RL 训练记忆管理策略（写什么/何时
更新/删什么）；30k token 训练泛化到 400k+（13 倍）；已被 Memory-R1 / Mem-T / TrustMem /
R²-Mem 扩展。警示：训练成本高、可用骨干偏小——与 2026-09-22 已裁决"训练侧成周级投资不立项"
同级。**最小落点**：用该范式离线评估现有 dedupe/expire/压缩启发式（影子策略对比），而非训练。

- 来源：[arXiv:2509.25911](https://arxiv.org/abs/2509.25911)
- 验证：双票通过（注：搜索层曾误标为 MemoryAgentBench，提取时已纠正）

### F8. 层级/抽象索引：方向真实、孤立贡献有争议 【置信度：中】

RAPTOR 的 +20pp QuALITY 与 GPT-4 耦合，作者 OpenReview 回复承认**未跑 GPT-4 + 简单检索
对照**，检索-only 边际约 2–5pp；H-MEM 多层语义抽象 + 位置索引路由胜 5 基线，但为自报且
LoCoMo 基准有 6.4% 答案键错误的审计污点、无独立复现；2026 生活日志证据显示摘要式记忆逊于
原生文本 RAG（见 F3）。**若采纳应作原生文本之上的可选视图**（用层级路由省检索预算），并以
自建基准严格 A/B。

- 来源：[arXiv:2401.18059](https://arxiv.org/abs/2401.18059)（+OpenReview jE7tbEQGky）、
  [arXiv:2507.22925](https://arxiv.org/abs/2507.22925)
- 验证：两项均经决胜存活——以"如实转述论文原话"为标准；附加反证警示已并入结论

### F9. 跨 agent 语义共享记忆 【置信度：中】

在 peer-mesh durable sync（状态复制）之上增加面向消费者的语义层：agent 写入带**受众/影响
标签**的变更记录（"这里变了什么"，互补于 AGENTS.md 的"规则是什么"），供其他服务/仓库的 agent
检索消费。Mem0 受控两服务实验：检索另一 agent 的记忆记录（事件字段 status→statusCode 重命名）
阻止了静默漏发收据的 bug。须同时纳入 fleet-memory 失败模式的形式化（泄漏、陈旧传播、矛盾
持久化、写污染，arXiv:2606.24535）。厂商演示级证据、无独立复现。

- 来源：[Mem0 blog](https://mem0.ai/blog/beyond-agents.md-shared-memory-for-coding-agents-across-services-and-repos)、
  [arXiv:2606.24535](https://arxiv.org/abs/2606.24535)
- 验证：双票通过（B 侧标注厂商演示级 + 失败模式风险面）

### F10. 评测方法论：在线流式口径 + 遗忘感知指标 【置信度：高】

离线记忆评测不可靠：未来上下文污染是不可控混杂（LifeDialBench：Mem0 在线答对的问句，离线
重建记忆后仅 **34.91%** 正确，top-k 20→100 亦无济，归因于离线构建时的不可逆覆写）；现有基准
偏重事实检索、欠评巩固/遗忘，FAMA 显式惩罚过时记忆依赖（先例限定：LongMemEval 知识更新与
MemoryAgentBench 选择性遗忘已部分覆盖）。

**对 Memplex 的含义**：新能力验收应在线流式口径下 A/B（与 LongMemEval-V2 的在线协议及现有
E1 证据纪律一致），避免离线快照的虚高/虚低。

- 来源：[arXiv:2604.11182](https://arxiv.org/abs/2604.11182)、[arXiv:2604.20006](https://arxiv.org/abs/2604.20006)
- 验证：双票通过（C21 经决胜裁定：原始文本与论文逐字一致；被 B 侧驳回的"(lower)"方向词为
  转述引入、非原始主张内容）

## 被驳回的主张（透明度）

### R1. 选择性遗忘的"分题型安全保证"（arXiv:2608.28978）

- **原始表述**：剪除 27,021 节点图的 9.8% 节点后 token F1 不变（+0.001），"**任何题型**判定
  正确率损失至多 ~3.8 点"。
- **驳回原因**：论文的 3.8 点是**全体 500 题的聚合 95% 置信区间**（judged correctness 总体降
  1.6 点，CI 上界 3.8），论文**没有**分题型的剪枝后数据。转述把它强化成了分题型一致性保证。
- **修正后口径（可通过）**：9.8% 节点/9.5% 字节、token F1 +0.001（CI [-0.015,+0.016]）、总体
  正确率损失上限约 3.8 点（聚合）；评分因子为 recency/access frequency/degree centrality/age。
  方向性结论（审慎剪枝可安全收缩记忆）仍被同一论文支持——但注意它测于基线低于平坦 RAG 的系统。

## 未能验证的主张

无。全部 21 条规范化主张均完成双角度核验（含 4 次决胜仲裁）。

## 保留意见（Caveats）

- **自报/单源打折**：Kumiho（前瞻索引、AGM 套件）为单作者预印本；PPMF 1.000→0 为作者自评；
  Anthropic 情境化检索幅度为厂商自报（独立复现温和）；Mem0 跨 agent 演示无独立复现。
- **基准质量**：LoCoMo 有 6.4% 答案键错误审计；Memora 错误归因每任务仅 25 样本。
- **适用域**：抽取式 KG 劣于平坦 RAG 仅测于抽取管线；剪枝安全性测于弱基线系统；原生文本优势
  在年级跨度收窄。
- **时效**：核心证据集中于 2026-05 至 2026-08 预印本，多未经同行评审或独立复现，半年窗口内
  可能被修正。
- **未评方向**：多模态（**2026-09-24 归档时更新：已解除 parked**——glm-5.3-flash 视觉
  SKU 开放，slice 实验落地：文本可恢复组截图注入 +15pp、需视觉组定论为检索瓶颈，
  见 `docs/evidence/lme2-v2b-web-mm-slice/`）；Mem-α 训练成本与
  已关闭的 GRPO 决策同级。
- **同步对齐成本**：provenance/信任级入 schema 后与 17 方法同步契约的对齐成本未在文献中量化。

## 开放问题

1. 前瞻索引在写入时生成的"未来场景蕴含"查询，如何在过滤幻觉（Doc2Query-- 教训）的同时，不让
   这条生成管道本身成为新的投毒写入通道？
2. RL 记忆管理所需奖励（下游 QA 准确率）在生产环境难以在线获取：离线轨迹 + 合规约束训练，
   还是必须维护影子环境做策略评估？
3. 来源权威（provenance/trust tier）应否成为记忆 schema 一等公民并纳入 Merkle 反熵同步——
   否则权威标签在对等节点间漂移；与 17 方法同步契约如何对齐？
4. 原生文本保真与存储/上下文成本的平衡点在哪：RAG 优势在年级跨度收窄，摘要/抽象层何时开始
   净收益为正？

## 来源清单（15 个抓取来源 + 验证期佐证）

| 来源 | 质量 | 角度 | 主张数 |
| --- | --- | --- | ---:|
| [arXiv:2605.06527](https://arxiv.org/abs/2605.06527) STALE | primary | landscape-delta | 5 |
| [arXiv:2604.20006](https://arxiv.org/abs/2604.20006) Memora | primary | landscape/eval | 5 |
| [anthropic.com contextual-retrieval](https://www.anthropic.com/news/contextual-retrieval) | primary(corp) | retrieval-untried | 5 |
| [arXiv:2401.18059](https://arxiv.org/abs/2401.18059) RAPTOR | primary | retrieval-untried | 5 |
| [arXiv:2603.17244](https://arxiv.org/abs/2603.17244) Kumiho | primary | memory-lifecycle | 5 |
| [arXiv:2608.28978](https://arxiv.org/abs/2608.28978) Selective Forgetting | primary | memory-lifecycle | 5 |
| [arXiv:2509.25911](https://arxiv.org/abs/2509.25911) Mem-α | primary | eval | 5 |
| [arXiv:2606.04329](https://arxiv.org/abs/2606.04329) MPBench | primary | eval/contrarian | 5 |
| [arXiv:2605.27825](https://arxiv.org/abs/2605.27825) MRMMIA | primary | contrarian-security | 4 |
| [Unit 42](https://unit42.paloaltonetworks.com/indirect-prompt-injection-poisons-ai-longterm-memory/) | primary(industry) | contrarian-security | 5 |
| [arXiv:2607.29167](https://arxiv.org/abs/2607.29167) PPMF | primary | team-interop | 4 |
| [Mem0 Beyond AGENTS.md](https://mem0.ai/blog/beyond-agents.md-shared-memory-for-coding-agents-across-services-and-repos) | blog(vendor) | team-interop | 5 |
| [arXiv:2507.22925](https://arxiv.org/abs/2507.22925) H-MEM | primary | retrieval-untried | 5 |
| [arXiv:2604.11182](https://arxiv.org/abs/2604.11182) LifeDialBench | primary | eval-benchmark | 5 |
| [arXiv:2601.05504](https://arxiv.org/abs/2601.05504) MINJA 后续（正典 2503.03704） | primary | contrarian-security | 5 |

验证期补充佐证：arXiv:2608.01619（STALE 跟进）、arXiv:2504.19754（ECIR 复现）、
arXiv:2606.24535（fleet-memory 失败模式）、arXiv:2503.03704（MINJA 正典）、MemPoison /
MemSecBench、Louck 2026（不可伪造来源绑定权威）、TierMem（ICLR 2026）、docTTTTTquery、
Letta sleep-time、LongMemEval / MemoryAgentBench 分析。

配额淘汰记录（12 项）：MAGMA（泛 URL）、mem0.ai 基准页（厂商自报）、Supermemory 转向、
particula 横评、MemOS GitHub、PLAID、HyDE 综述、2505.14816（与 2509.25911 疑似同事实，后证实
为不同论文）、A2A 官宣（2025 旧闻）、互操作协议综述、Hindsight、治理论文——均为 medium/low
相关度或与已知事实重复。

## 统计行

`6 angles / 15 sourcesFetched / 73 claimsExtracted / 21 canonicalClaims / 20 confirmed · 1 killed · 0 unverified / 68 agentCalls`

流程：六角度并行检索 → 去重配额（每角度保底 2 + 全局补齐至 15）→ 逐源提取（忠实重述纪律 +
inferred 标记）→ 语义合并为 21 条规范主张 → 每条 2 名独立核查员（A 忠实性/来源强度、B 反证/
时效）+ 分歧决胜（4 次）→ 综合。全部核查员结论以 JSON 回传并逐字段校验。
