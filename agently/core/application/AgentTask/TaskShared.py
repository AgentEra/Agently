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
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, cast, Literal, TYPE_CHECKING, TypeVar

from agently.core.orchestration import (
    TaskBoard,
    TriggerFlow,
    build_task_board_acceptance_index,
    build_task_board_evidence_view,
    build_task_board_focus_payload,
    build_task_board_handoff_projection,
    build_task_board_incremental_verification_plan,
    build_task_board_scoped_evidence_view,
    coerce_task_board_planning_result,
    resolve_task_board_planning_policy,
    task_board_blocking_state_facts,
    task_board_explicit_state_facts,
    task_board_preflight_diagnostics,
    task_board_planning_output_schema,
)
from agently.core.orchestration.TaskBoard.TaskBoardValidation import task_board_card_required
from agently.core.application.AgentExecution.Stream import (
    AgentExecutionTextDeltaProjector,
    project_agent_execution_text_delta,
)
from agently.core.model.StructuredOutputParser import parse_output_contract_dict
from agently.types.data import AgentExecutionStreamData, ReplanSignal, TaskBoardCardResult, TaskBoardRevision
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import DataFormatter
from agently.utils.LanguagePolicy import (
    apply_language_policy_to_prompt,
    language_policy_from_prompt_snapshot,
    resolve_language_policy,
)

from .BlockCarrier import (
    CarrierOutputPolicy,
    SCOPED_RETRIEVAL_RESULT_CAPACITY,
    WorkUnitIntent,
    WorkUnitResult,
    scoped_retrieval_policy,
    select_carrier_output_policy,
)
from .EvidenceLedger import (
    acceptance_locator_view_from_ledger,
    collect_evidence_use,
    evidence_envelope_from_value,
    evidence_ledger_view,
    reference_has_file_locator,
    resolve_evidence_reference,
    source_refs_from_ledger,
    validate_evidence_use,
    value_with_normalized_evidence_use,
    task_workspace_artifacts_from_ledger,
)
from .TaskReferences import (
    TaskReferenceCatalog,
    parse_reference_tokens,
    validate_reference_tokens,
)
from .TerminalConvergence import (
    TerminalConvergenceState,
    TerminalIssue,
    relevant_state_digest,
)

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.types.data import RecordRef
else:
    BaseAgent = Any
    RecordRef = dict[str, Any]

TaskContextView = dict[str, Any]


AgentTaskStatus = Literal[
    "created",
    "running",
    "completed",
    "blocked",
    "max_iterations",
    "timed_out",
    "capability_unavailable",
    "cancelled",
    "error",
]
AgentTaskExecutionStrategy = Literal["auto", "flat", "taskboard"]
AgentTaskEffectiveExecutionStrategy = Literal["flat", "taskboard"]

_AGENT_TASK_EXECUTION_STRATEGY_ALIASES = {
    "": "auto",
    "default": "auto",
    "automatic": "auto",
    "linear": "flat",
    "react": "flat",
    "flat_react": "flat",
    "task_board": "taskboard",
    "board": "taskboard",
    "taskboard_evidenceview": "taskboard",
}


_STEP_EXECUTION_SHAPES = {
    "direct",
    "actions",
    "dynamic_task",
    "execution_dag",
}

_DEGRADED_DAG_STEP_EXECUTION_SHAPES = {"dynamic_task", "execution_dag"}
_TASKBOARD_CONTROL_CARD_SHAPES = {
    "control",
    "model_control",
    "synthesis",
    "synthesize",
    "finalize",
    "final",
    "verification",
    "verify",
}
_TASKBOARD_READBACK_CARD_SHAPES = {
    "readback",
    "artifact_readback",
    "cold_readback",
    "evidence_readback",
}
_TASKBOARD_READBACK_PREVIEW_CHARS = 12000
_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS = 12000
_TASKBOARD_DEPENDENCY_READBACK_MAX_REFS = 4
_TASKBOARD_SOURCE_REFS_MAX = 16
_TASKBOARD_PROMPT_RESULT_CHARS = 1600
_TASKBOARD_STREAM_SUMMARY_CHARS = 3000
_TASKBOARD_SETBACK_CARD_STATUS = "setback"
_TASKBOARD_RECOVERABLE_CARD_STATUSES = {
    "failed",
    "error",
    "timed_out",
    "blocked",
    _TASKBOARD_SETBACK_CARD_STATUS,
}
_WORKSPACE_ARTIFACT_PREVIEW_BYTES = 4000
_WORKSPACE_ARTIFACT_CONTENT_KEYS = ("content", "markdown", "body", "text")
_WORKSPACE_ARTIFACT_RESULT_BODY_KEYS = (
    "artifact_markdown",
    "artifact_html",
    "candidate_final_result",
    "final_result",
    "answer",
)

