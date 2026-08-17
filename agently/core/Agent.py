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

from agently_stage import default_stage_call_bridge

import json
import os
import uuid

from collections.abc import Mapping
from typing import Any, AsyncGenerator, Generator, Sequence, TYPE_CHECKING, Literal, cast, overload
from typing_extensions import Self

from agently.core.extension import ExtensionHandlers
from agently.core.application import AgentTask, DynamicTask
from agently.core.model.AttachmentInput import ImageDetail, build_image_attachment
from agently.core.model import ModelRequest, Prompt, _resolve_quick_prompt_input, _UNSET
from agently.core.model.ModelRequestResult import DEFAULT_SPECIFIC_EVENTS
from agently.core.runtime import resolve_parent_run_context
from agently.core.TaskWorkspace import TaskWorkspace
from agently.utils import DataFormatter, Settings
from agently.utils.LanguagePolicy import apply_language_policy_to_prompt, resolve_language_policy

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        AgentlyModelResultMessage,
        AgentlyOriginalResultPayload,
        AgentlySpecificResultMessage,
        InstantStreamingContentType,
        OutputValidateHandler,
        PromptStandardSlot,
        ChatMessage,
        ChatMessageDict,
        ResultContentType,
        RunContext,
        SerializableValue,
        SpecificEvents,
        StreamingData,
        TaskDAG,
    )
    from agently.types.config import AgentlyConfigModel
    from agently.core.model import ModelRequestResult
    from agently.types.options import ExecutionOptions
    from agently.types.plugins import AgentExecution


