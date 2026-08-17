---
title: Action Runtime
description: Action architecture below TriggerFlow — ActionRuntime, ActionFlow, ActionExecutor, and the Agently.action surface.
keywords: Agently, action runtime, ActionRuntime, ActionFlow, ActionExecutor, action_func, use_actions
---

# Action Runtime

> Languages: **English** · [中文](../../cn/actions/action-runtime.md)

Agently's action stack has three replaceable plugin layers below the orchestration layer:

```text
   TriggerFlow                  ◄── orchestration above actions (loops, branches, pause/resume)
       │
       ▼
   ActionRuntime                ◄── planning + dispatch
       │  (uses ActionFlow as the bridge to the orchestration layer)
       ▼
   ActionExecutor               ◄── atomic execution (local function, MCP, sandbox)
```

## Layers in detail

| Layer | What it owns | Default builtin |
|---|---|---|
| `TriggerFlow` | high-level orchestration above actions (loops, branches, pause/resume, sub-flow) — see [TriggerFlow](../triggerflow/overview.md) | the `TriggerFlow` core |
| `ActionRuntime` | planning protocol, action call normalization, default execution orchestration | `AgentlyActionRuntime` |
| `ActionFlow` | bridge between an `ActionRuntime` and a flow representation | `TriggerFlowActionFlow` |
| `ActionExecutor` | how one action actually runs | local function, MCP, Python/Bash sandbox, Search/Browse, Node.js, common code runtime, Docker, SQLite executors |
| `ExecutionResource` | managed execution dependencies required before an executor call | MCP, Bash, Python, Node, Docker, Browser, SQLite providers |

`Action` in `agently.core` is a façade that wires:

- `ActionRegistry` and `ActionDispatcher` (stable core primitives)
- one active `ActionRuntime` plugin
- one active `ActionFlow` plugin

Default wiring:

```text
Agent → ActionExtension → Action façade → ActionRuntime → ActionFlow → ActionExecutor
```

## Plugin types

The plugin types you can replace are:

- `ActionRuntime` — for changing the planning protocol or call normalization
- `ActionFlow` — for changing the orchestration shape (e.g., custom flow representation)
- `ActionExecutor` — for adding a new backend (HTTP, gRPC, custom sandbox, remote worker)

Import the protocols and handler aliases from `agently.types.plugins`:

```python
from agently.types.plugins import (
    ActionExecutor,
    ActionRuntime,
    ActionFlow,
    ActionPlanningHandler,
    ActionExecutionHandler,
)
```

> The older `ToolManager` plugin type and `AgentlyToolManager` class are kept for explicit legacy use only and emit deprecation warnings once per deprecated API per Python process, unless `runtime.show_deprecation_warnings` is disabled. Don't write new plugins against `ToolManager`.

## The preferred surface — actions

For new code:

```python
from agently import Agently

agent = Agently.create_agent()


@agent.action_func
async def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@agent.action_func
async def python_code_executor(python_code: str):
    """Execute Python code and return the result."""
    ...


agent.use_actions([add, python_code_executor])

# Or register and run in one shot
@agent.auto_func
def calculate(formula: str) -> int:
    """Compute {formula}. Use available actions."""
    ...

print(calculate("3333+6666=?"))
```

| Surface | Purpose |
|---|---|
| `@agent.action_func` | mark a function as an action, derive its schema from signature + docstring |
| `agent.use_actions(actions)` | register a list, single action, or string-named action with the agent |
| `agent.use_actions(["name1", "name2"])` | register pre-registered actions by name |
| `agent.use_actions(Search(...))` | mount the built-in Search package from `agently.builtins.actions` |
| `agent.use_actions(Browse(...))` | mount the built-in Browse package from `agently.builtins.actions` |
| `agent.enable_python(...)` | mount a Docker-backed `run_python` action for deterministic code execution |
| `agent.enable_shell(...)` | mount a Docker-backed `run_bash` action with workspace roots, command allowlists, timeouts, and bounded output previews |
| `agent.enable_nodejs(...)` | mount a Docker-backed `run_nodejs` action |
| `agent.enable_code_runtime(...)` | mount a Workspace-backed, provider-neutral code execution Action for Python 3.10+, Node.js 18+, Go 1.25+, or C++20 |
| `agent.enable_sqlite(...)` | mount a managed `query_sqlite` action |
| `agent.enable_task_workspace_file_actions(...)` | expose the current TaskWorkspace file area as handler-backed list/search/read/write actions, plus `export_file` when `export=True` and `write=True` |
| `agent.enable_coding_agent_actions(...)` | expose coding-agent TaskWorkspace actions for file readback, glob/grep search, targeted edit, unified-diff patch, and guarded full-file writes |
| `@agent.auto_func` | turn a Python function signature + docstring into a model-backed implementation that uses the agent's actions |
| `agent.get_action_result(prompt=turn.prompt)` | retrieve action call records for a request-scoped turn |
| `extra.action_logs` | structured logs produced during the action loop |

