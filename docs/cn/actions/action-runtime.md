---
title: Action Runtime
description: TriggerFlow 之下的 action 架构 —— ActionRuntime、ActionFlow、ActionExecutor 与 Agently.action 入口。
keywords: Agently, action runtime, ActionRuntime, ActionFlow, ActionExecutor, action_func, use_actions
---

# Action Runtime

> 语言：[English](../../en/actions/action-runtime.md) · **中文**

Agently 的 action 栈在编排层之下有三个可替换插件层：

```text
   TriggerFlow                  ◄── action 之上的编排（loop、分支、pause/resume）
       │
       ▼
   ActionRuntime                ◄── 规划 + 派发
       │  （ActionFlow 是与编排层的桥）
       ▼
   ActionExecutor               ◄── 原子执行（local function、MCP、sandbox）
```

## 各层详细

| 层 | 拥有什么 | 默认 builtin |
|---|---|---|
| `TriggerFlow` | action 之上的高层编排（loop、分支、pause/resume、子流）—— 见 [TriggerFlow](../triggerflow/overview.md) | `TriggerFlow` 核心 |
| `ActionRuntime` | 规划协议、action 调用归一化、默认执行编排 | `AgentlyActionRuntime` |
| `ActionFlow` | `ActionRuntime` 与 flow 表示之间的桥 | `TriggerFlowActionFlow` |
| `ActionExecutor` | 单个 action 实际怎么跑 | local function、MCP、Python/Bash sandbox、Search/Browse、Node.js、常用代码 runtime、Docker、SQLite executors |
| `ExecutionResource` | executor 调用前需要准备的托管执行依赖 | MCP、Bash、Python、Node、Docker、Browser、SQLite providers |

`Action` 是 `agently.core` 根导出的执行门面，连线：

- `ActionRegistry` 与 `ActionDispatcher`（稳定核心原语）
- 一个 active `ActionRuntime` 插件
- 一个 active `ActionFlow` 插件

默认连线：

```text
Agent → ActionExtension → Action 门面 → ActionRuntime → ActionFlow → ActionExecutor
```

## 插件类型

可替换的插件类型：

- `ActionRuntime` —— 改规划协议或调用归一化
- `ActionFlow` —— 改编排形态（如自定义 flow 表示）
- `ActionExecutor` —— 加新后端（HTTP、gRPC、自定义沙箱、远程 worker）

从 `agently.types.plugins` import 协议与 handler 别名：

```python
from agently.types.plugins import (
    ActionExecutor,
    ActionRuntime,
    ActionFlow,
    ActionPlanningHandler,
    ActionExecutionHandler,
)
```

> 旧的 `ToolManager` 插件类型与 `AgentlyToolManager` 类仅作为遗留兼容保留；deprecation warning 按 deprecated API 在每个 Python 进程内只发一次，除非关闭了 `runtime.show_deprecation_warnings`。新插件不要写在 `ToolManager` 上。

## 推荐入口 —— actions

新代码：

```python
from agently import Agently

agent = Agently.create_agent()


@agent.action_func
async def add(a: int, b: int) -> int:
    """两个整数相加。"""
    return a + b


@agent.action_func
async def python_code_executor(python_code: str):
    """执行 Python 代码并返回结果。"""
    ...


agent.use_actions([add, python_code_executor])

# 或一次性注册并执行
@agent.auto_func
def calculate(formula: str) -> int:
    """计算 {formula}。使用可用的 action。"""
    ...

print(calculate("3333+6666=?"))
```

