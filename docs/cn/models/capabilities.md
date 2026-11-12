# 按用途配置模型与 Agent 多模态串联

开发中的规范配置按用途分开，复用已有 provider 和模型池：

```python
agent.set_settings("llm", {"provider": "OpenAICompatible", "model": "main-model", **llm_connection})
agent.set_settings("vlm", {"provider": "OpenAICompatible", "model": "vision-model", **vision_connection})
agent.set_settings("embeddings", {"provider": "OpenAICompatible", "model": "embedding-model", **embedding_connection})
agent.set_settings("stt", {"provider": "OMLX", "model": "asr-model", **audio_connection})
agent.set_settings("tts", {"provider": "OMLX", "model": "speech-model", **audio_connection})
```

各连接分别设置 base_url、api_key、request_options 等。即使使用相同 provider，
也不互相继承密钥、headers 或请求参数。类型化配置使用 ModelUseSettings。
也可以配置 `{"model_key": "pool-alias"}`，引用已有 model_pool/model_profiles/
api_key_pools；model_key 不与 inline profile 混用。请求显式 model_key 优先于 llm。
4.1 兼容线在 llm 未配置时保留 provider 命名空间的旧配置路径；已配置但无效的
用途不会被当作缺省，调用失败也不自动换模型。

## 图片

```python
execution = (
    agent.image("note.png", question="仔细阅读留言条。")
    .input("哈蒙德在哪里？")
    .output({"location": str, "evidence": str})
)
data = await execution.async_get_data()
```

question 只约束本次 image 调用的图片组，input 表示整体任务，output 表示最终结果。
多次 image 追加图片并保留次序与局部问题；支持 file/url/files/urls。
attachment 显式替换附件，同时清除旧图片路由声明。

| mode | 普通 Agent 任务 |
|---|---|
| vlm（默认） | 同时配置 vlm 与 llm：VLM 证据 → LLM；只配置一个：该模型直接完成 |
| llm | 原图直接交给 llm，跳过独立 VLM |
| ocr | OCR 提取文字，再将证据交给 llm |

接收原图的 LLM 必须支持视觉。对已知不支持的配置可设 vision=False，在分发前拒绝；
未知支持情况保留服务错误，不凭模型名猜测。无图的文本任务不创建视觉前处理阶段；
只配置 vlm 时也可以用它完成文本任务。管线将整体 input/info/instruct、图片局部要求
及最终交付要求交给 VLM，保留文字、空间/角色关系与不确定项。中间证据使用自己的
schema，最终生产者才按 output 交付。

`.vlm_only(True)` 要求独立 vlm，让本次 Execution 的最终结果由它直接生成。
在开始执行前设置；False 恢复普通路由。不能与 mode=llm/ocr 混用。

`await execution.async_to_text()`（同步 to_text）在启动前选择直接图片操作，
不追加 LLM。它仍遵循 input/question/output；结构化结果校验后按已有规则投影为文本，
随后 get_data 读取同次结果，不重复推理。普通 get_text 只是结果视图，不改变路由。
没有问题或整体任务时，视觉模型默认描述图片及可辨认文字，如实说明不确定内容。

