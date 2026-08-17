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
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from agently.types.data import TaskDAG, TaskDAGNode
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import DataLocator
from agently.core.operation.PolicyApproval import merge_access_control_policy

from .TaskDAGHelpers import (
    _approval_payload,
    _approval_required,
    _approval_type,
    _chunk_name,
    _collect_semantic_outputs,
    _done_graph_event,
    _done_task_event,
    _extract_artifact_refs,
    _failed_task_event,
    _fallback_action,
    _fallback_retry_config,
    _fallback_terminal_action,
    _graph_fingerprint,
    _graph_signature,
    _is_approval_task,
    _start_task_event,
)
from .TaskDAGResolver import TaskDAGContext, TaskDAGResolver, _coerce_resolver
from .TaskDAGValidation import TaskDAGValidation, validate_task_dag

if TYPE_CHECKING:
    from agently.core.orchestration.TriggerFlow import TriggerFlow
    from agently.core.orchestration.TriggerFlow.Execution import TriggerFlowExecution


_DYNAMIC_CACHE_ATTR = "_task_dag_executor_cache"
_RUNTIME_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class CompiledTaskDAG:
    graph: TaskDAG
    flow: "TriggerFlow"
    validation: TaskDAGValidation

    def create_execution(self, **kwargs: Any) -> "TriggerFlowExecution":
        return self.flow.create_execution(**kwargs)

    async def async_run(
        self,
        graph_input: Any = None,
        *,
        timeout: float | None = None,
        concurrency: int | None = None,
        runtime_resources: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        execution = self.create_execution(
            auto_close=False,
            concurrency=concurrency,
            runtime_resources=runtime_resources,
        )
        await execution.async_start(graph_input)
        return await execution.async_close(timeout=timeout)

def compile_task_dag(
    graph: TaskDAG | Mapping[str, Any],
    *,
    resolver: TaskDAGResolver | Mapping[str, Any] | None = None,
    flow: "TriggerFlow | None" = None,
    name: str | None = None,
) -> CompiledTaskDAG:
    from agently.core.orchestration.TriggerFlow import TriggerFlow

    resolved_resolver = _coerce_resolver(resolver)
    validation = validate_task_dag(graph, resolver=resolved_resolver)
    normalized = validation.graph
    target_flow = flow if flow is not None else TriggerFlow(name=name or f"dynamic-task:{ normalized.graph_id }")
    _assert_graph_signature_compatible(target_flow, normalized)

    cache = _get_compile_cache(target_flow)
    result_lock = cache["result_locks"].setdefault(normalized.graph_id, asyncio.Lock())

    kickoff = _cached_handler(
        cache,
        normalized.graph_id,
        "kickoff",
        "*",
        lambda: _make_kickoff_handler(normalized, validation.root_task_ids),
    )
    target_flow.to(kickoff, name=_chunk_name(normalized.graph_id, "kickoff", "*"))

    for task in normalized.tasks:
        runner = _cached_handler(
            cache,
            normalized.graph_id,
            "run",
            task.id,
            lambda task=task: _make_task_runner(
                graph=normalized,
                task=task,
                resolver=resolved_resolver,
                result_lock=result_lock,
            ),
        )
        trigger = _task_trigger(target_flow, task)
        trigger.to(runner, name=_chunk_name(normalized.graph_id, "run", task.id))

    finalize = _cached_handler(
        cache,
        normalized.graph_id,
        "finalize",
        "*",
        lambda: _make_finalize_handler(normalized),
    )
    target_flow.when(_done_graph_event(normalized.graph_id)).to(
        finalize,
        name=_chunk_name(normalized.graph_id, "finalize", "*"),
    )
    return CompiledTaskDAG(
        graph=normalized,
        flow=target_flow,
        validation=validation,
    )


def _get_compile_cache(flow: "TriggerFlow") -> dict[str, Any]:
    cache = getattr(flow, _DYNAMIC_CACHE_ATTR, None)
    if cache is None:
        cache = {
            "graph_signatures": {},
            "handlers": {},
            "result_locks": {},
        }
        setattr(flow, _DYNAMIC_CACHE_ATTR, cache)
    return cache


def _assert_graph_signature_compatible(flow: "TriggerFlow", graph: TaskDAG) -> None:
    cache = _get_compile_cache(flow)
    signature = _graph_signature(graph)
    existing = cache["graph_signatures"].get(graph.graph_id)
    if existing is not None and existing != signature:
        raise ValueError(
            f"Task DAG '{ graph.graph_id }' was already compiled with a different definition."
        )
    cache["graph_signatures"][graph.graph_id] = signature


def _cached_handler(
    cache: dict[str, Any],
    graph_id: str,
    phase: str,
    task_id: str,
    factory: Callable[[], Callable[[TriggerFlowRuntimeData], Any]],
):
    key = (graph_id, phase, task_id)
    if key not in cache["handlers"]:
        cache["handlers"][key] = factory()
    return cache["handlers"][key]


def _make_kickoff_handler(
    graph: TaskDAG,
    root_task_ids: tuple[str, ...],
):
    async def kickoff(data: TriggerFlowRuntimeData):
        await data.async_set_state("graph_id", graph.graph_id, emit=False)
        await data.async_set_state("graph_input", data.value, emit=False)
        await data.async_set_state("task_results", {}, emit=False)
        await data.async_set_state("task_statuses", {}, emit=False)
        await data.async_set_state("task_failures", {}, emit=False)
        await data.async_set_state("artifact_refs", {}, emit=False)
        await data.async_set_state("semantic_outputs", {}, emit=False)
        await data.async_set_state("task_dag", graph.to_dict(), emit=False)
        await data.async_set_state("task_dag_graph_fingerprint", _graph_fingerprint(graph), emit=False)
        root_tasks = []
        for task_id in root_task_ids:
            task = data.emit_nowait(_start_task_event(task_id), {"task_id": task_id, "graph_id": graph.graph_id})
            if task is not None:
                root_tasks.append(task)
        if root_tasks:
            await asyncio.gather(*root_tasks)

    return kickoff


def _make_task_runner(
    *,
    graph: TaskDAG,
    task: TaskDAGNode,
    resolver: TaskDAGResolver,
    result_lock: asyncio.Lock,
):
    async def run_task(data: TriggerFlowRuntimeData):
        graph_input = data.get_state("graph_input")
        task_results = dict(data.get_state("task_results", {}) or {})
        dependency_results = {dependency: task_results[dependency] for dependency in task.depends_on}
        execution_state = data.get_state() or {}
        resolved_inputs = _resolve_task_input_placeholders(
            task.inputs,
            trigger_payload=data.value,
            initial_input=graph_input,
            execution_state=execution_state,
            dependency_results=dependency_results,
            task_id=task.id,
        )
        resolved_task = replace(task, inputs=resolved_inputs)
        task_input = {
            "task_id": resolved_task.id,
            "task": resolved_task.to_dict(),
            "graph_id": graph.graph_id,
            "init_input": graph_input,
            "graph_input": graph_input,
            "inputs": resolved_inputs,
            "deps": dependency_results,
            "state": execution_state,
            "trigger": data.value,
            "dependency_payload": data.value,
        }
        await _put_task_event(data, graph, resolved_task, "start", input=task_input)

        try:
            if _approval_required(resolved_task) and not data.is_resume:
                await _put_task_event(data, graph, resolved_task, "approval_required", input=task_input)
                from agently.base import policy_approval
                from agently.core.orchestration.TriggerFlow.Control import TriggerFlowPauseSignal

                policy = merge_access_control_policy(dict(graph.policies), data.settings)
                gate_result = await policy_approval.async_gate(
                    data,
                    {
                        "source": "task_dag",
                        "capability": _approval_type(resolved_task),
                        "subject": resolved_task.title or resolved_task.id,
                        "risk": "task_dag_approval",
                        "payload": _approval_payload(resolved_task, task_input),
                        "policy": policy,
                        "lineage": {
                            "graph_id": graph.graph_id,
                            "task_id": resolved_task.id,
                        },
                        "execution_id": str(getattr(data.execution, "id", "")),
                    },
                    interrupt_id=f"task:{ graph.graph_id }:{ resolved_task.id }",
                    resume_to="self",
                    settings=data.settings,
                )
                if isinstance(gate_result, TriggerFlowPauseSignal):
                    return gate_result
                if gate_result.get("status") != "approved":
                    raise RuntimeError(str(gate_result.get("reason", "Task DAG approval was denied.")))
            output = data.resume.value if _is_approval_task(resolved_task) and data.is_resume else None
            if _is_approval_task(resolved_task) and output is None:
                output = {"status": "approved", "approved": True, "reason": "Task DAG approval passed."}
            if not _is_approval_task(resolved_task):
                context = TaskDAGContext(
                    graph=graph,
                    task=resolved_task,
                    task_input=task_input,
                    graph_input=graph_input,
                    dependency_results=dependency_results,
                    dependency_payload=data.value,
                    resources=data.resources.to_dict(),
                    runtime_data=data,
                )
                handler = resolver.resolve(resolved_task)
                output = await _execute_node_handler(data, graph, resolved_task, handler, context)
        except Exception as error:
            await _record_task_failure(data, graph, resolved_task, error, result_lock=result_lock)
            await _put_task_event(data, graph, resolved_task, "fail", error=str(error))
            if _fallback_terminal_action(resolved_task) == "skip":
                output = {"status": "skipped", "reason": str(error)}
                await _record_task_success(data, graph, resolved_task, output, result_lock=result_lock)
                await _put_task_event(data, graph, resolved_task, "skipped", output=output)
                return output
            raise

        await _record_task_success(data, graph, resolved_task, output, result_lock=result_lock)
        await _put_task_event(data, graph, resolved_task, "complete", output=output)
        return output

    return run_task


def _resolve_task_input_placeholders(
    value: Any,
    *,
    trigger_payload: Any,
    initial_input: Any,
    execution_state: Any,
    dependency_results: Mapping[str, Any],
    task_id: str,
) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _resolve_task_input_placeholders(
                item,
                trigger_payload=trigger_payload,
                initial_input=initial_input,
                execution_state=execution_state,
                dependency_results=dependency_results,
                task_id=task_id,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_task_input_placeholders(
                item,
                trigger_payload=trigger_payload,
                initial_input=initial_input,
                execution_state=execution_state,
                dependency_results=dependency_results,
                task_id=task_id,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _resolve_task_input_placeholders(
                item,
                trigger_payload=trigger_payload,
                initial_input=initial_input,
                execution_state=execution_state,
                dependency_results=dependency_results,
                task_id=task_id,
            )
            for item in value
        )
    if not isinstance(value, str):
        return value

    matches = list(_RUNTIME_PLACEHOLDER_RE.finditer(value))
    if not matches:
        return value

    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return _resolve_single_task_placeholder(
            matches[0].group(1),
            trigger_payload=trigger_payload,
            initial_input=initial_input,
            execution_state=execution_state,
            dependency_results=dependency_results,
            task_id=task_id,
        )

    def replace_match(match: re.Match[str]) -> str:
        resolved = _resolve_single_task_placeholder(
            match.group(1),
            trigger_payload=trigger_payload,
            initial_input=initial_input,
            execution_state=execution_state,
            dependency_results=dependency_results,
            task_id=task_id,
        )
        return _stringify_placeholder_value(resolved)

    return _RUNTIME_PLACEHOLDER_RE.sub(replace_match, value)


def _resolve_single_task_placeholder(
    expression: str,
    *,
    trigger_payload: Any,
    initial_input: Any,
    execution_state: Any,
    dependency_results: Mapping[str, Any],
    task_id: str,
) -> Any:
    parts = expression.strip().split(".", 1)
    root_name = parts[0].strip().upper()
    path = parts[1].strip() if len(parts) > 1 else ""

    if root_name == "INIT":
        root = initial_input
    elif root_name == "STATE":
        root = execution_state
    elif root_name == "DEPS":
        root = dependency_results
    elif root_name == "TRIGGER":
        root = trigger_payload
    else:
        raise ValueError(
            f"Dynamic task '{ task_id }' has unsupported runtime placeholder '${{{ expression }}}'. "
            "Use ${INIT...}, ${DEPS...}, ${STATE...}, or ${TRIGGER...}."
        )

    if not path:
        return root

    missing = object()
    value = DataLocator.locate_path_in_dict(root, path, "dot", default=missing)
    if value is missing:
        raise ValueError(
            f"Dynamic task '{ task_id }' runtime placeholder '${{{ expression }}}' "
            f"does not match an available runtime path."
        )
    return value


def _stringify_placeholder_value(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _make_finalize_handler(graph: TaskDAG):
    async def finalize(data: TriggerFlowRuntimeData):
        task_results = dict(data.get_state("task_results", {}) or {})
        artifact_refs = dict(data.get_state("artifact_refs", {}) or {})
        semantic_outputs = _collect_semantic_outputs(graph, task_results, artifact_refs)
        await data.async_set_state("semantic_outputs", semantic_outputs, emit=False)
        execution_result = {
            "graph_id": graph.graph_id,
            "task_results": task_results,
            "artifact_refs": artifact_refs,
            "semantic_outputs": semantic_outputs,
            "diagnostics": [dict(item) for item in graph.diagnostics],
        }
        await data.async_set_state("task_dag_execution", execution_result, emit=False)
        await _put_graph_event(data, graph, "complete", result=execution_result)
        return execution_result

    return finalize


async def _execute_node_handler(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    task: TaskDAGNode,
    handler: Any,
    context: TaskDAGContext,
):
    """Run a node handler, retrying on error when fallback.on_error == 'retry'.

    Retries use clamped exponential backoff. When attempts are exhausted the last
    error propagates to the caller's failure handling (which then applies the
    terminal action: skip or fail).
    """
    retry_config = _fallback_retry_config(task)
    if retry_config is None:
        return await _execute_handler(handler, context)
    max_attempts = retry_config["max_attempts"]
    attempt = 0
    while True:
        attempt += 1
        try:
            return await _execute_handler(handler, context)
        except Exception as error:
            if attempt >= max_attempts:
                raise
            delay = min(
                retry_config["backoff_max"],
                retry_config["backoff_base"] * (2 ** (attempt - 1)),
            )
            await _put_task_event(
                data,
                graph,
                task,
                "retry",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(error),
                next_delay=delay,
            )
            await asyncio.sleep(delay)


async def _execute_handler(handler: Any, context: TaskDAGContext):
    from agently.core.orchestration.TriggerFlow import TriggerFlow

    if isinstance(handler, TriggerFlow):
        execution = handler.create_execution(
            auto_close=True,
            runtime_resources=dict(context.resources),
            parent_run_context=context.runtime_data.chunk_run_context,
        )
        return await execution.async_start(dict(context.task_input))
    if callable(handler):
        return await default_stage_call_bridge.as_async(handler)(context)
    raise ValueError(
        f"Dynamic task '{ context.task.id }' kind '{ context.task.kind }' has no executable handler."
    )


async def _record_task_success(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    task: TaskDAGNode,
    output: Any,
    *,
    result_lock: asyncio.Lock,
) -> None:
    emit_done_graph = False
    async with result_lock:
        results = dict(data.get_state("task_results", {}) or {})
        statuses = dict(data.get_state("task_statuses", {}) or {})
        artifact_refs = dict(data.get_state("artifact_refs", {}) or {})
        results[task.id] = output
        statuses[task.id] = "completed"
        extracted_artifacts = _extract_artifact_refs(output)
        if extracted_artifacts:
            artifact_refs[task.id] = extracted_artifacts
        await data.async_set_state("task_results", results, emit=False)
        await data.async_set_state("task_statuses", statuses, emit=False)
        if extracted_artifacts:
            await data.async_set_state("artifact_refs", artifact_refs, emit=False)
        if len(results) == len(graph.tasks):
            emit_done_graph = True
    await data.async_emit(_done_task_event(task.id), {"task_id": task.id, "result": output})
    if emit_done_graph:
        await data.async_emit(_done_graph_event(graph.graph_id), dict(results))


async def _record_task_failure(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    task: TaskDAGNode,
    error: Exception,
    *,
    result_lock: asyncio.Lock,
) -> None:
    async with result_lock:
        failures = dict(data.get_state("task_failures", {}) or {})
        statuses = dict(data.get_state("task_statuses", {}) or {})
        failures[task.id] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        statuses[task.id] = "failed"
        await data.async_set_state("task_failures", failures, emit=False)
        await data.async_set_state("task_statuses", statuses, emit=False)
        failure_payload = failures[task.id]
    await data.async_emit(_failed_task_event(task.id), failure_payload)


async def _put_task_event(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    task: TaskDAGNode,
    action: Literal["start", "complete", "fail", "skipped", "approval_required", "retry"],
    **payload: Any,
) -> None:
    artifact_refs = payload.get("artifact_refs")
    output = payload.get("output")
    if artifact_refs is None:
        artifact_refs = _extract_artifact_refs(output)
    event_payload = {
        "graph_id": graph.graph_id,
        "graph_fingerprint": _graph_fingerprint(graph),
        "task_id": task.id,
        "task_kind": task.kind,
        "dependency_task_ids": list(task.depends_on),
        "dependency_signal_ids": [data.signal_id] if data.signal_id is not None else [],
        "node_result_status": action,
        "artifact_refs": artifact_refs or [],
        **payload,
    }
    await data.execution.async_put_into_stream(
        {
            "type": "task_dag.task",
            "action": action,
            "graph_id": graph.graph_id,
            "task_id": task.id,
            "task_kind": task.kind,
            "payload": event_payload,
        },
        _skip_contract_validation=True,
    )
    await data.execution._emit_runtime_event(
        "triggerflow.task_dag_node",
        message=f"TaskDAG node '{ task.id }' { action }.",
        payload=event_payload,
    )


async def _put_graph_event(
    data: TriggerFlowRuntimeData,
    graph: TaskDAG,
    action: Literal["complete"],
    **payload: Any,
) -> None:
    event_payload = {
        "graph_id": graph.graph_id,
        "graph_fingerprint": _graph_fingerprint(graph),
        "node_result_status": action,
        **payload,
    }
    await data.execution.async_put_into_stream(
        {
            "type": "task_dag.graph",
            "action": action,
            "graph_id": graph.graph_id,
            "payload": event_payload,
        },
        _skip_contract_validation=True,
    )
    await data.execution._emit_runtime_event(
        "triggerflow.task_dag_graph",
        message=f"TaskDAG graph '{ graph.graph_id }' { action }.",
        payload=event_payload,
    )


def _task_trigger(flow: "TriggerFlow", task: TaskDAGNode):
    if not task.depends_on:
        return flow.when(_start_task_event(task.id))
    if len(task.depends_on) == 1:
        return flow.when(_done_task_event(task.depends_on[0]))
    return flow.when(
        {"event": [_done_task_event(dependency) for dependency in task.depends_on]},
        mode="and",
    )
