---
title: TriggerFlow 模式
description: 分支、匹配、batch、for_each、事件驱动循环。
keywords: Agently, TriggerFlow, if_condition, match, batch, for_each, loop
---

# 模式

> 语言：[English](../../en/triggerflow/patterns.md) · **中文**

下面是日常 flow 的常见形态。

## 线性链

```python
flow.to(step_a).to(step_b).to(step_c)
```

每个 handler 收到上一 handler 的返回值作为 `data.input`。

## if / elif / else

```python
async def score(data):
    return {"score": 82}

async def store_grade(data):
    await data.async_set_state("grade", data.input)

(
    flow.to(score)
    .if_condition(lambda data: data.input["score"] >= 90)
        .to(lambda _: "A")
    .elif_condition(lambda data: data.input["score"] >= 80)
        .to(lambda _: "B")
    .else_condition()
        .to(lambda _: "C")
    .end_condition()
    .to(store_grade)
)
```

`end_condition()` 是必需的 —— 关闭条件分支并把链交还给你。被选中的分支返回成为下一 chunk 的 `data.input`。

## match / case

```python
(
    flow.to(lambda _: "medium")
    .match()
        .case("low").to(lambda _: "priority: low")
        .case("medium").to(lambda _: "priority: medium")
        .case("high").to(lambda _: "priority: high")
        .case_else().to(lambda _: "priority: unknown")
    .end_match()
    .to(store_result)
)
```

`match()` 对前一 chunk 的 `data.input` 分发。少量离散值用它；要 predicate 用 `if_condition`。

## when —— 事件 join

```python
flow.when(["task_a_done", "task_b_done"], mode="and").to(run_after_both)
```

list 形式是 `flow.when({"event": [...]}, mode="and")` 的事件专用简写。
开发者拥有的 DAG 依赖推荐使用这个形态，因为依赖边会保留在 TriggerFlow
definition 和 runtime events 里。

## batch —— 并行命名分支

```python
async def echo(data):
    return f"echo: {data.input}"

flow.batch(
    ("a", echo),
    ("b", echo),
    ("c", echo),
).to(store_batch)
```

所有分支并行跑同一份 `data.input`。下一 chunk 收到一个含所有分支输出的 list（或 dict，取决于配置）。

execution 级限并发：

```python
execution = flow.create_execution(concurrency=2)
```

execution concurrency 是该 execution 的全局 handler dispatch 上限，包括 chunk
continuation 和 `data.async_emit(...)` 触发的嵌套 dispatch。handler 等待内部
dispatch 时，TriggerFlow 会临时让出并在返回前重新取得 permit，所以
`concurrency=1` 下普通链式 flow 不会死锁。`batch(..., concurrency=...)`
和 `for_each(..., concurrency=...)` 仍是 operator 局部 fan-out 上限。

## for_each —— 对序列输入 fan-out

```python
async def double(data):
    return data.input * 2

(
    flow.for_each(concurrency=2)
        .to(double)
    .end_for_each()
    .to(store_items)
)

execution = flow.create_execution()
await execution.async_start([1, 2, 3, 4])
# store_items 收到 [2, 4, 6, 8]
```

`for_each` 会检查前一 chunk 的输出（或 start input）：非字符串 `Sequence` 会被拆成多个 item；标量值会被当成单个 item 处理。每个 item 在 `concurrency` 上限内并行跑 body，输出按输入顺序收集成 list。

如果要“按数字 N 循环 N 次”，先在前一 chunk 显式返回一个序列：

```python
async def make_range(data):
    return list(range(data.input))

flow.to(make_range).for_each().to(double).end_for_each()
```

## 事件驱动循环

Python 的 `for` 仍然可以写在 handler 函数内部。图层上的重复 / fan-out 用 `for_each`；需要由 flow 内部信号持续推进的循环，用 `emit` + `when` 表达：

```python
flow = TriggerFlow(name="loop")

async def start_loop(data):
    await data.async_set_state("values", [], emit=False)
    data.emit_nowait("Loop", 0)

async def loop_step(data):
    values = data.get_state("values", []) or []
    values.append(data.input)
    await data.async_set_state("values", values, emit=False)
    if data.input < 3:
        data.emit_nowait("Loop", data.input + 1)
    else:
        await data.async_set_state("done", {"last": data.input, "count": len(values)})

flow.to(start_loop)
flow.when("Loop").to(loop_step)
```

机制：

- chunk emit 循环事件，带下一轮 payload。
- `when(...)` 分支跑后要么再 emit（继续）要么停（退出）。
- 没人 emit 后 execution 自然 drain。

`async_set_state` 传 `emit=False` 表示这次 state 更新不触发观察者，适合热循环里降低观测开销。

长循环给 execution 合理的 `auto_close_timeout`（或 `auto_close=False` + 手动 `close()`），避免迭代间短暂停顿被 auto-close 误关。

## 不阻塞主链的旁路

`when(...)` 分支与主链独立运行，可用于 fire-and-forget log、telemetry、带外通知：

```python
flow.to(main_step)

@flow.when("MainStepDone").to
async def log_step(data):
    await some_external_log(data.input)
```

`main_step` 跑 `data.async_emit("MainStepDone", {...})`，旁路从那里 fan out 不阻塞主返回。

## 组合

单个 flow 经常混用模式。Sub-flow 页有一个 `if_condition` + `for_each` + 子流的完整例子，见 [Sub-Flow](sub-flow.md)。

## 另见

- [事件与流](events-and-streams.md) —— `emit` / `when` 机制
- [Sub-Flow](sub-flow.md) —— `to_sub_flow` 组合
- [Lifecycle](lifecycle.md) —— batched / for-each 何时算「drain 完」
