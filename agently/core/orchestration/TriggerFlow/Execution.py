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

from agently_stage import Stage, default_stage_call_bridge


import uuid
import asyncio
import warnings
import json
import time
import copy
import inspect
import hashlib
import importlib
from collections import deque
from pathlib import Path
from contextvars import ContextVar

from collections.abc import Coroutine
from typing import Any, Literal, TYPE_CHECKING, overload, AsyncGenerator, Generator, Generic, TypeVar, cast

if TYPE_CHECKING:
    from .TriggerFlow import TriggerFlow
    from agently.types.trigger_flow import TriggerFlowAllHandlers
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourceRequirement,
        RunContext,
    )
    from agently.types.plugins import RuntimeEventStore
    from agently.types.trigger_flow import TriggerFlowExecutionSnapshotStore

from agently.utils import DeprecationWarnings, StateData, Settings
from agently.core.runtime.RuntimeContext import bind_runtime_context, get_current_chunk_run_context
from agently.types.trigger_flow import (
    TriggerFlowContractMetadata,
    TriggerFlowContractSpec,
    TriggerFlowInterventionEvent,
    TriggerFlowInterruptEvent,
    TriggerFlowRuntimeData,
)
from agently.types.trigger_flow.runtime_keys import (
    AGGREGATION_SCOPE_META_KEY,
    PARENT_SIGNAL_ID_META_KEY,
    TRANSIENT_AGGREGATION_STATE_KEYS,
)
from agently.types.data import (
    EMPTY,
    RunContext,
    RuntimeEvent,
    SnapshotPruneResult,
    SnapshotRetentionPolicy,
)
from agently.types.data import ExecutionResourceRequirement
from .Control import (
    TriggerFlowPauseSignal,
    TRIGGER_FLOW_STATUS_CANCELLED,
    TRIGGER_FLOW_STATUS_COMPLETED,
    TRIGGER_FLOW_STATUS_CREATED,
    TRIGGER_FLOW_STATUS_FAILED,
    TRIGGER_FLOW_STATUS_RUNNING,
    TRIGGER_FLOW_LIFECYCLE_CLOSED,
    TRIGGER_FLOW_LIFECYCLE_OPEN,
    TRIGGER_FLOW_LIFECYCLE_SEALED,
)
from .Signal import TriggerFlowSignal, TriggerFlowSignalType
from .SignalNet import TriggerFlowSignalNet
from .ExecutionState import INTERVENTIONS_STATE_KEY, TriggerFlowInterventionMode
from .ExecutionResult import TriggerFlowExecutionResult
from .ExecutionInterrupts import TriggerFlowExecutionInterrupts
from .ExecutionPersistence import TriggerFlowExecutionPersistence
from .ExecutionRuntimeIO import TriggerFlowExecutionRuntimeIO
from ._runtime_stream_transport import StageRuntimeStreamTransport
from .ExecutionRetention import (
    TriggerFlowTerminalRetention,
    apply_triggerflow_terminal_retention,
    prepare_triggerflow_terminal_retention,
)
from .RecoveryDiagnostics import diagnose_runtime_event_records, project_runtime_event_record

InputT = TypeVar("InputT")
StreamT = TypeVar("StreamT")
ResultT = TypeVar("ResultT")
PendingInterruptClosePolicy = Literal["error", "cancel"]
DISTRIBUTED_SNAPSHOT_PROVIDER_CAPABILITIES = (
    "supports_cas",
    "supports_lease",
    "supports_range_read",
    "supports_retention",
)
DISTRIBUTED_SNAPSHOT_PROVIDER_METHODS = (
    "get_snapshot",
    "put_snapshot",
    "prune_snapshots",
    "claim_lease",
    "heartbeat_lease",
    "release_lease",
    "put_artifact_ref",
)
DISTRIBUTED_RUNTIME_EVENT_PROVIDER_CAPABILITIES = (
    "supports_event_sequence",
)


