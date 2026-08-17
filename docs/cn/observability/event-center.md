---
title: Event Center
description: Agently RuntimeEvent 的注册、过滤、结构与 DevTools 投影约束。
keywords: Agently, EventCenter, ObservationEvent, RuntimeEvent, observation, DevTools
---

# Event Center

> 语言：[English](../../en/observability/event-center.md) · **中文**

Event Center 是 Agently 的框架级运行时事件通道。它承载 **RuntimeEvent**：模型请求、Session 应用、TriggerFlow 生命周期、Action 调用等都会通过这里把结构化事件交给自定义 hook、日志 sink 或 DevTools bridge。

它和 TriggerFlow 的 `emit` / `when` 不是一回事：`emit` / `when` 改变 flow 内部控制流；RuntimeEvent 记录发生了什么。

命名兼容：

- `RuntimeEvent` 是新代码推荐使用的框架事件模型。
- Event Center 向普通 hook 分发 `RuntimeEvent`。
- `ObservationEvent` 是 DevTools bridge 从 `RuntimeEvent` 派生出的通讯投影。
- 现有 `emit_observation` / `async_emit_observation` 继续作为兼容 alias 可用。

run 与 retry 命名：

- `agent_execution` 是一次 AgentExecution-owned Agent run 的 run lineage 类型。
- `attempt_index` 描述一次请求内部的模型重试 attempt；它不是 AgentExecution 计数。
- DevTools 应保持两者语义分离：从 `run.run_kind` 渲染 `agent_execution`，从 `model_request` run 的 `payload.attempt_index` 或 `run.meta.attempt_index` 读取模型重试 attempt。

模型请求 telemetry：

- 模型 RuntimeEvent 可在 `model.request_started`、`model.requesting`、`model.status`、`model.completed`、`model.meta`、`model.request_failed`、`model.requester.error` 上携带 `payload["model_request_telemetry"]`。
- telemetry payload 只用于观察，可包含 `response_id`、`attempt_index`、run ids、provider/model、request URL、duration、raw usage、归一化 usage summary、输入/输出字符长度估算、side-channel 和规范化 error 事实。
- telemetry 去重只移除同一 `response_id + attempt_index + event kind` 的重复 telemetry 子 payload；不会抑制原始 RuntimeEvent。
- 不要把这些 telemetry 事实反馈给 route 选择、retry policy、verifier 判断、quality scoring、planner context 或 prompt 内容。它们只用于日志、DevTools 展示和诊断。
- 每个 attempt 结果最多发布一个 `model.status` 事实。终态 provider 错误还会发布一个
  `model.requester.error`；发布这条观察事实不会重新抛回 attempt loop，也不会再创建一份结果。
- 内置 requester 的异常消息保留 provider status/detail，但不再附加序列化 request body。
  结构化的 `model.requester.error.payload["request_data"]` 仍作为冷诊断证据保留，其中可能
  含有敏感 prompt 数据，因此 event sink 必须采用与 request log 相同的访问与保留控制。

模型请求状态：

- `model.status` 记录一次 ModelRequest attempt 的结果事实，只用于观察；它不决定 retry 或下游控制流。
- 原始 response stream 使用 `("status", payload)`；`instant` / `streaming_parse` 使用 `StreamingData(path="$status", value=payload)`。
- `payload["status"]` 为 `completed`、`failed` 或 `cancelled`。
- `failed` 且 `retry=true` 表示 `payload["attempt_index"]` 的 partial 输出已失效，下一次 attempt 是 `payload["next_attempt_index"]`。消费者应先清除临时输出，再渲染替代 attempt 的 delta。
- `reason` 包含有界的 provider/transport 实际说明；有异常对象时 `error_type` 为原始异常类型。它不包含 traceback 或原始 request body。
- 纯文本 `type="delta"` generator 会在同一重放边界输出独立的
  `"<$retry>{reason}</$retry>"` chunk；消费者必须据此清除临时文本。需要 lineage 或
  无碰撞结构化事实时，应使用 `type="all"`、`specific`、`instant` 或 `streaming_parse`。

## 注册 hook

```python
from agently import Agently

captured = []


async def capture(event):
    captured.append(event)


Agently.event_center.register_hook(
    capture,
    event_types="runtime.info",
    hook_name="docs.capture",
)

emitter = Agently.event_center.create_emitter("Docs")
await emitter.async_info("hello")

Agently.event_center.unregister_hook("docs.capture")
```

`event_types` 可传字符串、字符串列表或 `None`。传 `None` 时 hook 接收所有事件。同步函数也能注册；Event Center 会统一转成 async 调用。

