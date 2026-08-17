---
title: Model Setup
description: Configure model providers, env-driven settings, and per-agent overrides.
keywords: Agently, model setup, OpenAICompatible, AnthropicCompatible, env, settings
---

# Model Setup

Agently has three protocol-level request plugins shipped in `agently.builtins.plugins.ModelRequester`:

- `OpenAICompatible` — for endpoints that speak the OpenAI Chat Completions API. Covers OpenAI, DeepSeek, Qwen, Ollama, Kimi, GLM, MiniMax, Doubao, SiliconFlow, Groq, ERNIE, and Gemini-via-OpenAI.
- `OpenAIResponsesCompatible` — for endpoints that use the OpenAI Responses API shape.
- `AnthropicCompatible` — for Anthropic's native API (Claude). It is a separate requester plugin with Anthropic-specific request bodies (`anthropic_version`, `anthropic_beta`, `max_tokens`).

You pick which plugin's settings to populate by name. The plugin registry resolves the active requester from those settings.

## Global vs agent-level settings

Settings are hierarchical. `Agently.set_settings(...)` sets the global default; `agent.set_settings(...)` overrides for one agent. Keys you don't set on the agent fall through to global.

```python
from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
    },
)

agent = Agently.create_agent()

# Override the model for this agent only
agent.set_settings(
    "OpenAICompatible",
    {"model": "qwen2.5:7b"},
)
```

See [Settings](settings.md) for the full layering rules.

## Common provider recipes

### OpenAI

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

### Claude / Anthropic

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
})
```

### DeepSeek

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "${ENV.DEEPSEEK_API_KEY}",
    "model": "${ENV.DEEPSEEK_MODEL}",
})
```

### Qwen / DashScope

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "${ENV.DASHSCOPE_API_KEY}",
    "model": "${ENV.DASHSCOPE_MODEL}",
})
```

### Ollama (local)

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen2.5:7b",
})
```

`api_key` can be omitted when the local server doesn't require one.

For more recipes (Kimi, GLM, MiniMax, Doubao, SiliconFlow, Groq, ERNIE, Gemini), see [Models](../models/overview.md) and [Providers](../models/providers/).

## Environment variables and dotenv

Use `${ENV.<NAME>}` placeholders anywhere in settings to pick up environment variables at resolution time. The placeholder is parsed by `agently/utils/Settings.py`.

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "${ENV.OPENAI_BASE_URL}",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

To auto-load `.env` files at startup, pass `auto_load_env=True` when loading from a settings file:

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

## Typed provider settings

Provider settings can be supplied as dicts or typed helper classes. The typed
class only improves construction and validation; internally Agently stores the
same dict under the provider namespace.

```python
from agently import Agently
from agently.types.settings import OpenAICompatibleSettings

Agently.set_settings(
    OpenAICompatibleSettings(
        base_url="https://api.deepseek.com/v1",
        api_key="${ENV.DEEPSEEK_API_KEY}",
        model="deepseek-v4-flash",
        request_options={
            "thinking": {"type": "disabled"},
            "temperature": 0,
        },
    )
)
```

DeepSeek V4 Flash enables thinking by default. Disable it when the application
relies on sampling options such as `temperature`; DeepSeek ignores those
options while thinking is enabled. To request reasoning explicitly, use:

```python
Agently.set_settings("OpenAICompatible.request_options", {
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high",
})
```

The generic OpenAI-compatible adapter emits reasoning through
`reasoning_delta` / `reasoning_done` specific events and preserves
`reasoning_content` in `original_done`.

DeepSeek V4 also exposes an Anthropic Messages endpoint. Activate the separate
Anthropic-compatible plugin when that protocol is required:

```python
Agently.set_settings("plugins.ModelRequester.activate", "AnthropicCompatible")
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.deepseek.com/anthropic",
    "model": "deepseek-v4-flash",
    "auth": {"api_key": "${ENV.DEEPSEEK_API_KEY}"},
    "request_options": {
        "thinking": {"type": "enabled"},
        "output_config": {"effort": "high"},
    },
})
```

