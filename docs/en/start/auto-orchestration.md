# Agent Auto-Orchestration

> New 4.1.4.9 long tasks use the [unified Loop](long-task-loop.md). Flat/TaskBoard card scheduling, goal preparation and legacy task recovery below apply only to explicit 4.1.x compatibility strategies.

Agently 4.1.4 makes `agent.start()` the default user-layer entrypoint for an
Agent turn. It keeps returning the business result, while the Agent can route
through ordinary model response, Actions, or SkillLibrary-backed Skills
execution when those capabilities were explicitly injected.

```python
result = (
    agent
    .use_actions([market_data_action])
    .use_skills_packs(["equity-research"])
    .input("Review this renewal risk.")
    .output({"answer": (str, "final answer", True)})
    .start()
)
```

Candidate injection is the boundary. If no Actions, Skills, or Skills Packs are
registered, `agent.start()` remains an ordinary model request.

`TaskDAG` is the foundation DAG capability. `DynamicTask` remains a
compatibility and convenience facade over DAG planning/execution, not a second
recommended task lifecycle and not an AgentTask auto-strategy route. Use
TaskDAG / DynamicTask when the application or a visual automation surface owns
the graph shape and wants to run that graph explicitly.

Quick prompt chains create execution-scoped drafts. The Agent can be kept as a
service singleton for shared settings, model activation, Actions, Skills,
TaskWorkspace, and `define(...)` / `always=True` prompt, while `.input(...)`,
`.system(...)`, `.output(...)`, attachments, and per-execution options in one
chain are written to an isolated `AgentExecution` draft:

```python
results = await asyncio.gather(
    agent.input("Summarize request A").async_start(),
    agent.input("Summarize request B").async_start(),
    agent.input("Summarize request C").async_start(),
)
```

Capability configuration chained after a quick prompt stays on that same
unstarted execution. For example,
`agent.input(...).info(...).use_action(...)` retains the input and info; it does
not silently create a replacement execution.

For multi-statement setup, capture the execution draft explicitly:

```python
execution = agent.create_execution()
execution.input("Review this renewal risk.")
execution.output({"answer": (str, "final answer", True)})
result = await execution.async_start()
```

Do not rely on `agent.input(...); agent.output(...); await agent.async_start()`
for execution prompt accumulation. Use `always=True`,
`set_agent_prompt(...)`, or stable setup methods for Agent-lifetime state; use
`agent.define(...)` for reusable Agent definition state; use
`agent.create_request(...)` / `agent.request` only when you intentionally want
the lower-level request-builder surface.

Accepted development-line routing is candidate-driven and deterministic-first.
Required Skills bind immutable guidance into `TaskContext`; concrete
model-response consumption is recorded separately from executable capability
evidence. Ordinary Actions run through the normal `model_request`
AgentExecution action loop. Its default rounds choose either Actions or a final
response. A terminal response is delivered through that same AgentExecution;
an extra final-generation request is used only for legacy/custom fallback or a
separate delivery policy such as `auto_continue`. Skills never create a
route or planner capability.

The public Agent API stays in core. Its factory directly constructs the selected
AgentExecution plugin, which owns production and final policies. Skill Context
and the DAG substrate remain replaceable components within that execution.

## Human Interaction

Use the standard `.interact(handler)` method when this AgentExecution should
answer connected human-in-the-loop exchanges through a request-local callback:

```python
def handle_exchange(exchange):
    if exchange["kind"] == "approval":
        return {"status": "approved", "approved": True}
    return {"audience": "framework developers"}

result = (
    agent.create_execution("plan")
    .input("Plan the release.")
    .interact(handle_exchange)
    .start()
)
```

The handler receives one normalized `ExecutionExchangeView` with stable fields
such as `kind`, `subject`, `payload`, and `request`. It may be synchronous or
asynchronous and returns the response payload expected by the exchange:
approval handlers commonly return a Boolean or decision mapping, while
clarification handlers may return text, a list, or a mapping.

`.interact(...)` declares the response mechanism; it does not force an
interaction. An ordinary request that opens no exchange never calls the
handler. The declaration is isolated to that AgentExecution, selects connected
interaction for its request, survives pre-start execution configuration, and
does not register or replace a global provider. Calling it again before start
replaces the previous handler.

The existing owners remain unchanged. ExecutionExchange owns the normalized
request/provider envelope, and TriggerFlow owns pause/resume. Use registered
ExecutionExchange providers, routing handlers, and `interaction.*` settings for
durable queues, webhooks, cross-process hosts, or application-wide routing;
these advanced transport choices are intentionally not keyword arguments on
`.interact(...)`.

## Review And Final Validation

Use `.review(handler=None, *, rules=None, on_fail="warn")` to assess the final
result and its key artifacts. `rules` accepts one string or a sequence of strings.
A sync/async handler receives `(result, context)` and replaces the whole evaluator;
its context includes the rules, original contract, trusted artifact refs, and
TaskWorkspace. Return a Boolean or a mapping with Boolean `passed`.

```python
result = (
    agent.input(task).output(contract)
    .review(rules=["Check that conclusions are supported by the supplied evidence."])
    .start()
)

result = (
    agent.input(task).output(contract)
    .validate(release_check)  # (value, context) -> bool or {"ok": bool, ...}
    .review(on_fail="block")
    .start()
)
```

`on_fail="warn"` records a failed verdict without changing the result or success
status. `"block"` raises `AgentReviewError` and prevents terminal success. This is
host behavior, not a stronger model rubric. Neither mode revises or retries;
`"retry"` is unsupported. There is no public Agent/AgentExecution `verify()`.

Without a handler, the reviewer reads complete trusted text artifacts through
TaskWorkspace, verifies their content version, and makes one structured model
request. Identical text candidate/artifact content is sent once. Non-text,
unreadable, changed, or incomplete artifacts produce `not_assessable`, not a
successful metadata-only review. Use a suitable custom handler for other formats.
No progressive or segmented model review is implied; context overflow is an
explicit request failure. Plans are judged as plans, including accepted
clarifications, not as already-executed tasks. Missing goals are not invented.

Reports contain `passed`, `quality_level` (`strong`, `adequate`, `weak`, or
`not_assessable`), `summary`, one `checks[]` entry per supplied rule, structured
`issues[]` (`criterion`, `finding`, `evidence`, `suggestions[]`), and
`overall_suggestions[]`. Boolean handlers leave `quality_level=None`; they do not
manufacture a rating. Numeric model scores are not used. Read reports from
`(await execution.async_get_meta())["reviews"]`; events include `review.started`,
`review.completed`, `review.warning`, and `review.blocked`.

`validate(handler)` is the hard gate for the **final output of the current call**:
a direct response, final plan, host-assembled document, AgentTask final output,
or custom Execution's returned value. It never automatically checks intermediate
producer/task outputs. Direct ModelRequest validation and `auto_continue`
retain their existing controlled repair behavior. Other final checks run once
without replaying internal steps or side effects. Their context uses
`meta.scope="agent_execution_final"`, no provider response ID, and
`max_retries=0`. Validation precedes declared artifact delivery and review;
it does not undo side effects already performed by a task. Use explicit
TriggerFlow orchestration for a safely designed repair loop.

## Artifact Delivery

Use `.artifact(path, handler=None)` when the accepted business result must also
be delivered as a verified file:

```python
result = (
    agent
    .input("Prepare the launch report.")
    .output(report_contract)
    .artifact("reports/launch.json")
    .review()
    .start()
)
```

The method remains useful for ordinary text or structured requests; it is not
limited to workspace-oriented AgentTask runs. Its trust boundary is always the
Agent's TaskWorkspace. Strings are written unchanged, while mappings, lists,
tuples, and JSON primitives use readable JSON. A custom synchronous or
asynchronous handler receives `(result, context)` and returns `str` or `bytes`:

```python
agent.input(task).artifact(
    "exports/result.bin",
    lambda result, context: encode_result(result),
).start()
```

The handler only renders content. TaskWorkspace owns path containment, write
permission, physical digest readback, trusted file identity, and terminal
retention. In a read-only workspace, a new requested path is written to the
execution's private fallback area; inspect the trusted ref's `path` for the
actual location. In a read-write workspace, the requested path is used.

Artifact delivery does not replace or wrap the business result. Trusted refs
are exposed in `meta["logs"]["artifact_refs"]` and through
`artifact.started` / `artifact.completed` stream events. Multiple calls create
multiple independently verified files. Any declared delivery failure fails the
run; artifact materialization completes before review.

## Execution Plugins

An Agent owns reusable configuration and capabilities. An AgentExecution plugin
owns one isolated draft, production lifecycle, result, and final policies.
`agent.create_execution(name)` returns an instance of the registered class.

| Name | Production behavior | Explicit strategies |
| --- | --- | --- |
| `auto` (default) | Select the existing request/task/DAG route when execution starts | Existing route strategies |
| `request` | One request, including request-local parsing and repair | `auto`, `direct` |
| `long_task` | Retained multi-step goal work | `auto`, `task`, `task_loop`, `long_task`, `flat`, `taskboard` |
| `plan` | Readiness, connected clarification when needed, then a final plan | `auto` |
| `long_content` | Section planning, dependent writing, and host-ordered assembly | `auto` |

Explicit selection takes precedence over the configured default. Incompatible
strategy combinations fail before model or Action dispatch. A route policy
cannot silently replace an explicitly selected plugin with another producer.
The ordinary fluent API keeps deferred `auto` selection; registration alone
does not add model calls, review, or planning.

```python
execution = (
    agent.create_execution("plan")
    .input("Plan a workshop using the supplied facts.")
    .info(workshop_facts)
    .interact(handle_exchange)
    .output(plan_schema)
    .validate(validate_plan)
)
plan = execution.start()
meta = execution.get_meta()
print(meta["plugin"], meta["route"]["selected_route"])
```

A caller's `validate(...)` hard-checks the producer's final returned value,
not an intermediate readiness response, section plan, or task step. Direct
request repair remains request-owned. Artifact materialization/readback and
optional review follow final validation on the same execution.

`plan` uses bounded readiness and planning requests. Readiness also receives
the final `output(...)` field descriptions and constraints, while returning its
own readiness judgment rather than filling the final plan. Final planning uses the
original request and accepted clarification replies directly, without requiring
a separate restatement of the goal and deliverable. A connected handler
receives an `ExecutionExchangeView` when clarification is required.
Missing/rejected answers and exhausted clarification attempts produce an
explicit blocked outcome; they are not fabricated as accepted answers.
This built-in connected flow does not yet promise durable plan restoration.
Its settings are `plugins.AgentExecution.plan.max_clarification_rounds` (default 3) and
`max_questions_per_round` (default 3).

