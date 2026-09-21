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

import os
from pathlib import Path
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping, Sequence
from typing import Any, Literal, Protocol, TYPE_CHECKING, runtime_checkable
from typing_extensions import overload
from agently.types.options import ExecutionOptions

from agently.types.data import (
    AgentlySpecificResultMessage,
    AgentArtifactHandler,
    AgentArtifactResult,
    AgentExecutionLineage,
    AgentExecutionLimits,
    AgentExecutionMeta,
    AgentExecutionEffort,
    AgentExecutionStreamData,
    AgentExecutionRecordPurpose,
    AgentExecutionRecordWrite,
    AgentExecutionStatus,
    AgentExecutionControlResult,
    AgentExecutionControlCapabilities,
    AgentExecutionStrategy,
    AgentInteractionHandler,
    AgentReviewHandler,
    AgentReviewResult,
    ContextBudget,
    ContextConsumption,
    ContextPackage,
    ContextReadIntent,
    OutputValidateHandler,
    RunContext,
    SkillMode,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager
    from agently.types.data.audio import SpeechOptions, SpeechResult, TextSegmentOptions
    from pydantic import BaseModel

    from agently.core.Agent import BaseAgent
    from agently.core.extension import PluginManager
    from agently.core.operation import Action
    from agently.core.application.SkillLibrary import SkillBinding
    from agently.utils import Settings
    from agently.core.application import AgentTask
    from agently.core.application.AgentExecution import (
        AgentExecutionContext,
        AgentExecutionResult,
        AgentExecutionStream,
    )
    from agently.core.context import TaskContext
    from agently.core.model import ModelRequest, Prompt
    from agently.core.TaskWorkspace import TaskWorkspace


@runtime_checkable
class AgentExecution(Protocol):
    """Response-style contract for one bounded Agent execution object."""

    required_agent_capabilities: tuple[str, ...]

    def require_agent_capability(self, name: str) -> object: ...

    id: str
    revision: int
    agent: BaseAgent
    plugin_manager: PluginManager
    settings: Settings
    _review_contract: dict[str, object]
    lineage: AgentExecutionLineage
    limits: AgentExecutionLimits
    options: Any
    effective_options: dict[str, Any]
    consumed_options: dict[str, Any]
    status: AgentExecutionStatus
    request: "ModelRequest"
    request_prompt: "Prompt"
    prompt: "Prompt"
    stream: "AgentExecutionStream"
    execution_context: "AgentExecutionContext"
    task_context: "TaskContext"
    task_workspace: "TaskWorkspace"
    record_store: Any
    task_refs: dict[str, Any]
    task_record: "AgentTask | None"
    name: str
    artifact_results: list[AgentArtifactResult]
    review_results: list[AgentReviewResult]
    result: object | None
    generated_success_criteria: list[str]
    strategy_name: str | None
    skill_bindings: list[SkillBinding]
    local_action_ids: list[str]
    local_skill_selectors: list[dict[str, Any]]
    task_options: dict[str, Any]
    prompt_snapshot: dict[str, Any]
    route_info: dict[str, Any]
    logs: dict[str, Any]
    diagnostics: dict[str, Any]
    record_refs: dict[str, Any]

    @property
    def action(self) -> Action: ...

    @property
    def goal_items(self) -> list[str]: ...

    @goal_items.setter
    def goal_items(self, value: list[str]) -> None: ...

    @property
    def success_criteria_items(self) -> list[str]: ...

    @success_criteria_items.setter
    def success_criteria_items(self, value: list[str]) -> None: ...

    def system(self, prompt: object, *, mappings: dict[str, object] | None = None, always: bool = False) -> "AgentExecution": ...

    def rule(self, prompt: object, *, mappings: dict[str, object] | None = None, always: bool = False) -> "AgentExecution": ...

    def role(
        self, prompt: object = ..., value: object = ..., *,
        mappings: dict[str, object] | None = None, always: bool = False,
        **kwargs: object,
    ) -> "AgentExecution": ...

    def user_info(
        self, prompt: object = ..., value: object = ..., *,
        mappings: dict[str, object] | None = None, always: bool = False,
        **kwargs: object,
    ) -> "AgentExecution": ...

    def examples(
        self, prompt: object = ..., value: object = ..., *,
        mappings: dict[str, object] | None = None, always: bool = False,
        **kwargs: object,
    ) -> "AgentExecution": ...

    def attachment(
        self, prompt: list[dict[str, object]], *,
        mappings: dict[str, object] | None = None, always: bool = False,
    ) -> "AgentExecution": ...

    def image(
        self, file: str | os.PathLike[str] | None = None, *, question: str | None = None,
        mode: Literal["vlm", "llm", "ocr"] = "vlm",
        url: str | None = None,
        files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
        urls: list[str] | tuple[str, ...] | None = None,
        detail: Literal["auto", "low", "high"] | None = None,
        mappings: dict[str, object] | None = None, always: bool = False,
    ) -> "AgentExecution": ...

    def language(
        self, language: object = "auto", *, output: object = None,
        process: object = None, progress: object = None,
        accept_language: object = None, always: bool = False,
    ) -> "AgentExecution": ...

    def set_prompt_options(self, options: dict[str, object], *, always: bool = False) -> "AgentExecution": ...

    def configure_options(self, options: ExecutionOptions | Mapping[str, object] | None) -> "AgentExecution": ...

    def route_policy(self, value: object) -> "AgentExecution": ...

    def access_control_policy(self, value: object) -> "AgentExecution": ...

    def required_skill_ids(self) -> list[str]: ...

    def required_action_ids(self) -> list[str]: ...

    def is_task_strategy(self) -> bool: ...

    def task_strategy_options(self) -> dict[str, Any]: ...

    def use_dynamic_task(self, *args: object, **kwargs: object) -> "AgentExecution": ...

    def get_response(self) -> "AgentExecutionResult": ...

    def get_prompt_text(self) -> str: ...

    def get_json_prompt(self, save_to: str | Path | None = None, *, encoding: str | None = "utf-8") -> str: ...

    def get_yaml_prompt(self, save_to: str | Path | None = None, *, encoding: str | None = "utf-8") -> str: ...

    async def async_meta(self) -> dict[str, Any]: ...

    def meta(self) -> Any: ...

    async def async_add_guidance(
        self, content: object, *, author: str | None = None,
        target: object = "task", meta: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def add_guidance(
        self, content: object, *, author: str | None = None,
        target: object = "task", meta: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def record_context_consumption(self, package: ContextPackage, *, request_id: str) -> ContextConsumption: ...

    async def bridge_agent_task_stream_item(self, item: object, *, route: str = "agent_task") -> None: ...

    async def bridge_model_stream_item(
        self, item: object, *, route: str, source: str = "model_request",
        path_prefix: str | None = None, stage_id: str | None = None,
        task_id: str | None = None, action_id: str | None = None,
        graph_id: str | None = None, meta: dict[str, object] | None = None,
    ) -> None: ...

    def input(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def info(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def output(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def vlm_only(self, enabled: bool = True) -> "AgentExecution": ...

    def to_text(self, *, max_retries: int = 3) -> str: ...

    async def async_to_text(self, *, max_retries: int = 3) -> str: ...

    def say(self, *, scope: Literal["final", "all"] = "final", voice: str | None = None,
            options: "SpeechOptions | None" = None) -> "SpeechResult | None": ...

    async def async_say(self, *, scope: Literal["final", "all"] = "final", voice: str | None = None,
                        options: "SpeechOptions | None" = None) -> "SpeechResult | None": ...

    def stream_say(self, *, scope: Literal["final", "all"] = "final", voice: str | None = None,
                   options: "SpeechOptions | None" = None, segments: "TextSegmentOptions | None" = None,
                   ) -> "AbstractAsyncContextManager[AsyncIterator[SpeechResult]]": ...

    def use_system_one(self, enabled: bool = True) -> "AgentExecution":
        """Override automatic activation from system_one model configuration."""
        ...

    def auto_continue(self, enabled: bool = True) -> "AgentExecution":
        """Continue unfinished model output when needed; configure before start.

        Defaults off. Normal completion adds no continuation request. Does not
        select long_content, expand short answers, resume a task, or replace
        rework. Original output validation still applies to the final result.
        """
        ...

    def ensure_long_output(self, enabled: bool = True) -> "AgentExecution":
        """Compatibility alias for auto_continue(); prefer the shorter name."""
        ...

    def instruct(self, *args: Any, **kwargs: Any) -> "AgentExecution": ...

    def set_execution_prompt(self, key: Any, value: Any, *, mappings: dict[str, Any] | None = None) -> "AgentExecution": ...

    def remove_execution_prompt(self, key: Any) -> "AgentExecution": ...

    def goal(
        self,
        goal: str | list[str] | tuple[str, ...] | set[str],
        success_criteria: str | list[str] | tuple[str, ...] | set[str] | None = None,
        *,
        turn_on_long_task: bool = True,
    ) -> "AgentExecution":
        """Declare semantic goals; False leaves execution selection unchanged."""
        ...

    def goals(
        self,
        goal: str | list[str] | tuple[str, ...] | set[str],
        success_criteria: str | list[str] | tuple[str, ...] | set[str] | None = None,
        *,
        turn_on_long_task: bool = True,
    ) -> "AgentExecution": ...

    def interact(self, handler: AgentInteractionHandler) -> "AgentExecution":
        """Bind one connected human-interaction handler to this execution."""
        ...

    def review(
        self, handler: AgentReviewHandler | None = None, *,
        rules: str | Sequence[str] | None = None,
        on_fail: Literal["warn", "block"] = "warn",
    ) -> "AgentExecution":
        """Review final output and artifacts using rules or a replacement handler.

        on_fail warns by default or blocks delivery with AgentReviewError.
        It does not change the evaluator's rubric or replay execution steps.
        """
        ...

    def artifact(
        self,
        path: str | os.PathLike[str],
        handler: AgentArtifactHandler | None = None,
    ) -> "AgentExecution":
        """Declare a TaskWorkspace-relative artifact to materialize after the run."""
        ...

    @overload
    def effort(
        self,
        value: Literal["minimal", "low", "fast", "medium", "normal", "high", "max"] = "medium",
        **strategy: object,
    ) -> "AgentExecution": ...

    @overload
    def effort(
        self,
        value: AgentExecutionEffort = "medium",
        **strategy: object,
    ) -> "AgentExecution": ...

    def effort(
        self,
        value: AgentExecutionEffort = "medium",
        **strategy: object,
    ) -> "AgentExecution": ...

    @overload
    def strategy(
        self,
        value: Literal["auto", "direct", "task", "task_loop", "long_task", "flat", "taskboard"] | None = None,
        **options: object,
    ) -> "AgentExecution": ...

    @overload
    def strategy(
        self,
        value: AgentExecutionStrategy | None = None,
        **options: object,
    ) -> "AgentExecution": ...

    def strategy(
        self,
        value: AgentExecutionStrategy | None = None,
        **options: object,
    ) -> "AgentExecution": ...

    def use_actions(self, actions: object) -> "AgentExecution":
        """Attach Actions to this execution only."""
        ...

    def use_action(self, actions: object) -> "AgentExecution":
        """Attach one Action to this execution only."""
        ...

    def require_actions(self, actions: object) -> "AgentExecution":
        """Require Actions during this execution."""
        ...

    def use_tools(self, tools: object) -> "AgentExecution":
        """Compatibility alias for ``use_actions(...)``."""
        ...

    def use_tool(self, tools: object) -> "AgentExecution":
        """Compatibility alias for ``use_action(...)``."""
        ...

    def create_execution(
        self, *,
        lineage: AgentExecutionLineage | dict[str, object] | None = None,
        limits: AgentExecutionLimits | dict[str, object] | None = None,
        options: ExecutionOptions | Mapping[str, object] | None = None,
        parent_run_context: RunContext | None = None,
    ) -> "AgentExecution": ...

    def get_result(self, *, revision: int | None = None) -> "AgentExecutionResult": ...

    @property
    def control_capabilities(self) -> AgentExecutionControlCapabilities: ...

    async def async_rework(self, feedback: str, *, max_reworks: int = 3, allow_replay: bool = False) -> object: ...

    def rework(self, feedback: str, *, max_reworks: int = 3, allow_replay: bool = False) -> object: ...

    async def async_pause(self) -> AgentExecutionControlResult: ...

    async def async_interrupt(self, content: str, *, author: str | None = None) -> dict[str, object]: ...

    def interrupt(self, content: str, *, author: str | None = None) -> dict[str, object]: ...

    def pause(self) -> AgentExecutionControlResult: ...

    async def async_resume(self) -> object: ...

    def resume(self) -> object: ...

    def save(self) -> dict[str, object]: ...

    async def async_save(self) -> dict[str, object]: ...

    def load(self, snapshot: Mapping[str, object]) -> "AgentExecution": ...

    async def async_load(self, snapshot: Mapping[str, object]) -> "AgentExecution": ...

    async def async_cancel(
        self, *, reason: str = "cancelled", timeout: float | None = None,
    ) -> AgentExecutionControlResult: ...

    def cancel(
        self, *, reason: str = "cancelled", timeout: float | None = None,
    ) -> AgentExecutionControlResult: ...

    async def async_close(
        self, *, reason: str = "closed", timeout: float | None = None,
        pending: Literal["error", "cancel"] = "error",
    ) -> AgentExecutionControlResult: ...

    def close(
        self, *, reason: str = "closed", timeout: float | None = None,
        pending: Literal["error", "cancel"] = "error",
    ) -> AgentExecutionControlResult: ...

    def validate(self, handler: OutputValidateHandler) -> "AgentExecution":
        """Hard-check the final returned output; never intermediate producer steps."""
        ...

    def create_dynamic_task(self, *args: Any, **kwargs: Any) -> Any: ...

    def use_skills(
        self,
        skills: object,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
    ) -> "AgentExecution": ...

    def require_skills(
        self,
        skills: object,
        *,
        auto_allow: bool = False,
    ) -> "AgentExecution": ...

    def use_skills_packs(
        self,
        skills_packs: object,
        *,
        mode: SkillMode = "model_decision",
    ) -> "AgentExecution": ...

    async def async_prepare_task_context(self) -> "TaskContext": ...

    async def async_read_task_context(
        self,
        *,
        consumer_id: str,
        phase: str,
        intent: str | ContextReadIntent | None = None,
        budget: ContextBudget | None = None,
    ) -> ContextPackage: ...

    def run_skills_task(self, *args: Any, **kwargs: Any) -> Any: ...

    async def async_run_skills_task(self, *args: Any, **kwargs: Any) -> Any: ...

    async def select_route(self) -> tuple[str, dict[str, Any]]: ...

    async def emit_stream(
        self, path: str, value: object, *, route: str | None = None,
        source: str | None = "agent_execution", stage_id: str | None = None,
        task_id: str | None = None, action_id: str | None = None,
        graph_id: str | None = None, is_complete: bool | None = None,
        event_type: Literal["delta", "done"] = "done", delta: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> AgentExecutionStreamData: ...

    async def close_streams(self) -> None: ...

    def start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    def run(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    async def async_start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    async def async_run(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    async def async_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    async def async_get_full_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    async def async_get_text(self, **kwargs: Any) -> str: ...

    async def async_get_meta(self) -> AgentExecutionMeta: ...

    async def async_streaming_print(self) -> None: ...

    async def async_record_data(
        self,
        *,
        purpose: AgentExecutionRecordPurpose = "process",
        collection: str = "observations",
        kind: str | None = "agent_execution_observation",
        content: Any = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        checkpoint: bool = False,
        checkpoint_state: dict[str, Any] | None = None,
        checkpoint_step_id: str | None = None,
        profile: str = "fast",
    ) -> AgentExecutionRecordWrite: ...

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
    ) -> AsyncGenerator[tuple[str, AgentExecutionStreamData], None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[AgentlySpecificResultMessage, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["instant", "streaming_parse", "original"],
        content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[AgentExecutionStreamData, None]: ...

    @overload
    def get_async_generator(self, *args: Any, **kwargs: Any) -> AsyncGenerator[str, None]: ...

    def get_async_generator(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]: ...

    def get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    def get_full_data(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> Any: ...

    @overload
    def get_data_object(self) -> "BaseModel | None": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str],
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel": ...

    @overload
    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel | None": ...

    def get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel | None": ...

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: OutputValidateHandler | list[OutputValidateHandler] | None = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: RunContext | None = None,
    ) -> "BaseModel | None": ...

    def get_text(self, **kwargs: Any) -> str: ...

    def get_meta(self) -> AgentExecutionMeta: ...

    def streaming_print(self) -> None: ...

    def record_data(
        self,
        *,
        purpose: AgentExecutionRecordPurpose = "process",
        collection: str = "observations",
        kind: str | None = "agent_execution_observation",
        content: Any = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        checkpoint: bool = False,
        checkpoint_state: dict[str, Any] | None = None,
        checkpoint_step_id: str | None = None,
        profile: str = "fast",
    ) -> AgentExecutionRecordWrite: ...

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
    ) -> Generator[tuple[str, AgentExecutionStreamData], None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[AgentlySpecificResultMessage, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["instant", "streaming_parse", "original"],
        content: Any = None,
        **kwargs: Any,
    ) -> Generator[AgentExecutionStreamData, None, None]: ...

    @overload
    def get_generator(self, *args: Any, **kwargs: Any) -> Generator[str, None, None]: ...

    def get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]: ...

    async def async_get_key_result(self, key: str, *, must_in_prompt: bool = False) -> object | None: ...

    def get_key_result(self, key: str, *, must_in_prompt: bool = False) -> object | None: ...

    def async_wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ) -> AsyncGenerator[tuple[str, object], None]: ...

    def wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ) -> Generator[tuple[str, object], None, None]: ...

    def on_key(self, key: str, handler: Callable[[object], object | Awaitable[object]]) -> "AgentExecution": ...

    def when_key(self, key: str, handler: Callable[[object], object | Awaitable[object]]) -> "AgentExecution": ...

    async def async_start_waiter(self, *, must_in_prompt: bool = False) -> list[tuple[str, object, object]]: ...

    def start_waiter(self, *, must_in_prompt: bool = False) -> list[tuple[str, object, object]]: ...


@runtime_checkable
class AgentStepExecutor(Protocol):
    """Restricted adapter for asking an Agent to perform one task-step execution."""

    async def async_execute_step(
        self,
        *,
        lineage: AgentExecutionLineage | dict[str, Any] | None = None,
        limits: AgentExecutionLimits | dict[str, Any] | None = None,
    ) -> AgentExecutionMeta: ...
