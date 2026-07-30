---
title: Agently 4.1.4.5 发布说明
description: 可选长输出续写，以及由 Stage 直接支撑的 TriggerFlow task 生命周期。
keywords: Agently, 4.1.4.5, 长输出, TriggerFlow, Agently-Stage, settlement
---

# Agently 4.1.4.5 发布说明

Agently 4.1.4.5 加强了两个运行时边界，但没有改变语义 owner：

- 当 direct `ModelRequest` 以可观测的 length 或 incomplete terminal 结束时，
  `AgentExecution` 可以显式启用无损续写；
- `TriggerFlowExecution` 直接使用 Agently-Stage 管理进程内 task，不再复制第二套
  task scope。

## 可选长输出续写

当业务结果必须跨越 provider 单次响应窗口时，在尚未启动的 direct
`AgentExecution` 上调用 `.ensure_long_output()`：

```python
result = (
    agent
    .input({"topic": "runtime ownership"})
    .instruct("输出完整技术报告。")
    .ensure_long_output()
    .get_result()
)
```

第一次请求保留原有 Prompt 和输出契约。只有 provider/parser terminal 明确报告
length 或 incomplete，才会进入续写。已接受的文本或结构化单元采用 append-only
提交，通过 `TaskWorkspace` 保存并完整读回，最终仍需通过原始输出契约验证。

该策略默认关闭，支持纯文本和已声明的 JSON 输出契约，不能与显式 AgentTask
strategy 同时使用。普通成功响应仍保持单次请求。

参见[输出控制](../requests/output-control.md)和
[`examples/basic/ensure_long_output.py`](../../../examples/basic/ensure_long_output.py)。

## Stage 支撑的 TriggerFlow task ownership

Agently 现在依赖 `agently-stage >=0.3.5,<0.4.0`。

每个 `TriggerFlowExecution` 直接持有一个真实 Stage：

- execution 自己创建的 caller-loop task 通过 `Stage.create_task(...)` 进入；
- 只有接管前已经存在的 task 才通过 `Stage.adopt(...)` 进入；
- Stage 是唯一的实时 task/origin inventory 和 settlement owner；
- TriggerFlow 继续负责工作流失败策略、单一 close deadline、RuntimeEvent
  投影与 close snapshot。

旧的私有 `StageManagedTaskScope` adapter 已删除。这不意味着 Agently 对外暴露
Stage lifecycle：EventCenter、SignalNet、TriggerFlow、RuntimeEvent 和
AgentExecution 继续拥有各自语义。

隐藏 runtime-stream execution 会在消费结束后显式 close 和 settlement，避免
idle monitor 在 caller loop 退出后残留。EventCenter 继续使用原生后台任务机制，
因为实验中替换该热路径的可测开销不值得接受。

## 同步/异步兼容

Agently 内部的调用形态桥接使用 Agently-Stage `StageCallBridge`。
`FunctionShifter` 仍可导入，但只作为 deprecated 兼容 facade，并委托给同一个
bridge；新的框架代码不应让它负责 task 生命周期。

## 性能特征

Stage-native TriggerFlow 与旧私有 adapter 进行了两轮反向顺序本地 A/B，每个
variant 共记录 18 个样本：

| 工作负载 | Candidate 变化 |
|---|---:|
| Managed task 创建与 settlement | +4.74%（约 +0.61 µs/task） |
| 取消 settlement | -3.08% |
| 有限 TriggerFlow execution | -1.34% |
| TriggerFlow event fan-out | +0.29% |
| Peak traced task memory | 0.00% |

实验没有产生 pending-task、未消费异常或生命周期警告。这些数据证明的是本地开销
可控，并不表示 Stage 会让 provider-bound 模型请求变快。

## 兼容性

- Python：`>=3.10`
- Agently-Stage：`>=0.3.5,<0.4.0`
- 推荐 Agently DevTools：`>=0.1.10,<0.2.0`
- Skills authoring protocol：`agently-skills.authoring.v2`
- DevTools observation protocol：`agently-devtools.observation-runtime.v1`

4.1.4.4 的 ModelRequest、TriggerFlow snapshot、RecordStore retention 和
companion protocol 契约继续受支持。

