---
title: Output Control
description: The output validation pipeline — strict output, ensure_keys, custom validate, retries, and events.
keywords: Agently, output, validate, ensure_keys, retry, max_retries
---

# Output Control

> Languages: **English** · [中文](../../cn/requests/output-control.md)

The validation pipeline runs the first time a structured response result is consumed, then caches the outcome on that response result. It has a fixed order, and each step contributes to the same retry budget.

For Agently `4.1.0.1+`, the default authoring path is: mark fixed required leaves directly in `.output(...)` with the third-slot `ensure` flag, then let the runtime compile those flags into `ensure_keys`. Pass `ensure_keys=` manually only when the required path is runtime-dependent, conditional, or easier to express outside the static schema. By default, tuple `True` and runtime `ensure_keys` check path/key presence only; the value may be `None`, a blank string, `False`, `0`, an empty list, or another intentionally empty value. Use the explicit tuple marker `"not_null"` when a required path must also contain a meaningful value; it rejects `None`, blank strings, empty lists or wildcard matches, and lists containing missing required values while still accepting `False` and `0`.

Tuple ensure policies and supported Pydantic field constraints are rendered
into the initial structured-output prompt, not reserved for post-response
checks. A direct Pydantic output model also remains the typed acceptance
authority: a parsed dict that fails the model is corrected through the shared
retry budget and is never returned as successful business data.

## Direct downstream interface contracts

If downstream code passes the parsed result to an API, SDK, module interface,
or function, `.output(...)` should mirror the consumed request or argument
shape instead of returning an opaque `dict` such as `{"args": (dict,
"arguments", True)}`. Describe each consumed leaf with its contract meaning,
not only a generic label. Include the exact type, requiredness, and any relevant
enum, serialization format, range, unit, nullability, or field dependency.

The complete integration contract also includes the authoritative API docs,
signature, schema, or docstring in `info(...)`, the runtime facts in
`input(...)`, and the transformation/call rules in `instruct(...)`. This is
necessary output control rather than business-logic intrusion. Parsing and
`ensure` checks do not replace deterministic DTO/Pydantic/SDK validation before
a real call or side effect.

## Rule-first business validation

When the model is expected to satisfy a post-generation business validator,
give it every non-sensitive, model-satisfiable acceptance rule before the first
attempt. Put runtime candidates and limits in `input(...)`, authoritative
policy or interface material in `info(...)`, behavior and transformation rules
in `instruct(...)`, and field-level type, requiredness, enum, format, range,
nullability, and cross-field constraints in `output(...)`.

The host-side validator remains authoritative. Pydantic models, `.validate(...)`,
DTO/SDK validation, authorization checks, and side-effect guards must still
reject invalid output. Retry feedback should repair a declared contract; it
must not be the first place where the model learns the rule. An underspecified
generation followed by hard rejection and repeated retries is blind gate
discovery, not a validation strategy.

Security, authorization, anti-abuse, integrity, and holdout gates may keep
sensitive implementation details host-side when disclosure would weaken the
gate or leak an expected answer. Give the model the safe public contract, then
fail closed, stop without retry, require review, or use an explicit fallback;
do not repeatedly reveal hidden rules through correction prompts.

If a requested production gate cannot be stated safely or concretely but the
developer still asks to enforce it, the coding agent must stop before
implementation and explain:

- which rule is missing or intentionally hidden, and which output it affects;
- the expected retry, cost, latency, nondeterminism, and liveness risks;
- safer alternatives and the proposed retry and terminal behavior.

Implementation may continue only after a new developer response explicitly
confirms that named gate and its risks. A previous general instruction to
proceed is not that second confirmation.

## Choosing An Output Format

`.output(...)` reads its omitted format default from
`prompt.default_output_format`, whose global default is `json`. Agent-level and
request-level settings can override that default independently. Set
`prompt.default_output_format="auto"` only when a target model has passed
representative structured-output stability checks.

Explicit `format="auto"` chooses the structured format from the schema shape:
flat string-only dicts use `xml_field`; dicts that mix string fields with typed
non-string fields use `hybrid`; all-complex, all-control, and non-dict schemas
stay `json`. Auto does not inspect business meaning in field names or
descriptions. If downstream code relies on a specific wire shape, set the format
explicitly. `yaml_literal` is explicit opt-in and is not selected by auto.
`flat_markdown` remains explicit-only for compatibility.

