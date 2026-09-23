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

from agently.types.data import ContextPackage

from .TaskShared import (
    Any,
    build_task_board_evidence_view,
    cast,
    coerce_task_board_planning_result,
    DataFormatter,
    json,
    Mapping,
    Path,
    relevant_state_digest,
    resolve_task_board_planning_policy,
    SCOPED_RETRIEVAL_RESULT_CAPACITY,
    Sequence,
    task_board_card_required,
    task_board_explicit_state_facts,
    task_board_planning_output_schema,
    TaskBoard,
    TaskBoardRevision,
    TaskContextView,
    TerminalIssue,
    TriggerFlow,
    TriggerFlowRuntimeData,
    _AgentTaskDeadlineExceeded,
    _compact_agent_task_error_message,
)
from .TaskBoardCardExecution import AgentTaskTaskBoardCardExecutionMixin
from .TaskBoardFinalization import AgentTaskTaskBoardFinalizationMixin
from .TaskBoardPatching import AgentTaskTaskBoardPatchingMixin
from .TaskBoardProjection import AgentTaskTaskBoardProjectionMixin
from .TaskBoardReadback import AgentTaskTaskBoardReadbackMixin
from .TaskBoardRuntimeOptions import AgentTaskTaskBoardRuntimeOptionsMixin
from .TaskBoardScopedRetrieval import AgentTaskTaskBoardScopedRetrievalMixin
from .TaskBoardSourceRefs import AgentTaskTaskBoardSourceRefsMixin


