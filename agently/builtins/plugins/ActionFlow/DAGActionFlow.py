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

"""ActionFlow plugin using TaskDAGExecutor for the execute phase.

Replaces the serial ReAct execution loop with DAG-based scheduling:
- PLAN phase: same planning_handler call as TriggerFlowActionFlow
- EXECUTE phase: builds a TaskDAG from action_calls, executes via TaskDAGExecutor
  where each node handler calls action.async_execute_action() directly.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

from agently.core.runtime.RuntimeContext import (
    bind_runtime_context,
    get_current_agent_execution_context,
    resolve_parent_run_context,
)

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import ActionResult
    from agently.types.plugins import ActionFlowObservationHandler
    from agently.utils import Settings


class DAGActionFlow:
    name = "DAGActionFlow"
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
        runtime_observation_handler: "ActionFlowObservationHandler | None" = None,
    ) -> list["ActionResult"]:
        from agently.core.orchestration.TaskDAG import TaskDAGExecutor
        from agently.core.orchestration.TriggerFlow import TriggerFlow
        from agently.types.data import RunContext
        from agently.types.data.task_dag import TaskDAGNode

        if planning_handler is None:
            raise RuntimeError("[Agently DAGActionFlow] planning_handler is required.")

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

        if not isinstance(max_rounds, int) or max_rounds < 0:
            max_rounds = None
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
        artifact_scope = action._resolve_artifact_scope(
            run_context=action_loop_run,
            agent_execution_context=get_current_agent_execution_context(),
        )
        owns_artifact_scope = artifact_scope.get("kind") == "action_run"

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
                        "source": "DAGActionFlow",
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
                message=f"Action loop started for agent '{agent_name}'.",
                compat_message=f"Tool loop started for agent '{agent_name}'.",
                payload={
                    "agent_name": agent_name,
                    "action_count": len(action_list),
                    "tool_count": len(action_list),
                    "planning_protocol": action.action_runtime.resolve_planning_protocol(settings, planning_protocol),
                },
            )

        flow = TriggerFlow(name=f"dag-action-loop-{agent_name}")

        async def initialize_loop(data):
            await data.async_set_state("done_plans", [])
            await data.async_set_state("last_round_records", [])
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
            visible_action_list = action._with_action_artifact_recall_action(
                action_list,
                model_visible_last_round_records or model_visible_done_plans,
            )

            decision = action._normalize_action_decision(
                await planning_handler(
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
                message=f"Action plan ready for round {round_index}.",
                compat_message=f"Tool plan ready for round {round_index}.",
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
            if dispatch_confirmed:
                await data.async_emit("EXECUTE", decision.get("action_calls", []))
            else:
                await data.async_emit("DONE", done_plans)
            return decision

        async def execute_step_via_dag(data):
            action_calls = data.value if isinstance(data.value, list) else []
            round_index = data.get_state("round_index", 0)
            if not isinstance(round_index, int):
                round_index = 0
            done_plans = data.get_state("done_plans", [])
            if not isinstance(done_plans, list):
                done_plans = []

            if len(action_calls) == 0:
                await data.async_emit("PLAN", None)
                return []

            # Build DAG: each action_call becomes an independent node.
            # Future: model-generated depends_on relationships will create edges.
            nodes: list[dict[str, Any]] = []
            for command_index, command in enumerate(action_calls):
                action_id = str(command.get("action_id", command.get("tool_name", "unknown")))
                nodes.append({
                    "id": f"action_{round_index}_{command_index}_{action_id}",
                    "kind": "task",
                    "purpose": str(command.get("purpose", f"action_call_{command_index + 1}")),
                    "inputs": {
                        "action_id": action_id,
                        "kwargs": command.get("action_input", command.get("tool_kwargs", {})),
                        "command": command,
                    },
                    "depends_on": [],
                })

                # Emit per-action started events
                action_run = action_loop_run.create_child(
                    run_kind="action",
                    meta={
                        "action_type": "tool",
                        "action_name": action_id,
                        "round_index": round_index,
                        "command_index": command_index,
                    },
                )
                await publish_runtime_observation(
                    "action_started",
                    message=f"Action '{action_id}' started.",
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

            # Build resolver: map each task_id to a handler that calls the action
            resolver: dict[str, Any] = {}
            for command_index, command in enumerate(action_calls):
                action_id = str(command.get("action_id", command.get("tool_name", "")))
                task_id = f"action_{round_index}_{command_index}_{action_id}"
                resolver[task_id] = _make_dag_node_handler(
                    action=action,
                    action_id=action_id,
                    action_call=command,
                    command_index=command_index,
                    round_index=round_index,
                    agent_name=agent_name,
                    action_loop_run=action_loop_run,
                    artifact_scope=artifact_scope,
                )

            graph = {
                "graph_id": f"dag-action-round-{round_index}",
                "tasks": nodes,
                "task_schema_version": "task_dag/v1",
            }

            dag_executor = TaskDAGExecutor(resolver=resolver, name=f"dag-action-exec-{agent_name}-round-{round_index}")
            compiled_dag = dag_executor.compile(graph)
            dag_execution = compiled_dag.create_execution(
                auto_close=False,
                concurrency=concurrency,
                record_store=False,
            )
            await dag_execution.async_start({"round_index": round_index})
            result = await dag_execution.async_close(timeout=timeout)

            # Extract per-task results and collect them
            task_results: list[dict[str, Any]] = []
            task_result_map = result.get("task_results", {}) if isinstance(result, dict) else {}
            if not isinstance(task_result_map, dict):
                task_result_map = {}
            for command_index, command in enumerate(action_calls):
                action_id = str(command.get("action_id", command.get("tool_name", "")))
                task_id = f"action_{round_index}_{command_index}_{action_id}"
                task_result = task_result_map.get(task_id, {})
                if isinstance(task_result, dict):
                    task_results.append(task_result)
                else:
                    task_results.append({
                        "ok": False,
                        "status": "error",
                        "action_id": action_id,
                        "error": str(task_result),
                    })

            # Normalize and finalize records
            records = action._normalize_execution_records(
                task_results,
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
                    source="DAGActionFlow",
                )
            for record_index, record in enumerate(bounded_records):
                action_id = record.get("action_id", record.get("tool_name", "unknown"))
                success = bool(record.get("success"))
                status = str(record.get("status", "") or "")
                if success:
                    event_kind = "action_completed"
                    event_level = "INFO"
                    event_message = f"Action '{action_id}' completed."
                elif status == "approval_required":
                    event_kind = "action_approval_required"
                    event_level = "WARNING"
                    event_message = f"Action '{action_id}' requires approval."
                elif status == "blocked":
                    event_kind = "action_blocked"
                    event_level = "WARNING"
                    event_message = f"Action '{action_id}' blocked."
                else:
                    event_kind = "action_failed"
                    event_level = "WARNING"
                    event_message = f"Action '{action_id}' failed."
                action_run = action_loop_run.create_child(
                    run_kind="action",
                    meta={
                        "action_type": "tool",
                        "action_name": str(action_id),
                        "round_index": round_index,
                        "command_index": record_index,
                    },
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
            await data.async_emit("PLAN", None)
            return state_records

        async def finalize_loop(data):
            result = data.value if isinstance(data.value, list) else []
            await data.async_set_state("action_loop_result", result)
            await data.async_set_state("done_plans", result)
            return result

        flow.to(initialize_loop)
        flow.when("PLAN").to(plan_step)
        flow.when("EXECUTE").to(execute_step_via_dag)
        flow.when("DONE").to(finalize_loop)

        execution = flow.create_execution(
            parent_run_context=action_loop_run,
            record_store=False,
            auto_close=False,
        )
        action_loop_completed = False
        try:
            with bind_runtime_context(
                parent_run_context=action_loop_run,
                tool_phase_run_context=action_loop_run,
                settings=settings,
            ):
                await execution.async_start()
                result = await execution.async_close(timeout=timeout)
                action_loop_completed = True
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
                    message=f"Action loop failed for agent '{agent_name}'.",
                    compat_message=f"Tool loop failed for agent '{agent_name}'.",
                    payload={"agent_name": agent_name},
                    error=error,
                )
            raise
        finally:
            if owns_artifact_scope and not action_loop_completed:
                action._release_artifact_scope(artifact_scope)
        if isinstance(result, dict):
            result = result.get("action_loop_result", result.get("$final_result"))
        if not isinstance(result, list):
            if owns_artifact_scope:
                action._release_artifact_scope(artifact_scope)
            return []
        normalized = action._to_action_flow_return_records(result)
        if owns_artifact_scope:
            action._release_artifact_scope(artifact_scope)
        if owns_artifact_scope:
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
                "loop_completed",
                message=f"Action loop completed for agent '{agent_name}'.",
                compat_message=f"Tool loop completed for agent '{agent_name}'.",
                payload={
                    "agent_name": agent_name,
                    "record_count": len(normalized),
                },
            )
        return normalized


def _make_dag_node_handler(
    *,
    action,
    action_id: str,
    action_call: dict[str, Any],
    command_index: int,
    round_index: int,
    agent_name: str,
    action_loop_run,
    artifact_scope: dict[str, str],
):
    """Create a handler for a single action node in the DAG."""

    async def handler(ctx) -> dict[str, Any]:
        kwargs = action_call.get("action_input", action_call.get("tool_kwargs", {}))
        if not isinstance(kwargs, dict):
            kwargs = {}

        purpose = str(action_call.get("purpose", f"action_call_{command_index + 1}"))
        source_protocol = str(action_call.get("source_protocol", "dag_flow"))

        try:
            result = await action.async_execute_action(
                action_id,
                kwargs,
                purpose=purpose,
                source_protocol=source_protocol,
                todo_suggestion=str(action_call.get("todo_suggestion", action_call.get("next", ""))),
                next_value=str(action_call.get("todo_suggestion", action_call.get("next", ""))),
                artifact_scope=artifact_scope,
            )
        except asyncio.TimeoutError:
            result = {
                "ok": False,
                "status": "error",
                "action_id": action_id,
                "error": f"Action '{action_id}' timed out.",
                "purpose": purpose,
            }
        except Exception as exc:
            result = {
                "ok": False,
                "status": "error",
                "action_id": action_id,
                "error": str(exc),
                "purpose": purpose,
            }

        return dict(result) if isinstance(result, dict) else {"result": result, "action_id": action_id}

    return handler
