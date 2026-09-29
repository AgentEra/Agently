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

import hashlib
import json

from typing import ContextManager

from .TaskShared import (
    AgentTaskMixinBase,
    Any,
    Awaitable,
    build_task_board_evidence_view,
    cast,
    DataFormatter,
    evidence_ledger_view,
    Mapping,
    Sequence,
    TaskBoardCardResult,
    TaskContextView,
    WorkUnitIntent,
    _compact_agent_task_error_message,
    _TASKBOARD_DEPENDENCY_READBACK_MAX_REFS,
    _TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
    _TASKBOARD_PROMPT_RESULT_CHARS,
    _TASKBOARD_READBACK_PREVIEW_CHARS,
)


class AgentTaskTaskBoardReadbackMixin(AgentTaskMixinBase):
    """TaskBoard cold evidence readback and hot/cold ref projection."""

    def _taskboard_contiguous_read_end(
        self,
        ranges: Sequence[Mapping[str, Any]],
    ) -> int:
        normalized = sorted(
            (
                (
                    self._coerce_non_negative_int(item.get("offset")),
                    self._coerce_non_negative_int(item.get("end")),
                )
                for item in ranges
                if isinstance(item, Mapping)
            ),
            key=lambda item: (item[0], item[1]),
        )
        contiguous_end = 0
        for offset, end in normalized:
            if end <= contiguous_end:
                continue
            if offset > contiguous_end:
                break
            contiguous_end = end
        return contiguous_end

    def _taskboard_read_progress_records(
        self,
        *,
        owner: str,
        locator: str,
        content_version: str,
    ) -> list[Mapping[str, Any]]:
        state = getattr(self, "_taskboard_read_progress", None)
        raw_items = state.get("items") if isinstance(state, Mapping) else None
        if not isinstance(raw_items, Mapping):
            return []
        records: list[Mapping[str, Any]] = []
        for record in raw_items.values():
            if not isinstance(record, Mapping):
                continue
            if str(record.get("owner") or "") != owner:
                continue
            if str(record.get("locator") or "") != locator:
                continue
            recorded_version = str(record.get("content_version") or "")
            if content_version:
                if recorded_version != content_version:
                    continue
            elif owner != "action_artifact":
                # A TaskWorkspace locator may change in place. Without a
                # content version, a prior range cannot prove current progress.
                continue
            records.append(record)
        return records

    def _record_taskboard_read_progress(
        self,
        readback: Mapping[str, Any],
        *,
        card_id: str,
    ) -> None:
        if not bool(readback.get("ok")):
            return
        owner = str(readback.get("owner") or "").strip()
        locator = str(
            readback.get("locator")
            or readback.get("selection_key")
            or readback.get("path")
            or ""
        ).strip()
        content_version = str(readback.get("content_version") or "").strip()
        if not owner or not locator:
            return
        raw_range = readback.get("range")
        if isinstance(raw_range, Mapping):
            offset = self._coerce_non_negative_int(raw_range.get("offset"))
            end = self._coerce_non_negative_int(raw_range.get("end"))
        else:
            offset = self._coerce_non_negative_int(readback.get("offset"))
            end = offset + self._coerce_non_negative_int(readback.get("read_bytes"))
        if end <= offset:
            return
        state = getattr(self, "_taskboard_read_progress", None)
        if not isinstance(state, dict):
            state = {
                "schema_version": "agent_task_taskboard_read_progress/v1",
                "items": {},
            }
            self._taskboard_read_progress = state
        items = state.setdefault("items", {})
        if not isinstance(items, dict):
            return
        identity_key = json.dumps(
            [owner, locator, content_version],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        record = items.get(identity_key)
        record = dict(record) if isinstance(record, Mapping) else {
            "owner": owner,
            "locator": locator,
            "content_version": content_version,
            "ranges": [],
        }
        ranges = record.get("ranges")
        ranges = [dict(item) for item in ranges if isinstance(item, Mapping)] if isinstance(
            ranges, Sequence
        ) and not isinstance(ranges, str | bytes | bytearray) else []
        observed_range = {"offset": offset, "end": end}
        if observed_range not in ranges:
            ranges.append(observed_range)
        record["ranges"] = sorted(ranges, key=lambda item: (int(item["offset"]), int(item["end"])))
        record["next_offset"] = self._taskboard_contiguous_read_end(record["ranges"])
        record["total_bytes"] = max(
            self._coerce_non_negative_int(record.get("total_bytes")),
            self._coerce_non_negative_int(readback.get("total_bytes")),
        )
        record["last_card_id"] = card_id
        items[identity_key] = DataFormatter.sanitize(record)

    def _taskboard_next_read_offset(
        self,
        evidence_view: Mapping[str, Any],
        *,
        owner: str,
        locator: str,
        content_version: str,
        include_task_progress: bool = True,
    ) -> int:
        raw_items = evidence_view.get("evidence_items")
        if not isinstance(raw_items, Sequence) or isinstance(
            raw_items, str | bytes | bytearray
        ):
            raw_items = []
        ranges: list[Mapping[str, Any]] = []
        if include_task_progress:
            for progress in self._taskboard_read_progress_records(
                owner=owner,
                locator=locator,
                content_version=content_version,
            ):
                progress_ranges = progress.get("ranges")
                if isinstance(progress_ranges, Sequence) and not isinstance(
                    progress_ranges,
                    str | bytes | bytearray,
                ):
                    ranges.extend(
                        item
                        for item in progress_ranges
                        if isinstance(item, Mapping)
                    )
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            identity = item.get("read_identity")
            if not isinstance(identity, Mapping):
                continue
            if str(identity.get("owner") or "") != owner:
                continue
            if str(identity.get("locator") or "") != locator:
                continue
            recorded_version = str(identity.get("content_version") or "")
            if content_version and recorded_version != content_version:
                continue
            if not self._taskboard_evidence_item_delivered_body(item):
                continue
            read_range = identity.get("range")
            if not isinstance(read_range, Mapping):
                continue
            ranges.append(read_range)
        return self._taskboard_contiguous_read_end(ranges)

    @staticmethod
    def _taskboard_evidence_item_delivered_body(item: Mapping[str, Any]) -> bool:
        state = str(item.get("content_state") or item.get("body_state") or "").strip()
        if state not in {"bounded", "full", "truncated"}:
            return False
        return any(item.get(key) not in (None, "", [], {}) for key in ("body", "content", "preview"))

    def _taskboard_read_target_exhausted(
        self,
        ref: Mapping[str, Any],
        evidence_view: Mapping[str, Any],
    ) -> bool:
        owner = str(
            ref.get("owner")
            or ("action_artifact" if ref.get("selection_key") else None)
            or ("task_workspace" if ref.get("path") else None)
            or ""
        ).strip()
        locator = str(
            ref.get("locator") or ref.get("selection_key") or ref.get("path") or ""
        ).strip()
        content_version = str(
            ref.get("content_version")
            or ref.get("content_version_id")
            or ref.get("sha256")
            or ""
        ).strip()
        if not owner or not locator:
            return False
        total_bytes = self._coerce_non_negative_int(ref.get("bytes", ref.get("size")))
        raw_items = evidence_view.get("evidence_items")
        if isinstance(raw_items, Sequence) and not isinstance(
            raw_items, str | bytes | bytearray
        ):
            for item in raw_items:
                if not isinstance(item, Mapping):
                    continue
                identity = item.get("read_identity")
                if not isinstance(identity, Mapping):
                    continue
                if str(identity.get("owner") or "") != owner:
                    continue
                if str(identity.get("locator") or "") != locator:
                    continue
                recorded_version = str(identity.get("content_version") or "")
                if content_version and recorded_version != content_version:
                    continue
                if not self._taskboard_evidence_item_delivered_body(item):
                    continue
                total_bytes = max(
                    total_bytes,
                    self._coerce_non_negative_int(item.get("total_bytes")),
                )
        if total_bytes <= 0:
            return False
        next_offset = self._taskboard_next_read_offset(
            evidence_view,
            owner=owner,
            locator=locator,
            content_version=content_version,
            # An explicit consumer range asks for body delivery into this
            # consumer and therefore requires evidence in its current view.
            # A range-less artifact target may use task-owned physical progress
            # to avoid rereading bytes that are already completely materialized.
            include_task_progress=not isinstance(ref.get("range"), Mapping),
        )
        return next_offset >= total_bytes

    def _taskboard_requested_read_range(
        self,
        ref: Mapping[str, Any],
        evidence_view: Mapping[str, Any],
        *,
        default_max_bytes: int,
    ) -> tuple[int, int]:
        owner = str(
            ref.get("owner")
            or ("action_artifact" if ref.get("selection_key") else None)
            or ("task_workspace" if ref.get("path") else None)
            or ""
        ).strip()
        locator = str(
            ref.get("locator") or ref.get("selection_key") or ref.get("path") or ""
        ).strip()
        content_version = str(
            ref.get("content_version")
            or ref.get("content_version_id")
            or ref.get("sha256")
            or ""
        ).strip()
        raw_range = ref.get("range")
        raw_range = raw_range if isinstance(raw_range, Mapping) else {}
        if "offset" in raw_range:
            offset = self._coerce_non_negative_int(raw_range.get("offset"))
        else:
            offset = self._taskboard_next_read_offset(
                evidence_view,
                owner=owner,
                locator=locator,
                content_version=content_version,
            )
        max_bytes = self._coerce_positive_int(raw_range.get("max_bytes")) or default_max_bytes
        return offset, max_bytes

    async def _run_taskboard_readback_card(
        self,
        context: Any,
        context_pack: "TaskContextView",
    ) -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        refs = self._taskboard_readback_artifact_refs(evidence_view)
        file_refs = self._taskboard_readback_file_refs(evidence_view)
        card_metadata = getattr(context.card, "metadata", {})
        if isinstance(card_metadata, Mapping):
            target_refs = self._normalize_taskboard_target_refs(
                card_metadata.get("task_workspace_target_refs") or card_metadata.get("target_refs")
            )
            existing_selection_keys = {
                str(ref.get("selection_key") or "")
                for ref in refs
                if isinstance(ref, Mapping)
            }
            for ref in self._taskboard_action_target_ref_artifact_refs(target_refs):
                selection_key = str(ref.get("selection_key") or "")
                if selection_key and selection_key not in existing_selection_keys:
                    existing_selection_keys.add(selection_key)
                    refs.append(ref)
            self._merge_taskboard_file_refs(
                file_refs,
                self._taskboard_task_workspace_target_ref_file_refs(target_refs),
            )
        exhausted_artifact_refs = [
            ref
            for ref in refs
            if self._taskboard_read_target_exhausted(ref, evidence_view)
        ]
        exhausted_file_refs = [
            ref
            for ref in file_refs
            if self._taskboard_read_target_exhausted(ref, evidence_view)
        ]
        refs = [ref for ref in refs if ref not in exhausted_artifact_refs]
        file_refs = [ref for ref in file_refs if ref not in exhausted_file_refs]
        exhausted_ref_count = len(exhausted_artifact_refs) + len(exhausted_file_refs)
        hot_artifact_refs = self._compact_taskboard_artifact_refs_for_hot_payload(refs)
        hot_file_refs = self._compact_taskboard_file_refs_for_hot_payload(file_refs)
        work_unit = WorkUnitIntent(
            id=f"taskboard:{context.card.id}:readback",
            origin="taskboard_card",
            objective=str(getattr(context.card, "objective", "") or "Read scoped cold evidence."),
            input_payload={
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "card": context.card.to_dict(),
                "artifact_refs": hot_artifact_refs,
                "file_refs": hot_file_refs,
                "evidence_scope": evidence_card_ids or "all",
            },
            input_refs=tuple(
                dict(item)
                for item in [
                    *[ref for ref in refs if isinstance(ref, Mapping)],
                    *[ref for ref in file_refs if isinstance(ref, Mapping)],
                ]
                if isinstance(item, Mapping)
            ),
            expected_deliverable={
                "allowed_execution_shape": "readback",
                "artifact_ref_count": len(refs),
                "file_ref_count": len(file_refs),
            },
            evidence_requirements=tuple(
                [
                    {
                        "selection_key": str(ref.get("selection_key") or ""),
                        "source": "taskboard_readback_card",
                    }
                    for ref in refs
                    if isinstance(ref, Mapping)
                ]
                + [
                    {
                        "path": str(ref.get("path") or ""),
                        "source": "taskboard_task_workspace_file_readback",
                    }
                    for ref in file_refs
                    if isinstance(ref, Mapping)
                ]
            ),
            delivery_contract={
                "card": DataFormatter.sanitize(context.card.to_dict()),
                "execution_prompt": {"output_format": "json"},
            },
            retrieval_policy=self._task_context_retrieval_policy(),
            quality_gates=(
                {
                    "kind": "taskboard_artifact_readback_status",
                    "allowed_statuses": ["completed", "blocked", "failed"],
                },
            ),
            runtime_preferences={
                "handler": "agent_task_artifact_readback",
                "plan_block_kind": "action_call",
                "preferred_execution_shape": "taskboard_readback",
                "strategy": "taskboard",
                "card_id": context.card.id,
            },
        )
        carrier_plan = {
            "execution_shape": "taskboard_readback",
            "effective_execution_shape": "taskboard_readback",
            "step_instruction": str(getattr(context.card, "objective", "") or "Read scoped cold evidence."),
            "expected_evidence": [
                {
                    "selection_key": str(ref.get("selection_key") or ""),
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ]
            + [
                {
                    "path": str(ref.get("path") or ""),
                }
                for ref in file_refs
                if isinstance(ref, Mapping)
            ],
            "rationale": "Execute one TaskBoard artifact readback card through the shared Block carrier.",
            "step_scope": {},
        }
        scoped_retrieval = self._taskboard_card_scoped_retrieval(context.card)
        if scoped_retrieval:
            carrier_plan["scoped_retrieval"] = scoped_retrieval

        async def run_readback_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            scoped_payload = self._taskboard_card_payload_with_scoped_retrieval_results({}, _context)
            scoped_results = scoped_payload.get("scoped_retrieval_results", [])
            retrieved_groups = sum(
                bool(item.get("locator_refs") or item.get("evidence_snippets"))
                for item in scoped_results
            )
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.started",
                {
                    "card_id": context.card.id,
                    "ref_count": len(refs),
                    "file_ref_count": len(file_refs),
                },
            )
            readbacks: list[dict[str, Any]] = []
            file_readbacks: list[dict[str, Any]] = []
            effective_file_refs = [dict(ref) for ref in file_refs if isinstance(ref, Mapping)]
            canonical_action_file_refs: list[dict[str, Any]] = []
            diagnostics: list[dict[str, Any]] = []
            readback_evidence_items: list[dict[str, Any]] = []
            if not refs and not file_refs:
                status = "completed" if exhausted_ref_count or retrieved_groups else "blocked"
                success_count = 0
                failed_count = 0
                file_success_count = 0
                file_failed_count = 0
                diagnostics.append(
                    {
                        "code": (
                            "taskboard.readback.no_unread_ranges"
                            if exhausted_ref_count
                            else (
                                "taskboard.readback.context_read"
                                if scoped_retrieval else "taskboard.readback.no_refs"
                            )
                        ),
                        "card_id": context.card.id,
                        "evidence_scope": evidence_card_ids or "all",
                        "exhausted_ref_count": exhausted_ref_count,
                    }
                )
                payload = {
                    "status": status,
                    "answer": (
                        "All scoped Action artifact and TaskWorkspace target ranges were already read; no duplicate read was issued."
                        if exhausted_ref_count
                        else (
                            f"Retrieved scoped Context results for {retrieved_groups} query groups; content completeness is recorded per result."
                            if scoped_retrieval
                            else "No Action artifact refs or TaskWorkspace file refs are available for this readback card."
                        )
                    ),
                    "readbacks": readbacks,
                    "file_readbacks": file_readbacks,
                    "evidence": [],
                    "remaining_work": (
                        []
                        if exhausted_ref_count or retrieved_groups
                        else [
                            "No scoped Context results were returned."
                            if scoped_retrieval
                            else "Upstream cards must produce Action artifact refs or TaskWorkspace file refs before readback can run."
                        ]
                    ),
                    "diagnostics": diagnostics,
                }
            else:
                success_count = 0
                failed_count = 0
                action = getattr(self.agent, "action", None)
                reader = getattr(action, "async_read_action_artifact", None)
                if refs and not callable(reader):
                    success_count = 0
                    failed_count = len(refs)
                    diagnostics.append(
                        {
                            "code": "taskboard.readback.reader_unavailable",
                            "card_id": context.card.id,
                            "ref_count": len(refs),
                        }
                    )
                elif callable(reader):
                    artifact_manager = getattr(action, "_artifact_manager", None)
                    bind_artifact_scope = getattr(artifact_manager, "bind_artifact_scope", None)
                    for ref in refs:
                        selection_key = str(ref.get("selection_key") or "")
                        try:
                            if not selection_key:
                                raise ValueError("Action artifact readback requires selection_key.")
                            if not callable(bind_artifact_scope):
                                raise RuntimeError("Action artifact scope binding is unavailable.")
                            scope_context = cast(
                                ContextManager[dict[str, str]],
                                bind_artifact_scope({"kind": "agent_task", "id": self.id}),
                            )
                            offset, max_bytes = self._taskboard_requested_read_range(
                                ref,
                                evidence_view,
                                default_max_bytes=_TASKBOARD_READBACK_PREVIEW_CHARS,
                            )
                            with scope_context:
                                read_request = reader(
                                    selection_key=selection_key,
                                    offset=offset,
                                    max_bytes=max_bytes,
                                )
                                raw_readback = await self._await_taskboard_card_execution(
                                    cast(Awaitable[Any], read_request),
                                    card_id=context.card.id,
                                    stage="readback",
                                )
                        except Exception as error:
                            raw_readback = {
                                "ok": False,
                                "status": "error",
                                "selection_key": selection_key,
                                "error": (
                                    f"{error.__class__.__name__}: "
                                    + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                                ),
                            }
                        if isinstance(raw_readback, Mapping):
                            raw_value = raw_readback.get(
                                "value",
                                raw_readback.get("data", raw_readback.get("result")),
                            )
                            self._merge_taskboard_file_refs(
                                canonical_action_file_refs,
                                self._taskboard_file_refs_from_action_readbacks(
                                    [{"data": raw_value}]
                                ),
                            )
                        compact = self._compact_taskboard_action_artifact_readback(raw_readback, ref)
                        readbacks.append(compact)
                        self._record_taskboard_read_progress(
                            compact,
                            card_id=context.card.id,
                        )
                        if not compact.get("ok"):
                            diagnostics.append(
                                {
                                    "code": "taskboard.readback.ref_failed",
                                    "selection_key": selection_key,
                                    "status": compact.get("status"),
                                    "error": compact.get("error"),
                                }
                            )

                    success_count = sum(1 for item in readbacks if item.get("ok"))
                    failed_count = len(readbacks) - success_count
                # Canonical TaskWorkspace identity is extracted from the cold
                # Action result before model-hot preview compaction. The hot
                # preview may intentionally replace `path` with requested_path
                # and must not become an identity reconstruction source.
                discovered_file_refs = canonical_action_file_refs
                added_file_refs = self._merge_taskboard_file_refs(effective_file_refs, discovered_file_refs)
                if added_file_refs:
                    diagnostics.append(
                        {
                            "code": "taskboard.readback.task_workspace_file_refs_discovered",
                            "card_id": context.card.id,
                            "file_ref_count": len(added_file_refs),
                        }
                    )

                async def read_task_workspace_ref(ref: Mapping[str, Any]) -> Mapping[str, Any]:
                    path = str(ref.get("path") or "").strip()
                    mode = str(ref.get("readback_mode") or "").strip()
                    offset, max_bytes = self._taskboard_requested_read_range(
                        ref,
                        evidence_view,
                        default_max_bytes=_TASKBOARD_READBACK_PREVIEW_CHARS,
                    )
                    if mode == "record_store_content":
                        segment = await self._await_taskboard_card_execution(
                            self.record_store.read_bounded(path, offset=offset, limit=max_bytes),
                            card_id=context.card.id,
                            stage="task_workspace_content_readback",
                        )
                        return self._taskboard_task_workspace_content_segment_readback(segment, ref)
                    try:
                        return await self._await_taskboard_card_execution(
                            self.task_workspace.read_file(path, max_bytes=max_bytes, offset=offset),
                            card_id=context.card.id,
                            stage="task_workspace_file_readback",
                        )
                    except FileNotFoundError:
                        segment = await self._await_taskboard_card_execution(
                            self.record_store.read_bounded(path, offset=offset, limit=max_bytes),
                            card_id=context.card.id,
                            stage="task_workspace_content_readback",
                        )
                        return self._taskboard_task_workspace_content_segment_readback(segment, ref)

                for ref in effective_file_refs:
                    path = str(ref.get("path") or "").strip()
                    try:
                        raw_file_readback = await read_task_workspace_ref(ref)
                    except Exception as error:
                        raw_file_readback = {
                            "ok": False,
                            "readable": False,
                            "status": "error",
                            "path": path,
                            "error": (
                                f"{error.__class__.__name__}: "
                                + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                            ),
                        }
                    compact_file = self._compact_taskboard_task_workspace_file_readback(raw_file_readback, ref)
                    file_readbacks.append(compact_file)
                    self._record_taskboard_read_progress(
                        compact_file,
                        card_id=context.card.id,
                    )
                    if not compact_file.get("ok"):
                        diagnostics.append(
                            {
                                "code": "taskboard.readback.file_failed",
                                "path": path,
                                "status": compact_file.get("status"),
                                "error": compact_file.get("error"),
                            }
                        )
                file_success_count = sum(1 for item in file_readbacks if item.get("ok"))
                file_failed_count = len(file_readbacks) - file_success_count
                status = "completed" if (success_count + file_success_count + retrieved_groups) > 0 else "failed"
                remaining_work = []
                if failed_count:
                    remaining_work.append(f"{ failed_count } artifact refs could not be read.")
                if file_failed_count:
                    remaining_work.append(f"{ file_failed_count } TaskWorkspace file refs could not be read.")
                readback_evidence_items = [
                    *self._taskboard_action_artifact_readback_evidence_items(
                        readbacks,
                        source="taskboard_readback_card",
                        card_id=context.card.id,
                    ),
                    *self._taskboard_task_workspace_file_readback_evidence_items(
                        file_readbacks,
                        card_id=context.card.id,
                    ),
                ]
                payload = {
                    "status": status,
                    "answer": (
                        f"Read { success_count } of { len(refs) } Action artifact refs and "
                        f"{ file_success_count } of { len(effective_file_refs) } TaskWorkspace file refs with bounded previews."
                    ),
                    "readbacks": readbacks,
                    "file_readbacks": file_readbacks,
                    "file_refs": DataFormatter.sanitize(effective_file_refs),
                    "evidence_items": DataFormatter.sanitize(readback_evidence_items),
                    "evidence": [
                        *[
                            f"artifact:{ item.get('selection_key') } status={ item.get('status') }"
                            for item in readbacks
                            if item.get("selection_key")
                        ],
                        *[
                            f"file:{ item.get('path') } status={ item.get('status') }"
                            for item in file_readbacks
                            if item.get("path")
                        ],
                    ],
                    "remaining_work": remaining_work,
                    "diagnostics": diagnostics,
                }

            if scoped_retrieval:
                payload.update(scoped_payload)
                empty_groups = len(scoped_retrieval.get("query_groups", [])) - retrieved_groups
                if empty_groups > 0 and status == "completed":
                    payload["remaining_work"].append(f"{empty_groups} scoped Context query groups returned no results.")

            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.completed",
                {
                    "card_id": context.card.id,
                    "status": status,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "ref_count": len(refs),
                    "file_success_count": file_success_count,
                    "file_failed_count": file_failed_count,
                    "file_ref_count": len(effective_file_refs),
                },
            )
            execution_diagnostic = {
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(effective_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
            }
            return {
                "execution_result": DataFormatter.sanitize(payload),
                "execution_meta": {
                    "execution_id": f"{self.id}:taskboard:{context.card.id}:readback",
                    "status": status,
                    "route": {
                        "selected_route": "action_artifact_readback",
                        "status": status,
                    },
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [],
                    },
                    "diagnostics": [execution_diagnostic],
                    "artifact_refs": DataFormatter.sanitize(refs),
                    "file_refs": DataFormatter.sanitize(effective_file_refs),
                    "blocks": {
                        "evidence": {
                            "evidence_items": DataFormatter.sanitize(readback_evidence_items),
                        }
                    },
                },
                "action_evidence": [
                    {
                        "kind": "taskboard_artifact_readback",
                        "card_id": context.card.id,
                        "artifact_refs": DataFormatter.sanitize(refs),
                        "file_refs": DataFormatter.sanitize(effective_file_refs),
                        "readbacks": DataFormatter.sanitize(readbacks),
                        "file_readbacks": DataFormatter.sanitize(file_readbacks),
                        "status": status,
                    }
                ],
            }

        try:
            card_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=carrier_plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:taskboard:{context.card.id}:readback",
                handler=run_readback_work_unit,
                start_payload={"card_id": context.card.id, "ref_count": len(refs)},
            )
        except Exception as error:
            return self._failed_taskboard_card_result(
                card_id=context.card.id,
                error=error,
                execution_id=None,
            )

        payload = dict(card_output) if isinstance(card_output, Mapping) else {"status": "failed", "answer": card_output}
        diagnostics = []
        raw_diagnostics = payload.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics)
        success_count = int(payload.get("success_count", 0) or 0) if isinstance(payload.get("success_count"), int) else 0
        readbacks = payload.get("readbacks", [])
        if isinstance(readbacks, Sequence) and not isinstance(readbacks, str | bytes | bytearray):
            success_count = sum(1 for item in readbacks if isinstance(item, Mapping) and item.get("ok"))
        file_readbacks = payload.get("file_readbacks", [])
        file_success_count = 0
        if isinstance(file_readbacks, Sequence) and not isinstance(file_readbacks, str | bytes | bytearray):
            file_success_count = sum(1 for item in file_readbacks if isinstance(item, Mapping) and item.get("ok"))
        file_readback_evidence_items = self._taskboard_task_workspace_file_readback_evidence_items(
            [item for item in file_readbacks if isinstance(item, Mapping)],
            card_id=context.card.id,
        )
        if file_readback_evidence_items:
            existing_items = payload.get("evidence_items")
            existing_sequence = (
                list(existing_items)
                if isinstance(existing_items, Sequence) and not isinstance(existing_items, str | bytes | bytearray)
                else []
            )
            payload["evidence_items"] = DataFormatter.sanitize(
                self._dedupe_taskboard_readback_evidence_items(
                    [
                        *[item for item in existing_sequence if isinstance(item, Mapping)],
                        *file_readback_evidence_items,
                    ]
                )
            )
        result_file_refs = [dict(ref) for ref in file_refs if isinstance(ref, Mapping)]
        raw_result_file_refs = payload.get("file_refs")
        if isinstance(raw_result_file_refs, Sequence) and not isinstance(
            raw_result_file_refs,
            str | bytes | bytearray,
        ):
            result_file_refs = [dict(ref) for ref in raw_result_file_refs if isinstance(ref, Mapping)]
        failed_count = max(0, len(refs) - success_count)
        file_failed_count = max(0, len(result_file_refs) - file_success_count)
        diagnostics.append(
            {
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(result_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
                "block_carrier": self._compact_block_carrier_for_taskboard_meta(
                    execution_meta.get("block_carrier", {}),
                    blocks=execution_meta.get("blocks"),
                ),
            }
        )
        execution_evidence_ledger = self._evidence_ledger_from_execution_meta(cast(Mapping[str, Any], execution_meta))
        payload_evidence_items = payload.get("evidence_items")
        if isinstance(payload_evidence_items, Sequence) and not isinstance(
            payload_evidence_items,
            str | bytes | bytearray,
        ):
            execution_evidence_ledger = evidence_ledger_view(
                {
                    "evidence_items": [
                        *list(execution_evidence_ledger.get("items", [])),
                        *[item for item in payload_evidence_items if isinstance(item, Mapping)],
                    ]
                },
                max_items=80,
                body_chars=2400,
            )
        return TaskBoardCardResult(
            card_id=context.card.id,
            status=str(payload.get("status") or "failed"),
            preview=DataFormatter.sanitize(payload),
            artifact_refs=tuple(refs),
            file_refs=tuple(result_file_refs),
            diagnostics=tuple(diagnostics),
            metadata={
                "execution_id": execution_meta.get("execution_id"),
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(result_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
                "block_carrier": self._compact_block_carrier_for_taskboard_meta(
                    execution_meta.get("block_carrier", {}),
                    blocks=execution_meta.get("blocks"),
                ),
                "evidence_ledger": execution_evidence_ledger,
            },
        )

    @staticmethod
    def _taskboard_action_artifact_recall_records(evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_refs = evidence_view.get("artifact_refs")
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes | bytearray):
            return []
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_refs:
            if not isinstance(item, Mapping):
                continue
            selection_key = str(item.get("selection_key") or "").strip()
            if not selection_key:
                continue
            if selection_key in seen:
                continue
            seen.add(selection_key)
            refs.append(
                {
                    "owner": str(item.get("owner") or "action_artifact"),
                    "locator": str(item.get("locator") or selection_key),
                    "content_version": str(
                        item.get("content_version") or item.get("sha256") or ""
                    ),
                    "selection_key": selection_key,
                    "artifact_type": str(item.get("artifact_type") or ""),
                    "role": str(item.get("role") or ""),
                    "label": str(item.get("label") or ""),
                    "media_type": str(item.get("media_type") or ""),
                    "bytes": item.get("bytes", item.get("size")),
                    "truncated": bool(item.get("truncated")),
                    "preview_omitted": bool(item.get("preview_omitted")),
                    "readback_action_id": str(item.get("readback_action_id") or ""),
                    "full_value_available": bool(item.get("full_value_available", item.get("available", False))),
                }
            )
        if not refs:
            return []
        return [
            {
                "action_id": "taskboard_upstream_evidence",
                "status": "success",
                "artifact_refs": refs,
            }
        ]

    @classmethod
    def _taskboard_readback_artifact_refs(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        records = cls._taskboard_action_artifact_recall_records(evidence_view)
        if not records:
            return []
        refs = records[0].get("artifact_refs")
        if not isinstance(refs, list):
            return []
        return [dict(ref) for ref in refs if isinstance(ref, Mapping)]

    @classmethod
    def _taskboard_readback_file_refs(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def collect(value: Any) -> None:
            if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
                return
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                if not path:
                    continue
                sha = str(item.get("sha256") or "").strip()
                key = (path, sha)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(dict(DataFormatter.sanitize(item)))

        collect(evidence_view.get("file_refs"))
        collect(evidence_view.get("artifact_refs"))
        cards = evidence_view.get("cards")
        if isinstance(cards, Sequence) and not isinstance(cards, str | bytes | bytearray):
            for card in cards:
                if isinstance(card, Mapping):
                    collect(card.get("artifact_refs"))
                    collect(card.get("file_refs"))
        return refs

    @staticmethod
    def _taskboard_file_ref_key(ref: Mapping[str, Any]) -> tuple[str, str]:
        return (str(ref.get("path") or "").strip(), str(ref.get("sha256") or "").strip())

    @classmethod
    def _merge_taskboard_file_refs(
        cls,
        refs: list[dict[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        seen = {cls._taskboard_file_ref_key(ref) for ref in refs if cls._taskboard_file_ref_key(ref)[0]}
        added: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            path = str(candidate.get("path") or "").strip()
            if not path:
                continue
            item = dict(DataFormatter.sanitize(candidate))
            key = cls._taskboard_file_ref_key(item)
            if key in seen:
                continue
            seen.add(key)
            refs.append(item)
            added.append(item)
        return added

    @classmethod
    def _taskboard_file_refs_from_action_readbacks(cls, readbacks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def collect(value: Any, *, source_ref: Mapping[str, Any] | None = None) -> None:
            if not isinstance(value, Mapping):
                return
            raw_refs = value.get("file_refs")
            if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str | bytes | bytearray):
                for raw_ref in raw_refs:
                    if not isinstance(raw_ref, Mapping):
                        continue
                    path = str(raw_ref.get("path") or "").strip()
                    if not path:
                        continue
                    item = dict(DataFormatter.sanitize(raw_ref))
                    if source_ref is not None:
                        item.setdefault("source", "taskboard_action_artifact_readback")
                        selection_key = str(source_ref.get("selection_key") or "").strip()
                        if selection_key:
                            item.setdefault("source_selection_key", selection_key)
                    key = cls._taskboard_file_ref_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(item)
            for key in ("artifact_manifest", "read_preview", "value_preview", "data", "result"):
                nested = value.get(key)
                if isinstance(nested, Mapping):
                    collect(nested, source_ref=source_ref)

        for readback in readbacks:
            if isinstance(readback, Mapping):
                collect(readback, source_ref=readback)
        return refs

    @classmethod
    def _taskboard_task_workspace_readback_evidence_id(cls, prefix: str, path: str, source: str) -> str:
        raw = f"{ prefix }:{ source }:{ path }"
        return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in raw)[:240]

    @classmethod
    def _dedupe_taskboard_readback_evidence_items(
        cls,
        items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            read_identity = item.get("read_identity")
            if isinstance(read_identity, Mapping):
                identity_key = json.dumps(
                    DataFormatter.sanitize(read_identity),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            else:
                identity_key = str(item.get("id") or "").strip()
            if identity_key and identity_key in seen:
                continue
            if identity_key:
                seen.add(identity_key)
            deduped.append(dict(DataFormatter.sanitize(item)))
        return deduped

    def _taskboard_task_workspace_file_readback_evidence_items(
        self,
        file_readbacks: Sequence[Mapping[str, Any]],
        *,
        card_id: str,
    ) -> list[dict[str, Any]]:
        required_paths = {
            self._task_workspace_artifact_display_path(path)
            for path in self._required_task_workspace_deliverables()
        }
        items: list[dict[str, Any]] = []
        for index, readback in enumerate(file_readbacks):
            if not isinstance(readback, Mapping):
                continue
            path = str(readback.get("path") or "").strip()
            if not path:
                continue
            ref = readback.get("ref")
            ref = ref if isinstance(ref, Mapping) else {}
            owner = str(readback.get("owner") or ref.get("owner") or "task_workspace").strip()
            locator = str(
                readback.get("locator") or ref.get("locator") or path
            ).strip()
            content_version = str(
                readback.get("content_version")
                or ref.get("content_version")
                or ref.get("content_version_id")
                or ""
            ).strip()
            offset = self._coerce_non_negative_int(readback.get("offset"))
            read_bytes = self._coerce_non_negative_int(readback.get("read_bytes"))
            read_range = {"offset": offset, "end": offset + read_bytes}
            read_identity = {
                "owner": owner,
                "locator": locator,
                "content_version": content_version,
                "range": read_range,
            }
            logical_path = self._task_workspace_artifact_display_path(path)
            is_task_workspace_artifact = (
                logical_path in required_paths
                or self._is_trusted_task_workspace_artifact_ref(ref)
            )
            source_suffix = "task_workspace_artifact" if is_task_workspace_artifact else "task_workspace_file"
            source = f"agent_task.taskboard.card.{ card_id }.{ source_suffix }"
            prefix = "task_workspace_artifact_readback" if is_task_workspace_artifact else "task_workspace_file_readback"
            evidence_id = self._taskboard_readback_evidence_id(
                prefix,
                owner,
                locator,
                content_version,
                str(read_range["offset"]),
                str(read_range["end"]),
            )
            ok = bool(readback.get("ok"))
            preview = str(readback.get("content_preview") or "")
            preview_meta = readback.get("content_preview_meta")
            preview_meta = preview_meta if isinstance(preview_meta, Mapping) else {}
            truncated = bool(readback.get("truncated")) or bool(preview_meta.get("truncated"))
            item: dict[str, Any] = {
                "id": evidence_id,
                "kind": "task_workspace_artifact.readback" if is_task_workspace_artifact else "task_workspace_file.readback",
                "status": "ok" if ok else "failed",
                "raw_status": readback.get("status") or ("read" if ok else "failed"),
                "body_state": "truncated" if truncated else ("full" if preview else "ref_only"),
                "path": path,
                "owner": owner,
                "locator": locator,
                "content_version": content_version,
                "range": read_range,
                "read_identity": read_identity,
                "total_bytes": readback.get("total_bytes", readback.get("bytes")),
                "source": source,
                "read_bytes": readback.get("read_bytes"),
                "offset": readback.get("offset"),
                "truncated": truncated,
                "aliases": [
                    path,
                    f"{ card_id }:{ path }",
                    f"{ source }:{ path }",
                ],
                "provenance": {
                    "source": source,
                    "taskboard_card_id": card_id,
                    "path": path,
                    "readback_index": index,
                },
                "supports": {
                    "content": bool(ok and preview),
                    "unavailability": not ok,
                    "ref_pointer": False,
                },
            }
            if preview:
                item["body"] = preview
            if preview_meta:
                item["preview_meta"] = dict(DataFormatter.sanitize(preview_meta))
            error = readback.get("error")
            if error:
                item["diagnostics"] = [{"code": "taskboard.readback.file_failed", "message": str(error)}]
            items.append(DataFormatter.sanitize(item))
        return self._dedupe_taskboard_readback_evidence_items(items)

    @staticmethod
    def _taskboard_dependency_ref_needs_readback(ref: Mapping[str, Any]) -> bool:
        selection_key = str(ref.get("selection_key") or "").strip()
        if not selection_key:
            return False
        role = str(ref.get("role") or "").strip().lower()
        if role and role not in {"output", "result", "artifact"}:
            return False
        if not bool(ref.get("available", True)) and not bool(ref.get("full_value_available")):
            return False
        if bool(ref.get("truncated")) or bool(ref.get("preview_omitted")):
            return True
        try:
            size = int(ref.get("bytes", ref.get("size", 0)) or 0)
        except Exception:
            size = 0
        return bool(ref.get("full_value_available")) and size > _TASKBOARD_PROMPT_RESULT_CHARS

    async def _taskboard_dependency_action_artifact_readbacks(
        self,
        evidence_view: Mapping[str, Any],
        *,
        card_id: str,
        context_pack: "TaskContextView",
    ) -> dict[str, Any]:
        candidate_refs = [
            ref
            for ref in self._taskboard_readback_artifact_refs(evidence_view)
            if self._taskboard_dependency_ref_needs_readback(ref)
        ]
        exhausted_refs = [
            ref
            for ref in candidate_refs
            if self._taskboard_read_target_exhausted(ref, evidence_view)
        ]
        refs = [
            ref for ref in candidate_refs if ref not in exhausted_refs
        ][:_TASKBOARD_DEPENDENCY_READBACK_MAX_REFS]
        pending_ref_count = max(
            0,
            len(candidate_refs) - len(exhausted_refs) - len(refs),
        )
        frontier_projection = sorted(
            [
                {
                    "owner": str(ref.get("owner") or "action_artifact"),
                    "locator": str(
                        ref.get("locator") or ref.get("selection_key") or ""
                    ),
                    "content_version": str(
                        ref.get("content_version") or ref.get("sha256") or ""
                    ),
                    "total_bytes": self._coerce_non_negative_int(
                        ref.get("total_bytes", ref.get("bytes"))
                    ),
                }
                for ref in candidate_refs
                if isinstance(ref, Mapping)
            ],
            key=lambda item: (
                item["owner"],
                item["locator"],
                item["content_version"],
                item["total_bytes"],
            ),
        )
        frontier_digest = hashlib.sha256(
            json.dumps(
                frontier_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        hot_artifact_refs = self._compact_taskboard_artifact_refs_for_hot_payload(refs)
        payload: dict[str, Any] = {
            "schema_version": "agent_task_taskboard_dependency_readbacks/v1",
            "card_id": card_id,
            "ref_count": len(refs),
            "exhausted_ref_count": len(exhausted_refs),
            "readbacks": [],
            "diagnostics": (
                [
                    {
                        "code": "taskboard.dependency_readback.no_unread_ranges",
                        "exhausted_ref_count": len(exhausted_refs),
                    }
                ]
                if exhausted_refs and not refs
                else []
            ),
            "bounded": {
                "preview_chars": _TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
                "max_refs": _TASKBOARD_DEPENDENCY_READBACK_MAX_REFS,
            },
            "frontier": {
                "kind": "action_artifact_readback",
                "plan_digest": frontier_digest,
                "candidate_ref_count": len(candidate_refs),
                "attempted_ref_count": len(refs),
                "exhausted_ref_count": len(exhausted_refs),
                "pending_ref_count": pending_ref_count,
                "success_count": 0,
                "failed_count": 0,
                "integrity_complete": bool(candidate_refs)
                and not refs
                and pending_ref_count == 0,
                "exhausted": bool(candidate_refs)
                and len(exhausted_refs) == len(candidate_refs),
            },
        }
        if not refs:
            return payload

        work_unit = WorkUnitIntent(
            id=f"taskboard:{card_id}:dependency-readback",
            origin="taskboard_card",
            objective="Read bounded dependency Action artifact previews before executing the card.",
            input_payload={
                "task_id": self.id,
                "goal": self.goal,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "card_id": card_id,
                "artifact_refs": hot_artifact_refs,
                "bounded": dict(payload["bounded"]),
            },
            input_refs=tuple(dict(item) for item in refs if isinstance(item, Mapping)),
            expected_deliverable={
                "allowed_execution_shape": "dependency_readback",
                "artifact_ref_count": len(refs),
            },
            evidence_requirements=tuple(
                {
                    "selection_key": str(ref.get("selection_key") or ""),
                    "source": "taskboard_dependency_readback",
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ),
            delivery_contract={"execution_prompt": {"output_format": "json"}},
            retrieval_policy=self._task_context_retrieval_policy(),
            quality_gates=(
                {
                    "kind": "taskboard_dependency_readback_status",
                    "allowed_statuses": ["completed", "failed"],
                },
            ),
            runtime_preferences={
                "handler": "agent_task_dependency_artifact_readback",
                "plan_block_kind": "action_call",
                "preferred_execution_shape": "taskboard_dependency_readback",
                "strategy": "taskboard",
                "card_id": card_id,
            },
        )
        carrier_plan = {
            "execution_shape": "taskboard_dependency_readback",
            "effective_execution_shape": "taskboard_dependency_readback",
            "step_instruction": "Read bounded dependency Action artifact previews before executing the card.",
            "expected_evidence": [
                {
                    "selection_key": str(ref.get("selection_key") or ""),
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ],
            "rationale": "Execute TaskBoard dependency artifact prefetch through the shared Block carrier.",
            "step_scope": {},
        }

        async def run_dependency_readback_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            action = getattr(self.agent, "action", None)
            reader = getattr(action, "async_read_action_artifact", None)
            readbacks: list[dict[str, Any]] = []
            diagnostics: list[dict[str, Any]] = []
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.dependency_readback.started",
                {"card_id": card_id, "ref_count": len(refs)},
            )
            if not callable(reader):
                diagnostics.append(
                    {
                        "code": "taskboard.dependency_readback.reader_unavailable",
                        "message": "Action artifact readback is unavailable on the bound Agent.",
                        "ref_count": len(refs),
                    }
                )
            else:
                artifact_manager = getattr(action, "_artifact_manager", None)
                bind_artifact_scope = getattr(artifact_manager, "bind_artifact_scope", None)
                for ref in refs:
                    selection_key = str(ref.get("selection_key") or "")
                    try:
                        if not selection_key:
                            raise ValueError("Action artifact readback requires selection_key.")
                        if not callable(bind_artifact_scope):
                            raise RuntimeError("Action artifact scope binding is unavailable.")
                        scope_context = cast(
                            ContextManager[dict[str, str]],
                            bind_artifact_scope({"kind": "agent_task", "id": self.id}),
                        )
                        offset, max_bytes = self._taskboard_requested_read_range(
                            ref,
                            evidence_view,
                            default_max_bytes=_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
                        )
                        with scope_context:
                            read_request = reader(
                                selection_key=selection_key,
                                offset=offset,
                                max_bytes=max_bytes,
                            )
                            raw_readback = await self._await_taskboard_card_execution(
                                cast(Awaitable[Any], read_request),
                                card_id=card_id,
                                stage="dependency_readback",
                            )
                    except Exception as error:
                        raw_readback = {
                            "ok": False,
                            "status": "error",
                            "selection_key": selection_key,
                            "error": (
                                f"{error.__class__.__name__}: "
                                + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                            ),
                        }
                    compact = self._compact_taskboard_action_artifact_readback(
                        raw_readback,
                        ref,
                        max_chars=_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
                    )
                    readbacks.append(compact)
                    self._record_taskboard_read_progress(
                        compact,
                        card_id=card_id,
                    )
                    if not compact.get("ok"):
                        diagnostics.append(
                            {
                                "code": "taskboard.dependency_readback.ref_failed",
                                "selection_key": selection_key,
                                "status": compact.get("status"),
                                "error": compact.get("error"),
                            }
                        )
            output = dict(payload)
            output["readbacks"] = readbacks
            output["diagnostics"] = diagnostics
            output["evidence_items"] = self._taskboard_action_artifact_readback_evidence_items(
                readbacks,
                source="taskboard_dependency_readback",
                card_id=card_id,
            )
            output["success_count"] = sum(1 for item in readbacks if item.get("ok"))
            failed_count = len(readbacks) - int(output["success_count"])
            output["frontier"] = {
                **dict(payload["frontier"]),
                "success_count": int(output["success_count"]),
                "failed_count": failed_count,
                "integrity_complete": (
                    bool(candidate_refs)
                    and pending_ref_count == 0
                    and failed_count == 0
                ),
                "exhausted": (
                    bool(candidate_refs)
                    and pending_ref_count == 0
                    and failed_count == 0
                    and len(exhausted_refs) + int(output["success_count"])
                    == len(candidate_refs)
                ),
            }
            status = "completed" if int(output["success_count"]) > 0 else "failed"
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.dependency_readback.completed",
                {
                    "card_id": card_id,
                    "ref_count": len(refs),
                    "success_count": output["success_count"],
                    "failed_count": failed_count,
                },
            )
            return {
                "execution_result": DataFormatter.sanitize(output),
                "execution_meta": {
                    "execution_id": f"{self.id}:taskboard:{card_id}:dependency-readback",
                    "status": status,
                    "route": {
                        "selected_route": "action_artifact_dependency_readback",
                        "status": status,
                    },
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [],
                    },
                    "diagnostics": [
                        {
                            "execution_kind": "taskboard_dependency_artifact_readback",
                            "execution_strategy": self.execution_strategy,
                            "card_id": card_id,
                            "ref_count": len(refs),
                            "success_count": output["success_count"],
                            "failed_count": failed_count,
                        }
                    ],
                    "artifact_refs": DataFormatter.sanitize(refs),
                    "blocks": {
                        "evidence": {
                            "evidence_items": DataFormatter.sanitize(output["evidence_items"]),
                        }
                    },
                },
                "action_evidence": [
                    {
                        "kind": "taskboard_dependency_artifact_readback",
                        "card_id": card_id,
                        "artifact_refs": DataFormatter.sanitize(refs),
                        "readbacks": DataFormatter.sanitize(readbacks),
                        "status": status,
                    }
                ],
            }

        try:
            readback_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=carrier_plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:taskboard:{card_id}:dependency-readback",
                handler=run_dependency_readback_work_unit,
                start_payload={"card_id": card_id, "ref_count": len(refs)},
            )
        except Exception as error:
            payload["diagnostics"] = [
                {
                    "code": "taskboard.dependency_readback.execution_failed",
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "ref_count": len(refs),
                }
            ]
            return DataFormatter.sanitize(payload)

        output_payload = dict(readback_output) if isinstance(readback_output, Mapping) else payload
        compact_carrier = self._compact_block_carrier_for_taskboard_meta(
            execution_meta.get("block_carrier", {}),
            blocks=execution_meta.get("blocks"),
        )
        self.diagnostics.setdefault("taskboard_dependency_readback_block_carriers", []).append(
            {
                "card_id": card_id,
                "ref_count": len(refs),
                "block_carrier": compact_carrier,
            }
        )
        return DataFormatter.sanitize(output_payload)

    @classmethod
    def _taskboard_dependency_readback_evidence_items(cls, dependency_readbacks: Any) -> list[dict[str, Any]]:
        if not isinstance(dependency_readbacks, Mapping):
            return []
        raw_items = dependency_readbacks.get("evidence_items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, str | bytes | bytearray):
            return []
        return [dict(DataFormatter.sanitize(item)) for item in raw_items if isinstance(item, Mapping)]

    @staticmethod
    def _taskboard_dependency_readback_frontier(
        dependency_readbacks: Any,
    ) -> dict[str, Any]:
        if not isinstance(dependency_readbacks, Mapping):
            return {}
        frontier = dependency_readbacks.get("frontier")
        if not isinstance(frontier, Mapping):
            return {}
        return dict(DataFormatter.sanitize(frontier))

    @classmethod
    def _taskboard_action_artifact_readback_evidence_items(
        cls,
        readbacks: Sequence[Any],
        *,
        source: str,
        card_id: str = "",
    ) -> list[dict[str, Any]]:
        if not isinstance(readbacks, Sequence) or isinstance(readbacks, str | bytes | bytearray):
            return []
        items: list[dict[str, Any]] = []
        for index, readback in enumerate(readbacks):
            if not isinstance(readback, Mapping):
                continue
            selection_key = str(readback.get("selection_key") or "").strip()
            value_preview = readback.get("value_preview")
            body = cls._taskboard_readback_evidence_body(value_preview)
            preview_meta = readback.get("value_preview_meta")
            truncated = bool(preview_meta.get("truncated")) if isinstance(preview_meta, Mapping) else False
            ok = bool(readback.get("ok"))
            status = "ok" if ok and body else ("empty" if ok else "failed")
            body_state = "truncated" if ok and body and truncated else ("bounded" if ok and body else "ref_only")
            raw_status = str(readback.get("status") or status)
            ref = readback.get("ref")
            ref = ref if isinstance(ref, Mapping) else {}
            owner = str(readback.get("owner") or ref.get("owner") or "action_artifact").strip()
            locator = str(
                readback.get("locator") or ref.get("locator") or selection_key
            ).strip()
            content_version = str(
                readback.get("content_version")
                or ref.get("content_version")
                or ref.get("sha256")
                or ""
            ).strip()
            raw_range = readback.get("range")
            raw_range = raw_range if isinstance(raw_range, Mapping) else {}
            read_range = {
                "offset": cls._coerce_non_negative_int(raw_range.get("offset")),
                "end": cls._coerce_non_negative_int(raw_range.get("end")),
            }
            read_identity = {
                "owner": owner,
                "locator": locator,
                "content_version": content_version,
                "range": read_range,
            }
            evidence_id = cls._taskboard_readback_evidence_id(
                "taskboard_action_artifact_readback",
                owner,
                locator,
                content_version,
                str(read_range["offset"]),
                str(read_range["end"]),
            )
            item: dict[str, Any] = {
                "id": evidence_id,
                "kind": "taskboard_action_artifact.readback",
                "status": status,
                "raw_status": raw_status,
                "body_state": body_state,
                "selection_key": selection_key,
                "aliases": cls._taskboard_readback_evidence_aliases(readback),
                "source": source,
                "owner": owner,
                "locator": locator,
                "content_version": content_version,
                "range": read_range,
                "read_identity": read_identity,
                "total_bytes": readback.get("total_bytes"),
                "provenance": {
                    "source": source,
                    "taskboard_card_id": card_id,
                    "selection_key": selection_key,
                },
                "supports": {
                    "content": status == "ok" and body_state in {"bounded", "truncated"},
                    "unavailability": status in {"failed", "empty"},
                    "ref_pointer": False,
                },
            }
            if isinstance(ref, Mapping):
                item["ref"] = DataFormatter.sanitize(dict(ref))
                for field in ("path", "label", "role", "artifact_type"):
                    value = ref.get(field)
                    if value not in (None, "", [], {}):
                        item[field] = DataFormatter.sanitize(value)
            if body:
                item["body"] = body
            error = readback.get("error")
            if error:
                item["diagnostics"] = [
                    {
                        "code": "taskboard.action_artifact_readback.failed",
                        "message": cls._truncate_prompt_text(error, 1200),
                    }
                ]
            items.append(DataFormatter.sanitize(item))
        return items

    @staticmethod
    def _taskboard_readback_evidence_id(*parts: str) -> str:
        raw = ":".join(str(part or "").strip() for part in parts if str(part or "").strip())
        return "".join(ch if ch.isalnum() or ch in "._:-/" else "_" for ch in raw)[:240]

    @classmethod
    def _taskboard_readback_evidence_body(cls, value: Any) -> str:
        if value in (None, "", [], {}):
            return ""
        if isinstance(value, str):
            return cls._truncate_prompt_text(value, _TASKBOARD_READBACK_PREVIEW_CHARS)
        try:
            text = json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
        return cls._truncate_prompt_text(text, _TASKBOARD_READBACK_PREVIEW_CHARS)

    @classmethod
    def _taskboard_readback_evidence_aliases(cls, readback: Mapping[str, Any]) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)

        add(readback.get("selection_key"))
        ref = readback.get("ref")
        if isinstance(ref, Mapping):
            for field in ("selection_key", "path", "label", "artifact_type", "role"):
                add(ref.get(field))
        return aliases[:16]

    @classmethod
    def _compact_taskboard_action_artifact_readback(
        cls,
        readback: Any,
        ref: Mapping[str, Any],
        *,
        max_chars: int = _TASKBOARD_READBACK_PREVIEW_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(readback, Mapping):
            readback = {
                "ok": False,
                "status": "invalid_result",
                "error": f"Action artifact reader returned { type(readback).__name__ }.",
            }
        selection_key = str(readback.get("selection_key") or ref.get("selection_key") or "")
        value = readback.get("value", readback.get("data", readback.get("result")))
        original_chars = cls._serialized_prompt_chars(value)
        preview = cls._compact_taskboard_action_artifact_value_preview(value, max_chars=max_chars)
        preview_chars = cls._serialized_prompt_chars(preview)
        compact: dict[str, Any] = {
            "ok": bool(readback.get("ok")),
            "status": str(readback.get("status") or ""),
            "selection_key": selection_key,
            "owner": str(readback.get("owner") or ref.get("owner") or "action_artifact"),
            "locator": str(readback.get("locator") or ref.get("locator") or selection_key),
            "content_version": str(
                readback.get("content_version")
                or ref.get("content_version")
                or ref.get("sha256")
                or ""
            ),
            "range": DataFormatter.sanitize(readback.get("range", {})),
            "total_bytes": readback.get("total_bytes"),
            "artifact_type": str(readback.get("artifact_type") or ref.get("artifact_type") or ""),
            "label": str(readback.get("label") or ref.get("label") or ""),
            "ref": cls._compact_artifact_ref_for_verifier(ref),
            "value_preview": preview,
            "value_preview_meta": {
                "truncated": bool(readback.get("truncated")) or preview_chars < original_chars,
            },
        }
        error = readback.get("error")
        if error:
            compact["error"] = cls._truncate_prompt_text(error, 1200)
        return compact

    @classmethod
    def _compact_taskboard_artifact_refs_for_hot_payload(cls, refs: Sequence[Any]) -> list[Any]:
        return [cls._compact_artifact_ref_for_verifier(ref) for ref in refs if isinstance(ref, Mapping)]

    @classmethod
    def _compact_taskboard_file_refs_for_hot_payload(cls, refs: Sequence[Any]) -> list[dict[str, Any]]:
        return [
            cls._compact_taskboard_task_workspace_ref_for_prompt(ref)
            for ref in refs
            if isinstance(ref, Mapping)
        ]

    @classmethod
    def _compact_taskboard_action_artifact_value_preview(
        cls,
        value: Any,
        *,
        max_chars: int,
    ) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    structured = json.loads(stripped)
                except (TypeError, ValueError, json.JSONDecodeError):
                    structured = None
                if isinstance(structured, Mapping) or (
                    isinstance(structured, Sequence)
                    and not isinstance(structured, str | bytes | bytearray)
                ):
                    # Action artifact storage may return structured JSON as a
                    # serialized string. Recover its structure before bounded
                    # compaction so one very large leaf cannot hide sibling
                    # records in a middle-truncated text preview.
                    value = structured
        preview = cls._compact_verifier_prompt_value(value, max_chars=max_chars)
        return cls._compact_taskboard_framework_refs_in_hot_value(preview)

    @classmethod
    def _compact_taskboard_framework_refs_in_hot_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            compact: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in {"artifact_refs", "file_refs"}:
                    if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
                        compact[key_text] = [
                            cls._compact_artifact_ref_for_verifier(ref)
                            for ref in item
                            if isinstance(ref, Mapping)
                        ]
                    continue
                if key_text in {"ref", "locator_ref"} and isinstance(item, Mapping):
                    compact[key_text] = cls._compact_artifact_ref_for_verifier(item)
                    continue
                compact[key_text] = cls._compact_taskboard_framework_refs_in_hot_value(item)
            return compact
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return [cls._compact_taskboard_framework_refs_in_hot_value(item) for item in value]
        return value

    @classmethod
    def _compact_taskboard_task_workspace_file_readback(
        cls,
        readback: Any,
        ref: Mapping[str, Any],
        *,
        max_chars: int = _TASKBOARD_READBACK_PREVIEW_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(readback, Mapping):
            readback = {
                "ok": False,
                "readable": False,
                "status": "invalid_result",
                "error": f"TaskWorkspace file reader returned { type(readback).__name__ }.",
            }
        path = str(readback.get("path") or ref.get("path") or "")
        content = readback.get("content", readback.get("text", readback.get("value")))
        original_chars = cls._serialized_prompt_chars(content)
        preview = cls._compact_verifier_prompt_value(content, max_chars=max_chars)
        preview_chars = cls._serialized_prompt_chars(preview)
        ok = bool(readback.get("ok", readback.get("readable", False)))
        compact: dict[str, Any] = {
            "ok": ok,
            "status": str(readback.get("status") or ("completed" if ok else "error")),
            "path": path,
            "owner": str(ref.get("owner") or "task_workspace"),
            "locator": str(ref.get("locator") or path),
            "content_version": str(
                ref.get("content_version")
                or ref.get("content_version_id")
                or readback.get("sha256")
                or ""
            ),
            "read_bytes": readback.get("read_bytes"),
            "total_bytes": readback.get("total_bytes", readback.get("bytes")),
            "offset": readback.get("offset"),
            "truncated": bool(readback.get("truncated")),
            "ref": cls._compact_taskboard_task_workspace_ref_for_prompt(ref),
            "content_preview": preview,
            "content_preview_meta": {
                "truncated": preview_chars < original_chars or bool(readback.get("truncated")),
                "original_chars": original_chars,
                "preview_chars": preview_chars,
                "limit_chars": max_chars,
            },
        }
        error = readback.get("error")
        if error:
            compact["error"] = cls._truncate_prompt_text(error, 1200)
        diagnostics = readback.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
            compact["diagnostics"] = cls._compact_verifier_prompt_value(list(diagnostics), max_chars=1200)
        return compact

    @classmethod
    def _taskboard_task_workspace_content_segment_readback(
        cls,
        segment: Any,
        ref: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(segment, Mapping):
            return {
                "ok": False,
                "readable": False,
                "status": "invalid_result",
                "path": str(ref.get("path") or ""),
                "error": f"TaskWorkspace bounded reader returned { type(segment).__name__ }.",
            }
        envelope = segment.get("ref")
        if not isinstance(envelope, Mapping):
            envelope = {}
        offset = cls._positive_int(segment.get("offset"), default=0)
        read_bytes = cls._positive_int(segment.get("size"), default=0)
        total_size = cls._positive_int(segment.get("total_size"), default=read_bytes)
        eof = bool(segment.get("eof", True))
        return {
            "ok": True,
            "readable": True,
            "status": "completed",
            "path": str(envelope.get("content_ref") or ref.get("path") or ""),
            "owner": str(ref.get("owner") or "record_store"),
            "locator": str(ref.get("locator") or ref.get("path") or ""),
            "content_version": str(
                ref.get("content_version")
                or segment.get("digest")
                or envelope.get("digest")
                or ""
            ),
            "content": segment.get("content", ""),
            "media_type": str(segment.get("content_type") or ""),
            "bytes": total_size,
            "total_bytes": total_size,
            "read_bytes": read_bytes,
            "sha256": str(segment.get("digest") or envelope.get("digest") or ""),
            "offset": offset,
            "truncated": (not eof) or offset > 0 or read_bytes < total_size,
        }

    @staticmethod
    def _compact_taskboard_task_workspace_ref_for_prompt(ref: Mapping[str, Any]) -> dict[str, Any]:
        keep_keys = (
            "path",
            "role",
            "label",
            "source",
            "record_id",
            "collection",
            "kind",
            "content_state",
            "readback_mode",
        )
        return {key: ref.get(key) for key in keep_keys if key in ref and ref.get(key) not in (None, "")}

    @staticmethod
    def _serialized_prompt_chars(value: Any) -> int:
        try:
            return len(json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, default=str))
        except Exception:
            return len(str(value or ""))

    @classmethod
    def _taskboard_available_readback(cls, evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        records = cls._taskboard_action_artifact_recall_records(evidence_view)
        refs = records[0]["artifact_refs"] if records else []
        file_refs = cls._taskboard_readback_file_refs(evidence_view)
        return {
            "schema_version": "agent_task_taskboard_readback/v1",
            "taskboard_readback_shape": {
                "prior_refs_available": bool(refs or file_refs),
                "allowed_execution_shape": "readback",
                "artifact_refs": [cls._compact_artifact_ref_for_verifier(ref) for ref in refs],
                "file_refs": [
                    cls._compact_taskboard_task_workspace_ref_for_prompt(ref)
                    for ref in file_refs
                    if isinstance(ref, Mapping)
                ],
            },
            "action_artifact_readback": {
                "capability_available": True,
                "prior_refs_available": bool(refs),
                "action_id": "read_action_artifact",
                "artifact_refs": [cls._compact_artifact_ref_for_verifier(ref) for ref in refs],
            },
            "task_workspace_file_readback": {
                "prior_file_refs_available": bool(file_refs),
                "file_refs": [
                    cls._compact_taskboard_task_workspace_ref_for_prompt(ref)
                    for ref in file_refs
                    if isinstance(ref, Mapping)
                ],
            },
            "policy": (
                "The listed refs come only from prior TaskBoard evidence. The read_action_artifact capability remains "
                "available even when prior_refs_available is false; current ActionLoop records may expose new "
                "host-issued selection_key values after an Action runs. Use a TaskBoard readback card only when "
                "bounded previews are insufficient and the remaining work is scoped cold Action artifact or "
                "TaskWorkspace file readback. Mixed tool/readback work may still use the ActionRuntime "
                "read_action_artifact action or TaskWorkspace file actions."
            ),
        }


__all__ = ["AgentTaskTaskBoardReadbackMixin"]
