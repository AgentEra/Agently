---
title: 模型集成
description: 在 TriggerFlow chunk 内调 agent 与 model request。
keywords: Agently, TriggerFlow, agent, model, request, async, instant
---

# 模型集成

> 语言：[English](../../en/triggerflow/model-integration.md) · **中文**

chunk handler 是普通 async 函数。你可以在里面调任何 agent / request / response API。好的模式集中在三件事：async（因为周围 flow 是 async）、结构化输出（因为下一 chunk 期望已知结构）、用户真受益时再 streaming。

## 最小模式

```python
from agently import Agently, TriggerFlow, TriggerFlowRuntimeData

agent = Agently.create_agent()


async def classify(data: TriggerFlowRuntimeData):
    result = await (
        agent
        .input(data.input)
        .output({
            "category": (str, "分类", True),
            "confidence": (float, "0.0 到 1.0"),
        })
        .async_start()
    )
    await data.async_set_state("classification", result)
    return result


flow = TriggerFlow(name="classify")
flow.to(classify)
```

agent 在模块层创建以便跨 execution 复用。`await ... async_start()` 返回解析后的 dict。dict 进 state 进 close snapshot，也作为返回值传给下一 chunk 的 `data.input`。

## 永远用 async

周围 flow 是 async。chunk 内调 sync `start()` 能跑但会阻塞 event loop，损失并发。用 `async_start()` / `async_get_data()` / `get_async_generator(...)`。详见 [Async First](../start/async-first.md)。

## 显式表达新信息边界

后置字段只使用请求发出时的输入快照和同一响应里较早产出的有界字段时，可以保留在
同一个 ModelRequest 中。如果后置模型阶段需要请求发出后才产生的新事实，应显式
表达这条必须串行的值依赖：

```text
R1 -> Action/系统工作 -> 宿主校验/readback -> R2
```

这包括检索结果、API/数据库响应、文件或 artifact readback、approval/resume
payload，以及必须先由确定性代码算出、再交给模型判断的数据。高层 Agent execution
可以代为管理这个循环，但逻辑模型拓扑仍然跨越了不同请求。

`instant` 可以在 R1 完成前提前启动可取消、幂等的工作，让它与无关的后续生成重叠；
它不是向 R1 回传新信息的通道。如果 R2 需要该结果，应继续消费完 R1，对账最终接受
的触发集合，等待并校验工作结果，然后才发出 R2。这个必要 join 两侧的独立分支仍可
并发执行。

## 把结构化字段流向 runtime stream

消费 runtime stream 的 UI 受益于增量更新时，把模型 result 的结构化字段 patch
桥接到 TriggerFlow runtime stream：

```python
async def draft_with_streaming(data: TriggerFlowRuntimeData):
    result = (
        agent
        .input(data.input)
        .output({
            "title": (str, "标题", True),
            "body": (str, "正文", True),
        })
        .get_result()
    )

    async for item in result.get_async_generator(type="instant"):
        if item.delta:
            await data.async_put_into_stream({
                "path": item.path,
                "delta": item.delta,
                "done": item.is_complete,
            })

    final = await result.async_get_data()
    await data.async_set_state("draft", final)
    return final
```

`type="instant"` 产出结构化 `StreamingData` patch，不是原始 provider token。
消费者可以在 `body` 还在生成时先渲染 `title` delta。stream 结束后，
`async_get_data()` 返回同一个 result 的最终缓存解析 dict（不再发请求）。

普通 ModelRequest 和 AgentExecution 的 instant 值仍是暂定结果。即使已观察到完整字段，
schema、ensure 或 validator 失败仍按原有额度重试。最终校验后重新打开结果流会读取通过的
attempt；AgentExecution 转发带 attempt metadata 的替换结果。副作用以最终校验结果为准。

