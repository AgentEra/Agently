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

from collections.abc import AsyncGenerator, Generator
from typing import Any, Literal, TYPE_CHECKING, overload

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agently.types.data import (
        AgentlySpecificResultMessage,
        AgentExecutionMeta,
        AgentExecutionStreamData,
        OutputValidateHandler,
        RunContext,
    )


class AgentExecutionResult:
    """Reusable result facade for one AgentExecution."""

    def __init__(self, execution: Any):
        self.execution = execution
        self.execution_id = execution.id
        self.revision: int = getattr(execution, "revision", 0)

    @property
    def _view(self) -> Any:
        history = getattr(self.execution, "_revision_history", {})
        return history.get(self.revision, self.execution)

    @property
    def result(self) -> "AgentExecutionResult":
        return self

    @property
    def status(self) -> str:
        return str(self._view.status)

    @property
    def task_refs(self) -> dict[str, Any]:
        refs = getattr(self._view, "task_refs", None)
        return dict(refs) if isinstance(refs, dict) else {}

    @property
    def full_result_data(self) -> dict[str, Any]:
        raw_result = getattr(self._view, "result", None)
        if isinstance(raw_result, dict) and "result" in raw_result and "extra" in raw_result:
            return dict(raw_result)
        return {
            "result": raw_result,
            "extra": {
                "logs": getattr(self._view, "logs", {}),
                "task_refs": self.task_refs,
            },
        }

    @overload
    async def async_get_data(
        self,
        *,
        type: Literal["parsed"] = "parsed",
        ensure_keys: list[str],
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> dict[str, Any]: ...

    @overload
    async def async_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> Any: ...

    async def async_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> Any:
        return await self._view.async_get_data(
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
    def get_data(
        self,
        *,
        type: Literal["parsed"] = "parsed",
        ensure_keys: list[str],
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> dict[str, Any]: ...

    @overload
    def get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> Any: ...

    def get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> Any:
        return self._view.get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def async_get_full_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> Any:
        return await self._view.async_get_full_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    def get_full_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: Any = None,
    ) -> Any:
        return self._view.get_full_data(
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
    async def async_get_data_object(self) -> "BaseModel | None": ...

    @overload
    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
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
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel | None": ...

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel | None":
        return await self._view.async_get_data_object(
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
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
        parent_run_context: "RunContext | None" = None,
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
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel | None": ...

    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> "BaseModel | None":
        return self._view.get_data_object(
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def async_get_text(self, *, parent_run_context: Any = None) -> str:
        return await self._view.async_get_text(parent_run_context=parent_run_context)

    def get_text(self, *, parent_run_context: Any = None) -> str:
        return self._view.get_text(parent_run_context=parent_run_context)

    async def async_get_meta(self) -> "AgentExecutionMeta":
        return await self._view.async_get_meta()

    def get_meta(self) -> "AgentExecutionMeta":
        return self._view.get_meta()

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[tuple[str, "AgentExecutionStreamData"], None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator["AgentlySpecificResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["instant", "streaming_parse", "original"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator["AgentExecutionStreamData", None]: ...

    @overload
    def get_async_generator(self, *args: Any, **kwargs: Any) -> AsyncGenerator[str, None]: ...

    def get_async_generator(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        return self._view.get_async_generator(*args, **kwargs)

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[tuple[str, "AgentExecutionStreamData"], None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator["AgentlySpecificResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["instant", "streaming_parse", "original"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator["AgentExecutionStreamData", None, None]: ...

    @overload
    def get_generator(self, *args: Any, **kwargs: Any) -> Generator[str, None, None]: ...

    def get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return self._view.get_generator(*args, **kwargs)

    async def async_get_status(self) -> str:
        await self.async_get_meta()
        return str(self._view.status)

    def get_status(self) -> str:
        self.get_meta()
        return str(self._view.status)

    async def async_resume(self, *args: Any, **kwargs: Any) -> Any:
        task_id = kwargs.pop("task_id", None)
        explicit_task_id = task_id is not None
        remaining_args = args
        if task_id is None and remaining_args:
            task_id = remaining_args[0]
            explicit_task_id = True
            remaining_args = remaining_args[1:]
        if not explicit_task_id and not (
            self.task_refs.get("resumable", False) or self.task_refs.get("resume", False)
        ):
            task_id = None
        else:
            task_id = task_id or self.task_refs.get("task_id")
        if task_id:
            return await self._view.agent.async_resume(str(task_id), *remaining_args, **kwargs)
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "supported": False,
            "reason": "AgentExecutionResult has no resumable task_refs.",
        }

    def resume(self, *args: Any, **kwargs: Any) -> Any:
        task_id = kwargs.pop("task_id", None)
        explicit_task_id = task_id is not None
        remaining_args = args
        if task_id is None and remaining_args:
            task_id = remaining_args[0]
            explicit_task_id = True
            remaining_args = remaining_args[1:]
        if not explicit_task_id and not (
            self.task_refs.get("resumable", False) or self.task_refs.get("resume", False)
        ):
            task_id = None
        else:
            task_id = task_id or self.task_refs.get("task_id")
        if task_id:
            return self._view.agent.resume(str(task_id), *remaining_args, **kwargs)
        return {
            "execution_id": self.execution_id,
            "status": self.status,
            "supported": False,
            "reason": "AgentExecutionResult has no resumable task_refs.",
        }
