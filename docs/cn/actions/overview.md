---
title: Actions 概览
description: Action Runtime、工具兼容、MCP、沙箱执行与 TriggerFlow 的关系。
keywords: Agently, Action Runtime, tools, MCP, sandbox, TriggerFlow
---

# Actions 概览

> 语言：[English](../../en/actions/overview.md) · **中文**

Action 是 Agently 的请求期能力层：模型在回答一次请求时，可以选择调用已注册的函数、MCP tool、沙箱执行器，或其他后端。

它不是编排层。如果你需要分支、fan-out（扇出）、审批、等待恢复或可持久化执行，应把 TriggerFlow 放在 request 之上，再在 chunk 里调用 agent。

## 边界

| 主题 | 负责什么 | 不负责什么 |
|---|---|---|
| Action Runtime | 规划、action call 归一化、分发、action 日志 | 长跑 workflow lifecycle |
| Agent Component helpers | `enable_python`、`enable_shell`、`enable_workspace` 等业务快捷入口 | Provider 生命周期内部细节 |
| 工具兼容 | `tool_func`、`use_tool`、`use_tools`、`extra.tool_logs` 这些旧别名 | 新扩展架构设计 |
| MCP | 把本地或远程 MCP tool 装进 action surface | 独立工作流引擎 |
| 沙箱 action | 通过 `ActionExecutor` 后端运行代码 | 通用容器编排 |
| TriggerFlow | 阶段、分支、fan-out、暂停恢复、持久化 | tool schema 注册 |

## 当前源码结构

默认插件注册在 [`agently/_default_init.py`](../../../agently/_default_init.py)：

- `ActionRuntime`：`AgentlyActionRuntime`
- `ActionFlow`：`TriggerFlowActionFlow`
- `ActionExecutor`：本地函数、MCP、Python 沙箱、Bash 沙箱

公共 façade（外观入口）在 [`agently/core/Action.py`](../../../agently/core/Action.py)。Agent 级挂载入口在 [`agently/builtins/agent_extensions/ActionExtension.py`](../../../agently/builtins/agent_extensions/ActionExtension.py)。可运行示例按场景列在 [`examples/action_runtime/README.md`](../../../examples/action_runtime/README.md)，真实模型驱动的 cookbook 模式示例在 [`examples/cookbook/`](../../../examples/cookbook/)。

## 怎么读

| 你要做 | 去读 |
|---|---|
| 写新的函数 action | [Action Runtime](action-runtime.md) |
| 给业务 agent 开放 Python、shell 或 workspace 能力 | [Action Runtime](action-runtime.md) |
| 旧代码还在用 `tool_func` | [工具兼容](tools.md) |
| 使用本地或 HTTP MCP server | [MCP](mcp.md) |
| 让多个 action 跨步骤协作 | [TriggerFlow 模式](../triggerflow/patterns.md) |
| 把会用 action 的 agent 暴露成 HTTP 服务 | [FastAPI 服务封装](../services/fastapi.md) |

## 源码说明

`ToolManager` 插件类型仍保留给旧代码使用，但新的 examples 走 Action Runtime。`examples/action_runtime/` 里的示例会先用 `agent.get_action_result()` 查看中间 `ActionResult`，再调用 `agent.get_response()`，并读取 `extra.action_logs`。
