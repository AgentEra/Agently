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

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING, cast

from agently.utils import DataFormatter

if TYPE_CHECKING:
    from .execution import AgentExecution

_GUIDANCE_PREVIEW_CHARS = 800


async def interrupt(
    owner: "AgentExecution", content: str, *, author: str | None = None,
) -> dict[str, object]:
    """Supplement a future consumer, using the existing guidance/context owners."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Execution interrupt content must be non-empty text.")
    async with _guidance_lock(owner):
        receipt = _new_guidance_ref(owner, content, author=author, target="execution", meta={})
        owner.guidance_items.append(receipt)
        _record_guidance_diagnostic(owner, "received")
        await _emit_guidance(owner, "agent_execution.guidance.received", receipt)
        if owner._completed or owner._closed or owner._cancel_requested or owner._pause_boundary == "candidate_ready":
            receipt["status"] = "ignored"
            receipt["not_applied_reason"] = "no_future_producer_boundary"
            await _emit_guidance(owner, "agent_execution.guidance.ignored", receipt)
            return cast(dict[str, object], DataFormatter.sanitize(receipt))
        task = owner.task_record
        if task is not None:
            forwarded = await task.async_add_guidance(
                content, guidance_id=receipt["id"], author=author,
                meta={"execution_id": owner.id},
            )
            _merge_task_guidance(owner, receipt, forwarded)
            await _emit_guidance(owner, "agent_execution.guidance.forwarded", receipt)
            return cast(dict[str, object], DataFormatter.sanitize(receipt))
        owner.task_context.put(
            role="information", content=content, entry_id=receipt["id"], required=True,
            source_ref=receipt["id"], metadata={"source": "execution_interrupt", "author": author},
        )
        receipt["context_entry_id"] = receipt["id"]
        receipt["status"] = "inserted"
        owner._pending_guidance.append(receipt)
        await _emit_guidance(owner, "agent_execution.guidance.inserted", receipt)
        return cast(dict[str, object], DataFormatter.sanitize(receipt))


def record_interrupt_consumption(owner: "AgentExecution", package: Any, *, request_id: str) -> None:
    refs = {str(block.source_ref) for block in package.blocks}
    for receipt in owner.guidance_items:
        if receipt.get("status") == "inserted" and receipt.get("context_entry_id") in refs:
            receipt["status"] = "consumed"
            receipt["request_id"] = request_id
            _record_guidance_diagnostic(owner, "consumed")
    owner._pending_guidance = [item for item in owner._pending_guidance if item.get("status") != "consumed"]


async def add_guidance(
    owner: "AgentExecution",
    content: Any,
    *,
    author: str | None = None,
    target: Any = "task",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(content, str) and not content.strip():
        raise ValueError("AgentExecution guidance content must not be empty.")
    lock = _guidance_lock(owner)
    async with lock:
        guidance_ref = _new_guidance_ref(owner, content, author=author, target=target, meta=meta)
        owner.guidance_items.append(guidance_ref)
        _record_guidance_diagnostic(owner, "received")
        await _emit_guidance(owner, "agent_execution.guidance.received", guidance_ref)
        task = getattr(owner, "task_record", None)
        add_to_task = getattr(task, "async_add_guidance", None)
        if callable(add_to_task):
            forwarded = await cast(Callable[..., Awaitable[Any]], add_to_task)(
                guidance_ref["content"],
                guidance_id=guidance_ref["id"],
                author=guidance_ref.get("author"),
                target=guidance_ref.get("target", "task"),
                meta={
                    **dict(guidance_ref.get("meta") or {}),
                    "execution_id": owner.id,
                    "lineage": DataFormatter.sanitize(owner.lineage),
                },
            )
            _merge_task_guidance(owner, guidance_ref, forwarded)
            _record_guidance_diagnostic(owner, str(guidance_ref.get("status") or "forwarded"))
            return DataFormatter.sanitize(guidance_ref)
        if bool(getattr(owner, "_completed", False)):
            guidance_ref["status"] = "received_after_terminal"
            _record_guidance_diagnostic(owner, "received_after_terminal")
            await _emit_guidance(owner, "agent_execution.guidance.ignored", guidance_ref)
            return DataFormatter.sanitize(guidance_ref)
        guidance_ref["status"] = "queued"
        owner._pending_guidance.append(guidance_ref)
        _record_guidance_diagnostic(owner, "queued")
        await _emit_guidance(owner, "agent_execution.guidance.queued", guidance_ref)
        return DataFormatter.sanitize(guidance_ref)


async def insert_pending_guidance(owner: "AgentExecution") -> None:
    """Make queued execution guidance available to the next ContextPackage read."""
    async with _guidance_lock(owner):
        for receipt in owner._pending_guidance:
            if receipt.get("status") != "queued":
                continue
            owner.task_context.put(
                role="information", content=receipt["content"], entry_id=receipt["id"], required=True,
                source_ref=receipt["id"], metadata={"source": "execution_guidance", "author": receipt.get("author")},
            )
            receipt["context_entry_id"] = receipt["id"]
            receipt["status"] = "inserted"
            await _emit_guidance(owner, "agent_execution.guidance.inserted", receipt)


async def drain_pending_guidance_to_task(owner: "AgentExecution", task: Any) -> list[dict[str, Any]]:
    pending = [item for item in getattr(owner, "_pending_guidance", []) or [] if isinstance(item, dict)]
    if not pending:
        return []
    add_to_task = getattr(task, "async_add_guidance", None)
    if not callable(add_to_task):
        return []
    forwarded_items: list[dict[str, Any]] = []
    for guidance_ref in pending:
        forwarded = await cast(Callable[..., Awaitable[Any]], add_to_task)(
            guidance_ref.get("content"),
            guidance_id=str(guidance_ref.get("id") or ""),
            author=guidance_ref.get("author"),
            target=guidance_ref.get("target", "task"),
            meta={
                **dict(guidance_ref.get("meta") or {}),
                "execution_id": owner.id,
                "lineage": DataFormatter.sanitize(owner.lineage),
            },
        )
        _merge_task_guidance(owner, guidance_ref, forwarded)
        forwarded_items.append(DataFormatter.sanitize(guidance_ref))
        await _emit_guidance(owner, "agent_execution.guidance.forwarded", guidance_ref)
    owner._pending_guidance = [
        item
        for item in getattr(owner, "_pending_guidance", []) or []
        if not isinstance(item, dict) or item.get("id") not in {entry.get("id") for entry in forwarded_items}
    ]
    _record_guidance_diagnostic(owner, "forwarded", count=len(forwarded_items))
    return forwarded_items


async def mark_pending_guidance_not_applied(owner: "AgentExecution", *, reason: str) -> None:
    pending = [item for item in getattr(owner, "_pending_guidance", []) or [] if isinstance(item, dict)]
    if not pending:
        return
    for guidance_ref in pending:
        guidance_ref["status"] = "not_applied"
        guidance_ref["not_applied_reason"] = reason
        await _emit_guidance(owner, "agent_execution.guidance.ignored", guidance_ref)
    _record_guidance_diagnostic(owner, "not_applied", count=len(pending))
    owner._pending_guidance = []


def _new_guidance_ref(
    owner: "AgentExecution",
    content: Any,
    *,
    author: str | None,
    target: Any,
    meta: dict[str, Any] | None,
) -> dict[str, Any]:
    owner._guidance_sequence = int(getattr(owner, "_guidance_sequence", 0)) + 1
    return {
        "id": f"guidance-{uuid.uuid4().hex}",
        "kind": "guidance",
        "sequence": owner._guidance_sequence,
        "content": DataFormatter.sanitize(content),
        "content_preview": _guidance_preview(content),
        "author": str(author or "").strip() or None,
        "target": DataFormatter.sanitize(target or "task"),
        "status": "received",
        "received_at": time.time(),
        "meta": DataFormatter.sanitize(meta or {}),
    }


def _merge_task_guidance(owner: "AgentExecution", guidance_ref: dict[str, Any], task_guidance: Any) -> None:
    if not isinstance(task_guidance, dict):
        guidance_ref["status"] = "forwarded"
        return
    for key in ("status", "task_workspace_ref", "checkpoint_ref", "applied_at", "applied_iteration", "applied_boundary"):
        if key in task_guidance:
            guidance_ref[key] = DataFormatter.sanitize(task_guidance[key])
    task_id = getattr(getattr(owner, "task_record", None), "id", None)
    if task_id:
        guidance_ref["task_id"] = str(task_id)
    task_workspace_ref = guidance_ref.get("task_workspace_ref")
    if isinstance(task_workspace_ref, dict):
        _append_record_ref(owner, "guidance", task_workspace_ref)


def _append_record_ref(owner: "AgentExecution", key: str, ref: dict[str, Any]) -> None:
    ref_id = ref.get("id")
    if not ref_id:
        return
    refs = owner.record_refs.setdefault(key, [])
    if isinstance(refs, list) and ref_id not in refs:
        refs.append(ref_id)


def _guidance_lock(owner: "AgentExecution") -> asyncio.Lock:
    lock = getattr(owner, "_guidance_lock", None)
    if lock is None or not hasattr(lock, "acquire"):
        lock = asyncio.Lock()
        owner._guidance_lock = lock
    return lock


def _guidance_preview(content: Any) -> str:
    text = str(content if content is not None else "").strip()
    if len(text) <= _GUIDANCE_PREVIEW_CHARS:
        return text
    return text[: max(0, _GUIDANCE_PREVIEW_CHARS - 16)].rstrip() + " [truncated]"


def _record_guidance_diagnostic(owner: "AgentExecution", status: str, *, count: int = 1) -> None:
    diagnostics = owner.diagnostics.setdefault("guidance", {})
    if not isinstance(diagnostics, dict):
        diagnostics = {}
        owner.diagnostics["guidance"] = diagnostics
    key = str(status or "received")
    diagnostics[key] = int(diagnostics.get(key) or 0) + count
    diagnostics["total"] = len([item for item in getattr(owner, "guidance_items", []) if isinstance(item, dict)])


async def _emit_guidance(owner: "AgentExecution", path: str, guidance_ref: dict[str, Any]) -> None:
    await owner.emit_stream(
        path,
        {
            "execution_id": owner.id,
            "task_id": guidance_ref.get("task_id"),
            "guidance_id": guidance_ref.get("id"),
            "kind": "guidance",
            "status": guidance_ref.get("status"),
            "target": guidance_ref.get("target"),
            "content_preview": guidance_ref.get("content_preview"),
            "task_workspace_ref": guidance_ref.get("task_workspace_ref"),
        },
        route=str(owner.route_info.get("selected_route") or ""),
        source="agent_execution",
        task_id=str(guidance_ref.get("task_id") or "") or None,
        meta={
            "stream_kind": "guidance",
            "guidance_status": str(guidance_ref.get("status") or ""),
            "guidance_id": guidance_ref.get("id"),
        },
    )


__all__ = ["add_guidance", "drain_pending_guidance_to_task", "mark_pending_guidance_not_applied"]
