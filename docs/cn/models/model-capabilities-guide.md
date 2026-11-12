---
title: 模型角色与多模态 Execution
description: Agently 如何配置不同模型用途，并把视觉、OCR、音频、向量和 SystemOne 组织到一次 Execution 中。
keywords: Agently, Execution, VLM, OCR, STT, TTS, embeddings, SystemOne, Jev
---

# 模型角色与多模态 Execution

Agently 把“使用哪一种模型”和“这次任务怎样运行”分开处理。
`llm`、`vlm`、`ocr`、`stt`、`tts`、`embeddings` 是模型用途；`OpenAICompatible`、
`AnthropicCompatible`、`OMLX` 和 `Jev` 是协议或服务适配器。用途配置选择模型，
Requester 负责一次原子协议请求，`AgentExecution` 负责把多个阶段组织成一个可观察、
可取消、共享重试额度的任务。

因此，即使任务最后只有一次模型请求，也从 Execution 进入。这样直接请求和多阶段任务
拥有相同的生命周期、结果读取、取消、重试和元信息边界；未来增加 OCR、SystemOne 或
语音交付时，不需要再设计一套并行调度入口。

## 1. 按用途配置模型

每种用途都有自己的 provider、model、连接信息和 `request_options`。相同 provider 的
不同用途也不会互相继承密钥、headers 或生成参数。

```python
import os

from dotenv import find_dotenv, load_dotenv
from agently import Agently

load_dotenv(find_dotenv(usecwd=True))

agent = Agently.create_agent()
connection = {
    "base_url": os.environ["OMLX_BASE_URL"],
    "api_key": os.environ["OMLX_API_KEY"],
}

agent.set_settings("llm", {
    "provider": "OpenAICompatible",
    **connection,
    "model": os.environ["LLM_MODEL"],
    "request_options": {"temperature": 0},
})
agent.set_settings("vlm", {
    "provider": "OpenAICompatible",
    **connection,
    "model": os.environ["VLM_MODEL"],
})
agent.set_settings("ocr", {
    "provider": "OpenAICompatible",
    **connection,
    "model": os.environ["OCR_MODEL"],
})
agent.set_settings("stt", {"provider": "OMLX", **connection, "model": os.environ["STT_MODEL"]})
agent.set_settings("tts", {"provider": "OMLX", **connection, "model": os.environ["TTS_MODEL"]})
agent.set_settings("embeddings", {
    "provider": "OpenAICompatible",
    **connection,
    "model": os.environ["EMBEDDING_MODEL"],
})
```

也可以把用途配置为 `{"model_key": "pool-alias"}`，引用已有模型池。`model_key` 与
inline provider/model 配置互斥，显式请求覆盖优先于默认的 `llm`。缺少用途配置、配置
无效或服务不支持目标能力时，调用会在对应边界报错，不会静默换模型。

Requester 按协议选择，而不是按模型名称选择。例如，oMLX 提供 OpenAI 兼容接口时，
GLM-OCR 和 PaddleOCR 都复用 `OpenAICompatible`；只有服务协议不同，才需要新增独立
Requester。模型用途 `ocr` 不等于一个固定的 OCR 模型注册表。

## 2. 图片、VLM 与 OCR

`image()` 添加图片组，`question` 只描述这组图片的问题；`.input()` 描述整体任务，
`.output()` 定义最终结果合同。多次调用会按顺序追加图片及其局部问题。

```python
execution = (
    agent.image("note.png", question="仔细阅读这张留言条，并保留可辨认的文字。")
    .input("哈蒙德在哪里？")
    .output({"location": str, "evidence": str})
)
result = await execution.async_get_data()
```

普通图片任务由配置决定拓扑：

| 配置 | 路由 |
| --- | --- |
| 只有 `vlm` | VLM 直接生成最终结果 |
| 只有 `llm` | LLM 直接处理原图（前提是服务支持视觉） |
| 同时有 `vlm` 与 `llm` | VLM 生成视觉证据，LLM 生成最终结果 |
| `mode="llm"` | 本次图片绕过独立 VLM，原图交给 `llm` |
| `mode="ocr"` | OCR 提取文字，再交给 `llm` 完成整体任务 |

`.vlm_only(True)` 可以在 Execution 启动前显式要求配置的 VLM 直接产出最终结果。
`.to_text()` 是直接图片操作，即使 Agent 同时配置了 `llm` 也不会追加 LLM；普通
`get_text()` 只是已完成结果的读取视图。

纯 OCR 提取可以这样写：

```python
text = await agent.image("note.png", mode="ocr").async_to_text(max_retries=0)
print(text)
```

带问题和结构化输出时，OCR 结果会成为后续 LLM 的输入证据：

```python
answer = await (
    agent.image("note.png", mode="ocr")
    .input("哈蒙德在哪里？只根据识别出的文字回答。")
    .output({"answer": str, "evidence": str})
    .async_get_data()
)
```

`max_retries` 默认是 3；设为 0 只关闭本次 Execution 的修复重试，不改变路由。前置
OCR 阶段失败同样消耗整体调度的重试额度。可运行的纯 OCR 与 OCR→LLM 示例见
[ocr.py](../../../examples/model_capabilities/ocr.py)。

## 3. 音频与向量

音频输入是一个明确的处理声明，不是普通字典字段。`type="audio"` 触发 STT 前处理，
转录内容再进入同一个 Execution；`.say()` 是文本完成后的 TTS 消费动作。

```python
execution = (
    agent.input(file="question.wav", type="audio")
    .instruct("简洁回答。")
)
text = await execution.async_get_text()
speech = await execution.async_say(scope="final")
```

