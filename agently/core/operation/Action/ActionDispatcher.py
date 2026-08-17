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

from agently_stage import default_stage_call_bridge

import asyncio
import inspect
import json
import uuid
from typing import Any, cast

from agently.core.operation.ExecutionResource import (
    ExecutionResourceApprovalDenied,
    ExecutionResourceApprovalRequired,
    ExecutionResourceError,
)
from agently.core.operation.PolicyApproval import merge_access_control_policy
from agently.core.runtime.RuntimeContext import bind_runtime_context
from agently.types.data import (
    ActionApproval,
    ActionCall,
    ActionDiagnostic,
    ActionPolicy,
    ActionResult,
    ActionSpec,
    ExecutionResourceHandle,
    ExecutionResourcePolicy,
    ExecutionResourceRequirement,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceAccessRequirement,
)
from agently.utils import Settings, SettingsNamespace

from .ActionRegistry import ActionRegistry


class ActionDispatcher:
    VALID_STATUSES = {"success", "partial_success", "error", "approval_required", "blocked", "skipped"}

    # Policy keys that only host code may set through a direct
    # async_execute_action(policy_override=...) call. A model-planned action
    # command must never carry these: setting them would let model output grant
    # its own approval or widen sandbox/network/path limits.
    HOST_ONLY_POLICY_KEYS = frozenset({
        "auto_allow",
        "policy_approval_granted",
        "policy_approval_decision",
        "policy_approval_handler",
        "approval_mode",
        "task_workspace_roots",
        "path_allowlist",
        "path_denylist",
        "allowed_cmd_prefixes",
        "network_mode",
        "read_only",
        "allow_create",
        "allow_update",
        "allow_delete",
        "timeout_seconds",
        "max_output_bytes",
        "sandbox_required",
    })

    # Source protocols whose action commands originate from model output and are
    # therefore untrusted for host-only policy keys.
    MODEL_PLANNING_PROTOCOLS = frozenset({"structured_plan", "native_tool_calls"})

    def __init__(self, registry: ActionRegistry, settings: Settings):
        self.registry = registry
        self.settings = settings

    @staticmethod
    def _to_dict(value: Any):
        return value if isinstance(value, dict) else {}

    @classmethod
    def _sanitize_policy_override(
        cls,
        policy_override: ActionPolicy | None,
        *,
        source_protocol: str,
    ) -> tuple[ActionPolicy, list[str]]:
        """Drop host-only policy keys from a model-sourced policy override.

        Returns the sanitized override plus the list of stripped keys so the
        caller can surface a diagnostic. Host-sourced protocols (direct/dry_run)
        keep their override untouched.
        """
        if not isinstance(policy_override, dict):
            return cast(ActionPolicy, {}), []
        if source_protocol not in cls.MODEL_PLANNING_PROTOCOLS:
            return cast(ActionPolicy, dict(policy_override)), []
        sanitized: dict[str, Any] = {}
        stripped: list[str] = []
        for key, value in policy_override.items():
            if key in cls.HOST_ONLY_POLICY_KEYS:
                stripped.append(str(key))
            else:
                sanitized[key] = value
        return cast(ActionPolicy, sanitized), stripped

    @staticmethod
    def _compact_diagnostic_value(value: Any, *, limit: int = 800) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        if len(text) <= limit:
            return text
        return f"{text[:limit]}... [truncated {len(text) - limit} chars]"

    @classmethod
    def _declared_action_input_keys(cls, spec: ActionSpec) -> set[str] | None:
        kwargs = spec.get("kwargs", {})
        if not isinstance(kwargs, dict):
            return None
        return {str(key) for key in kwargs.keys()}

    @classmethod
    def _host_only_action_input_keys(cls, spec: ActionSpec) -> set[str]:
        meta = spec.get("meta", {})
        if not isinstance(meta, dict):
            return set()
        raw_keys = meta.get("host_only_input_keys", [])
        if isinstance(raw_keys, str):
            return {raw_keys}
        if isinstance(raw_keys, (list, tuple, set)):
            return {str(key) for key in raw_keys if str(key)}
        return set()

    @staticmethod
    def _required_action_input_keys(spec: ActionSpec) -> set[str]:
        raw_keys: Any = spec.get("required_input_keys", [])
        if isinstance(raw_keys, str):
            return {raw_keys}
        if isinstance(raw_keys, (list, tuple, set)):
            return {str(key) for key in raw_keys if str(key)}
        return set()

    @classmethod
    def _sanitize_action_input(
        cls,
        spec: ActionSpec,
        action_input: dict[str, Any],
        *,
        source_protocol: str,
    ) -> tuple[dict[str, Any], list[str]]:
        if source_protocol not in cls.MODEL_PLANNING_PROTOCOLS:
            return dict(action_input), []
        declared_keys = cls._declared_action_input_keys(spec)
        host_only_keys = cls._host_only_action_input_keys(spec)
        sanitized: dict[str, Any] = {}
        stripped: list[str] = []
        for key, value in action_input.items():
            key_text = str(key)
            if key_text in host_only_keys:
                stripped.append(key_text)
            elif declared_keys is None or key_text in declared_keys:
                sanitized[key_text] = value
            else:
                stripped.append(key_text)
        return sanitized, sorted(stripped)

    @classmethod
    def _input_stripped_diagnostic(
        cls,
        *,
        source_protocol: str,
        stripped_keys: list[str],
        original_input: dict[str, Any],
        sanitized_input: dict[str, Any],
    ) -> ActionDiagnostic:
        return cast(ActionDiagnostic, {
            "source": "ActionDispatcher",
            "severity": "warning",
            "code": "action.input.unexpected_keys_stripped",
            "message": (
                "Ignored undeclared or host-only action input keys from a model-planned action command: "
                f"{ ', '.join(stripped_keys) }."
            ),
            "meta": {
                "source_protocol": source_protocol,
                "stripped_keys": stripped_keys,
                "original_kwargs_preview": cls._compact_diagnostic_value(original_input),
                "executed_kwargs_preview": cls._compact_diagnostic_value(sanitized_input),
            },
        })

    @classmethod
    def _exception_diagnostic(
        cls,
        *,
        code: str,
        message: str,
        error: BaseException | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ActionDiagnostic:
        diagnostic_meta: dict[str, Any] = dict(meta or {})
        if error is not None:
            diagnostic_meta.setdefault("exception_type", type(error).__name__)
            diagnostic_meta.setdefault("exception_message", cls._compact_diagnostic_value(str(error)))
        return cast(ActionDiagnostic, {
            "source": "ActionDispatcher",
            "severity": "error",
            "code": code,
            "message": message,
            "meta": diagnostic_meta,
        })

    @classmethod
    def _call_diagnostics(
        cls,
        action_call: ActionCall,
        *extra_diagnostics: ActionDiagnostic,
    ) -> list[ActionDiagnostic]:
        diagnostics = action_call.get("diagnostics")
        base = cast(list[ActionDiagnostic], list(diagnostics)) if isinstance(diagnostics, list) else []
        return [*base, *extra_diagnostics]

    def _merge_policy(
        self,
        settings: Settings,
        spec: ActionSpec,
        policy_override: ActionPolicy | None = None,
    ) -> ActionPolicy:
        action_settings = SettingsNamespace(settings, "action")
        merged: dict[str, Any] = {}
        for candidate in (
            action_settings.get("policy.global", {}),
            action_settings.get("policy.agent", action_settings.get("policy", {})),
            spec.get("default_policy", {}),
            policy_override or {},
        ):
            if isinstance(candidate, dict):
                merged.update(cast(dict[str, Any], candidate))
        return cast(ActionPolicy, merge_access_control_policy(merged, settings))

    def _approval_result(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        policy: ActionPolicy,
        reason: str,
        message: str,
    ) -> ActionResult:
        action_id = str(spec.get("action_id", ""))
        tool_name = str(spec.get("name", action_id))
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        approval: ActionApproval = {
            "required": True,
            "reason": reason,
            "approval_mode": str(policy.get("approval_mode", "auto")),
            "suggested_policy": policy,
            "message": message,
        }
        result: dict[str, Any] = {
            "action_call_id": str(action_call.get("action_call_id", "")),
            "ok": False,
            "status": "approval_required",
            "purpose": str(action_call.get("purpose", f"Use { action_id }")),
            "action_id": action_id,
            "tool_name": tool_name,
            "kwargs": dict(action_input),
            "todo_suggestion": str(action_call.get("todo_suggestion", "")),
            "next": str(action_call.get("next", action_call.get("todo_suggestion", ""))),
            "success": False,
            "result": None,
            "data": None,
            "approval": approval,
            "error": message,
            "expose_to_model": bool(spec.get("expose_to_model", True)),
            "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
            "executor_type": str(spec.get("executor_type", "")),
        }
        return cast(ActionResult, result)

    async def _resolve_action_approval(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        policy: ActionPolicy,
    ) -> tuple[bool, ActionPolicy | None, ActionResult | None]:
        if policy.get("policy_approval_granted") is True:
            return True, policy, None

        from agently.base import policy_approval

        action_id = str(spec.get("action_id", ""))
        host_approval_context = cast(dict[str, Any], action_call).get("_host_approval_context", {})
        if not isinstance(host_approval_context, dict):
            host_approval_context = {}
        public_action_call = {
            key: value
            for key, value in dict(action_call).items()
            if not str(key).startswith("_")
        }
        approval_payload: dict[str, Any] = {
            "action_call": public_action_call,
            "action_spec": {
                "action_id": action_id,
                "name": str(spec.get("name", action_id)),
                "side_effect_level": str(spec.get("side_effect_level", "")),
                "executor_type": str(spec.get("executor_type", "")),
            },
        }
        extra_payload = host_approval_context.get("payload")
        if isinstance(extra_payload, dict):
            approval_payload.update(extra_payload)
        decision = await policy_approval.async_resolve(
            {
                "source": "action",
                "capability": action_id,
                "subject": str(host_approval_context.get("subject") or spec.get("name") or action_id),
                "risk": str(host_approval_context.get("risk") or spec.get("side_effect_level", "")),
                "payload": approval_payload,
                "policy": dict(policy),
            },
            handler=str(policy.get("policy_approval_handler") or "") or None,
        )
        status = str(decision.get("status", "pending"))
        if status == "approved":
            merged_policy = cast(ActionPolicy, dict(policy))
            override = decision.get("policy_override", {})
            if isinstance(override, dict):
                cast(dict[str, Any], merged_policy).update(override)
            merged_policy["policy_approval_granted"] = True
            merged_policy["policy_approval_decision"] = dict(decision)
            return True, merged_policy, None
        message = str(decision.get("reason") or f"Action '{ action_id }' requires approval before execution.")
        result = self._approval_result(
            spec=spec,
            action_call=action_call,
            policy=policy,
            reason=status,
            message=message,
        )
        approval = result.get("approval", {})
        if isinstance(approval, dict):
            approval["decision"] = dict(decision)
            result["approval"] = cast(ActionApproval, approval)
        if status == "denied":
            result["status"] = "blocked"
            result["error"] = message
        return False, None, result

    def _normalize_executor_output(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        output: Any,
        policy: ActionPolicy,
    ) -> ActionResult:
        action_id = str(spec.get("action_id", ""))
        tool_name = str(spec.get("name", action_id))
        if isinstance(output, dict) and output.get("status") in self.VALID_STATUSES:
            result: dict[str, Any] = dict(output)
            if result.get("status") not in {"success", "partial_success"}:
                reason = result.get("reason") or result.get("stderr") or result.get("detail")
                if reason and not result.get("error"):
                    result["error"] = str(reason)
                if "result" not in result and "data" not in result:
                    failure_data = {
                        key: result[key]
                        for key in (
                            "reason",
                            "detail",
                            "need_approval",
                            "diagnostics",
                            "workdir",
                            "cmd",
                            "timeout_seconds",
                        )
                        if key in result
                    }
                    result["result"] = failure_data
                    result["data"] = failure_data
        elif isinstance(output, dict) and output.get("need_approval") is True:
            message = str(output.get("reason", "Action execution requires approval."))
            result = dict(
                self._approval_result(
                    spec=spec,
                    action_call=action_call,
                    policy=policy,
                    reason=message,
                    message=message,
                )
            )
        else:
            status = "success"
            error = ""
            if isinstance(output, str) and output.strip().startswith("Error:"):
                status = "error"
                error = output
            elif isinstance(output, dict) and output.get("ok") is False:
                status = "error"
                error = str(output.get("reason", output.get("stderr", "Action execution failed.")))
            elif isinstance(output, dict) and isinstance(output.get("error"), str) and output.get("error"):
                status = "error"
                error = str(output["error"])

            result = {
                "ok": status == "success",
                "status": cast(Any, status),
                "data": output,
                "result": output,
                "error": error,
            }

        purpose = str(action_call.get("purpose", f"Use { action_id }"))
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        result.setdefault("ok", result.get("status") in {"success", "partial_success"})
        result.setdefault("action_call_id", str(action_call.get("action_call_id", "")))
        result.setdefault("status", "success" if result.get("ok") else "error")
        result.setdefault("purpose", purpose)
        result.setdefault("action_id", action_id)
        result.setdefault("tool_name", tool_name)
        result.setdefault("kwargs", dict(action_input))
        result.setdefault("todo_suggestion", str(action_call.get("todo_suggestion", "")))
        result.setdefault("next", str(action_call.get("next", action_call.get("todo_suggestion", ""))))
        result.setdefault("result", result.get("data"))
        result.setdefault("data", result.get("result"))
        result.setdefault("success", result.get("status") in {"success", "partial_success"})
        result.setdefault("artifacts", [])
        result.setdefault("diagnostics", [])
        result.setdefault("meta", {})
        result.setdefault("approval", {})
        result.setdefault("error", "")
        result.setdefault("expose_to_model", bool(spec.get("expose_to_model", True)))
        result.setdefault("side_effect_level", cast(Any, spec.get("side_effect_level", "read")))
        result.setdefault("executor_type", str(spec.get("executor_type", "")))
        call_diagnostics = action_call.get("diagnostics")
        if isinstance(call_diagnostics, list) and call_diagnostics:
            existing_diagnostics = result.get("diagnostics")
            existing_diagnostics = existing_diagnostics if isinstance(existing_diagnostics, list) else []
            result["diagnostics"] = [*call_diagnostics, *existing_diagnostics]
        return cast(ActionResult, result)

    @staticmethod
    def _to_execution_resource_policy(policy: ActionPolicy) -> ExecutionResourcePolicy:
        keys = {
            "auto_allow",
            "approval_mode",
            "policy_approval_handler",
            "task_workspace_roots",
            "path_allowlist",
            "path_denylist",
            "allowed_cmd_prefixes",
            "network_mode",
            "timeout_seconds",
            "max_output_bytes",
            "read_only",
            "allow_create",
            "allow_update",
            "allow_delete",
        }
        return cast(ExecutionResourcePolicy, {key: policy[key] for key in keys if key in policy})

    def _resolve_execution_resource_owner_id(
        self,
        settings: Settings,
        requirement: ExecutionResourceRequirement,
    ):
        if requirement.get("owner_id"):
            return str(requirement.get("owner_id", ""))
        configured = settings.get("execution_resource.owner_id", None)
        if isinstance(configured, str) and configured:
            return configured
        session_id = settings.get("runtime.session_id", None)
        if isinstance(session_id, str) and session_id:
            return session_id
        return self.registry.name or "Action"

    def _prepare_execution_resource_requirements(
        self,
        *,
        spec: ActionSpec,
        settings: Settings,
        policy: ActionPolicy,
    ):
        requirements = spec.get("execution_resources", [])
        if not isinstance(requirements, list):
            return []
        prepared: list[ExecutionResourceRequirement] = []
        action_policy = self._to_execution_resource_policy(policy)
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            prepared_requirement = cast(ExecutionResourceRequirement, dict(requirement))
            requirement_policy = dict(prepared_requirement.get("policy", {}))
            requirement_policy.update(action_policy)
            prepared_requirement["policy"] = cast(ExecutionResourcePolicy, requirement_policy)
            prepared_requirement.setdefault("scope", "action_call")
            prepared_requirement.setdefault("owner_id", self._resolve_execution_resource_owner_id(settings, prepared_requirement))
            prepared_requirement.setdefault("resource_key", str(spec.get("action_id", prepared_requirement.get("kind", ""))))
            prepared.append(prepared_requirement)
        return prepared

    @staticmethod
    def _resolve_task_workspace(settings: Settings):
        from agently.core.TaskWorkspace import TaskWorkspace
        from agently.core.TaskWorkspace._defaults import default_task_workspace_root
        from agently.core.runtime import get_current_agent_execution_context

        execution_context = get_current_agent_execution_context()
        bound_workspace = getattr(execution_context, "task_workspace", None)
        if isinstance(bound_workspace, TaskWorkspace):
            return bound_workspace
        configured_root = settings.get("task_workspace.root", None)
        configured_mode = str(settings.get("task_workspace.mode", "read_only"))
        if configured_root is not None:
            return TaskWorkspace(
                str(configured_root),
                mode=configured_mode,
                create=True,
                execution_id=f"action_{uuid.uuid4().hex}",
            )
        execution_id = f"action_{uuid.uuid4().hex}"
        return TaskWorkspace(
            default_task_workspace_root()
            / ".agently"
            / "task_workspaces"
            / "direct"
            / execution_id,
            mode="read_only",
            create=True,
            execution_id=execution_id,
        )

    @staticmethod
    def _workspace_access_requirement(
        value: dict[str, Any],
    ) -> TaskWorkspaceAccessRequirement:
        raw_inputs = value.get("input_paths", ())
        raw_outputs = value.get("output_paths", value.get("expected_outputs", ()))
        input_paths = (
            (raw_inputs,)
            if isinstance(raw_inputs, str)
            else tuple(raw_inputs) if isinstance(raw_inputs, (list, tuple)) else ()
        )
        output_paths = (
            (raw_outputs,)
            if isinstance(raw_outputs, str)
            else tuple(raw_outputs) if isinstance(raw_outputs, (list, tuple)) else ()
        )
        return TaskWorkspaceAccessRequirement(
            mode=cast(Any, str(value.get("mode", "snapshot"))),
            include_workspace_root=bool(value.get("include_workspace_root", False)),
            input_paths=tuple(str(item) for item in input_paths),
            output_paths=tuple(str(item) for item in output_paths),
            retain_source=bool(value.get("retain_source", False)),
        )

    @staticmethod
    def _close_workspace_grants(
        task_workspace: Any,
        grants: list[TaskWorkspaceAccessGrant],
    ) -> None:
        if task_workspace is None:
            return
        for grant in grants:
            task_workspace.close_execution_access(grant.grant_id)

    async def _release_execution_resources(
        self,
        execution_resource: Any,
        handles: list[ExecutionResourceHandle],
        *,
        action_call: ActionCall,
        action_scope_only: bool,
    ) -> list[ActionDiagnostic]:
        diagnostics: list[ActionDiagnostic] = []
        for handle in handles:
            if action_scope_only and handle.get("scope") != "action_call":
                continue
            try:
                await execution_resource.async_release(handle)
            except Exception as error:
                code = str(
                    getattr(error, "code", "")
                    or "execution_resource.release_failed"
                )
                diagnostics.append(
                    self._exception_diagnostic(
                        code=code,
                        message=str(error) or "Execution resource release failed.",
                        error=error,
                        meta={
                            "handle_id": str(handle.get("handle_id", "")),
                            "provider_id": str(handle.get("provider_id", "")),
                        },
                    )
                )
        if diagnostics:
            raw = action_call.get("diagnostics")
            existing = raw if isinstance(raw, list) else []
            action_call["diagnostics"] = [*existing, *diagnostics]
        return diagnostics

    def _execution_resource_error_result(
        self,
        *,
        spec: ActionSpec,
        action_call: ActionCall,
        status: str,
        error: str,
        approval: ActionApproval | None = None,
    ) -> ActionResult:
        action_id = str(spec.get("action_id", ""))
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        result = {
            "action_call_id": str(action_call.get("action_call_id", "")),
            "ok": False,
            "status": status,
            "purpose": str(action_call.get("purpose", f"Use { action_id }")),
            "action_id": action_id,
            "tool_name": str(spec.get("name", action_id)),
            "kwargs": dict(action_input),
            "todo_suggestion": str(action_call.get("todo_suggestion", "")),
            "next": str(action_call.get("next", action_call.get("todo_suggestion", ""))),
            "success": False,
            "result": None,
            "data": None,
            "approval": approval or {},
            "error": error,
            "expose_to_model": bool(spec.get("expose_to_model", True)),
            "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
            "executor_type": str(spec.get("executor_type", "")),
            "diagnostics": self._call_diagnostics(action_call),
        }
        return cast(ActionResult, result)

    async def async_execute(
        self,
        action_id: str,
        action_input: dict[str, Any],
        *,
        settings: Settings | None = None,
        purpose: str | None = None,
        policy_override: ActionPolicy | None = None,
        trusted_policy_override: ActionPolicy | None = None,
        source_protocol: str = "direct",
        todo_suggestion: str = "",
        next_value: str = "",
    ) -> ActionResult:
        action_call_id = f"act_call_{uuid.uuid4().hex}"
        execution_settings = settings if settings is not None else self.settings
        spec = self.registry.get_spec(action_id)
        if spec is None:
            return {
                "action_call_id": action_call_id,
                "ok": False,
                "status": "error",
                "purpose": purpose or f"Use { action_id }",
                "action_id": action_id,
                "tool_name": action_id,
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Can not find action named '{ action_id }'",
                "expose_to_model": False,
                "side_effect_level": "read",
                "executor_type": "",
            }

        executor = self.registry.get_executor(action_id)
        tool_name = str(spec.get("name", action_id))
        if executor is None:
            return {
                "action_call_id": action_call_id,
                "ok": False,
                "status": "error",
                "purpose": purpose or f"Use { action_id }",
                "action_id": action_id,
                "tool_name": tool_name,
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"No executor registered for action '{ action_id }'",
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }

        original_action_input = dict(action_input)
        sanitized_override, stripped_policy_keys = self._sanitize_policy_override(
            policy_override,
            source_protocol=source_protocol,
        )
        sanitized_action_input, stripped_input_keys = self._sanitize_action_input(
            spec,
            original_action_input,
            source_protocol=source_protocol,
        )
        action_input = sanitized_action_input
        call_diagnostics: list[ActionDiagnostic] = []
        if stripped_policy_keys:
            call_diagnostics.append(
                cast(ActionDiagnostic, {
                    "source": "ActionDispatcher",
                    "severity": "warning",
                    "code": "action.policy_override.host_only_keys_stripped",
                    "message": (
                        "Ignored host-only policy override keys from a model-planned action command: "
                        f"{ ', '.join(sorted(stripped_policy_keys)) }."
                    ),
                    "meta": {"source_protocol": source_protocol, "stripped_keys": sorted(stripped_policy_keys)},
                })
            )
        if stripped_input_keys:
            call_diagnostics.append(
                self._input_stripped_diagnostic(
                    source_protocol=source_protocol,
                    stripped_keys=stripped_input_keys,
                    original_input=original_action_input,
                    sanitized_input=sanitized_action_input,
                )
            )
        action_call: ActionCall = {
            "action_call_id": action_call_id,
            "purpose": purpose or f"Use { action_id }",
            "action_id": action_id,
            "action_input": dict(action_input),
            "policy_override": sanitized_override,
            "source_protocol": source_protocol,
            "todo_suggestion": todo_suggestion,
            "next": next_value or todo_suggestion,
            "tool_name": str(spec.get("name", action_id)),
            "tool_kwargs": dict(action_input),
            "diagnostics": call_diagnostics,
        }
        missing_input_keys = (
            sorted(self._required_action_input_keys(spec) - set(action_input))
            if source_protocol in self.MODEL_PLANNING_PROTOCOLS
            else []
        )
        if missing_input_keys:
            message = (
                f"Action '{action_id}' is missing required input keys: "
                + ", ".join(missing_input_keys)
                + "."
            )
            call_diagnostics.append(
                self._exception_diagnostic(
                    code="action.input.required_keys_missing",
                    message=message,
                    meta={
                        "source_protocol": source_protocol,
                        "missing_input_keys": missing_input_keys,
                    },
                )
            )
            return self._execution_resource_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=message,
            )
        policy = self._merge_policy(execution_settings, spec, sanitized_override)
        if isinstance(trusted_policy_override, dict) and trusted_policy_override:
            # Host-trusted grants (e.g. a policy approval resolved through the
            # ExecutionExchange gate) intentionally bypass the model-sourced
            # sanitization: they are passed by host/framework code and never
            # travel inside the planned action command.
            cast(dict[str, Any], policy).update(trusted_policy_override)
        policy_approval_handler = execution_settings.get("policy_approval.handler", None)
        if policy_approval_handler is not None and not policy.get("policy_approval_handler"):
            policy["policy_approval_handler"] = str(policy_approval_handler)

        dynamic_approval_required = False
        spec_meta = spec.get("meta")
        approval_predicate = (
            spec_meta.get("_host_approval_required_when")
            if isinstance(spec_meta, dict)
            else None
        )
        if callable(approval_predicate):
            predicate_result = approval_predicate(action_call)
            if inspect.isawaitable(predicate_result):
                predicate_result = await predicate_result
            if isinstance(predicate_result, dict):
                dynamic_approval_required = bool(predicate_result.get("required"))
                context = predicate_result.get("context")
                if isinstance(context, dict):
                    cast(dict[str, Any], action_call)["_host_approval_context"] = dict(context)
            else:
                dynamic_approval_required = bool(predicate_result)

        if (
            spec.get("approval_required") is True
            or policy.get("approval_mode") == "always"
            or dynamic_approval_required
        ):
            approved, approved_policy, approval_result = await self._resolve_action_approval(
                spec=spec,
                action_call=action_call,
                policy=policy,
            )
            if not approved:
                return cast(ActionResult, approval_result)
            if approved_policy is not None:
                policy = approved_policy
        resource_managed_isolation = any(
            isinstance(requirement, dict)
            and isinstance(
                dict(requirement.get("required_capabilities", {})).get(
                    "isolation"
                ),
                dict,
            )
            for requirement in spec.get("execution_resources", [])
            if isinstance(spec.get("execution_resources", []), list)
        )
        if (
            spec.get("sandbox_required") is True
            and not getattr(executor, "sandboxed", False)
            and not (
                getattr(executor, "resource_isolation_managed", False)
                and resource_managed_isolation
            )
        ):
            return {
                "action_call_id": action_call_id,
                "ok": False,
                "status": "blocked",
                "purpose": str(action_call.get("purpose", f"Use { action_id }")),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Action '{ action_id }' requires a sandboxed executor.",
                "diagnostics": self._call_diagnostics(
                    action_call,
                    self._exception_diagnostic(
                        code="action.execution.sandbox_required",
                        message=f"Action '{ action_id }' requires a sandboxed executor.",
                    ),
                ),
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }

        from agently.base import execution_resource

        ensured_handles: list[ExecutionResourceHandle] = []
        environment_resources: dict[str, Any] = {}
        environment_handles: dict[str, ExecutionResourceHandle] = {}
        workspace_grants: list[TaskWorkspaceAccessGrant] = []
        workspace_grants_by_key: dict[str, TaskWorkspaceAccessGrant] = {}
        task_workspace: Any = None
        try:
            for requirement in self._prepare_execution_resource_requirements(
                spec=spec,
                settings=execution_settings,
                policy=policy,
            ):
                raw_workspace_access = requirement.get("workspace_access")
                if isinstance(raw_workspace_access, dict):
                    if task_workspace is None:
                        task_workspace = self._resolve_task_workspace(execution_settings)
                    resource_key = str(
                        requirement.get("resource_key", action_id)
                    )
                    grant = task_workspace.issue_execution_access(
                        action_call_id=action_call_id,
                        requirement=self._workspace_access_requirement(raw_workspace_access),
                    )
                    workspace_grants.append(grant)
                    workspace_grants_by_key[resource_key] = grant
                    requirement["action_call_id"] = action_call_id
                    requirement["task_workspace_access_grant"] = grant
                handle = await execution_resource.async_ensure(
                    requirement,
                    owner_id=str(requirement.get("owner_id", "")),
                )
                ensured_handles.append(handle)
                resource_key = str(handle.get("resource_key", requirement.get("resource_key", "")))
                if resource_key:
                    environment_handles[resource_key] = handle
                    environment_resources[resource_key] = handle.get("resource")
            if environment_handles:
                action_call["execution_resource_handles"] = environment_handles
                action_call["execution_resource_resources"] = environment_resources
            if task_workspace is not None:
                action_call["task_workspace"] = task_workspace
                action_call["task_workspace_access_grants"] = workspace_grants_by_key
        except ExecutionResourceApprovalRequired as error:
            await self._release_execution_resources(
                execution_resource,
                ensured_handles,
                action_call=action_call,
                action_scope_only=False,
            )
            self._close_workspace_grants(task_workspace, workspace_grants)
            approval: ActionApproval = {
                "required": True,
                "reason": error.code,
                "approval_mode": str(error.payload.get("policy", {}).get("approval_mode", "auto")),
                "missing_permissions": [error.code],
                "suggested_policy": cast(ActionPolicy, error.payload.get("policy", {})),
                "message": str(error),
            }
            return self._execution_resource_error_result(
                spec=spec,
                action_call=action_call,
                status="approval_required",
                error=str(error),
                approval=approval,
            )
        except ExecutionResourceApprovalDenied as error:
            await self._release_execution_resources(
                execution_resource,
                ensured_handles,
                action_call=action_call,
                action_scope_only=False,
            )
            self._close_workspace_grants(task_workspace, workspace_grants)
            return self._execution_resource_error_result(
                spec=spec,
                action_call=action_call,
                status="blocked",
                error=str(error),
            )
        except ExecutionResourceError as error:
            await self._release_execution_resources(
                execution_resource,
                ensured_handles,
                action_call=action_call,
                action_scope_only=False,
            )
            self._close_workspace_grants(task_workspace, workspace_grants)
            action_call.setdefault("diagnostics", [])
            diagnostics = action_call.get("diagnostics")
            if isinstance(diagnostics, list):
                diagnostics.append(
                    self._exception_diagnostic(
                        code=error.code,
                        message=str(error),
                        meta=error.payload,
                    )
                )
            return self._execution_resource_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=str(error),
            )
        except Exception as error:
            await self._release_execution_resources(
                execution_resource,
                ensured_handles,
                action_call=action_call,
                action_scope_only=False,
            )
            self._close_workspace_grants(task_workspace, workspace_grants)
            return self._execution_resource_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=str(error),
            )

        timeout = policy.get("timeout_seconds", None)
        timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else 0.0
        cleanup_diagnostics: list[ActionDiagnostic] = []
        execution_error_result: ActionResult | None = None
        output: Any = None
        try:
            with bind_runtime_context(action_policy=cast(dict[str, Any], dict(policy))):
                if isinstance(timeout, (int, float)) and timeout > 0:
                    output = await asyncio.wait_for(
                        executor.execute(
                            spec=spec,
                            action_call=action_call,
                            policy=policy,
                            settings=execution_settings,
                        ),
                        timeout=float(timeout),
                    )
                else:
                    output = await executor.execute(
                        spec=spec,
                        action_call=action_call,
                        policy=policy,
                        settings=execution_settings,
                    )
        except asyncio.TimeoutError:
            timeout_diagnostic = self._exception_diagnostic(
                code="action.execution.timeout",
                message=f"Action '{ action_id }' timed out after { timeout_seconds } seconds.",
                meta={"timeout_seconds": timeout_seconds, "source_protocol": source_protocol},
            )
            execution_error_result = cast(ActionResult, {
                "action_call_id": action_call_id,
                "ok": False,
                "status": "error",
                "purpose": str(action_call.get("purpose", f"Use { action_id }")),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Action '{ action_id }' timed out after { timeout_seconds } seconds.",
                "diagnostics": self._call_diagnostics(action_call, timeout_diagnostic),
                "meta": {"timeout_seconds": timeout_seconds},
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            })
        except Exception as error:
            diagnostic_code = "action.input.type_error" if isinstance(error, TypeError) else "action.execution.exception"
            exception_diagnostic = self._exception_diagnostic(
                code=diagnostic_code,
                message=str(error) or f"Action '{ action_id }' raised { type(error).__name__ }.",
                error=error,
                meta={"source_protocol": source_protocol},
            )
            execution_error_result = cast(ActionResult, {
                "action_call_id": action_call_id,
                "ok": False,
                "status": "error",
                "purpose": str(action_call.get("purpose", f"Use { action_id }")),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": str(error),
                "diagnostics": self._call_diagnostics(action_call, exception_diagnostic),
                "meta": {
                    "exception_type": type(error).__name__,
                    "exception_message": self._compact_diagnostic_value(str(error)),
                },
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            })
        finally:
            cleanup_diagnostics = await self._release_execution_resources(
                execution_resource,
                ensured_handles,
                action_call=action_call,
                action_scope_only=True,
            )
            self._close_workspace_grants(task_workspace, workspace_grants)

        if execution_error_result is not None:
            if cleanup_diagnostics:
                raw_diagnostics = execution_error_result.get("diagnostics")
                result_diagnostics = (
                    list(raw_diagnostics)
                    if isinstance(raw_diagnostics, list)
                    else []
                )
                result_diagnostics.extend(cleanup_diagnostics)
                execution_error_result["diagnostics"] = result_diagnostics
            return execution_error_result

        if cleanup_diagnostics:
            return self._execution_resource_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=(
                    "Action execution completed, but its execution resource "
                    "could not be released safely."
                ),
            )

        result = self._normalize_executor_output(
            spec=spec,
            action_call=action_call,
            output=output,
            policy=policy,
        )
        max_output_bytes = policy.get("max_output_bytes")
        if isinstance(max_output_bytes, int) and max_output_bytes > 0:
            serialized = json.dumps(result.get("data"), ensure_ascii=False, default=str)
            if len(serialized.encode("utf-8")) > max_output_bytes:
                result_meta = result.get("meta")
                if not isinstance(result_meta, dict):
                    result_meta = {}
                result_meta["max_output_bytes_exceeded"] = True
                result_meta["max_output_bytes"] = max_output_bytes
                result_meta["original_output_bytes"] = len(serialized.encode("utf-8"))
                result["meta"] = result_meta
                diagnostics = result.get("diagnostics")
                diagnostics = diagnostics if isinstance(diagnostics, list) else []
                diagnostics.append(
                    self._exception_diagnostic(
                        code="action.output.max_output_bytes_exceeded",
                        message=(
                            "Action output exceeded policy max_output_bytes; full output is preserved for "
                            "Action artifact finalization while model-visible paths should use bounded previews."
                        ),
                        meta={
                            "max_output_bytes": max_output_bytes,
                            "original_output_bytes": len(serialized.encode("utf-8")),
                        },
                    )
                )
                result["diagnostics"] = diagnostics
        return result

    def execute(self, action_id: str, action_input: dict[str, Any], **kwargs):
        return default_stage_call_bridge.as_sync(self.async_execute)(action_id, action_input, **kwargs)

    async def async_dry_run(
        self,
        action_id: str,
        action_input: dict[str, Any],
        *,
        settings: Settings | None = None,
        policy_override: ActionPolicy | None = None,
    ) -> ActionResult:
        return await self.async_execute(
            action_id,
            action_input,
            settings=settings,
            purpose=f"Dry run { action_id }",
            policy_override=policy_override,
            source_protocol="dry_run",
        )

    def dry_run(self, action_id: str, action_input: dict[str, Any], **kwargs):
        return default_stage_call_bridge.as_sync(self.async_dry_run)(action_id, action_input, **kwargs)
