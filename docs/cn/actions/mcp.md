---
title: MCP
description: 从 Agently agent 调 MCP 服务。
keywords: Agently, MCP, Model Context Protocol, use_mcp, MCPActionExecutor
---

# MCP

> 语言：[English](../../en/actions/mcp.md) · **中文**

MCP（Model Context Protocol）向 AI agent 暴露外部工具。Agently 通过
`MCPActionExecutor` 把 MCP 服务接入 action runtime，所以模型把 MCP tool 与你的
`@agent.action_func` action 看作同一接口。

服务集成优先使用 URL / Streamable HTTP MCP endpoint；本地开发、桌面客户端或单用户本地
server 使用 stdio command config。SSE endpoint 只作为 legacy 兼容路径。

## 最小例子

FastMCP 是可选依赖。4.1.4.10 候选的新版适配可安装 `fastmcp==4.1.0`，使用 MCP SDK v2；
FastMCP 3.x 与其解析器选择的 SDK v1 仍兼容。不要单独把 `mcp` 升级到 FastMCP 要求的
范围之外。适配器直接读取 SDK v2 snake_case 字段，不依赖上游弃用的 camelCase 兼容桥，
保留服务端与 host 标签，并在内容值中保留 `mimeType` 等 MCP wire 别名。
空结构化对象保持 `{}`；服务端错误即使没有文本块也保持失败。这些是协议适配保证，
模型选择与回答质量仍需另外评估。

不调用模型的[协议冒烟](../../../examples/action_runtime/2_5_mcp_protocol_compat.py)
通过真实本地 MCP 服务检查 Schema、两次有状态调用与资源释放。一次注册共享一个
agent-scope 托管 client；是否存在服务端 session 取决于上游协议。FastMCP 4 的交互式
工具和后台任务扩展需要各自的 host 集成，本次适配不新增这些业务流程。
既有 `runtime.httpx_log_level` 同时控制 FastMCP 4 的 `httpx2` / `httpcore2` logger，
不改变工具权限或重试政策。

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
        .input("今天上海天气怎么样？")
        .async_start()
    )
    print(result)


asyncio.run(main())
```

`use_mcp(url)` 注册 MCP 服务暴露的所有工具。agent 接着把它们作为 {`@agent.action_func`、`use_tool`、`use_mcp` 工具} 的并集来规划，对模型像同一组。

同一次 MCP 注册得到的所有工具共享一个 agent-scope 托管 MCP client 会话。因此像 Playwright
这类有状态服务可以先通过一个 Action 导航，再由另一个 Action 读取或操作同一浏览器页面。长驻
host 在淘汰 agent 时应释放对应的 agent ExecutionResource scope；下方 Playwright 案例包含显式释放。

## API

| 方法 | 行为 |
|---|---|
| `await agent.use_mcp(url)` | 连接服务、列工具、注册；返回 agent 用于链式调用 |
| `await agent.use_mcp(url, headers={...})` | 带自定义 HTTP header（auth token 等） |
| `await agent.use_mcp({"mcpServers": {...}})` | 使用包含一个或多个 HTTP / stdio server 的 MCP config |

默认 executor 会先把 URL + `headers=` 规范化为 MCP config，再交给 FastMCP。

```python
await agent.use_mcp(
    "https://example.com/mcp",
    headers={"Authorization": f"Bearer {token}"},
)
```

本地 stdio server 直接传 MCP config：

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

## 与自定义 action 混用

```python
@agent.action_func
async def lookup_internal(id: str):
    """在内部数据库查记录。"""
    ...


await agent.use_mcp("https://example-mcp/server")
agent.use_actions(lookup_internal)

# 模型现在在同一 plan 里看到 MCP tool + lookup_internal
result = await agent.input(question).async_start()
```

MCP 提供的 tool 与本地 action 之间没有优先级。模型按名、描述、prompt 上下文来选。

## 看实际调了什么

对于 request-scoped turn，把 turn prompt 传给 action loop 来查看模型实际调了哪些工具：

```python
turn = agent.input("使用 MCP server 回答这个问题。")
records = agent.get_action_result(prompt=turn.prompt)
for r in records:
    print(r)
```

action 记录也写到 `extra.action_logs`（兼容入口下是 `extra.tool_logs`）。

当 MCP tool 返回 resource/content block，或结构化
`artifact_refs` / `artifacts` / `file_refs` 时，Agently 会把这些声明保留在
Action record 上，host 可以读取 `record["artifact_refs"]`，不需要轮询输出目录。
MCP server 必须显式声明 artifact metadata；Agently 不通过扫描文件系统推断未声明写入。

## Playwright 浏览器 E2E

[`examples/action_runtime/2_3_mcp_playwright_e2e_local.py`](../../../examples/action_runtime/2_3_mcp_playwright_e2e_local.py)
展示了一个完整的本地 Todo E2E：Agently 把 Microsoft Playwright MCP 的浏览器操作注册为
Action，host 通过 Action Runtime 依次执行导航、填写、点击和快照，最后检查真实 Action 记录和
服务端 Todo 状态。测试结论不依赖模型自述。

该案例不调用模型，需要 Node.js 20+、npm 和 Google Chrome。已验证的默认 MCP 包版本是
`@playwright/mcp@0.0.78`，可通过 `PLAYWRIGHT_MCP_PACKAGE` 显式测试其他不可变版本。浏览器以 headless、isolated
模式启动，并且只允许访问本次测试启动的临时本地 origin。如果默认 `npx` 属于较旧的 Node.js，
可把 `PLAYWRIGHT_NPX_BIN` 指向 Node.js 20+ 配套的 `npx`。

模型自主探索版本见
[`examples/action_runtime/2_4_mcp_playwright_agent_qwen.py`](../../../examples/action_runtime/2_4_mcp_playwright_agent_qwen.py)。
它让 `qwen3-32b` 根据每轮 accessibility snapshot 自主选择 navigate、type、click 和 snapshot
Action；host 不提供 selector 或元素答案。探索 snapshot 统一投影为无 target 的完整当前页面，
type/click 则只把模型选中的唯一 `[ref=eN]` 行校验并投影为 Playwright 的 canonical
`target="eN"`。Playwright 活浏览器仍负责判断 ref 是否属于当前页面。

该自主案例使用 `structured_plan`、四个 Action 的 request allowlist、并发 1、最多 8 轮，无业务
重试，并记录模型请求数、Action 输入投影、耗时、最终服务端状态和资源释放。2026-08-25 的真实
`qwen3-32b` 运行在第 8 轮结束，7 个浏览器 Action 全部成功；这是一条模型特定证据，不自动代表
其他模型或配置。运行前设置 `QWEN_API_KEY`，可选设置 `QWEN_BASE_URL` 和 `QWEN_MODEL`。

对于完全固定的 CI 回归，直接使用 Playwright Test 通常更简单；`2_3` 的重点是 Agently MCP
状态会话与 Action 证据，`2_4` 的重点是经过 host 身份边界校验的模型自主探索。

## 常见错误

- **忘 `await`**：`use_mcp(...)` 是 async 因为要从服务列工具。忘 `await` 返回协程，注册悄悄不发生。
- **URL 里传密钥**：优先 header 与环境变量。URL query 参数会进日志。
- **把 MCP 当本地 action 一样用**：hosted MCP 服务可能慢或限速。延迟敏感或高频调用优先本地 action。

## 另见

- [Action Runtime](action-runtime.md) —— `MCPActionExecutor` 是内置 executor 之一
- [工具](tools.md) —— 兼容入口下 `use_mcp(...)` 一样
