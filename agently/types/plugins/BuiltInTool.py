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


from typing import Protocol

from agently.types.data import ToolInfo


class BuiltInTool(Protocol):
    """
    Legacy authoring protocol for built-in tool facades.

    New built-in capabilities should expose `register_actions(...)` through
    `agently.builtins.actions` and use ActionExecutor plugins for execution
    backend variation. `tool_info_list` remains for compatibility mounts.
    """

    tool_info_list: list[ToolInfo]
