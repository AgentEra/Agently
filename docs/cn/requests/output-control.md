---
title: 输出控制
description: 输出校验流水线 —— strict output、ensure_keys、custom validate、retry 与事件。
keywords: Agently, output, validate, ensure_keys, retry, max_retries
---

# 输出控制

> 语言：[English](../../en/requests/output-control.md) · **中文**

第一次消费结构化 response 结果时，校验流水线会运行并缓存结果。它的执行顺序固定，每一步都共用同一份 retry 预算。

对 Agently `4.1.0.1+`，默认 authoring 路径是：在 `.output(...)` 里直接用第三槽 `ensure` 标记固定必填叶子，再由运行时把这些标记编译成 `ensure_keys`。只有当必填路径是运行时决定、条件分支决定，或用静态 schema 不好表达时，才手动传 `ensure_keys=`。默认情况下，第三槽 `True` 和手动 `ensure_keys` 只检查路径/key 是否出现；值可以是 `None`、空白字符串、`False`、`0`、空列表，或其他业务上合法的空值。若某个必填路径还必须包含可用值，显式写第三槽 `"not_null"`；它会拒绝 `None`、空白字符串、空列表或空 wildcard 匹配，以及列表中包含缺失的必填值，同时仍接受 `False` 和 `0`。

tuple ensure 策略与受支持的 Pydantic 字段约束会进入首次结构化输出提示词，
不会只留在响应后的校验阶段。直接声明的 Pydantic 输出模型同时仍是 typed
接受权威：解析出的 dict 如果未通过模型校验，会使用同一份 retry 预算修正，
不会被当作成功业务数据返回。

## 直接下游接口契约

如果下游代码会把解析结果传给 API、SDK、模块接口或函数，`.output(...)`
应当对应真实消费的请求或参数结构，而不是返回 `{"args": (dict,
"arguments", True)}` 这类不透明 `dict`。每个被消费的叶子都要写明契约语义，
不能只有泛化标签；应包含精确类型、必填性，以及适用的枚举、序列化格式、
范围、单位、可空性或字段依赖。

完整集成契约还包括：在 `info(...)` 中提供权威 API 文档、signature、schema
或 docstring，在 `input(...)` 中提供运行时事实，在 `instruct(...)` 中提供转换
与调用规则。这是必要的输出控制，不是业务逻辑侵入。解析与 `ensure` 检查
不能替代真实调用或副作用前的确定性 DTO/Pydantic/SDK 校验。

## 规则先行的业务校验

当模型需要满足生成后的业务 validator 时，所有非敏感、且模型能够遵守的接受规则，
都必须在首次生成前提供给模型：运行时候选项与限制放在 `input(...)`，权威政策或接口
材料放在 `info(...)`，行为与转换规则放在 `instruct(...)`，字段类型、必填性、枚举、
格式、范围、可空性和跨字段约束放在 `output(...)`。

宿主侧 validator 仍是最终权威。Pydantic model、`.validate(...)`、DTO/SDK 校验、
授权检查与副作用 guard 仍须确定性拒绝非法输出。重试反馈只能修复已经声明的契约，
不能成为模型第一次得知规则的位置。用信息不足的首次生成接硬拒绝，再通过反复重试
碰撞规则，是“盲目发现卡控规则”，不是可靠的校验策略。

安全、授权、反滥用、完整性与 holdout 卡控可以在披露会削弱卡控或泄露预期答案时，
把敏感实现细节保留在宿主侧。此时应向模型提供安全的公开契约，并选择 fail closed、
不重试、人工审核或明确 fallback；不要通过 correction prompt 逐次泄露隐藏规则。

如果某个生产卡控既无法安全披露，也无法具体表达，但开发者仍要求强制执行，
Coding Agent 必须在实现前停止并说明：

- 缺失或刻意隐藏的是哪条规则，以及它影响哪个输出；
- 预期的重试、成本、延迟、非确定性与活性风险；
- 更安全的替代方案，以及拟采用的重试和终止行为。

只有开发者在新的回复中明确确认这个具名卡控及其风险后，才可以继续实现。之前的
笼统“继续执行”不构成这次二次确认。

