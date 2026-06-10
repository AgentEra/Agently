import asyncio

import pytest
from pydantic import TypeAdapter

from agently import TriggerFlow, TriggerFlowInterruptEvent, TriggerFlowRuntimeData


@pytest.mark.asyncio
async def test_trigger_flow_pause_continue_with_saved_interrupt():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        data.set_runtime_data("draft", {"topic": data.value})
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_event="UserFeedback",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        return {
            "draft": data.get_runtime_data("draft"),
            "feedback": data.value,
        }

    flow.to(ask_feedback)
    flow.when("UserFeedback").to(finalize).end()

    execution = await flow.async_start_execution("pricing")
    pending_interrupts = execution.get_pending_interrupts()

    assert execution.get_status() == "waiting"
    assert len(pending_interrupts) == 1

    interrupt_id, interrupt = next(iter(pending_interrupts.items()))
    assert interrupt["resume_event"] == "UserFeedback"

    restored_execution = flow.create_execution()
    restored_execution.load(execution.save())

    assert restored_execution.get_status() == "waiting"
    assert interrupt_id in restored_execution.get_pending_interrupts()

    await restored_execution.async_continue_with(interrupt_id, {"approved": True})
    result = await restored_execution.async_get_result(timeout=1)
    close_snapshot = await restored_execution.async_close()

    assert close_snapshot["$final_result"] == result
    assert restored_execution.get_status() == "completed"
    assert result == {
        "draft": {"topic": "pricing"},
        "feedback": {"approved": True},
    }


@pytest.mark.asyncio
async def test_trigger_flow_pause_resume_to_next_continues_downstream_after_load():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        await data.async_set_state("draft", {"topic": data.value})
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            interrupt_id="approval",
            resume_to="next",
        )

    async def finalize(data: TriggerFlowRuntimeData):
        return {
            "draft": data.get_state("draft"),
            "feedback": data.value,
        }

    flow.to(ask_feedback).to(finalize).end()

    execution = await flow.async_start_execution("pricing", wait_for_result=False)
    saved_state = execution.save()
    restored = flow.create_execution(auto_close=False)
    restored.load(saved_state)

    await restored.async_continue_with("approval", {"approved": True})
    result = await restored.async_get_result(timeout=1)

    assert result == {
        "draft": {"topic": "pricing"},
        "feedback": {"approved": True},
    }


@pytest.mark.asyncio
async def test_trigger_flow_pause_resume_to_self_exposes_resume_context():
    flow = TriggerFlow()

    async def gate(data: TriggerFlowRuntimeData):
        if data.is_resume:
            assert data.resume.origin_signal is not None
            await data.async_set_state(
                "resume",
                {
                    "interrupt_id": data.resume.interrupt_id,
                    "value": data.resume.value,
                    "origin_event": data.resume.origin_signal["trigger_event"],
                    "input": data.value,
                },
            )
            return "approved"
        return await data.async_pause_for(
            type="approval",
            payload={"value": data.value},
            interrupt_id="gate",
            resume_to="self",
        )

    flow.to(gate).end()
    execution = await flow.async_start_execution("document", wait_for_result=False)

    await execution.async_continue_with("gate", {"approved": True})
    snapshot = await execution.async_close()

    assert snapshot["resume"] == {
        "interrupt_id": "gate",
        "value": {"approved": True},
        "origin_event": "START",
        "input": "document",
    }
    assert snapshot["$final_result"] == "approved"


@pytest.mark.asyncio
async def test_trigger_flow_hidden_start_rejects_pause_without_resume_handle():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_to="next",
        )

    flow.to(ask_feedback)

    with pytest.raises(RuntimeError, match="requires an exposed execution handle"):
        await flow.async_start("pricing")


def test_trigger_flow_hidden_runtime_stream_rejects_pause_without_resume_handle():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_to="next",
        )

    flow.to(ask_feedback)

    with pytest.raises(RuntimeError, match="requires an exposed execution handle"):
        list(flow.get_runtime_stream("pricing", timeout=1))


