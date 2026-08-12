---
title: Async First
description: 什么场景下 async 应该是默认路径，以及该用哪些 API。
keywords: Agently, async, async_get, get_async_generator, async_start
---

# Async First

> 语言：[English](../../en/start/async-first.md) · **中文**

Agently 在运行时层是 async-native。内部兼容方法通过 Agently-Stage 的
`StageCallBridge` 跨越同步/异步边界；只有拥有调度生命周期的运行时才显式请求
managed settlement。Deprecated `FunctionShifter.syncify()` / `asyncify()` 现在
委托给有独立作用域的 `Stage.as_sync()` / `Stage.as_async()`。一旦做真实服务，
async 仍应是默认路径。

## 什么时候 sync 也行

- 一次性脚本、Notebook、教学示例。
- 不和别的代码共享同一个事件循环。

当接口不由调用者控制时，同步兼容同样合理。例如工具提供方可以有意提供同步方法，
即使底层 SDK 是异步的：

```python
from agently_stage import Stage


def search(query: str):
    with Stage() as stage:
        return stage.get(search_tool.search, query)
```

Agently-Stage 0.3.6 会自动复用或选择物理上安全的 carrier；这个方法即使运行在同步
TriggerFlow chunk 内也无需知道 TriggerFlow 底层同样使用 Stage，返回后仍可继续调用
`data.set_state(...)` 等同步 execution-data 方法。这个边界会同步阻塞所在 worker；
如果外层 async API 也由你控制，仍优先直接 `await` 和 Agently 原生 async 方法。
绑定在调用方 loop 上的对象不能安全迁移到其他 carrier，应在其 owner loop 上 await。

## 什么时候 async 是默认

- 在 FastAPI、ASGI worker、SSE / WebSocket 处理器，或任何已经在 `asyncio` 里跑的代码。
- 流式 UI——希望字段 delta 先反应到界面，而不是等整个响应。
- 把模型输出和 TriggerFlow 事件、runtime stream 或外部 pubsub 结合起来。

## 先分析依赖，再选择执行形态

构建复杂 AI 服务或脚本时，不要从「全部串行」的循环起步。先画出业务阶段并标记：

- 必须串行的真实数据依赖或顺序约束；
- 可以并发执行的独立分支；
- 哪些可取消或幂等的准备工作可以安全使用 provisional 结构化进度；
- 哪些副作用和外部系统带来安全或容量限制。

可重叠的工作使用 Agently async API。结构化字段需要渐进到达 UI 或其他消费者时
使用 `instant`，但持久化写入或业务决策仍以 `async_get_data()` 返回的最终解析对象
并完成已配置校验后为准。`instant` 更新是 provisional，retry 可能使其失效；它们
只能驱动 UI 状态或明确可取消/幂等的准备工作，不能直接驱动不可逆副作用。应用拥有
的 fan-out、join 和依赖关系应通过 TriggerFlow 的 `batch(...)`、
`for_each(...)`，或信号驱动的 `when(...)` + `async_emit(...)` /
`async_emit_nowait(...)` 表达，让流程关系在图中可见。

只有真实依赖、顺序保证、副作用安全规则或外部容量限制要求时，才应使用串行。
完全不做这项依赖分析就直接选择串行，是反模式。

## 暴露压力控制参数

生产服务应在真正拥有压力边界的层级暴露有界参数：

| 压力边界 | 控制方式 |
|---|---|
| 服务入口 | 最大活跃 execution/协程数与有界队列 |
| 单个 TriggerFlow execution | `create_execution(concurrency=N)` 或 `execution.set_concurrency(N)` |
| 单个 fan-out operator | `batch(..., concurrency=N)` 或 `for_each(concurrency=N)` |
| 模型 provider | `model_request.scheduler.max_concurrency`、`model_request.scheduler.rate_per_second` 与 `model_request.scheduler.providers.<provider>` override |
| 阻塞 I/O SDK | 宿主拥有的 thread-pool 数量与队列上限 |
| CPU-bound 工作 | 宿主拥有的 process-pool/worker 数量与队列上限 |

有效吞吐取决于上述所有层级以及它们保护的下游系统。TriggerFlow 没有一个通用的
「线程数」设置；阻塞工作需要与 event loop 隔离时，线程池和进程池由应用宿主负责。

## 推荐组合

最值得先掌握的组合：

- `result.get_async_generator(type="instant")`——逐字段流出带 `path`、`delta`、`value`、`is_complete` 的结构化 `StreamingData` patch。
- `data.async_emit(...)`——把节点变成 TriggerFlow 信号。
- `data.async_put_into_stream(...)`——把中间状态推给 UI / SSE / 日志。

`instant` 是字段级事件，不是原始 provider token。它可以在字段还在增长时通过
`.delta` 提供部分字段文本，然后在 `.is_complete` 为 true 时发完成事件。把这些
事件当作渐进式 UI 状态；最终可靠对象在结束后用 `async_get_data()` 读取。
这类 stream handler 的入参类型可直接用 `agently` 根入口的 `StreamingData`
标注；需要完整 typed data 命名空间时也可以继续从 `agently.types.data` 导入。

## API 对照

| Sync | Async 等价 |
|---|---|
| `agent.start()` / `request.start()` | `agent.async_start()` / `request.async_start()` |
| `result.get_data()` | `result.async_get_data()` |
| `result.get_text()` | `result.async_get_text()` |
| `result.get_meta()` | `result.async_get_meta()` |
| `result.get_generator(type=...)` | `result.get_async_generator(type=...)` |
| `flow.start()` | `flow.async_start()` |
| `execution.start()` / `execution.close()` | `execution.async_start()` / `execution.async_close()` |
| `data.set_state(...)` / `data.emit(...)` | `data.async_set_state(...)` / `data.async_emit(...)` |
| `agent.add_chat_history(...)` | `await agent.async_add_chat_history(...)` |

## 最小 async 示例

```python
import asyncio
from agently import Agently

agent = Agently.create_agent()


async def main():
    result = (
        agent
        .input("给我一个标题和两条要点。")
        .output({
            "title": (str, "标题", True),
            "items": [(str, "要点", True)],
        })
        .get_result()
    )

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            print(item.path, "+", item.delta)
        if item.is_complete:
            print(item.path, "done")

    final = await result.async_get_data()
    print(final)


asyncio.run(main())
```

`get_result()` 返回一个可复用的 `ModelRequestResult`。你可以从同一个 result 拿 text、结构化 data 和 metadata，不会重发请求——见 [模型结果](../requests/model-response.md)。

## Async + TriggerFlow

事件驱动编排时优先用：

- `flow.async_start(...)`——调用方只需要 close snapshot 的有限、自闭合运行；有界
  async request handler 也可以使用。
- `flow.async_start_execution(...)`——显式启动长生命周期 execution，由你自己控制。
- chunk 内部使用 `data.async_emit(...)` 与 `data.async_put_into_stream(...)`。

详见 [TriggerFlow Lifecycle](../triggerflow/lifecycle.md)。

宿主需要 execution handle 来完成 pause/resume、外部事件、save/load、intervention、
inspection、cancellation、runtime-stream 断连处理或控制 close 时机时，应使用显式
execution，而不是 hidden sugar。

## 不要过度宣传 async

Async First 改善的是并发性、服务组合质量和渐进式 UX。它**不会**让单次请求的模型延迟变低——单次请求的墙钟时延由模型决定，与 sync/async 无关。
