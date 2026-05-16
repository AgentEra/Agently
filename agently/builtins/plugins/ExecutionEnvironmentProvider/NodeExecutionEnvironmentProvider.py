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

import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionEnvironmentHandle,
        ExecutionEnvironmentPolicy,
        ExecutionEnvironmentRequirement,
        ExecutionEnvironmentStatus,
    )


class NodeExecutionResource:
    def __init__(
        self,
        *,
        node_binary: str = "node",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 20,
    ):
        self.node_binary = node_binary
        self.cwd = str(Path(cwd).expanduser().resolve()) if cwd else None
        self.env = env
        self.timeout = timeout

    def is_available(self):
        return shutil.which(self.node_binary) is not None

    async def run(self, *, js_code: str, args: list[str] | None = None, timeout: int | None = None):
        if not self.is_available():
            return {
                "ok": False,
                "error": f"Node.js binary not found: { self.node_binary }",
            }
        result = subprocess.run(
            [self.node_binary, "-e", js_code, *(args or [])],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
            env=self.env,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


class NodeExecutionEnvironmentProvider:
    name = "NodeExecutionEnvironmentProvider"
    DEFAULT_SETTINGS = {}
    kind = "node"

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
        config = requirement.get("config", {})
        resource = NodeExecutionResource(
            node_binary=str(config.get("node_binary", "node")),
            cwd=config.get("cwd"),
            env=config.get("env"),
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 20))),
        )
        return {
            "handle_id": f"node:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "node_binary": resource.node_binary,
                "available": resource.is_available(),
            },
        }

    async def async_health_check(self, handle: "ExecutionEnvironmentHandle") -> "ExecutionEnvironmentStatus":
        resource = handle.get("resource")
        return "ready" if resource is not None and hasattr(resource, "run") else "unhealthy"

    async def async_release(self, handle: "ExecutionEnvironmentHandle") -> None:
        _ = handle
        return None
