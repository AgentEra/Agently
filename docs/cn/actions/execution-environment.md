---
title: Execution Resource
description: Action 与 TriggerFlow 的托管执行资源。
keywords: Agently, ExecutionResource, Action, TriggerFlow, sandbox, MCP, runtime_resources
---

# Execution Resource

> 语言：[English](../../en/actions/execution-environment.md) · **中文**

> 在 4.1.3.8 的 TaskWorkspace/ActionRuntime 边界重构中更名：托管的活动资源接缝现在
> 称为 **ExecutionResource**（`ExecutionResourceManager`、
> `ExecutionResourceProvider`、`Agently.execution_resource`），旧的
> `ExecutionEnvironment*` 名称已移除。本页保留原 URL 以保持链接稳定。

Execution Resource 是框架级执行资源层，用来在 action 或 workflow step
真正执行前准备、复用和释放托管执行资源。

它负责 MCP transport、命令 runner、sandbox、browser、SQLite connection 和外部进程
runner 等资源的生命周期和 policy。Action 与 TriggerFlow 可以声明需要这些环境，但不拥有环境生命周期。

## 面向对象

多数应用开发者不应该从这里开始。优先使用描述意图的 built-in actions 和
Agent Component helpers，例如启用 Python、shell、workspace、MCP、SQLite、
vector store 或 coding workspace 能力。

在这些情况下阅读本页：

- 你在写依赖托管 live resource 的自定义 `ActionExecutor`
- 你在写 `ExecutionResourceProvider` 插件
- 你在 review Action 或 TriggerFlow 如何接收托管资源
- 你在设计需要 sandbox、process、MCP、client、credential 或 cleanup 生命周期的新 built-in capability

不要把 `Agently.execution_resource` 暴露成默认应用开发心智。它是更高层能力背后的 core lifecycle 层。

## 所在位置

```text
Agent Component / built-in Action / custom Action / TriggerFlow / Skills plan
        |
        v
ActionSpec.execution_resources or TriggerFlow execution requirements
        |
        v
ExecutionResourceManager
        |
        v
ExecutionResourceProvider
        |
        v
managed handle / live resource
```

V1 全局 manager 暴露为：

```python
from agently import Agently

Agently.execution_resource
```

多数业务代码不需要直接调用 manager。内置 MCP、Bash、代码执行、Docker、
Browser、SQLite action 可以声明自己的 requirement，Action dispatcher 在 executor
调用前自动 ensure。

更完整的 ownership 模型见
[Architecture / 扩展边界](../architecture/extension-boundaries.md)。

## 内置行为

内置 provider：

| Kind | 使用方 | 托管资源 |
|---|---|---|
| `mcp` | `agent.use_mcp(...)` / MCP actions | MCP transport resource |
| `bash` | `sandbox="trusted_local"` shell actions | 配置后的本地命令 runner |
| `docker` | 隔离 shell actions、direct Docker Actions，以及一个 `code_execution` provider 候选 | Docker CLI runner 与镜像 provisioning |
| `code_execution` | `agent.enable_python(...)`、`agent.enable_nodejs(...)`、`agent.enable_code_runtime(...)` 与已授权 Skill script Actions | provider-neutral、Workspace-bound 执行；内置包括 Docker、可选的 gVisor/runsc、可选的 macOS Seatbelt 与显式无防护 `trusted_local` fallback |
| `browser` | 选择托管 browser resource 的 Browse actions | 托管 browser/page/session wrapper |
| `sqlite` | `agent.enable_sqlite(...)` / SQLite executor actions | SQLite connection |

Search 故意不放在这里。它是无状态的 Action-native capability package；proxy、timeout、
backend、region 属于 Search package/executor 配置，不属于 ExecutionResource。

