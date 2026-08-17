---
title: Prompt 管理
description: 分层 prompt 槽位、agent 与 request 作用域、YAML/JSON 加载与占位符。
keywords: Agently, prompt, role, system, info, instruct, input, configure_prompt
---

# Prompt 管理

> 语言：[English](../../en/requests/prompt-management.md) · **中文**

Agently 把 prompt 拆成命名槽位。槽位可组合，所以 agent 级持久内容只设一次，请求级槽位每次按需填。

## 槽位映射

| 槽位 | 落在哪 | 典型用途 |
|---|---|---|
| `role` / `system` | system 消息 | 角色、能力边界 |
| `info` | system 或 user（实现细节） | 背景事实、目录、工具清单 |
| `instruct` | user 消息 | 这类请求的步骤指令 |
| `input` | user 消息 | 实际问题或 payload |
| `output` | user 消息 + parser | 期望的返回结构 |

## 单次请求契约就近聚合

一次性的 Agently fluent request 应保持为一条可直接阅读的请求链：`input`、权威
`info`、`instruct`、`output` schema，以及 `get_result()`、`get_data()`、
`async_get_data()` 等终结结果调用都应在这条链上清晰可见：

```python
result = (
    agent
    .input({"ticket_text": ticket_text})
    .info({"allowed_queues": allowed_queues})
    .instruct("选择最合适的队列，并给出一条简洁说明。")
    .output({
        "queue": (str, "allowed_queues 中的一个值。", True),
        "explanation": (str, "用户可见的简洁说明。", True),
    })
    .get_result()
)
triage = await result.async_get_data()
```

一份 YAML/JSON Prompt Configure 文件配合显式 `mappings` 是等价的声明式表达。只有
schema 或 prompt 片段会被原样复用、由另一 owner 独立版本化或产品编辑，或者确实是
动态生成、条件组装时，才拆开这条请求链。不要仅仅为了让 Agently 请求链看起来更短，
就把只使用一次的 schema 或 prompt 步骤搬到别处。

## 请求本地上下文

每个 prompt 槽位只有在改变当前请求所要求、允许、禁止，或需要产出的内容时，才属于
请求本地上下文。应有意识地使用五种角色：`agent` 提供稳定的角色与能力；`input` 提供
当前事实；`info` 提供权威契约与证据；`instruct` 提供任务规则；`output` 提供所需结果
结构。它们合在一起必须让模型获得对当前请求自包含的说明，而不是假定模型知道未解释的
项目上下文。

对每个候选内容使用移除反事实：若移除它不会改变当前请求的有效任务、契约、证据或
决策，就应移除或改写它。内容来自项目级并不是移除的理由：只要共享策略或事实会改变
本次请求，就应保留。专有名称只有在标识会改变本次请求的真实领域契约、allowlist、
证据项、输入事实或能力边界时才可保留。否则，应把未解释的实现名称改写为它与请求相关
的角色，或直接移除。

| | `info` |
|---|---|
| 不好 | “遵循项目的 worker-manager 约定。” |
| 好 | “允许的操作：批准或拒绝。证据：附带的请求及其策略记录。” |

分两层审计：先按槽位角色和移除反事实检查每个槽位，再检查实际渲染的请求，包括
mappings 和引用。发送前，`execution.get_prompt_text()` 会渲染当前 execution draft，
以便确认上下文自包含且属于请求本地。execution 启动后，同一方法会优先返回为本次运行
捕获的 prompt snapshot。

## 严格的外部接口契约

当模型输出会直接作为已定义 API 请求、模块接口或函数调用的参数时，模型必须
看到该接口契约。Python signature、OpenAPI operation、JSON Schema、protobuf
定义或权威 docstring 不会自动出现在普通模型请求中。

把各槽位组合成一份集成契约：

| 槽位 | 集成职责 |
|---|---|
| `input` | 本次请求的动态值与源事实。 |
| `info` | 权威 API/schema 文档、signature、docstring、字段语义与已声明约束。 |
| `instruct` | 输入如何转换、目标 callable/operation，以及缺失信息如何处理。 |
| `output` | 下游接口要求的精确机器可消费类型与嵌套结构。 |

每个会被下游消费的输出字段都应说明含义，并声明类型、必填性，以及适用的
枚举、格式、范围、可空性或跨字段约束。复用这些权威接口事实属于必要的边界
与输出控制，不是业务逻辑侵入。不属于接口契约的业务决策仍由应用策略层拥有；
真实调用前，host 仍应执行确定性校验。

```python
from typing import Literal

ticket_body = await (
    agent
    .input({
        "request_text": request_text,
        "requester_id": requester_id,
    })
    .info({
        "target_operation": "POST /tickets",
        "operation_contract": openapi_ticket_operation,
    })
    .instruct([
        "根据输入事实生成一份 POST /tickets 请求 body。",
        "严格遵守目标 operation 契约，不要增加字段。",
    ])
    .output({
        "title": (
            str,
            "POST /tickets 接受的非空工单标题。",
            "not_null",
        ),
        "priority": (
            Literal["low", "normal", "high"],
            "必填 API 枚举：low、normal 或 high。",
            True,
        ),
        "requester_id": (
            str,
            "从 input 原样复制的必填请求人标识。",
            "not_null",
        ),
    }, format="json")
    .async_start()
)
```