## 选择输出格式

`.output(...)` 省略 `format` 时读取 `prompt.default_output_format`，全局默认值是
`json`。agent 级和 request 级 settings 可以独立覆盖这个默认值。只有目标模型通过
代表性结构化输出稳定性测试后，才建议把 `prompt.default_output_format` 设为
`"auto"`。

显式 `format="auto"` 时，Auto 会根据 schema 形态选择结构化格式：扁平纯字符串
dict 走 `xml_field`；字符串字段与 typed 非字符串字段混合的 dict 走 `hybrid`；
全复杂、全控制字段或非 dict 输出仍走 `json`。Auto 不检查字段名或描述里的业务含义。
如果下游代码依赖固定的原始输出形态，应显式指定格式。`yaml_literal` 是显式
opt-in 格式，不进入 auto；`flat_markdown` 仅作为显式兼容模式保留。

| 模式 | 适用场景 | 不适合 |
|---|---|---|
| `auto` | 明确接受 schema-driven 格式选择和重试延迟，并且目标模型已经通过稳定性测试。适合应用代码通过 Agently 消费解析后的数据，而不是依赖模型原始文本。 | 需要保守框架默认值，或旧消费者、测试 fixture、外部 API、保存的 prompt 期待原始 JSON 文本。此时显式用 `format="json"` 或保持默认 `json`。 |
| `flat_markdown` | 兼容旧 section-header prompt 的显式模式。 | auto 选择、嵌套 list/object、记录数组，或需要高可靠解析。 |
| `hybrid` | 显式格式，或 auto 目标；适合字符串 prose/code 字段与 typed 字段混合。字符串字段保持 Markdown 章节，list/object/boolean/number 字段放 fenced JSON block。 | 没有字符串 prose/code 字段、所有字段都是紧凑机器数据且 JSON 更直接、目标模型容易回显脚手架，或下游不能接受 Markdown-section raw output。 |
| `xml_field` | 显式格式，或 auto 目标；适合扁平纯字符串 dict。Agently 用自定义 XML-like parser 解析，不是严格 XML parser；text 字段可包含 Markdown、代码、`&` 或类似 XML 的片段。 | 下游消费者期待真实 XML 语义、namespace、entity escaping 或 XML schema validation。 |
| `yaml_literal` | 团队明确偏好 YAML document，且可接受 YAML 缩进敏感性时显式使用。长文本/代码字段用 YAML literal scalar（`|`），整体包在 `<<<BEGIN AGENTLY_YAML>>>` / `<<<END AGENTLY_YAML>>>` boundary 中。 | 通用 auto、低遵循模型，或 JSON 更简单稳定的 dense machine contract。 |
| `json` | 需要最稳定的机器契约、嵌套数据、数组、外部系统互通、兼容旧 prompt/测试，或下游明确依赖原始 JSON 行为。 | 大段嵌入文档或代码会让转义变脆弱，也更难让模型稳定生成。 |
| 纯文本 | 请求只要一个自由文本成品：文章、邮件、解释、报告、Markdown 页面、HTML 页面，或其他单一多段落文档。不要调用 `output()`；直接用 `start()` / `async_start()`，或读取 `result.get_text()`。 | 需要可单独寻址的字段、路径校验、`ensure_keys`、typed object 或下游分支。 |

### 可能超过单次模型输出窗口的结果

当一个业务结果可能超过 provider 的单次输出窗口时，在尚未启动的
`AgentExecution` 上使用 `.ensure_long_output()`：

```python
netlist = (
    agent
    .input({"requirements": requirements})
    .info({"component_rules": component_rules})
    .instruct("生成完整网表。")
    .output(
        {
            "components": [
                {
                    "refdes": (str, "唯一元件位号", True),
                    "value": (str, "元件参数", True),
                }
            ],
            "nets": [
                {
                    "name": (str, "网络名", True),
                    "connections": [(str, "refdes.pin", True)],
                }
            ],
        },
        format="json",
    )
    .ensure_long_output()
    .get_data()
)
```

