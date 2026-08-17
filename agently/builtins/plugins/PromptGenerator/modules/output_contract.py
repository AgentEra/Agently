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

"""Internal output-contract projection shared by structured prompt renderers."""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from agently.utils import DataPathBuilder


PYDANTIC_CONTRACT_META_KEY = "__agently_pydantic_output_contract__"


def _is_pydantic_model_type(value: Any) -> bool:
    return isinstance(value, type) and issubclass(value, BaseModel)


def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _split_nullable_annotation(annotation: Any) -> tuple[Any, bool]:
    annotation = _strip_annotated(annotation)
    if get_origin(annotation) not in (Union, types.UnionType):
        return annotation, False
    args = get_args(annotation)
    non_null_args = tuple(arg for arg in args if arg is not type(None))
    if len(non_null_args) == len(args):
        return annotation, False
    if len(non_null_args) == 1:
        return _strip_annotated(non_null_args[0]), True
    return annotation, True


def _json_schema_allows_null(schema: Mapping[str, Any]) -> bool:
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    for union_key in ("anyOf", "oneOf"):
        options = schema.get(union_key)
        if isinstance(options, list) and any(
            isinstance(option, Mapping) and option.get("type") == "null" for option in options
        ):
            return True
    return False


def _effective_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    effective = dict(schema)
    for union_key in ("anyOf", "oneOf"):
        options = effective.get(union_key)
        if not isinstance(options, list):
            continue
        non_null_options = [
            option for option in options if isinstance(option, Mapping) and option.get("type") != "null"
        ]
        if len(non_null_options) == 1:
            effective.pop(union_key, None)
            effective.update(dict(non_null_options[0]))
        break
    return effective


def _inline_local_json_schema_refs(
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    definitions = schema.get("$defs")
    resolved_definitions = (
        dict(definitions) if isinstance(definitions, Mapping) else {}
    )

    def resolve(value: Any, stack: frozenset[str] = frozenset()) -> Any:
        if isinstance(value, list):
            return [resolve(item, stack) for item in value]
        if not isinstance(value, Mapping):
            return value
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            declared = resolved_definitions.get(name)
            if isinstance(declared, Mapping) and name not in stack:
                merged = {
                    **dict(declared),
                    **{
                        key: item
                        for key, item in value.items()
                        if key != "$ref"
                    },
                }
                return resolve(merged, stack | {name})
        return {
            key: resolve(item, stack)
            for key, item in value.items()
            if key != "$defs"
        }

    resolved = resolve(schema)
    return dict(resolved) if isinstance(resolved, Mapping) else {}


def _annotation_json_schema(annotation: Any) -> dict[str, Any]:
    try:
        schema = TypeAdapter(annotation).json_schema()
    except Exception:
        return {}
    if not isinstance(schema, Mapping):
        return {}
    return _inline_local_json_schema_refs(_effective_json_schema(schema))


def output_schema_to_json_schema(
    output_schema: Any,
    *,
    strict_output: bool = False,
) -> dict[str, Any]:
    """Project an Agently output declaration without erasing nested containers."""

    def field_is_required(field_schema: Any) -> bool:
        if strict_output:
            return True
        if not isinstance(field_schema, tuple):
            return False
        pydantic_contract = _get_pydantic_contract(field_schema)
        if pydantic_contract is not None:
            return bool(pydantic_contract.get("required", False))
        marker = field_schema[2] if len(field_schema) >= 3 else None
        return DataPathBuilder.get_ensure_policy(marker) is not None

    def project(value: Any) -> dict[str, Any]:
        if isinstance(value, tuple):
            declared = value[0] if value else Any
            projected = project(declared)
            if len(value) >= 2 and value[1]:
                projected = {
                    **projected,
                    "description": str(value[1]),
                }
            return projected
        if isinstance(value, Mapping):
            properties = {
                str(key): project(child)
                for key, child in value.items()
            }
            required = [
                str(key)
                for key, child in value.items()
                if field_is_required(child)
            ]
            projected: dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "additionalProperties": not strict_output,
            }
            if required:
                projected["required"] = required
            return projected
        if isinstance(value, list):
            return {
                "type": "array",
                "items": project(value[0]) if value else {},
            }
        if isinstance(value, str):
            return {"description": value} if value else {}
        return _annotation_json_schema(value)

    return project(output_schema)


