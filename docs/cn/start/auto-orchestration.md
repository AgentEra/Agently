# Agent 自动编排

> 4.1.4.9 新长任务已使用[统一 Loop](long-task-loop.md)。下文 Flat/TaskBoard 卡片调度、目标补全和旧任务恢复说明仅适用于 4.1.x 显式旧策略兼容路径。

Agently 4.1.4 将 `agent.start()` 作为 Agent turn 的默认用户层入口。它仍然返回
业务结果，但 Agent 可以在显式注入候选能力后，路由到普通模型响应、Actions 或
AgentExecution-bound Skill context。

```python
result = (
    agent
    .use_actions([market_data_action])
    .use_skills_packs(["equity-research"])
    .input("Review this renewal risk.")
    .output({"answer": (str, "final answer", True)})
    .start()
)
```

候选注入是边界。如果没有注册 Actions、Skills 或 Skills Packs，`agent.start()`
仍然是普通模型请求。

`TaskDAG` 是 DAG 基石能力。`DynamicTask` 保留为 DAG planning/execution 之上的
兼容与便利 facade，不再是第二套推荐任务生命周期，也不是 AgentTask 的自动策略
route。当应用或可视化自动化界面拥有图形结构并需要显式运行该图时，使用
TaskDAG / DynamicTask。

quick prompt 链会创建 execution-scoped draft。Agent 可以作为服务单例保存共享
settings、模型激活、Actions、Skills、TaskWorkspace 和 `define(...)` / `always=True`
prompt；同一条链里的 `.input(...)`、`.system(...)`、`.output(...)`、附件和本次
execution options 会写入隔离的 `AgentExecution` draft：

```python
results = await asyncio.gather(
    agent.input("Summarize request A").async_start(),
    agent.input("Summarize request B").async_start(),
    agent.input("Summarize request C").async_start(),
)
```

quick prompt 后继续配置 capability 时仍然保留同一个未启动 execution。例如
`agent.input(...).info(...).use_action(...)` 会保留 input 和 info，不会静默创建一个
替代 execution。

多语句 setup 应显式拿住 execution draft：

```python
execution = agent.create_execution()
execution.input("Review this renewal risk.")
execution.output({"answer": (str, "final answer", True)})
result = await execution.async_start()
```

不要再依赖 `agent.input(...); agent.output(...); await agent.async_start()`
来累计本轮 execution prompt。Agent 生命周期状态使用 `always=True`、
`set_agent_prompt(...)` 或 `agent.define(...)`。只有明确需要低层 request-builder
兼容面时才使用 `agent.create_request(...)` / `agent.request`。

已验收开发线的路由是候选驱动、确定性优先：required Skills 将不可变 guidance
绑定进 `TaskContext`；具体模型响应对 Skill Context 的消费记录与可执行 capability
evidence 分开。普通 Actions 进入 `model_request` AgentExecution action loop。
默认每轮选择继续调用 Actions 或给出最终 response；终态 response 直接由同一个
AgentExecution 交付。只有 legacy/custom fallback 或 `auto_continue` 这类独立交付
策略才追加最终生成请求。Skills 不创建 route，也不是 planner capability。

公开 Agent API 仍由 core 持有；其工厂直接创建所选 AgentExecution 插件，
由该实例持有生产和最终处理规则。Skill Context 与 DAG substrate
仍是 execution 内可替换的组件。

## 人机交互

当本次 AgentExecution 需要通过请求级回调响应 connected HITL exchange 时，使用标准
方法 `.interact(handler)`：

```python
def handle_exchange(exchange):
    if exchange["kind"] == "approval":
        return {"status": "approved", "approved": True}
    return {"audience": "framework developers"}

result = (
    agent.create_execution("plan")
    .input("规划这次发布。")
    .interact(handle_exchange)
    .start()
)
```

handler 接收一个标准化 `ExecutionExchangeView`，其中稳定提供 `kind`、`subject`、
`payload`、`request` 等字段；handler 可以同步或异步执行，返回该 exchange consumer
所需的响应 payload。approval 通常返回 Boolean 或 decision mapping，clarification
可以返回文本、list 或 mapping。

`.interact(...)` 声明响应机制，不会强制产生交互。普通请求没有打开 exchange 时，
handler 不会被调用。声明仅属于当前 AgentExecution，会为其 request 选择 connected
interaction，且在启动前重新配置 execution 时仍保留；它不会注册或替换全局 provider。
启动前再次调用 `.interact(...)` 会替换前一个 handler。

既有 owner 边界保持不变：ExecutionExchange 负责标准化 request/provider envelope，
TriggerFlow 负责 pause/resume。durable queue、webhook、跨进程 host 或应用级 routing
继续使用已注册的 ExecutionExchange provider、routing handler 和 `interaction.*`
settings；这些高级 transport 选择不会变成 `.interact(...)` 的 kwargs。

## Review 与最终结果校验

使用 `.review(handler=None, *, rules=None, on_fail="warn")` 审查最终结果及其关键产物。
`rules` 支持字符串或字符串序列。同步/异步 handler 接收 `(result, context)`，
替换整个评估过程；context 包含规则、原始合同、可信产物引用与 TaskWorkspace。
返回 Boolean，或包含 Boolean `passed` 的 mapping。

```python
result = (
    agent.input(task).output(contract)
    .review(rules=["检查结论是否有输入证据支持。"])
    .start()
)

result = (
    agent.input(task).output(contract)
    .validate(release_check)  # (value, context) -> bool 或 {"ok": bool, ...}
    .review(on_fail="block")
    .start()
)
```

`on_fail="warn"` 记录失败评价，不改变结果和成功状态；`"block"` 抛出
`AgentReviewError`，阻止终态成功。它控制 Host 行为，不改变模型审查标准。
两种模式都不会改写或重试；暂不支持 `"retry"`。
Agent/AgentExecution 不提供公开 `verify()`。

不传 handler 时，默认审查器通过 TaskWorkspace 读取完整可信文本产物、核对内容版本，
再发起一次结构化模型请求。结果文本与产物完全相同时仅发送一次正文。
非文本、不可读、已变更或不完整产物得到 `not_assessable`，不会以元数据检查冒充
内容审查；其他格式需使用合适的自定义 handler。此处不隐含渐进式或分段模型审查，
上下文溢出会明确失败。plan 按计划产物及已接受的澄清回复审查，不要求已经执行计划；
缺少 goals 时不会另行编造目标。

报告包含 `passed`、描述档位 `quality_level`（`strong`、`adequate`、`weak`、
`not_assessable`）、`summary`、逐条规则对应的 `checks[]`、
结构化 `issues[]`（`criterion`、`finding`、`evidence`、`suggestions[]`），以及
不重复问题局部建议的 `overall_suggestions[]`。Boolean handler 不虚构档位，
`quality_level=None`。不使用模型生成的数字评分。
通过 `(await execution.async_get_meta())["reviews"]` 读取报告；
事件包括 `review.started`、`review.completed`、`review.warning`、`review.blocked`。

`validate(handler)` 硬校验的是**当前调用的最终输出**：直接响应、最终计划、
Host 组装的完整文档、长任务最终业务输出或自定义 Execution 返回值，
不自动检查内部步骤。直接 ModelRequest 与 `auto_continue` 保留已有受控修复；
其他最终校验仅执行一次，不重放内部步骤或副作用。后者的 context 使用
`meta.scope="agent_execution_final"`，没有 provider response ID，`max_retries=0`。
校验先于声明式产物交付及 review，但不会撤销任务已经产生的副作用。
需要安全修复循环时，使用显式 TriggerFlow 编排。

## Artifact 交付

当已接受的业务结果还必须交付为经过验证的文件时，使用
`.artifact(path, handler=None)`：

```python
result = (
    agent
    .input("准备上线报告。")
    .output(report_contract)
    .artifact("reports/launch.json")
    .review()
    .start()
)
```

它适用于普通文本和结构化请求，并不局限于 workspace-oriented AgentTask；但它的可信
边界始终是 Agent 绑定的 TaskWorkspace。字符串会原样写入；mapping、list、tuple 和
JSON primitive 会序列化为易读 JSON。自定义同步或异步 handler 接收
`(result, context)`，返回 `str` 或 `bytes`：

```python
agent.input(task).artifact(
    "exports/result.bin",
    lambda result, context: encode_result(result),
).start()
```

handler 只负责渲染内容。路径 containment、写权限、物理 digest 回读、可信文件身份和
终态保留仍由 TaskWorkspace 负责。只读 workspace 中，新请求路径会写入当前 execution
的私有 fallback area，应从可信 ref 的 `path` 读取实际位置；读写 workspace 则使用请求
路径。

artifact 交付不会替换或包装业务结果。可信 refs 位于
`meta["logs"]["artifact_refs"]`，并通过 `artifact.started` / `artifact.completed`
stream event 暴露。连续调用可创建多个分别校验的文件；任一已声明交付失败都会使本次
run 失败。artifact 物化总是在 review 之前完成。

## Execution 插件

Agent 持有可复用配置与能力；AgentExecution 插件持有一次执行的隔离草稿、
生产生命周期、结果和最终处理规则。`agent.create_execution(name)`
返回的就是已注册类的实例。

| 名称 | 生产行为 | 可显式搭配的策略 |
| --- | --- | --- |
| `auto`（默认） | 启动时选择已有请求、长任务或 DAG 路由 | 已有路由策略 |
| `request` | 单次请求，包括请求自己的解析与修复 | `auto`、`direct` |
| `long_task` | 保留状态的多步目标执行 | `auto`、`task`、`task_loop`、`long_task`、`flat`、`taskboard` |
| `plan` | 就绪检查、必要时连接式澄清、最终计划 | `auto` |
| `long_content` | 章节规划、依赖式写作、宿主按序组装 | `auto` |

显式选择优先于配置中的默认插件。不兼容的策略组合在模型或 Action 调用前报错。
路由策略不能悄悄把显式插件换成另一种生产方式。普通链式调用保留延迟
`auto` 选择；仅注册插件不会额外发起模型请求、规划或 review。

```python
execution = (
    agent.create_execution("plan")
    .input("根据已提供的事实规划一次工作坊。")
    .info(workshop_facts)
    .interact(handle_exchange)
    .output(plan_schema)
    .validate(validate_plan)
)
plan = execution.start()
meta = execution.get_meta()
print(meta["plugin"], meta["route"]["selected_route"])
```

调用方的 `validate(...)` 只硬校验生产方最终返回的值，不审查中间的就绪响应、
章节计划或任务步骤。直接请求的修复仍由 ModelRequest 负责。
Artifact 写入、回读和可选 review 在最终校验后由同一个 execution 执行。

`plan` 使用有界的就绪检查与规划请求。就绪检查也会读取最终 `output(...)`
的字段描述和约束，但自身仍返回独立的就绪判断，不提前填写最终计划。
最终规划直接使用原始任务与已接受的澄清回复，不要求先重新生成一份目标和交付要求的复述。
需要澄清时，连接式 handler 接收
`ExecutionExchangeView`。缺失或拒绝的回答、澄清次数耗尽会产生明确的 blocked
结果，不会伪造回答。本内置连接式流程尚不承诺持久化恢复计划。
配置为 `plugins.AgentExecution.plan.max_clarification_rounds`（默认 3）及
`max_questions_per_round`（默认 3）。

