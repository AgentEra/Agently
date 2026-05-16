---
title: Execution Environment
description: Action 与 TriggerFlow 的托管执行依赖。
keywords: Agently, ExecutionEnvironment, Action, TriggerFlow, sandbox, MCP, runtime_resources
---

# Execution Environment

> 语言：[English](../../en/actions/execution-environment.md) · **中文**

Execution Environment 是框架级执行环境层，用来在 action 或 workflow step
真正执行前准备、复用和释放托管执行依赖。

它负责 MCP transport、命令 runner、sandbox、browser、SQLite connection 和外部进程
runner 等资源的生命周期和 policy。Action 与 TriggerFlow 可以声明需要这些环境，但不拥有环境生命周期。

## 面向对象

多数应用开发者不应该从这里开始。优先使用描述意图的 built-in actions 和
Agent Component helpers，例如启用 Python、shell、workspace、MCP、SQLite、
vector store 或 coding workspace 能力。

在这些情况下阅读本页：

- 你在写依赖托管 live resource 的自定义 `ActionExecutor`
- 你在写 `ExecutionEnvironmentProvider` 插件
- 你在 review Action 或 TriggerFlow 如何接收托管资源
- 你在设计需要 sandbox、process、MCP、client、credential 或 cleanup 生命周期的新 built-in capability

不要把 `Agently.execution_environment` 暴露成默认应用开发心智。它是更高层能力背后的 core lifecycle 层。

## 所在位置

```text
Agent Component / built-in Action / custom Action / TriggerFlow / Skills plan
        |
        v
ActionSpec.execution_environments or TriggerFlow execution requirements
        |
        v
ExecutionEnvironmentManager
        |
        v
ExecutionEnvironmentProvider
        |
        v
managed handle / live resource
```

V1 全局 manager 暴露为：

```python
from agently import Agently

Agently.execution_environment
```

多数业务代码不需要直接调用 manager。内置 MCP、Bash、Python、Node.js、Docker、
Browser、SQLite action 可以声明自己的 requirement，Action dispatcher 在 executor
调用前自动 ensure。

更完整的 ownership 模型见
[Architecture / 扩展边界](../architecture/extension-boundaries.md)。

## 内置行为

内置 provider：

| Kind | 使用方 | 托管资源 |
|---|---|---|
| `mcp` | `agent.use_mcp(...)` / MCP actions | MCP transport resource |
| `bash` | `agent.enable_shell(...)` / Bash sandbox actions | 配置后的命令 runner |
| `python` | `agent.enable_python(...)` / Python sandbox actions | 配置后的 Python sandbox |
| `node` | `agent.enable_nodejs(...)` / Node.js executor actions | 配置后的 Node.js runner |
| `docker` | Docker executor actions | Docker CLI runner |
| `browser` | 选择托管 browser resource 的 Browse actions | 托管 browser/page/session wrapper |
| `sqlite` | `agent.enable_sqlite(...)` / SQLite executor actions | SQLite connection |

Search 故意不放在这里。它是无状态的 Action-native capability package；proxy、timeout、
backend、region 属于 Search package/executor 配置，不属于 Execution Environment。

这些 provider 是低层环境实现。面向用户的能力通常应该暴露为 Action，场景快捷入口应该通过 Agent Component 或未来的 `agent.enable_*` helpers 暴露。

Action 执行流：

```text
ActionCall
  -> resolve ActionSpec
  -> ensure ActionSpec.execution_environments
  -> 把 execution_environment_resources 注入 action_call
  -> ActionExecutor.execute(...)
  -> 释放 action_call scope 的 handles
```

自定义 `ActionExecutor.execute(...)` 签名不变。托管 handle 会通过
`action_call["execution_environment_handles"]` 传入，live resource 会通过
`action_call["execution_environment_resources"]` 传入。

## TriggerFlow

TriggerFlow 仍然使用 `runtime_resources` 作为 execution-local live resource 的兼容入口。
Execution Environment 不重命名也不替代这个 API。

可以在创建或启动 execution 时传入托管 requirement：

```python
execution = flow.create_execution(
    execution_environments=[
        {
            "kind": "python",
            "scope": "execution",
            "resource_key": "sandbox",
        }
    ],
)
```

manager 会 ensure 资源，把它注入 execution-local resources，并在 execution close 时释放。
手动传入的 `runtime_resources={...}` 仍是 unmanaged，不参与 manager 的 health check
或自动释放。

## 直接 Manager API

这组 API 面向框架、action 和 plugin 开发者。

manager 支持：

```python
Agently.execution_environment.declare(requirement)
Agently.execution_environment.ensure(requirement_or_id)
await Agently.execution_environment.async_ensure(requirement_or_id)
Agently.execution_environment.release(handle_or_id)
Agently.execution_environment.release_scope("session", owner_id)
Agently.execution_environment.inspect(id)
Agently.execution_environment.list(scope="execution")
Agently.execution_environment.set_decision_handler(handler)
```

声明是 lazy 的：只校验和记录 requirement，不启动任何东西。`ensure(...)` 会在 policy
与 approval 允许的情况下启动或复用 handle。复用 ready handle 前，manager 会调用
`provider.async_health_check(handle)`。健康则 `ref_count + 1` 后复用；不健康则发出
`execution_environment.unhealthy`，释放旧 handle，再 ensure 一个新 handle。V2 不加入后台
health scheduler、lease TTL 或自动 reconnect loop。

如果你在开发应用，应该先检查是否已有 built-in action 或 Agent Component 暴露了你需要的能力。

## Observation

manager 发出 `execution_environment.*` 事件：

- `execution_environment.declared`
- `execution_environment.approval_required`
- `execution_environment.ensuring`
- `execution_environment.ready`
- `execution_environment.unhealthy`
- `execution_environment.releasing`
- `execution_environment.released`
- `execution_environment.failed`

payload 只包含稳定 id 与状态元信息，不能包含原始凭证、环境变量、命令 secret 或 live resource 对象。

## Examples

可运行示例见
[`examples/execution_environment`](../../../examples/execution_environment/README.md)。
建议先看本地 `agent.enable_python(...)` quickstart，再看 Ollama 和 DeepSeek
驱动的模型决策示例。TriggerFlow 示例面向需要托管 execution-local resource 的
workflow 或框架开发者。

## 另见

- [Action Runtime](action-runtime.md)
- [MCP](mcp.md)
- [TriggerFlow State 与 Resources](../triggerflow/state-and-resources.md)
