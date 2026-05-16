---
title: Event Center
description: Agently observation event 的注册、过滤、结构与兼容约束。
keywords: Agently, EventCenter, ObservationEvent, RuntimeEvent, observation, DevTools
---

# Event Center

> 语言：[English](../../en/observability/event-center.md) · **中文**

Event Center 是 Agently 的框架级观测通道。它承载 **observation event**（观测事件）：模型请求、Session 应用、TriggerFlow 生命周期、Action 调用等都会通过这里把结构化事件交给 DevTools 或自定义日志 sink。

它和 TriggerFlow 的 `emit` / `when` 不是一回事：`emit` / `when` 改变 flow 内部控制流；observation event 只是观察发生了什么。

命名兼容：

- `ObservationEvent` 是新代码推荐命名。
- `RuntimeEvent` 是历史命名，在 Agently 4.1.x 线内继续作为兼容 alias。
- Agently 4.2 是已标注废弃事件命名的目标替换线。
- 现有 `emit_runtime` / `async_emit_runtime` 继续可用；新代码可以使用 `emit_observation` / `async_emit_observation`。

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

## 发送 observation event

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

顶层便捷 API：

```python
await Agently.async_emit_observation({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "preferred observation API",
})

# 4.1.x 兼容 alias：
await Agently.async_emit_runtime({
    "event_type": "runtime.info",
    "source": "Docs",
    "message": "legacy runtime API",
})
```

## Event 结构

`ObservationEvent` 的顶层字段来自 `agently.types.data.event.ObservationEvent`。`RuntimeEvent` 结构相同，并在 4.1.x 线内保留为兼容 alias：

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

具体 action 执行仍使用 `action.started`、`action.completed` 和 `action.failed`。对于 tool-backed action，`payload.action_type` 可以是 `"tool"`；这不会改变事件 family。

## Execution Environment 事件

Execution Environment 生命周期使用 `execution_environment.*`。Provider 与 DevTools
消费者都应把这个 namespace 当作可扩展协议处理。当前 manager 事件包括 `declared`、
`approval_required`、`ensuring`、`ready`、`unhealthy`、`releasing`、`released`
和 `failed`。`unhealthy` 表示 ready handle 在复用前 health check 失败；manager 会释放它并
ensure 一个新 handle。

## 兼容约束

Observation event 是观测协议。Agently-DevTools 和自定义消费者应按 fail-open 处理：

- 忽略未知顶层字段和未知 `payload` 字段。
- 对未知 `event_type` 不报错。
- 不把 `payload` 当成严格 schema；它可以按事件类型增量扩展。
- 需要关联请求、Session 或 TriggerFlow execution 时优先读 `run`，不要从 `message` 解析。

## 另见

- [TriggerFlow 事件与流](../triggerflow/events-and-streams.md) —— flow 内部控制流与 runtime stream
- [DevTools](devtools.md) —— 现成的观测与评估 bridge
- [FastAPI 服务封装](../services/fastapi.md) —— 把 runtime stream 转给服务客户端
- [Coding Agents](../development/coding-agents.md) —— 通过 Agently Skills 给 coding agent 提供指引
