"""Protocol tests; planner responses are synthetic, not model-quality evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agently import Agently
from agently.core import AgentTask
from agently.types.data import ContextOmission


@pytest.fixture
def planning_task(tmp_path, monkeypatch):
    agent = Agently.create_agent("board-context-handoff").use_task_workspace(tmp_path)
    task = AgentTask(
        agent,
        goal="Use the supplied facts",
        success_criteria=["Plan grounded work"],
        execution="taskboard",
    )
    task.task_context.put(role="information", content="Supplied fact", entry_id="fact", required=True)
    captured = {"calls": 0, "error": None}

    class Request:
        id = "synthetic-board-planner"

        def input(self, value):
            captured["input"] = value
            return self

        def attachment(self, value):
            captured["attachment"] = value
            return self

        def instruct(self, value):
            return self

        def output(self, value, **kwargs):
            return self

        def get_result(self):
            return self

        async def async_get_data(self):
            captured["calls"] += 1
            if captured["error"]:
                raise captured["error"]
            return {
                "board_goal": task.goal,
                "cards": [
                    {
                        "id": "work",
                        "objective": "Use facts",
                        "depends_on": [],
                        "done_when": "Grounded result",
                        "allowed_execution_shape": "direct",
                    }
                ],
                "completion_gate": "Grounded result",
            }

    monkeypatch.setattr(agent, "create_temp_request", Request)
    monkeypatch.setattr(task, "_apply_language_policy_to_request", lambda *args: None)
    monkeypatch.setattr(task, "_taskboard_should_fallback_to_flat", lambda revision: False)
    return task, captured


@pytest.mark.asyncio
async def test_taskboard_preparation_is_consumed_by_initial_planner(planning_task):
    task, captured = planning_task
    frame = await task._taskboard_context_prepare_stage({"iteration": 1})
    prepared_id = frame["context_pack"]["package_id"]
    assert task.context_consumptions == []
    await task._taskboard_work_plan_stage(frame)
    assert len(task.context_packages) == 1
    assert captured["input"]["context_pack"]["package_id"] == prepared_id
    assert len(task.context_consumptions) == 1
    consumption = task.context_consumptions[0]
    assert consumption.package_id == prepared_id
    assert consumption.request_id == "synthetic-board-planner"
    assert consumption.consumer_id == f"agent_task:{task.id}:taskboard-planner"
    assert consumption.block_ids == tuple(b.block_id for b in task.context_packages[0].blocks)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["entry", "source", "refreshed_reader", "foreign_consumer"])
async def test_planner_rereads_changed_context_and_updates_card_orientation(planning_task, change, tmp_path):
    task, captured = planning_task
    # AgentTask already binds its TaskWorkspace; changing a file changes that source revision.
    frame = await task._taskboard_context_prepare_stage({"iteration": 1})
    prepared = frame["planning_context_package"]
    if change == "source":
        (tmp_path / "new.txt").write_text("new source fact", encoding="utf-8")
    elif change == "foreign_consumer":
        frame["planning_context_package"] = replace(prepared, consumer_id="another-consumer")
    else:
        task.task_context.remove("fact")
        task.task_context.put(role="information", content="Updated fact", entry_id="fact", required=True)
        if change == "refreshed_reader":
            task._task_context_reader(phase="planning", consumer_id=prepared.consumer_id).refresh()
    await task._taskboard_work_plan_stage(frame)
    assert len(task.context_packages) == 2
    consumed = task.context_packages[-1]
    assert consumed.package_id != prepared.package_id
    assert captured["input"]["context_pack"]["package_id"] == consumed.package_id
    assert frame["context_pack"]["package_id"] == consumed.package_id
    assert task.context_consumptions[0].package_id == consumed.package_id
    if change in {"entry", "refreshed_reader"}:
        assert "Updated fact" in [b.content for b in consumed.blocks]
        assert "Supplied fact" not in [b.content for b in consumed.blocks]


@pytest.mark.asyncio
async def test_reused_package_preserves_rich_attachment(planning_task):
    task, captured = planning_task
    attachment = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    task.task_context.put(
        role="information",
        content=[attachment],
        entry_id="image",
        required=True,
        metadata={"context_representation": "image_attachment"},
    )
    frame = await task._taskboard_context_prepare_stage({"iteration": 1})
    await task._taskboard_work_plan_stage(frame)
    assert len(task.context_packages) == 1
    assert captured["attachment"][1] == attachment
    assert "data:image/png" not in str(captured["input"]["context_pack"])


@pytest.mark.asyncio
async def test_failed_planner_does_not_consume_prepared_context(planning_task):
    task, captured = planning_task
    frame = await task._taskboard_context_prepare_stage({"iteration": 1})
    captured["error"] = RuntimeError("provider failed")
    with pytest.raises(RuntimeError, match="provider failed"):
        await task._taskboard_work_plan_stage(frame)
    assert len(task.context_packages) == 1
    assert task.context_consumptions == []


@pytest.mark.asyncio
async def test_required_delivery_is_checked_before_reused_package_dispatch(planning_task):
    task, captured = planning_task
    frame = await task._taskboard_context_prepare_stage({"iteration": 1})
    frame["planning_context_package"] = replace(
        frame["planning_context_package"],
        omissions=(ContextOmission(block_key="required", source_ref="missing", reason="failed", required=True),),
    )
    with pytest.raises(RuntimeError, match="Required TaskContext"):
        await task._taskboard_work_plan_stage(frame)
    assert captured["calls"] == 0
    assert task.context_consumptions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", ["shape", "resume"])
async def test_reusing_existing_plan_does_not_record_planner_consumption(planning_task, monkeypatch, reuse):
    task, captured = planning_task
    # Build the fixture revision through the existing planner coercion once.
    result = await task._request_taskboard_plan({})
    captured["calls"] = 0
    task.context_consumptions.clear()
    if reuse == "shape":
        monkeypatch.setattr(task, "_initial_taskboard_plan_from_shape_analysis", lambda: result)
        frame = {"iteration": 1}
    else:
        frame = {"iteration": 1, "taskboard_revision": result.revision.to_dict()}
    frame = await task._taskboard_context_prepare_stage(frame)
    await task._taskboard_work_plan_stage(frame)
    assert captured["calls"] == 0
    assert task.context_consumptions == []
    assert frame["context_pack"]["items"]
