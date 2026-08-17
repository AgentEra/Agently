# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from agently_stage import default_stage_call_bridge

import asyncio
import inspect
import time
from dataclasses import dataclass
from pathlib import Path

from typing import TYPE_CHECKING, Any, Mapping, cast

from agently.types.data import ErrorInfo, EventDeliveryPolicy, ObservationEvent, RuntimeEvent
from agently.types.data.event import matches_runtime_event_type

if TYPE_CHECKING:
    from agently.types.data import EventHook, ObservationEventLevel, RunContext
    from agently.types.plugins import EventHooker


_INTERNAL_SOURCE_MODULES = {
    "agently.core.runtime.EventCenter",
    "agently.utils.RuntimeEmitter",
}


def _infer_runtime_source() -> str:
    frame = inspect.currentframe()
    try:
        current = frame.f_back if frame is not None else None
        while current is not None:
            module_name = str(current.f_globals.get("__name__", ""))
            if module_name in _INTERNAL_SOURCE_MODULES:
                current = current.f_back
                continue

            source_instance = current.f_locals.get("self")
            if source_instance is not None:
                source_name = getattr(source_instance, "name", None)
                if isinstance(source_name, str) and source_name:
                    return source_name
                class_name = getattr(source_instance.__class__, "__name__", None)
                if isinstance(class_name, str) and class_name:
                    return class_name

            source_class = current.f_locals.get("cls")
            class_name = getattr(source_class, "__name__", None)
            if isinstance(class_name, str) and class_name:
                return class_name

            if module_name and module_name != "__main__":
                return module_name.rsplit(".", 1)[-1]

            file_name = current.f_code.co_filename
            if file_name:
                return Path(file_name).stem

            current = current.f_back
    finally:
        del frame
    return "Agently"


def _normalize_delivery_policy(policy: EventDeliveryPolicy | Mapping[str, Any] | None) -> EventDeliveryPolicy:
    source = dict(policy or {})
    mode = source.get("mode", "raw")
    if mode not in ("raw", "summary"):
        mode = "raw"
    dispatch = source.get("dispatch", "await")
    if dispatch not in ("await", "background"):
        dispatch = "await"

    def _optional_float(value: Any) -> float | None:
        if value in (None, -1, "-1"):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _optional_int(value: Any) -> int | None:
        if value in (None, -1, "-1"):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    return {
        "mode": cast(Any, mode),
        "dispatch": cast(Any, dispatch),
        "emit_interval": _optional_float(source.get("emit_interval", source.get("interval"))),
        "max_items": _optional_int(source.get("max_items", source.get("batch_size"))),
        "high_frequency_only": bool(source.get("high_frequency_only", True)),
        "max_summary_items": _optional_int(source.get("max_summary_items")) or 20,
        "idle_flush_seconds": _optional_float(source.get("idle_flush_seconds")),
        "background_timeout": _optional_float(source.get("background_timeout")),
    }


def _is_summary_policy(policy: EventDeliveryPolicy) -> bool:
    return policy.get("mode") == "summary" or policy.get("emit_interval") is not None or policy.get("max_items") is not None


def _is_high_frequency_event(event: RuntimeEvent) -> bool:
    meta = event.meta or {}
    if meta.get("high_frequency") is True or meta.get("frequency") == "high":
        return True
    event_type = event.event_type or ""
    if event_type.endswith(".delta") or event_type.endswith("_delta"):
        return True
    if event_type in {"triggerflow.stream_item_emitted", "model.response.delta", "response.delta"}:
        return True
    payload = event.payload
    if isinstance(payload, Mapping):
        if payload.get("event_type") == "delta" or isinstance(payload.get("delta"), str):
            return True
    return False


def _compact_event_payload(event: RuntimeEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "source": event.source,
        "level": event.level,
        "message": event.message,
        "payload": event.payload,
        "run": event.run.model_dump() if event.run is not None else None,
        "meta": event.meta,
        "timestamp": event.timestamp,
    }


def _consume_task_exception(task: asyncio.Task[Any]):
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


@dataclass
class _BufferedEventOutlet:
    events: list[RuntimeEvent]
    first_seen_at: float
    last_seen_at: float


@dataclass
class _HookRegistration:
    event_types: set[str] | None
    callback: "EventHook"
    delivery_policy: EventDeliveryPolicy
    buffer: _BufferedEventOutlet | None = None


