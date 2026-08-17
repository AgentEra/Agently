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

from collections.abc import Mapping
from typing import Any, TYPE_CHECKING, Literal, cast

from agently.core.orchestration.TaskDAG import (
    CompiledTaskDAG,
    TaskDAGContext,
    TaskDAGHandler,
    TaskDAGResolver,
    TaskDAGExecutor,
    TaskDAGValidation,
    TaskDAGValidator,
)
from agently.types.data import TaskDAG
from agently.core.model.ModelRequest import ModelRequest
from agently.utils import Settings

if TYPE_CHECKING:
    from agently.core import PluginManager


class ActionTaskAdapter:
    def __init__(self, action: Any):
        self.action = action

    async def __call__(self, context: TaskDAGContext):
        action_id = None
        if isinstance(context.task.binding, str):
            action_id = context.task.binding
        if not action_id and isinstance(context.task.inputs, Mapping):
            action_id = context.task.inputs.get("action_id") or context.task.inputs.get("name")
        if not action_id:
            action_id = context.task.id

        kwargs = {}
        if isinstance(context.task.inputs, Mapping):
            raw_kwargs = context.task.inputs.get("kwargs", context.task.inputs.get("action_input", context.task.inputs))
            kwargs = dict(raw_kwargs) if isinstance(raw_kwargs, Mapping) else {"value": raw_kwargs}

        if hasattr(self.action, "async_call_action"):
            return await self.action.async_call_action(str(action_id), kwargs)
        if hasattr(self.action, "async_execute_action"):
            return await self.action.async_execute_action(str(action_id), kwargs)
        if callable(self.action):
            return await default_stage_call_bridge.as_async(self.action)(context)
        raise TypeError("Action dynamic task requires an Action-like object or callable.")


class SkillTaskAdapter:
    def __init__(self, skill_task_runner: Any):
        self.skill_task_runner = skill_task_runner

    async def __call__(self, context: TaskDAGContext):
        if hasattr(self.skill_task_runner, "async_run_skills_task"):
            skill_id = context.task.binding if isinstance(context.task.binding, str) else context.task.id
            return await self.skill_task_runner.async_run_skills_task(
                context.task.purpose or context.task.title or context.task.id,
                skills=[skill_id],
            )
        if callable(self.skill_task_runner):
            return await default_stage_call_bridge.as_async(self.skill_task_runner)(context)
        raise TypeError("Skill dynamic task requires an AgentExecution adapter or callable.")


