---
title: Coding Agents
description: Using Agently with coding agents like Codex, Claude Code, Cursor — the official Agently Skills.
keywords: Agently, coding agents, Codex, Claude Code, Cursor, Skills
---

# Coding Agents

> Languages: **English** · [中文](../../cn/development/coding-agents.md)

If you're building Agently apps with the help of an external coding agent (Codex, Claude Code, Cursor, etc.), the canonical way to give that agent good Agently context is the **official Agently Skills** packages, distributed in the `Agently-Skills` companion repo.

This page is about the **companion repo** path, not the framework-side runtime skill consumer. If you want Agently itself to install and apply external skills while serving real tasks, read [Skills Executor](skills-executor.md).

## What are Agently Skills

A skill is a bundle of:

- a `SKILL.md` describing what the skill does and when to apply it
- references — focused docs the coding agent can pull on demand
- examples — minimal runnable snippets
- validators — scripts the agent can run to confirm the user's project follows the recommended structure

Skills are **not** just documentation. They're structured for coding agents: each one tells the agent which problem it solves, what the recommended path looks like, and how to verify that the user's code is on that path.

## Companion skills vs framework skill execution

Keep these separate:

- `Agently-Skills` companion repo: skill bundles for external coding agents
- Agently `Skills Executor`: runtime capability inside the Agently framework

The companion repo does not become a runtime dependency of your Agently app. It remains a guidance package for coding agents.

## Current skills

| Skill | Use when the user is |
|---|---|
| `agently-playbook` | starting fresh — picking the right structure for a new Agently project |
| `agently-request` | model setup, prompt management, structured output, response reuse, session memory, embeddings, retrieval |
| `agently-runtime` | Action Runtime, built-in actions, MCP, Execution Environment, FastAPI exposure, DevTools wiring |
| `agently-triggerflow` | needing branching, concurrency, pause/resume, save/load |
| `agently-migration` | migrating from LangChain, LangGraph, LlamaIndex, CrewAI, or similar systems |

The current public catalog generation is `v2`. The actual default skill list lives in `Agently-Skills/skills/` and should contain only these 5 skills.

## Installing the skills

```bash
git clone https://github.com/AgentEra/Agently-Skills
```

Then point your coding agent at the skill directory according to its own loader:

- **Claude Code** — `claude` skills in `~/.claude/skills/` or project `.claude/skills/`
- **Codex** — see your Codex installation's skill / context loader
- **Cursor** — load via the project's rules / context surfaces

The skills are plain text + scripts; nothing Agently-specific runs at install time.

For CLI-based installs, the default `app` bundle is:

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

Add `agently-migration` only for migration projects. The frozen V1 12-skill catalog lives under `Agently-Skills/legacy/v1/` and last supports Agently `4.1.1`; do not use it as the recommended path for new projects.

## Why skills, not just docs

Documentation tells humans what's possible. Skills tell coding agents what's recommended **right now** — including which APIs are deprecated, what the current lifecycle looks like, and what to verify before reporting "done". This keeps coding agents aligned with the framework's evolution without users having to update their own context manually.

In particular, skills must NOT recommend deprecated paths like `.end()`, `set_result()`, `wait_for_result=`, or the old `runtime_data` API. If you find a skill recommending one of these, file an issue against `Agently-Skills`.

## Post-4.1 defaults

When you audit or author guidance for Agently `4.1+`, these are the defaults coding agents should prefer:

- Structured output: for fixed required leaves, mark `(TypeExpr, "description", True)` directly in `.output(...)`. Use manual `ensure_keys=` only for conditional or runtime-dependent paths.
- Actions: new code should start from `@agent.action_func` and `agent.use_actions(...)`. `tool_func`, `use_tool`, and `use_tools` are compatibility aliases, not the primary recommendation.
- TriggerFlow lifecycle: treat `close()` / `async_close()` and the close snapshot as the canonical completion path. Do not recommend `.end()`, `set_result()`, `get_result()`, or `wait_for_result=` as the normal starting point.
- TriggerFlow state: use `get_state(...)` / `set_state(...)` for per-execution data. Treat `flow_data` as an intentionally risky shared scope, not a normal state store.
- Settings loading: when provider settings live in files, prefer `Agently.load_settings("yaml_file", path, auto_load_env=True)`. Keep `Agently.set_settings(...)` for inline overrides.
- Execution style: prefer async-first for services, streaming, and workflows. Treat sync APIs as wrappers for scripts, REPL use, or compatibility bridges.
- Response reuse: when one model call must be consumed as text, parsed data, metadata, or structured stream updates, prefer `get_response()` and reuse the same response object rather than re-requesting.

## When to write your own skill

If your team has internal patterns layered on top of Agently (a particular project layout, a wrapped agent factory, a custom action set), consider authoring a private skill bundle that mirrors the public Agently Skills format. Coding agents will then apply your team's conventions consistently across projects.

## Validation scripts

Several skills ship validation scripts (e.g., `validate/validate_native_usage.py`). Coding agents can run these to confirm a user's project follows the recommended path before declaring a task complete. For example, the TriggerFlow validator checks that no deprecated API is being used as the recommended starting point.

## See also

- [Action Runtime](../actions/action-runtime.md) — the architecture skills assume for tool use
- [DevTools](../observability/devtools.md) — observation, evaluation, and interactive wrapper paths
- [TriggerFlow Compatibility](../triggerflow/compatibility.md) — the migration path skills steer toward
