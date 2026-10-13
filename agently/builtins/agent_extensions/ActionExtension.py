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

import asyncio
import contextlib
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Literal, TYPE_CHECKING, ParamSpec, TypeAlias, TypeVar, cast
from typing_extensions import Self, overload

from agently.core import BaseAgent
from agently.core.operation.Action.ActionMetadata import _scoped_action_list
from agently.core.model.ModelRequestRunner import PreparedModelResponse
from agently.core.runtime.RuntimeContext import (
    get_current_action_policy,
    get_current_agent_execution_context,
)
from agently.utils import DeprecationWarnings
from agently.builtins.actions.Cmd import DEFAULT_SAFE_CMD_PREFIXES
from agently.types.data.shell import ShellApproval, ShellEnvironment, ShellLanguage, ShellRiskHandler

if TYPE_CHECKING:
    from agently.core import Prompt, TaskWorkspace
    from agently.core.operation.Action import ToolCommand, ToolExecutionRecord
    from agently.types.data import ActionCall, ActionResult, AgentlyModelResult, KwargsType, MCPConfigs, ReturnType
    from agently.types.plugins import AgentExecution

from agently.base import action as global_action

P = ParamSpec("P")
R = TypeVar("R")
CapabilityDescMode: TypeAlias = Literal["append", "override", "default"]
CodeSandboxMode: TypeAlias = Literal["auto", "docker", "trusted_local"]
DependencyPolicyMode: TypeAlias = Literal["deny", "request", "install"]
ProvisioningProfileMode: TypeAlias = Literal["strict", "developer", "ci"]
ImagePullPolicyMode: TypeAlias = Literal["never", "request", "if_missing", "always"]
_TASK_WORKSPACE_ROOT_UNSET = object()


