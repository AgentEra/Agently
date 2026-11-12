# Copyright 2023-2026 AgentEra(Agently.Tech)

from __future__ import annotations

from pathlib import Path
from typing import Any

from agently.types.data import (
    TaskWorkspaceExportResult,
    TaskWorkspaceFileInfo,
    TaskWorkspaceFileOperation,
    TaskWorkspaceReadResult,
    TaskWorkspaceWriteResult,
)
from agently.types.plugins import TaskWorkspaceFileIOHandler

from .FileIO import (
    DefaultTextTaskWorkspaceFileIOHandler,
    HtmlExportTaskWorkspaceFileIOHandler,
    ImageVLMTaskWorkspaceFileIOHandler,
    OfficeTaskWorkspaceFileIOHandler,
    PdfTaskWorkspaceFileIOHandler,
    inspect_task_workspace_file,
    unsupported_export_result,
    unsupported_read_result,
    unsupported_write_result,
)


class _TaskWorkspaceFileIORegistry:
    """TaskWorkspace-internal dispatch for pluggable file representations."""

    def __init__(self) -> None:
        self._handlers: dict[str, TaskWorkspaceFileIOHandler] = {}
        for handler in (
            DefaultTextTaskWorkspaceFileIOHandler(),
            PdfTaskWorkspaceFileIOHandler(),
            OfficeTaskWorkspaceFileIOHandler(),
            ImageVLMTaskWorkspaceFileIOHandler(),
            HtmlExportTaskWorkspaceFileIOHandler(),
        ):
            self.register(handler)

    def register(
        self,
        handler: TaskWorkspaceFileIOHandler,
        *,
        replace: bool = False,
    ) -> None:
        name = str(getattr(handler, "name", "")).strip()
        if not name:
            raise ValueError("TaskWorkspace file IO handler name must be non-empty.")
        for method_name in ("supports", "read", "write", "export"):
            if not callable(getattr(handler, method_name, None)):
                raise TypeError(
                    f"TaskWorkspace file IO handler must provide {method_name}(...)."
                )
        if name in self._handlers and not replace:
            raise ValueError(f"TaskWorkspace file IO handler is already registered: {name}")
        self._handlers[name] = handler

    def unregister(self, handler_id: str) -> None:
        self._handlers.pop(str(handler_id).strip(), None)

    def list(self) -> list[str]:
        return sorted(self._handlers)

    def clone(self) -> "_TaskWorkspaceFileIORegistry":
        cloned = _TaskWorkspaceFileIORegistry()
        cloned._handlers = dict(self._handlers)
        return cloned

    @staticmethod
    def inspect(path: Path, *, relative_path: str) -> TaskWorkspaceFileInfo:
        return inspect_task_workspace_file(path, relative_path=relative_path)

    def _select(
        self,
        *,
        operation: TaskWorkspaceFileOperation,
        file_info: TaskWorkspaceFileInfo,
        handler: str | None = None,
        export_kind: str | None = None,
    ) -> TaskWorkspaceFileIOHandler | None:
        if handler is not None:
            selected = self._handlers.get(str(handler).strip())
            if selected is None:
                raise KeyError(f"TaskWorkspace file IO handler is not registered: {handler}")
            return (
                selected
                if selected.supports(
                    operation=operation,
                    file_info=file_info,
                    export_kind=export_kind,
                )
                else None
            )
        for candidate in sorted(
            self._handlers.values(),
            key=lambda item: (
                int(getattr(item, "priority", 1000)),
                str(getattr(item, "name", "")),
            ),
        ):
            if candidate.supports(
                operation=operation,
                file_info=file_info,
                export_kind=export_kind,
            ):
                return candidate
        return None

    async def read(
        self,
        path: Path,
        *,
        relative_path: str,
        max_bytes: int,
        offset: int = 0,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult:
        # Classification, digest and built-in text decoding share one observation.
        # Custom handlers retain their original path-based read contract.
        raw = path.read_bytes()
        file_info = inspect_task_workspace_file(path, relative_path=relative_path, _raw=raw)
        selected = self._select(operation="read", file_info=file_info, handler=handler)
        if selected is None:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=handler or "none",
                code="task_workspace.file.no_read_handler",
                message="No registered TaskWorkspace file IO handler can read this file type.",
            )
        if type(selected) is DefaultTextTaskWorkspaceFileIOHandler:
            return selected._read_bytes(raw, file_info, max_bytes=max_bytes, offset=offset)
        return await selected.read(
            path=path,
            file_info=file_info,
            max_bytes=max_bytes,
            offset=offset,
            options=options,
        )

    async def write(
        self,
        path: Path,
        *,
        relative_path: str,
        content: str,
        append: bool = False,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult:
        file_info = self.inspect(path, relative_path=relative_path)
        selected = self._select(operation="write", file_info=file_info, handler=handler)
        if selected is None:
            return unsupported_write_result(
                file_info=file_info,
                handler_id=handler or "none",
                code="task_workspace.file.no_write_handler",
                message="No registered TaskWorkspace file IO handler can write this file type.",
            )
        return await selected.write(
            path=path,
            file_info=file_info,
            content=content,
            append=append,
            options=options,
        )

    async def export(
        self,
        source_path: Path,
        output_path: Path,
        *,
        source_relative_path: str,
        output_relative_path: str,
        export_kind: str,
        handler: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult:
        source_info = self.inspect(source_path, relative_path=source_relative_path)
        output_info = self.inspect(output_path, relative_path=output_relative_path)
        selected = self._select(
            operation="export",
            file_info=source_info,
            handler=handler,
            export_kind=export_kind,
        )
        if selected is None:
            return unsupported_export_result(
                source_info=source_info,
                output_info=output_info,
                export_kind=export_kind,
                handler_id=handler or "none",
                code="task_workspace.file.no_export_handler",
                message=(
                    "No registered TaskWorkspace file IO handler can export this "
                    "source file to the requested kind."
                ),
            )
        return await selected.export(
            source_path=source_path,
            output_path=output_path,
            source_info=source_info,
            output_info=output_info,
            export_kind=export_kind,
            options=options,
        )


__all__: list[str] = []
