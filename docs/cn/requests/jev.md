# 在 output schema 中使用 Jev

开发中特性：`Probability`、`Choice`、`Score` 是 output 的原子判断叶子。
Agent 的 Execution 编排 Jev 与普通 LLM，再由 Host 组装结果。
`direct` 也经过 Execution；一次 Agent 执行可以包含多次 ModelRequest。

```python
import os
from dotenv import find_dotenv, load_dotenv
from agently import Agently, Probability, Choice, Score

load_dotenv(find_dotenv(usecwd=True))
Agently.set_settings("Jev", {"api_key": os.environ["JEV_API_KEY"]})
Agently.set_settings("system_one", {"provider": "Jev"})
agent = Agently.create_agent()
execution = agent.input("所有打款都失败了，请今天修复。").output({
    "urgent": Probability("客户是否明确要求今天修复？"),
    "team": Choice("应由哪个团队处理？", {
        "billing": "付款与打款", "sales": "购买咨询",
    }),
    "urgency": Score("请求有多紧急？", ["未表达紧急性", "有时效要求", "明确要求当天处理"]),
})
result = execution.get_data(max_retries=1)
details = execution.get_meta()["judgment"]
```

| 表达 | 业务值 |
|---|---|
| `Probability(question)` | [0, 1] 浮点数 |
| `Choice(question, options)` | 一个候选键，最多 255 项 |
| `Score(question, options)` | [0, 等级数−1] 浮点数，2–10 个有序等级 |

Score 可以落在等级之间，不会自动取整。“可信任 / 不可信任 / 无法判断”是无序分类，
应使用 Choice。question 必须表达完整问题，不能靠字段名补充判断含义。
Jev 只做判断，不生成解释或思维链。

## 独立配置

`Jev` 对应 `plugins.ModelRequester.Jev`。请求级覆盖 Agent 级，Agent 级覆盖全局。
默认 API base 为 `https://api.typesafe.ai/v1`，模型为 `jev-latest`，超时 60 秒，
每批最多 64 个判断。可配置 `base_url`、`model`、`timeout`、`batch_size`。
凭据与 `OpenAICompatible` 独立，不进入模型上下文。

未配置或关闭 `system_one` 时，判断转换为普通 LLM schema；仅配置 Jev 凭据不会启用它。
通过 `Agently.set_settings("system_one", {"provider": "Jev"})` 选择 Jev 后，必须提供有效凭据；
缺失或无效时在调用前报错，不自动降级。
`Agently.set_settings("Jev.enabled", False)` 则显式使用普通 LLM，即使留有无效 Jev 凭据。
Jev 调用失败不会静默转 LLM。
LLM 结果标记实际生产者，不伪造 Jev 原生分布或 confidence，也不宣称概率已校准。

纯静态 Jev 输出不需要 LLM 配置。混合输出还需要普通 ModelRequester；保持 LLM 为当前
provider，Execution 只在判断阶段局部切换到 Jev。关闭 Jev 且没有输出依赖时，合并为
一次普通结构化 LLM 请求。

## 输出依赖

```python
execution = agent.input(source_records).output({
    "items": [{
        "name": (str, "复制提供的记录名称"),
        "message": (str, "逐字复制同一条记录的消息"),
        "urgent": Probability("这条消息是否要求当天回复？", from_output="items[].message"),
    }],
})
result = execution.get_data()
```

这里先由 LLM 生成并校验列表及 message，再由 Jev 逐项判断，Host 填入固定条目。
空列表不发起逐项判断。根列表可写 `[].name`，`[]` 与 `[*]` 等价，支持嵌套列表。
同列表内绑定当前条目，列表外引用读取匹配集合。不同列表不隐式按位置配对；
数字索引明确选择某个条目。

静态就绪判断优先执行，结果作为只读证据提供给普通 LLM。判断也可引用其他判断字段，
形成后续阶段。不存在的路径、自引用、循环和跨列表通配符歧义在网络调用前报错。
不从自然语言猜测依赖。

当前支持 JSON、字典和单项列表 schema；含 `.`、括号或 `*` 的字面键会拒绝。
暂不支持可选判断 union 和与 `auto_continue` 组合；本次不新增普通 LLM 叶子的依赖语法。
`${OUTPUT...}` 仍是提示词位置标签，不是运行时取值表达。

## 结果与失败处理

`get_data()` 返回组装后的业务值，`get_data_object()` 返回校验模型。
`get_meta()["judgment"]` 保留字段来源、请求 id、revision、阶段状态/耗时及原生
answers/model/usage；Choice、Score 的分布、confidence、legend 也在其中。
LLM 不能覆盖 Jev 字段。最终 validate、artifact、review 面向组装结果。

所有阶段共享 `max_retries`：每个必要阶段的第一次调用不扣重试额度，再次尝试扣一次。
复用成功前驱；配置/依赖错误及取消不重试。子请求关闭底层 provider 重试、密钥故障转移和
独立输出修复，避免叠加额度。阶段进度不是最终结果，业务动作应等待最终校验。

独立原子 Jev 请求可设置
`request.set_settings("plugins.ModelRequester.activate", "Jev")`，output 只能含静态判断。
混合、动态结构和 `from_output` 使用 Agent Execution；独立请求的原生响应通过
`get_data(type="original")` 读取。

参见[可运行示例](../../../examples/jev_output.py)和
[TypeSafe API 文档](https://docs.typesafe.ai/api)。


## 多来源和前置输出

`from_output` 支持非空、无重复路径数组，绑定值为以原始路径为键的对象；
单元素数组也保留对象结构。单字符串仍传原始值，逐项/集合定位语义不变。

`after_output` 接受路径或路径数组：保证这些字段在判断之前完成，
但不把其值作为绑定信息送给 Jev。两类前置字段可在同一次 LLM 请求中
按原 schema 顺序生成，不强制增加调用。原 input/info/instruct 仍为公共上下文，
因此这不是敏感信息隔离功能。

```python
{
    "evidence": (str, "列出形成结论所依据的原始事实，供结论使用和审计"),
    "conclusion": (str, "根据 evidence 给出简要结论"),
    "risk": Score("结论所述问题有多严重？", ["轻微", "明显", "严重"],
                  from_output=["conclusion"], after_output=["evidence"]),
    "summary": (str, "结合结论及已接受的 risk 评分汇总"),
}
```

执行为：LLM 同次生成 evidence/conclusion → Jev → LLM summary。
动态列表示例见 [jev_output.py](../../../examples/jev_output.py) 的 `--dynamic` 模式。

## 输出模板与模型解耦

`OutputTemplate` 是 provider 无关的模板基类，包含 question、from_output、
after_output 和 to_schema()。Probability、Choice、Score 是同级模板；
Jev requester 负责把支持的模板翻译成原生协议，其他模板走普通 LLM。
自定义模板的 to_schema() 返回 `(Python类型, 描述, True, {"judgment": True})`，
类型可以是 Pydantic 模型。可运行示例见
[output_templates.py](../../../examples/output_templates.py)。

模板定义结果契约，Execution 策略负责模型和运行选项。小型 LLM、模型支持的
no-reasoning 模式也可生产这些模板或其他结构；继续使用现有模型/provider 配置。
通过 [SystemOne](system-one.md) 配置模板专用模型，不将模板本身视作延迟或校准保证。

SystemOne 未配置时默认关闭。Agent 使用 Jev 还需设置
`Agently.set_settings("system_one", {"provider": "Jev"})`；只有 Jev 凭据不会自动开启。
`.use_system_one(False)` 将模板交回普通 LLM，显式依赖仍生效。
