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


class BashSandboxActionExecutor:
    name = "BashSandboxActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "bash_sandbox"
    sandboxed = True

    def __init__(
        self,
        *,
        allowed_cmd_prefixes: list[str] | None = None,
        allowed_workdir_roots: list[str] | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
    ):
        self.allowed_cmd_prefixes = allowed_cmd_prefixes
        self.allowed_workdir_roots = allowed_workdir_roots
        self.timeout = timeout
        self.env = env

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def execute(self, *, spec, action_call, policy, settings) -> Any:
        _ = (spec, settings)
        from agently.builtins.actions import Cmd

        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        action_id = str(spec.get("action_id", "bash_sandbox"))
        environment_resources = action_call.get("execution_environment_resources", {})
        if isinstance(environment_resources, dict):
            cmd_resource = environment_resources.get(action_id)
            if cmd_resource is not None and hasattr(cmd_resource, "run"):
                return await cmd_resource.run(
                    cmd=action_input.get("cmd", ""),
                    workdir=action_input.get("workdir", None),
                    allow_unsafe=bool(action_input.get("allow_unsafe", False)),
                )

        cmd = Cmd(
            allowed_cmd_prefixes=policy.get("allowed_cmd_prefixes", self.allowed_cmd_prefixes),
            allowed_workdir_roots=policy.get("workspace_roots", self.allowed_workdir_roots),
            timeout=int(policy.get("timeout_seconds", self.timeout)),
            env=self.env,
        )
        return await cmd.run(
            cmd=action_input.get("cmd", ""),
            workdir=action_input.get("workdir", None),
            allow_unsafe=bool(action_input.get("allow_unsafe", False)),
        )
