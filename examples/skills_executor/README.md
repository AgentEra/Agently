# Skill examples

A Skill is an immutable, revisioned context package: `SKILL.md` guidance plus
indexed resources. `SkillLibrary` owns installation and package truth;
`AgentExecution` owns task-scoped selection and binding; `TaskContext` and
`ContextReader` own progressive disclosure.

`Agently.skills_executor` remains a thin management/compatibility facade. It
can configure the library, install/list/inspect/read Skills, and build
compatibility context packs. Its legacy TaskDAG helper is not an executor-ready
integration until host code adapts the real `TaskDAGContext`; do not present
direct registration as a working example. The facade does not choose execution
routes, run Skill-local strategies, actionize scripts, or grant capabilities.

Recommended execution:

```python
execution = (
    agent
    .use_skills([skill_id], mode="required")
    .input(task)
    .output({"summary": (str, "...")})
)
result = await execution.async_get_data()
```

Released convenience calls remain adapters over the same path:

```python
compat = await agent.async_run_skills_task(
    task,
    skills=[skill_id],
    mode="required",
    output={"summary": (str, "...")},
)
result = compat.output
```

Current runnable examples:

```bash
python examples/skills_executor/01_basic_declarative_skills.py
python examples/skills_executor/07_agently_skills_availability_check.py
python examples/skills_executor/08_architecture_diagram_skill.py
python examples/skills_executor/09_skill_script_exec.py
python examples/skills_executor/10_model_pool_key_pool_resolution.py
python examples/skills_executor/11_conditional_resource_read.py
python examples/skills_executor/12_complete_task_selection.py
```

`09_skill_script_exec.py` uses two requests in one Session. The first does not
need the offered Skill; the second creates a fresh AgentExecution, selects the
Skill from the current request, then host code enables the stable restricted
Python Action only in that execution. Scripts remain resources; the framework
does not generate an Action per script or per request.

`11_conditional_resource_read.py` uses a real model to choose supporting files
from the conditions in the already-read root Skill. Configure `AGENTLY_BASE_URL`,
`AGENTLY_API_KEY`, and `AGENTLY_MODEL` explicitly. It compares outline and handoff
phases through the existing execution context reader, without mounting Actions
or generating a final business answer.

`12_complete_task_selection.py` uses a real model to select optional Skills from
all declared goals, criteria and original task facts. Configure the same
`AGENTLY_*` variables; `AGENTLY_REQUEST_OPTIONS` accepts provider-specific JSON
options. It inspects preparation and binding reuse without executing scripts.
