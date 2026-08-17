---
title: Event Center
description: RuntimeEvent registration, filtering, shape, and DevTools projection rules in Agently.
keywords: Agently, EventCenter, ObservationEvent, RuntimeEvent, observation, DevTools
---

# Event Center

> Languages: **English** · [中文](../../cn/observability/event-center.md)

The Event Center is Agently's framework-level runtime event channel. It carries **RuntimeEvent** records: model requests, Session application, TriggerFlow lifecycle, Action calls, and similar framework activity can be forwarded to DevTools or to a custom log sink through this channel.

It is separate from TriggerFlow `emit` / `when`: `emit` / `when` changes control flow inside a flow; RuntimeEvent records report what happened.

Naming compatibility:

- `RuntimeEvent` is the preferred framework event model for new code.
- Event Center dispatches `RuntimeEvent` records to ordinary hooks.
- `ObservationEvent` is the DevTools bridge projection derived from `RuntimeEvent`.
- Existing `emit_observation` / `async_emit_observation` code continues to work as a compatibility alias.

Run and retry naming:

- `agent_execution` is the run lineage kind for one AgentExecution-owned Agent run.
- `attempt_index` describes a retryable model-request attempt inside a request; it is not an AgentExecution counter.
- DevTools should preserve both fields as separate semantics: render `agent_execution` from `run.run_kind`, and read model retry attempts from `payload.attempt_index` or `run.meta.attempt_index` on `model_request` runs.

Model request telemetry:

- Model RuntimeEvents may include `payload["model_request_telemetry"]` on `model.request_started`, `model.requesting`, `model.status`, `model.completed`, `model.meta`, `model.request_failed`, and `model.requester.error`.
- The telemetry payload is observation-only. It can contain `response_id`, `attempt_index`, run ids, provider/model, request URL, duration, raw usage, normalized usage summary, estimated input/output character lengths, side-channel, and normalized error facts.
- Telemetry dedupe only removes duplicate telemetry sub-payloads for the same `response_id + attempt_index + event kind`; it does not suppress the original RuntimeEvent.
- Do not feed these telemetry facts back into route selection, retry policy, verifier judgment, quality scoring, planner context, or prompt content. Use them for logs, DevTools display, and diagnostics.
- Each attempt outcome publishes at most one `model.status` fact. A terminal
  provider error also publishes one `model.requester.error`; publishing that
  observation does not rethrow into the attempt loop or create another outcome.
- Built-in requester exception messages retain provider status/detail but do
  not append the serialized request body. The structured
  `model.requester.error.payload["request_data"]` field remains available as
  cold diagnostic evidence and may contain sensitive prompt data, so event
  sinks must apply the same access and retention controls as request logs.

Model request status:

- `model.status` records a ModelRequest attempt outcome. It is observation-only;
  it does not decide retry or downstream control flow.
- The raw response stream event is `("status", payload)`; `instant` /
  `streaming_parse` exposes it as `StreamingData(path="$status", value=payload)`.
- `payload["status"]` is `completed`, `failed`, or `cancelled`.
- `failed` with `retry=true` invalidates partial output from
  `payload["attempt_index"]`; the next attempt is
  `payload["next_attempt_index"]`. Consumers must clear provisional output
  before rendering replacement deltas.
- `reason` carries a bounded provider/transport explanation and `error_type`
  carries the original exception class when available. It is not a traceback or
  a raw request body.
- Text-only `type="delta"` generators receive a standalone
  `"<$retry>{reason}</$retry>"` chunk at that replay boundary. They must clear
  provisional text on the marker; use `type="all"`, `specific`, `instant`, or
  `streaming_parse` when lineage or collision-free structured facts are
  required.

## Register a hook

```python
from agently import Agently

captured = []


async def capture(event):
    captured.append(event)


Agently.event_center.register_hook(
    capture,
    event_types="runtime.info",
    hook_name="docs.capture",
)

emitter = Agently.event_center.create_emitter("Docs")
await emitter.async_info("hello")

Agently.event_center.unregister_hook("docs.capture")
```

`event_types` can be a string, a list of strings, or `None`. With `None`, the hook receives every event. Sync callbacks are accepted too; Event Center normalizes them to async calls.

