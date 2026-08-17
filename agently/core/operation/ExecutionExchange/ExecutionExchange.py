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

from __future__ import annotations

from agently_stage import default_stage_call_bridge

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import (
    ExchangeRoutingHandler,
    ExecutionExchangeRequest,
    ExecutionExchangeRouting,
    ExecutionExchangeView,
)

if TYPE_CHECKING:
    from agently.core.runtime.EventCenter import EventCenter
    from agently.types.plugins import ExecutionExchangeProvider
    from agently.utils import Settings


_INTERACTION_MODE_WAIT_MODES = {
    "hot": "connected",
    "durable": "disconnected",
    "auto": "connected_then_disconnected",
}


class ExecutionExchangeManager:
    """Decision + transport coordinator for execution exchanges.

    Follows the Session two-handler precedent: routing handlers are the
    decision layer (whether/where a human is asked), the provider registry is
    the execution layer (channel transport). TriggerFlow interrupts stay the
    only wait/resume carrier; this manager never blocks inside TriggerFlow
    core — hot waits are driven by execution-handle owners.
    """

    def __init__(
        self,
        *,
        settings: "Settings",
        event_center: "EventCenter",
    ):
        self.settings = settings
        self.event_center = event_center
        self._providers: dict[str, "ExecutionExchangeProvider"] = {}
        self._routing_handlers: dict[str, ExchangeRoutingHandler] = {}
        self._live_waits: dict[str, dict[str, Any]] = {}
        # Stable bound-method reference: `self._posture_routing` yields a fresh
        # bound method on every access, so an identity check against it always
        # fails. Keep one reference and register/compare against it.
        self._posture_handler = self._posture_routing
        self.register_routing_handler("posture", self._posture_handler, replace=True)

        self.respond = default_stage_call_bridge.as_sync(self.async_respond)
        self.abandon = default_stage_call_bridge.as_sync(self.async_abandon)
        self.hot_wait_pending = default_stage_call_bridge.as_sync(self.async_hot_wait_pending)

    # ------------------------------------------------------------------ #
    # Provider registry (execution layer)
    # ------------------------------------------------------------------ #

    def register_provider(
        self,
        provider_id: str,
        provider: "ExecutionExchangeProvider",
        *,
        replace: bool = False,
    ) -> "ExecutionExchangeManager":
        resolved_id = str(provider_id or "").strip()
        if not resolved_id:
            raise ValueError("ExecutionExchange provider id cannot be empty.")
        if not callable(getattr(provider, "publish_request", None)):
            raise TypeError("ExecutionExchange provider must expose publish_request(...).")
        if resolved_id in self._providers and not replace:
            raise ValueError(f"ExecutionExchange provider '{ resolved_id }' is already registered.")
        self._providers[resolved_id] = provider
        return self

    def unregister_provider(self, provider_id: str) -> bool:
        return self._providers.pop(str(provider_id or "").strip(), None) is not None

    def get_provider(self, provider_id: str | None) -> "ExecutionExchangeProvider | None":
        if not provider_id:
            return None
        return self._providers.get(str(provider_id).strip())

    def list_providers(self) -> list[str]:
        return sorted(self._providers)

    # ------------------------------------------------------------------ #
    # Routing handlers (decision layer)
    # ------------------------------------------------------------------ #

    def register_routing_handler(
        self,
        name: str,
        handler: ExchangeRoutingHandler,
        *,
        replace: bool = False,
    ) -> "ExecutionExchangeManager":
        handler_name = str(name or "").strip()
        if not handler_name:
            raise ValueError("Exchange routing handler name cannot be empty.")
        if not callable(handler):
            raise TypeError("Exchange routing handler must be callable.")
        if handler_name in self._routing_handlers and not replace:
            raise ValueError(f"Exchange routing handler '{ handler_name }' is already registered.")
        self._routing_handlers[handler_name] = handler
        return self

    def unregister_routing_handler(self, name: str) -> bool:
        handler_name = str(name or "").strip()
        if handler_name == "posture":
            return False
        return self._routing_handlers.pop(handler_name, None) is not None

    def set_default_routing_handler(self, name: str) -> "ExecutionExchangeManager":
        handler_name = str(name or "").strip()
        if handler_name not in self._routing_handlers:
            raise ValueError(f"Exchange routing handler '{ handler_name }' is not registered.")
        self.settings.set("interaction.routing_handler", handler_name)
        return self

    async def async_route(
        self,
        request: ExecutionExchangeRequest | dict[str, Any],
        *,
        handler: str | None = None,
        settings: Any = None,
    ) -> ExecutionExchangeRouting | None:
        handler_name = str(
            handler
            or self.settings.get("interaction.routing_handler", "posture")
            or "posture"
        ).strip()
        selected = self._routing_handlers.get(handler_name) or self._posture_handler
        if selected is self._posture_handler:
            return self._posture_handler(cast(ExecutionExchangeRequest, dict(request or {})), settings=settings)
        result = selected(cast(ExecutionExchangeRequest, dict(request or {})))
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return None
        return cast(ExecutionExchangeRouting, dict(result))

    def _read_interaction_settings(self, settings: Any = None) -> Any:
        source = settings if settings is not None else self.settings
        getter = getattr(source, "get", None)
        if not callable(getter):
            return lambda key, default=None: default

        def _get(key: str, default: Any = None) -> Any:
            value = getter(f"interaction.{ key }", None)
            return value if value is not None else default

        return _get

    def _posture_routing(
        self,
        request: ExecutionExchangeRequest,
        *,
        settings: Any = None,
    ) -> ExecutionExchangeRouting:
        get = self._read_interaction_settings(settings)
        mode = str(get("mode", "hot") or "hot").strip().lower()
        overrides = get("overrides", {})
        kind = str(request.get("exchange_kind") or "")
        if isinstance(overrides, dict) and kind and overrides.get(kind):
            mode = str(overrides[kind]).strip().lower()
        wait_mode = _INTERACTION_MODE_WAIT_MODES.get(mode, "connected")
        hot_wait_timeout = get("hot_wait_timeout", 300)
        try:
            hot_wait_timeout = float(hot_wait_timeout) if hot_wait_timeout is not None else None
        except (TypeError, ValueError):
            hot_wait_timeout = 300.0
        routing: ExecutionExchangeRouting = {
            "provider_id": (str(get("exchange_provider")) if get("exchange_provider") else None),
            "channel_id": (str(get("channel_id")) if get("channel_id") else None),
            "wait_mode": cast(Any, wait_mode),
            "hot_wait_timeout": hot_wait_timeout if wait_mode != "disconnected" else None,
            "cold_persistence_policy": str(get("cold_persistence_policy", "persist") or "persist"),
            "handler": "posture",
            "meta": {"interaction_mode": mode},
        }
        return routing

    # ------------------------------------------------------------------ #
    # Live wait registry + respond facade
    # ------------------------------------------------------------------ #

    @staticmethod
    def _wait_keys(execution_id: str, interrupt_id: str, exchange_id: str | None) -> list[str]:
        keys = [f"{ execution_id }:{ interrupt_id }"]
        if exchange_id:
            keys.append(str(exchange_id))
        return keys

    def register_live_wait(
        self,
        *,
        execution: Any,
        interrupt_id: str,
        exchange_id: str | None = None,
        on_resolved: Any = None,
        on_closed: Any = None,
    ) -> str:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        entry = {
            "execution": execution,
            "execution_id": str(getattr(execution, "id", "")),
            "interrupt_id": str(interrupt_id),
            "exchange_id": str(exchange_id) if exchange_id else None,
            "loop": loop,
            "event": asyncio.Event(),
            "registered_at": time.time(),
            "on_resolved": on_resolved,
            "on_closed": on_closed,
        }
        keys = self._wait_keys(entry["execution_id"], entry["interrupt_id"], entry["exchange_id"])
        for key in keys:
            self._live_waits[key] = entry
        if callable(on_closed) and loop is not None:
            entry["close_watcher"] = loop.create_task(self._watch_live_execution(entry))
        return keys[0]

    async def _call_live_wait_hook(self, hook: Any) -> None:
        if not callable(hook):
            return
        result = hook()
        if inspect.isawaitable(result):
            await result

    async def _watch_live_execution(self, entry: dict[str, Any]) -> None:
        execution = entry["execution"]
        try:
            await execution._async_wait_for_close_snapshot()
            await self._call_live_wait_hook(entry.get("on_closed"))
        finally:
            self._unregister_execution_live_waits(entry["execution_id"])

    def _unregister_execution_live_waits(self, execution_id: str) -> None:
        for key, entry in list(self._live_waits.items()):
            if str(entry.get("execution_id") or "") == str(execution_id):
                self._live_waits.pop(key, None)

    def unregister_live_wait(self, *, execution_id: str, interrupt_id: str, exchange_id: str | None = None):
        for key in self._wait_keys(str(execution_id), str(interrupt_id), exchange_id):
            self._live_waits.pop(key, None)

    def get_live_wait(self, key: str) -> dict[str, Any] | None:
        return self._live_waits.get(str(key))

    def list_live_pending(self) -> list[ExecutionExchangeView]:
        views: list[ExecutionExchangeView] = []
        seen: set[int] = set()
        for entry in self._live_waits.values():
            if id(entry) in seen:
                continue
            seen.add(id(entry))
            execution = entry.get("execution")
            interrupt = None
            get_interrupt = getattr(execution, "get_interrupt", None)
            if callable(get_interrupt):
                interrupt = get_interrupt(entry["interrupt_id"])
            if isinstance(interrupt, dict) and interrupt.get("status") == "waiting":
                views.append(self.project_exchange(entry["execution_id"], interrupt))
        return views

    async def async_respond(
        self,
        exchange_key: str,
        payload: Any = None,
        *,
        actor: str | None = None,
        resume_request_id: str | None = None,
    ) -> ExecutionExchangeView:
        """Resolve a live pending exchange from any coroutine or thread.

        ``exchange_key`` accepts either an ``exchange_id`` or the
        ``"<execution_id>:<interrupt_id>"`` pair key. Cross-loop calls are
        marshalled onto the loop that owns the waiting execution.
        """
        entry = self._live_waits.get(str(exchange_key))
        if entry is None:
            raise KeyError(f"No live pending exchange found for '{ exchange_key }'.")
        execution = entry["execution"]
        interrupt_id = entry["interrupt_id"]
        owner_loop: asyncio.AbstractEventLoop | None = entry.get("loop")
        resolved_resume_request_id = resume_request_id or f"exchange:{ exchange_key }:response"

        async def _continue() -> None:
            await execution.async_continue_with(
                interrupt_id,
                payload,
                resume_request_id=resolved_resume_request_id,
                actor=actor,
            )
            await self._call_live_wait_hook(entry.get("on_resolved"))

        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if owner_loop is None or owner_loop is current_loop:
            await _continue()
        elif owner_loop.is_closed():
            raise RuntimeError(
                f"Can not respond to exchange '{ exchange_key }': its owning event loop is closed."
            )
        else:
            future = asyncio.run_coroutine_threadsafe(_continue(), owner_loop)
            if current_loop is None:
                future.result()
            else:
                await asyncio.wrap_future(future)

        event = entry.get("event")
        if isinstance(event, asyncio.Event):
            if owner_loop is None or owner_loop is current_loop:
                event.set()
            else:
                owner_loop.call_soon_threadsafe(event.set)
        interrupt = execution.get_interrupt(interrupt_id)
        view = self.project_exchange(entry["execution_id"], interrupt if isinstance(interrupt, dict) else {})
        self.unregister_live_wait(
            execution_id=entry["execution_id"],
            interrupt_id=interrupt_id,
            exchange_id=entry.get("exchange_id"),
        )
        return view

    async def async_abandon(self, exchange_key: str, *, reason: str = "abandoned") -> None:
        """Close a live exchange execution and run its owner cleanup hook."""

        entry = self._live_waits.get(str(exchange_key))
        if entry is None:
            raise KeyError(f"No live pending exchange found for '{ exchange_key }'.")
        execution = entry["execution"]
        owner_loop: asyncio.AbstractEventLoop | None = entry.get("loop")

        async def _close() -> None:
            await execution.async_close(reason=reason, pending_interrupts="cancel")
            await self._call_live_wait_hook(entry.get("on_closed"))
            self._unregister_execution_live_waits(entry["execution_id"])

        current_loop = asyncio.get_running_loop()
        if owner_loop is None or owner_loop is current_loop:
            await _close()
        elif owner_loop.is_closed():
            raise RuntimeError(
                f"Can not abandon exchange '{ exchange_key }': its owning event loop is closed."
            )
        else:
            await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_close(), owner_loop))

    # ------------------------------------------------------------------ #
    # Hot wait (connected mode driver, called by execution-handle owners)
    # ------------------------------------------------------------------ #

    def _resolve_interrupt_provider(
        self,
        execution: Any,
        interrupt: dict[str, Any],
    ) -> "ExecutionExchangeProvider | None":
        provider: ExecutionExchangeProvider | None = None
        get_resource = getattr(execution, "_get_runtime_resource", None)
        if callable(get_resource):
            candidate = get_resource("execution_exchange_provider", None)
            if callable(getattr(candidate, "publish_request", None)):
                provider = cast("ExecutionExchangeProvider", candidate)
        if provider is not None:
            return provider
        request = interrupt.get("external_wait_request")
        provider_id = request.get("provider_id") if isinstance(request, dict) else None
        return self.get_provider(provider_id)

    async def async_hot_wait(
        self,
        execution: Any,
        interrupt: dict[str, Any],
        *,
        timeout: float | None = None,
        actor: str | None = None,
    ) -> bool:
        """Drive one connected-mode wait; returns True when resolved.

        Provider ``await_response`` wins when available; otherwise waits for a
        host response delivered through ``async_respond``. Resolution always
        funnels through ``continue_with`` — there is no second resume path.
        """
        interrupt_id = str(interrupt.get("id", ""))
        execution_id = str(getattr(execution, "id", ""))
        request = interrupt.get("external_wait_request")
        request = dict(request) if isinstance(request, dict) else {}
        exchange_id = request.get("exchange_id")
        resolved_timeout = timeout
        if resolved_timeout is None:
            raw_timeout = request.get("hot_wait_timeout")
            resolved_timeout = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else None

        wait_key = self.register_live_wait(
            execution=execution,
            interrupt_id=interrupt_id,
            exchange_id=str(exchange_id) if exchange_id else None,
        )
        entry = self._live_waits.get(wait_key)
        provider = self._resolve_interrupt_provider(execution, interrupt)
        await_response = getattr(provider, "await_response", None) if provider is not None else None

        try:
            if callable(await_response):
                waiter = default_stage_call_bridge.as_async(await_response)(cast(ExecutionExchangeRequest, request))
                try:
                    if resolved_timeout is not None:
                        response = await asyncio.wait_for(waiter, timeout=resolved_timeout)
                    else:
                        response = await waiter
                except asyncio.TimeoutError:
                    return False
                current = execution.get_interrupt(interrupt_id)
                if isinstance(current, dict) and current.get("status") != "waiting":
                    return True
                if response is None:
                    return False
                await execution.async_continue_with(
                    interrupt_id,
                    response,
                    resume_request_id=f"exchange:{ wait_key }:provider",
                    actor=actor or "exchange_provider",
                )
                return True
            event = entry.get("event") if isinstance(entry, dict) else None
            if not isinstance(event, asyncio.Event):
                return False
            try:
                if resolved_timeout is not None:
                    await asyncio.wait_for(event.wait(), timeout=resolved_timeout)
                else:
                    await event.wait()
            except asyncio.TimeoutError:
                return False
            current = execution.get_interrupt(interrupt_id)
            return bool(isinstance(current, dict) and current.get("status") != "waiting")
        finally:
            self.unregister_live_wait(
                execution_id=execution_id,
                interrupt_id=interrupt_id,
                exchange_id=str(exchange_id) if exchange_id else None,
            )

    async def async_hot_wait_pending(
        self,
        execution: Any,
        *,
        timeout: float | None = None,
        actor: str | None = None,
    ) -> bool:
        """Hot-wait every pending interrupt on an execution; True if all resolved."""
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            pending = execution.get_pending_interrupts()
            if not pending:
                return True
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            interrupt = next(iter(pending.values()))
            resolved = await self.async_hot_wait(
                execution,
                interrupt,
                timeout=remaining,
                actor=actor,
            )
            if not resolved:
                return False

    # ------------------------------------------------------------------ #
    # Normalized projection
    # ------------------------------------------------------------------ #

    @staticmethod
    def project_exchange(execution_id: str, interrupt: dict[str, Any]) -> ExecutionExchangeView:
        request = interrupt.get("external_wait_request")
        request = dict(request) if isinstance(request, dict) else {}
        raw_status = str(interrupt.get("status") or "waiting")
        if raw_status == "waiting":
            status = "pending"
        elif raw_status == "resumed":
            status = "responded"
        elif raw_status == "cancelled":
            status = "expired" if "timeout" in str(interrupt.get("cancel_reason") or "") else "cancelled"
        else:
            status = "pending"
        audit_metadata = request.get("audit_metadata")
        audit_metadata = audit_metadata if isinstance(audit_metadata, dict) else {}
        payload = interrupt.get("payload")
        subject = ""
        if isinstance(payload, dict):
            subject = str(payload.get("subject") or "")
        if not subject:
            subject = str(audit_metadata.get("subject") or interrupt.get("exchange_kind") or interrupt.get("type") or "")
        view: ExecutionExchangeView = {
            "exchange_id": request.get("exchange_id"),
            "interrupt_id": str(interrupt.get("id", "")),
            "execution_id": str(execution_id),
            "kind": interrupt.get("exchange_kind"),
            "status": cast(Any, status),
            "subject": subject,
            "source": str(audit_metadata.get("source") or interrupt.get("source_flow_name") or ""),
            "payload": payload,
            "request": cast(ExecutionExchangeRequest, request),
            "response": interrupt.get("resume_value"),
            "actor_id": interrupt.get("resumed_by"),
            "created_at": interrupt.get("created_at"),
            "resolved_at": interrupt.get("resumed_at") or interrupt.get("cancelled_at"),
        }
        return view

    @classmethod
    def project_execution_exchanges(cls, execution: Any) -> list[ExecutionExchangeView]:
        execution_id = str(getattr(execution, "id", ""))
        get_interrupts = getattr(execution, "_get_interrupts", None)
        raw_interrupts = get_interrupts() if callable(get_interrupts) else {}
        interrupts: dict[Any, Any] = raw_interrupts if isinstance(raw_interrupts, dict) else {}
        views: list[ExecutionExchangeView] = []
        for interrupt in interrupts.values():
            if isinstance(interrupt, dict) and interrupt.get("exchange_kind"):
                views.append(cls.project_exchange(execution_id, interrupt))
        return views

    @classmethod
    def project_pending_exchanges(cls, execution: Any) -> list[ExecutionExchangeView]:
        return [view for view in cls.project_execution_exchanges(execution) if view.get("status") == "pending"]