| Mode | Use When | Avoid When |
|---|---|---|
| `auto` | You explicitly accept schema-driven format selection and retry latency for a model that has passed stability checks. Good for application code that consumes parsed data through Agently rather than raw model text. | You need the conservative framework default, a legacy consumer, test fixture, external API, or saved prompt expects raw JSON text. Use `format="json"` or leave the default at `json`. |
| `flat_markdown` | Explicit compatibility mode for legacy section-header prompts. | Auto selection, nested lists/objects, arrays of records, or high-reliability parsing. |
| `hybrid` | Explicit format, or auto target, when string prose/code fields are mixed with typed fields. String fields stay as Markdown sections; list/object/boolean/number fields use fenced JSON blocks. | There are no string prose/code fields, every field is compact machine data where JSON is simpler, the target model echoes section scaffolding, or a downstream consumer cannot tolerate Markdown-section raw output. |
| `xml_field` | Explicit format, or auto target, for flat string-only dict schemas. Agently parses this with a custom XML-like parser, not strict XML. | A downstream consumer expects real XML semantics, namespaces, entity escaping, or schema validation. |
| `yaml_literal` | Explicit opt-in for teams that prefer YAML documents and can tolerate YAML indentation sensitivity. Long text/code fields use YAML literal scalars (`|`) inside `<<<BEGIN AGENTLY_YAML>>>` / `<<<END AGENTLY_YAML>>>` boundaries. | General auto mode, low-adherence models, or dense machine contracts where JSON is simpler and less indentation-sensitive. |
| `json` | You need the strictest machine contract, nested data, arrays, interop with external systems, compatibility with old prompts/tests, or exact raw JSON behavior. | Large embedded documents or code blocks make escaping fragile or hard for the model to read. |
| Plain text | The request asks for one freeform artifact: an article, email, explanation, report, Markdown page, HTML page, or other single multi-paragraph document. Do not call `output()`; use `start()` / `async_start()` directly or read `result.get_text()`. | You need separately addressable fields, path validation, `ensure_keys`, typed objects, or downstream branching. |

### Output that may exceed one model window

Use `.ensure_long_output()` on an unstarted `AgentExecution` when one business
result may be larger than a provider output window:

```python
netlist = (
    agent
    .input({"requirements": requirements})
    .info({"component_rules": component_rules})
    .instruct("Generate the complete netlist.")
    .output(
        {
            "components": [
                {
                    "refdes": (str, "unique reference designator", True),
                    "value": (str, "component value", True),
                }
            ],
            "nets": [
                {
                    "name": (str, "net name", True),
                    "connections": [(str, "refdes.pin", True)],
                }
            ],
        },
        format="json",
    )
    .ensure_long_output()
    .get_data()
)
```

The option defaults to off. `.ensure_long_output(False)` disables it on the
same unstarted draft. It belongs to the execution rather than `.output(...)` or
a result getter, so `get_data()`, `get_text()`, `get_data_object()`,
`get_result()`, and generator consumers all observe one frozen policy. Calling
it after the execution starts raises the normal one-run lifecycle error.

The first model request is unchanged. If its normalized terminal is `stop`, the
ordinary one-request result and validation path are used. If the provider
reports `length` / `incomplete`, Agently starts a TriggerFlow-visible
continuation loop. It persists accepted units in the execution's private
TaskWorkspace, reads every write back with its SHA-256 digest, and replays the
final candidate before applying the original Pydantic/schema, ensure, and
custom validation contracts. Each structured update is also checked against
the JSON Schema for its own assembly slot before it can advance the manifest.

Current lossless carriers are plain text and explicit/resolved `json`.
`flat_markdown`, `hybrid`, `xml_field`, `yaml_literal`, and opaque custom
carriers fail before model dispatch when this option is enabled. Structured
continuation retains only values whose delimiters were observed in provider
text. Values created by incomplete-JSON repair have
`completion_source="synthetic_repair"` and are regenerated; they are never
committed as accepted units.

Continuation uses append-only revisions:

- plain text preserves the exact first prefix and appends closed, ordered text
  blocks without fuzzy overlap deletion. Each logical continuation commits
  exactly one text block so the next join is generated from a refreshed,
  accepted continuity suffix; if a response supplies more text updates, the
  first valid block is retained and the tail is regenerated;
