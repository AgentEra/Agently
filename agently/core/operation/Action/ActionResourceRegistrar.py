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

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from agently.types.data import (
    ActionPolicy,
    ExecutionResourcePolicy,
    ExecutionResourceProviderCandidate,
    ExecutionResourceRequirement,
)
from agently.types.data.code_execution import required_code_execution_isolation
from agently.utils import DataFormatter, LazyImport
from agently.utils.MCP import normalize_mcp_transport

if TYPE_CHECKING:
    from agently.types.data import MCPConfigs
    from .Action import Action


class ActionResourceRegistrar:
    def __init__(self, action: "Action"):
        self._action = action

    @staticmethod
    def _normalize_code_sandbox(value: Literal["auto", "docker", "gvisor", "seatbelt", "trusted_local"] | str) -> Literal["auto", "docker", "gvisor", "seatbelt", "trusted_local"]:
        normalized = str(value or "trusted_local").strip().lower().replace("-", "_")
        if normalized in {"local", "python", "node", "bash"}:
            normalized = "trusted_local"
        if normalized in {"gvisor", "runsc", "gvisor/runsc"}:
            normalized = "gvisor"
        if normalized not in {"auto", "docker", "gvisor", "seatbelt", "trusted_local"}:
            raise ValueError("sandbox must be one of: 'auto', 'docker', 'gvisor', 'seatbelt', 'trusted_local'.")
        return cast(Literal["auto", "docker", "gvisor", "seatbelt", "trusted_local"], normalized)

    @staticmethod
    def _normalize_dependency_policy(value: Literal["deny", "request", "install"] | dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(value, dict):
            normalized = dict(value)
            mode = str(normalized.get("mode", "deny")).strip().lower() or "deny"
        else:
            mode = str(value or "deny").strip().lower() or "deny"
            normalized = {}
        if mode not in {"deny", "request", "install"}:
            raise ValueError("dependency_policy must be one of: 'deny', 'request', 'install'.")
        normalized["mode"] = mode
        return normalized

    @staticmethod
    def _normalize_provisioning_profile(value: Literal["strict", "developer", "ci"] | str) -> Literal["strict", "developer", "ci"]:
        normalized = str(value or "strict").strip().lower().replace("-", "_")
        if normalized not in {"strict", "developer", "ci"}:
            raise ValueError("provisioning_profile must be one of: 'strict', 'developer', 'ci'.")
        return cast(Literal["strict", "developer", "ci"], normalized)

    @staticmethod
    def _normalize_image_pull_policy(
        value: Literal["never", "request", "if_missing", "always"] | str | None,
        *,
        provisioning_profile: Literal["strict", "developer", "ci"],
    ) -> Literal["never", "request", "if_missing", "always"]:
        if value is None or str(value).strip() == "":
            return "if_missing" if provisioning_profile in {"developer", "ci"} else "never"
        normalized = str(value).strip().lower().replace("-", "_")
        if normalized not in {"never", "request", "if_missing", "always"}:
            raise ValueError("image_pull_policy must be one of: 'never', 'request', 'if_missing', 'always'.")
        return cast(Literal["never", "request", "if_missing", "always"], normalized)

    @staticmethod
    def _docker_policy(
        default_policy: "ActionPolicy | None",
        *,
        timeout: int,
    ) -> "ExecutionResourcePolicy":
        policy = cast(ExecutionResourcePolicy, dict(default_policy or {}))
        policy.setdefault("network_mode", "disabled")
        policy.setdefault("timeout_seconds", timeout)
        return policy

    def _docker_runtime_requirement(
        self,
        *,
        action_id: str,
        language: str,
        image: str,
        timeout: int,
        default_policy: "ActionPolicy | None",
        docker_binary: str,
        docker_default_args: list[str] | None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
        runtime_profile: dict[str, Any] | None = None,
        provider_id: str = "docker",
    ) -> "ExecutionResourceRequirement":
        normalized_provisioning_profile = self._normalize_provisioning_profile(provisioning_profile)
        normalized_dependency_policy = (
            self._normalize_dependency_policy(dependency_policy)
            if dependency_policy is not None
            else {"mode": "install"} if normalized_provisioning_profile in {"developer", "ci"} else {"mode": "deny"}
        )
        normalized_image_pull_policy = self._normalize_image_pull_policy(
            image_pull_policy,
            provisioning_profile=normalized_provisioning_profile,
        )
        profile = dict(runtime_profile or {})
        profile.update(
            {
                "language": language,
                "image": image,
                "provisioning_profile": normalized_provisioning_profile,
                "image_pull_policy": normalized_image_pull_policy,
                "network_mode": (
                    profile.get("network_mode")
                    or (
                        "bridge"
                        if normalized_provisioning_profile in {"developer", "ci"}
                        and normalized_dependency_policy.get("mode") == "install"
                        else "disabled"
                    )
                ),
                "dependency_policy": normalized_dependency_policy,
            }
        )
        requirement = cast(ExecutionResourceRequirement, {
            "requirement_id": f"docker:{ action_id }",
            "kind": "docker",
            "scope": "action_call",
            "resource_key": action_id,
            "config": {
                "docker_binary": docker_binary,
                "timeout": timeout,
                "default_args": docker_default_args or [],
                "runtime_profile": profile,
            },
            "provider_id": provider_id,
            "policy": self._docker_policy(default_policy, timeout=timeout),
        })
        if normalized_dependency_policy.get("mode") == "request" or normalized_image_pull_policy == "request":
            requirement["approval_required"] = True
        return requirement

    def _normalize_code_execution_providers(
        self,
        providers: Sequence[str | Mapping[str, Any]] | None,
    ) -> list[ExecutionResourceProviderCandidate]:
        configured = (
            providers
            if providers is not None
            else self._action.settings.get("code_execution.providers", None)
        )
        if configured is None:
            configured = ["docker"]
        if (
            not isinstance(configured, Sequence)
            or isinstance(configured, str | bytes | bytearray)
            or not configured
        ):
            raise ValueError("code_execution providers must be a non-empty ordered sequence.")
        normalized: list[ExecutionResourceProviderCandidate] = []
        for index, item in enumerate(configured):
            if isinstance(item, str):
                provider_id = item.strip()
                config: dict[str, Any] = {}
            elif isinstance(item, Mapping):
                provider_id = str(item.get("provider_id", "")).strip()
                raw_config = item.get("config", {})
                if not isinstance(raw_config, Mapping):
                    raise TypeError(f"providers[{index}].config must be a mapping.")
                config = dict(raw_config)
            else:
                raise TypeError(f"providers[{index}] must be a provider id or descriptor.")
            if not provider_id:
                raise ValueError(f"providers[{index}].provider_id is required.")
            normalized.append(
                cast(
                    ExecutionResourceProviderCandidate,
                    {"provider_id": provider_id, "config": config},
                )
            )
        return normalized

    @staticmethod
    def _format_bash_sandbox_desc(
        desc: str,
        *,
        allowed_cmd_prefixes: list[str] | None,
        allowed_workdir_roots: list[str | Path] | None,
        timeout: int,
        max_output_chars: int | None = None,
    ) -> str:
        command_text = (
            ", ".join(str(prefix) for prefix in allowed_cmd_prefixes)
            if allowed_cmd_prefixes
            else "executor default safe profile"
        )
        roots_text = (
            ", ".join(str(Path(root)) for root in allowed_workdir_roots)
            if allowed_workdir_roots
            else "no TaskWorkspace root configured"
        )
        policy_desc = (
            "Pass exactly one command: use a string, an argv token list, or a one-item list "
            "containing the complete command; Agently parses it to argv and never invokes a shell. "
            f"Allowed command prefixes: {command_text}. "
            f"Allowed working directory roots: {roots_text}. "
            f"Timeout: {timeout} seconds."
        )
        if max_output_chars is not None:
            policy_desc += f" Output preview limit: {max_output_chars} characters per stream."
        base_desc = str(desc).strip()
        return f"{base_desc}\n\n{policy_desc}" if base_desc else policy_desc

    async def async_use_action_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        side_effect_level: Literal["read", "write", "exec"] = "read",
        approval_required: bool = False,
        sandbox_required: bool = False,
        replay_safe: bool = True,
        expose_to_model: bool = True,
    ):
        action = self._action
        LazyImport.import_package("fastmcp", version_constraint=">=3", auto_install=False)
        from fastmcp import Client  # pyright: ignore[reportMissingImports]

        transport = normalize_mcp_transport(transport, headers=headers)
        normalized_tags = action._normalize_tags(tags)

        action_ids: list[str] = []
        registration_snapshot: dict[str, dict[str, Any] | None] = {}
        try:
            async with Client(transport) as client:  # type: ignore[arg-type]
                tool_list = await client.list_tools()
                action_ids = [str(tool.name) for tool in tool_list]
                registration_snapshot = action._snapshot_action_registration_batch(action_ids)
                for tool in tool_list:
                    tool_tags = []
                    if hasattr(tool, "_meta") and tool._meta:  # type: ignore[attr-defined]
                        tool_tags = tool._meta.get("_fastmcp", {}).get("tags", [])  # type: ignore[index]
                    tool_tags.extend(normalized_tags)
                    action.register_action(
                        action_id=tool.name,
                        desc=tool.description,
                        kwargs=DataFormatter.from_schema_to_kwargs_format(tool.inputSchema),
                        required_input_keys=(
                            list(tool.inputSchema.get("required", []))
                            if isinstance(tool.inputSchema, dict)
                            else []
                        ),
                        returns=DataFormatter.from_schema_to_kwargs_format(tool.outputSchema),
                        executor=action._create_executor(
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
                        execution_resources=cast(
                            list[ExecutionResourceRequirement],
                            [
                                {
                                    "requirement_id": f"mcp:{ tool.name }",
                                    "kind": "mcp",
                                    "scope": "agent",
                                    "resource_key": tool.name,
                                    "config": {"transport": transport},
                                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                                    "approval_required": approval_required,
                                }
                            ],
                        ),
                    )
        except BaseException:
            if action_ids:
                action._rollback_action_registration_batch(action_ids, registration_snapshot)
            raise
        return action

    async def async_use_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
        tags: str | list[str] | None = None,
    ):
        await self.async_use_action_mcp(transport, headers=headers, tags=tags)
        return self._action

    def register_python_sandbox_action(
        self,
        *,
        action_id: str = "python_sandbox",
        desc: str = "Execute Python code through an explicitly trusted local Python execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        preset_objects: dict[str, object] | None = None,
        base_vars: dict[str, Any] | None = None,
        allowed_return_types: list[type] | None = None,
        sandbox: Literal["auto", "docker", "gvisor", "seatbelt", "trusted_local"] = "trusted_local",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
        timeout: int = 60,
    ):
        sandbox_mode = self._normalize_code_sandbox(sandbox)
        if preset_objects is not None or base_vars is not None or allowed_return_types is not None:
            raise ValueError(
                "register_python_sandbox_action() no longer supports in-process preset_objects, "
                "base_vars, or allowed_return_types. Use the Workspace-bound CodeExecution contract."
            )
        providers: list[str] | None = None
        unsafe_fallback = False
        isolation: Literal["required", "preferred", "none"] = "required"
        if sandbox_mode == "trusted_local":
            providers = ["trusted_local"]
            unsafe_fallback = True
            isolation = "none"
        elif sandbox_mode == "docker":
            providers = ["docker"]
        elif sandbox_mode == "gvisor":
            providers = ["gvisor"]
        elif sandbox_mode == "seatbelt":
            providers = ["seatbelt"]
            isolation = "preferred"
        return self.register_code_runtime_action(
            language="python",
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
            expose_to_model=expose_to_model,
            providers=providers,
            unsafe_fallback=unsafe_fallback,
            isolation=isolation,
            docker_image=docker_image,
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
            timeout=timeout,
        )

    def register_bash_sandbox_action(
        self,
        *,
        action_id: str = "bash_sandbox",
        desc: str = "Execute a shell command inside a constrained sandbox.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        allowed_cmd_prefixes: list[str] | None = None,
        allowed_workdir_roots: list[str | Path] | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        max_output_chars: int = 20000,
        output_artifact_dir: str | Path | None = None,
        task_workspace_mounts: list[dict[str, str]] | None = None,
        sandbox: Literal["auto", "docker", "gvisor", "trusted_local"] = "trusted_local",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
    ):
        action = self._action
        sandbox_mode = self._normalize_code_sandbox(sandbox)
        if sandbox_mode == "seatbelt":
            raise ValueError(
                "Seatbelt supports Workspace-bound code_execution helpers, not the broad Bash sandbox action."
            )
        model_desc = self._format_bash_sandbox_desc(
            desc,
            allowed_cmd_prefixes=allowed_cmd_prefixes,
            allowed_workdir_roots=allowed_workdir_roots,
            timeout=timeout,
            max_output_chars=max_output_chars,
        )
        if sandbox_mode == "trusted_local":
            execution_resources = cast(list[ExecutionResourceRequirement], [
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
                        "max_output_chars": max_output_chars,
                        "output_artifact_dir": output_artifact_dir,
                        "task_workspace_mounts": task_workspace_mounts,
                    },
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                }
            ])
        else:
            roots = [str(Path(root).expanduser().resolve()) for root in (allowed_workdir_roots or [])]
            execution_resources = [
                self._docker_runtime_requirement(
                    action_id=action_id,
                    language="shell",
                    image=docker_image,
                    timeout=timeout,
                    default_policy=default_policy,
                    docker_binary=docker_binary,
                    docker_default_args=docker_default_args,
                    dependency_policy=dependency_policy,
                    provisioning_profile=provisioning_profile,
                    image_pull_policy=image_pull_policy,
                    runtime_profile={
                        "allowed_cmd_prefixes": list(allowed_cmd_prefixes or []),
                        "allowed_workdir_roots": roots,
                        "env": env,
                        "max_output_chars": max_output_chars,
                        "output_artifact_dir": str(output_artifact_dir) if output_artifact_dir is not None else None,
                        "task_workspace_mounts": [dict(item) for item in (task_workspace_mounts or [])],
                    },
                    provider_id="gvisor" if sandbox_mode == "gvisor" else "docker",
                )
            ]
        action.register_action(
            action_id=action_id,
            desc=model_desc,
            kwargs={
                "cmd": (
                    "str | list[str]",
                    "Exactly one command: a command string, argv tokens, or a one-item list containing the complete command.",
                ),
                "workdir": ("str | None", "Working directory inside allowed roots."),
            },
            executor=action._create_executor(
                "BashSandboxActionExecutor",
                allowed_cmd_prefixes=allowed_cmd_prefixes,
                allowed_workdir_roots=allowed_workdir_roots,
                timeout=timeout,
                env=env,
                max_output_chars=max_output_chars,
                output_artifact_dir=output_artifact_dir,
            ),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            meta={"host_only_input_keys": ["allow_unsafe"]},
            execution_resources=execution_resources,
        )
        return action

    def register_nodejs_action(
        self,
        *,
        action_id: str = "run_nodejs",
        desc: str = "Execute JavaScript with Node.js inside a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        node_binary: str = "node",
        cwd: str | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        sandbox: Literal["auto", "docker", "gvisor", "seatbelt", "trusted_local"] = "trusted_local",
        docker_image: str = "node:22-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
    ):
        if node_binary != "node" or cwd is not None or env is not None:
            raise ValueError(
                "register_nodejs_action() no longer accepts provider-owned node_binary, cwd, or env "
                "settings. Use source files, arguments, TaskWorkspace access, and provider config."
            )
        sandbox_mode = self._normalize_code_sandbox(sandbox)
        providers: list[str] | None = None
        unsafe_fallback = False
        isolation: Literal["required", "preferred", "none"] = "required"
        if sandbox_mode == "trusted_local":
            providers = ["trusted_local"]
            unsafe_fallback = True
            isolation = "none"
        elif sandbox_mode == "docker":
            providers = ["docker"]
        elif sandbox_mode == "gvisor":
            providers = ["gvisor"]
        elif sandbox_mode == "seatbelt":
            providers = ["seatbelt"]
            isolation = "preferred"
        return self.register_code_runtime_action(
            language="nodejs",
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
            expose_to_model=expose_to_model,
            providers=providers,
            unsafe_fallback=unsafe_fallback,
            isolation=isolation,
            docker_image=docker_image,
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
            timeout=timeout,
        )

    def register_code_runtime_action(
        self,
        *,
        language: str,
        action_id: str | None = None,
        desc: str | None = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        providers: Sequence[str | Mapping[str, Any]] | None = None,
        unsafe_fallback: bool = False,
        isolation: Literal["required", "preferred", "none"] = "required",
        docker_image: str | None = None,
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
        timeout: int = 60,
    ):
        from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter

        action = self._action
        adapter = get_code_runtime_adapter(language)
        canonical_language = adapter.language_id
        resolved_action_id = action_id or f"run_{ canonical_language }_code"
        resolved_desc = desc or (
            f"Run { canonical_language } code through a Workspace-bound execution provider. "
            "Provider selection, dependency preparation and isolation are controlled by host policy; "
            "the action accepts source files and arguments, never raw package-manager or compiler commands."
        )
        if isolation not in {"required", "preferred", "none"}:
            raise ValueError("isolation must be one of: 'required', 'preferred', 'none'.")
        if unsafe_fallback and isolation == "required":
            raise ValueError(
                "unsafe_fallback cannot satisfy isolation='required'; choose isolation='preferred' or 'none' explicitly."
            )
        normalized_provisioning_profile = self._normalize_provisioning_profile(provisioning_profile)
        normalized_dependency_policy = (
            self._normalize_dependency_policy(dependency_policy)
            if dependency_policy is not None
            else {"mode": "install"}
            if normalized_provisioning_profile in {"developer", "ci"}
            else {"mode": "deny"}
        )
        normalized_image_pull_policy = self._normalize_image_pull_policy(
            image_pull_policy,
            provisioning_profile=normalized_provisioning_profile,
        )
        runtime_profile: dict[str, Any] = {
            "language": canonical_language,
            "provisioning_profile": normalized_provisioning_profile,
            "image_pull_policy": normalized_image_pull_policy,
            "network_mode": (
                "bridge"
                if normalized_provisioning_profile in {"developer", "ci"}
                and normalized_dependency_policy.get("mode") == "install"
                else "disabled"
            ),
            "dependency_policy": normalized_dependency_policy,
        }
        if docker_image:
            runtime_profile["image"] = str(docker_image)
        provider_candidates = self._normalize_code_execution_providers(providers)
        has_unsafe_candidate = False
        for candidate in provider_candidates:
            candidate_config = dict(candidate.get("config", {}))
            if candidate["provider_id"] in {"docker", "gvisor"}:
                candidate_runtime_profile = candidate_config.get("runtime_profile", {})
                if not isinstance(candidate_runtime_profile, dict):
                    raise TypeError("Docker candidate runtime_profile must be a mapping.")
                candidate["config"] = {
                    "docker_binary": docker_binary,
                    "timeout": timeout,
                    "default_args": list(docker_default_args or []),
                    **candidate_config,
                    "runtime_profile": {
                        **runtime_profile,
                        **candidate_runtime_profile,
                    },
                }
            elif candidate["provider_id"] == "trusted_local":
                has_unsafe_candidate = True
                if unsafe_fallback:
                    candidate_config["allow_unsafe_local"] = True
                candidate["config"] = candidate_config
        if unsafe_fallback and not has_unsafe_candidate:
            provider_candidates.append(
                cast(
                    ExecutionResourceProviderCandidate,
                    {
                        "provider_id": "trusted_local",
                        "config": {"allow_unsafe_local": True},
                    },
                )
            )
        required_capabilities: dict[str, Any] = {
            "language": canonical_language,
            "toolchains": {
                item.tool: {
                    **(
                        {"minimum_version": item.minimum_version}
                        if item.minimum_version is not None
                        else {}
                    ),
                    **(
                        {"exact_version": item.exact_version}
                        if item.exact_version is not None
                        else {}
                    ),
                    **(
                        {"required": True}
                        if item.minimum_version is None and item.exact_version is None
                        else {}
                    ),
                }
                for item in adapter.toolchain_requirements()
            },
            "workspace_access_mode": "snapshot",
        }
        preferred_capabilities: dict[str, Any] = {}
        if isolation == "required":
            required_capabilities["isolation"] = required_code_execution_isolation()
        elif isolation == "preferred":
            preferred_capabilities["isolation"] = required_code_execution_isolation()
        execution_resources = [
            cast(
                ExecutionResourceRequirement,
                {
                    "requirement_id": f"code_execution:{resolved_action_id}",
                    "kind": "code_execution",
                    "scope": "action_call",
                    "resource_key": resolved_action_id,
                    "provider_candidates": provider_candidates,
                    "required_capabilities": required_capabilities,
                    "preferred_capabilities": preferred_capabilities,
                    "workspace_access": {"mode": "snapshot"},
                    "config": {
                        "dependency_policy": normalized_dependency_policy,
                    },
                    "policy": self._docker_policy(default_policy, timeout=timeout),
                    "approval_required": (
                        normalized_dependency_policy.get("mode") == "request"
                        or normalized_image_pull_policy == "request"
                    ),
                    "meta": {
                        "isolation_preference": isolation,
                        "unsafe_fallback_enabled": unsafe_fallback,
                    },
                },
            )
        ]
        action.register_action(
            action_id=resolved_action_id,
            desc=resolved_desc,
            kwargs={
                "source_code": (str, "Primary source code to run in the configured language runtime."),
                "files": ("dict[str, str] | None", "Optional additional source or dependency manifest files."),
                "entrypoint": (
                    "str | None",
                    "Optional relative entrypoint inside the immutable source bundle.",
                ),
                "args": ("list[str]", "Optional command-line arguments passed to the program."),
                "expected_outputs": (
                    "list[str]",
                    "Optional bounded relative output paths to read back through TaskWorkspace.",
                ),
            },
            executor=action._create_executor(
                "CodeExecutionActionExecutor",
                language=canonical_language,
                timeout=timeout,
            ),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=isolation == "required",
            expose_to_model=expose_to_model,
            execution_resources=execution_resources,
        )
        return action

    def register_docker_action(
        self,
        *,
        action_id: str = "run_docker",
        desc: str = "Run a command in a Docker container through a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        image: str | None = None,
        timeout: int = 60,
        docker_binary: str = "docker",
        default_args: list[str] | None = None,
    ):
        action = self._action
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "image": ("str | None", "Docker image. Defaults to the configured image."),
                "cmd": ("str | list[str]", "Command to run in the container."),
                "workdir": ("str | None", "Container working directory."),
                "env": ("dict[str, str] | None", "Container environment variables."),
            },
            executor=action._create_executor("DockerActionExecutor", image=image, timeout=timeout),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            approval_required=True,
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_resources=cast(list[ExecutionResourceRequirement], [
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
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                    "approval_required": True,
                }
            ]),
        )
        return action

    def register_sqlite_action(
        self,
        *,
        action_id: str = "query_sqlite",
        desc: str = "Query a SQLite database through a managed execution resource.",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        database: str = ":memory:",
        read_only: bool = True,
        uri: bool = False,
    ):
        action = self._action
        merged_policy: ActionPolicy = cast(ActionPolicy, dict(default_policy or {}))
        merged_policy.setdefault("read_only", read_only)
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "query": (str, "SQLite query to execute."),
                "params": ("list | dict | None", "Optional SQLite query parameters."),
            },
            executor=action._create_executor("SQLiteActionExecutor", read_only=read_only),
            tags=tags,
            default_policy=merged_policy,
            side_effect_level="read" if read_only else "write",
            expose_to_model=expose_to_model,
            execution_resources=cast(list[ExecutionResourceRequirement], [
                {
                    "requirement_id": f"sqlite:{ action_id }",
                    "kind": "sqlite",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "database": database,
                        "uri": uri,
                    },
                    "policy": cast(ExecutionResourcePolicy, merged_policy),
                }
            ]),
        )
        return action
