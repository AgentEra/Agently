---
title: Settings
description: How Agently settings layer from global to agent to request, with env placeholders.
keywords: Agently, settings, set_settings, hierarchy, env, dotenv
---

# Settings

Agently settings are a hierarchical key-value store. Three scopes:

| Scope | Set with | Visible to |
|---|---|---|
| Global | `Agently.set_settings(...)` | every agent and request created after the call |
| Agent | `agent.set_settings(...)` | requests built from that agent |
| Request / runtime | `start(..., max_retries=...)` and similar method-level args | one call only |

Lower-scope keys override higher-scope keys; keys you don't override inherit through.

`agent.set_settings(...)` returns the same Agent, so inline overrides can stay
in a fluent Agent chain:

```python
agent = (
    Agently.create_agent()
    .set_settings("OpenAICompatible", {
        "model": "deepseek-v4-flash",
        "request_options": {"thinking": {"type": "disabled"}},
    })
    .set_settings("debug", True)
)
```

## Python 3.10 startup notice

On Python 3.10, Agently 4.1.4.11 displays one notice per process on first import. Set
`AGENTLY_PYTHON_310_NOTICE=0` in the environment before import to silence it.
See the [4.1.4.11 release notes](../development/release-notes-4.1.4.11.md) for examples.

## Setting paths

The first argument to `set_settings(...)` is a dotted path. Common paths:

| Path | Meaning |
|---|---|
| `OpenAICompatible` / `OpenAI` / `OAIClient` | shorthand aliases resolved to `plugins.ModelRequester.OpenAICompatible` |
| `OpenAIResponsesCompatible` / `OpenAIResponses` / `Responses` | shorthand aliases for the Responses API requester (`plugins.ModelRequester.OpenAIResponsesCompatible`) |
| `AnthropicCompatible` / `Anthropic` / `Claude` | shorthand aliases for the Claude requester (`plugins.ModelRequester.AnthropicCompatible`) |
| `plugins.ModelRequester.<Name>` | full path; same as the shorthand |
| `debug` | enable streaming console logs of the model request |
| `runtime.show_model_logs` | enable console logs for model requests and response parsing; `True` is equivalent to `"simple"` |
| `runtime.show_action_logs` | enable console logs for Action Runtime planning and execution; `True` is equivalent to `"simple"` |
| `runtime.show_tool_logs` | compatibility alias for `runtime.show_action_logs` in existing tool-loop examples |
| `runtime.show_trigger_flow_logs` | enable console logs for TriggerFlow execution / signal events; `True` is equivalent to `"simple"` |
| `runtime.show_runtime_logs` | enable console logs for request, session, chunk, `runtime.print`, and other generic observation events; `True` is equivalent to `"simple"` |
| `runtime.show_deprecation_warnings` | emit deprecated API warnings; defaults to `True`, set to `False` / `"off"` to silence deprecation warnings globally |
| `runtime.session_id` | bind a request to an explicit session id |

You can also pass a single dict and Agently merges by key:

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "model": "${ENV.OPENAI_MODEL}",
})
```

## Typed settings helpers

Dict settings remain the durable compatibility contract. For editor hints and
early validation, Agently also exposes typed helper classes under
`agently.types.settings`. The helper is converted back to the same dict
namespace before it enters the settings store:

```python
from agently import Agently
from agently.types.settings import OpenAICompatibleSettings

Agently.set_settings(
    OpenAICompatibleSettings(
        base_url="https://api.deepseek.com/v1",
        api_key="${ENV.DEEPSEEK_API_KEY}",
        model="deepseek-v4-flash",
        request_options={"thinking": {"type": "disabled"}},
    )
)
```

The old form stays valid and is the right choice for generated config files:

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "${ENV.DEEPSEEK_API_KEY}",
    "model": "deepseek-v4-flash",
    "request_options": {"thinking": {"type": "disabled"}},
})
```

## Reading settings back

```python
agent_settings = agent.settings.get("plugins.ModelRequester.OpenAICompatible", {})
print(agent_settings.get("model"))
```

`settings.get(path, default)` walks the dotted path; missing keys return the default.

## Env placeholders

Anywhere in a settings value, `${ENV.<NAME>}` is replaced with the matching environment variable when the settings are read. The pattern is parsed by [agently/utils/Settings.py](../../../agently/utils/Settings.py).

```python
Agently.set_settings("OpenAICompatible", {
    "api_key": "${ENV.OPENAI_API_KEY}",
})
```

## Loading from files

For non-trivial projects, keep settings in YAML / TOML / JSON instead of inline Python:

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

`auto_load_env=True` loads any `.env` in the working directory before resolving `${ENV.*}` placeholders.
Top-level aliases use the same mappings as `set_settings(...)`, so a file can use either `OpenAICompatible:` or the full `plugins.ModelRequester.OpenAICompatible:` path.

