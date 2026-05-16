# Agently Cookbook Examples

These examples collect recommended patterns for common model-app designs. They
are model-backed examples: the planning, routing, decomposition, evaluation, and
revision steps call a real model through DeepSeek or a local Ollama endpoint.

Set `COOKBOOK_MODEL_PROVIDER=deepseek` or `COOKBOOK_MODEL_PROVIDER=ollama`.
When the variable is omitted, the examples use DeepSeek if `DEEPSEEK_API_KEY` is
available, otherwise local Ollama.

## Patterns

| File | Pattern | What it demonstrates |
|---|---|---|
| `01_action_loop_math_model.py` | Action loop | Model plans real Action calls, then replies from action results. |
| `02_router_branching_model.py` | Router | Model classifies intent, stores route in execution state, dispatches to a focused model-backed branch. |
| `03_todo_concurrent_model.py` | To-do decomposition and concurrency | Model decomposes work, then TriggerFlow runs independent items with `for_each(concurrency=...)`. |
| `04_reflection_loop_model.py` | Reflection | Model generates, evaluates, and revises content with a max-round safety boundary. |
| `05_safe_shell_policy_model.py` | Action policy and sandbox | Model calls `agent.enable_shell(...)` and observes a blocked command. |

## Run

```bash
python examples/cookbook/01_action_loop_math_model.py
python examples/cookbook/02_router_branching_model.py
python examples/cookbook/03_todo_concurrent_model.py
python examples/cookbook/04_reflection_loop_model.py
python examples/cookbook/05_safe_shell_policy_model.py
```

## Environment

DeepSeek:

```bash
export COOKBOOK_MODEL_PROVIDER=deepseek
export DEEPSEEK_API_KEY=...
```

Ollama:

```bash
export COOKBOOK_MODEL_PROVIDER=ollama
export OLLAMA_BASE_URL=http://localhost:11434/v1
export OLLAMA_DEFAULT_MODEL=qwen2.5:7b
```

## Notes

- Action loop: if the default model-driven loop is enough, use
  `agent.use_actions(...)`, `agent.input(...)`, and `agent.get_action_result()`.
  Use a custom TriggerFlow loop only when you need explicit stage visibility,
  runtime streams, approvals, or custom stop conditions.
- Shell policy: keep policy in Action/Execution Environment configuration. Do
  not rely on prompt instructions for safety boundaries.

New cookbook examples should be runnable in their declared environment and
include an `Expected key output` comment in the file.
