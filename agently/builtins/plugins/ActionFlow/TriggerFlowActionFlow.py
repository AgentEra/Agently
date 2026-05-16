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

from typing import TYPE_CHECKING, Any

from agently.core.RuntimeContext import bind_runtime_context, resolve_parent_run_context
from agently.types.data import ErrorInfo

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import ActionResult
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
    ) -> list["ActionResult"]:
        from agently.base import async_emit_runtime
        from agently.core.TriggerFlow import TriggerFlow
        from agently.types.data import ObservationEvent, RunContext

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
            max_rounds = action.action_settings.get("loop.max_rounds", action.tool_settings.get("loop.max_rounds", 5))
        if concurrency is None:
            concurrency = action.action_settings.get(
                "loop.concurrency",
                action.tool_settings.get("loop.concurrency", None),
            )
        if timeout is None:
            timeout = action.action_settings.get("loop.timeout", action.tool_settings.get("loop.timeout", None))

        if not isinstance(max_rounds, int) or max_rounds < 0:
            max_rounds = 5
        if not isinstance(concurrency, int) or concurrency <= 0:
            concurrency = None
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            timeout = None

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

        async def emit_action_loop_event(
            event_type: str,
            *,
            message: str,
            payload: dict[str, Any],
            level: str = "INFO",
            error: BaseException | None = None,
            compat_event_family: str | None = "tool",
            compat_message: str | None = None,
        ):
            primary_event = ObservationEvent(
                event_type=event_type,
                source="ActionFlow",
                level=level,  # type: ignore[arg-type]
                message=message,
                payload=payload,
                error=ErrorInfo.from_exception(error) if error is not None else None,
                run=action_loop_run,
            )
            await async_emit_runtime(primary_event)

            tool_aliases = {
                "action.loop_started": "tool.loop_started",
                "action.plan_ready": "tool.plan_ready",
                "action.loop_failed": "tool.loop_failed",
                "action.loop_completed": "tool.loop_completed",
            }
            if compat_event_family != "tool" or event_type not in tool_aliases:
                return
            await async_emit_runtime(
                ObservationEvent(
                    event_type=tool_aliases[event_type],
                    source=primary_event.source,
                    level=primary_event.level,
                    message=compat_message or primary_event.message,
                    payload=primary_event.payload,
                    error=primary_event.error,
                    run=primary_event.run,
                    meta={
                        "compat_event_alias": True,
                        "compat_alias_for": primary_event.event_type,
                        "primary_event_id": primary_event.event_id,
                        "compat_family": "tool",
                    },
                )
            )

        with bind_runtime_context(
            parent_run_context=action_loop_run,
            tool_phase_run_context=action_loop_run,
            settings=settings,
        ):
            await emit_action_loop_event(
                "action.loop_started",
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
            data.set_state("done_plans", [])
            data.set_state("last_round_records", [])
            data.set_state("round_index", 0)
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

            await emit_action_loop_event(
                "action.plan_ready",
                message=f"Action plan ready for round { round_index }.",
                compat_message=f"Tool plan ready for round { round_index }.",
                payload={
                    "agent_name": agent_name,
                    "round_index": round_index,
                    "decision": decision,
                },
            )
            if action._should_continue(decision, round_index=round_index, max_rounds=max_rounds):
                await data.async_emit("EXECUTE", decision.get("action_calls", []))
            else:
                await data.async_emit("DONE", done_plans)
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
                await async_emit_runtime(
                    {
                        "event_type": "action.started",
                        "source": "ActionFlow",
                        "message": f"Action '{ action_id }' started.",
                        "payload": {
                            "agent_name": agent_name,
                            "round_index": round_index,
                            "command_index": command_index,
                            "action_type": "tool",
                            "action_name": action_id,
                            "command": command,
                        },
                        "run": action_run,
                    }
                )

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
                        "action": action,
                        "runtime": action.action_runtime,
                    },
                    {
                        "action_calls": action_calls,
                        "async_call_action": action.async_call_action,
                        "concurrency": concurrency,
                        "timeout": timeout,
                    },
                ),
                action_calls,
            )

            for record_index, record in enumerate(records):
                action_id = record.get("action_id", record.get("tool_name", "unknown"))
                success = bool(record.get("success"))
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
                await async_emit_runtime(
                    {
                        "event_type": "action.completed" if success else "action.failed",
                        "source": "ActionFlow",
                        "level": "INFO" if success else "WARNING",
                        "message": f"Action '{ action_id }' {'completed' if success else 'failed'}.",
                        "payload": {
                            "agent_name": agent_name,
                            "round_index": round_index,
                            "record_index": record_index,
                            "action_type": "tool",
                            "action_name": str(action_id),
                            "record": record,
                        },
                        "run": action_run,
                    }
                )

            done_plans.extend(records)
            data.set_state("done_plans", done_plans)
            data.set_state("last_round_records", records)
            data.set_state("round_index", round_index + 1)
            await data.async_emit("PLAN", None)
            return records

        flow.to(initialize_loop)
        flow.when("PLAN").to(plan_step)
        flow.when("EXECUTE").to(execute_step)
        flow.when("DONE").to(lambda data: data.value).end()

        execution = flow.create_execution(parent_run_context=action_loop_run)
        try:
            with bind_runtime_context(
                parent_run_context=action_loop_run,
                tool_phase_run_context=action_loop_run,
                settings=settings,
            ):
                await execution.async_start(wait_for_result=False)
                result = await execution.async_close(timeout=timeout)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            with bind_runtime_context(
                parent_run_context=action_loop_run,
                tool_phase_run_context=action_loop_run,
                settings=settings,
            ):
                await emit_action_loop_event(
                    "action.loop_failed",
                    level="ERROR",
                    message=f"Action loop failed for agent '{ agent_name }'.",
                    compat_message=f"Tool loop failed for agent '{ agent_name }'.",
                    payload={"agent_name": agent_name},
                    error=error,
                )
            raise
        if isinstance(result, dict):
            result = result.get("$final_result")
        if not isinstance(result, list):
            return []
        normalized = [
            action._finalize_action_result(action._normalize_execution_record(record, None, index))
            for index, record in enumerate(result)
        ]
        with bind_runtime_context(
            parent_run_context=action_loop_run,
            tool_phase_run_context=action_loop_run,
            settings=settings,
        ):
            await emit_action_loop_event(
                "action.loop_completed",
                message=f"Action loop completed for agent '{ agent_name }'.",
                compat_message=f"Tool loop completed for agent '{ agent_name }'.",
                payload={
                    "agent_name": agent_name,
                    "record_count": len(normalized),
                },
            )
        return normalized
