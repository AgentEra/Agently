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

"""Structured output parsing helpers for Agently output contracts."""

from __future__ import annotations

import re
import json
from typing import Annotated, Any, Callable, Literal, Mapping, get_args, get_origin

import json5
import yaml
from pydantic import BaseModel

from agently.types.data.prompt import _classify_field_spec
from agently.utils import DataLocator, StreamingJSONCompleter


STRUCTURED_OUTPUT_FORMATS = {"json", "flat_markdown", "hybrid", "xml_field", "yaml_literal"}


def parse_json_output(
    text: str,
    output_schema: Any,
    build_result_object: Callable[[Any], BaseModel | None],
) -> tuple[str | None, Any, BaseModel | None, bool]:
    declaration = output_schema[0] if isinstance(output_schema, tuple) and output_schema else output_schema
    while get_origin(declaration) is Annotated:
        declaration = get_args(declaration)[0]
    scalar_literal = get_origin(declaration) is Literal and all(isinstance(value, (str, int, float, bool)) for value in get_args(declaration))
    if declaration in (str, int, float, bool) or scalar_literal:
        # A scalar JSON root has no object/list boundary to locate. Do not
        # mistake brackets inside a string for a second structured result.
        scalar_text = text.strip()
        if scalar_text.startswith("```"):
            lines = scalar_text.splitlines()
            if len(lines) < 3 or lines[0].lower() not in {"```", "```json"} or lines[-1] != "```":
                return None, None, None, False
            scalar_text = "\n".join(lines[1:-1])
        try:
            parsed = json.loads(scalar_text)
        except (ValueError, TypeError):
            return None, None, None, False
        return scalar_text, parsed, build_result_object(parsed), False
    schema_for_location = declaration if isinstance(declaration, (Mapping, list)) else {}
    cleaned_json = DataLocator.locate_output_json(text, schema_for_location)
    if cleaned_json is None:
        return None, None, None, False

    completer = StreamingJSONCompleter()
    completer.reset(cleaned_json)
    completed = completer.complete()
    try:
        parsed = json5.loads(completed)
        return completed, parsed, build_result_object(parsed), False
    except Exception:
        repaired_json = DataLocator.repair_json_fragment(cleaned_json)
        if repaired_json == cleaned_json:
            return completed, None, None, False

        completer.reset(repaired_json)
        repaired_completed = completer.complete()
        try:
            parsed = json5.loads(repaired_completed)
            return repaired_completed, parsed, build_result_object(parsed), True
        except Exception:
            return repaired_completed, None, None, False


