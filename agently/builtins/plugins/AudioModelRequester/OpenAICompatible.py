"""Complete-file audio HTTP driver. No text ModelRequester or Prompt dependency."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from agently.types.data.audio import (
    AudioCapabilityError, AudioConnection, AudioOperation, AudioProtocolError, PCMFormat,
    SpeechRequest, SpeechResult, TranscriptEvent, TranscriptResult, TranscriptionOptions, TranscriptionRequest,
)
from .parsing import merge_extra, transcript, transcript_events


class OpenAICompatible:
    name = "OpenAICompatible"
    DEFAULT_SETTINGS: dict[str, Any] = {}

    def __init__(self, connection: AudioConnection):
        url = httpx.URL(connection.base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.query or url.fragment:
            raise ValueError("Audio base_url must be an absolute HTTP(S) API base without query or fragment.")
        if isinstance(connection.timeout, bool) or not math.isfinite(connection.timeout) or connection.timeout <= 0:
            raise ValueError("Audio timeout must be finite and positive.")
        self._connection = connection

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    @property
    def supported_operations(self) -> frozenset[AudioOperation]:
        return frozenset({"tts", "stt"})

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self._connection.api_key}"} if self._connection.api_key else {}
        return httpx.AsyncClient(
            base_url=self._connection.base_url.rstrip("/") + "/",
            headers=headers, timeout=self._connection.timeout, follow_redirects=False,
        )

    def _require(self, operation: AudioOperation) -> None:
        if operation not in self.supported_operations:
            raise AudioCapabilityError(f"Audio driver {self.name!r} does not support {operation}.")

    @staticmethod
    def _speech(request: SpeechRequest, *, stream: bool = False) -> dict[str, object]:
        options = request.options
        payload: dict[str, object] = {
            "model": request.model, "input": request.text,
            "response_format": options.response_format, "speed": options.speed,
        }
        for key, value in (("voice", request.voice), ("language", options.language), ("instructions", options.instructions)):
            if value is not None:
                payload[key] = value
        if stream:
            payload["stream"] = True
        return merge_extra(payload, options.extra, reserved={
            "model", "input", "voice", "language", "instructions", "response_format", "speed", "stream",
        })

    @staticmethod
    def _transcription(request: TranscriptionRequest, *, stream: bool = False) -> dict[str, str]:
        if request.options.input_options is not None:
            raise AudioCapabilityError("Input preprocessing requires AudioModelRequest; native drivers do not apply input_options.")
        payload: dict[str, object] = {"model": request.model, "response_format": "json"}
        for key, value in (("language", request.options.language), ("prompt", request.options.prompt)):
            if value is not None:
                payload[key] = value
        if stream:
            payload["stream"] = True
        merge_extra(payload, request.options.extra, reserved={"file", "model", "language", "prompt", "response_format", "stream"})
        result: dict[str, str] = {}
        for key, value in payload.items():
            if isinstance(value, str):
                result[key] = value
            elif isinstance(value, (bool, int, float)):
                result[key] = json.dumps(value)
            else:
                raise ValueError(f"Multipart audio option {key!r} must be a string, boolean or number.")
        return result

    async def tts(self, request: SpeechRequest) -> SpeechResult:
        payload = self._speech(request)
        async with self._client() as client:
            response = await client.post("audio/speech", json=payload)
            response.raise_for_status()
            if not response.content:
                raise AudioProtocolError("Speech response contains no audio bytes.")
            media_type = response.headers.get("content-type", "application/octet-stream").split(";")[0]
            if not (media_type.startswith("audio/") or media_type == "application/octet-stream"):
                raise AudioProtocolError(f"Speech response has unexpected media type {media_type!r}.")
            return SpeechResult(response.content, media_type, request.model)

    async def stt(self, request: TranscriptionRequest) -> TranscriptResult:
        data = self._transcription(request)
        async with self._client() as client:
            response = await client.post("audio/transcriptions", data=data, files={
                "file": (request.audio.filename, request.audio.data, request.audio.content_type),
            })
            response.raise_for_status()
            try:
                value = response.json()
            except ValueError as error:
                raise AudioProtocolError("Transcription response is not JSON.") from error
            return transcript(value, request.model)

    @asynccontextmanager
    async def stream_tts(self, request: SpeechRequest) -> AsyncIterator[AsyncIterator[bytes]]:
        self._require("stream_tts")
        payload = self._speech(request, stream=True)
        async with self._client() as client:
            async with client.stream("POST", "audio/speech", json=payload) as response:
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";")[0]
                if not media_type.startswith("audio/"):
                    raise AudioProtocolError("Streaming speech response is not audio.")

                async def chunks() -> AsyncIterator[bytes]:
                    received = False
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            received = True
                            yield chunk
                    if not received:
                        raise AudioProtocolError("Speech stream contains no audio bytes.")

                yield chunks()

    @asynccontextmanager
    async def stream_stt(self, request: TranscriptionRequest) -> AsyncIterator[AsyncIterator[TranscriptEvent]]:
        self._require("stream_stt")
        data = self._transcription(request, stream=True)
        async with self._client() as client:
            async with client.stream("POST", "audio/transcriptions", data=data, files={
                "file": (request.audio.filename, request.audio.data, request.audio.content_type),
            }) as response:
                response.raise_for_status()
                yield transcript_events(response)

    @asynccontextmanager
    async def stream_stt_input(
        self, chunks: AsyncIterable[bytes], *, model: str, audio_format: PCMFormat, options: TranscriptionOptions,
    ) -> AsyncIterator[AsyncIterator[TranscriptEvent]]:
        raise AudioCapabilityError(f"Audio driver {self.name!r} does not support realtime audio input.")
        yield  # pragma: no cover -- establishes the async context-manager shape
