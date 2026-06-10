---
title: 持久化与 Blueprint
description: execution 状态的 save / load，flow 定义的 save_blueprint / load_blueprint。
keywords: Agently, TriggerFlow, save, load, blueprint, persistence, durable
---

# 持久化与 Blueprint

> 语言：[English](../../en/triggerflow/persistence-and-blueprint.md) · **中文**

两条独立的序列化路径，不要混淆。

| 方法 | 序列化什么 | 典型用途 |
|---|---|---|
| `execution.save()` / `execution.load(saved)` | 一次 **execution** 在某个时刻的运行时 state | 跨进程重启恢复，交给另一 worker |
| `flow.save_blueprint()` / `flow.load_blueprint(blueprint)` | **flow 定义**的结构（chunk、分支、条件） | 把 flow 当配置 artifact 分发或版本控制 |

## Execution save / load

`save()` 捕获可安全重启的 execution snapshot：

- execution 的 `state`
- lifecycle metadata（status、时间戳、run id）
- pending interrupt state（如果碰到了 `pause_for(...)`）
- 带版本的 `checkpoint` envelope，包含 TriggerFlow 系统进度、interrupt
  ledger、resume ledger、resource requirements 与 flow definition
  fingerprint
- `resource_keys` 与 `checkpoint.resource_requirements` —— 恢复时期望的
  resource，但不含 live 值

它**不**捕获：

- live `runtime_resources` 本体（不可序列化；见 [State 与 Resources](state-and-resources.md)）
- 在途 chunk（不存在协程中段；在稳定状态保存）
- 分布式存储所有权。TriggerFlow 记录 lease metadata，但持久化 store 仍负责
  原子的 claim / compare-and-set 行为。

```python
flow.declare_resource_requirement("approval_service")

execution = flow.create_execution(auto_close=False)
await execution.async_start("refund request")

saved_state = execution.save()
# 把 saved_state 持久化到某处（Redis、DB、文件等）
```

后续恢复（可能是另一个进程）：

```python
report = flow.create_execution(auto_close=False).inspect_rehydration(
    saved_state,
    runtime_resources={"approval_service": new_approval_service},
)
if not report["ready"]:
    raise RuntimeError(report["diagnostics"])

restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved_state,
    runtime_resources={"approval_service": new_approval_service},
)

# 继续：emit、continue_with interrupt，再 close。
await restored.async_emit("UserFeedback", {"approved": True})
snapshot = await restored.async_close()
```

flow 定义两端必须一致（或兼容）。`save()` 会记录
`checkpoint.flow_definition_fingerprint`；如果 checkpoint 缺少指纹或指纹与当前
flow 定义不匹配，`inspect_rehydration(...)` 返回 `status="invalid_snapshot"`，
`load(...)` 会拒绝该 snapshot。`load()` 不会从 `saved_state` 重建 chunk 图，
要求 flow 已存在。

`load(saved_state)` 仍作为低层兼容 API 保留。重启或 worker handoff 路径推荐使用
`async_rehydrate(...)`，因为它会在继续运行前校验缺失资源，并可重建 managed
execution environments。

### 跨 pause_for 的恢复

```python
flow.declare_resource_requirement("approval_service")

execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")

# 此时 flow 可能已调 pause_for(...)
saved = execution.save()

# 几天后，另一 worker
restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved,
    runtime_resources={"approval_service": new_approval_service},
)

interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(
    interrupt_id,
    {"approved": True},
    resume_request_id="approval-webhook-42",
)
snapshot = await restored.async_close()
```

`get_pending_interrupts()` 返回 `pause_for(...)` 创建的 interrupt id 集合。`continue_with(id, payload)` 解析一个 interrupt，并按该 interrupt 的 `resume_to` 目标继续图。
Webhook、队列或审批回调应传入稳定的 `resume_request_id`，这样重复投递可以被重放，
但不会把同一次 resume 派发两次。

### Checkpoint stores

`execution.async_save_checkpoint(store, ...)` 会把当前 snapshot 写入任何实现了
`put_checkpoint(run_id, state, *, step_id=None)` 的 store。TriggerFlow 提供
snapshot 契约；生产级 store 负责持久留存、原子 claim、lease enforcement 和冲突处理。

```python
execution.claim_lease("worker-a", lease_ttl=30)
await execution.async_save_checkpoint(store, run_id=execution.id)

saved = await store.get_checkpoint(execution.id)
restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved,
    runtime_resources={"approval_service": approval_service},
)
```

checkpoint envelope 刻意基于 resource key。可序列化的 resource requirements 可以被
持久化和检查，但 client、callback、task、semaphore 与 coroutine frame 必须由恢复端重新创建。

## Flow blueprint save / load

blueprint 序列化 flow 的**结构** —— chunk 引用、分支、条件 —— 但不含 chunk 函数体（仍在代码里）。

```python
def upper(data):
    return str(data.input).upper()

def store(data):
    return data.async_set_state("output", data.input)

source = TriggerFlow(name="source")
source.register_chunk_handler(upper)
source.register_chunk_handler(store)
source.to(upper).to(store)

blueprint = source.save_blueprint()  # dict，可 JSON / YAML 序列化
```

