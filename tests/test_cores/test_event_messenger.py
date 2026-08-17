import logging
import asyncio
from typing import TYPE_CHECKING

import pytest

from agently import Agently
from agently.builtins.hookers.RuntimeConsoleSinkHooker import (
    RuntimeConsoleSinkHooker,
    _resolve_action_stage,
    _resolve_tool_stage,
    _resolve_tool_name,
    resolve_runtime_log_profile,
    should_render_console_event,
    should_render_storage_event,
)
from agently.core import EventCenter, ObservationEventEmitter, RuntimeEventEmitter
from agently.core.runtime.RuntimeContext import bind_runtime_context
from agently.types.data import ObservationEvent, RuntimeEvent
from agently.utils import Settings

if TYPE_CHECKING:
    from agently.types.data import ObservationEvent


_RUNTIME_LOG_KEYS = (
    "debug",
    "runtime.show_model_logs",
    "runtime.show_action_logs",
    "runtime.show_tool_logs",
    "runtime.show_trigger_flow_logs",
    "runtime.show_runtime_logs",
    "runtime.httpx_log_level",
)


def _snapshot_runtime_log_settings():
    return {key: Agently.settings.get(key, None) for key in _RUNTIME_LOG_KEYS}


def _restore_runtime_log_settings(snapshot):
    for key, value in snapshot.items():
        Agently.settings.set(key, value)
    level_name = Agently.settings.get("runtime.httpx_log_level", "WARNING")
    level = getattr(logging, str(level_name).upper(), logging.WARNING)
    logging.getLogger("httpx").setLevel(level)
    logging.getLogger("httpcore").setLevel(level)


def _build_runtime_log_settings(profile: str = "off") -> Settings:
    return Settings(
        {
            "runtime": {
                "show_model_logs": profile,
                "show_tool_logs": profile,
                "show_trigger_flow_logs": profile,
                "show_runtime_logs": profile,
            }
        }
    )


@pytest.mark.asyncio
async def test_async_runtime_emitter():
    emitter = Agently.event_center.create_emitter("Async Test")
    saved_event = None

    async def capture(event: "ObservationEvent"):
        nonlocal saved_event
        saved_event = event

    hook_name = "test_async_runtime_emitter.capture"
    Agently.event_center.register_hook(capture, event_types="runtime.info", hook_name=hook_name)
    try:
        await emitter.async_info("Hello")
        assert saved_event is not None
        assert saved_event.message == "Hello"
        assert saved_event.source == "Async Test"
        with pytest.raises(RuntimeError):
            await emitter.async_error("Something Wrong")
    finally:
        Agently.event_center.unregister_hook(hook_name)


def test_sync_runtime_emitter():
    emitter = Agently.event_center.create_emitter("Test", base_meta={"scope": "unit-test"})
    saved_event = None

    def capture(event: "ObservationEvent"):
        nonlocal saved_event
        saved_event = event

    hook_name = "test_sync_runtime_emitter.capture"
    Agently.event_center.register_hook(capture, event_types="runtime.info", hook_name=hook_name)
    try:
        emitter.info("Bye")
        assert saved_event is not None
        assert saved_event.message == "Bye"
        assert saved_event.meta["scope"] == "unit-test"
        with pytest.raises(RuntimeError):
            emitter.critical("Something Really Bad")
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_observation_event_names_are_preferred_aliases():
    assert issubclass(RuntimeEvent, ObservationEvent)
    assert issubclass(RuntimeEventEmitter, ObservationEventEmitter)
    assert hasattr(Agently, "emit_observation")
    assert hasattr(Agently, "async_emit_observation")

    ec = EventCenter()
    captured: list[ObservationEvent] = []

    async def capture(event: ObservationEvent):
        captured.append(event)

    ec.register_hook(capture, event_types="observation.alias", hook_name="capture_observation_alias")

    await ec.async_emit(ObservationEvent(event_type="observation.alias", message="alias object"))
    await ec.create_observation_emitter("ObservationTest").async_emit(
        "observation.alias",
        message="alias emitter",
    )
    await ec.async_emit(RuntimeEvent(event_type="observation.alias", message="legacy object"))

    assert all(isinstance(event, ObservationEvent) for event in captured)
    assert all(isinstance(event, RuntimeEvent) for event in captured)
    assert type(captured[0]) is RuntimeEvent
    assert type(captured[1]) is RuntimeEvent
    assert type(captured[2]) is RuntimeEvent
    assert [event.message for event in captured] == ["alias object", "alias emitter", "legacy object"]