`long_content` plans sections, writes each body, and records one actual
chapter-level summary only when a later chapter consumes it. The Host retains
the complete heading directory and assembles bodies in order without model
recopy. Configure `plugins.AgentExecution.long_content.max_sections` (default
12). The whole-document Execution returns text and rejects structured
`output(...)`. For a long-form field inside a structure, use
`(LongContent, "writing requirements")` or the compatible
`("long_content", "writing requirements")` on an ordinary request; the framework
generates that body separately and fills the original structure. See
[field-level declarations](../requests/output-control.md) for this distinct scope.
Chapter requests use conditional continuation; normal completion adds no request.
Root `.auto_continue()` does not request the assembled document again.
Released `.ensure_long_output()` remains an alias to the same policy. Long-form
production and request continuation are separate concerns; general in-structure
long-string continuation still has open acceptance work.

Custom implementations register in the same `AgentExecution` category.
Subclass a bundled implementation to reuse its lifecycle and specialize the
protected, typed production hook on the same instance:

```python
from agently.builtins.plugins.AgentExecution import RequestExecution, ProductionOptions

class AuditedRequest(RequestExecution):
    name = "audited_request"

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        route, value = await super()._async_produce(options)
        self.logs["audit"] = {"produced": True}
        return route, value

agent.plugin_manager.register("AgentExecution", AuditedRequest, activate=False)
execution = agent.create_execution("audited_request").input("Explain the migration.")
result = execution.start()
```

Use `activate=True` only to change the configured default. A complete custom
implementation can instead implement the public protocol. `run/async_run`
and compatible `start/async_start` readers share once-only production;
captured result readers do not dispatch a second execution.

Metadata exposes `plugin`; multi-request producers emit
`execution.stage.started/completed` and record stages under
`diagnostics.execution_run`. Types are available from
`agently.types.plugins` and `agently.types.data`, not added as root exports.
The released AgentOrchestrator activation path is a compatibility adapter;
the default no longer needs it. The unreleased `pattern()` selector and
AgentPattern plugin category have been replaced, not kept as parallel APIs.

Local Ollama Qwen examples:

- [Plan with connected clarification](../../../examples/agent_auto_orchestration/26_plan_execution_interaction_ollama.py)
- [Long content with artifact readback and review](../../../examples/agent_auto_orchestration/27_long_content_execution_artifact_ollama.py)

## Goal Pursuit

`agent.goal(goal_or_goals, success_criteria=None, *, turn_on_long_task=True)`
declares semantic goals and criteria. The default also enables bounded long-task
execution in the ordinary auto route. `turn_on_long_task=False` contributes the
same Prompt declarations without enabling that convenience path; it does not
disable an independently selected long_task plugin or strategy. Explicit
`direct` strategy and explicit plugin selection remain authoritative.
`agent.goals(...)` is a plural alias. Neither declaration automatically enables
review. Repeating goal() before starting replaces its convenience switch.

If an already selected long-task producer lacks goals or success criteria, it
asks the model to interpret only the missing fields before constructing its
task state. Explicit declarations and the original Prompt stay unchanged;
metadata records model provenance. Derived criteria cannot authorize new work
or invent business thresholds. Insufficient facts produce a blocked outcome.
Interpretation includes the final output contract (field descriptions, format,
and requiredness); no empty contract is added when output is undeclared.
Preparation consumes the same execution's model-request and time budgets;
constructing the task does not restart the deadline. A timeout before task
creation raises `RuntimeStageStallError`; after creation, the task retains its
`timed_out` result envelope and uses the remaining execution time.
Complete/restored contracts skip this node. Ordinary requests and review do
not gain a preflight call; plan/long-content keep their own planning stages.
See [missing-goal preparation](../../../examples/agent_auto_orchestration/28_missing_goal_preparation_ollama.py)
for a recorded 27B success and unsuccessful 9B outcomes; one run is not a
stability guarantee.

When task-specific options are assembled separately, attach them through the
task strategy:

```python
execution = agent.goal(goal, success_criteria).strategy("auto", options=options)
```

Here the nested `options` mapping belongs to AgentTask. Passing the same mapping
to `agent.create_execution(options=options)` configures AgentExecution instead
and is not the documented task-option path.

```python
result = (
    agent
    .use_skills("website-builder", "seo-reviewer")
    .use_actions(write_file, read_file)
    .require_actions("write_file")
    .goals(
        [
            "Build a small product website.",
            "Prepare a launch checklist.",
        ],
        success_criteria=[
            "The final artifact is a runnable page file.",
            "The page content covers every supplied business fact.",
            "Execution evidence includes file write, readback, and content inspection.",
        ],
    )
    .effort(
        "high",
        budget={
            "iteration_limit": 4,
            "model_call_limit": 10,
            "wall_time_seconds": 300,
        },
        planning={"depth": "expanded", "max_plan_items": 8},
        verification={"strictness": "strict"},
        replan={"policy": "on_verification_failure", "limit": 2},
        progress={"detail": "phase"},
    )
    .start()
)
```

Simple code can keep using `.effort("low" | "medium" | "high")`. The expanded
form keeps the same owner: effort controls strategy and resource intensity, not
whether an execution is a goal-pursuit task. `budget.iteration_limit`,
`model_call_limit`, and `wall_time_seconds` are soft strategy metadata: they
may shape planning, reflection, repair posture, and evidence depth, but they do
not silently set task-strategy `max_iterations` or AgentExecution hard limits. Use
explicit task options or `limits={...}` when the host needs hard resource
controls. By default, AgentTask does not impose model-request, iteration,
TaskBoard tick, or Action round quotas; no-progress and idle timeouts remain
liveness guards for stuck executions, not strategy evidence. Completion still
requires both model verification and host guards.
For task-strategy executions, effort also controls reflection density:
`low` records the final reflection and only planner-marked important process
points, `medium` reflects after each major task node or TaskBoard card/tick, and
`high` reflects after every framework-observable bounded step, Action/ACP call,
TaskBoard card, and final result. Reflection records stay in task memory and
runtime observation output by default and may enter verifier/replan input, but
they are not completion evidence by themselves.

`execution.step_plan` is retained only as compatibility guidance. Users
normally do not need to spell it out. AgentTask no longer uses TaskDAG /
DynamicTask as an internal bounded step strategy; legacy `dynamic_task` /
`execution_dag` step proposals and `execution={"step_plan": "dag"}` are
degraded to direct bounded execution with diagnostics. Use TaskDAG / DynamicTask
separately when the host owns a submitted or visual automation graph.

## AgentTask Strategy

Use `agent.create_task(...)` when the business goal needs a bounded multi-round
loop instead of one direct AgentExecution. It returns a task-strategy
`AgentExecution` draft; internally the retained `AgentTask` record runs one task
owned by one Agent: plan, execute one bounded step, collect bounded evidence,
verify, replan when needed, then finish as complete or blocked.

The retained runtime is one TriggerFlow lifecycle graph with visible nodes:
`lifecycle.start`, `context.prepare`, `work.plan`, `work.execute`,
`outputs.materialize`, `evidence.ingest`, `terminal.verify`, and
`transition.decide`. Stage events carry only `task_id`, monotonic
`state_version`, `frame_id`, `iteration`, and phase-specific `plan_id`,
`work_result_id`, or `evidence_ref` values. Prompt bodies, artifact bodies, and
full evidence objects stay in their owning request, TaskWorkspace, or host frame.
Every consumer rejects stale versions and cross-task signals. A terminal stage
result emits directly to `transition.decide`; it does not traverse unexecuted
stages. AgentTask seals and gracefully drains the execution before reading the
terminal state, so accepted internal signals finish without provider-style
early cancellation.

Internally, `flat` and `taskboard` are coordination strategies, not separate
execution carriers. Both lower strategy-owned work units through the internal
Block carrier into `ExecutionPlan` / Blocks / TriggerFlow evidence. The
TaskBoard primitive still owns board scheduling, dependency state, and patch
validation; AgentTask uses the carrier for bounded card execution evidence.
TaskBoard scheduling now defaults to the event-driven `frontier` mode: each
completed card can unlock and dispatch its ready successors immediately, while
fan-in cards still wait for all declared dependencies. Use
`taskboard_scheduler="batch"` only when you need the historical tick-batch
behavior for diagnostics or regression comparison.
TaskBoard remains the work-producer subflow owned by `work.execute`; after it
produces a structured iteration result, the outer graph owns
`outputs.materialize`, `evidence.ingest`, `terminal.verify`, and
`transition.decide`. TaskBoard does not finalize, verify, or run its own repair
loop inside `work.execute`. Flat and TaskBoard therefore converge on the same
terminal owners without duplicating materialization, evidence, or verification
responsibility.

In the current 4.1.3 line this is a hardened bounded public AgentTask strategy.
`agent.create_task_loop(...)` remains a compatibility spelling for the same
task strategy when code wants to make the strategy choice visible. Both APIs still return `AgentExecution`; new code should
consume data, text, stream, metadata, status, and task refs through
`execution.get_result()` or the execution stream/meta facade instead of treating
`AgentTask` as a second public lifecycle.

While a task-strategy `AgentExecution` is still running, host code may add
non-blocking operator context with `await execution.async_add_guidance(...)` or
`execution.add_guidance(...)`. Guidance is recorded immediately in running task
memory, surfaced through runtime events and `guidance_items`, and applied at the
next safe Flat or TaskBoard boundary. It does not create a RecordStore record by
default, pause execution, mutate non-task route prompts, or count as completion
evidence.

```python
execution = agent.create_task(
    goal="Prepare the incident summary.",
    success_criteria=["The answer reflects the latest operator context."],
    execution="flat",
)

run_task = asyncio.create_task(execution.async_get_data())

await execution.async_add_guidance(
    "Use the newly uploaded incident note as primary context.",
    author="operator",
)

data = await run_task
assert guidance["storage"] == "memory"
```

`AgentExecution.strategy("auto" | "direct" | "flat" | "taskboard")` is the
top-level route/execution selector. `direct` forces the ordinary
`model_request` route with the ActionLoop and does not create an AgentTask, even
when goal-like fields are present; host code owns any completion validation on
that route. `auto` is the default: ordinary prompt/action runs stay direct,
while explicit goals, success criteria, task options, Skill selectors, or other
task signals enter AgentTask. Once AgentTask is selected, `execution="auto"` is
the default task execution strategy: AgentTask asks the model for a
natural-language task-shape analysis plus a thin structured `execution_hint`,
then the strategy policy resolves the actual shape to `flat` or `taskboard`.
The hint is only strategy evidence; TaskBoard does not classify the task, and
the verifier cannot accept the hint as completion evidence. Use
`execution="flat"` or `.strategy("flat")` to force the linear loop, and
`execution="taskboard"` or `.strategy("taskboard")` only when the host
explicitly wants TaskBoard. Nested AgentExecution instances inherit the parent
strategy context unless the child explicitly calls `.strategy(...)`.

