import asyncio
import copy
import json
from pathlib import Path

import pytest
from agently_stage import Stage, default_stage_call_bridge

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


def test_sync_chunk_can_wrap_async_provider_with_stage_and_reenter_execution_state():
    flow = TriggerFlow(name="sync-provider-stage-roundtrip")

    async def provider(value: str) -> str:
        await asyncio.sleep(0)
        return value.upper()

    async def registered_action(value: str) -> str:
        with Stage() as stage:
            return stage.get(provider, value)

    def sync_provider(value: str) -> str:
        with Stage() as stage:
            return stage.get(registered_action, value)

    def transform(data: TriggerFlowRuntimeData):
        result = sync_provider(data.value)
        data.set_state("result", result, emit=False)
        data.set_state("steps", [result], emit=False)
        data.append_state("steps", "settled", emit=False)
        data.set_state("temporary", True, emit=False)
        data.del_state("temporary", emit=False)
        return {
            "result": result,
            "steps": data.get_state("steps"),
            "temporary": data.get_state("temporary"),
        }

    flow.to(transform)

    assert flow.start("stage") == {
        "result": "STAGE",
        "steps": ["STAGE", "settled"],
    }


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


def test_trigger_flow_sync_set_state_replaces_collection_value():
    execution = TriggerFlow().create_execution(auto_close=False)

    execution.set_state("pending", ["a", "b"], emit=False)
    execution.set_state("pending", [], emit=False)

    assert execution.get_state("pending") == []
    execution.close()


def test_trigger_flow_set_flow_data_replaces_collection_value():
    flow = TriggerFlow()

    flow.set_flow_data("shared", {"stale": True}, emit=False, no_warning=True)
    flow.set_flow_data("shared", {}, emit=False, no_warning=True)

    assert flow.get_flow_data("shared", no_warning=True) == {}


@pytest.mark.asyncio
async def test_trigger_flow_async_set_state_replaces_mapping_and_dot_path_values():
    execution = TriggerFlow().create_execution(auto_close=False)

    await execution.async_set_state("draft", {"facts": {"old": True}, "status": "stale"}, emit=False)
    await execution.async_set_state("draft.facts", {"new": True}, emit=False)
    assert execution.get_state("draft") == {"facts": {"new": True}, "status": "stale"}

    await execution.async_set_state("draft", {}, emit=False)
    assert execution.get_state("draft") == {}

    await asyncio.gather(
        execution.async_set_state("race", {"first": True}, emit=False),
        execution.async_set_state("race", {"second": True}, emit=False),
    )
    assert execution.get_state("race") in ({"first": True}, {"second": True})
    await execution.async_close()


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


@pytest.mark.asyncio
async def test_trigger_flow_signal_net_dynamic_binding_fanout_and_join():
    flow = TriggerFlow(name="signal-net-dynamic-fanout")
    active_count = 0
    max_active_count = 0
    lock = asyncio.Lock()

    async def prepare(data: TriggerFlowRuntimeData):
        await data.async_set_state("expected", len(data.value))
        await data.async_set_state("results", [])
        for item in data.value:
            await data.async_emit_nowait("dynamic.item", {"item": item})

    async def dynamic_item(data: TriggerFlowRuntimeData):
        nonlocal active_count, max_active_count
        async with lock:
            active_count += 1
            max_active_count = max(max_active_count, active_count)
        await asyncio.sleep(0.02)
        try:
            item = data.value["item"]
            async with lock:
                results = list(data.get_state("results", [], inherit=False))
                results.append(item * 2)
                await data.async_set_state("results", results)
                if len(results) == data.get_state("expected", 0, inherit=False):
                    await data.async_emit("dynamic.done", sorted(results))
        finally:
            async with lock:
                active_count -= 1

    async def finish(data: TriggerFlowRuntimeData):
        return {"results": data.value}

    flow.to(prepare)
    flow.when("dynamic.done").to(finish).end()
    execution = flow.create_execution(auto_close=False, concurrency=3)
    execution.on(
        "dynamic.item",
        dynamic_item,
        binding_id="test.dynamic_item",
    )

    await execution.async_start([1, 2, 3])
    snapshot = await execution.async_close(timeout=1)

    assert snapshot["$final_result"] == {"results": [2, 4, 6]}
    assert max_active_count > 1
    binding = execution.save()["signal_net"]["bindings"][0]
    assert binding["binding_id"] == "test.dynamic_item"
    assert binding["trigger_event"] == "dynamic.item"


