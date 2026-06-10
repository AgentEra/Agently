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


import uuid
import copy
import json
import asyncio
import hashlib
import yaml
from pathlib import Path
from json import JSONDecodeError
from asyncio import Event, Semaphore
from collections.abc import Mapping
from typing import Any, Literal, TYPE_CHECKING, Sequence, cast
from ._async_utils import gather_cancel_on_error

if TYPE_CHECKING:
    from agently.types.trigger_flow import (
        TriggerFlowAllHandlers,
        TriggerFlowHandler,
        TriggerFlowPathReadable,
        TriggerFlowPathWritable,
        TriggerFlowSubFlowCapture,
        TriggerFlowSubFlowWriteBack,
    )
    from agently.types.data import ExecutionEnvironmentRequirement
    from .TriggerFlow import TriggerFlow

from agently.types.data import EMPTY
from agently.types.trigger_flow.runtime_keys import AGGREGATION_SCOPE_META_KEY
from agently.utils import StateDataNamespace
from .Chunk import TriggerFlowChunk
from .Execution import TriggerFlowExecution
from .Definition import (
    TriggerFlowDefinition,
    build_callable_ref,
    is_callable_ref_exportable,
    make_signal_ref,
    render_callable_ref,
)
from .BluePrintSubFlow import TriggerFlowBlueprintSubFlow
from .SubFlowBindings import _CompiledSubFlowBinding


def _stable_definition_json(value: Any):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)


