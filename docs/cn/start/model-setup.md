---
title: 模型设置
description: 配置模型 provider、环境变量驱动的配置以及 agent 级覆盖。
keywords: Agently, 模型设置, OpenAICompatible, AnthropicCompatible, 环境变量
---

# 模型设置

> 语言：[English](../../en/start/model-setup.md) · **中文**

Agently 在 `agently.builtins.plugins.ModelRequester` 下提供三个协议层 Request 插件：

- `OpenAICompatible`——任何说 OpenAI Chat Completions API 的端点都走这条路径。覆盖 OpenAI、DeepSeek、Qwen、Ollama、Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE、Gemini-via-OpenAI。
- `OpenAIResponsesCompatible`——OpenAI Responses API 形态的端点。
- `AnthropicCompatible`——Anthropic 原生 API（Claude）。它是独立 requester 插件，请求体构造是 Anthropic 专属（`anthropic_version`、`anthropic_beta`、必填的 `max_tokens`）。

通过填写哪个插件名下的设置，来决定 active requester 是哪一个。

## 全局 vs Agent 级设置

设置是分层的。`Agently.set_settings(...)` 设全局默认；`agent.set_settings(...)` 只覆盖某个 agent。Agent 没显式覆盖的 key 沿用全局值。

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

# 仅这个 agent 覆盖 model
agent.set_settings(
    "OpenAICompatible",
    {"model": "qwen2.5:7b"},
)
```

详细分层规则见 [设置](settings.md)。

## 常见 provider 配置

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

### Ollama（本地）

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen2.5:7b",
})
```

本地服务不需要鉴权时可以省略 `api_key`。

更多 provider（Kimi、GLM、MiniMax、Doubao、SiliconFlow、Groq、ERNIE、Gemini）见 [模型概览](../models/overview.md) 与 [Providers](../models/providers/)。

## 环境变量与 dotenv

设置中任何位置使用 `${ENV.<NAME>}` 占位符，会在 settings 被读取时替换为对应环境变量。占位符由 [agently/utils/Settings.py](../../../agently/utils/Settings.py) 解析。

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "${ENV.OPENAI_BASE_URL}",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})
```

要在启动时自动加载 `.env`，从配置文件加载时传 `auto_load_env=True`：

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

## Typed provider settings

Provider settings 可以继续用 dict，也可以用 typed helper class。typed class 只负责
构造提示和提前校验；进入 Agently 后仍会存成同一个 provider namespace 下的 dict。

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

DeepSeek V4 Flash 默认开启 thinking。若应用依赖 `temperature` 等采样参数，应
显式关闭 thinking；DeepSeek 在 thinking 开启时会忽略这些参数。需要显式启用
推理时可使用：

```python
Agently.set_settings("OpenAICompatible.request_options", {
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high",
})
```

通用 OpenAI-Compatible 适配器会通过 `reasoning_delta` / `reasoning_done`
specific 事件输出推理内容，并在 `original_done` 中保留
`reasoning_content`。

DeepSeek V4 同时提供 Anthropic Messages endpoint。需要该协议时，应激活独立的
Anthropic-Compatible 插件：

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

该路径会把 Anthropic `thinking` block 输出为同一组 Agently
`reasoning_delta` / `reasoning_done` 事件，并保留原始 block 与 signature，
供 tool-call 后续轮次回传。DeepSeek 开启 thinking 时应使用自动工具选择；当前
endpoint 会拒绝强制指定名称的 `tool_choice`。

内置 provider helper 位于 `agently.types.settings`。第三方插件可以从自己的插件包
导出 settings class，并通过插件的 `SETTINGS_SCHEMAS` 注册。

## Model profiles 与 key pools

需要按业务场景路由模型时，使用分层配置：

- `model_pool`：业务 key 到 model profile id 的映射。
- `model_profiles`：保存 provider、model、endpoint、request options 和 key pool。
- `api_key_pools`：保存 API key 池与轮换策略。

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

`selection` 控制一次新的独立请求开始前如何选 key，支持 `fixed`、`random`、
`round_robin`、`least_used`；旧的顶层 `strategy` / `mode` 仍作为 selection 快捷写法。

`failover` 控制 provider 请求遇到 credential 或 provider-side 错误后怎么处理。如果没有
声明 `failover`，Agently 不会自动尝试另一个 credential。`OpenAICompatible` 仍然有一个
窄范围传输重放：会按 `OpenAICompatible.request_retry`（默认
`{"max_attempts": 2, "after_output": true}`）用同一个模型、prompt 和输出格式重试。
partial 输出后的重放会发出 `$status` 和 `"<$retry>{reason}</$retry>"` 标记，消费者需要
先清空临时输出，再接受替代 delta。内置 `try_next` 只会对
配置的 HTTP 状态码尝试下一个 key。默认建议使用 `401`、`403`、`429` 这类鉴权或额度相关
状态码。`405` 和 `422` 很多时候代表 endpoint、method、payload 或模型能力不匹配，只有当
你的 provider 明确用它们表达 key 或 quota 失败时才加入。

两层策略都可以直接使用 Python handler：

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

failover handler 可以返回 `"try_next"` / `"retry_next"`、`"retry_same"`、`"raise"`、
某个 key id、一个 key entry dict，或 `{"key_id": "b"}` /
`{"key_entry": context.keys[1]}` 这样的显式包装。provider failover 的 retry
budget 和输出解析 / validation 的 `max_retries` 是两件事。

旧的 `model_pool -> key_pool_strategy -> key_pool` 写法继续兼容。

## 验证连通性

```python
result = (
    Agently.create_agent()
    .input("用一句话打个招呼。")
    .start()
)
print(result)
```

如果能打印出文字，说明模型配置已生效。如果出现鉴权或连接错误，问题在 `base_url` / `api_key` / 网络可达性，与 prompt 或 output schema 无关。

## 另见

- [模型概览](../models/overview.md)——协议层细节
- [设置](settings.md)——分层与环境变量
- [项目结构](project-framework.md)——把模型配置放进文件而不是写在代码里