- JSON list items and declared values are committed only at trusted completion
  boundaries and after local slot-schema validation;
- the model-visible slot contract is projected from the original Agently output
  declaration, preserving nested array/object shapes and Pydantic length,
  numeric, multiplicity, and pattern constraints. The independently built
  Pydantic slot model remains the authoritative commit validator, so an invalid
  nested value cannot enter the manifest and wait for final validation;
- exact Pydantic list bounds are enforced incrementally. Once an exact list
  reaches `maxItems`, it is no longer offered; an incomplete exact list is an
  ordering barrier for later dependent slots. Structured continuations receive
  the accepted value as bounded, read-only canonical JSON evidence so later
  units can preserve established language, names, and cross-field facts;
- an explicitly closed empty list is retained as an empty-container manifest
  fact. Missing list paths are not synthesized as empty, and an empty-list
  declaration is rejected after any item or prior declaration;
- an explicitly closed empty string is retained by the same rule for text
  slots. Before trusting continuation `is_final`, Agently requires every
  declared ensure path to have a manifest fact; missing required paths continue
  the delivery loop without consuming the caller's final-validation retry
  allowance;
- a closed structured string is one atomic schema value. Once committed it is
  no longer offered to continuation and cannot be appended or rewritten. Model
  a structured value longer than the 4000-character unit bound as an ordered
  chunk list, or use plain text when the result is one freeform artifact;
- when a segment contains a valid contiguous prefix followed by a bad update,
  the prefix remains committed and the rejected tail is regenerated from the
  next `unit_index`; later updates in that tail are never skipped into the
  manifest;
- continuation packets carry the slot value contract, distinguish the private
  zero-based `unit_index` from any business field named `index`, and keep packet
  size small enough to close useful units under provider limits. Each offered
  slot exposes one exact host-issued mnemonic `path_key`, such as
  `p1:components`; no second model-copyable schema path competes with it, and
  the host still authorizes the complete offered key rather than parsing its
  suffix;
- every continuation starts with the small control header `base_revision`,
  `base_digest`, and `anchor`, closing those fields before any business update.
  The anchor is the short digest of the latest accepted unit. For plain text,
  a bounded document start, the exact accepted prose suffix, and the
  host-counted accepted character total are supplied separately as read-only
  continuity context. This preserves global formatting and local joins without
  making the model copy long business text into the control header or estimate
  the accepted length. If a provider `length` terminal arrives before that header
  closes, the attempt is recorded as no progress, the accepted manifest is left
  unchanged, and the next bounded recovery request is told to close the header
  before emitting at most one update;
- stale revisions, wrong digests, unknown paths, non-contiguous unit indexes, and
  rejected updates remain outside the manifest. Three consecutive no-progress
  continuations fail closed with the last reason code instead of looping;
- `LongOutputDelivery`, not an inner `ModelRequest` retry loop, owns continuation
  repair. Each continuation is one physical request. A provider-complete
  response that does not validate as the private envelope is persisted and
  recorded as bounded `continuation_envelope_invalid` no progress; the caller's
  `max_retries` remains reserved for final assembled-value validation;
- a zero-update `is_final` assertion after a provider `length` terminal is still
  no progress, not proof that the truncated business result was complete;
- continuation requests do not inherit Action/tool handlers, so output-only
  continuation cannot repeat side effects;
- the private continuation envelope never appears in the public text stream.

When instant JSON parsing had already deferred a large provisional snapshot,
an authoritative provider-complete final parse remains the final result;
observed-boundary events cannot replace it with the older snapshot. If the
final JSON parse fails or the provider ends by `length`, only genuinely closed
observed values remain eligible for partial-unit acceptance.

If a model incorrectly declares completion while the replayed candidate still
fails the original schema, ensure rules, or a declared validator, Agently keeps
every accepted unit and uses the existing bounded validation-retry allowance to
request only missing or additional units. Integrity failures such as manifest,
readback, digest, or lineage mismatches are never sent to the model for repair;
they fail immediately.

This is deliberately a direct ModelRequest delivery policy. With no explicit
task strategy, it selects the direct route. Combining it with an explicitly
selected AgentTask strategy fails before task execution; keep planning/tool
work in AgentTask and start a separate direct delivery execution for the long
terminal artifact.

