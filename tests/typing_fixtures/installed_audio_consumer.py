"""Strict installed-wheel contract probe; performs no model calls."""

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing_extensions import assert_type

from agently import (
    Agent, AudioCapability, AudioModelRequest, AudioInputEvent, AudioInputOptions, SpeechDetector, TranscriptionOptions, PCMFormat, PCMStream, SpeechResult,
    TextSegmenter, TextSegmentOptions, TranscriptBlock, TranscriptSegment,
)


class Pairs:
    def cut(self, text: str, *, final: bool) -> int | None:
        return 2 if len(text) >= 2 else None


def contracts(agent: Agent, audio: AudioModelRequest, text: AsyncIterable[str], pcm: AsyncIterable[bytes]) -> None:
    strategy: TextSegmenter = Pairs()
    capability: AudioCapability = audio
    assert_type(agent.use_audio(capability), Agent)
    assert_type(agent.stream_tts(text, segments=TextSegmentOptions(), segmenter=strategy), AbstractAsyncContextManager[PCMStream])
    assert_type(agent.stream_tts_with_auto_break(text), AbstractAsyncContextManager[AsyncIterator[SpeechResult]])
    assert_type(agent.stream_stt(pcm, audio_format=PCMFormat()), AbstractAsyncContextManager[AsyncIterator[TranscriptBlock]])
    assert_type(agent.stream_stt_with_auto_break(pcm, audio_format=PCMFormat()), AbstractAsyncContextManager[AsyncIterator[TranscriptSegment]])


async def consume(audio: AudioCapability, text: AsyncIterable[str]) -> None:
    async with audio.stream_tts(text) as stream:
        assert_type(stream.audio_format, PCMFormat | None)
        async for chunk in stream:
            assert_type(chunk, bytes)


async def input_contract(audio: AudioModelRequest, detector: SpeechDetector, pcm: AsyncIterable[bytes]) -> None:
    async def on_event(event: AudioInputEvent) -> None:
        assert_type(event.transcript, TranscriptBlock | None)
        assert_type(event.last_block, int | None)
    options = TranscriptionOptions(input_options=AudioInputOptions(detector=detector, on_event=on_event))
    async with audio.stream_stt(pcm, audio_format=PCMFormat(), options=options) as stream:
        async for block in stream:
            assert_type(block.speech_index, int | None)
