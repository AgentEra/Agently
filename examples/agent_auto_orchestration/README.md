# Agent Auto-Orchestration Examples

These examples are the current recommended examples for AgentExecution,
Dynamic Task DAG, ActionRuntime, and direct AgentExecution Skill binding.

For changes from released 4.1.4.7, see the bilingual
[4.1.4.8 example change guide](../release_pinned_usage/CHANGES_4_1_4_8.md).

Older Skills auto-orchestration examples from before the 4.1.3.8 Blocks
lifecycle refactor were moved to:

```text
examples/archived/pre-4.1.3.8-skills-orchestration/agent_auto_orchestration/
```

Those archived files are reference material only. They should not be treated as
runnable examples or recommended usage for 4.1.3.8 and later, and should not be
forced onto the new Blocks lifecycle.

## Current Commands

Run from the repository root. Earlier model examples need `DEEPSEEK_API_KEY` in
the environment or `.env`; set `DYNAMIC_TASK_MODEL_PROVIDER=ollama` for local
Ollama where supported. Examples 25-28 use local Ollama/Qwen directly and
default to `qwen`; override it with `AGENT_EXECUTION_OLLAMA_MODEL` or
`OLLAMA_DEFAULT_MODEL`.

```bash
python examples/agent_auto_orchestration/02_actions_dag_streaming.py
python examples/agent_auto_orchestration/05_model_field_delta_streaming.py
python examples/agent_auto_orchestration/06_parallel_dag_field_streaming.py
python examples/agent_auto_orchestration/20_agent_execution_lineage_workspace_loop.py
python examples/agent_auto_orchestration/21_agent_execution_github_issue_intake.py
python examples/agent_auto_orchestration/22_unified_agent_execution_result.py
python examples/agent_auto_orchestration/23_agent_execution_auto_dispatch.py
python examples/agent_auto_orchestration/24_independent_dynamic_task_dag.py
python examples/agent_auto_orchestration/25_agent_execution_delivery_review_ollama.py
python examples/agent_auto_orchestration/26_plan_execution_interaction_ollama.py
python examples/agent_auto_orchestration/27_long_content_execution_artifact_ollama.py
python examples/agent_auto_orchestration/28_missing_goal_preparation_ollama.py
python examples/agent_auto_orchestration/29_execution_controls_ollama.py
python examples/agent_auto_orchestration/29_field_long_content_ollama.py
```

`_TEMPLATE_standard_skill_orchestration.py` shows the released
`run_skills_task(...)` convenience adapter. New code should prefer
`agent.use_skills(...).input(...)` and consume the ordinary AgentExecution.

## Current Examples

- **02 - Customer Support Triage.** Independent Dynamic Task DAG with local
  handlers, dependency edges, and real model calls over mocked CRM data.
- **05 - Operator-visible Field Delta Streaming.** Independent Dynamic Task DAG
  with `kind="model"` nodes and field-level runtime streaming.
- **06 - Parallel DAG Field Delta Streaming.** Independent multi-branch Dynamic
  Task DAG with concurrent workstreams and a fan-in executive brief.
- **20 - AgentExecution Lineage Context Loop.** Two-step AgentExecution
  lineage, RecordStore persistence, and TaskContext disclosure example.
- **21 - GitHub Issue Intake.** AgentExecution plus restricted shell Action for
  real GitHub CLI issue intake.
- **22 - Unified AgentExecution Result.** Minimal quick prompt plus task-loop
  strategy consumed through the same result/stream/meta facade.
- **23 - AgentExecution Auto Dispatch.** Route-selection example proving
  default `model_request` and task-strategy `agent_task` dispatch.
- **24 - Independent Dynamic Task DAG.** Infrastructure smoke for direct
  `Agently.create_dynamic_task(...)` submitted-DAG execution.
- **25 - AgentExecution Delivery And Review.** Local Qwen business result,
  verified TaskWorkspace artifact, model-backed advisory review, and a
  host-owned blocking review handler.
- **26 - Plan Execution With Interaction.** Local Qwen readiness analysis,
  request-local connected clarification, host-validated structured plan, and
  verified artifact delivery.
- **27 - Long-Content Execution Delivery.** Local Qwen section planning and
  writing, host-ordered Markdown assembly, verified artifact delivery, and
  model-backed advisory review.

- **28 - Missing Goal Preparation.** An explicitly selected long-task producer
  asks the model to derive missing goal/criteria from the original request.
  No Actions are authorized. The early run timed out after preparation; the
  later release-candidate run completed preparation and accepted production.
- **29 - Execution Controls.** Safe outer pause, snapshot/rebind/resume,
  same-object revision rework, retained readers and explicit cleanup. Request rework
  preserves original information and instructions alongside the latest feedback.
- **29 - Field Long Content.** Explicit LongContent fields use the chapter
  producer and fill their final strings back into a structured result;
  conditional continuation remains an independent option.

Early 26/27 runs exposed semantic-quality problems; they are historical
observations, not the latest acceptance status. In the final candidate check,
26 preserved its original prompt: local output replaced the explicit metric,
while three unchanged-prompt DeepSeek runs preserved the metric and passed the
120-minute plan and artifact checks. Example 27 delivered the artifact and
truthfully returned a failed advisory review under the default warn policy;
this is not a strict quality guarantee. Model and input quality remain relevant.

Model calls are real. Business data is mocked unless the example explicitly
states that it uses a real external system such as MCP or GitHub CLI.
