---
title: Task context, files, and records
description: TaskContext, ContextReader, TaskWorkspace, and RecordStore ownership boundaries.
keywords: Agently, TaskContext, ContextReader, TaskWorkspace, RecordStore, progressive disclosure
---

# Task context, files, and records

Agently separates four responsibilities that were previously exposed through
one ambiguous Workspace concept.

| Owner | Responsibility | Does not own |
|---|---|---|
| `TaskContext` | Task-scoped information aggregate, direct entries, source bindings, and one internal derived index lifecycle | Files, persistence, semantic execution routing |
| `ContextReader` | Consumer/phase-bound retrieval and progressive disclosure; returns `ContextPackage` | Source storage, writes, side effects |
| `TaskWorkspace` | One explicit task file boundary: containment, mutation policy, format-aware readback, digest and file refs | Records, memory, snapshots, Skill selection |
| `RecordStore` | Durable records, retrieval indexes, links, checkpoints, TriggerFlow snapshots/events, memory persistence | Task files, prompt assembly, semantic relevance judgment |

`ContextSource` adapters make source-specific information readable without
moving source truth into `TaskContext`. A source exposes structural descriptors
through `async_enumerate_descriptors(...)` and bounded canonical content through
`async_read_exact(...)`; it does not choose cross-source relevance. Built-in
adapters cover SkillLibrary, TaskWorkspace, RecordStore, and SessionMemory
recall. Applications may attach their own source kind, such as an authorized
pinned-repository adapter.

A source may additionally implement the optional `ContextSourceScopedRead`
protocol. ContextReader uses it only after the canonical ref has been selected
and authorized, to locate a deterministic bounded range inside that ref. It is
a source mechanism—not a second index or semantic relevance owner—and ordinary
`async_read_exact(...)` remains the fallback.

## File boundary: TaskWorkspace

```python
from agently import Agently, TaskWorkspace

task_workspace = TaskWorkspace("./project", mode="read_only")
agent = Agently.create_agent("repo-review").use_task_workspace(
    "./project",
    mode="read_only",
)
```

The configured path is the ordinary file root. Existing external files remain
read-only unless `mode="read_write"` is selected. When a read-only boundary
needs a new execution product, Agently uses the execution fallback under
`.agently/files/<execution-id>/`; it does not overwrite an existing external
file. TaskWorkspace private locator and content-version identity metadata stays
under its own `.agently` area.

Agents without an explicit path receive isolated defaults under
`<entry-directory>/.agently/task_workspaces/<agent-id>`. Two Agents therefore
do not silently share a task file boundary.

Expose file operations to the model only when the task needs them:

```python
agent.enable_task_workspace_file_actions(
    read=True,
    write=True,
    expose_to_model=True,
)
```

File-format extension stays on the same owner. Register a
`TaskWorkspaceFileIOHandler` directly on the bound `TaskWorkspace`; there is no
separate Workspace manager or factory whose registry can drift from the file
boundary:

```python
task_workspace.register_file_io_handler(custom_file_handler)
```

TaskWorkspace produces stable locator and content-version facts for host-side
readback. A short application citation alias such as `[[ref:ref_1]]` is a
request-local display alias, not durable identity. Host code validates it and
maps it back to the canonical reference identity.

For a required AgentTask deliverable, the candidate bytes are first written as
a staged candidate and completely read back for terminal verification. Only
after verifier acceptance does TaskWorkspace perform digest-pinned atomic
promotion to the declared target and completely read the promoted bytes again.
Verification rejection leaves the previous target untouched; promotion or
post-promotion readback failure changes the task to a blocked result rather
than claiming delivery.

## Persistence boundary: RecordStore

```python
from agently.core.storage import RecordStore

record_store = RecordStore("./project-state", mode="read_write")
agent.use_record_store(record_store)

ref = await record_store.put(
    {"status": "verified"},
    collection="observations",
    kind="review_result",
    scope={"task_id": "review-42"},
)
```

The local provider materializes records at
`<root>/.agently/records/records.db`. Binding a RecordStore does not create or
change a TaskWorkspace. `SessionMemory` and TriggerFlow durability use
RecordStore ports.

TriggerFlow can opt out of its default RecordStore view with
`record_store=False`, or bind an explicit store:

```python
execution = flow.create_execution(
    record_store=record_store,
    runtime_resources={"runtime_event_store": record_store},
    auto_close=False,
)
```

AgentTask process state stays in memory and runtime logs by default. Enable
`record_store_recovery` only when restart recovery is required. Recovery refs
are RecordStore refs; final deliverable files remain TaskWorkspace refs.

### Retrieval rerank

`record_store.retrieve(...)` owns rerank policy: the structural gate, the
candidate window, drops, refill and packaging. Three engines can supply the
ordering, in this precedence:

