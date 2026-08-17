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
import json
import sys
import uuid
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import PolicyApprovalDecision, PolicyApprovalHandler, PolicyApprovalRequest
from agently.utils.DataGuardian import _copy_public
from .AccessControlPolicy import access_policy_auto_allow, merge_access_control_policy

if TYPE_CHECKING:
    from agently.core.runtime.EventCenter import EventCenter
    from agently.utils import Settings


class PolicyApprovalManager:
    def __init__(
        self,
        *,
        settings: "Settings",
        event_center: "EventCenter",
    ):
        self.settings = settings
        self.event_center = event_center
        self._handlers: dict[str, PolicyApprovalHandler] = {}
        self.register_handler("input_timeout_fail", self._input_timeout_fail, replace=True)
        self.register_handler("fail_closed", self._fail_closed, replace=True)
        self.register_handler("auto_approve", self._auto_approve, replace=True)
        self.register_handler("input", self._input, replace=True)

        self.resolve = default_stage_call_bridge.as_sync(self.async_resolve)
        self.gate = default_stage_call_bridge.as_sync(self.async_gate)

    def register_handler(
        self,
        name: str,
        handler: PolicyApprovalHandler,
        *,
        replace: bool = False,
    ) -> "PolicyApprovalManager":
        handler_name = str(name or "").strip()
        if not handler_name:
            raise ValueError("Policy approval handler name cannot be empty.")
        if not callable(handler):
            raise TypeError("Policy approval handler must be callable.")
        if handler_name in self._handlers and not replace:
            raise ValueError(f"Policy approval handler '{ handler_name }' is already registered.")
        self._handlers[handler_name] = handler
        return self

    def unregister_handler(self, name: str) -> bool:
        handler_name = str(name or "").strip()
        if handler_name in {"input_timeout_fail", "fail_closed", "auto_approve", "input"}:
            return False
        return self._handlers.pop(handler_name, None) is not None

    def list_handlers(self) -> list[str]:
        return sorted(self._handlers)

    def set_default_handler(self, name: str) -> "PolicyApprovalManager":
        handler_name = str(name or "").strip()
        if handler_name not in self._handlers:
            raise ValueError(f"Policy approval handler '{ handler_name }' is not registered.")
        self.settings.set("policy_approval.handler", handler_name)
        return self

    @staticmethod
    def normalize_request(request: PolicyApprovalRequest | dict[str, Any]) -> PolicyApprovalRequest:
        normalized = cast(PolicyApprovalRequest, dict(request or {}))
        if not str(normalized.get("request_id", "")).strip():
            normalized["request_id"] = uuid.uuid4().hex
        normalized["source"] = str(normalized.get("source") or "runtime")
        normalized["capability"] = str(normalized.get("capability") or "")
        normalized["subject"] = str(normalized.get("subject") or normalized.get("capability") or "")
        normalized["risk"] = str(normalized.get("risk") or "")
        normalized["payload"] = dict(normalized.get("payload") or {})
        normalized["policy"] = dict(normalized.get("policy") or {})
        normalized["lineage"] = dict(normalized.get("lineage") or {})
        normalized["meta"] = dict(normalized.get("meta") or {})
        return normalized

    @staticmethod
    def normalize_decision(
        decision: PolicyApprovalDecision | bool | dict[str, Any] | None,
        *,
        handler: str = "",
    ) -> PolicyApprovalDecision:
        if isinstance(decision, bool):
            return {
                "status": "approved" if decision else "denied",
                "approved": decision,
                "reason": "approved" if decision else "denied",
                "handler": handler,
            }
        raw = dict(decision or {})
        status = str(raw.get("status") or "").strip().lower()
        if status not in {"approved", "denied", "pending"}:
            if raw.get("approved") is True:
                status = "approved"
            elif raw.get("approved") is False and raw.get("reason"):
                status = "denied"
            else:
                status = "pending"
        normalized: PolicyApprovalDecision = {
            "status": cast(Any, status),
            "approved": status == "approved",
            "reason": str(raw.get("reason") or status),
            "handler": str(raw.get("handler") or handler),
        }
        policy_override = raw.get("policy_override")
        if isinstance(policy_override, dict):
            normalized["policy_override"] = dict(policy_override)
        if raw.get("wait_strategy") is not None:
            normalized["wait_strategy"] = str(raw.get("wait_strategy"))
        meta = raw.get("meta")
        if isinstance(meta, dict):
            normalized["meta"] = dict(meta)
        return normalized

    def _resolve_handler_name(self, handler: str | None = None) -> str:
        return str(
            handler
            or self.settings.get("policy_approval.handler", "input_timeout_fail")
            or "input_timeout_fail"
        ).strip()

    async def _emit(self, event_type: str, request: PolicyApprovalRequest, decision: PolicyApprovalDecision | None = None):
        payload: dict[str, Any] = {"request": _copy_public(request)}
        if decision is not None:
            payload["decision"] = _copy_public(decision)
        await self.event_center.async_emit(
            {
                "event_type": event_type,
                "source": "PolicyApprovalManager",
                "level": "WARNING" if event_type.endswith((".pending", ".denied")) else "INFO",
                "message": f"Policy approval { event_type.rsplit('.', 1)[-1] }.",
                "payload": payload,
            }
        )

    async def async_resolve(
        self,
        request: PolicyApprovalRequest | dict[str, Any],
        *,
        handler: str | None = None,
    ) -> PolicyApprovalDecision:
        normalized_request = self.normalize_request(request)
        normalized_request["policy"] = merge_access_control_policy(
            normalized_request.get("policy", {}),
            self.settings,
        )
        handler_name = self._resolve_handler_name(handler)
        await self._emit("policy.approval.requested", normalized_request)
        if access_policy_auto_allow(normalized_request.get("policy")):
            decision = self.normalize_decision(
                {
                    "status": "approved",
                    "approved": True,
                    "reason": (
                        "Auto-allowed by host access_control_policy.auto_allow "
                        f"for '{ normalized_request.get('subject') or normalized_request.get('capability') }'."
                    ),
                    "meta": {"auto_allow": True},
                },
                handler="access_control_policy.auto_allow",
            )
            await self._emit("policy.approval.approved", normalized_request, decision)
            return decision
        selected = self._handlers.get(handler_name)
        if selected is None:
            decision = self.normalize_decision(
                {
                    "status": "pending",
                    "reason": f"Policy approval handler is not registered: { handler_name }",
                    "wait_strategy": "fail_closed",
                },
                handler=handler_name,
            )
        else:
            result = await default_stage_call_bridge.as_async(selected)(normalized_request)
            decision = self.normalize_decision(result, handler=handler_name)
        await self._emit(f"policy.approval.{ decision.get('status', 'pending') }", normalized_request, decision)
        return decision

    async def async_gate(
        self,
        runtime_data: Any,
        request: PolicyApprovalRequest | dict[str, Any],
        *,
        handler: str | None = None,
        resume_to: Any = "self",
        interrupt_id: str | None = None,
        channel_id: str | None = None,
        provider_id: str | None = None,
        wait_mode: str | None = None,
        hot_wait_timeout: float | None = None,
        cold_persistence_policy: str | None = None,
        request_payload_schema: dict[str, Any] | None = None,
        response_payload_schema: dict[str, Any] | None = None,
        audit_metadata: dict[str, Any] | None = None,
        settings: Any = None,
    ):
        normalized_request = self.normalize_request(request)
        gate_settings = settings if settings is not None else getattr(runtime_data, "settings", None)
        normalized_request["policy"] = merge_access_control_policy(
            normalized_request.get("policy", {}),
            gate_settings,
        )
        if getattr(runtime_data, "is_resume", False):
            decision = self._claim_resumed_gate_decision(runtime_data, normalized_request, interrupt_id)
            if decision is not None:
                await self._emit("policy.approval.resumed", normalized_request, decision)
                return decision

        decision = await self.async_resolve(normalized_request, handler=handler)
        if decision.get("status") != "pending":
            return decision
        if channel_id is None and provider_id is None and wait_mode is None:
            from agently.base import execution_exchange

            routing = await execution_exchange.async_route(
                {
                    "exchange_kind": "approval",
                    "channel_id": channel_id,
                    "provider_id": provider_id,
                    "audit_metadata": {
                        "source": str(normalized_request.get("source") or ""),
                        "subject": str(normalized_request.get("subject") or ""),
                    },
                },
                settings=gate_settings,
            )
            if routing:
                channel_id = routing.get("channel_id")
                provider_id = routing.get("provider_id")
                wait_mode = routing.get("wait_mode")
                if hot_wait_timeout is None:
                    hot_wait_timeout = routing.get("hot_wait_timeout")
                if cold_persistence_policy is None:
                    cold_persistence_policy = routing.get("cold_persistence_policy")
        resolved_audit_metadata = {
            "source": str(normalized_request.get("source") or ""),
            "subject": str(normalized_request.get("subject") or ""),
            "risk": str(normalized_request.get("risk") or ""),
        }
        if audit_metadata:
            resolved_audit_metadata.update(dict(audit_metadata))
        return await runtime_data.async_pause_for(
            type="policy_approval",
            exchange_kind="approval",
            payload={"request": _copy_public(normalized_request), "decision": _copy_public(decision)},
            interrupt_id=interrupt_id or f"policy:{ normalized_request.get('request_id', '') }",
            resume_to=resume_to,
            channel_id=channel_id,
            provider_id=provider_id,
            wait_mode=wait_mode or "disconnected",
            hot_wait_timeout=hot_wait_timeout,
            cold_persistence_policy=cold_persistence_policy or "persist",
            request_payload_schema=request_payload_schema,
            response_payload_schema=response_payload_schema,
            audit_metadata=resolved_audit_metadata,
        )

    @staticmethod
    def _request_identity(request: Any) -> str | None:
        """Stable identity of a gate request across resume replays.

        ``request_id`` is regenerated on every replay, so identity is the
        remaining public request content.
        """
        if not isinstance(request, dict):
            return None
        try:
            return json.dumps(
                {
                    key: request.get(key)
                    for key in ("source", "capability", "subject", "risk", "payload", "policy", "lineage")
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            return None

    def _claim_resumed_gate_decision(
        self,
        runtime_data: Any,
        normalized_request: PolicyApprovalRequest,
        interrupt_id: str | None,
    ) -> PolicyApprovalDecision | None:
        """Claim the resumed interrupt that belongs to THIS gate, if any.

        A resumed chunk replays every gate it contains, and the resume context
        on the signal only describes the interrupt that triggered the replay.
        Consuming it blindly lets a later, different gate (for example a
        permission gate following a plan-approval gate) treat someone else's
        approval as its own. Instead each gate claims its own resumed interrupt
        from the execution's interrupt ledger — matched by explicit
        ``interrupt_id`` when the caller pinned one, otherwise by stable
        request identity — in creation order, at most once per chunk
        invocation. ``None`` means "no resume belongs to this gate": the gate
        falls through to its normal resolve/pause path.
        """
        execution = getattr(runtime_data, "execution", None)
        interrupts: dict[str, Any] = {}
        if execution is not None:
            get_interrupts = getattr(execution, "_get_interrupts", None)
            if callable(get_interrupts):
                try:
                    found = get_interrupts()
                    interrupts = dict(found) if isinstance(found, dict) else {}
                except Exception:
                    interrupts = {}
        if not interrupts:
            # No interrupt ledger is reachable (legacy snapshot or bare runtime
            # data): keep the historical behavior of consuming the triggering
            # resume payload.
            resume = getattr(runtime_data, "resume", None)
            if resume is None:
                return None
            return self.normalize_decision(getattr(resume, "value", None), handler="triggerflow_resume")
        claimed_ids = getattr(runtime_data, "_policy_gate_claimed_interrupt_ids", None)
        if not isinstance(claimed_ids, set):
            claimed_ids = set()
            try:
                setattr(runtime_data, "_policy_gate_claimed_interrupt_ids", claimed_ids)
            except Exception:
                pass
        identity = self._request_identity(normalized_request)
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for candidate_id, candidate in interrupts.items():
            if not isinstance(candidate, dict):
                continue
            if candidate.get("type") != "policy_approval":
                continue
            if candidate.get("status") != "resumed":
                continue
            if str(candidate_id) in claimed_ids:
                continue
            if interrupt_id is not None:
                if str(candidate_id) != str(interrupt_id):
                    continue
            else:
                payload = candidate.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                candidate_identity = self._request_identity(payload.get("request"))
                if candidate_identity is None or candidate_identity != identity:
                    continue
            created_at = candidate.get("created_at")
            created_at = float(created_at) if isinstance(created_at, (int, float)) else 0.0
            candidates.append((created_at, str(candidate_id), candidate))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, claimed_id, claimed = candidates[0]
        claimed_ids.add(claimed_id)
        return self.normalize_decision(claimed.get("resume_value"), handler="triggerflow_resume")

    def _fail_closed(self, request: PolicyApprovalRequest) -> PolicyApprovalDecision:
        return {
            "status": "pending",
            "approved": False,
            "wait_strategy": "fail_closed",
            "reason": f"Policy approval required for '{ request.get('subject') or request.get('capability') }'.",
        }

    def _auto_approve(self, request: PolicyApprovalRequest) -> PolicyApprovalDecision:
        return {
            "status": "approved",
            "approved": True,
            "reason": f"Auto-approved '{ request.get('subject') or request.get('capability') }'.",
        }

    async def _input_timeout_fail(self, request: PolicyApprovalRequest) -> PolicyApprovalDecision:
        subject = request.get("subject") or request.get("capability")
        policy = request.get("policy", {})
        timeout = policy.get("input_timeout_seconds", self.settings.get("policy_approval.input_timeout_seconds", 30))
        timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) and timeout >= 0 else 30.0
        if not sys.stdin or not sys.stdin.isatty():
            return {
                "status": "denied",
                "approved": False,
                "wait_strategy": "input_timeout_fail",
                "reason": f"Policy approval input is unavailable for '{ subject }' in a non-interactive environment.",
            }
        prompt = f"Approve '{ subject }'? This will fail after { timeout_seconds:g}s. [y/N] "
        try:
            answer = await asyncio.wait_for(asyncio.to_thread(input, prompt), timeout=timeout_seconds)
        except (asyncio.TimeoutError, EOFError):
            return {
                "status": "denied",
                "approved": False,
                "wait_strategy": "input_timeout_fail",
                "reason": f"Policy approval timed out for '{ subject }'.",
            }
        approved = str(answer).strip().lower() in {"y", "yes"}
        return {
            "status": "approved" if approved else "denied",
            "approved": approved,
            "wait_strategy": "input_timeout_fail",
            "reason": "Approved by input()." if approved else "Denied by input().",
        }

    def _input(self, request: PolicyApprovalRequest) -> PolicyApprovalDecision:
        prompt = f"Approve '{ request.get('subject') or request.get('capability') }'? [y/N] "
        answer = input(prompt).strip().lower()
        approved = answer in {"y", "yes"}
        return {
            "status": "approved" if approved else "denied",
            "approved": approved,
            "wait_strategy": "input",
            "reason": "Approved by input()." if approved else "Denied by input().",
        }
