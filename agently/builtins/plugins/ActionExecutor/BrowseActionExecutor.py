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


class BrowseActionExecutor:
    name = "BrowseActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "browse"
    sandboxed = False

    def __init__(self, *, browse):
        self.browse = browse

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = (policy, settings)
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        action_id = str(spec.get("action_id", "browse"))
        url = str(action_input.get("url", ""))
        environment_resources = action_call.get("execution_environment_resources", {})
        if isinstance(environment_resources, dict):
            browser_resource = environment_resources.get(action_id) or environment_resources.get("browse")
            if browser_resource is not None and hasattr(browser_resource, "browse"):
                return await FunctionShifter.asyncify(browser_resource.browse)(
                    browse_tool=self.browse,
                    url=url,
                )
        return await FunctionShifter.asyncify(self.browse.browse)(**action_input)
