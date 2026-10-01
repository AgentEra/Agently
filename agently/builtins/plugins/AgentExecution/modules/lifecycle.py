"""Owned run settlement and explicit control for every bundled producer."""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from agently.core.application.AgentExecution.Control import AgentExecutionPaused
from agently.core.orchestration.TriggerFlow import TriggerFlow
from agently.types.data.agent_execution import AgentExecutionControlResult
from agently.types.trigger_flow import TriggerFlowRuntimeData

if TYPE_CHECKING:
    from .execution import AgentExecution


def _timeout(value: float | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not math.isfinite(value) or value < 0
    ):
        raise ValueError("Execution control timeout must be finite and non-negative.")


def control_result(owner: AgentExecution) -> AgentExecutionControlResult:
    result = AgentExecutionControlResult(
        execution_id=owner.id,
        status=str(owner.status),
        closed=owner._closed,
    )
    if owner._pause_requested or owner._pause_boundary is not None:
        result["pause_requested"] = owner._pause_requested
        result["boundary"] = owner._pause_boundary
    return result


def _completion_waiter(completion: concurrent.futures.Future[Any]) -> asyncio.Future[Any]:
    waiter = asyncio.wrap_future(completion)

    def observe(future: asyncio.Future[Any]) -> None:
        # Timed-out shielded readers no longer await this local projection.
        # Retrieve its exception; the canonical future still raises it to every
        # subsequent reader and control call.
        if not future.cancelled():
            future.exception()

    waiter.add_done_callback(observe)
    return waiter


def _register_owned(owner: AgentExecution, create_run: Callable[[], Awaitable[Any]]) -> None:
    completion: concurrent.futures.Future[Any] = concurrent.futures.Future()
    owner._run_completion = completion
    owner._run_loop = asyncio.get_running_loop()

    async def produce() -> Any:
        return await create_run()

    task = owner._run_loop.create_task(produce())
    owner._run_task = task

    def settled(finished: asyncio.Task[Any]) -> None:
        try:
            value = finished.result()
        except BaseException as error:
            if not owner._started and isinstance(error, asyncio.CancelledError):
                owner._started = True
                owner._completed = True
                owner.status = "cancelled"
                owner._error = error
            completion.set_exception(error)
        else:
            completion.set_result(value)

    task.add_done_callback(settled)


async def run_owned(
    owner: AgentExecution,
    create_run: Callable[[], Awaitable[Any]],
) -> Any:
    # No await between admission and registration: same-loop readers cannot
    # create duplicate work. The concurrent Future carries settlement across
    # sync bridge loops without making a caller's subsequent work our resource.
    with owner._run_admission_lock:
        if owner.status == "paused":
            raise AgentExecutionPaused(owner.id, owner._pause_boundary or "unknown")
        if owner._run_completion is None:
            if owner._closed or owner._cancel_requested:
                if owner._error is not None:
                    raise owner._error
                raise RuntimeError("AgentExecution is closed and cannot start.")
            _register_owned(owner, create_run)
        shared = owner._run_completion
        assert shared is not None
    try:
        return await asyncio.shield(_completion_waiter(shared))
    except asyncio.CancelledError:
        # Preserve cancellation of the consuming run while ensuring that
        # caller cancellation cannot abandon owned provider/finally work.
        await cancel(owner, reason="run_consumer_cancelled", timeout=None)
        raise


async def _wait_settled(owner: AgentExecution, timeout: float | None) -> None:
    completion = owner._run_completion
    if completion is None:
        return
    waiter = _completion_waiter(completion)
    # A waiter timeout must not cancel the shared future or interrupt cleanup.
    shielded = asyncio.shield(waiter)
    try:
        if timeout is None:
            await shielded
        else:
            await asyncio.wait_for(shielded, timeout=timeout)
    except asyncio.CancelledError:
        if not completion.done() or not owner._cancel_requested:
            raise


async def cancel(
    owner: AgentExecution, *, reason: str, timeout: float | None,
) -> AgentExecutionControlResult:
    _timeout(timeout)
    if owner._run_task is asyncio.current_task():
        raise RuntimeError("An execution cannot await its own cancellation settlement.")
    deadline = time.monotonic() + timeout if timeout is not None else None
    if owner.status == "paused":
        # Finish the run which exposed the wait before replacing its settlement
        # handle. Cancellation of a persisted wait is itself owned work.
        try:
            await _wait_settled(owner, timeout)
        except AgentExecutionPaused:
            pass
        with owner._run_admission_lock:
            if owner.status == "paused":
                owner._cancel_requested = True
                owner._closing = True
                owner.status = "cancelling"
                _register_owned(owner, lambda: _cancel_pause(owner, reason))
        remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
        await _wait_settled(owner, remaining)
        return control_result(owner)
    with owner._run_admission_lock:
        if owner._completed and (
            owner._run_completion is None or owner._run_completion.done()
        ):
            return control_result(owner)
        first_request = not owner._cancel_requested
        owner._cancel_requested = True
        owner._closing = True
        task = owner._run_task
        loop = owner._run_loop
        if task is None:
            owner._started = True
            owner._error = asyncio.CancelledError(reason)
            owner.status = "cancelled"
            owner._completed = True
    if task is None:
        await owner.close_streams()
    elif first_request and loop is not None:
        # Only the first cancel request delivers cancellation. Repeated callers
        # join the same settlement rather than cancelling a provider's finally.
        loop.call_soon_threadsafe(task.cancel, reason)
    await _wait_settled(owner, timeout)
    return control_result(owner)