| 入口 | 用途 |
|---|---|
| `@agent.action_func` | 标记函数为 action，从签名 + docstring 推 schema |
| `agent.use_actions(actions)` | 在 agent 上注册 list、单个 action 或字符串名 action |
| `agent.use_actions(["name1", "name2"])` | 按名注册预注册的 action |
| `agent.use_actions(Search(...))` | 挂载来自 `agently.builtins.actions` 的内置 Search package |
| `agent.use_actions(Browse(...))` | 挂载来自 `agently.builtins.actions` 的内置 Browse package |
| `agent.enable_python(...)` | 挂载默认 Docker-backed 的 `run_python` action，用于确定性代码执行 |
| `agent.enable_shell(...)` | 挂载默认 Docker-backed、带 workspace root、命令 allowlist、timeout 和有界输出预览的 `run_bash` action |
| `agent.enable_nodejs(...)` | 挂载默认 Docker-backed 的 `run_nodejs` action |
| `agent.enable_code_runtime(...)` | 挂载 Workspace-backed、provider-neutral 代码执行 Action，支持 Python 3.10+、Node.js 18+、Go 1.25+ 和 C++20 |
| `agent.enable_sqlite(...)` | 挂载托管 `query_sqlite` action |
| `agent.enable_task_workspace_file_actions(...)` | 把当前 TaskWorkspace 文件作业区暴露成 handler-backed 列表、搜索、读取、写入 actions；`export=True` 且 `write=True` 时额外暴露 `export_file` |
| `agent.enable_coding_agent_actions(...)` | 暴露 coding-agent TaskWorkspace actions，用于文件读回、glob/grep 检索、定点编辑、unified diff patch 和带 guard 的整文件写入 |
| `@agent.auto_func` | 把 Python 函数签名 + docstring 变成模型驱动的实现，使用 agent 的 action |
| `agent.get_action_result(prompt=turn.prompt)` | 取 request-scoped turn 的 action 调用记录 |
| `extra.action_logs` | action loop 期间产生的结构化日志 |

内置多 Action package 使用原子注册批次。如果 Search 或 MCP tool 注册在任意阶段
失败（包括 MCP client 退出），Agently 会移除本批新增 Actions，并恢复同名宿主
Action 原有的 spec、executor、function 与 tags。失败的 package mount 不会留下
部分 Actions，也不会覆盖宿主持有的注册。

`agent.action.get_action_info()` 和 `agent.action.get_tool_info()` 默认返回该
agent 上可见的 action/tool schema，包括 agent-scoped actions、通过
`agent.use_mcp(...)` 挂载的 MCP tools，以及 `enable_*` component helpers。只有需要
窄范围子集时才传显式 `tags=[...]`。托管执行环境 metadata 在这个可见 schema
里会脱敏原始 `env` 值，但保留 env key；provider 只会在实际执行路径中拿到 raw env。

应用代码要给模型开放 Python、shell、workspace 等常见能力时，优先使用
`enable_*` helpers。只有在开发自定义 Action 后端时，才需要使用
`register_action(..., executor=..., execution_resources=[...])`。

`agent.enable_python(...)`、`agent.enable_shell(...)` 和
`agent.enable_nodejs(...)` 默认使用 `sandbox="auto"` 与
`provisioning_profile="strict"`。Python 与 Node.js 是 Workspace-bound
`code_execution` 契约上的语言快捷入口；shell 仍是更宽的命令 Action。默认 provider
路径会先检查本地 Docker CLI 与 daemon，再通过 Docker 执行。缺失镜像默认
`image_pull_policy="never"`，会用 `execution_resource.docker_image_missing`
等结构化 diagnostics fail closed，不会静默退回到宿主进程执行。只有明确接受无隔离宿主
Python、shell 或 Node.js 执行的可信路径，才应显式传 `sandbox="trusted_local"`。

`agent.enable_code_runtime(...)` 为所有受支持语言暴露同一条 provider-neutral 主链。每次调用先绑定
TaskWorkspace grant，再选择合格的 `code_execution` provider、落地不可变 source
bundle、执行 adapter 持有的 argv plan，最后通过 TaskWorkspace 读回声明的输出。
provider 顺序既可全局配置，也可在单个 Action 上配置：

```python
agent.settings.set(
    "code_execution.providers",
    [
        {"provider_id": "remote-or-platform-provider", "config": {}},
        "docker",
    ],
)
agent.enable_code_runtime(language="python")
```

默认隔离要求 fail closed。无防护宿主进程 runner 绝不会静默兜底；只有同时显式允许
fallback 并降低隔离要求才会启用：

```python
agent.enable_code_runtime(
    language="python",
    unsafe_fallback=True,
    isolation="preferred",
)
```

运行时契约见 [Execution Resource](execution-environment.md)，贡献者迁移说明见
[Code Execution Provider 迁移](../development/code-execution-provider-migration.md)。

