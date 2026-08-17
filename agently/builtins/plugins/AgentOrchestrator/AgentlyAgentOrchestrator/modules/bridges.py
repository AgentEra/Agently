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

from typing import Any, TYPE_CHECKING

from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution


# Framework loop signals (e.g. the action-loop max_rounds boundary, planning stalls)
# are surfaced as records so the model and observers can see them, but they are not
# capability actions the agent executed. They must not enter the executed action_logs
# (action scope, capability evidence, required-action gates all read that list).
_FRAMEWORK_DIAGNOSTIC_ACTION_IDS = frozenset({"action_loop", "action_planning"})


def record_model_response_id(owner: "AgentExecution", response_id: str | None) -> None:
    if not response_id:
        return
    ids = owner.logs.setdefault("model_response_ids", [])
    if isinstance(ids, list) and response_id not in ids:
        ids.append(response_id)
    owner.logs.setdefault("model_response_id", response_id)


async def record_action_log(
    owner: "AgentExecution",
    log: Any,
    *,
    route: str,
    source: str = "action",
    emit: bool = True,
) -> dict[str, Any] | None:
    if not isinstance(log, dict):
        return None
    normalized = normalize_action_log(log, route=route, source=source)
    key = _action_log_key(normalized)
    if key in owner._seen_action_log_keys:
        return None
    owner._seen_action_log_keys.add(key)
    action_id = str(normalized.get("action_id") or "action")
    artifact_refs = normalized.get("artifact_refs", [])
    if not isinstance(artifact_refs, list):
        artifact_refs = []

    # Keep framework loop diagnostics out of the executed action_logs; retain them in
    # a sibling channel so the boundary signal stays inspectable without being counted
    # as an action execution.
    target_log_key = "action_loop_diagnostics" if action_id in _FRAMEWORK_DIAGNOSTIC_ACTION_IDS else "action_logs"
    target_logs = owner.logs.setdefault(target_log_key, [])
    if isinstance(target_logs, list):
        target_logs.append(normalized)
    aggregated_artifact_refs = owner.logs.setdefault("artifact_refs", [])
    if isinstance(aggregated_artifact_refs, list):
        for ref in artifact_refs:
            if ref not in aggregated_artifact_refs:
                aggregated_artifact_refs.append(DataFormatter.sanitize(ref))
    if emit:
        await owner.emit_stream(
            f"actions.{ action_id }",
            normalized,
            route=route,
            source=source,
            action_id=action_id,
        )
    return normalized


def normalize_action_log(
    log: dict[str, Any],
    *,
    route: str,
    source: str,
) -> dict[str, Any]:
    """Build one bounded semantic log carrier without a nested raw record."""

    raw_model_digest = log.get("model_digest")
    model_digest: dict[str, Any] = raw_model_digest if isinstance(raw_model_digest, dict) else {}
    action_id = str(log.get("action_id") or log.get("tool_name") or model_digest.get("action_id") or "action")
    action_call_id = log.get("action_call_id") or model_digest.get("action_call_id")
    status = str(log.get("status") or model_digest.get("status") or "")
    identity: dict[str, int] = {}
    for identity_key in ("round_index", "command_index"):
        identity_value = log.get(identity_key)
        if identity_value is None:
            identity_value = model_digest.get(identity_key)
        if isinstance(identity_value, int) and not isinstance(identity_value, bool):
            identity[identity_key] = identity_value
    artifact_refs = log.get("artifact_refs") or model_digest.get("artifact_refs") or []
    if not isinstance(artifact_refs, list):
        artifact_refs = []
    data = log.get("result")
    if data is None or (isinstance(data, dict) and data.get("same_as")):
        data = log.get("data")
    if data is None or (isinstance(data, dict) and data.get("same_as")):
        data = model_digest
    normalized = DataFormatter.sanitize(
        {
            "action_call_id": action_call_id,
            "action_id": action_id,
            "status": status,
            "success": log.get("success") if "success" in log else model_digest.get("success"),
            "source": source,
            "route": route,
            "data": data if isinstance(data, dict) else {},
            "artifact_refs": artifact_refs,
            **identity,
        }
    )
    return normalized if isinstance(normalized, dict) else {}


