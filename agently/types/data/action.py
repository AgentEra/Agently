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

from typing import Any, Awaitable, Callable, Literal
from typing_extensions import TypedDict

from .tool import KwargsType, ReturnType
from .execution_environment import ExecutionEnvironmentRequirement

ActionStatus = Literal["success", "error", "approval_required", "blocked", "skipped"]
ActionSideEffectLevel = Literal["read", "write", "exec"]


class ActionPolicy(TypedDict, total=False):
    approval_mode: Literal["auto", "always", "never"]
    workspace_roots: list[str]
    path_allowlist: list[str]
    path_denylist: list[str]
    allowed_cmd_prefixes: list[str]
    network_mode: Literal["inherit", "enabled", "disabled"]
    timeout_seconds: float
    max_output_bytes: int
    read_only: bool
    allow_create: bool
    allow_update: bool
    allow_delete: bool


class ActionApproval(TypedDict, total=False):
    required: bool
    reason: str
    approval_mode: str
    missing_permissions: list[str]
    suggested_policy: ActionPolicy
    message: str


class ActionArtifact(TypedDict, total=False):
    artifact_id: str
    action_call_id: str
    label: str
    artifact_type: str
    path: str
    media_type: str
    preview: Any
    value: Any
    truncated: bool
    full_value_available: bool
    available: bool
    size: int
    meta: dict[str, Any]


class ActionDiagnostic(TypedDict, total=False):
    source: str
    severity: str
    code: str
    message: str
    path: str
    line: int
    column: int
    end_line: int
    end_column: int
    symbol: str
    suggestion: str
    meta: dict[str, Any]


class ActionSpec(TypedDict, total=False):
    action_id: str
    name: str
    desc: str
    kwargs: KwargsType
    returns: ReturnType
    tags: list[str]
    default_policy: ActionPolicy
    side_effect_level: ActionSideEffectLevel
    approval_required: bool
    sandbox_required: bool
    replay_safe: bool
    expose_to_model: bool
    executor_type: str
    execution_environments: list[ExecutionEnvironmentRequirement]
    meta: dict[str, Any]


class ActionCall(TypedDict, total=False):
    purpose: str
    action_id: str
    action_input: dict[str, Any]
    policy_override: ActionPolicy
    source_protocol: str
    todo_suggestion: str
    next: str
    tool_name: str
    tool_kwargs: dict[str, Any]
    execution_environment_handles: dict[str, Any]
    execution_environment_resources: dict[str, Any]


class ActionDecision(TypedDict, total=False):
    next_action: str
    use_action: bool
    next: str
    execution_actions: list[ActionCall]
    action_call: ActionCall
    action_calls: list[ActionCall]
    execution_commands: list[ActionCall]
    tool_command: ActionCall
    tool_commands: list[ActionCall]


class ActionResult(TypedDict, total=False):
    action_call_id: str
    ok: bool
    status: ActionStatus
    purpose: str
    action_id: str
    tool_name: str
    kwargs: dict[str, Any]
    todo_suggestion: str
    next: str
    success: bool
    result: Any
    data: Any
    model_digest: dict[str, Any]
    artifact_refs: list[ActionArtifact]
    artifacts: list[ActionArtifact]
    diagnostics: list[ActionDiagnostic]
    approval: ActionApproval
    timing: dict[str, Any]
    meta: dict[str, Any]
    redaction_report: list[str]
    error: str
    expose_to_model: bool
    side_effect_level: ActionSideEffectLevel
    executor_type: str


class _ActionRunContextRequired(TypedDict):
    prompt: Any
    settings: Any


class ActionRunContext(_ActionRunContextRequired, total=False):
    agent_name: str
    round_index: int
    max_rounds: int | None
    done_plans: list[ActionResult]
    last_round_records: list[ActionResult]
    parent_run_context: Any
    action: Any
    runtime: Any


class ActionPlanningRequest(TypedDict, total=False):
    action_list: list[dict[str, Any]]
    planning_protocol: str | None


class ActionExecutionRequest(TypedDict, total=False):
    action_calls: list[ActionCall]
    async_call_action: Callable[[str, dict[str, Any]], Awaitable[Any]]
    concurrency: int | None
    timeout: float | None
