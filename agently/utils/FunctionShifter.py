# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from agently_stage import Stage, default_stage_call_bridge

from .CallableUtils import filter_callable_options
from .DeprecationWarnings import DeprecationWarnings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Iterator

    from agently_stage import StageHandle

P = ParamSpec("P")
R = TypeVar("R")


def _warn(method: str) -> None:
    replacement = (
        "Stage.as_sync/as_async, StageCallBridge for advanced bridging, or filter_callable_options"
        if method in {"syncify", "asyncify"}
        else "Agently StageCallBridge or filter_callable_options"
    )
    DeprecationWarnings.warn_deprecated_once(
        f"FunctionShifter.{method}",
        f"FunctionShifter.{method} is deprecated; use {replacement}",
        stacklevel=3,
    )


class FunctionShifter:
    """Deprecated compatibility facade over Agently-Stage adapters."""

    @staticmethod
    def run_async_func_in_thread(
        func: Callable[..., Coroutine[Any, Any, R]],
        *args: Any,
        **kwargs: Any,
    ) -> R:
        _warn("run_async_func_in_thread")
        return default_stage_call_bridge.as_sync(func)(*args, **kwargs)

    @staticmethod
    def syncify(func: Callable[P, R | Awaitable[R]]) -> Callable[P, R]:
        _warn("syncify")
        return Stage.as_sync(func)

    @staticmethod
    def asyncify(func: Callable[P, R | Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        _warn("asyncify")
        return Stage.as_async(func)

    @staticmethod
    def future(func: Callable[P, R | Awaitable[R]]) -> Callable[P, StageHandle[R]]:
        _warn("future")

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> StageHandle[R]:
            return default_stage_call_bridge.submit(func, *args, **kwargs)

        return wrapper

    @staticmethod
    def syncify_async_generator(async_gen: AsyncIterator[R]) -> Iterator[R]:
        _warn("syncify_async_generator")
        return default_stage_call_bridge.iter_sync(async_gen)

    @staticmethod
    def asyncify_sync_generator(sync_gen: Iterator[R]) -> AsyncGenerator[R, None]:
        _warn("asyncify_sync_generator")
        return default_stage_call_bridge.iter_async(sync_gen)

    @staticmethod
    def auto_options_func(func: Callable[P, R]) -> Callable[..., R]:
        _warn("auto_options_func")
        return filter_callable_options(func)