Hooks that forward high-frequency runtime events to an expensive outlet can ask
Event Center to summarize delivery:

```python
Agently.event_center.register_hook(
    capture,
    event_types="model.response.delta",
    hook_name="docs.summary_capture",
    delivery_policy={
        "mode": "summary",
        "dispatch": "await",
        "emit_interval": 0.1,
        "max_items": 20,
        "high_frequency_only": True,
    },
)
```

The default delivery policy is raw and awaited. Summary delivery is per hook; it
does not change the producer's RuntimeEvent records or other hooks that request
raw events. Summary events carry `meta["coalesced"]`, `coalesced_count`,
`first_event_id`, and `last_event_id`.

Use `dispatch="background"` only for best-effort outlets that have an explicit
flush/close point. `await Agently.event_center.async_flush(hook_name)` drains
buffered summaries and tracked background deliveries before shutdown.
Event Center also runs an on-demand idle flush monitor while background
deliveries or summary buffers exist: each new event refreshes the idle timer,
and a quiet period triggers bounded flushing. This is a long-lived-loop safety
net, not a replacement for explicit flush before CLI/script shutdown.

Implementation note for framework integrators: Event Center retains its native
background-task tracking in Agently 4.1.4.5. A Stage-backed candidate preserved
behavior but added measurable elapsed, p95, and traced-memory cost in the
background-event benchmark, so it was not adopted. Event Center continues to
own RuntimeEvent normalization, filtering, coalescing, hook identity,
fail-open behavior, and flush policy. No Stage type, status, or event is part of
the Event Center API.

## Emit a runtime event

Agently-owned event types such as `model.*`, `request.*`, `action.*`,
`tool.*`, `session.*`, `agent_execution.*`, `triggerflow.*`, and
`execution_resource.*` are produced by core runtime coordinators. Custom
plugins and applications may emit their own Event Center messages, but they
should use an application/plugin-owned namespace and must not rely on official
Agently modules consuming those custom messages.

Built-in plugins report facts to core through typed observations, handler
decisions, or route stream callbacks. Core-owned coordinators map those facts
into official RuntimeEvent records and AgentExecution stream items.

The usual path is an emitter:

```python
emitter = Agently.event_center.create_emitter(
    "BillingWorker",
    base_meta={"tenant": "demo"},
)

await emitter.async_emit(
    "billing.invoice_created",
    message="invoice created",
    payload={"invoice_id": "inv-1"},
)
```

You can also emit a dict directly:

```python
await Agently.event_center.async_emit({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "direct event",
})
```

When synchronous code cannot wait for hook delivery, use the tracked nowait
entry:

```python
dispatch = Agently.event_center.emit_nowait({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "tracked nowait event",
})
```

On a running loop, `dispatch` is an `asyncio.Task`; without one it is a
loop-neutral Stage handle. `emit_nowait()` does not claim delivery completion.
Use `await Agently.event_center.async_flush()` at the owning shutdown or
observation barrier.

For top-level convenience APIs:

```python
await Agently.async_emit_observation({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "compatibility observation API",
})

await Agently.async_emit_runtime({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "runtime API",
})
```

## Event shape

The top-level fields come from `agently.types.data.event.RuntimeEvent`. `ObservationEvent` has the same serialized shape when the main framework DevTools bridge projects RuntimeEvent records for DevTools ingestion:

| Field | Meaning |
|---|---|
| `event_id` | event id, generated by default |
| `event_type` | dotted name such as `triggerflow.execution_started` |
| `source` | where the event came from |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `message` | human-readable message |
| `payload` | event-specific structured data |
| `error` | error information; exceptions are normalized to `ErrorInfo` |
| `run` | run lineage, including `run_id`, `parent_run_id`, `session_id`, `execution_id`, and related ids |
| `meta` | additional metadata |
| `timestamp` | millisecond timestamp |

For model request events, `payload.model_request_telemetry` is an extensible sub-payload. Consumers should treat missing fields as unknown, not as failure. Common fields are:

