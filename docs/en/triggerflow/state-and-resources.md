---
title: State and Resources
description: Three storage layers — state, flow_data, and runtime_resources — and when to use each.
keywords: Agently, TriggerFlow, state, flow_data, runtime_resources, snapshot, save, load
---

# State and Resources

> Languages: **English** · [中文](../../cn/triggerflow/state-and-resources.md)

A TriggerFlow execution carries three distinct storage layers. They look similar but solve different problems. Mixing them is a common source of subtle bugs.

## Three layers at a glance

| | `state` | `flow_data` | `runtime_resources` |
|---|---|---|---|
| Scope | execution-local | flow-shared (across all executions) | execution-local |
| Serializable | yes | yes | **no** |
| Goes into close snapshot | yes | no | no, only `resource_keys` recorded |
| Goes into execution snapshots | yes | **yes, as one flow-shared copy** | no, must be re-injected after `load()` |
| Recommended for | business state, intermediate values, anything you want back from `close()` | legacy compatibility / explicitly intentional flow-wide sharing | live clients, sockets, callbacks, file handles, cache references |
| Status | **recommended primary path** | risky-default — emits `RuntimeWarning` on every call | new concept — use this for anything that can't be serialized |

## state — the main path

State is execution-local, serializable, and snapshot-safe. It's what populates the close snapshot and what `save()` / `load()` round-trip.

```python
async def step(data: TriggerFlowRuntimeData):
    await data.async_set_state("greeting", f"hello {data.input}")
    current = data.get_state("greeting")
```

API:

- `data.async_set_state(key, value)` / `data.set_state(key, value)`
- `data.get_state(key, default=None)`
- `data.async_append_state(key, value)` / `data.append_state(key, value)` — for list-valued state
- `data.async_del_state(key)` / `data.del_state(key)`

Reading state is a local sync operation. Async chunks must await the async
write, append, delete, emit, and stream methods so execution stays on its owner
loop. Sync facades remain compatible from other contexts, but they block the
calling thread and are intended for sync chunks and sync callers.

A sync chunk may call a provider-owned synchronous wrapper that internally
uses `with Stage()` to await an async SDK, then continue with
`data.set_state(...)`. Agently-Stage 0.3.8 detects the surrounding physical
runtime automatically; providers do not need to discover TriggerFlow's private
Stage usage or redesign their public method as async.

`set_state(...)` is replacement, including for lists, mappings, sets, and empty
collections. Use `append_state(...)` only when list accumulation is intended.
For a mapping transition, compute the complete next mapping and set it; stale
keys are not merged into the new value. A single set mutation completes before
its event notification is dispatched, but a multi-call read-modify-write
sequence is not a compare-and-swap transaction and still needs host-owned
coordination when writers race.

Whatever you put in state at the time of `close()` shows up in the close snapshot.

## flow_data — risky shared scope

`flow_data` is shared across **every** execution of the same flow. That sounds convenient until you have:

- Two executions running in parallel — they overwrite each other.
- save/load — `save()` captures the current flow-shared value and `load()`
  replaces the target flow object's current shared value, so restoring one
  execution can affect every other execution using that flow object.
- Distributed scheduling — the value lives on whichever process loaded the flow.

Because of this, every call emits a `RuntimeWarning`:

```python
flow.set_flow_data("counter", 0)            # RuntimeWarning
flow.set_flow_data("counter", 0, no_warning=True)   # silenced
```

If you really mean shared scope (read-only config, a long-running cache that all executions are intentionally sharing), pass `no_warning=True`. For execution-local data — which is what 99% of code wants — use `state` instead.

`flow_data` is serialized in an execution save snapshot for compatibility, but
it is not an execution-local snapshot. Loading that snapshot clears and restores
the owning flow object's shared `flow_data`. This can overwrite newer values or
interfere with concurrent executions; save/load does not add isolation, CAS, or
merge semantics. Do not use `flow_data` as a recovery boundary. Put per-run
state in execution `state`, and put durable shared state in a host/RecordStore
provider with the required consistency policy.

API (each emits the warning unless suppressed):

- `flow.get_flow_data(key)` / `flow.set_flow_data(key, value)` / `flow.append_flow_data(...)` / `flow.del_flow_data(...)`
- async equivalents prefixed with `async_`

`set_flow_data(...)` also replaces the complete target value; append remains an
explicit separate operation. This does not make the shared scope concurrency-safe.

## runtime_resources — live objects

Some things can't go into state because they can't be serialized: database clients, callback functions, sockets, in-memory caches, anything with a file descriptor or live network connection. Those live in `runtime_resources`.

Inject at execution creation:

```python
execution = flow.create_execution(
    runtime_resources={
        "db": my_db_client,
        "logger": my_logger,
        "search_tool": search_function,
    },
)
```

