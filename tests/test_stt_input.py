"""Controlled PCM/probability fixtures prove mechanics, not real speech quality."""

from __future__ import annotations

import asyncio
import math
import random
import struct
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from agently import (
    Agently, AudioConnection, AudioInput, AudioInputEvent, AudioInputOptions, AudioModelRequest,
    AudioProtocolError, PCMFormat, SpeechResult, TranscriptResult, TranscriptionOptions, TranscriptionStreamOptions,
)
from agently.builtins.plugins.AudioModelRequester import OpenAICompatible
from agently.core.model.audio_stream.pcm import decode_pcm, encode_wav

FMT = PCMFormat(1000)


async def pieces(parts):
    for part in parts:
        yield part


def pcm(value, frames):
    return struct.pack('<h', value) * frames


class Detector:
    """A fixture treats sample 1 as speech, sample 0 as silence. No acoustic claim."""
    def __init__(self, *, frame_samples=10, probability=None):
        self.frame_samples = frame_samples
        self.probability = probability
        self.opens = self.closes = self.scores = 0

    @asynccontextmanager
    async def open(self, audio_format):
        self.opens += 1
        try:
            yield self
        finally:
            self.closes += 1

    async def score(self, data):
        self.scores += 1
        if isinstance(self.probability, BaseException):
            raise self.probability
        if self.probability is not None:
            return self.probability
        return 1.0 if any(struct.unpack('<' + 'h' * (len(data)//2), data)) else 0.0


class Driver(OpenAICompatible):
    def __init__(self, connection: AudioConnection = AudioConnection("http://unused.test")):
        super().__init__(connection)
        self.requests = []
        self.wait: asyncio.Event | None = None
        self.failure: Exception | None = None
        self.cancelled = False

    async def stt(self, request):
        assert request.options.input_options is None
        self.requests.append(request)
        try:
            if self.wait is not None:
                await self.wait.wait()
            if self.failure is not None:
                raise self.failure
            return TranscriptResult('原始文本。', request.model, 'zh', 999.0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def setup(**kwargs):
    detector = kwargs.pop('detector', Detector())
    events: list[AudioInputEvent] = []

    async def event(value: AudioInputEvent):
        events.append(value)

    base = AudioInputOptions(detector=detector, end_silence_seconds=.05, pre_speech_seconds=.02,
                             post_speech_seconds=.02, max_segment_seconds=.2, on_event=event)
    options = TranscriptionOptions(input_options=replace(base, **kwargs))
    driver = Driver()
    return AudioModelRequest(driver, stt_model='asr'), driver, detector, options, events


async def run(audio, raw, options, chunks=None, fmt=FMT):
    async with audio.stream_stt(pieces(chunks if chunks is not None else [raw]), audio_format=fmt,
                                options=options) as stream:
        return [block async for block in stream]


def samples(request):
    return decode_pcm(SpeechResult(request.audio.data, 'audio/wav', request.model), None, raw=False)[0]


@pytest.mark.asyncio
async def test_silence_short_reply_original_timeline_and_event_barrier():
    audio, driver, detector, options, events = setup()
    raw = pcm(0, 500) + pcm(1, 10) + pcm(0, 200)
    blocks = await run(audio, raw, options)
    assert len(blocks) == 1
    assert (blocks[0].start_seconds, blocks[0].end_seconds, blocks[0].speech_index) == (.48, .53, 0)
    assert samples(driver.requests[0]) == raw[960:1060]
    kinds = [e.kind for e in events]
    assert kinds.index('speech_start') < kinds.index('transcript') < kinds.index('pause') < kinds.index('input_end')
    pause = next(e for e in events if e.kind == 'pause')
    assert (pause.start_seconds, pause.end_seconds, pause.last_block) == (.51, .56, 0)
    assert events[-1].end_seconds == .71
    assert detector.opens == detector.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'', pcm(0, 3000)])
async def test_empty_and_all_silent_never_request(raw):
    audio, driver, detector, options, events = setup()
    assert await run(audio, raw, options, chunks=[b'', raw, b'']) == []
    assert driver.requests == []
    assert events[-1].kind == 'input_end'
    assert events[-1].end_seconds == len(raw)/2000
    assert detector.closes == 1


@pytest.mark.asyncio
async def test_random_transport_partition_invariance_and_short_eof():
    raw = pcm(0, 70) + pcm(1, 135) + pcm(0, 120) + pcm(1, 13)
    baseline = None
    for seed in range(15):
        rng = random.Random(seed)
        chunks = []
        cursor = 0
        while cursor < len(raw):
            size = rng.randint(1, 79)
            chunks.append(raw[cursor:cursor+size])
            cursor += size
        audio, driver, _, options, events = setup()
        blocks = await run(audio, raw, options, chunks=chunks)
        actual = (blocks, [samples(r) for r in driver.requests], events)
        if baseline is None:
            baseline = actual
        assert actual == baseline
        assert blocks[-1].end_seconds == len(raw)/2000
        assert samples(driver.requests[-1]).endswith(pcm(1, 13))


@pytest.mark.asyncio
async def test_continuous_speech_hard_limit_and_dense_pauses_no_sample_loss():
    raw = (pcm(1, 80) + pcm(0, 20)) * 20 + pcm(1, 17)
    audio, driver, _, options, events = setup()
    blocks = await run(audio, raw, options)
    assert b''.join(samples(r) for r in driver.requests) == raw
    assert all(b.end_seconds-b.start_seconds <= .20000001 for b in blocks)
    assert [b.speech_index for b in blocks] == [0]*len(blocks)
    assert all(e.kind != 'pause' for e in events)
    assert blocks[-1].end_seconds == 2.017
    assert [b.reason for b in blocks] == ["limit"]*(len(blocks)-1)+["input_end"]


@pytest.mark.asyncio
async def test_protection_never_duplicates_between_close_speech_runs():
    raw = pcm(1, 170) + pcm(0, 50) + pcm(1, 80) + pcm(0, 70)
    audio, driver, _, options, events = setup(pre_speech_seconds=.04, post_speech_seconds=.04)
    blocks = await run(audio, raw, options)
    assert all(a.end_seconds <= b.start_seconds for a,b in zip(blocks,blocks[1:]))
    assert all(samples(r) == raw[round(b.start_seconds*2000):round(b.end_seconds*2000)] for r,b in zip(driver.requests,blocks))
    assert [e.last_block for e in events if e.kind == 'pause'] == [1, 2]


@pytest.mark.asyncio
async def test_configured_minimum_is_explicit_rejection_not_default_short_filter():
    audio, driver, _, options, events = setup(min_speech_seconds=.03)
    assert await run(audio, pcm(1, 10)+pcm(0, 100), options) == []
    assert driver.requests == []
    assert 'rejected' in [e.kind for e in events]
    assert 'pause' not in [e.kind for e in events]


@pytest.mark.asyncio
async def test_minimum_not_met_at_buffer_limit_cannot_submit_candidate():
    audio, driver, _, options, events = setup(min_speech_seconds=.08)
    raw = (pcm(1,10)+pcm(0,40))*4
    assert await run(audio, raw, options) == []
    assert not driver.requests
    assert 'rejected' in [e.kind for e in events]


@pytest.mark.asyncio
async def test_pause_waits_for_model_and_consumer_and_callback_raw_block():
    audio, driver, _, options, events = setup()
    driver.wait = asyncio.Event()
    async with audio.stream_stt(pieces([pcm(1,10)+pcm(0,100)]), audio_format=FMT, options=options) as stream:
        pending = asyncio.ensure_future(anext(stream))
        while not driver.requests:
            await asyncio.sleep(0)
        assert 'pause' not in [e.kind for e in events]
        driver.wait.set()
        first = await pending
        assert events[-1].transcript == first
        assert 'pause' not in [e.kind for e in events]
        await asyncio.sleep(.01)
        assert len(driver.requests) == 1
        assert [block async for block in stream] == []
        assert next(e for e in events if e.kind == 'pause').last_block == first.index


@pytest.mark.asyncio
@pytest.mark.parametrize('where', ['driver', 'detector', 'source', 'callback', 'partial_frame'])
async def test_failure_no_flush_no_resume_and_detector_closed(where):
    audio, driver, detector, options, events = setup()
    if where == 'driver':
        driver.failure = RuntimeError('driver fail')
    if where == 'detector':
        detector.probability = RuntimeError('detector fail')
    if where == 'callback':
        async def fail(event):
            raise RuntimeError('callback fail')
        assert options.input_options is not None
        options = replace(options, input_options=replace(options.input_options, on_event=fail))

    async def source():
        if where == 'source':
            yield pcm(1, 10)
            raise RuntimeError('source fail')
        yield pcm(1, 10) + (b'x' if where == 'partial_frame' else pcm(0, 100))

    async with audio.stream_stt(source(), audio_format=FMT, options=options) as stream:
        with pytest.raises((RuntimeError, AudioProtocolError)):
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert detector.closes == 1
    assert 'input_end' not in [e.kind for e in events]
    assert 'pause' not in [e.kind for e in events]
    if where != 'driver':
        assert not driver.requests


@pytest.mark.asyncio
async def test_cancel_active_request_no_tail_and_close_idempotent():
    audio, driver, detector, options, events = setup()
    driver.wait = asyncio.Event()
    async with audio.stream_stt(pieces([pcm(1,1000)]), audio_format=FMT, options=options) as stream:
        pending = asyncio.ensure_future(anext(stream))
        while not driver.requests:
            await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert driver.cancelled
    assert len(driver.requests) == 1
    assert detector.closes == 1
    assert 'input_end' not in [e.kind for e in events]


@pytest.mark.asyncio
async def test_infinite_immediate_silence_is_cancellable():
    audio, driver, detector, options, _ = setup()
    async def endless():
        while True:
            yield pcm(0, 10)
    async with audio.stream_stt(endless(), audio_format=FMT, options=options) as stream:
        task = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert not driver.requests
    assert detector.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [math.nan, math.inf, -.1, 1.1, True, 'speech'])
async def test_invalid_detector_probability(value):
    audio, driver, _, options, _ = setup(detector=Detector(probability=value))
    with pytest.raises(AudioProtocolError):
        await run(audio, pcm(1, 10), options)
    assert not driver.requests


@pytest.mark.parametrize('changes', [
    dict(threshold=-.1), dict(threshold=1.1), dict(threshold=True), dict(threshold=math.nan),
    dict(end_silence_seconds=0), dict(post_speech_seconds=.1), dict(min_speech_seconds=-1),
    dict(max_segment_seconds=.04), dict(max_buffer_bytes=20), dict(max_file_bytes=True),
])
def test_invalid_options_preflight_without_source_consumption(changes):
    audio, driver, detector, options, _ = setup(**changes)
    with pytest.raises((ValueError, TypeError)):
        audio.stream_stt(pieces([pcm(1,10)]), audio_format=FMT, options=options)
    assert detector.opens == 0
    assert not driver.requests


@pytest.mark.asyncio
async def test_threshold_boundary_and_no_energy_gate():
    for probability, expected in ((.499,0),(.5,1)):
        audio, driver, _, options, _ = setup(detector=Detector(probability=probability))
        assert len(await run(audio, pcm(0,10), options)) == expected
        assert len(driver.requests) == expected  # PCM volume is not another gate


@pytest.mark.asyncio
async def test_file_and_agent_use_same_options_and_strip_before_driver(tmp_path):
    audio, driver, detector, options, events = setup()
    agent = Agently.create_agent().use_audio(audio)
    raw = pcm(0,100) + pcm(1,10) + pcm(0,100)
    path = tmp_path/'sample.wav'
    path.write_bytes(encode_wav(raw,FMT).data)
    result = await agent.async_stt(path, options=options)
    assert result.text == '原始文本。'
    assert result.duration is None
    assert detector.opens == detector.closes == 1
    assert events[-1].end_seconds == .21
    assert samples(driver.requests[0]) == raw[160:260]


def test_sync_file_reuses_async_preprocessing():
    audio, driver, detector, options, events = setup()
    result = audio.stt(encode_wav(pcm(0,500),FMT), options=options)
    assert result.text == '' and result.duration is None
    assert not driver.requests
    assert detector.opens == detector.closes == 1
    assert events[-1].kind == 'input_end'


@pytest.mark.asyncio
async def test_compressed_and_oversized_files_explicitly_fail(tmp_path):
    audio, driver, detector, options, _ = setup(max_file_bytes=10)
    path = tmp_path/'long.wav'
    path.write_bytes(b'a'*11)
    with pytest.raises(ValueError, match='max_file_bytes'):
        await audio.async_stt(path, options=options)
    with pytest.raises(AudioProtocolError, match='decode compressed'):
        await audio.async_stt(AudioInput(b'ID3fake','a.mp3','audio/mpeg'), options=options)
    assert not driver.requests and not detector.opens


@pytest.mark.asyncio
async def test_auto_break_preserves_raw_callback_and_text_contract():
    audio, _, _, options, events = setup()
    async with audio.stream_stt_with_auto_break(pieces([pcm(1,300)]), audio_format=FMT, options=options) as stream:
        segments = [segment async for segment in stream]
    assert ''.join(s.text for s in segments) == ''.join(e.transcript.text for e in events if e.transcript)
    assert all(s.reason == 'sentence_end' for s in segments)


@pytest.mark.asyncio
async def test_concurrent_calls_and_agent_rebinding_capture():
    audio, driver, detector, options, _ = setup()
    agent = Agently.create_agent().use_audio(audio)
    context = agent.stream_stt(pieces([pcm(1,10)]), audio_format=FMT, options=options)
    agent.use_audio(None)
    async with context as stream:
        blocks, other = await asyncio.gather(anext(stream), run(audio,pcm(1,20),options))
    assert blocks.start_seconds == other[0].start_seconds == 0
    assert len(driver.requests) == 2
    assert detector.opens == detector.closes == 2


@pytest.mark.asyncio
async def test_detector_frame_buffer_and_transport_bounds():
    audio, driver, detector, options, _ = setup(detector=Detector(frame_samples=500))
    with pytest.raises(ValueError, match="Detector frame"):
        await run(audio, pcm(1,10), options)
    assert detector.closes == 1 and not driver.requests
    audio, driver, _, options, _ = setup()
    with pytest.raises(ValueError, match="max_input_bytes"):
        async with audio.stream_stt(pieces([pcm(1,100)]), audio_format=FMT, options=options,
                                    stream_options=TranscriptionStreamOptions(window_seconds=.01, max_input_bytes=30)) as stream:
            await anext(stream)


@pytest.mark.asyncio
async def test_empty_valid_wav_and_multiple_final_blocks():
    import io
    import wave
    audio, driver, _, options, events = setup()
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setparams((1,2,1000,0,'NONE','not compressed'))
    result = await audio.async_stt(AudioInput(buffer.getvalue()), options=options)
    assert result.text == '' and not driver.requests
    assert events[-1].kind == 'input_end'
    result = await audio.async_stt(encode_wav(pcm(1,430),FMT), options=options)
    assert result.text == '原始文本。\n原始文本。\n原始文本。'
    assert result.duration is None


@pytest.mark.asyncio
async def test_native_driver_cannot_silently_ignore_input_processing():
    from agently import AudioCapabilityError, TranscriptionRequest
    _, _, _, options, _ = setup()
    driver = OpenAICompatible(AudioConnection('http://unused.test'))
    with pytest.raises(AudioCapabilityError, match='AudioModelRequest'):
        await driver.stt(TranscriptionRequest(encode_wav(pcm(1,10),FMT),'asr',options))


@pytest.mark.asyncio
async def test_unfiltered_failure_is_also_terminal():
    from agently.core.model.audio_stream.flow import pull_flow
    calls=[]
    async def request(value):
        calls.append(value)
        if value == 1:
            raise RuntimeError('first call failed')
        return value
    async with pull_flow(pieces([1,2]),request,lambda value:iter([value])) as stream:
        with pytest.raises(RuntimeError):
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    assert calls == [1]
