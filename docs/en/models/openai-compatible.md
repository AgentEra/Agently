---
title: OpenAICompatible
description: The protocol-level plugin used for OpenAI and every provider that speaks the same API.
keywords: Agently, OpenAICompatible, OpenAI, DeepSeek, Qwen, Ollama, model
---

# OpenAICompatible

> Languages: **English** · [中文](../../cn/models/openai-compatible.md)

`OpenAICompatible` is one of the three protocol-level model request plugins (see [Models Overview](overview.md)). It handles any endpoint that speaks the OpenAI Chat Completions API — which today covers most commercial providers and most local model servers.

## Settings

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

| Key | Meaning |
|---|---|
| `base_url` | the API root, e.g. `https://api.openai.com/v1` |
| `api_key` | bearer token; omit for local servers that don't require auth |
| `model` | provider-specific model name |
| `model_type` | `"chat"` (default) or `"completion"` for legacy completion endpoints |
| `request_retry` | transient transport retry policy; defaults to `{"max_attempts": 2, "after_output": true}` |
| `request_options` | extra dict forwarded to the underlying HTTP client (timeouts, headers) |

The full set lives in the [agently/builtins/plugins/ModelRequester/OpenAICompatible/](../../../agently/builtins/plugins/ModelRequester/OpenAICompatible/) package. The public plugin class is exported from `plugin.py`, while request building, credentials, transport, handler binding, and response mapping live under its private `modules/` package.

## Responses API variant

Some providers (and OpenAI itself for newer models) speak the Responses API rather than Chat Completions. Agently has a sibling plugin:

```python
Agently.set_settings("OpenAIResponsesCompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_RESPONSES_MODEL}",
})
```

`OpenAIResponsesCompatible` is a sibling of `OpenAICompatible`; pick whichever matches the protocol your endpoint exposes. Both plugins directly implement `ModelRequester`; neither plugin inherits from the other.

Responses reasoning options pass through `request_options`, for example:

```python
Agently.set_settings("OpenAIResponsesCompatible.request_options", {
    "reasoning": {"effort": "low", "summary": "auto"},
})
```

When the endpoint returns reasoning summaries, the plugin maps
`response.reasoning_summary_text.delta` to Agently `reasoning_delta` events
and the final `reasoning` output item's `summary_text` parts to
`reasoning_done`. The complete Responses object remains available through
`original_done`. Terminal `response.completed` and `response.incomplete`
events close the provider stream while allowing Agently's final lifecycle
status to propagate.

DeepSeek currently exposes Chat Completions and Anthropic Messages
compatibility, not a Responses endpoint. Use one of those two requesters for
DeepSeek; this Responses mapping applies to OpenAI and compatible gateways
that actually expose `/responses`.

## What "OpenAI-compatible" actually covers

A provider qualifies as OpenAI-compatible when its endpoint:

- Accepts a JSON body with `messages: [{"role": ..., "content": ...}, ...]`.
- Returns either a JSON response or an SSE stream of token deltas.
- Uses standard fields like `model`, `temperature`, `max_tokens`, `tools`, etc.

Providers that fit:

- OpenAI / Azure OpenAI
- DeepSeek (`https://api.deepseek.com/v1`)
- Qwen / DashScope's compatibility mode (`https://dashscope.aliyuncs.com/compatible-mode/v1`)
- Kimi / Moonshot (`https://api.moonshot.cn/v1`)
- GLM (`https://open.bigmodel.cn/api/paas/v4/`)
- MiniMax, Doubao, ERNIE — most ship an OpenAI-compatible mode
- SiliconFlow, Groq — both expose OpenAI-compatible endpoints
- Gemini — via OpenAI-compat endpoint
- Ollama (local) — `http://127.0.0.1:11434/v1`
- vLLM, LM Studio, llama.cpp server (local)
- Most internal gateways teams build over commercial models

For per-provider recipes, see [Providers](providers/).

## Per-agent overrides

Agent-level settings override the global preset:

```python
agent = Agently.create_agent()
agent.set_settings("OpenAICompatible", {"model": "${ENV.OPENAI_MODEL_FAST}"})
```

You can also set request-level overrides via the request chain — see [Settings](../start/settings.md).

## Streaming and tools

`OpenAICompatible` handles both streaming responses (used by `get_generator(...)` / `get_async_generator(...)`) and tool calling (used by the action runtime). You don't need to enable these per-provider — they're on as the protocol allows.

If a particular provider doesn't fully implement OpenAI semantics for one of these (e.g., a quirky streaming format), the underlying plugin tries to be tolerant; report concrete cases via issues.

For transient transport failures such as a connection reset or provider-side
disconnect, `OpenAICompatible` retries the same request once by default. This
does not change the selected model, prompt, or structured output format. The
default also covers failures after partial output has already streamed, so
stream consumers must process the reserved `$status` record, handle the
plain-delta `"<$retry>{reason}</$retry>"` replay marker as a public text
delimiter, or read only a final result. Retry control metadata comes from
`$status`, not from the plain-delta marker.
Set `"request_retry": {"max_attempts": 1}` or `"request_retry": False` to
disable replay, including physical SSE reconnection. Every physical connection
is owned by the same public attempt lifecycle; the transport does not perform
an unreported inner reconnect. Set `request_retry.after_output=False` only when
a consumer cannot reset provisional output after a retry boundary:

```python
agent.set_settings("OpenAICompatible.request_retry", {
    "max_attempts": 2,
    "after_output": False,
})
```

Before a replacement attempt, Agently emits a `("status", payload)` stream
event with `payload["status"] == "failed"` and `payload["retry"] is True`.
The payload includes the failed `attempt_index`, `next_attempt_index`, the
provider error `reason`, and `error_type`. `instant` / `streaming_parse`
consumers receive the same record at `$status`; they must clear provisional
output for the failed attempt. Plain `delta` generators receive the standalone
`"<$retry>{reason}</$retry>"` marker at the same boundary and must clear their
local delta buffer before accepting replacement text.

## See also

- [Models Overview](overview.md) — protocol picking
- [AnthropicCompatible](anthropic-compatible.md) — the other protocol plugin
- [Providers](providers/) — recipes per provider
- [Model Setup](../start/model-setup.md) — quickstart-level walkthrough
