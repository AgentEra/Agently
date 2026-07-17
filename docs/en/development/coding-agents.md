---
title: Coding Agents
description: Using Agently with coding agents like Codex, Claude Code, Cursor — the official Agently Skills.
keywords: Agently, coding agents, Codex, Claude Code, Cursor, Skills
---

# Coding Agents

> Languages: **English** · [中文](../../cn/development/coding-agents.md)

If you're building Agently apps with the help of an external coding agent (Codex, Claude Code, Cursor, etc.), the canonical way to give that agent good Agently context is the **official Agently Skills** packages, distributed in the `Agently-Skills` companion repo.

This page is about the **companion repo** path, not the framework-side runtime skill consumer. If you want Agently itself to install and apply external skills while serving real tasks, read [Skills Compatibility](skills-executor.md).

## What are Agently Skills

A skill is a bundle of:

- a `SKILL.md` describing what the skill does and when to apply it
- references — focused docs the coding agent can pull on demand
- examples — minimal runnable snippets
- validators — scripts the agent can run to confirm the user's project follows the recommended structure

Skills are **not** just documentation. They're structured for coding agents: each one tells the agent which problem it solves, what the recommended path looks like, and how to verify that the user's code is on that path.

## Companion skills vs framework skill execution

Keep these separate:

- `Agently-Skills` companion repo: skill bundles for external coding agents
- Agently runtime Skills: framework capability owned internally by SkillsManager,
  with `SkillsExecutor` retained as a compatibility facade

The companion repo does not become a runtime dependency of your Agently app. It remains a guidance package for coding agents.

## Current skills

| Skill | Use when the user is |
|---|---|
| `agently` | starting fresh — picking the right structure for a new Agently project |
| `agently-request` | model setup, prompt management, structured output, response reuse, session memory, embeddings, retrieval |
| `agently-runtime` | Action Runtime, built-in actions, MCP, ExecutionResource, FastAPI exposure, DevTools wiring |
| `agently-dynamic-task` | model-generated or app-submitted DAG planning, validation, and execution |
| `agently-triggerflow` | needing branching, concurrency, pause/resume, save/load |
| `agently-migration` | migrating from LangChain, LangGraph, LlamaIndex, CrewAI, or similar systems |

The current public catalog generation is `v2`. The actual default skill list lives in `Agently-Skills/skills/` and should contain only these 6 skills.

## Installing the skills

```bash
git clone https://github.com/AgentEra/Agently-Skills
```

Then point your coding agent at the skill directory according to its own loader:

- **Claude Code** — `claude` skills in `~/.claude/skills/` or project `.claude/skills/`
- **Codex** — see your Codex installation's skill / context loader
- **Cursor** — load via the project's rules / context surfaces

The skills are plain text + scripts; nothing Agently-specific runs at install time.

For CLI-based installs, the default `app` bundle is:

```bash
for skill in \
  agently \
  agently-request \
  agently-runtime \
  agently-dynamic-task \
  agently-triggerflow
do
  npx skills add AgentEra/Agently-Skills --agent "$AGENT" --skill "$skill" -y
done
```

Add `agently-migration` only for migration projects. Historical catalogs are kept on frozen archive branches instead of the default branch; the V1 12-skill catalog is archived on `update/archive-legacy-v1-catalog` and last supports Agently `4.1.1`. Do not add archived catalogs to a coding agent's normal search path for new projects.

## Why skills, not just docs

Documentation tells humans what's possible. Skills tell coding agents what's recommended **right now** — including which APIs are deprecated, what the current lifecycle looks like, and what to verify before reporting "done". This keeps coding agents aligned with the framework's evolution without users having to update their own context manually.

In particular, skills must NOT recommend deprecated paths like `.end()`, `set_result()`, `wait_for_result=`, or the old `runtime_data` API. If you find a skill recommending one of these, file an issue against `Agently-Skills`.

