---
title: Models Overview
description: How Agently organizes model providers behind three protocol-level requesters.
keywords: Agently, models, OpenAICompatible, AnthropicCompatible, providers
---

# Models Overview

Agently has three protocol-level request plugins, plus per-provider configuration recipes that select one of them.

## Layered view

```text
Application code
      │
      ▼
  ModelRequest  ──►  ModelRequestResult
      │
      ▼
ModelRequester plugin (the "protocol layer")
   ├── OpenAICompatible             ◄── most providers (Chat Completions)
   ├── OpenAIResponsesCompatible    ◄── Responses API variants
   └── AnthropicCompatible          ◄── Claude
      │
      ▼
HTTP to a model endpoint
```

The protocol plugin is what builds the HTTP request body and parses the wire response. Provider configuration is just a settings preset that targets one of these plugins.

## Why three plugins, not one

Earlier versions of the docs implied "every provider goes through `OpenAICompatible`". That is no longer accurate. `OpenAICompatible`, `OpenAIResponsesCompatible`, and `AnthropicCompatible` are separate requester plugins. Each one directly implements the `ModelRequester` protocol and owns its own protocol mapping. Anthropic in particular builds its own request bodies — `anthropic_version`, `anthropic_beta`, an explicit `max_tokens` requirement, and the `messages`/`system` field shape Claude expects. Those differences are real enough that lumping Claude under "OpenAICompatible" produces wrong configurations.

For custom requester handlers, `build_request_handlers()` returns
`AttemptHandlers`; annotate the handler stream with `AttemptStreamMessage` /
`AttemptStreamGenerator` from `agently.types.data`. `broadcast_response(...)`
then maps that attempt/provider stream into the public `AgentlyResultGenerator`.
It must pass core-owned `("status", payload)` attempt records unchanged rather
than treating them as provider wire payloads.

If you are pointing at `https://api.anthropic.com` (or a Claude-compatible proxy that speaks the same protocol), use [AnthropicCompatible](anthropic-compatible.md). For everything else (OpenAI, DeepSeek, Qwen, Ollama, Kimi, GLM, MiniMax, Doubao, SiliconFlow, Groq, ERNIE, Gemini's OpenAI-compat endpoint, plus any private gateway speaking the OpenAI Chat Completions API), use [OpenAICompatible](openai-compatible.md).

## Picking a plugin

| You're calling | Use plugin |
|---|---|
| OpenAI, Azure OpenAI, Gemini-via-OpenAI | `OpenAICompatible` |
| DeepSeek, Qwen, Kimi, GLM, MiniMax, Doubao, SiliconFlow, Groq, ERNIE | `OpenAICompatible` |
| Ollama or any other OpenAI-compatible local server | `OpenAICompatible` |
| Anthropic / Claude (native API) | `AnthropicCompatible` |
| A private gateway speaking the OpenAI Chat Completions API | `OpenAICompatible` |
| A private gateway speaking the OpenAI Responses API | `OpenAIResponsesCompatible` |
| A private gateway speaking the Anthropic Messages API | `AnthropicCompatible` |

## Minimal configuration

```python
from agently import Agently

# OpenAI-compatible
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})

# Or Anthropic
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
})
```

Per-provider recipes (env vars, common model names, base URLs) live in [Providers](providers/).

## Switching Models With Model Pool

For applications that use more than one model, configure model aliases with
`model_pool`, then switch the active Agent model with `activate_model(...)`.
The alias can be concrete and operational, such as `ollama-qwen2.5` or
`deepseek-v4`.

```python
agent.set_settings("model_pool", {
    "ollama-qwen2.5": "qwen2.5:7b",
    "deepseek-v4": "deepseek-v4-flash",
})
agent.set_settings("key_pool", {
    "local": "ollama",
    "deepseek-main": "${ENV.DEEPSEEK_API_KEY}",
    "deepseek-backup": "${ENV.DEEPSEEK_BACKUP_API_KEY}",
})
agent.set_settings("key_pool_strategy", {
    "qwen2.5:7b": {"mode": "fixed", "pool": ["local"]},
    "deepseek-v4-flash": {"mode": "round_robin", "pool": ["deepseek-main", "deepseek-backup"]},
})

result = (
    agent
    .activate_model("ollama-qwen2.5")
    .input("Summarize this incident.")
    .output({"summary": (str, "incident summary", True)})
    .start()
)
```

`activate_model(...)` affects subsequent Agent-owned requests, including
chain-style `agent.input(...).start()` and `agent.create_execution()`.
For a one-off override, use `agent.create_request(model_key="deepseek-v4")`.

API keys are selected at request time by the key-pool `selection` policy:
`fixed`, `random`, `round_robin`, or `least_used`. The legacy
`key_pool_strategy` path remains accepted.

Provider-error failover is opt-in through `api_key_pools.<pool>.failover`.
Without a failover policy, provider errors are surfaced as before. Built-in
failover policies can retry another key for configured HTTP status codes, and
custom handlers can inspect the provider error object and return `"try_next"`,
`"retry_same"`, `"raise"`, a key id, a key entry dict, or a wrapper such as
`{"key_id": "b"}` / `{"key_entry": context.keys[1]}`.

For `OpenAICompatible`, `OpenAIResponsesCompatible`, and
`AnthropicCompatible`, framework-created HTTP error messages include the
provider status and response detail but do not append the serialized model
request. Core `model.requester.error` RuntimeEvents retain structured
`payload.request_data` for protected cold diagnostics, while the request stream
propagates the provider error once through its normal terminal path.

The three compatible requesters also share one public transport-retry
lifecycle. `request_retry=False` or `max_attempts=1` means one physical SSE
connection; enabled replays receive public, incrementing attempt indexes and
retry boundaries. No requester performs a hidden inner SSE reconnect.

## Where the plugin code lives

- [agently/builtins/plugins/ModelRequester/OpenAICompatible/](../../../agently/builtins/plugins/ModelRequester/OpenAICompatible/)
- [agently/builtins/plugins/ModelRequester/OpenAIResponsesCompatible/](../../../agently/builtins/plugins/ModelRequester/OpenAIResponsesCompatible/)
- [agently/builtins/plugins/ModelRequester/AnthropicCompatible/](../../../agently/builtins/plugins/ModelRequester/AnthropicCompatible/)

Each built-in requester uses the runtime-handler package layout: `plugin.py` is
the public coordinator, and private implementation roles live in
`modules/request_builder.py`, `modules/credential.py`,
`modules/transport.py`, `modules/handlers.py`, and
`modules/response_adapter.py`.

If a provider is missing or speaks an incompatible protocol, you can add a new requester plugin — but in practice almost every commercial endpoint either ships an OpenAI-compatible mode, a Responses-style mode, or matches Anthropic's protocol, so these built-ins cover most cases.

## See also

- [OpenAICompatible details](openai-compatible.md)
- [AnthropicCompatible details](anthropic-compatible.md)
- [Providers](providers/) — per-provider recipes
- [Model Setup](../start/model-setup.md) — quickstart-level setup
- [Settings](../start/settings.md) — env placeholders and hierarchy