def _pydantic_contract_meta(
    annotation: Any,
    *,
    required: bool,
) -> dict[str, Any]:
    try:
        json_schema = TypeAdapter(annotation).json_schema()
    except Exception:
        json_schema = {}
    if not isinstance(json_schema, Mapping):
        json_schema = {}
    effective_schema = _effective_json_schema(json_schema)
    constraint_keys = (
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "pattern",
        "enum",
        "const",
        "format",
    )
    constraints = {key: effective_schema[key] for key in constraint_keys if key in effective_schema}
    return {
        PYDANTIC_CONTRACT_META_KEY: {
            "required": required,
            "nullable": _json_schema_allows_null(json_schema),
            "value_kind": effective_schema.get("type"),
            "constraints": constraints,
        }
    }


def _annotation_to_output_schema(
    annotation: Any,
    *,
    model_stack: frozenset[type[BaseModel]],
    attach_contract: bool = False,
) -> Any:
    original_annotation = annotation
    annotation, _ = _split_nullable_annotation(annotation)

    if _is_pydantic_model_type(annotation):
        output_schema: Any = (
            annotation.__name__
            if annotation in model_stack
            else pydantic_model_to_output_schema(
                annotation,
                model_stack=model_stack,
            )
        )
    elif get_origin(annotation) in (list, set, tuple):
        item_args = get_args(annotation)
        item_type = item_args[0] if item_args else Any
        output_schema = [
            _annotation_to_output_schema(
                item_type,
                model_stack=model_stack,
                attach_contract=True,
            )
        ]
    else:
        output_schema = annotation

    if not attach_contract:
        return output_schema

    contract_meta = _pydantic_contract_meta(
        original_annotation,
        required=False,
    )
    contract = contract_meta[PYDANTIC_CONTRACT_META_KEY]
    if contract["nullable"] or contract["constraints"]:
        return (output_schema, "", None, contract_meta)
    return output_schema


def pydantic_model_to_output_schema(
    model_type: type[BaseModel],
    *,
    model_stack: frozenset[type[BaseModel]] = frozenset(),
) -> dict[str, Any]:
    next_stack = model_stack | {model_type}
    schema: dict[str, Any] = {}
    for field_name, field in model_type.model_fields.items():
        output_name = field.alias if isinstance(field.alias, str) else field_name
        annotation = field.rebuild_annotation()
        field_schema = _annotation_to_output_schema(
            annotation,
            model_stack=next_stack,
        )
        description = field.description or ""
        contract_meta = _pydantic_contract_meta(
            annotation,
            required=field.is_required(),
        )
        schema[output_name] = (
            field_schema,
            description,
            True if field.is_required() else None,
            contract_meta,
        )
    return schema


def _get_pydantic_contract(field_spec: Any) -> Mapping[str, Any] | None:
    if not isinstance(field_spec, tuple) or len(field_spec) < 4:
        return None
    metadata = field_spec[3]
    if not isinstance(metadata, Mapping):
        return None
    contract = metadata.get(PYDANTIC_CONTRACT_META_KEY)
    return contract if isinstance(contract, Mapping) else None


