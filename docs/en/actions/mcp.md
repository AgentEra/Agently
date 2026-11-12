---
title: MCP
description: Calling MCP servers from an Agently agent.
keywords: Agently, MCP, Model Context Protocol, use_mcp, MCPActionExecutor
---

# MCP

> Languages: **English** · [中文](../../cn/actions/mcp.md)

MCP (Model Context Protocol) exposes external tools to AI agents. Agently wires
MCP servers into the action runtime via `MCPActionExecutor` so the model sees
MCP tools and your own `@agent.action_func` actions through the same interface.

Use URL / Streamable HTTP MCP endpoints for service integrations, and stdio
command configs for local development, desktop clients, or single-user local
servers. SSE endpoints remain a legacy compatibility path.

## Minimal example

FastMCP is optional. For the 4.1.4.10 candidate's updated adapter, install
`fastmcp==4.1.0` to use MCP SDK v2; FastMCP 3.x with its resolver-selected SDK
v1 remains compatible. Do not independently upgrade `mcp` across the range
required by your FastMCP version. The adapter reads SDK v2 snake_case fields
without requiring FastMCP's deprecated camelCase bridge, preserves server and
host tags, and retains MCP wire aliases such as `mimeType` in content values.
Empty structured objects remain `{}`; error results remain failures even when
the server supplies no text block. These are adapter guarantees, separate from
model selection or answer quality.

The model-free [protocol smoke](../../../examples/action_runtime/2_5_mcp_protocol_compat.py)
uses a real local MCP server and checks schemas, two stateful calls and resource
release. An agent owns one managed client per registration; the upstream
protocol determines whether a server session exists. FastMCP 4's interactive
tools and background-task extensions require their own host integration; this
adapter update does not add those workflows.
The existing `runtime.httpx_log_level` setting also controls FastMCP 4's
`httpx2` / `httpcore2` loggers; it does not change tool permissions or retry.

```python
import os
import asyncio
from dotenv import load_dotenv, find_dotenv
from agently import Agently

load_dotenv(find_dotenv())

Agently.set_settings("OpenAICompatible", {
    "base_url": "${ENV.OPENAI_BASE_URL}",
    "api_key": "${ENV.OPENAI_API_KEY}",
    "model": "${ENV.OPENAI_MODEL}",
})

agent = Agently.create_agent()


async def main():
    result = (
        await agent.use_mcp(f"https://mcp.amap.com/mcp?key={os.environ.get('AMAP_API_KEY')}")
        .input("What's the weather like in Shanghai today?")
        .async_start()
    )
    print(result)


asyncio.run(main())
```

`use_mcp(url)` registers all tools the MCP server exposes. The agent then plans tool calls against the union of {`@agent.action_func`, `use_tool`, `use_mcp` tools} as if they were one set.

All tools from one MCP registration share one agent-scoped managed MCP client
session. Stateful servers such as Playwright can therefore navigate through one
Action and inspect or operate the same browser page through later Actions. A
long-lived host should release that agent's ExecutionResource scope when it
retires the agent; the Playwright example below does so explicitly.

## API

| Method | Behavior |
|---|---|
| `await agent.use_mcp(url)` | connect to the server, list tools, register them; returns the agent for chaining |
| `await agent.use_mcp(url, headers={...})` | with custom HTTP headers (auth tokens, etc.) |
| `await agent.use_mcp({"mcpServers": {...}})` | use an MCP config with one or more HTTP or stdio servers |

For the default executor, `headers=` with a URL is normalized to an MCP config
before FastMCP sees it.

```python
await agent.use_mcp(
    "https://example.com/mcp",
    headers={"Authorization": f"Bearer {token}"},
)
```

For local stdio servers, pass MCP config directly:

```python
await agent.use_mcp({
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "./workspace"],
        }
    }
})
```

## Mixing MCP with custom actions

