from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.application.SkillLibrary import (
    SkillBinding,
    SkillBindingError,
    SkillContextSource,
    SkillLibrary,
)
from agently.core.context import ContextSelection, TaskContext
from agently.types.data import ContextBudget, ContextCandidate, ContextConsumer, ContextReadIntent


def _write_skill(root: Path, *, body: str = "Always verify the report before delivery.") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("references", "examples", "assets", "scripts"):
        (root / name).mkdir(exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: Report Verification\n"
        "description: Produce verified reports.\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    (root / "references" / "criteria.md").write_text(
        "Check accuracy, citations, and completeness.",
        encoding="utf-8",
    )
    (root / "examples" / "accepted.md").write_text("# Accepted", encoding="utf-8")
    (root / "assets" / "report.txt").write_text("REPORT TEMPLATE", encoding="utf-8")
    (root / "scripts" / "validate.py").write_text("print('validate')", encoding="utf-8")
    return root


class SelectRefs:
    async def async_select(
        self,
        *,
        intent: ContextReadIntent,
        candidates: list[ContextCandidate] | tuple[ContextCandidate, ...],
        consumer: ContextConsumer,
        phase: str,
    ) -> ContextSelection:
        return ContextSelection(
            selected_keys=tuple(
                item.block_key
                for item in candidates
                if item.source_ref.endswith("references/criteria.md")
            )
        )


class FailIfSelected:
    async def async_select(self, **_kwargs) -> ContextSelection:
        raise AssertionError("A Skill without optional resources needs no semantic selection.")


class SelectSearchSection:
    async def async_select(
        self,
        *,
        candidates: list[ContextCandidate] | tuple[ContextCandidate, ...],
        **_kwargs,
    ) -> ContextSelection:
        return ContextSelection(
            selected_keys=tuple(
                item.block_key
                for item in candidates
                if item.metadata.get("section_title") == "Search Action"
            )
        )


@pytest.mark.asyncio
async def test_skill_context_source_exposes_typed_progressive_candidates(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    binding = SkillBinding.create(package, task_id="task-1", mode="required")
    source = SkillContextSource(library, bindings=(binding,))

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=100,
    )
    descriptors = page.descriptors
    by_path = {item.metadata.get("resource_path", "core"): item for item in descriptors}

    assert source.source_id == "skill-context:task-1"
    assert package.revision in source.source_revision
    assert by_path["SKILL.md"].role == "instruction"
    assert by_path["SKILL.md"].required is True
    assert "resource-index" not in by_path
    assert by_path["references/criteria.md"].role == "information"
    assert by_path["examples/accepted.md"].role == "example"
    assert by_path["assets/report.txt"].role == "artifact"
    assert by_path["scripts/validate.py"].role == "capability"


@pytest.mark.asyncio
async def test_skill_context_source_enumerates_descriptors_without_task_intent(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library-descriptor")
    package = library.install(_write_skill(tmp_path / "skill-descriptor"), trust="trusted")
    binding = SkillBinding.create(package, task_id="task-descriptor", mode="required")
    source = SkillContextSource(library, bindings=(binding,))

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=100,
    )
    instruction = next(
        item for item in page.descriptors if item.metadata.get("resource_path") == "SKILL.md"
    )
    readback = await source.async_read_exact(instruction.source_ref, max_chars=1000)

    assert source.source_kind == "skill_library"
    assert instruction.required is True
    assert readback.content == "Always verify the report before delivery."


@pytest.mark.asyncio
async def test_skill_without_resources_does_not_offer_an_empty_optional_index(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "minimal-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: Minimal Skill\n"
        "description: One complete instruction with no extra resources.\n"
        "---\n\n"
        "Apply the minimal procedure.",
        encoding="utf-8",
    )
    library = SkillLibrary(tmp_path / "library")
    installed = library.install(skill_root, trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(
            SkillBinding.create(installed, task_id="minimal-task", mode="required"),
        ),
    )
    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=100,
    )
    descriptors = page.descriptors
    context = TaskContext("minimal-task")
    context.attach(source, required=True)
    package = await context.reader(
        consumer="direct-model",
        semantic_selector=FailIfSelected(),
    ).async_read("Apply the procedure")

    assert [item.metadata["resource_path"] for item in descriptors] == ["SKILL.md"]
    assert [block.content for block in package.blocks] == ["Apply the minimal procedure."]
    assert not any(item.code == "context.selection_failed" for item in package.diagnostics)