def _action_log_key(log: dict[str, Any]) -> str:
    action_call_id = log.get("action_call_id")
    if action_call_id:
        return str(action_call_id)
    action_id = str(log.get("action_id") or "action")
    status = str(log.get("status") or "")
    command_index = log.get("command_index")
    round_index = log.get("round_index")
    if isinstance(command_index, int) and not isinstance(command_index, bool):
        return f"position:{ round_index }:{ command_index }:{ action_id }:{ status }"
    digest = str(DataFormatter.sanitize(log.get("data")))
    return f"{ action_id }:{ status }:{ hash(digest) }"


async def bridge_task_dag_stream_item(owner: "AgentExecution", item: Any, *, route: str) -> None:
    await owner.stream.bridge_task_dag_item(item, route=route)


async def bridge_agent_task_stream_item(
    owner: "AgentExecution",
    item: Any,
    *,
    route: str = "agent_task",
) -> None:
    """Bridge one child-task event and project material progress to its parent.

    AgentTask owns its internal liveness clock, while the routed parent
    AgentExecution owns the outer idle deadline.  A typed non-heartbeat task
    event is the value edge between those two owners.  Project it onto the
    parent before publishing the stream item so long-running cards, Actions,
    and model streams remain observable without treating observational
    heartbeats as work.
    """

    raw_path = str(getattr(item, "path", "") or "agent_task.stream")
    item_meta = getattr(item, "meta", None)
    item_meta = dict(item_meta) if isinstance(item_meta, dict) else {}
    stream_kind = str(item_meta.get("stream_kind") or "").strip()
    if stream_kind != "heartbeat":
        raw_event_type = getattr(item, "event_type", "done")
        completed = bool(getattr(item, "is_complete", raw_event_type != "delta"))
        progress_meta: dict[str, Any] = {
            "route": route,
            "source": str(getattr(item, "source", "") or "agent_task"),
            "stream_kind": stream_kind or None,
            "task_id": getattr(item, "task_id", None) or item_meta.get("task_id"),
        }
        progress_meta.update(item_meta)
        child_meta = item_meta.get("child_meta")
        child_meta = child_meta if isinstance(child_meta, dict) else {}
        record_progress = getattr(owner.execution_context, "record_progress", None)
        if callable(record_progress):
            record_progress(
                stage=raw_path,
                status="completed" if completed else "progress",
                event_type=raw_path,
                run_id=str(
                    child_meta.get("model_run_id")
                    or child_meta.get("request_run_id")
                    or item_meta.get("model_run_id")
                    or item_meta.get("request_run_id")
                    or ""
                )
                or None,
                response_id=str(
                    child_meta.get("response_id") or item_meta.get("response_id") or ""
                )
                or None,
                meta=progress_meta,
                notify=False,
            )
    await owner.stream.bridge_agent_task_item(item, route=route)


async def bridge_model_stream_item(
    owner: "AgentExecution",
    item: Any,
    *,
    route: str,
    source: str = "model_request",
    path_prefix: str | None = None,
    stage_id: str | None = None,
    task_id: str | None = None,
    action_id: str | None = None,
    graph_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    raw_path = str(getattr(item, "path", "") or "model")
    path = f"{path_prefix}.{raw_path}" if path_prefix else raw_path
    raw_event_type = getattr(item, "event_type", "done")
    event_type = "delta" if raw_event_type == "delta" else "done"
    completed = bool(getattr(item, "is_complete", event_type == "done"))
    progress_meta = {
        "route": route,
        "source": source,
        "field_path": raw_path,
        "wildcard_path": getattr(item, "wildcard_path", None),
        "indexes": getattr(item, "indexes", None),
        "completion_source": getattr(item, "completion_source", None),
    }
    if meta:
        progress_meta.update(meta)
    record_progress = getattr(owner.execution_context, "record_progress", None)
    if callable(record_progress):
        record_progress(
            stage=path,
            status="completed" if completed else "progress",
            event_type=path,
            run_id=str(progress_meta.get("model_run_id") or progress_meta.get("request_run_id") or "") or None,
            response_id=str(progress_meta.get("response_id") or "") or None,
            meta=progress_meta,
            notify=False,
        )
    await owner.stream.bridge_model_stream_item(
        item,
        route=route,
        source=source,
        path_prefix=path_prefix,
        stage_id=stage_id,
        task_id=task_id,
        action_id=action_id,
        graph_id=graph_id,
        meta=meta,
    )
