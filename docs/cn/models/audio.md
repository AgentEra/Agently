# 音频请求

TTS/STT 使用独立的 `AudioModelRequest`，不走文本 `ModelRequest` 的 Prompt 链。
显式配置音频模型和连接；Agent 文本模型、历史、输出结构和 auto_continue 不会混入音频请求。

```python
import os
from agently import Agently, AudioInput, SpeechOptions

audio = Agently.create_audio_request(
    driver="OMLX",
    base_url=os.environ["AUDIO_BASE_URL"],
    api_key=os.getenv("AUDIO_API_KEY", ""),
    tts_model=os.environ["AUDIO_TTS_MODEL"],
    stt_model=os.environ["AUDIO_STT_MODEL"],
)
agent = Agently.create_agent()
agent.use_audio(audio)

# 在异步函数中：
speech = await agent.async_tts("欢迎参加会议。", voice=os.getenv("AUDIO_VOICE"))
transcript = await agent.async_stt(AudioInput(speech.data))
print(transcript.text)
```

同步对应方法为 `tts()` / `stt()`。独立对象也可直接调用，不依赖 Agent。
每次调用产生新请求，不是缓存读取。STT 接受文件路径或 `AudioInput`；非 WAV 数据应指定
真实 filename/content_type。只读取文件，不隐式录音、播放、下载 URL、转码或写文件。

`SpeechOptions` 明确定义格式、速度、语言、指令；`TranscriptionOptions` 定义语言和提示。
两者允许 extra 传递供应商参数，但不得覆盖 model/input/file/stream 等核心字段。
参数是否有效仍取决于驱动和模型。默认 120 秒是 HTTP 操作空闲超时，不是整个任务墙钟；
无自动重试、备用模型或音频补全行为。

## 持续消费与输出 auto break

基础 `tts/stt` 不变。以下四个方法可从独立 audio 对象或挂载后的 Agent 直接使用。
**auto break 区分输出形式，不区分输入是否已经分段。**

| 方法 | 输入 | 迭代输出 |
|---|---|---|
| stream_tts | str、非阻塞 Iterable[str] 或 AsyncIterable[str] | 连续的无文件头 PCM bytes |
| stream_tts_with_auto_break | 同上，共用内部文本组块 | 每段完整 SpeechResult 音频，可独立使用 |
| stream_stt | AsyncIterable[bytes]，显式 PCMFormat | 每个处理窗口的 TranscriptBlock |
| stream_stt_with_auto_break | 同上 | 识别后按文字句末交付 TranscriptSegment |

```python
from agently import PCMFormat, TextSegmentOptions, TranscriptionStreamOptions

# text_chunks 为调用方提供的文本碎片流；两个 TTS 模式共用此组块规则。
segments = TextSegmentOptions(expect_chars=300, tolerance_ratio=0.1, grace_chars=100)
async with agent.stream_tts(text_chunks, segments=segments) as stream:
    fmt = stream.audio_format  # 非空输入进入上下文时已就绪；空输入为 None
    async for pcm in stream:
        await pcm_sink.write(pcm)  # 使用 fmt 配置的应用消费端，不是 WAV 文件

async with agent.stream_tts_with_auto_break(fresh_text_chunks, segments=segments) as stream:
    async for speech in stream:
        await segment_sink.write(speech.data, speech.media_type)  # 每项一个完整音频段

# pcm_chunks 是显式格式的持续音频源。不要复用已耗尽的一次性 iterator。
async with agent.stream_stt_with_auto_break(
    pcm_chunks, audio_format=PCMFormat(sample_rate=16000),
    stream_options=TranscriptionStreamOptions(window_seconds=5, max_pending_chars=1000),
) as stream:
    async for segment in stream:
        print(segment.text, segment.reason, segment.first_block, segment.last_block)
```

TTS 在期望长度的比例区间内优先选换行/段落，其次句末，最后逗号，同级选最靠右边界。
无合适边界再读100字符宽限；仍有更早标点则使用它，完全无边界才硬切。
输入结束交付短尾，不把暂时无 token 当作结束。默认300字符只是可调工程值，不是模型最优长度。
任意输入分包不改变处理段；`segmenter: TextSegmenter` 可替换每流边界策略。
`max_input_chars` 默认65536，限制单个输入项；阻塞采集请自行适配为异步来源。