@pytest.mark.asyncio
async def test_skill_core_is_always_delivered_and_reference_body_is_progressive(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="task-1", mode="required"),),
    )
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:skills", required=True)
    reader = context.reader(
        consumer="worker",
        phase="execution",
        semantic_selector=SelectRefs(),
    )

    package_result = await reader.async_read("Create and verify the report")

    assert [block.role for block in package_result.blocks[:2]] == ["instruction", "information"]
    assert package_result.blocks[0].content == "Always verify the report before delivery."
    reference = next(
        block for block in package_result.blocks if block.source_ref.endswith("references/criteria.md")
    )
    assert reference.content == "Check accuracy, citations, and completeness."
    assert reference.completeness == "complete"


@pytest.mark.asyncio
async def test_oversized_markdown_reference_discloses_selected_section_within_budget(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill(tmp_path / "sectioned-skill")
    reference = skill_root / "references" / "runtime.md"
    reference.write_text(
        "# Runtime Reference\n\n"
        + ("General background stays cold.\n" * 300)
        + "\n## Search Action\n\n"
        + "Use `from agently.builtins.actions import Search` and configure timeout plus retry.\n"
        + "\n## Other Capability\n\nUnrelated details.\n",
        encoding="utf-8",
    )
    library = SkillLibrary(tmp_path / "library")
    package = library.install(skill_root, trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="section-task", mode="required"),),
    )
    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=100,
    )
    descriptors = page.descriptors
    whole = next(
        item
        for item in descriptors
        if item.metadata.get("resource_path") == "references/runtime.md"
    )
    search_section = next(
        item
        for item in descriptors
        if item.metadata.get("section_title") == "Search Action"
    )

    assert whole.estimated_chars > 6000
    assert search_section.estimated_chars < 1000
    context = TaskContext("section-task")
    context.attach(source, required=True)
    delivered = await context.reader(
        consumer="planner",
        budget=ContextBudget(max_chars=2500, max_blocks=8, max_block_chars=1000),
        semantic_selector=SelectSearchSection(),
    ).async_read(
        ContextReadIntent(
            query="Use the exact Search Action API",
            metadata={"required_overflow": "lossy_digest"},
        )
    )

    selected = next(
        block for block in delivered.blocks if block.metadata.get("section_title") == "Search Action"
    )
    assert "from agently.builtins.actions import Search" in selected.content
    assert selected.completeness == "complete"
    assert selected.refs == (search_section.source_ref, whole.source_ref)


@pytest.mark.asyncio
async def test_oversized_skill_can_build_explicit_lossy_outline_with_section_refs(
    tmp_path: Path,
) -> None:
    body = (
        "This preamble defines the protected operating boundary.\n\n"
        "## Planning\n\nPlan with exact contracts and trusted keys.\n\n"
        "## Execution\n\nExecute through ordinary Actions and preserve evidence.\n\n"
        "## Verification\n\nRead artifacts before judging completion.\n"
        + ("Additional detail that stays cold.\n" * 400)
    )
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "large-skill", body=body),
        trust="trusted",
    )
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="large-task", mode="required"),),
    )
    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=100,
    )
    descriptors = page.descriptors
    core = next(item for item in descriptors if item.metadata["resource_path"] == "SKILL.md")
    sections = [
        item for item in descriptors if str(item.metadata["resource_path"]).startswith("SKILL.md#section-")
    ]

    digest = await source.async_read_exact(
        core.source_ref,
        max_chars=900,
        representation="lossy_digest",
    )

    assert digest.completeness == "lossy"
    assert 0 < len(str(digest.content)) <= 900
    assert digest.metadata["representation"] == "lossy_digest"
    assert digest.metadata["original_chars"] == len(package.instruction_body)
    assert {item.metadata["section_title"] for item in sections} >= {
        "Planning",
        "Execution",
        "Verification",
    }
    assert set(item.source_ref for item in sections).issubset(set(digest.refs))

    context = TaskContext("large-task")
    context.attach(source, required=True)
    delivered = await context.reader(
        consumer="worker",
        budget=ContextBudget(max_chars=900, max_blocks=16, max_block_chars=900),
    ).async_read(
        ContextReadIntent(
            query="Execute and verify",
            roles=("instruction",),
            metadata={"required_overflow": "lossy_digest"},
        )
    )
    assert delivered.blocks[0].completeness == "lossy"
    assert not any(item.required for item in delivered.omissions)


