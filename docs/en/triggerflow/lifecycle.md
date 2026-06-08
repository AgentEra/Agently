---
title: TriggerFlow Lifecycle
description: Three execution states and the five entry APIs — what each one does and when to choose it.
keywords: Agently, TriggerFlow, lifecycle, seal, close, start, execution, auto_close
---

# Lifecycle

> Languages: **English** · [中文](../../cn/triggerflow/lifecycle.md)

A TriggerFlow execution moves through three states. Five entry APIs let you control how it starts and ends.

## Three states

```text
   open  ──seal()──►  sealed  ──close()──►  closed
    │                                            │
    └─── (auto_close fires after idle timeout) ──┘
```

| State | What it accepts | What still runs |
|---|---|---|
| `open` | new external events (`emit`, `continue_with`) | everything: chunks, runtime stream, registered tasks |
| `sealed` | nothing new from outside | already-accepted events, internal `emit` chains, registered tasks continue to drain |
| `closed` | nothing | runtime stream is closed; close snapshot is frozen |

Key distinction: `seal()` stops external input but lets in-flight work finish. `close()` does seal first, then drains and freezes.

## Five entry APIs

| API | Purpose | Returns |
|---|---|---|
| `flow.start(...)` / `flow.async_start(...)` | hidden-execution sugar; create + start + wait + close | close snapshot |
| `flow.start_execution(...)` / `flow.async_start_execution(...)` | explicit launch; you keep the execution handle | execution |
| `execution.start(...)` / `execution.async_start(...)` | start an execution you already created | close snapshot if `auto_close=True`; execution if `auto_close=False` |
| `execution.seal()` / `execution.async_seal()` | runtime seal | — |
| `execution.close()` / `execution.async_close()` | finalize | close snapshot |

### `flow.start(...)` — hidden sugar

```python
snapshot = await flow.async_start("input value")
```

What it does internally: `create_execution(auto_close=True, auto_close_timeout=0.0)`, start, wait until close, return snapshot.

Rules:

- **`auto_close=False` is illegal here** — raises immediately.
- `wait_for_result=` value is **ignored** with a warning. Return type is fixed to the close snapshot.
- `timeout=` is treated as `auto_close_timeout` — how long to wait after the last activity before auto-closing.
- If your flow uses `pause_for(...)`, do **not** use `flow.start()` — there is no handle for the outside to resume against. TriggerFlow fails fast when hidden execution sugar reaches `pause_for(...)`. Use `flow.start_execution(...)` or `flow.create_execution(...)`.

### `flow.start_execution(...)` — explicit launch

```python
execution = await flow.async_start_execution("input value")
# ... do something with the handle ...
snapshot = await execution.async_close()
```

Returns the execution. You decide when to close. Suited for services, SSE/WebSocket streams, human-in-the-loop, external `emit()` callers.

`wait_for_result=` is ignored here too.

### `execution.start(...)` — start a pre-built execution

```python
execution = flow.create_execution(auto_close=True)
snapshot = await execution.async_start("input")  # returns close snapshot
```

```python
execution = flow.create_execution(auto_close=False)
exec2 = await execution.async_start("input")  # returns the execution
# ... do work, then ...
snapshot = await execution.async_close()
```

| `auto_close` | `async_start` returns |
|---|---|
| `True` (default) | close snapshot |
| `False` | the execution itself |

Sync `start()` only supports `auto_close=True`. If your execution must be manually closed, use `await execution.async_start(...)` instead.

The value passed to `execution.async_start(value)` is the execution's start
input. It does not emit a custom event named by that value. Attach chunks that
should run from the start boundary with `flow.to(handler, name=...)`. If you
want a custom event such as `"start"`, start the execution and then call
`await execution.async_emit("start", payload)`.

### `execution.seal()` — stop new input, let in-flight finish

```python
await execution.async_seal()
```

After seal:

- New external `emit()` / `continue_with()` calls are rejected.
- Already-accepted events, internal `emit` chains, and registered tasks keep running.
- Runtime stream is **not** closed.
- Close snapshot is **not** frozen yet.

Use seal when you want to stop accepting new work but still finish what's in flight, and you'll close later (or let `auto_close` close it).

### `execution.close()` — finalize and return snapshot

```python
snapshot = await execution.async_close()
```

What close does, in order:

1. seal (if not already sealed)
2. drain pending tasks
3. close the runtime stream
4. freeze and return the close snapshot