`get_meta()["long_output"]` reports request/segment/unit counts, replayed and
rejected unit counts, no-progress event count, final-validation repair count,
the accepted digest, validation status, and the actual guarantee level.
`execution.diagnostics["long_output_no_progress"]` retains bounded per-attempt
reason/header/manifest facts for failed runs without copying raw provider
bodies. Transport and schema completeness do not prove that a business
inventory is exhaustive. Declare an expected count/key/reference rule through
`ensure_keys`, a Pydantic constraint, or `.validate(...)` when coverage must be
proven. Without such a rule, `semantic_exhaustiveness` remains `"not_claimed"`.

See the runnable
[`examples/basic/ensure_long_output.py`](../../../examples/basic/ensure_long_output.py)
for a 75-component JSON inventory with an explicit count/order coverage
validator and a bounded real-model request budget. The recorded 2026-07-28
Qwen run retained all 75 component units across truncated windows and used one
final-validation repair request to add only the missing summary.

### Instant Streaming

Use `get_generator(type="instant")` or `get_async_generator(type="instant")`
when the caller benefits from field-level structured updates before the full
response is finished: progress panels, live forms, long, sectioned, or
file-backed deliverables with independently renderable sections,
model-stage dashboards, or workflow UIs that can route one field while the rest
of the response is still generating. For one
freeform text artifact, use `type="delta"` instead; plain text has no structured
field paths for instant events.

`instant` events are not "final result chunks". They are `StreamingData` patches:

- `path` identifies the field, such as `customer_reply` or `risk_flags[0]`;
- `wildcard_path` normalizes indexes, such as `risk_flags[*]`;
- `delta` is the new fragment for progressive rendering;
- `value` is the parser's current value for that path;
- `is_complete` / `event_type == "done"` marks a field as closed.

Use the stream for provisional UI/progress. Use `get_data()` /
`async_get_data()` after the stream for durable business state; it reads the
cached final parse from the same response and does not issue a second model
request.

| Output Mode | Instant Support | Practical Guidance |
|---|---|---|
| `auto` | Yes, after auto resolves to `json`, `hybrid`, or `xml_field`. | Use only when explicit schema-driven selection is acceptable. If auto later degrades to JSON during final parsing, discard or overwrite provisional UI state with the final parsed result. |
| `flat_markdown` | Yes, field-level text deltas by `### field` sections. | Explicit compatibility mode. Prefer `json` for omitted-format defaults, and use explicit `xml_field` or `hybrid` only when their boundaries fit the target model. |
| `hybrid` | Yes, field-level text deltas by section. JSON block contents stream as text and are parsed into typed values at finalization. | Explicit path for prose/code plus structured records or control fields. Use instant for UI/progress, then use `get_data()` / `async_get_data()` for the finalized typed structure. |
| `xml_field` | Yes, field-level text deltas inside `<field name="..." type="...">` blocks. | Useful when explicit boundaries are easier for the target model than Markdown section headers. Final parsing consumes the normalized answer payload, not provider reasoning. |
| `yaml_literal` | Yes, top-level field deltas inside the target YAML boundary. | Treat as provisional UI state. Final YAML parsing is indentation-sensitive and should be checked through `get_data()`. |
| `json` | Yes, via incremental JSON parsing. | Best when arrays or nested objects need path-level updates. More sensitive to malformed or delayed JSON syntax while streaming; final repair still happens at completion. |
| Plain text / `text` | No structured instant paths. | Use `type="delta"` for text-increment streaming, or `get_text()` after completion. Use `original` / `original_delta` views only when debugging provider-level raw events. |

For latency-sensitive structured generation, put compact independent trigger
records before long explanations or artifacts. A useful order is:

```text
retrieval_tasks
-> bounded generation_plan / risk_checks
-> short user-safe progress_message
-> large_artifact
```

This lets complete `retrieval_tasks[*]` items start bounded preparation early
and lets the first `large_artifact` event map to a stable host-owned
`generating_artifact` status. Generate all independent trigger records first;
do not interleave a long explanation after every item unless that explanation
is a real prerequisite.

