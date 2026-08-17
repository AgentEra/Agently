---
title: 模型结果
description: 从一次 result 里读 text / data / metadata 与流式事件。
keywords: Agently, result, get_result, get_data, get_text, get_meta, generator, streaming
---

# 模型结果

> 语言：[English](../../en/requests/model-response.md) · **中文**

`agent.input(...).start()` 是便捷写法 —— 创建 `AgentExecution`、执行它并直接返回
解析后的 data。其他更有意思的事（text、metadata、流式、复用、status 或 task
refs）都走 `get_result()`。quick prompt 链返回 `AgentExecutionResult`；直接
`agent.create_request(...).get_result()` 返回 `ModelRequestResult`。
`ModelResponseResult` 不再作为公开 result facade；直接构造 `ModelResponse` 也仍然
deprecated。

## 两种消费方式

```python
# 方式 A：一次性，立即返回 parsed data
result = agent.input("...").output({...}).start()

# 方式 B：拿一个可复用的 result facade
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

非琐碎代码默认走方式 B。模型调用在你第一次从 `result` 消费时**懒触发**，结果**缓存**，后续读不会重发请求。`get_response()` 作为旧代码兼容别名保留，并返回同一个 result facade。

已完成的 `AgentExecution` 是不可变 run record。Agent quick-prompt 写法
`agent.input(...).start()` 每个表达式都会创建新的 execution。只要你显式拿到了
`AgentExecution`，就应该把它当成一次独立执行；它开始后再调用 `input(...)` 或
`output(...)` 等 prompt/config 方法会抛出生命周期错误。下一轮请求应从
`agent.input(...)`、`agent.create_execution(...)` 或
`execution.create_execution(...)` 创建新的 execution。

## 判断一次请求是否足够

ModelRequest 只能从发出时已有的 prompt、settings 和信息快照开始。后续字段只依赖
这个请求时输入/证据快照，以及同一响应里前面已生成的有界字段时，可以把多个语义
步骤合并进一个有序 output contract。这样既能避免重复传递上下文，也能让结论复用
同一次生成中较早产出的、任务相关的结构化分析。

如果后置语义输出所需事实，只有在 Action、tool、API/数据库访问、artifact
readback、approval/resume 或宿主计算执行后才会出现，就必须启动后续
ModelRequest：

```text
R1 检索计划
-> 执行检索并校验观测结果
-> R2 基于证据的回答
```

从同一个缓存 result 读取 text、data 和 metadata 不会重发请求；反过来，复用这个
result 也不能加入请求发出时尚不存在的信息。`instant` 可以根据已经完整生成的前置
字段提前启动可取消、幂等的工作，但不能把该工作稍后返回的结果送回仍在生成的同一次
请求。如果后续模型生成需要这个结果，应先完成最终对账与汇合，再把校验后的观测结果
传给新请求。

## 读取方法

| 方法 | 返回 |
|---|---|
| `result.get_text()` | 完整纯文本 |
| `result.get_data()` | 最终业务数据；用了 `output()` 时返回解析后的结构化 dict |
| `result.get_data_object()` | Pydantic 实例（`output()` 接受 `BaseModel` 时） |
| `result.get_meta()` | usage / model 信息 / 时间等 |

这些通用 reader 都有 async 版本：`async_get_text()`、`async_get_data()`、
`async_get_data_object()`、`async_get_meta()`。

对 `AgentExecutionResult` 来说，`get_data()` 在 direct、flat、TaskBoard
route 上都表示业务结果视图。task-strategy 如果返回带 `final_result` 的终态
envelope，`get_data()` 会返回该 `final_result`，并在可能时按声明的
`output(...)` contract 解析。`AgentExecutionResult` 还提供
`get_full_data()` / `async_get_full_data()`，用于读取 `status`、`accepted`、
`artifact_status`、`taskboard`、`completion_notes` 或 diagnostics 等执行内部信息。

混用没问题——它们都从同一份缓存里读：

```python
result = agent.input("...").output({...}).get_result()
data = result.get_data()        # 触发请求
text = result.get_text()        # 已缓存
meta = result.get_meta()        # 已缓存
```

`.validate(...)` 每个 result 也只跑一次——校验的就是这份缓存结果。

## 流式

`result.get_generator(type=...)`（sync）与 `get_async_generator(type=...)`（async）发流式事件。`type` 决定你看到什么：

| `type` | 你拿到的 | 适合 |
|---|---|---|
| `"delta"` | 文本 delta；重放替换前额外输出 `"<$retry>{reason}</$retry>"` | 终端打字机 UX |
| `"instant"` | 带 `path`、`delta`、`value`、`is_complete` 的结构化 `StreamingData` 事件 | 字段级 UI 更新 |
| `"streaming_parse"` | 与 `instant` 使用同一个结构化流式 parser 的兼容别名 | 兼容 / 增量 dict 读取 |
| `"specific"` | `(event, data)` 元组，按事件过滤（`delta`、`reasoning_delta`、`tool_calls` 等） | 精确订阅特定事件 |
| `"original"` | 原始 provider 事件 | 调试 / passthrough |
| `"all"` | 所有事件带类型标签 | 完整日志 |

常用类型注解可以直接从 `agently` 导入公开 stream item 类型：
`StreamingData` 对应 `instant` / `streaming_parse`，
`AgentlySpecificResultMessage` 对应 `specific`，
`AgentlyModelResultMessage` 对应 `all`。完整 typed data 命名空间仍可从
`agently.types.data` 导入。
旧的 `AgentlySpecificResponseMessage`、`AgentlyModelResponseMessage` 以及相关
`Response` 别名会继续在 `agently.types.data` 里兼容，但不会从 `agently`
根入口重新导出。推荐使用 `Result` 命名。

`ModelRequestResult` 是 canonical result class。不要再导入或用历史的
`ModelResponseResult` 名称做类型注解。

### Delta 例子

```python
gen = agent.input("讲个递归故事。").get_generator(type="delta")
for delta in gen:
    print(delta, end="", flush=True)
