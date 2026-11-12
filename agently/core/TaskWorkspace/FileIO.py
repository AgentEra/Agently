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

import codecs
import hashlib
import mimetypes
from pathlib import Path
from typing import Any, cast

from agently.core.model.AttachmentInput import (
    SUPPORTED_IMAGE_MIME_TYPES,
    detect_image_mime_type,
    image_file_to_data_url,
)
from agently.types.data.task_workspace import (
    TaskWorkspaceDiagnostic,
    TaskWorkspaceExportResult,
    TaskWorkspaceFileInfo,
    TaskWorkspaceReadResult,
    TaskWorkspaceFileRef,
    TaskWorkspaceWriteResult,
)
from agently.utils import LazyImport


TEXT_EXTENSIONS = {
    "",
    ".c",
    ".cc",
    ".cjs",
    ".cfg",
    ".conf",
    ".cpp",
    ".css",
    ".cts",
    ".csv",
    ".cxx",
    ".env",
    ".gitignore",
    ".go",
    ".h",
    ".hh",
    ".htm",
    ".html",
    ".hpp",
    ".hxx",
    ".ini",
    ".js",
    ".json",
    ".json5",
    ".jsx",
    ".log",
    ".md",
    ".mjs",
    ".mts",
    ".py",
    ".pyi",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

TEXT_MEDIA_TYPES = {
    "application/json",
    "application/json5",
    "application/toml",
    "application/x-yaml",
    "application/xml",
    "text/csv",
    "text/css",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
}

OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
BINARY_EXTENSIONS = {
    ".7z",
    ".bin",
    ".db",
    ".dll",
    ".doc",
    ".dylib",
    ".exe",
    ".feather",
    ".gz",
    ".ico",
    ".jar",
    ".mp3",
    ".mp4",
    ".parquet",
    ".pdf",
    ".png",
    ".ppt",
    ".rar",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".wav",
    ".xls",
    ".zip",
}


def _diagnostic(
    code: str,
    message: str,
    *,
    handler_id: str | None = None,
    dependency: str | None = None,
    detail: dict[str, Any] | None = None,
) -> TaskWorkspaceDiagnostic:
    diagnostic: TaskWorkspaceDiagnostic = {
        "code": code,
        "message": message,
    }
    if handler_id is not None:
        diagnostic["handler_id"] = handler_id
    if dependency is not None:
        diagnostic["dependency"] = dependency
    if detail:
        diagnostic["detail"] = detail
    return diagnostic


def _file_ref(path: str, file_info: TaskWorkspaceFileInfo, *, role: str) -> TaskWorkspaceFileRef:
    return cast(
        TaskWorkspaceFileRef,
        {
            "path": path,
            "bytes": int(file_info.get("bytes", 0)),
            "sha256": str(file_info.get("sha256", "")),
            "media_type": file_info.get("media_type"),
            "content_kind": str(file_info.get("content_kind", "unknown")),
            "role": role,
        },
    )


def _slice_text_bytes(
    text: str,
    *,
    max_bytes: int,
    offset: int,
    encoding: str = "utf-8",
) -> tuple[str, int, bool]:
    raw = text.encode(encoding)
    safe_offset = max(0, int(offset))
    safe_max = max(0, int(max_bytes))
    end = safe_offset + safe_max
    segment = raw[safe_offset:end]
    truncated = len(raw) > end
    return segment.decode(encoding, errors="ignore"), len(segment), truncated


def unsupported_read_result(
    *,
    file_info: TaskWorkspaceFileInfo,
    handler_id: str,
    code: str,
    message: str,
    dependency: str | None = None,
) -> TaskWorkspaceReadResult:
    path = str(file_info.get("path", ""))
    return {
        "ok": False,
        "readable": False,
        "path": path,
        "content": "",
        "truncated": False,
        "bytes": int(file_info.get("bytes", 0)),
        "offset": 0,
        "read_bytes": 0,
        "sha256": str(file_info.get("sha256", "")),
        "media_type": file_info.get("media_type"),
        "content_kind": str(file_info.get("content_kind", "unknown")),
        "encoding": None,
        "handler_id": handler_id,
        "extraction_method": "unsupported",
        "diagnostics": [
            _diagnostic(code, message, handler_id=handler_id, dependency=dependency),
        ],
        "file_refs": [_file_ref(path, file_info, role="source")] if path else [],
    }


def unsupported_write_result(
    *,
    file_info: TaskWorkspaceFileInfo,
    handler_id: str,
    code: str,
    message: str,
    dependency: str | None = None,
) -> TaskWorkspaceWriteResult:
    path = str(file_info.get("path", ""))
    return {
        "ok": False,
        "writable": False,
        "path": path,
        "bytes": int(file_info.get("bytes", 0)),
        "sha256": str(file_info.get("sha256", "")),
        "media_type": file_info.get("media_type"),
        "content_kind": str(file_info.get("content_kind", "unknown")),
        "encoding": None,
        "mode": "unsupported",
        "handler_id": handler_id,
        "diagnostics": [
            _diagnostic(code, message, handler_id=handler_id, dependency=dependency),
        ],
        "file_refs": [_file_ref(path, file_info, role="target")] if path else [],
    }


def unsupported_export_result(
    *,
    source_info: TaskWorkspaceFileInfo,
    output_info: TaskWorkspaceFileInfo,
    export_kind: str,
    handler_id: str,
    code: str,
    message: str,
    dependency: str | None = None,
) -> TaskWorkspaceExportResult:
    source_path = str(source_info.get("path", ""))
    output_path = str(output_info.get("path", ""))
    return {
        "ok": False,
        "exported": False,
        "source_path": source_path,
        "output_path": output_path,
        "export_kind": export_kind,
        "bytes": int(output_info.get("bytes", 0)),
        "sha256": str(output_info.get("sha256", "")),
        "media_type": output_info.get("media_type"),
        "content_kind": str(output_info.get("content_kind", "unknown")),
        "handler_id": handler_id,
        "diagnostics": [
            _diagnostic(code, message, handler_id=handler_id, dependency=dependency),
        ],
        "file_refs": [_file_ref(source_path, source_info, role="source")] if source_path else [],
    }


def inspect_task_workspace_file(
    path: Path, *, relative_path: str, _raw: bytes | None = None,
) -> TaskWorkspaceFileInfo:
    extension = path.suffix.lower()
    guessed_type = mimetypes.guess_type(str(path))[0]
    media_type = guessed_type
    exists = path.exists()
    raw = b""
    size = 0
    digest = hashlib.sha256()
    text_valid = True
    if exists and path.is_file():
        if _raw is not None:
            raw = _raw
            size = len(raw)
            digest.update(raw)
            try:
                raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text_valid = False
        else:
            # Revision/metadata inspection must not materialize a large file.
            # Keep only signatures while validating UTF-8 and hashing in chunks.
            decoder = codecs.getincrementaldecoder("utf-8-sig")()
            with path.open("rb") as carrier:
                while chunk := carrier.read(1024 * 1024):
                    if not raw:
                        raw = chunk[:4096]
                    size += len(chunk)
                    digest.update(chunk)
                    if text_valid:
                        try:
                            decoder.decode(chunk)
                        except UnicodeDecodeError:
                            text_valid = False
                if text_valid:
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        text_valid = False
    signatures: list[str] = []
    if raw.startswith(b"%PDF-"):
        signatures.append("pdf")
        media_type = media_type or "application/pdf"
    if raw.startswith(b"PK\x03\x04"):
        signatures.append("zip")
    image_media_type = None
    if exists and path.is_file() and (extension in IMAGE_EXTENSIONS or str(guessed_type or "").startswith("image/")):
        detected_image_type = detect_image_mime_type(path)
        if detected_image_type in SUPPORTED_IMAGE_MIME_TYPES:
            image_media_type = detected_image_type
    if image_media_type:
        signatures.append("image")
        media_type = image_media_type
    if b"\x00" in raw[:4096]:
        signatures.append("nul_byte")
    sha256 = digest.hexdigest()

    content_kind = "unknown"
    readable = False
    writable = extension not in BINARY_EXTENSIONS
    if "pdf" in signatures or extension == ".pdf":
        content_kind = "pdf"
        writable = False
    elif extension in OFFICE_EXTENSIONS:
        content_kind = "office"
        writable = False
    elif image_media_type or extension in IMAGE_EXTENSIONS:
        content_kind = "image"
        writable = False
    elif extension in TEXT_EXTENSIONS or media_type in TEXT_MEDIA_TYPES or str(media_type or "").startswith("text/"):
        content_kind = "text"
        writable = True
        readable = True
    elif exists and size:
        if not text_valid:
            content_kind = "binary" if "nul_byte" in signatures else "unknown"
            readable = False
        else:
            content_kind = "text"
            readable = True
            writable = True
    elif not exists and writable:
        content_kind = "text" if extension in TEXT_EXTENSIONS else "unknown"

    return {
        "path": relative_path,
        "extension": extension,
        "media_type": media_type,
        "content_kind": content_kind,
        "bytes": size,
        "sha256": sha256,
        "signatures": signatures,
        "readable": readable,
        "writable": writable,
        "exists": exists,
    }


class DefaultTextTaskWorkspaceFileIOHandler:
    name = "text"
    priority = 100
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register():
        return None

    @staticmethod
    def _on_unregister():
        return None

    def supports(self, *, operation: str, file_info: TaskWorkspaceFileInfo, export_kind: str | None = None) -> bool:
        _ = export_kind
        if operation == "read":
            return file_info.get("content_kind") == "text"
        if operation == "write":
            return bool(file_info.get("writable", False))
        return False

    async def read(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        max_bytes: int = 20000,
        offset: int = 0,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult:
        return self._read_bytes(path.read_bytes(), file_info, max_bytes=max_bytes, offset=offset)

    def _read_bytes(
        self, raw: bytes, file_info: TaskWorkspaceFileInfo, *, max_bytes: int, offset: int,
    ) -> TaskWorkspaceReadResult:
        """Decode bytes already observed by the registry in this read operation."""
        try:
            text = raw.decode("utf-8-sig")
            encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
            content, read_bytes, truncated = _slice_text_bytes(text, max_bytes=max_bytes, offset=offset)
        except UnicodeDecodeError:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.text_decode_failed",
                message="File is not valid UTF-8 text.",
            )
        safe_offset = max(0, int(offset))
        path_text = str(file_info.get("path", ""))
        return {
            "ok": True,
            "readable": True,
            "path": path_text,
            "content": content,
            "truncated": truncated,
            "bytes": len(raw),
            "offset": safe_offset,
            "read_bytes": read_bytes,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": file_info.get("media_type"),
            "content_kind": "text",
            "encoding": encoding,
            "handler_id": self.name,
            "extraction_method": "text.decode",
            "diagnostics": [],
            "file_refs": [_file_ref(path_text, file_info, role="source")] if path_text else [],
        }

    async def write(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        content: str,
        append: bool = False,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult:
        _ = options
        if not bool(file_info.get("writable", False)):
            return unsupported_write_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.text_write_unsupported",
                message="Default write_file only writes plain text files.",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if append:
            with path.open("a", encoding="utf-8") as file:
                file.write(content)
        else:
            path.write_text(content, encoding="utf-8")
        raw = path.read_bytes()
        path_text = str(file_info.get("path", ""))
        return {
            "ok": True,
            "writable": True,
            "path": path_text,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": file_info.get("media_type"),
            "content_kind": "text",
            "encoding": "utf-8",
            "mode": "append" if append else "write",
            "handler_id": self.name,
            "diagnostics": [],
            "file_refs": [
                cast(
                    TaskWorkspaceFileRef,
                    {
                    "path": path_text,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "media_type": file_info.get("media_type"),
                    "content_kind": "text",
                    "role": "output",
                    },
                )
            ],
        }

    async def export(
        self,
        *,
        source_path: Path,
        output_path: Path,
        source_info: TaskWorkspaceFileInfo,
        output_info: TaskWorkspaceFileInfo,
        export_kind: str,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult:
        _ = (source_path, output_path, options)
        return unsupported_export_result(
            source_info=source_info,
            output_info=output_info,
            export_kind=export_kind,
            handler_id=self.name,
            code="task_workspace.file.export_unsupported",
            message="The default text handler does not export files.",
        )


class PdfTaskWorkspaceFileIOHandler:
    name = "pdf"
    priority = 200
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register():
        return None

    @staticmethod
    def _on_unregister():
        return None

    def supports(self, *, operation: str, file_info: TaskWorkspaceFileInfo, export_kind: str | None = None) -> bool:
        _ = export_kind
        return operation == "read" and file_info.get("content_kind") == "pdf"

    async def read(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        max_bytes: int = 20000,
        offset: int = 0,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult:
        _ = options
        try:
            pypdf = LazyImport.import_package("pypdf", auto_install=False)
        except ImportError:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.pdf_dependency_missing",
                message="PDF text extraction requires optional dependency 'pypdf'.",
                dependency="pypdf",
            )
        try:
            reader = pypdf.PdfReader(str(path))
            if getattr(reader, "is_encrypted", False):
                return unsupported_read_result(
                    file_info=file_info,
                    handler_id=self.name,
                    code="task_workspace.file.pdf_encrypted",
                    message="Encrypted PDF files are not readable by the default PDF handler.",
                )
            page_texts = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(item for item in page_texts if item.strip())
        except Exception as exc:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.pdf_extract_failed",
                message=f"PDF text extraction failed: {exc}",
            )
        if not text.strip():
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.pdf_no_text",
                message="PDF contains no extractable text. Use an image/VLM preparation handler if appropriate.",
            )
        content, read_bytes, truncated = _slice_text_bytes(text, max_bytes=max_bytes, offset=offset)
        path_text = str(file_info.get("path", ""))
        return {
            "ok": True,
            "readable": True,
            "path": path_text,
            "content": content,
            "truncated": truncated,
            "bytes": int(file_info.get("bytes", 0)),
            "offset": max(0, int(offset)),
            "read_bytes": read_bytes,
            "sha256": str(file_info.get("sha256", "")),
            "media_type": file_info.get("media_type") or "application/pdf",
            "content_kind": "pdf",
            "encoding": "utf-8",
            "handler_id": self.name,
            "extraction_method": "pypdf.extract_text",
            "diagnostics": [],
            "file_refs": [_file_ref(path_text, file_info, role="source")] if path_text else [],
        }

    async def write(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        content: str,
        append: bool = False,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult:
        _ = (path, content, append, options)
        return unsupported_write_result(
            file_info=file_info,
            handler_id=self.name,
            code="task_workspace.file.pdf_write_unsupported",
            message="PDF output must use export_file(...), not write_file(...).",
        )

    async def export(
        self,
        *,
        source_path: Path,
        output_path: Path,
        source_info: TaskWorkspaceFileInfo,
        output_info: TaskWorkspaceFileInfo,
        export_kind: str,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult:
        _ = (source_path, output_path, options)
        return unsupported_export_result(
            source_info=source_info,
            output_info=output_info,
            export_kind=export_kind,
            handler_id=self.name,
            code="task_workspace.file.pdf_export_unsupported",
            message="The PDF read handler does not export files.",
        )


class OfficeTaskWorkspaceFileIOHandler:
    name = "office"
    priority = 210
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register():
        return None

    @staticmethod
    def _on_unregister():
        return None

    def supports(self, *, operation: str, file_info: TaskWorkspaceFileInfo, export_kind: str | None = None) -> bool:
        _ = export_kind
        return operation == "read" and file_info.get("content_kind") == "office"

    async def read(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        max_bytes: int = 20000,
        offset: int = 0,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult:
        _ = options
        extension = str(file_info.get("extension", "")).lower()
        if extension == ".docx":
            text_or_result = self._read_docx(path, file_info)
            method = "python-docx"
        elif extension == ".xlsx":
            text_or_result = self._read_xlsx(path, file_info)
            method = "openpyxl"
        elif extension == ".pptx":
            text_or_result = self._read_pptx(path, file_info)
            method = "python-pptx"
        else:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.office_extension_unsupported",
                message=f"Unsupported Office extension: {extension}",
            )
        if isinstance(text_or_result, dict):
            return text_or_result
        text = text_or_result
        if not text.strip():
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.office_no_text",
                message="Office file contains no extractable text.",
            )
        content, read_bytes, truncated = _slice_text_bytes(text, max_bytes=max_bytes, offset=offset)
        path_text = str(file_info.get("path", ""))
        return {
            "ok": True,
            "readable": True,
            "path": path_text,
            "content": content,
            "truncated": truncated,
            "bytes": int(file_info.get("bytes", 0)),
            "offset": max(0, int(offset)),
            "read_bytes": read_bytes,
            "sha256": str(file_info.get("sha256", "")),
            "media_type": file_info.get("media_type"),
            "content_kind": "office",
            "encoding": "utf-8",
            "handler_id": self.name,
            "extraction_method": method,
            "diagnostics": [],
            "file_refs": [_file_ref(path_text, file_info, role="source")] if path_text else [],
        }

    def _read_docx(self, path: Path, file_info: TaskWorkspaceFileInfo) -> str | TaskWorkspaceReadResult:
        try:
            docx = LazyImport.import_package("docx", auto_install=False, install_name="python-docx")
        except ImportError:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.docx_dependency_missing",
                message="DOCX text extraction requires optional dependency 'python-docx'.",
                dependency="python-docx",
            )
        document = docx.Document(str(path))
        paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
        return "\n".join(paragraphs)

    def _read_xlsx(self, path: Path, file_info: TaskWorkspaceFileInfo) -> str | TaskWorkspaceReadResult:
        try:
            openpyxl = LazyImport.import_package("openpyxl", auto_install=False)
        except ImportError:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.xlsx_dependency_missing",
                message="XLSX text extraction requires optional dependency 'openpyxl'.",
                dependency="openpyxl",
            )
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        try:
            chunks: list[str] = []
            for sheet in workbook.worksheets:
                chunks.append(f"# Sheet: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    values = ["" if value is None else str(value) for value in row]
                    if any(values):
                        chunks.append("\t".join(values))
            return "\n".join(chunks)
        finally:
            workbook.close()

    def _read_pptx(self, path: Path, file_info: TaskWorkspaceFileInfo) -> str | TaskWorkspaceReadResult:
        try:
            pptx = LazyImport.import_package("pptx", auto_install=False, install_name="python-pptx")
        except ImportError:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.pptx_dependency_missing",
                message="PPTX text extraction requires optional dependency 'python-pptx'.",
                dependency="python-pptx",
            )
        presentation = pptx.Presentation(str(path))
        chunks: list[str] = []
        for index, slide in enumerate(presentation.slides, start=1):
            chunks.append(f"# Slide {index}")
            for shape in slide.shapes:
                text = getattr(shape, "text", "")
                if text:
                    chunks.append(str(text))
        return "\n".join(chunks)

    async def write(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        content: str,
        append: bool = False,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult:
        _ = (path, content, append, options)
        return unsupported_write_result(
            file_info=file_info,
            handler_id=self.name,
            code="task_workspace.file.office_write_unsupported",
            message="Office output requires an explicit export or custom handler.",
        )

    async def export(
        self,
        *,
        source_path: Path,
        output_path: Path,
        source_info: TaskWorkspaceFileInfo,
        output_info: TaskWorkspaceFileInfo,
        export_kind: str,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult:
        _ = (source_path, output_path, options)
        return unsupported_export_result(
            source_info=source_info,
            output_info=output_info,
            export_kind=export_kind,
            handler_id=self.name,
            code="task_workspace.file.office_export_unsupported",
            message="The Office read handler does not export files.",
        )


class ImageVLMTaskWorkspaceFileIOHandler:
    name = "image_vlm"
    priority = 220
    DEFAULT_SETTINGS: dict[str, Any] = {}

    @staticmethod
    def _on_register():
        return None

    @staticmethod
    def _on_unregister():
        return None

    def supports(self, *, operation: str, file_info: TaskWorkspaceFileInfo, export_kind: str | None = None) -> bool:
        _ = export_kind
        return operation == "read" and file_info.get("content_kind") == "image"

    async def read(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        max_bytes: int = 20000,
        offset: int = 0,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult:
        _ = (max_bytes, offset, options)
        path_text = str(file_info.get("path", ""))
        try:
            data_url = image_file_to_data_url(path)
        except Exception as exc:
            return unsupported_read_result(
                file_info=file_info,
                handler_id=self.name,
                code="task_workspace.file.image_prepare_failed",
                message=f"Image attachment preparation failed: {exc}",
            )
        return {
            "ok": True,
            "readable": False,
            "path": path_text,
            "content": "",
            "truncated": False,
            "bytes": int(file_info.get("bytes", 0)),
            "offset": 0,
            "read_bytes": 0,
            "sha256": str(file_info.get("sha256", "")),
            "media_type": file_info.get("media_type"),
            "content_kind": "image",
            "encoding": None,
            "handler_id": self.name,
            "extraction_method": "model.image_attachment.prepare",
            "diagnostics": [
                _diagnostic(
                    "task_workspace.file.image_prepared_for_model",
                    "Image was prepared as a ModelRequest-compatible attachment. Interpretation belongs to ModelRequest.",
                    handler_id=self.name,
                )
            ],
            "file_refs": [_file_ref(path_text, file_info, role="source")] if path_text else [],
            "attachments": [{"type": "image_url", "image_url": {"url": data_url}}],
        }

    async def write(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        content: str,
        append: bool = False,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult:
        _ = (path, content, append, options)
        return unsupported_write_result(
            file_info=file_info,
            handler_id=self.name,
            code="task_workspace.file.image_write_unsupported",
            message="Image generation or editing is not owned by write_file(...).",
        )

    async def export(
        self,
        *,
        source_path: Path,
        output_path: Path,
        source_info: TaskWorkspaceFileInfo,
        output_info: TaskWorkspaceFileInfo,
        export_kind: str,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult:
        _ = (source_path, output_path, options)
        return unsupported_export_result(
            source_info=source_info,
            output_info=output_info,
            export_kind=export_kind,
            handler_id=self.name,
            code="task_workspace.file.image_export_unsupported",
            message="The image preparation handler does not export files.",
        )


class HtmlExportTaskWorkspaceFileIOHandler:
    name = "html_export"
    priority = 300
    DEFAULT_SETTINGS: dict[str, Any] = {}
    EXPORT_KINDS = {"html_pdf", "html_screenshot", "markdown_pdf"}

    @staticmethod
    def _on_register():
        return None

    @staticmethod
    def _on_unregister():
        return None

    def supports(self, *, operation: str, file_info: TaskWorkspaceFileInfo, export_kind: str | None = None) -> bool:
        _ = file_info
        return operation == "export" and str(export_kind or "") in self.EXPORT_KINDS

    async def read(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        max_bytes: int = 20000,
        offset: int = 0,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceReadResult:
        _ = (path, max_bytes, offset, options)
        return unsupported_read_result(
            file_info=file_info,
            handler_id=self.name,
            code="task_workspace.file.export_handler_read_unsupported",
            message="The HTML export handler does not read files.",
        )

    async def write(
        self,
        *,
        path: Path,
        file_info: TaskWorkspaceFileInfo,
        content: str,
        append: bool = False,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceWriteResult:
        _ = (path, content, append, options)
        return unsupported_write_result(
            file_info=file_info,
            handler_id=self.name,
            code="task_workspace.file.export_handler_write_unsupported",
            message="The HTML export handler does not write source text.",
        )

    async def export(
        self,
        *,
        source_path: Path,
        output_path: Path,
        source_info: TaskWorkspaceFileInfo,
        output_info: TaskWorkspaceFileInfo,
        export_kind: str,
        options: dict[str, Any] | None = None,
    ) -> TaskWorkspaceExportResult:
        options = dict(options or {})
        try:
            playwright_async = LazyImport.import_package(
                "playwright.async_api",
                auto_install=False,
                install_name="playwright",
            )
        except ImportError:
            return unsupported_export_result(
                source_info=source_info,
                output_info=output_info,
                export_kind=export_kind,
                handler_id=self.name,
                code="task_workspace.file.export_dependency_missing",
                message="HTML/PDF/screenshot export requires optional dependency 'playwright'.",
                dependency="playwright",
            )
        try:
            html = source_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return unsupported_export_result(
                source_info=source_info,
                output_info=output_info,
                export_kind=export_kind,
                handler_id=self.name,
                code="task_workspace.file.export_source_not_text",
                message="Export source must be UTF-8 text.",
            )
        if export_kind == "markdown_pdf":
            try:
                markdown = LazyImport.import_package("markdown", auto_install=False)
            except ImportError:
                return unsupported_export_result(
                    source_info=source_info,
                    output_info=output_info,
                    export_kind=export_kind,
                    handler_id=self.name,
                    code="task_workspace.file.markdown_dependency_missing",
                    message="Markdown-to-PDF export requires optional dependency 'markdown'.",
                    dependency="markdown",
                )
            html = f"<!doctype html><html><body>{markdown.markdown(html)}</body></html>"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        async_playwright = getattr(playwright_async, "async_playwright")
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page()
                    if not bool(options.get("allow_network", False)):
                        await page.route("http://*/*", lambda route: route.abort())
                        await page.route("https://*/*", lambda route: route.abort())
                    await page.set_content(html, wait_until="load")
                    if export_kind in {"html_pdf", "markdown_pdf"}:
                        await page.pdf(path=str(output_path))
                    elif export_kind == "html_screenshot":
                        await page.screenshot(path=str(output_path), full_page=bool(options.get("full_page", True)))
                    else:
                        return unsupported_export_result(
                            source_info=source_info,
                            output_info=output_info,
                            export_kind=export_kind,
                            handler_id=self.name,
                            code="task_workspace.file.export_kind_unsupported",
                            message=f"Unsupported export kind: {export_kind}",
                        )
                finally:
                    await browser.close()
        except Exception as exc:
            return unsupported_export_result(
                source_info=source_info,
                output_info=output_info,
                export_kind=export_kind,
                handler_id=self.name,
                code="task_workspace.file.export_failed",
                message=f"Export failed: {exc}",
            )
        output_raw = output_path.read_bytes()
        output_ref = cast(
            TaskWorkspaceFileRef,
            {
                "path": str(output_info.get("path", "")),
                "bytes": len(output_raw),
                "sha256": hashlib.sha256(output_raw).hexdigest(),
                "media_type": output_info.get("media_type"),
                "content_kind": str(output_info.get("content_kind", "unknown")),
                "role": "output",
            },
        )
        source_path_text = str(source_info.get("path", ""))
        return {
            "ok": True,
            "exported": True,
            "source_path": source_path_text,
            "output_path": str(output_info.get("path", "")),
            "export_kind": export_kind,
            "bytes": len(output_raw),
            "sha256": hashlib.sha256(output_raw).hexdigest(),
            "media_type": output_info.get("media_type"),
            "content_kind": str(output_info.get("content_kind", "unknown")),
            "handler_id": self.name,
            "diagnostics": [],
            "file_refs": [
                _file_ref(source_path_text, source_info, role="source"),
                output_ref,
            ],
        }
