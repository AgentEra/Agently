# SystemOne：输出模板的专用模型层

SystemOne 对应“系统一”，位于 Agent Execution 内，负责为输出模板选择快思考模型。
输出模板定义结果契约，SystemOne 选择模型，ModelRequester 执行协议请求。
Jev、小型 LLM、支持无 reasoning 的模型都可承担这一角色。

```python
import os
from dotenv import find_dotenv, load_dotenv
from agently import Agently, Probability

load_dotenv(find_dotenv(usecwd=True))
agent = Agently.create_agent()
agent.set_settings("OpenAICompatible", {
    "base_url": os.environ["OMLX_BASE_URL"],
    "api_key": os.environ["OMLX_API_KEY"],
    "model": os.environ["LLM_MODEL"],
})
agent.set_settings("system_one", {
    "provider": "OpenAICompatible",
    "base_url": os.environ["OMLX_BASE_URL"],
    "api_key": os.environ["OMLX_API_KEY"],
    "model": "Qwen3.5-9B-MLX-4bit",  # 先核对 /v1/models 的实际 ID
    "request_options": {
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": 800,
    },
})
result = agent.use_system_one(True).input("请今天修复付款问题。").output({
    "urgent": Probability("客户是否明确要求当天解决？"),
    "summary": (str, "结合原始事实与已接受的 urgent 判断进行简要汇总"),
}).get_data(max_retries=0)
```

配置非空 `system_one` 模型后默认开启；未配置或空对象默认关闭。
`.use_system_one(False)` 显式关闭，`True` 显式开启；显式开启缺模型配置时报错。
也可使用配置的 `enabled=False`。Agent 上的方法创建一次 Execution，Execution 上的方法
只配置当前草稿，不改 Agent 默认设置。执行开始后拒绝重新配置；下次运行请创建新 Execution，旧结果保持不变。
请求级设置覆盖 Agent 级与全局设置。无模板 output 不增加 SystemOne 调用。

配置沿用 model profile 的 provider/model/base_url/api_key/request_options/client_options
字段。也可只写 `{"model_key": "fast"}`，引用现有 model_pool/model_profiles；
不要与内联模型字段混用。`agently.types.settings.SystemOneSettings` 提供类型化配置。
SystemOne 子请求使用 provider 默认值和专用配置，不继承普通模型的鉴权、headers、model、
request_options 或 prompt options。原 input/info/instruct 仍为共同任务上下文。

使用 Jev 时保留独立 Jev 凭据，再配置 `system_one={"provider": "Jev"}`。
仅设置 Jev 凭据不会自动开启 SystemOne。`Jev.enabled=False` 也会关闭选中的 Jev 生产者。
缺失/无效配置在调用前报错，实际调用失败不静默切换模型。

## 执行顺序与结果

- 无依赖模板：SystemOne → 普通 LLM 生成其余字段。
- `from_output` / `after_output`：普通 LLM 可同次按 schema 顺序生成前置字段 → SystemOne → 其余普通字段。
- 关闭时：模板由普通 LLM 负责，显式依赖保留；无依赖部分可以合成一次请求。
- 自定义 OutputTemplate 可以输出字符串或结构化对象，不限定为三种内建模板。
  选择 Jev 时不支持的模板走普通 LLM；选择 SystemOne LLM 时由该 LLM 处理。

依赖、共享重试、取消和 Host 组装仍由现有 Execution/TriggerFlow 负责。
后续 LLM 收到问题、结果以及模板描述、候选含义、评分标准和 JSON Schema，避免只传数字而丢失量纲。
`execution.get_meta()["judgment"]` 记录 SystemOne 是否开启、各阶段的 provider/model、
角色、耗时及实际观察到的 reasoning 字符数，不记录凭据。

`enable_thinking=False` 是部署服务支持的参数，不是所有模型通用的 Agently 开关。
生成式 LLM 的概率/评分仍为估计值，不等同 Jev 的原生分布或校准能力。
额外请求和模型加载可能抵消推理节省，本次有限本地样本尚未证明总耗时改善。
完整可运行示例：[system_one.py](../../../examples/system_one.py)，支持纯模板、混合、动态、自定义及 `--disabled` 对照。
