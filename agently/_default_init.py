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
    from agently.builtins.plugins.ActionFlow import DAGActionFlow, TriggerFlowActionFlow
    from agently.builtins.plugins.ActionRuntime import AgentlyActionRuntime
    from agently.builtins.plugins.ActionExecutor import (
        BashSandboxActionExecutor,
        BrowseActionExecutor,
        CodeExecutionActionExecutor,
        DockerActionExecutor,
        LocalFunctionActionExecutor,
        MCPActionExecutor,
        SQLiteActionExecutor,
        SearchActionExecutor,
    )
    from agently.builtins.plugins.ExecutionResourceProvider import (
        ACPExecutionResourceProvider,
        BashExecutionResourceProvider,
        BrowserExecutionResourceProvider,
        DockerExecutionResourceProvider,
        GVisorDockerExecutionResourceProvider,
        SeatbeltExecutionResourceProvider,
        MCPExecutionResourceProvider,
        SQLiteExecutionResourceProvider,
        TrustedLocalExecutionResourceProvider,
    )

    plugin_manager.register("ActionRuntime", AgentlyActionRuntime)
    plugin_manager.register("ActionFlow", TriggerFlowActionFlow)
    plugin_manager.register("ActionFlow", DAGActionFlow, activate=False)
    plugin_manager.register("ActionExecutor", LocalFunctionActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", MCPActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", BashSandboxActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", SearchActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", BrowseActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", CodeExecutionActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", DockerActionExecutor, activate=False)
    plugin_manager.register("ActionExecutor", SQLiteActionExecutor, activate=False)
    plugin_manager.register("ExecutionResourceProvider", ACPExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", MCPExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", BashExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", DockerExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", GVisorDockerExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", SeatbeltExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", BrowserExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", SQLiteExecutionResourceProvider, activate=False)
    plugin_manager.register("ExecutionResourceProvider", TrustedLocalExecutionResourceProvider, activate=False)

    from agently.builtins.plugins.SkillSourceProvider import (
        GitSkillSourceProvider,
        LocalPathSkillSourceProvider,
    )

    plugin_manager.register("SkillSourceProvider", LocalPathSkillSourceProvider, activate=False)
    plugin_manager.register("SkillSourceProvider", GitSkillSourceProvider, activate=False)

    from agently.builtins.plugins.CodeRuntimeAdapter import (
        CppCodeRuntimeAdapter,
        GoCodeRuntimeAdapter,
        NodeCodeRuntimeAdapter,
        PythonCodeRuntimeAdapter,
    )

    plugin_manager.register("CodeRuntimeAdapter", PythonCodeRuntimeAdapter, activate=False)
    plugin_manager.register("CodeRuntimeAdapter", NodeCodeRuntimeAdapter, activate=False)
    plugin_manager.register("CodeRuntimeAdapter", GoCodeRuntimeAdapter, activate=False)
    plugin_manager.register("CodeRuntimeAdapter", CppCodeRuntimeAdapter, activate=False)

    from agently.builtins.plugins.PromptGenerator.AgentlyPromptGenerator import (
        AgentlyPromptGenerator,
    )

    plugin_manager.register("PromptGenerator", AgentlyPromptGenerator)

    from agently.builtins.plugins.TaskDAGPlanner import (
        AgentlyTaskDAGPlanner,
    )

    plugin_manager.register("TaskDAGPlanner", AgentlyTaskDAGPlanner)

    from agently.builtins.plugins.Blocks import AgentlyBlocks

    plugin_manager.register("Blocks", AgentlyBlocks)

    from agently.builtins.plugins.AgentOrchestrator import AgentlyAgentOrchestrator

    plugin_manager.register("AgentOrchestrator", AgentlyAgentOrchestrator)

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

    from agently.builtins.plugins.SessionMemory import AgentlyMemory

    plugin_manager.register("SessionMemory", AgentlyMemory)


def _load_default_settings(settings: "Settings"):
    settings.load("yaml_file", f"{str(Path(__file__).resolve().parent)}/_default_settings.yaml")


def _hook_default_event_handlers(event_center: "EventCenter"):
    from agently.builtins.hookers.RuntimeConsoleSinkHooker import RuntimeConsoleSinkHooker

    event_center.register_hooker_plugin(RuntimeConsoleSinkHooker)

    from agently.builtins.hookers.RuntimeStorageSinkHooker import RuntimeStorageSinkHooker

    event_center.register_hooker_plugin(RuntimeStorageSinkHooker)


def _load_default_actions(_):
    return None
