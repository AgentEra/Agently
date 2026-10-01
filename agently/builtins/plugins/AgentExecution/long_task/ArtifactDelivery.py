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

import html
import re
from pathlib import PurePosixPath

from agently.types.data import (
    TaskWorkspaceFileRef,
    RecordRef,
    RecordReference,
    TaskWorkspaceRetentionResult,
    TaskWorkspaceTerminalStatus,
)
from agently.core.TaskWorkspace import TaskWorkspacePolicyError

from .AcceptanceLocator import build_task_workspace_artifact_acceptance_locator_items, collect_acceptance_points
from .TaskShared import (
    AgentTaskMixinBase,
    Any,
    asyncio,
    Awaitable,
    cast,
    DataFormatter,
    evidence_envelope_from_value,
    json,
    Mapping,
    parse_reference_tokens,
    Path,
    Sequence,
    source_refs_from_ledger,
    suppress,
    TaskBoardCardResult,
    TaskContextView,
    validate_reference_tokens,
    _compact_agent_task_error_message,
    _WORKSPACE_ARTIFACT_CONTENT_KEYS,
    _WORKSPACE_ARTIFACT_PREVIEW_BYTES,
    _WORKSPACE_ARTIFACT_RESULT_BODY_KEYS,
)

_WORKSPACE_ARTIFACT_LOCATOR_SCAN_BYTES = 5_000_000
_GROUNDING_WORKSPACE_REPLACE_OLD_KEYS = (
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
_GROUNDING_WORKSPACE_REPLACE_NEW_KEYS = (
    "new_string",
    "new",
    "to",
    "replacement",
    "new_text",
    "to_text",
    "replacement_text",
)


_PUBLIC_DELTA_RETRY_MARKER_RE = re.compile(r"\A<\$retry(?::(?P<label>[^>]*)?)?>(?P<body>.*?)</\$retry>\Z", re.DOTALL)


class AgentTaskArtifactMixin(AgentTaskMixinBase):
    @staticmethod
    def _grounding_task_workspace_patch_scope_text(value: Any) -> str:
        text = " ".join(str(value or "").split())
        # Grounding claims are plain text, while exact TaskWorkspace spans may wrap
        # a label in Markdown emphasis. Scope comparison ignores only paired
        # emphasis delimiters; the actual edit still requires an exact match.
        return text.replace("**", "").replace("__", "").casefold()

    @staticmethod
    def _grounding_patch_mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _grounding_patch_first_string(
        value: Mapping[str, Any],
        keys: Sequence[str],
    ) -> str:
        for key in keys:
            if key in value:
                return str(value.get(key) or "")
        return ""

    @staticmethod
    def _grounding_task_workspace_patch_path(value: Mapping[str, Any]) -> str:
        for key in ("path", "file", "target_path", "target_file", "task_workspace_path"):
            path = str(value.get(key) or "").strip()
            if path:
                return path
        return ""

    @staticmethod
    def _grounding_task_workspace_patch_operations(value: Mapping[str, Any]) -> Any:
        for key in ("operations", "edits", "patches"):
            if key in value:
                return value.get(key)
        return None

    def _grounding_task_workspace_patch_scope(
        self,
        patch_proposal: Mapping[str, Any],
        grounding_contract: Mapping[str, Any],
        *,
        allowed_patch_paths: Sequence[Any],
        require_exact_claim_coverage: bool = False,
        require_versioned_requirements: bool = False,
    ) -> tuple[bool, str]:
        path = self._task_workspace_artifact_display_path(
            self._grounding_task_workspace_patch_path(patch_proposal)
        )
        allowed_paths = {
            self._task_workspace_artifact_display_path(item)
            for item in allowed_patch_paths
            if self._task_workspace_artifact_display_path(item)
        }
        if not path or path not in allowed_paths:
            return False, "Grounding patch must target an authorized final TaskWorkspace deliverable."

        requirements = [
            item
            for item in self._grounding_patch_mapping_sequence(grounding_contract.get("requirements"))
            if str(item.get("artifact_quote") or item.get("claim") or "").strip()
        ]
        operations = self._grounding_patch_mapping_sequence(
            self._grounding_task_workspace_patch_operations(patch_proposal)
        )
        if not requirements or not operations or len(operations) > len(requirements):
            return False, "Grounding patch requires at most one bounded replace operation per implicated claim."

        requirements_by_key = {
            str(item.get("claim_key") or "").strip(): item
            for item in requirements
            if str(item.get("claim_key") or "").strip()
        }
        if require_versioned_requirements and any(
            not str(item.get("claim_key") or "").strip()
            or not str(item.get("carrier_id") or "").strip()
            or not str(item.get("content_version_id") or "").strip()
            or not str(item.get("artifact_quote") or "").strip()
            for item in requirements
        ):
            return False, "Material-claim patch requires host-issued claim, carrier, and content-version identities."
        if require_versioned_requirements and len(requirements_by_key) != len(requirements):
            return False, "Grounding patch requirements must contain unique host-issued claim_key values."

        operation_claim_keys: list[str] = []
        normalized_claims = [
            self._grounding_task_workspace_patch_scope_text(item.get("artifact_quote") or item.get("claim"))
            for item in requirements
        ]
        for operation in operations:
            has_old_field = any(
                key in operation for key in _GROUNDING_WORKSPACE_REPLACE_OLD_KEYS
            )
            has_new_field = any(
                key in operation for key in _GROUNDING_WORKSPACE_REPLACE_NEW_KEYS
            )
            op = str(
                operation.get("type")
                or operation.get("op")
                or operation.get("operation")
                or ""
            ).strip().lower()
            if not op and has_old_field and has_new_field:
                op = "replace"
            if op != "replace" or self._normalize_bool(operation.get("replace_all"), default=False):
                return False, "Grounding patch forbids full writes, inserts, appends, and replace-all operations."
            if not has_old_field or not has_new_field:
                return False, "Grounding patch operations require explicit old_string and new_string fields."

            old = self._grounding_patch_first_string(operation, _GROUNDING_WORKSPACE_REPLACE_OLD_KEYS)
            normalized_old = self._grounding_task_workspace_patch_scope_text(old)
            claim_key = str(operation.get("claim_key") or "").strip()
            scoped_claims = normalized_claims
            if claim_key:
                requirement = requirements_by_key.get(claim_key)
                if requirement is None:
                    return False, "Grounding patch operation references an unknown claim_key."
                scoped_claims = [
                    self._grounding_task_workspace_patch_scope_text(
                        requirement.get("artifact_quote") or requirement.get("claim")
                    )
                ]
                operation_claim_keys.append(claim_key)
                if str(requirement.get("repair_policy") or "").strip() == "delete_only":
                    new = self._grounding_patch_first_string(
                        operation,
                        _GROUNDING_WORKSPACE_REPLACE_NEW_KEYS,
                    )
                    if new != "":
                        return (
                            False,
                            "Grounding patch repair_policy=delete_only requires an exactly empty new_string.",
                        )
            elif require_exact_claim_coverage:
                return False, "Every grounding patch operation must reference its host-issued claim_key."
            if len(normalized_old) < 8 or not any(
                normalized_old in claim or claim in normalized_old
                for claim in scoped_claims
                if claim
            ):
                return False, "Grounding patch old text must stay within its implicated artifact quote."

        if require_exact_claim_coverage:
            expected_keys = list(requirements_by_key)
            if (
                len(operation_claim_keys) != len(set(operation_claim_keys))
                or set(operation_claim_keys) != set(expected_keys)
            ):
                return False, "Grounding patch requires exactly one bounded replace operation per claim_key."
        return True, ""

    async def _apply_grounding_task_workspace_patch(
        self,
        patch_proposal: Mapping[str, Any],
        grounding_contract: Mapping[str, Any],
        *,
        allowed_patch_paths: Sequence[Any],
        source: str,
    ) -> dict[str, Any]:
        scoped, reason = self._grounding_task_workspace_patch_scope(
            patch_proposal,
            grounding_contract,
            allowed_patch_paths=allowed_patch_paths,
            require_exact_claim_coverage=True,
            require_versioned_requirements=True,
        )
        path = self._task_workspace_artifact_display_path(
            self._grounding_task_workspace_patch_path(patch_proposal)
        )
        if not scoped:
            return {"status": "failed", "path": path, "reason": reason}

        requirements = self._grounding_patch_mapping_sequence(grounding_contract.get("requirements"))
        expected_versions = {
            str(item.get("content_version_id") or "").strip()
            for item in requirements
            if str(item.get("content_version_id") or "").strip()
        }
        operations = self._grounding_patch_mapping_sequence(
            self._grounding_task_workspace_patch_operations(patch_proposal)
        )
        try:
            current_ref = await self.task_workspace._promote_file_identity(path, role="grounding_patch_base")
            current_version = str(current_ref.get("content_version_id") or "").strip()
            if expected_versions != {current_version}:
                raise ValueError("TaskWorkspace grounding candidate changed since the repair contract was created.")
            size = int(current_ref.get("bytes") or current_ref.get("size") or 0)
            current_readback = await self.task_workspace.read_file(path, max_bytes=max(1, size + 1))
            if bool(current_readback.get("truncated")) or not isinstance(current_readback.get("content"), str):
                raise ValueError("Grounding patch requires complete text readback before editing.")

            simulated = str(current_readback.get("content") or "")
            prepared: list[tuple[str, str, str]] = []
            for operation in operations:
                claim_key = str(operation.get("claim_key") or "").strip()
                old = self._grounding_patch_first_string(operation, _GROUNDING_WORKSPACE_REPLACE_OLD_KEYS)
                new = self._grounding_patch_first_string(operation, _GROUNDING_WORKSPACE_REPLACE_NEW_KEYS)
                if old == new:
                    raise ValueError(f"Grounding patch for {claim_key} does not change the artifact.")
                if simulated.count(old) != 1:
                    raise ValueError(
                        f"Grounding patch old text for {claim_key} must match exactly one current artifact span."
                    )
                simulated = simulated.replace(old, new, 1)
                prepared.append((claim_key, old, new))

            expected_sha256 = str(current_ref.get("sha256") or "")
            await self.task_workspace._atomic_replace_file_content(
                path,
                simulated,
                expected_sha256=expected_sha256,
                replacements=len(prepared),
            )
            operation_records = [
                {
                    "index": index,
                    "type": "replace",
                    "claim_key": claim_key,
                    "replacement_count": 1,
                }
                for index, (claim_key, _old, _new) in enumerate(prepared)
            ]

            promoted = await self.task_workspace._promote_file_identity(path, role="grounding_candidate")
            final_size = int(promoted.get("bytes") or promoted.get("size") or 0)
            readback = await self.task_workspace.read_file(
                path,
                max_bytes=min(max(1, final_size + 1), _WORKSPACE_ARTIFACT_LOCATOR_SCAN_BYTES),
            )
        except Exception as error:
            return {
                "status": "failed",
                "path": path,
                "reason": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                "error": {"type": error.__class__.__name__},
            }

        ref = {
            **dict(promoted),
            "role": "task_workspace_artifact",
            "source": source,
            "read_bytes": int(readback.get("read_bytes") or 0),
            "truncated": bool(readback.get("truncated")),
            "preview": self._truncate_prompt_text(
                str(readback.get("content") or ""),
                _WORKSPACE_ARTIFACT_PREVIEW_BYTES,
            ),
        }
        return {
            "status": "completed",
            "path": path,
            "operation_count": len(operation_records),
            "replacement_count": sum(
                int(item.get("replacement_count") or 0) for item in operation_records
            ),
            "operations": operation_records,
            "base_content_version_id": next(iter(expected_versions)),
            "content_version_id": promoted.get("content_version_id"),
            "readback": {
                "path": ref.get("path"),
                "bytes": ref.get("bytes"),
                "sha256": ref.get("sha256"),
                "content_version_id": ref.get("content_version_id"),
                "read_bytes": ref.get("read_bytes"),
                "truncated": ref.get("truncated"),
            },
            "file_refs": [DataFormatter.sanitize(ref)],
        }

    def _compact_terminal_final_result(
        self,
        value: Any,
        *,
        trusted_file_refs: Sequence[Mapping[str, Any]] = (),
        preserve_value: bool = False,
    ) -> Any:
        """Keep the actual answer, or a pointer for file-backed output."""

        if trusted_file_refs and not preserve_value:
            return self._task_workspace_artifact_final_result_from_refs(trusted_file_refs)
        return DataFormatter.sanitize(value)

    async def _register_terminal_deliverables(
        self,
        refs: Sequence[TaskWorkspaceFileRef | RecordRef | RecordReference],
    ) -> list[TaskWorkspaceFileRef]:
        """Retain only caller-selected, readback-verified file deliverables."""

        retained: list[dict[str, Any]] = []
        retention_candidates: list[dict[str, Any]] = []
        retained_keys: set[tuple[str, str, int]] = set()
        for raw_ref in refs:
            if not isinstance(raw_ref, Mapping) or raw_ref.get("type") != "file":
                self._terminal_retention_deferred = True
                self.diagnostics.setdefault("task_workspace_retention", {}).setdefault("diagnostics", []).append(
                    {
                        "code": "agent_task.retention.file_ref_required",
                        "message": "Terminal deliverables must be trusted TaskWorkspace file refs.",
                    }
                )
                continue
            candidate_ref = {
                key: DataFormatter.sanitize(raw_ref.get(key))
                for key in (
                    "type",
                    "path",
                    "task_workspace_id",
                    "execution_id",
                    "size",
                    "available",
                    "sha256",
                    "bytes",
                    "media_type",
                    "content_kind",
                    "role",
                )
                if key in raw_ref
            }
            path = str(raw_ref.get("path") or "").strip()
            expected_digest = str(raw_ref.get("sha256") or "").strip()
            raw_size = raw_ref.get("size")
            expected_size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else -1
            if (
                not path
                or expected_size < 0
                or not expected_digest
                or str(raw_ref.get("task_workspace_id") or "")
                != self.task_workspace.task_workspace_id
                or str(raw_ref.get("execution_id") or "") != self.task_workspace.execution_id
            ):
                self._terminal_retention_deferred = True
                self.diagnostics.setdefault("task_workspace_retention", {}).setdefault("diagnostics", []).append(
                    {
                        "code": "agent_task.retention.file_ref_invalid",
                        "message": "Terminal file ref identity, size, or digest is incomplete.",
                        "path": path,
                    }
                )
                retention_candidates.append(candidate_ref)
                continue
            try:
                readback = await self.task_workspace.read_file(path, max_bytes=1)
                actual_size = int(readback.get("bytes") or 0)
                actual_digest = str(readback.get("sha256") or "")
            except Exception as error:
                actual_size = -1
                actual_digest = ""
                self.diagnostics.setdefault("task_workspace_retention", {}).setdefault("diagnostics", []).append(
                    {
                        "code": "agent_task.retention.file_readback_failed",
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        "path": path,
                    }
                )
            if actual_size != expected_size or actual_digest != expected_digest:
                self._terminal_retention_deferred = True
                self.diagnostics.setdefault("task_workspace_retention", {}).setdefault("diagnostics", []).append(
                    {
                        "code": "agent_task.retention.file_integrity_mismatch",
                        "message": "Terminal file size or digest changed after trusted readback.",
                        "path": path,
                    }
                )
                retention_candidates.append(candidate_ref)
                continue
            reference_validation = await self._validate_terminal_artifact_reference_tokens(
                path,
                content_kind=str(raw_ref.get("content_kind") or readback.get("content_kind") or "unknown"),
            )
            if not reference_validation.get("valid"):
                self._terminal_retention_deferred = True
                self.diagnostics.setdefault("task_workspace_retention", {}).setdefault("diagnostics", []).append(
                    {
                        "code": "agent_task.retention.reference_token_invalid",
                        "message": str(reference_validation.get("reason") or "Terminal reference token validation failed."),
                        "path": path,
                    }
                )
                retention_candidates.append(candidate_ref)
                continue
            if reference_validation.get("legacy_reference_unverified"):
                self.diagnostics.setdefault("reference_tokens", {})[path] = {
                    "status": "legacy_reference_unverified",
                    "reference_ids": [],
                    "source_cards": [],
                }
            elif reference_validation.get("reference_ids"):
                self.diagnostics.setdefault("reference_tokens", {})[path] = {
                    "status": "validated",
                    "reference_ids": list(reference_validation.get("reference_ids") or []),
                    "source_cards": list(reference_validation.get("source_cards") or []),
                }
            try:
                promoted_ref = await self.task_workspace._promote_file_identity(
                    path,
                    role=str(raw_ref.get("role") or "task_workspace_artifact"),
                )
            except Exception as error:
                self._terminal_retention_deferred = True
                self.diagnostics.setdefault("task_workspace_retention", {}).setdefault("diagnostics", []).append(
                    {
                        "code": "agent_task.retention.identity_promotion_failed",
                        "message": _compact_agent_task_error_message(
                            error,
                            fallback=error.__class__.__name__,
                        ),
                        "path": path,
                    }
                )
                retention_candidates.append(candidate_ref)
                continue
            promoted_size = promoted_ref.get("size")
            if (
                str(promoted_ref.get("sha256") or "") != expected_digest
                or isinstance(promoted_size, bool)
                or not isinstance(promoted_size, int)
                or promoted_size != expected_size
            ):
                self._terminal_retention_deferred = True
                self.diagnostics.setdefault("task_workspace_retention", {}).setdefault("diagnostics", []).append(
                    {
                        "code": "agent_task.retention.identity_promotion_mismatch",
                        "message": "Promoted content identity does not match terminal readback.",
                        "path": path,
                    }
                )
                retention_candidates.append(candidate_ref)
                continue
            candidate_ref = dict(DataFormatter.sanitize(promoted_ref))
            key = (path, expected_digest, expected_size)
            if key in retained_keys:
                continue
            retained_keys.add(key)
            retained.append(candidate_ref)
            retention_candidates.append(candidate_ref)

        self._terminal_retained_refs = retention_candidates
        self._terminal_deliverable_refs = cast(Any, retained)
        return cast(list[TaskWorkspaceFileRef], retained)

    async def _validate_terminal_artifact_reference_tokens(
        self,
        path: str,
        *,
        content_kind: str,
    ) -> dict[str, Any]:
        if content_kind != "text":
            return {
                "valid": True,
                "legacy_reference_unverified": False,
                "reference_ids": [],
                "source_cards": [],
            }
        target = self.task_workspace.resolve_file_path(path)

        def scan() -> tuple[list[str], bool]:
            reference_ids: list[str] = []
            legacy_reference = False
            with target.open("r", encoding="utf-8", errors="ignore") as file:
                for line in file:
                    reference_ids.extend(parse_reference_tokens(line))
                    if re.search(r"(?<![A-Za-z0-9_])\(e[1-9][0-9]*\)(?![A-Za-z0-9_])", line):
                        legacy_reference = True
            return reference_ids, legacy_reference

        reference_ids, legacy_reference = await asyncio.to_thread(scan)
        if legacy_reference:
            return {
                "valid": True,
                "legacy_reference_unverified": True,
                "reference_ids": [],
                "source_cards": [],
            }
        offered = self._task_reference_catalog.offered_references()
        synthetic_text = " ".join(f"[[ref:{reference_id}]]" for reference_id in reference_ids)
        try:
            validated = validate_reference_tokens(synthetic_text, offered)
            source_cards = self._task_reference_catalog.source_cards(validated.get("reference_ids", []))
        except ValueError as error:
            return {"valid": False, "reason": str(error), "reference_ids": []}
        return {
            "valid": True,
            "legacy_reference_unverified": False,
            "reference_ids": list(validated.get("reference_ids", [])),
            "source_cards": source_cards,
        }

    async def _apply_terminal_task_workspace_retention(
        self,
        *,
        status: TaskWorkspaceTerminalStatus,
    ) -> TaskWorkspaceRetentionResult | None:
        """Close task-owned fallback files without touching the external root."""

        retained = list(getattr(self, "_terminal_retained_refs", []) or [])
        retained_content_version_ids = [
            str(ref.get("content_version_id") or "")
            for ref in retained
            if isinstance(ref, Mapping) and str(ref.get("content_version_id") or "")
        ]
        if retained_content_version_ids:
            await asyncio.to_thread(
                self.task_workspace._identity_catalog.retain_task_manifest,
                self.id,
                root_ids=retained_content_version_ids,
                state="accepted" if status == "completed" else "recovery",
                task_reference_catalog=self._task_reference_catalog.snapshot(),
            )
        close_status = "completed" if status == "completed" else "cancelled" if status == "cancelled" else "failed"
        closed = await self.task_workspace._close_execution_files(
            retained_refs=cast(Any, retained),
            status=close_status,
        )
        result = cast(TaskWorkspaceRetentionResult, closed)
        self.diagnostics["task_workspace_retention"] = {
            "status": result["status"],
            "retained_bytes": result["retained_bytes"],
            "deleted_bytes": result["deleted_bytes"],
            "diagnostics": DataFormatter.sanitize(result["diagnostics"]),
        }
        return result

    @classmethod
    def _trusted_terminal_refs(
        cls,
        *values: Any,
    ) -> list[TaskWorkspaceFileRef | RecordRef | RecordReference]:
        """Collect explicit structured TaskWorkspace refs from terminal carriers."""

        refs: list[TaskWorkspaceFileRef | RecordRef | RecordReference] = []
        seen: set[str] = set()

        def append_ref(
            value: Mapping[str, Any],
            *,
            identity: str,
        ) -> None:
            if identity in seen:
                return
            refs.append(
                cast(
                    TaskWorkspaceFileRef | RecordRef | RecordReference,
                    dict(DataFormatter.sanitize(dict(value))),
                )
            )
            seen.add(identity)

        def is_artifact_record_ref(value: Mapping[str, Any]) -> bool:
            return bool(str(value.get("id") or "")) and all(
                key in value for key in ("collection", "path", "sha256", "size", "source", "meta")
            )

        def is_artifact_reference_envelope(value: Mapping[str, Any]) -> bool:
            return (
                bool(str(value.get("record_store_id") or ""))
                and bool(str(value.get("record_id") or ""))
                and all(key in value for key in ("collection", "content_ref", "digest", "size"))
            )

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                if cls._task_workspace_artifact_ref_has_trusted_readback(value) and cls._is_trusted_task_workspace_artifact_ref(
                    value
                ):
                    append_ref(
                        value,
                        identity=(f"file:{value.get('path')}:{int(value.get('bytes') or 0)}:" f"{value.get('sha256')}"),
                    )
                    return
                if is_artifact_record_ref(value):
                    append_ref(value, identity=f"record:{value.get('id')}")
                    return
                if is_artifact_reference_envelope(value):
                    append_ref(
                        value,
                        identity=f"envelope:{value.get('record_store_id')}:{value.get('record_id')}",
                    )
                    return
                for key in ("file_refs", "artifact_refs"):
                    nested = value.get(key)
                    if isinstance(nested, Sequence) and not isinstance(nested, str | bytes | bytearray):
                        for item in nested:
                            collect(item)
                manifest = value.get("artifact_manifest")
                if isinstance(manifest, Mapping):
                    collect(manifest.get("file_refs"))
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    collect(item)

        for value in values:
            collect(value)
        return refs

    @classmethod
    def _trusted_terminal_file_refs(cls, *values: Any) -> list[dict[str, Any]]:
        """Return the file-backed subset used for compact user-facing pointers."""

        return [
            dict(ref)
            for ref in cls._trusted_terminal_refs(*values)
            if (
                str(ref.get("type") or "").strip().lower() == "file"
                or (
                    "id" not in ref
                    and "record_id" not in ref
                    and bool(str(ref.get("path") or "").strip())
                    and bool(str(ref.get("sha256") or "").strip())
                    and isinstance(ref.get("bytes"), int)
                )
            )
        ]

    @staticmethod
    def _task_workspace_artifact_manifest_path(manifest: Mapping[str, Any] | None) -> str:
        if isinstance(manifest, Mapping):
            for key in ("path", "output_path", "file_path"):
                value = str(manifest.get(key) or "").strip()
                if value:
                    return value
            deliverables = manifest.get("deliverables")
            if isinstance(deliverables, Sequence) and not isinstance(deliverables, str | bytes | bytearray):
                for item in deliverables:
                    if isinstance(item, Mapping):
                        value = str(item.get("path") or item.get("output_path") or "").strip()
                        if value:
                            return value
        return "final.md"

    @classmethod
    def _task_workspace_artifact_manifest_content(cls, manifest: Mapping[str, Any] | None) -> str:
        if not isinstance(manifest, Mapping):
            return ""
        for key in ("content", "markdown", "body", "text"):
            value = manifest.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        sections = manifest.get("sections")
        if not isinstance(sections, Sequence) or isinstance(sections, str | bytes | bytearray):
            return ""
        chunks: list[str] = []
        for section in sections:
            if isinstance(section, str):
                text = section.strip()
                if text and cls._manifest_section_string_is_body(text):
                    chunks.append(text)
                continue
            if not isinstance(section, Mapping):
                continue
            title = str(section.get("title") or section.get("name") or "").strip()
            body = ""
            for key in ("content", "markdown", "body", "text"):
                value = section.get(key)
                if isinstance(value, str) and value.strip():
                    body = value.strip()
                    break
            if not body:
                continue
            if title and not body.lstrip().startswith("#"):
                chunks.append(f"## {title}\n\n{body}")
            else:
                chunks.append(body)
        return "\n\n".join(chunks).strip()

    @staticmethod
    def _manifest_section_string_is_body(text: str) -> bool:
        """Treat short section-name strings as outlines, not artifact bodies."""

        stripped = text.strip()
        return bool("\n" in stripped or len(stripped) > 120 or stripped.startswith("#"))

    @classmethod
    def _task_workspace_artifact_manifest_needs_body(cls, manifest: Mapping[str, Any] | None) -> bool:
        if not isinstance(manifest, Mapping):
            return False
        if cls._task_workspace_artifact_manifest_content(manifest).strip():
            return False
        sections = manifest.get("sections")
        if isinstance(sections, Sequence) and not isinstance(sections, str | bytes | bytearray):
            return bool(sections)
        section_outline = manifest.get("section_outline")
        if isinstance(section_outline, Sequence) and not isinstance(section_outline, str | bytes | bytearray):
            return bool(section_outline)
        deliverables = manifest.get("deliverables")
        if isinstance(deliverables, Sequence) and not isinstance(deliverables, str | bytes | bytearray):
            return bool(deliverables)
        return False

    @staticmethod
    def _task_workspace_artifact_manifest_has_draftable_outline(manifest: Mapping[str, Any] | None) -> bool:
        if not isinstance(manifest, Mapping):
            return False
        for key in ("sections", "section_outline"):
            sections = manifest.get(key)
            if not isinstance(sections, Sequence) or isinstance(sections, str | bytes | bytearray):
                continue
            for section in sections:
                if isinstance(section, str) and section.strip():
                    return True
                if isinstance(section, Mapping):
                    for field in ("title", "summary", "intent", "description", "outline"):
                        value = section.get(field)
                        if isinstance(value, str) and value.strip():
                            return True
        return False

    @staticmethod
    def _task_workspace_artifact_retry_boundary_from_status(path: str, value: Any) -> dict[str, Any] | None:
        if not (
            (path == "$status" or path.endswith(".$status"))
            and isinstance(value, Mapping)
            and value.get("status") == "failed"
            and value.get("retry") is True
        ):
            return None
        return {
            "status": "retrying",
            "attempt_index": value.get("attempt_index"),
            "next_attempt_index": value.get("next_attempt_index"),
            "reason": str(value.get("reason") or "").strip(),
            "source": "structured_status",
        }

    @staticmethod
    def _task_workspace_artifact_public_delta_replay_marker(value: Any) -> dict[str, Any] | None:
        text = str(value or "")
        marker = _PUBLIC_DELTA_RETRY_MARKER_RE.match(text)
        if marker is None:
            return None
        reason = html.unescape(str(marker.group("body") or marker.group("label") or "")).strip()
        return {
            "marker": "retry",
            "reason": reason or "Retrying model request.",
            "source": "delta_replay_marker",
        }

    @staticmethod
    def _task_workspace_artifact_untrusted_refs(result: Mapping[str, Any], manifest: Mapping[str, Any] | None) -> list[Any]:
        refs: list[Any] = []
        raw_refs = result.get("file_refs")
        if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str | bytes | bytearray):
            refs.extend(raw_refs)
        if isinstance(manifest, Mapping):
            manifest_refs = manifest.get("file_refs")
            if isinstance(manifest_refs, Sequence) and not isinstance(manifest_refs, str | bytes | bytearray):
                refs.extend(manifest_refs)
        return refs

    @classmethod
    def _compact_task_workspace_artifact_manifest_for_hot_path(
        cls,
        manifest: Mapping[str, Any] | None,
        *,
        trusted_refs: list[dict[str, Any]],
        source: str,
    ) -> dict[str, Any]:
        compact = dict(manifest or {})
        for key in _WORKSPACE_ARTIFACT_CONTENT_KEYS:
            value = compact.pop(key, None)
            if isinstance(value, str) and value:
                compact.setdefault("omitted_content", []).append(
                    {
                        "field": key,
                        "chars": len(value),
                        "reason": "task_workspace_artifact_hot_path",
                    }
                )
        sections = compact.get("sections")
        if isinstance(sections, Sequence) and not isinstance(sections, str | bytes | bytearray):
            compact_sections: list[Any] = []
            for index, section in enumerate(sections):
                if isinstance(section, str):
                    compact_sections.append(
                        {
                            "index": index,
                            "content_omitted": True,
                            "chars": len(section),
                            "reason": "task_workspace_artifact_hot_path",
                        }
                    )
                    continue
                if not isinstance(section, Mapping):
                    compact_sections.append(DataFormatter.sanitize(section))
                    continue
                section_compact = dict(section)
                for key in _WORKSPACE_ARTIFACT_CONTENT_KEYS:
                    value = section_compact.pop(key, None)
                    if isinstance(value, str) and value:
                        section_compact.setdefault("omitted_content", []).append(
                            {
                                "field": key,
                                "chars": len(value),
                                "reason": "task_workspace_artifact_hot_path",
                            }
                        )
                compact_sections.append(section_compact)
            compact["sections"] = compact_sections
        if trusted_refs:
            ref = trusted_refs[0]
            compact.update(
                {
                    "path": ref.get("path"),
                    "bytes": ref.get("bytes"),
                    "sha256": ref.get("sha256"),
                    "file_refs": trusted_refs,
                    "source": source,
                }
            )
        return DataFormatter.sanitize(compact)

    @classmethod
    def _compact_task_workspace_artifact_result_for_hot_path(
        cls,
        result: dict[str, Any],
        *,
        content_key: str,
        content: str,
        trusted_refs: list[dict[str, Any]],
        preserve_fields: Sequence[str] = (),
    ) -> dict[str, Any]:
        if not trusted_refs:
            return result
        ref = trusted_refs[0]
        path = cls._task_workspace_artifact_display_path(ref.get("path"))
        replacement = f"TaskWorkspace artifact delivered at {path}; full content is available through file_refs/readback."
        omitted: list[dict[str, Any]] = []
        preserved = {str(field) for field in preserve_fields}
        for key in _WORKSPACE_ARTIFACT_RESULT_BODY_KEYS:
            if key in preserved:
                continue
            value = result.get(key)
            if isinstance(value, str) and value:
                result[key] = replacement
                omitted.append(
                    {
                        "field": key,
                        "chars": len(value),
                        "reason": "task_workspace_artifact_hot_path",
                    }
                )
        if content and content_key and content_key not in {item["field"] for item in omitted}:
            if cls._replace_task_workspace_artifact_nested_content(result, content_key, replacement):
                omitted.append(
                    {
                        "field": content_key,
                        "chars": len(content),
                        "reason": "task_workspace_artifact_hot_path",
                    }
                )
            else:
                omitted.append(
                    {
                        "field": content_key,
                        "chars": len(content),
                        "reason": "task_workspace_artifact_hot_path",
                    }
                )
        if omitted:
            result["task_workspace_artifact_content_omitted"] = omitted
        preview = str(ref.get("preview") or "")
        if preview:
            result["artifact_preview"] = preview
            result["artifact_preview_truncated"] = bool(ref.get("truncated"))
        return result

    @staticmethod
    def _replace_task_workspace_artifact_nested_content(result: dict[str, Any], content_key: str, replacement: str) -> bool:
        marker = re.match(r"\Aevidence\[(?P<index>\d+)\](?:\.(?P<field>[A-Za-z_][A-Za-z0-9_]*))?\Z", content_key)
        if marker is None:
            return False
        evidence = result.get("evidence")
        if not isinstance(evidence, list):
            return False
        index = int(marker.group("index"))
        if index < 0 or index >= len(evidence):
            return False
        field = marker.group("field")
        if field is None:
            if not isinstance(evidence[index], str):
                return False
            evidence[index] = replacement
            return True
        item = evidence[index]
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get(field), str):
            return False
        item[field] = replacement
        return True

    @classmethod
    def _handoff_task_workspace_artifact_remaining_work_to_verifier(
        cls,
        result: dict[str, Any],
        *,
        diagnostics: list[Any],
        path: str,
        source: str,
        content_key: str,
    ) -> dict[str, Any] | None:
        remaining_work = cls._normalize_string_list(result.get("remaining_work"))
        if not remaining_work:
            return None
        handoff = {
            "status": "handed_to_terminal_verification",
            "path": path,
            "content_key": content_key,
            "remaining_work": remaining_work,
            "reason": (
                "Trusted TaskWorkspace write/readback materialized the candidate artifact; "
                "terminal verification should judge remaining sufficiency."
            ),
        }
        result["remaining_work"] = []
        result["ready_for_final_verification"] = True
        result["task_workspace_artifact_remaining_work_handoff"] = DataFormatter.sanitize(handoff)
        diagnostics.append(
            {
                "code": "agent_task.task_workspace_artifact.remaining_work_handed_to_verifier",
                "message": (
                    "TaskWorkspace artifact content was written and read back while the work unit still reported "
                    "remaining work; the stale work-unit continuation was handed to terminal verification."
                ),
                "path": path,
                "source": source,
                "content_key": content_key,
                "remaining_work": remaining_work,
            }
        )
        return handoff

    @staticmethod
    def _task_workspace_artifact_content_is_complete_body(content: str) -> bool:
        text = content.strip()
        if not text:
            return False
        if text.startswith("#"):
            return True
        lowered = text[:128].lower()
        if lowered.startswith("<!doctype") or lowered.startswith("<html"):
            return True
        return bool("\n\n" in text and len(text) > 800)

    @classmethod
    def _task_workspace_artifact_evidence_content_candidates(
        cls,
        result: Mapping[str, Any],
        *,
        manifest_path: str,
    ) -> list[tuple[str, str]]:
        evidence = result.get("evidence")
        if not isinstance(evidence, Sequence) or isinstance(evidence, str | bytes | bytearray):
            return []
        candidates: list[tuple[str, str]] = []
        for index, item in enumerate(evidence):
            if isinstance(item, str):
                body = cls._task_workspace_artifact_body_from_evidence_text(item, manifest_path=manifest_path)
                if body:
                    candidates.append((f"evidence[{index}]", body))
                continue
            if not isinstance(item, Mapping):
                continue
            item_path = str(item.get("path") or item.get("artifact_path") or item.get("output_path") or "").strip()
            item_kind = str(item.get("kind") or item.get("type") or item.get("role") or "").strip().lower()
            item_declares_artifact = bool(
                item_path == manifest_path
                or "artifact" in item_kind
                or "deliverable" in item_kind
                or item.get("is_artifact_body") is True
            )
            for key in (*_WORKSPACE_ARTIFACT_RESULT_BODY_KEYS, *_WORKSPACE_ARTIFACT_CONTENT_KEYS):
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                key_declares_artifact = key.startswith("artifact_") or key in {
                    "candidate_final_result",
                    "final_result",
                }
                body = cls._task_workspace_artifact_body_from_evidence_text(
                    value,
                    manifest_path=manifest_path,
                    allow_bare_markdown=item_declares_artifact or key_declares_artifact,
                )
                if body:
                    candidates.append((f"evidence[{index}].{key}", body))
        return candidates

    @staticmethod
    def _task_workspace_artifact_body_from_evidence_text(
        value: str,
        *,
        manifest_path: str,
        allow_bare_markdown: bool = False,
    ) -> str:
        text = value.strip()
        if not text:
            return ""
        if allow_bare_markdown and text.startswith("#"):
            return text
        lines = text.splitlines()
        first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
        if first_index is None:
            return ""
        first_line = lines[first_index].strip()
        if not first_line.endswith(":"):
            return ""
        label = first_line[:-1].strip().lower()
        path = manifest_path.strip().lower()
        path_name = Path(path).name.lower() if path else ""
        label_declares_artifact = any(token in label for token in ("artifact", "deliverable", "markdown", "body"))
        if path and path in label:
            label_declares_artifact = True
        if path_name and path_name in label:
            label_declares_artifact = True
        if not label_declares_artifact:
            return ""
        body = "\n".join(lines[first_index + 1 :]).strip()
        if not body:
            return ""
        if body.startswith("#"):
            return body
        return ""

    def _task_workspace_artifact_delivery_mode(self, result: Any, *, context: Any = None) -> str:
        if not isinstance(result, Mapping):
            return ""
        if context is not None and not (
            self._taskboard_context_final_task_workspace_deliverables(context)
            or (self._required_task_workspace_deliverables() and self._taskboard_context_card_is_leaf(context))
        ):
            return ""
        manifest = result.get("artifact_manifest")
        if isinstance(manifest, Mapping) and manifest:
            return "sectioned_task_workspace_artifact"
        keys = ["artifact_markdown", "artifact_html"]
        if context is not None and (
            self._taskboard_context_final_task_workspace_deliverables(context)
            or (
                self._required_task_workspace_deliverables()
                and self._taskboard_context_card_is_leaf(context)
            )
        ):
            keys.extend(("candidate_final_result", "final_result"))
        for key in keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return "task_workspace_artifact"
        return ""

    @staticmethod
    def _taskboard_context_card_is_leaf(context: Any) -> bool:
        card = getattr(context, "card", None)
        card_id = str(getattr(card, "id", "") or "").strip()
        if not card_id:
            return False
        revision = getattr(context, "revision", None)
        graph = getattr(revision, "graph", None)
        cards = list(getattr(graph, "cards", []) or [])
        if not cards:
            return True
        depended_on: set[str] = set()
        for item in cards:
            depended_on.update(str(dep_id) for dep_id in getattr(item, "depends_on", ()) or ())
        return card_id not in depended_on

    def _taskboard_terminal_candidate_path(
        self,
        context: Any,
        target_path: str,
    ) -> str:
        target = self.task_workspace.resolve_path(target_path)
        relative_target = target.relative_to(self.task_workspace.root).as_posix()
        if relative_target == ".agently" or relative_target.startswith(".agently/"):
            raise TaskWorkspacePolicyError(
                "Terminal deliverables cannot target TaskWorkspace private state."
            )
        card = getattr(context, "card", None)
        card_id = str(getattr(card, "id", "") or "card").strip() or "card"
        safe_card_id = "".join(
            ch if ch.isalnum() or ch in {"-", "_", "."} else "-"
            for ch in card_id
        ) or "card"
        return (
            f"working/taskboard/{safe_card_id}/terminal-candidates/"
            f"{relative_target}"
        )

    def _prepare_taskboard_task_workspace_artifact_delivery(
        self,
        card_output: Any,
        context: Any,
        *,
        deliverable_mode: str | None,
        prefer_stream_draft: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        plan: dict[str, Any] = {"deliverable_mode": str(deliverable_mode or "").strip()}
        if prefer_stream_draft:
            plan["prefer_stream_draft"] = True
        if not plan["deliverable_mode"] or not isinstance(card_output, Mapping):
            return card_output, plan
        required_paths = {str(path or "").strip() for path in self._required_task_workspace_deliverables()}
        final_card_paths = [
            path
            for path in self._taskboard_context_final_task_workspace_deliverables(context)
            if not required_paths or path in required_paths
        ]
        manifest = card_output.get("artifact_manifest")
        manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
        requested_path = self._task_workspace_artifact_manifest_path(manifest_dict)
        has_remaining_work = self._has_remaining_work(
            card_output.get("remaining_work")
        ) or self._has_remaining_work(card_output.get("gaps"))
        status = str(card_output.get("status") or "").strip().lower()
        next_action = (
            str(card_output.get("next_board_action") or "")
            .strip()
            .lower()
            .replace("-", "_")
        )
        completed_leaf_delivery_handoff = bool(
            status == "completed"
            and card_output.get("sufficient") is True
            and next_action
            not in {
                "readback",
                "needs_readback",
                "repair",
                "patch",
                "block",
                "stop",
            }
        )
        leaf_can_stage_terminal_candidate = bool(
            self._taskboard_context_card_is_leaf(context)
            and (not has_remaining_work or completed_leaf_delivery_handoff)
        )
        terminal_target = ""
        if final_card_paths:
            terminal_target = (
                requested_path
                if requested_path in final_card_paths
                else final_card_paths[0]
            )
        elif (
            required_paths
            and leaf_can_stage_terminal_candidate
            and (requested_path in required_paths or not manifest_dict)
        ):
            terminal_target = (
                requested_path if requested_path in required_paths
                else self._required_task_workspace_deliverables()[0]
            )
        if terminal_target:
            staging_path = self._taskboard_terminal_candidate_path(
                context,
                terminal_target,
            )
            manifest_dict["path"] = staging_path
            result = dict(card_output)
            result["artifact_manifest"] = manifest_dict
            plan.update(
                {
                    "terminal_target_path": terminal_target,
                    "terminal_candidate_path": staging_path,
                }
            )
            diagnostics: list[Any] = []
            raw_diagnostics = result.get("diagnostics")
            if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                diagnostics.extend(raw_diagnostics)
            elif raw_diagnostics:
                diagnostics.append(raw_diagnostics)
            diagnostics.append(
                {
                    "code": "taskboard.task_workspace_artifact.terminal_candidate_staged",
                    "message": (
                        "A final TaskBoard card writes a verifier candidate; "
                        "the required target changes only after terminal acceptance."
                    ),
                    "requested_path": requested_path,
                    "terminal_candidate_path": staging_path,
                    "terminal_target_path": terminal_target,
                }
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return result, plan
        if leaf_can_stage_terminal_candidate:
            return card_output, plan

        manifest = card_output.get("artifact_manifest")
        manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
        requested_path = self._task_workspace_artifact_manifest_path(manifest_dict)

        card = getattr(context, "card", None)
        card_id = str(getattr(card, "id", "") or "card").strip() or "card"
        safe_card_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in card_id) or "card"
        file_name = Path(requested_path).name or "artifact.md"
        relocated_path = f"working/taskboard/{safe_card_id}/{file_name}"
        manifest_dict["path"] = relocated_path

        result = dict(card_output)
        result["artifact_manifest"] = manifest_dict
        diagnostics: list[Any] = []
        raw_diagnostics = result.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(raw_diagnostics)
        elif raw_diagnostics:
            diagnostics.append(raw_diagnostics)
        diagnostics.append(
            {
                "code": "taskboard.task_workspace_artifact.final_path_relocated_for_intermediate_card",
                "message": "A non-terminal TaskBoard card cannot write a required final deliverable path.",
                "card_id": card_id,
                "requested_path": requested_path,
                "relocated_path": relocated_path,
                "remaining_work_present": has_remaining_work,
            }
        )
        result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return result, plan

    @classmethod
    def _taskboard_context_final_task_workspace_deliverables(cls, context: Any) -> list[str]:
        card = getattr(context, "card", None)
        metadata = getattr(card, "metadata", None)
        if not isinstance(metadata, Mapping):
            return []
        return cls._normalize_string_list(metadata.get("final_task_workspace_deliverables"))

    def _taskboard_task_workspace_delivery_policy(self, context: Any) -> dict[str, Any]:
        required_paths = self._required_task_workspace_deliverables()
        final_card_paths = [
            path for path in self._taskboard_context_final_task_workspace_deliverables(context) if path in required_paths
        ]
        can_stage_required = bool(
            required_paths and (final_card_paths or self._taskboard_context_card_is_leaf(context))
        )
        authorized_targets = final_card_paths or (
            required_paths if can_stage_required else []
        )
        terminal_candidate_paths = {
            target: self._taskboard_terminal_candidate_path(context, target)
            for target in authorized_targets
        }
        return {
            "schema_version": "agent_task_taskboard_task_workspace_delivery/v1",
            "required_deliverables": required_paths,
            "authorized_terminal_candidate_paths": list(
                terminal_candidate_paths.values()
            ),
            "terminal_target_mappings": terminal_candidate_paths,
            "can_stage_required_deliverables": can_stage_required,
            "can_write_required_deliverables": False,
            "policy": (
                "Write final or framework-marked repair/continuation output only to the offered terminal candidate "
                "path. The required target path is protected until verifier acceptance and host promotion. Use "
                "working refs for intermediate evidence cards."
            ),
        }

    def _append_task_workspace_artifact_meta(
        self,
        execution_meta: Mapping[str, Any] | None,
        refs: list[dict[str, Any]],
    ) -> None:
        if not refs or not isinstance(execution_meta, dict):
            return
        logs = execution_meta.setdefault("logs", {})
        if not isinstance(logs, dict):
            logs = {}
            execution_meta["logs"] = logs
        artifact_refs = logs.setdefault("artifact_refs", [])
        if not isinstance(artifact_refs, list):
            artifact_refs = []
            logs["artifact_refs"] = artifact_refs
        artifact_refs.extend(DataFormatter.sanitize(refs))
        task_workspace_refs = execution_meta.setdefault("task_workspace_refs", {})
        if not isinstance(task_workspace_refs, dict):
            task_workspace_refs = {}
            execution_meta["task_workspace_refs"] = task_workspace_refs
        task_workspace_refs.setdefault("agent_task_artifacts", []).extend(
            DataFormatter.sanitize(refs)
        )
        logs["task_workspace_refs"] = task_workspace_refs
        evidence_items = [
            self._task_workspace_artifact_readback_evidence_item(ref)
            for ref in refs
            if isinstance(ref, Mapping)
        ]
        self._append_execution_meta_evidence_items(execution_meta, evidence_items)

    def _append_execution_meta_evidence_items(
        self,
        execution_meta: Mapping[str, Any] | None,
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> None:
        if not evidence_items or not isinstance(execution_meta, dict):
            return
        blocks = execution_meta.setdefault("blocks", {})
        if not isinstance(blocks, dict):
            blocks = {}
            execution_meta["blocks"] = blocks
        evidence = blocks.setdefault("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
            blocks["evidence"] = evidence
        ledger_items = evidence.setdefault("evidence_items", [])
        if not isinstance(ledger_items, list):
            ledger_items = []
            evidence["evidence_items"] = ledger_items
        seen = {
            str(item.get("evidence_id") or item.get("id") or "")
            for item in ledger_items
            if isinstance(item, Mapping)
        }
        # Allocate task-scoped identities only after the EvidenceEnvelope has
        # normalized provider/action vocabulary (for example
        # partial_success -> ok and complete -> full).  Allocating first and
        # normalizing on a later ledger render makes the same canonical item
        # appear to change immutable status/body-state fields across revisions.
        normalized_items = evidence_envelope_from_value(
            {"evidence_items": evidence_items}
        ).evidence_items
        for item in normalized_items:
            canonical_item = self._task_references().add_evidence(item)
            evidence_id = str(canonical_item.get("evidence_id") or canonical_item.get("id") or "").strip()
            if evidence_id and evidence_id in seen:
                continue
            if evidence_id:
                seen.add(evidence_id)
            ledger_items.append(DataFormatter.sanitize(canonical_item))

    async def _task_workspace_artifact_acceptance_locator_evidence_items(
        self,
        *,
        ref: Mapping[str, Any],
        result: Mapping[str, Any],
        manifest: Mapping[str, Any],
        source: str,
        content: str = "",
        card_context: Any | None = None,
    ) -> list[dict[str, Any]]:
        path = str(ref.get("path") or manifest.get("path") or "").strip()
        if not path:
            return []
        text = str(content or "")
        if not text:
            try:
                declared_bytes = self._coerce_non_negative_int(ref.get("bytes"))
                max_bytes = (
                    declared_bytes + 1
                    if 0 < declared_bytes <= _WORKSPACE_ARTIFACT_LOCATOR_SCAN_BYTES
                    else _WORKSPACE_ARTIFACT_LOCATOR_SCAN_BYTES
                )
                readback = await self.task_workspace.read_file(path, max_bytes=max_bytes)
            except Exception:
                text = str(ref.get("preview") or "")
            else:
                text = str(readback.get("content") or ref.get("preview") or "")
        acceptance_points = [
            *collect_acceptance_points(result),
            *self._task_workspace_artifact_acceptance_points_from_taskboard_context(card_context),
            *self._task_workspace_artifact_acceptance_points_from_output_contracts(path),
        ]
        artifact_evidence_id = self._task_workspace_artifact_readback_evidence_item(ref).get("id", "")
        locator_items = build_task_workspace_artifact_acceptance_locator_items(
            path=path,
            source=source,
            text=text,
            manifest=manifest,
            acceptance_points=acceptance_points,
            success_criteria=getattr(self, "success_criteria", ()),
            source_evidence_ids=self._artifact_readback_evidence_ids([ref]),
            artifact_evidence_id=str(artifact_evidence_id or ""),
        )
        identity = {
            field: ref.get(field)
            for field in ("locator_id", "content_version_id", "snapshot_id", "sha256")
            if ref.get(field) not in (None, "", [], {})
        }
        for item in locator_items:
            item.update(DataFormatter.sanitize(identity))
            provenance = item.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
                item["provenance"] = provenance
            provenance.update(DataFormatter.sanitize(identity))
        return DataFormatter.sanitize(locator_items)

    @staticmethod
    def _task_workspace_artifact_acceptance_points_from_taskboard_context(card_context: Any | None) -> list[dict[str, Any]]:
        card = getattr(card_context, "card", None)
        if card is None:
            return []
        card_id = str(getattr(card, "id", "") or "card").strip() or "card"
        points: list[dict[str, Any]] = []
        objective = str(getattr(card, "objective", "") or "").strip()
        if objective:
            points.append(
                {
                    "criterion_id": f"taskboard:{card_id}:objective",
                    "criterion": objective,
                    "source": "taskboard_card",
                }
            )
        for index, required_output in enumerate(getattr(card, "required_outputs", ()) or ()):
            text = str(required_output or "").strip()
            if not text:
                continue
            points.append(
                {
                    "criterion_id": f"taskboard:{card_id}:required_output:{index}",
                    "criterion": text,
                    "source": "taskboard_card",
                }
            )
        evidence_contract = getattr(card, "evidence_contract", None)
        if isinstance(evidence_contract, Mapping):
            points.extend(collect_acceptance_points(evidence_contract))
        return DataFormatter.sanitize(points)

    def _task_workspace_artifact_acceptance_points_from_output_contracts(self, path: str) -> list[dict[str, Any]]:
        artifact_path = str(path or "").strip()
        normalized_artifact_path = PurePosixPath(artifact_path).as_posix() if artifact_path else ""
        options = getattr(self, "options", None)
        if not isinstance(options, Mapping):
            return []
        points: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def slug(value: str) -> str:
            text = "-".join(re.findall(r"[a-z0-9]+", value.casefold()))
            return text[:80] or "section"

        def add_section(section: Any, *, source_key: str, index: int) -> None:
            if isinstance(section, str):
                title = section.strip()
            elif isinstance(section, Mapping):
                title = ""
                for key in ("title", "name", "heading", "id"):
                    value = str(section.get(key) or "").strip()
                    if value:
                        title = value
                        break
            else:
                title = ""
            if not title:
                return
            key = (artifact_path, title.casefold())
            if key in seen:
                return
            seen.add(key)
            points.append(
                {
                    "criterion_id": f"output_contract:{source_key}:section:{index}:{slug(title)}",
                    "criterion": f"Output contract section present: {title}",
                    "expected_anchor": title,
                    "artifact_path": artifact_path,
                    "source": "output_contract",
                }
            )

        def contract_deliverable_paths(value: Mapping[str, Any]) -> list[str]:
            paths: list[str] = []
            raw_deliverables = value.get("deliverables")
            if not isinstance(raw_deliverables, Sequence) or isinstance(raw_deliverables, str | bytes | bytearray):
                return paths
            for deliverable in raw_deliverables:
                if isinstance(deliverable, str):
                    candidate = deliverable.strip()
                elif isinstance(deliverable, Mapping):
                    candidate = str(deliverable.get("path") or "").strip()
                else:
                    candidate = ""
                if not candidate:
                    continue
                normalized = PurePosixPath(candidate).as_posix()
                if normalized not in paths:
                    paths.append(normalized)
            return paths

        def collect_contract(value: Any, *, source_key: str) -> None:
            if not isinstance(value, Mapping):
                return
            deliverable_paths = contract_deliverable_paths(value)
            if deliverable_paths and normalized_artifact_path not in deliverable_paths:
                return
            sections = value.get("sections")
            if not isinstance(sections, Sequence) or isinstance(sections, str | bytes | bytearray):
                return
            for index, section in enumerate(sections):
                add_section(section, source_key=source_key, index=index)

        collect_contract(self._agent_task_option("output_contract", None), source_key="task_options")
        execution_prompt = self._execution_prompt_context()
        collect_contract(execution_prompt.get("output_contract"), source_key="execution_prompt")
        prompt_input = execution_prompt.get("input")
        if isinstance(prompt_input, Mapping):
            collect_contract(prompt_input.get("output_contract"), source_key="prompt_input")
            case = prompt_input.get("case")
            if isinstance(case, Mapping):
                collect_contract(case.get("output_contract"), source_key="case")
        return DataFormatter.sanitize(points)

    @classmethod
    def _task_workspace_artifact_readback_evidence_item(cls, ref: Mapping[str, Any]) -> dict[str, Any]:
        path = str(ref.get("path") or "").strip()
        source = str(ref.get("source") or "agent_task.task_workspace_artifact").strip()
        truncated = bool(ref.get("truncated"))
        preview = str(ref.get("preview") or "")
        snapshot_key = str(
            ref.get("content_version_id")
            or ref.get("snapshot_id")
            or "unversioned"
        ).strip()
        evidence_id = cls._task_workspace_artifact_evidence_id(
            "task_workspace_artifact_readback",
            path,
            f"{source}:{snapshot_key}",
        )
        item: dict[str, Any] = {
            "id": evidence_id,
            "kind": "task_workspace_artifact.readback",
            "status": "ok",
            "raw_status": "read",
            "body_state": "truncated" if truncated else "full",
            "path": path,
            "sha256": str(ref.get("sha256") or ""),
            "bytes": ref.get("bytes"),
            "read_bytes": ref.get("read_bytes"),
            "media_type": ref.get("media_type"),
            "content_kind": ref.get("content_kind"),
            "source": source,
            "provenance": {
                "source": source,
                "path": path,
                "sha256": str(ref.get("sha256") or ""),
                "handler_id": ref.get("handler_id"),
            },
            "supports": {
                "content": True,
                "unavailability": False,
                "ref_pointer": False,
            },
        }
        for field in ("locator_id", "content_version_id", "snapshot_id"):
            if ref.get(field) not in (None, "", [], {}):
                item[field] = DataFormatter.sanitize(ref.get(field))
                item["provenance"][field] = DataFormatter.sanitize(ref.get(field))
        if preview:
            item["body"] = preview
        return DataFormatter.sanitize(item)

    @classmethod
    def _task_workspace_artifact_acceptance_coverage_evidence_item(
        cls,
        *,
        path: str,
        source: str,
        locator_items: Sequence[Mapping[str, Any]],
        targeted_readback_items: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        artifact_path = str(path or "").strip()
        if not artifact_path or not locator_items:
            return {}
        readbacks_by_locator = {
            str((item.get("provenance") or {}).get("source_evidence_id") or "").strip(): item
            for item in targeted_readback_items
            if isinstance(item, Mapping) and isinstance(item.get("provenance"), Mapping)
        }
        required_locators = [
            item
            for item in locator_items
            if isinstance(item, Mapping) and str(item.get("requirement_level") or "").strip() == "required"
        ]
        effective_locators = required_locators or [item for item in locator_items if isinstance(item, Mapping)]
        ok_locators = [item for item in effective_locators if str(item.get("status") or "").strip().lower() == "ok"]
        missing_locators = [
            item for item in effective_locators if str(item.get("status") or "").strip().lower() != "ok"
        ]
        readback_covered = [
            item
            for item in ok_locators
            if str(item.get("id") or "").strip() in readbacks_by_locator
            and str(readbacks_by_locator[str(item.get("id") or "").strip()].get("status") or "") == "ok"
        ]
        status = "ok" if ok_locators and not missing_locators and len(readback_covered) == len(ok_locators) else "empty"
        source_evidence_ids: list[str] = []
        lines = [
            f"Acceptance coverage for {artifact_path}.",
            f"Required acceptance points: {len(required_locators)}.",
            f"Located acceptance points: {len(ok_locators)}.",
            f"Targeted readbacks: {len(readback_covered)}.",
        ]
        if status == "ok":
            lines.append("All required acceptance points for this artifact have bounded targeted readback evidence.")
        else:
            lines.append("Required acceptance coverage is incomplete for this artifact.")
        for locator in effective_locators[:32]:
            locator_id = str(locator.get("id") or "").strip()
            readback = readbacks_by_locator.get(locator_id)
            readback_id = str(readback.get("id") or "").strip() if isinstance(readback, Mapping) else ""
            if locator_id:
                source_evidence_ids.append(locator_id)
            if readback_id:
                source_evidence_ids.append(readback_id)
            label = str(
                locator.get("heading")
                or locator.get("anchor_text")
                or locator.get("claim")
                or locator.get("criterion_id")
                or ""
            ).strip()
            if label:
                lines.append(
                    f"- {label}: locator_status={locator.get('status')}; "
                    f"readback_evidence_id={readback_id or 'missing'}"
                )
        basename = artifact_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        evidence_id = cls._task_workspace_artifact_evidence_id(
            "task_workspace_artifact_acceptance_coverage", artifact_path, source
        )
        item = {
            "id": evidence_id,
            "kind": "task_workspace_artifact.acceptance_coverage",
            "status": status,
            "raw_status": "covered" if status == "ok" else "incomplete",
            "body_state": "bounded" if status == "ok" else "ref_only",
            "path": artifact_path,
            "aliases": [
                artifact_path,
                basename,
                f"{artifact_path} acceptance coverage",
                f"{artifact_path} required acceptance points",
                "TaskWorkspace artifact acceptance coverage",
                "all required acceptance points",
                "all required output contract sections",
            ],
            "source": source,
            "provenance": {
                "source": source,
                "path": artifact_path,
                "source_evidence_ids": list(dict.fromkeys(source_evidence_ids)),
            },
            "supports": {
                "content": status == "ok",
                "unavailability": status != "ok",
                "ref_pointer": False,
            },
            "body": "\n".join(lines),
        }
        if status != "ok":
            item["diagnostics"] = [
                {
                    "code": "agent_task.task_workspace_artifact.acceptance_coverage_incomplete",
                    "message": "TaskWorkspace artifact acceptance locators or targeted readbacks are incomplete.",
                    "missing_locator_count": len(missing_locators),
                    "located_count": len(ok_locators),
                    "targeted_readback_count": len(readback_covered),
                }
            ]
        return DataFormatter.sanitize(item)

    @classmethod
    def _task_workspace_artifact_failure_evidence_item(
        cls,
        *,
        path: str,
        source: str,
        code: str,
        message: str,
        readback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": cls._task_workspace_artifact_evidence_id("task_workspace_artifact_readback_failed", path, code or source),
            "kind": "task_workspace_artifact.readback",
            "status": "failed",
            "raw_status": code or "failed",
            "body_state": "ref_only",
            "path": path,
            "source": source,
            "provenance": {"source": source, "path": path},
            "supports": {"content": False, "unavailability": True, "ref_pointer": False},
            "diagnostics": [{"code": code, "message": message, "readback": DataFormatter.sanitize(readback or {})}],
        }
        return DataFormatter.sanitize(item)

    @staticmethod
    def _task_workspace_artifact_evidence_id(prefix: str, path: str, source: str) -> str:
        raw = f"{ prefix }:{ source }:{ path }"
        return "".join(ch if ch.isalnum() or ch in "._:-/" else "_" for ch in raw)[:240]

    @staticmethod
    def _task_workspace_artifact_readback_missing_diagnostic(
        *,
        code: str,
        path: str,
        source: str,
        message: str,
        error: Exception | None = None,
        readback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        diagnostic: dict[str, Any] = {
            "code": code,
            "message": message,
            "path": path,
            "source": source,
        }
        if error is not None:
            diagnostic["error"] = {
                "type": error.__class__.__name__,
                "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
            }
        if readback is not None:
            diagnostic["readback"] = DataFormatter.sanitize(dict(readback))
        return diagnostic

    @staticmethod
    def _task_workspace_artifact_ref_has_trusted_readback(ref: Mapping[str, Any]) -> bool:
        path = str(ref.get("path") or "").strip()
        sha256 = str(ref.get("sha256") or "").strip()
        raw_byte_count = ref.get("bytes") if "bytes" in ref else None
        byte_count = raw_byte_count if isinstance(raw_byte_count, int) and not isinstance(raw_byte_count, bool) else -1
        return bool(path and sha256 and byte_count >= 0)

    @classmethod
    def _artifact_readback_evidence_ids(cls, refs: Any) -> list[str]:
        if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
            return []
        evidence_ids: list[str] = []
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            if not cls._task_workspace_artifact_ref_has_trusted_readback(ref):
                continue
            path = str(ref.get("path") or "").strip()
            evidence_id = path
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        return evidence_ids

    @staticmethod
    def _task_workspace_artifact_candidate_path_is_local(value: Any) -> bool:
        text = str(value or "").strip()
        if not text or "\n" in text or len(text) > 512:
            return False
        if re.match(r"\A[A-Za-z][A-Za-z0-9+.-]*://", text):
            return False
        if text.startswith(("mailto:", "tel:", "urn:")):
            return False
        return True

    @classmethod
    def _task_workspace_artifact_successful_action_file_paths(cls, execution_meta: Mapping[str, Any] | None) -> list[str]:
        if not isinstance(execution_meta, Mapping):
            return []
        paths: list[str] = []

        def add_path(value: Any) -> None:
            text = str(value or "").strip()
            if not cls._task_workspace_artifact_candidate_path_is_local(text):
                return
            if text not in paths:
                paths.append(text)

        def collect_file_refs(value: Any) -> None:
            if isinstance(value, Mapping):
                refs = value.get("file_refs")
                if isinstance(refs, Sequence) and not isinstance(refs, str | bytes | bytearray):
                    for ref in refs:
                        if not isinstance(ref, Mapping):
                            continue
                        role = str(ref.get("role") or "").strip().lower()
                        if role and role not in {"output", "task_workspace_artifact", "artifact", "file"}:
                            continue
                        add_path(ref.get("path") or ref.get("output_path") or ref.get("file_path"))
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    if isinstance(item, Mapping):
                        role = str(item.get("role") or "").strip().lower()
                        if role and role not in {"output", "task_workspace_artifact", "artifact", "file"}:
                            continue
                        add_path(item.get("path") or item.get("output_path") or item.get("file_path"))

        def mapping_reports_success(value: Any) -> bool:
            return isinstance(value, Mapping) and (
                value.get("ok") is True
                or str(value.get("status") or "").strip().lower() in {"success", "succeeded", "ok"}
            )

        def collect_result_path(record: Mapping[str, Any], value: Any) -> None:
            if not isinstance(value, Mapping):
                return
            action_id = str(record.get("action_id") or record.get("id") or record.get("name") or "").strip().lower()
            mode = str(value.get("mode") or value.get("operation") or "").strip().lower()
            is_file_materializer = action_id in {"write_file", "edit_file", "apply_patch"} or mode in {
                "write",
                "edit",
                "apply_patch",
                "replace",
                "append",
                "create",
            }
            candidate_path = value.get("path") or value.get("output_path") or value.get("file_path")
            has_file_metadata = any(
                value.get(key) not in (None, "", [], {})
                for key in (
                    "filename",
                    "file_name",
                    "size",
                    "bytes",
                    "sha256",
                    "media_type",
                    "mime_type",
                    "content_type",
                )
            )
            if not (is_file_materializer or (candidate_path and has_file_metadata)):
                return
            add_path(candidate_path)

        success_statuses = {"success", "succeeded", "ok", "completed", "partial_success"}
        for record in cls._collect_execution_action_records(execution_meta):
            if not isinstance(record, Mapping):
                continue
            status = str(record.get("status") or "").strip().lower()
            result_preview = record.get("result_preview")
            output_summary = record.get("output_summary")
            if status not in success_statuses and not (
                mapping_reports_success(result_preview) or mapping_reports_success(output_summary)
            ):
                continue
            collect_file_refs(record.get("file_refs"))
            collect_file_refs(result_preview)
            collect_file_refs(output_summary)
            collect_result_path(record, result_preview)
            collect_result_path(record, output_summary)
        return paths

    @classmethod
    def _taskboard_dependency_trusted_artifact_paths(cls, card_context: Any | None) -> list[str]:
        dependency_results = getattr(card_context, "dependency_results", None)
        if not isinstance(dependency_results, Mapping):
            return []
        paths: list[str] = []
        for raw_result in dependency_results.values():
            try:
                result = TaskBoardCardResult.from_value(raw_result)
            except (TypeError, ValueError):
                continue
            for ref in (*result.artifact_refs, *result.file_refs):
                if not isinstance(ref, Mapping):
                    continue
                if not cls._is_trusted_task_workspace_artifact_ref(ref):
                    continue
                if not cls._task_workspace_artifact_ref_has_trusted_readback(ref):
                    continue
                path = str(ref.get("path") or "").strip()
                if not cls._task_workspace_artifact_candidate_path_is_local(path):
                    continue
                if path not in paths:
                    paths.append(path)
        return paths

    @classmethod
    def _task_workspace_artifact_ordered_action_candidate_paths(
        cls,
        action_paths: Sequence[str],
        *,
        manifest: Mapping[str, Any],
        manifest_path: str,
        required_paths: Sequence[str],
    ) -> list[str]:
        remaining = [str(path or "").strip() for path in action_paths if str(path or "").strip()]
        ordered: list[str] = []

        def promote(path: Any) -> None:
            text = str(path or "").strip()
            if text and text in remaining and text not in ordered:
                ordered.append(text)

        promote(manifest_path)
        for path in required_paths:
            promote(path)
        deliverables = manifest.get("deliverables")
        if isinstance(deliverables, Sequence) and not isinstance(deliverables, str | bytes | bytearray):
            for item in deliverables:
                if isinstance(item, Mapping):
                    promote(item.get("path") or item.get("output_path") or item.get("file_path"))
                else:
                    promote(item)
        for path in remaining:
            if path not in ordered:
                ordered.append(path)
        return ordered

    def _task_workspace_artifact_ref_from_readback(
        self,
        read_result: Mapping[str, Any],
        *,
        path: str,
        source: str,
    ) -> dict[str, Any]:
        byte_count = int(read_result.get("bytes") or 0)
        return {
            "type": "file",
            "path": str(read_result.get("path") or path),
            "task_workspace_id": self.task_workspace.task_workspace_id,
            "execution_id": self.task_workspace.execution_id,
            "size": byte_count,
            "available": bool(read_result.get("exists", True)),
            "bytes": byte_count,
            "sha256": str(read_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": str(read_result.get("content_kind") or "text"),
            "role": "task_workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "truncated": bool(read_result.get("truncated")),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "handler_id": read_result.get("handler_id"),
        }

    async def _adopt_task_workspace_artifact_from_action_readback(
        self,
        result: dict[str, Any],
        *,
        manifest_dict: dict[str, Any],
        path: str,
        deliverable_mode: str,
        content_key: str,
        diagnostics: list[Any],
        execution_meta: Mapping[str, Any] | None,
        source: str,
        card_context: Any | None = None,
        exact_manifest_path: bool = False,
        trusted_candidate_paths: Sequence[str] | None = None,
        candidate_source: str = "execution_meta.action_logs",
    ) -> dict[str, Any] | None:
        if (
            deliverable_mode not in {"task_workspace_artifact", "sectioned_task_workspace_artifact"}
            and not str(path or "").strip()
        ):
            return None
        action_paths = (
            [
                str(candidate_path).strip()
                for candidate_path in trusted_candidate_paths
                if self._task_workspace_artifact_candidate_path_is_local(candidate_path)
            ]
            if trusted_candidate_paths is not None
            else self._task_workspace_artifact_successful_action_file_paths(execution_meta)
        )
        if not action_paths:
            return None
        try:
            required_paths = self._required_task_workspace_deliverables()
        except Exception:
            required_paths = []
        if exact_manifest_path:
            candidate_paths = [str(path or "").strip()]
        else:
            candidate_paths = self._task_workspace_artifact_ordered_action_candidate_paths(
                action_paths,
                manifest=manifest_dict,
                manifest_path=path,
                required_paths=required_paths,
            )
        for candidate_path in candidate_paths:
            try:
                read_result = await self.task_workspace.read_file(
                    candidate_path,
                    max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES,
                )
            except Exception as error:
                outside_workspace = isinstance(error, (ValueError, TaskWorkspacePolicyError))
                code = (
                    "agent_task.task_workspace_artifact.action_file_outside_workspace"
                    if outside_workspace
                    else "agent_task.task_workspace_artifact.action_file_readback_failed"
                )
                message = (
                    "A successful file-producing Action returned a candidate artifact path outside the "
                    "TaskWorkspace root; the path is preserved as Action result evidence but was not "
                    "promoted to trusted TaskWorkspace file_refs."
                    if outside_workspace
                    else (
                        "A successful file-producing Action returned a candidate artifact path, but "
                        "TaskWorkspace readback failed; trusted file_refs were not produced for this path."
                    )
                )
                diagnostics.append(
                    self._task_workspace_artifact_readback_missing_diagnostic(
                        code=code,
                        message=message,
                        path=candidate_path,
                        source=source,
                        error=error,
                    )
                )
                continue
            ref = self._task_workspace_artifact_ref_from_readback(
                read_result,
                path=candidate_path,
                source=source,
            )
            if not self._task_workspace_artifact_ref_has_trusted_readback(ref):
                diagnostics.append(
                    self._task_workspace_artifact_readback_missing_diagnostic(
                        code="agent_task.task_workspace_artifact.action_file_readback_insufficient",
                        message=(
                            "A successful TaskWorkspace file action produced a candidate artifact path, "
                            "but readback was empty or missing integrity data."
                        ),
                        path=candidate_path,
                        source=source,
                        readback=read_result,
                    )
                )
                continue

            trusted_refs = [DataFormatter.sanitize(ref)]
            manifest_dict.update(
                {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "file_refs": trusted_refs,
                    "source": source,
                }
            )
            result["file_refs"] = trusted_refs
            result = self._compact_task_workspace_artifact_result_for_hot_path(
                result,
                content_key=content_key,
                content="",
                trusted_refs=trusted_refs,
                preserve_fields=(
                    ("candidate_final_result", "final_result")
                    if deliverable_mode == "inline_final"
                    else ()
                ),
            )
            delivery_record: dict[str, Any] = {
                "source": source,
                "path": ref["path"],
                "status": "adopted_existing",
                "mode": deliverable_mode,
                "content_key": content_key or "action_file_ref",
                "candidate_source": candidate_source,
                "readback": {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "truncated": ref["truncated"],
                    "read_bytes": ref["read_bytes"],
                    "handler_id": ref["handler_id"],
                },
                "file_refs": trusted_refs,
            }
            handoff = self._handoff_task_workspace_artifact_remaining_work_to_verifier(
                result,
                diagnostics=diagnostics,
                path=ref["path"],
                source=source,
                content_key=content_key or "action_file_ref",
            )
            if handoff is not None:
                delivery_record["remaining_work_handoff"] = DataFormatter.sanitize(handoff)
            result["artifact_manifest"] = self._compact_task_workspace_artifact_manifest_for_hot_path(
                manifest_dict,
                trusted_refs=trusted_refs,
                source=source,
            )
            locator_items = await self._task_workspace_artifact_acceptance_locator_evidence_items(
                ref=trusted_refs[0],
                result=result,
                manifest=manifest_dict,
                source=source,
                card_context=card_context,
            )
            if locator_items:
                delivery_record["acceptance_locator_count"] = len(locator_items)
            dependency_owned = candidate_source == "taskboard_context.dependency_results"
            diagnostics.append(
                {
                    "code": (
                        "agent_task.task_workspace_artifact.dependency_file_adopted"
                        if dependency_owned
                        else "agent_task.task_workspace_artifact.action_file_adopted"
                    ),
                    "message": (
                        "A trusted TaskBoard dependency already owns this TaskWorkspace artifact path; AgentTask "
                        "read it back and adopted the current content without model redrafting."
                        if dependency_owned
                        else (
                            "A successful TaskWorkspace file action produced the artifact path; AgentTask read it back "
                            "and adopted the readback as trusted TaskWorkspace artifact evidence."
                        )
                    ),
                    "path": ref["path"],
                    "source": source,
                }
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            self._append_task_workspace_artifact_meta(execution_meta, trusted_refs)
            self._append_execution_meta_evidence_items(execution_meta, locator_items)
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            return DataFormatter.sanitize(result)
        return None

    def _task_workspace_artifact_delivery_failure_result(
        self,
        result: dict[str, Any],
        execution_meta: Mapping[str, Any] | None,
        diagnostics: list[Any],
        *,
        path: str,
        source: str,
        deliverable_mode: str,
        content_key: str,
        code: str,
        message: str,
        error_type: str,
        readback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        diagnostic = self._task_workspace_artifact_readback_missing_diagnostic(
            code=code,
            message=message,
            path=path,
            source=source,
            readback=readback,
        )
        diagnostics.append(diagnostic)
        delivery_record = {
            "source": source,
            "path": path,
            "status": "failed",
            "mode": deliverable_mode or "task_workspace_artifact",
            "content_key": content_key,
            "error": {"type": error_type, "message": message},
            "diagnostics": [diagnostic],
        }
        result["status"] = "blocked"
        result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
        self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
        self._append_execution_meta_evidence_items(
            execution_meta,
            [
                self._task_workspace_artifact_failure_evidence_item(
                    path=path,
                    source=source,
                    code=code,
                    message=message,
                    readback=readback,
                )
            ],
        )
        return DataFormatter.sanitize(result)

    async def _deliver_task_workspace_artifact(
        self,
        execution_result: Any,
        *,
        plan: Mapping[str, Any] | None = None,
        execution_meta: Mapping[str, Any] | None = None,
        evidence_ledger: Mapping[str, Any] | None = None,
        source: str = "agent_task.task_workspace_artifact",
        context_pack: "TaskContextView | None" = None,
        iteration_index: int | None = None,
        card_context: Any | None = None,
        repair_context: Mapping[str, Any] | None = None,
        allow_stream_draft: bool = True,
    ) -> Any:
        if not isinstance(execution_result, Mapping):
            return execution_result
        result = dict(execution_result)
        manifest = result.get("artifact_manifest")
        manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
        diagnostics: list[Any] = []
        raw_diagnostics = result.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(raw_diagnostics)
        elif raw_diagnostics:
            diagnostics.append(raw_diagnostics)

        untrusted_refs = self._task_workspace_artifact_untrusted_refs(result, manifest_dict)
        if untrusted_refs:
            diagnostics.append(
                {
                    "code": "agent_task.task_workspace_artifact.untrusted_model_file_refs",
                    "message": "Model-declared file_refs are diagnostics only; trusted file refs require TaskWorkspace write/readback.",
                    "file_refs": DataFormatter.sanitize(untrusted_refs),
                }
            )
        result["file_refs"] = []
        if manifest_dict:
            manifest_dict.pop("file_refs", None)
            result["artifact_manifest"] = DataFormatter.sanitize(manifest_dict)

        if card_context is not None and not str((plan or {}).get("deliverable_mode") or "").strip():
            # A text carrier does not authorize a file side effect. Retain old
            # response bodies for replay without turning their field names into
            # a new delivery contract.
            if not result.get("candidate_final_result"):
                for key in ("final_result", "artifact_markdown", "artifact_html"):
                    body = result.get(key)
                    if isinstance(body, str) and body.strip():
                        result["candidate_final_result"] = body
                        break
            if diagnostics:
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return DataFormatter.sanitize(result)

        deliverable_mode = str((plan or {}).get("deliverable_mode") or "").strip()
        preserve_result_fields: tuple[str, ...] = (
            ("candidate_final_result", "final_result")
            if deliverable_mode == "inline_final"
            else ()
        )
        path = self._task_workspace_artifact_manifest_path(manifest_dict)
        content, content_key = self._select_task_workspace_artifact_content(
            result,
            manifest_dict,
            deliverable_mode=deliverable_mode,
            manifest_path=path,
        )
        prefer_stream_draft = bool((plan or {}).get("prefer_stream_draft"))
        manifest_needs_body = self._task_workspace_artifact_manifest_needs_body(manifest_dict)
        has_draftable_outline = self._task_workspace_artifact_manifest_has_draftable_outline(manifest_dict)
        if deliverable_mode == "sectioned_task_workspace_artifact" and manifest_needs_body:
            prefer_stream_draft = True
        if (
            prefer_stream_draft
            and manifest_needs_body
            and not self._task_workspace_artifact_content_is_complete_body(content)
        ):
            content = ""
            content_key = ""
        stream_draft_attempted = False
        if not deliverable_mode and (
            content_key == "answer"
            or (
                card_context is not None
                and content_key in {"candidate_final_result", "final_result"}
                and not manifest_dict
            )
        ):
            if diagnostics:
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return DataFormatter.sanitize(result)

        action_paths = self._task_workspace_artifact_successful_action_file_paths(
            execution_meta
        )
        dependency_paths = self._taskboard_dependency_trusted_artifact_paths(card_context)
        trusted_owner_paths = [*action_paths]
        for dependency_path in dependency_paths:
            if dependency_path not in trusted_owner_paths:
                trusted_owner_paths.append(dependency_path)
        trusted_candidate_source = (
            "execution_meta.action_logs"
            if action_paths
            else "taskboard_context.dependency_results"
        )
        action_owns_target = False
        target_owner_source = ""
        if path and trusted_owner_paths:
            try:
                target_path = self.task_workspace.resolve_file_path(path)
            except (TypeError, ValueError, TaskWorkspacePolicyError):
                target_path = None
            for action_path in trusted_owner_paths:
                try:
                    action_target = self.task_workspace.resolve_file_path(action_path)
                except (TypeError, ValueError, TaskWorkspacePolicyError):
                    continue
                if target_path is not None and action_target == target_path:
                    action_owns_target = True
                    target_owner_source = (
                        "execution_meta.action_logs"
                        if action_path in action_paths
                        else "taskboard_context.dependency_results"
                    )
                    break
        if action_owns_target:
            if content:
                dependency_owned = target_owner_source == "taskboard_context.dependency_results"
                diagnostics.append(
                    {
                        "code": (
                            "agent_task.task_workspace_artifact.dependency_file_preferred_over_model_body"
                            if dependency_owned
                            else "agent_task.task_workspace_artifact.action_file_preferred_over_model_body"
                        ),
                        "message": (
                            "A trusted TaskBoard dependency already owns this TaskWorkspace artifact path; its "
                            "current readback is authoritative and the model-returned body was not written again."
                            if dependency_owned
                            else (
                                "A successful file Action already owns this TaskWorkspace artifact path; its current "
                                "readback is authoritative and the model-returned body was not written again."
                            )
                        ),
                        "path": path,
                        "source": source,
                        "ignored_content_key": content_key,
                    }
                )
            adopted = await self._adopt_task_workspace_artifact_from_action_readback(
                result,
                manifest_dict=manifest_dict,
                path=path,
                deliverable_mode=deliverable_mode,
                content_key=content_key or "action_file_ref",
                diagnostics=diagnostics,
                execution_meta=execution_meta,
                source=source,
                card_context=card_context,
                exact_manifest_path=True,
                trusted_candidate_paths=trusted_owner_paths,
                candidate_source=target_owner_source or trusted_candidate_source,
            )
            if adopted is not None:
                return adopted
            return self._task_workspace_artifact_delivery_failure_result(
                result,
                execution_meta,
                diagnostics,
                path=path,
                source=source,
                deliverable_mode=deliverable_mode,
                content_key=content_key,
                code=(
                    "agent_task.task_workspace_artifact.dependency_file_owner_readback_failed"
                    if target_owner_source == "taskboard_context.dependency_results"
                    else "agent_task.task_workspace_artifact.action_file_owner_readback_failed"
                ),
                message=(
                    "A trusted existing owner produced the requested TaskWorkspace artifact path, but its current "
                    "readback could not be adopted; the model-returned body was not allowed to mask the failed "
                    "readback."
                ),
                error_type="ActionFileOwnerReadbackError",
            )
        if not content:
            adopted = await self._adopt_task_workspace_artifact_from_action_readback(
                result,
                manifest_dict=manifest_dict,
                path=path,
                deliverable_mode=deliverable_mode,
                content_key=content_key or "action_file_ref",
                diagnostics=diagnostics,
                execution_meta=execution_meta,
                source=source,
                card_context=card_context,
                trusted_candidate_paths=(trusted_owner_paths or None),
                candidate_source=trusted_candidate_source,
            )
            if adopted is not None:
                return adopted
        if (
            not content
            and allow_stream_draft
            and deliverable_mode in {"task_workspace_artifact", "sectioned_task_workspace_artifact"}
            and has_draftable_outline
        ):
            stream_draft_attempted = True
            stream_delivery = await self._stream_task_workspace_artifact_draft(
                path=path,
                plan=plan,
                execution_result=result,
                execution_meta=execution_meta,
                evidence_ledger=evidence_ledger,
                source=source,
                context_pack=context_pack,
                iteration_index=iteration_index,
                card_context=card_context,
                repair_context=repair_context,
            )
            if stream_delivery is not None:
                trusted_refs = stream_delivery["file_refs"]
                manifest_dict.update(
                    {
                        "path": trusted_refs[0]["path"],
                        "bytes": trusted_refs[0]["bytes"],
                        "sha256": trusted_refs[0]["sha256"],
                        "file_refs": trusted_refs,
                        "source": source,
                    }
                )
                result["file_refs"] = trusted_refs
                result = self._compact_task_workspace_artifact_result_for_hot_path(
                    result,
                    content_key="streamed_task_workspace_artifact",
                    content="",
                    trusted_refs=trusted_refs,
                    preserve_fields=preserve_result_fields,
                )
                handoff = self._handoff_task_workspace_artifact_remaining_work_to_verifier(
                    result,
                    diagnostics=diagnostics,
                    path=trusted_refs[0]["path"],
                    source=source,
                    content_key="streamed_task_workspace_artifact",
                )
                if handoff is not None:
                    stream_delivery["remaining_work_handoff"] = DataFormatter.sanitize(handoff)
                result["artifact_manifest"] = self._compact_task_workspace_artifact_manifest_for_hot_path(
                    manifest_dict,
                    trusted_refs=trusted_refs,
                    source=source,
                )
                locator_items = await self._task_workspace_artifact_acceptance_locator_evidence_items(
                    ref=trusted_refs[0],
                    result=result,
                    manifest=manifest_dict,
                    source=source,
                    card_context=card_context,
                )
                if locator_items:
                    stream_delivery["acceptance_locator_count"] = len(locator_items)
                result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(stream_delivery)
                diagnostics.append(
                    {
                        "code": "agent_task.task_workspace_artifact.stream_drafted",
                        "message": "TaskWorkspace artifact body was generated through a dedicated text stream and written by AgentTask.",
                        "path": trusted_refs[0]["path"],
                        "source": source,
                    }
                )
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
                self._append_task_workspace_artifact_meta(execution_meta, trusted_refs)
                self._append_execution_meta_evidence_items(execution_meta, locator_items)
                self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                    DataFormatter.sanitize(stream_delivery)
                )
                return DataFormatter.sanitize(result)
        if not content:
            if stream_draft_attempted and deliverable_mode in {"task_workspace_artifact", "sectioned_task_workspace_artifact"}:
                latest_delivery: Mapping[str, Any] | None = None
                raw_deliveries = self.diagnostics.get("task_workspace_artifact_delivery")
                if isinstance(raw_deliveries, Sequence) and not isinstance(
                    raw_deliveries,
                    str | bytes | bytearray,
                ):
                    for delivery in reversed(raw_deliveries):
                        if not isinstance(delivery, Mapping):
                            continue
                        if str(delivery.get("path") or "") == path and str(delivery.get("status") or "") == "failed":
                            latest_delivery = delivery
                            break
                error = latest_delivery.get("error") if isinstance(latest_delivery, Mapping) else None
                message = (str(error.get("message") or "") if isinstance(error, Mapping) else "").strip() or (
                    "TaskWorkspace artifact streamed draft failed or produced no content; trusted file_refs were not produced."
                )
                diagnostics.append(
                    self._task_workspace_artifact_readback_missing_diagnostic(
                        code="agent_task.task_workspace_artifact.draft_failed",
                        message=message,
                        path=path,
                        source=source,
                    )
                )
                result["status"] = "blocked"
                if latest_delivery is not None:
                    result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(dict(latest_delivery))
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
                self._append_execution_meta_evidence_items(
                    execution_meta,
                    [
                        self._task_workspace_artifact_failure_evidence_item(
                            path=path,
                            source=source,
                            code="agent_task.task_workspace_artifact.draft_failed",
                            message=message,
                        )
                    ],
                )
                return DataFormatter.sanitize(result)
            if deliverable_mode in {"task_workspace_artifact", "sectioned_task_workspace_artifact"}:
                if self._normalize_string_list(result.get("remaining_work")):
                    diagnostics.append(
                        {
                            "code": "agent_task.task_workspace_artifact.awaiting_body",
                            "message": (
                                "TaskWorkspace artifact delivery is deferred because remaining work exists and no "
                                "complete artifact body was provided."
                            ),
                            "path": path,
                            "source": source,
                        }
                    )
                    result["diagnostics"] = DataFormatter.sanitize(diagnostics)
                    return DataFormatter.sanitize(result)
                return self._task_workspace_artifact_delivery_failure_result(
                    result,
                    execution_meta,
                    diagnostics,
                    path=path,
                    source=source,
                    deliverable_mode=deliverable_mode,
                    content_key=content_key,
                    code="agent_task.task_workspace_artifact.empty_body",
                    message=(
                        "TaskWorkspace artifact delivery requires a non-empty body or a successful streamed draft "
                        "readback; trusted file_refs were not produced."
                    ),
                    error_type="EmptyWorkspaceArtifactBody",
                )
            if diagnostics:
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return DataFormatter.sanitize(result)
        if deliverable_mode in {
            "task_workspace_artifact",
            "sectioned_task_workspace_artifact",
        } and self._task_workspace_artifact_draft_is_structured_wrapper(content):
            return self._task_workspace_artifact_delivery_failure_result(
                result,
                execution_meta,
                diagnostics,
                path=path,
                source=source,
                deliverable_mode=deliverable_mode,
                content_key=content_key,
                code="agent_task.task_workspace_artifact.structured_wrapper_body",
                message=(
                    "TaskWorkspace artifact delivery received a structured wrapper instead of the requested natural "
                    "Markdown/text body; trusted file_refs were not produced."
                ),
                error_type="StructuredWorkspaceArtifactBody",
            )
        delivery_record: dict[str, Any] = {
            "source": source,
            "path": path,
            "status": "started",
            "mode": deliverable_mode or "artifact_markdown",
            "content_key": content_key,
        }
        preserved = await self._preserve_existing_task_workspace_artifact_if_preferable(
            path=path,
            new_content=content,
            source=source,
            content_key=content_key,
        )
        if preserved is not None:
            ref = preserved["file_ref"]
            delivery_record.update(
                {
                    "status": "preserved_existing",
                    "reason": "existing_task_workspace_artifact_is_substantially_larger",
                    "existing_bytes": preserved["existing_bytes"],
                    "new_bytes": preserved["new_bytes"],
                    "file_refs": [DataFormatter.sanitize(ref)],
                }
            )
            diagnostics.append(
                {
                    "code": "agent_task.task_workspace_artifact.preserved_existing",
                    "message": (
                        "Existing TaskWorkspace artifact was preserved because the proposed replacement was "
                        "substantially smaller. Return a full replacement body to overwrite it."
                    ),
                    "path": path,
                    "source": source,
                    "content_key": content_key,
                    "existing_bytes": preserved["existing_bytes"],
                    "new_bytes": preserved["new_bytes"],
                }
            )
            trusted_refs = [DataFormatter.sanitize(ref)]
            result["file_refs"] = trusted_refs
            manifest_dict.update(
                {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "file_refs": trusted_refs,
                    "source": source,
                }
            )
            result = self._compact_task_workspace_artifact_result_for_hot_path(
                result,
                content_key=content_key,
                content=content,
                trusted_refs=trusted_refs,
                preserve_fields=preserve_result_fields,
            )
            handoff = self._handoff_task_workspace_artifact_remaining_work_to_verifier(
                result,
                diagnostics=diagnostics,
                path=ref["path"],
                source=source,
                content_key=content_key,
            )
            if handoff is not None:
                delivery_record["remaining_work_handoff"] = DataFormatter.sanitize(handoff)
            result["artifact_manifest"] = self._compact_task_workspace_artifact_manifest_for_hot_path(
                manifest_dict,
                trusted_refs=trusted_refs,
                source=source,
            )
            locator_items = await self._task_workspace_artifact_acceptance_locator_evidence_items(
                ref=trusted_refs[0],
                result=result,
                manifest=manifest_dict,
                source=source,
                card_context=card_context,
            )
            if locator_items:
                delivery_record["acceptance_locator_count"] = len(locator_items)
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            self._append_task_workspace_artifact_meta(execution_meta, trusted_refs)
            self._append_execution_meta_evidence_items(execution_meta, locator_items)
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            return DataFormatter.sanitize(result)
        try:
            write_result = await self.task_workspace.write_file(path, content, append=False)
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {"type": error.__class__.__name__, "message": message},
                }
            )
            diagnostics.append(
                {
                    "code": "agent_task.task_workspace_artifact.write_failed",
                    "message": message,
                    "path": path,
                    "source": source,
                }
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.task_workspace_artifact.write_failed",
                        message=message,
                    )
                ],
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            return DataFormatter.sanitize(result)

        written_path = str(write_result.get("path") or path)
        try:
            read_result = await self.task_workspace.read_file(
                written_path,
                max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES,
            )
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            delivery_record.update(
                {
                    "status": "readback_failed",
                    "write": DataFormatter.sanitize(write_result),
                    "error": {"type": error.__class__.__name__, "message": message},
                }
            )
            diagnostics.append(
                self._task_workspace_artifact_readback_missing_diagnostic(
                    code="agent_task.task_workspace_artifact.readback_failed",
                    message=("TaskWorkspace artifact readback failed after write; trusted file_refs were not produced."),
                    path=written_path,
                    source=source,
                    error=error,
                )
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=written_path,
                        source=source,
                        code="agent_task.task_workspace_artifact.readback_failed",
                        message="TaskWorkspace artifact readback failed after write; trusted file_refs were not produced.",
                    )
                ],
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            return DataFormatter.sanitize(result)

        ref = self._task_workspace_artifact_ref_from_readback(
            read_result,
            path=written_path,
            source=source,
        )
        if not self._task_workspace_artifact_ref_has_trusted_readback(ref):
            delivery_record.update(
                {
                    "status": "readback_insufficient",
                    "write": DataFormatter.sanitize(write_result),
                    "readback": {
                        "path": ref["path"],
                        "bytes": ref["bytes"],
                        "sha256": ref["sha256"],
                        "truncated": ref["truncated"],
                        "read_bytes": ref["read_bytes"],
                        "handler_id": ref["handler_id"],
                    },
                }
            )
            diagnostics.append(
                self._task_workspace_artifact_readback_missing_diagnostic(
                    code="agent_task.task_workspace_artifact.readback_insufficient",
                    message=(
                        "TaskWorkspace artifact readback was missing or insufficient; "
                        "trusted file_refs were not produced."
                    ),
                    path=path,
                    source=source,
                    readback=read_result,
                )
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.task_workspace_artifact.readback_insufficient",
                        message=(
                            "TaskWorkspace artifact readback was missing or insufficient; "
                            "trusted file_refs were not produced."
                        ),
                        readback=read_result,
                    )
                ],
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            return DataFormatter.sanitize(result)

        delivery_record.update(
            {
                "status": "delivered",
                "write": DataFormatter.sanitize(write_result),
                "readback": {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "truncated": ref["truncated"],
                    "read_bytes": ref["read_bytes"],
                    "handler_id": ref["handler_id"],
                },
                "file_refs": [DataFormatter.sanitize(ref)],
            }
        )
        trusted_refs = [DataFormatter.sanitize(ref)]
        result["file_refs"] = trusted_refs
        manifest_dict.update(
            {
                "path": ref["path"],
                "bytes": ref["bytes"],
                "sha256": ref["sha256"],
                "file_refs": trusted_refs,
                "source": source,
            }
        )
        result = self._compact_task_workspace_artifact_result_for_hot_path(
            result,
            content_key=content_key,
            content=content,
            trusted_refs=trusted_refs,
            preserve_fields=preserve_result_fields,
        )
        handoff = self._handoff_task_workspace_artifact_remaining_work_to_verifier(
            result,
            diagnostics=diagnostics,
            path=ref["path"],
            source=source,
            content_key=content_key,
        )
        if handoff is not None:
            delivery_record["remaining_work_handoff"] = DataFormatter.sanitize(handoff)
        result["artifact_manifest"] = self._compact_task_workspace_artifact_manifest_for_hot_path(
            manifest_dict,
            trusted_refs=trusted_refs,
            source=source,
        )
        locator_items = await self._task_workspace_artifact_acceptance_locator_evidence_items(
            ref=trusted_refs[0],
            result=result,
            manifest=manifest_dict,
            source=source,
            content=content,
            card_context=card_context,
        )
        if locator_items:
            delivery_record["acceptance_locator_count"] = len(locator_items)
        result["task_workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
        if diagnostics:
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        self._append_task_workspace_artifact_meta(execution_meta, trusted_refs)
        self._append_execution_meta_evidence_items(execution_meta, locator_items)
        self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
        return DataFormatter.sanitize(result)

    async def _preserve_existing_task_workspace_artifact_if_preferable(
        self,
        *,
        path: str,
        new_content: str,
        source: str,
        content_key: str,
    ) -> dict[str, Any] | None:
        new_bytes = len(new_content.encode("utf-8"))
        if new_bytes <= 0:
            return None
        try:
            read_result = await self.task_workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except FileNotFoundError:
            return None
        except Exception:
            return None
        existing_bytes = int(read_result.get("bytes") or 0)
        if existing_bytes <= 0:
            return None
        if existing_bytes < max(new_bytes * 2, new_bytes + 1024):
            return None
        ref = self._task_workspace_artifact_ref_from_readback(
            read_result,
            path=path,
            source=source,
        )
        return {
            "file_ref": DataFormatter.sanitize(ref),
            "existing_bytes": existing_bytes,
            "new_bytes": new_bytes,
            "content_key": content_key,
        }

    @classmethod
    def _select_task_workspace_artifact_content(
        cls,
        result: Mapping[str, Any],
        manifest_dict: Mapping[str, Any],
        *,
        deliverable_mode: str,
        manifest_path: str = "",
    ) -> tuple[str, str]:
        manifest_content = cls._task_workspace_artifact_manifest_content(manifest_dict)
        candidates: list[tuple[str, str]] = []
        if manifest_content.strip():
            candidates.append(("artifact_manifest", manifest_content.strip()))
        body_keys = (
            ("artifact_markdown", "artifact_html")
            if deliverable_mode == "inline_final"
            else ("artifact_markdown", "artifact_html", "candidate_final_result", "final_result", "answer")
        )
        for key in body_keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append((key, value.strip()))
        if deliverable_mode in {"task_workspace_artifact", "sectioned_task_workspace_artifact"}:
            candidates.extend(cls._task_workspace_artifact_evidence_content_candidates(result, manifest_path=manifest_path))
        if not candidates:
            return "", ""
        if deliverable_mode in {"task_workspace_artifact", "sectioned_task_workspace_artifact"}:
            explicit_candidates = [
                item
                for item in candidates
                if item[0]
                in {"artifact_manifest", "artifact_markdown", "artifact_html", "candidate_final_result", "final_result"}
                or item[0].startswith("evidence[")
            ]
            answer_candidates = [item for item in candidates if item[0] == "answer"]
            if explicit_candidates:
                key, content = max(explicit_candidates, key=lambda item: len(item[1]))
                if answer_candidates:
                    answer_key, answer_content = max(answer_candidates, key=lambda item: len(item[1]))
                    if len(answer_content) >= max(len(content) * 2, len(content) + 64):
                        return answer_content, answer_key
                return content, key
            if answer_candidates:
                key, content = max(answer_candidates, key=lambda item: len(item[1]))
                return content, key
        for preferred_key in (
            "artifact_manifest",
            "artifact_markdown",
            "artifact_html",
            "candidate_final_result",
            "final_result",
            "answer",
        ):
            for key, content in candidates:
                if key == preferred_key:
                    return content, key
        return candidates[0][1], candidates[0][0]

    async def _stream_task_workspace_artifact_draft(
        self,
        *,
        path: str,
        plan: Mapping[str, Any] | None,
        execution_result: Mapping[str, Any],
        execution_meta: Mapping[str, Any] | None,
        source: str,
        evidence_ledger: Mapping[str, Any] | None = None,
        context_pack: "TaskContextView | None" = None,
        iteration_index: int | None = None,
        card_context: Any | None = None,
        repair_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        draft_execution = self.agent.create_execution(
            lineage={
                "task_id": self.id,
                "iteration_id": f"iter-{iteration_index}" if iteration_index is not None else None,
                "step_id": "task_workspace_artifact_draft",
                "scope": {"strategy_phase": "agent_task_workspace_artifact_draft"},
            },
            limits=self._child_execution_limits(),
            options=self._child_execution_options(),
        )
        self._apply_child_execution_action_loop_guard(draft_execution)
        self._disable_child_execution_action_loop(draft_execution)
        draft_execution.route_policy(
            {
                "allowed_routes": ["model_request"],
                "on_violation": "block",
                "owner": "AgentTask",
                "step_execution_shape": "task_workspace_artifact_draft",
            }
        )
        language_policy = self._language_policy()
        draft_execution.language(language_policy.get("language", "auto"))
        cumulative_evidence_ledger = self._cumulative_evidence_ledger(
            dict(execution_meta or {})
        )
        explicit_items = (
            list(evidence_ledger.get("items", []))
            if isinstance(evidence_ledger, Mapping)
            and isinstance(evidence_ledger.get("items"), Sequence)
            and not isinstance(
                evidence_ledger.get("items"), str | bytes | bytearray
            )
            else []
        )
        cumulative_items = (
            list(cumulative_evidence_ledger.get("items", []))
            if isinstance(cumulative_evidence_ledger.get("items"), Sequence)
            and not isinstance(
                cumulative_evidence_ledger.get("items"), str | bytes | bytearray
            )
            else []
        )
        draft_evidence_ledger = self._stable_evidence_ledger_view(
            {"evidence_items": [*explicit_items, *cumulative_items]},
            max_items=96,
            body_chars=1800,
            budget_selection="content_first",
            max_overflow_refs=96,
        )
        draft_prompt_evidence_ledger = self._model_evidence_ledger_projection(
            draft_evidence_ledger,
            max_items=64,
        )
        offered_source_references = source_refs_from_ledger(
            draft_evidence_ledger,
            max_refs=64,
        )
        active_repair_context = (
            dict(repair_context) if isinstance(repair_context, Mapping) else self._active_repair_context()
        )
        draft_input = {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "execution_strategy": self.execution_strategy,
            "artifact_path": path,
            "plan": DataFormatter.sanitize(plan or {}),
            "execution_result": DataFormatter.sanitize(execution_result),
            "execution_meta_summary": self._execution_log_summary(dict(execution_meta or {})),
            "evidence_ledger": DataFormatter.sanitize(
                draft_prompt_evidence_ledger
            ),
            "offered_source_references": DataFormatter.sanitize(offered_source_references),
            "context_pack": DataFormatter.sanitize(context_pack or {}),
            "card": DataFormatter.sanitize(
                card_context.card.to_dict()
                if card_context is not None
                and getattr(card_context, "card", None) is not None
                and hasattr(card_context.card, "to_dict")
                else {}
            ),
            "dependency_results": (
                self._compact_taskboard_dependency_results(
                    dict(getattr(card_context, "dependency_results", {}) or {})
                )
                if card_context is not None
                else {}
            ),
            "language_policy": language_policy,
        }
        if active_repair_context:
            draft_input["repair_context"] = DataFormatter.sanitize(active_repair_context)
        draft_execution.input(draft_input)
        draft_execution.instruct(
            (
                "Write only the final Markdown artifact body. "
                "Do not output JSON, YAML, XML, code fences, file_refs, or a wrapper object. "
                "Use only the provided task context, execution result, dependency results, evidence_ledger, and "
                "evidence summaries. Treat evidence_ledger as the authoritative bounded body-bearing evidence "
                "projection and preserve its observed values exactly. "
                "For source-grounded artifacts, cite only an offered_source_references.reference_id using the exact "
                "token [[ref:<reference_id>]]. Do not copy or invent URLs, paths, Action ids, TaskWorkspace ids, or other "
                "canonical identities into the citation token. "
                "When repair_context contains fields, this artifact draft is a repair pass: use its acceptance_delta, "
                "advisory_repair_constraints, advisory_next_step_requirements, and available_evidence_anchors as the "
                "active correction contract for the Markdown body. Rewrite affected artifact sections instead of only "
                "stating that they were fixed. "
                "If the source evidence is incomplete, write a clear source-boundary section instead of fabricating facts. "
                "Your output is the Markdown artifact body only."
            )
        )

        delivery_record: dict[str, Any] = {
            "source": source,
            "path": path,
            "status": "started",
            "mode": "streamed_task_workspace_artifact",
            "draft_execution_id": str(getattr(draft_execution, "id", "") or ""),
        }
        wrote_any = False
        received_delta = False
        bytes_written = 0
        carrier_path = path
        draft_stream = draft_execution.get_async_generator(
            type="specific",
            specific=["delta", "status", "done"],
        )
        retry_boundaries: list[dict[str, Any]] = []
        public_replay_markers: list[dict[str, Any]] = []

        async def handle_retry_boundary(retry_boundary: Mapping[str, Any]) -> None:
            nonlocal wrote_any, bytes_written, carrier_path
            retry_boundaries.append(DataFormatter.sanitize(dict(retry_boundary)))
            delivery_record["retry_boundaries"] = DataFormatter.sanitize(retry_boundaries)
            if wrote_any:
                reset = await self.task_workspace.write_file(carrier_path, "", append=False)
                carrier_path = str(reset.get("path") or carrier_path)
            wrote_any = False
            bytes_written = 0
            if iteration_index is not None:
                await self._emit(
                    f"agent_task.iteration.{iteration_index}.task_workspace_artifact_draft.retry",
                    {"path": path, "retry_boundary": retry_boundary},
                    meta={
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "stage": "task_workspace_artifact_draft",
                        "stream_kind": "task_workspace_artifact_draft_retry",
                        "path": path,
                    },
                )

        async def handle_public_replay_marker(marker: Mapping[str, Any]) -> None:
            nonlocal wrote_any, bytes_written, carrier_path
            public_replay_markers.append(DataFormatter.sanitize(dict(marker)))
            delivery_record["public_replay_markers"] = DataFormatter.sanitize(public_replay_markers)
            if wrote_any:
                reset = await self.task_workspace.write_file(carrier_path, "", append=False)
                carrier_path = str(reset.get("path") or carrier_path)
            wrote_any = False
            bytes_written = 0
            if iteration_index is not None:
                await self._emit(
                    f"agent_task.iteration.{iteration_index}.task_workspace_artifact_draft.public_replay_marker",
                    {"path": path, "marker": marker},
                    meta={
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "stage": "task_workspace_artifact_draft",
                        "stream_kind": "task_workspace_artifact_draft_public_replay_marker",
                        "path": path,
                    },
                )

        async def write_chunk(chunk: str) -> None:
            nonlocal wrote_any, received_delta, bytes_written, carrier_path
            if not chunk:
                return
            replay_marker = self._task_workspace_artifact_public_delta_replay_marker(chunk)
            if replay_marker is not None:
                await handle_public_replay_marker(replay_marker)
                return
            received_delta = True
            write_result = await self.task_workspace.write_file(carrier_path, chunk, append=wrote_any)
            carrier_path = str(write_result.get("path") or carrier_path)
            wrote_any = True
            bytes_written += len(chunk.encode("utf-8"))
            if iteration_index is not None:
                await self._emit(
                    f"agent_task.iteration.{iteration_index}.task_workspace_artifact_draft.delta",
                    {"path": path, "bytes_written": bytes_written},
                    event_type="delta",
                    delta=chunk,
                    is_complete=False,
                    meta={
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "stage": "task_workspace_artifact_draft",
                        "stream_kind": "task_workspace_artifact_draft",
                        "path": path,
                    },
                )

        try:
            while True:
                try:
                    stream_item = await self._await_stream_next(
                        draft_stream,
                        stage="task_workspace_artifact_draft",
                    )
                except StopAsyncIteration:
                    break
                if isinstance(stream_item, str):
                    await write_chunk(stream_item)
                    continue
                if isinstance(stream_item, tuple) and len(stream_item) >= 2:
                    event, data = stream_item[0], stream_item[1]
                    if event == "status":
                        retry_boundary = self._task_workspace_artifact_retry_boundary_from_status("$status", data)
                        if retry_boundary is not None:
                            await handle_retry_boundary(retry_boundary)
                        continue
                    if event == "delta":
                        await write_chunk(str(data))
                    continue
                item_path = str(getattr(stream_item, "path", "") or "")
                retry_boundary = self._task_workspace_artifact_retry_boundary_from_status(
                    item_path,
                    getattr(stream_item, "value", None),
                )
                if retry_boundary is not None:
                    await handle_retry_boundary(retry_boundary)
                    continue
                if getattr(stream_item, "event_type", None) != "delta":
                    continue
                chunk = str(getattr(stream_item, "delta", None) or "")
                await write_chunk(chunk)
            draft_meta = await self._await_task_request(
                draft_execution.async_get_meta(),
                stage="task_workspace_artifact_draft_meta",
            )
            delivery_record["draft_meta"] = {
                "execution_id": draft_meta.get("execution_id"),
                "status": draft_meta.get("status"),
                "route": DataFormatter.sanitize(draft_meta.get("route")),
            }
            if not received_delta and draft_meta.get("status") in {"success", "completed"}:
                # A non-streaming request delivers its body only at completion.
                # Read this settled execution; do not replay partial delta attempts.
                completed_body = await self._await_task_request(
                    draft_execution.async_get_data(),
                    stage="task_workspace_artifact_draft_result",
                )
                if isinstance(completed_body, str):
                    await write_chunk(completed_body)
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {"type": error.__class__.__name__, "message": message},
                    "bytes_written": bytes_written,
                }
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.task_workspace_artifact.draft_failed",
                        message=message,
                    )
                ],
            )
            return None
        finally:
            aclose = getattr(draft_stream, "aclose", None)
            if callable(aclose):
                with suppress(Exception):
                    await cast(Awaitable[Any], aclose())
        if not wrote_any:
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {
                        "type": "EmptyWorkspaceArtifactDraft",
                        "message": "TaskWorkspace artifact draft stream produced no content.",
                    },
                    "bytes_written": bytes_written,
                }
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.task_workspace_artifact.draft_empty",
                        message="TaskWorkspace artifact draft stream produced no content.",
                    )
                ],
            )
            return None

        try:
            read_result = await self.task_workspace.read_file(
                carrier_path,
                max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES,
            )
        except Exception as error:
            diagnostic = self._task_workspace_artifact_readback_missing_diagnostic(
                code="agent_task.task_workspace_artifact.readback_failed",
                message="TaskWorkspace artifact draft readback failed after write; trusted file_refs were not produced.",
                path=carrier_path,
                source=source,
                error=error,
            )
            delivery_record.update(
                {
                    "status": "readback_failed",
                    "error": {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    },
                    "bytes_written": bytes_written,
                    "diagnostics": [diagnostic],
                }
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=carrier_path,
                        source=source,
                        code="agent_task.task_workspace_artifact.readback_failed",
                        message="TaskWorkspace artifact draft readback failed after write; trusted file_refs were not produced.",
                    )
                ],
            )
            return None

        ref = self._task_workspace_artifact_ref_from_readback(
            read_result,
            path=carrier_path,
            source=source,
        )
        if self._task_workspace_artifact_draft_is_structured_wrapper(str(read_result.get("content") or "")):
            diagnostic = self._task_workspace_artifact_readback_missing_diagnostic(
                code="agent_task.task_workspace_artifact.structured_wrapper_draft",
                message=(
                    "TaskWorkspace artifact draft returned a structured wrapper instead of the requested natural "
                    "Markdown/text body; trusted file_refs were not produced."
                ),
                path=path,
                source=source,
                readback=read_result,
            )
            with suppress(Exception):
                await self.task_workspace.write_file(carrier_path, "", append=False)
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {
                        "type": "StructuredWorkspaceArtifactDraft",
                        "message": "TaskWorkspace artifact draft returned a structured wrapper instead of a body.",
                    },
                    "bytes_written": bytes_written,
                    "readback": {
                        "path": ref["path"],
                        "bytes": ref["bytes"],
                        "sha256": ref["sha256"],
                        "truncated": ref["truncated"],
                        "read_bytes": ref["read_bytes"],
                        "handler_id": ref["handler_id"],
                    },
                    "diagnostics": [diagnostic],
                }
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.task_workspace_artifact.structured_wrapper_draft",
                        message="TaskWorkspace artifact draft returned a structured wrapper instead of a body.",
                        readback=read_result,
                    )
                ],
            )
            return None
        if not self._task_workspace_artifact_ref_has_trusted_readback(ref):
            diagnostic = self._task_workspace_artifact_readback_missing_diagnostic(
                code="agent_task.task_workspace_artifact.readback_insufficient",
                message=(
                    "TaskWorkspace artifact draft readback was missing or insufficient; "
                    "trusted file_refs were not produced."
                ),
                path=path,
                source=source,
                readback=read_result,
            )
            delivery_record.update(
                {
                    "status": "readback_insufficient",
                    "bytes_written": bytes_written,
                    "readback": {
                        "path": ref["path"],
                        "bytes": ref["bytes"],
                        "sha256": ref["sha256"],
                        "truncated": ref["truncated"],
                        "read_bytes": ref["read_bytes"],
                        "handler_id": ref["handler_id"],
                    },
                    "diagnostics": [diagnostic],
                }
            )
            self.diagnostics.setdefault("task_workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._task_workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.task_workspace_artifact.readback_insufficient",
                        message=(
                            "TaskWorkspace artifact draft readback was missing or insufficient; "
                            "trusted file_refs were not produced."
                        ),
                        readback=read_result,
                    )
                ],
            )
            return None

        delivery_record.update(
            {
                "status": "delivered",
                "bytes_written": bytes_written,
                "readback": {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "truncated": ref["truncated"],
                    "read_bytes": ref["read_bytes"],
                    "handler_id": ref["handler_id"],
                },
                "file_refs": [DataFormatter.sanitize(ref)],
            }
        )
        return DataFormatter.sanitize(delivery_record)

    @staticmethod
    def _is_trusted_task_workspace_artifact_ref(ref: Mapping[str, Any]) -> bool:
        role = str(ref.get("role") or "").strip()
        source = str(ref.get("source") or "").strip()
        return role == "task_workspace_artifact" or source.startswith("agent_task.task_workspace_artifact")

    @staticmethod
    def _looks_like_task_workspace_artifact_placeholder(value: str) -> bool:
        return value.strip().startswith("TaskWorkspace artifact delivered at ")

    @staticmethod
    def _task_workspace_artifact_draft_is_structured_wrapper(content: str) -> bool:
        text = content.strip()
        if not text:
            return False
        if not ((text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]"))):
            return False
        try:
            parsed = json.loads(text)
        except Exception:
            return False
        if isinstance(parsed, Mapping):
            wrapper_keys = {
                "answer",
                "status",
                "result",
                "message",
                "content",
                "data",
                "diagnostics",
                "remaining_work",
                "evidence",
            }
            return bool(set(str(key) for key in parsed.keys()).intersection(wrapper_keys))
        return isinstance(parsed, list)

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(number, 0)


__all__ = ["AgentTaskArtifactMixin"]
