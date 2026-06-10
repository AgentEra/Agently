import asyncio
import copy
import json
from pathlib import Path

import pytest

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently.types.data import RunContext
from agently.types.data.event import normalize_triggerflow_event_type
from agently.types.trigger_flow import AGGREGATION_SCOPE_META_KEY


def test_trigger_flow_sync_start_returns_close_snapshot():
    flow = TriggerFlow()
    flow.to(lambda data: {"value": data.value}).end()

    assert flow.start("ok") == {"$final_result": {"value": "ok"}}
    with pytest.warns(DeprecationWarning, match="wait_for_result"):
        assert flow.start("ok", wait_for_result=False) == {"$final_result": {"value": "ok"}}

    execution = flow.create_execution(auto_close_timeout=0.0)
    assert execution.start("ok") == {"$final_result": {"value": "ok"}}

    another_execution = flow.create_execution(auto_close_timeout=0.0)
    with pytest.warns(DeprecationWarning, match="wait_for_result"):
        assert another_execution.start("ok", wait_for_result=False) == {"$final_result": {"value": "ok"}}


def test_trigger_flow_start_waits_for_auto_close_snapshot_without_end():
    flow = TriggerFlow()

    async def fan_out(data: TriggerFlowRuntimeData):
        data.emit_nowait("Side", data.value + 1)

    async def side(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.01)
        await data.async_set_state("side", data.value)

    flow.to(fan_out)
    flow.when("Side").to(side)

    assert flow.start(1, auto_close_timeout=0.03) == {"side": 2}


@pytest.mark.asyncio
async def test_trigger_flow_execution_save_and_load_then_continue():
    flow = TriggerFlow()

    async def init(data: TriggerFlowRuntimeData):
        data.set_runtime_data("draft", {"topic": "pricing"})
        data.set_flow_data("global_flag", True)
        return "waiting"

    async def finalize(data: TriggerFlowRuntimeData):
        return {
            "feedback": data.value,
            "draft": data.get_runtime_data("draft"),
            "global_flag": data.get_flow_data("global_flag"),
        }

    flow.to(init)
    flow.when("UserFeedback").to(finalize).end()

    execution = await flow.async_start_execution("start")
    saved_state = execution.save()
    json.dumps(saved_state)
    assert "version" not in saved_state

    restored_execution = flow.create_execution()
    restored_execution.load(saved_state)
    await restored_execution.async_emit("UserFeedback", {"approve": True})
    result = await restored_execution.async_get_result(timeout=1)

    assert result == {
        "feedback": {"approve": True},
        "draft": {"topic": "pricing"},
        "global_flag": True,
    }