`scope="final"` 只朗读最终文本，`scope="all"` 还包括 Execution 已公开的自然语言过程。
原始 delta、reasoning、工具参数和 JSON 碎片不属于朗读内容。需要分段交付时使用
`stream_say(scope=...)`；它复用既有 TTS 分段和背压，不隐式播放或写文件。

向量是独立动作，不修改 `llm` 配置，也不自动迁移已有索引的模型身份：

```python
vectors = await agent.async_embed(["温室", "图书馆"])
assert len(vectors) == 2
```

返回行顺序与输入一致；缺失、重复、维度不一致或非有限值会报错。

## 4. SystemOne：可替换的快思考层

SystemOne 是 Execution 中的一个模型角色，不绑定某一种模型或输出格式。它可以使用
Jev，也可以使用小型 LLM 或服务支持的 no-reasoning 模式。输出模板定义结果合同，
SystemOne 只负责选择并执行专用模型。

```python
from agently import Probability, Score

agent.set_settings("system_one", {
    "provider": "OpenAICompatible",
    **connection,
    "model": os.environ["SYSTEM_ONE_MODEL"],
    "request_options": {
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
})

result = await (
    agent.input("所有打款都失败了，请今天修复。")
    .output({
        "urgent": Probability("客户是否明确要求今天修复？"),
        "urgency": Score("请求有多紧急？", ["未表达", "有时效要求", "明确要求当天处理"]),
        "summary": (str, "结合事实与判断给出一句总结"),
    })
    .async_get_data()
)
```

配置 `system_one` 后默认开启；未配置时默认关闭，`.use_system_one(False)` 可以对
当前 Execution 显式关闭。关闭后模板交给普通 LLM；这会改变模型角色，但不会删除
输出合同或依赖关系。

没有依赖的模板可以先由 SystemOne 产出，再由普通 LLM 完成其他字段。若模板通过
`from_output` 或 `after_output` 声明依赖，Execution 会根据字段依赖安排阶段：

```python
from agently import Probability, OutputTemplate

class ShortAnswer(OutputTemplate):
    def to_schema(self):
        return (str, self.question, True, {"judgment": True})

execution = agent.input("配送已确认周五，客户询问能否周四送达。").output({
    "evidence": (str, "提取确认日期和客户请求，供后续字段使用"),
    "conclusion": (str, "根据 evidence 给出结论"),
    "answer": ShortAnswer(
        "说明已确认日期，不要承诺提前配送。",
        from_output=["conclusion"],
        after_output=["evidence"],
    ),
})
```

`after_output` 表示字段必须先完成，但不把这些字段作为 SystemOne 的绑定输入；
`from_output` 才决定哪些已生成字段进入判断请求。字段依赖不从自然语言猜测，缺失路径、
自引用、循环和有歧义的跨列表通配符会在网络调用前报错。

## 5. Jev 是一种 SystemOne 实现

Jev 只负责快速判断，不负责生成解释或思维链。`Probability`、`Choice`、`Score`
是输出 schema 中的判断叶子：

```python
from agently import Choice, Probability, Score

agent.set_settings("Jev", {"api_key": os.environ["JEV_API_KEY"]})
agent.set_settings("system_one", {"provider": "Jev"})

result = await (
    agent.input("客户要求今天修复付款失败问题。")
    .output({
        "urgent": Probability("客户是否明确要求今天修复？"),
        "team": Choice("应由哪个团队处理？", {
            "billing": "付款、账单与退款",
            "sales": "购买与升级",
        }),
        "urgency": Score("请求有多紧急？", ["未表达", "有时效要求", "明确要求当天处理"]),
    })
    .async_get_data()
)
```

如果所有输出叶子都是 Jev 模板，可以形成一次纯 Jev 请求；混合普通字段时，Execution
会拼装 Jev 与 LLM 阶段。Jev 未配置或被显式关闭时，模板可以映射为普通 LLM 的结构化
schema；显式选择 Jev 但凭据无效时会友好报错，不静默降级。

`from_output` 支持字符串或路径数组，例如 `from_output=["items[].name", "items[].message"]`。
列表字段可与同一条目的前置字段绑定；`after_output` 可以声明同一次 LLM 请求中必须
先生成、但不投放给 Jev 的字段。Jev 失败与普通阶段一样消耗共享 `max_retries`，没有
额度就停止。

## 6. 如何选择入口

可以按下面的顺序判断：

1. 这是一次模型用途转换吗？使用 `async_embed()`、独立 STT/TTS，或图片的 `.to_text()`。
2. 这是一个需要最终业务结果的 Agent 任务吗？通过 `.input()`、`.info()`、`.instruct()`、
   `.output()` 创建 Execution。
3. 任务是否需要图片或音频前处理？使用 `.image()` 或 `input(file=..., type="audio")`，
   让 Execution 按配置建立媒体阶段。
4. 输出中是否有快速判断叶子？使用 SystemOne；如果选择 Jev，再额外配置 `Jev` 凭据。
5. 是否需要多个阶段、取消、流式交付或重试？继续使用同一个 Execution，不要在应用层
   手工拆成互相无法观察的 ModelRequest。

完整 API 细节见[独立模型用途与多模态串联](capabilities.md)、
[SystemOne](../requests/system-one.md)和[在 output schema 中使用 Jev](../requests/jev.md)。
可运行示例见 [vision.py](../../../examples/model_capabilities/vision.py)、
[audio_task.py](../../../examples/model_capabilities/audio_task.py)、
[embeddings.py](../../../examples/model_capabilities/embeddings.py)、
[system_one.py](../../../examples/system_one.py)和[jev_output.py](../../../examples/jev_output.py)。
