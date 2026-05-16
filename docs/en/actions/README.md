# Actions

Read in this order:

1. [Overview](overview.md): decide whether the problem belongs to Action Runtime, tool compatibility, MCP, or TriggerFlow.
2. [Action Runtime](action-runtime.md): the current action architecture and extension points.
3. [Execution Environment](execution-environment.md): advanced managed MCP/sandbox/process/browser/SQLite resources for Action, TriggerFlow, and plugin authors.
4. [Tools Compatibility](tools.md): old `tool_func` / `use_tools` aliases and their current status.
5. [MCP](mcp.md): mounting MCP servers as actions.

Use this folder when a model request needs to call something outside the model. Use [TriggerFlow](../triggerflow/) when the main problem is multi-step orchestration. If you are deciding whether a change belongs to core, a plugin, a built-in action, or an Agent Component, read [Architecture / Extension Boundaries](../architecture/extension-boundaries.md).