@pytest.mark.asyncio
async def test_trigger_flow_close_rejects_pending_interrupt_by_default_and_can_cancel():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="approval",
            payload={"question": "approve?"},
            interrupt_id="approval",
            resume_to="next",
        )

    flow.to(ask_feedback)

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("pricing", wait_for_result=False)

    with pytest.raises(RuntimeError, match="pending interrupts are waiting"):
        await execution.async_close()

    assert execution.get_status() == "waiting"
    assert "approval" in execution.get_pending_interrupts()

    snapshot = await execution.async_close(pending_interrupts="cancel")

    assert execution.get_status() == "cancelled"
    assert execution.get_pending_interrupts() == {}
    cancelled_interrupt = execution.get_interrupt("approval")
    assert isinstance(cancelled_interrupt, dict)
    assert cancelled_interrupt["status"] == "cancelled"
    assert isinstance(snapshot, dict)


def test_trigger_flow_public_interrupt_event_type_matches_runtime_stream_shape():
    flow = TriggerFlow()

    async def ask_feedback(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            type="human_input",
            payload={"question": "approve?"},
            resume_event="UserFeedback",
        )

    flow.to(ask_feedback)

    execution = flow.create_execution(auto_close=False)
    interrupt_event = next(execution.get_runtime_stream("pricing", timeout=1))
    validated_event = TypeAdapter(TriggerFlowInterruptEvent).validate_python(interrupt_event)

    assert validated_event["type"] == "interrupt"
    assert validated_event["action"] == "pause"
    assert validated_event["execution_id"]
    assert validated_event["interrupt"]["type"] == "human_input"
    assert validated_event["interrupt"].get("resume_event") == "UserFeedback"
    assert validated_event["interrupt"].get("source_execution_id") == validated_event["execution_id"]
    assert validated_event["interrupt"].get("source_operator_id")
    assert validated_event["interrupt"].get("source_signal") == validated_event.get("signal")
    assert "continuation_event" in validated_event["interrupt"]


@pytest.mark.asyncio
async def test_trigger_flow_when_and_state_is_isolated_per_execution():
    flow = TriggerFlow()
    flow.when({"event": ["A", "B"]}, mode="and").to(lambda data: data.value).end()

    execution_1 = flow.create_execution()
    execution_2 = flow.create_execution()

    await execution_1.async_emit("A", "execution-1-a")
    await execution_2.async_emit("B", "execution-2-b")

    with pytest.warns(UserWarning):
        assert await execution_1.async_get_result(timeout=0.01) is None
    with pytest.warns(UserWarning):
        assert await execution_2.async_get_result(timeout=0.01) is None

    await execution_1.async_emit("B", "execution-1-b")
    await execution_2.async_emit("A", "execution-2-a")

    result_1 = await execution_1.async_get_result(timeout=1)
    result_2 = await execution_2.async_get_result(timeout=1)

    assert result_1 == {
        "event": {
            "A": "execution-1-a",
            "B": "execution-1-b",
        }
    }
    assert result_2 == {
        "event": {
            "A": "execution-2-a",
            "B": "execution-2-b",
        }
    }


@pytest.mark.asyncio
async def test_trigger_flow_batch_state_is_isolated_per_execution():
    flow = TriggerFlow()

    async def left(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.01)
        return {"left": data.value}

    async def right(data: TriggerFlowRuntimeData):
        await asyncio.sleep(0.01)
        return {"right": data.value}

    flow.batch(left, right).to(lambda data: data.value).end()

    execution_1 = flow.create_execution()
    execution_2 = flow.create_execution()

    await asyncio.gather(
        execution_1.async_start("first"),
        execution_2.async_start("second"),
    )

    result_1 = await execution_1.async_get_result(timeout=1)
    result_2 = await execution_2.async_get_result(timeout=1)

    assert result_1 == {
        "left": {"left": "first"},
        "right": {"right": "first"},
    }
    assert result_2 == {
        "left": {"left": "second"},
        "right": {"right": "second"},
    }
