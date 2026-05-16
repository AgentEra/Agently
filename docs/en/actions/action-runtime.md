---
title: Action Runtime
description: Action architecture below TriggerFlow — ActionRuntime, ActionFlow, ActionExecutor, and the Agently.action surface.
keywords: Agently, action runtime, ActionRuntime, ActionFlow, ActionExecutor, action_func, use_actions
---

# Action Runtime

> Languages: **English** · [中文](../../cn/actions/action-runtime.md)

Agently's action stack has three replaceable plugin layers below the orchestration layer:

```text
   TriggerFlow                  ◄── orchestration above actions (loops, branches, pause/resume)
       │
       ▼
   ActionRuntime                ◄── planning + dispatch
       │  (uses ActionFlow as the bridge to the orchestration layer)
       ▼
   ActionExecutor               ◄── atomic execution (local function, MCP, sandbox)
```

## Layers in detail

| Layer | What it owns | Default builtin |
|---|---|---|
| `TriggerFlow` | high-level orchestration above actions (loops, branches, pause/resume, sub-flow) — see [TriggerFlow](../triggerflow/overview.md) | the `TriggerFlow` core |
| `ActionRuntime` | planning protocol, action call normalization, default execution orchestration | `AgentlyActionRuntime` |
| `ActionFlow` | bridge between an `ActionRuntime` and a flow representation | `TriggerFlowActionFlow` |
| `ActionExecutor` | how one action actually runs | local function, MCP, Python/Bash sandbox, Search/Browse, Node.js, Docker, SQLite executors |
| `ExecutionEnvironment` | managed execution dependencies required before an executor call | MCP, Bash, Python, Node, Docker, Browser, SQLite providers |

`Action` in `agently.core` is a façade that wires:

- `ActionRegistry` and `ActionDispatcher` (stable core primitives)
- one active `ActionRuntime` plugin
- one active `ActionFlow` plugin

Default wiring:

```text
Agent → ActionExtension → Action façade → ActionRuntime → ActionFlow → ActionExecutor
```

## Plugin types

The plugin types you can replace are:

- `ActionRuntime` — for changing the planning protocol or call normalization
- `ActionFlow` — for changing the orchestration shape (e.g., custom flow representation)
- `ActionExecutor` — for adding a new backend (HTTP, gRPC, custom sandbox, remote worker)

Import the protocols and handler aliases from `agently.types.plugins`:

```python
from agently.types.plugins import (
    ActionExecutor,
    ActionRuntime,
    ActionFlow,
    ActionPlanningHandler,
    ActionExecutionHandler,
)
```

> The older `ToolManager` plugin type and `AgentlyToolManager` class are kept for explicit legacy use only and emit deprecation warnings. Don't write new plugins against `ToolManager`.

## The preferred surface — actions

For new code:

```python
from agently import Agently

agent = Agently.create_agent()


@agent.action_func
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@agent.action_func
async def python_code_executor(python_code: str):
    """Execute Python code and return the result."""
    ...


agent.use_actions([add, python_code_executor])

# Or register and run in one shot
@agent.auto_func
def calculate(formula: str) -> int:
    """Compute {formula}. Use available actions."""
    ...

print(calculate("3333+6666=?"))
```

| Surface | Purpose |
|---|---|
| `@agent.action_func` | mark a function as an action, derive its schema from signature + docstring |
| `agent.use_actions(actions)` | register a list, single action, or string-named action with the agent |
| `agent.use_actions(["name1", "name2"])` | register pre-registered actions by name |
| `agent.use_actions(Search(...))` | mount the built-in Search package from `agently.builtins.actions` |
| `agent.use_actions(Browse(...))` | mount the built-in Browse package from `agently.builtins.actions` |
| `agent.enable_python(...)` | mount a managed `run_python` action for deterministic code execution |
| `agent.enable_shell(...)` | mount a managed `run_bash` action with workspace and command allowlists |
| `agent.enable_nodejs(...)` | mount a managed `run_nodejs` action |
| `agent.enable_sqlite(...)` | mount a managed `query_sqlite` action |
| `agent.enable_workspace(...)` | mount workspace file list/search/read/write actions |
| `@agent.auto_func` | turn a Python function signature + docstring into a model-backed implementation that uses the agent's actions |
| `agent.get_action_result()` | retrieve action call records after a request |
| `extra.action_logs` | structured logs produced during the action loop |

