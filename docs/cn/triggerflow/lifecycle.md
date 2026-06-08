---
title: TriggerFlow Lifecycle
description: 三种执行态与 5 个入口 API —— 各自做什么、何时用哪个。
keywords: Agently, TriggerFlow, lifecycle, seal, close, start, execution, auto_close
---

# Lifecycle

> 语言：[English](../../en/triggerflow/lifecycle.md) · **中文**

TriggerFlow execution 有三种状态。5 个入口 API 控制启动与结束方式。

## 三态

```text
   open  ──seal()──►  sealed  ──close()──►  closed
    │                                            │
    └───（auto_close 在空闲超时后自动触发）───────┘
```

| 状态 | 接受什么 | 还在跑什么 |
|---|---|---|
| `open` | 新外部事件（`emit`、`continue_with`） | 全部：chunk、runtime stream、已注册 task |
| `sealed` | 不再接外部 | 已接受事件、内部 `emit` 链、已注册 task 继续 drain |
| `closed` | 不接 | runtime stream 已关闭；close snapshot 已冻结 |

关键区别：`seal()` 停外部输入但让在途工作完成。`close()` 先 seal，再 drain，再冻结。

## 5 个入口 API

| API | 用途 | 返回 |
|---|---|---|
| `flow.start(...)` / `flow.async_start(...)` | 隐式 execution 语法糖；create + start + wait + close | close snapshot |
| `flow.start_execution(...)` / `flow.async_start_execution(...)` | 显式启动；你持有 execution handle | execution |
| `execution.start(...)` / `execution.async_start(...)` | 启动已创建的 execution | `auto_close=True` 返回 close snapshot；`auto_close=False` 返回 execution |
| `execution.seal()` / `execution.async_seal()` | 运行期 seal | — |
| `execution.close()` / `execution.async_close()` | 收尾 | close snapshot |

### `flow.start(...)` —— 隐式语法糖

```python
snapshot = await flow.async_start("input value")
```

内部做：`create_execution(auto_close=True, auto_close_timeout=0.0)`、启动、等到 close、返回 snapshot。

规则：

- **`auto_close=False` 非法** —— 立刻报错。
- `wait_for_result=` 的值被**忽略**并 warn。返回类型固定为 close snapshot。
- `timeout=` 当作 `auto_close_timeout` —— 最后一次活动后多久自动关闭。
- flow 用了 `pause_for(...)` 时**不要**用 `flow.start()` —— 外部没有 handle 来恢复。隐式 execution 走到 `pause_for(...)` 时 TriggerFlow 会 fail fast。改用 `flow.start_execution(...)` 或 `flow.create_execution(...)`。

### `flow.start_execution(...)` —— 显式启动

```python
execution = await flow.async_start_execution("input value")
# ... 用 handle 做事 ...
snapshot = await execution.async_close()
```

返回 execution，你决定何时 close。适合服务、SSE/WebSocket 流、人工介入、外部 `emit()`。

`wait_for_result=` 在这里也被忽略。

### `execution.start(...)` —— 启动已构建的 execution

```python
execution = flow.create_execution(auto_close=True)
snapshot = await execution.async_start("input")  # 返回 close snapshot
```

```python
execution = flow.create_execution(auto_close=False)
exec2 = await execution.async_start("input")  # 返回 execution
# ... 做事，再 ...
snapshot = await execution.async_close()
```

| `auto_close` | `async_start` 返回 |
|---|---|
| `True`（默认） | close snapshot |
| `False` | execution 本身 |

Sync `start()` 仅支持 `auto_close=True`。需要手动 close 时用 `await execution.async_start(...)`。

传给 `execution.async_start(value)` 的值是 execution 的 start input，
不会 emit 一个名为该值的自定义事件。应从 start boundary 开始运行的 chunk
用 `flow.to(handler, name=...)` 挂接。如果确实需要 `"start"` 这类自定义事件，
先启动 execution，再调用 `await execution.async_emit("start", payload)`。

### `execution.seal()` —— 停新输入，让在途完成

```python
await execution.async_seal()
```

seal 后：

- 新外部 `emit()` / `continue_with()` 被拒。
- 已接受事件、内部 `emit` 链、已注册 task 继续。
- runtime stream **不**关。
- close snapshot **不**冻结。

需要「停接新工作但让在途事完成」、稍后再 close（或让 `auto_close` 关）时用 seal。

### `execution.close()` —— 收尾返回 snapshot

```python
snapshot = await execution.async_close()
```

close 顺序：

1. seal（如未 seal）
2. drain 待办 task
3. 关 runtime stream
4. 冻结并返回 close snapshot

close 上的 `timeout=` 是 **drain timeout** —— 在途 task 的最大等待时间，与 auto-close 计时无关。

