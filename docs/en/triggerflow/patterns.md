---
title: TriggerFlow Patterns
description: Branching, matching, batches, for_each, and event-driven loops.
keywords: Agently, TriggerFlow, if_condition, match, batch, for_each, loop
---

# Patterns

> Languages: **English** · [中文](../../cn/triggerflow/patterns.md)

The patterns below cover the day-to-day shapes most flows fall into.

## Linear chain

```python
flow.to(step_a).to(step_b).to(step_c)
```

Each handler receives the previous handler's return value as `data.input`.

## if / elif / else

```python
async def score(data):
    return {"score": 82}

async def store_grade(data):
    await data.async_set_state("grade", data.input)

(
    flow.to(score)
    .if_condition(lambda data: data.input["score"] >= 90)
        .to(lambda _: "A")
    .elif_condition(lambda data: data.input["score"] >= 80)
        .to(lambda _: "B")
    .else_condition()
        .to(lambda _: "C")
    .end_condition()
    .to(store_grade)
)
```

`end_condition()` is required — it closes the conditional branch and gives you back the chain to continue. The chosen branch's return becomes the next chunk's `data.input`.

## match / case

```python
(
    flow.to(lambda _: "medium")
    .match()
        .case("low").to(lambda _: "priority: low")
        .case("medium").to(lambda _: "priority: medium")
        .case("high").to(lambda _: "priority: high")
        .case_else().to(lambda _: "priority: unknown")
    .end_match()
    .to(store_result)
)
```

`match()` switches on `data.input` from the previous chunk. Use it when you have a small set of discrete values; for predicates, prefer `if_condition`.

## when — event joins

```python
flow.when(["task_a_done", "task_b_done"], mode="and").to(run_after_both)
```

The list form is the event-only shorthand for
`flow.when({"event": [...]}, mode="and")`. It is the recommended shape for
developer-owned DAG dependencies because the dependency edge stays visible in
the TriggerFlow definition and runtime events.

## batch — parallel named branches

```python
async def echo(data):
    return f"echo: {data.input}"

flow.batch(
    ("a", echo),
    ("b", echo),
    ("c", echo),
).to(store_batch)
```

All branches run in parallel against the same `data.input`. The next chunk receives a list (or dict, depending on configuration) of all branch outputs.

Throttle concurrency at the execution level:

```python
execution = flow.create_execution(concurrency=2)
```

Execution concurrency is a global handler-dispatch limit for that execution,
including nested dispatch from chunk continuations and `data.async_emit(...)`.
When a handler awaits an internal dispatch, TriggerFlow yields and later
reacquires the permit so ordinary chains do not deadlock at
`concurrency=1`. Operator-level `batch(..., concurrency=...)` and
`for_each(..., concurrency=...)` remain local fan-out caps.

## for_each — fan-out over a sequence input

```python
async def double(data):
    return data.input * 2

(
    flow.for_each(concurrency=2)
        .to(double)
    .end_for_each()
    .to(store_items)
)

execution = flow.create_execution()
await execution.async_start([1, 2, 3, 4])
# store_items receives [2, 4, 6, 8]
```

`for_each` inspects the previous chunk's output (or the start input): non-string `Sequence` values are expanded into items; scalar values are treated as one item. Each item runs through the body in parallel up to the `concurrency` cap, and results are collected in input order.

If you want "run N times", return a sequence explicitly from the previous chunk:

```python
async def make_range(data):
    return list(range(data.input))

flow.to(make_range).for_each().to(double).end_for_each()
```

## Event-driven loops

Python `for` loops still belong inside handler functions. At the graph level, repeated fan-out is `for_each`; loops driven by flow-internal signals are expressed with `emit` + `when`:

```python
flow = TriggerFlow(name="loop")

async def start_loop(data):
    await data.async_set_state("values", [], emit=False)
    data.emit_nowait("Loop", 0)

async def loop_step(data):
    values = data.get_state("values", []) or []
    values.append(data.input)
    await data.async_set_state("values", values, emit=False)
    if data.input < 3:
        data.emit_nowait("Loop", data.input + 1)
    else:
        await data.async_set_state("done", {"last": data.input, "count": len(values)})

flow.to(start_loop)
flow.when("Loop").to(loop_step)
```

Mechanics:

- A chunk emits the loop event with the next iteration's payload.
- The `when(...)` branch runs and either emits again (continue) or stops emitting (exit).
- The execution drains naturally once nothing emits anymore.

Pass `emit=False` to `async_set_state` when you want to update state without triggering observers — useful inside hot loops to keep observation overhead reasonable.

For long loops, give the execution a sensible `auto_close_timeout` (or `auto_close=False` + manual `close()`) so it doesn't fall off the cliff during a brief pause between iterations.

## Side branches that don't block the main path

A `when(...)` branch and the main chain run independently. You can use this for fire-and-forget logging, telemetry, or out-of-band notifications:

```python
flow.to(main_step)

@flow.when("MainStepDone").to
async def log_step(data):
    await some_external_log(data.input)
```

`main_step` runs `data.async_emit("MainStepDone", {...})` and the side branch fans out from there without blocking the main return value.

## Combining patterns

A single flow often mixes patterns. The sub-flow page has a worked example with `if_condition` + `for_each` + sub-flow composition; see [Sub-Flow](sub-flow.md).

## See also

- [Events and Streams](events-and-streams.md) — `emit` / `when` mechanics
- [Sub-Flow](sub-flow.md) — composing flows with `to_sub_flow`
- [Lifecycle](lifecycle.md) — when batched / for-each work counts as "drained"