| Engine | Selected when | Behavior |
|---|---|---|
| `handler` | `rerank_handler=` is passed | Your callable owns the decisions |
| `provider` | a rerank provider is bound | A reranker model scores relevance |
| `model` | neither is present | The default ModelRequest usefulness judgment |

Bind a reranker model the same way an embedding model is bound — through a
component provider:

```python
from agently import Agently
from agently.base import record_store_registry

rerank_agent = Agently.create_agent()
rerank_agent.set_settings("OpenAICompatible", {
    "base_url": "${ENV.RERANK_BASE_URL}",
    "api_key": "${ENV.RERANK_API_KEY}",
    "model_type": "rerank",
    "model": "${ENV.RERANK_MODEL}",
})

record_store = record_store_registry.create(
    "./project-state",
    mode="read_write",
    rerank_provider="agent",
    rerank_options={"agent": rerank_agent},
)

package = await record_store.retrieve("how do I configure a local model service", rerank=True)
print(package["diagnostics"]["rerank"])  # engine, provider, scored, unscored, dropped
```

`rerank_provider` accepts `"agent"`, `"callable"` (with
`rerank_options={"rerank_function": fn}`), a plain callable, or any object
implementing `RerankProvider.rank(query, documents, *, top_n)`. Binding is lazy:
configuring a provider makes no model call until a rerank actually runs.

A `RerankProvider` owns scoring only. Because a reranker model reports
relevance and not usefulness, the provider engine **reorders without dropping**
by default; downstream packaging still applies `top_n` and the length budget.
Pass `budget={"rerank_score_floor": 0.5}` when you explicitly want a score cut —
those drops appear as `rerank_drop` in `omitted`. Candidates the provider did
not score keep their relative order behind the scored ones rather than being
discarded on absent evidence. If the provider fails, retrieval degrades to
deterministic order with diagnostics; it never silently switches to the model
engine, because engine choice is your configuration, not a failure path.

## Information delivery: TaskContext and ContextReader

```python
from agently.core.context import TaskContext
from agently.core.storage import RecordStoreContextSource
from agently.types.data import ContextBudget, ContextReadIntent

task_context = TaskContext("review-42")
task_context.put(
    role="instruction",
    content="Never modify source files during review.",
    required=True,
)
task_context.attach(
    RecordStoreContextSource(record_store),
    binding_id="review-records",
    scope="task",
)

reader = task_context.reader(
    consumer="review-planner",
    phase="planning",
    budget=ContextBudget(max_chars=6000, max_blocks=12),
)
package = await reader.async_read(
    ContextReadIntent(
        query="What evidence is relevant to the failed review?",
        filters={"source_kinds": ["record_store"]},
    )
)
```

`TaskContext` is the only task-information aggregate. TaskContext owns source
bindings and one internal `ContextIndex` that builds, synchronizes, invalidates,
and reuses derived source partitions. ContextIndex is not a public manager and
never becomes canonical source truth. TaskContext creates readers with
`task_context.reader(...)` and restores their exported state with
`task_context.restore_reader(...)`; constructing or restoring a
`ContextReader` independently is not supported. The reader is a public,
consumer/phase-bound handle, comparable to an execution handle owned by its
aggregate. `ContextPackage` is the immutable value returned across a request,
AgentTask, Blocks, or persistence boundary; it is not another context owner.

Each reader pins one TaskContext/source revision snapshot. If that snapshot
is already stale before a read, refresh it explicitly or create a new reader.
If candidate listing itself advances a source revision while the TaskContext
structure remains unchanged (for example, a source establishes a lazy read
view), ContextReader optimistically re-pins and recollects once. Repeated or
concurrent mutation still fails closed. Required and explicitly requested blocks
cannot be silently dropped. Optional prose relevance uses an Agently
`ModelRequest` semantic selector when more than one candidate needs judgment;
selection keys are host-issued and validated before canonical records are
reconstructed.

The native `RecordStoreContextSource` revision includes its bound view's effective
search scope and visible record metadata. Out-of-scope writes do not expire that
view; visible changes still require refresh. Page and exact reads bind to the
same read-only transaction. This source rejects exact reads outside its scope,
but public `RecordStore.read_bounded()` does not thereby become an authorization
boundary. Custom providers, read overrides and explicit source revisions retain
their existing path, not an automatic native scoped-revision guarantee. Native
revision checks still scan metadata; validate costs for large record collections.

ContextIndex enumerates source descriptors into revision/profile/provider-keyed
partitions. It may use `structural`, `lexical`, or host-configured `hybrid`
candidate retrieval, but exact bytes still come from the source's
`async_read_exact(...)` port or, after ref selection, its optional deterministic
scoped-read port. Reusable partitions may avoid rebuilding
unchanged embeddings; vector failure degrades only when policy allows it and is
reported in package diagnostics. ContextReader owns consumer-local offsets,
deduplication, optional ModelRequest selection, exact readback, and package
budgets. The returned package exposes per-binding `source_coverage` and index
diagnostics, never internal cache keys or provider vectors.