## auto_close 与 auto_close_timeout

`auto_close=True`（`create_execution` 的默认值）表示 execution 在**空闲** `auto_close_timeout` 秒后自动 close —— 没 chunk 在跑、没事件待处理、没 pending pause。

| 来源 | 默认 `auto_close_timeout` |
|---|---|
| `flow.create_execution(...)` | `10.0` 秒 |
| `flow.start(...)` / `flow.async_start(...)`（隐式糖） | `0.0` 秒（一空闲就 close） |

`pause_for(...)` 暂停 auto-close 计时。`continue_with(...)` 后空闲计时重新开始。

`close()` / `async_close()` 默认拒绝关闭仍有 pending interrupt 的 execution。应先恢复这些 interrupt；如果关闭时就是要放弃等待，必须显式传 `pending_interrupts="cancel"`。

close 还会释放 execution-local 的 transient aggregation state，例如未完成的
`when(mode="and")`、`batch`、`collect`、`for_each` 和 `match` bookkeeping。
这些 scratch key 不属于 durable close snapshot。

`auto_close_timeout=None` 关掉 auto-close —— execution 一直存活直到显式 `close()`。**不要把 `auto_close_timeout=None` 与隐式糖一起用** —— `flow.start()` 会永远不返回。

## save/load checkpoint 形态

`execution.save()` 返回可序列化的 execution state 字典。为了支撑可重启和未来
分布式恢复路径，这个字典包含一个 `checkpoint` 分区：

```python
saved = execution.save()
checkpoint = saved["checkpoint"]
```

checkpoint 分区记录：

- `schema_version`：checkpoint schema 版本。
- `durable_system_state`：TriggerFlow 自身需要跨 open/waiting execution
  rehydration 保存的进度，例如未完成的 `when(mode="and")` 聚合状态。
- `resource_requirements`：恢复后继续执行前必须重新注入的 live resource。

live resource 对象不会被序列化。`runtime_resources`、受管
execution-environment handle、client、callback 以及其他 live object 都不进入
saved state。checkpoint 只记录 requirement key；恢复后继续执行前，应通过
`load(..., runtime_resources={...})` 或宿主侧正常 resource provisioning 路径注入新对象。

这只是 execution snapshot contract，不是完整分布式 execution store。生产级
分布式 runner 仍需要围绕这个 saved state 增加存储、lease ownership、幂等 resume、
访问控制和 resource provisioning。

## 选哪个入口

| 场景 | 用 |
|---|---|
| 快速脚本，输入都已知 | `flow.start(...)` / `flow.async_start(...)` |
| 持续 emit / 消费 runtime stream 的服务 | `flow.start_execution(...)` |
| 需要 `pause_for(...)`（人工审批、异步 webhook） | `flow.create_execution(auto_close=False)` + `execution.async_start(...)` + 手动 `close()` |
| 跨重启 save/load | `create_execution(...)` + `execution.save()` / `load()` |

## 决策示例

```python
# 这个 flow 暂停等用户输入 —— 不要用 flow.start()
flow = TriggerFlow(name="approval")
async def ask(data):
    return await data.async_pause_for(type="approval", resume_to="next")
async def commit(data):
    await data.async_set_state("approved", data.input)
flow.to(ask).to(commit)

execution = flow.create_execution(auto_close=False)
await execution.async_start(None)
# ... 等外部系统调 execution.async_continue_with(...) ...
snapshot = await execution.async_close()
```

如果写成 `await flow.async_start(None)`，隐式 execution 没有可恢复 handle，走到 `pause_for(...)` 时会直接报错。

如果需要停止一个等待中的 execution 且不恢复它，必须显式表达取消等待：

```python
snapshot = await execution.async_close(pending_interrupts="cancel")
```

## 兼容参数

| 参数 | 状态 |
|---|---|
| `wait_for_result=True` / `False` | **值被忽略**，发 warning；返回类型由 `auto_close` 决定 |
| `set_result()` / `get_result()` / `.end()` | deprecated；见 [兼容](compatibility.md) |
| `runtime_data`（`get_runtime_data` / `set_runtime_data` 等） | `state` 的 deprecated 别名；见 [State 与 Resources](state-and-resources.md) |

## 另见

- [State 与 Resources](state-and-resources.md) —— 什么进 snapshot
- [Execution Result](execution-result.md) —— 通过统一 facade 读取 snapshot、state、兼容 result 和 metadata
- [Pause 与 Resume](pause-and-resume.md) —— `pause_for` 与 `continue_with`
- [持久化与 Blueprint](persistence-and-blueprint.md) —— `save` / `load`
- [兼容](compatibility.md) —— 从旧 API 迁移