只有实际使用 SystemOne 的阶段在观察到完整字段后禁止重放该阶段。最终校验仍可失败，
阶段保留错误、观察值及 `instant_retry_suppressed`，下游不消费失败结果。SystemOne 前后的
普通 LLM 阶段仍共享既有重试额度；provider 传输重试合同不变。

## 让生成与下游 fan-out 重叠

当靠前的完整字段能够启动独立检索或准备工作时，优先采用 TriggerFlow 可见的
start/cache/reconcile 模式：

- 把紧凑触发项放在长 progress prose 与最终 artifact 前；
- 完整项到达时，根据 payload 生成宿主拥有的 key，只调用一次
  `await data.async_emit_nowait(...)`；
- 继续 instant loop，不让后续项和进度被阻塞；
- `async_get_data()` 返回后，按最终接受项集合对账：复用匹配项、补发缺失项、
  取消或丢弃多余项；
- 只 join 最终接受的 key，再提交最终状态。

临时分支必须是只读或其他可幂等/可取消工作。execution `concurrency`、
model/provider limit 与外部 adapter quota 分别拥有各自的压力控制。原始 parser path
应转换成稳定 workflow status/event，不应直接暴露给前端。

## 在一个 chunk 内复用 result

调一次 `get_result()`，从 `result` 读 text + data + meta 不再发请求。详见 [模型结果](../requests/model-response.md)：

```python
async def step(data):
    result = agent.input(data.input).output({...}).get_result()
    text = await result.async_get_text()
    obj = await result.async_get_data()
    meta = await result.async_get_meta()
    await data.async_set_state("text", text)
    await data.async_set_state("obj", obj)
    await data.async_set_state("meta", meta)
```

## 按 execution 定制 agent

flow 的 chunk 需要按 execution 用不同模型配置时，通过 runtime resource 注入：

```python
execution = flow.create_execution(
    runtime_resources={"agent": Agently.create_agent().set_settings(...)},
)


async def step(data):
    agent = data.require_resource("agent")
    return await agent.input(data.input).async_start()
```

不要把 agent 放进 `state` —— agent 持有网络 client，不适合 snapshot。用 `runtime_resources`（见 [State 与 Resources](state-and-resources.md)）。

## 校验、重试、结构化输出

`.validate(...)` 与 `ensure_keys` 在 chunk 内的工作方式与 request 层一样。retry 预算按 request 算，chunk 内的模型重试不影响 flow 其他部分。详见 [输出控制](../requests/output-control.md)。

```python
async def step(data):
    return await (
        agent
        .input(data.input)
        .output({"answer": (str, "answer", True)})
        .validate(custom_business_check)
        .async_start(max_retries=5)
    )
```

## 不要把模型 state 放进 flow_data

`flow_data` 跨 flow 所有 execution 共享并发 warning。不要用它「记住上次模型答案」 —— execution-local 用 `state`；多轮对话用真正的 session，详见 [会话记忆](../requests/session-memory.md)。

## 单 flow 多 agent

多个 chunk 可以用多个 agent —— 不同 provider、不同 prompt、不同 toolset：

```python
classifier = Agently.create_agent().set_settings("OpenAICompatible", {"model": "${ENV.CLASSIFIER_MODEL}"})
writer = Agently.create_agent().set_settings("OpenAICompatible", {"model": "${ENV.WRITER_MODEL}"})

async def classify(data):
    return await classifier.input(data.input).output({...}).async_start()

async def draft(data):
    return await writer.input(data.input).async_start()

flow.to(classify).to(draft)
```

这就是 TriggerFlow 扮演编排角色的方式：flow 持有连线，每个 agent 仍是小而聚焦的单元。

## 另见

- [Async First](../start/async-first.md) —— 为什么每个 chunk 都该用 async
- [模型结果](../requests/model-response.md) —— `get_result()` 与 result 缓存
- [输出控制](../requests/output-control.md) —— chunk 内 validate / retry 行为
- [State 与 Resources](state-and-resources.md) —— agent 该放哪