```

### Instant 例子（结构化）

```python
gen = (
    agent.input("给一个定义和三条 tips。")
    .output({
        "definition": (str, "定义", True),
        "tips": [(str, "tip", True)],
    })
    .get_generator(type="instant")
)
for item in gen:
    if item.delta:
        print(f"[{item.path}] + {item.delta}")
    if item.is_complete:
        print(f"[{item.path}] done")
```

`item` 暴露 `.path`（如 `"tips[0]"`）、`.wildcard_path`（`"tips[*]"`）、
`.value`、`.delta`、`.is_complete`、`.event_type` 和 `.completion_source`。
用 `.delta` 更新正在增长
的字段；只有下游动作必须等字段关闭时，才用 `.is_complete` /
`event_type=="done"` 做触发条件。

对 JSON stream，`completion_source` 会区分
`"observed_boundary"`（provider 文本中真实出现定界符）、
`"final_reconciliation"`（raw final JSON 自身已闭合）和
`"synthetic_repair"`（parser 补全了开放尾部）。在最终校验前它们都仍是
provisional；尤其是不可逆动作与超长输出保留不能把 `synthetic_repair`
当作已接受数据。

### AgentExecution 投影

`AgentExecutionStreamData` 是 execution 层的结构化投影，不是
`ModelRequestResult`。一个 execution 持有模型请求时，`instant` / `all` 流会把模型
attempt 的事实作为结构化 stream item 保留下来：`$status` 表达 retry、失败和
完成状态，`meta` 带有 `response_id`、`request_run_id`、`model_run_id` 与
`attempt_index`。`type="delta"` 是纯文本投影，产出字符串，并用
`"<$retry>{reason}</$retry>"` 标记重放边界。
`type="instant"` 会保留每条原始结构化 item；当该 item 还能投影成自然语言文本时，
会紧跟着追加一个 synthetic `AgentExecutionStreamData`，其 `path="$delta"`、
`event_type="delta"`、`source="agent_execution"`，并带有
`meta["stream_kind"] == "text_projection"`。AgentTask Flat snapshot 可以投影为线性
plan/action 摘要；TaskBoard plan/tick event 可以先投影为紧凑 Markdown 状态表，
再在后续投影为 card 状态变化摘要。
heartbeat item 保持 structured-only，
不会追加 synthetic `$delta` 文本。`type="all"` 仍是 raw audit stream，
不包含这些 synthetic projection item。

结构化 `AgentExecution` 会让其持有的 `ModelRequest` 一直运行到 provider/parser
自然终态。`instant` 中出现 ensured 字段或已闭合的 mapping 字段，只表示该 provisional
path 已可见；它不会取消请求，也不能证明后续 evidence、self-check、summary、progress、
diagnostics、最终校验、usage 或 terminal events 已经到达。因此，无论调用方只读取最终
结果还是同时消费流，成功请求都会保留包括 `request.completed` 在内的普通完成链路。

```python
execution = agent.input("总结这份事故更新。")
async for item in execution.get_async_generator(type="instant"):
    if item.path == "$status":
        print(item.value["status"], item.meta["response_id"])
    elif item.path == "$delta" and item.delta:
        # 统一自然语言流槽位。
        print(item.delta, end="", flush=True)
    elif item.path == "model.delta" and item.delta:
        # 带源地址的模型 delta。它用于结构化 UI 状态，不要再写入
        # 已经消费 "$delta" 的同一个文本输出面。
        ui_state[item.path] = ui_state.get(item.path, "") + item.delta
