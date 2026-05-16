---
title: Action Runtime
description: TriggerFlow 之下的 action 架构 —— ActionRuntime、ActionFlow、ActionExecutor 与 Agently.action 入口。
keywords: Agently, action runtime, ActionRuntime, ActionFlow, ActionExecutor, action_func, use_actions
---

# Action Runtime

> 语言：[English](../../en/actions/action-runtime.md) · **中文**

Agently 的 action 栈在编排层之下有三个可替换插件层：

```text
   TriggerFlow                  ◄── action 之上的编排（loop、分支、pause/resume）
       │
       ▼
   ActionRuntime                ◄── 规划 + 派发
       │  （ActionFlow 是与编排层的桥）
       ▼
   ActionExecutor               ◄── 原子执行（local function、MCP、sandbox）
```

## 各层详细

| 层 | 拥有什么 | 默认 builtin |
|---|---|---|
| `TriggerFlow` | action 之上的高层编排（loop、分支、pause/resume、子流）—— 见 [TriggerFlow](../triggerflow/overview.md) | `TriggerFlow` 核心 |
| `ActionRuntime` | 规划协议、action 调用归一化、默认执行编排 | `AgentlyActionRuntime` |
| `ActionFlow` | `ActionRuntime` 与 flow 表示之间的桥 | `TriggerFlowActionFlow` |
| `ActionExecutor` | 单个 action 实际怎么跑 | local function、MCP、Python/Bash sandbox、Search/Browse、Node.js、Docker、SQLite executors |
| `ExecutionEnvironment` | executor 调用前需要准备的托管执行依赖 | MCP、Bash、Python、Node、Docker、Browser、SQLite providers |

`agently.core.Action` 是门面，连线：

- `ActionRegistry` 与 `ActionDispatcher`（稳定核心原语）
- 一个 active `ActionRuntime` 插件
- 一个 active `ActionFlow` 插件

默认连线：

```text
Agent → ActionExtension → Action 门面 → ActionRuntime → ActionFlow → ActionExecutor
```

## 插件类型

可替换的插件类型：

- `ActionRuntime` —— 改规划协议或调用归一化
- `ActionFlow` —— 改编排形态（如自定义 flow 表示）
- `ActionExecutor` —— 加新后端（HTTP、gRPC、自定义沙箱、远程 worker）

从 `agently.types.plugins` import 协议与 handler 别名：

```python
from agently.types.plugins import (
    ActionExecutor,
    ActionRuntime,
    ActionFlow,
    ActionPlanningHandler,
    ActionExecutionHandler,
)
```

> 旧的 `ToolManager` 插件类型与 `AgentlyToolManager` 类仅作为遗留兼容保留并发 deprecation 警告。新插件不要写在 `ToolManager` 上。

## 推荐入口 —— actions

新代码：

```python
from agently import Agently

agent = Agently.create_agent()


@agent.action_func
async def add(a: int, b: int) -> int:
    """两个整数相加。"""
    return a + b


@agent.action_func
async def python_code_executor(python_code: str):
    """执行 Python 代码并返回结果。"""
    ...


agent.use_actions([add, python_code_executor])

# 或一次性注册并执行
@agent.auto_func
def calculate(formula: str) -> int:
    """计算 {formula}。使用可用的 action。"""
    ...

print(calculate("3333+6666=?"))
```

| 入口 | 用途 |
|---|---|
| `@agent.action_func` | 标记函数为 action，从签名 + docstring 推 schema |
| `agent.use_actions(actions)` | 在 agent 上注册 list、单个 action 或字符串名 action |
| `agent.use_actions(["name1", "name2"])` | 按名注册预注册的 action |
| `agent.use_actions(Search(...))` | 挂载来自 `agently.builtins.actions` 的内置 Search package |
| `agent.use_actions(Browse(...))` | 挂载来自 `agently.builtins.actions` 的内置 Browse package |
| `agent.enable_python(...)` | 挂载托管 `run_python` action，用于确定性代码执行 |
| `agent.enable_shell(...)` | 挂载带 workspace 与命令 allowlist 的托管 `run_bash` action |
| `agent.enable_nodejs(...)` | 挂载托管 `run_nodejs` action |
| `agent.enable_sqlite(...)` | 挂载托管 `query_sqlite` action |
| `agent.enable_workspace(...)` | 挂载 workspace 文件列表、搜索、读取、写入 actions |
| `@agent.auto_func` | 把 Python 函数签名 + docstring 变成模型驱动的实现，使用 agent 的 action |
| `agent.get_action_result()` | 请求后取 action 调用记录 |
| `extra.action_logs` | action loop 期间产生的结构化日志 |