Coding Agent、Agently Skills、examples 和框架测试可使用
`provisioning_profile="developer"` 或 `"ci"`。这些 profile 默认
`image_pull_policy="if_missing"` 且 `dependency_policy="install"`，provider 可以自动
拉取缺失 Docker 镜像，并在固定 entrypoint 执行前从标准 manifest 准备依赖。依赖安装不是
模型可见的 action input；不要让模型通过代码 action schema 直接运行 `pip`、`npm`、
`cargo` 或其他包管理命令。

coding-agent 风格的本地文件工作优先使用
`agent.enable_coding_agent_actions(...)`。它会在当前 TaskWorkspace file root 上暴露
`read_file`、`glob_files`、`grep_files`、`edit_file`、`apply_patch` 和带 guard 的
`write_file`。`edit_file(...)` 可使用 `expected_sha256` stale guard；
`apply_patch(...)` 应用 unified diff，并可要求精确的 `expected_files`；
coding-agent mode 下的 `write_file(...)` 默认要求先读过目标文件或提供 expected hash，
除非 host 显式关闭这个 guard。测试、构建、git inspection 和只读诊断使用 shell；
文件读取、检索、编辑和写入使用 TaskWorkspace file actions。

`agent.enable_shell(...)` 不传显式 `commands=...` allowlist 时，Agently 会使用小型
safe shell profile，例如 `pwd`、`ls`、`rg`、`cat`、`git status`、`git diff`、
`git log`、`python -m pytest` 和 `python -m pyright`。stdout/stderr 以有界 preview
返回；某个 stream 超过 `max_output_chars` 时，完整 stream 会写入 TaskWorkspace root 下的
当前执行的 `.agently/files/<execution-id>/shell-output/` fallback，并在
action result 中返回引用。

`run_bash.workdir` 默认相对于注入的 workspace root；只有 host 提供的 absolute path 已在
该 root 内时才会接受。既可以使用 `.` / child path，也可以使用当前 TaskWorkspace 暴露的
逻辑 `.agently/files/<execution-id>` locator；host 对 root-prefixed logical locator 只消费
一次，不会把它再次拼到 physical root 后面。parent traversal 或任何逃出注入 root 的路径
都会 fail closed。
`allow_unsafe` 是 host-only 的直接执行授权，不会出现在模型可见的 shell action schema
中；模型计划出的 action input 里即使包含该字段也会被清洗。模型选择的命令超出 safe
profile 时，应通过需要审批的 action 或 ExecutionExchange provider 路由，而不是允许
模型输出自行授予 bypass。
自定义 action 如果需要仅 host/direct call 可用的参数，可以用
`meta={"host_only_input_keys": [...]}` 声明；Action Runtime 会从模型计划出的
`structured_plan` 和 native tool-call 输入里清洗这些 key，同时保留 host/direct call。

Action spec 还会携带 `required_input_keys`。`@agent.action_func` 会从没有默认值的
函数参数自动推导；基于 executor 的注册应通过
`register_action(..., required_input_keys=[...])` 显式声明，或者在 kwargs 描述元组中
使用第三个布尔值，例如 `(str, "Search query", True)`。Agently 会把同一约束写入
native tool JSON Schema，并在 dispatch 前拒绝缺少必填 key 的结构化或原生模型命令。
host 直接调用仍保留 executor 或 Python 函数原本的校验语义。

内置能力 package 位于 `agently.builtins.actions`。例如：

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15, backend="auto"))
agent.use_actions(Browse())
```

Search 是 Action-native package，不进入 ExecutionResource；proxy、timeout、
backend、region 都属于 package/executor 配置。Browse 也是 Action-native；默认主线是
Jina Reader -> Playwright -> BS4 -> restricted curl，pyautogui 保留为
legacy/advanced 配置。curl backend 是 Browse 内部的 URL fetch fallback，不是暴露给模型的
shell access。Jina Reader 会把目标 public URL 交给 `https://r.jina.ai/` 做
URL-to-Markdown 恢复；当默认 Reader endpoint 出现传输或服务错误时，会自动尝试官方替代
endpoint `https://r.jinaai.cn/`。如果应用不能接受这个外部服务边界，可以显式关闭：
`Browse(enable_jina_reader=False, fallback_order=("playwright", "bs4", "curl"))`。
如果 Browse action 需要托管 browser/page/session，可以启用 Browser ExecutionResource provider。

