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

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Literal


ImageDetail = Literal["low", "high", "auto"]

SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
}


def build_image_attachment(
    *,
    question: str | None = None,
    file: str | os.PathLike[str] | None = None,
    url: str | None = None,
    files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
    urls: list[str] | tuple[str, ...] | None = None,
    detail: ImageDetail | None = None,
) -> list[dict[str, Any]]:
    attachment: list[dict[str, Any]] = []
    if question is not None:
        attachment.append({"type": "text", "text": _validate_question(question)})

    image_urls: list[str] = []
    if file is not None:
        image_urls.append(image_file_to_data_url(file))
    for item in _coerce_sequence(files, "files"):
        image_urls.append(image_file_to_data_url(item))
    if url is not None:
        image_urls.append(_validate_url(url))
    for item in _coerce_sequence(urls, "urls"):
        image_urls.append(_validate_url(item))

    if not image_urls:
        raise ValueError("image() requires at least one image source: file, files, url, or urls.")

    for image_url in image_urls:
        image_value: dict[str, Any] = {"url": image_url}
        if detail is not None:
            image_value["detail"] = _validate_detail(detail)
        attachment.append({"type": "image_url", "image_url": image_value})
    return attachment


def image_file_to_data_url(file: str | os.PathLike[str]) -> str:
    path = Path(file)
    if not path.exists():
        raise FileNotFoundError(f"image() cannot read image file '{ path }': file does not exist.")
    if path.is_dir():
        raise IsADirectoryError(f"image() cannot read image file '{ path }': expected a file, got a directory.")

    mime_type = detect_image_mime_type(path)
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        supported = ", ".join(sorted(SUPPORTED_IMAGE_MIME_TYPES))
        raise ValueError(f"image() only supports image files ({ supported }); got '{ path }'.")

    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise OSError(f"image() cannot read image file '{ path }': { exc }") from exc
    return f"data:{mime_type};base64,{data}"


def detect_image_mime_type(path: Path) -> str | None:
    try:
        header = path.read_bytes()[:16]
    except OSError:
        header = b""

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return "image/gif"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"BM"):
        return "image/bmp"

    guessed_type = mimetypes.guess_type(str(path))[0]
    if guessed_type and guessed_type.startswith("image/"):
        return guessed_type
    return guessed_type


def _validate_question(question: str) -> str:
    if not isinstance(question, str):
        raise TypeError("image() question must be a string.")
    question_text = question.strip()
    if not question_text:
        raise ValueError("image() question must be a non-empty string.")
    return question_text


def _coerce_sequence(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"image() { name } must be a list or tuple.")
    return list(value)


def _validate_url(url: str) -> str:
    if not isinstance(url, str):
        raise TypeError("image() url values must be strings.")
    url_text = url.strip()
    if not url_text:
        raise ValueError("image() url values must be non-empty strings.")
    return url_text


def _validate_detail(detail: ImageDetail) -> ImageDetail:
    if detail not in ("low", "high", "auto"):
        raise ValueError("image() detail must be one of: low, high, auto.")
    return detail


def append_image(prompt: Any, settings: Any, attachment: list[dict[str, Any]], *, mode: str = "vlm", mappings: dict[str, Any] | None = None) -> None:
    """Append one image group while retaining its local instruction and mode."""
    if mode not in {"vlm", "llm", "ocr"}:
        raise ValueError("image mode must be vlm, llm, or ocr.")
    current = list(prompt.get("attachment", []) or [])
    groups = list(settings.get("execution.image_groups", []) or [])
    groups.append({"start": len(current), "end": len(current) + len(attachment), "mode": mode})
    prompt.set("attachment", current + attachment, mappings=mappings)
    settings.set("execution.image_groups", groups)
