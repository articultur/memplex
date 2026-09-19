# Deep Research：多源记忆系统提升方案与 Memplex 的 SOTA 路线（2026-09-18）

**问题**：目前多源记忆系统（LongMemEval 基准）的提升方案有哪些？Memplex（官方协议 J=0.886，glm-5.3 judge）如何达到 SOTA 水平（95+ 甚至 99）？95-99 分在方法学上可信吗？

## 执行摘要

没有一个 95+ 的 LongMemEval 分数在官方口径（gpt-4o 生成 + 官方判分提示词）下站得住。公开可复现、使用官方基准模型的最高分是 Mastra Observational Memory 的 **84.23%**（作者自己标注）；95+ 的成绩全部依赖非官方生成器（GPT-4.1/GPT-5-mini）、按题型调优的作答提示词、或与自家仓库工件不符的自报口径。判分提示词的宽松度可单向抬高分数数个百分点（临时估计 4-10pp）。对 Memplex 而言：0.886（glm judge）已处可信梯队的第一档；距离"95"的诚实差距主要在生成器（同架构换 gpt-5-mini 级模型实测 +10.6pp）与检索召回（官方 user-fact key 扩增 v2 数字 +9.4% recall）；"99" 在真值错误率设限的基准上不存在诚实路径，它只可能是评测缺陷的信号。

## 通俗结论（与聊天层一致）

1. **95+ 没有一个站得住**（官方口径可复现上限 ≈ 84-86）。
2. **Memplex 0.886 已在可信第一梯队**（换 judge 的口径差异是主要不可比因素）。
3. **提升方案有据可查的七条路线**（见下）。
4. **到 95 的诚实路径 = 生成器升级 + 检索召回补齐；99 不存在诚实路径**。

## 发现（经对抗验证）

### F1. 95+ 成绩的方法学解剖 —— 全部非官方口径（置信度：高）
- **OMEGA 95.4**：生成器 GPT-4.1（非官方 gpt-4o）+ **按题型调优的作答提示词**（`_CATEGORY_PROMPT`，"Best prompt per category determined empirically across 4 benchmark runs"，厂商自披露）；评测代码公开（`scripts/longmemeval_official.py`，1850 行）但**无第三方独立复现**；其仓库内 2026-02 基准报告记录的最佳成绩是 **76.8%**，与营销页 95.4% 存在差距；厂商自述"Different methodologies, not directly comparable"。［omegamax.co；github.com/omega-memory/omega-memory；验证过程修正了"仓库无评测代码"的错误初稿］
- **Mem0 94.4/94.8**：README 头条与仓库检入工件**不一致**——工件元数据显示 answerer/judge 均为 gpt-5、top-200 实测 93.4%（467/500）、top-50 90.4%、multi-session 86.5%；第三方（Memseek/MemBukkit）标注 self-reported；OpenReview 论文给出 94.4 的置信区间 [88.9, 98.4] 并质疑高分与真实记忆能力的相关性。最初流传的"judge=gpt-4.1"为捏造引文（验证中抓出）。［github.com/mem0ai/memory-benchmarks；docs.mem0.ai］
- **Mastra OM 94.87**：作者自己写明——**"OM with gpt-4o (84.23%) is the highest openly reproducible score that uses the official benchmark model"**；94.87 依赖 gpt-5-mini，93.27 依赖 gemini-3-pro。Emergence 的 86% 被 Mastra 标注为不可公开复现的 Internal 配置。［mastra.ai/research/observational-memory，逐字核实］
- **判分宽松度**（重新限定后成立）：同一批答案在不同判分提示词下的跨度可达数十个百分点（构造性压力测试，91.0% vs 35.0%）；分歧方向**单向**（宽松方给分、严格方拒分，863 处无一例外）；业界惯用提示词比未修改官方提示词（gpt-4o-2024-08-06）**高约 4-10 个百分点**（临时估计，未经独立复现；原始研究对象是 LoCoMo 答案集）。［github.com/mnemoverse/mnemoverse-benchmarks-paper "The Judge Is the Benchmark"］
- **Benchmark 乱象实例**（均获独立佐证）：MemPalace 头条 96.6% 实为 R@5 检索召回被当作 QA 分宣传（Vectorize/Gamgee 独立确认）；LoCoMo 官方裁判接受 **62.81%** 故意答错但主题相关的答案（Penfield Labs 压力测试，独立研究者复算确认）；存在"为三个具体问题打三个补丁后重测发分"的 teaching-to-the-test 实例（项目自家 BENCHMARKS.md 自称）；LoCoMo 约 99/1540 题真值错误或不可答（≈6.4%，隐含 ~93.6% 诚实上限）；EverMemOS 声称 92.32% 第三方仅复现 38.38%（争议未决）。［essays.bloo-mind.ai/posts/2026-05-20-mem-eval + 独立佐证］

