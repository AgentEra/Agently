---
title: Context Engineering
description: How to put background knowledge in front of a model without bloating the prompt.
keywords: Agently, context, info, session, KB, retrieval, instruct
---

# Context Engineering

> Languages: **English** · [中文](../../cn/requests/context-engineering.md)

The model only knows what fits in its context window. Context engineering is the discipline of choosing what goes in, where it goes, and what gets omitted.

## Where context can come from

| Source | Where it lands | Lifetime |
|---|---|---|
| `role` / `system` slot | system message | persistent for the agent |
| `info` slot | system or user (impl detail) | persistent or per-request |
| `instruct` slot | user message | persistent or per-request |
| `input` slot | user message | per-request |
| Session chat history | user/assistant messages | accumulates across requests |
| Session memo | system message | persists, written by custom resize handlers |
| Knowledge base retrieval | injected by retrieval code | per-request, on demand |
| `TaskContext` sources | bounded `ContextPackage` blocks | per task, consumer, and phase |
| Tool / MCP results | tool messages | accumulates within one tool loop |

Use the right slot for the right job:

- **role / system** — who the model is, hard rules (tone, persona, refusal patterns).
- **info** — facts that don't change between calls (product catalog, severity levels, formatting conventions).
- **instruct** — how to do *this kind* of request (steps, ordering, output style).
- **input** — the single payload that varies per call.
- **chat history** — what the user and model said in this session.
- **memo** — application-defined compressed long-term context.
- **KB** — large knowledge that's not always relevant.

Don't put everything in `input`. Don't put per-request payload in `info`.

## When to use which

| You have … | Put it in … |
|---|---|
| The agent's persona / tone / capability rules | `role` (`always=True`) |
| A fixed enumeration the model must know about (e.g., severity codes) | `info` (`always=True`) |
| Step-by-step instructions for one kind of task | `instruct` (`always=True` if the agent does only this kind) |
| The variable payload of one call | `input` |
| The previous turns of a conversation | session chat history |
| 100k tokens of company docs | KB with retrieval, not the prompt |
| Recently retrieved facts that are relevant *this turn* | `info` for this request only |

## Use TaskContext for task-scoped progressive disclosure

Prompt slots own model-facing material; they do not own a task's source
catalog. When one task may need Skills, files, records, SessionMemory recall,
evidence, or a pinned repository, bind those sources to `TaskContext` and read
one consumer/phase-specific `ContextPackage` through `ContextReader`.

TaskContext owns an internal `ContextIndex`. Sources contribute structural
descriptors and bounded exact reads; the internal index builds reusable
revisioned structural, lexical, or optional hybrid partitions. ContextReader
queries that index, performs any ModelRequest-owned optional relevance
selection, reads canonical source content, and applies disclosure budgets. The
model receives only the resulting blocks, refs, omissions, coverage, and
diagnostics—not an entire source tree or internal vectors.

Configure the derived index on its public aggregate owner. The embedding
provider is a mechanism adapter only; the consumer-bound ModelRequest selector
still decides semantic relevance:

```python
task_context.configure_index(
    strategy="hybrid",
    embedding_provider=embedding_provider,
)
```

In hybrid mode, vector/lexical ranking narrows the optional descriptor window
to the reader's `max_blocks` before semantic selection instead of multiplying
that window fourfold. The selector may still omit every candidate or choose an
ordered subset by relevance; the Host enforces the delivery budget. When structural filters leave one
canonical candidate, the index skips a query embedding because there is no
remaining order to improve.

After one canonical ref is structurally selected, a source may optionally
support deterministic bounded location inside that ref. This source-scoped read
does not choose relevance or accept evidence; `ContextReader` still owns the
read session and falls back to the ordinary bounded exact read when the optional
port is absent. An exact non-wildcard path that leaves one authorized candidate
does not need another model request merely to select that same candidate.

