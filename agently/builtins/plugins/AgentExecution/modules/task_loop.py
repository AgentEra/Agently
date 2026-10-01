"""Goal pursuit using one editable checklist and the existing execution owners."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal, cast

from agently.core.application.AgentExecution import AgentExecutionLimitExceeded
from agently.core.orchestration import TriggerFlow
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import DataFormatter

from .model_stage import run_model_stage
from .revisions import content_digest
from .runtime_guidance import insert_pending_guidance

if TYPE_CHECKING:
    from .execution import AgentExecution
    from agently.core.operation.Action import Action


_INSTRUCTION = """Advance the original task using the current checklist and actual observations.
The taskboard is an editable Markdown checklist. Add, remove, split, merge, reorder, check or reopen items as needed. New findings may add work; simple tasks need no checklist. Editing it does not change the original goal or required deliverables.
One action may advance several items; one item may require several rounds. Select only offered actions with their declared arguments. Calls within a batch must be independent; await actual results before deciding dependent calls. Plans and checkmarks are not evidence that actions succeeded.
Return continue when more work is possible, completed when the original task and required deliverables are fulfilled, or blocked when required information or available capabilities prevent further progress. Disclosing a missing requirement does not fulfill it; permitted template blanks do not imply failure.
Keep taskboard null to retain it. Return the current useful result or null while continuing, and the full useful result at termination. When applicable, explain unfinished work, uncertainties to check, actual consequences and information needed. Do not invent completion percentages or causes of failure.
Terminal decisions must have no action_calls: an operation cannot be declared complete before execution. Preserve usable work when blocked. Return result in the caller's declared output shape."""


def _store(owner: AgentExecution, state: dict[str, Any]) -> None:
    owner._producer_state = {"kind": "task_loop", "content": state, "digest": content_digest(state)}


def _state(owner: AgentExecution) -> dict[str, Any]:
    retained = owner._producer_state
    if retained.get("kind") != "task_loop":
        state: dict[str, Any] = {
            "taskboard": "", "rounds": 0, "observations": [], "result": None,
            "status": "continue", "succeeded_actions": [], "revision": owner.revision,
        }
        _store(owner, state)
        return state
    if retained.get("digest") != content_digest(retained.get("content")):
        raise ValueError("Retained long-task state changed outside its execution owner.")
    return deepcopy(retained["content"])


async def prepare_rework(owner: AgentExecution) -> None:
    state = _state(owner)
    if state["revision"] != owner.revision:
        state["status"] = "continue"
        state["revision"] = owner.revision
        _store(owner, state)
    owner._review_contract["rework_feedback"] = owner._rework_feedback


