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

import sqlite3
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionEnvironmentHandle,
        ExecutionEnvironmentPolicy,
        ExecutionEnvironmentRequirement,
        ExecutionEnvironmentStatus,
    )


class SQLiteExecutionResource:
    READ_KEYWORDS = {"select", "with", "pragma", "explain"}

    def __init__(self, *, database: str = ":memory:", uri: bool = False):
        self.database = database
        self.uri = uri
        self.connection = sqlite3.connect(database, uri=uri)
        self.connection.row_factory = sqlite3.Row

    @staticmethod
    def _is_read_query(query: str):
        stripped = query.strip().lower()
        if not stripped:
            return False
        return stripped.split(None, 1)[0] in SQLiteExecutionResource.READ_KEYWORDS

    async def execute(self, *, query: str, params: Any = None, read_only: bool = True):
        if read_only and not self._is_read_query(query):
            return {
                "ok": False,
                "need_approval": True,
                "reason": "sqlite_write_blocked",
                "query": query,
            }
        cursor = self.connection.execute(query, params or [])
        if cursor.description is None:
            self.connection.commit()
            return {
                "ok": True,
                "rowcount": cursor.rowcount,
                "rows": [],
            }
        rows = [dict(row) for row in cursor.fetchall()]
        return {
            "ok": True,
            "rowcount": len(rows),
            "rows": rows,
        }

    def health_check(self):
        try:
            self.connection.execute("select 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    async def close(self):
        self.connection.close()


class SQLiteExecutionEnvironmentProvider:
    name = "SQLiteExecutionEnvironmentProvider"
    DEFAULT_SETTINGS = {}
    kind = "sqlite"

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
        _ = (policy, existing_handle)
        config = requirement.get("config", {})
        database = str(config.get("database", ":memory:"))
        if database != ":memory:" and not bool(config.get("uri", False)):
            database = str(Path(database).expanduser().resolve())
        resource = SQLiteExecutionResource(database=database, uri=bool(config.get("uri", False)))
        return {
            "handle_id": f"sqlite:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "database": database,
            },
        }

    async def async_health_check(self, handle: "ExecutionEnvironmentHandle") -> "ExecutionEnvironmentStatus":
        resource = handle.get("resource")
        if resource is not None and hasattr(resource, "health_check") and resource.health_check():
            return "ready"
        return "unhealthy"

    async def async_release(self, handle: "ExecutionEnvironmentHandle") -> None:
        resource = handle.get("resource")
        if resource is not None and hasattr(resource, "close"):
            await resource.close()
        return None
