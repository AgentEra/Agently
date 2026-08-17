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
| 进 execution snapshot | 是 | **是，作为一份 flow-shared 副本** | 否，`load()` 后必须重新注入 |
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

读取 state 是本地同步操作。Async chunk 必须 await 对应的 async 写入、追加、删除、
emit 与 stream 方法，让 execution 留在 owner loop。Sync facade 仍可在其他上下文
兼容使用，但会阻塞调用线程，主要面向 sync chunk 与 sync caller。

同步 chunk 可以调用工具提供方的同步 wrapper；wrapper 内部即使使用 `with Stage()`
等待异步 SDK，返回后也可以继续调用 `data.set_state(...)`。Agently-Stage 0.3.8 会
自动识别外层物理运行环境；工具提供方无需发现 TriggerFlow 私有使用了 Stage，也无需
把自己的公开同步方法强制改成异步方法。

`set_state(...)` 表示完整替换；list、mapping、set 和空集合都不会与旧值合并。只有
确实需要 list 累加时才使用 `append_state(...)`。mapping 状态迁移应先构造完整的新
mapping，再一次 set，旧 key 不会被保留。单次 set 的本地修改会在事件通知派发前完成，
但跨多次调用的 read-modify-write 不是 compare-and-swap；并发写入仍需要宿主负责协调。

`close()` 时 state 里的内容就是 close snapshot。

## flow_data —— 风险共享

`flow_data` 在同一个 flow 的**每个** execution 之间共享。听起来方便，直到出现：

- 两个 execution 并行 —— 互相覆盖。
- save/load —— `save()` 会捕获当时的 flow-shared 值，`load()` 会替换目标 flow
  对象当前的共享值，因此恢复一个 execution 可能影响同一 flow 上的其他 execution。
- 分布式调度 —— 值只在加载该 flow 的进程上。

因此每次调用都发 `RuntimeWarning`：

```python
flow.set_flow_data("counter", 0)            # RuntimeWarning
flow.set_flow_data("counter", 0, no_warning=True)   # 静默
```

如果你确实想要共享（只读配置、所有 execution 有意共享的长跑 cache），传 `no_warning=True`。execution-local 的数据 —— 99% 的代码 —— 用 `state`。

为兼容性，execution save snapshot 会序列化 `flow_data`，但它不是 execution-local
snapshot。加载该 snapshot 时会清空并恢复所属 flow 对象的共享 `flow_data`，可能覆盖
更新后的值或干扰并发 execution；save/load 不会为它增加隔离、CAS 或 merge 语义。
不要把 `flow_data` 当作恢复边界。单次运行的数据放 execution `state`；需要持久共享的
数据放入具有相应一致性策略的宿主或 RecordStore provider。

API（不传 `no_warning=True` 都发 warning）：

- `flow.get_flow_data(key)` / `flow.set_flow_data(key, value)` / `flow.append_flow_data(...)` / `flow.del_flow_data(...)`
- async 等价加 `async_` 前缀

`set_flow_data(...)` 同样完整替换目标值，append 仍是独立的显式操作；这不会让共享
scope 获得并发安全。

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

close snapshot 应当是可序列化的 dict。live 对象不能序列化（没有有意义的表示，也没法在另一边重建 live 状态）。snapshot **会**记录 `resource_keys` 与 `resource_requirements` —— 恢复所需的 resource identity：

```python
flow.declare_resource_requirement("db")
flow.declare_resource_requirement("logger")
flow.declare_resource_requirement("search_tool")

saved = execution.save()
# saved 含 state、lifecycle metadata、interrupt state、
# resource requirements 和 resource keys，但不含 live 对象本体

restored = flow.create_execution(auto_close=False)
await restored.async_load(
    saved,
    runtime_resources={"db": new_db_client, "logger": new_logger, "search_tool": search_function},
)
```

调用方负责在 load 时重新注入所需 resource。所需 resource 已经在当前进程里可用时使用
`load(saved)`；重启和 worker handoff 路径使用 `async_load(...)`，这样缺失资源会在
execution 继续前失败。

对分布式 pause/resume 来说，resource 如果自己带状态，重新注入还不够。重新创建一个
HTTP client 可以和旧对象等价，但 browser page、sandbox process、remote task 或
exchange session 可能需要 provider-owned state ref、version、lease 或 fence token。
这些 ref 应进入 execution state 或 resource requirements，并由外部系统在 TriggerFlow
继续前恢复和校验 live object。

在每个 worker 都能导入同一个 factory 的服务部署中，可以声明 importable resolver
descriptor，让 `async_load(...)` 重建 live object：

```python
flow.declare_resource_requirement(
    "db",
    resolver="my_app.resources:create_db",
    provider_kind="database",
    config_ref="settings://db",
    secret_ref="secret://db",
)
```

resolver 会收到 context dict，并返回 live object 或
`{"resource": object, "health": "healthy"}`。缺失、unhealthy 和
policy-forbidden resource 会出现在 `inspect_load(...)` diagnostics 中；
`fail_policy="fail_open"` 会把阻断型 resolver 问题降级为 warning，默认
`fail_closed` 会阻断严格 load。

### 托管 execution resources

当你向 `flow.create_execution(...)`、`flow.start_execution(...)` 或
`flow.async_start(...)` 传入 `execution_resources=[...]` 时，
`runtime_resources` 也可以接收来自 `Agently.execution_resource` 的托管资源。

chunk 内仍然通过 `data.require_resource(...)` 读取。差异在 ownership：
ExecutionResourceManager 负责启动/复用资源，并在 execution close 时释放。
手动传入的 `runtime_resources={...}` 仍是 unmanaged。

## 决策表

| 你存的是 | 用 |
|---|---|
| 数字、字符串、dict、list 或其他 JSON 友好的、希望进 snapshot 的值 | `state` |
| pydantic 模型、dataclass，或任何可序列化为 dict 的 | `state` |
| DB client、HTTP client、websocket | `runtime_resources` |
| 函数或回调 | `runtime_resources` |
| 跨 execution 共享、有意全局的内存 cache | flow 级 `runtime_resources`（注意进程重启需要重注入，或把 cache state 外部化） |
| 必须跨 worker handoff 存活的有状态 session | `runtime_resources` 加 durable external state ref 和 resolver/provider validation |
| 跨 execution 共享、有意全局的配置 | `flow_data`（带 `no_warning=True`），或 `runtime_resources`（不可序列化时） |

## 常见错误

- **把 SDK client 放进 state**：要么序列化失败，要么悄悄抓了一份过期 snapshot。用 `runtime_resources`。
- **把单 execution 业务数据放进 `flow_data`**：两个并发 execution 互相覆盖。用 `state`。
- **`load()` 后忘记重新注入 `runtime_resources`**：execution 在 `require_resource(...)` 处崩。snapshot 里有 `resource_keys` —— 写一段不会漂移的重注入逻辑。
- **因为 resource key 存在就认为有状态 resource 已恢复**：key 存在只证明当前进程挂了 live object。这个 object 携带的状态仍需要外部系统恢复和校验。

## 另见

- [Lifecycle](lifecycle.md) —— `close()` 返回什么
- [ExecutionResource](../actions/execution-environment.md) —— 托管 live resource 生命周期
- [持久化与 Blueprint](persistence-and-blueprint.md) —— `save` / `load` 语义
- [分布式 Pause 与 Resume 边界](distributed-pause-resume.md) —— 宿主管理恢复和 live object ownership
- [兼容](compatibility.md) —— `runtime_data` 是 `state` 的 deprecated 别名
