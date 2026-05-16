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

import asyncio
import inspect
import json
import uuid
import warnings
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Callable,
    Coroutine,
    Literal,
    ParamSpec,
    TypeVar,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from agently.types.data import (
    ActionApproval,
    ActionCall,
    ActionDecision,
    ActionExecutionRequest,
    ActionPolicy,
    ActionPlanningRequest,
    ActionResult,
    ActionRunContext,
    ActionSpec,
    ExecutionEnvironmentHandle,
    ExecutionEnvironmentPolicy,
    ExecutionEnvironmentRequirement,
    ActionArtifact,
)
from agently.core.ExecutionEnvironment import (
    ExecutionEnvironmentApprovalDenied,
    ExecutionEnvironmentApprovalRequired,
    ExecutionEnvironmentError,
)
from agently.types.plugins import (
    ActionExecutionHandler,
    ActionExecutor,
    ActionPlanningHandler,
    StandardActionExecutionHandler,
    StandardActionPlanningHandler,
)
from agently.utils import FunctionShifter, Settings, SettingsNamespace
from agently.utils import DataFormatter, LazyImport

if TYPE_CHECKING:
    from agently.core import PluginManager, Prompt
    from agently.types.data import MCPConfigs, KwargsType, ReturnType
    from agently.types.plugins import ActionFlow, ActionRuntime

P = ParamSpec("P")
R = TypeVar("R")


class ActionRegistry:
    """
    Action is the single first-class executable abstraction in this runtime.
    Avoid parallel nouns unless the lifecycle is materially different.
    """

    def __init__(self, *, name: str | None = None):
        self.name = name
        self._specs: dict[str, ActionSpec] = {}
        self._executors: dict[str, ActionExecutor] = {}
        self._funcs: dict[str, Callable[..., Any]] = {}
        self._tag_mappings: dict[str, set[str]] = {}
        self._action_tags: dict[str, set[str]] = {}

    def register(
        self,
        spec: ActionSpec,
        executor: ActionExecutor,
        *,
        func: Callable[..., Any] | None = None,
    ):
        action_id = str(spec.get("action_id", ""))
        self._specs[action_id] = spec
        self._executors[action_id] = executor
        if func is not None:
            self._funcs[action_id] = func
        tags = spec.get("tags", [])
        if not isinstance(tags, list):
            tags = list(tags) if isinstance(tags, (tuple, set)) else []
        self._action_tags[action_id] = set([str(tag) for tag in tags])
        for tag in self._action_tags[action_id]:
            self._tag_mappings.setdefault(tag, set()).add(action_id)
        return self

    def tag(self, action_ids: str | list[str], tags: str | list[str]):
        if isinstance(action_ids, str):
            action_ids = [action_ids]
        if isinstance(tags, str):
            tags = [tags]
        for action_id in action_ids:
            if action_id not in self._specs:
                raise ValueError(f"Cannot find action named '{ action_id }'")
            self._action_tags.setdefault(action_id, set())
            for tag in tags:
                tag_text = str(tag)
                self._action_tags[action_id].add(tag_text)
                self._tag_mappings.setdefault(tag_text, set()).add(action_id)
            self._specs[action_id]["tags"] = sorted(self._action_tags[action_id])
        return self

    def has(self, action_id: str):
        return action_id in self._specs

    def get_spec(self, action_id: str):
        return self._specs.get(action_id)

    def get_executor(self, action_id: str):
        return self._executors.get(action_id)

    def get_func(self, action_id: str):
        return self._funcs.get(action_id)

    def get_tags(self, action_id: str):
        return self._action_tags.get(action_id, set())

    def list_action_ids(self, tags: str | list[str] | None = None):
        if tags is None:
            return list(self._specs.keys())
        if isinstance(tags, str):
            tags = [tags]
        collected: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            for action_id in self._tag_mappings.get(tag, set()):
                if action_id not in seen:
                    seen.add(action_id)
                    collected.append(action_id)
        return collected


class ActionDispatcher:
    VALID_STATUSES = {"success", "error", "approval_required", "blocked", "skipped"}

    def __init__(self, registry: ActionRegistry, settings: Settings):
        self.registry = registry
        self.settings = settings

    @staticmethod
    def _to_dict(value: Any):
        return value if isinstance(value, dict) else {}

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
        return cast(ActionPolicy, merged)

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
        result.setdefault("ok", result.get("status") == "success")
        result.setdefault("status", "success" if result.get("ok") else "error")
        result.setdefault("purpose", purpose)
        result.setdefault("action_id", action_id)
        result.setdefault("tool_name", tool_name)
        result.setdefault("kwargs", dict(action_input))
        result.setdefault("todo_suggestion", str(action_call.get("todo_suggestion", "")))
        result.setdefault("next", str(action_call.get("next", action_call.get("todo_suggestion", ""))))
        result.setdefault("result", result.get("data"))
        result.setdefault("data", result.get("result"))
        result.setdefault("success", result.get("status") == "success")
        result.setdefault("artifacts", [])
        result.setdefault("diagnostics", [])
        result.setdefault("meta", {})
        result.setdefault("approval", {})
        result.setdefault("error", "")
        result.setdefault("expose_to_model", bool(spec.get("expose_to_model", True)))
        result.setdefault("side_effect_level", cast(Any, spec.get("side_effect_level", "read")))
        result.setdefault("executor_type", str(spec.get("executor_type", "")))
        return cast(ActionResult, result)

    @staticmethod
    def _to_execution_environment_policy(policy: ActionPolicy) -> ExecutionEnvironmentPolicy:
        keys = {
            "approval_mode",
            "workspace_roots",
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
        return cast(ExecutionEnvironmentPolicy, {key: policy[key] for key in keys if key in policy})

    def _resolve_execution_environment_owner_id(
        self,
        settings: Settings,
        requirement: ExecutionEnvironmentRequirement,
    ):
        if requirement.get("owner_id"):
            return str(requirement.get("owner_id", ""))
        configured = settings.get("execution_environment.owner_id", None)
        if isinstance(configured, str) and configured:
            return configured
        session_id = settings.get("runtime.session_id", None)
        if isinstance(session_id, str) and session_id:
            return session_id
        return self.registry.name or "Action"

    def _prepare_execution_environment_requirements(
        self,
        *,
        spec: ActionSpec,
        settings: Settings,
        policy: ActionPolicy,
    ):
        requirements = spec.get("execution_environments", [])
        if not isinstance(requirements, list):
            return []
        prepared: list[ExecutionEnvironmentRequirement] = []
        action_policy = self._to_execution_environment_policy(policy)
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            prepared_requirement = cast(ExecutionEnvironmentRequirement, dict(requirement))
            requirement_policy = dict(prepared_requirement.get("policy", {}))
            requirement_policy.update(action_policy)
            prepared_requirement["policy"] = cast(ExecutionEnvironmentPolicy, requirement_policy)
            prepared_requirement.setdefault("scope", "action_call")
            prepared_requirement.setdefault("owner_id", self._resolve_execution_environment_owner_id(settings, prepared_requirement))
            prepared_requirement.setdefault("resource_key", str(spec.get("action_id", prepared_requirement.get("kind", ""))))
            prepared.append(prepared_requirement)
        return prepared

    def _execution_environment_error_result(
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
        return cast(ActionResult, {
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
        })

    async def async_execute(
        self,
        action_id: str,
        action_input: dict[str, Any],
        *,
        settings: Settings | None = None,
        purpose: str | None = None,
        policy_override: ActionPolicy | None = None,
        source_protocol: str = "direct",
        todo_suggestion: str = "",
        next_value: str = "",
    ) -> ActionResult:
        execution_settings = settings if settings is not None else self.settings
        spec = self.registry.get_spec(action_id)
        if spec is None:
            return {
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

        action_call: ActionCall = {
            "purpose": purpose or f"Use { action_id }",
            "action_id": action_id,
            "action_input": dict(action_input),
            "policy_override": policy_override or {},
            "source_protocol": source_protocol,
            "todo_suggestion": todo_suggestion,
            "next": next_value or todo_suggestion,
            "tool_name": str(spec.get("name", action_id)),
            "tool_kwargs": dict(action_input),
        }
        policy = self._merge_policy(execution_settings, spec, policy_override)

        if spec.get("approval_required") is True or policy.get("approval_mode") == "always":
            message = f"Action '{ action_id }' requires approval before execution."
            return self._approval_result(
                spec=spec,
                action_call=action_call,
                policy=policy,
                reason="approval_required",
                message=message,
            )
        if spec.get("sandbox_required") is True and not getattr(executor, "sandboxed", False):
            return {
                "ok": False,
                "status": "blocked",
                "purpose": str(action_call["purpose"]),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Action '{ action_id }' requires a sandboxed executor.",
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }

        from agently.base import execution_environment

        ensured_handles: list[ExecutionEnvironmentHandle] = []
        environment_resources: dict[str, Any] = {}
        environment_handles: dict[str, ExecutionEnvironmentHandle] = {}
        try:
            for requirement in self._prepare_execution_environment_requirements(
                spec=spec,
                settings=execution_settings,
                policy=policy,
            ):
                handle = await execution_environment.async_ensure(
                    requirement,
                    owner_id=str(requirement.get("owner_id", "")),
                )
                ensured_handles.append(handle)
                resource_key = str(handle.get("resource_key", requirement.get("resource_key", "")))
                if resource_key:
                    environment_handles[resource_key] = handle
                    environment_resources[resource_key] = handle.get("resource")
            if environment_handles:
                action_call["execution_environment_handles"] = environment_handles
                action_call["execution_environment_resources"] = environment_resources
        except ExecutionEnvironmentApprovalRequired as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            approval: ActionApproval = {
                "required": True,
                "reason": error.code,
                "approval_mode": str(error.payload.get("policy", {}).get("approval_mode", "auto")),
                "missing_permissions": [error.code],
                "suggested_policy": cast(ActionPolicy, error.payload.get("policy", {})),
                "message": str(error),
            }
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="approval_required",
                error=str(error),
                approval=approval,
            )
        except ExecutionEnvironmentApprovalDenied as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="blocked",
                error=str(error),
            )
        except ExecutionEnvironmentError as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=str(error),
            )
        except Exception as error:
            for handle in ensured_handles:
                await execution_environment.async_release(handle)
            return self._execution_environment_error_result(
                spec=spec,
                action_call=action_call,
                status="error",
                error=str(error),
            )

        timeout = policy.get("timeout_seconds", None)
        timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else 0.0
        try:
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
            for handle in ensured_handles:
                if handle.get("scope") == "action_call":
                    await execution_environment.async_release(handle)
            return {
                "ok": False,
                "status": "error",
                "purpose": str(action_call["purpose"]),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": f"Action '{ action_id }' timed out after { timeout_seconds } seconds.",
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }
        except Exception as error:
            for handle in ensured_handles:
                if handle.get("scope") == "action_call":
                    await execution_environment.async_release(handle)
            return {
                "ok": False,
                "status": "error",
                "purpose": str(action_call["purpose"]),
                "action_id": action_id,
                "tool_name": str(spec.get("name", action_id)),
                "kwargs": dict(action_input),
                "todo_suggestion": todo_suggestion,
                "next": next_value or todo_suggestion,
                "success": False,
                "result": None,
                "data": None,
                "error": str(error),
                "expose_to_model": bool(spec.get("expose_to_model", True)),
                "side_effect_level": cast(Any, spec.get("side_effect_level", "read")),
                "executor_type": str(spec.get("executor_type", "")),
            }
        finally:
            for handle in ensured_handles:
                if handle.get("scope") == "action_call":
                    await execution_environment.async_release(handle)

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
                truncated = serialized.encode("utf-8")[:max_output_bytes].decode("utf-8", errors="ignore")
                result["data"] = truncated
                result["result"] = truncated
                result_meta = result.get("meta")
                if not isinstance(result_meta, dict):
                    result_meta = {}
                result_meta["truncated"] = True
                result["meta"] = result_meta
        return result

    def execute(self, action_id: str, action_input: dict[str, Any], **kwargs):
        return FunctionShifter.syncify(self.async_execute)(action_id, action_input, **kwargs)

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
        return FunctionShifter.syncify(self.async_dry_run)(action_id, action_input, **kwargs)


