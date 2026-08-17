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

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Callable, Coroutine, Literal, ParamSpec, TypeVar, cast, get_args, get_origin, get_type_hints

from agently.core.operation.Action import ActionDispatcher, ActionRegistry
from agently.core.operation.Action.ActionMetadata import sanitize_action_spec_for_metadata, summarize_action_records
from agently.types.plugins import ToolManager
from agently.utils import DataFormatter, DeprecationWarnings, LazyImport, SettingsNamespace
from agently.utils.MCP import normalize_mcp_transport
from agently.types.data import ActionSpec

if TYPE_CHECKING:
    from agently.types.data import ActionPolicy, MCPConfigs, KwargsType, ReturnType
    from agently.utils import Settings

P = ParamSpec("P")
R = TypeVar("R")


class AgentlyToolManager(ToolManager):
    name = "AgentlyToolManager"

    DEFAULT_SETTINGS = {}

    def __init__(self, settings: "Settings"):
        DeprecationWarnings.warn_deprecated_once(
            "AgentlyToolManager.__init__",
            "AgentlyToolManager is deprecated as an internal runtime layer. Use Action directly; "
            "`tool` APIs remain public surface aliases.",
            stacklevel=2,
        )
        from agently.base import plugin_manager

        self.settings = settings
        self.plugin_manager = plugin_manager
        self.plugin_settings = SettingsNamespace(self.settings, f"plugins.ToolManager.{ self.name }")

        self.action_registry = ActionRegistry(name=f"{ self.name }-ActionRegistry")
        self.action_dispatcher = ActionDispatcher(self.action_registry, self.settings)

        self.action_funcs: dict[str, Callable[..., Any]] = {}
        self.tool_funcs = self.action_funcs

        self.use_action_mcp = default_stage_call_bridge.as_sync(self.async_use_action_mcp)
        self.use_mcp = default_stage_call_bridge.as_sync(self.async_use_mcp)

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

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

    def tag(self, tool_names: str | list[str], tags: str | list[str]):
        self.action_registry.tag(tool_names, tags)
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
            action_info[action_id] = sanitize_action_spec_for_metadata(spec)
        return action_info

    def summarize_records(
        self,
        records: list[dict[str, Any]] | None,
        *,
        validation_command_markers: list[str] | tuple[str, ...] | None = None,
        validation_command_predicate: Callable[[str, dict[str, Any]], bool] | None = None,
    ):
        return summarize_action_records(
            records,
            validation_command_markers=validation_command_markers,
            validation_command_predicate=validation_command_predicate,
        )

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
                return default_stage_call_bridge.as_sync(action_func)
            case "async":
                return default_stage_call_bridge.as_async(action_func)
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
        return await self.action_dispatcher.async_execute(
            name,
            kwargs,
            settings=settings,
            purpose=purpose,
            policy_override=policy_override,
            source_protocol=source_protocol,
            todo_suggestion=todo_suggestion,
            next_value=next_value,
        )

    def execute_action(self, name: str, kwargs: dict[str, Any], **kwargs_options):
        return self.action_dispatcher.execute(name, kwargs, **kwargs_options)

    async def async_call_action(self, name: str, kwargs: dict[str, Any]) -> Any:
        if not self.action_registry.has(name):
            return self._legacy_error(name, as_tool=False)
        result = await self.async_execute_action(name, kwargs)
        return self._legacy_result(result, as_tool=False)

    def call_action(self, name: str, kwargs: dict[str, Any]) -> Any:
        return default_stage_call_bridge.as_sync(self.async_call_action)(name, kwargs)

    async def async_call_tool(self, name: str, kwargs: dict[str, Any]) -> Any:
        if not self.action_registry.has(name):
            return self._legacy_error(name, as_tool=True)
        result = await self.async_execute_action(name, kwargs)
        return self._legacy_result(result, as_tool=True)

    def call_tool(self, name: str, kwargs: dict[str, Any]) -> Any:
        return default_stage_call_bridge.as_sync(self.async_call_tool)(name, kwargs)

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
        LazyImport.import_package("fastmcp", version_constraint=">=3", auto_install=False)
        from fastmcp import Client

        transport = normalize_mcp_transport(transport, headers=headers)
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
                )
        return self

    async def async_use_mcp(
        self,
        transport: "MCPConfigs | str | Any",
        *,
        headers: dict[str, str] | None = None,
        tags: str | list[str] | None = None,
    ):
        await self.async_use_action_mcp(transport, headers=headers, tags=tags)
        return self

    @staticmethod
    def _format_bash_sandbox_desc(
        desc: str,
        *,
        allowed_cmd_prefixes: list[str] | None,
        allowed_workdir_roots: list[str | Path] | None,
        timeout: int,
    ) -> str:
        command_text = (
            ", ".join(str(prefix) for prefix in allowed_cmd_prefixes)
            if allowed_cmd_prefixes
            else "no command allowlist configured"
        )
        roots_text = (
            ", ".join(str(Path(root)) for root in allowed_workdir_roots)
            if allowed_workdir_roots
            else "current working directory only"
        )
        policy_desc = (
            "Pass exactly one command: use a string, an argv token list, or a one-item list "
            "containing the complete command; Agently parses it to argv and never invokes a shell. "
            f"Allowed command prefixes: {command_text}. "
            f"Allowed working directory roots: {roots_text}. "
            f"Timeout: {timeout} seconds."
        )
        base_desc = str(desc).strip()
        return f"{base_desc}\n\n{policy_desc}" if base_desc else policy_desc

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
    ):
        model_desc = self._format_bash_sandbox_desc(
            desc,
            allowed_cmd_prefixes=allowed_cmd_prefixes,
            allowed_workdir_roots=allowed_workdir_roots,
            timeout=timeout,
        )
        self.register_action(
            action_id=action_id,
            desc=model_desc,
            kwargs={
                "cmd": (
                    "str | list[str]",
                    "Exactly one command: a command string, argv tokens, or a one-item list containing the complete command.",
                ),
                "workdir": ("str | None", "Working directory inside allowed roots."),
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
        )
        return self
