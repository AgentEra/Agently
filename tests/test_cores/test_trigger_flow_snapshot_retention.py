from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently.core.storage import RecordStore


def test_trigger_flow_execution_exposes_canonical_recovery_run_id() -> None:
    flow = TriggerFlow(name="snapshot-retention-run-id")
    execution = flow.create_execution(
        auto_close=False,
        record_store=False,
    )

    assert execution.run_id == execution.run_context.run_id
    assert execution.run_id != execution.id
    assert execution.result.get_meta()["run_id"] == execution.run_id


@pytest.mark.asyncio
async def test_trigger_flow_execution_prunes_its_bound_recovery_history_without_run_id(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": None},
    )
    flow = TriggerFlow(name="snapshot-retention-active-prune")
    execution = flow.create_execution(
        auto_close=False,
        record_store=store,
    )
    for index in range(4):
        await execution.async_set_state("index", index)
        await execution.async_save(step_id=f"state-{index}")

    result = await execution.async_prune_recovery_snapshots()
    history = await store.checkpoint_history(execution.run_id)

    assert result["run_id"] == execution.run_id
    assert result["keep_last"] == 1
    assert result["deleted_records"] == 3
    assert len(history) == 1
    assert execution.get_lifecycle_state() == "open"


@pytest.mark.asyncio
async def test_trigger_flow_execution_snapshot_retention_overrides_provider_default(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": 3},
    )
    flow = TriggerFlow(name="snapshot-retention-execution-policy")
    execution = flow.create_execution(
        auto_close=False,
        record_store=store,
    )
    execution.set_snapshot_retention_policy(keep_last=2)

    for index in range(4):
        await execution.async_set_state("index", index)
        await execution.async_save(step_id=f"state-{index}")

    history = await store.checkpoint_history(execution.run_id)

    assert len(history) == 2


@pytest.mark.asyncio
async def test_trigger_flow_execution_can_disable_provider_automatic_retention(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": 3},
    )
    flow = TriggerFlow(name="snapshot-retention-disabled")
    execution = flow.create_execution(
        auto_close=False,
        record_store=store,
    )
    execution.set_snapshot_retention_policy(keep_last=None)

    for index in range(4):
        await execution.async_set_state("index", index)
        await execution.async_save(step_id=f"state-{index}")

    assert len(await store.checkpoint_history(execution.run_id)) == 4


@pytest.mark.asyncio
async def test_trigger_flow_snapshot_retention_override_survives_save_and_load(
    tmp_path,
) -> None:
    store = RecordStore(
        tmp_path,
        mode="read_write",
        snapshot_retention={"keep_last": 3},
    )
    flow = TriggerFlow(name="snapshot-retention-restore")
    source = flow.create_execution(
        auto_close=False,
        record_store=store,
    )
    source.set_snapshot_retention_policy(keep_last=2)
    snapshot = source.save(require_idle=True)

    assert snapshot["snapshot_retention_policy"] == {"keep_last": 2}

    restored = flow.create_execution(
        auto_close=False,
        record_store=store,
    )
    restored.load(snapshot)
    for index in range(4):
        await restored.async_set_state("index", index)
        await restored.async_save(step_id=f"restored-{index}")

    assert len(await store.checkpoint_history(restored.run_id)) == 2


@pytest.mark.asyncio
async def test_trigger_flow_rejects_active_snapshot_prune_before_provider_mutation(
    tmp_path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release(data: TriggerFlowRuntimeData) -> None:
        _ = data
        entered.set()
        await release.wait()

    store = RecordStore(tmp_path, mode="read_write")
    flow = TriggerFlow(name="snapshot-retention-active-guard")
    flow.to(wait_for_release)
    execution = flow.create_execution(
        auto_close=False,
        record_store=store,
    )
    start_task = asyncio.create_task(execution.async_start(None))
    await entered.wait()

    with pytest.raises(RuntimeError, match="active TriggerFlow execution"):
        await execution.async_prune_recovery_snapshots()

    assert await store.checkpoint_history(execution.run_id) == []
    release.set()
    await start_task


class _SnapshotStoreWithoutRetention:
    def __init__(self) -> None:
        self.put_calls = 0

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, str]:
        _ = (run_id, state, step_id, expected_state_version)
        self.put_calls += 1
        return {"id": "snapshot"}

    async def get_snapshot(self, run_id: str) -> None:
        _ = run_id
        return None


@pytest.mark.asyncio
async def test_trigger_flow_retention_override_fails_before_unsupported_provider_write() -> None:
    store = _SnapshotStoreWithoutRetention()
    flow = TriggerFlow(name="snapshot-retention-provider-guard")
    execution = flow.create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={"snapshot_store": store},
    )
    execution.set_snapshot_retention_policy(keep_last=2)

    with pytest.raises(TypeError, match="must accept retention"):
        await execution.async_save()

    assert store.put_calls == 0


@pytest.mark.asyncio
async def test_trigger_flow_automatic_wait_snapshots_keep_latest_three_and_restore(
    tmp_path,
) -> None:
    async def pause_one(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="pause-1",
            resume_to="next",
        )

    async def pause_two(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="pause-2",
            resume_to="next",
        )

    async def pause_three(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="pause-3",
            resume_to="next",
        )

    async def pause_four(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="pause-4",
            resume_to="next",
        )

    async def pause_five(data: TriggerFlowRuntimeData):
        return await data.async_pause_for(
            interrupt_id="pause-5",
            resume_to="next",
        )

    store = RecordStore(tmp_path, mode="read_write")
    flow = TriggerFlow(name="snapshot-retention-automatic-waits")
    flow.to(pause_one).to(pause_two).to(pause_three).to(pause_four).to(pause_five)
    execution = flow.create_execution(
        auto_close=False,
        record_store=store,
    )

    await execution.async_start(None)
    for index in range(1, 5):
        await execution.async_continue_with(
            f"pause-{index}",
            {"approved": True},
            resume_request_id=f"request-{index}",
        )

    history = await store.checkpoint_history(execution.run_id)
    latest = await store.get_snapshot(execution.run_id)
    restored = flow.create_execution(
        auto_close=False,
        record_store=store,
    )
    assert latest is not None
    restored.load(latest)

    assert len(history) == 3
    assert set(restored.get_pending_interrupts()) == {"pause-5"}
