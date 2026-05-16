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


class DockerActionExecutor:
    name = "DockerActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "docker"
    sandboxed = True

    def __init__(self, *, image: str | None = None, timeout: int = 60):
        self.image = image
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
        image = str(action_input.get("image") or self.image or "")
        command = action_input.get("cmd", action_input.get("command", []))
        if isinstance(command, str):
            cmd = command
        elif isinstance(command, list):
            cmd = [str(item) for item in command]
        else:
            cmd = str(command)
        action_id = str(spec.get("action_id", "run_docker"))
        timeout = int(policy.get("timeout_seconds", self.timeout))
        environment_resources = action_call.get("execution_environment_resources", {})
        if isinstance(environment_resources, dict):
            docker_resource = environment_resources.get(action_id) or environment_resources.get("docker")
            if docker_resource is not None and hasattr(docker_resource, "run"):
                return await docker_resource.run(
                    image=image,
                    cmd=cmd,
                    workdir=action_input.get("workdir"),
                    env=action_input.get("env"),
                    timeout=timeout,
                )
        return {
            "ok": False,
            "error": "Docker execution environment resource is not available.",
        }