class _DeprecatedActionManagerProxy:
    def __init__(self, action: "Action", name: str):
        self._action = action
        self._name = name

    def __getattr__(self, item: str):
        warnings.warn(
            f"Action.{ self._name } is deprecated. Use Action directly; `tool` remains only as a public surface alias.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(self._action, item)


class Action:
    ACTION_RESULT_QUOTE_NOTICE = (
        "NOTICE: MUST QUOTE KEY INFO OR MARK SOURCE (PREFER URL INCLUDED) FROM {action_results} "
        "IN REPLY IF YOU USE {action_results} TO IMPROVE REPLY!"
    )
    TOOL_RESULT_QUOTE_NOTICE = ACTION_RESULT_QUOTE_NOTICE

    def __init__(
        self,
        plugin_manager: "PluginManager",
        parent_settings: "Settings",
    ):
        self.plugin_manager = plugin_manager
        self.settings = Settings(
            name="Action-Settings",
            parent=parent_settings,
        )
        self.action_settings = SettingsNamespace(self.settings, "action")
        self.action_settings.setdefault("loop.max_rounds", 5)
        self.action_settings.setdefault("loop.concurrency", None)
        self.action_settings.setdefault("loop.timeout", None)
        self.action_settings.setdefault("protocol", "structured_plan")
        self.action_settings.setdefault("policy.global", {})
        self.action_settings.setdefault("policy.agent", {})

        self.tool_settings = SettingsNamespace(self.settings, "tool")
        self.tool_settings.setdefault("loop.max_rounds", 5)
        self.tool_settings.setdefault("loop.concurrency", None)
        self.tool_settings.setdefault("loop.timeout", None)

        self.action_registry = ActionRegistry(name="ActionRegistry")
        self.action_dispatcher = ActionDispatcher(self.action_registry, self.settings)
        self.action_funcs: dict[str, Callable[..., Any]] = {}
        self.tool_funcs = self.action_funcs
        self._action_artifacts: dict[str, dict[str, Any]] = {}
        self._deprecated_action_manager = _DeprecatedActionManagerProxy(self, "action_manager")
        self._deprecated_tool_manager = _DeprecatedActionManagerProxy(self, "tool_manager")

        self.action_runtime = self._create_action_runtime()
        self.runtime = self.action_runtime
        self.action_flow = self._create_action_flow()
        self.flow = self.action_flow
        self._register_action_artifact_recall_action()

        self.plan_and_execute = FunctionShifter.syncify(self.async_plan_and_execute)
        self.generate_action_call = FunctionShifter.syncify(self.async_generate_action_call)
        self.generate_tool_command = FunctionShifter.syncify(self.async_generate_tool_command)
        self.use_action_mcp = FunctionShifter.syncify(self.async_use_action_mcp)
        self.use_mcp = FunctionShifter.syncify(self.async_use_mcp)
        self.read_action_artifact = FunctionShifter.syncify(self.async_read_action_artifact)

    def _register_action_artifact_recall_action(self):
        self.register_action(
            action_id="read_action_artifact",
            desc=(
                "Read full raw input, output, code, command, SQL, page, or log content "
                "from a previous Action call by artifact reference when the execution "
                "digest is not enough."
            ),
            kwargs={
                "artifact_id": (str, "Artifact id from a previous Action execution digest."),
                "action_call_id": (str, "Optional Action call id that should own this artifact."),
            },
            func=self.async_read_action_artifact,
            side_effect_level="read",
            expose_to_model=False,
            meta={
                "component": "action_loop_execution_recall",
                "auto_exposed_after_artifact": True,
            },
        )
        return self

    async def async_read_action_artifact(
        self,
        artifact_id: str,
        action_call_id: str | None = None,
    ) -> dict[str, Any]:
        from agently.base import async_emit_runtime
        from agently.types.data import ObservationEvent

        artifact = self._action_artifacts.get(str(artifact_id))
        if artifact is None:
            result = {
                "ok": False,
                "status": "not_found",
                "artifact_id": str(artifact_id),
                "error": "Action artifact was not found or is no longer retained.",
            }
        elif action_call_id and str(artifact.get("action_call_id", "")) != str(action_call_id):
            result = {
                "ok": False,
                "status": "forbidden",
                "artifact_id": str(artifact_id),
                "action_call_id": str(action_call_id),
                "error": "Action artifact does not belong to the requested action_call_id.",
            }
        else:
            result = {
                "ok": True,
                "status": "success",
                "artifact_id": str(artifact_id),
                "action_call_id": artifact.get("action_call_id", ""),
                "artifact_type": artifact.get("artifact_type", ""),
                "label": artifact.get("label", ""),
                "media_type": artifact.get("media_type", ""),
                "value": artifact.get("value"),
                "meta": artifact.get("meta", {}),
            }

        await async_emit_runtime(
            ObservationEvent(
                event_type="action.artifact_read",
                source="ActionRuntime",
                level="INFO" if result.get("ok") else "WARNING",
                message=f"Action artifact '{ artifact_id }' read.",
                payload={
                    "artifact_id": str(artifact_id),
                    "action_call_id": action_call_id,
                    "status": result.get("status"),
                    "ok": result.get("ok"),
                },
            )
        )
        return result

    @property
    def action_manager(self):
        warnings.warn(
            "Action.action_manager is deprecated. Use Action directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._deprecated_action_manager

    @property
    def tool_manager(self):
        warnings.warn(
            "Action.tool_manager is deprecated. Use Action directly; `tool` remains a public surface alias.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._deprecated_tool_manager

    @staticmethod
    def _normalize_tags(tags: str | list[str] | None):
        if tags is None:
            return []
        if isinstance(tags, str):
            return [tags]
        return [str(tag) for tag in tags]

    def create_action_executor(self, plugin_name: str, **kwargs) -> Any:
        plugin_class = cast(type[Any], self.plugin_manager.get_plugin("ActionExecutor", plugin_name))
        return plugin_class(**kwargs)

    def _create_executor(self, plugin_name: str, **kwargs) -> Any:
        return self.create_action_executor(plugin_name, **kwargs)

    @staticmethod
    def _sanitize_action_spec(
        *,
        action_id: str,
        desc: str | None,
        kwargs: "KwargsType | None",
        returns: "ReturnType | None",
        tags: list[str],
        default_policy: "ActionPolicy | None",
        side_effect_level: str,
        approval_required: bool,
        sandbox_required: bool,
        replay_safe: bool,
        expose_to_model: bool,
        executor_type: str,
        execution_environments: list[ExecutionEnvironmentRequirement] | None,
        meta: dict[str, Any] | None,
    ) -> "ActionSpec":
        spec = cast(ActionSpec, {
            "action_id": action_id,
            "name": action_id,
            "desc": desc if desc is not None else "",
            "kwargs": kwargs if kwargs is not None else {},
            "tags": tags,
            "default_policy": default_policy if default_policy is not None else {},
            "side_effect_level": side_effect_level,
            "approval_required": approval_required,
            "sandbox_required": sandbox_required,
            "replay_safe": replay_safe,
            "expose_to_model": expose_to_model,
            "executor_type": executor_type,
            "execution_environments": execution_environments if execution_environments is not None else [],
            "meta": meta if meta is not None else {},
        })
        if returns is not None:
            spec["returns"] = returns
        return spec

    def register_action(
        self,
        *,
        action_id: str,
        desc: str | None,
        kwargs: "KwargsType | None",
        func: Callable[..., Any] | None = None,
        executor=None,
        returns: "ReturnType | None" = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        side_effect_level: Literal["read", "write", "exec"] = "read",
        approval_required: bool = False,
        sandbox_required: bool = False,
        replay_safe: bool = True,
        expose_to_model: bool = True,
        execution_environments: list[ExecutionEnvironmentRequirement] | None = None,
        meta: dict[str, Any] | None = None,
    ):
        if executor is None:
            if func is None:
                raise ValueError("register_action() requires either func or executor.")
            executor = self._create_executor("LocalFunctionActionExecutor", func=func)
        normalized_tags = self._normalize_tags(tags)
        executor_type = str(getattr(executor, "kind", "function"))
        spec = self._sanitize_action_spec(
            action_id=action_id,
            desc=desc,
            kwargs=kwargs,
            returns=returns,
            tags=normalized_tags,
            default_policy=default_policy,
            side_effect_level=side_effect_level,
            approval_required=approval_required,
            sandbox_required=sandbox_required,
            replay_safe=replay_safe,
            expose_to_model=expose_to_model,
            executor_type=executor_type,
            execution_environments=execution_environments,
            meta=meta,
        )
        self.action_registry.register(spec, executor, func=func)
        if func is not None:
            self.action_funcs[action_id] = func
        return self

    def register(
        self,
        *,
        name: str | None = None,
        action_id: str | None = None,
        desc: str | None,
        kwargs: "KwargsType | None",
        func: Callable[..., Any],
        returns: "ReturnType | None" = None,
        tags: str | list[str] | None = None,
    ):
        resolved_name = action_id if isinstance(action_id, str) and action_id.strip() != "" else name
        if not isinstance(resolved_name, str) or resolved_name.strip() == "":
            raise ValueError("register() requires either name or action_id.")
        self.register_action(
            action_id=resolved_name,
            desc=desc,
            kwargs=kwargs,
            func=func,
            returns=returns,
            tags=tags,
        )
        return self

    def tag(self, action_ids: str | list[str], tags: str | list[str]):
        self.action_registry.tag(action_ids, tags)
        return self

    def action_func(self, func: Callable[P, R]) -> Callable[P, R]:
        action_id = func.__name__
        desc = inspect.getdoc(func) or func.__name__
        signature = inspect.signature(func)
        type_hints = get_type_hints(func)
        returns = None
        if "return" in type_hints:
            returns = DataFormatter.sanitize(type_hints["return"], remain_type=True)
        kwargs_signature = {}
        for param_name, param in signature.parameters.items():
            annotated_type = param.annotation
            if get_origin(annotated_type) is Annotated:
                base_type, *annotations = get_args(annotated_type)
            else:
                base_type = annotated_type
                annotations = []
            if param.default != inspect.Parameter.empty:
                annotations.append(f"Default: { param.default }")
            kwargs_signature[param_name] = (base_type, ";".join(annotations))
        self.register_action(
            action_id=action_id,
            desc=desc,
            kwargs=kwargs_signature,
            func=func,
            returns=returns,
        )
        return func

    def tool_func(self, func: Callable[P, R]) -> Callable[P, R]:
        return self.action_func(func)

    @staticmethod
    def _normalize_action_items(actions: Any) -> list[Any]:
        if isinstance(actions, str) or callable(actions) or hasattr(actions, "register_actions"):
            return [actions]
        if isinstance(actions, (list, tuple, set)):
            return list(actions)
        return [actions]

    @staticmethod
    def _normalize_registered_action_ids(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item)]
        return []

    def use_actions(
        self,
        actions: Callable | str | list[str | Callable] | Any,
        *,
        tags: str | list[str] | None = None,
    ):
        action_ids: list[str] = []
        for action_item in self._normalize_action_items(actions):
            register_actions = getattr(action_item, "register_actions", None)
            if callable(register_actions):
                action_ids.extend(self._normalize_registered_action_ids(register_actions(self, tags=tags)))
                continue
            if isinstance(action_item, str):
                if not self.action_registry.has(action_item):
                    raise ValueError(f"Can not find action named '{ action_item }'")
                action_ids.append(action_item)
                continue
            if callable(action_item):
                action_name = getattr(action_item, "__name__", "")
                if not action_name:
                    raise ValueError("Callable action must have a __name__.")
                if action_name not in self.action_funcs and not self.action_registry.has(action_name):
                    self.action_func(action_item)
                action_ids.append(action_name)
                continue
            raise TypeError("use_actions() expects action names, callables, or built-in action packages.")
        if tags and action_ids:
            self.tag(action_ids, tags)
        return self

    def use_tools(self, tools: Callable | str | list[str | Callable] | Any, *, tags: str | list[str] | None = None):
        return self.use_actions(tools, tags=tags)

    _RECALL_ACTION_ID = "read_action_artifact"
    _INSTRUCTION_HEAVY_EXECUTOR_TYPES = {
        "bash_sandbox",
        "python_sandbox",
        "nodejs",
        "docker",
        "sqlite",
        "browse",
        "search",
    }
    _INSTRUCTION_HEAVY_KWARGS = {
        "cmd",
        "command",
        "python_code",
        "js_code",
        "code",
        "query",
        "sql",
        "url",
    }
    _SENSITIVE_KEYWORDS = {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "header",
        "password",
        "secret",
        "token",
    }

    @classmethod
    def _is_sensitive_key(cls, key: Any):
        lowered = str(key).lower()
        return any(keyword in lowered for keyword in cls._SENSITIVE_KEYWORDS)

    @staticmethod
    def _compact_text(value: Any, *, limit: int = 700):
        text = str(value)
        if len(text) <= limit:
            return text
        return f"{ text[:limit] }... [truncated { len(text) - limit } chars]"

    @classmethod
    def _compact_value(cls, value: Any, *, limit: int = 700, depth: int = 0):
        if cls._is_sensitive_key(value) and depth < 0:
            return "[REDACTED]"
        if isinstance(value, dict):
            if depth >= 2:
                return f"[dict keys={list(value.keys())[:8]}]"
            compact: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 12:
                    compact["..."] = f"{ len(value) - index } more keys"
                    break
                if cls._is_sensitive_key(key):
                    compact[str(key)] = "[REDACTED]"
                else:
                    compact[str(key)] = cls._compact_value(item, limit=limit, depth=depth + 1)
            return compact
        if isinstance(value, (list, tuple, set)):
            items = list(value)
            compact_items = [cls._compact_value(item, limit=limit, depth=depth + 1) for item in items[:8]]
            if len(items) > 8:
                compact_items.append(f"... { len(items) - 8 } more items")
            return compact_items
        if isinstance(value, str):
            return cls._compact_text(value, limit=limit)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return cls._compact_text(value, limit=limit)

    @classmethod
    def _redaction_report_for_value(cls, value: Any, *, path: str = "") -> list[str]:
        report: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                item_path = f"{ path }.{ key }" if path else str(key)
                if cls._is_sensitive_key(key):
                    report.append(item_path)
                else:
                    report.extend(cls._redaction_report_for_value(item, path=item_path))
        elif isinstance(value, list):
            for index, item in enumerate(value[:20]):
                item_path = f"{ path }[{ index }]" if path else f"[{ index }]"
                report.extend(cls._redaction_report_for_value(item, path=item_path))
        return report

    @classmethod
    def _redact_value(cls, value: Any):
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if cls._is_sensitive_key(key):
                    redacted[str(key)] = "[REDACTED]"
                else:
                    redacted[str(key)] = cls._redact_value(item)
            return redacted
        if isinstance(value, list):
            return [cls._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._redact_value(item) for item in value)
        return value

    @classmethod
    def _safe_json_size(cls, value: Any):
        try:
            return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            return len(str(value).encode("utf-8", errors="ignore"))

    def _register_execution_artifact(
        self,
        *,
        action_call_id: str,
        artifact_type: str,
        label: str,
        value: Any,
        media_type: str = "application/json",
        meta: dict[str, Any] | None = None,
    ) -> ActionArtifact:
        artifact_id = f"act_art_{ uuid.uuid4().hex }"
        safe_value = self._redact_value(value)
        preview = self._compact_value(safe_value, limit=500)
        size = self._safe_json_size(safe_value)
        stored = {
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "label": label,
            "media_type": media_type,
            "value": safe_value,
            "meta": meta or {},
            "size": size,
        }
        self._action_artifacts[artifact_id] = stored
        return {
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": artifact_type,
            "label": label,
            "media_type": media_type,
            "preview": preview,
            "truncated": size > 500,
            "full_value_available": True,
            "available": True,
            "size": size,
            "meta": meta or {},
        }

    def _is_instruction_heavy_record(self, record: "ActionResult"):
        executor_type = str(record.get("executor_type", ""))
        if executor_type in self._INSTRUCTION_HEAVY_EXECUTOR_TYPES:
            return True
        kwargs = record.get("kwargs", {})
        if isinstance(kwargs, dict) and any(key in kwargs for key in self._INSTRUCTION_HEAVY_KWARGS):
            return True
        action_id = str(record.get("action_id", ""))
        return action_id in {
            "run_bash",
            "run_python",
            "run_nodejs",
            "query_sqlite",
            "write_sqlite",
            "browse",
            "search",
            "search_news",
            "search_wikipedia",
            "search_arxiv",
        }

    def _summarize_action_instruction(self, record: "ActionResult"):
        kwargs = record.get("kwargs", {})
        if not isinstance(kwargs, dict):
            return {}
        for key in ("cmd", "command", "python_code", "js_code", "code", "query", "sql", "url"):
            if key in kwargs:
                return {
                    "kind": key,
                    "preview": self._compact_value(kwargs.get(key), limit=900),
                }
        return self._compact_value(kwargs, limit=500)

    def _build_execution_digest(
        self,
        record: "ActionResult",
        *,
        artifact_refs: list[ActionArtifact],
        redaction_report: list[str],
    ) -> dict[str, Any]:
        data = record.get("data", record.get("result"))
        digest: dict[str, Any] = {
            "action_call_id": record.get("action_call_id", ""),
            "action_id": record.get("action_id", ""),
            "purpose": record.get("purpose", ""),
            "status": record.get("status", ""),
            "success": bool(record.get("success", record.get("ok", False))),
            "executor_type": record.get("executor_type", ""),
            "instruction": self._summarize_action_instruction(record),
            "result_preview": self._compact_value(data, limit=900),
            "artifact_refs": artifact_refs,
        }
        error = record.get("error", "")
        if isinstance(error, str) and error:
            digest["error"] = self._compact_text(error, limit=700)
        if redaction_report:
            digest["redaction_report"] = redaction_report
        return digest

    def _finalize_action_result(self, result: Any) -> "ActionResult":
        record = self._normalize_execution_record(result, None, 0) if not isinstance(result, dict) else cast(ActionResult, result)
        meta = record.get("meta", {})
        if not isinstance(meta, dict):
            meta = {}
        recall_meta = meta.get("execution_recall", {})
        if isinstance(recall_meta, dict) and recall_meta.get("finalized") is True:
            return record

        action_call_id = str(record.get("action_call_id", "") or f"act_call_{ uuid.uuid4().hex }")
        record["action_call_id"] = action_call_id

        if not self._is_instruction_heavy_record(record):
            return record

        kwargs = record.get("kwargs", {})
        data = record.get("data", record.get("result"))
        artifact_refs: list[ActionArtifact] = []
        if isinstance(kwargs, dict) and kwargs:
            artifact_refs.append(
                self._register_execution_artifact(
                    action_call_id=action_call_id,
                    artifact_type="action_input",
                    label="Action input arguments",
                    value=kwargs,
                )
            )
        if data is not None:
            artifact_refs.append(
                self._register_execution_artifact(
                    action_call_id=action_call_id,
                    artifact_type="action_output",
                    label="Action raw output",
                    value=data,
                )
            )

        existing_artifacts = record.get("artifacts", [])
        if isinstance(existing_artifacts, list):
            for artifact in existing_artifacts:
                if not isinstance(artifact, dict):
                    continue
                if artifact.get("artifact_id"):
                    artifact_refs.append(cast(ActionArtifact, artifact))
                    continue
                if "value" in artifact:
                    artifact_refs.append(
                        self._register_execution_artifact(
                            action_call_id=action_call_id,
                            artifact_type=str(artifact.get("artifact_type", "artifact")),
                            label=str(artifact.get("label", artifact.get("artifact_type", "artifact"))),
                            value=artifact.get("value"),
                            media_type=str(artifact.get("media_type", "application/json")),
                            meta=cast(dict[str, Any], artifact.get("meta", {})) if isinstance(artifact.get("meta"), dict) else {},
                        )
                    )

        redaction_report = self._redaction_report_for_value(kwargs) if isinstance(kwargs, dict) else []
        meta["execution_recall"] = {
            "finalized": True,
            "digest_version": 1,
            "artifact_count": len(artifact_refs),
        }
        record["meta"] = meta
        record["artifact_refs"] = artifact_refs
        record["artifacts"] = artifact_refs
        record["redaction_report"] = redaction_report
        record["model_digest"] = self._build_execution_digest(
            record,
            artifact_refs=artifact_refs,
            redaction_report=redaction_report,
        )
        return record

    @classmethod
    def _to_model_visible_record(cls, record: "ActionResult") -> "ActionResult":
        if not isinstance(record, dict):
            return record
        digest = record.get("model_digest")
        if not isinstance(digest, dict):
            return record
        visible = cast(ActionResult, dict(record))
        visible["result"] = digest
        visible["data"] = digest
        artifact_refs = record.get("artifact_refs", record.get("artifacts", []))
        visible["artifacts"] = artifact_refs if isinstance(artifact_refs, list) else []
        return visible

    @classmethod
    def to_model_visible_records(cls, records: list["ActionResult"] | None):
        if not isinstance(records, list):
            return []
        return [cls._to_model_visible_record(record) for record in records]

    def _with_action_artifact_recall_action(
        self,
        action_list: list[dict[str, Any]],
        records: list["ActionResult"] | None,
    ):
        if not isinstance(records, list):
            return action_list
        has_artifact_refs = any(
            isinstance(record, dict)
            and (
                isinstance(record.get("artifact_refs"), list)
                and len(record.get("artifact_refs", [])) > 0
                or isinstance(record.get("artifacts"), list)
                and any(isinstance(item, dict) and item.get("artifact_id") for item in record.get("artifacts", []))
            )
            for record in records
        )
        if not has_artifact_refs:
            return action_list
        if any(item.get("action_id") == self._RECALL_ACTION_ID for item in action_list if isinstance(item, dict)):
            return action_list
        recall_spec = self.action_registry.get_spec(self._RECALL_ACTION_ID)
        if recall_spec is None:
            return action_list
        return [*action_list, dict(recall_spec, expose_to_model=True)]

    def _iter_action_ids(self, tags: str | list[str] | None = None, *, expose_only: bool = True):
        if tags is None:
            action_ids = self.action_registry.list_action_ids()
            collected = []
            for action_id in action_ids:
                spec = self.action_registry.get_spec(action_id)
                if spec is None:
                    continue
                if expose_only and spec.get("expose_to_model", True) is not True:
                    continue
                if any(tag.startswith("agent-") for tag in self.action_registry.get_tags(action_id)):
                    continue
                collected.append(action_id)
            return collected

        action_ids = self.action_registry.list_action_ids(tags)
        collected = []
        for action_id in action_ids:
            spec = self.action_registry.get_spec(action_id)
            if spec is None:
                continue
            if expose_only and spec.get("expose_to_model", True) is not True:
                continue
            collected.append(action_id)
        return collected

    def get_action_info(self, tags: str | list[str] | None = None):
        action_info: dict[str, dict[str, Any]] = {}
        for action_id in self._iter_action_ids(tags, expose_only=True):
            spec = self.action_registry.get_spec(action_id)
            if spec is None:
                continue
            action_info[action_id] = dict(spec)
        return action_info

    def get_tool_info(self, tags: str | list[str] | None = None):
        tool_info: dict[str, dict[str, Any]] = {}
        for action_id, spec in self.get_action_info(tags).items():
            tool_spec = {
                "name": spec.get("name", action_id),
                "desc": spec.get("desc", ""),
                "kwargs": spec.get("kwargs", {}),
            }
            if "returns" in spec:
                tool_spec["returns"] = spec["returns"]
            tool_info[action_id] = tool_spec
        return tool_info

    def get_action_list(self, tags: str | list[str] | None = None):
        return list(self.get_action_info(tags).values())

    def get_tool_list(self, tags: str | list[str] | None = None):
        return list(self.get_tool_info(tags).values())

    def get_action_func(
        self,
        name: str,
        *,
        shift: Literal["sync", "async"] | None = None,
    ) -> Callable[..., Coroutine] | Callable[..., Any] | None:
        action_func = self.action_funcs[name] if name in self.action_funcs else None
        if action_func is None and self.action_registry.has(name):

            async def _call_action(**kwargs):
                return await self.async_call_action(name, kwargs)

            action_func = _call_action
        if action_func is None:
            return None
        match shift:
            case "sync":
                return FunctionShifter.syncify(action_func)
            case "async":
                return FunctionShifter.asyncify(action_func)
            case None:
                return action_func

    def get_tool_func(
        self,
        name: str,
        *,
        shift: Literal["sync", "async"] | None = None,
    ) -> Callable[..., Coroutine] | Callable[..., Any] | None:
        return self.get_action_func(name, shift=shift)

    def _legacy_error(self, name: str, *, as_tool: bool):
        subject = "tool" if as_tool else "action"
        return f"Can not find { subject } named '{ name }'"

    def _legacy_result(self, result: Any, *, as_tool: bool):
        if not isinstance(result, dict):
            return None
        status = str(result.get("status", "success"))
        data = result.get("data", result.get("result"))
        error = str(result.get("error", ""))
        if status == "success":
            return data
        if isinstance(data, dict) and "error" in data:
            return data
        if status in {"approval_required", "blocked"}:
            return {
                "status": status,
                "error": error or f"{'Tool' if as_tool else 'Action'} execution is blocked.",
                "approval": result.get("approval", {}),
            }
        label = "Tool" if as_tool else "Action"
        return f"Error: { error if error else f'{ label } execution failed.' }"

    async def async_execute_action(
        self,
        name: str,
        kwargs: dict[str, Any],
        *,
        settings: "Settings | None" = None,
        purpose: str | None = None,
        policy_override: "ActionPolicy | None" = None,
        source_protocol: str = "direct",
        todo_suggestion: str = "",
        next_value: str = "",
    ):
        result = await self.action_dispatcher.async_execute(
            name,
            kwargs,
            settings=settings,
            purpose=purpose,
            policy_override=policy_override,
            source_protocol=source_protocol,
            todo_suggestion=todo_suggestion,
            next_value=next_value,
        )
        return self._finalize_action_result(result)

    def execute_action(self, name: str, kwargs: dict[str, Any], **kwargs_options):
        return FunctionShifter.syncify(self.async_execute_action)(name, kwargs, **kwargs_options)

    async def async_call_action(self, name: str, kwargs: dict[str, Any]) -> Any:
        if not self.action_registry.has(name):
            return self._legacy_error(name, as_tool=False)
        result = await self.async_execute_action(name, kwargs)
        return self._legacy_result(result, as_tool=False)

    def call_action(self, name: str, kwargs: dict[str, Any]) -> Any:
        return FunctionShifter.syncify(self.async_call_action)(name, kwargs)

    async def async_call_tool(self, name: str, kwargs: dict[str, Any]) -> Any:
        if not self.action_registry.has(name):
            return self._legacy_error(name, as_tool=True)
        result = await self.async_execute_action(name, kwargs)
        return self._legacy_result(result, as_tool=True)

    def call_tool(self, name: str, kwargs: dict[str, Any]) -> Any:
        return FunctionShifter.syncify(self.async_call_tool)(name, kwargs)

    async def async_use_action_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        side_effect_level: Literal["read", "write", "exec"] = "read",
        approval_required: bool = False,
        sandbox_required: bool = False,
        replay_safe: bool = True,
        expose_to_model: bool = True,
    ):
        LazyImport.import_package("fastmcp", version_constraint=">=3")
        from fastmcp import Client

        normalized_tags = self._normalize_tags(tags)

        async with Client(transport) as client:  # type: ignore[arg-type]
            tool_list = await client.list_tools()
            for tool in tool_list:
                tool_tags = []
                if hasattr(tool, "_meta") and tool._meta:  # type: ignore[attr-defined]
                    tool_tags = tool._meta.get("_fastmcp", {}).get("tags", [])  # type: ignore[index]
                tool_tags.extend(normalized_tags)
                self.register_action(
                    action_id=tool.name,
                    desc=tool.description,
                    kwargs=DataFormatter.from_schema_to_kwargs_format(tool.inputSchema),
                    returns=DataFormatter.from_schema_to_kwargs_format(tool.outputSchema),
                    executor=self._create_executor(
                        "MCPActionExecutor",
                        action_id=tool.name,
                        transport=transport,
                    ),
                    tags=tool_tags,
                    default_policy=default_policy,
                    side_effect_level=side_effect_level,
                    approval_required=approval_required,
                    sandbox_required=sandbox_required,
                    replay_safe=replay_safe,
                    expose_to_model=expose_to_model,
                    execution_environments=cast(list[ExecutionEnvironmentRequirement], [
                        {
                            "requirement_id": f"mcp:{ tool.name }",
                            "kind": "mcp",
                            "scope": "agent",
                            "resource_key": tool.name,
                            "config": {"transport": transport},
                            "policy": cast(ExecutionEnvironmentPolicy, default_policy or {}),
                            "approval_required": approval_required,
                        }
                    ]),
                )
        return self

    async def async_use_mcp(self, transport: "MCPConfigs | str | Any", *, tags: str | list[str] | None = None):
        await self.async_use_action_mcp(transport, tags=tags)
        return self

    def register_python_sandbox_action(
        self,
        *,
        action_id: str = "python_sandbox",
        desc: str = "Execute Python code inside a restricted sandbox.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        preset_objects: dict[str, object] | None = None,
        base_vars: dict[str, Any] | None = None,
        allowed_return_types: list[type] | None = None,
    ):
        self.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={"python_code": (str, "Python code to execute in the sandbox.")},
            executor=self._create_executor(
                "PythonSandboxActionExecutor",
                preset_objects=preset_objects,
                base_vars=base_vars,
                allowed_return_types=allowed_return_types,
            ),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_environments=cast(list[ExecutionEnvironmentRequirement], [
                {
                    "requirement_id": f"python:{ action_id }",
                    "kind": "python",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "preset_objects": preset_objects,
                        "base_vars": base_vars,
                        "allowed_return_types": allowed_return_types,
                    },
                    "policy": cast(ExecutionEnvironmentPolicy, default_policy or {}),
                }
            ]),
        )
        return self

    def register_bash_sandbox_action(
        self,
        *,
        action_id: str = "bash_sandbox",
        desc: str = "Execute a shell command inside a constrained sandbox.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        allowed_cmd_prefixes: list[str] | None = None,
        allowed_workdir_roots: list[str] | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
    ):
        self.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "cmd": ("str | list[str]", "Command to run inside the sandbox."),
                "workdir": ("str | None", "Working directory inside allowed roots."),
                "allow_unsafe": ("bool", "Bypass the command allowlist."),
            },
            executor=self._create_executor(
                "BashSandboxActionExecutor",
                allowed_cmd_prefixes=allowed_cmd_prefixes,
                allowed_workdir_roots=allowed_workdir_roots,
                timeout=timeout,
                env=env,
            ),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_environments=cast(list[ExecutionEnvironmentRequirement], [
                {
                    "requirement_id": f"bash:{ action_id }",
                    "kind": "bash",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "allowed_cmd_prefixes": allowed_cmd_prefixes,
                        "allowed_workdir_roots": allowed_workdir_roots,
                        "timeout": timeout,
                        "env": env,
                    },
                    "policy": cast(ExecutionEnvironmentPolicy, default_policy or {}),
                }
            ]),
        )
        return self

    def register_nodejs_action(
        self,
        *,
        action_id: str = "run_nodejs",
        desc: str = "Execute JavaScript with Node.js inside a managed execution environment.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        node_binary: str = "node",
        cwd: str | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
    ):
        self.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "js_code": (str, "JavaScript code to execute with Node.js."),
                "args": ("list[str]", "Optional command-line arguments."),
            },
            executor=self._create_executor("NodeJSActionExecutor", timeout=timeout),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_environments=cast(list[ExecutionEnvironmentRequirement], [
                {
                    "requirement_id": f"node:{ action_id }",
                    "kind": "node",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "node_binary": node_binary,
                        "cwd": cwd,
                        "timeout": timeout,
                        "env": env,
                    },
                    "policy": cast(ExecutionEnvironmentPolicy, default_policy or {}),
                }
            ]),
        )
        return self

    def register_docker_action(
        self,
        *,
        action_id: str = "run_docker",
        desc: str = "Run a command in a Docker container through a managed execution environment.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        image: str | None = None,
        timeout: int = 60,
        docker_binary: str = "docker",
        default_args: list[str] | None = None,
    ):
        self.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "image": ("str | None", "Docker image. Defaults to the configured image."),
                "cmd": ("str | list[str]", "Command to run in the container."),
                "workdir": ("str | None", "Container working directory."),
                "env": ("dict[str, str] | None", "Container environment variables."),
            },
            executor=self._create_executor("DockerActionExecutor", image=image, timeout=timeout),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            approval_required=True,
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_environments=cast(list[ExecutionEnvironmentRequirement], [
                {
                    "requirement_id": f"docker:{ action_id }",
                    "kind": "docker",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "docker_binary": docker_binary,
                        "timeout": timeout,
                        "default_args": default_args or [],
                    },
                    "policy": cast(ExecutionEnvironmentPolicy, default_policy or {}),
                    "approval_required": True,
                }
            ]),
        )
        return self

    def register_sqlite_action(
        self,
        *,
        action_id: str = "query_sqlite",
        desc: str = "Query a SQLite database through a managed execution environment.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        database: str = ":memory:",
        read_only: bool = True,
        uri: bool = False,
    ):
        merged_policy: ActionPolicy = cast(ActionPolicy, dict(default_policy or {}))
        merged_policy.setdefault("read_only", read_only)
        self.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "query": (str, "SQLite query to execute."),
                "params": ("list | dict | None", "Optional SQLite query parameters."),
            },
            executor=self._create_executor("SQLiteActionExecutor", read_only=read_only),
            tags=tags,
            default_policy=merged_policy,
            side_effect_level="read" if read_only else "write",
            expose_to_model=expose_to_model,
            execution_environments=cast(list[ExecutionEnvironmentRequirement], [
                {
                    "requirement_id": f"sqlite:{ action_id }",
                    "kind": "sqlite",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "database": database,
                        "uri": uri,
                    },
                    "policy": cast(ExecutionEnvironmentPolicy, merged_policy),
                }
            ]),
        )
        return self

    def _create_action_runtime(self, plugin_name: str | None = None):
        runtime_name = plugin_name
        if not isinstance(runtime_name, str) or runtime_name.strip() == "":
            runtime_name = str(self.settings["plugins.ActionRuntime.activate"])
        runtime_plugin = cast(type[Any], self.plugin_manager.get_plugin("ActionRuntime", runtime_name))
        return runtime_plugin(action=self, plugin_manager=self.plugin_manager, settings=self.settings)

    def create_action_runtime(self, plugin_name: str, **kwargs):
        runtime_plugin = cast(type[Any], self.plugin_manager.get_plugin("ActionRuntime", plugin_name))
        return runtime_plugin(action=self, plugin_manager=self.plugin_manager, settings=self.settings, **kwargs)

    def _create_action_flow(self, plugin_name: str | None = None):
        flow_name = plugin_name
        if not isinstance(flow_name, str) or flow_name.strip() == "":
            flow_name = str(self.settings["plugins.ActionFlow.activate"])
        flow_plugin = cast(type[Any], self.plugin_manager.get_plugin("ActionFlow", flow_name))
        return flow_plugin(plugin_manager=self.plugin_manager, settings=self.settings)

    def create_action_flow(self, plugin_name: str, **kwargs):
        flow_plugin = cast(type[Any], self.plugin_manager.get_plugin("ActionFlow", plugin_name))
        return flow_plugin(plugin_manager=self.plugin_manager, settings=self.settings, **kwargs)

    def set_loop_options(
        self,
        *,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ):
        if max_rounds is not None:
            if not isinstance(max_rounds, int) or max_rounds < 0:
                raise ValueError("max_rounds must be an integer >= 0.")
            self.action_settings.set("loop.max_rounds", max_rounds)
            self.tool_settings.set("loop.max_rounds", max_rounds)
        if concurrency is not None:
            if not isinstance(concurrency, int) or concurrency <= 0:
                raise ValueError("concurrency must be an integer > 0.")
            self.action_settings.set("loop.concurrency", concurrency)
            self.tool_settings.set("loop.concurrency", concurrency)
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("timeout must be a number > 0.")
            self.action_settings.set("loop.timeout", float(timeout))
            self.tool_settings.set("loop.timeout", float(timeout))
        return self

    def register_action_planning_handler(self, handler: "ActionPlanningHandler | None"):
        self.action_runtime.register_action_planning_handler(handler)
        return self

    def register_plan_analysis_handler(self, handler: "ActionPlanningHandler | None"):
        return self.register_action_planning_handler(handler)

    def register_action_execution_handler(self, handler: "ActionExecutionHandler | None"):
        self.action_runtime.register_action_execution_handler(handler)
        return self

    def register_tool_execution_handler(self, handler: "ActionExecutionHandler | None"):
        return self.register_action_execution_handler(handler)

    def _resolve_planning_protocol(self, settings: "Settings", planning_protocol: str | None = None):
        return self.action_runtime.resolve_planning_protocol(settings, planning_protocol)

    async def _default_structured_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        runtime = cast(Any, self.action_runtime)
        return await runtime._default_structured_planning_handler(context, request)

    async def _default_native_tool_call_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        runtime = cast(Any, self.action_runtime)
        return await runtime._default_native_tool_call_planning_handler(context, request)

    async def _default_planning_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        runtime = cast(Any, self.action_runtime)
        return await runtime._default_planning_handler(context, request)

    async def _default_plan_analysis_handler(self, context: ActionRunContext, request: ActionPlanningRequest):
        runtime = cast(Any, self.action_runtime)
        return await runtime._default_structured_planning_handler(context, request)

    async def _default_action_execution_handler(self, context: ActionRunContext, request: ActionExecutionRequest):
        runtime = cast(Any, self.action_runtime)
        return await runtime._default_action_execution_handler(context, request)

    async def _default_tool_execution_handler(self, context: ActionRunContext, request: ActionExecutionRequest):
        runtime = cast(Any, self.action_runtime)
        return await runtime._default_action_execution_handler(context, request)

    @staticmethod
    def _is_next_action_path(path: Any) -> bool:
        if not isinstance(path, str):
            return False
        normalized = path.strip()
        if normalized.startswith("$"):
            normalized = normalized[1:]
        normalized = normalized.lstrip("./")
        return normalized == "next_action"

    @staticmethod
    async def _try_close_response_stream(response: Any):
        from agently.utils.GeneratorConsumer import GeneratorConsumer

        result = getattr(response, "result", None)
        parser = getattr(result, "_response_parser", None)
        consumer = getattr(parser, "_response_consumer", None)
        if isinstance(consumer, GeneratorConsumer):
            return
        close = getattr(consumer, "close", None)
        if callable(close):
            maybe_coroutine = close()
            if asyncio.iscoroutine(maybe_coroutine):
                await maybe_coroutine

    @staticmethod
    def _parse_native_arguments(raw_arguments: Any):
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            return {}
        text = raw_arguments.strip()
        if text == "":
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw_arguments": text}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    @classmethod
    def _normalize_native_action_calls(cls, tool_call_chunks: list[Any]) -> list[ActionCall]:
        collected: dict[int, dict[str, Any]] = {}

        def merge_one(item: Any, fallback_index: int):
            if not isinstance(item, dict):
                return
            index = item.get("index", fallback_index)
            if not isinstance(index, int):
                index = fallback_index
            current = collected.setdefault(
                index,
                {
                    "id": item.get("id"),
                    "type": item.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                },
            )
            if item.get("id"):
                current["id"] = item["id"]
            if item.get("type"):
                current["type"] = item["type"]
            function = item.get("function", {})
            if isinstance(function, dict):
                current_function = current.setdefault("function", {"name": "", "arguments": ""})
                name = function.get("name")
                if isinstance(name, str) and name:
                    current_function["name"] = name if not current_function.get("name") else current_function["name"] + name
                arguments = function.get("arguments")
                if isinstance(arguments, dict):
                    current_function["arguments"] = json.dumps(arguments, ensure_ascii=False)
                elif isinstance(arguments, str):
                    current_function["arguments"] = str(current_function.get("arguments", "")) + arguments

        for chunk in tool_call_chunks:
            if isinstance(chunk, list):
                for index, item in enumerate(chunk):
                    merge_one(item, index)
            else:
                merge_one(chunk, len(collected))

        action_calls: list[ActionCall] = []
        for index in sorted(collected.keys()):
            function = collected[index].get("function", {})
            action_id = function.get("name")
            if not isinstance(action_id, str) or action_id.strip() == "":
                continue
            parsed_arguments = cls._parse_native_arguments(function.get("arguments", ""))
            action_calls.append(
                {
                    "purpose": f"Use { action_id }",
                    "action_id": action_id,
                    "action_input": parsed_arguments,
                    "policy_override": {},
                    "source_protocol": "native_tool_calls",
                    "todo_suggestion": "",
                    "next": "",
                    "tool_name": action_id,
                    "tool_kwargs": parsed_arguments,
                }
            )
        return action_calls

    async def async_generate_action_call(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        planning_handler: "ActionPlanningHandler | None" = None,
        done_plans: list[ActionResult] | None = None,
        last_round_records: list[ActionResult] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list[ActionCall]:
        return await self.action_runtime.async_generate_action_call(
            prompt=prompt,
            settings=settings,
            action_list=action_list,
            agent_name=agent_name,
            planning_handler=planning_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

    async def async_generate_tool_command(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        tool_list: list[dict[str, Any]],
        agent_name: str = "Manual",
        plan_analysis_handler: "ActionPlanningHandler | None" = None,
        done_plans: list[ActionResult] | None = None,
        last_round_records: list[ActionResult] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list[ActionCall]:
        return await self.action_runtime.async_generate_tool_command(
            prompt=prompt,
            settings=settings,
            tool_list=tool_list,
            agent_name=agent_name,
            plan_analysis_handler=plan_analysis_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    @staticmethod
    def is_execution_error_result(result: Any):
        if isinstance(result, dict) and isinstance(result.get("status"), str):
            return result.get("status") != "success"
        if not isinstance(result, str):
            return False
        stripped = result.strip()
        return stripped.startswith("Error:") or stripped.startswith("Can not find tool named")

    @staticmethod
    def _normalize_action_call(command: Any, *, fallback_next: str | None = None) -> "ActionCall | None":
        if not isinstance(command, dict):
            return None

        action_id = command.get("action_id")
        if not isinstance(action_id, str) or action_id.strip() == "":
            action_id = command.get("tool_name")
        if not isinstance(action_id, str) or action_id.strip() == "":
            return None

        purpose = command.get("purpose")
        if not isinstance(purpose, str) or purpose.strip() == "":
            purpose = f"Use { action_id }"

        action_input = command.get("action_input", command.get("tool_kwargs", {}))
        if not isinstance(action_input, dict):
            action_input = {}

        policy_override = command.get("policy_override", {})
        if not isinstance(policy_override, dict):
            policy_override = {}

        command_next = command.get("todo_suggestion")
        if not isinstance(command_next, str) or command_next.strip() == "":
            command_next = command.get("next")
        if not isinstance(command_next, str) or command_next.strip() == "":
            command_next = fallback_next if isinstance(fallback_next, str) and fallback_next.strip() != "" else ""

        action_call: dict[str, Any] = {
            "purpose": purpose,
            "action_id": action_id,
            "action_input": action_input,
            "policy_override": policy_override,
            "source_protocol": str(command.get("source_protocol", "structured_plan")),
            "todo_suggestion": command_next,
            "next": command_next,
            "tool_name": action_id,
            "tool_kwargs": action_input,
        }
        return cast(ActionCall, action_call)

    def _normalize_action_decision(self, decision: Any) -> "ActionDecision":
        if not isinstance(decision, dict):
            return {
                "next_action": "response",
                "use_action": False,
                "next": "",
                "action_calls": [],
            }

        fallback_next = decision.get("todo_suggestion")
        if not isinstance(fallback_next, str):
            fallback_next = decision.get("next")
        if not isinstance(fallback_next, str):
            fallback_next = ""

        action_calls: list[ActionCall] = []
        command_key: str | None = None
        for key in ("execution_actions", "action_calls", "execution_commands", "tool_commands"):
            if isinstance(decision.get(key), list):
                command_key = key
                break
        if command_key:
            for command in decision[command_key]:
                action_call = self._normalize_action_call(command, fallback_next=fallback_next)
                if action_call is not None:
                    action_calls.append(action_call)

        if len(action_calls) == 0:
            for single_key in ("action_call", "tool_command"):
                if single_key in decision:
                    action_call = self._normalize_action_call(decision.get(single_key), fallback_next=fallback_next)
                    if action_call is not None:
                        action_calls.append(action_call)
                        break

        next_action = decision.get("next_action")
        if not isinstance(next_action, str) or next_action.strip() == "":
            next_action = "execute" if len(action_calls) > 0 else "response"
        next_action = next_action.lower()
        if next_action not in {"execute", "response"}:
            next_action = "execute" if len(action_calls) > 0 else "response"

        use_action = decision.get("use_action")
        if not isinstance(use_action, bool):
            use_action = decision.get("use_tool")
        if isinstance(use_action, bool):
            final_use_action = use_action and len(action_calls) > 0 and next_action == "execute"
        else:
            final_use_action = len(action_calls) > 0 and next_action == "execute"

        if not final_use_action:
            action_calls = []
            next_action = "response"

        return {
            "next_action": next_action,
            "use_action": final_use_action,
            "next": fallback_next,
            "execution_actions": action_calls,
            "action_calls": action_calls,
            "execution_commands": action_calls,
            "tool_commands": action_calls,
        }

    def _normalize_execution_record(
        self,
        record: Any,
        command: "ActionCall | None",
        index: int,
    ) -> "ActionResult":
        if command is None:
            command = {}

        fallback_action_id = str(command.get("action_id", command.get("tool_name", "")))
        fallback_kwargs = command.get("action_input", command.get("tool_kwargs", {}))
        if not isinstance(fallback_kwargs, dict):
            fallback_kwargs = {}
        fallback_purpose = str(command.get("purpose", f"action_call_{ index + 1 }"))
        fallback_next = str(command.get("todo_suggestion", command.get("next", "")))

        if isinstance(record, dict):
            action_id = record.get("action_id", fallback_action_id)
            if not isinstance(action_id, str):
                action_id = fallback_action_id

            kwargs = record.get("kwargs", fallback_kwargs)
            if not isinstance(kwargs, dict):
                kwargs = fallback_kwargs

            purpose = record.get("purpose", fallback_purpose)
            if not isinstance(purpose, str):
                purpose = fallback_purpose

            next_step = record.get("todo_suggestion", record.get("next", fallback_next))
            if not isinstance(next_step, str):
                next_step = fallback_next

            result = record.get("result", record.get("data"))
            error = record.get("error", "")
            if not isinstance(error, str):
                error = str(error)

            status = record.get("status", "success" if error == "" else "error")
            if not isinstance(status, str):
                status = "success" if error == "" else "error"

            success = record.get("success")
            if not isinstance(success, bool):
                success = status == "success" and not self.is_execution_error_result(result)

            if not success and error == "":
                error = str(result) if result is not None else "Action execution failed."

            normalized: ActionResult = {
                "action_call_id": str(record.get("action_call_id", "")),
                "ok": bool(record.get("ok", success)),
                "status": cast(Any, status),
                "purpose": purpose,
                "action_id": action_id,
                "tool_name": str(record.get("tool_name", action_id)),
                "kwargs": dict(kwargs),
                "todo_suggestion": next_step,
                "next": next_step,
                "success": success,
                "result": result,
                "data": record.get("data", result),
                "model_digest": record.get("model_digest", {}),
                "artifact_refs": record.get("artifact_refs", []),
                "artifacts": record.get("artifacts", []),
                "diagnostics": record.get("diagnostics", []),
                "approval": record.get("approval", {}),
                "timing": record.get("timing", {}),
                "meta": record.get("meta", {}),
                "redaction_report": record.get("redaction_report", []),
                "error": error,
                "expose_to_model": bool(record.get("expose_to_model", True)),
                "side_effect_level": cast(Any, record.get("side_effect_level", "read")),
                "executor_type": str(record.get("executor_type", "")),
            }
            if not isinstance(normalized.get("model_digest"), dict) or not normalized.get("model_digest"):
                normalized.pop("model_digest", None)
            return normalized

        result = record
        success = not self.is_execution_error_result(result)
        return {
            "action_call_id": "",
            "ok": success,
            "status": "success" if success else "error",
            "purpose": fallback_purpose,
            "action_id": fallback_action_id,
            "tool_name": fallback_action_id,
            "kwargs": dict(fallback_kwargs),
            "todo_suggestion": fallback_next,
            "next": fallback_next,
            "success": success,
            "result": result,
            "data": result,
            "artifact_refs": [],
            "artifacts": [],
            "diagnostics": [],
            "approval": {},
            "timing": {},
            "meta": {},
            "redaction_report": [],
            "error": "" if success else str(result),
            "expose_to_model": True,
            "side_effect_level": "read",
            "executor_type": "",
        }

    def _normalize_execution_records(
        self,
        records: Any,
        commands: list["ActionCall"],
    ) -> list["ActionResult"]:
        if not isinstance(records, list):
            return []

        normalized: list[ActionResult] = []
        for index, record in enumerate(records):
            command = commands[index] if index < len(commands) else None
            normalized.append(self._finalize_action_result(self._normalize_execution_record(record, command, index)))
        return normalized

    @staticmethod
    def to_action_results(records: list["ActionResult"]):
        action_results: dict[str, Any] = {}
        used_keys: set[str] = set()

        for index, record in enumerate(records):
            purpose = record.get("purpose")
            if not isinstance(purpose, str) or purpose.strip() == "":
                purpose = f"action_call_{ index + 1 }"

            key = purpose
            suffix = 2
            while key in used_keys:
                key = f"{ purpose } ({ suffix })"
                suffix += 1

            used_keys.add(key)
            model_digest = record.get("model_digest")
            result_value = model_digest if isinstance(model_digest, dict) else record.get("result", record.get("data"))
            if record.get("success"):
                action_results[key] = result_value
            else:
                action_results[key] = {
                    "error": record.get("error", "Action execution failed."),
                    "result": result_value,
                    "status": record.get("status", "error"),
                }

        return action_results

    @staticmethod
    def _should_continue(
        decision: "ActionDecision",
        *,
        round_index: int,
        max_rounds: int | None,
    ):
        if isinstance(max_rounds, int) and max_rounds >= 0 and round_index >= max_rounds:
            return False
        if decision.get("next_action") != "execute":
            return False
        if decision.get("use_action") is not True:
            return False
        commands = decision.get("action_calls")
        return isinstance(commands, list) and len(commands) > 0

    async def async_plan_and_execute(
        self,
        *,
        prompt: "Prompt",
        settings: "Settings",
        action_list: list[dict[str, Any]] | None = None,
        tool_list: list[dict[str, Any]] | None = None,
        agent_name: str = "Manual",
        parent_run_context=None,
        planning_handler: "ActionPlanningHandler | None" = None,
        plan_analysis_handler: "ActionPlanningHandler | None" = None,
        action_execution_handler: "ActionExecutionHandler | None" = None,
        tool_execution_handler: "ActionExecutionHandler | None" = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
    ) -> list["ActionResult"]:
        resolved_action_list = action_list if isinstance(action_list, list) else tool_list if isinstance(tool_list, list) else []
        if len(resolved_action_list) == 0:
            return []

        selected_planning_handler = planning_handler if planning_handler is not None else plan_analysis_handler
        selected_execution_handler = (
            action_execution_handler if action_execution_handler is not None else tool_execution_handler
        )

        return await self.action_flow.async_run(
            action=self,
            prompt=prompt,
            settings=settings,
            action_list=resolved_action_list,
            agent_name=agent_name,
            parent_run_context=parent_run_context,
            planning_handler=self.action_runtime.resolve_planning_handler(selected_planning_handler),
            execution_handler=self.action_runtime.resolve_execution_handler(selected_execution_handler),
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
            planning_protocol=planning_protocol,
        )


ToolCommand = ActionCall
ToolPlanDecision = ActionDecision
ToolExecutionRecord = ActionResult
ToolPlanAnalysisHandler = ActionPlanningHandler
StandardToolPlanAnalysisHandler = StandardActionPlanningHandler
ToolExecutionHandler = ActionExecutionHandler
StandardToolExecutionHandler = StandardActionExecutionHandler
