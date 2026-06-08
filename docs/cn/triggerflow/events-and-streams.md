---
title: 事件与流
description: TriggerFlow 内的 emit、when 与 runtime stream。
keywords: Agently, TriggerFlow, emit, when, runtime stream, async_put_into_stream
---

# 事件与流

> 语言：[English](../../en/triggerflow/events-and-streams.md) · **中文**

TriggerFlow 这里讨论两条和 flow 执行直接相关的通道。**不要混淆**。

| 通道 | flow 内部 | flow 外部 |
|---|---|---|
| **emit / when** | chunk emit 一个事件，挂在 `when(event)` 上的 chunk 被触发 | 外部代码也可在 execution 还 `open` 时 `execution.async_emit(...)` |
| **runtime stream** | chunk 通过 `put_into_stream(...)` 推 item | 外部通过 `execution.get_async_runtime_stream(...)` 消费给 UI / SSE / 日志 |

`emit` 是图内的控制流。`runtime stream` 是把数据推到外部。

## emit / when —— 控制流

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="emit-when")

    async def prepare(data: TriggerFlowRuntimeData):
        await data.async_set_state("flag", "ready")
        await data.async_emit("Prepared", {"flag": "ready"})

    async def route(data: TriggerFlowRuntimeData):
        await data.async_set_state("when_payload", data.input)

    flow.to(prepare)
    flow.when("Prepared").to(route)

    snapshot = await flow.async_start(None)
    print(snapshot["when_payload"])  # {'flag': 'ready'}


asyncio.run(main())
```

机制：

- `data.async_emit(event, payload)` 触发事件。payload 成为 `when(event)` 后续 handler 的 `data.input`。
- `flow.when("Event").to(handler)` 声明挂在该事件上的分支。
- `data.emit_nowait(event, payload)` 是 fire-and-forget 同步版本 —— chunk 不等被触发的 handler 跑完就返回。
- 多个 `when("Event")` 分支会同时触发。

### Definition 安全 vs runtime signal

TriggerFlow 的 module-safe definition 工作解决的是服务模块被 import、reload 或重复组装时，不要把同一条图边或同一个生成的 `when(...)` gate 声明两遍。
它不是 runtime signal 去重。

在一次 execution 中，每一次 `emit` / `emit_nowait` 调用仍然是一次业务事件。
如果某个 chunk 发三次 `Tick`，`when("Tick")` 就应该响应三次。这正是
`emit_nowait(...)` + `when(...)` 能支撑动态 To-Do executor、依赖 join、side branch 和 reflection loop 的原因。

多依赖 join 使用：

```python
flow.when(["done:a", "done:b"], mode="and").to(continue_after_both)
```

join 状态属于单个 execution，不能跨 execution 泄漏，也不应放进共享 flow data。

chunk 内部 emit 的信号会携带 parent signal id，并继承当前 aggregation scope。
这样 `batch`、`for_each` 以及 chunk 内部 fan-out 产生的信号，在
`when(..., mode="and")` join 时会保留同一组关联。没有共同 runtime scope 的外部
emit 是彼此独立的业务事件；如果 host 需要把外部提交的 `A` / `B` 事件按同一个业务对象
join，应让它们经过同一个有 scope 的 flow stage，或在 payload 中携带显式
correlation key 并据此分支。

### 外部 emit

execution 还 `open` 时外部也可 emit：

```python
await execution.async_emit("UserClicked", {"id": 42})
execution.emit_nowait("UserClicked", {"id": 42})
```

`seal()` 或 `close()` 后外部 emit 被拒。

## Runtime stream —— 数据出

```python
async def main():
    flow = TriggerFlow(name="runtime-stream")

    async def stream_steps(data: TriggerFlowRuntimeData):
        await data.async_put_into_stream("step-1")
        await data.async_put_into_stream("step-2")
        await data.async_set_state("done", True)

    flow.to(stream_steps)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")

    close_task = asyncio.create_task(execution.async_close())
    items = [item async for item in execution.get_async_runtime_stream(timeout=None)]
    snapshot = await close_task

    print(items)        # ['step-1', 'step-2']
    print(snapshot)     # {'done': True}
```

机制：

- `data.async_put_into_stream(item)` 往该 execution 的 stream 推一个 item。
- `data.put_into_stream(item)` 是同步版。
- `execution.get_async_runtime_stream(timeout=...)` 按到达顺序产出 item。execution close 时 stream 也关。
- 同步消费：`execution.get_runtime_stream(timeout=...)`。
- TriggerFlow 也会写入 interrupt 和 runtime intervention 的 fail-open system item。只关心业务 stream item 的 consumer 应忽略未知 `type`。

### Stream timeout vs auto-close timeout

两者独立：

| Timeout | 控制 |
|---|---|
| `get_async_runtime_stream(timeout=N)` | 消费者等下一 item 多久后抛/停 |
| execution 上的 `auto_close_timeout` | execution 空闲多久后自动 close |

stream timeout 设 `None` 意味着消费者等到 stream 真正关（即 `close()` 完成）才停。收集所有 item 时通常这么用。

## 隐式 stream 语法糖

`flow.get_async_runtime_stream(...)` 与 `flow.get_runtime_stream(...)` 在内部建一个隐式 execution 并 stream。和 `flow.start()` 一样，仅适用于自闭合 flow（无 `pause_for`、无外部 `emit`）。如果隐式 stream execution 走到 `pause_for(...)`，TriggerFlow 会 fail fast，因为外部没有可恢复 execution handle；需要等待/恢复时应创建显式 execution，再调用 `execution.get_async_runtime_stream(...)`。

## 不要把 live item 放进 state

大或 live 的 item 走 runtime stream，不进 state。state 是给 close snapshot 用的 —— 应该小且可序列化。`put_into_stream` 让消费者一来就处理，不撑大 snapshot。

## Observation events 不属于这条控制流

Agently 还会通过 Event Center 发出 **observation event**（观测事件），例如 TriggerFlow 生命周期、Session 应用、观察日志等。那是框架级观测通道，不是 `emit` / `when` 控制流，也不是 runtime stream 数据流。见 [Event Center](../observability/event-center.md)。

## 另见

- [模式](patterns.md) —— `when` 是几个流控原语之一
- [Pause 与 Resume](pause-and-resume.md) —— `continue_with(interrupt_id, payload)` 是恢复路径，与 `emit` 分开
- [Runtime Intervention](runtime-intervention.md) —— 在安全边界插入补充上下文
- [Lifecycle](lifecycle.md) —— `close()` 对 runtime stream 做了什么
- [Event Center](../observability/event-center.md) —— 框架级 observation event
