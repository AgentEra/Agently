---
title: Events and Streams
description: TriggerFlow emit, when, and runtime stream.
keywords: Agently, TriggerFlow, emit, when, runtime stream, async_put_into_stream
---

# Events and Streams

> Languages: **English** · [中文](../../cn/triggerflow/events-and-streams.md)

This page covers the two channels directly involved in TriggerFlow execution. **Don't confuse them.**

| Channel | Inside the flow | Outside the flow |
|---|---|---|
| **emit / when** | A chunk emits an event. Other chunks attached via `when(event)` get triggered. | Outside code can also call `execution.async_emit(...)` while the execution is still `open`. |
| **runtime stream** | A chunk pushes items via `put_into_stream(...)`. | Outside code consumes via `execution.get_async_runtime_stream(...)` for live UI / SSE / logging. |

`emit` is for control flow inside the graph. `runtime stream` is for shipping data out.

## emit / when — control flow

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="emit-when")

    async def prepare(data: TriggerFlowRuntimeData):
        await data.async_set_state("flag", "ready")
        await data.async_emit("Prepared", {"flag": "ready"})

    async def route(data: TriggerFlowRuntimeData):
        await data.async_set_state("when_payload", data.input)

    flow.to(prepare)
    flow.when("Prepared").to(route)

    snapshot = await flow.async_start(None)
    print(snapshot["when_payload"])  # {'flag': 'ready'}


asyncio.run(main())
```

Mechanics:

- `data.async_emit(event, payload)` fires an event. The payload becomes the `data.input` of any handler chained from `when(event)`.
- `flow.when("Event").to(handler)` declares a branch attached to that event.
- `data.emit_nowait(event, payload)` is the fire-and-forget sync variant — the chunk doesn't wait for triggered handlers to run before it returns.
- Multiple `when("Event")` branches all fire on the same event.

### Definition safety vs runtime event delivery

Normal Python imports execute a flow module once per process for the same module
name. TriggerFlow's duplicate-definition protection is the second line of
defense: it avoids declaring the same graph edge or generated `when(...)` gate
twice when application code explicitly runs the same `.to(...)` / `.when(...)`
wiring again on the same flow object. It is not runtime event deduplication.

During one execution, every `emit` / `emit_nowait` call is still a business
event. If a chunk emits `Tick` three times, `when("Tick")` should react three
times. This is what makes `emit_nowait(...)` + `when(...)` useful for dynamic
To-Do executors, dependency joins, side branches, and reflection loops.

### Execution-stage event bindings

TriggerFlow can also attach event handlers to one running execution with
`execution.on(...)`. This is an execution overlay, not a definition mutation:
the flow definition and its fingerprint stay static, while one
`TriggerFlowExecution` snapshot records the dynamic bindings and event attempts
created during that run.

Dynamic bindings are for framework-owned orchestration such as TaskBoard card
fan-out, where runnable work items are discovered during execution and each
branch may emit follow-up events before a join/synthesis point. Durable dynamic
bindings must use recoverable handler references. Anonymous closures, coroutine
stacks, sockets, and half-read model streams are not restored after process
restart.

Use `execution.on(...)` when the application or framework owner needs to
add a handler to the current execution without changing the reusable flow
definition:

```python
binding_id = execution.on(
    "CardRequested",
    run_card,
    binding_id="taskboard.run_card",
)
execution.off(binding_id)
```

Event Center remains separate: RuntimeEvent records may observe dynamic event
dispatch and recovery facts, but Event Center does not own control flow.

For multi-dependency joins, use:

```python
flow.when(["done:a", "done:b"], mode="and").to(continue_after_both)
```

The join state belongs to one execution. It must not leak across executions or
be stored in shared flow data.

Events emitted from inside a chunk carry execution correlation metadata and
inherit the current aggregation scope. That keeps framework-owned fan-out, such
as `batch`, `for_each`, and chunk-internal emits, correlated for
`when(..., mode="and")`
joins. External emits that do not share a runtime scope are separate business
events; if a host needs to join externally submitted `A` / `B` events for the
same business item, route them through one scoped flow stage or carry an
explicit correlation key in the payload and branch on it.

### Emitting from outside

While the execution is `open`, outside code can emit too:

```python
await execution.async_emit("UserClicked", {"id": 42})
execution.emit_nowait("UserClicked", {"id": 42})
```

After `seal()` or `close()`, external `emit` calls are rejected.

## Runtime stream — data out

```python
async def main():
    flow = TriggerFlow(name="runtime-stream")

    async def stream_steps(data: TriggerFlowRuntimeData):
        await data.async_put_into_stream("step-1")
        await data.async_put_into_stream("step-2")
        await data.async_set_state("done", True)

    flow.to(stream_steps)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")

    close_task = asyncio.create_task(execution.async_close())
    items = [item async for item in execution.get_async_runtime_stream(timeout=None)]
    snapshot = await close_task

    print(items)        # ['step-1', 'step-2']
    print(snapshot)     # {'done': True}