def _create_deadline(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    return time.monotonic() + max(0.0, timeout)


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


class TriggerFlowExecution(Generic[InputT, StreamT, ResultT]):
    def __init__(
        self,
        *,
        handlers: "TriggerFlowAllHandlers",
        trigger_flow: "TriggerFlow[InputT, StreamT, ResultT]",
        id: str | None = None,
        skip_exceptions: bool = False,
        concurrency: int | None = None,
        run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 10.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
        execution_resources: "list[ExecutionResourceRequirement] | None" = None,
        intervention_mode: TriggerFlowInterventionMode = None,
        intervention_policy: Any = None,
        resume_handle_exposed: bool = True,
    ):
        if intervention_mode not in {None, "planned", "auto"}:
            raise ValueError("TriggerFlow intervention_mode must be one of: None, 'planned', 'auto'.")
        if intervention_mode == "auto":
            planned_points = [
                operator
                for operator in trigger_flow._blue_print.definition.operators
                if operator.get("kind") == "intervention_point"
            ]
            if planned_points:
                names = [str(operator.get("name") or operator.get("id")) for operator in planned_points]
                raise ValueError(
                    "TriggerFlow intervention_mode='auto' can not be used with explicit intervention points. "
                    f"Found: { names }"
                )
        # Basic Attributions
        self.id = id if id is not None else uuid.uuid4().hex
        self._handlers = handlers
        self._trigger_flow = trigger_flow
        self._runtime_data = StateData()
        self._runtime_resources = StateData(
            name=f"TriggerFlowExecution-{ self.id }-RuntimeResources",
            parent=self._trigger_flow._runtime_resources,
        )
        self._system_runtime_data = StateData()
        self._skip_exceptions = skip_exceptions
        self._concurrency_semaphore = asyncio.Semaphore(concurrency) if concurrency and concurrency > 0 else None
        self._concurrency_permit_held = ContextVar(
            f"trigger_flow_execution_concurrency_permit_held_{ self.id }",
            default=False,
        )
        self._close_lock = asyncio.Lock()
        self._sub_flow_control_lock = asyncio.Lock()
        self._live_sub_flow_executions: dict[str, "TriggerFlowExecution[Any, Any, Any]"] = {}
        self._live_sub_flow_tasks: dict[str, asyncio.Task[Any]] = {}
        self._live_sub_flow_signal_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self.run_context = (
            run_context
            if run_context is not None
            else RunContext.create(
                run_kind="workflow_execution",
                execution_id=self.id,
                meta={"flow_name": self._trigger_flow.name},
            )
        )
        self._runtime_started_emitted = False
        self._runtime_completed_emitted = False
        self._runtime_failed_emitted = False
        self._runtime_result_set_emitted = False
        self._runtime_definition_emitted = False
        self._snapshot_store = None
        self._runtime_event_store = None
        self._auto_close = bool(auto_close)
        self._auto_close_timeout = auto_close_timeout
        self._resume_handle_exposed = bool(resume_handle_exposed)
        self._lifecycle_state = TRIGGER_FLOW_LIFECYCLE_OPEN
        self._created_at = time.time()
        self._started_at: float | None = None
        self._last_activity_at: float | None = None
        self._sealed_at: float | None = None
        self._closed_at: float | None = None
        self._close_reason: str | None = None
        self._state_version = 0
        self._owner_id = owner_id
        self._lease_ttl = lease_ttl
        self._execution_resource_requirements = execution_resources if execution_resources is not None else []
        self._managed_execution_resource_handles: list["ExecutionResourceHandle"] = []
        self._resource_requirements: list[dict[str, Any]] = []
        self._compaction_segments: list[dict[str, Any]] = []
        self._retained_lineage_anchors: list[dict[str, Any]] = []
        self._snapshot_artifact_refs: list[dict[str, Any]] = []
        self._compaction_policy: dict[str, Any] = {}
        self._snapshot_projection_policy: dict[str, Any] = {
            "enabled": False,
            "terminal_value_mode": "digest",
            "min_value_bytes": 4096,
            "project_terminal_signal_attempts": True,
        }
        self._snapshot_retention_policy: SnapshotRetentionPolicy | None = None
        self._load_policy: dict[str, Any] = {}
        self._heartbeat_at: float | None = None
        self._lease_until: float | None = self._created_at + lease_ttl if lease_ttl is not None else None
        self._task_stage = Stage(on_adopted_done=self._handle_managed_task_done)
        self._retained_managed_tasks: set[asyncio.Task[Any]] = set()
        self._retained_task_failures: deque[tuple[str, BaseException]] = deque()
        self._task_stage_close_started = False
        self._accepted_signal_ids: set[str] = set()
        self._signal_net = TriggerFlowSignalNet(self)
        self._active_runtime_operation_count = 0
        self._active_handler_count = 0
        self._auto_close_task: asyncio.Task[Any] | None = None
        self._close_started = False
        self._close_result: Any = None
        self._terminal_state_frozen = False
        self._terminal_retention: TriggerFlowTerminalRetention | None = None
        self._closed_event = asyncio.Event()
        self._runtime_stream_stopped = False
        self._intervention_mode: TriggerFlowInterventionMode = intervention_mode
        self._intervention_policy = intervention_policy
        self._intervention_policy_name = self._resolve_intervention_policy_name(intervention_policy)
        self._interventions_version = 0
        self._intervention_lock = asyncio.Lock()

        # Settings
        self.settings = Settings(
            parent=self._trigger_flow.settings,
            name=f"TriggerFlowExecution-{ self.id }-Settings",
        )
        self.set_settings = self.settings.set_settings
        self.load_settings = self.settings.load

        # Emit
        self.emit = default_stage_call_bridge.as_sync(self.async_emit)
        self.emit_nowait = self._emit_nowait

        # Flow Data
        self._get_flow_data = self._trigger_flow._get_flow_data
        self._set_flow_data = self._trigger_flow._set_flow_data
        self._append_flow_data = self._trigger_flow._append_flow_data
        self._del_flow_data = self._trigger_flow._del_flow_data
        self._async_set_flow_data = self._trigger_flow._async_set_flow_data
        self._async_append_flow_data = self._trigger_flow._async_append_flow_data
        self._async_del_flow_data = self._trigger_flow._async_del_flow_data
        self.get_flow_data = self._trigger_flow.get_flow_data
        self.set_flow_data = self._trigger_flow.set_flow_data
        self.async_set_flow_data = self._trigger_flow.async_set_flow_data
        self.append_flow_data = self._trigger_flow.append_flow_data
        self.async_append_flow_data = self._trigger_flow.async_append_flow_data
        self.del_flow_data = self._trigger_flow.del_flow_data
        self.async_del_flow_data = self._trigger_flow.async_del_flow_data

        # Runtime Data
        self.get_state = self._get_state
        self.set_state = default_stage_call_bridge.as_sync(self.async_set_state)
        self.append_state = default_stage_call_bridge.as_sync(self.async_append_state)
        self.del_state = default_stage_call_bridge.as_sync(self.async_del_state)
        self.get_runtime_data = self._deprecated_get_runtime_data
        self.set_runtime_data = default_stage_call_bridge.as_sync(self.async_set_runtime_data)
        self.append_runtime_data = default_stage_call_bridge.as_sync(self.async_append_runtime_data)
        self.del_runtime_data = default_stage_call_bridge.as_sync(self.async_del_runtime_data)
        self.set_runtime_resource = self._set_runtime_resource
        self.get_runtime_resource = self._get_runtime_resource
        self.del_runtime_resource = self._del_runtime_resource
        self.update_runtime_resources = self._update_runtime_resources
        self.clear_runtime_resources = self._clear_runtime_resources
        self.declare_resource_requirement = self._declare_resource_requirement
        self.set_compaction_policy = self._set_compaction_policy
        self.set_snapshot_projection_policy = self._set_snapshot_projection_policy
        self.set_snapshot_retention_policy = self._set_snapshot_retention_policy

        # Runtime Stream
        self.put_into_stream = default_stage_call_bridge.as_sync(self.async_put_into_stream)
        self.stop_stream = default_stage_call_bridge.as_sync(self.async_stop_stream)

        # Pause / Continue
        self.pause_for = default_stage_call_bridge.as_sync(self.async_pause_for)
        self.continue_with = default_stage_call_bridge.as_sync(self.async_continue_with)
        self.intervene = default_stage_call_bridge.as_sync(self.async_intervene)
        self.mark_intervention_consumed = default_stage_call_bridge.as_sync(self.async_mark_intervention_consumed)
        self.emit_to_sub_flow = default_stage_call_bridge.as_sync(self.async_emit_to_sub_flow)
        self.cancel_sub_flow = default_stage_call_bridge.as_sync(self.async_cancel_sub_flow)

        # Result
        self.get_result = default_stage_call_bridge.as_sync(self.async_get_result)

        # Lifecycle
        self.seal = default_stage_call_bridge.as_sync(self.async_seal)
        self.unseal = default_stage_call_bridge.as_sync(self.async_unseal)
        self.close = default_stage_call_bridge.as_sync(self.async_close)

        # Execution Status
        self._started = False
        self._status = TRIGGER_FLOW_STATUS_CREATED
        self._system_runtime_data.set("status", self._status)
        self._system_runtime_data.set("lifecycle_state", self._lifecycle_state)
        self._system_runtime_data.set("state_version", self._state_version)
        self._system_runtime_data.set("interrupts", {})
        self._system_runtime_data.set("interventions", {})
        self._system_runtime_data.set("intervention_mode", self._intervention_mode)
        self._system_runtime_data.set("intervention_policy", self._intervention_policy_name)
        self._system_runtime_data.set("intervention_version", self._interventions_version)
        self._system_runtime_data.set("sub_flow_frames", {})
        self._system_runtime_data.set("last_signal", None)
        self._system_runtime_data.set("result", EMPTY)
        self._system_runtime_data.set("result_ready", asyncio.Event())
        self._runtime_stream_transport = StageRuntimeStreamTransport()
        self.result = TriggerFlowExecutionResult(self)
        self._interrupts = TriggerFlowExecutionInterrupts(self)
        self._persistence = TriggerFlowExecutionPersistence(self)
        self._runtime_io = TriggerFlowExecutionRuntimeIO(self)

    async def _ensure_execution_resources(self):
        if not self._execution_resource_requirements:
            return
        from agently.base import execution_resource

        owner_id = self._owner_id or self.id
        for requirement in self._execution_resource_requirements:
            normalized_requirement = dict(requirement)
            normalized_requirement.setdefault("scope", "execution")
            normalized_requirement.setdefault("owner_id", owner_id)
            handle = await execution_resource.async_ensure(
                cast(ExecutionResourceRequirement, normalized_requirement),
                scope="execution",
                owner_id=owner_id,
            )
            self._managed_execution_resource_handles.append(handle)
            resource_key = str(handle.get("resource_key", normalized_requirement.get("resource_key", "")))
            if resource_key:
                self.set_runtime_resource(resource_key, handle.get("resource"))

    async def _release_managed_execution_resources(self):
        if not self._managed_execution_resource_handles:
            return
        from agently.base import execution_resource

        handles = list(self._managed_execution_resource_handles)
        self._managed_execution_resource_handles.clear()
        for handle in handles:
            await execution_resource.async_release(handle)

    def _to_serializable_value(self, value: Any):
        return json.loads(StateData({"value": value}).dump("json"))["value"]

    def _copy_value(self, value: Any):
        return StateData({"value": value}).get("value")

    def _resolve_intervention_policy_name(self, policy: Any):
        if policy is None:
            return "builtin" if self._intervention_mode == "auto" else None
        return getattr(policy, "__name__", type(policy).__name__)

    def _get_intervention_records(self) -> dict[str, dict[str, Any]]:
        records = self._system_runtime_data.get("interventions", {}, inherit=False)
        return records if isinstance(records, dict) else {}

    def _write_intervention_records(self, records: dict[str, dict[str, Any]], *, bump: bool = True):
        serializable_records = self._to_serializable_value(records)
        self._system_runtime_data.set("interventions", serializable_records)
        self._system_runtime_data.set("intervention_version", self._interventions_version)
        self._runtime_data.set(INTERVENTIONS_STATE_KEY, serializable_records)
        if bump:
            self._bump_state_version()

    def _intervention_matches_target(self, intervention: dict[str, Any], target: str | None):
        intervention_target = intervention.get("target")
        if target is None:
            return intervention_target is None
        return intervention_target == target

    def _sorted_intervention_records(self):
        return sorted(
            (
                record
                for record in self._get_intervention_records().values()
                if isinstance(record, dict)
            ),
            key=lambda record: int(record.get("version", 0)),
        )

    def get_interventions(
        self,
        status: str | None = None,
        target: str | None = None,
        since_version: int | None = None,
    ):
        interventions = []
        for record in self._sorted_intervention_records():
            if status is not None and record.get("status") != status:
                continue
            if target is not None and record.get("target") != target:
                continue
            if since_version is not None and int(record.get("version", 0)) <= since_version:
                continue
            interventions.append(self._copy_value(record))
        return interventions

    def get_pending_interventions(self, target: str | None = None):
        return self.get_interventions(status="pending", target=target)

    def get_latest_intervention(self, default: Any = None, **filters: Any):
        interventions = self.get_interventions(**filters)
        if not interventions:
            return default
        return interventions[-1]

    def _get_visible_interventions_snapshot(self):
        return self.get_interventions(status="inserted")

    async def _emit_intervention_event(self, event_type: str, action: str, intervention: dict[str, Any]):
        serializable_intervention = self._to_serializable_value(intervention)
        stream_event = cast(
            TriggerFlowInterventionEvent,
            {
                "type": "intervention",
                "action": action,
                "execution_id": self.id,
                "intervention": serializable_intervention,
            },
        )
        await self.async_put_into_stream(stream_event, _skip_contract_validation=True)
        await self._emit_runtime_event(
            event_type,
            message=f"TriggerFlow execution '{ self.id }' intervention '{ action }'.",
            payload={"intervention": serializable_intervention},
        )

    async def async_intervene(
        self,
        payload: Any,
        *,
        author: str | None = None,
        note: str | None = None,
        target: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        if self._intervention_mode is None:
            raise RuntimeError(
                "TriggerFlow runtime intervention is disabled for this execution. "
                "Create the execution with intervention_mode='planned' or intervention_mode='auto'."
            )
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
            raise RuntimeError(
                f"TriggerFlow execution { self.id } can not accept interventions while lifecycle state is "
                f"'{ self._lifecycle_state }'."
            )
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError(f"TriggerFlow intervention metadata must be a dictionary, got: { type(metadata) }.")

        serializable_payload = self._to_serializable_value(payload)
        serializable_metadata = self._to_serializable_value(metadata or {})
        if target is not None:
            target = str(target)
        if author is not None:
            author = str(author)
        if note is not None:
            note = str(note)

        async with self._intervention_lock:
            self._interventions_version += 1
            intervention_id = uuid.uuid4().hex
            intervention = {
                "id": intervention_id,
                "version": self._interventions_version,
                "status": "pending",
                "payload": serializable_payload,
                "target": target,
                "author": author,
                "note": note,
                "metadata": serializable_metadata,
                "created_at": time.time(),
                "inserted_at": None,
                "insertion": None,
                "rejected_at": None,
                "reject_reason": None,
                "expired_at": None,
                "consumers": {},
            }
            records = self._get_intervention_records().copy()
            records[intervention_id] = intervention
            self._write_intervention_records(records)
            self._mark_activity()

        await self._emit_intervention_event(
            "triggerflow.intervention_received",
            "append",
            intervention,
        )
        return self._copy_value(intervention)

    def _build_intervention_insertion(
        self,
        *,
        mode: Literal["planned", "auto"],
        operator: dict[str, Any] | None,
        signal: TriggerFlowSignal | None,
        policy_name: str | None = None,
    ):
        inserted_at = time.time()
        insertion = {
            "mode": mode,
            "operator_id": operator.get("id") if isinstance(operator, dict) else None,
            "operator_name": operator.get("name") if isinstance(operator, dict) else None,
            "operator_kind": operator.get("kind") if isinstance(operator, dict) else None,
            "group_id": operator.get("group_id") if isinstance(operator, dict) else None,
            "group_kind": operator.get("group_kind") if isinstance(operator, dict) else None,
            "signal": self._serialize_signal(signal),
            "inserted_at": inserted_at,
        }
        if policy_name is not None:
            insertion["policy"] = policy_name
        return inserted_at, insertion

    async def _async_insert_interventions(
        self,
        intervention_ids: list[str],
        *,
        mode: Literal["planned", "auto"],
        operator: dict[str, Any] | None,
        signal: TriggerFlowSignal | None,
        policy_name: str | None = None,
    ):
        inserted: list[dict[str, Any]] = []
        if not intervention_ids:
            return inserted
        async with self._intervention_lock:
            records = self._get_intervention_records().copy()
            inserted_at, insertion = self._build_intervention_insertion(
                mode=mode,
                operator=operator,
                signal=signal,
                policy_name=policy_name,
            )
            for intervention_id in intervention_ids:
                record = records.get(str(intervention_id))
                if not isinstance(record, dict) or record.get("status") != "pending":
                    continue
                updated = dict(record)
                updated["status"] = "inserted"
                updated["inserted_at"] = inserted_at
                updated["insertion"] = self._copy_value(insertion)
                records[str(intervention_id)] = updated
                inserted.append(updated)
            if inserted:
                self._write_intervention_records(records)
                self._mark_activity()
        for intervention in inserted:
            await self._emit_intervention_event(
                "triggerflow.intervention_inserted",
                "insert",
                intervention,
            )
        return [self._copy_value(intervention) for intervention in inserted]

    async def _async_insert_planned_interventions(
        self,
        *,
        target: str | None,
        operator: dict[str, Any] | None,
        signal: TriggerFlowSignal | None,
    ):
        if self._intervention_mode != "planned":
            return []
        pending_ids = [
            record["id"]
            for record in self._sorted_intervention_records()
            if record.get("status") == "pending" and self._intervention_matches_target(record, target)
        ]
        return await self._async_insert_interventions(
            pending_ids,
            mode="planned",
            operator=operator,
            signal=signal,
        )

    def _operator_matches_intervention_target(self, operator: dict[str, Any], target: Any):
        if target is None:
            return False
        target_value = str(target)
        candidates = {
            str(operator.get("id", "")),
            str(operator.get("name", "")),
            str(operator.get("kind", "")),
            str(operator.get("group_id", "")),
            str(operator.get("group_kind", "")),
        }
        return target_value in candidates

    def _builtin_auto_intervention_ids(self, context: dict[str, Any]):
        operator = context["operator"]
        pending = context["pending_interventions"]
        selected: list[str] = []
        for intervention in pending:
            target = intervention.get("target")
            if target is not None and self._operator_matches_intervention_target(operator, target):
                selected.append(str(intervention["id"]))
                continue
            if target is None and operator.get("kind") == "chunk":
                selected.append(str(intervention["id"]))
        return selected

    async def _async_auto_intervention_ids(self, context: dict[str, Any]):
        if self._intervention_policy is None:
            return self._builtin_auto_intervention_ids(context)
        result = await default_stage_call_bridge.as_async(self._intervention_policy, managed=True)(context)
        if result is None:
            return []
        if isinstance(result, str):
            return [result]
        return [str(item) for item in result]

    async def _async_apply_auto_interventions(self, operator: dict[str, Any] | None, signal: TriggerFlowSignal):
        if self._intervention_mode != "auto" or operator is None:
            return []
        pending = self.get_pending_interventions()
        if not pending:
            return []
        context = {
            "execution_id": self.id,
            "flow_name": self._trigger_flow.name,
            "signal": signal.to_state_dict(),
            "operator": self._to_serializable_value(operator),
            "operator_id": operator.get("id"),
            "operator_name": operator.get("name"),
            "operator_kind": operator.get("kind"),
            "group_id": operator.get("group_id"),
            "group_kind": operator.get("group_kind"),
            "pending_interventions": pending,
            "state": self._runtime_state_snapshot(),
            "latest_inserted_version": max(
                [int(item.get("version", 0)) for item in self.get_interventions(status="inserted")] or [0]
            ),
        }
        try:
            selected_ids = await self._async_auto_intervention_ids(context)
        except Exception as error:
            await self._emit_runtime_event(
                "triggerflow.intervention_rejected",
                level="WARNING",
                message=f"TriggerFlow execution '{ self.id }' intervention policy failed.",
                payload={"operator_id": operator.get("id")},
                error=error,
            )
            return []
        return await self._async_insert_interventions(
            selected_ids,
            mode="auto",
            operator=operator,
            signal=signal,
            policy_name=self._intervention_policy_name,
        )

    async def async_mark_intervention_consumed(
        self,
        intervention_id: str,
        *,
        consumer: str,
        status: Literal["applied", "ignored"] = "applied",
        note: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        if status not in {"applied", "ignored"}:
            raise ValueError("TriggerFlow intervention consumption status must be 'applied' or 'ignored'.")
        if not consumer:
            raise ValueError("TriggerFlow intervention consumer must be a non-empty string.")
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError(f"TriggerFlow intervention metadata must be a dictionary, got: { type(metadata) }.")
        async with self._intervention_lock:
            records = self._get_intervention_records().copy()
            if intervention_id not in records:
                raise KeyError(f"TriggerFlow intervention '{ intervention_id }' not found.")
            intervention = dict(records[intervention_id])
            consumers = intervention.get("consumers", {})
            if not isinstance(consumers, dict):
                consumers = {}
            consumers[str(consumer)] = {
                "status": status,
                "note": str(note) if note is not None else None,
                "metadata": self._to_serializable_value(metadata or {}),
                "consumed_at": time.time(),
            }
            intervention["consumers"] = consumers
            records[intervention_id] = intervention
            self._write_intervention_records(records)
            self._mark_activity()
        await self._emit_intervention_event(
            "triggerflow.intervention_consumed",
            "consume",
            intervention,
        )
        return self._copy_value(intervention)

    async def _async_expire_pending_interventions(self):
        expired: list[dict[str, Any]] = []
        async with self._intervention_lock:
            records = self._get_intervention_records().copy()
            expired_at = time.time()
            for intervention_id, intervention in list(records.items()):
                if not isinstance(intervention, dict) or intervention.get("status") != "pending":
                    continue
                updated = dict(intervention)
                updated["status"] = "expired"
                updated["expired_at"] = expired_at
                records[intervention_id] = updated
                expired.append(updated)
            if expired:
                self._write_intervention_records(records)
        for intervention in expired:
            await self._emit_intervention_event(
                "triggerflow.intervention_expired",
                "expire",
                intervention,
            )
        return expired

    def _set_status(self, status: str):
        self._status = status
        self._system_runtime_data.set("status", status)

    def _bump_state_version(self):
        self._state_version += 1
        self._system_runtime_data.set("state_version", self._state_version)

    def _set_lifecycle_state(self, state: str):
        if self._lifecycle_state == state:
            return
        self._lifecycle_state = state
        self._system_runtime_data.set("lifecycle_state", state)
        self._bump_state_version()

    def _mark_activity(self):
        self._last_activity_at = time.time()
        self._ensure_auto_close_monitor()

    def get_lifecycle_state(self):
        return self._lifecycle_state

    @property
    def run_id(self) -> str:
        return self.run_context.run_id or self.id

    @property
    def started(self) -> bool:
        """True once async_start has begun this execution's run."""
        return self._started

    def is_open(self):
        return self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_OPEN

    def is_sealed(self):
        return self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_SEALED

    def is_closed(self):
        return self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED

    def is_idle(self):
        return (
            self._active_runtime_operation_count == 0
            and self._active_handler_count == 0
            and not any(task is not self._auto_close_task and not task.done() for task in self._pending_tasks)
        )

    @property
    def _pending_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Private compatibility snapshot over the Stage-owned task inventory."""

        return self._task_stage.adopted_tasks

    @property
    def _task_origins(self) -> dict[asyncio.Task[Any], str]:
        """Private compatibility snapshot over owner-qualified task origins."""

        return {
            task: origin
            for task in self._task_stage.adopted_tasks
            if (origin := self._task_stage.origin_for_adopted(task)) is not None
        }

    def _warn_runtime_data_api(self, method_name: str):
        DeprecationWarnings.warn_deprecated_once(
            f"TriggerFlowExecution.{ method_name }",
            f"TriggerFlowExecution.{ method_name }() is deprecated; "
            "use execution state APIs such as get_state()/set_state() instead.",
            stacklevel=3,
        )

    def _deprecated_get_runtime_data(
        self,
        key: Any | None = None,
        default: Any = None,
        *,
        inherit: bool = True,
    ):
        self._warn_runtime_data_api("get_runtime_data")
        return self._get_state(key, default, inherit=inherit)

    def _get_state(
        self,
        key: Any | None = None,
        default: Any = None,
        *,
        inherit: bool = True,
    ):
        return self._runtime_data.get(key, default, inherit=inherit)

    def _runtime_state_snapshot(self):
        data = self._runtime_data.get(None, {}, inherit=False)
        return data if isinstance(data, dict) else {}

    def _compat_result_exists(self):
        return self._runtime_io.compat_result_exists()

    def _get_compat_result(self):
        return self._runtime_io.get_compat_result()

    def _build_close_snapshot(self):
        return self._runtime_io.build_close_snapshot()

    def _ensure_terminal_state_mutable(self):
        if (
            self._terminal_state_frozen
            and self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_CLOSED
        ):
            raise RuntimeError(
                f"TriggerFlow execution {self.id} can not mutate state because its "
                "terminal close snapshot is frozen. Retry async_close() with the existing terminal unit."
            )

    def _clear_transient_aggregation_state(self):
        for key in TRANSIENT_AGGREGATION_STATE_KEYS:
            self._system_runtime_data.pop(key, None)

    async def _async_wait_for_compat_result_or_close(self, *, timeout: float | None = None):
        return await self._runtime_io.async_wait_for_compat_result_or_close(timeout=timeout)

    async def _async_wait_for_close_snapshot(self, *, timeout: float | None = None):
        return await self._runtime_io.async_wait_for_close_snapshot(timeout=timeout)

    def _handle_managed_task_done(
        self,
        task: asyncio.Task[Any],
        origin: str,
    ) -> None:
        retained = task in self._retained_managed_tasks
        self._retained_managed_tasks.discard(task)
        if retained and not task.cancelled():
            error = task.exception()
            if error is not None:
                self._retained_task_failures.append((origin, error))
        self._mark_activity()
        if (
            self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED
            and self._task_stage.adopted_count == 0
            and not self._task_stage_close_started
        ):
            self._task_stage_close_started = True
            asyncio.create_task(self._task_stage.async_close(timeout=0))

    def _create_managed_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        origin: str,
        retain_outcome: bool | None = None,
    ) -> asyncio.Task[Any]:
        if retain_outcome is None:
            retain_outcome = not origin.startswith("handler:")
        task = self._task_stage.create_task(coroutine, origin=origin)
        if retain_outcome:
            self._retained_managed_tasks.add(task)
        self._ensure_auto_close_monitor()
        return task

    def _track_task(
        self,
        task: asyncio.Task[Any],
        *,
        origin: str,
        retain_outcome: bool | None = None,
    ):
        if retain_outcome is None:
            retain_outcome = not origin.startswith("handler:")
        if retain_outcome:
            self._retained_managed_tasks.add(task)
        try:
            self._task_stage.adopt(task, origin=origin)
        except BaseException:
            self._retained_managed_tasks.discard(task)
            raise
        self._ensure_auto_close_monitor()
        return task

    async def _drain_pending_tasks(self, *, timeout: float | None = None):
        current_task = asyncio.current_task()
        deadline = _create_deadline(timeout)
        results: list[Any] = []

        def collect_retained_results() -> None:
            while self._retained_task_failures:
                _, error = self._retained_task_failures.popleft()
                results.append(error)

        collect_retained_results()
        while True:
            pending = [
                task
                for task in self._task_stage.adopted_tasks
                if task is not current_task and task is not self._auto_close_task and not task.done()
            ]
            if current_task in self._task_stage.adopted_tasks:
                pending = [
                    task
                    for task in pending
                    if not (self._task_stage.origin_for_adopted(task) or "").startswith("emit")
                ]
            if not pending:
                await asyncio.sleep(0)
                collect_retained_results()
                return results

            if deadline is None:
                await asyncio.gather(*pending, return_exceptions=True)
                await asyncio.sleep(0)
                collect_retained_results()
                continue

            _, unresolved = await asyncio.wait(
                pending,
                timeout=_remaining_timeout(deadline),
            )
            await asyncio.sleep(0)
            collect_retained_results()
            if unresolved:
                unresolved_origins = sorted(
                    self._task_stage.origin_for_adopted(task) or "<unknown>"
                    for task in unresolved
                )
                warnings.warn(
                    f"TriggerFlow execution { self.id } closed before { len(unresolved) } pending task(s) finished.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                await self._emit_runtime_event(
                    "triggerflow.pending_tasks_cancelled",
                    level="WARNING",
                    message=f"TriggerFlow execution '{ self.id }' cancelled pending tasks during close.",
                    payload={
                        "pending_task_count": len(unresolved),
                        "timeout": timeout,
                        "pending_task_origins": unresolved_origins,
                    },
                )
                self._retained_managed_tasks.difference_update(unresolved)
                for task in unresolved:
                    task.cancel()
                await asyncio.sleep(0)
                still_unresolved = {task for task in unresolved if not task.done()}
                remaining = _remaining_timeout(deadline)
                if still_unresolved and remaining is not None and remaining > 0:
                    _, still_unresolved = await asyncio.wait(
                        still_unresolved,
                        timeout=remaining,
                    )
                if still_unresolved:
                    origins = sorted(
                        self._task_stage.origin_for_adopted(task) or "<unknown>"
                        for task in still_unresolved
                    )
                    raise TimeoutError(
                        "TriggerFlow managed-task cancellation did not settle before "
                        f"the close deadline; unresolved origins: {origins}"
                    )
                collect_retained_results()
                return results

    async def _raise_pending_task_errors(self, results: list[Any]):
        if self._skip_exceptions:
            return
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                if self._status == TRIGGER_FLOW_STATUS_CANCELLED:
                    continue
                self._set_status(TRIGGER_FLOW_STATUS_CANCELLED)
                await self._emit_runtime_event(
                    "triggerflow.execution_cancelled",
                    level="WARNING",
                    message=f"TriggerFlow execution '{ self.id }' was cancelled while draining managed tasks.",
                    payload={"reason": "managed_task_cancelled"},
                )
                raise result
            if isinstance(result, BaseException):
                self._set_status(TRIGGER_FLOW_STATUS_FAILED)
                if not self._runtime_failed_emitted:
                    self._runtime_failed_emitted = True
                    await self._emit_runtime_event(
                        "triggerflow.execution_failed",
                        level="ERROR",
                        message=f"TriggerFlow execution '{ self.id }' failed while draining managed tasks.",
                        payload={"reason": "managed_task_failed"},
                        error=result,
                    )
                raise result

    def _ensure_auto_close_monitor(self):
        if (
            not self._auto_close
            or self._auto_close_timeout is None
            or self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._auto_close_task is None or self._auto_close_task.done():
            self._auto_close_task = loop.create_task(self._auto_close_monitor())

    async def _auto_close_monitor(self):
        timeout = self._auto_close_timeout
        if timeout is None:
            return
        while self._auto_close and self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_CLOSED:
            sleep_seconds = min(max(timeout / 4, 0.05), 1.0)
            await asyncio.sleep(sleep_seconds)
            if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
                continue
            if self.is_waiting():
                continue
            if self._last_activity_at is None or not self.is_idle():
                continue
            if time.time() - self._last_activity_at < timeout:
                continue
            await self._emit_runtime_event(
                "triggerflow.auto_close_timeout",
                level="DEBUG",
                message=f"TriggerFlow execution '{ self.id }' reached auto-close idle timeout.",
                payload={
                    "timeout": timeout,
                    "last_activity_at": self._last_activity_at,
                },
            )
            await self.async_close(reason="auto_close_idle_timeout")
            break

    async def _reject_signal(self, signal: TriggerFlowSignal):
        warnings.warn(
            f"TriggerFlow execution { self.id } ignored event '{ signal.trigger_event }' "
            f"because lifecycle state is '{ self._lifecycle_state }'.",
            RuntimeWarning,
            stacklevel=3,
        )
        await self._emit_runtime_event(
            "triggerflow.event_rejected",
            level="WARNING",
            message=(
                f"TriggerFlow execution '{ self.id }' ignored event '{ signal.trigger_event }' "
                f"because lifecycle state is '{ self._lifecycle_state }'."
            ),
            payload={
                "lifecycle_state": self._lifecycle_state,
                "signal": signal.to_debug_dict(),
            },
        )

    def _accepts_signal_in_current_lifecycle(
        self,
        signal: TriggerFlowSignal,
        *,
        preaccepted: bool = False,
    ):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_OPEN:
            return True
        if preaccepted:
            return True
        return self._close_started and signal.source in {
            "chunk",
            "runtime_data",
            "flow_data",
            "interrupt",
        }

    async def async_seal(self, *, reason: str = "manual"):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            return self
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_SEALED:
            self._sealed_at = time.time()
            self._set_lifecycle_state(TRIGGER_FLOW_LIFECYCLE_SEALED)
            await self._emit_runtime_event(
                "triggerflow.execution_sealed",
                message=f"TriggerFlow execution '{ self.id }' sealed.",
                payload={
                    "reason": reason,
                    "sealed_at": self._sealed_at,
                },
            )
        return self

    async def async_unseal(self, *, reason: str = "manual"):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            warnings.warn(
                f"TriggerFlow execution { self.id } can not be unsealed because it is closed.",
                RuntimeWarning,
                stacklevel=2,
            )
            await self._emit_runtime_event(
                "triggerflow.unseal_rejected",
                level="WARNING",
                message=f"TriggerFlow execution '{ self.id }' can not be unsealed because it is closed.",
                payload={"reason": reason},
            )
            return self
        self._ensure_terminal_state_mutable()
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
            self._set_lifecycle_state(TRIGGER_FLOW_LIFECYCLE_OPEN)
            self._mark_activity()
            await self._emit_runtime_event(
                "triggerflow.execution_unsealed",
                message=f"TriggerFlow execution '{ self.id }' unsealed.",
                payload={"reason": reason},
            )
        return self

    async def async_close(
        self,
        *,
        reason: str = "manual",
        timeout: float | None = None,
        seal: bool = True,
        pending_interrupts: PendingInterruptClosePolicy = "error",
    ):
        if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            return self._close_result
        async with self._close_lock:
            if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
                return self._close_result
            self._validate_pending_interrupt_close_policy(pending_interrupts)

            self._close_started = True
            self._close_reason = reason
            sealed_for_close = False
            try:
                await self._handle_pending_interrupts_before_close(
                    pending_interrupts=pending_interrupts,
                    reason=reason,
                )

                if seal:
                    await self.async_seal(reason=reason)
                    sealed_for_close = True

                pending_task_results = await self._drain_pending_tasks(timeout=timeout)
                await self._raise_pending_task_errors(pending_task_results)
                await self._handle_pending_interrupts_before_close(
                    pending_interrupts=pending_interrupts,
                    reason=reason,
                )
                await self._async_expire_pending_interventions()
                self._clear_transient_aggregation_state()

                if self._terminal_retention is None:
                    result = self._build_close_snapshot()
                    self._terminal_state_frozen = True
                else:
                    result = self._terminal_retention["close_result"]
                if self._status not in {TRIGGER_FLOW_STATUS_FAILED, TRIGGER_FLOW_STATUS_CANCELLED}:
                    self._set_status(TRIGGER_FLOW_STATUS_COMPLETED)
                if self._terminal_retention is None:
                    self._terminal_retention = (
                        await prepare_triggerflow_terminal_retention(
                            self,
                            result,
                        )
                    )
                terminal_retention = self._terminal_retention
                if self._status not in {TRIGGER_FLOW_STATUS_FAILED, TRIGGER_FLOW_STATUS_CANCELLED}:
                    if not self._runtime_completed_emitted:
                        await self._emit_runtime_event(
                            "triggerflow.execution_completed",
                            message=f"TriggerFlow execution '{ self.id }' completed.",
                            payload=dict(terminal_retention["completed_event_payload"]),
                            event_id=terminal_retention["completed_event_id"],
                            event_timestamp=terminal_retention["completed_event_timestamp"],
                            state_version=terminal_retention["completed_event_state_version"],
                        )
                        self._runtime_completed_emitted = True

                await self.async_stop_stream()
                await self._release_managed_execution_resources()

                self._close_result = result
                closed_event_payload = terminal_retention["closed_event_payload"]
                if closed_event_payload is None:
                    self._closed_at = time.time()
                    record_store_configured = (
                        self._get_runtime_resource("record_store", None) is not None
                    )
                    prepared_result = terminal_retention["result"]
                    retention_status = (
                        str(prepared_result["status"])
                        if prepared_result is not None
                        else ("pending" if record_store_configured else "not_configured")
                    )
                    closed_event_payload = {
                        "reason": reason,
                        "closed_at": self._closed_at,
                        "execution_id": self.id,
                        "retention_status": retention_status,
                    }
                    terminal_retention["closed_event_payload"] = closed_event_payload
                    terminal_retention["closed_event_timestamp"] = int(time.time() * 1000)
                    terminal_retention["closed_event_state_version"] = self._state_version + 1
                else:
                    self._closed_at = cast(float, closed_event_payload["closed_at"])
                    self._close_reason = str(closed_event_payload["reason"])
                closed_event_task = asyncio.create_task(
                    self._emit_runtime_event(
                        "triggerflow.execution_closed",
                        message=f"TriggerFlow execution '{ self.id }' closed.",
                        payload=dict(closed_event_payload),
                        event_id=terminal_retention["closed_event_id"],
                        event_timestamp=cast(
                            int,
                            terminal_retention["closed_event_timestamp"],
                        ),
                        state_version=cast(
                            int,
                            terminal_retention["closed_event_state_version"],
                        ),
                    )
                )
                postcommit_error: BaseException | None = None
                try:
                    await asyncio.shield(closed_event_task)
                except asyncio.CancelledError as error:
                    postcommit_error = error
                    if not closed_event_task.done():
                        await asyncio.shield(closed_event_task)
                self._set_lifecycle_state(TRIGGER_FLOW_LIFECYCLE_CLOSED)
                try:
                    terminal_retention = await apply_triggerflow_terminal_retention(
                        self,
                        terminal_retention,
                    )
                    self._terminal_retention = terminal_retention
                except BaseException as error:
                    postcommit_error = error
                retention_result = terminal_retention["result"]
                if retention_result is not None:
                    retention_event_type = (
                        "triggerflow.execution_retention_applied"
                        if retention_result["status"] in {"applied", "noop"}
                        else "triggerflow.execution_retention_deferred"
                    )
                    try:
                        await self._emit_runtime_event(
                            retention_event_type,
                            level=(
                                "INFO"
                                if retention_result["status"] in {"applied", "noop"}
                                else "WARNING"
                            ),
                            message=(
                                f"TriggerFlow execution '{self.id}' terminal retention "
                                f"finished with status '{retention_result['status']}'."
                            ),
                            payload={
                                "execution_id": self.id,
                                "status": retention_result["status"],
                                "retained_ref_count": len(
                                    retention_result["retained_refs"]
                                ),
                                "recovery_snapshot_status": retention_result[
                                    "recovery_snapshot_status"
                                ],
                                "diagnostics": retention_result["diagnostics"],
                            },
                            persist=False,
                        )
                    except BaseException as error:
                        if not isinstance(error, Exception):
                            postcommit_error = postcommit_error or error
                self._closed_event.set()
                self._trigger_flow.remove_execution(self)
                if (
                    self._task_stage.adopted_count == 0
                    and not self._task_stage_close_started
                ):
                    self._task_stage_close_started = True
                    await self._task_stage.async_close(timeout=0)

                if self._auto_close_task is not None and self._auto_close_task is not asyncio.current_task():
                    self._auto_close_task.cancel()
                if postcommit_error is not None:
                    raise postcommit_error
                return self._close_result
            except BaseException:
                if self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
                    raise
                self._close_started = False
                self._close_reason = None
                if self._terminal_retention is None:
                    self._terminal_state_frozen = False
                if (
                    sealed_for_close
                    and self._terminal_retention is None
                    and self._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_SEALED
                ):
                    await self.async_unseal(reason="close_failed")
                raise

    async def _emit_runtime_event(
        self,
        event_type: str,
        *,
        level: str = "INFO",
        message: str | None = None,
        payload: Any = None,
        error: BaseException | None = None,
        persist: bool = True,
        event_id: str | None = None,
        event_timestamp: int | None = None,
        state_version: int | None = None,
    ):
        from agently.base import async_emit_runtime

        event_data: dict[str, Any] = {
            "event_type": event_type,
            "source": "TriggerFlowExecution",
            "level": level,
            "message": message,
            "payload": payload,
            "error": error,
            "run": self.run_context,
            "meta": {"execution_id": self.id},
        }
        if event_id is not None:
            event_data["event_id"] = event_id
        if event_timestamp is not None:
            event_data["timestamp"] = event_timestamp
        event = RuntimeEvent.model_validate(
            event_data
        )
        persistence_error: BaseException | None = None
        if persist:
            try:
                await self._persist_runtime_event(
                    event,
                    state_version=state_version,
                )
            except BaseException as error:
                persistence_error = error
        try:
            await async_emit_runtime(event)
        except BaseException as delivery_error:
            if persistence_error is not None:
                raise persistence_error from delivery_error
            raise
        if persistence_error is not None:
            raise persistence_error

    async def _emit_runtime_definition_event(self):
        if self._runtime_definition_emitted:
            return
        self._runtime_definition_emitted = True
        await self._emit_runtime_event(
            "triggerflow.definition_declared",
            message=f"TriggerFlow definition declared for execution '{ self.id }'.",
            payload={
                "flow_name": self._trigger_flow.name,
                "definition": self._to_serializable_value(
                    self._trigger_flow.get_flow_config(validate_serializable=False)
                ),
                "mermaid": {
                    "simplified": self._trigger_flow.to_mermaid(mode="simplified"),
                    "detailed": self._trigger_flow.to_mermaid(mode="detailed"),
                },
            },
        )

    def _get_handler_operator(self, handler_id: str):
        try:
            return self._trigger_flow._blue_print.definition.get_operator(handler_id)
        except KeyError:
            return None

    def _serialize_operator_signals(self, signals: Any):
        if not isinstance(signals, list):
            return []
        serialized: list[dict[str, Any]] = []
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            trigger_event = signal.get("trigger_event")
            trigger_type = signal.get("trigger_type")
            if not isinstance(trigger_event, str) or not isinstance(trigger_type, str):
                continue
            serialized_signal: dict[str, Any] = {
                "trigger_event": trigger_event,
                "trigger_type": trigger_type,
            }
            role = signal.get("role")
            if isinstance(role, str):
                serialized_signal["role"] = role
            signal_id = signal.get("id")
            if isinstance(signal_id, str):
                serialized_signal["id"] = signal_id
            serialized.append(serialized_signal)
        return serialized

    def _get_origin_chunk_payload(self):
        chunk_run_context = get_current_chunk_run_context()
        if chunk_run_context is None:
            return None
        return {
            "run_id": chunk_run_context.run_id,
            "chunk_id": chunk_run_context.meta.get("chunk_id"),
            "chunk_name": chunk_run_context.meta.get("chunk_name"),
            "operator_kind": chunk_run_context.meta.get("operator_kind"),
        }

    def _serialize_runtime_value(self, value: Any):
        try:
            return self._to_serializable_value(value)
        except Exception:
            return {
                "__repr__": repr(value),
                "__type__": type(value).__name__,
            }

    def _create_chunk_run_context(self, operator: dict[str, Any], signal: TriggerFlowSignal):
        operator_kind = str(operator.get("kind", "chunk"))
        operator_name = str(operator.get("name") or operator_kind)
        return self.run_context.create_child(
            run_kind="chunk_execution",
            execution_id=self.id,
            meta={
                "flow_name": self._trigger_flow.name,
                "chunk_id": str(operator.get("id", "")),
                "chunk_name": operator_name,
                "operator_kind": operator_kind,
                "trigger_event": signal.trigger_event,
                "trigger_type": signal.trigger_type,
                "signal_id": signal.id,
                "group_id": operator.get("group_id"),
                "group_kind": operator.get("group_kind"),
                "parent_group_id": operator.get("parent_group_id"),
                "parent_group_kind": operator.get("parent_group_kind"),
                "listen_signals": self._serialize_operator_signals(operator.get("listen_signals")),
                "emit_signals": self._serialize_operator_signals(operator.get("emit_signals")),
            },
        )

    async def _emit_chunk_runtime_event(
        self,
        event_type: str,
        chunk_run_context: RunContext,
        *,
        operator: dict[str, Any],
        signal: TriggerFlowSignal,
        level: str = "INFO",
        message: str | None = None,
        payload: Any = None,
        error: BaseException | None = None,
    ):
        from agently.base import async_emit_runtime

        operator_kind = str(operator.get("kind", "chunk"))
        operator_name = str(operator.get("name") or operator_kind)
        base_payload = {
            "chunk_id": str(operator.get("id", "")),
            "chunk_name": operator_name,
            "operator_kind": operator_kind,
            "trigger_event": signal.trigger_event,
            "trigger_type": signal.trigger_type,
            "signal_id": signal.id,
        }
        if isinstance(payload, dict):
            base_payload.update(payload)
        elif payload is not None:
            base_payload["value"] = payload
        event = RuntimeEvent.model_validate(
            {
                "event_type": event_type,
                "source": "TriggerFlowExecution",
                "level": level,
                "message": message,
                "payload": base_payload,
                "error": error,
                "run": chunk_run_context,
                "meta": {"execution_id": self.id},
            }
        )
        await self._persist_runtime_event(event)
        await async_emit_runtime(event)

    def _runtime_event_node_id(self, event: RuntimeEvent):
        payload = event.payload
        if isinstance(payload, dict):
            for key in ("node_id", "chunk_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            origin_chunk = payload.get("origin_chunk")
            if isinstance(origin_chunk, dict):
                value = origin_chunk.get("chunk_id")
                if isinstance(value, str) and value:
                    return value
        meta = event.meta or {}
        value = meta.get("node_id")
        return value if isinstance(value, str) and value else None

    def _runtime_event_payload_meta(self, event: RuntimeEvent):
        payload = event.payload
        if not isinstance(payload, dict):
            return {}
        meta = payload.get("META")
        if isinstance(meta, dict):
            return meta
        signal_meta = payload.get("signal_meta")
        if isinstance(signal_meta, dict):
            return signal_meta
        return {}

    def _runtime_event_aggregation_scope(self, event: RuntimeEvent):
        meta = event.meta or {}
        value = meta.get("aggregation_scope")
        if isinstance(value, str) and value:
            return value
        payload_meta = self._runtime_event_payload_meta(event)
        value = payload_meta.get(AGGREGATION_SCOPE_META_KEY) or payload_meta.get("aggregation_scope")
        if isinstance(value, str) and value:
            return value
        if event.run is None:
            return None
        return event.run.root_run_id or event.run.run_id

    def _runtime_event_parent_signal_id(self, event: RuntimeEvent):
        meta = event.meta or {}
        value = meta.get("parent_signal_id")
        if isinstance(value, str) and value:
            return value
        payload = event.payload
        if isinstance(payload, dict):
            for key in ("parent_signal_id", "signal_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            payload_meta = self._runtime_event_payload_meta(event)
            value = payload_meta.get(PARENT_SIGNAL_ID_META_KEY) or payload_meta.get("parent_signal_id")
            if isinstance(value, str) and value:
                return value
        return None

    def _runtime_event_operator_id(self, event: RuntimeEvent):
        payload = event.payload
        if isinstance(payload, dict):
            for key in ("operator_id", "chunk_id", "handler"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            payload_meta = self._runtime_event_payload_meta(event)
            origin_chunk = payload_meta.get("origin_chunk")
            if isinstance(origin_chunk, dict):
                value = origin_chunk.get("chunk_id")
                if isinstance(value, str) and value:
                    return value
        meta = event.meta or {}
        value = meta.get("operator_id")
        return value if isinstance(value, str) and value else None

    def _runtime_event_interrupt_id(self, event: RuntimeEvent):
        payload = event.payload
        if isinstance(payload, dict):
            value = payload.get("interrupt_id")
            if isinstance(value, str) and value:
                return value
            interrupt = payload.get("interrupt")
            if isinstance(interrupt, dict):
                value = interrupt.get("id")
                if isinstance(value, str) and value:
                    return value
            payload_meta = self._runtime_event_payload_meta(event)
            value = payload_meta.get("interrupt_id")
            if isinstance(value, str) and value:
                return value
        meta = event.meta or {}
        value = meta.get("interrupt_id")
        return value if isinstance(value, str) and value else None

    def _runtime_event_resume_request_id(self, event: RuntimeEvent):
        payload = event.payload
        if isinstance(payload, dict):
            value = payload.get("resume_request_id")
            if isinstance(value, str) and value:
                return value
            interrupt = payload.get("interrupt")
            if isinstance(interrupt, dict):
                value = interrupt.get("resume_request_id")
                if isinstance(value, str) and value:
                    return value
            payload_meta = self._runtime_event_payload_meta(event)
            value = payload_meta.get("resume_request_id")
            if isinstance(value, str) and value:
                return value
        meta = event.meta or {}
        value = meta.get("resume_request_id")
        return value if isinstance(value, str) and value else None

    def _runtime_event_actor_id(self, event: RuntimeEvent):
        payload = event.payload
        if isinstance(payload, dict):
            for key in ("actor_id", "actor", "resumed_by"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            interrupt = payload.get("interrupt")
            if isinstance(interrupt, dict):
                for key in ("actor_id", "resumed_by"):
                    value = interrupt.get(key)
                    if isinstance(value, str) and value:
                        return value
            payload_meta = self._runtime_event_payload_meta(event)
            for key in ("actor_id", "actor"):
                value = payload_meta.get(key)
                if isinstance(value, str) and value:
                    return value
        meta = event.meta or {}
        value = meta.get("actor_id")
        return value if isinstance(value, str) and value else None

    def _runtime_event_exchange_id(self, event: RuntimeEvent):
        payload = event.payload
        if isinstance(payload, dict):
            for key in ("exchange_id", "external_wait_request_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            for envelope_key in ("external_wait_request", "request"):
                envelope = payload.get(envelope_key)
                value = self._runtime_event_exchange_id_from_external_wait(envelope)
                if value:
                    return value
            interrupt = payload.get("interrupt")
            if isinstance(interrupt, dict):
                value = self._runtime_event_exchange_id_from_external_wait(
                    interrupt.get("external_wait_request")
                )
                if value:
                    return value
        meta = event.meta or {}
        value = meta.get("exchange_id")
        return value if isinstance(value, str) and value else None

    def _runtime_event_exchange_id_from_external_wait(self, envelope: Any):
        if not isinstance(envelope, dict):
            return None
        value = envelope.get("exchange_id")
        if isinstance(value, str) and value:
            return value
        audit_metadata = envelope.get("audit_metadata")
        if isinstance(audit_metadata, dict):
            value = audit_metadata.get("exchange_id")
            if isinstance(value, str) and value:
                return value
        return None

    async def _persist_runtime_event(
        self,
        event: RuntimeEvent,
        *,
        state_version: int | None = None,
    ):
        runtime_event_store = self._runtime_event_store
        if runtime_event_store is None:
            return None
        append_runtime_event = runtime_event_store.append_runtime_event
        append_kwargs = self._filter_callable_kwargs(
            append_runtime_event,
            {
                "idempotency_key": event.event_id,
                "state_version": (
                    self._state_version
                    if state_version is None
                    else state_version
                ),
                "parent_signal_id": self._runtime_event_parent_signal_id(event),
                "node_id": self._runtime_event_node_id(event),
                "operator_id": self._runtime_event_operator_id(event),
                "interrupt_id": self._runtime_event_interrupt_id(event),
                "resume_request_id": self._runtime_event_resume_request_id(event),
                "actor_id": self._runtime_event_actor_id(event),
                "exchange_id": self._runtime_event_exchange_id(event),
                "lease_owner_id": self._owner_id,
                "aggregation_scope": self._runtime_event_aggregation_scope(event),
            },
        )
        return await append_runtime_event(self.id, event, **append_kwargs)

    def _filter_callable_kwargs(self, callable_object: Any, kwargs: dict[str, Any]):
        try:
            signature = inspect.signature(callable_object)
        except (TypeError, ValueError):
            return kwargs
        parameters = signature.parameters.values()
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            return kwargs
        accepted_names = {
            name
            for name, parameter in signature.parameters.items()
            if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        }
        return {key: value for key, value in kwargs.items() if key in accepted_names}

    def get_status(self):
        return self._status

    def is_waiting(self):
        return self._interrupts.is_waiting()

    def _get_interrupts(self) -> dict[str, Any]:
        return self._interrupts.get_interrupts()

    def get_interrupt(self, interrupt_id: str):
        return self._interrupts.get_interrupt(interrupt_id)

    def get_pending_interrupts(self):
        return self._interrupts.get_pending_interrupts()

    def _has_pending_interrupts(self):
        return self._interrupts.has_pending_interrupts()

    def _refresh_waiting_status(self):
        return self._interrupts.refresh_waiting_status()

    def _validate_pending_interrupt_close_policy(self, policy: str):
        return self._interrupts.validate_pending_interrupt_close_policy(policy)

    async def _handle_pending_interrupts_before_close(
        self,
        *,
        pending_interrupts: PendingInterruptClosePolicy,
        reason: str,
    ):
        return await self._interrupts.handle_pending_interrupts_before_close(
            pending_interrupts=pending_interrupts,
            reason=reason,
        )

    async def _cancel_pending_interrupts(self, *, reason: str):
        return await self._interrupts.cancel_pending_interrupts(reason=reason)

    def _get_sub_flow_frames(self) -> dict[str, Any]:
        frames = self._system_runtime_data.get("sub_flow_frames", {}, inherit=False)
        return frames if isinstance(frames, dict) else {}

    def get_sub_flow_frames(self):
        return copy.deepcopy(self._get_sub_flow_frames())

    def _set_sub_flow_frame(self, frame_id: str, frame: dict[str, Any]):
        frames = self._get_sub_flow_frames().copy()
        frames[str(frame_id)] = frame
        self._system_runtime_data.set("sub_flow_frames", frames)
        self._bump_state_version()
        return frame

    def _register_live_sub_flow(
        self,
        frame_id: str,
        child_execution: "TriggerFlowExecution[Any, Any, Any]",
        control_task: asyncio.Task[Any],
        frame: dict[str, Any],
    ):
        normalized_frame_id = str(frame_id)
        self._live_sub_flow_executions[normalized_frame_id] = child_execution
        self._live_sub_flow_tasks[normalized_frame_id] = control_task
        self._set_sub_flow_frame(normalized_frame_id, frame)

    def _unregister_live_sub_flow(
        self,
        frame_id: str,
        child_execution: "TriggerFlowExecution[Any, Any, Any]",
    ):
        normalized_frame_id = str(frame_id)
        if self._live_sub_flow_executions.get(normalized_frame_id) is child_execution:
            self._live_sub_flow_executions.pop(normalized_frame_id, None)
            self._live_sub_flow_tasks.pop(normalized_frame_id, None)
            self._live_sub_flow_signal_tasks.pop(normalized_frame_id, None)

    def _sub_flow_event_payload(self, frame: dict[str, Any], **extra: Any):
        payload = {
            "execution_id": self.id,
            "parent_execution_id": frame.get("parent_execution_id", self.id),
            "parent_operator_id": frame.get("parent_operator_id"),
            "frame_id": frame.get("frame_id"),
            "child_execution_id": frame.get("child_execution_id"),
            "child_flow_name": frame.get("child_flow_name"),
            "status": frame.get("status"),
        }
        payload.update(extra)
        return payload

    async def async_emit_to_sub_flow(
        self,
        frame_id: str,
        trigger_event: str,
        value: Any = None,
        *,
        trigger_type: Literal["event", "runtime_data", "flow_data"] = "event",
    ):
        normalized_frame_id = str(frame_id)
        signal_task: asyncio.Task[Any]
        async with self._sub_flow_control_lock:
            frame = self._get_sub_flow_frames().get(normalized_frame_id)
            if not isinstance(frame, dict):
                raise KeyError(
                    f"Can not emit to TriggerFlow sub flow frame '{ normalized_frame_id }' because it was not found."
                )
            child_execution = self._live_sub_flow_executions.get(normalized_frame_id)
            if frame.get("status") != "running" or child_execution is None:
                raise RuntimeError(
                    f"Can not emit to TriggerFlow sub flow frame '{ normalized_frame_id }' because it is not active "
                    f"(status={frame.get('status')!r})."
                )
            signal_task = asyncio.create_task(
                child_execution.async_emit(
                    str(trigger_event),
                    value,
                    trigger_type=trigger_type,
                    _source="parent_sub_flow_control",
                    _meta={
                        "parent_execution_id": self.id,
                        "sub_flow_frame_id": normalized_frame_id,
                    },
                )
            )
            self._live_sub_flow_signal_tasks.setdefault(normalized_frame_id, set()).add(signal_task)
        try:
            result = await signal_task
        except asyncio.CancelledError:
            current_frame = self._get_sub_flow_frames().get(normalized_frame_id)
            if isinstance(current_frame, dict) and current_frame.get("status") in {
                "cancel_requested",
                "cancelled",
            }:
                raise RuntimeError(
                    f"TriggerFlow sub flow frame '{ normalized_frame_id }' was cancelled while forwarding "
                    f"signal '{ trigger_event }'."
                ) from None
            raise
        finally:
            async with self._sub_flow_control_lock:
                signal_tasks = self._live_sub_flow_signal_tasks.get(normalized_frame_id)
                if signal_tasks is not None:
                    signal_tasks.discard(signal_task)
                    if not signal_tasks:
                        self._live_sub_flow_signal_tasks.pop(normalized_frame_id, None)
        await self._emit_runtime_event(
            "triggerflow.sub_flow_signal_forwarded",
            message=(
                f"TriggerFlow execution '{ self.id }' forwarded signal '{ trigger_event }' "
                f"to sub-flow frame '{ normalized_frame_id }'."
            ),
            payload=self._sub_flow_event_payload(
                frame,
                trigger_event=str(trigger_event),
                trigger_type=trigger_type,
                value=self._to_serializable_value(value),
            ),
        )
        return result

    async def async_cancel_sub_flow(
        self,
        frame_id: str,
        *,
        reason: str = "manual",
    ) -> bool:
        normalized_frame_id = str(frame_id)
        control_task: asyncio.Task[Any] | None = None
        signal_tasks: list[asyncio.Task[Any]] = []
        child_execution: TriggerFlowExecution[Any, Any, Any] | None = None
        requested_frame: dict[str, Any]
        finalized_here = False
        async with self._sub_flow_control_lock:
            stored_frame = self._get_sub_flow_frames().get(normalized_frame_id)
            if not isinstance(stored_frame, dict):
                raise KeyError(
                    f"Can not cancel TriggerFlow sub flow frame '{ normalized_frame_id }' because it was not found."
                )
            status = stored_frame.get("status")
            if status in {"cancel_requested", "cancelled", "completed", "failed", "interrupted"}:
                return False
            if status not in {"running", "waiting"}:
                return False
            control_task = self._live_sub_flow_tasks.get(normalized_frame_id)
            if control_task is asyncio.current_task():
                raise RuntimeError(
                    f"TriggerFlow sub flow frame '{ normalized_frame_id }' can not cancel itself from its control task."
                )
            requested_frame = copy.deepcopy(stored_frame)
            requested_frame["status"] = "cancel_requested"
            requested_frame["cancel_requested_at"] = time.time()
            requested_frame["cancel_reason"] = str(reason)
            self._set_sub_flow_frame(normalized_frame_id, requested_frame)
            child_execution = self._live_sub_flow_executions.get(normalized_frame_id)
            signal_tasks = list(self._live_sub_flow_signal_tasks.get(normalized_frame_id, set()))

        await self._emit_runtime_event(
            "triggerflow.sub_flow_cancel_requested",
            level="WARNING",
            message=(
                f"TriggerFlow execution '{ self.id }' requested cancellation of "
                f"sub-flow frame '{ normalized_frame_id }'."
            ),
            payload=self._sub_flow_event_payload(requested_frame, reason=str(reason)),
        )

        if child_execution is not None:
            child_execution._set_status(TRIGGER_FLOW_STATUS_CANCELLED)
            await child_execution._emit_runtime_event(
                "triggerflow.execution_cancelled",
                level="WARNING",
                message=(
                    f"TriggerFlow child execution '{ child_execution.id }' was cancelled by "
                    f"parent frame '{ normalized_frame_id }'."
                ),
                payload={
                    "reason": str(reason),
                    "parent_execution_id": self.id,
                    "sub_flow_frame_id": normalized_frame_id,
                },
            )

        tasks_to_cancel = [
            task
            for task in [*signal_tasks, control_task]
            if task is not None and not task.done()
        ]
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        async with self._sub_flow_control_lock:
            stored_frame = self._get_sub_flow_frames().get(normalized_frame_id)
            if not isinstance(stored_frame, dict):
                return True
            if stored_frame.get("status") == "cancel_requested":
                finalized_here = True
                cancelled_frame = copy.deepcopy(stored_frame)
                cancelled_frame["status"] = "cancelled"
                cancelled_frame["cancelled_at"] = time.time()
                if child_execution is not None:
                    cancelled_frame["child_saved_state"] = child_execution.save()
                projected_ids = set(cancelled_frame.get("projected_interrupts", {}))
                if projected_ids:
                    interrupts = self._get_interrupts().copy()
                    for interrupt_id in projected_ids:
                        interrupt = interrupts.get(interrupt_id)
                        if not isinstance(interrupt, dict) or interrupt.get("status") != "waiting":
                            continue
                        cancelled_interrupt = copy.deepcopy(interrupt)
                        cancelled_interrupt["status"] = "cancelled"
                        cancelled_interrupt["cancelled_at"] = cancelled_frame["cancelled_at"]
                        cancelled_interrupt["cancel_reason"] = str(reason)
                        interrupts[interrupt_id] = cancelled_interrupt
                    self._system_runtime_data.set("interrupts", interrupts)
                self._set_sub_flow_frame(normalized_frame_id, cancelled_frame)
            else:
                cancelled_frame = copy.deepcopy(stored_frame)

        self._refresh_waiting_status()
        if finalized_here and cancelled_frame.get("status") == "cancelled":
            await self._emit_runtime_event(
                "triggerflow.sub_flow_cancelled",
                level="WARNING",
                message=(
                    f"TriggerFlow execution '{ self.id }' cancelled sub-flow frame "
                    f"'{ normalized_frame_id }'."
                ),
                payload=self._sub_flow_event_payload(cancelled_frame, reason=str(reason)),
            )
        return True

    def _build_resume_context(self, interrupt_id: str, interrupt: dict[str, Any], value: Any):
        return self._interrupts.build_resume_context(interrupt_id, interrupt, value)

    def _set_runtime_resource(self, key: str, value: Any):
        normalized_key = str(key)
        self._runtime_resources.set(normalized_key, value)
        self._bind_durable_provider_resource(normalized_key, value)
        return self

    def _get_runtime_resource(self, key: str, default: Any = None):
        return self._runtime_resources.get(str(key), default)

    def require_runtime_resource(self, key: str) -> Any:
        key = str(key)
        if key not in self._runtime_resources:
            available = sorted(str(resource_key) for resource_key in self.get_runtime_resources().keys())
            raise KeyError(
                f"Execution { self.id } missing required runtime resource '{ key }'. "
                f"Available resources: { available }"
            )
        return self._runtime_resources.get(key)

    def _del_runtime_resource(self, key: str):
        self._runtime_resources.pop(str(key), None)
        return self

    def _update_runtime_resources(
        self,
        mapping: dict[str, Any] | None = None,
        **kwargs,
    ):
        if mapping is not None:
            for key, value in dict(mapping).items():
                self._set_runtime_resource(str(key), value)
        for key, value in kwargs.items():
            self._set_runtime_resource(str(key), value)
        return self

    def _recovery_provider_candidate(self) -> Any | None:
        durable_provider = self._get_runtime_resource("durable_provider")
        if durable_provider is not None:
            return durable_provider
        return self._get_runtime_resource("record_store")

    def _activate_recovery_durability(self, *, reason: str) -> Any | None:
        provider = self._recovery_provider_candidate()
        supports_snapshot = callable(getattr(provider, "put_snapshot", None))
        if provider is not None:
            if self._snapshot_store is None and supports_snapshot:
                self._bind_snapshot_store(provider)
        if self._snapshot_store is not None and self._system_runtime_data.get(
            "recovery_durability_activation", None, inherit=False
        ) is None:
            self._system_runtime_data.set(
                "recovery_durability_activation",
                {
                    "reason": str(reason),
                    "activated_at_state_version": self._state_version,
                },
            )
        return provider

    async def _async_activate_recovery_durability(
        self,
        *,
        reason: str,
        persist_snapshot: bool = False,
        step_id: str | None = None,
    ) -> Any | None:
        provider = self._activate_recovery_durability(reason=reason)
        if persist_snapshot and self._snapshot_store is None:
            raise RuntimeError(
                "TriggerFlow recovery snapshot persistence requires a snapshot store."
            )
        if persist_snapshot:
            await self.async_save(step_id=step_id)
        return provider

    def _bind_configured_durability_resources(self) -> None:
        durable_provider = self._get_runtime_resource("durable_provider")
        snapshot_store = self._get_runtime_resource("snapshot_store")
        runtime_event_store = self._get_runtime_resource("runtime_event_store")
        if snapshot_store is not None:
            self._bind_snapshot_store(snapshot_store)
        elif callable(getattr(durable_provider, "put_snapshot", None)):
            self._bind_snapshot_store(durable_provider)
        if runtime_event_store is not None:
            self._bind_runtime_event_store(runtime_event_store)

    def _bind_durable_provider_resource(self, key: str, value: Any):
        if key == "record_store":
            return
        if key in {"durable_provider", "snapshot_store", "runtime_event_store"}:
            self._bind_configured_durability_resources()

    def _clear_runtime_resources(self):
        self._runtime_resources.clear()
        return self

    def _declare_resource_requirement(
        self,
        key: str,
        *,
        kind: str = "runtime_resource",
        required: bool = True,
        metadata: dict[str, Any] | None = None,
        resolver: str | None = None,
        provider_kind: str | None = None,
        secret_ref: str | None = None,
        config_ref: str | None = None,
        resolver_version: str | None = None,
        resolver_fingerprint: str | None = None,
        health: str | None = None,
        fail_policy: str | None = None,
    ):
        requirement = {
            "kind": str(kind),
            "key": str(key),
            "required": bool(required),
            "source": "execution",
            "metadata": {"scope": "execution", **dict(metadata or {})},
        }
        for field, value in (
            ("resolver", resolver),
            ("provider_kind", provider_kind),
            ("secret_ref", secret_ref),
            ("config_ref", config_ref),
            ("resolver_version", resolver_version),
            ("resolver_fingerprint", resolver_fingerprint),
            ("health", health),
            ("fail_policy", fail_policy),
        ):
            if value is not None:
                requirement[field] = str(value)
        self._resource_requirements = [
            item
            for item in self._resource_requirements
            if not (
                item.get("kind") == requirement["kind"]
                and item.get("key") == requirement["key"]
                and item.get("source") == requirement["source"]
            )
        ]
        self._resource_requirements.append(requirement)
        return self

    def get_resource_requirements(self):
        return copy.deepcopy(self._trigger_flow.get_resource_requirements()) + copy.deepcopy(
            self._resource_requirements
        )

    def _record_compaction_segment(
        self,
        segment_id: str,
        *,
        sequence_from: int,
        sequence_to: int,
        summary: str | None = None,
        artifact_refs: list[Any] | None = None,
        retained_anchor_ids: list[str] | None = None,
        reducer: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        if sequence_from < 0 or sequence_to < 0:
            raise ValueError("compaction segment sequence bounds must be non-negative.")
        if sequence_to < sequence_from:
            raise ValueError("compaction segment sequence_to must be greater than or equal to sequence_from.")
        segment = {
            "segment_id": str(segment_id),
            "sequence_from": int(sequence_from),
            "sequence_to": int(sequence_to),
            "summary": summary,
            "artifact_refs": self._to_serializable_value(artifact_refs or []),
            "retained_anchor_ids": [str(anchor_id) for anchor_id in retained_anchor_ids or []],
            "reducer": str(reducer) if reducer is not None else None,
            "metadata": self._to_serializable_value(dict(metadata or {})),
        }
        self._compaction_segments = [
            item
            for item in self._compaction_segments
            if item.get("segment_id") != segment["segment_id"]
        ]
        self._compaction_segments.append(segment)
        self._bump_state_version()
        return self

    def _record_retained_lineage_anchor(
        self,
        anchor_id: str,
        *,
        anchor_type: str = "compaction",
        sequence: int | None = None,
        event_id: str | None = None,
        parent_signal_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ):
        if sequence is not None and sequence < 0:
            raise ValueError("lineage anchor sequence must be non-negative or None.")
        anchor = {
            "anchor_id": str(anchor_id),
            "anchor_type": str(anchor_type),
            "sequence": int(sequence) if sequence is not None else None,
            "event_id": str(event_id) if event_id is not None else None,
            "parent_signal_id": str(parent_signal_id) if parent_signal_id is not None else None,
            "metadata": self._to_serializable_value(dict(metadata or {})),
        }
        anchor["fingerprint"] = fingerprint or self._lineage_anchor_fingerprint(anchor)
        self._retained_lineage_anchors = [
            item
            for item in self._retained_lineage_anchors
            if item.get("anchor_id") != anchor["anchor_id"]
        ]
        self._retained_lineage_anchors.append(anchor)
        self._bump_state_version()
        return self

    def _record_snapshot_artifact_ref(
        self,
        artifact_ref: Any,
        *,
        kind: str = "payload",
        required: bool = True,
        status: str = "available",
        metadata: dict[str, Any] | None = None,
    ):
        ref = {
            "kind": str(kind),
            "required": bool(required),
            "status": str(status),
            "ref": self._to_serializable_value(artifact_ref),
            "metadata": self._to_serializable_value(dict(metadata or {})),
        }
        self._snapshot_artifact_refs.append(ref)
        self._bump_state_version()
        return self

    def _set_compaction_policy(
        self,
        *,
        min_runtime_events: int | None = 1,
        reducer: Any | None = None,
        reducer_ref: str | None = None,
        artifact_kind: str = "snapshot_payload",
        load_read_limit: int | None = None,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ):
        if min_runtime_events is not None and min_runtime_events < 0:
            raise ValueError("compaction min_runtime_events must be non-negative or None.")
        if load_read_limit is not None and load_read_limit < 0:
            raise ValueError("compaction load_read_limit must be non-negative or None.")
        if reducer is not None and reducer_ref is not None:
            raise ValueError("Set either reducer or reducer_ref, not both.")
        resolved_reducer = reducer_ref if reducer_ref is not None else reducer
        self._compaction_policy = {
            "enabled": bool(enabled),
            "min_runtime_events": int(min_runtime_events) if min_runtime_events is not None else None,
            "reducer": resolved_reducer,
            "artifact_kind": str(artifact_kind),
            "load_read_limit": int(load_read_limit) if load_read_limit is not None else None,
            "metadata": self._to_serializable_value(dict(metadata or {})),
        }
        self._bump_state_version()
        return self

    def _set_snapshot_projection_policy(
        self,
        *,
        terminal_value_mode: Literal["full", "digest"] = "digest",
        min_value_bytes: int = 4096,
        project_terminal_signal_attempts: bool = True,
        enabled: bool = True,
    ) -> "TriggerFlowExecution[InputT, StreamT, ResultT]":
        if terminal_value_mode not in {"full", "digest"}:
            raise ValueError(
                "snapshot projection terminal_value_mode must be one of: "
                "'full', 'digest'."
            )
        if not isinstance(min_value_bytes, int) or isinstance(min_value_bytes, bool) or min_value_bytes < 0:
            raise ValueError("snapshot projection min_value_bytes must be a non-negative integer.")
        self._snapshot_projection_policy = {
            "enabled": bool(enabled),
            "terminal_value_mode": terminal_value_mode,
            "min_value_bytes": min_value_bytes,
            "project_terminal_signal_attempts": bool(project_terminal_signal_attempts),
        }
        self._bump_state_version()
        return self

    def _serializable_snapshot_projection_policy(self):
        return self._to_serializable_value(self._snapshot_projection_policy)

    def _set_snapshot_retention_policy(
        self,
        *,
        keep_last: int | None,
    ) -> "TriggerFlowExecution[InputT, StreamT, ResultT]":
        self._snapshot_retention_policy = self._normalize_snapshot_retention_policy({"keep_last": keep_last})
        self._bump_state_version()
        return self

    def _normalize_snapshot_retention_policy(
        self,
        policy: Any,
    ) -> SnapshotRetentionPolicy:
        if not isinstance(policy, dict):
            raise TypeError("snapshot retention policy must be a dictionary.")
        unknown = sorted(set(policy) - {"keep_last"})
        if unknown:
            raise ValueError("snapshot retention policy contains unsupported keys: " f"{', '.join(unknown)}.")
        keep_last = policy.get("keep_last")
        if keep_last is not None:
            if isinstance(keep_last, bool) or not isinstance(keep_last, int):
                raise TypeError("snapshot retention keep_last must be a positive integer or None.")
            if keep_last < 1:
                raise ValueError("snapshot retention keep_last must be greater than or equal to 1.")
        return {"keep_last": keep_last}

    def _set_load_read_limit(self, limit: int | None):
        if limit is not None and limit < 0:
            raise ValueError("load read limit must be non-negative or None.")
        if limit is None:
            self._load_policy.pop("runtime_event_read_limit", None)
        else:
            self._load_policy["runtime_event_read_limit"] = int(limit)
        self._bump_state_version()
        return self

    def _lineage_anchor_fingerprint(self, anchor: dict[str, Any]):
        material = {
            key: value
            for key, value in anchor.items()
            if key != "fingerprint"
        }
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _serializable_compaction_policy(self):
        policy = dict(self._compaction_policy)
        reducer = policy.get("reducer")
        if callable(reducer):
            reducer_ref = self._callable_ref(reducer)
            if reducer_ref is not None:
                policy["reducer"] = reducer_ref
            else:
                policy.pop("reducer", None)
                policy["reducer_kind"] = "callable"
        return self._to_serializable_value(policy)

    def _callable_ref(self, callback: Any):
        module = getattr(callback, "__module__", None)
        qualname = getattr(callback, "__qualname__", None)
        if not module or not qualname or "<locals>" in str(qualname):
            return None
        return f"{ module }:{ qualname }"

    def _resolve_import_ref(self, ref: str):
        if ":" not in ref:
            raise ValueError(f"TriggerFlow import reference must use 'module:attribute': { ref }")
        module_name, attribute_path = ref.split(":", 1)
        value = importlib.import_module(module_name)
        for part in attribute_path.split("."):
            value = getattr(value, part)
        return value

    def _compaction_reducer_ref(self):
        reducer = self._compaction_policy.get("reducer")
        if isinstance(reducer, str):
            return reducer
        if callable(reducer):
            return self._callable_ref(reducer)
        return None

    def _resolve_compaction_reducer(self):
        reducer = self._compaction_policy.get("reducer")
        if isinstance(reducer, str):
            reducer = self._resolve_import_ref(reducer)
        if reducer is not None and not callable(reducer):
            raise TypeError("TriggerFlow compaction reducer must be callable or an import reference string.")
        return reducer

    async def _call_compaction_reducer(
        self,
        reducer: Any,
        *,
        records: list[dict[str, Any]],
        sequence_from: int,
        sequence_to: int,
        run_id: str,
        snapshot_store: Any,
        runtime_event_store: Any,
    ) -> dict[str, Any]:
        context = {
            "execution": self,
            "records": records,
            "events": records,
            "sequence_from": sequence_from,
            "sequence_to": sequence_to,
            "run_id": run_id,
            "snapshot_store": snapshot_store,
            "runtime_event_store": runtime_event_store,
            "policy": self._serializable_compaction_policy(),
        }
        kwargs = {"context": context, **context}
        try:
            signature = inspect.signature(reducer)
            parameters = signature.parameters
            if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
                result = reducer(**kwargs)
            else:
                accepted_kwargs = {
                    key: value
                    for key, value in kwargs.items()
                    if key in parameters
                }
                result = reducer(**accepted_kwargs) if accepted_kwargs else reducer(context)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            result = reducer(context)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return {}
        if not isinstance(result, dict):
            return {"summary": str(result)}
        return cast(dict[str, Any], result)

    def _runtime_event_record_sequence(self, record: dict[str, Any]):
        try:
            return int(record["sequence"])
        except (KeyError, TypeError, ValueError):
            return None

    def _compaction_policy_min_events(self):
        min_events = self._compaction_policy.get("min_runtime_events")
        if min_events is None:
            return 1
        try:
            return int(min_events)
        except (TypeError, ValueError):
            return 1

    def _latest_compacted_sequence(self):
        latest = 0
        for segment in self._compaction_segments:
            try:
                latest = max(latest, int(segment.get("sequence_to", 0)))
            except (TypeError, ValueError):
                continue
        return latest

    def _compaction_provider(self, snapshot_store: Any, runtime_event_store: Any):
        if snapshot_store is not None and hasattr(snapshot_store, "put_artifact_ref"):
            return snapshot_store
        if runtime_event_store is not None and hasattr(runtime_event_store, "put_artifact_ref"):
            return runtime_event_store
        return snapshot_store

    async def _maybe_apply_compaction_policy(
        self,
        *,
        snapshot_store: Any,
        run_id: str,
    ):
        if not self._compaction_policy.get("enabled"):
            return False
        runtime_event_store = self._runtime_event_store or snapshot_store
        if runtime_event_store is None or not hasattr(runtime_event_store, "query_runtime_events"):
            raise TypeError(
                "TriggerFlow compaction policy requires a runtime_event_store with query_runtime_events(...)."
            )
        query_runtime_events = runtime_event_store.query_runtime_events
        sequence_from = self._latest_compacted_sequence() + 1
        query_kwargs = self._filter_callable_kwargs(
            query_runtime_events,
            {"sequence_from": sequence_from},
        )
        records = await query_runtime_events(self.id, **query_kwargs)
        if not isinstance(records, list):
            records = list(records or [])
        records = [dict(record) for record in records if isinstance(record, dict)]
        min_events = self._compaction_policy_min_events()
        if len(records) < min_events:
            return False
        sequences = [
            sequence
            for sequence in (self._runtime_event_record_sequence(record) for record in records)
            if sequence is not None
        ]
        if not sequences:
            return False
        segment_from = min(sequences)
        segment_to = max(sequences)
        reducer = self._resolve_compaction_reducer()
        reducer_result: dict[str, Any] = (
            await self._call_compaction_reducer(
                reducer,
                records=records,
                sequence_from=segment_from,
                sequence_to=segment_to,
                run_id=run_id,
                snapshot_store=snapshot_store,
                runtime_event_store=runtime_event_store,
            )
            if reducer is not None
            else {}
        )
        provider = self._compaction_provider(snapshot_store, runtime_event_store)
        policy_metadata = self._compaction_policy.get("metadata", {})
        if not isinstance(policy_metadata, dict):
            policy_metadata = {}
        reducer_metadata = reducer_result.get("metadata", {})
        if not isinstance(reducer_metadata, dict):
            reducer_metadata = {}
        metadata = {
            "generated_by": "triggerflow.compaction_policy",
            **policy_metadata,
            **reducer_metadata,
        }
        artifact_refs = list(reducer_result.get("artifact_refs", []) or [])
        if "artifact" in reducer_result:
            if provider is None or not hasattr(provider, "put_artifact_ref"):
                raise TypeError(
                    "TriggerFlow compaction reducer returned artifact content, but provider has no put_artifact_ref(...)."
                )
            artifact_metadata = {
                "kind": reducer_result.get("artifact_kind", self._compaction_policy.get("artifact_kind")),
                "summary": reducer_result.get("summary", f"Compacted TriggerFlow events { segment_from }-{ segment_to }"),
                "scope": {"execution_id": self.id, "sequence_from": segment_from, "sequence_to": segment_to},
                **metadata,
            }
            artifact_refs.append(await provider.put_artifact_ref(self.id, reducer_result["artifact"], metadata=artifact_metadata))

        retained_anchors = reducer_result.get("retained_lineage_anchors")
        if retained_anchors is None:
            first_record = records[0]
            retained_anchors = [
                {
                    "anchor_id": f"auto-compaction:{ segment_from }:{ segment_to }",
                    "anchor_type": "compaction",
                    "sequence": segment_from,
                    "event_id": first_record.get("event_id"),
                    "parent_signal_id": first_record.get("parent_signal_id"),
                    "metadata": metadata,
                }
            ]
        if isinstance(retained_anchors, dict):
            retained_anchors = [retained_anchors]
        anchor_ids: list[str] = []
        for anchor in retained_anchors or []:
            if not isinstance(anchor, dict):
                continue
            anchor_id = str(anchor.get("anchor_id") or f"auto-compaction:{ segment_from }:{ segment_to }")
            anchor_result_metadata = anchor.get("metadata", {})
            if not isinstance(anchor_result_metadata, dict):
                anchor_result_metadata = {}
            anchor_metadata = {
                **metadata,
                **anchor_result_metadata,
            }
            self._record_retained_lineage_anchor(
                anchor_id,
                anchor_type=str(anchor.get("anchor_type", "compaction")),
                sequence=anchor.get("sequence", segment_from),
                event_id=anchor.get("event_id"),
                parent_signal_id=anchor.get("parent_signal_id"),
                metadata=anchor_metadata,
                fingerprint=anchor.get("fingerprint"),
            )
            anchor_ids.append(anchor_id)

        for artifact_ref in artifact_refs:
            self._record_snapshot_artifact_ref(
                artifact_ref,
                kind=str(reducer_result.get("artifact_kind", self._compaction_policy.get("artifact_kind", "snapshot_payload"))),
                metadata=metadata,
            )

        segment_value = reducer_result.get("segment", {})
        segment = dict(segment_value) if isinstance(segment_value, dict) else {}
        segment_metadata = segment.get("metadata", {})
        if not isinstance(segment_metadata, dict):
            segment_metadata = {}
        self._record_compaction_segment(
            str(segment.get("segment_id") or f"auto-compaction:{ segment_from }:{ segment_to }"),
            sequence_from=int(segment.get("sequence_from", segment_from)),
            sequence_to=int(segment.get("sequence_to", segment_to)),
            summary=segment.get("summary", reducer_result.get("summary")),
            artifact_refs=artifact_refs,
            retained_anchor_ids=list(segment.get("retained_anchor_ids", anchor_ids) or anchor_ids),
            reducer=segment.get("reducer", self._compaction_reducer_ref()),
            metadata={**metadata, **segment_metadata},
        )
        load_read_limit = reducer_result.get("load_read_limit", self._compaction_policy.get("load_read_limit"))
        if load_read_limit is not None:
            self._set_load_read_limit(int(load_read_limit))
        return True

    def get_runtime_resources(self):
        resources = self._runtime_resources.get(None, {}, inherit=True)
        return resources if isinstance(resources, dict) else {}

    def _serialize_signal(self, signal: TriggerFlowSignal | dict[str, Any] | None):
        if signal is None:
            return None
        if isinstance(signal, TriggerFlowSignal):
            return self._to_serializable_value(signal.to_state_dict())
        return self._to_serializable_value(signal)

    def _restore_signal(self, signal_state: dict[str, Any] | None):
        if not isinstance(signal_state, dict):
            return None
        try:
            return TriggerFlowSignal(
                id=str(signal_state.get("id")),
                trigger_event=str(signal_state.get("trigger_event")),
                trigger_type=signal_state.get("trigger_type", "event"),
                value=signal_state.get("value"),
                layer_marks=list(signal_state.get("layer_marks", [])),
                source=str(signal_state.get("source", "runtime")),
                meta=dict(signal_state.get("meta", {})),
            )
        except Exception:
            return None

    def _build_signal(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: TriggerFlowSignalType = "event",
        source: str = "runtime",
        meta: dict[str, Any] | None = None,
    ):
        return TriggerFlowSignal.create(
            trigger_event=trigger_event,
            trigger_type=trigger_type,
            value=value,
            layer_marks=_layer_marks,
            source=source,
            meta=meta,
        )

    def _remember_signal(self, signal: TriggerFlowSignal):
        self._system_runtime_data.set("last_signal", signal.to_state_dict())

    def get_last_signal(self):
        return self._restore_signal(self._system_runtime_data.get("last_signal", None, inherit=False))

    def get_contract_metadata(self) -> TriggerFlowContractMetadata:
        return self._trigger_flow.get_contract_metadata()

    def get_contract(self) -> TriggerFlowContractSpec[InputT, StreamT, ResultT]:
        return self._trigger_flow.get_contract()

    def save(
        self,
        path: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
        require_idle: bool = False,
    ):
        self._activate_recovery_durability(reason="save")
        return self._persistence.save(
            path,
            encoding=encoding,
            require_idle=require_idle,
        )

    def load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: "list[ExecutionResourceRequirement] | None" = None,
        validate_resources: bool = False,
    ):
        if runtime_resources:
            self._update_runtime_resources(runtime_resources)
        self._bind_configured_durability_resources()
        self._activate_recovery_durability(reason="load")
        return self._persistence.load(
            state,
            encoding=encoding,
            runtime_resources=runtime_resources,
            execution_resources=execution_resources,
            validate_resources=validate_resources,
        )

    def _bind_snapshot_store(self, snapshot_store: "TriggerFlowExecutionSnapshotStore | None"):
        if snapshot_store is not None and not callable(
            getattr(snapshot_store, "put_snapshot", None)
        ):
            raise TypeError(
                "TriggerFlow snapshot_store must expose async put_snapshot(run_id, state, step_id=...)."
            )
        self._snapshot_store = snapshot_store
        return self

    def _bind_runtime_event_store(self, runtime_event_store: "RuntimeEventStore | None"):
        if runtime_event_store is not None and not callable(
            getattr(runtime_event_store, "append_runtime_event", None)
        ):
            raise TypeError(
                "TriggerFlow runtime_event_store must expose async append_runtime_event(execution_id, event, ...)."
            )
        self._runtime_event_store = runtime_event_store
        return self

    def _provider_features(self, provider: Any):
        capabilities_getter = getattr(provider, "capabilities", None)
        if not callable(capabilities_getter):
            return {}
        capabilities = capabilities_getter()
        if not isinstance(capabilities, dict):
            return {}
        raw_features = capabilities.get("features", capabilities)
        if not isinstance(raw_features, dict):
            return {}
        return {str(key): bool(value) for key, value in raw_features.items()}

    def _require_provider_capabilities(
        self,
        provider: Any,
        *,
        required: tuple[str, ...],
        usage: str,
    ):
        features = self._provider_features(provider)
        missing = [name for name in required if features.get(name) is not True]
        if missing:
            raise RuntimeError(
                f"TriggerFlow durable provider can not be used for { usage }; "
                f"missing capabilities: { ', '.join(missing) }. "
                "The provider must report them from capabilities()['features']."
            )
        return features

    def _require_provider_methods(
        self,
        provider: Any,
        *,
        required: tuple[str, ...],
        usage: str,
    ):
        missing = [name for name in required if not callable(getattr(provider, name, None))]
        if missing:
            raise RuntimeError(
                f"TriggerFlow durable provider can not be used for { usage }; "
                f"missing methods: { ', '.join(missing) }."
            )

    def _require_distributed_durable_provider(self, snapshot_store: Any):
        self._require_provider_capabilities(
            snapshot_store,
            required=DISTRIBUTED_SNAPSHOT_PROVIDER_CAPABILITIES,
            usage="distributed snapshot recovery",
        )
        self._require_provider_methods(
            snapshot_store,
            required=DISTRIBUTED_SNAPSHOT_PROVIDER_METHODS,
            usage="distributed snapshot recovery",
        )
        if self._runtime_event_store is None:
            raise RuntimeError(
                "TriggerFlow distributed recovery requires a runtime event store. "
                "Pass runtime_resources={'runtime_event_store': store} or set_runtime_resource('runtime_event_store', store)."
            )
        self._require_provider_capabilities(
            self._runtime_event_store,
            required=DISTRIBUTED_RUNTIME_EVENT_PROVIDER_CAPABILITIES,
            usage="distributed runtime event recovery",
        )

    def inspect_load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: "list[ExecutionResourceRequirement] | None" = None,
    ):
        return self._persistence.inspect_load(
            state,
            encoding=encoding,
            runtime_resources=runtime_resources,
            execution_resources=execution_resources,
        )

    def inspect_runtime_event_records(self, records: list[dict[str, Any]]):
        return diagnose_runtime_event_records(records)

    def project_runtime_event_record(self, record: dict[str, Any]):
        return project_runtime_event_record(record)

    async def async_load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: "list[ExecutionResourceRequirement] | None" = None,
        require_resources: bool = True,
        restore_execution_resources: bool = True,
    ):
        resolver_report = await self._persistence.async_resolve_load_resources(
            state,
            encoding=encoding,
            runtime_resources=runtime_resources,
            execution_resources=execution_resources,
            require_resources=require_resources,
        )
        resolved_runtime_resources = dict(runtime_resources or {})
        resolved_runtime_resources.update(resolver_report.get("runtime_resources", {}))
        if resolved_runtime_resources:
            self._update_runtime_resources(resolved_runtime_resources)
        self._bind_configured_durability_resources()
        self._activate_recovery_durability(reason="async_load")
        self.load(
            state,
            encoding=encoding,
            runtime_resources=resolved_runtime_resources or None,
            execution_resources=execution_resources,
            validate_resources=False,
        )
        if restore_execution_resources:
            await self._ensure_execution_resources()
        load = self.inspect_load(
            self.save(),
            runtime_resources=None,
            execution_resources=None,
        )
        load["diagnostics"] = [
            *load.get("diagnostics", []),
            *resolver_report.get("diagnostics", []),
        ]
        load["resolved_resource_keys"] = sorted(
            {
                *load.get("resolved_resource_keys", []),
                *resolver_report.get("resolved_resource_keys", []),
            }
        )
        load["unresolved_resource_keys"] = sorted(
            {
                *load.get("unresolved_resource_keys", []),
                *resolver_report.get("unresolved_resource_keys", []),
            }
        )
        if require_resources and not load.get("ready", False):
            raise RuntimeError(
                f"Can not load TriggerFlow execution { self.id }; "
                f"missing resources: { load.get('missing_resource_keys', []) }."
            )
        return load

    async def async_save(
        self,
        snapshot_store: Any | None = None,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        expected_state_version: int | None = None,
        require_idle: bool = False,
        require_distributed_provider: bool = False,
    ):
        await self._async_activate_recovery_durability(reason="async_save")
        resolved_snapshot_store = snapshot_store if snapshot_store is not None else self._snapshot_store
        if resolved_snapshot_store is None or not callable(getattr(resolved_snapshot_store, "put_snapshot", None)):
            raise TypeError(
                "TriggerFlow snapshot_store must expose async put_snapshot(run_id, state, step_id=...). "
                "Pass snapshot_store to async_save(...) or set runtime resource 'snapshot_store'."
            )
        resolved_snapshot_store = cast(Any, resolved_snapshot_store)
        if require_distributed_provider:
            self._require_distributed_durable_provider(resolved_snapshot_store)
        resolved_run_id = run_id or self.run_context.run_id or self.id
        await self._maybe_apply_compaction_policy(
            snapshot_store=resolved_snapshot_store,
            run_id=resolved_run_id,
        )
        state = self.save(require_idle=require_idle)
        resolved_step_id = step_id or f"state:{ self._state_version }"
        put_snapshot = resolved_snapshot_store.put_snapshot
        put_kwargs = self._filter_callable_kwargs(
            put_snapshot,
            {
                "step_id": resolved_step_id,
                "expected_state_version": expected_state_version,
                **(
                    {"retention": self._snapshot_retention_policy}
                    if self._snapshot_retention_policy is not None
                    else {}
                ),
            },
        )
        if self._snapshot_retention_policy is not None and "retention" not in put_kwargs:
            raise TypeError(
                "TriggerFlow snapshot_store must accept retention=... when an "
                "execution snapshot retention override is configured."
            )
        return await put_snapshot(
            resolved_run_id,
            state,
            **put_kwargs,
        )

    async def async_prune_recovery_snapshots(
        self,
        *,
        keep_last: int = 1,
    ) -> SnapshotPruneResult:
        if isinstance(keep_last, bool) or not isinstance(keep_last, int):
            raise TypeError("TriggerFlow recovery snapshot keep_last must be a positive integer.")
        if keep_last < 1:
            raise ValueError("TriggerFlow recovery snapshot keep_last must be greater than or equal to 1.")
        if not self.is_idle():
            raise RuntimeError(f"Can not prune recovery snapshots for active TriggerFlow execution {self.id}.")
        await self._async_activate_recovery_durability(reason="async_prune_recovery_snapshots")
        snapshot_store = self._snapshot_store
        prune_snapshots = getattr(snapshot_store, "prune_snapshots", None)
        if snapshot_store is None or not callable(prune_snapshots):
            raise TypeError(
                "TriggerFlow snapshot_store must expose "
                "async prune_snapshots(run_id, keep_last=...)."
            )
        return await cast(
            Any,
            prune_snapshots,
        )(
            self.run_id,
            keep_last=keep_last,
        )

    async def _async_read_runtime_events_for_load(
        self,
        runtime_event_store: Any | None = None,
        *,
        run_id: str | None = None,
        sequence_from: int | None = None,
        limit: int | None = None,
    ):
        if limit is None:
            limit = self._load_policy.get("runtime_event_read_limit")
        if limit is not None and limit < 0:
            raise ValueError("runtime event load read limit must be non-negative or None.")
        resolved_runtime_event_store = runtime_event_store if runtime_event_store is not None else self._runtime_event_store
        if resolved_runtime_event_store is None or not hasattr(resolved_runtime_event_store, "query_runtime_events"):
            raise TypeError(
                "TriggerFlow runtime_event_store must expose async query_runtime_events(run_id, ...)."
            )
        query_runtime_events = resolved_runtime_event_store.query_runtime_events
        query_kwargs = self._filter_callable_kwargs(
            query_runtime_events,
            {
                "sequence_from": sequence_from,
                "limit": limit,
            },
        )
        return await query_runtime_events(run_id or self.id, **query_kwargs)

    def claim_lease(
        self,
        owner_id: str,
        *,
        lease_ttl: float | None = None,
        now: float | None = None,
    ):
        if not owner_id:
            raise ValueError("TriggerFlow execution lease owner_id must be non-empty.")
        timestamp = time.time() if now is None else float(now)
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        self._owner_id = str(owner_id)
        self._lease_ttl = ttl
        self._heartbeat_at = timestamp
        self._lease_until = timestamp + ttl if ttl is not None else None
        self._bump_state_version()
        return self

    def heartbeat_lease(
        self,
        *,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
        now: float | None = None,
    ):
        if owner_id is not None and self._owner_id is not None and str(owner_id) != str(self._owner_id):
            raise RuntimeError(
                f"TriggerFlow execution { self.id } lease is owned by '{ self._owner_id }', "
                f"not '{ owner_id }'."
            )
        timestamp = time.time() if now is None else float(now)
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        self._lease_ttl = ttl
        self._heartbeat_at = timestamp
        self._lease_until = timestamp + ttl if ttl is not None else None
        self._bump_state_version()
        return self

    def get_lease(self):
        return {
            "owner_id": self._owner_id,
            "lease_ttl": self._lease_ttl,
            "lease_until": self._lease_until,
            "heartbeat_at": self._heartbeat_at,
        }

    # Set Concurrency
    def set_concurrency(self, concurrency):
        self._concurrency_semaphore = asyncio.Semaphore(concurrency) if concurrency and concurrency > 0 else None
        return self

    async def _async_dispatch_signal_yielding_current_permit(self, signal: TriggerFlowSignal):
        if self._concurrency_semaphore is None or not self._concurrency_permit_held.get():
            return await self._async_dispatch_signal(signal)
        token = self._concurrency_permit_held.set(False)
        self._concurrency_semaphore.release()
        try:
            return await self._async_dispatch_signal(signal)
        finally:
            await self._concurrency_semaphore.acquire()
            self._concurrency_permit_held.reset(token)

    def _without_current_concurrency_permit_context(self):
        if not self._concurrency_permit_held.get():
            return None
        return self._concurrency_permit_held.set(False)

    # Emit Event
    async def async_emit(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: Literal["event", "runtime_data", "flow_data"] = "event",
        _source: str = "runtime",
        _meta: dict[str, Any] | None = None,
    ):
        signal = self._build_signal(
            trigger_event,
            value,
            _layer_marks,
            trigger_type=trigger_type,
            source=_source,
            meta=_meta,
        )
        return await self._async_dispatch_signal_yielding_current_permit(signal)

    async def async_emit_nowait(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: Literal["event", "runtime_data", "flow_data"] = "event",
        _source: str = "runtime",
        _meta: dict[str, Any] | None = None,
    ):
        signal = self._build_signal(
            trigger_event,
            value,
            _layer_marks,
            trigger_type=trigger_type,
            source=_source,
            meta=_meta,
        )
        if not self._accepts_signal_in_current_lifecycle(signal):
            await self._reject_signal(signal)
            return None
        self._signal_net.accept_signal(signal)
        self._mark_activity()
        token = self._without_current_concurrency_permit_context()
        try:
            return self._create_managed_task(
                self._async_dispatch_signal(signal),
                origin=f"emit_nowait:{ trigger_type }:{ trigger_event }",
            )
        finally:
            if token is not None:
                self._concurrency_permit_held.reset(token)

    def _emit_nowait(
        self,
        trigger_event: str,
        value: Any = None,
        _layer_marks: list[str] | None = None,
        *,
        trigger_type: Literal["event", "runtime_data", "flow_data"] = "event",
        _source: str = "runtime",
        _meta: dict[str, Any] | None = None,
    ):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return default_stage_call_bridge.submit(
                self.async_emit,
                trigger_event,
                value,
                _layer_marks,
                trigger_type=trigger_type,
                _source=_source,
                _meta=_meta,
            )
        signal = self._build_signal(
            trigger_event,
            value,
            _layer_marks,
            trigger_type=trigger_type,
            source=_source,
            meta=_meta,
        )
        if not self._accepts_signal_in_current_lifecycle(signal):
            loop.create_task(self._reject_signal(signal))
            return None
        self._signal_net.accept_signal(signal)
        self._mark_activity()
        token = self._without_current_concurrency_permit_context()
        try:
            return self._create_managed_task(
                self._async_dispatch_signal(signal),
                origin=f"emit_nowait:{ trigger_type }:{ trigger_event }",
            )
        finally:
            if token is not None:
                self._concurrency_permit_held.reset(token)

    async def _resume_interrupts_for_signal(self, signal: TriggerFlowSignal):
        return await self._interrupts.async_resume_for_signal(signal)

    async def _async_dispatch_signal(self, signal: TriggerFlowSignal):
        self._active_runtime_operation_count += 1
        self._mark_activity()
        try:
            result = await self._async_dispatch_signal_inner(signal)
            self._signal_net.mark_completed(signal)
            return result
        except asyncio.CancelledError:
            self._signal_net.mark_interrupted(signal, reason="dispatch cancelled")
            raise
        except BaseException as error:
            self._signal_net.mark_failed(signal, error)
            raise
        finally:
            self._active_runtime_operation_count -= 1
            self._mark_activity()

    async def _async_dispatch_signal_inner(self, signal: TriggerFlowSignal):
        signal_preaccepted = self._signal_net.is_accepted(signal.id)
        if not self._accepts_signal_in_current_lifecycle(signal, preaccepted=signal_preaccepted):
            await self._reject_signal(signal)
            return None
        self._signal_net.mark_running(signal)

        self._mark_activity()
        await self._resume_interrupts_for_signal(signal)
        self._remember_signal(signal)
        await self._emit_runtime_event(
            "triggerflow.signal",
            level="DEBUG",
            message=f"Dispatch signal '{ signal.trigger_event }'.",
            payload=signal.to_debug_dict(),
        )
        tasks = []
        signal_handlers = list(self._signal_net.iter_handlers(signal, self._handlers))

        if signal_handlers:
            for handler_id, handler in signal_handlers:
                operator = self._get_handler_operator(handler_id)
                chunk_run_context = self._create_chunk_run_context(operator, signal) if operator is not None else None
                await self._emit_runtime_event(
                    "triggerflow.handler_dispatch",
                    level="DEBUG",
                    message=f"Dispatch handler '{ handler_id }' for signal '{ signal.trigger_event }'.",
                    payload={
                        "event": signal.trigger_event,
                        "type": signal.trigger_type,
                        "handler": handler_id,
                        "signal_id": signal.id,
                        "node_id": operator.get("id") if operator is not None else None,
                    },
                )
                await self._async_apply_auto_interventions(operator, signal)

                async def run_handler(
                    handler_func,
                    handler_data: TriggerFlowRuntimeData[Any, Any, Any],
                    *,
                    handler_id: str,
                    bound_operator: dict[str, Any] | None,
                    bound_chunk_run_context: RunContext | None,
                ):
                    self._active_handler_count += 1

                    async def execute_handler():
                        if bound_operator is not None and bound_chunk_run_context is not None:
                            await self._emit_chunk_runtime_event(
                                "chunk.started",
                                bound_chunk_run_context,
                                operator=bound_operator,
                                signal=signal,
                                message=(
                                    f"Chunk '{ bound_chunk_run_context.meta.get('chunk_name', bound_chunk_run_context.run_id) }' started."
                                ),
                                payload={
                                    "status": "running",
                                    "input": self._serialize_runtime_value(signal.value),
                                    "signal_source": signal.source,
                                    "signal_meta": self._serialize_runtime_value(signal.meta),
                                },
                            )
                        try:
                            with bind_runtime_context(
                                parent_run_context=(
                                    bound_chunk_run_context
                                    if bound_chunk_run_context is not None
                                    else self.run_context
                                ),
                                chunk_run_context=bound_chunk_run_context,
                            ):
                                return await handler_func(handler_data)
                        except BaseException as error:
                            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                                raise
                            if isinstance(error, asyncio.CancelledError):
                                raise
                            if bound_operator is not None and bound_chunk_run_context is not None:
                                await self._emit_chunk_runtime_event(
                                    "chunk.failed",
                                    bound_chunk_run_context,
                                    operator=bound_operator,
                                    signal=signal,
                                    level="ERROR",
                                    message=(
                                        f"Chunk '{ bound_chunk_run_context.meta.get('chunk_name', bound_chunk_run_context.run_id) }' failed."
                                    ),
                                    payload={
                                        "status": "failed",
                                        "input": self._serialize_runtime_value(signal.value),
                                        "signal_source": signal.source,
                                        "signal_meta": self._serialize_runtime_value(signal.meta),
                                    },
                                    error=error,
                                )
                            raise

                    try:
                        if self._concurrency_semaphore is None:
                            result = await execute_handler()
                        else:
                            await self._concurrency_semaphore.acquire()
                            token = self._concurrency_permit_held.set(True)
                            try:
                                result = await execute_handler()
                            finally:
                                self._concurrency_permit_held.reset(token)
                                self._concurrency_semaphore.release()

                        if bound_operator is not None and bound_chunk_run_context is not None:
                            await self._emit_chunk_runtime_event(
                                "chunk.completed",
                                bound_chunk_run_context,
                                operator=bound_operator,
                                signal=signal,
                                message=(
                                    f"Chunk '{ bound_chunk_run_context.meta.get('chunk_name', bound_chunk_run_context.run_id) }' completed."
                                ),
                                payload={
                                    "status": "waiting" if self.is_waiting() else "completed",
                                    "returned_pause_signal": isinstance(result, TriggerFlowPauseSignal),
                                    "input": self._serialize_runtime_value(signal.value),
                                    "signal_source": signal.source,
                                    "signal_meta": self._serialize_runtime_value(signal.meta),
                                    "output": (
                                        None
                                        if isinstance(result, TriggerFlowPauseSignal)
                                        else self._serialize_runtime_value(result)
                                    ),
                                },
                            )
                        return result
                    finally:
                        self._active_handler_count -= 1
                        self._mark_activity()

                handler_func = default_stage_call_bridge.as_async(handler, managed=True)
                handler_data = TriggerFlowRuntimeData(
                    trigger_event=signal.trigger_event,
                    trigger_type=signal.trigger_type,
                    value=signal.value,
                    execution=self,
                    _layer_marks=signal.layer_marks.copy(),
                    signal=signal,
                    chunk_run_context=chunk_run_context,
                )
                tasks.append(
                    self._create_managed_task(
                        run_handler(
                            handler_func,
                            handler_data,
                            handler_id=handler_id,
                            bound_operator=operator,
                            bound_chunk_run_context=chunk_run_context,
                        ),
                        origin=f"handler:{ handler_id }",
                        retain_outcome=False,
                    )
                )

        if tasks:
            try:
                result = await asyncio.gather(*tasks, return_exceptions=self._skip_exceptions)
                self._mark_activity()
                return result
            except Exception as error:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._set_status(TRIGGER_FLOW_STATUS_FAILED)
                if not self._runtime_failed_emitted:
                    self._runtime_failed_emitted = True
                    await self._emit_runtime_event(
                        "triggerflow.execution_failed",
                        level="ERROR",
                        message=f"TriggerFlow execution '{ self.id }' failed.",
                        payload={"last_signal": signal.to_debug_dict()},
                        error=error,
                    )
                raise
        self._mark_activity()
        return None

    def on(
        self,
        trigger_event: str,
        handler: Any,
        *,
        trigger_type: TriggerFlowSignalType = "event",
        binding_id: str | None = None,
        handler_ref: dict[str, Any] | str | None = None,
        metadata: dict[str, Any] | None = None,
        durable: bool = True,
    ):
        return self._signal_net.register_dynamic_handler(
            trigger_event,
            handler,
            trigger_type=trigger_type,
            binding_id=binding_id,
            handler_ref=handler_ref,
            metadata=metadata,
            durable=durable,
        )

    def off(
        self,
        binding_id: str,
        *,
        trigger_type: TriggerFlowSignalType | None = None,
        trigger_event: str | None = None,
    ):
        return self._signal_net.unregister_dynamic_handler(
            binding_id,
            trigger_type=trigger_type,
            trigger_event=trigger_event,
        )

    # Change Runtime Data
    async def _async_change_runtime_data(
        self,
        operation: Literal["set", "append", "del"],
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        self._ensure_terminal_state_mutable()
        futures = []
        handlers = self._handlers["runtime_data"]

        match operation:
            case "set":
                self._runtime_data.set(key, value)
                value = self._runtime_data[key]
                self._bump_state_version()
            case "append":
                self._runtime_data.append(key, value)
                value = self._runtime_data[key]
                self._bump_state_version()
            case "del":
                missing = object()
                if self._runtime_data.get(key, missing) is not missing:
                    del self._runtime_data[key]
                    value = None
                    self._bump_state_version()
                else:
                    return
        if emit:
            if key in handlers:
                futures.append(
                    self.async_emit(
                        key,
                        value,
                        trigger_type="runtime_data",
                        _source="runtime_data",
                    )
                )

            if futures:
                await asyncio.gather(*futures, return_exceptions=self._skip_exceptions)

    async def async_set_state(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        return await self._async_change_runtime_data("set", key, value, emit=emit)

    async def async_append_state(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        return await self._async_change_runtime_data("append", key, value, emit=emit)

    async def async_del_state(
        self,
        key: str,
        *,
        emit: bool = True,
    ):
        return await self._async_change_runtime_data("del", key, None, emit=emit)

    async def async_set_runtime_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        self._warn_runtime_data_api("async_set_runtime_data")
        return await self.async_set_state(key, value, emit=emit)

    async def async_append_runtime_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        self._warn_runtime_data_api("async_append_runtime_data")
        return await self.async_append_state(key, value, emit=emit)

    async def async_del_runtime_data(
        self,
        key: str,
        *,
        emit: bool = True,
    ):
        self._warn_runtime_data_api("async_del_runtime_data")
        return await self.async_del_state(key, emit=emit)

    def _warn_wait_for_result_deprecated(self, method_name: str):
        DeprecationWarnings.warn_deprecated_once(
            f"TriggerFlowExecution.{ method_name }.wait_for_result",
            f"TriggerFlowExecution.{ method_name }(..., wait_for_result=...) is deprecated. "
            "Execution start behavior is now driven by auto_close: "
            "auto-close executions wait for close; manual-close executions start and return the execution handle. "
            "Use create_execution()/start_execution() for explicit lifecycle control.",
            stacklevel=3,
        )

    async def _async_run_start(self, initial_value: InputT | None = None):
        self._active_runtime_operation_count += 1
        self._mark_activity()
        try:
            return await self._async_run_start_inner(initial_value)
        finally:
            self._active_runtime_operation_count -= 1
            self._mark_activity()

    async def _async_run_start_inner(self, initial_value: InputT | None = None):
        if self._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
            signal = self._build_signal("START", initial_value, trigger_type="event", source="start")
            await self._reject_signal(signal)
            return self
        if self._started:
            return self

        await self._ensure_execution_resources()
        self._started = True
        self._started_at = time.time()
        self._mark_activity()
        if self._status not in {
            TRIGGER_FLOW_STATUS_COMPLETED,
            TRIGGER_FLOW_STATUS_FAILED,
            TRIGGER_FLOW_STATUS_CANCELLED,
        }:
            self._set_status(TRIGGER_FLOW_STATUS_RUNNING)
        if not self._runtime_started_emitted:
            await self._emit_runtime_definition_event()
            self._runtime_started_emitted = True
            await self._emit_runtime_event(
                "triggerflow.execution_started",
                message=f"TriggerFlow execution '{ self.id }' started.",
                payload={"initial_value": initial_value},
            )
        initial_value = cast(InputT | None, self._trigger_flow._contract.validate_initial_input(initial_value))
        try:
            await self._async_dispatch_signal(
                self._build_signal(
                    "START",
                    initial_value,
                    trigger_type="event",
                    source="start",
                )
            )
        except Exception as error:
            if not self._runtime_failed_emitted:
                self._runtime_failed_emitted = True
                await self._emit_runtime_event(
                    "triggerflow.execution_failed",
                    level="ERROR",
                    message=f"TriggerFlow execution '{ self.id }' failed during start.",
                    payload={"initial_value": initial_value},
                    error=error,
                )
            raise
        return self

    @overload
    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[True] = True,
        timeout: float | None = None,
    ) -> ResultT: ...

    @overload
    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[False],
        timeout: float | None = None,
    ) -> None: ...

    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: bool = True,
        timeout: float | None = None,
    ) -> Any:
        if not self._auto_close:
            raise ValueError(
                "TriggerFlowExecution.start() with auto_close=False is not supported in sync mode. "
                "Use await execution.async_start(...) and close the execution explicitly."
            )
        return default_stage_call_bridge.as_sync(self.async_start)(
            initial_value,
            wait_for_result=wait_for_result,
            timeout=timeout,
        )

    @overload
    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[True] = True,
        timeout: float | None = None,
    ) -> ResultT: ...

    @overload
    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[False],
        timeout: float | None = None,
    ) -> None: ...

    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: bool = True,
        timeout: float | None = None,
    ) -> Any:
        if wait_for_result is False:
            self._warn_wait_for_result_deprecated("async_start")
        if timeout is not None:
            self._auto_close_timeout = timeout

        await self._async_run_start(initial_value)

        if self._auto_close:
            return await self._async_wait_for_close_snapshot()
        return self

    # Pause / Continue
    async def async_pause_for(
        self,
        *,
        type: str = "pause",
        exchange_kind: str | None = None,
        payload: Any = None,
        resume_event: str | None = None,
        interrupt_id: str | None = None,
        resume_to: Any = None,
        max_resumes: int | None = 1,
        channel_id: str | None = None,
        provider_id: str | None = None,
        wait_mode: str = "disconnected",
        hot_wait_timeout: float | None = None,
        cold_persistence_policy: str = "persist",
        request_payload_schema: dict[str, Any] | None = None,
        response_payload_schema: dict[str, Any] | None = None,
        audit_metadata: dict[str, Any] | None = None,
    ):
        return await self._interrupts.async_pause_for(
            type=type,
            exchange_kind=exchange_kind,
            payload=payload,
            resume_event=resume_event,
            interrupt_id=interrupt_id,
            resume_to=resume_to,
            max_resumes=max_resumes,
            channel_id=channel_id,
            provider_id=provider_id,
            wait_mode=wait_mode,
            hot_wait_timeout=hot_wait_timeout,
            cold_persistence_policy=cold_persistence_policy,
            request_payload_schema=request_payload_schema,
            response_payload_schema=response_payload_schema,
            audit_metadata=audit_metadata,
        )

    async def async_continue_with(
        self,
        interrupt_id: str,
        value: Any = None,
        *,
        resume_request_id: str | None = None,
        actor: str | None = None,
    ):
        return await self._interrupts.async_continue_with(
            interrupt_id,
            value,
            resume_request_id=resume_request_id,
            actor=actor,
        )

    # Runtime Stream
    async def async_put_into_stream(
        self,
        stream_item: StreamT | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent,
        *,
        _skip_contract_validation: bool = False,
        _origin_chunk: dict[str, Any] | None = None,
    ):
        return await self._runtime_io.async_put_into_stream(
            stream_item,
            _skip_contract_validation=_skip_contract_validation,
            _origin_chunk=_origin_chunk,
        )

    async def async_stop_stream(self):
        return await self._runtime_io.async_stop_stream()

    async def _consume_runtime_stream(
        self,
        *,
        initial_value: InputT | None,
        timeout: float | None,
    ) -> AsyncGenerator[StreamT | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None]:
        async for item in self._runtime_io.consume_runtime_stream(
            initial_value=initial_value,
            timeout=timeout,
        ):
            yield cast(StreamT | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, item)

    def get_async_runtime_stream(
        self,
        initial_value: InputT | None = None,
        *,
        timeout: float | None = 10,
    ) -> AsyncGenerator[StreamT | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None]:
        return self._runtime_io.get_async_runtime_stream(initial_value, timeout=timeout)

    def get_runtime_stream(
        self,
        initial_value: InputT | None = None,
        *,
        timeout: float | None = 10,
    ) -> Generator[StreamT | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None, None]:
        return self._runtime_io.get_runtime_stream(initial_value, timeout=timeout)

    # Result
    def _resolve_compat_result_or_snapshot(self):
        return self._runtime_io.resolve_compat_result_or_snapshot()

    def set_result(self, result: ResultT, *, _origin_chunk: dict[str, Any] | None = None):
        DeprecationWarnings.warn_deprecated_once(
            "TriggerFlowExecution.set_result",
            "TriggerFlowExecution.set_result() is deprecated; write execution state directly and let close() return "
            "the close snapshot. For compatibility, set_result() now writes '$final_result'.",
            stacklevel=2,
        )
        return self._runtime_io.set_result(result, _origin_chunk=_origin_chunk)

    async def async_get_result(self, *, timeout: float | None = None) -> ResultT | None:
        DeprecationWarnings.warn_deprecated_once(
            "TriggerFlowExecution.get_result",
            "TriggerFlowExecution.get_result()/async_get_result() are compatibility APIs; "
            "prefer close()/async_close() and execution state APIs for lifecycle-oriented workflows. "
            "get_result() now returns '$final_result' when present, otherwise the close snapshot.",
            stacklevel=2,
        )
        return await self._runtime_io.async_get_result(timeout=timeout)
