# Audio requests

TTS and STT use an independent `AudioModelRequest`, not the text `ModelRequest`
Prompt chain. Audio models and credentials are explicit; Agent text settings,
session history, output schemas and `auto_continue()` do not enter audio calls.

```python
import os
from agently import Agently, AudioInput, SpeechOptions

audio = Agently.create_audio_request(
    driver="OMLX",  # OpenAICompatible supports the complete-file HTTP endpoints.
    base_url=os.environ["AUDIO_BASE_URL"],
    api_key=os.getenv("AUDIO_API_KEY", ""),
    tts_model=os.environ["AUDIO_TTS_MODEL"],
    stt_model=os.environ["AUDIO_STT_MODEL"],
)
agent = Agently.create_agent()
agent.use_audio(audio)

# Inside an async function:
speech = await agent.async_tts("Welcome to the meeting.", voice=os.getenv("AUDIO_VOICE"))
transcript = await agent.async_stt(AudioInput(speech.data))
print(transcript.text)
```

`tts()` / `stt()` are synchronous counterparts. Direct `audio.async_tts()` and
`audio.async_stt()` work without an Agent. Each call is a fresh request, not a
cached result reader. STT also accepts a local file path. `AudioInput` carries
bytes, filename and media type; declare the actual type for non-WAV input.
Files are read, never rewritten. No implicit recording, playback, URL download
or audio conversion is performed.

`SpeechOptions` provides response_format, speed, language, instructions and
provider-specific extra options; `TranscriptionOptions` provides language,
prompt and extra. Provider/model support still applies. Reserved request fields
cannot be replaced through extra. Default timeout is 120 seconds of HTTP
operation inactivity, not a whole workflow deadline. There is no automatic retry
or fallback model.

## Continuous consumption and output auto break

The basic tts/stt calls are unchanged. All four methods are available on the
standalone audio object and on an explicitly bound Agent. **Auto break selects
the output presentation, not whether input is already segmented.**

| Method | Input | Yielded output |
|---|---|---|
| stream_tts | str, nonblocking Iterable[str], or AsyncIterable[str] | Continuous headerless PCM bytes |
| stream_tts_with_auto_break | Same text source and internal segmentation | Independent complete SpeechResult audio segments |
| stream_stt | AsyncIterable[bytes] with explicit PCMFormat | Finalized TranscriptBlock per audio window |
| stream_stt_with_auto_break | Same PCM source | TranscriptSegment split after recognition at text sentence punctuation |

```python
from agently import PCMFormat, TextSegmentOptions, TranscriptionStreamOptions

segments = TextSegmentOptions(expect_chars=300, tolerance_ratio=0.1, grace_chars=100)
async with agent.stream_tts(text_chunks, segments=segments) as stream:
    fmt = stream.audio_format  # Ready on entry for nonempty input; None for empty input
    async for pcm in stream:
        await pcm_sink.write(pcm)  # Application sink configured with fmt; not a WAV file

async with agent.stream_tts_with_auto_break(fresh_text_chunks, segments=segments) as stream:
    async for speech in stream:
        await segment_sink.write(speech.data, speech.media_type)

async with agent.stream_stt_with_auto_break(
    pcm_chunks, audio_format=PCMFormat(sample_rate=16000),
    stream_options=TranscriptionStreamOptions(window_seconds=5, max_pending_chars=1000),
) as stream:
    async for segment in stream:
        print(segment.text, segment.reason, segment.first_block, segment.last_block)
```

Text segmentation prefers newlines/paragraphs, then sentence endings, then commas
inside the expected-length tolerance interval; the rightmost boundary wins
within a priority. With no candidate, read the grace interval, then use an earlier
boundary if available; hard-cut only when none exists. EOF flushes a short tail;
temporary lack of tokens is not EOF. Lengths are Unicode code points, not tokens.
The 300-character default is tunable, not a model-optimal claim. Input packet
boundaries do not change processing segments. Supply a fresh typed
`TextSegmenter` to replace boundary selection; `max_input_chars=65536` bounds a
single source item. Adapt blocking capture to an async source explicitly.

