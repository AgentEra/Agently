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

import json
import re
import warnings
import yaml
from enum import Enum

from typing import (
    Any,
    Generator,
    List,
    Mapping,
    Sequence,
    Annotated,
    TYPE_CHECKING,
    TypeGuard,
    get_origin,
    get_args,
    cast,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    PlainValidator,
    PlainSerializer,
    TypeAdapter,
    Field,
    ValidationInfo,
    create_model,
    model_validator,
)

from agently.types.plugins import PromptGenerator
from agently.types.data import PromptModel, ChatMessageContent, TextMessageContent
from agently.types.data.prompt import _classify_field_spec
from agently.utils import SettingsNamespace, DataFormatter, DataPathBuilder, TimeInfo
from .modules.output_contract import (
    PYDANTIC_CONTRACT_META_KEY,
    generate_output_requirement_lines,
    pydantic_model_to_output_schema,
)

if TYPE_CHECKING:
    from pydantic import BaseModel
    from agently.types.data import SerializableMapping
    from agently.core import Prompt
    from agently.utils import Settings


class AgentlyPromptGenerator(PromptGenerator):
    name = "AgentlyPromptGenerator"

    _SLOT_REFERENCE_RE = re.compile(r"\$\{([^}]+)\}")
    _PROMPT_SLOT_TITLE_DEFAULTS = {
        "system": "SYSTEM",
        "developer": "DEVELOPER DIRECTIONS",
        "chat_history": "CHAT HISTORY",
        "info": "INFO",
        "tools": "TOOLS",
        "action_results": "ACTION RESULTS",
        "instruct": "INSTRUCT",
        "examples": "EXAMPLES",
        "input": "INPUT",
        "output_requirement": "OUTPUT REQUIREMENT",
        "output": "OUTPUT",
        "attachment": "ATTACHMENT",
    }

    _SANITIZED_SCALAR_TYPE_NAMES = {"str", "int", "float", "bool"}
    _PROMPT_SLOT_ALIASES = {
        "instruction": "instruct",
        "instructions": "instruct",
        "output": "output_requirement",
        "output_requirement": "output_requirement",
        "outputrequirement": "output_requirement",
    }

    DEFAULT_SETTINGS = {
        "$global": {
            "prompt": {
                "add_current_time": False,
            }
        }
    }

    class _RootListOutputModelMixin(BaseModel):
        model_config = ConfigDict(extra="allow")

        @model_validator(mode="before")
        @classmethod
        def _wrap_root_list(cls, value: Any):
            if not isinstance(value, Mapping) and isinstance(value, Sequence) and not isinstance(value, str):
                return {"list": value}
            return value

        @property
        def root(self):
            return getattr(self, "list")

        def __iter__(self) -> Generator[Any, None, None]:
            value = getattr(self, "list", None)
            if value is None:
                return
            yield from value

        def __len__(self):
            value = getattr(self, "list", None)
            return len(value) if isinstance(value, list) else 0

        def __getitem__(self, index):
            return getattr(self, "list")[index]

    def __init__(
        self,
        prompt: "Prompt",
        settings: "Settings",
    ):
        self.prompt = prompt
        self.settings = settings
        self.plugin_settings = SettingsNamespace(self.settings, f"plugins.PromptGenerator.{ self.name }")

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    @staticmethod
    def _get_enum_type(value: Any) -> type[Enum] | None:
        if isinstance(value, type) and issubclass(value, Enum):
            return value
        for arg in get_args(value):
            if isinstance(arg, type) and issubclass(arg, Enum):
                return arg
        return None

    @staticmethod
    def _is_pydantic_model_type(value: Any) -> TypeGuard[type[BaseModel]]:
        return isinstance(value, type) and issubclass(value, BaseModel)

    @staticmethod
    def _is_ensure_marker(value: Any) -> bool:
        return DataPathBuilder.is_ensure_marker(value)

    def _generate_output_requirement_lines(
        self,
        output: Any,
        *,
        title_mapping: dict[str, str] | None = None,
    ) -> list[str]:
        return generate_output_requirement_lines(
            output,
            replace_slot_references=self._replace_slot_references,
            title_mapping=title_mapping,
        )

    def _check_prompt_all_empty(self, prompt_object: PromptModel):
        # If prompt is customized, skip the check
        if prompt_object.model_extra:
            return
        # Check standard prompt keys
        fields_to_check = ["input", "info", "instruct", "output", "attachment"]
        all_empty = all(getattr(prompt_object, field) in (None, "", [], {}) for field in fields_to_check)
        if all_empty:
            raise KeyError(
                "Prompt requires at least one of 'input', 'info', 'instruct', 'output', 'attachment' or customize extra prompt keys to be provided."
            )

    def _replace_slot_references(
        self,
        value: Any,
        title_mapping: dict[str, str] | None = None,
    ) -> Any:
        if isinstance(value, str):
            return self._SLOT_REFERENCE_RE.sub(
                lambda match: self._render_slot_reference(
                    match.group(1),
                    original=match.group(0),
                    title_mapping=title_mapping,
                ),
                value,
            )
        if isinstance(value, Mapping):
            return {
                key: self._replace_slot_references(item, title_mapping)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._replace_slot_references(item, title_mapping) for item in value]
        if isinstance(value, tuple):
            return tuple(self._replace_slot_references(item, title_mapping) for item in value)
        return value

    def _render_slot_reference(
        self,
        expression: str,
        *,
        original: str,
        title_mapping: dict[str, str] | None = None,
    ) -> str:
        slot_expr, _, path = expression.strip().partition(".")
        slot_key = slot_expr.strip().lower().replace("-", "_")
        slot_key = self._PROMPT_SLOT_ALIASES.get(slot_key, slot_key)
        if slot_key not in self._PROMPT_SLOT_TITLE_DEFAULTS:
            return original
        title = str(
            (title_mapping or {}).get(
                slot_key,
                self._PROMPT_SLOT_TITLE_DEFAULTS[slot_key],
            )
        )
        path = path.strip()
        return f"[{ title } > { path }]" if path else f"[{ title }]"

    def _generate_json_output_prompt(
        self,
        output: Any,
        layer: int = 0,
        title_mapping: dict[str, str] | None = None,
    ) -> str:
        indent = "  " * layer
        next_indent = "  " * (layer + 1)

        if isinstance(output, dict):
            if not output:
                return "{}"
            lines = ["{"]
            items = list(output.items())
            for i, (key, value) in enumerate(items):
                value_str = self._generate_json_output_prompt(value, layer + 1, title_mapping)
                desc_str = ""
                if isinstance(value, tuple) and len(value) > 1:
                    desc_str = " //"
                    if len(value) >= 2 and value[1]:
                        desc_str += f" {self._replace_slot_references(value[1], title_mapping)}"
                comma = "," if i < len(items) - 1 else ""
                lines.append(f'{next_indent}"{key}": {value_str}{comma}{desc_str}')
            lines.append(f"{indent}}}")
            return "\n".join(lines)

        if isinstance(output, (list, set)):
            if not output:
                return "[]"
            lines = ["["]
            items = list(output)
            for i, item in enumerate(items):
                item_str = self._generate_json_output_prompt(item, layer + 1, title_mapping)
                desc_str = ""
                if isinstance(item, tuple) and len(item) > 1 and item[1]:
                    desc_str += f" // {self._replace_slot_references(item[1], title_mapping)}"
                if i < len(items):
                    lines.append(f"{next_indent}{item_str},{desc_str}")
            lines.append(f"{next_indent}...")
            lines.append(f"{indent}]")
            return "\n".join(lines)

        if isinstance(output, tuple) and len(output) >= 1:
            if isinstance(output[0], (dict, list, set)):
                return self._generate_json_output_prompt(output[0], layer + 1, title_mapping)
            return f"<{str(output[0])}>"

        if isinstance(output, type):
            return f"<{type(output).__name__}>"

        return f"<{str(output)}>"

    def _generate_flat_markdown_output_prompt(
        self,
        output: dict[str, Any],
        *,
        ensure_all_keys: bool = False,
        title_mapping: dict[str, str] | None = None,
    ) -> list[str]:
        lines: list[str] = [
            f"[{ (title_mapping or {}).get('output_requirement', 'OUTPUT REQUIREMENT') }]:",
            "Data Format: Structured Markdown",
            "",
            "Respond in clearly separated sections. Each section MUST start with the exact",
            "markdown header shown below (### field_name). Write your content after the header.",
            "Separate sections with a blank line.",
        ]
        if ensure_all_keys:
            lines.extend([
                "",
                "All defined fields are required. Every section header listed below MUST appear",
                "in your response exactly as specified.",
            ])
        lines.extend(
            self._generate_output_requirement_lines(
                output,
                title_mapping=title_mapping,
            )
        )
        lines.append("")
        lines.append("Required sections:")
        lines.append("")
        for field_name, field_spec in output.items():
            desc = ""
            if isinstance(field_spec, tuple) and len(field_spec) >= 2 and field_spec[1]:
                desc = f"<!-- {self._replace_slot_references(field_spec[1], title_mapping)} -->"
            lines.append(f"### {field_name}")
            if desc:
                lines.append(desc)
            lines.append("(your content here)")
            lines.append("")
        return lines

    @staticmethod
    def _is_string_output_field(field_spec: Any) -> bool:
        if isinstance(field_spec, tuple) and field_spec:
            return field_spec[0] in (str, "str")
        return field_spec in (str, "str")

    def _generate_yaml_literal_field_lines(self, field_name: str, field_spec: Any, indent: int = 0) -> list[str]:
        if isinstance(field_spec, tuple) and field_spec:
            return self._generate_yaml_literal_field_lines(field_name, field_spec[0], indent)

        pad = " " * indent
        if field_spec in (str, "str"):
            return [f"{pad}{field_name}: |", f"{pad}  write text here"]
        if field_spec in (bool, "bool", "boolean"):
            return [f"{pad}{field_name}: true"]
        if field_spec in (int, "int"):
            return [f"{pad}{field_name}: 3"]
        if field_spec in (float, "float", "number"):
            return [f"{pad}{field_name}: 3.14"]
        if isinstance(field_spec, dict):
            lines = [f"{pad}{field_name}:"]
            for child_name, child_spec in field_spec.items():
                lines.extend(self._generate_yaml_literal_field_lines(str(child_name), child_spec, indent + 2))
            return lines
        if isinstance(field_spec, list):
            item_spec = field_spec[0] if field_spec else (str,)
            lines = [f"{pad}{field_name}:"]
            if isinstance(item_spec, dict):
                first = True
                for child_name, child_spec in item_spec.items():
                    rendered = self._generate_yaml_literal_field_lines(str(child_name), child_spec, indent + 4)
                    if first:
                        lines.append(" " * (indent + 2) + "- " + rendered[0].strip())
                        lines.extend(rendered[1:])
                        first = False
                    else:
                        lines.extend(rendered)
            else:
                sample = "write text here" if item_spec in (str, "str", (str,), ("str",)) else "3"
                lines.append(" " * (indent + 2) + f"- {sample}")
            return lines
        return [f"{pad}{field_name}: \"...\""]

    def _generate_xml_field_output_prompt(
        self,
        output: dict[str, Any],
        *,
        ensure_all_keys: bool = False,
        title_mapping: dict[str, str] | None = None,
    ) -> list[str]:
        lines: list[str] = [
            f"[{ (title_mapping or {}).get('output_requirement', 'OUTPUT REQUIREMENT') }]:",
            "Data Format: XML-like field envelope",
            "",
            "Return exactly one <agently_output> payload. Each required field MUST be wrapped",
            "in <field name=\"field_name\" type=\"text|json\">...</field>.",
            "Use type=\"text\" only for string prose/code fields. Use type=\"json\" for",
            "lists, objects, booleans, numbers, and other strongly typed values.",
            "Text field content is raw text; it does not need XML entity escaping.",
        ]
        if ensure_all_keys:
            lines.extend([
                "",
                "All defined fields are required. Every field block listed below MUST appear.",
            ])
        lines.extend(
            self._generate_output_requirement_lines(
                output,
                title_mapping=title_mapping,
            )
        )
        lines.extend(["", "<agently_output>"])
        for field_name, field_spec in output.items():
            if self._is_string_output_field(field_spec):
                lines.extend([f'<field name="{field_name}" type="text">', "write text here", "</field>"])
            else:
                lines.extend([
                    f'<field name="{field_name}" type="json">',
                    self._generate_json_value_example(field_spec),
                    "</field>",
                ])
        lines.extend(["</agently_output>", ""])
        return lines

    def _generate_yaml_literal_output_prompt(
        self,
        output: dict[str, Any],
        *,
        ensure_all_keys: bool = False,
        title_mapping: dict[str, str] | None = None,
    ) -> list[str]:
        lines: list[str] = [
            f"[{ (title_mapping or {}).get('output_requirement', 'OUTPUT REQUIREMENT') }]:",
            "Data Format: YAML literal document",
            "",
            "Return exactly one YAML document between these boundary lines:",
            "<<<BEGIN AGENTLY_YAML>>>",
            "<<<END AGENTLY_YAML>>>",
            "Use YAML literal block scalars (|) for long text/code fields.",
        ]
        if ensure_all_keys:
            lines.extend([
                "",
                "All defined fields are required. Every top-level key listed below MUST appear.",
            ])
        lines.extend(
            self._generate_output_requirement_lines(
                output,
                title_mapping=title_mapping,
            )
        )
        lines.extend(["", "<<<BEGIN AGENTLY_YAML>>>"])
        for field_name, field_spec in output.items():
            lines.extend(self._generate_yaml_literal_field_lines(str(field_name), field_spec))
        lines.extend(["<<<END AGENTLY_YAML>>>", ""])
        return lines

    def _generate_hybrid_output_prompt(
        self,
        output: dict[str, Any],
        *,
        ensure_all_keys: bool = False,
        title_mapping: dict[str, str] | None = None,
    ) -> list[str]:
        lines: list[str] = [
            f"[{ (title_mapping or {}).get('output_requirement', 'OUTPUT REQUIREMENT') }]:",
            "Data Format: Structured Markdown with JSON blocks",
            "",
            "Respond in clearly separated sections. Each section MUST start with the exact",
            "markdown header shown below (### field_name).",
            "",
            "String text fields expect plain text content directly after the header.",
            "",
            "List, object, boolean, and number fields expect valid JSON inside a ```json code block.",
            "Write the opening ```json on its own line, then your JSON content, then",
            "the closing ``` on its own line.",
            "",
            "Separate sections with a blank line.",
        ]
        if ensure_all_keys:
            lines.extend([
                "",
                "All defined fields are required. Every section header listed below MUST appear",
                "in your response exactly as specified.",
            ])
        lines.extend(
            self._generate_output_requirement_lines(
                output,
                title_mapping=title_mapping,
            )
        )
        lines.append("")
        lines.append("Required sections:")
        lines.append("")
        for field_name, field_spec in output.items():
            kind = "scalar" if self._is_string_output_field(field_spec) else "json"
            desc = ""
            if isinstance(field_spec, tuple) and len(field_spec) >= 2 and field_spec[1]:
                desc = f"{self._replace_slot_references(field_spec[1], title_mapping)}"
            if desc:
                lines.append(f"- {field_name}: {'text' if kind == 'scalar' else 'json'}; {desc}")
            else:
                lines.append(f"- {field_name}: {'text' if kind == 'scalar' else 'json'}")
        lines.append("")
        lines.append("Response skeleton:")
        lines.append("")
        for field_name, field_spec in output.items():
            kind = "scalar" if self._is_string_output_field(field_spec) else "json"
            lines.append(f"### {field_name}")
            if kind == "scalar":
                lines.append("(your content here)")
            else:
                lines.append("```json")
                lines.append(self._generate_json_value_example(field_spec))
                lines.append("```")
            lines.append("")
        return lines

    def _generate_json_value_example(self, value: Any) -> str:
        if isinstance(value, tuple) and value:
            return self._generate_json_value_example(value[0])
        if value in (str, "str"):
            return '"..."'
        if value in (int, "int"):
            return "3"
        if value in (float, "float", "number"):
            return "3.14"
        if value in (bool, "bool", "boolean"):
            return "true"
        if isinstance(value, dict):
            items = [f'"{key}": {self._generate_json_value_example(child)}' for key, child in value.items()]
            return "{\n  " + ",\n  ".join(items) + "\n}" if items else "{}"
        if isinstance(value, list):
            item = value[0] if value else (str,)
            rendered = self._generate_json_value_example(item)
            if "\n" in rendered:
                rendered = "\n  ".join(rendered.splitlines())
                return "[\n  " + rendered + "\n]"
            return f"[{rendered}]"
        return '"..."'

    @classmethod
    def _classify_hybrid_field_spec(cls, field_spec: Any):
        if isinstance(field_spec, tuple) and field_spec:
            first = field_spec[0]
            if isinstance(first, str) and first in cls._SANITIZED_SCALAR_TYPE_NAMES:
                return "scalar"
        if isinstance(field_spec, str) and field_spec in cls._SANITIZED_SCALAR_TYPE_NAMES:
            return "scalar"
        return _classify_field_spec(field_spec)

    def _generate_yaml_prompt_list(self, title: str, prompt_part: Any) -> list[str]:
        title_mapping = cast(dict[str, str], self.settings.get("prompt.prompt_title_mapping", {}))
        if not isinstance(title_mapping, dict):
            title_mapping = {}
        sanitized_part = DataFormatter.sanitize(
            self._replace_slot_references(prompt_part, title_mapping)
        )
        return [
            f"[{ title }]:",
            (
                str(sanitized_part)
                if isinstance(sanitized_part, (str, int, float, bool)) or sanitized_part is None
                else yaml.safe_dump(sanitized_part, allow_unicode=True)
            ),
            "",
        ]

    def _generate_main_prompt(self, prompt_object: PromptModel):
        prompt_title_mapping = cast(dict[str, str], self.settings.get("prompt.prompt_title_mapping", {}))
        if not isinstance(prompt_title_mapping, dict):
            prompt_title_mapping = {}
        prompt_text_list = []
        # tools
        if prompt_object.tools and isinstance(prompt_object.tools, list):
            prompt_text_list.append(f"[{ prompt_title_mapping.get('tools', 'TOOLS') }]:")
            for tool_info in prompt_object.tools:
                if isinstance(tool_info, dict):
                    prompt_text_list.append("[")
                    for key, value in tool_info.items():
                        if key in ("kwargs", "returns"):
                            prompt_text_list.append(
                                f"{ key }: {self._generate_json_output_prompt(DataFormatter.sanitize(value), title_mapping=prompt_title_mapping)}"
                            )
                        else:
                            prompt_text_list.append(
                                f"{ key }: { self._replace_slot_references(value, prompt_title_mapping) }"
                            )
                    prompt_text_list.append("]")

        # action_results
        if prompt_object.action_results:
            prompt_text_list.extend(
                self._generate_yaml_prompt_list(
                    str(
                        prompt_title_mapping.get(
                            'action_results',
                            'ACTION RESULTS',
                        )
                    ),
                    prompt_object.action_results,
                )
            )

        # info
        if prompt_object.info:
            prompt_text_list.append(f"[{ prompt_title_mapping.get('info', 'INFO') }]:")
            if isinstance(prompt_object.info, Mapping):
                for title, content in prompt_object.info.items():
                    prompt_text_list.append(
                        f"- { title }: { DataFormatter.sanitize(self._replace_slot_references(content, prompt_title_mapping)) }"
                    )
            elif isinstance(prompt_object.info, Sequence) and not isinstance(prompt_object.info, str):
                prompt_text_list.extend(
                    [
                        f"- { DataFormatter.sanitize(self._replace_slot_references(info_line, prompt_title_mapping)) }"
                        for info_line in prompt_object.info
                    ]
                )
            else:
                prompt_text_list.append(
                    DataFormatter.sanitize(
                        self._replace_slot_references(prompt_object.info, prompt_title_mapping)
                    )
                )
            prompt_text_list.append("")

        # extra
        if prompt_object.model_extra:
            for title, content in prompt_object.model_extra.items():
                prompt_text_list.extend(self._generate_yaml_prompt_list(title, content))

        # instruct
        if prompt_object.instruct:
            prompt_text_list.extend(
                self._generate_yaml_prompt_list(
                    str(
                        prompt_title_mapping.get(
                            'instruct',
                            'INSTRUCT',
                        )
                    ),
                    prompt_object.instruct,
                )
            )

        # examples
        if prompt_object.examples:
            prompt_text_list.extend(
                self._generate_yaml_prompt_list(
                    str(
                        prompt_title_mapping.get(
                            'examples',
                            'EXAMPLES',
                        )
                    ),
                    prompt_object.examples,
                )
            )

        # input
        if prompt_object.input:
            prompt_text_list.extend(
                self._generate_yaml_prompt_list(
                    str(
                        prompt_title_mapping.get(
                            'input',
                            'INPUT',
                        )
                    ),
                    prompt_object.input,
                )
            )

        # output
        if prompt_object.output:
            match prompt_object.output_format:
                case "json":
                    final_output = DataFormatter.sanitize(prompt_object.output)
                    prompt_text_list.extend(
                        [
                            f"[{ prompt_title_mapping.get('output_requirement', 'OUTPUT REQUIREMENT') }]:",
                            "Data Format: JSON",
                            *(
                            [
                                "Output Guarantee: STRICT structure",
                                "All defined fields are required and extra fields are not allowed.",
                            ]
                                if getattr(prompt_object, "ensure_all_keys", False)
                                else []
                            ),
                            "Data Structure:",
                            self._generate_json_output_prompt(final_output, title_mapping=prompt_title_mapping),
                            *self._generate_output_requirement_lines(
                                final_output,
                                title_mapping=prompt_title_mapping,
                            ),
                            "",
                        ]
                    )
                case "flat_markdown":
                    prompt_text_list.extend(
                        self._generate_flat_markdown_output_prompt(
                            DataFormatter.sanitize(prompt_object.output),
                            ensure_all_keys=getattr(prompt_object, "ensure_all_keys", False),
                            title_mapping=prompt_title_mapping,
                        )
                    )
                case "hybrid":
                    prompt_text_list.extend(
                        self._generate_hybrid_output_prompt(
                            DataFormatter.sanitize(prompt_object.output),
                            ensure_all_keys=getattr(prompt_object, "ensure_all_keys", False),
                            title_mapping=prompt_title_mapping,
                        )
                    )
                case "xml_field":
                    prompt_text_list.extend(
                        self._generate_xml_field_output_prompt(
                            DataFormatter.sanitize(prompt_object.output),
                            ensure_all_keys=getattr(prompt_object, "ensure_all_keys", False),
                            title_mapping=prompt_title_mapping,
                        )
                    )
                case "yaml_literal":
                    prompt_text_list.extend(
                        self._generate_yaml_literal_output_prompt(
                            DataFormatter.sanitize(prompt_object.output),
                            ensure_all_keys=getattr(prompt_object, "ensure_all_keys", False),
                            title_mapping=prompt_title_mapping,
                        )
                    )
                case "markdown":
                    prompt_text_list.extend(
                        [
                            f"[{ prompt_title_mapping.get('output_requirement', 'OUTPUT REQUIREMENT') }]:",
                            "Data Format: markdown text",
                        ]
                    )
                case "text":
                    pass

        prompt_text_list.append(
            f"[{ prompt_title_mapping.get('output', 'OUTPUT') }]:",
        )
        return prompt_text_list

    def _generate_yaml_prompt_message(
        self,
        role: str,
        prompt_part: Any,
        *,
        role_mapping: dict[str, str],
    ) -> dict[str, str]:
        role = str(role_mapping[role]) if role in role_mapping else role
        title_mapping = cast(dict[str, str], self.settings.get("prompt.prompt_title_mapping", {}))
        if not isinstance(title_mapping, dict):
            title_mapping = {}
        sanitized_part = DataFormatter.sanitize(
            self._replace_slot_references(prompt_part, title_mapping)
        )
        return {
            "role": role,
            "content": (
                str(sanitized_part)
                if isinstance(sanitized_part, (str, int, float, bool)) or sanitized_part is None
                else yaml.safe_dump(sanitized_part, allow_unicode=True)
            ),
        }

    def to_prompt_object(self) -> PromptModel:
        prompt_data = dict(self.prompt)
        if "output" in prompt_data and "output_format" not in prompt_data:
            prompt_data["output_format"] = self.settings.get("prompt.default_output_format", "json")
        if self._is_pydantic_model_type(prompt_data.get("output")):
            prompt_data["output"] = pydantic_model_to_output_schema(prompt_data["output"])
        prompt_object = PromptModel(**prompt_data)
        return prompt_object

    def to_text(
        self,
        *,
        role_mapping: dict[str, str] | None = None,
    ) -> str:
        prompt_object = self.to_prompt_object()
        self._check_prompt_all_empty(prompt_object)

        prompt_text_list = []

        merged_role_mapping = cast(dict[str, str], self.settings.get("prompt.role_mapping", {}))
        prompt_title_mapping = cast(dict[str, str], self.settings.get("prompt.prompt_title_mapping", {}))
        if not isinstance(merged_role_mapping, dict):
            merged_role_mapping = {}

        if isinstance(role_mapping, dict):
            merged_role_mapping.update(role_mapping)

        prompt_text_list.append(f"{ (merged_role_mapping['user'] if 'user' in merged_role_mapping else 'user') }:")
        if self.settings.get("prompt.add_current_time") is True:
            prompt_text_list.append(f"[current time]: { TimeInfo.get_current_time() }")
            prompt_text_list.append("")

        # system & developer
        if prompt_object.system:
            prompt_text_list.extend(
                self._generate_yaml_prompt_list(
                    str(
                        prompt_title_mapping.get(
                            'system',
                            'SYSTEM',
                        )
                    ),
                    prompt_object.system,
                )
            )

        if prompt_object.developer:
            prompt_text_list.extend(
                self._generate_yaml_prompt_list(
                    str(
                        prompt_title_mapping.get(
                            'developer',
                            'DEVELOPER DIRECTIONS',
                        )
                    ),
                    prompt_object.developer,
                )
            )

        # chat_history
        if prompt_object.chat_history:
            chat_history_lines = [f"[{ prompt_title_mapping.get('chat_history', 'CHAT HISTORY') }]:"]
            content_adapter = TypeAdapter(ChatMessageContent)
            for message in prompt_object.chat_history:
                role = (
                    merged_role_mapping[message.role]
                    if message.role in merged_role_mapping
                    else (merged_role_mapping["_"] if "_" in merged_role_mapping else message.role)
                )
                content = message.content
                if isinstance(content, dict) and "type" in content:
                    content = [content]
                if isinstance(content, list):
                    content = [content_adapter.validate_python(message_content) for message_content in content]
                    for one_content in content:
                        if one_content.type == "text":
                            chat_history_lines.append(
                                f"[{ role }]:{ self._replace_slot_references(str(one_content.text), prompt_title_mapping) }"
                            )
                        else:
                            warnings.warn(
                                f"Skipped content: unable to convert type '{one_content.type}' to text. "
                                f"Content: {one_content.model_dump()}",
                                stacklevel=2,
                            )
                else:
                    chat_history_lines.append(
                        f"[{ role }]:{ DataFormatter.sanitize(self._replace_slot_references(content, prompt_title_mapping)) }"
                    )
            prompt_text_list.extend(chat_history_lines)
            prompt_text_list.append("")

        prompt_text_list.extend(self._generate_main_prompt(prompt_object))
        prompt_text_list.append(
            f"{ (merged_role_mapping['assistant'] if 'assistant' in merged_role_mapping else 'assistant') }:"
        )

        return "\n".join(prompt_text_list)

    def to_messages(
        self,
        *,
        role_mapping: dict[str, str] | None = None,
        rich_content: bool | None = False,
        strict_role_orders: bool | None = True,
    ) -> list[dict[str, Any]]:
        prompt_object = self.to_prompt_object()
        self._check_prompt_all_empty(prompt_object)

        prompt_messages = []

        merged_role_mapping = cast(dict[str, str], self.settings.get("prompt.role_mapping", {}))
        prompt_title_mapping = cast(dict[str, str], self.settings.get("prompt.prompt_title_mapping", {}))

        if not isinstance(merged_role_mapping, dict):
            merged_role_mapping = {}

        if isinstance(role_mapping, dict):
            merged_role_mapping.update(role_mapping)

        add_current_time = self.settings.get("prompt.add_current_time") is True
        current_time_prefix = f"[current time]: { TimeInfo.get_current_time() }\n\n" if add_current_time else ""

        def _prepend_current_time_text(text: str) -> str:
            return f"{ current_time_prefix }{ text }" if add_current_time else text

        def _prepend_current_time_rich(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if not add_current_time:
                return content
            for item in content:
                if item.get("type") == "text":
                    item["text"] = _prepend_current_time_text(str(item.get("text", "")))
                    return content
            return [{"type": "text", "text": current_time_prefix}] + content

        # system & developer
        if prompt_object.system:
            prompt_messages.append(
                self._generate_yaml_prompt_message(
                    "system",
                    prompt_object.system,
                    role_mapping=merged_role_mapping,
                )
            )

        if prompt_object.developer:
            prompt_messages.append(
                self._generate_yaml_prompt_message(
                    "developer",
                    prompt_object.developer,
                    role_mapping=merged_role_mapping,
                )
            )

        # chat_history
        if prompt_object.chat_history:
            chat_history = []
            content_adapter = TypeAdapter(ChatMessageContent)
            last_role = None
            for message in prompt_object.chat_history:
                role = (
                    merged_role_mapping[message.role]
                    if message.role in merged_role_mapping
                    else (merged_role_mapping["_"] if "_" in merged_role_mapping else message.role)
                )
                origin_content = message.content
                content = None
                if isinstance(origin_content, dict) and "type" in origin_content:
                    origin_content = [origin_content]
                elif not isinstance(origin_content, list):
                    origin_content = [{"type": "text", "text": str(origin_content)}]
                content = [
                    content_adapter.validate_python(message_content).model_dump() for message_content in origin_content
                ]
                # strict role orders
                if strict_role_orders:
                    if role == last_role:
                        chat_history[-1]["content"].extend(content)
                    else:
                        chat_history.append({"role": role, "content": content})
                else:
                    chat_history.append({"role": role, "content": content})
                # update last_role
                last_role = role
            # check first and last role in chat history
            if strict_role_orders:
                if chat_history[0]["role"] != "user":
                    chat_history.insert(
                        0,
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"[{ prompt_title_mapping.get('chat_history', 'CHAT HISTORY') }]",
                                }
                            ],
                        },
                    )
                if chat_history[-1]["role"] != "assistant":
                    chat_history.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "[User continue input]"}],
                        }
                    )

            # simplify rich content
            if rich_content is False:
                simplified_chat_history = []
                for message in chat_history:
                    origin_content = message["content"]
                    content = []
                    if isinstance(origin_content, str):
                        content.append(origin_content)
                    elif isinstance(origin_content, Sequence):
                        for one_content in origin_content:
                            if "type" in one_content and one_content["type"] == "text":
                                content.append(one_content["text"])
                            elif "type" in one_content:
                                warnings.warn(
                                    f"Skipped content: unable to convert type '{ one_content['type'] }' to chat message when `rich_content` == False. "
                                    f"Content: {one_content}",
                                    stacklevel=2,
                                )

                    content = "\n\n".join(content)
                    simplified_chat_history.append(
                        {
                            "role": message["role"],
                            "content": content,
                        }
                    )
                chat_history = simplified_chat_history.copy()

            prompt_messages.extend(chat_history)

        # special occasion: only input
        if (
            prompt_object.input
            and not prompt_object.tools
            and not prompt_object.action_results
            and not prompt_object.info
            and not prompt_object.instruct
            and not prompt_object.output
            and not prompt_object.model_extra
            and not prompt_object.attachment
        ):
            role = merged_role_mapping["user"] if "user" in merged_role_mapping else "user"
            prompt_messages.append(
                {
                    "role": role,
                    "content": _prepend_current_time_text(
                        DataFormatter.sanitize(
                            self._replace_slot_references(prompt_object.input, prompt_title_mapping)
                        )
                    ),
                }
            )
        # special occasion: only attachment
        elif (
            prompt_object.attachment
            and not prompt_object.input
            and not prompt_object.tools
            and not prompt_object.action_results
            and not prompt_object.info
            and not prompt_object.instruct
            and not prompt_object.output
            and not prompt_object.model_extra
        ):
            role = merged_role_mapping["user"] if "user" in merged_role_mapping else "user"
            if rich_content:
                attachment_content = [content.model_dump() for content in prompt_object.attachment]
                prompt_messages.append({"role": role, "content": _prepend_current_time_rich(attachment_content)})
            else:
                for one_content in prompt_object.attachment:
                    if one_content.type == "text" and isinstance(one_content, TextMessageContent):
                        prompt_messages.append(
                            {
                                "role": role,
                                "content": _prepend_current_time_text(
                                    self._replace_slot_references(one_content.text, prompt_title_mapping)
                                ),
                            }
                        )
                    else:
                        warnings.warn(
                            f"Skipped content: unable to put attachment content into prompt messages when `rich_content` == False\n"
                            f"Content: {str(one_content.model_dump())}",
                            stacklevel=2,
                        )
        else:
            role = merged_role_mapping["user"] if "user" in merged_role_mapping else "user"
            # attachment message
            if rich_content:
                user_message_content = []
                # main prompt content (info, instruct, input, output)
                user_message_content.append(
                    {
                        "type": "text",
                        "text": _prepend_current_time_text("\n".join(self._generate_main_prompt(prompt_object))),
                    }
                )
                # extend attachment content
                if prompt_object.attachment:
                    user_message_content.extend([content.model_dump() for content in prompt_object.attachment])
                prompt_messages.append(
                    {
                        "role": role,
                        "content": user_message_content,
                    }
                )
            # simple message
            else:
                # main prompt content (info, instruct, input, output)
                user_message_content = self._generate_main_prompt(prompt_object)
                # extend attachment content
                if prompt_object.attachment:
                    for one_content in prompt_object.attachment:
                        if one_content.type == "text" and isinstance(one_content, TextMessageContent):
                            prompt_messages.append(
                                {
                                    "role": role,
                                    "content": self._replace_slot_references(
                                        one_content.text,
                                        prompt_title_mapping,
                                    ),
                                }
                            )
                        else:
                            warnings.warn(
                                f"Skipped content: unable to put attachment content into prompt messages when `rich_content` == False\n"
                                f"Content: {str(one_content.model_dump())}",
                                stacklevel=2,
                            )
                prompt_messages.append(
                    {"role": role, "content": _prepend_current_time_text("\n".join(user_message_content))}
                )

        return prompt_messages

    def _generate_output_model(
        self,
        name: str,
        schema: Mapping[str, Any] | Sequence[Any],
        *,
        strict_output: bool = False,
    ) -> Any:
        fields = {}
        validators = {}

        def cast_enum_value(value: Any, enum_type: type[Enum]):
            if value is None or isinstance(value, enum_type):
                return value
            if isinstance(value, str):
                enum_member = enum_type.__members__.get(value)
                if enum_member is not None:
                    return enum_member
            return enum_type(value)

        def make_enum_validator(enum_type: type[Enum]):
            def validate_enum(value: Any, _: ValidationInfo):
                return cast_enum_value(value, enum_type)

            return validate_enum

        def ensure_list_and_cast(v: Any, target_type: Any):
            if not isinstance(v, list):
                v = [v]
            casted = []
            enum_type = self._get_enum_type(target_type)
            for item in v:
                if enum_type is not None:
                    casted.append(cast_enum_value(item, enum_type))
                    continue
                if target_type is Any or not isinstance(target_type, type):
                    casted.append(item)
                    continue
                target_type = cast(type, target_type)
                if isinstance(item, target_type):
                    casted.append(item)
                    continue
                if isinstance(item, Mapping):
                    model_validate = getattr(target_type, "model_validate", None)
                    if callable(model_validate):
                        casted.append(model_validate(item))
                        continue
                    parse_obj = getattr(target_type, "parse_obj", None)
                    if callable(parse_obj):
                        casted.append(parse_obj(item))
                        continue
                casted.append(target_type(item))
            return casted

        def field_constraint_kwargs(
            field_schema: Any,
        ) -> dict[str, Any]:
            if not isinstance(field_schema, tuple) or len(field_schema) < 4:
                return {}
            metadata = field_schema[3]
            if not isinstance(metadata, Mapping):
                return {}
            contract = metadata.get(PYDANTIC_CONTRACT_META_KEY)
            if not isinstance(contract, Mapping):
                return {}
            constraints = contract.get("constraints")
            if not isinstance(constraints, Mapping):
                return {}
            field_key_map = {
                "minLength": "min_length",
                "maxLength": "max_length",
                "minItems": "min_length",
                "maxItems": "max_length",
                "minimum": "ge",
                "maximum": "le",
                "exclusiveMinimum": "gt",
                "exclusiveMaximum": "lt",
                "multipleOf": "multiple_of",
                "pattern": "pattern",
            }
            return {
                field_key_map[key]: value
                for key, value in constraints.items()
                if key in field_key_map
            }

        if isinstance(schema, Mapping):
            for field_name, field_type_schema in schema.items():
                field_type = Any
                field_desc = None
                default_value = None
                field_required = False
                field_constraints = field_constraint_kwargs(
                    field_type_schema
                )
                if isinstance(field_type_schema, str):
                    field_desc = field_type_schema
                    field_required = strict_output
                elif isinstance(field_type_schema, Mapping):
                    field_type = self._generate_output_model(
                        f"{ name }_{ field_name.capitalize() }",
                        field_type_schema,
                        strict_output=strict_output,
                    )
                    field_required = strict_output
                elif isinstance(field_type_schema, tuple):
                    value_type = field_type_schema[0] if len(field_type_schema) > 0 else Any
                    desc = field_type_schema[1] if len(field_type_schema) > 1 else ""
                    third_value = field_type_schema[2] if len(field_type_schema) > 2 else None
                    ensure_marker = self._is_ensure_marker(third_value)
                    field_required = strict_output or ensure_marker
                    if field_required:
                        default_value = ...
                    if isinstance(value_type, Mapping):
                        field_type = self._generate_output_model(
                            f"{ name }_{ field_name.capitalize() }",
                            value_type,
                            strict_output=strict_output,
                        )
                        field_desc = desc
                    elif (
                        isinstance(value_type, Sequence)
                        and not isinstance(value_type, (str, bytes))
                    ):
                        field_type = self._generate_output_model(
                            f"{ name }_{ field_name.capitalize() }",
                            list(value_type),
                            strict_output=strict_output,
                        )
                        field_desc = desc
                    elif isinstance(value_type, type) or get_origin(value_type) is not None:
                        field_type = value_type
                        field_desc = desc
                    else:
                        field_desc = f"type: { value_type }; desc: { desc }"
                elif isinstance(field_type_schema, Sequence):
                    field_type = self._generate_output_model(
                        f"{ name }_{ field_name.capitalize() }",
                        list(field_type_schema),
                        strict_output=strict_output,
                    )
                    field_required = strict_output
                elif isinstance(field_type_schema, type) or get_origin(field_type_schema) is not None:
                    field_type = field_type_schema | None
                    field_required = strict_output
                else:
                    field_desc = str(field_type_schema)
                    field_required = strict_output
                if field_required and field_type is not Any:
                    field_annotation = cast(Any, field_type) | None
                else:
                    field_annotation = field_type
                if get_origin(field_type) in (list, List):
                    elem_type = cast(
                        type,
                        get_args(field_type)[0] if get_args(field_type) else Any,
                    )

                    fields.update(
                        {
                            field_name: (
                                Annotated[
                                    field_annotation,
                                    PlainValidator(lambda value: ensure_list_and_cast(value, elem_type)),
                                    PlainSerializer(lambda value: value, return_type=Any),
                                ],
                                Field(
                                    default_value,
                                    description=field_desc,
                                    **field_constraints,
                                ),
                            )
                        }
                    )
                elif self._get_enum_type(field_type) is not None:
                    enum_type = cast(type[Enum], self._get_enum_type(field_type))
                    fields.update(
                        {
                            field_name: (
                                Annotated[
                                    field_annotation,
                                    PlainValidator(make_enum_validator(enum_type)),
                                ],
                                Field(
                                    default_value,
                                    description=field_desc,
                                    **field_constraints,
                                ),
                            )
                        }
                    )
                else:
                    fields.update(
                        {
                            field_name: (
                                field_annotation,
                                Field(
                                    default_value,
                                    description=field_desc,
                                    **field_constraints,
                                ),
                            )
                        }
                    )

            return create_model(
                name,
                __config__={'extra': 'forbid' if strict_output else 'allow'},
                **fields,
                **validators,
            )
        else:
            item_type = Any
            if len(schema) > 0:
                origin_item = schema[0]
                if isinstance(origin_item, str):
                    item_type = Any
                elif isinstance(origin_item, Mapping):
                    item_type = self._generate_output_model(
                        f"{ name }_List",
                        origin_item,
                        strict_output=strict_output,
                    )
                elif isinstance(origin_item, tuple):
                    value_type = origin_item[0] if len(origin_item) > 0 else Any
                    desc = origin_item[1] if len(origin_item) > 1 else ""
                    if isinstance(value_type, type) or get_origin(value_type) is not None:
                        item_type = value_type
                    else:
                        item_type = Any
                elif isinstance(origin_item, Sequence):
                    item_type = self._generate_output_model(
                        f"{ name }_List",
                        list(origin_item),
                        strict_output=strict_output,
                    )
                elif isinstance(origin_item, type) or get_origin(origin_item) is not None:
                    item_type = origin_item
                else:
                    item_type = Any
            else:
                item_type = Any

            return Annotated[
                list[item_type | None] | None,
                PlainValidator(lambda v: ensure_list_and_cast(v, cast(type, item_type))),
                PlainSerializer(lambda value: value, return_type=Any),
            ]

    def to_output_model(self, *args, strict_output: bool | None = None, **kwargs) -> type["BaseModel"]:
        declared_output = self.prompt.get("output")
        if self._is_pydantic_model_type(declared_output):
            return cast(type["BaseModel"], declared_output)

        prompt_object = self.to_prompt_object()
        output_prompt = prompt_object.output

        if not isinstance(output_prompt, (Mapping, Sequence)) or isinstance(output_prompt, str):
            raise TypeError("Unable to generator output model because the output is not a structure data.")

        if strict_output is None:
            strict_output = bool(getattr(prompt_object, "ensure_all_keys", False))

        if isinstance(output_prompt, Mapping):
            return self._generate_output_model(
                "AgentlyOutput",
                DataFormatter.sanitize(output_prompt, remain_type=True),
                strict_output=strict_output,
            )
        else:
            return create_model(
                "AgentlyOutput",
                __base__=self._RootListOutputModelMixin,
                list=(
                    self._generate_output_model(
                        "AgentlyOutput_List",
                        DataFormatter.sanitize(output_prompt, remain_type=True),
                        strict_output=strict_output,
                    ),
                    ... if strict_output else None,
                ),
                __config__={'extra': 'forbid' if strict_output else 'allow'},
            )

    def _to_serializable_output_prompt(self, output_prompt_part: Any):
        if not isinstance(output_prompt_part, (Mapping, Sequence)) or isinstance(output_prompt_part, str):
            return output_prompt_part

        if isinstance(output_prompt_part, Mapping):
            result = {}
            for key, value in output_prompt_part.items():
                result[key] = self._to_serializable_output_prompt(value)
            return result
        else:
            if isinstance(output_prompt_part, tuple):
                match len(output_prompt_part):
                    case 0:
                        return []
                    case 1:
                        return {
                            "$type": output_prompt_part[0],
                        }
                    case 2:
                        result = {
                            "$type": output_prompt_part[0],
                        }
                        if output_prompt_part[1]:
                            result["$desc"] = output_prompt_part[1]
                        return result
                    case _:
                        result = {
                            "$type": output_prompt_part[0],
                        }
                        if output_prompt_part[1]:
                            result["$desc"] = output_prompt_part[1]
                        third_value = output_prompt_part[2]
                        ensure_marker = DataPathBuilder.normalize_ensure_marker(third_value)
                        if ensure_marker is not None:
                            result["$ensure"] = ensure_marker
                        return result
            else:
                return list(output_prompt_part)

    def to_serializable_prompt_data(self, inherit: bool = False) -> "SerializableMapping":
        prompt_data = self.prompt.get(
            default={},
            inherit=inherit,
        )
        if "output" in prompt_data:
            prompt_data["output"] = self._to_serializable_output_prompt(prompt_data["output"])
        return DataFormatter.sanitize(prompt_data)

    def to_json_prompt(self, inherit: bool = False):
        return json.dumps(
            self.to_serializable_prompt_data(inherit),
            indent=2,
            ensure_ascii=False,
        )

    def to_yaml_prompt(self, inherit: bool = False):
        return yaml.safe_dump(
            self.to_serializable_prompt_data(inherit),
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )
