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

import uuid
import asyncio
import warnings
import copy
from pathlib import Path

from typing import Callable, Any, Literal, TYPE_CHECKING, overload, AsyncGenerator, Generator, Generic, TypeVar, cast

if TYPE_CHECKING:
    from .Execution import TriggerFlowExecution
    from .Chunk import TriggerFlowHandler
    from agently.types.data import ExecutionEnvironmentRequirement, RunContext, SerializableValue

from agently.types.trigger_flow import (
    TriggerFlowBlockData,
    TriggerFlowContractMetadata,
    TriggerFlowInterventionEvent,
    TriggerFlowInterruptEvent,
)
from agently.types.data import RunContext
from agently.utils import DeprecationWarnings, Settings, StateData, FunctionShifter
from agently.core.runtime.RuntimeContext import resolve_parent_run_context
from .BluePrint import TriggerFlowBlueprint
from .Process import TriggerFlowProcess
from .Chunk import TriggerFlowChunk
from .Contract import CONTRACT_UNSET, TriggerFlowContract, TriggerFlowContractSpec

InputT = TypeVar("InputT")
StreamT = TypeVar("StreamT")
ResultT = TypeVar("ResultT")
ContractInputT = TypeVar("ContractInputT")
ContractStreamT = TypeVar("ContractStreamT")
ContractResultT = TypeVar("ContractResultT")


class _InterventionModeUnset:
    __slots__ = ()


_INTERVENTION_MODE_UNSET = _InterventionModeUnset()
_INTERVENTION_MODE_DEFAULT = cast(Any, _INTERVENTION_MODE_UNSET)


class TriggerFlow(Generic[InputT, StreamT, ResultT]):
    def __init__(
        self,
        blueprint: TriggerFlowBlueprint | None = None,
        name: str | None = None,
        skip_exceptions: bool = False,
    ):
        from agently.base import settings

        self.name = name or uuid.uuid4().hex
        self.settings = Settings(
            name=f"TriggerFlow-{ self.name }-Settings",
            parent=settings,
        )

        self._flow_data = StateData()
        self._runtime_resources = StateData(
            name=f"TriggerFlow-{ self.name }-RuntimeResources",
        )
        self._resource_requirements: list[dict[str, Any]] = []
        self._blue_print = blueprint if blueprint is not None else TriggerFlowBlueprint()
        self._skip_exceptions = skip_exceptions
        self._executions: dict[str, "TriggerFlowExecution[InputT, StreamT, ResultT]"] = {}
        self._contract = TriggerFlowContract[InputT, StreamT, ResultT]()
        self._contract_metadata: TriggerFlowContractMetadata | None = None
        self.set_settings = self.settings.set_settings
        self.load_settings = self.settings.load

        self._set_flow_data = FunctionShifter.syncify(self._async_set_flow_data)
        self._append_flow_data = FunctionShifter.syncify(self._async_append_flow_data)
        self._del_flow_data = FunctionShifter.syncify(self._async_del_flow_data)
        self.set_flow_data = FunctionShifter.syncify(self.async_set_flow_data)
        self.append_flow_data = FunctionShifter.syncify(self.async_append_flow_data)
        self.del_flow_data = FunctionShifter.syncify(self.async_del_flow_data)

        self.start_execution = FunctionShifter.syncify(self.async_start_execution)
        self.register_chunk_handler = self._blue_print.register_chunk_handler
        self.register_condition_handler = self._blue_print.register_condition_handler
        self.set_runtime_resource = self._set_runtime_resource
        self.get_runtime_resource = self._get_runtime_resource
        self.del_runtime_resource = self._del_runtime_resource
        self.update_runtime_resources = self._update_runtime_resources
        self.clear_runtime_resources = self._clear_runtime_resources
        self.declare_resource_requirement = self._declare_resource_requirement
        self._bind_start_process()

    def _bind_start_process(self):
        self._start_process = TriggerFlowProcess(
            flow_chunk=self.chunk,
            trigger_event="START",
            blueprint=self._blue_print,
            block_data=TriggerFlowBlockData(
                outer_block=None,
            ),
            definition_signals=[self._blue_print.make_signal("event", "START")],
            definition_group_id=None,
            definition_group_kind=None,
        )
        self.chunks = self._blue_print.chunks
        self.when = self._start_process.when
        self.to = self._start_process.to
        self.to_sub_flow = self._start_process.to_sub_flow
        self.intervention_point = self._start_process.intervention_point
        self.side_branch = self._start_process.side_branch
        self.batch = self._start_process.batch
        self.for_each = self._start_process.for_each
        self.match = self._start_process.match
        self.if_condition = self._start_process.if_condition

    def _has_intervention_points(self):
        return any(
            operator.get("kind") == "intervention_point"
            for operator in self._blue_print.definition.operators
        )

    def _resolve_intervention_mode(
        self,
        intervention_mode: Any,
    ) -> Literal["planned", "auto"] | None:
        if isinstance(intervention_mode, _InterventionModeUnset):
            if self._has_intervention_points():
                return "planned"
            return None
        if intervention_mode is None:
            return None
        return intervention_mode

    @overload
    def chunk(self, handler_or_name: "TriggerFlowHandler") -> TriggerFlowChunk: ...

    @overload
    def chunk(self, handler_or_name: str) -> "Callable[[TriggerFlowHandler], TriggerFlowChunk]": ...

    def chunk(
        self, handler_or_name: "TriggerFlowHandler | str"
    ) -> "TriggerFlowChunk | Callable[[TriggerFlowHandler], TriggerFlowChunk]":
        if isinstance(handler_or_name, str):

            def wrapper(func: "TriggerFlowHandler"):
                chunk = self._blue_print.create_chunk(
                    func,
                    name=handler_or_name,
                    explicit_name=handler_or_name,
                )
                return chunk

            return wrapper
        else:
            chunk = self._blue_print.create_chunk(
                handler_or_name,
                name=handler_or_name.__name__,
            )
            return chunk

    def create_execution(
        self,
        *,
        skip_exceptions: bool | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 10.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
        intervention_mode: Literal["planned", "auto"] | None = _INTERVENTION_MODE_DEFAULT,
        intervention_policy: Any = None,
        resume_handle_exposed: bool = True,
    ) -> "TriggerFlowExecution[InputT, StreamT, ResultT]":
        execution_id = uuid.uuid4().hex
        skip_exceptions = skip_exceptions if skip_exceptions is not None else self._skip_exceptions
        intervention_mode = self._resolve_intervention_mode(intervention_mode)
        parent_run_context = resolve_parent_run_context(parent_run_context)
        execution_run_context = run_context
        if execution_run_context is None:
            if parent_run_context is not None:
                execution_run_context = parent_run_context.create_child(
                    run_kind="workflow_execution",
                    execution_id=execution_id,
                    meta={"flow_name": self.name},
                )
            else:
                execution_run_context = RunContext.create(
                    run_kind="workflow_execution",
                    execution_id=execution_id,
                    meta={"flow_name": self.name},
                )
        execution = self._blue_print.create_execution(
            self,
            execution_id=execution_id,
            skip_exceptions=skip_exceptions,
            concurrency=concurrency,
            run_context=execution_run_context,
            auto_close=auto_close,
            auto_close_timeout=auto_close_timeout,
            owner_id=owner_id,
            lease_ttl=lease_ttl,
            execution_environments=execution_environments,
            intervention_mode=intervention_mode,
            intervention_policy=intervention_policy,
            resume_handle_exposed=resume_handle_exposed,
        )
        if runtime_resources:
            execution.update_runtime_resources(runtime_resources)
        self._executions[execution_id] = execution
        return cast("TriggerFlowExecution[InputT, StreamT, ResultT]", execution)

    @overload
    def set_contract(
        self,
        *,
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[InputT, StreamT, ResultT]": ...

    @overload
    def set_contract(
        self,
        *,
        initial_input: type[ContractInputT],
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[ContractInputT, StreamT, ResultT]": ...

    @overload
    def set_contract(
        self,
        *,
        stream: type[ContractStreamT],
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[InputT, ContractStreamT, ResultT]": ...

    @overload
    def set_contract(
        self,
        *,
        result: type[ContractResultT],
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[InputT, StreamT, ContractResultT]": ...

    @overload
    def set_contract(
        self,
        *,
        initial_input: type[ContractInputT],
        stream: type[ContractStreamT],
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[ContractInputT, ContractStreamT, ResultT]": ...

    @overload
    def set_contract(
        self,
        *,
        initial_input: type[ContractInputT],
        result: type[ContractResultT],
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[ContractInputT, StreamT, ContractResultT]": ...

    @overload
    def set_contract(
        self,
        *,
        stream: type[ContractStreamT],
        result: type[ContractResultT],
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[InputT, ContractStreamT, ContractResultT]": ...

    @overload
    def set_contract(
        self,
        *,
        initial_input: type[ContractInputT],
        stream: type[ContractStreamT],
        result: type[ContractResultT],
        meta: dict[str, Any] | None = None,
    ) -> "TriggerFlow[ContractInputT, ContractStreamT, ContractResultT]": ...

    def set_contract(
        self,
        *,
        initial_input: Any = CONTRACT_UNSET,
        stream: Any = CONTRACT_UNSET,
        result: Any = CONTRACT_UNSET,
        meta: dict[str, Any] | None | object = CONTRACT_UNSET,
    ) -> "TriggerFlow[Any, Any, Any]":
        self._contract.update(
            initial_input=initial_input,
            stream=stream,
            result=result,
            meta=meta,
        )
        self._contract_metadata = None
        self._blue_print.definition.contract = self._contract.export_metadata()
        return self

    def get_contract(self) -> TriggerFlowContractSpec[InputT, StreamT, ResultT]:
        return self._contract.snapshot()

    def get_contract_metadata(self) -> TriggerFlowContractMetadata:
        if self._contract_metadata is not None:
            return copy.deepcopy(self._contract_metadata)
        return self._contract.export_metadata()

    def _sync_contract_from_blueprint(self):
        self._contract = TriggerFlowContract[InputT, StreamT, ResultT]()
        contract_metadata = self._blue_print.definition.contract
        self._contract_metadata = copy.deepcopy(contract_metadata) if contract_metadata else None

    def _set_runtime_resource(self, key: str, value: Any):
        self._runtime_resources.set(str(key), value)
        return self

    def _get_runtime_resource(self, key: str, default: Any = None):
        return self._runtime_resources.get(str(key), default, inherit=False)

    def _del_runtime_resource(self, key: str):
        self._runtime_resources.pop(str(key), None)
        return self

    def _update_runtime_resources(
        self,
        mapping: dict[str, Any] | None = None,
        **kwargs,
    ):
        if mapping is not None:
            for key, value in dict(mapping).items():
                self._set_runtime_resource(str(key), value)
        for key, value in kwargs.items():
            self._set_runtime_resource(str(key), value)
        return self

    def _clear_runtime_resources(self):
        self._runtime_resources.clear()
        return self

    def _declare_resource_requirement(
        self,
        key: str,
        *,
        kind: str = "runtime_resource",
        required: bool = True,
        metadata: dict[str, Any] | None = None,
    ):
        requirement = {
            "kind": str(kind),
            "key": str(key),
            "required": bool(required),
            "source": "flow",
            "metadata": {"scope": "flow", **dict(metadata or {})},
        }
        self._resource_requirements = [
            item
            for item in self._resource_requirements
            if not (
                item.get("kind") == requirement["kind"]
                and item.get("key") == requirement["key"]
                and item.get("source") == requirement["source"]
            )
        ]
        self._resource_requirements.append(requirement)
        return self

    def get_resource_requirements(self):
        return copy.deepcopy(self._resource_requirements)

    def remove_execution(self, execution: "TriggerFlowExecution | str"):
        if isinstance(execution, str):
            if execution in self._executions:
                del self._executions[execution]
        else:
            if execution.id in self._executions:
                del self._executions[execution.id]

    def _warn_flow_data_api(self, method_name: str, *, no_warning: bool = False):
        if no_warning:
            return
        warnings.warn(
            f"TriggerFlow.{ method_name }() accesses flow-scoped data shared by all executions. "
            "Prefer execution state APIs for concurrent workflows, or pass no_warning=True if the shared scope is intentional.",
            RuntimeWarning,
            stacklevel=3,
        )

    def _get_flow_data(
        self,
        key: Any | None = None,
        default: Any = None,
        *,
        inherit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("_get_flow_data", no_warning=no_warning)
        return self._flow_data.get(key, default, inherit=inherit)

    def get_flow_data(
        self,
        key: Any | None = None,
        default: Any = None,
        *,
        inherit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("get_flow_data", no_warning=no_warning)
        return self._flow_data.get(key, default, inherit=inherit)

    async def _async_set_flow_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("_async_set_flow_data", no_warning=no_warning)
        return await self._async_change_flow_data("set", key, value, emit=emit)

    async def _async_append_flow_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("_async_append_flow_data", no_warning=no_warning)
        return await self._async_change_flow_data("append", key, value, emit=emit)

    async def _async_del_flow_data(
        self,
        key: str,
        *,
        emit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("_async_del_flow_data", no_warning=no_warning)
        return await self._async_change_flow_data("del", key, None, emit=emit)

    async def async_start_execution(
        self,
        initial_value: InputT | None,
        *,
        wait_for_result: bool = False,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 10.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
        intervention_mode: Literal["planned", "auto"] | None = _INTERVENTION_MODE_DEFAULT,
        intervention_policy: Any = None,
        resume_handle_exposed: bool = True,
    ) -> "TriggerFlowExecution[InputT, StreamT, ResultT]":
        if wait_for_result is not False:
            DeprecationWarnings.warn_deprecated_once(
                "TriggerFlow.async_start_execution.wait_for_result",
                "TriggerFlow.async_start_execution(..., wait_for_result=...) is deprecated and ignored. "
                "start_execution() now always returns the execution handle.",
                stacklevel=2,
            )
        effective_auto_close_timeout = timeout if timeout is not None else auto_close_timeout
        execution = self.create_execution(
            concurrency=concurrency,
            runtime_resources=runtime_resources,
            run_context=run_context,
            parent_run_context=parent_run_context,
            auto_close=auto_close,
            auto_close_timeout=effective_auto_close_timeout,
            owner_id=owner_id,
            lease_ttl=lease_ttl,
            execution_environments=execution_environments,
            intervention_mode=intervention_mode,
            intervention_policy=intervention_policy,
            resume_handle_exposed=resume_handle_exposed,
        )
        await execution._async_run_start(initial_value)
        return execution

    async def _async_change_flow_data(
        self,
        operation: Literal["set", "append", "del"],
        key: str,
        value: Any,
        *,
        emit: bool = True,
    ):
        futures = []
        match operation:
            case "set":
                self._flow_data.set(key, value)
                value = self._flow_data[key]
            case "append":
                self._flow_data.append(key, value)
                value = self._flow_data[key]
            case "del":
                missing = object()
                if self._flow_data.get(key, missing) is not missing:
                    del self._flow_data[key]
                    value = None
                else:
                    return

        if emit:
            for execution in self._executions.values():
                handlers = execution._handlers["flow_data"]
                if key in handlers:
                    futures.append(
                        execution.async_emit(
                            key,
                            value,
                            trigger_type="flow_data",
                            _source="flow_data",
                        )
                    )
            if futures:
                await asyncio.gather(*futures, return_exceptions=True)

    async def async_set_flow_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("async_set_flow_data", no_warning=no_warning)
        return await self._async_change_flow_data("set", key, value, emit=emit)

    async def async_append_flow_data(
        self,
        key: str,
        value: Any,
        *,
        emit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("async_append_flow_data", no_warning=no_warning)
        return await self._async_change_flow_data("append", key, value, emit=emit)

    async def async_del_flow_data(
        self,
        key: str,
        *,
        emit: bool = True,
        no_warning: bool = False,
    ):
        self._warn_flow_data_api("async_del_flow_data", no_warning=no_warning)
        return await self._async_change_flow_data("del", key, None, emit=emit)

    @overload
    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[True] = True,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 0.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
    ) -> ResultT: ...

    @overload
    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[False],
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 0.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
    ) -> None: ...

    def start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: bool = True,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 0.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
    ) -> Any:
        return FunctionShifter.syncify(self.async_start)(
            initial_value,
            wait_for_result=wait_for_result,
            timeout=timeout,
            concurrency=concurrency,
            runtime_resources=runtime_resources,
            execution_environments=execution_environments,
            run_context=run_context,
            parent_run_context=parent_run_context,
            auto_close=auto_close,
            auto_close_timeout=auto_close_timeout,
            owner_id=owner_id,
            lease_ttl=lease_ttl,
        )

    @overload
    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[True] = True,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 0.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
    ) -> ResultT: ...

    @overload
    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: Literal[False],
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 0.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
    ) -> None: ...

    async def async_start(
        self,
        initial_value: InputT | None = None,
        *,
        wait_for_result: bool = True,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        execution_environments: "list[ExecutionEnvironmentRequirement] | None" = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
        auto_close: bool = True,
        auto_close_timeout: float | None = 0.0,
        owner_id: str | None = None,
        lease_ttl: float | None = None,
    ) -> Any:
        if not auto_close:
            raise ValueError(
                "TriggerFlow.start()/async_start() require auto_close=True because the execution handle is hidden. "
                "Use start_execution()/create_execution() for manual lifecycle control."
            )
        if wait_for_result is False:
            DeprecationWarnings.warn_deprecated_once(
                "TriggerFlow.start.wait_for_result_false",
                "TriggerFlow.start()/async_start(..., wait_for_result=False) is deprecated and ignored. "
                "The hidden execution path now always waits for close and returns the close snapshot. "
                "Use start_execution()/create_execution() for non-blocking execution control.",
                stacklevel=2,
            )
        effective_auto_close_timeout = timeout if timeout is not None else auto_close_timeout
        execution = await self.async_start_execution(
            initial_value,
            wait_for_result=False,
            timeout=effective_auto_close_timeout,
            concurrency=concurrency,
            runtime_resources=runtime_resources,
            execution_environments=execution_environments,
            run_context=run_context,
            parent_run_context=parent_run_context,
            auto_close=auto_close,
            auto_close_timeout=effective_auto_close_timeout,
            owner_id=owner_id,
            lease_ttl=lease_ttl,
            resume_handle_exposed=False,
        )
        return await execution._async_wait_for_close_snapshot()

    def get_async_runtime_stream(
        self,
        initial_value: InputT | None = None,
        *,
        timeout: float | None = 10.0,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
    ) -> AsyncGenerator[StreamT | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None]:
        execution = self.create_execution(
            concurrency=concurrency,
            runtime_resources=runtime_resources,
            run_context=run_context,
            parent_run_context=parent_run_context,
            auto_close_timeout=0.0,
            resume_handle_exposed=False,
        )
        return execution.get_async_runtime_stream(
            initial_value,
            timeout=timeout,
        )

    def get_runtime_stream(
        self,
        initial_value: InputT | None = None,
        *,
        timeout: float | None = 10.0,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
        run_context: "RunContext | None" = None,
        parent_run_context: "RunContext | None" = None,
    ) -> Generator[StreamT | TriggerFlowInterruptEvent | TriggerFlowInterventionEvent, None, None]:
        execution = self.create_execution(
            concurrency=concurrency,
            runtime_resources=runtime_resources,
            run_context=run_context,
            parent_run_context=parent_run_context,
            auto_close_timeout=0.0,
            resume_handle_exposed=False,
        )
        return execution.get_runtime_stream(
            initial_value,
            timeout=timeout,
        )

    def save_blueprint(self):
        return self._blue_print.copy()

    def load_blueprint(self, new_blueprint: TriggerFlowBlueprint):
        self._blue_print = new_blueprint
        self._sync_contract_from_blueprint()
        self.register_chunk_handler = self._blue_print.register_chunk_handler
        self.register_condition_handler = self._blue_print.register_condition_handler
        self._bind_start_process()
        return self

    def get_flow_config(self, *, validate_serializable: bool = True):
        return self._blue_print.get_flow_config(name=self.name, validate_serializable=validate_serializable)

    def get_json_flow(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ):
        return self._blue_print.get_json_flow(
            save_to=save_to,
            encoding=encoding,
            name=self.name,
        )

    def get_yaml_flow(
        self,
        save_to: str | Path | None = None,
        *,
        encoding: str | None = "utf-8",
    ):
        return self._blue_print.get_yaml_flow(
            save_to=save_to,
            encoding=encoding,
            name=self.name,
        )

    def load_flow_config(
        self,
        config: dict[str, Any],
        *,
        replace: bool = True,
    ):
        self._blue_print.load_flow_config(config, replace=replace)
        self._sync_contract_from_blueprint()
        self.name = self._blue_print.name
        self._bind_start_process()
        return self

    def load_json_flow(
        self,
        path_or_content: str | Path,
        *,
        replace: bool = True,
        encoding: str | None = "utf-8",
    ):
        self._blue_print.load_json_flow(
            path_or_content,
            replace=replace,
            encoding=encoding,
        )
        self._sync_contract_from_blueprint()
        self.name = self._blue_print.name
        self._bind_start_process()
        return self

    def load_yaml_flow(
        self,
        path_or_content: str | Path,
        *,
        replace: bool = True,
        encoding: str | None = "utf-8",
    ):
        self._blue_print.load_yaml_flow(
            path_or_content,
            replace=replace,
            encoding=encoding,
        )
        self._sync_contract_from_blueprint()
        self.name = self._blue_print.name
        self._bind_start_process()
        return self

    def to_mermaid(self, *, mode: Literal["simplified", "detailed"] = "simplified"):
        return self._blue_print.to_mermaid(mode=mode, name=self.name)
