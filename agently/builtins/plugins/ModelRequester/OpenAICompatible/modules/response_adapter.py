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
from typing import Any, AsyncGenerator, cast

from agently.types.data import AgentlyResultGenerator
from agently.utils import DataFormatter, DataLocator

from .types import ContentMapping


class OpenAICompatibleResponseAdapterMixin:
    model_type: str
    plugin_settings: Any

    async def broadcast_response(self, response_generator: AsyncGenerator) -> "AgentlyResultGenerator":
        meta = {}
        message_record = {}
        reasoning_buffer = ""
        content_buffer = ""

        content_mapping = cast(
            ContentMapping,
            DataFormatter.to_str_key_dict(
                self.plugin_settings.get("content_mapping"),
                value_format="serializable",
            ),
        )
        id_mapping = content_mapping["id"]
        role_mapping = content_mapping["role"]
        reasoning_mapping = content_mapping["reasoning"]
        delta_mapping = content_mapping["delta"]
        tool_calls_mapping = content_mapping["tool_calls"]
        done_mapping = content_mapping["done"]
        usage_mapping = content_mapping["usage"]
        finish_reason_mapping = content_mapping["finish_reason"]
        extra_delta_mapping = content_mapping["extra_delta"]
        extra_done_mapping = content_mapping["extra_done"]
        yield_extra_content_separately = self.plugin_settings.get("yield_extra_content_separately", True)

        content_mapping_style = str(self.plugin_settings.get("content_mapping_style"))
        if content_mapping_style not in ("dot", "slash"):
            content_mapping_style = "dot"

        async for event, message in response_generator:
            if event == "error":
                yield "error", message
            elif event == "status":
                if isinstance(message, dict) and message.get("status") == "failed" and message.get("retry") is True:
                    meta = {}
                    message_record = {}
                    reasoning_buffer = ""
                    content_buffer = ""
                yield "status", message
            elif message != "[DONE]":
                yield "original_delta", message
                loaded_message = json.loads(message)
                message_record = loaded_message.copy()
                if "id" not in meta and id_mapping:
                    _id = DataLocator.locate_path_in_dict(
                        loaded_message,
                        id_mapping,
                        style=content_mapping_style,
                    )
                    if _id:
                        meta.update({"id": _id})
                if "role" not in meta and role_mapping:
                    role = DataLocator.locate_path_in_dict(
                        loaded_message,
                        role_mapping,
                        style=content_mapping_style,
                        default="assistant",
                    )
                    if role:
                        meta.update({"role": role})
                if reasoning_mapping:
                    reasoning = DataLocator.locate_path_in_dict(
                        loaded_message,
                        reasoning_mapping,
                        style=content_mapping_style,
                    )
                    if reasoning:
                        reasoning_buffer += str(reasoning)
                        yield "reasoning_delta", reasoning
                if delta_mapping:
                    delta = DataLocator.locate_path_in_dict(
                        loaded_message,
                        delta_mapping,
                        style=content_mapping_style,
                    )
                    if delta:
                        content_buffer += str(delta)
                        yield "delta", delta
                if tool_calls_mapping:
                    tool_calls = DataLocator.locate_path_in_dict(
                        loaded_message,
                        tool_calls_mapping,
                        style=content_mapping_style,
                    )
                    if tool_calls:
                        yield "tool_calls", tool_calls
                if extra_delta_mapping:
                    for extra_key, extra_path in extra_delta_mapping.items():
                        extra_value = DataLocator.locate_path_in_dict(
                            loaded_message,
                            extra_path,
                            style=content_mapping_style,
                        )
                        if extra_value:
                            yield "extra", {extra_key: extra_value}
                            if yield_extra_content_separately:
                                yield extra_key, extra_value  # type: ignore
            else:
                done_content = None
                if self.model_type == "embeddings" and done_mapping is None:
                    done_mapping = "data"
                    content_mapping_style = "dot"
                if done_mapping:
                    done_content = DataLocator.locate_path_in_dict(
                        message_record,
                        done_mapping,
                        style=content_mapping_style,
                    )
                if done_content is None and self.model_type in ("chat", "completions"):
                    done_content = DataLocator.locate_path_in_dict(
                        message_record,
                        "choices[0].message.content",
                        style="dot",
                    )
                if done_content is None and self.model_type == "completions":
                    done_content = DataLocator.locate_path_in_dict(
                        message_record,
                        "choices[0].text",
                        style="dot",
                    )
                if done_content:
                    yield "done", done_content
                else:
                    yield "done", content_buffer
                reasoning_content = None
                if reasoning_mapping:
                    reasoning_content = DataLocator.locate_path_in_dict(
                        message_record,
                        reasoning_mapping,
                        style=content_mapping_style,
                    )
                if reasoning_content is None and self.model_type == "chat":
                    reasoning_content = DataLocator.locate_path_in_dict(
                        message_record,
                        "choices[0].message.reasoning_content",
                        style="dot",
                    )
                if reasoning_content:
                    yield "reasoning_done", reasoning_content
                else:
                    yield "reasoning_done", reasoning_buffer
                match self.model_type:
                    case "embeddings":
                        yield "original_done", message_record
                    case _:
                        done_message = message_record
                        assistant_message = {
                            "role": meta["role"] if "role" in meta else "assistant",
                            "content": done_content if done_content else content_buffer,
                        }
                        if reasoning_content or reasoning_buffer:
                            assistant_message["reasoning_content"] = (
                                reasoning_content if reasoning_content else reasoning_buffer
                            )
                        # Some OpenAI-compatible gateways send a usage-only final chunk with an
                        # empty or missing "choices" array (e.g. YuDing, MiMo). Guard against it so
                        # the accumulated content is preserved instead of raising IndexError/KeyError.
                        choices = done_message.get("choices")
                        if isinstance(choices, list) and len(choices) > 0:
                            if "message" not in choices[0]:
                                choices[0].update({"message": {}})
                            choices[0]["message"].update(assistant_message)
                        else:
                            done_message["choices"] = [{"message": assistant_message}]
                        yield "original_done", done_message
                if finish_reason_mapping:
                    meta.update(
                        {
                            "finish_reason": DataLocator.locate_path_in_dict(
                                message_record,
                                finish_reason_mapping,
                                style=content_mapping_style,
                            )
                        }
                    )
                if usage_mapping:
                    meta.update(
                        {
                            "usage": DataLocator.locate_path_in_dict(
                                message_record,
                                usage_mapping,
                                style=content_mapping_style,
                            )
                        }
                    )
                yield "meta", meta
                if extra_done_mapping:
                    for extra_key, extra_path in extra_done_mapping.items():
                        extra_value = DataLocator.locate_path_in_dict(
                            message_record,
                            extra_path,
                            style=content_mapping_style,
                        )
                        if extra_value:
                            yield "extra", {extra_key: extra_value}
