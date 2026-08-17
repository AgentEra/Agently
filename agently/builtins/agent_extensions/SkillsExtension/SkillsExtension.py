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

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from typing_extensions import Self

from agently.core import BaseAgent
from agently.core.application.SkillLibrary import SkillBinding, SkillPackageRevision
from agently.types.data import SkillMode, SkillScriptAuthorization
from agently.utils.DataGuardian import _copy_public, _ensure_dict, _ensure_list

from .SkillActionBinder import BoundSkillAction, SkillActionBinder


@dataclass(frozen=True)
class SkillRunCompatibilityResult:
    """Released result-shaped view over one ordinary AgentExecution."""

    execution: Any
    output: Any

    @property
    def status(self) -> str:
        return str(getattr(self.execution, "status", "success"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": str(getattr(self.execution, "id", "")),
            "status": self.status,
            "output": self.output,
        }


class SkillsExtension(BaseAgent):
    """Agent-facing Skill intent and compatibility API.

    Installed package truth belongs to SkillLibrary. Task-scoped selection and
    binding belong to AgentExecution. This extension only records intent and
    adapts the released convenience calls to those owners.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        from agently.base import skill_library

        self.skill_library = skill_library
        self.__session_skill_selectors: list[dict[str, Any]] = []
        self.__session_skills_pack_selectors: list[dict[str, Any]] = []

    def bind_skill_script_action(
        self,
        execution: Any,
        *,
        binding_id: str,
        resource_path: str,
        authorization: SkillScriptAuthorization,
    ) -> BoundSkillAction:
        bindings = getattr(execution, "skill_bindings", None)
        if not isinstance(bindings, list) or not bindings:
            raise RuntimeError(
                "Prepare the AgentExecution TaskContext before binding a Skill script Action."
            )
        matches = [
            binding
            for binding in bindings
            if isinstance(binding, SkillBinding)
            and binding.binding_id == str(binding_id)
        ]
        if len(matches) != 1:
            raise ValueError("binding_id must identify one exact Skill binding.")
        return SkillActionBinder(self.skill_library).bind(
            execution=execution,
            skill_binding=matches[0],
            resource_path=resource_path,
            authorization=authorization,
        )

    def use_skills(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
        always: bool = False,
    ) -> "Self | Any":
        if not always:
            return self.create_execution().use_skills(
                skills,
                mode=mode,
                auto_allow=auto_allow,
            )
        self._add_skill_selectors(skills, mode=mode, auto_allow=auto_allow)
        return self

    def require_skills(
        self,
        skills: Any,
        *,
        auto_allow: bool = False,
        always: bool = False,
    ) -> "Self | Any":
        return self.use_skills(
            skills,
            mode="required",
            auto_allow=auto_allow,
            always=always,
        )

    def use_skills_packs(
        self,
        skills_packs: Any,
        *,
        mode: SkillMode = "model_decision",
        always: bool = False,
    ) -> "Self | Any":
        if not always:
            return self.create_execution().use_skills_packs(skills_packs, mode=mode)
        self._validate_mode(mode)
        self.__session_skills_pack_selectors.extend(
            {"selector": _copy_public(item), "mode": mode}
            for item in _ensure_list(skills_packs)
        )
        return self

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in {"model_decision", "required"}:
            raise ValueError("Skill mode must be 'model_decision' or 'required'.")

    def _normalize_skill_selector_entries(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
    ) -> list[dict[str, Any]]:
        self._validate_mode(mode)
        entries: list[dict[str, Any]] = []
        for item in _ensure_list(skills):
            selector = _copy_public(item)
            if auto_allow:
                if isinstance(selector, Mapping):
                    selector = {**dict(selector), "auto_allow": True}
                else:
                    selector = {"id": selector, "auto_allow": True}
            entries.append({"selector": selector, "mode": mode})
        return entries

    def _add_skill_selectors(
        self,
        skills: Any,
        *,
        mode: SkillMode = "model_decision",
        auto_allow: bool = False,
    ) -> list[dict[str, Any]]:
        entries = self._normalize_skill_selector_entries(
            skills,
            mode=mode,
            auto_allow=auto_allow,
        )
        self.__session_skill_selectors.extend(entries)
        return entries

    def _collect_skill_selectors(self, *, skills: Any, mode: SkillMode) -> list[Any]:
        self._validate_mode(mode)
        selectors = list(_ensure_list(skills)) if skills is not None else []
        selectors.extend(
            item.get("selector")
            for item in self.__session_skill_selectors
            if item.get("mode") == mode
        )
        return selectors

    def _collect_skills_pack_selectors(
        self,
        *,
        skills_packs: Any,
        mode: SkillMode,
    ) -> list[Any]:
        self._validate_mode(mode)
        selectors = list(_ensure_list(skills_packs)) if skills_packs is not None else []
        selectors.extend(
            item.get("selector")
            for item in self.__session_skills_pack_selectors
            if item.get("mode") == mode
        )
        return selectors

    @staticmethod
    def _selector_id(selector: Any) -> str:
        if isinstance(selector, str):
            return selector.strip()
        if isinstance(selector, Mapping):
            return str(
                selector.get("skill_id")
                or selector.get("skill_pack_id")
                or selector.get("skills_pack_id")
                or selector.get("id")
                or selector.get("name")
                or ""
            ).strip()
        return ""

    def _resolve_packages(
        self,
        selectors: Sequence[Any],
        *,
        required: bool,
        diagnostics: list[dict[str, Any]],
    ) -> list[SkillPackageRevision]:
        packages: list[SkillPackageRevision] = []
        seen: set[str] = set()
        for selector in selectors:
            selector_id = self._selector_id(selector)
            if not selector_id:
                message = "A Skill selector must identify one installed Skill revision."
                if required:
                    raise RuntimeError(message)
                diagnostics.append(
                    {
                        "code": "skills.selector.invalid",
                        "message": message,
                        "selector": _copy_public(selector),
                    }
                )
                continue
            try:
                package = self.skill_library.resolve(selector_id)
            except (KeyError, ValueError) as error:
                if required:
                    raise RuntimeError(
                        f"Required Skill {selector_id!r} is unavailable: {error}"
                    ) from error
                diagnostics.append(
                    {
                        "code": "skills.selector.unavailable",
                        "message": str(error),
                        "selector": selector_id,
                    }
                )
                continue
            if package.trust != "trusted":
                message = (
                    f"Skill {package.revision_ref!r} cannot be bound because its "
                    "installed revision is not trusted."
                )
                if required:
                    raise RuntimeError(message)
                diagnostics.append(
                    {
                        "code": "skills.selector.untrusted",
                        "message": message,
                        "selector": selector_id,
                    }
                )
                continue
            if package.revision_ref not in seen:
                packages.append(package)
                seen.add(package.revision_ref)
        return packages

    def _resolve_pack_packages(
        self,
        selectors: Sequence[Any],
        *,
        required: bool,
        diagnostics: list[dict[str, Any]],
    ) -> list[SkillPackageRevision]:
        packages: list[SkillPackageRevision] = []
        seen: set[str] = set()
        for selector in selectors:
            pack_id = self._selector_id(selector)
            if not pack_id:
                message = "A Skill pack selector must identify one installed library pack."
                if required:
                    raise RuntimeError(message)
                diagnostics.append(
                    {
                        "code": "skills.pack_selector.invalid",
                        "message": message,
                        "selector": _copy_public(selector),
                    }
                )
                continue
            try:
                pack = self.skill_library.inspect_pack(pack_id)
                resolved = [
                    self.skill_library.resolve(revision_ref)
                    for revision_ref in pack.revision_refs
                ]
            except (KeyError, ValueError) as error:
                if required:
                    raise RuntimeError(
                        f"Required Skill pack {pack_id!r} is unavailable: {error}"
                    ) from error
                diagnostics.append(
                    {
                        "code": "skills.pack_selector.unavailable",
                        "message": str(error),
                        "selector": pack_id,
                    }
                )
                continue
            for package in resolved:
                if package.trust != "trusted":
                    message = (
                        f"Skill {package.revision_ref!r} from pack {pack_id!r} cannot "
                        "be bound because its installed revision is not trusted."
                    )
                    if required:
                        raise RuntimeError(message)
                    diagnostics.append(
                        {
                            "code": "skills.pack_selector.untrusted",
                            "message": message,
                            "selector": pack_id,
                        }
                    )
                    continue
                if package.revision_ref not in seen:
                    packages.append(package)
                    seen.add(package.revision_ref)
        return packages

    async def _async_select_optional_packages(
        self,
        *,
        task: str,
        packages: Sequence[SkillPackageRevision],
        diagnostics: list[dict[str, Any]],
    ) -> list[SkillPackageRevision]:
        if not packages:
            return []
        request_factory = getattr(self, "create_temp_request", None)
        if not callable(request_factory):
            diagnostics.append(
                {
                    "code": "skills.selection.unavailable",
                    "message": "No model request factory is available for Skill applicability selection.",
                }
            )
            return []
        offered = {
            f"skill-option:{index}": package
            for index, package in enumerate(packages, start=1)
        }
        cards = [
            {
                "skill_key": key,
                "name": package.name,
                "description": package.description,
                "version": package.version,
            }
            for key, package in offered.items()
        ]
        try:
            request = cast(Any, request_factory())
            result = await (
                request
                .input({"task": task})
                .info({"offered_skills": cards})
                .instruct(
                    "Select only installed Skills whose real-world procedure is useful "
                    "for this task. Return only offered skill_key values. Do not copy "
                    "package identity, paths, revisions, metadata, or instructions."
                )
                .output(
                    {
                        "selected_keys": (
                            [str],
                            "Ordered subset of offered skill_key values.",
                            True,
                        )
                    },
                    format="json",
                )
                .async_get_data()
            )
        except Exception as error:
            diagnostics.append(
                {
                    "code": "skills.selection.failed",
                    "message": str(error),
                    "error_type": error.__class__.__name__,
                }
            )
            return []
        if not isinstance(result, Mapping) or not isinstance(result.get("selected_keys"), list):
            diagnostics.append(
                {
                    "code": "skills.selection.invalid",
                    "message": "Skill applicability selection returned an invalid output shape.",
                }
            )
            return []
        keys = result["selected_keys"]
        if (
            any(not isinstance(key, str) or key not in offered for key in keys)
            or len(keys) != len(set(keys))
        ):
            diagnostics.append(
                {
                    "code": "skills.selection.invalid",
                    "message": "Skill applicability selection returned unknown or duplicate offered keys.",
                }
            )
            return []
        return [offered[key] for key in keys]

    async def async_bind_skills_for_execution(self, execution: Any) -> list[SkillBinding]:
        diagnostics: list[dict[str, Any]] = []
        required_selectors = self._collect_skill_selectors(skills=None, mode="required")
        optional_selectors = self._collect_skill_selectors(
            skills=None,
            mode="model_decision",
        )
        required_selectors.extend(
            item.get("selector")
            for item in execution.local_skill_selectors
            if item.get("mode") == "required"
        )
        optional_selectors.extend(
            item.get("selector")
            for item in execution.local_skill_selectors
            if item.get("mode") == "model_decision"
        )
        required_packs = self._collect_skills_pack_selectors(
            skills_packs=None,
            mode="required",
        )
        required_packs.extend(
            item.get("selector")
            for item in execution.local_skills_pack_selectors
            if item.get("mode") == "required"
        )
        optional_packs = self._collect_skills_pack_selectors(
            skills_packs=None,
            mode="model_decision",
        )
        optional_packs.extend(
            item.get("selector")
            for item in execution.local_skills_pack_selectors
            if item.get("mode") == "model_decision"
        )
        required_packages = self._resolve_packages(
            required_selectors,
            required=True,
            diagnostics=diagnostics,
        )
        required_packages.extend(
            self._resolve_pack_packages(
                required_packs,
                required=True,
                diagnostics=diagnostics,
            )
        )
        required_packages = list(
            {package.revision_ref: package for package in required_packages}.values()
        )
        required_refs = {package.revision_ref for package in required_packages}
        optional_candidates = self._resolve_packages(
            optional_selectors,
            required=False,
            diagnostics=diagnostics,
        )
        optional_candidates.extend(
            self._resolve_pack_packages(
                optional_packs,
                required=False,
                diagnostics=diagnostics,
            )
        )
        optional_packages = [
            package
            for package in {
                item.revision_ref: item for item in optional_candidates
            }.values()
            if package.revision_ref not in required_refs
        ]
        selected_optional = await self._async_select_optional_packages(
            task=execution.task_target(),
            packages=optional_packages,
            diagnostics=diagnostics,
        )
        bindings: list[SkillBinding] = []
        for mode, packages in (
            ("required", required_packages),
            ("model_decision", selected_optional),
        ):
            for package in packages:
                bindings.append(
                    SkillBinding.create(
                        package,
                        task_id=execution.id,
                        mode=cast(Any, mode),
                        binding_id=f"skill_binding:{execution.id}:{len(bindings) + 1}",
                    )
                )
        status = "selected" if bindings else "none"
        if any(item.get("code") == "skills.selection.invalid" for item in diagnostics):
            status = "invalid"
        execution.diagnostics["skill_selection"] = {
            "status": status,
            "binding_ids": [binding.binding_id for binding in bindings],
            "revision_refs": [binding.revision_ref for binding in bindings],
            "diagnostics": diagnostics,
        }
        return bindings

    @staticmethod
    def _prompt_defaults(task: str | None, output: Any, semantic_outputs: Any) -> tuple[str, Any]:
        if output is not None and semantic_outputs is not None:
            raise ValueError("Use either output= or semantic_outputs=, not both.")
        normalized_task = str(task or "").strip()
        if not normalized_task:
            raise ValueError("Skill execution requires a non-empty task.")
        return normalized_task, output if output is not None else semantic_outputs

    async def async_resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Any = None,
        _settings_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del _settings_overrides
        task, output = self._prompt_defaults(task, output, semantic_outputs)
        execution = self.create_execution().input(task)
        if output is not None:
            execution.output(output, format=output_format)
        if skills is not None:
            execution.use_skills(skills, mode=mode)
        if skills_packs is not None:
            execution.use_skills_packs(skills_packs, mode=mode)
        await execution.async_prepare_task_context()
        return await self._async_project_skill_binding_plan(execution, mode=mode)

    async def _async_project_skill_binding_plan(
        self,
        execution: Any,
        *,
        mode: SkillMode,
    ) -> dict[str, Any]:
        route, route_meta = await execution.select_route()
        selected = []
        for binding in execution.skill_bindings:
            package = self.skill_library.resolve(binding.revision_ref)
            selected.append(
                {
                    "skill_id": package.skill_id,
                    "name": package.name,
                    "revision_ref": binding.revision_ref,
                    "binding_id": binding.binding_id,
                    "mode": binding.mode,
                }
            )
        return {
            "schema_version": "agently.skill_binding_plan.compat.v2",
            "plan_id": f"skill_binding_plan:{execution.id}",
            "status": "resolved" if selected else "no_match",
            "mode": mode,
            "selected_skills": selected,
            "route_preview": {
                "selected_route": route,
                "route_meta": route_meta,
            },
            "task_context_id": execution.task_context.context_id,
            "diagnostics": execution.diagnostics.get("skill_selection", {}),
        }

    def resolve_skills_plan(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return default_stage_call_bridge.as_sync(self.async_resolve_skills_plan)(*args, **kwargs)

    async def async_run_skills_task(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: SkillMode = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Any = None,
        stream_handler: Any = None,
        effort: str | None = None,
        _settings_overrides: dict[str, Any] | None = None,
    ) -> SkillRunCompatibilityResult:
        del _settings_overrides
        task, output = self._prompt_defaults(task, output, semantic_outputs)
        execution = self.create_execution().input(task)
        if output is not None:
            execution.output(output, format=output_format)
        if skills is not None:
            execution.use_skills(skills, mode=mode)
        if skills_packs is not None:
            execution.use_skills_packs(skills_packs, mode=mode)
        if effort is not None and callable(getattr(execution, "effort", None)):
            execution.effort(effort)
        result = await execution.async_get_data()
        if stream_handler is not None:
            handled = stream_handler(
                {
                    "path": "result",
                    "data": result,
                    "is_complete": True,
                    "source": "agent_execution",
                }
            )
            if inspect.isawaitable(handled):
                await handled
        return SkillRunCompatibilityResult(execution=execution, output=result)

    def run_skills_task(self, *args: Any, **kwargs: Any) -> SkillRunCompatibilityResult:
        return default_stage_call_bridge.as_sync(self.async_run_skills_task)(*args, **kwargs)


__all__ = ["SkillRunCompatibilityResult", "SkillsExtension"]