Agent Client Protocol（ACP）coding agent 作为 Action capability 暴露，不是
AgentExecution route。使用 `agent.use_acp(on_missing="skip")` 可以扫描本地
ACP endpoint 和内置本地 coding-agent CLI adapter，并且只在存在已验证可运行 agent 时注册
`acp_list_agents` 和 `acp_run_task`。`acp_list_agents` 还会返回非绑定的常见
ACP adapter 名称提示，例如 `codex`、`claude code` / `cc`、`openclaw`、
`hermes` / `hermes agent` 和 `gemini`；这些提示不会让 agent 变成 runnable。
内置本地 CLI adapter 会检查常见 Codex 和 Claude Code 命令位置以及当前进程 `PATH`，
使用框架固定 argv 模板，不暴露模型可见 shell execution。
默认 `on_missing="skip"` 只记录 diagnostics，不会伪造 runnable agent；
`on_missing="error"` 会 fail closed。ACP run action 会声明
`ExecutionResource(kind="acp")`，让 root scope 和 lifecycle 事实留在 resource 层。
如果省略 `root`，`agent.use_acp()` 使用当前 Agent 绑定的 TaskWorkspace `root`
作为 coding-agent project root；只有 host 明确授权另一个项目目录时才传入
`root=...`。ACP session 复用是 AgentExecution 内部 resource policy，不是普通任务启动
选项。CLI adapter 会标记 `acp_session.persistence="stateless_cli"`，除非存在真实可恢复的
ACP protocol session。

AgentTask 也可以在 bounded step 或 TaskBoard card 执行失败、且配置的重试耗尽后，
把 ACP 作为显式启用的 recovery fallback。这个路径仍然调用已注册的 `acp_run_task`
Action，并使用 `ExecutionResource(kind="acp")`；ACP 不是绕过 AgentExecution 或 task
strategy policy 的新 route。如果 host 从未调用 `agent.use_acp(...)`，fallback 只会记录
skipped diagnostics，不会导入 ACP 依赖，也不会伪造可用 agent。

`enable_*` helpers 的 `desc=` 是可选项。默认会作为补充说明追加，确保模型仍然看到基础用法和安全边界。
如果你确实要替换默认描述，使用 `desc_mode="override"`；如果要忽略传入描述、只保留内置描述，使用
`desc_mode="default"`。

## 模型来源输入安全

模型规划产生的 Action command 在 Action 边界被视为不可信输入。对于
`structured_plan` 和 `native_tool_calls` command，`ActionDispatcher` 会在调用
executor 之前，把 `action_input` 过滤到注册时 `ActionSpec.kwargs` 声明过的 key。
host 的 `direct` / `dry_run` 调用保持既有行为，不做这类过滤。

被过滤的调用会在 `ActionResult` 上保留结构化 diagnostics，包括
`action.input.unexpected_keys_stripped`、被移除的 key，以及原始 kwargs 和实际执行 kwargs
的有界预览。timeout 和 executor exception 也会返回带 diagnostics 的结构化 Action failed
结果。RuntimeEvent 消费者可以观察这些事实，但 RuntimeEvent 不负责输入过滤或授权。

## 执行回溯

`run_bash`、`run_python`、`run_nodejs`、`query_sqlite`、`browse`、`search`
这类指令型 action 会记录一份执行 digest 和一组 artifact references，用来控制后续模型上下文长度。

后续 action planning round 默认看到的是 digest。它包含 action id、call id、目的、状态、精简指令预览、
结果预览、preview 截断元数据、脱敏说明、artifact candidates，以及 Action 返回的 TaskWorkspace file refs。
完整代码、shell 输出、SQL 结果集、页面 HTML、截图、日志等原始内容会以脱敏 artifact 形式保留，
不会默认塞进每一轮 prompt。每个 model-visible candidate 只携带一个 host-issued
`selection_key`，以及 role、media type、label、有界 preview 等任务相关事实。
canonical artifact id、call id、scope、digest、size 与 provenance 仍由 host 持有，
不会让模型复制。
artifact 选择与 Action evidence 绑定是两个不同合同。当结构化 task result 要求模型把
claim 绑定到 Action evidence 时，每个候选 Action result 会携带宿主签发的
`action_call_id`。模型只返回本轮已经提供的 call id；host 校验它属于该结果集合后，
再确定性解析成 canonical EvidenceLedger identity。`action_call_id` 既不是模型生成的
canonical id，也不是 artifact readback selector。