该选项默认关闭；`.ensure_long_output(False)` 可在同一个未启动 draft 上关闭。
它约束的是整次 execution，而不是 `.output(...)` 或某个结果读取器，因此
`get_data()`、`get_text()`、`get_data_object()`、`get_result()` 和 generator
consumer 看到的是同一份冻结策略。execution 启动后再调用，会触发现有的一次运行
生命周期错误。

第一次模型请求保持原样。若归一化终态是 `stop`，继续使用普通单请求结果与校验
路径；若 provider 报告 `length` / `incomplete`，Agently 才启动一个
TriggerFlow 可见的续写循环。已接受单元会写入该 execution 的私有
TaskWorkspace，每次写入都完整读回并核对 SHA-256；最终 candidate 从 manifest
重放后，再执行原始 Pydantic/schema、ensure 与自定义 validator。每个结构化更新在
推进 manifest 前，还会单独通过所属 assembly slot 的 JSON Schema 校验。

当前可无损组装的 carrier 是纯文本和显式/解析后的 `json`。启用该选项时，
`flat_markdown`、`hybrid`、`xml_field`、`yaml_literal` 和不透明 custom carrier
会在模型 dispatch 前失败。结构化续写只保留 provider 文本中真实出现闭合定界符的
值；由不完整 JSON 修复生成的值带
`completion_source="synthetic_repair"`，必须重新生成，不能提交为已接受单元。

续写采用 append-only revision：

- 纯文本精确保留首次 prefix，再按顺序追加已闭合文本块，不做模糊 overlap 删除。
  每次逻辑 continuation 只提交一个文本块，使下一处拼接一定基于刷新后的已接受正文
  后缀生成；若响应给出多个文本 update，会保留第一个合法块并重新生成其余 tail；
- JSON list item 和声明值只在可信 completion boundary 且通过局部 slot schema
  校验后提交；
- 模型可见的 slot contract 从原始 Agently output 声明投影，保留嵌套
  array/object 形状以及 Pydantic 的长度、数值边界、倍数和 pattern 约束；独立生成的
  Pydantic slot model 仍是提交前的权威校验器，因此嵌套字段不合法的业务单元不会先
  进入 manifest、等到最终校验才被发现；
- 精确 Pydantic list 边界会在增量阶段执行：达到 `maxItems` 后不再提供该 slot，
  未完成的精确 list 会阻止后续依赖 slot 提前生成。结构化续写还会收到有界、只读的
  canonical accepted JSON 证据，用来保持已有语言、命名和跨字段事实；
- 真实闭合的空 list 会作为空容器 manifest 事实保留；缺失的 list path 不会被合成
  为空，已有 item 或已有空容器声明之后的重复声明也会被拒绝；
- 真实闭合的空字符串也会作为 text slot 的存在事实保留。在信任 continuation
  `is_final` 之前，Agently 要求每个已声明 ensure path 都已有 manifest 事实；缺失
  required path 会继续交付循环，不消耗调用方的最终 validation retry 额度；
- 真实闭合的结构化字符串是一个原子 schema value；提交后不会再提供给 continuation，
  也不能追加或改写。若一个结构化值超过 4000 字符单元上限，应在原 output contract
  中建模为有序 chunk list；若结果本身就是一个自由文本成品，则使用纯文本 carrier；
- 如果某段先产生合法连续前缀、随后出现坏更新，前缀继续保留，坏更新及其后续 tail
  不会被跳过提交，而是从下一个 `unit_index` 重新生成；
- continuation packet 会携带 slot value contract，并明确区分私有零基
  `unit_index` 与业务值内部可能存在的 `index` 字段；packet 大小也受到约束，以便在
  provider 窗口内形成可提交的闭合单元。每个 slot 只暴露一个需要模型原样返回的
  mnemonic `path_key`（例如 `p1:components`），不再同时暴露第二个可复制 schema
  path；宿主仍按完整 offered key 做授权，而不是解析后缀取得权限；
- 每个 continuation 都先输出并闭合最小控制头 `base_revision`、`base_digest` 和
  `anchor`，再开始业务 update。anchor 是最后一个已接受单元的短 digest；对纯文本，
  有界的文首样式片段、精确正文后缀和由宿主计数的已接受字符总数会另行作为只读
  continuity context 提供。这样既保留全局格式和局部拼接依据，也不要求模型把一大段
  业务正文逐字复制进控制头或自行估算已有长度。若 provider 在控制头闭合前就以
  `length` 终止，该次被记录为
  无进展，已接受 manifest 保持不变；下一次有界恢复请求会要求先闭合控制头，并且
  最多输出一个 update；
