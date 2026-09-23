# ADR-012: Lite SQLite v2 — authoritative store, single-writer queue

## Status

Proposed（设计定稿待用户确认后开工）

## Context（全部为实测事实）

当前 lite 后端的持久化是"内存驻留语料 + 全量 JSON pair 重写"：

1. **序列化墙**：每次提交把整个语料序列化为 `memory.json` + `changelog.json`
   并 fsync（`_commit_current_state` → `LitePair` 全量重写）。100k 文档级实测
   57 docs/s 饱和，profile 归因于每次提交 O(corpus) 的序列化+写盘——这是
   P1 战役确认的剩余超线性根因。
2. **死锁类**：v13.1 分片执行暴露 worker↔主线程锁竞争死锁 3 次（重启自愈、
   复现证据已存）。根因是两条线程都在同一 flock/durability 路径上竞争。
3. **已在的资产**：FTS5 sidecar（BM25+trigram 双表）、内存向量索引、
   journal + flock + digest 审计链、`deferred_commit` 批量、双时态
   supersede、principal/ACL 过滤——语义层完好，问题全在物理持久化层。

## Decision

### 1. 物理布局：SQLite 成权威存储，JSON 降级为导出格式

```text
memories          权威 canonical 行（id, kind, tenant, principal, workspace,
                  visibility, name, domain, payload_json, content_hash,
                  created_at, updated_at, valid_from, invalid_at）
memory_sources    provenance 边（memory_id, source_uri, actor, captured_at）
memory_versions   supersession 链（memory_id, superseded_by, as_of）
derivations       派生记录元数据（kind: fact|summary|hub, source_refs,
                  derivation_version, extractor_model）
change_log        单调逻辑序（seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_json）——sync/审计的权威事件流
fts_memory        FTS5 content 表（现 sidecar 收编为库内表，重建语义不变）
embedding_cache   (memory_id, model, dim, vector BLOB)——派生可重建
graph_edges       有界图边（带 per-edge confidence/age，重建语义不变）
meta              (format_version, generation, last_seq, digest_head)
```

**JSON pair 的职责收缩为**：export/backup/interchange/wire format。导入器
保留一个 release 周期做 dual-read（`storage.lite.format = json|sqlite`
feature flag；检测到旧布局自动迁移并保留 `.json` 原件）。

### 2. 并发模型：单写者队列 + WAL 多读

- **写路径**：所有 DB mutation 进入 single-writer queue（一个专职写者
  线程 + 独立写连接）。embedding/抽取/图计算在队列外并行，只把最终
  row mutation 提交进队列。**这从构造上消灭 worker↔主线程死锁类**：
  两条线程不再竞争同一 durability 路径，而是先后进队。
- **读路径**：WAL 模式（链接版本 3.53.1 > 3.51.3 的 WAL-reset 修复线，
  已核），任意数量只读连接，读不阻塞写、写不阻塞读。
- **批量语义保留**：`deferred_commit` 映射为队列里的单事务批提交，
  语义不变（成功前缀保留、异常回滚整个 scope）。

### 3. 持久化与审计契约（不降级）

- 提交 = 单事务 `BEGIN IMMEDIATE ... COMMIT` + `PRAGMA synchronous=FULL`
  （写吞吐瓶颈从 O(corpus) 序列化变为 O(batch) 行插入；FULL fsync 每事务
  一次，批量摊薄）。
- 现行审计链语义保留但变便宜：per-commit digest → `meta.digest_head`
  （滚动哈希链，仍是逐事件链式）；周期全量 decode 审计 → 抽样行级
  payload 反序列化校验（`content_hash` 不匹配即 fail-closed，等价于
  现行 `_FULL_DECODE_AUDIT_INTERVAL` 的回归检测目标）。
- 崩溃恢复：WAL 回放（SQLite 自带）+ `meta.last_seq` 与 `change_log`
  最大 seq 一致性断言，不一致即 fail-closed 拒开。

### 4. 迁移路径（三步，每步可回退）

```text
Phase A  影子写：读 JSON、双写 JSON+SQLite，比对工具逐行 diff
         （零行为变化，回退 = 关 flag）
Phase B  权威切换：读 SQLite、写 SQLite，JSON 退为每 N 代快照导出
         （回退 = format flag 回 json，导入器反向恢复）
Phase C  清理：默认 sqlite，json importer 保留一个 release
```

### 5. 测试计划与验收门槛

| 测试 | 方法 | 门槛 |
|---|---|---|
| 写吞吐 | 现行 `bench_audit_stall.py` 扩展（100k 文档播种） | **≥85 docs/s**（57 的 1.5×；挑战 ≥114） |
| 死锁 | 双线程压力（主线程批量写 + worker 轮询 + 并发读），跑 30 分钟 | **超时阻塞 = 0**（v13.1 的 3 次复现场景必须归零） |
| 崩溃注入 | 每阶段 kill -9：队列中/事务中/WAL checkpoint 中/迁移中 | **已提交记录丢失 = 0**；重开一致性断言全过 |
| 迁移等价 | 10k 语料 JSON→SQLite 全量 diff（行集+payload hash） | **100% 等价** |
| 回归 | 全量 lite 套件（3400+）+ PG 套件（730，共享 service 语义） | 零回归 |
| 审计 | content_hash 篡改注入 | fail-closed 检出 = 100% |

### 6. 明确不做

- 不引入第二数据库/服务（单文件 SQLite，保持 lite 零依赖部署）
- 不改 service/ACL/retrieval 语义层（物理层换血，逻辑面零变化）
- 不做跨机 SQLite 文件复制（peer-mesh 同步 change_log 事件流，不是文件）

## 预期收益

- 写吞吐 1.5-3×（序列化墙消除：提交成本从 O(corpus) 降为 O(batch)）
- 死锁类归零（构造性消灭，不是缓解）
- 崩溃恢复从"journal 重放到最新 pair"变为标准 WAL 回放（更快、更可测）
- 后续 peer-mesh 直接消费 `change_log` 的单调事件流（与 Merkle 反熵
  协议天然对接：树叶 = 事件哈希）

## 工程量估算

Phase A（影子写+diff 工具）：3 人日；Phase B（权威切换+审计链迁移）：4 人日；
测试计划全项：3 人日。合计 ~10 人日，分两个可停点（A 结束、B 结束）。
