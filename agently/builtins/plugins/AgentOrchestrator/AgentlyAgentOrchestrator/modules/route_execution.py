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
from typing import Any, Literal, TYPE_CHECKING

from agently.core.application.AgentExecution import AgentExecutionLimitExceeded, RuntimeStageStallError
from agently.core.runtime.RuntimeContext import bind_runtime_context
from agently.utils import DataFormatter

from .long_output import LongOutputError
from .routes import run_model_request_route
from .runtime_guidance import mark_pending_guidance_not_applied
from .task_strategy import run_agent_task_route
from .terminal_retention import (
    apply_agent_execution_terminal_retention,
    defer_agent_execution_terminal_retention,
    prepare_agent_execution_terminal_retention,
)

if TYPE_CHECKING:
    from agently.types.data import OutputValidateHandler, RunContext

    from .execution import AgentExecution


async def async_execute_route(
    owner: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"],
    ensure_keys: list[str] | None,
    ensure_all_keys: bool | None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
    key_style: Literal["dot", "slash"],
    max_retries: int,
    raise_ensure_failure: bool,
) -> tuple[str, Any]:
    with bind_runtime_context(agent_execution_context=owner.execution_context):
        owner.execution_context.record_progress(stage="route_selection", status="started")
        route, route_meta = await owner.select_route()
        owner.execution_context.record_progress(stage="route_selection", status="completed")
        owner.route_plan = owner.route_planner.build_route_plan(
            execution_id=owner.id,
            route=route,
            route_meta=route_meta,
        )
        owner.route_info.setdefault("selected_route", route)
        owner.route_info.setdefault("options", DataFormatter.sanitize(route_meta))
        owner.route_info.setdefault("reusable", True)
        await owner.emit_stream("route.selected", owner.route_plan, route=route)
        if route == "route_policy_blocked":
            reason = str(route_meta.get("route_policy_warning") or "Route policy could not be satisfied.")
            owner.status = "blocked"
            owner.close_snapshot = {"status": "blocked", "route": "route_policy_blocked", "route_meta": DataFormatter.sanitize(route_meta)}
            owner.diagnostics.setdefault("route_policy_violations", []).append(DataFormatter.sanitize(route_meta))
            await owner.emit_stream(
                "route.policy.blocked",
                DataFormatter.sanitize(route_meta),
                route="route_policy_blocked",
                source="agent_execution",
                meta={"status": "blocked"},
            )
            return route, {
                "status": "blocked",
                "accepted": False,
                "artifact_status": "blocked",
                "reason": reason,
                "final_response": (
                    "Task encountered a blocking condition. "
                    f"No complete final deliverable was accepted. Reason: {reason}"
                ),
                "route_policy": route_meta.get("route_policy"),
            }
        if route == "agent_task" and owner._ensure_long_output_enabled:
            raise LongOutputError(
                "ensure_long_output is a direct ModelRequest delivery policy and cannot be "
                "mixed with an explicitly selected AgentTask execution. Keep the long deliverable "
                "as a direct execution, or let AgentTask produce bounded planning results and start "
                "a separate direct delivery execution."
            )
        if route == "agent_task":
            result = await run_agent_task_route(owner, route_meta)
        else:
            result = await run_model_request_route(
                owner,
                type=type,
                ensure_keys=ensure_keys,
                ensure_all_keys=ensure_all_keys,
                validate_handler=validate_handler,
                key_style=key_style,
                max_retries=max_retries,
                raise_ensure_failure=raise_ensure_failure,
            )
        if route != "agent_task":
            await mark_pending_guidance_not_applied(owner, reason=f"route:{route}:not_agent_task")
        return route, result