The immutable ContextPackage retains complete omission and diagnostic facts for
audit. Model-hot AgentTask views bound repetitive optional omission details and
add reason counts; required delivery still fails closed before that projection.

Context delivery is media-aware. Plain text and source-parsed text may enter a
package; the built-in TaskWorkspace source parses supported PDF, DOCX, XLSX,
and PPTX files before disclosure. A document whose parser or optional
dependency is unavailable is ref-only. A PDF or Office descriptor and exact read
must both preserve `context_representation=parsed_text`, and the exact body must
be text; a caller-supplied string without that parser provenance is not admitted.
Known non-text MIME types or file extensions cannot be overridden by a conflicting
`content_kind="text"` claim. Conflicting type signals fail closed to a non-text
or unknown representation. Mainstream Python, Node.js, Go, C, and C++ source
extensions are classified as text, including empty source files.

Images, archives, executables, audio, video, unknown formats, and arbitrary bytes
are never coerced into guessed text. Their model-visible projection is limited to
the canonical filename/ref. Source-provided summaries, OCR text, and inferred
contents are stripped; MIME, digest, and size facts may remain host-side for audit.

An image is ref-only unless the exact consumer explicitly declares image
attachment support:

```python
from agently.types.data import ContextConsumer

reader = task_context.reader(
    consumer=ContextConsumer(
        "visual-reviewer",
        capabilities={"attachments": {"image": True}},
    ),
)
```

For AgentTask, declare the same capability on the selected task strategy:

```python
execution = agent.goal("Review the attached chart").strategy(
    "taskboard",
    context_consumer_capabilities={"attachments": {"image": True}},
)
```

Agently does not infer vision support from a model name or from a generic
`attachments=True` flag. When explicitly supported, ContextReader emits a
validated image attachment block and AgentTask binds it through the
ModelRequest attachment channel; the data URL is not serialized into the text
context pack. Without that explicit capability the model receives only the
filename/ref, never a generated summary or OCR substitute. Image interpretation
remains model-owned. An invalid or empty attachment fails the selected read
instead of falling back to a filename-based guess.

Required content remains fail-closed when it cannot fit. A caller that has
explicitly accepted a lossy projection may request
`metadata={"required_overflow": "lossy_digest"}`. Skill sources then return a
bounded `completeness="lossy"` outline with the immutable full ref, ordered
section refs, original size, and omission facts; they do not silently truncate
the authority-bearing instructions. Optional section candidates still use the
semantic selector. A host-only preflight that intentionally wants no optional
selection may additionally set `optional_selection="none"`.

AgentTask carries the same policy in its context budget:

```python
execution = agent.goal(goal, success_criteria=criteria).strategy(
    "taskboard",
    context_budget={
        "chars": 12_000,
        "required_overflow": "lossy_digest",
    },
)
```

Use this only when the Skill or caller accepts lossy disclosure. Otherwise use
a larger/focused consumer or let the required Skill fail before business work.

`source_kinds` is structural source filtering, not semantic routing and not a
closed enumeration. Valid values come from the source kinds actually attached
to that TaskContext, including built-in adapters such as `task_workspace`,
`record_store`, `skill_library`, `session_memory`, or `pinned_repository` when present. Unknown
kinds fail before source enumeration.

AgentTask creates an independent reader/package for each concrete planner,
worker, control-card, and verifier request. A successful response records a
`ContextConsumption` with the exact package, response/request id, phase, and
block ids; failed requests record no consumption and emit no
`skills.context.bound` event. AgentTask meta exposes both `context_packages`
and `context_consumptions` for audit.

## AgentExecution ownership

Every AgentExecution owns one TaskContext and execution-scoped views of the
Agent's TaskWorkspace and RecordStore. AgentTask reuses the exact TaskContext
and TaskWorkspace view handed to it by AgentExecution. Skills are bound as
immutable SkillLibrary revisions through a Skill ContextSource; they do not
create a Skills route or execution engine.

With `record_store_recovery` enabled, the durable snapshot also preserves
TaskContext direct entries, reconstructible built-in source bindings, reader
disclosure history, packages, and consumptions. Skill sources are rebuilt from
their exact immutable `revision_ref`. Custom ContextSources are not
automatically reconstructible and fail resume explicitly instead of disappearing.

Use `execution.async_read_task_context(consumer_id=..., phase=..., intent=...)`
when the ordinary AgentExecution should build the package. `intent` accepts a
query string or `ContextReadIntent`; when omitted, AgentExecution uses its task
target. Blocks accepts an already
bound `context_reader` for its read-only `context_read` block.
