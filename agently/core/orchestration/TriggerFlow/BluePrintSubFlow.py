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
import contextlib
import copy
import time
import uuid
from collections.abc import Mapping
from typing import Any, Literal, TYPE_CHECKING, Sequence, cast

from agently.core.runtime.RuntimeContext import resolve_parent_run_context
from agently.types.data import EMPTY, SerializableMapping
from .Control import (
    TRIGGER_FLOW_STATUS_CANCELLED,
    TRIGGER_FLOW_STATUS_FAILED,
    TRIGGER_FLOW_STATUS_WAITING,
    TriggerFlowPauseSignal,
)
from .Execution import TriggerFlowExecution
from .SubFlowBindings import (
    _CAPTURE_SOURCE_SCOPES,
    _CAPTURE_TARGET_SCOPES,
    _CompiledSubFlowBinding,
    _ParentSubFlowCaptureSource,
    _SUB_FLOW_PATH_SEGMENT_PATTERN,
    _SubFlowCaptureTarget,
    _SubFlowWriteBackSource,
    _SubFlowWriteBackTarget,
    _WRITE_BACK_SOURCE_SCOPES,
    _WRITE_BACK_TARGET_SCOPES,
    _clone_sub_flow_value,
)

if TYPE_CHECKING:
    from agently.types.trigger_flow import (
        TriggerFlowPathReadable,
        TriggerFlowPathWritable,
        TriggerFlowSubFlowCapture,
        TriggerFlowSubFlowWriteBack,
    )
    from .BluePrint import TriggerFlowBlueprint
    from .TriggerFlow import TriggerFlow