`long_content` 先规划章节，逐章生成正文，并在后续章节需要时生成一份实际章级
摘要；宿主保留完整子目录，按计划顺序组装正文，不让模型再复制全文。
配置 `plugins.AgentExecution.long_content.max_sections`（默认 12）。
整个 `long_content` Execution 输出文本，不能搭配结构化 `output(...)`。
结构内的长文则在普通请求上用 `(LongContent, "写作要求")` 或兼容字符串
`("long_content", "写作要求")` 声明；框架独立生成正文并填回原结构。
两种作用域不要混淆，详见[字段级长文声明](../requests/output-control.md)。
章节请求使用底层条件续写，正常完成不追加请求；根 `.auto_continue()` 不会把
组装后的全文重新请求。已发布 `.ensure_long_output()` 保留为同实现兼容入口。
长文生产与请求续写的职责独立，通用结构内长字符串接续仍有未完成的验收项。

自定义实现也注册到 `AgentExecution` 分类。可以继承内置实现复用生命周期，
在同一个实例上覆盖带明确类型的受保护生产方法：

```python
from agently.builtins.plugins.AgentExecution import RequestExecution, ProductionOptions

class AuditedRequest(RequestExecution):
    name = "audited_request"

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        route, value = await super()._async_produce(options)
        self.logs["audit"] = {"produced": True}
        return route, value

agent.plugin_manager.register("AgentExecution", AuditedRequest, activate=False)
execution = agent.create_execution("audited_request").input("解释这次迁移。")
result = execution.start()
```

仅在确实要改变默认插件时使用 `activate=True`。完整替换实现也可以直接遵守公开
协议。`run/async_run` 与兼容的 `start/async_start`、结果读取入口共享一次生产；
预先取得的结果读取器不会再启动另一份执行。

元数据提供 `plugin`；多请求生产方发出 `execution.stage.started/completed`，
在 `diagnostics.execution_run` 中记录阶段。类型从 `agently.types.plugins`
和 `agently.types.data` 导入，不额外扩张根包导出。
已发布的 AgentOrchestrator 激活路径作为兼容适配保留，但不再参与默认创建。
未发布的 `pattern()` 与 AgentPattern 分类已被替换，不作为并行 API 保留。

本地 Ollama Qwen 示例：

- [带连接式澄清的计划](../../../examples/agent_auto_orchestration/26_plan_execution_interaction_ollama.py)
- [带产物回读与 review 的长内容](../../../examples/agent_auto_orchestration/27_long_content_execution_artifact_ollama.py)

## Goal Pursuit

`agent.goal(goal_or_goals, success_criteria=None, *, turn_on_long_task=True)`
声明语义目标与成功标准；默认还为普通 auto 路由开启有界长任务便捷路径。
`turn_on_long_task=False` 写入相同 Prompt 声明，但不因此开启长任务，
也不会禁止独立选择的 long_task 插件或策略。显式 `direct` 策略和插件选择仍然有效。
`agent.goals(...)` 是复数别名；两者均不自动开启 review。
启动前再次调用 goal() 会替换该声明拥有的便捷开关。

已选定的长任务生产方若缺少 goal 或 success criteria，会在构造任务状态前调用模型，
仅从原始请求推导缺失字段。显式声明与原始 Prompt 保持不变，metadata 记录模型来源；
推导标准不能授权新工作或虚构业务门槛。必要事实不足时返回 blocked。
推导会参考最终输出合同（含字段描述、格式和必填要求）；未声明输出时不添加空合同。
补全阶段消耗同一 execution 的模型请求与时间预算，构造任务不会重新计时。
任务创建前超时会抛出 `RuntimeStageStallError`；创建后沿用任务的 `timed_out`
结果封装，并受 execution 剩余时间约束。
完整或已恢复的合同跳过此节点；普通请求与 review 不增加前置调用，plan/long_content
继续使用各自的规划节点。参见 [缺失目标补全示例](../../../examples/agent_auto_orchestration/28_missing_goal_preparation_ollama.py)
及其记录的 27B 成功样例与 9B 未通过结果；单次运行不代表稳定性保证。

task-specific options 单独组装时，应通过 task strategy 传入：

```python
execution = agent.goal(goal, success_criteria).strategy("auto", options=options)
```

这里嵌套的 `options` mapping 属于 AgentTask。把同一 mapping 传给
`agent.create_execution(options=options)` 配置的是 AgentExecution，不是文档推荐的
task-option 路径。

```python
result = (
    agent
    .use_skills("website-builder", "seo-reviewer")
    .use_actions(write_file, read_file)
    .require_actions("write_file")
    .goals(
        [
            "构建一个小型产品官网。",
            "准备上线检查清单。",
        ],
        success_criteria=[
            "最终产物是一个可运行页面文件。",
            "页面内容覆盖所有输入的业务事实。",
            "执行证据包含文件写入、读回和内容检查。",
        ],
    )
    .effort(
        "high",
        budget={
            "iteration_limit": 4,
            "model_call_limit": 10,
            "wall_time_seconds": 300,
        },
        planning={"depth": "expanded", "max_plan_items": 8},
        verification={"strictness": "strict"},
        replan={"policy": "on_verification_failure", "limit": 2},
        progress={"detail": "phase"},
    )
    .start()
)
```

简单代码仍然可以只写 `.effort("low" | "medium" | "high")`。展开形式仍然属于
同一个入口：effort 只控制策略和资源强度，不决定 execution 是否进入目标追求。
`budget.iteration_limit`、`model_call_limit` 和 `wall_time_seconds` 是软策略元数据：
它们可以影响 planning、reflection、repair 倾向和 evidence 深度，但不会静默设置
task-strategy `max_iterations` 或 AgentExecution hard limits。宿主需要硬资源控制时，
应显式使用 task options 或 `limits={...}`。默认情况下，AgentTask 不施加模型请求数、
迭代数、TaskBoard tick 数或 Action round 数配额；no-progress 和 idle timeout 仍作为
卡死执行的活性保护，而不是策略效果证据。完成仍然必须同时通过 model verification 和
host guards。
对于 task-strategy execution，effort 还控制 reflection 密度：`low` 总是记录最终
reflection，只在 planner 标记的重要过程节点记录过程 reflection；`medium` 在每个
大任务节点或 TaskBoard card/tick 后记录 reflection；`high` 在每个框架可观测的
bounded step、Action/ACP call、TaskBoard card 和最终结果后记录 reflection。
Reflection 默认只保留在任务内存和运行观测输出中，也可以进入 verifier/replan 输入，
但它本身不是完成证据。

`execution.step_plan` 只作为兼容指导保留，普通用户不需要显式写出来。AgentTask
不再把 TaskDAG / DynamicTask 作为内部 bounded step 策略；旧模型输出
`dynamic_task` / `execution_dag` 或旧配置 `execution={"step_plan": "dag"}` 会降级为
direct bounded execution，并留下 diagnostics。当宿主拥有提交式 DAG 或可视化自动化图
时，单独使用 TaskDAG / DynamicTask。

## AgentTask 策略

当业务目标需要一个有边界的多轮闭环，而不是一次 direct AgentExecution 时，使用
`agent.create_task(...)`。它返回一个 task-strategy `AgentExecution` draft；
内部保留的 `AgentTask` record 运行一个由单个 Agent 持有的任务：计划、执行一个
bounded step、收集有界证据、验证、必要时 replan，最后以 complete 或
blocked 结束。

保留的 runtime 是一张 TriggerFlow 生命周期图，明确暴露
`lifecycle.start`、`context.prepare`、`work.plan`、`work.execute`、
`outputs.materialize`、`evidence.ingest`、`terminal.verify` 和
`transition.decide` 节点。阶段事件只携带 `task_id`、单调递增的
`state_version`、`frame_id`、`iteration`，以及当前阶段的 `plan_id`、
`work_result_id` 或 `evidence_ref`。prompt 正文、artifact 正文和完整证据对象仍留在
各自的 request、TaskWorkspace 或 host frame 中。每个 consumer 都会拒绝旧版本或跨任务
signal。某个阶段已经产生终态结果时会直接发给 `transition.decide`，不会穿过未执行阶段。
AgentTask 会先 seal 并优雅 drain execution，再读取终态，因此已经接受的内部 signal 能够
完成，不会变成 provider-style early cancellation。

内部实现上，`flat` 和 `taskboard` 是协调策略，不是两套独立 execution carrier。
两者都会把 strategy 拥有的 work unit 下沉到内部 Block carrier，再进入
`ExecutionPlan` / Blocks / TriggerFlow evidence 路径。TaskBoard primitive 仍然
负责 board schedule、dependency state 和 patch validation；AgentTask 只把 bounded
card execution evidence 交给 carrier 承载。
TaskBoard 调度现在默认使用事件驱动的 `frontier` 模式：每张 card 完成后会立即
解锁并调度已满足依赖的后继 card；fan-in card 仍会等待所有声明的依赖完成。只有在
诊断或回归对照需要历史 tick-batch 行为时，才显式设置
`taskboard_scheduler="batch"`。
TaskBoard 仍是 `work.execute` 拥有的 work-producer subflow；它产出结构化 iteration
result 后，由外层图依次拥有 `outputs.materialize`、`evidence.ingest`、
`terminal.verify` 与 `transition.decide`。TaskBoard 不会在 `work.execute` 内自行 finalization、
verification 或运行隐藏 repair loop。这样 Flat 与 TaskBoard 会汇入同一套终态 owner，
同时避免重复承担物化、证据和验证责任。

在当前 4.1.3 线里，这是一个加固后的有边界公开 AgentTask strategy。
`agent.create_task_loop(...)` 保留为同一 task strategy 的兼容写法，适合代码需要把
strategy 选择说清楚的场景。两个 API 仍然
返回 `AgentExecution`；新代码应通过 `execution.get_result()` 或 execution 的
stream/meta facade 消费 data、text、stream、metadata、status 和 task refs，而不是
把 `AgentTask` 当成第二套 public lifecycle。

当 task-strategy `AgentExecution` 仍在运行时，host 可以用
`await execution.async_add_guidance(...)` 或 `execution.add_guidance(...)`
追加非阻塞的操作员上下文。guidance 会被立即记录在运行中的任务内存，通过 runtime
event 和 `guidance_items` 暴露，并在后续 Flat 或 TaskBoard 安全边界应用。默认不会
为它创建 RecordStore record；它不会暂停 execution，不会改写非 task route 的 prompt，
也不能作为完成证据。

```python
execution = agent.create_task(
    goal="准备事故总结。",
    success_criteria=["答案体现最新操作员上下文。"],
    execution="flat",
)

run_task = asyncio.create_task(execution.async_get_data())

await execution.async_add_guidance(
    "使用新上传的事故备注作为主要上下文。",
    author="operator",
)

data = await run_task
assert guidance["storage"] == "memory"
```

`AgentExecution.strategy("auto" | "direct" | "flat" | "taskboard")` 是外层
route/execution selector。`direct` 强制走普通 `model_request` route 和 ActionLoop，
不会创建 AgentTask；即使传入了 goal-like 字段，也由 host 自己负责这条 direct route
上的完成校验。`auto` 是默认值：普通 prompt/action run 保持 direct；显式 goals、
success criteria、task options、Skill selectors 或其他 task signals 才进入
AgentTask。进入 AgentTask 之后，`execution="auto"` 是默认 task execution strategy：
AgentTask 会请求模型做自然语言 task-shape analysis，再输出很薄的结构化
`execution_hint`；随后由策略层把实际执行形态解析为 `flat` 或 `taskboard`。这个 hint
只是策略证据；TaskBoard 不负责判断任务复杂度，verifier 也不能把 hint 当成完成证据。
需要强制线性 loop 时使用 `execution="flat"` 或 `.strategy("flat")`；只有 host 明确想用
TaskBoard 时才使用 `execution="taskboard"` 或 `.strategy("taskboard")`。嵌套的
AgentExecution 默认继承父执行的 strategy context，除非子执行显式调用
`.strategy(...)` 覆盖。