OCR 是文字提取能力。可配置：
`ocr={"provider":"MistralOCR", "model":"mistral-ocr-latest", ...}`。
内置适配器使用 [Mistral OCR API](https://docs.mistral.ai/api/endpoint/ocr)，返回页面 Markdown。
独立 mode=ocr/to_text 仅提取文字；需要 question/input/output 推理时用 OCR → LLM
普通管线。缺少 OCR 配置提前报错，多图为多个原子 OCR 请求，不静默改用 VLM。


OCR 服务若提供 OpenAI 兼容协议，可直接复用 `OpenAICompatible`；只有服务协议不同，
才需要另写 Requester。例如通过 oMLX 提供服务的 GLM-OCR 或 PaddleOCR：

```python
agent.set_settings("ocr", {
    "provider": "OpenAICompatible",
    "base_url": os.environ["OCR_BASE_URL"],
    "api_key": os.environ["OCR_API_KEY"],
    "model": os.environ["OCR_MODEL"],
})
text = await agent.image("note.png", mode="ocr").async_to_text(max_retries=0)
```

model 使用服务实际返回的 ID。`max_retries` 默认是 3，设为 0 可关闭 Execution
共享额度内的修复重试，不改变路由。完整的纯 OCR 与 OCR → LLM 结构化回答见
[OCR 示例](../../../examples/model_capabilities/ocr.py)。

## 音频输入与朗读

stt/tts 仍是独立 AudioModelRequest 转换动作，可配置不同提供方和模型。
显式 use_audio 绑定优先于配置构建；已有 Execution 绑定引用不会随 Agent 替换漂移。

```python
execution = agent.input(file="question.wav", type="audio").instruct("简洁回答。")
speech = await execution.async_say()  # STT → Agent 处理 → TTS
text = await execution.async_get_text()  # 复用同次文本结果
```

支持有限本地文件或 AudioInput，不隐式录音、下载 URL 或转换音频。
`input({"type":"audio", "file":"业务编号"})` 仍为普通业务数据；音频声明使用
独立 file/type 关键字。后续 input 可以给整体任务，转录仍作为输入证据保留。
音频文件属于当前 Execution，不支持 always=True 默认声明。

say/async_say 返回 SpeechResult；空文本返回 None。scope="final" 为默认，只朗读
最终文本投影；scope="all" 加上 Execution 公开过程中的自然语言部分。
不会直接朗读 provider 原始 delta、reasoning、工具参数或 JSON 碎片。
普通结构化最终结果沿用 Execution 的 JSON 文本投影，不额外请求模型改写。
同参数 say 再次读取复用音频结果；语音消费失败不抹去已完成的文本结果。

```python
async with execution.stream_say(scope="all") as segments:
    async for speech in segments:
        await your_audio_sink(speech.data, speech.media_type)
```

scope 决定内容范围，stream_say 决定分块交付完整 SpeechResult，沿用 TTS 分段与背压。
语音流为单次消费，不自动重播。若该流启动了 Execution，提前关闭会取消尚未完成的工作。
公开过程说明属于暂定内容，已交付后无法撤回；只需接受后的结果时使用 final。
所有入口均不隐式播放或写文件。

音频用途支持 base_url/api_key/headers/client_options/timeout.read；request_options
提供 voice、response_format、speed、language、instructions 或 STT 的 language/prompt
及提供方扩展参数。调用时的类型化 options 覆盖默认值。音频 full_url 与密钥 failover
明确拒绝，不静默忽略或重播。

## 向量与执行边界

`await agent.async_embed("文本")` / `agent.embed(["一", "二"])` 返回 list[list[float]]，
单文本也是一行。保持输入次序；缺失/重复索引、维度不一致和非有限值会报错。
它不修改 llm，也不自动迁移既有向量索引的模型身份。

Agent 任务都经过 Execution，媒体阶段由 TriggerFlow 编排，各 Requester 负责原子调用。
前处理重试消耗本次重试额度，最终生产得到剩余额度；成功前驱不会因后续重试重跑。
SystemOne、Actions、最终校验和取消保留各自 owner。
`get_meta()["media"]` 提供阶段与重试信息；文本模型保留请求 lineage。
音频阶段元信息不等于已经提供统一 token 计费。

示例：[图片](../../../examples/model_capabilities/vision.py)、
[音频任务](../../../examples/model_capabilities/audio_task.py)、
[向量](../../../examples/model_capabilities/embeddings.py)。

尚未处理的音频声明和已绑定音频能力需要显式重绑定合同，不能声称内置 save/load 可恢复。
rework 复用已完成的输入证据；语音缓存按 revision 隔离。直接 OCR 不处理 rework
反馈，也不能满足 AgentTask/Action 路由合同。
