---
title: 任务上下文、文件与记录
description: TaskContext、ContextReader、TaskWorkspace 与 RecordStore 的职责边界。
keywords: Agently, TaskContext, ContextReader, TaskWorkspace, RecordStore, 渐进式披露
---

# 任务上下文、文件与记录

Agently 把过去混在 Workspace 里的职责拆成四个所有者。

| 所有者 | 负责 | 不负责 |
|---|---|---|
| `TaskContext` | 任务信息聚合、直接信息块、source binding 与一套内部派生索引生命周期 | 文件、持久化、语义执行路由 |
| `ContextReader` | 绑定 consumer/phase 的检索与渐进式披露；返回 `ContextPackage` | source 存储、写入、副作用 |
| `TaskWorkspace` | 明确的任务文件边界：路径约束、写入策略、格式化 readback、digest 与 file refs | records、memory、snapshot、Skill 选择 |
| `RecordStore` | 持久 records、检索索引、links、checkpoints、TriggerFlow snapshots/events、memory 持久化 | 任务文件、prompt 组装、语义相关性判断 |

`ContextSource` adapter 让不同来源的信息可被读取，但不会把 source truth
搬进 `TaskContext`。source 通过 `async_enumerate_descriptors(...)` 暴露结构描述，
通过 `async_read_exact(...)` 返回有界 canonical 内容；它不判断跨 source 相关性。
内置 adapter 覆盖 SkillLibrary、TaskWorkspace、RecordStore 与 SessionMemory
recall；应用也可以挂载自己的 source kind，例如经授权的固定仓库 adapter。

source 还可以实现可选的 `ContextSourceScopedRead` protocol。ContextReader 只在
canonical ref 已经选定并通过授权后使用它，在该 ref 内定位一个确定性的有界范围。
这是 source mechanism，不是第二套 index 或语义相关性 owner；未实现时仍回退
`async_read_exact(...)`。

## 文件边界：TaskWorkspace

```python
from agently import Agently, TaskWorkspace

task_workspace = TaskWorkspace("./project", mode="read_only")
agent = Agently.create_agent("repo-review").use_task_workspace(
    "./project",
    mode="read_only",
)
```

配置路径就是普通文件根目录。除非明确选择 `mode="read_write"`，现有外部
文件保持只读。只读边界需要生成新制品时，Agently 使用
`.agently/files/<execution-id>/` 下的 execution fallback，不覆盖现有外部
文件。TaskWorkspace 的 locator 与 content-version 私有身份信息只保存在它
自己的 `.agently` 区域。

未显式指定路径的 Agent 使用
`<入口目录>/.agently/task_workspaces/<agent-id>`，因此不同 Agent 默认不会
悄悄共享任务文件边界。

只有任务确实需要时，才把文件 Action 暴露给模型：

```python
agent.enable_task_workspace_file_actions(
    read=True,
    write=True,
    expose_to_model=True,
)
```

文件格式扩展仍由同一个 owner 承担。把 `TaskWorkspaceFileIOHandler` 直接注册到
已绑定的 `TaskWorkspace`；不存在另一套可能与实际文件边界发生 registry 漂移的
Workspace manager 或 factory：

```python
task_workspace.register_file_io_handler(custom_file_handler)
```

TaskWorkspace 为宿主 readback 提供稳定的 locator 与 content-version facts。
`[[ref:ref_1]]` 这类短引用只是请求内显示别名，不是持久身份；宿主必须校验
它，再映射回 canonical reference 身份。

AgentTask 的 required 最终交付会先写成暂存候选，并在 terminal verification
前完成全文读回。只有 verifier 验收后，TaskWorkspace 才按 digest 对声明目标执行
原子提升，并再次完整读取提升后的 bytes。verification 拒绝不会覆盖旧目标；提升
或提升后读回失败会把任务转为 blocked，而不是声称已经交付。

## 持久化边界：RecordStore

