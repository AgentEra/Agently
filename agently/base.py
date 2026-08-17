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

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Type, TYPE_CHECKING, TypeVar, Generic, cast

from agently.builtins.hookers.RuntimeConsoleSinkHooker import coerce_runtime_log_profile
from agently._version import __version__ as package_version
from agently.utils import DeprecationWarnings, LazyImport, Settings, create_logger
from agently.utils.RequestScheduler import RequestScheduler
from agently.core import (
    Action,
    DynamicTask,
    ExecutionExchangeManager,
    ExecutionResourceManager,
    PolicyApprovalManager,
    PluginManager,
    EventCenter,
    Tool,
    TriggerFlow,
    Prompt,
    ModelRequest,
    BaseAgent,
    Blocks,
    SkillsExecutor,
    SkillLibrary,
)
from agently.core.storage import RecordStoreRegistry
from agently._default_init import (
    _load_default_actions,
    _load_default_settings,
    _load_default_plugins,
    _hook_default_event_handlers,
)

if TYPE_CHECKING:
    from agently.types.data import RuntimeEventLevel, SerializableValue, TaskDAG
    from agently.builtins.hookers.RuntimeConsoleSinkHooker import RuntimeLogProfile

# Basic Initialize

_SETTINGS_VALUE_UNSET = object()

settings: Settings = Settings(
    name="global_settings",
)
_load_default_settings(settings)
plugin_manager: PluginManager = PluginManager(
    settings,
    name="global_plugin_manager",
)
_load_default_plugins(plugin_manager)
event_center: EventCenter = EventCenter()
_hook_default_event_handlers(event_center)
async_emit_observation: Any = event_center.async_emit
emit_observation: Any = event_center.emit
async_emit_runtime: Any = event_center.async_emit
emit_runtime: Any = event_center.emit
logger: Any = create_logger()
httpx_level_name = settings.get("runtime.httpx_log_level", "WARNING")
httpx_level = getattr(logging, str(httpx_level_name).upper(), logging.WARNING)
logging.getLogger("httpx").setLevel(httpx_level)
logging.getLogger("httpcore").setLevel(httpx_level)
action: Action = Action(plugin_manager, settings)
tool: Action = action
execution_resource: ExecutionResourceManager = ExecutionResourceManager(
    plugin_manager=plugin_manager,
    settings=settings,
    event_center=event_center,
)
policy_approval: PolicyApprovalManager = PolicyApprovalManager(
    settings=settings,
    event_center=event_center,
)
execution_exchange: ExecutionExchangeManager = ExecutionExchangeManager(
    settings=settings,
    event_center=event_center,
)
request_scheduler: RequestScheduler = RequestScheduler()
action_registry: Any = action.action_registry
_load_default_actions(action_registry)
action_dispatcher: Any = action.action_dispatcher
action_runtime: Any = action.action_runtime
action_flow: Any = action.action_flow
skill_library: SkillLibrary = SkillLibrary(
    str(settings.get("skills.library.root", ".agently/skill-library")),
    plugin_manager=plugin_manager,
)
skills_executor: SkillsExecutor = SkillsExecutor(
    plugin_manager,
    settings,
    library=skill_library,
)
blocks: Blocks = Blocks(plugin_manager, settings)
# Private persistence construction registry. It is not part of the Agently
# application facade and will be replaced by concrete store ports.
record_store_registry: RecordStoreRegistry = RecordStoreRegistry()
_agently_emitter: Any = event_center.create_emitter("Agently")


def print_(content: Any, *args: Any) -> None:
    contents = [str(content)]
    if args:
        for arg in args:
            contents.append(str(arg))
    content_text = " ".join(contents)
    _agently_emitter.info(content_text, event_type="runtime.print")


async def async_print(content: Any, *args: Any) -> None:
    contents = [str(content)]
    if args:
        for arg in args:
            contents.append(str(arg))
    content_text = " ".join(contents)
    await _agently_emitter.async_info(content_text, event_type="runtime.print")


def _apply_debug_profile(
    target_settings: Settings,
    value: "SerializableValue",
    *,
    auto_load_env: bool = False,
    raise_empty: bool = False,
) -> "RuntimeLogProfile":
    if auto_load_env:
        value = Settings._substitute_env_placeholder(value, raise_empty=raise_empty)
    normalized = coerce_runtime_log_profile(value)
    target_settings.set_settings("debug", normalized)
    target_settings.set("debug", normalized)
    return normalized


# Settings Mappings

