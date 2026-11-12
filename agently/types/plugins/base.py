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

from typing import Any, Literal, Protocol, runtime_checkable

AgentlyPluginType = Literal[
    "PromptGenerator",
    "ModelRequester",
    "AudioModelRequester",
    "ResponseParser",
    "ToolManager",
    "Session",
    "SessionMemory",
    "ActionRuntime",
    "ActionFlow",
    "ActionExecutor",
    "ExecutionResourceProvider",
    "CodeRuntimeAdapter",
    "SkillSourceProvider",
    "TaskDAGPlanner",
    "Blocks",
    "AgentOrchestrator",
    "AgentExecution",
    "RecordStoreBackend",
    "DBStoreProvider",
    "ContentStore",
    "MetadataStore",
    "CheckpointStore",
    "ExecutionSnapshotStore",
    "TextIndex",
    "PolicyEngine",
    "VectorIndex",
    "EmbeddingProvider",
    "RerankProvider",
    "VectorStoreProvider",
    "RecordStoreProviderFactory",
    "TaskWorkspaceFileIOHandler",
]


@runtime_checkable
class AgentlyPlugin(Protocol):
    """
    Agently Plugin Base Protocol
    """

    name: str
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register():
        """
        Tasks to be done before register plugin
        """
        ...

    @staticmethod
    def _on_unregister():
        """
        Tasks to be done after unregister plugin
        """
        ...
