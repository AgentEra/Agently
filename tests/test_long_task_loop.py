"""Deterministic protocol checks; scripted decisions are not model-quality evidence."""
import json
from typing import Any, cast
from agently.builtins.plugins.AgentExecution import AgentExecution

import pytest

from agently.core.application.AgentExecution import AgentExecutionPaused, AgentExecutionLimitExceeded
from test_builtin_agent_executions import create_execution_agent, ScriptedExecutionRequester


def decision(status: str = "completed", result: Any = "done", calls: list[dict[str, Any]] | None = None, board: str | None = None) -> dict[str, Any]:
    return {"status": status, "result": result, "action_calls": calls or [], "taskboard": board}


@pytest.mark.asyncio
async def test_default_long_task_has_one_decision_and_no_shape_or_finalizer(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-simple", [decision()])
    run = agent.create_execution("long_task").input("Return a short result.")
    assert await run.async_get_data() == "done"
    assert ScriptedExecutionRequester.model_dispatches == 1
    assert run.task_record is None
    assert run.task_refs["effective_execution_strategy"] == "loop"


@pytest.mark.asyncio
async def test_tool_feedback_checklist_revision_and_structured_result(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-action", [
        decision("continue", None, [{"action_id": "read_note", "action_input": {}}], "- [ ] Read"),
        decision(result={"count": 2}, board="- [x] Read\n- [x] Resolve new item"),
    ])
    calls = []
    @agent.action_func
    def read_note() -> str:
        calls.append(1)
        return "actual observation"
    run = agent.create_execution("long_task").input("Read the note.").use_actions("read_note").output({"count": int})
    assert await run.async_get_data() == {"count": 2}
    assert calls == [1]
    assert "actual observation" in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)
    assert cast(AgentExecution, run)._producer_state["content"]["rounds"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,action_id", [("completed", "write"), ("continue", "unoffered")])
async def test_invalid_decision_never_dispatches_actions(tmp_path, status, action_id):
    agent = create_execution_agent(tmp_path, "loop-invalid", [decision(status, "candidate", [
        {"action_id": action_id, "action_input": {}}])])
    calls = []
    @agent.action_func
    def write() -> str:
        calls.append(1)
        return "effect"
    run = agent.create_execution("long_task").input("Do work").use_actions("write")
    with pytest.raises(ValueError):
        await run.async_get_data()
    assert calls == []


@pytest.mark.asyncio
async def test_required_file_failure_returns_to_same_decision(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-file", [decision(), decision("blocked", "retained draft")])
    run = agent.create_execution("long_task").input("Write the required file.")
    run.task_options["options"] = {"agent_task": {"required_deliverables": ["missing.txt"]}}
    assert await run.async_get_data() == "retained draft"
    assert run.status == "blocked"
    assert "missing.txt" in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)
    assert ScriptedExecutionRequester.model_dispatches == 2


@pytest.mark.asyncio
async def test_rework_retains_context_and_cumulative_rounds(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-rework", [decision(result="first"), decision(result="second")])
    run = agent.create_execution("long_task").input("Use the original facts").info({"fact": 7})
    old = run.get_result()
    assert await run.async_get_data() == "first"
    await run.async_rework("Revise presentation")
    assert await run.async_get_data() == "second"
    assert await old.async_get_data() == "first"
    assert cast(AgentExecution, run)._producer_state["content"]["rounds"] == 2
    last = json.dumps(ScriptedExecutionRequester.requests[-1], default=str)
    assert "Revise presentation" in last and "fact" in last and "first" in last


@pytest.mark.asyncio
async def test_round_limit_survives_rework(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-limit", [decision()])
    run = agent.create_execution("long_task").input("Work")
    run.task_options["max_iterations"] = 1
    await run.async_get_data()
    with pytest.raises(AgentExecutionLimitExceeded):
        await run.async_rework("More work")
    assert ScriptedExecutionRequester.model_dispatches == 1


@pytest.mark.asyncio
async def test_pause_after_action_json_restore_never_repeats_effect(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-resume", [
        decision("continue", None, [{"action_id": "read_note", "action_input": {}}]), decision()])
    calls = []
    run = None
    @agent.action_func
    async def read_note() -> str:
        calls.append(1)
        assert run is not None
        await run.async_pause()
        return "retained observation"
    def draft():
        return agent.create_execution("long_task").input("Read then answer").use_actions("read_note")
    run = draft()
    with pytest.raises(AgentExecutionPaused):
        await run.async_get_data()
    snapshot = json.loads(json.dumps(run.save()))
    assert snapshot["boundary"] == "long_task_step"
    await run.async_close(pending="cancel")
    restored = draft()
    restored.load(snapshot)
    await restored.async_resume()
    assert await restored.async_get_data() == "done"
    assert calls == [1]
    assert "retained observation" in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)


@pytest.mark.asyncio
async def test_required_action_needs_actual_success(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-required", [decision(),
        decision("continue", None, [{"action_id": "read_note", "action_input": {}}]), decision()])
    @agent.action_func
    def read_note() -> str:
        return "verified"
    run = agent.create_execution("long_task").input("Read").require_actions("read_note")
    assert await run.async_get_data() == "done"
    assert ScriptedExecutionRequester.model_dispatches == 3
    assert "required_actions_not_succeeded" in json.dumps(ScriptedExecutionRequester.requests[1], default=str)


