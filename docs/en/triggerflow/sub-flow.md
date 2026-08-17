---
title: Sub-Flow
description: Composing flows with to_sub_flow, capture, and write_back semantics.
keywords: Agently, TriggerFlow, sub_flow, to_sub_flow, capture, write_back, composition
---

# Sub-Flow

> Languages: **English** · [中文](../../cn/triggerflow/sub-flow.md)

`to_sub_flow(child_flow, ...)` lets a parent flow embed a child flow as a single chunk. The child runs to its own close, then the parent continues with whatever the child produced.

## Plain composition

```python
parent.to(prepare).to_sub_flow(child_flow).to(consume)
```

Without `capture` or `write_back`, the bridge does the simplest possible thing:

- The child receives the parent's current `data.input` as **its** start input.
- After the child closes, the parent's `data.input` for `consume` becomes the child's close snapshot.
- If the child wrote a compatibility result via the deprecated `set_result()` or `.end()`, the parent receives that compat value instead of the snapshot. (See [Compatibility](compatibility.md).)

## capture — selecting parent → child input

`capture` maps parent values into the child's input and runtime resources:

```python
parent.to(prepare_request).to_sub_flow(
    child_flow,
    capture={
        "input": "value",                       # child's start input = parent's current data.input
        "resources": {"logger": "resources.logger"},
    },
)
```

Common `capture` paths:

| Path | Resolves to |
|---|---|
| `"value"` | the parent's current `data.input` |
| `"state.<key>"` | a value from parent's state |
| `"resources.<name>"` | a parent runtime resource |

The right column is mapped onto the child's input or resources by the keys on the left.

Value, state, and `flow_data` captures are isolated copies. A
`resources -> resources` capture forwards the same live object by identity; it
does not deep-copy clients, callbacks, locks, events, or other live handles. If
a paused sub-flow is restored, the binding resolves against the resource
currently re-injected into the parent execution rather than retaining an old
object in the snapshot. Flow-level `runtime_resources` already configured on
the child template are also inherited by identity when TriggerFlow creates the
isolated child flow instance.

## write_back — child result → parent

`write_back` maps the child's final result back into the parent:

```python
parent.to(prepare).to_sub_flow(
    child_flow,
    capture={"input": "value"},
    write_back={"value": "result.report"},
).to(finalize)
```

Resolution rules for `write_back`:

| `write_back` value | Source preference |
|---|---|
| `"result"` | child compat result if present, otherwise child close snapshot |
| `"result.<path>"` | first try the same path inside child compat result; fall back to the matching path inside the close snapshot |
| `"snapshot"` | the close snapshot directly (skip compat result) |
| `"snapshot.<path>"` | path inside the snapshot |

The `value` key on the left side puts the resolved value back into the parent's `data.input` for the next chunk. Other keys (`state.<name>`) write into parent state.

This is why the same `result.<path>` syntax works for both legacy compat-result-style children and new state-first children — the lookup tries compat first, then falls back to the snapshot.

## Worked example

```python
def build_child_flow():
    child = TriggerFlow(name="child")
    (
        child.if_condition(has_multiple_sections)
            .to(use_multi_section_mode)
        .else_condition()
            .to(use_single_section_mode)
        .end_condition()
        .to(list_sections)
        .for_each()
            .to(draft_section)
        .end_for_each()
        .to(summarize_child_report)
    )
    return child


def build_parent_flow():
    parent = TriggerFlow(name="parent")
    parent.update_runtime_resources(logger=SimpleLogger())
    parent.to(prepare_request).to_sub_flow(
        build_child_flow(),
        capture={
            "input": "value",
            "resources": {"logger": "resources.logger"},
        },
        write_back={
            "value": "result.report",
        },
    ).to(finalize_request)
    return parent
```

What happens:

1. `prepare_request` produces a request context as its return value.
2. `to_sub_flow(...)` starts the child with that context as the child's `data.input`. The parent's `logger` resource is forwarded.
3. The child branches, fans out via `for_each`, drafts each section, summarizes, and writes the result to its own `state["report"]`.
4. The bridge resolves `write_back={"value": "result.report"}`: it looks for `report` first in any compat result the child set, then in the child's close snapshot, finds it, and assigns it as the parent's next `data.input`.
5. `finalize_request` runs in the parent with that `data.input`.

## Stream items cross sub-flow boundaries

Items pushed via `data.async_put_into_stream(...)` inside the child show up in the **parent execution's** runtime stream. From an external consumer's point of view, sub-flows look like part of the same execution.