Auto 可以复用 task-shape analysis 中通过校验的最小 board 形状；如果这个候选 board
只是很小的线性序列，且没有真实 dependency、parallelism、readback 或 recovery 价值，
则会记录 diagnostics 并回落到 Flat。显式 `execution="taskboard"` 仍然保留
TaskBoard。有明确最终 TaskWorkspace 交付合同时，可以把已完成的终态 candidate 直接
提升到 verification，再由 Host 交付并读回。普通候选由 loop finalizer 判断语义完成；
叶卡已完成本身不代表整个任务完成。明确的交付、能力及必需上下文合同，以及确定性的
完整性或生命周期阻断，仍触发 terminal verification。

```python
agent.language("zh-CN")

execution = agent.create_task(
    goal="将旧版 Agently 脚本迁移到当前 4.1.x API，并确保它可以运行。",
    success_criteria=[
        "原始失败已被记录。",
        "脚本不再使用不兼容的旧 API。",
        "修复后的脚本可以运行，并产出预期结构化结果。",
    ],
    workspace="./legacy-script-project",
    max_iterations=4,
    verify="before_done",
    options={
        "agent_task": {
            "stream_progress": True,
            "stream_progress_background": True,
            "stream_snapshots": True,
            # 可选：用单独 model key 基于 snapshot 生成自然语言进展。
            # 不设置时使用模板 progress，不增加模型请求。
            # "progress_model_key": "cheap-progress-model",
            # 可选兼容别名：只影响 progress narration。
            # "progress_language": "zh-CN",
        },
    },
)

result = execution.get_result()

async for item in result.get_async_generator():
    if (item.meta or {}).get("stream_kind") == "progress_delta":
        print(item.delta or "", end="", flush=True)
    elif (item.meta or {}).get("stream_kind") == "progress":
        print("[PROGRESS]", item.value["message"])
    elif (item.meta or {}).get("stream_kind") == "snapshot":
        print("[SNAPSHOT]", item.path, item.value["snapshot"])

data = await result.async_get_data()
meta = await result.async_get_meta()
task_refs = result.task_refs
```

AgentTask 过程状态默认只保留在内存和运行日志中。任务运行本身不会把 planning、
observation、verification、evidence link、reflection 或 TaskBoard checkpoint
物化成 RecordStore record。下一轮从有界的进程内 ContextPackage 继续工作；可信文件
制品仍通过 TaskWorkspace 写入、回读和终态保留。

只有宿主确实需要跨进程恢复时，才设置
`options={"agent_task": {"record_store_recovery": True}}`。该选项只持久化紧凑恢复快照，
不会把 TaskWorkspace 变成完整的过程事件或审计档案。

TaskBoard checkpoint 还会包含有界的长任务定向投影：基于声明 criteria/card refs
生成的 acceptance index，以及包含 active/setback/blocked/deferred card、evidence
ref、artifact ref 和显式 state fact 的 handoff projection。`setback` 表示 readback、
repair、patch 或 continuation 这类可恢复执行挫折；单次出现不是硬停止。同一个 required
card contract 在相同稳定 subject 下返回 `setback`、`failed` 或 `blocked`，且该 card
确实在当前 tick 执行时，就计入终态收敛；第三次出现会在该 contract 第四次执行前以
blocked 结束。这些投影用于 resume
或人工检查时快速理解 board 状态，不会重放 raw trace。它们不是 `EvidenceEnvelope`
证据，也不会验收任务；语义完成仍归 verifier 和 host guard 判断。

AgentTask 的验证仍由模型判断拥有，但最终验收采用保守 guard。loop 会规范化
verifier 输出；当仍有 missing criteria、必需 action evidence 失败或被 blocked、
仍需 approval，或必需 final deliverable 缺失时，不会把任务标记为 complete。
这些 guard 决策会记录在 task diagnostics 中，让下一轮基于具体证据 replan，而不是
接受一个证据不足的完成声明。

task-strategy AgentExecution stream 会发出结构化结果事件，并默认发出紧凑中间状态
`snapshot` item。
自然语言 `progress` item 需要通过
`options={"agent_task": {"stream_progress": True}}` 显式打开；内置描述是模板文本，
未配置 `progress_model_key` 时不会增加模型请求或 token 消耗。设置
`progress_model_key` 后，AgentTask 会用这个单独 model key 在后台基于已产生的
snapshot 和任务元数据总结进展。模型生成的进度会先以
`stream_kind="progress_delta"` 的 delta 事件边生成边输出，然后再以完整的
`stream_kind="progress"` item 发出，便于日志和 UI 收敛状态。推荐用
`agent.language("zh-CN")` 设置 Agent 级语言策略，它会影响最终输出、关键过程文本、
progress 文本，以及 Search/Browse 的 locale 默认值；也可以用
`execution.language("zh-CN")` 只作用于单次 AgentExecution draft。单次执行仍可用
`options={"agent_task": {"progress_language": "zh-CN"}}` 作为兼容别名只控制 progress
语言，也可以用 `Agently.set_settings("agent_task.progress.language", "zh-CN")`
设置 progress 全局默认；`auto` 保持框架默认。主循环不会为了 progress 多产出字段，也不会等待 progress
总结完成。progress narrator 失败属于 side-channel diagnostics 和
warning 级 runtime event，不会把主 execution 标记成 `model.request_failed`。
progress model 只接收 operator-safe snapshot；底层 TaskWorkspace/SQLite fallback
等 developer diagnostics 仍保留在 snapshot 和 `task.meta()["diagnostics"]`，
但不会进入 progress model 输入。

对于文本消费方，`get_async_generator(type="delta")` 仍然是公开文本流。task-strategy
execution 中，它既包含模型生成的文本增量，也会把部分过程事件投影成段落文本：
模板 progress、snapshot、phase 状态、Action observation、Flat plan/action 摘要、
TaskBoard 状态表、retry marker 和任务终态结果。这些公开文本投影面向 operator 可读性，会摘要而不是
直出原始 JSON payload：Action 输入、密钥和原始命令 JSON 不进入文本 delta，
只有有界结果摘要可以显示；可恢复失败会表达为 setback；终态先显示明确的整体状态，
再显示有界 `final_response`。过程段落会和
模型正文 delta 保持边界，避免 CLI 文本黏连。AgentTask 内部 child/control 模型字段
只保留在结构化 `instant`/`all` stream 和详细诊断中，不会拼接进公开 delta。过长终态
文本会截断；结构化最终对象只提示从 full result stream 读取，不会序列化为原始 JSON。
Flat 投影是线性展示摘要：
plan 完成时说明上一个已完成动作和当前行动规划。Direct ModelRequest 和 Skill 步骤
仍保持为一个高层步骤；只有框架已经规范化且即将调度的具体 Action 批次，才展开为
`Next Action`、有序批次、并行批次或未知并发的中性批次。并行标签来自 Action owner
的真实 concurrency 策略；之后再用 started/completed/failed 增量更新同一批调用，
终态输出完成阶段数与验收状态。
TaskBoard 状态表仍是结构化 AgentTask event 的展示投影：第一次 TaskBoard 投影
输出紧凑表格，后续 tick 默认输出 card 状态变化摘要，而不是反复重印整张表。
TaskBoard 状态汇总为未开始、进行中、完成、失败、降级五类；完成与质量判断仍来自
verifier 和 host guard 结构化事实，而不是投影文本本身。emoji 只是带有明确文字的
冗余视觉标签，不改变任务状态。长结果保留在 TaskWorkspace/Action artifact；只有 AgentTask
已经通过 path、bytes、digest 和物理回读核验的 TaskWorkspace file ref，才会在 delta 中显示
最多三个可打开链接。未核验路径或模型自行声明的路径不会被渲染为链接。UI 如果需要
原始结构化事件载荷，包括 `path`、`value`、`delta`、`is_complete` 和 `meta`，应使用
`type="instant"`。当某个结构化 execution item 也能投影成自然语言流式文本时，
`instant` 会先产出原始 item，然后额外产出一个 synthetic
`AgentExecutionStreamData`，其 `path="$delta"`、`event_type="delta"`、
`source="agent_execution"`，并在 `meta["stream_kind"] == "text_projection"` 下记录
来源 path。它只是消费侧投影：`type="all"` 仍是 raw audit stream，不包含 synthetic
`$delta` item。heartbeat item 在 `instant` 中保持 structured-only，不追加 synthetic
`$delta` 文本。
更丰富的 UI 应消费 `instant`：source-addressed 结构化 path 用于更新状态面板，
synthetic `$delta` 用于可见过程文字，模型正文 path 则和过程/状态面板分开渲染。
AgentTask 默认不会启动额外 narrator 请求；过程自然语言来自同一次 planner、
verifier、card 或 finalizer 请求里的有界字段，例如 `progress_message`、
`short_summary`、`verification_summary` 和 `final_response`。

长时间静默等待时，如果超过
`agent_task.heartbeat_interval_seconds` 秒没有任何其他 stream item，
AgentTask 可以发出 `agent_task.heartbeat`。默认间隔是 10 秒。heartbeat
只是一条观测状态：它帮助 UI 和日志消费者知道当前阶段，但不满足证据要求，
不掩盖卡死，也不替代 request/no-progress/task deadline timeout。任何正常的
progress、snapshot、child-execution、delta 或 phase 事件都会重置静默计时，
因此活跃流不会被 heartbeat 污染。公开 `delta` 不投影 heartbeat 文案；
详细时间和原始 heartbeat payload 仍保留在结构化 stream 和日志中。

任务终态和 artifact 验收是两件事。AgentTask 终局 result dict 会为 accepted、
degraded、partial 和 blocked outcome 都提供面向用户的 `final_response`。`completed`
表示 verifier 已验收结果（`accepted=True`、`artifact_status="accepted"`）。当不可用或
部分证据已明确披露、并且降级后仍满足用户目标时，TaskBoard 会返回
`accepted=True` 和 `artifact_status="degraded"`，同时提供 `final_response`
说明降级边界。这不是质量捷径：语义验收仍由 verifier 和 host guards 决定。
`max_iterations` 仍可能留下有用的 TaskWorkspace 文件或 checkpoint，但它只是 partial
artifact（`accepted=False`、`artifact_status="partial"`），不是已完成的业务结果。
partial 和 blocked result 会包含 `final_response`，让调用方说明产出了什么、在哪里受阻、
哪些要求仍未满足。`get_text()` / `async_get_text()` 对 task-strategy result dict
会优先返回这个字段。`get_data()` 返回最终业务结果，并在可能时按 `output(...)`
解析；需要完整 task 终态 payload 时使用 `get_full_data()`。
TaskBoard 终态 payload 还可能包含 `taskboard.completion_notes`：这是对 card
完成摘要、已知缺口、verifier 备注和 acceptance progress 的有界过程投影。它适合 UI
进度和最终答复的降级/不足披露，但不是证据，也不能替代 verifier 验收。
对于模型生产的 verifier/finalizer 字段，`status`、`reason`、`progress_message`
或 `final_response` 这类自然语言只能作为展示上下文；完成、修复和验收判断必须来自
`is_complete`、`requires_block`、`criterion_checks[].satisfied` 等结构化布尔字段
以及 host guards。

### 语义终态验证与收敛

AgentTask 对适合终态判断的当前 candidate 只发起一次语义 verifier 请求。请求前，host
先构造一个带版本的 terminal-carrier inventory。TaskWorkspace carrier 由 host 签发的
carrier id、物理 path、content-version id 与 digest 标识；compact inline result 使用自己
的 carrier id 与 digest。文件或 inline 内容变化后会得到新的 carrier identity。历史 carrier
只保留为冷侧审计记录，不会混入当前 verifier 投影。

