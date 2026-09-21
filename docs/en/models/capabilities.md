# Independent model uses and multimodal Agent tasks

The development API configures each model use independently:

```python
agent.set_settings("llm", {"provider": "OpenAICompatible", "model": "main-model", **llm_connection})
agent.set_settings("vlm", {"provider": "OpenAICompatible", "model": "vision-model", **vision_connection})
agent.set_settings("embeddings", {"provider": "OpenAICompatible", "model": "embedding-model", **embedding_connection})
agent.set_settings("stt", {"provider": "OMLX", "model": "speech-recognition-model", **audio_connection})
agent.set_settings("tts", {"provider": "OMLX", "model": "speech-model", **audio_connection})
```

A connection includes its own base_url, api_key and optional request_options.
Even two uses of one provider have independent credentials, headers and options.
Use `ModelUseSettings` for typed configuration. Alternatively, configure a use
as `{"model_key": "pool-alias"}` to resolve the existing model_pool/model_profiles/
api_key_pools. Do not combine model_key and an inline profile. Explicit request
model_key takes precedence over llm. On the 4.1 compatibility line, the existing
provider namespaces remain a fallback when llm is absent. An invalid configured
profile raises an error; provider failures never select another model silently.

## Images

```python
execution = (
    agent.image("note.png", question="Read the note carefully.")
    .input("Where is Hammond?")
    .output({"location": str, "evidence": str})
)
data = await execution.async_get_data()
```

`question` is local to the images in that call; input is the whole task, and
output is the final result contract. Repeated image calls append, retaining
local questions and order. The keyword forms file/url/files/urls remain valid.
Explicit attachment replaces the attachment list and its image routing metadata.

| Image mode | Ordinary Agent execution |
|---|---|
| vlm (default) | Both profiles: VLM evidence -> LLM; one configured profile: that model directly |
| llm | Send the original image to llm, bypassing the dedicated VLM |
| ocr | Extract text with the OCR provider, then give the evidence to llm |

An LLM receiving images must support vision. Set `vision=False` to reject a
known unsupported profile before dispatch; unknown support is decided by the
service, never guessed from a model name. A text-only task creates no visual
preprocessing stage. With only vlm configured, it can also answer text tasks.
The pipeline preserves task-relevant input/info/instruct, local image questions,
visible text, spatial/identity evidence and uncertainty. The final producer
respects output; intermediate evidence has its own schema.

`execution.vlm_only(True)` requires a configured VLM and makes it the final
producer even when llm is configured. Call it before execution starts. False
restores normal routing. It conflicts with image modes llm and ocr.

`await execution.async_to_text()` (sync: to_text) selects a direct image
operation before starting. It does not add an LLM continuation. It respects
input, question and output; structured data is validated and projected to text,
and get_data reads the same result without repeating inference. The ordinary
get_text reader does not change routing. Without a question/task, direct visual
processing describes the image and readable text, marking uncertainty.

OCR is extraction, not arbitrary visual reasoning. Configure, for example,
`ocr={"provider":"MistralOCR", "model":"mistral-ocr-latest", ...}`.
The adapter uses the [Mistral OCR endpoint](https://docs.mistral.ai/api/endpoint/ocr)
and returns page markdown. Direct mode=ocr/to_text accepts extraction only;
question/input/output processing requires the ordinary OCR -> LLM pipeline.
Missing OCR configuration errors before dispatch. Multiple OCR images use
separate atomic requests; no hidden VLM fallback is used.

## Audio input and delivery

Direct stt/tts remain independent AudioModelRequest operations. Their profiles
can select different providers/models. An explicit use_audio binding takes
precedence; existing bound Execution references stay stable.

```python
execution = agent.input(file="question.wav", type="audio").instruct("Answer briefly.")
speech = await execution.async_say()  # STT -> Agent processing -> TTS
text = await execution.async_get_text()  # Same completed text result
```

Finite local files and AudioInput are supported; there is no implicit microphone,
URL download or audio conversion. `input({"type":"audio", "file":"business-id"})`
remains ordinary business data. Use keyword file/type for the audio declaration.
A later input can state the overall task; the transcript remains input evidence.
Audio declarations belong to one execution, not always=True defaults.

say/async_say return SpeechResult (or None for empty text). `scope="final"` is
the default; `scope="all"` adds public intermediate natural-language output.
Raw provider deltas, reasoning, tool arguments and JSON fragments are excluded.
The final text follows the normal Execution text projection, including JSON for
an ordinary structured result. Same-argument say calls reuse the audio result.
A failed audio consumer does not erase the already completed text result.

For audio segments during execution:

```python
async with execution.stream_say(scope="all") as segments:
    async for speech in segments:
        await your_audio_sink(speech.data, speech.media_type)
```

The scope selects content; stream_say selects delivery as complete SpeechResult
segments. Existing TTS segmentation and backpressure apply. The stream is a
single consumption, not implicit audio replay. Closing a stream that started
this execution cancels its unfinished work. Public intermediate statements are
provisional and cannot be retracted after delivery; use final for accepted-only
speech. No entrypoint implicitly plays audio or writes files.

Audio profiles support base_url, api_key, headers, client_options and timeout.read.
request_options supplies speech voice/format/speed/language/instructions or STT
language/prompt and provider extra options. Per-call typed options override those
defaults. Audio full_url and key failover are rejected, never silently ignored.

## Embeddings and execution boundaries

`await agent.async_embed("text")` and `agent.embed(["first", "second"])` return
list[list[float]], including one row for one input. Results retain input order;
missing/duplicate indexes, inconsistent dimensions and non-finite values fail.
The operation never changes llm or migrates an existing vector index.

Agent tasks always run through Execution. Media stages use TriggerFlow and atomic
requesters; successful input stages are not replayed by later-stage retries.
Their retries reduce the remaining final-production retry allowance. Cancellation
stops owned work. Final validation, SystemOne and Actions retain their existing
owners. `get_meta()["media"]` records media stages and retries; text model request
lineage remains available. Audio metadata is not a claim of unified token billing.

Examples: [vision](../../../examples/model_capabilities/vision.py),
[audio task](../../../examples/model_capabilities/audio_task.py),
[embeddings](../../../examples/model_capabilities/embeddings.py).

Pending audio declarations and bound audio capabilities do not support built-in
save/load without an explicit rebinding contract. Rework reuses completed input
evidence and speech caching is per revision; direct OCR cannot apply rework
feedback. Direct OCR also cannot satisfy an AgentTask/Action route contract.
