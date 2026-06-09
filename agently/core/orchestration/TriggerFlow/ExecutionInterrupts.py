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


import time
import uuid
import warnings
from typing import Any, TYPE_CHECKING

from agently.types.trigger_flow.runtime_keys import SELF_RESUME_COUNT_META_KEY, SELF_RESUME_MAX_META_KEY

from .Control import (
    TRIGGER_FLOW_LIFECYCLE_OPEN,
    TRIGGER_FLOW_STATUS_CANCELLED,
    TRIGGER_FLOW_STATUS_RUNNING,
    TRIGGER_FLOW_STATUS_WAITING,
    TriggerFlowPauseSignal,
)
from .Signal import TriggerFlowSignal

if TYPE_CHECKING:
    from .Execution import TriggerFlowExecution


class TriggerFlowExecutionInterrupts:
    def __init__(self, execution: "TriggerFlowExecution[Any, Any, Any]"):
        self._execution = execution

    def is_waiting(self):
        return self._execution._status == TRIGGER_FLOW_STATUS_WAITING

    def get_interrupts(self) -> dict[str, Any]:
        interrupts = self._execution._system_runtime_data.get("interrupts", {}, inherit=False)
        return interrupts if isinstance(interrupts, dict) else {}

    def get_interrupt(self, interrupt_id: str):
        return self.get_interrupts().get(interrupt_id)

    def get_pending_interrupts(self):
        return {
            interrupt_id: interrupt
            for interrupt_id, interrupt in self.get_interrupts().items()
            if isinstance(interrupt, dict) and interrupt.get("status") == "waiting"
        }

    def has_pending_interrupts(self):
        return bool(self.get_pending_interrupts())

    def refresh_waiting_status(self):
        execution = self._execution
        if self.has_pending_interrupts():
            execution._set_status(TRIGGER_FLOW_STATUS_WAITING)
        elif execution._status == TRIGGER_FLOW_STATUS_WAITING:
            execution._set_status(TRIGGER_FLOW_STATUS_RUNNING)

    def validate_pending_interrupt_close_policy(self, policy: str):
        if policy not in {"error", "cancel"}:
            raise ValueError("pending_interrupts must be one of: 'error', 'cancel'.")

    async def handle_pending_interrupts_before_close(
        self,
        *,
        pending_interrupts: str,
        reason: str,
    ):
        execution = self._execution
        pending = self.get_pending_interrupts()
        if not pending:
            return
        if pending_interrupts == "cancel":
            await self.cancel_pending_interrupts(reason=reason)
            return
        await execution._emit_runtime_event(
            "triggerflow.pending_interrupts_close_rejected",
            level="ERROR",
            message=f"TriggerFlow execution '{ execution.id }' can not close while pending interrupts are waiting.",
            payload={
                "reason": reason,
                "pending_interrupt_ids": sorted(pending),
                "pending_interrupts": execution._to_serializable_value(pending),
            },
        )
        raise RuntimeError(
            f"Can not close TriggerFlow execution { execution.id } while pending interrupts are waiting: "
            f"{ sorted(pending) }. Resume them with continue_with(...) or pass pending_interrupts='cancel'."
        )

    async def cancel_pending_interrupts(self, *, reason: str):
        execution = self._execution
        interrupts = self.get_interrupts().copy()
        cancelled: list[dict[str, Any]] = []
        cancelled_at = time.time()
        for interrupt_id, interrupt_state in list(interrupts.items()):
            if not isinstance(interrupt_state, dict):
                continue
            if interrupt_state.get("status") != "waiting":
                continue
            interrupt = dict(interrupt_state)
            interrupt["status"] = "cancelled"
            interrupt["cancelled_at"] = cancelled_at
            interrupt["cancel_reason"] = reason
            interrupts[interrupt_id] = interrupt
            cancelled.append(interrupt)
        if not cancelled:
            return
        execution._system_runtime_data.set("interrupts", interrupts)
        execution._set_status(TRIGGER_FLOW_STATUS_CANCELLED)
        execution._bump_state_version()
        execution._mark_activity()
        await execution._emit_runtime_event(
            "triggerflow.pending_interrupts_cancelled",
            level="WARNING",
            message=f"TriggerFlow execution '{ execution.id }' cancelled pending interrupts before close.",
            payload={
                "reason": reason,
                "interrupts": execution._to_serializable_value(cancelled),
            },
        )

    def build_resume_context(self, interrupt_id: str, interrupt: dict[str, Any], value: Any):
        return {
            "interrupt_id": interrupt_id,
            "value": value,
            "interrupt": self._execution._to_serializable_value(interrupt),
            "origin_signal": interrupt.get("source_signal"),
        }

    async def async_resume_for_signal(self, signal: TriggerFlowSignal):
        execution = self._execution
        if signal.trigger_type != "event" or signal.source == "interrupt":
            return
        interrupts = self.get_interrupts().copy()
        resumed_interrupts: list[dict[str, Any]] = []
        for interrupt_id, interrupt_state in interrupts.items():
            if not isinstance(interrupt_state, dict):
                continue
            if interrupt_state.get("status") != "waiting":
                continue
            resume_to = interrupt_state.get("resume_to")
            resume_event = interrupt_state.get("resume_event")
            target_event = resume_to.get("event") if isinstance(resume_to, dict) else resume_event
            if target_event != signal.trigger_event:
                continue
            interrupt = dict(interrupt_state)
            interrupt["status"] = "resumed"
            interrupt["response"] = signal.value
            interrupt["resume_value"] = signal.value
            interrupt["resumed_at"] = time.time()
            interrupt["resumed_by_signal_id"] = signal.id
            interrupts[interrupt_id] = interrupt
            resumed_interrupts.append(interrupt)

        if not resumed_interrupts:
            return

        execution._system_runtime_data.set("interrupts", interrupts)
        self.refresh_waiting_status()
        execution._bump_state_version()
        await execution._emit_runtime_event(
            "triggerflow.execution_resumed",
            message=f"TriggerFlow execution '{ execution.id }' resumed by event '{ signal.trigger_event }'.",
            payload={
                "signal": signal.to_debug_dict(),
                "interrupts": execution._to_serializable_value(resumed_interrupts),
            },
        )
        for interrupt in resumed_interrupts:
            await execution.async_put_into_stream(
                {
                    "type": "interrupt",
                    "action": "resume",
                    "execution_id": execution.id,
                    "interrupt": execution._to_serializable_value(interrupt),
                    "value": execution._to_serializable_value(signal.value),
                },
                _skip_contract_validation=True,
            )

    async def async_pause_for(
        self,
        *,
        type: str = "pause",
        payload: Any = None,
        resume_event: str | None = None,
        interrupt_id: str | None = None,
        resume_to: Any = None,
        max_resumes: int | None = 1,
    ):
        execution = self._execution
        if max_resumes is not None and (not isinstance(max_resumes, int) or max_resumes < 0):
            raise ValueError("max_resumes must be a non-negative integer or None.")
        if not execution._resume_handle_exposed:
            await execution._emit_runtime_event(
                "triggerflow.interrupt_unhandled",
                level="ERROR",
                message=(
                    f"TriggerFlow execution '{ execution.id }' can not pause because its resume handle is hidden."
                ),
                payload={
                    "type": type,
                    "payload": execution._to_serializable_value(payload),
                    "resume_event": resume_event,
                    "resume_to": execution._to_serializable_value(resume_to),
                },
            )
            raise RuntimeError(
                "TriggerFlow pause_for(...) requires an exposed execution handle. "
                "Use flow.create_execution()/flow.start_execution(), then handle "
                "get_pending_interrupts() and continue_with(...)."
            )
        interrupt_id = interrupt_id if interrupt_id is not None else uuid.uuid4().hex
        interrupts = self.get_interrupts().copy()
        current_signal = execution.get_last_signal()
        origin_chunk = execution._get_origin_chunk_payload()
        source_operator_id = origin_chunk.get("chunk_id") if isinstance(origin_chunk, dict) else None
        continuation_event = None
        if source_operator_id:
            operator = execution._get_handler_operator(str(source_operator_id))
            if isinstance(operator, dict):
                for signal in operator.get("emit_signals", []):
                    if isinstance(signal, dict) and signal.get("role") == "continuation":
                        continuation_event = signal.get("trigger_event")
                        break
        normalized_resume_to = resume_to
        if normalized_resume_to is None:
            normalized_resume_to = {"event": resume_event} if resume_event else "next"
        current_signal_meta = current_signal.meta if current_signal is not None else {}
        self_resume_count = current_signal_meta.get(SELF_RESUME_COUNT_META_KEY, 0)
        if not isinstance(self_resume_count, int) or self_resume_count < 0:
            self_resume_count = 0
        if normalized_resume_to == "self" and max_resumes is not None and self_resume_count >= max_resumes:
            await execution._emit_runtime_event(
                "triggerflow.self_resume_limit_reached",
                level="ERROR",
                message=(
                    f"TriggerFlow execution '{ execution.id }' reached the self resume limit "
                    f"for interrupt '{ interrupt_id }'."
                ),
                payload={
                    "interrupt_id": interrupt_id,
                    "resume_count": self_resume_count,
                    "max_resumes": max_resumes,
                    "source_signal": execution._serialize_signal(current_signal),
                },
            )
            raise RuntimeError(
                f"TriggerFlow self resume limit reached for interrupt '{ interrupt_id }': "
                f"resume_count={ self_resume_count }, max_resumes={ max_resumes }."
            )
        interrupt = {
            "id": interrupt_id,
            "type": type,
            "payload": payload,
            "resume_event": resume_event,
            "resume_to": normalized_resume_to,
            "status": "waiting",
            "source_execution_id": execution.id,
            "source_flow_name": execution._trigger_flow.name,
            "source_operator_id": source_operator_id,
            "source_signal": execution._serialize_signal(current_signal),
            "continuation_event": continuation_event,
            "resume_count": self_resume_count if normalized_resume_to == "self" else 0,
            "max_resumes": max_resumes if normalized_resume_to == "self" else None,
            "created_at": time.time(),
            "resumed_at": None,
            "resume_value": None,
        }
        interrupts[interrupt_id] = interrupt
        execution._system_runtime_data.set("interrupts", interrupts)
        execution._set_status(TRIGGER_FLOW_STATUS_WAITING)
        execution._bump_state_version()
        execution._mark_activity()
        await execution._emit_runtime_event(
            "triggerflow.interrupt_raised",
            level="WARNING",
            message=f"TriggerFlow execution '{ execution.id }' paused for interrupt '{ interrupt_id }'.",
            payload={"interrupt": execution._to_serializable_value(interrupt)},
        )
        await execution.async_put_into_stream(
            {
                "type": "interrupt",
                "action": "pause",
                "execution_id": execution.id,
                "interrupt": execution._to_serializable_value(interrupt),
                "signal": execution._serialize_signal(current_signal),
            },
            _skip_contract_validation=True,
        )
        return TriggerFlowPauseSignal(interrupt)

    def _resume_request_record(
        self,
        *,
        resume_request_id: str,
        value: Any,
        actor: str | None,
    ):
        return {
            "request_id": resume_request_id,
            "status": "accepted",
            "value": self._execution._to_serializable_value(value),
            "actor": actor,
            "accepted_at": time.time(),
        }

    def _same_resume_request_value(self, record: dict[str, Any], value: Any):
        return record.get("value") == self._execution._to_serializable_value(value)

    async def async_continue_with(
        self,
        interrupt_id: str,
        value: Any = None,
        *,
        resume_request_id: str | None = None,
        actor: str | None = None,
    ):
        execution = self._execution
        if execution._lifecycle_state != TRIGGER_FLOW_LIFECYCLE_OPEN:
            warnings.warn(
                f"TriggerFlow execution { execution.id } ignored continue_with() because lifecycle state is "
                f"'{ execution._lifecycle_state }'.",
                RuntimeWarning,
                stacklevel=2,
            )
            await execution._emit_runtime_event(
                "triggerflow.continue_rejected",
                level="WARNING",
                message=(
                    f"TriggerFlow execution '{ execution.id }' ignored continue_with() because lifecycle state is "
                    f"'{ execution._lifecycle_state }'."
                ),
                payload={
                    "lifecycle_state": execution._lifecycle_state,
                    "interrupt_id": interrupt_id,
                },
            )
            return None
        interrupts = self.get_interrupts().copy()
        if interrupt_id not in interrupts:
            raise KeyError(f"Can not continue execution { execution.id }, interrupt '{ interrupt_id }' not found.")
        interrupt = dict(interrupts[interrupt_id])
        resume_requests = interrupt.get("resume_requests", {})
        if not isinstance(resume_requests, dict):
            resume_requests = {}
        if resume_request_id is not None:
            resume_request_id = str(resume_request_id)
            existing_request = resume_requests.get(resume_request_id)
            if isinstance(existing_request, dict):
                if not self._same_resume_request_value(existing_request, value):
                    raise ValueError(
                        f"Can not continue execution { execution.id }, interrupt '{ interrupt_id }' with "
                        f"conflicting resume_request_id '{ resume_request_id }'."
                    )
                return interrupt
        if interrupt.get("status") != "waiting":
            raise ValueError(
                f"Can not continue execution { execution.id }, interrupt '{ interrupt_id }' is not waiting."
            )
        if resume_request_id is not None:
            resume_requests[resume_request_id] = self._resume_request_record(
                resume_request_id=resume_request_id,
                value=value,
                actor=actor,
            )
            interrupt["resume_requests"] = resume_requests
            interrupt["resume_request_id"] = resume_request_id
            interrupt["resumed_by"] = actor
        interrupt["status"] = "resumed"
        interrupt["response"] = value
        interrupt["resume_value"] = value
        interrupt["resumed_at"] = time.time()
        resume_to = interrupt.get("resume_to")
        if resume_to == "self":
            resume_count = interrupt.get("resume_count", 0)
            if not isinstance(resume_count, int) or resume_count < 0:
                resume_count = 0
            max_resumes = interrupt.get("max_resumes")
            if not isinstance(max_resumes, int):
                max_resumes = None
            resume_count += 1
            if max_resumes is not None and resume_count > max_resumes:
                await execution._emit_runtime_event(
                    "triggerflow.self_resume_limit_reached",
                    level="ERROR",
                    message=(
                        f"TriggerFlow execution '{ execution.id }' rejected continue_with() because "
                        f"interrupt '{ interrupt_id }' exceeded its self resume limit."
                    ),
                    payload={
                        "interrupt_id": interrupt_id,
                        "resume_count": resume_count,
                        "max_resumes": max_resumes,
                    },
                )
                raise RuntimeError(
                    f"TriggerFlow self resume limit reached for interrupt '{ interrupt_id }': "
                    f"resume_count={ resume_count }, max_resumes={ max_resumes }."
                )
            interrupt["resume_count"] = resume_count
        interrupts[interrupt_id] = interrupt
        execution._system_runtime_data.set("interrupts", interrupts)
        execution._set_status(TRIGGER_FLOW_STATUS_RUNNING)
        execution._bump_state_version()
        execution._mark_activity()
        await execution._emit_runtime_event(
            "triggerflow.execution_resumed",
            message=f"TriggerFlow execution '{ execution.id }' resumed from interrupt '{ interrupt_id }'.",
            payload={
                "interrupt_id": interrupt_id,
                "value": execution._to_serializable_value(value),
            },
        )
        await execution.async_put_into_stream(
            {
                "type": "interrupt",
                "action": "resume",
                "execution_id": execution.id,
                "interrupt": execution._to_serializable_value(interrupt),
                "value": execution._to_serializable_value(value),
            },
            _skip_contract_validation=True,
        )

        sub_flow_frame_id = interrupt.get("sub_flow_frame_id")
        if sub_flow_frame_id:
            return await execution._trigger_flow._blue_print.async_resume_sub_flow_frame(
                execution,
                str(sub_flow_frame_id),
                interrupt_id,
                value,
            )

        if isinstance(resume_to, dict) and resume_to.get("event"):
            await execution._async_dispatch_signal(
                execution._build_signal(
                    str(resume_to["event"]),
                    value,
                    trigger_type="event",
                    source="interrupt",
                    meta={
                        "interrupt_id": interrupt_id,
                        "resume": self.build_resume_context(interrupt_id, interrupt, value),
                    },
                )
            )
        elif resume_to == "self":
            source_signal = execution._restore_signal(interrupt.get("source_signal"))
            if source_signal is None:
                raise RuntimeError(
                    f"Can not resume execution { execution.id } interrupt '{ interrupt_id }' to self "
                    "because the original signal is missing."
                )
            await execution._async_dispatch_signal(
                execution._build_signal(
                    source_signal.trigger_event,
                    source_signal.value,
                    source_signal.layer_marks.copy(),
                    trigger_type=source_signal.trigger_type,
                    source="interrupt",
                    meta={
                        **source_signal.meta,
                        SELF_RESUME_COUNT_META_KEY: interrupt.get("resume_count", 0),
                        **(
                            {SELF_RESUME_MAX_META_KEY: interrupt["max_resumes"]}
                            if isinstance(interrupt.get("max_resumes"), int)
                            else {}
                        ),
                        "interrupt_id": interrupt_id,
                        "resume": self.build_resume_context(interrupt_id, interrupt, value),
                    },
                )
            )
        elif resume_to == "next":
            continuation_event = interrupt.get("continuation_event")
            if not continuation_event:
                raise RuntimeError(
                    f"Can not resume execution { execution.id } interrupt '{ interrupt_id }' to next "
                    "because the paused operator has no continuation event."
                )
            source_signal = execution._restore_signal(interrupt.get("source_signal"))
            await execution._async_dispatch_signal(
                execution._build_signal(
                    str(continuation_event),
                    value,
                    source_signal.layer_marks.copy() if source_signal is not None else None,
                    trigger_type="event",
                    source="interrupt",
                    meta={
                        "interrupt_id": interrupt_id,
                        "resume": self.build_resume_context(interrupt_id, interrupt, value),
                    },
                )
            )
        else:
            resume_event = interrupt.get("resume_event")
            if resume_event:
                await execution._async_dispatch_signal(
                    execution._build_signal(
                        str(resume_event),
                        value,
                        trigger_type="event",
                        source="interrupt",
                        meta={
                            "interrupt_id": interrupt_id,
                            "resume": self.build_resume_context(interrupt_id, interrupt, value),
                        },
                    )
                )
        self.refresh_waiting_status()
        return interrupt