Auto may reuse a validated minimal board shape from task-shape analysis or fall
back to Flat when the proposed board is only a small linear sequence with no
real dependency, parallelism, readback, or recovery value. Explicit
`execution="taskboard"` still preserves TaskBoard. With an explicit final
TaskWorkspace delivery contract, TaskBoard may promote a completed terminal
candidate directly to verification, followed by Host delivery and readback.
Ordinary candidates go through the loop finalizer for their semantic completion
decision; a completed leaf card alone does not establish task completion.
Explicit delivery, capability and required-context contracts, and deterministic
integrity or lifecycle blocks still trigger terminal verification.

```python
agent.language("en")

execution = agent.create_task(
    goal="Upgrade a legacy Agently script so it runs on the current 4.1.x API.",
    success_criteria=[
        "The original failure is recorded.",
        "The script no longer uses incompatible legacy API calls.",
        "The fixed script runs and produces the expected structured result.",
    ],
    workspace="./legacy-script-project",
    max_iterations=4,
    verify="before_done",
    options={
        "agent_task": {
            "stream_progress": True,
            "stream_progress_background": True,
            "stream_snapshots": True,
            # Optional: use a separate model key to narrate progress from snapshots.
            # Omit this key to use template progress with no model requests.
            # "progress_model_key": "cheap-progress-model",
            # Optional compatibility alias for progress narration only.
            # "progress_language": "en",
        },
    },
)

result = execution.get_result()

async for item in result.get_async_generator():
    if (item.meta or {}).get("stream_kind") == "progress_delta":
        print(item.delta or "", end="", flush=True)
    elif (item.meta or {}).get("stream_kind") == "progress":
        print("[PROGRESS]", item.value["message"])
    elif (item.meta or {}).get("stream_kind") == "snapshot":
        print("[SNAPSHOT]", item.path, item.value["snapshot"])

data = await result.async_get_data()
meta = await result.async_get_meta()
task_refs = result.task_refs
```

AgentTask process state stays in memory and runtime logs by default. Planning,
observations, verification, evidence links, reflections, and TaskBoard
checkpoints do not materialize RecordStore records merely because a task runs.
The next iteration receives a bounded in-process ContextPackage, while trusted
file products still use TaskWorkspace write/readback and terminal retention.

Set `options={"agent_task": {"record_store_recovery": True}}` only when the host
needs restart-safe continuation. That option persists a compact resumable
snapshot; it does not turn TaskWorkspace into a complete process-event or audit
archive.

TaskBoard checkpoints also include bounded orientation projections for long
runs: an acceptance index over declared criteria/card refs and a handoff
projection with active/setback/blocked/deferred cards, evidence refs, artifact
refs, and explicit state facts. `setback` means a recoverable execution setback
such as readback, repair, patch, or continuation work; one occurrence is not a
hard stop. Each non-satisfying execution of the same required card contract
(`setback`, `failed`, or `blocked`) with the same stable subject counts toward
terminal convergence. The third occurrence ends the task as blocked before a
fourth execution of that contract can be scheduled.
These projections help resume or inspect the board without replaying raw traces.
They are not `EvidenceEnvelope` evidence and do not accept the task; semantic
completion still belongs to the verifier plus host guards.

AgentTask verification remains model-owned, but completion acceptance is
conservative. The loop normalizes verifier output and will not accept a task as
complete when missing criteria remain, required action evidence failed or was
blocked, approval is still required, or a required final deliverable is absent.
Those guard decisions are recorded in task diagnostics so the next iteration can
replan from concrete evidence instead of accepting a weak completion claim.

The task-strategy AgentExecution stream emits structured result events and, by
default, compact intermediate `snapshot` items. Natural-language `progress`
items are opt-in with
`options={"agent_task": {"stream_progress": True}}`; the built-in descriptions
are template-based when no `progress_model_key` is configured, so they do not
add model requests or token usage. When `progress_model_key` is set, AgentTask
uses that separate model key in a background task to summarize the already
emitted snapshot and task metadata. Model-generated progress is streamed as
`stream_kind="progress_delta"` delta events while the sentence is being written,
then emitted once as a complete `stream_kind="progress"` item for stable logs
and UI state. Set `options={"agent_task": {"progress_language": "zh-CN"}}` to
control progress narration for one execution, or set `agent.language("zh-CN")`
as the preferred Agent-level policy for final output, process text, progress
text, and Search/Browse locale defaults. `execution.language("zh-CN")` applies the
same policy to one AgentExecution draft. `progress_language` and
`agent_task.progress.language` remain compatibility controls for progress text
only; `auto` keeps the framework default. The main loop does not produce extra fields for progress
narration and does not wait for progress narration to finish.
Progress narrator failures are side-channel diagnostics and warning-level
runtime events; they do not turn the main execution into `model.request_failed`.
Model progress narration receives an operator-safe snapshot; developer
diagnostics such as low-level TaskWorkspace/SQLite fallback details remain in
snapshots and `task.meta()["diagnostics"]`, but are omitted from the progress
model input.

For text consumers, `get_async_generator(type="delta")` remains the public text
stream. In task-strategy executions it includes model-generated text increments
and also projects selected process events into operator-readable paragraph
text: template progress, snapshots, phase status, action observations,
Flat plan/action summaries, TaskBoard status tables, retry markers, and the terminal task
result. These public text projections intentionally summarize instead of
dumping raw JSON payloads: Action inputs, credentials, and raw command JSON are
omitted; bounded result summaries may be shown; recoverable failures are
presented as setbacks; and the terminal headline is followed by the bounded
`final_response` when available. Process
paragraphs are separated from model body deltas so CLI-style consumers do not
print glued-together text. Internal AgentTask child/control model fields stay in
the structured `instant`/`all` streams and detailed diagnostics; they are not
concatenated into public delta. Long terminal text is truncated, and a
structured final object is referenced through the full result stream instead
of being serialized as raw JSON. Flat projections are linear display-only summaries:
plan completion states the previous completed action and current action plan.
Direct ModelRequest and Skill steps stay at that high level. Only a concrete
Action batch that the framework has normalized and is about to dispatch expands
into `Next Action`, ordered-batch, parallel-batch, or neutral-batch rows; the
label comes from the Action owner's real concurrency policy. Live Action
started/completed/failed phases then update the same bounded calls, and terminal
output reports completed stages plus acceptance. TaskBoard tables
remain display-only projections from structured AgentTask events: the first
TaskBoard projection renders a compact table, and later ticks summarize
card-state changes instead of reprinting the whole table. TaskBoard state is
summarized as not started, in progress, completed, failed, or degraded;
completion and quality still come from verifier and host-guard facts, not from
the projected text. Emoji are redundant visual labels paired with explicit
words; they do not change task state. Long results stay in TaskWorkspace/Action
artifacts. Delta renders at most three compact links only when AgentTask has
verified a TaskWorkspace file by path, bytes, digest, and physical readback; an
unverified or model-declared path is never rendered as an openable link.
Use `type="instant"` when the UI needs the original structured event payloads
with `path`, `value`, `delta`, `is_complete`, and `meta`. When a structured
execution item can also be represented as natural-language stream text,
`instant` yields the original item first and then an additional synthetic
`AgentExecutionStreamData` item at `path="$delta"`. The synthetic item has
`event_type="delta"`, `source="agent_execution"`, and
`meta["stream_kind"] == "text_projection"` with source-path metadata. It is a
consumer projection only: `type="all"` stays the raw audit stream and does not
include synthetic `$delta` items. Heartbeat items are intentionally
structured-only in `instant`: they do not append synthetic `$delta` text.
For richer UI, consume `instant`: render source-addressed structured paths as
state, render synthetic `$delta` as visible process text, and keep model body
paths separate from process/status panels. AgentTask does not start a separate
narrator request by default; process prose comes from bounded fields such as
`progress_message`, `short_summary`, `verification_summary`, and
`final_response` on the existing planner, verifier, card, or finalizer request.

During long quiet waits, AgentTask may emit an `agent_task.heartbeat` stream item
after `agent_task.heartbeat_interval_seconds` seconds without any other stream
item. The default interval is 10 seconds. Heartbeats are observational status
only: they help UI and log consumers understand the current stage, but they do
not satisfy evidence, hide a stall, or replace request/no-progress and
task-deadline timeouts. Any normal progress, snapshot, child-execution, delta,
or phase event resets the quiet timer, so active streams do not get heartbeat
spam. Public `delta` does not project heartbeat text; detailed timing and raw
heartbeat payloads remain available through structured streams and logs.

Terminal task status and artifact acceptance are separate. AgentTask terminal
result dicts include a user-facing `final_response` for accepted, degraded,
partial, and blocked outcomes. `completed` means the verifier accepted the
result (`accepted=True`, `artifact_status="accepted"`). When a result is
accepted only because unavailable or partial evidence was explicitly disclosed
and still satisfies the user goal, TaskBoard reports `accepted=True` with
`artifact_status="degraded"` and includes a `final_response` that explains the
degradation. This is not a quality shortcut: semantic acceptance still comes
from the verifier and host guards. `max_iterations` can still leave a useful
TaskWorkspace file or checkpoint, but it is a partial artifact (`accepted=False`,
`artifact_status="partial"`), not a completed business result. Partial and
blocked results include `final_response` so callers can show what was produced,
what stopped, and which requirements remain unmet. `get_text()` /
`async_get_text()` prefer this field for task-strategy result dicts. `get_data()`
returns the final business result, parsed against `output(...)` when possible;
use `get_full_data()` when you need the complete task terminal payload.
TaskBoard terminal payloads may also include `taskboard.completion_notes`, a
bounded process projection of card summaries, known gaps, verifier notes, and
acceptance progress. It is useful for UI progress and final-response disclosure,
but it is not evidence and does not replace verifier acceptance.
For model-produced verifier or finalizer fields, prose such as `status`,
`reason`, `progress_message`, or `final_response` is display context only;
completion and repair decisions must come from structured booleans such as
`is_complete`, `requires_block`, and `criterion_checks[].satisfied` plus host
guards.

### Semantic terminal verification and convergence

AgentTask has one semantic terminal-verification request for a fitting current
candidate. Before that request, the host builds one versioned terminal-carrier
inventory. A TaskWorkspace carrier is identified by its host-issued carrier id,
physical path, content-version id, and digest; a compact inline result has its
own carrier id and digest. Replacing file contents or inline contents creates a
new carrier identity. Historical carriers remain cold audit records and are not
mixed into the current verifier projection.

The same verifier response covers success criteria and material claims. It
returns every offered `criterion_id` exactly once in `criterion_checks`, plus
`material_claim_coverage_complete` and `material_claim_checks`. Before the
request, the host structurally divides the visible current carriers into exact
text spans and assigns each span one request-local `claim_key`. Each material
claim check returns only one offered `claim_key`, a `claim_kind`, a semantic
state, offered evidence `reference_id` values, and
`required_for_criterion_ids`. The last field contains only exact offered
criterion ids and is empty for optional or extraneous claims. Direct facts may be
`supported`, while bounded analysis or a
recommendation may be `reasonable_derived` when its visible premises support a
conservative conclusion; support does not require source wording to repeat the
conclusion verbatim. `unsupported`, `contradicted`, and `unverifiable` checks
cannot be accepted.

