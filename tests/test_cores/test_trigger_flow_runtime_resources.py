import copy
import json
from typing import Any, Callable, cast

import pytest

from agently import TriggerFlow, TriggerFlowEventData, TriggerFlowRuntimeData
from agently.base import execution_environment
from agently.types.trigger_flow import TRIGGER_FLOW_CHECKPOINT_KIND


def test_trigger_flow_runtime_data_alias_is_exported():
    assert TriggerFlowRuntimeData is TriggerFlowEventData


@pytest.mark.asyncio
async def test_trigger_flow_runtime_data_namespaces_and_flow_resources():
    flow = TriggerFlow()
    flow.update_runtime_resources(logger="flow-logger")

    async def prepare(data: TriggerFlowRuntimeData):
        data.state.set("draft", {"topic": data.value})
        data.flow_state.set("shared_flag", True)
        data.state.set("shared_flag", data.flow_state.get("shared_flag"))
        data.state.set("logger", data.require_resource("logger"))

    flow.to(prepare)
    result = await flow.async_start("pricing")

    assert result == {
        "draft": {"topic": "pricing"},
        "shared_flag": True,
        "logger": "flow-logger",
    }


@pytest.mark.asyncio
async def test_trigger_flow_execution_resources_override_flow_defaults():
    flow = TriggerFlow()
    flow.update_runtime_resources(tool_name="flow-tool")

    async def inspect_resource(data: TriggerFlowRuntimeData):
        data.state.set("tool_name", data.require_resource("tool_name"))

    flow.to(inspect_resource)

    assert await flow.async_start("start") == {"tool_name": "flow-tool"}
    assert await flow.async_start("start", runtime_resources={"tool_name": "execution-tool"}) == {
        "tool_name": "execution-tool"
    }


@pytest.mark.asyncio
async def test_trigger_flow_runtime_data_set_resource_is_execution_scoped():
    flow = TriggerFlow()

    async def remember(data: TriggerFlowRuntimeData):
        data.set_resource("token", data.value)
        return data.value

    async def read_token(data: TriggerFlowRuntimeData):
        return data.require_resource("token")

    flow.to(remember).to(read_token).end()

    execution = flow.create_execution()
    await execution.async_start("alpha", wait_for_result=False)
    result = await execution.async_get_result(timeout=1)

    assert result == "alpha"

    another_execution = flow.create_execution()
    with pytest.raises(KeyError, match="missing required runtime resource"):
        another_execution.require_runtime_resource("token")


@pytest.mark.asyncio
async def test_trigger_flow_execution_save_records_resource_keys_only():
    flow = TriggerFlow()
    flow.update_runtime_resources(flow_tool=object())
    flow.declare_resource_requirement("flow_tool", metadata={"declared": True})

    async def passthrough(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(passthrough).end()
    execution = flow.create_execution(runtime_resources={"execution_logger": object()})
    await execution.async_start("ok", wait_for_result=False)
    state = execution.save()

    assert sorted(state["resource_keys"]) == ["execution_logger", "flow_tool"]
    assert state["checkpoint"]["kind"] == TRIGGER_FLOW_CHECKPOINT_KIND
    requirements = {
        requirement["key"]: requirement
        for requirement in state["checkpoint"]["resource_requirements"]
        if requirement["kind"] == "runtime_resource"
    }
    assert requirements["execution_logger"]["source"] == "execution"
    assert requirements["execution_logger"]["metadata"]["scope"] == "execution"
    assert requirements["flow_tool"]["source"] == "flow"
    assert requirements["flow_tool"]["metadata"]["scope"] == "flow"
    assert requirements["flow_tool"]["metadata"]["declared"] is True
    assert "runtime_resources" not in state
    json.dumps(state)


@pytest.mark.asyncio
async def test_trigger_flow_execution_load_requires_reinjecting_runtime_resources():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "continue?"},
            resume_event="Resume",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        service = cast(Callable[[Any], Any], data.require_resource("resume_service"))
        return service(data.value)

    flow.declare_resource_requirement("resume_service")
    flow.to(ask_feedback)
    flow.when("Resume").to(finalize).end()

    execution = await flow.async_start_execution("topic", wait_for_result=False)
    interrupt_id = next(iter(execution.get_pending_interrupts()))
    saved_state = execution.save()

    missing_resource_execution = flow.create_execution()
    report = missing_resource_execution.inspect_rehydration(saved_state)
    assert report["ready"] is False
    assert report["missing_resource_keys"] == ["resume_service"]

    with pytest.raises(RuntimeError, match="missing resources"):
        flow.create_execution().load(saved_state, validate_rehydration=True)

    legacy_state = copy.deepcopy(saved_state)
    legacy_state["checkpoint"].pop("resource_requirements", None)
    legacy_state["resource_keys"] = ["resume_service"]
    legacy_report = flow.create_execution().inspect_rehydration(legacy_state)
    assert legacy_report["ready"] is False
    assert legacy_report["missing_resource_keys"] == ["resume_service"]

    missing_resource_execution = flow.create_execution()
    missing_resource_execution.load(saved_state)
    with pytest.raises(KeyError, match="missing required runtime resource"):
        await missing_resource_execution.async_continue_with(interrupt_id, {"approved": True})

    restored_execution = flow.create_execution()
    rehydration = await restored_execution.async_rehydrate(
        saved_state,
        runtime_resources={
            "resume_service": lambda payload: {
                "approved": payload["approved"],
                "source": "resource",
            }
        },
    )
    assert rehydration["ready"] is True
    await restored_execution.async_continue_with(interrupt_id, {"approved": True})
    result = await restored_execution.async_get_result(timeout=1)

    assert result == {
        "approved": True,
        "source": "resource",
    }


