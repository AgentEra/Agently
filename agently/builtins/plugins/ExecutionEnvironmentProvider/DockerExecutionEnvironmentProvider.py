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

import shlex
import shutil
import subprocess
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionEnvironmentHandle,
        ExecutionEnvironmentPolicy,
        ExecutionEnvironmentRequirement,
        ExecutionEnvironmentStatus,
    )


class DockerExecutionResource:
    def __init__(
        self,
        *,
        docker_binary: str = "docker",
        timeout: int = 60,
        default_args: list[str] | None = None,
    ):
        self.docker_binary = docker_binary
        self.timeout = timeout
        self.default_args = default_args or []

    def is_available(self):
        return shutil.which(self.docker_binary) is not None

    async def run(
        self,
        *,
        image: str,
        cmd: str | list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ):
        if not image:
            return {"ok": False, "error": "Docker image is required."}
        if not self.is_available():
            return {"ok": False, "error": f"Docker binary not found: { self.docker_binary }"}
        args = [self.docker_binary, "run", "--rm", *self.default_args]
        if workdir:
            args.extend(["-w", str(workdir)])
        if isinstance(env, dict):
            for key, value in env.items():
                args.extend(["-e", f"{ key }={ value }"])
        args.append(image)
        if isinstance(cmd, str):
            args.extend(shlex.split(cmd))
        else:
            args.extend([str(item) for item in cmd])
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


class DockerExecutionEnvironmentProvider:
    name = "DockerExecutionEnvironmentProvider"
    DEFAULT_SETTINGS = {}
    kind = "docker"

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
        default_args = config.get("default_args", [])
        if not isinstance(default_args, list):
            default_args = []
        resource = DockerExecutionResource(
            docker_binary=str(config.get("docker_binary", "docker")),
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            default_args=[str(item) for item in default_args],
        )
        return {
            "handle_id": f"docker:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "docker_binary": resource.docker_binary,
                "available": resource.is_available(),
            },
        }

    async def async_health_check(self, handle: "ExecutionEnvironmentHandle") -> "ExecutionEnvironmentStatus":
        resource = handle.get("resource")
        return "ready" if resource is not None and hasattr(resource, "run") else "unhealthy"

    async def async_release(self, handle: "ExecutionEnvironmentHandle") -> None:
        _ = handle
        return None