```python
from agently.core.storage import RecordStore

record_store = RecordStore("./project-state", mode="read_write")
agent.use_record_store(record_store)

ref = await record_store.put(
    {"status": "verified"},
    collection="observations",
    kind="review_result",
    scope={"task_id": "review-42"},
)
```

本地 provider 把 records 写到
`<root>/.agently/records/records.db`。绑定 RecordStore 不会创建或改变
TaskWorkspace。`SessionMemory` 和 TriggerFlow durability 使用 RecordStore
ports。

TriggerFlow 可以用 `record_store=False` 关闭默认 RecordStore view，或绑定
显式 store：

```python
execution = flow.create_execution(
    record_store=record_store,
    runtime_resources={"runtime_event_store": record_store},
    auto_close=False,
)
```

AgentTask 过程状态默认只保留在内存和运行日志中。只有需要重启恢复时才启用
`record_store_recovery`。恢复引用属于 RecordStore；最终交付文件仍属于
TaskWorkspace。

### 检索重排

`record_store.retrieve(...)` 拥有重排策略：结构 gate、候选窗口、丢弃、回填与打包。
排序可由三种引擎提供，优先级如下：

| 引擎 | 何时选中 | 行为 |
|---|---|---|
| `handler` | 传了 `rerank_handler=` | 由你的回调决定 |
| `provider` | 绑定了 rerank provider | 重排模型给出相关性分数 |
| `model` | 两者都没有 | 默认的 ModelRequest 有用性判断 |

绑定重排模型的方式与绑定 embedding 模型一致 —— 通过组件 provider：

```python
from agently import Agently
from agently.base import record_store_registry

rerank_agent = Agently.create_agent()
rerank_agent.set_settings("OpenAICompatible", {
    "base_url": "${ENV.RERANK_BASE_URL}",
    "api_key": "${ENV.RERANK_API_KEY}",
    "model_type": "rerank",
    "model": "${ENV.RERANK_MODEL}",
})

record_store = record_store_registry.create(
    "./project-state",
    mode="read_write",
    rerank_provider="agent",
    rerank_options={"agent": rerank_agent},
)

package = await record_store.retrieve("怎么给框架接本地模型服务", rerank=True)
print(package["diagnostics"]["rerank"])  # engine、provider、scored、unscored、dropped
```

`rerank_provider` 接受 `"agent"`、`"callable"`（配合
`rerank_options={"rerank_function": fn}`）、普通可调用对象，或任何实现了
`RerankProvider.rank(query, documents, *, top_n)` 的对象。绑定是惰性的：
配置 provider 本身不会发起模型调用，真正执行重排时才会。

`RerankProvider` **只拥有打分**。因为重排模型给的是相关性而不是有用性，
provider 引擎默认**只重排序、不丢弃**；下游打包仍会按 `top_n` 和长度预算裁剪。
确实需要按分数截断时传 `budget={"rerank_score_floor": 0.5}`，被截掉的部分
会以 `rerank_drop` 出现在 `omitted` 中。provider 没有打分的候选保持原有相对顺序
排在已打分候选之后，不会因为「没有证据」就被丢掉。provider 失败时检索降级为
确定性顺序并记录 diagnostics，**不会**静默切换到模型引擎 —— 引擎选择是你的配置，
不是失败后的隐式升级。

## 信息交付：TaskContext 与 ContextReader

```python
from agently.core.context import TaskContext
from agently.core.storage import RecordStoreContextSource
from agently.types.data import ContextBudget, ContextReadIntent

task_context = TaskContext("review-42")
task_context.put(
    role="instruction",
    content="审查期间不得修改源文件。",
    required=True,
)
task_context.attach(
    RecordStoreContextSource(record_store),
    binding_id="review-records",
    scope="task",
)

reader = task_context.reader(
    consumer="review-planner",
    phase="planning",
    budget=ContextBudget(max_chars=6000, max_blocks=12),
)
package = await reader.async_read(
    ContextReadIntent(
        query="哪些证据与失败的审查相关？",
        filters={"source_kinds": ["record_store"]},
    )
)
```

