---
title: Agently 4.1.4.5 Release Notes
description: Opt-in long-output continuation and Stage-backed TriggerFlow task lifecycle ownership.
keywords: Agently, 4.1.4.5, long output, TriggerFlow, Agently-Stage, settlement
---

# Agently 4.1.4.5 Release Notes

Agently 4.1.4.5 strengthens two runtime boundaries without changing their
semantic owners:

- `AgentExecution` can opt into lossless continuation when a direct
  `ModelRequest` ends with an observed length or incomplete terminal;
- `TriggerFlowExecution` now uses Agently-Stage as the direct owner of its
  process-local managed tasks instead of reproducing a second task scope.

## Opt-in long-output continuation

Call `.ensure_long_output()` on an unstarted direct `AgentExecution` when the
business result must survive a provider response-window boundary:

```python
result = (
    agent
    .input({"topic": "runtime ownership"})
    .instruct("Write the complete technical report.")
    .ensure_long_output()
    .get_result()
)
```

The first request keeps its original prompt and output contract. Continuation
starts only after an observed provider/parser terminal explicitly reports
length or incomplete output. Accepted text or structured units are committed
append-only, stored through `TaskWorkspace`, read back, and validated against
the original output contract before delivery.

This policy is disabled by default. It supports plain text and declared JSON
output contracts and cannot be combined with an explicit AgentTask strategy.
Ordinary successful responses remain single-request executions.

See [Output Control](../requests/output-control.md) and
[`examples/basic/ensure_long_output.py`](../../../examples/basic/ensure_long_output.py).

## Stage-backed TriggerFlow task ownership

Agently now requires `agently-stage >=0.3.5,<0.4.0`.

Each `TriggerFlowExecution` directly owns one real Stage:

- execution-created caller-loop tasks enter through `Stage.create_task(...)`;
- genuinely pre-existing tasks enter through `Stage.adopt(...)`;
- Stage is the single live task/origin inventory and settlement owner;
- TriggerFlow retains workflow failure policy, one close deadline,
  RuntimeEvent projection, and close snapshots.

The former private `StageManagedTaskScope` adapter has been deleted. This is
not a public Stage API exposure through Agently: EventCenter, SignalNet,
TriggerFlow, RuntimeEvent, and AgentExecution keep their existing semantic
ownership.

Hidden runtime-stream executions now close and settle explicitly after stream
consumption, preventing an idle monitor from surviving the caller loop.
EventCenter keeps its native background-task mechanism because replacing that
hot path did not justify its measured overhead.

## Sync/async compatibility

Agently's internal call-shape bridges use Agently-Stage `StageCallBridge`.
`FunctionShifter` remains importable as a deprecated compatibility facade and
delegates to the same bridge; new framework code should not use it as a task
lifecycle owner.

## Package version inspection

The package and the default `Agently` facade expose the same release version:

```python
import agently
from agently import Agently

assert agently.version == "4.1.4.5"
assert Agently.version == agently.version
```

## Performance characterization

The Stage-native TriggerFlow candidate was compared with the previous private
adapter in two reverse-order local runs, with 18 recorded samples per variant:

| Workload | Candidate delta |
|---|---:|
| Managed task create and settlement | +4.74% (about +0.61 µs/task) |
| Cancellation settlement | -3.08% |
| Finite TriggerFlow execution | -1.34% |
| TriggerFlow event fan-out | +0.29% |
| Peak traced task memory | 0.00% |

No run emitted a pending-task, unconsumed-exception, or lifecycle warning.
These numbers demonstrate bounded local overhead; they do not claim that Stage
makes provider-bound model requests faster.

## Core changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Direct long output | Added opt-in, append-only continuation after an observed length/incomplete terminal | Call `.ensure_long_output()` before starting a direct `AgentExecution` | Additive and disabled by default; not compatible with explicit AgentTask strategy | Deterministic protocol suite, real DeepSeek structured run, public example |
| TriggerFlow task lifecycle | A real Agently-Stage 0.3.5 instance directly owns execution-managed local tasks | Keep using existing TriggerFlow execution APIs; no Stage object is exposed publicly by Agently | Internal owner change with bounded measured overhead; EventCenter remains native | Stage ownership/settlement tests, full suite, performance A/B |
| Sync/async bridge | Internal bridges delegate to `StageCallBridge`; `FunctionShifter` remains a deprecated facade | New framework code should use Stage bridges directly | Existing imports remain available | FunctionShifter compatibility tests |
| Package metadata | Added `agently.version` and `Agently.version` | Read either attribute for the installed Agently release version | Additive | Source/package metadata consistency and installed-wheel smoke |

## Compatibility

- Python: `>=3.10`
- Agently-Stage: `>=0.3.5,<0.4.0`
- Recommended Agently DevTools: `>=0.1.10,<0.2.0`
- Skills authoring protocol: `agently-skills.authoring.v2`
- DevTools observation protocol: `agently-devtools.observation-runtime.v1`

The 4.1.4.4 ModelRequest, TriggerFlow snapshot, RecordStore retention, and
companion protocol contracts remain supported.
