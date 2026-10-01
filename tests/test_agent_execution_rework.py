"""Protocol/Host rework tests through the real execution and ModelRequest seams."""
import json
from typing import Any, cast

import pytest
from test_builtin_agent_executions import (
    ScriptedExecutionRequester,
    create_execution_agent,
    not_ready_payload,
    ready_payload,
)


@pytest.mark.asyncio
async def test_long_content_rework_reuses_prefix_and_invalidates_dependants(tmp_path):
    plan = {"document_title": "Guide", "sections": [
        {"section_id": "section-1", "title": "Context", "brief": "Context"},
        {"section_id": "section-2", "title": "Design", "brief": "Design"},
        {"section_id": "section-3", "title": "Checks", "brief": "Checks"},
    ]}
    agent = create_execution_agent(tmp_path, "revision-document", [
        {"document_title": plan["document_title"], "part_plan": [
            {"part_title": item["title"], "part_brief": item["brief"]} for item in plan["sections"]]},
        {"body": "Original context"}, {"summary": "Context remains."},
        {"body": "Original design"}, {"summary": "Original decision."},
        {"body": "Original checks"},
        {"plan": plan, "invalidated_section_ids": ["section-2"]},
        {"body": "Revised design"}, {"summary": "Revised decision."},
        {"body": "Revised checks"},
    ])
    run = agent.create_execution("long_content", limits={"max_model_requests": 10}).input("Write the guide")
    old = run.get_result()
    first = await run.async_run()
    second = await run.async_rework("Change the design and its checks")
    assert first != second and await old.async_get_data() == first
    assert isinstance(second, str)
    assert "Original context" in second and "Revised checks" in second
    assert ScriptedExecutionRequester.model_dispatches == 10
    assert run.execution_context.model_request_count == 10
    prompt = json.dumps(ScriptedExecutionRequester.requests[-1], default=str)
    assert "Revised decision." in prompt and "Original decision." not in prompt


@pytest.mark.asyncio
async def test_plan_rework_keeps_accepted_clarification(tmp_path):
    agent = create_execution_agent(tmp_path, "revision-plan", [
        not_ready_payload(), ready_payload(), "First plan",
        ready_payload(), "Revised plan",
    ])
    answers = []

    def answer(exchange):
        answers.append(exchange)
        return "Staging only"

    run = agent.create_execution("plan").input("Plan the release").interact(answer)
    old = run.get_result()
    assert await run.async_run() == "First plan"
    assert await run.async_rework("Add a staging recovery step") == "Revised plan"
    assert len(answers) == 1
    assert await old.async_get_data() == "First plan"
    assert "Staging only" in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)
    assert "Add a staging recovery step" in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("safe,uncertain", [(False, False), (True, False), (False, True)])
