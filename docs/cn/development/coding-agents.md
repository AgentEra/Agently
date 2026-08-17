---
title: Coding Agents
description: 用 Agently 配合 Codex、Claude Code、Cursor 等 coding agent —— 官方 Agently Skills。
keywords: Agently, coding agents, Codex, Claude Code, Cursor, Skills
---

# Coding Agents

> 语言：[English](../../en/development/coding-agents.md) · **中文**

如果你借助外部 coding agent（Codex、Claude Code、Cursor 等）写 Agently 应用，给该 agent 提供良好 Agently 上下文的规范方式是 `Agently-Skills` 伴生仓中的**官方 Agently Skills** 包。

本文讲的是 **companion repo** 这条路径，不是框架内 runtime skill 消费。如果你要的是 Agently 自己在真实任务里安装并应用外部 skills，读 [Skills Compatibility](skills-executor.md)。

## 什么是 Agently Skills

skill 是一个包，含：

- `SKILL.md` 描述该 skill 做什么、何时应用
- references —— coding agent 按需拉的聚焦文档
- examples —— 最小可运行片段
- validators —— agent 可跑的脚本，确认用户项目遵循推荐结构

skill **不是**纯文档。它为 coding agent 结构化：每个 skill 告诉 agent 它解决什么问题、推荐路径长什么样、如何验证用户代码在该路径上。

## Companion skills 和框架内 skill 执行的区别

这两件事要分开：

- `Agently-Skills` companion repo：给外部 coding agent 用的 skill 包
- Agently runtime Skills：Agently 由 SkillLibrary 拥有、AgentExecution 绑定的 runtime
  能力，`SkillsExecutor` 仅保留为兼容 facade

伴生仓不会变成你的 Agently app 运行时依赖。它仍然只是给 coding agent 的指导包。

## 当前 skills

| Skill | 用户在做的事 |
|---|---|
| `agently` | 从零开始 —— 选合适的项目结构 |
| `agently-design` | 设计或审计跨 ModelRequest、证据、生命周期、并发或可观测性的非简单系统拓扑 |
| `agently-request` | 模型接入、Prompt 管理、结构化输出、响应复用、session memory、embedding、检索 |
| `agently-runtime` | Action Runtime、内置 actions、MCP、ExecutionResource、FastAPI 暴露、DevTools 接入 |
| `agently-dynamic-task` | 模型生成或应用提交的 DAG 规划、校验和执行 |
| `agently-triggerflow` | 需要分支、并发、pause/resume、save/load |
| `agently-migration` | 从 LangChain、LangGraph、LlamaIndex、CrewAI 或类似系统迁移 |

当前公开 catalog generation 是 `v2`。实际默认 skill 列表见 `Agently-Skills/skills/`，应只包含这 7 个 skills。

## 安装

```bash
git clone https://github.com/AgentEra/Agently-Skills
```

按 coding agent 自身的 loader 指向 skill 目录：

- **Claude Code** —— `~/.claude/skills/` 或项目 `.claude/skills/`
- **Codex** —— 见 Codex 安装的 skill / context loader
- **Cursor** —— 经项目 rules / context surface 加载

skill 是纯文本 + 脚本；安装时不跑 Agently 特定的东西。

如果用 CLI 安装，默认 `app` bundle 是：

```bash
for skill in \
  agently \
  agently-design \
  agently-request \
  agently-runtime \
  agently-dynamic-task \
  agently-triggerflow
do
  npx skills add AgentEra/Agently-Skills --agent "$AGENT" --skill "$skill" -y
done
```

只有迁移项目才额外安装 `agently-migration`。历史 catalog 通过冻结归档分支保留，
而不是放在默认分支文件树里；V1 12-skill catalog 归档在
`update/archive-legacy-v1-catalog`，最后支持 Agently `4.1.1`。新项目不要把归档
catalog 加入 coding agent 的常规搜索路径。

## 为什么是 skill 不是单纯文档

文档告诉人能做什么。skill 告诉 coding agent **当前**推荐什么 —— 包括哪些 API 已 deprecated、当前 lifecycle 是什么、报告"完成"前要验证什么。这让 coding agent 与框架演进对齐，不需要用户手动更新自己的 context。

特别地，skill **不得**推荐 deprecated 路径如 `.end()`、`set_result()`、`wait_for_result=`、旧 `runtime_data`。如果你发现某 skill 推荐其中之一，请向 `Agently-Skills` 提 issue。

