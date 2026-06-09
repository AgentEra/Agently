---
title: Pause 与 Resume
description: 用 pause_for 暂停 chunk 等待人工 / 外部事件，用 continue_with 恢复。
keywords: Agently, TriggerFlow, pause_for, continue_with, interrupt, human-in-the-loop
---

# Pause 与 Resume

> 语言：[English](../../en/triggerflow/pause-and-resume.md) · **中文**

`pause_for(...)` 让 chunk 停在可持久化的 interrupt barrier，把控制权交回框架等待外部事件。execution 保持 alive 但空闲。pause_for 期间 auto-close 暂停。外部调 `continue_with(...)` 后，TriggerFlow 按该 interrupt 的恢复目标继续图。

## 用 pause_for 挂起

```python
async def ask(data: TriggerFlowRuntimeData):
    return await data.async_pause_for(
        type="human_input",
        payload={"question": f"批准 {data.input} 的操作？"},
        resume_to="next",
    )
```

`pause_for` 做：

- 记录一个唯一 id 的 interrupt。
- 暂停该 execution 的 auto-close 计时。
- 返回框架。可持久化恢复依赖图目标，不依赖 Python 协程栈保存。
- interrupt 通过 `execution.get_pending_interrupts()` 暴露。
- 外部 `continue_with(interrupt_id, payload)` 后按 `resume_to` 继续图。

| 参数 | 含义 |
|---|---|
| `type=` | 字符串标签（如 `"human_input"`、`"approval"`、`"webhook"`）。应用据此决定如何呈现 interrupt。 |
| `payload=` | 给负责恢复方的结构化细节（UI 渲染问题、webhook 接收方等）。 |
| `resume_to=` | 可选恢复目标：`"next"`、`"self"` 或 `{"event": "EventName"}`。 |
| `resume_event=` | 兼容快捷方式。未显式设置 `resume_to` 时，`continue_with` 与匹配的 `emit(...)` 会路由到该事件。 |
| `interrupt_id=` | 可选。自己指定 id；否则框架生成。 |
| `max_resumes=` | `resume_to="self"` 的可选护栏。默认 `1`，所以恢复后的 chunk 必须处理 `data.is_resume`，不能再次无限暂停自己。有界 self-retry 传更大的整数；确实需要无界循环时传 `None`，并由应用自己保证退出条件。 |

## 用 continue_with 恢复

```python
interrupt_id = next(iter(execution.get_pending_interrupts()))
await execution.async_continue_with(interrupt_id, {"approved": True})
```

使用 `resume_to="next"` 时，payload 成为暂停 chunk 的输出，下一段 `.to(...)` 收到它。

使用 `resume_to="self"` 时，同一个 chunk 会再次运行。用 `data.is_resume` 与 `data.resume.value` 读取恢复上下文：

```python
async def gate(data: TriggerFlowRuntimeData):
    if data.is_resume:
        return {"decision": data.resume.value}
    return await data.async_pause_for(
        type="approval",
        payload={"question": "批准？"},
        resume_to="self",
    )
```

`resume_to="self"` 会在 interrupt ledger 和 signal metadata 中携带
`resume_count`。默认同一个 signal 只能重放一次；如果恢复后的 chunk 没处理
`data.is_resume`，又再次调用 `pause_for(..., resume_to="self")`，TriggerFlow
会以 self-resume limit error 失败，而不是构造无界 interrupt 循环。

使用 `resume_to={"event": "ApprovalGiven"}` 时，TriggerFlow 用恢复 payload 发出该事件。`resume_event="ApprovalGiven"` 保留旧的事件式恢复行为。

## 完整例子

```python
import asyncio
from agently import TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="approval")

    async def ask(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            payload={"question": f"批准工单 {data.input} 退款？"},
            resume_to="next",
        )

    async def commit(data: TriggerFlowRuntimeData):
        await data.async_set_state("decision", data.input)

    flow.to(ask).to(commit)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("T-001")

    # 真实系统里 UI / webhook 后续调 continue_with。
    # 这里在同一协程里恢复仅作 demo。
    interrupt_id = next(iter(execution.get_pending_interrupts()))
    await execution.async_continue_with(interrupt_id, {"approved": True})

    snapshot = await execution.async_close()
    print(snapshot["decision"])  # {'approved': True}


asyncio.run(main())
```

注意：这个 flow 用了 `pause_for(...)`。必须用 `flow.create_execution(...)`（或 `flow.start_execution(...)`），**不要**用 `flow.start(...)` —— 隐式 execution 没有外部可用的 handle 来调 `continue_with`，走到 `pause_for(...)` 时 TriggerFlow 会直接报错。

模型自主决定中断的文档审查例子见 `examples/step_by_step/11-triggerflow-19_document_review_pause_resume.py`：模型拥有的 gate 先判断是否需要人工复核，需要时调用 `pause_for(..., resume_to="self")`，恢复后同一 gate 通过 `data.is_resume` 与 `data.resume` 继续。

## 跨进程重启的 pause

`pause_for(...)` 可以和 checkpoint rehydration 配合：

```python
flow.declare_resource_requirement("approval_service")

execution = flow.create_execution(auto_close=False)
await execution.async_start("topic")
# 此时已碰到 pause_for；存在 pending interrupt

saved = execution.save()
# 持久化 saved

# 后续在另一进程 / worker：
restored = flow.create_execution(auto_close=False)
await restored.async_rehydrate(
    saved,
    runtime_resources={"approval_service": approval_service},
)
interrupt_id = next(iter(restored.get_pending_interrupts()))
await restored.async_continue_with(
    interrupt_id,
    {"approved": True},
    resume_request_id="approval-webhook-42",
)
snapshot = await restored.async_close()
```

interrupt 和已接受的 resume request id 都是 saved state 的一部分，新进程知道有什么待处理，也能忽略重复 resume。详见 [持久化与 Blueprint](persistence-and-blueprint.md)。

## 多个并发 pause

单 execution 可有多个未决 interrupt（如两个并行分支各等人工输入）。`get_pending_interrupts()` 返回全部；`continue_with(id, payload)` 一次解一个。

需要指定 id 时，给 `pause_for(...)` 传 `interrupt_id="my-id"`，`continue_with` 用同 id。

## Pause vs emit

| 模式 | 用途 |
|---|---|
| `pause_for(..., resume_to="next")` + `continue_with` | 下一个图步骤应收到恢复 payload |
| `pause_for(..., resume_to="self")` + `continue_with` | 同一 chunk 应带 `data.resume` 上下文再次运行 |
| `emit` + `when(...)` | 单独的 handler 在事件发生时跑；原 chunk 不必等 |

人工介入用 pause —— chunk 逻辑依赖人工回应。fan-out 副作用用 emit/when。

## auto_close 互动

只要存在未决 `pause_for`，`auto_close=True` 不触发。`continue_with` 解掉最后一个 pending interrupt 后 execution 重新进入空闲，auto-close 计时从零重启。

希望等待时永不 auto-close 用 `auto_close_timeout=None`（记得显式 `close()`）。

`async_close()` 默认拒绝关闭仍有 pending interrupt 的 execution。应先恢复这些 interrupt；如果确实要放弃等待，必须显式取消：

```python
snapshot = await execution.async_close(pending_interrupts="cancel")
```

## 另见

- [Lifecycle](lifecycle.md) —— 恢复后何时 seal/close
- [持久化与 Blueprint](persistence-and-blueprint.md) —— 跨 pause 保存
- [State 与 Resources](state-and-resources.md) —— `load()` 后重新注入 `runtime_resources`