@pytest.mark.asyncio
async def test_trigger_flow_signal_net_nested_emit_respects_concurrency_cap():
    flow = TriggerFlow(name="signal-net-nested-concurrency")
    active_count = 0
    max_active_count = 0
    lock = asyncio.Lock()

    async def prepare(data: TriggerFlowRuntimeData):
        await data.async_set_state("expected", len(data.value))
        await data.async_set_state("results", [])
        for item in data.value:
            await data.async_emit_nowait("dynamic.root", {"item": item})

    async def track_start():
        nonlocal active_count, max_active_count
        async with lock:
            active_count += 1
            max_active_count = max(max_active_count, active_count)

    async def track_done():
        nonlocal active_count
        async with lock:
            active_count -= 1

    async def dynamic_root(data: TriggerFlowRuntimeData):
        await track_start()
        try:
            await asyncio.sleep(0.01)
            await data.async_emit_nowait("dynamic.child", {"item": data.value["item"]})
        finally:
            await track_done()

    async def dynamic_child(data: TriggerFlowRuntimeData):
        await track_start()
        try:
            await asyncio.sleep(0.01)
            item = data.value["item"]
            async with lock:
                results = list(data.get_state("results", [], inherit=False))
                results.append(item * 10)
                await data.async_set_state("results", results)
                if len(results) == data.get_state("expected", 0, inherit=False):
                    await data.async_emit("dynamic.done", sorted(results))
        finally:
            await track_done()

    async def finish(data: TriggerFlowRuntimeData):
        return {"results": data.value}

    flow.to(prepare)
    flow.when("dynamic.done").to(finish).end()
    execution = flow.create_execution(auto_close=False, concurrency=1)
    execution.on("dynamic.root", dynamic_root, binding_id="test.dynamic_root")
    execution.on("dynamic.child", dynamic_child, binding_id="test.dynamic_child")

    await execution.async_start([1, 2, 3])
    snapshot = await execution.async_close(timeout=1)
    signal_net_state = execution.save()["signal_net"]
    completed_events = {
        attempt["trigger_event"]
        for attempt in signal_net_state["signal_attempts"]
        if attempt["status"] == "completed"
    }

    assert snapshot["$final_result"] == {"results": [10, 20, 30]}
    assert max_active_count == 1
    assert {"dynamic.root", "dynamic.child"}.issubset(completed_events)


@pytest.mark.asyncio
async def test_trigger_flow_defers_handler_awaitable_creation_until_concurrency_permit(
    monkeypatch,
):
    flow = TriggerFlow(name="deferred-handler-awaitable")
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    handler_awaitables_created = 0

    async def prepare(data: TriggerFlowRuntimeData):
        await data.async_emit_nowait("dynamic.item", {"item": 1})
        await data.async_emit_nowait("dynamic.item", {"item": 2})

    async def dynamic_handler(_data: TriggerFlowRuntimeData):
        handler_started.set()
        await release_handler.wait()

    original_asyncify = default_stage_call_bridge.as_async

    def tracking_asyncify(func, **options):
        async_func = original_asyncify(func, **options)
        if func is not dynamic_handler:
            return async_func

        def create_awaitable(*args, **kwargs):
            nonlocal handler_awaitables_created
            handler_awaitables_created += 1
            return async_func(*args, **kwargs)

        return create_awaitable

    monkeypatch.setattr(default_stage_call_bridge, "as_async", tracking_asyncify)
    flow.to(prepare)
    execution = flow.create_execution(auto_close=False, concurrency=1)
    execution.on(
        "dynamic.item",
        dynamic_handler,
        binding_id="test.deferred_handler_awaitable",
    )

    await execution.async_start(None)
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    await asyncio.sleep(0.01)
    try:
        assert handler_awaitables_created == 1
    finally:
        release_handler.set()
        await execution.async_close(timeout=1)


def test_trigger_flow_signal_net_rejects_anonymous_durable_handler():
    flow = TriggerFlow(name="signal-net-anonymous-handler")
    execution = flow.create_execution(auto_close=False)

    with pytest.raises(ValueError, match="recoverable handler ref"):
        execution.on("dynamic.item", lambda data: None)


