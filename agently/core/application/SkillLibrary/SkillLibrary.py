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

import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import SkillSourceRequest
from agently.types.plugins import SkillSourceProvider

from .Package import SkillPackageRevision, SkillPackRevision, SkillResourceRead
from .Parser import ParsedSkillPackage, SkillPackageError, parse_skill_package
from .Store import SkillPackageStore

if TYPE_CHECKING:
    from agently.core.extension.PluginManager import PluginManager


class SkillLibrary:
    """Installed real-world Skill package truth, independent from task execution."""

    def __init__(
        self,
        root: str | Path = ".agently/skill-library",
        *,
        plugin_manager: "PluginManager | None" = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.store = SkillPackageStore(self.root)
        self.plugin_manager = plugin_manager
        self._source_providers: dict[str, SkillSourceProvider] = {}
        self._library_owned_source_provider_ids: set[int] = set()
        from agently.builtins.plugins.SkillSourceProvider import (
            LocalPathSkillSourceProvider,
        )

        local_provider = LocalPathSkillSourceProvider(
            cache_root=self.root / "source-cache"
        )
        self.register_source_provider(local_provider)
        self._library_owned_source_provider_ids.add(id(local_provider))
        self.install_source = default_stage_call_bridge.as_sync(self.async_install_source)
        self.install_pack_source = default_stage_call_bridge.as_sync(
            self.async_install_pack_source
        )

    def configure(self, *, root: str | Path) -> "SkillLibrary":
        """Rebind the canonical library service without invalidating its holders."""

        self.root = Path(root).expanduser().resolve()
        self.store = SkillPackageStore(self.root)
        from agently.builtins.plugins.SkillSourceProvider import (
            LocalPathSkillSourceProvider,
        )

        for provider in tuple(dict.fromkeys(self._source_providers.values())):
            if (
                id(provider) not in self._library_owned_source_provider_ids
                or not isinstance(provider, LocalPathSkillSourceProvider)
            ):
                continue
            replacement = LocalPathSkillSourceProvider(
                cache_root=self.root / "source-cache",
                max_files=provider.max_files,
                max_bytes=provider.max_bytes,
            )
            for source_type, registered in tuple(self._source_providers.items()):
                if registered is provider:
                    self._source_providers[source_type] = replacement
            self._library_owned_source_provider_ids.discard(id(provider))
            self._library_owned_source_provider_ids.add(id(replacement))
        return self

    def register_source_provider(
        self,
        provider: SkillSourceProvider,
        *,
        replace: bool = True,
    ) -> "SkillLibrary":
        provider_id = str(getattr(provider, "provider_id", "")).strip()
        source_types = tuple(
            str(item).strip().lower()
            for item in getattr(provider, "source_types", ())
            if str(item).strip()
        )
        if not provider_id or not source_types:
            raise TypeError(
                "SkillSourceProvider requires provider_id and non-empty source_types."
            )
        for source_type in source_types:
            if source_type in self._source_providers and not replace:
                raise ValueError(
                    f"Skill source provider is already registered for source_type {source_type!r}."
                )
            self._source_providers[source_type] = provider
        return self

    def _load_source_provider_plugins(self) -> None:
        if self.plugin_manager is None:
            return
        try:
            names = self.plugin_manager.get_plugin_list("SkillSourceProvider")
        except Exception:
            names = []
        for name in names:
            plugin_class = cast(
                Any,
                self.plugin_manager.get_plugin("SkillSourceProvider", name),
            )
            provider = plugin_class()
            self.register_source_provider(provider)

    @staticmethod
    def _infer_source_type(request: SkillSourceRequest) -> str:
        if request.source_type != "auto":
            return request.source_type
        if Path(request.source).expanduser().exists():
            return "local"
        return "git"

    def _get_source_provider(self, request: SkillSourceRequest) -> SkillSourceProvider:
        source_type = self._infer_source_type(request)
        provider = self._source_providers.get(source_type)
        if provider is None:
            self._load_source_provider_plugins()
            provider = self._source_providers.get(source_type)
        if provider is None:
            raise ValueError(
                f"No Skill source provider is registered for source_type {source_type!r}."
            )
        return provider

    async def async_install_source(
        self,
        request: SkillSourceRequest,
        *,
        scope: str = "explicit",
        trust: str = "untrusted",
    ) -> SkillPackageRevision:
        provider = self._get_source_provider(request)
        snapshot = await provider.async_materialize(request)
        parsed = parse_skill_package(snapshot.materialized_path)
        return self.store.save(
            parsed,
            scope=str(scope or "explicit"),
            trust=str(trust or "untrusted"),
            source=request.to_dict()["source"],
            source_provenance=snapshot.to_dict(),
        )

    async def async_install_pack_source(
        self,
        request: SkillSourceRequest,
        *,
        skill_pack_id: str | None = None,
        name: str | None = None,
        trust: str = "untrusted",
    ) -> SkillPackRevision:
        provider = self._get_source_provider(request)
        snapshot = await provider.async_materialize(request)
        return self.install_pack(
            snapshot.materialized_path,
            skill_pack_id=skill_pack_id,
            name=name,
            trust=trust,
            source_label=request.to_dict()["source"],
            source_provenance=snapshot.to_dict(),
        )

    def install(
        self,
        source: str | Path,
        *,
        scope: str = "explicit",
        trust: str = "untrusted",
    ) -> SkillPackageRevision:
        parsed = parse_skill_package(source)
        return self.store.save(
            parsed,
            scope=str(scope or "explicit"),
            trust=str(trust or "untrusted"),
            source=str(Path(source).expanduser().resolve()),
        )

    @staticmethod
    def normalize_pack_id(value: str) -> str:
        normalized = re.sub(r"\s+", "-", str(value or "").strip().lower())
        normalized = re.sub(r"[^a-z0-9._-]+", "-", normalized).strip("._-")
        normalized = re.sub(r"[-_.]{2,}", "-", normalized).strip("._-")
        if not normalized:
            raise SkillPackageError("Skill pack id cannot be empty.")
        return normalized

    @staticmethod
    def _pack_directories(source: str | Path) -> tuple[Path, ...]:
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise SkillPackageError(f"Skill pack source is not a directory: {root}")
        if (root / "SKILL.md").is_file():
            return (root,)
        candidates: list[Path] = []
        for skill_file in sorted(root.rglob("SKILL.md")):
            candidate = skill_file.parent
            ancestors: list[Path] = []
            parent = candidate.parent
            while parent != root.parent:
                ancestors.append(parent)
                parent = parent.parent
            if any((ancestor / "SKILL.md").is_file() for ancestor in ancestors):
                continue
            candidates.append(candidate)
        if not candidates:
            raise SkillPackageError(f"Skill pack contains no SKILL.md packages: {root}")
        return tuple(candidates)

    def install_pack(
        self,
        source: str | Path,
        *,
        skill_pack_id: str | None = None,
        name: str | None = None,
        trust: str = "untrusted",
        source_label: str | None = None,
        source_provenance: dict[str, Any] | None = None,
    ) -> SkillPackRevision:
        root = Path(source).expanduser().resolve()
        resolved_id = self.normalize_pack_id(skill_pack_id or name or root.name)
        resolved_name = str(name or skill_pack_id or root.name).strip() or resolved_id
        installed: list[SkillPackageRevision] = []
        failed: list[dict[str, str]] = []
        for package_root in self._pack_directories(root):
            try:
                installed.append(
                    self.store.save(
                        parse_skill_package(package_root),
                        scope=resolved_id,
                        trust=trust,
                        source=str(source_label or package_root),
                        source_provenance=source_provenance,
                    )
                )
            except Exception as error:
                failed.append(
                    {
                        "path": str(package_root),
                        "error": str(error),
                        "error_type": error.__class__.__name__,
                    }
                )
        return self.store.save_pack(
            SkillPackRevision(
                skill_pack_id=resolved_id,
                name=resolved_name,
                source=str(source_label or root),
                trust=str(trust or "untrusted"),
                revision_refs=tuple(item.revision_ref for item in installed),
                installed_skills=tuple(item.skill_id for item in installed),
                failed_skills=tuple(failed),
                source_provenance=dict(source_provenance or {}),
            )
        )

    def discover_pack(self, source: str | Path) -> tuple[ParsedSkillPackage, ...]:
        """Parse a local pack without installing packages or mutating the catalog."""

        return tuple(
            parse_skill_package(package_root)
            for package_root in self._pack_directories(source)
        )

    def inspect_pack(self, skill_pack_id: str) -> SkillPackRevision:
        return self.store.load_pack(skill_pack_id)

    def list_packs(self) -> list[SkillPackRevision]:
        return [self.store.load_pack(pack_id) for pack_id in self.store.list_pack_ids()]

    def remove_pack(self, skill_pack_id: str) -> SkillPackRevision:
        return self.store.remove_pack(skill_pack_id)

    @staticmethod
    def _split_ref(skill: str | SkillPackageRevision) -> tuple[str, str | None]:
        if isinstance(skill, SkillPackageRevision):
            return skill.canonical_ref, skill.revision
        raw = str(skill or "").strip()
        marker = "@sha256:"
        if marker in raw:
            canonical, digest = raw.split(marker, 1)
            return canonical, f"sha256:{digest}"
        return raw, None

    def resolve(
        self,
        skill: str | SkillPackageRevision,
        revision: str | None = None,
    ) -> SkillPackageRevision:
        skill_ref, embedded_revision = self._split_ref(skill)
        return self.store.load(skill_ref, revision or embedded_revision)

    def inspect(self, skill: str | SkillPackageRevision) -> SkillPackageRevision:
        return self.resolve(skill)

    def list(self) -> list[SkillPackageRevision]:
        return [self.store.load(skill_id) for skill_id in self.store.list_skill_ids()]

    def list_revisions(self, skill: str | SkillPackageRevision) -> list[SkillPackageRevision]:
        skill_ref, _ = self._split_ref(skill)
        return [self.store.load(skill_ref, revision) for revision in self.store.revisions(skill_ref)]

    @staticmethod
    def _safe_resource_path(path: str) -> PurePosixPath:
        raw = str(path or "")
        resource = PurePosixPath(raw)
        if (
            not raw
            or resource.is_absolute()
            or ".." in resource.parts
            or not resource.parts
            or resource.parts[0] == ".agently"
        ):
            raise SkillPackageError(f"Unsafe Skill resource path: {path!r}")
        return resource

    def read_resource(
        self,
        skill: str | SkillPackageRevision,
        path: str,
        *,
        max_bytes: int = 1024 * 1024,
        offset: int = 0,
    ) -> SkillResourceRead:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer.")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        package = skill if isinstance(skill, SkillPackageRevision) else self.resolve(skill)
        resource_path = self._safe_resource_path(path)
        normalized = resource_path.as_posix()
        try:
            descriptor = package.resource(normalized)
        except KeyError as error:
            raise SkillPackageError(
                f"Unknown Skill resource path {normalized!r} in {package.revision_ref}."
            ) from error
        physical = (Path(package.installed_path) / Path(*resource_path.parts)).resolve()
        package_root = Path(package.installed_path).resolve()
        if package_root not in physical.parents or not physical.is_file():
            raise SkillPackageError(f"Skill resource path escapes or is unavailable: {normalized!r}")
        total = physical.stat().st_size
        with physical.open("rb") as file:
            file.seek(offset)
            data = file.read(max_bytes)
        return SkillResourceRead(
            revision_ref=package.revision_ref,
            path=normalized,
            data=data,
            total_bytes=total,
            offset=offset,
            truncated=offset + len(data) < total,
            sha256=descriptor.sha256,
            media_type=descriptor.media_type,
        )


__all__ = ["SkillLibrary"]
