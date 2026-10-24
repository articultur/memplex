# Curation UI — 现状（2026-10-24 核对）

人工治理控制台已按原设计的方案 A（web console）落地：HTTP 服务内建的
`/admin` 静态页 + 同源 JSON API，鉴权走与其余接口相同的 principal 体系。

## 已实现（代码核对）

| 功能 | 入口 | 说明 |
| --- | --- | --- |
| 治理台页面 | `GET /admin`（`memplex/operations_assets/admin.html`） | 无构建步骤的原生 JS；模板插值已全量 escape（2026-08-28 XSS 修复） |
| 记忆列表/筛选 | `GET /admin/api/memories?q=&tier=&limit=` | 按 tier（含"none"）/名称或 id 过滤，授权过滤后返回 |
| 晋升知识层 | `POST /admin/api/promote` | `service.promote`（owner-only 合同不变） |
| Fact 点时视图 | `GET /admin/api/facts?as_of=&limit=` | 双时态 `as_of` 参数已暴露 |
| 跨 agent 分享（API） | `POST /admin/api/share` | `service.share_with`；**页面尚未接线** |

依据：`memplex/adapters/http_api.py::_register_admin_routes`。

## 剩余差距（原目标 vs 现状）

1. **grant 撤销**：原目标 3 的 revoke 无 API 无 UI（当前 share 只增不减）。
2. **分享 UI**：API 已有，`admin.html` 未调用（页面 API 面仅
   `api/memories|promote|facts`）。
3. **逐条历史时间线**：`GET /memories/{id}/timeline` 端点已存在，未接入
   治理台详情视图。
4. **Preference/Observation 治理面**：列表主面是 Function，facts 有独立
   视图；Preference/Observation 无人工治理入口。

## 建议次序（小步）

1. 页面接线 share + timeline（两个端点均已存在，纯前端工作）；
2. revoke API（`service.revoke_share` 或等价物 + 权限测试）后再接 UI；
3. 视使用需要再扩 Preference/Observation 面。

> 历史设计讨论（选项 A/B/C 对比与"web console 优先"的裁决）见本文件
> 2026-10 之前的版本（git 历史）；本版改为现状核对口径。
