---
title: State 与 Resources
description: state、flow_data、runtime_resources 三种存储层及其选择。
keywords: Agently, TriggerFlow, state, flow_data, runtime_resources, snapshot, save, load
---

# State 与 Resources

> 语言：[English](../../en/triggerflow/state-and-resources.md) · **中文**

TriggerFlow execution 携带三种独立的存储层。它们看起来类似，但解决不同问题。混淆是常见的隐 bug 来源。

## 三层一览

| | `state` | `flow_data` | `runtime_resources` |
|---|---|---|---|
| Scope | execution-local | flow 共享（所有 execution 之间） | execution-local |
| 可序列化 | 是 | 是 | **否** |
| 进 close snapshot | 是 | 否 | 否，仅记录 `resource_keys` |
| 进 save / load checkpoint | 是 | 否 | 否，`load()` 后必须重新注入 |
| 推荐用途 | 业务 state、中间值、`close()` 想拿到的内容 | 历史兼容 / 显式有意的 flow 范围共享 | live client、socket、callback、文件句柄、cache 引用 |
| 状态 | **推荐主路径** | risky-default —— 每次调用发 `RuntimeWarning` | 新概念 —— 不可序列化的内容都用这个 |

## state —— 主路径

state 是 execution-local、可序列化、可快照的。它构成 close snapshot，并被 `save()` / `load()` 往返。

```python
async def step(data: TriggerFlowRuntimeData):
    await data.async_set_state("greeting", f"hello {data.input}")
    current = data.get_state("greeting")
```

API：

- `data.async_set_state(key, value)` / `data.set_state(key, value)`
- `data.get_state(key, default=None)`
- `data.async_append_state(key, value)` / `data.append_state(key, value)` —— list 类型 state
- `data.async_del_state(key)` / `data.del_state(key)`

读取 state 是本地同步操作；写入、追加、删除有 async 版本，方便在 async chunk 里保持一致。

`close()` 时 state 里的内容就是 close snapshot。

## flow_data —— 风险共享

`flow_data` 在同一个 flow 的**每个** execution 之间共享。听起来方便，直到出现：

- 两个 execution 并行 —— 互相覆盖。
- save/load —— 保存时的值在新进程 load 时不一定还在。
- 分布式调度 —— 值只在加载该 flow 的进程上。

因此每次调用都发 `RuntimeWarning`：

```python
flow.set_flow_data("counter", 0)            # RuntimeWarning
flow.set_flow_data("counter", 0, no_warning=True)   # 静默
```

如果你确实想要共享（只读配置、所有 execution 有意共享的长跑 cache），传 `no_warning=True`。execution-local 的数据 —— 99% 的代码 —— 用 `state`。

API（不传 `no_warning=True` 都发 warning）：

- `flow.get_flow_data(key)` / `flow.set_flow_data(key, value)` / `flow.append_flow_data(...)` / `flow.del_flow_data(...)`
- async 等价加 `async_` 前缀

## runtime_resources —— live 对象

有些东西不能进 state 因为不能序列化：DB client、回调函数、socket、内存 cache、任何带 fd 或 live 网络连接的。这些放进 `runtime_resources`。

execution 创建时注入：

```python
execution = flow.create_execution(
    runtime_resources={
        "db": my_db_client,
        "logger": my_logger,
        "search_tool": search_function,
    },
)
```

或在 flow 上更新（默认作用于该 flow 的所有 execution）：

```python
flow.update_runtime_resources(logger=my_logger)
```

chunk 内：

```python
async def step(data: TriggerFlowRuntimeData):
    logger = data.require_resource("logger")
    logger.info(f"received: {data.input}")
    db = data.require_resource("db")
    rows = await db.fetch("SELECT 1")
```

`require_resource(name)` 在 resource 没注入时抛错 —— 用于 chunk 真依赖该 resource 时。可选场景用 `data.get_resource(name, default=None)`。

### 为什么 resources 不进 snapshot

close snapshot 应当是可序列化的 dict。live 对象不能序列化（没有有意义的表示，也没法在另一边重建 live 状态）。snapshot **会**记录 `resource_keys` 与 `checkpoint.resource_requirements` —— 恢复所需的 resource identity：

```python
flow.declare_resource_requirement("db")
flow.declare_resource_requirement("logger")
flow.declare_resource_requirement("search_tool")

saved = execution.save()
# saved 含 state、lifecycle metadata、interrupt state、
# resource requirements 和 resource keys，但不含 live 对象本体

restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved,
    runtime_resources={"db": new_db_client, "logger": new_logger, "search_tool": search_function},
)
```

调用方负责在 rehydration 时重新注入兼容 resource。`load(saved)` 仍作为低层兼容
API 保留，但重启和 worker handoff 路径推荐使用 `async_rehydrate(...)`，这样缺失
资源会在 execution 继续前失败。

### 托管 execution resources

当你向 `flow.create_execution(...)`、`flow.start_execution(...)` 或
`flow.async_start(...)` 传入 `execution_environments=[...]` 时，
`runtime_resources` 也可以接收来自 `Agently.execution_environment` 的托管资源。

chunk 内仍然通过 `data.require_resource(...)` 读取。差异在 ownership：
Execution Environment Manager 负责启动/复用资源，并在 execution close 时释放。
手动传入的 `runtime_resources={...}` 仍是 unmanaged。

## 决策表

| 你存的是 | 用 |
|---|---|
| 数字、字符串、dict、list 或其他 JSON 友好的、希望进 snapshot 的值 | `state` |
| pydantic 模型、dataclass，或任何可序列化为 dict 的 | `state` |
| DB client、HTTP client、websocket | `runtime_resources` |
| 函数或回调 | `runtime_resources` |
| 跨 execution 共享、有意全局的内存 cache | flow 级 `runtime_resources`（注意进程重启需要重注入） |
| 跨 execution 共享、有意全局的配置 | `flow_data`（带 `no_warning=True`），或 `runtime_resources`（不可序列化时） |

## 常见错误

- **把 SDK client 放进 state**：要么序列化失败，要么悄悄抓了一份过期 snapshot。用 `runtime_resources`。
- **把单 execution 业务数据放进 `flow_data`**：两个并发 execution 互相覆盖。用 `state`。
- **`load()` 后忘记重新注入 `runtime_resources`**：execution 在 `require_resource(...)` 处崩。snapshot 里有 `resource_keys` —— 写一段不会漂移的重注入逻辑。

## 另见

- [Lifecycle](lifecycle.md) —— `close()` 返回什么
- [Execution Environment](../actions/execution-environment.md) —— 托管 live resource 生命周期
- [持久化与 Blueprint](persistence-and-blueprint.md) —— `save` / `load` 语义
- [兼容](compatibility.md) —— `runtime_data` 是 `state` 的 deprecated 别名
