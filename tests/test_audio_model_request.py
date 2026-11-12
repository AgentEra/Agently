"""Deterministic transport/owner tests. Synthetic audio is not model-quality evidence."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import httpx
import pytest
from typing import TYPE_CHECKING

from agently import (
    Agently, AudioCapabilityError, AudioConnection, AudioInput, AudioModelRequest, AudioOperation, AudioProtocolError,
    SpeechOptions, SpeechRequest, TranscriptEvent, TranscriptionOptions, TranscriptionRequest, PCMFormat,
)
from agently.builtins.plugins.AudioModelRequester import OMLX, OpenAICompatible, RapidMLX
from agently.builtins.plugins.AgentExecution import AgentExecution

if TYPE_CHECKING:
    from agently import Agent, AudioCapability, SpeechResult, TranscriptResult

    async def typed_consumer(audio: AudioModelRequest) -> tuple[SpeechResult, TranscriptResult]:
        capability: AudioCapability = audio
        agent: Agent = Agently.create_agent().use_audio(capability)
        speech = await agent.async_tts("hello")
        transcript = await agent.async_stt(AudioInput(speech.data))
        return speech, transcript


def test_audio_public_exports():
    import agently
    import agently.core
    import agently.core.model

    for module in (agently, agently.core, agently.core.model):
        assert "AudioModelRequest" in module.__all__
        assert module.AudioModelRequest is AudioModelRequest


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes]):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            yield part

    async def aclose(self):
        self.closed = True


def audio_client(monkeypatch, handler, *, driver_class: type[OpenAICompatible] = OMLX):
    driver = driver_class(AudioConnection("http://audio.test/v1", "private-key", 5))
    clients = []

    def client():
        value = httpx.AsyncClient(base_url="http://audio.test/v1/", transport=httpx.MockTransport(handler))
        clients.append(value)
        return value

    monkeypatch.setattr(driver, "_client", client)
    return AudioModelRequest(driver, tts_model="speech-model", stt_model="transcribe-model"), clients


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_class", [OMLX, RapidMLX])
async def test_complete_calls_preserve_audio_and_distinct_models(monkeypatch, tmp_path, driver_class):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("speech"):
            assert json.loads(request.content) == {
                "model": "speech-model", "input": "你好", "response_format": "wav", "speed": 1.0,
                "voice": "speaker", "language": "Chinese", "seed": 3,
            }
            return httpx.Response(200, content=b"audio-evidence", headers={"content-type": "audio/wav"})
        assert b'transcribe-model' in request.content
        assert b'audio-evidence' in request.content
        assert b'name="prompt"' in request.content
        return httpx.Response(200, json={"text": "你好", "language": "zh", "duration": 1.2})

    audio, clients = audio_client(monkeypatch, handler, driver_class=driver_class)
    speech = await audio.async_tts("你好", voice="speaker", options=SpeechOptions(language="Chinese", extra={"seed": 3}))
    result = await audio.async_stt(AudioInput(speech.data), options=TranscriptionOptions(prompt="词汇"))
    assert (result.text, result.model, result.duration) == ("你好", "transcribe-model", 1.2)
    source = tmp_path / "sample.wav"
    source.write_bytes(speech.data)
    await audio.async_stt(source, options=TranscriptionOptions(prompt="词汇"))
    assert source.read_bytes() == speech.data
    assert len(requests) == 3
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty_text", "missing_model", "empty_audio", "bad_speed", "bad_format", "override", "multipart_override", "missing_file"])
async def test_invalid_inputs_do_not_dispatch(monkeypatch, case):
    calls = []
    audio, _ = audio_client(monkeypatch, lambda request: calls.append(request))
    with pytest.raises((ValueError, FileNotFoundError)):
        if case == "empty_text":
            await audio.async_tts(" ")
        elif case == "missing_model":
            await audio.async_tts("hello", model="")
        elif case == "empty_audio":
            await audio.async_stt(AudioInput(b""))
        elif case == "bad_speed":
            await audio.async_tts("hello", options=SpeechOptions(speed=float("nan")))
        elif case == "bad_format":
            async with audio.stream_tts("hello", options=SpeechOptions(response_format="mp3")):
                pass
        elif case == "override":
            await audio.async_tts("hello", options=SpeechOptions(extra={"stream": True}))
        elif case == "multipart_override":
            await audio.async_stt(AudioInput(b"x"), options=TranscriptionOptions(extra={"file": "other"}))
        else:
            await audio.async_stt("/does-not-exist/audio.wav")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(500), httpx.Response(200, json={"no_text": "wrong"}),
    httpx.Response(200, json={"text": 123}), httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"text": "", "duration": -1}),
])
@pytest.mark.parametrize("driver_class", [OMLX, RapidMLX])
async def test_stt_failure_is_not_success_or_retry(monkeypatch, response, driver_class):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    audio, clients = audio_client(monkeypatch, handler, driver_class=driver_class)
    with pytest.raises((httpx.HTTPStatusError, AudioProtocolError)):
        await audio.async_stt(AudioInput(b"audio"))
    assert len(calls) == 1
    assert clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,media", [
    (200, b"", "audio/wav"), (200, b'{"error":"failed"}', "application/json"),
    (503, b"unavailable", "text/plain"), (302, b"redirect", "text/plain"),
])
@pytest.mark.parametrize("driver_class", [OMLX, RapidMLX])
async def test_bad_speech_delivery_is_rejected(monkeypatch, status, body, media, driver_class):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers={"content-type": media})

    audio, clients = audio_client(monkeypatch, handler, driver_class=driver_class)
    with pytest.raises((httpx.HTTPStatusError, AudioProtocolError)):
        await audio.async_tts("hello")
    assert len(calls) == 1 and clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("driver_class", [OMLX, RapidMLX])
async def test_timeout_closes_client_without_implicit_retry(monkeypatch, driver_class):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    audio, clients = audio_client(monkeypatch, handler, driver_class=driver_class)
    with pytest.raises(httpx.ReadTimeout):
        await audio.async_tts("hello")
    assert len(calls) == 1 and clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("early", [False, True])
async def test_speech_stream_is_ordered_and_closed(monkeypatch, early):
    stream = RecordingStream([b"header", b"part1", b"part2"])
    audio, clients = audio_client(monkeypatch, lambda _: httpx.Response(200, stream=stream, headers={"content-type": "audio/wav"}))
    parts = []
    async with audio.driver.stream_tts(SpeechRequest("hello", "speech-model")) as chunks:
        async for part in chunks:
            parts.append(part)
            if early:
                break
    assert parts == ([b"header"] if early else stream.parts)
    assert stream.closed and clients[0].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("tail,raises", [
    ('data: {"type":"transcript.text.done","text":"你好"}\n\n', False),
    ('', True),
    ('data: {"type":"error","error":"failed"}\n\n', True),
    ('data: {"type":"transcript.text.done","text":null}\n\n', True),
])
async def test_transcription_stream_requires_done(monkeypatch, tail, raises):
    stream = RecordingStream([
        b': keepalive\n\nevent: transcript.text.delta\ndata: {"delta": "',
        '你好"}\n\n'.encode(), tail.encode(),
    ])
    audio, clients = audio_client(monkeypatch, lambda _: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"}))
    seen = []

    async def consume():
        async with audio.driver.stream_stt(TranscriptionRequest(AudioInput(b"audio"), "transcribe-model")) as events:
            async for event in events:
                seen.append(event)

    if raises:
        with pytest.raises(AudioProtocolError):
            await consume()
    else:
        await consume()
        assert seen[-1] == TranscriptEvent("done", "你好")
    assert seen[0] == TranscriptEvent("delta", "你好")
    assert stream.closed and clients[0].is_closed


@pytest.mark.asyncio
async def test_cancel_closes_transport(monkeypatch):
    entered = asyncio.Event()

    class BlockingStream(RecordingStream):
        async def __aiter__(self):
            yield b"first"
            entered.set()
            await asyncio.Future()

    stream = BlockingStream([])
    audio, clients = audio_client(monkeypatch, lambda _: httpx.Response(200, stream=stream, headers={"content-type": "audio/wav"}))

    async def consume():
        async with audio.driver.stream_tts(SpeechRequest("hello", "speech-model")) as chunks:
            async for _ in chunks:
                pass

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and clients[0].is_closed


@pytest.mark.asyncio
async def test_realtime_is_not_emulated_by_buffering(monkeypatch):
    audio, clients = audio_client(monkeypatch, lambda _: pytest.fail("Unexpected network"))

    async def frames():
        pytest.fail("Unsupported driver must not consume input")
        yield b"pcm"

    with pytest.raises(AudioCapabilityError):
        async with audio.driver.stream_stt_input(frames(), model="stt", audio_format=PCMFormat(), options=TranscriptionOptions()):
            pass
    assert not clients


def test_default_driver_does_not_claim_all_compatible_streaming():
    audio = Agently.create_audio_request(base_url="http://audio.test/v1")
    assert audio.driver.supported_operations == frozenset({"tts", "stt"})
    assert audio.supported_operations == frozenset({"tts", "stt", "stream_tts", "stream_stt", "stream_tts_with_auto_break", "stream_stt_with_auto_break"})
    assert "private-key" not in repr(AudioConnection("http://audio.test/v1", "private-key"))


@pytest.mark.asyncio
async def test_sync_entry_in_async_caller_and_explicit_agent_mount(monkeypatch):
    audio, _ = audio_client(monkeypatch, lambda _: httpx.Response(200, content=b"audio", headers={"content-type": "audio/wav"}))
    agent = Agently.create_agent()
    with pytest.raises(RuntimeError, match="not bound"):
        agent.tts("hello")
    agent.use_audio(audio)
    assert agent.tts("hello").data == b"audio"
    assert audio.tts("hello").data == b"audio"
    assert Agently.create_agent().request is not agent.request
    agent.use_audio(None)
    with pytest.raises(RuntimeError, match="not bound"):
        await agent.async_tts("hello")


def test_execution_static_dynamic_dependency_and_binding_stability():
    class NeedsAudio(AgentExecution):
        required_agent_capabilities = ("audio",)

    agent = Agently.create_agent()
    with pytest.raises(RuntimeError, match="not bound"):
        NeedsAudio(agent)
    original, replacement = object(), object()
    agent.use_capability("audio", original)
    execution = NeedsAudio(agent)
    agent.use_capability("audio", replacement)
    assert execution.require_agent_capability("audio") is original
    assert NeedsAudio(agent).require_agent_capability("audio") is replacement
    with pytest.raises(RuntimeError, match="not bound"):
        execution.require_agent_capability("other")
    agent.use_capability("other", original)
    assert execution.require_agent_capability("other") is original
    with pytest.raises(NotImplementedError, match="rebinding"):
        execution.save()
    assert execution.control_capabilities["snapshot_boundaries"] == []


def test_factory_rejects_missing_dependency_before_plugin_constructor():
    constructed = []

    class NeedsAudio(AgentExecution):
        name = "audio_dependency_test"
        required_agent_capabilities = ("audio",)

        def __init__(self, agent, **kwargs):
            constructed.append(True)
            super().__init__(agent, **kwargs)

    Agently.plugin_manager.register("AgentExecution", NeedsAudio, activate=False)
    try:
        agent = Agently.create_agent()
        with pytest.raises(RuntimeError, match="not bound"):
            agent.create_execution(NeedsAudio.name)
        assert constructed == []
        agent.use_capability("audio", object())
        assert agent.create_execution(NeedsAudio.name).name == NeedsAudio.name
        assert constructed == [True]
    finally:
        Agently.plugin_manager.unregister("AgentExecution", NeedsAudio.name)


@pytest.mark.asyncio
async def test_concurrent_calls_keep_inputs_and_cleanup_separate(monkeypatch):
    async def handler(request):
        payload = json.loads(request.content)
        await asyncio.sleep(0)
        return httpx.Response(200, content=payload["input"].encode(), headers={"content-type": "audio/wav"})

    audio, clients = audio_client(monkeypatch, handler)
    results = await asyncio.gather(audio.async_tts("first"), audio.async_tts("second"))
    assert [result.data for result in results] == [b"first", b"second"]
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_input_stream_can_be_owned_by_replacement_driver():
    received = []
    closed = []

    class InputDriver(OpenAICompatible):
        @property
        def supported_operations(self) -> frozenset[AudioOperation]:
            return frozenset({"stream_stt_input"})

        @asynccontextmanager
        async def stream_stt_input(self, chunks, *, model, audio_format, options):
            async def events() -> AsyncIterator[TranscriptEvent]:
                async for part in chunks:
                    received.append(part)
                    yield TranscriptEvent("delta", "synthetic")
                yield TranscriptEvent("done", "synthetic")
            try:
                yield events()
            finally:
                closed.append(True)

    async def frames():
        yield b"frame1"
        yield b"frame2"

    audio = AudioModelRequest(InputDriver(AudioConnection("http://audio.test/v1")), stt_model="realtime")
    async with audio.driver.stream_stt_input(frames(), model="realtime", audio_format=PCMFormat(), options=TranscriptionOptions()) as events:
        assert [event.kind async for event in events] == ["delta", "delta", "done"]
    assert received == [b"frame1", b"frame2"] and closed == [True]


def test_rapid_mlx_registered_complete_file_profile():
    audio = Agently.create_audio_request(driver="RapidMLX", base_url="http://localhost:8000/v1")
    assert isinstance(audio.driver, RapidMLX)
    assert audio.driver.name == "RapidMLX"
    assert audio.driver.supported_operations == frozenset({"tts", "stt"})
    assert audio.supported_operations == frozenset({
        "tts", "stt", "stream_tts", "stream_stt", "stream_tts_with_auto_break", "stream_stt_with_auto_break",
    })


@pytest.mark.asyncio
async def test_rapid_mlx_native_streams_fail_before_dispatch(monkeypatch):
    audio, clients = audio_client(monkeypatch, lambda _: pytest.fail("Unexpected network"), driver_class=RapidMLX)
    with pytest.raises(AudioCapabilityError, match="stream_tts"):
        async with audio.driver.stream_tts(SpeechRequest("hello", "tts-1")):
            pass
    with pytest.raises(AudioCapabilityError, match="stream_stt"):
        async with audio.driver.stream_stt(TranscriptionRequest(AudioInput(b"audio"), "whisper-large-v3-turbo")):
            pass

    async def frames():
        pytest.fail("Unsupported driver must not consume input")
        yield b"pcm"

    with pytest.raises(AudioCapabilityError, match="realtime audio input"):
        async with audio.driver.stream_stt_input(
            frames(), model="whisper-large-v3-turbo", audio_format=PCMFormat(), options=TranscriptionOptions(),
        ):
            pass
    assert clients == []
