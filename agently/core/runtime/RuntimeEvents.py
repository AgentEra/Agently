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
import json
import math
import time
from collections.abc import Mapping
from typing import Any

from agently.types.data import ErrorInfo, RuntimeEvent

__all__ = [
    "RuntimeEvent",
    "async_emit_action_flow_observation",
    "async_emit_model_requester_error",
    "async_emit_response_parser_observation",
    "async_emit_session_observation",
    "emit_session_observation",
]

_MODEL_REQUEST_TELEMETRY_KEYS_META = "_model_request_telemetry_keys"
_MODEL_REQUEST_STARTED_AT_META = "_model_request_started_at"


def _normalize_error(error: Any) -> tuple[ErrorInfo, BaseException | None]:
    if isinstance(error, BaseException):
        return ErrorInfo.from_exception(error), error
    if isinstance(error, ErrorInfo):
        return error, None
    if isinstance(error, dict):
        try:
            return ErrorInfo.model_validate(error), None
        except Exception:
            pass
    final_error = RuntimeError(str(error))
    return ErrorInfo.from_exception(final_error), final_error


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _extract_payload_meta(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(payload.get("meta"))


def _extract_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    request = _mapping(payload.get("request"))
    if request:
        return request
    request_data = _mapping(payload.get("request_data"))
    if request_data:
        return request_data
    return {}


def _extract_model(payload: Mapping[str, Any]) -> Any:
    meta = _extract_payload_meta(payload)
    if meta.get("model") is not None:
        return meta.get("model")
    request = _extract_request(payload)
    request_data = _mapping(request.get("data"))
    if request_data.get("model") is not None:
        return request_data.get("model")
    if request.get("model") is not None:
        return request.get("model")
    return payload.get("model")


def _extract_usage(payload: Mapping[str, Any]) -> Any:
    meta = _extract_payload_meta(payload)
    if meta.get("usage") is not None:
        return meta.get("usage")
    if payload.get("usage") is not None:
        return payload.get("usage")
    return None


def _usage_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _first_usage_number(usage: Mapping[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        number = _usage_number(usage.get(key))
        if number is not None:
            return number
    return None


def _normalize_usage(usage: Any) -> dict[str, int | float | None]:
    usage_mapping = _mapping(usage)
    prompt_tokens = _first_usage_number(
        usage_mapping,
        "prompt_tokens",
        "input_tokens",
        "input_token_count",
        "input",
    )
    completion_tokens = _first_usage_number(
        usage_mapping,
        "completion_tokens",
        "output_tokens",
        "output_token_count",
        "completion",
    )
    total_tokens = _first_usage_number(usage_mapping, "total_tokens", "total_token_count", "total")
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": _first_usage_number(usage_mapping, "input_tokens", "prompt_tokens", "input_token_count"),
        "output_tokens": _first_usage_number(
            usage_mapping,
            "output_tokens",
            "completion_tokens",
            "output_token_count",
        ),
    }


def _safe_text_length(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except Exception:
        return len(str(value))


def _first_text_length(payload: Mapping[str, Any], candidates: tuple[tuple[str, Any], ...]) -> tuple[int | None, str | None]:
    for source, value in candidates:
        if value is None:
            continue
        length = _safe_text_length(value)
        if length is not None:
            return length, source
    return None, None


def _explicit_estimated_length(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _estimate_usage_lengths(payload: Mapping[str, Any]) -> dict[str, int | str | None]:
    request = _extract_request(payload)
    input_chars = _explicit_estimated_length(payload, "estimated_input_chars")
    input_source = str(payload.get("estimated_input_source") or "estimated_input_chars") if input_chars is not None else None
    if input_chars is None:
        input_chars, input_source = _first_text_length(
            payload,
            (
                ("prompt_text", payload.get("prompt_text")),
                ("prompt", payload.get("prompt")),
                ("request", request or None),
                ("input", payload.get("input")),
                ("request_data", payload.get("request_data")),
            ),
        )
    output_chars = _explicit_estimated_length(payload, "estimated_output_chars")
    output_source = (
        str(payload.get("estimated_output_source") or "estimated_output_chars")
        if output_chars is not None
        else None
    )
    if output_chars is None:
        output_chars, output_source = _first_text_length(
            payload,
            (
                ("raw_text", payload.get("raw_text")),
                ("streamed_text", payload.get("streamed_text")),
                ("cleaned_text", payload.get("cleaned_text")),
                ("result", payload.get("result")),
                ("parsed_data", payload.get("parsed_data")),
                ("output", payload.get("output")),
                ("data", payload.get("data")),
            ),
        )
    return {
        "input_chars": input_chars,
        "input_source": input_source,
        "output_chars": output_chars,
        "output_source": output_source,
    }


def _build_usage_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_usage(_extract_usage(payload))
    usage_available = any(value is not None for value in normalized.values())
    return {
        "available": usage_available,
        "source": "provider" if usage_available else "estimated_lengths",
        "provider": normalized,
        "estimated_lengths": _estimate_usage_lengths(payload),
    }


def _extract_request_url(payload: Mapping[str, Any]) -> Any:
    request = _extract_request(payload)
    if request.get("request_url") is not None:
        return request.get("request_url")
    return payload.get("request_url")


def _duration_ms(run: Any) -> float | None:
    meta = getattr(run, "meta", None)
    if not isinstance(meta, dict):
        return None
    started_at = meta.get(_MODEL_REQUEST_STARTED_AT_META)
    if not isinstance(started_at, int | float):
        return None
    return round((time.perf_counter() - float(started_at)) * 1000, 3)


def _should_attach_model_request_telemetry(run: Any, telemetry_key: str) -> bool:
    meta = getattr(run, "meta", None)
    if not isinstance(meta, dict):
        return True
    emitted_keys = meta.setdefault(_MODEL_REQUEST_TELEMETRY_KEYS_META, [])
    if not isinstance(emitted_keys, list):
        emitted_keys = []
        meta[_MODEL_REQUEST_TELEMETRY_KEYS_META] = emitted_keys
    if telemetry_key in emitted_keys:
        return False
    emitted_keys.append(telemetry_key)
    return True


def attach_model_request_telemetry(
    payload: dict[str, Any],
    *,
    event_kind: str,
    run: Any = None,
    source: str | None = None,
    error: Any = None,
) -> dict[str, Any]:
    """Attach observation-only ModelRequest telemetry to an existing payload."""

    response_id = payload.get("response_id") or getattr(run, "response_id", None)
    attempt_index = payload.get("attempt_index")
    run_meta = getattr(run, "meta", None)
    if attempt_index is None and isinstance(run_meta, Mapping):
        attempt_index = run_meta.get("attempt_index")
    request_run_id = payload.get("request_run_id")
    if request_run_id is None:
        request_run_id = getattr(run, "parent_run_id", None)
    model_run_id = payload.get("model_run_id")
    if model_run_id is None:
        model_run_id = getattr(run, "run_id", None)
    telemetry_key = f"{ response_id }:{ attempt_index }:{ event_kind }"
    if not _should_attach_model_request_telemetry(run, telemetry_key):
        return payload

    meta = _extract_payload_meta(payload)
    provider = payload.get("provider") or meta.get("provider") or source
    provider_family = payload.get("provider_family") or payload.get("requester") or source
    error_info = None
    if error is not None:
        normalized_error, _ = _normalize_error(error)
        error_info = normalized_error.model_dump(mode="json")

    payload["model_request_telemetry"] = {
        "event_kind": event_kind,
        "telemetry_key": telemetry_key,
        "agent_name": payload.get("agent_name") or getattr(run, "agent_name", None),
        "response_id": response_id,
        "attempt_index": attempt_index,
        "request_run_id": request_run_id,
        "model_run_id": model_run_id,
        "provider": provider,
        "provider_family": provider_family,
        "model": _extract_model(payload),
        "request_url": _extract_request_url(payload),
        "duration_ms": _duration_ms(run),
        "usage": _extract_usage(payload),
        "usage_summary": _build_usage_summary(payload),
        "side_channel": payload.get("side_channel"),
        "error": error_info,
    }
    return payload


async def async_emit_model_requester_error(
    error: Any,
    *,
    source: str,
    request_data: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    run: Any = None,
) -> None:
    """Emit the official provider request error RuntimeEvent from core.

    Publication is observation-only. The owning request stream remains
    responsible for propagating the original provider error.
    """

    from agently.base import async_emit_runtime

    event_payload: dict[str, Any] = {}
    if payload:
        event_payload.update(payload)
    if request_data is not None:
        event_payload.setdefault("request_data", request_data)
    error_info, _ = _normalize_error(error)
    attach_model_request_telemetry(
        event_payload,
        event_kind="model.requester.error",
        run=run,
        source=source,
        error=error_info,
    )
    await async_emit_runtime(
        {
            "event_type": "model.requester.error",
            "source": source,
            "level": "ERROR",
            "message": error_info.message,
            "payload": event_payload,
            "error": error_info,
            "run": run,
        }
    )


async def async_emit_response_parser_observation(
    observation: dict[str, Any],
    *,
    agent_name: str,
    response_id: str,
    run: Any = None,
) -> None:
    """Map response parser observations to official RuntimeEvent records."""

    from agently.base import async_emit_runtime

    kind = str(observation.get("kind", ""))
    event_types = {
        "completed": "model.completed",
        "parse_failed": "model.parse_failed",
        "streaming": "model.streaming",
        "streaming_canceled": "model.streaming_canceled",
        "meta": "model.meta",
        "failed": "model.failed",
    }
    event_type = event_types.get(kind)
    if event_type is None:
        return
    payload = observation.get("payload")
    resolved_payload = dict(payload) if isinstance(payload, dict) else {}
    resolved_payload.setdefault("agent_name", agent_name)
    resolved_payload.setdefault("response_id", response_id)
    if event_type in {"model.completed", "model.meta"}:
        attach_model_request_telemetry(
            resolved_payload,
            event_kind=event_type,
            run=run,
            source=str(observation.get("source") or "AgentlyResponseParser"),
            error=observation.get("error"),
        )
    await async_emit_runtime(
        {
            "event_type": event_type,
            "source": str(observation.get("source") or "AgentlyResponseParser"),
            "level": observation.get("level", "INFO"),
            "message": observation.get("message"),
            "payload": resolved_payload,
            "error": observation.get("error"),
            "run": run,
        }
    )


async def async_emit_action_flow_observation(observation: dict[str, Any]) -> None:
    """Map an Action-owned bounded observation to official RuntimeEvent records."""

    from agently.base import async_emit_runtime
    from agently.types.data import ObservationEvent

    kind = str(observation.get("kind", ""))
    event_types = {
        "loop_started": "action.loop_started",
        "plan_ready": "action.plan_ready",
        "loop_failed": "action.loop_failed",
        "loop_completed": "action.loop_completed",
        "action_started": "action.started",
        "action_completed": "action.completed",
        "action_approval_required": "action.approval_required",
        "action_blocked": "action.blocked",
        "action_failed": "action.failed",
        "exchange_pending": "action.exchange_pending",
        "exchange_resolved": "action.exchange_resolved",
        "exchange_paused": "action.exchange_paused",
        "loop_paused": "action.loop_paused",
    }
    event_type = event_types.get(kind)
    if event_type is None:
        return

    payload = observation.get("payload")
    resolved_payload = dict(payload) if isinstance(payload, dict) else {}

    error = observation.get("error")
    primary_event = ObservationEvent(
        event_type=event_type,
        source=str(observation.get("source") or "ActionFlow"),
        level=observation.get("level", "INFO"),
        message=observation.get("message"),
        payload=resolved_payload,
        error=error,
        run=observation.get("run"),
    )
    await async_emit_runtime(primary_event)

    tool_aliases = {
        "action.loop_started": "tool.loop_started",
        "action.plan_ready": "tool.plan_ready",
        "action.loop_failed": "tool.loop_failed",
        "action.loop_completed": "tool.loop_completed",
    }
    if observation.get("compat_event_family", "tool") != "tool" or event_type not in tool_aliases:
        return
    await async_emit_runtime(
        ObservationEvent(
            event_type=tool_aliases[event_type],
            source=primary_event.source,
            level=primary_event.level,
            message=observation.get("compat_message") or primary_event.message,
            payload=primary_event.payload,
            error=primary_event.error,
            run=primary_event.run,
            meta={
                "compat_event_alias": True,
                "compat_alias_for": primary_event.event_type,
                "primary_event_id": primary_event.event_id,
                "compat_family": "tool",
            },
        )
    )


def emit_session_observation(observation: dict[str, Any]) -> None:
    """Map session observations to official RuntimeEvent records."""

    from agently.base import event_center

    event = _build_session_event(observation)
    if event is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        event_center.emit(event)
    else:
        event_center.emit_nowait(event)


async def async_emit_session_observation(observation: dict[str, Any]) -> None:
    """Map session observations to official async RuntimeEvent records."""

    from agently.base import async_emit_runtime

    event = _build_session_event(observation)
    if event is not None:
        await async_emit_runtime(event)


def _build_session_event(observation: dict[str, Any]):
    from agently.types.data import ObservationEvent

    kind = str(observation.get("kind", ""))
    event_types = {
        "activated": "session.activated",
        "deactivated": "session.deactivated",
        "applied_to_request": "session.applied_to_request",
        "context_appended": "session.context_appended",
    }
    event_type = event_types.get(kind)
    if event_type is None:
        return None
    payload = observation.get("payload")
    resolved_payload = dict(payload) if isinstance(payload, dict) else {}
    error = observation.get("error")
    return ObservationEvent(
        event_type=event_type,
        source=str(observation.get("source") or "SessionExtension"),
        level=observation.get("level", "INFO"),
        message=observation.get("message"),
        payload=resolved_payload,
        error=ErrorInfo.from_exception(error) if isinstance(error, BaseException) else error,
        run=observation.get("run"),
    )
