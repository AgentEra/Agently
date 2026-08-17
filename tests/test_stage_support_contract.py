from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import time
from pathlib import Path
from typing import cast

import pytest
from packaging.version import Version

import agently
from agently import TriggerFlow
from agently_stage import Stage, StageHandle
from agently.core import EventCenter
from agently.types.data import RuntimeEvent
from agently.types.trigger_flow import TriggerFlowRuntimeData


ROOT = Path(__file__).resolve().parents[1]


def test_triggerflow_uses_real_stage_without_scope_adapter() -> None:
    execution = TriggerFlow(name="stage-native-task-owner").create_execution(auto_close=False)
    source = (ROOT / "agently/core/orchestration/TriggerFlow/Execution.py").read_text(encoding="utf-8")

    assert isinstance(execution._task_stage, Stage)
    assert not (ROOT / "agently/core/runtime/_task_support.py").exists()
    assert "StageManagedTaskScope" not in source
    assert "LocalTaskScope" not in source


def test_private_stage_runtime_stream_transport_exists() -> None:
    module = importlib.import_module("agently.core.orchestration.TriggerFlow._runtime_stream_transport")

    assert hasattr(module, "StageRuntimeStreamTransport")


def test_project_declares_supported_stage_dependency() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "agently-stage (>=0.3.8,<0.4.0)" in pyproject
    assert Version(importlib.metadata.version("agently-stage")) >= Version("0.3.8")
    assert not hasattr(agently, "Stage")
    assert not hasattr(agently, "Tunnel")
    assert not hasattr(agently, "LocalTaskScope")


def test_triggerflow_sync_emit_nowait_returns_loop_neutral_stage_handle() -> None:
    delivered: list[int] = []
    execution = TriggerFlow(name="stage-handle-nowait").create_execution(auto_close=False)

    async def handle(data: TriggerFlowRuntimeData) -> None:
        delivered.append(data.value)

    execution.on("probe", handle, binding_id="test.stage_handle_nowait")
    submitted = execution.emit_nowait("probe", 7)

    assert isinstance(submitted, StageHandle)
    assert submitted.result(timeout=1) == [None]
    submitted.wait_settled(timeout=1)
    assert delivered == [7]
    execution.close(timeout=1)


@pytest.mark.asyncio
async def test_triggerflow_creates_internal_managed_tasks_through_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_origins: list[str] = []
    original_create_task = Stage.create_task

    def record_create_task(
        self: Stage,
        coroutine,
        *,
        origin: str,
        name: str | None = None,
    ):
        created_origins.append(origin)
        return original_create_task(self, coroutine, origin=origin, name=name)

    monkeypatch.setattr(Stage, "create_task", record_create_task)
    execution = TriggerFlow(name="stage-native-create-task").create_execution(auto_close=False)
    delivered: list[int] = []

    async def handler(data: TriggerFlowRuntimeData) -> None:
        delivered.append(data.value)

    execution.on("probe", handler, binding_id="stage-native.handler")
    task = await execution.async_emit_nowait("probe", 7)
    assert task is not None
    assert await task == [None]
    await execution.async_close()

    assert delivered == [7]
    assert "emit_nowait:event:probe" in created_origins
    assert "handler:stage-native.handler" in created_origins


@pytest.mark.asyncio
async def test_triggerflow_adopts_only_preexisting_external_task() -> None:
    execution = TriggerFlow(name="stage-native-adopt").create_execution(auto_close=False)
    task = asyncio.create_task(asyncio.sleep(0, result="done"))

    assert execution._track_task(task, origin="external:preexisting") is task
    assert execution._task_stage.origin_for_adopted(task) == "external:preexisting"
    assert await task == "done"
    await execution.async_close()


@pytest.mark.asyncio
async def test_event_center_retains_native_task_support_after_stage_rejection() -> None:
    center = EventCenter()
    completed = asyncio.Event()

    async def hook(_event: object) -> None:
        completed.set()

    center.register_hook(
        hook,
        hook_name="stage-backed",
        delivery_policy={"dispatch": "background"},
    )
    await center.async_emit({"event_type": "runtime.info", "message": "test"})
    await asyncio.wait_for(completed.wait(), timeout=1)
    await center.async_flush()

    assert not hasattr(center, "_background_task_scope")
    assert center._background_tasks == set()
    assert "agently_stage" not in type(center).__module__


