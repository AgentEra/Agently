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

from typing import Any, Literal, TYPE_CHECKING

from agently.utils import DataFormatter

from .long_output import LongOutputDelivery, normalized_terminal

if TYPE_CHECKING:
    from .execution import AgentExecution
    from agently.types.data import OutputValidateHandler


async def run_model_request_route(
    execution: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"],
    ensure_keys: list[str] | None,
    ensure_all_keys: bool | None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
    key_style: Literal["dot", "slash"],
    max_retries: int,
    raise_ensure_failure: bool,
) -> Any:
    agent_execution_run_context = await execution._async_emit_agent_execution_started_once()
    context_package = await execution.async_read_task_context(
        consumer_id=f"model_request:{execution.id}",
        phase="direct",
    )
    context_lanes: dict[str, list[dict[str, Any]]] = {
        "instruct": [],
        "info": [],
        "examples": [],
    }
    for block in context_package.blocks:
        item = {
            "content": DataFormatter.sanitize(block.content),
            "role": block.role,
            "ref": block.source_ref,
            "completeness": block.completeness,
        }
        if block.role == "instruction":
            context_lanes["instruct"].append(item)
        elif block.role == "example":
            context_lanes["examples"].append(item)
        else:
            context_lanes["info"].append(item)
    for lane, items in context_lanes.items():
        if items:
            execution.request.prompt.append(
                lane,
                {"task_context_blocks": items},
            )
    await execution.emit_stream(
        "context.package",
        {
            "package_id": context_package.package_id,
            "block_count": len(context_package.blocks),
            "used_chars": context_package.used_chars,
        },
        route="model_request",
        source="task_context",
    )
    if ensure_all_keys is not None:
        execution.request.prompt.set("ensure_all_keys", ensure_all_keys)
    long_output_delivery: LongOutputDelivery | None = None
    if execution._ensure_long_output_enabled:
        long_output_delivery = LongOutputDelivery(
            execution,
            ensure_keys=ensure_keys,
            ensure_all_keys=ensure_all_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )
        long_output_delivery.preflight()
    result = execution.request.get_result(parent_run_context=agent_execution_run_context)
    execution._model_request_result = result
    execution.record_model_response_id(result.id)
    stream_meta = {
        "response_id": result.response_id,
        "request_run_id": result.request_run_context.run_id if result.request_run_context is not None else None,
        "model_run_id": result.model_run_context.run_id if result.model_run_context is not None else None,
        "attempt_index": result.attempt_index,
    }
    has_structured_stream = bool(execution.prompt_snapshot.get("output"))
    structured_stream_events: list[Any] = []
    if has_structured_stream:
        async for item in result.get_async_generator(type="instant"):
            if long_output_delivery is not None:
                structured_stream_events.append(item)
            await execution.bridge_model_stream_item(
                item,
                route="model_request",
                meta=stream_meta,
            )
    else:
        async for event, data in result.get_async_generator(type="all"):
            if event in {"action", "tool"}:
                await execution.record_action_log(
                    data,
                    route="model_request",
                    source="action" if event == "action" else "tool",
                )
            elif event == "delta":
                await execution.emit_stream(
                    "model.delta",
                    data,
                    route="model_request",
                    source="model_request",
                    delta=str(data),
                    event_type="delta",
                    is_complete=False,
                    meta={**stream_meta, "specific_event": "delta"},
                )
            elif event in {"reasoning_delta", "original_delta"}:
                await execution.emit_stream(
                    f"model.{event}",
                    data,
                    route="model_request",
                    source="model_request",
                    delta=str(data),
                    event_type="delta",
                    is_complete=False,
                    meta={**stream_meta, "specific_event": event},
                )
            elif event in {"tool_calls", "reasoning_done", "original_done"}:
                await execution.emit_stream(
                    f"model.{event}",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "specific_event": event},
                )
            elif event == "status":
                await execution.emit_stream(
                    "$status",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "field_path": "$status", "specific_event": "status"},
                )
            elif event == "done":
                await execution.emit_stream(
                    "model.text",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "specific_event": "done"},
                )
            elif event in {"meta", "extra", "error"}:
                await execution.emit_stream(
                    f"model.{event}",
                    data,
                    route="model_request",
                    source="model_request",
                    meta={**stream_meta, "specific_event": event},
                )
    if long_output_delivery is not None:
        terminal = normalized_terminal(await result.async_get_meta())
        if terminal == "length":
            await long_output_delivery.accept_initial(
                result,
                streaming_events=structured_stream_events,
            )
            data = await long_output_delivery.run_continuation_flow()
        else:
            data = await result.async_get_data(
                type=type,
                ensure_keys=ensure_keys,
                validate_handler=validate_handler,
                key_style=key_style,
                max_retries=max_retries,
                raise_ensure_failure=raise_ensure_failure,
            )
            validation_handlers = execution.request.extension_handlers.get(
                "validate_handlers",
                [],
            )
            declared_coverage = bool(
                ensure_keys
                or validate_handler is not None
                or validation_handlers
            )
            execution._long_output_meta = {
                "enabled": True,
                "status": "completed",
                "request_count": 1,
                "segment_count": 1,
                "transport_complete": True,
                "schema_complete": True,
                "declared_coverage_complete": True if declared_coverage else None,
                "semantic_exhaustiveness": "not_claimed",
                "guarantee_level": (
                    "single_request_with_declared_coverage"
                    if declared_coverage
                    else "single_request_validated"
                ),
            }
            execution.diagnostics["long_output"] = dict(execution._long_output_meta)
    else:
        data = await result.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
        )
    execution.record_context_consumption(
        context_package,
        request_id=str(result.response_id or result.id),
    )
    full_result_data = result.full_result_data
    extra = full_result_data.get("extra", {}) if isinstance(full_result_data, dict) else {}
    if isinstance(extra, dict):
        action_logs = extra.get("action_logs", [])
        if isinstance(action_logs, list):
            for log in action_logs:
                await execution.record_action_log(log, route="model_request", source="action")
        tool_logs = extra.get("tool_logs", [])
        if isinstance(tool_logs, list):
            for log in tool_logs:
                await execution.record_action_log(log, route="model_request", source="tool")
    capability_failure = await _required_action_failure(execution, route="model_request")
    if capability_failure is not None:
        execution.status = "blocked"
        execution.close_snapshot = {
            "status": "blocked",
            "route": "model_request",
            "required_capabilities": capability_failure,
        }
        execution.diagnostics.setdefault("required_capabilities", []).append(capability_failure)
        await execution.emit_stream(
            "route.required_capability.blocked",
            capability_failure,
            route="model_request",
            source="agent_execution",
            meta={"status": "blocked"},
        )
        return {
            "status": "blocked",
            "accepted": False,
            "artifact_status": "blocked",
            "reason": capability_failure["reason"],
            "final_response": (
                "Task encountered a blocking condition. "
                f"No complete final deliverable was accepted. Reason: {capability_failure['reason']}"
            ),
            "required_capabilities": capability_failure,
        }
    execution.close_snapshot = {"status": "success", "route": "model_request"}
    if long_output_delivery is not None:
        execution.close_snapshot["long_output"] = DataFormatter.sanitize(
            getattr(execution, "_long_output_meta", {})
        )
    return data


async def _required_action_failure(execution: "AgentExecution", *, route: str) -> dict[str, Any] | None:
    required_actions = execution.required_action_ids()
    if not required_actions:
        return None
    action_logs = execution.logs.get("action_logs", [])
    if not isinstance(action_logs, list):
        action_logs = []
    statuses: dict[str, str] = {}
    for item in action_logs:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or item.get("id") or item.get("name") or "").strip()
        if not action_id:
            continue
        statuses[action_id] = str(item.get("status") or "").strip().lower()
    successful_statuses = {"success", "succeeded", "partial_success"}
    missing = [action_id for action_id in required_actions if action_id not in statuses]
    failed = [
        action_id
        for action_id in required_actions
        if action_id in statuses and statuses[action_id] not in successful_statuses
    ]
    if not missing and not failed:
        return None
    reason_bits = []
    if missing:
        reason_bits.append(f"missing required action evidence: {', '.join(missing)}")
    if failed:
        reason_bits.append(f"required action did not succeed: {', '.join(failed)}")
    return {
        "route": route,
        "required_actions": required_actions,
        "missing_actions": missing,
        "failed_actions": failed,
        "action_statuses": statuses,
        "reason": "; ".join(reason_bits),
    }
