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
| Goes into save / load checkpoints | yes | no | no, must be re-injected after `load()` |
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

Reading state is a local sync operation. Writes, appends, and deletes have async variants so async chunks can stay async-first.

Whatever you put in state at the time of `close()` shows up in the close snapshot.

## flow_data — risky shared scope

`flow_data` is shared across **every** execution of the same flow. That sounds convenient until you have:

- Two executions running in parallel — they overwrite each other.
- save/load — the value at save time may not be there at load time on a new process.
- Distributed scheduling — the value lives on whichever process loaded the flow.

Because of this, every call emits a `RuntimeWarning`:

```python
flow.set_flow_data("counter", 0)            # RuntimeWarning
flow.set_flow_data("counter", 0, no_warning=True)   # silenced
```

If you really mean shared scope (read-only config, a long-running cache that all executions are intentionally sharing), pass `no_warning=True`. For execution-local data — which is what 99% of code wants — use `state` instead.

API (each emits the warning unless suppressed):

- `flow.get_flow_data(key)` / `flow.set_flow_data(key, value)` / `flow.append_flow_data(...)` / `flow.del_flow_data(...)`
- async equivalents prefixed with `async_`

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

A close snapshot is supposed to be a serializable dict. Live objects can't survive serialization (no meaningful representation, no way to reconstruct the live state on the other side). What the snapshot **does** record is `resource_keys` and `checkpoint.resource_requirements` — the resource identities needed for rehydration:

```python
flow.declare_resource_requirement("db")
flow.declare_resource_requirement("logger")
flow.declare_resource_requirement("search_tool")

saved = execution.save()
# saved contains state, lifecycle metadata, interrupt state,
# resource requirements, and resource keys, but NOT live objects

restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved,
    runtime_resources={"db": new_db_client, "logger": new_logger, "search_tool": search_function},
)
```

The caller is responsible for re-injecting compatible resources during
rehydration. `load(saved)` remains a low-level compatibility API, but restart
and worker-handoff paths should prefer `async_rehydrate(...)` so missing
resources fail before the execution continues.

### Managed execution resources

`runtime_resources` can also receive managed resources from
`Agently.execution_environment` when you pass `execution_environments=[...]` to
`flow.create_execution(...)`, `flow.start_execution(...)`, or
`flow.async_start(...)`.

Those resources are still read inside chunks through `data.require_resource(...)`.
The difference is ownership: the Execution Environment Manager starts/reuses the
resource and releases it when the execution closes. Manually passed
`runtime_resources={...}` remain unmanaged.

## Decision table

| You're storing | Use |
|---|---|
| A number, string, dict, list, or other JSON-friendly value that the close snapshot should include | `state` |
| A pydantic model, dataclass, or anything serializable to dict | `state` |
| A database client, HTTP client, websocket | `runtime_resources` |
| A function or callback | `runtime_resources` |
| An in-memory cache that should survive across executions of the same flow | `runtime_resources` injected at the flow level (and accept that resources don't survive process restarts unless you re-inject) |
| Configuration shared across executions, intentionally global | `flow_data` with `no_warning=True`, **or** `runtime_resources` if it isn't serializable |

## Common mistakes

- **Putting an SDK client in state.** It either fails to serialize or silently captures a stale snapshot. Use `runtime_resources`.
- **Putting per-execution business data in `flow_data`.** Two concurrent executions clobber each other. Use `state`.
- **Forgetting to re-inject `runtime_resources` after `load()`.** The execution restarts in a state where `require_resource(...)` fails. The save snapshot contains `resource_keys` so you can write a re-injection step that won't drift.

## See also

- [Lifecycle](lifecycle.md) — what `close()` returns
- [Execution Environment](../actions/execution-environment.md) — managed live resource lifecycle
- [Persistence and Blueprint](persistence-and-blueprint.md) — `save` / `load` semantics
- [Compatibility](compatibility.md) — `runtime_data` is the deprecated alias of `state`