### F2. 可信坐标系：官方口径下 ~84-86 是天花板，Memplex 0.886 在第一梯队（置信度：高）
- 统一 gpt-4o-mini 生成器的学术协议下（MemGAS，ICLR 2026）：MemGAS 60.20 / HippoRAG 2 57.60 / SeCom 56.00 / A-Mem 55.60 / Full History 50.60——先进记忆方法净差距仅 2.6-4.6 分（协议特定结论；独立重跑显示基线漂移可达该差距的量级）。MemGAS 检索端 Recall@10 = 94.47（LongMemEval-s）。［arXiv 2505.19549，注意 2509.23555 是无关论文］
- Mastra OM gpt-4o 84.23 > oracle 82.4 > full-context 60.2；我们的 0.886（glm-5.3 生成+判分）在"可信梯队第一档"，与 84.23 的主要不可比因素是 judge/生成器模型本身。
- **隐含结论**：在官方模型口径下，记忆架构之间的净差距是个位数分；两位数差距几乎全部来自生成器与判分配置。

### F3. 已验证的提升方案清单（按证据强度）
1. **生成器升级**（最大单一杠杆）：同架构仅换 Actor 模型 gpt-4o→gpt-5-mini = **+10.64pp**（84.23→94.87，作者控制变量确认）。［Mastra］
2. **user-fact key 扩增**（官方 v2 定稿数字）：+9.4% recall@k / +5.4% 最终准确率；keyphrase 单独作 key **有害**（Recall@5 0.582→0.282）；压缩形式只在并入原文时有效。［arXiv 2410.10813 v2，验证修正了 v1 的 +4% 旧数］
3. **时间感知索引/查询扩展**：temporal 子集 recall +11.3%（round 粒度）/+6.8%（session 粒度）；弱模型抽时间范围会幻觉出假阳性时间窗（Llama-8B 实测警告）。［同上 v2；仓库工具 temp_query_search_pruning.py 可复现］
4. **读侧动态演化**（CoM，ACL 2026）：把已检索片段组织成推理链 + 自适应截断，比强基线 +7.5-10.4%，token 开销仅为复杂架构的 2.7%、延迟 6.0%；动机是"朴素拼接无法把检索召回转化为推理准确率"。［arXiv 2601.14287，数字自报无复现］
5. **多粒度组织 + 路由**（MemGAS，ICLR 2026）：LLM 生成 session/turn/keyword/summary 四层元数据 + GMM 分集 + 熵路由选粒度。［arXiv 2505.19549］
6. **无检索压缩层级**（Mastra OM）：Observer/Reflector 双后台 agent 维护三层结构（消息→观察→反思），~30k token 稳定可缓存前缀，6 倍压缩。［mastra.ai，含全部逐字数字］
7. **Mem0 平台配方**：ADD-only 抽取（保留时序上下文，代价是 knowledge-update 最弱）+ 四信号融合（semantic+BM25+实体+时序，加性融合、时序只调序不过滤）+ top-200 大预算单遍检索；multi-session 88.0 为最弱题型（与我们的发现一致）。［mem0ai/memory-benchmarks + docs，配置核实］

### F4. Zep/Mem0 争议史（置信度：高，作为方法学教训）
Zep 在 Mem0 的 LoCoMo 评测 harness 中指出三个实施错误（图角色错配、时间戳拼接、顺序搜索夸大延迟）；Zep 自身把 84% 修正为 75.14%±0.17（10 次重跑）；Mem0 反诉 Zep 实为 58.44%（争议无第三方裁决、issue 因不活跃关闭）；无记忆 full-context 基线 ~73% 高于 Mem0 论文最佳 ~68%（Zep 以此论证 LoCoMo 对现代上下文窗口太短）。［blog.getzep.com/lies-damn-lies...；github.com/getzep/zep-papers issue #5，双向均核实］
Zep 论文自报 DMR 94.8% vs MemGPT 93.4%、LongMemEval 最高 +18.5%/延迟 -90%——可作 attributed self-report 引用，但其评估流程可信度受过上述公开质疑。［arXiv 2501.13956］

