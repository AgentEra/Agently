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

from typing import Any

from agently.utils import FunctionShifter


class SearchActionExecutor:
    name = "SearchActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "search"
    sandboxed = False

    def __init__(self, *, search, method_name: str):
        self.search = search
        self.method_name = method_name

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = (spec, policy, settings)
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        method = getattr(self.search, self.method_name)
        return await FunctionShifter.asyncify(method)(**action_input)
