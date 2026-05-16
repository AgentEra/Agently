---
title: 能力地图
description: 判断当前问题属于 Agently 的哪一层，并跳到对应章节。
keywords: Agently, 能力地图, 学习路径, request, TriggerFlow
---

# 能力地图

> 语言：[English](../../en/reference/capability-map.md) · **中文**

这是导航工具：先判断问题属于哪一层，再去对应章节。

## 七层模型

| 层 | 它回答的问题 | 去哪读 |
|---|---|---|
| 1. 单次请求 | 我能不能从模型拿到一个结构化答案？ | [快速开始](../start/quickstart.md)、[Requests 概览](../requests/overview.md) |
| 2. 稳定输出 | 我每次都能拿到期望的字段吗？ | [Schema as Prompt](../requests/schema-as-prompt.md)、[输出控制](../requests/output-control.md) |
| 3. 响应与记忆 | 我能复用一次响应，或延续一个受控窗口的对话吗？ | [模型响应](../requests/model-response.md)、[会话记忆](../requests/session-memory.md) |
| 4. Action 与执行环境 | 模型是否需要调用函数、MCP server 或带托管执行依赖的沙箱命令？ | [Actions 概览](../actions/overview.md)、[Action Runtime](../actions/action-runtime.md)、[Execution Environment](../actions/execution-environment.md) |
| 5. 知识与服务 | 是否需要检索、HTTP、SSE 或 WebSocket 暴露？ | [知识库](../knowledge/knowledge-base.md)、[FastAPI 服务封装](../services/fastapi.md) |
| 6. 观测与开发 | 是否需要 observation event、DevTools 或 coding-agent 指引？ | [观测概览](../observability/overview.md)、[Coding Agents](../development/coding-agents.md) |
| 7. 编排 | 分支、并发、暂停恢复、持久化 | [TriggerFlow 概览](../triggerflow/overview.md) |

每一层都依赖前面的层。跳层是出问题最常见的原因——比如，单次请求没稳定就跳进 TriggerFlow。

## 路径选择

| 你的处境 | 去哪 |
|---|---|
| 完全新手 | [快速开始](../start/quickstart.md) |
| 输出不稳定 / 偶尔缺字段 | [Schema as Prompt](../requests/schema-as-prompt.md) → [输出控制](../requests/output-control.md) |
| 想要字段级流式 UX | [Async First](../start/async-first.md) → [输出控制](../requests/output-control.md) |
| 一次响应想多种方式复用 | [模型响应](../requests/model-response.md) |
| 多轮对话且要控制窗口 | [会话记忆](../requests/session-memory.md) |
| 模型要调工具 / MCP | [Action Runtime](../actions/action-runtime.md) |
| 需要常见 Python / shell / workspace / Node.js / SQLite 能力 | [Action Runtime](../actions/action-runtime.md)，优先从 `agent.enable_python(...)`、`agent.enable_shell(...)`、`agent.enable_workspace(...)`、`agent.enable_nodejs(...)` 或 `agent.enable_sqlite(...)` 开始 |
| 需要 web search 或页面 browse | [Action Runtime](../actions/action-runtime.md)，使用 `from agently.builtins.actions import Search, Browse` 和 `agent.use_actions(...)` |
| 执行前需要托管 MCP/sandbox/process/browser/SQLite 生命周期 | [Execution Environment](../actions/execution-environment.md)，通常面向 action/plugin 开发者 |
| 判断新扩展应该放在哪一层 | [扩展边界](../architecture/extension-boundaries.md) |
| 把 agent 包成服务 | [FastAPI 服务封装](../services/fastapi.md) |
| 需要查看观测事件 | [Event Center](../observability/event-center.md) → [DevTools](../observability/devtools.md) |
| 多阶段带分支的工作流 | [TriggerFlow 概览](../triggerflow/overview.md) → [模式](../triggerflow/patterns.md) |
| 长跑流程带人工审批 / 中断 | [Pause 与 Resume](../triggerflow/pause-and-resume.md) |
| 跨重启保存恢复 execution | [持久化与 Blueprint](../triggerflow/persistence-and-blueprint.md) |
| 从 `.end()` / `set_result()` / 旧 runtime_data 迁移 | [TriggerFlow 兼容](../triggerflow/compatibility.md) |

## 决策捷径

- 「我需要 TriggerFlow 吗？」——只在有明确的阶段、分支、并发或暂停恢复时才需要。带重试的单次请求不需要 TriggerFlow。
- 「Sync 还是 async？」——脚本和 demo 用 sync。服务、流式 UI 与 TriggerFlow 用 async。见 [Async First](../start/async-first.md)。
- 「Action 还是 tool API？」——新代码：`Agently.action` / `agent.use_actions(...)`、来自 `agently.builtins.actions` 的内置 package，以及 `agent.enable_python(...)`、`agent.enable_shell(...)`、`agent.enable_workspace(...)` 等场景 helper。已有的 `tool_func` / `use_tools` / `use_mcp` / `use_sandbox` 仍可用，但定位为兼容入口；见 [Action Runtime](../actions/action-runtime.md)。
- 「Executor 还是 Execution Environment？」——Executor 负责一次调用；Execution Environment 在调用前准备可复用或受 policy 约束的依赖；见 [Execution Environment](../actions/execution-environment.md)。
- 「Core API 还是语法糖？」——应用开发者应优先使用 built-in actions 和 Agent Component helpers。Core manager 与 provider 面向框架、action、plugin 开发者；见 [扩展边界](../architecture/extension-boundaries.md)。
- 「Observation event 还是 TriggerFlow event？」——observation event 归 [Event Center](../observability/event-center.md)；`emit` / `when` 与 runtime stream 归 [TriggerFlow 事件与流](../triggerflow/events-and-streams.md)。