## Control a running child by frame id

`capture` and `write_back` are boundary bindings, not live bindings: `capture`
copies selected serializable parent values when the child starts, forwards
selected live resources by identity, and `write_back` runs only after successful
child completion. When the host needs to inspect, signal, or cancel an active
child, keep an explicit parent execution handle and use its sub-flow frame:

```python
execution = parent_flow.create_execution(auto_close=False)
start_task = asyncio.create_task(execution.async_start(input_value))

# After the host observes triggerflow.sub_flow_started for this execution:
frames = execution.get_sub_flow_frames()
frame_id = next(
    frame_id
    for frame_id, frame in frames.items()
    if frame["status"] == "running"
)

await execution.async_emit_to_sub_flow(
    frame_id,
    "StopRequested",
    {"reason": "superseded"},
)

cancelled = await execution.async_cancel_sub_flow(
    frame_id,
    reason="superseded",
)
await start_task
```

The synchronous counterparts are `emit_to_sub_flow(...)` and
`cancel_sub_flow(...)`.

Run [`examples/trigger_flow/active_sub_flow_control.py`](../../../examples/trigger_flow/active_sub_flow_control.py)
for a deterministic cancellation-and-fencing example.

`async_cancel_sub_flow(...)` returns `True` only when that call wins the active
or waiting frame's cancellation transition. A late or duplicate cancellation
returns `False`. Cancellation changes the frame through `cancel_requested` to
`cancelled`, cancels cooperative in-process child work, and prevents child
`write_back` and parent downstream continuation. It does **not** close the
parent execution, so the parent can continue accepting later events.

`async_emit_to_sub_flow(...)` forwards one signal through the child execution's
normal signal net. The child concurrency budget still applies: if a control
handler must run beside long-lived child work, configure enough sub-flow
`concurrency`. Signal forwarding is a best-effort control aid; cancellation or
an application/provider-owned idempotency fence remains the correctness
boundary for irreversible work. If cancellation wins while a signal is in
flight, the forwarding call raises `RuntimeError` and the signal handler is
cooperatively cancelled.

Frame statuses are observable as `running`, `waiting`, `cancel_requested`,
`cancelled`, `failed`, or `completed`. Running frame metadata is serializable
for audit, but live executions and tasks are not. Loading a snapshot containing
a `running` or `cancel_requested` frame fails closed; settle or cancel active
children before saving a restart-resumable snapshot. Existing `waiting` frames
remain restorable through projected root interrupts.

Framework cancellation cannot physically retract an already-submitted remote
model request, thread, subprocess, or external side effect. Those boundaries
still need provider abort, idempotency, or durable fence semantics.

## Child pauses project to the parent

If a child flow calls `pause_for(...)`, the parent execution becomes waiting too. External systems still manage only the parent execution id and the parent interrupt id:

```python
execution = parent_flow.create_execution(auto_close=False)
await execution.async_start(input_value)

root_interrupt_id = next(iter(execution.get_pending_interrupts()))
saved = execution.save()

restored = parent_flow.create_execution(auto_close=False)
await restored.async_load(saved, runtime_resources={...})
await restored.async_continue_with(
    root_interrupt_id,
    {"approved": True},
    resume_request_id="approval-webhook-42",
)
```

The projected interrupt includes `sub_flow_frame_id` and `local_interrupt_id` for debugging, but callers should treat the parent interrupt id as the public handle. After the child finishes, `write_back` runs normally and the parent continues downstream.

For a prearranged document-review approval gate, see
`examples/step_by_step/11-triggerflow-20_document_review_subflow_pause_resume.py`.
The child sub-flow has an explicit pause chunk and waits through
`when("LegalApprovalSubmitted")`; the parent still saves, reloads, and resumes
through the projected root interrupt.

## When to use a sub-flow

- The child is reusable — used by multiple parent flows or independently.
- The child has its own well-defined contract (input + result) you'd want to test in isolation.
- You want to keep the parent flow shorter and more readable.

## When *not* to use a sub-flow

- The child is only one or two chunks. Inline them.
- You're using sub-flow purely as a way to share state. Use a parent function or `runtime_resources` instead.
- You'd be tempted to share runtime stream filtering between parent and child — separate concerns, don't entangle.

## See also

- [Patterns](patterns.md) — `for_each`, `if_condition`, `match`
- [State and Resources](state-and-resources.md) — how `runtime_resources` propagate to child via `capture`
- [Compatibility](compatibility.md) — why `result.<path>` falls back to snapshot