# Upper bound on the in-memory stream replay buffer for late subscribers.
_STREAM_REPLAY_LIMIT = 5000
_VERIFIER_PROMPT_VALUE_CHARS = 12000
_VERIFIER_PROMPT_ITEM_CHARS = 2400
# The verifier receives one bounded, body-bearing evidence projection. Other
# indexes and summaries stay body-light so the same readback is not multiplied
# across the prompt as task history grows.
_VERIFIER_PROMPT_TARGET_CHARS = 160_000
_VERIFIER_LEDGER_MAX_ITEMS = 72
_VERIFIER_LEDGER_BODY_CHARS = 900
_VERIFIER_LEDGER_MAX_OVERFLOW_REFS = 80
_AGENT_TASK_ERROR_MESSAGE_CHARS = 2000
_AGENT_TASK_ERROR_PAYLOAD_MARKERS = (
    "\nrequest data:",
    "\nrequest body:",
    "\nrequest payload:",
    "\nraw request:",
    "\nprompt data:",
)
_AGENT_TASK_HOT_PATH_REQUEST_PAYLOAD_KEYS = {
    "requestdata",
    "requestbody",
    "requestpayload",
    "rawrequest",
    "promptdata",
    "providerrequest",
    "providerrequestdata",
    "modelrequestdata",
}
_AGENT_TASK_DEFAULT_MAX_ITERATIONS: int | None = None
_PROCESS_SUMMARY_TEXT_CHARS = 360
_PROCESS_SUMMARY_LIST_ITEMS = 8
_PROCESS_SUMMARY_FIELDS = (
    "task_intent",
    "turn_intent",
    "card_intent",
    "decision_basis",
    "self_check",
    "short_summary",
    "verification_summary",
    "criterion_checks",
    "repair_summary",
    "progress_message",
)
_PROCESS_SUMMARY_NEXT_STEP_FIELDS = (
    "self_check",
    "short_summary",
    "verification_summary",
    "criterion_checks",
    "repair_summary",
)
_PROCESS_SUMMARY_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def _format_agent_task_utc_offset(value: str) -> str:
    offset = str(value or "").strip()
    if len(offset) == 5 and offset[0] in {"+", "-"} and offset[1:].isdigit():
        return f"{offset[:3]}:{offset[3:]}"
    return offset


def _normalize_agent_task_max_iterations(value: Any) -> int | None:
    if value is None:
        return _AGENT_TASK_DEFAULT_MAX_ITERATIONS
    try:
        iterations = int(value)
    except (TypeError, ValueError):
        return _AGENT_TASK_DEFAULT_MAX_ITERATIONS
    return max(1, iterations)


class _AgentTaskMixinMeta(type):
    def __getattr__(cls, name: str) -> Any:
        raise AttributeError(name)


