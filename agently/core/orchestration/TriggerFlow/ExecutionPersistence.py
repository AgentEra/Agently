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


import asyncio
import hashlib
import importlib
import inspect
import json
import time
import uuid
from json import JSONDecodeError
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast

import yaml

from agently.types.data import ExecutionResourceRequirement
from agently.types.data import EMPTY, RunContext
from agently.types.trigger_flow.contract import TriggerFlowExecutionLoadReport
from agently.types.trigger_flow.runtime_keys import (
    DURABLE_SYSTEM_STATE_KEYS,
    TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND,
    TRIGGER_FLOW_SNAPSHOT_SCHEMA_VERSION,
    TRIGGER_FLOW_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS,
)
from .Control import (
    TRIGGER_FLOW_LIFECYCLE_CLOSED,
    TRIGGER_FLOW_LIFECYCLE_OPEN,
    TRIGGER_FLOW_LIFECYCLE_SEALED,
    TRIGGER_FLOW_STATUS_COMPLETED,
    TRIGGER_FLOW_STATUS_CREATED,
    TRIGGER_FLOW_STATUS_FAILED,
)
from .ExecutionState import INTERVENTIONS_STATE_KEY, TriggerFlowInterventionMode
from ._runtime_stream_transport import StageRuntimeStreamTransport
from .SnapshotProjection import (
    TRIGGER_FLOW_SNAPSHOT_PROJECTION_VERSION,
    TriggerFlowSnapshotProjector,
    is_value_digest_projection,
    validate_value_digest_projection,
)

if TYPE_CHECKING:
    from .Execution import TriggerFlowExecution


