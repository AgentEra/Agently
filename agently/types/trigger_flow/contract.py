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

from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Protocol, TypeAlias, TypeVar, runtime_checkable

from pydantic import TypeAdapter
from typing_extensions import NotRequired, TypedDict

InputT = TypeVar("InputT")
StreamT = TypeVar("StreamT")
ResultT = TypeVar("ResultT")


class TriggerFlowContractEntry(TypedDict):
    label: str
    schema: dict[str, Any] | None


class TriggerFlowInterrupt(TypedDict):
    id: str
    type: str
    status: Literal["waiting", "resumed", "cancelled"]
    payload: NotRequired[Any]
    resume_event: NotRequired[str | None]
    resume_to: NotRequired[Any]
    response: NotRequired[Any]
    resume_count: NotRequired[int]
    max_resumes: NotRequired[int | None]
    resume_request_id: NotRequired[str | None]
    resume_requests: NotRequired[dict[str, Any]]
    resumed_by: NotRequired[str | None]
    local_interrupt_id: NotRequired[str | None]
    source_execution_id: NotRequired[str | None]
    source_flow_name: NotRequired[str | None]
    source_operator_id: NotRequired[str | None]
    source_signal: NotRequired[dict[str, Any] | None]
    continuation_event: NotRequired[str | None]
    sub_flow_frame_id: NotRequired[str | None]


class TriggerFlowResourceRequirement(TypedDict):
    kind: Literal["runtime_resource", "managed_execution_environment", "execution_environment_requirement"]
    key: str
    required: bool
    source: NotRequired[Literal["flow", "execution", "managed", "external"] | str]
    metadata: NotRequired[dict[str, Any]]


class TriggerFlowExecutionSnapshot(TypedDict, total=False):
    schema_version: int
    kind: Literal["triggerflow.execution_snapshot"]
    snapshot_id: str
    created_at: float
    execution_id: str
    flow_name: str | None
    flow_definition_fingerprint: str
    status: str
    lifecycle_state: str
    state_version: int
    owner_id: str | None
    lease: dict[str, Any]
    run_context: dict[str, Any]
    runtime_data: dict[str, Any]
    flow_data: dict[str, Any]
    interrupts: dict[str, Any]
    intervention: dict[str, Any]
    sub_flow_frames: dict[str, Any]
    last_signal: dict[str, Any] | None
    result: dict[str, Any]
    durable_system_state: dict[str, Any]
    resource_requirements: list[TriggerFlowResourceRequirement]
    resource_keys: list[str]
    managed_resource_keys: list[str]
    execution_environment_requirement_ids: list[str]
    resume_ledger: dict[str, Any]


class TriggerFlowExecutionRehydration(TypedDict, total=False):
    snapshot: TriggerFlowExecutionSnapshot
    execution_id: str
    status: Literal["ready", "missing_resources", "invalid_snapshot"]
    ready: bool
    runtime_resources: dict[str, Any]
    current_flow_definition_fingerprint: str
    missing_resource_keys: list[str]
    resolved_resource_keys: list[str]
    pending_environment_resource_keys: list[str]
    resource_requirements: list[TriggerFlowResourceRequirement]
    execution_environment_requirements: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]


@runtime_checkable
class TriggerFlowCheckpointStore(Protocol):
    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
    ) -> Any: ...


class TriggerFlowInterruptEvent(TypedDict):
    type: Literal["interrupt"]
    action: Literal["pause", "resume", "project"]
    execution_id: str
    interrupt: TriggerFlowInterrupt
    signal: NotRequired[dict[str, Any] | None]
    value: NotRequired[Any]


class TriggerFlowInterventionConsumer(TypedDict):
    status: Literal["applied", "ignored"]
    note: str | None
    metadata: dict[str, Any]
    consumed_at: float


class TriggerFlowIntervention(TypedDict):
    id: str
    version: int
    status: Literal["pending", "inserted", "expired", "rejected"]
    payload: Any
    target: NotRequired[str | None]
    author: NotRequired[str | None]
    note: NotRequired[str | None]
    metadata: dict[str, Any]
    created_at: float
    inserted_at: float | None
    insertion: dict[str, Any] | None
    rejected_at: float | None
    reject_reason: str | None
    expired_at: float | None
    consumers: dict[str, TriggerFlowInterventionConsumer]


class TriggerFlowInterventionEvent(TypedDict):
    type: Literal["intervention"]
    action: Literal["append", "insert", "expire", "consume", "reject"]
    execution_id: str
    intervention: TriggerFlowIntervention


TriggerFlowSystemStreamEvent: TypeAlias = TriggerFlowInterruptEvent | TriggerFlowInterventionEvent
TRIGGER_FLOW_INTERRUPT_EVENT_SCHEMA = TypeAdapter(TriggerFlowInterruptEvent).json_schema()
TRIGGER_FLOW_INTERVENTION_EVENT_SCHEMA = TypeAdapter(TriggerFlowInterventionEvent).json_schema()


class TriggerFlowSystemStreamMetadata(TypedDict, total=False):
    interrupt: TriggerFlowContractEntry
    intervention: TriggerFlowContractEntry


class TriggerFlowContractMetadata(TypedDict, total=False):
    initial_input: TriggerFlowContractEntry | None
    stream: TriggerFlowContractEntry | None
    result: TriggerFlowContractEntry | None
    meta: dict[str, Any]
    system_stream: TriggerFlowSystemStreamMetadata


@dataclass(frozen=True)
class TriggerFlowContractSpec(Generic[InputT, StreamT, ResultT]):
    initial_input: Any | None = None
    stream: Any | None = None
    result: Any | None = None
    meta: dict[str, Any] = field(default_factory=dict)