同一个 verifier response 同时覆盖 success criteria 与 material claims。它必须在
`criterion_checks` 中逐一返回本次提供的 `criterion_id`，并返回
`material_claim_coverage_complete` 和 `material_claim_checks`。请求前，host 只按结构把当前
carrier 的可见正文拆成精确文本 span，并为每个 span 分配一个请求内 `claim_key`。每个
material claim check 只返回一个已提供的 `claim_key`、一个 `claim_kind`、语义状态、本次提供的
evidence `reference_id`，并返回 `required_for_criterion_ids`。最后这个字段只能包含本次提供的
精确 criterion id；可选或额外 claim 返回空列表。直接事实可以是 `supported`；如果可见前提足以支持
保守结论，分析或建议可以是 `reasonable_derived`，不要求来源逐字复述结论。
`unsupported`、`contradicted` 与 `unverifiable` 都不能通过验收。

不受 success criterion 要求的 unsupported claim 可以进入 `delete_only` 局部 patch；被某项
criterion 要求的 claim 不可以。host 会保留这条关系并强制进入 `replan_segment` 证据重获取。
如果没有新的已授权 source、locator 或 capability 路径，或者 planner 重复了已经耗尽的
canonical retrieval plan，TaskBoard 会 blocked 或请求澄清，而不是重复执行相同 evidence/repair
card 直到触发安全上限。

host 会校验 criterion id、claim key、evidence id 与证据资格，再从本次 claim-key map
确定性恢复 canonical carrier id、精确 quote、path 与 content version。它不分词 verifier
prose、不使用业务关键词/正则，也不再运行独立的 claim inventory、
source selection、逐 claim support judgment 或 empty-inventory review 模型循环。
candidate、delivery、acceptance 与 verifier-readback records 不能为后代 carrier 自证。
正向 claim 需要可见且合格的 source content；明确 unavailable claim 可以使用匹配的
failed/empty 结构化来源事实。task reference catalog 继续保存完整冷侧审计记录；verifier
只接收一份有界、含正文的 ledger，以及轻量 locator/ref indexes。

verifier request 的每个返回字段只对应一套 selection domain：`evidence_ledger.items` 是唯一
暴露 `reference_id` 的位置，也是 host 校验 `evidence_ids` 时使用的同一份不可变集合；
`material_claim_candidates` 是唯一 claim selection domain，只暴露 `claim_key`、当前精确文本
span 与任务相关 locator 事实。acceptance locator 与 trusted artifact index 只用于检查，
不暴露 selection id；execution 与 cumulative evidence summary 也会移除 evidence selection
id，仅可保留不能由模型返回的 Action call id 作为 inspection correlation。即使此前 work unit
尚未持久化 pinned `evidence_use`，当前有界 source
records 也会进入第一次终态请求；transport reference 既不会进入 grounding set，也不会虚增
其 `omitted_count`。

required capability/Action、output/schema、artifact/readback、evidence binding、criterion、
material claim 与 lifecycle guards 仍彼此独立；最终 acceptance 是它们的确定性合取。
material-claim 失败会产生结构化 `material_claim_repair_contract`，Flat 与 TaskBoard 直接
消费 contract，不解析 verifier prose。可信 file carrier 的修复路径只运行一次有界结构化
patch 请求，再由 host 校验并应用精确 replacement；它不会打开通用 AgentExecution /
ActionRuntime round，也不会因为挂载了 `write_file` 就允许整文件重写。
该 patch 请求只携带已授权 carrier identity、精确 dirty claim contract、有界 carrier quote
和输出 schema。无关 dependency results、board history、EvidenceLedger 正文与 acceptance
projection 都保留在冷侧；请求大小由 dirty scope 决定，而不是由任意 token 硬上限决定。

TaskBoard 会在产出结构化 `evidence_use` 的边界修复证据绑定。card、control、finalizer
与 binding-repair prompt ledger 中，每项都只向模型暴露一个稳定 `reference_id`，并
附带有界的 Action 输入/结果或 locator 事实；canonical id、请求内 `cite_as`、原始 call
id 与 aliases 留在 host 侧。终态 verifier 也使用同一套单身份投影，并且 criterion check
只能返回本次提供的 `reference_id`。同一个 canonical evidence object 的 raw 与 compact
表示会联回同一个 task reference；snapshot/content version/hash 变化时仍会分配新 reference。
已经加载的 Skill guidance readback 也进入同一份 card-local content ledger，因此 card 可以
直接绑定“遵循 Skill guidance”的 claim，而不必重跑 synthesis 或把 guidance 当作 ref-only pointer。
当前 card execution 新产出的证据会排在历史 board 证据之前，
固定候选预算不会再截掉刚生成的 Action result 或 artifact readback。仅绑定失败不会重试已经完成的 Action card，因此不会重复执行已
成功的外部 Action；框架只尝试一次有界 binding repair，仍无法解析时保持 untrusted。
无法解析的模型 binding 不能把 canonical 已成功的 Action 或 completed card 反向改成
业务执行失败；card execution 与终态语义 acceptance 是两个独立 owner。finalizer repair
会在终态 verifier 之前生效，其规范化 binding 可以在 host 侧固定 canonical evidence，
但 finalizer 的 `evidence_use` 不会复制进 verifier 的 `execution_result`。终态 verifier
只从自己收到的单一 stable ledger 中独立选择 grounding id。dependency、board、revision、evidence-ledger 与
artifact-draft dependency 各自使用独立的有界 prompt 投影，避免冷侧 execution metadata
在下一次 ActionRuntime 或 artifact-body 请求中递归倍增。

TaskWorkspace artifact delivery 还会从实际解析出的 Markdown section headings 确定性生成
轻量 locator。verifier 可直接取得中段/尾段的有界 readback，不再为“重新说出精确 heading”
额外调度模型 repair。material-claim repair 只允许修改结构化 contract 指定的失败
checks。对于可信的 file-backed terminal carrier，即使其路径来自当前 artifact/readback、
而不是显式 required-deliverable option，repair card 也会携带规范化后的路径授权。唯一叶子
delivery card 负责终态文件投影，中间 working artifact 只保留为冷侧证据，不再按 bytes 与
实际交付文件竞争。当 required TaskWorkspace path 已经存在时，它是唯一进入终态 verifier 的
TaskWorkspace carrier；其它可信 working files 只保留为冷侧 evidence，不再作为另一个终态
产物参与竞争。verifier 只返回选中的 `claim_key`；host 从该请求的不可变映射中恢复精确
`artifact_quote`、carrier id、path 与 content version，随后才生成 repair contract。模型只返回
有界 replace operations，由 host patch owner 应用；整文件 write、replace-all、未授权 path
或 contract claim 之外的 old text 都会 fail closed，从 repair 模型职责中移除完整正文抄写。
operation shape 复用 TaskWorkspace edit contract 的 `old_string` / `new_string` 字段，每个
host-issued `claim_key` 必须且只能对应一个 operation。写入前，host 会把 operation 联回
不可变 `segment_id`，并确认当前 promoted `content_version_id` 仍等于 repair contract 中
声明的版本。旧 version、重复或未知 claim key、非唯一精确匹配、无关路径都会 fail closed；
成功 readback 会创建新的 content version。scope comparison 会忽略 artifact 标签外层成对的
Markdown emphasis，但真正应用补丁时，`old_string` 仍必须与 TaskWorkspace 原文精确匹配。
没有可信 TaskWorkspace candidate 的 inline candidate 仍使用有界 corrected-result 路径。

control card 的 `remaining_work` 只描述当前 card objective/done_when 内的工作。已经分配给
下游 card 的写入或交付任务不会让上游 synthesis card 保持未完成，也不会触发完整正文重生成。
`next_board_action=continue` 与 `next_board_action=stop` 都是 board progression 决定，
不能覆盖当前 card 的显式状态。`status=completed`、`sufficient=true` 的 stop 输出仍是
completed；只有显式 card status 或 `next_board_action=block` 才会把 control result 置为
blocked。

TaskBoard 的 artifact/file evidence 投影会保留 producer `role` 与 `source`。生成出的
TaskWorkspace artifact 即使以相同内容复制到另一个 path 或 content version，仍是 transport
记录；这也包括 `agent_task.workspace_artifact.*` 下由 host 应用 material-claim patch 后生成的
readback。它们不能退化成独立 source 来支撑 carrier 自己；独立 Action、source 与
TaskWorkspace readback evidence 仍按原有规则参与验证。

重复终态修复按精确 `(gate_kind, issue_code, contract_subject)` key 计数；不同
issue code 不共享、也不推进彼此的收敛计数器。如果同一个精确问题在
carrier/source/capability/contract 状态未变化时再次出现，AgentTask 会记录 occurrence，
而不再支付一次重复 verifier request。同一个精确问题最多调度两次 repair；第三次出现时，
任务以 `status="blocked"`、
`accepted=False` 结束；存在有用 candidate 时保留为 `artifact_status="partial"`，同时
返回 missing criteria 和解释性的 `final_response`，且不会运行第四次 repair。required
Action 不可用、policy 被拒绝/阻塞、结构化 blocked lifecycle fact 或 immutable candidate
contract 无效时会立即 fail closed，不消耗三次收敛额度。

terminal verifier 的 malformed output 归 verifier boundary 处理，而不是 artifact repair。
未知或不合格的返回 id 会统一规范化为稳定的
`(output_contract, terminal_verifier_output_invalid,
verification:response)` 问题，并报告精确 field、invalid ids 与本轮 offered grounding
snapshot。如果多个 response section 同时不合法，host 会把各 section 的结构化 requirement
合并成同一个 stable issue repair contract，并连同当前 offered claim/evidence key 集合
传回下一次请求。TriggerFlow 只重入 `terminal.verify -> transition.decide`，让 verifier 修正结构化
response；不会重跑 work、重新物化 output、重建 evidence，也不会创建 TaskBoard repair
card。TaskBoard 还会复用已经准备好的 final candidate，因此 verifier-only retry 不会重复
finalizer 请求。第三次相同协议错误会阻塞任务。

这条收敛规则也覆盖 required TaskBoard card 反复返回非满足结构化状态
`setback`、`failed` 或 `blocked` 的情况。只有当前 tick 实际执行的 card 才会计数，
因此 board history 中保留的旧结果不会在执行无关工作时推进计数器。结构化且不可恢复的
capability 或 policy failure 仍由其所属门禁立即 fail closed。

对于 required TaskWorkspace deliverable，终态 finalization 以当前物理 locator/content-version
readback 为权威。旧 content version 可继续作为冷审计 identity 保留，但不能因为历史
candidate 更长而重新覆盖当前文件。Flat 与 TaskBoard 会把 artifact body、compact inline
result 和 trusted refs 作为三个独立 carrier。显式 `candidate_final_result` 不会被复制成
artifact body；文件正文只能来自显式 artifact payload，或来自绑定到声明 manifest path
且执行成功的 Action write。即使 planner 选择了 `inline_final`，后者的物理 readback 仍会被
提升为可信 artifact。后续 terminal verification 会在 repair 全程保持这个当前或累计可信
file carrier，不再静默切换到 inline summary hash。

如果没有单独的显式答案，file-backed 终态才携带简洁 TaskWorkspace pointer；若 execution 在
`final.md` 之外还返回非空 compact `candidate_final_result` 或 `final_result`，AgentTask 会把
这个有界答案与可信 file refs 一起保留，而不是用 pointer 覆盖它。文件正文仍留在
TaskWorkspace。未知 carrier id、未知 evidence id，或不是当前 carrier 精确 span 的 quote 都会
fail closed，并生成结构化 material-claim repair contract。

公共正文交付保留完整值；日志和元数据的预览长度限制不会裁剪 `final_result`。

