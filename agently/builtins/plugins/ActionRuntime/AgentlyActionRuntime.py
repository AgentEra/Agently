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
import json
import time
from typing import TYPE_CHECKING, Any, cast

from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.runtime.RuntimeContext import get_current_agent_execution_context, get_current_tool_phase_run_context
from agently.utils import SettingsNamespace

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import (
        ActionCall,
        ActionDecision,
        ActionDiagnostic,
        ActionExecutionRequest,
        ActionPlanningRequest,
        ActionResult,
        ActionRunContext,
        RunContext,
    )
    from agently.utils import Settings


def _get_model_request_result(request: Any, *, parent_run_context: "RunContext | None" = None) -> Any:
    getter = getattr(request, "get_result", None) or getattr(request, "get_response")
    return getter(parent_run_context=parent_run_context)


class AgentlyActionRuntime:
    name = "AgentlyActionRuntime"
    DEFAULT_SETTINGS = {}

    def __init__(self, *, action, plugin_manager, settings: "Settings"):
        self.action = action
        self.plugin_manager = plugin_manager
        self.settings = settings
        self.action_settings = SettingsNamespace(self.settings, "action")
        self.tool_settings = SettingsNamespace(self.settings, "tool")
        self._planning_handler = self._default_planning_handler
        self._execution_handler = self._default_action_execution_handler

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def register_action_planning_handler(self, handler):
        if handler is None:
            self._planning_handler = self._default_planning_handler
        else:
            self._planning_handler = default_stage_call_bridge.as_async(handler)
        return self

    def register_action_execution_handler(self, handler):
        if handler is None:
            self._execution_handler = self._default_action_execution_handler
        else:
            self._execution_handler = default_stage_call_bridge.as_async(handler)
        return self

    def resolve_planning_handler(self, handler=None):
        selected = handler if handler is not None else self._planning_handler
        if selected is None:
            raise RuntimeError("[Agently Action] Action planning handler is required.")
        resolved = default_stage_call_bridge.as_async(selected)

        async def wrapped(context, request):
            settings = context.get("settings", self.settings) if isinstance(context, dict) else self.settings
            planning_protocol = self.resolve_planning_protocol(settings, request.get("planning_protocol"))
            stage = "tool_call_selection" if planning_protocol == "native_tool_calls" else "action_planning"
            return await self._run_runtime_stage(
                stage,
                resolved(cast(Any, context), cast(Any, request)),
                settings=settings,
                planning_protocol=planning_protocol,
            )

        return wrapped

    def resolve_execution_handler(self, handler=None):
        selected = handler if handler is not None else self._execution_handler
        if selected is None:
            raise RuntimeError("[Agently Action] Action execution handler is required.")
        resolved = default_stage_call_bridge.as_async(selected)

        async def wrapped(context, request):
            settings = context.get("settings", self.settings) if isinstance(context, dict) else self.settings
            return await self._run_runtime_stage(
                "action_execution",
                resolved(cast(Any, context), cast(Any, request)),
                settings=settings,
                planning_protocol=request.get("planning_protocol") if isinstance(request, dict) else None,
            )

        return wrapped

    def resolve_planning_protocol(self, settings: "Settings", planning_protocol: str | None = None):
        candidate = planning_protocol
        if not isinstance(candidate, str) or candidate.strip() == "":
            candidate = cast(str | None, SettingsNamespace(settings, "action").get("protocol", None))
        if not isinstance(candidate, str) or candidate.strip() == "":
            candidate = cast(str | None, self.action_settings.get("protocol", None))
        if not isinstance(candidate, str) or candidate.strip() == "":
            candidate = "structured_plan"
        candidate = candidate.strip().lower()
        if candidate not in {"structured_plan", "native_tool_calls"}:
            return "structured_plan"
        return candidate

    async def _run_runtime_stage(
        self,
        stage: str,
        awaitable,
        *,
        settings: "Settings",
        planning_protocol: str | None = None,
    ):
        context = cast(Any, get_current_agent_execution_context())
        if callable(getattr(context, "record_progress", None)):
            context.record_progress(
                stage=stage,
                status="started",
                event_type=f"{ stage }.started",
                meta={"planning_protocol": planning_protocol},
            )
        idle_limit = self._resolve_stage_idle_timeout(settings, context)
        if idle_limit is None:
            result = await awaitable
            if callable(getattr(context, "record_progress", None)):
                context.record_progress(
                    stage=stage,
                    status="completed",
                    event_type=f"{ stage }.completed",
                    meta={"planning_protocol": planning_protocol},
                )
            return result

        task = asyncio.create_task(awaitable)
        try:
            while True:
                last_progress_at = float(getattr(context, "last_progress_at", time.monotonic()))
                timeout = max(0.0, (last_progress_at + idle_limit) - time.monotonic())
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                    if callable(getattr(context, "record_progress", None)):
                        context.record_progress(
                            stage=stage,
                            status="completed",
                            event_type=f"{ stage }.completed",
                            meta={"planning_protocol": planning_protocol},
                        )
                    return result
                except asyncio.TimeoutError as error:
                    if task.done():
                        return await task
                    now = time.monotonic()
                    idle_seconds = now - float(getattr(context, "last_progress_at", now))
                    if idle_seconds < idle_limit:
                        continue
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    last_event = getattr(context, "last_progress_event", None) or {}
                    raise RuntimeStageStallError(
                        (
                            f"ActionRuntime stage '{ stage }' made no progress before idle deadline: "
                            f"max_no_progress_seconds={ idle_limit }."
                        ),
                        stage=stage,
                        status="stalled",
                        idle_seconds=idle_seconds,
                        timeout_seconds=idle_limit,
                        last_progress_event=(
                            str(last_event.get("event_type"))
                            if isinstance(last_event, dict) and last_event.get("event_type") is not None
                            else None
                        ),
                        planning_protocol=planning_protocol,
                    ) from error
        except BaseException:
            if not task.done():
                task.cancel()
            raise

    def _resolve_stage_idle_timeout(self, settings: "Settings", context: Any) -> float | None:
        for namespace in (
            SettingsNamespace(settings, "action"),
            self.action_settings,
            SettingsNamespace(settings, "runtime"),
        ):
            raw_timeout = namespace.get("stage_idle_timeout", None)
            timeout = self._normalize_timeout(raw_timeout)
            if timeout is not None:
                return timeout
        limits = getattr(context, "limits", None)
        if isinstance(limits, dict):
            return self._normalize_timeout(limits.get("max_no_progress_seconds"))
        return None

    @staticmethod
    def _normalize_timeout(value: Any) -> float | None:
        if value is None or value == -1 or value == "-1":
            return None
        if isinstance(value, bool):
            return None
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return None
        return timeout if timeout > 0 else None

    @staticmethod
    def _record_agent_execution_progress(
        *,
        stage: str,
        status: str,
        event_type: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        context = get_current_agent_execution_context()
        record_progress = getattr(context, "record_progress", None)
        if callable(record_progress):
            record_progress(stage=stage, status=status, event_type=event_type, meta=meta or {})

    @staticmethod
    def _resolve_planning_model_key(settings: Any) -> str | None:
        value = settings.get("action.planning_model_key", None)
        if value is None:
            value = settings.get("tool.planning_model_key", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _resolve_round_dispatch_policy(settings: Any) -> str | None:
        value = settings.get("action.loop.round_dispatch_policy", None)
        if value is None:
            value = settings.get("tool.loop.round_dispatch_policy", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    async def _default_structured_planning_handler(
        self,
        context: "ActionRunContext",
        request: "ActionPlanningRequest",
    ) -> "ActionDecision":
        from agently.core import ModelRequest

        prompt = context["prompt"]
        settings = context["settings"]
        agent_name = str(context.get("agent_name", "Manual"))
        action_list = request.get("action_list", [])
        done_plans = context.get("done_plans", [])
        last_round_records = context.get("last_round_records", [])
        round_index = context.get("round_index", 0)
        max_rounds = context.get("max_rounds", None)
        round_dispatch_policy = self._resolve_round_dispatch_policy(settings)
        planning_instructions = [
            "Plan next actions to respond to {input.user_input} with {input.available_actions}.",
            "Decide this round action first via 'next_action': 'execute' or 'response'.",
            "If next_action is 'response', return empty 'execution_commands'.",
            (
                "If next_action is 'execute', return one or more 'execution_commands'; under "
                "round_dispatch_policy='single_action_id_cohort', every command in this round "
                "must use the same action_id. Wait for the next round and consume this round's "
                "results before planning calls with a different action_id."
                if round_dispatch_policy == "single_action_id_cohort"
                else "If next_action is 'execute', return one or more 'execution_commands' for parallel execution."
            ),
            "Each command must include 'todo_suggestion' for next round decision making.",
            "Use {info.done_plans}, {info.last_round_result}, {info.round_index}, and {info.max_rounds} for decision.",
        ]

        parent_run_context = get_current_tool_phase_run_context()
        action_plan_request = ModelRequest(
            self.plugin_manager,
            parent_settings=settings,
            agent_name=agent_name,
            model_key=self._resolve_planning_model_key(settings),
        )
        action_plan_request.input(
            {
                "user_input": prompt.get("input"),
                "user_extra_requirement": prompt.get("instruct"),
                "available_actions": action_list,
            }
        ).info(
            {
                "done_plans": done_plans,
                "last_round_result": last_round_records,
                "round_index": round_index,
                "max_rounds": max_rounds,
                "round_dispatch_policy": round_dispatch_policy,
            }
        ).instruct(planning_instructions).output(
            {
                "next_action": ("'execute' | 'response'", "This round action decision."),
                "execution_commands": [
                    {
                        "purpose": (str, "What this action call collects or verifies."),
                        "action_id": (str, "Must in {input.available_actions.[].name}"),
                        "action_input": (dict, "kwargs dict as {input.available_actions.[].kwargs} of {action_id}"),
                        "todo_suggestion": (str, "Suggestion for next round's next_action decision."),
                    }
                ],
            },
            format="json",
        )
        action_plan_result = _get_model_request_result(
            action_plan_request,
            parent_run_context=parent_run_context,
        )
        result_reader = getattr(action_plan_result, "result", action_plan_result)
        result = await result_reader.async_get_data()
        if not isinstance(result, dict):
            return {"next_action": "response", "execution_commands": []}
        return cast("ActionDecision", result)

    async def _default_native_tool_call_planning_handler(
        self,
        context: "ActionRunContext",
        request: "ActionPlanningRequest",
    ) -> "ActionDecision":
        from agently.core import ModelRequest

        prompt = context["prompt"]
        settings = context["settings"]
        agent_name = str(context.get("agent_name", "Manual"))
        action_list = request.get("action_list", [])
        done_plans = context.get("done_plans", [])
        last_round_records = context.get("last_round_records", [])
        round_index = context.get("round_index", 0)
        max_rounds = context.get("max_rounds", None)
        round_dispatch_policy = self._resolve_round_dispatch_policy(settings)

        parent_run_context = get_current_tool_phase_run_context()
        action_request = ModelRequest(
            self.plugin_manager,
            parent_settings=settings,
            agent_name=agent_name,
            model_key=self._resolve_planning_model_key(settings),
        )
        action_request.input(
            {
                "user_input": prompt.get("input"),
                "user_extra_requirement": prompt.get("instruct"),
                "available_actions": action_list,
            }
        ).info(
            {
                "done_plans": done_plans,
                "last_round_result": last_round_records,
                "round_index": round_index,
                "max_rounds": max_rounds,
                "round_dispatch_policy": round_dispatch_policy,
            }
        ).instruct(
            [
                "Decide whether native tool calls are required to answer {input.user_input}.",
                (
                    "If a tool is needed, emit one or more native tool calls that all use the same "
                    "action_id in this round. Wait for the next round's results before choosing a "
                    "different action_id."
                    if round_dispatch_policy == "single_action_id_cohort"
                    else "If a tool is needed, emit native tool calls for one or more available actions."
                ),
                "If no tool is needed, answer directly without emitting tool calls.",
            ]
        )
        action_request.prompt.set("tools", action_list)
        result = _get_model_request_result(action_request, parent_run_context=parent_run_context)
        tool_call_chunks: list[Any] = []
        text_fragments: list[str] = []
        async for event, data in result.get_async_generator(type="specific", specific=["tool_calls", "delta", "done"]):
            if event == "tool_calls":
                tool_call_chunks.append(data)
            elif event in {"message", "delta", "text"} and data:
                text_fragments.append(str(data))
            elif event == "done":
                break
        action_calls = self.action._normalize_native_action_calls(tool_call_chunks)
        if len(action_calls) == 0:
            diagnostic = cast("ActionDiagnostic", {
                "source": "ActionRuntime",
                "severity": "warning",
                "code": "action_runtime.native_tool_calls.empty",
                "message": (
                    "Native tool-call planning returned no executable tool calls. "
                    "The host should treat this as a planning diagnostic rather than executed action evidence."
                ),
                "meta": {
                    "planning_protocol": "native_tool_calls",
                    "textual_tool_markup_detected": any(
                        marker in "".join(text_fragments).lower()
                        for marker in ("<bash", "<tool", "<command", "```bash")
                    ),
                },
            })
            return {
                "next_action": "response",
                "use_action": False,
                "action_calls": [],
                "tool_commands": [],
                "diagnostics": [diagnostic],
            }
        return {
            "next_action": "execute",
            "use_action": True,
            "action_calls": action_calls,
            "tool_commands": action_calls,
            "execution_commands": action_calls,
        }

    async def _default_planning_handler(
        self,
        context: "ActionRunContext",
        request: "ActionPlanningRequest",
    ) -> "ActionDecision":
        settings = context["settings"]
        planning_protocol = self.resolve_planning_protocol(settings, request.get("planning_protocol"))
        if planning_protocol == "native_tool_calls":
            return await self._default_native_tool_call_planning_handler(context, request)
        return await self._default_structured_planning_handler(context, request)

    async def _default_action_execution_handler(
        self,
        context: "ActionRunContext",
        request: "ActionExecutionRequest",
    ) -> list["ActionResult"]:
        from agently.core.orchestration.TriggerFlow import TriggerFlow

        settings = context["settings"]
        action_calls = cast(list["ActionCall"], request.get("action_calls", []))
        concurrency = request.get("concurrency", None)
        timeout = request.get("timeout", None)
        trusted_policy_overrides = request.get("trusted_policy_overrides", {})
        if not isinstance(trusted_policy_overrides, dict):
            trusted_policy_overrides = {}
        artifact_scope = context.get("artifact_scope")
        if not isinstance(artifact_scope, dict):
            artifact_scope = None
        if len(action_calls) == 0:
            return []
        if self.action.async_execute_action is None:
            raise RuntimeError("[Agently Action] Action dispatcher is not available.")

        async def run_one(data):
            action_call = data.input
            if not isinstance(action_call, dict):
                action_call = {}
            action_id = str(action_call.get("action_id", ""))
            action_input = action_call.get("action_input", {})
            if not isinstance(action_input, dict):
                action_input = {}
            purpose = str(action_call.get("purpose", f"Use { action_id }"))
            next_step = str(action_call.get("todo_suggestion", action_call.get("next", "")))
            policy_override = action_call.get("policy_override", {})
            if not isinstance(policy_override, dict):
                policy_override = {}
            command_key = getattr(data, "index", None)
            if command_key is None:
                # for_each fan-out items carry no index attribute; locate the
                # command position in the original call list instead.
                try:
                    command_key = action_calls.index(cast("ActionCall", action_call))
                except ValueError:
                    command_key = None
            trusted_policy_override = trusted_policy_overrides.get(command_key)
            if trusted_policy_override is None and command_key is not None:
                trusted_policy_override = trusted_policy_overrides.get(str(command_key))
            if not isinstance(trusted_policy_override, dict):
                trusted_policy_override = None

            async def execute_once():
                command_index = getattr(data, "index", None)
                progress_meta = {"action_id": action_id, "command_index": command_index}
                self._record_agent_execution_progress(
                    stage=f"actions.{action_id}" if action_id else "actions.unknown",
                    status="started",
                    event_type="action.started",
                    meta=progress_meta,
                )
                return await self.action.async_execute_action(
                    action_id,
                    action_input,
                    settings=settings,
                    purpose=purpose,
                    policy_override=policy_override,
                    trusted_policy_override=trusted_policy_override,
                    source_protocol=str(action_call.get("source_protocol", "structured_plan")),
                    todo_suggestion=next_step,
                    next_value=next_step,
                    artifact_scope=artifact_scope,
                )

            try:
                result = await execute_once()
            except BaseException:
                self._record_agent_execution_progress(
                    stage=f"actions.{action_id}" if action_id else "actions.unknown",
                    status="failed",
                    event_type="action.failed",
                    meta={"action_id": action_id, "command_index": getattr(data, "index", None)},
                )
                raise
            status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else ""
            self._record_agent_execution_progress(
                stage=f"actions.{action_id}" if action_id else "actions.unknown",
                status=status or "completed",
                event_type="action.completed",
                meta={"action_id": action_id, "command_index": getattr(data, "index", None)},
            )
            return result

        async def collect_results(data):
            values = data.input if isinstance(data.input, list) else []
            await data.async_set_state("results", values)
            return values

        flow = TriggerFlow(name="action-runtime-execute-actions")
        flow.for_each(concurrency=concurrency).to(run_one).end_for_each().to(collect_results)
        execution = flow.create_execution(auto_close=False, record_store=False)
        await execution.async_start(list(action_calls))
        close_timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else None
        snapshot = await execution.async_close(timeout=close_timeout)
        results = snapshot.get("results")
        return cast(list["ActionResult"], results if isinstance(results, list) else [])

    async def async_generate_action_call(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        planning_handler=None,
        done_plans: list["ActionResult"] | None = None,
        last_round_records: list["ActionResult"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list["ActionCall"]:
        if len(action_list) == 0:
            return []

        standard_planning_handler = self.resolve_planning_handler(planning_handler)
        if max_rounds is None:
            configured_max_rounds = self.action_settings.get(
                "loop.max_rounds",
                self.tool_settings.get("loop.max_rounds", None),
            )
            max_rounds = configured_max_rounds if isinstance(configured_max_rounds, int) else None
        if not isinstance(max_rounds, int) or max_rounds < 0:
            max_rounds = None

        safe_done_plans = self.action.to_model_visible_records(done_plans if isinstance(done_plans, list) else [])
        safe_last_round_records = self.action.to_model_visible_records(
            last_round_records if isinstance(last_round_records, list) else []
        )
        visible_action_list = self.action._with_action_artifact_recall_action(
            action_list,
            safe_last_round_records or safe_done_plans,
        )
        if not isinstance(round_index, int) or round_index < 0:
            round_index = 0

        decision = self.action._normalize_action_decision(
            await standard_planning_handler(
                {
                    "prompt": prompt,
                    "settings": settings,
                    "agent_name": agent_name,
                    "round_index": round_index,
                    "max_rounds": max_rounds,
                    "done_plans": safe_done_plans,
                    "last_round_records": safe_last_round_records,
                    "action": self.action,
                    "runtime": self,
                },
                {
                    "action_list": visible_action_list,
                    "planning_protocol": planning_protocol,
                },
            )
        )
        commands = decision.get("action_calls", [])
        return commands if isinstance(commands, list) else []

    async def async_generate_tool_command(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        tool_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        plan_analysis_handler=None,
        done_plans: list["ActionResult"] | None = None,
        last_round_records: list["ActionResult"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ActionCall"]:
        return await self.async_generate_action_call(
            prompt=prompt,
            settings=settings,
            action_list=tool_list,
            agent_name=agent_name,
            planning_handler=plan_analysis_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol="structured_plan",
        )