def test_trigger_flow_checkpoint_records_definition_fingerprint_and_rejects_mismatch():
    flow = TriggerFlow(name="checkpoint-fingerprint")

    async def original_stage(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(original_stage)
    saved_state = flow.create_execution(auto_close=False).save()
    fingerprint = saved_state["checkpoint"]["flow_definition_fingerprint"]

    assert fingerprint.startswith("sha256:")
    report = flow.create_execution(auto_close=False).inspect_rehydration(saved_state)
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["current_flow_definition_fingerprint"] == fingerprint

    incompatible_flow = TriggerFlow(name="checkpoint-fingerprint")

    async def incompatible_stage(data: TriggerFlowRuntimeData):
        return {"changed": data.value}

    incompatible_flow.to(incompatible_stage)
    incompatible_report = incompatible_flow.create_execution(auto_close=False).inspect_rehydration(saved_state)

    assert incompatible_report["ready"] is False
    assert incompatible_report["status"] == "invalid_snapshot"
    assert {
        diagnostic["code"]
        for diagnostic in incompatible_report["diagnostics"]
    } == {"triggerflow.checkpoint.flow_definition_mismatch"}
    with pytest.raises(ValueError, match="flow definition fingerprint mismatch"):
        incompatible_flow.create_execution(auto_close=False).load(saved_state)


def test_trigger_flow_checkpoint_rejects_invalid_kind_and_schema_version():
    flow = TriggerFlow(name="checkpoint-contract")

    async def stage(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(stage)
    saved_state = flow.create_execution(auto_close=False).save()

    invalid_kind = copy.deepcopy(saved_state)
    invalid_kind["checkpoint"]["kind"] = "unknown.snapshot"
    kind_report = flow.create_execution(auto_close=False).inspect_rehydration(invalid_kind)
    assert kind_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.checkpoint.invalid_kind"
        for diagnostic in kind_report["diagnostics"]
    )
    with pytest.raises(ValueError, match="checkpoint kind"):
        flow.create_execution(auto_close=False).load(invalid_kind)

    invalid_schema = copy.deepcopy(saved_state)
    invalid_schema["checkpoint"]["schema_version"] = 999
    schema_report = flow.create_execution(auto_close=False).inspect_rehydration(invalid_schema)
    assert schema_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.checkpoint.invalid_schema_version"
        for diagnostic in schema_report["diagnostics"]
    )
    with pytest.raises(ValueError, match="schema_version"):
        flow.create_execution(auto_close=False).load(invalid_schema)

    missing_fingerprint = copy.deepcopy(saved_state)
    missing_fingerprint["checkpoint"].pop("flow_definition_fingerprint")
    fingerprint_report = flow.create_execution(auto_close=False).inspect_rehydration(missing_fingerprint)
    assert fingerprint_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.checkpoint.missing_flow_definition_fingerprint"
        for diagnostic in fingerprint_report["diagnostics"]
    )
    with pytest.raises(ValueError, match="flow definition fingerprint"):
        flow.create_execution(auto_close=False).load(missing_fingerprint)


@pytest.mark.asyncio
async def test_trigger_flow_execution_load_from_json_string():
    flow = TriggerFlow()

    async def setup(data: TriggerFlowRuntimeData):
        data.set_runtime_data("checkpoint", {"step": 2})
        return data.value

    flow.to(setup).end()
    execution = await flow.async_start_execution("ok")
    saved_state = execution.save()

    restored_execution = flow.create_execution()
    restored_execution.load(json.dumps(saved_state))

    assert restored_execution.get_runtime_data("checkpoint") == {"step": 2}


@pytest.mark.asyncio
async def test_trigger_flow_execution_checkpoint_restores_scoped_when_join_progress_after_load():
    flow = TriggerFlow(name="checkpoint-scoped-join")

    async def emit_left(data: TriggerFlowRuntimeData):
        await data.async_emit("A", {"left": data.value})

    async def joined(data: TriggerFlowRuntimeData):
        await data.async_set_state("joined", data.value, emit=False)

    flow.when("Run").to(emit_left)
    flow.when(["A", "B"], mode="and").to(joined)

    execution = flow.create_execution(auto_close=False)
    await execution.async_emit("Run", "task-1")
    saved_state = execution.save()

    checkpoint = saved_state["checkpoint"]
    assert checkpoint["schema_version"] == 1
    durable_when_states = checkpoint["durable_system_state"]["when_states"]
    signal_scope_keys = [
        scope_key
        for when_state in durable_when_states.values()
        for scope_key in when_state.keys()
        if str(scope_key).startswith("signal:")
    ]
    assert len(signal_scope_keys) == 1
    aggregation_scope = signal_scope_keys[0].removeprefix("signal:")

    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.load(saved_state)
    await restored_execution.async_emit(
        "B",
        {"right": "task-1"},
        _meta={AGGREGATION_SCOPE_META_KEY: aggregation_scope},
    )
    await restored_execution.async_close()

    assert restored_execution.get_state("joined") == {
        "event": {
            "A": {"left": "task-1"},
            "B": {"right": "task-1"},
        }
    }


