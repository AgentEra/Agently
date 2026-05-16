---
title: Coding Agents
description: 用 Agently 配合 Codex、Claude Code、Cursor 等 coding agent —— 官方 Agently Skills。
keywords: Agently, coding agents, Codex, Claude Code, Cursor, Skills
---

# Coding Agents

> 语言：[English](../../en/development/coding-agents.md) · **中文**

如果你借助外部 coding agent（Codex、Claude Code、Cursor 等）写 Agently 应用，给该 agent 提供良好 Agently 上下文的规范方式是 `Agently-Skills` 伴生仓中的**官方 Agently Skills** 包。

本文讲的是 **companion repo** 这条路径，不是框架内 runtime skill 消费。如果你要的是 Agently 自己在真实任务里安装并应用外部 skills，读 [Skills Executor](skills-executor.md)。

## 什么是 Agently Skills

skill 是一个包，含：

- `SKILL.md` 描述该 skill 做什么、何时应用
- references —— coding agent 按需拉的聚焦文档
- examples —— 最小可运行片段
- validators —— agent 可跑的脚本，确认用户项目遵循推荐结构

skill **不是**纯文档。它为 coding agent 结构化：每个 skill 告诉 agent 它解决什么问题、推荐路径长什么样、如何验证用户代码在该路径上。

## Companion skills 和框架内 skill 执行的区别

这两件事要分开：

- `Agently-Skills` companion repo：给外部 coding agent 用的 skill 包
- Agently `Skills Executor`：Agently 框架内部的 runtime 能力

伴生仓不会变成你的 Agently app 运行时依赖。它仍然只是给 coding agent 的指导包。

## 当前 skills

| Skill | 用户在做的事 |
|---|---|
| `agently-playbook` | 从零开始 —— 选合适的项目结构 |
| `agently-request` | 模型接入、Prompt 管理、结构化输出、响应复用、session memory、embedding、检索 |
| `agently-runtime` | Action Runtime、内置 actions、MCP、Execution Environment、FastAPI 暴露、DevTools 接入 |
| `agently-triggerflow` | 需要分支、并发、pause/resume、save/load |
| `agently-migration` | 从 LangChain、LangGraph、LlamaIndex、CrewAI 或类似系统迁移 |

当前公开 catalog generation 是 `v2`。实际默认 skill 列表见 `Agently-Skills/skills/`，应只包含这 5 个 skills。

## 安装

```bash
git clone https://github.com/AgentEra/Agently-Skills
```

按 coding agent 自身的 loader 指向 skill 目录：

- **Claude Code** —— `~/.claude/skills/` 或项目 `.claude/skills/`
- **Codex** —— 见 Codex 安装的 skill / context loader
- **Cursor** —— 经项目 rules / context surface 加载

skill 是纯文本 + 脚本；安装时不跑 Agently 特定的东西。

如果用 CLI 安装，默认 `app` bundle 是：

```bash
for skill in \
  agently-playbook \
  agently-request \
  agently-runtime \
  agently-triggerflow
do
  npx skills add AgentEra/Agently-Skills --agent "$AGENT" --skill "$skill" -y
done
```

只有迁移项目才额外安装 `agently-migration`。冻结的 V1 12-skill catalog 位于
`Agently-Skills/legacy/v1/`，最后支持 Agently `4.1.1`；不要把它作为新项目推荐路径。

## 为什么是 skill 不是单纯文档

文档告诉人能做什么。skill 告诉 coding agent **当前**推荐什么 —— 包括哪些 API 已 deprecated、当前 lifecycle 是什么、报告"完成"前要验证什么。这让 coding agent 与框架演进对齐，不需要用户手动更新自己的 context。

特别地，skill **不得**推荐 deprecated 路径如 `.end()`、`set_result()`、`wait_for_result=`、旧 `runtime_data`。如果你发现某 skill 推荐其中之一，请向 `Agently-Skills` 提 issue。

## 4.1 之后的默认推荐

当你审计或编写面向 Agently `4.1+` 的指导时，coding agent 应默认偏向这些用法：

- 结构化输出：固定必填叶子直接写在 `.output(...)` 的 `(TypeExpr, "description", True)` 里。手动 `ensure_keys=` 只留给条件路径或运行时决定的路径。
- Actions：新代码从 `@agent.action_func` 和 `agent.use_actions(...)` 起步。`tool_func`、`use_tool`、`use_tools` 是兼容别名，不是首选推荐。
- TriggerFlow lifecycle：把 `close()` / `async_close()` 和 close snapshot 视为规范收尾路径。不要把 `.end()`、`set_result()`、`get_result()`、`wait_for_result=` 当正常起点。
- TriggerFlow state：单次 execution 的数据用 `get_state(...)` / `set_state(...)`。`flow_data` 是有意共享时才使用的风险作用域，不是普通状态存储。
- Settings 加载：provider 配置落文件时，优先 `Agently.load_settings("yaml_file", path, auto_load_env=True)`。`Agently.set_settings(...)` 留给内联覆盖。
- 执行风格：服务、流式、工作流默认 async-first。sync API 视为脚本、REPL 或兼容桥接层。
- 响应复用：一次模型调用如果要同时消费文本、结构化数据、metadata 或流式更新，优先 `get_response()` 复用同一个 response，而不是重复发请求。

## 何时写自己的 skill

如果团队在 Agently 之上有内部模式（特定项目布局、包装的 agent factory、自定义 action 集），考虑作私有 skill 包，按公开 Agently Skills 格式。coding agent 会跨项目一致地应用团队约定。

## 验证脚本

数个 skill 携带验证脚本（如 `validate/validate_native_usage.py`）。coding agent 在宣布任务完成前可跑它们，确认用户项目遵循推荐路径。例如 TriggerFlow 验证器检查没有 deprecated API 作为推荐起点。

## 另见

- [Action Runtime](../actions/action-runtime.md) —— skill 假设的 tool 使用架构
- [DevTools](../observability/devtools.md) —— 观测、评估和交互式 wrapper 路径
- [TriggerFlow 兼容](../triggerflow/compatibility.md) —— skill 引导的迁移路径
