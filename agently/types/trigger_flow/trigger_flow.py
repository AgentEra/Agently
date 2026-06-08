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
from collections.abc import Mapping

from typing import Any, Callable, Literal, TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable, Generic, TypeVar
from typing_extensions import TypedDict
from agently.types.data import AVOID_COPY
from agently.types.trigger_flow.runtime_keys import AGGREGATION_SCOPE_META_KEY, PARENT_SIGNAL_ID_META_KEY

if TYPE_CHECKING:
    from agently.core import TriggerFlowExecution
    from agently.core.orchestration.TriggerFlow.Signal import TriggerFlowSignal
    from agently.types.data import RunContext

from agently.utils import StateData


class TriggerFlowBlockData:
    global_data = StateData()

    def __init__(
        self,
        outer_block: "TriggerFlowBlockData | None" = None,
        data: dict[str, Any] | None = None,
    ):
        self.outer_block = outer_block
        self.data = data if data is not None else {}


TriggerFlowPathSegments: TypeAlias = tuple[str, ...]
TriggerFlowSubFlowPathBindingMap: TypeAlias = Mapping[str, str]
TriggerFlowSubFlowRootBinding: TypeAlias = str | TriggerFlowSubFlowPathBindingMap
TriggerFlowSubFlowCaptureTargetScope: TypeAlias = Literal["input", "runtime_data", "flow_data", "resources"]
TriggerFlowSubFlowCaptureSourceScope: TypeAlias = Literal["value", "runtime_data", "flow_data", "resources"]
TriggerFlowSubFlowWriteBackTargetScope: TypeAlias = Literal["value", "runtime_data", "flow_data"]
TriggerFlowSubFlowWriteBackSourceScope: TypeAlias = Literal["result"]


class TriggerFlowSubFlowCapture(TypedDict, total=False):
    input: TriggerFlowSubFlowRootBinding
    runtime_data: TriggerFlowSubFlowPathBindingMap
    flow_data: TriggerFlowSubFlowPathBindingMap
    resources: TriggerFlowSubFlowPathBindingMap


class TriggerFlowSubFlowWriteBack(TypedDict, total=False):
    value: TriggerFlowSubFlowRootBinding
    runtime_data: TriggerFlowSubFlowPathBindingMap
    flow_data: TriggerFlowSubFlowPathBindingMap


@runtime_checkable
class TriggerFlowPathReadable(Protocol):
    def read_path(self, scope: str, path: TriggerFlowPathSegments) -> Any: ...


@runtime_checkable
class TriggerFlowPathWritable(Protocol):
    def write_path(self, scope: str, path: TriggerFlowPathSegments, value: Any) -> None: ...


_MISSING = object()
ValueT = TypeVar("ValueT")
StreamT = TypeVar("StreamT")
ResultT = TypeVar("ResultT")


class _TriggerFlowDataNamespace:
    def __init__(
        self,
        *,
        getter: Callable[..., Any],
        setter: Callable[..., Any],
        appender: Callable[..., Any],
        deleter: Callable[..., Any],
        async_setter: Callable[..., Any],
        async_appender: Callable[..., Any],
        async_deleter: Callable[..., Any],
    ):
        self._getter = getter
        self._setter = setter
        self._appender = appender
        self._deleter = deleter
        self.async_set = async_setter
        self.async_append = async_appender
        self.async_del = async_deleter

    def get(
        self,
        key: Any | None = None,
        default: Any = None,
        *,
        inherit: bool = True,
    ):
        return self._getter(key, default, inherit=inherit)

    def set(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        return self._setter(key, value, emit=emit)

    def append(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        return self._appender(key, value, emit=emit)

    def delete(
        self,
        key: str,
        *,
        emit: bool = True,
    ):
        return self._deleter(key, emit=emit)

    def to_dict(self, *, inherit: bool = True):
        data = self.get(None, {}, inherit=inherit)
        return data if isinstance(data, dict) else {}

    def keys(self):
        return self.to_dict().keys()

    def values(self):
        return self.to_dict().values()

    def items(self):
        return self.to_dict().items()

    def __getitem__(self, key: Any):
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key: Any):
        return self.get(key, _MISSING) is not _MISSING

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self):
        return len(self.to_dict())


class _TriggerFlowResourcesView(Mapping[str, Any]):
    def __init__(self, execution: "TriggerFlowExecution"):
        self._execution = execution

    def to_dict(self):
        return self._execution.get_runtime_resources()

    def __getitem__(self, key: str):
        value = self._execution.get_runtime_resource(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self):
        return len(self.to_dict())

    def get(self, key: str, default: Any = None):
        return self._execution.get_runtime_resource(key, default)


