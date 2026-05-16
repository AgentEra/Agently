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


class NodeJSActionExecutor:
    name = "NodeJSActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "nodejs"
    sandboxed = True

    def __init__(self, *, timeout: int = 20):
        self.timeout = timeout

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = settings
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        raw_code = action_input.get("js_code", action_input.get("code", ""))
        js_code = "\n".join(str(line) for line in raw_code) if isinstance(raw_code, list) else str(raw_code)
        args = action_input.get("args", [])
        if not isinstance(args, list):
            args = [str(args)]
        action_id = str(spec.get("action_id", "run_nodejs"))
        timeout = int(policy.get("timeout_seconds", self.timeout))
        environment_resources = action_call.get("execution_environment_resources", {})
        if isinstance(environment_resources, dict):
            node_resource = environment_resources.get(action_id) or environment_resources.get("node")
            if node_resource is not None and hasattr(node_resource, "run"):
                return await node_resource.run(js_code=js_code, args=[str(arg) for arg in args], timeout=timeout)
        return {
            "ok": False,
            "error": "Node.js execution environment resource is not available.",
        }
