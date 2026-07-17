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

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from agently.types.data import (
    ActionPolicy,
    ExecutionResourcePolicy,
    ExecutionResourceRequirement,
)
from agently.utils import DataFormatter, LazyImport
from agently.utils.MCP import normalize_mcp_transport

if TYPE_CHECKING:
    from agently.types.data import MCPConfigs
    from .Action import Action


class ActionResourceRegistrar:
    def __init__(self, action: "Action"):
        self._action = action

    @staticmethod
    def _normalize_code_sandbox(value: Literal["auto", "docker", "seatbelt", "trusted_local"] | str) -> Literal["auto", "docker", "seatbelt", "trusted_local"]:
        normalized = str(value or "trusted_local").strip().lower().replace("-", "_")
        if normalized in {"local", "python", "node", "bash"}:
            normalized = "trusted_local"
        if normalized not in {"auto", "docker", "seatbelt", "trusted_local"}:
            raise ValueError("sandbox must be one of: 'auto', 'docker', 'seatbelt', 'trusted_local'.")
        return cast(Literal["auto", "docker", "seatbelt", "trusted_local"], normalized)

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
            "policy": self._docker_policy(default_policy, timeout=timeout),
        })
        if normalized_dependency_policy.get("mode") == "request" or normalized_image_pull_policy == "request":
            requirement["approval_required"] = True
        return requirement

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
            else "no Workspace root configured"
        )
        policy_desc = (
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

        async with Client(transport) as client:  # type: ignore[arg-type]
            tool_list = await client.list_tools()
            for tool in tool_list:
                tool_tags = []
                if hasattr(tool, "_meta") and tool._meta:  # type: ignore[attr-defined]
                    tool_tags = tool._meta.get("_fastmcp", {}).get("tags", [])  # type: ignore[index]
                tool_tags.extend(normalized_tags)
                action.register_action(
                    action_id=tool.name,
                    desc=tool.description,
                    kwargs=DataFormatter.from_schema_to_kwargs_format(tool.inputSchema),
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
                    execution_resources=cast(list[ExecutionResourceRequirement], [
                        {
                            "requirement_id": f"mcp:{ tool.name }",
                            "kind": "mcp",
                            "scope": "agent",
                            "resource_key": tool.name,
                            "config": {"transport": transport},
                            "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                            "approval_required": approval_required,
                        }
                    ]),
                )
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
        sandbox: Literal["auto", "docker", "trusted_local"] = "trusted_local",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
        timeout: int = 60,
    ):
        action = self._action
        sandbox_mode = self._normalize_code_sandbox(sandbox)
        if sandbox_mode != "trusted_local" and (
            preset_objects is not None or base_vars is not None or allowed_return_types is not None
        ):
            raise ValueError(
                "preset_objects, base_vars, and allowed_return_types require sandbox='trusted_local'."
            )
        execution_resources: list[ExecutionResourceRequirement]
        if sandbox_mode == "trusted_local":
            execution_resources = cast(list[ExecutionResourceRequirement], [
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
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                }
            ])
        else:
            execution_resources = [
                self._docker_runtime_requirement(
                    action_id=action_id,
                    language="python",
                    image=docker_image,
                    timeout=timeout,
                    default_policy=default_policy,
                    docker_binary=docker_binary,
                    docker_default_args=docker_default_args,
                    dependency_policy=dependency_policy,
                    provisioning_profile=provisioning_profile,
                    image_pull_policy=image_pull_policy,
                )
            ]
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={"python_code": (str, "Python code to execute in the sandbox.")},
            executor=action._create_executor(
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
            execution_resources=execution_resources,
        )
        return action

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
        sandbox: Literal["auto", "docker", "trusted_local"] = "trusted_local",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
    ):
        action = self._action
        sandbox_mode = self._normalize_code_sandbox(sandbox)
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
                    },
                )
            ]
        action.register_action(
            action_id=action_id,
            desc=model_desc,
            kwargs={
                "cmd": ("str | list[str]", "Command to run inside the sandbox."),
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
        sandbox: Literal["auto", "docker", "trusted_local"] = "trusted_local",
        docker_image: str = "node:22-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
    ):
        action = self._action
        sandbox_mode = self._normalize_code_sandbox(sandbox)
        if sandbox_mode == "trusted_local":
            execution_resources = cast(list[ExecutionResourceRequirement], [
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
                    "policy": cast(ExecutionResourcePolicy, default_policy or {}),
                }
            ])
        else:
            execution_resources = [
                self._docker_runtime_requirement(
                    action_id=action_id,
                    language="nodejs",
                    image=docker_image,
                    timeout=timeout,
                    default_policy=default_policy,
                    docker_binary=docker_binary,
                    docker_default_args=docker_default_args,
                    dependency_policy=dependency_policy,
                    provisioning_profile=provisioning_profile,
                    image_pull_policy=image_pull_policy,
                    runtime_profile={
                        "cwd": cwd,
                        "env": env,
                    },
                )
            ]
        action.register_action(
            action_id=action_id,
            desc=desc,
            kwargs={
                "js_code": (str, "JavaScript code to execute with Node.js."),
                "args": ("list[str]", "Optional command-line arguments."),
            },
            executor=action._create_executor("NodeJSActionExecutor", timeout=timeout),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=expose_to_model,
            execution_resources=execution_resources,
        )
        return action

    def register_code_runtime_action(
        self,
        *,
        language: str,
        action_id: str | None = None,
        desc: str | None = None,
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        docker_image: str | None = None,
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: Literal["deny", "request", "install"] | dict[str, Any] | str | None = None,
        provisioning_profile: Literal["strict", "developer", "ci"] | str = "strict",
        image_pull_policy: Literal["never", "request", "if_missing", "always"] | str | None = None,
        timeout: int = 60,
    ):
        from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
            get_code_runtime_profile,
        )

        action = self._action
        profile = get_code_runtime_profile(language, image=docker_image)
        canonical_language = profile["language"]
        resolved_action_id = action_id or f"run_{ canonical_language }_code"
        resolved_desc = desc or (
            f"Run { canonical_language } code inside a Docker-backed code runtime sandbox. "
            "The runtime image and dependency preparation are controlled by host policy; "
            "do not include package-manager or compiler commands unless the action schema explicitly asks for source files."
        )
        execution_resources = [
            self._docker_runtime_requirement(
                action_id=resolved_action_id,
                language=canonical_language,
                image=str(profile["image"]),
                timeout=timeout,
                default_policy=default_policy,
                docker_binary=docker_binary,
                docker_default_args=docker_default_args,
                dependency_policy=dependency_policy,
                provisioning_profile=provisioning_profile,
                image_pull_policy=image_pull_policy,
                runtime_profile={
                    "source_file": profile["source_file"],
                    "entrypoint": profile["entrypoint"],
                },
            )
        ]
        action.register_action(
            action_id=resolved_action_id,
            desc=resolved_desc,
            kwargs={
                "source_code": (str, "Primary source code to run in the configured language runtime."),
                "files": ("dict[str, str] | None", "Optional additional source or dependency manifest files."),
                "args": ("list[str]", "Optional command-line arguments passed to the program."),
            },
            executor=action._create_executor(
                "CodeRuntimeActionExecutor",
                language=canonical_language,
                timeout=timeout,
            ),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            sandbox_required=True,
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

    # ------------------------------------------------------------------
    # Seatbelt sandbox registration (macOS only)
    # ------------------------------------------------------------------

    def register_seatbelt_sandbox_action(
        self,
        *,
        action_id: str = "seatbelt_sandbox",
        desc: str = "Execute Python code inside a macOS Seatbelt sandbox (sandbox-exec + SBPL).",
        tags: str | list[str] | None = None,
        default_policy: "ActionPolicy | None" = None,
        expose_to_model: bool = False,
        network: bool = False,
        writable_paths: list[str] | None = None,
        protected_paths: list[str] | None = None,
        deny_read_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
        timeout: int = 60,
    ):
        """Register a Python sandbox action backed by macOS Seatbelt.

        SBPL Profile Design:
        - File read: globally allowed (needed for system libs)
        - File write: whitelist only (writable_paths + temp dirs)
        - Protected paths: deny write (overrides writable_paths, last-match-wins)
        - Deny-read paths: deny both read and write (for secrets)
        - Network: optional outbound switch

        Only available on macOS. On other platforms the provider will
        report itself as unavailable at handle-creation time.
        """
        action = self._action.register_action(
            action_id=action_id,
            desc=desc,
            tags=tags,
            default_policy=default_policy,
            expose_to_model=expose_to_model,
        )

        merged_policy = self._merge_action_policy(
            action_policy=default_policy,
            timeout=timeout,
        )

        action.bind_execution(
            default_policy=merged_policy,
            side_effect_level="read",
            execution_resources=cast(list[ExecutionResourceRequirement], [
                {
                    "requirement_id": f"seatbelt:{ action_id }",
                    "kind": "seatbelt",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "timeout": timeout,
                        "network": network,
                        "writable_paths": writable_paths or [],
                        "protected_paths": protected_paths or [],
                        "deny_read_paths": deny_read_paths or [],
                        "extra_sbpl_rules": extra_sbpl_rules,
                    },
                    "policy": cast(ExecutionResourcePolicy, merged_policy),
                }
            ]),
        )
        return action