```

无参 execution generator 默认也是同一个 `delta` 投影，所以
`execution.get_generator()` 和 `execution.get_async_generator()` 都产出字符串。
consumer 需要结构化 `$status` 而不是文本标记时，使用 `type="instant"` 或
`type="all"`。UI 同时需要结构化状态更新和派生 `$delta` 文本槽时用
`type="instant"`，但要把两个输出面分开：`$delta` 渲染为统一自然语言流，
`model.delta` 或字段路径等带源地址的 delta 只更新自己的结构化状态槽。不要把两者
追加到同一个可见文本 buffer。records、DevTools-style replay、内部桥接或审计场景
需要避免派生 item 混入 source fact 时用 `type="all"`。

如果多个字段共用一个 CLI 输出区域，不要把 `.is_complete` 当成全局展示顺序屏障。
结构化 parser 往往是因为已经看到下一个 path 开始，才确认上一个 path 已关闭，
所以下一个 path 的首个 `.delta` 可能和上一个 path 的 done 事件几乎同时到达
consumer。Web UI、SSE 和 WebSocket 通常应把不同 `path` 渲染到各自的 UI slot。
如果 CLI 必须把多个 path 按固定阅读顺序打印到同一个终端区域，在 consumer
里维护一个很小的状态 flag 或 buffer，等前一个 path 的 `.is_complete` 事件已经
被处理后，再 flush 后一个 path 的内容。

### 没有渐进 consumer：直接读取最终 data

如果调用方不会发布 delta、更新 UI/state、记录 stream event，也不会用 provisional
字段启动明确可取消/幂等的准备工作，就不要打开 stream，直接等待最终解析对象：

```python
result = (
    agent
    .input("判断这张支持工单属于哪个路由。")
    .output({"route": (str, "billing | technical | other", True)})
    .get_result()
)

data = await result.async_get_data()
```

下面这种 discard-only `instant` drain loop 是反模式：

```python
# 反模式：没有发布或使用任何 item。
async for _item in result.get_async_generator(type="instant"):
    pass

data = await result.async_get_data()
```

它不会额外发起一次模型请求，但仍会增加 stream queue、迭代、event object 和 parser
处理，却没有产生 consumer value。只有 item 会驱动真实 consumer 时才使用 generator。
确实需要 streaming 时，应发布或应用这些 item，再从同一个 result 读取最终可靠对象：

```python
async for item in result.get_async_generator(type="instant"):
    await publish_structured_patch(item)

data = await result.async_get_data()
```

### 高价值模式：先流式更新 UI，再读取最终可靠结果

当应用可以在完整回答结束前展示或路由单个结构化字段时，用 `instant`。流式事件用于
渐进式 UI 状态；最终业务对象仍然应该来自 `async_get_data()`。

```python
import asyncio
from collections import defaultdict
from agently import Agently

agent = Agently.create_agent()


