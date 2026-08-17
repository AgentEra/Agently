---
title: Execution Resource
description: Managed execution resources for Actions and TriggerFlow.
keywords: Agently, ExecutionResource, Action, TriggerFlow, sandbox, MCP, runtime_resources
---

# Execution Resource

> Languages: **English** · [中文](../../cn/actions/execution-environment.md)

> Renamed in the 4.1.3.8 TaskWorkspace/ActionRuntime boundary refactor: the managed
> live-resource seam is now **ExecutionResource** (`ExecutionResourceManager`,
> `ExecutionResourceProvider`, `Agently.execution_resource`). The previous
> `ExecutionEnvironment*` names are removed. This page keeps its URL for link
> stability.

Execution Resource is the framework-level layer that prepares and releases
managed execution resources before an action or workflow step runs.

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
- writing an `ExecutionResourceProvider` plugin
- reviewing how Action or TriggerFlow receives managed resources
- designing a new built-in capability that needs sandbox, process, MCP, client,
  credential, or cleanup lifecycle

Do not expose `Agently.execution_resource` as the default app-development
mental model. It is the core lifecycle layer behind higher-level capabilities.

## Where it sits

```text
Agent Component / built-in Action / custom Action / TriggerFlow / Skills plan
        |
        v
ActionSpec.execution_resources or TriggerFlow execution requirements
        |
        v
ExecutionResourceManager
        |
        v
ExecutionResourceProvider
        |
        v
managed handle / live resource
```

V1 exposes the global manager as:

```python
from agently import Agently

Agently.execution_resource
```

Most application code does not call the manager directly. Built-in MCP, Bash,
code-execution, Docker, Browser, and SQLite actions can declare their
requirements and the Action dispatcher ensures them before executor calls.

For the broader ownership model, see
[Architecture / Extension Boundaries](../architecture/extension-boundaries.md).

## Built-in behavior

The built-in providers are:

| Kind | Used by | Managed resource |
|---|---|---|
| `mcp` | `agent.use_mcp(...)` / MCP actions | MCP transport resource |
| `bash` | `sandbox="trusted_local"` shell actions | configured local command runner |
| `docker` | isolated shell actions, direct Docker Actions, and one `code_execution` provider candidate | Docker CLI runner and image provisioning |
| `code_execution` | `agent.enable_python(...)`, `agent.enable_nodejs(...)`, `agent.enable_code_runtime(...)`, and authorized Skill script Actions | provider-neutral Workspace-bound execution; built-ins include Docker, optional gVisor/runsc, optional macOS Seatbelt, and the explicit unsafe `trusted_local` fallback |
| `browser` | Browse actions that opt into managed browser resources | managed browser/page/session wrapper |
| `sqlite` | `agent.enable_sqlite(...)` / SQLite executor actions | SQLite connection |

Search intentionally is not listed here. It is a stateless Action-native
capability package; proxy, timeout, backend, and region belong to the Search
package/executor configuration rather than ExecutionResource.

These providers are low-level environment implementations. User-facing
capabilities should normally be exposed as Actions, and scenario shortcuts
should be exposed through Agent Components or future `agent.enable_*` helpers.
Python and Node.js helpers are language-specific facades over the same
`kind="code_execution"` contract used by `enable_code_runtime(...)`; they are
not separate providers. Python, Node.js, Go, and C++ differ through language
adapters, while provider probes select the first installed and eligible
configured execution mechanism. Hard isolation and Workspace capability checks
still fail closed, and `trusted_local` remains explicitly unsafe.

Action execution flow:

```text
ActionCall
  -> resolve ActionSpec
  -> issue TaskWorkspace access grant when required
  -> probe/select and ensure ActionSpec.execution_resources
  -> inject execution_resource_resources into action_call
  -> materialize immutable code bundle into TaskWorkspace
  -> ActionExecutor.execute(...)
  -> collect declared outputs
  -> release action_call-scoped handles
  -> close Workspace grant
```

Custom `ActionExecutor.execute(...)` signatures do not change. Managed handles
are passed through `action_call["execution_resource_handles"]` and live
resources through `action_call["execution_resource_resources"]`.

### Ordered code-execution providers

Configure provider priority with strings or candidate descriptors. Descriptor
configuration is merged only for that candidate:

```python
agent.settings.set(
    "code_execution.providers",
    [
        {"provider_id": "preferred-provider", "config": {"profile": "strict"}},
        "docker",
    ],
)
agent.enable_code_runtime(language="go")
```

`trusted_local` executes host toolchains without isolation and accepts only a
snapshot grant. It requires explicit host authorization and cannot satisfy
`isolation="required"`. `unsafe_fallback=True` must therefore be paired with an
explicit `isolation="preferred"` or `"none"`; it is never selected implicitly.

The public `isolation=` option is selection policy, not a provider capability
label. A `code_execution` provider must report concrete boolean isolation axes:
process containment, host-filesystem restriction, privilege-escalation
blocking, and syscall restriction. Required isolation matches all requested
axes. Preferred isolation first searches the ordered candidate set for a full
match and only then uses an eligible fallback, recording that fallback in the
handle metadata. A provider name or a string such as `"required"` is not safety
evidence.

### gVisor Docker runtime

