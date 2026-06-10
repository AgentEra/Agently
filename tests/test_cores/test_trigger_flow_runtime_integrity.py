import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from agently import TriggerFlow, TriggerFlowRuntimeData


def _compat_result(value: Any):
    if isinstance(value, dict) and "$final_result" in value:
        return value["$final_result"]
    return value


class RuntimeIntegrityInput(BaseModel):
    text: str


class RuntimeIntegrityResult(BaseModel):
    value: str


async def _run_empty_for_each(flow: TriggerFlow):
    execution = flow.create_execution(auto_close=False)
    await execution.async_start([])
    snapshot = await execution.async_close(timeout=1)
    return _compat_result(snapshot)


@pytest.mark.asyncio
async def test_for_each_empty_sequence_completes_for_builder_and_loaded_config():
    flow = TriggerFlow(name="empty-for-each")

    async def scale(data: TriggerFlowRuntimeData):
        return data.value * 10

    flow.for_each().to(scale).end_for_each().end()

    assert await _run_empty_for_each(flow) == []

    restored = TriggerFlow()
    restored.register_chunk_handler(scale)
    restored.load_flow_config(flow.get_flow_config())

    assert await _run_empty_for_each(restored) == []


async def _run_match_without_hit(flow: TriggerFlow):
    execution = flow.create_execution(auto_close=False)
    await execution.async_start("actual")
    snapshot = await execution.async_close(timeout=1)
    return _compat_result(snapshot)


@pytest.mark.asyncio
async def test_match_without_hit_restores_layer_for_builder_and_loaded_config():
    flow = TriggerFlow(name="match-no-hit")

    def is_expected(data: TriggerFlowRuntimeData):
        return data.value == "expected"

    async def matched(data: TriggerFlowRuntimeData):
        return "matched"

    async def inspect_layers(data: TriggerFlowRuntimeData):
        return {
            "value": data.value,
            "layers": data._layer_marks.copy(),
        }

    flow.match().case(is_expected).to(matched).end_match().to(inspect_layers).end()

    assert await _run_match_without_hit(flow) == {"value": "actual", "layers": []}

    restored = TriggerFlow()
    restored.register_condition_handler(is_expected)
    restored.register_chunk_handler(matched)
    restored.register_chunk_handler(inspect_layers)
    restored.load_flow_config(flow.get_flow_config())

    assert await _run_match_without_hit(restored) == {"value": "actual", "layers": []}


@pytest.mark.asyncio
async def test_falsy_state_and_flow_data_values_can_be_deleted():
    flow = TriggerFlow(name="falsy-delete")
    execution = flow.create_execution(auto_close=False)

    await execution.async_set_state("zero", 0, emit=False)
    await execution.async_set_state("flag", False, emit=False)
    await execution.async_set_state("blank", "", emit=False)

    await execution.async_del_state("zero", emit=False)
    await execution.async_del_state("flag", emit=False)
    await execution.async_del_state("blank", emit=False)

    assert execution.get_state("zero", "missing") == "missing"
    assert execution.get_state("flag", "missing") == "missing"
    assert execution.get_state("blank", "missing") == "missing"

    await flow.async_set_flow_data("zero", 0, emit=False, no_warning=True)
    await flow.async_set_flow_data("flag", False, emit=False, no_warning=True)
    await flow.async_set_flow_data("blank", "", emit=False, no_warning=True)

    await flow.async_del_flow_data("zero", emit=False, no_warning=True)
    await flow.async_del_flow_data("flag", emit=False, no_warning=True)
    await flow.async_del_flow_data("blank", emit=False, no_warning=True)

    assert flow.get_flow_data("zero", "missing", no_warning=True) == "missing"
    assert flow.get_flow_data("flag", "missing", no_warning=True) == "missing"
    assert flow.get_flow_data("blank", "missing", no_warning=True) == "missing"

    await execution.async_close()


