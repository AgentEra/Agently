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

import json
import uuid
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

from agently.types.data import (
    ExecutionEnvironmentDecision,
    ExecutionEnvironmentHandle,
    ExecutionEnvironmentPolicy,
    ExecutionEnvironmentRequirement,
    ExecutionEnvironmentScope,
    ExecutionEnvironmentStatus,
)
from agently.utils import FunctionShifter

if TYPE_CHECKING:
    from agently.core.EventCenter import EventCenter
    from agently.core.PluginManager import PluginManager
    from agently.types.plugins import ExecutionEnvironmentProvider
    from agently.utils import Settings


ExecutionEnvironmentDecisionHandler = Callable[
    [ExecutionEnvironmentRequirement, ExecutionEnvironmentPolicy],
    bool | ExecutionEnvironmentDecision | Awaitable[bool | ExecutionEnvironmentDecision],
]


class ExecutionEnvironmentError(RuntimeError):
    def __init__(self, message: str, *, code: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload if payload is not None else {}


class ExecutionEnvironmentApprovalRequired(ExecutionEnvironmentError):
    def __init__(self, requirement: ExecutionEnvironmentRequirement, policy: ExecutionEnvironmentPolicy):
        super().__init__(
            "Execution environment approval is required.",
            code="execution_environment.approval_required",
            payload={
                "requirement_id": requirement.get("requirement_id", ""),
                "kind": requirement.get("kind", ""),
                "scope": requirement.get("scope", ""),
                "owner_id": requirement.get("owner_id", ""),
                "resource_key": requirement.get("resource_key", ""),
                "policy": dict(policy),
            },
        )


class ExecutionEnvironmentApprovalDenied(ExecutionEnvironmentError):
    def __init__(self, requirement: ExecutionEnvironmentRequirement, reason: str):
        super().__init__(
            reason or "Execution environment approval was denied.",
            code="execution_environment.approval_denied",
            payload={
                "requirement_id": requirement.get("requirement_id", ""),
                "kind": requirement.get("kind", ""),
                "scope": requirement.get("scope", ""),
                "owner_id": requirement.get("owner_id", ""),
                "resource_key": requirement.get("resource_key", ""),
            },
        )


class ExecutionEnvironmentManager:
    def __init__(
        self,
        *,
        plugin_manager: "PluginManager",
        settings: "Settings",
        event_center: "EventCenter",
    ):
        self.plugin_manager = plugin_manager
        self.settings = settings
        self.event_center = event_center
        self._requirements: dict[str, ExecutionEnvironmentRequirement] = {}
        self._handles: dict[str, ExecutionEnvironmentHandle] = {}
        self._handles_by_reuse_key: dict[str, str] = {}
        self._providers: dict[str, "ExecutionEnvironmentProvider"] = {}
        self._decision_handler: ExecutionEnvironmentDecisionHandler | None = None

        self.ensure = FunctionShifter.syncify(self.async_ensure)
        self.release = FunctionShifter.syncify(self.async_release)
        self.release_scope = FunctionShifter.syncify(self.async_release_scope)

    def register_provider(self, provider: "ExecutionEnvironmentProvider"):
        self._providers[str(provider.kind)] = provider
        return self

    def _get_provider(self, kind: str):
        if kind in self._providers:
            return self._providers[kind]
        try:
            plugin_names = self.plugin_manager.get_plugin_list("ExecutionEnvironmentProvider")
        except Exception:
            plugin_names = []
        for plugin_name in plugin_names:
            plugin_class = cast(Any, self.plugin_manager.get_plugin("ExecutionEnvironmentProvider", plugin_name))
            provider = plugin_class()
            self.register_provider(provider)
            if provider.kind == kind:
                return provider
        raise ExecutionEnvironmentError(
            f"Can not find ExecutionEnvironmentProvider for kind '{ kind }'.",
            code="execution_environment.provider_missing",
            payload={"kind": kind},
        )

    @staticmethod
    def _stable_json(value: Any):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)

    def _derive_reuse_key(self, requirement: ExecutionEnvironmentRequirement):
        if requirement.get("reuse_key"):
            return str(requirement.get("reuse_key", ""))
        stable_parts = {
            "kind": requirement.get("kind", ""),
            "scope": requirement.get("scope", ""),
            "owner_id": requirement.get("owner_id", ""),
            "resource_key": requirement.get("resource_key", ""),
            "config": requirement.get("config", {}),
        }
        return self._stable_json(stable_parts)

    def _normalize_requirement(
        self,
        requirement: ExecutionEnvironmentRequirement,
        *,
        scope: ExecutionEnvironmentScope | None = None,
        owner_id: str | None = None,
    ):
        normalized = cast(ExecutionEnvironmentRequirement, dict(requirement))
        kind = str(normalized.get("kind", "")).strip()
        if not kind:
            raise ValueError("ExecutionEnvironmentRequirement.kind is required.")
        normalized["kind"] = kind
        normalized["scope"] = cast(ExecutionEnvironmentScope, scope or normalized.get("scope", "action_call"))
        normalized["owner_id"] = str(owner_id or normalized.get("owner_id", "Agently"))
        normalized["resource_key"] = str(normalized.get("resource_key", kind))
        normalized["config"] = dict(normalized.get("config", {}))
        normalized["policy"] = cast(ExecutionEnvironmentPolicy, dict(normalized.get("policy", {})))
        normalized["meta"] = dict(normalized.get("meta", {}))
        if not normalized.get("requirement_id"):
            normalized["requirement_id"] = f"{ normalized['kind'] }:{ uuid.uuid4().hex }"
        normalized["reuse_key"] = self._derive_reuse_key(normalized)
        return normalized

    @staticmethod
    def _event_payload(
        requirement: ExecutionEnvironmentRequirement | None = None,
        handle: ExecutionEnvironmentHandle | None = None,
        *,
        status: ExecutionEnvironmentStatus | None = None,
        error: str | None = None,
    ):
        source = handle if handle is not None else requirement if requirement is not None else {}
        payload = {
            "requirement_id": source.get("requirement_id", ""),
            "handle_id": source.get("handle_id", ""),
            "kind": source.get("kind", ""),
            "scope": source.get("scope", ""),
            "owner_id": source.get("owner_id", ""),
            "resource_key": source.get("resource_key", ""),
            "status": status or source.get("status", ""),
            "reuse_key": source.get("reuse_key", "") or source.get("meta", {}).get("reuse_key", ""),
        }
        if error:
            payload["error"] = error
        return payload

    async def _emit(
        self,
        event_type: str,
        *,
        requirement: ExecutionEnvironmentRequirement | None = None,
        handle: ExecutionEnvironmentHandle | None = None,
        status: ExecutionEnvironmentStatus | None = None,
        message: str | None = None,
        error: str | None = None,
    ):
        await self.event_center.async_emit(
            {
                "event_type": event_type,
                "source": "ExecutionEnvironmentManager",
                "level": "ERROR" if error else "INFO",
                "message": message,
                "payload": self._event_payload(requirement, handle, status=status, error=error),
            }
        )

    def declare(self, requirement: ExecutionEnvironmentRequirement):
        normalized = self._normalize_requirement(requirement)
        self._requirements[str(normalized.get("requirement_id", ""))] = normalized
        self.event_center.emit(
            {
                "event_type": "execution_environment.declared",
                "source": "ExecutionEnvironmentManager",
                "message": "Execution environment requirement declared.",
                "payload": self._event_payload(normalized, status="declared"),
            }
        )
        return normalized

    def set_decision_handler(self, handler: ExecutionEnvironmentDecisionHandler | None):
        self._decision_handler = handler
        return self

    async def _resolve_approval(
        self,
        requirement: ExecutionEnvironmentRequirement,
        policy: ExecutionEnvironmentPolicy,
    ):
        approval_mode = str(policy.get("approval_mode", "auto"))
        approval_required = bool(requirement.get("approval_required", False)) or approval_mode == "always"
        if not approval_required:
            return policy
        await self._emit(
            "execution_environment.approval_required",
            requirement=requirement,
            status="pending_approval",
            message="Execution environment approval is required.",
        )
        if approval_mode == "never":
            raise ExecutionEnvironmentApprovalDenied(requirement, "Execution environment approval is disabled by policy.")
        if self._decision_handler is None:
            raise ExecutionEnvironmentApprovalRequired(requirement, policy)
        decision = await FunctionShifter.asyncify(self._decision_handler)(requirement, policy)
        if isinstance(decision, bool):
            if decision:
                return policy
            raise ExecutionEnvironmentApprovalDenied(requirement, "Execution environment approval was denied.")
        decision_dict = decision if isinstance(decision, dict) else {}
        if decision_dict.get("approved") is not True:
            raise ExecutionEnvironmentApprovalDenied(requirement, str(decision_dict.get("reason", "")))
        merged_policy = dict(policy)
        override = decision_dict.get("policy_override", {})
        if isinstance(override, dict):
            merged_policy.update(override)
        return cast(ExecutionEnvironmentPolicy, merged_policy)

    async def async_ensure(
        self,
        requirement_or_id: ExecutionEnvironmentRequirement | str,
        *,
        scope: ExecutionEnvironmentScope | None = None,
        owner_id: str | None = None,
    ):
        if isinstance(requirement_or_id, str):
            if requirement_or_id not in self._requirements:
                raise ValueError(f"Can not find execution environment requirement '{ requirement_or_id }'.")
            requirement = self._normalize_requirement(self._requirements[requirement_or_id], scope=scope, owner_id=owner_id)
        else:
            requirement = self._normalize_requirement(requirement_or_id, scope=scope, owner_id=owner_id)
            self._requirements[str(requirement.get("requirement_id", ""))] = requirement
        policy = cast(ExecutionEnvironmentPolicy, dict(requirement.get("policy", {})))
        policy = await self._resolve_approval(requirement, policy)
        reuse_key = str(requirement.get("reuse_key", ""))
        provider = self._get_provider(str(requirement["kind"]))
        existing_id = self._handles_by_reuse_key.get(reuse_key)
        if existing_id and existing_id in self._handles:
            existing_handle = self._handles[existing_id]
            if existing_handle.get("status") == "ready":
                health_error = None
                try:
                    health_status = await provider.async_health_check(existing_handle)
                except Exception as error:
                    health_status = cast(ExecutionEnvironmentStatus, "unhealthy")
                    health_error = str(error)
                if health_status == "ready":
                    existing_handle["ref_count"] = int(existing_handle.get("ref_count", 0)) + 1
                    return existing_handle
                existing_handle["status"] = "unhealthy"
                await self._emit(
                    "execution_environment.unhealthy",
                    handle=existing_handle,
                    status="unhealthy",
                    message="Execution environment health check failed before reuse.",
                    error=health_error,
                )
                await self._async_release_handle(existing_id, force=True)
            else:
                await self._async_release_handle(existing_id, force=True)

        await self._emit(
            "execution_environment.ensuring",
            requirement=requirement,
            status="ensuring",
            message="Execution environment ensuring started.",
        )
        try:
            handle = await provider.async_ensure(
                requirement=requirement,
                policy=policy,
                existing_handle=None,
            )
        except Exception as error:
            await self._emit(
                "execution_environment.failed",
                requirement=requirement,
                status="failed",
                message="Execution environment ensure failed.",
                error=str(error),
            )
            raise
        normalized_handle = cast(ExecutionEnvironmentHandle, dict(handle))
        normalized_handle.setdefault("handle_id", f"{ requirement.get('kind', '') }:{ uuid.uuid4().hex }")
        normalized_handle.setdefault("requirement_id", requirement.get("requirement_id", ""))
        normalized_handle.setdefault("kind", requirement.get("kind", ""))
        normalized_handle.setdefault("scope", requirement.get("scope", "action_call"))
        normalized_handle.setdefault("owner_id", requirement.get("owner_id", ""))
        normalized_handle.setdefault("resource_key", requirement.get("resource_key", ""))
        normalized_handle.setdefault("status", "ready")
        normalized_handle.setdefault("policy", policy)
        normalized_handle.setdefault("ref_count", 1)
        normalized_handle.setdefault("meta", {})
        normalized_handle["meta"] = dict(normalized_handle.get("meta", {}))
        normalized_handle["meta"]["reuse_key"] = reuse_key
        handle_id = str(normalized_handle.get("handle_id", ""))
        self._handles[handle_id] = normalized_handle
        self._handles_by_reuse_key[reuse_key] = handle_id
        await self._emit(
            "execution_environment.ready",
            handle=normalized_handle,
            status="ready",
            message="Execution environment is ready.",
        )
        return normalized_handle

    async def _async_release_handle(self, handle_id: str, *, force: bool = False):
        if not handle_id or handle_id not in self._handles:
            return None
        handle = self._handles[handle_id]
        ref_count = int(handle.get("ref_count", 1))
        if ref_count > 1 and not force:
            handle["ref_count"] = ref_count - 1
            return None
        provider = self._get_provider(str(handle.get("kind", "")))
        handle["status"] = "releasing"
        await self._emit(
            "execution_environment.releasing",
            handle=handle,
            status="releasing",
            message="Execution environment releasing started.",
        )
        try:
            await provider.async_release(handle)
        except Exception as error:
            handle["status"] = "failed"
            await self._emit(
                "execution_environment.failed",
                handle=handle,
                status="failed",
                message="Execution environment release failed.",
                error=str(error),
            )
            return None
        handle["status"] = "released"
        reuse_key = str(handle.get("meta", {}).get("reuse_key", ""))
        if reuse_key and self._handles_by_reuse_key.get(reuse_key) == handle_id:
            del self._handles_by_reuse_key[reuse_key]
        del self._handles[handle_id]
        await self._emit(
            "execution_environment.released",
            handle=handle,
            status="released",
            message="Execution environment released.",
        )
        return None

    async def async_release(self, handle_or_id: ExecutionEnvironmentHandle | str):
        handle_id = handle_or_id if isinstance(handle_or_id, str) else str(handle_or_id.get("handle_id", ""))
        return await self._async_release_handle(handle_id)

    async def async_release_scope(self, scope: ExecutionEnvironmentScope, owner_id: str):
        targets = [
            handle_id
            for handle_id, handle in self._handles.items()
            if handle.get("scope") == scope and handle.get("owner_id") == owner_id
        ]
        for handle_id in targets:
            await self.async_release(handle_id)

    def inspect(self, handle_or_requirement_id: str):
        if handle_or_requirement_id in self._handles:
            return self._handles[handle_or_requirement_id]
        return self._requirements.get(handle_or_requirement_id)

    def list(
        self,
        *,
        scope: ExecutionEnvironmentScope | None = None,
        owner_id: str | None = None,
        status: ExecutionEnvironmentStatus | None = None,
    ):
        handles = list(self._handles.values())
        if scope is not None:
            handles = [handle for handle in handles if handle.get("scope") == scope]
        if owner_id is not None:
            handles = [handle for handle in handles if handle.get("owner_id") == owner_id]
        if status is not None:
            handles = [handle for handle in handles if handle.get("status") == status]
        return handles
