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
from pathlib import Path

from agently.core.TaskWorkspace import TaskWorkspace, TaskWorkspaceContextSource
from agently.core.context import TaskContext
from agently.core.storage import RecordStore, RecordStoreContextSource

from .LifecycleState import AgentTaskLifecycleState
from .LifecycleFlow import AgentTaskLifecycleFlowMixin
from .TaskShared import *
from .StrategyRouter import AgentTaskStrategyRouterMixin
from .TaskBoardStrategy import AgentTaskTaskBoardStrategyMixin
from .ArtifactDelivery import AgentTaskArtifactMixin
from .FlatStrategy import AgentTaskFlatStrategyMixin
from .Carrier import AgentTaskCarrierMixin
from .TerminalVerification import AgentTaskTerminalVerificationMixin
from .AcpRecovery import AgentTaskAcpRecoveryMixin
from .Verification import AgentTaskVerificationMixin
from .Guidance import AgentTaskGuidanceMixin
from .RuntimeControl import AgentTaskRuntimeMixin
from .Resume import AgentTaskResumeMixin
from .Observation import AgentTaskObservationMixin
from .TaskEvidenceContextSource import TaskEvidenceContextSource


class AgentTask(
    AgentTaskLifecycleFlowMixin,
    AgentTaskStrategyRouterMixin,
    AgentTaskTaskBoardStrategyMixin,
    AgentTaskArtifactMixin,
    AgentTaskFlatStrategyMixin,
    AgentTaskCarrierMixin,
    AgentTaskTerminalVerificationMixin,
    AgentTaskAcpRecoveryMixin,
    AgentTaskVerificationMixin,
    AgentTaskGuidanceMixin,
    AgentTaskRuntimeMixin,
    AgentTaskResumeMixin,
    AgentTaskObservationMixin,
):
    """Retained owner for one Agent-managed business task lifecycle."""

    normalize_max_iterations = staticmethod(_normalize_agent_task_max_iterations)

    def __init__(
        self,
        agent: "BaseAgent",
        *,
        goal: str,
        success_criteria: list[str],
        execution: AgentTaskExecutionStrategy | str | None = "auto",
        record_store: RecordStore | None = None,
        task_context: TaskContext | None = None,
        task_workspace: TaskWorkspace | str | os.PathLike[str] | None = None,
        max_iterations: int | None = _AGENT_TASK_DEFAULT_MAX_ITERATIONS,
        verify: Literal["before_done"] = "before_done",
        context_profile: str = "auto",
        context_budget: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        task_id: str | None = None,
    ):
        if not str(goal or "").strip():
            raise ValueError("agent.create_task(...) requires a non-empty goal.")
        if not success_criteria:
            raise ValueError("agent.create_task(...) requires at least one success criterion.")
        self.agent = agent
        self.id = task_id or f"agent_task_{uuid.uuid4().hex}"
        agent_task_workspace = getattr(agent, "task_workspace", None)
        if isinstance(task_workspace, TaskWorkspace):
            # Routed AgentExecution hands AgentTask its already execution-scoped
            # file view. Preserve that exact view so fallback artifacts and the
            # shared TaskContext keep one identity.
            resolved_task_workspace = task_workspace
        else:
            requested_root = (
                Path(task_workspace).expanduser().resolve()
                if task_workspace is not None
                else None
            )
            if requested_root is None and isinstance(agent_task_workspace, TaskWorkspace):
                requested_root = agent_task_workspace.root
            if requested_root is None:
                resolved_task_workspace = None
            else:
                inherited_mode = (
                    agent_task_workspace.mode
                    if isinstance(agent_task_workspace, TaskWorkspace)
                    and agent_task_workspace.root == requested_root
                    else "read_write"
                )
                # A standalone AgentTask owns an execution-scoped view over the
                # selected file boundary; it must not reuse the Agent-wide
                # fallback namespace.
                resolved_task_workspace = TaskWorkspace(
                    requested_root,
                    mode=inherited_mode,
                    execution_id=self.id,
                )
        if not isinstance(resolved_task_workspace, TaskWorkspace):
            raise RuntimeError(
                "AgentTask requires a TaskWorkspace binding; pass task_workspace=... "
                "or call agent.use_task_workspace(...)."
            )
        self.task_workspace = resolved_task_workspace
        self._task_reference_catalog = TaskReferenceCatalog(self.id)
        owns_task_context = task_context is None
        if owns_task_context:
            self.task_context = TaskContext(
                task_id=self.id,
                context_id=f"agent_task:{self.id}:context",
            )
            self.task_context.attach(
                TaskWorkspaceContextSource(self.task_workspace),
                binding_id=f"task_workspace_binding:{self.id}",
                scope="task",
            )
        else:
            self.task_context = task_context
        evidence_binding_id = f"task_evidence_binding:{self.id}"
        if evidence_binding_id not in {
            binding.binding_id for binding in self.task_context.snapshot().bindings
        }:
            self.task_context.attach(
                TaskEvidenceContextSource(self._task_reference_catalog),
                binding_id=evidence_binding_id,
                scope="task",
                metadata={
                    "description": (
                        "Canonical body-bearing evidence produced during this AgentTask."
                    ),
                    # This source is a retrieval surface, not an instruction to
                    # resend every prior Action body in each ordinary context
                    # package. Scoped ContextReader plans opt into it by kind.
                    "disclosure_mode": "explicit_retrieval",
                },
            )
        self.context_readers: dict[tuple[str, str], Any] = {}
        self.context_packages: list[Any] = []
        self.context_consumptions: list[Any] = []
        self._terminal_convergence_state = TerminalConvergenceState(self.id)
        self.goal = str(goal)
        self.success_criteria = [str(item) for item in success_criteria if str(item).strip()]
        self.execution_strategy = self.normalize_execution_strategy(execution)
        resolved_options = dict(options or {})
        self.effective_execution_strategy: AgentTaskEffectiveExecutionStrategy | None = (
            cast(AgentTaskEffectiveExecutionStrategy, self.execution_strategy)
            if self.execution_strategy in {"flat", "taskboard"}
            else None
        )
        self._lifecycle_state = AgentTaskLifecycleState(
            task_id=self.id,
            requested_strategy=self.execution_strategy,
            effective_strategy=self.effective_execution_strategy,
            skill_bindings=(
                {
                    str(item.get("binding_id") or ""): dict(item)
                    for item in resolved_options.get("skill_bindings", [])
                    if isinstance(item, Mapping) and str(item.get("binding_id") or "").strip()
                }
                if isinstance(resolved_options.get("skill_bindings"), Sequence)
                and not isinstance(
                    resolved_options.get("skill_bindings"),
                    str | bytes | bytearray,
                )
                else {}
            ),
        )
        self._terminal_inline_values: dict[str, str] = {}
        self._terminal_materialization_diagnostics: list[dict[str, Any]] = []
        self._lifecycle_frames: dict[str, dict[str, Any]] = {}
        self._lifecycle_error: BaseException | None = None
        self.task_shape_analysis: dict[str, Any] = {}
        self.max_iterations = _normalize_agent_task_max_iterations(max_iterations)
        self.verify = verify
        self.context_profile = context_profile
        self.context_budget = dict(context_budget or {"chars": 6000})
        self.limits = dict(limits or {})
        self.options = resolved_options
        bound_record_store = record_store or getattr(agent, "record_store", None)
        if not isinstance(bound_record_store, RecordStore):
            raise RuntimeError(
                "AgentTask requires a RecordStore binding; pass record_store=... "
                "or configure agent.use_record_store(...)."
            )
        self.record_store: RecordStore = bound_record_store._bind_execution(
            self.id,
            scope={"task_id": self.id, "execution_id": self.id},
            search_scope={"task_id": self.id, "execution_id": self.id},
        )
        if owns_task_context:
            self.task_context.attach(
                RecordStoreContextSource(self.record_store),
                binding_id=f"record_store_binding:{self.id}",
                scope="task",
            )
        self.status: AgentTaskStatus = "created"
        self.result: Any = None
        self.diagnostics: dict[str, Any] = {}
        self.iterations: list[dict[str, Any]] = []
        self.record_refs: dict[str, list[str]] = {
            "observations": [],
            "decisions": [],
            "verification": [],
            "checkpoints": [],
            "evidence_links": [],
            "strategy": [],
            "reflections": [],
            "acp_recovery": [],
            "guidance": [],
        }
        self.guidance_items: list[dict[str, Any]] = []
        self._guidance_sequence = 0
        self._guidance_lock = asyncio.Lock()
        self.reflections: list[dict[str, Any]] = []
        self.created_at = time.time()
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self._completed = False
        self._error: BaseException | None = None
        self._start_lock = asyncio.Lock()
        # Required capabilities are satisfied cumulatively across iterations: a
        # capability used by any bounded step counts as satisfied for the task,
        # so a model_request step and a skills step in different iterations can
        # together satisfy required actions and required skills.
        self._satisfied_required_actions: set[str] = set()
        self._satisfied_required_skills: set[str] = set()
        # Capability ids used in any bounded step, accumulated across iterations so
        # a capability-evidence requirement can be satisfied cumulatively.
        self._satisfied_capabilities: set[str] = set()
        # Action ids that succeeded in any bounded step, accumulated the same way
        # so an action_succeeded evidence requirement is not lost on a later step.
        self._satisfied_succeeded_actions: set[str] = set()
        # Execution shapes that failed earlier in this task. In auto mode the
        # planner should adapt instead of repeatedly selecting the same failing
        # route shape.
        self._failed_execution_shapes: set[str] = set()
        # Durable resume state (populated by AgentTask.async_resume before run).
        self._resumed_from_iteration: int = 0
        self._resumed_iteration_summaries: list[dict[str, Any]] = []
        self._resumed_taskboard_state: dict[str, Any] | None = None
        self._latest_taskboard_acceptance_index: dict[str, Any] | None = None
        # TaskBoard read progress is operational task state, not diagnostics.
        # It is keyed by the stable owner/locator/content-version identity so
        # narrower evidence projections and durable resume do not reread a
        # completed byte range or reuse progress for changed content.
        self._taskboard_read_progress: dict[str, Any] = {
            "schema_version": "agent_task_taskboard_read_progress/v1",
            "items": {},
        }
        self._taskboard_planned_task_workspace_deliverables: list[str] = []
        self._resumed_prior_result: Any = None
        self._terminal_deliverable_refs: list[RecordRef] = []
        self._terminal_retained_refs: list[Any] = []
        self._terminal_retention_deferred = False
        # Routed AgentTask construction transfers this exact task-owned Action
        # artifact scope to its parent AgentExecution. Standalone tasks keep the
        # value unset and release the scope in their own terminal seam.
        self._action_artifact_scope_transferred_to_execution_id: str | None = None
        self._terminal_taskboard_state: dict[str, Any] | None = None
        self._stream_items: list[AgentExecutionStreamData] = []
        self._stream_queues: list[asyncio.Queue[Any]] = []
        self._background_stream_tasks: set[asyncio.Task[Any]] = set()
        self._emitted_action_event_keys: set[tuple[str, str, str, str, str]] = set()
        self._last_stream_emit_monotonic = time.monotonic()
        self._last_heartbeat_emit_monotonic = 0.0
        self._flow = self._build_flow()

        self.run: Any = self._run
        self.meta: Any = self._meta
        self.get_meta: Any = self.meta
        self.add_guidance: Any = default_stage_call_bridge.as_sync(self.async_add_guidance)
        self.stream: Any = self.get_async_generator
        self.get_generator: Any = self._get_generator

__all__ = [
    "AgentTask",
    "AgentTaskStatus",
    "AgentTaskExecutionStrategy",
    "AgentTaskEffectiveExecutionStrategy",
]
