"""Real local MCP protocol checks; no model or external service is used."""

from __future__ import annotations

import types
import warnings
from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.utils.MCP import mcp_tool_schema, mcp_tool_tags, normalize_mcp_transport


@pytest.fixture
def fastmcp_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = pytest.importorskip("fastmcp")
    if hasattr(module.settings, "mcp_camelcase_compat"):
        monkeypatch.setattr(module.settings, "mcp_camelcase_compat", False)
    return module


def test_mcp_tool_fields_preserve_both_sdk_shapes() -> None:
    schema = {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}
    for tool in (
        types.SimpleNamespace(input_schema=schema, output_schema={}, meta={"fastmcp": {"tags": ["server"]}}),
        types.SimpleNamespace(inputSchema=schema, outputSchema={}, _meta={"_fastmcp": {"tags": ["server"]}}),
    ):
        assert mcp_tool_schema(tool, "input") == schema
        assert mcp_tool_schema(tool, "output") == {}
        assert mcp_tool_tags(tool) == ["server"]
    assert mcp_tool_schema(types.SimpleNamespace(input_schema=schema), "output") is None
    with pytest.raises(ValueError, match="input schema"):
        mcp_tool_schema(types.SimpleNamespace(input_schema=None, inputSchema=schema), "input")


def test_mcp_tool_tags_do_not_mutate_metadata() -> None:
    tags = ["shared", "modern"]
    tool = types.SimpleNamespace(meta={"fastmcp": {"tags": tags}, "_fastmcp": {"tags": ["shared", "old", 1]}})
    assert mcp_tool_tags(tool) == ["shared", "modern", "old"]
    assert tags == ["shared", "modern"]


def test_mcp_script_paths_are_explicit_without_changing_urls() -> None:
    assert normalize_mcp_transport("server.py") == Path("server.py")
    assert normalize_mcp_transport("https://example.com/server.py") == "https://example.com/server.py"
    with pytest.raises(ValueError, match="headers="):
        normalize_mcp_transport("server.py", headers={"X-Test": "value"})


@pytest.mark.asyncio
async def test_real_mcp_registration_call_and_managed_lifecycle(fastmcp_module: Any) -> None:
    server = fastmcp_module.FastMCP("Agently protocol test")
    calls: list[str] = []

    @server.tool(tags={"server-tag"})
    async def echo(message: str) -> dict[str, str]:
        calls.append(message)
        return {"message": message}

    agent = Agently.create_agent()
    try:
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            await agent.action.async_use_mcp(server, tags=["host-tag"])
            spec = agent.action.action_registry.get_spec("echo")
            assert spec is not None
            assert set(spec.get("tags") or []) == {"server-tag", "host-tag"}
            assert spec.get("required_input_keys") == ["message"]
            assert await agent.action.async_call_action("echo", {"message": "first"}) == {"message": "first"}
            assert await agent.action.async_call_action("echo", {"message": "second"}) == {"message": "second"}
            missing = await agent.action.async_execute_action("echo", {})
            assert missing.get("success") is False
            assert calls == ["first", "second"]
            assert not any(type(item.message).__name__ == "FastMCPDeprecationWarning" for item in observed)
        handles = Agently.execution_resource.list(scope="agent", owner_id=agent.name)
        assert len(handles) == 1
        client = handles[0].get("resource")
        assert client is not None
        assert client.is_connected()
    finally:
        await Agently.execution_resource.async_release_scope("agent", agent.name)
    assert not client.is_connected()
    assert Agently.execution_resource.list(scope="agent", owner_id=agent.name) == []


@pytest.mark.asyncio
async def test_real_legacy_tool_manager_mcp_registration(fastmcp_module: Any) -> None:
    from agently.builtins.plugins.ToolManager.AgentlyToolManager import AgentlyToolManager

    server = fastmcp_module.FastMCP("Legacy registration test")

    @server.tool(tags={"legacy-server"})
    async def echo(message: str) -> str:
        return message

    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        manager = AgentlyToolManager(Agently.settings)
        await manager.async_use_action_mcp(server, tags=["host"])
        spec = manager.action_registry.get_spec("echo")
        assert spec is not None
        assert set(spec.get("tags") or []) == {"legacy-server", "host"}
        assert await manager.async_call_action("echo", {"message": "legacy call"}) == {"result": "legacy call"}
        assert not any(type(item.message).__name__ == "FastMCPDeprecationWarning" for item in observed)


@pytest.mark.asyncio
async def test_real_mcp_result_content_keeps_wire_aliases(fastmcp_module: Any) -> None:
    from fastmcp.tools import ToolResult
    from mcp.types import ImageContent, ResourceLink, TextContent

    server = fastmcp_module.FastMCP("Result protocol test")

    @server.tool(output_schema=None)
    async def empty() -> Any:
        return ToolResult(content=[], structured_content={})

    @server.tool(output_schema=None)
    async def text() -> Any:
        return ToolResult(content=[TextContent(type="text", text="observed text")])

    @server.tool(output_schema=None)
    async def image() -> Any:
        return ToolResult(content=[ImageContent.model_validate({"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"})])

    @server.tool(output_schema=None)
    async def link() -> Any:
        return ToolResult(content=[ResourceLink.model_validate({"type": "resource_link", "uri": "file:///report.md", "name": "report", "mimeType": "text/markdown"})])

    @server.tool(output_schema=None)
    async def failed() -> Any:
        raise ValueError("declared protocol error")

    agent = Agently.create_agent()
    try:
        await agent.action.async_use_mcp(server)
        assert await agent.action.async_call_action("empty", {}) == {}
        assert await agent.action.async_call_action("text", {}) == "observed text"
        for name, mime in (("image", "image/png"), ("link", "text/markdown")):
            record = await agent.action.async_execute_action(name, {})
            assert record.get("success") is True
            data = record.get("data")
            assert isinstance(data, dict)
            assert data["mimeType"] == mime
            assert "mime_type" not in data
            refs = record.get("artifact_refs") or []
            assert refs[0].get("media_type") == mime
        record = await agent.action.async_execute_action("failed", {})
        assert record.get("success") is False
        assert "declared protocol error" in str(record.get("error"))
    finally:
        await Agently.execution_resource.async_release_scope("agent", agent.name)


@pytest.mark.asyncio
async def test_mcp_empty_error_result_preserves_failure(fastmcp_module: Any) -> None:
    from agently.builtins.plugins.ActionExecutor.MCPActionExecutor import MCPActionExecutor

    class Client:
        async def call_tool(self, **_kwargs: Any) -> Any:
            return types.SimpleNamespace(is_error=True, content=[], structured_content=None)

    result = await MCPActionExecutor("empty_error", None).execute(
        spec={}, policy={}, settings=Agently.settings,
        action_call={"execution_resource_resources": {"empty_error": Client()}},
    )
    assert result == {"error": "MCP tool 'empty_error' reported an error."}