`TaskContext` 是唯一的任务信息 aggregate。TaskContext 负责 source bindings 与
一套内部 `ContextIndex`：构造、同步、失效并复用派生 source partition；它不是公开
manager，也不是 canonical source truth。TaskContext 通过
`task_context.reader(...)` 创建 reader，并通过
`task_context.restore_reader(...)` 恢复已导出的 reader state；不支持脱离
TaskContext 独立构造或恢复 `ContextReader`。reader 是公开的、绑定
consumer/phase 的句柄，类似由 aggregate 持有的 execution handle；
`ContextPackage` 是跨 ModelRequest、AgentTask、Blocks 或持久化边界传递的不可变值，
不是另一个 context owner。

每个 reader 固定一份 TaskContext/source revision 快照；读取开始前已经过期时应显式
refresh 或创建新 reader。如果列举候选本身推进了 source revision、但 TaskContext 结构
未变化（例如 source 首次建立惰性读视图），ContextReader 会重新固定新 revision 并重取
一次；持续或并发变化仍然 fail closed。required 和显式请求的信息块不能被静默丢弃。多个可选 prose
candidate 需要相关性判断时，使用 Agently `ModelRequest` semantic selector；
模型只返回宿主发放的 selection key，宿主校验后再重建 canonical record。

原生 `RecordStoreContextSource` 的版本包含绑定视图的有效查询范围和可见记录元数据。
范围外写入不使该视图过期，可见记录变化仍需刷新；页面与精确读回绑定同一次只读事务。
该 source 的精确读取拒绝范围外记录，但公共 `RecordStore.read_bounded()` 不因此成为
权限边界。自定义 provider、读取覆盖或显式 source revision 保留其原有路径，不自动获得
原生范围版本保证。原生版本检查仍扫描元数据；巨量记录的成本需要按实际规模验证。

ContextIndex 把 source descriptor 枚举成以 revision/profile/provider 为 key 的
partition，可使用 `structural`、`lexical` 或宿主配置的 `hybrid` 候选检索；精确 bytes
仍由 source 的 `async_read_exact(...)` 返回，或在 ref 选定后由可选的确定性 scoped-read
端口返回。可复用 partition 可以避免重复构建未变化
的 embedding；只有 policy 允许时 vector failure 才降级，并写入 package diagnostic。
ContextReader 负责 consumer-local offset、去重、可选 ModelRequest selection、精确
readback 与 package budget。返回 package 暴露逐 binding 的 `source_coverage` 与 index
diagnostic，不暴露内部 cache key 或 provider vector。

不可变 ContextPackage 保留完整 omission 与 diagnostic 事实用于审计。AgentTask 的
model-hot view 会限制重复的可选 omission 明细并增加原因计数；required delivery 仍在
该投影之前 fail closed。

Context 交付会区分媒体类型。纯文本以及由来源解析出的文本可以进入 package；内置
TaskWorkspace source 会先解析受支持的 PDF、DOCX、XLSX 与 PPTX 文件。若解析器或
可选依赖不可用，该文档只交付引用。PDF 或 Office 的 descriptor 与 exact read 必须
同时保持 `context_representation=parsed_text`，且精确读取结果必须是文本；调用方提供但
没有解析来源证明的字符串不能进入上下文。已知的非文本 MIME 或文件后缀不能被冲突的
`content_kind="text"` 声明覆盖；类型信号冲突时按非文本或 unknown 保守关闭。Python、
Node.js、Go、C 与 C++ 的主流源码后缀按文本处理，包括空源码文件。

