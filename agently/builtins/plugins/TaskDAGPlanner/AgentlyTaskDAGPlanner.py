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
from copy import deepcopy
from typing import Any, cast

from agently.core.orchestration.TaskDAG import (
    _GRAPH_SCHEMA_VERSION,
    _TASK_ID_PATTERN,
    TaskDAGValidator,
)
from agently.types.plugins import TaskDAGPlanner
from agently.utils import SettingsNamespace


TASK_DAG_PLANNER_OUTPUT_SCHEMA: dict[str, Any] = {
    "graph_id": (
        str,
        "Stable graph id using lowercase words, digits, dash, or underscore.",
        True,
    ),
    "task_schema_version": (
        str,
        f"Task graph schema version. Use '{ _GRAPH_SCHEMA_VERSION }'.",
        True,
    ),
    "tasks": [
        {
            "id": (
                str,
                "Unique stable task id. Use letters, digits, underscore, dot, or dash. Do not renumber on retry.",
                True,
            ),
            "kind": (
                str,
                "Task kind such as model, action, local, artifact, approval, emit, or validate.",
                True,
            ),
            "title": (str, "Short human-readable task title."),
            "purpose": (str, "Why this task exists in the graph."),
            "depends_on": (
                ["str"],
                "List of upstream task ids. Use an empty list for root tasks.",
                True,
            ),
            "inputs": (
                dict,
                "Task-local static inputs. Do not include dependency results here.",
            ),
            "binding": (
                str,
                "Optional resolver entry name such as risk_check_handler when kind alone is not specific enough.",
            ),
            "produces": [
                {
                    "role": (str, "Semantic output or artifact role produced by this task.", True),
                    "type": (str, "Result type such as text, json, artifact_ref, table, or list."),
                }
            ],
            "side_effect_policy": (
                dict,
                "Side-effect declaration such as network, local_write, external_write, or credential_usage.",
            ),
            "fallback": (
                dict,
                "Fallback policy: {'on_error': 'skip'}, {'on_error': 'fail'}, or "
                "{'on_error': 'retry', 'max_attempts': N, 'then': 'skip'|'fail'} for "
                "idempotent tasks that may hit transient failures.",
            ),
            "approval": (
                dict,
                "Approval policy. Use {'required': true, 'type': 'human_approval'} when a task must pause.",
            ),
        }
    ],
    "semantic_outputs": (
        dict,
        "Map final deliverable role to source task id or {'task_id': '<id>'}.",
        True,
    ),
    "policies": (
        dict,
        "Graph-level policy for concurrency, fallback, retry, approval, and side effects.",
    ),
    "diagnostics": [
        {
            "level": (str, "info, warning, or repaired."),
            "message": (str, "Planner normalization note."),
        }
    ],
}
TASK_DAG_PLANNER_ENSURE_KEYS = (
    "graph_id",
    "task_schema_version",
    "tasks[*].id",
    "tasks[*].kind",
    "tasks[*].depends_on",
    "semantic_outputs",
)