如果 hook 要把高频 runtime 事件转发到成本较高的出口，可以让 Event Center
对这个 hook 做摘要投递：

```python
Agently.event_center.register_hook(
    capture,
    event_types="model.response.delta",
    hook_name="docs.summary_capture",
    delivery_policy={
        "mode": "summary",
        "dispatch": "await",
        "emit_interval": 0.1,
        "max_items": 20,
        "high_frequency_only": True,
    },
)
```

默认投递策略是 raw 且 awaited。摘要投递只作用于当前 hook；不会改变生产者发出的
RuntimeEvent，也不会影响其他要求 raw 事件的 hook。摘要事件会带
`meta["coalesced"]`、`coalesced_count`、`first_event_id` 和 `last_event_id`。

只有具备明确 flush/close 回收点的 best-effort 出口才应使用
`dispatch="background"`。关闭前可调用
`await Agently.event_center.async_flush(hook_name)` 来排空摘要 buffer 和已跟踪的后台投递。
Event Center 在存在后台投递或摘要 buffer 时，也会启动按需 idle flush monitor：
新事件会刷新 idle 计时，安静一段时间后触发有界 flush。这个机制是长生命周期
event loop 的兜底，不替代 CLI/script 退出前的显式 flush。

面向框架集成者的实现说明：Agently 4.1.4.5 的 Event Center 保留原生后台任务
跟踪机制。Stage-backed 候选虽然保持了行为，但在后台事件基准中增加了可测量的
elapsed、p95 与 traced-memory 开销，因此没有接入。RuntimeEvent 规范化、过滤、
摘要、hook 身份、fail-open 行为与 flush policy 仍由 Event Center 拥有；
Event Center API 不会暴露 Stage 类型、状态或事件。

## 发送 runtime event

`model.*`、`request.*`、`action.*`、`tool.*`、`session.*`、
`agent_execution.*`、`triggerflow.*`、`execution_resource.*` 这类 Agently
官方事件类型由 core 运行时协调器产出。自定义插件和应用可以向 Event Center
发送自己的消息，但应该使用应用/插件自有命名空间，也不能依赖 Agently 官方模块
消费这些自定义消息。

内置插件通过 typed observation、handler decision 或 route stream callback
把事实报告给 core。core 运行时协调器再把这些事实映射成官方 RuntimeEvent
记录和 AgentExecution stream item。

常见路径是创建 emitter：

```python
emitter = Agently.event_center.create_emitter(
    "BillingWorker",
    base_meta={"tenant": "demo"},
)

await emitter.async_emit(
    "billing.invoice_created",
    message="invoice created",
    payload={"invoice_id": "inv-1"},
)
```

也可以直接发 dict：

```python
await Agently.event_center.async_emit({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "direct event",
})
```

同步代码不能等待 hook 投递时，使用受跟踪的 nowait 入口：

```python
dispatch = Agently.event_center.emit_nowait({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "tracked nowait event",
})
```

存在 running loop 时，`dispatch` 是 `asyncio.Task`；不存在时则是 loop-neutral
Stage handle。`emit_nowait()` 不代表投递已经完成，owner 应在关闭或观察 barrier
调用 `await Agently.event_center.async_flush()`。

顶层便捷 API：

```python
await Agently.async_emit_observation({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "compatibility observation API",
})

await Agently.async_emit_runtime({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "runtime API",
})
```

## Event 结构

`RuntimeEvent` 的顶层字段来自 `agently.types.data.event.RuntimeEvent`。主框架 DevTools bridge 把 RuntimeEvent 投影给 DevTools 时，`ObservationEvent` 使用相同的序列化结构：

| 字段 | 含义 |
|---|---|
| `event_id` | 事件 id，默认自动生成 |
| `event_type` | 点路径，例如 `triggerflow.execution_started` |
| `source` | 事件来源 |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `message` | 人可读消息 |
| `payload` | 事件自己的结构化数据 |
| `error` | 错误信息；传入异常时会规范化为 `ErrorInfo` |
| `run` | run lineage，包括 `run_id`、`parent_run_id`、`session_id`、`execution_id` 等 |
| `meta` | 附加元数据 |
| `timestamp` | 毫秒时间戳 |

对于模型请求事件，`payload.model_request_telemetry` 是可扩展子 payload。消费者应把缺失字段视为未知，而不是失败。常见字段：