Continuous TTS currently accepts uncompressed s16le PCM WAV, or raw PCM with an
explicit `audio_format`. It parses actual WAV chunks, not a fixed 44-byte header.
The first segment locks the format; mismatches fail without implicit resampling.
An explicit `audio_format=PCMFormat(...)` requires that exact output format.
`chunk_bytes=8192` is rounded down to complete frames. Auto break supports
independent encoded results such as WAV/MP3 according to the base driver; it
rejects raw PCM without self-describing metadata. PCM is not a WAV file, and
concatenated WAV files are not one continuous audio file. Each base TTS finishes
a segment before delivery: initial latency, cross-segment prosody and continuous
playback throughput are not guaranteed.

STT accumulates sample frames into configurable windows (default 5 seconds).
EOF submits remaining complete frames and rejects incomplete frames without padding.
`max_input_bytes=1048576` bounds source items and windows; `max_transcript_chars=65536`
bounds each transcription. Blocks include text/index/model/language and
sample-derived start/end seconds. Base `TranscriptResult.duration` remains a
provider-origin field: oMLX currently reports processing time, not recording duration.

STT auto break consumes finalized block text, not audio pauses, packet boundaries
or provisional SSE deltas. Reasons are `sentence_end`, `limit`, and `input_end`.
When no punctuation arrives, the pending-length limit/EOF delivers a labelled
remainder without inventing punctuation or making another model request.
Source block ranges are not word-aligned sentence timestamps. The default
punctuation rules are not a universal semantic segmenter. An ASCII alphanumeric
block join adds a display space; it does not reconstruct split words, and
original blocks remain unchanged. Windowed recognition may lose/repeat words
or insert punctuation: the framework does not semantically deduplicate transcripts.

Use `async with`. Streams are pull-driven, one model request at a time, with no
unbounded prefetch queue. Errors/cancellation/early close do not synthesize pending
tails or retry already delivered speech. Already yielded prefixes are not full
success. The stream owns its resources, not a shared microphone. A realtime
capture adapter must report overflow or use an explicit application policy:
backpressure cannot pause a person speaking. Bounded framing does not bound all
allocations inside a third-party driver's complete response implementation.
No implicit recording, playback, full duplex, durable resume or replay is promised.

Do not automatically speak Agent thinking, tool events or text that validation
or retries can replace. Await final text for final-result guarantees; callers
must explicitly accept irreversible effects when choosing low-latency playback.

## Provider-native streams

`audio.supported_operations` describes composed operations;
`audio.driver.supported_operations` describes provider-native support. Neither
proves health. OpenAICompatible base tts/stt is sufficient for composed streams;
native duplex support is not required. OMLX additionally supports WAV output
streaming and uploaded-file transcript.text.delta/done SSE. Advanced native
access is explicit through `audio.driver.stream_tts(SpeechRequest(...))` and
`audio.driver.stream_stt(TranscriptionRequest(...))`.

Native bytes are transport chunks, not independent audio files. Native STT
done replaces accumulated deltas; EOF without done fails.
`driver.stream_stt_input(...)` remains a custom-driver native-input seam;
built-ins do not implement it. Windowed continuous consumption does not imply
a native realtime-ASR session has been implemented.

## Replacement and Execution dependencies

Register an `AudioModelRequester` class through
`Agently.plugin_manager.register("AudioModelRequester", Driver, activate=False)`;
its constructor receives `AudioConnection`. Or instantiate
`AudioModelRequest(driver_instance, tts_model=..., stt_model=...)`. A driver owns
its complete transport mechanism and scoped cleanup, not just HTTP parameters.
`agent.use_audio()` also accepts another complete `AudioCapability` implementation.

Registration does not mount audio. Missing `agent.audio`, `.tts()` or `.stt()`
raises a capability error. `use_audio(None)` removes future access without
closing an externally owned shared object.

