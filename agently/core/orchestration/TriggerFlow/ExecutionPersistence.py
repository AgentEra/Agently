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
import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any, TYPE_CHECKING, cast

import yaml

from agently.types.data import EMPTY, RunContext
from agently.types.trigger_flow.runtime_keys import DURABLE_SYSTEM_STATE_KEYS, TRIGGER_FLOW_CHECKPOINT_SCHEMA_VERSION
from .Control import (
    TRIGGER_FLOW_LIFECYCLE_CLOSED,
    TRIGGER_FLOW_LIFECYCLE_OPEN,
    TRIGGER_FLOW_LIFECYCLE_SEALED,
    TRIGGER_FLOW_STATUS_COMPLETED,
    TRIGGER_FLOW_STATUS_CREATED,
    TRIGGER_FLOW_STATUS_FAILED,
)
from .ExecutionState import INTERVENTIONS_STATE_KEY, TriggerFlowInterventionMode

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
        result = execution._system_runtime_data.get("result")
        result_ready = result is not EMPTY
        durable_system_state = self._collect_durable_system_state()
        resource_keys = sorted(str(key) for key in execution.get_runtime_resources().keys())
        managed_resource_keys = sorted(
            str(handle.get("resource_key", ""))
            for handle in execution._managed_execution_environment_handles
        )
        execution_environment_requirement_ids = sorted(
            str(requirement.get("requirement_id", ""))
            for requirement in execution._execution_environment_requirements
            if requirement.get("requirement_id")
        )
        state = {
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
            "run_context": execution.run_context.model_dump(mode="json"),
            "runtime_data": json.loads(execution._runtime_data.dump("json")),
            "flow_data": json.loads(execution._trigger_flow._flow_data.dump("json")),
            "interrupts": execution._to_serializable_value(execution._get_interrupts()),
            "intervention": {
                "mode": execution._intervention_mode,
                "policy": execution._intervention_policy_name,
                "version": execution._interventions_version,
                "ledger": execution._to_serializable_value(execution._get_intervention_records()),
            },
            "sub_flow_frames": execution._to_serializable_value(execution._get_sub_flow_frames()),
            "last_signal": execution._serialize_signal(execution.get_last_signal()),
            "resource_keys": resource_keys,
            "managed_resource_keys": managed_resource_keys,
            "execution_environment_requirement_ids": execution_environment_requirement_ids,
            "checkpoint": self._build_checkpoint_section(
                durable_system_state=durable_system_state,
                resource_keys=resource_keys,
                managed_resource_keys=managed_resource_keys,
                execution_environment_requirement_ids=execution_environment_requirement_ids,
            ),
            "result": {
                "ready": result_ready,
                "value": execution._to_serializable_value(result) if result_ready else None,
            },
        }
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

    def _build_checkpoint_section(
        self,
        *,
        durable_system_state: dict[str, Any],
        resource_keys: list[str],
        managed_resource_keys: list[str],
        execution_environment_requirement_ids: list[str],
    ):
        execution = self._execution
        resource_requirements: list[dict[str, Any]] = [
            {
                "kind": "runtime_resource",
                "key": key,
                "required": True,
            }
            for key in resource_keys
        ]
        resource_requirements.extend(
            {
                "kind": "managed_execution_environment",
                "key": key,
                "required": True,
            }
            for key in managed_resource_keys
            if key
        )
        resource_requirements.extend(
            {
                "kind": "execution_environment_requirement",
                "key": requirement_id,
                "required": True,
            }
            for requirement_id in execution_environment_requirement_ids
        )
        return {
            "schema_version": TRIGGER_FLOW_CHECKPOINT_SCHEMA_VERSION,
            "kind": "triggerflow.execution_snapshot",
            "execution_id": execution.id,
            "flow_name": execution._trigger_flow.name,
            "status": execution._status,
            "lifecycle_state": execution._lifecycle_state,
            "state_version": execution._state_version,
            "durable_system_state": durable_system_state,
            "resource_requirements": resource_requirements,
        }

    def load(
        self,
        state: dict[str, Any] | str | Path,
        *,
        encoding: str | None = "utf-8",
        runtime_resources: dict[str, Any] | None = None,
    ):
        execution = self._execution
        state = self._load_state_content(state, encoding=encoding)
        self._validate_state_sections(state)

        runtime_data = state.get("runtime_data", {})
        flow_data = state.get("flow_data", {})
        interrupts = state.get("interrupts", {})
        intervention_state = state.get("intervention", {}) or {}
        interventions = intervention_state.get("ledger", runtime_data.get(INTERVENTIONS_STATE_KEY, {}))
        if interventions is None:
            interventions = {}
        checkpoint_state = state.get("checkpoint", {}) or {}
        durable_system_state = checkpoint_state.get("durable_system_state", {})
        if durable_system_state is None:
            durable_system_state = {}
        sub_flow_frames = state.get("sub_flow_frames", {})
        last_signal_state = state.get("last_signal", None)
        result_state = state.get("result", {})
        execution_id = state.get("execution_id", execution.id)
        run_context_state = state.get("run_context", None)

        ready = bool(result_state.get("ready", False))
        result_value = result_state.get("value")
        status = str(state.get("status", TRIGGER_FLOW_STATUS_CREATED))
        lifecycle_state = str(state.get("lifecycle_state", TRIGGER_FLOW_LIFECYCLE_OPEN))
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
        self._restore_durable_system_state(durable_system_state)
        execution._set_status(status)
        execution._auto_close = bool(state.get("auto_close", execution._auto_close))
        execution._auto_close_timeout = state.get("auto_close_timeout", execution._auto_close_timeout)
        execution._lifecycle_state = lifecycle_state
        execution._system_runtime_data.set("lifecycle_state", lifecycle_state)
        execution._created_at = float(state.get("created_at", execution._created_at) or execution._created_at)
        execution._started_at = state.get("started_at", execution._started_at)
        execution._last_activity_at = state.get("last_activity_at", execution._last_activity_at)
        execution._sealed_at = state.get("sealed_at", execution._sealed_at)
        execution._closed_at = state.get("closed_at", execution._closed_at)
        execution._close_reason = state.get("close_reason", execution._close_reason)
        execution._state_version = int(state.get("state_version", execution._state_version))
        execution._system_runtime_data.set("state_version", execution._state_version)
        execution._owner_id = state.get("owner_id", execution._owner_id)
        execution._lease_ttl = state.get("lease_ttl", execution._lease_ttl)
        execution._lease_until = state.get("lease_until", execution._lease_until)
        execution._heartbeat_at = state.get("heartbeat_at", execution._heartbeat_at)
        execution._runtime_stream_stopped = lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED
        if lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
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
        runtime_data = state.get("runtime_data", {})
        if not isinstance(runtime_data, dict):
            raise TypeError(f"Can not load key 'runtime_data', expect dictionary but got: { type(runtime_data) }")

        flow_data = state.get("flow_data", {})
        if not isinstance(flow_data, dict):
            raise TypeError(f"Can not load key 'flow_data', expect dictionary but got: { type(flow_data) }")

        interrupts = state.get("interrupts", {})
        if not isinstance(interrupts, dict):
            raise TypeError(f"Can not load key 'interrupts', expect dictionary but got: { type(interrupts) }")

        intervention_state = state.get("intervention", {})
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

        sub_flow_frames = state.get("sub_flow_frames", {})
        if not isinstance(sub_flow_frames, dict):
            raise TypeError(f"Can not load key 'sub_flow_frames', expect dictionary but got: { type(sub_flow_frames) }")

        last_signal_state = state.get("last_signal", None)
        if last_signal_state is not None and not isinstance(last_signal_state, dict):
            raise TypeError(
                f"Can not load key 'last_signal', expect dictionary/None but got: { type(last_signal_state) }"
            )

        result_state = state.get("result", {})
        if not isinstance(result_state, dict):
            raise TypeError(f"Can not load key 'result', expect dictionary but got: { type(result_state) }")

        checkpoint_state = state.get("checkpoint", {})
        if checkpoint_state is None:
            checkpoint_state = {}
        if not isinstance(checkpoint_state, dict):
            raise TypeError(
                f"Can not load key 'checkpoint', expect dictionary/None but got: { type(checkpoint_state) }"
            )
        durable_system_state = checkpoint_state.get("durable_system_state", {})
        if durable_system_state is None:
            durable_system_state = {}
        if not isinstance(durable_system_state, dict):
            raise TypeError(
                "Can not load key 'checkpoint.durable_system_state', "
                f"expect dictionary/None but got: { type(durable_system_state) }"
            )
        resource_requirements = checkpoint_state.get("resource_requirements", [])
        if resource_requirements is None:
            resource_requirements = []
        if not isinstance(resource_requirements, list):
            raise TypeError(
                "Can not load key 'checkpoint.resource_requirements', "
                f"expect list/None but got: { type(resource_requirements) }"
            )

        execution_id = state.get("execution_id", self._execution.id)
        if not isinstance(execution_id, str):
            raise TypeError(f"Can not load key 'execution_id', expect string but got: { type(execution_id) }")

        run_context_state = state.get("run_context", None)
        if run_context_state is not None and not isinstance(run_context_state, dict):
            raise TypeError(
                f"Can not load key 'run_context', expect dictionary/None but got: { type(run_context_state) }"
            )