def test_triggerflow_execution_uses_private_stage_stream_transport() -> None:
    module = importlib.import_module("agently.core.orchestration.TriggerFlow._runtime_stream_transport")
    execution = TriggerFlow(name="stage-stream-transport").create_execution(auto_close=False)

    assert isinstance(
        execution._runtime_stream_transport,
        module.StageRuntimeStreamTransport,
    )


def test_event_center_remains_reusable_across_sequential_loops() -> None:
    center = EventCenter(idle_flush_seconds=None)
    delivered: list[int] = []

    async def hook(event: RuntimeEvent) -> None:
        delivered.append(event.payload["index"])

    center.register_hook(
        hook,
        hook_name="cross-loop",
        delivery_policy={"dispatch": "background"},
    )

    async def emit(index: int) -> None:
        await center.async_emit(
            {
                "event_type": "runtime.info",
                "payload": {"index": index},
            }
        )
        await center.async_flush()

    asyncio.run(emit(1))
    asyncio.run(emit(2))

    assert delivered == [1, 2]


@pytest.mark.asyncio
async def test_triggerflow_stage_stream_replays_exact_items_to_concurrent_readers() -> None:
    flow = TriggerFlow(name="stage-stream-concurrent-readers")

    async def publish(data: TriggerFlowRuntimeData) -> None:
        await data.async_put("first")
        await asyncio.sleep(0)
        await data.async_put("second")

    flow.to(publish)
    execution = flow.create_execution(auto_close=True, auto_close_timeout=0)

    async def collect() -> list[str]:
        return [
            cast(str, item)
            async for item in execution.get_async_runtime_stream(
                "start",
                timeout=1,
            )
        ]

    first, second = await asyncio.gather(collect(), collect())

    assert first == ["first", "second"]
    assert second == ["first", "second"]


@pytest.mark.asyncio
async def test_triggerflow_stage_stream_keeps_public_state_stage_free() -> None:
    flow = TriggerFlow(name="stage-stream-no-public-leak")
    execution = flow.create_execution(auto_close=False)

    await execution.async_put_into_stream({"visible": "item"})
    await execution.async_stop_stream()
    items = [item async for item in execution.get_async_runtime_stream(timeout=1)]
    snapshot = execution.save()

    assert items == [{"visible": "item"}]
    serialized = json.dumps(snapshot, default=str)
    assert "agently_stage" not in serialized
    assert "LocalTaskScope" not in serialized
    assert "Tunnel" not in serialized


@pytest.mark.asyncio
async def test_triggerflow_retains_top_level_failure_until_close() -> None:
    flow = TriggerFlow(name="stage-support-completed-failure")
    execution = flow.create_execution(auto_close=False)

    async def fail_before_close() -> None:
        raise RuntimeError("managed task failed before close")

    task = execution._track_task(
        asyncio.create_task(fail_before_close()),
        origin="emit_nowait:event:fail",
    )
    with pytest.raises(RuntimeError, match="managed task failed before close"):
        await task
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="managed task failed before close"):
        await execution.async_close()


@pytest.mark.asyncio
async def test_triggerflow_close_timeout_is_one_settlement_deadline() -> None:
    flow = TriggerFlow(name="stage-support-close-deadline")
    execution = flow.create_execution(auto_close=False)
    cancellation_seen = asyncio.Event()

    async def suppress_cancellation_temporarily() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancellation_seen.set()
            await asyncio.sleep(0.12)

    execution._track_task(
        asyncio.create_task(suppress_cancellation_temporarily()),
        origin="emit_nowait:event:slow-cancel",
    )
    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match="slow-cancel"):
        await execution.async_close(timeout=0.02)
    elapsed = time.monotonic() - started_at

    assert cancellation_seen.is_set()
    assert elapsed < 0.08
