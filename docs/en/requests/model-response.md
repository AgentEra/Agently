---
title: Model Results
description: Reading text, structured data, metadata, and streaming events from one result.
keywords: Agently, result, get_result, get_data, get_text, get_meta, generator, streaming
---

# Model Result

> Languages: **English** · [中文](../../cn/requests/model-response.md)

`agent.input(...).start()` is a convenience that creates an `AgentExecution`,
runs it, and returns the parsed data. For everything more interesting — text,
metadata, streaming, reuse, status, or task refs — go through `get_result()`.
Quick prompt chains return an `AgentExecutionResult`; direct
`agent.create_request(...).get_result()` returns `ModelRequestResult`.
`ModelResponseResult` is no longer a public result facade. Direct
`ModelResponse` construction remains deprecated as well.

## Two consumption styles

```python
# Style A: one shot, return parsed data immediately
result = agent.input("...").output({...}).start()

# Style B: hold a reusable result facade
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

Style B is the default for non-trivial code. The actual model call runs lazily when you first consume from `result`, then results are **cached** — multiple reads do not re-issue the request. `get_response()` remains a compatibility alias for older code and returns the same result facade.

Completed `AgentExecution` objects are immutable run records. For compatibility
with Agent quick-prompt style, `agent.input(...).start()` creates a fresh
execution for that expression. If you explicitly capture an `AgentExecution`,
treat it as one independent run; after it starts, prompt/config methods such as
`input(...)` or `output(...)` raise a lifecycle error. Create a new execution
from `agent.input(...)`, `agent.create_execution(...)`, or
`execution.create_execution(...)` for the next request boundary.

## Decide whether one request is enough

A ModelRequest starts from the prompt, settings, and information available when
it is dispatched. Combine supporting semantic steps in one ordered output
contract when later fields need only that request-time input/evidence snapshot
and earlier bounded fields in the same response. This can avoid repeated
context and lets a conclusion reuse a task-specific assessment produced
earlier in the same generation.

Start a later ModelRequest when its semantic output needs a fact that appears
only after an Action, tool, API/database read, artifact readback,
approval/resume, or host computation runs:

```text
R1 search plan
-> execute retrieval and validate its observed result
-> R2 grounded answer
```

Reading text, data, and metadata from one cached result does not issue another
request. Conversely, reusing that result cannot add information that did not
exist at dispatch time. `instant` may start cancelable/idempotent work from a
complete early field, but it cannot feed the work's later result into the
already-running request. If later model generation needs that result, reconcile
and join first, then pass the validated observation to a new request.

## Reader methods

| Method | Returns |
|---|---|
| `result.get_text()` | full plain text |
| `result.get_data()` | final business data; parsed structured dict when `output()` was used |
| `result.get_data_object()` | Pydantic instance (when `output()` was given a `BaseModel`) |
| `result.get_meta()` | dict of usage / model info / timing |

Each common reader has an async sibling: `async_get_text()`,
`async_get_data()`, `async_get_data_object()`, `async_get_meta()`.

For `AgentExecutionResult`, `get_data()` is the business-result view across
direct, flat, and TaskBoard routes. If a task-strategy run returns a terminal
envelope with `final_result`, `get_data()` returns that `final_result` parsed
against the declared `output(...)` contract when possible.
`AgentExecutionResult` also provides `get_full_data()` /
`async_get_full_data()` for route/task internals such as `status`, `accepted`,
`artifact_status`, `taskboard`, `completion_notes`, or diagnostics.

Mixing readers is fine — they all consume from the same cached result:

```python
result = agent.input("...").output({...}).get_result()
data = result.get_data()        # triggers the request
text = result.get_text()        # already cached
meta = result.get_meta()        # already cached
```

This is also how `.validate(...)` runs only once per result — the cached result is what gets validated.

## Streaming

`result.get_generator(type=...)` (sync) and `get_async_generator(type=...)` (async) yield streaming events. The `type` parameter selects what you see:

| `type` | What you get | Use it for |
|---|---|---|
| `"delta"` | text deltas, plus `"<$retry>{reason}</$retry>"` before a replay replacement | terminal-style typing UX |
| `"instant"` | structured `StreamingData` events with `path`, `delta`, `value`, and `is_complete` | field-level UI updates |
| `"streaming_parse"` | alias for the same structured streaming parser used by `instant` | compatibility / incremental dict reads |
| `"specific"` | `(event, data)` tuples filtered by event type (`delta`, `reasoning_delta`, `tool_calls`, etc.) | pick exactly the events you care about |
| `"original"` | raw provider events | debugging / passthrough |
| `"all"` | every event with type tag | exhaustive logging |

For common type annotations, import the public stream item types from
`agently`: `StreamingData` for `instant` / `streaming_parse`,
`AgentlySpecificResultMessage` for `specific`, and
`AgentlyModelResultMessage` for `all`. The same types remain available from
`agently.types.data` when you want the full typed data namespace.
The older `AgentlySpecificResponseMessage`, `AgentlyModelResponseMessage`, and
related `Response` aliases remain available from `agently.types.data` for
compatibility, but they are not re-exported from the `agently` root. The
`Result` names are the recommended API.

`ModelRequestResult` is the canonical result class. Do not import or annotate
with the historical `ModelResponseResult` name.

### Delta example

```python
gen = agent.input("Tell me a recursion story.").get_generator(type="delta")
for delta in gen:
    print(delta, end="", flush=True)
