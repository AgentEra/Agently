# Actions

建议按这个顺序读：

1. [概览](overview.md)：先判断问题属于 Action Runtime、工具兼容、MCP，还是 TriggerFlow。
2. [Action Runtime](action-runtime.md)：当前 action 架构和扩展点。
3. [Execution Environment](execution-environment.md)：面向 Action、TriggerFlow 与插件开发者的高级托管 MCP/sandbox/process/browser/SQLite 资源。
4. [工具兼容](tools.md)：旧 `tool_func` / `use_tools` 别名的现状。
5. [MCP](mcp.md)：把 MCP server 挂成 action。

模型请求需要调用模型外部能力时读这个文件夹。主要问题是多步骤编排时读 [TriggerFlow](../triggerflow/)。如果你在判断一个变更应该属于 core、plugin、built-in action 还是 Agent Component，请读 [Architecture / 扩展边界](../architecture/extension-boundaries.md)。