TaskBoard 的 model/control 卡片使用 `candidate_final_result` 作为唯一完整正文槽。普通文字任务保持正文答复；只有任务或卡片声明的最终文件交付合同才授权 Host 自动物化，并开放 `artifact_manifest` 内容计划。旧响应的 `final_result` / `artifact_markdown` 正文仍可读取，但字段名本身不再授权创建文件。用户请求文件而未指定文件名时，由规划模型在既有交付字段中选择相对路径。最终卡片已返回完整正文但未给 manifest 路径时，Host 将现有正文暂存到指定目标对应的候选位置，不为搬运文件重新生成正文。readback 卡片也会通过现有 ContextReader 执行所声明的 `scoped_retrieval`；仅定位的结果仍是引用，正文读取的实际完整性则保留在共享证据账本中。

当 bounded step 返回显式 artifact，或具有文件交付合同的 TaskBoard card 返回完整正文或分段
`artifact_manifest` 时，AgentTask 会通过绑定的 TaskWorkspace 写入交付物，并立刻
readback。冷证据会记录 `path`、`bytes`、`sha256`、有界 preview 和 `file_refs`；
模型热 verifier 输入使用 path/ref handle、有界内容或 preview、截断状态。对于长篇、
分段或重自然语言交付物，应先选择合适的内容载体：单一自由正文可以直接生成自然
Markdown / plain text，不必为了携带正文而声明 `.output()`；如果调用方需要可独立寻址
的字段，可在适合目标模型和消费方的情况下使用
`.output(..., format=...)` 的 `xml_field`、`hybrid` 或 `yaml_literal`；AgentTask 的
TaskWorkspace artifact writer 消费的是 AgentExecution stream 事实：自然正文来自原始
delta item，retry 边界优先来自 provider 报告的 `$status`。因此这条自然文本路径不要求
draft request 使用 `.output()`。非流式草稿没有 delta 时，写入器读取同一次成功执行的最终正文，不追加模型请求，也不重复写入已有流式正文。如果 public `type="delta"` replay marker
`"<$retry>...</$retry>"` 到达 artifact consumer，它会被当作 public replay
delimiter 处理，绝不会写入或转运为 deliverable text，也不会被提升为 retry metadata；
structured `$status` 仍是 retry control source。如果 bounded work unit 已经在结构化
`evidence` 里返回完整 Markdown artifact body，AgentTask 只有在 evidence item 明确标注为
artifact/body/deliverable/Markdown 或绑定到 manifest path 时，才会把它当作可写入的交付正文；
未标注的 source content 和 source excerpt 仍只是 evidence snippet，不是文件正文。TaskWorkspace 写入和 readback 成功后，
残留的“写文件”类 `remaining_work` 会交给终局 verifier 判断，而不是再强制一轮只负责写同一文件的
iteration。状态、证据和校验保持为单独的紧凑 judgment/readback contract。若 AgentTask 必须交付可信文件
artifact，再使用 `artifact_manifest.sections` 加 TaskWorkspace readback。模型声明的
`file_refs` 只作为 diagnostics，只有框架完成 TaskWorkspace 写入和读回后才是可信证据，
同时仍保留真实 `final.md` 或其他成品文件供 host 复核。TaskBoard finalization 会把
file-backed deliverable 正文留在 TaskWorkspace；返回的 `final_result` 应保持为简洁摘要或
path/ref pointer，而不是文件正文的第二份拷贝。completed terminal leaf 显式返回的
`final_result` 是 summary/answer carrier，会与 artifact ref 一起保留；没有这个字段时才
回退为 path/ref pointer。

同一套 ref-backed 路径也可以用于中间过程。某个步骤可以下载文件、保存网页快照、
写入生成代码、沉淀搜索笔记或类似 memory 的任务笔记，或把大段抽取文本持久化为 TaskWorkspace / Action artifact refs。
热路径 prompt 应只携带紧凑 refs 和有界 preview；后续 block 真的需要正文时，再通过
`read_file(max_bytes=..., offset=...)` 或 artifact readback 打开 scoped snippet。
readback work unit 的热 payload 也只使用同一类紧凑 refs；完整 refs 留在冷侧
TaskWorkspace/Blocks 证据中，用于程序化 readback 和审计。这些中间 refs 是执行证据，不是最终交付物存在的证明。发现了某个 URL、路径、下载或
快照 ref，也不代表已经读过其内容；在有界 readback 或 content preview 可见之前，
它仍是 `ref_only`。显式 `content`、`excerpt` 或 `snippet` 字段只算可见片段的
有界 preview，不代表已经读过整份文件。source-grounded 交付物要么用结构化 `target_refs` 请求读取这些
未读 refs，要么把它们标为 discovered-only，不能声称事实来自未读内容。如果 Action
artifact readback 暴露了已物化下载文件的 TaskWorkspace `file_refs`，TaskBoard readback
会把这些嵌套 refs 提升为 card-level `file_refs`，让后续工作可以继续用 TaskWorkspace
readback，而不是依赖埋在 JSON preview 里的路径字符串。若非最终 TaskBoard card 提议写入 `final.md` 这类
required final path，AgentTask 会把该中间 artifact 重定位到
`working/taskboard/<card-id>/...`，并把声明的最终路径留给最终 synthesis/finalization card。
由框架生成的 final repair card 会把声明的 deliverable 写到 card 作用域内的
terminal-candidate 路径，而不会在 repair 阶段覆盖根路径。终验时，host 校验过的
delivery contract 会把已完整读回的 candidate 映射到 required target；verifier 以该
candidate 作为 target 的临时 carrier 判断内容和证据，不能仅因为 target 在验收前尚不存在
就拒绝 candidate。只有语义终验通过后，host 才会按摘要固定地把完全相同的字节原子提升到
声明的根路径，并完成 target 全量读回、摘要和字节数校验；这些 host guard 全部通过后任务
才能完成。
Flat source refs 也遵守同一边界：repository clone/list manifest 中发现的文件路径在文件读取、
artifact readback 或有界 content preview 出现前都是 `ref_only`。verifier 或 repair
planner 可以复用这些精确路径作为检索目标，但不能把它们当成文件内容事实的证明。
TaskBoard final verification 也会接收 board-level source refs，并保留同一套
`content_state` 边界；final synthesis 不能把 discovered path 升级为 source-content
evidence，除非已经有有界 preview/readback。

Flat 和 TaskBoard 的 work unit 也会收到同一份 task context contract。运行时
metadata 可以为诊断记录紧凑的 `current_time` 事实，但默认 model-hot prompt 只会收到
prompt-safe 的可用性元信息，不携带具体运行时 timestamp。对于 current、latest、recent
或 as-of 任务，如果业务上需要具体日期或来源时间，应由 caller 或 source 明确提供。该
contract 只是 model decision、planning、evidence selection 和 source-boundary handling
的上下文，不能被模型直接当作业务事实，也不会设置模型调用、工具调用、节点数、迭代数或
wall-clock 硬上限。

当模型生成的 TaskBoard card 携带一个结构有效、但各 query group 的 `max_results`
预留总量超过 64 的 scoped TaskContext retrieval plan 时，host 会保留其 Context
source kind，将它拆成多个有界 retrieval card 和一个依赖这些批次的 continuation。
框架不会静默截断，也不会把 pinned repository、Skill、Memory 或其他 Context source
改写成 TaskWorkspace Action。该归一化可通过
`agent_task.taskboard.plan.normalized` 观察。若单个 query group 本身超过容量、数量
非法或 source kind 未获提供，则仍会 fail closed，交给结构化 replan。

在 scoped query group 内，`path` 负责限定文件或目录，`pattern` 只表示文件名 glob，
例如 `*.py` 或 `**`。如果需要把有界读取定位到选定 source 内的精确代码符号或文本片段，
使用 `filters.content_contains`；每个精确 locator 会被归一化成独立的有界 query group。
语义意图继续放在 `query` 中，不要把代码符号或正文短语放进 `pattern`。

TaskBoard readback card 可以用有界冷读回读取 Action artifact refs 和可信的
TaskWorkspace file refs。框架生成的 readback card 会把 evidence scope 扩展到直接依赖和
上游 evidence card，所以 control-card readback 仍能读取更早 evidence-gathering card
产出的 Action refs。若框架生成的 continuation card 仍报告同一批 readback 不足，框架不会
继续递归合成新的 readback/continuation 链；该 card 必须提出其他可执行工作，或者带
diagnostics 保持 setback/blocked。
对于 scoped TaskWorkspace retrieval，`evidence_snippet` 会明确标记有界片段是否
`truncated`。如果带 scoped retrieval 的 TaskBoard card 返回 setback/blocked/insufficient，
且没有给出显式 next action，AgentTask 会把这个局部不足转成 action-capable evidence
card：使用放宽后的有界检索计划补证据，再接一个 continuation card。检索结果仍只是事实
上下文；是否足够继续由 continuation card 判断。
当缺失证据是新的具体 URL、路径或 ref，而不是已有 Action / TaskWorkspace ref 时，control card
应返回 `next_board_action="readback"` 加结构化 `target_refs`。AgentTask 会把这个紧凑意图
转成可执行 action 的 evidence card，由它负责下载、保存快照或物化目标，再运行 continuation
card。只写在 `gaps` 自然语言里的 URL 属于 diagnostics，不会被解析成可执行目标。
如果 control card 返回的是 `next_board_action="patch"` 加 TaskWorkspace 文本 patch
proposal，AgentTask 会把补丁应用到绑定的 TaskWorkspace 文件，写回后再读回并返回可信
`file_refs`。这只负责物化修补事实；最终是否完成仍由终局验收和 host guards 判断。
对于 `completed` 且 `sufficient=True` 的 control 输出，非致命 `gaps` 不会阻止 TaskWorkspace
artifact 物化；`remaining_work`、setback/blocked 状态、repair 或 readback 仍会阻止写入。
写入 artifact 只是为后续 readback 和 verification 创建证据，不代表最终任务已经被接受。
Flat 和 TaskBoard 都不需要在每个中间 work unit 后额外调用独立 verifier。Flat step
返回非空 `remaining_work` 时，默认表示当前 step 仍是中间工作；下一轮 iteration
会消费这些新事实并决定下一步行动。step 也可以返回
`ready_for_final_verification=false` 来显式表达这一点。只有当前结果需要立即进入
终局、阻塞或风险 verification 时，才显式设置 `ready_for_final_verification=true`。TaskBoard 中真正消费 dependency evidence 的下游
card 判断这些信息是否足够完成自己的目标。独立 verifier 应保留给终局验收、fan-in/control
合流验收、证据/artifact 边界审计、矛盾或高风险复核。
当终局 verifier 返回未完成结果时，紧凑的 `repair_context` 会进入下一轮 Flat work
unit；如果下一轮交付正文走 TaskWorkspace artifact draft，也会进入专门写文件正文的 draft
请求。这样真正重写或读取 artifact 的 consumer 能看到精确的 `acceptance_delta`、
repair constraints、next-step requirements 和可用 evidence anchors，同时不把冷侧完整性
metadata 重新塞回模型热路径。

如果终局 verifier 请求的是新的 evidence segment，而不是已挂载 capability 的修复，
专门的 ModelRequest 只能从 host 实际提供的 TaskContext source kinds 中选择有界语义
查询，后续 evidence card 仍通过 ContextReader 执行。完成条件要求 card 的
`evidence_use` 精确绑定 EvidenceLedger 在本 card 后新增的有正文 reference identity；
模型自然语言声称“已经获得证据”不能绕过这项 host guard。

source adapter 可以把 canonical repository URL、pinned commit 等小型权威 descriptor 事实
作为普通 typed `information` descriptor 和 exact read 暴露。ContextReader 披露该 block 后，
AgentTask 会像处理其他合格 ContextPackage information 一样为其签发 evidence binding；未披露
的 adapter metadata 不会自动成为证据。

