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

from agently_stage import default_stage_call_bridge

import uuid

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agently.types.trigger_flow import TriggerFlowHandler, TriggerFlowRuntimeData
    from .BluePrint import TriggerFlowBlueprint

from .Control import TriggerFlowPauseSignal


class TriggerFlowChunk:
    def __init__(
        self,
        handler: "TriggerFlowHandler",
        *,
        chunk_id: str | None = None,
        name: str | None = None,
        trigger: str | None = None,
        callable_ref: dict | None = None,
        blueprint: "TriggerFlowBlueprint | None" = None,
        emit_signals: list[str] | None = None,
    ):
        self.id = chunk_id if chunk_id is not None else uuid.uuid4().hex
        self.name = name if name is not None else self.id
        self._handler = handler
        self.trigger = trigger if trigger is not None else f"Chunk[{ handler.__name__ }]-{ self.id }"
        self.callable_ref = callable_ref.copy() if isinstance(callable_ref, dict) else None
        self._blue_print = blueprint
        self._emit_signals = list(dict.fromkeys(str(signal) for signal in (emit_signals or [])))

    async def async_call(self, data: "TriggerFlowRuntimeData"):
        result = await default_stage_call_bridge.as_async(self._handler, managed=True)(data)
        if isinstance(result, TriggerFlowPauseSignal) or data.execution.is_waiting():
            return result
        await data.async_emit(self.trigger, result, _layer_marks=data._layer_marks.copy(), _source="chunk")
        return result

    def call(self, data: "TriggerFlowRuntimeData"):
        result = default_stage_call_bridge.as_sync(self._handler, managed=True)(data)
        if isinstance(result, TriggerFlowPauseSignal) or data.execution.is_waiting():
            return result
        data.emit(self.trigger, result, _layer_marks=data._layer_marks.copy(), _source="chunk")
        return result

    @property
    def emit_signals(self):
        return self._emit_signals.copy()

    def declare_emits(self, *signals: str):
        normalized = []
        for signal in signals:
            if not isinstance(signal, str):
                raise TypeError(
                    f"TriggerFlowChunk.declare_emits() only accepts string event names, got: { type(signal) }"
                )
            normalized.append(signal)
        self._emit_signals = list(dict.fromkeys([*self._emit_signals, *normalized]))
        if self._blue_print is not None:
            self._blue_print.sync_chunk_definition(self)
        return self
