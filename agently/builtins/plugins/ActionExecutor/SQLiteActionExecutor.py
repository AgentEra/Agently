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


class SQLiteActionExecutor:
    name = "SQLiteActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "sqlite"
    sandboxed = False

    def __init__(self, *, read_only: bool = True):
        self.read_only = read_only

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
        action_id = str(spec.get("action_id", "query_sqlite"))
        query = str(action_input.get("query", ""))
        params = action_input.get("params", [])
        if params is None:
            params = []
        read_only = bool(policy.get("read_only", self.read_only))
        environment_resources = action_call.get("execution_environment_resources", {})
        if isinstance(environment_resources, dict):
            sqlite_resource = environment_resources.get(action_id) or environment_resources.get("sqlite")
            if sqlite_resource is not None and hasattr(sqlite_resource, "execute"):
                return await sqlite_resource.execute(query=query, params=params, read_only=read_only)
        return {
            "ok": False,
            "error": "SQLite execution environment resource is not available.",
        }