Built-in multi-Action packages use atomic registration batches. If Search or
MCP tool registration fails at any point, including MCP client shutdown,
Agently removes Actions created by that batch and restores any same-id host
Action's prior spec, executor, function, and tags. A failed package mount must
not leave partial Actions or overwrite host-owned registrations.

`agent.action.get_action_info()` and `agent.action.get_tool_info()` return the
visible action/tool schemas registered on that agent by default, including
agent-scoped actions, MCP tools mounted through `agent.use_mcp(...)`, and
`enable_*` component helpers. Pass explicit `tags=[...]` only when you need a
narrow subset. Managed execution environment metadata redacts raw `env` values
in this visible schema while preserving key names; providers still receive the
raw env only through the execution path.

For application code, prefer `enable_*` helpers when the goal is to give the
model a common capability such as Python, shell, or workspace access. Use
`register_action(..., executor=..., execution_resources=[...])` when you are
building a custom Action backend.

`agent.enable_python(...)`, `agent.enable_shell(...)`, and
`agent.enable_nodejs(...)` default to `sandbox="auto"` and
`provisioning_profile="strict"`. Python and Node.js are language facades over
the Workspace-bound `code_execution` contract; shell remains a broader command
Action. Their default provider path uses Docker after checking both the local
Docker CLI and daemon. Missing images use
`image_pull_policy="never"` by default and fail closed with structured
diagnostics such as `execution_resource.docker_image_missing`; they do not
silently fall back to host execution. Use `sandbox="trusted_local"` only for
explicitly trusted paths that intentionally accept unisolated host Python,
shell, or Node.js execution.

`agent.enable_code_runtime(...)` exposes the same provider-neutral path for all
supported languages. Every
call binds a TaskWorkspace grant, selects an eligible `code_execution`
provider, materializes an immutable source bundle, executes an adapter-owned
argv plan, and reads declared outputs back through TaskWorkspace. Configure an
ordered provider list globally or per Action:

```python
agent.settings.set(
    "code_execution.providers",
    [
        {"provider_id": "remote-or-platform-provider", "config": {}},
        "docker",
    ],
)
agent.enable_code_runtime(language="python")
```

The default isolation requirement fails closed. The unsafe host-process runner
is never a silent fallback. To use it, opt into both the fallback and a weaker
isolation requirement explicitly:

```python
agent.enable_code_runtime(
    language="python",
    unsafe_fallback=True,
    isolation="preferred",
)
```

See [Execution Resource](execution-environment.md) for the runtime contract and
[Code Execution Provider Migration](../development/code-execution-provider-migration.md)
for contributor guidance.

For Coding Agent, Agently Skills, examples, and framework tests, use
`provisioning_profile="developer"` or `"ci"`. These profiles default to
`image_pull_policy="if_missing"` and `dependency_policy="install"`, so the
provider may pull missing Docker images and prepare dependencies from standard
manifest files before running the fixed entrypoint. Dependency installation is
not a model-visible action input; do not ask the model to run `pip`, `npm`,
`cargo`, or other package-manager commands through code action schemas.

For coding-agent style local file work, prefer
`agent.enable_coding_agent_actions(...)`. It exposes `read_file`,
`glob_files`, `grep_files`, `edit_file`, `apply_patch`, and guarded
`write_file` over the current TaskWorkspace file root. `edit_file(...)` can use an
`expected_sha256` stale guard, `apply_patch(...)` applies a unified diff and can
require exact `expected_files`, and `write_file(...)` in coding-agent mode
requires either prior read state or an expected hash unless the host disables
that guard. Use shell for tests, builds, git inspection, and read-only
diagnostics; use TaskWorkspace file actions for file reading, search, editing, and
writing.