@pytest.mark.asyncio
async def test_event_center_filtering():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def allowed_only(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(allowed_only, event_types="custom.allowed", hook_name="allowed_only")
    emitter = ec.create_emitter("TestModule", base_meta={"scope": "unit-test"})

    await emitter.async_emit("custom.allowed", message="first", payload={"row": 1})
    await emitter.async_emit("custom.blocked", message="second")

    assert len(captured) == 1
    assert captured[0].event_type == "custom.allowed"
    assert captured[0].payload == {"row": 1}
    assert captured[0].meta["scope"] == "unit-test"


@pytest.mark.asyncio
async def test_event_center_summary_delivery_policy_batches_high_frequency_events():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        capture,
        event_types="model.response.delta",
        hook_name="summary_capture",
        delivery_policy={"mode": "summary", "max_items": 3},
    )

    for value in ("A", "B", "C"):
        await ec.async_emit(
            {
                "event_type": "model.response.delta",
                "source": "model",
                "payload": {"delta": value},
                "meta": {"response_id": "response-1", "frequency": "high"},
            }
        )

    assert len(captured) == 1
    assert captured[0].event_type == "model.response.delta"
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 3
    assert captured[0].payload["count"] == 3
    assert [event["payload"]["delta"] for event in captured[0].payload["events"]] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_event_center_summary_policy_flushes_before_non_high_frequency_event():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        capture,
        hook_name="mixed_summary_capture",
        delivery_policy={"mode": "summary", "max_items": 10, "high_frequency_only": True},
    )

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    await ec.async_emit({"event_type": "model.response.completed", "payload": {"text": "A"}})

    assert len(captured) == 2
    assert captured[0].event_type == "model.response.delta"
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 1
    assert captured[1].event_type == "model.response.completed"
    assert "coalesced" not in captured[1].meta


@pytest.mark.asyncio
async def test_event_center_flush_releases_buffered_summary_events():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        capture,
        event_types="model.response.delta",
        hook_name="flush_summary_capture",
        delivery_policy={"mode": "summary", "max_items": 10},
    )

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    assert captured == []

    await ec.async_flush("flush_summary_capture")

    assert len(captured) == 1
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 1


@pytest.mark.asyncio
async def test_event_center_default_delivery_policy_remains_raw():
    ec = EventCenter()
    captured: list[RuntimeEvent] = []

    async def capture(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(capture, event_types="model.response.delta", hook_name="raw_capture")

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "B"}})

    assert [event.payload["delta"] for event in captured] == ["A", "B"]
    assert all("coalesced" not in event.meta for event in captured)


@pytest.mark.asyncio
async def test_event_center_background_hook_does_not_block_emit_path_and_flush_recovers():
    ec = EventCenter()
    slow_started = asyncio.Event()
    slow_can_finish = asyncio.Event()
    fast_captured: list[RuntimeEvent] = []

    async def slow_hook(event: RuntimeEvent):
        slow_started.set()
        await slow_can_finish.wait()

    async def fast_hook(event: RuntimeEvent):
        fast_captured.append(event)

    ec.register_hook(slow_hook, hook_name="slow", delivery_policy={"dispatch": "background"})
    ec.register_hook(fast_hook, hook_name="fast")

    started_at = asyncio.get_running_loop().time()
    await ec.async_emit({"event_type": "runtime.info", "message": "hello"})
    elapsed = asyncio.get_running_loop().time() - started_at

    assert elapsed < 0.05
    assert slow_started.is_set()
    assert [event.message for event in fast_captured] == ["hello"]

    slow_can_finish.set()
    await ec.async_flush("slow")


