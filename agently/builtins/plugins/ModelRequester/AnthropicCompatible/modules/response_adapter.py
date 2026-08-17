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


class AnthropicCompatibleResponseAdapterMixin:

    @staticmethod
    def _create_tool_call_state(call_id: str, index: int) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "index": index,
            "name": "",
            "arguments": "",
            "name_emitted": False,
            "any_argument_delta_emitted": False,
        }

    @staticmethod
    def _emit_tool_call_chunk(
        tool_state: dict[str, Any],
        *,
        arguments_delta: str | None = None,
        name_only: bool = False,
    ) -> dict[str, Any]:
        function_payload: dict[str, Any] = {}
        if not tool_state.get("name_emitted") and tool_state.get("name"):
            function_payload["name"] = str(tool_state["name"])
            tool_state["name_emitted"] = True
        if not name_only and arguments_delta is not None:
            function_payload["arguments"] = arguments_delta
            tool_state["any_argument_delta_emitted"] = True
        return {
            "index": int(tool_state.get("index", 0)),
            "id": str(tool_state.get("call_id", "")),
            "type": "function",
            "function": function_payload,
        }

    @staticmethod
    def _extract_text_from_content_blocks(content_blocks: Any) -> str:
        if not isinstance(content_blocks, list):
            return ""
        texts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        return "".join(texts)

    @staticmethod
    def _extract_reasoning_from_content_blocks(content_blocks: Any) -> str:
        if not isinstance(content_blocks, list):
            return ""
        reasoning: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                reasoning.append(str(block.get("thinking", "")))
        return "".join(reasoning)

    @staticmethod
    def _map_finish_reason(stop_reason: Any) -> str:
        if stop_reason == "tool_use":
            return "tool_calls"
        if stop_reason == "max_tokens":
            return "length"
        return "stop"

    async def broadcast_response(self, response_generator: AsyncGenerator) -> "AgentlyResultGenerator":
        meta: dict[str, Any] = {}
        content_buffer = ""
        reasoning_buffer = ""
        message_record: dict[str, Any] = {}
        content_blocks: dict[int, dict[str, Any]] = {}
        tool_call_states: dict[str, dict[str, Any]] = {}
        completed = False
        saw_any_event = False

        def get_tool_state(tool_use_id: str, index: int):
            return tool_call_states.setdefault(
                tool_use_id,
                self._create_tool_call_state(tool_use_id, index),
            )

        async for event, message in response_generator:
            if event == "error":
                yield "error", message
                continue
            if event == "status":
                if isinstance(message, dict) and message.get("status") == "failed" and message.get("retry") is True:
                    meta = {}
                    content_buffer = ""
                    reasoning_buffer = ""
                    message_record = {}
                    content_blocks = {}
                    tool_call_states = {}
                    completed = False
                    saw_any_event = False
                yield "status", message
                continue

            saw_any_event = True
            if not isinstance(message, str):
                message = json.dumps(message, ensure_ascii=False)
            yield "original_delta", message
            loaded_message = json.loads(message)
            payload_type = str(loaded_message.get("type", event))

            if payload_type == "message":
                if isinstance(loaded_message, dict):
                    message_record.update(loaded_message)
                completed = True
                continue

            if payload_type == "message_start":
                message_payload = loaded_message.get("message", loaded_message)
                if isinstance(message_payload, dict):
                    message_record.update(message_payload)
                    if "id" in message_payload:
                        meta["id"] = message_payload["id"]
                    if "model" in message_payload:
                        meta["model"] = message_payload["model"]
                    if "role" in message_payload:
                        meta["role"] = message_payload["role"]
                    if "usage" in message_payload:
                        meta["usage"] = message_payload["usage"]
                continue

            if payload_type == "content_block_start":
                block = loaded_message.get("content_block")
                index = loaded_message.get("index", 0)
                if not isinstance(block, dict) or not isinstance(index, int):
                    continue
                content_blocks[index] = dict(block)
                if block.get("type") == "tool_use":
                    tool_use_id = str(block.get("id", ""))
                    if tool_use_id:
                        tool_state = get_tool_state(tool_use_id, index)
                        tool_state["name"] = str(block.get("name", ""))
                        tool_state["arguments"] = ""
                continue

            if payload_type == "content_block_delta":
                index = loaded_message.get("index", 0)
                delta = loaded_message.get("delta", {})
                if not isinstance(index, int) or not isinstance(delta, dict):
                    continue
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = str(delta.get("text", ""))
                    content_buffer += text
                    block = content_blocks.setdefault(index, {"type": "text", "text": ""})
                    block["text"] = str(block.get("text", "")) + text
                    yield "delta", text
                    continue
                if delta_type == "thinking_delta":
                    thinking = str(delta.get("thinking", ""))
                    reasoning_buffer += thinking
                    block = content_blocks.setdefault(index, {"type": "thinking", "thinking": ""})
                    block["thinking"] = str(block.get("thinking", "")) + thinking
                    yield "reasoning_delta", thinking
                    continue
                if delta_type == "signature_delta":
                    block = content_blocks.setdefault(index, {"type": "thinking"})
                    block["signature"] = str(delta.get("signature", ""))
                    continue
                if delta_type == "input_json_delta":
                    block = content_blocks.setdefault(index, {"type": "tool_use", "input": {}})
                    partial_json = str(delta.get("partial_json", ""))
                    accumulated = str(block.get("_partial_json", "")) + partial_json
                    block["_partial_json"] = accumulated
                    tool_use_id = str(block.get("id", ""))
                    if tool_use_id:
                        tool_state = get_tool_state(tool_use_id, index)
                        tool_state["name"] = str(block.get("name", tool_state.get("name", "")))
                        tool_state["arguments"] = str(tool_state.get("arguments", "")) + partial_json
                        yield "tool_calls", self._emit_tool_call_chunk(tool_state, arguments_delta=partial_json)
                    continue
                continue

            if payload_type == "content_block_stop":
                index = loaded_message.get("index", 0)
                if not isinstance(index, int):
                    continue
                block = content_blocks.get(index)
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    partial_json = block.pop("_partial_json", "")
                    if isinstance(partial_json, str) and partial_json.strip():
                        try:
                            block["input"] = json.loads(partial_json)
                        except json.JSONDecodeError:
                            block["input"] = {"raw_arguments": partial_json}
                continue

            if payload_type == "message_delta":
                delta = loaded_message.get("delta", {})
                if isinstance(delta, dict):
                    if "stop_reason" in delta:
                        message_record["stop_reason"] = delta["stop_reason"]
                    if "stop_sequence" in delta:
                        message_record["stop_sequence"] = delta["stop_sequence"]
                if isinstance(loaded_message.get("usage"), dict):
                    message_record["usage"] = loaded_message["usage"]
                continue

            if payload_type == "message_stop":
                completed = True
                continue

        if not saw_any_event:
            return

        if len(content_blocks) > 0:
            message_record["content"] = [content_blocks[index] for index in sorted(content_blocks.keys())]

        if not completed and "content" not in message_record:
            message_record["content"] = []

        for index, block in enumerate(cast(list[dict[str, Any]], message_record.get("content", []))):
            if block.get("type") != "tool_use":
                continue
            tool_use_id = str(block.get("id", ""))
            if not tool_use_id:
                continue
            tool_state = get_tool_state(tool_use_id, index)
            tool_state["name"] = str(block.get("name", tool_state.get("name", "")))
            arguments = block.get("input", {})
            if isinstance(arguments, dict):
                arguments_text = json.dumps(arguments, ensure_ascii=False)
            else:
                arguments_text = str(arguments)
            if not tool_state.get("any_argument_delta_emitted"):
                yield "tool_calls", self._emit_tool_call_chunk(tool_state, arguments_delta=arguments_text)
            elif not tool_state.get("name_emitted") and tool_state.get("name"):
                yield "tool_calls", self._emit_tool_call_chunk(tool_state, arguments_delta=None, name_only=True)

        done_content = self._extract_text_from_content_blocks(message_record.get("content", []))
        if done_content == "":
            done_content = content_buffer
        reasoning_done = self._extract_reasoning_from_content_blocks(message_record.get("content", []))
        if reasoning_done == "":
            reasoning_done = reasoning_buffer

        yield "done", done_content
        yield "reasoning_done", reasoning_done
        yield "original_done", message_record

        if "id" in message_record:
            meta["id"] = message_record["id"]
        if "model" in message_record:
            meta["model"] = message_record["model"]
        if "role" in message_record:
            meta["role"] = message_record["role"]
        if "usage" in message_record:
            meta["usage"] = message_record["usage"]
        meta["finish_reason"] = self._map_finish_reason(message_record.get("stop_reason"))
        yield "meta", meta
