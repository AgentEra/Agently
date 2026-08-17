# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from agently_stage import default_stage_call_bridge

import asyncio
import inspect

from typing import Any, AsyncGenerator, Awaitable, Callable, Literal, TYPE_CHECKING, cast, overload, Generator, Mapping

from agently.core.runtime import bind_runtime_context
from agently.utils import DeprecationWarnings
from .ModelRequestResultDataFlow import ModelRequestResultDataFlow

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agently.core import Prompt, ExtensionHandlers
    from agently.core.extension.PluginManager import PluginManager
    from agently.utils import Settings
    from agently.types.data import (
        AgentlyModelResultMessage,
        AgentlyOriginalResultPayload,
        AgentlySpecificResultMessage,
        InstantStreamingContentType,
        OutputValidateContext,
        OutputValidateHandler,
        OutputValidateResult,
        OutputValidateResultDict,
        ResultContentType,
        RunContext,
        SpecificEvents,
        StreamingData,
    )
    from agently.types.plugins import ResponseParser


DEFAULT_SPECIFIC_EVENTS: "SpecificEvents" = [
    "reasoning_delta",
    "delta",
    "reasoning_done",
    "done",
    "tool_calls",
]


class ModelRequestResult:
    def __init__(
        self,
        agent_name: str,
        response_id: str,
        prompt: "Prompt",
        response_generator: AsyncGenerator["AgentlyModelResultMessage", None],
        plugin_manager: "PluginManager",
        settings: "Settings",
        extension_handlers: "ExtensionHandlers",
        *,
        request_run_context: "RunContext | None" = None,
        model_run_context: "RunContext | None" = None,
        attempt_index: int = 1,
    ):
        self.agent_name = agent_name
        self.plugin_manager = plugin_manager
        self.settings = settings
        self.request_run_context = request_run_context
        self.model_run_context = model_run_context
        self.run_context = request_run_context
        self.attempt_index = attempt_index
        ResponseParser = cast(
            type["ResponseParser"],
            self.plugin_manager.get_plugin(
                "ResponseParser",
                str(self.settings["plugins.ResponseParser.activate"]),
            ),
        )
        self._response_id = response_id
        self.id: str = response_id
        self.response_id: str = response_id
        self.result: ModelRequestResult = self
        self._extension_handlers = extension_handlers
        self._response_parser = ResponseParser(
            agent_name,
            response_id,
            prompt,
            response_generator,
            self.settings,
            run_context=self.model_run_context,
        )
        self._finally_handlers_ran = False
        self._finally_handlers_lock = asyncio.Lock()
        self._run_finally_handlers_once_sync = default_stage_call_bridge.as_sync(self._run_finally_handlers_once)
        self._drain_response_parser_observations_sync = default_stage_call_bridge.as_sync(
            self._drain_response_parser_observations
        )
        self.prompt = prompt
        self._auto_ensure_keys_cache: dict[str, list[str]] = {}
        self._auto_ensure_policies_cache: dict[str, dict[str, Literal["presence", "not_null"]]] = {}
        self._validate_outcome: dict[str, Any] | None = None
        self._validate_lock = asyncio.Lock()
        self._validate_handler_signature: tuple[int, ...] | None = None
        self._accepted_retry_result: ModelRequestResult | None = None
        self._data_flow = ModelRequestResultDataFlow(self)
        self.full_result_data = self._response_parser.full_result_data
        self._get_meta_sync = cast(Callable[[], dict[str, Any]], default_stage_call_bridge.as_sync(self.async_get_meta))
        self._get_text_sync = cast(Callable[[], str], default_stage_call_bridge.as_sync(self.async_get_text))
        self._get_data_sync = default_stage_call_bridge.as_sync(self.async_get_data)
        self._get_data_object_sync = default_stage_call_bridge.as_sync(self.async_get_data_object)

    async def _drain_response_parser_observations(self):
        drain = getattr(self._response_parser, "drain_runtime_observations", None)
        if not callable(drain):
            return
        observations = drain()
        if inspect.isawaitable(observations):
            observations = await observations
        if not isinstance(observations, list):
            return

        from agently.core.runtime.RuntimeEvents import async_emit_response_parser_observation

        run = self.model_run_context or self.request_run_context
        for observation in observations:
            if isinstance(observation, Mapping):
                await async_emit_response_parser_observation(
                    dict(observation),
                    agent_name=self.agent_name,
                    response_id=self._response_id,
                    run=run,
                )

    async def _run_finally_handlers_once(self):
        if self._finally_handlers_ran:
            return
        async with self._finally_handlers_lock:
            if self._finally_handlers_ran:
                return
            # Mark as executed before invoking handlers so handlers can safely
            # call result getters without re-entering this hook chain.
            self._finally_handlers_ran = True
            finally_handlers = self._extension_handlers.get("finally", [])
            with bind_runtime_context(
                parent_run_context=self.request_run_context,
                request_run_context=self.request_run_context,
                model_run_context=self.model_run_context,
                settings=self.settings,
            ):
                for handler in finally_handlers:
                    if inspect.iscoroutinefunction(handler):
                        await handler(
                            self,
                            self.settings,
                        )
                    elif inspect.isgeneratorfunction(handler):
                        for _ in handler(
                            self,
                            self.settings,
                        ):
                            pass
                    elif inspect.isasyncgenfunction(handler):
                        async for _ in handler(
                            self,
                            self.settings,
                        ):
                            pass
                    elif inspect.isfunction(handler):
                        handler(
                            self,
                            self.settings,
                        )

    async def _await_materialization(self, awaitable: Awaitable[Any], *, stage: str):
        return await self._data_flow.await_materialization(awaitable, stage=stage)

    @overload
    async def async_get_data(
        self,
        *,
        type: Literal['parsed'] = "parsed",
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        _retry_count: int = 0,
    ) -> dict[str, Any]: ...

    @overload
    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        _retry_count: int = 0,
    ) -> Any: ...

    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        _retry_count: int = 0,
    ) -> Any:
        return await self._data_flow.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            retry_count=_retry_count,
        )

    @overload
    def get_data(
        self,
        *,
        type: Literal['parsed'] = "parsed",
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        _retry_count: int = 0,
    ) -> dict[str, Any]: ...

    @overload
    def get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        _retry_count: int = 0,
    ) -> Any: ...

    def get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        _retry_count: int = 0,
    ) -> Any:
        return self._get_data_sync(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            _retry_count=_retry_count,
        )

    @overload
    async def async_get_data_object(
        self,
    ) -> "BaseModel | None": ...

    @overload
    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel": ...

    @overload
    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel | None": ...

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel | None":
        return await self._data_flow.async_get_data_object(
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    @overload
    def get_data_object(self) -> "BaseModel | None": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel | None": ...

    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ) -> "BaseModel | None":
        return self._get_data_object_sync(
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    async def async_get_meta(self) -> dict[str, Any]:
        if self._accepted_retry_result is not None:
            return await self._accepted_retry_result.async_get_meta()
        try:
            return await self._await_materialization(
                self._response_parser.async_get_meta(),
                stage="response_materialization",
            )
        finally:
            await self._drain_response_parser_observations()
            await self._run_finally_handlers_once()

    def get_meta(self) -> dict[str, Any]:
        return self._get_meta_sync()

    async def async_get_text(self) -> str:
        if self._accepted_retry_result is not None:
            return await self._accepted_retry_result.async_get_text()
        try:
            return await self._await_materialization(
                self._response_parser.async_get_text(),
                stage="final_response_text_materialization",
            )
        finally:
            await self._drain_response_parser_observations()
            await self._run_finally_handlers_once()

    def get_text(self) -> str:
        return self._get_text_sync()

    @overload
    def get_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["StreamingData", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyModelResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlySpecificResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyOriginalResultPayload", None, None]: ...

    @overload
    def get_generator(
        self,
        type: "ResultContentType | None" = "delta",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator: ...

    def get_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator:
        if type is None:
            if content is not None:
                DeprecationWarnings.warn_deprecated_once(
                    "ModelRequestResult.get_generator.content",
                    "Parameter `content` in method .get_generator() is  deprecated and will be removed in future "
                    "version, please use parameter `type` instead.",
                    stacklevel=2,
                )
                type = content
            else:
                type = "delta"
        parsed_generator = self._response_parser.get_generator(type=type, specific=specific)
        completed = False
        try:
            for data in parsed_generator:
                self._drain_response_parser_observations_sync()
                yield data
                self._drain_response_parser_observations_sync()
            completed = True
        finally:
            self._drain_response_parser_observations_sync()
            if completed:
                self._run_finally_handlers_once_sync()

    @overload
    def get_async_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["StreamingData", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyModelResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlySpecificResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyOriginalResultPayload", None]: ...

    @overload
    def get_async_generator(
        self,
        type: "ResultContentType | None" = "delta",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator: ...

    async def get_async_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator:
        if type is None:
            if content is not None:
                DeprecationWarnings.warn_deprecated_once(
                    "ModelRequestResult.get_async_generator.content",
                    "Parameter `content` in method .get_async_generator() is  deprecated and will be removed in "
                    "future version, please use parameter `type` instead.",
                    stacklevel=2,
                )
                type = content
            else:
                type = "delta"
        parsed_generator = self._response_parser.get_async_generator(type=type, specific=specific)
        completed = False
        try:
            async for data in parsed_generator:
                await self._drain_response_parser_observations()
                yield data
                await self._drain_response_parser_observations()
            completed = True
        finally:
            await self._drain_response_parser_observations()
            if completed:
                await self._run_finally_handlers_once()
