"""Speech consumption of one Execution's public output, separate from its producer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Any, Literal

from agently.core.application.AgentExecution.Stream import AgentExecutionTextDeltaProjector
from agently.core.model.AudioConfig import resolve_audio
from agently.types.data.audio import SpeechOptions, SpeechResult, TextSegmentOptions

SpeechScope = Literal["final", "all"]


def _scope(scope: SpeechScope) -> None:
    if scope not in {"final", "all"}:
        raise ValueError("say scope must be final or all.")


async def _text(owner: Any, scope: SpeechScope) -> AsyncGenerator[str, None]:
    if scope == "all":
        projector = AgentExecutionTextDeltaProjector()
        async with aclosing(owner.get_async_generator(type="all")) as stream:
            async for _, item in stream:
                meta = item.meta or {}
                kind = meta.get("stream_kind")
                if item.path == "result" or kind not in {"progress", "progress_delta", "snapshot", "guidance", "phase", "action_observation"}:
                    continue
                text = projector.project(item)
                if text:
                    yield text
    final = await owner.async_get_text()
    if final.strip():
        yield final


async def say(owner: Any, *, scope: SpeechScope, voice: str | None, options: SpeechOptions | None) -> SpeechResult | None:
    _scope(scope)
    audio = resolve_audio(owner, "tts")
    if not hasattr(owner, "_speech_lock"):
        owner._speech_lock = asyncio.Lock()
        owner._speech_results = {}
    key = (owner.revision, scope, voice, repr(options))
    async with owner._speech_lock:
        if key in owner._speech_results:
            result = owner._speech_results[key]
            if isinstance(result, BaseException):
                raise result
            return result
        try:
            text = "".join([chunk async for chunk in _text(owner, scope)])
            result = await audio.async_tts(text, voice=voice, options=options) if text.strip() else None
        except BaseException as error:
            owner._speech_results[key] = error
            raise
        owner._speech_results[key] = result
        return result


@asynccontextmanager
async def stream_say(
    owner: Any, *, scope: SpeechScope, voice: str | None, options: SpeechOptions | None,
    segments: TextSegmentOptions | None,
) -> AsyncIterator[AsyncIterator[SpeechResult]]:
    _scope(scope)
    audio = resolve_audio(owner, "tts")
    if getattr(owner, "_speech_stream_revision", None) == owner.revision:
        raise RuntimeError("This execution's speech stream was already consumed; audio is not replayed automatically.")
    owner._speech_stream_revision = owner.revision
    owned_start = not owner._started
    try:
        async with aclosing(_text(owner, scope)) as text:
            async with audio.stream_tts_with_auto_break(text, voice=voice, options=options, segments=segments) as chunks:
                yield chunks
    finally:
        if owned_start and owner._started and not owner._completed:
            await owner.async_cancel(reason="speech_consumer_closed")
