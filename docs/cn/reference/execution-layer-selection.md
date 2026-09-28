---
title: 执行层选择
description: 选择 ModelRequest、AgentExecution、AgentTask、TaskDAG、Blocks、TriggerFlow 与上下文/存储 owner。
keywords: Agently, AgentExecution, AgentTask, TaskContext, TaskDAG, Blocks, TriggerFlow
---

# 执行层选择

一次 Agent run 默认从 `AgentExecution` 开始；只有业务问题需要某个更底层 owner
的 contract 时，才下沉到该层。

```mermaid
flowchart TD
    App["业务应用"]
    AE["AgentExecution\nrun, result, stream, meta"]
    MR["ModelRequest\n一次语义请求"]
    AT["AgentTask\ngoal, plan, verify, replan"]
    DAG["TaskDAG\n已校验的提交式 DAG"]
    BL["Blocks\n已校验 execution blocks"]
    TF["TriggerFlow\nworkflow lifecycle"]
    TC["TaskContext + ContextReader\n渐进式披露"]
    TW["TaskWorkspace\n任务文件"]
    RS["RecordStore\nrecords 与 durability"]

    App --> AE
    AE --> MR
    AE --> AT
    DAG --> TF
    DAG -. "显式 Blocks carrier" .-> BL
    AT --> BL
    BL --> TF
    AE --> TC
    AT --> TC
    TC --> TW
    TC --> RS
    TF --> RS
```

## 所有者

| 所有者 | 用于 | 不用于 |
|---|---|---|
| `ModelRequest` | 精确 prompt、结构化输出、settings、一次 response | workflow state 或长任务 lifecycle |
| `AgentExecution` | 一次公开 Agent run、candidate binding、result/stream/meta | custom DAG validation 或持久 workflow topology |
| `AgentTask` | 一个 goal-driven task 的 plan、bounded execution、evidence、verification、repair/replan | developer-owned 稳定 workflow topology |
| `TaskDAG` | 模型/应用提交的 acyclic plan data、validation、resolver、dependency results | 面向人的 acceptance 或 pause/resume policy |
| `Blocks` | 已校验 block lowering、signals、result/evidence mapping | Skill 安装、capability grant、storage |
| `TriggerFlow` | branch、concurrency、wait、resume、runtime stream、save/load | prompt 或 DAG task semantics |
| `TaskContext` / `ContextReader` | source binding 与 consumer/phase 信息交付 | source persistence 或副作用 |
| `TaskWorkspace` | 任务文件边界、mutation/readback、file refs | records、memory、snapshots |
| `RecordStore` | records、检索索引、links、checkpoints、snapshots/events | 任务文件或语义决策 |

## 按问题形态选择

| 业务形态 | 起点 |
|---|---|
| 一次抽取、分类、改写或回答 | `ModelRequest` |
| 一次使用 Actions 或 Skills 的 Agent 请求 | `AgentExecution` |
| 必须验证完成情况的单个业务目标 | `agent.create_task(...)` / AgentTask strategy |
| 应用或模型提交的 DAG data | `TaskDAG` / DynamicTask facade |
| 源码中由开发者拥有的稳定 workflow topology | `TriggerFlow` |
| 从多个已知 source 获取有界相关信息 | `TaskContext` + consumer-bound `ContextReader` |
| 读取或修改已有项目文件 | `TaskWorkspace` |
| 持久 memory、evidence records 或 recovery | `RecordStore` |

Skills 不增加新执行层。`SkillLibrary` 拥有安装 revision；AgentExecution 把它们
绑定到 TaskContext；实际工作由所选执行 owner 完成。

TaskDAG 是 data，执行前必须 validate/resolve。开发者拥有的稳定 topology 可以
直接使用 TriggerFlow。TaskDAG 默认 executor 使用 TriggerFlow；只有需要 block
lifecycle evidence 时才显式 `compile_blocks(...)`。

`context_read` 只接收调用方绑定的 ContextReader。文件操作使用 TaskWorkspace
Actions，持久化使用 RecordStore ports。一个 owner 的 readback 不能被当作另一个
owner 的 required capability 已执行的证据。

TaskBoard 卡片的最终文件路径指定交付位置，不决定执行方式。需要 Actions 的卡片
即使要等后续信息才能确定调用参数，也保留其执行方式；control 卡片仍可把文本交给
Host 写入。指定最终路径时，Host 可以将已接受的候选制品复制到该位置，并核实复制
内容后再报告完成，不必要求模型亲自执行复制。

对于没有明确交付或能力合同的普通 TaskBoard 结果，由 loop 的最终判断驱动完成。
终端语义校验只在明确硬合同、确定性完整性问题或生命周期阻断时保留，不再自动对
每个文字结果追加第二轮审阅。
已完成的叶卡候选本身不等于语义完成证明；候选提升仅用于明确的最终
TaskWorkspace 交付合同。

普通 finalizer 拒绝结果时，由同一次响应的 `replan_signal` 驱动既有 loop：
`repair` 使用现有证据修复，`replan_segment` 先补充证据再修复，`blocked` 或
`clarify` 等待缺失的外部条件。不追加 verifier 来选择下一步。Host 校验结构化信号，
沿用无进展收敛；普通文字修复保留原来的内联输出形式。