async def stream_triage_card(ticket_text: str):
    result = (
        agent
        .input(ticket_text)
        .output(
            {
                "status_summary": (str, "给用户看的一句话状态", True),
                "risk_flags": [(str, "明确风险点", True)],
                "next_actions": [(str, "支持团队下一步动作", True)],
                "customer_reply": (str, "发给客户的回复", True),
            },
            format="json",
        )
        .get_result()
    )

    ui_state: dict[str, str] = defaultdict(str)

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            # 把字段级 patch 推给 UI / SSE / WebSocket。
            ui_state[item.path] += item.delta
            print({"path": item.path, "delta": item.delta})
        if item.is_complete:
            print({"path": item.path, "status": "done", "value": item.value})

    # 不会发第二次请求：这里读取的是同一个 result 的最终缓存解析结果。
    final_data = await result.async_get_data()
    return final_data


asyncio.run(stream_triage_card(
    "Ticket T-104: enterprise billing export failed twice; CFO waiting."
))
```

服务里优先用 async 消费。同步 `get_generator(type="instant")` 适合脚本和
notebook。

### 提前启动下游工作，再以最终结果对账

一个靠前且已完成的字段，可以在模型继续生成时启动只读或其他可幂等/可取消的工作。
使用下面的契约：

1. 只响应完整的规范字段或列表项，例如
   `wildcard_path == "retrieval_tasks[*]" and is_complete`；
2. 根据任务相关 payload 生成宿主拥有的 key，通过有界 async owner 派发，不在
   stream loop 内等待耗时工作；
3. 继续消费模型 stream；
4. 用 `async_get_data()` 读取最终通过校验的对象；
5. 复用匹配工作，补发流式阶段没有观察到的最终项，取消或丢弃多余临时项。

必须做最终对账，因为 parse、ensure 或自定义校验可能在原始 instant stream
结束后接受一个替换 attempt。retry event 与重复 delta 必须收敛到同一个宿主 key，
不能重复检索。不可逆副作用与最终业务决策仍要等待最终接受对象。

### Specific 例子（事件）

```python
gen = agent.input("打个招呼。").get_generator(type="specific")
for event, data in gen:
    if event == "delta":
        print(data, end="", flush=True)
    elif event == "reasoning_delta":
        print("[reasoning]", data, end="", flush=True)
    elif event == "tool_calls":
        print("[tool call]", data)
```

### Reasoning 事件

有些 provider 会用原生 response 字段提供 reasoning。有些本地或 OpenAI-compatible
reasoning 模型可能把开头的外层 `<think>...</think>` 放进普通 content。Agently
会在结构化解析前统一归一：

- `reasoning_delta` / `reasoning_done` 承载 reasoning 文本。
- `delta` / `done` 只承载 parser 应消费的 answer payload。
- `original_delta` / `original_done` 保留 provider 原始内容，不做改写。
- 只归一位于 answer payload 之前的完整外层 `<think>...</think>`。字段、代码块或
  长文本 payload 内部的 `<think>` 会作为普通 answer 内容保留。

## Async 流式

同样的 generator 改 async：

```python
import asyncio

async def main():
    result = agent.input("...").output({...}).get_result()
    async for item in result.get_async_generator(type="instant"):
        if item.is_complete:
            print(item.path, item.value)

asyncio.run(main())
```

服务和 TriggerFlow 场景应走 async —— 见 [Async First](../start/async-first.md)。

### Attempt 状态

`$status` 是框架保留的 stream path，不是模型输出字段。当显式允许 provider 在已经有
partial 输出后重放时，它用于通知 UI/SSE 消费者：

```python
result = agent.create_request().input("总结这次事故。").get_result()

async for item in result.get_async_generator(type="instant"):
    if item.path == "$status" and item.value["status"] == "failed" and item.value["retry"]:
        clear_provisional_answer()
        continue
    render_field_update(item)