`generation_plan`, `evidence_assessment`, or `risk_checks` may improve a complex
result only when a later field, workflow stage, or user-process view consumes
the bounded artifact. Define its type, bounds, visibility, retention, and
failure behavior. Do not request hidden chain-of-thought or add an unconsumed
generic `reasoning`, `analysis`, or `thinking` field.

Incremental JSON parsing can emit
`$status.status == "streaming_parse_deferred"` when a large incomplete buffer
crosses the configured safety threshold. Keep early control fields compact.
Deferred streaming removes the progressive optimization, not final
correctness; final parse and validation remain authoritative. Hybrid typed JSON
blocks stream as block text and become typed values at finalization, so use JSON
when nested path-level early triggers are required.

### Current Format Contracts

Current guidance is based on the implemented parser/prompt contracts and should
be validated against representative target models before broad production
rollout. Experimental runs for format recommendation must store raw outputs and
validate only parsing, required field presence, and structural types. They must
not use tokenization, keywords, or substring matching as the correctness signal
for model-owned content.

| Concern | Contract |
|---|---|
| `auto` selection | Uses schema structure only. It does not inspect field names, descriptions, model output, or business meaning. |
| `flat_markdown` | Explicit compatibility mode only; it is no longer selected by auto. |
| default selection | Omitted `.output(..., format=...)` reads `prompt.default_output_format`; the global default is `json`. |
| `hybrid` | String fields are Markdown sections. Non-string fields are fenced JSON blocks and must parse as JSON values, including booleans and numbers. Explicit `format="hybrid"` or auto can select it for mixed string + typed schemas. Current qwen2.5:7b stability checks found scaffold/header omissions and copied scaffold comments, so keep it explicit unless the target model has passed representative tests. |
| `xml_field` | Uses one `<agently_output>` payload with `<field name="..." type="text|json">` blocks. The parser is XML-like and boundary-based, not strict XML. Explicit `format="xml_field"` or auto can select it for flat string-only dict schemas. |
| `yaml_literal` | Uses a target YAML boundary and literal scalars for long text. It is explicit opt-in and remains outside auto by default. |
| reasoning text | Provider-native reasoning and leading outer `<think>...</think>` content before the payload are normalized to reasoning events before parsing. Payload/code/text-internal `<think>` content is preserved. |
| tuple `ensure` | Third-slot `True` compiles to `ensure_keys` and checks path/key presence. Third-slot `"not_null"` opts into strict value presence: `None`, blank strings, empty lists or wildcard matches, and lists containing missing required values retry; `False` and `0` remain valid. |

Typical usage:

```python
# Default: json, read from prompt.default_output_format.
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# Per-agent opt-in: omitted .output(..., format=...) now uses auto.
agent.set_settings("prompt.default_output_format", "auto")
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# Force JSON when a downstream contract expects raw JSON-like structure.
agent.input("Extract invoice fields.").output({
    "vendor": (str, "vendor name", True),
    "line_items": [{"sku": (str,), "amount": (float,)}],
}, format="json").start()

# Explicit hybrid when prose/code fields are mixed with records.
agent.input("Create an EDA netlist with design notes.").output({
    "analysis": (str, "one paragraph design rationale", True),
    "components": [{"refdes": (str, "reference designator", True), "value": (str, "part value", True)}],
    "nets": [{"name": (str, "net name", True), "connections": [{"refdes": (str, "refdes", True), "pin": (str, "pin", True)}]}],
}, format="hybrid").start()

# XML-like field envelope for long text mixed with typed records.
agent.input("Create lesson material.").output({
    "lesson_script": (str, "long lesson script", True),
    "environment_checklist": [{"item": (str,), "why": (str,), "command": (str,)}],
    "final_confirmation": (str, "one sentence", True),
}, format="xml_field").start()

# Plain text: one artifact, no structured parser.
html = agent.input("Write a complete landing page as HTML.").start()
```

Progressive UI example:

```python
result = (
    agent
    .input("Turn this incident note into a customer-safe update: ...")
    .output(
        {
            "status_summary": (str, "one sentence status", True),
            "risk_flags": [(str, "risk flag", True)],
            "customer_reply": (str, "customer-safe reply", True),
        },
        format="json",
    )
    .get_result()
)

ui_state = {}

async for item in result.get_async_generator(type="instant"):
    if item.delta:
        ui_state[item.path] = ui_state.get(item.path, "") + item.delta
        await websocket.send_json({
            "path": item.path,
            "delta": item.delta,
            "done": item.is_complete,
        })

final = await result.async_get_data()
await save_case_update(final)
```

