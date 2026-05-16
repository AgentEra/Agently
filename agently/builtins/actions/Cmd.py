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
import subprocess
from pathlib import Path
from typing import Iterable, Sequence


class Cmd:
    def __init__(
        self,
        *,
        allowed_cmd_prefixes: Sequence[str] | None = None,
        allowed_workdir_roots: Iterable[str | Path] | None = None,
        timeout: int = 20,
        env: dict[str, str] | None = None,
    ):
        self.allowed_cmd_prefixes = set(
            allowed_cmd_prefixes
            if allowed_cmd_prefixes is not None
            else ["ls", "rg", "cat", "pwd", "whoami", "date", "head", "tail"]
        )
        roots = allowed_workdir_roots if allowed_workdir_roots is not None else [Path.cwd()]
        self.allowed_workdir_roots = [Path(root).resolve() for root in roots]
        self.timeout = timeout
        self.env = env

    def register_actions(
        self,
        action,
        *,
        tags: str | list[str] | None = None,
        action_prefix: str = "",
        expose_to_model: bool = True,
        default_policy: dict | None = None,
    ) -> list[str]:
        prefix = action_prefix.strip()
        action_id = f"{ prefix }cmd" if prefix else "cmd"
        action.register_action(
            action_id=action_id,
            desc="Run a low-level allowlisted shell command. Prefer `agent.enable_shell(...)` for user-facing shell access.",
            kwargs={
                "cmd": ("str | list[str]", "Command to run."),
                "workdir": ("str | None", "Working directory."),
                "allow_unsafe": (bool, "Allow command outside allowlist. Default: False."),
            },
            func=self.run,
            tags=tags,
            default_policy=default_policy,
            side_effect_level="exec",
            approval_required=False,
            sandbox_required=False,
            expose_to_model=expose_to_model,
            meta={
                "component": "builtins.actions.Cmd",
                "legacy_tool_facade": "agently.builtins.tools.Cmd",
                "recommended_public_helper": "agent.enable_shell",
            },
        )
        return [action_id]

    def _normalize_cmd(self, cmd: str | Sequence[str]) -> list[str]:
        if isinstance(cmd, str):
            return shlex.split(cmd)
        return list(cmd)

    def _is_cmd_allowed(self, args: list[str]) -> bool:
        if not args:
            return False
        cmd = args[0]
        base = Path(cmd).name
        return base in self.allowed_cmd_prefixes

    def _is_workdir_allowed(self, workdir: str | Path | None) -> bool:
        workdir_path = Path(workdir or Path.cwd()).resolve()
        for root in self.allowed_workdir_roots:
            try:
                workdir_path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    async def run(
        self,
        cmd: str | Sequence[str],
        workdir: str | Path | None = None,
        allow_unsafe: bool = False,
    ) -> dict:
        args = self._normalize_cmd(cmd)
        if not self._is_workdir_allowed(workdir):
            return {
                "ok": False,
                "need_approval": True,
                "reason": "workdir_not_allowed",
                "workdir": str(workdir or Path.cwd()),
            }
        if not self._is_cmd_allowed(args) and not allow_unsafe:
            return {
                "ok": False,
                "need_approval": True,
                "reason": "cmd_not_allowed",
                "cmd": args,
            }
        result = subprocess.run(
            args,
            cwd=str(Path(workdir).resolve()) if workdir else None,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=self.env,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
