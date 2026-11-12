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

import json
from typing import Any, Literal, cast, get_args, get_origin

from agently.utils import DataFormatter


class FunctionTools:
    @staticmethod
    def _build_simple_type_schema(type_name: str) -> dict[str, Any]:
        normalized = type_name.strip()
        scalar_mapping = {
            "str": "string",
            "string": "string",
            "int": "integer",
            "integer": "integer",
            "float": "number",
            "number": "number",
            "bool": "boolean",
            "boolean": "boolean",
            "None": "null",
            "none": "null",
        }
        if normalized in scalar_mapping:
            return {"type": scalar_mapping[normalized]}
        if normalized in {"Any", "any", "unknown", "object"}:
            return {}
        if normalized.startswith("list[") and normalized.endswith("]"):
            item_type = normalized[5:-1].strip()
            return {"type": "array", "items": FunctionTools._build_simple_type_schema(item_type)}
        if normalized.startswith("dict[") and normalized.endswith("]"):
            return {"type": "object"}
        if normalized.startswith("Literal[") and normalized.endswith("]"):
            literal_body = normalized[len("Literal[") : -1]
            values = [item.strip() for item in literal_body.split(",") if item.strip()]
            enum_values = []
            for value in values:
                try:
                    enum_values.append(json.loads(value))
                except Exception:
                    enum_values.append(value.strip("'\""))
            schema: dict[str, Any] = {"enum": enum_values}
            if len(enum_values) > 0:
                if all(isinstance(item, str) for item in enum_values):
                    schema["type"] = "string"
                elif all(isinstance(item, bool) for item in enum_values):
                    schema["type"] = "boolean"
                elif all(isinstance(item, int) and not isinstance(item, bool) for item in enum_values):
                    schema["type"] = "integer"
                elif all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in enum_values):
                    schema["type"] = "number"
            return schema
        if "|" in normalized:
            options = [part.strip() for part in normalized.split("|") if part.strip()]
            return {"anyOf": [FunctionTools._build_simple_type_schema(option) for option in options]}
        return {}

    @classmethod
    def _annotation_to_schema(cls, annotation: Any) -> dict[str, Any]:
        if annotation is None:
            return {"type": "null"}
        if isinstance(annotation, dict):
            return cls._kwargs_to_json_schema(cast(dict[str, Any], annotation))
        if isinstance(annotation, str):
            return cls._build_simple_type_schema(annotation)

        origin = get_origin(annotation)
        if origin is list:
            args = get_args(annotation)
            item_annotation = args[0] if args else Any
            return {"type": "array", "items": cls._annotation_to_schema(item_annotation)}
        if origin is dict:
            return {"type": "object"}
        if origin is Literal:
            literal_values = list(get_args(annotation))
            schema: dict[str, Any] = {"enum": literal_values}
            if len(literal_values) > 0:
                first_value = literal_values[0]
                if isinstance(first_value, str):
                    schema["type"] = "string"
                elif isinstance(first_value, bool):
                    schema["type"] = "boolean"
                elif isinstance(first_value, int) and not isinstance(first_value, bool):
                    schema["type"] = "integer"
                elif isinstance(first_value, float):
                    schema["type"] = "number"
            return schema
        if origin is not None:
            return cls._build_simple_type_schema(str(DataFormatter.sanitize(annotation)))

        if isinstance(annotation, type):
            base_mapping = {
                str: {"type": "string"},
                int: {"type": "integer"},
                float: {"type": "number"},
                bool: {"type": "boolean"},
            }
            if annotation in base_mapping:
                return base_mapping[annotation].copy()
            if hasattr(annotation, "model_json_schema"):
                try:
                    return cast(dict[str, Any], annotation.model_json_schema())
                except Exception:
                    return {"type": "object"}
        sanitized = DataFormatter.sanitize(annotation)
        if isinstance(sanitized, dict):
            return cls._kwargs_to_json_schema(cast(dict[str, Any], sanitized))
        if isinstance(sanitized, str):
            return cls._build_simple_type_schema(sanitized)
        return {}

    @classmethod
    def _kwargs_to_json_schema(
        cls,
        kwargs_schema: dict[str, Any] | None,
        required_input_keys: Any = None,
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        additional_properties: bool | dict[str, Any] = False
        if not isinstance(kwargs_schema, dict) or len(kwargs_schema) == 0:
            return {"type": "object", "properties": {}}

        for key, raw_value in kwargs_schema.items():
            if key == "<*>":
                wildcard_value = raw_value
                wildcard_annotation = wildcard_value[0] if isinstance(wildcard_value, tuple) and wildcard_value else wildcard_value
                wildcard_schema = cls._annotation_to_schema(wildcard_annotation)
                additional_properties = wildcard_schema if len(wildcard_schema) > 0 else True
                continue

            annotation = raw_value
            description = None
            if isinstance(raw_value, tuple):
                annotation = raw_value[0] if len(raw_value) > 0 else Any
                if len(raw_value) > 1 and isinstance(raw_value[1], str) and raw_value[1]:
                    description = raw_value[1]
            schema = cls._annotation_to_schema(annotation)
            if description:
                schema = schema.copy()
                schema["description"] = description
            properties[str(key)] = schema

        result: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        required = [
            str(key)
            for key in required_input_keys
            if isinstance(key, str) and key in properties
        ] if isinstance(required_input_keys, (list, tuple, set)) else []
        if required:
            result["required"] = required
        if additional_properties is not False:
            result["additionalProperties"] = additional_properties
        else:
            result["additionalProperties"] = False
        return result

    @classmethod
    def _tool_name(cls, tool: dict[str, Any]) -> str | None:
        name = tool.get("name", tool.get("action_id"))
        return str(name) if isinstance(name, str) and name.strip() else None
    @classmethod

    def _prompt_tool_functions(cls, prompt_tools: Any) -> list[dict[str, Any]]:
        if not isinstance(prompt_tools, list):
            return []
        result: list[dict[str, Any]] = []
        for tool in prompt_tools:
            if not isinstance(tool, dict):
                continue
            name = cls._tool_name(tool)
            if name is None:
                continue
            description = tool.get("desc", tool.get("description", ""))
            parameters = cls._kwargs_to_json_schema(
                cast(dict[str, Any] | None, tool.get("kwargs")),
                tool.get("required_input_keys"),
            )
            result.append(
                {
                    "name": name,
                    "description": str(description) if description is not None else "",
                    "parameters": parameters,
                }
            )
        return result
