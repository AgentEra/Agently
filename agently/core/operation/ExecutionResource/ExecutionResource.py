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
import json
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import (
    ExecutionResourceHandle,
    ExecutionResourcePolicy,
    ExecutionResourceProviderCandidate,
    ExecutionResourceProviderProbe,
    ExecutionResourceRequirement,
    ExecutionResourceScope,
    ExecutionResourceStatus,
)
from agently.core.operation.PolicyApproval import access_policy_auto_allow, merge_access_control_policy

if TYPE_CHECKING:
    from agently.core.runtime.EventCenter import EventCenter
    from agently.core.extension.PluginManager import PluginManager
    from agently.types.plugins import ExecutionResourceProvider
    from agently.utils import Settings


class ExecutionResourceError(RuntimeError):
    def __init__(self, message: str, *, code: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload if payload is not None else {}


class ExecutionResourceApprovalRequired(ExecutionResourceError):
    def __init__(self, requirement: ExecutionResourceRequirement, policy: ExecutionResourcePolicy):
        super().__init__(
            "Execution environment approval is required.",
            code="execution_resource.approval_required",
            payload={
                "requirement_id": requirement.get("requirement_id", ""),
                "kind": requirement.get("kind", ""),
                "scope": requirement.get("scope", ""),
                "owner_id": requirement.get("owner_id", ""),
                "resource_key": requirement.get("resource_key", ""),
                "policy": dict(policy),
            },
        )


class ExecutionResourceApprovalDenied(ExecutionResourceError):
    def __init__(self, requirement: ExecutionResourceRequirement, reason: str):
        super().__init__(
            reason or "Execution environment approval was denied.",
            code="execution_resource.approval_denied",
            payload={
                "requirement_id": requirement.get("requirement_id", ""),
                "kind": requirement.get("kind", ""),
                "scope": requirement.get("scope", ""),
                "owner_id": requirement.get("owner_id", ""),
                "resource_key": requirement.get("resource_key", ""),
            },
        )


class ExecutionResourceManager:
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
        self._requirements: dict[str, ExecutionResourceRequirement] = {}
        self._handles: dict[str, ExecutionResourceHandle] = {}
        self._handles_by_reuse_key: dict[str, str] = {}
        self._providers: dict[str, dict[str, "ExecutionResourceProvider"]] = {}
        self._providers_by_id: dict[str, "ExecutionResourceProvider"] = {}
        self._plugins_loaded = False

        self.ensure = default_stage_call_bridge.as_sync(self.async_ensure)
        self.release = default_stage_call_bridge.as_sync(self.async_release)
        self.release_scope = default_stage_call_bridge.as_sync(self.async_release_scope)

    @staticmethod
    def _provider_id(provider: "ExecutionResourceProvider") -> str:
        provider_id = str(
            getattr(provider, "provider_id", "")
            or getattr(provider, "name", "")
            or getattr(provider, "kind", "")
        ).strip()
        if not provider_id:
            raise ValueError("ExecutionResourceProvider.provider_id is required.")
        return provider_id

    @staticmethod
    def _supported_kinds(provider: "ExecutionResourceProvider") -> tuple[str, ...]:
        raw_kinds = getattr(provider, "supported_kinds", None)
        if raw_kinds is None:
            legacy_kind = str(getattr(provider, "kind", "")).strip()
            raw_kinds = (legacy_kind,) if legacy_kind else ()
        if isinstance(raw_kinds, str):
            raw_kinds = (raw_kinds,)
        kinds = tuple(dict.fromkeys(str(item).strip() for item in raw_kinds if str(item).strip()))
        if not kinds:
            raise ValueError("ExecutionResourceProvider.supported_kinds is required.")
        return kinds

    def register_provider(self, provider: "ExecutionResourceProvider"):
        provider_id = self._provider_id(provider)
        existing = self._providers_by_id.get(provider_id)
        if existing is not None and existing is not provider:
            raise ValueError(f"duplicate ExecutionResourceProvider.provider_id: {provider_id!r}")
        if existing is provider:
            return self
        self._providers_by_id[provider_id] = provider
        for kind in self._supported_kinds(provider):
            self._providers.setdefault(kind, {})[provider_id] = provider
        return self

    def _load_plugin_providers(self) -> None:
        if self._plugins_loaded:
            return
        self._plugins_loaded = True
        try:
            plugin_names = self.plugin_manager.get_plugin_list("ExecutionResourceProvider")
        except Exception:
            plugin_names = []
        for plugin_name in plugin_names:
            plugin_class = cast(Any, self.plugin_manager.get_plugin("ExecutionResourceProvider", plugin_name))
            provider = plugin_class()
            self.register_provider(provider)

    def _get_provider(self, kind: str, *, provider_id: str | None = None):
        if provider_id and provider_id in self._providers_by_id:
            provider = self._providers_by_id[provider_id]
            if kind in self._supported_kinds(provider):
                return provider
        if kind in self._providers and self._providers[kind]:
            return next(iter(self._providers[kind].values()))
        self._load_plugin_providers()
        if provider_id and provider_id in self._providers_by_id:
            provider = self._providers_by_id[provider_id]
            if kind in self._supported_kinds(provider):
                return provider
        if kind in self._providers and self._providers[kind]:
            return next(iter(self._providers[kind].values()))
        raise ExecutionResourceError(
            f"Can not find ExecutionResourceProvider for kind '{ kind }'.",
            code="execution_resource.provider_missing",
            payload={"kind": kind, "provider_id": provider_id or ""},
        )

    @staticmethod
    def _capability_matches(key: str, expected: Any, capabilities: dict[str, Any]) -> bool:
        aliases = {
            "language": "languages",
            "workspace_access_mode": "workspace_access_modes",
            "toolchain": "toolchains",
        }
        actual = capabilities.get(key)
        if actual is None and key in aliases:
            actual = capabilities.get(aliases[key])
        if actual is None:
            return False
        if key == "toolchains":
            return ExecutionResourceManager._toolchains_match(expected, actual)
        if key == "isolation" and isinstance(expected, str) and isinstance(actual, str):
            levels = {"none": 0, "preferred": 1, "required": 2}
            if expected in levels and actual in levels:
                return levels[actual] >= levels[expected]
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                ExecutionResourceManager._capability_matches(
                    nested_key,
                    nested_expected,
                    actual,
                )
                for nested_key, nested_expected in expected.items()
            )
        if isinstance(actual, (list, tuple, set)):
            if isinstance(expected, (list, tuple, set)):
                return set(expected).issubset(set(actual))
            return expected in actual
        if isinstance(expected, (list, tuple, set)):
            return actual in expected
        return actual == expected

    @staticmethod
    def _version_parts(value: Any) -> tuple[int, ...] | None:
        from agently.types.data.code_execution import extract_code_toolchain_version

        version = extract_code_toolchain_version(str(value or ""))
        if not version:
            return None
        return tuple(int(part) for part in version.split("."))

    @classmethod
    def _toolchains_match(cls, expected: Any, actual: Any) -> bool:
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            return False
        for tool, raw_constraint in expected.items():
            fact = actual.get(str(tool))
            if not isinstance(fact, dict):
                return False
            if fact.get("available", True) is not True:
                return False
            constraint = raw_constraint if isinstance(raw_constraint, dict) else {}
            actual_parts = cls._version_parts(fact.get("version") or fact.get("raw_version"))
            minimum = constraint.get("minimum_version")
            if minimum is not None:
                minimum_parts = cls._version_parts(minimum)
                if actual_parts is None or minimum_parts is None:
                    return False
                width = max(len(actual_parts), len(minimum_parts))
                if actual_parts + (0,) * (width - len(actual_parts)) < minimum_parts + (0,) * (width - len(minimum_parts)):
                    return False
            exact = constraint.get("exact_version")
            if exact is not None:
                exact_parts = cls._version_parts(exact)
                if actual_parts is None or exact_parts is None:
                    return False
                width = max(len(actual_parts), len(exact_parts))
                if actual_parts + (0,) * (width - len(actual_parts)) != exact_parts + (0,) * (width - len(exact_parts)):
                    return False
        return True

    @classmethod
    def _probe_is_eligible(
        cls,
        probe: ExecutionResourceProviderProbe,
        *,
        kind: str,
        required_capabilities: dict[str, Any],
    ) -> bool:
        if not probe.get("available", False):
            return False
        if kind not in probe.get("supported_kinds", []):
            return False
        capabilities = dict(probe.get("capabilities", {}))
        return all(
            cls._capability_matches(key, expected, capabilities)
            for key, expected in required_capabilities.items()
        )

    async def _probe_provider(
        self,
        provider: "ExecutionResourceProvider",
        *,
        requirement: ExecutionResourceRequirement,
        policy: ExecutionResourcePolicy,
    ) -> ExecutionResourceProviderProbe:
        provider_id = self._provider_id(provider)
        supported_kinds = list(self._supported_kinds(provider))
        probe_method = getattr(provider, "async_probe", None)
        if probe_method is None:
            return {
                "provider_id": provider_id,
                "available": True,
                "supported_kinds": supported_kinds,
                "capabilities": dict(getattr(provider, "capabilities", {}) or {}),
                "reason": "legacy provider has no explicit probe",
            }
        try:
            raw_probe = await probe_method(requirement=requirement, policy=policy)
            probe = dict(raw_probe or {})
        except Exception as error:
            return {
                "provider_id": provider_id,
                "available": False,
                "supported_kinds": supported_kinds,
                "capabilities": {},
                "reason": f"probe failed: {str(error)[:500]}",
            }
        return {
            "provider_id": provider_id,
            "available": bool(probe.get("available", False)),
            "supported_kinds": supported_kinds,
            "capabilities": dict(probe.get("capabilities", {}) or {}),
            "reason": str(probe.get("reason", ""))[:500],
            "diagnostics": list(probe.get("diagnostics", []) or [])[:20],
            "meta": dict(probe.get("meta", {}) or {}),
        }

    async def _select_provider(
        self,
        *,
        requirement: ExecutionResourceRequirement,
        policy: ExecutionResourcePolicy,
    ) -> tuple[
        "ExecutionResourceProvider",
        list[ExecutionResourceProviderProbe],
        ExecutionResourceRequirement,
        dict[str, Any],
    ]:
        kind = str(requirement.get("kind", ""))
        explicit_id = str(requirement.get("provider_id", "")).strip()
        configured = cast(
            list[ExecutionResourceProviderCandidate],
            list(requirement.get("provider_candidates", [])),
        )
        if not self._providers.get(kind) or explicit_id or configured:
            self._load_plugin_providers()
        if explicit_id:
            candidates = [
                cast(
                    ExecutionResourceProviderCandidate,
                    {"provider_id": explicit_id, "config": {}},
                )
            ]
        elif configured:
            candidates = configured
        else:
            candidates = [
                cast(
                    ExecutionResourceProviderCandidate,
                    {"provider_id": provider_id, "config": {}},
                )
                for provider_id in self._providers.get(kind, {})
            ]
        if not candidates:
            raise ExecutionResourceError(
                f"Can not find ExecutionResourceProvider for kind '{kind}'.",
                code="execution_resource.provider_missing",
                payload={"kind": kind},
            )

        required_capabilities = dict(requirement.get("required_capabilities", {}) or {})
        preferred_capabilities = dict(
            requirement.get("preferred_capabilities", {}) or {}
        )
        probes: list[ExecutionResourceProviderProbe] = []
        eligible: list[
            tuple[
                int,
                str,
                "ExecutionResourceProvider",
                ExecutionResourceRequirement,
                bool,
            ]
        ] = []
        for candidate_index, candidate in enumerate(candidates):
            provider_id = str(candidate.get("provider_id", ""))
            candidate_requirement = cast(ExecutionResourceRequirement, dict(requirement))
            candidate_requirement["provider_id"] = provider_id
            candidate_requirement["config"] = {
                **dict(requirement.get("config", {})),
                **dict(candidate.get("config", {})),
            }
            provider = self._providers_by_id.get(provider_id)
            if provider is None or kind not in self._supported_kinds(provider):
                probes.append(
                    {
                        "provider_id": provider_id,
                        "available": False,
                        "supported_kinds": [],
                        "capabilities": {},
                        "reason": "provider is not registered for the requested kind",
                        "meta": {"candidate_index": candidate_index},
                    }
                )
                continue
            probe = await self._probe_provider(
                provider,
                requirement=candidate_requirement,
                policy=policy,
            )
            probe["meta"] = {
                **dict(probe.get("meta", {})),
                "candidate_index": candidate_index,
            }
            probes.append(probe)
            if not self._probe_is_eligible(
                probe,
                kind=kind,
                required_capabilities=required_capabilities,
            ):
                continue
            preference_satisfied = not preferred_capabilities or self._probe_is_eligible(
                probe,
                kind=kind,
                required_capabilities=preferred_capabilities,
            )
            eligible.append(
                (
                    candidate_index,
                    provider_id,
                    provider,
                    candidate_requirement,
                    preference_satisfied,
                )
            )

        if not eligible:
            raise ExecutionResourceError(
                f"No eligible ExecutionResourceProvider is available for kind '{kind}'.",
                code="execution_resource.provider_unavailable",
                payload={
                    "kind": kind,
                    "required_capabilities": required_capabilities,
                    "preferred_capabilities": preferred_capabilities,
                    "provider_probes": probes,
                },
            )

        preferred_eligible = [item for item in eligible if item[4]]
        selection_order = (
            [*preferred_eligible, *[item for item in eligible if not item[4]]]
            if preferred_capabilities
            else eligible
        )
        for (
            candidate_index,
            provider_id,
            provider,
            candidate_requirement,
            initially_preferred,
        ) in selection_order:
            ensure_probe = await self._probe_provider(
                provider,
                requirement=candidate_requirement,
                policy=policy,
            )
            for recorded_probe in probes:
                if (
                    str(recorded_probe.get("provider_id", "")) == provider_id
                    and int(dict(recorded_probe.get("meta", {})).get("candidate_index", -1))
                    == candidate_index
                ):
                    recorded_probe["meta"] = {
                        **dict(recorded_probe.get("meta", {})),
                        "ensure_probe_available": bool(
                            ensure_probe.get("available", False)
                        ),
                        "ensure_probe_reason": str(
                            ensure_probe.get("reason", "")
                        ),
                    }
                    break
            ensure_preference_satisfied = (
                not preferred_capabilities
                or self._probe_is_eligible(
                    ensure_probe,
                    kind=kind,
                    required_capabilities=preferred_capabilities,
                )
            )
            if initially_preferred and not ensure_preference_satisfied:
                continue
            if not self._probe_is_eligible(
                ensure_probe,
                kind=kind,
                required_capabilities=required_capabilities,
            ):
                continue
            selected: dict[str, Any] = {
                "index": candidate_index,
                "provider_id": provider_id,
            }
            if preferred_capabilities:
                selected.update(
                    preferred_capabilities_satisfied=(
                        initially_preferred and ensure_preference_satisfied
                    ),
                    preference_fallback=not (
                        initially_preferred and ensure_preference_satisfied
                    ),
                )
            return (
                provider,
                probes,
                candidate_requirement,
                selected,
            )
        raise ExecutionResourceError(
            f"No eligible ExecutionResourceProvider is available for kind '{kind}'.",
            code="execution_resource.provider_unavailable",
            payload={
                "kind": kind,
                "required_capabilities": required_capabilities,
                "preferred_capabilities": preferred_capabilities,
                "provider_probes": probes,
            },
        )

    @staticmethod
    def _stable_json(value: Any):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)

    def _derive_reuse_key(self, requirement: ExecutionResourceRequirement):
        if requirement.get("reuse_key"):
            return str(requirement.get("reuse_key", ""))
        stable_parts = {
            "kind": requirement.get("kind", ""),
            "scope": requirement.get("scope", ""),
            "owner_id": requirement.get("owner_id", ""),
            "resource_key": requirement.get("resource_key", ""),
            "config": requirement.get("config", {}),
            "provider_id": requirement.get("provider_id", ""),
            "provider_candidates": requirement.get("provider_candidates", []),
            "required_capabilities": requirement.get("required_capabilities", {}),
            "preferred_capabilities": requirement.get("preferred_capabilities", {}),
            "task_workspace_access_grant_id": getattr(
                requirement.get("task_workspace_access_grant"),
                "grant_id",
                "",
            ),
        }
        return self._stable_json(stable_parts)

    def _normalize_requirement(
        self,
        requirement: ExecutionResourceRequirement,
        *,
        scope: ExecutionResourceScope | None = None,
        owner_id: str | None = None,
    ):
        normalized = cast(ExecutionResourceRequirement, dict(requirement))
        kind = str(normalized.get("kind", "")).strip()
        if not kind:
            raise ValueError("ExecutionResourceRequirement.kind is required.")
        normalized["kind"] = kind
        normalized["scope"] = cast(ExecutionResourceScope, scope or normalized.get("scope", "action_call"))
        normalized["owner_id"] = str(owner_id or normalized.get("owner_id", "Agently"))
        normalized["resource_key"] = str(normalized.get("resource_key", kind))
        normalized["config"] = dict(normalized.get("config", {}))
        provider_candidates: list[ExecutionResourceProviderCandidate] = []
        for index, item in enumerate(normalized.get("provider_candidates", [])):
            if isinstance(item, str):
                provider_id = item.strip()
                config: dict[str, Any] = {}
            elif isinstance(item, dict):
                provider_id = str(item.get("provider_id", "")).strip()
                raw_config = item.get("config", {})
                if not isinstance(raw_config, dict):
                    raise TypeError(
                        f"ExecutionResourceRequirement.provider_candidates[{index}].config must be a mapping."
                    )
                config = dict(raw_config)
            else:
                raise TypeError(
                    f"ExecutionResourceRequirement.provider_candidates[{index}] must be a provider id or descriptor."
                )
            if not provider_id:
                raise ValueError(
                    f"ExecutionResourceRequirement.provider_candidates[{index}].provider_id is required."
                )
            provider_candidates.append(
                cast(
                    ExecutionResourceProviderCandidate,
                    {"provider_id": provider_id, "config": config},
                )
            )
        normalized["provider_candidates"] = cast(
            list[str | ExecutionResourceProviderCandidate],
            provider_candidates,
        )
        normalized["required_capabilities"] = dict(
            normalized.get("required_capabilities", {})
        )
        normalized["preferred_capabilities"] = dict(
            normalized.get("preferred_capabilities", {})
        )
        if normalized["kind"] == "code_execution":
            for capability_group in (
                "required_capabilities",
                "preferred_capabilities",
            ):
                isolation = normalized[capability_group].get("isolation")
                if isolation is not None and not isinstance(isolation, dict):
                    raise ValueError(
                        "code_execution isolation capability must be a mapping of "
                        "verifiable isolation axes, not a policy label"
                    )
        normalized["policy"] = cast(ExecutionResourcePolicy, dict(normalized.get("policy", {})))
        normalized["meta"] = dict(normalized.get("meta", {}))
        if not normalized.get("requirement_id"):
            normalized["requirement_id"] = f"{ normalized['kind'] }:{ uuid.uuid4().hex }"
        normalized["reuse_key"] = self._derive_reuse_key(normalized)
        return normalized

    @staticmethod
    def _event_payload(
        requirement: ExecutionResourceRequirement | None = None,
        handle: ExecutionResourceHandle | None = None,
        *,
        status: ExecutionResourceStatus | None = None,
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
            "provider_id": source.get("provider_id", ""),
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
        requirement: ExecutionResourceRequirement | None = None,
        handle: ExecutionResourceHandle | None = None,
        status: ExecutionResourceStatus | None = None,
        message: str | None = None,
        error: str | None = None,
    ):
        await self.event_center.async_emit(
            {
                "event_type": event_type,
                "source": "ExecutionResourceManager",
                "level": "ERROR" if error else "INFO",
                "message": message,
                "payload": self._event_payload(requirement, handle, status=status, error=error),
            }
        )

    def declare(self, requirement: ExecutionResourceRequirement):
        normalized = self._normalize_requirement(requirement)
        self._requirements[str(normalized.get("requirement_id", ""))] = normalized
        self.event_center.emit(
            {
                "event_type": "execution_resource.declared",
                "source": "ExecutionResourceManager",
                "message": "Execution environment requirement declared.",
                "payload": self._event_payload(normalized, status="declared"),
            }
        )
        return normalized

    async def _resolve_approval(
        self,
        requirement: ExecutionResourceRequirement,
        policy: ExecutionResourcePolicy,
    ):
        approval_mode = str(policy.get("approval_mode", "auto"))
        approval_required = bool(requirement.get("approval_required", False)) or approval_mode == "always"
        if not approval_required:
            return policy
        if access_policy_auto_allow(policy):
            return policy
        await self._emit(
            "execution_resource.approval_required",
            requirement=requirement,
            status="pending_approval",
            message="Execution environment approval is required.",
        )
        if approval_mode == "never":
            raise ExecutionResourceApprovalDenied(requirement, "Execution environment approval is disabled by policy.")
        from agently.base import policy_approval

        decision = await policy_approval.async_resolve(
            {
                "source": "execution_resource",
                "capability": str(requirement.get("kind", "")),
                "subject": str(requirement.get("resource_key") or requirement.get("kind") or ""),
                "risk": "resource",
                "payload": {
                    "requirement": dict(requirement),
                },
                "policy": dict(policy),
                "lineage": {
                    "owner_id": str(requirement.get("owner_id", "")),
                    "scope": str(requirement.get("scope", "")),
                },
                "meta": {
                    "requirement_id": str(requirement.get("requirement_id", "")),
                },
            },
            handler=str(policy.get("policy_approval_handler", "") or "") or None,
        )
        status = str(decision.get("status", "pending"))
        if status == "pending":
            raise ExecutionResourceApprovalRequired(requirement, policy)
        if status != "approved":
            raise ExecutionResourceApprovalDenied(requirement, str(decision.get("reason", "")))
        merged_policy = dict(policy)
        override = decision.get("policy_override", {})
        if isinstance(override, dict):
            merged_policy.update(override)
        return cast(ExecutionResourcePolicy, merged_policy)

    async def async_ensure(
        self,
        requirement_or_id: ExecutionResourceRequirement | str,
        *,
        scope: ExecutionResourceScope | None = None,
        owner_id: str | None = None,
    ):
        if isinstance(requirement_or_id, str):
            if requirement_or_id not in self._requirements:
                raise ValueError(f"Can not find execution environment requirement '{ requirement_or_id }'.")
            requirement = self._normalize_requirement(self._requirements[requirement_or_id], scope=scope, owner_id=owner_id)
        else:
            requirement = self._normalize_requirement(requirement_or_id, scope=scope, owner_id=owner_id)
            self._requirements[str(requirement.get("requirement_id", ""))] = requirement
        policy = cast(ExecutionResourcePolicy, merge_access_control_policy(requirement.get("policy", {}), self.settings))
        policy = await self._resolve_approval(requirement, policy)
        provider, provider_probes, selected_requirement, selected_candidate = await self._select_provider(
            requirement=requirement,
            policy=policy,
        )
        provider_id = self._provider_id(provider)
        reuse_key = self._stable_json(
            {
                "requirement": str(requirement.get("reuse_key", "")),
                "provider_id": provider_id,
            }
        )
        existing_id = self._handles_by_reuse_key.get(reuse_key)
        if existing_id and existing_id in self._handles:
            existing_handle = self._handles[existing_id]
            if existing_handle.get("status") == "ready":
                health_error = None
                try:
                    health_status = await provider.async_health_check(existing_handle)
                except Exception as error:
                    health_status = cast(ExecutionResourceStatus, "unhealthy")
                    health_error = str(error)
                if health_status == "ready":
                    existing_handle["ref_count"] = int(existing_handle.get("ref_count", 0)) + 1
                    return existing_handle
                existing_handle["status"] = "unhealthy"
                await self._emit(
                    "execution_resource.unhealthy",
                    handle=existing_handle,
                    status="unhealthy",
                    message="Execution environment health check failed before reuse.",
                    error=health_error,
                )
                await self._async_release_handle(existing_id, force=True)
            else:
                await self._async_release_handle(existing_id, force=True)

        await self._emit(
            "execution_resource.ensuring",
            requirement=requirement,
            status="ensuring",
            message="Execution environment ensuring started.",
        )
        try:
            handle = await provider.async_ensure(
                requirement=selected_requirement,
                policy=policy,
                existing_handle=None,
            )
        except Exception as error:
            await self._emit(
                "execution_resource.failed",
                requirement=requirement,
                status="failed",
                message="Execution environment ensure failed.",
                error=str(error),
            )
            raise
        normalized_handle = cast(ExecutionResourceHandle, dict(handle))
        normalized_handle.setdefault("handle_id", f"{ requirement.get('kind', '') }:{ uuid.uuid4().hex }")
        normalized_handle.setdefault("requirement_id", requirement.get("requirement_id", ""))
        normalized_handle.setdefault("kind", requirement.get("kind", ""))
        normalized_handle.setdefault("scope", requirement.get("scope", "action_call"))
        normalized_handle.setdefault("owner_id", requirement.get("owner_id", ""))
        normalized_handle.setdefault("resource_key", requirement.get("resource_key", ""))
        normalized_handle.setdefault("action_call_id", requirement.get("action_call_id", ""))
        normalized_handle["provider_id"] = provider_id
        normalized_handle.setdefault("status", "ready")
        normalized_handle.setdefault("policy", policy)
        normalized_handle.setdefault("ref_count", 1)
        normalized_handle.setdefault("meta", {})
        normalized_handle["meta"] = dict(normalized_handle.get("meta", {}))
        normalized_handle["meta"]["reuse_key"] = reuse_key
        normalized_handle["meta"]["provider_probes"] = provider_probes
        normalized_handle["meta"]["selected_provider_candidate"] = selected_candidate
        handle_id = str(normalized_handle.get("handle_id", ""))
        self._handles[handle_id] = normalized_handle
        self._handles_by_reuse_key[reuse_key] = handle_id
        await self._emit(
            "execution_resource.ready",
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
        provider = self._get_provider(
            str(handle.get("kind", "")),
            provider_id=str(handle.get("provider_id", "")) or None,
        )
        handle["status"] = "releasing"
        await self._emit(
            "execution_resource.releasing",
            handle=handle,
            status="releasing",
            message="Execution environment releasing started.",
        )
        try:
            await provider.async_release(handle)
        except Exception as error:
            handle["status"] = "failed"
            handle.setdefault("meta", {})
            handle["meta"] = dict(handle.get("meta", {}))
            handle["meta"]["cleanup_error"] = str(error)
            await self._emit(
                "execution_resource.failed",
                handle=handle,
                status="failed",
                message="Execution environment release failed.",
                error=str(error),
            )
            raise ExecutionResourceError(
                "Execution environment release failed; the resource remains quarantined.",
                code="execution_resource.release_failed",
                payload={
                    "handle_id": handle_id,
                    "provider_id": str(handle.get("provider_id", "")),
                    "cleanup_error": str(error),
                },
            ) from error
        handle["status"] = "released"
        reuse_key = str(handle.get("meta", {}).get("reuse_key", ""))
        if reuse_key and self._handles_by_reuse_key.get(reuse_key) == handle_id:
            del self._handles_by_reuse_key[reuse_key]
        del self._handles[handle_id]
        await self._emit(
            "execution_resource.released",
            handle=handle,
            status="released",
            message="Execution environment released.",
        )
        return None

    async def async_release(self, handle_or_id: ExecutionResourceHandle | str):
        handle_id = handle_or_id if isinstance(handle_or_id, str) else str(handle_or_id.get("handle_id", ""))
        return await self._async_release_handle(handle_id)

    async def async_release_scope(self, scope: ExecutionResourceScope, owner_id: str):
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
        scope: ExecutionResourceScope | None = None,
        owner_id: str | None = None,
        status: ExecutionResourceStatus | None = None,
    ):
        handles = list(self._handles.values())
        if scope is not None:
            handles = [handle for handle in handles if handle.get("scope") == scope]
        if owner_id is not None:
            handles = [handle for handle in handles if handle.get("owner_id") == owner_id]
        if status is not None:
            handles = [handle for handle in handles if handle.get("status") == status]
        return handles
