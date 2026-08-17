from __future__ import annotations

import threading
from typing import Any, Literal, TypeAlias

from agently_stage import Tunnel, TunnelSubscription


RuntimeStreamStart: TypeAlias = Literal["earliest", "latest"] | int


class RuntimeStreamCursor:
    """Agently-private cursor projection over one Stage Tunnel subscription."""

    def __init__(self, subscription: TunnelSubscription[Any]) -> None:
        self._subscription = subscription

    def __iter__(self) -> RuntimeStreamCursor:
        return self

    def __next__(self) -> Any:
        return next(self._subscription)

    def __aiter__(self) -> RuntimeStreamCursor:
        return self

    async def __anext__(self) -> Any:
        return await anext(self._subscription)

    @property
    def next_sequence(self) -> int:
        return self._subscription.next_sequence

    def close(self) -> None:
        self._subscription.close()

    async def async_close(self) -> None:
        await self._subscription.async_close()


class StageRuntimeStreamTransport:
    """Unbounded process-local TriggerFlow stream transport backed by Tunnel."""

    def __init__(self) -> None:
        self._tunnel: Tunnel[Any] = Tunnel(timeout=None, max_history=None)
        self._terminal_lock = threading.Lock()
        self._terminal = False
        self._failure: BaseException | None = None

    async def publish(self, item: Any) -> None:
        await self._tunnel.async_put(item)

    def close(self) -> None:
        with self._terminal_lock:
            if self._terminal:
                return
            self._terminal = True
        self._tunnel.close()

    def fail(self, error: BaseException) -> None:
        with self._terminal_lock:
            if self._terminal:
                return
            self._terminal = True
            self._failure = error
        self._tunnel.fail(error)

    def subscribe(
        self,
        *,
        start: RuntimeStreamStart = "earliest",
        timeout: float | None = None,
    ) -> RuntimeStreamCursor:
        return RuntimeStreamCursor(
            self._tunnel.subscribe(
                start=start,
                timeout=timeout,
            )
        )

    @property
    def is_terminal(self) -> bool:
        with self._terminal_lock:
            return self._terminal

    @property
    def failure(self) -> BaseException | None:
        with self._terminal_lock:
            return self._failure

    @property
    def retained_range(self) -> tuple[int, int]:
        return self._tunnel.retained_range
