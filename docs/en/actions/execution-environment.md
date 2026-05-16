---
title: Execution Environment
description: Managed execution dependencies for Actions and TriggerFlow.
keywords: Agently, ExecutionEnvironment, Action, TriggerFlow, sandbox, MCP, runtime_resources
---

# Execution Environment

> Languages: **English** · [中文](../../cn/actions/execution-environment.md)

Execution Environment is the framework-level layer that prepares and releases
managed execution dependencies before an action or workflow step runs.

It owns lifecycle and policy for resources such as MCP transports, command
runners, sandboxes, browsers, SQLite connections, and external process runners.
Action and TriggerFlow can require those environments, but they do not own
environment lifecycle.

## Audience

Most application developers should not start here. Prefer built-in actions and
Agent Component helpers that describe intent, such as enabling Python, shell,
workspace, MCP, SQLite, vector-store, or coding-workspace capabilities.

Read this page when you are:

- writing a custom `ActionExecutor` that depends on a managed live resource
- writing an `ExecutionEnvironmentProvider` plugin
- reviewing how Action or TriggerFlow receives managed resources
- designing a new built-in capability that needs sandbox, process, MCP, client,
  credential, or cleanup lifecycle

Do not expose `Agently.execution_environment` as the default app-development
mental model. It is the core lifecycle layer behind higher-level capabilities.

## Where it sits

```text
Agent Component / built-in Action / custom Action / TriggerFlow / Skills plan
        |
        v
ActionSpec.execution_environments or TriggerFlow execution requirements
        |
        v
ExecutionEnvironmentManager
        |
        v
ExecutionEnvironmentProvider
        |
        v
managed handle / live resource
```

V1 exposes the global manager as:

```python
from agently import Agently

Agently.execution_environment
```

Most application code does not call the manager directly. Built-in MCP, Bash,
Python, Node.js, Docker, Browser, and SQLite actions can declare their
requirements and the Action dispatcher ensures them before executor calls.

For the broader ownership model, see
[Architecture / Extension Boundaries](../architecture/extension-boundaries.md).

## Built-in behavior

The built-in providers are:

| Kind | Used by | Managed resource |
|---|---|---|
| `mcp` | `agent.use_mcp(...)` / MCP actions | MCP transport resource |
| `bash` | `agent.enable_shell(...)` / Bash sandbox actions | configured command runner |
| `python` | `agent.enable_python(...)` / Python sandbox actions | configured Python sandbox |
| `node` | `agent.enable_nodejs(...)` / Node.js executor actions | configured Node.js runner |
| `docker` | Docker executor actions | Docker CLI runner |
| `browser` | Browse actions that opt into managed browser resources | managed browser/page/session wrapper |
| `sqlite` | `agent.enable_sqlite(...)` / SQLite executor actions | SQLite connection |

Search intentionally is not listed here. It is a stateless Action-native
capability package; proxy, timeout, backend, and region belong to the Search
package/executor configuration rather than Execution Environment.

These providers are low-level environment implementations. User-facing
capabilities should normally be exposed as Actions, and scenario shortcuts
should be exposed through Agent Components or future `agent.enable_*` helpers.

Action execution flow:

```text
ActionCall
  -> resolve ActionSpec
  -> ensure ActionSpec.execution_environments
  -> inject execution_environment_resources into action_call
  -> ActionExecutor.execute(...)
  -> release action_call-scoped handles
```

Custom `ActionExecutor.execute(...)` signatures do not change. Managed handles
are passed through `action_call["execution_environment_handles"]` and live
resources through `action_call["execution_environment_resources"]`.

## TriggerFlow

TriggerFlow still uses `runtime_resources` as the compatibility surface for
live execution-local resources. Execution Environment does not rename or replace
that API.

You can pass managed requirements at execution creation or start:

```python
execution = flow.create_execution(
    execution_environments=[
        {
            "kind": "python",
            "scope": "execution",
            "resource_key": "sandbox",
        }
    ],
)
```

The manager ensures the resource, injects it into the execution-local resources,
and releases it when the execution closes. Manual `runtime_resources={...}` are
still unmanaged and are not health-checked or auto-released by the manager.

## Direct manager API

This API is for framework, action, and plugin developers.

The manager supports:

```python
Agently.execution_environment.declare(requirement)
Agently.execution_environment.ensure(requirement_or_id)
await Agently.execution_environment.async_ensure(requirement_or_id)
Agently.execution_environment.release(handle_or_id)
Agently.execution_environment.release_scope("session", owner_id)
Agently.execution_environment.inspect(id)
Agently.execution_environment.list(scope="execution")
Agently.execution_environment.set_decision_handler(handler)
```

Declaration is lazy. It validates and records a requirement but does not start
anything. `ensure(...)` starts or reuses a handle subject to policy and approval.
Before reusing a ready handle, the manager calls
`provider.async_health_check(handle)`. Healthy handles are reused with
`ref_count + 1`; unhealthy handles emit `execution_environment.unhealthy`, are
released, and then a fresh handle is ensured. V2 intentionally does not add a
background scheduler, lease TTL, or automatic reconnect loop.

If you are building an application, first check whether a built-in action or
Agent Component already exposes the capability you need.

## Observation

The manager emits framework events in the `execution_environment.*` family:

- `execution_environment.declared`
- `execution_environment.approval_required`
- `execution_environment.ensuring`
- `execution_environment.ready`
- `execution_environment.unhealthy`
- `execution_environment.releasing`
- `execution_environment.released`
- `execution_environment.failed`

Payloads include stable ids and status metadata only. They must not include raw
credentials, environment variables, command secrets, or live resource objects.

## Examples

Runnable examples are available in
[`examples/execution_environment`](../../../examples/execution_environment/README.md).
Start with the local `agent.enable_python(...)` quickstart, then move to the
Ollama and DeepSeek model-driven examples. The TriggerFlow example is intended
for workflow or framework developers who need managed execution-local resources.

## See also

- [Action Runtime](action-runtime.md)
- [MCP](mcp.md)
- [TriggerFlow State and Resources](../triggerflow/state-and-resources.md)