这些 provider 是低层环境实现。面向用户的能力通常应该暴露为 Action，场景快捷入口应该通过 Agent Component 的 `agent.enable_*` helpers 暴露。Python 与 Node.js helper 只是统一 `kind="code_execution"` 契约上的语言快捷入口，并不是独立 provider；Python、Node.js、Go、C++ 的差异由语言 adapter 负责。provider probe 按配置顺序选择第一个实际安装且符合要求的执行机制，硬性隔离和 Workspace 能力要求仍然 fail closed，`trusted_local` 始终明确标记为无隔离执行。

Action 执行流：

```text
ActionCall
  -> resolve ActionSpec
  -> 按需签发 TaskWorkspace access grant
  -> probe/select 并 ensure ActionSpec.execution_resources
  -> 把 execution_resource_resources 注入 action_call
  -> 把不可变 code bundle 落到 TaskWorkspace
  -> ActionExecutor.execute(...)
  -> 收集声明的输出
  -> 释放 action_call scope 的 handles
  -> 关闭 Workspace grant
```

自定义 `ActionExecutor.execute(...)` 签名不变。托管 handle 会通过
`action_call["execution_resource_handles"]` 传入，live resource 会通过
`action_call["execution_resource_resources"]` 传入。

### 有序 code-execution providers

provider 优先级可用字符串或候选描述符配置。描述符 config 只对该候选合并：

```python
agent.settings.set(
    "code_execution.providers",
    [
        {"provider_id": "preferred-provider", "config": {"profile": "strict"}},
        "docker",
    ],
)
agent.enable_code_runtime(language="go")
```

`trusted_local` 直接使用宿主 toolchain，没有隔离，只接受 snapshot grant。它需要显式
host 授权，且不能满足 `isolation="required"`。因此 `unsafe_fallback=True` 必须同时
显式选择 `isolation="preferred"` 或 `"none"`，不能被隐式选中。

公开的 `isolation=` 参数是选择策略，不是 provider 能力标签。`code_execution`
provider 必须报告具体布尔隔离轴：进程 containment、宿主文件系统限制、提权阻断和
syscall 限制。required isolation 必须满足全部请求轴；preferred isolation 会先在有序
候选集中寻找完整匹配，找不到时才使用其他合格 fallback，并在 handle metadata 中记录
该 fallback。provider 名称或 `"required"` 之类字符串都不是安全证据。

### gVisor Docker runtime

当宿主 Docker daemon 已配置 `runsc` runtime 时，可显式选择 `gvisor`：

```python
agent.enable_python(sandbox="gvisor")
```

该选择只使用可选的 `gvisor` provider。handle 就绪前，Agently 会验证 Docker
可用性、确认 daemon 已注册 `runsc`、按既有镜像策略准备镜像，并通过有界的
`runsc` 容器 probe 验证实际可执行。runtime 缺失、注册表格式错误、未注册或实际
无法执行时都会 fail closed；显式选择不会回退到 Docker/runc、`auto` 或
`trusted_local`。经验证的 active runtime 会保留在 handle 与 code-execution
result metadata 中。gVisor 不会增加默认 import 依赖，也不会把不安全 Docker
参数解释为更强的安全证据。

### macOS Seatbelt

在 macOS 上可以显式选择可选的 Seatbelt provider：

```python
agent.enable_python(sandbox="seatbelt")
```

Seatbelt 使用系统 `sandbox-exec`，默认禁止网络，并且所有可写文件系统规则都只从
TaskWorkspace grant 派生。它不接受 raw SBPL 或额外宿主写路径，也不会回退到 Docker
或 `trusted_local`。为了兼容宿主 toolchain 与动态库，初版 profile 允许较宽的宿主读取，
因此会如实上报 `host_filesystem_restricted=false`，helper 使用 preferred 而不是 required
isolation；需要宿主读取隔离时应选择 Docker/gVisor。

代码请求最多声明 128 个 expected outputs；每条路径都有长度边界、必须规范化并位于
`output/` 下，缺少任一声明制品都会使 Action 失败。stdout/stderr 有界保留，取消会终止
所拥有的进程组或容器；资源 release 失败会把原本成功的 Action 改为 error，不会报告
虚假成功。