| Field | Meaning |
|---|---|
| `event_kind` | original model event kind that carried the telemetry |
| `telemetry_key` | dedupe key, usually `response_id:attempt_index:event_kind` |
| `response_id` | request/response correlation id |
| `attempt_index` | retry attempt number inside the request |
| `request_run_id` / `model_run_id` | run lineage ids for request and model attempt |
| `provider` / `provider_family` / `model` | provider metadata when known |
| `request_url` | provider endpoint or provider-owned symbolic URL when known |
| `duration_ms` | elapsed time from model request start when available |
| `usage` | provider-reported usage metadata when available |
| `usage_summary` | observation-only usage summary with normalized provider token fields and estimated input/output character lengths; terminal `model.status` events may carry estimated lengths without exposing raw request payloads; missing provider tokens should be displayed as unknown, not as a failure |
| `side_channel` | whether the model event came from a side-channel request path |
| `error` | normalized error facts for failed/requester-error events |

## TriggerFlow event aliases

Event Center keeps compatibility with historical TriggerFlow event prefixes. A subscription to `workflow.execution_started` can receive `triggerflow.execution_started`; a subscription to `trigger_flow.signal` can receive `triggerflow.signal`. Documentation and new code should prefer `triggerflow.*`.

## Action compatibility events

Action Runtime lifecycle events use `action.*` as the primary namespace. When the current Action Runtime branch is tool-compatible, Agently also emits paired `tool.*` compatibility events for existing subscribers and old examples:

| Primary event | Tool compatibility event |
|---|---|
| `action.loop_started` | `tool.loop_started` |
| `action.plan_ready` | `tool.plan_ready` |
| `action.loop_failed` | `tool.loop_failed` |
| `action.loop_completed` | `tool.loop_completed` |

Paired compatibility events include `meta.compat_event_alias=True`, `meta.compat_alias_for`, and `meta.primary_event_id` so consumers can deduplicate them from the primary `action.*` event.

Concrete action execution uses `action.started`, `action.completed`, and
`action.failed`. Policy or sandbox gates that stop an action before normal
execution use `action.approval_required` or `action.blocked` instead of being
reported as ordinary failures. For tool-backed actions, `payload.action_type`
may be `"tool"`; that does not change the event family.

## ExecutionResource events

ExecutionResource lifecycle uses `execution_resource.*`. Providers and
DevTools consumers should treat this namespace as extensible. Current manager
events include `declared`, `approval_required`, `ensuring`, `ready`,
`unhealthy`, `releasing`, `released`, and `failed`. `unhealthy` means a ready
handle failed the health check before reuse; the manager releases it and ensures
a fresh handle.

## Runtime Progress And Stall Diagnostics

Event Center is the runtime-event intake and outlet dispatch layer. Liveness
state is updated before expensive hook delivery, so a slow or failing hook must
not block stall diagnostics.

AgentExecution records progress in `async_get_meta()["diagnostics"]`:

- `diagnostics["stages"]["events"]` keeps recent stage progress.
- `diagnostics["last_progress"]` records the latest accepted progress event.
- `diagnostics["timeouts"]` records hard-deadline failures.
- `diagnostics["stalls"]` records idle no-progress failures.

When debugging a live app, attach a temporary Event Center hook or enable
console logs with `.set_settings("debug", True)` for request/result and process
summaries, or `.set_settings("debug", "detail")` for full observation and model
delta output. RuntimeEvent diagnostics and public business text are separate:
also consume `execution.get_async_generator(type="delta")` or call
`await execution.async_streaming_print()` to see the complete readable process
and final result. Remove temporary debug hooks and debug settings after the
issue is understood.

## Compatibility rules

RuntimeEvent records are an extensible framework event protocol. Agently-DevTools and custom consumers should fail open:

- Ignore unknown top-level fields and unknown `payload` fields.
- Do not fail on unknown `event_type` values.
- Do not treat `payload` as a closed schema; it can grow per event type.
- When correlating a request, Session, or TriggerFlow execution, prefer `run` fields over parsing `message`.

## See also

- [TriggerFlow Events and Streams](../triggerflow/events-and-streams.md) — flow control and runtime stream
- [DevTools](devtools.md) — ready-made observation and evaluation bridge
- [FastAPI Service Exposure](../services/fastapi.md) — forwarding runtime streams to service clients
- [Coding Agents](../development/coding-agents.md) — coding-agent guidance through Agently Skills