class DynamicTask:
    def __init__(
        self,
        plugin_manager: "PluginManager",
        target: str,
        *,
        plan: TaskDAG | Mapping[str, Any] | None = None,
        planner: Any = None,
        model: Any = None,
        actions: Any = None,
        skills: Any = None,
        handlers: Mapping[str, TaskDAGHandler] | None = None,
        parent_settings: Settings | None = None,
        name: str | None = None,
        max_tasks: int | None = None,
        output_schema: Any = None,
        ensure_keys: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        initial_graph_input: Any = None,
    ):
        if not target:
            raise ValueError("DynamicTask requires non-empty target.")

        self.plugin_manager = plugin_manager
        self.target = target
        self.plan_data = plan
        self.planner_source = planner
        self.model_source = model
        self.actions = actions
        self.skill_task_runner = skills
        self.handlers = dict(handlers or {})
        self.output_schema = output_schema
        self.ensure_keys = ensure_keys
        self.output_format = output_format
        self.initial_graph_input = initial_graph_input
        self.name = name if name is not None else "DynamicTask"
        self.settings = Settings(
            name=f"{ self.name }-Settings",
            parent=parent_settings,
        )
        self.resolver = self._make_resolver()
        self.validator = TaskDAGValidator(self.resolver)
        self.planner = self._create_planner(
            max_tasks=max_tasks,
        )
        self.executor = TaskDAGExecutor(
            self.resolver,
            name=self.name,
            validator=self.validator,
        )

        self.start = default_stage_call_bridge.as_sync(self.async_start)
        self.run = default_stage_call_bridge.as_sync(self.async_run)
        self.plan = default_stage_call_bridge.as_sync(self.async_plan)

    def _make_resolver(self) -> TaskDAGResolver:
        resolver = TaskDAGResolver()
        resolver.register("model", self._run_model_task)
        if self.actions is not None:
            resolver.register("action", ActionTaskAdapter(self.actions))
        if self.skill_task_runner is not None:
            resolver.register("skill", SkillTaskAdapter(self.skill_task_runner))
        for key, handler in self.handlers.items():
            handler_key = str(key).strip()
            if handler_key in {"model", "action", "skill", "validate", "approval", "artifact", "emit"}:
                raise ValueError(f"DynamicTask handler key '{ handler_key }' is reserved.")
            if not handler_key.endswith("_handler"):
                raise ValueError("DynamicTask custom handler keys must end with '_handler'.")
            resolver.register(handler_key, handler)
        return resolver

    def _create_planner(
        self,
        *,
        max_tasks: int | None = None,
    ):
        planner_name = str(
            self.settings.get(
                "plugins.TaskDAGPlanner.activate",
                "AgentlyTaskDAGPlanner",
            )
        )
        planner_class = cast(
            type[Any],
            self.plugin_manager.get_plugin("TaskDAGPlanner", planner_name),
        )
        return planner_class(
            self.settings,
            validator=self.validator,
            available_bindings=self._planner_available_bindings(),
            max_tasks=max_tasks,
        )

    def _planner_available_bindings(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in self.resolver.keys()
            if key not in {"validate", "emit"}
        )

    def _new_request(self, source: Any, name: str):
        if hasattr(source, "create_temp_request"):
            return source.create_temp_request()
        if hasattr(source, "create_request"):
            return source.create_request(name=name)
        if source is not None and not isinstance(source, Mapping):
            return source
        request = ModelRequest(
            self.plugin_manager,
            parent_settings=self.settings,
            agent_name=name,
        )
        if isinstance(source, Mapping):
            for key, value in source.items():
                request.set_settings(str(key), value)
        return request

    def _new_model_request(self, name: str):
        return self._new_request(self.model_source, name)

    def _new_planner_request(self, name: str):
        source = self.planner_source if self.planner_source is not None else self.model_source
        return self._new_request(source, name)

    async def _run_model_task(self, context: TaskDAGContext):
        request = self._new_model_request(f"{ self.name }-{ context.task.id }")
        if not hasattr(request, "input") or not hasattr(request, "async_start"):
            raise TypeError("Model dynamic task handler requires an Agent or ModelRequest-like object.")
        task_options = context.task.inputs if isinstance(context.task.inputs, Mapping) else {}
        should_apply_default_contract = self._should_apply_default_output_contract(
            context.graph,
            context.task.id,
        )
        output_schema = task_options.get("output_schema", task_options.get("output"))
        if output_schema is None and should_apply_default_contract:
            output_schema = self.output_schema
        task_output_format = task_options.get("output_format", self.output_format)
        output_format = (
            self._normalize_model_output_format(task_output_format, task_id=context.task.id)
            if task_output_format is not None
            else None
        )
        ensure_keys = task_options.get("ensure_keys")
        if ensure_keys is None and should_apply_default_contract:
            ensure_keys = self.ensure_keys
        # The host-supplied output_schema/ensure_keys define a structured frontstage
        # contract on the semantic-output node. A model-planned `flat_markdown`
        # format cannot carry a multi-field structured object, so its ensure_keys
        # could never be satisfied. Let the structural auto-selector pick a
        # compatible format instead of honoring the incompatible planner choice.
        if (
            should_apply_default_contract
            and output_format == "flat_markdown"
            and (self._is_structured_contract(output_schema) or self._is_structured_contract(ensure_keys))
        ):
            output_format = "auto"
        max_retries = task_options.get("max_retries")

        prepared_request = (
            request
            .input(
                {
                    "target": self.target,
                    "task": context.task.to_dict(),
                    "graph_input": context.graph_input,
                    "dependency_results": dict(context.dependency_results),
                }
            )
            .instruct(
                [
                    "Complete only the current dynamic graph task.",
                    "Use dependency_results as completed upstream task outputs.",
                    "Return the task result directly.",
                ]
            )
        )
        if output_schema is not None:
            if not hasattr(prepared_request, "output"):
                raise TypeError("Model dynamic task output_schema requires a request with .output(...).")
            prepared_request = prepared_request.output(output_schema, format=output_format)

        start_kwargs: dict[str, Any] = {}
        normalized_ensure_keys = self._normalize_ensure_keys(ensure_keys)
        if normalized_ensure_keys is not None:
            start_kwargs["ensure_keys"] = normalized_ensure_keys
        if isinstance(max_retries, int):
            start_kwargs["max_retries"] = max_retries
        if hasattr(prepared_request, "get_result") or hasattr(prepared_request, "get_response"):
            getter = getattr(prepared_request, "get_result", None) or getattr(prepared_request, "get_response")
            result = getter(parent_run_context=context.runtime_data.chunk_run_context)
            async for item in result.get_async_generator(type="instant"):
                await self._put_model_stream_item(context, item)
            return await result.async_get_data(**start_kwargs)
        return await prepared_request.async_start(**start_kwargs)

    def _normalize_model_output_format(
        self,
        value: Any,
        *,
        task_id: str,
    ) -> Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"]:
        output_format = str(value or "auto").strip()
        if output_format not in {"json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"}:
            raise ValueError(
                f"Dynamic Task model task '{ task_id }' received invalid output_format "
                f"'{ output_format }'. Expected one of: json, flat_markdown, hybrid, "
                "xml_field, yaml_literal, auto."
            )
        return cast(Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"], output_format)

    async def _put_model_stream_item(self, context: TaskDAGContext, item: Any) -> None:
        event_type = str(getattr(item, "event_type", "done"))
        if event_type not in {"delta", "done"}:
            event_type = "done"
        field_path = str(getattr(item, "path", "") or "model")
        await context.runtime_data.execution.async_put_into_stream(
            {
                "type": "task_dag.model_field",
                "action": event_type,
                "event_type": event_type,
                "graph_id": context.graph.graph_id,
                "task_id": context.task.id,
                "field_path": field_path,
                "value": getattr(item, "value", None),
                "delta": getattr(item, "delta", None),
                "is_complete": bool(getattr(item, "is_complete", event_type == "done")),
                "payload": {
                    "field_path": field_path,
                    "wildcard_path": getattr(item, "wildcard_path", None),
                    "indexes": getattr(item, "indexes", None),
                },
            },
            _skip_contract_validation=True,
        )

    def _should_apply_default_output_contract(self, graph: TaskDAG, task_id: str) -> bool:
        if self.output_schema is None and self.ensure_keys is None:
            return False
        semantic_task_ids = self._semantic_output_task_ids(graph.semantic_outputs)
        if semantic_task_ids:
            return task_id in semantic_task_ids
        depended_task_ids = {
            dep_id
            for task in graph.tasks
            for dep_id in task.depends_on
        }
        return task_id not in depended_task_ids

    @staticmethod
    def _semantic_output_task_ids(semantic_outputs: Any) -> set[str]:
        task_ids: set[str] = set()
        if isinstance(semantic_outputs, str):
            task_ids.add(semantic_outputs)
        elif isinstance(semantic_outputs, Mapping):
            for value in semantic_outputs.values():
                if isinstance(value, str):
                    task_ids.add(value)
                elif isinstance(value, Mapping):
                    task_id = value.get("task_id") or value.get("task") or value.get("id")
                    if task_id is not None:
                        task_ids.add(str(task_id))
        elif isinstance(semantic_outputs, (list, tuple, set)):
            for value in semantic_outputs:
                if isinstance(value, str):
                    task_ids.add(value)
                elif isinstance(value, Mapping):
                    task_id = value.get("task_id") or value.get("task") or value.get("id")
                    if task_id is not None:
                        task_ids.add(str(task_id))
        return task_ids

    @staticmethod
    def _is_structured_contract(schema: Any) -> bool:
        """True when a schema/ensure_keys describes a multi-field structured object.

        A flat_markdown response can carry a single free-text field but not a
        multi-key object; such a contract requires json/hybrid output.
        """
        if isinstance(schema, Mapping):
            return len(schema) > 1
        if isinstance(schema, (list, tuple, set)):
            return len([item for item in schema if item is not None]) > 1
        return False

    @staticmethod
    def _normalize_ensure_keys(ensure_keys: Any) -> list[str] | None:
        if ensure_keys is None:
            return None
        if isinstance(ensure_keys, str):
            return [ensure_keys]
        if isinstance(ensure_keys, (list, tuple, set)):
            return [str(item) for item in ensure_keys]
        return [str(ensure_keys)]

    def _graph_input(self, graph_input: Any = None):
        if graph_input is not None:
            return graph_input
        if self.initial_graph_input is not None:
            return self.initial_graph_input
        return {"target": self.target}

    async def async_plan(
        self,
        *,
        max_retries: int = 3,
    ) -> Any:
        if self.plan_data is not None:
            return self.plan_data
        request = self._new_planner_request(f"{ self.name }-Planner")
        self.plan_data = await self.planner.async_plan(
            request,
            {"target": self.target},
            max_retries=max_retries,
        )
        return self.plan_data

    def validate(
        self,
        graph: TaskDAG | Mapping[str, Any] | None = None,
        *,
        strict_schema_version: bool = False,
    ) -> TaskDAGValidation:
        target_graph = graph if graph is not None else self.plan_data
        if target_graph is None:
            raise ValueError("No dynamic graph task plan is available to validate.")
        return self.validator.validate(
            target_graph,
            strict_schema_version=strict_schema_version,
        )

    def compile(
        self,
        graph: TaskDAG | Mapping[str, Any] | None = None,
    ) -> CompiledTaskDAG:
        target_graph = graph if graph is not None else self.plan_data
        if target_graph is None:
            raise ValueError("No dynamic graph task plan is available to compile.")
        return self.executor.compile(target_graph)

    async def async_run(
        self,
        graph: TaskDAG | Mapping[str, Any] | None = None,
        graph_input: Any = None,
        *,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target_graph = graph if graph is not None else await self.async_plan()
        return await self.executor.async_run(
            target_graph,
            self._graph_input(graph_input),
            timeout=timeout,
            concurrency=concurrency,
            runtime_resources=runtime_resources,
        )

    async def async_start(
        self,
        *,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        graph = await self.async_plan(max_retries=max_retries)
        self.validate(graph, strict_schema_version=True)
        return await self.async_run(
            graph,
            timeout=timeout,
            concurrency=concurrency,
            runtime_resources=runtime_resources,
        )