agent 级持久设置：

```python
agent = (
    Agently.create_agent()
    .role("你是 Agently 客服助手。", always=True)
    .info({"product": "Agently 4.x"}, always=True)
)
```

`always=True` 表示该槽位停留在 agent 级，每次请求都带。

请求级单次设置：

```python
result = (
    agent
    .instruct(["回复不超过 80 字。", "不要编造产品名。"])
    .input("怎么配置一个模型？")
    .output({"answer": (str, "answer", True)})
    .start()
)
```

这里 `instruct(...)` 没传 `always=True`，所以仅本次请求生效。

## Agent vs Execution 作用域

| 作用域 | API |
|---|---|
| Agent definition（每次 execution 都生效） | `.define(...)`、`.role(..., always=True)`、`.info(..., always=True)`、`.set_agent_prompt(key, value)` |
| AgentExecution draft（仅一次 execution 生效） | `.input(...)`、`.output(...)`、`.set_execution_prompt(key, value)` |

同一作用域内最后一次设置覆盖前面，所以可以在单个 execution 里覆盖 agent 默认值，而不修改 agent。

## YAML / JSON prompt 文件

同一套槽位模型，声明式写法：

```yaml
# prompts/triage.yaml
$ensure_all_keys: true
.agent:
  system: 你是一个工单分流助手。
  info:
    severities: ["P0", "P1", "P2", "P3"]
.execution:
  instruct: 对工单文本分类。
  output:
    $format: json
    severity:
      $type: str
      $desc: P0/P1/P2/P3 之一
      $ensure: true
    rationale:
      $type: str
      $desc: 一行说明原因
      $ensure: true
```

加载：

```python
agent = Agently.create_agent().load_yaml_prompt("prompts/triage.yaml")

result = (
    agent
    .create_execution()
    .set_execution_prompt("input", "EU 区域所有用户登录失败。")
    .start()
)
```

`load_json_prompt(...)` 是 JSON 版本的同一 API。两者都接受路径或原始字符串。可以一份配置一个 prompt，也可以用 `prompt_key_path="demo.output_control"` 在多 prompt 文件里挑一个。

Prompt 配置使用 `.execution` 表示单次 execution。turn/request-scoped prompt
config alias 已移除；旧 prompt 文件应改成 `.execution`。

顶层 `$ensure_all_keys: true` 会强制所有叶子都必填，覆盖每叶子的 `$ensure`。整个 schema 必须完整返回时使用。

`output` 块里的 `$format` 会映射到 `.output(..., format=...)` 同一个输出格式设置。
支持 `auto`、`json`、`flat_markdown`、`hybrid`、`xml_field`、`yaml_literal`。如果配置文件需要更明确的 key，
也可以写 `.format`、`$output_format` 或 `.output_format`。

## 往返转换

可以把代码里组装的 prompt 转成 YAML/JSON 用于 review 或存储：

```python
execution = agent.role("你是 Agently 助手。", always=True).input("打个招呼。").output({
    "reply": (str, "reply", True),
})
print(execution.get_yaml_prompt())
print(execution.get_json_prompt())
print(execution.get_prompt_text())  # 模型实际看到的渲染文本
```

这种往返是把「我以为我在发」与「框架实际发的」对上的标准方式。

## 占位符

prompt 槽位中：`{name}` 引用另一个槽位的 key；`${name}` 在加载时由 `mappings={"name": "value"}` 替换。常见用法：

- `instruct: "Reply {input} politely."` — 把请求的 `input` 拉进 instruct。
- `${ENV.OPENAI_API_KEY}` 是**设置**层的环境变量替换，不是 prompt 的；prompt 用 `${name}` + 显式 mappings。
- `${INPUT.customer}`、`${INFO.policy}`、`${INSTRUCT.step}` 是渲染时的 slot
  引用，会变成 `[INPUT > customer]` 这类 prompt 段落指针，而不是把另一个
  slot 的值复制进来。Slot 名大小写不敏感，文档推荐大写。Slot 后面的 path
  不做存在性校验，因为它只是给模型看的引用标签。
- `${OUTPUT}` 是 `[OUTPUT REQUIREMENT]` 的别名。

加载时触发 `${...}` 替换：

```python
agent.load_yaml_prompt(yaml_text, mappings={"product_name": "Agently"})
```

## 每层 prompt 的来源

请求实际发出时，Agently 按以下顺序合并 prompt：

1. Agent 级槽位（`always=True` 或 `set_agent_prompt`）
2. Request 级槽位（不带 `always=True`）
3. 框架扩展或应用代码填入的槽位（Session 注入 chat history；检索代码通常把片段放进本次请求的 `info(...)`）

一次性链式调用后，用 `execution.get_prompt_text()` 看发送前的合并结果，例如
`execution = agent.input(...).output(...)`。`agent.get_prompt_text()` 只查看保留在
Agent 自身上的 prompt，例如通过 `always=True` 设置的持久槽位。

## 另见

- [Schema as Prompt](schema-as-prompt.md) — 叶子 authoring 与 `$ensure`
- [输出控制](output-control.md) — 解析之后的事
- [项目结构](../start/project-framework.md) — 多 prompt 项目的目录布局