```

Mechanics:

- `data.async_put_into_stream(item)` pushes one item into the per-execution stream.
- `data.put_into_stream(item)` is the sync variant.
- `execution.get_async_runtime_stream(timeout=...)` yields items as they arrive. The stream closes when the execution closes.
- Sync consumer: `execution.get_runtime_stream(timeout=...)`.
- TriggerFlow also writes fail-open system items for interrupts and runtime intervention. Consumers that only expect business stream items should ignore unknown `type` values.

### Stream timeout vs auto-close timeout

These are independent:

| Timeout | Controls |
|---|---|
| `get_async_runtime_stream(timeout=N)` | how long the consumer waits for the next item before raising / yielding nothing |
| `auto_close_timeout` on the execution | how long the execution waits while idle before auto-closing |

Setting the stream timeout to `None` makes the consumer wait until the stream actually closes (i.e., until `close()` finishes). That's usually what you want when you're collecting all items.

Agently 4.1.4.5 uses an unbounded process-local Agently-Stage Tunnel beneath
this execution-stream API. This is an implementation detail: TriggerFlow still
owns item validation, lazy execution start, timeout warnings, subflow bridging,
and stream close. Each reader has an independent replay cursor, so a late or
parallel reader receives the same accepted items in order. The compatibility
path is deliberately unbounded; it is not backpressure, durable replay,
acknowledgement, or exactly-once delivery. Save/load creates a fresh local
transport and does not serialize Tunnel state.

## Hidden execution sugar for streams

`flow.get_async_runtime_stream(...)` and `flow.get_runtime_stream(...)` create a hidden execution under the hood and stream from it. As with `flow.start()`, this is for finite, self-closing flows (no `pause_for`, no external `emit`) when the caller does not need an execution handle. A bounded streaming request may use it; use `execution.get_async_runtime_stream(...)` on an explicit execution when the host needs disconnect cancellation, save/load, intervention, inspection, external events, or controlled close. If a hidden stream execution reaches `pause_for(...)`, TriggerFlow fails fast because there is no resumable handle.

## Don't put live items in state

Big or live items belong in the runtime stream, not state. State is for the eventual close snapshot — it should be small and serializable. Streaming through `put_into_stream` lets the consumer process each item as it arrives without bloating the snapshot.

## Observation events are not this control-flow channel

Agently also emits **observation events** through the Event Center, for example TriggerFlow lifecycle events, Session application events, and observation logs. That is a framework-level observation channel, not `emit` / `when` control flow and not runtime stream data. See [Event Center](../observability/event-center.md).

## See also

- [Patterns](patterns.md) — `when` is one of several flow-control primitives
- [Pause and Resume](pause-and-resume.md) — `continue_with(interrupt_id, payload)` is the resume path, separate from `emit`
- [Runtime Intervention](runtime-intervention.md) — guidance context inserted at safe boundaries
- [Lifecycle](lifecycle.md) — what `close()` does to the runtime stream
- [Event Center](../observability/event-center.md) — framework-level observation events
