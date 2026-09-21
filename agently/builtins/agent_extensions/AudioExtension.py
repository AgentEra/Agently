"""Thin Agent access to an explicitly bound, independently replaceable audio capability."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager
from os import PathLike
from typing import TypeVar, cast

from agently_stage import default_stage_call_bridge

from agently.core.Agent import BaseAgent
from agently.core.model.AudioConfig import resolve_audio
from agently.types.data.audio import (
    AudioInput, SpeechOptions, SpeechResult, TranscriptionOptions, TranscriptResult, PCMFormat, PCMStream,
    TextSource, TextSegmentOptions, TranscriptionStreamOptions, TranscriptBlock, TranscriptSegment,
)
from agently.types.plugins.AudioModelRequester import AudioCapability, TextSegmenter

AudioAgent = TypeVar("AudioAgent", bound="AudioExtension")


class AudioExtension(BaseAgent):
    def use_audio(self: AudioAgent, audio: AudioCapability | None) -> AudioAgent:
        """Mount audio explicitly; None unbinds it for future calls/executions."""
        if audio is not None:
            for method in ("async_tts", "async_stt", "stream_tts", "stream_stt", "stream_tts_with_auto_break", "stream_stt_with_auto_break"):
                if not callable(getattr(audio, method, None)):
                    raise TypeError(f"Audio capability must implement {method}.")
            if not isinstance(audio.supported_operations, frozenset):
                raise TypeError("Audio capability supported_operations must be a frozenset.")
        self.use_capability("audio", audio)
        return self

    @property
    def audio(self) -> AudioCapability:
        """Bound audio capability, including its explicitly scoped stream methods."""
        return cast(AudioCapability, self.require_capability("audio"))

    async def async_tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult:
        return await resolve_audio(self, "tts").async_tts(text, model=model, voice=voice, options=options)

    def tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult:
        return default_stage_call_bridge.as_sync(self.async_tts)(text, model=model, voice=voice, options=options)

    async def async_stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult:
        return await resolve_audio(self, "stt").async_stt(audio, model=model, options=options)

    def stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult:
        return default_stage_call_bridge.as_sync(self.async_stt)(audio, model=model, options=options)

    def stream_tts(
        self, text: TextSource, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None, segments: TextSegmentOptions | None = None,
        segmenter: TextSegmenter | None = None, audio_format: PCMFormat | None = None, chunk_bytes: int = 8192,
    ) -> AbstractAsyncContextManager[PCMStream]:
        return resolve_audio(self, "tts").stream_tts(
            text, model=model, voice=voice, options=options, segments=segments,
            segmenter=segmenter, audio_format=audio_format, chunk_bytes=chunk_bytes,
        )

    def stream_tts_with_auto_break(
        self, text: TextSource, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None, segments: TextSegmentOptions | None = None,
        segmenter: TextSegmenter | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[SpeechResult]]:
        return resolve_audio(self, "tts").stream_tts_with_auto_break(
            text, model=model, voice=voice, options=options, segments=segments, segmenter=segmenter,
        )

    def stream_stt(
        self, audio: AsyncIterable[bytes], *, audio_format: PCMFormat, model: str | None = None,
        options: TranscriptionOptions | None = None, stream_options: TranscriptionStreamOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptBlock]]:
        return resolve_audio(self, "stt").stream_stt(
            audio, audio_format=audio_format, model=model, options=options, stream_options=stream_options,
        )

    def stream_stt_with_auto_break(
        self, audio: AsyncIterable[bytes], *, audio_format: PCMFormat, model: str | None = None,
        options: TranscriptionOptions | None = None, stream_options: TranscriptionStreamOptions | None = None,
        segmenter: TextSegmenter | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptSegment]]:
        return resolve_audio(self, "stt").stream_stt_with_auto_break(
            audio, audio_format=audio_format, model=model, options=options,
            stream_options=stream_options, segmenter=segmenter,
        )