class TriggerFlowBlueprintSubFlow:
    def __init__(self, blueprint: "TriggerFlowBlueprint"):
        self._blueprint = blueprint

    def parse_relative_path(self, path: str, *, option_name: str):
        if not isinstance(path, str):
            raise TypeError(
                f"TriggerFlow sub flow { option_name } target path must be a string, got: { type(path) }."
            )
        if path == "":
            raise ValueError(f"TriggerFlow sub flow { option_name } target path can not be empty.")
        segments = tuple(path.split("."))
        for segment in segments:
            if not _SUB_FLOW_PATH_SEGMENT_PATTERN.fullmatch(segment):
                raise ValueError(
                    f"TriggerFlow sub flow { option_name } target path '{ path }' contains invalid segment '{ segment }'."
                )
        return segments

    def parse_source_path(
        self,
        path: str,
        *,
        mode: Literal["capture", "write_back"],
        target_scope: str,
    ):
        if not isinstance(path, str):
            raise TypeError(
                f"TriggerFlow sub flow { mode } source path for target scope '{ target_scope }' "
                f"must be a string, got: { type(path) }."
            )
        if path == "":
            raise ValueError(
                f"TriggerFlow sub flow { mode } source path for target scope '{ target_scope }' can not be empty."
            )
        segments = tuple(path.split("."))
        root_scope = segments[0]
        allowed_source_scopes = _CAPTURE_SOURCE_SCOPES if mode == "capture" else _WRITE_BACK_SOURCE_SCOPES
        if root_scope not in allowed_source_scopes:
            raise ValueError(
                f"TriggerFlow sub flow { mode } source scope '{ root_scope }' is not supported. "
                f"Allowed scopes: { sorted(allowed_source_scopes) }"
            )
        for segment in segments[1:]:
            if not _SUB_FLOW_PATH_SEGMENT_PATTERN.fullmatch(segment):
                raise ValueError(
                    f"TriggerFlow sub flow { mode } source path '{ path }' contains invalid segment '{ segment }'."
                )
        return root_scope, segments[1:]

    def normalize_scope_binding(
        self,
        binding: Any,
        *,
        mode: Literal["capture", "write_back"],
        scope: str,
    ):
        if isinstance(binding, str):
            if scope not in {"input", "value"}:
                raise TypeError(
                    f"TriggerFlow sub flow { mode } scope '{ scope }' only accepts key-path mappings."
                )
            return binding

        if not isinstance(binding, Mapping):
            raise TypeError(
                f"TriggerFlow sub flow { mode } scope '{ scope }' expects a string or mapping, got: { type(binding) }."
            )

        normalized_binding: dict[str, str] = {}
        for target_path, source_path in binding.items():
            if not isinstance(target_path, str):
                raise TypeError(
                    f"TriggerFlow sub flow { mode } target path for scope '{ scope }' must be a string, "
                    f"got: { type(target_path) }."
                )
            if not isinstance(source_path, str):
                raise TypeError(
                    f"TriggerFlow sub flow { mode } source path for scope '{ scope }' must be a string, "
                    f"got: { type(source_path) }."
                )
            normalized_binding[str(target_path)] = str(source_path)
        return normalized_binding

    def normalize_spec(
        self,
        spec: "TriggerFlowSubFlowCapture | TriggerFlowSubFlowWriteBack | None",
        *,
        mode: Literal["capture", "write_back"],
    ):
        if spec is None:
            return None
        if not isinstance(spec, Mapping):
            raise TypeError(f"TriggerFlow sub flow { mode } spec must be a mapping, got: { type(spec) }.")

        allowed_target_scopes = _CAPTURE_TARGET_SCOPES if mode == "capture" else _WRITE_BACK_TARGET_SCOPES
        normalized_spec: dict[str, str | dict[str, str]] = {}
        for target_scope, binding in spec.items():
            if not isinstance(target_scope, str):
                raise TypeError(
                    f"TriggerFlow sub flow { mode } target scope must be a string, got: { type(target_scope) }."
                )
            if target_scope not in allowed_target_scopes:
                raise ValueError(
                    f"TriggerFlow sub flow { mode } target scope '{ target_scope }' is not supported. "
                    f"Allowed scopes: { sorted(allowed_target_scopes) }"
                )
            normalized_spec[target_scope] = self.normalize_scope_binding(
                binding,
                mode=mode,
                scope=target_scope,
            )
        return normalized_spec

    def validate_target_conflicts(
        self,
        target_paths: list[tuple[str, ...]],
        *,
        mode: Literal["capture", "write_back"],
        scope: str,
    ):
        seen_paths: set[tuple[str, ...]] = set()
        for target_path in sorted(target_paths, key=len):
            if target_path in seen_paths:
                raise ValueError(
                    f"TriggerFlow sub flow { mode } target path '{ scope }.{ '.'.join(target_path) }' is duplicated."
                )
            for depth in range(1, len(target_path)):
                if target_path[:depth] in seen_paths:
                    raise ValueError(
                        f"TriggerFlow sub flow { mode } target paths conflict under scope '{ scope }': "
                        f"'{ scope }.{ '.'.join(target_path[:depth]) }' and '{ scope }.{ '.'.join(target_path) }'."
                    )
            seen_paths.add(target_path)

    def compile_bindings(
        self,
        spec: "TriggerFlowSubFlowCapture | TriggerFlowSubFlowWriteBack | None",
        *,
        mode: Literal["capture", "write_back"],
    ):
        normalized_spec = self.normalize_spec(spec, mode=mode)
        compiled_bindings: list[_CompiledSubFlowBinding] = []

        if normalized_spec is None:
            default_target_scope = "input" if mode == "capture" else "value"
            default_source_path = "value" if mode == "capture" else "result"
            source_scope, source_path = self.parse_source_path(
                default_source_path,
                mode=mode,
                target_scope=default_target_scope,
            )
            compiled_bindings.append(
                _CompiledSubFlowBinding(
                    target_scope=default_target_scope,
                    target_path=tuple(),
                    source_scope=cast(
                        Literal["value", "runtime_data", "flow_data", "resources", "result"],
                        source_scope,
                    ),
                    source_path=tuple(source_path),
                )
            )
            return normalized_spec, compiled_bindings

        for target_scope, binding in normalized_spec.items():
            if isinstance(binding, str):
                source_scope, source_path = self.parse_source_path(
                    binding,
                    mode=mode,
                    target_scope=target_scope,
                )
                compiled_bindings.append(
                    _CompiledSubFlowBinding(
                        target_scope=cast(
                            Literal["input", "runtime_data", "flow_data", "resources", "value"],
                            target_scope,
                        ),
                        target_path=tuple(),
                        source_scope=cast(
                            Literal["value", "runtime_data", "flow_data", "resources", "result"],
                            source_scope,
                        ),
                        source_path=tuple(source_path),
                    )
                )
                continue

            target_paths: list[tuple[str, ...]] = []
            for target_path_str, source_path_str in binding.items():
                target_path = self.parse_relative_path(
                    target_path_str,
                    option_name=f"{ mode }.{ target_scope }",
                )
                source_scope, source_path = self.parse_source_path(
                    source_path_str,
                    mode=mode,
                    target_scope=target_scope,
                )
                target_paths.append(target_path)
                compiled_bindings.append(
                    _CompiledSubFlowBinding(
                        target_scope=cast(
                            Literal["input", "runtime_data", "flow_data", "resources", "value"],
                            target_scope,
                        ),
                        target_path=target_path,
                        source_scope=cast(
                            Literal["value", "runtime_data", "flow_data", "resources", "result"],
                            source_scope,
                        ),
                        source_path=tuple(source_path),
                    )
                )
            self.validate_target_conflicts(
                target_paths,
                mode=mode,
                scope=target_scope,
            )

        return normalized_spec, compiled_bindings

    def apply_bindings(
        self,
        bindings: Sequence[_CompiledSubFlowBinding],
        *,
        source: "TriggerFlowPathReadable",
        target: "TriggerFlowPathWritable",
    ):
        for binding in bindings:
            value = source.read_path(binding.source_scope, binding.source_path)
            target.write_path(binding.target_scope, binding.target_path, value)

    @staticmethod
    def is_resource_identity_binding(binding: _CompiledSubFlowBinding) -> bool:
        return (
            binding.target_scope == "resources"
            and binding.source_scope == "resources"
            and bool(binding.target_path)
            and bool(binding.source_path)
        )

    def instantiate_isolated_sub_flow(self, trigger_flow: "TriggerFlow"):
        from .TriggerFlow import TriggerFlow

        isolated_sub_flow = TriggerFlow(
            blueprint=trigger_flow.save_blueprint(),
            name=trigger_flow.name,
            skip_exceptions=trigger_flow._skip_exceptions,
        )
        settings_snapshot = copy.deepcopy(trigger_flow.settings.get(None, {}, inherit=False))
        if not isinstance(settings_snapshot, Mapping):
            raise TypeError(
                f"TriggerFlow settings snapshot must be a mapping, got: { type(settings_snapshot) }."
            )
        isolated_sub_flow.settings.update(cast(SerializableMapping, settings_snapshot))
        isolated_sub_flow._flow_data.update(
            copy.deepcopy(trigger_flow._flow_data.get(None, {}, inherit=False))
        )
        isolated_sub_flow._runtime_resources.update(
            dict(trigger_flow._runtime_resources.get(None, {}, inherit=False))
        )
        return isolated_sub_flow

    async def bridge_runtime_stream(
        self,
        child_execution: TriggerFlowExecution,
        parent_execution: TriggerFlowExecution,
    ):
        subscription = child_execution._runtime_stream_transport.subscribe(
            start="earliest",
            timeout=None,
        )
        try:
            async for stream_item in subscription:
                await parent_execution.async_put_into_stream(stream_item)
        finally:
            await subscription.async_close()

    def build_resource_bindings(
        self,
        capture_bindings: Sequence[_CompiledSubFlowBinding],
    ):
        bindings: dict[str, str] = {}
        for binding in capture_bindings:
            if not self.is_resource_identity_binding(binding):
                continue
            target_key = ".".join(binding.target_path)
            source_key = ".".join(binding.source_path)
            bindings[target_key] = source_key
        return bindings

    def apply_resource_bindings(
        self,
        child_execution: TriggerFlowExecution,
        parent_execution: TriggerFlowExecution,
        resource_bindings: Mapping[str, str],
    ):
        for target_key, source_key in resource_bindings.items():
            child_execution.set_runtime_resource(
                str(target_key),
                parent_execution.require_runtime_resource(str(source_key)),
            )

    def restore_frame_resources(
        self,
        child_execution: TriggerFlowExecution,
        parent_execution: TriggerFlowExecution,
        frame: dict[str, Any],
    ):
        resource_bindings = frame.get("resource_bindings", {})
        if isinstance(resource_bindings, Mapping):
            self.apply_resource_bindings(
                child_execution,
                parent_execution,
                resource_bindings,
            )
        for resource_key in frame.get("resource_keys", []):
            key = str(resource_key)
            if child_execution.get_runtime_resource(key, EMPTY) is not EMPTY:
                continue
            child_execution.set_runtime_resource(
                key,
                parent_execution.require_runtime_resource(key),
            )

    def make_frame_id(self, parent_execution: TriggerFlowExecution, operator_id: str):
        return f"{ parent_execution.id }:{ operator_id }:{ uuid.uuid4().hex }"

    def build_active_frame(
        self,
        *,
        parent_execution: TriggerFlowExecution,
        child_execution: TriggerFlowExecution,
        child_flow_name: str,
        operator: dict[str, Any],
        data: Any,
        resource_bindings: Mapping[str, str],
    ):
        now = time.time()
        return {
            "frame_id": self.make_frame_id(parent_execution, operator["id"]),
            "status": "running",
            "parent_execution_id": parent_execution.id,
            "parent_operator_id": operator["id"],
            "child_execution_id": child_execution.id,
            "child_flow_name": child_flow_name,
            "created_at": now,
            "started_at": now,
            "parent_signal": parent_execution._serialize_signal(data.signal),
            "parent_layer_marks": data._layer_marks.copy(),
            "parent_value": _clone_sub_flow_value(data.value),
            "projected_interrupts": {},
            "resource_bindings": dict(resource_bindings),
            "resource_keys": sorted(str(key) for key in child_execution.get_runtime_resources().keys()),
        }

    def error_summary(self, error: BaseException):
        return {
            "type": type(error).__name__,
            "message": str(error),
        }

    async def finalize_cancelled_frame(
        self,
        *,
        parent_execution: TriggerFlowExecution,
        child_execution: TriggerFlowExecution,
        frame_id: str,
        reason: str,
    ):
        child_execution._set_status(TRIGGER_FLOW_STATUS_CANCELLED)
        cleanup_error: BaseException | None = None
        try:
            await child_execution.async_close(
                reason="sub_flow_cancelled",
                timeout=0,
                pending_interrupts="cancel",
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            cleanup_error = error

        transitioned = False
        async with parent_execution._sub_flow_control_lock:
            stored_frame = parent_execution._get_sub_flow_frames().get(frame_id)
            if not isinstance(stored_frame, dict):
                return None
            if stored_frame.get("status") in {"completed", "failed"}:
                return copy.deepcopy(stored_frame)
            frame = copy.deepcopy(stored_frame)
            if frame.get("status") != "cancelled":
                transitioned = True
            frame["status"] = "cancelled"
            frame["cancel_reason"] = str(reason)
            frame.setdefault("cancel_requested_at", time.time())
            frame["cancelled_at"] = time.time()
            frame["child_saved_state"] = child_execution.save()
            if cleanup_error is not None:
                frame["cleanup_error"] = self.error_summary(cleanup_error)
            parent_execution._set_sub_flow_frame(frame_id, frame)

        if transitioned:
            await parent_execution._emit_runtime_event(
                "triggerflow.sub_flow_cancelled",
                level="WARNING",
                message=(
                    f"TriggerFlow execution '{ parent_execution.id }' cancelled "
                    f"sub-flow frame '{ frame_id }'."
                ),
                payload=parent_execution._sub_flow_event_payload(frame, reason=str(reason)),
            )
        return frame

    async def fail_frame(
        self,
        *,
        parent_execution: TriggerFlowExecution,
        child_execution: TriggerFlowExecution,
        frame_id: str,
        error: BaseException,
    ):
        child_execution._set_status(TRIGGER_FLOW_STATUS_FAILED)
        async with parent_execution._sub_flow_control_lock:
            stored_frame = parent_execution._get_sub_flow_frames().get(frame_id)
            if not isinstance(stored_frame, dict):
                return None
            if stored_frame.get("status") in {"cancel_requested", "cancelled"}:
                return copy.deepcopy(stored_frame)
            frame = copy.deepcopy(stored_frame)
            frame["status"] = "failed"
            frame["failed_at"] = time.time()
            frame["error"] = self.error_summary(error)
            frame["child_saved_state"] = child_execution.save()
            parent_execution._set_sub_flow_frame(frame_id, frame)
        await parent_execution._emit_runtime_event(
            "triggerflow.sub_flow_failed",
            level="ERROR",
            message=(
                f"TriggerFlow execution '{ parent_execution.id }' sub-flow frame "
                f"'{ frame_id }' failed."
            ),
            payload=parent_execution._sub_flow_event_payload(
                frame,
                error=self.error_summary(error),
            ),
            error=error,
        )
        return frame

    def build_parent_data(
        self,
        parent_execution: TriggerFlowExecution,
        frame: dict[str, Any],
        operator: dict[str, Any],
    ):
        from agently.types.trigger_flow import TriggerFlowRuntimeData

        parent_signal = parent_execution._restore_signal(frame.get("parent_signal"))
        if parent_signal is None:
            parent_signal = parent_execution._build_signal(
                "START",
                frame.get("parent_value"),
                trigger_type="event",
                source="sub_flow",
            )
        chunk_run_context = parent_execution._create_chunk_run_context(operator, parent_signal)
        return TriggerFlowRuntimeData(
            trigger_event=parent_signal.trigger_event,
            trigger_type=parent_signal.trigger_type,
            value=frame.get("parent_value"),
            execution=parent_execution,
            _layer_marks=list(frame.get("parent_layer_marks", [])),
            signal=parent_signal,
            chunk_run_context=chunk_run_context,
        )

    async def project_child_interrupts(
        self,
        *,
        parent_execution: TriggerFlowExecution,
        child_execution: TriggerFlowExecution,
        frame: dict[str, Any],
    ):
        pending_child_interrupts = child_execution.get_pending_interrupts()
        projected_interrupts = dict(frame.get("projected_interrupts", {}))
        parent_interrupts = parent_execution._get_interrupts().copy()
        projected_root_ids: list[str] = []

        for child_interrupt_id, child_interrupt in pending_child_interrupts.items():
            root_interrupt_id = None
            for stored_root_id, stored_child_id in projected_interrupts.items():
                if stored_child_id == child_interrupt_id:
                    root_interrupt_id = stored_root_id
                    break
            if root_interrupt_id is None:
                root_interrupt_id = f"{ frame['frame_id'] }:{ child_interrupt_id }"
                projected_interrupts[root_interrupt_id] = child_interrupt_id

            root_interrupt = copy.deepcopy(child_interrupt)
            root_interrupt.update(
                {
                    "id": root_interrupt_id,
                    "local_interrupt_id": child_interrupt_id,
                    "status": "waiting",
                    "source_execution_id": child_execution.id,
                    "source_flow_name": child_execution._trigger_flow.name,
                    "sub_flow_frame_id": frame["frame_id"],
                    "child_interrupt": copy.deepcopy(child_interrupt),
                }
            )
            parent_interrupts[root_interrupt_id] = root_interrupt
            projected_root_ids.append(root_interrupt_id)

            await parent_execution.async_put_into_stream(
                {
                    "type": "interrupt",
                    "action": "project",
                    "execution_id": parent_execution.id,
                    "interrupt": parent_execution._to_serializable_value(root_interrupt),
                    "value": parent_execution._to_serializable_value(child_interrupt.get("payload")),
                },
                _skip_contract_validation=True,
            )

        frame["status"] = "waiting"
        frame["child_saved_state"] = child_execution.save()
        frame["projected_interrupts"] = projected_interrupts
        frame["resource_keys"] = sorted(str(key) for key in child_execution.get_runtime_resources().keys())
        frame["projected_root_interrupt_ids"] = projected_root_ids
        parent_execution._system_runtime_data.set("interrupts", parent_interrupts)
        parent_execution._set_sub_flow_frame(frame["frame_id"], frame)
        parent_execution._set_status(TRIGGER_FLOW_STATUS_WAITING)
        return projected_root_ids

    async def complete_frame(
        self,
        *,
        parent_execution: TriggerFlowExecution,
        child_execution: TriggerFlowExecution,
        frame: dict[str, Any],
        operator: dict[str, Any],
        normalized_write_back: Any,
        write_back_bindings: Sequence[_CompiledSubFlowBinding],
    ):
        result = await child_execution.async_close(reason="sub_flow_completed")
        data = self.build_parent_data(parent_execution, frame, operator)
        frame_id = str(frame["frame_id"])
        cancellation_reason: str | None = None
        async with parent_execution._sub_flow_control_lock:
            stored_frame = parent_execution._get_sub_flow_frames().get(frame_id)
            if not isinstance(stored_frame, dict):
                raise RuntimeError(
                    f"TriggerFlow sub flow frame '{ frame_id }' disappeared before completion."
                )
            if stored_frame.get("status") in {"cancel_requested", "cancelled"}:
                cancellation_reason = str(stored_frame.get("cancel_reason") or "cancelled")
            else:
                if normalized_write_back is None:
                    if isinstance(result, dict) and "$final_result" in result:
                        data.value = result["$final_result"]
                    else:
                        data.value = result
                else:
                    write_back_target = _SubFlowWriteBackTarget(data.value)
                    self.apply_bindings(
                        write_back_bindings,
                        source=_SubFlowWriteBackSource(result),
                        target=write_back_target,
                    )
                    write_back_target.apply(data)

                frame = copy.deepcopy(stored_frame)
                frame["status"] = "completed"
                frame["completed_at"] = time.time()
                frame["child_saved_state"] = child_execution.save()
                frame["result"] = parent_execution._to_serializable_value(result)
                parent_execution._set_sub_flow_frame(frame_id, frame)

        if cancellation_reason is not None:
            await self.finalize_cancelled_frame(
                parent_execution=parent_execution,
                child_execution=child_execution,
                frame_id=frame_id,
                reason=cancellation_reason,
            )
            return None

        await parent_execution._emit_runtime_event(
            "triggerflow.sub_flow_completed",
            message=(
                f"TriggerFlow execution '{ parent_execution.id }' completed "
                f"sub-flow frame '{ frame_id }'."
            ),
            payload=parent_execution._sub_flow_event_payload(frame),
        )

        emit_signal = operator["emit_signals"][0]
        await data.async_emit(
            emit_signal["trigger_event"],
            data.value,
            _layer_marks=data._layer_marks.copy(),
        )
        parent_execution._refresh_waiting_status()
        return result

    def build_from_operator(self, operator: dict[str, Any]):
        from .TriggerFlow import TriggerFlow

        blueprint = self._blueprint
        options = operator.get("options", {})
        sub_flow_config = options.get("sub_flow_config")
        if not isinstance(sub_flow_config, dict):
            raise TypeError(
                f"TriggerFlow sub flow operator '{ operator['id'] }' missing valid 'sub_flow_config'."
            )

        sub_blueprint = type(blueprint)(
            name=str(
                sub_flow_config.get("name")
                or options.get("sub_flow_name")
                or operator.get("name")
                or f"SubFlow-{ operator['id'] }"
            )
        )
        sub_blueprint._chunk_registry = blueprint._chunk_registry.copy()
        sub_blueprint._condition_registry = blueprint._condition_registry.copy()
        sub_blueprint.load_flow_config(copy.deepcopy(sub_flow_config))
        return TriggerFlow(
            blueprint=sub_blueprint,
            name=sub_blueprint.name,
        )

    def compile_operator(
        self,
        operator: dict[str, Any],
        *,
        trigger_flow: "TriggerFlow | None" = None,
    ):
        blueprint = self._blueprint
        options = operator.get("options", {})
        _, capture_bindings = self.compile_bindings(
            options.get("capture"),
            mode="capture",
        )
        normalized_write_back, write_back_bindings = self.compile_bindings(
            options.get("write_back"),
            mode="write_back",
        )
        concurrency = operator["options"].get("concurrency")
        sub_flow_template = trigger_flow if trigger_flow is not None else self.build_from_operator(operator)
        resource_bindings = self.build_resource_bindings(capture_bindings)
        value_capture_bindings = tuple(
            binding
            for binding in capture_bindings
            if not self.is_resource_identity_binding(binding)
        )

        async def call_sub_flow(data):
            isolated_sub_flow = self.instantiate_isolated_sub_flow(sub_flow_template)

            capture_source = _ParentSubFlowCaptureSource(data)
            capture_target = _SubFlowCaptureTarget()
            self.apply_bindings(
                value_capture_bindings,
                source=capture_source,
                target=capture_target,
            )

            captured_flow_data = capture_target.build_flow_data()
            if captured_flow_data:
                isolated_sub_flow._flow_data.update(captured_flow_data)

            sub_flow_execution = isolated_sub_flow.create_execution(
                concurrency=concurrency,
                auto_close=False,
                parent_run_context=resolve_parent_run_context() or data.execution.run_context,
            )
            captured_runtime_data = capture_target.build_runtime_data()
            if captured_runtime_data:
                sub_flow_execution._runtime_data.update(captured_runtime_data)

            captured_resources = capture_target.build_resources()
            if captured_resources:
                sub_flow_execution.update_runtime_resources(captured_resources)
            if resource_bindings:
                self.apply_resource_bindings(
                    sub_flow_execution,
                    data.execution,
                    resource_bindings,
                )

            frame = self.build_active_frame(
                parent_execution=data.execution,
                child_execution=sub_flow_execution,
                child_flow_name=isolated_sub_flow.name,
                operator=operator,
                data=data,
                resource_bindings=resource_bindings,
            )
            frame_id = str(frame["frame_id"])
            control_task = asyncio.current_task()
            if control_task is None:
                raise RuntimeError("TriggerFlow sub flow control requires a running asyncio task.")
            data.execution._register_live_sub_flow(
                frame_id,
                sub_flow_execution,
                control_task,
                frame,
            )
            stream_bridge_task = asyncio.create_task(
                self.bridge_runtime_stream(
                    sub_flow_execution,
                    data.execution,
                )
            )
            try:
                await data.execution._emit_runtime_event(
                    "triggerflow.sub_flow_started",
                    message=(
                        f"TriggerFlow execution '{ data.execution.id }' started "
                        f"sub-flow frame '{ frame_id }'."
                    ),
                    payload=data.execution._sub_flow_event_payload(frame),
                )
                await sub_flow_execution._async_run_start(capture_target.build_input())
                if sub_flow_execution.is_waiting():
                    projected_root_ids = await self.project_child_interrupts(
                        parent_execution=data.execution,
                        child_execution=sub_flow_execution,
                        frame=frame,
                    )
                    root_interrupt = (
                        data.execution.get_interrupt(projected_root_ids[0])
                        if projected_root_ids
                        else {
                            "id": frame_id,
                            "type": "sub_flow",
                            "status": "waiting",
                            "sub_flow_frame_id": frame_id,
                        }
                    )
                    return TriggerFlowPauseSignal(root_interrupt)
                result = await self.complete_frame(
                    parent_execution=data.execution,
                    child_execution=sub_flow_execution,
                    frame=frame,
                    operator=operator,
                    normalized_write_back=normalized_write_back,
                    write_back_bindings=write_back_bindings,
                )
            except asyncio.CancelledError:
                stored_frame = data.execution._get_sub_flow_frames().get(frame_id, frame)
                cancel_requested = (
                    isinstance(stored_frame, dict)
                    and stored_frame.get("status") in {"cancel_requested", "cancelled"}
                )
                reason = (
                    str(stored_frame.get("cancel_reason") or "cancelled")
                    if isinstance(stored_frame, dict)
                    else "cancelled"
                )
                await self.finalize_cancelled_frame(
                    parent_execution=data.execution,
                    child_execution=sub_flow_execution,
                    frame_id=frame_id,
                    reason=reason if cancel_requested else "parent_control_task_cancelled",
                )
                if cancel_requested:
                    return None
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                stored_frame = data.execution._get_sub_flow_frames().get(frame_id, frame)
                if (
                    isinstance(stored_frame, dict)
                    and stored_frame.get("status") in {"cancel_requested", "cancelled"}
                ):
                    await self.finalize_cancelled_frame(
                        parent_execution=data.execution,
                        child_execution=sub_flow_execution,
                        frame_id=frame_id,
                        reason=str(stored_frame.get("cancel_reason") or "cancelled"),
                    )
                    return None
                await self.fail_frame(
                    parent_execution=data.execution,
                    child_execution=sub_flow_execution,
                    frame_id=frame_id,
                    error=error,
                )
                raise
            finally:
                data.execution._unregister_live_sub_flow(frame_id, sub_flow_execution)
                stream_bridge_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_bridge_task

            return result

        for signal in operator["listen_signals"]:
            blueprint.add_handler(
                signal["trigger_type"],
                signal["trigger_event"],
                call_sub_flow,
                id=operator["id"],
            )

    async def async_resume_frame(
        self,
        parent_execution: TriggerFlowExecution,
        frame_id: str,
        root_interrupt_id: str,
        value: Any = None,
    ):
        frames = parent_execution._get_sub_flow_frames()
        if frame_id not in frames:
            raise KeyError(
                f"Can not resume TriggerFlow sub flow frame '{ frame_id }' because it was not found."
            )
        frame = copy.deepcopy(frames[frame_id])
        operator = self._blueprint.definition.get_operator(str(frame["parent_operator_id"]))
        options = operator.get("options", {})
        _, write_back_bindings = self.compile_bindings(
            options.get("write_back"),
            mode="write_back",
        )
        normalized_write_back = options.get("write_back")
        child_flow = self.build_from_operator(operator)
        child_execution = child_flow.create_execution(
            concurrency=options.get("concurrency"),
            auto_close=False,
            parent_run_context=parent_execution.run_context,
        )
        child_execution.load(frame["child_saved_state"])
        self.restore_frame_resources(child_execution, parent_execution, frame)

        child_interrupt_id = frame.get("projected_interrupts", {}).get(root_interrupt_id)
        if not child_interrupt_id:
            raise KeyError(
                f"Can not resume TriggerFlow sub flow frame '{ frame_id }' because root interrupt "
                f"'{ root_interrupt_id }' is not mapped to a child interrupt."
            )

        frame["status"] = "running"
        frame["resumed_at"] = time.time()
        control_task = asyncio.current_task()
        if control_task is None:
            raise RuntimeError("TriggerFlow sub flow resume control requires a running asyncio task.")
        parent_execution._register_live_sub_flow(
            frame_id,
            child_execution,
            control_task,
            frame,
        )
        stream_bridge_task = asyncio.create_task(
            self.bridge_runtime_stream(
                child_execution,
                parent_execution,
            )
        )
        try:
            await child_execution.async_continue_with(str(child_interrupt_id), value)
            if child_execution.is_waiting():
                projected_root_ids = await self.project_child_interrupts(
                    parent_execution=parent_execution,
                    child_execution=child_execution,
                    frame=frame,
                )
                root_interrupt = (
                    parent_execution.get_interrupt(projected_root_ids[0])
                    if projected_root_ids
                    else parent_execution.get_interrupt(root_interrupt_id)
                )
                return TriggerFlowPauseSignal(root_interrupt or {})
            return await self.complete_frame(
                parent_execution=parent_execution,
                child_execution=child_execution,
                frame=frame,
                operator=operator,
                normalized_write_back=normalized_write_back,
                write_back_bindings=write_back_bindings,
            )
        except asyncio.CancelledError:
            stored_frame = parent_execution._get_sub_flow_frames().get(frame_id, frame)
            cancel_requested = (
                isinstance(stored_frame, dict)
                and stored_frame.get("status") in {"cancel_requested", "cancelled"}
            )
            reason = (
                str(stored_frame.get("cancel_reason") or "cancelled")
                if isinstance(stored_frame, dict)
                else "cancelled"
            )
            await self.finalize_cancelled_frame(
                parent_execution=parent_execution,
                child_execution=child_execution,
                frame_id=frame_id,
                reason=reason if cancel_requested else "parent_control_task_cancelled",
            )
            if cancel_requested:
                return None
            raise
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            stored_frame = parent_execution._get_sub_flow_frames().get(frame_id, frame)
            if (
                isinstance(stored_frame, dict)
                and stored_frame.get("status") in {"cancel_requested", "cancelled"}
            ):
                await self.finalize_cancelled_frame(
                    parent_execution=parent_execution,
                    child_execution=child_execution,
                    frame_id=frame_id,
                    reason=str(stored_frame.get("cancel_reason") or "cancelled"),
                )
                return None
            await self.fail_frame(
                parent_execution=parent_execution,
                child_execution=child_execution,
                frame_id=frame_id,
                error=error,
            )
            raise
        finally:
            parent_execution._unregister_live_sub_flow(frame_id, child_execution)
            stream_bridge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream_bridge_task

    def attach(
        self,
        trigger_flow: "TriggerFlow",
        listen_signals: list[dict[str, Any]],
        *,
        name: str | None = None,
        capture: "TriggerFlowSubFlowCapture | None" = None,
        write_back: "TriggerFlowSubFlowWriteBack | None" = None,
        concurrency: int | None = None,
        group_id: str | None = None,
        group_kind: str | None = None,
        parent_group_id: str | None = None,
        parent_group_kind: str | None = None,
    ):
        blueprint = self._blueprint
        blueprint._merge_registries_from_blueprint(trigger_flow._blue_print)
        normalized_capture, _ = self.compile_bindings(capture, mode="capture")
        normalized_write_back, _ = self.compile_bindings(write_back, mode="write_back")
        sub_flow_config = trigger_flow._blue_print.definition.to_dict(name=trigger_flow.name)
        identity_base = {
            "listen_signals": listen_signals,
            "name": name,
        }
        if name is None:
            identity_base.update(
                {
                    "sub_flow_name": trigger_flow.name,
                    "sub_flow_config": sub_flow_config,
                    "capture": normalized_capture,
                    "write_back": normalized_write_back,
                    "concurrency": concurrency,
                }
            )
        sub_flow_instance_id = blueprint.make_stable_identity_digest(identity_base)
        operator_id = f"sub-flow-{ sub_flow_instance_id }"
        operator = blueprint.definition.add_operator(
            id=operator_id,
            kind="sub_flow",
            name=name if name is not None else trigger_flow.name,
            listen_signals=listen_signals,
            emit_signals=[
                blueprint.make_signal(
                    "event",
                    f"SubFlow-{ sub_flow_instance_id }-Result",
                    role="continuation",
                )
            ],
            options={
                "sub_flow_name": trigger_flow.name,
                "sub_flow_config": sub_flow_config,
                "capture": normalized_capture,
                "write_back": normalized_write_back,
                "concurrency": concurrency,
            },
            group_id=group_id,
            group_kind=group_kind,
            parent_group_id=parent_group_id,
            parent_group_kind=parent_group_kind,
        )
        self.compile_operator(
            operator,
            trigger_flow=trigger_flow,
        )
        return operator