另一端恢复：

```python
restored = TriggerFlow(name="restored")
restored.register_chunk_handler(upper)   # 同名函数体必须可用
restored.register_chunk_handler(store)
restored.load_blueprint(blueprint)
```

关键约束：blueprint 用到的 chunk 必须在恢复端**按相同 handler 名注册**。没 `register_chunk_handler(...)` loader 无法把名映回函数，load 失败。

## 服务化推荐封装

服务代码优先使用这种封装形态：

1. chunks 和 conditions 写成模块顶层 named functions。
2. 把普通 `TriggerFlow(...)` 对象视为 flow definition surface。
3. 稳定 live 依赖用 `flow.update_runtime_resources(...)` 注入。
4. 请求级或租户级依赖用每次 execution 的 `runtime_resources={...}` 注入。
5. 单次请求的业务中间值放 execution `state`，不要放 `flow_data`。

```python
async def analyze(data):
    agent_factory = data.require_resource("agent_factory")
    prompts_path = data.require_resource("prompts_path")
    question = data.input
    data.set_state("question", question)
    agent = agent_factory()
    return agent.load_yaml_prompt(
        prompts_path,
        prompt_key_path="analyze",
        mappings={"question": question},
    ).start()


async def answer(data):
    policy_doc = data.require_resource("policy_doc")
    question = data.get_state("question")
    return f"{policy_doc}\n\nQ: {question}"


def build_policy_flow() -> TriggerFlow:
    flow = TriggerFlow(name="policy")
    flow.update_runtime_resources(
        agent_factory=make_agent,
        prompts_path=PROMPTS_DIR / "policy.yaml",
    )
    flow.to(analyze).to(answer)
    return flow


flow = build_policy_flow()
snapshot = flow.start(
    "travel subsidy?",
    runtime_resources={"policy_doc": tenant_policy_doc},
)
```

这种写法让业务模块尽量轻，同时保留 config / blueprint 兼容性。闭包适合短脚本，但服务化推荐模块顶层 named handlers：更容易测试、注册、导出和重载。

当前行为：TriggerFlow 的 module-safe definition assembly 把 `TriggerFlow(...)`
本身视为规划面，把 `create_execution(...)` / `start_execution(...)` 视为进入一次运行的边界。
没有额外的 `TriggerFlow.define(...)` 模式。服务模块可以安全重放同一段 definition assembly：
named function 保持稳定 stage identity；同一个函数承担两个业务 stage 时，用 `name=...`
显式区分。

对于运行时由模型生成 To-Do List 或依赖图的模型应用，动态图应按 plan 或 request 局部生成。
extract / analyze 这类可复用 sub-flow template 可以放在模块级；per-plan executor 应用 task id
作为动态 stage identity，把 task 结果写入 execution state，并避免修改 main flow definition。

### 何时用 blueprint

- 用 YAML / JSON 配置声明式作 flow 并在启动时 load。
- 把 flow 结构与 handler 代码分开版本管理。
- 把 flow 分发到多个已经有 chunk 实现的 worker。

### 何时**不**用 blueprint

- 一次性脚本。直接 Python 写 flow。
- 与没有 handler 代码的消费者共享。Blueprint 不自包含。

## save vs save_blueprint 对照

```text
Flow 定义（chunk、分支、条件）
        │
        ├── save_blueprint()  →  描述图结构的 dict
        │
        ▼
   create_execution()  ────►  一个 Execution
                                  │
                                  ├── save()  →  描述该 execution 状态的 dict
                                  │
                                  ▼
                              async_close() → close snapshot
```

两条路径都返回 JSON 友好 dict。存储后端（Redis、Postgres、S3、文件）由应用层选 —— 框架不带后端。

## 实用模式

**单服务器恢复**

```python
flow.declare_resource_requirement("approval_service")

saved = execution.save()
redis.set(f"flow:{exec_id}", json.dumps(saved))

# 后续
saved = json.loads(redis.get(f"flow:{exec_id}"))
restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved,
    runtime_resources={"approval_service": approval_service},
)
```

**分布式 worker 拉起**

把 blueprint（存一次）和 execution checkpoint（每个 execution 存一份）配对。
worker rehydrate 并继续运行前，持久化 store 应先原子分配所有权：

```python
blueprint = source_flow.save_blueprint()
db.save("flow_blueprints", blueprint_id, blueprint)

# worker 中
saved = await checkpoint_store.claim(run_id, owner_id=worker_id)

flow = TriggerFlow(name="loaded")
register_all_handlers(flow)            # 你的注册入口
flow.load_blueprint(db.load("flow_blueprints", blueprint_id))

execution = flow.create_execution(auto_close=False)
await execution.async_rehydrate(
    saved,
    runtime_resources=runtime_resources_for(saved),
)
execution.claim_lease(worker_id, lease_ttl=30)
```

## 另见

- [Lifecycle](lifecycle.md) —— 什么算「稳定可保存」的 execution
- [Pause 与 Resume](pause-and-resume.md) —— `pause_for` / `continue_with`，最常见的保存场景
- [State 与 Resources](state-and-resources.md) —— 什么存活、什么必须重新注入
