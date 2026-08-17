---
title: Sub-Flow
description: 用 to_sub_flow + capture + write_back 组合 flow。
keywords: Agently, TriggerFlow, sub_flow, to_sub_flow, capture, write_back, 组合
---

# Sub-Flow

> 语言：[English](../../en/triggerflow/sub-flow.md) · **中文**

`to_sub_flow(child_flow, ...)` 让父 flow 把子 flow 当作单个 chunk 嵌入。子流跑到自己的 close，父流继续。

## 普通组合

```python
parent.to(prepare).to_sub_flow(child_flow).to(consume)
```

不带 `capture` / `write_back` 时桥做最简单的事：

- 子流以父的当前 `data.input` 作为**它的** start input。
- 子流 close 后，父在 `consume` 处的 `data.input` 是子流的 close snapshot。
- 子流通过 deprecated `set_result()` 或 `.end()` 写了兼容结果时，父收到的是该兼容值，而非 snapshot。（见 [兼容](compatibility.md)。）

## capture —— 选父 → 子

`capture` 把父的值映射到子的 input 与 runtime resource：

```python
parent.to(prepare_request).to_sub_flow(
    child_flow,
    capture={
        "input": "value",                       # 子 start input = 父当前 data.input
        "resources": {"logger": "resources.logger"},
    },
)
```

常用 `capture` 路径：

| 路径 | 解析为 |
|---|---|
| `"value"` | 父当前 `data.input` |
| `"state.<key>"` | 父 state 中的值 |
| `"resources.<name>"` | 父的 runtime resource |

右列按左列 key 映射到子的 input 或 resource。

对 value、state 与 `flow_data` 的 capture 会生成隔离副本。
`resources -> resources` capture 则按对象身份传递同一个 live object，不会
deep-copy client、callback、lock、event 或其他 live handle。暂停的子流程恢复时，
该绑定会解析父 execution 当前重新注入的 resource，不会从 snapshot 保留旧对象。
TriggerFlow 创建隔离的子 flow 实例时，也会按对象身份继承 child template 上已有的
flow-level `runtime_resources`。

## write_back —— 子结果 → 父

`write_back` 把子的最终结果映回父：

```python
parent.to(prepare).to_sub_flow(
    child_flow,
    capture={"input": "value"},
    write_back={"value": "result.report"},
).to(finalize)
```

`write_back` 解析规则：

| `write_back` 值 | 来源优先级 |
|---|---|
| `"result"` | 子兼容结果（如有），否则 close snapshot |
| `"result.<path>"` | 先在子兼容结果按该路径找；找不到则在 close snapshot 同路径找 |
| `"snapshot"` | 直接 close snapshot（跳过兼容结果） |
| `"snapshot.<path>"` | snapshot 内路径 |

左侧 `value` key 把解析值放回父的 `data.input` 给下一 chunk。其他 key（`state.<name>`）写入父 state。

这就是 `result.<path>` 同时支持遗留兼容结果风格的子流与新 state-first 子流的原因 —— 查找先试兼容，再回退 snapshot。

## 完整例子

```python
def build_child_flow():
    child = TriggerFlow(name="child")
    (
        child.if_condition(has_multiple_sections)
            .to(use_multi_section_mode)
        .else_condition()
            .to(use_single_section_mode)
        .end_condition()
        .to(list_sections)
        .for_each()
            .to(draft_section)
        .end_for_each()
        .to(summarize_child_report)
    )
    return child


def build_parent_flow():
    parent = TriggerFlow(name="parent")
    parent.update_runtime_resources(logger=SimpleLogger())
    parent.to(prepare_request).to_sub_flow(
        build_child_flow(),
        capture={
            "input": "value",
            "resources": {"logger": "resources.logger"},
        },
        write_back={
            "value": "result.report",
        },
    ).to(finalize_request)
    return parent
```

发生了什么：

1. `prepare_request` 返回 request context。
2. `to_sub_flow(...)` 用该 context 作子的 `data.input` 启动子流，父的 `logger` 资源被转发。
3. 子流分支、`for_each` fan-out、起草各 section、汇总，把结果写到自己的 `state["report"]`。
4. 桥解析 `write_back={"value": "result.report"}`：先在子任何 compat result 里找 `report`，再到子 close snapshot，找到就赋给父的下一 `data.input`。
5. 父的 `finalize_request` 用该 `data.input` 跑。

## stream item 跨子流边界

子流内 `data.async_put_into_stream(...)` 推的 item 出现在**父 execution** 的 runtime stream。从外部消费者看子流像是同一个 execution 的一部分。

## 按 frame id 控制运行中的子流