def test_loaded_blueprint_and_config_preserve_contract_metadata():
    flow = TriggerFlow(name="contract-load").set_contract(
        initial_input=RuntimeIntegrityInput,
        result=RuntimeIntegrityResult,
        meta={"area": "runtime-integrity"},
    )

    blueprint_loaded = TriggerFlow().load_blueprint(flow.save_blueprint())
    config_loaded = TriggerFlow().load_flow_config(flow.get_flow_config())
    json_loaded = TriggerFlow().load_json_flow(flow.get_json_flow())
    yaml_loaded = TriggerFlow().load_yaml_flow(flow.get_yaml_flow())

    for restored in (blueprint_loaded, config_loaded, json_loaded, yaml_loaded):
        metadata = restored.get_contract_metadata()
        initial_input = metadata.get("initial_input")
        result = metadata.get("result")
        assert initial_input is not None
        assert result is not None
        assert initial_input["label"] == "RuntimeIntegrityInput"
        assert result["label"] == "RuntimeIntegrityResult"
        assert metadata.get("meta") == {"area": "runtime-integrity"}


@pytest.mark.asyncio
async def test_async_close_concurrent_callers_close_once(monkeypatch):
    flow = TriggerFlow(name="close-once")
    execution = flow.create_execution(auto_close=False)
    seal_calls = 0
    original_seal = execution.async_seal
    original_interrupt_handler = execution._handle_pending_interrupts_before_close

    async def slow_interrupt_handler(*args, **kwargs):
        await asyncio.sleep(0)
        return await original_interrupt_handler(*args, **kwargs)

    async def counted_seal(*args, **kwargs):
        nonlocal seal_calls
        seal_calls += 1
        await asyncio.sleep(0)
        return await original_seal(*args, **kwargs)

    monkeypatch.setattr(execution, "_handle_pending_interrupts_before_close", slow_interrupt_handler)
    monkeypatch.setattr(execution, "async_seal", counted_seal)

    result_1, result_2 = await asyncio.gather(
        execution.async_close(),
        execution.async_close(),
    )

    assert result_1 == result_2
    assert seal_calls == 1


@pytest.mark.asyncio
async def test_closed_execution_is_removed_from_flow_registry():
    flow = TriggerFlow(name="execution-registry-cleanup")
    execution = flow.create_execution(auto_close=False)

    assert execution.id in flow._executions

    await execution.async_close()

    assert execution.id not in flow._executions


async def _run_concurrent_batch(flow: TriggerFlow):
    execution = flow.create_execution(auto_close=False)
    await asyncio.gather(
        execution.async_emit("Run", 1),
        execution.async_emit("Run", 2),
    )
    await execution.async_close()
    return sorted(execution.get_state("batch_results", []), key=lambda item: item["left"])


@pytest.mark.asyncio
async def test_batch_parallel_invocations_use_isolated_runtime_scopes_for_builder_and_loaded_config():
    flow = TriggerFlow(name="batch-scope")

    async def left(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0)
        return f"L{data.value}"

    async def right(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.02 if data.value == 1 else 0.04)
        return f"R{data.value}"

    async def record(data: TriggerFlowRuntimeData):
        results = list(data.get_state("batch_results", []) or [])
        results.append(data.value)
        await data.async_set_state("batch_results", results, emit=False)

    flow.when("Run").batch(left, right).to(record)

    assert await _run_concurrent_batch(flow) == [
        {"left": "L1", "right": "R1"},
        {"left": "L2", "right": "R2"},
    ]

    restored = TriggerFlow()
    restored.register_chunk_handler(left)
    restored.register_chunk_handler(right)
    restored.register_chunk_handler(record)
    restored.load_flow_config(flow.get_flow_config())

    assert await _run_concurrent_batch(restored) == [
        {"left": "L1", "right": "R1"},
        {"left": "L2", "right": "R2"},
    ]