Execution plugins can declare `required_agent_capabilities = ("audio",)`.
The factory checks presence before construction; the shared Execution implementation
captures these objects at construction. Producers obtain them through
`execution.require_agent_capability("audio")`, which also binds dynamic dependencies
before use. Replacing the Agent binding does not change an already captured object.
Child executions declare their own requirements; creating a child does not grant
permissions. A wholly custom Execution must implement the same binding contract.
Presence is not authorization, model support, health or proof of an actual call.
Snapshots with extra capability bindings currently fail explicitly: live clients
are not serialized, and no automatic rebinding/replay guarantee is claimed.

Audio calls do not currently emit text-model token events or enter Execution text
model-request budgets. Use application-owned deadlines/admission for audio work;
do not infer cost accounting, cancellation rollback or durable audio resume.

See [four continuous output modes](../../../examples/audio/continuous_audio.py) and
the [base/native round-trip example](../../../examples/audio/tts_stt_roundtrip.py).

## Optional input speech detection (4.1.4.9 development)

`TranscriptionOptions.input_options=None` preserves the existing STT path.
When explicitly enabled, a detector estimates speech probability; the framework
submits detected speech plus original surrounding samples. **Speech detection
and minimum speech duration are separate controls.** The default minimum is zero
so short detected replies remain eligible. Do not raise the minimum merely to
hide microphone noise false positives. VAD is acoustic, not a semantic relevance
filter; it cannot guarantee rejection of every cough, music or noise, or recall
of every quiet reply.

```python
from agently import AudioInputEvent, AudioInputOptions, TranscriptionOptions, PCMFormat
from agently.integrations.silero import SileroVAD

# Explicit optional installation:
# pip install 'numpy>=1.24,<3' 'onnxruntime>=1.16,<2'
# Obtain the trusted upstream v5/v6 silero_vad.onnx file yourself.
# Loading is synchronous; construct before entering a latency-sensitive loop.
detector = SileroVAD(os.environ["SILERO_VAD_MODEL_PATH"])

async def on_audio(event: AudioInputEvent) -> None:
    if event.kind == "pause":
        print("Acoustic pause; finalized through raw block", event.last_block)
    elif event.kind == "silence":
        print("Silence", event.start_seconds, event.end_seconds)

options = TranscriptionOptions(input_options=AudioInputOptions(
    detector=detector, threshold=0.5, min_speech_seconds=0,
    end_silence_seconds=0.5, pre_speech_seconds=0.15,
    post_speech_seconds=0.15, max_segment_seconds=15, on_event=on_audio,
))
# In an async function; a bound agent exposes the same call.
async with audio.stream_stt(pcm_chunks, audio_format=PCMFormat(), options=options) as stream:
    async for block in stream:
        print(block.index, block.speech_index, block.start_seconds, block.end_seconds,
              block.reason, block.text)

result = await audio.async_stt("recording.wav", options=options)
# Use audio.stt(..., options=options) in synchronous scripts.
```

Ordinary `import agently` loads no VAD dependencies. Importing the optional Silero
adapter requires NumPy and ONNX Runtime; missing dependencies produce an explicit
install error, with no automatic installation. The framework never downloads a
model and does not require Torch. Silero supports mono s16le at 8/16 kHz. A shared
`SileroVAD` has fresh recurrent state per call. Replace it through
`SpeechDetector.open(audio_format)`, an async context returning a
`SpeechDetectionSession` with `frame_samples` and async `score(pcm) -> float`.
Scores must be finite in `[0,1]`. Inputs contain complete sample frames, with a
possibly shorter final analysis window. Pad only an analysis copy if needed.

