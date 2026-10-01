# Unified long-task Loop

For the 4.1.4.9 development line, new goal-driven tasks use one long-task Loop. The model advances the original request using relevant context, a mutable checklist and actual observations. TaskBoard is Markdown: items can be added, removed, split, merged, reordered, checked or reopened. It is neither a DAG nor a per-card executor.

```python
execution = (
    agent.create_execution("long_task")
    .input("Read the current roster; report confirmed attendees, materials and open questions.")
    .use_actions("read_document")
    .output(str)
)
result = await execution.async_get_data()
```

ContextPackage supplies task-relevant material. Existing ActionRuntime owns tool execution; a later decision receives actual results. Independent calls may share a batch; dependent calls wait for new observations. There is no default goal-completion, per-card review or separate final judge request. Explicit validate/artifact/review policies retain their existing contracts.

async_get_data() returns full text or structured business data; async_get_data_object() uses the original output model. async_get_full_data() also carries status, accepted, taskboard and final_result. A blocked result preserves useful work and, when applicable, explains unfinished requirements, uncertainties to check, actual risks and missing information. Model self-assessment is not a correctness guarantee.

Host checks explicit commitments: require_actions() requires actual successful execution, and task_options options.agent_task.required_deliverables declares mandatory destination files. Chat text and fallback files do not satisfy an exact destination. Failures return to the same Loop. File-content semantics remain governed by the original task, without an extra judge. Use artifact(path) when Host should save the final business result.

Observe long_task.progress (taskboard, status, round), existing execution.stage.* events and Action logs. Round counts belong to Host; budgets are not model instructions. max_iterations defaults to 20; execution model/time limits and rework accounting remain cumulative.

async_pause() settles before the next decision, including after an Action batch. Catch AgentExecutionPaused and save(); rebind a fresh matching execution, load() and async_resume(). Completed Actions are not replayed. Active operations cannot be snapshotted. async_rework() retains original facts, checklist and observations; Host still controls replay permission. async_interrupt() and compatible async_add_guidance() reach a future ContextPackage. Cancellation waits for owned work to settle.

## Migration

4.1.x retains explicit strategy("flat") / strategy("taskboard"), create_task(execution=...) and old RecordStore task_id recovery on the legacy implementation. Old snapshots stay on that route; there is no automatic conversion or replay. Select long_task for new tasks, without choosing a strategy by task complexity. Old card, verifier and scheduler options apply only to that compatibility route.

4.2 uses create_execution("long_task") and execution save/load/resume. Old strategy names, Agent.create_task/create_task_loop and Agent.resume by legacy task_id are removed. Finish old state in 4.1.x, or export useful results before starting a new task; lossless conversion is not promised. Independent TaskDAG and TriggerFlow uses are unchanged.