- 旧 revision、错误 digest、未知 path、非连续 unit index 和坏 update 都不会进入
  manifest；连续三次无进展会携带最后一个 reason code fail closed，而不是无限循环；
- continuation 修正由 `LongOutputDelivery` 独占，不再嵌套一层
  `ModelRequest` 重试。每个 continuation 对应一次真实请求；provider-complete
  响应若不满足私有 envelope，会先持久化并记为有界
  `continuation_envelope_invalid` 无进展。调用方的 `max_retries` 只留给最终组装值
  的 validation repair；
- provider 报告 `length` 后，零更新的 `is_final` 声明仍属于无进展，不能单独证明
  被截断的业务结果已经完整；
- 续写请求不继承 Action/tool handler，因此只输出的续写不会重复副作用；
- 私有 continuation envelope 不会进入公开文本流。

当大型 JSON 的 instant parser 已经延迟增量解析时，provider-complete 的权威 final
parse 仍是最终结果；observed-boundary 事件不能用旧 provisional snapshot 覆盖它。
只有 final JSON parse 失败或 provider 以 `length` 结束时，真实闭合的 observed
value 才作为局部单元接受依据。

如果模型错误地声明完成，而 manifest 重放后的 candidate 仍未通过原始 schema、
ensure rule 或已声明 validator，Agently 会保留所有已接受单元，并使用现有的有界
validation retry 额度，只请求缺少或需要追加的单元。manifest、readback、digest 或
lineage 不一致属于完整性故障，绝不会交给模型“修复”，而是立即失败。

这是直接 ModelRequest 的交付策略。未显式选择 task strategy 时，它会选择 direct
route；若与显式 AgentTask strategy 混用，则在任务执行前失败。规划/工具工作继续放在
AgentTask，超长最终 artifact 使用独立的 direct delivery execution。

`get_meta()["long_output"]` 会报告 request/segment/unit 数、重放与拒绝单元数、
无进展事件数、最终 validation 修正次数、已接受 digest、校验状态和实际保证等级。
失败运行的 `execution.diagnostics["long_output_no_progress"]` 会保留有界的
reason/header/manifest 事实，但不复制原始 provider 正文。transport 与 schema 完整
并不等于业务清单必然穷尽；如果必须证明覆盖率，应通过 `ensure_keys`、Pydantic
constraint 或 `.validate(...)` 声明 expected count/key/reference 规则。没有这类规则时，
`semantic_exhaustiveness` 保持 `"not_claimed"`。

可运行示例见
[`examples/basic/ensure_long_output.py`](../../../examples/basic/ensure_long_output.py)：
它生成 75 个 JSON 元件，声明 count/order coverage validator，并设置有界的真实模型
请求预算。2026-07-28 的 Qwen 记录运行跨多个截断窗口保留了全部 75 个元件，并通过
一次最终 validation 修正请求只补充缺少的 summary。

### Instant Streaming

当调用方需要在完整响应结束前看到字段级更新时，使用
`get_generator(type="instant")` 或 `get_async_generator(type="instant")`：
进度面板、实时表单、可分区渲染的长报告、模型阶段 dashboard，或能在剩余响应还在
生成时先路由某个字段的 workflow UI。对于单一自由文本成品，用 `type="delta"`；
纯文本没有结构化字段路径可供 instant 事件使用。

`instant` 事件不是“最终结果分块”。它是 `StreamingData` patch：

- `path` 标识字段，例如 `customer_reply` 或 `risk_flags[0]`；
- `wildcard_path` 归一化数组下标，例如 `risk_flags[*]`；
- `delta` 是这次新增的片段，用于渐进渲染；
- `value` 是该 path 当前的 parser 值；
- `is_complete` / `event_type == "done"` 表示字段关闭。