@pytest.mark.asyncio
async def test_trigger_flow_execution_load_restore_ready_result():
    flow = TriggerFlow()
    flow.to(lambda data: data.value).end()

    execution = flow.create_execution()
    execution.set_result({"done": True})
    saved_state = execution.save()

    restored_execution = flow.create_execution()
    restored_execution.load(saved_state)
    result = await restored_execution.async_get_result(timeout=0.01)

    assert result == {"done": True}


@pytest.mark.asyncio
async def test_trigger_flow_execution_checkpoint_preserves_self_resume_count_after_load():
    flow = TriggerFlow(name="checkpoint-self-resume")

    async def always_pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="approval",
            resume_to="self",
            max_resumes=2,
        )

    flow.to(always_pause)
    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    await execution.async_continue_with("approval", {"round": 1})

    pending = execution.get_pending_interrupts()
    assert pending["approval"]["resume_count"] == 1
    assert pending["approval"]["max_resumes"] == 2

    saved_state = execution.save()
    assert saved_state["interrupts"]["approval"]["resume_count"] == 1

    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.load(saved_state)
    restored_pending = restored_execution.get_pending_interrupts()
    assert restored_pending["approval"]["resume_count"] == 1
    assert restored_pending["approval"]["max_resumes"] == 2

    with pytest.raises(RuntimeError, match="self resume limit"):
        await restored_execution.async_continue_with("approval", {"round": 2})


