import asyncio

import pytest
from pydantic import TypeAdapter

from agently import TriggerFlow, TriggerFlowInterventionEvent, TriggerFlowRuntimeData


@pytest.mark.asyncio
async def test_trigger_flow_intervention_disabled_mode_rejects_without_state_change():
    flow = TriggerFlow()
    execution = flow.create_execution(auto_close=False)

    with pytest.raises(RuntimeError, match="runtime intervention is disabled"):
        await execution.async_intervene({"text": "late note"})

    assert execution.get_interventions() == []
    assert "$interventions" not in execution.save()["runtime_data"]


@pytest.mark.asyncio
async def test_trigger_flow_explicit_none_intervention_mode_disables_planned_inference():
    flow = TriggerFlow()
    flow.intervention_point(name="before_start")
    execution = flow.create_execution(auto_close=False, intervention_mode=None)

    with pytest.raises(RuntimeError, match="runtime intervention is disabled"):
        await execution.async_intervene({"text": "late note"})


@pytest.mark.asyncio
async def test_trigger_flow_planned_intervention_point_passes_through_without_pending_item():
    flow = TriggerFlow()

    async def first(data: TriggerFlowRuntimeData):
        return {"value": data.value}

    async def second(data: TriggerFlowRuntimeData):
        return {
            "value": data.value,
            "interventions": data.get_interventions(),
        }

    flow.to(first).intervention_point(name="before_second", target="before_second").to(second).end()

    execution = flow.create_execution(auto_close=False, intervention_mode="planned")
    await execution.async_start("document")
    snapshot = await execution.async_close()

    assert snapshot["$final_result"] == {
        "value": {"value": "document"},
        "interventions": [],
    }


@pytest.mark.asyncio
async def test_trigger_flow_planned_intervention_point_inserts_matching_pending_once():
    flow = TriggerFlow()
    release = asyncio.Event()

    async def extract(data: TriggerFlowRuntimeData):
        await release.wait()
        return {"terms": data.value}

    async def assess(data: TriggerFlowRuntimeData):
        interventions = data.get_interventions(status="inserted", target="before_assess")
        for intervention in interventions:
            await data.async_mark_intervention_consumed(
                intervention["id"],
                status="applied",
            )
        return {
            "terms": data.value["terms"],
            "guidance": [item["payload"] for item in interventions],
        }

    flow.to(extract).intervention_point(name="before_assess", target="before_assess").to(assess).end()
    execution = flow.create_execution(auto_close=False)

    start_task = asyncio.create_task(execution.async_start("contract"))
    await asyncio.sleep(0)
    intervention = await execution.async_intervene(
        {"text": "Attachment A is latest."},
        author="reviewer",
        target="before_assess",
    )
    assert intervention is not None
    release.set()
    await start_task
    snapshot = await execution.async_close()

    inserted = execution.get_interventions(status="inserted", target="before_assess")
    assert len(inserted) == 1
    assert inserted[0]["id"] == intervention["id"]
    assert inserted[0]["consumers"]["assess"]["status"] == "applied"
    assert snapshot["$final_result"] == {
        "terms": "contract",
        "guidance": [{"text": "Attachment A is latest."}],
    }