When application development exposes a missing framework capability, behavior
that does not match docs, examples, Skills guidance, or the expected
model-application responsibility boundary, an unexposed or unfriendly API, or a
responsibility Agently should own but business code must patch with
workarounds, glue, private wrappers, or duplicated mechanisms, generate a
concise issue report. Recommend filing it at
[`github.com/AgentEra/Agently`](https://github.com/AgentEra/Agently/issues) with
the scenario, expected framework responsibility, actual behavior, workaround,
minimal reproduction or affected docs/examples, and compatibility concern.
The issue must make the encountered scenario concrete enough to explain what
kind of model-application development problem was being solved. If business
details are confidential, anonymize or omit them, but still describe the
application category, workflow shape, decision point, and framework
responsibility needed for maintainers to understand the issue.
For manual filing, provide the issue content and filing method to the user only.
Ask before automatic filing; if the user approves, first verify local GitHub
submission capability and permission, reproduce that the problem still exists,
and re-check Agently docs, examples, Skills guidance, and API usage to confirm
the report is not caused by overlooked information or incorrect usage. Before
creating the remote issue, sanitize the body so it does not include secrets,
tokens, customer data, or local absolute paths.

When adding a framework deprecation, route the warning through `agently.utils.DeprecationWarnings.warn_deprecated_once(...)` or the `agently.utils.warn_deprecated_once(...)` alias with a stable API key. Do not add direct `warnings.warn(..., DeprecationWarning, ...)` calls; deprecated API warnings are intentionally once per API per Python process and respect `runtime.show_deprecation_warnings`.

## Simulation-first model experiments

When problem discovery or strategy tuning is likely to require multiple model
rounds, ask the development agent to self-simulate a realistic target request,
response, and behavior chain first. Define the acceptance criteria up front and
iterate the prompt, output schema, topology, instrumentation, and failure paths
until this same-context **warm preflight** meets them without calling the target
model API. Mark every artifact `simulated`: it is low-cost hypothesis and
protocol-design material, not an observed fact or real-model evidence.

Simulation can exercise content, schemas, branches, error envelopes, and the
expected shape of accounting metadata. It cannot accurately reproduce
provider-generated request IDs, token accounting, cache or billing fields,
latency, finish behavior, or other provider telemetry. Mark invented values
`synthetic`, estimates `estimated`, unavailable fields `unavailable`, and
recorded-trace playback `replayed` with its source. Only current values returned
by the target provider are `observed`; never add simulated usage or metadata to
real experiment totals.

After the warm preflight stabilizes, choose at most one feasible isolated
carrier for a **cold preflight**:

- a native coding-agent subagent with fresh or no inherited context;
- a handshake-verified ACP coding agent; or
- a fresh isolated task or session of the development agent.

ACP is one option, not a requirement, and the workflow does not need to run all
three. Give the selected carrier only task-relevant input, authoritative
`info`, `instruct`, the exact `output` contract, and the written acceptance
criteria. Do not disclose the intended answer, prior conclusions, the full
conversation, customer secrets, or unrelated files. Enforce tool, network, and
file boundaries plus call and time limits in the host. Mark the result
`simulated` and `cold_preflight`; unless the carrier proves exactly one
underlying model request and exposes its accounting, also mark it
`agent_simulation`, not `single_model_request_simulation`.

Direct simulation in the existing context is only `warm_preflight`; it never
counts as cold review. If no isolated carrier is available, record
`cold_preflight=skipped` with the reason and continue to the smallest
representative, bounded real-model comparison. Do not block the real check or
relabel the warm preflight as cold. Base the final conclusion on real traces;
when simulation and reality differ, the real trace wins and the design returns
to the analysis/revision loop. Use authorized project- or developer-owned test
credentials with explicit call, concurrency, retry, and budget limits. Do not
consume customer API credentials or quota without explicit customer
authorization and a disclosed maximum call count or spend.

## Post-4.1 defaults

When you audit or author guidance for Agently `4.1+`, these are the defaults coding agents should prefer:

- API shape: apply Occam's razor. Do not add a new entity, method, facade, or compatibility patch when an existing surface already expresses the concept. If a name is unclear, prefer a narrow alias or documentation clarification over another overlapping method.
- Structured output: for fixed required leaves, mark `(TypeExpr, "description", True)` directly in `.output(...)`. Use `(TypeExpr, "description", "not_null")` only when empty values must retry. Use manual `ensure_keys=` only for conditional or runtime-dependent paths.
- Identifier joins: when the model judges, selects, ranks, or references host
  records, give each candidate one host-issued trusted `selection_key` plus only
  the facts relevant to the task. The model should return that one key with its
  judgment. Host code must validate it against the offered set, then look up and
  reconstruct canonical ids, UUIDs, metadata, opaque refs, and the full record
  deterministically. Do not send identity-heavy complete objects or ask the
  model to reproduce several ids and unrelated `meta`; that is a transcription
  anti-pattern, not useful reasoning or output control. The `selection_key` is
  an application-local projection, not another canonical identity. Declare it
  as a required string constrained to the offered key set, and reject unknown
  or disallowed duplicate keys before lookup.
- Actions: new code should start from `@agent.action_func` and `agent.use_actions(...)`. `tool_func`, `use_tool`, and `use_tools` are compatibility aliases, not the primary recommendation.
- TriggerFlow lifecycle: treat `close()` / `async_close()` and the close snapshot as the canonical completion path. Do not recommend `.end()`, `set_result()`, `get_result()`, or `wait_for_result=` as the normal starting point.
- TriggerFlow state: use `get_state(...)` / `set_state(...)` for per-execution data. Treat `flow_data` as an intentionally risky shared scope, not a normal state store.
- Settings loading: when provider settings live in files, prefer `Agently.load_settings("yaml_file", path, auto_load_env=True)`. Keep `Agently.set_settings(...)` for inline overrides.
- Execution style: prefer async-first for services, streaming, and workflows. Treat sync APIs as wrappers for scripts, REPL use, or compatibility bridges.
- Complex execution planning: map real serial and parallel dependencies before choosing a topology. Use async Agently APIs, provisional `instant` structured streams for UI or cancelable/idempotent preparation, and TriggerFlow signals/joins for application-owned coordination; wait for the final parsed result and configured validation before business decisions or irreversible effects. Run independent work concurrently with bounded execution, operator, model-scheduler, and host admission limits; expose host worker/thread-pool settings when blocking code is present. An unexamined all-serial design is an anti-pattern.
- Result reuse: when one model call must be consumed as text, parsed data, metadata, or structured stream updates, prefer `get_result()` and reuse the same result object rather than re-requesting.
- Result consumption: if no caller actually consumes progressive output, await
  `result.async_get_data()` directly. A discard-only
  `get_async_generator(type="instant")` loop followed by the final getter is an
  anti-pattern: it adds stream queue, iteration, and parser work without
  publishing or using any intermediate value. Stream only when the application
  forwards deltas, updates UI/state, records events, or starts explicitly
  cancelable/idempotent preparation; then read final data from the same result.
- Retrieval citations: give the model one short trusted `ref_id` (or an existing
  evidence `cite_as`) per selected source and require inline
  `[[ref:<ref_id>]]` tokens such as `[[ref:r1]]`. Host code validates and
  resolves those tokens, renders safe links, and separately emits complete
  authorized source-card records for hover cards, source lists, or attached
  result cards. Avoid bare `${ref_id}` because `${...}` is already Agently
  placeholder syntax; do not ask the model to reproduce URLs or full retrieval
  metadata.
- Task execution quality: when a goal-pursuit task must use a particular capability (an Action, Skill, or Skill pack), do not lean on a strong instruction in the prompt or a business-specific special case to force or check it. Express the requirement as framework contract: make capabilities visible to the planner (`planner_capabilities`), bound action steps with structured `step_scope` that reaches the ActionRuntime boundary, and require completion evidence with a structured `capability_evidence_requirements` entry. For Skills steps that may produce long artifact text, configure the Skills route output format instead of forcing large raw content through JSON streaming. If a Skills step needs file writes, reads, shell calls, HTTP calls, or other side effects, explicitly grant the action/tool scope through route/effort configuration, declare required side-effect actions when the React strategy should stop after they succeed, and require `action_succeeded` evidence for the host actions; Skills provide guidance, while ActionRuntime owns callable execution and evidence. Prior-step Workspace context must preserve action evidence before bulky execution metadata. TaskDAG / DynamicTask is not an AgentTask bounded-step strategy; use TaskDAG / DynamicTask separately when the application or visual automation surface owns the submitted graph. The AgentTask host guard checks requirements deterministically against execution evidence; the prompt is explanatory, not the guarantee. Keep scenario-specific checks (visual fingerprints, domain names, source choices) in examples and tests, never in framework paths.

## When to write your own skill

If your team has internal patterns layered on top of Agently (a particular project layout, a wrapped agent factory, a custom action set), consider authoring a private skill bundle that mirrors the public Agently Skills format. Coding agents will then apply your team's conventions consistently across projects.

## Validation scripts

Several skills ship validation scripts (e.g., `validate/validate_native_usage.py`). Coding agents can run these to confirm a user's project follows the recommended path before declaring a task complete. For example, the TriggerFlow validator checks that no deprecated API is being used as the recommended starting point.

Feature acceptance also requires spec reconciliation: update the relevant spec to the final implemented design, move fully landed planned specs into `spec/implemented/`, and update `spec/README.md` in the same work item.

User-visible feature work must add or update examples for the scenario the feature enables. Keep the example runnable in its declared environment, aligned with the current recommended API, and explicit about the important runtime behavior. Its `Expected key output` comment should preserve stable key values from one real run, not a generic "shows X" description. When the behavior is not obvious from output alone, add concise working-principle notes or an ASCII flow diagram in the example comment.

For Agently `4.1.3` development work, include `examples/agent_auto_orchestration/` when the task touches default `agent.start()` routing, `agent.create_execution()`, or Agent process streaming. Treat local smoke scripts in that directory as infrastructure checks only; model-app or acceptance claims still require real DeepSeek or local Ollama examples. For the 4.1.2.5 foundation line, treat `examples/cookbook/`, `examples/action_runtime/`, `examples/execution_resource/`, `examples/builtin_actions/`, `examples/trigger_flow/`, `examples/dynamic_task/`, and `examples/fastapi/` as the recommended starting surfaces. Treat `examples/archived/` as compatibility reference only.

When reporting API, recommended usage, examples, or compatibility changes, include concise sample code that shows the updated usage shape. Prefer current usage snippets or before/after snippets over abstract prose when that makes the change easier to inspect.

## See also

- [Action Runtime](../actions/action-runtime.md) — the architecture skills assume for tool use
- [DevTools](../observability/devtools.md) — observation, evaluation, and interactive wrapper paths
- [TriggerFlow Compatibility](../triggerflow/compatibility.md) — the migration path skills steer toward