Or update on the flow itself (default for all executions of that flow):

```python
flow.update_runtime_resources(logger=my_logger)
```

Inside a chunk:

```python
async def step(data: TriggerFlowRuntimeData):
    logger = data.require_resource("logger")
    logger.info(f"received: {data.input}")
    db = data.require_resource("db")
    rows = await db.fetch("SELECT 1")
```

`require_resource(name)` raises if the resource isn't injected — use it when the chunk genuinely depends on the resource. There's also `data.get_resource(name, default=None)` for optional cases.

### Why resources don't enter the snapshot

A close snapshot is supposed to be a serializable dict. Live objects can't survive serialization (no meaningful representation, no way to reconstruct the live state on the other side). What the snapshot **does** record is `resource_keys` and `resource_requirements` — the resource identities needed for load:

```python
flow.declare_resource_requirement("db")
flow.declare_resource_requirement("logger")
flow.declare_resource_requirement("search_tool")

saved = execution.save()
# saved contains state, lifecycle metadata, interrupt state,
# resource requirements, and resource keys, but NOT live objects

restored = flow.create_execution(auto_close=False)
await restored.async_load(
    saved,
    runtime_resources={"db": new_db_client, "logger": new_logger, "search_tool": search_function},
)
```

The caller is responsible for re-injecting required resources during load.
Use `load(saved)` when those resources are already available in the current
process. Use `async_load(...)` for restart and worker-handoff paths so missing
resources fail before the execution continues.

For distributed pause/resume, re-injection is not enough when the resource
carries state. A recreated HTTP client can be equivalent to the old one, but a
browser page, sandbox process, remote task, or exchange session may need a
provider-owned state ref, version, lease, or fence token. Store those refs in
execution state or resource requirements, and let the external system restore
and validate the live object before TriggerFlow continues.

For service deployments where every worker can import the same factory, declare
an importable resolver descriptor and let `async_load(...)` rebuild the
live object:

```python
flow.declare_resource_requirement(
    "db",
    resolver="my_app.resources:create_db",
    provider_kind="database",
    config_ref="settings://db",
    secret_ref="secret://db",
)
```

Resolvers receive a context dictionary and return either the live object or
`{"resource": object, "health": "healthy"}`. Missing, unhealthy, and
policy-forbidden resources are surfaced in `inspect_load(...)`
diagnostics; `fail_policy="fail_open"` turns a blocking resolver problem into a
warning, while the default `fail_closed` blocks strict load.

### Managed execution resources

`runtime_resources` can also receive managed resources from
`Agently.execution_resource` when you pass `execution_resources=[...]` to
`flow.create_execution(...)`, `flow.start_execution(...)`, or
`flow.async_start(...)`.

Those resources are still read inside chunks through `data.require_resource(...)`.
The difference is ownership: the ExecutionResourceManager starts/reuses the
resource and releases it when the execution closes. Manually passed
`runtime_resources={...}` remain unmanaged.

## Decision table

| You're storing | Use |
|---|---|
| A number, string, dict, list, or other JSON-friendly value that the close snapshot should include | `state` |
| A pydantic model, dataclass, or anything serializable to dict | `state` |
| A database client, HTTP client, websocket | `runtime_resources` |
| A function or callback | `runtime_resources` |
| An in-memory cache that should survive across executions of the same flow | `runtime_resources` injected at the flow level (and accept that resources don't survive process restarts unless you re-inject or externalize the cache state) |
| A stateful session that must survive worker handoff | `runtime_resources` plus a durable external state ref and resolver/provider validation |
| Configuration shared across executions, intentionally global | `flow_data` with `no_warning=True`, **or** `runtime_resources` if it isn't serializable |

## Common mistakes

- **Putting an SDK client in state.** It either fails to serialize or silently captures a stale snapshot. Use `runtime_resources`.
- **Putting per-execution business data in `flow_data`.** Two concurrent executions clobber each other. Use `state`.
- **Forgetting to re-inject `runtime_resources` after `load()`.** The execution restarts in a state where `require_resource(...)` fails. The save snapshot contains `resource_keys` so you can write a re-injection step that won't drift.
- **Treating a stateful resource as recovered because the key exists.** Key presence only proves that a live object was mounted. The external system still has to restore and validate any state that object carries.

## See also

- [Lifecycle](lifecycle.md) — what `close()` returns
- [ExecutionResource](../actions/execution-environment.md) — managed live resource lifecycle
- [Persistence and Blueprint](persistence-and-blueprint.md) — `save` / `load` semantics
- [Distributed Pause and Resume Boundaries](distributed-pause-resume.md) — host-managed recovery and live object ownership
- [Compatibility](compatibility.md) — `runtime_data` is the deprecated alias of `state`
