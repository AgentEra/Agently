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

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any

from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.runtime.RuntimeContext import bind_runtime_context, get_current_agent_execution_context, resolve_parent_run_context

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import ActionResult
    from agently.types.plugins import ActionFlowObservationHandler
    from agently.utils import Settings


class TriggerFlowActionFlow:
    name = "TriggerFlowActionFlow"
    DEFAULT_SETTINGS = {}

    def __init__(self, *, plugin_manager, settings: "Settings"):
        self.plugin_manager = plugin_manager
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    @staticmethod
    def _record_action_id(record: dict[str, Any]) -> str:
        return str(record.get("action_id") or record.get("tool_name") or "").strip()

    @staticmethod
    def _record_has_progress(record: dict[str, Any]) -> bool:
        status = str(record.get("status") or "").strip().lower()
        return bool(record.get("success")) or status in {
            "success",
            "succeeded",
            "partial_success",
            "approval_required",
            "blocked",
        }

    @classmethod
    def _failed_action_ids_without_progress(cls, records: list[dict[str, Any]]) -> set[str]:
        failed_action_ids: set[str] = set()
        for record in records:
            if cls._record_has_progress(record):
                return set()
            action_id = cls._record_action_id(record)
            if action_id:
                failed_action_ids.add(action_id)
        return failed_action_ids

    @classmethod
    async def _update_failed_action_counts(
        cls,
        data: Any,
        records: list[dict[str, Any]],
        *,
        max_consecutive_failed_rounds_per_action: int,
    ) -> bool:
        failed_action_ids = cls._failed_action_ids_without_progress(records)
        if not failed_action_ids:
            await data.async_set_state("consecutive_failed_action_counts", {})
            return False
        raw_counts = data.get_state("consecutive_failed_action_counts", {})
        previous_counts = raw_counts if isinstance(raw_counts, dict) else {}
        next_counts: dict[str, int] = {}
        for action_id in failed_action_ids:
            try:
                previous = int(previous_counts.get(action_id, 0))
            except Exception:
                previous = 0
            next_counts[action_id] = previous + 1
        await data.async_set_state("consecutive_failed_action_counts", next_counts)
        return any(count >= max_consecutive_failed_rounds_per_action for count in next_counts.values())

    @staticmethod
    def _typed_evidence_identity(record: dict[str, Any]) -> dict[str, Any] | None:
        if not TriggerFlowActionFlow._record_has_progress(record):
            return None
        candidates: list[Any] = [record]
        for key in ("result", "data", "model_digest"):
            candidate = record.get(key)
            if isinstance(candidate, dict):
                candidates.append(candidate)
                preview = candidate.get("result_preview")
                if isinstance(preview, dict):
                    candidates.append(preview)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            owner = str(candidate.get("owner") or "").strip()
            locator = str(candidate.get("locator") or "").strip()
            content_version = str(candidate.get("content_version") or "").strip()
            raw_range = candidate.get("range")
            if not owner or not locator or not content_version or not isinstance(raw_range, dict):
                continue
            bounded_range = {
                key: raw_range.get(key)
                for key in ("offset", "end", "read_bytes")
                if isinstance(raw_range.get(key), int) and not isinstance(raw_range.get(key), bool)
            }
            if not bounded_range:
                continue
            return {
                "owner": owner,
                "locator": locator,
                "content_version": content_version,
                "range": bounded_range,
            }
        return None

    @classmethod
    async def _update_unchanged_evidence_page_state(
        cls,
        data: Any,
        records: list[dict[str, Any]],
        *,
        max_consecutive_unchanged_evidence_rounds: int,
    ) -> tuple[bool, int, list[dict[str, Any]]]:
        progress_records = [record for record in records if cls._record_has_progress(record)]
        typed_identities = [cls._typed_evidence_identity(record) for record in progress_records]
        if not progress_records or any(identity is None for identity in typed_identities):
            # Repeating one page is not stagnation when the same round also
            # produced any other successful information. Reset the counter so
            # this guard cannot turn mixed evidence progress into a false stop.
            await data.async_set_state("unchanged_evidence_page_state", {})
            return False, 0, []
        identities = {
            (
                identity["owner"],
                identity["locator"],
                identity["content_version"],
                tuple(sorted(identity["range"].items())),
            ): identity
            for identity in typed_identities
            if identity is not None
        }
        ordered = [identities[key] for key in sorted(identities)]
        if not ordered:
            await data.async_set_state("unchanged_evidence_page_state", {})
            return False, 0, []
        previous = data.get_state("unchanged_evidence_page_state", {})
        previous = previous if isinstance(previous, dict) else {}
        occurrence_count = (
            int(previous.get("occurrence_count", 0)) + 1
            if previous.get("evidence_identities") == ordered
            else 1
        )
        await data.async_set_state(
            "unchanged_evidence_page_state",
            {
                "occurrence_count": occurrence_count,
                "evidence_identities": ordered,
            },
        )
        return (
            occurrence_count >= max_consecutive_unchanged_evidence_rounds,
            occurrence_count,
            ordered,
        )

    async def async_run(
        self,
        *,
        action,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        parent_run_context=None,
        planning_handler=None,
        execution_handler=None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
        runtime_observation_handler: "ActionFlowObservationHandler | None" = None,
    ) -> list["ActionResult"]:
        from agently.core.orchestration.TriggerFlow import TriggerFlow
        from agently.types.data import RunContext

        if planning_handler is None:
            raise RuntimeError("[Agently ActionFlow] planning_handler is required.")
        if execution_handler is None:
            raise RuntimeError("[Agently ActionFlow] execution_handler is required.")

        resolved_planning_handler = planning_handler
        resolved_execution_handler = execution_handler

        parent_run_context = resolve_parent_run_context(parent_run_context)
        if len(action_list) == 0:
            return []

        if max_rounds is None:
            max_rounds = action.action_settings.get(
                "loop.max_rounds",
                action.tool_settings.get("loop.max_rounds", None),
            )
        if concurrency is None:
            concurrency = action.action_settings.get(
                "loop.concurrency",
                action.tool_settings.get("loop.concurrency", None),
            )
        if timeout is None:
            timeout = action.action_settings.get("loop.timeout", action.tool_settings.get("loop.timeout", None))
        max_consecutive_failed_rounds_per_action = action.action_settings.get(
            "loop.max_consecutive_failed_rounds_per_action",
            action.tool_settings.get("loop.max_consecutive_failed_rounds_per_action", 2),
        )

        if not isinstance(max_rounds, int) or max_rounds < 0:
            max_rounds = None
        if not isinstance(concurrency, int) or concurrency <= 0:
            concurrency = None
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            timeout = None
        if (
            not isinstance(max_consecutive_failed_rounds_per_action, int)
            or max_consecutive_failed_rounds_per_action <= 0
        ):
            max_consecutive_failed_rounds_per_action = 2
        max_consecutive_unchanged_evidence_rounds = action.action_settings.get(
            "loop.max_consecutive_unchanged_evidence_rounds",
            action.tool_settings.get("loop.max_consecutive_unchanged_evidence_rounds", 3),
        )
        if (
            not isinstance(max_consecutive_unchanged_evidence_rounds, int)
            or max_consecutive_unchanged_evidence_rounds <= 0
        ):
            max_consecutive_unchanged_evidence_rounds = 3

        action_loop_run = RunContext.create(
            run_kind="action_loop",
            parent=parent_run_context,
            agent_name=agent_name,
            session_id=str(settings.get("runtime.session_id")) if settings.get("runtime.session_id", None) else None,
            meta={
                "action_count": len(action_list),
                "tool_count": len(action_list),
                "action_type": "action_loop",
                "compat_event_family": "tool",
            },
        )
        artifact_scope = action._resolve_artifact_scope(
            run_context=action_loop_run,
            agent_execution_context=get_current_agent_execution_context(),
        )
        owns_artifact_scope = artifact_scope.get("kind") == "action_run"

        async def async_call_scoped_action(name: str, kwargs: dict[str, Any]) -> Any:
            return await action._async_call_action_with_scope(
                name,
                kwargs,
                artifact_scope=artifact_scope,
            )

        async def publish_runtime_observation(
            kind: str,
            *,
            message: str,
            payload: dict[str, Any],
            stream_projection: dict[str, Any] | None = None,
            level: str = "INFO",
            error: BaseException | None = None,
            run=None,
            compat_event_family: str | None = "tool",
            compat_message: str | None = None,
        ):
            if runtime_observation_handler is None:
                return
            result = runtime_observation_handler(
                action._to_runtime_visible_observation(
                    {
                        "kind": kind,
                        "source": "ActionFlow",
                        "level": level,
                        "message": message,
                        "payload": payload,
                        "error": error,
                        "run": action_loop_run if run is None else run,
                        "compat_event_family": compat_event_family,
                        "compat_message": compat_message,
                        **(
                            {"stream_projection": stream_projection}
                            if stream_projection is not None
                            else {}
                        ),
                    }
                )
            )
            if inspect.isawaitable(result):
                await result

        with bind_runtime_context(
            parent_run_context=action_loop_run,
            tool_phase_run_context=action_loop_run,
            settings=settings,
        ):
            await publish_runtime_observation(
                "loop_started",
                message=f"Action loop started for agent '{ agent_name }'.",
                compat_message=f"Tool loop started for agent '{ agent_name }'.",
                payload={
                    "agent_name": agent_name,
                    "action_count": len(action_list),
                    "tool_count": len(action_list),
                    "planning_protocol": action.action_runtime.resolve_planning_protocol(settings, planning_protocol),
                },
            )

        flow = TriggerFlow(name=f"action-loop-{ agent_name }")

        async def initialize_loop(data):
            await data.async_set_state("done_plans", [])
            await data.async_set_state("last_round_records", [])
            await data.async_set_state("consecutive_failed_action_counts", {})
            await data.async_set_state("unchanged_evidence_page_state", {})
            await data.async_set_state("round_index", 0)
            await data.async_emit("PLAN", None)
            return None

        async def plan_step(data):
            round_index = data.get_state("round_index", 0)
            if not isinstance(round_index, int):
                round_index = 0
            done_plans = data.get_state("done_plans", [])
            if not isinstance(done_plans, list):
                done_plans = []
            last_round_records = data.get_state("last_round_records", [])
            if not isinstance(last_round_records, list):
                last_round_records = []
            model_visible_done_plans = action.to_model_visible_records(done_plans)
            model_visible_last_round_records = action.to_model_visible_records(last_round_records)

            def max_rounds_diagnostic_records(
                *,
                pending_action_count: int = 0,
                record_index: int = 0,
            ) -> list[dict[str, Any]]:
                diagnostic = {
                    "source": "ActionFlow",
                    "severity": "warning",
                    "code": "action_loop.max_rounds_reached",
                    "message": "Action loop stopped because max_rounds was reached before the planner converged.",
                    "meta": {
                        "round_index": round_index,
                        "max_rounds": max_rounds,
                        "pending_action_count": pending_action_count,
                        "planning_protocol": planning_protocol,
                    },
                }
                return [
                    action._normalize_execution_record(
                        {
                            "ok": False,
                            "status": "blocked",
                            "success": False,
                            "purpose": diagnostic["message"],
                            "action_id": "action_loop",
                            "tool_name": "action_loop",
                            "kwargs": {},
                            "result": diagnostic,
                            "data": diagnostic,
                            "error": diagnostic["message"],
                            "diagnostics": [diagnostic],
                            "expose_to_model": True,
                            "meta": {
                                "planning_protocol": planning_protocol,
                                "round_index": round_index,
                                "max_rounds": max_rounds,
                            },
                        },
                        None,
                        record_index,
                    )
                ]

            if isinstance(max_rounds, int) and max_rounds >= 0 and round_index >= max_rounds:
                await data.async_emit("DONE", [*done_plans, *max_rounds_diagnostic_records()])
                return {
                    "next_action": "response",
                    "use_action": False,
                    "next": "",
                    "action_calls": [],
                    "diagnostics": [],
                }

            visible_action_list = action._with_action_artifact_recall_action(
                action_list,
                model_visible_last_round_records or model_visible_done_plans,
            )

            decision = action._normalize_action_decision(
                await resolved_planning_handler(
                    {
                        "prompt": prompt,
                        "settings": settings,
                        "agent_name": agent_name,
                        "round_index": round_index,
                        "max_rounds": max_rounds,
                        "done_plans": model_visible_done_plans,
                        "last_round_records": model_visible_last_round_records,
                        "parent_run_context": parent_run_context,
                        "action": action,
                        "runtime": action.action_runtime,
                    },
                    {
                        "action_list": visible_action_list,
                        "planning_protocol": planning_protocol,
                    },
                )
            )
            decision = action._apply_action_decision_round_dispatch_policy(decision, settings)

            dispatch_confirmed = action._should_continue(
                decision,
                round_index=round_index,
                max_rounds=max_rounds,
            )
            await publish_runtime_observation(
                "plan_ready",
                message=f"Action plan ready for round { round_index }.",
                compat_message=f"Tool plan ready for round { round_index }.",
                payload={
                    "agent_name": agent_name,
                    "round_index": round_index,
                    "decision": decision,
                },
                stream_projection={
                    "concurrency": concurrency,
                    "parallel": len(decision.get("action_calls", [])) > 1 and concurrency != 1,
                    "dispatch_confirmed": dispatch_confirmed,
                },
            )
            planning_diagnostics = decision.get("diagnostics", [])
            diagnostic_records = []
            if isinstance(planning_diagnostics, list) and planning_diagnostics:
                for diagnostic_index, diagnostic in enumerate(planning_diagnostics):
                    if not isinstance(diagnostic, dict):
                        continue
                    diagnostic_records.append(
                        action._normalize_execution_record(
                            {
                                "ok": False,
                                "status": "skipped",
                                "success": False,
                                "purpose": str(diagnostic.get("message", "Action planning diagnostic.")),
                                "action_id": "action_planning",
                                "tool_name": "action_planning",
                                "kwargs": {},
                                "result": diagnostic,
                                "data": diagnostic,
                                "error": str(diagnostic.get("message", "Action planning diagnostic.")),
                                "diagnostics": [diagnostic],
                                "expose_to_model": False,
                                "meta": {
                                    "planning_protocol": planning_protocol,
                                    "round_index": round_index,
                                },
                            },
                            None,
                            diagnostic_index,
                        )
                    )
            action_calls = decision.get("action_calls")
            wants_action = (
                decision.get("next_action") == "execute"
                and decision.get("use_action") is True
                and isinstance(action_calls, list)
                and len(action_calls) > 0
            )
            max_rounds_reached = (
                wants_action
                and isinstance(max_rounds, int)
                and max_rounds >= 0
                and round_index >= max_rounds
            )
            if max_rounds_reached:
                diagnostic_records.extend(
                    max_rounds_diagnostic_records(
                        pending_action_count=len(action_calls) if isinstance(action_calls, list) else 0,
                        record_index=len(diagnostic_records),
                    )
                )
            if dispatch_confirmed:
                await data.async_emit("EXECUTE", decision.get("action_calls", []))
            else:
                await data.async_emit("DONE", [*done_plans, *diagnostic_records])
            return decision

        async def execute_step(data):
            action_calls = data.value if isinstance(data.value, list) else []
            round_index = data.get_state("round_index", 0)
            if not isinstance(round_index, int):
                round_index = 0
            done_plans = data.get_state("done_plans", [])
            if not isinstance(done_plans, list):
                done_plans = []
            last_round_records = data.get_state("last_round_records", [])
            if not isinstance(last_round_records, list):
                last_round_records = []

            approval_decisions = data.get_state("policy_approval_decisions", {})
            if not isinstance(approval_decisions, dict):
                approval_decisions = {}
            pending_approval_key = str(data.get_state("pending_policy_approval_key", "") or "")
            if getattr(data, "is_resume", False) and pending_approval_key:
                from agently.base import policy_approval

                resume_value = getattr(getattr(data, "resume", None), "value", None)
                resume_decision = policy_approval.normalize_decision(resume_value, handler="triggerflow_resume")
                if resume_decision.get("status") == "approved":
                    approval_decisions[pending_approval_key] = resume_decision
                    await data.async_set_state("policy_approval_decisions", approval_decisions)
                    await data.async_set_state("pending_policy_approval_key", "")
                else:
                    pending_action = data.get_state("pending_policy_approval_action", {})
                    if not isinstance(pending_action, dict):
                        pending_action = {}
                    blocked_action_id = str(pending_action.get("action_id", pending_action.get("tool_name", "unknown")))
                    blocked_record = {
                        "ok": False,
                        "status": "blocked",
                        "success": False,
                        "purpose": str(pending_action.get("purpose", f"Use { blocked_action_id }")),
                        "action_id": blocked_action_id,
                        "tool_name": str(pending_action.get("tool_name", blocked_action_id)),
                        "kwargs": pending_action.get("action_input", {}),
                        "result": None,
                        "data": None,
                        "error": str(resume_decision.get("reason", "Policy approval was denied.")),
                        "approval": {"required": True, "decision": resume_decision},
                    }
                    records = action._normalize_execution_records(
                        [blocked_record],
                        [pending_action],
                        artifact_scope=artifact_scope,
                    )
                    state_records = action._to_action_flow_return_records(records)
                    done_plans.extend(state_records)
                    await data.async_set_state("done_plans", done_plans)
                    await data.async_set_state("last_round_records", state_records)
                    await data.async_set_state("round_index", round_index + 1)
                    await data.async_set_state("pending_policy_approval_key", "")
                    await data.async_set_state("pending_policy_approval_action", {})
                    await data.async_emit("PLAN", None)
                    return state_records

            from agently.base import policy_approval
            from agently.core.orchestration.TriggerFlow.Control import TriggerFlowPauseSignal

            for command_index, command in enumerate(action_calls):
                action_id = str(command.get("action_id", command.get("tool_name", "")))
                if not action_id:
                    continue
                spec = action.action_registry.get_spec(action_id)
                if spec is None:
                    continue
                policy_override = command.get("policy_override", {})
                if not isinstance(policy_override, dict):
                    policy_override = {}
                sanitized_policy_override, _ = action.action_dispatcher._sanitize_policy_override(
                    policy_override,
                    source_protocol="structured_plan",
                )
                policy = action.action_dispatcher._merge_policy(settings, spec, sanitized_policy_override)
                policy_approval_handler = settings.get("policy_approval.handler", None)
                if policy_approval_handler is not None and not policy.get("policy_approval_handler"):
                    policy["policy_approval_handler"] = str(policy_approval_handler)
                approval_needed = spec.get("approval_required") is True or policy.get("approval_mode") == "always"
                approval_key = f"{ round_index }:{ command_index }:{ action_id }"
                if not approval_needed:
                    continue
                if approval_key in approval_decisions:
                    approved_override = dict(sanitized_policy_override)
                    approved_override["policy_approval_granted"] = True
                    approved_override["policy_approval_decision"] = approval_decisions[approval_key]
                    command["policy_override"] = approved_override
                    continue
                await data.async_set_state("pending_policy_approval_key", approval_key)
                await data.async_set_state("pending_policy_approval_action", dict(command))
                if data.execution._get_runtime_resource("record_store", None) is None:
                    data.execution.set_runtime_resource("record_store", recovery_record_store)
                gate_result = await policy_approval.async_gate(
                    data,
                    {
                        "source": "action",
                        "capability": action_id,
                        "subject": str(spec.get("name") or action_id),
                        "risk": str(spec.get("side_effect_level", "")),
                        "payload": {
                            "action_call": dict(command),
                            "round_index": round_index,
                            "command_index": command_index,
                        },
                        "policy": dict(policy),
                        "lineage": {
                            "agent_name": agent_name,
                            "round_index": round_index,
                            "command_index": command_index,
                        },
                        "execution_id": str(getattr(data.execution, "id", "")),
                    },
                    handler=str(policy.get("policy_approval_handler") or "") or None,
                    resume_to="self",
                    settings=settings,
                )
                if isinstance(gate_result, TriggerFlowPauseSignal):
                    return gate_result
                if gate_result.get("status") == "approved":
                    approval_decisions[approval_key] = gate_result
                    await data.async_set_state("policy_approval_decisions", approval_decisions)
                    approved_override = dict(sanitized_policy_override)
                    approved_override["policy_approval_granted"] = True
                    approved_override["policy_approval_decision"] = gate_result
                    command["policy_override"] = approved_override
                    await data.async_set_state("pending_policy_approval_key", "")
                    await data.async_set_state("pending_policy_approval_action", {})
                    continue
                blocked_record = {
                    "ok": False,
                    "status": "blocked",
                    "success": False,
                    "purpose": str(command.get("purpose", f"Use { action_id }")),
                    "action_id": action_id,
                    "tool_name": str(command.get("tool_name", action_id)),
                    "kwargs": command.get("action_input", {}),
                    "result": None,
                    "data": None,
                    "error": str(gate_result.get("reason", "Policy approval was denied.")),
                    "approval": {"required": True, "decision": gate_result},
                }
                records = action._normalize_execution_records(
                    [blocked_record],
                    [command],
                    artifact_scope=artifact_scope,
                )
                state_records = action._to_action_flow_return_records(records)
                done_plans.extend(state_records)
                await data.async_set_state("done_plans", done_plans)
                await data.async_set_state("last_round_records", state_records)
                await data.async_set_state("round_index", round_index + 1)
                await data.async_set_state("pending_policy_approval_key", "")
                await data.async_set_state("pending_policy_approval_action", {})
                await data.async_emit("PLAN", None)
                return state_records

            action_runs = []
            for command_index, command in enumerate(action_calls):
                action_id = str(command.get("action_id", command.get("tool_name", "unknown")))
                purpose = str(command.get("purpose", f"action_call_{ command_index + 1 }"))
                action_run = action_loop_run.create_child(
                    run_kind="action",
                    meta={
                        "action_type": "tool",
                        "action_name": action_id,
                        "purpose": purpose,
                        "round_index": round_index,
                        "command_index": command_index,
                    },
                )
                action_runs.append(action_run)
                await publish_runtime_observation(
                    "action_started",
                    message=f"Action '{ action_id }' started.",
                    payload={
                        "agent_name": agent_name,
                        "round_index": round_index,
                        "command_index": command_index,
                        "action_type": "tool",
                        "action_name": action_id,
                        "command": command,
                    },
                    run=action_run,
                )

            trusted_policy_overrides: dict[int, dict[str, Any]] = {}
            for command_index, command in enumerate(action_calls):
                command_action_id = str(command.get("action_id", command.get("tool_name", "")))
                decision_key = f"{ round_index }:{ command_index }:{ command_action_id }"
                decision = approval_decisions.get(decision_key)
                if isinstance(decision, dict) and decision.get("status") == "approved":
                    # Approval grants travel host-side: the dispatcher strips
                    # policy_approval_granted from model-planned commands, so
                    # the grant must never rely on the action_call payload.
                    trusted_policy_overrides[command_index] = {
                        "policy_approval_granted": True,
                        "policy_approval_decision": dict(decision),
                    }
            records = action._normalize_execution_records(
                await resolved_execution_handler(
                    {
                        "prompt": prompt,
                        "settings": settings,
                        "agent_name": agent_name,
                        "round_index": round_index,
                        "max_rounds": max_rounds,
                        "done_plans": done_plans,
                        "last_round_records": last_round_records,
                        "parent_run_context": parent_run_context,
                        "artifact_scope": artifact_scope,
                        "action": action,
                        "runtime": action.action_runtime,
                    },
                    {
                        "action_calls": action_calls,
                        "async_call_action": async_call_scoped_action,
                        "concurrency": concurrency,
                        "timeout": timeout,
                        "trusted_policy_overrides": trusted_policy_overrides,
                    },
                ),
                action_calls,
                artifact_scope=artifact_scope,
            )
            bounded_records = action._to_action_flow_return_records(records)
            agent_execution_context = get_current_agent_execution_context()
            record_action_records = getattr(agent_execution_context, "record_action_records", None)
            if callable(record_action_records):
                record_action_records(
                    [
                        {
                            **record,
                            "round_index": round_index,
                            "command_index": record_index,
                        }
                        for record_index, record in enumerate(bounded_records)
                    ],
                    source="ActionFlow",
                )
            should_stop_after_failed_actions = await self._update_failed_action_counts(
                data,
                bounded_records,
                max_consecutive_failed_rounds_per_action=max_consecutive_failed_rounds_per_action,
            )
            (
                should_stop_after_unchanged_evidence,
                unchanged_evidence_occurrence_count,
                unchanged_evidence_identities,
            ) = await self._update_unchanged_evidence_page_state(
                data,
                bounded_records,
                max_consecutive_unchanged_evidence_rounds=(
                    max_consecutive_unchanged_evidence_rounds
                ),
            )

            for record_index, record in enumerate(bounded_records):
                action_id = record.get("action_id", record.get("tool_name", "unknown"))
                success = bool(record.get("success"))
                status = str(record.get("status", "") or "")
                if success:
                    event_kind = "action_completed"
                    event_level = "INFO"
                    event_message = f"Action '{ action_id }' completed."
                elif status == "approval_required":
                    event_kind = "action_approval_required"
                    event_level = "WARNING"
                    event_message = f"Action '{ action_id }' requires approval."
                elif status == "blocked":
                    event_kind = "action_blocked"
                    event_level = "WARNING"
                    event_message = f"Action '{ action_id }' blocked."
                else:
                    event_kind = "action_failed"
                    event_level = "WARNING"
                    event_message = f"Action '{ action_id }' failed."
                action_run = (
                    action_runs[record_index]
                    if record_index < len(action_runs)
                    else action_loop_run.create_child(
                        run_kind="action",
                        meta={
                            "action_type": "tool",
                            "action_name": str(action_id),
                            "round_index": round_index,
                            "command_index": record_index,
                        },
                    )
                )
                await publish_runtime_observation(
                    event_kind,
                    level=event_level,
                    message=event_message,
                    payload={
                        "agent_name": agent_name,
                        "round_index": round_index,
                        "record_index": record_index,
                        "action_type": "tool",
                        "action_name": str(action_id),
                        "record": record,
                    },
                    run=action_run,
                )

            state_records = bounded_records
            done_plans.extend(state_records)
            await data.async_set_state("done_plans", done_plans)
            await data.async_set_state("last_round_records", state_records)
            await data.async_set_state("round_index", round_index + 1)
            if should_stop_after_failed_actions:
                await publish_runtime_observation(
                    "loop_failed_action_converged",
                    level="WARNING",
                    message="Action loop stopped after repeated failed action rounds.",
                    payload={
                        "agent_name": agent_name,
                        "round_index": round_index,
                        "max_consecutive_failed_rounds_per_action": max_consecutive_failed_rounds_per_action,
                        "records": bounded_records,
                    },
                )
                await data.async_emit("DONE", done_plans)
                return state_records
            if should_stop_after_unchanged_evidence:
                await publish_runtime_observation(
                    "loop_unchanged_evidence_converged",
                    level="WARNING",
                    message="Action loop stopped after repeated unchanged typed evidence pages.",
                    payload={
                        "agent_name": agent_name,
                        "round_index": round_index,
                        "occurrence_count": unchanged_evidence_occurrence_count,
                        "max_consecutive_unchanged_evidence_rounds": (
                            max_consecutive_unchanged_evidence_rounds
                        ),
                        "evidence_identities": unchanged_evidence_identities,
                    },
                )
                await data.async_emit("DONE", done_plans)
                return state_records
            await data.async_emit("PLAN", None)
            return state_records

        async def finalize_loop(data):
            result = data.value if isinstance(data.value, list) else []
            await data.async_set_state("action_loop_result", result)
            await data.async_set_state("done_plans", result)
            return result

        flow.to(initialize_loop)
        flow.when("PLAN").to(plan_step)
        flow.when("EXECUTE").to(execute_step)
        flow.when("DONE").to(finalize_loop)

        execution = flow.create_execution(
            parent_run_context=action_loop_run,
            record_store=False,
            auto_close=False,
        )
        recovery_record_store = flow._create_execution_record_store_resource(
            execution.id,
            execution.run_context,
        )
        exchange_paused = False
        action_loop_completed = False
        standalone_scope_released = False

        def release_standalone_artifact_scope_once() -> None:
            nonlocal standalone_scope_released
            if not owns_artifact_scope or standalone_scope_released:
                return
            standalone_scope_released = True
            action._release_artifact_scope(artifact_scope)

        async def finalize_live_exchange_execution() -> None:
            from agently.base import execution_exchange

            pending = execution.get_pending_interrupts()
            if pending:
                for pending_interrupt in pending.values():
                    pending_envelope = pending_interrupt.get("external_wait_request")
                    pending_envelope = pending_envelope if isinstance(pending_envelope, dict) else {}
                    execution_exchange.register_live_wait(
                        execution=execution,
                        interrupt_id=str(pending_interrupt.get("id", "")),
                        exchange_id=pending_envelope.get("exchange_id"),
                        on_resolved=finalize_live_exchange_execution,
                        on_closed=release_standalone_artifact_scope_once,
                    )
                return
            await execution.async_close(reason="action_loop_exchange_resolved")
            release_standalone_artifact_scope_once()
        try:
            with bind_runtime_context(
                parent_run_context=action_loop_run,
                tool_phase_run_context=action_loop_run,
                settings=settings,
            ):
                async def build_exchange_paused_records():
                    """End the run with typed paused records instead of raising.

                    The execution stays open and registered as a live wait so a
                    host can still respond in process; a snapshot is saved when
                    a snapshot store is bound so a cold host can recover it.
                    """
                    from agently.base import execution_exchange

                    pending = execution.get_pending_interrupts()
                    pending_views = execution_exchange.project_pending_exchanges(execution)
                    snapshot_ref = None
                    if getattr(execution, "_snapshot_store", None) is not None:
                        try:
                            snapshot_ref = await execution.async_save(step_id="exchange-pending")
                        except BaseException:
                            snapshot_ref = None
                    respond_keys: list[str] = []
                    for pending_interrupt in pending.values():
                        pending_envelope = pending_interrupt.get("external_wait_request")
                        pending_envelope = pending_envelope if isinstance(pending_envelope, dict) else {}
                        respond_keys.append(
                            execution_exchange.register_live_wait(
                                execution=execution,
                                interrupt_id=str(pending_interrupt.get("id", "")),
                                exchange_id=pending_envelope.get("exchange_id"),
                                on_resolved=finalize_live_exchange_execution,
                                on_closed=release_standalone_artifact_scope_once,
                            )
                        )
                    pending_action = execution.get_state("pending_policy_approval_action", {})
                    if not isinstance(pending_action, dict):
                        pending_action = {}
                    action_id = str(pending_action.get("action_id", pending_action.get("tool_name", "")) or "")
                    exchange_meta = {
                        "pending": pending_views,
                        "execution_id": execution.id,
                        "flow_name": flow.name,
                        "snapshot_ref": snapshot_ref,
                        "respond_keys": respond_keys,
                    }
                    paused_record = {
                        "ok": False,
                        "status": "approval_required",
                        "success": False,
                        "purpose": str(pending_action.get("purpose", "Wait for human exchange response.")),
                        "action_id": action_id or "execution_exchange",
                        "tool_name": str(pending_action.get("tool_name", action_id) or "execution_exchange"),
                        "kwargs": pending_action.get("action_input", {}),
                        "result": None,
                        "data": None,
                        "error": "Waiting for a human exchange response.",
                        "approval": {
                            "required": True,
                            "decision": {"status": "pending", "approved": False, "reason": "Waiting for a human exchange response."},
                        },
                        "meta": {"exchange": exchange_meta},
                    }
                    records = action._normalize_execution_records(
                        [paused_record],
                        [pending_action],
                        artifact_scope=artifact_scope,
                    )
                    done_plans = execution.get_state("done_plans", [])
                    if not isinstance(done_plans, list):
                        done_plans = []
                    await publish_runtime_observation(
                        "exchange_paused",
                        level="WARNING",
                        message=f"Action loop paused on a pending human exchange for agent '{ agent_name }'.",
                        payload={
                            "agent_name": agent_name,
                            "exchange": execution._to_serializable_value(exchange_meta),
                        },
                    )
                    return [*done_plans, *records]

                async def resolve_exchange_waits():
                    """Drive pending HITL exchanges after the flow settles.

                    Returns None when the loop can close normally, or the final
                    record list when this run ends paused on a durable wait.
                    """
                    from agently.base import execution_exchange

                    while True:
                        pending = execution.get_pending_interrupts()
                        if not pending:
                            return None
                        interrupt = next(iter(pending.values()))
                        interrupt_id = str(interrupt.get("id", ""))
                        envelope = interrupt.get("external_wait_request")
                        envelope = envelope if isinstance(envelope, dict) else {}
                        wait_mode = str(envelope.get("wait_mode") or "disconnected")
                        cold_policy = str(envelope.get("cold_persistence_policy") or "persist")
                        pending_exchange_views = execution._to_serializable_value(
                            execution_exchange.project_pending_exchanges(execution)
                        )
                        await publish_runtime_observation(
                            "exchange_pending",
                            level="WARNING",
                            message=f"Action loop is waiting on a human exchange for agent '{ agent_name }'.",
                            payload={
                                "agent_name": agent_name,
                                "interrupt_id": interrupt_id,
                                "wait_mode": wait_mode,
                                "pending_exchanges": pending_exchange_views,
                            },
                        )
                        await self._notify_agent_execution_exchange(
                            "pending",
                            pending_exchange_views if isinstance(pending_exchange_views, list) else [],
                            interrupt_id=interrupt_id,
                        )
                        provider = execution_exchange._resolve_interrupt_provider(execution, interrupt)
                        if provider is None and wait_mode != "disconnected":
                            # Nobody was told about this exchange (publish went
                            # nowhere), so waiting cannot resolve it. Resume
                            # with a denial instead of hanging or raising.
                            await execution.async_continue_with(
                                interrupt_id,
                                {
                                    "status": "denied",
                                    "approved": False,
                                    "reason": "No ExecutionExchange provider is configured to deliver this exchange.",
                                },
                                resume_request_id=f"exchange:{ execution.id }:{ interrupt_id }:no-provider",
                                actor="exchange_policy",
                            )
                            continue
                        if wait_mode in ("connected", "connected_then_disconnected"):
                            resolved = await execution_exchange.async_hot_wait(execution, interrupt)
                            if resolved:
                                await publish_runtime_observation(
                                    "exchange_resolved",
                                    message=f"Human exchange resolved for agent '{ agent_name }'.",
                                    payload={
                                        "agent_name": agent_name,
                                        "interrupt_id": interrupt_id,
                                    },
                                )
                                resolved_interrupt = execution.get_interrupt(interrupt_id)
                                resolved_views = (
                                    [
                                        execution._to_serializable_value(
                                            execution_exchange.project_exchange(execution.id, resolved_interrupt)
                                        )
                                    ]
                                    if isinstance(resolved_interrupt, dict)
                                    else []
                                )
                                await self._notify_agent_execution_exchange(
                                    "resolved",
                                    resolved_views,
                                    interrupt_id=interrupt_id,
                                )
                                continue
                            if wait_mode == "connected":
                                # Deny-by-timeout keeps pure connected mode
                                # deterministic: the gate resumes with a denial
                                # and the model sees a blocked record.
                                await execution.async_continue_with(
                                    interrupt_id,
                                    {
                                        "status": "denied",
                                        "approved": False,
                                        "reason": "Exchange hot wait timed out before a human response arrived.",
                                    },
                                    resume_request_id=f"exchange:{ execution.id }:{ interrupt_id }:timeout",
                                    actor="exchange_timeout",
                                )
                                continue
                        if cold_policy != "persist":
                            await execution.async_continue_with(
                                interrupt_id,
                                {
                                    "status": "denied",
                                    "approved": False,
                                    "reason": f"Exchange wait was not persisted (cold_persistence_policy={ cold_policy }).",
                                },
                                resume_request_id=f"exchange:{ execution.id }:{ interrupt_id }:{ cold_policy }",
                                actor="exchange_policy",
                            )
                            continue
                        return await build_exchange_paused_records()

                async def run_action_loop():
                    nonlocal exchange_paused
                    if timeout is None:
                        await execution.async_start()
                    else:
                        await asyncio.wait_for(execution.async_start(), timeout=float(timeout))
                    paused_records = await resolve_exchange_waits()
                    if paused_records is not None:
                        exchange_paused = True
                        self._record_agent_execution_progress("action_loop_paused", "exchange_pending", planning_protocol)
                        return {"action_loop_result": paused_records}
                    self._record_agent_execution_progress("action_loop_close", "started", planning_protocol)
                    if timeout is None:
                        close_result = await execution.async_close()
                    else:
                        close_result = await asyncio.wait_for(
                            execution.async_close(timeout=timeout), timeout=float(timeout)
                        )
                    self._record_agent_execution_progress("action_loop_close", "completed", planning_protocol)
                    return close_result

                try:
                    result = await run_action_loop()
                    action_loop_completed = True
                except asyncio.TimeoutError as error:
                    try:
                        await execution.async_close(
                            reason="action_loop_timeout",
                            timeout=0,
                            pending_interrupts="cancel",
                        )
                    except BaseException:
                        pass
                    raise self._build_action_loop_close_stall(timeout, planning_protocol) from error
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            with bind_runtime_context(
                parent_run_context=action_loop_run,
                tool_phase_run_context=action_loop_run,
                settings=settings,
            ):
                await publish_runtime_observation(
                    "loop_failed",
                    level="ERROR",
                    message=f"Action loop failed for agent '{ agent_name }'.",
                    compat_message=f"Tool loop failed for agent '{ agent_name }'.",
                    payload={"agent_name": agent_name},
                    error=error,
                )
            raise
        finally:
            if (
                owns_artifact_scope
                and not exchange_paused
                and not action_loop_completed
            ):
                release_standalone_artifact_scope_once()
        if isinstance(result, dict):
            result = result.get("action_loop_result", result.get("$final_result"))
        if not isinstance(result, list):
            if owns_artifact_scope and not exchange_paused:
                release_standalone_artifact_scope_once()
            return []
        normalized = action._to_action_flow_return_records(result)
        if owns_artifact_scope and not exchange_paused:
            release_standalone_artifact_scope_once()
        if owns_artifact_scope and not exchange_paused:
            normalized = action._project_released_artifact_scope(normalized, artifact_scope)
            for state_key in ("done_plans", "last_round_records", "action_loop_result"):
                state_value = execution.get_state(state_key, [])
                await execution.async_set_state(
                    state_key,
                    action._project_released_artifact_scope(state_value, artifact_scope),
                )
        normalized = action._to_action_flow_return_records(normalized)
        normalized = action.to_model_visible_records(normalized)
        with bind_runtime_context(
            parent_run_context=action_loop_run,
            tool_phase_run_context=action_loop_run,
            settings=settings,
        ):
            await publish_runtime_observation(
                "loop_paused" if exchange_paused else "loop_completed",
                level="WARNING" if exchange_paused else "INFO",
                message=(
                    f"Action loop paused on a pending human exchange for agent '{ agent_name }'."
                    if exchange_paused
                    else f"Action loop completed for agent '{ agent_name }'."
                ),
                compat_message=(
                    f"Tool loop paused for agent '{ agent_name }'."
                    if exchange_paused
                    else f"Tool loop completed for agent '{ agent_name }'."
                ),
                payload={
                    "agent_name": agent_name,
                    "record_count": len(normalized),
                    "exchange_paused": exchange_paused,
                },
            )
        return normalized

    @staticmethod
    async def _notify_agent_execution_exchange(
        action: str,
        exchanges: list[Any],
        *,
        interrupt_id: str | None = None,
    ):
        """Project an exchange lifecycle moment onto the owning AgentExecution.

        No-op for plain agent requests: the contextvar only carries an
        AgentExecutionContext when an AgentExecution owns this run.
        """
        context = get_current_agent_execution_context()
        notify = getattr(context, "async_notify_exchange", None)
        if callable(notify):
            result = notify(
                action,
                [item for item in exchanges if isinstance(item, dict)],
                meta={"interrupt_id": interrupt_id} if interrupt_id else None,
            )
            if inspect.isawaitable(result):
                await result

    def _record_agent_execution_progress(
        self,
        stage: str,
        status: str,
        planning_protocol: str | None,
    ):
        context = get_current_agent_execution_context()
        record_progress = getattr(context, "record_progress", None)
        if callable(record_progress):
            record_progress(
                stage=stage,
                status=status,
                event_type=f"{ stage }.{ status }",
                meta={"planning_protocol": planning_protocol},
            )

    def _build_action_loop_close_stall(
        self,
        timeout: float | None,
        planning_protocol: str | None,
    ) -> RuntimeStageStallError:
        context = get_current_agent_execution_context()
        now = time.monotonic()
        last_progress_at = float(getattr(context, "last_progress_at", now))
        last_event = getattr(context, "last_progress_event", None) or {}
        return RuntimeStageStallError(
            (
                "ActionFlow loop close did not complete before timeout"
                + (f": timeout={ timeout }." if timeout is not None else ".")
            ),
            stage="action_loop_close",
            status="stalled",
            idle_seconds=now - last_progress_at,
            timeout_seconds=float(timeout) if timeout is not None else None,
            last_progress_event=(
                str(last_event.get("event_type"))
                if isinstance(last_event, dict) and last_event.get("event_type") is not None
                else None
            ),
            planning_protocol=planning_protocol,
        )