把 stream 当作临时 UI / 进度状态。结束后用 `get_data()` / `async_get_data()`
读取可靠业务状态；它读取同一个 response 的最终缓存解析结果，不会重新发模型请求。

| 输出模式 | Instant 支持 | 使用建议 |
|---|---|---|
| `auto` | 支持，auto 先解析为 `json`、`hybrid` 或 `xml_field` 后使用对应流式解析器。 | 仅在明确接受 schema-driven 选择时使用。如果 auto 最终降级到 JSON 重试，用最终解析结果覆盖或丢弃临时 UI 状态。 |
| `flat_markdown` | 支持，按 `### field` 章节输出字段级 text delta。 | 显式兼容模式。省略格式时优先保持 `json` 默认；只有目标模型适配时才显式使用 `xml_field` 或 `hybrid`。 |
| `hybrid` | 支持，按章节输出字段级 text delta。JSON block 内容先按文本流出，最终再解析成 typed 值。 | prose/code + 结构化 records/control fields 的显式路径。instant 用于 UI/进度，最终 typed 结构用 `get_data()` / `async_get_data()`。 |
| `xml_field` | 支持，在 `<field name="..." type="...">` block 内输出字段级 text delta。 | 当显式 boundary 比 Markdown header 更容易被目标模型遵循时使用。最终解析消费归一化后的 answer payload，不消费 provider reasoning。 |
| `yaml_literal` | 支持，在目标 YAML boundary 内输出顶层字段 delta。 | 作为临时 UI 状态使用。最终 YAML parsing 对缩进敏感，应以 `get_data()` 结果为准。 |
| `json` | 支持，走增量 JSON parser。 | 适合数组或嵌套对象的路径级更新。流式阶段更依赖模型及时输出合法 JSON 片段；完成后仍会做最终 repair/parse。 |
| 纯文本 / `text` | 不提供结构化 instant path。 | 用 `type="delta"` 做文本增量流式，或完成后 `get_text()`。只有调试 provider 级原始事件时才使用 `original` / `original_delta` 视图。 |

对延迟敏感的结构化生成，把紧凑、彼此独立的触发记录放在长解释或大结构前面。
一个实用顺序是：

```text
retrieval_tasks
-> 有界 generation_plan / risk_checks
-> 简短、用户可读的 progress_message
-> large_artifact
```

这样完整的 `retrieval_tasks[*]` 可以提前启动有界准备工作；观察到
`large_artifact` 的第一个事件时，也可以映射成宿主拥有的稳定
`generating_artifact` 状态。先统一生成所有独立触发记录；除非解释是下一项的真实
前置条件，否则不要在每一项后插入长解释。

`generation_plan`、`evidence_assessment` 或 `risk_checks` 只有在后续字段、
workflow 阶段或用户过程视图会消费这个有界产物时，才可能改善复杂结果。必须定义其
类型、边界、可见性、保留策略和失败行为。不要要求隐藏思维链，也不要增加没有消费者
的通用 `reasoning`、`analysis` 或 `thinking` 字段。

当较大的未完成 JSON buffer 超过配置的安全阈值时，增量 JSON parser 可能发出
`$status.status == "streaming_parse_deferred"`。因此应保持前置控制字段紧凑。
deferred streaming 只会失去渐进优化，不会改变最终正确性；最终 parse 与 validation
仍是权威。hybrid 的 typed JSON block 在流式阶段是 block text，finalization 时才成为
typed value；需要嵌套 path 级提前触发时使用 JSON。

### 当前格式契约

当前指导基于已经实现的 parser / prompt 契约。大规模生产推荐前，应使用代表性目标模型
重新验证。格式推荐实验必须保存原始输出，只校验解析、必填字段存在和结构类型；不得用
分词、关键词或子串匹配作为模型生成内容正确性的判断信号。

