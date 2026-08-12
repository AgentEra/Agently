---
title: Agently 4.1.4.7 开发说明
description: TriggerFlow 集成中的 Stage 混合同步/异步环境自动路由。
keywords: Agently, 4.1.4.7, Agently-Stage, TriggerFlow, sync, async, carrier
---

# Agently 4.1.4.7 开发说明

Agently 4.1.4.7 开发线将 Stage 最低依赖更新为
`agently-stage >=0.3.6,<0.4.0`，用于准备针对
[Agently #347](https://github.com/AgentEra/Agently/issues/347)、
[Agently-Stage #24](https://github.com/AgentEra/Agently-Stage/issues/24) 与
[Agently-Stage #25](https://github.com/AgentEra/Agently-Stage/issues/25)
所暴露运行时问题的 Agently 集成。

根因修正在 Stage：同步与异步 scope 现在会区分继承的逻辑执行 lineage，以及当前调用
在物理上可以安全阻塞的线程和 event loop。4.1.4.7 开发线增加完整 TriggerFlow
链路的回归契约；待该集成版本发布后，后续 Agently 安装才会默认取得这项修正。

## 开发者可见变化

| 区域 | 4.1.4.7 行为 | 兼容性 |
|---|---|---|
| 同步 TriggerFlow chunk | 工具提供方的同步 wrapper 可以用 `with Stage()` 调用异步 SDK，随后继续调用 `data.set_state(...)`、`append_state(...)` 或 `del_state(...)` | 不要求提供方改写 API，也不要求其知道 TriggerFlow 私有使用 Stage |
| Stage 依赖 | 在既有 `<0.4.0` 兼容线内将最低版本提升为 `0.3.6` | 只升级 Stage 即可修复现有脚本；升级 Agently 可修正后续安装的依赖解析 |
| `FunctionShifter.syncify/asyncify` | Deprecated 名称与警告保留；标量调用委托给 `Stage.as_sync/as_async` | 既有 import 与调用形式继续有效 |
| 内部 bridge | Agently 需要轻桥接、注入生命周期、stream 或显式 `managed=True` settlement 的位置继续使用 `default_stage_call_bridge` | 不做全量替换，不改变这些边界的语义 |
| Workflow ownership | TriggerFlow 继续拥有 workflow state、生命周期、持久化、并发与错误 | Stage carrier 细节保持私有且不会序列化 |

## 工具提供方拥有的同步接口

即使底层实现是异步的，工具或 Function 提供方仍可有意保留同步方法：

```python
from agently_stage import Stage


def search(query: str):
    with Stage() as stage:
        return stage.get(search_tool.search, query)


def chunk(data):
    result = search(data.input)
    data.set_state("search_result", result, emit=False)
    return result
```

Stage 会自动选择物理上安全的 carrier。工具提供方无需探测 TriggerFlow 是否已在内部
使用 Stage。

这个调用边界仍会同步阻塞所在 worker。原生 async chunk 仍应优先使用 `await` 与
`data.async_set_state(...)`。依赖 caller loop-bound 对象的工作必须留在其 owner loop
并在那里 await。

## Stage 接口

Agently 不会重新导出 Stage。应用需要直接使用 scoped adapter 时，从
`agently_stage` 导入：

```python
from agently_stage import Stage

sync_search = Stage.as_sync(search_tool.search)
async_transform = Stage.as_async(transform)
```

每次 adapter 调用拥有一个自动 Stage scope，并等待结果及 Stage-owned settlement。
stream 转换、Stage/executor 注入、独立 close 与轻桥接继续由高级接口
`StageCallBridge` 负责。

## 升级

报告中的调用链不需要修改应用源码。在 Agently 4.1.4.7 仍处于开发阶段时，如果现有
Agently 安装已经允许 Stage 0.3 兼容线，可以只把 Stage 升级到 0.3.6；待该集成版本
正式发布后，再正常安装 Agently 4.1.4.7。