```python
@agent.action_func
async def lookup_internal(id: str):
    """Look up a record in the internal database."""
    ...


await agent.use_mcp("https://example-mcp/server")
agent.use_actions(lookup_internal)

# The model now sees MCP tools + lookup_internal in the same plan
result = await agent.input(question).async_start()
```

There's no precedence between MCP-provided tools and locally-defined actions. The model picks based on names, descriptions, and the prompt context.

## Inspecting what was called

For a request-scoped turn, pass the turn prompt into the action loop to inspect
what tools the model actually invoked:

```python
turn = agent.input("Use the MCP server to answer this question.")
records = agent.get_action_result(prompt=turn.prompt)
for r in records:
    print(r)
```

Action records are also written to `extra.action_logs` (or `extra.tool_logs` on the compat surface).

When an MCP tool returns resource/content blocks or structured
`artifact_refs`/`artifacts`/`file_refs`, Agently preserves those declarations on
the Action record so hosts can inspect `record["artifact_refs"]` instead of
polling output directories. The MCP server must declare the artifact metadata;
Agently does not scan the filesystem to infer undeclared writes.

## Playwright browser E2E

[`examples/action_runtime/2_3_mcp_playwright_e2e_local.py`](../../../examples/action_runtime/2_3_mcp_playwright_e2e_local.py)
runs a complete local Todo E2E. Agently registers Microsoft's Playwright MCP
browser operations as Actions; the host dispatches navigation, typing, clicks,
and snapshots through Action Runtime. It then checks real Action records and
canonical server-side Todo state, without relying on model prose.

The example does not call a model. It requires Node.js 20+, npm, and Google
Chrome. Its tested default MCP package is `@playwright/mcp@0.0.78`; set
`PLAYWRIGHT_MCP_PACKAGE` to test another immutable version deliberately. It
launches a headless, isolated browser and permits browser requests
only to the ephemeral local test origin. If the default `npx` belongs to an older
Node.js installation, point `PLAYWRIGHT_NPX_BIN` at an `npx` installation backed
by Node.js 20+.

The model-autonomous version is
[`examples/action_runtime/2_4_mcp_playwright_agent_qwen.py`](../../../examples/action_runtime/2_4_mcp_playwright_agent_qwen.py).
It lets `qwen3-32b` inspect each accessibility snapshot and independently choose
navigate, type, click, and snapshot Actions. The host supplies no selector or
element answer. Exploratory snapshots are projected to the complete current
page with no target. Type/click calls must contain one model-selected
`[ref=eN]` marker, which the host projects to Playwright's canonical
`target="eN"`; the live Playwright backend still decides whether that ref
belongs to the current page.

The autonomous example uses `structured_plan`, a request allowlist of four
Actions, concurrency one, at most eight planning rounds, and no business retry.
It records model requests, Action input projections, elapsed time, canonical
server state, and resource release. In the observed 2026-08-25 `qwen3-32b` run,
the loop settled in round eight and all seven browser Actions succeeded. This
is model-specific evidence, not an automatic claim for other models or
configurations. Set `QWEN_API_KEY`; `QWEN_BASE_URL` and `QWEN_MODEL` are optional.

Playwright Test is usually simpler for a fully fixed CI regression suite. The
`2_3` example verifies Agently's stateful MCP and Action evidence boundaries;
`2_4` verifies autonomous exploration with a host-owned identity projection.

## Common pitfalls

- **Forgetting `await`**: `use_mcp(...)` is async because it lists tools from the server. Forgetting `await` returns a coroutine and the registration silently doesn't happen.
- **Passing secrets in URLs**: prefer headers and env vars. URL query params end up in logs.
- **Treating MCP as identical to local actions**: hosted MCP servers can be slow or rate-limited. For latency-sensitive or high-volume calls, prefer local action functions.

## See also

- [Action Runtime](action-runtime.md) — `MCPActionExecutor` is one of the bundled executors
- [Tools](tools.md) — `use_mcp(...)` is the same on the compat surface