When `agent.enable_shell(...)` is called without an explicit `commands=...`
allowlist, Agently uses a small safe shell profile for commands such as `pwd`,
`ls`, `rg`, `cat`, `git status`, `git diff`, `git log`, `python -m pytest`, and
`python -m pyright`. Stdout and stderr are returned as bounded previews; if a
stream exceeds `max_output_chars`, the full stream is written under the
current execution fallback at `.agently/files/<execution-id>/shell-output/`
and referenced from the action result.

`run_bash.workdir` is relative to the injected workspace root unless the host
supplies an absolute path already contained by that root. Both `.`/child paths
and the logical `.agently/files/<execution-id>` locator exposed by the current
TaskWorkspace are accepted; a root-prefixed logical locator is consumed once,
not appended to the physical root again. Parent traversal or any other path
outside the injected root fails closed.
`allow_unsafe` is a host-only direct execution grant; it is not exposed in
model-visible shell action schemas and is stripped from model-planned action
inputs. If a model-selected command is outside the safe profile, route it
through an approval-required action or an ExecutionExchange provider instead of
letting model output grant its own bypass.
Custom actions that need direct-call-only parameters can mark them with
`meta={"host_only_input_keys": [...]}`; Action Runtime strips those keys from
model-planned `structured_plan` and native tool-call inputs while preserving
host/direct calls.

Action specs also carry `required_input_keys`. `@agent.action_func` derives
them from function parameters without defaults. Executor-backed registrations
should declare them explicitly with
`register_action(..., required_input_keys=[...])`, or use a third boolean in a
kwargs descriptor tuple such as `(str, "Search query", True)`. Agently emits
the same requirement in native tool JSON Schema and rejects a structured or
native model command with missing required keys before dispatch. Direct host
calls keep the executor or Python function's ordinary validation behavior.

Built-in capability packages live under `agently.builtins.actions`. For example:

```python
from agently.builtins.actions import Browse, Search

agent.use_actions(Search(timeout=15, backend="auto"))
agent.use_actions(Browse())
```

Search is an Action-native package and does not use ExecutionResource;
proxy, timeout, backend, and region are package/executor configuration. Browse
is also Action-native; its default path is Jina Reader -> Playwright -> BS4 ->
restricted curl, while pyautogui is kept as legacy/advanced configuration. The
curl backend is a Browse-internal URL fetch fallback, not model-visible shell
access. Jina Reader delegates the target public URL to `https://r.jina.ai/` for
URL-to-Markdown recovery and automatically tries the official alternate endpoint
`https://r.jinaai.cn/` when the primary Reader endpoint has a transport or
service failure. Disable it explicitly with
`Browse(enable_jina_reader=False, fallback_order=("playwright", "bs4", "curl"))`
when that external service boundary is not acceptable.
If a Browse action needs a managed browser/page/session, register it with a
Browser ExecutionResource provider enabled.

Agent Client Protocol (ACP) coding agents are exposed as Action capability, not
as an AgentExecution route. Use `agent.use_acp(on_missing="skip")` to
scan local ACP endpoints and built-in local coding-agent CLI adapters, then
register `acp_list_agents` plus `acp_run_task` only when a runnable agent is
verified. `acp_list_agents` also returns non-binding adapter-name hints for
common ACP adapters such as `codex`, `claude code` / `cc`, `openclaw`,
`hermes` / `hermes agent`, and `gemini`; these hints do not make an agent
runnable. Built-in local CLI adapters cover common Codex and Claude Code command
locations in addition to the current process `PATH`; they use fixed
framework-owned argv templates and do not expose model-visible shell execution.
The default `on_missing="skip"` records diagnostics and avoids fake runnable
agents; `on_missing="error"` fails closed. ACP run actions declare
`ExecutionResource(kind="acp")` so root scope and lifecycle facts stay in the
resource layer. If `root` is omitted, `agent.use_acp()` uses the Agent's bound
TaskWorkspace `root` as the coding-agent project root; pass `root=...` only
when the host intentionally authorizes a different project directory. ACP
session reuse is an internal AgentExecution resource policy, not an ordinary
task-start option. CLI adapters are marked
`acp_session.persistence="stateless_cli"` unless a real protocol session is
available.

