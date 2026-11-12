from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.plugins import TaskWorkspaceFileIOHandler


class _SyntheticExportHandler:
    name = "synthetic_export"
    priority = 1

    def supports(self, *, operation, file_info, export_kind=None):
        _ = file_info
        return operation == "export" and export_kind == "synthetic"

    async def read(self, **kwargs):
        _ = kwargs
        raise NotImplementedError

    async def write(self, **kwargs):
        _ = kwargs
        raise NotImplementedError

    async def export(
        self,
        *,
        source_path,
        output_path,
        source_info,
        output_info,
        export_kind,
        options=None,
    ):
        _ = (source_info, output_info, export_kind, options)
        output_path.write_text(
            f"synthetic:{source_path.read_text(encoding='utf-8')}",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "exported": True,
            "source_path": source_path.name,
            "output_path": output_path.name,
            "export_kind": "synthetic",
            "handler_id": self.name,
            "diagnostics": [],
            "file_refs": [],
        }


@pytest.mark.asyncio
async def test_task_workspace_owns_registered_file_io_handler_lifecycle(
    tmp_path: Path,
) -> None:
    workspace = TaskWorkspace(tmp_path, mode="read_write")
    workspace.register_file_io_handler(
        cast(TaskWorkspaceFileIOHandler, _SyntheticExportHandler())
    )
    await workspace.write_file("report.md", "current-run")

    result = await workspace.export_file(
        "report.md",
        "report.synthetic",
        export_kind="synthetic",
    )

    assert "synthetic_export" in workspace.list_file_io_handlers()
    assert result["exported"] is True
    assert result["handler_id"] == "synthetic_export"
    assert (tmp_path / "report.synthetic").read_text(encoding="utf-8") == (
        "synthetic:current-run"
    )


def test_task_workspace_package_does_not_expose_a_second_manager_owner() -> None:
    import agently.core.TaskWorkspace as task_workspace_package

    assert not hasattr(task_workspace_package, "TaskWorkspaceManager")


def test_agent_execution_inherits_task_workspace_file_io_extensions(
    tmp_path: Path,
) -> None:
    from agently import Agently

    agent = Agently.create_agent("workspace-file-io-inheritance").use_task_workspace(
        tmp_path,
        mode="read_write",
    )
    agent.task_workspace.register_file_io_handler(
        cast(TaskWorkspaceFileIOHandler, _SyntheticExportHandler())
    )

    execution = agent.create_execution()

    assert "synthetic_export" in execution.task_workspace.list_file_io_handlers()
    assert execution.task_workspace.execution_id == execution.id


def test_inspection_streams_large_unknown_files_and_checks_utf8_tail(tmp_path, monkeypatch):
    import hashlib
    from agently.core.TaskWorkspace.FileIO import inspect_task_workspace_file

    target = tmp_path / 'payload.custom'
    body = b'a' * (2 * 1024 * 1024) + b'\xff'
    target.write_bytes(body)

    def no_full_read(*args, **kwargs):
        raise AssertionError('metadata inspection must stream, not read_bytes')

    monkeypatch.setattr(Path, 'read_bytes', no_full_read)
    info = inspect_task_workspace_file(target, relative_path='payload.custom')
    assert info['bytes'] == len(body)
    assert info['sha256'] == hashlib.sha256(body).hexdigest()
    assert info['content_kind'] == 'unknown'
    assert not info['readable']
