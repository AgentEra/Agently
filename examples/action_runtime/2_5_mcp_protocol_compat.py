"""Model-free MCP adapter smoke: real tools, schemas, state and client cleanup."""

import asyncio
import json

import fastmcp
from agently import Agently


async def main() -> None:
    server = fastmcp.FastMCP("Agently MCP adapter smoke")
    messages: list[str] = []

    @server.tool(tags={"server"})
    async def remember(message: str) -> dict[str, str | int]:
        """Append one message to this local server's state."""
        messages.append(message)
        return {"count": len(messages), "last": message}

    agent = Agently.create_agent()
    try:
        await agent.action.async_use_mcp(server, tags=["host"])
        spec = agent.action.action_registry.get_spec("remember")
        assert spec is not None
        assert set(spec.get("tags") or []) == {"server", "host"}
        assert spec.get("required_input_keys") == ["message"]
        first = await agent.action.async_call_action("remember", {"message": "first"})
        second = await agent.action.async_call_action("remember", {"message": "second"})
        assert first == {"count": 1, "last": "first"}
        assert second == {"count": 2, "last": "second"}
    finally:
        await Agently.execution_resource.async_release_scope("agent", agent.name)
    assert Agently.execution_resource.list(scope="agent", owner_id=agent.name) == []
    print(json.dumps({"calls": len(messages), "tags": ["host", "server"], "required_input": ["message"], "last": second, "resources_released": True}))


if __name__ == "__main__":
    asyncio.run(main())

# This exercises the real in-process MCP protocol without a model or credentials.
# It proves adapter behavior, not model action selection or business reasoning.
# Expected key output (observed with FastMCP 4.1.0 and 3.4.7):
# {"calls": 2, "tags": ["host", "server"], "required_input": ["message"],
#  "last": {"count": 2, "last": "second"}, "resources_released": true}
