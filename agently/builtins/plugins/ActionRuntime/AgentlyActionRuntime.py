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
import inspect

import time
import uuid
from typing import TYPE_CHECKING, Any, cast

from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.runtime.RuntimeContext import (
    bind_runtime_context,
    get_current_agent_execution_context,
    get_current_tool_phase_run_context,
)
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
        self._programmatic_catalogs: dict[str, dict[str, Any]] = {}

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
        if candidate not in {
            "structured_plan",
            "native_tool_calls",
            "programmatic",
        }:
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

    @staticmethod
    def _mark_action_planning_request(request: Any) -> None:
        request_settings = getattr(request, "settings", None)
        set_value = getattr(request_settings, "set", None)
        if callable(set_value):
            set_value("$model_request.role", "action_planning")

    @staticmethod
    def _seed_action_planning_prompt(request: Any, source_prompt: Any) -> Any:
        """Materialize the prepared prompt so the decision output can replace it.

        Prompt inheritance recursively merges mapping slots.  An Action decision
        envelope must retain every prepared request-local slot except the original
        output schema, so using the prepared Prompt as a live parent would merge
        both output contracts.  A local snapshot preserves the prompt while letting
        ``request.output(...)`` replace the model-facing schema cleanly.
        """

        request_prompt = getattr(request, "prompt", None)
        source_get = getattr(source_prompt, "get", None)
        prompt_set = getattr(request_prompt, "set", None)
        if request_prompt is None or not callable(source_get) or not callable(prompt_set):
            return request_prompt
        source_data = source_get()
        if isinstance(source_data, dict):
            for key, value in source_data.items():
                prompt_set(key, value)
        return request_prompt

    @staticmethod
    def _final_response_contract(prompt: Any) -> dict[str, Any]:
        output = prompt.get("output", default=None)
        output_format = prompt.get("output_format", default=None)
        if not output:
            return {
                "kind": "text",
                "instruction": "Return the complete user-facing answer as plain text.",
            }
        return {
            "kind": "structured",
            "format": output_format or "json",
            "schema": output,
            "instruction": (
                "Return the complete final carrier as text in response. The outer Request will parse "
                "and validate that text against this original output contract."
            ),
        }

    @staticmethod
    def _validate_action_or_response_decision(value: dict[str, Any], _validation_context: Any):
        next_action = value.get("next_action")
        commands = value.get("execution_commands")
        response = value.get("response")
        if next_action == "execute":
            if not isinstance(commands, list) or len(commands) == 0:
                return {
                    "ok": False,
                    "reason": "next_action=execute requires at least one execution command.",
                }
            if response not in (None, ""):
                return {
                    "ok": False,
                    "reason": "next_action=execute requires response to be null or empty.",
                }
            return True
        if next_action == "response":
            if not isinstance(commands, list) or len(commands) != 0:
                return {
                    "ok": False,
                    "reason": "next_action=response requires empty execution_commands.",
                }
            if not isinstance(response, str) or not response.strip():
                return {
                    "ok": False,
                    "reason": "next_action=response requires a non-empty response.",
                }
            return True
        return {
            "ok": False,
            "reason": "next_action must be execute or response.",
        }

    @staticmethod
    async def _call_response_stream_handler(handler: Any, event: str, data: Any) -> None:
        if handler is None:
            return
        result = handler(event, data)
        if inspect.isawaitable(result):
            await result

    async def _stream_action_response_field(self, result_reader: Any, handler: Any) -> str:
        get_generator = getattr(result_reader, "get_async_generator", None)
        if handler is None or not callable(get_generator):
            return ""
        next_action = ""
        pending_response_deltas: list[str] = []
        streamed_response_deltas: list[str] = []
        async for item in cast(Any, get_generator)(type="instant"):
            path = str(getattr(item, "path", "") or "")
            if path == "$status":
                status = getattr(item, "value", None)
                if isinstance(status, dict) and status.get("retry"):
                    await self._call_response_stream_handler(handler, "status", status)
                continue
            if path == "next_action":
                value = getattr(item, "value", None)
                if isinstance(value, str):
                    next_action = value.strip().lower()
                    if next_action == "response" and pending_response_deltas:
                        for delta in pending_response_deltas:
                            await self._call_response_stream_handler(handler, "delta", delta)
                            streamed_response_deltas.append(delta)
                        pending_response_deltas.clear()
                continue
            if path != "response":
                continue
            delta = getattr(item, "delta", None)
            if not isinstance(delta, str) or not delta:
                continue
            if next_action == "response":
                await self._call_response_stream_handler(handler, "delta", delta)
                streamed_response_deltas.append(delta)
            elif not next_action:
                pending_response_deltas.append(delta)
        return "".join(streamed_response_deltas)

    async def _default_structured_planning_handler(
        self,
        context: "ActionRunContext",
        request: "ActionPlanningRequest",
    ) -> "ActionDecision":
        from agently.core import ModelRequest

        prompt = context["prompt"]
        settings = context["settings"]
        agent_name = str(context.get("agent_name", "Manual"))
        action_list = self.action._to_model_planning_action_list(request.get("action_list", []))
        done_plans = self.action._to_model_planning_records(context.get("done_plans", []))
        last_round_records = self.action._to_model_planning_records(context.get("last_round_records", []))
        round_index = context.get("round_index", 0)
        max_rounds = context.get("max_rounds", None)
        round_dispatch_policy = self._resolve_round_dispatch_policy(settings)
        planning_instructions = [
            "Handle the current request by either planning one Action round or returning the final response.",
            "Decide this round action first via 'next_action': 'execute' or 'response'.",
            (
                "If next_action is 'response', return empty 'execution_commands' and put the complete "
                "final response carrier in 'response'."
            ),
            (
                "If next_action is 'execute', return one or more 'execution_commands'; under "
                "round_dispatch_policy='single_action_id_cohort', every command in this round "
                "must use the same action_id. Wait for the next round and consume this round's "
                "results before planning calls with a different action_id."
                if round_dispatch_policy == "single_action_id_cohort"
                else "If next_action is 'execute', return one or more 'execution_commands' for parallel execution."
            ),
            "If next_action is 'execute', keep 'response' null or empty.",
            "Each command must include 'todo_suggestion' for next round decision making.",
        ]
        planning_info: dict[str, Any] = {}
        prior_records = list(done_plans) if isinstance(done_plans, list) else []
        latest_records = list(last_round_records) if isinstance(last_round_records, list) else []
        if (
            latest_records
            and len(prior_records) >= len(latest_records)
            and prior_records[-len(latest_records) :] == latest_records
        ):
            prior_records = prior_records[: -len(latest_records)]
        if prior_records:
            planning_info["prior_action_results"] = prior_records
        if latest_records:
            planning_info["last_round_result"] = latest_records
        if round_index:
            planning_info["round_index"] = round_index
        if max_rounds is not None:
            planning_info["max_rounds"] = max_rounds
        if round_dispatch_policy and round_dispatch_policy != "parallel":
            planning_info["round_dispatch_policy"] = round_dispatch_policy
        if planning_info:
            planning_instructions.append(
                "Use the non-empty Action round state in ${INFO} as evidence. "
                "A failed Action or environment check is not a successful execution."
            )
        if latest_records and any(
            record.get("success") is not True
            for record in latest_records
            if isinstance(record, dict)
        ):
            planning_instructions.append(
                "For a repairable argument, generated-code, or runtime error, execute a corrected Action call. "
                "For provider/environment unavailability, choose another eligible Action or explain the blocker "
                "and remedy; "
                "never fabricate the unavailable result."
            )

        parent_run_context = get_current_tool_phase_run_context()
        action_plan_request = ModelRequest(
            self.plugin_manager,
            parent_settings=settings,
            agent_name=agent_name,
            model_key=self._resolve_planning_model_key(settings),
        )
        self._mark_action_planning_request(action_plan_request)
        action_round_info = {
            "available_actions": action_list,
            "round_state": planning_info,
            "final_response_contract": self._final_response_contract(prompt),
        }
        request_prompt = self._seed_action_planning_prompt(action_plan_request, prompt)
        if request_prompt is not None and callable(getattr(request_prompt, "append", None)):
            request_prompt.append("info", {"action_runtime": action_round_info})
            request_prompt.append("instruct", {"action_runtime": planning_instructions})
        else:
            # Lightweight custom/fake ModelRequest implementations may expose
            # only the fluent methods. Keep that extension-test surface usable.
            action_plan_request.input(
                {
                    "user_input": prompt.get("input"),
                    "available_actions": action_list,
                }
            ).info(action_round_info).instruct(planning_instructions)
        action_plan_request.output(
            {
                "next_action": ("'execute' | 'response'", "This round action decision."),
                "execution_commands": [
                    {
                        "purpose": (str, "What this action call collects or verifies."),
                        "action_id": (str, "Must be one ${INFO.action_runtime.available_actions.[].action_id}"),
                        "action_input": (
                            dict,
                            "Must satisfy that Action's ${INFO.action_runtime.available_actions.[].kwargs} contract",
                        ),
                        "todo_suggestion": (str, "Suggestion for next round's next_action decision."),
                    }
                ],
                "response": (
                    "str | None",
                    "Complete final response carrier when next_action=response; otherwise null or empty.",
                ),
            },
            format="json",
        )
        validate = getattr(action_plan_request, "validate", None)
        if callable(validate):
            validate(self._validate_action_or_response_decision)
        action_plan_result = _get_model_request_result(
            action_plan_request,
            parent_run_context=parent_run_context,
        )
        result_reader = getattr(action_plan_result, "result", action_plan_result)
        response_stream_handler = context.get("response_stream_handler")
        await self._stream_action_response_field(result_reader, response_stream_handler)
        result = await result_reader.async_get_data()
        accepted_retry_result = getattr(result_reader, "_accepted_retry_result", None)
        if accepted_retry_result is not None and response_stream_handler is not None:
            await self._call_response_stream_handler(
                response_stream_handler,
                "status",
                {
                    "status": "failed",
                    "retry": True,
                    "reason": "action_or_response_validation",
                },
            )
            await self._stream_action_response_field(
                accepted_retry_result,
                response_stream_handler,
            )
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
            parent_prompt=prompt,
            agent_name=agent_name,
            model_key=self._resolve_planning_model_key(settings),
        )
        self._mark_action_planning_request(action_request)
        native_round_info = {
            "available_actions": action_list,
            "round_state": {
                "done_plans": done_plans,
                "last_round_result": last_round_records,
                "round_index": round_index,
                "max_rounds": max_rounds,
                "round_dispatch_policy": round_dispatch_policy,
            },
            "final_response_contract": self._final_response_contract(prompt),
        }
        native_instructions = [
            "Decide whether native Action calls are required to answer the current request.",
            (
                "If a tool is needed, emit one or more native tool calls that all use the same "
                "action_id in this round. Wait for the next round's results before choosing a "
                "different action_id."
                if round_dispatch_policy == "single_action_id_cohort"
                else "If a tool is needed, emit native tool calls for one or more available actions."
            ),
            "If no Action is needed, return the complete final response directly without emitting tool calls.",
        ]
        request_prompt = getattr(action_request, "prompt", None)
        if request_prompt is not None and callable(getattr(request_prompt, "append", None)):
            request_prompt.append("info", {"action_runtime": native_round_info})
            request_prompt.append("instruct", {"action_runtime": native_instructions})
        else:
            action_request.input(
                {
                    "user_input": prompt.get("input"),
                    "available_actions": action_list,
                }
            ).info(native_round_info).instruct(native_instructions)
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
                if not text_fragments and isinstance(data, str) and data:
                    text_fragments.append(data)
                break
        action_calls = self.action._normalize_native_action_calls(tool_call_chunks)
        if len(action_calls) == 0:
            response = "".join(text_fragments)
            textual_tool_markup_detected = any(
                marker in response.lower()
                for marker in ("<bash", "<tool", "<command", "```bash")
            )
            if response.strip() and not textual_tool_markup_detected:
                response_stream_handler = context.get("response_stream_handler")
                for fragment in text_fragments:
                    await self._call_response_stream_handler(
                        response_stream_handler,
                        "delta",
                        fragment,
                    )
                return {
                    "next_action": "response",
                    "response": response,
                    "use_action": False,
                    "action_calls": [],
                    "tool_commands": [],
                    "diagnostics": [],
                }
            diagnostic = cast(
                "ActionDiagnostic",
                {
                    "source": "ActionRuntime",
                    "severity": "warning",
                    "code": "action_runtime.native_tool_calls.empty",
                    "message": (
                        "Native tool-call planning returned no executable tool calls. "
                        "The host should treat this as a planning diagnostic rather than executed action evidence."
                    ),
                    "meta": {
                        "planning_protocol": "native_tool_calls",
                        "textual_tool_markup_detected": textual_tool_markup_detected,
                    },
                },
            )
            return {
                "next_action": "response",
                "response": "",
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

    def _retain_programmatic_catalog(
        self,
        catalog: dict[str, Any],
        *,
        max_active_catalogs: int = 64,
    ) -> None:
        revision = str(catalog.get("catalog_revision", "")).strip()
        if not revision:
            raise ValueError("Programmatic Action catalog revision is required.")
        existing = self._programmatic_catalogs.get(revision)
        if isinstance(existing, dict):
            existing["leases"] = int(existing.get("leases", 0)) + 1
            return
        if len(self._programmatic_catalogs) >= max_active_catalogs:
            raise RuntimeError(
                "Programmatic Action catalog capacity is exhausted by active or "
                "generated-but-unsettled program decisions."
            )
        catalog_snapshot = dict(catalog)
        entries = catalog_snapshot.get("entries", [])
        catalog_snapshot["_registration_versions"] = {
            str(entry.get("action_id", "")): self.action.action_registry._registration_version(
                str(entry.get("action_id", ""))
            )
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("action_id", ""))
        }
        self._programmatic_catalogs[revision] = {
            "catalog": catalog_snapshot,
            "leases": 1,
            "planning_lease": isinstance(catalog_snapshot.get("_planning_scope"), dict),
        }

    def resolve_programmatic_catalog(self, revision: str) -> dict[str, Any] | None:
        retained = self._programmatic_catalogs.get(str(revision))
        catalog = retained.get("catalog") if isinstance(retained, dict) else None
        return dict(catalog) if isinstance(catalog, dict) else None

    def release_programmatic_catalog(self, revision: str) -> None:
        retained = self._programmatic_catalogs.get(str(revision))
        if not isinstance(retained, dict):
            return
        leases = int(retained.get("leases", 0)) - 1
        if leases <= 0:
            self._programmatic_catalogs.pop(str(revision), None)
        else:
            retained["leases"] = leases

    def _release_planned_catalog(self, revision: str) -> bool:
        retained = self._programmatic_catalogs.get(str(revision))
        if not isinstance(retained, dict) or not retained.get("planning_lease"):
            return False
        retained["planning_lease"] = False
        self.release_programmatic_catalog(revision)
        return True

    def _release_programmatic_scope(self, run_id: str) -> None:
        """Settle only unconsumed default-planner leases owned by this loop."""

        for revision, retained in list(self._programmatic_catalogs.items()):
            catalog = retained.get("catalog", {})
            origin = catalog.get("_planning_scope") if isinstance(catalog, dict) else None
            if (
                retained.get("planning_lease")
                and isinstance(origin, dict)
                and origin.get("run_id") == run_id
            ):
                self._release_planned_catalog(revision)

    async def _default_programmatic_planning_handler(
        self,
        context: "ActionRunContext",
        request: "ActionPlanningRequest",
    ) -> "ActionDecision":
        from agently.core import ModelRequest
        from agently.core.operation.Action.ActionProgram import (
            build_programmatic_action_catalog,
            build_programmatic_python_source,
            normalize_programmatic_action_decision,
        )
        from agently.types.data import PROGRAMMATIC_ACTION_TRANSPORT_ID

        prompt = context["prompt"]
        settings = context["settings"]
        agent_name = str(context.get("agent_name", "Manual"))
        action_list = request.get("action_list", [])
        done_plans = context.get("done_plans", [])
        last_round_records = context.get("last_round_records", [])
        round_index = context.get("round_index", 0)
        max_rounds = context.get("max_rounds", None)
        max_program_bytes_raw = settings.get(
            "action.programmatic.max_program_bytes",
            20000,
        )
        max_program_bytes = (
            int(max_program_bytes_raw)
            if isinstance(max_program_bytes_raw, int)
            and not isinstance(max_program_bytes_raw, bool)
            and max_program_bytes_raw > 0
            else 20000
        )
        max_description_bytes_raw = settings.get(
            "action.programmatic.max_description_bytes",
            1024,
        )
        max_description_bytes = (
            int(max_description_bytes_raw)
            if isinstance(max_description_bytes_raw, int)
            and not isinstance(max_description_bytes_raw, bool)
            and max_description_bytes_raw > 0
            else 1024
        )
        parent_run_context = get_current_tool_phase_run_context()
        parent_run_id = str(getattr(parent_run_context, "run_id", "") or "")
        revision_seed = f"{parent_run_id}:{uuid.uuid4().hex}"
        ensure_transport = getattr(self.action, "_ensure_programmatic_action_transport", None)
        if not callable(ensure_transport):
            raise RuntimeError("Programmatic Action transport is not available.")
        ensure_transport(settings=settings)
        transport_spec = self.action.action_registry.get_spec(PROGRAMMATIC_ACTION_TRANSPORT_ID)
        if transport_spec is None:
            raise RuntimeError("Programmatic Action transport registration failed.")
        transport_policy = self.action.action_dispatcher._merge_policy(
            settings,
            transport_spec,
            {},
        )
        if transport_spec.get("approval_required") is True or transport_policy.get("approval_mode") == "always":
            return cast(
                "ActionDecision",
                {
                    "next_action": "response",
                    "use_action": False,
                    "action_calls": [],
                    "execution_commands": [],
                    "diagnostics": [
                        {
                            "source": "ActionRuntime",
                            "severity": "warning",
                            "code": "action.programmatic.transport_approval_required",
                            "message": (
                                "Programmatic Action V1 cannot start when the "
                                "reserved code transport requires approval. Use "
                                "an outer graph-visible approved stage instead."
                            ),
                            "meta": {"planning_protocol": "programmatic"},
                        }
                    ],
                },
            )
        catalog = build_programmatic_action_catalog(
            action_list,
            revision_seed=revision_seed,
        )
        catalog_payload = cast(dict[str, Any], catalog)
        catalog_payload["_revision_seed"] = revision_seed
        catalog_entries = catalog.get("entries", [])
        diagnostics = list(catalog.get("diagnostics", []))
        planning_observation = {
            "planning_protocol": "programmatic",
            "sdk_renderer_version": str(catalog.get("renderer_version", "")),
            "eligible_action_count": len(catalog_entries) if isinstance(catalog_entries, list) else 0,
            "ineligible_action_count": sum(
                1
                for diagnostic in diagnostics
                if isinstance(diagnostic, dict)
                and str(diagnostic.get("code", "")).startswith("action.programmatic.ineligible.")
            ),
            "sdk_bytes": int(catalog.get("sdk_bytes", 0)),
            "contract_bytes": int(catalog.get("contract_bytes", 0)),
            "program_bytes": 0,
        }
        if not isinstance(catalog_entries, list) or not catalog_entries:
            diagnostics.append(
                {
                    "source": "ActionRuntime",
                    "severity": "warning",
                    "code": "action.programmatic.no_eligible_actions",
                    "message": (
                        "Programmatic Action planning has no eligible read-only, "
                        "replay-safe Action with an explicit returns contract."
                    ),
                    "meta": {"planning_protocol": "programmatic"},
                }
            )
            return cast(
                "ActionDecision",
                {
                    "next_action": "response",
                    "use_action": False,
                    "action_calls": [],
                    "execution_commands": [],
                    "diagnostics": diagnostics,
                    "planning_observation": planning_observation,
                },
            )

        def validate_program_decision(value: dict[str, Any], _validation_context: Any):
            try:
                normalized = normalize_programmatic_action_decision(
                    value,
                    max_program_bytes=max_program_bytes,
                    max_description_bytes=max_description_bytes,
                )
                if normalized["next_action"] == "execute":
                    build_programmatic_python_source(str(normalized["program"]))
            except (TypeError, ValueError) as error:
                return cast(
                    Any,
                    {
                        "ok": False,
                        "reason": str(error),
                        "payload": {"planning_protocol": "programmatic"},
                    },
                )
            return True

        action_plan_request = ModelRequest(
            self.plugin_manager,
            parent_settings=settings,
            agent_name=agent_name,
            model_key=self._resolve_planning_model_key(settings),
        )
        self._mark_action_planning_request(action_plan_request)
        action_plan_request._set_output_observation_policy(sensitive_paths=["program"])
        programmatic_round_info = {
            "action_sdk": catalog.get("sdk", ""),
            "done_plans": done_plans,
            "last_round_result": last_round_records,
            "round_index": round_index,
            "max_rounds": max_rounds,
            "final_response_contract": self._final_response_contract(prompt),
            "program_contract": {
                "language": "python",
                "python_version": ">=3.10",
                "source_shape": (
                    "statements inside an existing async function body; local helper definitions are allowed"
                ),
                "completion": "an explicit return directly owned by the generated body",
                "max_program_bytes": max_program_bytes,
                "max_description_bytes": max_description_bytes,
                "dispatch": (
                    "explicit concurrency_mode='parallel' Actions may overlap up to the host limit; "
                    "exclusive Actions form ordering barriers"
                ),
                "return": "lossless JSON",
                "model_hot_result": "bounded print output and return value only",
            },
        }
        programmatic_instructions = [
            "Decide whether another Action round is required to answer the current request.",
            "Use next_action='response' with program=null and a complete final response when no Action work remains.",
            (
                "When Action work remains, write only the statements that belong inside the already-existing "
                "async function body, using the authoritative SDK in {info.action_sdk}."
            ),
            (
                "You may define local helper functions, async handlers, or classes when they make the program "
                "clearer; do not wrap or invoke a second top-level program entry point."
            ),
            (
                "The generated body must contain at least one explicit return directly owned by that body, "
                "not only a return nested under another function or class."
            ),
            "Call only offered bindings as await actions.name({...}) or await actions['exotic-name']({...}).",
            (
                "Treat every SDK input and return schema as authoritative and exhaustive. Never invent unlisted "
                "fields, wildcard ids, magic values, or implicit bulk operations."
            ),
            (
                "When a later Action requires an id or field produced by an earlier Action, read that exact "
                "field from the earlier result and pass it explicitly."
            ),
            "Use loops, branches, and local JSON data transformations only when they help the current task.",
            (
                "Use asyncio.gather only for calls that are independent. Actions whose SDK contract declares "
                "concurrency_mode='parallel' may overlap; exclusive Actions are serialized as ordering barriers "
                "by the host."
            ),
            "Return one bounded lossless-JSON value containing only what the next response round needs.",
            (
                "Only print(...) output and the return value become the outer Action result; intermediate Action "
                "values stay program-local."
            ),
            "Do not import network, filesystem, subprocess, package-manager, credential, or host-control capabilities.",
            "Do not request writes, execution, approvals, or irreversible side effects in programmatic V1.",
            "Handle the declared bounded Action errors honestly. Do not invent missing results.",
        ]
        request_prompt = self._seed_action_planning_prompt(action_plan_request, prompt)
        if request_prompt is not None and callable(getattr(request_prompt, "append", None)):
            request_prompt.append("info", {"action_runtime": programmatic_round_info})
            request_prompt.append("instruct", {"action_runtime": programmatic_instructions})
        else:
            action_plan_request.input({"user_input": prompt.get("input")}).info(
                programmatic_round_info
            ).instruct(programmatic_instructions)
        action_plan_request.output(
            {
                "next_action": (
                    "'execute' | 'response'",
                    "Whether to run one program or return the final response.",
                    True,
                ),
                "description": (
                    str,
                    "Short observer-facing label for this program decision.",
                    True,
                ),
                "program": (
                    "str | None",
                    (
                        "Statements for the inside of an existing async function when "
                        "next_action=execute; local helper definitions are allowed, and the "
                        "outer generated body must include a direct return. Otherwise null."
                    ),
                    True,
                ),
                "response": (
                    "str | None",
                    "Complete final response carrier when next_action=response; otherwise null or empty.",
                ),
            },
            format="json",
        ).validate(
            validate_program_decision
        )
        action_plan_result = _get_model_request_result(
            action_plan_request,
            parent_run_context=parent_run_context,
        )
        result_reader = getattr(action_plan_result, "result", action_plan_result)
        response_stream_handler = context.get("response_stream_handler")
        await self._stream_action_response_field(result_reader, response_stream_handler)
        raw_decision = await result_reader.async_get_data()
        accepted_retry_result = getattr(result_reader, "_accepted_retry_result", None)
        if accepted_retry_result is not None and response_stream_handler is not None:
            await self._call_response_stream_handler(
                response_stream_handler,
                "status",
                {
                    "status": "retrying",
                    "retry": True,
                    "reason": "action_or_response_validation",
                },
            )
            await self._stream_action_response_field(
                accepted_retry_result,
                response_stream_handler,
            )
        decision = normalize_programmatic_action_decision(
            raw_decision,
            max_program_bytes=max_program_bytes,
            max_description_bytes=max_description_bytes,
        )
        if decision["next_action"] == "response":
            return cast(
                "ActionDecision",
                {
                    "next_action": "response",
                    "response": decision.get("response") or "",
                    "use_action": False,
                    "action_calls": [],
                    "execution_commands": [],
                    "diagnostics": diagnostics,
                    "planning_observation": planning_observation,
                },
            )

        planning_observation["program_bytes"] = len(str(decision["program"]).encode("utf-8"))

        max_active_catalogs_raw = settings.get(
            "action.programmatic.max_active_catalogs",
            64,
        )
        max_active_catalogs = (
            int(max_active_catalogs_raw)
            if isinstance(max_active_catalogs_raw, int)
            and not isinstance(max_active_catalogs_raw, bool)
            and max_active_catalogs_raw > 0
            else 64
        )
        action_call = {
            "purpose": decision["description"],
            "action_id": PROGRAMMATIC_ACTION_TRANSPORT_ID,
            "action_input": {
                "program": decision["program"],
                "description": decision["description"],
                "catalog_revision": catalog["catalog_revision"],
            },
            "source_protocol": "programmatic",
            "todo_suggestion": (
                "Use the bounded program result to decide whether to respond or run another Action round."
            ),
        }
        if parent_run_id:
            catalog_payload["_planning_scope"] = {"run_id": parent_run_id, "round_index": round_index}
            catalog_payload["_action_input"] = dict(action_call["action_input"])
        self._retain_programmatic_catalog(
            catalog_payload,
            max_active_catalogs=max_active_catalogs,
        )
        return cast(
            "ActionDecision",
            {
                "next_action": "execute",
                "use_action": True,
                "action_calls": [action_call],
                "execution_commands": [action_call],
                "diagnostics": diagnostics,
                "planning_observation": planning_observation,
            },
        )

    async def _default_planning_handler(
        self,
        context: "ActionRunContext",
        request: "ActionPlanningRequest",
    ) -> "ActionDecision":
        settings = context["settings"]
        planning_protocol = self.resolve_planning_protocol(settings, request.get("planning_protocol"))
        if planning_protocol == "native_tool_calls":
            return await self._default_native_tool_call_planning_handler(context, request)
        if planning_protocol == "programmatic":
            return await self._default_programmatic_planning_handler(context, request)
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
        action_run_contexts = request.get("action_run_contexts", [])
        if not isinstance(action_run_contexts, list):
            action_run_contexts = []
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
            selected_run_context = (
                action_run_contexts[command_key]
                if isinstance(command_key, int) and 0 <= command_key < len(action_run_contexts)
                else None
            )

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
                if selected_run_context is not None:
                    with bind_runtime_context(
                        parent_run_context=selected_run_context,
                        tool_phase_run_context=selected_run_context,
                        settings=settings,
                    ):
                        result = await execute_once()
                else:
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
