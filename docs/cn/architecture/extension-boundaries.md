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

这个顺序是框架规范，不只是理解模型。新增能力前必须先判断：稳定 contract
由哪一层拥有，可替换实现由哪个 plugin/provider 拥有，面向用户的快捷入口由哪个
Agent Component 或 facade 拥有。

## 谁需要关心

| 对象 | 优先使用 | 避免 |
|---|---|---|
| 应用开发者 | `agent.use_actions(...)`、`agent.use_mcp(...)`、built-in actions、未来的 `agent.enable_*` helpers | 直接使用 manager/provider API，除非应用明确拥有环境生命周期 |
| Action 开发者 | `register_action(...)`、自定义 `ActionExecutor`、`execution_resources=[...]` | 在 executor 内部私自启动长生命周期 sandbox、MCP client、process 或 service |
| Plugin 开发者 | `ExecutionResourceProvider`、`ActionExecutor`、`ActionRuntime`、`ActionFlow` 插件契约 | 把 plugin 代码绑定到某个应用级语法糖 |
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

Core 不应该直接变成能力目录。例如，`ExecutionResourceManager` 应该知道如何管理 environment requirement，但它不应该成为“让模型在我的 repo 里做 coding 工作”的用户入口。

Agently-Stage 在这个边界上属于私有机制依赖：TriggerFlow 直接持有一个 Stage
structured scope，由 Stage 支撑 caller-loop task 创建、ownership、取消传递、
origin 诊断与 settlement；Stage 在首个任务进入时才 lazy 选择 caller loop。
TriggerFlow 自己创建的 managed work 使用 `Stage.create_task(...)`，只有真实存在于
接管之前的外部 task 才使用 `Stage.adopt(...)`。Stage 是唯一的实时 task/origin
registry；TriggerFlow 只保留业务错误投影、单 deadline close policy 与
RuntimeEvent 映射，不再存在 Agently task-scope adapter 或影子 registry。
Tunnel 支撑 TriggerFlow 的进程内 execution stream 传输。Event Center 保留原生
后台任务机制，因为 Stage-backed 候选在热路径上存在可测量的额外开销。
EventCenter、RuntimeEvent、SignalNet、
TriggerFlowExecution 与 AgentExecution 仍是语义 owner。应用与插件不应选择
所谓“Stage runtime”，也不应把 Stage 对象放入 execution state。Stage
EventEmitter 不替换 EventCenter/SignalNet。Agently 内部的同步/异步调用适配
统一使用 `StageCallBridge`；`FunctionShifter` 只作为 deprecated 公共兼容
facade 保留，并委托给同一个 bridge。
调用形态转换默认使用轻桥接。只有 TriggerFlow、EventCenter 这类真正拥有调度
生命周期的边界才请求 `managed=True`，避免兼容 helper 仅仅因为转换 sync/async
形态就改变业务错误或取消语义。

Agently 依赖 Agently-Stage 0.3.x 兼容线中的 0.3.5 或更高版本。
`LocalTaskScope` 不是 Agently 的集成接口；它只是 Agently-Stage 中为兼容保留、
并计划在 Agently-Stage 0.4 移除的 deprecated shim。

当 plugin 或 Agent Component 层已经有相应职责时，Core 也不应该拥有 plugin output
prompt、provider-specific default，或 Agent Component 的便利行为。Plugin 可以导入 core
contract；core 不能依赖 built-in plugin 或 Agent Component 实现。

### Plugins And Providers

Plugin 在 core contract 背后提供可替换的 backend 行为。

示例：

- 用于 Bash、provider-neutral 代码执行、MCP、SQLite、vector store、browser 或 remote runner 的 `ExecutionResourceProvider`；Python 与 Node.js 属于 `CodeRuntimeAdapter`，不是独立 provider。
- 用于一次原子 action call 的 `ActionExecutor`。
- 用于 action planning 和 loop 行为的 `ActionRuntime`。
- 用于执行策略的 `ActionFlow`。

Provider 代码负责环境相关的 startup、health check 和 release。它不负责决定某个 agent 是否应该被允许使用这个环境。

不要为这一层引入 `ActionProvider`、`CapabilityProvider` 或独立 capability dispatcher
这类平行概念。可调用能力仍然是 `Action`；执行方式变化属于 `ActionExecutor`；
live resource 生命周期属于 `ExecutionResourceProvider`。

