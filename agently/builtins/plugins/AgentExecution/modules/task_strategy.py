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

from collections.abc import Mapping, Sequence
from typing import Any, TYPE_CHECKING, cast

from agently.utils import DataFormatter
from agently.utils.LanguagePolicy import language_policy_from_prompt_snapshot

from .runtime_guidance import drain_pending_guidance_to_task
from .limits import execution_wall_clock_limits
from .goal_preparation import PreparedGoal, prepare_missing_goal, retain_prepared_goal

if TYPE_CHECKING:
    from .execution import AgentExecution


def _required_skill_block_reason(plan_summary: Mapping[str, Any]) -> str:
    reasons: list[str] = []
    for key in ("rejected_skills", "rejected_skills_packs"):
        items = plan_summary.get(key)
        if not isinstance(items, Sequence) or isinstance(items, str | bytes | bytearray):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            reason = str(item.get("reason") or "").strip()
            if reason and reason not in reasons:
                reasons.append(reason)
    if not reasons:
        diagnostics = plan_summary.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
            for item in diagnostics:
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("level") or "").lower() not in {"warning", "error"}:
                    continue
                message = str(item.get("message") or "").strip()
                if message and message not in reasons:
                    reasons.append(message)
    detail = "; ".join(reasons) if reasons else "required Skill resolution returned no usable installed Skill"
    return f"Required Skill availability check failed: {detail}"


async def _resolve_required_skill_availability(
    execution: "AgentExecution",
    *,
    goal: str,
) -> tuple[list[str], dict[str, Any] | None]:
    del goal
    bindings = [
        binding
        for binding in execution.skill_bindings
        if str(getattr(binding, "mode", "")) == "required"
    ]
    configured_required_ids = execution.required_skill_ids()
    selected_skill_ids: list[str] = []
    selected_identity_keys: set[str] = set()
    installations: list[dict[str, Any]] = []
    library = execution.skill_library
    if bindings and library is None:
        plan_summary = {
            "schema_version": "agently.skill_binding_plan.v2",
            "mode": "required",
            "status": "blocked",
            "selected_skill_ids": [],
            "binding_ids": [],
            "installations": [],
            "missing_skill_ids": configured_required_ids,
            "diagnostics": [
                {
                    "code": "required_skill_library_unavailable",
                    "level": "error",
                    "message": "Required Skill bindings have no active SkillLibrary.",
                }
            ],
        }
        execution.logs["required_skills_plan"] = plan_summary
        execution.diagnostics["required_skills_plan"] = plan_summary
        return [], plan_summary
    for binding in bindings:
        package = cast(Any, library).resolve(binding.revision_ref)
        selected_skill_ids.append(package.skill_id)
        selected_identity_keys.update(
            {
                str(package.skill_id),
                str(package.canonical_ref),
                str(package.revision_ref),
            }
        )
        installations.append(
            {
                "binding_id": binding.binding_id,
                "canonical_skill_id": package.skill_id,
                "canonical_ref": package.canonical_ref,
                "revision": package.revision,
                "revision_ref": package.revision_ref,
                "mode": binding.mode,
            }
        )
    selected_skill_ids = list(dict.fromkeys(selected_skill_ids))
    missing = [
        item for item in configured_required_ids if item not in selected_identity_keys
    ]
    plan_summary = DataFormatter.sanitize(
        {
            "schema_version": "agently.skill_binding_plan.v2",
            "mode": "required",
            "status": "blocked" if missing else "resolved",
            "selected_skill_ids": selected_skill_ids,
            "binding_ids": [item["binding_id"] for item in installations],
            "installations": installations,
            "missing_skill_ids": missing,
            "diagnostics": (
                [
                    {
                        "code": "required_skill_binding_missing",
                        "missing_skill_ids": missing,
                    }
                ]
                if missing
                else []
            ),
        }
    )
    execution.logs["required_skills_plan"] = plan_summary
    execution.diagnostics["required_skills_plan"] = plan_summary
    status = "blocked" if missing else "completed"
    await execution.emit_stream(
        "skills.binding.ready",
        {
            "execution_id": execution.id,
            "skill_ids": selected_skill_ids,
            "revision_refs": [item["revision_ref"] for item in installations],
            "binding_ids": [item["binding_id"] for item in installations],
            "status": status,
        },
        route="agent_task",
        source="skill_library",
        meta={"status": status, "mode": "required"},
    )
    if missing:
        return [], plan_summary
    return selected_skill_ids, None


