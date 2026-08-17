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

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agently.core.application.SkillLibrary import (
    ParsedSkillPackage,
    SkillBinding,
    SkillContextSource,
    SkillLibrary,
    SkillPackRevision,
    SkillPackageRevision,
)
from agently.core.context import TaskContext
from agently.types.data import ContextBudget, ContextReadIntent, SkillSourceRequest

if TYPE_CHECKING:
    from agently.core import PluginManager
    from agently.utils import Settings


def _trust_from_compatibility(
    value: str | None,
    *,
    default: str = "local",
) -> str:
    normalized = str(value or default).strip().lower()
    if normalized in {"local", "trusted"}:
        return "trusted"
    return normalized or "untrusted"


def _contract(
    package: SkillPackageRevision,
    *,
    pack: SkillPackRevision | None = None,
) -> dict[str, Any]:
    resources = [
        resource.to_dict()
        for resource in package.resources
        if resource.kind in {"reference", "example", "asset", "script"}
    ]
    keywords_value = package.frontmatter.get("keywords", ())
    keywords = (
        [str(item) for item in keywords_value if str(item).strip()]
        if isinstance(keywords_value, (list, tuple))
        else []
    )
    checksum = package.revision.removeprefix("sha256:")
    decision_card = {
        "skill_id": package.skill_id,
        "name": package.name,
        "description": package.description,
        "keywords": keywords,
        "guidance_excerpt": package.instruction_body[:1000],
        "resource_summary": [
            {
                "path": resource["path"],
                "kind": resource["kind"],
                "size": resource["size"],
            }
            for resource in resources
        ],
        "checksum": checksum,
    }
    diagnostics = []
    if not package.description:
        diagnostics.append(
            {
                "code": "missing_description",
                "message": "SKILL.md frontmatter has no description.",
            }
        )
    return {
        "skill_id": package.skill_id,
        "display_name": package.name,
        "description": package.description,
        "skills_pack_id": pack.skill_pack_id if pack is not None else "",
        "skills_pack_name": pack.name if pack is not None else "",
        "version": package.version,
        "source": {
            "source": package.source,
            "source_type": "skill_library",
            "installed_path": package.installed_path,
            "canonical_ref": package.canonical_ref,
            "revision_ref": package.revision_ref,
            "skills_pack_id": pack.skill_pack_id if pack is not None else "",
            "skills_pack_name": pack.name if pack is not None else "",
        },
        "trust_level": package.trust,
        "card": {
            "display_name": package.name,
            "name": package.name,
            "description": package.description,
            "activation_hints": {"keywords": keywords},
        },
        "decision_card": decision_card,
        "guidance": {
            "path": "SKILL.md",
            "content": package.instruction_body,
        },
        "assets": {"skill_root": package.installed_path},
        "resource_index": {"resources": resources},
        "checksums": {
            "root_checksum": checksum,
            "files": {
                resource.path: resource.sha256 for resource in package.resources
            },
        },
        "metadata": {
            "frontmatter": dict(package.frontmatter),
            "scope": package.scope,
            "revision": package.revision,
            "skill_format": "anthropic-skill",
        },
        "diagnostics": diagnostics,
    }


def _pack_contract(pack: SkillPackRevision) -> dict[str, Any]:
    return pack.to_dict()


def _discovery_contract(package: ParsedSkillPackage) -> dict[str, Any]:
    return {
        "skill_id": package.skill_id,
        "version": package.version,
        "card": {
            "display_name": package.name,
            "name": package.name,
            "description": package.description,
        },
        "guidance": {"path": "SKILL.md", "content": package.instruction_body},
        "resource_index": {
            "resources": [
                item.to_dict()
                for item in package.resources
                if item.kind in {"reference", "example", "asset", "script"}
            ]
        },
        "checksums": {"root_checksum": package.revision.removeprefix("sha256:")},
        "metadata": {
            "frontmatter": dict(package.frontmatter),
            "revision": package.revision,
            "skill_format": "anthropic-skill",
        },
    }


