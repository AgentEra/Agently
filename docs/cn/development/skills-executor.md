---
title: Skills 与 AgentExecution
description: SkillLibrary、TaskContext 渐进式披露和轻量 SkillsExecutor 兼容 facade。
keywords: Agently, Skills, SkillLibrary, AgentExecution, TaskContext, SkillsExecutor
---

# Skills 与 AgentExecution

真实世界的 Skill 是带 revision 的知识与工作方法包。`SKILL.md` 提供指导，索引资源
可以提供 references、examples、assets 或 scripts。Skill 不是执行 route、策略
引擎、Action 授权或 workflow。

## 所有权

| 层 | 所有者 |
|---|---|
| 安装、解析、revision、resolve、list、pack membership | `SkillLibrary` |
| 任务级 selector 意图、精确 revision binding、required/model-decision mode | `AgentExecution` |
| guidance/resources 渐进式披露 | `TaskContext` + `SkillContextSource` + `ContextReader` |
| ModelRequest、AgentTask、TaskDAG、workflow、副作用 | 原有执行所有者 |
| 已发布的管理形态兼容调用 | `Agently.skills_executor` 轻量 facade |

`SkillLibrary` 安装不可变、content-addressed revision。execution 绑定精确
revision，不绑定可变目录 alias。Skill description 可以交给语义模型 selector；
本地代码不得用关键词表或正则从自由文本任务中选择 Skill。

同一个 `Agently` application 中的 `Agently.skills_executor`、
`Agently.skill_library` 与新建 agent 共享同一个 canonical `SkillLibrary` 实例。
兼容 facade 只会原地重配这个实例，不会把 pack 安装到另一个全局 registry。
如果 pack 由 facade 安装，应通过 `Agently.skill_library` 解析 pack member，再把返回的
精确 revision 绑定到 agent execution。

## 推荐 Agent API

```python
contract = Agently.skills_executor.install_skills(
    "./skills/release-review",
    trust_level="local",
    update=True,
)

execution = (
    agent
    .use_skills([contract["skill_id"]], mode="required")
    .input("审查 3.2.0 发布候选。")
    .output({
        "decision": (str, "GO 或 NO-GO", True),
        "risks": ([str], "有证据支撑的发布风险", True),
    })
)
result = await execution.async_get_data()
```

`mode="required"` 以 fail-closed 方式绑定所选 revision。
`mode="model_decision"` 下，AgentExecution 用结构化 `ModelRequest` 从宿主发放
的 key 中选择，校验后绑定 revision；未知或重复 key 会 fail closed。

4.1.4.9 的适用性请求会同时考虑全部已声明目标、验收条件、原始 input，以及
system/info/instruct/output 中的任务约束和交付要求。可选 Skill 仍只返回候选 key；
没有适用项时可以不选。把后续阶段的已知要求在准备前写入本次 execution，避免
选择时缺少依据；这不会增加选择请求，也不会自动授予脚本执行权限。

Skills 与 Actions 使用同一种组合表达，不新增另一套公开集合 API。
`agent.use_skills(..., always=True)` 配置 Agent 默认可用集合，
`execution.use_skills(...)` 增加本次 execution 的声明。选择前，AgentExecution
把这些声明解析成 execution-scoped、revision-pinned 的快照，只向模型投影其中有界的
meta cards。没有 Skill 声明的 execution 不会扫描全局 SkillLibrary；准备完成后再安装
或修改其他 Skill，也不会静默扩大正在运行的 execution。

`agent.require_skills(...)` 是 required mode 的便捷方法。
`agent.use_skills_packs(...)` 把已安装的不可变 pack 展开为固定 revision refs。

revision 可用不等于已经被消费。只有披露后的 context package 绑定到一个具体的
ModelRequest response 时，AgentTask 才记录 Skill context consumption。该记录属于
上下文证据，不是可执行的 planner capability，也不是 Action evidence。

## `Agently.skills_executor` 仍然负责什么

facade 只保留已经发布的 Skill 管理和投影调用：

- 配置 SkillLibrary root 和允许的 trust label；
- 安装、列出、检查、读取 Skill package；
- 安装、列出、检查本地 Skill pack，以及已授权 Git/local source snapshot；
- 构造兼容 context-pack projection；
- 提供 TaskDAG `skill` resolver helper。

