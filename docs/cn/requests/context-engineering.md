---
title: Context Engineering
description: 怎么把背景知识送到模型面前但不撑爆 prompt。
keywords: Agently, context, info, session, KB, retrieval, instruct
---

# Context Engineering

> 语言：[English](../../en/requests/context-engineering.md) · **中文**

模型只看得见上下文窗口里的内容。Context engineering 就是决定「放什么、放哪、不放什么」的纪律。

## 上下文从哪来

| 来源 | 落在哪 | 生命周期 |
|---|---|---|
| `role` / `system` 槽 | system 消息 | agent 持久 |
| `info` 槽 | system 或 user（实现细节） | 持久或单次 |
| `instruct` 槽 | user 消息 | 持久或单次 |
| `input` 槽 | user 消息 | 单次 |
| Session chat history | user/assistant 消息 | 跨请求累积 |
| Session memo | system 消息 | 持久，由自定义 resize handler 写入 |
| 知识库检索 | 检索代码注入 | 单次按需 |
| `TaskContext` sources | 有界 `ContextPackage` 信息块 | 按 task、consumer 与 phase |
| 工具 / MCP 结果 | tool 消息 | 单次工具循环内累积 |

按职责选槽：

- **role / system** —— 模型是谁、硬性规则（语气、人设、拒绝模式）。
- **info** —— 跨调用不变的事实（产品目录、严重度等级、格式约定）。
- **instruct** —— 这类请求该怎么做（步骤、顺序、输出风格）。
- **input** —— 每次调用都变的单一 payload。
- **chat history** —— 当前会话里用户与模型说过的话。
- **memo** —— 应用自定义压缩后的长期上下文。
- **KB** —— 大规模、不总是相关的知识。

不要全往 `input` 塞。也不要把每次都变的 payload 写进 `info`。

## 何时用什么

| 你有的内容 | 放进 |
|---|---|
| agent 的人设、语气、能力规则 | `role`（`always=True`） |
| 模型必须知道的固定枚举（如严重度代码） | `info`（`always=True`） |
| 一类任务的步骤指令 | `instruct`（`always=True` 当 agent 只做这类任务） |
| 一次调用的可变 payload | `input` |
| 上几轮对话 | session chat history |
| 100k tokens 公司文档 | KB + 检索，**不**放进 prompt |
| 当前轮检索到的相关事实 | 仅本次请求的 `info` |

## 用 TaskContext 做任务级渐进式披露

prompt slot 负责模型可见材料，不拥有任务 source catalog。当一个任务可能需要
Skills、files、records、SessionMemory recall、evidence 或固定仓库时，把这些 source
绑定到 `TaskContext`，再由 `ContextReader` 按 consumer/phase 读取一份
`ContextPackage`。

ContextPackage 的不可变结构化信息在进入 Prompt 时会递归投影为普通数据，保留嵌套字段、数值、布尔值和空值；不会把只读映射转成 Python 对象描述。投影不修改原包。

TaskBoard 准备阶段按首次规划的用途读取资料，并把同一份包直接交给规划请求；
资料未变化时，不在下一阶段再次选择和读取。若规划前任务上下文或来源 revision
发生变化，则刷新资料包及后续卡片使用的上下文。图片等附件随包交付，只有规划请求
成功后才记录消费。其他阶段和卡片仍按各自范围读取。

TaskContext 拥有内部 `ContextIndex`。source 提供结构 descriptor 与有界精确读取；
内部 index 构建可复用、带 revision 的 structural、lexical 或可选 hybrid partition。
ContextReader 查询 index，完成由 ModelRequest 负责的可选相关性选择，读取 canonical
source 内容并执行披露预算。模型只收到最终 blocks、refs、omissions、coverage 与
diagnostics，不接收完整 source tree 或内部 vector。

派生索引应通过其公开聚合 owner 配置。embedding provider 只提供机制能力；绑定具体
consumer 的 ModelRequest selector 仍负责语义相关性判断：

```python
task_context.configure_index(
    strategy="hybrid",
    embedding_provider=embedding_provider,
)
```

hybrid 模式会先通过 vector/lexical 排序，把可选 descriptor 窗口缩到 reader 的
`max_blocks`，再交给语义选择，而不是把候选窗口扩成四倍。selector 仍可全部省略，
或按相关性返回有序子集，由 Host 执行交付预算。当结构 filter 已经只留下一个 canonical candidate 时，
index 不再请求 query embedding，因为此时不存在需要优化的候选顺序。

当一个 canonical ref 已经通过结构过滤选定后，source 可以选择支持在该 ref 内进行
确定性、有界定位。这个 source-scoped read 不判断相关性，也不验收 evidence；
`ContextReader` 仍拥有读取会话，source 未提供该可选端口时回退普通有界 exact read。
一个无通配符的精确 path 若只剩一个已授权 candidate，不需要再发一次模型请求来选择
同一个 candidate。