@pytest.mark.asyncio
async def test_event_center_emit_nowait_tracks_dispatch_until_flush():
    ec = EventCenter(idle_flush_seconds=None)
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def hook(event: RuntimeEvent):
        started.set()
        await release.wait()
        observed.append(event.event_type)

    ec.register_hook(hook, hook_name="tracked")
    dispatch = ec.emit_nowait(
        RuntimeEvent(
            event_type="runtime.info",
            source="emit-nowait-test",
        )
    )

    assert isinstance(dispatch, asyncio.Task)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert dispatch in ec._background_tasks

    release.set()
    await ec.async_flush()
    assert observed == ["runtime.info"]
    assert not ec._background_tasks


@pytest.mark.asyncio
async def test_event_center_idle_flush_recovers_background_delivery():
    ec = EventCenter(idle_flush_seconds=0.01, background_timeout=0.1)
    completed: list[str] = []

    async def hook(event: RuntimeEvent):
        await asyncio.sleep(0.01)
        completed.append(event.message or "")

    ec.register_hook(hook, hook_name="background", delivery_policy={"dispatch": "background"})

    await ec.async_emit({"event_type": "runtime.info", "message": "idle"})
    await asyncio.sleep(0.05)

    assert completed == ["idle"]


@pytest.mark.asyncio
async def test_event_center_idle_flush_cancels_stuck_background_delivery():
    ec = EventCenter(idle_flush_seconds=0.01, background_timeout=0.01)
    cancelled = asyncio.Event()

    async def hook(event: RuntimeEvent):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    ec.register_hook(hook, hook_name="stuck", delivery_policy={"dispatch": "background"})

    await ec.async_emit({"event_type": "runtime.info", "message": "stuck"})
    await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    await asyncio.sleep(0)

    assert not ec._background_tasks


@pytest.mark.asyncio
async def test_event_center_idle_flush_releases_summary_buffer():
    ec = EventCenter(idle_flush_seconds=0.01, background_timeout=0.1)
    captured: list[RuntimeEvent] = []

    async def hook(event: RuntimeEvent):
        captured.append(event)

    ec.register_hook(
        hook,
        hook_name="summary",
        event_types="model.response.delta",
        delivery_policy={"mode": "summary", "max_items": 10},
    )

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    assert captured == []

    await asyncio.sleep(0.05)

    assert len(captured) == 1
    assert captured[0].meta["coalesced"] is True
    assert captured[0].meta["coalesced_count"] == 1


@pytest.mark.asyncio
async def test_event_center_emit_uses_hook_snapshot_when_hook_unregisters_during_flush():
    ec = EventCenter(idle_flush_seconds=None)
    victim_events: list[str] = []

    async def unregistering_hook(event: RuntimeEvent):
        ec.unregister_hook("victim")

    async def victim_hook(event: RuntimeEvent):
        victim_events.append(event.event_type)

    ec.register_hook(
        unregistering_hook,
        hook_name="unregistering",
        delivery_policy={"mode": "summary"},
    )
    ec.register_hook(victim_hook, hook_name="victim")

    await ec.async_emit({"event_type": "model.response.delta", "payload": {"delta": "A"}})
    await ec.async_emit({"event_type": "request.completed", "message": "done"})

    assert victim_events == ["model.response.delta", "request.completed"]
    assert "victim" not in ec._hooks


@pytest.mark.asyncio
async def test_event_center_default_hook_dispatch_awaits_completion():
    ec = EventCenter()
    completed: list[str] = []

    async def slow_hook(event: RuntimeEvent):
        await asyncio.sleep(0.01)
        completed.append(event.message or "")

    ec.register_hook(slow_hook, hook_name="slow_default")

    await ec.async_emit({"event_type": "runtime.info", "message": "reliable"})

    assert completed == ["reliable"]