def _decision(value: object, offered: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Long-task decision must be a mapping.")
    decision = dict(value)
    if decision.get("status") not in {"continue", "completed", "blocked"}:
        raise ValueError("Invalid long-task status.")
    if decision.get("taskboard") is not None and not isinstance(decision["taskboard"], str):
        raise ValueError("Taskboard must be text or null.")
    calls = decision.get("action_calls")
    if not isinstance(calls, list):
        raise ValueError("Long-task action_calls must be a list.")
    for call in calls:
        if (not isinstance(call, dict) or call.get("action_id") not in offered
                or not isinstance(call.get("action_input"), dict)):
            raise ValueError("Long-task call must name an offered Action and mapping arguments.")
    if decision["status"] != "continue" and (calls or decision.get("result") is None):
        raise ValueError("Terminal decisions require a result and no pending Actions.")
    return decision


async def run_task_loop(owner: AgentExecution) -> dict[str, Any]:
    """Run a settled sequence; outer execution owns cancellation and safe pauses."""
    from .task_strategy import _resolve_required_skill_availability

    _, skill_failure = await _resolve_required_skill_availability(owner, goal=owner.task_goal())
    if skill_failure is not None:
        owner.status = "blocked"
        return {"status": "blocked", "accepted": False, "final_result": None,
                "reason": "Required Skills are unavailable.", "required_capabilities": skill_failure}

    state = _state(owner)
    task_options = owner.task_strategy_options()
    max_rounds = task_options.get("max_iterations", 20)
    if max_rounds is None:
        max_rounds = 20
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
        raise ValueError("Long-task max_iterations must be a positive integer.")
    options = task_options.get("options") or {}
    task_settings = options.get("agent_task", {}) if isinstance(options, Mapping) else {}
    paths = task_settings.get("required_deliverables", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
        raise ValueError("required_deliverables must contain non-empty paths.")
    candidates = owner.action_candidates()
    offered = {str(spec.get("action_id") or spec.get("name")) for spec in candidates}
    action = cast("Action", getattr(owner.agent, "action"))
    actions = action._to_model_planning_action_list(candidates)
    output = owner.request.prompt.get("output") or str
    schema = {
        "taskboard": (str, "Complete revised Markdown checklist, or null to keep it.", False),
        "status": (Literal["continue", "completed", "blocked"], "Original task's current status."),
        "action_calls": [{"action_id": str, "action_input": dict}],
        "result": (output, "Full useful result in the original output shape; null while work continues.", False),
    }
    required = set(owner.required_action_ids())
    flow: TriggerFlow[Any, Any, Any] = TriggerFlow(name="long-task-loop")
    error: list[BaseException] = []
    paused = False

    async def decide(data: TriggerFlowRuntimeData) -> None:
        nonlocal state, paused
        try:
            if owner._pause_requested:
                paused = True
                return
            if state["rounds"] >= max_rounds:
                raise AgentExecutionLimitExceeded("Long-task round limit reached before completion.",
                    limit_name="max_iterations", limit_value=max_rounds, used=state["rounds"])
            state["rounds"] += 1
            _store(owner, state)
            await insert_pending_guidance(owner)
            for reader in owner.context_readers.values():
                if not reader.is_current:
                    reader.refresh()
            stage = await run_model_stage(
                owner, producer="long_task", stage="decide",
                stage_input={"goals": owner.goal_items, "success_criteria": owner.success_criteria_items},
                stage_info={"taskboard": state["taskboard"], "observations": state["observations"],
                    "current_result": state["result"], "available_actions": actions,
                    "required_actions": sorted(required), "required_deliverables": paths,
                    "feedback": owner._rework_feedback if owner.revision else None},
                stage_instructions=[_INSTRUCTION], output=schema, inherit_extension_handlers=False,
            )
            decision = _decision(stage.value, offered)
            if decision.get("taskboard") is not None:
                state["taskboard"] = decision["taskboard"]
            if decision.get("result") is not None:
                state["result"] = decision["result"]
            state["status"] = decision["status"]
            if state["status"] == "completed":
                failures: list[dict[str, Any]] = []
                missing = required - set(state["succeeded_actions"])
                if missing:
                    failures.append({"required_actions_not_succeeded": sorted(missing)})
                refs: list[dict[str, Any]] = []
                for path in paths:
                    try:
                        workspace = owner.task_workspace
                        if workspace.resolve_file_path(path) != workspace.resolve_path(path):
                            raise ValueError("File exists only at a fallback path, not the required destination.")
                        read = await workspace.read_file(path)
                        if not read.readable:
                            raise ValueError("Required file cannot be read with the configured file handler.")
                        refs.append({"type": "file", "path": read.path, "sha256": read.sha256,
                            "size": read.total_bytes, "task_workspace_id": read.task_workspace_id,
                            "execution_id": read.execution_id, "role": "deliverable"})
                    except (OSError, ValueError) as failure:
                        failures.append({"path": path, "error": str(failure)})
                owner._terminal_task_handoff_refs = refs
                if failures:
                    state["status"] = "continue"
                    state["observations"].append({"source": "required_delivery", "status": "error",
                        "result": failures})
            _store(owner, state)
            await owner.emit_stream("long_task.progress", {"taskboard": state["taskboard"],
                "status": state["status"], "round": state["rounds"]}, route="agent_task")
            if state["status"] == "continue":
                await data.async_emit("ACTION" if decision["action_calls"] else "DECIDE", decision["action_calls"])
        except BaseException as failure:
            error.append(failure)
            raise

    async def act(data: TriggerFlowRuntimeData) -> None:
        try:
            records = await action._async_execute_action_calls(
                action_calls=data.value, settings=owner.request.settings, agent_name=owner.agent.name,
                parent_run_context=owner.agent_execution_run_context,
            )
            for record in records:
                await owner.record_action_log(record, route="agent_task")
                state["observations"].append(DataFormatter.sanitize({key: record.get(key)
                    for key in ("action_id", "kwargs", "status", "result", "error") if key in record}))
                if record.get("status") == "success":
                    state["succeeded_actions"] = sorted(set(state["succeeded_actions"]) | {str(record.get("action_id"))})
            _store(owner, state)
            await data.async_emit("DECIDE", None)
        except BaseException as failure:
            error.append(failure)
            raise

    flow.to(decide)
    flow.when("DECIDE").to(decide)
    flow.when("ACTION").to(act)
    execution = flow.create_execution(auto_close=False, intervention_mode=None,
        parent_run_context=owner.agent_execution_run_context)
    try:
        await execution.async_start(None)
        if error:
            raise error[0]
    finally:
        await execution.async_close(pending_interrupts="cancel")
    if paused:
        from .lifecycle import pause_at
        from .route_execution import prepare_production
        async def continue_task() -> tuple[str, object]:
            assert owner._production_options is not None
            return await prepare_production(owner, owner._production_options)
        await pause_at(owner, "long_task_step", continue_task)
    accepted = state["status"] == "completed"
    owner.status = "success" if accepted else "blocked"
    owner.task_refs = {"task_id": owner.id, "effective_execution_strategy": "loop",
                       "status": state["status"]}
    owner.close_snapshot = {"status": state["status"], "route": "agent_task", "rounds": state["rounds"]}
    return {"status": state["status"], "accepted": accepted, "artifact_status": "accepted" if accepted else "blocked",
            "task_id": owner.id, "effective_execution_strategy": "loop", "taskboard": state["taskboard"],
            "final_result": state["result"], "final_response": state["result"] if isinstance(state["result"], str) else ""}