AgentTask can also use ACP as an opt-in recovery fallback after a bounded
step or TaskBoard card fails and configured retries are exhausted. This still
uses the registered `acp_run_task` Action plus `ExecutionResource(kind="acp")`;
ACP is not a route that bypasses AgentExecution or task strategy policy. If the
host never called `agent.use_acp(...)`, the fallback records skipped diagnostics
instead of importing ACP dependencies or inventing an agent.

The `desc=` argument on `enable_*` helpers is optional. By default it is appended
as additional guidance so the model still sees the baseline usage and safety
constraints. Use `desc_mode="override"` when you intentionally want to replace
the default description, or `desc_mode="default"` to ignore the supplied
description and keep only the built-in one.

## Model-sourced input safety

Action commands produced by model planning are treated as untrusted input at the
Action boundary. For `structured_plan` and `native_tool_calls` commands,
`ActionDispatcher` filters `action_input` to the keys declared in the registered
`ActionSpec.kwargs` before the executor is called. Direct host calls keep their
existing behavior and are not filtered this way.

Filtered calls keep structured diagnostics on the `ActionResult`, including
`action.input.unexpected_keys_stripped`, the stripped keys, and bounded previews
of the original and executed kwargs. Timeout and executor exceptions also return
structured Action failures with diagnostics. RuntimeEvent consumers may observe
those facts, but RuntimeEvent does not enforce input safety or authorization.

## Execution recall

Instruction-heavy actions such as `run_bash`, `run_python`, `run_nodejs`,
`query_sqlite`, `browse`, and `search` keep later model context compact by
recording an execution digest plus artifact references.

The digest is what the next action-planning round normally sees. It includes the
action id, call id, purpose, status, a compact instruction preview, result
preview, preview truncation metadata, redaction notes, artifact candidates, and any
TaskWorkspace file refs returned by the Action. Full raw content such as complete
code, shell output, SQL rows, page HTML, screenshots, or logs is retained as a
redacted artifact instead of being inserted into every prompt. Each model-visible
candidate carries one host-issued `selection_key` plus task-relevant facts such
as role, media type, label, and a bounded preview. Canonical artifact ids, call
ids, scope, digest, size, and provenance remain host-owned and are not copied
through the model.
Artifact selection and Action-evidence binding are separate contracts. When a
structured task result asks the model to bind claims to Action evidence, each
offered Action result includes its host-issued `action_call_id`. The model
returns only an offered call id; host code validates it against that result set
and resolves it to the canonical EvidenceLedger identity. An `action_call_id`
is neither a model-invented canonical id nor an artifact readback selector.

### Required Action evidence

When AgentTask completion requires `action_succeeded`, the requirement names an
exact capability id and kind. TaskWorkspace readback cannot satisfy a specified
Action: a readable file may prove file content, but it does not prove that the
required callable capability ran successfully. If the exact Action is mounted,
TaskBoard may create an Action-shaped repair that carries the same structured
id/kind contract through dispatch and evidence binding. It does not derive the
Action from verifier prose or special-case an Action name. AgentTask evaluates
this deterministic evidence contract before the semantic terminal verifier, so
a missing Action schedules the capability-directed repair without spending or
masking the gap behind a verifier request.

When final verification or grounding requests a repair without an exact
`action_succeeded` requirement, a trusted file-backed factual-grounding repair
uses a control-shaped bounded TaskWorkspace patch in both Flat and TaskBoard. The
patch proposal is a structured ModelRequest result; this repair does not open a
general AgentExecution/ActionRuntime round, so mounted `write_file` or unrelated
Actions cannot rewrite the artifact. Host code validates the authorized path,
one operation per `claim_key`, exact `old_string` scope, and the current
`content_version_id` before applying and reading back the patch. Other repairs
keep the ordinary `auto` execution shape, so mounted Actions remain available
for fresh evidence collection. The host does not infer an Action id from
verifier prose in either case. An exact structured Action gap continues to use
the narrowed Action-shaped route above.

An unavailable required Action fails closed immediately instead of scheduling
an equivalent-looking read, tool, or model-only substitute. Denied/blocked
Action policy also fails closed. This is separate from required Skill
availability: required remote Skills must finish discovery, installation, and
inspection before AgentTask business work begins, so an artifact produced
without an unavailable required Skill cannot later pass the terminal gate.