对于 TaskBoard，已声明且 required 的 `action_succeeded` capability requirement 也拥有
repair dispatch。如果缺失的正是该 Action evidence，并且能力已经挂载，TaskBoard 会创建
Action-shaped repair，继续携带原始 capability id 与 kind。verifier prose 不负责选择
Action，TaskWorkspace readback 也不能满足 Action requirement。如果能力不可用，repair 会
fail closed，不会替换成其它操作。该 repair card 会让这份精确 requirement 依次穿过
card contract、`WorkUnitIntent`、Blocks
capability resolution 和 TaskBoard Action allow/required scope；它不是 repair
prompt 里的提示语。每个 Action card 只接收一个 card-local
objective/done-when work unit 及其 dependency evidence。global task 只为 response synthesis
提供方向，不授权该 card 执行 sibling work。

TaskBoard Action card 默认不使用开放式 ActionLoop。board plan 已经包含完整的
`action_id` 与 `action_input` 时，Blocks carrier 会先对照已挂载 Action registry
校验命令，再直接交给 ActionRuntime 执行，不产生 Action planning 模型请求。经过校验且
非空的 `action_commands` 契约强于泛化的 `allowed_execution_shape` 提示；如果 planner
同时返回两者且发生冲突，宿主会把该 card 归一化为 Action 执行，并在 diagnostics 中保留
原始提示与归一化原因。Action id
已知、但参数依赖上游 card 结果时，只发出一次窄结构化请求来返回有界的
`action_commands` 批次，宿主校验后仍走同一条直接执行路径。所有模型生成的命令都会在
dispatch 前按已挂载 Action 声明的输入字段校验。首轮 plan 中的无效命令会被移除，并通过
携带权威契约的窄请求重做一次；窄请求仍返回无效参数时会在进入 ActionRuntime 前 fail
closed。精确的 TaskWorkspace 最终产物
交接同样会直接降低为必需的 write/readback Actions。未知 Action id、遗漏必需 Action id
或无效参数都会 fail closed。只有后续 Action 选择确实依赖前一轮 Action 结果的开放式普通
Agent 执行才保留多轮 ActionLoop。

首轮 TaskBoard planner 会把任务的结构化 capability-evidence requirements 作为规划
contract 的一部分接收。`action_commands` 表示该 card 的穷尽命令批次，不是执行后还会
静默继续 synthesis 的第一阶段。如果首轮 card 同时包含非交付 Action 命令和
`final_workspace_deliverables`，AgentTask 会在 board validation 前把它拆成上游 Action
card 与依赖该 card 的 control card。没有精确命令批次的最终交付 card 会按 control card
处理；已经完整且通过 schema 校验的 TaskWorkspace write 命令仍保持 Action card。下游 control
request 接收已收集 evidence 并拥有 synthesis。任务 contract 显式要求 TaskWorkspace write/read
Actions 时，合成正文会先通过这些 Actions 真实写入并读回，再由普通 artifact delivery
接纳。这样 Action success、TaskWorkspace readback 与最终正文所有权位于同一条可见的值/事件链，
不再依赖后续 repair loop 补救。control card 产生的 Action records 与普通 Action card
使用同一个 execution-summary carrier，因此终态 capability check 能看到已经完成的
write/read 事件，不会再调度重复 repair。`action_succeeded` requirement 由任意一条真实成功
Action record 满足；同一 Action 的另一次失败仍保留给 execution-risk 处理，但不会抹掉已经
发生的成功事件。

如果后续 TaskBoard leaf 只验证或引用同一产物，宿主会把它请求的 path 与 canonical
dependency `TaskBoardCardResult` 中的可信 artifact refs 做确定性关联，并接纳当前物理读回。
模型重复返回的 `artifact_manifest` 或 `file_refs` 投影不会授权再次发起 artifact-draft
请求，也不能覆盖 dependency owner 已写入的正文。

Flat AgentTask step 使用同一个命令降低 owner。Flat planner 只从紧凑 capability list
选择 `required_action_ids`，不会在缺少严格 kwargs schema 时猜测参数。如果内部结构化 plan
已经携带通过校验的 `action_commands`，宿主无需追加规划请求即可执行；否则只发出一次窄结构化
请求，该请求仅接收必需 Actions 的权威 schema 与有界 step context，返回完整命令批次，或
`requires_observation=true` 与空命令列表。所有参数已经有依据时，宿主校验后串行交给
ActionRuntime；仅顺序依赖（例如写入再读取已知路径）不需要另开规划轮次。
后续参数必须依赖前一 Action 的新结果时，即使全部 Action id 已知，Flat 也会使用已有
有界子 ActionLoop，先观察结果再规划下一调用。交接前不会执行半个批次。
就绪字段缺失或自相矛盾会明确失败；未知或不可用的必需 Action 在窄请求之前失败。
显式 `action_commands` 仍是固定 kwargs，不是结果引用或变量替换语言。
子执行的 scope、权限、deadline 和最终验收保持不变；交接时还会将批次 required ids
绑定到子执行已有的 `require_actions` 证据门槛，不能仅设置可见范围。
该门槛证明成功调用，不代替参数语义或重复调用次数的业务验收。交接时的一次窄请求记录在
`execution_meta.action_command_planning` 中。
这种自适应交接不套用普通子执行隐式的两轮上限，因为调用后可能还需要终态请求；
显式任务 `action_loop_max_rounds`、任务 deadline 和请求预算继续生效。

如果该步骤已完成 `scoped_retrieval`，参数请求同时接收本步有界读取结果和既有证据账本，
保留原任务、步骤和上下文，不重复读取或额外请求模型。失败、空结果、仅引用和截断状态
保持原义；未读正文不能作为参数依据。没有本步读取时请求保持原样；显式预计划命令仍使用
固定参数，转交子执行时继续携带同一份证据。

任务级 `require_actions` 在全任务累计 Action 证据中检查，不会重复变成每次子请求的
必调用要求。仅撰写最终答复或产物的子执行无需重做已完成的 Action；显式 step-required
仍由子执行检查，任务所需 Action 缺失或失败仍会阻止验收。Agent 默认要求在任务创建时
确定，并随任务 options 保存，恢复时不因后来新增默认要求而改变原任务义务。

Flat 中仅产生普通观察的成功命令批次会交给下一步消费，不先把尚未完成全任务判成失败。
这一交接只适用于没有显式终态就绪标记、实际候选、产物引用或既存终态修复的已知命令结果；
计划中的 `inline_final` 只声明交付格式，不表示正文已经生成。累计 required 待办和精确
Action 返回值继续传递，真实失败、权限、grounding 与修复要求不被隐藏。
最终候选仍须经过终态验收；仅有中间观察或预算耗尽不能成为已接受结果。
TaskBoard、产物读回和外层 `review` / `validate` 的原有职责不变。

`examples/agent_task/action_result_dependency.py` 用真实模型执行工单读取/确认任务，
revision 只在 Action 调用时生成。配置 `MODEL_BASE_URL`、`MODEL_API_KEY` 和
`MODEL_NAME` 即可运行；可选 `MODEL_REQUEST_OPTIONS` 接收 JSON 对象形式的 provider 参数。

AgentTask observation 也会在结构化 stream 上发布归一化 action 事实：
`agent_task.action.started`、`agent_task.action.completed` 和
`agent_task.action.failed`。这些事件从已有 Action records 汇总安全的 input summary、
result preview、refs、耗时、diagnostics 和 work-unit 归属。已恢复的 `success` 或
`partial_success` Action records 会投影为 completed observation；failed event 只保留给真实
失败、blocked、timeout 或未恢复 error 记录。它们只是给 DevTools、UI 和
实验日志使用的 observation facts；是否有用、质量如何、任务是否完成仍由下游 consumer、
终局 verifier/final control 和 strategy 判断。

写入成功且读回可信时，verifier 输入只携带一个有界且包含正文的 evidence ledger，
其中包含相关 readback content/preview；acceptance locator、artifact 与 overflow
ref/state 则使用不带正文的轻量 index。raw evidence 和完整性 metadata 继续保留给
有范围的 TaskWorkspace readback 与审计，不再复制进每个热 summary。
如果同一个 claim 同时引用了有效 content evidence，以及结构上不能作为正向支持的附带
id（例如 failed 或 `ref_only` record），binding repair 只能删除不兼容 id，并且保留后的
binding 必须再次通过同一确定性 guard。它不会创造正向证据；没有兼容 id 可保留时仍然
fail closed。
`capability_evidence.artifacts.readback` 仍携带路径 handle；
在 `max_iterations=1` 下，真实已写入且可读回的
artifact 不应只因为 evidence 链缺失而变成 partial。如果读回失败，或缺少可信的
`path` / `bytes` / `sha256` 证据，diagnostics 会使用
`agent_task.workspace_artifact.readback_failed` 或
`agent_task.workspace_artifact.readback_insufficient`，明确报告 TaskWorkspace artifact
readback missing/insufficient，而不是泛化为预算或迭代不足。

如果结构化 task input 或 output contract 声明了必需交付物，AgentTask 的 host
guard 会要求这些 TaskWorkspace 文件真实存在并可读回，才允许验收完成。verifier
声称文件已存在并不够，必须以声明的最终路径 TaskWorkspace readback 为准。

对于框架介绍、API 指南这类公开参考材料，task verifier 的 accepted 仍不等于来源质量
保证。应把当前 docs/spec/source references 喂入任务，或增加 Agently model-judge /
source-reference 校验，避免旧 API、泛化 API 只因为 task-level verifier 接受草稿而通过。

带强结构合同的中间处理步骤必须在所属的 `ModelRequest` 或 `AgentExecution` 上使用
Agently `.output(..., format=...)`。不要只为了控制长篇自然语言正文而给纯正文生成请求
添加限制性的 JSON `.output()` contract。紧凑控制 payload 可以使用 JSON；当内容较重的
payload 确实需要结构化合同，可按场景使用 `xml_field` 表达 XML-like 字段边界，使用
`hybrid` 表达正文加类型化控制字段，或在适合目标模型和消费方时使用 `yaml_literal`。
如果声明的非 JSON 格式解析失败，Agently 会尝试用 JSON 解析兜底，并且只有解析结果是
dict 时才接受。execution output contract 存在时，task `final_result` 也执行同一守卫。

`examples/agent_task/goal_effort_public_stream.py` 是这个合同的公开链式 API
流式证明。它运行
`.goal(...).effort(...).input(...).output(...).strategy("flat")`，消费
`get_async_generator()`，流式输出模型生成的进度 delta，并检查 execution prompt
snapshot 是否进入 AgentTask 的 planning、execution 和 verification。
`examples/agent_task/goal_pursuit_acceptance_matrix.py` 仍保留为 accepted 与
non-accepted 终态矩阵脚本。

`examples/agent_task/real_complex_bundle_goal_stream.py` 是同一路径下的高封装
真实复杂任务证明。它通过公开 Agent capability API 挂载 Search、AMap MCP、
TaskWorkspace 文件 Actions 和 CocoonAI `architecture-diagram` Skill，然后让任务循环
生成 operator 日报、杭州商务游记和 HTML/SVG 架构图，并流式输出自然语言进度
delta。它使用多轮 bounded direct steps，以证明当前公开 AgentTask 生命周期，而不
依赖混合 DynamicTask/DAG 执行。较底层的
`examples/blocks/07_real_complex_bundle_stream.py` 只保留为 Blocks 外部能力
substrate 探针，不作为推荐业务入口。