@pytest.mark.asyncio
async def test_event_center_infers_source_for_emitter_and_direct_emit():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def capture(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(capture, hook_name="capture")

    class SourceOwner:
        name = "InferredOwner"

        def build_emitter(self):
            return ec.create_emitter()

        async def emit_directly(self):
            await ec.async_emit({"event_type": "custom.direct", "message": "direct"})

    owner = SourceOwner()
    emitter = owner.build_emitter()
    await emitter.async_emit("custom.emitter", message="via emitter")
    await owner.emit_directly()

    assert len(captured) == 2
    assert captured[0].source == "InferredOwner"
    assert captured[1].source == "InferredOwner"


@pytest.mark.asyncio
async def test_event_center_matches_triggerflow_aliases_for_legacy_subscriptions():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def capture(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(capture, event_types="workflow.execution_started", hook_name="capture")
    emitter = ec.create_emitter("TriggerFlowTest")

    await emitter.async_emit("triggerflow.execution_started", message="started")

    assert len(captured) == 1
    assert captured[0].event_type == "triggerflow.execution_started"
    assert captured[0].message == "started"


@pytest.mark.asyncio
async def test_event_center_keeps_action_and_tool_loop_filters_exact():
    ec = EventCenter()
    action_captured: list["ObservationEvent"] = []
    tool_captured: list["ObservationEvent"] = []

    async def capture_action(event: "ObservationEvent"):
        action_captured.append(event)

    async def capture_tool(event: "ObservationEvent"):
        tool_captured.append(event)

    ec.register_hook(capture_action, event_types="action.loop_started", hook_name="capture_action")
    ec.register_hook(capture_tool, event_types="tool.loop_started", hook_name="capture_tool")
    emitter = ec.create_emitter("ActionFlowTest")

    await emitter.async_emit("action.loop_started", message="started")
    await emitter.async_emit("tool.loop_started", message="legacy started")

    assert [event.event_type for event in action_captured] == ["action.loop_started"]
    assert [event.event_type for event in tool_captured] == ["tool.loop_started"]


@pytest.mark.asyncio
async def test_event_center_normalizes_cancelled_error():
    ec = EventCenter()
    captured: list["ObservationEvent"] = []

    async def capture(event: "ObservationEvent"):
        captured.append(event)

    ec.register_hook(capture, event_types="runtime.error", hook_name="capture_cancelled_error")

    await ec.async_emit(
        {
            "event_type": "runtime.error",
            "error": asyncio.CancelledError(),
        }
    )

    assert len(captured) == 1
    assert captured[0].error is not None
    assert captured[0].error.type == "CancelledError"
    assert captured[0].error.module == "asyncio.exceptions"


def test_runtime_log_profiles_keep_default_off_quiet():
    settings = _build_runtime_log_settings("off")

    assert not should_render_console_event(RuntimeEvent(event_type="model.requesting"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="tool.loop_started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="triggerflow.execution_started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="request.completed"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.requesting", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="model.requester.error", level="ERROR"), settings)
    assert should_render_storage_event(RuntimeEvent(event_type="request.failed", level="WARNING"), settings)


def test_runtime_log_profiles_simple_mode_uses_summary_whitelists():
    settings = _build_runtime_log_settings("simple")

    assert should_render_console_event(RuntimeEvent(event_type="model.requesting", message="requesting"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="model.completed", payload={"raw_text": "done"}), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="model.streaming", message="delta"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="model.request_failed", level="ERROR"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="model.validation_failed", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.loop_started", message="started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="action.plan_ready", message="ready"), settings)
    assert should_render_console_event(
        RuntimeEvent(event_type="action.started", payload={"action_type": "tool", "action_name": "get_weather"}),
        settings,
    )
    assert should_render_console_event(RuntimeEvent(event_type="action.completed", level="INFO"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.approval_required", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.blocked", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.failed", level="WARNING"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="tool.loop_started", message="started"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="tool.plan_ready", message="ready"), settings)
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="tool.loop_started",
            message="compat started",
            meta={"compat_event_alias": True, "compat_alias_for": "action.loop_started"},
        ),
        settings,
    )
    assert should_render_console_event(
        RuntimeEvent(event_type="triggerflow.execution_started", message="execution started"),
        settings,
    )
    assert should_render_console_event(RuntimeEvent(event_type="triggerflow.execution_failed", level="ERROR"), settings)
    assert not should_render_console_event(RuntimeEvent(event_type="triggerflow.signal", message="signal"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="agent_execution.started", message="started"), settings)
    assert should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={"stream_kind": "phase", "path": "agent_task.phase.planned", "value": {"phase": "planned"}},
        ),
        settings,
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream",
            payload={"stream_kind": "model_delta", "path": "agent_task.model.delta", "value": "A"},
        ),
        settings,
    )
    assert not should_render_console_event(
        RuntimeEvent(
            event_type="agent_execution.stream.delta",
            payload={"stream_kind": "progress_delta", "path": "agent_task.progress", "delta": "A"},
        ),
        settings,
    )
    assert not should_render_console_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="request.failed", level="ERROR"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.requesting", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.failed", level="ERROR"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="runtime.print", level="INFO", message="hello"), settings)
    assert not should_render_storage_event(
        RuntimeEvent(
            event_type="tool.loop_failed",
            level="ERROR",
            meta={"compat_event_alias": True, "compat_alias_for": "action.loop_failed"},
        ),
        settings,
    )