@pytest.mark.asyncio
async def test_skill_domain_identity_survives_task_context_source_binding(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    skill_binding = SkillBinding.create(
        package,
        task_id="task-1",
        mode="model_decision",
        binding_id="skill_binding:task-1:1",
    )
    context = TaskContext("task-1")
    context.attach(
        SkillContextSource(library, bindings=(skill_binding,)),
        binding_id="task_context_source_binding:skills",
    )

    result = await context.reader(consumer="planner").async_read("Use the Skill")
    instruction = next(block for block in result.blocks if block.role == "instruction")

    # Generic Context identity remains the TaskContext attachment identity.
    assert instruction.binding_id == "task_context_source_binding:skills"
    # Skill-domain identity and activation policy are explicit metadata; callers
    # must never infer either value from the generic binding or required flag.
    assert instruction.metadata["skill_binding_id"] == "skill_binding:task-1:1"
    assert instruction.metadata["skill_mode"] == "model_decision"
    assert instruction.required is True


@pytest.mark.asyncio
async def test_skill_resources_remain_cold_until_selected(tmp_path: Path, monkeypatch) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="task-1", mode="required"),),
    )
    reads: list[str] = []
    original = library.read_resource

    def recording_read(skill, path, **kwargs):
        reads.append(path)
        return original(skill, path, **kwargs)

    monkeypatch.setattr(library, "read_resource", recording_read)
    context = TaskContext("task-1")
    context.attach(source, binding_id="binding:skills")
    reader = context.reader(consumer="planner")

    result = await reader.async_read("Inspect available capabilities")

    assert [block.role for block in result.blocks] == ["instruction"]
    assert reads == []
    assert any(item.code == "context.semantic_selector_unavailable" for item in result.diagnostics)


@pytest.mark.asyncio
async def test_skill_script_read_returns_descriptor_not_executable_object(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="task-1", mode="required"),),
    )
    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=100,
    )
    script = next(item for item in page.descriptors if item.role == "capability")

    block = await source.async_read_exact(script.source_ref, max_chars=1000)

    assert script.role == "capability"
    assert block.completeness == "ref_only"
    assert block.content == {
        "descriptor_kind": "skill_script",
        "revision_ref": package.revision_ref,
        "resource_path": "scripts/validate.py",
        "sha256": package.resource("scripts/validate.py").sha256,
        "size": package.resource("scripts/validate.py").size,
    }
    assert not callable(block.content)
    assert "action" not in block.metadata
    assert "permission" not in block.metadata


@pytest.mark.asyncio
async def test_skill_context_source_enumerates_descriptors_once_across_pages(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(_write_skill(tmp_path / "skill"), trust="trusted")
    source = SkillContextSource(
        library,
        bindings=(SkillBinding.create(package, task_id="paged-skill", mode="required"),),
    )
    first = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=1,
    )
    second = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=first.next_cursor,
        limit=1,
    )

    assert [item.metadata["resource_path"] for item in first.descriptors if item.required] == [
        "SKILL.md"
    ]
    assert not any(item.required for item in second.descriptors)
    assert first.descriptors[0].descriptor_key != second.descriptors[0].descriptor_key
    assert first.next_cursor is not None


def test_untrusted_skill_cannot_create_active_instruction_binding(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "skill"),
        scope="project",
        trust="untrusted",
    )

    with pytest.raises(SkillBindingError, match="trusted"):
        SkillBinding.create(package, task_id="task-1", mode="required")


def test_skill_binding_pins_exact_revision_across_library_updates(tmp_path: Path) -> None:
    source_root = _write_skill(tmp_path / "skill")
    library = SkillLibrary(tmp_path / "library")
    first = library.install(source_root, trust="trusted")
    binding = SkillBinding.create(first, task_id="task-1", mode="required")
    _write_skill(source_root, body="Revised instructions")
    latest = library.install(source_root, trust="trusted")
    context_source = SkillContextSource(library, bindings=(binding,))

    assert latest.revision != first.revision
    assert context_source.bindings[0].revision_ref == first.revision_ref
    assert context_source.packages[0].instruction_body == first.instruction_body


@pytest.mark.asyncio
async def test_skill_catalog_remains_exact_readable_without_becoming_a_candidate(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / 'library')
    package = library.install(_write_skill(tmp_path / 'skill'), trust='trusted')
    source = SkillContextSource(library, bindings=(SkillBinding.create(package, task_id='catalog', mode='required'),))
    page = await source.async_enumerate_descriptors(profile={}, cursor=None, limit=100)
    assert all(item.role != 'index' for item in page.descriptors)
    read = await source.async_read_exact(package.revision_ref + '/resource-index', max_chars=10_000)
    assert read.content
    assert read.completeness == 'complete'
    assert read.source_revision == source.source_revision
