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
import html
import json
from contextlib import suppress
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import PurePath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from agently.types.data import AgentExecutionStreamData
from agently.utils import DataFormatter


def project_agent_execution_text_delta(item: Any) -> str | None:
    """Project structured execution stream items onto the public text delta stream."""
    path = str(getattr(item, "path", "") or "")
    value = getattr(item, "value", None)
    source = str(getattr(item, "source", "") or "")
    meta = getattr(item, "meta", None)
    item_meta = meta if isinstance(meta, Mapping) else {}
    if str(item_meta.get("specific_event") or "") == "original_delta":
        return None
    if _is_retry_status_marker_source(path, value):
        return _format_retry_marker(value)
    if getattr(item, "event_type", None) == "delta":
        if (
            source == "agent_task"
            and path.startswith("agent_task.")
            and str(item_meta.get("stream_kind") or "") != "progress_delta"
        ):
            return None
        delta = getattr(item, "delta", None)
        if delta is None:
            return None
        return str(delta)
    return _project_done_item_text(path, value, item_meta, source=source)


def _project_done_item_text(path: str, value: Any, meta: Any, *, source: str) -> str | None:
    item_meta = meta if isinstance(meta, Mapping) else {}
    stream_kind = str(item_meta.get("stream_kind") or "")
    taskboard_status = _taskboard_status_text(path, value)
    if taskboard_status is not None:
        return taskboard_status
    if stream_kind == "progress":
        if str(item_meta.get("progress_source") or "") == "model":
            return None
        return _paragraph(_progress_text(value, item_meta))
    if stream_kind == "snapshot":
        return _paragraph(_snapshot_text(value, item_meta))
    if stream_kind == "heartbeat":
        return None
    if stream_kind == "guidance":
        return _paragraph(_guidance_text(value, item_meta))
    if stream_kind == "action_observation":
        return _paragraph(_action_observation_text(value, item_meta))
    if stream_kind == "phase":
        return _paragraph(_phase_text(value))
    if path == "agent_task.error":
        return _paragraph(_terminal_error_text(value))
    if path == "result" and source == "agent_task":
        return _paragraph(_terminal_result_text(value))
    return None


def _mapping_text(value: Any, key: str) -> str:
    if not isinstance(value, Mapping):
        return ""
    text = value.get(key)
    return str(text).strip() if text is not None else ""


def _paragraph(text: str | None) -> str | None:
    normalized = str(text or "").strip()
    if not normalized:
        return None
    return f"{normalized}\n\n"


def _terminal_error_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return f"Task failed: {_value_to_text(value)}"
    error_type = str(value.get("type") or "error").strip()
    message = str(value.get("message") or "").strip()
    if message:
        return f"Task failed: {error_type}: {message}"
    return f"Task failed: {error_type}"


