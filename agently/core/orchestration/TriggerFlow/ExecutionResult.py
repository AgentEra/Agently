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
import warnings
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from agently.types.data import EMPTY
from agently.utils import StateData

from .ExecutionState import COMPAT_FINAL_RESULT_KEY, INTERVENTIONS_STATE_KEY

ResultT = TypeVar("ResultT")

if TYPE_CHECKING:
    from .Execution import TriggerFlowExecution


class TriggerFlowExecutionResult(Generic[ResultT]):
    def __init__(self, execution: "TriggerFlowExecution[Any, Any, ResultT]"):
        self._execution = execution
        self.get_final_result = default_stage_call_bridge.as_sync(self.async_get_final_result)

    def _state_view(self):
        if self._execution._closed_event.is_set() and self._execution._close_result is not None:
            state = self._execution._close_result
        else:
            state = self._execution._runtime_state_snapshot()
        return state if isinstance(state, dict) else {}

    def _copy_value(self, value: Any):
        return StateData({"value": value}).get("value")

    def _resolve_final_result_or_snapshot(self):
        compat_result = self._execution._get_state(COMPAT_FINAL_RESULT_KEY, EMPTY, inherit=False)
        if compat_result is not EMPTY:
            return compat_result

        result = self._execution._system_runtime_data.get("result")
        if result is not EMPTY:
            return result

        if self._execution._close_result is not None:
            return self._execution._close_result
        return self._execution._build_close_snapshot()

    def get_state(self, key: Any | None = None, default: Any = None):
        state = StateData(self._state_view())
        return state.get(key, default, inherit=False)

    async def async_get_final_result(self, timeout: float | None = None) -> ResultT | None:
        if self._execution._compat_result_exists() or self._execution._closed_event.is_set():
            return cast(ResultT | None, self._resolve_final_result_or_snapshot())

        result_ready = self._execution._system_runtime_data.get("result_ready")
        waiters: list[asyncio.Task[Any]] = []
        if isinstance(result_ready, asyncio.Event):
            waiters.append(asyncio.create_task(result_ready.wait()))
        waiters.append(asyncio.create_task(self._execution._closed_event.wait()))

        try:
            if timeout is None:
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            else:
                done, _ = await asyncio.wait(
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
                f"Can not get the compatibility result of trigger flow { self._execution.id } because it took too "
                "long and timeout.\nUse close()/async_close(), reduce auto_close_timeout, or pass timeout=None to "
                f"wait forever. Timeout: { timeout }"
            )
            return None
        return cast(ResultT | None, self._resolve_final_result_or_snapshot())

    def _read_intervention_records(self):
        records = self._execution._system_runtime_data.get("interventions", EMPTY, inherit=False)
        if records is EMPTY:
            records = self._execution._get_state(INTERVENTIONS_STATE_KEY, EMPTY, inherit=False)
        if records is EMPTY:
            records = self._execution._get_state("interventions", EMPTY, inherit=False)
        if records is EMPTY:
            return []
        if isinstance(records, dict):
            return [record for record in records.values() if isinstance(record, dict)]
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
        return []

    def get_interventions(
        self,
        status: str | None = None,
        target: str | None = None,
        since_version: int | None = None,
        consumed_by: str | None = None,
    ):
        interventions = []
        for record in self._read_intervention_records():
            if status is not None and record.get("status") != status:
                continue
            if target is not None and record.get("target") != target:
                continue
            if since_version is not None and int(record.get("version", 0)) <= since_version:
                continue
            if consumed_by is not None:
                consumers = record.get("consumers", {})
                legacy_consumed_by = record.get("consumed_by")
                if isinstance(consumers, dict) and consumed_by in consumers:
                    pass
                elif isinstance(legacy_consumed_by, (list, tuple, set)) and consumed_by in legacy_consumed_by:
                    pass
                elif legacy_consumed_by == consumed_by:
                    pass
                else:
                    continue
            interventions.append(self._copy_value(record))
        return interventions

    def get_latest_intervention(self, default: Any = None, **filters: Any):
        interventions = self.get_interventions(**filters)
        if not interventions:
            return default
        return interventions[-1]

    def get_meta(self):
        return {
            "execution_id": self._execution.id,
            "run_id": self._execution.run_id,
            "flow_name": self._execution._trigger_flow.name,
            "status": self._execution._status,
            "lifecycle_state": self._execution._lifecycle_state,
            "created_at": self._execution._created_at,
            "started_at": self._execution._started_at,
            "closed_at": self._execution._closed_at,
            "close_reason": self._execution._close_reason,
            "state_version": self._execution._state_version,
        }