class SkillsCompatibilityRegistry:
    """Released registry-shaped projection over the immutable SkillLibrary."""

    def __init__(self, library: SkillLibrary) -> None:
        self.library = library

    def _pack_for_revision(
        self,
        package: SkillPackageRevision,
    ) -> SkillPackRevision | None:
        if package.scope and package.scope != "explicit":
            try:
                scoped_pack = self.library.inspect_pack(package.scope)
            except (KeyError, FileNotFoundError, ValueError):
                scoped_pack = None
            if (
                scoped_pack is not None
                and package.revision_ref in scoped_pack.revision_refs
            ):
                return scoped_pack
        for pack in self.library.list_packs():
            if package.revision_ref in pack.revision_refs:
                return pack
        return None

    def _contract(self, package: SkillPackageRevision) -> dict[str, Any]:
        return _contract(
            package,
            pack=self._pack_for_revision(package),
        )

    def install_skills(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> dict[str, Any]:
        del source_type, update
        package = self.library.install(
            source,
            scope="explicit",
            trust=_trust_from_compatibility(trust_level),
        )
        return self._contract(package)

    def inspect_skills(self, skill_id: str) -> dict[str, Any]:
        return self._contract(self.library.resolve(skill_id))

    def list_skills(self) -> list[dict[str, Any]]:
        return [self._contract(package) for package in self.library.list()]

    def install_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        trust_level: str | None = None,
    ) -> dict[str, Any]:
        return _pack_contract(
            self.library.install_pack(
                source,
                skill_pack_id=skills_pack_id,
                name=name,
                trust=_trust_from_compatibility(trust_level),
            )
        )

    def list_skills_packs(self) -> list[dict[str, Any]]:
        return [_pack_contract(pack) for pack in self.library.list_packs()]

    def inspect_skills_pack(self, skills_pack_id: str) -> dict[str, Any]:
        return _pack_contract(self.library.inspect_pack(skills_pack_id))

    def discover_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        trust_level: str | None = None,
    ) -> dict[str, Any]:
        packages = self.library.discover_pack(source)
        pack_id = self.library.normalize_pack_id(
            skills_pack_id or name or Path(source).expanduser().resolve().name
        )
        return {
            "skill_pack_id": pack_id,
            "skills_pack_id": pack_id,
            "name": str(name or skills_pack_id or pack_id),
            "source": str(source),
            "source_type": "local",
            "trust_level": _trust_from_compatibility(trust_level),
            "contracts": [_discovery_contract(package) for package in packages],
            "failed_skills": [],
            "status": "success" if packages else "error",
        }

    def read_resource(
        self,
        skill_id: str,
        path: str,
        *,
        max_bytes: int = 262144,
    ) -> str:
        package = self.library.resolve(skill_id)
        return self.library.read_resource(package, path, max_bytes=max_bytes).text