@pytest.mark.asyncio
async def test_runtime_data_emit_inherits_current_layer_scope_for_when_join():
    flow = TriggerFlow(name="runtime-emit-layer-scope")

    async def fan_out(data: TriggerFlowRuntimeData):
        await data.async_emit("A", f"A{data.value}")
        await asyncio.sleep(0)
        await data.async_emit("B", f"B{data.value}")

    async def joined(data: TriggerFlowRuntimeData):
        results = list(data.get_state("joined", []) or [])
        results.append(data.value)
        await data.async_set_state("joined", results, emit=False)

    flow.when("Run").batch(("fan_out", fan_out)).when({"event": ["A", "B"]}, mode="and").to(joined)

    execution = flow.create_execution(auto_close=False)
    await asyncio.gather(
        execution.async_emit("Run", 1),
        execution.async_emit("Run", 2),
    )
    await execution.async_close()

    assert sorted(execution.get_state("joined", []), key=lambda item: item["event"]["A"]) == [
        {"event": {"A": "A1", "B": "B1"}},
        {"event": {"A": "A2", "B": "B2"}},
    ]


@pytest.mark.asyncio
async def test_chunk_emitted_event_list_join_uses_signal_scope_for_builder_and_loaded_config():
    async def fan_out(data: TriggerFlowRuntimeData):
        await data.async_emit("A", f"A{data.value}")
        await asyncio.sleep(0.02 if data.value == 1 else 0)
        await data.async_emit("B", f"B{data.value}")

    async def joined(data: TriggerFlowRuntimeData):
        results = list(data.get_state("joined", []) or [])
        results.append(data.value)
        await data.async_set_state("joined", results, emit=False)

    async def run(flow: TriggerFlow):
        execution = flow.create_execution(auto_close=False)
        await asyncio.gather(
            execution.async_emit("Run", 1),
            execution.async_emit("Run", 2),
        )
        await execution.async_close()
        return sorted(execution.get_state("joined", []), key=lambda item: item["event"]["A"])

    flow = TriggerFlow(name="signal-scope-event-list-join")
    flow.when("Run").to(fan_out)
    flow.when(["A", "B"], mode="and").to(joined)

    expected = [
        {"event": {"A": "A1", "B": "B1"}},
        {"event": {"A": "A2", "B": "B2"}},
    ]
    assert await run(flow) == expected

    restored = TriggerFlow()
    restored.register_chunk_handler(fan_out)
    restored.register_chunk_handler(joined)
    restored.load_flow_config(flow.get_flow_config())

    assert await run(restored) == expected


@pytest.mark.asyncio
async def test_async_close_clears_transient_aggregation_state():
    flow = TriggerFlow(name="close-scratch-cleanup")
    flow.when(["A", "B"], mode="and").to(lambda data: data.value)
    execution = flow.create_execution(auto_close=False)

    await execution.async_emit("A", "partial")
    assert execution._system_runtime_data.get("when_states", {}, inherit=False)

    await execution.async_close()

    assert [
        key
        for key in execution._system_runtime_data.keys()
        if str(key)
        in {
            "when_states",
            "batch_states",
            "collect_states",
            "for_each_results",
            "match_results",
            "batch_semaphores",
            "batch_fanout_semaphores",
            "for_each_semaphores",
        }
    ] == []


@pytest.mark.asyncio
async def test_pause_resume_to_self_has_default_replay_guard():
    flow = TriggerFlow(name="self-resume-guard")

    async def always_pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            interrupt_id="approval",
            resume_to="self",
        )

    flow.to(always_pause)
    execution = flow.create_execution(auto_close=False)

    await execution.async_start(None)
    assert "approval" in execution.get_pending_interrupts()

    with pytest.raises(RuntimeError, match="self resume limit"):
        await execution.async_continue_with("approval", {"approved": True})

    assert execution.get_pending_interrupts() == {}


@pytest.mark.asyncio
async def test_set_concurrency_limits_nested_dispatch_without_deadlocking_chain():
    flow = TriggerFlow(name="global-concurrency")
    active = 0
    max_active = 0

    async def tracked_step(data: TriggerFlowRuntimeData):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return data.value + 1

    async def final_step(data: TriggerFlowRuntimeData):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return data.value

    flow.to(tracked_step).to(final_step).end()
    execution = flow.create_execution(auto_close_timeout=0.0)
    execution.set_concurrency(1)

    result = await asyncio.wait_for(execution.async_start(1), timeout=1)

    assert _compat_result(result) == 2
    assert max_active == 1