图片、压缩包、可执行文件、音视频、未知格式和任意二进制字节都不会被强制转换为猜测
文本；模型侧投影只包含规范文件名/引用。来源提供的摘要、OCR 文本和推测内容会被移除；
MIME、摘要哈希和大小等事实只能在宿主侧作为审计元数据保留。

图片只有在具体 consumer 显式声明支持图片附件时才会进入附件通道，否则只交付引用：

```python
from agently.types.data import ContextConsumer

reader = task_context.reader(
    consumer=ContextConsumer(
        "visual-reviewer",
        capabilities={"attachments": {"image": True}},
    ),
)
```

AgentTask 使用同一能力声明：

```python
execution = agent.goal("Review the attached chart").strategy(
    "taskboard",
    context_consumer_capabilities={"attachments": {"image": True}},
)
```

Agently 不根据模型名推断视觉能力，通用的 `attachments=True` 也不代表支持图片理解。
显式支持时，ContextReader 生成经过校验的图片附件块，AgentTask 通过 ModelRequest
附件通道绑定；data URL 不会序列化进文本 context pack。没有该显式能力时，模型只会
收到文件名/引用，不会收到生成的摘要或 OCR 替代内容。图片理解仍由模型负责。附件为空
或格式非法时，本次读取失败，不会退化为根据文件名猜测内容。

required 内容超出预算时默认仍然 fail closed。只有 Skill 或调用方显式接受有损投影
时，才可设置 `metadata={"required_overflow": "lossy_digest"}`。此时 Skill source
返回有界、`completeness="lossy"` 的结构化纲要，并保留不可变全文 ref、有序 section
refs、原始长度与省略事实；不会把静默截断伪装成完整权威指令。可选 section 仍由语义
selector 选择。只需要 required core、明确不做可选选择的 host preflight 还可以设置
`optional_selection="none"`。

AgentTask 通过同一份 context budget 传递该策略：

```python
execution = agent.goal(goal, success_criteria=criteria).strategy(
    "taskboard",
    context_budget={
        "chars": 12_000,
        "required_overflow": "lossy_digest",
    },
)
```

只有明确接受有损披露时才使用该设置；否则应换用更大/更聚焦的 consumer，或让
required Skill 在业务执行前失败。

`source_kinds` 是结构性来源过滤，不是语义路由，也不是封闭枚举。有效值来自当前
TaskContext 实际挂载的 source kind；存在相应 adapter 时可以包括
`task_workspace`、`record_store`、`skill_library`、`session_memory`、`pinned_repository` 等。
未知 kind 会在 source 枚举前失败。

AgentTask 为每个实际 planner、worker、control card 与 verifier 请求创建独立的
reader/package。只有响应成功后才记录 `ContextConsumption`，其中保留精确的
package、response/request id、phase 与 block ids；失败请求不记录 consumption，也
不发出 `skills.context.bound`。AgentTask meta 通过 `context_packages` 与
`context_consumptions` 暴露审计信息。

## AgentExecution 所有权

每个 AgentExecution 拥有一个 TaskContext，以及 Agent 的 TaskWorkspace 与
RecordStore 的 execution-scoped view。AgentTask 复用 AgentExecution 交给它的
同一个 TaskContext 和 TaskWorkspace view。Skills 以不可变 SkillLibrary
revision 绑定为 Skill ContextSource，不会创建 Skills route 或执行引擎。

启用 `record_store_recovery` 后，持久快照同时保留 TaskContext 直接条目、可重建的
内建 source bindings、reader 披露历史、packages 与 consumptions。Skill source
按不可变 `revision_ref` 精确重建；自定义 ContextSource 不会被自动重建，会在
resume 时明确失败而不是静默消失。

普通 AgentExecution 使用
`execution.async_read_task_context(consumer_id=..., phase=..., intent=...)`
构造信息包。`intent` 可传查询字符串或 `ContextReadIntent`；省略时使用
AgentExecution 的 task target。
Blocks 的只读 `context_read` block 接收调用方已经绑定好的
`context_reader`。
