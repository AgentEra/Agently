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

from typing import Any, Literal, TypeAlias
from typing_extensions import NotRequired, TypedDict

from .record_store import RecordRef


AgentExecutionStatus: TypeAlias = Literal["created", "running", "success", "blocked", "error", "cancelled"] | str
AgentExecutionRecordPurpose: TypeAlias = Literal["process", "deliverable", "recovery", "audit"]


class AgentExecutionLineage(TypedDict):
    task_id: str | None
    iteration_id: str | None
    step_id: str | None
    parent_execution_id: str | None
    scope: dict[str, Any]


class AgentExecutionLimits(TypedDict):
    allow_create_task: bool
    max_model_requests: int | None
    max_nested_agent_steps: int | None
    max_seconds: float | None
    max_no_progress_seconds: float | None


class AgentExecutionRecordRefs(TypedDict):
    observations: list[str]
    artifacts: list[str]
    decisions: list[str]
    checkpoints: list[str]
    verification_evidence: list[str]


class AgentExecutionDiagnostics(TypedDict):
    budget: dict[str, Any]
    limit_events: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    stalls: list[dict[str, Any]]
    timeouts: list[dict[str, Any]]
    stages: dict[str, Any]
    last_progress: dict[str, Any]
    required_capabilities: list[dict[str, Any]]
    task_workspace_retention: NotRequired[dict[str, Any]]
    action_artifact_release: NotRequired["ActionArtifactReleaseDiagnostics"]
    long_output: NotRequired[dict[str, Any]]


class ActionArtifactReleaseDiagnostic(TypedDict):
    code: str
    message: str


class ActionArtifactReleaseDiagnostics(TypedDict, total=False):
    status: Literal["released", "deferred", "failed"]
    scope: dict[str, str]
    released_count: int
    preserved_artifact_ids: list[str]
    diagnostics: list[ActionArtifactReleaseDiagnostic]


class AgentExecutionRouteInfo(TypedDict):
    selected_route: str
    selected_by: str | None
    options: dict[str, Any]
    reusable: bool


class AgentExecutionActionLog(TypedDict, total=False):
    action_call_id: str | None
    action_id: str
    status: str
    success: bool | None
    source: str
    route: str
    data: dict[str, Any]
    model_digest: dict[str, Any]
    artifact_refs: list[dict[str, Any]]


class AgentExecutionMeta(TypedDict):
    execution_id: str
    status: AgentExecutionStatus
    strategy: str | None
    goals: list[str]
    success_criteria: list[str]
    generated_success_criteria: list[str]
    task_refs: dict[str, Any]
    iterations: list[dict[str, Any]]
    lineage: AgentExecutionLineage
    limits: AgentExecutionLimits
    options: dict[str, Any]
    effective_options: dict[str, Any]
    consumed_options: dict[str, Any]
    route_plan: dict[str, Any]
    route: AgentExecutionRouteInfo
    close_snapshot: dict[str, Any]
    logs: dict[str, Any]
    diagnostics: AgentExecutionDiagnostics
    record_refs: AgentExecutionRecordRefs
    long_output: NotRequired[dict[str, Any]]


CapabilityKind: TypeAlias = Literal["action"]
CapabilityRoute: TypeAlias = Literal["model_request", "agent_task"]
GuidanceAccess: TypeAlias = Literal["prompt_bound", "route_context", "context_pack", "summary_only", "none"]


class PlannerCapabilityCandidate(TypedDict, total=False):
    """One planner-facing executable Action candidate, sanitized to inert data."""

    id: str
    kind: CapabilityKind
    route: CapabilityRoute
    guidance_access: GuidanceAccess
    mode: str
    execution_resource_requirements: list[dict[str, Any]]
    evidence_requirement_kind: EvidenceRequirementKind
    description: str


class PlannerCapabilitySummary(TypedDict, total=False):
    """Sanitized planner-facing capability snapshot injected into AgentTask options.

    A typed, inert snapshot (capability ids + descriptions, no plugin objects),
    computed once at task construction from the top-level routing execution.
    AgentTask consumes only this snapshot; it must not import AgentOrchestrator
    or HybridRoutePlanner internals or hold an execution-draft reference.
    """

    capabilities: list[PlannerCapabilityCandidate]


# Evidence-requirement kinds. Only the kinds with a deterministic structural
# check are enforced by the AgentTask host guard today
# (`capability_used`, `action_succeeded`); the remainder are reserved contract
# vocabulary that the host guard does not yet enforce and that the model verifier
# may treat advisorily, so a requirement never claims a guarantee the guard
# cannot actually make.
EvidenceRequirementKind: TypeAlias = Literal[
    "capability_used",
    "action_succeeded",
    "artifact_readback",
    "validation_passed",
    "source_referenced",
]
_ENFORCED_EVIDENCE_REQUIREMENT_KINDS: frozenset[str] = frozenset({"capability_used", "action_succeeded"})


class EvidenceRequirement(TypedDict, total=False):
    """One structured completion-evidence requirement authored for a task.

    The trigger for the load-bearing verifier gate: a deterministic
    requirement-vs-evidence correspondence the host guard checks, never a
    free-text reading of success criteria. `capability_id` names the capability
    that must appear in execution evidence; `kind` selects which evidence
    bucket; `required` gates acceptance; `source` records provenance.
    `criterion_id` is optional and only used when criteria carry stable ids.
    """

    capability_id: str
    capability_kind: CapabilityKind
    kind: EvidenceRequirementKind
    required: bool
    source: Literal["host", "criterion", "policy"]
    criterion_id: str


class AgentExecutionStreamMeta(TypedDict, total=False):
    execution_id: str
    lineage: AgentExecutionLineage


class AgentExecutionRecordWrite(TypedDict):
    record: RecordRef
    checkpoint: RecordRef | None
    record_refs: AgentExecutionRecordRefs
