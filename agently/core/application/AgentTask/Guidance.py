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

import hashlib
import json

from agently.core.context import ModelRequestContextSelector
from agently.types.data import (
    ContextBudget,
    ContextConsumer,
    ContextConsumption,
    ContextReadIntent,
)

from .TaskShared import *

_GUIDANCE_PREVIEW_CHARS = 800


class AgentTaskGuidanceMixin(AgentTaskMixinBase):
    _TASK_CONTEXT_EVIDENCE_ROLES = frozenset(
        {"information", "state", "artifact"}
    )

    def _task_context_semantic_selector(self) -> Any:
        request_factory = getattr(self.agent, "create_temp_request", None)
        return (
            ModelRequestContextSelector(request_factory)
            if callable(request_factory)
            else None
        )

    def _task_context_reader(self, *, phase: str, consumer_id: str) -> Any:
        key = (str(consumer_id), str(phase))
        reader = self.context_readers.get(key)
        if reader is not None:
            if not reader.is_current:
                reader.refresh()
            return reader
        raw_chars = self.context_budget.get("chars", 6000)
        try:
            max_chars = max(1, int(raw_chars))
        except (TypeError, ValueError):
            max_chars = 6000
        reader = self.task_context.reader(
            consumer=ContextConsumer(
                consumer_id=str(consumer_id),
                model=str(getattr(self.agent, "_active_model_key", "") or "")
                or None,
                capabilities=self._task_context_consumer_capabilities(),
            ),
            phase=phase,
            budget=ContextBudget(
                max_chars=max_chars,
                max_blocks=64,
                max_block_chars=min(max_chars, 6000),
            ),
            semantic_selector=self._task_context_semantic_selector(),
        )
        self.context_readers[key] = reader
        return reader

    def _task_context_consumer_capabilities(self) -> dict[str, Any]:
        """Resolve explicit model-input capabilities without guessing by name."""

        candidates: list[Any] = []
        candidates.append(self.options.get("context_consumer_capabilities"))
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, Mapping):
            candidates.append(
                agent_task_options.get("context_consumer_capabilities")
            )
        settings = getattr(self.agent, "settings", None)
        get_setting = getattr(settings, "get", None)
        if callable(get_setting):
            candidates.append(get_setting("model_capabilities", None))
            provider = str(
                get_setting(
                    "plugins.ModelRequester.activate",
                    "",
                )
                or ""
            ).strip()
            if provider:
                candidates.append(
                    get_setting(
                        f"plugins.ModelRequester.{provider}.capabilities",
                        None,
                    )
                )
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                return DataFormatter.sanitize(dict(candidate))
        return {}

    @staticmethod
    def _bind_task_context_attachments(request: Any, package: Any) -> None:
        """Bind disclosed image blocks through ModelRequest rich content only."""

        def plain(value: Any) -> Any:
            if isinstance(value, Mapping):
                return {str(key): plain(item) for key, item in value.items()}
            if isinstance(value, Sequence) and not isinstance(
                value,
                str | bytes | bytearray,
            ):
                return [plain(item) for item in value]
            return value

        attachments: list[dict[str, Any]] = []
        for block in getattr(package, "blocks", ()):
            if (
                str(block.metadata.get("context_representation") or "")
                != "image_attachment"
            ):
                continue
            content = block.content
            if not isinstance(content, Sequence) or isinstance(
                content,
                str | bytes | bytearray,
            ):
                continue
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("type") or "") != "image_url":
                    continue
                attachments.append(cast(dict[str, Any], plain(item)))
        if not attachments:
            return
        prompt = getattr(request, "prompt", None)
        current = prompt.get("attachment") if prompt is not None else None
        rich_content = (
            [dict(item) for item in current if isinstance(item, Mapping)]
            if isinstance(current, Sequence)
            and not isinstance(current, str | bytes | bytearray)
            else []
        )
        rich_content.extend(
            [
                {
                    "type": "text",
                    "text": (
                        "The following image inputs were disclosed by TaskContext. "
                        "Use their source_ref entries in the accompanying context pack; "
                        "do not infer content from filenames."
                    ),
                },
                *attachments,
            ]
        )
        request.attachment(rich_content)

    async def _context_pack_with_task_context(self, context_pack: Any) -> "TaskContextView":
        projected, _package = await self._read_task_context_view(
            phase="planning",
            consumer_id=f"agent_task:{self.id}:planner",
        )
        if isinstance(context_pack, Mapping):
            projected["legacy_input_diagnostics"] = DataFormatter.sanitize(
                context_pack.get("diagnostics", {})
            )
        return cast("TaskContextView", projected)

    async def _read_task_context_view(
        self,
        *,
        phase: str,
        consumer_id: str,
        intent: str | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """Read and project one package for one concrete consumer boundary."""

        package = await self._read_task_context_package(
            phase=phase,
            consumer_id=consumer_id,
            intent=intent,
        )
        projected = self._context_pack_with_guidance(
            cast("TaskContextView", self._project_task_context_package(package))
        )
        return dict(projected), package

    async def _read_task_context_package(
        self,
        *,
        phase: str,
        consumer_id: str,
        intent: str | None = None,
    ) -> Any:
        reader = self._task_context_reader(phase=phase, consumer_id=consumer_id)
        if not reader.is_current:
            reader.refresh()
        intent_metadata: dict[str, Any] = {"exclude_already_in_prompt": True}
        required_overflow = str(
            self.context_budget.get("required_overflow") or "fail"
        ).strip()
        if required_overflow == "lossy_digest":
            intent_metadata["required_overflow"] = required_overflow
        optional_selection = str(
            self.context_budget.get("optional_selection") or ""
        ).strip()
        if optional_selection == "none":
            intent_metadata["optional_selection"] = optional_selection
        implicit_source_kinds = tuple(
            binding.source_kind
            for binding in self.task_context.snapshot().bindings
            if str(binding.metadata.get("disclosure_mode") or "implicit").strip()
            != "explicit_retrieval"
        )
        package = await reader.async_read(
            ContextReadIntent(
                query=str(intent or self.goal),
                filters=(
                    {"source_kinds": implicit_source_kinds}
                    if implicit_source_kinds
                    else {}
                ),
                metadata=intent_metadata,
            )
        )
        reader.ensure_required_delivery(package)
        self.context_packages.append(package)
        return package

    @staticmethod
    def _task_context_block_content_version(block: Any) -> tuple[str, Any]:
        representation = str(
            getattr(block, "metadata", {}).get("context_representation") or ""
        ).strip()
        content = (
            None
            if representation == "image_attachment"
            else DataFormatter.sanitize(getattr(block, "content", None))
        )
        encoded = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest(), content

    @classmethod
    def _task_context_package_evidence_items(cls, package: Any) -> list[dict[str, Any]]:
        """Project model-visible source facts into the canonical evidence domain.

        ContextPackage remains the disclosure/read owner.  AgentTask only adds
        host-issued evidence identity for body-bearing information, state, and
        artifact blocks; procedural instruction/example/capability/index blocks
        remain context and cannot silently become business evidence.
        """

        items: list[dict[str, Any]] = []
        for block in getattr(package, "blocks", ()):
            role = str(getattr(block, "role", "") or "").strip()
            if role not in cls._TASK_CONTEXT_EVIDENCE_ROLES:
                continue
            completeness = str(
                getattr(block, "completeness", "") or ""
            ).strip()
            status = (
                "failed"
                if completeness == "failed"
                else "empty"
                if completeness == "empty"
                else "ok"
            )
            content_digest, content = cls._task_context_block_content_version(
                block
            )
            body_state = (
                "full"
                if completeness == "complete" and content not in (None, "", [], {})
                else "truncated"
                if completeness in {"truncated", "lossy"}
                and content not in (None, "", [], {})
                else "ref_only"
            )
            identity_payload = {
                "task_context_id": str(getattr(package, "task_context_id", "") or ""),
                "block_key": str(getattr(block, "block_key", "") or ""),
                "source_id": str(getattr(block, "source_id", "") or ""),
                "source_revision": str(
                    getattr(block, "source_revision", "") or ""
                ),
                "source_ref": str(getattr(block, "source_ref", "") or ""),
                "binding_id": str(getattr(block, "binding_id", "") or ""),
                "content_digest": content_digest,
                "completeness": completeness,
            }
            identity_digest = hashlib.sha256(
                json.dumps(
                    identity_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            metadata = getattr(block, "metadata", {})
            item: dict[str, Any] = {
                "id": f"task_context:{identity_digest}",
                "kind": "task_context.content",
                "status": status,
                "body_state": body_state,
                "source_id": identity_payload["source_id"],
                "source_revision": identity_payload["source_revision"],
                "source_ref": identity_payload["source_ref"],
                "binding_id": identity_payload["binding_id"],
                "evidence_role": role,
                "content_version_id": f"context_content:{content_digest}",
                "locator": identity_payload["source_ref"],
                "provenance": {
                    "source": "task_context",
                    "package_id": str(getattr(package, "package_id", "") or ""),
                    "task_context_id": identity_payload["task_context_id"],
                    "context_revision": getattr(package, "context_revision", None),
                    "consumer_id": str(getattr(package, "consumer_id", "") or ""),
                    "phase": str(getattr(package, "phase", "") or ""),
                    "block_key": identity_payload["block_key"],
                },
                "aliases": [
                    str(value)
                    for value in (
                        identity_payload["source_ref"],
                        *tuple(getattr(block, "refs", ()) or ()),
                    )
                    if str(value or "").strip()
                ],
            }
            if body_state != "ref_only":
                item["body"] = content
            if isinstance(metadata, Mapping):
                for field in (
                    "path",
                    "record_id",
                    "collection",
                    "source_url",
                    "selected_url",
                    "requested_url",
                    "canonical_url",
                    "url",
                    "href",
                ):
                    if metadata.get(field) not in (None, "", [], {}):
                        item[field] = DataFormatter.sanitize(metadata.get(field))
            items.append(DataFormatter.sanitize(item))
        return items

    def _task_context_package_evidence_ledger(
        self,
        package: Any,
        *,
        max_items: int = 64,
        body_chars: int = 1800,
    ) -> dict[str, Any]:
        return self._stable_evidence_ledger_view(
            {
                "evidence_items": self._task_context_package_evidence_items(
                    package
                )
            },
            max_items=max_items,
            body_chars=body_chars,
            budget_selection="content_first",
        )

    def _record_task_context_consumption(
        self,
        package: Any,
        *,
        request_id: str,
    ) -> ContextConsumption:
        consumption = ContextConsumption(
            consumption_id=f"context_consumption:{uuid.uuid4().hex}",
            package_id=package.package_id,
            request_id=str(request_id),
            consumer_id=package.consumer_id,
            phase=package.phase,
            block_ids=tuple(block.block_id for block in package.blocks),
        )
        self.context_consumptions.append(consumption)
        return consumption

    @staticmethod
    def _project_task_context_package(package: Any) -> dict[str, Any]:
        source_coverage = package.to_dict().get("source_coverage", {})
        continuation_available = any(
            bool(record.get("continuation_available"))
            for record in source_coverage.values()
            if isinstance(record, Mapping)
        )
        items = [
            {
                "id": block.block_id,
                "role": block.role,
                "content": (
                    None
                    if str(
                        block.metadata.get("context_representation") or ""
                    )
                    == "image_attachment"
                    else DataFormatter.sanitize(block.content)
                ),
                "source_ref": block.source_ref,
                "completeness": block.completeness,
                "required": block.required,
            }
            for block in package.blocks
        ]
        skills: dict[str, dict[str, Any]] = {}
        for block in package.blocks:
            skill_id = str(block.metadata.get("skill_id") or "").strip()
            if not skill_id:
                continue
            target = skills.setdefault(
                skill_id,
                {
                    "skill_id": skill_id,
                    "binding_id": str(block.metadata.get("skill_binding_id") or ""),
                    "revision_ref": str(block.metadata.get("revision_ref") or ""),
                    "mode": str(block.metadata.get("skill_mode") or "model_decision"),
                    "guidance": None,
                    "selected_resources": [],
                    "action_candidates": [],
                },
            )
            resource_path = str(block.metadata.get("resource_path") or "")
            if resource_path == "SKILL.md":
                target["guidance"] = {
                    "excerpt": DataFormatter.sanitize(block.content),
                    "completeness": block.completeness,
                }
            elif resource_path and resource_path != "resource-index":
                target["selected_resources"].append(
                    {
                        "path": resource_path,
                        "content": DataFormatter.sanitize(block.content),
                        "completeness": block.completeness,
                        "source_ref": block.source_ref,
                    }
                )
        omission_records = [item.to_dict() for item in package.omissions]
        reason_counts: dict[str, int] = {}
        for item in omission_records:
            reason = str(item.get("reason") or "unspecified")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        omission_details = sorted(
            enumerate(omission_records),
            key=lambda pair: (
                not bool(pair[1].get("required")),
                pair[0],
            ),
        )[:8]
        projected_omissions = [item for _index, item in omission_details]
        return {
            "schema_version": "agently.context_package.agent_task.v2",
            "package_id": package.package_id,
            "task_context_id": package.task_context_id,
            "context_revision": package.context_revision,
            "profile": "task_context",
            "items": items,
            # ContextPackage retains the complete audit record.  Model-hot
            # projections carry bounded details plus counts so a large source
            # catalog cannot consume the request merely by being unselected.
            "omitted": projected_omissions,
            "omission_summary": {
                "total": len(omission_records),
                "required": sum(
                    bool(item.get("required")) for item in omission_records
                ),
                "details_returned": len(projected_omissions),
                "details_omitted": len(omission_records) - len(projected_omissions),
                "reason_counts": reason_counts,
            },
            "diagnostics": [item.to_dict() for item in package.diagnostics],
            "used_chars": package.used_chars,
            "source_coverage": source_coverage,
            "continuation_available": continuation_available,
            "skill_projection": {
                "schema_version": "agently.context_package.skill_projection.v2",
                "skills": list(skills.values()),
                "required_skill_ids": [
                    skill_id
                    for skill_id, item in skills.items()
                    if item.get("mode") == "required"
                ],
                "used_chars": sum(
                    block.content_chars
                    for block in package.blocks
                    if block.metadata.get("skill_id")
                ),
                "usable": True,
                "diagnostics": [],
            },
        }

    async def _context_pack_with_required_skill_context(self, context_pack: Any) -> "TaskContextView":
        return cast("TaskContextView", context_pack)

    @classmethod
    def _compact_skill_projection_for_agent_task(cls, skill_projection: Any) -> dict[str, Any]:
        if not isinstance(skill_projection, Mapping):
            return {}
        compact: dict[str, Any] = {}
        for key in (
            "schema_version",
            "task",
            "intent",
            "budget_chars",
            "used_chars",
            "truncated",
            "citations",
            "public_sources",
            "diagnostics",
            "usable",
            "required_skill_ids",
        ):
            if key in skill_projection:
                compact[key] = DataFormatter.sanitize(skill_projection.get(key))
        raw_skills = skill_projection.get("skills")
        skills: list[dict[str, Any]] = []
        if isinstance(raw_skills, Sequence) and not isinstance(raw_skills, str | bytes | bytearray):
            for item in raw_skills:
                if not isinstance(item, Mapping):
                    continue
                skill_item: dict[str, Any] = {}
                for key in (
                    "skill_id",
                    "display_name",
                    "guidance",
                    "selected_resources",
                    "action_candidates",
                    "resource_index",
                    "binding_id",
                    "revision_ref",
                    "skill_key",
                    "mode",
                    "allocation",
                ):
                    if key in item:
                        skill_item[key] = DataFormatter.sanitize(item.get(key))
                if skill_item:
                    skills.append(skill_item)
        compact["skills"] = skills
        compact["resource_policy"] = {
            "skills_citations_are_already_loaded": True,
            "do_not_use_citations_as_task_workspace_paths": True,
            "cold_source_paths_hidden": True,
        }
        return DataFormatter.sanitize(compact)

    def _required_skill_context_blocker(
        self,
        context_pack: Any,
    ) -> dict[str, Any] | None:
        required_skill_ids, required_skill_pack_ids = self._required_skill_context_selectors()
        if not required_skill_ids and not required_skill_pack_ids:
            return None
        if not isinstance(context_pack, Mapping):
            return {
                "reason_code": "required_skill_context_unavailable",
                "reason": "Required Skill context preparation returned no structured context pack.",
                "required_skill_ids": required_skill_ids,
                "required_skill_pack_ids": required_skill_pack_ids,
                "missing_skill_ids": required_skill_ids,
            }

        context_diagnostics = context_pack.get("diagnostics")
        skills_diagnostic = (
            context_diagnostics.get("skill_projection") if isinstance(context_diagnostics, Mapping) else None
        )
        skills_pack = context_pack.get("skill_projection")
        if not isinstance(skills_pack, Mapping):
            return {
                "reason_code": "required_skill_context_unavailable",
                "reason": "Required Skill context could not be built before business planning.",
                "required_skill_ids": required_skill_ids,
                "required_skill_pack_ids": required_skill_pack_ids,
                "missing_skill_ids": required_skill_ids,
                "diagnostics": DataFormatter.sanitize(skills_diagnostic or {}),
            }

        raw_skills = skills_pack.get("skills")
        skills = (
            [item for item in raw_skills if isinstance(item, Mapping)]
            if isinstance(raw_skills, Sequence)
            and not isinstance(
                raw_skills,
                str | bytes | bytearray,
            )
            else []
        )
        skill_by_id = {
            str(item.get("skill_id") or "").strip(): item for item in skills if str(item.get("skill_id") or "").strip()
        }
        missing_skill_ids = [skill_id for skill_id in required_skill_ids if skill_id not in skill_by_id]
        empty_guidance_skill_ids: list[str] = []
        context_required_ids = set(self._normalize_string_list(skills_pack.get("required_skill_ids")))
        context_required_ids.update(required_skill_ids)
        if required_skill_pack_ids:
            context_required_ids.update(skill_by_id)
        for skill_id in sorted(context_required_ids):
            item = skill_by_id.get(skill_id)
            if not isinstance(item, Mapping):
                continue
            guidance = item.get("guidance")
            excerpt = str(guidance.get("excerpt") or "").strip() if isinstance(guidance, Mapping) else ""
            allocation = item.get("allocation")
            raw_guidance_chars = allocation.get("guidance_chars") if isinstance(allocation, Mapping) else len(excerpt)
            try:
                guidance_chars = int(raw_guidance_chars or 0)
            except (TypeError, ValueError):
                guidance_chars = 0
            if not excerpt or guidance_chars <= 0:
                empty_guidance_skill_ids.append(skill_id)

        status = str(skills_diagnostic.get("status") or "") if isinstance(skills_diagnostic, Mapping) else ""
        unusable = skills_pack.get("usable") is False or status in {
            "unavailable",
            "failed",
            "empty",
            "unusable",
        }
        if not (unusable or missing_skill_ids or empty_guidance_skill_ids):
            return None
        return {
            "reason_code": "required_skill_context_unavailable",
            "reason": (
                "Required Skill context was not installed and bound with non-empty "
                "guidance before business planning."
            ),
            "required_skill_ids": required_skill_ids,
            "required_skill_pack_ids": required_skill_pack_ids,
            "missing_skill_ids": missing_skill_ids,
            "empty_guidance_skill_ids": empty_guidance_skill_ids,
            "diagnostics": DataFormatter.sanitize(skills_diagnostic or {}),
            "pack_diagnostics": DataFormatter.sanitize(skills_pack.get("diagnostics", [])),
        }

    async def _terminate_required_skill_context_blocked(
        self,
        iteration_index: int,
        blocker: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.status = "blocked"
        required_skill_ids = self._normalize_string_list(blocker.get("required_skill_ids"))
        required_skill_pack_ids = self._normalize_string_list(blocker.get("required_skill_pack_ids"))
        missing_skill_ids = self._merge_string_lists(
            self._normalize_string_list(blocker.get("missing_skill_ids")),
            self._normalize_string_list(blocker.get("empty_guidance_skill_ids")),
        )
        missing_criteria = [
            f"required_skill_context:{skill_id}" for skill_id in (missing_skill_ids or required_skill_ids)
        ]
        missing_criteria.extend(f"required_skill_pack_context:{pack_id}" for pack_id in required_skill_pack_ids)
        missing_criteria = self._merge_string_lists(missing_criteria)
        reason = str(blocker.get("reason") or "Required Skill context is unavailable.")
        reason_code = str(blocker.get("reason_code") or "required_skill_context_unavailable")
        self.diagnostics["required_skill_context"] = DataFormatter.sanitize(dict(blocker))
        self.diagnostics["terminal_reason"] = reason_code
        self.result = {
            "status": "blocked",
            "accepted": False,
            "artifact_status": "blocked",
            "task_id": self.id,
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "reason_code": reason_code,
            "reason": reason,
            "final_response": self._agent_task_user_final_response(
                accepted=False,
                artifact_status="blocked",
                status="blocked",
                reason=reason,
            ),
            "final_result": "",
            "artifact_refs": [],
            "missing_criteria": missing_criteria,
            "required_skill_context": DataFormatter.sanitize(dict(blocker)),
        }
        await self._record_phase(
            "terminal",
            iteration=iteration_index,
            diagnostics={
                "status": "blocked",
                "reason_code": reason_code,
                "missing_criteria": missing_criteria,
            },
        )
        await self._emit("agent_task.blocked", self.result)
        return {
            "terminal": True,
            "status": "blocked",
            "reason_code": reason_code,
            "missing_criteria": missing_criteria,
        }

    async def _emit_required_skill_context_bound(
        self,
        context_pack: Any,
        *,
        request_id: str,
        phase: str,
    ) -> None:
        required_skill_ids, required_skill_pack_ids = self._required_skill_context_selectors()
        if not required_skill_ids and not required_skill_pack_ids:
            return
        if not isinstance(context_pack, Mapping):
            return
        skills_pack = context_pack.get("skill_projection")
        if not isinstance(skills_pack, Mapping) or skills_pack.get("usable") is False:
            return
        raw_skills = skills_pack.get("skills")
        if not isinstance(raw_skills, Sequence) or isinstance(
            raw_skills,
            str | bytes | bytearray,
        ):
            return
        required_set = set(required_skill_ids)
        bindings: list[dict[str, Any]] = []
        for item in raw_skills:
            if not isinstance(item, Mapping):
                continue
            skill_id = str(item.get("skill_id") or "").strip()
            mode = str(item.get("mode") or "")
            if skill_id not in required_set and mode != "required" and not required_skill_pack_ids:
                continue
            guidance = item.get("guidance")
            excerpt = str(guidance.get("excerpt") or "") if isinstance(guidance, Mapping) else ""
            allocation = item.get("allocation")
            allocation = allocation if isinstance(allocation, Mapping) else {}
            selected_resources = item.get("selected_resources")
            selected_resources = (
                list(selected_resources)
                if isinstance(selected_resources, Sequence)
                and not isinstance(selected_resources, str | bytes | bytearray)
                else []
            )
            selected_resource_keys = self._normalize_string_list(allocation.get("selected_resource_keys"))
            if not selected_resource_keys:
                selected_resource_keys = [
                    str(resource.get("selection_key") or "")
                    for resource in selected_resources
                    if isinstance(resource, Mapping) and str(resource.get("selection_key") or "")
                ]
            bindings.append(
                {
                    "binding_id": str(item.get("binding_id") or ""),
                    "canonical_skill_id": skill_id,
                    "mode": mode or "required",
                    "guidance_chars": int(allocation.get("guidance_chars") or len(excerpt)),
                    "resource_chars": int(allocation.get("resource_chars") or 0),
                    "selected_resource_keys": selected_resource_keys,
                    "truncated": bool(
                        allocation.get("truncated") or (isinstance(guidance, Mapping) and guidance.get("truncated"))
                    ),
                }
            )
        if not bindings:
            return
        emitted = getattr(self, "_emitted_skill_context_binding_request_ids", None)
        if not isinstance(emitted, set):
            emitted = set()
            setattr(self, "_emitted_skill_context_binding_request_ids", emitted)
        if request_id in emitted:
            return
        emitted.add(request_id)
        self._lifecycle_state.record_skill_context_binding(
            request_id=request_id,
            phase=phase,
            bindings=bindings,
        )
        await self._emit(
            "skills.context.bound",
            {
                "task_id": self.id,
                "request_id": request_id,
                "phase": phase,
                "binding_ids": [item["binding_id"] for item in bindings],
                "canonical_skill_ids": [item["canonical_skill_id"] for item in bindings],
                "bindings": bindings,
            },
            meta={
                "task_id": self.id,
                "request_id": request_id,
                "phase": phase,
                "stream_kind": "skills_context_binding",
            },
        )

    def _required_skill_context_selectors(self) -> tuple[list[str], list[str]]:
        skill_ids: list[str] = []
        skill_pack_ids: list[str] = []

        constraints = self.options.get("capability_constraints") if isinstance(self.options, Mapping) else None
        if isinstance(constraints, Mapping):
            skills = constraints.get("skills")
            if isinstance(skills, Mapping):
                for skill_id in self._normalize_string_list(skills.get("required")):
                    if skill_id not in skill_ids:
                        skill_ids.append(skill_id)
            packs = constraints.get("skill_packs")
            if isinstance(packs, Mapping):
                for pack_id in self._normalize_string_list(packs.get("required")):
                    if pack_id not in skill_pack_ids:
                        skill_pack_ids.append(pack_id)

        planner_capabilities = getattr(self, "_planner_capabilities", None)
        capabilities = planner_capabilities() if callable(planner_capabilities) else []
        if isinstance(capabilities, Sequence) and not isinstance(capabilities, str | bytes | bytearray):
            for item in capabilities:
                if not isinstance(item, Mapping):
                    continue
                if str(item.get("mode") or "").strip() != "required":
                    continue
                capability_id = str(item.get("id") or item.get("capability_id") or "").strip()
                if not capability_id:
                    continue
                kind = str(item.get("kind") or "").strip()
                if kind == "skill" and capability_id not in skill_ids:
                    skill_ids.append(capability_id)
                elif kind == "skill_pack" and capability_id not in skill_pack_ids:
                    skill_pack_ids.append(capability_id)
        return skill_ids, skill_pack_ids

    @classmethod
    def _skill_projection_skill_ids(cls, skill_projection: Any) -> list[str]:
        if not isinstance(skill_projection, Mapping):
            return []
        raw_skills = skill_projection.get("skills")
        if not isinstance(raw_skills, Sequence) or isinstance(raw_skills, str | bytes | bytearray):
            return []
        skill_ids: list[str] = []
        for item in raw_skills:
            if not isinstance(item, Mapping):
                continue
            skill_id = str(item.get("skill_id") or item.get("id") or "").strip()
            if skill_id and skill_id not in skill_ids:
                skill_ids.append(skill_id)
        return skill_ids

    def _skills_context_budget_chars(self) -> int:
        return self._option_int(("skills_context_budget_chars", "skills_context_budget"), default=12000)

    def _skills_context_max_resource_chars(self) -> int:
        return self._option_int(("skills_context_max_resource_chars", "skills_context_resource_chars"), default=6000)

    def _option_int(self, names: tuple[str, ...], *, default: int) -> int:
        sources: list[Any] = [self.options]
        agent_task_options = self.options.get("agent_task") if isinstance(self.options, Mapping) else None
        if isinstance(agent_task_options, Mapping):
            sources.append(agent_task_options)
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            for name in names:
                raw_value = source.get(name)
                if raw_value in (None, "", [], {}):
                    continue
                try:
                    return max(0, int(raw_value))
                except (TypeError, ValueError):
                    continue
        return default

    async def async_add_guidance(
        self,
        content: Any,
        *,
        guidance_id: str | None = None,
        author: str | None = None,
        target: Any = "task",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(content, str) and not content.strip():
            raise ValueError("AgentTask guidance content must not be empty.")
        lock = self._ensure_guidance_lock()
        async with lock:
            guidance_ref = self._new_guidance_ref(
                content,
                guidance_id=guidance_id,
                author=author,
                target=target,
                meta=meta,
            )
            terminal = bool(getattr(self, "_completed", False))
            guidance_ref["status"] = "received_after_terminal" if terminal else "received"
            guidance_ref["storage"] = "memory"
            self.guidance_items.append(DataFormatter.sanitize(guidance_ref))
            self._record_guidance_diagnostic(guidance_ref["status"])
            event_name = (
                "agent_task.guidance.ignored"
                if guidance_ref["status"] == "received_after_terminal"
                else "agent_task.guidance.received"
            )
            await self._emit(
                event_name,
                self._guidance_event_payload(guidance_ref),
                meta={
                    "task_id": self.id,
                    "status": self.status,
                    "stream_kind": "guidance",
                    "guidance_status": guidance_ref["status"],
                    "guidance_id": guidance_ref["id"],
                },
            )
            return DataFormatter.sanitize(guidance_ref)

    def add_guidance(self, *args: Any, **kwargs: Any) -> Any:
        return default_stage_call_bridge.as_sync(self.async_add_guidance)(*args, **kwargs)

    async def _apply_guidance_boundary(
        self,
        *,
        iteration_index: int | None = None,
        boundary: str,
        target: Any = None,
    ) -> list[dict[str, Any]]:
        applicable_statuses = {"received", "queued", "forwarded"}
        applied: list[dict[str, Any]] = []
        now = time.time()
        for item in getattr(self, "guidance_items", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "") not in applicable_statuses:
                continue
            if target not in (None, "", "task"):
                guidance_target = item.get("target")
                if guidance_target not in (target, "task"):
                    continue
            item["status"] = "applied"
            item["applied_at"] = now
            item["applied_iteration"] = iteration_index
            item["applied_boundary"] = boundary
            applied.append(DataFormatter.sanitize(item))
        if not applied:
            return []
        self._record_guidance_diagnostic("applied", count=len(applied))
        await self._emit(
            "agent_task.guidance.applied",
            {
                "task_id": self.id,
                "status": "applied",
                "guidance_ids": [item["id"] for item in applied],
                "iteration": iteration_index,
                "boundary": boundary,
                "guidance": self._guidance_context_projection(items=applied),
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration_index,
                "stream_kind": "guidance",
                "guidance_status": "applied",
                "boundary": boundary,
            },
        )
        return applied

    def _context_pack_with_guidance(self, context_pack: Any) -> "TaskContextView":
        if not isinstance(context_pack, Mapping):
            return cast("TaskContextView", context_pack)
        context = dict(context_pack)
        projection = self._guidance_context_projection()
        if projection:
            context["guidance"] = projection
            diagnostics = context.get("diagnostics")
            diagnostics = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
            diagnostics["guidance_count"] = len(projection)
            diagnostics["guidance_ids"] = [item["id"] for item in projection]
            context["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return cast("TaskContextView", DataFormatter.sanitize(context))

    def _guidance_context_projection(
        self,
        *,
        items: Sequence[Mapping[str, Any]] | None = None,
        extra: Sequence[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        source_items: list[Mapping[str, Any]] = []
        if items is None:
            for item in getattr(self, "guidance_items", []) or []:
                if isinstance(item, Mapping):
                    source_items.append(item)
        else:
            source_items.extend(item for item in items if isinstance(item, Mapping))
        if extra is not None:
            source_items.extend(item for item in extra if isinstance(item, Mapping))
        projection: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in source_items:
            guidance_id = str(item.get("id") or "").strip()
            if not guidance_id or guidance_id in seen:
                continue
            status = str(item.get("status") or "")
            if status in {"ignored", "received_after_terminal"}:
                continue
            task_workspace_ref = item.get("task_workspace_ref")
            task_workspace_ref_id = task_workspace_ref.get("id") if isinstance(task_workspace_ref, Mapping) else None
            projection.append(
                DataFormatter.sanitize(
                    {
                        "id": guidance_id,
                        "kind": "guidance",
                        "status": status,
                        "target": item.get("target", "task"),
                        "content_preview": item.get("content_preview"),
                        "task_workspace_ref": task_workspace_ref_id,
                        "applied_iteration": item.get("applied_iteration"),
                        "applied_boundary": item.get("applied_boundary"),
                    }
                )
            )
            seen.add(guidance_id)
        return projection

    def _new_guidance_ref(
        self,
        content: Any,
        *,
        guidance_id: str | None = None,
        author: str | None = None,
        target: Any = "task",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._guidance_sequence = int(getattr(self, "_guidance_sequence", 0)) + 1
        resolved_id = str(guidance_id or "").strip() or f"guidance-{uuid.uuid4().hex}"
        return {
            "id": resolved_id,
            "task_id": self.id,
            "kind": "guidance",
            "sequence": self._guidance_sequence,
            "content": DataFormatter.sanitize(content),
            "content_preview": self._guidance_preview(content),
            "author": str(author or "").strip() or None,
            "target": DataFormatter.sanitize(target or "task"),
            "status": "received",
            "received_at": time.time(),
            "meta": DataFormatter.sanitize(meta or {}),
        }

    def _ensure_guidance_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_guidance_lock", None)
        if lock is None or not hasattr(lock, "acquire"):
            lock = asyncio.Lock()
            self._guidance_lock = lock
        return lock

    @staticmethod
    def _guidance_preview(content: Any) -> str:
        text = str(content if content is not None else "").strip()
        if len(text) <= _GUIDANCE_PREVIEW_CHARS:
            return text
        return text[: max(0, _GUIDANCE_PREVIEW_CHARS - 16)].rstrip() + " [truncated]"

    def _record_guidance_diagnostic(self, status: str, *, count: int = 1) -> None:
        diagnostics = self.diagnostics.setdefault("guidance", {})
        if not isinstance(diagnostics, dict):
            diagnostics = {}
            self.diagnostics["guidance"] = diagnostics
        key = str(status or "received")
        diagnostics[key] = int(diagnostics.get(key) or 0) + count
        diagnostics["total"] = len([item for item in getattr(self, "guidance_items", []) if isinstance(item, dict)])

    @staticmethod
    def _guidance_event_payload(guidance_ref: Mapping[str, Any]) -> dict[str, Any]:
        return DataFormatter.sanitize(
            {
                "task_id": guidance_ref.get("task_id"),
                "guidance_id": guidance_ref.get("id"),
                "kind": "guidance",
                "status": guidance_ref.get("status"),
                "target": guidance_ref.get("target"),
                "content_preview": guidance_ref.get("content_preview"),
                "task_workspace_ref": guidance_ref.get("task_workspace_ref"),
                "checkpoint_ref": guidance_ref.get("checkpoint_ref"),
            }
        )


__all__ = ["AgentTaskGuidanceMixin"]
