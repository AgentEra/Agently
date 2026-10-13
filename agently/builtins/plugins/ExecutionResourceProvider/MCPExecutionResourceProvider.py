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
from collections.abc import Awaitable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from agently.utils import LazyImport
from agently.utils.MCP import normalize_mcp_transport

from ._base import BuiltinExecutionResourceProvider

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


class MCPExecutionResourceProvider(BuiltinExecutionResourceProvider):
    name = "MCPExecutionResourceProvider"
    DEFAULT_SETTINGS = {}
    kind = "mcp"

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        _ = (policy, existing_handle)
        config = requirement.get("config", {})
        transport = normalize_mcp_transport(config.get("transport"))
        LazyImport.import_package("fastmcp", version_constraint=">=3", auto_install=False)
        from fastmcp import Client

        client = Client(transport)
        try:
            await client.__aenter__()
        except BaseException:
            with suppress(Exception):
                await client.close()
            raise
        return {
            "handle_id": f"mcp:{ uuid.uuid4().hex }",
            "resource": client,
            "status": "ready",
            "meta": {"provider": self.name, "managed_session": True},
        }

    async def async_health_check(self, handle: "ExecutionResourceHandle") -> "ExecutionResourceStatus":
        client = handle.get("resource")
        is_connected = getattr(client, "is_connected", None)
        return "ready" if callable(is_connected) and is_connected() else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        client: Any = handle.get("resource")
        close = getattr(client, "close", None)
        if callable(close):
            await cast(Awaitable[None], close())
