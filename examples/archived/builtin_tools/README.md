# Archived Built-in Tool Examples

These examples are kept for historical reference only. New code should start
from `examples/builtin_actions/` and import built-in capability packages from
`agently.builtins.actions`.

Current replacements:

- Search/Browse package registration: `examples/builtin_actions/`
- Action loop and execution recall: `examples/action_runtime/`
- Shell access: `agent.enable_shell(...)` or `examples/action_runtime/3_2_bash_sandbox_action_deepseek.py`

Preferred import path:

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15))
agent.use_actions(Browse())
```

`agently.builtins.tools` remains a thin legacy import facade. The implementation
lives under `agently.builtins.actions`; the facade only preserves old imports and
legacy `tool_info_list` metadata. Prefer `agent.enable_shell(...)` for
user-facing shell access; `Cmd` is the low-level compatibility package.
