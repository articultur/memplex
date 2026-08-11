---
name: mem-turn
description: 在 Codex 回合开始/结束时使用 Memplex 共享记忆。适用于需要召回历史决策、跨 Claude Code/OpenClaw/Hermes 复用上下文，或显式保存本轮结论的任务。
---

# Memplex 回合联动

Codex 原生 Hook 已自动执行回合级召回与捕获。只有在 Hook 不可用、用户要求显式验证，或需要指定可见性时，才直接调用 MCP 工具。

## 回合开始

调用 `memory_turn_begin`，只显式传入当前用户提示 `prompt`。身份不属于模型参数：

- 受管宿主 launcher 通过进程环境绑定 `agent/user/session/project`；
- 未受管 MCP 只使用 OS 用户、当前进程会话与工作目录；
- MCP tool arguments 不能覆盖这些身份字段。

同一 `user_id + canonical project_path` 的 workspace 记忆可由 Claude Code、OpenClaw、Hermes 与 Codex 共同召回；session 私有记忆不会跨会话扩散。

## 回合结束

调用 `memory_turn_end`，传入本轮 `user_message` 与 `assistant_message`；不要在模型参数中传身份字段。`agent` 与 `session_id` 仅用于来源追踪和 session 私有边界，不是共享工作区身份。

## 安全约束

- 不使用 `default` 作为生产用户身份。
- 不把 `<private>...</private>` 内容写入记忆。
- 当用户或工作区身份不明确时，不扩大读取范围。
- Memplex visibility metadata 不是独立 ACL；宿主仍需负责可信身份与授权。