def test_runtime_log_profiles_detail_mode_allows_full_runtime_detail():
    settings = _build_runtime_log_settings("detail")

    assert should_render_console_event(RuntimeEvent(event_type="model.streaming", message="delta"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="tool.plan_ready", message="ready"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="triggerflow.signal", message="signal"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="session.applied_to_request", level="INFO"), settings)
    assert should_render_console_event(RuntimeEvent(event_type="action.completed", level="INFO"), settings)

    assert not should_render_storage_event(RuntimeEvent(event_type="model.completed", level="INFO"), settings)
    assert not should_render_storage_event(RuntimeEvent(event_type="request.completed", level="INFO"), settings)


def test_action_logs_prefer_action_setting_and_fall_back_to_tool_setting():
    legacy_settings = Settings({"runtime": {"show_tool_logs": "simple"}})
    assert resolve_runtime_log_profile(legacy_settings, "action.started") == "simple"
    assert should_render_console_event(RuntimeEvent(event_type="action.started"), legacy_settings)

    preferred_settings = Settings({"runtime": {"show_action_logs": "off", "show_tool_logs": "detail"}})
    assert resolve_runtime_log_profile(preferred_settings, "action.started") == "off"
    assert not should_render_console_event(RuntimeEvent(event_type="action.started"), preferred_settings)

    parent_settings = Settings({"runtime": {"show_action_logs": "detail"}})
    child_settings = Settings({"runtime": {"show_tool_logs": "off"}}, parent=parent_settings)
    assert resolve_runtime_log_profile(child_settings, "action.started") == "off"


def test_tool_console_stage_uses_event_type_before_success_payload():
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.loop_started")) == "Started"
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.loop_completed")) == "Completed"
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.loop_failed", level="ERROR")) == "Failed"
    assert _resolve_tool_stage(RuntimeEvent(event_type="tool.plan_ready")) == "Plan Ready"
    assert _resolve_tool_stage(RuntimeEvent(event_type="custom.completed", payload={"success": True})) == "Completed"
    assert _resolve_tool_stage(RuntimeEvent(event_type="custom.failed", payload={"success": False})) == "Failed"


def test_action_console_stage_uses_action_loop_event_types():
    assert _resolve_action_stage(RuntimeEvent(event_type="action.loop_started")) == "Started"
    assert _resolve_action_stage(RuntimeEvent(event_type="action.plan_ready")) == "Plan Ready"
    assert _resolve_action_stage(RuntimeEvent(event_type="action.loop_completed")) == "Completed"
    assert _resolve_action_stage(RuntimeEvent(event_type="action.loop_failed", level="ERROR")) == "Failed"


def test_tool_console_name_uses_action_payload_and_record():
    assert _resolve_tool_name(RuntimeEvent(event_type="tool.loop_started")) is None
    assert (
        _resolve_tool_name(RuntimeEvent(event_type="action.started", payload={"action_name": "get_weather"}))
        == "get_weather"
    )
    assert (
        _resolve_tool_name(
            RuntimeEvent(event_type="action.completed", payload={"record": {"tool_name": "search_docs"}})
        )
        == "search_docs"
    )