@pytest.mark.asyncio
async def test_trigger_flow_condition_handler_can_use_runtime_resources():
    flow = TriggerFlow()

    def should_take_left(data: TriggerFlowRuntimeData):
        return bool(data.require_resource("take_left"))

    async def left(data: TriggerFlowRuntimeData):
        data.state.set("branch", "left")

    async def right(data: TriggerFlowRuntimeData):
        data.state.set("branch", "right")

    flow.if_condition(should_take_left).to(left).else_condition().to(right).end_condition()

    assert await flow.async_start("x", runtime_resources={"take_left": True}) == {"branch": "left"}
    assert await flow.async_start("x", runtime_resources={"take_left": False}) == {"branch": "right"}


@pytest.mark.asyncio
async def test_trigger_flow_config_round_trip_with_runtime_resources():
    flow = TriggerFlow(name="runtime-resources-config")

    async def render(data: TriggerFlowRuntimeData):
        renderer = cast(Callable[[Any], Any], data.require_resource("renderer"))
        data.state.set("rendered", renderer(data.value))

    flow.to(render)
    config = flow.get_flow_config()

    assert "renderer" not in json.dumps(config)

    restored = TriggerFlow()
    restored.register_chunk_handler(render)
    restored.load_flow_config(config)

    result = await restored.async_start(
        "hello",
        runtime_resources={"renderer": lambda value: value.upper()},
    )
    assert result == {"rendered": "HELLO"}


@pytest.mark.asyncio
async def test_trigger_flow_execution_environment_injects_managed_resource():
    flow = TriggerFlow()

    async def calculate(data: TriggerFlowRuntimeData):
        sandbox = data.require_resource("managed_python")
        assert sandbox is not None
        data.state.set("calculated", sandbox.run("result = value + 1")["result"])

    flow.to(calculate)

    result = await flow.async_start(
        41,
        execution_environments=[
            {
                "kind": "python",
                "scope": "execution",
                "resource_key": "managed_python",
                "config": {"base_vars": {"value": 41}},
            }
        ],
    )

    assert result == {"calculated": 42}
    assert execution_environment.list(scope="execution") == []


@pytest.mark.asyncio
async def test_trigger_flow_save_records_managed_execution_environment_keys():
    flow = TriggerFlow()

    async def hold_resource(data: TriggerFlowRuntimeData):
        data.state.set("has_resource", data.require_resource("managed_python") is not None)

    flow.to(hold_resource).end()

    execution = flow.create_execution(
        auto_close=False,
        execution_environments=[
            {
                "requirement_id": "managed-python-save-test",
                "kind": "python",
                "scope": "execution",
                "resource_key": "managed_python",
            }
        ],
    )
    await execution.async_start("start", wait_for_result=False)
    state = execution.save()

    assert "managed_python" in state["managed_resource_keys"]
    assert "managed-python-save-test" in state["execution_environment_requirement_ids"]
    requirements = state["checkpoint"]["resource_requirements"]
    environment_requirements = [
        requirement
        for requirement in requirements
        if requirement["kind"] == "execution_environment_requirement"
    ]
    assert environment_requirements[0]["metadata"]["resource_key"] == "managed_python"
    assert environment_requirements[0]["metadata"]["requirement"]["requirement_id"] == "managed-python-save-test"

    await execution.async_close()
    assert execution_environment.list(scope="execution") == []


@pytest.mark.asyncio
async def test_trigger_flow_async_rehydrate_restores_managed_execution_environment():
    flow = TriggerFlow()

    async def pause(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(type="approval", interrupt_id="approval", resume_to="next")

    async def use_environment(data: TriggerFlowRuntimeData):
        sandbox = data.require_resource("managed_python")
        assert sandbox is not None
        data.state.set("answer", sandbox.run("result = value + 1")["result"])

    flow.to(pause).to(use_environment)
    execution = flow.create_execution(
        auto_close=False,
        execution_environments=[
            {
                "requirement_id": "managed-python-rehydrate-test",
                "kind": "python",
                "scope": "execution",
                "resource_key": "managed_python",
                "config": {"base_vars": {"value": 41}},
            }
        ],
    )
    await execution.async_start("start")
    saved_state = execution.save()
    await execution.async_close(pending_interrupts="cancel")

    restored = flow.create_execution(auto_close=False)
    rehydration = await restored.async_rehydrate(saved_state)
    assert rehydration["ready"] is True
    assert rehydration["pending_environment_resource_keys"] == []

    await restored.async_continue_with("approval", {"approved": True})
    snapshot = await restored.async_close()

    assert snapshot["answer"] == 42
    assert execution_environment.list(scope="execution") == []


@pytest.mark.asyncio
async def test_trigger_flow_execution_async_save_checkpoint_uses_checkpoint_store():
    class Store:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        async def put_checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
            self.calls.append({"run_id": run_id, "state": state, "step_id": step_id})
            return {"id": "checkpoint-1", "run_id": run_id, "step_id": step_id}

    flow = TriggerFlow(name="checkpoint-store")
    execution = flow.create_execution()
    execution.set_state("value", 1)
    store = Store()

    ref = await execution.async_save_checkpoint(store)

    assert ref == {
        "id": "checkpoint-1",
        "run_id": execution.run_context.run_id,
        "step_id": f"state:{ execution._state_version }",
    }
    assert store.calls[0]["state"]["checkpoint"]["kind"] == TRIGGER_FLOW_CHECKPOINT_KIND
    assert store.calls[0]["state"]["checkpoint"]["execution_id"] == execution.id
