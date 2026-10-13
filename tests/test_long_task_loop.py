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


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "blocked"])
@pytest.mark.parametrize("pause", [False, True])
async def test_selected_action_file_survives_cleanup_and_restore(tmp_path, terminal, pause):
    agent = create_execution_agent(tmp_path, "loop-file-selection", [])
    run = None
    produced = []

    @agent.action_func
    async def make_file() -> dict:
        assert run is not None
        workspace = run.task_workspace
        for name in ("result.pdf", "scratch.pdf"):
            path = workspace.fallback_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4\n\x00\xffbinary")
            produced.append(path)
        ref = await workspace._promote_file_identity(produced[0].relative_to(workspace.root).as_posix())
        ScriptedExecutionRequester.responses.append(decision(terminal, {"pdf_path": ref["path"]}))
        if pause:
            await run.async_pause()
        return {"status": "success", "result": {"stdout": "generated"},
                "artifacts": [{**ref, "artifact_type": "file"}]}

    def draft():
        return agent.create_execution("long_task").input("Generate a PDF").use_actions("make_file").output({"pdf_path": str})

    run = draft()
    run.options["context_budget"] = {"optional_selection": "none"}
    ScriptedExecutionRequester.responses.append(decision("continue", None, [{"action_id": "make_file", "action_input": {}}]))
    if pause:
        with pytest.raises(AgentExecutionPaused):
            await run.async_get_data()
        snapshot = json.loads(json.dumps(run.save()))
        paused_run = run
        run = draft()
        run.options["context_budget"] = {"optional_selection": "none"}
        run.load(snapshot)
        await run.async_resume()
    result = await run.async_get_data()
    assert produced[0].exists()
    assert not produced[1].exists()
    assert produced[0].read_bytes() == b"%PDF-1.4\n\x00\xffbinary"
    assert result["pdf_path"] in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)
    assert len(cast(AgentExecution, run)._terminal_task_handoff_refs) == 1
    assert (await run.async_get_full_data())["accepted"] == (terminal == "completed")
    if pause:
        # Cancellation ends ownership and may invalidate saved workspace files.
        # Close only after the restored execution has consumed the snapshot.
        await paused_run.async_close(pending="cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["changed", "deleted", "unknown"])
async def test_file_selection_rejects_stale_files_and_ignores_unknown_paths(tmp_path, mutation):
    agent = create_execution_agent(tmp_path, "loop-file-invalid", [])
    produced = []

    @agent.action_func
    async def make_file() -> dict:
        path = run.task_workspace.fallback_root / "result.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
        produced.append(path)
        ref = await run.task_workspace._promote_file_identity(path.relative_to(run.task_workspace.root).as_posix())
        selected = ref["path"] if mutation != "unknown" else "unobserved.pdf"
        ScriptedExecutionRequester.responses.append(decision(result={"pdf_path": selected}))
        if mutation != "unknown":
            ScriptedExecutionRequester.responses.append(decision("blocked", {"pdf_path": ""}))
        return {"status": "success", "result": "made", "artifacts": [{**ref, "artifact_type": "file"}]}

    @agent.action_func
    def change_file() -> str:
        if mutation == "changed":
            produced[0].write_bytes(b"different")
        else:
            produced[0].unlink()
        return mutation

    run = agent.create_execution("long_task").input("Generate PDF").use_actions(["make_file", "change_file"]).output({"pdf_path": str})
    run.options["context_budget"] = {"optional_selection": "none"}
    ScriptedExecutionRequester.responses.append(decision("continue", None, [{"action_id": "make_file", "action_input": {}}]))
    if mutation != "unknown":
        # The mutation is a later Action, after the original artifact was observed.
        ScriptedExecutionRequester.responses.append(decision("continue", None, [{"action_id": "change_file", "action_input": {}}]))
    full = await run.async_get_full_data()
    assert not produced[0].exists()
    assert not cast(AgentExecution, run)._terminal_task_handoff_refs
    if mutation != "unknown":
        assert not full["accepted"]
        assert "selected_delivery" in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)


