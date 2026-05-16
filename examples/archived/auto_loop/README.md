# Archived Custom Auto Loop Examples

These examples predate the current Action Runtime loop. They are kept only for
historical reference because they hand-roll planning, tool routing, and memory
inside TriggerFlow chunks.

Current replacements:

- Cookbook patterns for Action loop, routing, todo concurrency, reflection, and safe shell policy: `examples/cookbook/`
- Model-driven Action loop: `examples/action_runtime/`
- Execution recall and artifact refs: `examples/action_runtime/3_5_action_execution_recall_local.py`
- Built-in Search/Browse packages: `examples/builtin_actions/`
- TriggerFlow orchestration patterns: `examples/trigger_flow/`
- TriggerFlow config and Mermaid export/import: `examples/step_by_step/11-triggerflow-16_flow_config_and_mermaid.py`
- TriggerFlow blueprint save/load: `examples/trigger_flow/save_and_load_blueprint.py`

New code should prefer `agent.use_actions(...)`, `agent.get_action_result()`,
TriggerFlow execution state, and `agent.action.read_action_artifact(...)`
instead of maintaining a custom tool loop.