### Required Action evidence

AgentTask 的完成条件要求 `action_succeeded` 时，requirement 必须指向精确 capability
id 与 kind。TaskWorkspace readback 不能满足指定 Action：一个可读文件可以证明文件内容，
但不能证明 required callable capability 已成功执行。如果精确 Action 已挂载，TaskBoard
可以创建 Action-shaped repair，并让同一份结构化 id/kind contract 穿过 dispatch 与
evidence binding；它不会从 verifier prose 推断 Action，也不会特判 Action name。AgentTask
会在语义 terminal verifier 之前确定性检查这份 evidence contract，因此缺失 Action 会直接进入
capability-directed repair，不会先消耗 verifier 请求，也不会被 verifier output 问题遮蔽。

如果 final verification 或 grounding 要求 repair，但没有精确的 `action_succeeded`
requirement，可信 file-backed factual-grounding repair 会使用 control-shaped 的有界
TaskWorkspace patch，Flat 与 TaskBoard 都遵守这条路径。patch proposal 来自结构化
ModelRequest；该 repair 不会打开通用 AgentExecution/ActionRuntime round，因此已挂载的
`write_file` 或无关 Action 不能改写整篇 artifact。host 会在应用并 readback 之前校验授权
path、每个 `claim_key` 恰好一个 operation、精确 `old_string` scope 与当前
`content_version_id`。其它 repair 保留普通 `auto` execution shape，使已挂载 Actions 仍可用于
收集新证据。两种情况中 host 都不会从 verifier prose 推断 Action id。存在精确结构化 Action
gap 时，仍使用上面的收窄 Action-shaped route。

不可用的 required Action 会 fail closed，不会调度一个看似等价的 read、tool 或纯模型
替代操作。Action policy 被拒绝或阻塞时同样 fail closed。这与 required Skill
availability 分开：required remote Skill 必须在 AgentTask 业务工作开始前完成 discovery、
installation 与 inspection，因此缺少 required Skill 时产出的 artifact 不能在终态门禁中
补票通过。

显式返回 `artifacts` 或 `artifact_refs` 的 action 即使输出很小也使用同一合同。
这包括 `MCPActionExecutor` 暴露的 MCP resource/content block；Agently 记录
声明过的 artifact metadata，但不会通过扫描目录推断未声明的文件写入。
如果宿主 action 会生成供 AgentTask 或 TaskBoard 后续消费的文件，建议返回带
path、size/bytes、media type，以及可用时 SHA-256 的 typed `file_refs` 或
`artifact_refs`。只有 `{filename, path, size}` 这类 path-only payload 时，Agently
会把它保留为有界 Action result evidence 和 ref pointer；只有当 path 位于
TaskWorkspace files root 内且 TaskWorkspace readback 成功时，才会升级为可信 TaskWorkspace
file ref。

对于已声明的 AgentTask 交付路径，成功的文件生成 Action 是写入 owner。AgentTask 会
readback 并采用这一份确切的 TaskWorkspace 文件，不会在终态 materialization 中再用模型返回
的 artifact 正文覆盖它。更新文件必须再执行一次显式文件 Action；如果成功 Action 声明
的路径无法 readback，artifact delivery 会 fail closed，而不是用模型 prose 替代。

Search、Browse 等内置 Web actions 在运行时不会弹出包安装确认。可选依赖缺失会
作为结构化 Action failure 暴露给宿主，由宿主决定安装、重试或降级。
如果 digest 对后续规划或回复 hot path 仍然过大，Agently 会再次压缩模型可见 digest：
`result` 保留有界 digest，重复的 `data` / `model_digest` 字段可能变成
`same_as="result"` 指针，artifact refs 会省略 preview 正文但保留 readback id。
这个压缩只作用于 hot-path 模型上下文；完整脱敏内容仍留在 Action artifact store 里，
需要显式读回。