An unsupported optional claim may produce a `delete_only` local patch. A claim
required by a success criterion cannot: the host preserves that relationship
and requires `replan_segment` evidence reacquisition. If no new authorized
source, locator, or capability path remains, or the planner repeats an exhausted
canonical retrieval plan, TaskBoard closes blocked or asks for clarification
instead of replaying the same evidence/repair cards until a safety limit.

Host code validates criterion ids, claim keys, evidence ids, and evidence
eligibility, then reconstructs the canonical carrier id, exact quote, path, and
content version from the offered claim-key map. It does not tokenize verifier
prose, apply business keyword/regex rules, or run a separate claim-inventory,
source-selection, per-claim support-judgment, or empty-inventory review loop.
Candidate, delivery, acceptance, and verifier-readback records cannot
self-ground a descendant carrier. Positive claims require visible eligible
source content; explicit unavailability claims may use matching failed or empty
structured source facts. The task reference catalog remains the complete cold
audit record, while the verifier sees one bounded body-bearing ledger plus
body-light locator/ref indexes.

The verifier request exposes one selection domain per returned field. Its
`evidence_ledger.items` is the only place that exposes `reference_id` and is the
exact immutable set that host validation accepts in returned `evidence_ids`.
`material_claim_candidates` is the only claim selection domain and exposes only
`claim_key` plus the exact current text span and task-relevant location facts.
Acceptance locators and trusted artifact indexes are inspection-only and expose
no selection ids. Execution and cumulative evidence summaries also strip
evidence selection ids; a non-returnable Action call id may remain only as an
inspection correlation fact. Current bounded source records enter the first terminal
request even when no earlier work unit has persisted a pinned `evidence_use`;
transport references neither enter the grounding set nor inflate its
`omitted_count`.

Required capability/Action, output/schema, artifact/readback, evidence-binding,
criterion, material-claim, and lifecycle guards remain independent. Final
acceptance is their conjunction. A material-claim failure returns a structured
`material_claim_repair_contract`; Flat and TaskBoard consume that contract
without parsing verifier prose. For a trusted file carrier, the repair path uses
one bounded structured patch request and a host-validated exact replacement.
It does not open a general AgentExecution/ActionRuntime round or authorize a
whole-file rewrite merely because `write_file` is mounted.
That patch request contains only the authorized carrier identity, exact dirty
claim contract, bounded carrier quote, and output schema. Unrelated dependency
results, board history, evidence-ledger bodies, and acceptance projections stay
cold; request sizing is therefore a consequence of the dirty scope rather than
an arbitrary token cap.

TaskBoard evidence binding is repaired at the boundary that produced the
structured `evidence_use`. Card, control, finalizer, and binding-repair prompt
ledgers expose one stable `reference_id` plus bounded Action input/result or
locator facts; canonical ids, request-local `cite_as` values, call ids, and
aliases remain host-side. The terminal verifier uses this same one-identity
projection and returns only offered `reference_id` values in criterion checks.
Already-loaded Skill guidance readbacks enter the same card-local content
ledger, so a card can bind a Skill-guidance claim without rerunning synthesis
or treating the guidance as a ref-only pointer.
Raw and compact representations of one canonical evidence object rejoin the
same task reference; a changed snapshot/content version/hash still receives a
new reference. Current card-execution evidence is projected before
historical board evidence, so a fixed candidate budget cannot discard the
Action result or artifact readback that the card just produced. A binding-only failure does not retry a
completed Action card and therefore does not repeat successful external
Actions; one bounded binding repair is attempted and an unresolved binding
remains untrusted. A malformed model-authored binding cannot reverse a
canonically successful Action or completed card into a business-execution
failure; card execution and terminal semantic acceptance are separate owners.
Finalizer repairs are applied before terminal verification. Their normalized
bindings may pin canonical evidence in host code, but the finalizer's
`evidence_use` is not copied into the verifier's `execution_result`. The
terminal verifier selects grounding ids independently from its one offered
stable ledger. Dependency, board,
revision, evidence-ledger, and artifact-draft dependency views are separately
bounded, so cold execution metadata is not recursively multiplied through the
next ActionRuntime or artifact-body request.

TaskWorkspace artifact delivery also derives body-light locators from the actual
parsed Markdown section headings. The verifier can therefore request bounded
middle/tail section readbacks directly instead of scheduling a model repair
whose only purpose is to restate an exact heading. A material-claim repair
remains scoped to the structured failed checks. For a trusted file-backed
terminal carrier, including a carrier discovered from the current
artifact/readback rather than an explicitly declared required-deliverable
option, the repair card carries that normalized path as its authorization. The
unique leaf delivery card owns the terminal file projection, so intermediate
working artifacts remain cold evidence instead of competing with the delivered
file by byte size. When the caller has not supplied a stronger structured
required-deliverable contract, the unique completed leaf's structured
`artifact_manifest.path` becomes its TaskBoard terminal delivery target. The
host joins that target to trusted TaskWorkspace readback by exact normalized full
path; a same-named file under another directory is not equivalent. The
framework-internal `working/` namespace remains intermediate evidence and cannot
become a terminal target merely because the model declares it; only an explicit
caller-owned structured required-deliverable contract can authorize such a
path. Once a required TaskWorkspace path exists, it is the only TaskWorkspace carrier
offered to terminal verification; other trusted working files remain cold
evidence instead of competing as alternate terminal products. The verifier
returns only the selected `claim_key`; the host reconstructs its exact
`artifact_quote`, carrier id, path, and content version from the immutable
request map before producing a repair contract. The model returns bounded replace operations
and the host patch owner applies them;
full-file writes, replace-all operations, unauthorized paths, and old text
outside the named claims fail closed. This removes complete-body copying from
the repair model. The operation shape reuses the TaskWorkspace edit contract's
`old_string` and `new_string` fields, with exactly one operation keyed by each
host-issued `claim_key`. Before writing, the host rejoins the operation to its
immutable `segment_id` and verifies that the current promoted
`content_version_id` is still the version named by the repair contract. A stale
version, duplicate/unknown claim key, ambiguous exact match, or unrelated path
fails closed. Successful readback creates a new content version. Scope
comparison ignores paired Markdown emphasis around artifact labels,
while patch application still requires `old_string` to match the TaskWorkspace text
exactly. Inline candidates retain the bounded corrected-result path when no
trusted TaskWorkspace candidate exists.

Control-card `remaining_work` is local to that card's objective and completion
condition. Work already assigned to a downstream card does not keep an upstream
synthesis card open or cause the complete body to be generated again.
`next_board_action=continue` advances the board after preserving the current
card's explicit status; it does not turn a completed card into a setback.
`next_board_action=stop` is likewise a board-progression decision: a
`status=completed`, `sufficient=true` card remains completed while the board
stops scheduling further work. Only the explicit card status or
`next_board_action=block` makes that control result blocked.

TaskBoard artifact/file evidence projections retain their producer `role` and
`source`. A generated TaskWorkspace artifact remains a transport record even when
the same bytes are copied to another path or content version; those copies
include host-applied material-claim patch readbacks under
`agent_task.workspace_artifact.*` and cannot become independent sources that
support the carrier itself. Independent
Action, source, and TaskWorkspace-readback evidence keeps its normal eligibility.

Repeated terminal repair is counted by the exact
`(gate_kind, issue_code, contract_subject)` key. Different issue codes do not
share or advance one another's convergence counter. If the exact issue recurs
with unchanged carrier/source/capability/contract state, AgentTask records the
next occurrence without paying for a duplicate verifier request. At most two
repairs are scheduled for that exact issue. Its third occurrence stops the task with
`status="blocked"`, `accepted=False`, a useful
`artifact_status="partial"` candidate when one exists, missing criteria, and an
explanatory `final_response`; no fourth repair runs. An unavailable required
Action, denied/blocked policy, structured blocked lifecycle fact, or invalid
immutable candidate contract fails closed immediately instead of consuming the
three-occurrence allowance.

Malformed terminal-verifier output is owned by the verifier boundary, not by
artifact repair. Unknown or ineligible returned ids normalize to the stable
`(output_contract, terminal_verifier_output_invalid,
verification:response)` issue and report the exact field, invalid ids, and
offered grounding snapshot. If more than one response section is invalid, the
host combines every section's structured requirements into one repair contract
for that stable issue and sends it back with the current offered carrier and
evidence-id sets. TriggerFlow re-enters only
`terminal.verify -> transition.decide` so the verifier can correct its schema
response; it does not rerun work, rematerialize the output, rebuild evidence,
or create a TaskBoard repair card. TaskBoard also reuses the already prepared
final candidate, so a verifier-only retry does not repeat the finalizer request.
The third identical protocol failure blocks the task.

This convergence rule also covers a required TaskBoard card that repeatedly
returns a non-satisfying structured status: `setback`, `failed`, or `blocked`.
Only cards actually executed in the current tick are counted, so a stale result
retained in board history does not advance the counter while unrelated work
runs. Structured unrecoverable capability or policy failures still fail closed
immediately through their owning gate.

For a required TaskWorkspace deliverable, terminal finalization makes the current
physical locator/content-version readback authoritative. Older content versions
remain available as cold audit identity but cannot replace the current file by
being a longer historical candidate. Flat and TaskBoard keep the artifact body,
compact inline result, and trusted refs as separate carriers. An explicit
`candidate_final_result` is never copied into the artifact body. The file body
comes from an explicit artifact payload or from a successful Action write bound
to the declared manifest path; its physical readback is promoted even when the
planner selected `inline_final`. Terminal verification then keeps that current
or cumulative trusted file carrier throughout repair instead of
silently switching to an inline-summary hash.

A file-backed terminal result carries a concise TaskWorkspace pointer when no
separate explicit answer exists. If the execution returns a non-empty compact
`candidate_final_result` or `final_result` in addition to `final.md`, AgentTask
preserves that bounded answer alongside the trusted file refs instead of
replacing it with a pointer. File bodies remain in TaskWorkspace. Unknown or
duplicate verifier claim keys and unknown evidence ids fail closed. Exact
carrier identity and quote scope are reconstructed from the immutable host
claim map before a structured material-claim repair contract is created.

Public answer delivery preserves the complete value; metadata and log preview limits do not truncate `final_result`.

TaskBoard model/control cards use `candidate_final_result` as their single complete-body slot. Ordinary text stays inline. Only a declared task/card final file-delivery contract authorizes automatic Host materialization and exposes the `artifact_manifest` content plan. Legacy `final_result` / `artifact_markdown` bodies remain readable, but their field names alone do not authorize file creation. When the task requests a file without naming it, the planner chooses a relative path through the existing delivery field. If a final card supplies complete text without a manifest path, the Host stages that text for the declared target; it does not ask the model to rewrite it just to move it. A readback card also executes its declared `scoped_retrieval` through the existing ContextReader. Locator-only results remain references, while returned source bodies retain their actual completeness in the shared evidence ledger.