For application code, prefer `enable_*` helpers when the goal is to give the
model a common capability such as Python, shell, or workspace access. Use
`register_action(..., executor=..., execution_environments=[...])` when you are
building a custom Action backend.

Built-in capability packages live under `agently.builtins.actions`. For example:

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15, backend="duckduckgo"))
agent.use_actions(Browse())
```

Search is an Action-native package and does not use Execution Environment;
proxy, timeout, backend, and region are package/executor configuration. Browse
is also Action-native; its default path is Playwright + BS4, while pyautogui is
kept as legacy/advanced configuration. If a Browse action needs a managed
browser/page/session, register it with Browser Execution Environment enabled.

The `desc=` argument on `enable_*` helpers is optional. By default it is appended
as additional guidance so the model still sees the baseline usage and safety
constraints. Use `desc_mode="override"` when you intentionally want to replace
the default description, or `desc_mode="default"` to ignore the supplied
description and keep only the built-in one.

## Execution recall

Instruction-heavy actions such as `run_bash`, `run_python`, `run_nodejs`,
`query_sqlite`, `browse`, and `search` keep later model context compact by
recording an execution digest plus artifact references.

The digest is what the next action-planning round normally sees. It includes the
action id, call id, purpose, status, a compact instruction preview, result
preview, redaction notes, and artifact refs. Full raw content such as complete
code, shell output, SQL rows, page HTML, screenshots, or logs is retained as a
redacted artifact instead of being inserted into every prompt.

When the model or application needs the omitted detail, read it explicitly:

```python
records = agent.get_action_result()
artifact_ref = records[0]["artifact_refs"][0]

raw = agent.action.read_action_artifact(
    artifact_id=artifact_ref["artifact_id"],
    action_call_id=artifact_ref["action_call_id"],
)
```

`Action.to_action_results(records)` uses the digest for instruction-heavy
actions, so follow-up replies can reason about what happened without receiving
the full payload by default.

## Compatibility surface — tools

The older surface still works:

```python
@agent.tool_func
def add(a: int, b: int) -> int:
    return a + b

agent.use_tool(add)
agent.use_tools([add])
agent.use_mcp("https://...")
agent.use_sandbox(...)
extra.tool_logs  # equivalent to extra.action_logs at the old surface
```

These remain valid public mounting surfaces. They map onto the new action runtime internally — they don't imply a `ToolManager` implementation. Migrate to the action surface when convenient; nothing breaks immediately.

## Handler interface

If you're writing a custom `ActionRuntime` or `ActionFlow` plugin, the planning and execution handlers use one stable two-argument contract:

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

Context fields include `prompt`, `settings`, `agent_name`, `round_index`, `max_rounds`, `done_plans`, `last_round_records`, `action`, `runtime`. Request fields include `action_list`, `planning_protocol`, `action_calls`, `async_call_action`, `concurrency`, `timeout`.

There is no legacy positional handler signature — the public contract is `(context, request)` only.

## Extension guidance

| You want to change | Replace |
|---|---|
| Just the backend (HTTP, gRPC, remote worker, sandbox) | `ActionExecutor` |
| The planning protocol or how calls are normalized | `ActionRuntime` |
| The orchestration shape between runtime and flow | `ActionFlow` |
| Higher-level flow control over many action calls | use `TriggerFlow` above the runtime — don't embed it inside an executor |
| Lifecycle for MCP/sandbox/process-like dependencies | declare an `ExecutionEnvironment` requirement — don't hide lifecycle inside an executor |

## See also

- [Actions Overview](overview.md) — where Action Runtime stops and orchestration starts
- [Execution Environment](execution-environment.md) — managed MCP/sandbox dependencies
- [Tools](tools.md) — the compat surface in more detail
- [MCP](mcp.md) — `agent.use_mcp(...)`
- [TriggerFlow Overview](../triggerflow/overview.md) — orchestration above actions