Actions that explicitly return `artifacts` or `artifact_refs` use the same
contract even when the output is small. This includes MCP resource/content
blocks surfaced by `MCPActionExecutor`; Agently records the declared artifact
metadata, but it does not infer undeclared file writes by scanning directories.
Host actions that create files for later AgentTask or TaskBoard consumption
should return typed `file_refs` or `artifact_refs` with the path, size or bytes,
media type, and SHA-256 when available. A path-only payload such as
`{"filename": "...", "path": "...", "size": ...}` stays visible as bounded
Action result evidence and a ref pointer, but it is not treated as a trusted
TaskWorkspace file unless the path is inside the TaskWorkspace files root and TaskWorkspace
readback succeeds.

For a declared AgentTask deliverable path, a successful file-producing Action
is the write owner. AgentTask reads back and adopts that exact TaskWorkspace file;
it does not overwrite it with a model-returned artifact body during terminal
materialization. Updating the file requires another explicit file Action. If
the successful Action's path cannot be read back, artifact delivery fails
closed rather than substituting model prose.

Built-in web actions such as Search and Browse do not prompt for package
installation while running. Missing optional dependencies surface as structured
Action failures so service hosts can decide whether to install, retry, or fall
back.
When the digest is still too large for later planning or reply hot paths,
Agently compacts the model-visible digest again: `result` keeps the bounded
digest, duplicate `data` / `model_digest` fields may become `same_as="result"`
pointers, and artifact refs omit preview bodies while keeping readback ids.
That compaction only applies to hot-path model context; full redacted content
stays in the Action artifact store for explicit readback.

While the owning ActionFlow scope is still live, the model can request omitted
detail through the built-in readback Action:

```python
readback_call = {
    "action_id": "read_action_artifact",
    "action_input": {"selection_key": artifact_candidate["selection_key"]},
}
```

This is an in-flow readback contract. After a standalone ActionFlow returns,
its candidates truthfully report `available=false`; applications must use a
durably promoted TaskWorkspace ref instead of trying to read the released scope.
The public readback selector is only `selection_key`. Agently resolves it
against the currently bound AgentExecution, AgentTask, or standalone ActionFlow
artifact scope; TaskBoard host code binds the current task lineage so sibling
cards in one task can consume the same retained artifact. Missing scope and
cross-task or cross-execution access fail closed. Canonical artifact ids and
Action call ids are not alternate readback selectors.

When `max_bytes` is supplied, a successful readback is one explicitly bounded
progressive-disclosure page. The next planning round receives that page inline
together with its typed `owner`, `locator`, `content_version`, and byte range.
Agently does not externalize the page again or create another selection key for
it. Action success or the existence of a selection key proves only execution
or reference availability; content claims require a consumed readback page.
AgentTask preserves a bounded page body plus the same typed identity as Action
evidence. If the verifier needs material outside the visible snippet, repair
must acquire a narrower or subsequent page rather than lower the success
criterion. Three consecutive reads of the same unchanged typed page terminate
the open ActionLoop as no information progress and return control to TaskBoard;
that transition is not task acceptance.

Oversized direct Action and ActionFlow carriers are compacted as complete
records before they enter a TriggerFlow state or return boundary. This covers
large kwargs/instructions as well as large output fields and avoids retaining
duplicate `data`, `result`, and `model_digest` payloads. Finite internal
ActionRuntime execution flows, ActionFlows, and TaskDAG executions do not bind a
TaskWorkspace. `TriggerFlowActionFlow` binds RecordStore recovery only when an
approval pause needs save/resume.

`Action.to_action_results(records)` uses the digest for instruction-heavy
actions, so follow-up replies can reason about what happened without receiving
the full payload by default.

`max_output_bytes` is an output evidence policy, not a destructive storage
operation. When an Action output exceeds it, Agently records diagnostics and
keeps the full value behind an artifact ref while the model-visible path
continues to use bounded previews.

When a host explicitly calls `agent.get_action_result(prompt=...)`, Agently
marks that prompt as having consumed the action loop even if the returned record
list is empty. A later response read for the same prompt will not re-enter
ActionRuntime just to materialize final text.