### F5. 对 Memplex 的路线映射（0.886 → 诚实 SOTA）
已在做且有内部证据背书：会话级检索单元（≈官方 expansion 的确定性变体）、邻接扩展、查询分解、日期前缀+参照日期（time-aware 的弱化实现）、分题型指令（≈OMEGA 的 category-tuned，我们已披露）、SC 投票。
尚未做、证据支持值得做的：
- **a. 生成器升级**是通往 90+ 的最大单一杠杆（Mastra 的 +10.6pp 实测）——但需如实声明"这是生成器的能力，记忆系统本身的贡献要单独消融"；
- **b. user-fact key 扩增**（官方 v2 +9.4% recall）——我们唯一没试过的官方检索增强，与 B 类"证据未检回"失败直接对症；
- **c. 读侧 chain 组织**（CoM 路线）——把 top-24 上下文重组织为推理链再作答，针对 A 类生成失败；
- **d. 报分口径自律**：参照 Mastra 双口径（官方模型可复现分 + 最强模型分分开报）；我们已有 glm judge 披露，建议表内加"官方口径锚点 84.23"一行。
- "99" 不设为目标：LoCoMo 真值错误率已证明此类基准存在 <100% 的诚实上限；LongMemEval cleaned 版正是官方对真值问题的修复动作。

## 被否决的断言（透明度记录）
1. "OMEGA 仓库不含 LongMemEval 评测代码" —— 被仓库树直接证伪（1850 行官方 harness 一直在）。
2. "Mem0 94.4 的 judge 为 gpt-4.1" —— 捏造引文；实际工件为 gpt-5，README 头条与工件不符。
3. "官方 key 扩增增益 +4%/+5%" —— v1 旧数；v2 定稿为 +9.4%/+5.4%。
4. "判分宽松度使 LongMemEval 虚高 5-10pp（mnemoverse 量化）" —— 原研究重判的是 LoCoMo 答案集且自带幅度免责声明；重新限定为"单向、数个百分点、临时估计"后成立。

## 未验证/开放问题
1. OMEGA 仓库内 76.8% 与营销页 95.4% 之间的差距细节（未逐 commit 追溯评测配置变化）。
2. Mem0 平台版（非 OSS）94.4 的完整运行工件是否存在于仓库之外。
3. glm-5.3 与 gpt-4o 作为 judge 的系统性偏差方向与幅度（未做双 judge 对照）。
4. LongMemEval-V2（2026-05，web agent 状态追踪）上各方案的表现尚未有可比数据。

## 来源清单
| 来源 | 质量 | 角度 | 引用数 |
|---|---|---|---|
| mastra.ai/research/observational-memory | 一手（厂商研究） | 榜首系统/生成端 | 5 |
| github.com/mem0ai/memory-benchmarks + docs.mem0.ai | 一手（厂商 harness） | Mem0 管线 | 10 |
| arxiv.org/abs/2410.10813 (v1+v2) | 一手（论文） | 官方配方 | 5 |
| github.com/xiaowu0162/LongMemEval | 一手（官方仓库） | 官方配方 | 5 |
| omegamax.co + github.com/omega-memory/omega-memory | 营销页+一手源码 | 榜首系统 | 10 |
| blog.getzep.com/lies-damn-lies... | 博客（竞争方） | 方法学质疑 | 5 |
| essays.bloo-mind.ai/posts/2026-05-20-mem-eval | 博客（独立分析） | 方法学质疑 | 5 |
| github.com/mnemoverse/mnemoverse-benchmarks-paper | 一手（论文仓库） | 方法学质疑 | 5 |
| arxiv.org/abs/2601.14287 (CoM) | 一手（论文） | 榜首系统 | 5 |
| arxiv.org/abs/2505.19549 (MemGAS) | 一手（论文） | 生成端 | 5 |
| arxiv.org/abs/2501.13956 (Zep) | 一手（论文） | 2026 新技术 | 5 |
| arxiv.org/abs/2602.05665 (图记忆综述) | 一手（综述） | 2026 新技术 | 5 |

## 统计行
`angles 6 / sourcesFetched 16 / claimsExtracted 60 / canonicalClaims 11 / confirmed 7 · refuted-with-correction 4 / agentCalls 47`