When a bounded step returns an explicit artifact, or a TaskBoard card with a
file-delivery contract returns a complete body or sectioned `artifact_manifest`, AgentTask writes the deliverable through the
bound TaskWorkspace and immediately reads it back. The cold evidence records
`path`, `bytes`, `sha256`, bounded preview, and `file_refs`; model-hot verifier
input uses path/ref handles, bounded content or preview, and truncation status.
For long, sectioned, or prose-heavy
deliverables, choose the content carrier deliberately. A single freeform
document can draft as natural Markdown/plain text with no `.output()` contract.
AgentTask's TaskWorkspace artifact writer consumes AgentExecution stream facts:
natural body text comes from raw delta items, and retry boundaries come from
`$status` when the provider reports it. This natural-text path does not require
the draft request to use `.output()`. A non-streaming draft uses the completed
text from that same execution when no delta was delivered, without another
model request or duplicating an already streamed body. If the public `type="delta"` replay marker
`"<$retry>...</$retry>"` reaches the artifact consumer, it is treated as a
public replay delimiter and is never written or transported as deliverable text.
It is not promoted into retry metadata; structured `$status` remains the retry
control source.
If a bounded work unit already returned a complete Markdown artifact body in
structured `evidence`, AgentTask only treats it as a deliverable body when the
evidence item is explicitly labeled as artifact/body/deliverable/Markdown or
tied to the manifest path. Untyped source content and source excerpts remain
evidence snippets, not file bodies. After TaskWorkspace write/readback succeeds,
remaining artifact-write intent is handed to terminal verification instead of
forcing another iteration just to write the same file.
For a completed and sufficient TaskBoard leaf that already carries a complete
artifact body, delivery/readback work in `remaining_work` does not suppress that
write. AgentTask first materializes the leaf's structured declared path, reads
it back, and hands the residual work to terminal verification. Non-terminal,
insufficient, repair, patch, blocked, or explicit readback results still do not
gain this delivery authorization.
When the caller needs separately addressable fields, use Agently
`.output(..., format=...)` with `xml_field`, `hybrid`, or `yaml_literal` when
that format fits the payload instead of forcing the long body into compact JSON
fields. Keep status, evidence, and verification in separate compact
judgment/readback contracts. When AgentTask must deliver a trusted file
artifact, use `artifact_manifest.sections` plus TaskWorkspace readback.
Model-declared `file_refs` are diagnostics only until the framework has
produced this TaskWorkspace readback evidence, preserving a real `final.md` or other
deliverable for host-side review.
TaskBoard finalization keeps file-backed deliverable bodies in TaskWorkspace; the
returned `final_result` should stay a concise summary or path/ref pointer rather
than a second copy of the file body. A completed terminal leaf's explicit
`final_result` is the summary/answer carrier and survives together with the
artifact ref; absence of that field falls back to the path/ref pointer.

The same ref-backed path is also valid for intermediate work. A step may
download a file, save a webpage snapshot, write generated code, keep search
notes or memory-like task notes, or persist large extracted text as TaskWorkspace or Action artifact refs.
Hot prompts should carry compact refs and bounded previews; later blocks can
open scoped snippets with `read_file(max_bytes=..., offset=...)` or artifact
readback when they need the content. Readback work-unit hot payloads use the
same compact refs; complete refs remain in cold TaskWorkspace/Blocks evidence for
programmatic readback and audit. These intermediate refs are execution evidence,
not final deliverable proof. A discovered URL, path, download, or
snapshot ref is also not evidence that the content has been read; it remains
`ref_only` until a bounded readback or content preview is visible. Explicit
`content`, `excerpt`, or `snippet` fields count as bounded previews only for the
visible excerpt, not for the whole file.
Source-grounded deliverables should either request structured `target_refs`
readback for unread refs or label them as discovered-only rather than claiming
facts from them. If an Action artifact readback exposes TaskWorkspace `file_refs`
for a materialized download, TaskBoard readback promotes those nested refs to
card-level `file_refs` so later work can use TaskWorkspace readback instead of
relying on a buried JSON preview. When a non-final
TaskBoard card proposes a required final path such as `final.md`, AgentTask
relocates that intermediate artifact to `working/taskboard/<card-id>/...` and
keeps the declared final path for the final synthesis or finalization card.
Framework-generated final repair cards stage the declared deliverable under a
card-scoped terminal-candidate path. They do not overwrite the root path during
repair. During terminal verification, a host-validated delivery contract maps
that completely read candidate to its required target. The verifier judges the
candidate's content and evidence as the provisional carrier for the target; it
must not reject the candidate merely because the target does not exist before
acceptance. Only a successful semantic verdict can trigger the host-owned,
digest-pinned promotion of those exact bytes to the declared root path. The host
then performs complete target readback and digest/byte-count checks before the
task can complete.
Flat source refs carry the same boundary: repository clone/list manifest paths
are `ref_only` until a file read, artifact readback, or bounded content preview
is visible. A verifier or repair planner can reuse exact paths as retrieval
targets, but not as proof of file contents.
TaskBoard final verification receives board-level source refs with the same
`content_state` boundary, so final synthesis cannot upgrade a discovered path
into source-content evidence without a bounded preview/readback.

Flat and TaskBoard work units also receive a task context contract. Runtime
metadata can record compact `current_time` facts for diagnostics, but default
model-hot prompts receive only prompt-safe availability metadata and omit the
concrete runtime timestamp. For current, latest, recent, or as-of tasks, pass
the intended business date or source timestamp explicitly when it matters. The
contract is context for model decisions, planning, evidence selection, and
source-boundary handling; it must not be used as a business fact by itself and
does not set model-call, tool-call, node-count, iteration, or wall-clock caps.

When a model-authored TaskBoard card contains an otherwise valid scoped
TaskContext retrieval plan whose reserved `max_results` total exceeds 64, the
host preserves its Context source kinds and splits it into bounded retrieval
cards plus one dependent continuation. It does not silently truncate the plan
or rewrite pinned repositories, Skills, Memory, or other Context sources as
TaskWorkspace Actions. The normalization is observable through
`agent_task.taskboard.plan.normalized`. A single query group larger than the
capacity, invalid result counts, or unknown source kinds still fail closed for
structured replanning.

Inside a scoped query group, `path` selects a file or directory scope and
`pattern` is only a file-name glob such as `*.py` or `**`. To position a
bounded read at an exact code symbol or text fragment inside the selected
source, use `filters.content_contains`; each exact locator is normalized into
its own bounded query group. Keep semantic intent in `query`. Do not put code
symbols or content phrases in `pattern`.

TaskBoard readback cards can inspect both Action artifact refs and trusted
TaskWorkspace file refs with bounded cold readback previews. Framework-generated
readback cards scope evidence to direct dependencies plus upstream evidence
cards, so a control-card readback can still inspect Action refs produced by
earlier evidence-gathering cards. If a generated continuation card still
reports that the same readback is insufficient, the framework does not
recursively synthesize another readback/continuation chain; the card must
propose different executable work or remain in setback/blocked diagnostics.
For scoped TaskWorkspace retrieval, `evidence_snippet` records include whether the
bounded snippet was `truncated`. AgentTask now carries these retrieval facts
through the canonical `EvidenceEnvelope.evidence_items` ledger and injects a
model-hot `evidence_ledger` view into Flat and TaskBoard work units. The older
`scoped_retrieval_results` and TaskBoard `source_refs` views remain
compatibility projections, not separate grounding authorities. Failed or empty
search/readback items support unavailable or missing-data claims only;
`ref_only` locator items prove only discovery until a bounded readback evidence
item exists. If a TaskBoard card with scoped retrieval
returns setback/blocked/insufficient output without an explicit next action, AgentTask
turns that local insufficiency into an action-capable evidence card with an
expanded bounded retrieval plan plus a continuation card. The search result is
still only factual context; the continuation card decides whether it is enough.
When the missing evidence is a new concrete URL, path, or ref rather than an
existing Action/TaskWorkspace ref, the control card should return
`next_board_action="readback"` plus structured `target_refs`. AgentTask turns
that compact intent into an action-capable evidence card that can download,
snapshot, or otherwise materialize the target before the continuation card
runs. URLs mentioned only inside `gaps` prose are diagnostics; they are not
parsed as executable targets.
When a control card instead returns `next_board_action="patch"` with a TaskWorkspace
text patch proposal, AgentTask applies the patch to the bound TaskWorkspace file,
writes it back, and returns trusted `file_refs` after readback. This is a
materialization step only: final completion still belongs to terminal
acceptance and host guards.
For completed and sufficient control outputs, non-fatal `gaps` do not prevent
TaskWorkspace artifact materialization. A completed and sufficient leaf may also
materialize its complete artifact before handing `remaining_work` to terminal
verification; on other control outputs, `remaining_work`, setback/blocked
status, repair, or readback still prevent delivery. Writing the artifact only
creates evidence for later readback and verification. It does not mean the
final task has been accepted.
Flat and TaskBoard do not need an independent verifier after every intermediate
work unit. In Flat, non-empty `remaining_work` defaults the current step to an
intermediate result, so the next iteration consumes the new facts and decides
the next action; a step may also return `ready_for_final_verification=false` to
make that explicit. Set `ready_for_final_verification=true` only when the
current result intentionally needs terminal, blocking, or risk verification
now. In TaskBoard, the downstream card that consumes
dependency evidence decides whether it is enough for its own objective.
Independent verifier requests are for final acceptance, fan-in/control
acceptance, evidence/artifact boundary audit, contradictions, or high-risk
review.
When a terminal verifier returns an incomplete result, its compact
`repair_context` is carried into the next Flat work unit and into the dedicated
TaskWorkspace artifact draft request when the next deliverable body is file-backed.
This keeps exact `acceptance_delta`, repair constraints, next-step
requirements, and available evidence anchors visible to the consumer that
actually rewrites or reads the artifact, without putting cold integrity metadata
back into the model-hot path.

If terminal verification requests a new evidence segment rather than a mounted
capability repair, a dedicated ModelRequest selects bounded semantic queries
from the TaskContext source kinds actually offered by the host. The resulting
evidence card continues to execute through ContextReader. Completion requires
the card's `evidence_use` bindings to name the exact new body-bearing reference
identities added to EvidenceLedger; prose claiming that evidence was acquired
cannot satisfy this guard.

Source adapters may expose small authoritative descriptor facts, such as a
canonical repository URL and pinned commit, as ordinary typed `information`
descriptors with exact reads. Once ContextReader discloses such a block,
AgentTask gives it the same host-issued evidence binding as other eligible
ContextPackage information; undisclosed adapter metadata does not become
evidence automatically.

