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

from agently.core.orchestration import TaskBoardValidator
from agently.types.data import TaskBoardPatch

from .TaskShared import (
    AgentTaskMixinBase,
    build_task_board_evidence_view,
    Any,
    cast,
    collect_evidence_use,
    DataFormatter,
    Mapping,
    relevant_state_digest,
    scoped_retrieval_policy,
    Sequence,
    TaskBoardRevision,
    TerminalIssue,
    _compact_agent_task_error_message,
    _WORKSPACE_ARTIFACT_PREVIEW_BYTES,
)


_TASKBOARD_WORKSPACE_REPLACE_OLD_KEYS = (
    "old_string",
    "old",
    "from",
    "search",
    "old_text",
    "from_text",
    "search_text",
    "find",
    "find_text",
)
_TASKBOARD_WORKSPACE_REPLACE_NEW_KEYS = (
    "new_string",
    "new",
    "to",
    "replacement",
    "new_text",
    "to_text",
    "replacement_text",
)


class AgentTaskTaskBoardPatchingMixin(AgentTaskMixinBase):
    @classmethod
    def _taskboard_exhausted_retrieval_plans(
        cls,
        revision: Any,
    ) -> list[dict[str, Any]]:
        """Project bounded identities for evidence plans that cannot be replayed."""

        effective_revision = TaskBoardRevision.from_value(revision)
        exhausted: list[dict[str, Any]] = []
        seen_digests: set[str] = set()
        for card in effective_revision.graph.cards:
            evidence_contract = getattr(card, "evidence_contract", None)
            if not isinstance(evidence_contract, Mapping) or str(
                evidence_contract.get("kind") or ""
            ).strip() != "taskboard_final_verification_evidence_reacquisition":
                continue
            metadata = getattr(card, "metadata", None)
            result = effective_revision.card_results.get(card.id)
            result_metadata = getattr(result, "metadata", None)
            proof = (
                result_metadata.get("evidence_reacquisition_proof")
                if isinstance(result_metadata, Mapping)
                else None
            )
            explicitly_exhausted = (
                isinstance(metadata, Mapping)
                and str(metadata.get("superseded_reason") or "").strip()
                == "evidence_reacquisition_plan_exhausted"
            )
            proof_rejected = isinstance(proof, Mapping) and proof.get("satisfied") is False
            if not explicitly_exhausted and not proof_rejected:
                continue
            plan = cls._normalize_scoped_retrieval_plan(
                evidence_contract.get("scoped_retrieval")
                or (metadata.get("scoped_retrieval") if isinstance(metadata, Mapping) else None)
            )
            plan_digest = str(
                evidence_contract.get("scoped_retrieval_plan_digest")
                or (metadata.get("scoped_retrieval_plan_digest") if isinstance(metadata, Mapping) else "")
                or cls._taskboard_scoped_retrieval_plan_digest(plan)
            ).strip()
            if not plan or not plan_digest or plan_digest in seen_digests:
                continue
            seen_digests.add(plan_digest)
            raw_groups = plan.get("query_groups")
            groups = (
                [group for group in raw_groups if isinstance(group, Mapping)]
                if isinstance(raw_groups, Sequence)
                and not isinstance(raw_groups, str | bytes | bytearray)
                else []
            )
            source_kinds = sorted(
                {
                    source_kind
                    for group in groups
                    for source_kind in cls._normalize_string_list(
                        group.get("source_kinds")
                    )
                }
            )
            query_summaries: list[dict[str, Any]] = []
            for group in groups[:8]:
                summary = {
                    "query": str(group.get("query") or "")[:320],
                    "expected_role": str(
                        group.get("expected_role") or ""
                    )[:80],
                    "source_kinds": cls._normalize_string_list(
                        group.get("source_kinds")
                    )[:8],
                }
                for field in ("path", "pattern"):
                    value = str(group.get(field) or "").strip()
                    if value:
                        summary[field] = value[:500]
                filters = group.get("filters")
                if isinstance(filters, Mapping):
                    content_contains = cls._normalize_string_list(
                        filters.get("content_contains")
                    )[:4]
                    if content_contains:
                        summary["content_contains"] = [
                            value[:320] for value in content_contains
                        ]
                query_summaries.append(summary)
            exhausted.append(
                {
                    "card_id": card.id,
                    "plan_digest": plan_digest,
                    "query_group_count": len(groups),
                    "source_kinds": source_kinds,
                    "query_summaries": query_summaries,
                }
            )
            if len(exhausted) >= 8:
                break
        return exhausted

    @classmethod
    def _taskboard_evidence_reacquisition_exhaustion_patch(
        cls,
        context: Any,
        *,
        plan_digest: str,
        frontier_kind: str = "scoped_retrieval",
    ) -> dict[str, Any] | None:
        """Supersede one exhausted evidence plan and its stale generated repair."""

        revision = getattr(context, "revision", None)
        card = getattr(context, "card", None)
        if revision is None or card is None:
            return None
        effective_revision = TaskBoardRevision.from_value(revision)
        current_id = str(getattr(card, "id", "") or "").strip()
        if not current_id:
            return None
        operations: list[dict[str, Any]] = []

        current_card = dict(card.to_dict() if hasattr(card, "to_dict") else {})
        if not current_card:
            return None
        current_metadata = dict(current_card.get("metadata") or {})
        current_metadata.update(
            {
                "superseded_by": "taskboard_final_verification_replan",
                "superseded_reason": "evidence_reacquisition_plan_exhausted",
                "evidence_reacquisition_plan_digest": plan_digest,
                "evidence_reacquisition_frontier_kind": frontier_kind,
            }
        )
        if frontier_kind == "scoped_retrieval":
            current_metadata["scoped_retrieval_plan_digest"] = plan_digest
        current_card.update({"status": "skipped", "metadata": current_metadata})
        operations.append({"op": "update_card", "card": current_card})

        superseded_repair_ids: list[str] = []
        for candidate in effective_revision.graph.cards:
            if current_id not in set(getattr(candidate, "depends_on", ()) or ()):
                continue
            candidate_metadata = getattr(candidate, "metadata", None)
            if not isinstance(candidate_metadata, Mapping) or str(
                candidate_metadata.get("generated_by") or ""
            ).strip() != "agent_task.taskboard.final_verification_repair":
                continue
            candidate_result = effective_revision.card_results.get(candidate.id)
            if candidate_result is not None and str(
                getattr(candidate_result, "status", "") or ""
            ).strip().lower() == "completed":
                continue
            replacement = dict(candidate.to_dict())
            replacement_metadata = dict(replacement.get("metadata") or {})
            replacement_metadata.update(
                {
                    "superseded_by": "taskboard_final_verification_replan",
                    "superseded_reason": (
                        "evidence_reacquisition_dependency_exhausted"
                    ),
                    "exhausted_evidence_card_id": current_id,
                    "evidence_reacquisition_plan_digest": plan_digest,
                    "evidence_reacquisition_frontier_kind": frontier_kind,
                }
            )
            if frontier_kind == "scoped_retrieval":
                replacement_metadata["scoped_retrieval_plan_digest"] = plan_digest
            replacement.update(
                {"status": "skipped", "metadata": replacement_metadata}
            )
            operations.append({"op": "update_card", "card": replacement})
            superseded_repair_ids.append(candidate.id)

        diagnostic = {
            "code": "taskboard.final_repair.evidence_reacquisition_replan_required",
            "message": (
                "The host rejected an evidence-reacquisition result without a new "
                "content identity and superseded the exhausted plan before replanning."
            ),
            "card_id": current_id,
            "evidence_reacquisition_plan_digest": plan_digest,
            "evidence_reacquisition_frontier_kind": frontier_kind,
            "superseded_repair_card_ids": superseded_repair_ids,
        }
        if frontier_kind == "scoped_retrieval":
            diagnostic["scoped_retrieval_plan_digest"] = plan_digest
        operations.extend(
            [
                {"op": "append_diagnostic", "diagnostic": diagnostic},
                {"op": "set_board_status", "status": "running"},
            ]
        )
        return DataFormatter.sanitize(
            {
                "base_revision": effective_revision.revision_id,
                "source": (
                    "agent_task.taskboard.evidence_reacquisition_plan_exhausted"
                ),
                "operations": operations,
                "diagnostics": [diagnostic],
            }
        )

    @classmethod
    def _taskboard_control_output_allows_task_workspace_delivery(cls, card_output: Any) -> bool:
        if not isinstance(card_output, Mapping):
            return True
        status = str(card_output.get("status") or "").strip().lower()
        if status in {"setback", "blocked", "failed", "skipped", "error", "timed_out"}:
            return False
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        if next_action in {"readback", "needs_readback", "repair", "patch", "block", "stop"}:
            return False
        if card_output.get("sufficient") is False:
            return False
        if cls._has_remaining_work(card_output.get("remaining_work")):
            manifest = card_output.get("artifact_manifest")
            manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
            manifest_path = cls._task_workspace_artifact_manifest_path(manifest_dict)
            content, _content_key = cls._select_task_workspace_artifact_content(
                card_output,
                manifest_dict,
                deliverable_mode="task_workspace_artifact",
                manifest_path=manifest_path,
            )
            framework_can_materialize_deliverable = bool(
                cls._task_workspace_artifact_content_is_complete_body(content)
                or (
                    cls._task_workspace_artifact_manifest_needs_body(manifest_dict)
                    and cls._task_workspace_artifact_manifest_has_draftable_outline(
                        manifest_dict
                    )
                )
            )
            if not (
                status == "completed"
                and card_output.get("sufficient") is True
                and next_action == "finalize"
                and framework_can_materialize_deliverable
            ):
                return False
        if cls._has_remaining_work(card_output.get("gaps")):
            if not (status == "completed" and card_output.get("sufficient") is True):
                return False
        return True

    @classmethod
    def _taskboard_control_patch_proposal(
        cls,
        context: Any,
        card_output: Mapping[str, Any],
        diagnostics: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        raw_patch = card_output.get("patch_proposal")
        if isinstance(raw_patch, Mapping):
            if cls._taskboard_patch_proposal_is_task_workspace_patch(raw_patch):
                return None
            try:
                patch = TaskBoardPatch.from_value(raw_patch)
                revision = getattr(context, "revision", None)
                if revision is None:
                    raise ValueError("TaskBoard control patch validation requires a revision.")
                TaskBoardValidator().apply_patch(cast(TaskBoardRevision | Mapping[str, Any], revision), patch)
            except Exception as error:
                diagnostics.append(
                    {
                        "code": "taskboard.control.invalid_model_patch_proposal",
                        "message": _compact_agent_task_error_message(
                            error,
                            fallback="Model patch_proposal was not a valid TaskBoardPatch.",
                        ),
                        "card_id": str(getattr(getattr(context, "card", None), "id", "") or ""),
                        "requested_action": str(raw_patch.get("action") or ""),
                    }
                )
                next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
                if (
                    cls._taskboard_patch_proposal_requests_readback(raw_patch)
                    or next_action in {"readback", "needs_readback"}
                ):
                    target_refs = cls._taskboard_patch_proposal_target_refs(raw_patch)
                    if not target_refs:
                        target_refs = cls._taskboard_control_output_target_refs(card_output)
                    auto_patch_input = dict(card_output)
                    auto_patch_input["next_board_action"] = "readback"
                    return cls._taskboard_control_auto_patch(
                        context,
                        auto_patch_input,
                        target_refs=target_refs,
                    )
                return None
            return DataFormatter.sanitize(patch.to_dict())
        return cls._taskboard_control_auto_patch(
            context,
            card_output,
            target_refs=cls._taskboard_control_output_target_refs(card_output),
        )

    async def _materialize_taskboard_task_workspace_patch(
        self,
        context: Any,
        card_output: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raw_patch = card_output.get("patch_proposal")
        if not isinstance(raw_patch, Mapping) or not self._taskboard_patch_proposal_is_task_workspace_patch(raw_patch):
            return card_output
        card_id = str(getattr(getattr(context, "card", None), "id", "") or "")
        grounding_repair_contract = self._taskboard_grounding_repair_contract(context)
        grounding_patch_paths = self._taskboard_grounding_patch_paths(context)
        repair_carrier = (
            self._terminal_carrier_for_repair_contract(grounding_repair_contract)
            if grounding_repair_contract
            else None
        )
        if grounding_repair_contract:
            scoped, reason = self._taskboard_grounding_task_workspace_patch_scope(
                raw_patch,
                grounding_repair_contract,
                allowed_patch_paths=grounding_patch_paths,
            )
            if not scoped:
                patched_output = dict(card_output)
                patched_output["task_workspace_patch_proposal"] = DataFormatter.sanitize(raw_patch)
                patched_output.pop("patch_proposal", None)
                patched_output["status"] = "blocked"
                patched_output["sufficient"] = False
                patched_output["task_workspace_patch_delivery"] = {
                    "status": "failed",
                    "reason": reason,
                }
                diagnostics = [
                    dict(item)
                    for item in self._taskboard_mapping_sequence(patched_output.get("diagnostics"))
                ]
                diagnostics.append(
                    {
                        "code": "taskboard.control.grounding_patch_out_of_scope",
                        "card_id": str(getattr(getattr(context, "card", None), "id", "") or ""),
                        "message": reason,
                        "source": "agent_task.taskboard.task_workspace_patch",
                    }
                )
                patched_output["diagnostics"] = DataFormatter.sanitize(diagnostics)
                remaining_work = self._normalize_string_list(patched_output.get("remaining_work"))
                if reason and reason not in remaining_work:
                    remaining_work.append(reason)
                patched_output["remaining_work"] = remaining_work
                self.diagnostics.setdefault("taskboard_task_workspace_patch_delivery", []).append(
                    DataFormatter.sanitize(patched_output["task_workspace_patch_delivery"])
                )
                return DataFormatter.sanitize(patched_output)
        patched_output = dict(card_output)
        patched_output["task_workspace_patch_proposal"] = DataFormatter.sanitize(raw_patch)
        patched_output.pop("patch_proposal", None)
        if grounding_repair_contract:
            delivery = await self._apply_grounding_task_workspace_patch(
                raw_patch,
                grounding_repair_contract,
                allowed_patch_paths=grounding_patch_paths,
                source="agent_task.task_workspace_artifact.taskboard_patch",
            )
            delivery = {**delivery, "card_id": card_id}
            if delivery.get("status") == "completed" and repair_carrier is not None:
                next_content_version_id = str(delivery.get("content_version_id") or "").strip()
                readback = delivery.get("readback")
                next_digest = (
                    str(readback.get("sha256") or "").strip()
                    if isinstance(readback, Mapping)
                    else ""
                )
                inventory = self._lifecycle_state.advance_terminal_carrier_version(
                    carrier_id=repair_carrier.carrier_id,
                    expected_content_version_id=repair_carrier.content_version_id,
                    content_version_id=next_content_version_id,
                    content_digest=next_digest,
                    source_work_result_id=f"taskboard_patch:{card_id or 'repair'}",
                    expected_version=self._lifecycle_state.state_version,
                )
                delivery = {
                    **delivery,
                    "terminal_carrier_id": repair_carrier.carrier_id,
                    "terminal_inventory_version": inventory.inventory_version,
                    "terminal_state_version": inventory.state_version,
                }
        else:
            delivery = await self._apply_taskboard_task_workspace_patch(raw_patch, card_id=card_id)
        patched_output["task_workspace_patch_delivery"] = DataFormatter.sanitize(delivery)
        diagnostics = [dict(item) for item in self._taskboard_mapping_sequence(patched_output.get("diagnostics"))]
        if delivery.get("status") == "completed":
            diagnostics.append(
                {
                    "code": "taskboard.control.task_workspace_patch_applied",
                    "card_id": card_id,
                    "path": delivery.get("path"),
                    "operation_count": delivery.get("operation_count", 0),
                    "replacement_count": delivery.get("replacement_count", 0),
                    "source": "agent_task.taskboard.task_workspace_patch",
                }
            )
            file_refs = [dict(item) for item in self._taskboard_mapping_sequence(patched_output.get("file_refs"))]
            file_refs.extend(dict(item) for item in self._taskboard_mapping_sequence(delivery.get("file_refs")))
            patched_output["file_refs"] = DataFormatter.sanitize(self._dedupe_ref_records(file_refs))
            status = str(patched_output.get("status") or "").strip().lower()
            if status not in {"completed", "skipped"}:
                patched_output["status"] = "completed"
            if not patched_output.get("sufficient"):
                patched_output["sufficient"] = True
        else:
            diagnostics.append(
                {
                    "code": "taskboard.control.task_workspace_patch_failed",
                    "card_id": card_id,
                    "path": delivery.get("path"),
                    "reason": delivery.get("reason") or delivery.get("error"),
                    "source": "agent_task.taskboard.task_workspace_patch",
                }
            )
            patched_output["status"] = "blocked"
            patched_output["sufficient"] = False
            remaining_work = self._normalize_string_list(patched_output.get("remaining_work"))
            reason = str(delivery.get("reason") or "TaskWorkspace patch could not be applied.").strip()
            if reason:
                remaining_work.append(reason)
            patched_output["remaining_work"] = remaining_work
        patched_output["diagnostics"] = DataFormatter.sanitize(diagnostics)
        self.diagnostics.setdefault("taskboard_task_workspace_patch_delivery", []).append(
            DataFormatter.sanitize(delivery)
        )
        return DataFormatter.sanitize(patched_output)

    @staticmethod
    def _taskboard_grounding_repair_contract(context: Any) -> dict[str, Any] | None:
        card = getattr(context, "card", None)
        evidence_contract = getattr(card, "evidence_contract", {})
        if not isinstance(evidence_contract, Mapping):
            return None
        grounding_contract = evidence_contract.get("material_claim_repair_contract")
        if not isinstance(grounding_contract, Mapping):
            return None
        return dict(DataFormatter.sanitize(grounding_contract))

    def _taskboard_grounding_patch_paths(self, context: Any) -> list[str]:
        card = getattr(context, "card", None)
        evidence_contract = getattr(card, "evidence_contract", {})
        raw_paths = (
            evidence_contract.get("material_claim_patch_paths")
            if isinstance(evidence_contract, Mapping)
            else None
        )
        candidates = (
            raw_paths
            if isinstance(raw_paths, Sequence)
            and not isinstance(raw_paths, str | bytes | bytearray)
            else [raw_paths]
            if raw_paths is not None
            else []
        )
        paths: list[str] = []
        for item in candidates:
            path = self._task_workspace_artifact_display_path(item)
            if path and path not in paths:
                paths.append(path)
        return paths

    def _taskboard_grounding_task_workspace_patch_scope(
        self,
        patch_proposal: Mapping[str, Any],
        grounding_contract: Mapping[str, Any],
        *,
        allowed_patch_paths: Sequence[Any],
    ) -> tuple[bool, str]:
        return self._grounding_task_workspace_patch_scope(
            patch_proposal,
            grounding_contract,
            allowed_patch_paths=allowed_patch_paths,
            require_exact_claim_coverage=True,
            require_versioned_requirements=True,
        )

    @classmethod
    def _taskboard_patch_proposal_is_task_workspace_patch(cls, patch_proposal: Mapping[str, Any]) -> bool:
        if not isinstance(patch_proposal, Mapping):
            return False
        if cls._taskboard_patch_proposal_is_task_workspace_file_copy(patch_proposal):
            return True
        if any(str(patch_proposal.get(key) or "").strip() for key in ("file", "path", "target_file", "target_path")):
            return True
        raw_operations = cls._taskboard_task_workspace_patch_raw_operations(patch_proposal)
        if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, str | bytes | bytearray):
            return False
        task_workspace_ops = {"replace", "insert", "delete", "append", "write"}
        taskboard_ops = {
            "add_card",
            "update_card",
            "remove_card",
            "record_card_result",
            "append_diagnostic",
            "set_board_status",
            "update_metadata",
            "add_dependency",
            "remove_dependency",
        }
        has_task_workspace_op = False
        for operation in raw_operations:
            if not isinstance(operation, Mapping):
                continue
            op = str(operation.get("type") or operation.get("op") or operation.get("operation") or "").strip()
            if op in taskboard_ops:
                return False
            if op in task_workspace_ops:
                has_task_workspace_op = True
            elif cls._taskboard_task_workspace_patch_operation_has_replace_fields(operation):
                has_task_workspace_op = True
        return has_task_workspace_op

    @classmethod
    def _taskboard_patch_proposal_is_task_workspace_file_copy(cls, patch_proposal: Mapping[str, Any]) -> bool:
        if not isinstance(patch_proposal, Mapping):
            return False
        kind = (
            str(
                patch_proposal.get("kind")
                or patch_proposal.get("type")
                or patch_proposal.get("operation")
                or ""
            )
            .strip()
            .lower()
            .replace("-", "_")
        )
        if kind not in {"task_workspace_file_copy", "file_copy", "copy_file", "copy"}:
            return False
        return bool(
            cls._taskboard_task_workspace_copy_source_path(patch_proposal)
            and cls._taskboard_task_workspace_copy_target_path(patch_proposal)
        )

    @staticmethod
    def _taskboard_task_workspace_copy_source_path(patch_proposal: Mapping[str, Any]) -> str:
        for key in ("source", "source_path", "source_file", "from", "from_path"):
            value = str(patch_proposal.get(key) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _taskboard_task_workspace_copy_target_path(cls, patch_proposal: Mapping[str, Any]) -> str:
        for key in ("target", "target_path", "target_file", "destination", "destination_path", "to", "to_path"):
            value = str(patch_proposal.get(key) or "").strip()
            if value:
                return value
        return cls._taskboard_task_workspace_patch_path(patch_proposal)

    @classmethod
    def _taskboard_task_workspace_patch_path(
        cls,
        patch_proposal: Mapping[str, Any],
        operation: Mapping[str, Any] | None = None,
    ) -> str:
        for source in (operation or {}, patch_proposal):
            for key in ("file", "path", "target_file", "target_path", "task_workspace_path"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    async def _apply_taskboard_task_workspace_patch(
        self,
        patch_proposal: Mapping[str, Any],
        *,
        card_id: str,
    ) -> dict[str, Any]:
        if self._taskboard_patch_proposal_is_task_workspace_file_copy(patch_proposal):
            return await self._apply_taskboard_task_workspace_file_copy_patch(patch_proposal, card_id=card_id)

        raw_operations = self._taskboard_task_workspace_patch_raw_operations(patch_proposal)
        if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, str | bytes | bytearray):
            write_content = self._taskboard_task_workspace_patch_content(patch_proposal)
            if not write_content:
                return {
                    "status": "failed",
                    "card_id": card_id,
                    "reason": "TaskWorkspace patch requires an operations list.",
                }
            raw_operations = [
                {
                    "type": "write",
                    "path": self._taskboard_task_workspace_patch_path(patch_proposal),
                    "content": write_content,
                }
            ]
        operations = [dict(item) for item in raw_operations if isinstance(item, Mapping)]
        if not operations:
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "TaskWorkspace patch has no valid operations.",
            }
        path = self._taskboard_task_workspace_patch_path(patch_proposal, operations[0])
        if not path:
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "TaskWorkspace patch requires file/path.",
            }
        try:
            first_op = str(
                operations[0].get("type") or operations[0].get("op") or operations[0].get("operation") or ""
            ).strip().lower()
            content = "" if first_op in {"write", "overwrite", "replace_file"} else await self._read_task_workspace_patch_text(path)
            operation_records: list[dict[str, Any]] = []
            replacement_count = 0
            for index, operation in enumerate(operations):
                operation_path = self._taskboard_task_workspace_patch_path(patch_proposal, operation)
                if operation_path != path:
                    raise ValueError("TaskWorkspace patch operations must target one file per patch proposal.")
                content, record = self._apply_taskboard_task_workspace_patch_operation(
                    content,
                    operation,
                    index=index,
                )
                replacement_count += int(record.get("replacement_count") or 0)
                operation_records.append(record)
            write_result = await self.task_workspace.write_file(path, content, append=False)
            read_result = await self.task_workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            return {
                "status": "failed",
                "card_id": card_id,
                "path": path,
                "reason": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                "error": {"type": error.__class__.__name__},
            }
        trusted_refs = [
            dict(item)
            for item in write_result.get("file_refs", [])
            if isinstance(item, Mapping)
        ]
        ref = {
            **(trusted_refs[0] if trusted_refs else {}),
            "path": str(read_result.get("path") or path),
            "bytes": int(read_result.get("bytes") or write_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or write_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": read_result.get("content_kind", "text"),
            "encoding": read_result.get("encoding"),
            "handler_id": read_result.get("handler_id"),
            "role": "task_workspace_artifact",
            "source": "agent_task.task_workspace_artifact.taskboard_patch",
            "card_id": card_id,
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "truncated": bool(read_result.get("truncated")),
            "preview": self._truncate_prompt_text(str(read_result.get("content") or ""), _WORKSPACE_ARTIFACT_PREVIEW_BYTES),
        }
        return {
            "status": "completed",
            "card_id": card_id,
            "path": path,
            "operation_count": len(operation_records),
            "replacement_count": replacement_count,
            "operations": operation_records,
            "write": {
                "path": str(write_result.get("path") or path),
                "bytes": int(write_result.get("bytes") or 0),
                "sha256": str(write_result.get("sha256") or ""),
            },
            "readback": {
                "path": ref["path"],
                "bytes": ref["bytes"],
                "sha256": ref["sha256"],
                "read_bytes": ref["read_bytes"],
                "truncated": ref["truncated"],
                "handler_id": ref["handler_id"],
            },
            "file_refs": [ref],
        }

    async def _apply_taskboard_task_workspace_file_copy_patch(
        self,
        patch_proposal: Mapping[str, Any],
        *,
        card_id: str,
    ) -> dict[str, Any]:
        source_path = self._taskboard_task_workspace_copy_source_path(patch_proposal)
        target_path = self._taskboard_task_workspace_copy_target_path(patch_proposal)
        if not source_path or not target_path:
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "TaskWorkspace file-copy patch requires source and target paths.",
            }
        previous_sha = ""
        try:
            source_target = self.task_workspace.resolve_file_path(source_path)
            max_bytes = max(int(source_target.stat().st_size) + 1, _WORKSPACE_ARTIFACT_PREVIEW_BYTES)
            source_read = await self.task_workspace.read_file(source_path, max_bytes=max_bytes)
            content = source_read.get("content")
            if not isinstance(content, str) or bool(source_read.get("truncated")):
                raise ValueError("TaskWorkspace file-copy patch requires complete text readback.")
            try:
                previous_read = await self.task_workspace.read_file(target_path, max_bytes=1)
                previous_sha = str(previous_read.get("sha256") or "")
            except FileNotFoundError:
                previous_sha = ""
            write_result = await self.task_workspace.write_file(target_path, content, append=False)
            read_result = await self.task_workspace.read_file(target_path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            return {
                "status": "failed",
                "card_id": card_id,
                "source_path": source_path,
                "target_path": target_path,
                "reason": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                "error": {"type": error.__class__.__name__},
            }

        trusted_refs = [
            dict(item)
            for item in write_result.get("file_refs", [])
            if isinstance(item, Mapping)
        ]
        ref = {
            **(trusted_refs[0] if trusted_refs else {}),
            "path": str(read_result.get("path") or target_path),
            "bytes": int(read_result.get("bytes") or write_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or write_result.get("sha256") or ""),
            "media_type": read_result.get("media_type") or source_read.get("media_type"),
            "content_kind": read_result.get("content_kind", "text"),
            "encoding": read_result.get("encoding") or source_read.get("encoding"),
            "handler_id": read_result.get("handler_id"),
            "role": "task_workspace_artifact",
            "source": "agent_task.task_workspace_artifact.taskboard_patch",
            "source_path": source_path,
            "card_id": card_id,
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "truncated": bool(read_result.get("truncated")),
            "preview": self._truncate_prompt_text(str(read_result.get("content") or ""), _WORKSPACE_ARTIFACT_PREVIEW_BYTES),
        }
        return {
            "status": "completed",
            "card_id": card_id,
            "path": target_path,
            "source_path": source_path,
            "operation_count": 1,
            "replacement_count": 0 if previous_sha and previous_sha == ref["sha256"] else 1,
            "operations": [
                {
                    "index": 0,
                    "type": "copy",
                    "source": source_path,
                    "target": target_path,
                }
            ],
            "write": {
                "path": str(write_result.get("path") or target_path),
                "bytes": int(write_result.get("bytes") or 0),
                "sha256": str(write_result.get("sha256") or ""),
            },
            "readback": {
                "path": ref["path"],
                "bytes": ref["bytes"],
                "sha256": ref["sha256"],
                "read_bytes": ref["read_bytes"],
                "truncated": ref["truncated"],
                "handler_id": ref["handler_id"],
            },
            "file_refs": [ref],
        }

    @staticmethod
    def _taskboard_task_workspace_patch_content(patch_proposal: Mapping[str, Any]) -> str:
        for key in ("content", "markdown", "body", "text", "new", "replacement"):
            value = patch_proposal.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _taskboard_task_workspace_patch_raw_operations(patch_proposal: Mapping[str, Any]) -> Any:
        for key in ("operations", "edits", "patches"):
            value = patch_proposal.get(key)
            if value is not None:
                return value
        return None

    async def _read_task_workspace_patch_text(self, path: str) -> str:
        target = self.task_workspace.resolve_file_path(path)
        max_bytes = max(int(target.stat().st_size) + 1, _WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        read_result = await self.task_workspace.read_file(path, max_bytes=max_bytes)
        if not bool(read_result.get("ok")):
            raise ValueError(f"TaskWorkspace file could not be read for patch: { path }")
        if bool(read_result.get("truncated")):
            raise ValueError(f"TaskWorkspace patch requires complete readback before editing: { path }")
        content = read_result.get("content")
        if not isinstance(content, str):
            raise ValueError(f"TaskWorkspace patch requires text content: { path }")
        return content

    @classmethod
    def _apply_taskboard_task_workspace_patch_operation(
        cls,
        content: str,
        operation: Mapping[str, Any],
        *,
        index: int,
    ) -> tuple[str, dict[str, Any]]:
        op = str(operation.get("type") or operation.get("op") or operation.get("operation") or "").strip().lower()
        if not op and cls._taskboard_task_workspace_patch_operation_has_replace_fields(operation):
            op = "replace"
        if op in {"write", "overwrite", "replace_file"}:
            new_content = cls._taskboard_task_workspace_patch_content(operation)
            if not new_content:
                raise ValueError("TaskWorkspace write patch requires non-empty content.")
            return new_content, {
                "index": index,
                "type": "write",
                "replacement_count": 1 if content != new_content else 0,
            }
        if op != "replace":
            raise ValueError(f"Unsupported TaskWorkspace patch operation '{ op or '<empty>' }'.")
        old = cls._first_present_patch_string(
            operation,
            _TASKBOARD_WORKSPACE_REPLACE_OLD_KEYS,
        )
        if not old:
            raise ValueError("TaskWorkspace replace patch requires non-empty old/from/search text.")
        new = cls._first_present_patch_string(
            operation,
            _TASKBOARD_WORKSPACE_REPLACE_NEW_KEYS,
            default="",
        )
        match_count = content.count(old)
        if match_count <= 0:
            raise ValueError("TaskWorkspace replace patch old text was not found.")
        replace_all = cls._normalize_bool(operation.get("replace_all"), default=False)
        occurrence = cls._coerce_positive_int(operation.get("occurrence"))
        if occurrence is not None:
            if occurrence > match_count:
                raise ValueError("TaskWorkspace replace patch occurrence is greater than match count.")
            patched = cls._replace_nth(content, old, new, occurrence)
            replacement_count = 1
        elif replace_all:
            patched = content.replace(old, new)
            replacement_count = match_count
        elif match_count == 1:
            patched = content.replace(old, new, 1)
            replacement_count = 1
        else:
            raise ValueError(
                "TaskWorkspace replace patch matched multiple locations; set occurrence or replace_all explicitly."
            )
        return patched, {
            "index": index,
            "type": "replace",
            "match_count": match_count,
            "replacement_count": replacement_count,
        }

    @staticmethod
    def _replace_nth(content: str, old: str, new: str, occurrence: int) -> str:
        start = -1
        search_from = 0
        for _ in range(occurrence):
            start = content.find(old, search_from)
            if start < 0:
                return content
            search_from = start + len(old)
        return content[:start] + new + content[start + len(old) :]

    @staticmethod
    def _first_present_patch_string(
        operation: Mapping[str, Any],
        keys: Sequence[str],
        *,
        default: str | None = None,
    ) -> str:
        for key in keys:
            if key in operation:
                return str(operation.get(key) or "")
        return "" if default is None else default

    @staticmethod
    def _taskboard_task_workspace_patch_operation_has_replace_fields(operation: Mapping[str, Any]) -> bool:
        return any(key in operation for key in _TASKBOARD_WORKSPACE_REPLACE_OLD_KEYS) and any(
            key in operation for key in _TASKBOARD_WORKSPACE_REPLACE_NEW_KEYS
        )

    @staticmethod
    def _taskboard_mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return None
        return coerced if coerced > 0 else None

    @staticmethod
    def _taskboard_patch_proposal_requests_readback(patch_proposal: Mapping[str, Any]) -> bool:
        action = str(
            patch_proposal.get("action")
            or patch_proposal.get("next_board_action")
            or patch_proposal.get("patch_type")
            or patch_proposal.get("type")
            or ""
        ).strip().lower()
        return action.replace("-", "_") in {
            "readback",
            "needs_readback",
            "cold_readback",
            "artifact_readback",
            "readback_required",
        }

    @classmethod
    def _taskboard_patch_proposal_target_refs(cls, patch_proposal: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_refs = patch_proposal.get("target_refs") or patch_proposal.get("refs") or patch_proposal.get("urls")
        return cls._normalize_taskboard_target_refs(raw_refs)

    @classmethod
    def _taskboard_control_output_target_refs(cls, card_output: Mapping[str, Any]) -> list[dict[str, Any]]:
        return cls._normalize_taskboard_target_refs(card_output.get("target_refs"))

    @classmethod
    def _normalize_taskboard_target_refs(cls, raw_refs: Any) -> list[dict[str, Any]]:
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes | bytearray):
            return []
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, int, int | None]] = set()
        for item in raw_refs:
            if not isinstance(item, Mapping):
                continue
            owner = str(item.get("owner") or "").strip().lower()
            locator = str(item.get("locator") or "").strip()
            if owner not in {"task_workspace", "record_store", "action_artifact", "external"}:
                continue
            if not locator:
                continue
            content_version = str(item.get("content_version") or "").strip()
            read_range: dict[str, int] = {}
            raw_range = item.get("range")
            if isinstance(raw_range, Mapping):
                offset = cls._coerce_non_negative_int(raw_range.get("offset"))
                max_bytes = cls._coerce_positive_int(raw_range.get("max_bytes"))
                if offset is not None:
                    read_range["offset"] = offset
                if max_bytes is not None:
                    read_range["max_bytes"] = max_bytes
            offset_key = int(read_range.get("offset", 0))
            max_bytes_key = read_range.get("max_bytes")
            key = (owner, locator, content_version, offset_key, max_bytes_key)
            if key in seen:
                continue
            seen.add(key)
            ref: dict[str, Any] = {"owner": owner, "locator": locator}
            if content_version:
                ref["content_version"] = content_version
            if read_range:
                ref["range"] = read_range
            refs.append(ref)
        return refs[:8]

    @staticmethod
    def _taskboard_target_ref_requires_action(ref: Mapping[str, Any]) -> bool:
        return str(ref.get("owner") or "").strip().lower() == "external"

    @classmethod
    def _split_taskboard_target_refs(
        cls,
        refs: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        local_refs: list[dict[str, Any]] = []
        action_refs: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            item = dict(DataFormatter.sanitize(ref))
            if cls._taskboard_target_ref_requires_action(item):
                action_refs.append(item)
            else:
                local_refs.append(item)
        return local_refs, action_refs

    @staticmethod
    def _taskboard_target_ref_label(ref: Mapping[str, Any]) -> str:
        return str(ref.get("locator") or "").strip()

    @classmethod
    def _taskboard_task_workspace_target_ref_file_refs(
        cls,
        refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        file_refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            owner = str(ref.get("owner") or "").strip().lower()
            if owner not in {"task_workspace", "record_store"}:
                continue
            locator = str(ref.get("locator") or "").strip()
            content_version = str(ref.get("content_version") or "").strip()
            key = (owner, locator, content_version)
            if not locator or key in seen:
                continue
            seen.add(key)
            item: dict[str, Any] = {
                "owner": owner,
                "locator": locator,
                "path": locator,
                "source": "taskboard_target_ref",
                "content_state": "ref_only",
                "readback_mode": (
                    "task_workspace_file" if owner == "task_workspace" else "record_store_content"
                ),
            }
            if content_version:
                item["content_version"] = content_version
            if isinstance(ref.get("range"), Mapping):
                item["range"] = DataFormatter.sanitize(ref["range"])
            file_refs.append(item)
        return file_refs

    @classmethod
    def _taskboard_action_target_ref_artifact_refs(
        cls,
        refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, Mapping) or str(ref.get("owner") or "") != "action_artifact":
                continue
            locator = str(ref.get("locator") or "").strip()
            if not locator:
                continue
            item = dict(DataFormatter.sanitize(ref))
            item["selection_key"] = locator
            item.setdefault("role", "output")
            item.setdefault("available", True)
            item.setdefault("full_value_available", True)
            output.append(item)
        return output

    def _taskboard_scoped_retrieval_continuation_patch(
        self,
        context: Any,
        card_output: Mapping[str, Any],
        diagnostics: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        if next_action:
            return None
        status = str(card_output.get("status") or "").strip().lower()
        structurally_insufficient = (
            status in {"blocked", "setback"}
            or card_output.get("sufficient") is False
            or self._has_remaining_work(card_output.get("remaining_work"))
        )
        if not structurally_insufficient:
            return None
        scoped_retrieval = self._taskboard_card_scoped_retrieval(getattr(context, "card", None))
        if not scoped_retrieval:
            return None
        expanded_scoped_retrieval = self._taskboard_expand_scoped_retrieval_for_continuation(scoped_retrieval)
        if not expanded_scoped_retrieval:
            return None
        patch_input = dict(card_output)
        patch_input["next_board_action"] = "readback"
        patch = self._taskboard_control_auto_patch(
            context,
            patch_input,
            scoped_retrieval=expanded_scoped_retrieval,
            source="agent_task.taskboard.scoped_retrieval_continuation",
            diagnostic_code="taskboard.scoped_retrieval.auto_continuation_patch",
            auto_patch_reason="setback_scoped_retrieval_consumer_continuation",
        )
        if patch is None:
            return None
        diagnostics.append(
            {
                "code": "taskboard.scoped_retrieval.auto_continuation_patch",
                "message": (
                    "Converted a structurally insufficient TaskBoard scoped-retrieval card into "
                    "an evidence card plus continuation."
                ),
                "card_id": str(getattr(getattr(context, "card", None), "id", "") or ""),
                "query_group_count": len(expanded_scoped_retrieval.get("query_groups", [])),
            }
        )
        return patch

    @classmethod
    def _taskboard_expand_scoped_retrieval_for_continuation(
        cls,
        scoped_retrieval: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = cls._normalize_scoped_retrieval_plan(scoped_retrieval)
        raw_groups = normalized.get("query_groups")
        if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, str | bytes | bytearray):
            return {}
        bounded_defaults = scoped_retrieval_policy().get("bounded_defaults", {})
        default_snippet_limit = cls._positive_int(
            bounded_defaults.get("snippet_limit") if isinstance(bounded_defaults, Mapping) else None,
            default=1200,
        )
        expanded_groups: list[dict[str, Any]] = []
        for item in raw_groups:
            if not isinstance(item, Mapping):
                continue
            group = dict(item)
            current_limit = cls._positive_int(group.get("snippet_limit"), default=0)
            if current_limit <= 0:
                next_limit = default_snippet_limit
            elif current_limit < default_snippet_limit:
                next_limit = default_snippet_limit
            else:
                next_limit = min(current_limit * 2, 12000)
            group["snippet_limit"] = next_limit
            raw_source_kinds = group.get("source_kinds")
            if isinstance(raw_source_kinds, str):
                source_kinds = {raw_source_kinds.strip()}
            elif isinstance(raw_source_kinds, Sequence) and not isinstance(
                raw_source_kinds, (str, bytes, bytearray)
            ):
                source_kinds = {str(value).strip() for value in raw_source_kinds}
            else:
                source_kinds = set()
            if "task_workspace" in source_kinds or group.get("path") or group.get("pattern"):
                current_context_lines = cls._positive_int(group.get("context_lines"), default=0)
                group["context_lines"] = max(current_context_lines, 3)
            expanded_groups.append(DataFormatter.sanitize(group))
            if len(expanded_groups) >= 8:
                break
        if not expanded_groups:
            return {}
        expanded = {"query_groups": expanded_groups}
        fallback_order = normalized.get("fallback_order")
        if isinstance(fallback_order, Sequence) and not isinstance(fallback_order, str | bytes | bytearray):
            expanded["fallback_order"] = DataFormatter.sanitize(list(fallback_order))
        return expanded

    @staticmethod
    def _positive_int(value: Any, *, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, number)

    @classmethod
    def _taskboard_control_auto_patch(
        cls,
        context: Any,
        card_output: Mapping[str, Any],
        *,
        target_refs: Sequence[Mapping[str, Any]] | None = None,
        scoped_retrieval: Mapping[str, Any] | None = None,
        source: str | None = None,
        diagnostic_code: str = "taskboard.control.auto_readback_patch",
        auto_patch_reason: str = "next_board_action=readback",
    ) -> dict[str, Any] | None:
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        scoped_retrieval_plan = cls._normalize_scoped_retrieval_plan(scoped_retrieval)
        if next_action not in {"readback", "needs_readback"} and not scoped_retrieval_plan:
            return None
        raw_target_refs: Sequence[Mapping[str, Any]] | None = target_refs
        if raw_target_refs is None:
            raw_target_refs = cls._taskboard_control_output_target_refs(card_output)
        target_ref_list = cls._normalize_taskboard_target_refs(raw_target_refs)
        revision = getattr(context, "revision", None)
        card = getattr(context, "card", None)
        if revision is None or card is None:
            return None
        graph = getattr(revision, "graph", None)
        if graph is None or not hasattr(graph, "card_by_id"):
            return None
        existing_ids = set(graph.card_by_id())
        current_id = str(getattr(card, "id", "") or "").strip()
        if not current_id:
            return None
        task_workspace_target_refs, action_target_refs = cls._split_taskboard_target_refs(target_ref_list)
        support_card_needed = bool(scoped_retrieval_plan or action_target_refs)

        def safe_id(raw: str) -> str:
            text = "".join(ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in raw.strip())
            text = text.strip(".-")
            return text or "card"

        def unique_id(prefix: str) -> str:
            base = safe_id(prefix)[:80]
            candidate = base
            index = 1
            while candidate in existing_ids:
                index += 1
                candidate = f"{base}-{index}"
            existing_ids.add(candidate)
            return candidate

        continuation_id = unique_id(f"{current_id}.continue")
        current_card = dict(card.to_dict() if hasattr(card, "to_dict") else {})
        if not current_card:
            return None
        current_metadata = dict(current_card.get("metadata") or {})
        convergence_subject = cls._taskboard_card_convergence_subject(card)
        current_metadata["terminal_convergence_subject"] = convergence_subject
        if (
            str(current_metadata.get("generated_by") or "")
            in {
                "agent_task.taskboard.control_auto_readback",
                "agent_task.taskboard.control_auto_target_refs",
                "agent_task.taskboard.scoped_retrieval_continuation",
            }
            and (
                str(current_metadata.get("readback_card_id") or "").strip()
                or str(current_metadata.get("evidence_card_id") or "").strip()
            )
        ):
            previous_target_refs = cls._normalize_taskboard_target_refs(
                current_metadata.get("target_refs") or current_metadata.get("readback_target_refs")
            )
            previous_keys = {
                json.dumps(ref, ensure_ascii=False, sort_keys=True)
                for ref in previous_target_refs
            }
            target_keys = {
                json.dumps(ref, ensure_ascii=False, sort_keys=True)
                for ref in target_ref_list
            }
            if not target_ref_list or (previous_keys and target_keys.issubset(previous_keys)):
                return None
        patch_source = source or (
            "agent_task.taskboard.control_auto_target_refs"
            if target_ref_list
            else "agent_task.taskboard.control_auto_readback"
        )
        evidence_acquisition_card_id = (
            unique_id(f"{current_id}.evidence")
            if support_card_needed
            else ""
        )
        local_readback_card_id = (
            unique_id(f"{current_id}.readback")
            if task_workspace_target_refs or not support_card_needed
            else ""
        )
        support_card_ids = [
            card_id
            for card_id in (evidence_acquisition_card_id, local_readback_card_id)
            if card_id
        ]
        primary_evidence_card_id = support_card_ids[0]
        current_metadata.update(
            {
                "superseded_by": continuation_id,
                "auto_patch_reason": auto_patch_reason,
            }
        )
        current_card.update(
            {
                "failure_policy": "degradable",
                # The result record preserves the observed setback.  Mark the
                # graph card skipped so it cannot replay after the replacement
                # branch is installed.  Replacement cards depend on its
                # prerequisites, never on this superseded card itself.
                "status": "skipped",
                "metadata": current_metadata,
            }
        )
        dependencies = list(getattr(card, "depends_on", ()) or [])
        readback_dependencies = cls._taskboard_auto_readback_scope(card, graph)
        evidence_dependencies = list(dict.fromkeys(readback_dependencies))
        continuation_dependencies = list(
            # The continuation consumes the prior card's retained result as
            # well as the new support cards.  The support cards themselves do
            # not depend on the superseded card, so they remain runnable.
            dict.fromkeys([current_id, *dependencies, *support_card_ids])
        )
        gaps = cls._normalize_string_list(card_output.get("gaps"))
        remaining_work = cls._normalize_string_list(card_output.get("remaining_work"))
        if scoped_retrieval_plan:
            evidence_objective = "Run expanded bounded scoped retrieval before continuing the setback card."
            if action_target_refs:
                evidence_objective = (
                    f"{evidence_objective} Also collect explicit external target refs: "
                    f"{'; '.join(cls._taskboard_target_ref_label(ref) for ref in action_target_refs)}"
                )
        else:
            evidence_objective = (
                "Collect scoped evidence from the explicit external target refs required before continuing the "
                "setback control card. Target refs: "
                f"{'; '.join(cls._taskboard_target_ref_label(ref) for ref in action_target_refs)}"
            )
        if task_workspace_target_refs:
            local_readback_objective = (
                "Read bounded TaskWorkspace target refs required before continuing the setback control card. "
                "Target refs: "
                f"{'; '.join(cls._taskboard_target_ref_label(ref) for ref in task_workspace_target_refs)}"
            )
        else:
            local_readback_objective = (
                "Read scoped cold evidence required before continuing the setback control card."
            )
        if gaps:
            evidence_objective = f"{evidence_objective} Gaps: {'; '.join(gaps[:3])}"
            local_readback_objective = (
                f"{local_readback_objective} Gaps: {'; '.join(gaps[:3])}"
            )
        continuation_objective = str(getattr(card, "objective", "") or "Continue the setback TaskBoard card.").strip()
        if remaining_work:
            continuation_objective = f"{continuation_objective} Remaining work: {'; '.join(remaining_work[:3])}"
        final_task_workspace_deliverables = cls._normalize_string_list(
            current_metadata.get("final_task_workspace_deliverables")
        )
        if final_task_workspace_deliverables:
            continuation_objective = (
                f"{continuation_objective} Materialize required TaskWorkspace final deliverable path(s): "
                f"{'; '.join(final_task_workspace_deliverables)}"
            )
        shared_evidence_metadata = {
            "evidence_scope": evidence_dependencies,
            "generated_by": patch_source,
            "source_card_id": current_id,
            "terminal_convergence_subject": convergence_subject,
        }
        evidence_acquisition_metadata = dict(shared_evidence_metadata)
        if action_target_refs:
            evidence_acquisition_metadata["target_refs"] = action_target_refs
            evidence_acquisition_metadata["external_target_refs"] = action_target_refs
        if scoped_retrieval_plan:
            evidence_acquisition_metadata["scoped_retrieval"] = DataFormatter.sanitize(scoped_retrieval_plan)
            evidence_acquisition_metadata["retrieval_policy"] = scoped_retrieval_policy()
        local_readback_metadata = dict(shared_evidence_metadata)
        if task_workspace_target_refs:
            local_readback_metadata["target_refs"] = task_workspace_target_refs
            local_readback_metadata["task_workspace_target_refs"] = task_workspace_target_refs
        continuation_metadata: dict[str, Any] = {
            "generated_by": patch_source,
            "continues_card_id": current_id,
            "readback_card_id": local_readback_card_id or primary_evidence_card_id,
            "evidence_card_id": evidence_acquisition_card_id,
            "terminal_convergence_subject": convergence_subject,
        }
        if target_ref_list:
            continuation_metadata["target_refs"] = target_ref_list
        if final_task_workspace_deliverables:
            continuation_metadata["final_task_workspace_deliverables"] = final_task_workspace_deliverables
        evidence_cards: list[dict[str, Any]] = []
        if evidence_acquisition_card_id:
            evidence_cards.append(
                {
                    "id": evidence_acquisition_card_id,
                    "objective": evidence_objective,
                    "depends_on": evidence_dependencies,
                    "required_outputs": (
                        ["Expanded bounded scoped retrieval evidence or diagnostics explaining why it remains insufficient."]
                        if scoped_retrieval_plan
                        else ["Evidence gathered from external target refs or diagnostics explaining inaccessible refs."]
                    ),
                    "allowed_execution_shape": "actions" if action_target_refs else "auto",
                    "failure_policy": "required",
                    "metadata": evidence_acquisition_metadata,
                }
            )
        if local_readback_card_id:
            evidence_cards.append(
                {
                    "id": local_readback_card_id,
                    "objective": local_readback_objective,
                    "depends_on": evidence_dependencies,
                    "required_outputs": (
                        ["Bounded TaskWorkspace target-ref readback previews or diagnostics explaining inaccessible refs."]
                        if task_workspace_target_refs
                        else ["Bounded readback previews for verifier-visible cold evidence."]
                    ),
                    "allowed_execution_shape": "readback",
                    "failure_policy": "required",
                    "metadata": local_readback_metadata,
                }
            )
        patch = {
            "base_revision": str(getattr(revision, "revision_id", "") or ""),
            "source": patch_source,
            "operations": [
                {"op": "update_card", "card": current_card},
                *({"op": "add_card", "card": evidence_card} for evidence_card in evidence_cards),
                {
                    "op": "add_card",
                    "card": {
                        "id": continuation_id,
                        "objective": continuation_objective,
                        "depends_on": continuation_dependencies,
                        "required_outputs": list(getattr(card, "required_outputs", ()) or ()),
                        "allowed_execution_shape": str(getattr(card, "allowed_execution_shape", "") or "control"),
                        "failure_policy": str(getattr(card, "failure_policy", "") or "required"),
                        "metadata": continuation_metadata,
                    },
                },
                {
                    "op": "append_diagnostic",
                    "diagnostic": {
                        "code": "taskboard.control.auto_readback_patch",
                        "source_code": diagnostic_code,
                        "card_id": current_id,
                        "readback_card_id": local_readback_card_id or primary_evidence_card_id,
                        "evidence_card_id": evidence_acquisition_card_id,
                        "continuation_card_id": continuation_id,
                        "target_ref_count": len(target_ref_list),
                        "task_workspace_target_ref_count": len(task_workspace_target_refs),
                        "external_target_ref_count": len(action_target_refs),
                        "scoped_retrieval": bool(scoped_retrieval_plan),
                    },
                },
                {"op": "set_board_status", "status": "running"},
            ],
            "diagnostics": [
                {
                    "code": diagnostic_code,
                    "card_id": current_id,
                    "readback_card_id": local_readback_card_id or primary_evidence_card_id,
                    "evidence_card_id": evidence_acquisition_card_id,
                    "continuation_card_id": continuation_id,
                    "target_ref_count": len(target_ref_list),
                    "task_workspace_target_ref_count": len(task_workspace_target_refs),
                    "external_target_ref_count": len(action_target_refs),
                    "scoped_retrieval": bool(scoped_retrieval_plan),
                }
            ],
        }
        return DataFormatter.sanitize(patch)

    @classmethod
    def _taskboard_auto_readback_scope(cls, card: Any, graph: Any) -> list[str]:
        """Scope auto-readback to direct dependencies plus their upstream evidence."""

        if graph is None or not hasattr(graph, "card_by_id"):
            return list(getattr(card, "depends_on", ()) or [])
        card_by_id = graph.card_by_id()
        ordered: list[str] = []
        seen: set[str] = set()

        def add(card_id: str) -> None:
            if not card_id or card_id in seen:
                return
            seen.add(card_id)
            ordered.append(card_id)

        def visit(card_id: str) -> None:
            add(card_id)
            dependency_card = card_by_id.get(card_id)
            if dependency_card is None:
                return
            for dependency_id in getattr(dependency_card, "depends_on", ()) or ():
                visit(str(dependency_id))

        for dependency_id in getattr(card, "depends_on", ()) or ():
            visit(str(dependency_id))
        return ordered

    @classmethod
    def _taskboard_final_repair_acceptance_evidence_state(
        cls,
        final_verification: Mapping[str, Any],
        *,
        output_subjects: Sequence[str],
    ) -> tuple[TerminalIssue, dict[str, Any], str, list[str]]:
        """Describe verifier-observed progress without counting carrier rewrites.

        A new TaskWorkspace content version proves that a carrier changed, not
        that the acceptance gap gained evidence. This repair-specific state is
        therefore limited to failed structured criterion subjects, verifier-used
        evidence refs, missing capability facts, and the structured replan kind.
        """

        criterion_subjects: list[str] = []
        evidence_refs: list[str] = []

        def add_unique(target: list[str], value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in target:
                target.append(text)

        raw_criterion_checks = final_verification.get("criterion_checks")
        if isinstance(raw_criterion_checks, Sequence) and not isinstance(
            raw_criterion_checks,
            str | bytes | bytearray,
        ):
            for check in raw_criterion_checks:
                if not isinstance(check, Mapping) or check.get("satisfied") is True:
                    continue
                add_unique(criterion_subjects, check.get("criterion_id"))
                for evidence_id in cls._normalize_string_list(check.get("evidence_ids")):
                    add_unique(evidence_refs, evidence_id)

        raw_material_checks = final_verification.get("material_claim_checks")
        if isinstance(raw_material_checks, Sequence) and not isinstance(
            raw_material_checks,
            str | bytes | bytearray,
        ):
            for check in raw_material_checks:
                if not isinstance(check, Mapping):
                    continue
                state = str(check.get("state") or "").strip()
                if state in {"supported", "reasonable_derived", "not_material"}:
                    continue
                add_unique(
                    criterion_subjects,
                    cls._taskboard_material_claim_subject(check),
                )
                for evidence_id in cls._normalize_string_list(check.get("evidence_ids")):
                    add_unique(evidence_refs, evidence_id)

        replan_signal = final_verification.get("replan_signal")
        replan_status = "repair"
        if isinstance(replan_signal, Mapping):
            replan_status = str(replan_signal.get("status") or "repair").strip() or "repair"
            for evidence_id in cls._normalize_string_list(replan_signal.get("evidence_refs")):
                add_unique(evidence_refs, evidence_id)

        missing_capability_ids = cls._normalize_string_list(
            final_verification.get("missing_capability_evidence")
        )
        contract_subject = "|".join(sorted(criterion_subjects)) or "taskboard_final_verification"
        issue = TerminalIssue(
            "taskboard_final_repair",
            "unchanged_acceptance_evidence",
            contract_subject,
        )
        repair_contract = {
            "criterion_subjects": sorted(criterion_subjects),
            "evidence_refs": sorted(evidence_refs),
            "missing_capability_ids": sorted(missing_capability_ids),
            "replan_status": replan_status,
        }
        state_digest = relevant_state_digest(
            {
                "source_reference_targets": sorted(evidence_refs),
                "capability_facts": {
                    capability_id: "missing"
                    for capability_id in sorted(missing_capability_ids)
                },
                "criterion_subjects": sorted(criterion_subjects),
                "output_subjects": sorted(
                    str(item).strip() for item in output_subjects if str(item).strip()
                ),
                "repair_contract": {
                    "replan_status": replan_status,
                },
            }
        )
        return issue, repair_contract, state_digest, sorted(evidence_refs)

    @staticmethod
    def _taskboard_material_claim_subject(check: Mapping[str, Any]) -> str:
        """Return a revision-stable identity for one exact material claim.

        ``claim_key`` is only a model-selection key within one verifier
        response.  Persisting it across TaskBoard rounds can silently bind a
        newly acquired source to a different line after artifact reordering.
        The task host therefore owns a separate target identity based on the
        exact claim text and its stable delivery anchor.  A semantic rewrite is
        intentionally a new subject; semantic equivalence remains model-owned
        and must not be guessed with local text heuristics.
        """

        artifact_quote = str(check.get("artifact_quote") or "").strip()
        if not artifact_quote:
            return ""
        path = str(check.get("path") or "").strip()
        carrier_id = str(check.get("carrier_id") or "").strip()
        anchor = f"path:{path}" if path else f"carrier:{carrier_id}"
        if not path and not carrier_id:
            return ""
        payload = json.dumps(
            {
                "anchor": anchor,
                "artifact_quote": artifact_quote,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"material_claim:{hashlib.sha256(payload).hexdigest()}"

    @classmethod
    def _taskboard_latest_completed_evidence_reacquisition(
        cls,
        revision: Any,
    ) -> dict[str, Any] | None:
        cards = list(getattr(getattr(revision, "graph", None), "cards", ()) or ())
        results = getattr(revision, "card_results", {})
        if not isinstance(results, Mapping):
            return None
        for card in reversed(cards):
            contract = getattr(card, "evidence_contract", None)
            if not isinstance(contract, Mapping) or str(
                contract.get("kind") or ""
            ).strip() != "taskboard_final_verification_evidence_reacquisition":
                continue
            card_id = str(getattr(card, "id", "") or "")
            result = results.get(card_id)
            if result is None or str(getattr(result, "status", "")).strip().lower() != "completed":
                continue
            metadata = getattr(result, "metadata", None)
            proof = (
                metadata.get("evidence_reacquisition_proof")
                if isinstance(metadata, Mapping)
                else None
            )
            if not isinstance(proof, Mapping) or proof.get("satisfied") is not True:
                continue
            return {
                "card_id": card_id,
                "target_subjects": cls._normalize_string_list(
                    contract.get("criterion_subjects")
                ),
                "new_reference_ids": cls._normalize_string_list(
                    proof.get("validated_new_reference_ids")
                ),
            }
        return None

    @classmethod
    def _taskboard_verifier_check_evidence_refs(
        cls,
        final_verification: Mapping[str, Any],
        *,
        target_subjects: Sequence[str],
    ) -> list[str]:
        targets = {
            str(subject).strip()
            for subject in target_subjects
            if str(subject or "").strip()
        }
        if not targets:
            return []
        evidence_refs: list[str] = []

        def collect(raw_checks: Any, *, material_claims: bool = False) -> None:
            if not isinstance(raw_checks, Sequence) or isinstance(
                raw_checks,
                str | bytes | bytearray,
            ):
                return
            for check in raw_checks:
                if not isinstance(check, Mapping):
                    continue
                subject = (
                    cls._taskboard_material_claim_subject(check)
                    if material_claims
                    else str(check.get("criterion_id") or "").strip()
                )
                if subject not in targets:
                    continue
                for evidence_id in cls._normalize_string_list(
                    check.get("evidence_ids")
                ):
                    if evidence_id not in evidence_refs:
                        evidence_refs.append(evidence_id)

        collect(final_verification.get("criterion_checks"))
        collect(
            final_verification.get("material_claim_checks"),
            material_claims=True,
        )
        return evidence_refs

    @classmethod
    def _taskboard_irrelevant_reacquired_evidence_state(
        cls,
        *,
        target_subjects: Sequence[str],
    ) -> tuple[TerminalIssue, dict[str, Any], str]:
        """Track repeated irrelevant reacquisition without treating new refs as progress.

        Different irrelevant source refs must not reset convergence.  Prefer the
        host-issued success-criterion identities as the stable problem subject;
        claim identities are used only when no criterion identity is available.
        """

        normalized_subjects = sorted(
            {
                str(subject).strip()
                for subject in target_subjects
                if str(subject or "").strip()
            }
        )
        criterion_subjects = [
            subject
            for subject in normalized_subjects
            if subject.startswith("criterion:")
        ]
        stable_subjects = criterion_subjects or normalized_subjects
        contract_subject = "|".join(stable_subjects) or "taskboard_final_verification"
        issue = TerminalIssue(
            "taskboard_final_repair",
            "irrelevant_reacquired_evidence",
            contract_subject,
        )
        repair_contract = {
            "criterion_subjects": stable_subjects,
            "relevance_status": "not_used_by_target_check",
        }
        state_digest = relevant_state_digest(
            {
                "criterion_subjects": stable_subjects,
                "repair_contract": {
                    "relevance_status": "not_used_by_target_check",
                },
            }
        )
        return issue, repair_contract, state_digest

    async def _request_taskboard_final_evidence_retrieval_plan(
        self,
        *,
        revision: Any,
        final_verification: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Ask the model for semantic queries while the host owns execution policy."""

        retrieval_policy = self._task_context_retrieval_policy()
        source_kinds = retrieval_policy.get("source_kinds")
        if not isinstance(source_kinds, Mapping) or not source_kinds:
            self.diagnostics.setdefault(
                "taskboard_final_repair_retrieval_plan_errors",
                [],
            ).append(
                {
                    "code": "taskboard.final_verification.no_context_sources",
                    "revision_id": str(getattr(revision, "revision_id", "") or ""),
                }
            )
            return {}
        exhausted_retrieval_plans = self._taskboard_exhausted_retrieval_plans(
            revision
        )
        exhausted_plan_digests = {
            str(item.get("plan_digest") or "")
            for item in exhausted_retrieval_plans
            if str(item.get("plan_digest") or "")
        }
        request = self.agent.create_temp_request()
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "verification_gap": {
                    "reason": str(final_verification.get("reason") or ""),
                    "missing_criteria": self._normalize_string_list(
                        final_verification.get("missing_criteria")
                    ),
                    "next_step_requirements": self._normalize_string_list(
                        final_verification.get("next_step_requirements")
                    ),
                    "acceptance_delta": self._normalize_string_list(
                        final_verification.get("acceptance_delta")
                    ),
                    "replan_signal": DataFormatter.sanitize(
                        final_verification.get("replan_signal") or {}
                    ),
                },
                "retrieval_policy": retrieval_policy,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "language_policy": self._language_policy(),
                "board_state": {
                    "revision_id": str(
                        getattr(revision, "revision_id", "") or ""
                    ),
                    "completed_card_ids": [
                        str(card_id)
                        for card_id, result in dict(
                            getattr(revision, "card_results", {}) or {}
                        ).items()
                        if str(getattr(result, "status", "") or "").strip().lower()
                        == "completed"
                    ],
                    "exhausted_retrieval_plans": exhausted_retrieval_plans,
                },
            }
        )
        request.instruct(
            "Plan only the bounded TaskContext retrieval needed to close the stated verification gap. "
            "Choose semantic query text and exact source_kinds only from retrieval_policy.source_kinds. "
            "Return scoped_retrieval.query_groups; do not choose lexical/vector mechanisms, do not call Actions, "
            "and do not write the final artifact. TaskContext/ContextReader owns source reads. TaskWorkspace "
            "Actions cannot read a pinned repository, RecordStore, Skill, or external Context source unless it "
            "was explicitly materialized into TaskWorkspace. Keep every max_results positive. The host will "
            "validate capacity and deterministically split an otherwise valid over-capacity plan into ordered "
            "TaskBoard batches without changing query semantics. Use expected_role=evidence_snippet when the "
            "verifier needs body evidence and locator_ref only for discovery. path is a file or directory scope, "
            "and pattern is only a file-name glob such as '*.py' or '**'; never put a code symbol or content "
            "phrase in pattern. When an exact symbol or text fragment must position a bounded read inside a known "
            "source file, put that exact locator in filters.content_contains (one exact locator per query group). "
            "board_state.exhausted_retrieval_plans lists host-owned digests of plans that produced no new "
            "eligible evidence identity. When that list is non-empty, return a semantically changed plan; do not "
            "repeat an exhausted plan. The host will reject a digest-identical replacement before execution."
        )
        request.output(
            {
                "scoped_retrieval": (
                    dict,
                    "Required bounded retrieval plan: {query_groups: [{query, expected_role, source_kinds, path?, pattern?, filters?: {content_contains?: [exact source text]}, max_results?, snippet_limit?}]}. pattern is a file-name glob only; use filters.content_contains for exact code/text locators. Source kinds must be exact offered keys.",
                    True,
                ),
                "planning_summary": (
                    str,
                    "One concise summary of why these queries cover the verification gap.",
                    False,
                ),
            },
            format="json",
        )
        result_handle = request.get_result()
        raw_result = await self._await_task_request(
            result_handle.async_get_data(),
            stage="taskboard_evidence_retrieval_plan",
        )
        raw_plan = (
            raw_result.get("scoped_retrieval")
            if isinstance(raw_result, Mapping)
            else None
        )
        plan = self._normalize_scoped_retrieval_plan(raw_plan)
        raw_groups = plan.get("query_groups")
        groups = (
            [dict(group) for group in raw_groups if isinstance(group, Mapping)]
            if isinstance(raw_groups, Sequence)
            and not isinstance(raw_groups, str | bytes | bytearray)
            else []
        )
        offered_source_kinds = {str(key) for key in source_kinds}
        invalid_source_kinds: list[str] = []
        for group in groups:
            for source_kind in self._normalize_string_list(
                group.get("source_kinds")
            ):
                if source_kind not in offered_source_kinds:
                    invalid_source_kinds.append(source_kind)
        if not groups or invalid_source_kinds:
            self.diagnostics.setdefault(
                "taskboard_final_repair_retrieval_plan_errors",
                [],
            ).append(
                {
                    "code": (
                        "taskboard.final_verification.invalid_evidence_retrieval_plan"
                    ),
                    "revision_id": str(
                        getattr(revision, "revision_id", "") or ""
                    ),
                    "query_group_count": len(groups),
                    "invalid_source_kinds": sorted(set(invalid_source_kinds)),
                    "offered_source_kinds": sorted(offered_source_kinds),
                }
            )
            return {}
        plan_digest = self._taskboard_scoped_retrieval_plan_digest(plan)
        if plan_digest in exhausted_plan_digests:
            self.diagnostics.setdefault(
                "taskboard_final_repair_retrieval_plan_errors",
                [],
            ).append(
                {
                    "code": (
                        "taskboard.final_verification.exhausted_evidence_retrieval_plan_replayed"
                    ),
                    "revision_id": str(
                        getattr(revision, "revision_id", "") or ""
                    ),
                    "plan_digest": plan_digest,
                    "exhausted_plan_digests": sorted(exhausted_plan_digests),
                }
            )
            return {}
        analysis = self._taskboard_scoped_retrieval_capacity_analysis(plan)
        diagnostic = {
            "code": "taskboard.final_verification.evidence_retrieval_plan",
            "revision_id": str(getattr(revision, "revision_id", "") or ""),
            "request_id": str(getattr(result_handle, "id", "") or ""),
            "query_group_count": len(groups),
            "capacity_status": analysis.get("status"),
            "reserved_results": analysis.get("reserved_results"),
            "capacity": analysis.get("capacity"),
            "batch_reserved_results": analysis.get("batch_reserved_results"),
            "plan_digest": plan_digest,
        }
        self.diagnostics.setdefault(
            "taskboard_final_repair_retrieval_plans",
            [],
        ).append(diagnostic)
        return DataFormatter.sanitize(plan)

    def _taskboard_final_verification_repair_revision(
        self,
        revision: Any,
        *,
        final: Mapping[str, Any],
        final_verification: Mapping[str, Any],
        evidence_retrieval_plan: Mapping[str, Any] | None = None,
    ) -> TaskBoardRevision | None:
        from agently.core.orchestration.TaskBoard import apply_task_board_patch

        effective_revision = TaskBoardRevision.from_value(revision)
        existing_ids = set(effective_revision.graph.card_by_id())
        completed_dependencies = [
            str(card_id)
            for card_id, result in effective_revision.card_results.items()
            if str(getattr(result, "status", "")).strip().lower() == "completed"
        ]
        if not completed_dependencies:
            return None
        raw_replan_signal = final_verification.get("replan_signal")
        replan_signal = (
            dict(DataFormatter.sanitize(raw_replan_signal))
            if isinstance(raw_replan_signal, Mapping)
            else None
        )
        replan_status = str(
            replan_signal.get("status") if replan_signal is not None else ""
        ).strip()
        if replan_status in {"blocked", "clarify"}:
            # A terminal verifier block is an explicit reachability boundary,
            # not permission to manufacture another repair card.  Replaying
            # the same carrier/contract/evidence frontier only spends model
            # requests without creating a satisfiable state transition.
            self.diagnostics.setdefault(
                "taskboard_final_repair_stopped",
                [],
            ).append(
                {
                    "code": "taskboard.final_verification.repair_stopped",
                    "status": replan_status,
                    "reason": str(
                        replan_signal.get("reason")
                        if replan_signal is not None
                        else final_verification.get("reason")
                        or ""
                    ),
                    "revision_id": effective_revision.revision_id,
                }
            )
            return None
        evidence_reacquisition = bool(
            replan_signal is not None
            and replan_status == "replan_segment"
        )
        relevance_diagnostic: dict[str, Any] | None = None
        if evidence_reacquisition:
            prior_reacquisition = (
                self._taskboard_latest_completed_evidence_reacquisition(
                    effective_revision
                )
            )
            if prior_reacquisition is not None:
                target_subjects = self._normalize_string_list(
                    prior_reacquisition.get("target_subjects")
                )
                acquired_reference_ids = self._normalize_string_list(
                    prior_reacquisition.get("new_reference_ids")
                )
                verifier_check_refs = self._taskboard_verifier_check_evidence_refs(
                    final_verification,
                    target_subjects=target_subjects,
                )
                verified_new_reference_ids = [
                    reference_id
                    for reference_id in acquired_reference_ids
                    if reference_id in verifier_check_refs
                ]
                relevance_diagnostic = {
                    "card_id": str(prior_reacquisition.get("card_id") or ""),
                    "target_subjects": target_subjects,
                    "acquired_reference_ids": acquired_reference_ids,
                    "verifier_check_evidence_refs": verifier_check_refs,
                    "verified_new_reference_ids": verified_new_reference_ids,
                    "status": (
                        "used_by_target_check"
                        if verified_new_reference_ids
                        else "not_used_by_target_check"
                    ),
                    "retry_scheduled": False,
                    "revision_id": effective_revision.revision_id,
                }
                (
                    relevance_issue,
                    relevance_contract,
                    relevance_digest,
                ) = self._taskboard_irrelevant_reacquired_evidence_state(
                    target_subjects=target_subjects,
                )
                if verified_new_reference_ids:
                    self._terminal_convergence_state.mark_resolved(
                        relevance_issue
                    )
                else:
                    relevance_convergence = (
                        self._terminal_convergence_state.record_detection(
                            relevance_issue,
                            relevance_digest,
                            repair_contract=relevance_contract,
                            verifier_called=True,
                        )
                    )
                    relevance_diagnostic["convergence"] = {
                        **dict(DataFormatter.sanitize(relevance_convergence)),
                        "issue": {
                            "gate_kind": relevance_issue.gate_kind,
                            "issue_code": relevance_issue.issue_code,
                            "contract_subject": relevance_issue.contract_subject,
                        },
                        "relevant_state_digest": relevance_digest,
                    }
                self.diagnostics.setdefault(
                    "taskboard_evidence_relevance",
                    [],
                ).append(relevance_diagnostic)
                self.diagnostics["terminal_convergence"] = (
                    self._terminal_convergence_state.snapshot()
                )
                if (
                    not verified_new_reference_ids
                    and relevance_diagnostic["convergence"].get("terminal")
                    is True
                ):
                    return None
        grounding_repair_contract: dict[str, Any] | None = None
        raw_repair_contract = final_verification.get("material_claim_repair_contract")
        if isinstance(raw_repair_contract, Mapping):
            grounding_repair_contract = dict(DataFormatter.sanitize(raw_repair_contract))
        prior_grounding_repair_contract = grounding_repair_contract
        if evidence_reacquisition:
            # The verifier determined that the current evidence segment is
            # insufficient. A claim-only carrier patch would erase unsupported
            # text without resolving the missing source boundary, so keep the
            # claim contract as context and reopen the ordinary Action-capable
            # card path.
            grounding_repair_contract = None
        if grounding_repair_contract is not None:
            raw_requirements = grounding_repair_contract.get("requirements")
            requirements = (
                raw_requirements
                if isinstance(raw_requirements, Sequence)
                and not isinstance(raw_requirements, str | bytes | bytearray)
                else []
            )
            gaps = [
                ": ".join(
                    part
                    for part in (
                        str(requirement.get("subject_key") or "").strip(),
                        str(requirement.get("state") or "").strip(),
                        str(
                            requirement.get("artifact_quote")
                            or requirement.get("claim")
                            or requirement.get("reason")
                            or ""
                        ).strip(),
                    )
                    if part
                )
                for requirement in requirements
                if isinstance(requirement, Mapping)
            ]
            gaps = [gap for gap in gaps if gap]
        else:
            gaps = [
                *self._normalize_string_list(final_verification.get("missing_criteria")),
                *self._normalize_string_list(final_verification.get("next_step_requirements")),
                *self._normalize_string_list(final_verification.get("acceptance_delta")),
            ]
        if not gaps:
            reason = str(final_verification.get("reason") or final.get("reason") or "").strip()
            if reason:
                gaps.append(reason)
        if not gaps:
            return None

        missing_capability_ids = self._normalize_string_list(
            final_verification.get("missing_capability_evidence")
        )
        missing_capability_id_set = set(missing_capability_ids)
        action_requirements = self._merge_capability_evidence_requirements(
            [
                requirement
                for requirement in self._capability_evidence_requirements()
                if requirement.get("required", True) is not False
                and str(requirement.get("kind") or "capability_used")
                == "action_succeeded"
                and str(requirement.get("capability_id") or "").strip()
                in missing_capability_id_set
            ]
        )
        required_action_ids = self._normalize_string_list(
            [
                requirement.get("capability_id")
                for requirement in action_requirements
            ]
        )
        if required_action_ids:
            available_action_ids = {
                str(capability.get("id") or "").strip()
                for capability in self._planner_capabilities()
                if isinstance(capability, Mapping)
                and str(capability.get("kind") or "").strip() == "action"
                and str(capability.get("id") or "").strip()
            }
            unavailable_action_ids = [
                capability_id
                for capability_id in required_action_ids
                if capability_id not in available_action_ids
            ]
            if unavailable_action_ids:
                self.diagnostics.setdefault(
                    "taskboard_final_repair_unavailable_capabilities",
                    [],
                ).append(
                    {
                        "code": "taskboard.final_verification.repair_capability_unavailable",
                        "unavailable_capability_ids": unavailable_action_ids,
                        "revision_id": effective_revision.revision_id,
                    }
                )
                return None

        required_deliverables, _invalid_terminal_paths = self._taskboard_terminal_task_workspace_deliverables(
            effective_revision
        )
        repair_carrier = (
            self._terminal_carrier_for_repair_contract(grounding_repair_contract)
            if grounding_repair_contract is not None
            else None
        )
        repair_carrier_path = ""
        grounding_patch_paths: list[str] = []
        if repair_carrier is not None and repair_carrier.kind == "task_workspace_artifact":
            repair_carrier_path = self._task_workspace_artifact_display_path(
                repair_carrier.path
            )
            if repair_carrier_path:
                grounding_patch_paths.append(repair_carrier_path)

        def safe_id(raw: str) -> str:
            text = "".join(ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in raw.strip())
            text = text.strip(".-")
            return text or "card"

        def unique_id(prefix: str) -> str:
            base = safe_id(prefix)[:80]
            candidate = base
            index = 1
            while candidate in existing_ids:
                index += 1
                candidate = f"{base}-{index}"
            existing_ids.add(candidate)
            return candidate

        evidence_retrieval_batches: list[dict[str, Any]] = []
        if evidence_reacquisition and evidence_retrieval_plan:
            retrieval_capacity = self._taskboard_scoped_retrieval_capacity_analysis(
                evidence_retrieval_plan
            )
            if retrieval_capacity.get("status") == "unpartitionable":
                self.diagnostics.setdefault(
                    "taskboard_final_repair_retrieval_plan_errors",
                    [],
                ).append(
                    {
                        "code": (
                            "taskboard.final_verification.evidence_retrieval_plan_unpartitionable"
                        ),
                        "capacity": retrieval_capacity.get("capacity"),
                        "reserved_results": retrieval_capacity.get(
                            "reserved_results"
                        ),
                        "largest_group": retrieval_capacity.get("largest_group"),
                        "reason": retrieval_capacity.get("reason"),
                        "revision_id": effective_revision.revision_id,
                    }
                )
                return None
            raw_batches = retrieval_capacity.get("batches")
            if isinstance(raw_batches, Sequence) and not isinstance(
                raw_batches,
                str | bytes | bytearray,
            ):
                evidence_retrieval_batches = [
                    dict(DataFormatter.sanitize(batch))
                    for batch in raw_batches
                    if isinstance(batch, Mapping)
                ]
        evidence_reacquisition_ids: list[str] = []
        if evidence_reacquisition:
            batch_count = max(1, len(evidence_retrieval_batches))
            for batch_index in range(batch_count):
                prefix = (
                    "final-verification-evidence"
                    if batch_count == 1
                    else f"final-verification-evidence-{batch_index + 1}"
                )
                evidence_reacquisition_ids.append(unique_id(prefix))
        evidence_reacquisition_id = (
            evidence_reacquisition_ids[0]
            if evidence_reacquisition_ids
            else ""
        )
        repair_id = unique_id("final-verification-repair")
        gap_text = "; ".join(gaps[:6])
        required_outputs = [
            "Corrected final deliverable that resolves final verification gaps using existing evidence.",
        ]
        grounding_scope_instruction = ""
        grounding_patch_mode = bool(
            grounding_repair_contract is not None
            and grounding_patch_paths
            and repair_carrier is not None
            and repair_carrier.kind == "task_workspace_artifact"
            and self._task_workspace_artifact_display_path(repair_carrier.path)
            in grounding_patch_paths
            and not action_requirements
        )
        repair_deliverables = list(required_deliverables)
        if (
            not repair_deliverables
            and repair_carrier_path
            and not self._taskboard_task_workspace_path_is_internal_working(
                repair_carrier_path
            )
        ):
            repair_deliverables.append(repair_carrier_path)
        repair_output_subjects = (
            grounding_patch_paths if grounding_patch_mode else repair_deliverables
        )
        (
            acceptance_evidence_issue,
            acceptance_evidence_contract,
            acceptance_evidence_digest,
            verifier_used_evidence_refs,
        ) = self._taskboard_final_repair_acceptance_evidence_state(
            final_verification,
            output_subjects=repair_output_subjects,
        )
        acceptance_evidence_convergence = self._terminal_convergence_state.record_detection(
            acceptance_evidence_issue,
            acceptance_evidence_digest,
            repair_contract=acceptance_evidence_contract,
            verifier_called=True,
        )
        convergence_diagnostic = {
            **dict(DataFormatter.sanitize(acceptance_evidence_convergence)),
            "issue": {
                "gate_kind": acceptance_evidence_issue.gate_kind,
                "issue_code": acceptance_evidence_issue.issue_code,
                "contract_subject": acceptance_evidence_issue.contract_subject,
            },
            "relevant_state_digest": acceptance_evidence_digest,
            "evidence_refs": verifier_used_evidence_refs,
            "revision_id": effective_revision.revision_id,
        }
        self.diagnostics.setdefault("taskboard_final_repair_convergence", []).append(
            convergence_diagnostic
        )
        self.diagnostics["terminal_convergence"] = self._terminal_convergence_state.snapshot()
        if acceptance_evidence_convergence.get("terminal") is True:
            return None
        if grounding_repair_contract is not None:
            if grounding_patch_mode:
                required_outputs[0] = (
                    "A bounded TaskWorkspace replace patch that changes only the structured grounding claim requirements."
                )
                grounding_scope_instruction = (
                    " Change only the implicated claims named by the structured grounding repair contract and preserve "
                    "all unrelated artifact text and facts exactly. Return a TaskWorkspace replace patch for the authorized "
                    "final deliverable. Do not return or rewrite the complete artifact body; do not introduce a new "
                    "factual clause outside the implicated claims."
                )
            else:
                required_outputs[0] = (
                    "Minimally corrected final deliverable that changes only the structured grounding claim requirements."
                )
                grounding_scope_instruction = (
                    " Change only the implicated claims named by the structured grounding repair contract and preserve "
                    "all unrelated artifact text and facts exactly. When a complete artifact body is required for "
                    "delivery, copy unchanged sections verbatim; do not introduce a new factual clause outside the "
                    "implicated claims."
                )
        if repair_deliverables:
            required_outputs.append(
                "Trusted TaskWorkspace final deliverable path(s): " + ", ".join(repair_deliverables)
            )
        repair_instruction = "Repair"
        if required_action_ids and not evidence_reacquisition:
            repair_instruction = (
                "First produce the listed structured Action evidence with the mounted capabilities "
                f"({', '.join(required_action_ids)}), then repair"
            )
        if evidence_reacquisition:
            repair_completion_instruction = (
                " Use the new verifier-visible evidence produced by dependency card(s) "
                f"{', '.join(evidence_reacquisition_ids)}. "
                "Do not lower, relax, or replace the original success criteria. Produce a complete corrected "
                "deliverable grounded in that dependency evidence."
            )
        else:
            repair_completion_instruction = (
                " Preserve verifier-visible source refs; remove, qualify, or replace unsupported facts instead of "
                "inventing evidence."
                if grounding_patch_mode
                else " Produce a complete corrected deliverable; preserve verifier-visible source refs; remove, "
                "qualify, or replace unsupported facts instead of inventing evidence."
            )
        inline_repair = (
            final_verification.get("decision_source") == "taskboard_finalizer"
            and not repair_deliverables
            and not self._taskboard_final_refs_from_evidence_view(
                build_task_board_evidence_view(effective_revision).to_dict()
            )
        )
        if inline_repair:
            repair_completion_instruction += (
                " Preserve the current inline answer format. Return the complete corrected body in "
                "candidate_final_result; leave artifact_markdown, artifact_manifest, file_refs, and "
                "artifact acceptance_points empty because this repair has no file delivery contract."
            )
        evidence_contract = {
            "kind": "taskboard_final_verification_repair",
            "missing_criteria": self._normalize_string_list(final_verification.get("missing_criteria")),
            "next_step_requirements": self._normalize_string_list(final_verification.get("next_step_requirements")),
            "acceptance_delta": self._normalize_string_list(final_verification.get("acceptance_delta")),
            "reason": str(final_verification.get("reason") or ""),
        }
        if inline_repair:
            evidence_contract["deliverable_mode"] = "inline_final"
        prior_final_evidence_use = []
        for item in collect_evidence_use(final)[:24]:
            if not isinstance(item, Mapping):
                continue
            prior_final_evidence_use.append(
                {
                    "claim": self._truncate_prompt_text(item.get("claim"), 500),
                    "evidence_ids": self._normalize_string_list(item.get("evidence_ids"))[:8],
                    "support_type": str(item.get("support_type") or "content"),
                }
            )
        if prior_final_evidence_use:
            evidence_contract["prior_final_evidence_use"] = DataFormatter.sanitize(prior_final_evidence_use)
        metadata = {
            "generated_by": "agent_task.taskboard.final_verification_repair",
            "repair_source": "final_verification",
            "previous_revision_id": effective_revision.revision_id,
            "final_task_workspace_deliverables": repair_deliverables,
            "terminal_convergence_subject": "taskboard_final_verification",
            "acceptance_evidence_convergence": {
                **dict(DataFormatter.sanitize(acceptance_evidence_convergence)),
                "relevant_state_digest": acceptance_evidence_digest,
                "evidence_refs": verifier_used_evidence_refs,
            },
        }
        if evidence_reacquisition and replan_signal is not None:
            evidence_contract["replan_signal"] = replan_signal
            evidence_contract["evidence_reacquisition_card_id"] = (
                evidence_reacquisition_id
            )
            evidence_contract["evidence_reacquisition_card_ids"] = (
                evidence_reacquisition_ids
            )
            if prior_grounding_repair_contract is not None:
                evidence_contract["prior_material_claim_repair_contract"] = (
                    prior_grounding_repair_contract
                )
                contract_subject = str(
                    prior_grounding_repair_contract.get("contract_subject") or ""
                ).strip()
                if contract_subject:
                    metadata["terminal_convergence_subject"] = contract_subject
            metadata["repair_source"] = "verification_evidence_reacquisition"
        if grounding_repair_contract is not None:
            evidence_contract["material_claim_repair_contract"] = grounding_repair_contract
            if grounding_patch_mode:
                evidence_contract["material_claim_patch_paths"] = grounding_patch_paths
            metadata["repair_source"] = "material_claim_audit"
            contract_subject = str(grounding_repair_contract.get("contract_subject") or "").strip()
            if contract_subject:
                metadata["terminal_convergence_subject"] = contract_subject
        if action_requirements and not evidence_reacquisition:
            evidence_contract["capability_evidence_requirements"] = action_requirements
            evidence_contract["requires_capability_ids"] = required_action_ids
            metadata["requires_capability_ids"] = required_action_ids
        evidence_cards: list[dict[str, Any]] = []
        if evidence_reacquisition:
            eligible_source_roles = [
                "action",
                "source",
                "task_workspace_readback",
            ]
            baseline_snapshot = self._taskboard_grounding_evidence_snapshot(
                eligible_source_roles=eligible_source_roles,
                excluded_artifact_paths=repair_deliverables,
            )
            evidence_done_when = (
                "At least one new eligible body-bearing source content identity, distinct from the replan "
                "baseline and excluding final-deliverable transport, is verifier-visible for the failed criteria."
            )
            evidence_card_contract: dict[str, Any] = {
                "kind": "taskboard_final_verification_evidence_reacquisition",
                "done_when": evidence_done_when,
                "missing_criteria": self._normalize_string_list(
                    final_verification.get("missing_criteria")
                ),
                "next_step_requirements": self._normalize_string_list(
                    final_verification.get("next_step_requirements")
                ),
                "acceptance_delta": self._normalize_string_list(
                    final_verification.get("acceptance_delta")
                ),
                "reason": str(final_verification.get("reason") or ""),
                "replan_signal": replan_signal,
                "baseline_content_identities": self._normalize_string_list(
                    baseline_snapshot.get("content_identities")
                ),
                "minimum_new_content_identity_count": 1,
                "eligible_source_roles": eligible_source_roles,
                "excluded_artifact_paths": repair_deliverables,
                "criterion_subjects": self._normalize_string_list(
                    acceptance_evidence_contract.get("criterion_subjects")
                ),
            }
            evidence_card_metadata: dict[str, Any] = {
                "generated_by": (
                    "agent_task.taskboard.final_verification_evidence_reacquisition"
                ),
                "repair_source": "verification_evidence_reacquisition",
                "previous_revision_id": effective_revision.revision_id,
                "terminal_convergence_subject": metadata[
                    "terminal_convergence_subject"
                ],
                "acceptance_evidence_convergence": metadata[
                    "acceptance_evidence_convergence"
                ],
            }
            if action_requirements:
                evidence_card_contract["capability_evidence_requirements"] = (
                    action_requirements
                )
                evidence_card_contract["requires_capability_ids"] = required_action_ids
                evidence_card_metadata["requires_capability_ids"] = required_action_ids
            batch_count = len(evidence_reacquisition_ids)
            for batch_index, evidence_card_id in enumerate(
                evidence_reacquisition_ids,
                start=1,
            ):
                card_contract = dict(evidence_card_contract)
                card_metadata = dict(evidence_card_metadata)
                scoped_retrieval = (
                    evidence_retrieval_batches[batch_index - 1]
                    if batch_index <= len(evidence_retrieval_batches)
                    else {}
                )
                if scoped_retrieval:
                    scoped_retrieval_plan_digest = (
                        self._taskboard_scoped_retrieval_plan_digest(
                            scoped_retrieval
                        )
                    )
                    card_contract["scoped_retrieval"] = DataFormatter.sanitize(
                        scoped_retrieval
                    )
                    card_contract["scoped_retrieval_plan_digest"] = (
                        scoped_retrieval_plan_digest
                    )
                    card_metadata["scoped_retrieval"] = DataFormatter.sanitize(
                        scoped_retrieval
                    )
                    card_metadata["scoped_retrieval_plan_digest"] = (
                        scoped_retrieval_plan_digest
                    )
                    card_metadata["retrieval_policy"] = scoped_retrieval_policy()
                    card_metadata["retrieval_batch_index"] = batch_index
                    card_metadata["retrieval_batch_count"] = batch_count
                evidence_cards.append(
                    {
                        "id": evidence_card_id,
                        "objective": (
                            "Acquire additional verifier-visible evidence for the final verification gaps: "
                            f"{gap_text}. Do not lower, relax, or replace the original success criteria. Merely "
                            "rewriting or qualifying the deliverable is not evidence acquisition and must not count "
                            "as progress. Use the declared scoped TaskContext retrieval plan or mounted capabilities "
                            "to obtain new bounded evidence. Do not write or rewrite the final deliverable in this card. "
                            "If the required evidence is unavailable, return setback or blocked with the missing source boundary."
                        ),
                        "depends_on": completed_dependencies,
                        "required_outputs": [evidence_done_when],
                        "allowed_execution_shape": (
                            "actions"
                            if action_requirements
                            else ("control" if scoped_retrieval else "auto")
                        ),
                        "failure_policy": "required",
                        "evidence_contract": card_contract,
                        "metadata": card_metadata,
                    }
                )
        repair_card = {
            "id": repair_id,
            "objective": (
                repair_instruction
                + " the final TaskBoard deliverable using existing completed-card evidence and final "
                f"verification feedback. Address these gaps: {gap_text}.{grounding_scope_instruction}"
                + repair_completion_instruction
            ),
            "depends_on": (
                evidence_reacquisition_ids
                if evidence_reacquisition
                else completed_dependencies
            ),
            "required_outputs": required_outputs,
            # Exact action_succeeded gaps remain narrowed deterministically by
            # action_requirements. A file-backed grounding-only repair uses the
            # host-owned bounded patch route; other semantic repairs retain the
            # ordinary bounded route with mounted capabilities available.
            "allowed_execution_shape": (
                "actions"
                if action_requirements
                else "control"
            ),
            "failure_policy": "required",
            "evidence_contract": evidence_contract,
            "metadata": metadata,
        }
        diagnostic = {
            "code": "taskboard.final_verification.repair_patch",
            "repair_card_id": repair_id,
            "evidence_reacquisition_card_id": evidence_reacquisition_id,
            "evidence_reacquisition_card_ids": evidence_reacquisition_ids,
            "depends_on": repair_card["depends_on"],
            "missing_criteria": self._normalize_string_list(final_verification.get("missing_criteria")),
            "reason": str(final_verification.get("reason") or ""),
        }
        operations: list[dict[str, Any]] = []
        for evidence_card in evidence_cards:
            operations.append({"op": "add_card", "card": evidence_card})
        operations.extend(
            [
                {"op": "add_card", "card": repair_card},
                {"op": "append_diagnostic", "diagnostic": diagnostic},
                {"op": "set_board_status", "status": "running"},
            ]
        )
        patch = {
            "base_revision": effective_revision.revision_id,
            "source": "agent_task.taskboard.final_verification_repair",
            "operations": operations,
            "diagnostics": [diagnostic],
        }
        try:
            repaired_revision = apply_task_board_patch(effective_revision, patch)
        except Exception as error:
            self.diagnostics.setdefault("taskboard_final_repair_patch_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "repair_card_id": repair_id,
                    "revision_id": effective_revision.revision_id,
                }
            )
            return None
        self.diagnostics.setdefault("taskboard_final_repair_patches", []).append(
            {
                "repair_card_id": repair_id,
                "evidence_reacquisition_card_id": evidence_reacquisition_id,
                "evidence_reacquisition_card_ids": evidence_reacquisition_ids,
                "previous_revision_id": effective_revision.revision_id,
                "revision_id": repaired_revision.revision_id,
                "missing_criteria": diagnostic["missing_criteria"],
            }
        )
        if relevance_diagnostic is not None:
            relevance_diagnostic["retry_scheduled"] = True
        return repaired_revision


__all__ = ["AgentTaskTaskBoardPatchingMixin"]