settings.update_mappings(
    {
        "path_mappings": {
            "agently_api_key": "agently.api_key",
        },
        "key_value_mappings": {
            "debug": {
                "simple": {
                    "runtime.show_model_logs": "simple",
                    "runtime.show_action_logs": "simple",
                    "runtime.show_tool_logs": "simple",
                    "runtime.show_trigger_flow_logs": "simple",
                    "runtime.show_runtime_logs": "simple",
                    "runtime.httpx_log_level": "WARNING",
                },
                "detail": {
                    "runtime.show_model_logs": "detail",
                    "runtime.show_action_logs": "detail",
                    "runtime.show_tool_logs": "detail",
                    "runtime.show_trigger_flow_logs": "detail",
                    "runtime.show_runtime_logs": "detail",
                    "runtime.httpx_log_level": "INFO",
                },
                "off": {
                    "runtime.show_model_logs": "off",
                    "runtime.show_action_logs": "off",
                    "runtime.show_tool_logs": "off",
                    "runtime.show_trigger_flow_logs": "off",
                    "runtime.show_runtime_logs": "off",
                    "runtime.httpx_log_level": "WARNING",
                },
                True: {
                    "runtime.show_model_logs": "simple",
                    "runtime.show_action_logs": "simple",
                    "runtime.show_tool_logs": "simple",
                    "runtime.show_trigger_flow_logs": "simple",
                    "runtime.show_runtime_logs": "simple",
                    "runtime.httpx_log_level": "WARNING",
                },
                False: {
                    "runtime.show_model_logs": "off",
                    "runtime.show_action_logs": "off",
                    "runtime.show_tool_logs": "off",
                    "runtime.show_trigger_flow_logs": "off",
                    "runtime.show_runtime_logs": "off",
                    "runtime.httpx_log_level": "WARNING",
                },
            }
        },
    }
)

if settings.get("debug", None) is not None:
    _apply_debug_profile(settings, settings.get("debug"))

# Extensions Installation
# BaseAgent + Extensions = Agent
from agently.builtins.agent_extensions import (
    StreamingPrintExtension,
    SessionExtension,
    TaskWorkspaceExtension,
    RecordStoreExtension,
    ActionExtension,
    SkillsExtension,
    KeyWaiterExtension,
    AutoFuncExtension,
    ConfigurePromptExtension,
)


class Agent(
    StreamingPrintExtension,
    SessionExtension,
    SkillsExtension,
    TaskWorkspaceExtension,
    RecordStoreExtension,
    ActionExtension,
    KeyWaiterExtension,
    AutoFuncExtension,
    ConfigurePromptExtension,
    BaseAgent,
):
    def __init__(
        self,
        *args: Any,
        plugin_manager_: PluginManager | None = None,
        parent_settings: Settings | None = None,
        name: str | None = None,
    ):
        if len(args) > 1:
            raise TypeError("Agent(...) accepts at most one positional argument: name or plugin manager.")
        if args:
            first = args[0]
            if isinstance(first, str) or first is None:
                name = first
            elif plugin_manager_ is None:
                plugin_manager_ = cast(PluginManager, first)
            else:
                raise TypeError("Agent(...) received both positional and keyword plugin managers.")
        super().__init__(
            plugin_manager_ or plugin_manager,
            parent_settings=parent_settings if parent_settings is not None else settings,
            name=name,
        )


A = TypeVar("A", bound=Agent)

# Agently Main