The complete `ContextPackage` remains available for audit. AgentTask model-hot
projections bound repetitive optional-omission details and carry aggregate
reason counts, so an unselected source catalog does not become prompt content.
When scoped evidence snippets are already present, each snippet carries one
host-issued `reference_id`; repeated locator/body copies stay out of the hot
prompt while canonical provenance remains host-side. The host joins each body
one-to-one with its execution block, ContextBlock, source revision, binding,
and canonical ref before disclosure. A missing or ambiguous join excludes the
body and emits a diagnostic. Opaque execution/block/binding identities remain
host-side; the model selects only `reference_id` plus relevant source labels.

A scoped-retrieval plan may reserve at most 64 model-visible results across its
query groups (the sum of each `max_results`). Overflow is rejected before the
Blocks graph is compiled; it is never silently truncated. Split larger reads
into consumer-owned continuation batches.

Embedding usage and model prompt usage are separate facts. A cache hit or
smaller ContextPackage can explain an efficiency change, but only complete
provider-observed prompt-token usage from comparable requests proves a model
input-token reduction. Never convert character counts into billed tokens.

See [Task context, files, and records](workspace.md) for the source contract and
ownership boundaries.

## Keep info diffable

`info` accepts dicts and the framework renders them. This is preferable to baking JSON strings into the prompt yourself — diffs stay readable, and the framework can render to YAML / JSON / pseudo-table forms consistently.

```python
agent.info({
    "severities": ["P0", "P1", "P2", "P3"],
    "format": "Use markdown bullets, no preamble.",
}, always=True)
```

## Don't carry tool catalogs by hand

If you're using actions / tools, the framework already injects the tool catalog when the model needs to plan a tool call. Don't manually copy tool descriptions into `info`. See [Action Runtime](../actions/action-runtime.md).

## Session vs KB vs `info`

| Scenario | Best fit |
|---|---|
| "Remember the user's name across this conversation" | session chat history |
| "Remember the user's preferences across many conversations" | custom session resize / memo, or an application-level user profile |
| "Look up the right snippet from a knowledge base" | KB retrieval, then put the retrieved snippets in per-request `info` |
| "The model always needs this fixed list" | `info(always=True)` |
| "The user just sent a 500-word problem statement" | `input` |

## Compression beats truncation

When the context window starts to fill up:

- The default Session only trims the window by `session.max_length`; when you need summaries, register a custom resize handler and write the summary into session `memo`. See [Session Memory](session-memory.md).
- For task sources, prefer bounded TaskContext reads and reusable refs. For a
  truly one-off long input, summarize before the request rather than truncating
  mid-sentence.

## Per-request info without polluting the agent

```python
result = (
    agent
    .info({"retrieved_snippets": chunks}, always=False)  # request-only
    .input(question)
    .output({...})
    .start()
)
```

Without `always=True`, `info` is set only for this call.

## See also

- [Prompt Management](prompt-management.md) — slot semantics in detail
- [Session Memory](session-memory.md) — chat history and memo
- [Knowledge Base](../knowledge/knowledge-base.md) — retrieval-before-prompt pattern
- [Action Runtime](../actions/action-runtime.md) — tool catalogs are injected automatically

### Task preparation and source reuse (4.1.4.9)

Resource selection receives the complete execution task together with the current
read intent and phase. Later task goals do not expand the current read scope.
Candidates carry an offered key, role, summary and completeness. Byte estimates,
limits and canonical identities stay Host-side. Required instruction bodies can
explain when optional resources should be read; optional bodies are read only after
selection. Skill resource catalogs are no longer offered as extra `resource-index`
candidates, and resource summaries omit byte counts. Direct source exact reads of
the catalog remain available.

The built-in TaskWorkspace source reuses per-file observations and bounded, complete
small UTF-8 text bodies. It rechecks contained paths and local file identity on use;
replacement, deletion or changed file stat invalidates observations. It still checks
pinned revisions and rejects files that change during a read. This optimization
assumes reliable local filesystem identity/stat information. Large, BOM-bearing,
parsed-document and custom-handler bodies use ordinary exact reads. It does not
change write permission, artifact promotion or final readback, and introduces no
public cache or index manager. Refresh stale readers explicitly.

The complete task projection also reaches context selection inside long_task planning, execution, and verification, while each read keeps its own intent and phase.
