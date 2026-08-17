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

import os
from typing import Any, AsyncGenerator, Literal, TYPE_CHECKING, overload, Generator, Mapping
from typing_extensions import Self

from agently.core.extension import ExtensionHandlers
from agently.core.runtime import resolve_parent_run_context
from agently.utils import Settings

from .Prompt import Prompt
from .ModelRequestRunner import ModelRequestRunner
from .ModelRequestResult import ModelRequestResult
from .ModelResponse import ModelResponse
from .AttachmentInput import ImageDetail, build_image_attachment

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agently.core import PluginManager
    from agently.types.data import (
        InstantStreamingContentType,
        AgentlyModelResultMessage,
        AgentlyOriginalResultPayload,
        AgentlySpecificResultMessage,
        OutputValidateHandler,
        PromptStandardSlot,
        ResultContentType,
        RunContext,
        SpecificEvents,
        StreamingData,
    )


DEFAULT_SPECIFIC_EVENTS: "SpecificEvents" = [
    "reasoning_delta",
    "delta",
    "reasoning_done",
    "done",
    "tool_calls",
]

_UNSET = object()


def _resolve_quick_prompt_input(
    prompt: Any = _UNSET,
    value: Any = _UNSET,
    mappings: dict[str, Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    extra_items = dict(kwargs or {})

    if value is _UNSET:
        if extra_items:
            if prompt is _UNSET or prompt is None:
                return extra_items, mappings
            if isinstance(prompt, Mapping):
                merged_prompt = dict(prompt)
                merged_prompt.update(extra_items)
                return merged_prompt, mappings
            raise TypeError("Keyword prompt pairs require no positional prompt, a mapping prompt, or a key/value pair.")
        if prompt is _UNSET:
            raise TypeError("Missing prompt value.")
        return prompt, mappings

    if not isinstance(prompt, str):
        raise TypeError("Key/value quick prompt input expects a string key.")

    prompt_data: dict[str, Any] = {prompt: value}
    if extra_items:
        prompt_data.update(extra_items)
    return prompt_data, mappings


class ModelRequest:
    def __init__(
        self,
        plugin_manager: "PluginManager",
        *,
        agent_name: str | None = None,
        agent_id: str | None = None,
        parent_settings: Settings | None = None,
        parent_prompt: Prompt | None = None,
        parent_extension_handlers: ExtensionHandlers | None = None,
        model_key: str | None = None,
    ) -> None:
        self.agent_name = agent_name if agent_name is not None else "Directly Request"
        self.agent_id = agent_id
        self.plugin_manager = plugin_manager
        self.settings = Settings(
            name="Request-Settings",
            parent=parent_settings,
        )
        self.prompt = Prompt(
            name="Request-Prompt",
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
            parent_prompt=parent_prompt,
        )
        self.extension_handlers = ExtensionHandlers(
            {
                "request_prefixes": [],
                "broadcast_prefixes": [],
                "broadcast_suffixes": [],
                "finally": [],
                "validate_handlers": [],
            },
            name="Request-ExtensionHandlers",
            parent=parent_extension_handlers,
        )
        self._model_key = model_key

        self.set_settings = self.settings.set_settings
        self.load_settings = self.settings.load

    def _create_request_run_context(
        self,
        response_id: str | None = None,
        *,
        parent_run_context: "RunContext | None" = None,
    ) -> "RunContext":
        from agently.types.data import RunContext

        parent_run_context = resolve_parent_run_context(parent_run_context)
        session_id = self.settings.get("runtime.session_id", None)
        if session_id is not None:
            session_id = str(session_id)
        return RunContext.create(
            run_kind="request",
            parent=parent_run_context,
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            session_id=session_id,
            response_id=response_id,
        )

    def set_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: tuple[type, str | None, str | None] | Any,
        *,
        mappings: dict[str, Any] | None = None,
    ) -> Self:
        self.prompt.set(key, value, mappings=mappings)
        return self

    # Quick Prompt
    def system(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.set("system", prompt, mappings=mappings)
        return self

    def rule(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.append("system", "{system.rule} ARE IMPORTANT RULES YOU SHALL FOLLOW!")
        self.prompt.set("system.rule", prompt, mappings=mappings)
        return self

    def role(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.append("system", "YOU MUST REACT AND RESPOND AS {system.your_role}!")
        self.prompt.set("system.your_role", prompt, mappings=mappings)
        return self

    def user_info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.append("system", "{system.user_info} IS IMPORTANT INFORMATION ABOUT USER!")
        self.prompt.set("system.user_info", prompt, mappings=mappings)
        return self

    def input(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.set("input", prompt, mappings=mappings)
        return self

    def info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.set("info", prompt, mappings=mappings)
        return self

    def instruct(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.set("instruct", prompt, mappings=mappings)
        return self

    def examples(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Self:
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        self.prompt.set("examples", prompt, mappings=mappings)
        return self

    def output(
        self,
        prompt: (
            dict[str, tuple[type, str | None, str, None] | Any]
            | list[tuple[type, str | None, str, None] | Any]
            | tuple[type, str | None, str, None]
            | Any
        ),
        *,
        mappings: dict[str, Any] | None = None,
        format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> Self:
        self.prompt.set("output", prompt, mappings=mappings)
        if format is not None:
            self.prompt.set("output_format", format)
        return self

    def attachment(
        self,
        prompt: list[dict[str, Any]],
        *,
        mappings: dict[str, Any] | None = None,
    ) -> Self:
        self.prompt.set("attachment", prompt, mappings=mappings)
        return self

    def image(
        self,
        *,
        question: str,
        file: str | os.PathLike[str] | None = None,
        url: str | None = None,
        files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
        urls: list[str] | tuple[str, ...] | None = None,
        detail: ImageDetail | None = None,
        mappings: dict[str, Any] | None = None,
    ) -> Self:
        self.prompt.set(
            "attachment",
            build_image_attachment(
                question=question,
                file=file,
                url=url,
                files=files,
                urls=urls,
                detail=detail,
            ),
            mappings=mappings,
        )
        return self

    def validate(self, handler: "OutputValidateHandler") -> Self:
        self.extension_handlers.append("validate_handlers", handler)
        return self

    # Result
    def _create_model_result(self, *, parent_run_context: "RunContext | None" = None) -> ModelRequestResult:
        if self._model_key:
            from agently.utils.ModelPool import resolve_model_pool_settings
            resolve_model_pool_settings(self._model_key, self.settings)
        parent_run_context = resolve_parent_run_context(parent_run_context)
        agent_execution_run_context = (
            parent_run_context
            if parent_run_context is not None and parent_run_context.run_kind == "agent_execution"
            else None
        )
        response = ModelRequestRunner(
            self.agent_name,
            self.plugin_manager,
            self.settings,
            self.prompt,
            self.extension_handlers,
            run_context=self._create_request_run_context(parent_run_context=parent_run_context),
            agent_execution_run_context=agent_execution_run_context,
        )
        response.run_context.response_id = response.id
        self.prompt.clear()
        return response.result

    def get_result(self, *, parent_run_context: "RunContext | None" = None) -> ModelRequestResult:
        return self._create_model_result(parent_run_context=parent_run_context)

    def get_response(self, *, parent_run_context: "RunContext | None" = None) -> ModelRequestResult:
        return self.get_result(parent_run_context=parent_run_context)

    def get_meta(self, *, parent_run_context: "RunContext | None" = None) -> dict[str, Any]:
        return self.get_result(parent_run_context=parent_run_context).get_meta()

    async def async_get_meta(self, *, parent_run_context: "RunContext | None" = None) -> dict[str, Any]:
        return await self.get_result(parent_run_context=parent_run_context).async_get_meta()

    def get_text(self, *, parent_run_context: "RunContext | None" = None) -> str:
        return self.get_result(parent_run_context=parent_run_context).get_text()

    async def async_get_text(self, *, parent_run_context: "RunContext | None" = None) -> str:
        return await self.get_result(parent_run_context=parent_run_context).async_get_text()

    @overload
    def get_data(
        self,
        *,
        type: Literal['parsed'],
        ensure_keys: list[str],
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> dict[str, Any]: ...

    @overload
    def get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any: ...

    def get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return default_stage_call_bridge.as_sync(self.async_get_data)(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        if ensure_all_keys is not None:
            self.prompt.set("ensure_all_keys", ensure_all_keys)
        result = self.get_result(parent_run_context=parent_run_context)
        return await result.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    @overload
    def get_data_object(
        self,
    ) -> "BaseModel | None": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str],
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel | None": ...

    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel | None":
        return default_stage_call_bridge.as_sync(self.async_get_data_object)(
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel | None":
        if ensure_all_keys is not None:
            self.prompt.set("ensure_all_keys", ensure_all_keys)
        result = self.get_result(parent_run_context=parent_run_context)
        return await result.async_get_data_object(
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    def start(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return self.get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def async_start(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await self.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    @overload
    def get_generator(
        self,
        type: "InstantStreamingContentType",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["StreamingData", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyModelResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlySpecificResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["original"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyOriginalResultPayload", None, None]: ...

    @overload
    def get_generator(
        self,
        type: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator: ...

    def get_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator:
        return self.get_result(parent_run_context=parent_run_context).get_generator(
            type=type,
            content=content,
            specific=specific,
        )  # type: ignore for `content` compatible

    @overload
    def get_async_generator(
        self,
        type: "InstantStreamingContentType",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["StreamingData", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyModelResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlySpecificResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["original"],
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyOriginalResultPayload", None]: ...

    @overload
    def get_async_generator(
        self,
        type: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator: ...

    def get_async_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator:
        return self.get_result(parent_run_context=parent_run_context).get_async_generator(
            type=type,
            content=content,
            specific=specific,
        )  # type: ignore for `content` compatible


__all__ = [
    "ModelRequest",
    "ModelRequestResult",
    "ModelResponse",
]