普通 TTS 当前只接收 PCM s16le WAV 或声明 `audio_format` 的原始PCM。
解析 WAV 容器后输出采样，不是去掉固定44字节；首段确定格式，后续不匹配即报错，
不隐式重采样。需要固定格式可传 `audio_format=PCMFormat(...)` 作校验。
`chunk_bytes` 默认8192，按完整采样帧输出。压缩格式请选择auto break，返回各自完整文件；
auto break暂不接受无自描述格式的裸PCM。连续PCM不能当作WAV文件，多个WAV也不能直接拼字节。
每段基础TTS完成后才交付该段，首段有等待时间；不保证韵律跨段一致或播放器永不卡顿。

STT 按采样帧聚合，默认每5秒调用一次基础识别，EOF处理剩余完整帧；半帧报错，不补零。
默认 `max_input_bytes=1048576` 限制单个输入包和处理窗口，`max_transcript_chars=65536`
限制单次转录；超限明确失败。普通块包含text/index/model/language和按采样计算的起止秒数。
`TranscriptResult.duration` 仍是提供方原始字段：oMLX当前返回识别处理耗时，不能当录音时长。

STT auto break消费**定稿转录**，跨块缓冲后按文字标点断句，不按VAD停顿、音频包或SSE事件断句。
`reason` 为 `sentence_end`、`limit` 或 `input_end`：无标点超过上限或EOF，交付原文余段，
不补造句号、不追加模型请求。来源块范围不是精准句子时间戳。中英文句末规则不是通用语义分句器；
跨ASCII字母/数字块边界补显示空格，不修复被识别窗口切开的单词。原始块保持不变。
模型自行添加的标点会影响分句，短窗识别也可能漏词/重复；框架不靠关键词去重修正文意。

四种流都必须 `async with`。按下游消费推进，不后台无限预取；每流最多一个模型请求。
提前关闭/取消/异常不合成未交付尾部，不自动重试已播放内容；已输出前缀不是完整成功。
只关闭本流资源，不接管共享麦克风。真实采集超速需由输入适配器报告溢出或显式应用策略，
背压不会让现实讲话暂停。框架分段缓冲有界，不代表第三方驱动整段响应分配也被框架限制。
输入流是单消费者、一次性的；无默认保存/重放/全双工/录音/播放。

不要把Agent的thinking、工具事件或可被validate/retry替换的临时文字自动播报。
需要最终结果保证时先取得最终文本；低延迟播报不可撤回文字须由调用方明确接受。

## 提供方原生输出流

`audio.supported_operations` 表示框架组合能力；`audio.driver.supported_operations` 表示原生驱动能力，
都不是服务健康证明。OpenAICompatible有基础tts/stt即可使用上述组合接口，不要求原生双向流。
OMLX额外支持原生WAV输出和完整文件上传后的transcript.text.delta/done SSE；
高级调用通过 `audio.driver.stream_tts(SpeechRequest(...))` /
`audio.driver.stream_stt(TranscriptionRequest(...))` 显式使用。

原生音频包不等于完整文件；原生STT的done是完整转录，替换而非追加delta；缺done报错。
原生持续输入 `driver.stream_stt_input(...)` 仍是自定义驱动接缝，内置尚未适配。
它与框架按窗口持续调用stt是两种交互机制，不因本轮组合支持而宣称已实现原生实时ASR。

## 插件替换与 Execution 依赖

驱动实现 `AudioModelRequester`，构造参数为 `AudioConnection`；通过
`Agently.plugin_manager.register("AudioModelRequester", Driver, activate=False)` 注册。
也可直接 `AudioModelRequest(driver_instance, tts_model=..., stt_model=...)`。
驱动可以替换整个传输机制，不限于 HTTP 参数。`use_audio` 还接受整个 `AudioCapability`
协议的替换实现。注册不等于 Agent 挂载；未挂载就调用会报错。
`use_audio(None)` 解除未来访问，不代应用关闭共享对象。