当所属 ActionFlow scope 仍处于 live 状态时，模型可以通过内置 readback Action
请求被省略的细节：

```python
readback_call = {
    "action_id": "read_action_artifact",
    "action_input": {"selection_key": artifact_candidate["selection_key"]},
}
```

这是 flow 内 readback 合同。standalone ActionFlow 返回后，candidate 会如实报告
`available=false`；应用应使用已 durable promotion 的 TaskWorkspace ref，而不是读取已释放 scope。
公开 readback selector 只有 `selection_key`。Agently 会在当前绑定的
AgentExecution、AgentTask 或 standalone ActionFlow artifact scope 内解析它；TaskBoard
host 代码绑定当前 task lineage，使同一任务的 sibling cards 可以消费同一 retained artifact。
缺少 scope、跨 task 或跨 execution 访问会 fail closed。canonical artifact id 与
Action call id 不是备用 selector。

传入 `max_bytes` 时，成功的 readback 是一页显式有界的渐进式披露结果。下一轮
planning 会内联收到该页，以及其类型化的 `owner`、`locator`、`content_version`
和字节范围；Agently 不会再次外置这一页，也不会为它生成新的 selection key。
Action 成功或 selection key 存在只证明执行完成或引用可用，不能证明内容已经被消费；
事实性 claim 必须依赖实际读回的内容页。AgentTask 会把有界页正文与相同的类型化身份
投影为 Action evidence。如果 verifier 所需材料不在当前可见片段中，repair 必须读取更窄
或后续的页面，不能降低原成功标准。连续三次读取完全相同且未变化的类型化页面时，
开放 ActionLoop 会以“没有信息增量”结束并把控制权交还 TaskBoard；该转换不代表任务通过。

过大的 direct Action 与 ActionFlow carrier 会在进入 TriggerFlow state 或返回边界前，
按完整 record 做压缩。这个规则同时覆盖大型 kwargs/instruction 与大型 output，并避免
在 `data`、`result`、`model_digest` 中重复携带同一 payload。有限的内部 ActionRuntime
execution flow、ActionFlow 与 TaskDAG execution 不绑定 TaskWorkspace；只有
`TriggerFlowActionFlow` 的 approval pause 需要 save/resume 时，才会绑定 RecordStore recovery。

`Action.to_action_results(records)` 对指令型 action 使用 digest，因此后续回复能知道发生了什么，
但不会默认拿到完整 payload。

`max_output_bytes` 是输出证据策略，不是破坏性存储操作。Action 输出超过该限制时，Agently 会记录
diagnostics，并把完整值保留在 artifact ref 后面；模型可见路径仍然使用有界 preview。

当 host 显式调用 `agent.get_action_result(prompt=...)` 时，即使返回的
records 为空，Agently 也会把该 prompt 标记为已经消费过 action loop。之后同一
prompt 的响应读取不会为了生成最终文本再次进入 ActionRuntime。

host 需要权威 action evidence 摘要时，可以使用
`agent.action.summarize_records(records)`：

```python
summary = agent.action.summarize_records(
    records,
    validation_command_markers=["pytest", "pyright"],
)

assert summary["latest_validation"]["status"] in {"passed", "failed"}
```

摘要会报告 failed actions、尝试过的命令、成功命令，以及最后一个匹配的验证命令。

## 兼容入口 —— tools

旧入口仍可用：

```python
@agent.tool_func
def add(a: int, b: int) -> int:
    return a + b

agent.use_tool(add)
agent.use_tools([add])
agent.use_mcp("https://...")
agent.use_sandbox(...)
extra.tool_logs  # 旧入口的 extra.action_logs
```

它们仍是有效的公开挂载入口。内部映射到新 action runtime —— 不意味着 `ToolManager` 实现。方便时迁到 action 入口；什么都不会立刻坏。

历史 `use_sandbox(...)`、`bash_sandbox` 与 `PythonSandbox` 名称属于兼容入口，
不是统一代码执行的隔离 owner。新的 Python/Node.js/Go/C++ 执行在
`ExecutionResource` provider 上声明 isolation capabilities；`trusted_local`
仍是显式不安全 fallback，不能满足 `isolation=required`。