class _AgentDefinitionBuilder:
    def __init__(self, agent: "BaseAgent") -> None:
        self._agent = agent

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    def activate_model(self, model_key: str | None = None) -> Self:
        self._agent.activate_model(model_key)
        return self

    def set_settings(self, *args: Any, **kwargs: Any) -> Self:
        self._agent.set_settings(*args, **kwargs)
        return self

    def use_task_workspace(self, *args: Any, **kwargs: Any) -> Self:
        cast(Any, self._agent).use_task_workspace(*args, **kwargs)
        return self

    def configure_policy_approval(self, *args: Any, **kwargs: Any) -> Self:
        self._agent.configure_policy_approval(*args, **kwargs)
        return self

    def set_agent_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: Any,
        *,
        mappings: dict[str, Any] | None = None,
    ) -> Self:
        self._agent.set_agent_prompt(key, value, mappings=mappings)
        return self

    def system(self, prompt: Any, *, mappings: dict[str, Any] | None = None) -> Self:
        self._agent.system(prompt, mappings=mappings, always=True)
        return self

    def rule(self, prompt: Any, *, mappings: dict[str, Any] | None = None) -> Self:
        self._agent.rule(prompt, mappings=mappings, always=True)
        return self

    def role(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.role(*args, **kwargs)
        return self

    def user_info(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.user_info(*args, **kwargs)
        return self

    def input(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.input(*args, **kwargs)
        return self

    def info(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.info(*args, **kwargs)
        return self

    def instruct(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.instruct(*args, **kwargs)
        return self

    def examples(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.examples(*args, **kwargs)
        return self

    def output(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.output(*args, **kwargs)
        return self

    def attachment(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.attachment(*args, **kwargs)
        return self

    def image(self, *args: Any, **kwargs: Any) -> Self:
        kwargs["always"] = True
        self._agent.image(*args, **kwargs)
        return self

    def options(self, options: dict[str, Any]) -> Self:
        self._agent.options(options, always=True)
        return self

    def language(self, *args: Any, **kwargs: Any) -> Self:
        self._agent.language(*args, **kwargs)
        return self


class BaseAgent:
    def __init__(
        self,
        plugin_manager: "PluginManager",
        *,
        parent_settings: "Settings | None" = None,
        name: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.name = name if name is not None else self.id[:7]

        self.plugin_manager = plugin_manager
        self.settings = Settings(
            name=f"Agent-{ self.name }-Settings",
            parent=parent_settings,
        )
        self.agent_prompt = Prompt(
            name=f"Agent-{ self.name }-Prompt",
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
        )
        self.extension_handlers = ExtensionHandlers(
            {
                "request_prefixes": [],
                "broadcast_prefixes": [],
                "broadcast_suffixes": [],
                "finally": [],
                "validate_handlers": [],
            },
            name=f"Agent-{ self.name }-ExtensionHandlers",
        )
        self._active_model_key: str | None = None
        self.request = ModelRequest(
            agent_name=self.name,
            agent_id=self.id,
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
            parent_prompt=self.agent_prompt,
            parent_extension_handlers=self.extension_handlers,
        )
        self.request_prompt = self.request.prompt
        self.prompt = self.request_prompt

        self.load_settings = self.settings.load

    def set_settings(
        self,
        key: "str | AgentlyConfigModel",
        value: "SerializableValue | object" = _UNSET,
        *,
        auto_load_env: bool = False,
        raise_empty: bool = False,
    ) -> Self:
        if value is _UNSET:
            self.settings.set_settings(
                key,
                auto_load_env=auto_load_env,
                raise_empty=raise_empty,
            )
        else:
            self.settings.set_settings(
                key,
                value,
                auto_load_env=auto_load_env,
                raise_empty=raise_empty,
            )
        return self

    def configure_policy_approval(self, *, handler: str | None = None) -> Self:
        if handler is not None:
            self.settings.set("policy_approval.handler", str(handler))
        return self

    def language(
        self,
        language: Any = "auto",
        *,
        output: Any = None,
        process: Any = None,
        progress: Any = None,
        accept_language: Any = None,
    ) -> Self:
        policy = resolve_language_policy(
            language,
            output_language=output,
            process_language=process,
            progress_language=progress,
            accept_language=accept_language,
        )
        self.settings.set("agent.language_policy", cast(Any, dict(policy)))
        self.settings.set("agent_task.progress.language", policy.get("progress_language", policy.get("language", "auto")))
        apply_language_policy_to_prompt(self.agent_prompt, policy)
        return self

    def activate_model(self, model_key: str | None = None) -> Self:
        """Set the default model key for subsequent Agent-owned requests.

        The model key is resolved through the existing model_pool /
        key_pool_strategy / key_pool settings when a request is consumed.
        Passing None clears the active model key.
        """
        if model_key is None:
            self._active_model_key = None
            self.request._model_key = None
            return self
        normalized = str(model_key).strip()
        if not normalized:
            raise ValueError("activate_model(...) requires a non-empty model_key, or None to clear it.")
        self._active_model_key = normalized
        self.request._model_key = normalized
        return self

    def define(
        self,
        *,
        model: str | None = None,
        prompt: Mapping[str, Any] | Any | None = None,
        actions: Any = None,
        skills: Any = None,
        task_workspace: str | os.PathLike[str] | None = None,
        policy: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> _AgentDefinitionBuilder:
        builder = _AgentDefinitionBuilder(self)
        if model is not None:
            self.activate_model(model)
        if settings is not None:
            for key, value in settings.items():
                self.settings.set(str(key), value)
        if prompt is not None:
            if isinstance(prompt, Mapping):
                for key, value in prompt.items():
                    self.set_agent_prompt(str(key), value)
            else:
                self.set_agent_prompt("system", prompt)
        if actions is not None:
            use_actions = getattr(self, "use_actions", None)
            if not callable(use_actions):
                raise AttributeError("agent.define(actions=...) requires the Actions extension.")
            use_actions(actions, always=True)
        if skills is not None:
            use_skills = getattr(self, "use_skills", None)
            if not callable(use_skills):
                raise AttributeError("agent.define(skills=...) requires the Skills extension.")
            if isinstance(skills, (list, tuple)):
                for item in skills:
                    use_skills(item, always=True)
            else:
                use_skills(skills, always=True)
        if task_workspace is not None:
            cast(Any, self).use_task_workspace(task_workspace)
        if policy is not None:
            handler = policy.get("handler")
            self.configure_policy_approval(handler=str(handler) if handler is not None else None)
        for key, value in kwargs.items():
            if value is not None:
                self.settings.set(f"agent.define.{ key }", value)
        return builder

    # Create Request
    def create_request(
        self,
        *,
        name: str | None = None,
        inherit_agent_prompt: bool = True,
        inherit_extension_handlers: bool = True,
        model_key: str | None = None,
    ) -> ModelRequest:
        """
        Create a request instance.

        By default this method returns an isolated request that only inherits
        agent settings, which avoids pulling in agent prompt/session handlers
        implicitly.

        Set `inherit_agent_prompt` / `inherit_extension_handlers` to True when
        you intentionally want a request to reuse current agent context.
        """
        return ModelRequest(
            agent_name=name if name is not None else self.name,
            agent_id=self.id,
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
            parent_prompt=self.agent_prompt if inherit_agent_prompt else None,
            parent_extension_handlers=self.extension_handlers if inherit_extension_handlers else None,
            model_key=model_key if model_key is not None else self._active_model_key,
        )

    def create_temp_request(self, model_key: str | None = None) -> ModelRequest:
        return self.create_request(
            name=f"{ self.name }-Temp-{ uuid.uuid4().hex }",
            inherit_agent_prompt=False,
            inherit_extension_handlers=False,
            model_key=model_key,
        )

    _DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS = {
        "output",
        "output_format",
        "ensure_all_keys",
    }

    def _snapshot_request_prompt(self) -> dict[str, Any]:
        prompt_snapshot = self.request.prompt.get()
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, Mapping) else {}

    def _has_dynamic_task_prompt_context(self, prompt_snapshot: Mapping[str, Any]) -> bool:
        for key, value in prompt_snapshot.items():
            if key in self._DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS:
                continue
            if value not in (None, "", [], {}):
                return True
        return False

    def _has_dynamic_task_prompt_context_besides_input(self, prompt_snapshot: Mapping[str, Any]) -> bool:
        for key, value in prompt_snapshot.items():
            if key == "input" or key in self._DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS:
                continue
            if value not in (None, "", [], {}):
                return True
        return False

    def _render_dynamic_task_target_from_prompt(self, prompt_snapshot: Mapping[str, Any]) -> str | None:
        prompt_data = {
            key: value
            for key, value in prompt_snapshot.items()
            if key not in self._DYNAMIC_TASK_TARGET_EXCLUDED_PROMPT_KEYS
        }
        if not self._has_dynamic_task_prompt_context(prompt_data):
            return None
        prompt = Prompt(
            name=f"{ self.name }-DynamicTaskPromptSnapshot",
            plugin_manager=self.plugin_manager,
            parent_settings=self.settings,
            prompt_dict=dict(prompt_data),
        )
        text = str(prompt.to_text() or "").strip()
        text = text.removesuffix("assistant:").rstrip()
        text = text.removesuffix("[OUTPUT]:").rstrip()
        return text or None

    def _dynamic_task_prompt_defaults(
        self,
        target: Any = None,
        *,
        prompt_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = dict(prompt_snapshot or self._snapshot_request_prompt())
        original_snapshot = dict(snapshot)
        if target is not None:
            snapshot["input"] = target

        output_schema = snapshot.get("output")
        output_format = snapshot.get("output_format")
        input_value = snapshot.get("input")

        target_text: str | None = None
        if target is not None and not self._has_dynamic_task_prompt_context_besides_input(original_snapshot):
            target_text = str(target)
        elif (
            target is None
            and set(key for key, value in snapshot.items() if value not in (None, "", [], {}))
            <= {"input", "output", "output_format", "ensure_all_keys"}
            and isinstance(input_value, str)
            and input_value.strip()
        ):
            target_text = input_value.strip()
        else:
            target_text = self._render_dynamic_task_target_from_prompt(snapshot)

        if target_text is None and input_value is not None:
            if isinstance(input_value, str):
                target_text = input_value.strip() or None
            else:
                target_text = json.dumps(DataFormatter.sanitize(input_value), ensure_ascii=False)

        return {
            "target": target_text,
            "output_schema": output_schema,
            "output_format": output_format,
            "initial_graph_input": input_value,
        }

    def create_dynamic_task(
        self,
        target: str | None = None,
        *,
        plan: "TaskDAG | Mapping[str, Any] | None" = None,
        planner: Any = None,
        model: Any = None,
        actions: Any = None,
        skills: Any = None,
        handlers: Mapping[str, Any] | None = None,
        name: str | None = None,
        max_tasks: int | None = None,
        output_schema: Any = None,
        ensure_keys: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        _prompt_snapshot: Mapping[str, Any] | None = None,
    ) -> DynamicTask:
        prompt_defaults = self._dynamic_task_prompt_defaults(target, prompt_snapshot=_prompt_snapshot)
        resolved_target = target if target is not None and prompt_defaults["target"] is None else prompt_defaults["target"]
        if not resolved_target:
            raise ValueError("agent.create_dynamic_task(...) requires target=... or a configured agent.input(...).")
        resolved_output_schema = output_schema if output_schema is not None else prompt_defaults["output_schema"]
        resolved_output_format = output_format if output_format is not None else prompt_defaults["output_format"]
        initial_graph_input = prompt_defaults["initial_graph_input"]
        if _prompt_snapshot is None:
            self.request.prompt.clear()
        return DynamicTask(
            self.plugin_manager,
            str(resolved_target),
            plan=plan,
            planner=self if planner is None else planner,
            model=self if model is None else model,
            actions=actions,
            skills=skills,
            handlers=handlers,
            parent_settings=self.settings,
            name=name if name is not None else f"{ self.name }-DynamicTask",
            max_tasks=max_tasks,
            output_schema=resolved_output_schema,
            ensure_keys=ensure_keys,
            output_format=cast(Any, resolved_output_format),
            initial_graph_input=initial_graph_input,
        )

    def use_dynamic_task(
        self,
        *,
        mode: Literal["auto", "submitted"] = "auto",
        plan: "TaskDAG | Mapping[str, Any] | None" = None,
        planner: Any = None,
        model: Any = None,
        actions: Any = None,
        skills: Any = None,
        handlers: Mapping[str, Any] | None = None,
        name: str | None = None,
        max_tasks: int | None = None,
        output_schema: Any = None,
        ensure_keys: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        graph_input: Any = _UNSET,
        timeout: float | None = None,
        max_retries: int = 3,
    ) -> Self:
        raise ValueError(
            "Agent.use_dynamic_task(...) no longer registers a DynamicTask route for "
            "agent.start(), agent.async_start(), or AgentExecution.async_start(). "
            "Use Agently.create_dynamic_task(...) or direct TaskDAGExecutor(...) for "
            "independent DAG workflows."
        )

    def _create_agent_execution_run_context(
        self,
        *,
        parent_run_context: "RunContext | None" = None,
        execution_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "RunContext":
        from agently.types.data import RunContext

        parent_run_context = resolve_parent_run_context(parent_run_context)
        session_id = self.settings.get("runtime.session_id", None)
        if session_id is not None:
            session_id = str(session_id)
        run_meta = {"entrypoint": "agent"}
        if meta is not None:
            run_meta.update(meta)
        return RunContext.create(
            run_kind="agent_execution",
            parent=parent_run_context,
            agent_id=self.id,
            agent_name=self.name,
            session_id=session_id,
            execution_id=execution_id,
            meta=run_meta,
        )

    def _emit_agent_execution_started(self, agent_execution_run_context: "RunContext"):
        from agently.base import emit_runtime

        emit_runtime(
            {
                "event_type": "agent_execution.started",
                "source": "BaseAgent",
                "message": f"AgentExecution started for '{ self.name }'.",
                "payload": {
                    "agent_id": self.id,
                    "agent_name": self.name,
                },
                "run": agent_execution_run_context,
            }
        )

    async def _async_emit_agent_execution_started(self, agent_execution_run_context: "RunContext"):
        from agently.base import async_emit_runtime

        await async_emit_runtime(
            {
                "event_type": "agent_execution.started",
                "source": "BaseAgent",
                "message": f"AgentExecution started for '{ self.name }'.",
                "payload": {
                    "agent_id": self.id,
                    "agent_name": self.name,
                },
                "run": agent_execution_run_context,
            }
        )

    async def _async_emit_agent_execution_terminal_event(
        self,
        agent_execution_run_context: "RunContext",
        *,
        execution_id: str,
        status: str,
        route: str | None,
        strategy: str | None,
        task_refs: dict[str, Any],
        close_snapshot: dict[str, Any],
        terminal_status: Literal["completed", "failed", "cancelled"],
    ) -> None:
        from agently.base import async_emit_runtime

        event_type = f"agent_execution.{terminal_status}"
        await async_emit_runtime(
            {
                "event_type": event_type,
                "source": "BaseAgent",
                "message": f"AgentExecution {terminal_status} for '{ self.name }'.",
                "payload": {
                    "execution_id": execution_id,
                    "status": status,
                    "route": route,
                    "strategy": strategy,
                    "task_refs": DataFormatter.sanitize(task_refs),
                    "close_snapshot": DataFormatter.sanitize(close_snapshot),
                },
                "run": agent_execution_run_context,
                "meta": {
                    "execution_id": execution_id,
                    "route": route,
                    "strategy": strategy,
                },
            }
        )

    async def _async_emit_agent_execution_stream_event(
        self,
        agent_execution_run_context: "RunContext",
        *,
        execution_id: str,
        item: Any,
        execution_strategy: str | None,
        effective_execution_strategy: str | None,
    ) -> None:
        from agently.base import async_emit_runtime

        item_meta = dict(getattr(item, "meta", None) or {})
        stream_event_type = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        stream_kind = item_meta.get("stream_kind")
        payload = {
            "execution_id": execution_id,
            "path": getattr(item, "path", None),
            "value": DataFormatter.sanitize(getattr(item, "value", None)),
            "delta": getattr(item, "delta", None),
            "is_complete": getattr(item, "is_complete", None),
            "stream_event_type": stream_event_type,
            "source": getattr(item, "source", None),
            "route": getattr(item, "route", None),
            "stage_id": getattr(item, "stage_id", None),
            "task_id": getattr(item, "task_id", None),
            "action_id": getattr(item, "action_id", None),
            "graph_id": getattr(item, "graph_id", None),
            "stream_kind": stream_kind,
            "execution_strategy": execution_strategy,
            "effective_execution_strategy": effective_execution_strategy,
            "item": item.model_dump(mode="json") if hasattr(item, "model_dump") else DataFormatter.sanitize(item),
            "meta": DataFormatter.sanitize(item_meta),
        }
        await async_emit_runtime(
            {
                "event_type": "agent_execution.stream.delta" if stream_event_type == "delta" else "agent_execution.stream",
                "source": "BaseAgent",
                "message": str(getattr(item, "path", None) or ""),
                "payload": payload,
                "run": agent_execution_run_context,
                "meta": {
                    "execution_id": execution_id,
                    "path": getattr(item, "path", None),
                    "route": getattr(item, "route", None),
                    "source": getattr(item, "source", None),
                    "task_id": getattr(item, "task_id", None),
                    "action_id": getattr(item, "action_id", None),
                    "graph_id": getattr(item, "graph_id", None),
                    "stream_kind": stream_kind,
                    "high_frequency": stream_event_type == "delta",
                },
            }
        )

    def _emit_session_runtime_observation(
        self,
        kind: str,
        *,
        message: str,
        payload: dict[str, Any],
        run: "RunContext | None" = None,
        level: str = "INFO",
        error: BaseException | None = None,
    ):
        from agently.core.runtime import emit_session_observation

        emit_session_observation(
            {
                "kind": kind,
                "source": "SessionExtension",
                "level": level,
                "message": message,
                "payload": payload,
                "error": error,
                "run": run,
            }
        )

    async def _async_emit_session_runtime_observation(
        self,
        kind: str,
        *,
        message: str,
        payload: dict[str, Any],
        run: "RunContext | None" = None,
        level: str = "INFO",
        error: BaseException | None = None,
    ):
        from agently.core.runtime import async_emit_session_observation

        await async_emit_session_observation(
            {
                "kind": kind,
                "source": "SessionExtension",
                "level": level,
                "message": message,
                "payload": payload,
                "error": error,
                "run": run,
            }
        )

    def get_response(self, *, parent_run_context: "RunContext | None" = None) -> "ModelRequestResult":
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return self.request.get_response(parent_run_context=agent_execution_run_context)

    def get_result(self, *, parent_run_context: "RunContext | None" = None) -> "ModelRequestResult":
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return self.request.get_result(parent_run_context=agent_execution_run_context)

    def get_meta(self, *, parent_run_context: "RunContext | None" = None) -> dict[str, Any]:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return self.request.get_meta(parent_run_context=agent_execution_run_context)

    async def async_get_meta(self, *, parent_run_context: "RunContext | None" = None) -> dict[str, Any]:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_execution_started(agent_execution_run_context)
        return await self.request.async_get_meta(parent_run_context=agent_execution_run_context)

    def get_text(self, *, parent_run_context: "RunContext | None" = None) -> str:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return self.request.get_text(parent_run_context=agent_execution_run_context)

    async def async_get_text(self, *, parent_run_context: "RunContext | None" = None) -> str:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_execution_started(agent_execution_run_context)
        return await self.request.async_get_text(parent_run_context=agent_execution_run_context)

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
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return self.request.get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=agent_execution_run_context,
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
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_execution_started(agent_execution_run_context)
        return await self.request.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=agent_execution_run_context,
        )

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
    ) -> Any:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return self.request.get_data_object(
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=agent_execution_run_context,
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
    ) -> Any:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        await self._async_emit_agent_execution_started(agent_execution_run_context)
        return await self.request.async_get_data_object(
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=agent_execution_run_context,
        )

    @overload
    def get_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["StreamingData", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["AgentlyModelResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["AgentlySpecificResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator["AgentlyOriginalResultPayload", None, None]: ...

    @overload
    def get_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator: ...

    def get_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return cast(Any, self.request).get_generator(
            type=type,
            content=content,
            specific=specific,
            parent_run_context=agent_execution_run_context,
        )

    @overload
    def get_async_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["StreamingData", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["AgentlyModelResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["AgentlySpecificResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator["AgentlyOriginalResultPayload", None]: ...

    @overload
    def get_async_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator: ...

    def get_async_generator(
        self,
        type: "ResultContentType | None" = None,
        content: "ResultContentType | None" = None,
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator:
        agent_execution_run_context = self._create_agent_execution_run_context(parent_run_context=parent_run_context)
        self._emit_agent_execution_started(agent_execution_run_context)
        return cast(Any, self.request).get_async_generator(
            type=type,
            content=content,
            specific=specific,
            parent_run_context=agent_execution_run_context,
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
        return self.create_execution(parent_run_context=parent_run_context).get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
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
        return await self.create_execution(parent_run_context=parent_run_context).async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    def create_execution(
        self,
        *,
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: "ExecutionOptions | dict[str, Any] | None" = None,
        parent_run_context: "RunContext | None" = None,
    ) -> "AgentExecution":
        plugin_name = str(self.settings.get("plugins.AgentOrchestrator.activate", "AgentlyAgentOrchestrator"))
        plugin_class = cast(Any, self.plugin_manager.get_plugin("AgentOrchestrator", plugin_name))
        orchestrator = plugin_class(plugin_manager=self.plugin_manager, settings=self.settings)
        return orchestrator.create_execution(
            self,
            lineage=lineage,
            limits=limits,
            options=options,
            parent_run_context=parent_run_context,
        )

    def create_task(
        self,
        *,
        goal: str,
        success_criteria: list[str] | None = None,
        execution: Literal["auto", "flat", "taskboard"] | str | None = "auto",
        task_workspace: str | os.PathLike[str] | None = None,
        max_iterations: int | None = None,
        verify: Literal["before_done"] = "before_done",
        context_profile: str = "auto",
        context_budget: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> "AgentExecution":
        if task_workspace is not None:
            cast(Any, self).use_task_workspace(task_workspace)
        normalized_execution = AgentTask.normalize_execution_strategy(execution)
        resolved_task_id = task_id or f"agent_task_{uuid.uuid4().hex}"
        resolved_options = dict(options or {})
        agent_task_options = dict(resolved_options.get("agent_task") or {})
        language_policy = self.settings.get("agent.language_policy", None)
        if isinstance(language_policy, Mapping):
            agent_task_options.setdefault("language_policy", dict(language_policy))
        if agent_task_options:
            resolved_options["agent_task"] = agent_task_options
        task_options = {
            "goal": goal,
            "success_criteria": success_criteria,
            "execution": normalized_execution,
            "task_workspace": task_workspace,
            "max_iterations": max_iterations,
            "verify": verify,
            "context_profile": context_profile,
            "context_budget": context_budget,
            "limits": limits,
            "options": resolved_options if resolved_options else options,
            "task_id": resolved_task_id,
        }
        agent_execution = self.create_execution(
            lineage={"task_id": resolved_task_id},
            options={
                "strategy": "task",
                "task": {key: value for key, value in task_options.items() if value is not None},
            },
        )
        scoped_task_workspace_action_ids: list[str] = []
        if bool(agent_task_options.get("enable_task_workspace_readback_actions")) or bool(
            agent_task_options.get("enable_task_workspace_coding_actions")
        ):
            scoped_task_workspace_action_ids = self._enable_task_workspace_read_actions(
                task_workspace=agent_execution.task_workspace,
                coding_agent=bool(agent_task_options.get("enable_task_workspace_coding_actions")),
            )
        if scoped_task_workspace_action_ids:
            agent_execution.use_actions(scoped_task_workspace_action_ids)
        agent_execution.goal(goal, success_criteria)
        return agent_execution

    def _enable_task_workspace_read_actions(
        self,
        *,
        task_workspace: TaskWorkspace | None = None,
        coding_agent: bool = False,
    ) -> list[str]:
        task_workspace = task_workspace or getattr(self, "task_workspace", None)
        enable = getattr(
            self,
            "enable_coding_agent_actions"
            if coding_agent
            else "enable_task_workspace_file_actions",
            None,
        )
        if not isinstance(task_workspace, TaskWorkspace) or not callable(enable):
            return []
        execution_scope = str(task_workspace.execution_id or "").strip()
        if not execution_scope:
            return []
        safe_scope = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in execution_scope
        )
        action_prefix = f"task_workspace_{safe_scope}__"
        try:
            enable(
                root=task_workspace.root,
                task_workspace=task_workspace,
                read=True,
                write=bool(coding_agent),
                search=True,
                list_files=True,
                expose_to_model=True,
                action_prefix=action_prefix,
                max_file_bytes=50000,
                max_search_file_bytes=200000,
                desc=(
                    "Read and edit files written by this AgentTask, including trusted "
                    "TaskWorkspace deliverables and bounded evidence readbacks. Prefer "
                    "edit_file/apply_patch for targeted repairs."
                    if coding_agent
                    else (
                        "Read files written by this AgentTask, including trusted "
                        "TaskWorkspace deliverables and bounded evidence readbacks."
                    )
                ),
            )
        except Exception:
            return []
        registry = getattr(getattr(self, "action", None), "action_registry", None)
        has_action = getattr(registry, "has", None)
        if not callable(has_action):
            return []
        candidates = (
            "list_files",
            "read_file",
            "search_files",
            "glob_files",
            "grep_files",
            "write_file",
            "edit_file",
            "apply_patch",
        )
        scoped_candidates = [f"{action_prefix}{action_id}" for action_id in candidates]
        return [action_id for action_id in scoped_candidates if has_action(action_id)]

    async def async_resume(
        self,
        task_id: str,
        *,
        task_workspace: str | os.PathLike[str] | None = None,
    ) -> "AgentExecution":
        """Resume a previously checkpointed Agent task as an AgentExecution.

        Reads the task's latest durable snapshot from the RecordStore and returns
        a task-strategy AgentExecution draft. The returned execution continues
        from the iteration after the last completed one (or exposes the stored
        terminal result) through the normal AgentExecution result/meta/stream
        surface.
        """
        from agently.core.application import AgentTask

        normalized_task_id = str(task_id)
        task = await AgentTask.async_resume(
            cast(Any, self),
            normalized_task_id,
            task_workspace=task_workspace,
        )
        execution = self.create_execution(
            lineage={"task_id": normalized_task_id},
            options={
                "strategy": "task",
                "task": {
                    "task_id": normalized_task_id,
                    "task_workspace": task_workspace,
                    "resume": True,
                },
            },
        )
        execution.goal(task.goal, list(task.success_criteria))
        execution.record_store = getattr(self, "record_store", None)
        execution.task_record = task
        execution.task_refs = {
            "task_id": task.id,
            "strategy": "task",
            "resume": True,
            "resumed_from_iteration": getattr(task, "_resumed_from_iteration", 0),
        }
        return execution

    def resume(
        self,
        task_id: str,
        *,
        task_workspace: str | os.PathLike[str] | None = None,
    ) -> "AgentExecution":
        return default_stage_call_bridge.as_sync(self.async_resume)(task_id, task_workspace=task_workspace)

    async def async_resume_task(
        self,
        task_id: str,
        *,
        task_workspace: str | os.PathLike[str] | None = None,
    ) -> "AgentExecution":
        """Compatibility alias for async_resume(...)."""
        return await self.async_resume(task_id, task_workspace=task_workspace)

    def resume_task(
        self,
        task_id: str,
        *,
        task_workspace: str | os.PathLike[str] | None = None,
    ) -> "AgentExecution":
        return self.resume(task_id, task_workspace=task_workspace)

    def create_task_loop(
        self,
        *,
        goal: str,
        success_criteria: list[str] | None = None,
        execution: Literal["auto", "flat", "taskboard"] | str | None = "auto",
        task_workspace: str | os.PathLike[str] | None = None,
        max_iterations: int | None = None,
        verify: Literal["before_done"] = "before_done",
        context_profile: str = "auto",
        context_budget: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> "AgentExecution":
        agent_execution = self.create_task(
            goal=goal,
            success_criteria=success_criteria,
            execution=execution,
            task_workspace=task_workspace,
            max_iterations=max_iterations,
            verify=verify,
            context_profile=context_profile,
            context_budget=context_budget,
            limits=limits,
            options=options,
            task_id=task_id,
        )
        agent_execution.strategy("task_loop")
        return agent_execution

    def validate(self, handler: "OutputValidateHandler") -> Self:
        self.extension_handlers.append("validate_handlers", handler)
        return self

    # Basic Methods
    def set_agent_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: Any,
        *,
        mappings: dict[str, Any] | None = None,
    ) -> Self:
        self.agent_prompt.set(key, value, mappings=mappings)
        return self

    def remove_agent_prompt(self, key: "PromptStandardSlot | str") -> Self:
        self.agent_prompt.set(key, None)
        return self

    def remove_execution_prompt(self, key: "PromptStandardSlot | str") -> Self:
        self.request.prompt.set(key, None)
        return self

    def _replace_agent_prompt_value(self, key: "PromptStandardSlot | str", value: Any):
        if key in self.agent_prompt:
            del self.agent_prompt[key]
        self.agent_prompt.set(key, value)

    def reset_chat_history(self) -> Self:
        self._replace_agent_prompt_value("chat_history", [])
        return self

    def set_chat_history(self, chat_history: "Sequence[ChatMessage | ChatMessageDict]") -> Self:
        if not isinstance(chat_history, Sequence):
            chat_history = [chat_history]
        self._replace_agent_prompt_value("chat_history", chat_history)
        return self

    def add_chat_history(self, chat_history: "Sequence[ChatMessage | ChatMessageDict] | ChatMessageDict | ChatMessage") -> Self:
        if not isinstance(chat_history, Sequence):
            chat_history = [chat_history]
        self.agent_prompt.extend("chat_history", chat_history)
        return self

    def reset_action_results(self) -> Self:
        if "action_results" in self.agent_prompt:
            del self.agent_prompt["action_results"]
        return self

    def set_action_results(self, action_results: list[dict[str, Any]]) -> Self:
        self._replace_agent_prompt_value("action_results", action_results)
        return self

    def add_action_results(self, action: str, result: Any) -> Self:
        self.agent_prompt.append("action_results", {action: result})
        return self

    # Quick Prompt
    @overload
    def system(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
    ) -> Self: ...

    @overload
    def system(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

    def system(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ) -> "Self | AgentExecution":
        if always:
            self.agent_prompt.set("system", prompt, mappings=mappings)
            return self
        return self.create_execution().system(prompt, mappings=mappings)

    @overload
    def rule(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
    ) -> Self: ...

    @overload
    def rule(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

    def rule(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ) -> "Self | AgentExecution":
        if always:
            self.agent_prompt.append("instruct", "{system.rule} ARE IMPORTANT RULES YOU SHALL FOLLOW!")
            self.agent_prompt.set("system.rule", prompt, mappings=mappings)
            return self
        return self.create_execution().rule(prompt, mappings=mappings)

    @overload
    def role(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
        **kwargs: Any,
    ) -> Self: ...

    @overload
    def role(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
        **kwargs: Any,
    ) -> "AgentExecution": ...

    def role(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ) -> "Self | AgentExecution":
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.append("instruct", "YOU MUST REACT AND RESPOND AS {system.role}!")
            self.agent_prompt.set("system.your_role", prompt, mappings=mappings)
            return self
        return self.create_execution().role(prompt, mappings=mappings)

    @overload
    def user_info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
        **kwargs: Any,
    ) -> Self: ...

    @overload
    def user_info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
        **kwargs: Any,
    ) -> "AgentExecution": ...

    def user_info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ) -> "Self | AgentExecution":
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.append("instruct", "{system.user_info} IS IMPORTANT INFORMATION ABOUT USER!")
            self.agent_prompt.set("system.user_info", prompt, mappings=mappings)
            return self
        return self.create_execution().user_info(prompt, mappings=mappings)

    @overload
    def input(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
        **kwargs: Any,
    ) -> Self: ...

    @overload
    def input(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
        **kwargs: Any,
    ) -> "AgentExecution": ...

    def input(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ) -> "Self | AgentExecution":
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("input", prompt, mappings=mappings)
            return self
        return self.create_execution().input(prompt, mappings=mappings)

    @overload
    def info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
        **kwargs: Any,
    ) -> Self: ...

    @overload
    def info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
        **kwargs: Any,
    ) -> "AgentExecution": ...

    def info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ) -> "Self | AgentExecution":
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("info", prompt, mappings=mappings)
            return self
        return self.create_execution().info(prompt, mappings=mappings)

    @overload
    def instruct(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
        **kwargs: Any,
    ) -> Self: ...

    @overload
    def instruct(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
        **kwargs: Any,
    ) -> "AgentExecution": ...

    def instruct(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ) -> "Self | AgentExecution":
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("instruct", prompt, mappings=mappings)
            return self
        return self.create_execution().instruct(prompt, mappings=mappings)

    @overload
    def examples(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
        **kwargs: Any,
    ) -> Self: ...

    @overload
    def examples(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
        **kwargs: Any,
    ) -> "AgentExecution": ...

    def examples(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ) -> "Self | AgentExecution":
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent_prompt.set("examples", prompt, mappings=mappings)
            return self
        return self.create_execution().examples(prompt, mappings=mappings)

    @overload
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
        always: Literal[True],
        format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> Self: ...

    @overload
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
        always: Literal[False] = False,
        format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> "AgentExecution": ...

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
        always: bool = False,
        format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> "Self | AgentExecution":
        if always:
            self.agent_prompt.set("output", prompt, mappings=mappings)
            if format is not None:
                self.agent_prompt.set("output_format", format)
            return self
        return self.create_execution().output(prompt, mappings=mappings, format=format)

    @overload
    def attachment(
        self,
        prompt: list[dict[str, Any]],
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[True],
    ) -> Self: ...

    @overload
    def attachment(
        self,
        prompt: list[dict[str, Any]],
        *,
        mappings: dict[str, Any] | None = None,
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

    def attachment(
        self,
        prompt: list[dict[str, Any]],
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ) -> "Self | AgentExecution":
        if always:
            self.agent_prompt.set("attachment", prompt, mappings=mappings)
            return self
        return self.create_execution().attachment(prompt, mappings=mappings)

    @overload
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
        always: Literal[True],
    ) -> Self: ...

    @overload
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
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

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
        always: bool = False,
    ) -> "Self | AgentExecution":
        attachment = build_image_attachment(
            question=question,
            file=file,
            url=url,
            files=files,
            urls=urls,
            detail=detail,
        )
        if always:
            self.agent_prompt.set("attachment", attachment, mappings=mappings)
            return self
        return self.create_execution().attachment(attachment, mappings=mappings)

    @overload
    def options(
        self,
        options: dict[str, Any],
        *,
        always: Literal[True],
    ) -> Self: ...

    @overload
    def options(
        self,
        options: dict[str, Any],
        *,
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

    def options(
        self,
        options: dict[str, Any],
        *,
        always: bool = False,
    ) -> "Self | AgentExecution":
        if always:
            self.agent_prompt.set("options", options)
            return self
        return self.create_execution().set_prompt_options(options)

    def goal(self, goal: Any, success_criteria: Any = None) -> "AgentExecution":
        return self.create_execution().goal(goal, success_criteria=success_criteria)

    goals = goal

    def effort(self, value: Any = "medium", **strategy: Any) -> "AgentExecution":
        return self.create_execution().effort(value, **strategy)

    def route_policy(self, value: Any) -> "AgentExecution":
        return self.create_execution().route_policy(value)

    def strategy(self, value: str | None = None, **options: Any) -> "AgentExecution":
        return self.create_execution().strategy(value, **options)

    # Prompt
    def get_prompt_text(self) -> str:
        try:
            return self.request_prompt.to_text()[6:][:-11]
        except KeyError as error:
            raise RuntimeError(
                "`Agent.get_prompt_text()` only inspects prompt data kept on the Agent. "
                "`agent.input(...)` creates and returns an AgentExecution; assign that "
                "execution and call `execution.get_prompt_text()` to inspect one-run "
                "chain prompts."
            ) from error
