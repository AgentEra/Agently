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
import json
from typing import TYPE_CHECKING, Any, cast

from agently.core.RuntimeContext import get_current_tool_phase_run_context
from agently.utils import FunctionShifter, SettingsNamespace

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import (
        ActionCall,
        ActionDecision,
        ActionExecutionRequest,
        ActionPlanningRequest,
        ActionResult,
        ActionRunContext,
    )
    from agently.utils import Settings


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
            self._planning_handler = FunctionShifter.asyncify(handler)
        return self

    def register_action_execution_handler(self, handler):
        if handler is None:
            self._execution_handler = self._default_action_execution_handler
        else:
            self._execution_handler = FunctionShifter.asyncify(handler)
        return self

    def resolve_planning_handler(self, handler=None):
        selected = handler if handler is not None else self._planning_handler
        if selected is None:
            raise RuntimeError("[Agently Action] Action planning handler is required.")
        return FunctionShifter.asyncify(selected)

    def resolve_execution_handler(self, handler=None):
        selected = handler if handler is not None else self._execution_handler
        if selected is None:
            raise RuntimeError("[Agently Action] Action execution handler is required.")
        return FunctionShifter.asyncify(selected)

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

        parent_run_context = get_current_tool_phase_run_context()
        action_plan_request = ModelRequest(
            self.plugin_manager,
            parent_settings=settings,
            agent_name=agent_name,
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
            }
        ).instruct(
            [
                "Plan next actions to respond to {input.user_input} with {input.available_actions}.",
                "Decide this round action first via 'next_action': 'execute' or 'response'.",
                "If next_action is 'response', return empty 'execution_commands'.",
                "If next_action is 'execute', return one or more 'execution_commands' for parallel execution.",
                "Each command must include 'todo_suggestion' for next round decision making.",
                "Use {info.done_plans}, {info.last_round_result}, {info.round_index}, and {info.max_rounds} for decision.",
            ]
        ).output(
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
            }
        )
        action_plan_response = action_plan_request.get_response(parent_run_context=parent_run_context)
        async for instant in action_plan_response.get_async_generator(type="instant"):
            if not instant.is_complete:
                continue
            if not self.action._is_next_action_path(instant.path):
                continue
            if isinstance(instant.value, str) and instant.value.strip().lower() == "response":
                await self.action._try_close_response_stream(action_plan_response)
                return {
                    "next_action": "response",
                    "execution_commands": [],
                }
            break
        result = await action_plan_response.result.async_get_data()
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

        parent_run_context = get_current_tool_phase_run_context()
        action_request = ModelRequest(
            self.plugin_manager,
            parent_settings=settings,
            agent_name=agent_name,
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
            }
        ).instruct(
            [
                "Decide whether native tool calls are required to answer {input.user_input}.",
                "If a tool is needed, emit native tool calls for one or more available actions.",
                "If no tool is needed, answer directly without emitting tool calls.",
            ]
        )
        action_request.prompt.set("tools", action_list)
        response = action_request.get_response(parent_run_context=parent_run_context)
        tool_call_chunks: list[Any] = []
        async for event, data in response.get_async_generator(type="specific", specific=["tool_calls", "done"]):
            if event == "tool_calls":
                tool_call_chunks.append(data)
            elif event == "done":
                break
        action_calls = self.action._normalize_native_action_calls(tool_call_chunks)
        if len(action_calls) == 0:
            return {
                "next_action": "response",
                "use_action": False,
                "action_calls": [],
                "tool_commands": [],
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
        settings = context["settings"]
        action_calls = request.get("action_calls", [])
        concurrency = request.get("concurrency", None)
        if len(action_calls) == 0:
            return []
        if self.action.async_execute_action is None:
            raise RuntimeError("[Agently Action] Action dispatcher is not available.")

        semaphore = asyncio.Semaphore(concurrency) if isinstance(concurrency, int) and concurrency > 0 else None

        async def run_one(action_call: "ActionCall"):
            action_id = str(action_call.get("action_id", ""))
            action_input = action_call.get("action_input", {})
            if not isinstance(action_input, dict):
                action_input = {}
            purpose = str(action_call.get("purpose", f"Use { action_id }"))
            next_step = str(action_call.get("todo_suggestion", action_call.get("next", "")))
            policy_override = action_call.get("policy_override", {})
            if not isinstance(policy_override, dict):
                policy_override = {}

            async def execute_once():
                return await self.action.async_execute_action(
                    action_id,
                    action_input,
                    settings=settings,
                    purpose=purpose,
                    policy_override=policy_override,
                    source_protocol=str(action_call.get("source_protocol", "structured_plan")),
                    todo_suggestion=next_step,
                    next_value=next_step,
                )

            if semaphore is None:
                return await execute_once()
            async with semaphore:
                return await execute_once()

        return await asyncio.gather(*[run_one(action_call) for action_call in action_calls])

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
            configured_max_rounds = self.action_settings.get("loop.max_rounds", self.tool_settings.get("loop.max_rounds", 5))
            max_rounds = configured_max_rounds if isinstance(configured_max_rounds, int) else 5
        if not isinstance(max_rounds, int) or max_rounds < 0:
            max_rounds = 5

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