Use the explicit `gvisor` sandbox when the host Docker daemon is configured
with the `runsc` runtime:

```python
agent.enable_python(sandbox="gvisor")
```

This chooses only the optional `gvisor` provider. Before a handle is ready,
Agently verifies Docker availability, requires a registered `runsc` runtime,
prepares the selected image under the configured image policy, and starts a
bounded `runsc` container probe. A missing, malformed, unregistered, or
non-executable runtime fails closed; this explicit choice never falls back to
Docker/runc, `auto`, or `trusted_local`. The verified active runtime is retained
in handle and code-execution result metadata. gVisor adds no default import
dependency and does not make unsafe Docker arguments stronger safety evidence.

### macOS Seatbelt

On macOS, explicitly select the optional Seatbelt provider with:

```python
agent.enable_python(sandbox="seatbelt")
```

Seatbelt uses the system `sandbox-exec` mechanism, denies network access by
default, and derives every writable filesystem rule from the TaskWorkspace
grant. It never accepts raw SBPL rules or additional host write paths and never
falls back to Docker or `trusted_local`. To keep host toolchains and dynamic
libraries usable, this initial profile permits broad host reads; it therefore
reports `host_filesystem_restricted=false` and the helper records preferred,
not required, isolation. Use Docker/gVisor when host-read isolation is required.

Code requests declare at most 128 expected outputs. Each path is bounded,
normalized, and must be under `output/`; missing declared outputs make the
Action fail. Stdout/stderr retention is bounded, cancellation terminates the
owned process group or container, and resource-release failure changes an
otherwise successful Action into an error instead of reporting false success.

External isolation implementations register through the same
`ExecutionResourceProvider` seam. See
[Code Execution Provider Migration](../development/code-execution-provider-migration.md).

## TriggerFlow

TriggerFlow still uses `runtime_resources` as the compatibility surface for
live execution-local resources. ExecutionResource does not rename or replace
that API.

You can pass managed requirements at execution creation or start:

```python
execution = flow.create_execution(
    execution_resources=[
        {
            "kind": "custom_runtime",
            "provider_id": "my-runtime-provider",
            "scope": "execution",
            "resource_key": "runtime",
        }
    ],
)
```

After the host registers the named provider, the manager ensures the resource,
injects it into the execution-local resources, and releases it when the
execution closes. Code execution itself should still be invoked through a
Workspace-bound CodeExecution Action so bundle materialization and output
readback remain in the Action Runtime chain. Manual `runtime_resources={...}`
are unmanaged and are not health-checked or auto-released by the manager.

## Direct manager API

This API is for framework, action, and plugin developers.

The manager supports:

```python
Agently.execution_resource.declare(requirement)
Agently.execution_resource.ensure(requirement_or_id)
await Agently.execution_resource.async_ensure(requirement_or_id)
Agently.execution_resource.release(handle_or_id)
Agently.execution_resource.release_scope("session", owner_id)
Agently.execution_resource.inspect(id)
Agently.execution_resource.list(scope="execution")
Agently.policy_approval.register_handler("my_handler", handler)
Agently.configure_policy_approval(handler="my_handler")
Agently.set_settings("access_control_policy.auto_allow", True)
```

Declaration is lazy. It validates and records a requirement but does not start
anything. `ensure(...)` starts or reuses a handle subject to policy and approval.
Approval is resolved through the framework-wide `Agently.policy_approval`
handler. The default `input_timeout_fail` handler prompts only in an interactive
CLI and denies after timeout or immediately in non-interactive services. Service
wrappers around TriggerFlow executions should register their own handler, for
example one that stores a pending approval and resumes with `continue_with(...)`.
Trusted hosts can set `access_control_policy.auto_allow=True` through settings
to approve policy gates automatically; this does not bypass provider, sandbox,
path, command, or network constraints encoded in the requirement policy.
Before reusing a ready handle, the manager calls
`provider.async_health_check(handle)`. Healthy handles are reused with
`ref_count + 1`; unhealthy handles emit `execution_resource.unhealthy`, are
released, and then a fresh handle is ensured. V2 intentionally does not add a
background scheduler, lease TTL, or automatic reconnect loop.

If you are building an application, first check whether a built-in action or
Agent Component already exposes the capability you need.

## Observation

The manager emits framework events in the `execution_resource.*` family:

- `execution_resource.declared`
- `execution_resource.approval_required`
- `execution_resource.ensuring`
- `execution_resource.ready`
- `execution_resource.unhealthy`
- `execution_resource.releasing`
- `execution_resource.released`
- `execution_resource.failed`

Payloads include stable ids and status metadata only. They must not include raw
credentials, environment variables, command secrets, or live resource objects.

## Examples

Runnable examples are available in
[`examples/execution_resource`](../../../examples/execution_resource/README.md).
Start with the trusted-local `agent.enable_python(..., sandbox="trusted_local")`
quickstart when no Docker service is available, then move to the Docker-backed
Ollama, DeepSeek, and common-language code runtime examples. The TriggerFlow
example is intended for workflow or framework developers who need managed
execution-local resources.

## See also

- [Action Runtime](action-runtime.md)
- [MCP](mcp.md)
- [TriggerFlow State and Resources](../triggerflow/state-and-resources.md)