Use `agent.action.summarize_records(records)` when host code needs an
authoritative action-evidence summary:

```python
summary = agent.action.summarize_records(
    records,
    validation_command_markers=["pytest", "pyright"],
)

assert summary["latest_validation"]["status"] in {"passed", "failed"}
```

The summary reports failed actions, commands attempted, successful commands,
and the latest matching validation command.

## Compatibility surface — tools

The older surface still works:

```python
@agent.tool_func
def add(a: int, b: int) -> int:
    return a + b

agent.use_tool(add)
agent.use_tools([add])
agent.use_mcp("https://...")
agent.use_sandbox(...)
extra.tool_logs  # equivalent to extra.action_logs at the old surface
```

These remain valid public mounting surfaces. They map onto the new action runtime internally — they don't imply a `ToolManager` implementation. Migrate to the action surface when convenient; nothing breaks immediately.

The historical `use_sandbox(...)`, `bash_sandbox`, and `PythonSandbox` names are
compatibility surfaces, not the isolation owner for unified code execution.
New Python/Node.js/Go/C++ execution declares isolation capabilities on an
`ExecutionResource` provider; `trusted_local` remains an explicit unsafe
fallback and cannot satisfy `isolation=required`.

## Planning model key

Action planning is a model-owned step. When an Agent uses `model_pool`, set
`action.planning_model_key` to the business model key that should plan action
rounds:

```python
agent.set_settings("model_pool", {"task-main": "deepseek-v4-flash-prod"})
agent.set_settings("model_profiles", {
    "deepseek-v4-flash-prod": {
        "provider": "OpenAICompatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash",
        "api_key_pool": "deepseek-prod",
        "request_options": {"thinking": {"type": "disabled"}},
    }
})
agent.set_settings("action.planning_model_key", "task-main")
```

This applies to the default structured-plan and native tool-call planning
paths. It is especially important when a higher-level runtime such as
AgentExecution Skill binding or AgentTask delegates a bounded action round to
ActionRuntime.

Structured planning fields are provisional while the provider response is
still open. `next_action="response"` means that ActionRuntime should schedule no
further Action; it is not a cancellation signal. ActionRuntime awaits the final
parsed structured response so normal request/model completion, metadata, and
usage can settle before the bounded Action step closes.

`agent.get_action_result(..., timeout=N)` bounds the full action loop,
including structured planning and native tool-call selection. If the loop
cannot finish before the deadline, Agently raises `RuntimeStageStallError` with
`stage="action_loop_close"`.

When `planning_protocol="native_tool_calls"` returns no provider-native tool
calls, Agently emits a skipped diagnostic action record with code
`action_runtime.native_tool_calls.empty`. Host code should treat that diagnostic
as planning evidence, not as executed work.

When consecutive rounds select the same failing action ids and none of those
records shows progress, the default `TriggerFlowActionFlow` closes the current
bounded action step and returns the failure evidence it already has. The default
threshold is `action.loop.max_consecutive_failed_rounds_per_action = 2`
(`tool.loop...` remains a compatibility alias). This is not a task budget; a
higher-level owner such as AgentTask can then verify, replan, or block from the
structured failure records.

## Handler interface

If you're writing a custom `ActionRuntime` or `ActionFlow` plugin, the planning and execution handlers use one stable two-argument contract:

```python
async def planning_handler(
    context: ActionRunContext,
    request: ActionPlanningRequest,
) -> ActionDecision:
    ...


async def execution_handler(
    context: ActionRunContext,
    request: ActionExecutionRequest,
) -> list[ActionResult]:
    ...
```

Context fields include `prompt`, `settings`, `agent_name`, `round_index`, `max_rounds`, `done_plans`, `last_round_records`, `action`, `runtime`. Request fields include `action_list`, `planning_protocol`, `action_calls`, `async_call_action`, `concurrency`, `timeout`.

Custom `ActionFlow` plugins may accept an optional
`runtime_observation_handler` keyword. If present, the flow should send
plain observation dictionaries to that handler instead of emitting official
`action.*` or `tool.*` RuntimeEvents directly; core maps those observations to
the official event stream.

