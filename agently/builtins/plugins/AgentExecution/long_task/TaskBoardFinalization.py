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

from pathlib import PurePosixPath

from .TaskShared import (
    AgentTaskMixinBase,
    Any,
    build_task_board_acceptance_index,
    build_task_board_evidence_view,
    build_task_board_focus_payload,
    build_task_board_incremental_verification_plan,
    build_task_board_scoped_evidence_view,
    cast,
    collect_evidence_use,
    DataFormatter,
    Mapping,
    Sequence,
    source_refs_from_ledger,
    task_board_blocking_state_facts,
    task_board_explicit_state_facts,
    TaskBoard,
    TaskBoardRevision,
    TaskContextView,
    validate_evidence_use,
    value_with_normalized_evidence_use,
    _compact_agent_task_error_message,
    _VERIFIER_PROMPT_VALUE_CHARS,
    _WORKSPACE_ARTIFACT_PREVIEW_BYTES,
)


TASK_BOARD_COMPLETION_NOTES_SCHEMA_VERSION = "task_board_completion_notes/v1"


class AgentTaskTaskBoardFinalizationMixin(AgentTaskMixinBase):
    """TaskBoard final synthesis, terminal verification, and final repair routing."""

    def _taskboard_final_evidence_logs(
        self,
        revision: Any,
    ) -> dict[str, Any]:
        effective_revision = TaskBoardRevision.from_value(revision)
        action_logs: list[dict[str, Any]] = []
        skill_context_consumptions: list[dict[str, Any]] = []

        def add_consumptions(values: Any) -> None:
            if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
                return
            existing = {
                (str(item.get("skill_id") or ""), str(item.get("request_id") or ""))
                for item in skill_context_consumptions
            }
            for item in values:
                if not isinstance(item, Mapping):
                    continue
                normalized = dict(DataFormatter.sanitize(item))
                key = (
                    str(normalized.get("skill_id") or ""),
                    str(normalized.get("request_id") or ""),
                )
                if not all(key) or key in existing:
                    continue
                existing.add(key)
                skill_context_consumptions.append(normalized)

        for result in effective_revision.card_results.values():
            if not isinstance(getattr(result, "diagnostics", None), Sequence):
                continue
            for diagnostic in result.diagnostics:
                if not isinstance(diagnostic, Mapping):
                    continue
                summary = diagnostic.get("evidence_summary")
                if not isinstance(summary, Mapping):
                    continue
                raw_actions = summary.get("actions")
                if isinstance(raw_actions, Sequence) and not isinstance(raw_actions, str | bytes | bytearray):
                    action_logs.extend(dict(item) for item in raw_actions if isinstance(item, Mapping))
                context_evidence = summary.get("context_evidence")
                if isinstance(context_evidence, Mapping):
                    skills = context_evidence.get("skills")
                    if isinstance(skills, Mapping):
                        add_consumptions(skills.get("consumptions"))

        consumed_skill_ids = list(
            dict.fromkeys(
                str(item.get("skill_id") or "")
                for item in skill_context_consumptions
                if str(item.get("skill_id") or "")
            )
        )
        return {
            "action_logs": self._dedupe_action_records(action_logs),
            "skill_context_consumptions": skill_context_consumptions,
            "consumed_skill_ids": consumed_skill_ids,
        }

    @classmethod
    def _taskboard_final_refs_from_evidence_view(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
                return
            for item in value:
                if isinstance(item, Mapping):
                    refs.append(dict(DataFormatter.sanitize(item)))

        collect(evidence_view.get("artifact_refs"))
        collect(evidence_view.get("file_refs"))
        cards = evidence_view.get("cards")
        if isinstance(cards, Sequence) and not isinstance(cards, str | bytes | bytearray):
            for card in cards:
                if not isinstance(card, Mapping):
                    continue
                collect(card.get("artifact_refs"))
                collect(card.get("file_refs"))
        return cls._dedupe_ref_records(refs)

    def _prioritize_taskboard_final_refs(self, refs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        required_paths = {str(path or "").strip() for path in self._required_task_workspace_deliverables()}

        def priority(item: tuple[int, Mapping[str, Any]]) -> tuple[int, int]:
            index, ref = item
            path = self._taskboard_task_workspace_path_key(
                ref.get("staged_target_path") or ref.get("path")
            )
            required_path_keys = {
                self._taskboard_task_workspace_path_key(required_path)
                for required_path in required_paths
            }
            if path and path in required_path_keys:
                return (0, index)
            if self._is_trusted_task_workspace_artifact_ref(ref):
                return (1, index)
            return (2, index)

        ordered = sorted(enumerate(refs), key=priority)
        return [dict(DataFormatter.sanitize(ref)) for _, ref in ordered]

    def _taskboard_terminal_candidate_refs(
        self,
        revision: Any,
        refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project terminal refs from the unique leaf deliverable owner.

        Intermediate TaskWorkspace artifacts remain in the TaskBoard evidence view,
        but they must not compete with the leaf delivery card for strict
        grounding merely because their byte size is equal or larger.
        """

        current_refs = [
            dict(DataFormatter.sanitize(ref))
            for ref in refs
            if isinstance(ref, Mapping)
        ]
        required_path_keys = {
            self._taskboard_task_workspace_path_key(path)
            for path in self._required_task_workspace_deliverables()
            if self._taskboard_task_workspace_path_key(path)
        }
        if required_path_keys:
            staged = [
                ref
                for ref in current_refs
                if self._taskboard_task_workspace_path_key(
                    ref.get("staged_target_path")
                )
                in required_path_keys
            ]
            if staged:
                return self._dedupe_ref_records(staged)
            return self._dedupe_ref_records(
                [
                    ref
                    for ref in current_refs
                    if self._taskboard_task_workspace_path_key(ref.get("path")) in required_path_keys
                ]
            )

        sources = self._taskboard_promotable_deliverable_sources(revision)
        if len(sources) != 1:
            return current_refs

        declared_path_keys = {
            self._taskboard_task_workspace_path_key(path)
            for path in self._normalize_string_list(sources[0].get("declared_task_workspace_paths"))
            if self._taskboard_task_workspace_path_key(path)
            and not self._taskboard_task_workspace_path_is_internal_working(path)
        }
        raw_declared_paths = self._normalize_string_list(sources[0].get("declared_task_workspace_paths"))
        if raw_declared_paths:
            return self._dedupe_ref_records(
                [
                    ref
                    for ref in current_refs
                    if self._taskboard_task_workspace_path_key(ref.get("path")) in declared_path_keys
                ]
            )

        raw_source_refs = sources[0].get("file_refs")
        if not isinstance(raw_source_refs, Sequence) or isinstance(
            raw_source_refs,
            str | bytes | bytearray,
        ):
            return []
        source_refs = [
            item
            for item in raw_source_refs
            if isinstance(item, Mapping)
            and not self._taskboard_task_workspace_path_is_internal_working(item.get("path"))
        ]
        if not source_refs:
            return []

        def matches(ref: Mapping[str, Any], source_ref: Mapping[str, Any]) -> bool:
            path = self._taskboard_task_workspace_path_key(ref.get("path"))
            source_path = self._taskboard_task_workspace_path_key(source_ref.get("path"))
            if not path or path != source_path:
                return False
            digest = str(ref.get("sha256") or "").strip()
            source_digest = str(source_ref.get("sha256") or "").strip()
            return not digest or not source_digest or digest == source_digest

        selected = [
            ref
            for ref in current_refs
            if any(matches(ref, source_ref) for source_ref in source_refs)
        ]
        return self._dedupe_ref_records(selected)

    def _taskboard_final_source_refs_from_evidence_view(self, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        stable_ledger = self._stable_evidence_ledger_view(
            evidence_view,
            max_items=120,
            body_chars=0,
            include_body=False,
            budget_selection="content_first",
        )
        ledger_refs = source_refs_from_ledger(stable_ledger, max_refs=32)
        if ledger_refs:
            return ledger_refs
        return self._collect_taskboard_source_refs(evidence_view, max_refs=32)

    @classmethod
    def _taskboard_task_workspace_path_key(cls, path: Any) -> str:
        text = cls._task_workspace_artifact_display_path(path)
        if not text:
            return ""
        return PurePosixPath(text).as_posix()

    @classmethod
    def _taskboard_task_workspace_path_name(cls, path: Any) -> str:
        path_key = cls._taskboard_task_workspace_path_key(path)
        if not path_key:
            return ""
        return PurePosixPath(path_key).name

    @classmethod
    def _taskboard_task_workspace_path_is_internal_working(cls, path: Any) -> bool:
        path_key = cls._taskboard_task_workspace_path_key(path)
        if not path_key:
            return False
        parts = PurePosixPath(path_key).parts
        return bool(parts and parts[0] == "working")

    @classmethod
    def _taskboard_declared_task_workspace_paths_from_preview(cls, preview: Any) -> list[str]:
        if not isinstance(preview, Mapping):
            return []
        manifest = preview.get("artifact_manifest")
        if not isinstance(manifest, Mapping) or not manifest:
            return []
        paths: list[str] = []

        def add_path(value: Any) -> None:
            path = cls._task_workspace_artifact_display_path(value)
            if (
                path
                and cls._task_workspace_artifact_candidate_path_is_local(path)
                and path not in paths
            ):
                paths.append(path)

        for key in ("path", "output_path", "file_path"):
            add_path(manifest.get(key))
        deliverables = manifest.get("deliverables")
        if isinstance(deliverables, Sequence) and not isinstance(
            deliverables,
            str | bytes | bytearray,
        ):
            for item in deliverables:
                if isinstance(item, Mapping):
                    add_path(item.get("path") or item.get("output_path") or item.get("file_path"))
        return paths

    def _taskboard_terminal_task_workspace_deliverables(
        self,
        revision: Any,
    ) -> tuple[list[str], list[str]]:
        required_paths = [
            self._task_workspace_artifact_display_path(path)
            for path in self._required_task_workspace_deliverables()
            if self._task_workspace_artifact_display_path(path)
        ]
        if required_paths:
            return self._normalize_string_list(required_paths), []

        sources = self._taskboard_promotable_deliverable_sources(revision)
        if len(sources) != 1:
            return [], []
        declared_paths = self._normalize_string_list(sources[0].get("declared_task_workspace_paths"))
        valid_paths: list[str] = []
        invalid_internal_paths: list[str] = []
        for path in declared_paths:
            if self._taskboard_task_workspace_path_is_internal_working(path):
                invalid_internal_paths.append(path)
            else:
                valid_paths.append(path)
        return valid_paths, invalid_internal_paths

    async def _missing_taskboard_terminal_task_workspace_deliverables(
        self,
        revision: Any,
    ) -> list[str]:
        required_paths, _invalid_internal_paths = self._taskboard_terminal_task_workspace_deliverables(revision)
        missing: list[str] = []
        for path in required_paths:
            try:
                read_result = await self.task_workspace.read_file(path, max_bytes=1)
                byte_count = int(read_result.get("bytes") or 0)
            except Exception:
                missing.append(path)
                continue
            if byte_count <= 0:
                missing.append(path)
        return missing

    def _taskboard_select_required_final_deliverable_source_ref(
        self,
        *,
        target_path_key: str,
        current_refs: Sequence[Mapping[str, Any]],
        target_refs: Sequence[Mapping[str, Any]],
        missing_required: bool,
        allow_unique_fallback: bool = True,
    ) -> dict[str, Any] | None:
        target_name = self._taskboard_task_workspace_path_name(target_path_key)
        if not target_name:
            return None
        if not target_refs and not missing_required:
            return None
        target_bytes = max((self._coerce_non_negative_int(ref.get("bytes")) for ref in target_refs), default=0)
        target_sha_values = {
            str(ref.get("sha256") or "").strip()
            for ref in target_refs
            if str(ref.get("sha256") or "").strip()
        }
        same_name_candidates: list[tuple[int, str, str, Mapping[str, Any], str]] = []
        fallback_candidates: list[tuple[int, str, str, Mapping[str, Any], str]] = []
        repair_candidates: list[tuple[int, str, str, Mapping[str, Any], str]] = []
        for ref in current_refs:
            if not self._is_trusted_task_workspace_artifact_ref(ref):
                continue
            path_key = self._taskboard_task_workspace_path_key(ref.get("path"))
            if not path_key or path_key == target_path_key:
                continue
            if not self._task_workspace_artifact_candidate_path_is_local(path_key):
                continue
            byte_count = self._coerce_non_negative_int(ref.get("bytes"))
            sha256 = str(ref.get("sha256") or "").strip()
            if byte_count <= 0 or not sha256:
                continue
            source = str(ref.get("source") or "").strip()
            reason = (
                "final_verification_repair_source"
                if source.startswith("agent_task.taskboard.card.final-verification-repair")
                or "taskboard_final_verification_repair" in source
                else ""
            )
            candidate = (byte_count, sha256, path_key, ref, reason)
            fallback_candidates.append(candidate)
            if reason == "final_verification_repair_source":
                repair_candidates.append(candidate)
            if self._taskboard_task_workspace_path_name(path_key) == target_name:
                same_name_candidates.append(candidate)
        same_name_candidates = self._taskboard_unique_final_deliverable_promotion_candidates(same_name_candidates)
        fallback_candidates = self._taskboard_unique_final_deliverable_promotion_candidates(fallback_candidates)
        repair_candidates = self._taskboard_unique_final_deliverable_promotion_candidates(repair_candidates)
        candidates = same_name_candidates
        if not candidates and target_refs and len(repair_candidates) == 1:
            candidates = repair_candidates
            self.diagnostics.setdefault("taskboard_final_deliverable_promotion", []).append(
                DataFormatter.sanitize(
                    {
                        "status": "selected",
                        "reason": "unique_final_verification_repair_source_for_required_deliverable",
                        "target_path": target_path_key,
                        "source_path": repair_candidates[0][2],
                    }
                )
            )
        elif not candidates and target_refs and len(repair_candidates) > 1:
            self.diagnostics.setdefault("taskboard_final_deliverable_promotion", []).append(
                DataFormatter.sanitize(
                    {
                        "status": "skipped",
                        "reason": "final_verification_repair_source_ambiguous",
                        "target_path": target_path_key,
                        "candidate_paths": [item[2] for item in repair_candidates],
                    }
                )
            )
        if (
            not candidates
            and missing_required
            and not target_refs
            and allow_unique_fallback
        ):
            if len(fallback_candidates) == 1:
                candidates = fallback_candidates
                self.diagnostics.setdefault("taskboard_final_deliverable_promotion", []).append(
                    DataFormatter.sanitize(
                        {
                            "status": "selected",
                            "reason": "unique_trusted_source_for_required_deliverable",
                            "target_path": target_path_key,
                            "source_path": fallback_candidates[0][2],
                        }
                    )
                )
            elif len(fallback_candidates) > 1:
                self.diagnostics.setdefault("taskboard_final_deliverable_promotion", []).append(
                    DataFormatter.sanitize(
                        {
                            "status": "skipped",
                            "reason": "unique_required_deliverable_source_ambiguous",
                            "target_path": target_path_key,
                            "candidate_paths": [item[2] for item in fallback_candidates],
                        }
                    )
                )
        if not candidates:
            return None

        candidates.sort(key=lambda item: (-item[0], item[2]))
        best_bytes = candidates[0][0]
        best_candidates = [item for item in candidates if item[0] == best_bytes]
        best_sha_values = {item[1] for item in best_candidates}
        if len(best_sha_values) > 1:
            self.diagnostics.setdefault("taskboard_final_deliverable_promotion", []).append(
                DataFormatter.sanitize(
                    {
                        "status": "skipped",
                        "reason": "same_size_source_ref_ambiguous",
                        "target_path": target_path_key,
                        "candidate_paths": [item[2] for item in best_candidates],
                        "bytes": best_bytes,
                    }
                )
            )
            return None

        best = best_candidates[0]
        if best[1] in target_sha_values:
            return None
        best_reason = best[4]
        if target_refs and target_bytes >= best[0] and best_reason != "final_verification_repair_source":
            return None
        if (
            target_refs
            and not missing_required
            and best_reason != "final_verification_repair_source"
            and target_bytes >= _WORKSPACE_ARTIFACT_PREVIEW_BYTES
            and best[0] - target_bytes < _WORKSPACE_ARTIFACT_PREVIEW_BYTES
        ):
            return None
        return dict(best[3])

    @staticmethod
    def _taskboard_unique_final_deliverable_promotion_candidates(
        candidates: Sequence[tuple[int, str, str, Mapping[str, Any], str]],
    ) -> list[tuple[int, str, str, Mapping[str, Any], str]]:
        unique: dict[tuple[str, str], tuple[int, str, str, Mapping[str, Any], str]] = {}
        for candidate in candidates:
            key = (candidate[2], candidate[1])
            previous = unique.get(key)
            if previous is None or candidate[0] > previous[0]:
                unique[key] = candidate
        return list(unique.values())

    async def _taskboard_stage_required_final_deliverable_refs(
        self,
        refs: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        current_refs = self._dedupe_ref_records(
            [dict(DataFormatter.sanitize(ref)) for ref in refs if isinstance(ref, Mapping)]
        )
        current_refs = await self._taskboard_materialize_promotion_candidate_refs(current_refs)
        required_paths = [
            str(path).strip()
            for path in self._required_task_workspace_deliverables()
            if str(path or "").strip()
        ]
        required_by_key = {
            self._taskboard_task_workspace_path_key(path): path
            for path in required_paths
            if self._taskboard_task_workspace_path_key(path)
        }
        if not required_by_key:
            return self._prioritize_taskboard_final_refs(current_refs), []

        missing_required_keys = {
            self._taskboard_task_workspace_path_key(path)
            for path in await self._missing_required_task_workspace_deliverables()
        }
        staged_refs: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        allow_unique_fallback = len(required_by_key) == 1
        for target_path_key, target_path in required_by_key.items():
            target_refs = [
                ref
                for ref in current_refs
                if self._taskboard_task_workspace_path_key(ref.get("path"))
                == target_path_key
            ]
            source_ref = self._taskboard_select_required_final_deliverable_source_ref(
                target_path_key=target_path_key,
                current_refs=current_refs,
                target_refs=target_refs,
                missing_required=target_path_key in missing_required_keys,
                allow_unique_fallback=allow_unique_fallback,
            )
            if source_ref is None:
                if target_refs and target_path_key not in missing_required_keys:
                    continue
                trusted_ref_count = len(
                    [
                        ref
                        for ref in current_refs
                        if self._is_trusted_task_workspace_artifact_ref(ref)
                        and self._taskboard_task_workspace_path_key(ref.get("path"))
                        and self._taskboard_task_workspace_path_key(ref.get("path"))
                        != target_path_key
                    ]
                )
                self.diagnostics.setdefault(
                    "taskboard_final_deliverable_promotion", []
                ).append(
                    DataFormatter.sanitize(
                        {
                            "status": "skipped",
                            "reason": "required_deliverable_source_ref_unavailable",
                            "required_deliverables": [target_path],
                            "trusted_ref_count": trusted_ref_count,
                        }
                    )
                )
                continue

            source_path = str(source_ref.get("path") or "").strip()
            if self._taskboard_task_workspace_path_key(
                source_path
            ) == self._taskboard_task_workspace_path_key(target_path):
                continue
            try:
                staged_ref, promotion = await self._taskboard_stage_final_deliverable_ref(
                    source_ref,
                    target_path=target_path,
                )
            except Exception as error:
                self.diagnostics.setdefault(
                    "taskboard_final_deliverable_promotion", []
                ).append(
                    DataFormatter.sanitize(
                        {
                            "status": "failed",
                            "source_path": source_path,
                            "target_path": target_path,
                            "error": {
                                "type": error.__class__.__name__,
                                "message": _compact_agent_task_error_message(
                                    error,
                                    fallback=error.__class__.__name__,
                                ),
                            },
                        }
                    )
                )
                continue
            staged_refs.append(staged_ref)
            promotions.append(promotion)
        return (
            self._prioritize_taskboard_final_refs(
                self._dedupe_ref_records([*staged_refs, *current_refs])
            ),
            promotions,
        )

    async def _taskboard_stage_final_deliverable_ref(
        self,
        source_ref: Mapping[str, Any],
        *,
        target_path: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source_path = str(source_ref.get("path") or "").strip()
        source_identity = await self.task_workspace._promote_file_identity(
            source_path,
            role="task_workspace_artifact",
        )
        expected_source_sha256 = str(source_ref.get("sha256") or "").strip()
        source_sha256 = str(source_identity.get("sha256") or "").strip()
        if expected_source_sha256 and expected_source_sha256 != source_sha256:
            raise ValueError(
                "TaskWorkspace staged candidate changed since its source ref was collected."
            )
        source_bytes = int(
            source_identity.get("bytes") or source_identity.get("size") or 0
        )
        source_read = await self.task_workspace.read_file(
            source_path,
            max_bytes=max(source_bytes + 1, _WORKSPACE_ARTIFACT_PREVIEW_BYTES),
        )
        if bool(source_read.get("truncated")):
            raise ValueError(
                "TaskWorkspace staged candidate requires complete verifier readback."
            )
        if str(source_read.get("sha256") or "") != source_sha256:
            raise ValueError(
                "TaskWorkspace staged candidate changed during complete readback."
            )
        source_content = str(source_read.get("content") or "")
        preview_truncated = len(source_content) > _WORKSPACE_ARTIFACT_PREVIEW_BYTES

        staged_ref = {
            **dict(DataFormatter.sanitize(source_ref)),
            **dict(DataFormatter.sanitize(source_identity)),
            "path": str(source_read.get("path") or source_path),
            "bytes": int(source_read.get("bytes") or source_bytes),
            "size": int(source_read.get("bytes") or source_bytes),
            "sha256": source_sha256,
            "media_type": source_read.get("media_type")
            or source_ref.get("media_type"),
            "content_kind": str(
                source_read.get("content_kind")
                or source_ref.get("content_kind")
                or "text"
            ),
            "encoding": source_read.get("encoding") or source_ref.get("encoding"),
            "handler_id": source_read.get("handler_id")
            or source_ref.get("handler_id"),
            "role": "task_workspace_artifact",
            "source": "agent_task.task_workspace_artifact.taskboard_final_deliverable_staging",
            "staged_target_path": target_path,
            "promotion_state": "staged",
            "read_bytes": int(source_read.get("read_bytes") or 0),
            "truncated": preview_truncated,
            "preview": self._truncate_prompt_text(
                source_content,
                _WORKSPACE_ARTIFACT_PREVIEW_BYTES,
            ),
            "preview_truncated": preview_truncated,
            "complete_readback_verified": True,
        }
        promotion = {
            "source_path": self._task_workspace_artifact_display_path(
                staged_ref["path"]
            ),
            "target_path": target_path,
            "source_sha256": source_sha256,
            "source_content_version_id": str(
                staged_ref.get("content_version_id") or ""
            ),
        }
        self.diagnostics.setdefault(
            "taskboard_final_deliverable_promotion", []
        ).append(
            DataFormatter.sanitize(
                {
                    "status": "staged",
                    "source_path": staged_ref["path"],
                    "target_path": target_path,
                    "bytes": staged_ref["bytes"],
                    "sha256": staged_ref["sha256"],
                    "source_content_version_id": staged_ref.get(
                        "content_version_id"
                    ),
                }
            )
        )
        return staged_ref, promotion

    async def _taskboard_promote_staged_deliverables(
        self,
        promotions: Sequence[Mapping[str, Any]],
        terminal_refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Promote verifier-accepted staged bytes and completely read back targets."""

        if not promotions:
            return self._prioritize_taskboard_final_refs(terminal_refs)
        staged_refs = [
            dict(DataFormatter.sanitize(ref))
            for ref in terminal_refs
            if isinstance(ref, Mapping)
        ]
        promoted_refs: list[dict[str, Any]] = []
        consumed_staged_keys: set[tuple[str, str, str]] = set()
        promoted_target_keys: set[str] = set()
        for raw_promotion in promotions:
            source_path = self._task_workspace_artifact_display_path(
                raw_promotion.get("source_path")
            )
            target_path = self._task_workspace_artifact_display_path(
                raw_promotion.get("target_path")
            )
            source_sha256 = str(
                raw_promotion.get("source_sha256") or ""
            ).strip()
            source_content_version_id = str(
                raw_promotion.get("source_content_version_id") or ""
            ).strip()
            if (
                not source_path
                or not target_path
                or not source_sha256
                or not source_content_version_id
            ):
                raise ValueError(
                    "TaskBoard staged promotion requires source/target paths, digest, and content version."
                )
            matching_refs = [
                ref
                for ref in staged_refs
                if self._task_workspace_artifact_display_path(ref.get("path"))
                == source_path
                and self._task_workspace_artifact_display_path(
                    ref.get("staged_target_path")
                )
                == target_path
                and str(ref.get("sha256") or "") == source_sha256
                and str(ref.get("content_version_id") or "")
                == source_content_version_id
            ]
            if len(matching_refs) != 1:
                raise ValueError(
                    "TaskBoard staged promotion does not resolve to one verifier-visible candidate ref."
                )

            promoted_identity = await self.task_workspace.atomic_promote_file(
                source_path,
                target_path,
                expected_sha256=source_sha256,
            )
            target_bytes = int(
                promoted_identity.get("bytes")
                or promoted_identity.get("size")
                or 0
            )
            readback = await self.task_workspace.read_file(
                target_path,
                max_bytes=max(1, target_bytes + 1),
            )
            if bool(readback.get("truncated")):
                raise ValueError(
                    "Promoted TaskWorkspace deliverable did not support complete readback."
                )
            if (
                str(readback.get("sha256") or "") != source_sha256
                or int(readback.get("bytes") or 0) != target_bytes
            ):
                raise ValueError(
                    "Promoted TaskWorkspace deliverable differs from verifier-accepted staged bytes."
                )
            readback_content = str(readback.get("content") or "")
            preview_truncated = (
                len(readback_content) > _WORKSPACE_ARTIFACT_PREVIEW_BYTES
            )
            promoted_ref = {
                **dict(DataFormatter.sanitize(promoted_identity)),
                "path": str(readback.get("path") or target_path),
                "bytes": int(readback.get("bytes") or 0),
                "size": int(readback.get("bytes") or 0),
                "sha256": str(readback.get("sha256") or ""),
                "media_type": readback.get("media_type"),
                "content_kind": str(readback.get("content_kind") or "text"),
                "encoding": readback.get("encoding"),
                "handler_id": readback.get("handler_id"),
                "role": "task_workspace_artifact",
                "source": "agent_task.task_workspace_artifact.taskboard_final_deliverable_promotion",
                "source_path": source_path,
                "source_content_version_id": source_content_version_id,
                "promotion_state": "accepted",
                "read_bytes": int(readback.get("read_bytes") or 0),
                "truncated": preview_truncated,
                "preview": self._truncate_prompt_text(
                    readback_content,
                    _WORKSPACE_ARTIFACT_PREVIEW_BYTES,
                ),
                "preview_truncated": preview_truncated,
                "complete_readback_verified": True,
            }
            promoted_refs.append(DataFormatter.sanitize(promoted_ref))
            consumed_staged_keys.add(
                (source_path, target_path, source_sha256)
            )
            promoted_target_keys.add(
                self._taskboard_task_workspace_path_key(target_path)
            )
            self.diagnostics.setdefault(
                "taskboard_final_deliverable_promotion", []
            ).append(
                DataFormatter.sanitize(
                    {
                        "status": "promoted",
                        "source_path": source_path,
                        "source_content_version_id": source_content_version_id,
                        "target_path": promoted_ref["path"],
                        "target_content_version_id": promoted_ref.get(
                            "content_version_id"
                        ),
                        "bytes": promoted_ref["bytes"],
                        "sha256": promoted_ref["sha256"],
                        "complete_readback": True,
                    }
                )
            )

        retained_refs: list[dict[str, Any]] = []
        for ref in staged_refs:
            ref_path = self._task_workspace_artifact_display_path(ref.get("path"))
            staged_target = self._task_workspace_artifact_display_path(
                ref.get("staged_target_path")
            )
            staged_key = (ref_path, staged_target, str(ref.get("sha256") or ""))
            path_key = self._taskboard_task_workspace_path_key(ref_path)
            if staged_key in consumed_staged_keys or path_key in promoted_target_keys:
                continue
            retained_refs.append(ref)
        return self._prioritize_taskboard_final_refs(
            self._dedupe_ref_records([*promoted_refs, *retained_refs])
        )

    async def _taskboard_refresh_current_required_final_refs(
        self,
        refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Make current physical required deliverables authoritative in the hot terminal view."""

        required_paths = [
            str(path or "").strip()
            for path in self._required_task_workspace_deliverables()
            if str(path or "").strip()
        ]
        required_by_key = {
            self._taskboard_task_workspace_path_key(path): path
            for path in required_paths
            if self._taskboard_task_workspace_path_key(path)
        }
        if not required_by_key:
            return self._prioritize_taskboard_final_refs(refs)

        current_refs: list[dict[str, Any]] = []
        refreshed_path_keys: set[str] = set()
        for path_key, path in required_by_key.items():
            try:
                identity_ref = await self.task_workspace._promote_file_identity(
                    path,
                    role="task_workspace_artifact",
                )
                read_result = await self.task_workspace.read_file(
                    path,
                    max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES,
                )
            except FileNotFoundError:
                continue
            except Exception as error:
                self.diagnostics.setdefault("taskboard_current_final_deliverable", []).append(
                    DataFormatter.sanitize(
                        {
                            "status": "readback_failed",
                            "path": path,
                            "error": {
                                "type": error.__class__.__name__,
                                "message": _compact_agent_task_error_message(
                                    error,
                                    fallback=error.__class__.__name__,
                                ),
                            },
                        }
                    )
                )
                continue
            current_ref = {
                **dict(DataFormatter.sanitize(identity_ref)),
                "path": str(read_result.get("path") or path),
                "bytes": int(read_result.get("bytes") or identity_ref.get("bytes") or 0),
                "size": int(read_result.get("bytes") or identity_ref.get("size") or 0),
                "sha256": str(read_result.get("sha256") or identity_ref.get("sha256") or ""),
                "media_type": read_result.get("media_type") or identity_ref.get("media_type"),
                "content_kind": str(
                    read_result.get("content_kind") or identity_ref.get("content_kind") or "text"
                ),
                "encoding": read_result.get("encoding"),
                "handler_id": read_result.get("handler_id"),
                "role": "task_workspace_artifact",
                "source": "agent_task.task_workspace_artifact.taskboard_current_final_deliverable",
                "read_bytes": int(read_result.get("read_bytes") or 0),
                "truncated": bool(read_result.get("truncated")),
                "preview": str(read_result.get("content") or ""),
            }
            current_refs.append(DataFormatter.sanitize(current_ref))
            refreshed_path_keys.add(path_key)

        if not current_refs:
            return self._prioritize_taskboard_final_refs(refs)
        retained_refs = [
            dict(DataFormatter.sanitize(ref))
            for ref in refs
            if isinstance(ref, Mapping)
            and self._taskboard_task_workspace_path_key(ref.get("path")) not in refreshed_path_keys
        ]
        self.diagnostics.setdefault("taskboard_current_final_deliverable", []).extend(
            {
                "status": "current",
                "path": ref.get("path"),
                "locator_id": ref.get("locator_id"),
                "content_version_id": ref.get("content_version_id"),
                "sha256": ref.get("sha256"),
            }
            for ref in current_refs
        )
        return self._prioritize_taskboard_final_refs(
            self._dedupe_ref_records([*current_refs, *retained_refs])
        )

    async def _taskboard_materialize_promotion_candidate_refs(
        self,
        refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        materialized_refs: list[dict[str, Any]] = []
        for ref in refs:
            current_ref = dict(DataFormatter.sanitize(ref))
            if self._task_workspace_artifact_ref_has_trusted_readback(current_ref):
                materialized_refs.append(current_ref)
                continue

            role = str(current_ref.get("role") or "").strip().lower()
            path = str(current_ref.get("path") or "").strip()
            if not self._is_trusted_task_workspace_artifact_ref(current_ref):
                materialized_refs.append(current_ref)
                continue
            if role not in {"task_workspace_artifact", "artifact"} or not self._task_workspace_artifact_candidate_path_is_local(path):
                materialized_refs.append(current_ref)
                continue

            materialized_ref, _content, failure_item = await self._taskboard_materialize_final_artifact_ref(
                current_ref,
                source="agent_task.task_workspace_artifact.taskboard_final_deliverable_source_readback",
            )
            if failure_item is not None:
                self.diagnostics.setdefault("taskboard_final_deliverable_promotion", []).append(
                    DataFormatter.sanitize(
                        {
                            "status": "candidate_readback_failed",
                            "path": path,
                            "failure": failure_item,
                        }
                    )
                )
            materialized_refs.append(materialized_ref)
        return self._dedupe_ref_records(materialized_refs)

    @staticmethod
    def _taskboard_final_pinned_evidence_ids(final_evidence_guard: Mapping[str, Any]) -> list[str]:
        pinned: list[str] = []
        normalized = final_evidence_guard.get("normalized_evidence_use")
        if not isinstance(normalized, Sequence) or isinstance(normalized, str | bytes | bytearray):
            return pinned
        for entry in normalized:
            if not isinstance(entry, Mapping):
                continue
            evidence_ids = entry.get("evidence_ids")
            if not isinstance(evidence_ids, Sequence) or isinstance(evidence_ids, str | bytes | bytearray):
                continue
            for evidence_id in evidence_ids:
                text = str(evidence_id or "").strip()
                if text and text not in pinned:
                    pinned.append(text)
        return pinned

    @staticmethod
    def _taskboard_final_verification_evidence_items(
        scoped_evidence_view: Mapping[str, Any],
        *,
        pinned_evidence_ids: Sequence[str],
        evidence_view: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any] | None = None,
        max_pinned_items: int = 48,
    ) -> list[dict[str, Any]]:
        """Merge the scoped dirty-acceptance projection with cited evidence.

        The scoped view is a hot projection of dirty acceptance items only; the
        evidence ids the final candidate actually cites (pinned) may live
        elsewhere on the board (for example a source-content action readback).
        Final verification must still see those items or it will judge a
        once-read source as unread/ref_only and request unfixable repairs.
        """
        scoped_items = [
            dict(item)
            for item in (scoped_evidence_view.get("evidence_items") or [])
            if isinstance(item, Mapping)
        ]
        seen_ids = {
            str(item.get(field) or "").strip()
            for item in scoped_items
            for field in ("id", "reference_id")
            if str(item.get(field) or "").strip()
        }
        pinned_wanted = [str(evidence_id or "").strip() for evidence_id in pinned_evidence_ids]
        pinned_wanted = [evidence_id for evidence_id in pinned_wanted if evidence_id and evidence_id not in seen_ids]
        raw_items = evidence_view.get("evidence_items") if isinstance(evidence_view, Mapping) else None
        raw_sequence = (
            raw_items
            if isinstance(raw_items, Sequence)
            and not isinstance(raw_items, str | bytes | bytearray)
            else ()
        )
        items_by_id: dict[str, Mapping[str, Any]] = {}
        raw_items_by_canonical_id: dict[str, Mapping[str, Any]] = {}
        for raw_item in raw_sequence:
            if not isinstance(raw_item, Mapping):
                continue
            canonical_id = str(raw_item.get("id") or "").strip()
            if canonical_id:
                raw_items_by_canonical_id.setdefault(canonical_id, raw_item)
            for field in ("id", "reference_id"):
                raw_id = str(raw_item.get(field) or "").strip()
                if raw_id and raw_id not in items_by_id:
                    items_by_id[raw_id] = raw_item
        ledger_items = evidence_ledger.get("items") if isinstance(evidence_ledger, Mapping) else None
        ledger_sequence = (
            ledger_items
            if isinstance(ledger_items, Sequence)
            and not isinstance(ledger_items, str | bytes | bytearray)
            else ()
        )
        merged_ledger_items: list[dict[str, Any]] = []
        for ledger_item in ledger_sequence:
            if not isinstance(ledger_item, Mapping):
                continue
            canonical_id = str(ledger_item.get("id") or "").strip()
            merged_item = dict(ledger_item)
            raw_item = raw_items_by_canonical_id.get(canonical_id)
            if raw_item is not None:
                merged_item.update(dict(raw_item))
                for identity_field in ("reference_id", "evidence_id"):
                    identity_value = str(ledger_item.get(identity_field) or "").strip()
                    if identity_value:
                        merged_item[identity_field] = identity_value
            merged_ledger_items.append(merged_item)
            for field in ("id", "reference_id"):
                value = str(merged_item.get(field) or "").strip()
                if value:
                    items_by_id[value] = merged_item
        appended = 0
        for evidence_id in pinned_wanted:
            raw_item = items_by_id.get(evidence_id)
            if raw_item is None:
                continue
            scoped_items.append(dict(DataFormatter.sanitize(raw_item)))
            seen_ids.update(
                str(raw_item.get(field) or "").strip()
                for field in ("id", "reference_id")
                if str(raw_item.get(field) or "").strip()
            )
            appended += 1
            if appended >= max_pinned_items:
                break
        if appended < max_pinned_items:
            for merged_item in merged_ledger_items:
                item_ids = {
                    str(merged_item.get(field) or "").strip()
                    for field in ("id", "reference_id")
                    if str(merged_item.get(field) or "").strip()
                }
                if not item_ids or item_ids.intersection(seen_ids):
                    continue
                scoped_items.append(dict(DataFormatter.sanitize(merged_item)))
                seen_ids.update(item_ids)
                appended += 1
                if appended >= max_pinned_items:
                    break
        return scoped_items

    async def _finalize_taskboard(
        self,
        revision: Any,
        *,
        context_pack: "TaskContextView",
        previous_acceptance_index: Mapping[str, Any] | None = None,
        prepared_outputs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        prepared = dict(prepared_outputs or {})
        schedule = prepared.get("schedule")
        if schedule is None:
            schedule = TaskBoard(revision, handler=lambda _context: None).schedule()
        result_status = str(
            prepared.get("result_status")
            or self._taskboard_terminal_status(revision, schedule)
        )
        evidence_view = (
            dict(prepared["evidence_view"])
            if isinstance(prepared.get("evidence_view"), Mapping)
            else build_task_board_evidence_view(revision).to_dict()
        )
        evidence_ledger = (
            dict(prepared["evidence_ledger"])
            if isinstance(prepared.get("evidence_ledger"), Mapping)
            else self._stable_evidence_ledger_view(
                evidence_view,
                max_items=120,
                body_chars=2400,
                budget_selection="content_first",
            )
        )
        explicit_state_facts = (
            list(prepared["explicit_state_facts"])
            if isinstance(prepared.get("explicit_state_facts"), Sequence)
            and not isinstance(prepared.get("explicit_state_facts"), str | bytes | bytearray)
            else task_board_explicit_state_facts(revision, evidence_view=evidence_view)
        )
        blocking_state_facts = task_board_blocking_state_facts(explicit_state_facts)
        candidate_final_result = str(
            prepared.get("candidate_final_result")
            or self._taskboard_candidate_final_result(revision)
        )
        staged_promotions = (
            [
                dict(DataFormatter.sanitize(item))
                for item in prepared["staged_promotions"]
                if isinstance(item, Mapping)
            ]
            if isinstance(prepared.get("staged_promotions"), Sequence)
            and not isinstance(
                prepared.get("staged_promotions"), str | bytes | bytearray
            )
            else []
        )
        if isinstance(prepared.get("final_refs"), Sequence) and not isinstance(
            prepared.get("final_refs"),
            str | bytes | bytearray,
        ):
            final_refs = list(prepared["final_refs"])
        else:
            final_refs = self._prioritize_taskboard_final_refs(
                self._taskboard_final_refs_from_evidence_view(evidence_view)
            )
            final_refs, staged_promotions = (
                await self._taskboard_stage_required_final_deliverable_refs(final_refs)
            )
            final_refs = await self._taskboard_refresh_current_required_final_refs(final_refs)
            final_refs = self._taskboard_terminal_candidate_refs(revision, final_refs)
        trusted_terminal_refs = (
            list(prepared["trusted_terminal_refs"])
            if isinstance(prepared.get("trusted_terminal_refs"), Sequence)
            and not isinstance(prepared.get("trusted_terminal_refs"), str | bytes | bytearray)
            else self._trusted_terminal_refs(final_refs)
        )
        trusted_final_refs = self._trusted_terminal_file_refs(trusted_terminal_refs)
        effective_candidate_final_result = "" if trusted_final_refs else candidate_final_result
        can_attempt_degraded_final = self._taskboard_can_attempt_degraded_final(revision, schedule)
        if result_status != "completed" and not can_attempt_degraded_final:
            promoted_partial_refs = await self._register_terminal_deliverables(trusted_terminal_refs)
            self.status = "blocked" if result_status == "blocked" else "error"
            reason = "TaskBoard did not reach a completed board state."
            final_response = self._taskboard_user_final_response(
                final={},
                accepted=False,
                artifact_status="partial",
                reason=reason,
                missing_criteria=["TaskBoard did not reach a completed board state."],
                final_refs=trusted_final_refs,
                board_status=result_status,
                degraded_finalization_attempted=False,
            )
            self.result = {
                "status": self.status,
                "accepted": False,
                "artifact_status": "partial",
                "task_id": self.id,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "reason": reason,
                "final_response": final_response,
                "final_result": self._compact_terminal_final_result(
                    candidate_final_result,
                    trusted_file_refs=trusted_final_refs,
                ),
                "artifact_refs": promoted_partial_refs,
                "missing_criteria": ["TaskBoard did not reach a completed board state."],
            }
            self._terminal_taskboard_state = {
                "revision": revision.to_dict(),
                "schedule": schedule.to_dict(),
                "evidence_view": evidence_view,
                "terminal_status": result_status,
            }
            await self._emit("agent_task.blocked", self.result)
            return {"terminal": True, "status": self.status}

        final_artifact_evidence_items = await self._taskboard_final_artifact_verification_evidence_items(
            trusted_final_refs,
            final={},
        )
        if final_artifact_evidence_items:
            evidence_view = self._taskboard_evidence_view_with_additional_items(
                evidence_view,
                final_artifact_evidence_items,
            )
            evidence_ledger = self._stable_evidence_ledger_view(
                evidence_view, max_items=120, body_chars=2400, budget_selection="content_first"
            )

        prepared_final_candidate = prepared.get("final_candidate")
        reusing_prepared_final = isinstance(prepared_final_candidate, Mapping)
        finalization_source = "prepared_final_retry"
        final = (
            dict(DataFormatter.sanitize(prepared_final_candidate))
            if isinstance(prepared_final_candidate, Mapping)
            else None
        )
        if final is None:
            finalization_source = "model_finalizer"
            final = self._promote_taskboard_final_candidate(
                revision,
                candidate_final_result=effective_candidate_final_result,
                final_refs=final_refs,
                board_status=result_status,
            )
        if final is not None and not reusing_prepared_final:
            promotion_guard = validate_evidence_use(collect_evidence_use(final), evidence_ledger)
            if promotion_guard.get("valid") is True:
                final = value_with_normalized_evidence_use(final, promotion_guard.get("normalized_evidence_use"))
                finalization_source = "candidate_promotion"
                await self._record_phase(
                    "taskboard_final_candidate_promoted",
                    diagnostics={
                        "reason": final.get("reason", ""),
                        "file_ref_count": len(final_refs),
                        "checked_claims": promotion_guard.get("checked_claims"),
                    },
                )
            else:
                self.diagnostics.setdefault("taskboard_final_candidate_promotion", []).append(
                    DataFormatter.sanitize({"accepted": False, "guard": promotion_guard})
                )
                final = None
        if final is None:
            final = await self._request_taskboard_final(
                revision,
                evidence_view,
                candidate_final_result=effective_candidate_final_result,
                board_status=result_status,
                schedule=schedule,
                allow_degraded_final=result_status != "completed",
            )
            final = self._normalize_taskboard_final_result(
                final,
                effective_candidate_final_result,
                fallback_final_result=self._task_workspace_artifact_final_result_from_refs(trusted_final_refs),
            )
        final_artifact_evidence_items = self._dedupe_taskboard_final_evidence_items(
            [
                *final_artifact_evidence_items,
                *await self._taskboard_final_artifact_verification_evidence_items(
                    trusted_final_refs,
                    final=final,
                ),
            ]
        )
        if final_artifact_evidence_items:
            evidence_view = self._taskboard_evidence_view_with_additional_items(
                evidence_view,
                final_artifact_evidence_items,
            )
            evidence_ledger = self._stable_evidence_ledger_view(
                evidence_view, max_items=120, body_chars=2400, budget_selection="content_first"
            )
        revision_metadata = getattr(revision, "metadata", {})
        previous_acceptance_index = previous_acceptance_index or (
            revision_metadata.get("taskboard_acceptance_index")
            if isinstance(revision_metadata, Mapping)
            and isinstance(revision_metadata.get("taskboard_acceptance_index"), Mapping)
            else None
        )
        acceptance_index = build_task_board_acceptance_index(
            revision,
            success_criteria=self.success_criteria,
            evidence_view=evidence_view,
            evidence_ledger=evidence_ledger,
            explicit_state_facts=explicit_state_facts,
            previous_acceptance_index=previous_acceptance_index,
        )
        acceptance_verification_plan = build_task_board_incremental_verification_plan(acceptance_index)
        scoped_evidence_view = build_task_board_scoped_evidence_view(
            acceptance_index,
            evidence_view=evidence_view,
            evidence_ledger=evidence_ledger,
        )
        final_evidence_guard = validate_evidence_use(collect_evidence_use(final), evidence_ledger)
        final = value_with_normalized_evidence_use(final, final_evidence_guard.get("normalized_evidence_use"))
        if final_evidence_guard.get("blocking_count"):
            final, final_evidence_guard = await self._repair_taskboard_final_evidence_use(
                final,
                final_evidence_guard,
                evidence_ledger,
                language_policy=self._language_policy(),
            )
        pinned_evidence_ids = self._taskboard_final_pinned_evidence_ids(final_evidence_guard)
        accepted = self._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        final_verification: dict[str, Any] | None = None
        terminal_transition: dict[str, Any] | None = None
        missing_deliverables = await self._missing_taskboard_terminal_task_workspace_deliverables(revision)
        staged_target_keys = {
            self._taskboard_task_workspace_path_key(item.get("target_path"))
            for item in staged_promotions
            if self._taskboard_task_workspace_path_key(item.get("target_path"))
        }
        missing_deliverables = [
            path
            for path in missing_deliverables
            if self._taskboard_task_workspace_path_key(path)
            not in staged_target_keys
        ]
        _terminal_deliverables, invalid_internal_terminal_paths = (
            self._taskboard_terminal_task_workspace_deliverables(revision)
        )
        required_skill_ids, required_skill_pack_ids = self._required_skill_context_selectors()
        # Skill/SkillPack requirements are authored context contracts. They
        # remain Host-owned hard gates, while ordinary semantic completion stays
        # with the TaskBoard loop.
        required_context_contract = bool(required_skill_ids or required_skill_pack_ids)
        # The TaskBoard loop owns ordinary semantic completion. A second
        # semantic verdict is only justified by an explicit Host-owned hard
        # contract or a deterministic integrity/lifecycle block.
        explicit_delivery_contract = bool(self._required_task_workspace_deliverables())
        explicit_capability_contract = bool(self._capability_evidence_requirements())
        terminal_hard_gate = bool(
            explicit_delivery_contract
            or explicit_capability_contract
            or required_context_contract
            or bool(missing_deliverables)
            or bool(_terminal_deliverables)
            or staged_promotions
            or invalid_internal_terminal_paths
            or blocking_state_facts
        )
        should_verify_final = terminal_hard_gate
        if should_verify_final:
            taskboard_evidence_logs = self._taskboard_final_evidence_logs(revision)
            verifier_final_result = str(final.get("final_result") or "").strip()
            if not verifier_final_result and trusted_final_refs:
                verifier_final_result = self._task_workspace_artifact_final_result_from_refs(trusted_final_refs)
            if not verifier_final_result:
                verifier_final_result = str(effective_candidate_final_result or "").strip()
            verification_options = dict(DataFormatter.sanitize(self.options))
            final_source_refs = self._taskboard_final_source_refs_from_evidence_view(evidence_view)
            final_execution_result = {
                "status": "completed",
                "accepted": accepted,
                "final_result": verifier_final_result,
                "reason": final.get("reason", ""),
                "missing_criteria": final.get("missing_criteria", []),
                "file_refs": final_refs,
                "artifact_refs": final_refs,
                "staged_promotions": DataFormatter.sanitize(staged_promotions),
                "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_stream(evidence_view),
                "taskboard_scoped_evidence_view": DataFormatter.sanitize(scoped_evidence_view),
                "evidence_ledger": evidence_ledger,
            }
            final_execution_meta = {
                "status": "completed",
                "route": {
                    "selected_route": "agent_task",
                    "execution_strategy": self.execution_strategy,
                    "effective_execution_strategy": self.effective_execution_strategy,
                },
                "options": DataFormatter.sanitize(verification_options),
                "effective_options": DataFormatter.sanitize(verification_options),
                "logs": {
                    "artifact_refs": final_refs,
                    "source_refs": final_source_refs,
                    **DataFormatter.sanitize(taskboard_evidence_logs),
                },
                "task_workspace_refs": {"agent_task_artifacts": final_refs},
                "blocks": {
                    "evidence": {
                        # Readback-state reuse: the dirty-acceptance projection
                        # alone drops the source evidence the final artifact
                        # cites (an already-read PDF preview then re-judges as
                        # unread in final verification), so the cited/pinned
                        # ledger items travel with the final candidate.
                        "evidence_items": self._taskboard_final_verification_evidence_items(
                            scoped_evidence_view,
                            pinned_evidence_ids=pinned_evidence_ids,
                            evidence_view=evidence_view,
                            evidence_ledger=evidence_ledger,
                        ),
                        "pinned_evidence_ids": pinned_evidence_ids,
                        "diagnostics": [],
                    }
                },
                "diagnostics": {
                    "taskboard_terminal_status": result_status,
                    "taskboard_acceptance_index": DataFormatter.sanitize(acceptance_index),
                    "taskboard_acceptance_verification_plan": DataFormatter.sanitize(acceptance_verification_plan),
                    "taskboard_explicit_state_facts": explicit_state_facts,
                    "taskboard_blocking_state_facts": blocking_state_facts,
                    "taskboard_evidence_logs": DataFormatter.sanitize(taskboard_evidence_logs),
                    "taskboard_staged_promotions": DataFormatter.sanitize(
                        staged_promotions
                    ),
                },
            }
            # The acceptance index is a projection/cache for dirty-item
            # selection. It is not a terminal semantic authority: the current
            # carrier content may have changed since the cached criterion
            # verdict. Always verify the current terminal candidate once.
            terminal_guard_issues: list[dict[str, Any]] = []
            for terminal_ref in trusted_final_refs:
                terminal_path = self._task_workspace_artifact_display_path(
                    terminal_ref.get("path")
                )
                if not terminal_path:
                    continue
                reference_validation = (
                    await self._validate_terminal_artifact_reference_tokens(
                        terminal_path,
                        content_kind=str(
                            terminal_ref.get("content_kind") or "unknown"
                        ),
                    )
                )
                if reference_validation.get("valid"):
                    continue
                reference_reason = str(
                    reference_validation.get("reason")
                    or "Terminal artifact reference-token validation failed."
                )
                terminal_guard_issues.append(
                    {
                        "code": "taskboard_terminal_reference_token_invalid",
                        "reason": reference_reason,
                        "requires_block": False,
                        "missing_criteria": [
                            "Repair malformed, unknown, or ineligible source references in the staged terminal carrier."
                        ],
                    }
                )
            if invalid_internal_terminal_paths:
                invalid_message = (
                    "A model-declared framework-internal working path cannot satisfy terminal delivery without an "
                    "explicit structured required-deliverable contract: "
                    + ", ".join(invalid_internal_terminal_paths)
                )
                terminal_guard_issues.append(
                    {
                        "code": "taskboard_terminal_task_workspace_delivery_invalid",
                        "reason": invalid_message,
                        "requires_block": True,
                    }
                )
            if blocking_state_facts:
                blocking_reason = "; ".join(
                    str(
                        fact.get("reason")
                        or fact.get("code")
                        or fact.get("status")
                        or "explicit state fact"
                    )
                    for fact in blocking_state_facts
                )
                terminal_guard_issues.append(
                    {
                        "code": "taskboard_explicit_state_fact_block",
                        "reason": blocking_reason
                        or "TaskBoard final gate blocked on explicit state facts.",
                        "requires_block": True,
                        "missing_criteria": [
                            "Resolve explicit task-scoped state facts before accepting the final result."
                        ],
                    }
                )
            promotion_meta = final.get("taskboard_final_promotion")
            preserve_explicit_final_result = (
                isinstance(promotion_meta, Mapping)
                and promotion_meta.get("final_result_source")
                == "explicit_final_result"
            )
            try:
                terminal_transition = await self._run_terminal_verification(
                    max(len(self.iterations) + 1, 1),
                    plan={
                        "execution_shape": "taskboard",
                        "effective_execution_shape": "taskboard",
                        "deliverable_mode": "task_workspace_artifact",
                        "expected_evidence": "Current TaskBoard terminal carriers and trusted TaskWorkspace refs",
                    },
                    execution_result=final_execution_result,
                    execution_meta=final_execution_meta,
                    context_pack=context_pack,
                    missing_deliverables=missing_deliverables,
                    terminal_guard_issues=terminal_guard_issues,
                    preferred_final_result=final.get("final_result") or None,
                    terminal_refs=trusted_terminal_refs,
                    preserve_final_result=preserve_explicit_final_result,
                )
                assert terminal_transition is not None
                final_verification = cast(
                    dict[str, Any],
                    terminal_transition["verification"],
                )
            except Exception as error:
                message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                final_verification = {
                    "is_complete": False,
                    "requires_block": True,
                    "reason": "TaskBoard final verification failed structurally.",
                    "failure_analysis": message,
                    "acceptance_delta": ["TaskBoard final verification must return structured completion status."],
                    "missing_criteria": ["TaskBoard final verification did not produce a valid structured result."],
                    "replan_instruction": "Run a continuation step that produces verifier-readable final evidence.",
                    "repair_constraints": ["Preserve trusted TaskWorkspace refs and final deliverable evidence."],
                    "next_step_requirements": ["Return structured verification fields."],
                    "final_result_required": True,
                    "final_result": "",
                    "guard_reasons": ["taskboard_final_verification_error"],
                    "error": {"type": error.__class__.__name__, "message": message},
                }
                terminal_transition = {
                    "transition": "blocked",
                    "verification": final_verification,
                    "issue": {
                        "gate_kind": "structure",
                        "issue_code": "taskboard_final_verification_error",
                        "contract_subject": f"taskboard:{revision.revision_id}:final",
                    },
                    "repair_contract": {},
                    "accepted_carrier_ids": [],
                    "rejected_carrier_ids": [],
                    "terminal_result": {},
                }
            if (
                final_verification is not None
                and terminal_transition is not None
                and terminal_transition.get("transition") == "accepted"
            ):
                accepted = True
                final = dict(final)
                final["accepted"] = True
                verification_final_result = final_verification.get("final_result")
                if not str(final.get("final_result") or "").strip():
                    if verification_final_result not in (None, "", [], {}):
                        final["final_result"] = str(verification_final_result).strip()
                    elif verifier_final_result:
                        final["final_result"] = verifier_final_result
                verifier_reason = str(final_verification.get("reason") or "").strip()
                final["reason"] = verifier_reason or "TaskBoard final verification accepted."
                final["missing_criteria"] = []
                if staged_promotions:
                    try:
                        self._lifecycle_state.advance(
                            "promoting",
                            expected_version=self._lifecycle_state.state_version,
                        )
                        promoted_terminal_refs = (
                            await self._taskboard_promote_staged_deliverables(
                                staged_promotions,
                                trusted_terminal_refs,
                            )
                        )
                        promoted_final_refs = self._trusted_terminal_file_refs(
                            promoted_terminal_refs
                        )
                        promoted_inventory = await self._replace_terminal_carriers(
                            execution_result={
                                "file_refs": promoted_final_refs,
                                "artifact_refs": promoted_final_refs,
                                "final_result": final.get("final_result", ""),
                            },
                            execution_evidence_summary={},
                            source_work_result_id=(
                                f"taskboard-terminal-promotion:{revision.revision_id}"
                            ),
                        )
                        promoted_carrier_ids = [
                            carrier.carrier_id
                            for carrier in promoted_inventory.carriers
                        ]
                        self._lifecycle_state.advance(
                            "root_verifying",
                            expected_version=self._lifecycle_state.state_version,
                        )
                        decided_inventory = (
                            self._lifecycle_state.record_terminal_transition(
                                "accepted",
                                expected_version=self._lifecycle_state.state_version,
                                accepted_carrier_ids=promoted_carrier_ids,
                            )
                        )
                    except Exception as error:
                        message = _compact_agent_task_error_message(
                            error,
                            fallback=error.__class__.__name__,
                        )
                        accepted = False
                        final["accepted"] = False
                        final["reason"] = (
                            "TaskBoard terminal artifact promotion or complete readback failed: "
                            + message
                        )
                        final["missing_criteria"] = [
                            "Promote the verifier-accepted staged bytes to every required deliverable and read them back completely."
                        ]
                        final_verification = {
                            **dict(final_verification),
                            "is_complete": False,
                            "requires_block": True,
                            "reason": final["reason"],
                            "missing_criteria": list(final["missing_criteria"]),
                            "guard_reasons": self._merge_string_lists(
                                final_verification.get("guard_reasons"),
                                ["taskboard_terminal_artifact_promotion_failed"],
                            ),
                            "error": {
                                "type": error.__class__.__name__,
                                "message": message,
                            },
                        }
                        failed_inventory = (
                            self._lifecycle_state.record_terminal_transition(
                                "blocked",
                                expected_version=self._lifecycle_state.state_version,
                                issue={
                                    "gate_kind": "artifact_promotion",
                                    "issue_code": "taskboard_terminal_artifact_promotion_failed",
                                    "contract_subject": (
                                        f"taskboard:{revision.revision_id}:final"
                                    ),
                                },
                            )
                        )
                        terminal_transition = {
                            **dict(terminal_transition),
                            "transition": "blocked",
                            "verification": DataFormatter.sanitize(
                                final_verification
                            ),
                            "accepted_carrier_ids": [],
                            "state_version": failed_inventory.state_version,
                            "carrier_inventory_version": (
                                failed_inventory.inventory_version
                            ),
                        }
                    else:
                        trusted_terminal_refs = promoted_terminal_refs
                        trusted_final_refs = promoted_final_refs
                        prior_terminal_result = terminal_transition.get(
                            "terminal_result"
                        )
                        terminal_result = (
                            dict(prior_terminal_result)
                            if isinstance(prior_terminal_result, Mapping)
                            else {}
                        )
                        terminal_result.update(
                            {
                                "carrier_ids": promoted_carrier_ids,
                                "required_carrier_ids": [
                                    carrier.carrier_id
                                    for carrier in decided_inventory.carriers
                                    if carrier.required
                                ],
                                "task_workspace_paths": [
                                    carrier.path
                                    for carrier in decided_inventory.carriers
                                    if carrier.kind
                                    == "task_workspace_artifact"
                                ],
                                "terminal_refs": DataFormatter.sanitize(
                                    promoted_terminal_refs
                                ),
                                "final_file_refs": DataFormatter.sanitize(
                                    promoted_final_refs
                                ),
                                "final_result": self._compact_terminal_final_result(
                                    final.get("final_result", ""),
                                    trusted_file_refs=promoted_final_refs,
                                    preserve_value=preserve_explicit_final_result,
                                ),
                            }
                        )
                        terminal_transition = {
                            **dict(terminal_transition),
                            "accepted_carrier_ids": promoted_carrier_ids,
                            "terminal_result": terminal_result,
                            "state_version": decided_inventory.state_version,
                            "carrier_inventory_version": decided_inventory.inventory_version,
                        }
            elif final_verification is not None:
                if (
                    terminal_transition is not None
                    and terminal_transition.get("transition")
                    == "verification_retry"
                ):
                    return {
                        "terminal": False,
                        "status": "verification_retry",
                        "prepared_final": DataFormatter.sanitize(final),
                        "final_verification": DataFormatter.sanitize(
                            final_verification
                        ),
                        "terminal_transition": DataFormatter.sanitize(
                            terminal_transition
                        ),
                    }
                repair_revision = None
                if self._taskboard_final_verification_allows_repair(
                    final_verification,
                    blocking_state_facts=blocking_state_facts,
                ):
                    raw_replan_signal = final_verification.get("replan_signal")
                    replan_signal = (
                        raw_replan_signal
                        if isinstance(raw_replan_signal, Mapping)
                        else {}
                    )
                    evidence_replan = (
                        str(replan_signal.get("status") or "").strip()
                        == "replan_segment"
                    )
                    missing_capability_ids = self._normalize_string_list(
                        final_verification.get("missing_capability_evidence")
                    )
                    evidence_retrieval_plan: dict[str, Any] | None = None
                    evidence_plan_ready = True
                    if evidence_replan and not missing_capability_ids:
                        evidence_retrieval_plan = (
                            await self._request_taskboard_final_evidence_retrieval_plan(
                                revision=revision,
                                final_verification=final_verification,
                            )
                        )
                        evidence_plan_ready = bool(evidence_retrieval_plan)
                    if evidence_plan_ready:
                        repair_revision = self._taskboard_final_verification_repair_revision(
                            revision,
                            final=final,
                            final_verification=final_verification,
                            evidence_retrieval_plan=evidence_retrieval_plan,
                        )
                if repair_revision is not None:
                    await self._record_phase(
                        "taskboard_final_repair_requested",
                        diagnostics={
                            "revision_id": repair_revision.revision_id,
                            "previous_revision_id": revision.revision_id,
                            "reason": final_verification.get("reason"),
                            "missing_criteria": final_verification.get("missing_criteria", []),
                        },
                    )
                    await self._emit(
                        "agent_task.taskboard.final_verification.repair_requested",
                        {
                            "revision_id": repair_revision.revision_id,
                            "previous_revision_id": revision.revision_id,
                            "missing_criteria": final_verification.get("missing_criteria", []),
                        },
                    )
                    return {
                        "terminal": False,
                        "status": "repair_requested",
                        "revision": repair_revision.to_dict(),
                        "final_verification": DataFormatter.sanitize(final_verification),
                    }
                accepted = False
                final = dict(final)
                final["accepted"] = False
                final["reason"] = final_verification.get("reason") or "TaskBoard final verification failed."
                final["missing_criteria"] = final_verification.get("missing_criteria", [])
                final["final_result"] = final_verification.get("final_result") or final.get("final_result", "")
            if final_verification is not None:
                verification_cost_telemetry = {
                    "model_requests": 1,
                    "semantic_verifier_model_requests": 1,
                }
                acceptance_index = build_task_board_acceptance_index(
                    revision,
                    success_criteria=self.success_criteria,
                    verification=final_verification,
                    evidence_view=evidence_view,
                    evidence_ledger=evidence_ledger,
                    explicit_state_facts=explicit_state_facts,
                    previous_acceptance_index=previous_acceptance_index,
                    cost_telemetry=verification_cost_telemetry,
                )
                acceptance_verification_plan = build_task_board_incremental_verification_plan(acceptance_index)
                scoped_evidence_view = build_task_board_scoped_evidence_view(
                    acceptance_index,
                    evidence_view=evidence_view,
                    evidence_ledger=evidence_ledger,
                )
                self._latest_taskboard_acceptance_index = DataFormatter.sanitize(acceptance_index)
        degraded_finalization_attempted = result_status != "completed"
        completion_notes = self._taskboard_completion_notes(
            revision,
            final=final,
            final_verification=final_verification,
            acceptance_verification_plan=acceptance_verification_plan,
        )
        degraded = self._taskboard_final_is_degraded(
            final,
            board_status=result_status,
            degraded_finalization_attempted=degraded_finalization_attempted,
            completion_notes=completion_notes,
        )
        artifact_status = self._taskboard_final_artifact_status(
            accepted=accepted,
            degraded=degraded,
            final=final,
        )
        terminal_result_projection = (
            terminal_transition.get("terminal_result")
            if isinstance(terminal_transition, Mapping)
            and isinstance(terminal_transition.get("terminal_result"), Mapping)
            else None
        )
        if isinstance(terminal_result_projection, Mapping):
            trusted_terminal_refs = list(
                terminal_result_projection.get("terminal_refs") or []
            )
            trusted_final_refs = list(
                terminal_result_projection.get("final_file_refs") or []
            )
        final_response = self._taskboard_user_final_response(
            final=final,
            accepted=accepted,
            artifact_status=artifact_status,
            reason=str(final.get("reason") or ""),
            missing_criteria=final.get("missing_criteria", []),
            final_refs=trusted_final_refs,
            board_status=result_status,
            degraded_finalization_attempted=degraded_finalization_attempted,
            completion_notes=completion_notes,
        )
        self.status = "completed" if accepted else "blocked"
        promoted_refs = await self._register_terminal_deliverables(trusted_terminal_refs)
        if isinstance(terminal_result_projection, Mapping):
            compact_final_result = terminal_result_projection.get("final_result", "")
        else:
            promotion_meta = final.get("taskboard_final_promotion")
            preserve_explicit_final_result = (
                isinstance(promotion_meta, Mapping)
                and promotion_meta.get("final_result_source")
                == "explicit_final_result"
            )
            compact_final_result = self._compact_terminal_final_result(
                final.get("final_result", ""),
                trusted_file_refs=(
                    () if preserve_explicit_final_result else trusted_final_refs
                ),
            )
        self._terminal_taskboard_state = {
            "revision": revision.to_dict(),
            "schedule": schedule.to_dict(),
            "evidence_view": evidence_view,
            "taskboard_acceptance_index": DataFormatter.sanitize(acceptance_index),
            "acceptance_verification_plan": DataFormatter.sanitize(acceptance_verification_plan),
            "taskboard_scoped_evidence_view": DataFormatter.sanitize(scoped_evidence_view),
            "completion_notes": completion_notes,
            "explicit_state_facts": explicit_state_facts,
            "blocking_state_facts": blocking_state_facts,
            "terminal_status": result_status,
            "degraded_finalization_attempted": degraded_finalization_attempted,
            "finalization_source": finalization_source,
            "final_verification": final_verification,
        }
        self.result = {
            "status": self.status,
            "accepted": accepted,
            "artifact_status": artifact_status,
            "task_id": self.id,
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "final_result": compact_final_result,
            "reason": final.get("reason", ""),
            "final_response": final_response,
            "missing_criteria": final.get("missing_criteria", []),
            "artifact_refs": promoted_refs,
        }
        await self._record_phase(
            "terminal",
            diagnostics={
                "status": self.status,
                "accepted": accepted,
                "execution_strategy": self.execution_strategy,
                "effective_execution_strategy": self.effective_execution_strategy,
                "taskboard_revision_id": revision.revision_id,
            },
        )
        await self._emit("agent_task.completed" if accepted else "agent_task.blocked", self.result)
        return {"terminal": True, "status": self.status}

    @staticmethod
    def _taskboard_final_verification_allows_repair(
        final_verification: Mapping[str, Any],
        *,
        blocking_state_facts: Sequence[Mapping[str, Any]],
    ) -> bool:
        if not isinstance(final_verification, Mapping) or bool(final_verification.get("is_complete")):
            return False
        if blocking_state_facts:
            return False
        for field in (
            "criterion_repair_contract",
            "material_claim_repair_contract",
        ):
            repair_contract = final_verification.get(field)
            if (
                isinstance(repair_contract, Mapping)
                and str(repair_contract.get("gate_kind") or "")
                == "output_contract"
            ):
                return False
        if not bool(final_verification.get("requires_block")):
            return True
        return False

    @classmethod
    def _dedupe_taskboard_final_evidence_items(
        cls,
        items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            evidence_id = str(item.get("id") or "").strip()
            if evidence_id and evidence_id in seen:
                continue
            if evidence_id:
                seen.add(evidence_id)
            deduped.append(dict(DataFormatter.sanitize(item)))
        return deduped

    @classmethod
    def _taskboard_evidence_view_with_additional_items(
        cls,
        evidence_view: Mapping[str, Any],
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        additional = [item for item in evidence_items if isinstance(item, Mapping)]
        if not additional:
            return dict(DataFormatter.sanitize(evidence_view))
        updated = dict(DataFormatter.sanitize(evidence_view))
        raw_items = updated.get("evidence_items")
        existing = (
            [item for item in raw_items if isinstance(item, Mapping)]
            if isinstance(raw_items, Sequence) and not isinstance(raw_items, str | bytes | bytearray)
            else []
        )
        updated["evidence_items"] = cls._dedupe_taskboard_final_evidence_items([*additional, *existing])
        return DataFormatter.sanitize(updated)

    async def _taskboard_final_artifact_verification_evidence_items(
        self,
        refs: Sequence[Mapping[str, Any]],
        *,
        final: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for ref in self._dedupe_ref_records([dict(item) for item in refs if isinstance(item, Mapping)])[:4]:
            path = str(ref.get("path") or "").strip()
            if not path:
                continue
            source = str(ref.get("source") or "agent_task.taskboard.final_verification.task_workspace_artifact").strip()
            materialized_ref, content_for_locator, failure_item = await self._taskboard_materialize_final_artifact_ref(
                ref,
                source=source,
            )
            if failure_item is not None:
                items.append(failure_item)
                continue
            items.append(self._task_workspace_artifact_readback_evidence_item(materialized_ref))
            manifest = self._taskboard_final_artifact_manifest(
                materialized_ref,
                final=final,
                source=source,
            )
            locator_items = await self._task_workspace_artifact_acceptance_locator_evidence_items(
                ref=materialized_ref,
                result=final,
                manifest=manifest,
                source=source,
                content=content_for_locator,
            )
            targeted_readback_items = await self._taskboard_acceptance_locator_targeted_readback_items(locator_items)
            coverage_item = self._task_workspace_artifact_acceptance_coverage_evidence_item(
                path=path,
                source=source,
                locator_items=locator_items,
                targeted_readback_items=targeted_readback_items,
            )
            if coverage_item:
                items.append(coverage_item)
            items.extend(targeted_readback_items)
            items.extend(locator_items)
        return self._dedupe_taskboard_final_evidence_items(items)

    async def _taskboard_acceptance_locator_targeted_readback_items(
        self,
        locator_items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for locator in locator_items:
            if not isinstance(locator, Mapping):
                continue
            if str(locator.get("status") or "").strip().lower() != "ok":
                continue
            readback = await self._task_workspace_artifact_acceptance_locator_readback(locator)
            if readback is None:
                continue
            items.append(self._task_workspace_artifact_targeted_readback_evidence_item(locator, readback))
        return self._dedupe_taskboard_final_evidence_items(items)

    async def _taskboard_materialize_final_artifact_ref(
        self,
        ref: Mapping[str, Any],
        *,
        source: str,
    ) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        path = str(ref.get("path") or "").strip()
        materialized = dict(DataFormatter.sanitize(ref))
        materialized.setdefault("role", "task_workspace_artifact")
        materialized["source"] = source
        if not path:
            return materialized, "", None

        has_preview = bool(str(materialized.get("preview") or ""))
        needs_readback = (
            bool(materialized.get("truncated"))
            or bool(materialized.get("preview_truncated"))
            or not self._task_workspace_artifact_ref_has_trusted_readback(materialized)
            or not has_preview
        )
        if not needs_readback:
            content = str(materialized.get("preview") or "")
            return materialized, "" if materialized.get("truncated") else content, None

        declared_bytes = self._coerce_non_negative_int(materialized.get("bytes"))
        if declared_bytes > 0 and declared_bytes < _VERIFIER_PROMPT_VALUE_CHARS:
            max_read_bytes = declared_bytes + 1
        else:
            max_read_bytes = max(_WORKSPACE_ARTIFACT_PREVIEW_BYTES, _VERIFIER_PROMPT_VALUE_CHARS)

        try:
            read_result = await self.task_workspace.read_file(path, max_bytes=max_read_bytes)
        except Exception as error:
            return (
                materialized,
                "",
                self._task_workspace_artifact_failure_evidence_item(
                    path=path,
                    source=source,
                    code="agent_task.taskboard.final_artifact_readback_failed",
                    message="TaskBoard final artifact readback failed before final verification.",
                    readback={
                        "error": {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        }
                    },
                ),
            )

        content = str(read_result.get("content") or "")
        materialized.update(
            {
                "path": str(read_result.get("path") or path),
                "bytes": int(read_result.get("bytes") or materialized.get("bytes") or 0),
                "sha256": str(read_result.get("sha256") or materialized.get("sha256") or ""),
                "media_type": read_result.get("media_type") or materialized.get("media_type"),
                "content_kind": str(read_result.get("content_kind") or materialized.get("content_kind") or "text"),
                "preview": content,
                "truncated": bool(read_result.get("truncated")),
                "read_bytes": int(read_result.get("read_bytes") or 0),
                "handler_id": read_result.get("handler_id") or materialized.get("handler_id"),
            }
        )
        return materialized, "" if materialized.get("truncated") else content, None

    @classmethod
    def _taskboard_final_artifact_manifest(
        cls,
        ref: Mapping[str, Any],
        *,
        final: Mapping[str, Any],
        source: str,
    ) -> dict[str, Any]:
        path = str(ref.get("path") or "").strip()
        manifest: dict[str, Any] = {}
        raw_manifest = final.get("artifact_manifest") if isinstance(final, Mapping) else None
        if isinstance(raw_manifest, Mapping):
            manifest_path = str(raw_manifest.get("path") or "").strip()
            if not manifest_path or cls._task_workspace_artifact_display_path(
                manifest_path
            ) == cls._task_workspace_artifact_display_path(path):
                manifest.update(dict(DataFormatter.sanitize(raw_manifest)))
        if path:
            manifest["path"] = path
        manifest["source"] = source
        manifest["file_refs"] = [DataFormatter.sanitize(dict(ref))]
        for key in ("bytes", "sha256", "media_type", "content_kind"):
            if ref.get(key) not in (None, "", [], {}):
                manifest[key] = DataFormatter.sanitize(ref.get(key))
        return DataFormatter.sanitize(manifest)

    async def _repair_taskboard_final_evidence_use(
        self,
        final: Mapping[str, Any],
        evidence_use_guard: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any],
        *,
        language_policy: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        normalized_final = dict(
            value_with_normalized_evidence_use(
                final,
                evidence_use_guard.get("normalized_evidence_use"),
            )
        )
        original_blocking_count = self._taskboard_evidence_guard_blocking_count(evidence_use_guard)
        if original_blocking_count <= 0:
            return normalized_final, evidence_use_guard
        binding_reference_ids = set(
            self._task_reference_catalog.offered_references()
        )
        repaired_evidence_use = self._deterministic_evidence_binding_repair(
            evidence_use_guard,
            evidence_ledger,
            offered_reference_ids=binding_reference_ids,
        )
        repair_source = "deterministic"
        if not repaired_evidence_use and self._should_attempt_evidence_binding_repair(evidence_use_guard):
            if self._can_attempt_model_evidence_binding_repair():
                try:
                    attempt_count = int(self.diagnostics.get("evidence_binding_repair_attempt_count") or 0)
                except (TypeError, ValueError):
                    attempt_count = 0
                self.diagnostics["evidence_binding_repair_attempt_count"] = attempt_count + 1
                try:
                    repaired_evidence_use = await self._request_evidence_binding_repair(
                        evidence_use_guard,
                        evidence_ledger,
                        language_policy=language_policy,
                        offered_reference_ids=binding_reference_ids,
                    )
                except Exception as error:
                    self.diagnostics.setdefault("taskboard_final_evidence_binding_repair", []).append(
                        DataFormatter.sanitize(
                            {
                                "source": "model",
                                "accepted": False,
                                "original_blocking_count": original_blocking_count,
                                "error": {
                                    "type": error.__class__.__name__,
                                    "message": str(error),
                                },
                            }
                        )
                    )
                    return normalized_final, evidence_use_guard
                repair_source = "model"
        if not repaired_evidence_use:
            return normalized_final, evidence_use_guard
        merged_evidence_use = self._merge_repaired_evidence_use(
            evidence_use_guard.get("normalized_evidence_use"),
            repaired_evidence_use,
        )
        candidate_final = dict(value_with_normalized_evidence_use(normalized_final, merged_evidence_use))
        candidate_guard = validate_evidence_use(collect_evidence_use(candidate_final), evidence_ledger)
        repaired_blocking_count = self._taskboard_evidence_guard_blocking_count(candidate_guard)
        accepted = repaired_blocking_count < original_blocking_count
        self.diagnostics.setdefault("taskboard_final_evidence_binding_repair", []).append(
            DataFormatter.sanitize(
                {
                    "source": repair_source,
                    "accepted": accepted,
                    "original_blocking_count": original_blocking_count,
                    "repaired_blocking_count": repaired_blocking_count,
                    "repaired_claim_count": len(repaired_evidence_use),
                }
            )
        )
        if not accepted:
            return normalized_final, evidence_use_guard
        return candidate_final, candidate_guard

    async def _request_taskboard_final(
        self,
        revision: Any,
        evidence_view: Mapping[str, Any],
        *,
        candidate_final_result: str = "",
        board_status: str = "completed",
        schedule: Any = None,
        allow_degraded_final: bool = False,
    ) -> dict[str, Any]:
        language_policy = self._language_policy()
        explicit_state_facts = task_board_explicit_state_facts(revision, evidence_view=evidence_view)
        acceptance_index = build_task_board_acceptance_index(
            revision,
            success_criteria=self.success_criteria,
            evidence_view=evidence_view,
            explicit_state_facts=explicit_state_facts,
        )
        focus_payload = build_task_board_focus_payload(
            revision,
            acceptance_index=acceptance_index,
            schedule=schedule,
            preflight_diagnostics=explicit_state_facts,
        )
        finalizer_evidence_ledger = self._stable_evidence_ledger_view(
            evidence_view,
            max_items=64,
            body_chars=1200,
            budget_selection="content_first",
            max_overflow_refs=64,
        )
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "board_status": board_status,
                "allow_degraded_final": allow_degraded_final,
                "schedule": DataFormatter.sanitize(schedule.to_dict() if schedule is not None else {}),
                "taskboard_evidence_view": self._compact_taskboard_evidence_view_for_prompt(evidence_view),
                "evidence_ledger": self._model_evidence_ledger_projection(
                    finalizer_evidence_ledger,
                    max_items=64,
                    offered_reference_ids=set(
                        self._task_reference_catalog.offered_references()
                    ),
                ),
                "taskboard_acceptance_index": DataFormatter.sanitize(acceptance_index),
                "taskboard_focus_payload": DataFormatter.sanitize(focus_payload),
                "taskboard_explicit_state_facts": DataFormatter.sanitize(explicit_state_facts),
                "source_ref_policy": self._taskboard_source_ref_policy(),
                "source_refs": source_refs_from_ledger(evidence_view, max_refs=32)
                or self._taskboard_final_source_refs_from_evidence_view(evidence_view),
                "revision": self._compact_taskboard_revision_for_prompt(
                    revision,
                    include_card_results=False,
                ),
                "candidate_final_result": self._compact_verifier_prompt_value(candidate_final_result),
                "execution_prompt": self._execution_prompt_context(),
                "language_policy": language_policy,
            }
        )
        request.instruct(
            "Assemble a verifier-ready final result for this TaskBoard task from completed card evidence. "
            "Return the TaskBoard loop's completion decision and final user-facing synthesis. "
            "Do not create a second semantic review layer; Host-owned hard delivery and integrity gates "
            "remain authoritative when they are explicitly present. "
            "Use evidence_ledger as the authoritative grounding ledger and bind factual claims only through exact "
            "offered evidence_ledger.items[].reference_id values in evidence_use.evidence_ids; no other prompt field "
            "is an evidence identity. Use the hot evidence view for summaries and preserve cold refs "
            "as evidence pointers; do not invent unsupported facts. failed/empty ledger items support only missing "
            "or unavailable-data claims; ref_only items support only URL/path/ref discovery until readback exists. "
            "When candidate_final_result contains a "
            "complete non-file answer/report body that satisfies the criteria, preserve it as final_result "
            "instead of rewriting it into a shorter summary. For file-backed deliverables, do not copy the file body "
            "into final_result; return a concise path/ref pointer, or leave final_result empty when trusted refs already "
            "identify the delivered artifact. For source-grounded tasks, the final_result must include "
            "the concrete source URLs, file paths, or evidence refs that support the deliverable; source titles or "
            "general source names without verifier-visible URL/path refs are not enough when refs are available. "
            "For file-backed deliverables, return acceptance_points with expected headings or exact anchors for "
            "critical verification points; do not invent line numbers or trusted file refs. "
            "Apply source_ref_policy: source_refs with content_state='ref_only' are retrieval targets only, while "
            "source refs marked bounded_readback_available can support only the visible bounded preview/excerpt. "
            "If allow_degraded_final is true, the board has stopped with failed, blocked, skipped, or pending "
            "cards. You may still accept only when the completed/degraded evidence is enough to satisfy the "
            "user goal and success criteria with explicit missing-source or degraded-source boundaries in "
            "the final_result and final_response. If critical evidence is missing, set accepted=false and explain "
            "the missing criteria. When the final deliverable is useful but incomplete, keep accepted=false and "
            "write final_response as a user-facing partial-delivery note that states what was produced, what is "
            "usable, what degraded or unavailable evidence constrained the work, and which requested requirements "
            "remain unmet. Set degraded=true only when the response intentionally relies on disclosed partial, "
            "unavailable, optional, or degraded evidence rather than a full evidence path. "
            "Do not add concrete times, dates, publication states, validation states, numbers, source headings, or "
            "status details unless they are visible in the goal, evidence_ledger, trusted artifact readback, or "
            "source_refs, or are explicitly marked as derived from those facts. The runtime/current date is execution "
            "context only; do not write it as a business, incident, deployment, publication, approval, or validation "
            "date unless task evidence explicitly provides it. Unsupported concrete additions must "
            "be reported as gaps instead of accepted as harmless prose. Preserve uncertainty and evidence strength "
            "exactly: no-known-loss, still-running audit, unpublished manifest, missing sign-off, and unresolved "
            "warning states must not become confirmed absence, complete validation, publication, approval, or fix. "
            "When evidence says no data loss is known and an audit is still running, do not state or imply that data "
            "is intact, complete, safe, fully verified, or that no data was lost. "
            "Unless the user explicitly requests a fill-in template, do not leave unresolved placeholders such as "
            "[date], [time], [name], [Your Name], [Title], TODO, or TBD in a final deliverable; omit unknown "
            "details or write a role-generic sentence grounded in available facts. "
            "After the final result fields, include short self_check, short_summary, and progress_message for "
            "downstream verification/repair context and human progress. These process fields are not evidence and "
            "must not include raw chain-of-thought or long evidence bodies."
        )
        request.output(
            {
                "accepted": (bool, "True only when all success criteria are satisfied", True),
                "reason": (str, "Concise final verification reason", True),
                "final_result": (
                    str,
                    "Final non-file business result or concise TaskWorkspace artifact path/ref pointer when accepted.",
                    False,
                ),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when accepted", False),
                "degraded": (
                    bool,
                    "True when the final answer intentionally uses disclosed partial/unavailable/degraded evidence.",
                    False,
                ),
                "degradation_reason": (
                    str,
                    "Short user-safe explanation of the degraded evidence or execution boundary.",
                    False,
                ),
                "final_response": (
                    str,
                    "User-facing final answer/status note addressed to the original request. Include artifact quality, degradation boundaries, and unmet requirements without copying long file bodies.",
                    False,
                ),
                "evidence_use": (
                    [dict],
                    "Claim bindings: [{claim, evidence_ids, support_type}], where every evidence_ids value is an exact offered evidence_ledger.items[].reference_id and support_type is content, unavailability, or ref_pointer",
                    False,
                ),
                "acceptance_points": (
                    [dict],
                    "Optional artifact verification anchors: [{criterion, expected_anchor, evidence_ids, artifact_path}], where evidence_ids contains only exact offered evidence_ledger.items[].reference_id values",
                    False,
                ),
                "self_check": (
                    str,
                    "Short finalization self check of uncertainty or residual risk.",
                    False,
                ),
                "short_summary": (
                    str,
                    "Short finalization summary for terminal verification or repair.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable finalization progress sentence; do not claim verifier acceptance.",
                    False,
                ),
            },
            format="json",
        )
        result = await self._await_task_request(request.async_get_data(), stage="taskboard_finalize")
        if isinstance(result, Mapping):
            await self._emit_process_progress_from_output(
                result,
                stage="taskboard_finalize",
            )
            return dict(result)
        return {"accepted": False, "reason": str(result), "final_result": "", "missing_criteria": self.success_criteria}

    def _promote_taskboard_final_candidate(
        self,
        revision: Any,
        *,
        candidate_final_result: str,
        final_refs: Sequence[Mapping[str, Any]],
        board_status: str,
    ) -> dict[str, Any] | None:
        if str(board_status or "") != "completed":
            return None
        sources = self._taskboard_promotable_deliverable_sources(revision)
        if len(sources) != 1:
            return None
        trusted_refs = [dict(DataFormatter.sanitize(ref)) for ref in final_refs if self._is_trusted_task_workspace_artifact_ref(ref)]
        source = sources[0]
        explicit_final_result = str(source.get("explicit_final_result") or "").strip()
        if explicit_final_result:
            final_result = explicit_final_result
            final_result_source = "explicit_final_result"
        elif trusted_refs:
            final_result = self._task_workspace_artifact_final_result_from_refs(trusted_refs)
            final_result_source = "task_workspace_artifact"
        else:
            final_result = candidate_final_result.strip()
            final_result_source = "candidate_final_result"
        if not final_result:
            return None
        evidence_use = source.get("evidence_use") if isinstance(source, Mapping) else []
        return DataFormatter.sanitize(
            {
                "accepted": True,
                "reason": "Promoted a completed terminal TaskBoard candidate without redundant final synthesis.",
                "final_result": final_result,
                "missing_criteria": [],
                "evidence_use": evidence_use if isinstance(evidence_use, Sequence) and not isinstance(evidence_use, str | bytes | bytearray) else [],
                "taskboard_final_promotion": {
                    "source_card_id": source.get("card_id"),
                    "file_ref_count": len(trusted_refs),
                    "final_result_source": final_result_source,
                },
            }
        )

    def _taskboard_promotable_deliverable_sources(self, revision: Any) -> list[dict[str, Any]]:
        graph = getattr(revision, "graph", None)
        cards = list(getattr(graph, "cards", []) or [])
        card_results = getattr(revision, "card_results", {}) or {}
        depended_on: set[str] = set()
        for card in cards:
            depended_on.update(str(card_id) for card_id in getattr(card, "depends_on", ()) or ())
        leaf_ids = {str(getattr(card, "id", "")) for card in cards if str(getattr(card, "id", "")) not in depended_on}
        candidate_ids = leaf_ids or {str(card_id) for card_id in card_results.keys()}
        sources: list[dict[str, Any]] = []
        for card_id, result in card_results.items():
            card_id_text = str(card_id)
            if card_id_text not in candidate_ids:
                continue
            if str(getattr(result, "status", "")).strip().lower() != "completed":
                continue
            preview = getattr(result, "preview", None)
            candidate = self._candidate_final_result_from_execution_result(preview, include_answer=False)
            explicit_final_result = (
                str(preview.get("final_result") or "").strip()
                if isinstance(preview, Mapping)
                else ""
            )
            trusted_refs = self._trusted_taskboard_result_refs(result, preview)
            if not candidate and not trusted_refs:
                continue
            sources.append(
                {
                    "card_id": card_id_text,
                    "candidate_final_result": candidate,
                    "explicit_final_result": explicit_final_result,
                    "file_refs": trusted_refs,
                    "declared_task_workspace_paths": self._taskboard_declared_task_workspace_paths_from_preview(preview),
                    "evidence_use": self._taskboard_result_evidence_use(preview),
                }
            )
        return DataFormatter.sanitize(sources)

    def _trusted_taskboard_result_refs(self, result: Any, preview: Any) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                if self._is_trusted_task_workspace_artifact_ref(value):
                    refs.append(dict(DataFormatter.sanitize(value)))
                    return
                for key in ("file_refs", "artifact_refs"):
                    nested = value.get(key)
                    if isinstance(nested, Sequence) and not isinstance(nested, str | bytes | bytearray):
                        for item in nested:
                            collect(item)
                manifest = value.get("artifact_manifest")
                if isinstance(manifest, Mapping):
                    collect(manifest)
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    collect(item)

        collect(getattr(result, "file_refs", ()))
        collect(getattr(result, "artifact_refs", ()))
        collect(preview)
        return self._dedupe_ref_records(refs)

    @staticmethod
    def _taskboard_result_evidence_use(preview: Any) -> list[dict[str, Any]]:
        if not isinstance(preview, Mapping):
            return []
        evidence_use = preview.get("evidence_use")
        if isinstance(evidence_use, Mapping):
            return [dict(DataFormatter.sanitize(evidence_use))]
        if isinstance(evidence_use, Sequence) and not isinstance(evidence_use, str | bytes | bytearray):
            return [dict(DataFormatter.sanitize(item)) for item in evidence_use if isinstance(item, Mapping)]
        return []

    def _taskboard_candidate_final_result(self, revision: Any) -> str:
        graph = getattr(revision, "graph", None)
        cards = list(getattr(graph, "cards", []) or [])
        card_results = getattr(revision, "card_results", {}) or {}
        depended_on: set[str] = set()
        for card in cards:
            depended_on.update(str(card_id) for card_id in getattr(card, "depends_on", ()) or ())
        leaf_ids = {str(getattr(card, "id", "")) for card in cards if str(getattr(card, "id", "")) not in depended_on}
        structured_candidates: list[str] = []
        leaf_fallback_candidates: list[str] = []
        fallback_candidates: list[str] = []
        for card_id, result in card_results.items():
            if str(getattr(result, "status", "")) != "completed":
                continue
            preview = getattr(result, "preview", None)
            structured_candidate = self._candidate_final_result_from_execution_result(
                preview,
                include_answer=False,
            )
            if structured_candidate:
                structured_candidates.append(structured_candidate)
                continue
            fallback_candidate = self._candidate_final_result_from_execution_result(
                preview,
                include_answer=True,
            )
            if not fallback_candidate:
                continue
            fallback_candidates.append(fallback_candidate)
            if not leaf_ids or str(card_id) in leaf_ids:
                leaf_fallback_candidates.append(fallback_candidate)
        if structured_candidates:
            return max(structured_candidates, key=len, default="")
        if leaf_fallback_candidates:
            return max(leaf_fallback_candidates, key=len, default="")
        return max(fallback_candidates, key=len, default="")

    @classmethod
    def _normalize_taskboard_final_result(
        cls,
        final: dict[str, Any],
        candidate_final_result: str,
        *,
        fallback_final_result: str = "",
    ) -> dict[str, Any]:
        candidate = candidate_final_result.strip()
        fallback = fallback_final_result.strip()
        if not candidate and not fallback:
            return final
        accepted = cls._normalize_bool(final.get("accepted"), default=bool(final.get("final_result")))
        if not accepted:
            return final
        final_result = str(final.get("final_result") or "").strip()
        if not final_result and fallback:
            normalized = dict(final)
            normalized["final_result"] = fallback
            return normalized
        if candidate and (
            not final_result
            or cls._looks_like_candidate_prefix(final_result, candidate)
            or cls._candidate_substantially_more_complete(final_result, candidate)
        ):
            normalized = dict(final)
            normalized["final_result"] = candidate
            return normalized
        return final

    @classmethod
    def _taskboard_completion_notes(
        cls,
        revision: Any,
        *,
        final: Mapping[str, Any] | None = None,
        final_verification: Mapping[str, Any] | None = None,
        acceptance_verification_plan: Mapping[str, Any] | None = None,
        max_cards: int = 12,
        max_notes: int = 12,
    ) -> dict[str, Any]:
        """Project bounded card/process notes for human-visible final status.

        Completion notes are deliberately a projection over card outputs,
        finalizer fields, verifier fields, and acceptance diagnostics. They are
        not EvidenceEnvelope evidence and do not accept or reject the task.
        """
        graph = getattr(revision, "graph", None)
        graph_cards = list(getattr(graph, "cards", []) or [])
        card_by_id = {str(getattr(card, "id", "")): card for card in graph_cards if str(getattr(card, "id", ""))}
        card_results = getattr(revision, "card_results", {}) or {}
        ordered_card_ids = [str(getattr(card, "id", "")) for card in graph_cards if str(getattr(card, "id", ""))]
        for card_id in card_results.keys():
            card_id_text = str(card_id)
            if card_id_text not in ordered_card_ids:
                ordered_card_ids.append(card_id_text)

        cards: list[dict[str, Any]] = []
        known_limits: list[str] = []
        quality_notes: list[str] = []
        process_notes: list[str] = []
        final_verification_complete = (
            isinstance(final_verification, Mapping)
            and final_verification.get("is_complete") is True
        )
        acceptance_all_satisfied = (
            isinstance(acceptance_verification_plan, Mapping)
            and acceptance_verification_plan.get("all_satisfied") is True
        )
        resolved_terminal_state = final_verification_complete and acceptance_all_satisfied

        for card_id in ordered_card_ids[: max(max_cards, 0)]:
            result = card_results.get(card_id)
            card = card_by_id.get(card_id)
            status = str(getattr(result, "status", getattr(card, "status", "")) or "").strip()
            preview = getattr(result, "preview", None)
            metadata = getattr(result, "metadata", {}) if result is not None else {}
            process_summary = metadata.get("process_summary") if isinstance(metadata, Mapping) else None

            summary = cls._taskboard_first_completion_note(
                cls._taskboard_completion_field_texts(
                    preview,
                    ("short_summary", "summary", "final_result", "candidate_final_result", "answer", "reason"),
                    max_items=4,
                ),
                cls._taskboard_completion_field_texts(
                    process_summary,
                    ("short_summary", "summary", "reason"),
                    max_items=4,
                ),
            )
            progress = cls._taskboard_first_completion_note(
                cls._taskboard_completion_field_texts(preview, ("progress_message",), max_items=2),
                cls._taskboard_completion_field_texts(process_summary, ("progress_message",), max_items=2),
            )
            card_limits = cls._taskboard_dedupe_notes(
                [
                    *cls._taskboard_completion_field_texts(
                        preview,
                        ("gaps", "remaining_work", "degradation_reason", "missing_criteria"),
                        max_items=8,
                    ),
                    *cls._taskboard_completion_field_texts(
                        process_summary,
                        ("gaps", "remaining_work", "degradation_reason", "missing_criteria"),
                        max_items=8,
                    ),
                ],
                max_notes=8,
            )
            card_quality_notes = cls._taskboard_dedupe_notes(
                [
                    *cls._taskboard_completion_field_texts(preview, ("self_check", "verification_summary"), max_items=4),
                    *cls._taskboard_completion_field_texts(
                        process_summary,
                        ("self_check", "verification_summary"),
                        max_items=4,
                    ),
                ],
                max_notes=6,
            )
            diagnostics = getattr(result, "diagnostics", ()) if result is not None else ()
            diagnostic_notes = cls._taskboard_completion_field_texts(
                diagnostics,
                ("reason", "message", "summary"),
                max_items=4,
            )
            status_lower = status.lower()
            if status_lower in {"setback", "blocked", "failed", "skipped"} and not card_limits:
                fallback = cls._taskboard_first_completion_note(diagnostic_notes, [summary])
                if fallback:
                    card_limits.append(fallback)
                else:
                    card_limits.append(f"Card {card_id} ended with status {status}.")

            objective = str(getattr(card, "objective", "") or "").strip() if card is not None else ""
            card_note: dict[str, Any] = {
                "card_id": card_id,
                "status": status or "unknown",
            }
            if objective:
                card_note["objective"] = cls._taskboard_note_text(objective, max_chars=240)
            if summary:
                card_note["completion_summary"] = summary
            if progress:
                card_note["progress_message"] = progress
            card_limits_are_resolved_process = resolved_terminal_state and (
                status_lower in {"setback", "blocked", "failed", "skipped"}
                or card_id.startswith("final-verification-repair")
            )
            if card_limits and not card_limits_are_resolved_process:
                card_note["known_limits"] = card_limits[:4]
            if card_quality_notes:
                card_note["quality_notes"] = card_quality_notes[:4]
            if diagnostic_notes and status_lower in {"setback", "blocked", "failed", "skipped"}:
                card_note["diagnostic_notes"] = diagnostic_notes[:3]

            cards.append(card_note)
            for note in card_limits:
                if card_limits_are_resolved_process:
                    process_notes.append(f"{card_id}: resolved earlier setback - {note}")
                else:
                    known_limits.append(f"{card_id}: {note}")
            for note in card_quality_notes:
                quality_notes.append(f"{card_id}: {note}")

        effective_final = final if isinstance(final, Mapping) else {}
        known_limits.extend(
            cls._taskboard_completion_field_texts(
                effective_final,
                ("degradation_reason", "missing_criteria"),
                max_items=6,
            )
        )
        quality_notes.extend(
            cls._taskboard_completion_field_texts(
                effective_final,
                ("self_check", "short_summary", "progress_message"),
                max_items=6,
            )
        )

        verification_summary = cls._taskboard_final_verification_notes(
            final_verification,
            max_notes=max_notes,
        )
        known_limits.extend(verification_summary.get("known_limits", []))
        quality_notes.extend(verification_summary.get("quality_notes", []))

        acceptance_summary = cls._taskboard_acceptance_summary_notes(
            acceptance_verification_plan,
        )
        process_notes.extend(acceptance_summary.get("process_notes", []))

        return DataFormatter.sanitize(
            {
                "schema_version": TASK_BOARD_COMPLETION_NOTES_SCHEMA_VERSION,
                "authority": "projection_only",
                "semantic_owner": "taskboard_final_verifier",
                "cards": cards,
                "known_limits": cls._taskboard_dedupe_notes(known_limits, max_notes=max_notes),
                "quality_notes": cls._taskboard_dedupe_notes(quality_notes, max_notes=max_notes),
                "process_notes": cls._taskboard_dedupe_notes(process_notes, max_notes=max_notes),
                "acceptance_summary": acceptance_summary.get("summary", {}),
                "final_verification_summary": verification_summary.get("summary", {}),
                "metadata": {
                    "projection_role": "human_progress_and_final_response_context",
                    "not_evidence": True,
                    "max_cards": max_cards,
                    "max_notes": max_notes,
                },
            }
        )

    @classmethod
    def _taskboard_completion_field_texts(
        cls,
        value: Any,
        field_names: Sequence[str],
        *,
        max_items: int = 6,
        depth: int = 0,
    ) -> list[str]:
        if max_items <= 0:
            return []
        if value in (None, "", [], {}):
            return []
        wanted = {str(name).lower() for name in field_names}
        notes: list[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key).lower()
                if key_text in wanted:
                    notes.extend(cls._taskboard_completion_texts(item, max_items=max_items - len(notes)))
                if depth < 3:
                    notes.extend(
                        cls._taskboard_completion_field_texts(
                            item,
                            field_names,
                            max_items=max_items - len(notes),
                            depth=depth + 1,
                        )
                    )
                if len(notes) >= max_items:
                    break
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for item in value:
                notes.extend(
                    cls._taskboard_completion_field_texts(
                        item,
                        field_names,
                        max_items=max_items - len(notes),
                        depth=depth,
                    )
                )
                if len(notes) >= max_items:
                    break
        return cls._taskboard_dedupe_notes(notes, max_notes=max_items)

    @classmethod
    def _taskboard_completion_texts(cls, value: Any, *, max_items: int = 6) -> list[str]:
        if max_items <= 0 or value in (None, "", [], {}):
            return []
        if isinstance(value, str):
            text = cls._taskboard_note_text(value)
            return [text] if text else []
        if isinstance(value, bool | int | float):
            return [str(value)]
        if isinstance(value, Mapping):
            notes: list[str] = []
            for key in ("summary", "reason", "message", "criterion", "status", "name"):
                if key in value:
                    notes.extend(cls._taskboard_completion_texts(value.get(key), max_items=max_items - len(notes)))
                if len(notes) >= max_items:
                    break
            if notes:
                return cls._taskboard_dedupe_notes(notes, max_notes=max_items)
            text = cls._taskboard_note_text(value)
            return [text] if text else []
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            notes: list[str] = []
            for item in value:
                notes.extend(cls._taskboard_completion_texts(item, max_items=max_items - len(notes)))
                if len(notes) >= max_items:
                    break
            return cls._taskboard_dedupe_notes(notes, max_notes=max_items)
        text = cls._taskboard_note_text(value)
        return [text] if text else []

    @classmethod
    def _taskboard_first_completion_note(cls, *note_groups: Sequence[str]) -> str:
        for group in note_groups:
            for note in group:
                text = cls._taskboard_note_text(note)
                if text:
                    return text
        return ""

    @staticmethod
    def _taskboard_note_text(value: Any, *, max_chars: int = 320) -> str:
        text = " ".join(str(DataFormatter.sanitize(value) if isinstance(value, Mapping) else value or "").split())
        if not text:
            return ""
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 24)].rstrip() + " [truncated]"

    @classmethod
    def _taskboard_dedupe_notes(cls, notes: Sequence[Any], *, max_notes: int) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for note in notes:
            text = cls._taskboard_note_text(note)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(text)
            if len(deduped) >= max_notes:
                break
        return deduped

    @classmethod
    def _taskboard_final_verification_notes(
        cls,
        final_verification: Mapping[str, Any] | None,
        *,
        max_notes: int,
    ) -> dict[str, Any]:
        if not isinstance(final_verification, Mapping):
            return {"summary": {}, "known_limits": [], "quality_notes": []}
        summary: dict[str, Any] = {}
        reason = cls._taskboard_note_text(final_verification.get("reason"), max_chars=420)
        if reason:
            summary["reason"] = reason
        if final_verification.get("verification_source") not in (None, "", [], {}):
            summary["verification_source"] = DataFormatter.sanitize(final_verification.get("verification_source"))
        is_complete = final_verification.get("is_complete") is True
        failure_analysis = cls._taskboard_completion_field_texts(
            final_verification,
            ("failure_analysis",),
            max_items=max_notes,
        )
        known_limits = cls._taskboard_completion_field_texts(
            final_verification,
            ("missing_criteria",),
            max_items=max_notes,
        )
        acceptance_delta = cls._taskboard_completion_field_texts(
            final_verification,
            ("acceptance_delta",),
            max_items=max_notes,
        )
        quality_notes: list[str] = []
        if is_complete:
            quality_notes.extend(failure_analysis)
            quality_notes.extend(acceptance_delta)
        else:
            known_limits.extend(failure_analysis)
            known_limits.extend(acceptance_delta)
        criterion_notes: list[dict[str, Any]] = []
        raw_checks = final_verification.get("criterion_checks")
        checks = raw_checks if isinstance(raw_checks, Sequence) and not isinstance(raw_checks, str | bytes | bytearray) else ()
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            criterion = cls._taskboard_note_text(check.get("criterion") or check.get("name") or check.get("claim"), max_chars=240)
            status = cls._taskboard_note_text(check.get("status"), max_chars=80)
            note = cls._taskboard_note_text(check.get("summary") or check.get("reason") or check.get("evidence"), max_chars=320)
            compact_check: dict[str, Any] = {}
            if criterion:
                compact_check["criterion"] = criterion
            if status:
                compact_check["status"] = status
            if note:
                compact_check["summary"] = note
            if compact_check:
                criterion_notes.append(compact_check)
            line = ": ".join(part for part in (criterion, status) if part)
            if note:
                line = f"{line} - {note}" if line else note
            if not line:
                continue
            if cls._taskboard_criterion_check_is_satisfied(check):
                quality_notes.append(line)
            else:
                known_limits.append(line)
            if len(criterion_notes) >= max_notes:
                break
        if criterion_notes:
            summary["criterion_checks"] = criterion_notes
        return {
            "summary": DataFormatter.sanitize(summary),
            "known_limits": cls._taskboard_dedupe_notes(known_limits, max_notes=max_notes),
            "quality_notes": cls._taskboard_dedupe_notes(quality_notes, max_notes=max_notes),
        }

    @staticmethod
    def _taskboard_criterion_check_is_satisfied(check: Mapping[str, Any]) -> bool:
        return check.get("satisfied") is True

    @classmethod
    def _taskboard_acceptance_summary_notes(
        cls,
        acceptance_verification_plan: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not isinstance(acceptance_verification_plan, Mapping):
            return {"summary": {}, "process_notes": []}
        metadata = acceptance_verification_plan.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        status_counts = acceptance_verification_plan.get("status_counts")
        status_counts = status_counts if isinstance(status_counts, Mapping) else {}
        summary = {
            "all_satisfied": bool(acceptance_verification_plan.get("all_satisfied")),
            "status_counts": dict(DataFormatter.sanitize(status_counts)),
            "dirty_count": metadata.get("dirty_count"),
            "green_count": metadata.get("green_count"),
            "total_items": metadata.get("total_items"),
            "acceptance_progress_percent": metadata.get("acceptance_progress_percent"),
        }
        process_notes: list[str] = []
        if acceptance_verification_plan.get("all_satisfied") is not True:
            unresolved_parts = [
                f"{status}={count}"
                for status, count in status_counts.items()
                if status in {"unknown", "active", "setback", "blocked", "deferred"}
                and cls._coerce_non_negative_int(count) > 0
            ]
            progress = metadata.get("acceptance_progress_percent")
            dirty = metadata.get("dirty_count")
            detail = ", ".join(unresolved_parts) if unresolved_parts else "projection not fully green"
            suffix = []
            if progress not in (None, "", [], {}):
                suffix.append(f"progress={progress}%")
            if dirty not in (None, "", [], {}):
                suffix.append(f"dirty_count={dirty}")
            process_notes.append(
                "Acceptance projection still reports unresolved progress"
                + f" ({detail}{'; ' + ', '.join(suffix) if suffix else ''})."
            )
        return {"summary": DataFormatter.sanitize(summary), "process_notes": process_notes}

    @classmethod
    def _taskboard_completion_notes_known_limits(cls, completion_notes: Mapping[str, Any] | None) -> list[str]:
        if not isinstance(completion_notes, Mapping):
            return []
        return cls._taskboard_dedupe_notes(
            cls._normalize_string_list(completion_notes.get("known_limits")),
            max_notes=12,
        )

    @classmethod
    def _taskboard_completion_notes_disclosure(cls, completion_notes: Mapping[str, Any] | None) -> str:
        if not isinstance(completion_notes, Mapping):
            return ""
        known_limits = cls._taskboard_completion_notes_known_limits(completion_notes)
        quality_notes = cls._taskboard_dedupe_notes(
            cls._normalize_string_list(completion_notes.get("quality_notes")),
            max_notes=6,
        )
        process_notes = cls._taskboard_dedupe_notes(
            cls._normalize_string_list(completion_notes.get("process_notes")),
            max_notes=6,
        )
        selected = list(known_limits[:3])
        for note in quality_notes:
            if len(selected) >= 4:
                break
            if note not in selected:
                selected.append(note)
        if not selected:
            selected = process_notes[:3]
        if not selected:
            return ""
        label = "Known limitations/notes" if known_limits else "Process notes"
        return f"{label}: " + "; ".join(selected) + "."

    @classmethod
    def _taskboard_final_is_degraded(
        cls,
        final: Mapping[str, Any],
        *,
        board_status: str,
        degraded_finalization_attempted: bool,
        completion_notes: Mapping[str, Any] | None = None,
    ) -> bool:
        if degraded_finalization_attempted or str(board_status or "").strip().lower() != "completed":
            return True
        if cls._normalize_bool(final.get("degraded"), default=False):
            return True
        artifact_status = str(final.get("artifact_status") or "").strip().lower()
        if artifact_status in {"degraded", "partial_success"}:
            return True
        return bool(cls._taskboard_completion_notes_known_limits(completion_notes))

    @classmethod
    def _taskboard_final_artifact_status(
        cls,
        *,
        accepted: bool,
        degraded: bool,
        final: Mapping[str, Any],
    ) -> str:
        raw_status = str(final.get("artifact_status") or "").strip().lower()
        if accepted:
            if degraded or raw_status in {"degraded", "partial_success"}:
                return "degraded"
            return "accepted"
        if raw_status == "blocked":
            return "blocked"
        return "partial"

    @classmethod
    def _taskboard_user_final_response(
        cls,
        *,
        final: Mapping[str, Any],
        accepted: bool,
        artifact_status: str,
        reason: str,
        missing_criteria: Any,
        final_refs: Sequence[Mapping[str, Any]],
        board_status: str,
        degraded_finalization_attempted: bool,
        completion_notes: Mapping[str, Any] | None = None,
    ) -> str:
        has_known_limits = bool(cls._taskboard_completion_notes_known_limits(completion_notes))
        should_disclose_notes = (not accepted) or artifact_status == "degraded" or has_known_limits
        disclosure = (
            cls._taskboard_completion_notes_disclosure(completion_notes)
            if should_disclose_notes
            else ""
        )
        return cls._agent_task_user_final_response(
            final=final,
            accepted=accepted,
            artifact_status=artifact_status,
            status="completed" if accepted else "blocked",
            reason=reason,
            missing_criteria=missing_criteria,
            final_refs=final_refs,
            final_result=final.get("final_result"),
            degraded=artifact_status == "degraded",
            degradation_reason=str(final.get("degradation_reason") or ""),
            degraded_finalization_attempted=degraded_finalization_attempted,
            board_status=board_status,
            disclosure=disclosure,
        )

    @staticmethod
    def _looks_like_candidate_prefix(value: str, candidate: str) -> bool:
        value = value.strip()
        candidate = candidate.strip()
        if not value or len(value) >= len(candidate):
            return False
        if candidate.startswith(value):
            return True
        compact_value = " ".join(value.split())
        compact_candidate = " ".join(candidate.split())
        return len(compact_value) < len(compact_candidate) and compact_candidate.startswith(compact_value)

    @staticmethod
    def _candidate_substantially_more_complete(value: str, candidate: str) -> bool:
        value = value.strip()
        candidate = candidate.strip()
        if len(candidate) < 1200 or len(value) >= len(candidate):
            return False
        return len(value) <= max(800, len(candidate) // 2)


__all__ = ["AgentTaskTaskBoardFinalizationMixin"]