应用代码要给模型开放 Python、shell、workspace 等常见能力时，优先使用
`enable_*` helpers。只有在开发自定义 Action 后端时，才需要使用
`register_action(..., executor=..., execution_environments=[...])`。

内置能力 package 位于 `agently.builtins.actions`。例如：

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15, backend="duckduckgo"))
agent.use_actions(Browse())
```

Search 是 Action-native package，不进入 Execution Environment；proxy、timeout、
backend、region 都属于 package/executor 配置。Browse 也是 Action-native；默认主线是
Playwright + BS4，pyautogui 保留为 legacy/advanced 配置。如果 Browse action 需要托管
browser/page/session，可以启用 Browser Execution Environment。

`enable_*` helpers 的 `desc=` 是可选项。默认会作为补充说明追加，确保模型仍然看到基础用法和安全边界。
如果你确实要替换默认描述，使用 `desc_mode="override"`；如果要忽略传入描述、只保留内置描述，使用
`desc_mode="default"`。

## 执行回溯

`run_bash`、`run_python`、`run_nodejs`、`query_sqlite`、`browse`、`search`
这类指令型 action 会记录一份执行 digest 和一组 artifact references，用来控制后续模型上下文长度。

后续 action planning round 默认看到的是 digest。它包含 action id、call id、目的、状态、精简指令预览、
结果预览、脱敏说明和 artifact refs。完整代码、shell 输出、SQL 结果集、页面 HTML、截图、日志等原始内容
会以脱敏 artifact 形式保留，不会默认塞进每一轮 prompt。

如果模型或应用需要回溯细节，可以显式读取：

```python
records = agent.get_action_result()
artifact_ref = records[0]["artifact_refs"][0]

raw = agent.action.read_action_artifact(
    artifact_id=artifact_ref["artifact_id"],
    action_call_id=artifact_ref["action_call_id"],
)
```

`Action.to_action_results(records)` 对指令型 action 使用 digest，因此后续回复能知道发生了什么，
但不会默认拿到完整 payload。

## 兼容入口 —— tools

旧入口仍可用：

```python
@agent.tool_func
def add(a: int, b: int) -> int:
    return a + b

agent.use_tool(add)
agent.use_tools([add])
agent.use_mcp("https://...")
agent.use_sandbox(...)
extra.tool_logs  # 旧入口的 extra.action_logs
```

它们仍是有效的公开挂载入口。内部映射到新 action runtime —— 不意味着 `ToolManager` 实现。方便时迁到 action 入口；什么都不会立刻坏。

## handler 接口

如果你写自定义 `ActionRuntime` 或 `ActionFlow` 插件，规划与执行 handler 用稳定的双参数契约：

```python
async def planning_handler(
    context: ActionRunContext,
    request: ActionPlanningRequest,
) -> ActionDecision:
    ...


async def execution_handler(
    context: ActionRunContext,
    request: ActionExecutionRequest,
) -> list[ActionResult]:
    ...
```

context 字段含 `prompt`、`settings`、`agent_name`、`round_index`、`max_rounds`、`done_plans`、`last_round_records`、`action`、`runtime`。request 字段含 `action_list`、`planning_protocol`、`action_calls`、`async_call_action`、`concurrency`、`timeout`。

没有 legacy positional 签名 —— 公开契约只是 `(context, request)`。

## 扩展指南

| 你想改 | 替换 |
|---|---|
| 仅后端（HTTP、gRPC、远程 worker、sandbox） | `ActionExecutor` |
| 规划协议或调用归一化 | `ActionRuntime` |
| runtime 与 flow 之间的编排形态 | `ActionFlow` |
| 多个 action 调用之上的更高层流控 | 用 `TriggerFlow` 在 runtime 之上 —— 不要把它塞进 executor |
| MCP/sandbox/process 类依赖的生命周期 | 声明 `ExecutionEnvironment` requirement —— 不要把生命周期藏进 executor |

## 另见

- [Actions 概览](overview.md) —— Action Runtime 到哪里停止、编排从哪里开始
- [Execution Environment](execution-environment.md) —— 托管 MCP/sandbox 执行依赖
- [工具](tools.md) —— 兼容入口详细
- [MCP](mcp.md) —— `agent.use_mcp(...)`
- [TriggerFlow 概览](../triggerflow/overview.md) —— action 之上的编排
