---
title: Agently 4.1.4.6 Release Notes
description: Standard package version inspection, unified reasoning lifecycle events, and live TriggerFlow sub-flow resources.
keywords: Agently, 4.1.4.6, version, reasoning, thinking, DeepSeek, Anthropic, Responses, TriggerFlow
---

# Agently 4.1.4.6 Release Notes

Agently 4.1.4.6 is a focused compatibility patch over 4.1.4.5. It:

- exposes the package version through the standard `__version__` spelling;
- normalizes generated reasoning across OpenAI-compatible Chat Completions,
  Anthropic-compatible Messages, and Responses adapters;
- makes the public compatible-request retry policy the sole owner of physical
  SSE reconnection across Chat Completions, Anthropic Messages, and Responses;
- keeps live runtime resources live when they cross a TriggerFlow sub-flow
  boundary.

The sandbox-provider drafts tracked in
[#342](https://github.com/AgentEra/Agently/issues/342) are not part of this
release.

## Core changes

| Area | What changed | Recommended usage | Compatibility / risk | Evidence |
|---|---|---|---|---|
| Package version | Added the standard `agently.__version__` and `Agently.__version__` surfaces; removed the mistaken 4.1.4.5 `version` attributes | Inspect either `__version__` surface | Intentional breaking correction only for code that adopted the mistaken 4.1.4.5 attributes | Package/version tests, built wheel and clean-install smoke |
| Compatible model reasoning | Chat Completions, Anthropic Messages, and Responses now converge generated reasoning into `reasoning_delta`, `reasoning_done`, and provider-native `original_done` | Keep provider controls in `request_options`; consume the public reasoning lifecycle | Additive response normalization; provider request options remain unchanged | Adapter suites plus real `deepseek-v4-flash` Chat Completions and Anthropic Messages probes |
| Compatible SSE retry | Removed hidden transport reconnects and made `request_retry` / `AttemptRunner` the sole physical-request retry owner across all three adapters | Use `request_retry=False` or `max_attempts=1` for one physical connection; use `after_output=False` when provisional output cannot be reset | Default bounded replay remains; newly observable boundaries prevent uncounted requests and mixed output | Physical-connection, retry-boundary, attempt-index, `[DONE]`, and EDA TransportGuard regression tests |
| TriggerFlow sub-flow resources | Resource capture and isolated child templates preserve live runtime-resource object identity | Pass clients, locks, callbacks, and services through `resources`; re-inject them after restoring a saved root execution | Resource capture is shared by identity; ordinary input/value capture remains copy-isolated | TriggerFlow resource-identity tests and sub-flow foundation example |
| Sandbox provider contributions | No sandbox provider draft is merged in 4.1.4.6 | Coordinate and consolidate the contributor work through issue #342 | Explicitly deferred; no runtime or dependency change in this release | Issue #342 and unchanged optional-provider surface |

## Standard package version

Use either public version surface:

```python
import agently
from agently import Agently

assert agently.__version__ == "4.1.4.6"
assert Agently.__version__ == agently.__version__
```

The mistakenly introduced `agently.version` and `Agently.version` attributes
from 4.1.4.5 are removed rather than retained as aliases.

## Unified reasoning lifecycle

Provider-specific request options remain under `request_options` and pass
through unchanged. For example, an OpenAI-compatible endpoint can receive:

```yaml
plugins:
  ModelRequester:
    OpenAICompatible:
      model: deepseek-v4-flash
      request_options:
        thinking:
          type: enabled
```

Generated reasoning is projected through the same public response lifecycle:

- `reasoning_delta` carries streamed reasoning increments;
- `reasoning_done` carries the reconciled reasoning value exactly once;
- `original_done` retains the provider-native completed response.

OpenAI-compatible Chat Completions now retains non-stream and streamed
`reasoning_content`. Anthropic-compatible responses reconcile `thinking`
blocks, signatures, tool-use continuations, and an empty or populated terminal
reasoning event. Responses adapters map reasoning-summary delta/done events and
completed-output reasoning items without exposing request configuration such as
`reasoning.effort` or `reasoning.summary` as generated content.

This is adapter-level normalization, not a DeepSeek-only branch. Providers that
use the corresponding compatible protocol inherit the same lifecycle behavior.

## One observable SSE retry lifecycle

`request_retry` now owns every physical streaming request across
`OpenAICompatible`, `AnthropicCompatible`, and `OpenAIResponsesCompatible`.
The transports no longer perform an additional stamina reconnect inside one
public attempt. Therefore `request_retry=False` and `max_attempts=1` both make
at most one physical SSE connection; `after_output=False` also prevents replay
after partial output. When replay is enabled, each replacement connection has
the public, incrementing `attempt_index` and retry boundary.

The removed inner reconnect also removes its synchronous SSE retry-delay sleep
from the async event loop. `[DONE]` remains the logical terminal marker; a
disconnect before `[DONE]` remains a transport failure governed by the public
policy.

## TriggerFlow sub-flow runtime resources

Direct `resources -> resources` capture forwards the live object by identity:

```python
parent_flow.to_sub_flow(
    child_flow,
    capture={"resources": {"service": "resources.service"}},
)

execution = parent_flow.create_execution(
    auto_close=False,
    runtime_resources={"service": live_service},
)
```

An isolated child-flow template also inherits its flow-level runtime resources
by identity. Clients, callbacks, locks, events, and other live handles are not
deep-copied. Ordinary input/value capture remains isolated by copy, so this fix
does not turn general sub-flow data into shared mutable state.

Saved executions still do not serialize live resources. The host must re-inject
required resources when restoring a root execution.

## Compatibility

- Python: `>=3.10`
- Agently-Stage: `>=0.3.5,<0.4.0`
- Recommended Agently DevTools: `>=0.1.10,<0.2.0`
- Skills authoring protocol: `agently-skills.authoring.v2`
- DevTools observation protocol: `agently-devtools.observation-runtime.v1`

The opt-in long-output continuation and Stage-backed TriggerFlow task ownership
introduced in 4.1.4.5 remain unchanged.