def test_tool_console_rendering_does_not_mark_loop_start_as_failed(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_tool_event(  # type: ignore[attr-defined]
        RuntimeEvent(event_type="tool.loop_started", message="Tool loop started."),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_tool_event(  # type: ignore[attr-defined]
        RuntimeEvent(event_type="tool.loop_completed", message="Tool loop completed."),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "Started" in rendered
    assert "Completed" in rendered
    assert "ToolLoop" in rendered
    assert "Tool-unknown" not in rendered
    assert "Failed" not in rendered


def test_action_console_rendering_shows_action_name_and_type(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_action_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="action.completed",
            message="Action 'get_weather' completed.",
            payload={"action_type": "tool", "action_name": "get_weather"},
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "Action-get_weather" in rendered
    assert "type=tool" in rendered
    assert "Completed" in rendered
    assert "Action-unknown" not in rendered


def test_action_console_rendering_shows_loop_without_unknown_action(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_action_event(  # type: ignore[attr-defined]
        RuntimeEvent(event_type="action.loop_started", message="Action loop started."),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "ActionLoop" in rendered
    assert "Started" in rendered
    assert "Action-unknown" not in rendered


def test_model_console_simple_renders_request_and_result(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.requesting",
            source="probe",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-1",
                "request_text": "USER: summarize revenue risk",
            },
        ),
        "simple",
    )
    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.completed",
            source="probe",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-1",
                "raw_text": "Revenue risk is moderate.",
                "result": {"summary": "fallback should not win when raw_text exists"},
            },
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "Requesting" in rendered
    assert "USER: summarize revenue risk" in rendered
    assert "Done" in rendered
    assert "Revenue risk is moderate." in rendered
    assert "fallback should not win" not in rendered


def test_model_console_detail_keeps_structured_result_priority(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_model_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="model.completed",
            source="probe",
            payload={
                "agent_name": "debug-agent",
                "response_id": "resp-1",
                "raw_text": "raw final text",
                "result": {"summary": "structured final result"},
            },
        ),
        "detail",
    )

    rendered = "\n".join(printed)
    assert "structured final result" in rendered
    assert "raw final text" not in rendered


def test_agent_execution_console_simple_renders_process_summary(monkeypatch):
    printed: list[str] = []

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    RuntimeConsoleSinkHooker._handle_agent_execution_event(  # type: ignore[attr-defined]
        RuntimeEvent(
            event_type="agent_execution.stream",
            source="BaseAgent",
            payload={
                "execution_id": "exec-1",
                "stream_kind": "phase",
                "path": "agent_task.phase.planned",
                "value": {"phase": "planned", "detail": "Plan accepted."},
            },
            meta={"execution_id": "exec-1"},
        ),
        "simple",
    )

    rendered = "\n".join(printed)
    assert "AgentExecution" in rendered
    assert "Execution-exec-1" in rendered
    assert "Process" in rendered
    assert "kind=phase" in rendered
    assert "agent_task.phase.planned" in rendered
    assert "Plan accepted." in rendered


@pytest.mark.asyncio
async def test_runtime_console_sink_renders_generic_runtime_events(monkeypatch):
    printed: list[str] = []
    settings = _build_runtime_log_settings("detail")

    def capture_print(*args, **kwargs):
        _ = kwargs
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)

    with bind_runtime_context(settings=settings):
        await RuntimeConsoleSinkHooker.handler(
            RuntimeEvent(
                event_type="request.completed",
                source="ModelResponse",
                message="Request completed.",
            )
        )

    rendered = "\n".join(printed)
    assert "[ModelResponse] [request.completed]" in rendered
    assert "Request completed." in rendered


@pytest.mark.asyncio
async def test_runtime_console_sink_uses_run_context_log_settings(monkeypatch):
    snapshot = _snapshot_runtime_log_settings()
    printed: list[str] = []
    ec = EventCenter()
    hook_name = "runtime_console_sink.test"

    def capture_print(*args, **kwargs):
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("builtins.print", capture_print)
    ec.register_hook(RuntimeConsoleSinkHooker.handler, hook_name=hook_name)

    try:
        Agently.set_settings("debug", False)
        request = Agently.create_request("debug-check")
        request.set_settings("debug", True)

        with bind_runtime_context(settings=request.settings):
            await ec.async_emit(
                {
                    "event_type": "model.requesting",
                    "source": "probe",
                    "level": "INFO",
                    "message": "requesting",
                    "run": request._create_request_run_context(),
                }
            )

        assert printed
        assert any("requesting" in line for line in printed)
    finally:
        ec.unregister_hook(hook_name)
        _restore_runtime_log_settings(snapshot)