For TaskBoard, an authored required `action_succeeded` capability requirement
also owns repair dispatch. If that exact Action evidence is missing and the
capability is mounted, TaskBoard schedules an Action-shaped repair carrying the
same capability id and kind. Verifier prose does not select the Action, and a
TaskWorkspace readback cannot satisfy an Action requirement. If the capability is
unavailable, repair fails closed instead of substituting a different operation.
The repair card keeps that exact requirement through its card contract,
`WorkUnitIntent`, Blocks capability resolution, and the TaskBoard Action
allow/required scope; it is not merely a hint in the repair prompt.
Each Action card receives one card-local objective/done-when work unit plus its
dependency evidence. The global task remains orientation for response
synthesis; it does not authorize that card to execute sibling work.

TaskBoard Action cards do not use an open-ended ActionLoop by default. When the
board plan already contains complete `action_id` plus `action_input` commands,
the Blocks carrier validates those commands against the mounted Action registry
and dispatches them directly through ActionRuntime, without an Action-planning
model request. A validated, non-empty `action_commands` contract is stronger
than a generic `allowed_execution_shape` hint: if a planner returns both and
the hint conflicts, the host normalizes the card to Action execution while
preserving the declared hint and normalization reason in diagnostics. When the
Action ids are known but arguments depend on upstream
card results, one narrow structured request returns only the bounded
`action_commands` batch; host validation then performs the same direct
dispatch. Every model-authored command is checked against the mounted Action's
declared input keys before dispatch. An invalid initial-plan command is removed
and re-planned once through that authoritative narrow contract; an invalid
narrow response fails closed without reaching ActionRuntime. Exact TaskWorkspace final-artifact handoff is likewise lowered to the
required write/readback Actions. Unknown Action ids, missing required ids, and
invalid arguments fail closed. The ordinary multi-round ActionLoop remains for
open-ended Agent execution where later Action choices genuinely depend on
earlier Action results.

The initial TaskBoard planner receives the task's structured capability-evidence
requirements as part of the planning contract. Its `action_commands` field is
an exhaustive batch, not the first phase of a card that will silently continue
with synthesis after dispatch. If an initial card combines non-delivery Action
commands with `final_workspace_deliverables`, AgentTask separates it into an
upstream Action card and a dependent control card before board validation. A
final-delivery card without an exact command batch is treated as a control card,
while an already complete, schema-valid TaskWorkspace write command remains an
Action card. The dependent control request receives the collected evidence and
owns synthesis. When the task contract explicitly requires TaskWorkspace write and
read Actions, the synthesized body is then materialized and read back through
those Actions before normal artifact adoption. This keeps Action success,
TaskWorkspace readback, and final content ownership on one visible value/event
chain instead of relying on a later repair loop. Control-card Action records use
the same execution-summary carrier as ordinary Action cards, so terminal
capability checks observe the completed write/read events instead of scheduling
a duplicate repair. An `action_succeeded` requirement is satisfied by an actual
successful call record even when a separate call of the same Action failed;
that failure remains visible to execution-risk handling and does not erase the
successful event.

If a later TaskBoard leaf only verifies or references that same artifact, the
host joins its requested path to the trusted artifact refs in the canonical
dependency `TaskBoardCardResult` and adopts the current physical readback. A
model-repeated `artifact_manifest` or `file_refs` projection does not authorize
another artifact-draft request and cannot overwrite the dependency-owned body.

Flat AgentTask steps use the same command-lowering owner. The Flat planner
selects `required_action_ids` from the compact capability list; it is not asked
to guess strict kwargs from that list. If an internal structured plan already
carries validated `action_commands`, the host dispatches them with no additional
planning request. Otherwise, one narrow structured request receives only the
required Actions' authoritative schemas plus the bounded step context, returns
either a complete command batch or `requires_observation=true` with no commands.
When all arguments are already grounded, the host validates and dispatches the
batch serially through ActionRuntime. Ordering alone (such as writing then reading
a known path) does not require another planning round. If later arguments need an
earlier Action's new result, Flat uses the existing bounded child ActionLoop to
observe that result before planning the next call, even when all Action ids are
known. It never dispatches a partial batch before this handoff. Missing or
contradictory readiness fields fail closed; unknown or unavailable required
Actions fail before the narrow request. Explicit `action_commands` remain fixed
kwargs, not a result-reference or substitution language. Child scope, policy,
deadlines, and final verification remain unchanged. The handoff also binds all
batch-required ids through the child's existing `require_actions` evidence gate;
visibility alone is insufficient. This gate proves successful calls, not correct
arguments or every required repetition. Handoff metadata records the
one narrow request in `execution_meta.action_command_planning`.
The adaptive handoff does not impose the ordinary child's implicit two-round
cap: calls may need another round for final synthesis. Explicit task
`action_loop_max_rounds`, task deadlines and request budgets still apply.

When the step has completed `scoped_retrieval`, its argument request also receives
the bounded read results and existing evidence ledger, preserving the original
task, step and context without another read or model request. Failed, empty,
ref-only and truncated states retain their meaning; unread content cannot ground
arguments. Requests without a current-step read remain unchanged. Explicit
preplanned commands keep fixed arguments, and a deferred child receives the same
evidence.

Task-wide `require_actions` is checked against the task's cumulative Action
evidence, not repeated as an obligation on every child request. A child that
only writes the final answer or an artifact need not repeat completed Actions.
Explicit step-required Actions still use the child's gate; missing or failed
task-required Actions still prevent acceptance. Agent default requirements are
captured when the task is created and retained with its saved options.

In Flat, a successful command batch that only produces an ordinary observation
passes its evidence to the next step without treating unfinished task goals as
a failed step. This applies only to known command results without an explicit
terminal-readiness flag, actual candidate, artifact refs or active terminal
repair. A planned `inline_final` format does not mean an answer already exists.
Pending required obligations and exact Action results remain available; real
failures, permissions, grounding and repair findings still apply. Final
candidates still require terminal verification. Intermediate observations or
budget exhaustion never establish acceptance. TaskBoard, artifact readback and
outer `review` / `validate` retain their existing responsibilities.

`examples/agent_task/action_result_dependency.py` runs a real-model ticket
lookup/acknowledgement task with a revision generated only at Action execution.
Configure its `MODEL_BASE_URL`, `MODEL_API_KEY`, and `MODEL_NAME`; optional
`MODEL_REQUEST_OPTIONS` supplies a JSON object of provider options.

AgentTask observation also publishes normalized action facts on the structured
stream as `agent_task.action.started`, `agent_task.action.completed`, and
`agent_task.action.failed`. These events summarize existing Action records with
safe input summaries, result previews, refs, timing, diagnostics, and work-unit
ownership. Recovered `success` or `partial_success` Action records are projected
as completed observations, while failed events are reserved for actual failed,
blocked, timed-out, or unrecovered error records. They are observation facts for DevTools, UI, and experiment logs; the
downstream consumer, terminal verifier/final control, and strategy still own
usefulness, quality, and completion judgment.

When the write succeeds and readback is trusted, verifier input carries one
bounded body-bearing evidence ledger, including the relevant readback content
or preview, plus body-light acceptance-locator, artifact, and overflow ref/state
indexes. Raw evidence and integrity metadata remain available for scoped
TaskWorkspace readback and audit instead of being copied into every hot summary.
When one claim cites both valid content evidence and structurally incompatible
auxiliary ids (for example failed or `ref_only` records as positive support),
binding repair may remove only the incompatible ids and only after the retained
binding passes the same deterministic guard. It never invents positive evidence;
if no compatible id remains, verification stays fail closed.
`capability_evidence.artifacts.readback` continues to carry path handles; with
`max_iterations=1`, a real readable artifact should not become partial only
because the evidence chain was omitted. If readback fails or lacks trusted
`path` / `bytes` / `sha256` evidence, diagnostics use
`agent_task.workspace_artifact.readback_failed` or
`agent_task.workspace_artifact.readback_insufficient` so the problem is reported
as TaskWorkspace artifact readback missing/insufficient, not as a generic budget or
iteration shortfall.

If the structured task input or output contract declares required deliverables,
AgentTask host guards require those TaskWorkspace files to exist and read back before
accepting completion. A verifier response that says a file exists is not enough
unless TaskWorkspace readback confirms the declared final path. That caller-owned
contract is authoritative. Without one, a unique completed TaskBoard leaf's
structured `artifact_manifest.path` supplies the narrower execution-local
delivery target; terminal acceptance still requires exact-path readback and
does not substitute an upstream or same-basename working file.

For public reference material, such as framework introductions or API guidance,
task verifier acceptance is still not a source-quality guarantee by itself. Feed
current docs/spec/source references into the task or add an Agently model-judge /
source-reference check so stale or generalized API claims cannot pass only
because the task-level verifier accepted the draft.

Intermediate process steps with strong structured contracts use Agently
`.output(..., format=...)` on the owning `ModelRequest` or `AgentExecution`.
Do not add a restrictive JSON `.output()` contract to a pure long-prose drafting
request only to control the body. Compact control payloads can use JSON; when a
structured contract is genuinely needed for a content-heavy payload, use
`xml_field` for XML-like field boundaries, `hybrid` for prose plus typed control
fields, or `yaml_literal` for a literal document payload when that format fits
the target model and consumer. If a declared non-JSON format cannot be parsed,
Agently tries JSON as a recovery parser and accepts it only when the parsed
value is a dict. The same guard applies to task `final_result` when an
execution output contract is present.

`examples/agent_task/goal_effort_public_stream.py` is the public-chain
streaming proof for this contract. It runs
`.goal(...).effort(...).input(...).output(...).strategy("flat")`, consumes
`get_async_generator()`, streams model-generated progress deltas, and checks
that the execution prompt snapshot reaches AgentTask planning, execution,
and verification. `examples/agent_task/goal_pursuit_acceptance_matrix.py`
remains a smaller matrix script for accepted and non-accepted terminal states.

`examples/agent_task/real_complex_bundle_goal_stream.py` is the high-level real
complex proof for the same path. It mounts Search, AMap MCP, TaskWorkspace file
actions, and the CocoonAI `architecture-diagram` Skill through public Agent
capability APIs, then asks the task loop to produce an operator daily report, a
Hangzhou business travelogue, and an HTML/SVG architecture diagram while
streaming natural-language progress deltas. It uses multi-round bounded direct
steps so the proof stays on the current public AgentTask lifecycle instead of
depending on mixed DynamicTask/DAG execution. The lower-level
`examples/blocks/07_real_complex_bundle_stream.py` remains a Blocks
external-capability substrate probe rather than the recommended business entry
point.

`examples/agent_task/agently_architecture_diagram_task.py` is the longer
design-document experiment for the same path. It keeps the legacy
`.goal(...).effort(...).strategy("task")` spelling only as a compatibility
probe, not as the recommended selector for new code. New code should use
`.strategy("direct")` for the ordinary model_request/ActionLoop route, or
`.strategy("flat")` / `.strategy("taskboard")` when the host explicitly wants
AgentTask. The example also uses a repository-source Action, TaskWorkspace file
Actions, and an independent Agently model judge to produce and review a readable
Agently architecture diagram.

