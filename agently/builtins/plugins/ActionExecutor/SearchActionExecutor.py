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

from typing import Any



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
        action_method = getattr(self.search, "_execute_action_method", None)
        if callable(action_method):
            return await default_stage_call_bridge.as_async(action_method)(self.method_name, **action_input)
        method = getattr(self.search, self.method_name)
        return await default_stage_call_bridge.as_async(method)(**action_input)