class _TriggerFlowSignalInfo:
    def __init__(
        self,
        *,
        trigger_event: str,
        trigger_type: Literal["event", "runtime_data", "flow_data", "collect"],
        value: Any,
        signal: "TriggerFlowSignal | None",
        signal_id: str | None,
        signal_source: str | None,
        signal_meta: dict[str, Any],
    ):
        self.trigger_event = trigger_event
        self.trigger_type = trigger_type
        self.value = value
        self.signal = signal
        self.signal_id = signal_id
        self.signal_source = signal_source
        self.signal_meta = signal_meta

    @property
    def event(self):
        return self.trigger_event

    @property
    def type(self):
        return self.trigger_type

    @property
    def source(self):
        return self.signal_source

    @property
    def meta(self):
        return self.signal_meta

    def to_dict(self):
        return {
            "trigger_event": self.trigger_event,
            "trigger_type": self.trigger_type,
            "value": self.value,
            "signal_id": self.signal_id,
            "signal_source": self.signal_source,
            "signal_meta": self.signal_meta.copy(),
        }


class _TriggerFlowResumeInfo:
    def __init__(self, data: dict[str, Any] | None):
        payload = data if isinstance(data, dict) else {}
        self.interrupt_id = payload.get("interrupt_id")
        self.value = payload.get("value")
        self.interrupt = payload.get("interrupt")
        self.origin_signal = payload.get("origin_signal")

    def to_dict(self):
        return {
            "interrupt_id": self.interrupt_id,
            "value": self.value,
            "interrupt": self.interrupt,
            "origin_signal": self.origin_signal,
        }


