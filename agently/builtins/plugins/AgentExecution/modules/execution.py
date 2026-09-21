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
import concurrent.futures
import inspect
import os
import threading
import uuid
from pathlib import Path
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping, Sequence
from typing import Any, Literal, TYPE_CHECKING, cast, overload

import json5
import yaml

from agently.core.application.AgentExecution import (
    AgentExecutionContext,
    AgentExecutionPromptDraft,
    AgentExecutionResult,
    AgentExecutionStream,
    RuntimeStageStallError,
    merge_stream_meta,
    normalize_execution_limits,
    normalize_execution_lineage,
)
from agently.core.application.SkillLibrary import (
    SkillBinding,
    SkillContextSource,
)
from agently.core.context import ModelRequestContextSelector, TaskContext
from agently.core.runtime.RuntimeContext import bind_runtime_context
from agently.core.TaskWorkspace import TaskWorkspace, TaskWorkspaceContextSource
from agently.types.data import (
    AgentExecutionStreamData,
    AgentExecutionEffort,
    AgentExecutionMeta,
    AgentExecutionStatus,
    AgentExecutionStrategy,
    ContextBudget,
    ContextConsumption,
    ContextReadIntent,
    SkillMode,
)
from agently.types.plugins import ContextSource
from agently.types.options import ExecutionOptions
from agently.utils import DataFormatter

from .bridges import (
    bridge_agent_task_stream_item as bridge_agent_task_stream_item_entry,
    bridge_model_stream_item as bridge_model_stream_item_entry,
    bridge_task_dag_stream_item as bridge_task_dag_stream_item_entry,
    record_action_log as record_action_log_entry,
    record_model_response_id as record_model_response_id_entry,
)
from .artifact import declare_artifact
from .interaction import _AgentInteractionProvider, declare_interaction
from .diagnostics import (
    initial_diagnostics,
    initial_record_refs,
    record_error_diagnostic,
    refresh_diagnostics,
)
from .limits import (
    await_route_with_limits,
    build_execution_stall_error,
    cancel_limited_task,
)
from .lifecycle import (
    cancel as cancel_execution, close as close_execution, run_owned,
    pause as pause_execution, resume as resume_execution,
)
from .snapshot import save as save_execution, load as load_execution
from agently.types.data.agent_execution import AgentExecutionControlResult, AgentExecutionControlCapabilities
from .result_views import (
    async_get_data as async_get_data_entry,
    async_get_data_object as async_get_data_object_entry,
    async_get_full_data as async_get_full_data_entry,
    async_get_meta as async_get_meta_entry,
    async_get_text as async_get_text_entry,
    get_async_generator as get_async_generator_entry,
    sync_generator as sync_generator_entry,
)
from .review import declare_review
from .production import ProductionOptions
from .goal_preparation import PreparedGoal
from .route_execution import async_execute_route, produce_default_route, start_execution
from .runtime_guidance import add_guidance as add_guidance_entry
from .runtime_guidance import interrupt as interrupt_execution, record_interrupt_consumption
from .routing import HybridRoutePlanner
from .state import (
    ExecutionOptionsState,
    apply_strategy_selection,
    apply_effort_strategy_limits,
    build_effective_options,
    configure_effort,
    configure_execution_options,
    is_task_strategy as state_is_task_strategy,
    load_strategy_state_from_options,
    normalize_options_state,
    record_consumed_option as state_record_consumed_option,
    route_options as state_route_options,
    task_goal as state_task_goal,
    task_success_criteria as state_task_success_criteria,
    task_target as state_task_target,
)
from .record_store_records import (
    append_record_ref,
    default_checkpoint_state,
    default_record_content,
    default_record_summary,
    record_data as record_data_entry,
    record_scope,
    record_source,
)

if TYPE_CHECKING:
    from agently.core.orchestration.TriggerFlow.Execution import TriggerFlowExecution
    from agently.core.operation import Action
    from agently.core.Agent import BaseAgent
    from agently.types.data import (
        AgentExecutionLineage,
        AgentExecutionLimits,
        AgentExecutionRecordPurpose,
        AgentExecutionRecordWrite,
        AgentArtifactHandler,
        AgentArtifactResult,
        AgentInteractionHandler,
        AgentReviewHandler,
        AgentReviewResult,
        OutputValidateHandler,
        RunContext,
    )
    from agently.core.application import DynamicTask
    from .artifact import _AgentArtifactDeclaration
    from .review import _AgentReviewDeclaration


