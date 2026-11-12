from __future__ import annotations

from pathlib import Path

import pytest

from agently.core.TaskWorkspace import (
    TaskWorkspace,
    TaskWorkspaceContextSource,
    TaskWorkspacePolicyError,
)
from agently.core.application.SkillLibrary import SkillLibrary


@pytest.mark.asyncio
async def test_task_workspace_edits_caller_directory_with_direct_root(tmp_path: Path) -> None:
    (tmp_path / "draft.md").write_text("Draft one", encoding="utf-8")
    workspace = TaskWorkspace(tmp_path, mode="read_write", execution_id="task-1")

    edited = await workspace.edit_file("draft.md", "one", "two")
    created = await workspace.write_file("result.md", "Final result")
    readback = await workspace.read_file("result.md")

    assert workspace.root == tmp_path.resolve()
    assert edited.path == "draft.md"
    assert (tmp_path / "draft.md").read_text(encoding="utf-8") == "Draft two"
    assert created.path == "result.md"
    assert readback.content == "Final result"
    assert readback.truncated is False
    assert readback.sha256 == created.sha256


@pytest.mark.asyncio
async def test_task_workspace_atomic_content_replace_is_digest_conditional(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_write", execution_id="task-atomic-edit")
    original = await workspace.write_file("candidate.md", "first\nsecond\n")

    replaced = await workspace._atomic_replace_file_content(
        "candidate.md",
        "first\nrepaired\n",
        expected_sha256=original.sha256,
        replacements=1,
    )

    assert replaced.replacements == 1
    assert (tmp_path / "candidate.md").read_text(encoding="utf-8") == "first\nrepaired\n"
    with pytest.raises(ValueError, match="changed since"):
        await workspace._atomic_replace_file_content(
            "candidate.md",
            "stale overwrite\n",
            expected_sha256=original.sha256,
        )
    assert (tmp_path / "candidate.md").read_text(encoding="utf-8") == "first\nrepaired\n"


@pytest.mark.asyncio
async def test_terminal_target_write_guard_allows_only_digest_pinned_promotion(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_write", execution_id="task-guard")
    await workspace.write_file("final.md", "previous accepted body\n")
    staged = await workspace.write_file(
        "working/taskboard/finalize/terminal-candidates/final.md",
        "verified candidate body\n",
    )

    with workspace._protect_terminal_targets(["final.md"]):
        with pytest.raises(TaskWorkspacePolicyError, match="terminal target"):
            await workspace.write_file("final.md", "premature overwrite")
        with pytest.raises(TaskWorkspacePolicyError, match="terminal target"):
            await workspace.edit_file("final.md", "previous", "premature")
        with pytest.raises(TaskWorkspacePolicyError, match="terminal target"):
            await workspace.apply_patch(
                """diff --git a/final.md b/final.md
--- a/final.md
+++ b/final.md
@@ -1 +1 @@
-previous accepted body
+premature patch body
"""
            )
        promoted = await workspace.atomic_promote_file(
            staged.path,
            "final.md",
            expected_sha256=staged.sha256,
        )

    assert promoted["sha256"] == staged.sha256
    assert (tmp_path / "final.md").read_text(encoding="utf-8") == (
        "verified candidate body\n"
    )


@pytest.mark.asyncio
async def test_read_only_task_workspace_creates_task_artifact_in_private_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "existing.md").write_text("Do not mutate", encoding="utf-8")
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-42")

    created = await workspace.write_file("new/report.md", "Task artifact")
    logical_readback = await workspace.read_file("new/report.md")
    search_results = await workspace.search_files(
        "artifact",
        path="new",
        pattern="*.md",
    )

    assert created.requested_path == "new/report.md"
    assert created.path == ".agently/files/task-42/new/report.md"
    assert created.to_dict()["file_refs"][0]["requested_path"] == "new/report.md"
    assert workspace.resolve_file_path("new/report.md") == (tmp_path / created.path).resolve()
    assert logical_readback.path == created.path
    assert logical_readback.content == "Task artifact"
    assert [item["path"] for item in search_results] == [created.path]
    assert (tmp_path / created.path).read_text(encoding="utf-8") == "Task artifact"
    with pytest.raises(TaskWorkspacePolicyError, match="write permission"):
        await workspace.write_file("existing.md", "mutated")


@pytest.mark.asyncio
async def test_context_source_exposes_execution_fallback_as_logical_file(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-context")
    created = await workspace.write_file(
        "notes/alpha.md",
        "release deadline is 2026-07-01\n",
    )
    source = TaskWorkspaceContextSource(workspace)

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=20,
    )

    assert created.path == ".agently/files/task-context/notes/alpha.md"
    assert [item.source_ref for item in page.descriptors] == ["notes/alpha.md"]
    assert page.descriptors[0].metadata["path"] == "notes/alpha.md"
    readback = await source.async_read_exact("notes/alpha.md", max_chars=200)
    assert readback.content == "release deadline is 2026-07-01\n"


@pytest.mark.asyncio
async def test_read_only_task_workspace_can_continue_only_its_own_fallback_carrier(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-42")
    created = await workspace.write_file("report.md", "first")

    appended = await workspace.write_file(created.path, " second", append=True)

    assert appended.path == created.path
    assert (tmp_path / appended.path).read_text(encoding="utf-8") == "first second"
    with pytest.raises(TaskWorkspacePolicyError, match="private state"):
        await workspace.write_file(".agently/files/another-task/report.md", "bad")


@pytest.mark.asyncio
async def test_task_workspace_enforces_path_containment(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_write")

    for unsafe in ("../outside.txt", "/tmp/outside.txt"):
        with pytest.raises(TaskWorkspacePolicyError, match="outside TaskWorkspace root"):
            await workspace.write_file(unsafe, "bad")


@pytest.mark.asyncio
async def test_task_workspace_context_source_enumerates_all_files_and_exact_reads(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.md").write_text(
        "Revenue increased by 12 percent.\nMargin was stable.",
        encoding="utf-8",
    )
    (tmp_path / "notes.md").write_text("Unrelated notes", encoding="utf-8")
    workspace = TaskWorkspace(tmp_path, mode="read_only", execution_id="task-1")
    source = TaskWorkspaceContextSource(workspace)

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=20,
    )
    descriptors = page.descriptors

    assert [item.source_ref for item in descriptors] == ["notes.md", "report.md"]
    report = next(item for item in descriptors if item.source_ref == "report.md")
    assert report.role == "information"
    block = await source.async_read_exact(report.source_ref, max_chars=200)
    assert block.content == "Revenue increased by 12 percent.\nMargin was stable."
    assert block.completeness == "complete"
    assert block.source_ref == "report.md"
    assert block.metadata["sha256"]


@pytest.mark.asyncio
async def test_task_workspace_context_source_pages_matching_files(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"report-{index}.md").write_text(
            f"shared marker {index}",
            encoding="utf-8",
        )
    source = TaskWorkspaceContextSource(
        TaskWorkspace(tmp_path, mode="read_only", execution_id="task-pages")
    )
    first = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=2,
    )
    second = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=first.next_cursor,
        limit=2,
    )

    assert [item.source_ref for item in first.descriptors] == [
        "report-0.md",
        "report-1.md",
    ]
    assert [item.source_ref for item in second.descriptors] == [
        "report-2.md",
        "report-3.md",
    ]
    assert first.next_cursor is not None
    assert set(item.source_ref for item in first.descriptors).isdisjoint(
        item.source_ref for item in second.descriptors
    )