外部隔离实现通过同一个 `ExecutionResourceProvider` seam 注册。见
[Code Execution Provider 迁移](../development/code-execution-provider-migration.md)。

## TriggerFlow

TriggerFlow 仍然使用 `runtime_resources` 作为 execution-local live resource 的兼容入口。
ExecutionResource 不重命名也不替代这个 API。

可以在创建或启动 execution 时传入托管 requirement：

```python
execution = flow.create_execution(
    execution_resources=[
        {
            "kind": "custom_runtime",
            "provider_id": "my-runtime-provider",
            "scope": "execution",
            "resource_key": "runtime",
        }
    ],
)
```

宿主注册指定 provider 后，manager 会 ensure 资源，把它注入 execution-local resources，
并在 execution close 时释放。代码执行本身仍应通过 Workspace-bound CodeExecution Action
调用，使 bundle 落地与输出读回继续留在 Action Runtime 链路中。手动传入的
`runtime_resources={...}` 仍是 unmanaged，不参与 manager 的 health check 或自动释放。

## 直接 Manager API

这组 API 面向框架、action 和 plugin 开发者。

manager 支持：

```python
Agently.execution_resource.declare(requirement)
Agently.execution_resource.ensure(requirement_or_id)
await Agently.execution_resource.async_ensure(requirement_or_id)
Agently.execution_resource.release(handle_or_id)
Agently.execution_resource.release_scope("session", owner_id)
Agently.execution_resource.inspect(id)
Agently.execution_resource.list(scope="execution")
Agently.policy_approval.register_handler("my_handler", handler)
Agently.configure_policy_approval(handler="my_handler")
Agently.set_settings("access_control_policy.auto_allow", True)
```

声明是 lazy 的：只校验和记录 requirement，不启动任何东西。`ensure(...)` 会在 policy
与 approval 允许的情况下启动或复用 handle。approval 由框架全局
`Agently.policy_approval` handler 决定。默认 `input_timeout_fail` 只会在交互式 CLI
中提示输入，并在超时后失败；非交互服务环境会立即失败。包裹 TriggerFlow execution
的服务应注册自己的 handler，例如写入 pending approval 后用 `continue_with(...)` 恢复。
可信宿主可以通过 settings 设置 `access_control_policy.auto_allow=True` 来自动批准
policy gate；这不会绕过 requirement policy 中的 provider、sandbox、路径、命令或
网络约束。
复用 ready handle 前，manager 会调用
`provider.async_health_check(handle)`。健康则 `ref_count + 1` 后复用；不健康则发出
`execution_resource.unhealthy`，释放旧 handle，再 ensure 一个新 handle。V2 不加入后台
health scheduler、lease TTL 或自动 reconnect loop。

如果你在开发应用，应该先检查是否已有 built-in action 或 Agent Component 暴露了你需要的能力。

## Observation

manager 发出 `execution_resource.*` 事件：

- `execution_resource.declared`
- `execution_resource.approval_required`
- `execution_resource.ensuring`
- `execution_resource.ready`
- `execution_resource.unhealthy`
- `execution_resource.releasing`
- `execution_resource.released`
- `execution_resource.failed`

payload 只包含稳定 id 与状态元信息，不能包含原始凭证、环境变量、命令 secret 或 live resource 对象。

## Examples

可运行示例见
[`examples/execution_resource`](../../../examples/execution_resource/README.md)。
建议先看本地 `agent.enable_python(...)` quickstart，再看 Ollama、DeepSeek 和常用语言
code runtime 示例。TriggerFlow 示例面向需要托管 execution-local resource 的 workflow
或框架开发者。

## 另见

- [Action Runtime](action-runtime.md)
- [MCP](mcp.md)
- [TriggerFlow State 与 Resources](../triggerflow/state-and-resources.md)