@pytest.mark.asyncio
async def test_tool_error_and_guidance_reach_next_decision(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-error", [
        decision("continue", None, [{"action_id": "read_note", "action_input": {}}]),
        decision("blocked", "Cannot read source")])
    @agent.action_func
    async def read_note() -> str:
        await run.async_add_guidance("Preserve the partial draft")
        raise OSError("source unavailable")
    run = agent.create_execution("long_task").input("Read").use_actions("read_note")
    assert await run.async_get_data() == "Cannot read source"
    last = json.dumps(ScriptedExecutionRequester.requests[-1], default=str)
    assert "source unavailable" in last and "Preserve the partial draft" in last
    assert cast(AgentExecution, run).guidance_items[-1]["status"] == "consumed"
    assert not cast(AgentExecution, run)._producer_state["content"]["succeeded_actions"]


@pytest.mark.asyncio
async def test_required_delivered_file_retains_reference(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-delivered", [decision()])
    agent.use_task_workspace(tmp_path / "loop-delivered", mode="read_write")
    run = agent.create_execution("long_task").input("Return existing delivery")
    run.options["context_budget"] = {"optional_selection": "none"}
    await run.task_workspace.write_file("deliverables/result.txt", "actual body")
    run.task_options["options"] = {"agent_task": {"required_deliverables": ["deliverables/result.txt"]}}
    assert await run.async_get_data() == "done"
    refs = cast(AgentExecution, run)._terminal_task_handoff_refs
    assert refs[0]["path"] == "deliverables/result.txt" and refs[0]["size"] == 11


@pytest.mark.asyncio
async def test_typed_result_uses_original_output_model(tmp_path):
    from pydantic import BaseModel
    class Result(BaseModel):
        count: int
    agent = create_execution_agent(tmp_path, "loop-typed", [decision(result={"count": 3})])
    run = agent.create_execution("long_task").input("Count").output(Result)
    value = await run.async_get_data_object()
    assert isinstance(value, Result) and value.count == 3


@pytest.mark.asyncio
async def test_cancellation_settles_running_action(tmp_path):
    import asyncio
    agent = create_execution_agent(tmp_path, "loop-cancel", [
        decision("continue", None, [{"action_id": "wait_note", "action_input": {}}])])
    entered, cleaned = asyncio.Event(), asyncio.Event()
    @agent.action_func
    async def wait_note() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return "unreachable"
    run = agent.create_execution("long_task").input("Wait").use_actions("wait_note")
    task = asyncio.create_task(run.async_get_data())
    await asyncio.wait_for(entered.wait(), 3)
    assert (await asyncio.wait_for(run.async_cancel(), 3))["status"] == "cancelled"
    assert cleaned.is_set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_creation_limit_blocks_without_model_dispatch(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-disabled", [])
    run = agent.create_execution("long_task", limits={"allow_create_task": False}).input("Work")
    full = await run.async_get_full_data()
    assert full["status"] == "blocked" and not full["accepted"]
    assert ScriptedExecutionRequester.model_dispatches == 0


@pytest.mark.asyncio
async def test_stream_progress_and_explicit_validation_use_final_business_value(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-stream", [decision(result={"count": 2}, board="- [x] Count")])
    seen = []
    def validate(data, _context):
        seen.append(data)
        return True
    run = agent.create_execution("long_task").input("Count").output({"count": int}).validate(validate)
    assert "long_task_step" in run.control_capabilities["snapshot_boundaries"]
    events = [event async for event in run.get_async_generator(type="instant")]
    assert any(event.path == "long_task.progress" and event.value["taskboard"] == "- [x] Count" for event in events)
    assert seen == [{"count": 2}]
    assert await run.async_get_data() == {"count": 2}