| Option | Default | Meaning |
|---|---|---|
| threshold | 0.5 | Probability at least this value is speech; no extra volume gate |
| min_speech_seconds | 0 | Accumulated speech-probability frame duration; increasing it can remove meaningful short replies |
| end_silence_seconds | 0.5 | Consecutive non-speech sample duration, not a wall-clock or missing-packet timeout |
| pre_speech_seconds / post_speech_seconds | 0.15 / 0.15 | Original surrounding samples; post-roll cannot exceed end silence; never repeat already submitted samples |
| max_segment_seconds | 15 | Per-request duration including protection; continuous speech is hard-cut without a false pause |
| max_buffer_bytes | 1048576 | Maximum segment plus two detector frames must fit; not a total process/provider memory bound |
| max_file_bytes | 33554432 | Complete-file byte limit when preprocessing is enabled |
| on_event | None | Awaited async callback; failures terminate, with no background event queue |

Durations round up to sample frames. Detector resolution affects observations
(Silero: 32 ms). Maximum segment duration must exceed pre-roll plus minimum
speech plus end silence and accommodate a detector frame. Candidates still
below an explicit minimum are rejected at pause/EOF/full-buffer boundaries,
never buffered indefinitely. Protection at a maximum-size boundary can produce
an additional short request. Smaller segments can increase request count,
latency or split words; compare submitted seconds and request counts separately.

When enabled, `max_segment_seconds` owns segmentation and
`TranscriptionStreamOptions.window_seconds` is unused. `max_input_bytes` still
bounds each source packet, `max_transcript_chars` bounds each response, and
`max_pending_chars` only bounds text auto-break. No recording is saved implicitly.

`TranscriptBlock` times always use original input samples, retaining gaps after
silence filtering and including protection. New `speech_index` associates blocks
from the same speech candidate; `reason` is `pause`, `limit` or `input_end`.
Historical unfiltered blocks retain `speech_index=None, reason="window"`.
These are not word timestamps.

Events are ordered: `speech_start` (candidate observed), one or more `transcript`
callbacks carrying raw finalized blocks, then `pause` after end silence and all
associated transcriptions have completed. `last_block` identifies the completion
barrier. Callbacks run during pull consumption: `transcript` precedes the raw
block yield; `pause` advances with the next pull. Applications needing text at
pauses can consume `event.transcript` and then handle `pause`. No background work
advances an undrained stream. `silence` coalesces silent sample ranges;
`input_end` follows only normal EOF and valid tail processing. EOF is not a pause.
Cancellation/errors do not flush or emit successful completion, and failed
streams cannot resume. Callbacks must return promptly and not re-enter the stream.

`stream_stt_with_auto_break` still uses recognized text punctuation. A pause
ensures raw blocks are ready; punctuation-free `TranscriptSegment` text may
remain pending. Acoustic segmentation and business text-cleanup frequency are
independent. The framework does not remove filler words, summarize, detect
completed thoughts or schedule business processing.

Complete-file preprocessing accepts only PCM s16le WAV; decode compressed
MP3/AAC/Opus explicitly into a PCM stream. Single-file results join raw block
texts with newlines and return `duration=None`; all-silent input returns empty
text with zero STT requests. Use events/streaming for individual raw blocks and
times. Disabled preprocessing preserves the driver's original file-format support.
Raw byte streams must obey their fixed declared format; arbitrary bytes cannot
reliably reveal a false sample-rate declaration or an undeclared format switch.
Incomplete EOF sample frames fail without padding; missing packets are not
silence. Capture adapters must report overflow because backpressure cannot pause
real speech. Cancelling CPU scoring waits for the started local frame to settle;
this is not an acknowledgement of server-side ASR cancellation.

See [Silero VAD](https://github.com/snakers4/silero-vad),
[faster-whisper VAD](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/vad.py),
and `examples/audio/stt_input.py` for a runnable example.

Input preprocessing belongs to `AudioModelRequest`. Native driver methods reject
nonempty `input_options`; a third-party `AudioCapability` must implement this
contract itself when accepting the option. Agent forwarding alone does not add
preprocessing to a custom capability.

See [Independent model uses and multimodal tasks](capabilities.md) for the development API.
