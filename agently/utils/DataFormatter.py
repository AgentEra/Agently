# Copyright 2023-2025 AgentEra(Agently.Tech)
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

import re
import datetime
import warnings
from typing import (
    Any,
    Literal,
    Mapping,
    Sequence,
    Union,
    get_origin,
    get_args,
    overload,
    TYPE_CHECKING,
    TypeVar,
)
from pydantic import BaseModel

if TYPE_CHECKING:
    from agently.types.data import SerializableValue, KwargsType
    from re import Pattern

T = TypeVar("T")

DEFAULT_PLACEHOLDER_PATTERN = re.compile(r"\$\{\s*([^}]+?)\s*\}")


class DataFormatter:
    @staticmethod
    def sanitize(value: Any, *, remain_type: bool = False) -> Any:
        from .RuntimeData import RuntimeData, RuntimeDataNamespace

        if isinstance(value, (str, int, float, bool)) or value is None:
            return value

        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()

        if issubclass(type(value), RuntimeData) or issubclass(type(value), RuntimeDataNamespace):
            return DataFormatter.sanitize(value.data, remain_type=remain_type)

        if isinstance(value, type):
            if issubclass(value, BaseModel):
                extracted_value = {}
                for name, field in value.model_fields.items():
                    annotation = field.annotation
                    if hasattr(field, "rebuild_annotation"):
                        try:
                            annotation = field.rebuild_annotation()
                        except Exception:
                            annotation = field.annotation
                    extracted_value.update(
                        {
                            name: (
                                (
                                    DataFormatter.sanitize(annotation, remain_type=remain_type),
                                    field.description,
                                )
                                if field.description
                                else (DataFormatter.sanitize(annotation, remain_type=remain_type),)
                            )
                        }
                    )
                return extracted_value
            else:
                if remain_type:
                    return value
                return value.__name__ if hasattr(value, "__name__") else str(value)
        if get_origin(value) is not None:
            if remain_type:
                return value
            original_text = get_origin(value)
            args = get_args(value)
            if original_text is list:
                return f"list[{DataFormatter.sanitize(args[0], remain_type=remain_type)}]"
            if original_text is dict:
                return f"dict[{DataFormatter.sanitize(args[0], remain_type=remain_type)}, {DataFormatter.sanitize(args[1], remain_type=remain_type)}]"
            if original_text is tuple:
                return f"tuple[{', '.join(DataFormatter.sanitize(a, remain_type=remain_type) for a in args)}]"
            if original_text is Union:
                return " | ".join(str(DataFormatter.sanitize(a, remain_type=remain_type)) for a in args)
            if original_text is Literal:
                return f"Literal[{ ', '.join(str(DataFormatter.sanitize(a, remain_type=remain_type)) for a in args) }]"
            if isinstance(value, type) and hasattr(value, "__name__"):
                return value.__name__
            return str(value)

        if isinstance(value, dict):
            return {str(k): DataFormatter.sanitize(v, remain_type=remain_type) for k, v in value.items()}
        if isinstance(value, list):
            return [DataFormatter.sanitize(v, remain_type=remain_type) for v in value]
        if isinstance(value, set):
            return {DataFormatter.sanitize(v, remain_type=remain_type) for v in value}
        if isinstance(value, tuple):
            return tuple(DataFormatter.sanitize(v, remain_type=remain_type) for v in value)

        return str(value)

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: None = None,
        default_key: str,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, Any | T]: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["serializable"],
        default_key: str,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, "SerializableValue" | T]: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["str"],
        default_key: str,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, str | T]: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: None = None,
        default_key: None,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, Any] | T: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["serializable"],
        default_key: None,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, "SerializableValue"] | T: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["str"],
        default_key: None,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, str | T] | T: ...

    @overload
    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal['serializable', 'str'] | None = None,
        default_key: str | None = None,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, Any] | T: ...

    @staticmethod
    def to_str_key_dict(
        value: Any,
        *,
        value_format: Literal["serializable", "str"] | None = None,
        default_key: str | None = None,
        default_value: T = None,
        inconvertible_warning: bool = False,
    ) -> dict[str, Any] | T:
        if isinstance(value, Mapping):
            if value_format is None:
                return {str(DataFormatter.sanitize(k)): v for k, v in value.items()}
            elif value_format == "serializable":
                return {str(DataFormatter.sanitize(k)): DataFormatter.sanitize(v) for k, v in value.items()}
            elif value_format == "str":
                return {str(DataFormatter.sanitize(k)): str(DataFormatter.sanitize(v)) for k, v in value.items()}
        else:
            if inconvertible_warning:
                warnings.warn(
                    f"Error: Non-dictionary value cannot be convert to a string key dictionary.\n"
                    f"Value: { value }\n"
                    "Tips: You can provide parameter 'default_key' to allow DataFormatter.to_str_key_dict() convert non-dictionary value to a dictionary { default_key: value } automatically."
                )
            # restructure value to {default_key: value}
            if default_key is not None:
                return DataFormatter.to_str_key_dict(
                    {default_key: value},
                    value_format=value_format,
                    default_value=default_value,
                )
            return default_value

    @staticmethod
    def to_str(value: Any) -> str:
        return str(DataFormatter.sanitize(value))

    @staticmethod
    def from_schema_to_kwargs_format(input_schema: dict[str, Any] | None) -> "KwargsType | None":
        if input_schema and len(input_schema.keys()) > 0:
            if "type" not in input_schema:
                raise KeyError(f"Cannot find key 'type' in input schema: { input_schema }")
            if input_schema["type"] != "object":
                raise TypeError(f"Input schema type is not 'object' but: { input_schema['type'] }")

            kwargs_format: "KwargsType" = {}

            if "properties" in input_schema and input_schema["properties"]:
                properties = input_schema["properties"]
                for kwarg_name, kwarg_schema in properties.items():
                    kwarg_type = kwarg_schema.pop("type", Any)
                    kwarg_schema.pop("title", None)
                    kwarg_desc = ";".join([f"{k}: {v}" for k, v in kwarg_schema.items()]) if kwarg_schema else ""
                    kwargs_format[kwarg_name] = (kwarg_type, kwarg_desc)

            if "additionalProperties" in input_schema:
                additional_properties = input_schema["additionalProperties"]
                if additional_properties is False:
                    pass
                elif additional_properties is True or additional_properties is None:
                    kwargs_format["<*>"] = (Any, "")
                elif isinstance(additional_properties, dict):
                    additional_type = additional_properties.pop("type", Any)
                    additional_properties.pop("title", None)
                    additional_desc = (
                        ";".join([f"{k}: {v}" for k, v in additional_properties.items()])
                        if additional_properties
                        else ""
                    )
                    kwargs_format["<*>"] = (additional_type, additional_desc)
                else:
                    kwargs_format["<*>"] = (Any, "")

            return kwargs_format or None

        return None

    @staticmethod
    def substitute_placeholder(
        obj: T,
        variable_mappings: dict[str, Any],
        *,
        placeholder_pattern: "Pattern | None" = None,
    ) -> T | Any:
        if placeholder_pattern is None:
            placeholder_pattern = DEFAULT_PLACEHOLDER_PATTERN

        if not isinstance(variable_mappings, dict):
            raise TypeError(f"Variable mappings require a dictionary but got: { variable_mappings }")

        if isinstance(obj, str):
            full_match = placeholder_pattern.fullmatch(obj)
            if full_match:
                key = full_match.group(1).strip()
                return variable_mappings.get(key, obj)
            else:

                def replacer(match):
                    key = match.group(1).strip()
                    return str(variable_mappings.get(key, match.group(0)))

                return placeholder_pattern.sub(replacer, obj)

        if isinstance(obj, Mapping):
            return {
                DataFormatter.substitute_placeholder(
                    key,
                    variable_mappings,
                    placeholder_pattern=placeholder_pattern,
                ): DataFormatter.substitute_placeholder(
                    value,
                    variable_mappings,
                    placeholder_pattern=placeholder_pattern,
                )
                for key, value in obj.items()
            }

        if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            if isinstance(obj, tuple):
                return tuple(
                    DataFormatter.substitute_placeholder(
                        value,
                        variable_mappings,
                        placeholder_pattern=placeholder_pattern,
                    )
                    for value in obj
                )
            else:
                return [
                    DataFormatter.substitute_placeholder(
                        value,
                        variable_mappings,
                        placeholder_pattern=placeholder_pattern,
                    )
                    for value in obj
                ]

        if isinstance(obj, set):
            return {
                DataFormatter.substitute_placeholder(
                    value,
                    variable_mappings,
                    placeholder_pattern=placeholder_pattern,
                )
                for value in obj
            }

        return obj