class TriggerFlowBlueprint:
    def __init__(self, *, name: str | None = None):
        self.name = name if name is not None else f"Blueprint-{ uuid.uuid4().hex }"
        self._handlers: "TriggerFlowAllHandlers" = {
            "event": {},
            "flow_data": {},
            "runtime_data": {},
        }
        self.chunks: dict[str, TriggerFlowChunk] = {}
        self.definition = TriggerFlowDefinition(name=self.name)
        self._chunk_registry: dict[str, Any] = {}
        self._condition_registry: dict[str, Any] = {}
        self._sub_flow = TriggerFlowBlueprintSubFlow(self)

    def make_stable_identity_digest(self, identity: Any):
        digest = hashlib.sha1(_stable_definition_json(identity).encode("utf-8")).hexdigest()[:16]
        return digest

    def make_stable_operator_id(self, kind: str, identity: Any):
        digest = self.make_stable_identity_digest(identity)
        return f"{ kind }-{ digest }"

    def _callable_identity(self, callable_ref: dict[str, Any], *, explicit_name: str | None = None):
        callable_name = callable_ref.get("callable_name")
        if explicit_name is not None and explicit_name != callable_name:
            return {
                "kind": "explicit_name",
                "name": explicit_name,
            }
        if callable_ref.get("kind") in {"registered", "inspected"} and callable_ref.get("name"):
            return {
                "kind": "callable",
                "name": callable_ref.get("name"),
                "module": callable_ref.get("module"),
                "qualname": callable_ref.get("qualname"),
                "file": callable_ref.get("file"),
                "line": callable_ref.get("line"),
            }
        return None

    def _get_chunk_by_id(self, chunk_id: str):
        for chunk in self.chunks.values():
            if chunk.id == chunk_id:
                return chunk
        return None

    def _get_registry(self, registry_type: Literal["chunk", "condition"]):
        return self._chunk_registry if registry_type == "chunk" else self._condition_registry

    def _register_callable(
        self,
        registry_type: Literal["chunk", "condition"],
        handler: Any,
        *,
        name: str | None = None,
        strict: bool,
    ):
        callable_ref = build_callable_ref(handler, explicit_name=name)
        registry_name = callable_ref.get("name")
        if callable_ref["kind"] in {"registered", "inspected"} and registry_name:
            registry = self._get_registry(registry_type)
            existing = registry.get(str(registry_name))
            if existing is not None and existing is not handler:
                if strict or name is not None:
                    raise ValueError(
                        f"TriggerFlow { registry_type } handler '{ registry_name }' is already registered to another callable."
                    )
                fallback_ref = copy.deepcopy(callable_ref)
                fallback_ref["kind"] = "anonymous"
                return fallback_ref
            registry[str(registry_name)] = handler
            return callable_ref
        if strict:
            raise ValueError(
                f"TriggerFlow { registry_type } handler '{ render_callable_ref(callable_ref) }' "
                "must be a named function to support config import/export."
            )
        return callable_ref

    def register_chunk_handler(self, handler: Any, *, name: str | None = None):
        self._register_callable("chunk", handler, name=name, strict=True)
        return self

    def register_condition_handler(self, handler: Any, *, name: str | None = None):
        self._register_callable("condition", handler, name=name, strict=True)
        return self

    def _resolve_callable(self, registry_type: Literal["chunk", "condition"], callable_ref: dict[str, Any] | None):
        if not is_callable_ref_exportable(callable_ref):
            raise ValueError(
                f"Cannot load TriggerFlow config because { registry_type } reference "
                f"'{ render_callable_ref(callable_ref) }' is not serializable."
            )
        assert callable_ref is not None
        name = str(callable_ref["name"])
        registry = self._get_registry(registry_type)
        if name not in registry:
            raise ValueError(
                f"Cannot load TriggerFlow config because { registry_type } handler '{ name }' is not registered."
            )
        return registry[name]

    def make_signal(
        self,
        trigger_type: Literal["event", "runtime_data", "flow_data"],
        trigger_event: str,
        *,
        role: str | None = None,
    ):
        return make_signal_ref(trigger_type, trigger_event, role=role)

    def _mark_operator_group(
        self,
        operator_id: str,
        *,
        group_id: str | None = None,
        group_kind: str | None = None,
        parent_group_id: str | None = None,
        parent_group_kind: str | None = None,
    ):
        if group_id is None or group_kind is None:
            return self.definition.get_operator(operator_id)
        operator = self.definition.get_operator(operator_id)
        if operator.get("group_id") is None:
            operator["group_id"] = group_id
            operator["group_kind"] = group_kind
            operator["parent_group_id"] = parent_group_id
            operator["parent_group_kind"] = parent_group_kind
            return operator
        if operator.get("group_id") == group_id and operator.get("group_kind") == group_kind:
            if operator.get("parent_group_id") is None and parent_group_id is not None:
                operator["parent_group_id"] = parent_group_id
                operator["parent_group_kind"] = parent_group_kind
            return operator
        options = copy.deepcopy(operator.get("options", {}))
        usage_groups = options.get("usage_groups", [])
        group_entry = {
            "group_id": group_id,
            "group_kind": group_kind,
            "parent_group_id": parent_group_id,
            "parent_group_kind": parent_group_kind,
        }
        if group_entry not in usage_groups:
            usage_groups.append(group_entry)
        options["usage_groups"] = usage_groups
        operator["options"] = options
        return operator

    def create_chunk(
        self,
        handler: "TriggerFlowHandler",
        *,
        name: str | None = None,
        explicit_name: str | None = None,
    ):
        callable_ref = self._register_callable("chunk", handler, name=explicit_name, strict=False)
        stable_identity = self._callable_identity(callable_ref, explicit_name=explicit_name)
        chunk_id = (
            self.make_stable_operator_id("chunk", stable_identity)
            if stable_identity is not None
            else None
        )
        if chunk_id is not None:
            existing_chunk = self._get_chunk_by_id(chunk_id)
            if existing_chunk is not None:
                if existing_chunk._handler is not handler:
                    raise ValueError(
                        f"TriggerFlow chunk identity '{ existing_chunk.name }' is already bound to another callable."
                    )
                return existing_chunk
        chunk_name = name if name is not None else (callable_ref.get("name") if stable_identity is not None else None)
        trigger = f"Chunk[{ chunk_name }]-{ chunk_id }" if chunk_id is not None and chunk_name is not None else None
        chunk = TriggerFlowChunk(
            handler,
            chunk_id=chunk_id,
            name=chunk_name,
            trigger=trigger,
            callable_ref=callable_ref,
            blueprint=self,
        )
        self.chunks[chunk.name] = chunk
        self.sync_chunk_definition(chunk)
        return chunk

    def _merge_callable_registry(
        self,
        registry_type: Literal["chunk", "condition"],
        source_registry: dict[str, Any],
    ):
        target_registry = self._get_registry(registry_type)
        for name, handler in source_registry.items():
            existing = target_registry.get(name)
            if existing is not None and existing is not handler:
                raise ValueError(
                    f"TriggerFlow { registry_type } handler '{ name }' is already registered to another callable."
                )
            target_registry[name] = handler
        return self

    def _merge_registries_from_blueprint(self, blueprint: "TriggerFlowBlueprint"):
        self._merge_callable_registry("chunk", blueprint._chunk_registry)
        self._merge_callable_registry("condition", blueprint._condition_registry)
        return self

    def sync_chunk_definition(
        self,
        chunk: TriggerFlowChunk,
        *,
        group_id: str | None = None,
        group_kind: str | None = None,
        parent_group_id: str | None = None,
        parent_group_kind: str | None = None,
    ):
        emit_signals = [
            self.make_signal("event", chunk.trigger, role="continuation"),
            *[self.make_signal("event", signal, role="declared_emit") for signal in chunk.emit_signals],
        ]
        if chunk.id not in {operator["id"] for operator in self.definition.operators}:
            self.definition.add_operator(
                id=chunk.id,
                kind="chunk",
                name=chunk.name,
                handler_ref=chunk.callable_ref,
                emit_signals=emit_signals,
                group_id=group_id,
                group_kind=group_kind,
                parent_group_id=parent_group_id,
                parent_group_kind=parent_group_kind,
            )
        else:
            self.definition.update_operator(
                chunk.id,
                name=chunk.name,
                handler_ref=chunk.callable_ref,
                emit_signals=emit_signals,
            )
            self._mark_operator_group(
                chunk.id,
                group_id=group_id,
                group_kind=group_kind,
                parent_group_id=parent_group_id,
                parent_group_kind=parent_group_kind,
            )
        return self.definition.get_operator(chunk.id)

    def attach_chunk(
        self,
        chunk: TriggerFlowChunk,
        listen_signals: list[dict[str, Any]],
        *,
        group_id: str | None = None,
        group_kind: str | None = None,
        parent_group_id: str | None = None,
        parent_group_kind: str | None = None,
    ):
        self.sync_chunk_definition(
            chunk,
            group_id=group_id,
            group_kind=group_kind,
            parent_group_id=parent_group_id,
            parent_group_kind=parent_group_kind,
        )
        self.definition.append_listen_signals(chunk.id, listen_signals)
        self._mark_operator_group(
            chunk.id,
            group_id=group_id,
            group_kind=group_kind,
            parent_group_id=parent_group_id,
            parent_group_kind=parent_group_kind,
        )
        return self.definition.get_operator(chunk.id)

    def _parse_sub_flow_relative_path(self, path: str, *, option_name: str):
        return self._sub_flow.parse_relative_path(path, option_name=option_name)

    def _parse_sub_flow_source_path(
        self,
        path: str,
        *,
        mode: Literal["capture", "write_back"],
        target_scope: str,
    ):
        return self._sub_flow.parse_source_path(
            path,
            mode=mode,
            target_scope=target_scope,
        )

    def _normalize_sub_flow_scope_binding(
        self,
        binding: Any,
        *,
        mode: Literal["capture", "write_back"],
        scope: str,
    ):
        return self._sub_flow.normalize_scope_binding(
            binding,
            mode=mode,
            scope=scope,
        )

    def _normalize_sub_flow_spec(
        self,
        spec: "TriggerFlowSubFlowCapture | TriggerFlowSubFlowWriteBack | None",
        *,
        mode: Literal["capture", "write_back"],
    ):
        return self._sub_flow.normalize_spec(spec, mode=mode)

    def _validate_sub_flow_target_conflicts(
        self,
        target_paths: list[tuple[str, ...]],
        *,
        mode: Literal["capture", "write_back"],
        scope: str,
    ):
        return self._sub_flow.validate_target_conflicts(
            target_paths,
            mode=mode,
            scope=scope,
        )

    def _compile_sub_flow_bindings(
        self,
        spec: "TriggerFlowSubFlowCapture | TriggerFlowSubFlowWriteBack | None",
        *,
        mode: Literal["capture", "write_back"],
    ):
        return self._sub_flow.compile_bindings(spec, mode=mode)

    def _apply_sub_flow_bindings(
        self,
        bindings: Sequence[_CompiledSubFlowBinding],
        *,
        source: "TriggerFlowPathReadable",
        target: "TriggerFlowPathWritable",
    ):
        return self._sub_flow.apply_bindings(bindings, source=source, target=target)

    def _instantiate_isolated_sub_flow(self, trigger_flow: "TriggerFlow"):
        return self._sub_flow.instantiate_isolated_sub_flow(trigger_flow)

    async def _bridge_sub_flow_runtime_stream(
        self,
        child_execution: TriggerFlowExecution,
        parent_execution: TriggerFlowExecution,
    ):
        return await self._sub_flow.bridge_runtime_stream(child_execution, parent_execution)

    def _build_sub_flow_resource_bindings(
        self,
        capture_bindings: Sequence[_CompiledSubFlowBinding],
    ):
        return self._sub_flow.build_resource_bindings(capture_bindings)

    def _apply_sub_flow_resource_bindings(
        self,
        child_execution: TriggerFlowExecution,
        parent_execution: TriggerFlowExecution,
        resource_bindings: Mapping[str, str],
    ):
        return self._sub_flow.apply_resource_bindings(
            child_execution,
            parent_execution,
            resource_bindings,
        )

    def _restore_sub_flow_frame_resources(
        self,
        child_execution: TriggerFlowExecution,
        parent_execution: TriggerFlowExecution,
        frame: dict[str, Any],
    ):
        return self._sub_flow.restore_frame_resources(child_execution, parent_execution, frame)

    def _make_sub_flow_frame_id(self, parent_execution: TriggerFlowExecution, operator_id: str):
        return self._sub_flow.make_frame_id(parent_execution, operator_id)

    def _build_sub_flow_parent_data(
        self,
        parent_execution: TriggerFlowExecution,
        frame: dict[str, Any],
        operator: dict[str, Any],
    ):
        return self._sub_flow.build_parent_data(parent_execution, frame, operator)

    async def _project_child_interrupts(
        self,
        *,
        parent_execution: TriggerFlowExecution,
        child_execution: TriggerFlowExecution,
        frame: dict[str, Any],
    ):
        return await self._sub_flow.project_child_interrupts(
            parent_execution=parent_execution,
            child_execution=child_execution,
            frame=frame,
        )

    async def _complete_sub_flow_frame(
        self,
        *,
        parent_execution: TriggerFlowExecution,
        child_execution: TriggerFlowExecution,
        frame: dict[str, Any],
        operator: dict[str, Any],
        normalized_write_back: Any,
        write_back_bindings: Sequence[_CompiledSubFlowBinding],
    ):
        return await self._sub_flow.complete_frame(
            parent_execution=parent_execution,
            child_execution=child_execution,
            frame=frame,
            operator=operator,
            normalized_write_back=normalized_write_back,
            write_back_bindings=write_back_bindings,
        )

    def _build_sub_flow_from_operator(self, operator: dict[str, Any]):
        return self._sub_flow.build_from_operator(operator)

    def _compile_sub_flow_operator(
        self,
        operator: dict[str, Any],
        *,
        trigger_flow: "TriggerFlow | None" = None,
    ):
        return self._sub_flow.compile_operator(operator, trigger_flow=trigger_flow)

    async def async_resume_sub_flow_frame(
        self,
        parent_execution: TriggerFlowExecution,
        frame_id: str,
        root_interrupt_id: str,
        value: Any = None,
    ):
        return await self._sub_flow.async_resume_frame(
            parent_execution,
            frame_id,
            root_interrupt_id,
            value,
        )

    def attach_sub_flow(
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
        return self._sub_flow.attach(
            trigger_flow,
            listen_signals,
            name=name,
            capture=capture,
            write_back=write_back,
            concurrency=concurrency,
            group_id=group_id,
            group_kind=group_kind,
            parent_group_id=parent_group_id,
            parent_group_kind=parent_group_kind,
        )

    def add_handler(
        self,
        type: Literal["event", "flow_data", "runtime_data"],
        target: str,
        handler: "TriggerFlowHandler",
        *,
        id: str | None = None,
    ):
        handler_id = str(id) if id is not None else f"Handler<{ handler.__name__ }>-{ uuid.uuid4().hex }"
        handlers = self._handlers[type]
        if target not in handlers:
            handlers[target] = {}
        if handler_id in handlers[target]:
            return handler_id
        for stored_id, stored_handler in handlers[target].items():
            if handler == stored_handler:
                return stored_id
        handlers[target][handler_id] = handler
        return handler_id

    def remove_handler(
        self,
        type: Literal["event", "flow_data", "runtime_data"],
        target: str,
        handler: "TriggerFlowHandler | str",
    ):
        handlers = self._handlers[type]
        if target in handlers:
            if isinstance(handler, str):
                handlers[target].pop(handler)
            else:
                for id, stored_handler in handlers[target].items():
                    if handler == stored_handler:
                        del handlers[target][id]
                        return

    def remove_all(
        self,
        type: Literal["event", "flow_data", "runtime_data"],
        target: str,
    ):
        handlers = self._handlers[type]
        if target in handlers:
            handlers[target] = {}

    def add_event_handler(
        self,
        event: str,
        handler: "TriggerFlowHandler",
        *,
        id: str | None = None,
    ):
        return self.add_handler("event", event, handler, id=id)

    def remove_event_handler(
        self,
        event: str,
        handler: "TriggerFlowHandler | str",
    ):
        return self.remove_handler("event", event, handler)

    def add_flow_data_handler(
        self,
        key: str,
        handler: "TriggerFlowHandler",
        *,
        id: str | None = None,
    ):
        return self.add_handler("flow_data", key, handler, id=id)

    def remove_flow_data_handler(
        self,
        key: str,
        handler: "TriggerFlowHandler | str",
    ):
        return self.remove_handler("flow_data", key, handler)

    def add_runtime_data_handler(
        self,
        key: str,
        handler: "TriggerFlowHandler",
        *,
        id: str | None = None,
    ):
        return self.add_handler("runtime_data", key, handler, id=id)

    def remove_runtime_data_handler(
        self,
        key: str,
        handler: "TriggerFlowHandler | str",
    ):
        return self.remove_handler("runtime_data", key, handler)

    def _reset_runtime(self):
        self._handlers = {
            "event": {},
            "flow_data": {},
            "runtime_data": {},
        }
        self.chunks = {}

    @staticmethod
    def _layer_key(data):
        if data._layer_marks:
            return ".".join(data._layer_marks)
        signal_scope = data.signal_meta.get(AGGREGATION_SCOPE_META_KEY)
        if signal_scope is not None:
            return f"signal:{ signal_scope }"
        return "__root__"

    def _compile_chunk_operator(self, operator: dict[str, Any]):
        handler = self._resolve_callable("chunk", operator.get("handler_ref"))
        continuation_signal = next(
            (
                signal
                for signal in operator["emit_signals"]
                if signal.get("role") != "declared_emit"
            ),
            self.make_signal("event", f"Chunk-{ operator['id'] }", role="continuation"),
        )
        chunk = TriggerFlowChunk(
            handler,
            chunk_id=operator["id"],
            name=operator.get("name"),
            trigger=continuation_signal["trigger_event"],
            callable_ref=operator.get("handler_ref"),
            blueprint=self,
            emit_signals=[
                signal["trigger_event"]
                for signal in operator["emit_signals"]
                if signal.get("role") == "declared_emit"
            ],
        )
        self.chunks[chunk.name] = chunk
        for signal in operator["listen_signals"]:
            self.add_handler(
                signal["trigger_type"],
                signal["trigger_event"],
                chunk.async_call,
                id=operator["id"],
            )

    def _compile_signal_gate_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]
        mode = operator["options"].get("mode", "and")
        values_template: dict[str, dict[str, Any]] = {}
        for signal in operator["listen_signals"]:
            values_template.setdefault(signal["trigger_type"], {})
            values_template[signal["trigger_type"]][signal["trigger_event"]] = EMPTY

        async def wait_trigger(data):
            match mode:
                case "or" | "simple_or":
                    await data.async_emit(
                        emit_signal["trigger_event"],
                        (
                            data.value
                            if mode == "simple_or"
                            else (data.trigger_type, data.trigger_event, data.value)
                        ),
                        _layer_marks=data._layer_marks.copy(),
                    )
                case "and":
                    state_key = f"when_states.{ operator['id'] }.{ self._layer_key(data) }"
                    state = data._system_runtime_data.get(state_key)
                    if not isinstance(state, dict):
                        state = copy.deepcopy(values_template)
                    if data.trigger_type in state and data.trigger_event in state[data.trigger_type]:
                        state[data.trigger_type][data.trigger_event] = data.value
                    data._system_runtime_data.set(state_key, state)
                    for trigger_event_dict in state.values():
                        for event_value in trigger_event_dict.values():
                            if event_value is EMPTY:
                                return
                    await data.async_emit(
                        emit_signal["trigger_event"],
                        state,
                        _layer_marks=data._layer_marks.copy(),
                    )
                    del data._system_runtime_data[state_key]

        for signal in operator["listen_signals"]:
            self.add_handler(
                signal["trigger_type"],
                signal["trigger_event"],
                wait_trigger,
                id=operator["id"],
            )

    def _compile_batch_fanout_operator(self, operator: dict[str, Any]):
        concurrency = operator["options"].get("concurrency")

        async def send_to_branches(data):
            data.layer_in()
            layer_marks = data._layer_marks.copy()

            async def emit_branch(signal: dict[str, Any]):
                if concurrency is None or concurrency <= 0:
                    await data.async_emit(
                        signal["trigger_event"],
                        data.value,
                        _layer_marks=layer_marks,
                    )
                    return
                semaphore_key = f"batch_fanout_semaphores.{ operator['id'] }"
                semaphore = data._system_runtime_data.get(semaphore_key, inherit=False)
                if not isinstance(semaphore, Semaphore):
                    semaphore = Semaphore(concurrency)
                    data._system_runtime_data.set(semaphore_key, semaphore)
                async with semaphore:
                    await data.async_emit(
                        signal["trigger_event"],
                        data.value,
                        _layer_marks=layer_marks,
                    )

            try:
                await gather_cancel_on_error(*[emit_branch(signal) for signal in operator["emit_signals"]])
            finally:
                data.layer_out()

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], send_to_branches, id=operator["id"])

    def _compile_batch_collect_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]
        result_keys = dict(operator["options"].get("result_keys", {}))
        trigger_to_result_key = {signal_id: result_key for signal_id, result_key in result_keys.items()}
        triggers_template = {signal["id"]: False for signal in operator["listen_signals"]}
        results_template = {result_key: None for result_key in trigger_to_result_key.values()}

        async def wait_all_chunks(data):
            signal_id = f"{ data.trigger_type }:{ data.trigger_event }"
            if signal_id not in trigger_to_result_key:
                return
            layer_key = self._layer_key(data)
            state_key = f"batch_states.{ operator['id'] }.{ layer_key }"
            state = data._system_runtime_data.get(state_key)
            if not isinstance(state, dict):
                state = {
                    "results": copy.deepcopy(results_template),
                    "triggers": triggers_template.copy(),
                }
            state["results"][trigger_to_result_key[signal_id]] = data.value
            state["triggers"][signal_id] = True
            data._system_runtime_data.set(state_key, state)
            for done in state["triggers"].values():
                if done is False:
                    return
            data.layer_out()
            await data.async_emit(
                emit_signal["trigger_event"],
                state["results"],
                _layer_marks=data._layer_marks.copy(),
            )
            del data._system_runtime_data[state_key]

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], wait_all_chunks, id=operator["id"])

    def _compile_for_each_split_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]
        end_signal = next(
            (
                signal
                for signal in operator.get("emit_signals", [])
                if str(signal.get("trigger_event", "")).endswith("-End")
            ),
            None,
        )
        if end_signal is None:
            group_id = operator.get("group_id") or str(operator["id"]).removeprefix("for_each-split-")
            end_signal = self.make_signal("event", f"ForEach-{ group_id }-End", role="continuation")
        concurrency = operator["options"].get("concurrency")

        async def send_items(data):
            data.layer_in()
            for_each_instance_id = data.layer_mark
            assert for_each_instance_id is not None
            send_tasks = []

            def prepare_item(item):
                data.layer_in()
                item_id = data.layer_mark
                assert item_id is not None
                layer_marks = data._layer_marks.copy()
                data._system_runtime_data.set(f"for_each_results.{ for_each_instance_id }.{ item_id }", EMPTY)
                data.layer_out()
                return layer_marks, item

            async def emit_item(item, layer_marks):
                if concurrency is None or concurrency <= 0:
                    await data.async_emit(
                        emit_signal["trigger_event"],
                        item,
                        layer_marks,
                    )
                else:
                    semaphore_key = f"for_each_semaphores.{ operator['id'] }"
                    semaphore = data._system_runtime_data.get(semaphore_key, inherit=False)
                    if not isinstance(semaphore, asyncio.Semaphore):
                        semaphore = asyncio.Semaphore(concurrency)
                        data._system_runtime_data.set(semaphore_key, semaphore)
                    async with semaphore:
                        await data.async_emit(
                            emit_signal["trigger_event"],
                            item,
                            layer_marks,
                        )

            if not isinstance(data.value, str) and isinstance(data.value, Sequence):
                items = list(data.value)
                if not items:
                    data.layer_out()
                    await data.async_emit(
                        end_signal["trigger_event"],
                        [],
                        data._layer_marks.copy(),
                    )
                    return
                for item in items:
                    layer_marks, item_value = prepare_item(item)
                    send_tasks.append(emit_item(item_value, layer_marks))
                await gather_cancel_on_error(*send_tasks)
            else:
                layer_marks, item_value = prepare_item(data.value)
                await emit_item(item_value, layer_marks)

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], send_items, id=operator["id"])

    def _compile_for_each_collect_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]

        async def collect_results(data):
            for_each_instance_id = data.upper_layer_mark
            item_id = data.layer_mark
            assert for_each_instance_id is not None and item_id is not None
            for_each_results = StateDataNamespace(data._system_runtime_data, "for_each_results")
            if for_each_instance_id in for_each_results and item_id in for_each_results[for_each_instance_id]:
                for_each_results.set(f"{ for_each_instance_id }.{ item_id }", data.value)
                for value in for_each_results.get(for_each_instance_id, {}).values():
                    if value is EMPTY:
                        return
                data.layer_out()
                data.layer_out()
                await data.async_emit(
                    emit_signal["trigger_event"],
                    list(for_each_results[for_each_instance_id].values()),
                    data._layer_marks.copy(),
                )
                for_each_results.delete(for_each_instance_id)

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], collect_results, id=operator["id"])

    def _compile_match_route_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]
        mode = operator["options"].get("mode", "hit_first")
        cases = []
        for case in operator["options"].get("cases", []):
            condition = None
            if case.get("condition_ref") is not None:
                condition = self._resolve_callable("condition", case["condition_ref"])
            elif "condition_value" in case:
                condition = case["condition_value"]
            cases.append(
                {
                    "route_signal": case.get("route_signal"),
                    "condition": condition,
                    "is_else": bool(case.get("is_else", False)),
                }
            )
        else_signal = operator["options"].get("else_signal")

        async def match_case(data):
            data.layer_in()
            matched_count = 0
            tasks = []
            for case in cases:
                if case["is_else"]:
                    continue
                condition = case["condition"]
                if callable(condition):
                    judgement = condition(data)
                else:
                    judgement = bool(data.value == condition)
                if judgement is True:
                    if mode == "hit_first":
                        await data.async_emit(
                            case["route_signal"]["trigger_event"],
                            data.value,
                            _layer_marks=data._layer_marks.copy(),
                        )
                        return
                    if mode == "hit_all":
                        data.layer_in()
                        matched_count += 1
                        data._system_runtime_data.set(
                            f"match_results.{ data.upper_layer_mark }.{ data.layer_mark }",
                            EMPTY,
                        )
                        tasks.append(
                            data.async_emit(
                                case["route_signal"]["trigger_event"],
                                data.value,
                                _layer_marks=data._layer_marks.copy(),
                            )
                        )
                        data.layer_out()
            await gather_cancel_on_error(*tasks)
            if matched_count == 0:
                if isinstance(else_signal, dict):
                    await data.async_emit(
                        else_signal["trigger_event"],
                        data.value,
                        _layer_marks=data._layer_marks.copy(),
                    )
                else:
                    data.layer_out()
                    await data.async_emit(
                        emit_signal["trigger_event"],
                        data.value,
                        _layer_marks=data._layer_marks.copy(),
                    )

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], match_case, id=operator["id"])

    def _compile_match_case_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]

        async def pass_case(data):
            await data.async_emit(
                emit_signal["trigger_event"],
                data.value,
                _layer_marks=data._layer_marks.copy(),
            )

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], pass_case, id=operator["id"])

    def _compile_match_collect_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]

        async def collect_branch_result(data):
            match_results = data._system_runtime_data.get(f"match_results.{ data.upper_layer_mark }")
            if match_results:
                if data.layer_mark in match_results:
                    match_results[data.layer_mark] = data.value
                for value in match_results.values():
                    if value is EMPTY:
                        data._system_runtime_data.set(f"match_results.{ data.upper_layer_mark }", match_results)
                        return
                data.layer_out()
                await data.async_emit(
                    emit_signal["trigger_event"],
                    list(match_results.values()),
                    _layer_marks=data._layer_marks.copy(),
                )
                del data._system_runtime_data[f"match_results.{ data.upper_layer_mark }"]
            else:
                data.layer_out()
                await data.async_emit(
                    emit_signal["trigger_event"],
                    data.value,
                    _layer_marks=data._layer_marks.copy(),
                )

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], collect_branch_result, id=operator["id"])

    def _compile_collect_branch_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]
        collect_id = operator["options"].get("collect_id", operator["id"])
        branch_ids = list(operator["options"].get("branch_ids", []))
        branch_id = operator["options"].get("branch_id")
        mode = operator["options"].get("mode", "filled_and_update")

        async def collect_branches(data):
            state_key = f"collect_states.{ collect_id }.{ self._layer_key(data) }"
            state = data._system_runtime_data.get(state_key)
            if not isinstance(state, dict):
                state = {configured_branch_id: EMPTY for configured_branch_id in branch_ids}
            if branch_id is not None:
                state[branch_id] = data.value
            data._system_runtime_data.set(state_key, state)

            for configured_branch_id in branch_ids:
                if state.get(configured_branch_id, EMPTY) is EMPTY:
                    return

            collected = {configured_branch_id: state[configured_branch_id] for configured_branch_id in branch_ids}
            await data.async_emit(
                emit_signal["trigger_event"],
                collected,
                _layer_marks=data._layer_marks.copy(),
            )
            if mode == "filled_then_empty":
                del data._system_runtime_data[state_key]

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], collect_branches, id=operator["id"])

    def _compile_intervention_point_operator(self, operator: dict[str, Any]):
        emit_signal = operator["emit_signals"][0]
        target = operator.get("options", {}).get("target")

        async def insert_interventions(data):
            await data.execution._async_insert_planned_interventions(
                target=str(target) if target is not None else None,
                operator=operator,
                signal=data.signal,
            )
            await data.async_emit(
                emit_signal["trigger_event"],
                data.value,
                _layer_marks=data._layer_marks.copy(),
            )

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], insert_interventions, id=operator["id"])

    def _compile_result_sink_operator(self, operator: dict[str, Any]):
        async def set_default_result(data):
            result = data._system_runtime_data.get("result")
            if result is EMPTY:
                data.set_result(data.value)
            else:
                result_ready = data._system_runtime_data.get("result_ready")
                if isinstance(result_ready, Event):
                    result_ready.set()

        for signal in operator["listen_signals"]:
            self.add_handler(signal["trigger_type"], signal["trigger_event"], set_default_result, id=operator["id"])

    def _compile_operator(self, operator: dict[str, Any]):
        kind = operator["kind"]
        if kind == "chunk":
            self._compile_chunk_operator(operator)
        elif kind == "signal_gate":
            self._compile_signal_gate_operator(operator)
        elif kind == "batch_fanout":
            self._compile_batch_fanout_operator(operator)
        elif kind == "batch_collect":
            self._compile_batch_collect_operator(operator)
        elif kind == "for_each_split":
            self._compile_for_each_split_operator(operator)
        elif kind == "for_each_collect":
            self._compile_for_each_collect_operator(operator)
        elif kind == "match_route":
            self._compile_match_route_operator(operator)
        elif kind == "match_case":
            self._compile_match_case_operator(operator)
        elif kind == "match_collect":
            self._compile_match_collect_operator(operator)
        elif kind == "collect_branch":
            self._compile_collect_branch_operator(operator)
        elif kind == "intervention_point":
            self._compile_intervention_point_operator(operator)
        elif kind == "sub_flow":
            self._compile_sub_flow_operator(operator)
        elif kind == "result_sink":
            self._compile_result_sink_operator(operator)
        else:
            raise ValueError(f"Unsupported TriggerFlow operator kind '{ kind }' in config compiler.")

    def _compile_definition(self):
        self._reset_runtime()
        for operator in self.definition.operators:
            self._compile_operator(operator)
        return self

    def get_flow_config(self, *, name: str | None = None, validate_serializable: bool = True):
        return self.definition.to_dict(
            validate_serializable=validate_serializable,
            name=name if name is not None else self.name,
        )

    def _get_definition_fingerprint(self):
        config = self.definition.to_dict(
            validate_serializable=False,
            name=self.name,
        )
        config = copy.deepcopy(config)
        config.pop("name", None)
        digest = hashlib.sha256(_stable_definition_json(config).encode("utf-8")).hexdigest()
        return f"sha256:{ digest }"

    def get_json_flow(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
        name: str | None = None,
    ):
        content = json.dumps(
            self.get_flow_config(name=name),
            indent=2,
            ensure_ascii=False,
        )
        if save_to is not None:
            path = Path(save_to)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding=encoding)
        return content

    def get_yaml_flow(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
        name: str | None = None,
    ):
        content = yaml.safe_dump(
            self.get_flow_config(name=name),
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )
        if save_to is not None:
            path = Path(save_to)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding=encoding)
        return content

    def to_mermaid(self, *, mode: Literal["simplified", "detailed"] = "simplified", name: str | None = None):
        if name is not None:
            self.definition.name = name
        return self.definition.to_mermaid(mode=mode)

    def load_flow_config(
        self,
        config: dict[str, Any],
        *,
        replace: bool = True,
    ):
        loaded_definition = TriggerFlowDefinition.from_dict(config)
        has_existing_definition = bool(self.definition.operators) or bool(self.definition.meta)

        if replace or not has_existing_definition:
            self.definition = loaded_definition
        else:
            merged_definition = self.definition.copy()
            merged_definition.meta = {
                **merged_definition.meta,
                **loaded_definition.meta,
            }
            if loaded_definition.contract:
                merged_definition.contract = loaded_definition.contract.copy()
            for operator in loaded_definition.operators:
                merged_definition.add_operator(**operator)
            self.definition = merged_definition
        self.name = self.definition.name
        self._compile_definition()
        return self

    def load_json_flow(
        self,
        path_or_content: str | Path,
        *,
        replace: bool = True,
        encoding: str | None = "utf-8",
    ):
        path = Path(path_or_content)
        is_json_file = False
        try:
            is_json_file = path.exists() and path.is_file()
        except (OSError, ValueError):
            is_json_file = False
        if is_json_file:
            try:
                content = path.read_text(encoding=encoding)
                config = json.loads(content)
            except (JSONDecodeError, ValueError) as e:
                raise ValueError(f"Cannot load TriggerFlow JSON file '{ path_or_content }'.\nError: { e }")
        else:
            try:
                config = json.loads(str(path_or_content))
            except (JSONDecodeError, ValueError) as e:
                raise ValueError(f"Cannot load TriggerFlow JSON content or file path not existed.\nError: { e }")
        if not isinstance(config, dict):
            raise TypeError(f"Cannot load TriggerFlow JSON config, expect dictionary but got: { type(config) }")
        return self.load_flow_config(config, replace=replace)

    def load_yaml_flow(
        self,
        path_or_content: str | Path,
        *,
        replace: bool = True,
        encoding: str | None = "utf-8",
    ):
        path = Path(path_or_content)
        is_yaml_file = False
        try:
            is_yaml_file = path.exists() and path.is_file()
        except (OSError, ValueError):
            is_yaml_file = False
        if is_yaml_file:
            try:
                content = path.read_text(encoding=encoding)
                config = yaml.safe_load(content)
            except yaml.YAMLError as e:
                raise ValueError(f"Cannot load TriggerFlow YAML file '{ path_or_content }'.\nError: { e }")
        else:
            try:
                config = yaml.safe_load(str(path_or_content))
            except yaml.YAMLError as e:
                raise ValueError(f"Cannot load TriggerFlow YAML content or file path not existed.\nError: { e }")
        if not isinstance(config, dict):
            raise TypeError(f"Cannot load TriggerFlow YAML config, expect dictionary but got: { type(config) }")
        return self.load_flow_config(config, replace=replace)

    def create_execution(
        self,
        trigger_flow: "TriggerFlow",
        *,
        execution_id: str | None = None,
        skip_exceptions: bool = False,
        concurrency: int | None = None,
        run_context=None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 10.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        intervention_mode: Literal["planned", "auto"] | None = None,
        intervention_policy: Any = None,
        resume_handle_exposed: bool = True,
    ):
        handlers_snapshot: TriggerFlowAllHandlers = {
            "event": {k: v.copy() for k, v in self._handlers["event"].items()},
            "flow_data": {k: v.copy() for k, v in self._handlers["flow_data"].items()},
            "runtime_data": {k: v.copy() for k, v in self._handlers["runtime_data"].items()},
        }
        return TriggerFlowExecution(
            handlers=handlers_snapshot,
            trigger_flow=trigger_flow,
            id=execution_id,
            skip_exceptions=skip_exceptions,
            concurrency=concurrency,
            run_context=run_context,
            auto_close=auto_close,
            auto_close_timeout=auto_close_timeout,
            owner_id=owner_id,
            lease_ttl=lease_ttl,
            execution_environments=execution_environments,
            intervention_mode=intervention_mode,
            intervention_policy=intervention_policy,
            resume_handle_exposed=resume_handle_exposed,
        )

    def copy(self, *, name: str | None = None):
        new_blueprint = type(self)(name=name if name is not None else self.name)
        new_blueprint._handlers = {
            "event": {key: value.copy() for key, value in self._handlers["event"].items()},
            "flow_data": {key: value.copy() for key, value in self._handlers["flow_data"].items()},
            "runtime_data": {key: value.copy() for key, value in self._handlers["runtime_data"].items()},
        }
        new_blueprint.definition = self.definition.copy()
        new_blueprint._chunk_registry = self._chunk_registry.copy()
        new_blueprint._condition_registry = self._condition_registry.copy()
        for chunk in self.chunks.values():
            new_blueprint.chunks[chunk.name] = TriggerFlowChunk(
                chunk._handler,
                chunk_id=chunk.id,
                name=chunk.name,
                trigger=chunk.trigger,
                callable_ref=chunk.callable_ref,
                blueprint=new_blueprint,
                emit_signals=chunk.emit_signals,
            )
        return new_blueprint