def test_task_workspace_source_revision_observes_external_file_change(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.write_text("First", encoding="utf-8")
    source = TaskWorkspaceContextSource(TaskWorkspace(tmp_path, mode="read_only"))

    first = source.source_revision
    target.write_text("Second", encoding="utf-8")
    second = source.source_revision

    assert first != second


@pytest.mark.asyncio
async def test_task_workspace_context_source_enumerates_without_query_and_reads_exact(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.md").write_text("Revenue increased", encoding="utf-8")
    source = TaskWorkspaceContextSource(
        TaskWorkspace(tmp_path, mode="read_only", execution_id="descriptor-source")
    )

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=10,
    )
    readback = await source.async_read_exact("report.md", max_chars=100)

    assert source.source_kind == "task_workspace"
    assert [item.source_ref for item in page.descriptors] == ["report.md"]
    assert page.descriptors[0].index_text
    assert readback.content == "Revenue increased"
    assert readback.source_revision == page.source_revision


@pytest.mark.asyncio
async def test_task_workspace_context_source_indexes_image_name_without_preparing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Minimal valid PNG signature is sufficient for deterministic media
    # classification; enumeration must not base64-prepare it.
    (tmp_path / "chart.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )
    workspace = TaskWorkspace(
        tmp_path,
        mode="read_only",
        execution_id="image-context-source",
    )
    source = TaskWorkspaceContextSource(workspace)

    async def fail_if_read(*_args, **_kwargs):
        raise AssertionError("image enumeration must not prepare attachment bytes")

    monkeypatch.setattr(workspace, "read_file", fail_if_read)
    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=10,
    )

    descriptor = page.descriptors[0]
    assert descriptor.source_ref == "chart.png"
    assert descriptor.summary == "chart.png"
    assert descriptor.index_text == "chart.png"
    assert descriptor.metadata["content_kind"] == "image"
    assert descriptor.metadata["media_type"] == "image/png"
    assert "base64" not in descriptor.index_text


@pytest.mark.asyncio
async def test_task_workspace_context_source_returns_image_attachment_only_on_exact_representation(
    tmp_path: Path,
) -> None:
    (tmp_path / "chart.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )
    source = TaskWorkspaceContextSource(
        TaskWorkspace(
            tmp_path,
            mode="read_only",
            execution_id="image-context-exact-read",
        )
    )

    readback = await source.async_read_exact(
        "chart.png",
        max_chars=1,
        representation="image_attachment",
    )

    assert readback.content[0]["type"] == "image_url"
    assert readback.content[0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert readback.metadata["context_representation"] == "image_attachment"


@pytest.mark.asyncio
async def test_task_workspace_context_source_parses_xlsx_for_context(
    tmp_path: Path,
) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "课程规划"
    sheet.append(["课程", "说明"])
    sheet.append(["AI 能力边界与银行责任", "只交付解析后的文本"])
    workbook.save(tmp_path / "curriculum.xlsx")
    workbook.close()
    source = TaskWorkspaceContextSource(
        TaskWorkspace(
            tmp_path,
            mode="read_only",
            execution_id="xlsx-context-source",
        )
    )

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=10,
    )
    descriptor = page.descriptors[0]
    readback = await source.async_read_exact(
        "curriculum.xlsx",
        max_chars=20_000,
    )

    assert descriptor.metadata["content_kind"] == "office"
    assert descriptor.metadata["context_representation"] == "parsed_text"
    assert readback.metadata["context_representation"] == "parsed_text"
    assert "AI 能力边界与银行责任" in readback.content
    assert not isinstance(readback.content, bytes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    [
        "main.cjs",
        "main.cts",
        "main.go",
        "main.mjs",
        "main.mts",
        "types.pyi",
        "main.c",
        "main.cc",
        "main.cpp",
        "main.h",
        "main.hpp",
    ],
)
async def test_task_workspace_context_source_keeps_empty_mainstream_code_as_text(
    tmp_path: Path,
    filename: str,
) -> None:
    (tmp_path / filename).write_text("", encoding="utf-8")
    workspace = TaskWorkspace(
        tmp_path,
        mode="read_only",
        execution_id=f"empty-source-{filename}",
    )
    source = TaskWorkspaceContextSource(workspace)

    page = await source.async_enumerate_descriptors(
        profile={"schema_version": "context-index/v1"},
        cursor=None,
        limit=10,
    )

    assert workspace.inspect_file(filename)["content_kind"] == "text"
    assert page.descriptors[0].metadata["context_representation"] == "text"


def test_task_workspace_has_no_cross_source_context_builder(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path)

    assert not hasattr(workspace, "build_context")
    assert not hasattr(workspace, "context_builder")
    assert not hasattr(workspace, "semantic_selector")
    assert not hasattr(workspace, "request")


@pytest.mark.asyncio
async def test_skill_asset_copy_on_write_does_not_mutate_installed_revision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skill"
    (source / "assets").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: Template Skill\ndescription: Provides a template.\n---\nUse the template.",
        encoding="utf-8",
    )
    (source / "assets" / "template.txt").write_text("ORIGINAL", encoding="utf-8")
    library = SkillLibrary(tmp_path / "library")
    package = library.install(source, trust="trusted")
    workspace = TaskWorkspace(tmp_path / "work", mode="read_write")

    copied = await workspace.copy_from(
        Path(package.installed_path) / "assets" / "template.txt",
        "drafts/template.txt",
    )
    await workspace.edit_file(copied.path, "ORIGINAL", "CHANGED")

    assert (workspace.root / copied.path).read_text(encoding="utf-8") == "CHANGED"
    assert library.read_resource(package.revision_ref, "assets/template.txt").text == "ORIGINAL"


@pytest.mark.asyncio
async def test_context_reuses_small_text_and_invalidates_same_size_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    target = tmp_path / 'note.txt'
    target.write_text('first body')
    source = TaskWorkspaceContextSource(TaskWorkspace(tmp_path))
    opened = []
    original = Path.open

    def track(path, *args, **kwargs):
        if path == target:
            opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', track)
    page = await source.async_enumerate_descriptors(profile={}, cursor=None, limit=10)
    read = await source.async_read_exact('note.txt', max_chars=100)
    assert read.content == 'first body'
    # Source observation plus one IO operation; registry classification and
    # decoding no longer reread the file. Exact disclosure reuses the body.
    assert len(opened) == 2
    assert read.content_digest == page.descriptors[0].content_digest
    before = target.stat()
    target.write_text('other body')
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = await source.async_read_exact('note.txt', max_chars=100)
    assert changed.content == 'other body'
    assert changed.source_revision != read.source_revision
    assert changed.content_digest != read.content_digest


@pytest.mark.asyncio
async def test_cached_context_preserves_utf8_ranges_and_bom_behavior(tmp_path: Path) -> None:
    workspace = TaskWorkspace(tmp_path)
    for name, body in [('utf8.txt', '甲乙abc'.encode()), ('bom.txt', b'\xef\xbb\xbfhello')]:
        (tmp_path / name).write_bytes(body)
        source = TaskWorkspaceContextSource(workspace)
        await source.async_enumerate_descriptors(profile={}, cursor=None, limit=10)
        for offset, size in [(0, 2), (1, 5), (3, 4), (50, 5)]:
            expected = await workspace.read_file(name, offset=offset, max_bytes=size)
            actual = await source.async_read_exact(name, range_start=offset, max_chars=size)
            assert actual.content == expected.content
            assert actual.completeness == ('truncated' if expected.truncated else 'complete')
            assert actual.content_digest == expected.sha256


@pytest.mark.asyncio
async def test_cached_source_checks_replacement_deletion_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / 'work'
    root.mkdir()
    target = root / 'note.txt'
    target.write_text('first')
    source = TaskWorkspaceContextSource(TaskWorkspace(root))
    first = await source.async_read_exact('note.txt', max_chars=100)
    replacement = tmp_path / 'replacement.txt'
    replacement.write_text('next!')
    replacement.replace(target)
    second = await source.async_read_exact('note.txt', max_chars=100)
    assert second.content == 'next!'
    assert first.source_revision != second.source_revision
    target.unlink()
    assert source.source_revision != second.source_revision
    assert not source._reads
    with pytest.raises(FileNotFoundError):
        await source.async_read_exact('note.txt', max_chars=100)
    outside = tmp_path / 'outside.txt'
    outside.write_text('private')
    target.symlink_to(outside)
    with pytest.raises(TaskWorkspacePolicyError):
        await source.async_read_exact('note.txt', max_chars=100)


@pytest.mark.asyncio
async def test_source_rejects_file_changed_by_handler_during_read(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / 'note.txt'
    target.write_text('first')
    workspace = TaskWorkspace(tmp_path)
    source = TaskWorkspaceContextSource(workspace)
    original = workspace.read_file

    async def racing(*args, **kwargs):
        result = await original(*args, **kwargs)
        target.write_text('changed')
        return result

    monkeypatch.setattr(workspace, 'read_file', racing)
    with pytest.raises(ValueError, match='changed during read'):
        await source.async_read_exact('note.txt', max_chars=100)
    assert not source._reads


@pytest.mark.asyncio
async def test_source_does_not_cache_custom_text_handler(tmp_path: Path) -> None:
    from agently.core.TaskWorkspace.FileIO import DefaultTextTaskWorkspaceFileIOHandler

    class CustomText(DefaultTextTaskWorkspaceFileIOHandler):
        name = 'custom'
        priority = 0
        calls = 0

        async def read(self, **kwargs):
            self.calls += 1
            result = await super().read(**kwargs)
            result['content'] = f'custom {self.calls}'
            return result

    (tmp_path / 'note.txt').write_text('source')
    workspace = TaskWorkspace(tmp_path)
    source = TaskWorkspaceContextSource(workspace)
    await source.async_read_exact('note.txt', max_chars=100)
    custom = CustomText()
    workspace.register_file_io_handler(custom)
    first = await source.async_read_exact('note.txt', max_chars=100)
    second = await source.async_read_exact('note.txt', max_chars=100)
    assert (first.content, second.content) == ('custom 1', 'custom 2')
    assert not source._reads


@pytest.mark.asyncio
async def test_source_body_cache_is_bounded_and_pinned_reader_stays_stale(tmp_path: Path) -> None:
    from agently.core.context import TaskContext
    from agently.types.data import ContextReadIntent
    from agently.core.context import ContextStaleError

    for i in range(60):
        (tmp_path / f'{i}.txt').write_text('x' * 20_000)
    source = TaskWorkspaceContextSource(TaskWorkspace(tmp_path))
    for i in range(60):
        await source.async_read_exact(f'{i}.txt', max_chars=20_000)
    assert source._read_bytes <= 1_048_576
    assert len(source._reads) < 60
    context = TaskContext(task_id='cache-stale')
    context.attach(source)
    reader = context.reader(consumer='test', phase='read')
    (tmp_path / '0.txt').write_text('changed')
    with pytest.raises(ContextStaleError):
        await reader.async_read(ContextReadIntent(query='read', explicit_refs=('0.txt',)))