| 关注点 | 契约 |
|---|---|
| `auto` 选择 | 只看 schema 结构。不看字段名、描述、模型输出或业务语义。 |
| `flat_markdown` | 仅保留为显式兼容模式，不再由 auto 选择。 |
| 默认选择 | 省略 `.output(..., format=...)` 时读取 `prompt.default_output_format`；全局默认是 `json`。 |
| `hybrid` | 字符串字段是 Markdown section。非字符串字段是 fenced JSON block，并且必须解析成 JSON value，包括 boolean 和 number。显式 `format="hybrid"` 或 auto 会将字符串 + typed 混合 schema 解析到该格式。当前 qwen2.5:7b 稳定性检查发现过标题缺失和脚手架注释回显，因此除非目标模型已通过代表性测试，否则保持显式使用。 |
| `xml_field` | 使用一个 `<agently_output>` payload 和 `<field name="..." type="text|json">` block。parser 是 XML-like boundary parser，不是严格 XML。显式 `format="xml_field"` 或 auto 会将扁平纯字符串 dict 解析到该格式。 |
| `yaml_literal` | 使用目标 YAML boundary；长文本字段使用 literal scalar。显式 opt-in，默认不进入 auto。 |
| reasoning 文本 | provider-native reasoning 和目标 payload 前面的完整外层 `<think>...</think>` 会在解析前归一为 reasoning event。payload/code/text 内部的 `<think>` 会保留。 |
| 元组 `ensure` | 第三槽 `True` 会编译为 `ensure_keys`，检查路径/key 是否出现。第三槽 `"not_null"` 显式开启严格值存在校验：`None`、空白字符串、空列表或空 wildcard 匹配，以及列表中包含缺失必填值都会重试；`False` 与 `0` 仍然有效。 |

典型用法：

```python
# 默认：json，来自 prompt.default_output_format。
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# 按 agent 显式 opt-in：省略 .output(..., format=...) 时改用 auto。
agent.set_settings("prompt.default_output_format", "auto")
agent.input("Create a self-contained page.").output({
    "html": (str, "complete HTML document"),
    "notes": (str, "short implementation notes"),
}).start()

# 下游契约期待 JSON 时，显式固定 json。
agent.input("Extract invoice fields.").output({
    "vendor": (str, "vendor name", True),
    "line_items": [{"sku": (str,), "amount": (float,)}],
}, format="json").start()

# prose/code 字段混合 records 时，可显式使用 hybrid。
agent.input("Create an EDA netlist with design notes.").output({
    "analysis": (str, "one paragraph design rationale", True),
    "components": [{"refdes": (str, "reference designator", True), "value": (str, "part value", True)}],
    "nets": [{"name": (str, "net name", True), "connections": [{"refdes": (str, "refdes", True), "pin": (str, "pin", True)}]}],
}, format="hybrid").start()

# 长文本混合 typed records 时，使用 XML-like field envelope。
agent.input("Create lesson material.").output({
    "lesson_script": (str, "long lesson script", True),
    "environment_checklist": [{"item": (str,), "why": (str,), "command": (str,)}],
    "final_confirmation": (str, "one sentence", True),
}, format="xml_field").start()

# 纯文本：一个成品文档，不走结构化 parser。
html = agent.input("Write a complete landing page as HTML.").start()
```

渐进式 UI 示例：

```python
result = (
    agent
    .input("把这条事故记录改写成客户可读状态更新：...")
    .output(
        {
            "status_summary": (str, "一句话状态", True),
            "risk_flags": [(str, "风险点", True)],
            "customer_reply": (str, "客户回复", True),
        },
        format="json",
    )
    .get_result()
)

ui_state = {}

async for item in result.get_async_generator(type="instant"):
    if item.delta:
        ui_state[item.path] = ui_state.get(item.path, "") + item.delta
        await websocket.send_json({
            "path": item.path,
            "delta": item.delta,
            "done": item.is_complete,
        })

final = await result.async_get_data()
await save_case_update(final)
```

## 流水线

```text
   模型返回文本
       │
       ▼
1. parse / repair          ← 从文本中抽取结构化对象
       │
       ▼
2. Pydantic validation     ← 使用最初声明的 BaseModel（如有）
       │
       ▼
3. strict output           ← 对照 .output(...) 形态校验；启用了 ensure_all_keys 则全检查
       │
       ▼
4. ensure_keys             ← 每叶子的必填路径检查（由 ensure 标记编译而来）
       │
       ▼
5. custom validate         ← .validate(handler) 与 validate_handler= 业务规则
       │
       ▼
   通过 → 返回结果   |   失败 → retry（预算未耗尽时）→ 回到顶部
```

