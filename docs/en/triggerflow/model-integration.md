---
title: Model Integration
description: Calling agents and model requests from inside TriggerFlow chunks.
keywords: Agently, TriggerFlow, agent, model, request, async, instant
---

# Model Integration

> Languages: **English** · [中文](../../cn/triggerflow/model-integration.md)

A chunk handler is a regular async function. You can call any agent, request, or response API inside it. The good patterns concentrate on three things: async (because the surrounding flow is async), structured output (because the next chunk expects a known shape), and streaming when the user actually benefits.

## Minimal pattern

```python
from agently import Agently, TriggerFlow, TriggerFlowRuntimeData

agent = Agently.create_agent()


async def classify(data: TriggerFlowRuntimeData):
    result = await (
        agent
        .input(data.input)
        .output({
            "category": (str, "Category", True),
            "confidence": (float, "0.0 to 1.0"),
        })
        .async_start()
    )
    await data.async_set_state("classification", result)
    return result


flow = TriggerFlow(name="classify")
flow.to(classify)
```

The agent is created at module scope so it's reused across executions. `await ... async_start()` returns the parsed dict. The dict goes into state for the close snapshot, and is also returned so the next chunk receives it as `data.input`.

## Use async, always

The surrounding flow is async. Calling sync `start()` inside a chunk works but blocks the event loop while the model request is in flight, hurting concurrency. Use `async_start()` / `async_get_data()` / `get_async_generator(...)`. See [Async First](../start/async-first.md).

## Make new-information boundaries visible

A later field may stay in the same ModelRequest when it uses only the
request-time input snapshot and earlier bounded fields from that response.
When a later model stage needs a fact produced after dispatch, represent the
required serial value edge explicitly:

```text
R1 -> Action/system work -> host validation/readback -> R2
```

This includes retrieval results, API/database responses, file or artifact
readback, approval/resume payloads, and deterministic calculations that the
later semantic step must inspect. A high-level Agent execution may manage this
loop for you, but the logical model topology still crosses separate requests.

`instant` can start cancelable/idempotent work before R1 finishes and overlap it
with independent generation. It is not a back-channel into R1. If R2 needs the
new result, keep consuming R1, reconcile its final accepted trigger set, await
and validate the work, and only then dispatch R2. Independent branches can
still fan out concurrently around this required join.

## Streaming structured fields into the runtime stream

When the UI consuming the runtime stream benefits from incremental updates,
bridge structured field patches from the model result into the TriggerFlow
runtime stream:

```python
async def draft_with_streaming(data: TriggerFlowRuntimeData):
    result = (
        agent
        .input(data.input)
        .output({
            "title": (str, "Title", True),
            "body": (str, "Body", True),
        })
        .get_result()
    )

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            await data.async_put_into_stream({
                "path": item.path,
                "delta": item.delta,
                "done": item.is_complete,
            })

    final = await result.async_get_data()
    await data.async_set_state("draft", final)
    return final
```

`type="instant"` yields structured `StreamingData` patches, not raw provider
tokens. Consumers can render `title` deltas while `body` is still generating.
After the stream ends, `async_get_data()` returns the cached final parsed dict
from the same result (no second request).

Once a declared target field has emitted a complete instant event, Agently marks
that field as observed for the request lifecycle. A later schema/ensure/validator
failure does not replay the provider to repair that same request. The final
validated result remains authoritative: the request can still fail, and the
framework preserves its error, usage, and metadata with
`instant_retry_suppressed` and `instant_complete_paths`. This rule applies to
the AgentExecution structured stream as well; before any target field completes,
the normal retry contract remains available. Treat instant values as provisional
UI or idempotent preparation and use the final result for side effects.

## Overlap generation with downstream fan-out

When a complete early field can start independent retrieval or preparation,
prefer a TriggerFlow-visible start/cache/reconcile pattern:

- put compact trigger items before long progress prose and the final artifact;
- on a complete item, derive a host-owned payload key and call
  `await data.async_emit_nowait(...)` once;
- continue the instant loop so later items and progress are not blocked;
- after `async_get_data()` returns, reconcile the accepted final item set:
  reuse matches, emit missing items, and cancel or discard extras;
- join only accepted keys before committing the final state.

The provisional branch must be read-only or otherwise idempotent/cancelable.
Execution `concurrency`, model/provider limits, and external-adapter quotas own
pressure independently. Raw parser paths should be translated into stable
workflow statuses and events rather than exposed to the frontend.

## Reusing one result across the chunk

Call `get_result()` once, then read text + data + meta from `result` without re-issuing. See [Model Result](../requests/model-response.md):

```python
async def step(data):
    result = agent.input(data.input).output({...}).get_result()
    text = await result.async_get_text()
    obj = await result.async_get_data()
    meta = await result.async_get_meta()
    await data.async_set_state("text", text)
    await data.async_set_state("obj", obj)
    await data.async_set_state("meta", meta)
```

## Per-execution agent customization

If the flow's chunks need different model configuration per execution, inject the configured agent via runtime resources:

```python
execution = flow.create_execution(
    runtime_resources={"agent": Agently.create_agent().set_settings(...)},
)


async def step(data):
    agent = data.require_resource("agent")
    return await agent.input(data.input).async_start()
```

Don't put the agent in `state` — agents hold network clients and aren't snapshot-friendly. Use `runtime_resources` (see [State and Resources](state-and-resources.md)).

## Validation, retries, and structured output

`.validate(...)` and `ensure_keys` work the same way inside a chunk as they do at the request layer. The retry budget is per-request, so a chunk that needs to retry the model call doesn't affect the rest of the flow. See [Output Control](../requests/output-control.md).

```python
async def step(data):
    return await (
        agent
        .input(data.input)
        .output({"answer": (str, "answer", True)})
        .validate(custom_business_check)
        .async_start(max_retries=5)
    )
```

## Don't put model state in flow_data

`flow_data` is shared across all executions of the flow and emits a warning. Don't use it to "remember the last model answer" — use `state` for execution-local memory, or a real session if it's a multi-turn conversation. See [Session Memory](../requests/session-memory.md).

## Multi-agent inside one flow

Multiple chunks can use multiple agents — different model providers, different prompt configurations, different tool sets:

```python
classifier = Agently.create_agent().set_settings("OpenAICompatible", {"model": "${ENV.CLASSIFIER_MODEL}"})
writer = Agently.create_agent().set_settings("OpenAICompatible", {"model": "${ENV.WRITER_MODEL}"})

async def classify(data):
    return await classifier.input(data.input).output({...}).async_start()

async def draft(data):
    return await writer.input(data.input).async_start()

flow.to(classify).to(draft)
```

This is how TriggerFlow plays the orchestration role: the flow keeps the wiring; each agent stays a small, focused unit.

## See also

- [Async First](../start/async-first.md) — why every chunk should use async APIs
- [Model Result](../requests/model-response.md) — `get_result()` and the result cache
- [Output Control](../requests/output-control.md) — validate / retry behavior inside a chunk
- [State and Resources](state-and-resources.md) — where the agent should live
