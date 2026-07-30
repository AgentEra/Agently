---
title: Agently 文档
description: Agently 文档首页。快速开始、模型设置、单次请求、Action、服务、观测与 TriggerFlow。
keywords: Agently, AI agent 框架, 文档, 快速开始, TriggerFlow
---

# Agently 文档

> 语言：[English](../en/index.md) · **中文**

Agently 是一个面向 AI 应用开发的框架，服务于团队从模型原型走向可维护应用的阶段。它关注归一化模型请求、稳定的结构化输出、Prompt 与响应契约、可观测的 Action 调用、服务化暴露、运行时观测，以及可持久化的信号驱动流程编排。

当应用代码需要清晰拥有 AI 执行边界时，适合使用 Agently：模型请求应返回可检查的数据，工具调用应留下 Action 记录，工作流应暴露生命周期状态，项目设置与 Prompt 应能脱离一次性脚本被审阅和复用。

本手册按学习路径组织。如果你还没有跑过一次最小请求，请从 [快速开始](start/quickstart.md) 开始。如果你正在把 Agently 做成服务，请直接看 [Async First](start/async-first.md)。如果你要设计事件驱动或长跑流程，请去 [TriggerFlow 概览](triggerflow/overview.md)。

## 学习路径

1. **入门** — 安装、首次请求、Async 优先策略、项目结构
   - [快速开始](start/quickstart.md)
   - [Async First](start/async-first.md)
   - [模型设置](start/model-setup.md)
   - [设置层级](start/settings.md)
   - [项目结构](start/project-framework.md)

2. **把单次请求做好** — Prompt、输出 schema、校验、响应复用、会话记忆
   - [Requests 概览](requests/overview.md)
   - [Prompt 管理](requests/prompt-management.md)
   - [Schema as Prompt](requests/schema-as-prompt.md)
   - [输出控制](requests/output-control.md)
   - [模型响应](requests/model-response.md)
   - [会话记忆](requests/session-memory.md)
   - [Context Engineering](requests/context-engineering.md)
   - [任务上下文、文件与记录](requests/workspace.md)

3. **Action** — 可被模型调用的动作、内置能力包、MCP、托管执行资源与兼容入口
   - [Actions 概览](actions/overview.md)
   - [Action Runtime](actions/action-runtime.md)
   - [ExecutionResource](actions/execution-environment.md)
   - [工具兼容](actions/tools.md)
   - [MCP](actions/mcp.md)

4. **知识与服务** — 检索增强回答、HTTP 与流式服务暴露
   - [知识库](knowledge/knowledge-base.md)
   - [FastAPI 服务封装](services/fastapi.md)

5. **观测与开发** — observation event、DevTools 与 coding-agent 指引
   - [观测概览](observability/overview.md)
   - [Event Center](observability/event-center.md)
   - [DevTools](observability/devtools.md)
   - [Coding Agents](development/coding-agents.md)
   - [Agently 4.1.4.5 Release Notes](development/release-notes-4.1.4.5.md)
   - [Agently 4.1.4.4 Release Notes](development/release-notes-4.1.4.4.md)
   - [Agently 4.1.4.3 Release Notes](development/release-notes-4.1.4.3.md)
   - [Agently 4.1.4.2 Release Notes](development/release-notes-4.1.4.2.md)
   - [Agently 4.1.4 Development Notes](development/release-notes-4.1.4.md)
   - [Agently 4.1.3.9 Release Notes](development/release-notes-4.1.3.9.md)
   - [Agently 4.1.3.8 Release Notes](development/release-notes-4.1.3.8.md)
   - [Agently 4.1.3.7 Release Notes](development/release-notes-4.1.3.7.md)
   - [Agently 4.1.3.6 Release Notes](development/release-notes-4.1.3.6.md)
   - [Agently 4.1.3.5 Release Notes](development/release-notes-4.1.3.5.md)
   - [Agently 4.1.3.4 Release Notes](development/release-notes-4.1.3.4.md)
   - [Agently 4.1.3.3 Release Notes](development/release-notes-4.1.3.3.md)
   - [Agently 4.1.3.2 Release Notes](development/release-notes-4.1.3.2.md)
   - [Agently 4.1.3.1 Release Notes](development/release-notes-4.1.3.1.md)
   - [Agently 4.1.3 Release Notes](development/release-notes-4.1.3.md)
   - [Release Workflows](development/release-workflows.md)

6. **模型** — 协议层与各 provider 配置
   - [模型概览](models/overview.md)
   - [OpenAICompatible](models/openai-compatible.md) · [AnthropicCompatible](models/anthropic-compatible.md)
   - [Providers](models/providers/)

7. **TriggerFlow** — 编排、生命周期、状态、持久化
   - [概览](triggerflow/overview.md)
   - [Lifecycle](triggerflow/lifecycle.md)
   - [State 与 Resources](triggerflow/state-and-resources.md)
   - [事件与流](triggerflow/events-and-streams.md)
   - [模式](triggerflow/patterns.md) · [Sub-Flow](triggerflow/sub-flow.md)
   - [持久化与 Blueprint](triggerflow/persistence-and-blueprint.md)
   - [Pause 与 Resume](triggerflow/pause-and-resume.md)
   - [模型集成](triggerflow/model-integration.md)
   - [兼容](triggerflow/compatibility.md)

8. **Playbook 与案例** — 上述能力的组合用法
   - [Playbooks](playbooks/overview.md)
   - [案例](case-studies/overview.md)

9. **架构与参考**
   - [扩展边界](architecture/extension-boundaries.md)
   - [能力地图](reference/capability-map.md)
   - [执行层选择](reference/execution-layer-selection.md)
   - [术语表](reference/glossary.md)

## 社区

- 微信群：<https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf>
- 讨论：<https://github.com/AgentEra/Agently/discussions>
- Issues：<https://github.com/AgentEra/Agently/issues>
- Twitter / X：<https://x.com/AgentlyTech>