class ActionExtension(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.action = type(global_action)(self.plugin_manager, self.settings)
        self.tool = self.action

        self.use_mcp = default_stage_call_bridge.as_sync(self.async_use_mcp)
        self.use_sandbox = self.use_action_sandbox
        self.use_python = self.enable_python
        self.use_shell = self.enable_shell
        self.use_nodejs = self.enable_nodejs
        self.use_code_runtime = self.enable_code_runtime
        self.use_sqlite = self.enable_sqlite
        self.use_docker = self.enable_docker
        self.use_task_workspace_file_actions = self.enable_task_workspace_file_actions

        self.settings.setdefault("action.loop.max_rounds", None, inherit=True)
        self.settings.setdefault("action.loop.concurrency", None, inherit=True)
        self.settings.setdefault("action.loop.timeout", None, inherit=True)
        self.settings.setdefault("action.loop.max_consecutive_failed_rounds_per_action", 2, inherit=True)
        self.settings.setdefault("action.loop.enabled", True, inherit=True)
        self.settings.setdefault("tool.loop.max_rounds", None, inherit=True)
        self.settings.setdefault("tool.loop.concurrency", None, inherit=True)
        self.settings.setdefault("tool.loop.timeout", None, inherit=True)
        self.settings.setdefault("tool.loop.max_consecutive_failed_rounds_per_action", 2, inherit=True)
        self.settings.setdefault("tool.loop.enabled", True, inherit=True)
        self.settings.setdefault("execution_resource.owner_id", self.name, inherit=False)

        self.__action_logs: list[ActionResult] = []
        self.__prepared_action_results: dict[str, Any] | None = None
        self.__action_planning_handler = None
        self.__action_execution_handler = None
        self.__required_action_ids: list[str] = []

        self.extension_handlers.append("request_prefixes", self.__request_prefix)
        self.extension_handlers.append("broadcast_prefixes", self.__broadcast_prefix)

    def __import_global_action(self, action_id: str):
        if not isinstance(action_id, str) or action_id.strip() == "":
            return
        local_registry = getattr(self.action, "action_registry", None)
        if local_registry is not None and local_registry.has(action_id):
            return
        global_registry = getattr(global_action, "action_registry", None)
        if global_registry is None or not global_registry.has(action_id):
            return

        spec = global_registry.get_spec(action_id)
        executor = global_registry.get_executor(action_id)
        if spec is None or executor is None:
            return

        copied_spec = dict(spec)
        for key in ("kwargs", "default_policy", "meta"):
            if isinstance(copied_spec.get(key), dict):
                copied_spec[key] = dict(copied_spec[key])
        if isinstance(copied_spec.get("tags"), list):
            copied_spec["tags"] = list(copied_spec["tags"])
        if isinstance(copied_spec.get("execution_resources"), list):
            copied_spec["execution_resources"] = [dict(item) for item in copied_spec["execution_resources"]]

        self.action.register_action(
            action_id=str(copied_spec.get("action_id", action_id)),
            desc=str(copied_spec.get("desc", "")),
            kwargs=copied_spec.get("kwargs", {}),
            func=global_registry.get_func(action_id),
            executor=executor,
            required_input_keys=copied_spec.get("required_input_keys"),
            returns=copied_spec.get("returns"),
            tags=copied_spec.get("tags", []),
            default_policy=copied_spec.get("default_policy", {}),
            side_effect_level=copied_spec.get("side_effect_level", "read"),
            approval_required=bool(copied_spec.get("approval_required", False)),
            sandbox_required=bool(copied_spec.get("sandbox_required", False)),
            replay_safe=bool(copied_spec.get("replay_safe", True)),
            concurrency_mode=cast(
                Literal["parallel", "exclusive"],
                copied_spec.get("concurrency_mode", "exclusive"),
            ),
            expose_to_model=bool(copied_spec.get("expose_to_model", True)),
            execution_resources=copied_spec.get("execution_resources", []),
            meta=copied_spec.get("meta", {}),
        )

    def register_action(
        self,
        *,
        name: str,
        desc: str,
        kwargs: "KwargsType",
        func: Callable,
        returns: "ReturnType | None" = None,
        concurrency_mode: Literal["parallel", "exclusive"] = "exclusive",
    ) -> Self:
        """Register one Agent-local Action.

        In eligible programmatic batches, ``concurrency_mode="parallel"``
        permits overlap with other parallel Actions. ``"exclusive"`` keeps
        this Action behind an ordering barrier.
        """
        self.action.register_action(
            action_id=name,
            desc=desc,
            kwargs=kwargs,
            func=func,
            tags=[f"agent-{ self.name }"],
            returns=returns,
            concurrency_mode=concurrency_mode,
        )
        return self

    def register_tool(
        self,
        *,
        name: str,
        desc: str,
        kwargs: "KwargsType",
        func: Callable,
        returns: "ReturnType | None" = None,
    ) -> Self:
        return self.register_action(name=name, desc=desc, kwargs=kwargs, func=func, returns=returns)

    def action_func(self, func: Callable[P, R]) -> Callable[P, R]:
        self.action.action_func(func)
        name = func.__name__
        self.action.tag([name], [f"agent-{ self.name }"])
        return func

    def tool_func(self, func: Callable[P, R]) -> Callable[P, R]:
        return self.action_func(func)

    @staticmethod
    def _normalize_action_items(actions: Any):
        if isinstance(actions, str) or callable(actions) or hasattr(actions, "register_actions"):
            return [actions]
        if isinstance(actions, (list, tuple, set)):
            return list(actions)
        return [actions]

    @staticmethod
    def _normalize_registered_action_ids(value: Any):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item)]
        return []

    def _register_action_items(self, actions: Callable | str | list[str | Callable] | Any) -> list[str]:
        names: list[str] = []
        local_registry = getattr(self.action, "action_registry", None)
        agent_tag = f"agent-{ self.name }"
        for action_item in self._normalize_action_items(actions):
            register_actions = getattr(action_item, "register_actions", None)
            if callable(register_actions):
                language_policy = self.settings.get("agent.language_policy", None)
                apply_language_policy = getattr(action_item, "apply_language_policy", None)
                if isinstance(language_policy, Mapping) and callable(apply_language_policy):
                    apply_language_policy(language_policy)
                names.extend(self._normalize_registered_action_ids(register_actions(self.action, tags=[agent_tag])))
                continue
            if isinstance(action_item, str):
                self.__import_global_action(action_item)
                names.append(action_item)
            else:
                action_name = getattr(action_item, "__name__", "")
                if not action_name:
                    raise TypeError("use_actions() expects action names, callables, or built-in action packages.")
                if action_name not in self.action.tool_funcs and (
                    local_registry is None or not local_registry.has(action_name)
                ):
                    self.action_func(action_item)
                names.append(action_name)
        if names:
            self.action.tag(names, agent_tag)
        return names

    @overload
    def use_actions(
        self,
        actions: object,
        *,
        always: Literal[True],
    ) -> Self: ...

    @overload
    def use_actions(
        self,
        actions: object,
        *,
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

    def use_actions(
        self,
        actions: object,
        *,
        always: bool = False,
    ) -> "Self | AgentExecution":
        """Attach Actions to one execution, or to future runs with ``always=True``."""
        if not always:
            return self.create_execution().use_actions(actions)
        self._register_action_items(actions)
        return self

    @overload
    def use_action(
        self,
        actions: object,
        *,
        always: Literal[True],
    ) -> Self: ...

    @overload
    def use_action(
        self,
        actions: object,
        *,
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

    def use_action(
        self,
        actions: object,
        *,
        always: bool = False,
    ) -> "Self | AgentExecution":
        """Attach one Action to a run, or to future runs with ``always=True``."""
        return self.use_actions(actions, always=always)

    def use_acp(
        self,
        *,
        root: str | Path | None = None,
        agent_ids: list[str] | tuple[str, ...] | str | None = None,
        provider: Any | None = None,
        on_missing: Literal["skip", "error"] = "skip",
        timeout_seconds: float | None = 600,
        action_prefix: str = "",
    ) -> Self:
        from agently.builtins.actions import ACP

        resolved_root = root
        if resolved_root is None:
            task_workspace = getattr(self, "task_workspace", None)
            task_workspace_root = getattr(task_workspace, "root", None)
            if task_workspace_root is not None:
                resolved_root = task_workspace_root
        acp = ACP(
            root=resolved_root,
            agent_ids=agent_ids,
            provider=provider,
            on_missing=on_missing,
            timeout_seconds=timeout_seconds,
            action_prefix=action_prefix,
        )
        self._register_action_items(acp)
        diagnostics = acp.list_agents().get("diagnostics", [])
        if diagnostics:
            self.settings.set("agent.acp.diagnostics", cast(Any, diagnostics))
        return self

    @overload
    def require_actions(
        self,
        actions: object,
        *,
        always: Literal[True],
    ) -> Self: ...

    @overload
    def require_actions(
        self,
        actions: object,
        *,
        always: Literal[False] = False,
    ) -> "AgentExecution": ...

    def require_actions(
        self,
        actions: object,
        *,
        always: bool = False,
    ) -> "Self | AgentExecution":
        """Require Actions for one execution, or for future runs with ``always=True``."""
        if not always:
            return self.create_execution().require_actions(actions)
        for name in self._register_action_items(actions):
            if name not in self.__required_action_ids:
                self.__required_action_ids.append(name)
        return self

    def _collect_required_action_ids(self) -> list[str]:
        return list(self.__required_action_ids)

    @staticmethod
    def _action_item_id(item: dict[str, Any]) -> str:
        return str(item.get("action_id") or item.get("name") or "").strip()

    def _get_scoped_action_list(self) -> list[dict[str, Any]]:
        return _scoped_action_list(self.action, self.name)

    def use_tools(self, tools: object) -> "AgentExecution":
        """Compatibility alias for execution-local ``use_actions(...)``."""
        return self.use_actions(tools)

    def use_tool(self, tools: object) -> "AgentExecution":
        """Compatibility alias for execution-local ``use_action(...)``."""
        return self.use_action(tools)

    @staticmethod
    def _build_capability_desc(
        default_desc: str,
        desc: str | None = None,
        *,
        mode: CapabilityDescMode = "append",
    ) -> str:
        extra = desc.strip() if isinstance(desc, str) else ""
        if not extra or mode == "default":
            return default_desc
        if mode == "override":
            return extra
        if mode != "append":
            raise ValueError("desc_mode must be one of: 'append', 'override', 'default'.")
        return f"{ default_desc }\n\nAdditional guidance: { extra }"

    @staticmethod
    def _normalize_code_sandbox_mode(value: CodeSandboxMode) -> CodeSandboxMode:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if normalized not in {"auto", "docker", "trusted_local"}:
            raise ValueError("sandbox must be one of: 'auto', 'docker', 'trusted_local'.")
        return cast(CodeSandboxMode, normalized)

    async def async_use_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
    ) -> Self:
        await self.action.async_use_mcp(transport, headers=headers, tags=[f"agent-{ self.name }"])
        return self

    def use_action_sandbox(
        self,
        sandbox: str,
        *,
        action_id: str | None = None,
        expose_to_model: bool = True,
        sandbox_mode: CodeSandboxMode | None = None,
        **kwargs: Any,
    ) -> Self:
        sandbox_name = sandbox.strip().lower() if isinstance(sandbox, str) else ""
        if sandbox_name in {"python", "python_sandbox"}:
            resolved_action_id = action_id or "python_sandbox"
            kwargs.setdefault("sandbox", sandbox_mode or "auto")
            self.action.register_python_sandbox_action(
                action_id=resolved_action_id,
                tags=[f"agent-{ self.name }"],
                expose_to_model=expose_to_model,
                **kwargs,
            )
            return self
        if sandbox_name in {"bash", "shell", "bash_sandbox"}:
            resolved_action_id = action_id or "bash_sandbox"
            kwargs.setdefault("sandbox", sandbox_mode or "auto")
            self.action.register_bash_sandbox_action(
                action_id=resolved_action_id,
                tags=[f"agent-{ self.name }"],
                expose_to_model=expose_to_model,
                **kwargs,
            )
            return self
        raise ValueError("sandbox must be one of: 'python', 'bash'.")

    def enable_python(
        self,
        *,
        action_id: str = "run_python",
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        preset_objects: dict[str, object] | None = None,
        base_vars: dict[str, Any] | None = None,
        allowed_return_types: list[type] | None = None,
        sandbox: CodeSandboxMode = "auto",
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
        timeout: int = 60,
    ) -> Self:
        if preset_objects is not None or base_vars is not None or allowed_return_types is not None:
            raise ValueError(
                "enable_python() no longer supports in-process preset_objects, base_vars, or "
                "allowed_return_types. Materialize explicit files and use the Workspace-bound "
                "CodeExecution input/output contract."
            )
        sandbox_mode = self._normalize_code_sandbox_mode(sandbox)
        providers: list[str] | None = None
        unsafe_fallback = False
        isolation: Literal["required", "preferred", "none"] = "required"
        if sandbox_mode == "docker":
            providers = ["docker"]
        elif sandbox_mode == "trusted_local":
            providers = ["trusted_local"]
            unsafe_fallback = True
            isolation = "none"
        default_desc = (
            "Run Python code through the canonical Workspace-bound CodeExecution chain. "
            "The host prepares an immutable bundle, selects one eligible provider, and reads "
            "declared outputs back through TaskWorkspace."
        )
        return self.enable_code_runtime(
            language="python",
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            desc_mode="override",
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

    def enable_shell(
        self,
        *,
        root: str | Path | None = None,
        commands: list[str] | None = None,
        action_id: str | None = None,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        max_output_chars: int = 20000,
        sandbox: CodeSandboxMode | None = None,
        docker_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
        environment: ShellEnvironment | None = None,
        approval: ShellApproval = "all",
        shell: ShellLanguage | None = None,
        binary: str | None = None,
        read_paths: Mapping[str, str | Path] | None = None,
        max_output_bytes: int = 20000,
        deny: Sequence[str] = (),
        risk_handler: ShellRiskHandler | None = None,
    ) -> Self:
        """Expose general Shell source with explicit environment and approval.

        Defaults to offline isolation and approval for every call. Windows uses
        PowerShell; other hosts use Bash. ``host`` has no filesystem/network
        sandbox. ``read_paths`` maps resource aliases to host-selected read-only
        directories (e.g. an entire Skill package, without per-script Actions).
        Selective write/delete approval uses advisory risk analysis, not a hard
        sandbox. Missing providers never fall back to host execution.

        Explicit legacy ``commands``/``sandbox`` retains the old argv contract;
        do not mix it with the new Shell policy parameters.
        """
        task_workspace = getattr(self, "task_workspace", None)
        if root is None and task_workspace is not None:
            root = getattr(task_workspace, "root", None)
        root_path = Path(root).expanduser().resolve() if root is not None else None
        if commands is None and sandbox is None:
            if root_path is None or not root_path.is_dir():
                raise ValueError("Shell requires an existing root or bound TaskWorkspace")
            environment = environment or "offline"
            shell = shell or ("powershell" if os.name == "nt" else "bash")
            if environment not in {"offline", "online", "host"} or approval not in {"all", "write", "delete", "none"}:
                raise ValueError("Invalid Shell environment or approval mode")
            if shell not in {"bash", "powershell"}:
                raise ValueError("shell must be 'bash' or 'powershell'")
            if (
                isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
                or isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0
            ):
                raise ValueError("Shell timeout and output byte limit must be positive")
            if docker_default_args or dependency_policy is not None or max_output_chars != 20000:
                raise ValueError("General Shell does not accept legacy Docker arguments or dependency_policy")
            read_only = task_workspace is not None and root_path == Path(str(task_workspace.root)).resolve() and task_workspace.mode == "read_only"
            if read_only and environment == "host":
                raise ValueError("Host Shell cannot enforce a read-only TaskWorkspace; select isolation or grant read_write")
            paths: dict[str, str] = {}
            for alias, path in (read_paths or {}).items():
                if not isinstance(alias, str) or not alias.isidentifier():
                    raise ValueError("Shell read_paths aliases must be identifiers")
                resolved = Path(path).expanduser().resolve(strict=True)
                if not resolved.is_dir() or resolved == Path(resolved.anchor):
                    raise ValueError("Shell read_paths requires bounded directories, not a filesystem root")
                paths[alias] = str(resolved)
            forbidden = ("\n", "\r", "%") if os.name == "nt" else (":", "\n", "\r")
            if environment != "host" and any(char in path for path in [str(root_path), *paths.values()] for char in forbidden):
                raise ValueError("Shell mount paths contain unsupported platform mount characters")
            if any(not isinstance(rule, str) or not rule for rule in deny):
                raise ValueError("Shell deny rules must be non-empty strings")
            if isinstance(deny, str):
                raise ValueError("Shell deny must be a sequence of rules, not a string")
            if binary is not None and (not isinstance(binary, str) or not binary or "\0" in binary):
                raise ValueError("Shell binary must be a non-empty NUL-free string")
            if root_path == Path(root_path.anchor):
                raise ValueError("Shell root cannot be a filesystem root")
            config = {
                "root": str(root_path), "shell": shell, "environment": environment,
                "binary": binary or ("powershell.exe" if os.name == "nt" and shell == "powershell" else "pwsh" if shell == "powershell" else "bash"),
                "env": dict(env or {}), "timeout": timeout, "max_output_bytes": max_output_bytes,
                "read_paths": paths, "read_only": read_only, "docker_image": docker_image,
                "docker_binary": docker_binary, "provisioning_profile": provisioning_profile,
                "image_pull_policy": image_pull_policy,
            }
            executor = self.action._create_executor("ShellActionExecutor",
                config=config, approval=approval, deny=tuple(deny),
                request_factory=self.create_temp_request, risk_handler=risk_handler,
            )
            action_id = action_id or "run_shell"
            from agently.builtins.plugins.ExecutionResourceProvider._windows_sandbox import sandbox_paths
            visible_root, visible_paths = sandbox_paths(config)
            default_desc = (
                f"Run one {shell} script in environment={environment}. "
                f"Starting root: {visible_root}. "
                f"Resource paths: {visible_paths}. "
                "Pipes, redirection and multiline source use the selected interpreter's syntax. "
                "No interactive input, persistent sessions or detached background work. "
                "Return bounded stdout/stderr, exit code and truncation facts. "
                + ("Host mode is NOT sandboxed; cwd does not constrain command effects." if environment == "host" else "Only host-authorized paths are mounted.")
            )
            self.action.register_action(
                action_id=action_id, desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
                kwargs={"command": (str, "Complete source for the selected Shell."), "workdir": (str, "Starting directory under the stated root; defaults to '.'.")},
                required_input_keys=["command"], executor=executor,
                tags=[f"agent-{self.name}"], expose_to_model=expose_to_model,
                side_effect_level="exec", replay_safe=False, sandbox_required=environment != "host",
                execution_resources=[{
                    "requirement_id": f"shell:{action_id}", "kind": "shell", "scope": "action_call",
                    "resource_key": action_id, "config": config,
                    "required_capabilities": {"shell": shell, **({"isolation": {
                        "process_contained": True, "host_filesystem_restricted": True,
                        "network_mode": "disabled" if environment == "offline" else "enabled",
                    }} if environment != "host" else {})},
                }],
                meta={"_host_approval_required_when": lambda call: executor.needs_approval(call)},
            )
            return self
        if environment is not None or approval != "all" or shell is not None or binary is not None or read_paths or deny or risk_handler is not None or max_output_bytes != 20000:
            raise ValueError("Do not mix legacy argv commands/sandbox with general Shell configuration")
        DeprecationWarnings.warn_deprecated_once(
            "enable_shell.argv",
            "enable_shell(commands=.../sandbox=...) is the legacy argv interface. "
            "For general Shell source, select environment= and approval= without legacy parameters; "
            "the default Action becomes run_shell(command=..., workdir=...). Legacy cleanup is planned for 4.2.",
        )
        action_id = action_id or "run_bash"
        sandbox = sandbox or "auto"
        if (
            task_workspace is not None
            and root_path is not None
            and root_path == Path(str(getattr(task_workspace, "root", ""))).expanduser().resolve()
            and getattr(task_workspace, "mode", "read_only") == "read_only"
            and sandbox == "trusted_local"
        ):
            raise ValueError(
                "A read-only TaskWorkspace cannot use sandbox='trusted_local' because cwd checks "
                "cannot enforce filesystem write isolation. Use the Docker sandbox or grant "
                "the TaskWorkspace read_write mode explicitly."
            )
        roots: list[str | Path] | None = [str(root_path)] if root_path is not None else None
        resolved_commands = list(commands) if commands is not None else list(DEFAULT_SAFE_CMD_PREFIXES)
        task_workspace_mounts: list[dict[str, str]] | None = None
        output_artifact_dir: str | None = None
        if (
            task_workspace is not None
            and root_path is not None
            and root_path == Path(str(getattr(task_workspace, "root", ""))).expanduser().resolve()
        ):
            fallback_root = root_path / ".agently" / "files" / str(task_workspace.execution_id)
            output_artifact_dir = str(fallback_root / "shell-output")
            task_workspace_mounts = [
                {
                    "host_path": str(root_path),
                    "container_path": "/task_workspace",
                    "mode": "rw" if getattr(task_workspace, "mode", "read_only") == "read_write" else "ro",
                }
            ]
            if getattr(task_workspace, "mode", "read_only") == "read_only":
                task_workspace_mounts.append(
                    {
                        "host_path": str(fallback_root),
                        "container_path": f"/task_workspace/.agently/files/{task_workspace.execution_id}",
                        "mode": "rw",
                    }
                )
        boundary_text = (
            "Docker-backed task_workspace boundary"
            if sandbox != "trusted_local"
            else "explicitly trusted local task_workspace boundary"
        )
        default_desc = (
            f"Run an allowlisted shell command inside a { boundary_text } for tests, builds, "
            "git status inspection, and read-only diagnostics. Prefer dedicated TaskWorkspace actions "
            "`read_file`, `glob_files`, `grep_files`, `edit_file`, `apply_patch`, and `write_file` for "
            "file reading, searching, editing, and writing. Do not start background long-running "
            "commands; each command is bounded by timeout and output preview limits. Dependency installation "
            "is controlled by the host resource policy, not by model-visible action inputs."
        )
        self.action.register_bash_sandbox_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            allowed_cmd_prefixes=resolved_commands,
            allowed_workdir_roots=roots,
            timeout=timeout,
            env=env,
            max_output_chars=max_output_chars,
            output_artifact_dir=output_artifact_dir,
            task_workspace_mounts=task_workspace_mounts,
            sandbox=sandbox,
            docker_image=docker_image,
            docker_binary=docker_binary,
            docker_default_args=docker_default_args,
            dependency_policy=dependency_policy,
            provisioning_profile=provisioning_profile,
            image_pull_policy=image_pull_policy,
        )
        return self

    def enable_nodejs(
        self,
        *,
        action_id: str = "run_nodejs",
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        node_binary: str = "node",
        cwd: str | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
        sandbox: CodeSandboxMode = "auto",
        docker_image: str = "node:22-slim",
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
    ) -> Self:
        if node_binary != "node" or cwd is not None or env is not None:
            raise ValueError(
                "enable_nodejs() no longer accepts provider-owned node_binary, cwd, or env settings. "
                "Use source files, arguments, TaskWorkspace access, and provider candidate config."
            )
        sandbox_mode = self._normalize_code_sandbox_mode(sandbox)
        providers: list[str] | None = None
        unsafe_fallback = False
        isolation: Literal["required", "preferred", "none"] = "required"
        if sandbox_mode == "docker":
            providers = ["docker"]
        elif sandbox_mode == "trusted_local":
            providers = ["trusted_local"]
            unsafe_fallback = True
            isolation = "none"
        default_desc = (
            "Run JavaScript with Node.js through the canonical Workspace-bound CodeExecution chain. "
            "The host prepares an immutable bundle, selects one eligible provider, and reads "
            "declared outputs back through TaskWorkspace."
        )
        return self.enable_code_runtime(
            language="nodejs",
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            desc_mode="override",
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

    def enable_code_runtime(
        self,
        *,
        language: str,
        action_id: str | None = None,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        providers: Sequence[str | Mapping[str, Any]] | None = None,
        unsafe_fallback: bool = False,
        isolation: Literal["required", "preferred", "none"] = "required",
        docker_image: str | None = None,
        docker_binary: str = "docker",
        docker_default_args: list[str] | None = None,
        dependency_policy: DependencyPolicyMode | dict[str, Any] | None = None,
        provisioning_profile: ProvisioningProfileMode = "strict",
        image_pull_policy: ImagePullPolicyMode | None = None,
        timeout: int = 60,
    ) -> Self:
        from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter

        adapter = get_code_runtime_adapter(language)
        display_names = {
            "nodejs": "JavaScript/Node.js",
            "cpp": "C++",
        }
        language_name = display_names.get(adapter.language_id, adapter.language_id.capitalize())
        default_desc = (
            f"Run { language_name } code through a Workspace-bound execution provider. "
            "The host-selected provider and runtime policy own isolation and dependency preparation; "
            "the model can provide source files and arguments but not raw compiler or package-manager commands."
        )
        self.action.register_code_runtime_action(
            language=adapter.language_id,
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
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
        return self

    def enable_sqlite(
        self,
        *,
        database: str = ":memory:",
        action_id: str = "query_sqlite",
        read_only: bool = True,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        uri: bool = False,
    ) -> Self:
        default_desc = "Query a SQLite database through a managed execution resource."
        self.action.register_sqlite_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            database=database,
            read_only=read_only,
            uri=uri,
        )
        return self

    def enable_docker(
        self,
        *,
        action_id: str = "run_docker",
        image: str | None = None,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = False,
        timeout: int = 60,
        docker_binary: str = "docker",
        default_args: list[str] | None = None,
    ) -> Self:
        default_desc = "Run a command in a Docker container through a managed execution resource."
        self.action.register_docker_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            image=image,
            timeout=timeout,
            docker_binary=docker_binary,
            default_args=default_args,
        )
        return self

    def enable_task_workspace_file_actions(
        self,
        *,
        root: str | Path | object = _TASK_WORKSPACE_ROOT_UNSET,
        task_workspace: "TaskWorkspace | None" = None,
        isolated: bool = False,
        read: bool = True,
        write: bool = True,
        search: bool = True,
        list_files: bool = True,
        export: bool = False,
        action_prefix: str = "",
        expose_to_model: bool = True,
        max_file_bytes: int = 20000,
        max_search_file_bytes: int = 200000,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        coding_agent: bool = False,
    ) -> Self:
        explicit_root = root is not _TASK_WORKSPACE_ROOT_UNSET
        explicit_task_workspace = task_workspace
        if task_workspace is None and not explicit_root:
            task_workspace = getattr(self, "task_workspace", None)
        if isolated and root is _TASK_WORKSPACE_ROOT_UNSET:
            root = tempfile.mkdtemp(prefix="agently-task_workspace-action-")
        elif root is _TASK_WORKSPACE_ROOT_UNSET and task_workspace is not None:
            root = getattr(task_workspace, "root")
        elif root is _TASK_WORKSPACE_ROOT_UNSET:
            raise RuntimeError(
                "TaskWorkspace file actions require an explicit root or an Agent " "TaskWorkspace binding."
            )
        root_path = Path(str(root)).expanduser().resolve()
        agent_tag = f"agent-{ self.name }"
        prefix = action_prefix.strip()
        task_workspace_for_actions = None
        if explicit_task_workspace is not None:
            task_workspace_root = Path(str(getattr(task_workspace, "root", ""))).expanduser().resolve()
            if task_workspace_root == root_path:
                task_workspace_for_actions = task_workspace
        elif explicit_root:
            from agently.core.TaskWorkspace import TaskWorkspace

            task_workspace_for_actions = TaskWorkspace(
                root_path,
                mode="read_write" if write else "read_only",
            )
        elif task_workspace is not None:
            task_workspace_for_actions = task_workspace

        if task_workspace_for_actions is None:
            from agently.core.TaskWorkspace import TaskWorkspace

            task_workspace_for_actions = TaskWorkspace(
                root_path,
                mode="read_write" if write else "read_only",
            )
        active_task_workspace = cast("TaskWorkspace", task_workspace_for_actions)

        def action_name(name: str):
            return f"{ prefix }{ name }" if prefix else name

        def has_action(name: str):
            registry = getattr(self.action, "action_registry", None)
            return bool(registry is not None and registry.has(action_name(name)))

        def resolve_task_workspace_path(path: str | Path = "."):
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = root_path / candidate
            resolved = candidate.expanduser().resolve()
            try:
                resolved.relative_to(root_path)
            except ValueError as error:
                raise ValueError(f"Path is outside task_workspace root: { path }") from error
            return resolved

        def current_task_workspace() -> "TaskWorkspace":
            # Implicit Agent bindings follow the current run, like code execution.
            # Explicit file roots/workspaces retain their caller-owned boundary.
            if not explicit_root and explicit_task_workspace is None and not isolated:
                context = get_current_agent_execution_context()
                workspace = getattr(context, "task_workspace", None)
                if (workspace is not None
                        and workspace.task_workspace_id == active_task_workspace.task_workspace_id):
                    return cast("TaskWorkspace", workspace)
            return active_task_workspace

        def mutation_task_workspace():
            if current_task_workspace().mode == "read_write":
                return current_task_workspace()
            policy = get_current_action_policy() or {}
            if policy.get("policy_approval_granted") is not True:
                return current_task_workspace()
            return current_task_workspace()._derive(
                mode="read_write",
            )

        def task_workspace_mutation_approval(
            operation: str,
            action_call: dict[str, Any],
        ) -> dict[str, Any]:
            if current_task_workspace().mode == "read_write":
                return {"required": False}
            action_input = action_call.get("action_input", {})
            action_input = action_input if isinstance(action_input, dict) else {}
            path = str(action_input.get("path") or "")
            if operation == "apply_patch":
                paths = patch_paths(str(action_input.get("patch") or ""))
                external_required = any(
                    current_task_workspace()._resolve_external_file_path(item).exists() for item in paths
                )
            else:
                target = current_task_workspace()._resolve_external_file_path(path)
                paths = [str(target.relative_to(root_path))]
                external_required = target.exists()
            if not external_required:
                return {"required": False}
            canonical_paths = [str((root_path / item).resolve()) for item in paths]
            mutation_facts: dict[str, Any] = {
                "operation": operation,
                "path": paths[0] if len(paths) == 1 else None,
                "paths": paths,
                "canonical_path": canonical_paths[0] if len(canonical_paths) == 1 else None,
                "canonical_paths": canonical_paths,
                "task_workspace_id": current_task_workspace().task_workspace_id,
            }
            return {
                "required": True,
                "context": {
                    "risk": "filesystem_write",
                    "subject": f"{operation}:{paths[0] if paths else root_path}",
                    "payload": {"task_workspace_mutation": mutation_facts},
                },
            }

        def relative_path(path: Path):
            return str(path.relative_to(root_path))

        def ordinary_action_path(path: str | Path):
            target = current_task_workspace().resolve_file_path(path)
            return current_task_workspace()._ordinary_file_relative_path(target)

        def file_result_action_output(result: Any):
            return {
                "status": "success",
                "ok": True,
                "data": result,
                "result": result,
            }

        read_state: dict[str, dict[str, Any]] = {}

        def inspect_task_workspace_file(path: str | Path):
            info = dict(current_task_workspace().inspect_file(path))
            info["path"] = ordinary_action_path(path)
            return info

        def remember_read(path: str | Path, result: Mapping[str, Any], *, offset: int = 0):
            if not coding_agent:
                return
            if offset != 0 or result.get("truncated") or not result.get("ok", result.get("readable")):
                return
            path_text = str(result.get("path") or relative_path(resolve_task_workspace_path(path)))
            sha256 = str(result.get("sha256") or "")
            if sha256:
                read_state[path_text] = {"sha256": sha256}

        def remember_write(path: str | Path, result: Mapping[str, Any]):
            if not coding_agent:
                return
            path_text = str(result.get("path") or relative_path(resolve_task_workspace_path(path)))
            sha256 = str(result.get("sha256") or "")
            if sha256:
                read_state[path_text] = {"sha256": sha256}

        def require_fresh_for_write(path: str | Path, expected_sha256: str | None = None):
            if not coding_agent:
                return
            info = inspect_task_workspace_file(path)
            path_text = str(info.get("path") or relative_path(resolve_task_workspace_path(path)))
            current_sha = str(info.get("sha256") or "")
            if not info.get("exists"):
                if expected_sha256 not in (None, ""):
                    raise ValueError("TaskWorkspace file does not exist; expected_sha256 cannot be satisfied.")
                return
            if expected_sha256:
                if current_sha != str(expected_sha256):
                    raise ValueError("TaskWorkspace file has changed since the expected sha256.")
                return
            state = read_state.get(path_text)
            if not state:
                raise PermissionError(
                    "File has not been read through this coding-agent action set; read it first or pass expected_sha256."
                )
            if str(state.get("sha256") or "") != current_sha:
                raise ValueError("File has been modified since it was read; read it again before writing.")

        async def manager_read_file(path: str, max_bytes: int = max_file_bytes, offset: int = 0):
            result = await current_task_workspace().read_file(
                path,
                max_bytes=max_bytes,
                offset=offset,
            )
            return result.to_dict()

        async def manager_write_file(path: str, content: str, append: bool = False):
            selected_workspace = mutation_task_workspace()
            result = await selected_workspace.write_file(path, content, append=append)
            return result.to_dict()

        async def manager_edit_file(
            path: str,
            old_string: str,
            new_string: str,
            *,
            replace_all: bool = False,
            expected_sha256: str | None = None,
        ):
            require_fresh_for_write(path, expected_sha256)
            selected_workspace = mutation_task_workspace()
            result = await selected_workspace.edit_file(
                path,
                old_string,
                new_string,
                replace_all=replace_all,
                expected_sha256=expected_sha256,
            )
            result = result.to_dict()
            remember_write(path, result)
            return result

        def patch_paths(patch: str) -> list[str]:
            paths: list[str] = []

            def add(raw_path: str):
                text = raw_path.strip()
                if not text or text == "/dev/null":
                    return
                if text.startswith("a/") or text.startswith("b/"):
                    text = text[2:]
                if "\t" in text:
                    text = text.split("\t", 1)[0]
                target = resolve_task_workspace_path(text)
                normalized = relative_path(target)
                if normalized not in paths:
                    paths.append(normalized)

            for line in str(patch or "").splitlines():
                if line.startswith("diff --git "):
                    parts = line.split()
                    if len(parts) >= 4:
                        add(parts[2])
                        add(parts[3])
                    continue
                if line.startswith("+++ ") or line.startswith("--- "):
                    add(line[4:])
            return paths

        async def manager_apply_patch(patch: str, expected_files: list[str] | None = None):
            paths = patch_paths(patch)
            if not paths:
                raise ValueError("Patch did not declare any file paths.")
            if expected_files is not None:
                expected = [relative_path(resolve_task_workspace_path(item)) for item in expected_files]
                if sorted(paths) != sorted(dict.fromkeys(expected)):
                    raise ValueError("Patch file set does not match expected_files.")
            for path in paths:
                if inspect_task_workspace_file(path).get("exists"):
                    require_fresh_for_write(path)
            selected_workspace = mutation_task_workspace()
            result = await selected_workspace.apply_patch(
                patch,
                expected_files=expected_files,
            )
            for path in paths:
                info = inspect_task_workspace_file(path)
                if info.get("exists"):
                    read_state[str(info.get("path") or path)] = {"sha256": str(info.get("sha256") or "")}
            return result

        async def manager_export_file(
            source_path: str,
            output_path: str,
            export_kind: str,
            options: dict[str, Any] | None = None,
        ):
            selected_workspace = mutation_task_workspace()
            return await selected_workspace.export_file(
                source_path,
                output_path,
                export_kind=export_kind,
                options=options,
            )

        if read and list_files and not has_action("list_files"):

            async def list_task_workspace_files(
                path: str = ".",
                pattern: str = "*",
                max_results: int = 200,
                include_hidden: bool = False,
            ):
                result = await current_task_workspace().glob_files(
                    pattern,
                    path=path,
                    max_results=max_results,
                    include_hidden=include_hidden,
                )
                return list(result.get("matches", []))

            self.action.register_action(
                action_id=action_name("list_files"),
                desc=self._build_capability_desc(
                    f"List files under the task_workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "TaskWorkspace-relative directory or file path. Default: '.'."),
                    "pattern": (str, "Glob pattern. Default: '*'."),
                    "max_results": (int, "Maximum files to return. Default: 200."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=list_task_workspace_files,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "task_workspace", "root": str(root_path)},
            )

        if read and not has_action("read_file"):

            async def read_file(path: str, max_bytes: int = max_file_bytes, offset: int = 0):
                result = await manager_read_file(path, max_bytes=max_bytes, offset=offset)
                remember_read(path, result, offset=offset)
                return file_result_action_output(result)

            self.action.register_action(
                action_id=action_name("read_file"),
                desc=self._build_capability_desc(
                    f"Read a file under the task_workspace root { root_path } through registered TaskWorkspace file IO handlers.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "TaskWorkspace-relative file path."),
                    "max_bytes": (int, f"Maximum bytes to read. Default: { max_file_bytes }."),
                    "offset": (int, "Byte offset to start reading from. Default: 0."),
                },
                func=read_file,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "task_workspace", "root": str(root_path)},
            )

        if read and coding_agent and not has_action("glob_files"):

            async def glob_files(
                pattern: str,
                path: str = ".",
                max_results: int = 200,
                include_hidden: bool = False,
            ):
                return await current_task_workspace().glob_files(
                    pattern,
                    path=path,
                    max_results=max_results,
                    include_hidden=include_hidden,
                )

            self.action.register_action(
                action_id=action_name("glob_files"),
                desc=self._build_capability_desc(
                    (
                        f"Find files under the task_workspace root { root_path } by glob. "
                        "Use this instead of shell find/ls when looking for files."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "pattern": (str, "Glob pattern such as '*.py' or '**/*.md'."),
                    "path": (str, "TaskWorkspace-relative directory or file path. Default: '.'."),
                    "max_results": (int, "Maximum files to return. Default: 200."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=glob_files,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "task_workspace", "root": str(root_path), "coding_agent": True},
            )

        if read and coding_agent and not has_action("grep_files"):

            async def grep_files(
                pattern: str,
                path: str = ".",
                regex: bool = True,
                glob: str | None = None,
                context_lines: int = 0,
                max_results: int = 50,
                include_hidden: bool = False,
            ):
                return await current_task_workspace().grep_files(
                    pattern,
                    path=path,
                    regex=regex,
                    glob=glob,
                    context_lines=context_lines,
                    max_results=max_results,
                    include_hidden=include_hidden,
                    max_file_bytes=max_search_file_bytes,
                )

            self.action.register_action(
                action_id=action_name("grep_files"),
                desc=self._build_capability_desc(
                    (
                        f"Search file contents under the task_workspace root { root_path } with regex or fixed text. "
                        "Use this instead of shell grep/rg when looking inside files."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "pattern": (str, "Regex or fixed text pattern to search for."),
                    "path": (str, "TaskWorkspace-relative directory or file path. Default: '.'."),
                    "regex": (bool, "Treat pattern as a regular expression. Default: True."),
                    "glob": (str, "Optional file glob such as '*.py' or '**/*.md'. Default: None."),
                    "context_lines": (int, "Number of surrounding lines to include. Default: 0."),
                    "max_results": (int, "Maximum matches to return. Default: 50."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=grep_files,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "task_workspace", "root": str(root_path), "coding_agent": True},
            )

        if read and search and not has_action("search_files"):

            async def search_files_action(
                query: str,
                path: str = ".",
                pattern: str = "*",
                max_results: int = 50,
                include_hidden: bool = False,
            ):
                return await current_task_workspace().search_files(
                    query,
                    path=path,
                    pattern=pattern,
                    max_results=max_results,
                    include_hidden=include_hidden,
                    max_file_bytes=max_search_file_bytes,
                )

            self.action.register_action(
                action_id=action_name("search_files"),
                desc=self._build_capability_desc(
                    f"Search UTF-8 text files under the task_workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "query": (str, "Exact text to search for."),
                    "path": (str, "TaskWorkspace-relative directory or file path. Default: '.'."),
                    "pattern": (str, "Glob pattern. Default: '*'."),
                    "max_results": (int, "Maximum matching files to return. Default: 50."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=search_files_action,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "task_workspace", "root": str(root_path)},
            )

        if write and not has_action("write_file"):

            async def write_file(
                path: str,
                content: str,
                append: bool = False,
                expected_sha256: str | None = None,
            ):
                require_fresh_for_write(path, expected_sha256)
                result = await manager_write_file(path, content, append=append)
                remember_write(path, result)
                return file_result_action_output(result)

            self.action.register_action(
                action_id=action_name("write_file"),
                desc=self._build_capability_desc(
                    f"Write a plain text file under the task_workspace root { root_path } through registered TaskWorkspace file IO handlers.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "TaskWorkspace-relative file path."),
                    "content": (str, "Text content to write."),
                    "append": (bool, "Append instead of overwrite. Default: False."),
                    "expected_sha256": (
                        str,
                        "Optional current file sha256. In coding-agent mode, existing files require this or a prior full read.",
                    ),
                },
                func=write_file,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={
                    "component": "task_workspace",
                    "root": str(root_path),
                    "write": True,
                    "_host_approval_required_when": lambda call: task_workspace_mutation_approval("write_file", call),
                },
            )

        if write and coding_agent and not has_action("edit_file"):

            async def edit_file(
                path: str,
                old_string: str,
                new_string: str,
                replace_all: bool = False,
                expected_sha256: str | None = None,
            ):
                return file_result_action_output(
                    await manager_edit_file(
                        path,
                        old_string,
                        new_string,
                        replace_all=replace_all,
                        expected_sha256=expected_sha256,
                    )
                )

            self.action.register_action(
                action_id=action_name("edit_file"),
                desc=self._build_capability_desc(
                    (
                        f"Edit a text file under the task_workspace root { root_path } by exact string replacement. "
                        "Existing files require a prior full read or expected_sha256; ambiguous replacements fail closed."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "TaskWorkspace-relative file path."),
                    "old_string": (str, "Exact string to replace. Use empty string only to create a new file."),
                    "new_string": (str, "Replacement text."),
                    "replace_all": (bool, "Replace every occurrence. Default: False."),
                    "expected_sha256": (str, "Optional current file sha256."),
                },
                func=edit_file,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={
                    "component": "task_workspace",
                    "root": str(root_path),
                    "write": True,
                    "coding_agent": True,
                    "_host_approval_required_when": lambda call: task_workspace_mutation_approval("edit_file", call),
                },
            )

        if write and coding_agent and not has_action("apply_patch"):

            async def apply_patch(patch: str, expected_files: list[str] | None = None):
                return file_result_action_output(await manager_apply_patch(patch, expected_files=expected_files))

            self.action.register_action(
                action_id=action_name("apply_patch"),
                desc=self._build_capability_desc(
                    (
                        f"Apply a unified diff patch under the task_workspace root { root_path }. "
                        "Existing patched files require a prior full read; expected_files must match the patch file set when provided."
                    ),
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "patch": (str, "Unified diff patch to apply."),
                    "expected_files": (
                        [str],
                        "Optional exact list of TaskWorkspace-relative files expected in the patch.",
                    ),
                },
                func=apply_patch,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={
                    "component": "task_workspace",
                    "root": str(root_path),
                    "write": True,
                    "coding_agent": True,
                    "_host_approval_required_when": lambda call: task_workspace_mutation_approval("apply_patch", call),
                },
            )

        if write and export and not has_action("export_file"):

            async def export_file(
                source_path: str,
                output_path: str,
                export_kind: str,
                options: dict[str, Any] | None = None,
            ):
                return file_result_action_output(
                    await manager_export_file(
                        source_path,
                        output_path,
                        export_kind,
                        options=options,
                    )
                )

            self.action.register_action(
                action_id=action_name("export_file"),
                desc=self._build_capability_desc(
                    f"Export a TaskWorkspace file under { root_path } using registered TaskWorkspace file IO handlers.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "source_path": (str, "TaskWorkspace-relative source file path."),
                    "output_path": (str, "TaskWorkspace-relative output file path."),
                    "export_kind": (
                        str,
                        "Export kind such as 'html_pdf', 'markdown_pdf', or 'html_screenshot'.",
                    ),
                    "options": (dict, "Optional handler-specific export options. Default: None."),
                },
                func=export_file,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={"component": "task_workspace", "root": str(root_path), "write": True, "export": True},
            )

        return self

    def enable_coding_agent_actions(
        self,
        *,
        root: str | Path | object = _TASK_WORKSPACE_ROOT_UNSET,
        task_workspace: "TaskWorkspace | None" = None,
        isolated: bool = False,
        read: bool = True,
        write: bool = True,
        search: bool = True,
        list_files: bool = True,
        export: bool = False,
        action_prefix: str = "",
        expose_to_model: bool = True,
        max_file_bytes: int = 20000,
        max_search_file_bytes: int = 200000,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
    ) -> Self:
        default_desc = (
            "Coding-agent TaskWorkspace file actions. Use read_file/glob_files/grep_files for inspection, "
            "edit_file/apply_patch for targeted edits, and write_file only for deliberate full-file writes. "
            "Existing file writes require a prior full read through this action set or expected_sha256."
        )
        return self.enable_task_workspace_file_actions(
            root=root,
            task_workspace=task_workspace,
            isolated=isolated,
            read=read,
            write=write,
            search=search,
            list_files=list_files,
            export=export,
            action_prefix=action_prefix,
            expose_to_model=expose_to_model,
            max_file_bytes=max_file_bytes,
            max_search_file_bytes=max_search_file_bytes,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            desc_mode="append",
            coding_agent=True,
        )

    def set_action_loop(
        self,
        *,
        enabled: bool | None = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: (
            Literal[
                "structured_plan",
                "native_tool_calls",
                "programmatic",
            ]
            | None
        ) = None,
    ) -> Self:
        """Configure the Action loop and its model-planning protocol.

        ``structured_plan`` uses Agently's structured planner,
        ``native_tool_calls`` consumes provider-native tool calls, and
        ``programmatic`` asks an eligible model for executable Action-call code.
        """
        if enabled is not None:
            self.settings.set("action.loop.enabled", bool(enabled))
            self.settings.set("tool.loop.enabled", bool(enabled))
        if max_rounds is not None:
            if not isinstance(max_rounds, int) or max_rounds < 0:
                raise ValueError("max_rounds must be an integer >= 0.")
            self.settings.set("action.loop.max_rounds", max_rounds)
            self.settings.set("tool.loop.max_rounds", max_rounds)
        if concurrency is not None:
            if not isinstance(concurrency, int) or concurrency <= 0:
                raise ValueError("concurrency must be an integer > 0.")
            self.settings.set("action.loop.concurrency", concurrency)
            self.settings.set("tool.loop.concurrency", concurrency)
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("timeout must be a number > 0.")
            self.settings.set("action.loop.timeout", float(timeout))
            self.settings.set("tool.loop.timeout", float(timeout))
        if planning_protocol is not None:
            if planning_protocol not in {
                "structured_plan",
                "native_tool_calls",
                "programmatic",
            }:
                raise ValueError(
                    "planning_protocol must be one of: 'structured_plan', " "'native_tool_calls', 'programmatic'."
                )
            self.settings.set("action.protocol", planning_protocol)
            self.settings.set("tool.protocol", planning_protocol)
        return self

    def set_tool_loop(
        self,
        *,
        enabled: bool | None = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: (
            Literal[
                "structured_plan",
                "native_tool_calls",
                "programmatic",
            ]
            | None
        ) = None,
    ) -> Self:
        return self.set_action_loop(
            enabled=enabled,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
            planning_protocol=planning_protocol,
        )

    def register_action_planning_handler(self, handler: Any) -> Self:
        self.__action_planning_handler = handler
        return self

    def register_tool_plan_analysis_handler(self, handler: Any) -> Self:
        return self.register_action_planning_handler(handler)

    def register_action_execution_handler(self, handler: Any) -> Self:
        self.__action_execution_handler = handler
        return self

    def register_tool_execution_handler(self, handler: Any) -> Self:
        return self.register_action_execution_handler(handler)

    async def async_generate_action_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ActionResult"] | None = None,
        last_round_records: list["ActionResult"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list["ActionCall"]:
        target_prompt = prompt if prompt is not None else self.request.prompt
        action_list = self._get_scoped_action_list()
        return await self.action.async_generate_action_call(
            prompt=target_prompt,
            settings=self.settings,
            action_list=action_list,
            agent_name=self.name,
            planning_handler=self.__action_planning_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

    def generate_action_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ActionResult"] | None = None,
        last_round_records: list["ActionResult"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
        planning_protocol: str | None = None,
    ) -> list["ActionCall"]:
        return default_stage_call_bridge.as_sync(self.async_generate_action_call)(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

    def release_programmatic_action_calls(
        self,
        action_calls: list["ActionCall"] | list[dict[str, Any]],
    ) -> int:
        """Release generated program calls that will not be executed.

        Returns the number of retained program leases released by this call.
        """

        return self.action.release_programmatic_action_calls(action_calls)

    async def async_get_action_result(
        self,
        prompt: "Prompt | None" = None,
        *,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
        store_for_reply: bool = True,
    ) -> list["ActionResult"]:
        target_prompt = prompt if prompt is not None else self.request.prompt
        action_list = self._get_scoped_action_list()
        if len(action_list) == 0:
            return []

        records = await self.action.async_plan_and_execute(
            prompt=target_prompt,
            settings=self.settings,
            action_list=action_list,
            agent_name=self.name,
            planning_handler=self.__action_planning_handler,
            action_execution_handler=self.__action_execution_handler,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
            planning_protocol=planning_protocol,
        )
        if store_for_reply:
            action_results = self.action.to_action_results(records)
            target_prompt.set("action_results", action_results)
            if len(records) > 0:
                target_prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
            self.__action_logs = records
            self.__prepared_action_results = action_results
        return records

    def get_action_result(
        self,
        prompt: "Prompt | None" = None,
        *,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
        planning_protocol: str | None = None,
        store_for_reply: bool = True,
    ) -> list["ActionResult"]:
        return default_stage_call_bridge.as_sync(self.async_get_action_result)(
            prompt=prompt,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
            planning_protocol=planning_protocol,
            store_for_reply=store_for_reply,
        )

    async def async_generate_tool_command(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        target_prompt = prompt if prompt is not None else self.request.prompt
        tool_list = self.tool.get_tool_list(tags=[f"agent-{ self.name }"])
        return await self.tool.async_generate_tool_command(
            prompt=target_prompt,
            settings=self.settings,
            tool_list=tool_list,
            agent_name=self.name,
            plan_analysis_handler=self.__action_planning_handler,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    def generate_tool_command(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        return default_stage_call_bridge.as_sync(self.async_generate_tool_command)(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    async def async_must_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        DeprecationWarnings.warn_deprecated_once(
            "ActionExtension.async_must_call",
            "Method .async_must_call() is deprecated and will be removed in future version, "
            "please use .async_generate_tool_command() instead.",
            stacklevel=2,
        )
        return await self.async_generate_tool_command(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    def must_call(
        self,
        prompt: "Prompt | None" = None,
        *,
        done_plans: list["ToolExecutionRecord"] | None = None,
        last_round_records: list["ToolExecutionRecord"] | None = None,
        round_index: int = 0,
        max_rounds: int | None = None,
    ) -> list["ToolCommand"]:
        DeprecationWarnings.warn_deprecated_once(
            "ActionExtension.must_call",
            "Method .must_call() is deprecated and will be removed in future version, "
            "please use .generate_tool_command() instead.",
            stacklevel=2,
        )
        return self.generate_tool_command(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
        )

    async def __request_prefix(self, prompt: "Prompt", _settings):
        settings = _settings if _settings is not None else self.settings
        missing = object()
        existing_action_results = prompt.get("action_results", default=missing)
        if existing_action_results is not missing:
            if self.__prepared_action_results is not None and existing_action_results == self.__prepared_action_results:
                self.__prepared_action_results = None
            else:
                self.__action_logs = []
            has_action_results = bool(existing_action_results)
            if has_action_results and prompt.get("extra_instruction", default=missing) is missing:
                prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
            return

        self.__action_logs = []
        if settings.get("action.loop.enabled", settings.get("tool.loop.enabled", True)) is not True:
            return

        if settings.get("$agent_execution.ensure_long_output", False) is True:
            return

        action_list = self._get_scoped_action_list()
        if len(action_list) == 0:
            return

        prepared_response = PreparedModelResponse()

        async def response_generator():
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
            terminal_decision: dict[str, Any] = {}
            streamed_response_parts: list[str] = []

            async def response_stream_handler(event: str, data: Any) -> None:
                if event == "status" and isinstance(data, Mapping) and data.get("retry"):
                    streamed_response_parts.clear()
                elif event == "delta" and isinstance(data, str):
                    streamed_response_parts.append(data)
                await queue.put((event, data))

            async def terminal_response_handler(decision: dict[str, Any]) -> None:
                terminal_decision.clear()
                terminal_decision.update(decision)

            async def run_action_loop() -> None:
                try:
                    records = await self.action.async_plan_and_execute(
                        prompt=prompt,
                        settings=settings,
                        action_list=action_list,
                        agent_name=self.name,
                        planning_handler=self.__action_planning_handler,
                        action_execution_handler=self.__action_execution_handler,
                        max_rounds=settings.get(  # type: ignore[arg-type]
                            "action.loop.max_rounds",
                            settings.get("tool.loop.max_rounds", None),
                        ),
                        concurrency=settings.get(  # type: ignore[arg-type]
                            "action.loop.concurrency",
                            settings.get("tool.loop.concurrency", None),
                        ),
                        timeout=settings.get(  # type: ignore[arg-type]
                            "action.loop.timeout",
                            settings.get("tool.loop.timeout", None),
                        ),
                        response_stream_handler=response_stream_handler,
                        terminal_response_handler=terminal_response_handler,
                    )
                except BaseException as error:
                    await queue.put(("$action_loop_error", error))
                    return
                await queue.put(("$action_loop_done", records))

            action_loop_task = asyncio.create_task(run_action_loop())
            try:
                while True:
                    event, data = await queue.get()
                    if event == "$action_loop_error":
                        raise data
                    if event == "$action_loop_done":
                        records = data if isinstance(data, list) else []
                        action_results = self.action.to_action_results(records)
                        prompt.set("action_results", action_results)
                        if records:
                            prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
                        self.__action_logs = records
                        self.__prepared_action_results = action_results

                        response = terminal_decision.get("response")
                        if isinstance(response, str) and response.strip():
                            streamed_response = "".join(streamed_response_parts)
                            if not streamed_response:
                                yield "delta", response
                            elif response.startswith(streamed_response):
                                remainder = response[len(streamed_response) :]
                                if remainder:
                                    yield "delta", remainder
                            elif streamed_response != response:
                                yield "status", {
                                    "status": "failed",
                                    "retry": True,
                                    "reason": "Replacing a provisional Action response with the accepted response.",
                                }
                                yield "delta", response
                            prepared_response.mark_handled(
                                delivery="action_response_direct",
                                planning_protocol=settings.get(
                                    "action.protocol",
                                    settings.get("tool.protocol", "structured_plan"),
                                ),
                            )
                            yield "meta", {
                                "action_response_delivery": "direct",
                                "action_response_fallback": False,
                            }
                            yield "status", {"status": "completed", "retry": False}
                            yield "done", response
                        else:
                            prepared_response.meta.update(
                                {
                                    "delivery": "final_request_fallback",
                                    "reason": "terminal_response_unavailable",
                                }
                            )
                        break
                    yield event, data
            finally:
                if not action_loop_task.done():
                    action_loop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await action_loop_task

        prepared_response.set_generator(response_generator())
        return prepared_response

    async def __broadcast_prefix(self, full_result_data: "AgentlyModelResult", _):
        if len(self.__action_logs) == 0:
            return

        tool_logs = [log for log in self.__action_logs if log.get("expose_to_model", True)]

        for action_log in self.__action_logs:
            yield "action", action_log
        for tool_log in tool_logs:
            yield "tool", tool_log

        if "extra" not in full_result_data:
            full_result_data["extra"] = {}
        if isinstance(full_result_data["extra"], dict) and "action_logs" not in full_result_data["extra"]:
            full_result_data["extra"]["action_logs"] = []
        if isinstance(full_result_data["extra"], dict) and "tool_logs" not in full_result_data["extra"]:
            full_result_data["extra"]["tool_logs"] = []
        if (
            "extra" in full_result_data
            and isinstance(full_result_data["extra"], dict)
            and isinstance(full_result_data["extra"].get("action_logs"), list)
        ):
            full_result_data["extra"]["action_logs"].extend(self.__action_logs)
        if (
            "extra" in full_result_data
            and isinstance(full_result_data["extra"], dict)
            and isinstance(full_result_data["extra"].get("tool_logs"), list)
        ):
            full_result_data["extra"]["tool_logs"].extend(tool_logs)
        self.__action_logs = []