应用开发中如果发现框架能力缺失、行为与 docs、examples、Skills 指导或预期的
模型应用责任边界不符、API 未暴露或使用不友好，或某项本应由 Agently 承担的
责任只能由业务代码通过 workaround、补丁、胶水、私有 wrapper 或重复机制弥补，
应生成简洁规范的 issue 说明。建议到
[`github.com/AgentEra/Agently`](https://github.com/AgentEra/Agently/issues)
提报，并包含业务场景、期望的框架责任、实际行为、当前 workaround、最小复现或
受影响 docs/examples，以及兼容性问题。
issue 必须把遭遇问题时的具体场景讲清楚，说明当时尝试解决的是哪一类模型应用
开发问题。涉密时可以脱敏或省略具体业务细节，但仍要描述应用类别、workflow
形态、决策点和期望由框架承担的责任，方便维护者理解问题。
人工提交时，只把 issue 内容和提交方式提供给使用者。自动提交前必须先询问用户；
如果用户确认自动提交，先检查本地 GitHub 提交权限和能力、本地复现问题仍存在，
并复核 Agently docs、examples、Skills 指导和 API 用法，确认不是遗漏信息或不当
使用造成的问题。创建远端 issue 前必须清理正文，确保不包含 secret、token、
客户数据或本机绝对路径。

新增框架 deprecation 时，必须通过 `agently.utils.DeprecationWarnings.warn_deprecated_once(...)` 或 `agently.utils.warn_deprecated_once(...)` alias 搭配稳定 API key 发 warning。不要直接新增 `warnings.warn(..., DeprecationWarning, ...)`；deprecated API warning 设计为每个 Python 进程内每个 API 只发一次，并遵守 `runtime.show_deprecation_warnings`。

## 模拟优先的模型实验

如果问题发现或策略调优预计需要多轮模型调用，先让开发 Agent 在当前任务中
自我模拟一条尽量贴近目标的请求、返回与行为链。预先写明验收条件，并在不调用
目标模型 API 的情况下，迭代 prompt、输出 schema、拓扑、观测信息和失败路径，直到
同一上下文内的**热预演**达到这些条件。所有产物必须标记为 `simulated`：它们只是
低成本的假设和协议设计材料，不是观测事实，也不是真实模型证据。

模拟可以检查内容、schema、分支、错误包络以及计量元数据的预期形状，但不能准确
复现 provider 生成的 request ID、token 用量、cache / billing 字段、时延、结束行为
或其他遥测。虚构值标记为 `synthetic`，估算值标记为 `estimated`，无法获得的字段
标记为 `unavailable`，历史 trace 回放标记为 `replayed` 并注明来源。只有目标 provider
在当前真实运行中返回的值才能标记为 `observed`；不得把模拟用量或元数据计入真实实验
汇总。

热预演稳定后，最多选择一个可行的隔离载体做**冷复核**：

- 使用全新或不继承上下文的原生 coding-agent 子 Agent；
- 使用经过 handshake 确认的 ACP coding agent；或
- 为开发 Agent 创建一个全新、隔离的任务或会话。

ACP 只是选项之一，不是必选项，也不需要把三个选项全部执行。只向选定载体提供当前
任务相关的 input、权威 `info`、`instruct`、精确 `output` contract 和书面验收条件；
不得泄漏预期答案、既有结论、完整对话、客户 secret 或无关文件。由宿主强制限制工具、
网络、文件范围、调用次数和时间。结果标记为 `simulated` 与 `cold_preflight`；除非载体
能够证明底层恰好只有一次模型请求并暴露其计量信息，否则还必须标记为
`agent_simulation`，不得标记为 `single_model_request_simulation`。

在当前上下文中直接自我模拟只能算 `warm_preflight`，不能充当冷复核。没有可用隔离载体
时，记录 `cold_preflight=skipped` 及原因，然后继续执行最小、具有代表性且有明确上限的
真实模型校验；不得因此阻塞真实校验，也不得把热预演伪装成冷复核。最终结论必须来自
真实 trace；模拟与现实不一致时，以真实 trace 为准并回到分析与修订循环。默认使用项目
或开发方已授权的测试凭据，并明确限制调用数、并发、重试与预算。未经客户明确授权并
告知最大调用数或费用，不得消耗客户 API 凭据或额度。

## 4.1 之后的默认推荐

当你审计或编写面向 Agently `4.1+` 的指导时，coding agent 应默认偏向这些用法：

- API 形态：遵守奥卡姆剃刀原则。如无必要，勿增实体、方法、facade 或兼容补丁；已有表面能清晰承载语义时优先复用。若只是命名表意不清，优先建议窄别名或文档澄清，而不是再加一个容易重叠的方法。
- 请求本地 prompt 上下文：只放入会改变当前请求的事实、契约、证据、规则和结果结构；使用移除反事实，并在发送前检查 `execution.get_prompt_text()`。
- 结构化输出：固定必填叶子直接写在 `.output(...)` 的 `(TypeExpr, "description", True)` 里。只有空值必须触发重试时才用 `(TypeExpr, "description", "not_null")`。手动 `ensure_keys=` 只留给条件路径或运行时决定的路径。
- 规则先行校验：如果模型需要满足业务 validator，所有非敏感、且模型能够遵守的规则都要
  在首次请求的 `input` / `info` / `instruct` / `output` 契约中说明。确定性校验仍是
  最终权威，但不得用硬拒绝与重试充当模型的规则发现回路。请求的生产卡控无法安全披露
  或具体表达时，应先说明受影响输出、成本、延迟、非确定性、活性、替代方案与终止行为，
  并在实现前要求开发者用新的回复明确二次确认这个具名卡控。
- 标识连接：当模型需要判断、选择、排序或引用宿主记录时，每个候选只提供一个由
  宿主签发的可信 `selection_key`，以及与当前任务直接相关的事实。模型只需随判断
  返回这一个 key；宿主代码应先校验它属于本次候选集合，再确定性查找和重建
  canonical id、UUID、metadata、opaque ref 与完整记录。不要把包含多个 id、无关
  `meta` 的完整对象交给模型全量抄写；这是提高转录和后续 join 失败率的反模式，
  不是有价值的推理或输出控制。`selection_key` 只是 application-local projection，
  不是第二套 canonical identity；应把它声明为受本次候选 key 集合约束的 required
  string，并在查找前拒绝未知 key 和业务上不允许的重复 key。
- Actions：新代码从 `@agent.action_func` 和 `agent.use_actions(...)` 起步。`tool_func`、`use_tool`、`use_tools` 是兼容别名，不是首选推荐。
- TriggerFlow lifecycle：把 `close()` / `async_close()` 和 close snapshot 视为规范收尾路径。不要把 `.end()`、`set_result()`、`get_result()`、`wait_for_result=` 当正常起点。
- TriggerFlow state：单次 execution 的数据用 `get_state(...)` / `set_state(...)`。`flow_data` 是有意共享时才使用的风险作用域，不是普通状态存储。
- Settings 加载：provider 配置落文件时，优先 `Agently.load_settings("yaml_file", path, auto_load_env=True)`。`Agently.set_settings(...)` 留给内联覆盖。
- 执行风格：服务、流式、工作流默认 async-first。sync API 视为脚本、REPL 或兼容桥接层。
- 复杂执行规划：选择拓扑前先画出真实串并依赖。使用 Agently async API；provisional `instant` 结构化流只用于 UI 或可取消/幂等的准备工作；应用拥有的协调用 TriggerFlow signal/join；业务决策和不可逆副作用必须等待最终解析结果与已配置校验。独立工作应在 execution、operator、模型 scheduler 和宿主入口的有界限制下并发执行；存在阻塞代码时还要暴露宿主 worker/thread-pool 设置。未经分析就采用全串行方案是反模式。
- Result 复用：一次模型调用如果要同时消费文本、结构化数据、metadata 或流式更新，优先 `get_result()` 复用同一个 result，而不是重复发请求。
- Result 消费：没有调用方真正消费渐进输出时，直接等待
  `result.async_get_data()`。先用 `get_async_generator(type="instant")` 空循环丢弃
  所有 item、再读取最终结果是反模式：它增加 stream queue、事件迭代和 parser 工作，
  却没有发布或使用任何中间值。只有应用确实转发 delta、更新 UI/state、记录事件，
  或启动可取消/幂等准备工作时才使用 stream；最终 data 继续从同一个 result 读取。
- ActionRuntime 收尾：把 streamed `next_action="response"` 视为“不再调度 Action”的
  provisional decision，而不是关闭 provider stream 的许可。应等待最终 planning 解析结果，
  让正常 request/model terminal events、metadata 和 usage 完成收尾。
- 检索引用：每个被选 source 只给模型一个短可信稳定 `ref_id`，要求在正文中写
  `[[ref:<ref_id>]]`，例如 `[[ref:ref_2]]`。evidence `cite_as`（如 `e1`）只是
  请求内显示别名；持久化响应前必须通过该次精确 offered map 完成归一化。宿主负责
  校验和解析 token、渲染安全链接，并另外发送完整且已授权的 source-card record，
  供前端显示 hover card、来源列表或回复后的附加结果卡。不要使用裸 `${ref_id}`，
  因为 `${...}` 已是 Agently placeholder 语法；也不要让模型抄写 URL 或完整检索
  metadata。
- required Skill availability：有权限的宿主代码先物化远程 source，SkillLibrary
  再安装不可变本地 revision。只有 canonical revision binding 成功后才继续，否则
  fail closed。
- 任务执行质量：required Action、Skill 与 Skill pack 必须表达为框架 contract，
  不能只靠 prompt 或业务特例。AgentExecution 把 Skill 绑定进 TaskContext；
  ActionRuntime 拥有可调用副作用与证据，TaskWorkspace 拥有文件 mutation/readback，
  RecordStore 拥有持久 records。Skill 不能授予这些 capability。TaskDAG / DynamicTask
  仍是独立提交式 DAG data，不是 AgentTask bounded-step strategy。场景特定检查只留
  在 examples 与 tests。
  缺失 required `action_succeeded` evidence 时，只能依据已声明的结构化 requirement
  创建 TaskBoard Action-shaped repair；不要解析 verifier prose、特判 Action name，也
  不要让 TaskWorkspace readback 满足 Action requirement。model-visible Action result 可提供
  宿主签发的 `action_call_id`；host 校验这个候选 key 后再解析 canonical evidence
  identity。每个 TaskBoard Action card 只接收一个 card-local work unit 和 dependency
  evidence；global task 只提供方向，不授权 sibling work。terminal verifier 热输入只保留
  一个有界且带正文的 evidence ledger，以及不带正文的 locator/ref indexes；raw evidence
  留在冷侧，用于 scoped readback 和审计。
  合适的 candidate 只会针对当前版本的 terminal-carrier inventory 进入一次语义 terminal
  verifier request。同一个结构化 response 返回精确 criterion checks 和 material-claim
  checks；host 重新连接当前 carrier id、精确 artifact quote、content version 与 offered
  evidence id，不再运行独立的 claim inventory、source selection 或逐 claim support
  judgment 请求循环。candidate 与 delivery/readback/acceptance record 不能支撑其后代
  carrier。失败的 material check 会产生结构化 repair contract；对其精确
  gate-kind/issue-code/contract-subject key 使用三次收敛：最多调度两次 repair；相关状态不变
  时跳过重复 verifier；第三次返回保留有用 artifact 的 partial blocked 结果。required TaskBoard
  card 的非满足结构化结果（`setback`、`failed` 或 `blocked`）只在该 card 于当前 tick
  实际执行时计数，并在同一稳定 contract subject 第四次执行前停止。没有精确 Action
  requirement 的框架 final repair 保留普通 `auto` shape，让已挂载
  capabilities 仍可用；不得从 verifier prose 推断 capability id。

## 何时写自己的 skill

如果团队在 Agently 之上有内部模式（特定项目布局、包装的 agent factory、自定义 action 集），考虑作私有 skill 包，按公开 Agently Skills 格式。coding agent 会跨项目一致地应用团队约定。

## 验证脚本

数个 skill 携带验证脚本（如 `validate/validate_native_usage.py`）。coding agent 在宣布任务完成前可跑它们，确认用户项目遵循推荐路径。例如 TriggerFlow 验证器检查没有 deprecated API 作为推荐起点。

功能验收通过还要求完成 spec 对齐：把相关 spec 更新为最终实现方案，已完整落地的 planned spec 移入 `spec/implemented/`，并在同一工作项里更新 `spec/README.md`。

用户可见 feature work 必须为功能所对应的场景新增或更新 examples。example 应在声明环境中可运行，使用当前推荐 API，并通过输出、断言或注释把关键运行时行为展示出来。`Expected key output` 注释应保留一次实际运行中的稳定关键值，而不是只写“可以看到 X”一类泛化描述。当输出本身不足以解释行为时，可在 example 注释中补充简短工作原理或 ASCII 流程图。

对 Agently `4.1.3` 开发线，如果任务涉及默认 `agent.start()` 路由、`agent.create_execution()` 或 Agent 过程流式输出，需要纳入 `examples/agent_auto_orchestration/`。该目录中的本地 smoke 脚本只能作为基础设施检查；模型应用或验收结论仍必须来自真实 DeepSeek 或本地 Ollama 示例。对 4.1.2.5 基础能力线，把 `examples/cookbook/`、`examples/action_runtime/`、`examples/execution_resource/`、`examples/builtin_actions/`、`examples/trigger_flow/`、`examples/dynamic_task/` 和 `examples/fastapi/` 视为推荐起点；`examples/archived/` 只作为兼容参考。

汇报 API、推荐用法、examples 或兼容线变化时，应给出能直观看出新用法的简短样例代码。能用当前用法或 before/after 片段说明时，优先用代码片段而不是抽象描述。

## 另见

- [Action Runtime](../actions/action-runtime.md) —— skill 假设的 tool 使用架构
- [DevTools](../observability/devtools.md) —— 观测、评估和交互式 wrapper 路径
- [TriggerFlow 兼容](../triggerflow/compatibility.md) —— skill 引导的迁移路径
