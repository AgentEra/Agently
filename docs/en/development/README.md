# Development

Current development target: `4.1.4.7`. This line integrates Agently-Stage 0.3.6
automatic mixed sync/async carrier routing with TriggerFlow.

Read in this order:

1. [Coding Agents](coding-agents.md): using the Agently-Skills companion repo with Codex, Claude Code, Cursor, and similar tools.
2. [Skills Compatibility](skills-executor.md): framework-side Skill consumption through Agent APIs, plans, Actions, and the legacy SkillsExecutor facade.
3. [Code Execution Provider Migration](code-execution-provider-migration.md): Workspace-backed provider contract and contributor-owned migration targets for external isolation providers.
4. [Agently 4.1.4.7 Development Notes](release-notes-4.1.4.7.md): automatic Stage routing across provider-owned sync wrappers and TriggerFlow async runtime boundaries.
5. [Agently 4.1.4.6 Release Notes](release-notes-4.1.4.6.md): standard version inspection, unified compatible-provider reasoning events, and live sub-flow resources.
6. [Agently 4.1.4.5 Release Notes](release-notes-4.1.4.5.md): opt-in long-output continuation and Stage-backed TriggerFlow task lifecycle ownership.
7. [Agently 4.1.4.4 Release Notes](release-notes-4.1.4.4.md): stronger Pydantic constraints, recovery-aware TriggerFlow snapshots, and online-model-first release validation.
8. [Agently 4.1.4.3 Release Notes](release-notes-4.1.4.3.md): direct and nested Pydantic v2 output-model compatibility.
9. [Agently 4.1.4.2 Release Notes](release-notes-4.1.4.2.md): breaking TaskContext, TaskWorkspace, RecordStore, and SkillLibrary ownership convergence.
10. [Agently 4.1.4.1 Release Notes](release-notes-4.1.4.1.md): AgentExecutionResult business-data and full-data reader compatibility.
11. [Agently 4.1.4 Development Notes](release-notes-4.1.4.md): TaskBoard incremental acceptance and verifier-cache optimization.
12. [Agently 4.1.3.9 Release Notes](release-notes-4.1.3.9.md): Workspace retrieval, SessionMemory, AgentTask scoped retrieval, vector-index seams, and public typing hardening.
13. [Agently 4.1.3.8 Release Notes](release-notes-4.1.3.8.md): task execution strategy optimization, TaskBoard policy selection, ACP fallback capability, output-control fallback, observation compatibility, and public typing metadata.
14. [Agently 4.1.3.7 Release Notes](release-notes-4.1.3.7.md): AgentExecution-backed AgentTaskLoop hardening, goal/effort configuration, Skills context packs, and release-blocker runtime fixes.
15. [Agently 4.1.3.6 Release Notes](release-notes-4.1.3.6.md): AgentExecution ownership, Result-first consumption, stream-end hardening, and bounded task-loop slice.
16. [Agently 4.1.3.5 Release Notes](release-notes-4.1.3.5.md): historical 4.1.3.5 notes for settings-owned output defaults and prompt isolation work superseded by the current AgentExecution draft model.
17. [Agently 4.1.3.4 Release Notes](release-notes-4.1.3.4.md): structured output parsing hardening, request retry, runtime capability policy, and AgentTaskLoop first public slice.
18. [Agently 4.1.3.3 Release Notes](release-notes-4.1.3.3.md): typed settings/options, model profiles, API key pool failover, runtime handler ownership, core package refactors, and image input.
19. [Agently 4.1.3.2 Release Notes](release-notes-4.1.3.2.md): bounded AgentExecution task steps, Workspace-backed step context, runtime stall control, and EventCenter RuntimeEvent delivery.
20. [Agently 4.1.3.1 Release Notes](release-notes-4.1.3.1.md): Workspace foundation, Recall skeleton, and explicit multi-turn task information management.
21. [Agently 4.1.3 Release Notes](release-notes-4.1.3.md): final 4.1.3 runtime goals, user-facing code shape, and business value.
22. [Release Workflows](release-workflows.md): current repository automation for docs, installers, and PyPI publishing.

DevTools belongs to [Observability](../observability/) because it consumes observation events. Action, MCP, and service APIs live in their own folders.