## 规划模型 key

Action planning 是模型拥有的步骤。如果 Agent 使用 `model_pool`，应把
`action.planning_model_key` 设置为负责规划 action round 的业务模型 key：

```python
agent.set_settings("model_pool", {"task-main": "deepseek-v4-flash-prod"})
agent.set_settings("model_profiles", {
    "deepseek-v4-flash-prod": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "api_key_pool": "deepseek-prod",
        "request_options": {"thinking": {"type": "disabled"}},
    }
})
agent.set_settings("action.planning_model_key", "task-main")
```

这个配置同时作用于默认 structured-plan 和 native tool-call planning
路径。当 AgentExecution Skill binding 或 AgentTask 把一个 bounded action round
委托给 ActionRuntime 时尤其重要，否则 action planning 可能没有显式使用
预期的 `model_pool` 业务 key。

provider response 尚未闭合时，structured planning 字段都只是 provisional。
`next_action="response"` 表示 ActionRuntime 不再调度后续 Action，不是 cancellation
signal。ActionRuntime 会等待最终结构化解析结果，让正常的 request/model completion、
metadata 与 usage 完成收尾，再关闭当前 bounded Action step。

`agent.get_action_result(..., timeout=N)` 会约束完整 action loop，包括
structured planning 和 native tool-call selection。如果 loop 不能在 deadline
前结束，Agently 会抛出 `RuntimeStageStallError`，其中
`stage="action_loop_close"`。

当 `planning_protocol="native_tool_calls"` 没有拿到 provider-native tool calls
时，Agently 会返回一个 `skipped` 诊断 action record，诊断 code 为
`action_runtime.native_tool_calls.empty`。host 应把它当作 planning evidence，
而不是已执行工作。

当连续 action rounds 反复选择同一批失败 action id，并且这些记录都没有产生进展时，
默认的 `TriggerFlowActionFlow` 会关闭当前 bounded action step，返回已经获得的失败
evidence。默认阈值是 `action.loop.max_consecutive_failed_rounds_per_action = 2`
（`tool.loop...` 仍作为兼容别名）。这不是任务预算；上层 owner，例如
AgentTask，可以基于这些结构化失败记录继续 verify、replan 或 block。

## handler 接口

如果你写自定义 `ActionRuntime` 或 `ActionFlow` 插件，规划与执行 handler 用稳定的双参数契约：

```python
async def planning_handler(
    context: ActionRunContext,
    request: ActionPlanningRequest,
) -> ActionDecision:
    ...


async def execution_handler(
    context: ActionRunContext,
    request: ActionExecutionRequest,
) -> list[ActionResult]:
    ...
```

context 字段含 `prompt`、`settings`、`agent_name`、`round_index`、`max_rounds`、`done_plans`、`last_round_records`、`action`、`runtime`。request 字段含 `action_list`、`planning_protocol`、`action_calls`、`async_call_action`、`concurrency`、`timeout`。

自定义 `ActionFlow` 插件可以接受可选的 `runtime_observation_handler`
关键字参数。若提供，flow 应把普通 observation dict 交给这个 handler，
不要直接发送官方 `action.*` 或 `tool.*` RuntimeEvent；core 会把这些
observation 映射到官方事件流。

内置 `TriggerFlowActionFlow` 与 `DAGActionFlow` 会在调用该 handler 之前，
以及 core 发送官方事件和兼容事件之前，统一经过 Action 所有的有界、脱敏投影。
plan observation 只暴露一份 canonical `decision.action_calls`，不再通过 legacy
decision aliases 重复复制 command；command observation 使用 canonical
`action_id` 与有界、脱敏的 `action_input`。重复失败收敛 observation 也只携带
bounded records，不会携带私有的完整 Action value。同一个 carrier budget 同时覆盖
`payload` 与 `error`：raw exception 会在 direct callback 之前转成一份有界、脱敏且
兼容 ErrorInfo 的 mapping；官方 `action.*` 和兼容 `tool.*` 事件直接复用该 mapping，
不会再从原始异常重建完整 message 或 traceback。opaque string/bytes 异常参数不会保留
任何原始前缀，只会投影为固定脱敏摘要、原始 UTF-8 字节数与 SHA-256 digest。显式结构化
参数可在敏感键脱敏后保留有界事实。投影后的 traceback 只包含结构化 frame 事实，不包含
格式化异常行、源码行、notes、locals、cause 或 context。

