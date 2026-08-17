---
title: Agently 4.1.4.6 发布说明
description: 标准包版本入口、统一 reasoning 生命周期，以及保持 live resource 的 TriggerFlow sub-flow。
keywords: Agently, 4.1.4.6, version, reasoning, thinking, DeepSeek, Anthropic, Responses, TriggerFlow
---

# Agently 4.1.4.6 发布说明

Agently 4.1.4.6 是基于 4.1.4.5 的聚焦兼容性补丁：

- 通过标准 `__version__` 名称公开包版本；
- 统一 OpenAI-compatible Chat Completions、Anthropic-compatible Messages 和
  Responses adapter 的生成 reasoning 生命周期；
- 让公开 compatible-request 重试策略在 Chat Completions、Anthropic Messages 和
  Responses 中独占物理 SSE 重连；
- live runtime resource 穿过 TriggerFlow sub-flow 边界时保持对象身份。

[#342](https://github.com/AgentEra/Agently/issues/342) 管理的 sandbox provider
草案不进入本次发布。

## 核心变化

| 区域 | 变化 | 推荐用法 | 兼容性 / 风险 | 证据 |
|---|---|---|---|---|
| 包版本 | 新增标准 `agently.__version__` 与 `Agently.__version__`，移除 4.1.4.5 误引入的 `version` 属性 | 使用任一 `__version__` 入口检查版本 | 仅对已经采用 4.1.4.5 错误属性的代码构成有意纠正 | 包/版本测试、wheel 构建与全新环境安装 smoke |
| Compatible model reasoning | Chat Completions、Anthropic Messages 与 Responses 将模型生成 reasoning 统一收敛为 `reasoning_delta`、`reasoning_done` 和 provider-native `original_done` | Provider 控制项继续放在 `request_options`，消费公开 reasoning 生命周期 | 响应归一化为增量能力；provider 请求参数不变 | 三类 adapter 套件及真实 `deepseek-v4-flash` Chat Completions、Anthropic Messages 探针 |
| Compatible SSE retry | 移除 transport 隐式重连，让三个 adapter 的 `request_retry` / `AttemptRunner` 独占物理请求重试 | 需要单次物理连接时使用 `request_retry=False` 或 `max_attempts=1`；不能清空临时输出时使用 `after_output=False` | 默认有界重放保留；公开边界避免请求漏计和输出混合 | 物理连接、retry boundary、attempt index、`[DONE]` 与 EDA TransportGuard 回归 |
| TriggerFlow sub-flow resource | Resource capture 与隔离 child template 保持 live runtime resource 对象身份 | client、lock、callback、service 通过 `resources` 传递；恢复保存的 root execution 后重新注入 | Resource 按身份共享；普通 input/value capture 仍复制隔离 | TriggerFlow resource identity 测试与 sub-flow foundation example |
| Sandbox provider 贡献 | 4.1.4.6 不合入 sandbox provider 草案 | 通过 issue #342 集中协调与整合贡献者工作 | 明确延期；本版本不改变 runtime 或依赖 | Issue #342 与未变化的 optional-provider surface |

## 标准包版本入口

可以使用以下任一公开入口：

```python
import agently
from agently import Agently

assert agently.__version__ == "4.1.4.6"
assert Agently.__version__ == agently.__version__
```

4.1.4.5 误引入的 `agently.version` 和 `Agently.version` 不作为 alias 保留。

## 统一 reasoning 生命周期

Provider 特有请求参数继续放在 `request_options` 下，并原样透传。例如
OpenAI-compatible endpoint 可以这样启用 thinking：

```yaml
plugins:
  ModelRequester:
    OpenAICompatible:
      model: deepseek-v4-flash
      request_options:
        thinking:
          type: enabled
```

模型生成的 reasoning 统一映射到以下公开响应生命周期：

- `reasoning_delta`：流式 reasoning 增量；
- `reasoning_done`：只发送一次的最终 reasoning；
- `original_done`：保留 provider 原始完整响应。

OpenAI-compatible Chat Completions 现在会保留非流式和流式
`reasoning_content`。Anthropic-compatible response 会正确收敛 `thinking`
block、signature、tool-use continuation，以及空或非空的 terminal reasoning
事件。Responses adapter 会映射 reasoning summary 的 delta/done 事件和完整输出
中的 reasoning item，同时不会把 `reasoning.effort`、`reasoning.summary` 等请求
配置误当成生成内容。

这是 adapter 层的通用规范化，不是 DeepSeek 特例；使用相应兼容协议的 provider
会获得相同的生命周期行为。

## 唯一、可观测的 SSE 重试生命周期

`OpenAICompatible`、`AnthropicCompatible` 和 `OpenAIResponsesCompatible` 的
`request_retry` 现在管理每一次物理流式请求。transport 不再在一个公开 attempt 内
额外执行 stamina 重连。因此 `request_retry=False` 和 `max_attempts=1` 都最多只建立
一次物理 SSE 连接；`after_output=False` 也会阻止 partial 输出后的重放。启用重放时，
每个替换连接都对应公开递增的 `attempt_index` 和 retry boundary。

移除内部重连也同时移除了异步事件循环中的同步 SSE retry-delay sleep。`[DONE]`
仍是逻辑终止标记；`[DONE]` 前断流仍作为 transport failure 交给公开策略处理。

## TriggerFlow sub-flow runtime resource

直接的 `resources -> resources` capture 会按对象身份传递 live object：

```python
parent_flow.to_sub_flow(
    child_flow,
    capture={"resources": {"service": "resources.service"}},
)

execution = parent_flow.create_execution(
    auto_close=False,
    runtime_resources={"service": live_service},
)
```

隔离的 child-flow template 也会按对象身份继承其 flow-level runtime resource。
client、callback、lock、event 和其他 live handle 不会被 deep-copy。普通
input/value capture 仍然复制隔离，因此该修复不会把一般 sub-flow 数据变成共享
可变状态。

保存 execution 时仍不会序列化 live resource；恢复 root execution 时，host
必须重新注入所需 resource。

## 兼容性

- Python：`>=3.10`
- Agently-Stage：`>=0.3.5,<0.4.0`
- 推荐 Agently DevTools：`>=0.1.10,<0.2.0`
- Skills authoring protocol：`agently-skills.authoring.v2`
- DevTools observation protocol：`agently-devtools.observation-runtime.v1`

4.1.4.5 引入的可选长输出续写和 Stage-backed TriggerFlow task ownership 保持不变。
