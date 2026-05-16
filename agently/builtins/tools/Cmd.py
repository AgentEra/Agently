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

from agently.builtins.actions.Cmd import Cmd as _Cmd


class Cmd(_Cmd):
    """Legacy import facade for `agently.builtins.actions.Cmd`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_info_list = [
            {
                "name": "cmd",
                "desc": "Run a shell command with allowlist checks.",
                "kwargs": {
                    "cmd": ("str | list[str]", "Command to run"),
                    "workdir": ("str | None", "Working directory"),
                    "allow_unsafe": ("bool", "Allow command outside allowlist"),
                },
                "func": self.run,
            }
        ]