| 字段 | 含义 |
|---|---|
| `event_kind` | 携带该 telemetry 的原始模型事件类型 |
| `telemetry_key` | 去重 key，通常是 `response_id:attempt_index:event_kind` |
| `response_id` | request/response 关联 id |
| `attempt_index` | 请求内部的 retry attempt 编号 |
| `request_run_id` / `model_run_id` | request 与 model attempt 的 run lineage id |
| `provider` / `provider_family` / `model` | 可得的 provider 元数据 |
| `request_url` | provider endpoint 或 provider 自有 symbolic URL |
| `duration_ms` | 可得时从模型请求开始计算的耗时 |
| `usage` | provider 上报的 usage 元数据 |
| `usage_summary` | 只用于观察的 usage 摘要，包含归一化 provider token 字段和输入/输出字符长度估算；终态 `model.status` 可携带估算长度而不暴露 raw request payload；provider token 缺失时展示为未知，而不是失败 |
| `side_channel` | 是否来自 side-channel request 路径 |
| `error` | failed/requester-error 事件上的规范化错误事实 |

## TriggerFlow 事件别名

Event Center 会兼容 TriggerFlow 历史事件前缀。订阅 `workflow.execution_started` 可以收到 `triggerflow.execution_started`；订阅 `trigger_flow.signal` 可以收到 `triggerflow.signal`。文档和新代码优先写 `triggerflow.*`。

## Action 兼容事件

Action Runtime 生命周期事件以 `action.*` 作为主命名空间。当当前 Action Runtime 分支兼容 tool 时，Agently 会额外发出配对的 `tool.*` 兼容事件，用于旧订阅者和旧示例：

| 主事件 | tool 兼容事件 |
|---|---|
| `action.loop_started` | `tool.loop_started` |
| `action.plan_ready` | `tool.plan_ready` |
| `action.loop_failed` | `tool.loop_failed` |
| `action.loop_completed` | `tool.loop_completed` |

配对兼容事件会带上 `meta.compat_event_alias=True`、`meta.compat_alias_for` 和 `meta.primary_event_id`，方便消费者与主 `action.*` 事件去重。

具体 action 执行使用 `action.started`、`action.completed` 和 `action.failed`。
因 policy 或 sandbox gate 在正常执行前停止的 action 使用
`action.approval_required` 或 `action.blocked`，不会再被记录成普通失败。
对于 tool-backed action，`payload.action_type` 可以是 `"tool"`；这不会改变事件 family。

## ExecutionResource 事件

ExecutionResource 生命周期使用 `execution_resource.*`。Provider 与 DevTools
消费者都应把这个 namespace 当作可扩展协议处理。当前 manager 事件包括 `declared`、
`approval_required`、`ensuring`、`ready`、`unhealthy`、`releasing`、`released`
和 `failed`。`unhealthy` 表示 ready handle 在复用前 health check 失败；manager 会释放它并
ensure 一个新 handle。

## 运行进展与卡死诊断

Event Center 是 runtime event 的接收与出口分发层。liveness 状态会在高成本 hook
投递前更新，因此缓慢或失败的 hook 不能阻塞卡死诊断。

AgentExecution 会把进展记录在 `async_get_meta()["diagnostics"]`：

- `diagnostics["stages"]["events"]` 保留最近的 stage 进展。
- `diagnostics["last_progress"]` 记录最后一次被接受的进展事件。
- `diagnostics["timeouts"]` 记录硬截止超时。
- `diagnostics["stalls"]` 记录 idle 无进展卡死。

调试线上或本地应用时，可以临时挂 Event Center hook，或用
`.set_settings("debug", True)` 打开请求/结果与过程摘要，用
`.set_settings("debug", "detail")` 打开完整 observation 与模型 delta 输出。
RuntimeEvent 诊断与公开业务文本是两个独立出口；还需要消费
`execution.get_async_generator(type="delta")` 或调用
`await execution.async_streaming_print()`，才能看到完整可读过程与最终结果。
问题定位后，应从代码中移除临时 debug hook 和 debug settings。

## 兼容约束

RuntimeEvent 是可扩展的框架事件协议。Agently-DevTools 和自定义消费者应按 fail-open 处理：

- 忽略未知顶层字段和未知 `payload` 字段。
- 对未知 `event_type` 不报错。
- 不把 `payload` 当成严格 schema；它可以按事件类型增量扩展。
- 需要关联请求、Session 或 TriggerFlow execution 时优先读 `run`，不要从 `message` 解析。

## 另见

- [TriggerFlow 事件与流](../triggerflow/events-and-streams.md) —— flow 内部控制流与 runtime stream
- [DevTools](devtools.md) —— 现成的观测与评估 bridge
- [FastAPI 服务封装](../services/fastapi.md) —— 把 runtime stream 转给服务客户端
- [Coding Agents](../development/coding-agents.md) —— 通过 Agently Skills 给 coding agent 提供指引