Execution 插件声明 `required_agent_capabilities = ("audio",)`，工厂在构造前检查依赖。
共享 Execution 实现在创建时绑定对象，生产方通过 `require_agent_capability("audio")`
取用。动态依赖也通过此方法在使用前绑定；已绑定的引用不随 Agent 后续替换而漂移。
子 Execution 声明自己的依赖，创建子执行不会授予权限。完整替换 Execution 的插件也必须
履行该合同。依赖存在不代表授权、模型支持、健康状态或已经实际调用。
带额外能力绑定的 save/load 当前明确不支持，不能序列化客户端或假称自动重绑与重放安全。

本切片没有增加文本模型 token 事件，也未把音频纳入 Execution 的文本请求预算。
应用需要对音频设置自己的并发准入和任务总时限；不宣称费用统一核算、取消回滚或持久恢复。

可运行示例：[四种持续输出](../../../examples/audio/continuous_audio.py)、
[基础与原生流回环](../../../examples/audio/tts_stt_roundtrip.py)。

## 可选入口语音检测（4.1.4.9 开发中）

`TranscriptionOptions.input_options` 默认是 `None`，保持原有 STT 行为。
显式开启后，检测器先判断语音概率，框架保留语音及前后保护采样，再提交原始 STT。
**底噪是否属于语音，与语音持续多久是两项独立判断。** 默认最短语音时长为 0，
保留被检测为语音的“对”“不”“嗯”等短回答；不会通过删除短回答来过滤话筒底噪。
VAD 有误检/漏检，不保证识别所有咳嗽、音乐、噪声或很轻的语音，也不判断文字是否有意义。

```python
from agently import AudioInputEvent, AudioInputOptions, TranscriptionOptions, PCMFormat
from agently.integrations.silero import SileroVAD

# 显式安装可选依赖：pip install 'numpy>=1.24,<3' 'onnxruntime>=1.16,<2'
# 从可信 Silero 上游获取 v5/v6 的 silero_vad.onnx，配置实际本机路径。
# 构造时同步加载文件，建议在进入对延迟敏感的异步循环之前完成。
detector = SileroVAD(os.environ["SILERO_VAD_MODEL_PATH"])

async def on_audio(event: AudioInputEvent) -> None:
    if event.kind == "pause":
        print("声学停顿；此前原始转写已完成至块", event.last_block)
    elif event.kind == "silence":
        print("静默时间", event.start_seconds, event.end_seconds)

options = TranscriptionOptions(input_options=AudioInputOptions(
    detector=detector, threshold=0.5, min_speech_seconds=0,
    end_silence_seconds=0.5, pre_speech_seconds=0.15,
    post_speech_seconds=0.15, max_segment_seconds=15, on_event=on_audio,
))
# 在异步函数中；也可使用已绑定音频的 agent.stream_stt。
async with audio.stream_stt(pcm_chunks, audio_format=PCMFormat(), options=options) as stream:
    async for block in stream:
        print(block.index, block.speech_index, block.start_seconds, block.end_seconds,
              block.reason, block.text)

# 单次 PCM s16le WAV 使用完全相同的配置：
result = await audio.async_stt("recording.wav", options=options)
# 同步脚本使用 audio.stt(..., options=options)，Agent 对应入口也支持。
```

普通 `import agently` 不加载 VAD 依赖。显式导入 Silero 适配器需要 NumPy/ONNX Runtime；
缺依赖时给出安装错误，不自动安装。模型文件由开发者提供，框架不下载，不依赖 Torch。
`SileroVAD` 支持单声道 8/16 kHz s16le，可共享检测器对象，每次调用的状态独立。
通过 `SpeechDetector.open(audio_format)` 返回异步上下文中的 `SpeechDetectionSession`
可替换实现；session 声明 `frame_samples`，`score(pcm)` 异步返回有限 `[0,1]` 概率。
调用方传入完整采样帧；EOF 可短于一个检测帧。后端若补齐分析窗口，只能补分析副本。