async def test_action_dispatch_replay_protection_includes_nested_and_uncertain_effects(tmp_path, safe, uncertain):
    from agently.core.application.AgentExecution import AgentExecutionContext
    from agently.core.runtime.RuntimeContext import bind_runtime_context

    agent = create_execution_agent(tmp_path, "replay-actions", [])
    calls = []

    class Executor:
        kind = "test"
        sandboxed = False

        async def execute(self, **kwargs):
            calls.append(kwargs["action_call"])
            if uncertain:
                raise RuntimeError("effect happened, acknowledgement lost")
            return "written"

    agent.action.action_registry.register(
        {"action_id": "external_write", "name": "external_write", "desc": "Host replay fixture",
         "kwargs": {}, "returns": (str, "receipt"), "replay_safe": safe}, Executor(),
    )
    parent = AgentExecutionContext(execution_id="parent", lineage={}, limits={})
    with bind_runtime_context(agent_execution_context=parent):
        first = await agent.action.action_dispatcher.async_execute("external_write", {})
        child = AgentExecutionContext(execution_id="child", lineage={}, limits={})
    assert first.get("success") is not uncertain
    assert parent._dispatched_action_ids == {"external_write"}
    parent._rework_blocked_action_ids = set(parent._dispatched_action_ids)
    # A child granting itself replay cannot override its ancestor's protection.
    with bind_runtime_context(agent_execution_context=child):
        second = await agent.action.action_dispatcher.async_execute("external_write", {})
    assert len(calls) == (2 if safe else 1)
    if not safe:
        assert second.get("status") == "blocked" and "replay_safe" in str(second.get("error"))
    parent._rework_blocked_action_ids.clear()
    with bind_runtime_context(agent_execution_context=parent):
        await agent.action.action_dispatcher.async_execute("external_write", {})
    assert len(calls) == (3 if safe else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
async def test_unified_flat_rework_creates_new_work_with_cumulative_iterations(tmp_path, restore):
    agent = create_execution_agent(tmp_path, "revision-flat", [{"invalidated_work_ids": ["work_0"]}])
    run = agent.create_task(goal="Write a release note", success_criteria=["Readable note"],
                            execution="flat", max_iterations=2)
    calls = []

    async def plan(iteration_index, context_pack):
        calls.append(iteration_index)
        if restore and iteration_index == 1:
            await run.async_pause()
        return {"execution_shape": "direct", "step_instruction": "Write the note",
                "expected_evidence": "note", "rationale": "Host lifecycle fixture"}

    async def execute(iteration_index, *_args):
        return ({"step_result": f"note {iteration_index}", "evidence": [], "remaining_work": [],
                 "ready_for_final_verification": True},
                {"execution_id": f"step-{iteration_index}", "status": "completed", "logs": {},
                 "route": {"selected_route": "model_request"}})

    async def verify(iteration_index, **_kwargs):
        return {"is_complete": True, "requires_block": False, "reason": "Fixture passed",
                "missing_criteria": [], "final_result": f"note {iteration_index}"}

    cast(Any, run)._agent_task_step_overrides = {"_request_plan": plan, "_execute_step": execute,
                                     "_request_verification": verify}
    old = run.get_result()
    if restore:
        from agently.core.application.AgentExecution import AgentExecutionPaused
        with pytest.raises(AgentExecutionPaused):
            await run.async_run()
        snapshot = run.save()
        new = agent.create_task(goal="Write a release note", success_criteria=["Readable note"],
                                execution="flat", max_iterations=2, task_id=cast(Any, run.task_record).id)
        new.load(json.loads(json.dumps(snapshot)))
        cast(Any, new)._agent_task_step_overrides = cast(Any, run)._agent_task_step_overrides
        await run.async_cancel()
        run = new
        old = run.get_result()
        await run.async_resume()
    else:
        await run.async_run()
    assert await old.async_get_data() == "note 1"
    task = run.task_record
    await run.async_rework("Improve the note")
    assert run.task_record is task and run.revision == 1
    assert calls == [1, 2]
    assert await run.get_result().async_get_data() == "note 2"
    assert await old.async_get_data() == "note 1"
    assert run.execution_context.model_request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_taskboard_rework_validates_reused_files_and_dependency_closure(tmp_path, changed):
    import hashlib

    from agently.builtins.plugins.AgentExecution.long_task import AgentTask
    from agently.builtins.plugins.AgentExecution.long_task.Rework import (
        prepare_task_rework,
    )
    from agently.builtins.plugins.AgentExecution.modules.revisions import content_digest
    from agently.types.data import TaskBoardRevision

    agent = create_execution_agent(tmp_path, "revision-board", [{"invalidated_work_ids": ["design"]}])
    run = cast(Any, agent.create_execution("long_task", options={"context_budget": {"optional_selection": "none"}}))
    task = AgentTask(agent, goal="Build a guide", success_criteria=["Guide is checked"], execution="taskboard")
    run.task_record = task
    source = task.task_workspace.root / "source.txt"
    source.write_text("original evidence")
    ref = {"path": "source.txt", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    board = TaskBoardRevision.from_value({"board_id": task.id, "revision_id": "revision-0", "graph": {
        "graph_id": "guide", "cards": [
            {"id": "research", "objective": "Collect source", "allowed_execution_shape": "model"},
            {"id": "design", "objective": "Design", "depends_on": ["research"], "allowed_execution_shape": "model"},
            {"id": "check", "objective": "Check design", "depends_on": ["design"], "allowed_execution_shape": "model"},
        ]}, "card_results": {
            key: {"card_id": key, "status": "completed", "preview": key,
                  "file_refs": [ref] if key == "research" else []} for key in ("research", "design", "check")}})
    content = {"strategy": "taskboard", "board": board.to_dict(), "tick_index": 4, "iteration": 4, "iterations": []}
    run._producer_state = {"kind": "long_task", "content": content, "digest": content_digest(content)}
    run.revision = 1
    run._rework_feedback = "Repair design"
    if changed:
        source.write_text("different evidence")
        with pytest.raises(ValueError, match="resource changed"):
            await prepare_task_rework(run)
        assert task._resumed_taskboard_state is None
    else:
        await prepare_task_rework(run)
        assert task._resumed_taskboard_state is not None
        updated = TaskBoardRevision.from_value(task._resumed_taskboard_state["revision"])
        assert updated.card_results["research"].status == "completed"
        assert updated.card_results["design"].status == updated.card_results["check"].status == "pending"
        assert updated.revision_id != board.revision_id
        assert board.card_results["design"].status == "completed"
        assert task._resumed_from_iteration == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("ticks,scope", [(1, "collect"), (10, "collect"), (10, "candidate")])
async def test_unified_taskboard_rework_reenters_producer_instead_of_terminal_resume(tmp_path, ticks, scope):
    from test_agent_execution_step_contract import (
        MockTaskBoardRequester,
        _create_taskboard_agent,
    )

    class RevisionBoardRequester(MockTaskBoardRequester):
        name = "RevisionBoardRequester"
        produced_cards = 0

        async def request_model(self, request_data):
            text = json.dumps(request_data.data, default=str)
            if "invalidated_work_ids" in text and "rework_scope" in text:
                yield "message", json.dumps({"invalidated_work_ids": [scope]})
                return
            if "Execute exactly one TaskBoard card" in text:
                type(self).produced_cards += 1
            async for item in super().request_model(request_data):
                yield item

    agent = _create_taskboard_agent("revision-unified-board").use_task_workspace(tmp_path)
    agent.plugin_manager.register("ModelRequester", RevisionBoardRequester, activate=True)
    run = agent.create_task(goal="Write a fact summary", success_criteria=["Fact summarized"],
                            execution="taskboard", max_iterations=10, options={"taskboard_max_ticks": ticks})
    old = run.get_result()
    first = await run.async_run()
    first_count = run.execution_context.model_request_count
    assert RevisionBoardRequester.produced_cards == 1
    await run.async_rework("Repair the collected fact summary")
    assert RevisionBoardRequester.produced_cards == (1 if ticks == 1 or scope == "candidate" else 2)
    assert run.execution_context.model_request_count > first_count
    assert run.revision == 1 and await old.async_get_full_data() == first
    assert run.diagnostics["rework"]["invalidated_work_ids"] == [scope]


@pytest.mark.asyncio
async def test_compatibility_task_budget_covers_rework_scope_before_dispatch(tmp_path):
    from agently.core.application.AgentExecution import RuntimeStageStallError
    from test_agent_execution_step_contract import _create_agent

    agent = _create_agent("rework-expired-task").use_task_workspace(tmp_path)
    run = agent.create_task(goal="Produce a bounded answer", success_criteria=["An answer"],
                            execution="flat", max_iterations=2,
                            limits={"max_model_requests": 10, "max_seconds": 10})
    assert run.limits.get("max_model_requests") is None
    assert cast(Any, run).task_strategy_options()["limits"]["max_model_requests"] == 10

    async def plan(*args):
        return {"execution_shape": "direct", "step_instruction": "Answer", "expected_evidence": "answer"}

    async def execute(*args):
        return ({"step_result": "first", "evidence": [], "remaining_work": [],
                 "ready_for_final_verification": True},
                {"execution_id": "step-1", "status": "completed", "logs": {},
                 "route": {"selected_route": "model_request"}})

    async def verify(*args, **kwargs):
        return {"is_complete": True, "requires_block": False, "reason": "Fixture", "missing_criteria": [],
                "final_result": "first"}

    cast(Any, run)._agent_task_step_overrides = {"_request_plan": plan, "_execute_step": execute,
                                               "_request_verification": verify}
    await run.async_run()
    count = run.execution_context.model_request_count
    run.execution_context.started_at -= 11
    with pytest.raises(RuntimeStageStallError):
        await run.async_rework("Change the answer")
    assert run.execution_context.model_request_count == count
    assert await run.get_result(revision=0).async_get_data() == "first"


@pytest.mark.asyncio
async def test_request_rework_preserves_original_information_and_instructions(tmp_path):
    agent = create_execution_agent(tmp_path, 'revision-context', ['Draft', 'Revised draft', 'Final draft'])
    run = (agent.create_execution('request')
           .goal('Write the note', success_criteria=['Follow the supplied policy'], turn_on_long_task=False)
           .info({'policy': 'Only describe confirmed observations.'})
           .instruct(['Keep unknown fields explicit.']))
    await run.async_run()
    await run.async_rework('Make the note clearer')
    prompt = ScriptedExecutionRequester.requests[-1]
    assert 'Only describe confirmed observations.' in json.dumps(prompt['info'])
    assert 'Keep unknown fields explicit.' in json.dumps(prompt['instruct'])
    assert 'Make the note clearer' in json.dumps(prompt['info'])
    assert 'Draft' in json.dumps(prompt['info'])
    await run.async_rework('Use a shorter introduction')
    prompt = ScriptedExecutionRequester.requests[-1]
    assert 'Only describe confirmed observations.' in json.dumps(prompt['info'])
    assert 'Keep unknown fields explicit.' in json.dumps(prompt['instruct'])
    assert 'Use a shorter introduction' in json.dumps(prompt['info'])
    assert 'Make the note clearer' not in json.dumps(prompt['info'])
    assert 'Revised draft' in json.dumps(prompt['info'])
