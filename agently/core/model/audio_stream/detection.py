"""Bounded acoustic segmentation on the original PCM timeline."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Literal

from agently.types.data.audio import AudioInput, AudioInputEvent, AudioInputOptions, AudioProtocolError, PCMFormat
from .pcm import encode_wav, validate_pcm
from .segmentation import positive_int


@dataclass(frozen=True)
class AudioSpan:
    audio: AudioInput
    start: int
    end: int
    speech_index: int
    index: int
    reason: Literal["pause", "limit", "input_end"]


DetectionItem = AudioSpan | AudioInputEvent


def validate_input(fmt: PCMFormat | None, options: AudioInputOptions) -> None:
    for name in ("max_buffer_bytes", "max_file_bytes"):
        positive_int(getattr(options, name), name)
    for name in ("threshold", "min_speech_seconds", "end_silence_seconds", "pre_speech_seconds",
                 "post_speech_seconds", "max_segment_seconds"):
        value = getattr(options, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative.")
    if options.threshold > 1:
        raise ValueError("threshold must be in [0, 1].")
    if options.end_silence_seconds <= 0 or options.max_segment_seconds <= 0:
        raise ValueError("end_silence_seconds and max_segment_seconds must be positive.")
    if options.post_speech_seconds > options.end_silence_seconds:
        raise ValueError("post_speech_seconds cannot exceed end_silence_seconds.")
    if not callable(getattr(options.detector, "open", None)):
        raise TypeError("detector must implement open(audio_format).")
    if options.on_event is not None and not callable(options.on_event):
        raise TypeError("on_event must be an async callback.")
    if fmt is None:
        return  # complete-file metadata is validated after bounded decoding
    frame_bytes = validate_pcm(fmt)
    rate = fmt.sample_rate
    bound = math.ceil(options.max_segment_seconds * rate)
    required = sum(math.ceil(value * rate) for value in (
        options.pre_speech_seconds, options.end_silence_seconds, options.min_speech_seconds,
    ))
    if bound <= required or bound * frame_bytes > options.max_buffer_bytes:
        raise ValueError("max_segment_seconds must exceed pre/min-speech/end-silence and fit max_buffer_bytes.")



class SpeechSegments:
    """Owns only active PCM and a bounded look-behind, never completed history."""

    def __init__(self, fmt: PCMFormat, options: AudioInputOptions):
        self.fmt, self.options = fmt, options
        self.width = fmt.channels * 2
        self.pre = math.ceil(options.pre_speech_seconds * fmt.sample_rate)
        self.post = math.ceil(options.post_speech_seconds * fmt.sample_rate)
        self.silence = math.ceil(options.end_silence_seconds * fmt.sample_rate)
        self.minimum = math.ceil(options.min_speech_seconds * fmt.sample_rate)
        self.maximum = math.ceil(options.max_segment_seconds * fmt.sample_rate)
        self.buffer = bytearray()
        self.start = 0
        self.position = 0
        self.onset: int | None = None
        self.last_voice = 0
        self.voice_frames = 0
        self.speech_index = -1
        self.block_index = 0
        self.last_block: int | None = None
        self.reported_silence = 0

    def event(self, kind: Literal["speech_start", "transcript", "pause", "rejected", "silence", "input_end"], start: int, end: int) -> AudioInputEvent:
        return AudioInputEvent(kind, start / self.fmt.sample_rate, end / self.fmt.sample_rate,
                               self.speech_index if self.onset is not None else None, self.last_block)

    def _trim(self, start: int) -> None:
        start = max(self.start, start)
        del self.buffer[:(start - self.start) * self.width]
        self.start = start

    def _span(self, end: int, reason: Literal["pause", "limit", "input_end"]) -> AudioSpan:
        count = (end - self.start) * self.width
        result = AudioSpan(encode_wav(bytes(self.buffer[:count]), self.fmt), self.start, end,
                           self.speech_index, self.block_index, reason)
        self._trim(end)
        self.last_block = self.block_index
        self.block_index += 1
        return result

    def add(self, pcm: bytes, probability: float) -> Iterator[DetectionItem]:
        frames = len(pcm) // self.width
        previous = self.position
        self.position += frames
        voiced = probability >= self.options.threshold
        if self.onset is None and voiced:
            self.onset = previous
            self.speech_index += 1
            self.last_block = None
            self.voice_frames = 0
            self._trim(max(self.start, previous - self.pre))
            yield self.event("speech_start", previous, self.position)
        self.buffer.extend(pcm)
        if self.onset is not None:
            if voiced:
                self.last_voice = self.position
                self.voice_frames += frames
            # Wait for acoustic end before trimming the post-roll. This order
            # prevents a silence-only hard cut when a pause lands at the limit.
            if not voiced and self.position - self.last_voice >= self.silence:
                yield from self._end(pause=True)
            else:
                while self.position - self.start >= self.maximum:
                    if self.voice_frames < self.minimum:
                        yield self.event("rejected", self.onset, self.position)
                        self.onset = None
                        self.last_block = None
                        self.reported_silence = self.position
                        self._trim(max(self.start, self.position - self.pre))
                        break
                    yield self._span(self.start + self.maximum, "limit")
        else:
            self._trim(max(0, self.position - self.pre))
            if self.position - self.reported_silence >= self.silence:
                yield self.event("silence", self.reported_silence, self.position)
                self.reported_silence = self.position

    def _end(self, *, pause: bool) -> Iterator[DetectionItem]:
        assert self.onset is not None
        end = min(self.position, self.last_voice + self.post) if pause else self.position
        if self.voice_frames >= self.minimum:
            while end - self.start > self.maximum:
                yield self._span(self.start + self.maximum, "limit")
            if end > self.start:
                yield self._span(end, "pause" if pause else "input_end")
            if pause:
                yield self.event("pause", self.last_voice, self.position)
        else:
            yield self.event("rejected", self.onset, self.position)
        self.onset = None
        self.last_block = None
        self.reported_silence = self.position
        self._trim(max(self.start, self.position - self.pre))

    def finish(self) -> Iterator[DetectionItem]:
        if self.onset is not None:
            yield from self._end(pause=False)
        elif self.position > self.reported_silence:
            yield self.event("silence", self.reported_silence, self.position)
        yield self.event("input_end", 0, self.position)
        self.buffer.clear()


async def detected_audio(
    source: AsyncIterable[bytes], fmt: PCMFormat, options: AudioInputOptions, max_input_bytes: int,
) -> AsyncIterator[DetectionItem]:
    validate_input(fmt, options)
    segments = SpeechSegments(fmt, options)
    async with options.detector.open(fmt) as detector:
        positive_int(detector.frame_samples, "detector.frame_samples")
        size = detector.frame_samples * fmt.channels * 2
        if (segments.maximum + 2 * detector.frame_samples) * fmt.channels * 2 > options.max_buffer_bytes:
            raise ValueError("Segment plus detector framing buffers exceed max_buffer_bytes.")
        # A detection frame can cross a hard segment boundary but must not
        # delay a minimum-speech decision past that boundary.
        if detector.frame_samples + segments.pre + segments.minimum >= segments.maximum:
            raise ValueError("Detector frame plus pre/min-speech must fit max_segment_seconds.")
        pending = bytearray()

        async def analyze(pcm: bytes) -> float:
            probability = await detector.score(pcm)
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise AudioProtocolError("Speech detector must return a finite probability in [0, 1].")
            return probability

        async for chunk in source:
            if not isinstance(chunk, bytes):
                raise TypeError("PCM streams must yield bytes.")
            if len(chunk) > max_input_bytes:
                raise ValueError("PCM source item exceeds max_input_bytes.")
            offset = 0
            while offset < len(chunk):
                take = min(size - len(pending), len(chunk) - offset)
                pending.extend(chunk[offset:offset + take])
                offset += take
                if len(pending) == size:
                    pcm = bytes(pending)
                    pending.clear()
                    for item in segments.add(pcm, await analyze(pcm)):
                        yield item
            # Even immediate custom scorers / in-memory silent sources must
            # cooperate with cancellation and other tasks.
            await asyncio.sleep(0)
        if len(pending) % (fmt.channels * 2):
            raise AudioProtocolError("PCM ends with an incomplete sample frame.")
        if pending:
            pcm = bytes(pending)
            for item in segments.add(pcm, await analyze(pcm)):
                yield item
        for item in segments.finish():
            yield item