`timeout=` on close is the **drain timeout** — the maximum wait for in-flight tasks before forcing the close. It is not the auto-close timer.

## auto_close and auto_close_timeout

`auto_close=True` (the default for `create_execution`) means the execution will close itself after `auto_close_timeout` seconds of being **idle** — no chunks running, no events to process, no pending pause.

| Source | Default `auto_close_timeout` |
|---|---|
| `flow.create_execution(...)` | `10.0` seconds |
| `flow.start(...)` / `flow.async_start(...)` (hidden sugar) | `0.0` seconds (close as soon as idle) |

`pause_for(...)` pauses the auto-close timer. After `continue_with(...)`, the idle timer starts fresh.

`close()` / `async_close()` reject pending interrupts by default. Resume them first, or explicitly cancel them with `pending_interrupts="cancel"` when shutdown should abandon the wait.

Close also releases execution-local transient aggregation state such as partial
`when(mode="and")`, `batch`, `collect`, `for_each`, and `match` bookkeeping.
These scratch keys are not part of the durable close snapshot.

`auto_close_timeout=None` disables auto-close — the execution stays alive until you call `close()` explicitly. **Don't combine `auto_close_timeout=None` with hidden sugar** — `flow.start()` would never return.

## Save/load checkpoint shape

`execution.save()` returns a serializable execution state dictionary. For
restart-safe and future distributed recovery paths, that dictionary includes a
`checkpoint` section:

```python
saved = execution.save()
checkpoint = saved["checkpoint"]
```

The checkpoint section records:

- `schema_version`: the checkpoint schema version.
- `durable_system_state`: TriggerFlow-owned progress that must survive
  open/waiting execution rehydration, such as partial `when(mode="and")`
  aggregation state.
- `resource_requirements`: the live resources that must be re-injected before a
  restored execution can safely continue.

Live resource objects are not serialized. `runtime_resources`, managed
execution-environment handles, clients, callbacks, and other live objects remain
outside the saved state. The checkpoint only records the requirement keys; pass
fresh objects through `load(..., runtime_resources={...})` or the host's normal
resource provisioning path before resuming.

This is an execution snapshot contract, not a complete distributed execution
store. A production distributed runner still needs storage, lease ownership,
idempotent resume, access control, and resource provisioning around this saved
state.

## Picking the right entry

| Situation | Use |
|---|---|
| Quick script, all inputs known up front | `flow.start(...)` / `flow.async_start(...)` |
| Service that needs to keep emitting / consuming runtime stream | `flow.start_execution(...)` |
| Need `pause_for(...)` (human approval, async webhook) | `flow.create_execution(auto_close=False)` + `execution.async_start(...)` + manual `close()` |
| Need to save and resume across restarts | `create_execution(...)` + `execution.save()` / `load()` |

## A quick decision example

```python
# This flow pauses for user input — DO NOT use flow.start()
flow = TriggerFlow(name="approval")
async def ask(data):
    return await data.async_pause_for(type="approval", resume_to="next")
async def commit(data):
    await data.async_set_state("approved", data.input)
flow.to(ask).to(commit)

execution = flow.create_execution(auto_close=False)
await execution.async_start(None)
# ... wait for an external system to call execution.async_continue_with(...) ...
snapshot = await execution.async_close()
```

If you'd written `await flow.async_start(None)` instead, TriggerFlow would raise when `pause_for(...)` is reached because the hidden execution has no resumable handle.

If you need to stop a waiting execution without resuming it, make that explicit:

```python
snapshot = await execution.async_close(pending_interrupts="cancel")
```

## Compatibility parameters

| Parameter | Status |
|---|---|
| `wait_for_result=True` / `False` | **value is ignored**, warning emitted; return type is governed by `auto_close` |
| `set_result()` / `get_result()` / `.end()` | deprecated; see [Compatibility](compatibility.md) |
| `runtime_data` (`get_runtime_data` / `set_runtime_data` etc.) | deprecated alias of `state`; see [State and Resources](state-and-resources.md) |

## See also

- [State and Resources](state-and-resources.md) — what makes it into the snapshot
- [Execution Result](execution-result.md) — reading snapshots, state, compatibility results, and metadata through one facade
- [Pause and Resume](pause-and-resume.md) — `pause_for` and `continue_with`
- [Persistence and Blueprint](persistence-and-blueprint.md) — `save` / `load`
- [Compatibility](compatibility.md) — migration from older APIs