class TriggerFlowExecutionPersistence:
    def __init__(self, execution: "TriggerFlowExecution[Any, Any, Any]"):
        self._execution = execution

    def save(
        self,
        path: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
        require_idle: bool = False,
    ):
        execution = self._execution
        if require_idle and not execution.is_idle():
            raise RuntimeError(
                f"Can not save TriggerFlowExecution { execution.id } with require_idle=True while tasks are active."
            )
        snapshot_id = uuid.uuid4().hex
        saved_at = time.time()
        result = execution._system_runtime_data.get("result")
        result_ready = result is not EMPTY
        durable_system_state = self._collect_durable_system_state()
        interrupts = execution._to_serializable_value(execution._get_interrupts())
        run_context = execution.run_context.model_dump(mode="json")
        runtime_data = json.loads(execution._runtime_data.dump("json"))
        flow_data = json.loads(execution._trigger_flow._flow_data.dump("json"))
        intervention = {
            "mode": execution._intervention_mode,
            "policy": execution._intervention_policy_name,
            "version": execution._interventions_version,
            "ledger": execution._to_serializable_value(execution._get_intervention_records()),
        }
        sub_flow_frames = execution._to_serializable_value(execution._get_sub_flow_frames())
        last_signal = execution._serialize_signal(execution.get_last_signal())
        result_state = {
            "ready": result_ready,
            "value": execution._to_serializable_value(result) if result_ready else None,
        }
        signal_net = execution._signal_net.to_snapshot()
        interrupts, signal_net, snapshot_projection = TriggerFlowSnapshotProjector(
            execution._serializable_snapshot_projection_policy()
        ).project(
            interrupts=interrupts,
            signal_net=signal_net,
            execution_idle=execution.is_idle(),
        )
        resource_keys = sorted(str(key) for key in execution.get_runtime_resources().keys())
        managed_resource_keys = sorted(
            str(handle.get("resource_key", ""))
            for handle in execution._managed_execution_resource_handles
        )
        execution_resource_requirement_ids = sorted(
            str(requirement.get("requirement_id", ""))
            for requirement in execution._execution_resource_requirements
            if requirement.get("requirement_id")
        )
        resource_requirements = self._build_resource_requirements(
            resource_keys=resource_keys,
            managed_resource_keys=managed_resource_keys,
            execution_resource_requirement_ids=execution_resource_requirement_ids,
        )
        state = self._build_execution_snapshot(
            snapshot_id=snapshot_id,
            saved_at=saved_at,
            durable_system_state=durable_system_state,
            run_context=run_context,
            runtime_data=runtime_data,
            flow_data=flow_data,
            interrupts=interrupts,
            intervention=intervention,
            sub_flow_frames=sub_flow_frames,
            last_signal=last_signal,
            result_state=result_state,
            signal_net=signal_net,
            snapshot_projection=snapshot_projection,
            resource_keys=resource_keys,
            managed_resource_keys=managed_resource_keys,
            execution_resource_requirement_ids=execution_resource_requirement_ids,
            resource_requirements=resource_requirements,
        )
        state.update(
            {
            "execution_id": execution.id,
            "status": execution._status,
            "lifecycle_state": execution._lifecycle_state,
            "auto_close": execution._auto_close,
            "auto_close_timeout": execution._auto_close_timeout,
            "created_at": execution._created_at,
            "started_at": execution._started_at,
            "last_activity_at": execution._last_activity_at,
            "sealed_at": execution._sealed_at,
            "closed_at": execution._closed_at,
            "close_reason": execution._close_reason,
            "state_version": execution._state_version,
            "owner_id": execution._owner_id,
            "lease_ttl": execution._lease_ttl,
            "lease_until": execution._lease_until,
            "heartbeat_at": execution._heartbeat_at,
            "pending_task_count": len(
                [
                    task
                    for task in execution._pending_tasks
                    if task is not execution._auto_close_task and not task.done()
                ]
            ),
            "result": result_state,
            }
        )
        if path is None:
            return state

        target = Path(path)
        suffix = target.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            content = yaml.safe_dump(
                state,
                indent=2,
                allow_unicode=True,
                sort_keys=False,
            )
        else:
            content = json.dumps(
                state,
                indent=2,
                ensure_ascii=False,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return state

    def _collect_durable_system_state(self):
        execution = self._execution
        durable_state: dict[str, Any] = {}
        for key in DURABLE_SYSTEM_STATE_KEYS:
            value = execution._system_runtime_data.get(key, EMPTY, inherit=False)
            if value is EMPTY or value is None or value == {}:
                continue
            durable_state[key] = execution._to_serializable_value(value)
        return durable_state

    def _runtime_resource_scope(self, key: str):
        execution = self._execution
        execution_resources = execution._runtime_resources.get(None, {}, inherit=False)
        if isinstance(execution_resources, dict) and key in execution_resources:
            return "execution"
        flow_resources = execution._trigger_flow._runtime_resources.get(None, {}, inherit=False)
        if isinstance(flow_resources, dict) and key in flow_resources:
            return "flow"
        return "external"

    def _build_resource_requirements(
        self,
        *,
        resource_keys: list[str],
        managed_resource_keys: list[str],
        execution_resource_requirement_ids: list[str],
    ):
        execution = self._execution
        resource_requirements: list[dict[str, Any]] = [
            {
                "kind": "runtime_resource",
                "key": key,
                "required": True,
                "source": self._runtime_resource_scope(key),
                "metadata": {"scope": self._runtime_resource_scope(key)},
            }
            for key in resource_keys
        ]
        requirements_by_id = {
            str(requirement.get("requirement_id")): requirement
            for requirement in execution._execution_resource_requirements
            if requirement.get("requirement_id")
        }
        resource_requirements.extend(
            {
                "kind": "managed_execution_resource",
                "key": key,
                "required": True,
                "source": "managed",
                "metadata": self._managed_environment_metadata(key),
            }
            for key in managed_resource_keys
            if key
        )
        resource_requirements.extend(
            {
                "kind": "execution_resource_requirement",
                "key": requirement_id,
                "required": True,
                "source": "managed",
                "metadata": self._execution_resource_requirement_metadata(
                    dict(requirements_by_id.get(requirement_id, {})),
                ),
            }
            for requirement_id in execution_resource_requirement_ids
        )
        resource_requirements.extend(
            execution._to_serializable_value(requirement)
            for requirement in execution.get_resource_requirements()
        )
        return self._dedupe_resource_requirements(resource_requirements)

    def _dedupe_resource_requirements(self, requirements: list[dict[str, Any]]):
        deduped_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for requirement in requirements:
            key = (
                str(requirement.get("kind", "")),
                str(requirement.get("key", "")),
                str(requirement.get("source", "")),
            )
            if key not in seen:
                seen.add(key)
                order.append(key)
            deduped_by_key[key] = requirement
        return [deduped_by_key[key] for key in order]

    def _managed_environment_metadata(self, resource_key: str):
        execution = self._execution
        for handle in execution._managed_execution_resource_handles:
            if str(handle.get("resource_key", "")) != resource_key:
                continue
            metadata = {
                key: value
                for key, value in dict(handle).items()
                if key != "resource"
            }
            return execution._to_serializable_value(metadata)
        return {"resource_key": resource_key}

    def _execution_resource_requirement_metadata(self, requirement: dict[str, Any]):
        execution = self._execution
        if not requirement:
            return {}
        return {
            "resource_key": requirement.get("resource_key"),
            "requirement_id": requirement.get("requirement_id"),
            "kind": requirement.get("kind"),
            "requirement": execution._to_serializable_value(requirement),
        }

    def _collect_resume_ledger(self, interrupts: dict[str, Any]):
        ledger: dict[str, Any] = {}
        for interrupt_id, interrupt in interrupts.items():
            if not isinstance(interrupt, dict):
                continue
            requests = interrupt.get("resume_requests", {})
            if requests:
                ledger[str(interrupt_id)] = requests
            elif interrupt.get("resume_request_id"):
                ledger[str(interrupt_id)] = {
                    str(interrupt["resume_request_id"]): {
                        "status": "accepted",
                        "accepted_at": interrupt.get("resumed_at"),
                    }
                }
        return self._execution._to_serializable_value(ledger)

    def _build_execution_snapshot(
        self,
        *,
        snapshot_id: str,
        saved_at: float,
        durable_system_state: dict[str, Any],
        run_context: dict[str, Any],
        runtime_data: dict[str, Any],
        flow_data: dict[str, Any],
        interrupts: dict[str, Any],
        intervention: dict[str, Any],
        sub_flow_frames: dict[str, Any],
        last_signal: dict[str, Any] | None,
        result_state: dict[str, Any],
        signal_net: dict[str, Any],
        snapshot_projection: dict[str, Any],
        resource_keys: list[str],
        managed_resource_keys: list[str],
        execution_resource_requirement_ids: list[str],
        resource_requirements: list[dict[str, Any]],
    ):
        execution = self._execution
        return {
            "schema_version": TRIGGER_FLOW_SNAPSHOT_SCHEMA_VERSION,
            "kind": TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND,
            "snapshot_id": snapshot_id,
            "created_at": saved_at,
            "execution_id": execution.id,
            "flow_name": execution._trigger_flow.name,
            "flow_definition_fingerprint": self._current_flow_definition_fingerprint(),
            "status": execution._status,
            "lifecycle_state": execution._lifecycle_state,
            "state_version": execution._state_version,
            "owner_id": execution._owner_id,
            "lease": {
                "owner_id": execution._owner_id,
                "lease_ttl": execution._lease_ttl,
                "lease_until": execution._lease_until,
                "heartbeat_at": execution._heartbeat_at,
            },
            "run_context": run_context,
            "runtime_data": runtime_data,
            "flow_data": flow_data,
            "interrupts": interrupts,
            "intervention": intervention,
            "sub_flow_frames": sub_flow_frames,
            "last_signal": last_signal,
            "signal_net": signal_net,
            "snapshot_projection": snapshot_projection,
            "snapshot_retention_policy": execution._to_serializable_value(
                execution._snapshot_retention_policy
            ),
            "result": result_state,
            "durable_system_state": durable_system_state,
            "resource_requirements": resource_requirements,
            "resource_keys": resource_keys,
            "managed_resource_keys": managed_resource_keys,
            "execution_resource_requirement_ids": execution_resource_requirement_ids,
            "resume_ledger": self._collect_resume_ledger(interrupts),
            "compaction": {
                "segments": execution._to_serializable_value(execution._compaction_segments),
                "retained_lineage_anchors": execution._to_serializable_value(
                    execution._retained_lineage_anchors
                ),
                "artifact_refs": execution._to_serializable_value(execution._snapshot_artifact_refs),
                "policy": execution._serializable_compaction_policy(),
                "load_policy": execution._to_serializable_value(execution._load_policy),
            },
        }

    def load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: list[ExecutionResourceRequirement] | None = None,
        validate_resources: bool = False,
    ):
        execution = self._execution
        state = self._load_state_content(state, encoding=encoding)
        self._validate_state_sections(state)
        snapshot_state = state
        self._raise_for_snapshot_contract(snapshot_state)
        active_sub_flow_frame_ids = sorted(
            str(frame_id)
            for frame_id, frame in snapshot_state.get("sub_flow_frames", {}).items()
            if isinstance(frame, dict) and frame.get("status") in {"running", "cancel_requested"}
        )
        if active_sub_flow_frame_ids:
            raise RuntimeError(
                "Can not load TriggerFlow execution snapshot with active sub-flow frame(s) "
                f"{ active_sub_flow_frame_ids }. Live child executions and tasks are process-local and are not "
                "restart-resumable; settle or cancel them before saving a resumable snapshot."
            )
        if validate_resources:
            load = self.inspect_load_state(
                snapshot_state,
                runtime_resources=runtime_resources,
                execution_resources=execution_resources,
            )
            if not load.get("ready", False):
                raise RuntimeError(
                    f"Can not load TriggerFlow execution { state.get('execution_id', execution.id) }; "
                    f"missing resources: { load.get('missing_resource_keys', []) }."
                )

        runtime_data = snapshot_state.get("runtime_data", {})
        flow_data = snapshot_state.get("flow_data", {})
        interrupts = snapshot_state.get("interrupts", {})
        intervention_state = snapshot_state.get("intervention", {}) or {}
        interventions = intervention_state.get("ledger", runtime_data.get(INTERVENTIONS_STATE_KEY, {}))
        if interventions is None:
            interventions = {}
        durable_system_state = snapshot_state.get("durable_system_state", {})
        if durable_system_state is None:
            durable_system_state = {}
        sub_flow_frames = snapshot_state.get("sub_flow_frames", {})
        last_signal_state = snapshot_state.get("last_signal", None)
        signal_net_state = snapshot_state.get("signal_net", {})
        result_state = snapshot_state.get("result", {})
        execution_id = snapshot_state.get("execution_id", execution.id)
        run_context_state = snapshot_state.get("run_context", None)

        ready = bool(result_state.get("ready", False))
        result_value = result_state.get("value")
        status = str(snapshot_state.get("status", TRIGGER_FLOW_STATUS_CREATED))
        lifecycle_state = str(snapshot_state.get("lifecycle_state", TRIGGER_FLOW_LIFECYCLE_OPEN))
        if lifecycle_state not in {
            TRIGGER_FLOW_LIFECYCLE_OPEN,
            TRIGGER_FLOW_LIFECYCLE_SEALED,
            TRIGGER_FLOW_LIFECYCLE_CLOSED,
        }:
            lifecycle_state = TRIGGER_FLOW_LIFECYCLE_OPEN

        original_execution_id = execution.id
        execution.id = execution_id
        if original_execution_id != execution.id:
            execution._trigger_flow._executions.pop(original_execution_id, None)
            execution._trigger_flow._executions[execution.id] = execution

        if run_context_state is not None:
            execution.run_context = RunContext.model_validate(run_context_state)
        if execution.run_context.execution_id is None:
            execution.run_context.execution_id = execution.id

        execution._runtime_data.clear()
        execution._runtime_data.update(runtime_data)

        execution._trigger_flow._flow_data.clear()
        execution._trigger_flow._flow_data.update(flow_data)

        result_ready = asyncio.Event()
        if ready:
            execution._system_runtime_data.set("result", result_value)
            result_ready.set()
        else:
            execution._system_runtime_data.set("result", EMPTY)
        execution._system_runtime_data.set("result_ready", result_ready)
        execution._system_runtime_data.set("interrupts", interrupts)
        execution._intervention_mode = cast(
            TriggerFlowInterventionMode,
            intervention_state.get("mode", execution._intervention_mode),
        )
        if execution._intervention_mode not in {None, "planned", "auto"}:
            execution._intervention_mode = None
        saved_policy = intervention_state.get("policy", execution._intervention_policy_name)
        execution._intervention_policy_name = str(saved_policy) if saved_policy is not None else None
        if execution._intervention_policy is not None:
            execution._intervention_policy_name = execution._resolve_intervention_policy_name(
                execution._intervention_policy
            )
        try:
            execution._interventions_version = int(intervention_state.get("version", 0))
        except (TypeError, ValueError):
            execution._interventions_version = 0
        if interventions:
            execution._interventions_version = max(
                execution._interventions_version,
                max(
                    int(record.get("version", 0))
                    for record in interventions.values()
                    if isinstance(record, dict)
                ),
            )
        execution._system_runtime_data.set("interventions", execution._to_serializable_value(interventions))
        execution._system_runtime_data.set("intervention_mode", execution._intervention_mode)
        execution._system_runtime_data.set("intervention_policy", execution._intervention_policy_name)
        execution._system_runtime_data.set("intervention_version", execution._interventions_version)
        execution._runtime_data.set(INTERVENTIONS_STATE_KEY, execution._to_serializable_value(interventions))
        execution._system_runtime_data.set("sub_flow_frames", sub_flow_frames)
        execution._system_runtime_data.set("last_signal", last_signal_state)
        execution._signal_net.load_snapshot(signal_net_state if isinstance(signal_net_state, dict) else {})
        self._restore_durable_system_state(durable_system_state)
        execution._set_status(status)
        execution._auto_close = bool(snapshot_state.get("auto_close", execution._auto_close))
        execution._auto_close_timeout = snapshot_state.get("auto_close_timeout", execution._auto_close_timeout)
        execution._lifecycle_state = lifecycle_state
        execution._system_runtime_data.set("lifecycle_state", lifecycle_state)
        execution._created_at = float(snapshot_state.get("created_at", execution._created_at) or execution._created_at)
        execution._started_at = snapshot_state.get("started_at", execution._started_at)
        execution._last_activity_at = snapshot_state.get("last_activity_at", execution._last_activity_at)
        execution._sealed_at = snapshot_state.get("sealed_at", execution._sealed_at)
        execution._closed_at = snapshot_state.get("closed_at", execution._closed_at)
        execution._close_reason = snapshot_state.get("close_reason", execution._close_reason)
        execution._state_version = int(snapshot_state.get("state_version", execution._state_version))
        execution._system_runtime_data.set("state_version", execution._state_version)
        execution._owner_id = snapshot_state.get("owner_id", execution._owner_id)
        execution._lease_ttl = snapshot_state.get("lease_ttl", execution._lease_ttl)
        execution._lease_until = snapshot_state.get("lease_until", execution._lease_until)
        execution._heartbeat_at = snapshot_state.get("heartbeat_at", execution._heartbeat_at)
        execution._execution_resource_requirements = self._resolve_execution_resource_requirements(
            snapshot_state,
            execution_resources=execution_resources,
        )
        compaction_state = self._snapshot_compaction_state(snapshot_state)
        execution._compaction_segments = compaction_state["segments"]
        execution._retained_lineage_anchors = compaction_state["retained_lineage_anchors"]
        execution._snapshot_artifact_refs = compaction_state["artifact_refs"]
        execution._compaction_policy = compaction_state["policy"]
        execution._load_policy = compaction_state["load_policy"]
        snapshot_projection = snapshot_state.get("snapshot_projection", {})
        if isinstance(snapshot_projection, dict):
            saved_projection_policy = snapshot_projection.get("policy")
            if isinstance(saved_projection_policy, dict):
                execution._snapshot_projection_policy = execution._to_serializable_value(
                    saved_projection_policy
                )
        saved_retention_policy = snapshot_state.get("snapshot_retention_policy")
        execution._snapshot_retention_policy = (
            execution._normalize_snapshot_retention_policy(saved_retention_policy)
            if saved_retention_policy is not None
            else None
        )
        execution._runtime_stream_transport = StageRuntimeStreamTransport()
        execution._runtime_io.reset_stream_start_state()
        execution._runtime_stream_stopped = lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED
        if lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            execution._runtime_stream_transport.close()
            close_result = execution._build_close_snapshot()
            execution._closed_event.set()
            execution._close_result = close_result
            execution._close_started = True
        else:
            execution._closed_event.clear()
            execution._close_started = False
            execution._close_result = None
        execution._started = (
            status != TRIGGER_FLOW_STATUS_CREATED
            or bool(runtime_data)
            or ready
            or bool(interrupts)
        )
        execution._runtime_started_emitted = execution._started
        execution._runtime_completed_emitted = status == TRIGGER_FLOW_STATUS_COMPLETED and ready
        execution._runtime_failed_emitted = status == TRIGGER_FLOW_STATUS_FAILED
        if runtime_resources:
            execution.update_runtime_resources(runtime_resources)
        if execution._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_CLOSED:
            execution._ensure_auto_close_monitor()

        return execution

    def inspect_load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: list[ExecutionResourceRequirement] | None = None,
    ) -> TriggerFlowExecutionLoadReport:
        state = self._load_state_content(state, encoding=encoding)
        self._validate_state_sections(state)
        return self.inspect_load_state(
            state,
            runtime_resources=runtime_resources,
            execution_resources=execution_resources,
        )

    def inspect_load_state(
        self,
        state: dict[str, Any],
        *,
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: list[ExecutionResourceRequirement] | None = None,
    ) -> TriggerFlowExecutionLoadReport:
        execution = self._execution
        snapshot_state = state
        resource_requirements = self._snapshot_resource_requirements(snapshot_state)
        execution_resource_requirements = self._resolve_execution_resource_requirements(
            snapshot_state,
            execution_resources=execution_resources,
        )
        available_resource_keys = self._available_resource_keys(runtime_resources=runtime_resources)
        environment_resource_keys = {
            str(requirement.get("resource_key"))
            for requirement in execution_resource_requirements
            if requirement.get("resource_key")
        }
        missing_resource_keys: set[str] = set()
        unresolved_resource_keys: set[str] = set()
        pending_resolver_keys: set[str] = set()
        pending_environment_resource_keys: set[str] = set()
        policy_blocked_resource_keys: set[str] = set()
        resolved_resource_keys: set[str] = set()
        diagnostics: list[dict[str, Any]] = self._snapshot_contract_diagnostics(snapshot_state)
        diagnostics.extend(self._resume_ledger_diagnostics(snapshot_state))
        missing_dynamic_event_handlers = execution._signal_net.snapshot_missing_handler_ids(
            snapshot_state.get("signal_net", {})
        )
        for binding_id in missing_dynamic_event_handlers:
            resource_key = f"dynamic_event_handler:{ binding_id }"
            missing_resource_keys.add(resource_key)
            diagnostics.append(
                {
                    "code": "triggerflow.signal_net.missing_dynamic_handler",
                    "severity": "warning",
                    "message": (
                        f"Dynamic event binding '{ binding_id }' requires a registered "
                        "handler before TriggerFlow load can be ready."
                    ),
                    "resource_key": resource_key,
                    "details": {"binding_id": binding_id},
                }
            )
        snapshot_errors = [
            diagnostic
            for diagnostic in diagnostics
            if diagnostic.get("severity") == "error"
        ]

        for requirement in resource_requirements:
            if requirement.get("required", True) is False:
                continue
            resource_key = self._resource_requirement_resource_key(requirement)
            if not resource_key:
                continue
            fail_policy = self._resource_requirement_fail_policy(requirement)
            descriptor_health = self._resource_requirement_health(requirement)
            if self._resource_health_blocks_load(descriptor_health):
                severity = self._resource_policy_severity(fail_policy)
                if severity == "error":
                    missing_resource_keys.add(resource_key)
                    if descriptor_health == "policy_forbidden":
                        policy_blocked_resource_keys.add(resource_key)
                else:
                    unresolved_resource_keys.add(resource_key)
                diagnostics.append(
                    {
                        "code": self._resource_health_diagnostic_code(descriptor_health),
                        "severity": severity,
                        "message": (
                            f"Resource '{ resource_key }' is { descriptor_health } during TriggerFlow "
                            f"load."
                        ),
                        "resource_key": resource_key,
                        "health": descriptor_health,
                        "fail_policy": fail_policy,
                        "requirement": execution._to_serializable_value(requirement),
                    }
                )
                continue
            if resource_key in available_resource_keys:
                resolved_resource_keys.add(resource_key)
                resolver_ref = self._resource_requirement_resolver(requirement)
                if resolver_ref:
                    diagnostics.append(
                        {
                            "code": "triggerflow.load.resolved_resource",
                            "severity": "info",
                            "message": f"Resource '{ resource_key }' is available for TriggerFlow load.",
                            "resource_key": resource_key,
                            "resolver": resolver_ref,
                            "requirement": execution._to_serializable_value(requirement),
                        }
                    )
                continue
            if resource_key in environment_resource_keys:
                pending_environment_resource_keys.add(resource_key)
                severity = self._resource_policy_severity(fail_policy)
                if severity == "error":
                    missing_resource_keys.add(resource_key)
                else:
                    unresolved_resource_keys.add(resource_key)
                continue
            resolver_ref = self._resource_requirement_resolver(requirement)
            if resolver_ref:
                if self._resolver_reference_available(resolver_ref):
                    pending_resolver_keys.add(resource_key)
                    severity = self._resource_policy_severity(fail_policy)
                    if severity == "error":
                        missing_resource_keys.add(resource_key)
                    else:
                        unresolved_resource_keys.add(resource_key)
                    diagnostics.append(
                        {
                            "code": "triggerflow.load.resolver_pending",
                            "severity": severity,
                            "message": (
                                f"Resource '{ resource_key }' is expected to be restored by resolver "
                                f"'{ resolver_ref }'."
                            ),
                            "resource_key": resource_key,
                            "resolver": resolver_ref,
                            "requirement": execution._to_serializable_value(requirement),
                        }
                    )
                    continue
                diagnostic_code = "triggerflow.load.missing_resolver"
                message = (
                    f"Resource '{ resource_key }' declares resolver '{ resolver_ref }', "
                    "but the resolver module is not importable."
                )
            else:
                diagnostic_code = "triggerflow.load.missing_resource"
                message = f"Resource '{ resource_key }' is required but was not provided."
            severity = self._resource_policy_severity(fail_policy)
            if severity == "error":
                missing_resource_keys.add(resource_key)
            else:
                unresolved_resource_keys.add(resource_key)
            diagnostics.append(
                {
                    "code": diagnostic_code,
                    "severity": severity,
                    "message": message,
                    "resource_key": resource_key,
                    "resolver": resolver_ref,
                    "fail_policy": fail_policy,
                    "requirement": execution._to_serializable_value(requirement),
                }
            )

        ready = not snapshot_errors and not missing_resource_keys
        status = "ready"
        if snapshot_errors:
            status = "invalid_snapshot"
        elif missing_resource_keys:
            pending_resource_keys = pending_resolver_keys | pending_environment_resource_keys
            status = "pending_resources" if missing_resource_keys <= pending_resource_keys else "missing_resources"
        return cast(
            TriggerFlowExecutionLoadReport,
            {
                "snapshot": snapshot_state,
                "execution_id": str(snapshot_state.get("execution_id", execution.id)),
                "status": status,
                "ready": ready,
                "runtime_resources": {
                    key: "<provided>"
                    for key in sorted((runtime_resources or {}).keys())
                },
                "current_flow_definition_fingerprint": self._current_flow_definition_fingerprint(),
                "missing_resource_keys": sorted(missing_resource_keys),
                "unresolved_resource_keys": sorted(unresolved_resource_keys),
                "resolved_resource_keys": sorted(resolved_resource_keys),
                "pending_resolver_keys": sorted(pending_resolver_keys),
                "pending_environment_resource_keys": sorted(pending_environment_resource_keys),
                "policy_blocked_resource_keys": sorted(policy_blocked_resource_keys),
                "resource_requirements": resource_requirements,
                "execution_resource_requirements": execution._to_serializable_value(
                    execution_resource_requirements
                ),
                "compaction": self._snapshot_compaction_state(snapshot_state),
                "diagnostics": diagnostics,
            },
        )

    async def async_resolve_load_resources(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
        execution_resources: list[ExecutionResourceRequirement] | None = None,
        require_resources: bool = True,
    ):
        state = self._load_state_content(state, encoding=encoding)
        self._validate_state_sections(state)
        snapshot_state = state
        resource_requirements = self._snapshot_resource_requirements(snapshot_state)
        execution_resource_requirements = self._resolve_execution_resource_requirements(
            snapshot_state,
            execution_resources=execution_resources,
        )
        available_resource_keys = self._available_resource_keys(runtime_resources=runtime_resources)
        environment_resource_keys = {
            str(requirement.get("resource_key"))
            for requirement in execution_resource_requirements
            if requirement.get("resource_key")
        }
        resolved_resources: dict[str, Any] = {}
        resolved_resource_keys: set[str] = set()
        unresolved_resource_keys: set[str] = set()
        blocked_resource_keys: set[str] = set()
        diagnostics: list[dict[str, Any]] = []

        for requirement in resource_requirements:
            if requirement.get("required", True) is False:
                continue
            resource_key = self._resource_requirement_resource_key(requirement)
            if not resource_key:
                continue
            if resource_key in available_resource_keys or resource_key in environment_resource_keys:
                continue
            fail_policy = self._resource_requirement_fail_policy(requirement)
            descriptor_health = self._resource_requirement_health(requirement)
            if self._resource_health_blocks_load(descriptor_health):
                diagnostic = self._resource_health_diagnostic(
                    resource_key=resource_key,
                    requirement=requirement,
                    health=descriptor_health,
                    fail_policy=fail_policy,
                )
                diagnostics.append(diagnostic)
                if diagnostic.get("severity") == "error":
                    blocked_resource_keys.add(resource_key)
                else:
                    unresolved_resource_keys.add(resource_key)
                continue
            resolver_ref = self._resource_requirement_resolver(requirement)
            if not resolver_ref:
                continue
            try:
                resolver = self._import_resource_resolver(resolver_ref)
            except (ImportError, AttributeError, TypeError, ValueError) as exc:
                diagnostic = self._resolver_failure_diagnostic(
                    resource_key=resource_key,
                    requirement=requirement,
                    resolver_ref=resolver_ref,
                    fail_policy=fail_policy,
                    error=exc,
                    code="triggerflow.load.missing_resolver",
                )
                diagnostics.append(diagnostic)
                if diagnostic.get("severity") == "error":
                    blocked_resource_keys.add(resource_key)
                else:
                    unresolved_resource_keys.add(resource_key)
                continue

            context = self._resource_resolver_context(
                state=snapshot_state,
                snapshot_state=snapshot_state,
                requirement=requirement,
                resource_key=resource_key,
            )
            try:
                result = resolver(context)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                diagnostic = self._resolver_failure_diagnostic(
                    resource_key=resource_key,
                    requirement=requirement,
                    resolver_ref=resolver_ref,
                    fail_policy=fail_policy,
                    error=exc,
                    code="triggerflow.load.resolver_failed",
                )
                diagnostics.append(diagnostic)
                if diagnostic.get("severity") == "error":
                    blocked_resource_keys.add(resource_key)
                else:
                    unresolved_resource_keys.add(resource_key)
                continue

            resource, health = self._coerce_resolver_result(result)
            if self._resource_health_blocks_load(health):
                diagnostic = self._resource_health_diagnostic(
                    resource_key=resource_key,
                    requirement=requirement,
                    health=health,
                    fail_policy=fail_policy,
                    resolver_ref=resolver_ref,
                )
                diagnostics.append(diagnostic)
                if diagnostic.get("severity") == "error":
                    blocked_resource_keys.add(resource_key)
                else:
                    unresolved_resource_keys.add(resource_key)
                continue

            resolved_resources[resource_key] = resource
            available_resource_keys.add(resource_key)
            resolved_resource_keys.add(resource_key)
            diagnostics.append(
                {
                    "code": "triggerflow.load.resolver_resolved_resource",
                    "severity": "info",
                    "message": (
                        f"Resource '{ resource_key }' was restored by resolver "
                        f"'{ resolver_ref }'."
                    ),
                    "resource_key": resource_key,
                    "resolver": resolver_ref,
                    "health": health,
                    "requirement": self._execution._to_serializable_value(requirement),
                }
            )

        if require_resources and blocked_resource_keys:
            raise RuntimeError(
                f"Can not load TriggerFlow execution { state.get('execution_id', self._execution.id) }; "
                f"resource resolver failed for: { sorted(blocked_resource_keys) }."
            )

        return {
            "runtime_resources": resolved_resources,
            "resolved_resource_keys": sorted(resolved_resource_keys),
            "unresolved_resource_keys": sorted(unresolved_resource_keys),
            "blocked_resource_keys": sorted(blocked_resource_keys),
            "diagnostics": diagnostics,
        }

    def _current_flow_definition_fingerprint(self):
        return self._execution._trigger_flow._blue_print._get_definition_fingerprint()

    def _snapshot_contract_diagnostics(self, snapshot_state: dict[str, Any]):
        diagnostics: list[dict[str, Any]] = []
        kind = snapshot_state.get("kind")
        if kind != TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND:
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_kind",
                    "severity": "error",
                    "message": (
                        "TriggerFlow execution snapshot kind does not match "
                        f"{ TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND }."
                    ),
                    "expected": TRIGGER_FLOW_EXECUTION_SNAPSHOT_KIND,
                    "actual": kind,
                }
            )

        schema_version = snapshot_state.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version not in TRIGGER_FLOW_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS
        ):
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_schema_version",
                    "severity": "error",
                    "message": (
                        "TriggerFlow execution snapshot schema_version is not supported. "
                        f"Supported versions: { TRIGGER_FLOW_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS }."
                    ),
                    "expected": list(TRIGGER_FLOW_SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS),
                    "actual": schema_version,
                }
            )
        elif schema_version >= 2:
            diagnostics.extend(self._snapshot_projection_diagnostics(snapshot_state))

        snapshot_fingerprint = snapshot_state.get("flow_definition_fingerprint")
        current_fingerprint = self._current_flow_definition_fingerprint()
        if snapshot_fingerprint is None:
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.missing_flow_definition_fingerprint",
                    "severity": "error",
                    "message": "TriggerFlow execution snapshot has no flow definition fingerprint.",
                    "current": current_fingerprint,
                }
            )
        elif not isinstance(snapshot_fingerprint, str):
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_flow_definition_fingerprint",
                    "severity": "error",
                    "message": "TriggerFlow execution snapshot flow definition fingerprint must be a string.",
                    "actual": snapshot_fingerprint,
                }
            )
        elif snapshot_fingerprint != current_fingerprint:
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.flow_definition_mismatch",
                    "severity": "error",
                    "message": "TriggerFlow execution snapshot flow definition fingerprint mismatch.",
                    "expected": snapshot_fingerprint,
                    "actual": current_fingerprint,
                    "flow_name": snapshot_state.get("flow_name"),
                }
            )
        diagnostics.extend(self._snapshot_lease_diagnostics(snapshot_state))
        diagnostics.extend(self._snapshot_when_join_diagnostics(snapshot_state))
        diagnostics.extend(self._task_dag_snapshot_diagnostics(snapshot_state))
        diagnostics.extend(self._compaction_snapshot_diagnostics(snapshot_state))
        return diagnostics

    def _snapshot_projection_diagnostics(self, snapshot_state: dict[str, Any]):
        projection_state = snapshot_state.get("snapshot_projection")
        if not isinstance(projection_state, dict):
            return [
                {
                    "code": "triggerflow.snapshot.missing_projection_state",
                    "severity": "error",
                    "message": (
                        "TriggerFlow execution snapshot schema_version 2 requires "
                        "snapshot_projection metadata."
                    ),
                }
            ]

        diagnostics: list[dict[str, Any]] = []
        if projection_state.get("version") != TRIGGER_FLOW_SNAPSHOT_PROJECTION_VERSION:
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_projection_version",
                    "severity": "error",
                    "message": (
                        "TriggerFlow snapshot projection version is not supported."
                    ),
                    "expected": TRIGGER_FLOW_SNAPSHOT_PROJECTION_VERSION,
                    "actual": projection_state.get("version"),
                }
            )
        if not isinstance(projection_state.get("applied"), bool):
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_projection_state",
                    "severity": "error",
                    "message": "TriggerFlow snapshot projection applied flag must be boolean.",
                }
            )
        deferred_reason = projection_state.get("deferred_reason")
        if deferred_reason is not None and not isinstance(deferred_reason, str):
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_projection_state",
                    "severity": "error",
                    "message": (
                        "TriggerFlow snapshot projection deferred_reason must be "
                        "a string or None."
                    ),
                }
            )
        projected_interrupt_ids = projection_state.get("projected_terminal_interrupt_ids")
        if not isinstance(projected_interrupt_ids, list) or not all(
            isinstance(interrupt_id, str) for interrupt_id in projected_interrupt_ids
        ):
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_projection_state",
                    "severity": "error",
                    "message": (
                        "TriggerFlow snapshot projected_terminal_interrupt_ids "
                        "must be a list of strings."
                    ),
                }
            )
        for key in (
            "projected_value_count",
            "original_value_bytes",
            "projected_value_bytes",
        ):
            value = projection_state.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                diagnostics.append(
                    {
                        "code": "triggerflow.snapshot.invalid_projection_state",
                        "severity": "error",
                        "message": (
                            f"TriggerFlow snapshot projection '{ key }' "
                            "must be a non-negative integer."
                        ),
                    }
                )
        policy = projection_state.get("policy")
        if not isinstance(policy, dict):
            diagnostics.append(
                {
                    "code": "triggerflow.snapshot.invalid_projection_policy",
                    "severity": "error",
                    "message": "TriggerFlow snapshot projection policy must be a mapping.",
                }
            )
        else:
            mode = policy.get("terminal_value_mode")
            minimum_bytes = policy.get("min_value_bytes")
            if mode not in {"full", "digest"}:
                diagnostics.append(
                    {
                        "code": "triggerflow.snapshot.invalid_projection_policy",
                        "severity": "error",
                        "message": (
                            "TriggerFlow snapshot projection terminal_value_mode must be "
                            "'full' or 'digest'."
                        ),
                    }
                )
            if (
                not isinstance(minimum_bytes, int)
                or isinstance(minimum_bytes, bool)
                or minimum_bytes < 0
            ):
                diagnostics.append(
                    {
                        "code": "triggerflow.snapshot.invalid_projection_policy",
                        "severity": "error",
                        "message": (
                            "TriggerFlow snapshot projection min_value_bytes must be "
                            "a non-negative integer."
                        ),
                    }
                )
            for key in ("enabled", "project_terminal_signal_attempts"):
                if not isinstance(policy.get(key), bool):
                    diagnostics.append(
                        {
                            "code": "triggerflow.snapshot.invalid_projection_policy",
                            "severity": "error",
                            "message": (
                                f"TriggerFlow snapshot projection policy '{ key }' "
                                "must be boolean."
                            ),
                        }
                    )

        def validate_projected_values(value: Any, path: str):
            if is_value_digest_projection(value):
                try:
                    validate_value_digest_projection(value)
                except ValueError as exc:
                    diagnostics.append(
                        {
                            "code": "triggerflow.snapshot.invalid_value_digest",
                            "severity": "error",
                            "message": str(exc),
                            "path": path,
                        }
                    )
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    validate_projected_values(item, f"{ path }.{ key }")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    validate_projected_values(item, f"{ path }[{ index }]")

        validate_projected_values(snapshot_state, "$")
        return diagnostics

    def _snapshot_lease_diagnostics(self, snapshot_state: dict[str, Any]):
        lease = snapshot_state.get("lease")
        if not isinstance(lease, dict):
            lease = {}
        snapshot_owner_id = snapshot_state.get("owner_id")
        lease_owner_id = lease.get("owner_id", snapshot_owner_id)
        lease_until = lease.get("lease_until", snapshot_state.get("lease_until"))
        heartbeat_at = lease.get("heartbeat_at", snapshot_state.get("heartbeat_at"))
        lease_ttl = lease.get("lease_ttl", snapshot_state.get("lease_ttl"))

        diagnostics: list[dict[str, Any]] = []
        if (
            snapshot_owner_id is not None
            and lease_owner_id is not None
            and str(snapshot_owner_id) != str(lease_owner_id)
        ):
            diagnostics.append(
                {
                    "code": "triggerflow.lease.owner_mismatch",
                    "severity": "error",
                    "message": "TriggerFlow execution snapshot owner does not match snapshot lease owner.",
                    "owner_id": str(snapshot_owner_id),
                    "lease_owner_id": str(lease_owner_id),
                    "details": {
                        "lease": self._execution._to_serializable_value(lease),
                    },
                }
            )

        parsed_lease_until: float | None = None
        if lease_until is not None:
            try:
                parsed_lease_until = float(lease_until)
            except (TypeError, ValueError):
                diagnostics.append(
                    {
                        "code": "triggerflow.lease.invalid_metadata",
                        "severity": "error",
                        "message": "TriggerFlow execution snapshot lease_until must be numeric when present.",
                        "lease_until": lease_until,
                    }
                )

        current_owner_id = self._execution._owner_id
        if parsed_lease_until is not None and parsed_lease_until <= time.time():
            diagnostics.append(
                {
                    "code": "triggerflow.lease.expired",
                    "severity": "warning",
                    "message": "TriggerFlow execution snapshot lease has expired and should be reclaimed before dispatch.",
                    "owner_id": str(lease_owner_id) if lease_owner_id is not None else None,
                    "lease_until": parsed_lease_until,
                    "heartbeat_at": heartbeat_at,
                    "lease_ttl": lease_ttl,
                }
            )
        elif (
            current_owner_id is not None
            and lease_owner_id is not None
            and str(current_owner_id) != str(lease_owner_id)
        ):
            diagnostics.append(
                {
                    "code": "triggerflow.lease.owner_conflict",
                    "severity": "error",
                    "message": "TriggerFlow execution snapshot lease is still owned by another worker.",
                    "owner_id": str(current_owner_id),
                    "lease_owner_id": str(lease_owner_id),
                    "lease_until": parsed_lease_until,
                    "heartbeat_at": heartbeat_at,
                    "lease_ttl": lease_ttl,
                }
            )
        return diagnostics

    def _and_signal_gate_expectations(self):
        expectations: dict[str, dict[str, set[str]]] = {}
        for operator in self._execution._trigger_flow._blue_print.definition.operators:
            if operator.get("kind") != "signal_gate" or operator.get("options", {}).get("mode", "and") != "and":
                continue
            expected_signals: dict[str, set[str]] = {}
            for signal in operator.get("listen_signals", []):
                trigger_type = str(signal.get("trigger_type", ""))
                trigger_event = str(signal.get("trigger_event", ""))
                if not trigger_type or not trigger_event:
                    continue
                expected_signals.setdefault(trigger_type, set()).add(trigger_event)
            operator_id = str(operator.get("id", ""))
            if operator_id:
                expectations[operator_id] = expected_signals
                if operator_id.startswith("when-"):
                    expectations[operator_id.removeprefix("when-")] = expected_signals
        return expectations

    def _snapshot_when_join_diagnostics(self, snapshot_state: dict[str, Any]):
        durable_system_state = snapshot_state.get("durable_system_state", {})
        if not isinstance(durable_system_state, dict):
            return []
        when_states = durable_system_state.get("when_states")
        if when_states is None:
            return []
        if not isinstance(when_states, dict):
            return [
                {
                    "code": "triggerflow.when_join.invalid_state",
                    "severity": "error",
                    "message": "TriggerFlow execution snapshot when_states must be a mapping.",
                    "details": {"when_states": self._execution._to_serializable_value(when_states)},
                }
            ]

        expectations = self._and_signal_gate_expectations()
        diagnostics: list[dict[str, Any]] = []
        for gate_id, scopes in when_states.items():
            expected_by_type = expectations.get(str(gate_id))
            if expected_by_type is None:
                diagnostics.append(
                    {
                        "code": "triggerflow.when_join.unknown_gate",
                        "severity": "error",
                        "message": "TriggerFlow execution snapshot contains a durable join gate not present in the flow.",
                        "operator_id": str(gate_id),
                    }
                )
                continue
            if not isinstance(scopes, dict):
                diagnostics.append(
                    {
                        "code": "triggerflow.when_join.invalid_state",
                        "severity": "error",
                        "message": "TriggerFlow execution snapshot when_states gate entry must be a mapping.",
                        "operator_id": str(gate_id),
                    }
                )
                continue
            for scope_key, scoped_state in scopes.items():
                if not isinstance(scoped_state, dict):
                    diagnostics.append(
                        {
                            "code": "triggerflow.when_join.invalid_state",
                            "severity": "error",
                            "message": "TriggerFlow execution snapshot when_states scoped entry must be a mapping.",
                            "operator_id": str(gate_id),
                            "details": {"scope": str(scope_key)},
                        }
                    )
                    continue
                for trigger_type, expected_events in expected_by_type.items():
                    actual_events = scoped_state.get(trigger_type)
                    if not isinstance(actual_events, dict):
                        diagnostics.append(
                            {
                                "code": "triggerflow.when_join.missing_trigger_type",
                                "severity": "error",
                                "message": "TriggerFlow execution snapshot join state is missing an expected trigger type.",
                                "operator_id": str(gate_id),
                                "details": {
                                    "scope": str(scope_key),
                                    "trigger_type": trigger_type,
                                },
                            }
                        )
                        continue
                    missing_events = sorted(event for event in expected_events if event not in actual_events)
                    if missing_events:
                        diagnostics.append(
                            {
                                "code": "triggerflow.when_join.missing_event",
                                "severity": "error",
                                "message": "TriggerFlow execution snapshot join state is missing expected events.",
                                "operator_id": str(gate_id),
                                "details": {
                                    "scope": str(scope_key),
                                    "trigger_type": trigger_type,
                                    "missing_events": missing_events,
                                },
                            }
                        )
                    unexpected_events = sorted(str(event) for event in actual_events if str(event) not in expected_events)
                    if unexpected_events:
                        diagnostics.append(
                            {
                                "code": "triggerflow.when_join.unexpected_event",
                                "severity": "error",
                                "message": "TriggerFlow execution snapshot join state contains unexpected events.",
                                "operator_id": str(gate_id),
                                "details": {
                                    "scope": str(scope_key),
                                    "trigger_type": trigger_type,
                                    "unexpected_events": unexpected_events,
                                },
                            }
                        )
        return diagnostics

    def _task_dag_snapshot_diagnostics(self, snapshot_state: dict[str, Any]):
        runtime_data = snapshot_state.get("runtime_data", {}) or {}
        if not isinstance(runtime_data, dict):
            return []
        graph = runtime_data.get("task_dag")
        expected_fingerprint = runtime_data.get("task_dag_graph_fingerprint")
        if graph is None or expected_fingerprint is None:
            return []
        actual_fingerprint = hashlib.sha256(
            json.dumps(graph, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if actual_fingerprint == expected_fingerprint:
            return []
        return [
            {
                "code": "triggerflow.task_dag.graph_fingerprint_mismatch",
                "severity": "error",
                "message": "TaskDAG graph fingerprint does not match the execution snapshot graph payload.",
                "expected": expected_fingerprint,
                "actual": actual_fingerprint,
                "details": {
                    "graph_id": graph.get("graph_id") if isinstance(graph, dict) else None,
                },
            }
        ]

    def _snapshot_compaction_state(self, snapshot_state: dict[str, Any]):
        compaction = snapshot_state.get("compaction", {})
        if not isinstance(compaction, dict):
            compaction = {}
        return {
            "segments": self._coerce_dict_list(compaction.get("segments")),
            "retained_lineage_anchors": self._coerce_dict_list(compaction.get("retained_lineage_anchors")),
            "artifact_refs": self._coerce_dict_list(compaction.get("artifact_refs")),
            "policy": dict(compaction.get("policy", {}) or {})
            if isinstance(compaction.get("policy", {}), dict)
            else {},
            "load_policy": dict(compaction.get("load_policy", {}) or {})
            if isinstance(compaction.get("load_policy", {}), dict)
            else {},
        }

    def _coerce_dict_list(self, value: Any):
        if value is None:
            return []
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def _compaction_snapshot_diagnostics(self, snapshot_state: dict[str, Any]):
        compaction = self._snapshot_compaction_state(snapshot_state)
        diagnostics: list[dict[str, Any]] = []
        anchors_by_id = {
            str(anchor.get("anchor_id")): anchor
            for anchor in compaction["retained_lineage_anchors"]
            if anchor.get("anchor_id") is not None
        }
        for anchor_id, anchor in anchors_by_id.items():
            expected = anchor.get("fingerprint")
            actual = self._lineage_anchor_fingerprint(anchor)
            if expected and expected != actual:
                diagnostics.append(
                    {
                        "code": "triggerflow.compaction.lineage_anchor_mismatch",
                        "severity": "error",
                        "message": "Retained lineage anchor fingerprint does not match snapshot anchor data.",
                        "anchor_id": anchor_id,
                        "expected": expected,
                        "actual": actual,
                    }
                )
        for segment in compaction["segments"]:
            segment_id = str(segment.get("segment_id", ""))
            try:
                sequence_from = int(segment.get("sequence_from"))
                sequence_to = int(segment.get("sequence_to"))
            except (TypeError, ValueError):
                diagnostics.append(
                    {
                        "code": "triggerflow.compaction.invalid_segment_range",
                        "severity": "error",
                        "message": "Compaction segment has invalid sequence bounds.",
                        "segment_id": segment_id,
                    }
                )
                continue
            if sequence_to < sequence_from:
                diagnostics.append(
                    {
                        "code": "triggerflow.compaction.invalid_segment_range",
                        "severity": "error",
                        "message": "Compaction segment sequence_to is before sequence_from.",
                        "segment_id": segment_id,
                        "sequence_from": sequence_from,
                        "sequence_to": sequence_to,
                    }
                )
            for anchor_id in segment.get("retained_anchor_ids", []) or []:
                if str(anchor_id) not in anchors_by_id:
                    diagnostics.append(
                        {
                            "code": "triggerflow.compaction.missing_lineage_anchor",
                            "severity": "error",
                            "message": "Compaction segment references a retained lineage anchor that is missing.",
                            "segment_id": segment_id,
                            "anchor_id": str(anchor_id),
                        }
                    )
        for artifact_ref in compaction["artifact_refs"]:
            if not artifact_ref.get("required", True):
                continue
            status = str(artifact_ref.get("status", "available")).lower()
            ref = artifact_ref.get("ref")
            if status in {"missing", "not_found", "unavailable"} or not self._artifact_ref_has_identifier(ref):
                diagnostics.append(
                    {
                        "code": "triggerflow.compaction.missing_artifact",
                        "severity": "error",
                        "message": "Required snapshot artifact reference is missing or unresolved.",
                        "artifact_ref": self._execution._to_serializable_value(artifact_ref),
                    }
                )
        load_policy = compaction["load_policy"]
        read_limit = load_policy.get("runtime_event_read_limit")
        if read_limit is not None:
            try:
                if int(read_limit) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                diagnostics.append(
                    {
                        "code": "triggerflow.compaction.invalid_load_read_limit",
                        "severity": "error",
                        "message": "Compaction load policy runtime_event_read_limit must be non-negative.",
                        "runtime_event_read_limit": read_limit,
                    }
                )
        return diagnostics

    def _lineage_anchor_fingerprint(self, anchor: dict[str, Any]):
        material = {
            key: value
            for key, value in anchor.items()
            if key != "fingerprint"
        }
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _artifact_ref_has_identifier(self, ref: Any):
        if isinstance(ref, str):
            return bool(ref)
        if not isinstance(ref, dict):
            return ref is not None
        for key in ("record_id", "id", "uri", "path", "artifact_id"):
            value = ref.get(key)
            if isinstance(value, str) and value:
                return True
        return False

    def _resume_ledger_diagnostics(self, state: dict[str, Any]):
        snapshot_state = state
        interrupts = snapshot_state.get("interrupts", {}) or {}
        if not isinstance(interrupts, dict):
            return []
        resume_ledger = snapshot_state.get("resume_ledger")
        if not isinstance(resume_ledger, dict):
            resume_ledger = self._collect_resume_ledger(interrupts)
        diagnostics: list[dict[str, Any]] = []
        for interrupt_id, requests in resume_ledger.items():
            if not isinstance(requests, dict):
                continue
            interrupt = interrupts.get(str(interrupt_id), {})
            interrupt_status = interrupt.get("status") if isinstance(interrupt, dict) else None
            for request_id, request in requests.items():
                if not isinstance(request, dict):
                    continue
                status = request.get("status")
                if status == "accepted" and interrupt_status == "waiting":
                    diagnostics.append(
                        {
                            "code": "triggerflow.resume.accepted_not_dispatched",
                            "severity": "warning",
                            "message": "Resume request was accepted but not durably dispatched.",
                            "interrupt_id": str(interrupt_id),
                            "resume_request_id": str(request_id),
                            "details": self._execution._to_serializable_value(request),
                        }
                    )
                elif status == "dispatched" and interrupt_status == "waiting":
                    diagnostics.append(
                        {
                            "code": "triggerflow.resume.dispatched_not_completed",
                            "severity": "warning",
                            "message": "Resume request was dispatched but completion was not recorded.",
                            "interrupt_id": str(interrupt_id),
                            "resume_request_id": str(request_id),
                            "details": self._execution._to_serializable_value(request),
                        }
                    )
        return diagnostics

    def _raise_for_snapshot_contract(self, state: dict[str, Any]):
        errors = [
            diagnostic
            for diagnostic in self._snapshot_contract_diagnostics(state)
            if diagnostic.get("severity") == "error"
        ]
        if not errors:
            return
        messages = [
            str(diagnostic.get("message", diagnostic.get("code", "invalid snapshot")))
            for diagnostic in errors
        ]
        raise ValueError(
            "Can not load TriggerFlow execution snapshot. "
            + " ".join(messages)
        )

    def _available_resource_keys(self, *, runtime_resources: dict[str, Any] | None = None):
        resources = dict(self._execution.get_runtime_resources())
        if runtime_resources:
            resources.update({str(key): value for key, value in runtime_resources.items()})
        return {str(key) for key in resources.keys()}

    def _resource_requirement_resolver(self, requirement: dict[str, Any]):
        value = requirement.get("resolver")
        if isinstance(value, str) and value.strip():
            return value.strip()
        metadata = requirement.get("metadata", {})
        if isinstance(metadata, dict):
            value = metadata.get("resolver")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _resource_requirement_fail_policy(self, requirement: dict[str, Any]):
        value = requirement.get("fail_policy")
        if not isinstance(value, str):
            metadata = requirement.get("metadata", {})
            if isinstance(metadata, dict):
                value = metadata.get("fail_policy")
        normalized = str(value or "fail_closed").strip().lower()
        return "fail_open" if normalized == "fail_open" else "fail_closed"

    def _resource_requirement_health(self, requirement: dict[str, Any]):
        value = requirement.get("health")
        if value is None:
            value = requirement.get("health")
        metadata = requirement.get("metadata", {})
        if value is None and isinstance(metadata, dict):
            value = metadata.get("health", metadata.get("health"))
        return self._normalize_resource_health(value)

    def _normalize_resource_health(self, value: Any):
        if isinstance(value, dict):
            value = value.get("status", value.get("state", value.get("health")))
        if value is None:
            return "unknown"
        normalized = str(value).strip().lower()
        if normalized in {"", "unknown", "pending"}:
            return "unknown"
        if normalized in {"ok", "ready", "healthy", "resolved"}:
            return "healthy"
        if normalized in {"forbidden", "policy_forbidden", "policy-forbidden"}:
            return "policy_forbidden"
        if normalized in {"missing", "not_found", "not-found"}:
            return "missing"
        if normalized in {"unhealthy", "failed", "error", "unavailable"}:
            return "unhealthy"
        return normalized

    def _resource_health_blocks_load(self, health: str):
        return health in {"missing", "policy_forbidden", "unhealthy"}

    def _resource_policy_severity(self, fail_policy: str):
        return "warning" if fail_policy == "fail_open" else "error"

    def _resource_health_diagnostic_code(self, health: str):
        if health == "policy_forbidden":
            return "triggerflow.load.policy_forbidden_resource"
        if health == "unhealthy":
            return "triggerflow.load.unhealthy_resource"
        return "triggerflow.load.missing_resource"

    def _resource_health_diagnostic(
        self,
        *,
        resource_key: str,
        requirement: dict[str, Any],
        health: str,
        fail_policy: str,
        resolver_ref: str | None = None,
    ):
        return {
            "code": self._resource_health_diagnostic_code(health),
            "severity": self._resource_policy_severity(fail_policy),
            "message": (
                f"Resource '{ resource_key }' is { health } during TriggerFlow load."
            ),
            "resource_key": resource_key,
            "resolver": resolver_ref,
            "health": health,
            "fail_policy": fail_policy,
            "requirement": self._execution._to_serializable_value(requirement),
        }

    def _resolver_failure_diagnostic(
        self,
        *,
        resource_key: str,
        requirement: dict[str, Any],
        resolver_ref: str,
        fail_policy: str,
        error: BaseException,
        code: str,
    ):
        return {
            "code": code,
            "severity": self._resource_policy_severity(fail_policy),
            "message": (
                f"Resource '{ resource_key }' resolver '{ resolver_ref }' failed during "
                f"TriggerFlow load: { error }"
            ),
            "resource_key": resource_key,
            "resolver": resolver_ref,
            "fail_policy": fail_policy,
            "error": str(error),
            "requirement": self._execution._to_serializable_value(requirement),
        }

    def _resolver_reference_available(self, resolver_ref: str):
        try:
            self._import_resource_resolver(resolver_ref)
        except (ImportError, AttributeError, TypeError, ValueError):
            return False
        return True

    def _import_resource_resolver(self, resolver_ref: str):
        resolver_ref = str(resolver_ref).strip()
        if ":" in resolver_ref:
            module_name, attr_path = resolver_ref.split(":", 1)
        else:
            module_name, _, attr_path = resolver_ref.rpartition(".")
        if not module_name or not attr_path:
            raise ValueError(
                "TriggerFlow resource resolver must use 'package.module:function' "
                "or 'package.module.function'."
            )
        module = importlib.import_module(module_name)
        resolver: Any = module
        for attr in attr_path.split("."):
            if not attr:
                raise ValueError(f"Invalid TriggerFlow resource resolver reference: { resolver_ref }")
            resolver = getattr(resolver, attr)
        if not callable(resolver):
            raise TypeError(f"TriggerFlow resource resolver is not callable: { resolver_ref }")
        return resolver

    def _resource_resolver_context(
        self,
        *,
        state: dict[str, Any],
        snapshot_state: dict[str, Any],
        requirement: dict[str, Any],
        resource_key: str,
    ):
        return {
            "resource_key": resource_key,
            "requirement": self._execution._to_serializable_value(requirement),
            "execution_id": str(state.get("execution_id", self._execution.id)),
            "flow_name": state.get("flow_name"),
            "snapshot": self._execution._to_serializable_value(snapshot_state),
            "execution": self._execution,
        }

    def _coerce_resolver_result(self, result: Any):
        if isinstance(result, dict) and any(
            key in result
            for key in (
                "resource",
                "health",
                "status",
                "health",
                "error",
                "message",
            )
        ):
            resource = result.get("resource")
            health = self._normalize_resource_health(
                result.get("health", result.get("status", result.get("health")))
            )
            if resource is None and health in {"unknown", "healthy"}:
                health = "missing"
            return resource, health
        if result is None:
            return None, "missing"
        return result, "healthy"

    def _resource_requirement_resource_key(self, requirement: dict[str, Any]):
        kind = requirement.get("kind")
        key = str(requirement.get("key", ""))
        metadata = requirement.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        resource_key = metadata.get("resource_key")
        if isinstance(resource_key, str) and resource_key:
            return resource_key
        nested_requirement = metadata.get("requirement")
        if isinstance(nested_requirement, dict):
            nested_resource_key = nested_requirement.get("resource_key")
            if isinstance(nested_resource_key, str) and nested_resource_key:
                return nested_resource_key
        if kind == "execution_resource_requirement":
            return None
        return key or None

    def _coerce_resource_requirements(self, resource_requirements: Any):
        if resource_requirements is None:
            return []
        if not isinstance(resource_requirements, list):
            raise TypeError(
                "Can not inspect TriggerFlow execution snapshot resource requirements, "
                f"expect list/None but got: { type(resource_requirements) }"
            )
        normalized: list[dict[str, Any]] = []
        for index, requirement in enumerate(resource_requirements):
            if not isinstance(requirement, dict):
                raise TypeError(
                    "Can not inspect TriggerFlow execution snapshot resource requirement "
                    f"#{ index }, expect dictionary but got: { type(requirement) }"
                )
            normalized.append(dict(requirement))
        return normalized

    def _snapshot_resource_requirements(self, state: dict[str, Any]):
        snapshot_state = state
        resource_requirements = self._coerce_resource_requirements(
            snapshot_state.get("resource_requirements", [])
        )
        if resource_requirements:
            return resource_requirements
        legacy_requirements: list[dict[str, Any]] = []
        runtime_resource_keys = self._coerce_key_list(snapshot_state.get("resource_keys"))
        if not runtime_resource_keys:
            runtime_resource_keys = self._coerce_key_list(state.get("resource_keys", []))
        for key in runtime_resource_keys:
            legacy_requirements.append(
                {
                    "kind": "runtime_resource",
                    "key": key,
                    "required": True,
                    "source": "legacy",
                    "metadata": {},
                }
            )
        managed_resource_keys = self._coerce_key_list(snapshot_state.get("managed_resource_keys"))
        if not managed_resource_keys:
            managed_resource_keys = self._coerce_key_list(state.get("managed_resource_keys", []))
        for key in managed_resource_keys:
            legacy_requirements.append(
                {
                    "kind": "managed_execution_resource",
                    "key": key,
                    "required": True,
                    "source": "legacy",
                    "metadata": {"resource_key": key},
                }
            )
        return self._dedupe_resource_requirements(legacy_requirements)

    def _coerce_key_list(self, value: Any):
        if value is None:
            return []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item)]

    def _resolve_execution_resource_requirements(
        self,
        snapshot_state: dict[str, Any],
        *,
        execution_resources: list[ExecutionResourceRequirement] | None = None,
    ):
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_requirement(requirement: dict[str, Any]):
            if not requirement:
                return
            key = str(
                requirement.get("requirement_id")
                or requirement.get("resource_key")
                or json.dumps(requirement, sort_keys=True, default=str)
            )
            if key in seen:
                return
            seen.add(key)
            resolved.append(dict(requirement))

        for requirement in self._execution._execution_resource_requirements:
            add_requirement(dict(requirement))
        for requirement in execution_resources or []:
            add_requirement(dict(requirement))
        for resource_requirement in self._coerce_resource_requirements(
            snapshot_state.get("resource_requirements", [])
        ):
            metadata = resource_requirement.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            nested_requirement = metadata.get("requirement")
            if isinstance(nested_requirement, dict):
                add_requirement(nested_requirement)
        return cast(list[ExecutionResourceRequirement], resolved)

    def _restore_durable_system_state(self, durable_system_state: dict[str, Any]):
        execution = self._execution
        for key in DURABLE_SYSTEM_STATE_KEYS:
            execution._system_runtime_data.pop(key, None)
        for key in DURABLE_SYSTEM_STATE_KEYS:
            value = durable_system_state.get(key)
            if value is not None:
                execution._system_runtime_data.set(key, value)

    def _load_state_content(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None,
    ) -> dict[str, Any]:
        if isinstance(state, (str, Path)):
            path = Path(state)
            is_file = False
            try:
                is_file = path.exists() and path.is_file()
            except (OSError, ValueError):
                is_file = False
            if is_file:
                suffix = path.suffix.lower()
                content = path.read_text(encoding=encoding)
                if suffix in {".yaml", ".yml"}:
                    try:
                        return self._coerce_state_dict(yaml.safe_load(content))
                    except yaml.YAMLError as e:
                        raise ValueError(
                            f"Can not load TriggerFlowExecution state from YAML file '{ state }'.\nError: { e }"
                        )
                try:
                    return self._coerce_state_dict(json.loads(content))
                except JSONDecodeError as e:
                    raise ValueError(
                        f"Can not load TriggerFlowExecution state from JSON file '{ state }'.\nError: { e }"
                    )
            if isinstance(state, str):
                original = state
                try:
                    return self._coerce_state_dict(json.loads(state))
                except JSONDecodeError:
                    try:
                        return self._coerce_state_dict(yaml.safe_load(state))
                    except yaml.YAMLError as e:
                        raise ValueError(
                            "Can not load TriggerFlowExecution state from JSON/YAML content."
                            f"\nError: { e }\nContent: { original }"
                        )
            raise TypeError(
                f"Can not load TriggerFlowExecution state, expect dictionary/string/path but got: { type(state) }"
            )

        return self._coerce_state_dict(state)

    def _coerce_state_dict(self, state: Any) -> dict[str, Any]:
        if state is None:
            raise TypeError("Can not load TriggerFlowExecution state, got None.")

        if not isinstance(state, dict):
            raise TypeError(f"Can not load TriggerFlowExecution state, expect dictionary but got: { type(state) }")
        return state

    def _validate_state_sections(self, state: dict[str, Any]):
        snapshot_state = state
        runtime_data = snapshot_state.get("runtime_data", {})
        if not isinstance(runtime_data, dict):
            raise TypeError(f"Can not load key 'runtime_data', expect dictionary but got: { type(runtime_data) }")

        flow_data = snapshot_state.get("flow_data", {})
        if not isinstance(flow_data, dict):
            raise TypeError(f"Can not load key 'flow_data', expect dictionary but got: { type(flow_data) }")

        interrupts = snapshot_state.get("interrupts", {})
        if not isinstance(interrupts, dict):
            raise TypeError(f"Can not load key 'interrupts', expect dictionary but got: { type(interrupts) }")

        intervention_state = snapshot_state.get("intervention", {})
        if intervention_state is None:
            intervention_state = {}
        if not isinstance(intervention_state, dict):
            raise TypeError(
                f"Can not load key 'intervention', expect dictionary/None but got: { type(intervention_state) }"
            )
        interventions = intervention_state.get("ledger", runtime_data.get(INTERVENTIONS_STATE_KEY, {}))
        if interventions is None:
            interventions = {}
        if not isinstance(interventions, dict):
            raise TypeError(
                f"Can not load key 'intervention.ledger', expect dictionary but got: { type(interventions) }"
            )

        sub_flow_frames = snapshot_state.get("sub_flow_frames", {})
        if not isinstance(sub_flow_frames, dict):
            raise TypeError(f"Can not load key 'sub_flow_frames', expect dictionary but got: { type(sub_flow_frames) }")

        last_signal_state = snapshot_state.get("last_signal", None)
        if last_signal_state is not None and not isinstance(last_signal_state, dict):
            raise TypeError(
                f"Can not load key 'last_signal', expect dictionary/None but got: { type(last_signal_state) }"
            )

        result_state = snapshot_state.get("result", {})
        if not isinstance(result_state, dict):
            raise TypeError(f"Can not load key 'result', expect dictionary but got: { type(result_state) }")

        durable_system_state = snapshot_state.get("durable_system_state", {})
        if durable_system_state is None:
            durable_system_state = {}
        if not isinstance(durable_system_state, dict):
            raise TypeError(
                "Can not load key 'durable_system_state', "
                f"expect dictionary/None but got: { type(durable_system_state) }"
            )
        resource_requirements = snapshot_state.get("resource_requirements", [])
        if resource_requirements is None:
            resource_requirements = []
        if not isinstance(resource_requirements, list):
            raise TypeError(
                "Can not load key 'resource_requirements', "
                f"expect list/None but got: { type(resource_requirements) }"
            )

        execution_id = snapshot_state.get("execution_id", self._execution.id)
        if not isinstance(execution_id, str):
            raise TypeError(f"Can not load key 'execution_id', expect string but got: { type(execution_id) }")

        run_context_state = snapshot_state.get("run_context", None)
        if run_context_state is not None and not isinstance(run_context_state, dict):
            raise TypeError(
                f"Can not load key 'run_context', expect dictionary/None but got: { type(run_context_state) }"
            )

        snapshot_retention_policy = snapshot_state.get("snapshot_retention_policy")
        if snapshot_retention_policy is not None:
            self._execution._normalize_snapshot_retention_policy(snapshot_retention_policy)
