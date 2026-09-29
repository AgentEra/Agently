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

from typing import Literal

from .TaskShared import (
    AgentExecutionStreamData,
    AgentTaskMixinBase,
    Any,
    apply_language_policy_to_prompt,
    asyncio,
    Awaitable,
    cast,
    collect_evidence_use,
    DataFormatter,
    language_policy_from_prompt_snapshot,
    Mapping,
    RecordRef,
    resolve_language_policy,
    Sequence,
    TaskContextView,
    validate_evidence_use,
    value_with_normalized_evidence_use,
    _AgentTaskDeadlineExceeded,
    _compact_agent_task_error_message,
    _DEGRADED_DAG_STEP_EXECUTION_SHAPES,
    _STEP_EXECUTION_SHAPES,
)


class AgentTaskFlatStrategyMixin(AgentTaskMixinBase):
    async def _run_iteration(self, iteration_index: int) -> dict[str, Any]:
        """Compatibility entry point for one iteration outside the lifecycle flow."""

        frame: dict[str, Any] = {"iteration": iteration_index}
        for stage in (
            self._flat_context_prepare_stage,
            self._flat_work_plan_stage,
            self._flat_work_execute_stage,
            self._flat_outputs_materialize_stage,
            self._flat_evidence_ingest_stage,
            self._flat_terminal_verify_stage,
            self._flat_transition_decide_stage,
        ):
            frame = await stage(frame)
            if frame.get("iteration_result") is not None:
                break
        result = frame.get("iteration_result")
        if not isinstance(result, Mapping):
            raise ValueError("Flat lifecycle did not produce a structured iteration result.")
        return dict(result)

    async def _flat_context_prepare_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        iteration_index = int(frame["iteration"])
        try:
            if self._task_deadline_exceeded():
                frame["iteration_result"] = await self._terminate_timed_out(
                    iteration_index,
                    stage="plan",
                )
                return frame
            await self._emit_progress(
                iteration_index,
                "context",
                f"Iteration {iteration_index}: building a TaskContext package for the task goal.",
            )
            await self._emit(f"agent_task.iteration.{iteration_index}.started", {"iteration": iteration_index})
            await self._apply_guidance_boundary(iteration_index=iteration_index, boundary="flat_context")
            context_pack = await self._await_task_deadline(
                self._build_context(),
                stage="context",
            )
            await self._emit(f"agent_task.iteration.{iteration_index}.context", context_pack)
            required_skill_blocker = self._required_skill_context_blocker(context_pack)
            if required_skill_blocker is not None:
                frame["context_pack"] = context_pack
                frame["iteration_result"] = await self._terminate_required_skill_context_blocked(
                    iteration_index,
                    required_skill_blocker,
                )
                return frame
            await self._emit_snapshot(
                iteration_index,
                "context",
                {
                    "context_item_count": len(context_pack.get("items", [])),
                    "diagnostics": context_pack.get("diagnostics", {}),
                },
                message=(
                    f"Iteration {iteration_index}: context pack ready with "
                    f"{len(context_pack.get('items', []))} item(s)."
                ),
            )

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
        return frame

    async def _flat_work_plan_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        context_pack = cast("TaskContextView", frame["context_pack"])
        try:
            await self._emit_progress(
                iteration_index,
                "plan",
                f"Iteration {iteration_index}: asking the model to plan one bounded execution step.",
            )
            plan = self._normalize_step_plan(
                await self._await_task_deadline(
                    self._request_plan(iteration_index, context_pack),
                    stage="plan",
                )
            )
        except _AgentTaskDeadlineExceeded as error:
            frame["iteration_result"] = await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
            return frame
        await self._emit_process_progress_from_output(plan, stage="plan", iteration=iteration_index)
        await self._record_phase(
            "planned",
            iteration=iteration_index,
            diagnostics={
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get(
                    "effective_execution_shape", plan.get("execution_shape", "direct")
                ),
                "step_instruction": plan.get("step_instruction", ""),
                "expected_evidence": plan.get("expected_evidence", ""),
                "rationale": plan.get("rationale", ""),
            },
        )
        await self._emit(f"agent_task.iteration.{iteration_index}.plan", plan)
        await self._emit_snapshot(
            iteration_index,
            "plan",
            {
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get(
                    "effective_execution_shape", plan.get("execution_shape", "direct")
                ),
                "step_instruction": plan.get("step_instruction", ""),
                "expected_evidence": plan.get("expected_evidence", ""),
                "rationale": plan.get("rationale", ""),
            },
            message=f"Iteration {iteration_index}: plan ready; next bounded step is selected.",
        )
        decision_ref = await self._record_decision(iteration_index, plan, context_pack)
        await self._emit(f"agent_task.iteration.{iteration_index}.decision", {"record": decision_ref})
        frame["plan"] = plan
        frame["decision_ref"] = decision_ref
        return frame

    async def _flat_work_execute_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        plan = cast(dict[str, Any], frame["plan"])
        context_pack = cast("TaskContextView", frame["context_pack"])
        await self._emit_progress(
            iteration_index,
            "execute",
            f"Iteration {iteration_index}: executing the bounded step and collecting evidence.",
        )
        await self._record_phase(
            "executing",
            iteration=iteration_index,
            diagnostics={
                "execution_shape": plan.get("execution_shape", "direct"),
                "effective_execution_shape": plan.get(
                    "effective_execution_shape", plan.get("execution_shape", "direct")
                ),
                "step_instruction": plan.get("step_instruction", ""),
            },
        )
        try:
            execution_result, execution_meta = await self._await_task_deadline(
                self._execute_step(iteration_index, plan, context_pack),
                stage="execute",
            )
        except _AgentTaskDeadlineExceeded as error:
            frame["iteration_result"] = await self._terminate_timed_out(
                iteration_index,
                stage=error.stage,
                reason=error.reason,
                limit_name=error.limit_name,
                timeout_seconds=error.timeout_seconds,
            )
            return frame
        execution_failed = str(execution_meta.get("status") or "").strip().lower() in {
            "failed",
            "error",
            "timed_out",
            "blocked",
        }
        grounding_patch_mode = self._flat_execution_is_grounding_task_workspace_patch(
            execution_meta
        )
        if execution_failed:
            self._record_failed_execution_shape(plan, execution_meta)
            if not grounding_patch_mode:
                execution_result, execution_meta = await self._maybe_run_acp_recovery(
                    iteration_index,
                    plan=plan,
                    execution_result=execution_result,
                    execution_meta=execution_meta,
                )
                execution_failed = str(execution_meta.get("status") or "").strip().lower() in {
                    "failed",
                    "error",
                    "timed_out",
                    "blocked",
                }
        frame["execution_result"] = execution_result
        frame["execution_meta"] = execution_meta
        frame["execution_failed"] = execution_failed
        frame["grounding_patch_mode"] = grounding_patch_mode
        return frame

    async def _flat_outputs_materialize_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        plan = cast(dict[str, Any], frame["plan"])
        context_pack = cast("TaskContextView", frame["context_pack"])
        execution_result = frame["execution_result"]
        execution_meta = cast(dict[str, Any], frame["execution_meta"])
        execution_failed = bool(frame["execution_failed"])
        grounding_patch_mode = bool(frame["grounding_patch_mode"])
        if not grounding_patch_mode:
            execution_result = await self._deliver_task_workspace_artifact(
                execution_result,
                plan=plan,
                execution_meta=execution_meta,
                source=f"agent_task.iteration.{iteration_index}.task_workspace_artifact",
                context_pack=context_pack,
                iteration_index=iteration_index,
                repair_context=self._active_repair_context(),
                allow_stream_draft=not execution_failed,
            )
        frame["execution_result"] = execution_result
        return frame

    async def _flat_evidence_ingest_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        plan = cast(dict[str, Any], frame["plan"])
        context_pack = cast("TaskContextView", frame["context_pack"])
        decision_ref = frame["decision_ref"]
        execution_result = frame["execution_result"]
        execution_meta = cast(dict[str, Any], frame["execution_meta"])
        execution_failed = bool(frame["execution_failed"])
        cumulative_evidence_ledger = self._cumulative_evidence_ledger(execution_meta)
        flat_evidence_guard = validate_evidence_use(
            collect_evidence_use(execution_result),
            cumulative_evidence_ledger,
        )
        flat_evidence_repair_diagnostic: dict[str, Any] | None = None
        if isinstance(execution_result, Mapping):
            # Apply the same deterministic anchor-aware binding repair the TaskBoard
            # card and verifier paths use, so a flat step that referenced evidence by
            # a human-readable locator label gets canonicalized here instead of being
            # deferred to verify-time repair only.
            original_blocking = self._taskboard_evidence_guard_blocking_count(flat_evidence_guard)
            if original_blocking > 0:
                repaired_evidence_use = self._deterministic_evidence_binding_repair(
                    flat_evidence_guard, cumulative_evidence_ledger
                )
                if repaired_evidence_use:
                    repaired_result = value_with_normalized_evidence_use(execution_result, repaired_evidence_use)
                    repaired_guard = validate_evidence_use(
                        collect_evidence_use(repaired_result), cumulative_evidence_ledger
                    )
                    repaired_blocking = self._taskboard_evidence_guard_blocking_count(repaired_guard)
                    if repaired_blocking < original_blocking:
                        execution_result = repaired_result
                        flat_evidence_guard = repaired_guard
                        flat_evidence_repair_diagnostic = {
                            "code": "agent_task.flat.evidence_binding_repair",
                            "status": "completed" if repaired_blocking == 0 else "partial",
                            "original_blocking_count": original_blocking,
                            "repaired_blocking_count": repaired_blocking,
                            "repaired_claim_count": len(repaired_evidence_use),
                        }
            execution_result = value_with_normalized_evidence_use(
                execution_result,
                flat_evidence_guard.get("normalized_evidence_use"),
            )
        execution_meta.setdefault("diagnostics", {})
        if isinstance(execution_meta.get("diagnostics"), dict):
            execution_meta["diagnostics"]["evidence_use_guard"] = DataFormatter.sanitize(flat_evidence_guard)
            if flat_evidence_repair_diagnostic is not None:
                execution_meta["diagnostics"]["evidence_binding_repair"] = DataFormatter.sanitize(
                    flat_evidence_repair_diagnostic
                )
        execution_meta = self._flat_execution_meta_with_skill_context_consumptions(
            execution_meta,
            context_pack=context_pack,
        )
        await self._emit_process_progress_from_output(
            execution_result,
            stage="execution",
            iteration=iteration_index,
        )
        await self._emit_snapshot(
            iteration_index,
            "execution",
            {
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_id": execution_meta.get("execution_id"),
                "route": execution_meta.get("route"),
                "logs": self._execution_log_summary(execution_meta),
            },
            message=(
                f"Iteration {iteration_index}: bounded step failed; failure evidence was captured."
                if execution_failed
                else f"Iteration {iteration_index}: bounded step finished; execution evidence was captured."
            ),
        )
        observation_ref, checkpoint_ref = await self._record_observation(
            iteration_index,
            plan=plan,
            decision_ref=decision_ref,
            execution_result=execution_result,
            execution_meta=execution_meta,
        )
        step_reflection_ref = None
        if self._should_record_process_reflection("bounded_step", plan=plan):
            step_reflection_ref = await self._record_reflection(
                iteration_index,
                phase="bounded_step",
                subject_ref=observation_ref,
                summary=self._bounded_step_reflection_summary(
                    plan=plan,
                    execution_meta=execution_meta,
                    execution_failed=execution_failed,
                ),
            )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.observation",
            {"record": observation_ref, "checkpoint": checkpoint_ref},
        )
        await self._record_phase(
            "evidence_recorded",
            iteration=iteration_index,
            diagnostics={
                "observation_ref": observation_ref,
                "checkpoint_ref": checkpoint_ref,
                "execution_id": execution_meta.get("execution_id"),
                "route": execution_meta.get("route"),
            },
        )
        frame["execution_result"] = execution_result
        frame["execution_meta"] = execution_meta
        frame["observation_ref"] = observation_ref
        frame["checkpoint_ref"] = checkpoint_ref
        frame["step_reflection_ref"] = step_reflection_ref
        frame["grounding_guard"] = flat_evidence_guard
        return frame

    async def _flat_terminal_verify_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        plan = cast(dict[str, Any], frame["plan"])
        context_pack = cast("TaskContextView", frame["context_pack"])
        decision_ref = frame["decision_ref"]
        execution_result = frame["execution_result"]
        execution_meta = cast(dict[str, Any], frame["execution_meta"])
        observation_ref = frame["observation_ref"]
        step_reflection_ref = frame.get("step_reflection_ref")
        should_verify, verification_decision = self._should_request_flat_final_verification(
            execution_result,
            execution_meta,
        )
        terminal_transition: dict[str, Any] | None = None
        if should_verify:
            await self._emit_progress(
                iteration_index,
                "verify",
                f"Iteration {iteration_index}: verifying the evidence against every success criterion.",
            )
            try:
                terminal_transition = await self._await_task_deadline(
                    self._run_terminal_verification(
                        iteration_index,
                        plan=plan,
                        execution_result=execution_result,
                        execution_meta=execution_meta,
                        context_pack=context_pack,
                        preferred_final_result=(
                            self._candidate_final_result_from_execution_result(
                                execution_result,
                                include_answer=False,
                            )
                            if str(plan.get("deliverable_mode") or "")
                            == "inline_final"
                            else None
                        ),
                    ),
                    stage="verify",
                )
                assert terminal_transition is not None
                verification = cast(
                    dict[str, Any],
                    terminal_transition["verification"],
                )
            except _AgentTaskDeadlineExceeded as error:
                await self._record_timed_out_verification_iteration(
                    iteration_index,
                    plan=plan,
                    context_pack=context_pack,
                    decision_ref=decision_ref,
                    execution_meta=execution_meta,
                    observation_ref=observation_ref,
                    step_reflection_ref=step_reflection_ref,
                    error=error,
                )
                frame["iteration_result"] = await self._terminate_timed_out(
                    iteration_index,
                    stage=error.stage,
                    reason=error.reason,
                    limit_name=error.limit_name,
                    timeout_seconds=error.timeout_seconds,
                )
                return frame
            verification_source = "independent_verifier"
        else:
            verification = self._flat_consumer_continuation_verification(
                execution_result,
                execution_meta,
                decision=verification_decision,
                grounding_guard=frame.get("grounding_guard"),
            )
            verification_source = "consumer_driven_continuation"
            await self._emit_progress(
                iteration_index,
                "continue",
                f"Iteration {iteration_index}: bounded step evidence is available for the next iteration.",
            )
        await self._record_phase(
            "verified",
            iteration=iteration_index,
            diagnostics={
                "verification_source": verification_source,
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
                "missing_criteria": verification.get("missing_criteria", []),
                "final_result_present": bool(str(verification.get("final_result") or "").strip()),
            },
        )
        await self._record_phase(
            "guarded",
            iteration=iteration_index,
            diagnostics={
                "verification_source": verification_source,
                "guard_reasons": verification.get("guard_reasons", []),
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
            },
        )
        verification_ref = await self._record_verification(iteration_index, verification, observation_ref)
        verification_reflection_ref = None
        if self._should_record_process_reflection("major_node", plan=plan):
            verification_reflection_ref = await self._record_reflection(
                iteration_index,
                phase="major_node",
                subject_ref=verification_ref,
                summary=self._major_node_reflection_summary(verification=verification),
            )
        await self._emit_snapshot(
            iteration_index,
            "verification",
            {
                "is_complete": verification.get("is_complete"),
                "requires_block": verification.get("requires_block"),
                "verification_source": verification_source,
                "reason": verification.get("reason"),
                "missing_criteria": verification.get("missing_criteria", []),
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
                "replan_instruction": verification.get("replan_instruction", ""),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
            },
            message=(
                f"Iteration {iteration_index}: verification "
                f"{'passed' if verification.get('is_complete') else 'requires another step'}."
            ),
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.verification",
            {"verification": verification, "record": verification_ref},
        )

        iteration_record = {
            "iteration": iteration_index,
            "plan": plan,
            "decision_ref": decision_ref,
            "execution_meta": execution_meta,
            "observation_ref": observation_ref,
            "verification": verification,
            "verification_ref": verification_ref,
            "verification_source": verification_source,
            "reflection_refs": [ref for ref in (step_reflection_ref, verification_reflection_ref) if ref is not None],
            "context_item_count": len(context_pack.get("items", [])),
            "process_summary": self._combined_process_summary(
                plan=plan,
                execution_result=execution_result,
                verification=verification,
            ),
        }
        self.iterations.append(DataFormatter.sanitize(iteration_record))
        # The cumulative satisfied-capability sets are updated inside
        # _normalize_verification; persist a resumable snapshot for this
        # iteration so a crashed task can continue from the next iteration.
        await self._write_resume_snapshot(iteration_index, verification)
        frame["verification"] = verification
        frame["verification_source"] = verification_source
        frame["verification_ref"] = verification_ref
        frame["verification_reflection_ref"] = verification_reflection_ref
        frame["terminal_transition"] = terminal_transition
        return frame

    async def _flat_transition_decide_stage(
        self,
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        if frame.get("iteration_result") is not None:
            return frame
        iteration_index = int(frame["iteration"])
        verification = cast(dict[str, Any], frame["verification"])
        verification_source = str(frame["verification_source"])
        execution_result = frame["execution_result"]
        terminal_transition = cast(
            dict[str, Any] | None,
            frame.get("terminal_transition"),
        )
        if bool(verification.get("is_complete")):
            self.status = "completed"
            terminal_result = cast(
                Mapping[str, Any],
                terminal_transition.get("terminal_result", {})
                if terminal_transition is not None
                else {},
            )
            final_result = terminal_result.get("final_result")
            terminal_refs = list(terminal_result.get("terminal_refs") or [])
            terminal_file_refs = list(terminal_result.get("final_file_refs") or [])
            promoted_refs = await self._register_terminal_deliverables(terminal_refs)
            self.result = {
                "status": "completed",
                "accepted": True,
                "artifact_status": "accepted",
                "task_id": self.id,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "final_result": final_result,
                "final_response": self._agent_task_user_final_response(
                    final=verification,
                    accepted=True,
                    artifact_status="accepted",
                    status="completed",
                    reason=str(verification.get("reason") or ""),
                    missing_criteria=verification.get("missing_criteria", []),
                    final_refs=terminal_file_refs,
                    final_result=final_result,
                ),
                "artifact_refs": promoted_refs,
                "reason": verification.get("reason", ""),
                "missing_criteria": verification.get("missing_criteria", []),
            }
            await self._emit_progress(
                iteration_index,
                "completed",
                f"Iteration {iteration_index}: all success criteria are satisfied; the task is complete.",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": True, "artifact_status": "accepted"},
            )
            await self._emit("agent_task.completed", self.result)
            frame["iteration_result"] = {"terminal": True, "status": self.status}
            return frame

        if verification.get("requires_block"):
            self.status = "blocked"
            reason = verification.get("reason") or "Verifier blocked the task."
            convergence = verification.get("terminal_convergence")
            stopped_after_repeated_issue = isinstance(convergence, Mapping) and bool(
                convergence.get("stopped_after_third_occurrence")
            )
            artifact_status = "partial" if stopped_after_repeated_issue else "blocked"
            terminal_result = cast(
                Mapping[str, Any],
                terminal_transition.get("terminal_result", {})
                if terminal_transition is not None
                else {},
            )
            blocked_final_result = terminal_result.get("final_result", "")
            blocked_terminal_refs = list(terminal_result.get("terminal_refs") or [])
            blocked_final_refs = list(terminal_result.get("final_file_refs") or [])
            promoted_refs = await self._register_terminal_deliverables(blocked_terminal_refs)
            self.result = {
                "status": "blocked",
                "accepted": False,
                "artifact_status": artifact_status,
                "task_id": self.id,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "reason": reason,
                "final_response": self._agent_task_user_final_response(
                    final=verification,
                    accepted=False,
                    artifact_status=artifact_status,
                    status="blocked",
                    reason=str(reason),
                    missing_criteria=verification.get("missing_criteria", []),
                    final_refs=blocked_final_refs,
                    final_result=blocked_final_result,
                ),
                "artifact_refs": promoted_refs,
                "missing_criteria": verification.get("missing_criteria", []),
            }
            if blocked_final_result not in (None, ""):
                self.result["final_result"] = blocked_final_result
            await self._emit_progress(
                iteration_index,
                "blocked",
                f"Iteration {iteration_index}: verifier blocked the task because it cannot continue safely.",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": False, "artifact_status": artifact_status},
            )
            await self._emit("agent_task.blocked", self.result)
            frame["iteration_result"] = {"terminal": True, "status": self.status}
            return frame

        if self.max_iterations is not None and iteration_index >= self.max_iterations:
            missing_capabilities = self._normalize_string_list(verification.get("missing_required_capabilities"))
            if missing_capabilities:
                self.status = "capability_unavailable"
                reason = (
                    "Task could not satisfy required capabilities before max_iterations: "
                    f"{', '.join(missing_capabilities)}."
                )
            else:
                self.status = "max_iterations"
                reason = verification.get("reason") or "Task did not pass verification before max_iterations."
            terminal_result = cast(
                Mapping[str, Any],
                terminal_transition.get("terminal_result", {})
                if terminal_transition is not None
                else {},
            )
            partial_final_result = terminal_result.get("final_result") or verification.get("final_result") or ""
            partial_terminal_refs = list(terminal_result.get("terminal_refs") or [])
            partial_final_refs = list(terminal_result.get("final_file_refs") or [])
            if terminal_transition is None:
                partial_terminal_refs = self._trusted_terminal_refs(
                    execution_result,
                    verification,
                )
                partial_final_refs = self._trusted_terminal_file_refs(
                    partial_terminal_refs
                )
            promoted_refs = await self._register_terminal_deliverables(partial_terminal_refs)
            if terminal_transition is None:
                partial_final_result = self._compact_terminal_final_result(
                    partial_final_result,
                    trusted_file_refs=partial_final_refs,
                )
            self.result = {
                "status": self.status,
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "reason": reason,
                "final_response": self._agent_task_user_final_response(
                    final=verification,
                    accepted=False,
                    artifact_status="partial",
                    status=self.status,
                    reason=str(reason),
                    missing_criteria=verification.get("missing_criteria", []),
                    final_refs=partial_final_refs,
                    final_result=partial_final_result,
                ),
                "artifact_refs": promoted_refs,
                "missing_criteria": verification.get("missing_criteria", []),
            }
            if partial_final_result not in (None, ""):
                self.result["final_result"] = partial_final_result
            await self._emit_progress(
                iteration_index,
                self.status,
                f"Iteration {iteration_index}: { reason }",
            )
            await self._record_phase(
                "terminal",
                iteration=iteration_index,
                diagnostics={"status": self.status, "accepted": False, "artifact_status": "partial"},
            )
            await self._emit("agent_task.blocked", self.result)
            frame["iteration_result"] = {"terminal": True, "status": self.status}
            return frame

        await self._emit_progress(
            iteration_index,
            "replan",
            (
                f"Iteration {iteration_index}: consume the new observation and plan the next bounded step."
                if verification_source == "consumer_driven_continuation"
                and verification.get("replan_signal", {}).get("status") == "continue"
                and not verification.get("guard_reasons")
                else f"Iteration {iteration_index}: verification found gaps; the next iteration will replan."
            ),
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.replan",
            {
                "reason": verification.get("reason"),
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
                "replan_instruction": verification.get("replan_instruction"),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
                "replan_signals": verification.get("replan_signals", []),
                "verification_source": verification_source,
            },
        )
        await self._record_phase(
            "replanned",
            iteration=iteration_index,
            diagnostics={
                "verification_source": verification_source,
                "reason": verification.get("reason"),
                "failure_analysis": verification.get("failure_analysis", ""),
                "acceptance_delta": verification.get("acceptance_delta", []),
                "replan_instruction": verification.get("replan_instruction"),
                "repair_constraints": verification.get("repair_constraints", []),
                "next_step_requirements": verification.get("next_step_requirements", []),
                "replan_signals": verification.get("replan_signals", []),
            },
        )
        frame["iteration_result"] = {"terminal": False, "status": "continue"}
        return frame

    def _flat_execution_meta_with_skill_context_consumptions(
        self,
        execution_meta: Mapping[str, Any],
        *,
        context_pack: "TaskContextView",
    ) -> dict[str, Any]:
        meta = dict(execution_meta)
        if not isinstance(context_pack, Mapping):
            return meta
        skill_projection = context_pack.get("skill_projection")
        if not isinstance(skill_projection, Mapping):
            return meta
        loaded_skill_ids = self._skill_projection_skill_ids(skill_projection)
        if not loaded_skill_ids:
            return meta

        logs = meta.get("logs")
        logs = dict(logs) if isinstance(logs, Mapping) else {}
        response_ids = self._normalize_string_list(logs.get("model_response_ids"))
        existing_consumptions = [
            dict(item)
            for item in logs.get("skill_context_consumptions", [])
            if isinstance(item, Mapping)
        ]
        existing_keys = {
            (str(item.get("skill_id") or ""), str(item.get("request_id") or ""))
            for item in existing_consumptions
        }
        skill_items = {
            str(item.get("skill_id") or "").strip(): item
            for item in skill_projection.get("skills", [])
            if isinstance(item, Mapping) and str(item.get("skill_id") or "").strip()
        }
        package_id = str(context_pack.get("package_id") or "")
        for response_id in response_ids:
            for skill_id in loaded_skill_ids:
                key = (skill_id, response_id)
                if key in existing_keys:
                    continue
                skill_item = skill_items.get(skill_id, {})
                existing_consumptions.append(
                    {
                        "skill_id": skill_id,
                        "binding_id": str(skill_item.get("binding_id") or ""),
                        "revision_ref": str(skill_item.get("revision_ref") or ""),
                        "package_id": package_id,
                        "request_id": response_id,
                        "phase": "work.execute",
                    }
                )
                existing_keys.add(key)
        logs["skill_context_consumptions"] = DataFormatter.sanitize(existing_consumptions)
        meta["logs"] = DataFormatter.sanitize(logs)

        diagnostics = meta.get("diagnostics")
        diagnostics = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
        diagnostics["flat_skill_context_consumptions"] = {
            "available_skill_ids": loaded_skill_ids,
            "consumed_skill_ids": sorted(
                {
                    str(item.get("skill_id") or "")
                    for item in existing_consumptions
                    if str(item.get("request_id") or "")
                }
            ),
            "consumption_count": len(existing_consumptions),
            "source": "task_context_consumption",
        }
        meta["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return meta

    def _should_request_flat_final_verification(
        self,
        execution_result: Any,
        execution_meta: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        status = str(execution_meta.get("status") or "").strip().lower()
        if status in {"failed", "error", "timed_out", "blocked"}:
            return True, {"reason": "execution_status_requires_verification", "status": status}
        if not isinstance(execution_result, Mapping):
            return True, {"reason": "non_mapping_execution_result"}
        remaining_work = self._normalize_string_list(execution_result.get("remaining_work"))
        ready_for_final_verification = execution_result.get("ready_for_final_verification")
        ready_is_explicit = "ready_for_final_verification" in execution_result
        ready_is_true = self._normalize_bool(ready_for_final_verification, default=True) is True
        if ready_is_explicit and not ready_is_true:
            return False, {
                "reason": "work_unit_not_ready_for_final_verification",
                "remaining_work": remaining_work,
            }
        if remaining_work:
            if ready_is_explicit and ready_is_true:
                return True, {
                    "reason": "explicit_ready_for_final_verification",
                    "remaining_work": remaining_work,
                }
            return False, {
                "reason": "work_unit_reports_remaining_work",
                "remaining_work": remaining_work,
            }
        return True, {"reason": "ready_for_final_verification"}

    def _flat_consumer_continuation_verification(
        self,
        execution_result: Any,
        execution_meta: Mapping[str, Any],
        *,
        decision: Mapping[str, Any],
        grounding_guard: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_summary = self._cumulative_execution_evidence_summary(dict(execution_meta))
        remaining_work = []
        if isinstance(execution_result, Mapping):
            remaining_work = self._normalize_string_list(execution_result.get("remaining_work"))
        if not remaining_work:
            remaining_work = self._normalize_string_list(decision.get("remaining_work"))
        reason = "Current work unit produced intermediate evidence for the next Flat iteration to consume."
        if remaining_work:
            reason = "Current work unit reported remaining work for the next Flat iteration."
        raw_verification = {
            "is_complete": False,
            "requires_block": False,
            "reason": reason,
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": remaining_work,
            "final_result_required": False,
            "final_result": "",
        }
        normalized = self._normalize_verification(
            raw_verification,
            execution_evidence_summary=raw_summary,
            candidate_final_result="",
            grounding_guard=grounding_guard,
            terminal=False,
        )
        normalized["verification_source"] = "consumer_driven_continuation"
        normalized["consumer_driven_sufficiency"] = {
            "consumer": "next_flat_iteration",
            "decision": DataFormatter.sanitize(dict(decision)),
        }
        return normalized

    async def _build_context(self) -> "TaskContextView":
        return await self._context_pack_with_task_context(
            cast(
                "TaskContextView",
                {
                    "goal": self.goal,
                    "profile": "task_context",
                    "items": [],
                    "omitted": [],
                    "diagnostics": {"owner": "TaskContext"},
                },
            )
        )

    def _step_execution_policy(self) -> dict[str, Any]:
        agent_task_options = self.options.get("agent_task")
        effort = agent_task_options.get("effort") if isinstance(agent_task_options, dict) else None
        effort = effort if isinstance(effort, dict) else {}
        execution_policy = effort.get("execution")
        policy = dict(execution_policy) if isinstance(execution_policy, dict) else {}
        raw_step_plan = (
            str(policy.get("step_plan") or policy.get("step_execution") or policy.get("execution_shape") or "direct")
            .strip()
            .lower()
        )
        requested_step_plan = raw_step_plan
        if raw_step_plan in {"dynamic_task", "task_dag", "execution_dag", "dag"}:
            raw_step_plan = "direct"
            policy["step_plan_degraded_from"] = requested_step_plan
            policy["step_plan_degradation_reason"] = "task_dag_not_agent_execution_strategy"
        if raw_step_plan not in {"direct", "auto"}:
            raw_step_plan = "direct"
        effective_execution_strategy = self.effective_execution_strategy or (
            "flat" if self.execution_strategy == "flat" else self.execution_strategy
        )
        explicit_flat_strategy = self.execution_strategy == "flat"
        if explicit_flat_strategy:
            raw_step_plan = "direct"
        policy["step_plan"] = raw_step_plan
        policy["execution_strategy"] = self.execution_strategy
        policy["effective_execution_strategy"] = effective_execution_strategy
        if "max_tasks" not in policy and "max_plan_items" in policy:
            policy["max_tasks"] = policy.get("max_plan_items")
        policy["allow_dag_steps"] = False
        failed_dag_shapes = sorted(self._failed_execution_shapes.intersection(_DEGRADED_DAG_STEP_EXECUTION_SHAPES))
        if failed_dag_shapes:
            policy["suppressed_execution_shapes"] = failed_dag_shapes
        return policy

    def _execution_prompt_context(self) -> dict[str, Any]:
        raw = self.options.get("execution_prompt_snapshot")
        if not isinstance(raw, Mapping):
            return {}
        return cast(dict[str, Any], DataFormatter.sanitize(dict(raw)))

    def _language_policy(self) -> dict[str, Any]:
        raw_policy = self._agent_task_option("language_policy", None)
        if raw_policy is None:
            raw_policy = self._agent_task_option("language", None)
        if raw_policy is None:
            raw_policy = language_policy_from_prompt_snapshot(self.options.get("execution_prompt_snapshot"))
        if raw_policy is None:
            getter = getattr(getattr(self.agent, "settings", None), "get", None)
            if callable(getter):
                raw_policy = getter("agent.language_policy", None)
        progress_language = self._agent_task_option("progress_language", None) or self._agent_task_option(
            "stream_progress_language", None
        )
        if isinstance(raw_policy, Mapping):
            return dict(resolve_language_policy(base=raw_policy, progress_language=progress_language))
        return dict(resolve_language_policy(raw_policy or "auto", progress_language=progress_language))

    def _apply_language_policy_to_request(self, request: Any, policy: Mapping[str, Any] | None = None) -> None:
        apply_language_policy_to_prompt(getattr(request, "prompt", request), policy or self._language_policy())

    def _required_task_workspace_deliverables(self) -> list[str]:
        paths: list[str] = []

        def add_path(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in paths:
                paths.append(text)

        def add_deliverables(value: Any) -> None:
            if isinstance(value, str):
                add_path(value)
                return
            if isinstance(value, Mapping):
                path = value.get("path") or value.get("file") or value.get("name")
                if path is not None:
                    add_path(path)
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    add_deliverables(item)

        def add_contract(value: Any) -> None:
            if not isinstance(value, Mapping):
                add_deliverables(value)
                return
            add_deliverables(value.get("deliverables"))
            add_deliverables(value.get("required_deliverables"))

        add_contract(self._agent_task_option("output_contract", None))
        add_deliverables(self._agent_task_option("required_deliverables", None))
        add_deliverables(self._agent_task_option("deliverables", None))

        execution_prompt = self._execution_prompt_context()
        add_contract(execution_prompt.get("output_contract"))
        prompt_input = execution_prompt.get("input")
        if isinstance(prompt_input, Mapping):
            add_contract(prompt_input.get("output_contract"))
            add_deliverables(prompt_input.get("required_deliverables"))
            case = prompt_input.get("case")
            if isinstance(case, Mapping):
                add_contract(case.get("output_contract"))
        add_deliverables(getattr(self, "_taskboard_planned_task_workspace_deliverables", []))
        return paths

    def _normalize_step_plan(self, plan: Any) -> dict[str, Any]:
        normalized: dict[str, Any]
        if isinstance(plan, dict):
            normalized = plan
        else:
            normalized = {"step_instruction": str(plan), "expected_evidence": "", "rationale": ""}
        if not str(normalized.get("expected_evidence") or "").strip():
            expected_evidence_alias = normalized.pop("expected_expected_evidence", None)
            if isinstance(expected_evidence_alias, str) and expected_evidence_alias.strip():
                normalized["expected_evidence"] = expected_evidence_alias.strip()
                diagnostics = normalized.setdefault("normalization_diagnostics", [])
                if isinstance(diagnostics, list):
                    diagnostics.append(
                        {
                            "code": "agent_task.flat_plan.expected_evidence_alias",
                            "source_key": "expected_expected_evidence",
                            "target_key": "expected_evidence",
                        }
                    )
        raw_shape = (
            normalized.get("execution_shape")
            or normalized.get("step_kind")
            or normalized.get("route")
            or normalized.get("route_hint")
            or "direct"
        )
        shape = self._normalize_step_execution_shape(raw_shape)
        normalized["execution_shape"] = shape
        normalized.setdefault("effective_execution_shape", shape)
        normalized.setdefault("step_instruction", "")
        normalized.setdefault("expected_evidence", "")
        normalized.setdefault("rationale", "")
        # Structured step scope (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # scope comes from explicit capability lists, never from parsing the
        # natural-language step_instruction. `allowed_action_ids` is retained as an
        # internal alias for the action-id enforcement seam.
        raw_scope = normalized.get("step_scope")
        if not isinstance(raw_scope, dict):
            raw_scope = {}
        allowed_capability_ids = self._normalize_string_list(
            raw_scope.get("allowed_capability_ids", normalized.get("allowed_action_ids"))
        )
        normalized["step_scope"] = {"allowed_capability_ids": allowed_capability_ids}
        normalized["allowed_action_ids"] = allowed_capability_ids
        required_action_ids = self._normalize_string_list(
            normalized.get("required_action_ids")
            or normalized.get("required_actions")
            or raw_scope.get("required_action_ids")
            or raw_scope.get("required_actions")
        )
        normalized["required_action_ids"] = required_action_ids
        raw_action_commands = normalized.get("action_commands")
        if raw_action_commands is not None:
            normalized["action_commands"] = DataFormatter.sanitize(raw_action_commands)
        if raw_action_commands not in (None, [], ()) and shape != "actions":
            normalized["declared_execution_shape"] = shape
            diagnostics = normalized.setdefault("normalization_diagnostics", [])
            if isinstance(diagnostics, list):
                diagnostics.append(
                    {
                        "code": "agent_task.flat_plan.action_commands_override_shape",
                        "declared_execution_shape": shape,
                        "resolved_execution_shape": "actions",
                    }
                )
            normalized["execution_shape"] = "actions"
            normalized["effective_execution_shape"] = "actions"
        scoped_retrieval = self._normalize_scoped_retrieval_plan(normalized.get("scoped_retrieval"))
        if scoped_retrieval:
            normalized["scoped_retrieval"] = scoped_retrieval
        else:
            normalized.pop("scoped_retrieval", None)
        self._normalize_step_deliverable_mode(normalized)
        return normalized

    @classmethod
    def _normalize_scoped_retrieval_plan(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            return {}
        raw_queries = raw.get("query_groups", raw.get("queries", raw.get("query")))
        if raw_queries is None:
            query_values: list[Any] = []
        elif isinstance(raw_queries, str):
            query_values = [raw_queries]
        elif isinstance(raw_queries, Sequence) and not isinstance(raw_queries, (bytes, bytearray)):
            query_values = list(raw_queries)
        else:
            query_values = [raw_queries]
        query_groups: list[dict[str, Any]] = []
        for item in query_values:
            if isinstance(item, Mapping):
                query = str(item.get("query") or item.get("text") or item.get("keyword") or "").strip()
                expected_role = str(item.get("expected_role") or item.get("role") or "").strip()
                candidate: dict[str, Any] = {
                    "query": query,
                    "expected_role": expected_role if expected_role in {"evidence_snippet", "locator_ref"} else "",
                }
                for key in ("path", "pattern", "collection", "kind"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        candidate[key] = cls._normalize_scoped_retrieval_pattern(value) if key == "pattern" else value
                raw_source_kinds = item.get("source_kinds")
                if isinstance(raw_source_kinds, str):
                    source_kinds = [raw_source_kinds.strip()]
                elif isinstance(raw_source_kinds, Sequence) and not isinstance(
                    raw_source_kinds, (bytes, bytearray)
                ):
                    source_kinds = [str(value).strip() for value in raw_source_kinds]
                else:
                    source_kinds = []
                source_kinds = [
                    value
                    for value in source_kinds
                    if value
                ]
                if source_kinds:
                    candidate["source_kinds"] = list(dict.fromkeys(source_kinds))
                for key in (
                    "max_results",
                    "snippet_limit",
                    "snippet_offset",
                    "max_file_bytes",
                ):
                    value = item.get(key)
                    if value is not None:
                        candidate[key] = DataFormatter.sanitize(value)
                for key in ("tags",):
                    value = item.get(key)
                    if value is not None:
                        candidate[key] = DataFormatter.sanitize(value)
                if item.get("include_hidden") is not None:
                    candidate["include_hidden"] = bool(item.get("include_hidden"))
                filters = item.get("filters")
                if isinstance(filters, Mapping):
                    candidate["filters"] = DataFormatter.sanitize(dict(filters))
                content_queries = cls._scoped_retrieval_content_queries(candidate.get("filters"))
            else:
                query = str(item or "").strip()
                candidate = {"query": query, "expected_role": ""}
                content_queries = []
            if not query and not content_queries:
                continue
            if not candidate.get("expected_role"):
                candidate.pop("expected_role", None)
            if content_queries:
                template = dict(candidate)
                filters = template.get("filters")
                if isinstance(filters, Mapping):
                    filtered = dict(filters)
                    filtered.pop("content_contains", None)
                    if filtered:
                        template["filters"] = filtered
                    else:
                        template.pop("filters", None)
                for content_query in content_queries:
                    expanded = dict(template)
                    expanded["query"] = content_query
                    query_groups.append(expanded)
                continue
            query_groups.append(candidate)
        if not query_groups:
            return {}
        raw_fallback_order = raw.get("fallback_order") or raw.get("fallbacks")
        if isinstance(raw_fallback_order, str):
            fallback_values = [raw_fallback_order]
        elif isinstance(raw_fallback_order, Sequence) and not isinstance(
            raw_fallback_order, (bytes, bytearray)
        ):
            fallback_values = list(raw_fallback_order)
        else:
            fallback_values = []
        fallback_order: list[str] = []
        for item in fallback_values:
            text = str(item or "").strip()
            if text:
                fallback_order.append(text)
        normalized: dict[str, Any] = {"query_groups": query_groups}
        if fallback_order:
            normalized["fallback_order"] = fallback_order[:8]
        return normalized

    @staticmethod
    def _normalize_scoped_retrieval_pattern(value: str) -> str:
        text = str(value or "").strip()
        if "," in text:
            return "**"
        return text

    @staticmethod
    def _scoped_retrieval_content_queries(filters: Any) -> list[str]:
        if not isinstance(filters, Mapping):
            return []
        raw_terms = filters.get("content_contains")
        if isinstance(raw_terms, str):
            values = [raw_terms]
        elif isinstance(raw_terms, Sequence) and not isinstance(raw_terms, (bytes, bytearray)):
            values = list(raw_terms)
        else:
            values = []
        queries: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in queries:
                queries.append(text)
        return queries

    def _normalize_step_deliverable_mode(self, plan: dict[str, Any]) -> None:
        raw_mode = str(plan.get("deliverable_mode") or "").strip().lower().replace("-", "_")
        mode_aliases = {
            "": "",
            "none": "",
            "inline": "inline_final",
            "inline_answer": "inline_final",
            "final_answer": "inline_final",
            "file": "task_workspace_artifact",
            "file_backed": "task_workspace_artifact",
            "artifact": "task_workspace_artifact",
            "task_workspace": "task_workspace_artifact",
            "sectioned": "sectioned_task_workspace_artifact",
            "sectioned_artifact": "sectioned_task_workspace_artifact",
            "sectioned_workspace": "sectioned_task_workspace_artifact",
        }
        normalized_mode = mode_aliases.get(raw_mode, raw_mode)
        if normalized_mode not in {"", "inline_final", "task_workspace_artifact", "sectioned_task_workspace_artifact"}:
            normalized_mode = ""

        required_deliverables = self._required_task_workspace_deliverables()
        if required_deliverables and not normalized_mode:
            plan["deliverable_mode"] = "sectioned_task_workspace_artifact"
            plan["deliverable_mode_source"] = "required_task_workspace_deliverables"
            plan.setdefault("required_task_workspace_deliverables", required_deliverables)
            plan.setdefault("prefer_stream_draft", True)
            return
        if normalized_mode:
            plan["deliverable_mode"] = normalized_mode
            plan.setdefault("deliverable_mode_source", "planner")

    @staticmethod
    def _normalize_step_execution_shape(value: Any) -> str:
        text = str(value or "direct").strip().lower().replace("-", "_")
        aliases = {
            "model": "direct",
            "model_request": "direct",
            "direct_request": "direct",
            "flat": "direct",
            "flat_react": "direct",
            "action": "actions",
            "tool": "actions",
            "tools": "actions",
            "dag": "dynamic_task",
            "task_dag": "dynamic_task",
            "dynamic_task_dag": "dynamic_task",
            "agent_execution_dag": "execution_dag",
        }
        normalized = aliases.get(text, text)
        return normalized if normalized in _STEP_EXECUTION_SHAPES else "direct"

    def _configure_step_execution(self, execution: Any, plan: dict[str, Any]) -> dict[str, Any]:
        policy = self._step_execution_policy()
        # Validate before use_actions/require_actions can mutate registration tags.
        # Model-required ids and planner snapshots select from Host authority.
        from agently.core.operation.Action.ActionMetadata import _scoped_action_list

        host_ids = {
            str(item.get("action_id") or item.get("name") or "")
            for item in _scoped_action_list(self.agent.action, self.agent.name)
        }
        step_scope = plan.get("step_scope")
        if not isinstance(step_scope, dict):
            step_scope = {}
        allowed_capability_ids = self._normalize_string_list(
            step_scope.get("allowed_capability_ids")
        )
        raw_required_action_ids = self._normalize_string_list(plan.get("required_action_ids"))
        outside = (set(allowed_capability_ids) | set(raw_required_action_ids)) - host_ids
        if outside:
            raise PermissionError("Step Actions are outside the visible execution scope: " + ", ".join(sorted(outside)))
        if allowed_capability_ids and set(raw_required_action_ids) - set(allowed_capability_ids):
            raise PermissionError("Required Actions are outside the declared step scope.")
        requested_shape = str(plan.get("execution_shape") or "direct")
        effective_shape = requested_shape
        dag_allowed = False
        warning: str | None = None

        dag_shape_degraded = requested_shape in _DEGRADED_DAG_STEP_EXECUTION_SHAPES
        if dag_shape_degraded:
            effective_shape = "direct"
            warning = "dag_shape_not_agent_execution_strategy"

        pending_action_requirements = self._pending_action_succeeded_requirements()
        if effective_shape == "direct" and pending_action_requirements:
            action_capability_ids = {
                str(item.get("id") or "").strip()
                for item in self._planner_capabilities()
                if isinstance(item, Mapping)
                and str(item.get("kind") or "").strip() == "action"
                and str(item.get("id") or "").strip()
                and str(item.get("id") or "").strip() in host_ids
            }
            if not action_capability_ids:
                action_candidates = getattr(execution, "action_candidates", None)
                if callable(action_candidates):
                    try:
                        raw_action_candidates = action_candidates() or []
                        candidates = (
                            cast(Sequence[Any], raw_action_candidates)
                            if isinstance(raw_action_candidates, Sequence)
                            else []
                        )
                        for item in candidates:
                            if not isinstance(item, Mapping):
                                continue
                            action_id = str(item.get("action_id") or item.get("name") or "").strip()
                            if action_id and action_id in host_ids:
                                action_capability_ids.add(action_id)
                    except Exception:
                        action_capability_ids = set()
            if action_capability_ids:
                effective_shape = "actions"
                plan["execution_shape_adjustment"] = {
                    "from": requested_shape,
                    "to": effective_shape,
                    "reason": "pending_action_succeeded_evidence",
                    "pending_action_ids": [
                        action_id for action_id in pending_action_requirements if action_id in action_capability_ids
                    ],
                }

        plan["effective_execution_shape"] = effective_shape
        # Structured step scope (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC):
        # when the plan names an explicit capability allowlist, narrow this step's
        # action candidates to it via the execution-local action-id seam. Scope
        # comes from the structured step_scope field, never from parsing the
        # step_instruction prose. The hard guarantee remains the verifier evidence
        # gate; this only prevents an evidence-gathering step from silently
        # completing the whole task with unrelated capabilities.
        task_contract_required_action_ids = self._task_contract_required_action_ids()
        task_required_action_ids = [
            action_id for action_id in raw_required_action_ids if action_id in task_contract_required_action_ids
        ]
        step_required_action_ids = [
            action_id for action_id in raw_required_action_ids if action_id not in task_contract_required_action_ids
        ]
        scoped_action_ids = self._merge_string_lists(allowed_capability_ids, step_required_action_ids)
        if scoped_action_ids and effective_shape in {"direct", "actions"}:
            use_actions = getattr(execution, "use_actions", None)
            if callable(use_actions):
                use_actions(scoped_action_ids)
            else:
                local_action_ids = getattr(execution, "local_action_ids", None)
                if isinstance(local_action_ids, list):
                    for capability_id in scoped_action_ids:
                        if capability_id not in local_action_ids:
                            local_action_ids.append(capability_id)
                sync_action_scope = getattr(execution, "_sync_action_scope", None)
                if callable(sync_action_scope):
                    sync_action_scope(source="AgentTask.step_scope")
        action_scope_source = (
            "step_required_action_ids"
            if step_required_action_ids
            else ("step_scope" if allowed_capability_ids else "")
        )
        if task_required_action_ids and effective_shape in {"direct", "actions"}:
            require_actions = getattr(execution, "require_actions", None)
            if callable(require_actions):
                require_actions(task_required_action_ids)
            else:
                local_action_ids = getattr(execution, "local_action_ids", None)
                if isinstance(local_action_ids, list):
                    for action_id in task_required_action_ids:
                        if action_id not in local_action_ids:
                            local_action_ids.append(action_id)
                local_required_action_ids = getattr(execution, "local_required_action_ids", None)
                if isinstance(local_required_action_ids, list):
                    for action_id in task_required_action_ids:
                        if action_id not in local_required_action_ids:
                            local_required_action_ids.append(action_id)
                sync_action_scope = getattr(execution, "_sync_action_scope", None)
                if callable(sync_action_scope):
                    sync_action_scope(source="AgentTask.required_action_ids")
            action_scope_source = "required_action_ids"
        if effective_shape == "actions" and not allowed_capability_ids:
            action_capability_ids = [
                str(item.get("id") or "").strip()
                for item in self._planner_capabilities()
                if isinstance(item, Mapping)
                and str(item.get("kind") or "").strip() == "action"
                and str(item.get("id") or "").strip()
                and str(item.get("id") or "").strip() in host_ids
            ]
            if action_capability_ids:
                use_actions = getattr(execution, "use_actions", None)
                if callable(use_actions):
                    use_actions(action_capability_ids)
                    action_scope_source = "planner_capabilities"
        step_execution = {
            "requested_shape": requested_shape,
            "effective_shape": effective_shape,
            "dag_allowed": dag_allowed,
            "dag_shape_degraded": dag_shape_degraded,
            "step_scope": DataFormatter.sanitize(step_scope),
            "required_action_ids": DataFormatter.sanitize(raw_required_action_ids),
            "task_required_action_ids": DataFormatter.sanitize(task_required_action_ids),
            "step_required_action_ids": DataFormatter.sanitize(step_required_action_ids),
            "action_scope_source": action_scope_source,
            "policy": DataFormatter.sanitize(policy),
        }
        route_policy = self._route_policy_for_step_execution(effective_shape)
        if route_policy:
            apply_route_policy = getattr(execution, "route_policy", None)
            if callable(apply_route_policy):
                apply_route_policy(route_policy)
            step_execution["route_policy"] = DataFormatter.sanitize(route_policy)
        if warning is not None:
            step_execution["warning"] = warning
            execution_warnings = plan.get("execution_warnings")
            if not isinstance(execution_warnings, list):
                execution_warnings = []
            execution_warnings.append(warning)
            plan["execution_warnings"] = execution_warnings
            self.diagnostics.setdefault("step_execution_warnings", []).append(
                {"iteration_shape": requested_shape, "warning": warning}
            )
        plan["step_execution"] = step_execution
        record_option = getattr(execution, "record_consumed_option", None)
        if callable(record_option):
            record_option("agent_task.step.execution_shape", effective_shape, owner="AgentTask")
            if route_policy:
                record_option("agent_task.step.route_policy", route_policy, owner="AgentTask")
            if policy.get("step_plan") != "direct":
                record_option("effort.execution.step_plan", policy.get("step_plan"), owner="AgentTask")
        return step_execution

    @staticmethod
    def _route_policy_for_step_execution(effective_shape: str) -> dict[str, Any]:
        route_by_shape = {
            "direct": "model_request",
            "actions": "model_request",
        }
        route = route_by_shape.get(str(effective_shape or "").strip())
        if route is None:
            return {}
        return {
            "allowed_routes": [route],
            # A bounded step that cannot honor its selected shape must not silently
            # run model_request: block so the loop sees the mismatch and replans.
            "on_violation": "block",
            "owner": "AgentTask",
            "step_execution_shape": effective_shape,
        }

    def _planner_capabilities(self) -> list[dict[str, Any]]:
        """Planner-facing capability candidate snapshot (inert data only).

        Read from the typed snapshot the orchestrator route injected into options
        at task construction (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC).
        Covers executable Actions only. Real-world Skills are already-bound
        TaskContext sources and never become planner capabilities or routes.
        AgentTask consumes only this snapshot; it does not reach back into the
        routing plugin.
        """
        raw = self.options.get("planner_capabilities")
        if not isinstance(raw, list):
            return []
        capabilities: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            capability_id = str(item.get("id") or item.get("capability_id") or "").strip()
            if not capability_id:
                continue
            kind = str(item.get("kind") or "action")
            if kind != "action":
                continue
            entry: dict[str, Any] = {
                "id": capability_id,
                "kind": kind,
                "route": str(item.get("route") or "model_request"),
                "guidance_access": str(item.get("guidance_access") or "none"),
                "description": str(item.get("description") or ""),
            }
            if item.get("mode"):
                entry["mode"] = str(item.get("mode"))
            if "side_effect_level" in item:
                entry["side_effect_level"] = str(item.get("side_effect_level") or "")
            if "replay_safe" in item:
                entry["replay_safe"] = self._normalize_bool(item.get("replay_safe"), default=False)
            if isinstance(item.get("execution_resource_requirements"), Sequence) and not isinstance(
                item.get("execution_resource_requirements"),
                str | bytes | bytearray,
            ):
                entry["execution_resource_requirements"] = DataFormatter.sanitize(
                    [
                        dict(requirement)
                        for requirement in item.get("execution_resource_requirements", [])
                        if isinstance(requirement, Mapping)
                    ]
                )
            evidence_requirement_kind = str(item.get("evidence_requirement_kind") or "").strip()
            if evidence_requirement_kind:
                entry["evidence_requirement_kind"] = evidence_requirement_kind
            capabilities.append(entry)
        return capabilities

    @classmethod
    def _capability_evidence_requirements_from_mapping(cls, source: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(source, Mapping):
            return []
        raw = source.get("capability_evidence_requirements")
        return cls._normalize_capability_evidence_requirements(raw)

    @classmethod
    def _normalize_capability_evidence_requirements(cls, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, dict):
            raw = raw.get("capabilities") or raw.get("skills")
        if not isinstance(raw, (list, tuple)):
            return []
        requirements: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, str):
                capability_id = item.strip()
                if capability_id:
                    requirements.append(
                        {
                            "capability_id": capability_id,
                            "kind": "capability_used",
                            "required": True,
                            "source": "criterion",
                        }
                    )
                continue
            if not isinstance(item, dict):
                continue
            capability_id = str(item.get("capability_id") or item.get("id") or "").strip()
            if not capability_id:
                continue
            requirement: dict[str, Any] = {
                "capability_id": capability_id,
                "kind": str(item.get("kind") or "capability_used"),
                "required": bool(item.get("required", True)),
                "source": str(item.get("source") or "criterion"),
            }
            if item.get("capability_kind"):
                requirement["capability_kind"] = str(item.get("capability_kind"))
            if item.get("criterion_id"):
                requirement["criterion_id"] = str(item.get("criterion_id"))
            requirements.append(requirement)
        return requirements

    @classmethod
    def _merge_capability_evidence_requirements(
        cls,
        *groups: Sequence[Mapping[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for group in groups:
            if not isinstance(group, Sequence) or isinstance(group, (str, bytes, bytearray)):
                continue
            for item in group:
                if not isinstance(item, Mapping):
                    continue
                capability_id = str(item.get("capability_id") or item.get("id") or "").strip()
                if not capability_id:
                    continue
                kind = str(item.get("kind") or "capability_used")
                capability_kind = str(item.get("capability_kind") or "")
                criterion_id = str(item.get("criterion_id") or "")
                key = (capability_id, kind, capability_kind, criterion_id)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(DataFormatter.sanitize(dict(item)))
        return merged

    def _capability_evidence_requirements(
        self,
        source: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Structured, authored completion-evidence requirements (inert data).

        The load-bearing gate's trigger
        (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC): which capabilities
        must appear in execution evidence for the task to be acceptable. Authored
        as a structured option, independent of capability mode; never inferred
        from free-text criteria. Accepts either a list of capability-id strings
        (treated as `capability_used`) or a list of EvidenceRequirement dicts. The
        """
        option_requirements = self._capability_evidence_requirements_from_mapping(self.options)
        source_requirements = self._capability_evidence_requirements_from_mapping(source)
        return self._merge_capability_evidence_requirements(option_requirements, source_requirements)

    def _pending_action_succeeded_requirements(self) -> list[str]:
        pending: list[str] = []
        for requirement in self._capability_evidence_requirements():
            if not requirement.get("required", True):
                continue
            if str(requirement.get("kind") or "capability_used") != "action_succeeded":
                continue
            capability_id = str(requirement.get("capability_id") or "").strip()
            if capability_id and capability_id not in self._satisfied_succeeded_actions and capability_id not in pending:
                pending.append(capability_id)
        return pending

    def _task_contract_required_action_ids(self) -> set[str]:
        required: set[str] = set()
        option_sources: list[Any] = [self.options]
        agent_task_options = self.options.get("agent_task") if isinstance(self.options, Mapping) else None
        if isinstance(agent_task_options, Mapping):
            option_sources.append(agent_task_options)
        for source in option_sources:
            if not isinstance(source, Mapping):
                continue
            constraints = source.get("capability_constraints")
            if isinstance(constraints, Mapping):
                actions = constraints.get("actions")
                raw_required = actions.get("required", []) if isinstance(actions, Mapping) else constraints.get("required_actions", [])
                required.update(self._normalize_string_list(raw_required))
        for requirement in self._capability_evidence_requirements():
            if not requirement.get("required", True):
                continue
            if str(requirement.get("kind") or "capability_used") != "action_succeeded":
                continue
            capability_id = str(requirement.get("capability_id") or "").strip()
            if capability_id:
                required.add(capability_id)
        return required

    def _untried_read_action_continuation(
        self,
        execution_evidence_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        read_action_ids = {
            str(item.get("id") or "").strip()
            for item in self._planner_capabilities()
            if isinstance(item, Mapping)
            and str(item.get("kind") or "").strip() == "action"
            and str(item.get("id") or "").strip()
            and str(item.get("side_effect_level") or "").strip().lower() == "read"
        }
        if not read_action_ids:
            return {}
        used_action_ids = set(self._normalize_string_list(execution_evidence_summary.get("action_ids")))
        used_action_ids.update(self._satisfied_capabilities)
        untried_action_ids = sorted(read_action_ids - used_action_ids)
        if not untried_action_ids:
            return {}
        blocked_actions = self._normalize_string_list(execution_evidence_summary.get("blocked_actions"))
        approval_required_actions = self._normalize_string_list(
            execution_evidence_summary.get("approval_required_actions")
        )
        if blocked_actions or approval_required_actions:
            return {}
        failed_actions = self._normalize_string_list(execution_evidence_summary.get("failed_actions"))
        unsafe_failed_actions = [action_id for action_id in failed_actions if action_id not in read_action_ids]
        if unsafe_failed_actions:
            return {}
        artifact_evidence = execution_evidence_summary.get("capability_evidence")
        artifact_readbacks: list[str] = []
        if isinstance(artifact_evidence, Mapping):
            artifacts = artifact_evidence.get("artifacts")
            if isinstance(artifacts, Mapping):
                artifact_readbacks = self._normalize_string_list(artifacts.get("readback"))
        missing_required_read_actions = [
            action_id
            for action_id in self._normalize_string_list(execution_evidence_summary.get("missing_required_actions"))
            if action_id in read_action_ids
        ]
        if artifact_readbacks and not failed_actions and not missing_required_read_actions:
            return {}
        return {
            "reason": "read_action_continuation_available",
            "untried_action_ids": missing_required_read_actions or untried_action_ids,
            "failed_read_action_ids": sorted(action_id for action_id in failed_actions if action_id in read_action_ids),
        }

    def _evaluate_capability_evidence(
        self,
        source: Mapping[str, Any] | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Deterministically check structured evidence requirements.

        Returns (missing_capability_ids, unenforced_requirements). Checks run
        against capability evidence accumulated across iterations
        (`_satisfied_capabilities` for `capability_used`,
        `_satisfied_succeeded_actions` for `action_succeeded`). Only the wired
        combinations are enforced; anything without a structural producer
        (the reserved evidence kinds, and `capability_used` for a
        `dynamic_task` capability whose usage is not recorded in evidence) is
        returned as an unenforced diagnostic rather than silently passing or
        false-failing.
        """
        requirements = self._capability_evidence_requirements(source)
        if not requirements:
            return [], []

        missing: list[str] = []
        unenforced: list[dict[str, Any]] = []
        for requirement in requirements:
            if not requirement.get("required", True):
                continue
            capability_id = str(requirement.get("capability_id") or "").strip()
            if not capability_id:
                continue
            kind = str(requirement.get("kind") or "capability_used")
            capability_kind = str(requirement.get("capability_kind") or "")
            if kind == "capability_used" and capability_kind != "dynamic_task":
                if capability_id not in self._satisfied_capabilities:
                    missing.append(capability_id)
            elif kind == "action_succeeded":
                if capability_id not in self._satisfied_succeeded_actions:
                    missing.append(capability_id)
            else:
                unenforced.append(
                    {
                        "task_id": self.id,
                        "capability_id": capability_id,
                        "kind": kind,
                        "capability_kind": capability_kind,
                    }
                )
        # De-duplicate while preserving order.
        deduped: list[str] = []
        for capability_id in missing:
            if capability_id not in deduped:
                deduped.append(capability_id)
        return deduped, unenforced

    def _accumulate_capability_evidence(
        self,
        execution_evidence_summary: Mapping[str, Any],
    ) -> None:
        """Record host-observed capability facts before any completion gate.

        This is deliberately structural: Action success comes only from the
        accumulated Action evidence producer, never from verifier prose or a
        TaskWorkspace readback. Both terminal preflight and post-verifier
        normalization consume the same state owner.
        """

        self._satisfied_required_actions.update(
            self._normalize_string_list(execution_evidence_summary.get("action_ids"))
        )
        self._satisfied_required_skills.update(
            self._normalize_string_list(execution_evidence_summary.get("consumed_skill_ids"))
        )
        self._satisfied_capabilities.update(
            self._normalize_string_list(execution_evidence_summary.get("capabilities_used"))
        )
        capability_evidence = execution_evidence_summary.get("capability_evidence")
        if isinstance(capability_evidence, Mapping) and isinstance(
            capability_evidence.get("actions"),
            Mapping,
        ):
            self._satisfied_succeeded_actions.update(
                self._normalize_string_list(capability_evidence["actions"].get("succeeded"))
            )

    async def _request_plan(self, iteration_index: int, context_pack: "TaskContextView") -> dict[str, Any]:
        del context_pack
        request_context_pack, context_package = await self._read_task_context_view(
            phase="planning",
            consumer_id=f"agent_task:{self.id}:planner:iteration:{iteration_index}",
            intent=f"Plan iteration {iteration_index}: {self.goal}",
        )
        request = self.agent.create_temp_request()
        self._bind_task_context_attachments(request, context_package)
        language_policy = self._language_policy()
        self._apply_language_policy_to_request(request, language_policy)
        planner_capabilities = self._planner_capabilities()
        execution_prompt = self._execution_prompt_context()
        previous_iterations = self._iteration_prompt_summaries()
        repair_context = self._planner_repair_context(previous_iterations)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "iteration": iteration_index,
                "previous_iterations": previous_iterations,
                "repair_context": repair_context,
                "context_pack": DataFormatter.sanitize(request_context_pack),
                "execution_prompt": execution_prompt,
                "execution_policy": self._step_execution_policy(),
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "task_shape_analysis": DataFormatter.sanitize(self.task_shape_analysis),
                "planner_capabilities": planner_capabilities,
                "capability_evidence_requirements": self._capability_evidence_requirements(),
                "retrieval_policy": self._task_context_retrieval_policy(),
                "language_policy": language_policy,
            }
        )
        # Explanatory note only (not a guarantee): the hard guarantee is the
        # verifier evidence gate, not this prompt text. It tells the planner which
        # capabilities exist and how guidance reaches the bounded step.
        capability_note = (
            " Available executable Actions are listed in planner_capabilities. Required Skill "
            "instructions are already present in context_pack.skill_projection; they are context, "
            "not capabilities or routes. Choose Actions when the task needs Action, MCP, "
            "TaskWorkspace, or tool evidence."
            if planner_capabilities
            else ""
        )
        allowed_shapes = "direct or actions"
        strategy_note = (
            " The selected execution_strategy is flat: keep the task in a linear AgentTask loop and do not plan DAG or TaskBoard steps."
            if self.execution_strategy == "flat"
            else ""
        )
        request.instruct(
            "Plan the next bounded AgentExecution step for this AgentTask. "
            "Before choosing the execution shape, provide short turn_intent and decision_basis fields to frame this "
            "single-step decision; do not include raw chain-of-thought, hidden reasoning, or completion claims there. "
            "Treat execution_prompt as caller-provided task context, including any input, instructions, and output contract. "
            "Use task_context_contract for prompt-safe temporal policy and ref-backed intermediate-resource handling. "
            "Concrete runtime current_time values may be omitted from the model hot path; do not infer or write a "
            "current date/time as a business fact unless it appears in task facts or source evidence. It is not a resource cap. "
            "Use prior verification evidence when present. Do not finalize unless all success criteria can be verified. "
            "[input.repair_context.verification_source] distinguishes intermediate observations from verification feedback. "
            "For ordinary continuation, pending capabilities remain task obligations, not a failed step. "
            "Actual guard and repair findings still apply. "
            "Use [input.repair_context] to choose the next bounded step. The verifier does not choose tools, "
            "routes, execution shapes, or exact methods; the planner owns the next action while respecting grounded "
            "acceptance facts and deterministic guards. When repair_context.available_evidence_anchors is present, "
            "use its exact source_refs values and action_result_previews as the bounded evidence anchor set for repair; "
            "do not shorten, infer, or reconstruct URLs, file paths, or source refs from source titles or prose feedback. "
            "When repair_context.material_claim_repair_contract is present, repair its structured claim/state "
            "requirements directly; do not derive a factual repair contract from verifier prose. "
            "source_refs with content_state='ref_only' prove only discovery or materialization; read the referenced "
            "file/ref before using its content for repository, document, or source-grounded claims. "
            "When context_pack.skill_projection is present, its guidance and selected_resources are already "
            "TaskContext-disclosed Skill procedure. Apply it directly; do not treat it as business evidence or plan readback or "
            "scoped_retrieval over skills/... citations, and do not treat Skill citations as TaskWorkspace file paths "
            "or local registry paths. "
            "For web discovery tasks, if the task context already names an official domain, homepage, or URL and "
            "search results are empty, unstable, or inconclusive, plan a Browse step for that known entry point and "
            "follow same-site navigation links before concluding that the required source is unavailable. Search "
            "result snippets are discovery hints, not source evidence. Before using a search result snippet or a broad "
            "announcement page as the source boundary, plan Browse/readback for the candidate page and relevant same-site "
            "index/list/download/navigation pages so a more specific official source can be discovered. "
            f"Set execution_shape to {allowed_shapes}. "
            "Do not plan TaskDAG, DynamicTask, or DAG-shaped execution here; TaskDAG is an independent "
            "manual/configured or visual-automation orchestration surface, not an AgentTask strategy."
            + strategy_note
            + capability_note
            + " Optionally set step_scope.allowed_capability_ids to limit this bounded step to specific capability "
            "ids when it is only meant to gather evidence; leave it empty when the step may use any available capability. "
            "When the user explicitly requires a named action/tool to be called, or the next step cannot be accepted "
            "without that exact action's execution record, set required_action_ids to those action ids instead of "
            "claiming the action was requested in prose. The host will then provide only those required Action contracts "
            "to one narrow command request, using bounded stepwise execution when arguments need new Action results; "
            "do not attempt to reproduce strict "
            "Action kwargs from the compact planner capability list."
            " For TaskWorkspace, repository, or file-backed evidence, prefer scoped retrieval before bulk reads when it can "
            "reduce prompt input. If useful, return scoped_retrieval.query_groups with prioritized exact phrases or "
            "natural search text plus expected_role='evidence_snippet' or 'locator_ref'. Select source_kinds only from "
            "retrieval_policy.source_kinds and keep source-specific structural filters in separate query groups. ContextReader only "
            "record bounded facts; the planner/verifier must judge semantic usefulness after seeing snippets or readbacks. "
            "Use query_group.path and pattern for bounded file-source search. Query groups may express business filters, "
            "exact refs, tags, max_results, snippet_limit, and source-owned size bounds; never choose lexical, vector, "
            "rerank, or source-native search mechanisms because ContextIndex owns those mechanisms. "
            "Blocks use context_read.read and record ContextPackage diagnostics in bounded facts. "
            "For TaskWorkspace files, query is the content text to search, path is the directory or file scope, and pattern is a "
            "file glob such as '*.md' or '*' rather than another content keyword. "
            "When the task context names a concrete RecordStore collection/kind or TaskWorkspace path/scope, "
            "carry record collections as filters.collection; carry record kinds as filters.kind only when the exact kind "
            "is provided, never by guessing a generic kind such as 'note'; carry file scopes as path/pattern so scoped "
            "search targets task evidence "
            "instead of framework planning, checkpoint, verification, or reflection records."
            " For long, sectioned, or prose-heavy deliverables, separate the content-carrier decision from the "
            "control/evidence contract. A single freeform document can be drafted as natural Markdown/plain text. When "
            "field boundaries are required, preserve the caller's declared .output(..., format=...) contract such as "
            "xml_field, hybrid, or yaml_literal instead of forcing the long body into compact JSON fields. Keep status, "
            "evidence, and verification as separate compact judgment/readback contracts. If this AgentTask step must "
            "deliver through TaskWorkspace, choose deliverable_mode='task_workspace_artifact' or 'sectioned_task_workspace_artifact' and "
            "instruct the execution step to return either a complete bounded artifact body when it fits, or an "
            "artifact_manifest path plus a section outline as the structured deliverable contract when the body is too long. "
            "The model must not self-declare trusted file_refs for a deliverable, and artifact_manifest is not itself "
            "proof that the artifact body exists."
        )
        request.output(
            {
                "turn_intent": (
                    str,
                    "One short sentence stating what this iteration should accomplish.",
                    False,
                ),
                "decision_basis": (
                    [str],
                    "Short factors that justify the bounded-step decision; no raw chain-of-thought.",
                    False,
                ),
                "execution_shape": (
                    str,
                    "Execution shape for this bounded step: direct or actions",
                    False,
                ),
                "step_instruction": (str, "Instruction for one bounded AgentExecution step", True),
                "expected_evidence": (str, "Evidence this step should produce", False),
                "rationale": (str, "Why this is the next step", True),
                "deliverable_mode": (
                    str,
                    "inline_final, task_workspace_artifact, or sectioned_task_workspace_artifact for expected deliverables",
                    False,
                ),
                "step_scope": (
                    dict,
                    "Optional structured scope: {allowed_capability_ids: [...]}; empty means no restriction",
                    False,
                ),
                "required_action_ids": (
                    [str],
                    "Action ids that must produce real ActionRuntime evidence in this bounded step; use for explicit user-required tools/actions",
                    False,
                ),
                "scoped_retrieval": (
                    dict,
                    "Optional retrieval plan: {query_groups: [{query, expected_role, source_kinds?: [offered source kind], path?, pattern?, filters?, tags?, max_results?, snippet_limit?, max_file_bytes?}], fallback_order?: [...]}; use only retrieval_policy.source_kinds and express business scope, never retrieval mechanisms; executors return facts only",
                    False,
                ),
            },
            format="json",
        )
        result_handle = request.get_result()
        plan = await self._await_task_request(result_handle.async_get_data(), stage="plan")
        self._record_task_context_consumption(
            context_package,
            request_id=result_handle.id,
        )
        await self._emit_required_skill_context_bound(
            request_context_pack,
            request_id=result_handle.id,
            phase="work.plan",
        )
        return self._normalize_step_plan(plan)

    async def _try_flat_preplanned_action_calls(
        self,
        iteration_index: int,
        plan: Mapping[str, Any],
        *,
        raw_commands_override: Any = None,
        command_source: str = "flat_plan",
        action_planning_model_requests: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "") != "actions":
            return None
        raw_commands = raw_commands_override if raw_commands_override is not None else plan.get("action_commands")
        if raw_commands in (None, [], ()):
            return None
        step_scope = plan.get("step_scope")
        allowed_action_ids = self._normalize_string_list(
            step_scope.get("allowed_capability_ids")
        ) if isinstance(step_scope, Mapping) else []
        result, meta = await self._execute_bounded_action_commands(
            raw_commands=raw_commands,
            required_action_ids=self._normalize_string_list(plan.get("required_action_ids")),
            allowed_action_ids=allowed_action_ids,
            execution_id=f"{self.id}:flat:iter-{iteration_index}:action-call",
            code_prefix="agent_task.flat.action_commands",
            execution_kind="flat_bounded_action_calls",
            command_source=command_source,
            action_planning_model_requests=action_planning_model_requests,
            unit_label="Flat step",
            todo_suggestion="Finish this bounded Flat action step after execution.",
            concurrency=1,
            iteration_index=iteration_index,
            project_flat_action_batch=True,
        )
        # This Carrier produces execution status, not a model-consumed final answer.
        # A planned output format does not mean a candidate already exists.
        inventory = self._lifecycle_state.carrier_inventory
        if (
            meta.get("status") == "success"
            and "ready_for_final_verification" not in result
            and str(plan.get("deliverable_mode") or "")
            not in {"task_workspace_artifact", "sectioned_task_workspace_artifact"}
            and not self._candidate_final_result_from_execution_result(result, include_answer=False)
            and "artifact_manifest" not in result
            and not result.get("artifact_refs")
            and not result.get("file_refs")
            and not (inventory and (inventory.inventory_version > 0 or inventory.carriers))
            and not self._lifecycle_state.active_issue
            and not self._lifecycle_state.repair_contract
            and not self._terminal_convergence_state.active_records()
        ):
            result = {**result, "ready_for_final_verification": False}
        return result, meta

    async def _try_flat_narrow_action_command_request(
        self,
        iteration_index: int,
        plan: Mapping[str, Any],
        context_pack: "TaskContextView",
        *,
        scoped_retrieval_results: Sequence[Mapping[str, Any]] | None = None,
        evidence_ledger: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | Literal["requires_observation"] | None:
        """Dispatch ready kwargs, or defer new-observation dependencies to the existing loop."""

        if str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "") != "actions":
            return None
        if plan.get("action_commands") not in (None, [], ()):
            return None
        required_action_ids = self._normalize_string_list(plan.get("required_action_ids"))
        if not required_action_ids:
            return None

        request_context_pack, context_package = await self._read_task_context_view(
            phase="execution",
            consumer_id=f"agent_task:{self.id}:action-planner:iteration:{iteration_index}",
            intent=f"Resolve Action inputs for iteration {iteration_index}: {self.goal}",
        )

        execution_id = f"{self.id}:flat:iter-{iteration_index}:action-call"
        step_scope = plan.get("step_scope")
        allowed_action_ids = self._normalize_string_list(
            step_scope.get("allowed_capability_ids")
        ) if isinstance(step_scope, Mapping) else []
        action_contracts, unavailable_action_id = self._bounded_action_contracts(
            required_action_ids,
            allowed_action_ids=allowed_action_ids,
        )
        if unavailable_action_id is not None:
            return self._bounded_action_command_failure(
                execution_id=execution_id,
                code="agent_task.flat.action_commands.required_action_unavailable",
                message=f"Required Flat Action '{unavailable_action_id}' is unavailable.",
                execution_kind="flat_bounded_action_calls",
                command_source="flat_action_command_request",
                action_planning_model_requests=0,
            )

        request = self.agent.create_temp_request()
        self._bind_task_context_attachments(request, context_package)
        language_policy = self._language_policy()
        self._apply_language_policy_to_request(request, language_policy)
        repair_context = self._active_repair_context()
        input_payload = {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "iteration": iteration_index,
            "bounded_step_plan": DataFormatter.sanitize(dict(plan)),
            "context_pack": DataFormatter.sanitize(request_context_pack),
            "repair_context": DataFormatter.sanitize(repair_context or {}),
        }
        if scoped_retrieval_results:
            input_payload.update({
                "scoped_retrieval_results": DataFormatter.sanitize(list(scoped_retrieval_results)),
                "evidence_ledger": DataFormatter.sanitize(evidence_ledger or {}),
            })
        request.input(input_payload)
        request.info(
            {
                "available_actions": action_contracts,
                "required_action_ids": required_action_ids,
            }
        )
        request.instruct(
            "Resolve Action arguments for [input.bounded_step_plan] using [input.context_pack], "
            "[input.repair_context], and the exact contracts in [info.available_actions]. "
            "[input.repair_context.verification_source] distinguishes intermediate observations from verification feedback; "
            "pending capabilities during ordinary continuation are task obligations, not a failed step. "
            "Actual guard and repair findings still apply. "
            "Assess [output.requires_observation] before constructing [output.action_commands]. "
            "Serial dispatch preserves order but cannot supply a future Action result to this request. "
            "Do not execute Actions, guess missing values, invent placeholders, or synthesize a final response "
            "outside Action inputs."
            + (
                " Use [input.scoped_retrieval_results] with the authoritative ids and states in "
                "[input.evidence_ledger]. Do not infer source content from failed, empty, or ref_only "
                "entries, or beyond truncated excerpts."
                if scoped_retrieval_results else ""
            )
        )
        request.output(
            {
                "requires_observation": (
                    bool,
                    "True if any call in this step needs arguments determined by a not-yet-executed Action's "
                    "result, including repeated calls to the same Action. Return no commands in that case; "
                    "the host will use bounded stepwise execution. False when all arguments can be grounded "
                    "now; ordering alone does not require a new observation.",
                    True,
                ),
                "action_commands": (
                    [
                        {
                            "purpose": (str, "Bounded purpose for this exact Action call.", True),
                            "action_id": (str, "Exact offered Action id.", True),
                            "action_input": (dict, "Complete kwargs for the Action contract.", True),
                        }
                    ],
                    "Empty list when requires_observation is true. Otherwise, the complete batch in execution "
                    "order, including each [info.required_action_ids] entry at least once. Repeat calls only "
                    "for distinct inputs required by this step.",
                    True,
                )
            },
            format="json",
        )
        await self._emit(
            f"agent_task.iteration.{iteration_index}.action_commands.started",
            {
                "iteration": iteration_index,
                "required_action_ids": required_action_ids,
            },
        )
        result_handle = request.get_result()
        raw = await self._await_task_request(result_handle.async_get_data(), stage="execute")
        self._record_task_context_consumption(
            context_package,
            request_id=result_handle.id,
        )
        await self._emit_required_skill_context_bound(
            request_context_pack,
            request_id=result_handle.id,
            phase="work.execute",
        )
        raw_commands = raw.get("action_commands") if isinstance(raw, Mapping) else None
        requires_observation = raw.get("requires_observation") if isinstance(raw, Mapping) else None
        if (
            not isinstance(requires_observation, bool)
            or not isinstance(raw_commands, list)
            or (requires_observation and raw_commands)
        ):
            return self._bounded_action_command_failure(
                execution_id=execution_id,
                code="agent_task.flat.action_commands.invalid_readiness",
                message="Expected a boolean requires_observation and a command list, empty when observation is needed.",
                execution_kind="flat_bounded_action_calls",
                command_source="flat_action_command_request",
                action_planning_model_requests=1,
            )
        if requires_observation:
            return "requires_observation"
        if raw_commands in (None, [], ()):
            return self._bounded_action_command_failure(
                execution_id=execution_id,
                code="agent_task.flat.action_commands.empty_model_result",
                message="The Flat Action command request returned no commands.",
                execution_kind="flat_bounded_action_calls",
                command_source="flat_action_command_request",
                action_planning_model_requests=1,
            )
        return await self._try_flat_preplanned_action_calls(
            iteration_index,
            plan,
            raw_commands_override=raw_commands,
            command_source="flat_action_command_request",
            action_planning_model_requests=1,
        )

    async def _execute_step(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "TaskContextView",
    ) -> tuple[Any, dict[str, Any]]:
        override = self._step_stage_override("_execute_step")
        if override is not None:
            result = override(iteration_index, plan, context_pack)
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result
            return cast(tuple[Any, dict[str, Any]], result)

        plan = self._normalize_step_plan(plan)
        work_unit = self._build_flat_work_unit_intent(iteration_index, plan, context_pack)
        grounding_patch_context = self._flat_grounding_task_workspace_patch_context(
            self._active_repair_context()
        )

        async def run_agent_step(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            if grounding_patch_context:
                return await self._run_flat_grounding_task_workspace_patch_step(
                    iteration_index,
                    plan=plan,
                    patch_context=grounding_patch_context,
                )
            preplanned_action_calls = await self._try_flat_preplanned_action_calls(
                iteration_index,
                plan,
            )
            if preplanned_action_calls is not None:
                direct_result, direct_meta = preplanned_action_calls
                return {
                    "execution_result": DataFormatter.sanitize(direct_result),
                    "execution_meta": DataFormatter.sanitize(direct_meta),
                }
            scoped_retrieval_results = self._scoped_retrieval_results_from_block_context(_context)
            evidence_ledger = self._flat_step_evidence_ledger(_context) if scoped_retrieval_results else None
            scoped_inputs = {
                "scoped_retrieval_results": scoped_retrieval_results,
                "evidence_ledger": evidence_ledger,
            } if scoped_retrieval_results else {}
            narrow_action_commands = await self._try_flat_narrow_action_command_request(
                iteration_index,
                plan,
                context_pack,
                **scoped_inputs,
            )
            if isinstance(narrow_action_commands, tuple):
                direct_result, direct_meta = narrow_action_commands
                return {
                    "execution_result": DataFormatter.sanitize(direct_result),
                    "execution_meta": DataFormatter.sanitize(direct_meta),
                }
            if evidence_ledger is None:
                evidence_ledger = self._flat_step_evidence_ledger(_context)
            execution_result, execution_meta = await self._run_bounded_agent_execution_step(
                iteration_index,
                plan,
                context_pack,
                require_step_actions=narrow_action_commands == "requires_observation",
                carrier_output_policy=self._carrier_output_policy_from_block_context(_context),
                scoped_retrieval_results=scoped_retrieval_results,
                evidence_ledger=evidence_ledger,
            )
            if narrow_action_commands == "requires_observation":
                execution_meta["action_command_planning"] = {
                    "command_source": "flat_action_command_request",
                    "action_planning_model_requests": 1,
                    "requires_observation": True,
                    "command_count": 0,
                }
            return {
                "execution_result": DataFormatter.sanitize(execution_result),
                "execution_meta": DataFormatter.sanitize(execution_meta),
                "scoped_retrieval_results": DataFormatter.sanitize(scoped_retrieval_results),
                "evidence_ledger": DataFormatter.sanitize(evidence_ledger),
            }

        try:
            execution_result, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:iter-{iteration_index}",
                handler=run_agent_step,
                start_payload=(
                    {
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "material_claim_task_workspace_patch": {
                            "path": grounding_patch_context.get("path"),
                            "content_version_id": grounding_patch_context.get("content_version_id"),
                        },
                    }
                    if grounding_patch_context
                    else {
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "plan": DataFormatter.sanitize(plan),
                        "context_pack": DataFormatter.sanitize(context_pack),
                    }
                ),
            )
        except Exception as error:
            result, failed_meta = self._failed_execution_result(
                iteration_index,
                plan=plan,
                error=error,
                execution_id=f"{self.id}:iter-{iteration_index}:blocks-step",
            )
            await self._emit(
                f"agent_task.iteration.{iteration_index}.execution.failed",
                {"execution_meta": failed_meta},
            )
            await self._record_phase(
                "execution_failed",
                iteration=iteration_index,
                diagnostics={
                    "execution_id": failed_meta.get("execution_id"),
                    "route": failed_meta.get("route"),
                    "error": failed_meta.get("diagnostics", {}).get("execution_error"),
                    "work_unit": work_unit.to_dict(),
                },
            )
            return result, failed_meta

        self._reconcile_effective_shape(plan, execution_meta)
        status = str(execution_meta.get("status") or "").strip().lower()
        if status not in {"failed", "error", "timed_out", "blocked"}:
            await self._emit(f"agent_task.iteration.{iteration_index}.execution.completed", execution_meta)
        return execution_result, cast(dict[str, Any], execution_meta)

    @staticmethod
    def _flat_execution_is_grounding_task_workspace_patch(
        execution_meta: Mapping[str, Any],
    ) -> bool:
        diagnostics = execution_meta.get("diagnostics")
        candidates: list[Mapping[str, Any]] = []
        if isinstance(diagnostics, Mapping):
            candidates.append(diagnostics)
        elif isinstance(diagnostics, Sequence) and not isinstance(
            diagnostics,
            str | bytes | bytearray,
        ):
            candidates.extend(item for item in diagnostics if isinstance(item, Mapping))
        return any(
            str(item.get("execution_kind") or "") == "flat_grounding_task_workspace_patch"
            for item in candidates
        )

    def _flat_grounding_task_workspace_patch_context(
        self,
        repair_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(repair_context, Mapping):
            return {}
        grounding_contract = repair_context.get("material_claim_repair_contract")
        if not isinstance(grounding_contract, Mapping):
            return {}
        requirements = self._grounding_patch_mapping_sequence(
            grounding_contract.get("requirements")
        )
        if not requirements:
            return {}
        candidate = self._terminal_carrier_for_repair_contract(grounding_contract)
        if candidate is None or candidate.kind != "task_workspace_artifact":
            return {}
        candidate_path = self._task_workspace_artifact_display_path(candidate.path)
        if not candidate_path:
            return {}
        required_paths = {
            self._task_workspace_artifact_display_path(path)
            for path in self._required_task_workspace_deliverables()
            if self._task_workspace_artifact_display_path(path)
        }
        # A promoted Grounding candidate is already a host-selected file. When
        # the task declares file deliverables, never redirect the repair to a
        # different path supplied by the model.
        if required_paths and candidate_path not in required_paths:
            return {
                "path": candidate_path,
                "content_version_id": candidate.content_version_id,
                "material_claim_repair_contract": DataFormatter.sanitize(dict(grounding_contract)),
                "invalid_reason": (
                    "The promoted grounding candidate is not an authorized required TaskWorkspace deliverable."
                ),
            }
        candidate_version = candidate.content_version_id
        contract_versions = {
            str(item.get("content_version_id") or "").strip()
            for item in requirements
            if str(item.get("content_version_id") or "").strip()
        }
        if not candidate_version or contract_versions != {candidate_version}:
            return {
                "path": candidate_path,
                "content_version_id": candidate_version,
                "material_claim_repair_contract": DataFormatter.sanitize(dict(grounding_contract)),
                "invalid_reason": (
                    "The grounding repair contract does not identify the current promoted artifact content version."
                ),
            }
        return {
            "path": candidate_path,
            "content_version_id": candidate_version,
            "material_claim_repair_contract": DataFormatter.sanitize(dict(grounding_contract)),
        }

    @staticmethod
    def _flat_grounding_task_workspace_patch_output_schema() -> dict[str, Any]:
        return {
            "step_result": (
                str,
                "Concise summary of the bounded grounding repair; no artifact body",
                True,
            ),
            "patch_proposal": (
                {
                    "path": (str, "The one authorized TaskWorkspace artifact path", True),
                    "operations": (
                        [
                            {
                                "claim_key": (
                                    str,
                                    "One exact host-issued material_claim_repair_contract claim_key",
                                    True,
                                ),
                                "op": (Literal["replace"], "Only exact replacement is allowed", True),
                                "old_string": (
                                    str,
                                    "Exact current artifact text wholly within that claim's artifact_quote",
                                    True,
                                ),
                                "new_string": (
                                    str,
                                    "Bounded supported replacement, or exactly empty when repair_policy is delete_only",
                                    True,
                                ),
                            }
                        ],
                        "Exactly one replace operation per offered claim_key",
                        True,
                    ),
                },
                "A claim-scoped TaskWorkspace patch proposal; never a complete artifact body",
                True,
            ),
            "evidence": (
                [str],
                "Short notes identifying the offered evidence used for each replacement",
                False,
            ),
            "remaining_work": (
                [str],
                "Grounding repair work that remains after the proposed replacements",
                False,
            ),
            "ready_for_final_verification": (
                bool,
                "True only when every grounding requirement has one bounded replacement",
                False,
            ),
            "self_check": (
                str,
                "Short check that no unrelated artifact text was changed",
                False,
            ),
            "short_summary": (
                str,
                "Short downstream summary without copying the artifact body",
                False,
            ),
            "progress_message": (
                str,
                "One safe progress sentence without a whole-task completion claim",
                False,
            ),
        }

    async def _run_flat_grounding_task_workspace_patch_step(
        self,
        iteration_index: int,
        *,
        plan: Mapping[str, Any],
        patch_context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        grounding_contract = patch_context.get("material_claim_repair_contract")
        if not isinstance(grounding_contract, Mapping):
            raise ValueError("Flat grounding patch requires a structured grounding repair contract.")
        path = self._task_workspace_artifact_display_path(patch_context.get("path"))
        invalid_reason = str(patch_context.get("invalid_reason") or "").strip()
        if invalid_reason:
            execution_id = f"{self.id}:iter-{iteration_index}:grounding-patch"
            await self._emit(
                f"agent_task.iteration.{iteration_index}.execution.started",
                {
                    "execution_id": execution_id,
                    "step_execution": {
                        "requested_shape": str(plan.get("execution_shape") or "direct"),
                        "effective_shape": "direct",
                        "action_scope_source": "grounding_host_patch",
                    },
                },
            )
            return {
                "execution_result": {
                    "step_result": "Grounding-only TaskWorkspace repair was rejected before model execution.",
                    "task_workspace_patch_delivery": {
                        "status": "failed",
                        "path": path,
                        "reason": invalid_reason,
                    },
                    "file_refs": [],
                    "remaining_work": [invalid_reason],
                    "ready_for_final_verification": False,
                    "diagnostics": [
                        {
                            "code": "agent_task.flat.grounding_task_workspace_patch_contract_invalid",
                            "path": path,
                            "message": invalid_reason,
                            "source": "agent_task.flat.grounding_task_workspace_patch",
                        }
                    ],
                },
                "execution_meta": {
                    "execution_id": execution_id,
                    "status": "blocked",
                    "route": {"selected_route": "model_request", "status": "not_started"},
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [{"message": invalid_reason}],
                    },
                    "diagnostics": [
                        {
                            "execution_kind": "flat_grounding_task_workspace_patch",
                            "execution_strategy": self.execution_strategy,
                            "path": path,
                            "contract_status": "invalid",
                        }
                    ],
                },
            }
        request = self.agent.create_temp_request()
        language_policy = self._language_policy()
        self._apply_language_policy_to_request(request, language_policy)
        repair_context = self._active_repair_context()
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "iteration": iteration_index,
                "authorized_task_workspace_target": {
                    "path": path,
                    "content_version_id": patch_context.get("content_version_id"),
                },
                "available_evidence_anchors": DataFormatter.sanitize(
                    repair_context.get("available_evidence_anchors", {})
                    if isinstance(repair_context, Mapping)
                    else {}
                ),
                "language_policy": language_policy,
            }
        )
        request.info(
            {
                "material_claim_repair_contract": DataFormatter.sanitize(dict(grounding_contract)),
                "patch_contract": {
                    "authorized_operation": "replace",
                    "claim_cardinality": "exactly_one_operation_per_claim_key",
                    "scope": "old_string_within_matching_artifact_quote",
                    "version_policy": "host_validates_content_version_before_write",
                },
            }
        )
        request.instruct(
            "Propose the smallest deterministic repair for the structured grounding requirements. "
            "Return exactly one replace operation for every offered claim_key and copy old_string exactly from that "
            "requirement's artifact_quote. Use only the authorized TaskWorkspace target path. This request cannot acquire "
            "or validate new evidence, so never invent a new fact, readback state, path assertion, or evidence citation. "
            "When repair_policy='delete_only', new_string must be the exactly empty string; a qualifying rewording is not "
            "a deletion. Otherwise new_string may narrow the claim only to what the already offered evidence supports. "
            "Do not call Actions, do not return candidate_final_result, final_result, artifact_markdown, a full-file "
            "body, a full-file rewrite, append/insert/write operations, replace_all, or edits outside the implicated "
            "artifact quotes. The host validates identity, scope, current content version, exact-match cardinality, "
            "applies the patch, and reads the artifact back."
        )
        request.output(self._flat_grounding_task_workspace_patch_output_schema(), format="json")
        execution_id = f"{self.id}:iter-{iteration_index}:grounding-patch"
        await self._emit(
            f"agent_task.iteration.{iteration_index}.execution.started",
            {
                "execution_id": execution_id,
                "step_execution": {
                    "requested_shape": str(plan.get("execution_shape") or "direct"),
                    "effective_shape": "direct",
                    "action_scope_source": "grounding_host_patch",
                },
            },
        )
        output = await self._await_task_request(request.async_get_data(), stage="execute")
        result: dict[str, Any] = (
            dict(output) if isinstance(output, Mapping) else {"step_result": str(output or "")}
        )
        raw_patch = result.get("patch_proposal")
        delivery = (
            await self._apply_grounding_task_workspace_patch(
                raw_patch,
                grounding_contract,
                allowed_patch_paths=[path],
                source=f"agent_task.iteration.{iteration_index}.grounding_task_workspace_patch",
            )
            if isinstance(raw_patch, Mapping)
            else {
                "status": "failed",
                "path": path,
                "reason": "Grounding repair request returned no structured patch_proposal.",
            }
        )
        result["task_workspace_patch_proposal"] = DataFormatter.sanitize(raw_patch or {})
        result.pop("patch_proposal", None)
        result["task_workspace_patch_delivery"] = DataFormatter.sanitize(delivery)
        diagnostics = self._grounding_patch_mapping_sequence(result.get("diagnostics"))
        execution_meta: dict[str, Any] = {
            "execution_id": execution_id,
            "status": "completed" if delivery.get("status") == "completed" else "blocked",
            "route": {"selected_route": "model_request", "status": "completed"},
            "logs": {"action_logs": {}, "route_logs": {}, "errors": []},
            "diagnostics": [
                {
                    "execution_kind": "flat_grounding_task_workspace_patch",
                    "execution_strategy": self.execution_strategy,
                    "path": path,
                    "base_content_version_id": patch_context.get("content_version_id"),
                    "result_content_version_id": delivery.get("content_version_id"),
                }
            ],
        }
        if delivery.get("status") == "completed":
            refs = [
                dict(item)
                for item in self._grounding_patch_mapping_sequence(delivery.get("file_refs"))
            ]
            result["file_refs"] = DataFormatter.sanitize(refs)
            result["remaining_work"] = []
            result["ready_for_final_verification"] = True
            diagnostics.append(
                {
                    "code": "agent_task.flat.grounding_task_workspace_patch_applied",
                    "path": path,
                    "operation_count": delivery.get("operation_count", 0),
                    "source": "agent_task.flat.grounding_task_workspace_patch",
                }
            )
            self._append_task_workspace_artifact_meta(execution_meta, refs)
        else:
            reason = str(delivery.get("reason") or "Grounding TaskWorkspace patch could not be applied.").strip()
            result["file_refs"] = []
            result["remaining_work"] = [reason]
            result["ready_for_final_verification"] = False
            diagnostics.append(
                {
                    "code": "agent_task.flat.grounding_task_workspace_patch_failed",
                    "path": path,
                    "message": reason,
                    "source": "agent_task.flat.grounding_task_workspace_patch",
                }
            )
            execution_meta["logs"]["errors"].append({"message": reason})
        result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return {
            "execution_result": DataFormatter.sanitize(result),
            "execution_meta": DataFormatter.sanitize(execution_meta),
        }

    async def _run_bounded_agent_execution_step(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "TaskContextView",
        *,
        require_step_actions: bool = False,
        carrier_output_policy: Mapping[str, Any] | None = None,
        scoped_retrieval_results: Sequence[Mapping[str, Any]] | None = None,
        evidence_ledger: Mapping[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        plan = self._normalize_step_plan(plan)
        repair_context = self._active_repair_context()
        execution = self._create_bounded_child_execution(
            lineage={
                "task_id": self.id,
                "iteration_id": f"iter-{iteration_index}",
                "step_id": "execute",
                "scope": {"strategy_phase": "agent_task_execution_step"},
            },
        )
        step_execution = self._configure_step_execution(execution, plan)
        if require_step_actions:
            # A deferred command batch retains its required-call evidence contract,
            # not merely the candidate visibility of an ordinary planned step.
            execution.require_actions(self._normalize_string_list(plan.get("required_action_ids")))
            # New-observation steps may need multiple calls plus final synthesis.
            # Keep explicit task bounds, not the generic two-round convenience cap.
            supplied, max_rounds = self._explicit_task_action_loop_max_rounds()
            execution.request.settings.set("action.loop.max_rounds", max_rounds if supplied else None)
            execution.request.settings.set("tool.loop.max_rounds", max_rounds if supplied else None)
        language_policy = self._language_policy()
        input_payload = {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "task_context_contract": self._task_context_contract_for_model_prompt(),
            "iteration": iteration_index,
            "plan": DataFormatter.sanitize(plan),
            "step_execution": step_execution,
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "context_pack": DataFormatter.sanitize(context_pack),
            "execution_prompt": self._execution_prompt_context(),
            "retrieval_policy": self._task_context_retrieval_policy(),
            "scoped_retrieval": DataFormatter.sanitize(plan.get("scoped_retrieval", {})),
            "evidence_ledger": DataFormatter.sanitize(evidence_ledger or {}),
            "scoped_retrieval_results": DataFormatter.sanitize(list(scoped_retrieval_results or ())),
            "language_policy": language_policy,
        }
        if repair_context:
            input_payload["repair_context"] = DataFormatter.sanitize(repair_context)
        try:
            result, meta = await self._run_bounded_child_execution(
                execution=execution,
                language_policy=language_policy,
                input_payload=input_payload,
                instruction=(
                    "Execute exactly one bounded step for the AgentTask. "
                    f"Use the selected execution shape: {step_execution.get('effective_shape', 'direct')}. "
                    f"The AgentTask requested execution_strategy is {self.execution_strategy}; "
                    f"the effective execution_strategy is {self.effective_execution_strategy or self.execution_strategy}. "
                    "Respect the caller-provided execution_prompt context and output contract when present. "
                    "Use task_context_contract for prompt-safe temporal policy; do not infer or write a current "
                    "date/time as a business fact unless it appears in task facts or source evidence. "
                    "keep downloads, web snapshots, notes, generated code, and large extracted text as refs until scoped "
                    "readback is needed. "
                    "Preserve task-provided facts and ground factual claims in available evidence, including additions "
                    "beyond the success criteria. Distinguish evidence-derived analysis from reported facts; preserve the "
                    "evidence's scope, uncertainty, and pending states. Provide the content required by the task; "
                    "unresolved required content remains a gap. Templates or intentionally open fields are valid when the "
                    "task requests them. "
                    "Return concrete evidence for the verifier. If this step produces the requested final answer, report, "
                    "file body, or artifact body, put the complete candidate deliverable in candidate_final_result instead "
                    "of burying the only copy inside evidence when it fits the bounded output. If the plan deliverable_mode "
                    "is task_workspace_artifact or sectioned_task_workspace_artifact, return either a complete bounded body in "
                    "artifact_markdown when it fits, or an artifact_manifest with path and section outline as the structured "
                    "deliverable contract for long or multi-section deliverables. Do not put the full long body in "
                    "artifact_manifest section content, answer, candidate_final_result, or final_result. Do not self-declare "
                    "trusted file_refs. Return acceptance_points with expected headings or exact anchors for "
                    "critical artifact verification points; do not invent line numbers or trusted file refs. "
                    "Do not invent file_refs for deliverables. "
                    "For web-source steps, treat Search results as discovery hints only. Browse official pages and follow "
                    "same-site index/list/download/navigation links before relying on a broad announcement page as the "
                    "source boundary. "
                    "For repository or file-source steps, a clone/list manifest path is ref_only; read the specific "
                    "file or artifact before making claims about its content. "
                    "When scoped_retrieval.query_groups is present, try the prioritized scoped ContextReader search before broad "
                    "reads; use evidence_snippet results as bounded source text and locator_ref results only as targets "
                    "for later bounded readback. If scoped_retrieval_results is present, those are already executed "
                    "Blocks context_read facts for the current step; inspect them before choosing broader reads. "
                    "Treat evidence_ledger as the authoritative grounding ledger for item ids, cite handles, status, "
                    "body_state, and grounding rules. Use its item ids in evidence_use for factual claims. Current-step "
                    "retrieval excerpt text is carried by scoped_retrieval_results.evidence_snippets, while cold "
                    "provenance and larger bodies stay outside the synthesis prompt. status=failed or status=empty is "
                    "evidence of unavailability only, not support for positive business facts. body_state=ref_only "
                    "supports only discovery/ref-pointer claims; read the referenced source before asserting its "
                    "content. body_state=truncated supports only the visible excerpt, not whole-source or exhaustive "
                    "claims. scoped_retrieval_results is a compatibility view derived from the same ledger and is not a "
                    "separate grounding authority. "
                    "Do not treat a retrieval hit as semantic acceptance by itself. "
                    "[input.repair_context.verification_source] distinguishes intermediate observations from verification feedback; "
                    "pending capabilities during ordinary continuation are task obligations, not a failed step. "
                    "Actual guard and repair findings still apply. "
                    "Use its acceptance_delta, advisory_repair_constraints, advisory_next_step_requirements, and "
                    "available_evidence_anchors as work-unit guidance; do not rely on the planner restating every "
                    "repair fact in step_instruction. "
                    "If material_claim_repair_contract is present, consume its structured claim requirements directly and "
                    "do not infer them from reason or other prose fields. "
                    "Do not claim final completion unless evidence supports it. "
                    "Use remaining_work for task-level work that the next Flat iteration should consume or perform. "
                    "Non-empty remaining_work defaults to intermediate and skips terminal verification for this work "
                    "unit. Set ready_for_final_verification=false to make that explicit, or true only when this work "
                    "unit intentionally needs terminal, blocking, or risk verification now."
                    + self._bounded_step_carrier_instruction(carrier_output_policy)
                ),
                output_schema=self._bounded_step_output_schema(carrier_output_policy),
                output_format=self._carrier_control_output_format(carrier_output_policy),
                use_output=self._carrier_uses_control_output(carrier_output_policy),
                carrier_output_policy=carrier_output_policy,
                started_event=f"agent_task.iteration.{iteration_index}.execution.started",
                started_payload={"step_execution": step_execution},
                stream_bridge=lambda child_execution: self._bridge_step_execution_stream(
                    iteration_index,
                    child_execution,
                    execution_shape=str(step_execution.get("effective_shape") or ""),
                ),
            )
        except Exception as error:
            child_meta = await self._read_child_execution_meta(execution)
            result, failed_meta = self._failed_execution_result(
                iteration_index,
                plan=plan,
                error=error,
                execution_id=str(getattr(execution, "id", "") or "") or None,
                child_meta=child_meta,
            )
            await self._emit(
                f"agent_task.iteration.{iteration_index}.execution.failed",
                {"execution_meta": failed_meta},
            )
            await self._record_phase(
                "execution_failed",
                iteration=iteration_index,
                diagnostics={
                    "execution_id": failed_meta.get("execution_id"),
                    "route": failed_meta.get("route"),
                    "error": failed_meta.get("diagnostics", {}).get("execution_error"),
                },
            )
            return result, failed_meta
        return result, cast(dict[str, Any], meta)

    async def _record_timed_out_verification_iteration(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        context_pack: "TaskContextView",
        decision_ref: "RecordRef",
        execution_meta: dict[str, Any],
        observation_ref: "RecordRef",
        step_reflection_ref: "RecordRef | None",
        error: _AgentTaskDeadlineExceeded,
    ) -> None:
        if any(record.get("iteration") == iteration_index for record in self.iterations):
            return
        verification = {
            "is_complete": False,
            "requires_block": False,
            "reason": error.reason or str(error),
            "missing_criteria": ["Verification timed out before completion could be judged."],
            "failure_analysis": "The verify stage hit the task no-progress or wall-clock guard.",
            "acceptance_delta": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": "",
            "guard_reasons": ["verify_timeout"],
        }
        verification_ref = await self._record_verification(iteration_index, verification, observation_ref)
        iteration_record = {
            "iteration": iteration_index,
            "plan": plan,
            "decision_ref": decision_ref,
            "execution_meta": execution_meta,
            "observation_ref": observation_ref,
            "verification": verification,
            "verification_ref": verification_ref,
            "verification_source": "verify_timeout_guard",
            "reflection_refs": [ref for ref in (step_reflection_ref,) if ref is not None],
            "context_item_count": len(context_pack.get("items", [])),
            "process_summary": self._combined_process_summary(
                plan=plan,
                execution_result={},
                verification=verification,
            ),
        }
        self.iterations.append(DataFormatter.sanitize(iteration_record))
        await self._write_resume_snapshot(iteration_index, verification)

    @staticmethod
    def _bounded_step_output_schema(carrier_output_policy: Mapping[str, Any] | None) -> dict[str, Any]:
        if (
            isinstance(carrier_output_policy, Mapping)
            and str(carrier_output_policy.get("body_transport") or "") == "task_workspace_artifact"
            and carrier_output_policy.get("body_uses_output") is False
        ):
            return {
                "step_result": (
                    str,
                    "Concise status and evidence summary for this bounded step; do not include the artifact body",
                    True,
                ),
                "artifact_manifest": (
                    dict,
                    "Optional TaskWorkspace artifact manifest with path and section outline only; no full body content and no file_refs",
                    False,
                ),
                "evidence": (
                    [str],
                    "Optional model-visible evidence notes; Action and TaskWorkspace ledger records remain the trusted evidence source",
                    False,
                ),
                "remaining_work": (
                    [str],
                    "Task-level remaining work for the next Flat iteration; non-empty values skip terminal verification unless ready_for_final_verification is explicitly true",
                ),
                "ready_for_final_verification": (
                    bool,
                    "False when the next Flat iteration should consume this output before terminal verification; true only for terminal, blocking, or risk verification now",
                    False,
                ),
                "evidence_use": (
                    [dict],
                    "Claim bindings: [{claim, evidence_ids, support_type}], where evidence_ids contains only offered stable reference_id values and support_type is content, unavailability, or ref_pointer; for file/section claims select the bounded readback reference, never a free-text locator label",
                    False,
                ),
                "acceptance_points": (
                    [dict],
                    "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}]",
                    False,
                ),
                "self_check": (
                    str,
                    "Short post-step self check of what is still uncertain; no new facts beyond the step output.",
                    False,
                ),
                "short_summary": (
                    str,
                    "Short summary for the next AgentTask step; do not include full artifact bodies.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable progress sentence; do not claim completion or verification.",
                    False,
                ),
            }
        return {
            "step_result": (str, "Concrete result of this bounded step", True),
            "candidate_final_result": (
                str,
                "Complete answer/report/artifact body produced by this step when it may satisfy the final task",
                False,
            ),
            "artifact_markdown": (
                str,
                "Short markdown deliverable body when this step creates one and it fits bounded output",
                False,
            ),
            "artifact_manifest": (
                dict,
                "TaskWorkspace artifact manifest for file-backed or sectioned deliverables",
                False,
            ),
            "file_refs": (
                [dict],
                "Existing evidence refs only; deliverable refs are trusted only when backed by verifier-visible TaskWorkspace/readback evidence",
                False,
            ),
            "evidence": ([str], "Evidence produced by the step", True),
            "remaining_work": (
                [str],
                "Task-level remaining work for the next Flat iteration; non-empty values skip terminal verification unless ready_for_final_verification is explicitly true",
            ),
            "ready_for_final_verification": (
                bool,
                "False when the next Flat iteration should consume this output before terminal verification; true only for terminal, blocking, or risk verification now",
                False,
            ),
            "evidence_use": (
                [dict],
                "Claim bindings: [{claim, evidence_ids, support_type}], where evidence_ids contains only offered stable reference_id values and support_type is content, unavailability, or ref_pointer; for file/section claims select the bounded readback reference, never a free-text locator label",
                False,
            ),
            "acceptance_points": (
                [dict],
                "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}]",
                False,
            ),
            "self_check": (
                str,
                "Short post-step self check of what is still uncertain; no new facts beyond the step output.",
                False,
            ),
            "short_summary": (
                str,
                "Short summary for the next AgentTask step; do not include full artifact bodies.",
                False,
            ),
            "progress_message": (
                str,
                "One safe human-readable progress sentence; do not claim completion or verification.",
                False,
            ),
        }

    @staticmethod
    def _bounded_step_carrier_instruction(carrier_output_policy: Mapping[str, Any] | None) -> str:
        if (
            isinstance(carrier_output_policy, Mapping)
            and str(carrier_output_policy.get("body_transport") or "") == "task_workspace_artifact"
            and carrier_output_policy.get("body_uses_output") is False
        ):
            return (
                " This work unit uses a TaskWorkspace artifact carrier: return compact control data only. "
                "Use artifact_manifest for the target path and section outline when the artifact is ready, "
                "and keep the full prose body out of structured output fields."
            )
        return ""

    async def _bridge_step_execution_stream(
        self,
        iteration_index: int,
        execution: Any,
        *,
        execution_shape: str | None = None,
    ) -> None:
        try:
            async for stream_record in execution.get_async_generator(type="all"):
                if isinstance(stream_record, tuple) and len(stream_record) == 2:
                    _, item = stream_record
                else:
                    item = stream_record
                await self._emit_step_execution_stream_item(
                    iteration_index,
                    execution,
                    item,
                    execution_shape=execution_shape,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.diagnostics.setdefault("stream_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "iteration": iteration_index,
                    "stage": "execution",
                    "child_execution_id": str(getattr(execution, "id", "") or ""),
                }
            )

    async def _emit_step_execution_stream_item(
        self,
        iteration_index: int,
        execution: Any,
        item: Any,
        *,
        execution_shape: str | None = None,
    ) -> AgentExecutionStreamData:
        raw_path = str(getattr(item, "path", "") or "stream")
        event_type: Literal["delta", "done"] = "delta" if getattr(item, "event_type", None) == "delta" else "done"
        delta = None if self._is_process_summary_stream_path(raw_path) else getattr(item, "delta", None)
        item_meta = getattr(item, "meta", None)
        meta: dict[str, Any] = {
            "task_id": self.id,
            "status": self.status,
            "iteration": iteration_index,
            "stage": "execution",
            "stream_kind": "child_execution",
            "child_execution_id": str(getattr(execution, "id", "") or ""),
            "child_path": raw_path,
            "child_source": str(getattr(item, "source", "") or ""),
            "child_route": str(getattr(item, "route", "") or ""),
        }
        if isinstance(item_meta, Mapping):
            meta["child_meta"] = DataFormatter.sanitize(dict(item_meta))
        item_value = getattr(item, "value", None)
        if (
            str(execution_shape or "").strip().lower() == "actions"
            and isinstance(item_meta, Mapping)
            and item_meta.get("stream_kind") == "runtime_progress"
            and isinstance(item_value, Mapping)
            and item_value.get("stage") == "action_observation"
        ):
            progress_meta = item_value.get("meta")
            observation = (
                progress_meta.get("action_observation")
                if isinstance(progress_meta, Mapping)
                else None
            )
            if isinstance(observation, Mapping):
                await self._project_live_action_observation(
                    iteration_index,
                    observation,
                    child_execution_id=str(getattr(execution, "id", "") or ""),
                )
        return await self._emit(
            f"agent_task.iteration.{iteration_index}.execution.{raw_path}",
            item_value,
            event_type=event_type,
            delta=delta,
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            meta=meta,
        )

    def _step_stage_override(self, stage_name: str):
        overrides = getattr(self, "_agent_task_step_overrides", None)
        if not isinstance(overrides, dict):
            return None
        handler = overrides.get(stage_name)
        return handler if callable(handler) else None

    def _record_failed_execution_shape(self, plan: dict[str, Any], execution_meta: dict[str, Any]) -> None:
        route = execution_meta.get("route", {})
        route_name = ""
        if isinstance(route, dict):
            route_name = str(route.get("selected_route") or "")
        shape = self._shape_for_route(route_name) if route_name else ""
        if not shape:
            shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "")
        shape = self._normalize_step_execution_shape(shape)
        if shape:
            self._failed_execution_shapes.add(shape)


__all__ = ["AgentTaskFlatStrategyMixin"]
