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

Prints concise model request and result logs to the console. For AgentTask runs,
`"simple"` also prints process event summaries such as phases, progress, and
snapshots without token-level deltas. Use `debug="detail"` when you need the
full diagnostic RuntimeEvent stream, including model delta output. Debug output
does not automatically print the user-facing process and final answer. Consume
the public delta stream as well:

```python
agent.set_settings("debug", "detail")
task = agent.create_task(goal="Prepare the report.", execution="flat")
await task.async_streaming_print()
result = await task.async_get_full_data()
```

This combination shows complete diagnostics plus the readable task stages and
terminal result without mixing raw event JSON into public text delta.

Runtime logs can also be enabled per family:

```python
Agently.set_settings("runtime.show_model_logs", True)
Agently.set_settings("runtime.show_action_logs", True)
Agently.set_settings("runtime.show_trigger_flow_logs", True)
Agently.set_settings("runtime.show_runtime_logs", "detail")
```

Each switch accepts `False` / `"off"`, `True` / `"simple"`, or `"detail"`. `"simple"` prints request/result summaries, AgentTask process summaries, and warning/error/critical events; `"detail"` prints the full observation event stream for that family, including model delta output. Action loop events render as `ActionLoop`; concrete `action.*` events render with the action name and `action_type`. `runtime.show_tool_logs` remains accepted for existing code and enables the same Action Runtime log family when `runtime.show_action_logs` is not set. Start events render as `Started`, normal completion renders as `Completed`, and only failure events or explicit failure payloads render as `Failed`.

Production deployments that intentionally keep legacy compatibility calls can silence deprecation warnings globally:

```python
Agently.set_settings("runtime.show_deprecation_warnings", False)
```

This only affects Agently deprecation warnings. Operational runtime warnings, errors, and risky-scope warnings such as `flow_data` remain controlled by their own APIs and settings.

## See also

- [Model Setup](model-setup.md) — provider-specific recipes
- [Project Framework](project-framework.md) — file-based settings layout