| 参数 | 默认值 | 含义与关系 |
|---|---|---|
| threshold | 0.5 | 概率大于等于此值视为语音；不另加音量硬门槛 |
| min_speech_seconds | 0 | 累计语音概率帧的最短时长；调高可能误删真实短回答 |
| end_silence_seconds | 0.5 | 连续非语音的采样时长，不是网络无包或墙钟超时 |
| pre_speech_seconds / post_speech_seconds | 0.15 / 0.15 | 保留真实前后采样；后保护不得超过结束静默；不重复已提交采样 |
| max_segment_seconds | 15 | 包含保护的单次 STT 上限；连续发言硬切，不伪造 pause |
| max_buffer_bytes | 1048576 | 最大段加两个检测帧暂存必须装入此界限；不代表进程/驱动总内存上限 |
| max_file_bytes | 33554432 | 仅开启处理的单次文件大小上限 |
| on_event | None | 可选异步回调；等待完成，异常会终止流，不建后台事件队列 |

时长按采样帧向上取整；检测分辨率由后端声明（Silero 为 32 ms）。最大段必须大于
前保护、最短语音和结束静默之和，并容纳检测帧。未达到显式最短语音门槛的候选在
pause/EOF/满段边界发 `rejected`，不无限等待。最大段含保护，边界处可能额外产生短保护块；
调小它可能增加请求次数、延迟或截断词语。比较效果时同时记录提交秒数和请求数。

开启检测时 `max_segment_seconds` 负责分块，`TranscriptionStreamOptions.window_seconds`
不参与；`max_input_bytes` 仍限制每个源包，`max_transcript_chars` 限制每次转写结果，
`max_pending_chars` 仍只限制输出文字断句。缓冲是有界 PCM，不默认保存录音。

`TranscriptBlock` 的时间始终是原始采样时间，过滤后的静默间隙仍保留；保护采样也计入范围。
新 `speech_index` 关联同一语音的多个块，`reason` 为 `pause`、`limit`、`input_end`；
未开启的历史窗口仍为 `speech_index=None, reason="window"`。它们不是词级时间戳。

事件顺序：`speech_start`（候选开始）→ 一个或多个 `transcript`（原始块就绪）→
`pause`（已观察结束静默，且对应最终转写已完成）。`last_block` 指向完成的最后块。
回调发生在拉取消费中：`transcript` 在原始块 yield 前通知，`pause` 随后续拉取推进。
需要基于停顿处理文本时，可消费 `transcript` 回调中的 `event.transcript`，再处理 `pause`。
停止拉取不会有后台任务继续推进。`silence` 合并通知静默范围，`input_end` 仅在正常 EOF
及合法尾部处理完成后发送；EOF 不伪装为声学停顿。取消或任何错误不发完成、不冲刷尾部，
失败流不可恢复；新调用需提供新输入。回调应及时返回，不重入同一个流。

`stream_stt_with_auto_break` 仍按识别后的文字标点断句。其 `pause` 只保证原始块就绪，
不保证无标点的 `TranscriptSegment` 已交付。音频分段与业务文字整理频次独立；
框架不实现语气词清理、摘要、思想结束判断或业务触发调度。

单次开启时只接受完整 PCM s16le WAV；MP3/AAC/Opus 等需调用方先解码为显式 PCM 流。
多块单次结果以换行连接原始块文本，`duration=None`；全静默返回空文字并跳过 STT。
需要各块原始文本及位置时使用流或事件。未开启时原驱动的单次格式能力保持不变。
流中裸 bytes 必须遵守声明的固定格式；无法从任意裸字节可靠识别谎报格式或采样率切换。
EOF 半采样帧报错，不补零；缺包不等于静默。拉取背压无法暂停真实讲话，采集适配器应
自行报告 overflow。CPU 帧推理取消需等待已开始的本地推理收尾，不等于服务端取消确认。

参考：[Silero VAD](https://github.com/snakers4/silero-vad)、
[faster-whisper 的 VAD 实现](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/vad.py)。
可运行用法见 `examples/audio/stt_input.py`。

输入预处理由 `AudioModelRequest` 负责。直接调用原生 driver 时，非空
`input_options` 会被拒绝；第三方 `AudioCapability` 若接受此选项，需自行实现
该合同。Agent 转发本身不会给自定义能力补上预处理。
