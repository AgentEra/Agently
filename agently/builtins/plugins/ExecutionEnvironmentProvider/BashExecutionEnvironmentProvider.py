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

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionEnvironmentHandle,
        ExecutionEnvironmentPolicy,
        ExecutionEnvironmentRequirement,
        ExecutionEnvironmentStatus,
    )


class BashExecutionEnvironmentProvider:
    name = "BashExecutionEnvironmentProvider"
    DEFAULT_SETTINGS = {}
    kind = "bash"

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionEnvironmentRequirement",
        policy: "ExecutionEnvironmentPolicy",
        existing_handle: "ExecutionEnvironmentHandle | None" = None,
    ) -> "ExecutionEnvironmentHandle":
        _ = existing_handle
        from agently.builtins.actions import Cmd

        config = requirement.get("config", {})
        resource = Cmd(
            allowed_cmd_prefixes=policy.get("allowed_cmd_prefixes", config.get("allowed_cmd_prefixes")),
            allowed_workdir_roots=policy.get("workspace_roots", config.get("allowed_workdir_roots")),
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 20))),
            env=config.get("env", None),
        )
        return {
            "handle_id": f"bash:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {"provider": self.name},
        }

    async def async_health_check(self, handle: "ExecutionEnvironmentHandle") -> "ExecutionEnvironmentStatus":
        return "ready" if handle.get("resource") is not None else "unhealthy"

    async def async_release(self, handle: "ExecutionEnvironmentHandle") -> None:
        _ = handle
        return None