## The pipeline

```text
   model returns text
        │
        ▼
1. parse / repair         ← extract structured object from text
        │
        ▼
2. Pydantic validation    ← original declared BaseModel, when used
        │
        ▼
3. strict output          ← match against .output(...) shape; ensure_all_keys checks if set
        │
        ▼
4. ensure_keys            ← per-leaf required-path checks (compiled from the ensure flag)
        │
        ▼
5. custom validate        ← .validate(handler) and validate_handler= business rules
        │
        ▼
   pass → return result   |   fail → retry (if budget remains) → top of pipeline
```

A retryable failure at any step retries the request. Pydantic failures add bounded
field-level correction messages to the next attempt. A retryable custom
validator failure adds its bounded `reason` to the same correction prompt.
Retries share one budget controlled by `max_retries` (default `3`). When the
budget is exhausted:

- A Pydantic model violation always raises; an invalid dict is not an accepted
  value, even when `raise_ensure_failure=False`.
- For strict-shape or ensure failures, `raise_ensure_failure=True` (the default)
  raises, while `False` returns the latest parsed result.

## Where validate plugs in

`.validate(handler)` registers a custom check. It runs **after** strict output and `ensure_keys` have already passed, on a canonical dict snapshot of the result.

When a retryable handler result is not accepted, Agently sends its `reason`
(up to 300 characters) to the next model attempt and asks for a complete
replacement output. The optional validation `payload` and handler exception
details remain host/runtime diagnostics and are not automatically copied into
the model prompt.

```python
def must_be_short(result, ctx):
    if len(result.get("answer", "")) > 280:
        return {"ok": False, "reason": "answer too long", "validator_name": "length"}
    return True

agent.input("Summarize.").output({
    "answer": (str, "answer", True),
}).validate(must_be_short).start()
```