```

### Instant example (structured)

```python
gen = (
    agent.input("Give me a definition and three tips.")
    .output({
        "definition": (str, "Definition", True),
        "tips": [(str, "Tip", True)],
    })
    .get_generator(type="instant")
)
for item in gen:
    if item.delta:
        print(f"[{item.path}] + {item.delta}")
    if item.is_complete:
        print(f"[{item.path}] done")
```

`item` exposes `.path` (e.g. `"tips[0]"`), `.wildcard_path` (`"tips[*]"`),
`.value`, `.delta`, `.is_complete`, `.event_type`, and `.completion_source`.
Use `.delta` to update
the visible field while it is growing. Use `.is_complete` / `event_type=="done"`
when downstream work should wait until the field is closed.

For JSON streams, `completion_source` distinguishes
`"observed_boundary"` (a delimiter appeared in provider text),
`"final_reconciliation"` (the raw final JSON itself closed), and
`"synthetic_repair"` (the parser completed an open suffix). All remain
provisional until final validation. In particular, irreversible work and
long-output retention must not treat `synthetic_repair` as accepted data.

### AgentExecution projection

`AgentExecutionStreamData` is an execution-level structured projection, not a
`ModelRequestResult`. When an execution owns a model request, `instant` / `all`
streams preserve model attempt facts as structured stream items. In particular,
`$status` carries retry/failure/completion state and its `meta` includes
`response_id`, `request_run_id`, `model_run_id`, and `attempt_index`.
`type="delta"` is the plain text projection; it yields strings and uses
`"<$retry>{reason}</$retry>"` to mark a replay boundary.
`type="instant"` preserves each original structured item and, when that item can
also be projected to natural-language text, immediately appends a synthetic
`AgentExecutionStreamData` item with `path="$delta"`, `event_type="delta"`,
`source="agent_execution"`, and `meta["stream_kind"] == "text_projection"`.
AgentTask Flat snapshots may project to linear plan/action summaries, while
TaskBoard plan/tick events may project to compact Markdown status tables first
and later card-state summaries.
Heartbeat items are structured-only and do not append synthetic `$delta` text.
`type="all"` remains the raw audit stream and does not include those synthetic
projection items.

A structured `AgentExecution` keeps its owned `ModelRequest` open until the
request reaches its natural provider/parser terminal boundary. Seeing an
ensured field or a completed mapping field in `instant` means that provisional
path is available; it does not cancel the request or prove that later evidence,
self-check, summary, progress, diagnostics, final validation, usage, or terminal
events have arrived. Successful final-only and streaming consumers therefore
observe ordinary request completion, including `request.completed`.

```python
execution = agent.input("Summarize the incident update.")
async for item in execution.get_async_generator(type="instant"):
    if item.path == "$status":
        print(item.value["status"], item.meta["response_id"])
    elif item.path == "$delta" and item.delta:
        # Unified natural-language stream slot.
        print(item.delta, end="", flush=True)
    elif item.path == "model.delta" and item.delta:
        # Source-addressed model delta. Use this for structured UI state, not
        # for the same text surface that consumes "$delta".
        ui_state[item.path] = ui_state.get(item.path, "") + item.delta
```

The no-argument execution generator defaults to the same `delta` projection, so
`execution.get_generator()` and `execution.get_async_generator()` yield strings.
Use `type="instant"` or `type="all"` when the consumer needs the structured
`$status` item instead of the text marker. Use `type="instant"` when a UI needs
both structured state updates and the derived `$delta` text slot, but keep
those surfaces separate: render `$delta` as the unified natural-language stream
and render source-addressed deltas such as `model.delta` or field paths only into
their own structured state slots. Do not append both to the same visible text
buffer. Use `type="all"` for records, DevTools-style replay, internal bridges,
or audits that must not mix derived items with source facts.

For shared-output CLI rendering, do not treat `.is_complete` as a global
display-order barrier. A structured parser often confirms that one path is
closed because it has already seen the next path begin, so a later path's first
`.delta` can arrive at the consumer near the earlier path's done event. Web UIs,
SSE, and WebSocket consumers should usually render each `path` into its own UI
slot. If a CLI must print several paths into one terminal area in a fixed human
order, keep a small state flag or buffer in the consumer and flush the later
path only after the earlier path's `.is_complete` event has been handled.

### No progressive consumer: read final data directly

If the caller does not publish deltas, update UI/state, record stream events, or
use provisional fields for explicitly cancelable/idempotent preparation, do not
open a stream. Await the final parsed object directly:

```python
result = (
    agent
    .input("Classify this support ticket.")
    .output({"route": (str, "billing | technical | other", True)})
    .get_result()
)

