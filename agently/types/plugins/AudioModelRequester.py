"""Replaceable audio interaction contracts, including streaming input."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager
from os import PathLike
from typing import Protocol

from agently.types.data.audio import (
    AudioConnection, AudioInput, AudioOperation, PCMFormat, SpeechOptions,
    SpeechRequest, SpeechResult, TranscriptEvent, TranscriptResult,
    TranscriptionOptions, TranscriptionRequest, PCMStream, TextSource, TextSegmentOptions,
    TranscriptBlock, TranscriptSegment, TranscriptionStreamOptions,
)
from .base import AgentlyPlugin


class TextSegmenter(Protocol):
    """Fresh per-stream boundary strategy: prefix length or None to wait.

    The framework owns accumulation, exact prefix delivery and hard bounds.
    At EOF the remaining text is delivered even when no boundary is selected.
    """

    def cut(self, text: str, *, final: bool) -> int | None: ...


class SpeechDetectionSession(Protocol):
    """One stream's detector state. score receives 1..frame_samples real frames.

    A backend needing full frames may pad its analysis copy at EOF, never
    the audio submitted to STT. score must be cancellation-safe and return
    a finite probability in [0, 1]. Implementations must not block the loop.
    """

    @property
    def frame_samples(self) -> int: ...

    async def score(self, pcm: bytes) -> float: ...


class SpeechDetector(Protocol):
    """Replaceable acoustic scoring; open validates format before input is read.

    Each context owns fresh state and settles its work on exit. Sharing a
    detector across streams must not share recurrent state or close peers.
    """

    def open(self, audio_format: PCMFormat) -> AbstractAsyncContextManager[SpeechDetectionSession]: ...


class AudioModelRequester(AgentlyPlugin, Protocol):
    """Driver owns each transport through completion, cancellation or context exit.

    Framework input_options are consumed by AudioModelRequest before dispatch.
    Native callers must not expect a driver to apply them; reject non-None
    input_options explicitly rather than silently claiming preprocessing.
    """

    name: str

    def __init__(self, connection: AudioConnection): ...

    @property
    def supported_operations(self) -> frozenset[AudioOperation]: ...

    async def tts(self, request: SpeechRequest) -> SpeechResult: ...

    async def stt(self, request: TranscriptionRequest) -> TranscriptResult: ...

    def stream_tts(self, request: SpeechRequest) -> AbstractAsyncContextManager[AsyncIterator[bytes]]: ...

    def stream_stt(self, request: TranscriptionRequest) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]: ...

    def stream_stt_input(
        self, chunks: AsyncIterable[bytes], *, model: str, audio_format: PCMFormat,
        options: TranscriptionOptions,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]: ...


class AudioCapability(Protocol):
    """Agent may bind any complete implementation of this capability, not only the default facade."""

    @property
    def supported_operations(self) -> frozenset[AudioOperation]: ...

    async def async_tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult: ...

    async def async_stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult: ...

    def stream_tts(
        self, text: TextSource, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None, segments: TextSegmentOptions | None = None,
        segmenter: TextSegmenter | None = None, audio_format: PCMFormat | None = None, chunk_bytes: int = 8192,
    ) -> AbstractAsyncContextManager[PCMStream]: ...

    def stream_tts_with_auto_break(
        self, text: TextSource, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None, segments: TextSegmentOptions | None = None,
        segmenter: TextSegmenter | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[SpeechResult]]: ...

    def stream_stt(
        self, audio: AsyncIterable[bytes], *, audio_format: PCMFormat, model: str | None = None,
        options: TranscriptionOptions | None = None, stream_options: TranscriptionStreamOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptBlock]]: ...

    def stream_stt_with_auto_break(
        self, audio: AsyncIterable[bytes], *, audio_format: PCMFormat, model: str | None = None,
        options: TranscriptionOptions | None = None, stream_options: TranscriptionStreamOptions | None = None,
        segmenter: TextSegmenter | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptSegment]]: ...