The handler runs only on structured-result getters: `start()`, `async_start()`, `get_data()`, `async_get_data()`, `get_data_object()`, `async_get_data_object()`. It does **not** run on `get_text()` / `get_meta()` (those don't carry the parsed structure that validate would inspect).

## Ordered Fields And Evaluation Levels

Agently output schemas are ordered. When later fields depend on earlier
judgment, put support fields first: evidence, assumptions, clarifications,
source notes, calculation plans, concise rationale, rule checks, and
intermediate facts. Put final booleans, verdicts, replies, summaries, and action
decisions last. User-facing renderers can reorder sections for natural reading,
but the model generation contract should keep support-before-conclusion order.

For model-owned grading, confidence, trust, relevance, usability, or quality
judgments, prefer conceptual levels with explicit definitions over precise
numeric scores. For example, ask for `high_trust`, `moderate_trust`, or
`low_trust`, and define each level in the prompt. If downstream code needs a
score for thresholds, weighting, statistics, or index calculations, map levels
to deterministic numbers in code after generation.

For complex arithmetic, long-number calculation, weighting, aggregation, or
statistical transformations, ask the model for an executable calculation plan or
code, run it with tools, and pass the original question, code, and observed
result into the next model step. Do not make text generation be the calculator.

You can also pass handlers per-call:

```python
agent.input("...").output({...}).start(validate_handler=must_be_short)
agent.input("...").output({...}).start(validate_handler=[check_a, check_b])
```

`.validate(...)` handlers run before `validate_handler=` handlers. Multiple `.validate(...)` calls preserve order.

## Handler return shape

| Return | Meaning |
|---|---|
| `True` | pass |
| `False` | fail — retry if budget remains |
| `dict` | structured result; see keys below |

Supported `dict` keys:

| Key | Effect |
|---|---|
| `ok` | `True` = pass, `False` = fail |
| `reason` | text shown in retry events / error messages |
| `payload` | structured details for downstream consumers |
| `validator_name` | tag the validator in events |
| `no_retry` / `stop` | fail but don't retry |
| `error` / `exception` / `raise` | fail with the given exception |

Anything not in this list becomes a `model.validation_error` and consumes retry budget.

## Async handlers

Both sync and async handlers are supported. An async handler signature:

```python
async def check_remote(result, ctx):
    ok = await some_external_check(result["answer"])
    return ok
```

## Context object

The second argument is an `OutputValidateContext` with at least:

- `value`, `input`, `agent_name`, `response_id`
- `attempt_index`, `retry_count`, `max_retries`
- `prompt`, `settings`, `request_run_context`, `model_run_context`
- `response_text`, `raw_text`, `parsed_result`, `result_object`, `typed`, `meta`

Use `ctx.attempt_index` if you want different behavior on later attempts (e.g., loosen the rule on retry).

Treat these fields as observational by default, but `ctx.prompt` and `ctx.settings` are live state objects for the current response-attempt chain. In advanced handlers, if you need to adjust the prompt / options / settings for a **later retry**, you can write back through them inside the validator.

For example, lower sampling parameters on the next retry:

```python
def check(result, ctx):
    if result.get("score", 0) < 0.8 and ctx.retry_count < ctx.max_retries:
        ctx.prompt.set("options", {"temperature": 0.2, "top_p": 0.7})
        return {"ok": False, "reason": "score too low"}
    return True
```

Or change settings:

```python
def check(result, ctx):
    if should_switch_mode(result):
        ctx.settings.set("my_plugin.some_flag", True)
        return False
    return True
```

Two caveats:

- These writes affect **later retries only**. They do not change the current attempt that has already completed.
- These writes also do **not** leak into later fresh requests. Each new `response` is created from a new prompt/settings snapshot at the request/agent layer, so validator write-backs stay inside the current response's retry chain.
- Do not rely on mutating `opts = ctx.prompt.get("options", {})` in place. `get()` returns a view/copy; use write APIs such as `ctx.prompt.set(...)`, `ctx.prompt.update(...)`, or `ctx.settings.set(...)` if you need the change to persist.

## Single execution per response

Validation runs **once** per `ModelRequestResult` and the outcome is cached. Repeated calls — `get_data()` then `get_data()` again, or `get_data()` then `get_data_object()` — do **not** rerun validators. If you try to inject a different handler on the same result after validation has already finalized, the new handler is ignored with a warning.

This means: don't expect to swap validators per consumer. If you need different validation for different consumers, run the request twice.

## Retry events and visibility

Validation contributes two new observation event types:

- `model.validation_failed` — handler returned a fail
- `model.validation_error` — handler raised, returned an unsupported value, etc.

There is intentionally **no** `model.validation_passed` event in phase 1 — passing is the silent default.

The standard `model.retrying` event picks up validation-specific fields when the retry came from validate:

- `retry_reason`, `validator_name`, `validation_reason`, `validation_payload`

Agently-DevTools consumes these defensively. New event keys are additive and should not break existing dashboards.

## Combining with ensure_keys

`ensure_keys` and `.validate(...)` are layered:

- `ensure_keys` handles **path presence** (compiled from the `ensure` flag in `.output(...)`).
- tuple `"not_null"` handles the common built-in **value presence** rule when empty values should retry.
- `.validate(...)` handles **value rules** that depend on the actual content.

For fixed required leaves, prefer `(TypeExpr, "description", True)` in `.output(...)` rather than manually repeating the same paths in `ensure_keys=`. Use `(TypeExpr, "description", "not_null")` only when empty values are invalid for that field. Use manual `ensure_keys` for conditional or runtime-only paths. Use `.validate(...)` for "this field must satisfy this business rule".

## Common patterns

**Loosen on the last retry**:

```python
def check(result, ctx):
    if ctx.attempt_index == ctx.max_retries:
        return True  # accept whatever came back
    return strict_check(result)
```

**Fail without retrying** (e.g., validation reveals a permanent business issue):

```python
def policy_check(result, ctx):
    return {"ok": False, "reason": "policy violation", "no_retry": True}
```

**Raise a custom exception**:

```python
def policy_check(result, ctx):
    return {"ok": False, "raise": MyDomainError("rejected by policy")}
```

## See also

- [Schema as Prompt](schema-as-prompt.md) — `.output(...)` authoring and `ensure` flag
- [Model Response](model-response.md) — what cached vs re-runnable means in practice
- [Glossary: ensure](../reference/glossary.md#ensure-third-tuple-slot)