@pytest.mark.asyncio
async def test_trigger_flow_signal_net_snapshot_requires_dynamic_handler_before_load():
    flow = TriggerFlow(name="signal-net-load")

    async def dynamic_handler(data: TriggerFlowRuntimeData):
        await data.async_set_state("seen", data.value)

    execution = flow.create_execution(auto_close=False)
    execution.on(
        "dynamic.item",
        dynamic_handler,
        binding_id="test.dynamic_load_handler",
    )
    saved_state = execution.save()

    missing_handler_execution = flow.create_execution(auto_close=False)
    missing_report = missing_handler_execution.inspect_load(saved_state)
    assert missing_report["ready"] is False
    assert missing_report["status"] == "missing_resources"
    assert "dynamic_event_handler:test.dynamic_load_handler" in missing_report["missing_resource_keys"]

    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.on(
        "dynamic.item",
        dynamic_handler,
        binding_id="test.dynamic_load_handler",
    )
    restored_execution.load(saved_state, validate_resources=True)
    await restored_execution.async_emit("dynamic.item", {"value": "restored"})
    snapshot = await restored_execution.async_close(timeout=1)

    assert snapshot["seen"] == {"value": "restored"}
    assert restored_execution.save()["signal_net"]["bindings"][0]["status"] == "active"


def test_trigger_flow_signal_net_load_marks_nonterminal_attempts_interrupted():
    flow = TriggerFlow(name="signal-net-interrupted-attempt")
    execution = flow.create_execution(auto_close=False)
    saved_state = execution.save()
    saved_state["signal_net"]["accepted_signal_ids"] = ["signal-1"]
    saved_state["signal_net"]["signal_attempts"] = [
        {
            "signal_id": "signal-1",
            "trigger_type": "event",
            "trigger_event": "dynamic.item",
            "source": "runtime",
            "status": "running",
        }
    ]

    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.load(saved_state)
    restored_state = restored_execution.save()["signal_net"]

    assert restored_state["accepted_signal_ids"] == []
    assert restored_state["signal_attempts"][0]["status"] == "interrupted"


def test_trigger_flow_snapshot_records_definition_fingerprint_and_rejects_mismatch():
    flow = TriggerFlow(name="snapshot-fingerprint")

    async def original_stage(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(original_stage)
    saved_state = flow.create_execution(auto_close=False).save()
    fingerprint = saved_state["flow_definition_fingerprint"]

    assert fingerprint.startswith("sha256:")
    report = flow.create_execution(auto_close=False).inspect_load(saved_state)
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["current_flow_definition_fingerprint"] == fingerprint

    incompatible_flow = TriggerFlow(name="snapshot-fingerprint")

    async def incompatible_stage(data: TriggerFlowRuntimeData):
        return {"changed": data.value}

    incompatible_flow.to(incompatible_stage)
    incompatible_report = incompatible_flow.create_execution(auto_close=False).inspect_load(saved_state)

    assert incompatible_report["ready"] is False
    assert incompatible_report["status"] == "invalid_snapshot"
    assert {
        diagnostic["code"]
        for diagnostic in incompatible_report["diagnostics"]
    } == {"triggerflow.snapshot.flow_definition_mismatch"}
    with pytest.raises(ValueError, match="flow definition fingerprint mismatch"):
        incompatible_flow.create_execution(auto_close=False).load(saved_state)


def test_trigger_flow_snapshot_rejects_invalid_kind_and_schema_version():
    flow = TriggerFlow(name="snapshot-contract")

    async def stage(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(stage)
    saved_state = flow.create_execution(auto_close=False).save()

    invalid_kind = copy.deepcopy(saved_state)
    invalid_kind["kind"] = "unknown.snapshot"
    kind_report = flow.create_execution(auto_close=False).inspect_load(invalid_kind)
    assert kind_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.snapshot.invalid_kind"
        for diagnostic in kind_report["diagnostics"]
    )
    with pytest.raises(ValueError, match="execution snapshot kind"):
        flow.create_execution(auto_close=False).load(invalid_kind)

    invalid_schema = copy.deepcopy(saved_state)
    invalid_schema["schema_version"] = 999
    schema_report = flow.create_execution(auto_close=False).inspect_load(invalid_schema)
    assert schema_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.snapshot.invalid_schema_version"
        for diagnostic in schema_report["diagnostics"]
    )
    with pytest.raises(ValueError, match="schema_version"):
        flow.create_execution(auto_close=False).load(invalid_schema)

    missing_fingerprint = copy.deepcopy(saved_state)
    missing_fingerprint.pop("flow_definition_fingerprint")
    fingerprint_report = flow.create_execution(auto_close=False).inspect_load(missing_fingerprint)
    assert fingerprint_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.snapshot.missing_flow_definition_fingerprint"
        for diagnostic in fingerprint_report["diagnostics"]
    )
    with pytest.raises(ValueError, match="flow definition fingerprint"):
        flow.create_execution(auto_close=False).load(missing_fingerprint)