`examples/agent_task_experiments/` contains compact developer examples based on
the core AgentTask experiment scenarios: stock-risk briefing, Agent engineering
weekly, LMCC mock exam generation, repository reading, and multi-runtime code
execution. The same folder also includes mixed capability examples that combine
native Actions, real MCP registration, local Skills, TaskWorkspace file actions,
and delta streaming for travel planning, equity risk analysis, and market-entry
analysis. These examples deliberately rely on `agent.create_task(...)`
defaults, including `execution="auto"`, so the example code stays close to
ordinary application usage, and they consume `get_async_generator(type="delta")`
to show the task information stream.

The first public slice is intentionally narrow: single task, one Agent owner,
roughly 2-5 iterations, and bounded steps through `AgentExecution`. Those steps
may use Actions or Skills that the host already enabled on the Agent or attached
to the current execution. AgentTask does not provide
multi-task coordination, background autonomy, distributed leases, mid-step
pause/resume, or long-term memory management. Crash recovery for this slice is
exposed through `agent.resume(...)` / `agent.async_resume(...)`, which rebuild a
task-strategy `AgentExecution` instead of exposing AgentTask as a second public
lifecycle.

### Resume a task after a crash

RecordStore recovery is opt-in. Create the original task with a stable `task_id`
and `record_store_recovery=True` when a crash must be resumable:

```python
execution = agent.create_task(
    task_id="issue-123",
    goal="Repair the issue and verify the result.",
    options={"agent_task": {"record_store_recovery": True}},
)
await execution.async_start()
```

With that option, AgentTask persists a compact resumable snapshot after each
completed iteration. If the process crashes, resume the task as a fresh
`AgentExecution` and continue from the next iteration. Completed iterations are
not re-executed:

```python
execution = await agent.async_resume("issue-123")   # or agent.resume("issue-123")
result = await execution.async_start()              # continues from iteration N+1
meta = await execution.async_get_meta()
```

Resume reads the task's latest snapshot from the RecordStore, restores its
iteration history and cumulative required-capability progress, and raises
`ValueError` if no resumable snapshot exists. An iteration that was in flight at
crash time is re-planned, so non-replay-safe step side effects are the host's
responsibility. `AgentExecutionResult.resume()` delegates to the same Agent
resume facade when the result carries resumable `task_refs`; otherwise it
returns an unsupported resume response. `resume_task(...)` remains only as a
compatibility alias for `resume(...)`.

### Route a user's "continue" request

An application interface should not treat the word "continue" as a deterministic
resume command. A structured UI action, such as a **Resume this task** button,
may route directly after the host validates the task reference, authorization,
snapshot compatibility, and live execution status. A reply bound to one task
card identifies a candidate, but its free-form text remains a semantic intent
decision.

For a free-form message, the host should first project only authorized,
resumable candidates with one short selection key and bounded task facts. Ask
one structured ModelRequest to choose among `resume_existing`, `start_new`, and
`clarify`; then validate the returned key and lifecycle facts before dispatch:

```python
from typing import Literal

from pydantic import BaseModel


class ContinuationRoute(BaseModel):
    decision: Literal["resume_existing", "start_new", "clarify"]
    selection_key: str | None = None
    clarification_question: str | None = None


route = await (
    agent.create_request()
    .input(
        {
            "user_message": user_message,
            "resumable_candidates": [
                {
                    "selection_key": "c1",
                    "goal_summary": "Complete the supplier risk report.",
                    "last_progress": "Evidence collection completed; report drafting remains.",
                }
            ],
        }
    )
    .info(
        {
            "routing_contract": {
                "resume_existing": "Continue the same unfinished goal and success criteria.",
                "start_new": "Start follow-up work or change the goal, criteria, or deliverable.",
                "clarify": "The intended task or lifecycle action is ambiguous.",
            }
        }
    )
    .instruct(
        "When selection_key is present, return only an offered key. "
        "Do not create or reproduce task ids."
    )
    .output(ContinuationRoute, format="json")
    .get_response()
    .async_get_data_object()
)
```

The model owns the prose-derived relationship between the message and the
offered tasks. Host code owns candidate discovery, authorization, offered-key
membership, snapshot/version checks, canonical `task_id` lookup, and the actual
call to `agent.async_resume(...)` or `agent.create_task(...)`.

Resume only when the selected task is non-terminal, is not already running, has
a compatible snapshot, and the user is continuing the same goal and success
criteria. A completed task, a changed goal or output contract, or new follow-up
work starts a fresh task; the host may bind the prior result or evidence into
the new TaskContext without presenting that task as a resume. When candidates
or intent remain ambiguous, ask the user instead of falling back to keyword
routing or silently starting duplicate work.

For examples that validate model-owned semantic content, combine deterministic
smoke checks with a second Agently model-judge request. Structural checks such
as file existence, question count, and visible source labels are useful smoke
gates, but semantic acceptance should come from a judge schema with per-rule
evidence and boolean results.

Business-system fixtures may be mocked in examples, but they should only return
business facts, records, policies, or intentionally incomplete source data.
They should not return pass/fail labels, hidden expected answers, or local
quality verdicts. If the scenario needs to decide whether an artifact handled
defective or conflicting data correctly, let AgentTask verification or a
separate Agently model-judge request make that judgment from explicit rules and
evidence.

## Execution Object

Use `agent.create_execution()` when the caller needs route diagnostics, multiple
result views, or process streaming:

```python
execution = agent.create_execution()
execution.input("Summarize the reviewed DAG snapshot for the operator.")
execution.info({"dag_snapshot": snapshot})

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

The execution object follows the same consumption style as model responses:
`get_data`, `get_full_data`, `get_text`, `get_meta`, `get_generator`, and async
equivalents.
The default stream is `type="delta"` and yields plain text strings, including
the reserved `"<$retry>{reason}</$retry>"` boundary when a model stream is
replayed. The marker is for public text replay consumers only; internal
artifact writers and structured UIs should prefer structured status events when
available, and only handle the marker at a plain-text consumption boundary. Use
`type="instant"` for structured execution events:
`AgentExecutionStreamData` keeps the familiar `path`, `value`, `delta`, and
`is_complete` fields and adds route metadata for process-level events. For UI
consumers that want one text slot plus structured state updates, `instant` also
appends synthetic `path="$delta"` text-projection items after source events that
can be projected to text. Heartbeat stays structured-only and does not append
`$delta`; `all` does not include those derived items.

`create_execution()` creates an AgentExecution draft. Ordinary prompt-only
drafts run as direct model requests. DynamicTask/TaskDAG workflows run through
`Agently.create_dynamic_task(...)` or `TaskDAGExecutor(...)` first and can pass
their snapshots into a later AgentExecution as evidence. When a developer-owned
loop or task strategy needs a bounded AgentExecution step, express the boundary
with `lineage` and `limits`:

```python
execution = agent.input("Try one bounded fix step.").create_execution(
    lineage={
        "task_id": "issue-123",
        "iteration_id": "iter-2",
        "step_id": "execute-fix",
        "parent_execution_id": "exec-prev",
    },
    limits={
        "max_model_requests": 3,
        "max_seconds": 180,
        "max_no_progress_seconds": 60,
    },
)
```

This is still one AgentExecution, not a multi-turn loop. `lineage` provides
stable correlation, while `limits` provides shared model-request budget counting
across direct model and AgentTask requests.
Nested AgentExecution contexts consume the same ancestor budget atomically;
creating a child does not reset the root allowance. A child may additionally
set a smaller local allowance for its own subtree without lowering a sibling's
local allowance.
Use `None` for an unlimited budget.

If a bounded execution exceeds its model-request budget, Agently raises
`AgentExecutionLimitExceeded`, available from the root `agently.core` export or
from `agently.core.application.AgentExecution`. The execution meta remains
inspectable and records `status="blocked"` plus the limit event in
`diagnostics`.

For stuck executions, `limits.max_seconds` is a hard deadline for the whole
AgentExecution. In Goal Pursuit / task-strategy runs, AgentTask owns that
wall-clock budget and returns a task `timed_out` result with task metadata; other
routes surface the hard deadline as `RuntimeStageStallError`, available from the
root `agently.core` export or from `agently.core.application.AgentExecution`.
`limits.max_no_progress_seconds` is an idle stall boundary: any accepted runtime
progress from route selection, model streaming, Context reading, or ActionRuntime
refreshes the timer. `async_get_meta()` remains inspectable and records
`status="timed_out"` or `status="stalled"` with `diagnostics["timeouts"]` /
`diagnostics["stalls"]` and the last progress event.

Provider and response materialization waits have separate knobs:

```python
Agently.set_settings("OpenAICompatible.stream_idle_timeout", 60.0)
Agently.set_settings("OpenAIResponsesCompatible.stream_idle_timeout", 60.0)
Agently.set_settings("response.materialization_idle_timeout", 60.0)
```

`stream_idle_timeout` bounds the gap between provider stream items that contain
meaningful response data. Empty SSE keep-alive frames do not start or refresh
response liveness. First-event and stream-idle deadlines also surface without
waiting for delayed transport-cancellation cleanup. Both raise
`RuntimeStageStallError` with provider/model fields when the requester can
identify them. The timeout remains inside the ModelRequest attempt lifecycle:
`request_retry.max_attempts` is consumed before the final error fails closed.
Each failed `model.status` payload keeps its attempt index, retry decision, typed
stall diagnostic, meaningful-response progress basis, and asynchronous cleanup
fact for audit.
Each `model.requesting` payload also projects the effective non-secret liveness
policy (`timeout_mode`, typed HTTP timeouts, `stream_idle_timeout`, and
`request_retry`) so a missing retry can be distinguished from a request that
never produced a retryable provider failure.
`response.materialization_idle_timeout` bounds the wait while final text, data,
object, or meta is materialized from the response parser. `None` is unlimited;
`-1` is accepted for compatibility. If the provider or response construction
emits an explicit stream error before materialization completes,
`get_text()` / `get_data()` / `get_meta()` propagates that original error
instead of waiting for the materialization timeout.

High-frequency RuntimeEvent outlets should request Event Center summary
delivery instead of asking AgentExecution to throttle at the source:

```python
Agently.event_center.register_hook(
    handler,
    event_types="model.response.delta",
    hook_name="app.delta_summary",
    delivery_policy={"mode": "summary", "emit_interval": 0.1, "max_items": 20},
)
```

AgentExecution stream APIs stay raw. Event Center outlet summaries include
`meta["coalesced"]`, `coalesced_count`, and source event ids when a hook opts in
to summary delivery.

`async_get_meta()` includes `lineage`, `limits`,
`route`, `route_plan`, `logs`, `diagnostics`, and `task_workspace_refs`. `logs` is
the route-independent place to inspect runtime
facts such as model response ids, ActionRuntime action records, and artifact
refs:

```python
meta = await execution.async_get_meta()
meta["route"]["selected_route"]
meta["logs"]["model_response_ids"]
meta["logs"]["action_logs"]
meta["logs"]["artifact_refs"]
```

When a `model_request` route uses Actions, the execution exposes the action
records through both meta and stream events such as `actions.<action_id>`.
Hosts that need to persist business evidence should read the framework action
record or artifact, then explicitly write the selected observation to
RecordStore. Do not ask the model to copy raw action stdout just to make the host
able to store it.

Every process-stream item also receives correlation metadata:

```python
item.meta["execution_id"]
item.meta["lineage"]["task_id"]
```

Default Agents carry separate lazy TaskWorkspace and RecordStore bindings.
`agent.use_task_workspace(...)` selects the file root;
`agent.use_record_store(...)` selects persistence. AgentExecution still does
not decide what becomes memory automatically; persist explicitly:

```python
record_ref = await execution.record_store.put(
    collection="observations",
    kind="agent_execution_observation",
    content={"result": data},
)
checkpoint_ref = await execution.record_store.checkpoint(
    execution.lineage["task_id"],
    {"result": data, "record_ref": record_ref},
)
```

This writes through the execution-scoped RecordStore view. Attach a
`RecordStoreContextSource` to TaskContext, or use
`execution.async_read_task_context(...)`, for the next bounded read.
TaskWorkspace continues to own file artifacts and their readback refs.

For development diagnostics, attach an EventCenter observation hook or
temporarily enable console details:

```python
agent.set_settings("debug", "detail")