This path emits Anthropic `thinking` blocks as the same Agently
`reasoning_delta` / `reasoning_done` events and preserves the original block
and signature for tool-call continuation. With DeepSeek thinking enabled, use
automatic tool selection; the current endpoint rejects a forced named
`tool_choice`.

Built-in provider helpers live in `agently.types.settings`. Third-party plugins
can expose their own settings classes from their plugin package and register
them through the plugin's `SETTINGS_SCHEMAS`.

## Model profiles and key pools

For applications that route by business scenario, use the layered settings
shape:

- `model_pool`: maps a business key to a model profile id.
- `model_profiles`: stores provider, model, endpoint, request options, and the
  key pool to use.
- `api_key_pools`: stores credential pools and their selection strategy.

```python
Agently.set_settings("model_pool", {
    "support-chat": "deepseek-v4-flash-prod",
    "reasoning": "deepseek-reason-prod",
})

Agently.set_settings("model_profiles", {
    "deepseek-v4-flash-prod": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "api_key_pool": "deepseek-prod",
        "request_options": {"thinking": {"type": "disabled"}},
    },
    "deepseek-reason-prod": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "api_key_pool": "deepseek-prod",
        "request_options": {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
    },
})

Agently.set_settings("api_key_pools", {
    "deepseek-prod": {
        "selection": {"strategy": "round_robin"},
        "failover": {
            "strategy": "try_next",
            "max_attempts": 2,
            "retry_status_codes": [401, 403, 429],
        },
        "keys": [
            {"id": "a", "value": "${ENV.DEEPSEEK_API_KEY_A}"},
            {"id": "b", "value": "${ENV.DEEPSEEK_API_KEY_B}"},
        ],
    }
})

agent = Agently.create_agent()
agent.activate_model("reasoning")
```

`selection` controls which key is used for a new independent request. It
supports `fixed`, `random`, `round_robin`, and `least_used`; the legacy top-level
`strategy` / `mode` fields remain selection shortcuts.

`failover` controls what happens after the provider request fails with a
credential or provider-side error. Without a `failover` policy, Agently does not
try another credential. `OpenAICompatible` still has a narrow transport replay:
transient disconnects are retried with the same model, prompt, and output format
according to `OpenAICompatible.request_retry` (default
`{"max_attempts": 2, "after_output": true}`). Replay after partial output emits
`$status` and the plain-delta `"<$retry>{reason}</$retry>"` marker, so consumers
must reset provisional output before accepting replacement deltas. The built-in
`try_next` policy retries the next key only for configured HTTP status codes. By
default, use credential or quota-oriented codes such as `401`, `403`, and
`429`. Status codes such as `405` and `422` often mean endpoint, method,
payload, or model-capability mismatch; add them only when your provider uses
them for key or quota failures.

Both layers can use direct Python handlers for application-specific behavior:

```python
def select_key(context):
    return context.keys[0]["id"]


def failover(error, context):
    if context.status_code == 429:
        return "try_next"
    if context.status_code in {405, 422}:
        return "raise"
    return "raise"


Agently.set_settings("api_key_pools", {
    "deepseek-prod": {
        "selection": select_key,
        "failover": {"handler": failover, "max_attempts": 2},
        "keys": [
            {"id": "a", "value": "${ENV.DEEPSEEK_API_KEY_A}"},
            {"id": "b", "value": "${ENV.DEEPSEEK_API_KEY_B}"},
        ],
    }
})
```

Failover handlers can return `"try_next"` / `"retry_next"`, `"retry_same"`,
`"raise"`, a key id, a key entry dict, or an explicit wrapper such as
`{"key_id": "b"}` / `{"key_entry": context.keys[1]}`. Failover retry budget is
separate from output parsing and validation `max_retries`.

The legacy `model_pool -> key_pool_strategy -> key_pool` form remains accepted.

## Verify connectivity

```python
result = (
    Agently.create_agent()
    .input("Say hello in one short sentence.")
    .start()
)
print(result)
```

If this returns text, model setup is working. If you get an auth or connection error, the failure is in `base_url` / `api_key` / network reachability, not in your prompt or output schema.

## See also

- [Models Overview](../models/overview.md) — protocol layer details
- [Settings](settings.md) — hierarchy and env handling
- [Project Framework](project-framework.md) — putting model config into files instead of code