class AgentlyMain(Generic[A]):
    __version__ = package_version

    def __init__(self, AgentType: Type[A] = Agent):
        self.settings = settings
        self.plugin_manager = plugin_manager
        self.event_center = event_center
        self.emit_observation = emit_observation
        self.async_emit_observation = async_emit_observation
        self.emit_runtime = emit_runtime
        self.async_emit_runtime = async_emit_runtime
        self.logger = logger
        self.print = print_
        self.async_print = async_print
        self.action = action
        self.tool = tool
        self.execution_resource = execution_resource
        self.policy_approval = policy_approval
        self.action_registry = action_registry
        self.action_dispatcher = action_dispatcher
        self.action_runtime = action_runtime
        self.action_flow = action_flow
        self.skills_executor = skills_executor
        self.skill_library = skill_library
        self.blocks = blocks
        self.AgentType = AgentType

        def refresh_httpx_log_level() -> None:
            level_name = self.settings.get("runtime.httpx_log_level", "WARNING")
            level = getattr(logging, str(level_name).upper(), logging.WARNING)
            logging.getLogger("httpx").setLevel(level)
            logging.getLogger("httpcore").setLevel(level)

        def set_settings(
            key: Any,
            value: "SerializableValue | object" = _SETTINGS_VALUE_UNSET,
            *,
            auto_load_env: bool = False,
            raise_empty: bool = False,
        ) -> "AgentlyMain[A]":
            if isinstance(key, str) and key == "debug" and value is not _SETTINGS_VALUE_UNSET:
                _apply_debug_profile(
                    self.settings,
                    cast("SerializableValue", value),
                    auto_load_env=auto_load_env,
                    raise_empty=raise_empty,
                )
            else:
                if value is _SETTINGS_VALUE_UNSET:
                    self.settings.set_settings(key, auto_load_env=auto_load_env, raise_empty=raise_empty)
                else:
                    self.settings.set_settings(key, value, auto_load_env=auto_load_env, raise_empty=raise_empty)
            if isinstance(key, str) and key in ("runtime.httpx_log_level", "debug"):
                refresh_httpx_log_level()
            return self

        def load_settings(
            data_type: Literal["json_file", "yaml_file", "toml_file", "json", "yaml", "toml"],
            value: str,
            *,
            auto_load_env: bool = False,
            raise_empty: bool = False,
        ) -> "AgentlyMain[A]":
            self.settings.load(data_type, value, auto_load_env=auto_load_env, raise_empty=raise_empty)
            if self.settings.get("debug", None) is not None:
                _apply_debug_profile(self.settings, self.settings.get("debug"))
            refresh_httpx_log_level()
            return self

        self.set_settings = set_settings
        self.load_settings = load_settings

    def set_api_key(self, api_key: str) -> "AgentlyMain[A]":
        self.set_settings("agently.api_key", api_key)
        return self

    def set_debug_console(self, debug_console_status: Literal["ON", "OFF"]) -> "AgentlyMain[A]":
        # Deprecated: debug console mode is retired and no longer participates in runtime.
        if debug_console_status == "ON":
            DeprecationWarnings.log_deprecated_once(
                "Agently.set_debug_console.ON",
                self.logger,
                "`set_debug_console(\"ON\")` is deprecated and has no effect.",
            )
        return self

    def set_log_level(self, log_level: "RuntimeEventLevel") -> "AgentlyMain[A]":
        self.logger.setLevel(log_level)
        return self

    def configure_policy_approval(self, *, handler: str | None = None) -> "AgentlyMain[A]":
        if handler is not None:
            self.policy_approval.set_default_handler(handler)
        return self

    def create_prompt(self, name: str = "agently_prompt") -> Prompt:
        return Prompt(
            self.plugin_manager,
            self.settings,
            name=name,
        )

    def create_request(self, name: str | None = None) -> ModelRequest:
        return ModelRequest(
            self.plugin_manager,
            parent_settings=self.settings,
            agent_name=name,
        )

    def create_dynamic_task(
        self,
        target: str,
        *,
        plan: "TaskDAG | Mapping[str, Any] | None" = None,
        planner: Any = None,
        model: Any = None,
        actions: Any = None,
        skills: Any = None,
        handlers: Mapping[str, Any] | None = None,
        name: str | None = None,
        max_tasks: int | None = None,
        output_schema: Any = None,
        ensure_keys: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ) -> DynamicTask:
        return DynamicTask(
            self.plugin_manager,
            target,
            plan=plan,
            planner=planner,
            model=model,
            actions=actions,
            skills=skills,
            handlers=handlers,
            parent_settings=self.settings,
            name=name,
            max_tasks=max_tasks,
            output_schema=output_schema,
            ensure_keys=ensure_keys,
            output_format=output_format,
        )

    def create_agent(self, name: str | None = None) -> A:
        return self.AgentType(
            self.plugin_manager,
            parent_settings=self.settings,
            name=name,
        )

    def create_trigger_flow(
        self,
        name: str | None = None,
        *,
        skip_exceptions: bool = False,
    ) -> TriggerFlow[Any, Any, Any]:
        return TriggerFlow(name=name, skip_exceptions=skip_exceptions)

    def create_observation_bridge(self, *watch_targets: Any, **bridge_options: Any) -> Any:
        devtools = LazyImport.import_package(
            "agently_devtools",
            auto_install=False,
            install_name="agently-devtools",
        )
        ObservationBridge = devtools.ObservationBridge
        bridge = ObservationBridge(self, **bridge_options)
        if watch_targets:
            bridge.watch(*watch_targets)
        return bridge

    def observe(self, *watch_targets: Any, **bridge_options: Any) -> Any:
        return self.create_observation_bridge(*watch_targets, **bridge_options)
