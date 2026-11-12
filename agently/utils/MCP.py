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

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse


def _tool_field(tool: object, current: str, legacy: str) -> object:
    """Prefer SDK v2 fields without invoking FastMCP's deprecated aliases."""
    try:
        return getattr(tool, current)
    except AttributeError:
        return getattr(tool, legacy, None)


def mcp_tool_schema(tool: object, kind: Literal["input", "output"]) -> dict[str, Any] | None:
    """Translate the optional SDK boundary; JSON Schema remains lossless."""
    value = _tool_field(tool, f"{kind}_schema", f"{kind}Schema")
    if value is None and kind == "output":
        return None
    if not isinstance(value, dict):
        raise ValueError(f"MCP tool {kind} schema must be a JSON object.")
    return value


def mcp_tool_tags(tool: object) -> list[str]:
    meta = _tool_field(tool, "meta", "_meta")
    if not isinstance(meta, dict):
        return []
    tags: list[str] = []
    for key in ("fastmcp", "_fastmcp"):
        namespace = meta.get(key)
        if not isinstance(namespace, dict):
            continue
        declared = namespace.get("tags", [])
        if isinstance(declared, list):
            for tag in declared:
                if isinstance(tag, str) and tag not in tags:
                    tags.append(tag)
    return tags


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def normalize_mcp_transport(transport: Any, *, headers: dict[str, str] | None = None) -> Any:
    """Normalize Python stdio paths and URL + headers for FastMCP."""

    if not headers:
        if isinstance(transport, str) and transport.endswith(".py") and not _is_url(transport):
            return Path(transport)
        return transport
    normalized_headers = {str(key): str(value) for key, value in headers.items()}
    if isinstance(transport, str) and _is_url(transport):
        return {"mcpServers": {"default": {"url": transport, "headers": normalized_headers}}}
    if isinstance(transport, dict):
        normalized = deepcopy(transport)
        if isinstance(normalized.get("mcpServers"), dict):
            for config in normalized["mcpServers"].values():
                if isinstance(config, dict) and config.get("url"):
                    merged = dict(config.get("headers") or {})
                    merged.update(normalized_headers)
                    config["headers"] = merged
            return normalized
        if normalized.get("url"):
            merged = dict(normalized.get("headers") or {})
            merged.update(normalized_headers)
            normalized["headers"] = merged
            return normalized
    raise ValueError("headers= is supported only for MCP URL or URL-based MCP config transports.")
