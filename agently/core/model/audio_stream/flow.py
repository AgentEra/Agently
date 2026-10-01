"""Pull-driven TriggerFlow composition. No background producer or replay queue."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Generic, TypeVar, cast

from agently.core.orchestration.TriggerFlow import TriggerFlow
from agently.types.trigger_flow import TriggerFlowRuntimeData

InputT = TypeVar("InputT")
R = TypeVar("R")
OutputT = TypeVar("OutputT")


class _PullFlow(Generic[InputT, R, OutputT]):
    """One live source/request/output adapter, passed as an explicit flow resource."""

    def __init__(
        self, source: AsyncIterator[InputT], request: Callable[[InputT], Awaitable[R]],
        deliver: Callable[[R], Iterator[OutputT]],
    ):
        self.source = source
        self.request = request
        self.deliver = deliver
        self.item: InputT | None = None
        self.result: R | None = None
        self.output: Iterator[OutputT] = iter(())
        self.ended = False
        self.busy = False
        self.closed = False
        # A per-stream definition avoids retaining executions in a global flow.
        self.flow = TriggerFlow(name="audio-stream")
        self.flow.when("audio.next").to(_read).to(_request).to(_deliver)
        self.dispatch: asyncio.Task[object] | None = None

    def __aiter__(self) -> AsyncIterator[OutputT]:
        return self

    async def __anext__(self) -> OutputT:
        if self.closed:
            raise StopAsyncIteration
        if self.busy:
            raise RuntimeError("Audio streams support one active consumer.")
        self.busy = True
        try:
            while True:
                try:
                    return next(self.output)
                except StopIteration:
                    if self.ended:
                        raise StopAsyncIteration from None
                    # Each demand is an explicit graph activation; reads, model
                    # calls and conversion are separate nodes, not a hidden
                    # provider/retry loop inside a chunk.
                    # SignalNet retains execution history. Keep each processing
                    # segment finite instead of retaining an infinite live log.
                    execution = self.flow.create_execution(
                        runtime_resources={"audio_stream": self}, record_store=False, auto_close=False,
                        skip_exceptions=False, intervention_mode=None,
                    )
                    try:
                        self.dispatch = await execution.async_emit_nowait("audio.next")
                        if self.dispatch is None:
                            raise RuntimeError("Audio segment dispatch was rejected.")
                        await self.dispatch
                    finally:
                        try:
                            await execution.async_close(reason="audio_segment_settled")
                        finally:
                            self.flow.remove_execution(execution)
                            self.dispatch = None
        except BaseException:
            # Failed and cancelled streams cannot be resumed by catching the
            # exception inside the context. In particular, never flush their tail.
            await self.aclose("settled")
            raise
        finally:
            self.busy = False

    async def aclose(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        if self.dispatch is not None and not self.dispatch.done():
            self.dispatch.cancel()
            await asyncio.gather(self.dispatch, return_exceptions=True)
        self.output = iter(())
        self.item = None
        self.result = None
        closer = getattr(self.source, "aclose", None)
        if closer is not None:
            await closer()


def _session(data: TriggerFlowRuntimeData) -> _PullFlow[object, object, object]:
    return cast(_PullFlow[object, object, object], data.require_resource("audio_stream"))


async def _read(data: TriggerFlowRuntimeData) -> None:
    session = _session(data)
    try:
        session.item = await anext(session.source)
    except StopAsyncIteration:
        session.item = None
        session.ended = True


async def _request(data: TriggerFlowRuntimeData) -> None:
    session = _session(data)
    if not session.ended:
        session.result = await session.request(session.item)
    session.item = None


async def _deliver(data: TriggerFlowRuntimeData) -> None:
    session = _session(data)
    if not session.ended:
        session.output = session.deliver(session.result)
    session.result = None


@asynccontextmanager
async def pull_flow(
    source: AsyncIterator[InputT], request: Callable[[InputT], Awaitable[R]], deliver: Callable[[R], Iterator[OutputT]],
) -> AsyncIterator[AsyncIterator[OutputT]]:
    stream = _PullFlow(source, request, deliver)
    reason = "consumer_closed"
    try:
        yield stream
        reason = "input_end" if stream.ended else "consumer_closed"
    except BaseException:
        reason = "failed_or_cancelled"
        raise
    finally:
        await stream.aclose(reason)