async def start_execution(
    owner: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"],
    ensure_keys: list[str] | None,
    ensure_all_keys: bool | None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
    key_style: Literal["dot", "slash"],
    max_retries: int,
    raise_ensure_failure: bool,
    parent_run_context: "RunContext | None",
) -> Any:
    if parent_run_context is not None:
        owner.parent_run_context = parent_run_context
    async with owner._start_lock:
        # The lock is held across the whole run, so a second entrant always sees
        # a completed execution here; there is no started-but-not-completed state
        # to busy-wait on.
        if owner._completed:
            if owner._error is not None:
                raise owner._error
            return owner.result
        owner._started = True
        owner.status = "running"
        try:
            await owner.async_prepare_task_context()
            await owner._async_emit_agent_execution_started_once()
            owner.execution_context.raise_if_nesting_exceeded()
            owner.execution_context.record_progress(
                stage="agent_execution",
                status="started",
                event_type="agent_execution.started",
                meta={"execution_id": owner.id},
            )
            run_coro = owner._async_execute_route(
                type=type,
                ensure_keys=ensure_keys,
                ensure_all_keys=ensure_all_keys,
                validate_handler=validate_handler,
                key_style=key_style,
                max_retries=max_retries,
                raise_ensure_failure=raise_ensure_failure,
            )
            route, owner.result = await owner._await_route_with_limits(run_coro)
            if owner.status == "running":
                owner.status = "success"
            terminal_projection = await _prepare_terminal_projection(owner)
            await owner.emit_stream(
                "result",
                terminal_projection[0],
                route=route,
                source="agent_execution",
            )
            await _finalize_terminal_execution(
                owner,
                terminal_status=(
                    "completed" if owner.status in {"success", "completed"} else "failed"
                ),
                terminal_projection=terminal_projection,
            )
            return owner.result
        except RuntimeStageStallError as error:
            owner.status = "timed_out" if error.status == "timed_out" else "stalled"
            owner._error = error
            error_projection = owner._record_error_diagnostic(error)
            await owner.emit_stream(
                "error",
                error_projection,
                source="agent_execution",
            )
            await _finalize_terminal_execution(owner, terminal_status="failed")
            raise
        except asyncio.TimeoutError as error:
            owner.status = "timed_out"
            timeout_error = RuntimeStageStallError(
                (
                    "AgentExecution hard deadline exceeded: "
                    f"max_seconds={ owner.limits.get('max_seconds') }."
                ),
                stage=str((owner.execution_context.last_progress_event or {}).get("stage") or "agent_execution"),
                status="timed_out",
                elapsed_seconds=None,
                timeout_seconds=owner.limits.get("max_seconds"),
                last_progress_event=(owner.execution_context.last_progress_event or {}).get("event_type"),
            )
            owner._error = timeout_error
            error_projection = owner._record_error_diagnostic(timeout_error)
            await owner.emit_stream(
                "error",
                error_projection,
                source="agent_execution",
            )
            await _finalize_terminal_execution(owner, terminal_status="failed")
            raise timeout_error from error
        except AgentExecutionLimitExceeded as error:
            owner.status = "blocked"
            owner._error = error
            error_projection = owner._record_error_diagnostic(error)
            await owner.emit_stream(
                "error",
                error_projection,
                source="agent_execution",
            )
            await _finalize_terminal_execution(owner, terminal_status="failed")
            raise
        except asyncio.CancelledError as error:
            owner.status = "cancelled"
            owner._error = error
            error_projection = owner._record_error_diagnostic(error)
            await owner.emit_stream(
                "cancelled",
                {**error_projection, "status": "cancelled"},
                source="agent_execution",
            )
            await _finalize_terminal_execution(owner, terminal_status="cancelled")
            raise
        except BaseException as error:
            owner.status = "error"
            owner._error = error
            error_projection = owner._record_error_diagnostic(error)
            await owner.emit_stream(
                "error",
                error_projection,
                source="agent_execution",
            )
            await _finalize_terminal_execution(owner, terminal_status="failed")
            raise
        finally:
            owner._refresh_diagnostics()
            owner._completed = True
            await owner.close_streams()


async def _prepare_terminal_projection(
    owner: "AgentExecution",
) -> tuple[Any, list[Any]]:
    try:
        return await prepare_agent_execution_terminal_retention(owner)
    except Exception as error:
        defer_agent_execution_terminal_retention(
            owner,
            code="agent_execution.retention.prepare_failed",
            error=error,
        )
        return (
            {
                "status": owner.status,
                "kind": "agent_execution_terminal_result_unavailable",
            },
            [],
        )


async def _finalize_terminal_execution(
    owner: "AgentExecution",
    *,
    terminal_status: Literal["completed", "failed", "cancelled"],
    terminal_projection: tuple[Any, list[Any]] | None = None,
) -> None:
    owner._terminal_status = terminal_status
    try:
        event_result, retained_refs = (
            terminal_projection
            if terminal_projection is not None
            else await _prepare_terminal_projection(owner)
        )
        owner.close_snapshot = {
            **dict(owner.close_snapshot),
            "terminal_result": DataFormatter.sanitize(event_result),
            "terminal_retained_refs": DataFormatter.sanitize(retained_refs),
        }
        terminal_close_snapshot = {
            "status": owner.close_snapshot.get("status", owner.status),
            "route": owner.close_snapshot.get("route") or owner.route_info.get("selected_route"),
            "terminal_result": owner.close_snapshot["terminal_result"],
            "terminal_retained_refs": owner.close_snapshot["terminal_retained_refs"],
        }
        reason = owner.close_snapshot.get("reason")
        if reason:
            terminal_close_snapshot["reason"] = str(reason)[:360]
        try:
            await owner._async_emit_agent_execution_terminal_event(
                terminal_status=terminal_status,
                close_snapshot=terminal_close_snapshot,
            )
        except Exception as error:
            defer_agent_execution_terminal_retention(
                owner,
                code="agent_execution.retention.terminal_event_delivery_failed",
                error=error,
            )
        async with owner._record_store_write_lock:
            await apply_agent_execution_terminal_retention(owner, status=terminal_status)
    finally:
        action = getattr(owner.agent, "action", None)
        release_scope = getattr(action, "_release_artifact_scope_except", None)
        if callable(release_scope):
            try:
                preserved_ids = set(owner._terminal_preserved_action_artifact_ids)
                task = getattr(owner, "task_record", None)
                task_id = str(getattr(task, "id", "") or "").strip()
                artifact_scope = (
                    {"kind": "agent_task", "id": task_id}
                    if task_id
                    else {"kind": "agent_execution", "id": owner.id}
                )
                released = release_scope(
                    artifact_scope,
                    retained_artifact_ids=preserved_ids,
                )
                released_count = released if isinstance(released, int) else 0
                owner.diagnostics["action_artifact_release"] = {
                    "status": "deferred" if preserved_ids else "released",
                    "scope": artifact_scope,
                    "released_count": released_count,
                    "preserved_artifact_ids": sorted(preserved_ids),
                }
            except Exception as error:
                owner.diagnostics["action_artifact_release"] = {
                    "status": "failed",
                    "scope": {"kind": "agent_execution", "id": owner.id},
                    "diagnostics": [
                        {
                            "code": "agent_execution.action_artifact_release_failed",
                            "message": (str(error).strip() or error.__class__.__name__)[:360],
                        }
                    ],
                }