没有 legacy positional 签名 —— 公开契约只是 `(context, request)`。

Custom execution handler 的结果与内置 handler 使用同一个完整 record byte bound：进入
AgentExecution context、ActionFlow RuntimeEvent、TriggerFlow state、日志、metadata 或公开
返回值前就完成压缩。route log 只保留一个 bounded semantic payload，不会在 `raw` 下再存一份
complete record，也不会同时在 `data` 与 `model_digest` 中复制它。精确值只存在于仍存活的
private Action artifact scope。

## Action artifact 生命周期

大型 Action value 会以 exact value 保存在私有 `ActionArtifactManager` 中。敏感字段
redaction 与 truncation 只作用于 model-visible preview 和 RuntimeEvent，不会修改供
durable promotion 选择的私有值。AgentExecution 只接受当前 execution 恰好提供一次、
terminal result 也恰好返回一次的 `selection_key`。host 会结合预期 execution scope
解析该 key，并重建 canonical ref 与 exact value；unknown key、duplicate key 和复制的
canonical identity 都会被拒绝。业务字段 `accepted` 不具备选择权限。provider 提供的
artifact id 只作为 provenance 保存，每个 scope 都会得到新的 local artifact id。

Standalone direct Action call、`TriggerFlowActionFlow` 与 `DAGActionFlow` 会在
success、failure 和 cancellation 的 `finally` 中释放精确 `action_call` 或
`action_run` scope。standalone AgentTask 会在自己的 terminal seam 释放精确 task scope；
routed AgentTask 会把该 scope 显式 transfer 给 parent AgentExecution，由 parent 保持到
terminal selection/promotion 完成后再释放；parent cancellation 或 timeout 时，routed
task 的 stream owner 会先取消并 join child，再允许 parent 释放 scope，因此 child
不能在 terminal cleanup 后继续创建 artifact 或 RecordStore process record。child
ActionFlow 不会提前释放继承的 scope。若 selected promotion 失败，selected source
会连同 bounded retry diagnostics 一起保留，而同一精确 scope 中未选择的 artifacts
会被释放。

Standalone `TriggerFlowActionFlow` 的 durable pause 会在 exchange pending 期间保留
scope。response/resume 会在最后一个 interrupt 后关闭 flow；显式 abandon 或 host close
会取消等待。这三条路径都会且只会释放一次 standalone scope。

Standalone scope 在 run end 被丢弃，所以该 run 返回的 artifact ref 是历史投影，
`available=false` 且 `full_value_available=false`。bounded digest/preview 仍可用于
审计，但 `read_action_artifact` 无法读取已经释放的 value。只有 ref 明确报告
`available=true` 时才应调用 readback，例如尚未完成 transfer/cleanup 的
execution-owned scope。
在模型与 terminal 边界，`artifact_refs` 和兼容别名 `artifacts` 会被归一化为同一份
只含 selection key 的列表，避免一个别名重新暴露另一个别名已经移除的 canonical identity。

## 扩展指南

| 你想改 | 替换 |
|---|---|
| 仅后端（HTTP、gRPC、远程 worker、sandbox） | `ActionExecutor` |
| 规划协议或调用归一化 | `ActionRuntime` |
| runtime 与 flow 之间的编排形态 | `ActionFlow` |
| 多个 action 调用之上的更高层流控 | 用 `TriggerFlow` 在 runtime 之上 —— 不要把它塞进 executor |
| MCP/sandbox/process 类依赖的生命周期 | 声明 `ExecutionResource` requirement —— 不要把生命周期藏进 executor |

## 另见

- [Actions 概览](overview.md) —— Action Runtime 到哪里停止、编排从哪里开始
- [ExecutionResource](execution-environment.md) —— 托管 MCP/sandbox 执行依赖
- [工具](tools.md) —— 兼容入口详细
- [MCP](mcp.md) —— `agent.use_mcp(...)`
- [TriggerFlow 概览](../triggerflow/overview.md) —— action 之上的编排