`examples/agent_task/agently_architecture_diagram_task.py` 是同一路径下更长的
设计文档实验。它保留 `.goal(...).effort(...).strategy("task")` 只是为了验证旧兼容
拼写，不是新代码的推荐 selector。新代码需要普通 model_request/ActionLoop route 时
使用 `.strategy("direct")`；host 明确想要 AgentTask 时使用 `.strategy("flat")` 或
`.strategy("taskboard")`。这个示例同时使用仓库源码资料 Action、TaskWorkspace 文件
Actions，以及独立 Agently model judge，生成并复查一份层次清晰的 Agently 架构图。

`examples/agent_task_experiments/` 提供基于核心 AgentTask 实验场景的精简开发者
示例：股票风险简报、Agent 工程周报、LMCC 模拟题、仓库阅读和多运行时代码执行。
同一目录也包含混合能力示例：旅行规划、股票风险分析、市场进入分析会组合 native
Actions、真实 MCP 注册、本地 Skills、TaskWorkspace 文件 Actions 和 delta stream。这些示例
刻意使用 `agent.create_task(...)` 默认值，包括默认的 `execution="auto"`，让示例代码
更接近日常应用写法，并通过 `get_async_generator(type="delta")` 展示任务信息流。

第一版公开 slice 有明确边界：单任务、单 Agent owner、约 2-5 次迭代，并通过
`AgentExecution` 执行 bounded step。这些 step 可以使用调用方已经在 Agent 上启用的
Actions、Skills 或 DAG 候选，也可以使用当前 execution 上临时挂载的 DAG 候选。
AgentTask 不提供多任务协同、后台自治、分布式租约、step 内 pause/resume 或
长期记忆管理。这个 slice 的崩溃恢复通过 `agent.resume(...)` /
`agent.async_resume(...)` 暴露，它会重建 task-strategy `AgentExecution`，而不是把
AgentTask 暴露成第二套公开生命周期。

### 崩溃后恢复任务

RecordStore 恢复需要显式启用。原始任务必须使用稳定的 `task_id` 并设置
`record_store_recovery=True`：

```python
execution = agent.create_task(
    task_id="issue-123",
    goal="修复问题并验证结果。",
    options={"agent_task": {"record_store_recovery": True}},
)
await execution.async_start()
```

启用后，AgentTask 才会在每次已完成迭代后持久化紧凑恢复快照。若进程崩溃，可以恢复成
新的 `AgentExecution` 并从下一次迭代继续。已完成的迭代不会重复执行：

```python
execution = await agent.async_resume("issue-123")   # 或 agent.resume("issue-123")
result = await execution.async_start()              # 从第 N+1 次迭代继续
meta = await execution.async_get_meta()
```

恢复会从 RecordStore 读取该任务最新快照，还原迭代历史与累计的 required 能力进度；若不存在
可恢复快照则抛出 `ValueError`。崩溃时正在执行中的那次迭代会被重新规划，因此非
replay-safe 的 step 副作用由宿主负责。当 result 带有可恢复的 `task_refs` 时，
`AgentExecutionResult.resume()` 会委托同一个 Agent resume facade；否则返回不支持恢复的
响应。`resume_task(...)` 只保留为 `resume(...)` 的兼容别名。

### 路由用户的“继续”请求

应用 Interface 不应把“继续”这个词直接当成确定性的恢复命令。“恢复此任务”按钮等
结构化 UI 操作可以在 host 校验任务引用、权限、快照兼容性和当前运行状态后直接路由。
回复到某个任务卡片只能确定候选任务，其中的自由文本仍然属于语义意图判断。

处理自由文本时，host 应先只投影当前用户有权访问的可恢复候选，为每个候选提供一个短
selection key 和有界任务事实。使用一次结构化 ModelRequest 在
`resume_existing`、`start_new`、`clarify` 中选择，再由 host 校验返回 key 和生命周期事实：

```python
from typing import Literal

from pydantic import BaseModel


class ContinuationRoute(BaseModel):
    decision: Literal["resume_existing", "start_new", "clarify"]
    selection_key: str | None = None
    clarification_question: str | None = None


route = await (
    agent.create_request()
    .input(
        {
            "user_message": user_message,
            "resumable_candidates": [
                {
                    "selection_key": "c1",
                    "goal_summary": "完成供应商风险报告。",
                    "last_progress": "资料收集已完成，报告撰写尚未完成。",
                }
            ],
        }
    )
    .info(
        {
            "routing_contract": {
                "resume_existing": "继续同一个未完成目标和成功标准。",
                "start_new": "启动后续工作，或改变目标、成功标准或交付物。",
                "clarify": "目标任务或生命周期操作仍不明确。",
            }
        }
    )
    .instruct(
        "存在 selection_key 时只能返回已提供的 key；不要创建或复述 task id。"
    )
    .output(ContinuationRoute, format="json")
    .get_response()
    .async_get_data_object()
)
```

模型负责判断用户文本与候选任务之间的语义关系。host 负责候选发现、权限、offered-key
成员关系、快照/版本校验、canonical `task_id` 重建，以及实际调用
`agent.async_resume(...)` 或 `agent.create_task(...)`。

只有当选中任务尚未终止、当前没有继续运行、存在兼容快照，并且用户仍在继续同一目标和成功
标准时才恢复。任务已经完成、目标或输出契约发生变化，或用户要求新的后续工作时，应创建新的
task；host 可以把旧结果或证据绑定进新 TaskContext，但不能把它描述成恢复。候选或意图仍然
不明确时，应向用户澄清，不能退化成关键词路由，也不能静默启动重复任务。

当示例需要验证模型生成内容的语义质量时，应组合 deterministic smoke check 和第二个
Agently model-judge request。文件存在、问题数量、source label 可见等结构检查只能作为
smoke gate；语义验收应使用带每条规则 evidence 和 boolean 结果的 judge schema。

示例里的业务系统 fixture 可以 mock，但只能返回业务事实、记录、政策或有缺陷/不完整的
source data。不要让 mock 返回 pass/fail、隐藏标准答案或本地质量 verdict。若场景需要判断
artifact 是否正确处理了缺陷数据或冲突事实，应由 AgentTask verifier 或独立 Agently
model-judge request 基于明确规则和证据做判断。

## Execution 对象

当调用方需要路线诊断、多种结果视图或过程流式输出时，使用
`agent.create_execution()`：

```python
execution = agent.create_execution()
execution.input("Summarize the reviewed DAG snapshot for the operator.")
execution.info({"dag_snapshot": snapshot})

async for item in execution.get_async_generator(type="instant"):
    if item.is_complete:
        print(item.path, item.value)

data = await execution.async_get_data()
meta = await execution.async_get_meta()
```

execution 对象沿用模型 response 的消费风格：`get_data`、`get_full_data`、
`get_text`、`get_meta`、`get_generator` 以及对应 async 方法。
默认 stream 是 `type="delta"`，产出纯文本字符串；模型流式请求重放时会产出保留的
`"<$retry>{reason}</$retry>"` 边界标记。该 marker 只服务 public 文本 replay consumer；
内部 artifact writer 和结构化 UI 应优先消费结构化 status 事件；只有在明确选择纯文本
消费边界时才处理该 marker。
需要结构化执行事件时使用
`type="instant"`：`AgentExecutionStreamData` 保留熟悉的 `path`、`value`、
`delta`、`is_complete` 字段，并增加过程级事件需要的 route metadata。对于同时需要统一
文本槽和结构化状态更新的 UI，`instant` 会在可投影成文本的来源事件之后追加 synthetic
`path="$delta"` text-projection item；heartbeat 保持 structured-only，不追加 `$delta`；
`all` 不包含这些派生 item。

`create_execution()` 创建一个 AgentExecution draft。只有 prompt 的 draft 会作为
直接模型请求执行。DynamicTask/TaskDAG workflow 先通过
`Agently.create_dynamic_task(...)` 或 `TaskDAGExecutor(...)` 运行，再把 snapshot
作为 evidence 传给后续 AgentExecution。当开发者自己编写循环，或 task strategy
需要一个有边界的单步 AgentExecution 时，用 `lineage` 和 `limits` 表达边界：

```python
execution = agent.input("Try one bounded fix step.").create_execution(
    lineage={
        "task_id": "issue-123",
        "iteration_id": "iter-2",
        "step_id": "execute-fix",
        "parent_execution_id": "exec-prev",
    },
    limits={
        "max_model_requests": 3,
        "max_seconds": 180,
        "max_no_progress_seconds": 60,
    },
)
```

这仍然只是一次 AgentExecution，不是多轮循环本身。`lineage` 提供稳定关联，
`limits` 提供跨普通模型请求和 AgentTask 请求共享的模型请求预算计数。
嵌套 AgentExecution context 会原子地消耗同一份祖先预算；创建 child 不会重置
root allowance。child 可以额外为自己的 subtree 设置更小的本地 allowance，但不会
降低 sibling 的本地 allowance。
无限预算用 `None` 表达。

如果有边界的 execution 超出模型请求预算，Agently 会抛出
`AgentExecutionLimitExceeded`，可以从 `agently.core` 根导出或
`agently.core.application.AgentExecution` 引入。execution meta 仍然可以检查，
并会记录 `status="blocked"`，以及 `diagnostics` 里的 limit event。

对于卡住的执行，`limits.max_seconds` 是整个 AgentExecution 的硬截止时间。在
Goal Pursuit / task strategy 运行中，这个 wall-clock budget 由 AgentTask
拥有，并返回带 task metadata 的 `timed_out` 任务结果；其它 route 会把硬截止
时间暴露为 `RuntimeStageStallError`，可以从 `agently.core` 根导出或
`agently.core.application.AgentExecution` 引入。`limits.max_no_progress_seconds`
是 idle stall 边界：route selection、模型流、Context 读取、ActionRuntime
任何被接受的运行进展都会刷新计时。`async_get_meta()` 仍然可检查，并记录
`status="timed_out"` 或 `status="stalled"`，以及 `diagnostics["timeouts"]` /
`diagnostics["stalls"]` 和最后一次进展事件。

Provider 与 response materialization 等待有独立配置：

```python
Agently.set_settings("OpenAICompatible.stream_idle_timeout", 60.0)
Agently.set_settings("OpenAIResponsesCompatible.stream_idle_timeout", 60.0)
Agently.set_settings("response.materialization_idle_timeout", 60.0)
```

`stream_idle_timeout` 限制携带有效响应数据的 provider stream item 之间的空闲间隔。
空 SSE keep-alive 帧不会启动或刷新响应活跃期限；首事件和 stream idle deadline
也不会等待迟缓的 transport 取消清理完成。两者都会抛出 `RuntimeStageStallError`，在 requester
能够识别时带上 provider/model 字段。该 timeout 归属于 ModelRequest attempt 生命周期：
先消耗 `request_retry.max_attempts`，次数耗尽后再 fail closed。每个失败的
`model.status` payload 都会保留 attempt index、retry 决策、typed stall diagnostic、
有效响应进展口径和异步清理事实，供审计使用。
每个 `model.requesting` payload 还会投影实际生效的非敏感 liveness policy：
`timeout_mode`、类型化 HTTP timeout、`stream_idle_timeout` 和 `request_retry`。这样可以
区分“retry 机制没有执行”和“请求从未产生可供 retry 消费的 provider failure”。
`response.materialization_idle_timeout` 限制最终 text、data、object 或 meta 从
response parser materialize 出来的等待时间。`None` 表示无限制；`-1` 作为兼容写法可用。
如果 provider 或最终响应构造在 materialization 完成前发出显式 stream error，
`get_text()` / `get_data()` / `get_meta()` 会传播该原始错误，而不是继续等到
materialization timeout。

高频 RuntimeEvent 出口应该通过 Event Center 请求摘要投递，而不是让
AgentExecution 在信号源降频：

```python
Agently.event_center.register_hook(
    handler,
    event_types="model.response.delta",
    hook_name="app.delta_summary",
    delivery_policy={"mode": "summary", "emit_interval": 0.1, "max_items": 20},
)
```