The built-in `TriggerFlowActionFlow` and `DAGActionFlow` apply the same
Action-owned bounded/redacted projection before invoking that handler and before
core emits official or compatibility events. Plan observations expose one
canonical `decision.action_calls` list rather than copying commands through the
legacy decision aliases; command observations use canonical `action_id` and
bounded/redacted `action_input` fields. Repeated-failure convergence observations
also carry bounded records, never the private complete Action values. The same
carrier budget covers `payload` and `error`: a raw exception becomes one
bounded/redacted ErrorInfo-compatible mapping before the direct callback, and
official `action.*` plus compatibility `tool.*` events reuse that mapping
without rebuilding the original message or traceback. Opaque string or bytes
exception arguments never preserve a raw prefix: they project to a fixed
redacted summary plus the original UTF-8 byte length and SHA-256 digest.
Explicitly structured arguments may retain bounded facts after sensitive-key
redaction. Projected tracebacks contain structural frame facts only and exclude
the formatted exception line, source line, notes, locals, cause, and context.

There is no legacy positional handler signature — the public contract is `(context, request)` only.

Custom execution-handler results pass through the same complete-record bound as
built-in handlers before they enter AgentExecution context, ActionFlow
RuntimeEvents, TriggerFlow state, logs, metadata, or the public return. Route
logs keep one bounded semantic payload; they do not retain another complete
record under `raw` or duplicate it across `data` and `model_digest`. The exact
value remains only in the live private Action artifact scope.

## Action Artifact Lifetime

Large Action values stay exact in the private `ActionArtifactManager`. Sensitive
field redaction and truncation apply to model-visible previews and RuntimeEvents,
not to the private value selected for durable promotion. AgentExecution accepts
only a `selection_key` offered exactly once by that execution and returned
exactly once by the terminal result. The host resolves that key with the expected
execution scope and reconstructs the canonical ref and exact value; unknown,
duplicate, or copied canonical identities are rejected. A business field named
`accepted` has no selection authority. Provider-supplied artifact ids are stored
only as provenance while each scope receives a fresh local artifact id.

Standalone direct Action calls, `TriggerFlowActionFlow`, and `DAGActionFlow`
release their exact `action_call` or `action_run` scope in `finally` on success,
failure, and cancellation. A standalone AgentTask releases its exact task scope
in its own terminal seam. A routed AgentTask explicitly transfers that scope to
its parent AgentExecution, which keeps it live through terminal selection and
promotion and releases it afterward; on parent cancellation or timeout, the
routed task's stream owner cancels and joins the child before that release, so
the child cannot create a late artifact or RecordStore process record. Child
ActionFlows never release that inherited scope early. If selected promotion
fails, the selected source is kept with bounded retry diagnostics while
unselected artifacts from that exact scope are released.

A durable standalone `TriggerFlowActionFlow` pause keeps its scope while the
exchange is pending. Response/resume closes the flow after the final interrupt;
explicit abandonment or host close cancels the wait. All three paths release
the standalone scope once.

Because a standalone scope is discarded at run end, any artifact refs returned
from that run are historical projections with `available=false` and
`full_value_available=false`. Their bounded digest/preview remains useful, but
`read_action_artifact` cannot retrieve the released value. Only call readback
while a ref explicitly reports `available=true`, such as an execution-owned
scope that has not yet completed transfer or cleanup.
At model and terminal boundaries, `artifact_refs` and the compatibility alias
`artifacts` are normalized to the same selection-key-only list so one alias
cannot expose a canonical identity omitted by the other.

## Extension guidance

| You want to change | Replace |
|---|---|
| Just the backend (HTTP, gRPC, remote worker, sandbox) | `ActionExecutor` |
| The planning protocol or how calls are normalized | `ActionRuntime` |
| The orchestration shape between runtime and flow | `ActionFlow` |
| Higher-level flow control over many action calls | use `TriggerFlow` above the runtime — don't embed it inside an executor |
| Lifecycle for MCP/sandbox/process-like dependencies | declare an `ExecutionResource` requirement — don't hide lifecycle inside an executor |

## See also

- [Actions Overview](overview.md) — where Action Runtime stops and orchestration starts
- [ExecutionResource](execution-environment.md) — managed MCP/sandbox dependencies
- [Tools](tools.md) — the compat surface in more detail
- [MCP](mcp.md) — `agent.use_mcp(...)`
- [TriggerFlow Overview](../triggerflow/overview.md) — orchestration above actions