该 TaskDAG helper 是历史兼容 seam，4.1.4.8 延续 4.1.4.7 的限制，并不是已经完整接通的
`TaskDAGExecutor` integration。真实 executor 传入 `TaskDAGContext`，而 helper
消费 mapping-shaped projection，因此宿主仍需
适配这个边界。在框架拥有 adapter 和端到端 executor test 之前，不应把直接注册
描述为已验证路径；后续 release line 需要重新核验该限制。

它不负责 route selection、effort strategy、stage、React loop、runtime chain、
Blocks lowering、script execution、capability inference、自动 Action mounting 或审批。
已注册的 `SkillSourceProvider` 可以把已授权远程 source 落成不可变本地 snapshot；
SkillLibrary 只安装该 snapshot，并记录精确 provenance。
远程兼容安装默认使用 `untrusted`；只有调用方审查过不可变 revision 后才能显式提升 trust。
本地安装继续使用本地 trust 默认值。Git/local source 的选定 `subpath` 在解析时不会跟随
逃出已物化 source root 的 symlink component。

```python
pack = await Agently.skills_executor.async_build_context_pack(
    task="准备发布审查",
    skills=[contract["skill_id"]],
    include_references=True,
)
```

该方法创建临时 TaskContext，并使用与普通 execution 相同的 ContextReader
contract。仅为兼容保留的 `actionize_scripts=True` 仍把选中的 script 作为普通
resource descriptor 返回，并发出 `skills.compat.actionize_scripts_ignored`；它不会发现、
生成、挂载或授权 Action。

每个 Skill 投影保留 `action_candidates: []`，兼容已发布版本的字典读取方式。
无论该开关是否开启，此数组始终为空；脚本描述仍位于 `selected_resources`，
不是待注册的 Action 列表。

普通 AgentExecution 应先准备 Skill scope，再显式为所需语言启用一个受限的 script-exec
Action。模型只传相对 `script_path` 与有界 `args`；宿主根据本次 execution 已冻结的
精确 revision bindings 解析路径，并把实际 revision、path、digest 写入 Action evidence。
启用 Action 不会让已准备的 TaskContext 失效，也不会再次执行 Skill applicability
selection。

```python
from agently.types.data import SkillScriptAuthorization

await execution.async_prepare_task_context()
exec_action_id = agent.enable_skill_script_exec(
    execution,
    authorization=SkillScriptAuthorization(
        auto_allow=True,
        expected_outputs=("output/report.json",),
    ),
)
action_result = await agent.action.async_execute_action(
    exec_action_id,
    {"script_path": "scripts/check.py", "args": []},
)
artifact = next(
    item
    for item in action_result["artifacts"]
    if item["path"].endswith("output/report.json")
)
readback = await execution.task_workspace.read_file(artifact["path"])
```

`enable_skill_script_exec(...)` 为每个 Agent/语言复用一个稳定的普通 Action 定义，
并只在当前 execution 的 Action scope 和 execution context 中绑定授权；它不会为每个
script 或每个用户回合生成新 Action。若多个已绑定
Skill 含有相同相对路径，应收窄本次 execution 的 Skill 声明；也可以用已发布的
`bind_skill_script_action(...)` 兼容 API 显式绑定精确路径。不要只为执行 Skill script
调用 `enable_code_runtime(...)`，否则会额外暴露一个通用代码 Action。trust 是 package
provenance policy，不是脚本执行授权；只有成功 Action 记录加 TaskWorkspace readback
才能证明副作用和实际回收的 bytes。发布后的 artifact path 是
`.agently/files/.../code_execution/.../output/` 下的 TaskWorkspace-relative 私有路径。

### 同一任务的后续阶段与新用户请求

候选范围不等于已经绑定的 Skill。默认实现仅在准备 TaskContext 时进行初次适用性
选择；后续 `async_read_task_context(...)` 按 intent、consumer 和 phase 在已绑定来源
内选读资源，不会自动激活初次未选中的 Skill，也不会重扫全局 SkillLibrary。

如果已知某些 Skill 是整个任务（包括后续阶段）的必需指导，在启动前明确声明：

```python
execution = (
    agent.create_execution()
    .input(task)
    .require_skills([planning_skill_ref, delivery_skill_ref])
)
await execution.async_prepare_task_context()
```

