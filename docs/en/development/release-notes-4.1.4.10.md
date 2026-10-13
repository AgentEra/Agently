# Agently 4.1.4.10 candidate notes

This candidate repairs long-task file delivery while preserving the existing
4.1 interfaces and compatibility entrypoints. It does not publish 4.2.0 or
include the compatibility removals planned for that line.

| Area | Change | Usage | Compatibility and limits | Evidence |
|---|---|---|---|---|
| File observations and terminal delivery | Native Action file references reach the Loop; selected files are identity-checked and retained | Return each complete observed path as a separate structured result value | No path extraction from prose; unselected private intermediates are cleaned | `tests/test_long_task_loop.py`, `examples/agent_task/unified_loop_file.py` |
| Implicit file Actions | Reads and writes use the current execution's workspace identity | Inherit the Agent's configured native file Actions | Explicit root/workspace and isolated permission boundaries stay intact | Native code-product readback and execution-private write regressions |
| FastMCP adapter | Read SDK v2 schemas/tags; preserve empty structured objects, errors and content wire aliases | Optionally install `fastmcp==4.1.0`; keep `agent.use_mcp(...)` | FastMCP 3.x retained; no new interactive-tool or background-task workflows | Real dual-version MCP protocol tests and `2_5_mcp_protocol_compat.py` |
| deferred: model effectiveness | No new default semantic judge or unverified Prompt strategy | Evaluate business completion, source faithfulness and content/visual quality separately | Deliverable files do not prove correct semantic judgment; partial reports do not automatically fulfill the original goal | Mechanical checks and real-model effectiveness evaluations are separate |

With the model and workspace configured, the existing call shape remains:

```python
execution = (
    agent.create_execution("long_task")
    .input(task)
    .use_actions(["read_file", "write_file"])
    .output({"files": [str], "summary": str})
)
result = await execution.async_get_data()
```

Each `files` value should be the complete path returned by an Action. The Host
checks existence, workspace/execution identity, size and digest before retaining
selected files through close. Changed or missing files feed back into the same
Loop. A blocked task can also retain selected useful partial artifacts.

A write receipt proves storage and identity, not content inspection. Obtain
current actual content for a content review and evaluate it against the original
task and sources. Use declared `validate`/review/delivery policies for mandatory
acceptance; checklist marks or a model's `completed` judgment alone are not proof.
Publishing PDF bytes still requires an explicit artifact handler; the default
artifact policy serializes the business result.

Verify execution reachability and logic first, then inspect actual model input
and test bounded improvements. Adopt only demonstrated gains. If the declared
minimum remains unmet, propose an authorized stronger-model comparison. Passing
one layer does not establish the others.

These are candidate notes. Integration, package publication and final release
acceptance remain separate checks.