任意一步可重试的失败都会触发重试。Pydantic 校验失败会把有界的字段级修正信息加入下一次
attempt；可重试的自定义 validator 失败也会把有界的 `reason` 加入同一份修正
提示词。重试共用一份预算，由 `max_retries`（默认 `3`）控制。预算耗尽时：

- Pydantic 模型违反总会抛异常；即使 `raise_ensure_failure=False`，不合规 dict
  也不会成为已接受结果。
- 对 strict shape 或 ensure 失败，`raise_ensure_failure=True`（默认）会抛异常，
  `False` 才会返回最近一次解析结果。

## validate 在哪一步

`.validate(handler)` 注册自定义检查。它在 strict output 与 `ensure_keys` 都通过**之后**跑，作用对象是结果的 canonical dict snapshot。

当可重试的 handler 结果未通过时，Agently 会把它的 `reason`（最多 300 字）
提供给下一次模型调用，并要求重新给出完整输出。可选的 validation `payload`
和 handler 异常详情仍然只用于 host/runtime 诊断，不会自动复制进模型提示词。

```python
def must_be_short(result, ctx):
    if len(result.get("answer", "")) > 280:
        return {"ok": False, "reason": "answer 太长", "validator_name": "length"}
    return True

agent.input("总结。").output({
    "answer": (str, "answer", True),
}).validate(must_be_short).start()
```

handler **只**挂在结构化结果 getter 上：`start()`、`async_start()`、`get_data()`、`async_get_data()`、`get_data_object()`、`async_get_data_object()`。**不挂**在 `get_text()` / `get_meta()` 上（它们没有 validate 要看的解析结构）。

## 字段顺序与评估等级

Agently output schema 是有序的。当后续字段依赖前置判断时，把支撑字段放在前面：
证据、假设、澄清、来源说明、计算计划、简要依据、规则检查、中间事实。最终布尔值、
评判、回复、总结和行动决策放在后面。面向人类展示时可以按自然阅读习惯重排，但模型
生成契约应保持「支撑信息先于结论」。

模型负责分级、置信度、可信度、相关性、可用性或质量评估时，优先使用带明确定义的
概念等级，而不是精确数字分数。例如要求输出 `high_trust`、`moderate_trust`、
`low_trust`，并在提示词里定义每个等级。若下游代码需要阈值、加权、统计或指数化
计算，在模型输出后用代码把等级映射为确定数字。

复杂算术、长位数计算、加权聚合或统计转换不要直接交给模型文本生成。让模型输出可执行
的计算计划或代码，通过工具运行，再把原始问题、代码和运行结果交给后续模型步骤使用。

也可以在调用时传 handler：

```python
agent.input("...").output({...}).start(validate_handler=must_be_short)
agent.input("...").output({...}).start(validate_handler=[check_a, check_b])
```

`.validate(...)` 注册的 handler 先于 `validate_handler=` 传入的。多次 `.validate(...)` 调用顺序保留。

## handler 返回值

| 返回 | 含义 |
|---|---|
| `True` | 通过 |
| `False` | 失败 —— 预算未耗尽则重试 |
| `dict` | 结构化结果，见下表 |

支持的 dict key：

| Key | 效果 |
|---|---|
| `ok` | `True` 通过，`False` 失败 |
| `reason` | 出现在 retry event / 错误信息中 |
| `payload` | 给下游的结构化细节 |
| `validator_name` | 给该 validator 起名（用于事件） |
| `no_retry` / `stop` | 失败但不重试 |
| `error` / `exception` / `raise` | 用指定异常失败 |

不在此列的返回会变成 `model.validation_error` 并消耗预算。

## Async handler

sync 与 async handler 都支持：

```python
async def check_remote(result, ctx):
    ok = await some_external_check(result["answer"])
    return ok
```

## Context 对象

handler 第二个参数是 `OutputValidateContext`，至少包含：

- `value`、`input`、`agent_name`、`response_id`
- `attempt_index`、`retry_count`、`max_retries`
- `prompt`、`settings`、`request_run_context`、`model_run_context`
- `response_text`、`raw_text`、`parsed_result`、`result_object`、`typed`、`meta`

