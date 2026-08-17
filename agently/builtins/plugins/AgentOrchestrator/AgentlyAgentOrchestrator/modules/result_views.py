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
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import suppress
from typing import Any, Literal, TYPE_CHECKING

from agently.core.model.StructuredOutputParser import (
    STRUCTURED_OUTPUT_FORMATS,
    parse_output_contract_dict,
)
from agently.core.model.ModelRequestResult import DEFAULT_SPECIFIC_EVENTS
from agently.core.application.AgentExecution.Stream import (
    AgentExecutionTextDeltaProjector,
    project_agent_execution_text_delta,
)
from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter

from .diagnostics import build_execution_meta

if TYPE_CHECKING:
    from agently.types.data import OutputValidateHandler, RunContext

    from .execution import AgentExecution


async def async_get_data(
    owner: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"] = "parsed",
    ensure_keys: list[str] | None = None,
    ensure_all_keys: bool | None = None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
    key_style: Literal["dot", "slash"] = "dot",
    max_retries: int = 3,
    raise_ensure_failure: bool = True,
    parent_run_context: "RunContext | None" = None,
) -> Any:
    data = await async_get_full_data(
        owner,
        type=type,
        ensure_keys=ensure_keys,
        ensure_all_keys=ensure_all_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
        parent_run_context=parent_run_context,
    )
    return _business_data_from_full_data(owner, data)


async def async_get_full_data(
    owner: "AgentExecution",
    *,
    type: Literal["original", "parsed", "all"] = "parsed",
    ensure_keys: list[str] | None = None,
    ensure_all_keys: bool | None = None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
    key_style: Literal["dot", "slash"] = "dot",
    max_retries: int = 3,
    raise_ensure_failure: bool = True,
    parent_run_context: "RunContext | None" = None,
) -> Any:
    return await owner.async_start(
        type=type,
        ensure_keys=ensure_keys,
        ensure_all_keys=ensure_all_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
        parent_run_context=parent_run_context,
    )


async def async_get_data_object(
    owner: "AgentExecution",
    *,
    ensure_keys: list[str] | None = None,
    validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
    key_style: Literal["dot", "slash"] = "dot",
    max_retries: int = 3,
    raise_ensure_failure: bool = True,
    parent_run_context: "RunContext | None" = None,
) -> Any:
    await owner.async_start(
        ensure_keys=ensure_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
        parent_run_context=parent_run_context,
    )
    if owner._ensure_long_output_enabled and owner._long_output_result_object is not None:
        return owner._long_output_result_object
    model_result = getattr(owner, "_model_request_result", None)
    if model_result is None:
        return None
    return await model_result.async_get_data_object(
        ensure_keys=ensure_keys,
        validate_handler=validate_handler,
        key_style=key_style,
        max_retries=max_retries,
        raise_ensure_failure=raise_ensure_failure,
    )


async def async_get_text(
    owner: "AgentExecution",
    *,
    parent_run_context: "RunContext | None" = None,
    **kwargs: Any,
) -> str:
    data = await owner.async_get_full_data(parent_run_context=parent_run_context, **kwargs)
    if isinstance(data, str):
        return data
    final_response = _final_response_from_data(data)
    if final_response:
        return final_response
    return json.dumps(DataFormatter.sanitize(data), ensure_ascii=False)


async def async_get_meta(owner: "AgentExecution") -> dict[str, Any]:
    if not owner._completed:
        await owner.async_start()
    owner._refresh_diagnostics()
    return build_execution_meta(owner)


async def get_async_generator(
    owner: "AgentExecution",
    type: Literal["delta", "instant", "streaming_parse", "all", "specific"] | str | None = "delta",
    content: Any = None,
    **kwargs: Any,
) -> AsyncGenerator[Any, None]:
    if content is not None and type is None:
        type = content
    text_projector = AgentExecutionTextDeltaProjector() if type in {"delta", "instant"} else None
    specific_events = _normalize_specific_events(kwargs.get("specific", DEFAULT_SPECIFIC_EVENTS))
    if owner._completed:
        for item in owner.stream.items:
            for projected in _project_stream_items(
                item,
                type,
                text_projector=text_projector,
                specific_events=specific_events,
            ):
                yield projected
        return
    queue: asyncio.Queue[Any] = asyncio.Queue()
    for item in owner.stream.items:
        await queue.put(item)
    owner.stream.queues.append(queue)
    start_task = asyncio.create_task(owner.async_start())
    start_task.add_done_callback(_retrieve_generator_start_exception)
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            for projected in _project_stream_items(
                item,
                type,
                text_projector=text_projector,
                specific_events=specific_events,
            ):
                yield projected
        await start_task
    finally:
        if queue in owner.stream.queues:
            owner.stream.queues.remove(queue)


def _project_stream_items(
    item: Any,
    type: Any,
    *,
    text_projector: AgentExecutionTextDeltaProjector | None = None,
    specific_events: set[str] | None = None,
) -> Generator[Any, None, None]:
    if type == "all":
        yield ("agent_execution", item)
        return
    if type == "specific":
        projected = _project_specific_item(item, specific_events=specific_events)
        if projected is not None:
            yield projected
        return
    if type == "delta":
        projected = (
            text_projector.project(item)
            if text_projector is not None
            else project_agent_execution_text_delta(item)
        )
        if projected is not None:
            yield projected
        return
    yield item
    if type == "instant":
        projected = _project_instant_delta_item(item, text_projector=text_projector)
        if projected is not None:
            yield projected


