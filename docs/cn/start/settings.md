---
title: 设置
description: Agently 设置如何在全局、agent 与 request 之间分层，含环境变量占位。
keywords: Agently, 设置, set_settings, 分层, 环境变量, dotenv
---

# 设置

> 语言：[English](../../en/start/settings.md) · **中文**

Agently 设置是一个分层 key-value 存储，分三个 scope：

| Scope | 设置方式 | 可见范围 |
|---|---|---|
| 全局 | `Agently.set_settings(...)` | 此调用之后创建的所有 agent / request |
| Agent | `agent.set_settings(...)` | 由该 agent 创建的所有 request |
| Request / runtime | `start(..., max_retries=...)` 等方法级参数 | 仅这一次调用 |

低 scope 覆盖高 scope；没显式覆盖的 key 沿用上层。

`agent.set_settings(...)` 返回同一个 Agent，因此内联覆盖可以直接留在 Agent
链式表达式里：

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

## 设置路径

`set_settings(...)` 的第一个参数是点路径，常用：

| 路径 | 含义 |
|---|---|
| `OpenAICompatible` / `OpenAI` / `OAIClient` | `plugins.ModelRequester.OpenAICompatible` 的别名 |
| `OpenAIResponsesCompatible` / `OpenAIResponses` / `Responses` | `plugins.ModelRequester.OpenAIResponsesCompatible` 的别名 |
| `AnthropicCompatible` / `Anthropic` / `Claude` | `plugins.ModelRequester.AnthropicCompatible` 的别名 |
| `plugins.ModelRequester.<Name>` | 完整路径，与上面别名等价 |
| `debug` | 打开模型请求的流式控制台日志 |
| `runtime.show_model_logs` | 打开模型请求与响应解析的控制台日志；`True` 等价于 `"simple"` |
| `runtime.show_action_logs` | 打开 Action Runtime planning 与 execution 的控制台日志；`True` 等价于 `"simple"` |
| `runtime.show_tool_logs` | `runtime.show_action_logs` 的兼容别名，用于旧工具回路示例 |
| `runtime.show_trigger_flow_logs` | 打开 TriggerFlow execution / signal 的控制台日志；`True` 等价于 `"simple"` |
| `runtime.show_runtime_logs` | 打开 request、session、chunk、`runtime.print` 等通用 observation 事件的控制台日志；`True` 等价于 `"simple"` |
| `runtime.show_deprecation_warnings` | 发出 deprecated API warning；默认 `True`，设为 `False` / `"off"` 可全局关闭 deprecation warning |
| `runtime.session_id` | 把请求绑定到指定的 session id |

也可以一次传入一个 dict，按 key 合并：

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.openai.com/v1",
    "model": "${ENV.OPENAI_MODEL}",
})
```

## Typed settings helper

dict settings 仍然是长期兼容契约。为了获得编辑器提示和更早的类型校验，
Agently 也在 `agently.types.settings` 下提供 typed helper class。helper 会在
进入 settings store 前转换回同一个 dict namespace：

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

旧写法继续有效；生成式配置文件或 YAML/TOML/JSON 配置仍建议用 dict：

```python
Agently.set_settings("OpenAICompatible", {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "${ENV.DEEPSEEK_API_KEY}",
    "model": "deepseek-v4-flash",
    "request_options": {"thinking": {"type": "disabled"}},
})
```

## 读取设置

```python
agent_settings = agent.settings.get("plugins.ModelRequester.OpenAICompatible", {})
print(agent_settings.get("model"))
```

`settings.get(path, default)` 按点路径查找，找不到时返回 default。

## 环境变量占位

设置值的任何位置都可以写 `${ENV.<NAME>}`，读取时替换为对应环境变量。占位符由 [agently/utils/Settings.py](../../../agently/utils/Settings.py) 解析。

```python
Agently.set_settings("OpenAICompatible", {
    "api_key": "${ENV.OPENAI_API_KEY}",
})
```

## 从文件加载

非琐碎项目里，把设置放到 YAML / TOML / JSON，而不是写在 Python 内：

```python
from agently import Agently

Agently.load_settings("yaml_file", "settings.yaml", auto_load_env=True)
```

`auto_load_env=True` 会先加载工作目录下的 `.env`，然后再解析 `${ENV.*}`。
顶层别名会使用与 `set_settings(...)` 相同的映射，因此文件里既可以写 `OpenAICompatible:`，也可以写完整的 `plugins.ModelRequester.OpenAICompatible:` 路径。

如果需要直接操作 `Settings` 对象，也可以用 `Settings().load(...)`：

```python
from agently.utils import Settings

settings = Settings()
settings.load("yaml_file", "settings.yaml", auto_load_env=True)
```

完整的项目结构示例见 [项目结构](project-framework.md)。

## Debug 开关

```python
Agently.set_settings("debug", True)
```

打印精简的模型请求与结果日志。AgentTask 运行时，`"simple"` 还会打印 phase、
progress、snapshot 等过程事件摘要，但不打印 token 级 delta。需要完整 observation
诊断流和模型 delta 输出时，使用 `debug="detail"`。debug 不会自动替代面向用户的过程
与最终答案输出；还需要同时消费公开 delta：

```python
agent.set_settings("debug", "detail")
task = agent.create_task(goal="准备报告。", execution="flat")
await task.async_streaming_print()
result = await task.async_get_full_data()
```

这个组合会同时显示完整诊断、可读任务阶段和终态结果，但不会把原始事件 JSON 混入
公开文本 delta。

运行时日志也可以按 family 单独打开：

```python
Agently.set_settings("runtime.show_model_logs", True)
Agently.set_settings("runtime.show_action_logs", True)
Agently.set_settings("runtime.show_trigger_flow_logs", True)
Agently.set_settings("runtime.show_runtime_logs", "detail")
```

这些开关都接受 `False` / `"off"`、`True` / `"simple"`、`"detail"`。`"simple"` 打印请求/结果摘要、AgentTask 过程摘要和 warning/error/critical 事件；`"detail"` 打印该 family 的完整 observation 事件，包括模型 delta 输出。Action loop 事件显示为 `ActionLoop`；具体 `action.*` 事件会显示 action 名称和 `action_type`。`runtime.show_tool_logs` 仍兼容旧代码；当没有显式设置 `runtime.show_action_logs` 时，它会启用同一组 Action Runtime 日志。开始事件显示 `Started`，正常结束显示 `Completed`，只有失败事件或显式失败 payload 才显示 `Failed`。

如果生产环境明确保留了一些 legacy compatibility 调用，可以全局关闭 deprecation warning：

```python
Agently.set_settings("runtime.show_deprecation_warnings", False)
```

这个开关只影响 Agently 的 deprecation warning。运行期告警、错误，以及 `flow_data` 这类 risky-scope warning，仍由各自 API 与设置控制。

## 另见

- [模型设置](model-setup.md)——provider 专属配置
- [项目结构](project-framework.md)——基于文件的设置布局