class AgentTaskTaskBoardStrategyMixin(
    AgentTaskTaskBoardCardExecutionMixin,
    AgentTaskTaskBoardRuntimeOptionsMixin,
    AgentTaskTaskBoardScopedRetrievalMixin,
    AgentTaskTaskBoardSourceRefsMixin,
    AgentTaskTaskBoardFinalizationMixin,
    AgentTaskTaskBoardReadbackMixin,
    AgentTaskTaskBoardPatchingMixin,
    AgentTaskTaskBoardProjectionMixin,
):
    _latest_taskboard_acceptance_index: dict[str, Any] | None

    def _set_taskboard_planned_task_workspace_deliverables(self, revision: Any) -> list[str]:
        """Cache model-selected final paths after host-owned TaskWorkspace validation."""

        accepted: list[str] = []
        invalid: list[dict[str, Any]] = []
        graph = getattr(revision, "graph", None)
        for card in list(getattr(graph, "cards", ()) or ()):
            metadata = getattr(card, "metadata", None)
            if not isinstance(metadata, Mapping):
                continue
            for raw_path in self._normalize_string_list(
                metadata.get("final_task_workspace_deliverables")
            ):
                path = Path(raw_path)
                try:
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("TaskBoard final deliverable paths must stay TaskWorkspace-relative.")
                    self.task_workspace.resolve_file_path(raw_path)
                except Exception as error:
                    invalid.append(
                        {
                            "card_id": str(getattr(card, "id", "") or ""),
                            "path": raw_path,
                            "error": str(error),
                        }
                    )
                    continue
                normalized = path.as_posix()
                if normalized not in accepted:
                    accepted.append(normalized)
        self._taskboard_planned_task_workspace_deliverables = accepted
        if invalid:
            self.diagnostics.setdefault("taskboard_invalid_final_deliverables", []).extend(invalid)
        return accepted

    @staticmethod
    def _taskboard_card_convergence_subject(card: Any) -> str:
        metadata = getattr(card, "metadata", None)
        if isinstance(metadata, Mapping):
            supplied = str(metadata.get("terminal_convergence_subject") or "").strip()
            if supplied:
                return supplied
        evidence_contract = getattr(card, "evidence_contract", None)
        grounding_contract = (
            evidence_contract.get("material_claim_repair_contract")
            if isinstance(evidence_contract, Mapping)
            else None
        )
        if isinstance(grounding_contract, Mapping):
            supplied = str(grounding_contract.get("contract_subject") or "").strip()
            if supplied:
                return supplied
        card_id = str(getattr(card, "id", "") or "").strip()
        return f"taskboard_card:{card_id}"

    @staticmethod
    def _taskboard_card_reference_targets(result: Any) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        for collection_name in ("artifact_refs", "file_refs"):
            collection = getattr(result, collection_name, ())
            if not isinstance(collection, Sequence) or isinstance(
                collection,
                str | bytes | bytearray,
            ):
                continue
            for raw_ref in collection:
                if not isinstance(raw_ref, Mapping):
                    continue
                projected = {
                    field: DataFormatter.sanitize(raw_ref.get(field))
                    for field in (
                        "id",
                        "record_id",
                        "reference_id",
                        "locator_id",
                        "content_version_id",
                        "path",
                        "sha256",
                        "role",
                    )
                    if raw_ref.get(field) not in (None, "", [], {})
                }
                if projected:
                    targets.append(projected)
        return targets

    def _taskboard_card_convergence_result(
        self,
        revision: TaskBoardRevision | Mapping[str, Any],
        *,
        executed_card_ids: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """Stop an unbounded board after one required card stays unsatisfied three times."""

        effective_revision = TaskBoardRevision.from_value(revision)
        executed = (
            {str(card_id) for card_id in executed_card_ids if str(card_id)} if executed_card_ids is not None else None
        )
        for card in effective_revision.graph.cards:
            if executed is not None and card.id not in executed:
                continue
            result = effective_revision.card_results.get(card.id)
            if result is None or not task_board_card_required(card):
                continue
            status = str(result.status or "").strip().lower()
            if status not in {"setback", "failed", "blocked"}:
                continue
            contract_subject = self._taskboard_card_convergence_subject(card)
            issue = TerminalIssue(
                "taskboard_card",
                "required_card_unsatisfied",
                contract_subject,
            )
            evidence_contract = getattr(card, "evidence_contract", None)
            requires_capability_ids = self._normalize_string_list(
                evidence_contract.get("requires_capability_ids") if isinstance(evidence_contract, Mapping) else None
            )
            repair_contract = {
                "gate_kind": issue.gate_kind,
                "issue_code": issue.issue_code,
                "contract_subject": issue.contract_subject,
                "requirements": [
                    {
                        "card_id": card.id,
                        "status": status,
                        "allowed_execution_shape": str(getattr(card, "allowed_execution_shape", "") or "auto"),
                        "requires_capability_ids": requires_capability_ids,
                    }
                ],
            }
            reference_targets = self._taskboard_card_reference_targets(result)
            metadata = getattr(card, "metadata", None)
            generated_by = (
                str(metadata.get("generated_by") or "").strip()
                if isinstance(metadata, Mapping)
                else ""
            )
            if generated_by == "agent_task.taskboard.final_verification_repair":
                reference_targets = [
                    target
                    for target in reference_targets
                    if "/terminal-candidates/"
                    not in str(target.get("path") or "")
                    and str(target.get("role") or "")
                    != "taskboard_terminal_candidate"
                ]
            target_artifact_paths = self._normalize_string_list(
                metadata.get("final_task_workspace_deliverables")
                if isinstance(metadata, Mapping)
                else None
            )
            state_digest = relevant_state_digest(
                {
                    "source_reference_targets": reference_targets,
                    "capability_facts": {capability_id: "required" for capability_id in requires_capability_ids},
                    "output_subjects": [contract_subject, *target_artifact_paths],
                    # setback/failed/blocked and regenerated repair-card ids are
                    # equivalent observations of the same unresolved contract.
                    # Model-authored output_digest prose is not a content
                    # identity and must not masquerade as relevant-state
                    # progress. Actual new artifacts still reset convergence
                    # through their versioned source_reference_targets.
                    "repair_contract": {
                        "allowed_execution_shape": str(
                            getattr(card, "allowed_execution_shape", "") or "auto"
                        ),
                        "requires_capability_ids": requires_capability_ids,
                    },
                }
            )
            decision = self._terminal_convergence_state.record_detection(
                issue,
                state_digest,
                repair_contract=repair_contract,
                verifier_called=False,
            )
            convergence = {
                **dict(DataFormatter.sanitize(decision)),
                "issue": {
                    "gate_kind": issue.gate_kind,
                    "issue_code": issue.issue_code,
                    "contract_subject": issue.contract_subject,
                },
                "repair_contract": repair_contract,
                "relevant_state_digest": state_digest,
                "stopped_after_third_occurrence": decision.get("terminal") is True,
            }
            self.diagnostics.setdefault("taskboard_card_terminal_convergence", []).append(
                {
                    "board_id": effective_revision.board_id,
                    "revision_id": effective_revision.revision_id,
                    "card_id": card.id,
                    "status": status,
                    "terminal_convergence": convergence,
                }
            )
            self.diagnostics["terminal_convergence"] = self._terminal_convergence_state.snapshot()
            if decision.get("terminal") is not True:
                continue
            reason = (
                "TaskBoard stopped because the same required card contract remained unsatisfied "
                f"across three executions: {card.id}."
            )
            terminal_result = {
                "terminal": True,
                "status": "blocked",
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "final_result": reason,
                "final_response": reason,
                "reason": reason,
                "missing_criteria": [reason],
                "artifact_refs": [],
                "terminal_convergence": convergence,
            }
            self.status = "blocked"
            self.result = terminal_result
            return terminal_result
        return None

    @staticmethod
    def _taskboard_tick_executed_card_ids(tick_result: Any) -> tuple[str, ...]:
        executed: list[str] = []

        def add_card_ids(value: Any) -> None:
            if isinstance(value, Mapping):
                candidates = value.keys()
            elif isinstance(value, Sequence) and not isinstance(
                value,
                str | bytes | bytearray,
            ):
                candidates = value
            else:
                return
            for card_id in candidates:
                normalized = str(card_id)
                if normalized and normalized not in executed:
                    executed.append(normalized)

        snapshot = getattr(tick_result, "triggerflow_snapshot", None)
        if isinstance(snapshot, Mapping):
            add_card_ids(snapshot.get("collected_card_results"))
            collected_json = snapshot.get("collected_card_results_json")
            if isinstance(collected_json, str) and collected_json.strip():
                try:
                    parsed = json.loads(collected_json)
                except json.JSONDecodeError:
                    parsed = None
                add_card_ids(parsed)
            add_card_ids(snapshot.get("expected_card_ids"))
            expected_json = snapshot.get("expected_card_ids_json")
            if isinstance(expected_json, str) and expected_json.strip():
                try:
                    parsed = json.loads(expected_json)
                except json.JSONDecodeError:
                    parsed = None
                add_card_ids(parsed)
            if executed:
                return tuple(executed)
        schedule = getattr(tick_result, "schedule", None)
        runnable_card_ids = getattr(schedule, "runnable_card_ids", ())
        add_card_ids(runnable_card_ids)
        return tuple(executed)

    async def _run_taskboard(self) -> dict[str, Any]:
        """Compatibility entry point for the TaskBoard work-producer subflow."""

        frame: dict[str, Any] = {"iteration": 1}
        frame = await self._taskboard_context_prepare_stage(frame)
        frame = await self._taskboard_work_plan_stage(frame)
        if self.effective_execution_strategy == "flat":
            for stage in (
                self._flat_work_execute_stage,
                self._flat_outputs_materialize_stage,
                self._flat_evidence_ingest_stage,
                self._flat_terminal_verify_stage,
                self._flat_transition_decide_stage,
            ):
                frame = await stage(frame)
                if frame.get("iteration_result") is not None:
                    break
        else:
            for stage in (
                self._taskboard_work_execute_stage,
                self._taskboard_outputs_materialize_stage,
                self._taskboard_evidence_ingest_stage,
                self._taskboard_terminal_verify_stage,
                self._taskboard_transition_decide_stage,
            ):
                frame = await stage(frame)
                if frame.get("iteration_result") is not None:
                    break
        result = frame.get("iteration_result")
        if not isinstance(result, Mapping):
            raise ValueError("TaskBoard lifecycle did not produce a structured result.")
        return dict(result)

    async def _taskboard_context_prepare_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        iteration_index = int(frame["iteration"])
        try:
            if self._task_deadline_exceeded():
                frame["iteration_result"] = await self._terminate_timed_out(
                    iteration_index
                )
                return frame
            await self._emit_progress(
                iteration_index,
                "context",
                "TaskBoard: building a TaskContext package for board planning.",
            )
            await self._apply_guidance_boundary(iteration_index=iteration_index, boundary="taskboard_context")
            context_pack, context_package = await self._await_task_deadline(
                self._read_task_context_view(
                    phase="planning",
                    consumer_id=f"agent_task:{self.id}:taskboard-planner",
                    intent=f"Plan the TaskBoard: {self.goal}",
                ),
                stage="context",
            )
            frame["planning_context_package"] = context_package
            await self._emit("agent_task.taskboard.context", context_pack)
            required_skill_blocker = self._required_skill_context_blocker(context_pack)
            if required_skill_blocker is not None:
                frame["context_pack"] = context_pack
                frame["iteration_result"] = await self._terminate_required_skill_context_blocked(
                    iteration_index,
                    required_skill_blocker,
                )
                return frame
            carried_revision = frame.get("taskboard_revision")
            if isinstance(carried_revision, Mapping):
                resumed_taskboard_state = {
                    "revision": DataFormatter.sanitize(carried_revision),
                    "stage": "outer_lifecycle_transition",
                    "tick_index": int(frame.get("taskboard_tick_index") or 0),
                    **(
                        {
                            "acceptance_index": DataFormatter.sanitize(
                                frame["taskboard_acceptance_index"]
                            )
                        }
                        if isinstance(frame.get("taskboard_acceptance_index"), Mapping)
                        else {}
                    ),
                }
            else:
                resumed_taskboard_state = (
                    self._resumed_taskboard_state
                    if isinstance(self._resumed_taskboard_state, Mapping)
                    else None
                )
            resumed_revision: TaskBoardRevision | None = None
            if resumed_taskboard_state is not None and isinstance(resumed_taskboard_state.get("revision"), Mapping):
                resumed_revision = TaskBoardRevision.from_value(resumed_taskboard_state["revision"])
        except _AgentTaskDeadlineExceeded as error:
            frame["iteration_result"] = await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
            return frame
        frame["context_pack"] = context_pack
        frame["resumed_taskboard_state"] = resumed_taskboard_state
        frame["resumed_revision"] = resumed_revision
        return frame

    async def _taskboard_work_plan_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        context_pack = cast("TaskContextView", frame["context_pack"])
        resumed_taskboard_state = cast(
            Mapping[str, Any] | None,
            frame.get("resumed_taskboard_state"),
        )
        resumed_revision = cast(
            TaskBoardRevision | None,
            frame.get("resumed_revision"),
        )
        try:
            if resumed_revision is None:
                planning_result = self._initial_taskboard_plan_from_shape_analysis()
                if planning_result is None:
                    await self._emit_progress(
                        iteration_index,
                        "taskboard_plan",
                        "TaskBoard: asking the model to plan the initial board.",
                    )
                    planning_result = await self._await_task_deadline(
                        self._request_taskboard_plan(
                            context_pack,
                            context_package=frame.get("planning_context_package"),
                        ),
                        stage="taskboard_plan",
                    )
                else:
                    await self._emit_progress(
                        iteration_index,
                        "taskboard_plan",
                        "TaskBoard: reusing the task-shape analysis initial board plan.",
                    )
                board_revision = planning_result.revision
                planning_policy = planning_result.planning_policy
            else:
                planning_policy = resolve_task_board_planning_policy(
                    self._taskboard_effort(),
                    metadata={
                        "execution_strategy": self.execution_strategy,
                        "task_id": self.id,
                        "resume": True,
                    },
                )
                board_revision = resumed_revision
        except _AgentTaskDeadlineExceeded as error:
            frame["iteration_result"] = await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
            return frame

        self._set_taskboard_planned_task_workspace_deliverables(board_revision)

        if resumed_revision is None and self._taskboard_should_fallback_to_flat(board_revision):
            self.diagnostics.setdefault("execution_strategy_gates", []).append(
                {
                    "gate": "taskboard_small_linear_board_fallback",
                    "accepted": True,
                    "card_count": len(getattr(board_revision.graph, "cards", ()) or ()),
                }
            )
            self._set_effective_execution_strategy("flat", source="taskboard_small_linear_board_fallback")
            return await self._flat_work_plan_stage(frame)

        board = TaskBoard(
            board_revision,
            handler=lambda context: self._run_taskboard_card(context, context_pack),
            planning_policy=planning_policy,
            scheduler=self._taskboard_scheduler(),
        )
        if resumed_revision is None:
            await self._record_phase(
                "taskboard_planned",
                iteration=iteration_index,
                diagnostics={
                    "board_id": board.revision.board_id,
                    "revision_id": board.revision.revision_id,
                    "card_count": len(board.revision.graph.cards),
                    "execution_strategy": self.execution_strategy,
                },
            )
            await self._emit(
                "agent_task.taskboard.plan",
                {
                    "revision": board.revision.to_dict(),
                    "planning_policy": planning_policy.to_prompt_payload(),
                },
            )
        else:
            await self._record_phase(
                "taskboard_resumed",
                iteration=iteration_index,
                diagnostics={
                    "board_id": board.revision.board_id,
                    "revision_id": board.revision.revision_id,
                    "card_count": len(board.revision.graph.cards),
                    "execution_strategy": self.execution_strategy,
                    "checkpoint_stage": resumed_taskboard_state.get("stage") if resumed_taskboard_state else None,
                    "checkpoint_tick_index": (
                        resumed_taskboard_state.get("tick_index") if resumed_taskboard_state else None
                    ),
                },
            )
            await self._emit(
                "agent_task.taskboard.resumed",
                {
                    "revision_id": board.revision.revision_id,
                    "tick_index": resumed_taskboard_state.get("tick_index") if resumed_taskboard_state else None,
                },
            )
        frame["board"] = board
        frame["planning_policy"] = planning_policy
        frame["board_revision"] = board_revision
        return frame

    async def _taskboard_work_execute_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        context_pack = cast("TaskContextView", frame["context_pack"])
        resumed_taskboard_state = cast(
            Mapping[str, Any] | None,
            frame.get("resumed_taskboard_state"),
        )
        board = cast(TaskBoard, frame["board"])
        planning_policy = frame["planning_policy"]
        lifecycle_flow = TriggerFlow(name=f"agent-task-taskboard-lifecycle-{ self.id }")
        tick_requested_event = f"agent_task.taskboard.lifecycle.tick.requested.{ self.id }"
        finalize_requested_event = f"agent_task.taskboard.lifecycle.finalize.requested.{ self.id }"
        revision_state_key = "taskboard_revision_json"
        initial_tick_index = 1
        if resumed_taskboard_state is not None:
            try:
                initial_tick_index = max(int(resumed_taskboard_state.get("tick_index") or 0) + 1, 1)
            except (TypeError, ValueError):
                initial_tick_index = 1

        def _board_from_revision(revision: TaskBoardRevision | Mapping[str, Any]) -> TaskBoard:
            return TaskBoard(
                revision,
                handler=lambda context: self._run_taskboard_card(context, context_pack),
                planning_policy=planning_policy,
                scheduler=self._taskboard_scheduler(),
            )

        def _pack_revision_state(revision: TaskBoardRevision | Mapping[str, Any]) -> str:
            effective_revision = TaskBoardRevision.from_value(revision)
            return json.dumps(effective_revision.to_dict(), ensure_ascii=False, sort_keys=True)

        def _unpack_revision_state(data: TriggerFlowRuntimeData[Any, Any, Any]) -> TaskBoardRevision:
            raw_revision = data.get_state(revision_state_key, None, inherit=False)
            if isinstance(raw_revision, str) and raw_revision.strip():
                return TaskBoardRevision.from_value(json.loads(raw_revision))
            if isinstance(raw_revision, Mapping):
                return TaskBoardRevision.from_value(raw_revision)
            return TaskBoardRevision.from_value(board.revision)

        async def _sync_latest_acceptance_index(data: TriggerFlowRuntimeData[Any, Any, Any]) -> None:
            latest_acceptance_index = getattr(self, "_latest_taskboard_acceptance_index", None)
            if isinstance(latest_acceptance_index, Mapping):
                await data.async_set_state(
                    "taskboard_acceptance_index",
                    DataFormatter.sanitize(latest_acceptance_index),
                    emit=False,
                )

        async def start_lifecycle(data: TriggerFlowRuntimeData[Any, Any, Any]):
            max_ticks = self._taskboard_max_ticks()
            max_ticks_source = self._taskboard_max_ticks_source()
            topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick_requested_event": tick_requested_event,
                "finalize_requested_event": finalize_requested_event,
                "max_ticks": max_ticks,
                "max_ticks_source": max_ticks_source,
                "tick_fanout": (
                    "taskboard_runtime_frontier_signal_net"
                    if self._taskboard_scheduler() == "frontier"
                    else "taskboard_runtime_signal_net"
                ),
            }
            await data.async_set_state(revision_state_key, _pack_revision_state(board.revision), emit=False)
            await data.async_set_state("tick_index", initial_tick_index, emit=False)
            await data.async_set_state("max_ticks", max_ticks, emit=False)
            await data.async_set_state("runtime_topology", topology, emit=False)
            if resumed_taskboard_state is not None and isinstance(
                resumed_taskboard_state.get("acceptance_index"), Mapping
            ):
                self._latest_taskboard_acceptance_index = DataFormatter.sanitize(
                    resumed_taskboard_state.get("acceptance_index")
                )
                await _sync_latest_acceptance_index(data)
            await self._record_phase(
                "taskboard_lifecycle_started",
                iteration=iteration_index,
                diagnostics={
                    "revision_id": board.revision.revision_id,
                    "runtime_topology": topology,
                },
            )
            await self._emit(
                "agent_task.taskboard.lifecycle.started",
                {
                    "revision_id": board.revision.revision_id,
                    "runtime_topology": topology,
                },
            )
            if max_ticks is not None and initial_tick_index > max_ticks:
                await data.async_set_state("terminal_reason", "max_ticks", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": initial_tick_index - 1})
            else:
                await data.async_emit_nowait(tick_requested_event, {"tick_index": initial_tick_index})
            return {"runtime_topology": topology}

        async def run_lifecycle_tick(data: TriggerFlowRuntimeData[Any, Any, Any]):
            if data.get_state("terminal_result", None, inherit=False) is not None:
                return data.get_state("terminal_result", inherit=False)
            if data.get_state("final_result", None, inherit=False) is not None:
                return data.get_state("final_result", inherit=False)
            payload = data.input if isinstance(data.input, Mapping) else {}
            try:
                tick_index = int(payload.get("tick_index") or data.get_state("tick_index", 1, inherit=False) or 1)
            except (TypeError, ValueError):
                tick_index = 1
            await self._apply_guidance_boundary(iteration_index=tick_index, boundary="taskboard_tick")
            max_ticks = data.get_state("max_ticks", None, inherit=False)
            max_ticks_int: int | None
            try:
                max_ticks_int = int(max_ticks) if max_ticks is not None else None
            except (TypeError, ValueError):
                max_ticks_int = self._taskboard_max_ticks()
            if self._task_deadline_exceeded():
                result = await self._terminate_timed_out(tick_index, stage="taskboard_tick")
                await data.async_set_state("terminal_result", result, emit=False)
                return result

            revision = _unpack_revision_state(data)
            current_board = _board_from_revision(revision)
            schedule = current_board.schedule()
            tick_concurrency = self._taskboard_concurrency()
            evidence_view = build_task_board_evidence_view(current_board.revision).to_dict()
            runtime_topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick": DataFormatter.sanitize(data.get_state("runtime_topology", {}, inherit=False) or {}),
            }
            await self._emit(
                f"agent_task.taskboard.tick.{tick_index}.scheduled",
                self._taskboard_scheduled_stream_payload(
                    schedule=schedule,
                    evidence_view=evidence_view,
                    concurrency=tick_concurrency,
                ),
            )
            if not schedule.runnable_card_ids:
                await data.async_set_state(revision_state_key, _pack_revision_state(current_board.revision), emit=False)
                await data.async_set_state("terminal_reason", "no_runnable_cards", emit=False)
                await self._record_taskboard_checkpoint(
                    stage="tick",
                    tick_index=tick_index,
                    revision=current_board.revision,
                    runtime_topology=runtime_topology,
                    terminal_reason="no_runnable_cards",
                )
                await _sync_latest_acceptance_index(data)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
                return {"terminal": False, "status": "ready_to_finalize"}

            try:
                tick_result = await self._await_task_deadline(
                    current_board.async_run_tick(timeout=None, concurrency=tick_concurrency),
                    stage="taskboard_tick",
                )
            except _AgentTaskDeadlineExceeded as error:
                result = await self._terminate_timed_out(
                    tick_index,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result
            except TimeoutError:
                result = await self._terminate_timed_out(
                    tick_index,
                    stage="taskboard_tick",
                    reason="TaskBoard tick timed out.",
                    limit_name="taskboard_tick_timeout_seconds",
                    timeout_seconds=self._taskboard_tick_timeout(),
                )
                await data.async_set_state("terminal_result", result, emit=False)
                return result

            await data.async_set_state(revision_state_key, _pack_revision_state(tick_result.revision), emit=False)
            await data.async_set_state("tick_index", tick_index + 1, emit=False)
            frame["taskboard_tick_index"] = tick_index
            frame["taskboard_revision"] = tick_result.revision.to_dict()
            await self._emit(
                f"agent_task.taskboard.tick.{tick_index}.completed",
                self._taskboard_completed_stream_payload(tick_result),
            )
            tick_runtime_topology = {
                "driver": "triggerflow_taskboard_lifecycle",
                "tick": DataFormatter.sanitize(tick_result.triggerflow_snapshot.get("runtime_topology", {})),
            }
            await self._record_phase(
                "taskboard_tick",
                iteration=tick_index,
                diagnostics={
                    "revision_id": tick_result.revision.revision_id,
                    "runnable_card_ids": list(tick_result.schedule.runnable_card_ids),
                    "completed_card_ids": list(tick_result.schedule.completed_card_ids),
                    "concurrency": tick_concurrency,
                    "runtime_topology": tick_runtime_topology,
                },
            )
            convergence_result = self._taskboard_card_convergence_result(
                tick_result.revision,
                executed_card_ids=self._taskboard_tick_executed_card_ids(tick_result),
            )
            if convergence_result is not None:
                await data.async_set_state(
                    "terminal_reason",
                    "terminal_convergence_stopped",
                    emit=False,
                )
                await data.async_set_state(
                    "terminal_result",
                    convergence_result,
                    emit=False,
                )
                await self._record_taskboard_checkpoint(
                    stage="terminal_convergence",
                    tick_index=tick_index,
                    revision=tick_result.revision,
                    runtime_topology=tick_runtime_topology,
                    terminal_reason="terminal_convergence_stopped",
                    final_result=convergence_result,
                )
                await _sync_latest_acceptance_index(data)
                await self._emit(
                    "agent_task.terminal_convergence",
                    convergence_result["terminal_convergence"],
                )
                return convergence_result
            await self._record_taskboard_checkpoint(
                stage="tick",
                tick_index=tick_index,
                revision=tick_result.revision,
                runtime_topology=tick_runtime_topology,
            )
            await _sync_latest_acceptance_index(data)
            if self._taskboard_revision_completed(tick_result.revision):
                await data.async_set_state("terminal_reason", "board_completed", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
            elif max_ticks_int is not None and tick_index >= max_ticks_int:
                await data.async_set_state("terminal_reason", "max_ticks_reached", emit=False)
                await data.async_emit_nowait(finalize_requested_event, {"tick_index": tick_index})
            else:
                await data.async_emit_nowait(tick_requested_event, {"tick_index": tick_index + 1})
            return tick_result.revision.to_dict()

        async def finalize_lifecycle(data: TriggerFlowRuntimeData[Any, Any, Any]):
            if data.get_state("terminal_result", None, inherit=False) is not None:
                return data.get_state("terminal_result", inherit=False)
            if data.get_state("final_result", None, inherit=False) is not None:
                return data.get_state("final_result", inherit=False)
            revision = _unpack_revision_state(data)
            previous_acceptance_index = data.get_state("taskboard_acceptance_index", None, inherit=False)
            if not isinstance(previous_acceptance_index, Mapping):
                previous_acceptance_index = getattr(self, "_latest_taskboard_acceptance_index", None)
            tick_index = max(
                int(data.get_state("tick_index", 1, inherit=False) or 1) - 1,
                1,
            )
            result = {
                "terminal": False,
                "status": "ready_to_finalize",
                "revision": revision.to_dict(),
                "tick_index": tick_index,
                "terminal_reason": str(
                    data.get_state("terminal_reason", "", inherit=False) or ""
                ),
                **(
                    {
                        "previous_acceptance_index": DataFormatter.sanitize(
                            previous_acceptance_index
                        )
                    }
                    if isinstance(previous_acceptance_index, Mapping)
                    else {}
                ),
            }
            await data.async_set_state("final_result", result, emit=False)
            await self._record_taskboard_checkpoint(
                stage="work_execute",
                tick_index=tick_index,
                revision=revision,
                runtime_topology=DataFormatter.sanitize(data.get_state("runtime_topology", {}, inherit=False) or {}),
                terminal_reason=str(data.get_state("terminal_reason", "", inherit=False) or "") or None,
                final_result=result,
            )
            await _sync_latest_acceptance_index(data)
            return result

        lifecycle_flow.to(start_lifecycle, name="task_board.lifecycle.start")
        lifecycle_flow.when(tick_requested_event).to(run_lifecycle_tick, name="task_board.lifecycle.tick")
        lifecycle_flow.when(finalize_requested_event).to(finalize_lifecycle, name="task_board.lifecycle.finalize")

        execution = lifecycle_flow.create_execution(auto_close=False, concurrency=1)
        await execution.async_start(board.revision.to_dict())
        snapshot = await execution.async_close()
        terminal_result = snapshot.get("terminal_result")
        if isinstance(terminal_result, Mapping):
            frame["iteration_result"] = dict(terminal_result)
            return frame
        result = snapshot.get("final_result")
        if isinstance(result, Mapping) and isinstance(result.get("revision"), Mapping):
            frame["taskboard_revision"] = DataFormatter.sanitize(result["revision"])
            frame["taskboard_tick_index"] = int(result.get("tick_index") or 0)
            frame["taskboard_terminal_reason"] = str(result.get("terminal_reason") or "")
            if isinstance(result.get("previous_acceptance_index"), Mapping):
                frame["taskboard_acceptance_index"] = DataFormatter.sanitize(
                    result["previous_acceptance_index"]
                )
            frame["taskboard_work_result"] = DataFormatter.sanitize(result)
            return frame
        if isinstance(result, Mapping):
            frame["iteration_result"] = dict(result)
            return frame
        raw_revision = snapshot.get(revision_state_key)
        if isinstance(raw_revision, str) and raw_revision.strip():
            revision = TaskBoardRevision.from_value(json.loads(raw_revision))
        else:
            revision = TaskBoardRevision.from_value(board.revision)
        frame["taskboard_revision"] = revision.to_dict()
        frame["taskboard_tick_index"] = max(initial_tick_index - 1, 0)
        return frame

    async def _taskboard_outputs_materialize_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        revision = TaskBoardRevision.from_value(frame["taskboard_revision"])
        schedule = TaskBoard(revision, handler=lambda _context: None).schedule()
        evidence_view = build_task_board_evidence_view(revision).to_dict()
        candidate_final_result = self._taskboard_candidate_final_result(revision)
        final_refs = self._prioritize_taskboard_final_refs(
            self._taskboard_final_refs_from_evidence_view(evidence_view)
        )
        final_refs, staged_promotions = (
            await self._taskboard_stage_required_final_deliverable_refs(final_refs)
        )
        final_refs = await self._taskboard_refresh_current_required_final_refs(final_refs)
        final_refs = self._taskboard_terminal_candidate_refs(revision, final_refs)
        frame["taskboard_materialized_outputs"] = {
            "revision": revision,
            "schedule": schedule,
            "result_status": self._taskboard_terminal_status(revision, schedule),
            "evidence_view": evidence_view,
            "candidate_final_result": candidate_final_result,
            "final_refs": final_refs,
            "staged_promotions": staged_promotions,
            "trusted_terminal_refs": self._trusted_terminal_refs(final_refs),
        }
        return frame

    async def _taskboard_evidence_ingest_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        materialized = cast(
            dict[str, Any],
            frame["taskboard_materialized_outputs"],
        )
        evidence_view = cast(dict[str, Any], materialized["evidence_view"])
        evidence_ledger = self._stable_evidence_ledger_view(
            evidence_view,
            max_items=120,
            body_chars=2400,
            budget_selection="content_first",
        )
        materialized["evidence_ledger"] = evidence_ledger
        materialized["explicit_state_facts"] = task_board_explicit_state_facts(
            materialized["revision"],
            evidence_view=evidence_view,
        )
        frame["taskboard_materialized_outputs"] = materialized
        return frame

    async def _taskboard_terminal_verify_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        previous_acceptance_index = frame.get("taskboard_acceptance_index")
        transition_result = await self._finalize_taskboard(
            TaskBoardRevision.from_value(frame["taskboard_revision"]),
            context_pack=cast("TaskContextView", frame["context_pack"]),
            previous_acceptance_index=(
                previous_acceptance_index
                if isinstance(previous_acceptance_index, Mapping)
                else None
            ),
            prepared_outputs=cast(
                Mapping[str, Any],
                frame.get("taskboard_materialized_outputs") or {},
            ),
        )
        frame["taskboard_transition_result"] = transition_result
        if (
            transition_result.get("status") == "verification_retry"
            and isinstance(transition_result.get("prepared_final"), Mapping)
        ):
            materialized_outputs = dict(
                frame.get("taskboard_materialized_outputs") or {}
            )
            materialized_outputs["final_candidate"] = DataFormatter.sanitize(
                transition_result["prepared_final"]
            )
            frame["taskboard_materialized_outputs"] = materialized_outputs
        return frame

    async def _taskboard_transition_decide_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        result = frame.get("taskboard_transition_result")
        if not isinstance(result, Mapping):
            raise ValueError("TaskBoard terminal verification did not produce a transition result.")
        if result.get("terminal") is False and isinstance(result.get("revision"), Mapping):
            frame["next_frame_state"] = {
                "taskboard_revision": DataFormatter.sanitize(result["revision"]),
                "taskboard_tick_index": int(frame.get("taskboard_tick_index") or 0),
                **(
                    {"taskboard_acceptance_index": DataFormatter.sanitize(self._latest_taskboard_acceptance_index)}
                    if isinstance(self._latest_taskboard_acceptance_index, Mapping)
                    else {}
                ),
            }
        if self._terminal_taskboard_state is not None:
            self._terminal_taskboard_state["tick_index"] = int(frame.get("taskboard_tick_index") or 0)
        frame["iteration_result"] = dict(result)
        return frame

    async def _request_taskboard_plan(
        self,
        context_pack: "TaskContextView",
        *,
        context_package: ContextPackage | None = None,
    ):
        consumer_id = f"agent_task:{self.id}:taskboard-planner"
        reader = self._task_context_reader(
            phase="planning", consumer_id=consumer_id,
        )
        snapshot = reader.snapshot
        if (
            context_package is None
            or context_package.task_context_id != snapshot.context_id
            or context_package.consumer_id != consumer_id
            or context_package.phase != "planning"
            or context_package.context_revision != snapshot.revision
            or context_package.source_revisions != snapshot.source_revisions
        ):
            request_context_pack, context_package = await self._read_task_context_view(
                phase="planning",
                consumer_id=consumer_id,
                intent=f"Plan the TaskBoard: {self.goal}",
            )
        else:
            reader.ensure_required_delivery(context_package)
            request_context_pack = dict(self._context_pack_with_guidance(
                cast("TaskContextView", self._project_task_context_package(context_package))
            ))
        # Keep the orientation used by downstream cards aligned with the actual
        # planning input, including when sources changed after preparation.
        context_pack.update(request_context_pack)
        policy = resolve_task_board_planning_policy(
            self._taskboard_effort(),
            metadata={"execution_strategy": self.execution_strategy, "task_id": self.id},
        )
        language_policy = self._language_policy()
        request = self.agent.create_temp_request()
        self._bind_task_context_attachments(request, context_package)
        self._apply_language_policy_to_request(request, language_policy)
        previous_iterations = self._iteration_prompt_summaries()
        repair_context = self._planner_repair_context(previous_iterations)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "context_pack": DataFormatter.sanitize(request_context_pack),
                "execution_prompt": self._execution_prompt_context(),
                "planning_policy": policy.to_prompt_payload(),
                "taskboard_harness_policy": {
                    "acceptance_index": {
                        "schema_version": "task_board_acceptance_index/v1",
                        "authority": "projection_only",
                        "semantic_owner": "verifier",
                    },
                    "handoff_projection": {
                        "schema_version": "task_board_handoff_projection/v1",
                        "authority": "orientation_only",
                    },
                    "preflight": {
                        "allowed_only_with_mounted_capabilities": True,
                        "metadata_fields": [
                            "preflight_kind",
                            "requires_capability_ids",
                            "requires_task_workspace_refs",
                            "focus_item_ids",
                        ],
                    },
                },
                "retrieval_policy": self._task_context_retrieval_policy(),
                "planner_capabilities": self._planner_capabilities(),
                "capability_evidence_requirements": self._capability_evidence_requirements(),
                "repair_context": repair_context,
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Plan a card board for this submitted task. "
            "Do not discuss route selection. "
            "Use task_context_contract for prompt-safe temporal policy and ref-backed intermediate-resource handling. "
            "Concrete runtime current_time values may be omitted from the model hot path; do not infer or write a "
            "current date/time as a business fact unless it appears in task facts or source evidence. It is not a resource cap. "
            "Use the planning_policy as vocabulary guidance for orchestration complexity, evidence depth, "
            "reflection density, and repair tendency. Do not create hard budgets, fixed card counts, "
            "or action allowlists from the effort profile. "
            "When context_pack.skill_projection is present, its guidance and selected_resources are already "
            "TaskContext-disclosed Skill procedure. Apply it directly; do not treat it as business evidence or create readback "
            "cards or scoped_retrieval query groups for skills/... citations, and do not treat Skill citations "
            "as TaskWorkspace file paths or local registry paths. "
            "When repair_context is present, reuse prior evidence anchors and exact source/readback refs before planning new reads. "
            "Add only evidence required by the recorded acceptance delta; do not rediscover a repository or discard stronger prior facts. "
            "Plan card objectives and done_when conditions around user-visible outcomes, not around one "
            "specific provider, endpoint, file format, or auxiliary guidance source unless the user explicitly "
            "requires that exact source or artifact. Mark replaceable evidence attempts, optional guidance, "
            "style checks, and non-critical cross-checks as optional or degradable through failure_policy. "
            "Card ids are optional short hints only; keep them readable and do not spend tokens inventing opaque ids. "
            "Use allowed_execution_shape='control' for synthesis, verification, finalization, or board-continuation "
            "decision cards that should be handled by one structured model request. Use allowed_execution_shape='readback' "
            "for cards whose only job is bounded cold artifact readback. Use retrieval_policy and scoped_retrieval for "
            "TaskContext reads, selecting only exact source kinds offered by retrieval_policy.source_kinds. ContextReader "
            "performs those reads before the card carrier; they do not require a file Action. TaskWorkspace Actions cannot read "
            "a pinned repository, RecordStore, Skill, or any other Context source unless that content was actually materialized "
            "inside the bound TaskWorkspace. Use an action-capable shape such as 'actions' or 'auto' only for mounted external "
            "Actions, TaskWorkspace side effects, or genuinely mixed Action/readback work. "
            "For an actions card, include action_commands only when the exact offered Action ids and every required "
            "argument are already known now. Omit action_commands when any argument depends on a future card result; "
            "do not invent placeholders, copy opaque ids, or guess arguments. "
            "When a later Action needs a locator, selection, path, or value returned by an earlier Action, represent "
            "that value dependency with separate dependent cards when their outputs are useful board evidence. If the "
            "dependency is a tightly coupled bounded operation inside one card, omit action_commands so the card keeps "
            "its ActionLoop result-to-next-request backedge; never freeze search and selected-read calls as one parallel batch. "
            "Treat action_commands as the exhaustive command batch for that card: completion follows after that exact "
            "batch finishes. Never combine evidence-gathering action_commands with later synthesis or final TaskWorkspace "
            "delivery in the same card. Put synthesis and final_task_workspace_deliverables on a dependent control card so "
            "the complete dependency evidence is available to its structured request. "
            "Use capability_evidence_requirements as hard completion evidence contracts. When an exact Action success "
            "is required, ensure the board has a card whose execution can produce that Action event; prose or an "
            "ordinary TaskWorkspace readback does not satisfy it. "
            "When readiness checks are necessary, express them as optional preflight metadata on cards "
            "(preflight_kind, requires_capability_ids, requires_task_workspace_refs, focus_item_ids) and only for mounted "
            "capabilities or existing TaskWorkspace refs; do not require universal git, browser, shell, or init-script checks. "
            "When the submitted task explicitly requires an exact TaskWorkspace-relative final file path, put that exact "
            "path in final_task_workspace_deliverables on the one card that owns final materialization. Do not put working "
            "paths, inferred filenames, source refs, or intermediate artifacts in this field. "
            "After evidence fan-in, do not create a serial chain of control-only cards for synthesis, finalization, "
            "review, and next-step decision when one control card can return the deliverable, sufficient/gaps, "
            "next_board_action, diagnostics, and optional patch_proposal. Multiple dependent control cards are only "
            "justified for distinct user-visible artifacts, different upstream evidence sets, or materially separate "
            "decisions that cannot be verified in one request."
        )
        request.output(task_board_planning_output_schema(), format="json")
        result_handle = request.get_result()
        raw_plan = await self._await_task_request(
            result_handle.async_get_data(),
            stage="taskboard_plan",
        )
        self._record_task_context_consumption(
            context_package,
            request_id=result_handle.id,
        )
        await self._emit_required_skill_context_bound(
            request_context_pack,
            request_id=result_handle.id,
            phase="work.plan",
        )
        if not isinstance(raw_plan, Mapping):
            raise TypeError("TaskBoard planning request must return a mapping.")
        raw_plan = self._normalize_taskboard_initial_plan(raw_plan)
        host_diagnostics = self._taskboard_host_plan_diagnostics(raw_plan)
        if host_diagnostics:
            self.diagnostics.setdefault(
                "taskboard_initial_plan_normalization",
                [],
            ).extend(DataFormatter.sanitize(host_diagnostics))
            await self._emit(
                "agent_task.taskboard.plan.normalized",
                {"diagnostics": DataFormatter.sanitize(host_diagnostics)},
            )
        return coerce_task_board_planning_result(
            raw_plan,
            board_id=self.id,
            graph_id=f"{self.id}.taskboard",
            effort=self._taskboard_effort(),
            planning_policy=policy,
            metadata={"execution_strategy": self.execution_strategy},
        )

    @classmethod
    def _taskboard_scoped_retrieval_capacity_analysis(
        cls,
        value: Any,
    ) -> dict[str, Any]:
        normalized = cls._normalize_scoped_retrieval_plan(value)
        raw_groups = normalized.get("query_groups")
        if not isinstance(raw_groups, Sequence) or isinstance(
            raw_groups,
            str | bytes | bytearray,
        ):
            return {"status": "absent", "plan": normalized}
        groups = [dict(group) for group in raw_groups if isinstance(group, Mapping)]
        if not groups:
            return {"status": "absent", "plan": normalized}
        reserved: list[int] = []
        for group in groups:
            try:
                max_results = int(group.get("max_results", 8))
            except (TypeError, ValueError):
                return {
                    "status": "unpartitionable",
                    "plan": normalized,
                    "reason": "invalid_max_results",
                    "capacity": SCOPED_RETRIEVAL_RESULT_CAPACITY,
                    "reserved_results": 0,
                    "largest_group": 0,
                    "batches": [],
                }
            if max_results <= 0:
                return {
                    "status": "unpartitionable",
                    "plan": normalized,
                    "reason": "invalid_max_results",
                    "capacity": SCOPED_RETRIEVAL_RESULT_CAPACITY,
                    "reserved_results": 0,
                    "largest_group": max_results,
                    "batches": [],
                }
            reserved.append(max_results)
        total = sum(reserved)
        largest = max(reserved)
        if total <= SCOPED_RETRIEVAL_RESULT_CAPACITY:
            return {
                "status": "within_capacity",
                "plan": normalized,
                "capacity": SCOPED_RETRIEVAL_RESULT_CAPACITY,
                "reserved_results": total,
                "largest_group": largest,
                "batches": [normalized],
                "batch_reserved_results": [total],
            }
        if largest > SCOPED_RETRIEVAL_RESULT_CAPACITY:
            return {
                "status": "unpartitionable",
                "plan": normalized,
                "reason": "individual_group_exceeds_capacity",
                "capacity": SCOPED_RETRIEVAL_RESULT_CAPACITY,
                "reserved_results": total,
                "largest_group": largest,
                "batches": [],
            }

        batch_groups: list[list[dict[str, Any]]] = []
        batch_reserved_results: list[int] = []
        current_groups: list[dict[str, Any]] = []
        current_reserved = 0
        for group, group_reserved in zip(groups, reserved, strict=True):
            if current_groups and (
                current_reserved + group_reserved
                > SCOPED_RETRIEVAL_RESULT_CAPACITY
            ):
                batch_groups.append(current_groups)
                batch_reserved_results.append(current_reserved)
                current_groups = []
                current_reserved = 0
            current_groups.append(group)
            current_reserved += group_reserved
        if current_groups:
            batch_groups.append(current_groups)
            batch_reserved_results.append(current_reserved)

        shared = {
            key: DataFormatter.sanitize(item)
            for key, item in normalized.items()
            if key != "query_groups"
        }
        batches = [
            {
                **shared,
                "query_groups": DataFormatter.sanitize(batch),
            }
            for batch in batch_groups
        ]
        return {
            "status": "batched",
            "plan": normalized,
            "capacity": SCOPED_RETRIEVAL_RESULT_CAPACITY,
            "reserved_results": total,
            "largest_group": largest,
            "batches": batches,
            "batch_reserved_results": batch_reserved_results,
        }

    @classmethod
    def _taskboard_raw_card_scoped_retrieval(cls, card: Mapping[str, Any]) -> dict[str, Any]:
        for container in (
            card,
            card.get("metadata"),
            card.get("evidence_contract"),
        ):
            if not isinstance(container, Mapping):
                continue
            normalized = cls._normalize_scoped_retrieval_plan(
                container.get("scoped_retrieval")
            )
            if normalized:
                return normalized
        return {}

    @staticmethod
    def _taskboard_remove_raw_card_scoped_retrieval(card: dict[str, Any]) -> None:
        card.pop("scoped_retrieval", None)
        for container_key in ("metadata", "evidence_contract"):
            container = card.get(container_key)
            if not isinstance(container, Mapping):
                continue
            normalized = dict(container)
            normalized.pop("scoped_retrieval", None)
            card[container_key] = normalized

    def _normalize_taskboard_initial_plan(
        self,
        raw_plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Separate exact Action batches from dependency-aware final synthesis."""

        normalized = dict(raw_plan)
        raw_cards = raw_plan.get("cards")
        if not isinstance(raw_cards, Sequence) or isinstance(
            raw_cards,
            str | bytes | bytearray,
        ):
            return normalized

        used_ids = {
            str(card.get("id") or card.get("card_id") or "").strip()
            for card in raw_cards
            if isinstance(card, Mapping)
        }
        used_ids.discard("")
        split_final_ids: dict[str, str] = {}
        prepared: list[Any] = []
        normalization_diagnostics: list[dict[str, Any]] = []

        for index, raw_card in enumerate(raw_cards):
            if not isinstance(raw_card, Mapping):
                prepared.append(raw_card)
                continue
            card = dict(raw_card)
            final_paths = self._normalize_string_list(card.get("final_task_workspace_deliverables"))
            card_dependencies = self._normalize_string_list(card.get("depends_on"))
            commands_value = card.get("action_commands")
            commands = (
                [dict(command) for command in commands_value if isinstance(command, Mapping)]
                if isinstance(commands_value, Sequence)
                and not isinstance(commands_value, str | bytes | bytearray)
                else []
            )
            if commands_value not in (None, [], ()):
                required_action_ids = self._normalize_string_list(
                    card.get("requires_capability_ids")
                )
                attempted_action_ids = self._merge_string_lists(
                    [command.get("action_id") for command in commands]
                )
                if not final_paths and len(attempted_action_ids) > 1:
                    card.pop("action_commands", None)
                    commands = []
                    if attempted_action_ids:
                        card["requires_capability_ids"] = self._merge_string_lists(
                            [*required_action_ids, *attempted_action_ids]
                        )
                    normalization_diagnostics.append(
                        {
                            "code": "taskboard.initial_plan.action_commands_require_adaptive_loop",
                            "card_id": str(
                                card.get("id") or card.get("card_id") or f"card-{index + 1}"
                            ),
                            "message": (
                                "A multi-capability Action batch cannot preserve result-to-next-call value dependencies."
                            ),
                            "fallback": "action_loop",
                        }
                    )
                else:
                    _normalized_commands, validation_error = (
                        self._normalize_bounded_action_commands(
                            raw_commands=commands_value,
                            required_action_ids=([] if final_paths else required_action_ids),
                            unit_label="TaskBoard",
                        )
                    )
                    if validation_error is not None:
                        validation_code, message = validation_error
                        card.pop("action_commands", None)
                        commands = []
                        if attempted_action_ids:
                            card["requires_capability_ids"] = self._merge_string_lists(
                                [*required_action_ids, *attempted_action_ids]
                            )
                        normalization_diagnostics.append(
                            {
                                "code": "taskboard.initial_plan.action_commands_rejected",
                                "card_id": str(
                                    card.get("id") or card.get("card_id") or f"card-{index + 1}"
                                ),
                                "validation_code": validation_code,
                                "message": message,
                                "fallback": "taskboard_action_command_request",
                            }
                        )

            if commands and card_dependencies and not final_paths:
                required_action_ids = self._normalize_string_list(
                    card.get("requires_capability_ids")
                )
                attempted_action_ids = self._merge_string_lists(
                    [command.get("action_id") for command in commands]
                )
                card.pop("action_commands", None)
                commands = []
                if attempted_action_ids:
                    card["requires_capability_ids"] = self._merge_string_lists(
                        [*required_action_ids, *attempted_action_ids]
                    )
                normalization_diagnostics.append(
                    {
                        "code": "taskboard.initial_plan.dependent_action_commands_deferred",
                        "card_id": str(
                            card.get("id")
                            or card.get("card_id")
                            or f"card-{index + 1}"
                        ),
                        "depends_on": card_dependencies,
                        "deferred_action_ids": attempted_action_ids,
                        "fallback": "taskboard_action_command_request",
                    }
                )

            if not final_paths:
                prepared.append(card)
                continue

            if not commands:
                card["allowed_execution_shape"] = "control"
                card.pop("action_commands", None)
                prepared.append(card)
                continue

            original_dependencies = card_dependencies
            exact_final_write_commands: list[dict[str, Any]] = []
            for command in commands:
                action_id = str(command.get("action_id") or "").strip()
                action_input = command.get("action_input")
                if (
                    self._taskboard_task_workspace_write_action_ids([action_id])
                    and isinstance(action_input, Mapping)
                    and str(action_input.get("path") or "").strip() in final_paths
                    and isinstance(action_input.get("content"), str)
                ):
                    exact_final_write_commands.append(command)
            if exact_final_write_commands and not original_dependencies:
                prepared.append(card)
                continue
            if exact_final_write_commands:
                deferred_command_ids = {id(command) for command in exact_final_write_commands}
                commands = [
                    command for command in commands if id(command) not in deferred_command_ids
                ]
                deferred_action_ids = set(
                    self._normalize_string_list(
                        [command.get("action_id") for command in exact_final_write_commands]
                    )
                )
                remaining_required_action_ids = [
                    action_id
                    for action_id in self._normalize_string_list(
                        card.get("requires_capability_ids")
                    )
                    if action_id not in deferred_action_ids
                ]
                if remaining_required_action_ids:
                    card["requires_capability_ids"] = remaining_required_action_ids
                else:
                    card.pop("requires_capability_ids", None)
                normalization_diagnostics.append(
                    {
                        "code": "taskboard.initial_plan.dependent_final_write_deferred",
                        "card_id": str(
                            card.get("id")
                            or card.get("card_id")
                            or f"card-{index + 1}"
                        ),
                        "depends_on": original_dependencies,
                        "deferred_action_ids": sorted(deferred_action_ids),
                        "fallback": "dependency_aware_control_delivery",
                    }
                )
                if not commands:
                    card["allowed_execution_shape"] = "control"
                    card.pop("action_commands", None)
                    prepared.append(card)
                    continue
                card["action_commands"] = commands

            card_id = str(card.get("id") or card.get("card_id") or "").strip()
            if not card_id:
                card_id = f"card-{index + 1}"
                while card_id in used_ids:
                    card_id = f"{card_id}-action"
                card["id"] = card_id
                used_ids.add(card_id)
            final_id = f"{card_id}-finalize"
            suffix = 2
            while final_id in used_ids:
                final_id = f"{card_id}-finalize-{suffix}"
                suffix += 1
            used_ids.add(final_id)
            split_final_ids[card_id] = final_id

            source_card = dict(card)
            source_card["objective"] = (
                "Execute the declared Action command batch and preserve its results as dependency evidence."
            )
            source_card["action_block"] = source_card["objective"]
            source_card["done_when"] = (
                "Every declared action_commands entry has a recorded terminal result."
            )
            source_card["allowed_execution_shape"] = "actions"
            if original_dependencies:
                source_card.pop("action_commands", None)
                normalization_diagnostics.append(
                    {
                        "code": "taskboard.initial_plan.dependent_action_commands_deferred",
                        "card_id": card_id,
                        "depends_on": original_dependencies,
                        "deferred_action_ids": self._normalize_string_list(
                            [command.get("action_id") for command in commands]
                        ),
                        "fallback": "taskboard_action_command_request",
                    }
                )
            else:
                source_card["action_commands"] = commands
            source_card.pop("final_task_workspace_deliverables", None)
            command_action_ids = self._normalize_string_list(
                [command.get("action_id") for command in commands]
            )
            if command_action_ids:
                source_card["requires_capability_ids"] = command_action_ids

            final_card = dict(card)
            final_card["id"] = final_id
            final_card["objective"] = (
                "Synthesize the dependency evidence and materialize the declared final TaskWorkspace deliverable."
            )
            final_card["action_block"] = final_card["objective"]
            final_card["depends_on"] = self._merge_string_lists(
                [*original_dependencies, card_id]
            )
            final_card["allowed_execution_shape"] = "control"
            final_card.pop("action_commands", None)
            final_required_action_ids = [
                action_id
                for action_id in self._normalize_string_list(
                    card.get("requires_capability_ids")
                )
                if action_id not in set(command_action_ids)
            ]
            if final_required_action_ids:
                final_card["requires_capability_ids"] = final_required_action_ids
            else:
                final_card.pop("requires_capability_ids", None)
            prepared.extend([source_card, final_card])

        if split_final_ids:
            for card in prepared:
                if not isinstance(card, dict):
                    continue
                card_id = str(card.get("id") or card.get("card_id") or "").strip()
                own_source_id = next(
                    (
                        source_id
                        for source_id, final_id in split_final_ids.items()
                        if final_id == card_id
                    ),
                    None,
                )
                dependencies = self._normalize_string_list(card.get("depends_on"))
                if dependencies:
                    card["depends_on"] = [
                        (
                            dependency_id
                            if dependency_id == own_source_id
                            else split_final_ids.get(dependency_id, dependency_id)
                        )
                        for dependency_id in dependencies
                    ]

        capacity_prepared: list[Any] = []
        for index, raw_card in enumerate(prepared):
            if not isinstance(raw_card, dict):
                capacity_prepared.append(raw_card)
                continue
            scoped_retrieval = self._taskboard_raw_card_scoped_retrieval(raw_card)
            capacity = self._taskboard_scoped_retrieval_capacity_analysis(
                scoped_retrieval
            )
            if capacity.get("status") == "unpartitionable":
                normalization_diagnostics.append(
                    {
                        "code": (
                            "taskboard.scoped_retrieval.capacity_overflow_unpartitionable"
                        ),
                        "card_id": str(
                            raw_card.get("id")
                            or raw_card.get("card_id")
                            or f"card-{index + 1}"
                        ),
                        "capacity": capacity.get("capacity"),
                        "reserved_results": capacity.get("reserved_results"),
                        "largest_group": capacity.get("largest_group"),
                        "reason": capacity.get("reason"),
                        "fallback": "structured_model_replan",
                    }
                )
                capacity_prepared.append(raw_card)
                continue
            if capacity.get("status") != "batched":
                capacity_prepared.append(raw_card)
                continue

            card = dict(raw_card)
            card_id = str(card.get("id") or card.get("card_id") or "").strip()
            if not card_id:
                card_id = f"card-{index + 1}"
                while card_id in used_ids:
                    card_id = f"{card_id}-scoped"
                card["id"] = card_id
                used_ids.add(card_id)
            original_dependencies = self._normalize_string_list(card.get("depends_on"))
            batch_cards: list[dict[str, Any]] = []
            batch_ids: list[str] = []
            batches = capacity.get("batches")
            batch_plans = (
                [dict(batch) for batch in batches if isinstance(batch, Mapping)]
                if isinstance(batches, Sequence)
                and not isinstance(batches, str | bytes | bytearray)
                else []
            )
            for batch_index, batch_plan in enumerate(batch_plans, start=1):
                base_batch_id = f"{card_id}.retrieval-{batch_index}"
                batch_id = base_batch_id
                suffix = 2
                while batch_id in used_ids:
                    batch_id = f"{base_batch_id}-{suffix}"
                    suffix += 1
                used_ids.add(batch_id)
                batch_ids.append(batch_id)
                batch_card = dict(card)
                batch_card["id"] = batch_id
                batch_card.pop("card_id", None)
                batch_card["objective"] = (
                    f"Run bounded scoped retrieval batch {batch_index}/{len(batch_plans)} "
                    f"for card {card_id}; preserve the declared Context source ownership."
                )
                batch_card["action_block"] = batch_card["objective"]
                batch_card["done_when"] = (
                    "The bounded ContextReader batch returned host-identified evidence or "
                    "structured source diagnostics."
                )
                batch_card["required_outputs"] = [
                    "Host-identified bounded scoped retrieval evidence or structured diagnostics."
                ]
                batch_card["depends_on"] = original_dependencies
                batch_card["allowed_execution_shape"] = "control"
                batch_card.pop("action_commands", None)
                batch_card.pop("requires_capability_ids", None)
                batch_card.pop("final_task_workspace_deliverables", None)
                self._taskboard_remove_raw_card_scoped_retrieval(batch_card)
                batch_card["scoped_retrieval"] = DataFormatter.sanitize(batch_plan)
                batch_metadata = dict(batch_card.get("metadata") or {})
                convergence_subject = str(
                    batch_metadata.get("terminal_convergence_subject") or ""
                ).strip() or f"taskboard_card:{card_id}"
                batch_metadata.update(
                    {
                        "generated_by": (
                            "agent_task.taskboard.scoped_retrieval_capacity_batch"
                        ),
                        "source_card_id": card_id,
                        "retrieval_batch_index": batch_index,
                        "retrieval_batch_count": len(batch_plans),
                        "terminal_convergence_subject": convergence_subject,
                    }
                )
                batch_card["metadata"] = batch_metadata
                batch_cards.append(batch_card)

            continuation = dict(card)
            self._taskboard_remove_raw_card_scoped_retrieval(continuation)
            continuation["depends_on"] = self._merge_string_lists(
                [*original_dependencies, *batch_ids]
            )
            continuation.pop("action_commands", None)
            if not self._normalize_string_list(
                continuation.get("requires_capability_ids")
            ):
                continuation["allowed_execution_shape"] = "control"
            continuation_metadata = dict(continuation.get("metadata") or {})
            continuation_metadata.update(
                {
                    "generated_by": (
                        "agent_task.taskboard.scoped_retrieval_capacity_continuation"
                    ),
                    "retrieval_batch_card_ids": batch_ids,
                }
            )
            continuation["metadata"] = continuation_metadata
            capacity_prepared.extend([*batch_cards, continuation])
            normalization_diagnostics.append(
                {
                    "code": "taskboard.scoped_retrieval.capacity_overflow_batched",
                    "card_id": card_id,
                    "capacity": capacity.get("capacity"),
                    "reserved_results": capacity.get("reserved_results"),
                    "largest_group": capacity.get("largest_group"),
                    "batch_card_ids": batch_ids,
                    "batch_reserved_results": capacity.get(
                        "batch_reserved_results"
                    ),
                    "source_owner_preserved": True,
                }
            )
        prepared = capacity_prepared

        normalized["cards"] = prepared
        if split_final_ids:
            normalization_diagnostics.append(
                {
                    "code": "taskboard.initial_plan.final_delivery_split",
                    "split_cards": dict(split_final_ids),
                }
            )
        if normalization_diagnostics:
            for diagnostic in normalization_diagnostics:
                diagnostic["owner"] = "host"
            diagnostics = normalized.get("diagnostics")
            normalized_diagnostics = (
                [dict(item) for item in diagnostics if isinstance(item, Mapping)]
                if isinstance(diagnostics, Sequence)
                and not isinstance(diagnostics, str | bytes | bytearray)
                else []
            )
            normalized_diagnostics.extend(normalization_diagnostics)
            normalized["diagnostics"] = normalized_diagnostics
        return normalized

    @staticmethod
    def _taskboard_host_plan_diagnostics(
        plan: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        raw_diagnostics = plan.get("diagnostics")
        if not isinstance(raw_diagnostics, Sequence) or isinstance(
            raw_diagnostics,
            str | bytes | bytearray,
        ):
            return []
        return [
            dict(item)
            for item in raw_diagnostics
            if isinstance(item, Mapping)
            and str(item.get("owner") or "") == "host"
            and str(item.get("code") or "").startswith("taskboard.")
        ]

    def _initial_taskboard_plan_from_shape_analysis(self):
        if self.execution_strategy != "auto":
            return None
        raw_plan = (
            self.task_shape_analysis.get("initial_taskboard_plan")
            if isinstance(self.task_shape_analysis, Mapping)
            else None
        )
        if not isinstance(raw_plan, Mapping) or not raw_plan:
            return None
        raw_plan = self._normalize_taskboard_initial_plan(raw_plan)
        host_diagnostics = self._taskboard_host_plan_diagnostics(raw_plan)
        if host_diagnostics:
            self.diagnostics.setdefault(
                "taskboard_initial_plan_normalization",
                [],
            ).extend(DataFormatter.sanitize(host_diagnostics))
        policy = resolve_task_board_planning_policy(
            self._taskboard_effort(),
            metadata={
                "execution_strategy": self.execution_strategy,
                "task_id": self.id,
                "source": "task_shape_analysis",
            },
        )
        try:
            planning_result = coerce_task_board_planning_result(
                raw_plan,
                board_id=self.id,
                graph_id=f"{self.id}.taskboard",
                effort=self._taskboard_effort(),
                planning_policy=policy,
                metadata={"execution_strategy": self.execution_strategy, "source": "task_shape_analysis"},
            )
        except Exception as error:
            self.diagnostics.setdefault("taskboard_initial_plan", []).append(
                {
                    "source": "task_shape_analysis",
                    "accepted": False,
                    "error": {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    },
                }
            )
            return None
        self.diagnostics.setdefault("taskboard_initial_plan", []).append(
            {
                "source": "task_shape_analysis",
                "accepted": True,
                "card_count": len(planning_result.revision.graph.cards),
            }
        )
        return planning_result

    def _taskboard_should_fallback_to_flat(self, revision: Any) -> bool:
        if self.execution_strategy != "auto":
            return False
        cards = list(getattr(getattr(revision, "graph", None), "cards", ()) or ())
        if not cards or len(cards) > 2:
            return False
        if any(str(getattr(card, "failure_policy", "required") or "required") != "required" for card in cards):
            return False
        if any(self._taskboard_card_has_complex_contract(card) for card in cards):
            return False
        shapes = {str(getattr(card, "allowed_execution_shape", "auto") or "auto").strip().lower() for card in cards}
        if shapes.intersection({"readback", "artifact_readback", "control", "model_control", "synthesis", "finalize"}):
            return False
        dependency_edges = sum(len(getattr(card, "depends_on", ()) or ()) for card in cards)
        if len(cards) == 1:
            return True
        if dependency_edges != 1:
            return False
        depended_on = {str(dep) for card in cards for dep in (getattr(card, "depends_on", ()) or ())}
        card_ids = {str(getattr(card, "id", "")) for card in cards}
        return depended_on.issubset(card_ids)

    @staticmethod
    def _taskboard_card_has_complex_contract(card: Any) -> bool:
        if getattr(card, "policy_scope_refs", ()):
            return True
        if getattr(card, "input_refs", ()):
            return True
        contract = getattr(card, "evidence_contract", {})
        if not isinstance(contract, Mapping) or not contract:
            return False
        if contract.get("scoped_retrieval"):
            return True
        if contract.get("evidence_to_use"):
            return True
        informational_keys = {"action_block", "done_when", "failure_policy", "evidence_to_use"}
        return any(key not in informational_keys and value not in (None, "", [], {}) for key, value in contract.items())

    @staticmethod
    def _taskboard_revision_completed(revision: Any) -> bool:
        cards = list(revision.graph.cards)
        if not cards:
            return False
        for card in cards:
            result = revision.card_results.get(card.id)
            status = str(result.status if result is not None else card.status).strip().lower()
            if task_board_card_required(card):
                if status != "completed":
                    return False
            elif status not in {"completed", "failed", "setback", "blocked", "skipped"}:
                return False
        return True

    @staticmethod
    def _taskboard_can_attempt_degraded_final(revision: Any, schedule: Any) -> bool:
        if getattr(schedule, "runnable_card_ids", ()):
            return False
        return any(str(result.status).strip().lower() == "completed" for result in revision.card_results.values())

    @classmethod
    def _taskboard_terminal_status(cls, revision: Any, schedule: Any) -> str:
        card_by_id = revision.graph.card_by_id()
        required_statuses = {
            card_id: str(result.status)
            for card_id, result in revision.card_results.items()
            if task_board_card_required(card_by_id[card_id])
        }
        if "failed" in required_statuses.values():
            return "failed"
        if (
            "blocked" in required_statuses.values()
            or "setback" in required_statuses.values()
            or schedule.blocked_card_ids
        ):
            return "blocked"
        if cls._taskboard_revision_completed(revision):
            return "completed"
        return "running"


__all__ = ["AgentTaskTaskBoardStrategyMixin"]