def test_trigger_flow_load_reports_lease_expiry_and_owner_conflict():
    flow = TriggerFlow(name="snapshot-lease-diagnostics")
    saved_state = flow.create_execution(
        auto_close=False,
        owner_id="worker-1",
        lease_ttl=60.0,
    ).save()

    conflict_report = flow.create_execution(
        auto_close=False,
        owner_id="worker-2",
    ).inspect_load(saved_state)
    assert conflict_report["ready"] is False
    assert conflict_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.lease.owner_conflict"
        for diagnostic in conflict_report["diagnostics"]
    )

    expired_state = copy.deepcopy(saved_state)
    expired_state["lease_until"] = 1.0
    expired_state["lease"]["lease_until"] = 1.0
    expired_report = flow.create_execution(
        auto_close=False,
        owner_id="worker-2",
    ).inspect_load(expired_state)
    assert expired_report["ready"] is True
    assert expired_report["status"] == "ready"
    assert any(
        diagnostic["code"] == "triggerflow.lease.expired"
        and diagnostic["severity"] == "warning"
        for diagnostic in expired_report["diagnostics"]
    )
    assert not any(
        diagnostic["code"] == "triggerflow.lease.owner_conflict"
        for diagnostic in expired_report["diagnostics"]
    )


@pytest.mark.asyncio
async def test_trigger_flow_continue_with_fails_fast_after_lease_expiry_and_handoff():
    flow = TriggerFlow(name="snapshot-lease-callback-handoff")

    async def gate(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        await data.async_set_state("approval", data.value, emit=False)

    flow.to(gate).to(finalize)
    execution = flow.create_execution(
        auto_close=False,
        owner_id="worker-1",
        lease_ttl=30.0,
    )
    await execution.async_start(None)
    execution.heartbeat_lease(owner_id="worker-1", lease_ttl=1.0, now=1.0)

    with pytest.raises(RuntimeError, match="lease.*expired"):
        await execution.async_continue_with(
            "approval",
            {"approved": True},
            resume_request_id="late-callback",
            actor="reviewer",
        )

    pending = execution.get_pending_interrupts()["approval"]
    assert "late-callback" not in pending.get("resume_requests", {})

    expired_state = execution.save()
    handoff = flow.create_execution(auto_close=False, owner_id="worker-2")
    report = handoff.inspect_load(expired_state)
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert any(
        diagnostic["code"] == "triggerflow.lease.expired"
        for diagnostic in report["diagnostics"]
    )

    await handoff.async_load(expired_state)
    handoff.claim_lease("worker-2", lease_ttl=30.0)
    await handoff.async_continue_with(
        "approval",
        {"approved": True},
        resume_request_id="handoff-callback",
        actor="reviewer",
    )
    snapshot = await handoff.async_close()
    await execution.async_close(pending_interrupts="cancel")

    assert snapshot["approval"] == {"approved": True}
    assert (
        handoff.save()["resume_ledger"]["approval"]["handoff-callback"]["status"]
        == "completed"
    )


@pytest.mark.asyncio
async def test_trigger_flow_execution_load_from_json_string():
    flow = TriggerFlow()

    async def setup(data: TriggerFlowRuntimeData):
        data.set_runtime_data("progress_marker", {"step": 2})
        return data.value

    flow.to(setup).end()
    execution = await flow.async_start_execution("ok")
    saved_state = execution.save()

    restored_execution = flow.create_execution()
    restored_execution.load(json.dumps(saved_state))

    assert restored_execution.get_runtime_data("progress_marker") == {"step": 2}


@pytest.mark.asyncio
async def test_trigger_flow_execution_snapshot_restores_scoped_when_join_progress_after_load():
    flow = TriggerFlow(name="snapshot-scoped-join")

    async def emit_left(data: TriggerFlowRuntimeData):
        await data.async_emit("A", {"left": data.value})

    async def joined(data: TriggerFlowRuntimeData):
        await data.async_set_state("joined", data.value, emit=False)

    flow.when("Run").to(emit_left)
    flow.when(["A", "B"], mode="and").to(joined)

    execution = flow.create_execution(auto_close=False)
    await execution.async_emit("Run", "task-1")
    saved_state = execution.save()

    snapshot = saved_state
    assert snapshot["schema_version"] == 2
    durable_when_states = snapshot["durable_system_state"]["when_states"]
    signal_scope_keys = [
        scope_key
        for when_state in durable_when_states.values()
        for scope_key in when_state.keys()
        if str(scope_key).startswith("signal:")
    ]
    assert len(signal_scope_keys) == 1
    aggregation_scope = signal_scope_keys[0].removeprefix("signal:")

    tampered_state = copy.deepcopy(saved_state)
    tampered_when_states = tampered_state["durable_system_state"]["when_states"]
    gate_state = next(iter(tampered_when_states.values()))
    scope_state = gate_state[signal_scope_keys[0]]
    scope_state["event"].pop("B")
    tampered_report = flow.create_execution(auto_close=False).inspect_load(tampered_state)
    assert tampered_report["ready"] is False
    assert tampered_report["status"] == "invalid_snapshot"
    assert any(
        diagnostic["code"] == "triggerflow.when_join.missing_event"
        for diagnostic in tampered_report["diagnostics"]
    )
    with pytest.raises(ValueError, match="join state"):
        flow.create_execution(auto_close=False).load(tampered_state)

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
async def test_trigger_flow_execution_load_keeps_ready_result():
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
async def test_trigger_flow_execution_snapshot_preserves_self_resume_count_after_load():
    flow = TriggerFlow(name="snapshot-self-resume")

    async def always_pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
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
    flow = TriggerFlow(name="snapshot-resume-idempotency")

    async def gate(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
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
    assert saved_state["resume_ledger"]["approval"]["resume-1"]["status"] == "completed"

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
async def test_trigger_flow_dispatch_failed_resume_request_retries_after_load(monkeypatch):
    flow = TriggerFlow(name="snapshot-resume-dispatch-failed")

    async def gate(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        await data.async_set_state("approval", data.value, emit=False)

    flow.to(gate).to(finalize)
    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)

    original_dispatch = execution._async_dispatch_signal
    dispatch_attempts = 0

    async def fail_first_dispatch(*args, **kwargs):
        nonlocal dispatch_attempts
        dispatch_attempts += 1
        if dispatch_attempts == 1:
            raise RuntimeError("dispatch failed")
        return await original_dispatch(*args, **kwargs)

    monkeypatch.setattr(execution, "_async_dispatch_signal", fail_first_dispatch)

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await execution.async_continue_with(
            "approval",
            {"approved": True},
            resume_request_id="resume-dispatch-failed-1",
            actor="reviewer",
        )

    pending = execution.get_pending_interrupts()
    request = pending["approval"]["resume_requests"]["resume-dispatch-failed-1"]

    assert pending["approval"]["status"] == "waiting"
    assert pending["approval"]["external_wait_request"]["dispatch_state"] == "dispatch_failed"
    assert request["status"] == "dispatch_failed"
    assert execution.is_waiting()

    failed_state = execution.save()
    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.load(failed_state)
    await restored_execution.async_continue_with(
        "approval",
        {"approved": True},
        resume_request_id="resume-dispatch-failed-1",
        actor="reviewer",
    )
    retried_state = restored_execution.save()

    assert restored_execution.get_state("approval") == {"approved": True}
    assert (
        retried_state["resume_ledger"]["approval"]["resume-dispatch-failed-1"]["status"]
        == "completed"
    )
    assert (
        retried_state["resume_ledger"]["approval"]["resume-dispatch-failed-1"]["dispatch_attempts"]
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_status", "diagnostic_code"),
    [
        ("accepted", "triggerflow.resume.accepted_not_dispatched"),
        ("dispatched", "triggerflow.resume.dispatched_not_completed"),
    ],
)
async def test_trigger_flow_resume_request_replays_crash_window_from_ledger(
    request_status: str,
    diagnostic_code: str,
):
    flow = TriggerFlow(name=f"resume-crash-window-{request_status}")

    async def gate(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="exchange", exchange_kind="approval",
            interrupt_id="approval",
            resume_to="next",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        await data.async_set_state("approval", data.value, emit=False)

    flow.to(gate).to(finalize)
    execution = flow.create_execution(auto_close=False)
    await execution.async_start(None)
    saved_state = execution.save()
    request = {
        "request_id": "resume-window-1",
        "status": request_status,
        "value": {"approved": True},
        "actor": "reviewer",
        "accepted_at": 1.0,
    }
    if request_status == "dispatched":
        request["dispatched_at"] = 2.0
        request["dispatch_attempts"] = 1

    crash_state = copy.deepcopy(saved_state)
    crash_state["interrupts"]["approval"]["resume_requests"] = {"resume-window-1": request}
    crash_state["interrupts"]["approval"]["resume_request_id"] = "resume-window-1"
    crash_state["interrupts"]["approval"]["resumed_by"] = "reviewer"
    crash_state["interrupts"] = copy.deepcopy(crash_state["interrupts"])
    crash_state["resume_ledger"] = {"approval": {"resume-window-1": request}}

    inspector = flow.create_execution(auto_close=False)
    report = inspector.inspect_load(crash_state)
    assert report["ready"] is True
    assert diagnostic_code in {diagnostic["code"] for diagnostic in report["diagnostics"]}

    restored_execution = flow.create_execution(auto_close=False)
    restored_execution.load(crash_state)
    await restored_execution.async_continue_with(
        "approval",
        {"approved": True},
        resume_request_id="resume-window-1",
        actor="reviewer",
    )
    resumed_state = restored_execution.save()

    assert restored_execution.get_state("approval") == {"approved": True}
    assert (
        resumed_state["resume_ledger"]["approval"]["resume-window-1"]["status"]
        == "completed"
    )


@pytest.mark.asyncio
async def test_trigger_flow_execution_save_to_json_file_and_load_from_file(tmp_path: Path):
    flow = TriggerFlow()
    flow.to(lambda data: data.value).end()

    execution = flow.create_execution()
    execution.set_runtime_data("progress_marker", {"step": 1})
    json_path = tmp_path / "execution_state.json"

    saved_state = execution.save(json_path)
    assert json_path.exists()
    assert isinstance(saved_state, dict)

    restored_execution = flow.create_execution()
    restored_execution.load(json_path)
    assert restored_execution.get_runtime_data("progress_marker") == {"step": 1}


@pytest.mark.asyncio
async def test_trigger_flow_execution_save_to_yaml_file_and_load_from_file(tmp_path: Path):
    flow = TriggerFlow()
    flow.to(lambda data: data.value).end()

    execution = flow.create_execution()
    execution.set_runtime_data("progress_marker", {"step": 2})
    yaml_path = tmp_path / "execution_state.yaml"

    execution.save(yaml_path)
    assert yaml_path.exists()

    restored_execution = flow.create_execution()
    restored_execution.load(yaml_path)
    assert restored_execution.get_runtime_data("progress_marker") == {"step": 2}


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

    await execution.async_set_state("progress_marker", {"step": 1})
    result = await execution.async_close()

    assert result == {"progress_marker": {"step": 1}}
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


@pytest.mark.asyncio
async def test_hidden_runtime_stream_settles_and_closes_its_stage_tasks():
    flow = TriggerFlow(name="stream-hidden-close")

    async def stream_once(data: TriggerFlowRuntimeData):
        await data.async_put_into_stream(data.value)
        await data.async_stop_stream()

    flow.to(stream_once)
    execution = flow.create_execution(
        auto_close_timeout=0.0,
        resume_handle_exposed=False,
    )

    items = [
        item
        async for item in execution.get_async_runtime_stream(
            "settled",
            timeout=1,
        )
    ]

    assert items == ["settled"]
    assert execution.is_closed()
    assert execution._task_stage.adopted_tasks == ()