`capture` 和 `write_back` 是边界绑定，不是实时绑定：`capture` 在子流启动时
复制选定的可序列化父值，并按对象身份传递选中的 live resources；`write_back`
只在子流成功完成后执行。host 如需检查、发送信号或取消运行中的子流，应保留
显式父 execution handle，并使用 sub-flow frame：

```python
execution = parent_flow.create_execution(auto_close=False)
start_task = asyncio.create_task(execution.async_start(input_value))

# host 观察到该 execution 的 triggerflow.sub_flow_started 事件后：
frames = execution.get_sub_flow_frames()
frame_id = next(
    frame_id
    for frame_id, frame in frames.items()
    if frame["status"] == "running"
)

await execution.async_emit_to_sub_flow(
    frame_id,
    "StopRequested",
    {"reason": "superseded"},
)

cancelled = await execution.async_cancel_sub_flow(
    frame_id,
    reason="superseded",
)
await start_task
```

同步对应方法是 `emit_to_sub_flow(...)` 和 `cancel_sub_flow(...)`。

可以运行 [`examples/trigger_flow/active_sub_flow_control.py`](../../../examples/trigger_flow/active_sub_flow_control.py)
查看可确定复现的取消与 fencing 示例。

只有当前调用赢得 active/waiting frame 的取消转换时，
`async_cancel_sub_flow(...)` 才返回 `True`；过晚或重复取消返回 `False`。
取消会让 frame 从 `cancel_requested` 进入 `cancelled`，取消框架管理的协作式
进程内子任务，并阻止 child `write_back` 和父下游 continuation。它**不会**
关闭父 execution，因此父 execution 仍能接收后续事件。

`async_emit_to_sub_flow(...)` 通过 child execution 的正常 signal net 转发信号。
child concurrency 预算仍然生效：控制 handler 如需与长时间运行的 child work
并行执行，应为 sub-flow 配置足够的 `concurrency`。信号转发只是 best-effort
控制手段；不可逆操作仍应以显式取消/fence，或应用、provider 自己的幂等/fence
为正确性边界。若取消在信号转发期间获胜，转发调用会抛出 `RuntimeError`，对应
signal handler 会被协作式取消。

frame 可观察状态包括 `running`、`waiting`、`cancel_requested`、`cancelled`、
`failed` 和 `completed`。运行中 frame 的元数据可以序列化用于审计，但 live
execution 和 task 不可序列化。包含 `running` 或 `cancel_requested` frame 的
snapshot 在 load 时会 fail closed；保存可重启恢复的 snapshot 前，应先让 active
child 完成或取消。既有 `waiting` frame 仍通过投影到 root 的 interrupt 恢复。

框架取消无法物理撤回已经提交的远端模型请求、线程、子进程或外部副作用；这些
边界仍需要 provider abort、幂等或持久 fence 语义。

## 子流 pause 会投影到父 execution

如果子流调用 `pause_for(...)`，父 execution 也会进入 waiting。外部系统仍只管理父 execution id 和父 interrupt id：

```python
execution = parent_flow.create_execution(auto_close=False)
await execution.async_start(input_value)

root_interrupt_id = next(iter(execution.get_pending_interrupts()))
saved = execution.save()

restored = parent_flow.create_execution(auto_close=False)
await restored.async_load(saved, runtime_resources={...})
await restored.async_continue_with(
    root_interrupt_id,
    {"approved": True},
    resume_request_id="approval-webhook-42",
)
```

投影出来的 interrupt 会带 `sub_flow_frame_id` 与 `local_interrupt_id` 便于调试，但调用方应把父 interrupt id 当作公开 handle。子流完成后，`write_back` 正常执行，父 flow 继续下游。

预编排文档审批闸门例子见 `examples/step_by_step/11-triggerflow-20_document_review_subflow_pause_resume.py`：子流包含明确的 pause chunk，并通过 `when("LegalApprovalSubmitted")` 等待审批事件；父 execution 仍然只暴露投影后的 root interrupt，用它保存、加载、恢复。

## 何时用子流

- 子可复用 —— 多个父 flow 用，或独立用。
- 子有清晰契约（input + result），适合独立测试。
- 想保持父 flow 短而可读。

## 何时**不**用子流

- 子只一两个 chunk。直接内联。
- 仅当作共享 state 的方式。用父函数或 `runtime_resources`。
- 想在父子之间共享 runtime stream 过滤。关注点应分离。

## 另见

- [模式](patterns.md) —— `for_each`、`if_condition`、`match`
- [State 与 Resources](state-and-resources.md) —— `runtime_resources` 通过 `capture` 如何传给子
- [兼容](compatibility.md) —— 为何 `result.<path>` 回退到 snapshot