class SkillsExecutor:
    """Thin compatibility facade over SkillLibrary and generic Context reading."""

    def __init__(
        self,
        plugin_manager: "PluginManager | None" = None,
        settings: "Settings | None" = None,
        *,
        library: SkillLibrary | None = None,
    ) -> None:
        self.plugin_manager = plugin_manager
        self.settings = settings
        if library is None:
            root = (
                settings.get("skills.library.root", ".agently/skill-library")
                if settings is not None
                else ".agently/skill-library"
            )
            library = SkillLibrary(str(root))
        self.library = library
        self.registry = SkillsCompatibilityRegistry(library)
        self._allowed_trust_levels: frozenset[str] | None = None

    def configure(
        self,
        *,
        registry_root: str | Path | None = None,
        allowed_trust_levels: list[str] | None = None,
    ) -> "SkillsExecutor":
        if registry_root is not None:
            self.library.configure(root=registry_root)
        self._allowed_trust_levels = (
            frozenset(_trust_from_compatibility(item) for item in allowed_trust_levels)
            if allowed_trust_levels is not None
            else None
        )
        return self

    def install_skills(
        self,
        source: str | Path,
        *,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> dict[str, Any]:
        resolved_trust = _trust_from_compatibility(trust_level)
        if (
            self._allowed_trust_levels is not None
            and resolved_trust not in self._allowed_trust_levels
        ):
            raise ValueError(
                f"Skill trust level is not allowed by this facade: {resolved_trust!r}."
            )
        return self.registry.install_skills(
            source,
            source_type=source_type,
            trust_level=trust_level,
            update=update,
        )

    def inspect_skills(self, skill_id: str) -> dict[str, Any]:
        return self.registry.inspect_skills(skill_id)

    def install_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        fetch: bool = False,
        ref: str | None = None,
        subpath: str | None = None,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = True,
        discover: str = "auto",
        resolver_mode: str = "deterministic",
        resolver_agent: Any = None,
    ) -> dict[str, Any]:
        del discover, resolver_mode, resolver_agent
        uses_source_provider = bool(
            fetch
            or ref
            or subpath
            or source_type not in {None, "local", "path"}
        )
        effective_source_type = str(
            source_type or ("git" if uses_source_provider else "local")
        ).strip().lower()
        resolved_trust = _trust_from_compatibility(
            trust_level,
            default=(
                "local"
                if effective_source_type in {"local", "path"}
                else "untrusted"
            ),
        )
        if (
            self._allowed_trust_levels is not None
            and resolved_trust not in self._allowed_trust_levels
        ):
            raise ValueError(
                f"Skill trust level is not allowed by this facade: {resolved_trust!r}."
            )
        if uses_source_provider:
            pack = self.library.install_pack_source(
                SkillSourceRequest(
                    source=str(source),
                    source_type=effective_source_type,
                    ref=ref,
                    subpath=subpath,
                    update=update,
                ),
                name=name,
                skill_pack_id=skills_pack_id,
                trust=resolved_trust,
            )
            return _pack_contract(pack)
        return self.registry.install_skills_pack(
            source,
            name=name,
            skills_pack_id=skills_pack_id,
            trust_level=trust_level,
        )

    def list_skills_packs(self) -> list[dict[str, Any]]:
        return self.registry.list_skills_packs()

    def inspect_skills_pack(self, skills_pack_id: str) -> dict[str, Any]:
        return self.registry.inspect_skills_pack(skills_pack_id)

    def discover_skills_pack(
        self,
        source: str | Path,
        *,
        name: str | None = None,
        skills_pack_id: str | None = None,
        fetch: bool = False,
        ref: str | None = None,
        subpath: str | None = None,
        source_type: str | None = None,
        trust_level: str | None = None,
        update: bool = False,
    ) -> dict[str, Any]:
        del update
        if fetch or ref or subpath or source_type not in {None, "local", "path"}:
            raise ValueError(
                "The SkillLibrary compatibility facade discovers local Skill packs only; "
                "materialize remote sources before discovery."
            )
        return self.registry.discover_skills_pack(
            source,
            name=name,
            skills_pack_id=skills_pack_id,
            trust_level=trust_level,
        )

    def list_skills(self) -> list[dict[str, Any]]:
        return self.registry.list_skills()

    def read_resource(
        self,
        skill_id: str,
        path: str,
        *,
        max_bytes: int = 262144,
    ) -> str:
        return self.registry.read_resource(skill_id, path, max_bytes=max_bytes)

    @staticmethod
    def _selectors(
        skill_ids: Sequence[str] | None,
        skills: Any,
    ) -> tuple[str, ...]:
        selected: list[str] = [str(item) for item in (skill_ids or ())]
        if isinstance(skills, str):
            selected.append(skills)
        elif isinstance(skills, Sequence) and not isinstance(skills, bytes):
            for item in skills:
                if isinstance(item, str):
                    selected.append(item)
                elif isinstance(item, Mapping):
                    selector = item.get("skill_id") or item.get("id") or item.get("name")
                    if selector:
                        selected.append(str(selector))
        return tuple(dict.fromkeys(item.strip() for item in selected if item.strip()))

    @staticmethod
    def _pack_selectors(skills_packs: Any) -> tuple[str, ...]:
        selected: list[str] = []
        if isinstance(skills_packs, str):
            selected.append(skills_packs)
        elif isinstance(skills_packs, Sequence) and not isinstance(skills_packs, bytes):
            for item in skills_packs:
                if isinstance(item, str):
                    selected.append(item)
                elif isinstance(item, Mapping):
                    selector = (
                        item.get("skill_pack_id")
                        or item.get("skills_pack_id")
                        or item.get("id")
                        or item.get("name")
                    )
                    if selector:
                        selected.append(str(selector))
        return tuple(dict.fromkeys(item.strip() for item in selected if item.strip()))

    @staticmethod
    def _explicit_resource_refs(
        packages: Sequence[SkillPackageRevision],
        *,
        include_examples: Any,
        include_references: Any,
        include_assets: Any,
    ) -> tuple[str, ...]:
        included_kinds: set[str] = set()
        if include_examples is True:
            included_kinds.add("example")
        if include_references is True:
            included_kinds.add("reference")
        if include_assets is True:
            included_kinds.add("asset")
        return tuple(
            f"{package.revision_ref}/{resource.path}"
            for package in packages
            for resource in package.resources
            if resource.kind in included_kinds
        )

    @staticmethod
    def _project_context_pack(
        *,
        package: Any,
        installed: Sequence[SkillPackageRevision],
        include_guidance: bool,
        actionize_scripts: bool,
        include_public_lookup: bool,
    ) -> dict[str, Any]:
        projected: dict[str, dict[str, Any]] = {
            skill.revision_ref: {
                "skill_id": skill.skill_id,
                "revision_ref": skill.revision_ref,
                "guidance": None,
                "selected_resources": [],
                "action_candidates": [],
            }
            for skill in installed
        }
        for block in package.blocks:
            revision_ref = str(block.metadata.get("revision_ref") or "")
            target = projected.get(revision_ref)
            if target is None:
                continue
            path = str(block.metadata.get("resource_path") or "")
            if path == "SKILL.md":
                if include_guidance:
                    target["guidance"] = {
                        "path": path,
                        "excerpt": block.content,
                        "completeness": block.completeness,
                    }
                continue
            if path == "resource-index" or not path:
                continue
            target["selected_resources"].append(
                {
                    "path": path,
                    "kind": block.metadata.get("resource_kind"),
                    "content": block.content,
                    "completeness": block.completeness,
                    "source_ref": block.source_ref,
                }
            )
        diagnostics = [diagnostic.to_dict() for diagnostic in package.diagnostics]
        if actionize_scripts:
            diagnostics.append(
                {
                    "code": "skills.compat.actionize_scripts_ignored",
                    "message": "Skill scripts remain capability descriptors; this facade cannot execute them.",
                    "details": {},
                }
            )
        if include_public_lookup:
            diagnostics.append(
                {
                    "code": "skills.compat.public_lookup_unavailable",
                    "message": "Public discovery is outside the installed SkillLibrary boundary.",
                    "details": {},
                }
            )
        return {
            "schema_version": "agently.skills.context_pack.compat.v2",
            "context_package_id": package.package_id,
            "task_context_id": package.task_context_id,
            "context_revision": package.context_revision,
            "source_revisions": dict(package.source_revisions),
            "skills": list(projected.values()),
            "omissions": [omission.to_dict() for omission in package.omissions],
            "diagnostics": diagnostics,
            "used_chars": package.used_chars,
        }

    async def async_build_context_pack(
        self,
        *,
        context: Any = None,
        task: str | None = None,
        intent: str | None = None,
        skill_ids: Sequence[str] | None = None,
        skills: Any = None,
        skills_packs: Any = None,
        include_guidance: bool = True,
        include_examples: Any = "auto",
        include_references: Any = "auto",
        include_assets: Any = False,
        include_public_lookup: bool = False,
        actionize_scripts: bool = False,
        budget_chars: int = 12000,
        max_resource_chars: int = 6000,
    ) -> dict[str, Any]:
        del context
        selectors = list(self._selectors(skill_ids, skills))
        for pack_selector in self._pack_selectors(skills_packs):
            selectors.extend(self.library.inspect_pack(pack_selector).revision_refs)
        selectors = list(dict.fromkeys(selectors))
        if not selectors:
            raise ValueError("At least one explicit Skill or Skill pack selector is required.")
        packages = tuple(self.library.resolve(selector) for selector in selectors)
        compatibility_id = f"skills_compat:{uuid.uuid4().hex}"
        task_context = TaskContext(
            task_id=compatibility_id,
            context_id=compatibility_id,
        )
        bindings = tuple(
            SkillBinding.create(package, task_id=task_context.task_id, mode="required")
            for package in packages
        )
        task_context.attach(
            SkillContextSource(self.library, bindings=bindings),
            required=True,
            scope="compatibility",
        )
        explicit_refs = self._explicit_resource_refs(
            packages,
            include_examples=include_examples,
            include_references=include_references,
            include_assets=include_assets,
        )
        reader = task_context.reader(
            consumer="skills_executor.compatibility",
            phase="execution",
            budget=ContextBudget(
                max_chars=budget_chars,
                max_blocks=max(2, sum(len(item.resources) + 2 for item in packages)),
                max_block_chars=max_resource_chars,
            ),
        )
        context_package = await reader.async_read(
            ContextReadIntent(
                query=str(intent or task or "Build Skill context"),
                explicit_refs=explicit_refs,
            )
        )
        return self._project_context_pack(
            package=context_package,
            installed=packages,
            include_guidance=include_guidance,
            actionize_scripts=actionize_scripts,
            include_public_lookup=include_public_lookup,
        )

    def build_context_pack(self, **kwargs: Any) -> dict[str, Any]:
        return default_stage_call_bridge.as_sync(self.async_build_context_pack)(**kwargs)

    def task_dag_resolver(
        self,
        *,
        context: Any = None,
        defaults: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        configured = dict(defaults or {})

        async def build(task_context: Any) -> dict[str, Any]:
            values = dict(configured)
            if isinstance(task_context, Mapping):
                values.update(dict(task_context))
            elif task_context is not None:
                values.setdefault("task", str(task_context))
            values.setdefault("context", context)
            return await self.async_build_context_pack(**values)

        return {"skill": build}


__all__ = ["SkillsCompatibilityRegistry", "SkillsExecutor"]
