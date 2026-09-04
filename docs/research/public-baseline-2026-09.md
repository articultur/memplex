# 公开数据集基线（clean-SHA）与容量发现 — 2026-09-04

## 状态与口径

本文记录 2026-09-04 在**干净提交 SHA**上产出的首批公开数据集 G003 证据
bundle（E1 aggregate、本地保留、未签名、未发布），以及过程中发现的两项
工程事实。bundle 位于 `/tmp`（本地审计证据，非仓库制品；上游数据集的
再分发许可未核实，故不入库——manifest 内嵌了实际数据的 digest 与
sample_ids_digest 可供独立复核）。

## Bundle 清单

| Bundle | SHA | dirty | 数据 | n | 关键指标（默认词汇栈） |
| --- | --- | --- | --- | ---: | --- |
| `/tmp/g003-public-popqa-final` | `ef917ea` | false | akariasai/PopQA（HF） | 100 | mrr 0.9917；recall@1 0.99；recall@10 1.0；生成 f1 0.2008 |
| `/tmp/g003-public-longmemeval-5` | `ef917ea` | false | xiaowu0162/longmemeval-cleaned（官方 JSON 本地放置） | 5 | token_f1 0.0449；substring_hit 0.2 |
| `/tmp/g003-public-hotpotqa-n100` | 二跳提交前 | false | hotpotqa/hotpot_qa fullwiki validation（HF parquet） | 100 | mrr 0.0751；multihop_accuracy 0.0；hop_coverage 0.0 |
| `/tmp/g003-public-triviaqa-clean` | 二跳提交 | false | mandarjoshi/trivia_qa rc.nocontext validation | 100 | 全零——解析器与 parquet schema 不兼容（适配器 TODO，非检索结论） |

两者均通过 strict verifier（`evidence_level=E1`）。longmemeval 为
seed=17 分层子集（5 类 question_type 各 1）；全量 500 条因下述容量问题
本轮不可行。

**诚实结论**：longmemeval 真实全 haystack 场景下默认词汇栈的 token_f1
（0.045）远低于历史合成口径（0.333）——这是预料中的词汇-语义差距在真实
数据上的直接体现，与 paraphrase 低重叠层（recall@1 0.027）同源。
语义栈（`MEMPLEX_EMBEDDING_MODEL=minilm`，已验证低重叠 recall@1 0.784）
的对照 bundle 是下一步；签名与不可变发布仍在等待仓库级凭证。

## 发现 1：G003 公开模式的静默 synthetic 落回（已修复）

`mteb/popqa` 已从 HuggingFace 下架；loader 的 HF 抓取失败后静默落回
synthetic 生成器，而 runner 会把结果标注为 `public_huggingface`——
**合成数据差点披上公开数据的标签**。已修复（commit 于
`fix: public-run HF fetch is fail-closed`）：runner 直接调用
`_fetch_from_huggingface`，抓取失败即报错；popqa 映射刷新为
`akariasai/PopQA` 并做字段重映射。这正是"公开基线"最容易出错的地方：
不是跑不动，而是悄悄跑了假数据。

## 发现 2：lite 播种路径在多千文档规模超线性退化（未修复，已记录）

longmemeval 每个样本向服务播种完整 haystack（数百条会话）。实测 CPU
时间：3 样本 ≈ 1 分钟，10 样本 > 21 分钟（未完成即终止），50 样本
> 14 小时（终止）——差于二次增长。38e8f8a 的 commit fast path 优化了
提交路径本身，但该规模下每写一文档的成本随存量语料增长（嫌疑：
`_validate_resident_graph` 全图校验、FTS 增量索引或 resident 索引重建
随 N 扫描）。**这是 `latency_capacity` 维度的首个实测证据**，修复属于
后续性能战役（建议先 profile `_commit_current_state` 与
`SQLiteFTSIndex._ensure_index` 在 5k/10k 文档下的分布）。

## 发现 3：重排权重校准——词汇栈上 6 维集成已近天花板（工具已落地）

`scripts/calibrate_reranker.py`：在线录制每查询每候选的 6 维分数
（一次 paraphrase 运行），离线坐标下降搜索权重（秒级）。词汇栈结果：
baseline 整体 recall@1 0.582 → 最优 0.592（+1pp，medium 层 +2.7pp，
high/low 层无变化），**低于预设的 ≥+2pp 应用门槛，故不改变默认权重**。
结论：瓶颈在语义维（TF-IDF 词汇余弦）本身，权重无法弥补；该工具的
正确用法是语义栈落地后的重新校准（彼时 semantic_similarity 维携带
真实信号，权重分配才有信息量）。

## 未接线项（更新）

- **nq / triviaqa 解析器适配**：parquet 原生 release 可下载，但
  `natural_questions` 的 annotations 结构与 `trivia_qa` 的 answer 结构
  与现有解析器不兼容（triviaqa 实测全零），需按新 schema 适配
  `_parse_natural_questions` / `_extract_answer_aliases` 后重跑。
- **BEAM**：仓库无对应 runner/loader 模块，属新功能开发。
- **locomo 官方数据**：需从 snap-research 仓库手工获取。
- **Actions**：已于 2026-09-04 启用（enabled=true, allowed=all），
  workflow 均注册为 active；但 run 在 startup_failure（0 jobs），
  症状指向**账户级 billing 阻断**（需仓库所有者在 GitHub 设置中
  处理 spending limit/unpaid balance，一键解封后 dispatch 即可）。
  PR #24 已开，解封后自动获得全矩阵 check runs。


- **BEAM**：仓库无对应 runner/loader 模块，属新功能开发（需按其官方
  协议接入），本轮如实记为 not-integrated。
- **locomo 官方数据**：HF 无官方镜像，需从 snap-research/locomo 仓库
  手工获取后放置 data-dir（管线已支持）。

## 复现命令

```bash
.venv/bin/python scripts/run_g003_benchmark.py run \
    --data-dir <dir-with-popqa.json> --dataset popqa \
    --num-samples 100 --top-k 10 --seed 17 --run-dir <fresh-dir>
.venv/bin/python scripts/run_g003_benchmark.py verify --run-dir <fresh-dir>
```