async def _cancel_pause(owner: AgentExecution, reason: str) -> None:
    try:
        if owner._pause_flow is not None:
            await owner._pause_flow.async_close(reason=reason, pending_interrupts="cancel")
        from .route_execution import _finalize_terminal_execution

        owner._pause_requested = False
        owner.status = "cancelled"
        owner._error = asyncio.CancelledError(reason)
        await _finalize_terminal_execution(owner, terminal_status="cancelled")
        await owner.close_streams()
    except BaseException as error:
        owner._error = error
        owner.status = "error"
        raise
    finally:
        owner._completed = True
    raise asyncio.CancelledError(reason)


async def close(
    owner: AgentExecution, *, reason: str, timeout: float | None,
    pending: Literal["error", "cancel"],
) -> AgentExecutionControlResult:
    _timeout(timeout)
    if pending not in {"error", "cancel"}:
        raise ValueError("Execution close pending policy must be 'error' or 'cancel'.")
    if owner._run_task is asyncio.current_task():
        raise RuntimeError("An execution cannot await its own close settlement.")
    with owner._run_admission_lock:
        if owner._closed:
            return control_result(owner)
        if owner.status in {"paused", "waiting"} and pending == "error":
            raise RuntimeError("Execution has unresolved waits; resume or explicitly cancel them.")
        owner._closing = True
        if owner._run_completion is None and not owner._started:
            owner._started = True
            owner._completed = True
            owner._closed = True
            owner.status = "closed"
            owner._error = RuntimeError("AgentExecution was closed before production.")
    if pending == "cancel" and not owner._completed:
        await cancel(owner, reason=reason, timeout=timeout)
    else:
        await _wait_settled(owner, timeout)
    await owner.close_streams()
    owner._closed = True
    return control_result(owner)


async def _pause_node(data: TriggerFlowRuntimeData) -> object:
    owner = cast("AgentExecution", data.require_resource("agent_execution"))
    return await data.async_pause_for(
        type="agent_execution_pause",
        payload={"execution_id": owner.id, "boundary": owner._pause_boundary},
        interrupt_id="execution-pause",
        resume_to="next",
    )


async def _continue_node(data: TriggerFlowRuntimeData) -> None:
    owner = cast("AgentExecution", data.require_resource("agent_execution"))
    continuation = owner._paused_continuation
    if continuation is None:
        raise RuntimeError("Execution continuation dependency was not rebound.")
    owner._continued_result = await continuation()


def pause_flow() -> TriggerFlow[Any, Any, Any]:
    flow: TriggerFlow[Any, Any, Any] = TriggerFlow(name="agent-execution-safe-pause")
    flow.to(_pause_node).to(_continue_node)
    return flow


async def pause_at(
    owner: AgentExecution,
    boundary: Literal["before_production", "candidate_ready", "long_task_step"],
    continuation: Callable[[], Awaitable[tuple[str, object]]],
) -> None:
    if not owner._pause_requested:
        return
    owner._pause_boundary = boundary
    owner._paused_continuation = continuation
    owner._pause_flow = pause_flow().create_execution(
        auto_close=False,
        record_store=owner.record_store,
        runtime_resources={"agent_execution": owner, "record_store": owner.record_store},
        parent_run_context=owner.agent_execution_run_context,
        intervention_mode=None,
    )
    await owner._pause_flow.async_start(None)
    owner.status = "paused"
    raise AgentExecutionPaused(owner.id, boundary)


async def pause(owner: AgentExecution) -> AgentExecutionControlResult:
    with owner._run_admission_lock:
        if owner._closed or owner._cancel_requested or owner._completed:
            raise RuntimeError("Only an unstarted or active execution can be paused.")
        owner._pause_requested = True
    return control_result(owner)


async def resume_route(owner: AgentExecution) -> tuple[str, object]:
    flow = owner._pause_flow
    if flow is None:
        raise RuntimeError("Execution has no retained pause continuation.")
    try:
        await flow.async_continue_with("execution-pause", None)
        await flow.async_close(reason="agent_execution_resumed")
    except AgentExecutionPaused:
        # A resumed producer may reach a later safe boundary. It owns a new
        # flow; closing the already-consumed old one must not erase that wait.
        if not flow.is_closed():
            await flow.async_close(reason="agent_execution_paused_again", pending_interrupts="cancel")
        raise
    except BaseException:
        if not flow.is_closed():
            await flow.async_close(reason="agent_execution_resume_failed", pending_interrupts="cancel")
        raise
    result = owner._continued_result
    if result is None:
        raise RuntimeError("Execution resumed without a producer result.")
    owner._pause_flow = None
    owner._pause_boundary = None
    owner._paused_continuation = None
    owner._continued_result = None
    return result


async def resume(owner: AgentExecution) -> object:
    with owner._run_admission_lock:
        if owner.status != "paused" or owner._closed or owner._cancel_requested:
            raise RuntimeError("Execution is not paused; completed results cannot resume production.")
        if owner._run_completion is not None and not owner._run_completion.done():
            raise RuntimeError("Pause has not settled yet; await the current run before resuming.")
        owner._pause_requested = False
        owner._resuming = True
        owner.status = "resuming"
        owner._run_completion = None
        owner._run_task = None
    return await owner.async_run()


async def release_owned_resources(owner: AgentExecution) -> None:
    """Release only execution scopes whose exact owner belongs to this run."""
    from agently.base import execution_resource
    if owner._resource_release_error is not None:
        raise owner._resource_release_error
    owner_ids = {owner.id}
    if owner.task_record is not None:
        owner_ids.add(owner.task_record.id)
    try:
        for identity in owner_ids:
            owned = [execution_resource.inspect(key) for key in owner.execution_context._resource_handle_ids]
            if any(handle is not None and handle.get("scope") == "execution"
                   and handle.get("owner_id") == identity and handle.get("status") != "released"
                   for handle in owned):
                await execution_resource.async_release_scope("execution", identity)
    except Exception as error:
        owner._resource_release_error = error
        raise