def _format_compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _format_pydantic_constraint_notes(contract: Mapping[str, Any]) -> list[str]:
    notes: list[str] = []
    required = bool(contract.get("required", False))
    nullable = bool(contract.get("nullable", False))
    if required:
        notes.append("required")
        notes.append("null is allowed" if nullable else "must not be null")

    constraints = contract.get("constraints")
    if not isinstance(constraints, Mapping):
        return notes
    value_kind = contract.get("value_kind")

    minimum = constraints.get("minimum")
    exclusive_minimum = constraints.get("exclusiveMinimum")
    maximum = constraints.get("maximum")
    exclusive_maximum = constraints.get("exclusiveMaximum")
    lower_bound = (
        f"> {exclusive_minimum}"
        if exclusive_minimum is not None
        else (f">= {minimum}" if minimum is not None else None)
    )
    upper_bound = (
        f"< {exclusive_maximum}"
        if exclusive_maximum is not None
        else (f"<= {maximum}" if maximum is not None else None)
    )

    min_length = constraints.get("minLength")
    max_length = constraints.get("maxLength")
    min_items = constraints.get("minItems")
    max_items = constraints.get("maxItems")
    min_properties = constraints.get("minProperties")
    max_properties = constraints.get("maxProperties")

    if min_length is not None or max_length is not None:
        length = (
            f"{min_length}..{max_length}"
            if min_length is not None and max_length is not None
            else (f">= {min_length}" if min_length is not None else f"<= {max_length}")
        )
        notes.append(f"length: {length} characters")
    if min_items is not None or max_items is not None:
        item_count = (
            f"{min_items}..{max_items}"
            if min_items is not None and max_items is not None
            else (f">= {min_items}" if min_items is not None else f"<= {max_items}")
        )
        notes.append(f"item count: {item_count}")
    if min_properties is not None or max_properties is not None:
        property_count = (
            f"{min_properties}..{max_properties}"
            if min_properties is not None and max_properties is not None
            else (f">= {min_properties}" if min_properties is not None else f"<= {max_properties}")
        )
        notes.append(f"property count: {property_count}")
    if lower_bound or upper_bound:
        bounds = " and ".join(bound for bound in (lower_bound, upper_bound) if bound)
        notes.append(f"value: {bounds}")
    if "multipleOf" in constraints:
        notes.append(f"multiple of {constraints['multipleOf']}")
    if "enum" in constraints:
        notes.append(f"allowed values: {_format_compact_json(constraints['enum'])}")
    if "const" in constraints:
        notes.append(f"constant value: {_format_compact_json(constraints['const'])}")
    if "pattern" in constraints:
        notes.append(f"pattern: {constraints['pattern']}")
    if "format" in constraints and value_kind == "string":
        notes.append(f"format: {constraints['format']}")
    return notes


def _field_requirement_parts(
    field_spec: Any,
    *,
    replace_slot_references: Callable[[Any, dict[str, str] | None], Any],
    title_mapping: dict[str, str] | None,
) -> list[str]:
    if not isinstance(field_spec, tuple):
        return []
    description = ""
    if len(field_spec) >= 2 and field_spec[1]:
        description = str(replace_slot_references(field_spec[1], title_mapping))

    pydantic_contract = _get_pydantic_contract(field_spec)
    if pydantic_contract is not None:
        notes = _format_pydantic_constraint_notes(pydantic_contract)
        return [description, *notes] if description and notes else notes

    ensure_marker = field_spec[2] if len(field_spec) >= 3 else None
    ensure_policy = DataPathBuilder.get_ensure_policy(ensure_marker)
    if ensure_policy == "presence":
        notes = [
            "required key",
            "null or empty values are allowed",
        ]
    elif ensure_policy == "not_null":
        notes = [
            "required key",
            "value must not be null, blank, or empty",
        ]
    else:
        return []
    return [description, *notes] if description else notes


def generate_output_requirement_lines(
    output: Any,
    *,
    replace_slot_references: Callable[[Any, dict[str, str] | None], Any],
    title_mapping: dict[str, str] | None = None,
) -> list[str]:
    requirements: list[tuple[str, list[str]]] = []

    def traverse(value: Any, path: str):
        if isinstance(value, tuple):
            parts = _field_requirement_parts(
                value,
                replace_slot_references=replace_slot_references,
                title_mapping=title_mapping,
            )
            if path and parts:
                requirements.append((path, parts))
            if value:
                traverse(value[0], path)
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                traverse(child, child_path)
            return
        if isinstance(value, (list, set)):
            for child in value:
                child_path = f"{path}[*]" if path else "[*]"
                traverse(child, child_path)

    traverse(output, "")
    if not requirements:
        return []
    return [
        "",
        "Field requirements:",
        *[f"- {path}: {'; '.join(parts)}" for path, parts in requirements],
    ]
