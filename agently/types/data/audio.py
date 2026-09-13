"""Typed audio inputs, options and results; independent of text Prompt data."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from agently.types.plugins.AudioModelRequester import SpeechDetector

AudioFormat = Literal["wav", "mp3", "opus", "aac", "flac", "pcm"]
AudioOperation = Literal[
    "tts", "stt", "stream_tts", "stream_stt", "stream_stt_input",
    "stream_tts_with_auto_break", "stream_stt_with_auto_break",
]
TextSource = str | Iterable[str] | AsyncIterable[str]


class AudioCapabilityError(RuntimeError):
    """A requested audio operation is not available on the selected driver."""


class AudioProtocolError(RuntimeError):
    """The provider did not deliver a valid result or required terminal event."""


@dataclass(frozen=True)
class AudioConnection:
    base_url: str
    api_key: str = field(default="", repr=False)
    timeout: float = 120.0


@dataclass(frozen=True)
class AudioInput:
    data: bytes = field(repr=False)
    filename: str = "audio.wav"
    content_type: str = "audio/wav"


@dataclass(frozen=True)
class SpeechOptions:
    response_format: AudioFormat = "wav"
    speed: float = 1.0
    language: str | None = None
    instructions: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptionOptions:
    """Provider options plus explicit, framework-owned input preprocessing."""

    language: str | None = None
    prompt: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)
    input_options: AudioInputOptions | None = None


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    model: str
    voice: str | None = None
    options: SpeechOptions = field(default_factory=SpeechOptions)


@dataclass(frozen=True)
class TranscriptionRequest:
    audio: AudioInput
    model: str
    options: TranscriptionOptions = field(default_factory=TranscriptionOptions)


@dataclass(frozen=True)
class SpeechResult:
    data: bytes = field(repr=False)
    media_type: str
    model: str


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    model: str
    language: str | None = None
    duration: float | None = None


@dataclass(frozen=True)
class TranscriptEvent:
    """Delta text is incremental; done text is the authoritative full transcript."""

    kind: Literal["delta", "done"]
    text: str


@dataclass(frozen=True)
class PCMFormat:
    """Raw input frames; no codec conversion is implied by this declaration."""

    sample_rate: int = 16000
    channels: int = 1
    encoding: Literal["s16le"] = "s16le"


@dataclass(frozen=True)
class TextSegmentOptions:
    """Code-point lengths, not tokens. Input yield boundaries do not define segments."""

    expect_chars: int = 300
    tolerance_ratio: float = 0.1
    grace_chars: int = 100
    max_input_chars: int = 65536


@dataclass(frozen=True)
class TranscriptionStreamOptions:
    window_seconds: float = 5.0
    max_input_bytes: int = 1048576
    max_transcript_chars: int = 65536
    max_pending_chars: int = 1000


@dataclass(frozen=True)
class TranscriptBlock:
    """One finalized window. Times come from input frames, not provider duration."""

    text: str
    index: int
    start_seconds: float
    end_seconds: float
    model: str
    language: str | None = None
    speech_index: int | None = None
    reason: Literal["window", "pause", "limit", "input_end"] = "window"


@dataclass(frozen=True)
class TranscriptSegment:
    """Punctuation-delimited text or an explicitly marked limit/EOF remainder."""

    text: str
    reason: Literal["sentence_end", "limit", "input_end"]
    first_block: int
    last_block: int


class PCMStream(Protocol):
    """Fixed-format headerless PCM. Empty input has no inferred format."""

    @property
    def audio_format(self) -> PCMFormat | None: ...

    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def __anext__(self) -> bytes: ...


@dataclass(frozen=True)
class AudioInputEvent:
    """Input-sample times, not wall time. Pause follows finalized block callbacks.

    STT text auto-break may still hold punctuation-free text at a pause.
    No event implies that a sentence or thought is complete.
    """

    kind: Literal["speech_start", "transcript", "pause", "rejected", "silence", "input_end"]
    start_seconds: float
    end_seconds: float
    speech_index: int | None = None
    last_block: int | None = None
    transcript: TranscriptBlock | None = None


AudioInputHandler = Callable[[AudioInputEvent], Awaitable[None]]


@dataclass(frozen=True)
class AudioInputOptions:
    """Opt-in acoustic segmentation; detector state is fresh for each call.

    Durations are input-sample seconds. min_speech_seconds=0 keeps short
    detected replies. max_segment_seconds replaces fixed STT windowing.
    Explicitly increasing min_speech_seconds can remove meaningful speech.
    """

    detector: SpeechDetector
    threshold: float = 0.5
    min_speech_seconds: float = 0.0
    end_silence_seconds: float = 0.5
    pre_speech_seconds: float = 0.15
    post_speech_seconds: float = 0.15
    max_segment_seconds: float = 15.0
    max_buffer_bytes: int = 1048576
    max_file_bytes: int = 33554432
    on_event: AudioInputHandler | None = None