async def run_agent_task_route(execution: "AgentExecution", route_meta: dict[str, Any]) -> Any:
    """Use the unified producer; explicit 4.1 strategies retain their migration path."""
    options = execution.task_strategy_options()
    legacy = (execution.strategy_name in {"task", "task_loop"}
              or options.get("execution") in {"flat", "taskboard"}
              or execution.task_record is not None
              or options.get("resume") or options.get("resume_task_id") is not None
              or bool(getattr(execution, "_agent_task_step_overrides", None)))
    if not legacy and execution.limits.get("allow_create_task") is not False:
        from .task_loop import run_task_loop
        return await run_task_loop(execution)
    return await _run_agent_task_route_impl(execution, route_meta)


async def _run_agent_task_route_impl(
    execution: "AgentExecution",
    route_meta: dict[str, Any],
) -> Any:
    from ..long_task import AgentTask

    if execution.limits.get("allow_create_task") is False:
        reason = "AgentExecution limits disallow task creation (allow_create_task=False)."
        execution.status = "blocked"
        execution.close_snapshot = {"status": "blocked", "route": "agent_task", "reason": reason}
        execution.diagnostics.setdefault("limit_events", []).append(
            {"limit_name": "allow_create_task", "limit_value": False, "reason": reason}
        )
        await execution.emit_stream(
            "route.agent_task.blocked",
            {"reason": reason, "limit_name": "allow_create_task"},
            route="agent_task",
            source="agent_execution",
            meta={"status": "blocked"},
        )
        return {
            "status": "blocked",
            "accepted": False,
            "artifact_status": "blocked",
            "reason": reason,
            "final_response": (
                "Task encountered a blocking condition. "
                f"No complete final deliverable was accepted. Reason: {reason}"
            ),
        }

    task_options = execution.task_strategy_options()
    resume_task_id = task_options.get("resume_task_id")
    if resume_task_id is None and task_options.get("resume"):
        resume_task_id = task_options.get("task_id") or execution.lineage.get("task_id")
    task = getattr(execution, "task_record", None)
    if not isinstance(task, AgentTask) and resume_task_id is not None:
        task = await AgentTask.async_resume(
            execution.agent,
            str(resume_task_id),
            task_workspace=cast(Any, task_options.get("task_workspace")),
            record_store=execution.record_store,
        )
        execution.task_record = task

    resolved_required_skills, required_skill_failure = await _resolve_required_skill_availability(
        execution,
        goal=task.goal if isinstance(task, AgentTask) else execution.task_goal(),
    )
    if required_skill_failure is not None:
        reason = _required_skill_block_reason(required_skill_failure)
        required_capabilities = {"skills_plan": required_skill_failure}
        execution.status = "blocked"
        execution.close_snapshot = {
            "status": "blocked",
            "route": "agent_task",
            "reason": reason,
            "required_capabilities": required_capabilities,
        }
        await execution.emit_stream(
            "route.agent_task.blocked",
            {
                "reason": reason,
                "required_capabilities": required_capabilities,
            },
            route="agent_task",
            source="skill_library",
            meta={"status": "blocked"},
        )
        return {
            "status": "blocked",
            "accepted": False,
            "artifact_status": "blocked",
            "reason": reason,
            "final_response": (
                "Task encountered a blocking condition. "
                f"No complete final deliverable was accepted. Reason: {reason}"
            ),
            "required_capabilities": required_capabilities,
        }

    if isinstance(task, AgentTask):
        goal = task.goal
        success_criteria = list(task.success_criteria)
        execution_strategy = task.execution_strategy
        retained_goal = task.options.get("goal_preparation")
        if retained_goal is not None:
            retain_prepared_goal(execution, PreparedGoal.from_record(retained_goal))
    else:
        execution_strategy = AgentTask.normalize_execution_strategy(task_options.get("execution", "auto"))
        blocked = await prepare_missing_goal(execution)
        if blocked is not None:
            return blocked
        goal = execution.task_goal()
        success_criteria = execution.task_success_criteria()

    effort_strategy = execution.effective_options.get("effort_strategy")
    effort_strategy = dict(effort_strategy) if isinstance(effort_strategy, dict) else {}
    max_iterations = task_options.get("max_iterations")
    agent_task_options = dict(task_options.get("options") or {})
    if execution._prepared_goal is not None:
        agent_task_options["goal_preparation"] = execution._prepared_goal.to_record()
    if effort_strategy:
        agent_task_options.setdefault("agent_task", {})
        if isinstance(agent_task_options["agent_task"], dict):
            agent_task_options["agent_task"].setdefault("effort", effort_strategy)
    agent_task_options.setdefault("agent_task", {})
    if isinstance(agent_task_options["agent_task"], dict):
        agent_task_options["agent_task"]["execution_strategy"] = execution_strategy
        source = task_options.get("_execution_strategy_source")
        if source is not None:
            agent_task_options["agent_task"]["execution_strategy_source"] = str(source)
    required_actions = execution.required_action_ids()
    # This route already resolved root defaults or the bounded step's local
    # obligations. Task construction must not collect Agent defaults again.
    agent_task_options["_required_actions_bound"] = True
    required_skills = resolved_required_skills or execution.required_skill_ids()
    if required_actions or required_skills:
        constraints = dict(agent_task_options.get("capability_constraints") or {})
        if required_actions:
            constraints.setdefault("actions", {})
            if isinstance(constraints["actions"], dict):
                constraints["actions"]["required"] = required_actions
        if required_skills:
            constraints.setdefault("skills", {})
            if isinstance(constraints["skills"], dict):
                constraints["skills"]["required"] = required_skills
        agent_task_options["capability_constraints"] = constraints
    if execution.skill_bindings:
        library = execution.skill_library
        if library is None:
            raise RuntimeError("Selected Skill bindings require an active SkillLibrary.")
        skill_bindings: list[dict[str, Any]] = []
        for binding in execution.skill_bindings:
            package = library.resolve(binding.revision_ref)
            skill_bindings.append(
                {
                    "binding_id": binding.binding_id,
                    "task_id": binding.task_id,
                    "canonical_skill_id": package.skill_id,
                    "canonical_ref": binding.canonical_ref,
                    "revision": binding.revision,
                    "revision_ref": binding.revision_ref,
                    "resolved_revision": binding.revision_ref,
                    "mode": binding.mode,
                    "scope": binding.scope,
                }
            )
        agent_task_options["skill_bindings"] = DataFormatter.sanitize(skill_bindings)
    # Planner capability visibility: adapt executable Action candidates into one
    # sanitized, inert capability snapshot and pass it into
    # AgentTask options. AgentTask reads only this snapshot; it never imports
    # HybridRoutePlanner or holds the execution draft. Computed once here, at task
    # construction, from the top-level routing execution. A caller-supplied
    # snapshot, if any, wins.
    if "planner_capabilities" not in agent_task_options:
        capability_snapshot = _planner_capability_snapshot(execution)
        if capability_snapshot:
            agent_task_options["planner_capabilities"] = capability_snapshot
    prompt_snapshot = getattr(execution, "prompt_snapshot", {})
    if isinstance(prompt_snapshot, dict) and prompt_snapshot:
        agent_task_options.setdefault(
            "execution_prompt_snapshot",
            DataFormatter.sanitize(dict(prompt_snapshot)),
        )
        language_policy = language_policy_from_prompt_snapshot(prompt_snapshot)
        if language_policy is not None:
            agent_task_options.setdefault("language_policy", dict(language_policy))

    if not isinstance(task, AgentTask):
        task = AgentTask(
            execution.agent,
            goal=goal,
            success_criteria=success_criteria,
            execution=execution_strategy,
            # AgentExecution owns the route and hands its exact scoped view to
            # AgentTask. AgentTask derives a descendant instead of rebinding
            # from the Agent-wide TaskWorkspace or inferring a filesystem path.
            record_store=execution.record_store,
            task_context=execution.task_context,
            task_workspace=execution.task_workspace,
            max_iterations=AgentTask.normalize_max_iterations(max_iterations),
            verify=cast(Any, task_options.get("verify", "before_done")),
            context_profile=str(task_options.get("context_profile", "auto")),
            context_budget=cast(Any, task_options.get("context_budget")),
            limits=cast(Any, task_options.get("limits", execution.limits)),
            options=cast(Any, agent_task_options),
            task_id=cast(Any, task_options.get("task_id") or execution.lineage.get("task_id")),
        )
    # Keep the original execution deadline across prerequisite preparation and
    # task construction. This process-local bound is not a recovery option.
    max_seconds = execution.limits.get("max_seconds")
    if execution.revision:
        max_seconds, _ = execution_wall_clock_limits(execution)
    task._execution_deadline_monotonic = (
        execution.execution_context.started_at + float(max_seconds) if max_seconds is not None else None
    )
    # This is the exact host-owned transfer seam: a routed task keeps its
    # agent_task Action artifact scope live until the parent AgentExecution has
    # completed terminal selection/promotion and releases it. Standalone tasks
    # never receive this transfer marker and clean up in AgentTask finalization.
    task._action_artifact_scope_transferred_to_execution_id = execution.id
    # Advanced/test step-stage override channel. Callers may set an explicit
    # `execution._agent_task_step_overrides = {"_request_plan": ..., ...}` before
    # running to drive the plan/execute/verify stages deterministically. This is
    # an intentional, documented seam (not a public API): only the named stage
    # handlers are applied, and nothing is read in normal goal-pursuit runs.
    step_overrides = getattr(execution, "_agent_task_step_overrides", None)
    if isinstance(step_overrides, dict):
        for stage_name in ("_request_plan", "_execute_step", "_request_verification"):
            handler = step_overrides.get(stage_name)
            if callable(handler):
                setattr(task, stage_name, handler)
    execution.task_record = task
    await drain_pending_guidance_to_task(execution, task)
    required_skill_plan = execution.logs.get("required_skills_plan", {})
    required_skill_plan = required_skill_plan if isinstance(required_skill_plan, Mapping) else {}
    if resolved_required_skills:
        await execution.emit_stream(
            "skills.revisions.bound",
            {
                "execution_id": execution.id,
                "task_id": task.id,
                "binding_ids": list(required_skill_plan.get("binding_ids") or []),
                "canonical_skill_ids": list(resolved_required_skills),
                "mode": "required",
                "policy_status": "resolved",
            },
            route="agent_task",
            source="skill_library",
            task_id=task.id,
            meta={"status": "bound", "mode": "required"},
        )
    execution.task_refs = {
        "task_id": task.id,
        "strategy": route_meta.get("strategy") or execution.strategy_name or "task",
        "execution_strategy": task.execution_strategy,
        "effective_execution_strategy": task.effective_execution_strategy,
        "resume": bool(resume_task_id is not None or task_options.get("resume")),
        "resumed_from_iteration": getattr(task, "_resumed_from_iteration", 0),
    }
    await execution.emit_stream(
        "agent_task.created",
        {
            "task_id": task.id,
            "goal": goal,
            "success_criteria": success_criteria,
            "execution_strategy": task.execution_strategy,
            "effective_execution_strategy": task.effective_execution_strategy,
        },
        route="agent_task",
        source="agent_execution",
        task_id=task.id,
    )

    async for item in task.get_async_generator(type="instant"):
        await execution.bridge_agent_task_stream_item(item, route="agent_task")

    task_meta = await task.async_meta()
    execution._terminal_task_handoff_refs = [
        dict(ref) for ref in list(getattr(task, "_terminal_deliverable_refs", []) or []) if isinstance(ref, Mapping)
    ]
    execution.task_refs.update(
        {
            "status": task.status,
            "execution_strategy": task.execution_strategy,
            "effective_execution_strategy": task_meta.get("effective_execution_strategy"),
            "task_shape_analysis": task_meta.get("task_shape_analysis"),
            "record_refs": task_meta.get("record_refs", {}),
        }
    )
    execution.logs["route_logs"] = {"agent_task": task_meta}
    execution.close_snapshot = {
        "status": task.status,
        "route": "agent_task",
        "task": task_meta,
    }
    if isinstance(task_meta.get("record_refs"), dict):
        execution.record_refs["agent_task"] = task_meta["record_refs"]
    execution.status = "success" if task.status == "completed" else str(task.status)
    from ..long_task.Rework import retain_task_production
    retain_task_production(execution)
    return task.result


def _planner_capability_snapshot(
    execution: "AgentExecution",
) -> list[dict[str, Any]]:
    """Sanitized planner-facing executable Action snapshot (inert data only)."""
    capabilities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(
        candidate_id: str,
        kind: str,
        route: str,
        guidance_access: str,
        *,
        mode: str = "",
        description: str = "",
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id or (candidate_id, kind) in seen:
            return
        seen.add((candidate_id, kind))
        entry: dict[str, Any] = {
            "id": candidate_id,
            "kind": kind,
            "route": route,
            "guidance_access": guidance_access,
            "description": str(description or "").strip(),
        }
        if mode:
            entry["mode"] = mode
        if meta:
            for key in ("side_effect_level", "replay_safe"):
                if key in meta:
                    entry[key] = DataFormatter.sanitize(meta[key])
        capabilities.append(entry)

    # Actions -> model_request route, no model-facing guidance beyond their spec.
    try:
        for action in execution.action_candidates() or []:
            if not isinstance(action, dict):
                continue
            add(
                action.get("action_id") or action.get("name") or "",
                "action",
                "model_request",
                "none",
                description=str(action.get("desc") or action.get("description") or ""),
                meta=action,
            )
    except Exception:
        pass

    return capabilities
