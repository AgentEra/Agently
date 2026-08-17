---
title: AnthropicCompatible
description: 用于 Claude 与 Anthropic 兼容端点的协议层插件。
keywords: Agently, AnthropicCompatible, Claude, Anthropic, model
---

# AnthropicCompatible

> 语言：[English](../../en/models/anthropic-compatible.md) · **中文**

`AnthropicCompatible` 是 Claude 的协议层插件。它说 Anthropic 的 Messages API —— 与 OpenAI Chat Completions 区别足够大，用 `OpenAICompatible` 映射会出错配置。指向 `https://api.anthropic.com` 或任何 Claude 兼容代理时用本插件。

## 设置

```python
from agently import Agently

Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
    "stream_idle_timeout": 45.0,
})
```

| Key | 含义 |
|---|---|
| `base_url` | `https://api.anthropic.com`（或代理） |
| `api_key` | bearer token |
| `model` | Claude 模型 id；接入时以 Anthropic 当前模型列表为准 |
| `max_tokens` | Anthropic API **必填**；有合理默认但建议显式 pin 以可预期成本 |
| `anthropic_version` | API 版本 header（默认近期稳定版本） |
| `anthropic_beta` | 可选 beta-feature header（字符串或字符串列表） |
| `stream_idle_timeout` | 可选 liveness deadline，约束流式事件间隔和非流式响应生成等待 |
| `request_retry` | 临时传输错误重试策略；默认 `{"max_attempts": 2, "after_output": true}` |
| `request_options` | 转给底层 HTTP client 的额外 dict |

公开 coordinator 在 [agently/builtins/plugins/ModelRequester/AnthropicCompatible/plugin.py](../../../agently/builtins/plugins/ModelRequester/AnthropicCompatible/plugin.py)，私有实现模块放在 `modules/` 下。

## 为什么是独立插件

Claude 与 OpenAI 在以下方面有插件需处理的具体差异：

- 请求体顶层用 `system` 字段，不在 `messages` 里。
- `max_tokens` 必填。
- header 含 `anthropic-version` 与（适用时）`anthropic-beta`。
- 流式事件形态用 `message_start`、`content_block_delta`、`message_delta` 等，而非 OpenAI 的 `chat.completion.chunk`。
- tool calling 用 Anthropic 自己的请求 / 响应形态。

`AnthropicCompatible` 直接实现 `ModelRequester`；请求体构造与解析仍然是 Anthropic 专属。不要把它想成「OpenAI 换 URL」。

## Per-agent 覆盖

与任何插件设置同模式：

```python
agent = Agently.create_agent()
agent.set_settings("AnthropicCompatible", {"model": "${ENV.ANTHROPIC_MODEL_FAST}"})
```

## Tool calling

`AnthropicCompatible` 原生支持 Claude 的 tool-use 协议。通过 `@agent.action_func` / `agent.use_actions(...)` 注册的 tool 以 Claude 期望的格式暴露，tool 调用结果经 Messages API 正确往返。

## 流式

`result.get_generator(type="delta")` / `get_async_generator(type="delta")` 产出增量文本。`type="instant"` 结构化流式与 `OpenAICompatible` 上一样 —— 区别仅在上游解析。

物理 SSE 连接与其他 compatible adapter 共用同一套公开请求 attempt 生命周期。
设置 `request_retry=False` 或 `max_attempts=1` 可关闭重放；设置
`after_output=False` 可阻止 partial 输出后的重放。transport 不会执行未报告的内部重连。

## Beta 特性

需要 beta 特性（长上下文、自定义 tool 变体等）时设 `anthropic_beta`：

```python
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
    "anthropic_beta": "tools-2024-04-04",  # 或列表
})
```

header 原样转发。有效值查 Anthropic 当前 beta 文档。

## 另见

- [模型概览](overview.md) —— 协议选择与 OpenAI vs Anthropic 拆分
- [OpenAICompatible](openai-compatible.md) —— 另一个协议插件
- [Action Runtime](../actions/action-runtime.md) —— 协议层之上的 tool calling
- [模型设置](../start/model-setup.md) —— 快速入门级走读