class AgentTaskMixinBase(metaclass=_AgentTaskMixinMeta):
    """Typing aid for AgentTask mixins composed by the root AgentTask class."""

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def _task_context_retrieval_policy(self) -> dict[str, Any]:
        task_context = getattr(self, "task_context", None)
        catalog = (
            task_context.source_catalog()
            if task_context is not None
            and callable(getattr(task_context, "source_catalog", None))
            else {}
        )
        return scoped_retrieval_policy(catalog)

    def _task_references(self) -> TaskReferenceCatalog:
        catalog = cast(TaskReferenceCatalog | None, getattr(self, "_task_reference_catalog", None))
        if catalog is None:
            catalog = TaskReferenceCatalog(str(getattr(self, "id", "")))
            setattr(self, "_task_reference_catalog", catalog)
        return catalog

    def _stable_evidence_ledger_view(self, value: Any, **kwargs: Any) -> dict[str, Any]:
        return evidence_ledger_view(value, task_references=self._task_references(), **kwargs)

    @staticmethod
    def _taskboard_normalized_evidence_path(value: Any) -> str:
        path = str(value or "").strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        return path.strip("/")

    @classmethod
    def _taskboard_grounding_evidence_identity(
        cls,
        resolved: Mapping[str, Any],
        *,
        excluded_paths: set[str],
    ) -> dict[str, Any] | None:
        target = resolved.get("target")
        if not isinstance(target, Mapping):
            return None
        status = str(resolved.get("status") or target.get("status") or "").strip().lower()
        body_state = str(
            resolved.get("body_state") or target.get("body_state") or ""
        ).strip().lower()
        if status != "ok" or body_state not in {"full", "bounded", "truncated"}:
            return None

        body: Any = None
        for field in ("body", "content", "text", "snippet", "preview"):
            candidate = target.get(field)
            if candidate not in (None, "", [], {}):
                body = candidate
                break
        if body in (None, "", [], {}):
            return None
        body_value = DataFormatter.sanitize(body)
        body_text = (
            body_value
            if isinstance(body_value, str)
            else json.dumps(
                body_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        if not str(body_text).strip():
            return None

        provenance = target.get("provenance")
        provenance_map = provenance if isinstance(provenance, Mapping) else {}
        input_preview = target.get("input_preview")
        input_map = input_preview if isinstance(input_preview, Mapping) else {}

        path = ""
        for source in (target, input_map, provenance_map):
            for field in ("path", "file_path", "output_path"):
                path = cls._taskboard_normalized_evidence_path(source.get(field))
                if path:
                    break
            if path:
                break
        if path and path in excluded_paths:
            return None

        owner = str(
            target.get("owner")
            or provenance_map.get("owner")
            or resolved.get("source_role")
            or "source"
        ).strip()
        locator = str(target.get("locator") or provenance_map.get("locator") or path).strip()
        if not locator:
            for source in (target, input_map, provenance_map):
                for field in (
                    "source_url",
                    "canonical_url",
                    "url",
                    "record_id",
                    "selection_key",
                    "query",
                ):
                    locator = str(source.get(field) or "").strip()
                    if locator:
                        break
                if locator:
                    break
        if not locator:
            locator = str(target.get("action_id") or target.get("kind") or "source").strip()

        content_version = str(
            target.get("content_version")
            or target.get("content_version_id")
            or target.get("sha256")
            or provenance_map.get("content_version")
            or provenance_map.get("content_version_id")
            or provenance_map.get("sha256")
            or hashlib.sha256(str(body_text).encode("utf-8")).hexdigest()
        ).strip()
        read_range: dict[str, Any] = {}
        raw_range = target.get("range")
        if isinstance(raw_range, Mapping):
            read_range.update(
                {
                    str(key): DataFormatter.sanitize(value)
                    for key, value in raw_range.items()
                    if value not in (None, "", [], {})
                }
            )
        for field in ("offset", "max_bytes", "start_line", "end_line"):
            if field not in read_range and input_map.get(field) not in (None, "", [], {}):
                read_range[field] = DataFormatter.sanitize(input_map.get(field))

        identity_payload = {
            "owner": owner,
            "locator": locator,
            "content_version": content_version,
            "range": read_range,
        }
        identity = hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "identity": f"evidence:{identity}",
            "reference_id": str(resolved.get("reference_id") or ""),
            "source_role": str(resolved.get("source_role") or ""),
            "owner": owner,
            "locator": locator,
            "content_version": content_version,
            "range": read_range,
            "path": path,
        }

    def _taskboard_grounding_evidence_snapshot(
        self,
        *,
        eligible_source_roles: Sequence[str] = (
            "action",
            "source",
            "task_workspace_readback",
        ),
        excluded_artifact_paths: Sequence[str] = (),
    ) -> dict[str, Any]:
        roles = tuple(
            str(role).strip()
            for role in eligible_source_roles
            if str(role or "").strip()
        )
        excluded_paths = {
            path
            for path in (
                self._taskboard_normalized_evidence_path(value)
                for value in excluded_artifact_paths
            )
            if path
        }
        offered = self._task_references().offered_references(
            eligible_roles=roles,
        )
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for reference_id in offered:
            try:
                resolved = self._task_references().resolve(reference_id)
            except ValueError:
                continue
            item = self._taskboard_grounding_evidence_identity(
                resolved,
                excluded_paths=excluded_paths,
            )
            if item is None:
                continue
            identity = str(item.get("identity") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            items.append(item)
        items.sort(key=lambda item: str(item.get("identity") or ""))
        return DataFormatter.sanitize(
            {
                "content_identities": [
                    str(item.get("identity") or "") for item in items
                ],
                "reference_ids": [
                    str(item.get("reference_id") or "") for item in items
                ],
                "items": items,
                "eligible_source_roles": list(roles),
                "excluded_artifact_paths": sorted(excluded_paths),
            }
        )

    @classmethod
    def _process_summary_from_value(
        cls,
        value: Any,
        *,
        stage: str = "",
        next_step_only: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        fields = _PROCESS_SUMMARY_NEXT_STEP_FIELDS if next_step_only else _PROCESS_SUMMARY_FIELDS
        summary: dict[str, Any] = {}
        if stage:
            summary["stage"] = stage
        for field in fields:
            if field not in value:
                continue
            compacted = cls._compact_process_summary_value(value.get(field))
            if compacted not in (None, "", [], {}):
                summary[field] = compacted
        if len(summary) == 1 and "stage" in summary:
            return {}
        return DataFormatter.sanitize(summary)

    @classmethod
    def _combined_process_summary(
        cls,
        *,
        plan: Any = None,
        execution_result: Any = None,
        verification: Any = None,
    ) -> dict[str, Any]:
        combined: dict[str, Any] = {}
        plan_summary = cls._process_summary_from_value(plan, stage="plan", next_step_only=True)
        execution_summary = cls._process_summary_from_value(
            execution_result,
            stage="execution",
            next_step_only=True,
        )
        verification_summary = cls._process_summary_from_value(
            verification,
            stage="verification",
            next_step_only=True,
        )
        if plan_summary:
            combined["plan"] = plan_summary
        if execution_summary:
            combined["execution"] = execution_summary
        if verification_summary:
            combined["verification"] = verification_summary
        return DataFormatter.sanitize(combined)

    @classmethod
    def _compact_process_summary_value(cls, value: Any, *, depth: int = 0) -> Any:
        if value in (None, "", [], {}):
            return None
        if isinstance(value, str):
            return cls._truncate_process_summary_text(value)
        if isinstance(value, bool | int | float):
            return value
        if isinstance(value, Mapping):
            if depth >= 2:
                return cls._truncate_process_summary_text(value)
            compacted: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                if any(part in lowered for part in _PROCESS_SUMMARY_SENSITIVE_KEY_PARTS):
                    compacted[key_text] = "[redacted]"
                    continue
                if lowered in {
                    "body",
                    "content",
                    "data",
                    "evidence_body",
                    "full_text",
                    "raw",
                    "raw_output",
                    "text",
                }:
                    continue
                child = cls._compact_process_summary_value(item, depth=depth + 1)
                if child not in (None, "", [], {}):
                    compacted[key_text] = child
                if len(compacted) >= _PROCESS_SUMMARY_LIST_ITEMS:
                    compacted["omitted"] = {"reason": "process_summary_budget"}
                    break
            return compacted or None
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            compacted_items: list[Any] = []
            for item in value[:_PROCESS_SUMMARY_LIST_ITEMS]:
                child = cls._compact_process_summary_value(item, depth=depth + 1)
                if child not in (None, "", [], {}):
                    compacted_items.append(child)
            return compacted_items or None
        return cls._truncate_process_summary_text(value)

    @staticmethod
    def _truncate_process_summary_text(value: Any) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= _PROCESS_SUMMARY_TEXT_CHARS:
            return text
        return text[: max(0, _PROCESS_SUMMARY_TEXT_CHARS - 24)].rstrip() + " [truncated]"

    @classmethod
    def _agent_task_user_final_response(
        cls,
        *,
        final: Mapping[str, Any] | None = None,
        accepted: bool,
        artifact_status: str,
        status: str = "",
        reason: str = "",
        missing_criteria: Any = None,
        final_refs: Sequence[Mapping[str, Any]] | None = None,
        final_result: Any = None,
        degraded: bool = False,
        degradation_reason: str = "",
        degraded_finalization_attempted: bool = False,
        board_status: str = "",
        disclosure: str = "",
    ) -> str:
        def bounded(text: str) -> str:
            raw = text.encode("utf-8")
            if len(raw) <= 4096:
                return text
            suffix = " [truncated]"
            budget = 4096 - len(suffix.encode("utf-8"))
            return raw[:budget].decode("utf-8", errors="ignore").rstrip() + suffix

        final_map = final if isinstance(final, Mapping) else {}
        provided = str(final_map.get("final_response") or "").strip()
        disclosure = str(disclosure or "").strip()
        result_value = final_result if final_result is not None else final_map.get("final_result")
        ref_paths = [
            str(ref.get("path") or "").strip()
            for ref in list(final_refs or [])
            if isinstance(ref, Mapping) and str(ref.get("path") or "").strip()
        ]
        ref_paths = list(dict.fromkeys(ref_paths))
        if ref_paths:
            deliverable = "Deliverable artifact: " + ", ".join(ref_paths[:4]) + "."
        else:
            result_text = cls._agent_task_final_result_text(result_value)
            if result_text:
                if len(result_text) <= 700:
                    deliverable = f"Deliverable result: {result_text}"
                else:
                    deliverable = (
                        "Deliverable result is available in final_result; "
                        "it is not duplicated in this status note."
                    )
            elif accepted:
                deliverable = "No separate final artifact was produced."
            else:
                deliverable = "No complete final deliverable was accepted."

        normalized_artifact_status = str(artifact_status or "").strip().lower()
        normalized_missing = cls._agent_task_string_list(missing_criteria)
        reason_text = str(reason or final_map.get("reason") or "").strip()
        degradation_reason = str(degradation_reason or final_map.get("degradation_reason") or "").strip()
        if not degradation_reason and degraded_finalization_attempted:
            degradation_reason = f"TaskBoard terminal board status was {str(board_status or 'unknown')}."
        if not degradation_reason and (degraded or normalized_artifact_status == "degraded"):
            degradation_reason = reason_text

        # A canonical file ref is the terminal deliverable. Model-provided text
        # may be the file body and must never become a second terminal carrier.
        if provided and accepted and not ref_paths:
            additions: list[str] = []
            provided_lower = provided.casefold()
            if (normalized_artifact_status == "degraded" or degraded) and degradation_reason:
                degradation_note = f"Limitation: {degradation_reason}"
                if degradation_reason.casefold() not in provided_lower:
                    additions.append(degradation_note)
            if disclosure:
                disclosure_notes = disclosure.split(":", 1)[-1].strip(" .")
                disclosure_seen = any(
                    note and note.casefold() in provided_lower
                    for note in [part.strip() for part in disclosure_notes.split(";")]
                )
                if not disclosure_seen:
                    additions.append(disclosure)
            if additions:
                return bounded(f"{provided.rstrip()} {' '.join(additions)}".strip())
            return bounded(provided)

        if accepted:
            if normalized_artifact_status == "degraded" or degraded:
                response = "Completed with disclosed limitations. " + deliverable
                if degradation_reason:
                    response += f" Limitation: {degradation_reason}"
            else:
                response = "Completed. " + deliverable
                if reason_text:
                    response += f" Summary: {reason_text}"
        elif normalized_artifact_status == "blocked":
            response = "Task encountered a blocking condition. " + deliverable
            if reason_text:
                response += f" Reason: {reason_text}"
        else:
            response = "Partial result available, but the task was not fully accepted. " + deliverable
            if reason_text:
                response += f" Reason: {reason_text}"
            if degradation_reason:
                response += f" Limitation: {degradation_reason}"
        if normalized_missing:
            response += " Unmet requirements: " + "; ".join(normalized_missing[:5]) + "."
        if disclosure:
            response += " " + disclosure
        return bounded(response.strip())

    @staticmethod
    def _agent_task_final_result_text(value: Any) -> str:
        if value in (None, "", [], {}):
            return ""
        if isinstance(value, str):
            return " ".join(value.split())
        try:
            return json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, sort_keys=True)
        except Exception:
            return " ".join(str(value).split())

    @staticmethod
    def _agent_task_string_list(value: Any) -> list[str]:
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            result: list[str] = []
            for item in value:
                text = str(item or "").strip()
                if text:
                    result.append(text)
            return result
        return []

    @staticmethod
    def _is_process_summary_stream_path(path: Any) -> bool:
        text = str(path or "").strip()
        if not text:
            return False
        token = text.rsplit(".", 1)[-1].split("[", 1)[0]
        return token in set(_PROCESS_SUMMARY_FIELDS)

    async def _emit_process_progress_from_output(
        self,
        value: Any,
        *,
        stage: str,
        iteration: int | None = None,
        card_id: str | None = None,
    ) -> AgentExecutionStreamData | None:
        summary = self._process_summary_from_value(value, stage=stage)
        if not summary:
            return None
        message = str(summary.get("progress_message") or "").strip()
        if not message:
            return None
        payload = {
            "message": self._truncate_process_summary_text(message),
            "stage": stage,
            "iteration": iteration,
            "card_id": card_id,
            "status": getattr(self, "status", None),
            "process_summary": summary,
        }
        return await self._emit(
            "agent_task.process.progress",
            payload,
            meta={
                "task_id": getattr(self, "id", ""),
                "status": getattr(self, "status", None),
                "iteration": iteration,
                "stage": stage,
                "card_id": card_id,
                "stream_kind": "progress",
                "progress_source": "process_summary",
            },
        )

    def _task_context_contract(self) -> dict[str, Any]:
        run_epoch = getattr(self, "started_at", None)
        if run_epoch is None:
            run_epoch = getattr(self, "created_at", None)
        if run_epoch is None:
            run_epoch = time.time()
        try:
            run_epoch_float = float(run_epoch)
        except (TypeError, ValueError):
            run_epoch_float = time.time()
        current_time: dict[str, Any] = {
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(run_epoch_float)),
        }
        try:
            local_struct = time.localtime(run_epoch_float)
            local_timezone = str(os.environ.get("TZ") or "").strip() or time.strftime("%Z", local_struct).strip()
            local_utc_offset = _format_agent_task_utc_offset(time.strftime("%z", local_struct))
            if local_timezone or local_utc_offset:
                run_time_local = time.strftime("%Y-%m-%dT%H:%M:%S", local_struct)
                if local_utc_offset:
                    run_time_local = f"{run_time_local}{local_utc_offset}"
                current_time["local"] = run_time_local
                if local_timezone:
                    current_time["timezone"] = local_timezone
        except (OSError, OverflowError, ValueError):
            pass
        return {
            "schema_version": "agent_task_context_contract/v1",
            "current_time": current_time,
            "temporal_policy": {
                "currentness_reference": (
                    "When the task asks for current, latest, recent, or as-of information without an explicit "
                    "date, treat current_time.utc and any local current_time facts as the reference time context "
                    "and expose any source-date limitation. The date is derivable from the ISO timestamp."
                ),
                "dated_evidence": (
                    "Dated evidence may be useful, but older or historical material must be labeled with its "
                    "time boundary instead of being presented as current by implication."
                ),
                "general_decision_context": (
                    "UTC and recognizable local time facts are available to model decisions broadly, not only to "
                    "search or retrieval planning."
                ),
                "query_planning": (
                    "For external search or browse planning, include the caller's explicit as-of date when present; "
                    "otherwise use the task time facts/current year as grounding facts, not execution caps."
                ),
            },
            "intermediate_resource_policy": {
                "cold_resource_kinds": [
                    "download",
                    "webpage_snapshot",
                    "search_note",
                    "generated_code",
                    "large_extraction",
                    "task_workspace_note",
                ],
                "default_state": "ref_only",
                "hot_path": (
                    "Pass compact refs and bounded previews through prompts. Keep large intermediate resources in "
                    "TaskWorkspace or Action artifacts until a later block explicitly needs scoped content."
                ),
                "readback": (
                    "Use bounded readback with concrete refs, max_bytes, offsets, or scoped snippets when content is "
                    "needed for synthesis, verification, or repair."
                ),
                "evidence_boundary": (
                    "A ref proves discovery or materialization only. It becomes source-content evidence only after "
                    "bounded readback or content preview is visible to the executing block."
                ),
            },
            "resource_policy": {
                "hard_execution_caps": "Do not infer model-call, tool-call, node-count, or iteration caps from this contract.",
                "liveness_timeouts": "Operational idle/no-progress timeouts may still protect against stuck execution.",
            },
        }

    def _task_context_contract_for_model_prompt(self) -> dict[str, Any]:
        contract = self._task_context_contract()
        contract["current_time"] = {
            "available": True,
            "visibility": "omitted_from_default_model_hot_path",
            "reason": (
                "Concrete runtime date/time values are execution context only and are not supplied to "
                "default model generation, verification, or finalization prompts."
            ),
        }
        temporal_policy = dict(contract.get("temporal_policy") or {})
        temporal_policy["currentness_reference"] = (
            "Concrete runtime current_time values are omitted from this model prompt. Do not infer, "
            "write, or verify a current date/time as a business fact unless it appears in the user task, "
            "source evidence, readback, or an explicit caller-supplied as-of fact."
        )
        temporal_policy["general_decision_context"] = (
            "The runtime/current date is not general business evidence. If the task genuinely requires "
            "current/latest/as-of grounding, use explicit task facts or source/tool evidence and label "
            "their time boundary."
        )
        contract["temporal_policy"] = temporal_policy
        return contract


_AgentTaskT = TypeVar("_AgentTaskT", bound=AgentTaskMixinBase)


class _AgentTaskDeadlineExceeded(TimeoutError):
    def __init__(
        self,
        stage: str,
        *,
        reason: str | None = None,
        limit_name: str = "max_seconds",
        timeout_seconds: float | None = None,
    ):
        super().__init__(reason or f"AgentTask exceeded {limit_name} while running stage '{stage}'.")
        self.stage = stage
        self.reason = reason
        self.limit_name = limit_name
        self.timeout_seconds = timeout_seconds


def _compact_agent_task_error_message(
    message: Any,
    *,
    fallback: str = "",
    max_chars: int = _AGENT_TASK_ERROR_MESSAGE_CHARS,
) -> str:
    raw = str(message or fallback or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw:
        return fallback
    lower = raw.lower()
    payload_omitted = False
    end_index: int | None = None
    for marker in _AGENT_TASK_ERROR_PAYLOAD_MARKERS:
        marker_index = lower.find(marker)
        if marker_index >= 0 and (end_index is None or marker_index < end_index):
            end_index = marker_index
    if lower.startswith("request data:") or lower.startswith("request body:") or lower.startswith("request payload:"):
        end_index = 0
    if end_index is not None:
        raw_prefix = raw[:end_index].strip()
        raw = raw_prefix or fallback or "Provider request failed."
        payload_omitted = True

    compact_lines: list[str] = []
    prior_blank = False
    for line in raw.split("\n"):
        stripped = line.rstrip()
        if not stripped:
            if compact_lines and not prior_blank:
                compact_lines.append("")
            prior_blank = True
            continue
        compact_lines.append(stripped)
        prior_blank = False
    compact = "\n".join(compact_lines).strip() or fallback
    original_length = len(str(message or ""))
    truncated = len(compact) > max_chars
    if truncated:
        compact = compact[:max_chars].rstrip()
    suffix_parts: list[str] = []
    if payload_omitted:
        suffix_parts.append("provider payload omitted")
    if truncated:
        suffix_parts.append(f"truncated from {original_length} chars")
    if suffix_parts:
        suffix = f" [{'; '.join(suffix_parts)}]"
        if len(compact) + len(suffix) > max_chars:
            compact = compact[: max(0, max_chars - len(suffix))].rstrip()
        compact = f"{compact}{suffix}".strip()
    return compact or fallback


def _agent_task_hot_path_request_payload_omission(value: Any) -> dict[str, Any]:
    try:
        text = json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, default=str)
        chars = len(text)
    except Exception:
        chars = len(str(value or ""))
    return {
        "omitted": True,
        "reason": "provider_request_payload_hot_path",
        "chars": chars,
    }


def _agent_task_hot_path_key_is_request_payload(key: Any) -> bool:
    normalized = "".join(char for char in str(key or "").lower() if char.isalnum())
    return normalized in _AGENT_TASK_HOT_PATH_REQUEST_PAYLOAD_KEYS


def _agent_task_hot_path_key_is_integrity_metadata(key: Any) -> bool:
    normalized = str(key or "").strip().lower()
    return normalized in {"sha256", "digest", "bytes", "read_bytes", "size", "media_type", "content_kind", "handler_id"}


def _drop_agent_task_integrity_metadata_lines(text: str) -> str:
    omitted_prefixes = ("sha256:", "digest:", "bytes:", "read_bytes:", "size:", "media_type:", "handler_id:")
    lines = []
    changed = False
    for line in text.splitlines():
        if line.strip().lower().startswith(omitted_prefixes):
            changed = True
            continue
        lines.append(line)
    return "\n".join(lines) if changed else text


def _omit_agent_task_request_payloads_from_hot_path(value: Any, *, depth: int = 0) -> Any:
    """Remove full provider request payloads before planner/verifier hot paths."""

    sanitized = DataFormatter.sanitize(value)
    if sanitized is None or isinstance(sanitized, (bool, int, float)):
        return sanitized
    if isinstance(sanitized, bytes):
        return sanitized
    if isinstance(sanitized, str):
        lower = sanitized.lower()
        for marker in _AGENT_TASK_ERROR_PAYLOAD_MARKERS:
            marker_text = marker.strip()
            if lower.startswith(marker_text):
                return _agent_task_hot_path_request_payload_omission(sanitized)
            marker_index = lower.find(marker)
            if marker_index >= 0:
                prefix = sanitized[:marker_index].strip()
                return {
                    "preview": prefix[: _AGENT_TASK_ERROR_MESSAGE_CHARS] if prefix else "Provider request payload omitted.",
                    "omitted": True,
                    "reason": "provider_request_payload_hot_path",
                    "chars": len(sanitized),
                }
        return _drop_agent_task_integrity_metadata_lines(sanitized)
    if depth >= 8:
        return sanitized
    if isinstance(sanitized, Mapping):
        compacted: dict[str, Any] = {}
        for key, item in sanitized.items():
            key_text = str(key)
            if _agent_task_hot_path_key_is_request_payload(key_text):
                compacted[key_text] = _agent_task_hot_path_request_payload_omission(item)
            elif _agent_task_hot_path_key_is_integrity_metadata(key_text):
                continue
            else:
                compacted[key_text] = _omit_agent_task_request_payloads_from_hot_path(
                    item,
                    depth=depth + 1,
                )
        return compacted
    if isinstance(sanitized, Sequence) and not isinstance(sanitized, str | bytes | bytearray):
        return [
            _omit_agent_task_request_payloads_from_hot_path(item, depth=depth + 1)
            for item in sanitized
        ]
    return sanitized


def _compact_agent_task_error_info(error_info: Any) -> Any:
    if isinstance(error_info, Mapping):
        compacted = dict(error_info)
        if "message" in compacted:
            original_message = str(compacted.get("message") or "")
            fallback = str(compacted.get("type") or "ExecutionError")
            compacted_message = _compact_agent_task_error_message(original_message, fallback=fallback)
            compacted["message"] = compacted_message
            if compacted_message != original_message:
                compacted["message_compacted"] = True
                compacted["message_original_length"] = len(original_message)
        return compacted
    if isinstance(error_info, str):
        return _compact_agent_task_error_message(error_info)
    return error_info


__all__ = [
    "Any",
    "AsyncGenerator",
    "Awaitable",
    "Callable",
    "Generator",
    "Literal",
    "Mapping",
    "Path",
    "Sequence",
    "TypeVar",
    "asyncio",
    "cast",
    "json",
    "os",
    "suppress",
    "time",
    "uuid",
    "AgentExecutionStreamData",
    "AgentExecutionTextDeltaProjector",
    "AgentTaskEffectiveExecutionStrategy",
    "AgentTaskExecutionStrategy",
    "AgentTaskMixinBase",
    "AgentTaskStatus",
    "BaseAgent",
    "CarrierOutputPolicy",
    "SCOPED_RETRIEVAL_RESULT_CAPACITY",
    "DataFormatter",
    "ReplanSignal",
    "TaskBoard",
    "TaskBoardCardResult",
    "TaskBoardRevision",
    "TriggerFlow",
    "TriggerFlowRuntimeData",
    "TaskContextView",
    "RecordRef",
    "WorkUnitIntent",
    "WorkUnitResult",
    "_AGENT_TASK_EXECUTION_STRATEGY_ALIASES",
    "_AGENT_TASK_DEFAULT_MAX_ITERATIONS",
    "_AgentTaskT",
    "_AgentTaskDeadlineExceeded",
    "_AGENT_TASK_ERROR_MESSAGE_CHARS",
    "_DEGRADED_DAG_STEP_EXECUTION_SHAPES",
    "_STEP_EXECUTION_SHAPES",
    "_STREAM_REPLAY_LIMIT",
    "_TASKBOARD_CONTROL_CARD_SHAPES",
    "_TASKBOARD_DEPENDENCY_READBACK_MAX_REFS",
    "_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS",
    "_TASKBOARD_PROMPT_RESULT_CHARS",
    "_TASKBOARD_READBACK_CARD_SHAPES",
    "_TASKBOARD_READBACK_PREVIEW_CHARS",
    "_TASKBOARD_RECOVERABLE_CARD_STATUSES",
    "_TASKBOARD_SOURCE_REFS_MAX",
    "_TASKBOARD_STREAM_SUMMARY_CHARS",
    "_VERIFIER_LEDGER_BODY_CHARS",
    "_VERIFIER_LEDGER_MAX_ITEMS",
    "_VERIFIER_LEDGER_MAX_OVERFLOW_REFS",
    "_VERIFIER_PROMPT_ITEM_CHARS",
    "_VERIFIER_PROMPT_TARGET_CHARS",
    "_VERIFIER_PROMPT_VALUE_CHARS",
    "_WORKSPACE_ARTIFACT_CONTENT_KEYS",
    "_WORKSPACE_ARTIFACT_PREVIEW_BYTES",
    "_WORKSPACE_ARTIFACT_RESULT_BODY_KEYS",
    "_compact_agent_task_error_info",
    "_compact_agent_task_error_message",
    "_normalize_agent_task_max_iterations",
    "_omit_agent_task_request_payloads_from_hot_path",
    "apply_language_policy_to_prompt",
    "acceptance_locator_view_from_ledger",
    "build_task_board_acceptance_index",
    "build_task_board_evidence_view",
    "build_task_board_focus_payload",
    "build_task_board_handoff_projection",
    "build_task_board_incremental_verification_plan",
    "build_task_board_scoped_evidence_view",
    "coerce_task_board_planning_result",
    "collect_evidence_use",
    "evidence_envelope_from_value",
    "evidence_ledger_view",
    "language_policy_from_prompt_snapshot",
    "parse_output_contract_dict",
    "project_agent_execution_text_delta",
    "reference_has_file_locator",
    "resolve_evidence_reference",
    "resolve_language_policy",
    "resolve_task_board_planning_policy",
    "scoped_retrieval_policy",
    "select_carrier_output_policy",
    "source_refs_from_ledger",
    "task_board_blocking_state_facts",
    "task_board_card_required",
    "task_board_explicit_state_facts",
    "task_board_planning_output_schema",
    "task_board_preflight_diagnostics",
    "validate_evidence_use",
    "value_with_normalized_evidence_use",
    "task_workspace_artifacts_from_ledger",
    "TaskReferenceCatalog",
    "parse_reference_tokens",
    "validate_reference_tokens",
    "TerminalConvergenceState",
    "TerminalIssue",
    "relevant_state_digest",
]
