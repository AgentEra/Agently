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
import re
from collections.abc import Mapping, Sequence
from typing import Any

from agently.types.data import (
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
    ContextSourceRead,
)

from .Binding import SkillBinding, SkillBindingError
from .Package import SkillPackageRevision, SkillResourceDescriptor
from .SkillLibrary import SkillLibrary


class SkillContextSource:
    """ContextSource adapter for exact trusted Skill revision bindings."""

    source_kind = "skill_library"

    def __init__(
        self,
        library: SkillLibrary,
        *,
        bindings: Sequence[SkillBinding],
    ) -> None:
        if not bindings:
            raise SkillBindingError("SkillContextSource requires at least one binding.")
        task_ids = {binding.task_id for binding in bindings}
        if len(task_ids) != 1:
            raise SkillBindingError("SkillContextSource bindings must belong to one task_id.")
        self.library = library
        self.bindings = tuple(bindings)
        self.packages = tuple(library.resolve(binding.revision_ref) for binding in self.bindings)
        for binding, package in zip(self.bindings, self.packages):
            if binding.revision_ref != package.revision_ref:
                raise SkillBindingError(
                    f"Skill binding revision could not be resolved exactly: {binding.revision_ref}."
                )
            if package.trust != "trusted":
                raise SkillBindingError(
                    f"Active Skill instruction binding requires trust: {binding.revision_ref}."
                )
        task_id = next(iter(task_ids))
        self.source_id = f"skill-context:{task_id}"
        self.source_revision = "skill-context:" + "|".join(
            package.revision for package in self.packages
        )
        self._package_by_revision_ref = {
            package.revision_ref: package for package in self.packages
        }
        self._binding_by_revision_ref = {
            binding.revision_ref: binding for binding in self.bindings
        }

    @staticmethod
    def _source_ref(package: SkillPackageRevision, path: str) -> str:
        return f"{package.revision_ref}/{path}"

    @staticmethod
    def _block_id(package: SkillPackageRevision, path: str) -> str:
        digest = hashlib.sha256(f"{package.revision_ref}\0{path}".encode("utf-8")).hexdigest()
        return f"skill_context_block:{digest}"

    @staticmethod
    def _resource_role(resource: SkillResourceDescriptor) -> str:
        return {
            "reference": "information",
            "example": "example",
            "asset": "artifact",
            "script": "capability",
        }.get(resource.kind, "information")

    @staticmethod
    def _resource_summary(resource: SkillResourceDescriptor) -> str:
        return f"{resource.kind} resource {resource.path}"

    @classmethod
    def _markdown_sections(
        cls,
        path: str,
        content: str,
    ) -> tuple[tuple[str, str, str], ...]:
        lines = content.splitlines(keepends=True)
        headings: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if match is not None:
                headings.append((index, match.group(1).strip()))
        sections: list[tuple[str, str, str]] = []
        if headings and headings[0][0] > 0:
            preamble = "".join(lines[: headings[0][0]]).strip()
            if preamble:
                sections.append((f"{path}#section-0", "Overview", preamble))
        for ordinal, (start, title) in enumerate(headings, start=1):
            end = headings[ordinal][0] if ordinal < len(headings) else len(lines)
            body = "".join(lines[start:end]).strip()
            if body:
                sections.append((f"{path}#section-{ordinal}", title, body))
        return tuple(sections)

    @classmethod
    def _instruction_sections(
        cls,
        package: SkillPackageRevision,
    ) -> tuple[tuple[str, str, str], ...]:
        return cls._markdown_sections("SKILL.md", package.instruction_body)

    @classmethod
    def _lossy_instruction_digest(
        cls,
        package: SkillPackageRevision,
        *,
        max_chars: int,
    ) -> tuple[str, tuple[str, ...], tuple[tuple[str, str, str], ...]]:
        sections = cls._instruction_sections(package)
        full_ref = cls._source_ref(package, "SKILL.md")
        section_refs = tuple(cls._source_ref(package, path) for path, _, _ in sections)
        lines = [
            f"# {package.name} — lossy task digest",
            "",
            (
                "This digest is an explicitly authorized lossy projection. "
                "The immutable full Skill remains authoritative at the original ref."
            ),
            "",
            f"Skill: `{package.skill_id}`",
            f"Revision: `{package.revision_ref}`",
            f"Description: {package.description or '(not provided)'}",
            f"Full instructions ref: `{full_ref}`",
        ]
        if sections:
            lines.extend(["", "## Section refs"])
            lines.extend(
                f"- {title}: `{cls._source_ref(package, path)}`"
                for path, title, _ in sections
            )
        content = "\n".join(lines).strip()
        if len(content) > max_chars:
            marker = "\n\n[outline truncated; use refs from block metadata]"
            content = content[: max(1, max_chars - len(marker))].rstrip() + marker
            content = content[:max_chars]
        return content, (full_ref, *section_refs), sections

    @staticmethod
    def _domain_metadata(
        binding: SkillBinding,
        package: SkillPackageRevision,
        path: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "revision_ref": package.revision_ref,
            "resource_path": path,
            "skill_id": package.skill_id,
            "skill_binding_id": binding.binding_id,
            "skill_mode": binding.mode,
            **extra,
        }

    def _descriptor(
        self,
        *,
        binding: SkillBinding,
        package: SkillPackageRevision,
        path: str,
        role: str,
        summary: str,
        estimated_chars: int,
        required: bool,
        completeness: str,
        index_text: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ContextSourceDescriptor:
        del completeness
        domain_metadata = self._domain_metadata(
            binding,
            package,
            path,
            **dict(metadata or {}),
        )
        return ContextSourceDescriptor(
            descriptor_key=f"skill-source:{binding.binding_id}:{path}",
            source_id=self.source_id,
            source_revision=self.source_revision,
            source_ref=self._source_ref(package, path),
            role=role,  # type: ignore[arg-type]
            title=str(domain_metadata.get("section_title") or path),
            summary=summary,
            estimated_chars=estimated_chars,
            required=required,
            index_text=str(index_text if index_text is not None else summary),
            content_digest=(
                str(domain_metadata.get("sha256"))
                if domain_metadata.get("sha256")
                else None
            ),
            metadata=domain_metadata,
        )

    def _all_descriptors(self) -> tuple[ContextSourceDescriptor, ...]:
        descriptors: list[ContextSourceDescriptor] = []
        for binding, package in zip(self.bindings, self.packages):
            descriptors.append(
                self._descriptor(
                    binding=binding,
                    package=package,
                    path="SKILL.md",
                    role="instruction",
                    summary=package.description or package.name,
                    estimated_chars=len(package.instruction_body),
                    required=True,
                    completeness="complete",
                    index_text=(
                        f"{package.name}\n{package.description}\n"
                        f"{package.instruction_body}"
                    ),
                )
            )
            for section_path, section_title, section_body in self._instruction_sections(package):
                descriptors.append(
                    self._descriptor(
                        binding=binding,
                        package=package,
                        path=section_path,
                        role="instruction",
                        summary=f"Skill instruction section: {section_title}",
                        estimated_chars=len(section_body),
                        required=False,
                        completeness="complete",
                        index_text=f"{section_title}\n{section_body}",
                        metadata={
                            "section_title": section_title,
                            "parent_resource_path": "SKILL.md",
                            "parent_source_ref": self._source_ref(
                                package,
                                "SKILL.md",
                            ),
                        },
                    )
                )
            for resource in package.resources:
                if resource.path == "SKILL.md":
                    continue
                descriptors.append(
                    self._descriptor(
                        binding=binding,
                        package=package,
                        path=resource.path,
                        role=self._resource_role(resource),
                        summary=self._resource_summary(resource),
                        estimated_chars=resource.size,
                        required=False,
                        completeness="ref_only",
                        metadata={
                            "resource_kind": resource.kind,
                            "sha256": resource.sha256,
                            "size": resource.size,
                            "media_type": resource.media_type,
                        },
                    )
                )
                if resource.kind not in {"reference", "example"} or not resource.path.endswith(
                    ".md"
                ):
                    continue
                raw_sections = resource.metadata.get("markdown_sections", ())
                if not isinstance(raw_sections, Sequence) or isinstance(
                    raw_sections,
                    str | bytes | bytearray,
                ):
                    continue
                for raw_section in raw_sections:
                    if not isinstance(raw_section, Mapping):
                        continue
                    section_path = str(raw_section.get("section_path") or "")
                    section_title = str(raw_section.get("title") or "")
                    estimated_chars = int(raw_section.get("estimated_chars") or 0)
                    if not section_path or not section_title or estimated_chars <= 0:
                        continue
                    descriptors.append(
                        self._descriptor(
                            binding=binding,
                            package=package,
                            path=section_path,
                            role=self._resource_role(resource),
                            summary=(
                                f"{resource.kind} section: {section_title} "
                                f"({resource.path})"
                            ),
                            estimated_chars=estimated_chars,
                            required=False,
                            completeness="complete",
                            metadata={
                                "resource_kind": resource.kind,
                                "parent_resource_path": resource.path,
                                "parent_source_ref": self._source_ref(
                                    package,
                                    resource.path,
                                ),
                                "section_title": section_title,
                                "byte_offset": int(raw_section.get("byte_offset") or 0),
                                "byte_size": int(raw_section.get("byte_size") or 0),
                                "sha256": resource.sha256,
                                "size": resource.size,
                                "media_type": resource.media_type,
                            },
                        )
                    )
        return tuple(descriptors)

    async def async_enumerate_descriptors(
        self,
        *,
        profile: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> ContextSourceDescriptorPage:
        del profile
        page_size = int(limit)
        if page_size <= 0:
            raise ValueError("limit must be a positive integer.")
        try:
            offset = int(cursor or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("Skill descriptor cursor is invalid.") from error
        if offset < 0:
            raise ValueError("Skill descriptor cursor cannot be negative.")
        descriptors = self._all_descriptors()
        page = descriptors[offset : offset + page_size]
        next_offset = offset + len(page)
        return ContextSourceDescriptorPage(
            source_id=self.source_id,
            source_revision=self.source_revision,
            descriptors=page,
            next_cursor=(str(next_offset) if next_offset < len(descriptors) else None),
        )

    def _resolve_source_ref(
        self,
        source_ref: str,
    ) -> tuple[SkillBinding, SkillPackageRevision, str]:
        for revision_ref, package in self._package_by_revision_ref.items():
            prefix = f"{revision_ref}/"
            if not source_ref.startswith(prefix):
                continue
            path = source_ref[len(prefix) :]
            binding = self._binding_by_revision_ref[revision_ref]
            if not path:
                break
            return binding, package, path
        raise SkillBindingError(
            "Skill Context source_ref is not part of this exact binding set."
        )

    async def async_read_exact(
        self,
        source_ref: str,
        *,
        max_chars: int,
        representation: str | None = None,
        range_start: int = 0,
    ) -> ContextSourceRead:
        binding, package, path = self._resolve_source_ref(source_ref)
        if range_start < 0:
            raise ValueError("range_start cannot be negative.")
        common: dict[str, Any] = {
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_ref": source_ref,
        }
        if path == "SKILL.md":
            if representation == "lossy_digest":
                if range_start:
                    raise ValueError("lossy Skill digest does not support range_start.")
                content, refs, sections = self._lossy_instruction_digest(
                    package,
                    max_chars=max_chars,
                )
                return ContextSourceRead(
                    **common,
                    content=content,
                    completeness="lossy",
                    refs=refs,
                    metadata=self._domain_metadata(
                        binding,
                        package,
                        path,
                        trust=package.trust,
                        representation="lossy_digest",
                        original_chars=len(package.instruction_body),
                        omitted_chars=max(0, len(package.instruction_body) - len(content)),
                        section_refs=[
                            {
                                "title": title,
                                "source_ref": self._source_ref(package, section_path),
                                "estimated_chars": len(body),
                            }
                            for section_path, title, body in sections
                        ],
                    ),
                )
            body = package.instruction_body
            content = body[range_start : range_start + max_chars]
            next_range_start = range_start + len(content)
            complete = next_range_start >= len(body)
            return ContextSourceRead(
                **common,
                content=content,
                completeness="complete" if complete else "truncated",
                next_range_start=None if complete else next_range_start,
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    trust=package.trust,
                ),
            )
        if path.startswith("SKILL.md#section-"):
            section = next(
                (
                    (section_title, section_body)
                    for section_path, section_title, section_body in self._instruction_sections(package)
                    if section_path == path
                ),
                None,
            )
            if section is None:
                raise SkillBindingError("Skill instruction section no longer matches the bound revision.")
            section_title, section_body = section
            content = section_body[range_start : range_start + max_chars]
            next_range_start = range_start + len(content)
            complete = next_range_start >= len(section_body)
            return ContextSourceRead(
                **common,
                content=content,
                completeness="complete" if complete else "truncated",
                next_range_start=None if complete else next_range_start,
                refs=(source_ref, self._source_ref(package, "SKILL.md")),
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    section_title=section_title,
                    total_chars=len(section_body),
                ),
            )
        if path == "resource-index":
            index = tuple(
                {
                    "path": resource.path,
                    "kind": resource.kind,
                    "size": resource.size,
                    "sha256": resource.sha256,
                }
                for resource in package.resources
                if resource.path != "SKILL.md"
            )
            return ContextSourceRead(
                **common,
                content=index,
                completeness="complete",
                metadata=self._domain_metadata(binding, package, path),
            )
        resource_section = re.match(r"^(.+\.md)#section-\d+$", path)
        if resource_section is not None:
            parent_path = resource_section.group(1)
            resource = package.resource(parent_path)
            raw_sections = resource.metadata.get("markdown_sections", ())
            section = next(
                (
                    raw_section
                    for raw_section in raw_sections
                    if isinstance(raw_section, Mapping)
                    and str(raw_section.get("section_path") or "") == path
                ),
                None,
            )
            if section is None:
                raise SkillBindingError(
                    "Skill resource section no longer matches the bound revision."
                )
            section_title = str(section.get("title") or "")
            byte_offset = int(section.get("byte_offset") or 0)
            byte_size = int(section.get("byte_size") or 0)
            readback = self.library.read_resource(
                package.revision_ref,
                parent_path,
                max_bytes=max(1, byte_size),
                offset=byte_offset,
            )
            section_body = readback.text.strip()
            content = section_body[range_start : range_start + max_chars]
            next_range_start = range_start + len(content)
            complete = next_range_start >= len(section_body)
            return ContextSourceRead(
                **common,
                content=content,
                completeness="complete" if complete else "truncated",
                next_range_start=None if complete else next_range_start,
                content_digest=resource.sha256,
                refs=(source_ref, self._source_ref(package, parent_path)),
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    resource_kind=resource.kind,
                    parent_resource_path=parent_path,
                    section_title=section_title,
                    total_chars=int(section.get("estimated_chars") or len(section_body)),
                    byte_offset=byte_offset,
                    byte_size=byte_size,
                    sha256=resource.sha256,
                ),
            )
        resource = package.resource(path)
        if resource.kind == "script":
            descriptor = {
                "descriptor_kind": "skill_script",
                "revision_ref": package.revision_ref,
                "resource_path": resource.path,
                "sha256": resource.sha256,
                "size": resource.size,
            }
            return ContextSourceRead(
                **common,
                content=descriptor,
                completeness="ref_only",
                content_digest=resource.sha256,
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    resource_kind=resource.kind,
                    descriptor_kind="skill_script",
                    sha256=resource.sha256,
                    size=resource.size,
                    executable=resource.executable,
                    action_status="binding_required",
                ),
            )
        if resource.kind == "asset":
            descriptor = {
                "descriptor_kind": "skill_asset",
                "revision_ref": package.revision_ref,
                "resource_path": resource.path,
                "sha256": resource.sha256,
                "size": resource.size,
                "media_type": resource.media_type,
            }
            return ContextSourceRead(
                **common,
                content=descriptor,
                completeness="ref_only",
                content_digest=resource.sha256,
                metadata=self._domain_metadata(
                    binding,
                    package,
                    path,
                    resource_kind=resource.kind,
                ),
            )
        readback = self.library.read_resource(
            package.revision_ref,
            path,
            max_bytes=max_chars,
            offset=range_start,
        )
        next_range_start = range_start + len(readback.text.encode("utf-8"))
        return ContextSourceRead(
            **common,
            content=readback.text,
            completeness="truncated" if readback.truncated else "complete",
            next_range_start=(next_range_start if readback.truncated else None),
            content_digest=resource.sha256,
            metadata=self._domain_metadata(
                binding,
                package,
                path,
                resource_kind=resource.kind,
                sha256=resource.sha256,
                total_bytes=readback.total_bytes,
            ),
        )


__all__ = ["SkillContextSource"]
