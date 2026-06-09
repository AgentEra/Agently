---
title: Pause and Resume
description: Suspending a chunk for human input or external events with pause_for, continuing with continue_with.
keywords: Agently, TriggerFlow, pause_for, continue_with, interrupt, human-in-the-loop
---

# Pause and Resume

> Languages: **English** · [中文](../../cn/triggerflow/pause-and-resume.md)

`pause_for(...)` lets a chunk stop at a durable interrupt barrier while it waits for an external event. The execution stays alive but idle. Auto-close is paused while a `pause_for` is outstanding. When the outside calls `continue_with(...)`, TriggerFlow resumes the graph according to the interrupt's resume target.

## Suspend with pause_for

```python
async def ask(data: TriggerFlowRuntimeData):
    return await data.async_pause_for(
        type="human_input",
        payload={"question": f"Approve action for {data.input}?"},
        resume_to="next",
    )
```

What `pause_for` does:

- Records an interrupt with a unique id.
- Stops the auto-close timer for this execution.
- Returns to the framework. Durable continuation is graph-based, not Python stack persistence.
- The interrupt is exposed via `execution.get_pending_interrupts()`.
- When `continue_with(interrupt_id, payload)` is called, the graph resumes according to `resume_to`.

| Argument | Meaning |
|---|---|
| `type=` | a string label (e.g. `"human_input"`, `"approval"`, `"webhook"`). The application uses this to decide how to surface the interrupt. |
| `payload=` | structured details for whatever's responsible for resuming (UI to render a question, webhook recipient, etc.). |
| `resume_to=` | optional target for `continue_with`: `"next"`, `"self"`, or `{"event": "EventName"}`. |
| `resume_event=` | compatibility shortcut. If set without `resume_to`, `continue_with` and matching `emit(...)` route to that event. |
| `interrupt_id=` | optional. Specify the id yourself; otherwise the framework generates one. |
| `max_resumes=` | optional guard for `resume_to="self"`. Defaults to `1`, so a resumed chunk must handle `data.is_resume` instead of pausing itself forever. Pass a higher integer for bounded self-retry loops, or `None` only for an intentionally unbounded loop with its own exit guard. |

## Resume with continue_with

```python
interrupt_id = next(iter(execution.get_pending_interrupts()))
await execution.async_continue_with(interrupt_id, {"approved": True})
```

With `resume_to="next"`, the payload becomes the paused chunk's output and the next `.to(...)` receives it.

With `resume_to="self"`, the same chunk runs again. Use `data.is_resume` and `data.resume.value`:

```python
async def gate(data: TriggerFlowRuntimeData):
    if data.is_resume:
        return {"decision": data.resume.value}
    return await data.async_pause_for(
        type="approval",
        payload={"question": "Approve?"},
        resume_to="self",
    )
```

`resume_to="self"` carries a `resume_count` in the interrupt ledger and signal
metadata. By default the same signal may be replayed once; if the resumed chunk
calls `pause_for(..., resume_to="self")` again without handling
`data.is_resume`, TriggerFlow fails with a self-resume limit error instead of
building an unbounded interrupt loop.

With `resume_to={"event": "ApprovalGiven"}`, TriggerFlow emits that event with the resume payload. `resume_event="ApprovalGiven"` keeps the older event-based behavior.

## Worked example

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="approval")

    async def ask(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            payload={"question": f"Approve refund for ticket {data.input}?"},
            resume_to="next",
        )

    async def commit(data: TriggerFlowRuntimeData):
        await data.async_set_state("decision", data.input)

    flow.to(ask).to(commit)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("T-001")

    # In a real system the UI / webhook would call continue_with later.
    # Here we resume from the same coroutine for demo purposes.
    interrupt_id = next(iter(execution.get_pending_interrupts()))
    await execution.async_continue_with(interrupt_id, {"approved": True})

    snapshot = await execution.async_close()
    print(snapshot["decision"])  # {'approved': True}


asyncio.run(main())
```

Note: this flow uses `pause_for(...)`. It must be created with `flow.create_execution(...)` (or `flow.start_execution(...)`), **not** `flow.start(...)` — the hidden execution sugar has no handle the outside can use to call `continue_with`, so TriggerFlow raises as soon as `pause_for(...)` is reached.

For a document-review example where the model itself decides to interrupt the
workflow, see `examples/step_by_step/11-triggerflow-19_document_review_pause_resume.py`.
It uses `pause_for(..., resume_to="self")` inside the model-owned gate, so the
same gate re-enters with `data.is_resume` and `data.resume` after human review.

## Pause across process restarts

`pause_for(...)` integrates cleanly with checkpoint rehydration:

```python
flow.declare_resource_requirement("approval_service")

execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")
# at this point pause_for has been hit; an interrupt is pending

saved = execution.save()
# persist saved somewhere

# later, in a different process / worker:
restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved,
    runtime_resources={"approval_service": approval_service},
)
interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(
    interrupt_id,
    {"approved": True},
    resume_request_id="approval-webhook-42",
)
snapshot = await restored.async_close()
```

The interrupt and accepted resume request ids are part of the saved state, so
the new process knows what's pending and can ignore duplicate resume retries.
See [Persistence and Blueprint](persistence-and-blueprint.md).

## Multiple concurrent pauses

A single execution can have several outstanding interrupts (e.g., two parallel branches each waiting for human input). `get_pending_interrupts()` returns all of them; `continue_with(id, payload)` resolves one at a time.

If you want a specific id, supply `interrupt_id="my-id"` to `pause_for(...)` and use the same id in `continue_with`.

## Pause vs emit

| Pattern | Use |
|---|---|
| `pause_for(..., resume_to="next")` + `continue_with` | the next graph step should receive the resume payload |
| `pause_for(..., resume_to="self")` + `continue_with` | the same chunk should run again with `data.resume` context |
| `emit` + `when(...)` | a separate handler should run when an event happens; the original chunk doesn't need to wait |

Pause is the right choice for human-in-the-loop because the chunk's logic depends on the human response. Emit/when is the right choice for fan-out side effects.

## auto_close interaction

`auto_close=True` does not fire while any `pause_for` is outstanding. Once `continue_with` resolves the last pending interrupt and the execution becomes idle again, the auto-close timer restarts from zero.

If you want the execution to never auto-close while waiting indefinitely, use `auto_close_timeout=None` (and remember to call `close()` explicitly).

`async_close()` refuses to close while interrupts are still waiting. Resume them first, or explicitly cancel the waits:

```python
snapshot = await execution.async_close(pending_interrupts="cancel")
```

## See also

- [Lifecycle](lifecycle.md) — when seal/close run after a resume
- [Persistence and Blueprint](persistence-and-blueprint.md) — saving across pauses
- [State and Resources](state-and-resources.md) — re-inject `runtime_resources` after `load()`
