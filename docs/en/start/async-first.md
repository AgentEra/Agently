---
title: Async First
description: When async is the default path, and which APIs to reach for.
keywords: Agently, async, async_get, get_async_generator, async_start
---

# Async First

Agently is async-native at the runtime layer. Internal compatibility methods
cross the boundary through Agently-Stage's `StageCallBridge`; the bridge stays
light unless the owning runtime explicitly requests managed settlement.
Deprecated `FunctionShifter.syncify()` / `asyncify()` now delegate to the
scoped `Stage.as_sync()` / `Stage.as_async()` adapters. For real services,
async should be the default path.

## When sync is fine

- One-off scripts, notebooks, teaching demos.
- Code that doesn't share an event loop with anything else.

Sync compatibility is also valid at an interface you do not own. For example,
a tool provider may intentionally expose a synchronous method even when its
underlying SDK is async:

```python
from agently_stage import Stage


def search(query: str):
    with Stage() as stage:
        return stage.get(search_tool.search, query)
```

Agently-Stage 0.3.8 automatically reuses or selects a physically safe carrier,
including when this method runs inside a synchronous TriggerFlow chunk. The
provider does not need to know that TriggerFlow also uses Stage, and may call
sync execution-data methods such as `data.set_state(...)` afterward. This is a
synchronous boundary and blocks its worker thread; when you own the surrounding
async API, direct `await` and the native async Agently methods remain preferable.
An object that is bound to the caller's event loop cannot be moved to another
carrier; await that work on its owner loop.

## When async is the default

- Inside FastAPI, ASGI workers, SSE / WebSocket handlers, or any code already running in `asyncio`.
- Streaming UI where you want to react to field deltas instead of waiting for the full response.
- Combining model output with TriggerFlow events, runtime stream, or external pubsub.

## Plan dependencies before choosing the execution shape

For a complex AI service or script, do not begin with an all-serial loop. First
map the business stages and mark:

- real data or ordering dependencies that must remain serial;
- independent branches that can run concurrently;
- reversible or idempotent preparation that can use provisional structured progress;
- side effects and external systems that impose safety or capacity limits.

Use async Agently APIs for work that can overlap. Use `instant` when structured
fields should reach a UI or another consumer progressively, then read the final
parsed object with `async_get_data()` and apply configured validation before
durable writes or business decisions. `instant` updates are provisional and
may be invalidated by a retry; they may drive UI state or explicitly
cancelable/idempotent preparation, not irreversible side effects. Use
TriggerFlow `batch(...)`, `for_each(...)`, or signal-driven
`when(...)` + `async_emit(...)` / `async_emit_nowait(...)` to keep application-
owned fan-out, joins, and dependencies visible in the workflow graph.

Serial execution is valid when a real dependency, ordering guarantee,
side-effect safety rule, or external limit requires it. Choosing serial
execution without first doing this dependency analysis is an anti-pattern.

## Expose pressure controls

Production services should expose bounded settings at their actual owner:

| Pressure boundary | Control |
|---|---|
| Service admission | maximum active executions/coroutines and a bounded queue |
| One TriggerFlow execution | `create_execution(concurrency=N)` or `execution.set_concurrency(N)` |
| One fan-out operator | `batch(..., concurrency=N)` or `for_each(concurrency=N)` |
| Model provider | `model_request.scheduler.max_concurrency`, `model_request.scheduler.rate_per_second`, and `model_request.scheduler.providers.<provider>` overrides |
| Blocking I/O SDK | host-owned thread-pool size and queue limit |
| CPU-bound work | host-owned process-pool/worker size and queue limit |

The effective throughput is bounded by all of these layers and the downstream
systems they protect. TriggerFlow does not provide one universal thread-count
setting: thread and process pools belong to the application host when blocking
work must be isolated from the event loop.

## Recommended pairing

The combination worth learning first:

- `result.get_async_generator(type="instant")` — yields structured `StreamingData` patches with `path`, `delta`, `value`, and `is_complete`.
- `data.async_emit(...)` — turns nodes into TriggerFlow signals.
- `data.async_put_into_stream(...)` — forwards intermediate state to UI / SSE / logs.

`instant` events are field-level, not raw provider tokens. They can carry partial
field text in `.delta` while the field is still growing, then emit a completion
event when `.is_complete` becomes true. Treat these events as progressive UI
state; read `async_get_data()` at the end for the durable parsed object.
Annotate these stream handlers with `StreamingData` from `agently` for the
common import path, or from `agently.types.data` when you prefer the full typed
data namespace.

## API surface map

| Sync | Async equivalent |
|---|---|
| `agent.start()` / `request.start()` | `agent.async_start()` / `request.async_start()` |
| `result.get_data()` | `result.async_get_data()` |
| `result.get_text()` | `result.async_get_text()` |
| `result.get_meta()` | `result.async_get_meta()` |
| `result.get_generator(type=...)` | `result.get_async_generator(type=...)` |
| `flow.start()` | `flow.async_start()` |
| `execution.start()` / `execution.close()` | `execution.async_start()` / `execution.async_close()` |
| `data.set_state(...)` / `data.emit(...)` | `data.async_set_state(...)` / `data.async_emit(...)` |
| `agent.add_chat_history(...)` | `await agent.async_add_chat_history(...)` |

## Minimal async example

```python
import asyncio
from agently import Agently

agent = Agently.create_agent()


async def main():
    result = (
        agent
        .input("Give me a title and two bullets.")
        .output({
            "title": (str, "Title", True),
            "items": [(str, "Bullet point", True)],
        })
        .get_result()
    )

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            print(item.path, "+", item.delta)
        if item.is_complete:
            print(item.path, "done")

    final = await result.async_get_data()
    print(final)


asyncio.run(main())
```

`get_result()` returns a reusable `ModelRequestResult`. You can pull text, structured data, and metadata from the same result without re-issuing the request — see [Model Result](../requests/model-response.md).

## Async + TriggerFlow

For event-driven orchestration, prefer:

- `flow.async_start(...)` for a finite, self-closing run when the caller only
  needs the close snapshot; this may be a bounded async request handler.
- `flow.async_start_execution(...)` for explicit, long-lived executions you want to control yourself.
- `data.async_emit(...)` and `data.async_put_into_stream(...)` inside chunks.

See [TriggerFlow Lifecycle](../triggerflow/lifecycle.md).

Use an explicit execution, rather than hidden sugar, when the host needs an
execution handle for pause/resume, external events, save/load, intervention,
inspection, cancellation, runtime-stream disconnect handling, or controlled
close.

## Don't oversell async

Async First improves concurrency, service composition, and progressive UX. It does **not** make a single isolated model request faster. The wall-clock latency of one request is bounded by the model, not by sync vs async.
