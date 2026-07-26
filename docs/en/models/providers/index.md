---
title: Provider Recipes
description: Configuration recipes per model provider — base URLs, env vars, and model-name placeholders.
keywords: Agently, providers, OpenAI, DeepSeek, Qwen, Claude, Ollama, recipes
---

# Provider Recipes

> Languages: **English** · [中文](../../../cn/models/providers/index.md)

Each entry below is a configuration block. Drop it into your `Agently.set_settings(...)` call and fill the current model name from the provider's official documentation. All examples use `${ENV.*}` placeholders so you can keep secrets out of code.

For the protocol-level details (which plugin, what fields), see [Models Overview](../overview.md).

## OpenAI

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

For models on the Responses API:

```python
Agently.set_settings("OpenAIResponsesCompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_RESPONSES_MODEL}",
})
```

## Anthropic / Claude

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
})
```

See [AnthropicCompatible](../anthropic-compatible.md) for `anthropic_beta` and other Anthropic-specific keys.

## DeepSeek

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "${ENV.DEEPSEEK_API_KEY}",
    "model": "${ENV.DEEPSEEK_MODEL}",
})
```

## Qwen / DashScope

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "${ENV.DASHSCOPE_API_KEY}",
    "model": "${ENV.DASHSCOPE_MODEL}",
})
```

## Kimi / Moonshot

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.moonshot.cn/v1",
    "api_key": "${ENV.MOONSHOT_API_KEY}",
    "model": "${ENV.MOONSHOT_MODEL}",
})
```

## GLM / Zhipu

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://open.bigmodel.cn/api/paas/v4/",
    "api_key": "${ENV.GLM_API_KEY}",
    "model": "${ENV.GLM_MODEL}",
})
```

## MiniMax

MiniMax provides OpenAI-compatible and Anthropic-compatible endpoints in both global and China regions.

OpenAI-compatible global endpoint:

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.minimax.io/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

OpenAI-compatible China endpoint:

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.minimaxi.com/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

Anthropic-compatible global endpoint:

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.minimax.io/anthropic/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

Anthropic-compatible China endpoint:

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.minimaxi.com/anthropic/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

## Doubao

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "${ENV.DOUBAO_API_KEY}",
    "model": "${ENV.DOUBAO_MODEL}",
})
```

## ERNIE

ERNIE supports OpenAI-compatible mode through Qianfan:

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://qianfan.baidubce.com/v2",
    "api_key": "${ENV.QIANFAN_API_KEY}",
    "model": "${ENV.QIANFAN_MODEL}",
})
```

## SiliconFlow

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.siliconflow.cn/v1",
    "api_key": "${ENV.SILICONFLOW_API_KEY}",
    "model": "${ENV.SILICONFLOW_MODEL}",
})
```

## Groq

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.groq.com/openai/v1",
    "api_key": "${ENV.GROQ_API_KEY}",
    "model": "${ENV.GROQ_MODEL}",
})
```

## Gemini (via OpenAI-compatible endpoint)

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "api_key": "${ENV.GEMINI_API_KEY}",
    "model": "${ENV.GEMINI_MODEL}",
})
```

## Ollama (local)

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen2.5:7b",   # or whatever model you've pulled
})
```

`api_key` can be omitted when the local server doesn't require auth.

## vLLM / LM Studio / llama.cpp server (local)

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://localhost:8000/v1",   # whatever your server exposes
    "model": "your-served-model-name",
})
```

## Custom internal gateway

If your team runs a private gateway that speaks the OpenAI Chat Completions or Anthropic Messages API, use the matching plugin and point `base_url` at the gateway:

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://gw.internal/openai-compat/v1",
    "api_key": "${ENV.INTERNAL_GATEWAY_TOKEN}",
    "model": "internal-default",
})
```

## See also

- [Models Overview](../overview.md) — picking the right protocol plugin
- [OpenAICompatible](../openai-compatible.md) and [AnthropicCompatible](../anthropic-compatible.md) — full settings keys
- [Settings](../../start/settings.md) — global vs agent vs request scope, env placeholders