这些 Skill 的根指导必需交付，可选资源仍按需读取。只声明确实必需的 Skill；这不保证
模型已消费指导，也不自动授权脚本。同一运行中按需激活尚未绑定 Skill 不是当前默认能力。

### 后续用户消息重新需要 Skill

每个用户请求使用一个新的 AgentExecution。Session 只延续对话和 memory，不延续上一轮
的 Skill bindings、Action scope 或脚本授权。把可能相关的 Skill 按普通 Action 组合语法
声明为 Agent 默认候选；每个新 execution 都根据当前消息重新判断是否选择它：

```python
agent.use_skills([contract["skill_id"]], always=True)

# 第一条消息不需要脚本：本次 selector 可以不选择该 Skill，也不启用 Action。
first = agent.create_execution().input(first_user_message)
first_result = await first.async_get_data()

# 后续消息提出脚本任务：必须创建新的 execution。
later = agent.create_execution().input(later_user_message)
await later.async_prepare_task_context()
if later.skill_bindings:  # 应用仍须执行自己的 allowlist / policy 判断
    agent.enable_skill_script_exec(
        later,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )
later_result = await later.async_get_data()
```

已经启动的 execution 和已经发出的 ModelRequest 都是快照，不能热注入新 Skill 或 Action。
如果在启动前、授权后又修改 prompt 或 Skill 声明，框架会撤销依赖旧 TaskContext 的脚本
授权和 Action 可见性；重新准备并再次显式授权即可。

## 已发布的执行便捷 adapter

`agent.run_skills_task(...)` 与 `agent.async_run_skills_task(...)` 保留为普通
AgentExecution 的 result-shaped adapter：

```python
compat = await agent.async_run_skills_task(
    "审查 3.2.0 发布候选。",
    skills=[contract["skill_id"]],
    mode="required",
    output={"decision": (str, "GO 或 NO-GO", True)},
)
print(compat.execution.id, compat.output)
```

adapter 不选择 `skills` route。execution 和其他请求一样使用
`model_request`，或调用方显式指定的 AgentTask strategy。需要 stream、meta、
TaskContext diagnostics、retry 或 lifecycle control 的新代码应直接使用
AgentExecution API。

## 上下文限制与渐进式披露

安装 Skill 不会把全部资源复制进每次 prompt。required `SKILL.md` guidance
在同一 ContextPackage 中只交付一次；完整 root 已存在时，它的 child section descriptors 不会再次提供给
selector 或重复进入 package。`SkillContextSource` 向 TaskContext 拥有的内部 ContextIndex 提供固定
revision 的 resource descriptor 与 exact read；resource index 与显式 refs 支持
后续 bounded read。structural、lexical 或可选 hybrid index 可以缩小可复用
candidate，但 TaskContext 仍是 aggregate，SkillLibrary 仍是 source truth。上下文
过大时，reader 返回 omissions、diagnostics 和可继续读取的 refs，不会把合成
summary 伪装成完整 source。

默认资源选择请求会收到本次已读的 instruction 内容及其完整性标记，用来理解
“什么阶段需要读哪个资源”等条件。它不会预读所有可选资源正文；选中后仍由
Host 验证候选键并精确读回。没有已读指引时，继续按 intent、phase 和候选卡选择。
这不增加选择节点，也不扩展 Skill 或 Action 权限；自定义 selector 的调用签名不变。
如果选中同一资源的完整正文和子章节，包交付会按已验证的来源身份与父子关系去重，
不依赖模型选择顺序。父正文未完整读回时保留子章节；已发生的读取仍计入预算。

按 consumer 和 phase 返回一份或多份有界信息块。完整文件和原始 evidence
留在 SkillLibrary、TaskWorkspace 或 RecordStore；hot model path 只携带当前任务
相关的 package。
Embedding/cache 计量与模型 prompt token 计量彼此独立；cache 复用本身不能证明
最终 prompt token 更少。

## 副作用

Skill 描述工作方法。Host code、ActionRuntime、ExecutionResource、
TaskWorkspace、RecordStore、TaskDAG 与 TriggerFlow 继续承担原有责任。Skill
不能静默授予 filesystem、network、MCP、credential 或 process 权限。
