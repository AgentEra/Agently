---
title: Agently 4.1.4.7 Development Notes
description: Automatic mixed sync/async Stage routing for TriggerFlow integrations.
keywords: Agently, 4.1.4.7, Agently-Stage, TriggerFlow, sync, async, carrier
---

# Agently 4.1.4.7 Development Notes

The Agently 4.1.4.7 development line updates the minimum Stage dependency to
`agently-stage >=0.3.6,<0.4.0`. It is preparing the Agently integration for the
runtime bug exposed by [Agently #347](https://github.com/AgentEra/Agently/issues/347),
[Agently-Stage #24](https://github.com/AgentEra/Agently-Stage/issues/24), and
[Agently-Stage #25](https://github.com/AgentEra/Agently-Stage/issues/25).

The correction belongs primarily to Stage: synchronous and asynchronous scopes
now distinguish inherited logical execution lineage from the physical thread
and event loop that a call can safely block. The 4.1.4.7 development line adds
an end-to-end TriggerFlow regression contract and will ensure later Agently
installations receive that correction after the integration release.

## Developer-visible changes

| Area | Behavior in 4.1.4.7 | Compatibility |
|---|---|---|
| Sync TriggerFlow chunks | A provider-owned sync wrapper may use `with Stage()` for an async SDK and then call `data.set_state(...)`, `append_state(...)`, or `del_state(...)` | No provider API rewrite and no knowledge of TriggerFlow's private Stage use is required |
| Stage dependency | Minimum version is `0.3.6` in the existing `<0.4.0` compatibility line | Updating Stage alone fixes the existing script; updating Agently fixes dependency resolution for later installs |
| `FunctionShifter.syncify/asyncify` | Deprecated names and warnings remain; scalar calls delegate to `Stage.as_sync/as_async` | Existing imports and call forms remain valid |
| Internal bridges | `default_stage_call_bridge` remains the owner where Agently requires lightweight bridging, injected lifecycle, streams, or explicit `managed=True` settlement | No blanket bridge replacement or semantic change |
| Workflow ownership | TriggerFlow still owns workflow state, lifecycle, persistence, concurrency, and errors | Stage carrier details remain private and are not serialized |

## Provider-owned synchronous interface

Tool and Function providers may keep a deliberate synchronous method even when
the underlying implementation is async:

```python
from agently_stage import Stage


def search(query: str):
    with Stage() as stage:
        return stage.get(search_tool.search, query)


def chunk(data):
    result = search(data.input)
    data.set_state("search_result", result, emit=False)
    return result
```

Stage automatically chooses a physically safe carrier. The provider is not
responsible for discovering whether TriggerFlow already uses Stage internally.

This remains a synchronous boundary and blocks its calling worker. Native async
chunks should still use `await` and `data.async_set_state(...)`. Work that owns
an object bound to the caller's event loop must stay on that owner loop and be
awaited there.

## Stage surface

Agently does not re-export Stage. Applications that directly need Stage's
scoped adapters import it from `agently_stage`:

```python
from agently_stage import Stage

sync_search = Stage.as_sync(search_tool.search)
async_transform = Stage.as_async(transform)
```

Each adapter invocation owns one automatic Stage scope and waits for the result
and Stage-owned settlement. `StageCallBridge` remains the advanced API for
stream conversion, injected Stage/executor ownership, independent close, and
lightweight adaptation.

## Upgrade

No application source migration is required for the reported call chain.
While Agently 4.1.4.7 remains under development, an existing Agently
installation that permits the Stage 0.3 compatibility line can upgrade only
Stage to 0.3.6. Install Agently 4.1.4.7 normally after that integration version
is released.
