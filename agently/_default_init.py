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

from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agently.core import PluginManager, EventCenter
    from agently.utils import Settings


def _load_default_plugins(plugin_manager: "PluginManager"):
    from agently.builtins.plugins.ActionFlow import TriggerFlowActionFlow
    from agently.builtins.plugins.ActionRuntime import AgentlyActionRuntime
    from agently.builtins.plugins.ActionExecutor import (
        BashSandboxActionExecutor,
        BrowseActionExecutor,
        DockerActionExecutor,
        LocalFunctionActionExecutor,
        MCPActionExecutor,
        NodeJSActionExecutor,
        PythonSandboxActionExecutor,
        SQLiteActionExecutor,
        SearchActionExecutor,
    )
    from agently.builtins.plugins.ExecutionEnvironmentProvider import (
        BashExecutionEnvironmentProvider,
        BrowserExecutionEnvironmentProvider,
        DockerExecutionEnvironmentProvider,
        MCPExecutionEnvironmentProvider,
        NodeExecutionEnvironmentProvider,
        PythonExecutionEnvironmentProvider,
        SQLiteExecutionEnvironmentProvider,
    )

    plugin_manager.register("ActionRuntime", AgentlyActionRuntime)
    plugin_manager.register("ActionFlow", TriggerFlowActionFlow)
    plugin_manager.register("ActionExecutor", LocalFunctionActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", MCPActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", PythonSandboxActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", BashSandboxActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", SearchActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", BrowseActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", NodeJSActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", DockerActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", SQLiteActionExecutor, activate=False)
    plugin_manager.register("ExecutionEnvironmentProvider", MCPExecutionEnvironmentProvider, activate=False)
    plugin_manager.register("ExecutionEnvironmentProvider", BashExecutionEnvironmentProvider, activate=False)
    plugin_manager.register("ExecutionEnvironmentProvider", PythonExecutionEnvironmentProvider, activate=False)
    plugin_manager.register("ExecutionEnvironmentProvider", NodeExecutionEnvironmentProvider, activate=False)
    plugin_manager.register("ExecutionEnvironmentProvider", DockerExecutionEnvironmentProvider, activate=False)
    plugin_manager.register("ExecutionEnvironmentProvider", BrowserExecutionEnvironmentProvider, activate=False)
    plugin_manager.register("ExecutionEnvironmentProvider", SQLiteExecutionEnvironmentProvider, activate=False)

    from agently.builtins.plugins.PromptGenerator.AgentlyPromptGenerator import (
        AgentlyPromptGenerator,
    )

    plugin_manager.register("PromptGenerator", AgentlyPromptGenerator)

    from agently.builtins.plugins.ModelRequester.OpenAICompatible import (
        OpenAICompatible,
    )
    from agently.builtins.plugins.ModelRequester.AnthropicCompatible import (
        AnthropicCompatible,
    )
    from agently.builtins.plugins.ModelRequester.OpenAIResponsesCompatible import (
        OpenAIResponsesCompatible,
    )

    plugin_manager.register(
        "ModelRequester",
        OpenAICompatible,
        activate=True,
    )
    plugin_manager.register(
        "ModelRequester",
        AnthropicCompatible,
        activate=False,
    )
    plugin_manager.register(
        "ModelRequester",
        OpenAIResponsesCompatible,
        activate=False,
    )

    from agently.builtins.plugins.ResponseParser.AgentlyResponseParser import AgentlyResponseParser

    plugin_manager.register("ResponseParser", AgentlyResponseParser)


def _load_default_settings(settings: "Settings"):
    settings.load("yaml_file", f"{str(Path(__file__).resolve().parent)}/_default_settings.yaml")


def _hook_default_event_handlers(event_center: "EventCenter"):
    from agently.builtins.hookers.RuntimeConsoleSinkHooker import RuntimeConsoleSinkHooker

    event_center.register_hooker_plugin(RuntimeConsoleSinkHooker)

    from agently.builtins.hookers.RuntimeStorageSinkHooker import RuntimeStorageSinkHooker

    event_center.register_hooker_plugin(RuntimeStorageSinkHooker)


def _load_default_actions(_):
    return None