task = agent.create_task(
    goal="Prepare the report.",
    success_criteria=["The report is complete and verified."],
    execution="flat",
)
await task.async_streaming_print()
result = await task.async_get_full_data()
```

`debug=True` (the `simple` profile) prints a readable Prompt, concise
request/result facts, and meaningful process states. `debug="detail"` adds
sanitized provider request JSON, attempt/validation/telemetry, Action detail,
and route/stage metadata, while still filtering compatibility aliases, transport
mirrors, and repeated progress. The console is not a complete RuntimeEvent flow
and does not replace business output: consume `type="delta"` or call
`async_streaming_print()` for readable task stages and the final result. Use an
EventCenter hook or DevTools for complete audit, storage, or replay. Remove debug
settings from examples and production snippets once the problem is understood.
Concurrent model responses remain concurrent: ConsoleSink gives the first
delta-producing response the foreground display, buffers later response display
in FIFO order, and promotes it after the foreground terminal event. This affects
presentation only and never blocks or reorders the underlying execution. While
that foreground stream is active, ordinary Prompt/request/process/success
diagnostics are bounded and deferred until every FIFO response display finishes;
the console inserts only a compact background notice. Actionable warnings,
failures, cancellation, interrupts, and approvals stay immediate, and EventCenter
and DevTools event timing is unchanged.
Simple mode preserves one complete projection of each successful model response:
either its fully rendered live stream or its complete terminal result. If a
background replay buffer fills, the partial replay is replaced by the complete
authoritative result at completion rather than being presented as the whole
response.

### Execution controls

`run()`/`async_run()` and result readers share one owned run. Concurrent readers
cannot duplicate production. Inspect `execution.control_capabilities` before
starting when the caller needs a supported control boundary.

- `pause()`/`async_pause()` requests a safe boundary before production or after
  the candidate is ready, before final validation/artifact/review policies.
  A requested pause is not yet suspension; an in-flight provider finishes first.
- At an actual pause, run/readers raise `AgentExecutionPaused` without turning
  the candidate into a terminal result. `resume()`/`async_resume()` explicitly
  continues the retained TriggerFlow. Ordinary reads never resume implicitly.
- `interrupt(content, author=None)` supplies information for a future model
  request through TaskContext. Its receipt distinguishes insertion, request
  consumption and ignoring. It cannot change an already dispatched request;
  information arriving after candidate production is ignored.
- `cancel(reason=..., timeout=...)` cancels owned work and waits for cleanup.
  A timeout is an unsettled outcome; repeat the call to join the same cleanup.
  Cancellation does not roll back external effects.
- `close(reason=..., timeout=..., pending="error")` drains and seals. Pending
  waits fail by default; use `pending="cancel"` explicitly to abandon them.
  Closing a draft prevents start; closing a completed result preserves readers.

Every control has an `async_` counterpart. A snapshot is supported only at a
settled safe pause:

```python
from agently.core.application.AgentExecution import AgentExecutionPaused

execution = agent.create_execution("request").input("Summarize these supplied notes.")
await execution.async_pause()
try:
    await execution.async_run()
except AgentExecutionPaused:
    snapshot = execution.save()

restored = agent.create_execution("request").input("Summarize these supplied notes.")
restored.load(snapshot)  # Validation and rebinding only; no model or Action dispatch.
await execution.async_cancel()  # Retire the original paused handle before handing off.
data = await restored.async_resume()
await restored.async_close()
```

Configure the same original draft, limits, policy callbacks, Actions, Skills,
TaskWorkspace, RecordStore and external ContextSources on the fresh handle.
Snapshots contain JSON data and resource identities, never settings, credentials,
clients or executable callbacks. Missing/changed bindings fail before readiness.
The current format conservatively rejects any Skill catalog change. Model-call
usage and elapsed time survive load, including time spent offline. A candidate
pause resumes final policies without repeating production.

After recovery, the first `get_data_object()` locally validates the saved
candidate against the rebound original output schema and caches the typed
object, including nested/root Pydantic types and `LongContent` declarations.
Load does not run output-model validators. Typed reads do not repeat model
requests or final validate/artifact/review policies; reconstruction errors raise.
Historical revisions rebuild from their own candidate, never the latest cache.

### Rework and retained revisions

```python
execution = agent.create_execution("request", limits={"max_model_requests": 3}).input(
    "Summarize: staging passed; production awaits approval."
)
previous = execution.get_result()  # Captures revision 0.
first = await execution.async_run()
revised = await execution.async_rework("Lead with the pending approval.", max_reworks=2)
assert execution.revision == 1  # Same object and execution ID.
assert await previous.async_get_full_data() == first
assert await execution.get_result(revision=0).async_get_full_data() == first
```

Rework produces a new candidate and returns its full result. New readers select
its revision; captured readers retain their original result, metadata and stream.
The original task and acceptance contract remain available alongside the feedback.
Request rework preserves the original `info` and `instruct` slots and appends the
current feedback and previous candidate; repeated revisions do not accumulate obsolete feedback.
Request producers revise the previous candidate; Plan keeps accepted clarification;
LongContent reuses an unchanged prefix and rewrites affected dependent sections.
LongTask asks the model to select retained work, then validates the IDs and
invalidates dependants (the remaining serial suffix for Flat). Selecting the
final candidate alone re-enters delivery without invalidating completed work. TaskBoard preserves
unaffected card results and checks reused file content identities.

Model-call usage, elapsed time, Flat iterations and TaskBoard ticks remain
cumulative. Use `create_execution("long_task", limits=...)` for an overall model
request cap; the legacy `create_task(limits=...)` request cap retains its per-step
meaning, while its wall-clock limit spans rework. `max_reworks` bounds revisions and cannot raise an already established
cap. A failed revision remains a failure while earlier candidates remain readable.
Previously dispatched Actions, including uncertain failures, require declared
`replay_safe` semantics or the host's explicit `allow_replay=True`; children cannot
relax ancestor protection. Artifact callbacks also require that explicit choice.
External effects are not rolled back and historical file references are not file
backups. Custom producers must declare their own rework support.

Safe-pause snapshots include revision history, producer state and replay protection.
Rebind the original task ID when using `create_task(..., task_id=...)`. Settled task
resources use the built-in task recovery contract; unsupported ContextSources fail
explicitly. Historical snapshot readers restore data, metadata and stream records,
not live provider result objects or original Python exception classes. A live
ExecutionResource without a checkpoint contract prevents save; settle or release
it first. Terminal cleanup releases this execution's owned execution scopes.

This supports the outer execution boundaries only. In-flight provider state,
active children, disconnected plan clarification and nested-budget restoration
remain unsupported. Legacy task resume does not establish those capabilities.
See `examples/agent_auto_orchestration/29_execution_controls_ollama.py` for a bounded
real-model snapshot handoff and same-execution rework.


## Submitted DAG Input

Submitted DAGs routed through the independent DynamicTask facade keep using DAG
runtime placeholders such as `${INIT.ticket}` and `${DEPS.lookup}` inside task
`inputs`. The graph input source is resolved in this order:

```text
async_run(graph_input=...)
> {"target": task_target}
```

This keeps DAG input explicit and separate from AgentExecution prompt routing:

```python
task = Agently.create_dynamic_task(
    target="review ticket",
    plan=graph,
    handlers=handlers,
)
snapshot = await task.async_run(graph_input={"ticket": "TICKET-OK"})
```

If an AgentExecution needs the result, pass the snapshot as ordinary evidence in
`input(...)`, `info(...)`, or a RecordStore record. The DAG snapshot does not by
itself mean the broader business goal is complete.

## Skills Semantics

`agent.use_skills(...)` and `agent.use_skills_packs(...)` register binding
intent on an Agent or AgentExecution using the same composition grammar as
`use_actions(...)`; there is no separate public Skill collection API. Each execution
resolves only those declarations into an exact-revision scope. It does not scan
the global SkillLibrary. `mode="model_decision"` uses a structured semantic
selector over that execution scope; `mode="required"` binds the selected
SKILL.md guidance fail-closed. The ordinary `model_request` or explicit
AgentTask strategy then consumes that guidance through TaskContext.

`run_skills_task(...)` is only a released result-shaped adapter over the same
AgentExecution path. SkillLibrary installs local materialized sources; remote
fetching belongs to authorized host code before installation.

## Process Stream

Agent execution stream items follow the familiar instant-stream shape:

```python
item.path
item.value
item.delta
item.event_type
item.is_complete
item.route
item.stage_id
item.task_id
item.action_id
item.graph_id
```

Executor routes bridge route and ModelRequest instant checkpoints so services
can stream route decisions, task/action progress, selected model field deltas,
and final semantic outputs.
If a TriggerFlow-backed route fails, the Agent execution stream is closed and
the original error is raised to the consumer instead of leaving
`get_async_generator(...)` waiting for more items.

For independent TaskDAG model nodes, consume the TriggerFlow runtime stream and
normalize `task_dag.model_field` items if the host wants field-level display:

```python
task = Agently.create_dynamic_task(target="reply", plan=graph, handlers=handlers)
execution = task.compile(graph).create_execution(auto_close=False)
async for item in execution.get_async_runtime_stream({"ticket": ticket}, timeout=None):
    if item.get("type") == "task_dag.model_field" and item.get("field_path") == "reply":
        print(item.get("delta") or "", end="", flush=True)
```

This keeps AgentExecution stream semantics separate from independent DAG
runtime stream semantics.
