---
title: 模型概览
description: Agently 如何用三个协议层 Requester 组织模型 provider。
keywords: Agently, 模型, OpenAICompatible, AnthropicCompatible, providers
---

# 模型概览

> 语言：[English](../../en/models/overview.md) · **中文**

Agently 在协议层有三个 Request 插件，外加一组按 provider 整理的配置 recipe，每个 recipe 选其中一个插件。

## 分层视图

```text
应用代码
   │
   ▼
ModelRequest  ──►  ModelRequestResult
   │
   ▼
ModelRequester 插件（"协议层"）
   ├── OpenAICompatible             ◄── 多数 provider（Chat Completions）
   ├── OpenAIResponsesCompatible    ◄── Responses API 变体
   └── AnthropicCompatible          ◄── Claude
   │
   ▼
HTTP 调用模型端点
```

协议插件负责构造 HTTP 请求体并解析返回。Provider 配置只是设置预设，把 base_url / model 等填到对应插件下。

## 为什么是三个插件而不是一个

旧文档曾暗示「所有 provider 都走 OpenAICompatible」——这已经不准确。`OpenAICompatible`、`OpenAIResponsesCompatible`、`AnthropicCompatible` 是相互独立的 requester 插件；每个插件都直接实现 `ModelRequester` 协议，并各自维护自己的协议映射。Anthropic 尤其会构造自己的请求体——`anthropic_version`、`anthropic_beta`、必填的 `max_tokens`，以及 Claude 期望的 `messages`/`system` 字段形态。这些差异已经足够把 Claude 配错——所以新文档把它单独成一条路径。

自定义 requester handler 中，`build_request_handlers()` 返回
`AttemptHandlers`；handler stream 可用 `agently.types.data` 里的
`AttemptStreamMessage` / `AttemptStreamGenerator` 标注。`broadcast_response(...)`
再把 attempt/provider stream 映射成公开的 `AgentlyResultGenerator`。它必须原样透传
core-owned 的 `("status", payload)` attempt 记录，不能把它当作 provider wire payload。

如果你指向 `https://api.anthropic.com`（或某个走相同协议的 Claude 兼容代理），用 [AnthropicCompatible](anthropic-compatible.md)。其他情况（OpenAI、DeepSeek、Qwen、Ollama、Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE、走 OpenAI 兼容模式的 Gemini，以及任何说 OpenAI Chat Completions API 的私有网关），用 [OpenAICompatible](openai-compatible.md)。

## 插件选择表

| 你在调用 | 用哪个插件 |
|---|---|
| OpenAI、Azure OpenAI、Gemini-via-OpenAI | `OpenAICompatible` |
| DeepSeek、Qwen、Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE | `OpenAICompatible` |
| Ollama 或任何 OpenAI 兼容的本地服务 | `OpenAICompatible` |
| Anthropic / Claude（原生 API） | `AnthropicCompatible` |
| 说 OpenAI Chat Completions API 的私有网关 | `OpenAICompatible` |
| 说 OpenAI Responses API 的私有网关 | `OpenAIResponsesCompatible` |
| 说 Anthropic Messages API 的私有网关 | `AnthropicCompatible` |

## 最小配置

```python
from agently import Agently

# OpenAI 兼容
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})

# 或 Anthropic
Agently.set_settings("AnthropicCompatible", {
    "base_url": "https://api.anthropic.com",
    "api_key": "${ENV.ANTHROPIC_API_KEY}",
    "model": "${ENV.ANTHROPIC_MODEL}",
    "max_tokens": 4096,
})
```

每个 provider 的 recipe（环境变量、常用 model 名、base URL）见 [Providers](providers/)。

## 用 Model Pool 切换模型

需要使用多个模型的应用，可以先用 `model_pool` 配置模型别名，再用
`activate_model(...)` 切换当前 Agent 模型。别名可以是具体的运行含义，例如
`ollama-qwen2.5` 或 `deepseek-v4`。

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

`activate_model(...)` 影响后续 Agent 自己创建和持有的请求，包括链式
`agent.input(...).start()` 和 `agent.create_execution()`。如果只想覆盖单次调用，
使用 `agent.create_request(model_key="deepseek-v4")`。

API key 会在请求时根据 key pool 的 `selection` 策略选择：`fixed`、`random`、
`round_robin` 或 `least_used`。旧的 `key_pool_strategy` 路径继续兼容。

provider 错误后的 failover 需要通过 `api_key_pools.<pool>.failover` 显式开启。没有
failover 策略时，provider 错误会按旧行为直接暴露。内置 failover 策略可以针对配置的
HTTP 状态码重试另一个 key；自定义 handler 可以检查 provider error object，并返回
`"try_next"`、`"retry_same"`、`"raise"`、某个 key id、一个 key entry dict，或
`{"key_id": "b"}` / `{"key_entry": context.keys[1]}` 这样的包装。

对 `OpenAICompatible`、`OpenAIResponsesCompatible` 和 `AnthropicCompatible`，框架构造的
HTTP 错误消息会保留 provider status 与 response detail，但不会附加序列化的模型请求。
core `model.requester.error` RuntimeEvent 仍在 `payload.request_data` 中保留结构化请求，供受控
冷诊断使用；request stream 则只通过正常终态路径传播一次 provider error。

三个 compatible requester 也共用同一套公开 transport retry 生命周期。
`request_retry=False` 或 `max_attempts=1` 表示只建立一次物理 SSE 连接；启用重放时，
每次连接都有公开递增的 attempt index 与 retry boundary。任何 requester 都不会执行
隐藏的内部 SSE 重连。

## 插件源码位置

- [agently/builtins/plugins/ModelRequester/OpenAICompatible/](../../../agently/builtins/plugins/ModelRequester/OpenAICompatible/)
- [agently/builtins/plugins/ModelRequester/OpenAIResponsesCompatible/](../../../agently/builtins/plugins/ModelRequester/OpenAIResponsesCompatible/)
- [agently/builtins/plugins/ModelRequester/AnthropicCompatible/](../../../agently/builtins/plugins/ModelRequester/AnthropicCompatible/)

每个内置 requester 都使用 runtime-handler 包结构：`plugin.py` 是公开
coordinator，私有实现职责分别放在 `modules/request_builder.py`、
`modules/credential.py`、`modules/transport.py`、`modules/handlers.py`
和 `modules/response_adapter.py`。

如果某个 provider 不在以上协议族（极少见），可以新增一个 requester 插件；但实际上几乎所有商用端点要么提供 OpenAI 兼容模式、Responses 形态，要么对齐 Anthropic 协议，所以这些内置插件覆盖大部分场景。

## 另见

- [OpenAICompatible 详情](openai-compatible.md)
- [AnthropicCompatible 详情](anthropic-compatible.md)
- [Providers](providers/)——按 provider 分组的 recipe
- [模型设置](../start/model-setup.md)——快速入门级配置
- [设置](../start/settings.md)——环境变量与分层