完整 `ContextPackage` 继续保留作审计。AgentTask 的 model-hot 投影会限制重复的可选
omission 明细并携带原因计数，避免未选择的 source catalog 反而占满 prompt。已有 scoped
evidence snippet 时，每段 snippet 直接带一个宿主发放的 `reference_id`；重复 locator
与正文副本不进入热 prompt，canonical provenance 仍保留在宿主侧。宿主会在披露前，
把每段正文与 execution block、ContextBlock、source revision、binding 和 canonical ref
做一对一连接；缺失或歧义连接会排除正文并产生 diagnostic。execution/block/binding
等不透明身份留在宿主侧，模型只选择 `reference_id` 和任务相关的 source label。

一份 scoped-retrieval plan 的全部 query group 最多预留 64 个模型可见结果（即各组
`max_results` 之和）。超出容量时会在 Blocks graph 编译前拒绝，绝不静默截断；更大
读取应拆成由 consumer 负责的 continuation batch。

Embedding usage 与模型 prompt usage 是两类独立事实。cache hit 或更小的
ContextPackage 可以解释效率变化，但只有可比请求的完整 provider-observed prompt
token 才能证明模型输入 token 下降；不得把字符数换算成计费 token。

source contract 与 owner 边界见[任务上下文、文件与记录](workspace.md)。

## 让 info 可 diff

`info` 接受 dict，框架渲染。这比手工把 JSON 拼进 prompt 好 —— diff 可读，框架可一致地渲染成 YAML / JSON / 伪表格。

```python
agent.info({
    "severities": ["P0", "P1", "P2", "P3"],
    "format": "用 markdown bullet，无开场白。",
}, always=True)
```

## 不要手抄工具目录

用了 actions / tools 后，框架会在模型规划工具调用时自动注入工具目录。不要手工把工具描述抄进 `info`。详见 [Action Runtime](../actions/action-runtime.md)。

## Session vs KB vs `info`

| 场景 | 最合适的位置 |
|---|---|
| 「记住用户在这次对话里报的姓名」 | session chat history |
| 「跨多次对话记住用户偏好」 | 自定义 session resize / memo，或应用层用户画像 |
| 「从知识库里查到相关片段」 | KB 检索 → 把片段放进单次 `info` |
| 「模型每次都要看的固定列表」 | `info(always=True)` |
| 「用户刚发了 500 字的问题」 | `input` |

## 压缩优于截断

上下文窗口快满时：

- 默认 session 只按 `session.max_length` 做窗口裁剪；需要摘要时，注册自定义 resize handler，把摘要写入 session `memo`。详见 [会话记忆](session-memory.md)。
- 任务 source 优先使用有界 TaskContext read 与可复用 ref。真正一次性的长输入，
  先摘要再请求，不要中间截断。

## 单次 info 而不污染 agent

```python
result = (
    agent
    .info({"retrieved_snippets": chunks}, always=False)  # 仅本次
    .input(question)
    .output({...})
    .start()
)
```

不传 `always=True`，`info` 仅本次有效。

## 另见

- [Prompt 管理](prompt-management.md) —— 槽位语义详解
- [会话记忆](session-memory.md) —— chat history 与 memo
- [知识库](../knowledge/knowledge-base.md) —— 检索-后-prompt 模式
- [Action Runtime](../actions/action-runtime.md) —— 工具目录是自动注入的

### 任务准备与来源复用（4.1.4.9）

资料选择同时收到完整执行任务、当前读取意图与阶段；后续目标不扩大当前阶段的
读取范围。候选只携带选择 key、role、摘要和完整性。字节估算、限额和规范身份由
Host 持有。已读的必要指令可以说明可选资料的读取条件，可选正文在选中后读取。
Skill 资源目录不再作为额外的 `resource-index` 候选，资源摘要也不再附带字节数；
直接对来源执行目录 exact read 的能力仍保留。

内置 TaskWorkspace 来源复用文件观察以及有界、完整的小型 UTF-8 文本正文。
使用时仍检查路径归属和本地文件身份；替换、删除或 stat 变化会使观察失效。
pinned revision 检查继续生效，读取中变化的文件拒绝交付。这项复用依赖可靠的
本地文件系统身份/stat 信息。大文件、BOM 文本、解析文档和自定义 handler 正文
走普通 exact read。写入权限、产物晋级和最终读回不受影响，也没有新增公共缓存
或索引 manager。旧 reader 失效后需要显式 refresh。

完整任务投影同样用于 long_task 内部的规划、执行和验证资料读取，保留各次读取的意图和阶段边界。