@pytest.mark.asyncio
async def test_trigger_flow_continue_with_resume_request_id_is_idempotent_after_load():
    flow = TriggerFlow(name="checkpoint-resume-idempotency")

    async def gate(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        resumes = data.get_state("resumes", []) or []
        resumes.append(data.value)
        await data.async_set_state("resumes", resumes, emit=False)

    flow.to(gate).to(finalize)
    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    await execution.async_continue_with(
        "approval",
        {"approved": True},
        resume_request_id="resume-1",
        actor="reviewer",
    )
    saved_state = execution.save()
    assert saved_state["checkpoint"]["resume_ledger"]["approval"]["resume-1"]["status"] == "accepted"

    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.load(saved_state)
    retry_result = await restored_execution.async_continue_with(
        "approval",
        {"approved": True},
        resume_request_id="resume-1",
        actor="reviewer",
    )
    with pytest.raises(ValueError, match="conflicting resume_request_id"):
        await restored_execution.async_continue_with(
            "approval",
            {"approved": False},
            resume_request_id="resume-1",
            actor="reviewer",
        )
    await restored_execution.async_close()

    assert retry_result is not None
    assert retry_result["resume_request_id"] == "resume-1"
    assert retry_result["resumed_by"] == "reviewer"
    assert restored_execution.get_state("resumes") == [{"approved": True}]


@pytest.mark.asyncio
async def test_trigger_flow_execution_save_to_json_file_and_load_from_file(tmp_path: Path):
    flow = TriggerFlow()
    flow.to(lambda data: data.value).end()

    execution = flow.create_execution()
    execution.set_runtime_data("checkpoint", {"step": 1})
    json_path = tmp_path / "execution_state.json"

    saved_state = execution.save(json_path)
    assert json_path.exists()
    assert isinstance(saved_state, dict)

    restored_execution = flow.create_execution()
    restored_execution.load(json_path)
    assert restored_execution.get_runtime_data("checkpoint") == {"step": 1}


@pytest.mark.asyncio
async def test_trigger_flow_execution_save_to_yaml_file_and_load_from_file(tmp_path: Path):
    flow = TriggerFlow()
    flow.to(lambda data: data.value).end()

    execution = flow.create_execution()
    execution.set_runtime_data("checkpoint", {"step": 2})
    yaml_path = tmp_path / "execution_state.yaml"

    execution.save(yaml_path)
    assert yaml_path.exists()

    restored_execution = flow.create_execution()
    restored_execution.load(yaml_path)
    assert restored_execution.get_runtime_data("checkpoint") == {"step": 2}


def test_trigger_flow_execution_save_and_load_preserves_run_context():
    flow = TriggerFlow(name="persisted-lineage-flow")
    parent_run = RunContext.create(
        run_kind="request",
        agent_id="agent-1",
        agent_name="tester",
        session_id="session-1",
    )

    execution = flow.create_execution(parent_run_context=parent_run)
    saved_state = execution.save()

    assert saved_state["run_context"]["run_id"] == execution.run_context.run_id
    assert saved_state["run_context"]["parent_run_id"] == parent_run.run_id
    assert saved_state["run_context"]["root_run_id"] == parent_run.root_run_id

    restored_execution = flow.create_execution()
    restored_execution.load(saved_state)

    assert restored_execution.id == execution.id
    assert restored_execution.run_context.run_id == execution.run_context.run_id
    assert restored_execution.run_context.parent_run_id == parent_run.run_id
    assert restored_execution.run_context.root_run_id == parent_run.root_run_id
    assert restored_execution.run_context.execution_id == execution.id
    assert restored_execution.run_context.agent_id == "agent-1"
    assert restored_execution.run_context.agent_name == "tester"
    assert restored_execution.run_context.session_id == "session-1"


@pytest.mark.asyncio
async def test_trigger_flow_execution_close_returns_state_snapshot():
    flow = TriggerFlow()
    execution = flow.create_execution(auto_close=False)

    await execution.async_set_state("checkpoint", {"step": 1})
    result = await execution.async_close()

    assert result == {"checkpoint": {"step": 1}}
    assert execution.is_closed()
    assert execution.get_lifecycle_state() == "closed"


@pytest.mark.asyncio
async def test_trigger_flow_runtime_data_input_aliases_follow_value():
    flow = TriggerFlow()

    async def rewrite(data: TriggerFlowRuntimeData):
        assert data.input == "original"
        assert data.inputs == "original"
        data.value = "rewritten"
        await data.async_set_state("input", data.input)
        await data.async_set_state("inputs", data.inputs)

    flow.to(rewrite)
    execution = flow.create_execution(auto_close=False)
    returned_execution = await execution.async_start("original")
    state = await execution.async_close()

    assert returned_execution is execution
    assert state == {"input": "rewritten", "inputs": "rewritten"}


@pytest.mark.asyncio
async def test_trigger_flow_execution_seal_rejects_new_events_until_unsealed():
    flow = TriggerFlow()
    observed = []

    async def collect(data: TriggerFlowRuntimeData):
        observed.append(data.value)

    flow.when("External").to(collect)
    execution = flow.create_execution(auto_close=False)

    await execution.async_seal()
    with pytest.warns(RuntimeWarning, match="ignored event 'External'"):
        await execution.async_emit("External", "sealed")
    assert observed == []

    await execution.async_unseal()
    await execution.async_emit("External", "open")
    assert observed == ["open"]


@pytest.mark.asyncio
async def test_trigger_flow_execution_close_drains_registered_nowait_emit():
    flow = TriggerFlow()

    async def kick(data: TriggerFlowRuntimeData):
        data.emit_nowait("Side", data.value + 1)
        await data.async_set_state("kick", data.value)

    async def side(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.01)
        await data.async_set_state("side", data.value)

    flow.when("Kick").to(kick)
    flow.when("Side").to(side)
    execution = flow.create_execution(auto_close=False)

    await execution.async_emit("Kick", 1)
    result = await execution.async_close(timeout=1)

    assert result == {"kick": 1, "side": 2}
    assert execution.is_closed()


@pytest.mark.asyncio
async def test_trigger_flow_execution_close_drains_chained_nowait_emits():
    flow = TriggerFlow()

    async def start_loop(data: TriggerFlowRuntimeData):
        await data.async_set_state("values", [], emit=False)
        data.emit_nowait("Tick", 1)

    async def on_tick(data: TriggerFlowRuntimeData):
        values = data.get_state("values", []) or []
        values.append(data.value)
        await data.async_set_state("values", values, emit=False)
        if data.value < 3:
            data.emit_nowait("Tick", data.value + 1)

    flow.to(start_loop)
    flow.when("Tick").to(on_tick)
    execution = flow.create_execution(auto_close=False)

    returned_execution = await execution.async_start(None)
    result = await execution.async_close(timeout=1)

    assert returned_execution is execution
    assert result["values"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_trigger_flow_execution_auto_closes_after_idle_timeout():
    flow = TriggerFlow()

    async def remember(data: TriggerFlowRuntimeData):
        await data.async_set_state("value", data.value)

    flow.when("Ping").to(remember)
    execution = flow.create_execution(auto_close=True, auto_close_timeout=0.03)

    await execution.async_emit("Ping", "pong")
    for _ in range(20):
        if execution.is_closed():
            break
        await asyncio.sleep(0.02)

    assert execution.is_closed()
    assert execution.get_state("value") == "pong"


@pytest.mark.asyncio
async def test_trigger_flow_execution_close_stops_runtime_stream():
    flow = TriggerFlow()

    async def emit_stream(data: TriggerFlowRuntimeData):
        await data.async_put_into_stream({"value": data.value})

    flow.to(emit_stream)
    execution = flow.create_execution(auto_close=False)
    stream = execution.get_async_runtime_stream("topic", timeout=1)

    first_item = await stream.__anext__()
    await execution.async_close()
    remaining_items = [item async for item in stream]

    assert first_item == {"value": "topic"}
    assert remaining_items == []


def test_trigger_flow_start_propagates_parent_run_context():
    flow = TriggerFlow(name="start-parent-lineage")
    flow.to(lambda data: data.value).end()

    parent_run = RunContext.create(
        run_kind="request",
        agent_id="agent-sync",
        agent_name="sync-owner",
        session_id="sync-session",
    )
    captured = []

    def capture(event):
        if normalize_triggerflow_event_type(event.event_type) == "triggerflow.execution_started" and event.run is not None:
            captured.append(event)

    hook_name = "test_trigger_flow_start_propagates_parent_run_context.capture"
    Agently.event_center.register_hook(capture, event_types="workflow.execution_started", hook_name=hook_name)
    try:
        assert flow.start("ok", parent_run_context=parent_run) == {"$final_result": "ok"}
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert len(captured) == 1
    event = captured[0]
    assert event.run.parent_run_id == parent_run.run_id
    assert event.run.root_run_id == parent_run.root_run_id
    assert event.run.agent_id == "agent-sync"
    assert event.run.agent_name == "sync-owner"
    assert event.run.session_id == "sync-session"


@pytest.mark.asyncio
async def test_trigger_flow_runtime_stream_propagates_parent_run_context():
    flow = TriggerFlow(name="stream-parent-lineage")

    async def stream_once(data: TriggerFlowRuntimeData):
        await data.async_put_into_stream({"value": data.value})
        await data.async_stop_stream()
        return data.value

    flow.to(stream_once).end()

    parent_run = RunContext.create(
        run_kind="request",
        agent_id="agent-stream",
        agent_name="stream-owner",
        session_id="stream-session",
    )
    captured = []

    async def capture(event):
        if normalize_triggerflow_event_type(event.event_type) == "triggerflow.execution_started" and event.run is not None:
            captured.append(event)

    hook_name = "test_trigger_flow_runtime_stream_propagates_parent_run_context.capture"
    Agently.event_center.register_hook(capture, event_types="workflow.execution_started", hook_name=hook_name)
    try:
        items = [item async for item in flow.get_async_runtime_stream("topic", parent_run_context=parent_run, timeout=1)]
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert items == [{"value": "topic"}]
    assert len(captured) == 1
    event = captured[0]
    assert event.run.parent_run_id == parent_run.run_id
    assert event.run.root_run_id == parent_run.root_run_id
    assert event.run.agent_id == "agent-stream"
    assert event.run.agent_name == "stream-owner"
    assert event.run.session_id == "stream-session"
