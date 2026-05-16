---
title: Extension Boundaries
description: Core, plugin, built-in capability, and Agent Component ownership rules.
keywords: Agently, architecture, plugin, action, execution environment, agent component
---

# Extension Boundaries

> Languages: **English** · [中文](../../cn/architecture/extension-boundaries.md)

Agently separates stable framework contracts from default implementations and
developer-facing shortcuts.

Use this rule when designing or extending a capability:

```text
Core contract
  -> plugin/provider implementation
  -> built-in capability Action
  -> Agent Component or enable_* syntax sugar
  -> business application
```

## Who Should Care

| Audience | What to use first | What to avoid |
|---|---|---|
| App developers | `agent.use_actions(...)`, `agent.use_mcp(...)`, built-in actions, future `agent.enable_*` helpers | Direct manager/provider APIs unless the app owns the environment lifecycle |
| Action developers | `register_action(...)`, custom `ActionExecutor`, `execution_environments=[...]` | Starting long-lived sandboxes, MCP clients, processes, or services inside an executor |
| Plugin developers | `ExecutionEnvironmentProvider`, `ActionExecutor`, `ActionRuntime`, `ActionFlow` plugin contracts | Coupling plugin code to one app-level shortcut |
| Framework maintainers | Core data types, managers, dispatch paths, compatibility rules | Putting product-specific behavior into core |

## Layer Responsibilities

### Core

Core defines stable abstractions and lifecycle contracts. It should stay small.

Core owns:

- data types and public contracts
- registries and dispatch boundaries
- lifecycle state machines
- policy, approval, scope, and cleanup semantics
- observation event contracts

Core should not directly become the feature catalog. For example,
`ExecutionEnvironmentManager` should know how to manage an environment
requirement, but it should not be the user-facing API for "do coding work in my
repo".

### Plugins And Providers

Plugins implement replaceable backend behavior behind core contracts.

Examples:

- `ExecutionEnvironmentProvider` for Python, Bash, Node.js, Docker, MCP, SQLite,
  vector stores, browsers, or remote runners.
- `ActionExecutor` for one atomic action call.
- `ActionRuntime` for action planning and loop behavior.
- `ActionFlow` for execution strategy.

Provider code owns environment-specific startup, health check, and release. It
does not own app-level decisions about whether an agent should be allowed to use
that environment.

Do not introduce parallel nouns such as `ActionProvider`,
`CapabilityProvider`, or a standalone capability dispatcher for this layer.
Callable ability remains `Action`; execution variation belongs to
`ActionExecutor`; live resource lifecycle belongs to `ExecutionEnvironmentProvider`.

### Built-in Capability Actions

Built-ins are the default capability catalog shipped by Agently. They expose
model-callable operations as Actions and may depend on Execution Environment.

Good built-in candidates:

- run Bash commands inside a policy-bound workspace
- run Python code in a safe sandbox
- run Node.js code through a managed runner
- search, read, and write files
- search the web and browse pages
- read and write SQLite data
- search and update vector stores
- call pre-registered Python functions
- call MCP tools

Action is the model-visible callable surface. Execution Environment is only used
when the action needs a managed live dependency, isolation boundary, reusable
client, or cleanup policy.

Built-in capability packages use `agently.builtins.actions` as the primary
authoring/import path and implementation home. `agently.builtins.tools` is a
thin legacy facade for existing code and should not be treated as the new
authoring layer.

### Agent Components And Syntax Sugar

Agent Components should provide scenario-level shortcuts for application
developers. They compose built-in actions, policies, prompt guidance, and
environment requirements.

Expected shape:

```python
agent.enable_python(...)
agent.enable_shell(...)
agent.enable_workspace(...)
agent.enable_nodejs(...)
agent.enable_sqlite(...)
agent.enable_vector_store(...)
agent.enable_coding_workspace(...)
```

These APIs should describe developer intent. They should not force app
developers to understand `ExecutionEnvironmentHandle`, provider lifecycle, or
executor internals.

### Typing And IDE Assistance

Public APIs should expose constrained semantics through typing whenever
practical. Use `Literal` for finite option sets, `TypedDict` or dataclasses for
structured payloads, `Protocol` for plugin contracts, and precise union types
instead of bare `str` or `dict` when values have a known shape.

Typing is part of developer experience and API stability. For example, an option
such as `desc_mode` should be typed as `Literal["append", "override",
"default"]`, while runtime validation should remain for untyped or dynamic
callers.

## Action And Execution Environment

Action and Execution Environment are separate layers.

Action answers:

- What can the model or agent call?
- What input schema does it use?
- How is one call normalized into `ActionResult`?

Execution Environment answers:

- What live dependency must exist before execution?
- Is it allowed under policy and approval?
- How is it started, reused, health-checked, scoped, and released?

Not every Action needs Execution Environment. File policy checks, pure local
functions, and simple stateless operations can be plain Actions. Use Execution
Environment when lifecycle, isolation, health, credentials, or cleanup matters.

## Skills Boundary

Skills should not be a parallel executor.

A skill package may declare guidance, scripts, MCP assets, hooks, resources, or
workflow templates. The Skills layer should resolve those declarations into a
plan, then apply them to existing Agently layers:

- guidance -> prompt/context
- scripts -> built-in actions such as run Python, run Bash, or run Node.js
- MCP assets -> MCP actions plus execution environment requirements
- hooks -> approved actions or sandbox-backed executors
- workflow templates -> TriggerFlow templates
- resource dependencies -> resource providers or execution-local handles

This keeps Skills useful for packaging and selection without making them a
second Action Runtime.

## Directory Guidance

If a document explains how to use a capability in an app, put it near the
capability docs. If it explains ownership boundaries or extension rules, put it
under `architecture/`.

Examples:

- `actions/`: how to expose callable operations to a model.
- `triggerflow/`: how to orchestrate multi-step work.
- `observability/`: how to observe and debug.
- `architecture/`: who owns which layer and where extensions belong.