class AgentExecution:
    """Unified execution draft, run owner, and result source for one Agent run."""

    name: str = "auto"
    producer_route: str | None = None
    supported_strategies: frozenset[str] | None = None
    DEFAULT_SETTINGS: dict[str, Any] = {}
    OPTIONS_SCHEMAS = {"execution": ExecutionOptions}
    required_agent_capabilities: tuple[str, ...] = ()

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: Any = None,
        parent_run_context: "RunContext | None" = None,
        request: Any = None,
    ):
        self.agent = getattr(agent, "_agent", agent)
        self._bound_agent_capabilities: dict[str, object] = self.agent._bind_required_capabilities(
            self.required_agent_capabilities,
        )
        self.plugin_manager = self.agent.plugin_manager
        self.settings = self.agent.settings
        self.request = self._resolve_request(agent, request)
        self.request_prompt = self.request.prompt
        self.prompt = self.request_prompt
        self._draft = AgentExecutionPromptDraft(self.agent, self.request)
        self.id = uuid.uuid4().hex
        self.revision = 0
        self._revision_history: dict[int, AgentExecution] = {}
        self._retained_meta: dict[str, Any] | None = None
        self._rework_limit: int | None = None
        self._rework_feedback: str | None = None
        self._rework_allow_replay = False
        self._producer_state: dict[str, Any] = {}
        self._resource_release_error: Exception | None = None
        self.lineage: "AgentExecutionLineage" = normalize_execution_lineage(lineage)
        self.limits: "AgentExecutionLimits" = normalize_execution_limits(limits)
        self._effort_applied_limits: set[str] = set()
        self.options: ExecutionOptionsState = normalize_options_state(self, options)
        self.task_refs: dict[str, Any] = {}
        self.task_record: Any = None
        self._goal_turn_on_long_task = False
        self.generated_success_criteria: list[str] = []
        self.local_action_ids: list[str] = []
        self.local_required_action_ids: list[str] = []
        self._task_context_dependent_action_ids: set[str] = set()
        self.local_skill_selectors: list[dict[str, Any]] = []
        self.local_skills_pack_selectors: list[dict[str, Any]] = []
        self._agent_task_step_overrides: dict[str, Any] = {}
        self.guidance_items: list[dict[str, Any]] = []
        self._pending_guidance: list[dict[str, Any]] = []
        self._guidance_sequence = 0
        self._guidance_lock = asyncio.Lock()
        self.task_options: dict[str, Any] = {}
        self.strategy_name: str | None = None
        self.inherited_task_execution_strategy: str | None = None
        self.inherited_effective_task_execution_strategy: str | None = None
        self.inherited_strategy_context_source: str | None = None
        self.effective_options: dict[str, Any] = {}
        self.consumed_options: dict[str, Any] = {}
        self.record_store: Any = getattr(self.agent, "record_store", None)
        bind_execution = getattr(self.record_store, "_bind_execution", None)
        if callable(bind_execution):
            execution_scope = {"execution_id": self.id}
            for key in ("task_id", "iteration_id", "step_id"):
                value = self.lineage.get(key)
                if value is not None:
                    execution_scope[key] = value
            self.record_store = bind_execution(
                self.id,
                scope=execution_scope,
                search_scope=execution_scope,
            )
        agent_task_workspace = getattr(self.agent, "task_workspace", None)
        if isinstance(agent_task_workspace, TaskWorkspace):
            self.task_workspace = agent_task_workspace._derive(
                execution_id=self.id,
            )
        else:
            raise RuntimeError("AgentExecution requires a TaskWorkspace binding before route selection.")
        self.task_context = TaskContext(
            task_id=self.id,
            context_id=f"agent_execution:{self.id}:context",
        )
        self._task_workspace_context_source = TaskWorkspaceContextSource(self.task_workspace)
        self._task_workspace_context_binding_id = self.task_context.attach(
            self._task_workspace_context_source,
            binding_id=f"task_workspace_binding:{self.id}",
            scope="execution",
        )
        from agently.core.storage import RecordStore, RecordStoreContextSource

        self._record_store_context_binding_id: str | None = None
        if isinstance(self.record_store, RecordStore):
            self._record_store_context_binding_id = self.task_context.attach(
                RecordStoreContextSource(self.record_store),
                binding_id=f"record_store_binding:{self.id}",
                scope="execution",
            )
        self.skill_library = getattr(self.agent, "skill_library", None)
        self.skill_bindings: list[SkillBinding] = []
        self._skill_context_binding_id: str | None = None
        self._session_memory_context_binding_id: str | None = None
        self._task_context_prompt_entry_ids: set[str] = set()
        self._task_context_prepared = False
        self.context_readers: dict[tuple[str, str], Any] = {}
        self.context_packages: list[Any] = []
        self.context_consumptions: list[ContextConsumption] = []
        self._nesting_depth, self._nesting_budget = self._resolve_nesting_state()
        self._parent_model_request_budget = self._resolve_parent_model_request_budget()
        self._load_inherited_strategy_context()
        self._interaction_handler: "AgentInteractionHandler | None" = None
        self._interaction_provider: _AgentInteractionProvider | None = None
        self.execution_context = AgentExecutionContext(
            execution_id=self.id,
            lineage=self.lineage,
            limits=self.limits,
            nesting_depth=self._nesting_depth,
            nesting_budget=self._nesting_budget,
            task_execution_strategy=self.inherited_task_execution_strategy,
            effective_task_execution_strategy=self.inherited_effective_task_execution_strategy,
            strategy_context_source=self.inherited_strategy_context_source,
            task_workspace=self.task_workspace,
            execution_exchange_provider=self._interaction_provider,
            parent_model_request_budget=self._parent_model_request_budget,
        )
        self.parent_run_context = parent_run_context
        self.agent_execution_run_context: "RunContext | None" = None
        self._agent_execution_started_emitted = False
        self.route_info: dict[str, Any] = {}
        self.route_plan: dict[str, Any] = {}
        self.close_snapshot: dict[str, Any] = {}
        self.logs: dict[str, Any] = {
            "model_response_ids": [],
            "action_logs": [],
            "artifact_refs": [],
            "route_logs": {},
        }
        self.diagnostics: dict[str, Any] = initial_diagnostics()
        self.record_refs: dict[str, Any] = initial_record_refs()
        self.result: Any = None
        self._terminal_inline_result: Any = None
        self._terminal_retained_refs: list[Any] = []
        self._terminal_retention_deferred = False
        self._terminal_retention_diagnostics: list[dict[str, Any]] = []
        self._terminal_task_handoff_refs: list[Any] = []
        self._terminal_selected_action_artifact_ids: set[str] = set()
        self._terminal_preserved_action_artifact_ids: set[str] = set()
        self._terminal_error_projection: dict[str, Any] | None = None
        self._terminal_status: Literal["completed", "failed", "cancelled"] | None = None
        self._model_request_result: Any = None
        self._ensure_long_output_enabled = False
        self._long_output_result_object: Any = None
        self._producer_result_object: Any = None
        self._restored_result_pending = False
        self._long_output_meta: dict[str, Any] = {}
        self.artifact_declarations: list["_AgentArtifactDeclaration"] = []
        self.artifact_results: list["AgentArtifactResult"] = []
        self.review_declarations: list["_AgentReviewDeclaration"] = []
        self._review_contract: dict[str, object] = {}
        self._prepared_goal: PreparedGoal | None = None
        self.review_results: list["AgentReviewResult"] = []
        self.status: AgentExecutionStatus = "created"
        self._started = False
        self._completed = False
        self._error: BaseException | None = None
        self._selected_route: tuple[str, dict[str, Any]] | None = None
        self._load_strategy_state_from_options()
        self.effective_options = self._build_effective_options()
        apply_effort_strategy_limits(self)
        self.effective_options = self._build_effective_options()
        self.prompt_snapshot: dict[str, Any] = self._snapshot_prompt()
        self.execution_prompt_snapshot: dict[str, Any] = self._snapshot_execution_prompt()

        self._start_lock = asyncio.Lock()
        self._run_admission_lock = threading.Lock()
        self._run_task: asyncio.Task[Any] | None = None
        self._run_loop: asyncio.AbstractEventLoop | None = None
        self._run_completion: concurrent.futures.Future[Any] | None = None
        self._cancel_requested = False
        self._closing = False
        self._closed = False
        self._pause_requested = False
        self._pause_boundary: str | None = None
        self._pause_flow: "TriggerFlowExecution[Any, Any, Any] | None" = None
        self._paused_continuation: Callable[[], Awaitable[tuple[str, object]]] | None = None
        self._continued_result: tuple[str, object] | None = None
        self._resuming = False
        self._production_options: ProductionOptions | None = None
        self._candidate_validation_handlers: list["OutputValidateHandler"] = []
        self._candidate_request_validated = False
        self._record_store_write_lock = asyncio.Lock()
        self.route_planner = HybridRoutePlanner(self.agent, prompt_snapshot=self.prompt_snapshot, execution=self)
        self.stream = AgentExecutionStream(
            execution_id=self.id,
            lineage=self.lineage,
        ).bind_execution(self)
        self.execution_context.set_progress_callback(self._publish_runtime_progress)
        self.execution_context.set_exchange_callback(self._publish_exchange_stream_item)
        self._seen_action_log_keys: set[str] = set()
        self._key_waiter_handlers: dict[str, list[Any]] = {}

        self._bind_result_sugar()

    def _bind_result_sugar(self) -> None:
        self.start = default_stage_call_bridge.as_sync(self.async_start)
        self.get_data = default_stage_call_bridge.as_sync(self.async_get_data)
        self.get_data_object = default_stage_call_bridge.as_sync(self.async_get_data_object)
        self.get_full_data = default_stage_call_bridge.as_sync(self.async_get_full_data)
        self.get_text = default_stage_call_bridge.as_sync(self.async_get_text)
        self.get_meta = default_stage_call_bridge.as_sync(self.async_get_meta)
        self.record_data = default_stage_call_bridge.as_sync(self.async_record_data)
        self.add_guidance = default_stage_call_bridge.as_sync(self.async_add_guidance)
        self.get_key_result = default_stage_call_bridge.as_sync(self.async_get_key_result)
        self.streaming_print = default_stage_call_bridge.as_sync(self.async_streaming_print)
        self.when_key = self.on_key
        self.get_generator = self._get_generator
        self.meta = self._compat_meta

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.agent, name)
        if not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any):
            # Agent methods with an ``always`` switch normally create a fresh
            # execution when called from the Agent. Once a fluent chain already
            # owns this execution, keep configuring that same chain instead of
            # silently replacing it and losing its request-local Prompt.
            if "always" not in kwargs:
                try:
                    if "always" in inspect.signature(attr).parameters:
                        kwargs["always"] = True
                except (TypeError, ValueError):
                    pass
            result = attr(*args, **kwargs)
            if result is self.agent:
                return self._reconfiguration_target()
            return result

        return wrapper

    def _clear_task_context_dependent_actions(self) -> None:
        action_ids = set(self._task_context_dependent_action_ids)
        if not action_ids:
            return
        self.local_action_ids = [
            action_id
            for action_id in self.local_action_ids
            if action_id not in action_ids
        ]
        self.local_required_action_ids = [
            action_id
            for action_id in self.local_required_action_ids
            if action_id not in action_ids
        ]
        self.execution_context.clear_skill_script_exec_authorizations(action_ids)
        self._task_context_dependent_action_ids.clear()
        self._sync_action_scope(source="AgentExecution.task_context_invalidated")

    def _reconfiguration_target(
        self,
        *,
        invalidate_task_context: bool = True,
    ) -> "AgentExecution":
        if not self._started:
            if invalidate_task_context:
                self._task_context_prepared = False
                self._clear_task_context_dependent_actions()
            return self
        raise RuntimeError(
            "AgentExecution represents one independent run and has already started. "
            "Create a fresh execution for the next run with `agent.input(...)`, "
            "`agent.create_execution(...)`, or `execution.create_execution(...)`."
        )

    def _fork_for_reconfiguration(self) -> "AgentExecution":
        fork = cast(
            AgentExecution,
            self.agent.create_execution(
                lineage=dict(self.lineage),
                limits=dict(self.limits),
                options=dict(self.options),
                parent_run_context=self.parent_run_context,
            ),
        )
        prompt_snapshot = dict(self.execution_prompt_snapshot)
        for key, value in prompt_snapshot.items():
            fork.set_execution_prompt(key, value)
        extension_handlers_snapshot = self.request.extension_handlers.get(inherit=False)
        if isinstance(extension_handlers_snapshot, dict):
            for key, value in extension_handlers_snapshot.items():
                fork.request.extension_handlers.set(key, value)
        fork.goal_items = list(self.goal_items)
        fork.success_criteria_items = list(self.success_criteria_items)
        fork._goal_turn_on_long_task = self._goal_turn_on_long_task
        fork.generated_success_criteria = list(self.generated_success_criteria)
        fork.local_action_ids = [
            action_id
            for action_id in self.local_action_ids
            if action_id not in self._task_context_dependent_action_ids
        ]
        fork.local_required_action_ids = list(self.local_required_action_ids)
        fork.local_skill_selectors = [dict(item) for item in self.local_skill_selectors]
        fork.local_skills_pack_selectors = [dict(item) for item in self.local_skills_pack_selectors]
        fork.task_options = dict(self.task_options)
        fork.strategy_name = self.strategy_name
        if self._interaction_handler is not None:
            declare_interaction(fork, self._interaction_handler)
        fork.artifact_declarations = [
            cast("_AgentArtifactDeclaration", dict(item))
            for item in self.artifact_declarations
        ]
        fork.review_declarations = [
            cast("_AgentReviewDeclaration", dict(item))
            for item in self.review_declarations
        ]
        fork._sync_action_scope(source="AgentExecution.compatibility_fork")
        fork.effective_options = fork._build_effective_options()
        fork._selected_route = None
        return fork

    def _resolve_request(self, agent: Any, request: Any):
        if request is not None:
            return request
        if getattr(agent, "_agent", None) is not None:
            return agent.request
        isolated_request = self.agent.create_request()
        pending_prompt = self.agent._snapshot_request_prompt()
        if pending_prompt:
            isolated_request.prompt.update(pending_prompt)
            self.agent.request.prompt.clear()
        return isolated_request

    def _snapshot_prompt(self) -> dict[str, Any]:
        prompt_snapshot = self.request.prompt.get()
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, dict) else {}

    def _snapshot_execution_prompt(self) -> dict[str, Any]:
        prompt_snapshot = self.request.prompt.get(inherit=False)
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, dict) else {}

    def _refresh_prompt_snapshot(self):
        self.prompt_snapshot = self._snapshot_prompt()
        self.execution_prompt_snapshot = self._snapshot_execution_prompt()
        # Option hydration can declare a goal before the planner is constructed.
        if "route_planner" in self.__dict__:
            self.route_planner.prompt_snapshot = dict(self.prompt_snapshot)
        self._selected_route = None
        return self

    def _load_strategy_state_from_options(self):
        load_strategy_state_from_options(self)

    def _resolve_nesting_state(self) -> tuple[int, int | None]:
        """Compute this execution's nesting depth and the effective nesting budget.

        Depth is one deeper than the currently bound parent AgentExecutionContext
        (root = 0). The budget is the most restrictive `max_nested_agent_steps`
        among the constraining ancestor and this execution's own limits.
        """
        from agently.core.runtime.RuntimeContext import get_current_agent_execution_context

        parent_context = get_current_agent_execution_context()
        parent_depth = getattr(parent_context, "nesting_depth", None)
        depth = parent_depth + 1 if isinstance(parent_depth, int) else 0
        own_budget = self.limits.get("max_nested_agent_steps")
        parent_budget = getattr(parent_context, "nesting_budget", None)
        budgets = [value for value in (parent_budget, own_budget) if isinstance(value, int)]
        budget = min(budgets) if budgets else None
        return depth, budget

    def _load_inherited_strategy_context(self):
        from agently.core.runtime.RuntimeContext import get_current_agent_execution_context

        parent_context = get_current_agent_execution_context()
        self.inherited_task_execution_strategy = getattr(parent_context, "task_execution_strategy", None)
        self.inherited_effective_task_execution_strategy = getattr(
            parent_context,
            "effective_task_execution_strategy",
            None,
        )
        self.inherited_strategy_context_source = getattr(parent_context, "strategy_context_source", None)
        return self

    def _resolve_parent_model_request_budget(self):
        from agently.core.runtime.RuntimeContext import get_current_agent_execution_context

        parent_context = get_current_agent_execution_context()
        parent_budget = (
            getattr(parent_context, "model_request_budget", None)
            if parent_context is not getattr(self, "execution_context", None)
            else None
        )
        if parent_budget is not None:
            return parent_budget
        return getattr(self, "_parent_model_request_budget", None)

    def _replace_runtime_context(self):
        self._task_context_prepared = False
        self._clear_task_context_dependent_actions()
        self._nesting_depth, self._nesting_budget = self._resolve_nesting_state()
        self._parent_model_request_budget = self._resolve_parent_model_request_budget()
        self._load_inherited_strategy_context()
        self.execution_context = AgentExecutionContext(
            execution_id=self.id,
            lineage=self.lineage,
            limits=self.limits,
            nesting_depth=self._nesting_depth,
            nesting_budget=self._nesting_budget,
            task_execution_strategy=self.inherited_task_execution_strategy,
            effective_task_execution_strategy=self.inherited_effective_task_execution_strategy,
            strategy_context_source=self.inherited_strategy_context_source,
            task_workspace=self.task_workspace,
            execution_exchange_provider=self._interaction_provider,
            parent_model_request_budget=self._parent_model_request_budget,
        )
        self.stream = AgentExecutionStream(
            execution_id=self.id,
            lineage=self.lineage,
        ).bind_execution(self)
        self.execution_context.set_progress_callback(self._publish_runtime_progress)
        self.execution_context.set_exchange_callback(self._publish_exchange_stream_item)
        self._selected_route = None
        self.route_info = {}
        self.route_plan = {}
        self._model_request_result = None
        self.effective_options = self._build_effective_options()

    def _build_effective_options(self) -> dict[str, Any]:
        return build_effective_options(self)

    async def _publish_exchange_stream_item(
        self,
        action: str,
        exchanges: list[dict[str, Any]],
        meta: dict[str, Any],
    ):
        """Project pending/resolved human exchanges as typed stream items.

        Hosts consume these as instant items with meta.stream_kind="exchange"
        carrying normalized ExecutionExchangeView payloads, instead of reading
        raw TriggerFlow interrupt fields.
        """
        await self.stream.emit(
            f"exchange.{ action }",
            {"action": action, "exchanges": exchanges},
            route=str(self.route_info.get("selected_route") or ""),
            source="execution_exchange",
            meta={
                **meta,
                "stream_kind": "exchange",
                "exchange_action": action,
            },
        )

    async def _publish_runtime_progress(self, event: dict[str, Any]):
        stage = str(event.get("stage") or "runtime").strip() or "runtime"
        status = str(event.get("status") or "progress").strip() or "progress"
        path_stage = stage.replace("/", ".").replace(" ", "_")
        path_status = status.replace("/", ".").replace(" ", "_")
        await self.stream.emit(
            f"runtime.progress.{path_stage}.{path_status}",
            event,
            route=str(self.route_info.get("selected_route") or ""),
            source="agent_execution",
            meta={
                "stream_kind": "runtime_progress",
                "event_type": event.get("event_type"),
                "stage": stage,
                "status": status,
            },
        )

    def _ensure_agent_execution_run_context(self) -> "RunContext":
        if self.agent_execution_run_context is None:
            self.agent_execution_run_context = self.agent._create_agent_execution_run_context(
                parent_run_context=self.parent_run_context,
                execution_id=self.id,
                meta={
                    "execution_id": self.id,
                    "revision": self.revision,
                    "strategy": self.strategy_name,
                    "lineage": DataFormatter.sanitize(self.lineage),
                },
            )
        assert self.agent_execution_run_context is not None
        return self.agent_execution_run_context

    async def _async_emit_agent_execution_started_once(self) -> "RunContext":
        run_context = self._ensure_agent_execution_run_context()
        if not self._agent_execution_started_emitted:
            await self.agent._async_emit_agent_execution_started(run_context)
            self._agent_execution_started_emitted = True
        return run_context

    async def _async_emit_agent_execution_terminal_event(
        self,
        *,
        terminal_status: Literal["completed", "failed", "cancelled"],
        close_snapshot: dict[str, Any] | None = None,
    ) -> None:
        if self.agent_execution_run_context is None:
            return
        await self.agent._async_emit_agent_execution_terminal_event(
            self.agent_execution_run_context,
            execution_id=self.id,
            status=self.status,
            route=cast(str | None, self.route_info.get("selected_route")),
            strategy=self.strategy_name,
            task_refs=self.task_refs,
            close_snapshot=self.close_snapshot if close_snapshot is None else close_snapshot,
            terminal_status=terminal_status,
        )

    async def _async_emit_stream_runtime_event(self, item: AgentExecutionStreamData) -> None:
        if self.agent_execution_run_context is None:
            return
        await self.agent._async_emit_agent_execution_stream_event(
            self.agent_execution_run_context,
            execution_id=self.id,
            item=item,
            execution_strategy=cast(str | None, self.task_refs.get("execution_strategy") or self.task_options.get("execution")),
            effective_execution_strategy=cast(str | None, self.task_refs.get("effective_execution_strategy")),
        )

    def configure_options(self, options: Any) -> "AgentExecution":
        return configure_execution_options(self, options)

    def create_execution(
        self,
        *,
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: Any = None,
        parent_run_context: "RunContext | None" = None,
    ) -> "AgentExecution":
        if self._started:
            return cast(
                AgentExecution,
                self.agent.create_execution(
                    lineage=lineage,
                    limits=limits,
                    options=options,
                    parent_run_context=parent_run_context,
                ),
            )
        if lineage is not None:
            self.lineage = normalize_execution_lineage(lineage)
        self.limits = normalize_execution_limits(limits)
        if options is not None:
            self.configure_options(options)
        if parent_run_context is not None:
            self.parent_run_context = parent_run_context
        self._replace_runtime_context()
        return self

    def new_execution(
        self,
        *,
        lineage: "AgentExecutionLineage | dict[str, Any] | None" = None,
        limits: "AgentExecutionLimits | dict[str, Any] | None" = None,
        options: Any = None,
        parent_run_context: "RunContext | None" = None,
    ) -> "AgentExecution":
        return cast(
            AgentExecution,
            self.agent.create_execution(
                lineage=lineage,
                limits=limits,
                options=options,
                parent_run_context=parent_run_context,
            ),
        )

    def get_result(self, *, revision: int | None = None) -> AgentExecutionResult:
        if revision is not None and revision != self.revision and revision not in self._revision_history:
            raise ValueError(f"Unknown execution revision: {revision}.")
        result = AgentExecutionResult(self)
        if revision is not None:
            result.revision = revision
        return result

    def get_response(self) -> AgentExecutionResult:
        return self.get_result()

    async def async_streaming_print(self) -> None:
        print()
        async for delta in self.get_async_generator(type="delta"):
            print(delta, end="", flush=True)
        print()

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await async_get_data_object_entry(
            self,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    def run(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        kwargs: dict[str, Any] = dict(
            type=type, ensure_keys=ensure_keys, ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler, key_style=key_style,
            max_retries=max_retries, raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.start(**kwargs)
        return self.async_run(**kwargs)

    def _compat_meta(self, *args: Any, **kwargs: Any) -> Any:
        if self.task_record is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return self.task_record._meta()
            return self.task_record.async_meta()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.get_meta(*args, **kwargs)
        return self.async_get_meta(*args, **kwargs)

    async def async_meta(self) -> dict[str, Any]:
        task_record = self.task_record
        if task_record is not None:
            return await task_record.async_meta()
        await self.async_start()
        task_record = self.task_record
        if task_record is not None:
            return await task_record.async_meta()
        return dict(await self.async_get_meta())

    async def async_add_guidance(
        self,
        content: Any,
        *,
        author: str | None = None,
        target: Any = "task",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await add_guidance_entry(self, content, author=author, target=target, meta=meta)

    def set_execution_prompt(self, key: Any, value: Any, *, mappings: dict[str, Any] | None = None) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.set_execution_prompt(key, value, mappings=mappings)
        return target._refresh_prompt_snapshot()

    def remove_execution_prompt(self, key: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.remove_execution_prompt(key)
        return target._refresh_prompt_snapshot()

    def validate(self, handler: "OutputValidateHandler") -> "AgentExecution":
        """Hard-check this call's final output, not internal Pattern/task steps.

        Direct request repair remains request-owned. Assembled/AgentTask
        results are checked once without replaying steps or side effects.
        """
        target = self._reconfiguration_target()
        target._draft.validate(handler)
        return target

    def system(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.system(prompt, mappings=mappings, always=always)
        return target._refresh_prompt_snapshot()

    def rule(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.rule(prompt, mappings=mappings, always=always)
        return target._refresh_prompt_snapshot()

    def role(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.role(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def user_info(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.user_info(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def input(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.input(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def info(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.info(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def instruct(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.instruct(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def examples(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.examples(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def output(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.output(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def use_system_one(self, enabled: bool = True) -> "AgentExecution":
        """Select the dedicated template model; configured models default to enabled."""
        if not isinstance(enabled, bool):
            raise TypeError("use_system_one expects a bool.")
        target = self._reconfiguration_target()
        target.request.settings.set("system_one.enabled", enabled)
        return target

    def auto_continue(self, enabled: bool = True) -> "AgentExecution":
        """Enable conditional continuation of unfinished model output for this draft.

        Normal completion adds no continuation request. This does not select
        long_content, expand short answers, resume a task, or replace rework.
        Defaults off on each new draft; all result readers share this policy.
        Configure before starting the execution. Existing validation still applies.
        """

        target = self._reconfiguration_target()
        target._ensure_long_output_enabled = bool(enabled)
        return target

    def ensure_long_output(self, enabled: bool = True) -> "AgentExecution":
        """Compatibility alias for auto_continue(); prefer the shorter name."""

        return self.auto_continue(enabled)

    def attachment(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.attachment(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def image(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.image(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def set_prompt_options(self, options: dict[str, Any], *, always: bool = False) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.set_prompt_options(options, always=always)
        return target._refresh_prompt_snapshot()

    def language(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target._draft.language(*args, **kwargs)
        return target._refresh_prompt_snapshot()

    def use_dynamic_task(self, *args: Any, **kwargs: Any) -> "AgentExecution":
        raise ValueError(
            "AgentExecution.use_dynamic_task(...) is no longer an AgentExecution route. "
            "Use Agently.create_dynamic_task(...) or direct TaskDAGExecutor(...) for "
            "independent DAG workflows."
        )

    def resolve_skills_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return default_stage_call_bridge.as_sync(self.async_resolve_skills_plan)(*args, **kwargs)

    async def async_resolve_skills_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs = self._with_local_skill_kwargs(kwargs)
        task = args[0] if args else kwargs.pop("task", None)
        mode = kwargs.pop("mode", "model_decision")
        output = kwargs.pop("output", None)
        semantic_outputs = kwargs.pop("semantic_outputs", None)
        output_format = kwargs.pop("output_format", None)
        skills = kwargs.pop("skills", None)
        skills_packs = kwargs.pop("skills_packs", None)
        kwargs.pop("_settings_overrides", None)
        if kwargs:
            raise TypeError(f"Unsupported Skill plan arguments: {sorted(kwargs)}")
        task, output, output_format = self._draft._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        self.input(task)
        if output is not None:
            self.output(output, format=output_format)
        if skills is not None:
            self.use_skills(skills, mode=mode)
        if skills_packs is not None:
            self.use_skills_packs(skills_packs, mode=mode)
        self.strategy("direct")
        await self.async_prepare_task_context()
        project = getattr(self.agent, "_async_project_skill_binding_plan")
        return cast(Any, await project(self, mode=mode))

    def run_skills_task(self, *args: Any, **kwargs: Any) -> Any:
        return default_stage_call_bridge.as_sync(self.async_run_skills_task)(*args, **kwargs)

    async def async_run_skills_task(self, *args: Any, **kwargs: Any) -> Any:
        kwargs = self._with_local_skill_kwargs(kwargs)
        task = args[0] if args else kwargs.pop("task", None)
        mode = kwargs.pop("mode", "model_decision")
        output = kwargs.pop("output", None)
        semantic_outputs = kwargs.pop("semantic_outputs", None)
        output_format = kwargs.pop("output_format", None)
        stream_handler = kwargs.pop("stream_handler", None)
        effort = kwargs.pop("effort", None)
        skills = kwargs.pop("skills", None)
        skills_packs = kwargs.pop("skills_packs", None)
        kwargs.pop("_settings_overrides", None)
        if kwargs:
            raise TypeError(f"Unsupported Skill execution arguments: {sorted(kwargs)}")
        task, output, output_format = self._draft._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        self.input(task)
        if output is not None:
            self.output(output, format=output_format)
        if skills is not None:
            self.use_skills(skills, mode=mode)
        if skills_packs is not None:
            self.use_skills_packs(skills_packs, mode=mode)
        self.strategy("direct")
        if effort is not None:
            self.effort(effort)
        result = await self.async_get_data()
        if stream_handler is not None:
            handled = stream_handler(
                {
                    "path": "result",
                    "data": result,
                    "is_complete": True,
                    "source": "agent_execution",
                }
            )
            if inspect.isawaitable(handled):
                await handled
        from agently.builtins.agent_extensions.SkillsExtension.SkillsExtension import (
            SkillRunCompatibilityResult,
        )

        return SkillRunCompatibilityResult(execution=self, output=result)

    def _with_local_skill_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        updated = dict(kwargs)
        if "_settings_overrides" not in updated:
            execution_access_policy = self.effective_options.get("access_control_policy", {})
            if isinstance(execution_access_policy, Mapping):
                updated["_settings_overrides"] = {"access_control_policy": dict(execution_access_policy)}
        mode = str(updated.get("mode") or "model_decision")
        if "skills" not in updated:
            selectors = [
                item.get("selector")
                for item in self.local_skill_selectors
                if item.get("mode") == mode
            ]
            if selectors:
                updated["skills"] = selectors
        if "skills_packs" not in updated:
            pack_selectors = [
                item.get("selector")
                for item in self.local_skills_pack_selectors
                if item.get("mode") == mode
            ]
            if pack_selectors:
                updated["skills_packs"] = pack_selectors
        return updated

    def create_dynamic_task(self, *args: Any, **kwargs: Any) -> "DynamicTask":
        return self._draft.create_dynamic_task(*args, **kwargs)

    def get_prompt_text(self) -> str:
        if not self._started:
            return self._draft.get_prompt_text()
        prompt_text = self._prompt_text_from_snapshot(self.prompt_snapshot)
        if prompt_text:
            return prompt_text
        return self._draft.get_prompt_text()

    def get_json_prompt(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ) -> str:
        prompt_data = {
            ".agent": self.agent.agent_prompt.to_serializable_prompt_data(),
            ".execution": self.request_prompt.to_serializable_prompt_data(),
        }
        content = str(json5.dumps(
            prompt_data,
            indent=2,
            ensure_ascii=False,
        ))
        if save_to is not None:
            target = Path(save_to)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding or "utf-8")
        return content

    def get_yaml_prompt(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ) -> str:
        prompt_data = {
            ".agent": self.agent.agent_prompt.to_serializable_prompt_data(),
            ".execution": self.request_prompt.to_serializable_prompt_data(),
        }
        content = yaml.safe_dump(
            prompt_data,
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )
        if save_to is not None:
            target = Path(save_to)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding or "utf-8")
        return content

    def _prompt_text_from_snapshot(self, prompt_snapshot: dict[str, Any]) -> str:
        if not prompt_snapshot:
            return ""
        request = self.agent.create_request()
        try:
            request.prompt.update(prompt_snapshot)
            return request.prompt.to_text()[6:][:-11]
        finally:
            request.prompt.clear()

    def _output_prompt_snapshot(self) -> dict[str, Any]:
        if not self._started:
            prompt_snapshot = self._snapshot_prompt()
        else:
            prompt_snapshot = self.prompt_snapshot
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, dict) else {}

    def _check_keys_in_output(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ) -> None:
        prompt_snapshot = self._output_prompt_snapshot()
        output_prompt = prompt_snapshot.get("output")
        if not output_prompt:
            raise NotImplementedError(
                "Cannot wait for structured output keys before defining `.output(...)` "
                "on this AgentExecution."
            )
        if must_in_prompt and isinstance(output_prompt, dict):
            missing = [key for key in keys if key not in output_prompt]
            if missing:
                raise NotImplementedError(
                    f"Cannot wait for key/keys { missing } because they are not defined "
                    "in this AgentExecution `.output(...)` prompt."
                )

    async def async_get_key_result(
        self,
        key: str,
        *,
        must_in_prompt: bool = False,
    ) -> Any:
        self._check_keys_in_output([key], must_in_prompt=must_in_prompt)
        async for data in self.get_async_generator(type="instant"):
            if self._is_output_key_stream_item(data, key):
                return getattr(data, "value", None)
        return None

    async def async_wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ) -> AsyncGenerator[tuple[str, Any], None]:
        self._check_keys_in_output(keys, must_in_prompt=must_in_prompt)
        expected = set(keys)
        async for data in self.get_async_generator(type="instant"):
            path = getattr(data, "path", None)
            if path in expected and self._is_output_key_stream_item(data, str(path)):
                yield str(path), getattr(data, "value", None)

    def wait_keys(
        self,
        keys: list[str],
        *,
        must_in_prompt: bool = False,
    ) -> Generator[tuple[str, Any], None, None]:
        return default_stage_call_bridge.iter_sync(
            self.async_wait_keys(keys, must_in_prompt=must_in_prompt)
        )

    def on_key(self, key: str, handler: Any) -> "AgentExecution":
        self._key_waiter_handlers.setdefault(key, []).append(handler)
        return self

    def start_waiter(self, *, must_in_prompt: bool = False) -> list[tuple[str, Any, Any]]:
        return cast(
            list[tuple[str, Any, Any]],
            default_stage_call_bridge.as_sync(self.async_start_waiter)(must_in_prompt=must_in_prompt),
        )

    async def async_start_waiter(self, *, must_in_prompt: bool = False) -> list[tuple[str, Any, Any]]:
        if not self._key_waiter_handlers:
            raise NotImplementedError(
                "Use `.when_key(<key>, <handler>)` to provide at least one key handler "
                "before `.start_waiter()`."
            )
        keys = list(self._key_waiter_handlers)
        self._check_keys_in_output(keys, must_in_prompt=must_in_prompt)
        tasks: list[asyncio.Task[tuple[str, Any, Any]]] = []

        async def handler_wrapper(path: str, value: Any, handler: Any) -> tuple[str, Any, Any]:
            return path, value, await default_stage_call_bridge.as_async(handler)(value)

        async for path, value in self.async_wait_keys(keys, must_in_prompt=False):
            for handler in self._key_waiter_handlers.get(path, []):
                tasks.append(asyncio.create_task(handler_wrapper(path, value, handler)))
        if not tasks:
            return []
        return await asyncio.gather(*tasks)

    def _is_output_key_stream_item(self, data: Any, key: str) -> bool:
        if getattr(data, "path", None) != key:
            return False
        if not getattr(data, "is_complete", False):
            return False
        source = str(getattr(data, "source", "") or "")
        if source and source != "model_request":
            return False
        meta = getattr(data, "meta", None)
        meta_map = meta if isinstance(meta, dict) else {}
        stream_kind = str(meta_map.get("stream_kind") or "")
        return stream_kind != "text_projection"

    @property
    def goal_items(self) -> list[str]:
        """Compatibility projection of the authoritative semantic Prompt."""
        if self._started and self.request_prompt.get("goal") is None:
            declared = self._draft._goal_values(self.prompt_snapshot.get("goal"))
        else:
            declared = self._draft.goal_items
        return declared or (list(self._prepared_goal.goals) if self._prepared_goal else [])

    @property
    def action(self) -> "Action":
        """The carrying Agent's Action module; execution scope stays local."""
        return cast("Action", getattr(self.agent, "action"))

    @goal_items.setter
    def goal_items(self, value: list[str]) -> None:
        self.request_prompt.set("goal", list(value))

    @property
    def success_criteria_items(self) -> list[str]:
        if self._started and self.request_prompt.get("success_criteria") is None:
            declared = self._draft._goal_values(self.prompt_snapshot.get("success_criteria"))
        else:
            declared = self._draft.success_criteria_items
        return declared or (list(self._prepared_goal.success_criteria) if self._prepared_goal else [])

    @success_criteria_items.setter
    def success_criteria_items(self, value: list[str]) -> None:
        self.request_prompt.set("success_criteria", list(value))

    def goal(
        self,
        goal: str | list[str] | tuple[str, ...] | set[str],
        success_criteria: str | list[str] | tuple[str, ...] | set[str] | None = None,
        *,
        turn_on_long_task: bool = True,
    ) -> "AgentExecution":
        """Declare a goal; optionally enable the long-task convenience path.

        False is Prompt-only: it neither forces direct execution nor disables
        an independently selected task strategy. Explicit selection wins.
        """
        if not isinstance(turn_on_long_task, bool):
            raise TypeError("turn_on_long_task must be bool.")
        target = self._reconfiguration_target()
        target._draft.goal(goal, success_criteria)
        target._goal_turn_on_long_task = turn_on_long_task
        execution_options = dict(target.options.get("execution") or {})
        execution_options["turn_on_long_task"] = turn_on_long_task
        target.options["execution"] = execution_options
        if target._draft._goal_values(success_criteria):
            target.generated_success_criteria = []
        target.effective_options = target._build_effective_options()
        target._selected_route = None
        return target._refresh_prompt_snapshot()

    goals = goal

    def interact(self, handler: "AgentInteractionHandler") -> "AgentExecution":
        """Bind one execution-local connected human-interaction handler."""
        return declare_interaction(self, handler)

    def review(
        self, handler: "AgentReviewHandler | None" = None, *,
        rules: str | Sequence[str] | None = None,
        on_fail: Literal["warn", "block"] = "warn",
    ) -> "AgentExecution":
        """Review final output and artifacts using rules or a replacement handler.

        on_fail warns by default or blocks delivery with AgentReviewError.
        It does not change the evaluator's rubric or replay execution steps.
        """
        return declare_review(self, handler=handler, rules=rules, on_fail=on_fail)

    def artifact(
        self,
        path: str | os.PathLike[str],
        handler: "AgentArtifactHandler | None" = None,
    ) -> "AgentExecution":
        """Declare a TaskWorkspace-relative artifact to materialize after the run."""
        return declare_artifact(self, path, handler)

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
    ) -> "AgentExecution":
        """Apply an effort profile and optional execution-strategy overrides."""
        return configure_effort(self._reconfiguration_target(), value, **strategy)

    def use_actions(self, actions: object) -> "AgentExecution":
        """Attach Actions to this execution without changing Agent defaults."""
        target = self._reconfiguration_target()
        register = getattr(self.agent, "_register_action_items", None)
        if callable(register):
            raw_names = register(actions)
        else:
            agent_any = cast(Any, self.agent)
            agent_any.use_actions(actions, always=True)
            raw_names = getattr(self.agent, "_normalize_registered_action_ids", lambda value: [])(actions)
        names = raw_names if isinstance(raw_names, (list, tuple, set)) else []
        for name in names:
            text = str(name or "").strip()
            if text and text not in target.local_action_ids:
                target.local_action_ids.append(text)
        target._sync_action_scope(source="AgentExecution.use_actions")
        target._selected_route = None
        target.effective_options = target._build_effective_options()
        return target

    def use_action(self, actions: object) -> "AgentExecution":
        """Attach one Action to this execution."""
        return self.use_actions(actions)

    def use_tools(self, tools: object) -> "AgentExecution":
        """Compatibility alias for ``use_actions(...)``."""
        return self.use_actions(tools)

    def use_tool(self, tools: object) -> "AgentExecution":
        """Compatibility alias for ``use_action(...)``."""
        return self.use_action(tools)

    def require_actions(self, actions: object) -> "AgentExecution":
        """Require Actions during this execution."""
        target = self._reconfiguration_target()
        register = getattr(self.agent, "_register_action_items", None)
        if callable(register):
            raw_names = register(actions)
        else:
            agent_any = cast(Any, self.agent)
            agent_any.require_actions(actions, always=True)
            raw_names = getattr(self.agent, "_normalize_registered_action_ids", lambda value: [])(actions)
        names = raw_names if isinstance(raw_names, (list, tuple, set)) else []
        for name in names:
            text = str(name or "").strip()
            if text and text not in target.local_action_ids:
                target.local_action_ids.append(text)
            if text and text not in target.local_required_action_ids:
                target.local_required_action_ids.append(text)
        target._sync_action_scope(source="AgentExecution.require_actions")
        target._selected_route = None
        target.effective_options = target._build_effective_options()
        return target

    def _sync_action_scope(self, *, source: str):
        self.execution_context.set_action_scope(self.local_action_ids, source=source)
        self.diagnostics["action_scope"] = DataFormatter.sanitize(
            dict(self.execution_context.action_scope)
        )
        return self

    def _enable_task_context_dependent_action(self, action_id: str) -> "AgentExecution":
        target = self._reconfiguration_target(invalidate_task_context=False)
        if not target._task_context_prepared:
            raise RuntimeError(
                "Prepare the AgentExecution TaskContext before enabling a dependent Action."
            )
        normalized = str(action_id or "").strip()
        registry = getattr(getattr(target, "action", None), "action_registry", None)
        if not normalized or registry is None or not registry.has(normalized):
            raise ValueError("A dependent Action must already be registered.")
        if normalized not in target.local_action_ids:
            target.local_action_ids.append(normalized)
        target._task_context_dependent_action_ids.add(normalized)
        target._sync_action_scope(
            source="AgentExecution.task_context_dependent_action"
        )
        target._selected_route = None
        target.effective_options = target._build_effective_options()
        return target

    @staticmethod
    def _skill_selector_id(selector: Any) -> str:
        if isinstance(selector, str):
            return selector.strip()
        if isinstance(selector, Mapping):
            return str(
                selector.get("skill_id")
                or selector.get("id")
                or selector.get("name")
                or ""
            ).strip()
        return ""

    async def async_prepare_task_context(self) -> TaskContext:
        """Synchronize prompt facts and exact required Skill bindings before routing."""

        if self._task_context_prepared:
            return self.task_context

        for entry_id in tuple(self._task_context_prompt_entry_ids):
            self.task_context.remove(entry_id)
        self._task_context_prompt_entry_ids.clear()
        if self._skill_context_binding_id is not None:
            self.task_context.remove(self._skill_context_binding_id)
            self._skill_context_binding_id = None
        if self._session_memory_context_binding_id is not None:
            self.task_context.remove(self._session_memory_context_binding_id)
            self._session_memory_context_binding_id = None
        self.skill_bindings = []
        self.context_readers.clear()

        prompt_snapshot = self._draft.snapshot()
        slot_roles = {
            "system": "instruction",
            "input": "state",
            "info": "information",
            "instruct": "instruction",
            "examples": "example",
            "attachment": "artifact",
        }
        for slot, role in slot_roles.items():
            value = prompt_snapshot.get(slot)
            if value is None or value == "" or value == [] or value == {}:
                continue
            entry_id = f"agent_execution_prompt:{self.id}:{slot}"
            self.task_context.put(
                role=cast(Any, role),
                content=value,
                entry_id=entry_id,
                required=slot not in {"examples", "attachment"},
                source_ref=f"agent-execution:{self.id}:prompt:{slot}",
                metadata={
                    "prompt_slot": slot,
                    "already_in_prompt": True,
                    "owner": "agent_execution",
                },
            )
            self._task_context_prompt_entry_ids.add(entry_id)

        bind_skills = getattr(self.agent, "async_bind_skills_for_execution", None)
        if callable(bind_skills):
            with bind_runtime_context(
                agent_execution_context=self.execution_context,
                settings=self.request.settings,
            ):
                resolved_bindings = await cast(Any, bind_skills)(self)
            self.skill_bindings = list(resolved_bindings or [])
        if self.skill_bindings:
            library = self.skill_library
            if library is None:
                raise RuntimeError("Skill binding preparation needs an active SkillLibrary.")
            source = SkillContextSource(library, bindings=tuple(self.skill_bindings))
            self._skill_context_binding_id = self.task_context.attach(
                source,
                binding_id=f"skill_context_binding:{self.id}",
                required=True,
                scope="execution",
                metadata={
                    "binding_ids": [item.binding_id for item in self.skill_bindings],
                    "revision_refs": [item.revision_ref for item in self.skill_bindings],
                    "modes": [item.mode for item in self.skill_bindings],
                },
            )

        active_session = getattr(self.agent, "activated_session", None)
        create_memory_source = getattr(
            active_session,
            "create_memory_context_source",
            None,
        )
        if callable(create_memory_source):
            memory_source = cast(
                ContextSource | None,
                create_memory_source(settings=self.request.settings),
            )
            if memory_source is not None:
                self._session_memory_context_binding_id = self.task_context.attach(
                    memory_source,
                    binding_id=f"session_memory_binding:{self.id}",
                    scope="session",
                    metadata={
                        "session_id": str(getattr(active_session, "id", "")),
                    },
                )

        self.prompt_snapshot = dict(prompt_snapshot)
        self.execution_prompt_snapshot = self._snapshot_execution_prompt()
        self.route_planner.prompt_snapshot = dict(self.prompt_snapshot)
        self._task_context_prepared = True
        return self.task_context

    async def async_read_task_context(
        self,
        *,
        consumer_id: str,
        phase: str,
        intent: str | ContextReadIntent | None = None,
        budget: ContextBudget | None = None,
    ) -> Any:
        if not self._task_context_prepared:
            await self.async_prepare_task_context()
        raw_context_budget = self.options.get("context_budget")
        context_budget = (
            raw_context_budget
            if isinstance(raw_context_budget, Mapping)
            else {}
        )

        def positive_int(*keys: str, default: int) -> int:
            for key in keys:
                value = context_budget.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
            return default

        resolved_budget = budget or ContextBudget(
            max_chars=positive_int("max_chars", "chars", default=12000),
            max_blocks=positive_int("max_blocks", default=64),
            max_block_chars=positive_int("max_block_chars", default=6000),
        )
        key = (str(consumer_id), str(phase))
        reader = self.context_readers.get(key)
        if reader is None:
            request_factory = getattr(self.agent, "create_temp_request", None)
            semantic_selector = (
                ModelRequestContextSelector(request_factory)
                if callable(request_factory)
                else None
            )
            reader = self.task_context.reader(
                consumer=consumer_id,
                phase=phase,
                budget=resolved_budget,
                semantic_selector=semantic_selector,
            )
            self.context_readers[key] = reader
        policy_metadata: dict[str, Any] = {}
        if str(context_budget.get("required_overflow") or "").strip() == "lossy_digest":
            policy_metadata["required_overflow"] = "lossy_digest"
        if str(context_budget.get("optional_selection") or "").strip() == "none":
            policy_metadata["optional_selection"] = "none"
        resolved_intent = intent
        if resolved_intent is None:
            resolved_intent = ContextReadIntent(
                query=self.task_target(),
                metadata={
                    "exclude_already_in_prompt": True,
                    **policy_metadata,
                },
            )
        elif isinstance(resolved_intent, ContextReadIntent) and policy_metadata:
            resolved_intent = ContextReadIntent(
                query=resolved_intent.query,
                explicit_refs=resolved_intent.explicit_refs,
                roles=resolved_intent.roles,
                filters=resolved_intent.filters,
                metadata={**policy_metadata, **dict(resolved_intent.metadata)},
            )
        elif isinstance(resolved_intent, str) and policy_metadata:
            resolved_intent = ContextReadIntent(
                query=resolved_intent,
                metadata=policy_metadata,
            )
        with bind_runtime_context(
            agent_execution_context=self.execution_context,
            settings=self.request.settings,
        ):
            package = await reader.async_read(resolved_intent)
        reader.ensure_required_delivery(package)
        self.context_packages.append(package)
        self.logs.setdefault("context_packages", []).append(package.to_dict())
        return package

    def record_context_consumption(self, package: Any, *, request_id: str) -> ContextConsumption:
        record_interrupt_consumption(self, package, request_id=request_id)
        consumption = ContextConsumption(
            consumption_id=f"context_consumption:{uuid.uuid4().hex}",
            package_id=package.package_id,
            request_id=str(request_id),
            consumer_id=package.consumer_id,
            phase=package.phase,
            block_ids=tuple(block.block_id for block in package.blocks),
        )
        self.context_consumptions.append(consumption)
        self.logs.setdefault("context_consumptions", []).append(consumption.to_dict())
        return consumption

    def use_skills(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
    ) -> "AgentExecution":
        target = self._reconfiguration_target()
        normalize = getattr(self.agent, "_normalize_skill_selector_entries", None)
        if callable(normalize):
            raw_entries = normalize(skills, mode=mode, auto_allow=auto_allow)
        else:
            raw_entries = [{"selector": skills, "mode": mode}]
        entries = raw_entries if isinstance(raw_entries, list) else []
        target.local_skill_selectors.extend(entries)
        target._selected_route = None
        target.effective_options = target._build_effective_options()
        return target

    def require_skills(
        self,
        skills: Any,
        *,
        auto_allow: bool = False,
    ) -> "AgentExecution":
        return self.use_skills(skills, mode="required", auto_allow=auto_allow)

    def use_skills_packs(
        self,
        skills_packs: Any,
        *,
        mode: SkillMode = "model_decision",
    ) -> "AgentExecution":
        target = self._reconfiguration_target()
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill pack mode must be one of: 'model_decision', 'required'.")
        items = skills_packs if isinstance(skills_packs, (list, tuple, set)) else [skills_packs]
        target.local_skills_pack_selectors.extend(
            {"selector": item, "mode": mode}
            for item in items
        )
        target._selected_route = None
        target.effective_options = target._build_effective_options()
        return target

    def route_policy(self, value: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target.options["route_policy"] = DataFormatter.sanitize(value)
        target.effective_options = target._build_effective_options()
        target._selected_route = None
        return target

    def access_control_policy(self, value: Any) -> "AgentExecution":
        target = self._reconfiguration_target()
        target.options["access_control_policy"] = DataFormatter.sanitize(value)
        target.effective_options = target._build_effective_options()
        return target

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
    ) -> "AgentExecution":
        """Select an execution strategy and optional strategy-specific settings."""
        target = self._reconfiguration_target()
        if value is not None:
            apply_strategy_selection(target, value, source="explicit_strategy")
        if options:
            if "execution" in options:
                from ..long_task import AgentTask

                options = dict(options)
                options["execution"] = AgentTask.normalize_execution_strategy(options.get("execution"))
                options["_execution_strategy_source"] = "explicit_strategy_option"
            target.task_options.update(options)
        target.effective_options = target._build_effective_options()
        target._selected_route = None
        return target

    def route_options(self, route_name: str) -> dict[str, Any]:
        return state_route_options(self, route_name)

    def record_consumed_option(self, path: str, value: Any, *, owner: str) -> None:
        state_record_consumed_option(self, path, value, owner_name=owner)

    def task_target(self) -> str:
        return state_task_target(self)

    def task_goal(self) -> str:
        return state_task_goal(self)

    def task_success_criteria(self) -> list[str]:
        return state_task_success_criteria(self)

    def required_action_ids(self) -> list[str]:
        collect = getattr(self.agent, "_collect_required_action_ids", None)
        required = [*self.local_required_action_ids]
        if self.options.get("_inherit_required_actions", True) and callable(collect):
            collected = collect()
            if isinstance(collected, (list, tuple, set)):
                required.extend(collected)
        constraints = self.options.get("capability_constraints")
        if isinstance(constraints, dict):
            actions = constraints.get("actions")
            if isinstance(actions, dict):
                configured = actions.get("required", [])
            else:
                configured = constraints.get("required_actions", [])
            if isinstance(configured, str):
                required = [*required, configured]
            elif isinstance(configured, (list, tuple, set)):
                required = [*required, *configured]
        result: list[str] = []
        for item in required:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def required_skill_ids(self) -> list[str]:
        required: list[Any] = []
        for item in self.local_skill_selectors:
            if item.get("mode") == "required":
                selector = item.get("selector")
                if isinstance(selector, dict):
                    required.append(selector.get("id") or selector.get("skill_id") or selector.get("name") or selector.get("source"))
                else:
                    required.append(selector)
        collect = getattr(self.agent, "_collect_skill_selectors", None)
        try:
            raw_required_selectors = collect(skills=None, mode="required") if callable(collect) else []
        except Exception:
            raw_required_selectors = []
        required_selectors = raw_required_selectors if isinstance(raw_required_selectors, (list, tuple, set)) else []
        for item in required_selectors:
            selector = (
                item.get("selector")
                if isinstance(item, dict) and "selector" in item
                else item
            )
            if isinstance(selector, dict):
                required.append(selector.get("id") or selector.get("skill_id") or selector.get("name") or selector.get("source"))
            else:
                required.append(selector)
        constraints = self.options.get("capability_constraints")
        if isinstance(constraints, dict):
            skills = constraints.get("skills")
            if isinstance(skills, dict):
                configured = skills.get("required", [])
            else:
                configured = constraints.get("required_skills", [])
            if isinstance(configured, str):
                required.append(configured)
            elif isinstance(configured, (list, tuple, set)):
                required.extend(configured)
        result: list[str] = []
        for item in required:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def is_task_strategy(self) -> bool:
        return state_is_task_strategy(self)

    def task_strategy_options(self) -> dict[str, Any]:
        options = dict(self.task_options)
        if "execution" not in options:
            inherited = self.inherited_effective_task_execution_strategy or self.inherited_task_execution_strategy
            if inherited in {"flat", "taskboard"}:
                options["execution"] = inherited
                options["_execution_strategy_source"] = "inherited_agent_execution_context"
        return options

    async def emit_stream(
        self,
        path: str,
        value: Any,
        *,
        route: str | None = None,
        source: str | None = "agent_execution",
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        is_complete: bool | None = None,
        event_type: Literal["delta", "done"] = "done",
        delta: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        completed = event_type == "done"
        if is_complete is not None:
            completed = is_complete
        if path != "error":
            self.execution_context.record_progress(
                stage=path,
                status="completed" if completed else "progress",
                event_type=path,
                meta=meta,
            )
        stream_meta = merge_stream_meta(
            meta,
            execution_id=self.id,
            lineage=self.lineage,
        )
        stream_meta["revision"] = self.revision
        return await self.stream.emit(
            path,
            value,
            delta=delta,
            route=route,
            source=source,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            is_complete=completed,
            event_type=event_type,
            meta=stream_meta,
        )

    async def close_streams(self) -> None:
        await self.stream.close()

    def action_candidates(self) -> list[dict[str, Any]]:
        return self.route_planner.action_candidates()

    def skill_candidate_summary(self) -> dict[str, Any]:
        return self.route_planner.skill_candidate_summary()

    async def select_route(self) -> tuple[str, dict[str, Any]]:
        if self._selected_route is not None:
            return self._selected_route
        self._refresh_prompt_snapshot()
        route: str
        route_meta: dict[str, Any]
        if self.producer_route is not None:
            if (
                self.strategy_name is not None
                and self.supported_strategies is not None
                and self.strategy_name not in self.supported_strategies
            ):
                raise ValueError(
                    f"AgentExecution {self.name!r} does not support strategy {self.strategy_name!r}."
                )
            route, route_meta = self.producer_route, {
                "selected_by": "execution_plugin", "plugin": self.name,
                "strategy": self.strategy_name,
            }
            if not self.route_planner.route_allowed(route):
                route, route_meta = "route_policy_blocked", {
                    **route_meta,
                    "route_policy": self.route_planner.route_policy(),
                    "route_policy_warning": f"Selected execution plugin {self.name!r} is disallowed by route policy.",
                }
        elif self.strategy_name == "direct":
            required_actions = self.required_action_ids()
            required_skills = self.required_skill_ids()
            route, route_meta = "model_request", {
                "strategy": "direct",
                "selected_by": "execution_strategy",
            }
            if self.action_candidates() or required_actions:
                route_meta["with_actions"] = True
            if required_actions:
                route_meta["required_actions"] = required_actions
            if required_skills:
                route_meta["required_skills"] = required_skills
        elif self.is_task_strategy():
            strategy = self.strategy_name or "task"
            route, route_meta = "agent_task", {
                "strategy": strategy,
                "selected_by": "execution_strategy",
                "goals": list(self.goal_items),
                "success_criteria": list(self.success_criteria_items),
                "generated_success_criteria": list(self.generated_success_criteria),
            }
        elif self._ensure_long_output_enabled:
            route, route_meta = "model_request", {
                "strategy": "direct",
                "selected_by": "ensure_long_output",
            }
        elif self.required_action_ids() and self.route_planner.route_allowed("model_request"):
            route, route_meta = "model_request", {
                "with_actions": True,
                "required_actions": self.required_action_ids(),
                "required_skills": self.required_skill_ids(),
                "selected_by": "required_capability",
            }
        else:
            route, route_meta = await self.route_planner.select_route()
        self._selected_route = (route, route_meta)
        self.route_info = {
            "selected_route": route,
            "selected_by": route_meta.get("selected_by"),
            "options": DataFormatter.sanitize(route_meta),
            "reusable": True,
        }
        return self._selected_route

    async def _async_execute_route(
        self,
        *,
        type: Literal["original", "parsed", "all"],
        ensure_keys: list[str] | None,
        ensure_all_keys: bool | None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
        key_style: Literal["dot", "slash"],
        max_retries: int,
        raise_ensure_failure: bool,
    ) -> tuple[str, Any]:
        return await async_execute_route(
            self,
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        """Produce on this instance; the shared lifecycle applies final policies.

        Override this hook to reuse draft, budget, result and terminal handling.
        Caller validators are withheld from overridden producers and applied
        once to their final return value. Internal validators remain local.
        """
        return await produce_default_route(self, options)

    def record_model_response_id(self, response_id: str | None) -> None:
        record_model_response_id_entry(self, response_id)

    async def record_action_log(
        self,
        log: Any,
        *,
        route: str,
        source: str = "action",
        emit: bool = True,
    ) -> dict[str, Any] | None:
        return await record_action_log_entry(self, log, route=route, source=source, emit=emit)

    async def bridge_task_dag_stream_item(self, item: Any, *, route: str) -> None:
        await bridge_task_dag_stream_item_entry(self, item, route=route)

    async def bridge_agent_task_stream_item(
        self,
        item: Any,
        *,
        route: str = "agent_task",
    ) -> None:
        await bridge_agent_task_stream_item_entry(self, item, route=route)

    async def bridge_model_stream_item(
        self,
        item: Any,
        *,
        route: str,
        source: str = "model_request",
        path_prefix: str | None = None,
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        await bridge_model_stream_item_entry(
            self,
            item,
            route=route,
            source=source,
            path_prefix=path_prefix,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=meta,
        )

    async def async_start(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await self.async_run(
            type=type, ensure_keys=ensure_keys, ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler, key_style=key_style,
            max_retries=max_retries, raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    def require_agent_capability(self, name: str) -> object:
        """Bind a dynamic dependency once, before the producer uses it."""
        if name not in self._bound_agent_capabilities:
            self._bound_agent_capabilities[name] = self.agent.require_capability(name)
        return self._bound_agent_capabilities[name]

    async def async_run(
        self,
        *,
        type: Literal["original", "parsed", "all"] = "parsed",
        ensure_keys: list[str] | None = None,
        ensure_all_keys: bool | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        with bind_runtime_context(settings=self.request.settings):
            return await run_owned(self, lambda: start_execution(
                self,
                type=type,
                ensure_keys=ensure_keys,
                ensure_all_keys=ensure_all_keys,
                validate_handler=validate_handler,
                key_style=key_style,
                max_retries=max_retries,
                raise_ensure_failure=raise_ensure_failure,
                parent_run_context=parent_run_context,
            ))

    def _assert_rework_supported(self) -> None:
        if self.__class__._async_produce is not AgentExecution._async_produce:
            raise NotImplementedError(f"Producer {self.name!r} must declare its own safe rework contract.")
        route = self.route_info.get("selected_route")
        if route == "agent_task":
            if self.task_record is None or self._producer_state.get("kind") != "long_task":
                raise RuntimeError("Long-task rework requires its retained producer and evidence bindings.")
            if any(not task.done() for task in self.task_record._background_stream_tasks):
                raise RuntimeError("Long-task child work has not settled.")
        elif route != "model_request":
            raise NotImplementedError("This producer does not support retained-state rework.")

    async def _async_rework_produce(self, options: ProductionOptions) -> tuple[str, object]:
        if self.route_info.get("selected_route") == "agent_task":
            from ..long_task.Rework import prepare_task_rework
            await prepare_task_rework(self)
        else:
            from .revisions import rework_request
            await rework_request(self)
        return await self._async_produce(options)

    async def async_rework(self, feedback: str, *, max_reworks: int = 3, allow_replay: bool = False) -> object:
        from .revisions import rework
        return await rework(self, feedback, max_reworks=max_reworks, allow_replay=allow_replay)

    def rework(self, feedback: str, *, max_reworks: int = 3, allow_replay: bool = False) -> object:
        return default_stage_call_bridge.as_sync(self.async_rework)(
            feedback, max_reworks=max_reworks, allow_replay=allow_replay,
        )

    async def async_cancel(
        self, *, reason: str = "cancelled", timeout: float | None = None,
    ) -> AgentExecutionControlResult:
        """Cancel owned work and await settlement; timeout never means success."""
        return await cancel_execution(self, reason=reason, timeout=timeout)

    @property
    def control_capabilities(self) -> AgentExecutionControlCapabilities:
        """Describe implemented boundaries without starting the producer."""
        return {
            "pause_boundaries": ["before_production", "candidate_ready"],
            "snapshot_boundaries": [] if self._bound_agent_capabilities else ["before_production", "candidate_ready"],
            "resume": "explicit_pending_pause",
            "rework": ("same_execution_revision" if (
                self.__class__._async_produce is AgentExecution._async_produce
                or self.__class__._assert_rework_supported is not AgentExecution._assert_rework_supported
            ) else "unsupported"),
            "active_child_snapshot": False,
        }

    async def async_pause(self) -> AgentExecutionControlResult:
        """Request suspension at a safe producer boundary; receipt is not suspension."""
        return await pause_execution(self)

    async def async_interrupt(self, content: str, *, author: str | None = None) -> dict[str, object]:
        """Supply information to a future producer boundary; never cancel execution."""
        return await interrupt_execution(self, content, author=author)

    def interrupt(self, content: str, *, author: str | None = None) -> dict[str, object]:
        return default_stage_call_bridge.as_sync(self.async_interrupt)(content, author=author)

    def pause(self) -> AgentExecutionControlResult:
        return default_stage_call_bridge.as_sync(self.async_pause)()

    async def async_resume(self) -> object:
        """Continue a retained safe pause without replaying completed production."""
        return await resume_execution(self)

    def resume(self) -> object:
        return default_stage_call_bridge.as_sync(self.async_resume)()

    def save(self) -> dict[str, object]:
        """Return a data-only snapshot at a settled safe pause."""
        return save_execution(self)

    async def async_save(self) -> dict[str, object]:
        return self.save()

    def load(self, snapshot: Mapping[str, object]) -> "AgentExecution":
        """Restore into a fresh explicitly rebound draft; never dispatch or resume."""
        load_execution(self, snapshot)
        return self

    async def async_load(self, snapshot: Mapping[str, object]) -> "AgentExecution":
        return self.load(snapshot)

    def cancel(
        self, *, reason: str = "cancelled", timeout: float | None = None,
    ) -> AgentExecutionControlResult:
        return default_stage_call_bridge.as_sync(self.async_cancel)(reason=reason, timeout=timeout)

    async def async_close(
        self, *, reason: str = "closed", timeout: float | None = None,
        pending: Literal["error", "cancel"] = "error",
    ) -> AgentExecutionControlResult:
        """Drain and seal execution resources, preserving completed result readers."""
        return await close_execution(self, reason=reason, timeout=timeout, pending=pending)

    def close(
        self, *, reason: str = "closed", timeout: float | None = None,
        pending: Literal["error", "cancel"] = "error",
    ) -> AgentExecutionControlResult:
        return default_stage_call_bridge.as_sync(self.async_close)(
            reason=reason, timeout=timeout, pending=pending,
        )

    async def _await_route_with_limits(self, run_coro: Any):
        return await await_route_with_limits(self, run_coro)

    async def _cancel_limited_task(self, task: "asyncio.Task[Any]"):
        await cancel_limited_task(task)

    def _build_execution_stall_error(
        self,
        *,
        status: Literal["stalled", "timed_out"],
        message: str,
        elapsed_seconds: float | None,
        idle_seconds: float | None,
        timeout_seconds: float | None,
    ) -> RuntimeStageStallError:
        return build_execution_stall_error(
            self,
            status=status,
            message=message,
            elapsed_seconds=elapsed_seconds,
            idle_seconds=idle_seconds,
            timeout_seconds=timeout_seconds,
        )

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
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await async_get_data_entry(
            self,
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
        parent_run_context: "RunContext | None" = None,
    ) -> Any:
        return await async_get_full_data_entry(
            self,
            type=type,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            parent_run_context=parent_run_context,
        )

    async def async_get_text(
        self,
        *,
        parent_run_context: "RunContext | None" = None,
        **kwargs: Any,
    ) -> str:
        return await async_get_text_entry(self, parent_run_context=parent_run_context, **kwargs)

    async def async_get_meta(self) -> AgentExecutionMeta:
        return cast(AgentExecutionMeta, await async_get_meta_entry(self))

    async def async_record_data(
        self,
        *,
        purpose: "AgentExecutionRecordPurpose" = "process",
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
    ) -> "AgentExecutionRecordWrite":
        return await record_data_entry(
            self,
            purpose=purpose,
            collection=collection,
            kind=kind,
            content=content,
            summary=summary,
            scope=scope,
            source=source,
            meta=meta,
            checkpoint=checkpoint,
            checkpoint_state=checkpoint_state,
            checkpoint_step_id=checkpoint_step_id,
            profile=profile,
        )

    async def get_async_generator(
        self,
        type: Literal["delta", "instant", "streaming_parse", "all"] | str | None = "delta",
        content: Any = None,
        **_: Any,
    ) -> AsyncGenerator[Any, None]:
        async for item in get_async_generator_entry(self, type=type, content=content, **_):
            yield item

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return sync_generator_entry(self, *args, **kwargs)

    def _refresh_diagnostics(self) -> None:
        refresh_diagnostics(self)

    def _record_error_diagnostic(self, error: BaseException) -> dict[str, Any]:
        return record_error_diagnostic(self, error)

    def raise_if_limit_exceeded(self) -> None:
        self.execution_context.raise_if_limit_exceeded()

    def _record_scope(self, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        return record_scope(self, scope)

    def _record_source(self, source: dict[str, Any] | None = None) -> dict[str, Any]:
        return record_source(self, source)

    def _default_record_content(self) -> dict[str, Any]:
        return default_record_content(self)

    def _default_record_summary(self, collection: str) -> str:
        return default_record_summary(self, collection)

    def _default_checkpoint_state(self, record_ref: dict[str, Any]) -> dict[str, Any]:
        return default_checkpoint_state(self, record_ref)

    def _append_record_ref(self, key: str, ref: dict[str, Any]):
        append_record_ref(self, key, ref)