需要根据「第几次尝试」改变行为时（如最后一次放宽规则），用 `ctx.attempt_index`。

默认把这些字段当作观察上下文来读；但 `ctx.prompt` 与 `ctx.settings` 是当前 response attempt 链路上的 live state。高级用法里，如果你要调整**后续 retry** 的 prompt / options / settings，可以在 validator 里直接写回它们。

例如，降低下一次 retry 的采样参数：

```python
def check(result, ctx):
    if result.get("score", 0) < 0.8 and ctx.retry_count < ctx.max_retries:
        ctx.prompt.set("options", {"temperature": 0.2, "top_p": 0.7})
        return {"ok": False, "reason": "score too low"}
    return True
```

或者改 settings：

```python
def check(result, ctx):
    if should_switch_mode(result):
        ctx.settings.set("my_plugin.some_flag", True)
        return False
    return True
```

注意两点：

- 这些写入只影响**后续 retry**，不会改变当前这次已经完成的 attempt。
- 这些写入也**不会污染后续新请求**。每次新建 `response` 时都会从 request / agent 层重新做一次 prompt 与 settings 快照；validator 里的写回只停留在当前 response 的 retry 链里。
- 不要依赖 `opts = ctx.prompt.get("options", {})` 后再原地改 `opts`。`get()` 返回的是 view/copy；要持久生效，使用 `ctx.prompt.set(...)`、`ctx.prompt.update(...)`、`ctx.settings.set(...)` 这类写接口。

## 单 response 单次执行

每个 `ModelRequestResult` 只跑**一次** validation 并缓存结果。多次调用——`get_data()` 再 `get_data()`，或 `get_data()` 后 `get_data_object()`——**不会**重跑 validator。如果 validation 已经定型后再往同一个 result 注入新 handler，新 handler 被忽略并发 warning。

含义：不要为不同 consumer 切换 validator。需要不同校验时，发两次请求。

## Retry 事件与可观测

validate 引入两个新 observation event：

- `model.validation_failed` —— handler 返回失败
- `model.validation_error` —— handler 抛异常 / 返回不支持的值

phase 1 **没有** `model.validation_passed` 事件 —— 通过是默认且静默的。

`model.retrying` 事件在 retry 由 validate 触发时会带上 validation 相关字段：

- `retry_reason`、`validator_name`、`validation_reason`、`validation_payload`

`../Agently-Devtools` 防御性消费这些事件，新 key 不破坏现有 dashboard。

## 与 ensure_keys 的关系

`ensure_keys` 与 `.validate(...)` 是分层的：

- `ensure_keys` 处理**路径存在性**（由 `.output(...)` 中的 `ensure` 编译而来）。
- 元组 `"not_null"` 处理常见的内置**值存在性**规则，用于空值也应触发重试的字段。
- `.validate(...)` 处理基于实际内容的**值规则**。

固定必填叶子优先写 `(TypeExpr, "description", True)`，不要把同一批路径再手动重复到 `ensure_keys=`。只有当空值对该字段非法时，才写 `(TypeExpr, "description", "not_null")`。条件型或运行时决定的路径，再用手动 `ensure_keys`。而「这字段必须满足某业务规则」用 `.validate(...)`。

## 常见模式

**最后一次放宽**：

```python
def check(result, ctx):
    if ctx.attempt_index == ctx.max_retries:
        return True  # 接受现有结果
    return strict_check(result)
```

**失败但不重试**（如 validation 暴露了一条永久性业务问题）：

```python
def policy_check(result, ctx):
    return {"ok": False, "reason": "policy violation", "no_retry": True}
```

**抛自定义异常**：

```python
def policy_check(result, ctx):
    return {"ok": False, "raise": MyDomainError("rejected by policy")}
```

## 另见

- [Schema as Prompt](schema-as-prompt.md) —— `.output(...)` authoring 与 `ensure` 标记
- [模型响应](model-response.md) —— 缓存与重跑的实际差别
- [术语表：ensure](../reference/glossary.md#ensure-third-tuple-slot)
