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
        _ = policy
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        action_id = str(spec.get("action_id", "browse"))
        url = str(action_input.get("url", ""))
        task_workspace = None
        settings_get = getattr(settings, "get", None)
        if callable(settings_get):
            task_workspace = settings_get("action.task_workspace", None)
        environment_resources = action_call.get("execution_resource_resources", {})
        if isinstance(environment_resources, dict):
            browser_resource = environment_resources.get(action_id) or environment_resources.get("browse")
            if browser_resource is not None and hasattr(browser_resource, "browse"):
                return await default_stage_call_bridge.as_async(browser_resource.browse)(
                    browse_tool=self.browse,
                    url=url,
                )
        action_method = getattr(self.browse, "_execute_action_method", None)
        if callable(action_method):
            return await default_stage_call_bridge.as_async(action_method)(
                "browse",
                task_workspace=task_workspace,
                **action_input,
            )
        return await default_stage_call_bridge.as_async(self.browse.browse)(**action_input)