class EventCenter:
    def __init__(
        self,
        *,
        idle_flush_seconds: float | None = 0.1,
        background_timeout: float | None = 5.0,
    ):
        self._hooks: dict[str, _HookRegistration] = {}
        self._hookers: dict[str, type["EventHooker"]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._background_task_hooks: dict[asyncio.Task[Any], str] = {}
        self._idle_flush_seconds = idle_flush_seconds
        self._next_idle_flush_seconds = idle_flush_seconds
        self._background_timeout = background_timeout
        self._idle_flush_generation = 0
        self._idle_flush_task: asyncio.Task[Any] | None = None
        self.emit = default_stage_call_bridge.as_sync(self.async_emit)
        self.flush = default_stage_call_bridge.as_sync(self.async_flush)

    def emit_nowait(self, event: "Mapping[str, Any] | ObservationEvent | RuntimeEvent"):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return default_stage_call_bridge.submit(self.async_emit, event)
        task = loop.create_task(self.async_emit(event))
        return self._track_background_task(task, hook_name="$event_center.emit")

    def register_hook(
        self,
        callback: "EventHook",
        *,
        event_types: str | list[str] | None = None,
        hook_name: str | None = None,
        delivery_policy: EventDeliveryPolicy | Mapping[str, Any] | None = None,
    ):
        if hook_name is None:
            hook_name = callback.__name__
        normalized_event_types: set[str] | None = None
        if event_types is not None:
            normalized_event_types = {event_types} if isinstance(event_types, str) else set(event_types)
        self._hooks[hook_name] = _HookRegistration(
            event_types=normalized_event_types,
            callback=callback,
            delivery_policy=_normalize_delivery_policy(delivery_policy),
        )

    def unregister_hook(self, hook_name: str):
        if hook_name in self._hooks:
            del self._hooks[hook_name]

    def register_hooker_plugin(self, hooker: type["EventHooker"]):
        if hasattr(hooker, "_on_register"):
            hooker._on_register()
        self.register_hook(
            hooker.handler,
            event_types=hooker.event_types,
            hook_name=hooker.name,
            delivery_policy=getattr(hooker, "delivery_policy", None),
        )
        self._hookers[hooker.name] = hooker

    def unregister_hooker_plugin(self, hooker: str | type["EventHooker"]):
        if isinstance(hooker, str):
            if hooker not in self._hookers:
                return
            hooker = self._hookers[hooker]
        self.unregister_hook(hooker.name)
        if hasattr(hooker, "_on_unregister"):
            hooker._on_unregister()
        del self._hookers[hooker.name]

    async def async_emit(self, event: "Mapping[str, Any] | ObservationEvent | RuntimeEvent"):
        event_object = self._normalize_event(event)
        await_tasks: list[asyncio.Task[Any]] = []
        for hook_name, registration in list(self._hooks.items()):
            if not matches_runtime_event_type(event_object.event_type, registration.event_types):
                continue
            task = await self._prepare_hook_delivery(registration, event_object)
            if task is not None:
                if registration.delivery_policy.get("dispatch") == "background":
                    self._track_background_task(task, hook_name=hook_name)
                else:
                    await_tasks.append(task)
        if await_tasks:
            await asyncio.gather(*await_tasks, return_exceptions=True)
        if self._background_tasks:
            await asyncio.sleep(0)

    def _normalize_event(self, event: "Mapping[str, Any] | ObservationEvent | RuntimeEvent") -> RuntimeEvent:
        if isinstance(event, RuntimeEvent):
            return event
        elif isinstance(event, ObservationEvent):
            return RuntimeEvent.model_validate(event.model_dump())
        else:
            event_data: dict[str, Any] = dict(event)
            if not event_data.get("source"):
                event_data["source"] = _infer_runtime_source()
            return RuntimeEvent.model_validate(event_data)

    async def _prepare_hook_delivery(
        self,
        registration: _HookRegistration,
        event: RuntimeEvent,
    ) -> asyncio.Task[Any] | None:
        policy = registration.delivery_policy
        if not _is_summary_policy(policy):
            return self._create_hook_task(registration, event)
        if policy.get("high_frequency_only", True) and not _is_high_frequency_event(event):
            flush_task = await self._flush_registration(registration)
            current_task = self._create_hook_task(registration, event)
            if flush_task is not None:
                await asyncio.gather(flush_task, return_exceptions=True)
            return current_task

        now = time.monotonic()
        if registration.buffer is None:
            registration.buffer = _BufferedEventOutlet(events=[], first_seen_at=now, last_seen_at=now)
        registration.buffer.events.append(event)
        registration.buffer.last_seen_at = now
        self._schedule_idle_flush(registration.delivery_policy)

        max_items = policy.get("max_items")
        interval = policy.get("emit_interval")
        should_flush = False
        if isinstance(max_items, int) and max_items > 0 and len(registration.buffer.events) >= max_items:
            should_flush = True
        if isinstance(interval, (int, float)) and interval > 0:
            should_flush = should_flush or (now - registration.buffer.first_seen_at) >= float(interval)
        if should_flush:
            return await self._flush_registration(registration)
        return None

    def _create_hook_task(self, registration: _HookRegistration, event: RuntimeEvent) -> asyncio.Task[Any]:
        coro = default_stage_call_bridge.as_async(registration.callback, managed=True)
        return asyncio.create_task(coro(event))

    def _track_background_task(self, task: asyncio.Task[Any], *, hook_name: str):
        self._background_tasks.add(task)
        self._background_task_hooks[task] = hook_name
        self._schedule_idle_flush()
        task.add_done_callback(self._forget_background_task)
        return task

    def _forget_background_task(self, done_task: asyncio.Task[Any]):
        self._background_tasks.discard(done_task)
        self._background_task_hooks.pop(done_task, None)
        _consume_task_exception(done_task)

    def _has_buffered_events(self) -> bool:
        return any(registration.buffer is not None and bool(registration.buffer.events) for registration in self._hooks.values())

    def _schedule_idle_flush(self, policy: EventDeliveryPolicy | None = None):
        idle_seconds = (
            policy.get("idle_flush_seconds")
            if policy is not None and policy.get("idle_flush_seconds") is not None
            else self._idle_flush_seconds
        )
        if idle_seconds is None:
            return
        self._next_idle_flush_seconds = float(idle_seconds)
        self._idle_flush_generation += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._idle_flush_task is None or self._idle_flush_task.done():
            self._idle_flush_task = asyncio.create_task(self._run_idle_flush_monitor())
            self._idle_flush_task.add_done_callback(_consume_task_exception)

    async def _run_idle_flush_monitor(self):
        while self._background_tasks or self._has_buffered_events():
            generation = self._idle_flush_generation
            await asyncio.sleep(float(self._next_idle_flush_seconds or 0))
            if generation != self._idle_flush_generation:
                continue
            await self.async_flush(timeout=self._background_timeout)
            if generation == self._idle_flush_generation:
                break

    async def _flush_registration(self, registration: _HookRegistration) -> asyncio.Task[Any] | None:
        if registration.buffer is None or not registration.buffer.events:
            registration.buffer = None
            return None
        buffered = registration.buffer
        registration.buffer = None
        event = self._build_summary_event(buffered, registration.delivery_policy)
        return self._create_hook_task(registration, event)

    def _build_summary_event(self, buffered: _BufferedEventOutlet, policy: EventDeliveryPolicy) -> RuntimeEvent:
        events = buffered.events
        last = events[-1]
        max_summary_items = policy.get("max_summary_items") or 20
        selected_events = events[-int(max_summary_items) :]
        meta = dict(last.meta or {})
        meta.update(
            {
                "coalesced": True,
                "coalesced_count": len(events),
                "coalesced_event_type": last.event_type,
                "first_event_id": events[0].event_id,
                "last_event_id": last.event_id,
                "first_event_at": events[0].timestamp,
                "last_event_at": last.timestamp,
                "buffer_first_seen_at": buffered.first_seen_at,
                "buffer_last_seen_at": buffered.last_seen_at,
            }
        )
        return RuntimeEvent(
            event_type=last.event_type,
            source=last.source,
            level=last.level,
            message=f"{ len(events) } coalesced runtime events",
            payload={
                "count": len(events),
                "events": [_compact_event_payload(event) for event in selected_events],
                "last_payload": last.payload,
                "truncated": len(selected_events) < len(events),
            },
            error=last.error,
            run=last.run,
            meta=meta,
        )

    async def async_flush(self, hook_name: str | None = None, *, timeout: float | None = None):
        tasks: list[asyncio.Task[Any]] = []
        registrations = (
            [self._hooks[hook_name]]
            if hook_name is not None and hook_name in self._hooks
            else list(self._hooks.values())
        )
        for registration in registrations:
            task = await self._flush_registration(registration)
            if task is not None:
                tasks.append(task)
        tasks.extend(
            task
            for task in list(self._background_tasks)
            if hook_name is None or self._background_task_hooks.get(task) == hook_name
        )
        if tasks:
            if timeout is None:
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                done, pending = await asyncio.wait(tasks, timeout=timeout)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
            for task in tasks:
                if task.done() and task in self._background_tasks:
                    self._forget_background_task(task)

    def create_emitter(
        self,
        source: str | None = None,
        *,
        base_meta: dict[str, Any] | None = None,
        base_run: "RunContext | None" = None,
    ):
        return ObservationEventEmitter(
            self,
            source if source is not None else _infer_runtime_source(),
            base_meta=base_meta,
            base_run=base_run,
        )

    def create_observation_emitter(
        self,
        source: str | None = None,
        *,
        base_meta: dict[str, Any] | None = None,
        base_run: "RunContext | None" = None,
    ):
        return self.create_emitter(source, base_meta=base_meta, base_run=base_run)


class ObservationEventEmitter:
    def __init__(
        self,
        event_center: EventCenter,
        source: str,
        *,
        base_meta: dict[str, Any] | None = None,
        base_run: "RunContext | None" = None,
    ):
        self._event_center = event_center
        self._source = source
        self._base_meta = base_meta if base_meta is not None else {}
        self._base_run = base_run

        self.emit = default_stage_call_bridge.as_sync(self.async_emit)
        self.debug = default_stage_call_bridge.as_sync(self.async_debug)
        self.info = default_stage_call_bridge.as_sync(self.async_info)
        self.warning = default_stage_call_bridge.as_sync(self.async_warning)
        self.error = default_stage_call_bridge.as_sync(self.async_error)
        self.critical = default_stage_call_bridge.as_sync(self.async_critical)

    def update_base_meta(self, update_dict: dict[str, Any]):
        self._base_meta.update(update_dict)

    async def async_emit(
        self,
        event_type: str,
        *,
        level: "ObservationEventLevel" = "INFO",
        message: str | None = None,
        payload: Any = None,
        error: ErrorInfo | BaseException | None = None,
        run: "RunContext | None" = None,
        meta: dict[str, Any] | None = None,
    ):
        final_meta = self._base_meta.copy()
        if meta is not None:
            final_meta.update(meta)
        final_error: ErrorInfo | None = None
        if isinstance(error, BaseException):
            final_error = ErrorInfo.from_exception(error)
        else:
            final_error = error
        await self._event_center.async_emit(
            RuntimeEvent(
                event_type=event_type,
                source=self._source,
                level=level,
                message=message,
                payload=payload,
                error=final_error,
                run=run if run is not None else self._base_run,
                meta=final_meta,
            )
        )

    async def async_debug(
        self,
        message: Any,
        *,
        event_type: str = "runtime.debug",
        payload: Any = None,
        run: "RunContext | None" = None,
        meta: dict[str, Any] | None = None,
    ):
        await self.async_emit(
            event_type,
            level="DEBUG",
            message=str(message),
            payload=payload,
            run=run,
            meta=meta,
        )

    async def async_info(
        self,
        message: Any,
        *,
        event_type: str = "runtime.info",
        payload: Any = None,
        run: "RunContext | None" = None,
        meta: dict[str, Any] | None = None,
    ):
        await self.async_emit(
            event_type,
            level="INFO",
            message=str(message),
            payload=payload,
            run=run,
            meta=meta,
        )

    async def async_warning(
        self,
        message: Any,
        *,
        event_type: str = "runtime.warning",
        payload: Any = None,
        run: "RunContext | None" = None,
        meta: dict[str, Any] | None = None,
    ):
        await self.async_emit(
            event_type,
            level="WARNING",
            message=str(message),
            payload=payload,
            run=run,
            meta=meta,
        )

    async def async_error(
        self,
        error: str | BaseException,
        *,
        event_type: str = "runtime.error",
        message: str | None = None,
        payload: Any = None,
        run: "RunContext | None" = None,
        meta: dict[str, Any] | None = None,
    ):
        final_error = error if isinstance(error, BaseException) else RuntimeError(error)
        await self.async_emit(
            event_type,
            level="ERROR",
            message=message if message is not None else str(final_error),
            payload=payload,
            error=final_error,
            run=run,
            meta=meta,
        )
        from agently.base import settings

        if settings.get("runtime.raise_error"):
            raise final_error

    async def async_critical(
        self,
        critical: str | BaseException,
        *,
        event_type: str = "runtime.critical",
        message: str | None = None,
        payload: Any = None,
        run: "RunContext | None" = None,
        meta: dict[str, Any] | None = None,
    ):
        final_critical = critical if isinstance(critical, BaseException) else RuntimeError(critical)
        await self.async_emit(
            event_type,
            level="CRITICAL",
            message=message if message is not None else str(final_critical),
            payload=payload,
            error=final_critical,
            run=run,
            meta=meta,
        )
        from agently.base import settings

        if settings.get("runtime.raise_critical"):
            raise final_critical


class RuntimeEventEmitter(ObservationEventEmitter):
    pass
