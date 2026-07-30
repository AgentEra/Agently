---
title: Agently Docs
description: Agently documentation home. Quickstart, model setup, requests, actions, services, observability, and TriggerFlow.
keywords: Agently, AI agent framework, documentation, quickstart, TriggerFlow
---

# Agently Docs

> Languages: **English** · [中文](../cn/index.md)

Agently is an AI application development framework for teams moving from model prototypes to maintainable applications. It focuses on normalized model requests, stable structured outputs, explicit prompt and response contracts, observable actions, service exposure, observability, and durable signal-driven workflow orchestration.

Use Agently when application code needs to own the AI execution boundary: model requests should return inspectable data, tool calls should leave action records, workflows should expose lifecycle state, and project settings or prompts should be reviewable outside one-off scripts.

This handbook is organized as a learning path. If you have not run a single request yet, start at [Quickstart](start/quickstart.md). If you are integrating Agently into a service, jump to [Async First](start/async-first.md). If you are designing event-driven or long-running flows, go to [TriggerFlow Overview](triggerflow/overview.md).

## Learning path

1. **Get Started** — installation, first request, async-first guidance, project layout
   - [Quickstart](start/quickstart.md)
   - [Async First](start/async-first.md)
   - [Model Setup](start/model-setup.md)
   - [Settings](start/settings.md)
   - [Project Framework](start/project-framework.md)

2. **Make a single request well** — prompt, output schema, validation, response reuse, session memory
   - [Requests Overview](requests/overview.md)
   - [Prompt Management](requests/prompt-management.md)
   - [Schema as Prompt](requests/schema-as-prompt.md)
   - [Output Control](requests/output-control.md)
   - [Model Response](requests/model-response.md)
   - [Session Memory](requests/session-memory.md)
   - [Context Engineering](requests/context-engineering.md)
   - [Task context, files, and records](requests/workspace.md)

3. **Actions** — model-callable actions, built-in capability packages, MCP, managed execution resources, and compatibility surfaces
   - [Actions Overview](actions/overview.md)
   - [Action Runtime](actions/action-runtime.md)
   - [ExecutionResource](actions/execution-environment.md)
   - [Tools Compatibility](actions/tools.md)
   - [MCP](actions/mcp.md)

4. **Knowledge and services** — retrieval-backed answers and HTTP / stream exposure
   - [Knowledge Base](knowledge/knowledge-base.md)
   - [FastAPI Service Exposure](services/fastapi.md)

5. **Observability and development** — observation events, DevTools, and coding-agent guidance
   - [Observability Overview](observability/overview.md)
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

6. **Models** — protocol layers and per-provider recipes
   - [Models Overview](models/overview.md)
   - [OpenAICompatible](models/openai-compatible.md) · [AnthropicCompatible](models/anthropic-compatible.md)
   - [Providers](models/providers/)

7. **TriggerFlow** — orchestration, lifecycle, state, persistence
   - [Overview](triggerflow/overview.md)
   - [Lifecycle](triggerflow/lifecycle.md)
   - [State and Resources](triggerflow/state-and-resources.md)
   - [Events and Streams](triggerflow/events-and-streams.md)
   - [Patterns](triggerflow/patterns.md) · [Sub-Flow](triggerflow/sub-flow.md)
   - [Persistence and Blueprint](triggerflow/persistence-and-blueprint.md)
   - [Pause and Resume](triggerflow/pause-and-resume.md)
   - [Model Integration](triggerflow/model-integration.md)
   - [Compatibility](triggerflow/compatibility.md)

8. **Playbooks and case studies** — opinionated combinations of the above
   - [Playbooks](playbooks/overview.md)
   - [Case Studies](case-studies/overview.md)

9. **Architecture and reference**
   - [Extension Boundaries](architecture/extension-boundaries.md)
   - [Capability Map](reference/capability-map.md)
   - [Execution Layer Selection](reference/execution-layer-selection.md)
   - [Glossary](reference/glossary.md)

## Community

- WeChat group: <https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf>
- Discussions: <https://github.com/AgentEra/Agently/discussions>
- Issues: <https://github.com/AgentEra/Agently/issues>
- Twitter / X: <https://x.com/AgentlyTech>
