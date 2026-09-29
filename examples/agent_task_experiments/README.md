# AgentTask Experiment Examples

These examples are compact developer-facing AgentTask capability probes based
on the experiment scenarios used under
`spec/experiments/agent-task-block-carrier`. They are intentionally smaller
than the experiment runner: each script creates one Agent, lets
the public execution entry point choose the right execution path, and prints
`get_async_generator(type="delta")` so the information stream is visible before
the final accepted task result summary.

Run from the repository root:

```bash
python3 examples/agent_task_experiments/01_auto_stock_risk_brief.py
python3 examples/agent_task_experiments/02_auto_agent_engineering_weekly.py
python3 examples/agent_task_experiments/03_auto_lmcc_mock_exam.py
python3 examples/agent_task_experiments/04_auto_repo_reading.py
python3 examples/agent_task_experiments/05_auto_multiruntime_code_task.py
python3 examples/agent_task_experiments/06_auto_mixed_travel_planning.py
python3 examples/agent_task_experiments/07_auto_mixed_equity_analysis.py
python3 examples/agent_task_experiments/08_auto_mixed_business_analysis.py
```

Model selection is handled by `_shared.py`: set
`AGENT_TASK_EXAMPLE_MODEL_PROVIDER=longcat|deepseek|ollama` when you need to
force a provider. Otherwise the helper uses LONGCAT when `LONGCAT_API_KEY` is
available, then DeepSeek, then local Ollama.

The examples avoid explicit `execution="flat"` or `execution="taskboard"`.
The mixed Skill examples use ordinary `goal(...).create_execution(...)`; Skills
injection is what makes them task-oriented.

The first five scripts cover the core experiment scenarios: stock-risk
briefing, Agent engineering weekly report, LMCC mock exam generation,
repository reading, and multi-runtime code execution.
`05_auto_multiruntime_code_task.py` mounts TaskWorkspace coding actions, framework
runtime preflight, and a bounded shell action because it writes and runs
generated code.

The mixed examples mount several capability types at once:

- `06_auto_mixed_travel_planning.py` uses a native travel-policy Action, the
  local `travel-planner` Skill, TaskWorkspace file actions, and the real remote
  AMap MCP endpoint for a Hangzhou domestic-travel scenario. It requires
  `AMAP_API_KEY` and the optional MCP runtime packages in the active Python
  environment.
- `07_auto_mixed_equity_analysis.py` uses a native portfolio-mandate Action,
  the local `equity-risk-reviewer` Skill, TaskWorkspace file actions, and a local
  stdio MCP business-system server for example market/news data. The MCP server
  is real protocol plumbing; its data is explicitly non-live example data, not
  investment advice.
- `08_auto_mixed_business_analysis.py` uses a native board-context Action, the
  local `market-entry-analyst` Skill, TaskWorkspace file actions, and the same
  local stdio MCP server for example CRM and competitor-signal data. The data
  boundary is explicit and should not be presented as a production CRM export.

The examples do not install MCP, ACP, model, language, or package-manager
dependencies. Prepare the environment first; for the local development setup,
run them from the `3.10` conda environment when those optional dependencies are
installed there.

For TaskBoard runs, an ordinary incomplete final answer can return to the existing
repair loop through the finalizer's structured continuation decision. A repair
using available facts does not need another verifier to decide whether to run.
Missing external input can still produce a blocked partial result; a task is
not completed merely because all original cards finished. Explicit file delivery
continues to require actual Host delivery and readback.

When evidence bindings fail and no offered references are available, the Host
skips the binding-repair model request and retains the unresolved result.

`09_taskboard_inline_inventory.py` uses the configured local OpenAI-compatible
model (`OMLX_BASE_URL`, `OMLX_API_KEY`, optional `OMLX_MODEL`). It reads two
synthetic CSV sources and returns an inline inventory report. A normal text
result does not request a file artifact; explicit file-delivery contracts still
stage and promote the requested file through TaskWorkspace.