### Built-in Capability Actions

Built-in 是 Agently 随框架提供的默认能力目录。它们以 Action 的形式暴露模型可调用操作，并且可以依赖 ExecutionResource。

适合作为 built-in 的能力：

- 在受 policy 约束的 workspace 内执行 Bash 命令
- 通过同一组 Workspace-bound CodeExecution Actions 与有序 execution providers
  运行 Python、Node.js、Go 或 C++
- 文件搜索、读取、写入
- web 搜索与页面 browse
- SQLite 读写
- vector store 搜索与写入
- 调用预注册 Python 函数
- 调用 MCP tools

Action 是模型可见的调用面。只有当 action 需要托管 live dependency、隔离边界、可复用 client 或 cleanup policy 时，才需要 ExecutionResource。

内置 capability package 的主 authoring/import path 与实现归属是
`agently.builtins.actions`。`agently.builtins.tools` 是既有代码的薄 legacy facade，
不作为新的 authoring 层。

### Agent Components And Syntax Sugar

Agent Component 应该给应用开发者提供场景级快捷入口。它们组合 built-in actions、policy、prompt guidance 与 environment requirements。

预期形态：

```python
agent.enable_python(...)
agent.enable_shell(...)
agent.enable_task_workspace_file_actions(...)
agent.enable_nodejs(...)
agent.enable_code_runtime(...)
agent.enable_sqlite(...)
agent.enable_vector_store(...)
agent.enable_coding_workspace(...)
```

这些 API 应该描述开发者意图，不应该要求应用开发者理解 `ExecutionResourceHandle`、provider lifecycle 或 executor 内部机制。Python 与 Node.js helper 和通用 code runtime 使用同一条 Workspace-bound、有序 provider 主链；只有显式授权的可信路径才允许无防护本地执行。shell 仍是独立的宽命令 Action。

### Typing 与 IDE 辅助

公开 API 应该尽可能用 typing 显性表达受限语义。有限选项使用 `Literal`，结构化 payload 使用
`TypedDict` 或 dataclass，plugin contract 使用 `Protocol`，已知形状的值应使用精确 union type，
不要用裸 `str` 或 `dict` 承载本可以被类型系统表达的约束。

Typing 是开发体验和 API 稳定性的一部分。例如 `desc_mode` 这类选项应写成
`Literal["append", "override", "default"]`，同时保留运行时校验来覆盖未类型化或动态调用方。

### 模块组织

Core 和 builtins 能力都可以用子目录 package 表达。当一个 feature 同时包含 facade、
manager、默认实现、registry、adapter、policy 或 validation 等多个架构角色时，
应优先采用这种形态。

新增 core 或 builtins 能力时，不应默认使用单文件模式。先判断 feature 预期的
子模块体量和 ownership 边界；只有能力确实体量很小、拆分反而是过度设计时，
才保持单文件。

已落地案例包括 `core/Action`、`core/TriggerFlow`、`core/orchestration/TaskDAG`、
`core/context`、`core/TaskWorkspace`、`core/storage`、
`builtins/plugins/ExecutionResourceProvider` 和 `core/application/SkillLibrary`。
`core/application/SkillsExecutor` 是轻量兼容 facade，不是 execution plugin。
公开 import 通过 package `__init__.py` 和顶层 re-export 保持稳定。

## Action 与 ExecutionResource

Action 和 ExecutionResource 是两个独立层。

Action 回答：

- 模型或 agent 能调用什么？
- 输入 schema 是什么？
- 一次调用如何归一化成 `ActionResult`？

ExecutionResource 回答：

- 执行前必须存在什么 live dependency？
- 是否被 policy 和 approval 允许？
- 如何启动、复用、健康检查、按 scope 管理和释放？

不是所有 Action 都需要 ExecutionResource。文件 policy 检查、纯本地函数和简单无状态操作可以只是普通 Action。当涉及生命周期、隔离、健康检查、凭证或 cleanup 时，再使用 ExecutionResource。

## Skills 边界

Skills 不应该成为平行执行器。

Skill package 可以声明 guidance、scripts、MCP assets、hooks、resources 或 workflow templates。Skills 层应该把这些声明解析成 plan，再应用到已有 Agently 层：

- guidance -> prompt/context
- 已授权 scripts -> 绑定精确 Skill revision 与 TaskWorkspace grant 的普通 code-execution Actions
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