If you need a standalone `Settings` object, use `Settings().load(...)`:

```python
from agently.utils import Settings

settings = Settings()
settings.load("yaml_file", "settings.yaml", auto_load_env=True)
```

A typical layout for a project that uses files is in [Project Framework](project-framework.md).

## Debug toggle

```python
Agently.set_settings("debug", True)
```

`True` is exactly equivalent to `"simple"`: it prints a readable Prompt,
provider/model summary, model response stream, Action target and result preview,
and meaningful process or failure states without expanding provider request JSON.
For AgentTask runs, model-generated progress messages update one continuous block.
Direct responses use the order-authoritative normalized ModelRequest stream: all
characters appear before Done, and later AgentExecution projections neither
repeat nor reopen the stream after completion.
When ModelRequests overlap, the first response that emits a delta keeps the
foreground console stream. Later responses continue executing normally while
ConsoleSink shows one background notice and buffers only their presentation;
after the foreground terminal event, a still-running response loads its bounded
buffer and continues live, while an already completed response prints its final
materialized result. This FIFO display policy never serializes, throttles, or
otherwise changes ModelRequest or AgentExecution scheduling.
Simple mode always keeps at least one complete successful response projection.
A normal live stream is already complete and is not repeated. A response with
no rendered stream prints its complete materialized result. If a concurrent
background response exceeds the live replay buffer, ConsoleSink stops treating
that replay as complete and prints the authoritative result in full when
generation finishes; only diagnostics and previews remain length-bounded.
Once a response owns the foreground stream, ConsoleSink preserves reading focus:
it prints only the compact background-response notice between normal response
characters. Ordinary Prompt, provider request, process, and successful lifecycle
diagnostics are kept in a bounded console-only queue and printed afterward under
`[Deferred diagnostics]`, once every FIFO response display has finished. Warning,
failure, cancellation, blocked/unhealthy, interrupt, and approval-required events
remain immediate. EventCenter and DevTools still receive the original events at
their original time; only the human console presentation is deferred.
For an Action-or-Response loop, simple mode hides the normal internal planning
Prompt/decision stream and displays the accepted outer response once; planning
validation failures remain visible. Detail mode may show the internal decision
as diagnostic evidence.

`debug="detail"` is a high-information diagnostic view, not an "everything"
event dump. It additionally shows the full readable Prompt, sanitized provider
request JSON, attempt/validation/telemetry facts, Action arguments and results,
route/stage metadata, and the final materialized result. For a streaming request,
the heavier Prompt/request/process detail appears in the labeled deferred section
after the response display instead of interrupting it. A ModelRequest character
stream is displayed once; `runtime.progress.*` and AgentExecution mirrors do not
repeat it. Use an EventCenter hook or DevTools for complete event audit, storage,
or replay. Debug output also does not replace the complete user-facing process and
final answer. Consume the public delta stream as well:

```python
agent.set_settings("debug", "detail")
task = agent.create_task(goal="Prepare the report.", execution="flat")
await task.async_streaming_print()
result = await task.async_get_full_data()
```

This combination shows detailed diagnostics plus the readable task stages and
terminal result without mixing raw event JSON into public text delta.

Runtime logs can also be enabled per family:

```python
Agently.set_settings("runtime.show_model_logs", True)
Agently.set_settings("runtime.show_action_logs", True)
Agently.set_settings("runtime.show_trigger_flow_logs", True)
Agently.set_settings("runtime.show_runtime_logs", "detail")
```

Each switch accepts `False` / `"off"`, `True` / `"simple"`, or `"detail"`. `"simple"` is the readable execution summary; `"detail"` is a selected, deduplicated, and bounded deep diagnostic. Neither is a complete RuntimeEvent dump. Action loop events render as `ActionLoop`; concrete `action.*` events render with the action name and `action_type`. `runtime.show_tool_logs` remains accepted for existing code and enables the same Action Runtime log family when `runtime.show_action_logs` is not set. Start events render as `Started`, normal completion renders as `Completed`, and only failure events or explicit failure payloads render as `Failed`.

For ModelRequest output validation, `"simple"` shows the validator, failure
reason, attempt summary, and retry transition. `"detail"` may additionally
show a bounded validation context or validator traceback, but it does not
repeat the model response or validation reason in the adjacent retry entry.
The complete structured facts remain available on the corresponding
RuntimeEvent and through DevTools observation.

Production deployments that intentionally keep legacy compatibility calls can silence deprecation warnings globally:

```python
Agently.set_settings("runtime.show_deprecation_warnings", False)
```

This only affects Agently deprecation warnings. Operational runtime warnings, errors, and risky-scope warnings such as `flow_data` remain controlled by their own APIs and settings.

## See also

- [Model Setup](model-setup.md) — provider-specific recipes
- [Project Framework](project-framework.md) — file-based settings layout
