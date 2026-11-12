---
title: Skills and AgentExecution
description: SkillLibrary, TaskContext disclosure, and the thin SkillsExecutor compatibility facade.
keywords: Agently, Skills, SkillLibrary, AgentExecution, TaskContext, SkillsExecutor
---

# Skills and AgentExecution

A real-world Skill is a revisioned knowledge and work-procedure package. Its
`SKILL.md` supplies guidance and its indexed resources may supply references,
examples, assets, or scripts. A Skill is not an execution route, strategy
engine, action grant, or workflow.

## Ownership

| Layer | Owner |
|---|---|
| Install, parse, revision, resolve, list, pack membership | `SkillLibrary` |
| Task-scoped selector intent, exact revision binding, required/model-decision mode | `AgentExecution` |
| Progressive disclosure of guidance/resources | `TaskContext` + `SkillContextSource` + `ContextReader` |
| Model request, AgentTask, TaskDAG, workflow, side effect | Existing execution owner |
| Released management-shaped compatibility calls | `Agently.skills_executor` thin facade |

`SkillLibrary` installs immutable content-addressed revisions. An execution
binds the exact revision, not a mutable directory alias. Skill descriptions may
be offered to a semantic model selector; local code must not route free-form
task text with keyword tables or regular expressions.

`Agently.skills_executor`, `Agently.skill_library`, and agents created by the
same `Agently` application share one canonical `SkillLibrary` instance. The
compatibility facade reconfigures that instance in place; it does not install
packs into a separate global registry. Resolve pack members through
`Agently.skill_library` when the installation call starts from the facade, then
bind the returned exact revision through the agent execution.

## Recommended Agent API

```python
contract = Agently.skills_executor.install_skills(
    "./skills/release-review",
    trust_level="local",
    update=True,
)

execution = (
    agent
    .use_skills([contract["skill_id"]], mode="required")
    .input("Review release candidate 3.2.0.")
    .output({
        "decision": (str, "GO or NO-GO", True),
        "risks": ([str], "Grounded release risks", True),
    })
)
result = await execution.async_get_data()
```

`mode="required"` binds the selected revisions fail-closed. With
`mode="model_decision"`, AgentExecution asks a structured `ModelRequest` to
select from host-issued keys, validates the result, and binds the chosen
revisions. Unknown or duplicate keys fail closed.

In 4.1.4.9, applicability selection considers every declared goal and success
criterion, the original input, and task constraints and delivery requirements
from system/info/instruct/output. It still returns only offered Skill keys and
may select none. Declare known requirements for later phases before preparing
the execution so selection can account for them. This adds no selection request
and grants no script execution permission.

Skills use the same composition grammar as Actions; there is no separate public
collection API. `agent.use_skills(..., always=True)` configures the Agent defaults,
while `execution.use_skills(...)` adds declarations for one execution. Before
selection, AgentExecution resolves those declarations into an execution-scoped,
revision-pinned snapshot and offers only its bounded metadata cards to the
model. An execution with no Skill declarations does not scan the global
SkillLibrary. Installing or changing another Skill after preparation does not
silently widen the running execution.

`agent.require_skills(...)` is the explicit required-mode convenience method.
`agent.use_skills_packs(...)` expands an installed immutable pack to its pinned
revision refs.

Revision availability is not consumption evidence. AgentTask records a Skill
context consumption only when the disclosed package is attached to a concrete
ModelRequest response. That consumption is context evidence, not an executable
planner capability or Action evidence.

## What `Agently.skills_executor` still does

The facade is retained for released application calls that manage or project
Skills:

- configure the SkillLibrary root and accepted trust labels;
- install, list, inspect, and read Skill packages;
- install/list/inspect local Skill packs and authorized Git/local source snapshots;
- build a compatibility context-pack projection;
- expose the TaskDAG `skill` resolver helper.

That TaskDAG helper is a legacy compatibility seam, not a complete
`TaskDAGExecutor` integration in 4.1.4.8 (unchanged from 4.1.4.7). The real executor passes
`TaskDAGContext` while the helper consumes a
mapping-shaped projection. Host code would have to adapt that boundary. Do not
present direct registration as proven until the framework owns the adapter and
an end-to-end executor test; reverify this limitation for later release lines.

It does not own route selection, effort strategies, stages, React loops,
runtime chains, Blocks lowering, script execution, capability inference,
automatic Action mounting, or approvals. A registered `SkillSourceProvider`
may materialize an authorized remote source to an immutable local snapshot;
SkillLibrary installs only that snapshot and records the exact provenance.
Remote compatibility installs default to `untrusted`; callers must explicitly
promote a reviewed immutable revision. Local installs retain their local trust
default. A selected Git/local `subpath` is resolved without following symlink
components outside the materialized source root.

```python
pack = await Agently.skills_executor.async_build_context_pack(
    task="Prepare the release review",
    skills=[contract["skill_id"]],
    include_references=True,
)
```

This method creates a temporary TaskContext and uses the same ContextReader
contracts as ordinary execution. The compatibility-only
`actionize_scripts=True` flag leaves selected scripts as ordinary resource
descriptors and emits `skills.compat.actionize_scripts_ignored`; it does not
discover, generate, mount, or authorize Actions.

Each projected Skill retains `action_candidates: []` for compatibility with
released dictionary consumers. It stays empty regardless of that flag; script
descriptors remain in `selected_resources`, not an Action registration list.

For normal AgentExecution work, prepare the Skill scope and explicitly enable
one restricted script-exec Action for the required language. The Action accepts
only a relative `script_path` and bounded `args`; the host resolves that path
against the execution's frozen exact-revision bindings and records the resolved
revision, path, and digest in Action evidence. Enabling the Action does not
invalidate the prepared TaskContext or repeat Skill applicability selection.