@pytest.mark.asyncio
async def test_file_selection_survives_rework_without_replaying_action(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-file-rework", [])
    calls = []

    @agent.action_func
    async def make_file() -> dict:
        calls.append(1)
        written = await run.task_workspace.write_file("result.txt", "retained body")
        ref = await run.task_workspace._promote_file_identity(written.path)
        ScriptedExecutionRequester.responses.extend([decision(result={"path": ref["path"]}),
                                                     decision(result={"path": ref["path"]})])
        return {"status": "success", "result": {"body": "written", "file_refs": [ref]}}

    run = agent.create_execution("long_task").input("Write file").use_actions("make_file").output({"path": str})
    run.options["context_budget"] = {"optional_selection": "none"}
    ScriptedExecutionRequester.responses.append(decision("continue", None, [{"action_id": "make_file", "action_input": {}}]))
    first = await run.async_get_data()
    await run.async_rework("Keep the useful file")
    assert await run.async_get_data() == first
    assert run.task_workspace.resolve_file_path(first["path"]).read_text() == "retained body"
    assert calls == [1]


@pytest.mark.asyncio
async def test_observation_preserves_small_complete_source_body(tmp_path):
    body = "a" * 4000 + "END_OF_SOURCE"
    agent = create_execution_agent(tmp_path, "loop-full-source", [
        decision("continue", None, [{"action_id": "read_source", "action_input": {}}]), decision()])

    @agent.action_func
    def read_source() -> str:
        return body

    run = agent.create_execution("long_task").input("Read source").use_actions("read_source")
    assert await run.async_get_data() == "done"
    assert body in json.dumps(ScriptedExecutionRequester.requests[-1], default=str)


@pytest.mark.asyncio
async def test_required_binary_delivery_needs_identity_not_text_parser(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-binary", [decision()])
    agent.use_task_workspace(tmp_path / "loop-binary", mode="read_write")
    run = agent.create_execution("long_task").input("Deliver binary")
    run.options["context_budget"] = {"optional_selection": "none"}
    path = run.task_workspace.resolve_path("output.bin")
    path.write_bytes(b"\x00\xff\x01")
    run.task_options["options"] = {"agent_task": {"required_deliverables": ["output.bin"]}}
    assert (await run.async_get_full_data())["accepted"]
    assert path.read_bytes() == b"\x00\xff\x01"


@pytest.mark.asyncio
async def test_native_file_actions_use_execution_identity_and_preserve_code_product(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-native-readback", [])
    agent.enable_task_workspace_file_actions()
    product = None

    @agent.action_func
    async def make_file() -> dict:
        nonlocal product
        written = await run.task_workspace.write_file("result.txt", "code product")
        product = run.task_workspace.resolve_file_path(written.path)
        ref = await run.task_workspace._promote_file_identity(written.path)
        ScriptedExecutionRequester.responses.extend([
            decision("continue", None, [{"action_id": "read_file", "action_input": {"path": ref["path"]}}]),
            decision(result={"path": ref["path"]}),
        ])
        return {"status": "success", "result": "written", "artifacts": [{**ref, "artifact_type": "file"}]}

    run = agent.create_execution("long_task").input("Generate and read file").use_actions(["make_file", "read_file"]).output({"path": str})
    run.options["context_budget"] = {"optional_selection": "none"}
    run.task_options["max_iterations"] = 3
    ScriptedExecutionRequester.responses.append(decision("continue", None, [{"action_id": "make_file", "action_input": {}}]))
    result = await run.async_get_data()
    assert product is not None and product.read_text() == "code product"
    state = cast(AgentExecution, run)._producer_state["content"]
    assert state["files"][result["path"]]["execution_id"] == run.id
    observed = state["observations"][1]
    refs = observed.get("file_refs", observed.get("result", {}).get("file_refs", []))
    assert refs and refs[0]["path"] == result["path"]
    assert not any(item.get("source") == "selected_delivery" for item in state["observations"])


@pytest.mark.asyncio
async def test_native_write_uses_current_execution_private_area(tmp_path):
    agent = create_execution_agent(tmp_path, "loop-native-write", [])
    agent.enable_task_workspace_file_actions()
    run = agent.create_execution("long_task").input("Write note").use_actions("write_file").output({"path": str})
    path = f".agently/files/{run.id}/note.txt"
    ScriptedExecutionRequester.responses.extend([
        decision("continue", None, [{"action_id": "write_file", "action_input": {"path": "note.txt", "content": "actual note"}}]),
        decision(result={"path": path}),
    ])
    run.options["context_budget"] = {"optional_selection": "none"}
    assert (await run.async_get_data())["path"] == path
    assert run.task_workspace.resolve_file_path(path).read_text() == "actual note"
