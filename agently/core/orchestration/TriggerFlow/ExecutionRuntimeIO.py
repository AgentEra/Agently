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


import asyncio
import threading
import warnings
from typing import Any, AsyncGenerator, Generator, TYPE_CHECKING

from agently.types.data import EMPTY
from agently.types.trigger_flow import (
    TriggerFlowInterventionEvent,
    TriggerFlowInterruptEvent,
)
from .Control import TRIGGER_FLOW_LIFECYCLE_CLOSED
from .ExecutionState import COMPAT_FINAL_RESULT_KEY

if TYPE_CHECKING:
    from .Execution import TriggerFlowExecution


class TriggerFlowExecutionRuntimeIO:
    def __init__(self, execution: "TriggerFlowExecution[Any, Any, Any]"):
        self._execution = execution
        self.reset_stream_start_state()

    def reset_stream_start_state(self) -> None:
        self._stream_start_lock = threading.Lock()
        self._stream_start_claimed = False
        self._async_stream_start_task: asyncio.Task[Any] | None = None
        self._sync_stream_driver: threading.Thread | None = None

    def compat_result_exists(self):
        execution = self._execution
        if execution._get_state(COMPAT_FINAL_RESULT_KEY, EMPTY, inherit=False) is not EMPTY:
            return True
        return execution._system_runtime_data.get("result") is not EMPTY

    def get_compat_result(self):
        execution = self._execution
        compat_result = execution._get_state(COMPAT_FINAL_RESULT_KEY, EMPTY, inherit=False)
        if compat_result is not EMPTY:
            return compat_result
        result = execution._system_runtime_data.get("result")
        return None if result is EMPTY else result

    def build_close_snapshot(self):
        execution = self._execution
        snapshot = dict(execution._runtime_state_snapshot())
        compat_result = self.get_compat_result()
        if compat_result is not None and COMPAT_FINAL_RESULT_KEY not in snapshot:
            snapshot[COMPAT_FINAL_RESULT_KEY] = compat_result
        return snapshot

    def resolve_compat_result_or_snapshot(self):
        execution = self._execution
        compat_result = self.get_compat_result()
        if compat_result is not None:
            return compat_result
        if execution._close_result is not None:
            return execution._close_result
        return self.build_close_snapshot()

    async def async_wait_for_compat_result_or_close(self, *, timeout: float | None = None):
        execution = self._execution
        if self.compat_result_exists():
            return self.resolve_compat_result_or_snapshot()
        if execution._closed_event.is_set():
            return self.resolve_compat_result_or_snapshot()

        result_ready = execution._system_runtime_data.get("result_ready")
        waiters: list[asyncio.Task[Any]] = []
        if isinstance(result_ready, asyncio.Event):
            waiters.append(asyncio.create_task(result_ready.wait()))
        waiters.append(asyncio.create_task(execution._closed_event.wait()))

        try:
            if timeout is None:
                done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            else:
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

        if not done:
            warnings.warn(
                f"Can not get the compatibility result of trigger flow { execution.id } because it took too long and timeout.\n"
                "Use close()/async_close(), reduce auto_close_timeout, or pass timeout=None to wait forever."
                f"Timeout: { timeout }"
            )
            return None
        return self.resolve_compat_result_or_snapshot()

    async def async_wait_for_close_snapshot(self, *, timeout: float | None = None):
        execution = self._execution
        if execution._closed_event.is_set():
            return execution._close_result if execution._close_result is not None else self.build_close_snapshot()
        try:
            if timeout is None:
                await execution._closed_event.wait()
            else:
                await asyncio.wait_for(execution._closed_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            warnings.warn(
                f"Can not wait for trigger flow { execution.id } to close because it took too long and timeout.\n"
                "Use close()/async_close(), reduce auto_close_timeout, or pass timeout=None to wait forever."
                f"Timeout: { timeout }"
            )
            return None
        return execution._close_result if execution._close_result is not None else self.build_close_snapshot()

    async def async_put_into_stream(
        self,
        stream_item: Any,
        *,
        _skip_contract_validation: bool = False,
        _origin_chunk: dict[str, Any] | None = None,
    ):
        execution = self._execution
        if execution._lifecycle_state == TRIGGER_FLOW_LIFECYCLE_CLOSED:
            warnings.warn(
                f"TriggerFlow execution { execution.id } ignored stream item because it is closed.",
                RuntimeWarning,
                stacklevel=3,
            )
            await execution._emit_runtime_event(
                "triggerflow.stream_item_rejected",
                level="WARNING",
                message=f"TriggerFlow execution '{ execution.id }' ignored stream item because it is closed.",
                payload={
                    "item": execution._to_serializable_value(stream_item),
                    "origin_chunk": _origin_chunk or execution._get_origin_chunk_payload(),
                },
            )
            return None
        if not _skip_contract_validation:
            stream_item = execution._trigger_flow._contract.validate_stream_item(stream_item)
        await execution._runtime_stream_transport.publish(stream_item)
        execution._mark_activity()
        await execution._emit_runtime_event(
            "triggerflow.stream_item_emitted",
            message=f"TriggerFlow execution '{ execution.id }' emitted a stream item.",
            payload={
                "item": execution._to_serializable_value(stream_item),
                "item_type": type(stream_item).__name__,
                "origin_chunk": _origin_chunk or execution._get_origin_chunk_payload(),
            },
        )

    async def async_stop_stream(self):
        execution = self._execution
        if execution._runtime_stream_stopped:
            return
        execution._runtime_stream_stopped = True
        execution._runtime_stream_transport.close()
        await execution._emit_runtime_event(
            "triggerflow.stream_closed",
            message=f"TriggerFlow execution '{ execution.id }' runtime stream closed.",
            payload={"execution_id": execution.id},
        )

    async def consume_runtime_stream(
        self,
        *,
        initial_value: Any,
        timeout: float | None,
    ) -> AsyncGenerator[Any | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None]:
        execution = self._execution
        subscription = execution._runtime_stream_transport.subscribe(
            start="earliest",
            timeout=None,
        )
        start_task = self._ensure_async_stream_started(initial_value)
        try:
            while True:
                try:
                    if timeout is None:
                        item = await anext(subscription)
                    else:
                        item = await asyncio.wait_for(
                            anext(subscription),
                            timeout=timeout,
                        )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    self._warn_runtime_stream_timeout(timeout)
                    break
                yield item
        finally:
            await subscription.async_close()
            if not execution._resume_handle_exposed:
                await execution.async_close(reason="hidden_runtime_stream_complete")
            elif start_task is not None and start_task.done():
                await start_task

    def _publish_stream_start_failure(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            self._execution._runtime_stream_transport.fail(asyncio.CancelledError())
            return
        error = task.exception()
        if error is not None:
            self._execution._runtime_stream_transport.fail(error)

    def _ensure_async_stream_started(
        self,
        initial_value: Any,
    ) -> asyncio.Task[Any] | None:
        execution = self._execution
        with self._stream_start_lock:
            if execution._started or self._stream_start_claimed:
                return self._async_stream_start_task
            self._stream_start_claimed = True
            task = execution._create_managed_task(
                execution._async_run_start(initial_value=initial_value),
                origin="runtime_stream.start",
            )
            task.add_done_callback(self._publish_stream_start_failure)
            self._async_stream_start_task = task
            return task

    def _ensure_sync_stream_started(self, initial_value: Any) -> None:
        execution = self._execution
        with self._stream_start_lock:
            if execution._started or self._stream_start_claimed:
                return
            self._stream_start_claimed = True

            def drive() -> None:
                async def run() -> None:
                    try:
                        await execution._async_run_start(initial_value=initial_value)
                    except BaseException as error:
                        execution._runtime_stream_transport.fail(error)
                    finally:
                        if not execution._resume_handle_exposed:
                            await execution.async_close(reason="hidden_runtime_stream_complete")

                asyncio.run(run())

            driver = threading.Thread(
                target=drive,
                name=f"TriggerFlowStream[{execution.id}]",
                daemon=True,
            )
            self._sync_stream_driver = driver
            driver.start()

    def _warn_runtime_stream_timeout(self, timeout: float | None) -> None:
        execution = self._execution
        warnings.warn(
            f"Execution { execution.id } runtime stream stopped because of timeout.\n"
            f"Timeout seconds: { timeout }\n"
            "You can use execution.get_async_runtime_stream(timeout=<int | None>) or "
            "execution.get_runtime_stream(timeout=<int | None>) to reset new timeout "
            "seconds or use None to wait forever."
        )

    def get_async_runtime_stream(
        self,
        initial_value: Any = None,
        *,
        timeout: float | None = 10,
    ) -> AsyncGenerator[Any | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None]:
        return self.consume_runtime_stream(
            initial_value=initial_value,
            timeout=timeout,
        )

    def get_runtime_stream(
        self,
        initial_value: Any = None,
        *,
        timeout: float | None = 10,
    ) -> Generator[Any | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None, None]:
        execution = self._execution
        subscription = execution._runtime_stream_transport.subscribe(
            start="earliest",
            timeout=timeout,
        )
        self._ensure_sync_stream_started(initial_value)

        def generate() -> Generator[
            Any | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent,
            None,
            None,
        ]:
            try:
                while True:
                    try:
                        yield next(subscription)
                    except StopIteration:
                        if not execution._runtime_stream_transport.is_terminal:
                            self._warn_runtime_stream_timeout(timeout)
                        return
            finally:
                subscription.close()

        return generate()

    def set_result(self, result: Any, *, _origin_chunk: dict[str, Any] | None = None):
        execution = self._execution
        execution._ensure_terminal_state_mutable()
        result = execution._trigger_flow._contract.validate_result(result)
        previous_result = self.get_compat_result()
        if previous_result is not None:
            warnings.warn(
                f"TriggerFlow execution { execution.id } overwrote compatibility final result '{ COMPAT_FINAL_RESULT_KEY }'.",
                RuntimeWarning,
                stacklevel=3,
            )
        execution._runtime_data.set(COMPAT_FINAL_RESULT_KEY, result)
        execution._system_runtime_data.set("result", result)
        result_ready = execution._system_runtime_data.get("result_ready")
        if isinstance(result_ready, asyncio.Event):
            result_ready.set()
        execution._bump_state_version()
        execution._mark_activity()
        if not execution._runtime_result_set_emitted:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                execution._runtime_result_set_emitted = True
                loop.create_task(
                    execution._emit_runtime_event(
                        "triggerflow.result_set",
                        message=f"TriggerFlow execution '{ execution.id }' set a result.",
                        payload={
                            "result": execution._to_serializable_value(result),
                            "state_key": COMPAT_FINAL_RESULT_KEY,
                            "origin_chunk": _origin_chunk or execution._get_origin_chunk_payload(),
                        },
                    )
                )

    async def async_get_result(self, *, timeout: float | None = None):
        return await self._execution.result.async_get_final_result(timeout=timeout)
