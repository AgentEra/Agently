# Execution Resource Examples

These examples show the recommended developer path for managed execution
capabilities.

Most application code should start with `agent.enable_*` helpers or built-in
action packages. They hide the core manager/provider lifecycle and expose
model-callable Actions with sensible defaults.

## Start Here

| File | Audience | What it shows |
|---|---|---|
| `01_action_python_resource_local.py` | App developers, no model required | Enable a trusted-local Python action with `agent.enable_python(..., sandbox="trusted_local")` and call it directly for a deterministic local check. |
| `02_agent_python_resource_ollama.py` | App developers with local Ollama | Let a model decide to call the enabled Python action before replying. |
| `03_agent_issue_processor_deepseek.py` | App developers with DeepSeek | A realistic issue-triage task where the model uses Python for deterministic metrics and then writes the summary. |
| `04_triggerflow_python_resource_local.py` | Workflow/framework developers | Let a TriggerFlow chunk invoke the canonical Workspace-bound Python CodeExecution Action. |
| `05_action_nodejs_resource_local.py` | App developers, no model required | Enable a Node.js CodeExecution action and execute it through the explicit trusted-local provider. |
| `06_action_sqlite_resource_local.py` | App developers, no model required | Enable a managed SQLite query action against a local database file. |
| `07_browser_resource_browse_local.py` | Action/plugin developers | Browse a local page through Browser Execution Resource. |
| `08_health_check_reuse_local.py` | Provider/plugin developers | Show V2 health-check-before-reuse behavior with a custom provider. |
| `09_action_common_code_runtime_local.py` | App developers, no model required | Run a Workspace-backed `enable_code_runtime(...)` Action with ordered providers and an explicit unsafe fallback. |

## Copy-Paste Shape

For application developers, the shape is intentionally small:

```python
agent = Agently.create_agent()
agent.enable_python(desc="Use for exact calculations. Print the final result as JSON.")

turn = agent.input("Use Python to calculate the average of [15, 23, 42, 8, 12].")
records = agent.get_action_result(prompt=turn.prompt)
result = turn.get_result()
```

You normally do not need to call `Agently.execution_resource` directly. The
Action dispatcher ensures and releases the managed environment when the enabled
action is called.

## Example Details

- `01_action_python_resource_local.py`
  - Runs without any model API key.
  - Uses `agent.enable_python(..., sandbox="trusted_local")`, then calls the registered action directly.
  - Good for verifying the local package without Docker or a model endpoint.
- `02_agent_python_resource_ollama.py`
  - Uses an Ollama OpenAI-compatible endpoint.
  - Defaults to `qwen2.5:7b`, which is sufficient for the small action-selection task.
  - Lets the model choose the default Docker-backed Python action, then prints action records and the final reply.
- `03_agent_issue_processor_deepseek.py`
  - Uses DeepSeek for a more complex issue-processing prompt.
  - Shows that execution is real: model planning calls the Docker-backed Python action, the sandbox computes metrics, and the final reply uses those action results.
- `04_triggerflow_python_resource_local.py`
  - Runs without any model API key.
  - Keeps TriggerFlow responsible for orchestration and invokes a CodeExecution Action from one chunk.
  - Shows the Workspace materialization and action-call resource boundary without a model API key.
- `05_action_nodejs_resource_local.py`
  - Runs without any model API key.
  - Requires `node` on `PATH`; otherwise it prints a skip message.
  - Demonstrates `agent.enable_nodejs(..., sandbox="trusted_local")` and action-call-scoped release.
- `06_action_sqlite_resource_local.py`
  - Runs without any model API key.
  - Creates a temporary SQLite database and queries it through `agent.enable_sqlite(...)`.
- `07_browser_resource_browse_local.py`
  - Runs without any model API key.
  - Requires Playwright and Chromium; otherwise it prints a skip message.
  - Demonstrates `Browse(use_browser_environment=True)` with a managed browser resource.
- `08_health_check_reuse_local.py`
  - Runs without any model API key.
  - Creates a local manager and provider to show that unhealthy ready handles are released and replaced before reuse.
- `09_action_common_code_runtime_local.py`
  - Runs without any model API key.
  - Tries an eligible local Docker provider first, then uses the explicitly authorized unsafe local fallback when Python is installed.
  - Demonstrates Workspace grant, ordered provider selection, and the Python/Node.js/Go/C++ adapter catalog.

Before running the Ollama example, make sure Ollama is running and the model is
available:

```bash
ollama pull qwen2.5:7b
```

The Ollama and DeepSeek Python-action examples use the default Docker-backed
runtime profile. Make sure the local Docker CLI and daemon are available, or
pass `sandbox="trusted_local"` only in trusted compatibility examples.
The common-language code runtime example uses `isolation="preferred"` and
`unsafe_fallback=True` deliberately. Production code that requires isolation
should keep the default `isolation="required"` and omit the unsafe fallback.

Optional Ollama environment variables:

- `OLLAMA_BASE_URL`, defaults to `http://localhost:11434/v1`
- `OLLAMA_DEFAULT_MODEL`, defaults to `qwen2.5:7b`

Before running the DeepSeek example, set:

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`, optional, defaults to `https://api.deepseek.com/v1`
- `DEEPSEEK_DEFAULT_MODEL`, optional, defaults to `deepseek-v4-flash`

Run:

```bash
python examples/execution_resource/01_action_python_resource_local.py
python examples/execution_resource/02_agent_python_resource_ollama.py
python examples/execution_resource/03_agent_issue_processor_deepseek.py
python examples/execution_resource/04_triggerflow_python_resource_local.py
python examples/execution_resource/05_action_nodejs_resource_local.py
python examples/execution_resource/06_action_sqlite_resource_local.py
python examples/execution_resource/07_browser_resource_browse_local.py
python examples/execution_resource/08_health_check_reuse_local.py
python examples/execution_resource/09_action_common_code_runtime_local.py
```

Notes:

- Execution Resource declarations are lazy; a declaration does not start a sandbox or transport.
- Business examples should prefer `agent.enable_python(...)`, `agent.enable_shell(...)`, `agent.enable_task_workspace_file_actions(...)`, `agent.enable_nodejs(...)`, `agent.enable_code_runtime(...)`, and `agent.enable_sqlite(...)` over direct manager/provider APIs. `enable_code_runtime(...)` is provider-neutral; unsafe local execution requires explicit fallback and isolation configuration.
- Built-in providers currently cover MCP, Bash, Docker, provider-neutral code execution, Browser, and SQLite. Python, Node.js, Go, and C++ share the `code_execution` kind and differ only through language adapters. Search is intentionally not an Execution Resource provider; configure proxy, timeout, backend, and region on `agently.builtins.actions.Search(...)`.
- Ready handles are health-checked before reuse. Unhealthy handles emit `execution_resource.unhealthy`, are released, and are replaced with fresh handles.
- `enable_*` helpers provide default action descriptions, so `desc=` is optional. By default `desc=` appends extra guidance; `desc_mode="override"` replaces the default description only when you need full control.
- Action dispatch ensures required environments immediately before executor calls.
- `action_call` scoped handles are released after the action call.
- TriggerFlow still exposes non-code live resources through `runtime_resources`; Workspace-bound code should run through CodeExecution Actions so grants, bundles, provider selection, evidence, and release stay on one path.