def _normalize_specific_events(specific: Any) -> set[str] | None:
    if specific is None:
        return None
    if isinstance(specific, str):
        return {specific}
    if isinstance(specific, (list, tuple, set)):
        return {str(item) for item in specific if item not in (None, "")}
    default_specific = DEFAULT_SPECIFIC_EVENTS
    if default_specific is None:
        return None
    if isinstance(default_specific, str):
        return {default_specific}
    return {str(item) for item in default_specific if item not in (None, "")}


def _project_specific_item(
    item: Any,
    *,
    specific_events: set[str] | None,
) -> tuple[str, Any] | None:
    meta = getattr(item, "meta", None)
    meta_map = meta if isinstance(meta, Mapping) else {}
    event = str(meta_map.get("specific_event") or "").strip()
    path = str(getattr(item, "path", "") or "")
    if not event:
        if getattr(item, "event_type", None) == "delta":
            event = "delta"
        elif path == "model.text":
            event = "done"
        elif path == "$status":
            event = "status"
    if not event:
        return None
    if specific_events is not None and event not in specific_events:
        return None
    if event.endswith("_delta") or event == "delta":
        value = getattr(item, "delta", None)
        if value is None:
            value = getattr(item, "value", None)
    else:
        value = getattr(item, "value", None)
    return event, value


def _final_response_from_data(data: Any) -> str:
    if not isinstance(data, Mapping):
        return ""
    if _looks_like_terminal_result(data):
        final_response = str(data.get("final_response") or "").strip()
        if final_response:
            return final_response
    result = data.get("result")
    if isinstance(result, Mapping) and _looks_like_terminal_result(result):
        return str(result.get("final_response") or "").strip()
    return ""


def _business_data_from_full_data(owner: "AgentExecution", data: Any) -> Any:
    terminal = _terminal_result_mapping(data)
    if terminal is None:
        return data
    if "final_result" not in terminal:
        return data
    final_result = terminal.get("final_result")
    if final_result in (None, "", [], {}):
        return data
    parsed = _parse_business_final_result(owner, final_result)
    if parsed is not _NO_BUSINESS_PARSE:
        return parsed
    return final_result


def _terminal_result_mapping(data: Any) -> Mapping[str, Any] | None:
    if isinstance(data, Mapping) and _looks_like_terminal_result(data):
        return data
    if isinstance(data, Mapping):
        result = data.get("result")
        if isinstance(result, Mapping) and _looks_like_terminal_result(result):
            return result
    return None


_NO_BUSINESS_PARSE = object()


def _parse_business_final_result(owner: "AgentExecution", value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return _NO_BUSINESS_PARSE
    prompt_snapshot = getattr(owner, "prompt_snapshot", None)
    prompt_data = prompt_snapshot if isinstance(prompt_snapshot, Mapping) else {}
    output_schema = prompt_data.get("output")
    if not isinstance(output_schema, Mapping) or not output_schema:
        return _NO_BUSINESS_PARSE
    output_format = str(prompt_data.get("output_format") or "").strip().lower()
    if output_format in {"", "json_object", "application/json", "auto"}:
        output_format = "json"
    if output_format not in STRUCTURED_OUTPUT_FORMATS:
        return _NO_BUSINESS_PARSE
    for format_name in [output_format, *([] if output_format == "json" else ["json"])]:
        parsed, _error = parse_output_contract_dict(
            text,
            output_schema=dict(output_schema),
            output_format=format_name,
        )
        if parsed is not None:
            return parsed
    return _NO_BUSINESS_PARSE


def _looks_like_terminal_result(data: Mapping[str, Any]) -> bool:
    if "status" not in data:
        return False
    if "accepted" in data or "artifact_status" in data:
        return True
    final_response = str(data.get("final_response") or "").strip()
    if final_response and data.get("task_id") not in (None, ""):
        return True
    if final_response and isinstance(data.get("taskboard"), Mapping):
        return True
    return False


def _project_instant_delta_item(
    item: Any,
    *,
    text_projector: AgentExecutionTextDeltaProjector | None = None,
) -> AgentExecutionStreamData | None:
    delta = text_projector.project(item) if text_projector is not None else project_agent_execution_text_delta(item)
    if delta is None:
        return None
    item_meta = getattr(item, "meta", None)
    meta_map = item_meta if isinstance(item_meta, Mapping) else {}
    stream_kind = meta_map.get("stream_kind")
    projection_meta: dict[str, Any] = {
        "stream_kind": "text_projection",
        "projection_source_path": str(getattr(item, "path", "") or ""),
        "projection_source_stream_kind": str(stream_kind) if stream_kind not in (None, "") else None,
    }
    for key in ("execution_id", "lineage"):
        if key in meta_map:
            projection_meta[key] = meta_map[key]
    event_type = getattr(item, "event_type", None)
    if event_type:
        projection_meta["projection_source_event_type"] = str(event_type)
    source = getattr(item, "source", None)
    if source:
        projection_meta["projection_source"] = str(source)
    return AgentExecutionStreamData(
        path="$delta",
        value=delta,
        delta=delta,
        event_type="delta",
        is_complete=False,
        full_data=getattr(item, "full_data", None),
        source="agent_execution",
        route=getattr(item, "route", None),
        stage_id=getattr(item, "stage_id", None),
        task_id=getattr(item, "task_id", None),
        action_id=getattr(item, "action_id", None),
        graph_id=getattr(item, "graph_id", None),
        meta=projection_meta,
    )


def _retrieve_generator_start_exception(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    with suppress(Exception):
        task.exception()


def sync_generator(owner: "AgentExecution", *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
    return default_stage_call_bridge.iter_sync(owner.get_async_generator(*args, **kwargs))