def _progress_text(value: Any, meta: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        return ""
    explicit_message = _mapping_text(value, "message")
    if explicit_message:
        return explicit_message
    stage = str(meta.get("stage") or value.get("stage") or "").strip()
    iteration = value.get("iteration") if value.get("iteration") not in (None, "") else meta.get("iteration")
    label = _iteration_label(iteration)
    if stage == "context":
        return f"{label} preparing the working context."
    if stage in {"plan", "taskboard_plan"}:
        return f"{label} planning the next step."
    if stage == "execute":
        return f"{label} executing the selected step."
    if stage == "verify":
        return f"{label} verifying the result against the success criteria."
    if stage == "continue":
        return f"{label} more work remains; carrying the evidence into the next step."
    if stage == "completed":
        return "All success criteria are satisfied; the task is complete."
    if stage == "blocked":
        return f"{label} task hit a setback and needs operator guidance."
    if stage in {"replan", "replanned"}:
        return f"{label} more evidence is needed; planning another step."
    if stage in {"max_iterations", "capability_unavailable", "timed_out"}:
        return _mapping_text(value, "message")
    return _mapping_text(value, "message")


def _snapshot_text(value: Any, meta: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        return ""
    stage = str(meta.get("stage") or value.get("stage") or "").strip()
    iteration = meta.get("iteration") if meta.get("iteration") not in (None, "") else value.get("iteration")
    label = _iteration_label(iteration)
    snapshot = value.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    if stage == "context":
        count = snapshot.get("context_item_count")
        if count not in (None, ""):
            return f"{label} context is ready with {count} item(s)."
        return f"{label} context is ready."
    if stage in {"plan", "taskboard_plan"}:
        return f"{label} plan ready."
    if stage == "execution":
        execution_result = snapshot.get("execution_result")
        if _status_is_failed(execution_result):
            return f"{label} execution hit a setback; evidence was captured for recovery."
        return f"{label} execution evidence was captured."
    if stage == "verification":
        if snapshot.get("is_complete") is True:
            return f"{label} verification passed."
        if snapshot.get("requires_block") is True:
            return f"{label} verification found an issue that needs operator guidance."
        return f"{label} verification needs another step."
    return _mapping_text(value, "message")


def _phase_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    phase = str(value.get("phase") or "").strip()
    if not phase:
        return ""
    iteration = value.get("iteration")
    status = str(value.get("status") or "").strip()
    label = _iteration_label(iteration)
    if phase == "configured":
        return ""
    if phase == "planned":
        return f"{label} plan accepted."
    if phase == "executing":
        return f"{label} execution started."
    if phase == "evidence_recorded":
        return f"{label} evidence recorded."
    if phase == "verified":
        return ""
    if phase == "guarded":
        return ""
    if phase == "replanned":
        return f"{label} continuing with a revised plan."
    if phase == "terminal":
        if status == "completed":
            return "Task completed."
        if status:
            return f"Task ended with status {status}."
        return "Task ended."
    suffix = f" ({status})" if status else ""
    return f"{label} {phase.replace('_', ' ')}{suffix}."


def _guidance_text(value: Any, meta: Mapping[str, Any]) -> str:
    status = str(meta.get("guidance_status") or "").strip()
    if not status and isinstance(value, Mapping):
        status = str(value.get("status") or "").strip()
    if status in {"received", "queued", "forwarded"}:
        return "Guidance received; it will be applied at the next safe task boundary."
    if status == "applied":
        return "Guidance applied at the next safe task boundary."
    if status == "received_after_terminal":
        return "Guidance received after the task had already ended; it was recorded for audit."
    if status in {"ignored", "not_applied"}:
        return "Guidance was recorded but not applied to this task run."
    return "Guidance update recorded."


def _iteration_label(iteration: Any) -> str:
    return f"Iteration {iteration}:" if iteration not in (None, "") else "Task:"


def _taskboard_status_text(path: str, value: Any) -> str | None:
    projection = _taskboard_status_projection(path, value)
    if projection is None:
        return None
    return _format_taskboard_status_projection(projection)


def _taskboard_status_projection(path: str, value: Any) -> dict[str, Any] | None:
    if not path.startswith("agent_task.taskboard.") or not isinstance(value, Mapping):
        return None
    title: str
    revision: Any
    schedule: Any
    card_results: Any
    if path == "agent_task.taskboard.plan":
        title = "TaskBoard planned"
        revision = value.get("revision")
        schedule = None
        card_results = None
    elif path.startswith("agent_task.taskboard.tick.") and path.endswith(".scheduled"):
        title = _taskboard_tick_title(path, "scheduled")
        revision = value.get("revision")
        schedule = value.get("schedule")
        card_results = value.get("card_results")
    elif path.startswith("agent_task.taskboard.tick.") and path.endswith(".completed"):
        title = _taskboard_tick_title(path, "updated")
        revision = value.get("revision")
        schedule = value.get("schedule")
        card_results = value.get("card_results")
    else:
        return None
    cards = _taskboard_display_cards(revision, schedule, card_results)
    if not cards:
        return None
    explicit_board_id = _mapping_text(revision, "board_id")
    board_id = explicit_board_id or "taskboard"
    revision_id = _mapping_text(revision, "revision_id") or _mapping_text(schedule, "revision_id")
    return {
        "title": title,
        "board_id": board_id,
        "board_id_explicit": bool(explicit_board_id),
        "revision_id": revision_id,
        "cards": cards,
        "counts": _taskboard_status_counts(cards),
    }


def _taskboard_tick_title(path: str, fallback: str) -> str:
    parts = path.split(".")
    for index, part in enumerate(parts):
        if part == "tick" and index + 1 < len(parts):
            tick_index = parts[index + 1].strip()
            if tick_index:
                return f"TaskBoard tick {tick_index} {fallback}"
    return f"TaskBoard {fallback}"


def _format_taskboard_status_block(
    *,
    title: str,
    revision: Any,
    schedule: Any,
    card_results: Any,
) -> str | None:
    cards = _taskboard_display_cards(revision, schedule, card_results)
    if not cards:
        return None
    board_id = _mapping_text(revision, "board_id") or "taskboard"
    revision_id = _mapping_text(revision, "revision_id") or _mapping_text(schedule, "revision_id")
    return _format_taskboard_status_projection(
        {
            "title": title,
            "board_id": board_id,
            "revision_id": revision_id,
            "cards": cards,
            "counts": _taskboard_status_counts(cards),
        }
    )


def _format_taskboard_status_projection(projection: Mapping[str, Any]) -> str | None:
    cards = projection.get("cards")
    if not isinstance(cards, Sequence) or isinstance(cards, str | bytes | bytearray) or not cards:
        return None
    header = _taskboard_projection_header(projection)
    lines = [
        header,
        f"📊 Overall progress: {_taskboard_progress_summary(projection)}",
        "",
        "| State | Card | Task |",
        "| --- | --- | --- |",
    ]
    max_rows = 8
    for card in cards[:max_rows]:
        if not isinstance(card, Mapping):
            continue
        state = card["display_state"]
        lines.append(
            "| "
            + _markdown_table_cell(_taskboard_state_label(state))
            + " | "
            + f"`{_markdown_inline_code(card['id'])}`"
            + " | "
            + _markdown_table_cell(card.get("objective") or card["id"])
            + " |"
        )
    omitted = len(cards) - max_rows
    if omitted > 0:
        lines.append(f"| ... | ... | {omitted} more cards omitted. |")
    return "\n".join(lines) + "\n\n"


def _format_taskboard_status_update(
    projection: Mapping[str, Any],
    previous_cards: Mapping[str, Mapping[str, Any]],
) -> str | None:
    cards = projection.get("cards")
    if not isinstance(cards, Sequence) or isinstance(cards, str | bytes | bytearray) or not cards:
        return None
    lines = [
        _taskboard_update_header(projection),
        f"📊 Overall progress: {_taskboard_progress_summary(projection)}",
        "",
    ]
    changes: list[str] = []
    for raw_card in cards:
        if not isinstance(raw_card, Mapping):
            continue
        card_id = _clean_taskboard_text(raw_card.get("id"))
        if not card_id:
            continue
        state = str(raw_card.get("display_state") or "not_started")
        previous = previous_cards.get(card_id)
        previous_state = str(previous.get("display_state") or "") if isinstance(previous, Mapping) else ""
        objective = _compact_inline_text(raw_card.get("objective") or card_id, max_chars=160)
        if not previous:
            changes.append(f"- {_taskboard_state_label(state)} `{_markdown_inline_code(card_id)}` {objective}")
        elif previous_state != state:
            changes.append(
                "- "
                + f"{_taskboard_state_label(state)} `{_markdown_inline_code(card_id)}` {objective} "
                + f"(was {_taskboard_state_label(previous_state)})"
            )
    if changes:
        lines.append("Changes:")
        lines.extend(changes[:8])
        omitted = len(changes) - 8
        if omitted > 0:
            lines.append(f"- ... {omitted} more changes omitted.")
    else:
        lines.append("No card state changes.")
    return "\n".join(lines) + "\n\n"


def _taskboard_projection_header(projection: Mapping[str, Any]) -> str:
    title = _clean_taskboard_text(projection.get("title")) or "TaskBoard"
    board_id = _clean_taskboard_text(projection.get("board_id")) or "taskboard"
    revision_id = _clean_taskboard_text(projection.get("revision_id"))
    header = f"📋 {title} `{_markdown_inline_code(board_id)}`"
    if revision_id:
        header += f" - revision `{_markdown_inline_code(revision_id)}`"
    return header


def _taskboard_update_header(projection: Mapping[str, Any]) -> str:
    title = _clean_taskboard_text(projection.get("title")) or "TaskBoard"
    board_id = _clean_taskboard_text(projection.get("board_id")) or "taskboard"
    revision_id = _clean_taskboard_text(projection.get("revision_id"))
    header = f"🔄 TaskBoard update — {title} `{_markdown_inline_code(board_id)}`"
    if revision_id:
        header += f" - revision `{_markdown_inline_code(revision_id)}`"
    return header


def _taskboard_progress_summary(projection: Mapping[str, Any]) -> str:
    cards = projection.get("cards")
    counts_value = projection.get("counts")
    counts = counts_value if isinstance(counts_value, Mapping) else {}
    total = len(cards) if isinstance(cards, Sequence) and not isinstance(cards, str | bytes | bytearray) else 0
    summary_bits = [
        f"{counts.get('completed', 0)}/{total} completed",
        f"{counts.get('in_progress', 0)} in progress",
        f"{counts.get('not_started', 0)} not started",
    ]
    if counts.get("failed", 0):
        summary_bits.append(f"{counts['failed']} failed")
    if counts.get("degraded", 0):
        summary_bits.append(f"{counts['degraded']} degraded")
    return " - ".join(summary_bits)


def _taskboard_card_state_map(projection: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    cards = projection.get("cards")
    result: dict[str, Mapping[str, Any]] = {}
    if not isinstance(cards, Sequence) or isinstance(cards, str | bytes | bytearray):
        return result
    for raw_card in cards:
        if not isinstance(raw_card, Mapping):
            continue
        card_id = _clean_taskboard_text(raw_card.get("id"))
        if card_id:
            result[card_id] = {
                "display_state": _clean_taskboard_text(raw_card.get("display_state") or "not_started"),
                "objective": _clean_taskboard_text(raw_card.get("objective") or ""),
            }
    return result


def _taskboard_projection_with_previous_objectives(
    projection: Mapping[str, Any],
    previous_cards: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    cards = projection.get("cards")
    if not isinstance(cards, Sequence) or isinstance(cards, str | bytes | bytearray):
        return dict(projection)
    merged_cards: list[Any] = []
    for raw_card in cards:
        if not isinstance(raw_card, Mapping):
            merged_cards.append(raw_card)
            continue
        card = dict(raw_card)
        card_id = _clean_taskboard_text(card.get("id"))
        previous = previous_cards.get(card_id)
        if (
            not _clean_taskboard_text(card.get("objective"))
            and isinstance(previous, Mapping)
            and _clean_taskboard_text(previous.get("objective"))
        ):
            card["objective"] = _clean_taskboard_text(previous.get("objective"))
        merged_cards.append(card)
    merged = dict(projection)
    merged["cards"] = merged_cards
    return merged


def _taskboard_display_cards(revision: Any, schedule: Any, card_results: Any) -> list[dict[str, Any]]:
    revision_view = revision if isinstance(revision, Mapping) else {}
    schedule_view = schedule if isinstance(schedule, Mapping) else {}
    result_view = _taskboard_result_view(revision_view, card_results)
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    graph_value = revision_view.get("graph")
    graph: Mapping[str, Any] = graph_value if isinstance(graph_value, Mapping) else {}
    for raw_card in _sequence_of_mappings(graph.get("cards")):
        card_id = _clean_taskboard_text(raw_card.get("id") or raw_card.get("card_id"))
        if not card_id:
            continue
        seen.add(card_id)
        cards.append(
            {
                "id": card_id,
                "objective": _clean_taskboard_text(raw_card.get("objective") or raw_card.get("goal")),
                "status": _clean_taskboard_text(raw_card.get("status")),
                "failure_policy": _clean_taskboard_text(raw_card.get("failure_policy")),
                "display_state": _taskboard_display_state(card_id, raw_card, result_view.get(card_id), schedule_view),
            }
        )
    for card_id in _taskboard_schedule_ids(schedule_view) + list(result_view):
        if card_id in seen:
            continue
        seen.add(card_id)
        result = result_view.get(card_id)
        cards.append(
            {
                "id": card_id,
                "objective": "",
                "status": _clean_taskboard_text(result.get("status")) if isinstance(result, Mapping) else "",
                "failure_policy": "",
                "display_state": _taskboard_display_state(card_id, {}, result, schedule_view),
            }
        )
    return cards


def _taskboard_result_view(revision: Mapping[str, Any], card_results: Any) -> dict[str, Mapping[str, Any]]:
    raw_results = card_results if isinstance(card_results, Mapping) else revision.get("card_results")
    results: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_results, Mapping):
        for raw_id, raw_result in raw_results.items():
            card_id = _clean_taskboard_text(raw_id)
            if not card_id:
                continue
            if isinstance(raw_result, Mapping):
                results[card_id] = raw_result
            elif raw_result not in (None, ""):
                results[card_id] = {"status": str(raw_result)}
    raw_statuses = revision.get("card_result_statuses")
    if isinstance(raw_statuses, Mapping):
        for raw_id, raw_status in raw_statuses.items():
            card_id = _clean_taskboard_text(raw_id)
            if card_id and card_id not in results:
                results[card_id] = {"status": str(raw_status)}
    return results


def _taskboard_schedule_ids(schedule: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("completed_card_ids", "runnable_card_ids", "blocked_card_ids"):
        for card_id in _sequence_of_strings(schedule.get(key)):
            if card_id not in ids:
                ids.append(card_id)
    return ids


def _taskboard_display_state(
    card_id: str,
    card: Mapping[str, Any],
    result: Any,
    schedule: Mapping[str, Any],
) -> str:
    result_status = _normalize_taskboard_status(result.get("status")) if isinstance(result, Mapping) else ""
    card_status = _normalize_taskboard_status(card.get("status"))
    failure_policy = _normalize_taskboard_status(card.get("failure_policy"))
    metadata: Mapping[str, Any] = {}
    if isinstance(result, Mapping):
        metadata_value = result.get("metadata")
        if isinstance(metadata_value, Mapping):
            metadata = metadata_value
    if result_status in {"completed", "accepted", "succeeded", "success", "ok"}:
        return "completed"
    if result_status in {"degraded", "partial", "setback", "skipped", "deferred"}:
        return "degraded"
    if result_status in {"failed", "error", "timeout", "timed_out", "cancelled", "blocked"}:
        if failure_policy in {"optional", "degradable"} or metadata.get("deferred") is True:
            return "degraded"
        return "failed"
    completed_ids = set(_sequence_of_strings(schedule.get("completed_card_ids")))
    runnable_ids = set(_sequence_of_strings(schedule.get("runnable_card_ids")))
    if card_id in completed_ids or card_status in {"completed", "accepted", "succeeded", "success", "ok"}:
        return "completed"
    if card_status in {"degraded", "partial", "setback", "skipped", "deferred"}:
        return "degraded"
    if card_status in {"failed", "error", "timeout", "timed_out", "cancelled"}:
        return "failed"
    if card_id in runnable_ids or card_status in {"running", "ready", "active", "in_progress"}:
        return "in_progress"
    return "not_started"


def _taskboard_status_counts(cards: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"not_started": 0, "in_progress": 0, "completed": 0, "failed": 0, "degraded": 0}
    for card in cards:
        state = str(card.get("display_state") or "not_started")
        counts[state if state in counts else "not_started"] += 1
    return counts


def _taskboard_state_label(state: str) -> str:
    labels = {
        "not_started": "⏳ Not started",
        "in_progress": "🔄 In progress",
        "completed": "✅ Completed",
        "failed": "❌ Failed",
        "degraded": "⚠️ Degraded",
    }
    return labels.get(state, labels["not_started"])


def _normalize_taskboard_status(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _clean_taskboard_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return " ".join(str(value).split()).strip()


def _sequence_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list | tuple):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _sequence_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    result: list[str] = []
    for item in value:
        text = _clean_taskboard_text(item)
        if text:
            result.append(text)
    return result


_FLAT_SNAPSHOT_STAGES = {"context", "plan", "execution", "verification"}


def _flat_snapshot_projection(path: str, value: Any, meta: Mapping[str, Any]) -> dict[str, Any] | None:
    if not path.startswith("agent_task.iteration.") or ".snapshot." not in path or not isinstance(value, Mapping):
        return None
    stage = str(meta.get("stage") or value.get("stage") or "").strip()
    if not stage:
        parts = path.split(".")
        try:
            stage = parts[parts.index("snapshot") + 1]
        except (ValueError, IndexError):
            stage = ""
    if stage not in _FLAT_SNAPSHOT_STAGES:
        return None
    iteration = value.get("iteration") if value.get("iteration") not in (None, "") else meta.get("iteration")
    if iteration in (None, ""):
        parts = path.split(".")
        try:
            iteration = parts[parts.index("iteration") + 1]
        except (ValueError, IndexError):
            iteration = ""
    snapshot = value.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    task_id = _clean_taskboard_text(meta.get("task_id"))
    return {
        "iteration": iteration,
        "stage": stage,
        "task_id": task_id,
        "snapshot": snapshot,
        "message": _mapping_text(value, "message"),
    }


def _flat_snapshot_detail(stage: str, snapshot: Mapping[str, Any], message: str = "") -> str:
    if stage == "context":
        count = snapshot.get("context_item_count")
        if count not in (None, ""):
            return f"prepared the working context with {count} item(s)"
        return "Context ready."
    if stage == "plan":
        for key in ("step_instruction", "expected_evidence", "rationale"):
            text = _compact_inline_text(snapshot.get(key), max_chars=180)
            if text:
                return text
        return "selected the next bounded step"
    if stage == "execution":
        execution_result = snapshot.get("execution_result")
        if isinstance(execution_result, Mapping):
            for key in ("progress_message", "short_summary", "step_result", "answer", "summary"):
                text = _compact_inline_text(execution_result.get(key), max_chars=180)
                if text:
                    return text
            remaining = _sequence_of_strings(execution_result.get("remaining_work"))
            if remaining:
                return "recorded remaining work: " + _compact_inline_text(remaining[0], max_chars=150)
        if _status_is_failed(execution_result):
            return "hit a setback and captured recovery evidence"
        return "captured execution evidence"
    if stage == "verification":
        process_summary = snapshot.get("process_summary")
        if isinstance(process_summary, Mapping):
            for key in ("progress_message", "verification_summary", "short_summary"):
                text = _compact_inline_text(process_summary.get(key), max_chars=180)
                if text:
                    return text
        for key in ("progress_message", "verification_summary", "reason", "failure_analysis"):
            text = _compact_inline_text(snapshot.get(key), max_chars=180)
            if text:
                return text
        missing = _sequence_of_strings(snapshot.get("missing_criteria"))
        if missing:
            return "needs: " + _compact_inline_text(missing[0], max_chars=150)
        if snapshot.get("is_complete") is True:
            return "verification passed"
        return "verification needs another step"
    return message


def _flat_action_sentence(text: str) -> str:
    normalized = _compact_inline_text(text, max_chars=220)
    if not normalized:
        return ""
    if normalized.endswith((".", "!", "?")):
        return normalized
    return normalized + "."


def _format_flat_plan_text(
    *,
    iteration: Any,
    snapshot: Mapping[str, Any],
    previous_action: str | None,
) -> str:
    label = _iteration_label(iteration).rstrip(":")
    plan = _flat_snapshot_detail("plan", snapshot)
    expected = _compact_inline_text(snapshot.get("expected_evidence"), max_chars=180)
    lines = [f"🧭 {label} — Plan ready"]
    if previous_action:
        lines.append(f"Previous completed action: {_flat_action_sentence(previous_action)}")
    if plan:
        lines.append(f"Current action plan: {_flat_action_sentence(plan)}")
    if expected and expected != plan:
        lines.append(f"Expected evidence: {_flat_action_sentence(expected)}")
    return "\n".join(lines)


def _format_flat_execution_text(*, iteration: Any, detail: str, failed: bool) -> str:
    label = _iteration_label(iteration).rstrip(":")
    if failed:
        return f"❌ {label} — Step failed: {_flat_action_sentence(detail)} Evidence was captured for recovery."
    return f"✅ {label} — Step completed: {_flat_action_sentence(detail)} The execution evidence was captured."


def _format_flat_verification_text(*, iteration: Any, snapshot: Mapping[str, Any], detail: str) -> str:
    label = _iteration_label(iteration).rstrip(":")
    if snapshot.get("is_complete") is True:
        return f"🔎 {label} — Verification passed: {_flat_action_sentence(detail)}"
    if snapshot.get("requires_block") is True:
        return f"🔎 {label} — Verification needs guidance: {_flat_action_sentence(detail)}"
    next_steps = _sequence_of_strings(snapshot.get("next_step_requirements"))
    if next_steps:
        return f"🔎 {label} — Verification needs another step: {_flat_action_sentence(next_steps[0])}"
    return f"🔎 {label} — Verification needs another step: {_flat_action_sentence(detail)}"


def _format_flat_terminal_summary(value: Any, completed_actions: Sequence[str]) -> str:
    base_result = _terminal_result_text(value)
    base_lines = base_result.splitlines()
    heading = base_lines[0] if base_lines else "Task finished"
    if isinstance(value, Mapping) and value.get("accepted") is True:
        completion_state = "accepted"
    elif isinstance(value, Mapping) and value.get("accepted") is False:
        completion_state = "not accepted"
    else:
        completion_state = str(value.get("status") or "finished") if isinstance(value, Mapping) else "finished"
    lines = [
        heading,
        f"📊 Overall progress: {len(completed_actions)} completed stages · {completion_state}",
    ]
    if completed_actions:
        lines.append("What was done:")
        for action in completed_actions[-6:]:
            lines.append(f"- {_flat_action_sentence(action)}")
    if len(base_lines) > 1:
        lines.append("")
        lines.append("Result:")
        lines.extend(base_lines[1:])
    return "\n".join(lines)


def _markdown_table_cell(value: Any) -> str:
    text = _compact_inline_text(value, max_chars=96)
    return text.replace("|", "\\|") or "-"


def _markdown_inline_code(value: Any) -> str:
    return _compact_inline_text(value, max_chars=80).replace("`", "'") or "-"


def _action_observation_text(value: Any, meta: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        return ""
    phase = str(meta.get("phase") or "").strip().lower()
    if phase == "planned":
        if str(meta.get("strategy") or "").strip().lower() != "flat":
            return ""
        return _planned_action_batch_text(value)
    action_id = str(value.get("action_id") or value.get("action_call_id") or "action").strip()
    action_label = action_id or "action"
    kind = str(value.get("kind") or value.get("action_type") or "").strip()
    label = f"{action_label} ({kind})" if kind else action_label
    display_label = _markdown_inline_code(label)
    status = str(value.get("status") or "").strip().lower()
    if phase == "started" or status == "started":
        return f"🔄 `{display_label}` — Running"
    if phase == "failed" or status in {"failed", "error", "timeout", "timed_out", "blocked"}:
        text = f"❌ `{display_label}` — Failed"
        error = _action_error_text(value.get("error"))
        if error:
            text += f": {error}"
        return text
    if phase == "completed" or value.get("success") is True or status in {"success", "succeeded", "completed", "ok"}:
        text = f"✅ `{display_label}` — Completed"
        output_summary = _action_output_text(value)
        if output_summary:
            text += f": {output_summary}"
        refs_text = _action_refs_text(
            value,
            allow_open_paths=meta.get("trusted_ref_projection") is True,
        )
        if refs_text:
            text += f"\n📎 References: {refs_text}"
        return text
    return f"Action update: {label} ({status})." if status else f"Action update: {label}."


def _planned_action_batch_text(value: Mapping[str, Any]) -> str:
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, Sequence) or isinstance(raw_actions, str | bytes | bytearray):
        return ""
    actions = [item for item in raw_actions if isinstance(item, Mapping)]
    if not actions:
        return ""
    if len(actions) == 1:
        heading = "🚀 Next Action"
    elif value.get("parallel") is True:
        heading = f"🚀 Next parallel Action batch — {len(actions)} tasks"
    elif value.get("parallel") is False:
        heading = f"🚀 Next ordered Action batch — {len(actions)} tasks"
    else:
        heading = f"🚀 Next Action batch — {len(actions)} tasks"
    lines = [heading]
    for action in actions[:8]:
        action_id = _markdown_inline_code(action.get("action_id") or action.get("action_call_id") or "action")
        purpose = _compact_inline_text(action.get("purpose"), max_chars=160) or "Run the validated Action"
        lines.append(f"- ⏳ `{action_id}` — {purpose}")
    if len(actions) > 8:
        lines.append(f"- … {len(actions) - 8} more validated Actions")
    return "\n".join(lines)


def _action_output_text(value: Mapping[str, Any]) -> str:
    raw = value.get("output_summary")
    if raw in (None, "", [], {}):
        raw = value.get("result_summary") or value.get("result_preview")
    if isinstance(raw, Mapping):
        path = raw.get("path")
        if path not in (None, ""):
            display_path = _reference_display_label(path)
            if raw.get("readable") is True:
                return f"read {display_path}"
            if raw.get("writable") is True or raw.get("ok") is True:
                return f"wrote {display_path}"
            return f"path {display_path}"
        if raw.get("content") not in (None, ""):
            return "content preview available"
        if raw.get("items") not in (None, "", [], {}):
            items = raw.get("items")
            if isinstance(items, Sequence) and not isinstance(items, str | bytes | bytearray):
                return f"returned {len(items)} item(s)"
        keys = [str(key) for key, item in raw.items() if item not in (None, "", [], {})]
        return "structured result: " + ", ".join(keys[:4]) if keys else ""
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
        return f"returned {len(raw)} item(s)"
    return _compact_inline_text(raw, max_chars=180)


def _action_error_text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return _compact_inline_text(value, max_chars=180)
    return "structured error details are available in the full result stream"


def _reference_display_label(value: Any) -> str:
    raw = str(value or "").strip()
    if "://" in raw:
        raw = urlsplit(raw).path
    else:
        raw = raw.split("?", 1)[0].split("#", 1)[0]
    label = PurePath(raw).name or "reference"
    return _compact_inline_text(label, max_chars=80).replace("[", "\\[").replace("]", "\\]")


def _action_refs_text(value: Mapping[str, Any], *, allow_open_paths: bool = False) -> str:
    refs: list[str] = []
    for key in ("artifact_refs", "file_refs", "source_refs"):
        raw_refs = value.get(key)
        if not isinstance(raw_refs, list):
            continue
        for item in raw_refs:
            if not isinstance(item, Mapping):
                continue
            raw_label = str(
                item.get("display_path")
                or item.get("path")
                or item.get("value")
                or item.get("id")
                or "reference"
            ).strip()
            label = _reference_display_label(raw_label)
            open_path = str(item.get("open_path") or "").strip()
            if (
                allow_open_paths
                and item.get("open_path_verified") is True
                and open_path
                and "\n" not in open_path
                and "\r" not in open_path
                and ">" not in open_path
            ):
                ref_text = f"[{label}](<{open_path}>)"
            else:
                ref_text = label
            if ref_text and ref_text not in refs:
                refs.append(ref_text)
    visible_refs = refs[:3]
    text = ", ".join(visible_refs)
    if len(refs) > len(visible_refs):
        text += f" (+{len(refs) - len(visible_refs)} more)"
    return text


def _compact_inline_text(value: Any, *, max_chars: int = 280) -> str:
    if value in (None, "", [], {}):
        return ""
    text = _value_to_text(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 14)].rstrip() + " [truncated]"


def _terminal_result_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        if isinstance(value, str):
            text = _compact_inline_text(value, max_chars=600)
        elif value in (None, "", [], {}):
            text = ""
        else:
            text = "Structured final result is available in the full result stream."
        return f"Task finished\nFinal result:\n{text}" if text else "Task finished"
    status = str(value.get("status") or "").strip()
    accepted = value.get("accepted")
    artifact_status = str(value.get("artifact_status") or "").strip()
    final_response_value = value.get("final_response")
    final_response = (
        _compact_inline_text(final_response_value, max_chars=600)
        if isinstance(final_response_value, str)
        else ""
    )
    heading = _terminal_result_heading(status=status, accepted=accepted, artifact_status=artifact_status)
    if final_response:
        return f"{heading}\n{final_response}"
    if final_response_value not in (None, "", [], {}):
        return f"{heading}\nStructured final result is available in the full result stream."
    final_result = value.get("final_result")
    reason_value = value.get("reason")
    reason = _compact_inline_text(reason_value, max_chars=480) if isinstance(reason_value, str) else ""
    if final_result not in (None, ""):
        if isinstance(final_result, str):
            return f"{heading}\nFinal result:\n{_compact_inline_text(final_result, max_chars=600)}"
        return f"{heading}\nStructured final result is available in the full result stream."
    if reason:
        return f"{heading}: {reason}"
    if reason_value not in (None, "", [], {}):
        return f"{heading}: structured failure details are available in the full result stream."
    return heading


def _terminal_result_heading(*, status: str, accepted: Any, artifact_status: str) -> str:
    normalized_artifact = artifact_status.lower().replace("-", "_")
    if accepted is True and normalized_artifact == "degraded":
        return "⚠️ Task completed with limitations"
    if accepted is False and normalized_artifact in {"partial", "partial_success"}:
        return "⚠️ Task partially completed"
    if status == "completed" or accepted is True:
        return "🎯 Task completed"
    if status.lower() in {
        "blocked",
        "failed",
        "error",
        "timeout",
        "timed_out",
        "cancelled",
        "capability_unavailable",
    } or accepted is False:
        return "❌ Task failed"
    if status:
        return f"Task finished with status {status}"
    return "Task finished"


def _status_is_failed(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("success") is False:
            return True
        status = str(value.get("status") or "").strip().lower()
        if status in {"failed", "error", "blocked", "timeout", "timed_out", "cancelled"}:
            return True
        error = value.get("error")
        if isinstance(error, Mapping) and error:
            return True
        if error not in (None, "", [], {}):
            return True
    return False


def _value_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(DataFormatter.sanitize(value), ensure_ascii=False)
    except Exception:
        return str(value).strip()


def _is_retry_status_marker_source(path: str, value: Any) -> bool:
    return (
        (path == "$status" or path.endswith(".$status"))
        and isinstance(value, Mapping)
        and value.get("status") == "failed"
        and value.get("retry") is True
    )


def _format_retry_marker(value: Any) -> str:
    reason = value.get("reason") if isinstance(value, Mapping) else None
    text = str(reason).strip() if reason is not None else ""
    if not text:
        text = "Retrying model request."
    return f"<$retry>{html.escape(text, quote=False)}</$retry>"


class AgentExecutionTextDeltaProjector:
    """Stateful public-delta renderer for human-facing stream consumption."""

    def __init__(self):
        self._last_kind: str | None = None
        self._last_text_tail = ""
        self._taskboard_states: dict[str, dict[str, Mapping[str, Any]]] = {}
        self._flat_last_completed_action: dict[str, str] = {}
        self._flat_completed_actions: dict[str, list[str]] = {}

    def project(self, item: Any) -> str | None:
        text = project_agent_execution_text_delta(item)
        if text is None:
            return None
        kind = self._projection_kind(item)
        if kind == "taskboard":
            text = self._project_taskboard_text(item, fallback=text)
            if text is None:
                return None
        elif kind == "flat_snapshot":
            text = self._project_flat_snapshot_text(item, fallback=text)
            if text is None:
                return None
        elif kind == "flat_terminal":
            text = self._project_flat_terminal_text(item, fallback=text)
            if text is None:
                return None
        elif kind == "taskboard_terminal":
            text = self._project_taskboard_terminal_text(item, fallback=text)
            if text is None:
                return None
        text = self._with_stream_boundaries(text, kind)
        self._last_kind = kind
        self._last_text_tail = text[-8:]
        return text

    def _projection_kind(self, item: Any) -> str:
        path = str(getattr(item, "path", "") or "")
        value = getattr(item, "value", None)
        if _is_retry_status_marker_source(path, value):
            return "retry"
        if _taskboard_status_projection(path, value) is not None:
            return "taskboard"
        meta = getattr(item, "meta", None)
        item_meta = meta if isinstance(meta, Mapping) else {}
        if _flat_snapshot_projection(path, value, item_meta) is not None:
            return "flat_snapshot"
        source = str(getattr(item, "source", "") or "")
        if path == "result" and source == "agent_task" and self._taskboard_states:
            return "taskboard_terminal"
        if path == "result" and source == "agent_task" and self._flat_completed_actions:
            return "flat_terminal"
        if getattr(item, "event_type", None) == "delta":
            return "model_delta"
        return "process"

    def _project_taskboard_text(self, item: Any, *, fallback: str) -> str | None:
        path = str(getattr(item, "path", "") or "")
        value = getattr(item, "value", None)
        projection = _taskboard_status_projection(path, value)
        if projection is None:
            return fallback
        board_id = _clean_taskboard_text(projection.get("board_id")) or "taskboard"
        if projection.get("board_id_explicit") is not True and len(self._taskboard_states) == 1:
            board_id = next(iter(self._taskboard_states))
            projection = {**projection, "board_id": board_id}
        previous_state = self._taskboard_states.get(board_id)
        if previous_state is not None:
            projection = _taskboard_projection_with_previous_objectives(projection, previous_state)
        current_state = _taskboard_card_state_map(projection)
        self._taskboard_states[board_id] = current_state
        if previous_state is None or path == "agent_task.taskboard.plan":
            return _format_taskboard_status_projection(projection)
        return _format_taskboard_status_update(projection, previous_state)

    def _project_flat_snapshot_text(self, item: Any, *, fallback: str) -> str | None:
        path = str(getattr(item, "path", "") or "")
        value = getattr(item, "value", None)
        meta = getattr(item, "meta", None)
        item_meta = meta if isinstance(meta, Mapping) else {}
        projection = _flat_snapshot_projection(path, value, item_meta)
        if projection is None:
            return fallback
        task_key = self._flat_task_key(item, projection)
        iteration = projection.get("iteration")
        stage = str(projection.get("stage") or "")
        snapshot = projection.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        message = str(projection.get("message") or "")
        detail = _flat_snapshot_detail(stage, snapshot, message)
        if stage == "context":
            self._flat_last_completed_action[task_key] = detail
            return fallback
        if stage == "plan":
            return _paragraph(
                _format_flat_plan_text(
                    iteration=iteration,
                    snapshot=snapshot,
                    previous_action=self._flat_last_completed_action.get(task_key),
                )
            )
        if stage == "execution":
            failed = _status_is_failed(snapshot.get("execution_result"))
            self._flat_last_completed_action[task_key] = detail
            self._flat_completed_actions.setdefault(task_key, []).append(detail)
            return _paragraph(_format_flat_execution_text(iteration=iteration, detail=detail, failed=failed))
        if stage == "verification":
            if snapshot.get("is_complete") is True:
                self._flat_completed_actions.setdefault(task_key, []).append("verified the final result")
            return _paragraph(_format_flat_verification_text(iteration=iteration, snapshot=snapshot, detail=detail))
        return fallback

    def _project_flat_terminal_text(self, item: Any, *, fallback: str) -> str | None:
        value = getattr(item, "value", None)
        task_key = self._flat_task_key(item, {})
        actions = self._flat_completed_actions.get(task_key)
        if not actions and self._flat_completed_actions:
            actions = next(iter(self._flat_completed_actions.values()))
        if not actions:
            return fallback
        return _paragraph(_format_flat_terminal_summary(value, actions))

    def _project_taskboard_terminal_text(self, item: Any, *, fallback: str) -> str | None:
        if not self._taskboard_states:
            return fallback
        board_id = next(reversed(self._taskboard_states))
        cards = list(self._taskboard_states[board_id].values())
        progress = _taskboard_progress_summary(
            {
                "cards": cards,
                "counts": _taskboard_status_counts(cards),
            }
        )
        base = _terminal_result_text(getattr(item, "value", None))
        lines = base.splitlines()
        if not lines:
            return fallback
        lines.insert(1, f"📊 Overall progress: {progress}")
        return _paragraph("\n".join(lines))

    def _flat_task_key(self, item: Any, projection: Mapping[str, Any]) -> str:
        meta = getattr(item, "meta", None)
        item_meta = meta if isinstance(meta, Mapping) else {}
        return (
            _clean_taskboard_text(projection.get("task_id"))
            or _clean_taskboard_text(getattr(item, "task_id", ""))
            or _clean_taskboard_text(item_meta.get("task_id"))
            or "task"
        )

    def _with_stream_boundaries(self, text: str, kind: str) -> str:
        if not text:
            return text
        if kind == "model_delta":
            return text
        if kind == "retry":
            return self._ensure_leading_boundary(text).rstrip() + "\n\n"
        if self._last_kind == "model_delta":
            return self._ensure_leading_boundary(text)
        return text

    def _ensure_leading_boundary(self, text: str) -> str:
        if not self._last_text_tail:
            return text
        if self._last_text_tail.endswith("\n\n"):
            return text
        return "\n\n" + text


class AgentExecutionStream:
    """Execution-local raw stream buffer and TriggerFlow bridge."""

    def __init__(
        self,
        *,
        execution_id: str | None = None,
        lineage: Mapping[str, Any] | None = None,
    ):
        self.items: list[AgentExecutionStreamData] = []
        self.queues: list[asyncio.Queue[Any]] = []
        self.execution_id = execution_id
        self.lineage = dict(lineage or {})
        self._execution: Any = None

    def bind_execution(self, execution: Any):
        self._execution = execution
        return self

    def __call__(self, *args: Any, **kwargs: Any):
        if self._execution is None:
            raise TypeError("AgentExecutionStream is not bound to an AgentExecution.")
        return self._execution.get_async_generator(*args, **kwargs)

    async def emit(
        self,
        path: str,
        value: Any,
        *,
        delta: str | None = None,
        full_data: Any = None,
        route: str | None = None,
        source: str | None = "agent_execution",
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        is_complete: bool | None = None,
        event_type: Literal["delta", "done"] = "done",
        completion_source: Literal[
            "observed_boundary",
            "final_reconciliation",
            "synthetic_repair",
        ] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        item_meta = dict(meta or {})
        if self.execution_id is not None:
            item_meta.setdefault("execution_id", self.execution_id)
        if self.lineage:
            item_meta.setdefault("lineage", dict(self.lineage))
        completed = event_type == "done"
        if is_complete is not None:
            completed = is_complete
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            delta=delta,
            full_data=DataFormatter.sanitize(full_data),
            is_complete=completed,
            event_type=event_type,
            completion_source=completion_source,
            source=source,
            route=route,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            meta=DataFormatter.sanitize(item_meta) if item_meta else None,
        )
        return await self._publish(item)

    async def close(self):
        for queue in list(self.queues):
            await queue.put(None)

    async def flush_delta_buffer(self) -> AgentExecutionStreamData | None:
        return None

    async def _publish(self, item: AgentExecutionStreamData) -> AgentExecutionStreamData:
        self.items.append(item)
        for queue in list(self.queues):
            await queue.put(item)
        if self._execution is not None:
            emit_runtime_projection = getattr(self._execution, "_async_emit_stream_runtime_event", None)
            if callable(emit_runtime_projection):
                with suppress(Exception):
                    await cast(
                        Callable[[AgentExecutionStreamData], Awaitable[None]],
                        emit_runtime_projection,
                    )(item)
        return item

    def _is_compatible_delta(self, left: AgentExecutionStreamData, right: AgentExecutionStreamData) -> bool:
        return (
            left.path == right.path
            and left.source == right.source
            and left.route == right.route
            and left.stage_id == right.stage_id
            and left.task_id == right.task_id
            and left.action_id == right.action_id
            and left.graph_id == right.graph_id
            and (left.meta or {}).get("execution_id") == (right.meta or {}).get("execution_id")
            and (left.meta or {}).get("response_id") == (right.meta or {}).get("response_id")
            and (left.meta or {}).get("field_path") == (right.meta or {}).get("field_path")
        )

    async def bridge_model_stream_item(
        self,
        item: Any,
        *,
        route: str,
        source: str = "model_request",
        path_prefix: str | None = None,
        stage_id: str | None = None,
        task_id: str | None = None,
        action_id: str | None = None,
        graph_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ):
        raw_path = str(getattr(item, "path", "") or "model")
        path = f"{path_prefix}.{raw_path}" if path_prefix else raw_path
        raw_event_type = getattr(item, "event_type", "done")
        event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
        item_meta = {
            "field_path": raw_path,
            "wildcard_path": getattr(item, "wildcard_path", None),
            "indexes": getattr(item, "indexes", None),
        }
        if meta:
            item_meta.update(meta)
        await self.emit(
            path,
            getattr(item, "value", None),
            delta=getattr(item, "delta", None),
            full_data=getattr(item, "full_data", None),
            route=route,
            source=source,
            stage_id=stage_id,
            task_id=task_id,
            action_id=action_id,
            graph_id=graph_id,
            is_complete=bool(getattr(item, "is_complete", event_type == "done")),
            event_type=event_type,
            completion_source=getattr(item, "completion_source", None),
            meta=item_meta,
        )

    async def bridge_task_dag_item(self, item: Any, *, route: str):
        if not isinstance(item, dict):
            await self.emit("runtime.stream", item, route=route, source="triggerflow")
            return
        item_type = str(item.get("type") or "runtime.stream")
        action = str(item.get("action") or "event")
        payload = item.get("payload", {})
        task_id = str(item.get("task_id") or "") or None
        stage_id = str(item.get("stage_id") or "") or None
        graph_id = str(item.get("graph_id") or "") or None
        if item_type == "skills.stage_field" and stage_id:
            field_path = str(item.get("field_path") or "model")
            raw_event_type = str(item.get("event_type") or action)
            event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
            await self.emit(
                f"skills.stages.{stage_id}.fields.{field_path}",
                item.get("value"),
                delta=item.get("delta") if isinstance(item.get("delta"), str) else None,
                route=route,
                source="model_request",
                stage_id=stage_id,
                task_id=task_id,
                graph_id=graph_id,
                is_complete=bool(item.get("is_complete", event_type == "done")),
                event_type=event_type,
                meta=payload if isinstance(payload, dict) else None,
            )
            return
        if item_type == "skills.model_stream":
            field_path = str(item.get("path") or "model")
            raw_event_type = str(item.get("event_type") or action)
            event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
            await self.emit(
                f"skills.model.fields.{field_path}",
                item.get("value"),
                delta=item.get("delta") if isinstance(item.get("delta"), str) else None,
                route=route,
                source="model_request",
                stage_id=stage_id,
                graph_id=graph_id,
                is_complete=bool(item.get("is_complete", event_type == "done")),
                event_type=event_type,
                meta=payload if isinstance(payload, dict) else None,
            )
            return
        if item_type == "task_dag.model_field" and task_id:
            field_path = str(item.get("field_path") or "model")
            raw_event_type = str(item.get("event_type") or action)
            event_type: Literal["delta", "done"] = "delta" if raw_event_type == "delta" else "done"
            await self.emit(
                f"task_dag.tasks.{task_id}.fields.{field_path}",
                item.get("value"),
                delta=item.get("delta") if isinstance(item.get("delta"), str) else None,
                route=route,
                source="model_request",
                task_id=task_id,
                graph_id=graph_id,
                is_complete=bool(item.get("is_complete", event_type == "done")),
                event_type=event_type,
                meta=payload if isinstance(payload, dict) else None,
            )
            return
        if item_type == "task_dag.task" and task_id:
            path = f"task_dag.tasks.{ task_id }.{ action }"
        elif item_type == "task_dag.graph" and graph_id:
            path = f"task_dag.graphs.{ graph_id }.{ action }"
        else:
            path = item_type.replace("/", ".")
        await self.emit(
            path,
            item,
            route=route,
            source="triggerflow",
            stage_id=stage_id,
            task_id=task_id,
            graph_id=graph_id,
            meta=payload if isinstance(payload, dict) else None,
        )

    async def bridge_agent_task_item(self, item: Any, *, route: str = "agent_task"):
        if not isinstance(item, AgentExecutionStreamData):
            await self.emit("agent_task.stream", item, route=route, source="agent_task")
            return
        item_meta = dict(item.meta or {})
        await self.emit(
            item.path,
            item.value,
            delta=item.delta,
            route=route,
            source=item.source or "agent_task",
            stage_id=item.stage_id,
            task_id=item.task_id,
            action_id=item.action_id,
            graph_id=item.graph_id,
            is_complete=item.is_complete,
            event_type="delta" if item.event_type == "delta" else "done",
            meta=item_meta,
        )
