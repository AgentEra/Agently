"""TTS/STT shared production with independently selected output presentation."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager

from agently.types.data.audio import (
    AudioInput, AudioProtocolError, PCMFormat, PCMStream, SpeechResult, TextSegmentOptions,
    TextSource, TranscriptBlock, TranscriptResult, TranscriptSegment, TranscriptionStreamOptions,
)
from agently.types.plugins.AudioModelRequester import TextSegmenter
from .flow import pull_flow
from .pcm import decode_pcm, encode_wav, validate_pcm
from .segmentation import GreedyTextSegmenter, boundaries, positive_int, text_parts, validate_segments


def speech_source(source: TextSource, options: TextSegmentOptions, segmenter: TextSegmenter | None) -> AsyncIterator[str]:
    validate_segments(options)
    if not isinstance(source, str) and not hasattr(source, "__iter__") and not hasattr(source, "__aiter__"):
        raise TypeError("TTS source must be str or an iterable of text chunks.")
    return text_parts(source, options, segmenter if segmenter is not None else GreedyTextSegmenter(options))


def speech_chunks(result: SpeechResult) -> Iterator[SpeechResult]:
    if not result.data:
        raise AudioProtocolError("TTS produced empty audio.")
    return iter((result,))


class _PCMOutput:
    def __init__(self, expected: PCMFormat | None, raw: bool, chunk_bytes: int):
        positive_int(chunk_bytes, "chunk_bytes")
        if expected is not None:
            validate_pcm(expected)
        if raw and expected is None:
            raise ValueError("Raw PCM output requires audio_format.")
        self.format = expected
        self.raw = raw
        self.chunk_bytes = chunk_bytes

    def convert(self, result: SpeechResult) -> Iterator[bytes]:
        samples, fmt = decode_pcm(result, self.format, raw=self.raw)
        self.format = fmt
        frame_size = fmt.channels * 2
        if self.chunk_bytes < frame_size:
            raise ValueError("chunk_bytes is smaller than a sample frame.")
        size = self.chunk_bytes // frame_size * frame_size
        return (samples[i:i + size] for i in range(0, len(samples), size))


class _PCMReader:
    def __init__(self, iterator: AsyncIterator[bytes], output: _PCMOutput):
        self._iterator = iterator
        self._output = output
        self._first: bytes | None = None

    @property
    def audio_format(self) -> PCMFormat | None:
        return self._output.format

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._first is not None:
            value, self._first = self._first, None
            return value
        return await anext(self._iterator)

    async def prime(self) -> None:
        try:
            self._first = await anext(self._iterator)
        except StopAsyncIteration:
            pass


@asynccontextmanager
async def pcm_stream(
    source: AsyncIterator[str], request: Callable[[str], Awaitable[SpeechResult]], output: _PCMOutput,
) -> AsyncIterator[PCMStream]:
    async with pull_flow(source, request, output.convert) as iterator:
        reader = _PCMReader(iterator, output)
        await reader.prime()  # nonempty stream format is ready before the first bytes are consumed
        try:
            yield reader
        finally:
            reader._first = None


def validate_transcription_stream(
    fmt: PCMFormat, options: TranscriptionStreamOptions, *, windowed: bool = True,
) -> int:
    frame_bytes = validate_pcm(fmt)
    positive_int(options.max_input_bytes, "max_input_bytes")
    positive_int(options.max_transcript_chars, "max_transcript_chars")
    positive_int(options.max_pending_chars, "max_pending_chars")
    if not windowed:
        return 0  # acoustic input options own the segment size
    seconds = options.window_seconds
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("window_seconds must be finite and positive.")
    frames = int(seconds * fmt.sample_rate)
    if frames < 1 or frames * frame_bytes > options.max_input_bytes:
        raise ValueError("STT window must contain at least one frame and fit max_input_bytes.")
    return frames * frame_bytes


async def audio_windows(
    source: AsyncIterable[bytes], fmt: PCMFormat, options: TranscriptionStreamOptions,
) -> AsyncIterator[AudioInput]:
    window = validate_transcription_stream(fmt, options)
    buffer = bytearray()
    async for chunk in source:
        if not isinstance(chunk, bytes):
            raise TypeError("PCM streams must yield bytes.")
        if len(chunk) > options.max_input_bytes:
            raise ValueError("PCM source item exceeds max_input_bytes.")
        if not chunk:
            await asyncio.sleep(0)
            continue
        offset = 0
        while offset < len(chunk):
            take = min(window - len(buffer), len(chunk) - offset)
            buffer.extend(chunk[offset:offset + take])
            offset += take
            if len(buffer) == window:
                yield encode_wav(bytes(buffer), fmt)
                buffer.clear()
    if buffer:
        yield encode_wav(bytes(buffer), fmt)  # rejects a partial sample frame at EOF


class TranscriptionOutput:
    def __init__(self, fmt: PCMFormat, options: TranscriptionStreamOptions):
        self.fmt = fmt
        self.options = options
        self.index = 0
        self.frames = 0

    def block(self, result: TranscriptResult, frames: int) -> TranscriptBlock:
        if len(result.text) > self.options.max_transcript_chars:
            raise AudioProtocolError("Transcription exceeds max_transcript_chars.")
        block = TranscriptBlock(
            result.text, self.index, self.frames / self.fmt.sample_rate,
            (self.frames + frames) / self.fmt.sample_rate, result.model, result.language,
        )
        self.frames += frames
        self.index += 1
        return block


class SentenceOutput:
    """Retains only the pending text and its source block spans."""

    def __init__(self, limit: int, segmenter: TextSegmenter | None = None):
        self.limit = limit
        self.segmenter = segmenter
        self.pending = ""
        self.last_source_char = ""
        self.spans: deque[tuple[int, int]] = deque()

    def _take(self, count: int, reason: str) -> TranscriptSegment:
        first = self.spans[0][1]
        remaining, last = count, first
        while remaining:
            length, last = self.spans.popleft()
            used = min(length, remaining)
            remaining -= used
            if length > used:
                self.spans.appendleft((length - used, last))
        text, self.pending = self.pending[:count], self.pending[count:]
        from typing import cast, Literal
        return TranscriptSegment(text, cast(Literal["sentence_end", "limit", "input_end"], reason), first, last)

    def add(self, block: TranscriptBlock) -> Iterator[TranscriptSegment]:
        text = block.text
        if not text:
            return iter(())
        # Explicit display separator only at an ASCII word boundary. This does
        # not reconstruct a word split by an ASR window or rewrite source blocks.
        if self.last_source_char and self.last_source_char.isascii() and self.last_source_char.isalnum() and text[0].isascii() and text[0].isalnum():
            text = " " + text
        self.last_source_char = text[-1]
        self.pending += text
        self.spans.append((len(text), block.index))
        return self._drain(final=False)

    def _drain(self, *, final: bool) -> Iterator[TranscriptSegment]:
        while self.pending:
            if self.segmenter is not None:
                cut = self.segmenter.cut(self.pending[:self.limit + 1], final=final and len(self.pending) <= self.limit)
                if cut is not None and (isinstance(cut, bool) or not isinstance(cut, int) or not 0 < cut <= min(len(self.pending), self.limit)):
                    raise ValueError("Sentence segmenter returned an invalid prefix length.")
            else:
                cut = next((pos for pos, rank in boundaries(self.pending[:self.limit + 1], final=final and len(self.pending) <= self.limit) if rank == 1 and pos <= self.limit), None)
            if cut is not None:
                yield self._take(cut, "sentence_end")
            elif len(self.pending) >= self.limit:
                yield self._take(self.limit, "limit")
            elif final:
                yield self._take(len(self.pending), "input_end")
            else:
                break

    def finish(self) -> Iterator[TranscriptSegment]:
        return self._drain(final=True)


@asynccontextmanager
async def sentence_stream(
    blocks: AsyncIterator[TranscriptBlock], output: SentenceOutput,
) -> AsyncIterator[AsyncIterator[TranscriptSegment]]:
    async def sentences() -> AsyncGenerator[TranscriptSegment, None]:
        async for block in blocks:
            for sentence in output.add(block):
                yield sentence
        for sentence in output.finish():
            yield sentence

    iterator = sentences()
    try:
        yield iterator
    finally:
        await iterator.aclose()