class AgentlyTaskDAGPlanner(TaskDAGPlanner):
    name = "AgentlyTaskDAGPlanner"

    DEFAULT_SETTINGS = {
        "$mappings": {
            "path_mappings": {
                "AgentlyTaskDAGPlanner": "plugins.TaskDAGPlanner.AgentlyTaskDAGPlanner",
                "TaskDAGPlanner": "plugins.TaskDAGPlanner.AgentlyTaskDAGPlanner",
            },
        },
        "schema_version": _GRAPH_SCHEMA_VERSION,
        "available_bindings": [],
        "max_tasks": None,
    }

    def __init__(
        self,
        settings: Any = None,
        *,
        validator: TaskDAGValidator | None = None,
        resolver: Any = None,
        available_bindings: list[str] | tuple[str, ...] | None = None,
        max_tasks: int | None = None,
    ):
        if resolver is None and isinstance(settings, Mapping):
            resolver = settings
            settings = None
        plugin_settings = None
        if settings is not None:
            plugin_settings = SettingsNamespace(settings, f"plugins.TaskDAGPlanner.{ self.name }")
        schema_version = (
            str(plugin_settings.get("schema_version", _GRAPH_SCHEMA_VERSION))
            if plugin_settings is not None
            else _GRAPH_SCHEMA_VERSION
        )
        self.validator = (
            validator
            if validator is not None
            else TaskDAGValidator(resolver=resolver, schema_version=schema_version)
        )
        configured_bindings = cast(Any, plugin_settings).get("available_bindings", []) if plugin_settings is not None else []
        binding_source = available_bindings if available_bindings is not None else configured_bindings
        self.available_bindings = tuple(binding_source or sorted(self.validator.resolver.keys()))
        configured_max_tasks = (
            cast(Any, plugin_settings).get("max_tasks", None)
            if plugin_settings is not None
            else None
        )
        self.max_tasks = max_tasks if max_tasks is not None else configured_max_tasks

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def output_schema(self) -> dict[str, Any]:
        return deepcopy(TASK_DAG_PLANNER_OUTPUT_SCHEMA)

    def ensure_keys(self) -> list[str]:
        return list(TASK_DAG_PLANNER_ENSURE_KEYS)

    def validate_output(self, result: dict[str, Any], context: Any = None):
        return self.validator.validate_planner_output(result, context)

    def instructions(self) -> list[str]:
        constraints = [
            "Return one executable Task DAG object only.",
            f"Set task_schema_version to '{ self.validator.schema_version }'.",
            "Use stable task ids that match letters, digits, underscore, dot, or dash.",
            "Reference dependencies only by upstream task id in depends_on.",
            "Keep depends_on empty for root tasks and never create dependency cycles.",
            "Do not copy dependency result values into task.inputs. For direct runtime wiring use placeholders such as ${INIT.foo} for the initial graph input, ${DEPS.task_id.path} for dependency results, ${STATE.task_results.task_id.path} for execution state, or ${TRIGGER.result} for the raw TriggerFlow trigger payload; otherwise read dependency_results in the task handler/model prompt.",
            "Use semantic_outputs to map each final deliverable role to a source task id.",
            "Declare side_effect_policy for network, local_write, external_write, or credential_usage tasks.",
            "Add approval.required=true for side-effect tasks when graph policy requires approval.",
            "Do not mark ordinary model tasks as network side effects only because they call the model provider; the executor manages provider access.",
            "Keep approval empty for read-only model analysis, synthesis, drafting, validation, or final response tasks unless the user explicitly asks for a human approval gate.",
            "For model tasks, set task.inputs.output_format to json for compact machine-control outputs, action arguments, routing flags, numeric or boolean facts, model judges, dense nested arrays/objects, and strict extraction.",
            "For model tasks, set task.inputs.output_format to flat_markdown for flat string long text, code, HTML, SVG, Markdown, SQL, or template fields.",
            "For model tasks, set task.inputs.output_format to hybrid only as an explicit opt-in for long prose plus structured lists, tables, citations, metadata, or nested evidence when retry latency is acceptable.",
            "Use task.inputs.output_format=auto only when conservative schema-driven format selection and retry latency are acceptable.",
            "Do not invent executor handlers; use only task kinds or binding names that the resolver declares as available.",
        ]
        if self.available_bindings:
            constraints.append("Available task kinds and bindings: " + ", ".join(self.available_bindings) + ".")
        if self.max_tasks is not None:
            constraints.append(f"Use no more than { self.max_tasks } tasks unless the user explicitly asks for more.")
        return constraints

    def plugin_constraints(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema_version": self.validator.schema_version,
            "available_bindings": list(self.available_bindings),
            "task_id_pattern": _TASK_ID_PATTERN.pattern,
            "required_keys": self.ensure_keys(),
            "max_tasks": self.max_tasks,
            "request_contract": {
                "output_schema": "Task DAG v1 Agently output schema",
                "output_format": "json",
                "ensure_keys": self.ensure_keys(),
                "validate_handler": "validate_output",
                "retryable": True,
            },
            "validation": [
                "schema_version",
                "duplicate_task_ids",
                "missing_dependencies",
                "dependency_cycles",
                "binding_availability",
                "semantic_output_refs",
                "side_effect_approval_policy",
            ],
            "forbidden": [
                "unstable_or_generated_task_ids_on_retry",
                "dependency_results_inside_task_inputs",
                "task_kinds_or_bindings_without_resolver_entries",
                "implicit_external_side_effects",
                "agent_or_provider_assumptions_inside_executor",
            ],
        }

    def prepare_request(self, request: Any, graph_input: Any = None):
        prepared = request
        if graph_input is not None and hasattr(prepared, "input"):
            prepared = prepared.input(graph_input)
        if hasattr(prepared, "instruct"):
            prepared = prepared.instruct(self.instructions())
        if hasattr(prepared, "output"):
            prepared = prepared.output(self.output_schema(), format="json")
        if hasattr(prepared, "validate"):
            prepared = prepared.validate(self.validate_output)
        return prepared

    async def async_plan(
        self,
        request: Any,
        graph_input: Any = None,
        *,
        max_retries: int = 3,
    ) -> Any:
        prepared = self.prepare_request(request, graph_input)
        if not hasattr(prepared, "async_start"):
            raise TypeError("Task DAG planner request must provide async_start(...).")
        return await prepared.async_start(
            ensure_keys=self.ensure_keys(),
            validate_handler=self.validate_output,
            max_retries=max_retries,
        )

    def plan(
        self,
        request: Any,
        graph_input: Any = None,
        *,
        max_retries: int = 3,
    ) -> Any:
        return default_stage_call_bridge.as_sync(self.async_plan)(
            request,
            graph_input,
            max_retries=max_retries,
        )