data = await result.async_get_data()
```

This discard-only `instant` drain loop is an anti-pattern:

```python
# Anti-pattern: no item is published or used.
async for _item in result.get_async_generator(type="instant"):
    pass

data = await result.async_get_data()
```

It does not issue a second model request, but it still adds stream queue,
iteration, event-object, and parser work without creating consumer value. Use a
generator only when its items drive a real consumer. If streaming is needed,
publish or apply the items and then read the durable final object from the same
result:

```python
async for item in result.get_async_generator(type="instant"):
    await publish_structured_patch(item)

data = await result.async_get_data()
```

### High-value pattern: stream fields to UI, then read the durable result

Use `instant` when the application can show or route individual structured
fields before the whole answer is finished. The stream is for progressive UI
state; the final business object should still come from `async_get_data()`.

```python
import asyncio
from collections import defaultdict
from agently import Agently

agent = Agently.create_agent()


async def stream_triage_card(ticket_text: str):
    result = (
        agent
        .input(ticket_text)
        .output(
            {
                "status_summary": (str, "One sentence status for the user", True),
                "risk_flags": [(str, "Concrete risk flag", True)],
                "next_actions": [(str, "Action the support team should take", True)],
                "customer_reply": (str, "Polished reply to the customer", True),
            },
            format="json",
        )
        .get_result()
    )

    ui_state: dict[str, str] = defaultdict(str)

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            # Render a field-level patch to your UI / SSE / WebSocket channel.
            ui_state[item.path] += item.delta
            print({"path": item.path, "delta": item.delta})
        if item.is_complete:
            print({"path": item.path, "status": "done", "value": item.value})

    # No second request: this reads the cached final parse from the same result.
    final_data = await result.async_get_data()
    return final_data


asyncio.run(stream_triage_card(
    "Ticket T-104: enterprise billing export failed twice; CFO waiting."
))
```

Prefer async consumption for services. Synchronous `get_generator(type="instant")`
is fine for scripts and notebooks.

### Start downstream work early, then reconcile

An early completed field may start read-only or otherwise
idempotent/cancelable work while the model continues generating. Use this
contract:

1. react only to a complete canonical field or list item, such as
   `wildcard_path == "retrieval_tasks[*]" and is_complete`;
2. derive a host-owned key from the task-relevant payload and dispatch through
   a bounded async owner without awaiting the long operation in the stream loop;
3. keep consuming the model stream;
4. read the final validated object with `async_get_data()`;
5. reuse matching work, start accepted items missed by streaming, and cancel or
   discard provisional extras.

Final reconciliation is required because parsing, ensure, or custom validation
may accept a replacement attempt after the original instant stream. Retry
events and repeated deltas must converge on the same host key rather than repeat
retrievals. Irreversible effects and final decisions still wait for the final
accepted object.

### Specific example (events)

```python
gen = agent.input("Hello.").get_generator(type="specific")
for event, data in gen:
    if event == "delta":
        print(data, end="", flush=True)
    elif event == "reasoning_delta":
        print("[reasoning]", data, end="", flush=True)
    elif event == "tool_calls":
        print("[tool call]", data)
```

### Reasoning events

Some providers expose reasoning in native response fields. Some local or
OpenAI-compatible reasoning models may instead place a leading outer
`<think>...</think>` block in ordinary content. Agently normalizes both cases
before structured parsing:

- `reasoning_delta` / `reasoning_done` carry reasoning text.
- `delta` / `done` carry only the answer payload that parsers should consume.
- `original_delta` / `original_done` keep the provider's raw content unchanged.
- Only a complete leading outer `<think>...</think>` before the answer payload is
  normalized. `<think>` inside a field, code block, or long text payload remains
  ordinary answer content.

## Async streaming

Same generators in async form:

```python
import asyncio

async def main():
    result = agent.input("...").output({...}).get_result()
    async for item in result.get_async_generator(type="instant"):
        if item.is_complete:
            print(item.path, item.value)

