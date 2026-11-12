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

from typing import TYPE_CHECKING, Any

from agently.types.data import AgentlyRequestData, AgentlyRequestDataDict
from agently.utils import DataFormatter

from ...function_tools import FunctionTools

if TYPE_CHECKING:
    from httpx import Timeout


class OpenAIResponsesCompatibleRequestBuilderMixin(FunctionTools):
    name: str
    plugin_settings: Any
    prompt: Any

    if TYPE_CHECKING:
        def _get_http_timeout(self, *, disable_read: bool = False) -> "Timeout": ...

    @classmethod
    def _merge_tools(cls, auto_tools: list[dict[str, Any]], explicit_tools: Any) -> list[dict[str, Any]]:
        if not isinstance(explicit_tools, list) or len(explicit_tools) == 0:
            return auto_tools

        merged_named: dict[str, dict[str, Any]] = {}
        ordered: list[dict[str, Any]] = []
        for tool in auto_tools:
            tool_name = tool.get("name")
            if isinstance(tool_name, str) and tool_name:
                merged_named[tool_name] = tool
            else:
                ordered.append(tool)

        explicit_non_named: list[dict[str, Any]] = []
        for tool in explicit_tools:
            if not isinstance(tool, dict):
                continue
            tool_name = tool.get("name")
            if tool.get("type") == "function" and isinstance(tool_name, str) and tool_name:
                merged_named[tool_name] = tool
            else:
                explicit_non_named.append(tool)

        merged_tools = list(merged_named.values())
        merged_tools.extend(ordered)
        merged_tools.extend(explicit_non_named)
        return merged_tools

    @staticmethod
    def _message_text_to_content(text: Any) -> dict[str, Any]:
        return {"type": "input_text", "text": str(text)}

    @classmethod
    def _normalize_content_part(cls, part: Any) -> dict[str, Any]:
        if isinstance(part, str):
            return cls._message_text_to_content(part)
        if not isinstance(part, dict):
            return cls._message_text_to_content(part)

        part_type = part.get("type")
        if part_type == "text":
            return {"type": "input_text", "text": str(part.get("text", ""))}
        if part_type == "image_url":
            image_value = part.get("image_url")
            mapped: dict[str, Any] = {"type": "input_image"}
            if isinstance(image_value, dict):
                if "url" in image_value:
                    mapped["image_url"] = image_value["url"]
                if "detail" in image_value:
                    mapped["detail"] = image_value["detail"]
                if "file_id" in image_value:
                    mapped["file_id"] = image_value["file_id"]
            else:
                mapped["image_url"] = image_value
            return mapped
        if part_type in ("input_text", "input_image", "input_file"):
            mapped = dict(part)
            if mapped.get(str(part_type)) is None:
                del mapped[str(part_type)]
            return mapped
        raise TypeError(
            f"Plugin Name: { cls.name }\n"
            f"Error: Unsupported rich content type for Responses input: '{ part_type }'\n"
            f"Content: { part }"
        )

    def _build_input_items(self) -> list[dict[str, Any]]:
        messages = self.prompt.to_messages(
            rich_content=True,
            strict_role_orders=bool(self.plugin_settings.get("strict_role_orders", True)),
        )
        input_items: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "user"))
            raw_content = message.get("content", "")
            if isinstance(raw_content, list):
                content = [self._normalize_content_part(part) for part in raw_content]
            else:
                content = [self._normalize_content_part(raw_content)]
            input_items.append({"type": "message", "role": role, "content": content})
        return input_items

    def generate_request_data(self) -> "AgentlyRequestData":
        agently_request_dict: AgentlyRequestDataDict = {
            "client_options": {},
            "headers": {},
            "data": {},
            "request_options": {},
            "request_url": "",
        }

        agently_request_dict["data"] = {
            "input": self._build_input_items(),
        }

        headers: dict[str, str] = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("headers"),
            value_format="str",
            default_value={},
        )
        headers.update({"Connection": "close"})
        agently_request_dict["headers"] = headers

        client_options = DataFormatter.to_str_key_dict(self.plugin_settings.get("client_options"), default_value={})
        client_options.setdefault("trust_env", False)
        proxy = self.plugin_settings.get("proxy", None)
        if proxy:
            client_options.update({"proxy": proxy})
        client_options.update({"timeout": self._get_http_timeout()})
        agently_request_dict["client_options"] = client_options

        legacy_options = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("options"),
            value_format="serializable",
            default_value={},
        )
        request_options = DataFormatter.to_str_key_dict(
            self.plugin_settings.get("request_options"),
            value_format="serializable",
            default_value={},
        )
        request_options = {**legacy_options, **request_options}
        request_options_in_prompt = self.prompt.get("options", {})
        if request_options_in_prompt:
            request_options.update(request_options_in_prompt)
            request_options = DataFormatter.to_str_key_dict(
                request_options,
                value_format="serializable",
                default_value={},
            )

        request_options.update(
            {
                "model": self.plugin_settings.get(
                    "model",
                    self.plugin_settings.get("default_model", "gpt-5.5"),
                )
            }
        )

        is_stream = self.plugin_settings.get("stream")
        request_options.update({"stream": True if is_stream is None else bool(is_stream)})

        auto_tools = [
            {"type": "function", **function, "strict": False}
            for function in self._prompt_tool_functions(self.prompt.to_prompt_object().tools)
        ]
        merged_tools = self._merge_tools(auto_tools, request_options.get("tools"))
        if len(merged_tools) > 0:
            request_options["tools"] = merged_tools

        agently_request_dict["request_options"] = request_options

        full_url = self.plugin_settings.get("full_url")
        base_url = str(self.plugin_settings.get("base_url"))
        base_url = base_url[:-1] if base_url[-1] == "/" else base_url
        agently_request_dict["request_url"] = str(full_url) if isinstance(full_url, str) else f"{ base_url }/responses"

        return AgentlyRequestData(**agently_request_dict)
