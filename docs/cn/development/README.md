# Development

当前开发目标：`4.1.4.7`。该版本要求 Agently-Stage 0.3.8，包含混合同步/异步
carrier 自动路由和 Python 3.14 task factory 兼容修复。

建议按这个顺序读：

1. [Coding Agents](coding-agents.md)：用 Agently-Skills companion repo 帮 Codex、Claude Code、Cursor 等工具获得当前 Agently 指引。
2. [Skills Compatibility](skills-executor.md)：框架内通过 Agent API、plan、Actions 和 legacy SkillsExecutor facade 消费 runtime skills。
3. [Code Execution Provider 迁移](code-execution-provider-migration.md)：Workspace-backed provider 契约和外部隔离 provider 的贡献者自有迁移目标。
4. [Agently 4.1.4.7 开发说明](release-notes-4.1.4.7.md)：工具提供方同步 wrapper 与 TriggerFlow 异步运行时之间的 Stage 自动路由。
5. [Agently 4.1.4.6 Release Notes](release-notes-4.1.4.6.md)：标准版本入口、兼容 provider 的统一 reasoning 事件和 live sub-flow resource。
6. [Agently 4.1.4.5 Release Notes](release-notes-4.1.4.5.md)：可选长输出续写，以及由 Stage 直接支撑的 TriggerFlow task 生命周期。
7. [Agently 4.1.4.4 Release Notes](release-notes-4.1.4.4.md)：更完整的 Pydantic 约束、具备恢复语义的 TriggerFlow 快照，以及线上模型优先的发布校验。
8. [Agently 4.1.4.3 Release Notes](release-notes-4.1.4.3.md)：直接与嵌套 Pydantic v2 输出模型兼容性修复。
9. [Agently 4.1.4.2 Release Notes](release-notes-4.1.4.2.md)：TaskContext、TaskWorkspace、RecordStore 与 SkillLibrary 所有权收敛的破坏式更新。
10. [Agently 4.1.4.1 Release Notes](release-notes-4.1.4.1.md)：AgentExecutionResult 业务数据和完整数据 reader 兼容性。
11. [Agently 4.1.4 Development Notes](release-notes-4.1.4.md)：TaskBoard 增量验收和 verifier cache 优化。
12. [Agently 4.1.3.9 Release Notes](release-notes-4.1.3.9.md)：Workspace retrieval、SessionMemory、AgentTask scoped retrieval、向量索引接缝和公开 typing 加固。
13. [Agently 4.1.3.8 Release Notes](release-notes-4.1.3.8.md)：任务执行策略优化、TaskBoard 策略选择、ACP fallback 能力、输出控制兜底、观测兼容和公开类型元数据。
14. [Agently 4.1.3.7 Release Notes](release-notes-4.1.3.7.md)：AgentExecution-backed AgentTaskLoop 加固、goal/effort 配置、Skills context packs 和 release-blocker runtime 修复。
15. [Agently 4.1.3.6 Release Notes](release-notes-4.1.3.6.md)：AgentExecution ownership、Result-first 消费、stream-end hardening 和 bounded task-loop slice。
16. [Agently 4.1.3.5 Release Notes](release-notes-4.1.3.5.md)：4.1.3.5 的历史 release note，记录 settings-owned 输出默认值和 prompt 隔离工作；当前合同已由 AgentExecution draft 模型承接。
17. [Agently 4.1.3.4 Release Notes](release-notes-4.1.3.4.md)：结构化输出解析加固、请求重试、运行时能力策略和 AgentTaskLoop first public slice。
18. [Agently 4.1.3.3 Release Notes](release-notes-4.1.3.3.md)：typed settings/options、model profiles、API key pool failover、runtime handler ownership、core package refactors 和 image input。
19. [Agently 4.1.3.2 Release Notes](release-notes-4.1.3.2.md)：bounded AgentExecution task steps、Workspace-backed step context、runtime stall control 和 EventCenter RuntimeEvent delivery。
20. [Agently 4.1.3.1 Release Notes](release-notes-4.1.3.1.md)：Workspace foundation、Recall skeleton 和显式多轮任务信息管理。
21. [Agently 4.1.3 Release Notes](release-notes-4.1.3.md)：4.1.3 最终运行时目标、推荐代码形态和业务价值。
22. [Release Workflows](release-workflows.md)：当前主仓库的 docs、安装包和 PyPI 发布自动化。

DevTools 归 [Observability](../observability/)，因为它消费 observation event。Action、MCP 和服务 API 放在各自文件夹里。
