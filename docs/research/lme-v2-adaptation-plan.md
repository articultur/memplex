# LongMemEval-V2 适配方案（⑨，2026-09-23 勘察定稿）

## 基准事实（官方仓库实测勘察）

- **接口**：`memory_modules/memory.py` 定义 `Memory` ABC——两个抽象方法
  `insert(trajectory)` / `query(query, query_image) -> list[MemoryContextItem]`
  （text/image 两型条目）。适配 Memplex = 写一个 `MemplexMemory(Memory)`
  适配器：insert 时把轨迹文本喂 `svc.write_text`（可叠加实体枢纽派生），
  query 时调 `svc.query(orchestrated=True)` 并把 top-k 摘要包装成
  `MemoryContextItem`。
- **数据**：HuggingFace `xiaowu0162/longmemeval-v2`（451 题、5 能力、
  500 轨迹/haystack、最大 115M token；web + enterprise 两域、
  small/medium 两档）。
- **评估**：`run_eval.py` 消费任意 Memory 子类；指标含准确率（多选/短语
  集/boxed 提取）**和查询延迟**；排行榜按 accuracy-latency frontier
  （LAFS 相对参考前沿的增益）。
- **参考前沿（官方硬编码）**：RAG slice+notes 51.0%@0.2s /
  AgentRunbook-R 58.6%@26.9s / Codex 69.9%@177.2s /
  AgentRunbook-C 74.9%@108.3s。

## Memplex 适配的先天优势与风险

- **优势**：延迟——我们的检索是本地 bge-m3 + 并行多路（毫秒级），
  参考前沿的 0.2s-177s 全区间内我们天然贴近左下角；准确性——
  v13.2 的检索配方（枢纽+PAR+分解）正是"从长历史找证据"的任务。
- **风险**：V2 的 haystack 是**多模态 web 轨迹**（截图/DOM），纯文本
  派生可能丢状态信息；trajectory 切片语义（raw_state_slice）与我们的
  会话粒度不同；115M token 的播种吞吐受 P1 战役确认的写路径限制
  （SQLite v2 之后的正解）。

## 分阶段适配（探针先行纪律不变）

| 阶段 | 内容 | 门槛 |
|---|---|---|
| V2-a | small 档 web 域 + 文本轨迹子集 + `MemplexMemory` 适配器（纯文本 insert/query） | 20 题冒烟跑通 harness；准确率 > no_retrieval 基线 |
| V2-b | small 全量（~200 题）+ orchestrated 检索 + 延迟记录 | 报出第一个 frontier 点 |
| V2-c | 全域能力对照 + LAFS 计算 | 与参考前沿四点同图 |

## 依赖

- 数据下载（HF 直连/镜像，~GB 级）与磁盘空间
- 写路径吞吐（V2-b 起 SQLite v2 增益直接兑现——两战役在此汇合）
- glm-5.3 作答端复用现有代理（V2 的 reader 模型可配自定义 base_url）
