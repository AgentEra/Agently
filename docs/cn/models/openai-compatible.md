---
title: OpenAICompatible
description: 用于 OpenAI 与每个说同协议 provider 的协议层插件。
keywords: Agently, OpenAICompatible, OpenAI, DeepSeek, Qwen, Ollama, model
---

# OpenAICompatible

> 语言：[English](../../en/models/openai-compatible.md) · **中文**

`OpenAICompatible` 是三个协议层模型 request 插件之一（见 [模型概览](overview.md)）。它处理任何说 OpenAI Chat Completions API 的端点 —— 今天覆盖多数商用 provider 与多数本地模型服务。

## 设置

```python
from agently import Agently

Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

| Key | 含义 |
|---|---|
| `base_url` | API 根，如 `https://api.openai.com/v1` |
| `api_key` | bearer token；本地无鉴权服务可省略 |
| `model` | provider 模型名 |
| `model_type` | `"chat"`（默认）或 `"completion"`（旧 completion 端点） |
| `request_retry` | 临时传输错误重试策略；默认 `{"max_attempts": 2, "after_output": true}` |
| `request_options` | 转给底层 HTTP client 的额外 dict（timeout、header） |

完整集合在 [agently/builtins/plugins/ModelRequester/OpenAICompatible/](../../../agently/builtins/plugins/ModelRequester/OpenAICompatible/) 包目录中。公开插件类由 `plugin.py` 导出，请求构造、鉴权、transport、handler 绑定和 response mapping 放在私有 `modules/` 包下。

## Responses API 变体

部分 provider（与 OpenAI 自身的新模型）说 Responses API 而非 Chat Completions。Agently 有兄弟插件：

```python
Agently.set_settings("OpenAIResponsesCompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_RESPONSES_MODEL}",
})
```

`OpenAIResponsesCompatible` 是 `OpenAICompatible` 的兄弟；按你端点暴露的协议选。两个插件都直接实现 `ModelRequester`，彼此不继承。

Responses 的 reasoning 配置会通过 `request_options` 原样传递，例如：

```python
Agently.set_settings("OpenAIResponsesCompatible.request_options", {
    "reasoning": {"effort": "low", "summary": "auto"},
})
```

当端点返回 reasoning summary 时，插件会把
`response.reasoning_summary_text.delta` 映射为 Agently
`reasoning_delta`，并把最终 `reasoning` output item 中的
`summary_text` 合并为 `reasoning_done`；完整 Responses 对象仍通过
`original_done` 保留。`response.completed` 与 `response.incomplete`
终态事件会结束 provider stream，同时继续传递 Agently 的最终生命周期状态。

DeepSeek 当前提供 Chat Completions 与 Anthropic Messages 兼容入口，没有
Responses endpoint。DeepSeek 应选择前两种 requester；这里的 Responses 映射适用于
OpenAI 以及确实暴露 `/responses` 的兼容网关。

## 「OpenAI 兼容」实际覆盖什么

provider 满足 OpenAI 兼容当其端点：

- 接受 JSON body 含 `messages: [{"role": ..., "content": ...}, ...]`。
- 返回 JSON 响应或 token delta 的 SSE 流。
- 用标准字段如 `model`、`temperature`、`max_tokens`、`tools` 等。

适配的 provider：

- OpenAI / Azure OpenAI
- DeepSeek（`https://api.deepseek.com/v1`）
- Qwen / DashScope 兼容模式（`https://dashscope.aliyuncs.com/compatible-mode/v1`）
- Kimi / Moonshot（`https://api.moonshot.cn/v1`）
- GLM（`https://open.bigmodel.cn/api/paas/v4/`）
- MiniMax、Doubao、ERNIE —— 多数发 OpenAI 兼容模式
- SiliconFlow、Groq —— 都暴露 OpenAI 兼容端点
- Gemini —— 经 OpenAI 兼容端点
- Ollama（本地）—— `http://127.0.0.1:11434/v1`
- vLLM、LM Studio、llama.cpp server（本地）
- 多数团队在商用模型之上自建的内部网关

按 provider 的 recipe 见 [Providers](providers/)。

## Per-agent 覆盖

agent 级设置覆盖全局：

```python
agent = Agently.create_agent()
agent.set_settings("OpenAICompatible", {"model": "${ENV.OPENAI_MODEL_FAST}"})
```

也可经请求链做请求级覆盖 —— 见 [设置](../start/settings.md)。

## 流式与 tool

`OpenAICompatible` 处理流式响应（被 `get_generator(...)` / `get_async_generator(...)` 用）与 tool calling（被 action runtime 用）。不需要按 provider 启用 —— 协议允许就开。

某 provider 没完全实现 OpenAI 语义中的某项时（如怪异流式格式），底层插件尽量容忍；具体 case 通过 issue 报告。

对连接重置、provider 断开连接这类临时传输错误，`OpenAICompatible` 默认用同一个请求重试
一次。它不会改变已选模型、prompt 或结构化输出格式。默认也覆盖已经输出 partial 内容后
发生的断流，因此 stream 消费者必须处理保留的 `$status` 记录、处理纯文本 delta 的
`"<$retry>{reason}</$retry>"` 标记，或只读取最终结果。设置
`"request_retry": {"max_attempts": 1}` 或 `"request_retry": False` 可以关闭这次重放。
这也会关闭物理 SSE 重连：每次物理连接都由同一套公开 attempt 生命周期管理，transport
不会执行未报告的内部重连。
只有当消费者无法在 retry 边界清空临时输出时，才设置 `request_retry.after_output=False`：

```python
agent.set_settings("OpenAICompatible.request_retry", {
    "max_attempts": 2,
    "after_output": False,
})
```

重放前 Agently 会发出 `("status", payload)` stream event，其中
`payload["status"] == "failed"` 且 `payload["retry"] is True`。payload 包含失败的
`attempt_index`、`next_attempt_index`、provider 的实际错误 `reason` 和 `error_type`。
`instant` / `streaming_parse` 消费者会在 `$status` 收到同一记录，应清除这个失败 attempt
的临时输出。普通 `delta` generator 会在同一边界收到独立的
`"<$retry>{reason}</$retry>"` 标记，必须先清空本地 delta buffer，再接受替换 attempt
的正文。

## 另见

- [模型概览](overview.md) —— 协议选择
- [AnthropicCompatible](anthropic-compatible.md) —— 另一个协议插件
- [Providers](providers/) —— 各 provider 的 recipe
- [模型设置](../start/model-setup.md) —— 快速入门级走读
