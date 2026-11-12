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

## Python 3.10 启动通知

在 Python 3.10 下，Agently 4.1.4.11 在每个进程首次导入时显示一条提示。
导入前设置环境变量 `AGENTLY_PYTHON_310_NOTICE=0` 即可关闭。
命令示例见 [4.1.4.11 发布说明](../development/release-notes-4.1.4.11.md)。

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

`True` 与 `"simple"` 完全等价：打印可读 Prompt、provider/model 摘要、模型响应流、
Action 目标与结果切片，以及关键过程/失败状态；不展开 provider 请求 JSON。AgentTask
运行时，模型生成的 progress 消息会在同一个持续更新的控制台块中展示。直接模型响应
使用顺序权威的规范化 ModelRequest stream；所有字符都会先于 Done 展示，稍后的
AgentExecution 投影不会重复或在 Done 后重新打开流。
当多个 ModelRequest 重叠时，最先产生 delta 的响应保持前台控制台流；后到响应继续正常
执行，ConsoleSink 只提示一次后台生成并缓冲其展示内容。前台请求终止后，仍在运行的响应
装载有界 buffer 并继续实时输出，已经完成的响应则直接打印最终物化结果。这个 FIFO
规则只调度展示，绝不串行化、限流或改变 ModelRequest / AgentExecution 的真实调度。
simple 模式始终为每个成功响应保留至少一个完整投影：正常实时流已经是完整正文，不会
重复打印；没有实际展示流的响应会完整打印最终物化结果；并发后台响应若超过实时重放
buffer，ConsoleSink 不会把残缺重放冒充完整输出，而是在生成完成时完整打印权威结果。
只有诊断和预览允许按各自合同限长。
只要某个响应占有前台流，ConsoleSink 就会优先保证正文阅读连续性：普通响应字符之间只会
插入一条精简的后台响应提示。普通 Prompt、provider request、Process 和成功生命周期诊断
会进入有界的控制台展示队列，在所有 FIFO 响应都展示完成后，统一列在
`[Deferred diagnostics]` 下。Warning、failure、cancellation、blocked/unhealthy、interrupt
与 approval-required 事件仍会即时显示。EventCenter 与 DevTools 仍按原始时刻收到原始
事件；延后的只有给人看的控制台排版。
对于 Action-or-Response loop，simple 模式隐藏正常的内部规划 Prompt/决策流，只展示
一次已接受的外层 response；规划 validation 失败仍然可见。detail 模式可以把内部决策
作为诊断证据展示。

`debug="detail"` 是高信息密度诊断视图，不是“打印全部事件”。它额外展示完整的可读
Prompt、脱敏后的 provider 请求 JSON、attempt/validation/telemetry、Action 参数与结果
明细、路由/阶段元数据和最终物化结果。对于流式请求，较重的 Prompt/request/process
明细会在响应展示完成后进入带标签的延后诊断区，而不是打断正文；同一 ModelRequest 的流式字符只展示一次，
`runtime.progress.*` 与 AgentExecution 镜像不会重复打印。需要完整事件审计、存储或重放时，
请使用 EventCenter hook 或 DevTools。debug 也不会替代完整的面向用户过程与最终答案输出；
还需要同时消费公开 delta：

```python
agent.set_settings("debug", "detail")
task = agent.create_task(goal="准备报告。", execution="flat")
await task.async_streaming_print()
result = await task.async_get_full_data()
```

这个组合会同时显示详细诊断、可读任务阶段和终态结果，但不会把原始事件 JSON 混入
公开文本 delta。

运行时日志也可以按 family 单独打开：

```python
Agently.set_settings("runtime.show_model_logs", True)
Agently.set_settings("runtime.show_action_logs", True)
Agently.set_settings("runtime.show_trigger_flow_logs", True)
Agently.set_settings("runtime.show_runtime_logs", "detail")
```

这些开关都接受 `False` / `"off"`、`True` / `"simple"`、`"detail"`。`"simple"` 是可读业务执行摘要，`"detail"` 是经过筛选、去重和有界展示的深度诊断；两者都不是完整 RuntimeEvent dump。Action loop 事件显示为 `ActionLoop`；具体 `action.*` 事件会显示 action 名称和 `action_type`。`runtime.show_tool_logs` 仍兼容旧代码；当没有显式设置 `runtime.show_action_logs` 时，它会启用同一组 Action Runtime 日志。开始事件显示 `Started`，正常结束显示 `Completed`，只有失败事件或显式失败 payload 才显示 `Failed`。

对于 ModelRequest 输出校验，`"simple"` 会显示 validator、失败原因、尝试次数摘要和
重试迁移；`"detail"` 可额外显示有限长度的校验上下文或 validator traceback，但不会在
相邻的 retry 条目中重复模型响应或校验原因。完整的结构化事实仍保留在对应的
RuntimeEvent 和 DevTools observation 中。

如果生产环境明确保留了一些 legacy compatibility 调用，可以全局关闭 deprecation warning：

```python
Agently.set_settings("runtime.show_deprecation_warnings", False)
```

这个开关只影响 Agently 的 deprecation warning。运行期告警、错误，以及 `flow_data` 这类 risky-scope warning，仍由各自 API 与设置控制。

## 另见

- [模型设置](model-setup.md)——provider 专属配置
- [项目结构](project-framework.md)——基于文件的设置布局