def parse_flat_markdown_output(text: str, output_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(output_schema, Mapping) or not output_schema:
        return None

    field_names = list(output_schema.keys())
    if not field_names:
        return None
    escaped_names = "|".join(re.escape(name) for name in field_names)
    pattern = rf"^###\s+({escaped_names})\s*(?:\[(?:text|JSON)\])?\s*$"

    sections = re.split(pattern, text, flags=re.MULTILINE)
    result: dict[str, Any] = {}
    for index in range(1, len(sections), 2):
        field_name = sections[index].strip()
        content = sections[index + 1].strip() if index + 1 < len(sections) else ""
        field_spec = output_schema.get(field_name)
        if _classify_field_spec(field_spec) == "complex":
            ok, value = normalize_complex_section_value(
                content,
                field_name=field_name,
            )
        else:
            ok, value = normalize_scalar_section_value(
                content,
                field_name=field_name,
            )
        if not ok:
            return None
        result[field_name] = value

    return result if result else None


def parse_hybrid_output(text: str, output_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(output_schema, Mapping) or not output_schema:
        return None

    field_names = list(output_schema.keys())
    if not field_names:
        return None
    escaped_names = "|".join(re.escape(name) for name in field_names)
    pattern = rf"^###\s+({escaped_names})\s*(?:\[(?:text|JSON)\])?\s*$"

    sections = re.split(pattern, text, flags=re.MULTILINE)
    result: dict[str, Any] = {}
    for index in range(1, len(sections), 2):
        field_name = sections[index].strip()
        content = sections[index + 1].strip() if index + 1 < len(sections) else ""
        field_spec = output_schema.get(field_name)

        if not _is_hybrid_text_field(field_spec):
            json_text = _extract_json_block(content)
            if json_text is None:
                json_text = DataLocator.locate_output_json(content, {field_name: field_spec})
            ok, value = normalize_json_section_value(
                json_text if json_text is not None else content,
                field_name=field_name,
            )
        else:
            ok, value = normalize_scalar_section_value(
                content,
                field_name=field_name,
            )
        if not ok:
            return None
        result[field_name] = value

    return result if result else None


def extract_xml_field_target(text: str) -> str | None:
    start = _TARGET_START_RE.search(text)
    if start is None:
        return None
    end = None
    for match in _TARGET_END_RE.finditer(text, start.end()):
        end = match
    if end is None:
        return None
    return text[start.start(): end.end()]


def parse_xml_field_output(text: str, output_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(output_schema, Mapping) or not output_schema:
        return None

    target = extract_xml_field_target(text)
    if target is None:
        return None

    result: dict[str, Any] = {}
    for field_name, field_type, raw_content in _iter_field_sections(target):
        if field_name not in output_schema:
            continue
        content = raw_content.strip()
        if field_type == "json":
            ok, value = normalize_json_section_value(content, field_name=field_name)
        else:
            ok, value = normalize_scalar_section_value(content, field_name=field_name)
        if not ok:
            return None
        result[field_name] = value

    return result if result else None


def extract_yaml_literal_target(text: str) -> str | None:
    match = _YAML_TARGET_RE.search(text)
    if match:
        return match.group(1).strip()
    fence = re.search(r"```(?:yaml|yml)?[ \t]*\n(.*?)\n?```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return None


def parse_yaml_literal_output(text: str, output_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(output_schema, Mapping) or not output_schema:
        return None
    target = extract_yaml_literal_target(text)
    if target is None:
        return None
    data = yaml.safe_load(target)
    if not isinstance(data, dict):
        return None
    return data


def parse_output_contract_dict(
    text: str,
    *,
    output_schema: Mapping[str, Any],
    output_format: str,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        match output_format:
            case "json":
                _cleaned, parsed, _result_object, _repaired = parse_json_output(
                    text,
                    output_schema,
                    lambda _parsed: None,
                )
            case "flat_markdown":
                parsed = parse_flat_markdown_output(text, output_schema)
            case "hybrid":
                parsed = parse_hybrid_output(text, output_schema)
            case "xml_field":
                parsed = parse_xml_field_output(text, output_schema)
            case "yaml_literal":
                parsed = parse_yaml_literal_output(text, output_schema)
            case _:
                return None, f"Unsupported output format: {output_format}"
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    if not isinstance(parsed, Mapping):
        return None, f"{output_format} parser did not produce a dict."
    return dict(parsed), None


_FENCE_LINE = re.compile(r"^(`{3,}|~{3,})([^\n]*)$")
_INFO_TOKEN = re.compile(r"[\w.+#-]*")
_COMMENT_RE = re.compile(r"^\s*<!--(?P<body>.*?)-->\s*$", flags=re.DOTALL)
_LEADING_SCAFFOLD_COMMENT_RE = re.compile(
    r"^\s*<!--\s*\((?:text|json)\)(?:\s+.*?)?\s*-->\s*(?:\r?\n)?",
    flags=re.IGNORECASE,
)
_ANGLE_PLACEHOLDER_RE = re.compile(
    r"^\s*\(?\s*(?:json|text)?\s*\)?\s*<[^>\n]{1,160}>\s*$",
    flags=re.IGNORECASE,
)
_PLACEHOLDER_HINT_RE = re.compile(
    r"(?:placeholder|description|your\s+\w+|todo|字段|描述|说明|内容)",
    flags=re.IGNORECASE,
)
_TARGET_START_RE = re.compile(r"<agently_output\b[^>]*>", flags=re.IGNORECASE)
_TARGET_END_RE = re.compile(r"</agently_output>", flags=re.IGNORECASE)
_FIELD_START_RE = re.compile(
    r"<field\s+name=[\"']([^\"']+)[\"']\s+type=[\"'](text|json)[\"']\s*>",
    flags=re.IGNORECASE,
)
_FIELD_CLOSE_RE = re.compile(
    r"</field\s*>(?=\s*(?:<field\b|</agently_output>))",
    flags=re.IGNORECASE,
)
_YAML_TARGET_RE = re.compile(
    r"<<<BEGIN\s+AGENTLY_YAML>>>\s*(.*?)\s*<<<END\s+AGENTLY_YAML>>>",
    flags=re.DOTALL | re.IGNORECASE,
)


def normalize_scalar_section_value(
    content: str,
    *,
    field_name: str,
) -> tuple[bool, Any]:
    raw = _clean_section_text(content)
    if not raw and content.strip():
        return False, None
    if _looks_like_placeholder(raw):
        return False, None

    if _should_parse_scalar_json(raw):
        parsed_ok, parsed = _try_parse_json(raw)
        if parsed_ok:
            return _normalize_json_value(parsed, field_name=field_name, scalar=True)

    return True, raw


def normalize_complex_section_value(
    content: str,
    *,
    field_name: str,
) -> tuple[bool, Any]:
    raw = _clean_section_text(content)
    if not raw and content.strip():
        return False, None
    if _looks_like_placeholder(raw):
        return False, None

    parsed_ok, parsed = _try_parse_json(raw)
    if not parsed_ok:
        return False, None
    return _normalize_json_value(parsed, field_name=field_name, scalar=False)


def normalize_json_section_value(
    content: str,
    *,
    field_name: str,
) -> tuple[bool, Any]:
    raw = _clean_section_text(content)
    if not raw and content.strip():
        return False, None
    if _looks_like_placeholder(raw):
        return False, None

    parsed_ok, parsed = _try_parse_json(raw)
    if not parsed_ok:
        return False, None
    if isinstance(parsed, dict) and set(parsed.keys()) == {field_name}:
        parsed = parsed[field_name]
    if isinstance(parsed, str) and _looks_like_placeholder(parsed):
        return False, None
    return True, parsed


def _extract_json_block(content: str) -> str | None:
    match = re.match(r"\s*```(?:json)?[ \t]*\n(.*?)\n?```\s*", content, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _is_hybrid_text_field(field_spec: Any) -> bool:
    if isinstance(field_spec, tuple) and field_spec:
        return field_spec[0] in (str, "str")
    return field_spec in (str, "str")


def _iter_field_sections(target: str):
    search_pos = 0
    while True:
        start = _FIELD_START_RE.search(target, search_pos)
        if start is None:
            return
        close = _FIELD_CLOSE_RE.search(target, start.end())
        if close is None:
            return
        yield start.group(1), start.group(2).lower(), target[start.end(): close.start()]
        search_pos = close.end()


def _clean_section_text(content: str) -> str:
    raw = _strip_enclosing_code_fence(content).strip()
    while True:
        cleaned = _LEADING_SCAFFOLD_COMMENT_RE.sub("", raw, count=1).strip()
        if cleaned == raw:
            return raw
        raw = cleaned


def _strip_enclosing_code_fence(text: str) -> str:
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    lines = stripped.split("\n")
    if len(lines) < 2:
        return text

    open_match = _FENCE_LINE.match(lines[0])
    if not open_match:
        return text
    marker, info = open_match.group(1), open_match.group(2).strip()
    fence_char, open_len = marker[0], len(marker)
    if not _INFO_TOKEN.fullmatch(info):
        return text

    for index in range(1, len(lines)):
        if _is_closing_fence(lines[index], fence_char=fence_char, min_len=open_len):
            if index != len(lines) - 1:
                return text
            return "\n".join(lines[1:index])
    return text


def _is_closing_fence(line: str, *, fence_char: str, min_len: int) -> bool:
    match = _FENCE_LINE.match(line)
    if not match:
        return False
    marker = match.group(1)
    return marker[0] == fence_char and len(marker) >= min_len and not match.group(2).strip()


def _try_parse_json(text: str) -> tuple[bool, Any]:
    try:
        return True, json5.loads(text)
    except Exception:
        return False, None


def _should_parse_scalar_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("{", "[", '"'))


def _normalize_json_value(
    value: Any,
    *,
    field_name: str,
    scalar: bool,
) -> tuple[bool, Any]:
    if isinstance(value, dict):
        if set(value.keys()) != {field_name}:
            return False, None
        value = value[field_name]

    if isinstance(value, str) and _looks_like_placeholder(value):
        return False, None

    if scalar:
        if isinstance(value, (dict, list)):
            return False, None
        return True, value

    if isinstance(value, (dict, list)):
        return True, value
    return False, None


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False

    comment = _COMMENT_RE.match(stripped)
    if comment:
        body = comment.group("body").strip()
        return bool(_ANGLE_PLACEHOLDER_RE.match(body) or _PLACEHOLDER_HINT_RE.search(body))

    if _ANGLE_PLACEHOLDER_RE.match(stripped) and _PLACEHOLDER_HINT_RE.search(stripped):
        return True

    return False