AgentExecution stream API 保持 raw。某个 hook 主动选择 summary delivery 时，
Event Center 摘要事件会包含 `meta["coalesced"]`、`coalesced_count` 和源事件 id。

`async_get_meta()` 会包含 `lineage`、`limits`、
`route`、`route_plan`、`logs`、`diagnostics` 和 `task_workspace_refs`。`logs` 是跨
route 稳定检查运行事实的位置，例如模型响应 id、
ActionRuntime action records 和 artifact refs：

```python
meta = await execution.async_get_meta()
meta["route"]["selected_route"]
meta["logs"]["model_response_ids"]
meta["logs"]["action_logs"]
meta["logs"]["artifact_refs"]
```

当 `model_request` route 使用 Actions 时，execution 会通过 meta 和
`actions.<action_id>` 这类 stream event 暴露 action records。需要持久化业务证据时，
host 应读取框架 action record 或 artifact，再显式写入 RecordStore；不要为了让 host
能存储结果而要求模型把 raw action stdout 再复制一遍。

每条过程流 item 也会带上关联 metadata：

```python
item.meta["execution_id"]
item.meta["lineage"]["task_id"]
```

默认 Agent 带有相互独立的 lazy TaskWorkspace 与 RecordStore binding。
`agent.use_task_workspace(...)` 选择文件 root，`agent.use_record_store(...)`
选择持久化。AgentExecution 不会自动决定什么进入记忆；调用方应显式持久化：

```python
record_ref = await execution.record_store.put(
    collection="observations",
    kind="agent_execution_observation",
    content={"result": data},
)
checkpoint_ref = await execution.record_store.checkpoint(
    execution.lineage["task_id"],
    {"result": data, "record_ref": record_ref},
)
```

这会通过 execution-scoped RecordStore view 写入。下一步可以把
`RecordStoreContextSource` 挂进 TaskContext，或直接调用
`execution.async_read_task_context(...)` 做 bounded read。TaskWorkspace 继续拥有
文件制品及其 readback refs。

开发排障时，可以挂 EventCenter observation hook，或临时打开控制台明细：

```python
agent.set_settings("debug", "detail")

task = agent.create_task(
    goal="准备报告。",
    success_criteria=["报告内容完整并已核验。"],
    execution="flat",
)
await task.async_streaming_print()
result = await task.async_get_full_data()
```

`debug=True`（即 `simple` profile）打印可读 Prompt、精简请求/结果和关键过程；
`debug="detail"` 增加脱敏 provider 请求 JSON、attempt/validation/telemetry、Action 与
路由/阶段明细，但仍会筛选兼容别名、传输镜像和重复进展。控制台不是完整 RuntimeEvent
流，也不会替代业务输出：要查看可读的任务阶段和最终结果，仍需消费 `type="delta"` 或
调用 `async_streaming_print()`；要完整审计、存储或重放则使用 EventCenter hook 或
DevTools。问题定位后，应从示例和生产代码中移除 debug settings。
并发模型响应仍然并发执行：ConsoleSink 只把最先产生 delta 的响应设为前台展示，将后到
响应的展示按 FIFO 缓冲，并在前台终止后提升；这只影响展示，绝不阻塞或重排底层执行。
前台流存在时，普通 Prompt/request/process/成功状态会有界延后到全部 FIFO 响应展示完成
之后，正文间只插入一条精简后台提示。需要处理的 warning、failure、cancellation、interrupt
与 approval 仍即时出现，EventCenter 与 DevTools 的事件时序不变。
simple 模式会为每个成功模型响应保留一个完整投影：要么是完整展示的实时流，要么是完整
终态结果。后台重放 buffer 若已满，控制台会在完成时用完整权威结果替代残缺重放，不会把
部分正文当成全部输出。

## 提交式 DAG 输入

通过独立 DynamicTask facade 运行的提交式 DAG，其 task `inputs` 继续使用 DAG 运行时
占位符，例如 `${INIT.ticket}` 和 `${DEPS.lookup}`。graph input 按以下顺序解析：

```text
async_run(graph_input=...)
> {"target": task_target}
```

这让 DAG 输入显式独立于 AgentExecution prompt 路由：

```python
task = Agently.create_dynamic_task(
    target="review ticket",
    plan=graph,
    handlers=handlers,
)
snapshot = await task.async_run(graph_input={"ticket": "TICKET-OK"})
```

如果 AgentExecution 需要使用结果，把 snapshot 作为普通 evidence 传入
`input(...)`、`info(...)` 或 RecordStore record。DAG snapshot 本身不等于更大业务目标
已经完成。

## Skills 语义

`agent.use_skills(...)` 和 `agent.use_skills_packs(...)` 在 AgentExecution 上登记
binding intent，并与 `use_actions(...)` 使用同一种组合表达；没有另一套公开的
Skill 集合 API。每个 execution 只把这些声明解析成自己的精确 revision 范围，不扫描
全局 SkillLibrary。`mode="model_decision"` 用结构化语义 selector 从本次 execution
范围中选择；`mode="required"` 以 fail-closed 方式绑定 SKILL.md guidance。普通
`model_request` 或显式 AgentTask strategy 再通过 TaskContext 消费这些 guidance。

`run_skills_task(...)` 只是同一 AgentExecution path 的已发布 result-shaped
adapter。SkillLibrary 只安装宿主已物化的本地 source；远程下载属于有权限的宿主
代码，必须在安装前完成。

## 过程流

Agent execution stream item 保留熟悉的 instant stream 形态：

```python
item.path
item.value
item.delta
item.event_type
item.is_complete
item.route
item.stage_id
item.task_id
item.action_id
item.graph_id
```

Executor route 会桥接 route 与 ModelRequest instant checkpoint，让服务能流式输出
route decision、task/action 进度、选定模型字段 delta 和最终 semantic outputs。
如果 TriggerFlow-backed route 失败，Agent execution stream 会关闭，并把原始
错误抛给消费者，而不是让 `get_async_generator(...)` 一直等待后续 item。

独立 TaskDAG model 节点如果需要字段级展示，应消费 TriggerFlow runtime stream 并
归一化 `task_dag.model_field` item：

```python
task = Agently.create_dynamic_task(target="reply", plan=graph, handlers=handlers)
execution = task.compile(graph).create_execution(auto_close=False)
async for item in execution.get_async_runtime_stream({"ticket": ticket}, timeout=None):
    if item.get("type") == "task_dag.model_field" and item.get("field_path") == "reply":
        print(item.get("delta") or "", end="", flush=True)
```

这样可以把 AgentExecution stream 语义和独立 DAG runtime stream 语义分开。


## 统一执行控制

`run()` / `async_run()` 与结果 reader 共享同一个自有运行任务，并发读取不会
重复生产。启动前可查看 `execution.control_capabilities` 确认支持边界。

- `pause()` / `async_pause()` 请求在生产前，或候选产生后、最终策略前暂停。
  requested 不等于 paused；在途 provider 先结算。实际暂停时 run 和 reader
  抛出 `AgentExecutionPaused`，不会伪造终态结果。
- `resume()` / `async_resume()` 显式继续原 TriggerFlow；普通读取不会自动恢复，
  候选暂停后的恢复不会重复生产。已完成的结果不能借 resume 再次生产。
- `interrupt(content, author=None)` 通过 TaskContext 补充后续请求的信息；回执
  区分插入、请求消费与忽略，不会修改已分发请求。候选已经产生时明确忽略。
- `cancel(reason=..., timeout=...)` 取消并等待自有工作清理。超时仍未结算，
  重复调用继续等待同一清理过程；取消不回滚外部副作用。
- `close(reason=..., timeout=..., pending="error")` 默认排空并关闭；未解决的
  等待会报错，只有显式 `pending="cancel"` 才放弃。关闭 draft 禁止启动，
  关闭已完成结果仍保留 reader。所有控制方法均提供 async 对应形式。

在已经结算的安全暂停处调用 `snapshot = execution.save()`；先配置新的执行
对象，再 `restored.load(snapshot)`，最后显式 `await restored.async_resume()`。
load 本身不调用模型或 Action。恢复新对象前先取消旧暂停对象，避免保留两份可继续的句柄。

新对象须重新绑定同一原始 draft、预算、策略 callback、Action/Skill、Workspace、
RecordStore 及外部 ContextSource。快照只有 JSON 数据与资源身份，没有 settings、
密钥、客户端或可执行 callback；缺失或变更的绑定明确失败。目前保守拒绝任何
Skill 目录变化。累计模型调用数与 elapsed time 跨 load 保留，停机时间也计入。

恢复后首次 `get_data_object()` 使用重绑的原输出 schema 在本地校验并重建对象，
之后复用缓存；支持 Pydantic 嵌套/根类型及 `LongContent` 声明。load 不运行
输出模型校验器，typed reader 不重新调用模型或最终 validate/artifact/review。
历史 revision 从各自候选重建，不能读到新版本的类型化缓存；本地重建失败会抛错。

### 返工与历史版本

```python
execution = agent.create_execution("request", limits={"max_model_requests": 3}).input(
    "总结：staging 已通过；production 尚待批准。"
)
previous = execution.get_result()  # 固定读取 revision 0。
first = await execution.async_run()
revised = await execution.async_rework("先说明待批准事项。", max_reworks=2)
assert execution.revision == 1  # 同一个对象、同一个 execution ID。
assert await previous.async_get_full_data() == first
assert await execution.get_result(revision=0).async_get_full_data() == first
```

`rework` 返回新候选的完整结果，已有 reader 保留原版本的数据、meta 和 stream；
新 reader 默认读取当前 revision。原始任务和验收标准与本次反馈一起交给生产者。
Request 返工保留原始 `info` 和 `instruct`；当前反馈与上一候选单独追加，连续返工不累积过时反馈。
Request 修改上一候选；Plan 保留已接受的澄清；LongContent 复用未变的前缀，
重写受影响章节及后续章节。LongTask 由模型选择需失效的工作，Host 校验 ID
并失效依赖：Flat 失效后续串行工作，TaskBoard 保留无关卡片并核验复用文件内容。
也可以只选择最终 candidate 返工，保留全部已完成工作，重新生产交付结果。

模型调用数、耗时、Flat 迭代数和 TaskBoard tick 累计。用
`create_execution("long_task", limits=...)` 声明总模型预算；兼容
`create_task(limits=...)` 的模型次数仍保持原有单步含义，但墙钟跨返工累计。
`max_reworks` 建立的次数
上限不能被后续调用提高。失败的 revision 不会变成成功，旧候选仍可读取。
已分发的 Action（包括副作用结果不确定的失败）再次执行，需要 `replay_safe`
声明或 Host 显式传入 `allow_replay=True`；子执行不能放宽父执行限制。
Artifact callback 同样需要显式允许重放。副作用不会回滚，历史文件引用不等于
文件备份；自定义生产者须声明自己的安全返工契约。

安全暂停快照包含版本历史、生产者状态和重放保护。使用 `create_task` 恢复时
须显式重新绑定原 `task_id`；已结算任务资源通过原有 task recovery 契约恢复，
不能恢复的 ContextSource 明确失败。快照恢复历史数据、meta、stream 记录，
不恢复 provider 活对象或原 Python 异常类。仍存活且没有 checkpoint 契约的
ExecutionResource 会阻止保存，须先结算或释放；终态清理释放当前执行自有的 execution scope。

当前仅支持外层执行安全边界；在途 provider、活跃子执行、离线 Plan 澄清和
嵌套父预算恢复仍不支持，不能用旧 AgentTask resume 能力代替。真实模型快照
交接与同一 execution 返工示例见 `examples/agent_auto_orchestration/29_execution_controls_ollama.py`。