```

最终的 `get_data()` 不含 `$status`。需要原始状态事件时，用 `type="all"` 或
`type="specific", specific="status"`。`reason` 给出有界的 transport/provider 实际说明；
`cancelled` 与失败请求不同。

对于 OpenAI-compatible SSE，显式 `[DONE]` 是响应的逻辑终止。Agently 会立即停止消费
该 SSE iterator，因此即使网关随后遗漏或损坏 HTTP chunked terminator，也不会用物理
收尾错误覆盖已经完成的响应；最终 `original_done`、`meta`、usage 和 `finish_reason`
仍会保留。若断流发生在 `[DONE]` 之前，它仍是 transport failure，并遵循已配置的
failover/retry 策略；仅有 partial 输出绝不会被合成为成功。

纯文本 `delta` 消费者会在替代 attempt 的正文前收到独立的
`"<$retry>{reason}</$retry>"` 标记。它是重放边界，不是模型正文：

```python
import html

provisional_text = ""
for chunk in result.get_generator(type="delta"):
    if "<$retry>" in chunk:
        retry_reason = html.unescape(
            chunk.removeprefix("<$retry>").removesuffix("</$retry>")
        )
        provisional_text = ""
        clear_provisional_answer(retry_reason)
        continue
    provisional_text += chunk
    render_delta(chunk)
```

标记里的 reason 会对 provider 错误消息中的 `<`、`>`、`&` 做 XML text 转义。
当结构化事件可用时，`$status` 是优先使用的 retry 控制记录；当消费侧选择纯
`delta` 时，这个 marker 就是对应的公开 replay boundary。纯文本流无法让 sentinel
完全无碰撞；必须保留模型输出中包含 `"<$retry>"` 的文本 chunk 时，应改用
`instant`、`specific` 或 `all`。

AgentExecution 会把同一状态投影成结构化 process item，并在 `item.meta` 中加入来源
request/run lineage。消费侧需要结构化 retry 事实时，使用 `instant` 或 `specific`：

```python
execution = agent.input("总结这次事故。")

async for item in execution.get_async_generator(type="instant"):
    if item.path == "$status" and item.value["retry"]:
        clear_provisional_output(item.meta["response_id"])
        continue
    render_execution_item(item)
```

它的公开 `type="delta"` 投影可能用文本发出同一个 `<$retry>...</$retry>` replay
marker。持久化 artifact writer 或 SSE/UI 消费者选择纯文本 stream 时，应在消费边界处理
这个 marker；不要为了拿到 instant 字段而把自由文档正文强行塞进 `.output()`。

## 并发

因为 `get_result()` 只在你消费时才发请求，可以先建多个 result，再并发消费：

```python
import asyncio

async def ask(prompt):
    r = agent.input(prompt).get_result()
    return await r.async_get_text()

results = await asyncio.gather(
    ask("总结递归。"),
    ask("给一个 Python 例子。"),
)
```

这是标准 async 模式，Agently 没有特别封装。

### 可选的请求调度

当大量并发请求（或长程任务）有触发供应商并发/速率上限的风险时，可以按 provider
限制模型请求的下发。调度是可选的；不配置时请求立即下发、重试立即重发（行为不变）。

```python
# 限制所有 provider 的在途并发与每秒下发数，可对单个 provider 覆盖。
agent.set_settings("model_request.scheduler.max_concurrency", 8)
agent.set_settings("model_request.scheduler.rate_per_second", 5)
agent.set_settings("model_request.scheduler.providers",
                   {"OpenAICompatible": {"max_concurrency": 2}})

# 重试之间退避而非立即重发（指数 + 抖动）。
agent.set_settings("model_request.retry_backoff_base", 0.5)  # 秒
agent.set_settings("model_request.retry_backoff_max", 30)
```

由于重试也走同一个 per-provider 槽位，速率限制同样会拉开重试调用的间隔，从而抑制
供应商错误风暴。

## 能复用就别重发

```python
# 不好——同一请求跑了三次
text = agent.input("...").start()
data = agent.input("...").output({...}).start()
meta = agent.input("...").output({...}).get_result().get_meta()

# 好——跑一次，读三种视图
result = agent.input("...").output({...}).get_result()
text = result.get_text()
data = result.get_data()
meta = result.get_meta()
```

## 另见

- [Async First](../start/async-first.md) —— 何时切到 `get_async_generator(...)`
- [输出控制](output-control.md) —— 「模型返回」与「你读到」之间发生了什么
- [Schema as Prompt](schema-as-prompt.md) —— `output()` 能接受什么
