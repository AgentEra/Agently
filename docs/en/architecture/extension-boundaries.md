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

This order is a framework rule, not only a mental model. Before adding a
feature, decide which layer owns the stable contract, which plugin/provider owns
replaceable implementation behavior, and which Agent Component or facade owns
the user-facing shortcut.

## Who Should Care

| Audience | What to use first | What to avoid |
|---|---|---|
| App developers | `agent.use_actions(...)`, `agent.use_mcp(...)`, built-in actions, future `agent.enable_*` helpers | Direct manager/provider APIs unless the app owns the environment lifecycle |
| Action developers | `register_action(...)`, custom `ActionExecutor`, `execution_resources=[...]` | Starting long-lived sandboxes, MCP clients, processes, or services inside an executor |
| Plugin developers | `ExecutionResourceProvider`, `ActionExecutor`, `ActionRuntime`, `ActionFlow` plugin contracts | Coupling plugin code to one app-level shortcut |
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
`ExecutionResourceManager` should know how to manage an environment
requirement, but it should not be the user-facing API for "do coding work in my
repo".

Agently-Stage is a private mechanism dependency at this boundary. TriggerFlow
directly owns one Stage structured scope for caller-loop task creation,
ownership, cancellation delivery, origin diagnostics, and settlement; Stage
selects the caller loop lazily when the first task enters. TriggerFlow-created
managed work uses `Stage.create_task(...)`; a genuinely pre-existing external
task may use `Stage.adopt(...)`. Stage remains the single live task/origin
registry. TriggerFlow retains only its business error projection, one-deadline
close policy, and RuntimeEvent mapping; there is no Agently task-scope adapter
or shadow registry. Stage's Tunnel supports
TriggerFlow's process-local execution stream transport. EventCenter keeps its
native background-task mechanism because the Stage-backed candidate had a
measurable hot-path cost. EventCenter, RuntimeEvent, SignalNet,
TriggerFlowExecution, and AgentExecution remain the semantic owners.
An application or plugin may use Agently-Stage's public `with Stage()`,
`Stage.as_sync()` / `Stage.as_async()`, `Tunnel`, or `EventEmitter` capabilities
independently for its own process-local lifetime, call bridge, replay channel,
or listener boundary. That does not make Stage the Agently workflow owner:
integrations must not place Stage objects in execution state or use Stage
EventEmitter to replace EventCenter/SignalNet. Agently's internal sync/async call adaptation uses
`StageCallBridge`; `FunctionShifter` remains only as a deprecated public
compatibility facade. Its scalar `syncify()` / `asyncify()` methods delegate to
the scoped `Stage.as_sync()` / `Stage.as_async()` deep interface, while its
future and stream compatibility methods retain the advanced bridge.
`StageCallBridge` call-shape conversion is light by default. Only a scheduling owner such as
TriggerFlow or EventCenter requests `managed=True`; this prevents compatibility
helpers from changing error or cancellation semantics merely because they adapt
sync and async call shapes.

Agently 4.1.4.7 depends on Agently-Stage 0.3.8 or newer in the 0.3 compatibility line.
Stage owns automatic physical carrier selection across mixed sync/async
boundaries; TriggerFlow still owns workflow lifecycle and public state. Reusing
one safe carrier does not merge separate Stage scope inventories or expose
carrier concepts through Agently's public lifecycle.
`LocalTaskScope` is not an Agently integration surface; it is a deprecated
Agently-Stage compatibility shim scheduled for removal in Agently-Stage 0.4.

Core also should not own plugin output prompts, provider-specific defaults, or
Agent Component convenience behavior when a lower layer already has a contract
for them. Plugins can import core contracts; core cannot depend on built-in
plugin or Agent Component implementations.

### Plugins And Providers

Plugins implement replaceable backend behavior behind core contracts.

Examples:

- `ExecutionResourceProvider` for Bash, provider-neutral code execution, MCP,
  SQLite, vector stores, browsers, or remote runners; Python and Node.js are
  `CodeRuntimeAdapter` concerns rather than separate providers.
- `ActionExecutor` for one atomic action call.
- `ActionRuntime` for action planning and loop behavior.
- `ActionFlow` for execution strategy.

Provider code owns environment-specific startup, health check, and release. It
does not own app-level decisions about whether an agent should be allowed to use
that environment.

Do not introduce parallel nouns such as `ActionProvider`,
`CapabilityProvider`, or a standalone capability dispatcher for this layer.
Callable ability remains `Action`; execution variation belongs to
`ActionExecutor`; live resource lifecycle belongs to `ExecutionResourceProvider`.

### Built-in Capability Actions

Built-ins are the default capability catalog shipped by Agently. They expose
model-callable operations as Actions and may depend on ExecutionResource.

Good built-in candidates:

- run Bash commands inside a policy-bound workspace
- run Python, Node.js, Go, or C++ through one Workspace-bound CodeExecution
  Action family and ordered execution providers
- search, read, and write files
- search the web and browse pages
- read and write SQLite data
- search and update vector stores
- call pre-registered Python functions
- call MCP tools

Action is the model-visible callable surface. ExecutionResource is only used
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
agent.enable_task_workspace_file_actions(...)
agent.enable_nodejs(...)
agent.enable_code_runtime(...)
agent.enable_sqlite(...)
agent.enable_vector_store(...)
agent.enable_coding_workspace(...)
```

These APIs should describe developer intent. They should not force app
developers to understand `ExecutionResourceHandle`, provider lifecycle, or
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

### Module Organization

Core and builtins capabilities can be implemented as subdirectory packages.
Prefer that shape when a feature has multiple architectural roles, such as a
facade, manager, default implementation, registry, adapter, policy, or
validation layer.

Do not default new core or builtins work to a single file. First judge the
feature's expected submodule volume and ownership boundaries. A single file is
appropriate only when the capability is genuinely small and splitting it would
be over-design.

Landed examples include `core/Action`, `core/TriggerFlow`,
`core/orchestration/TaskDAG`, `core/context`, `core/TaskWorkspace`,
`core/storage`, `builtins/plugins/ExecutionResourceProvider`, and
`core/application/SkillLibrary`. `core/application/SkillsExecutor` is a thin
compatibility facade, not an execution plugin. Keep public imports stable through package
`__init__.py` files and top-level re-exports.

## Action And ExecutionResource

Action and ExecutionResource are separate layers.

Action answers:

- What can the model or agent call?
- What input schema does it use?
- How is one call normalized into `ActionResult`?

ExecutionResource answers:

- What live dependency must exist before execution?
- Is it allowed under policy and approval?
- How is it started, reused, health-checked, scoped, and released?

Not every Action needs ExecutionResource. File policy checks, pure local
functions, and simple stateless operations can be plain Actions. Use
ExecutionResource when lifecycle, isolation, health, credentials, or cleanup matters.

## Skills Boundary

Skills should not be a parallel executor.

A skill package may declare guidance, scripts, MCP assets, hooks, resources, or
workflow templates. The Skills layer should resolve those declarations into a
plan, then apply them to existing Agently layers:

- guidance -> prompt/context
- authorized scripts -> ordinary code-execution Actions bound to an exact Skill
  revision and TaskWorkspace grant
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
