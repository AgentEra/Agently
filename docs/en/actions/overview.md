---
title: Actions Overview
description: How Action Runtime, tools compatibility, MCP, sandbox execution, and TriggerFlow relate.
keywords: Agently, Action Runtime, tools, MCP, sandbox, TriggerFlow
---

# Actions Overview

> Languages: **English** · [中文](../../cn/actions/overview.md)

Actions are Agently's request-time capability layer: the model can choose a registered function, MCP tool, sandbox executor, or other backend while answering one request.

This is not the orchestration layer. If you need branches, fan-out, approval, wait/resume, or durable execution, put TriggerFlow above the request and call the agent from a chunk.

## Boundaries

| Topic | Owns | Does not own |
|---|---|---|
| Action Runtime | Planning, action-call normalization, dispatch, action logs | Long-running workflow lifecycle |
| Agent Component helpers | Business-facing shortcuts such as `enable_python`, `enable_shell`, `enable_workspace` | Provider lifecycle internals |
| Tools compatibility | `tool_func`, `use_tool`, `use_tools`, `extra.tool_logs` aliases | New extension design |
| MCP | Loading remote or local MCP tools into the action surface | A separate workflow engine |
| Sandbox actions | Running code through an `ActionExecutor` backend | General container orchestration |
| TriggerFlow | Stages, branches, fan-out, pause/resume, persistence | Tool schema registration |

## Current source-backed structure

Default plugin wiring lives in [`agently/_default_init.py`](../../../agently/_default_init.py):

- `ActionRuntime`: `AgentlyActionRuntime`
- `ActionFlow`: `TriggerFlowActionFlow`
- `ActionExecutor`: local function, MCP, Python sandbox, Bash sandbox

The public façade is [`agently/core/Action.py`](../../../agently/core/Action.py). Agent-level mounting lives in [`agently/builtins/agent_extensions/ActionExtension.py`](../../../agently/builtins/agent_extensions/ActionExtension.py). The runnable examples are grouped under [`examples/action_runtime/README.md`](../../../examples/action_runtime/README.md), with model-backed cookbook patterns under [`examples/cookbook/`](../../../examples/cookbook/).

## Reading choices

| You need | Read |
|---|---|
| New function actions | [Action Runtime](action-runtime.md) |
| Give an app agent Python, shell, or workspace access | [Action Runtime](action-runtime.md) |
| Existing code still uses `tool_func` | [Tools Compatibility](tools.md) |
| Use a local or HTTP MCP server | [MCP](mcp.md) |
| Route many actions across steps | [TriggerFlow Patterns](../triggerflow/patterns.md) |
| Expose the action-using agent over HTTP | [FastAPI Service Exposure](../services/fastapi.md) |

## Source notes

The `ToolManager` plugin type still exists for legacy use, but new examples use the Action Runtime path. The examples in `examples/action_runtime/` inspect `agent.get_action_result()` first, then call `agent.get_response()` and read `extra.action_logs`.
