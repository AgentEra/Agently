---
title: Provider 配置
description: 按模型 provider 分组的配置 recipe —— base URL、环境变量、模型名占位。
keywords: Agently, providers, OpenAI, DeepSeek, Qwen, Claude, Ollama, recipes
---

# Provider 配置

> 语言：[English](../../../en/models/providers/index.md) · **中文**

下面每条都是配置块。放进 `Agently.set_settings(...)` 调用并按 provider 官方文档填写当前可用的模型名。所有例子用 `${ENV.*}` 占位让你不把密钥放代码。

协议层细节（用哪个插件、有什么字段）见 [模型概览](../overview.md)。

## OpenAI

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

走 Responses API 的模型：

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

`anthropic_beta` 等 Anthropic 专属 key 见 [AnthropicCompatible](../anthropic-compatible.md)。

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

## GLM / 智谱

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://open.bigmodel.cn/api/paas/v4/",
    "api_key": "${ENV.GLM_API_KEY}",
    "model": "${ENV.GLM_MODEL}",
})
```

## MiniMax

MiniMax 同时提供全球站与中国站的 OpenAI 兼容和 Anthropic 兼容端点。

OpenAI 兼容全球站端点：

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.minimax.io/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

OpenAI 兼容中国站端点：

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.minimaxi.com/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

Anthropic 兼容全球站端点：

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.minimax.io/anthropic/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

Anthropic 兼容中国站端点：

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.minimaxi.com/anthropic/v1",
    "api_key": "${ENV.MINIMAX_API_KEY}",
    "model": "${ENV.MINIMAX_MODEL}",
})
```

## Doubao（豆包）

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "api_key": "${ENV.DOUBAO_API_KEY}",
    "model": "${ENV.DOUBAO_MODEL}",
})
```

## ERNIE（文心）

ERNIE 通过千帆支持 OpenAI 兼容模式：

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://qianfan.baidubce.com/v2",
    "api_key": "${ENV.QIANFAN_API_KEY}",
    "model": "${ENV.QIANFAN_MODEL}",
})
```

## SiliconFlow（硅基流动）

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

## Gemini（经 OpenAI 兼容端点）

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "api_key": "${ENV.GEMINI_API_KEY}",
    "model": "${ENV.GEMINI_MODEL}",
})
```

## Ollama（本地）

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen2.5:7b",   # 或你 pull 的模型
})
```

本地服务无鉴权可省略 `api_key`。

## vLLM / LM Studio / llama.cpp server（本地）

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://localhost:8000/v1",   # 你的服务暴露的
    "model": "your-served-model-name",
})
```

## 自建内部网关

团队跑了私有网关说 OpenAI Chat Completions 或 Anthropic Messages API 时用对应插件并把 `base_url` 指向网关：

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://gw.internal/openai-compat/v1",
    "api_key": "${ENV.INTERNAL_GATEWAY_TOKEN}",
    "model": "internal-default",
})
```

## 另见

- [模型概览](../overview.md) —— 选对协议插件
- [OpenAICompatible](../openai-compatible.md) 与 [AnthropicCompatible](../anthropic-compatible.md) —— 完整设置 key
- [设置](../../start/settings.md) —— 全局 vs agent vs request 范围、环境变量占位
