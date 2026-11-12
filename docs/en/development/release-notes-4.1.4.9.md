---
title: Agently 4.1.4.9 Release Notes
description: Unified long-task Loop, effective context, independent model roles, SystemOne and instant streams.
---

# Agently 4.1.4.9 Release Notes

This is an unpublished release candidate. Changes are relative to released 4.1.4.8.
Python remains 3.10+ with Agently-Stage >=0.3.8,<0.4.0. Optional DevTools is
recommended at >=0.2.0,<0.3.0; use the V3 Skills catalog aligned with 4.1.4.9.

## Recommended usage

With a configured model and a registered `read_document` Action:

```python
execution = (
    agent.create_execution("long_task")
    .input("Read the latest roster; summarize confirmed attendees, materials and missing information.")
    .use_actions("read_document")
    .output(str)
)
result = await execution.async_get_data()
```

New tasks use one Loop and a model-editable Markdown checklist. ContextPackage
supplies effective material and ActionRuntime returns actual observations. Use
`.artifact("reports/summary.md")` when Host should write the final business result.

```python
# Retained 4.1.x compatibility; old task state stays on its original producer.
legacy = agent.create_task(goal="Existing task", execution="taskboard")
# Recommended for new tasks, not automatic conversion of old state.
current = agent.create_execution("long_task").input("New task")
```

## Core changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Long tasks | One decision Loop, editable checklist and actual Action observations; no default per-card execution/judge chain | `create_execution("long_task")` | Legacy strategies and task-id recovery remain in 4.1.x; removals belong to 4.2 | `examples/agent_task/unified_loop_roster.py`, `tests/test_long_task_loop.py` |
| Context / Skills | Full task facts for selection, reused reads and less redundant indexing/Host-budget projection | Existing Skills and ContextPackage APIs | Host budgets, authorization and scope remain effective | `examples/skills_executor/12_complete_task_selection.py`, context/Skill tests |
| Model roles | Independent LLM/VLM/OCR/embedding/STT/TTS configuration | [Model capabilities](../models/capabilities.md) | No implicit cross-role credential/options inheritance | Capability examples and protocol tests |
| SystemOne / Jev | Independent model for output templates, Probability/Choice/Score and field dependencies; LLM stages forward instant events | Configure `system_one`; ordinary fields retain the ordinary model | Active provider failures do not switch models; unconfigured use stays on the ordinary path | `tests/test_system_one.py`, `tests/test_judgment_output.py` |
| instant validation | Only actual SystemOne stages suppress replay after a complete field is observed | Ordinary instant retains bounded validation retry; final data is authoritative | Ordinary Agent, ModelRequest and ordinary LLM composition stages preserve retry | Pinned example 06, validate and SystemOne regressions |
| Audio input | Optional acoustic detection and bounded PCM/WAV segmentation with original sample offsets | Explicit `AudioInputOptions` | Defaults unchanged; no semantic denoising or arbitrary codec promise | Audio preprocessing examples and tests |
| Installed typing | Explicit re-exports of existing public types for companion consumers | Existing import paths | Same runtime objects | Installed-package typing smoke and DevTools typing |
| Deferred | 4.2 compatibility removal, active-child/arbitrary inner snapshots and lossless legacy-state conversion | Use declared settled pause boundaries | Not promised by this release | [Long-task migration](../start/long-task-loop.md) |

## instant migration

```python
response = agent.create_request().input("Task").output({"answer": str}).get_result()
async for item in response.get_async_generator(type="instant"):
    render_provisional(item)  # Application-owned display function.
# Ordinary validation retry may replace provisional fields; reconcile with final data.
final = await response.async_get_data(max_retries=2)
```

Ordinary ModelRequest and Agent validation retries remain available after instant
consumption. Only actual SystemOne stages suppress replay after observing a complete
field; ordinary predecessor and successor stages retain bounded retries. Provider transport retry and `$status` replay boundaries keep
their provider-owned contract; this change concerns output validation retries.
Do not execute irreversible effects from provisional values. See
[model streaming integration](../triggerflow/model-integration.md).

## Known limits

Model completion judgments are not correctness guarantees. Qwen samples still
include stale draft text beside corrected numbers and a valid negative conclusion
marked blocked. Select explicit `validate` / `review` for application needs.
Blocked tasks retain useful work and relevant unfinished requirements, uncertainties,
risks and missing information. Disclosure alone does not complete missing work.
Host still checks exact destinations and required Actions; chat text or fallback
files cannot fulfill them. Legacy TaskBoard ordinary answers finish at their
finalizer, retaining necessary checks for explicit delivery/capability contracts.
Small experiments do not establish a universal accuracy, latency or convergence guarantee.
