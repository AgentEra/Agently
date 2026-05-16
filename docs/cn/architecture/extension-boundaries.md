---
title: 扩展边界
description: Core、plugin、built-in capability 与 Agent Component 的职责边界。
keywords: Agently, architecture, plugin, action, execution environment, agent component
---

# 扩展边界

> 语言：[English](../../en/architecture/extension-boundaries.md) · **中文**

Agently 把稳定框架契约、默认实现和面向开发者的语法糖分开。

设计或扩展能力时使用这条规则：

```text
Core contract
  -> plugin/provider implementation
  -> built-in capability Action
  -> Agent Component or enable_* syntax sugar
  -> business application
```

## 谁需要关心

| 对象 | 优先使用 | 避免 |
|---|---|---|
| 应用开发者 | `agent.use_actions(...)`、`agent.use_mcp(...)`、built-in actions、未来的 `agent.enable_*` helpers | 直接使用 manager/provider API，除非应用明确拥有环境生命周期 |
| Action 开发者 | `register_action(...)`、自定义 `ActionExecutor`、`execution_environments=[...]` | 在 executor 内部私自启动长生命周期 sandbox、MCP client、process 或 service |
| Plugin 开发者 | `ExecutionEnvironmentProvider`、`ActionExecutor`、`ActionRuntime`、`ActionFlow` 插件契约 | 把 plugin 代码绑定到某个应用级语法糖 |
| 框架维护者 | Core data types、manager、dispatch path、兼容规则 | 把产品或业务专用行为放进 core |

## 分层职责

### Core

Core 定义稳定抽象和生命周期契约，应该保持克制。

Core 负责：

- data types 和公开 contract
- registry 与 dispatch 边界
- 生命周期状态机
- policy、approval、scope、cleanup 语义
- observation event contract

Core 不应该直接变成能力目录。例如，`ExecutionEnvironmentManager` 应该知道如何管理 environment requirement，但它不应该成为“让模型在我的 repo 里做 coding 工作”的用户入口。

### Plugins And Providers

Plugin 在 core contract 背后提供可替换的 backend 行为。

示例：

- 用于 Python、Bash、Node.js、Docker、MCP、SQLite、vector store、browser 或 remote runner 的 `ExecutionEnvironmentProvider`。
- 用于一次原子 action call 的 `ActionExecutor`。
- 用于 action planning 和 loop 行为的 `ActionRuntime`。
- 用于执行策略的 `ActionFlow`。

Provider 代码负责环境相关的 startup、health check 和 release。它不负责决定某个 agent 是否应该被允许使用这个环境。

不要为这一层引入 `ActionProvider`、`CapabilityProvider` 或独立 capability dispatcher
这类平行概念。可调用能力仍然是 `Action`；执行方式变化属于 `ActionExecutor`；
live resource 生命周期属于 `ExecutionEnvironmentProvider`。

### Built-in Capability Actions

Built-in 是 Agently 随框架提供的默认能力目录。它们以 Action 的形式暴露模型可调用操作，并且可以依赖 Execution Environment。

适合作为 built-in 的能力：

- 在受 policy 约束的 workspace 内执行 Bash 命令
- 在安全 sandbox 内运行 Python 代码
- 通过托管 runner 运行 Node.js 代码
- 文件搜索、读取、写入
- web 搜索与页面 browse
- SQLite 读写
- vector store 搜索与写入
- 调用预注册 Python 函数
- 调用 MCP tools

Action 是模型可见的调用面。只有当 action 需要托管 live dependency、隔离边界、可复用 client 或 cleanup policy 时，才需要 Execution Environment。

内置 capability package 的主 authoring/import path 与实现归属是
`agently.builtins.actions`。`agently.builtins.tools` 是既有代码的薄 legacy facade，
不作为新的 authoring 层。

### Agent Components And Syntax Sugar

Agent Component 应该给应用开发者提供场景级快捷入口。它们组合 built-in actions、policy、prompt guidance 与 environment requirements。

预期形态：

```python
agent.enable_python(...)
agent.enable_shell(...)
agent.enable_workspace(...)
agent.enable_nodejs(...)
agent.enable_sqlite(...)
agent.enable_vector_store(...)
agent.enable_coding_workspace(...)
```

这些 API 应该描述开发者意图，不应该要求应用开发者理解 `ExecutionEnvironmentHandle`、provider lifecycle 或 executor 内部机制。

### Typing 与 IDE 辅助

公开 API 应该尽可能用 typing 显性表达受限语义。有限选项使用 `Literal`，结构化 payload 使用
`TypedDict` 或 dataclass，plugin contract 使用 `Protocol`，已知形状的值应使用精确 union type，
不要用裸 `str` 或 `dict` 承载本可以被类型系统表达的约束。

Typing 是开发体验和 API 稳定性的一部分。例如 `desc_mode` 这类选项应写成
`Literal["append", "override", "default"]`，同时保留运行时校验来覆盖未类型化或动态调用方。

## Action 与 Execution Environment

Action 和 Execution Environment 是两个独立层。

Action 回答：

- 模型或 agent 能调用什么？
- 输入 schema 是什么？
- 一次调用如何归一化成 `ActionResult`？

Execution Environment 回答：

- 执行前必须存在什么 live dependency？
- 是否被 policy 和 approval 允许？
- 如何启动、复用、健康检查、按 scope 管理和释放？

不是所有 Action 都需要 Execution Environment。文件 policy 检查、纯本地函数和简单无状态操作可以只是普通 Action。当涉及生命周期、隔离、健康检查、凭证或 cleanup 时，再使用 Execution Environment。

## Skills 边界

Skills 不应该成为平行执行器。

Skill package 可以声明 guidance、scripts、MCP assets、hooks、resources 或 workflow templates。Skills 层应该把这些声明解析成 plan，再应用到已有 Agently 层：

- guidance -> prompt/context
- scripts -> run Python、run Bash、run Node.js 等 built-in actions
- MCP assets -> MCP actions 与 execution environment requirements
- hooks -> 经过审批的 actions 或 sandbox-backed executors
- workflow templates -> TriggerFlow templates
- resource dependencies -> resource providers 或 execution-local handles

这样 Skills 负责打包与选择，不会变成第二套 Action Runtime。

## 目录指引

如果文档说明“如何在应用中使用某个能力”，放在对应能力目录。如果文档说明“谁拥有哪一层、扩展应该放哪”，放在 `architecture/`。

示例：

- `actions/`：如何把可调用操作暴露给模型。
- `triggerflow/`：如何编排多步骤工作。
- `observability/`：如何观测和调试。
- `architecture/`：层 ownership 与扩展边界。