def test_trigger_flow_intervention_point_named_definition_is_idempotent():
    flow = TriggerFlow()

    def step(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(step).intervention_point(name="before_step", target="before_step")
    flow.to(step).intervention_point(name="before_step", target="before_step")

    points = [
        operator
        for operator in flow.get_flow_config(validate_serializable=False)["operators"]
        if operator["kind"] == "intervention_point"
    ]
    assert len(points) == 1


def test_trigger_flow_intervention_point_same_name_different_target_fails_fast():
    flow = TriggerFlow()

    def step(data: TriggerFlowRuntimeData):
        return data.value

    flow.to(step).intervention_point(name="before_step", target="one")
    with pytest.raises(ValueError, match="already exists with a different definition"):
        flow.to(step).intervention_point(name="before_step", target="two")


@pytest.mark.asyncio
async def test_trigger_flow_auto_intervention_inserts_targeted_before_matching_chunk():
    flow = TriggerFlow()
    release = asyncio.Event()

    async def first(data: TriggerFlowRuntimeData):
        await release.wait()
        return data.value

    async def second(data: TriggerFlowRuntimeData):
        return {
            "value": data.value,
            "interventions": data.get_interventions(status="inserted", target="second"),
        }

    flow.to(("first", first)).to(("second", second)).end()
    execution = flow.create_execution(auto_close=False, intervention_mode="auto")

    start_task = asyncio.create_task(execution.async_start("draft"))
    await asyncio.sleep(0)
    await execution.async_intervene({"text": "Use the newer table."}, target="second")
    release.set()
    await start_task
    snapshot = await execution.async_close()

    assert snapshot["$final_result"]["interventions"][0]["payload"] == {"text": "Use the newer table."}


@pytest.mark.asyncio
async def test_trigger_flow_auto_intervention_inserts_untargeted_at_next_chunk_boundary():
    flow = TriggerFlow()
    release = asyncio.Event()

    async def first(data: TriggerFlowRuntimeData):
        await release.wait()
        return data.value

    async def second(data: TriggerFlowRuntimeData):
        return data.get_latest_intervention()["payload"]

    flow.to(("first", first)).to(("second", second)).end()
    execution = flow.create_execution(auto_close=False, intervention_mode="auto")

    start_task = asyncio.create_task(execution.async_start("draft"))
    await asyncio.sleep(0)
    await execution.async_intervene({"text": "General context."})
    release.set()
    await start_task
    snapshot = await execution.async_close()

    assert snapshot["$final_result"] == {"text": "General context."}


def test_trigger_flow_auto_mode_rejects_planned_intervention_points():
    flow = TriggerFlow()
    flow.intervention_point(name="before_start", target="before_start")

    with pytest.raises(ValueError, match="can not be used with explicit intervention points"):
        flow.create_execution(intervention_mode="auto")


@pytest.mark.asyncio
async def test_trigger_flow_pending_interventions_expire_on_close_and_result_surface_reads_them():
    flow = TriggerFlow()
    execution = flow.create_execution(auto_close=False, intervention_mode="planned")

    intervention = await execution.async_intervene({"text": "Too late."}, target="missing")
    assert intervention is not None
    snapshot = await execution.async_close()
    assert isinstance(snapshot, dict)

    expired = execution.result.get_interventions(status="expired")
    assert expired[0]["id"] == intervention["id"]
    assert snapshot["$interventions"][intervention["id"]]["status"] == "expired"


def test_trigger_flow_intervention_save_load_preserves_ledger_and_consumers():
    flow = TriggerFlow()
    execution = flow.create_execution(auto_close=False, intervention_mode="planned")

    intervention = execution.intervene({"text": "Persist me."}, target="review")
    assert intervention is not None
    execution.mark_intervention_consumed(
        intervention["id"],
        consumer="ui",
        status="ignored",
        note="displayed only",
    )

    restored = flow.create_execution(auto_close=False)
    restored.load(execution.save())

    assert restored.get_latest_intervention()["payload"] == {"text": "Persist me."}
    assert restored.get_latest_intervention()["consumers"]["ui"]["status"] == "ignored"
    assert restored.save()["intervention"]["mode"] == "planned"


@pytest.mark.asyncio
async def test_trigger_flow_intervention_runtime_stream_event_shape_is_fail_open():
    flow = TriggerFlow()
    execution = flow.create_execution(auto_close=False, intervention_mode="planned")
    subscription = execution._runtime_stream_transport.subscribe(
        start="earliest",
        timeout=None,
    )

    try:
        await execution.async_intervene({"text": "Stream me."})
        stream_event = await anext(subscription)
        validated = TypeAdapter(TriggerFlowInterventionEvent).validate_python(
            stream_event
        )
    finally:
        await subscription.async_close()

    assert validated["type"] == "intervention"
    assert validated["action"] == "append"
    assert validated["execution_id"] == execution.id