class TriggerFlowRuntimeData(Generic[ValueT, StreamT, ResultT]):
    put: Callable[[StreamT], None]
    async_put: Callable[[StreamT], Any]
    put_into_stream: Callable[[StreamT], None]
    async_put_into_stream: Callable[[StreamT], Any]
    set_result: Callable[[ResultT], None]
    get_result: Callable[..., ResultT | None]

    @property
    def value(self) -> ValueT:
        return self._value

    @value.setter
    def value(self, value: ValueT):
        self._value = value

    @property
    def input(self) -> ValueT:
        return self._value

    @input.setter
    def input(self, value: ValueT):
        self._value = value

    @property
    def inputs(self) -> ValueT:
        return self._value

    @inputs.setter
    def inputs(self, value: ValueT):
        self._value = value

    def __init__(
        self,
        *,
        trigger_event: str,
        trigger_type: Literal["event", "runtime_data", "flow_data", "collect"],
        value: ValueT,
        execution: "TriggerFlowExecution",
        _layer_marks: list[str] | None = None,
        signal: "TriggerFlowSignal | None" = None,
        chunk_run_context: "RunContext | None" = None,
    ):
        self.trigger_event = trigger_event
        self.trigger_type: Literal["event", "runtime_data", "flow_data", "collect"] = trigger_type
        self.event = trigger_event
        self.type = trigger_type
        self.value = value
        self.execution = execution
        self.execution_id = execution.id
        self._layer_marks = _layer_marks if _layer_marks is not None else []
        self.settings = execution.settings
        self.signal = signal
        self.signal_id = signal.id if signal is not None else None
        self.signal_source = signal.source if signal is not None else None
        self.signal_meta = signal.meta.copy() if signal is not None else {}
        resume_context = self.signal_meta.get("resume") if isinstance(self.signal_meta, dict) else None
        self.is_resume = isinstance(resume_context, dict)
        self.resume = _TriggerFlowResumeInfo(resume_context if isinstance(resume_context, dict) else None)
        self.signal_info = _TriggerFlowSignalInfo(
            trigger_event=trigger_event,
            trigger_type=trigger_type,
            value=value,
            signal=signal,
            signal_id=self.signal_id,
            signal_source=self.signal_source,
            signal_meta=self.signal_meta.copy(),
        )
        self.chunk_run_context = chunk_run_context

        self.get_flow_data = execution.get_flow_data
        self.set_flow_data = execution.set_flow_data
        self.append_flow_data = execution.append_flow_data
        self.del_flow_data = execution.del_flow_data
        self.async_set_flow_data = execution.async_set_flow_data
        self.async_append_flow_data = execution.async_append_flow_data
        self.async_del_flow_data = execution.async_del_flow_data

        self.get_runtime_data = execution.get_runtime_data
        self.set_runtime_data = execution.set_runtime_data
        self.append_runtime_data = execution.append_runtime_data
        self.del_runtime_data = execution.del_runtime_data
        self.async_set_runtime_data = execution.async_set_runtime_data
        self.async_append_runtime_data = execution.async_append_runtime_data
        self.async_del_runtime_data = execution.async_del_runtime_data
        self.get_state = execution.get_state
        self.set_state = execution.set_state
        self.append_state = execution.append_state
        self.del_state = execution.del_state
        self.async_set_state = execution.async_set_state
        self.async_append_state = execution.async_append_state
        self.async_del_state = execution.async_del_state
        self.state = _TriggerFlowDataNamespace(
            getter=self.get_state,
            setter=self.set_state,
            appender=self.append_state,
            deleter=self.del_state,
            async_setter=self.async_set_state,
            async_appender=self.async_append_state,
            async_deleter=self.async_del_state,
        )
        self.flow_state = _TriggerFlowDataNamespace(
            getter=self.get_flow_data,
            setter=self.set_flow_data,
            appender=self.append_flow_data,
            deleter=self.del_flow_data,
            async_setter=self.async_set_flow_data,
            async_appender=self.async_append_flow_data,
            async_deleter=self.async_del_flow_data,
        )
        self.resources = _TriggerFlowResourcesView(execution)

        self.get_resource = execution.get_runtime_resource
        self.require_resource = execution.require_runtime_resource
        self.set_resource = execution.set_runtime_resource
        self.del_resource = execution.del_runtime_resource

        def _origin_chunk_payload():
            if self.chunk_run_context is None:
                return None
            return {
                "run_id": self.chunk_run_context.run_id,
                "chunk_id": self.chunk_run_context.meta.get("chunk_id"),
                "chunk_name": self.chunk_run_context.meta.get("chunk_name"),
                "operator_kind": self.chunk_run_context.meta.get("operator_kind"),
            }

        def _default_intervention_consumer():
            origin_chunk = _origin_chunk_payload()
            if isinstance(origin_chunk, dict):
                chunk_name = origin_chunk.get("chunk_name")
                if chunk_name:
                    return str(chunk_name)
                chunk_id = origin_chunk.get("chunk_id")
                if chunk_id:
                    return str(chunk_id)
            return "chunk"

        def _chunk_signal_meta(meta: dict[str, Any] | None = None):
            origin_chunk = _origin_chunk_payload()
            merged_meta = dict(meta) if isinstance(meta, dict) else {}
            if self.signal_id is not None:
                inherited_scope = self.signal_meta.get(AGGREGATION_SCOPE_META_KEY)
                merged_meta.setdefault(PARENT_SIGNAL_ID_META_KEY, self.signal_id)
                merged_meta.setdefault(
                    AGGREGATION_SCOPE_META_KEY,
                    inherited_scope if inherited_scope is not None else self.signal_id,
                )
            if origin_chunk is not None:
                merged_meta.setdefault("origin_chunk", origin_chunk)
            return merged_meta

        def _emit_from_chunk(
            trigger_event: str,
            value: Any = None,
            _layer_marks: list[str] | None = None,
            **kwargs,
        ):
            kwargs.setdefault("_source", "chunk")
            kwargs["_meta"] = _chunk_signal_meta(kwargs.get("_meta"))
            layer_marks = self._layer_marks.copy() if _layer_marks is None else _layer_marks
            return execution.emit(trigger_event, value, layer_marks, **kwargs)

        async def _async_emit_from_chunk(
            trigger_event: str,
            value: Any = None,
            _layer_marks: list[str] | None = None,
            **kwargs,
        ):
            kwargs.setdefault("_source", "chunk")
            kwargs["_meta"] = _chunk_signal_meta(kwargs.get("_meta"))
            layer_marks = self._layer_marks.copy() if _layer_marks is None else _layer_marks
            return await execution.async_emit(trigger_event, value, layer_marks, **kwargs)

        def _emit_nowait_from_chunk(
            trigger_event: str,
            value: Any = None,
            _layer_marks: list[str] | None = None,
            **kwargs,
        ):
            kwargs.setdefault("_source", "chunk")
            kwargs["_meta"] = _chunk_signal_meta(kwargs.get("_meta"))
            layer_marks = self._layer_marks.copy() if _layer_marks is None else _layer_marks
            return execution.emit_nowait(trigger_event, value, layer_marks, **kwargs)

        async def _async_emit_nowait_from_chunk(
            trigger_event: str,
            value: Any = None,
            _layer_marks: list[str] | None = None,
            **kwargs,
        ):
            kwargs.setdefault("_source", "chunk")
            kwargs["_meta"] = _chunk_signal_meta(kwargs.get("_meta"))
            layer_marks = self._layer_marks.copy() if _layer_marks is None else _layer_marks
            return await execution.async_emit_nowait(trigger_event, value, layer_marks, **kwargs)

        self.emit = _emit_from_chunk
        self.async_emit = _async_emit_from_chunk
        self.emit_nowait = _emit_nowait_from_chunk
        self.async_emit_nowait = _async_emit_nowait_from_chunk

        self.put = lambda stream_item: execution.put_into_stream(
            stream_item,
            _origin_chunk=_origin_chunk_payload(),
        )
        self.async_put = lambda stream_item: execution.async_put_into_stream(
            stream_item,
            _origin_chunk=_origin_chunk_payload(),
        )
        self.put_into_stream = self.put
        self.async_put_into_stream = self.async_put
        self.stop_stream = execution.stop_stream
        self.async_stop_stream = execution.async_stop_stream

        self.pause_for = execution.pause_for
        self.async_pause_for = execution.async_pause_for
        self.continue_with = execution.continue_with
        self.async_continue_with = execution.async_continue_with
        self.get_status = execution.get_status
        self.is_waiting = execution.is_waiting
        self.get_interrupt = execution.get_interrupt
        self.get_pending_interrupts = execution.get_pending_interrupts
        self.interventions = execution._get_visible_interventions_snapshot()
        self.get_interventions = self._get_visible_interventions
        self.get_latest_intervention = self._get_latest_visible_intervention

        def _mark_intervention_consumed_from_chunk(
            intervention_id: str,
            *,
            consumer: str | None = None,
            status: Literal["applied", "ignored"] = "applied",
            note: str | None = None,
            metadata: dict[str, Any] | None = None,
        ):
            return execution.mark_intervention_consumed(
                intervention_id,
                consumer=consumer if consumer is not None else _default_intervention_consumer(),
                status=status,
                note=note,
                metadata=metadata,
            )

        async def _async_mark_intervention_consumed_from_chunk(
            intervention_id: str,
            *,
            consumer: str | None = None,
            status: Literal["applied", "ignored"] = "applied",
            note: str | None = None,
            metadata: dict[str, Any] | None = None,
        ):
            return await execution.async_mark_intervention_consumed(
                intervention_id,
                consumer=consumer if consumer is not None else _default_intervention_consumer(),
                status=status,
                note=note,
                metadata=metadata,
            )

        self.mark_intervention_consumed = _mark_intervention_consumed_from_chunk
        self.async_mark_intervention_consumed = _async_mark_intervention_consumed_from_chunk

        self.set_result = lambda result: execution.set_result(
            result,
            _origin_chunk=_origin_chunk_payload(),
        )
        self.get_result = execution.get_result
        self.get_last_signal = execution.get_last_signal

        self._system_runtime_data = execution._system_runtime_data

    @property
    def upper_layer_mark(self):
        return self._layer_marks[-2] if len(self._layer_marks) > 1 else None

    @property
    def layer_mark(self):
        return self._layer_marks[-1] if len(self._layer_marks) > 0 else None

    def layer_in(self):
        self._layer_marks.append(uuid.uuid4().hex)

    def layer_out(self):
        self._layer_marks = self._layer_marks[:-1] if len(self._layer_marks) > 0 else []

    def _copy_intervention(self, intervention: dict[str, Any]):
        return StateData({"value": intervention}).get("value")

    def _get_visible_interventions(
        self,
        status: str | None = None,
        target: str | None = None,
        since_version: int | None = None,
    ):
        interventions = []
        for intervention in self.interventions:
            if status is not None and intervention.get("status") != status:
                continue
            if target is not None and intervention.get("target") != target:
                continue
            if since_version is not None and int(intervention.get("version", 0)) <= since_version:
                continue
            interventions.append(self._copy_intervention(intervention))
        return interventions

    def _get_latest_visible_intervention(self, default: Any = None, **filters: Any):
        interventions = self._get_visible_interventions(**filters)
        if not interventions:
            return default
        return interventions[-1]


TriggerFlowEventData = TriggerFlowRuntimeData
TriggerFlowHandler = Callable[[TriggerFlowRuntimeData[Any, Any, Any]], Any]
TriggerFlowHandlers = dict[str, dict[str, TriggerFlowHandler]]
TriggerFlowAllHandlers = dict[Literal["event", "flow_data", "runtime_data"], TriggerFlowHandlers]

RUNTIME_STREAM_STOP = AVOID_COPY()