```python
from agently.types.data import SkillScriptAuthorization

await execution.async_prepare_task_context()
exec_action_id = agent.enable_skill_script_exec(
    execution,
    authorization=SkillScriptAuthorization(
        auto_allow=True,
        expected_outputs=("output/report.json",),
    ),
)
action_result = await agent.action.async_execute_action(
    exec_action_id,
    {"script_path": "scripts/check.py", "args": []},
)
artifact = next(
    item
    for item in action_result["artifacts"]
    if item["path"].endswith("output/report.json")
)
readback = await execution.task_workspace.read_file(artifact["path"])
```

`enable_skill_script_exec(...)` reuses one stable ordinary Action definition per
Agent/language, then binds authorization only in the current execution's Action
scope and execution context. It does not create one Action per script or user
request. If the same relative path exists in more than one
bound Skill, narrow the execution's Skill declarations or use the released
`bind_skill_script_action(...)` compatibility API for an explicit exact-path
binding. Do not call `enable_code_runtime(...)` only for a Skill script; that
would expose an additional general-purpose code Action. Trust is package
provenance policy, not script permission. Only the successful Action record
plus TaskWorkspace readback proves the side effect and collected bytes.
Published artifact paths are TaskWorkspace-relative private paths under
`.agently/files/.../code_execution/.../output/`.

### Later phases of one task versus new user requests

The candidate scope is not the set of bound Skills. The default implementation
performs initial applicability selection when preparing TaskContext. Later
`async_read_task_context(...)` calls select resources within already bound
sources for their intent, consumer and phase. They do not automatically activate
initially unselected Skills or rescan the global SkillLibrary.

Declare Skills known to be required for the whole task, including later phases,
before starting:

```python
execution = (
    agent.create_execution()
    .input(task)
    .require_skills([planning_skill_ref, delivery_skill_ref])
)
await execution.async_prepare_task_context()
```

Their root guidance is required content; optional resources remain progressively
read. Require only genuinely necessary Skills. Availability does not prove model
consumption or authorize scripts. On-demand activation of an unbound Skill
within the same run is not a current default capability.

### A later user message needs the Skill

Use a fresh AgentExecution for every user request. Session carries conversation
and memory only; it does not carry the previous execution's Skill bindings,
Action scope, or script authorization. Declare potentially relevant Skills as
ordinary Agent defaults so each fresh execution can evaluate them against its
current message:

```python
agent.use_skills([contract["skill_id"]], always=True)

# The first message does not need a script. Its selector may choose no Skill,
# and the host enables no script Action.
first = agent.create_execution().input(first_user_message)
first_result = await first.async_get_data()

# A later message requests script-backed work, so it gets a fresh execution.
later = agent.create_execution().input(later_user_message)
await later.async_prepare_task_context()
if later.skill_bindings:  # The application still applies its allowlist/policy.
    agent.enable_skill_script_exec(
        later,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )
later_result = await later.async_get_data()
```

A started execution and a dispatched ModelRequest are snapshots; neither can
receive a hot-added Skill or Action. If prompt or Skill declarations change
after authorization but before start, Agently revokes the script authorization
and Action visibility that depended on the old TaskContext. Prepare and
authorize again.

## Released execution convenience adapter

`agent.run_skills_task(...)` and `agent.async_run_skills_task(...)` remain
result-shaped adapters over one ordinary AgentExecution:

```python
compat = await agent.async_run_skills_task(
    "Review release candidate 3.2.0.",
    skills=[contract["skill_id"]],
    mode="required",
    output={"decision": (str, "GO or NO-GO", True)},
)
print(compat.execution.id, compat.output)
```

The adapter does not choose a `skills` route. The execution uses the same
`model_request` or explicit AgentTask strategy as any other request. New code
should prefer the direct AgentExecution API when it needs streams, metadata,
TaskContext diagnostics, retries, or lifecycle control.

## Context limits and progressive disclosure

Installing a Skill does not copy all of its resources into every prompt.
`SkillContextSource` contributes revision-pinned resource descriptors and exact
reads to the TaskContext-owned internal ContextIndex. Required `SKILL.md`
guidance is delivered once within a ContextPackage; its child section descriptors are not offered or
delivered again when the complete root is already present. Resource indexes and
explicit references allow later bounded reads. Structural, lexical, or optional hybrid indexing may
narrow reusable candidates, but TaskContext remains the aggregate and
SkillLibrary remains source truth. When available context is too large, the
reader returns omissions and diagnostics plus refs for later reads. It never
pretends that a synthetic summary is the full source.

The default resource-selection request receives instruction content already read
for this read, with its completeness markers, to interpret conditional resource
requirements for the current phase. Optional resource bodies stay unread until
the Host validates the selected keys and reads them exactly. Without read
guidance, selection continues to use the intent, phase, and candidate cards.
This adds no selection node or Skill/Action permissions and does not change the
custom selector call signature.
If both a complete resource root and its child section are selected, package
delivery deduplicates them using their source identity and declared parent
relation, regardless of selection order. An incomplete root does not replace
its child; reads already performed still consume their budget.

Use one or more bounded information blocks selected for the consumer and
phase. Keep full files and raw evidence in SkillLibrary, TaskWorkspace, or
RecordStore; put only the task-relevant package on the hot model path.
Embedding/cache accounting is separate from model prompt-token accounting;
cache reuse alone is not evidence that the final prompt used fewer tokens.

## Side effects

Skills describe how work should be done. Host code, ActionRuntime,
ExecutionResource, TaskWorkspace, RecordStore, TaskDAG, and TriggerFlow retain
their existing responsibilities. A Skill cannot silently grant filesystem,
network, MCP, credential, or process access.
