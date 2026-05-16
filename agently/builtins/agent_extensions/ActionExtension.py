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

import warnings
from pathlib import Path
from typing import Any, Callable, Literal, TYPE_CHECKING, ParamSpec, TypeAlias, TypeVar

from agently.core import BaseAgent
from agently.utils import FunctionShifter

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.core.Tool import ToolCommand, ToolExecutionRecord
    from agently.types.data import ActionCall, ActionResult, AgentlyModelResult, KwargsType, MCPConfigs, ReturnType

from agently.base import action as global_action

P = ParamSpec("P")
R = TypeVar("R")
CapabilityDescMode: TypeAlias = Literal["append", "override", "default"]


class ActionExtension(BaseAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.action = type(global_action)(self.plugin_manager, self.settings)
        self.tool = self.action

        self.use_action = self.use_actions
        self.use_tool = self.use_tools
        self.use_mcp = FunctionShifter.syncify(self.async_use_mcp)
        self.use_sandbox = self.use_action_sandbox
        self.use_python = self.enable_python
        self.use_shell = self.enable_shell
        self.use_nodejs = self.enable_nodejs
        self.use_sqlite = self.enable_sqlite
        self.use_docker = self.enable_docker
        self.use_workspace = self.enable_workspace

        self.settings.setdefault("action.loop.max_rounds", 5, inherit=True)
        self.settings.setdefault("action.loop.concurrency", None, inherit=True)
        self.settings.setdefault("action.loop.timeout", None, inherit=True)
        self.settings.setdefault("action.loop.enabled", True, inherit=True)
        self.settings.setdefault("tool.loop.max_rounds", 5, inherit=True)
        self.settings.setdefault("tool.loop.concurrency", None, inherit=True)
        self.settings.setdefault("tool.loop.timeout", None, inherit=True)
        self.settings.setdefault("tool.loop.enabled", True, inherit=True)
        self.settings.setdefault("execution_environment.owner_id", self.name, inherit=False)

        self.__action_logs: list[ActionResult] = []
        self.__prepared_action_results: dict[str, Any] | None = None
        self.__action_planning_handler = None
        self.__action_execution_handler = None

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
        if isinstance(copied_spec.get("execution_environments"), list):
            copied_spec["execution_environments"] = [dict(item) for item in copied_spec["execution_environments"]]

        self.action.register_action(
            action_id=str(copied_spec.get("action_id", action_id)),
            desc=str(copied_spec.get("desc", "")),
            kwargs=copied_spec.get("kwargs", {}),
            func=global_registry.get_func(action_id),
            executor=executor,
            returns=copied_spec.get("returns"),
            tags=copied_spec.get("tags", []),
            default_policy=copied_spec.get("default_policy", {}),
            side_effect_level=copied_spec.get("side_effect_level", "read"),
            approval_required=bool(copied_spec.get("approval_required", False)),
            sandbox_required=bool(copied_spec.get("sandbox_required", False)),
            replay_safe=bool(copied_spec.get("replay_safe", True)),
            expose_to_model=bool(copied_spec.get("expose_to_model", True)),
            execution_environments=copied_spec.get("execution_environments", []),
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
    ):
        self.action.register_action(
            action_id=name,
            desc=desc,
            kwargs=kwargs,
            func=func,
            tags=[f"agent-{ self.name }"],
            returns=returns,
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
    ):
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

    def use_actions(self, actions: Callable | str | list[str | Callable] | Any):
        names: list[str] = []
        local_registry = getattr(self.action, "action_registry", None)
        agent_tag = f"agent-{ self.name }"
        for action_item in self._normalize_action_items(actions):
            register_actions = getattr(action_item, "register_actions", None)
            if callable(register_actions):
                names.extend(self._normalize_registered_action_ids(register_actions(self.action, tags=[agent_tag])))
                continue
            if isinstance(action_item, str):
                self.__import_global_action(action_item)
                names.append(action_item)
            else:
                action_name = getattr(action_item, "__name__", "")
                if not action_name:
                    raise TypeError("use_actions() expects action names, callables, or built-in action packages.")
                if action_name not in self.action.tool_funcs and (local_registry is None or not local_registry.has(action_name)):
                    self.action_func(action_item)
                names.append(action_name)
        if names:
            self.action.tag(names, agent_tag)
        return self

    def use_tools(self, tools: Callable | str | list[str | Callable] | Any):
        return self.use_actions(tools)

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

    async def async_use_mcp(self, transport: "MCPConfigs | str | Any"):
        await self.action.async_use_mcp(transport, tags=[f"agent-{ self.name }"])
        return self

    def use_action_sandbox(
        self,
        sandbox: str,
        *,
        action_id: str | None = None,
        expose_to_model: bool = True,
        **kwargs,
    ):
        sandbox_name = sandbox.strip().lower() if isinstance(sandbox, str) else ""
        if sandbox_name in {"python", "python_sandbox"}:
            resolved_action_id = action_id or "python_sandbox"
            self.action.register_python_sandbox_action(
                action_id=resolved_action_id,
                tags=[f"agent-{ self.name }"],
                expose_to_model=expose_to_model,
                **kwargs,
            )
            return self
        if sandbox_name in {"bash", "shell", "bash_sandbox"}:
            resolved_action_id = action_id or "bash_sandbox"
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
    ):
        default_desc = (
            "Run Python code in a managed safe sandbox for deterministic calculation "
            "or small data shaping. Assign the final value to `result`."
        )
        return self.use_action_sandbox(
            "python",
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            expose_to_model=expose_to_model,
            preset_objects=preset_objects,
            base_vars=base_vars,
            allowed_return_types=allowed_return_types,
        )

    def enable_shell(
        self,
        *,
        root: str | Path | None = None,
        commands: list[str] | None = None,
        action_id: str = "run_bash",
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
        expose_to_model: bool = True,
        timeout: int = 20,
        env: dict[str, str] | None = None,
    ):
        roots = [str(Path(root).expanduser().resolve())] if root is not None else None
        default_desc = "Run an allowlisted shell command inside a managed workspace boundary."
        return self.use_action_sandbox(
            "bash",
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            expose_to_model=expose_to_model,
            allowed_cmd_prefixes=commands,
            allowed_workdir_roots=roots,
            timeout=timeout,
            env=env,
        )

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
    ):
        default_desc = "Run JavaScript with Node.js inside a managed execution environment."
        self.action.register_nodejs_action(
            action_id=action_id,
            desc=self._build_capability_desc(default_desc, desc, mode=desc_mode),
            tags=[f"agent-{ self.name }"],
            expose_to_model=expose_to_model,
            node_binary=node_binary,
            cwd=cwd,
            timeout=timeout,
            env=env,
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
    ):
        default_desc = "Query a SQLite database through a managed execution environment."
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
    ):
        default_desc = "Run a command in a Docker container through a managed execution environment."
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

    def enable_workspace(
        self,
        *,
        root: str | Path = ".",
        read: bool = True,
        write: bool = False,
        search: bool = True,
        list_files: bool = True,
        action_prefix: str = "",
        expose_to_model: bool = True,
        max_file_bytes: int = 20000,
        max_search_file_bytes: int = 200000,
        desc: str | None = None,
        desc_mode: CapabilityDescMode = "append",
    ):
        root_path = Path(root).expanduser().resolve()
        agent_tag = f"agent-{ self.name }"
        prefix = action_prefix.strip()

        def action_name(name: str):
            return f"{ prefix }{ name }" if prefix else name

        def resolve_workspace_path(path: str | Path = "."):
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = root_path / candidate
            resolved = candidate.expanduser().resolve()
            try:
                resolved.relative_to(root_path)
            except ValueError as error:
                raise ValueError(f"Path is outside workspace root: { path }") from error
            return resolved

        def is_hidden(path: Path):
            try:
                relative_parts = path.relative_to(root_path).parts
            except ValueError:
                return True
            return any(part.startswith(".") for part in relative_parts)

        def iter_workspace_files(
            path: str = ".",
            pattern: str = "*",
            max_results: int = 200,
            include_hidden: bool = False,
        ):
            base = resolve_workspace_path(path)
            if base.is_file():
                candidates = [base]
            elif base.exists():
                candidates = base.rglob(pattern)
            else:
                candidates = []
            collected: list[Path] = []
            for candidate in candidates:
                if len(collected) >= max_results:
                    break
                if not candidate.is_file():
                    continue
                if not include_hidden and is_hidden(candidate):
                    continue
                collected.append(candidate)
            return collected

        if read and list_files:

            def list_workspace_files(
                path: str = ".",
                pattern: str = "*",
                max_results: int = 200,
                include_hidden: bool = False,
            ):
                files = iter_workspace_files(path, pattern, max_results, include_hidden)
                return [str(file.relative_to(root_path)) for file in files]

            self.action.register_action(
                action_id=action_name("list_files"),
                desc=self._build_capability_desc(
                    f"List files under the workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "Workspace-relative directory or file path. Default: '.'."),
                    "pattern": (str, "Glob pattern. Default: '*'."),
                    "max_results": (int, "Maximum files to return. Default: 200."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=list_workspace_files,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path)},
            )

        if read:

            def read_file(path: str, max_bytes: int = max_file_bytes):
                target = resolve_workspace_path(path)
                if not target.is_file():
                    raise FileNotFoundError(f"Workspace file not found: { path }")
                content_bytes = target.read_bytes()
                truncated = len(content_bytes) > max_bytes
                content = content_bytes[:max_bytes].decode("utf-8", errors="replace")
                return {
                    "path": str(target.relative_to(root_path)),
                    "content": content,
                    "truncated": truncated,
                    "bytes": len(content_bytes),
                }

            self.action.register_action(
                action_id=action_name("read_file"),
                desc=self._build_capability_desc(
                    f"Read a UTF-8 text file under the workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "Workspace-relative file path."),
                    "max_bytes": (int, f"Maximum bytes to read. Default: { max_file_bytes }."),
                },
                func=read_file,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path)},
            )

        if read and search:

            def search_files_action(
                query: str,
                path: str = ".",
                pattern: str = "*",
                max_results: int = 50,
                include_hidden: bool = False,
            ):
                results: list[dict[str, Any]] = []
                files = iter_workspace_files(path, pattern, max_results=1000, include_hidden=include_hidden)
                for file in files:
                    if len(results) >= max_results:
                        break
                    try:
                        content_bytes = file.read_bytes()
                    except OSError:
                        continue
                    if len(content_bytes) > max_search_file_bytes:
                        continue
                    text = content_bytes.decode("utf-8", errors="ignore")
                    for line_no, line in enumerate(text.splitlines(), start=1):
                        if query in line:
                            results.append(
                                {
                                    "path": str(file.relative_to(root_path)),
                                    "line": line_no,
                                    "text": line,
                                }
                            )
                            break
                return results

            self.action.register_action(
                action_id=action_name("search_files"),
                desc=self._build_capability_desc(
                    f"Search UTF-8 text files under the workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "query": (str, "Exact text to search for."),
                    "path": (str, "Workspace-relative directory or file path. Default: '.'."),
                    "pattern": (str, "Glob pattern. Default: '*'."),
                    "max_results": (int, "Maximum matching files to return. Default: 50."),
                    "include_hidden": (bool, "Whether to include hidden paths. Default: False."),
                },
                func=search_files_action,
                tags=[agent_tag],
                side_effect_level="read",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path)},
            )

        if write:

            def write_file(path: str, content: str, append: bool = False):
                target = resolve_workspace_path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                if append:
                    with target.open("a", encoding="utf-8") as file:
                        file.write(content)
                else:
                    target.write_text(content, encoding="utf-8")
                return {
                    "path": str(target.relative_to(root_path)),
                    "bytes": len(content.encode("utf-8")),
                    "mode": "append" if append else "write",
                }

            self.action.register_action(
                action_id=action_name("write_file"),
                desc=self._build_capability_desc(
                    f"Write a UTF-8 text file under the workspace root { root_path }.",
                    desc,
                    mode=desc_mode,
                ),
                kwargs={
                    "path": (str, "Workspace-relative file path."),
                    "content": (str, "Text content to write."),
                    "append": (bool, "Append instead of overwrite. Default: False."),
                },
                func=write_file,
                tags=[agent_tag],
                side_effect_level="write",
                expose_to_model=expose_to_model,
                meta={"component": "workspace", "root": str(root_path), "write": True},
            )

        return self

    def set_action_loop(
        self,
        *,
        enabled: bool | None = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ):
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
        return self

    def set_tool_loop(
        self,
        *,
        enabled: bool | None = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        timeout: float | None = None,
    ):
        return self.set_action_loop(
            enabled=enabled,
            max_rounds=max_rounds,
            concurrency=concurrency,
            timeout=timeout,
        )

    def register_action_planning_handler(self, handler):
        self.__action_planning_handler = handler
        return self

    def register_tool_plan_analysis_handler(self, handler):
        return self.register_action_planning_handler(handler)

    def register_action_execution_handler(self, handler):
        self.__action_execution_handler = handler
        return self

    def register_tool_execution_handler(self, handler):
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
        action_list = self.action.get_action_list(tags=[f"agent-{ self.name }"])
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
        return FunctionShifter.syncify(self.async_generate_action_call)(
            prompt=prompt,
            done_plans=done_plans,
            last_round_records=last_round_records,
            round_index=round_index,
            max_rounds=max_rounds,
            planning_protocol=planning_protocol,
        )

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
        action_list = self.action.get_action_list(tags=[f"agent-{ self.name }"])
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
        if store_for_reply and len(records) > 0:
            action_results = self.action.to_action_results(records)
            target_prompt.set("action_results", action_results)
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
        return FunctionShifter.syncify(self.async_get_action_result)(
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
        return FunctionShifter.syncify(self.async_generate_tool_command)(
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
        warnings.warn(
            "Method .async_must_call() is deprecated and will be removed in future version, "
            "please use .async_generate_tool_command() instead.",
            DeprecationWarning,
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
        warnings.warn(
            "Method .must_call() is deprecated and will be removed in future version, "
            "please use .generate_tool_command() instead.",
            DeprecationWarning,
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
            if prompt.get("extra_instruction", default=missing) is missing:
                prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
            return

        self.__action_logs = []
        if settings.get("action.loop.enabled", settings.get("tool.loop.enabled", True)) is not True:
            return

        action_list = self.action.get_action_list(tags=[f"agent-{ self.name }"])
        if len(action_list) == 0:
            return

        records = await self.action.async_plan_and_execute(
            prompt=prompt,
            settings=settings,
            action_list=action_list,
            agent_name=self.name,
            planning_handler=self.__action_planning_handler,
            action_execution_handler=self.__action_execution_handler,
            max_rounds=settings.get("action.loop.max_rounds", settings.get("tool.loop.max_rounds", 5)),  # type: ignore[arg-type]
            concurrency=settings.get("action.loop.concurrency", settings.get("tool.loop.concurrency", None)),  # type: ignore[arg-type]
            timeout=settings.get("action.loop.timeout", settings.get("tool.loop.timeout", None)),  # type: ignore[arg-type]
        )

        if len(records) > 0:
            prompt.set("action_results", self.action.to_action_results(records))
            prompt.set("extra_instruction", self.action.ACTION_RESULT_QUOTE_NOTICE)
            self.__action_logs = records

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