asyncio.run(main())
```

For services and TriggerFlow usage, async is the recommended path — see [Async First](../start/async-first.md).

### Attempt status

`$status` is a reserved framework stream path, not a model output field. It is
useful when a provider replay is explicitly allowed after partial output:

```python
result = agent.create_request().input("Summarize the incident.").get_result()

async for item in result.get_async_generator(type="instant"):
    if item.path == "$status" and item.value["status"] == "failed" and item.value["retry"]:
        clear_provisional_answer()
        continue
    render_field_update(item)
```

The final `get_data()` result contains no `$status`. Use `type="all"` or
`type="specific", specific="status"` when a consumer needs raw status events.
`reason` contains a bounded transport/provider explanation, and `cancelled` is
distinct from a failed request.

For OpenAI-compatible SSE, an explicit `[DONE]` is the logical response
terminal. Agently stops consuming that SSE iterator immediately, so a gateway
that omits or damages the later HTTP chunked terminator cannot replace the
already completed response with a transport error. The final
`original_done`, `meta`, usage, and `finish_reason` are preserved. A disconnect
before `[DONE]` remains a transport failure and follows the configured
failover/retry policy; partial output alone is never synthesized into success.

Plain `delta` consumers receive the standalone
`"<$retry>{reason}</$retry>"` marker before replacement text. It is a replay
boundary, not model content:

```python
import html

provisional_text = ""
for chunk in result.get_generator(type="delta"):
    if "<$retry>" in chunk:
        retry_reason = html.unescape(
            chunk.removeprefix("<$retry>").removesuffix("</$retry>")
        )
        provisional_text = ""
        clear_provisional_answer(retry_reason)
        continue
    provisional_text += chunk
    render_delta(chunk)
```

The marker reason XML-escapes `<`, `>`, and `&` from the provider message.
When structured events are available, `$status` is the preferred retry control
record. When a consumer chooses plain `delta`, the marker is the corresponding
public replay boundary. A text-only stream cannot make a sentinel collision-free,
so consumers that must preserve a literal model chunk containing `"<$retry>"`
should use `instant`, `specific`, or `all`.

An AgentExecution projects the same status as a structured process item and
adds the originating request/run lineage in `item.meta`. Use `instant` or
`specific` when the consumer needs those structured retry facts:

```python
execution = agent.input("Summarize the incident.")

async for item in execution.get_async_generator(type="instant"):
    if item.path == "$status" and item.value["retry"]:
        clear_provisional_output(item.meta["response_id"])
        continue
    render_execution_item(item)
```

Its public `type="delta"` projection may emit the same `<$retry>...</$retry>`
replay marker as text. Durable artifact writers and SSE/UI consumers should
handle that marker as a public replay delimiter when they choose a plain-text
stream, but structured `$status` is the retry control source and the only source
for retry metadata such as attempt indexes. Do not force a freeform document
body through `.output()` only to obtain instant fields.

## Concurrency

Because `get_result()` only kicks off the actual request when you consume it, you can build many results up front and consume them in parallel:

```python
import asyncio

async def ask(prompt):
    r = agent.input(prompt).get_result()
    return await r.async_get_text()

results = await asyncio.gather(
    ask("Summarize recursion."),
    ask("Give one Python example."),
)
```

This is a standard async pattern; nothing in Agently is special about it.

### Optional request scheduling

When many concurrent requests (or long-running tasks) risk hitting a provider's
concurrency or rate limits, you can bound model request dispatch per provider.
Scheduling is opt-in; with no configuration, requests dispatch immediately and
retries re-issue immediately (unchanged behavior).

```python
# Cap concurrent in-flight requests and starts/second for all providers,
# with an optional per-provider override.
agent.set_settings("model_request.scheduler.max_concurrency", 8)
agent.set_settings("model_request.scheduler.rate_per_second", 5)
agent.set_settings("model_request.scheduler.providers",
                   {"OpenAICompatible": {"max_concurrency": 2}})

# Back off between retries instead of re-issuing immediately (exponential + jitter).
agent.set_settings("model_request.retry_backoff_base", 0.5)  # seconds
agent.set_settings("model_request.retry_backoff_max", 30)
```

Because retries re-issue through the same per-provider slot, the rate limit also
spaces out retried calls, which dampens provider error storms.

## Don't re-issue when you can re-read

```python
# bad — runs the request three times
text = agent.input("...").start()
data = agent.input("...").output({...}).start()
meta = agent.input("...").output({...}).get_result().get_meta()

# good — runs once, reads three views
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

## See also

- [Async First](../start/async-first.md) — when to switch to `get_async_generator(...)`
- [Output Control](output-control.md) — what runs between "model returned" and "you read"
- [Schema as Prompt](schema-as-prompt.md) — what `output()` accepts
