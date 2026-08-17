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
from typing import Any, AsyncGenerator

from agently.types.data import AgentlyResultGenerator


class OpenAIResponsesCompatibleResponseAdapterMixin:

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
    def _collect_output_items(completed_output_items: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        return [completed_output_items[index] for index in sorted(completed_output_items.keys())]

    @staticmethod
    def _collect_assistant_text(output_items: Any) -> str:
        if not isinstance(output_items, list):
            return ""
        texts: list[str] = []
        for item in output_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            if item.get("role") != "assistant":
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    texts.append(str(part.get("text", "")))
        return "".join(texts)

    @staticmethod
    def _extract_reasoning_summary(response_record: dict[str, Any]) -> str:
        output_items = response_record.get("output")
        if isinstance(output_items, list):
            output_texts: list[str] = []
            for output_item in output_items:
                if not isinstance(output_item, dict) or output_item.get("type") != "reasoning":
                    continue
                summary = output_item.get("summary")
                if not isinstance(summary, list):
                    continue
                for summary_part in summary:
                    if isinstance(summary_part, str):
                        output_texts.append(summary_part)
                    elif isinstance(summary_part, dict) and isinstance(summary_part.get("text"), str):
                        output_texts.append(str(summary_part["text"]))
            if output_texts:
                return "".join(output_texts)

        # Retain compatibility with gateways that put generated reasoning
        # directly on the response-level reasoning object.
        reasoning = response_record.get("reasoning")
        if not isinstance(reasoning, dict):
            return ""
        summary = reasoning.get("summary")
        if isinstance(summary, str):
            if summary.strip().lower() in {"auto", "concise", "detailed", "none"}:
                return ""
            return summary
        if isinstance(summary, list):
            texts: list[str] = []
            for item in summary:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        texts.append(str(item["text"]))
                    elif isinstance(item.get("summary_text"), str):
                        texts.append(str(item["summary_text"]))
            return "".join(texts)
        return ""

    @staticmethod
    def _has_function_call_output(output_items: Any) -> bool:
        if not isinstance(output_items, list):
            return False
        for item in output_items:
            if isinstance(item, dict) and item.get("type") == "function_call":
                return True
        return False

    @classmethod
    def _build_finish_reason(cls, response_record: dict[str, Any]) -> str:
        output_items = response_record.get("output", [])
        if cls._has_function_call_output(output_items):
            return "tool_calls"
        incomplete_details = response_record.get("incomplete_details")
        if isinstance(incomplete_details, dict) and incomplete_details.get("reason") == "max_output_tokens":
            return "length"
        return "stop"

    async def broadcast_response(self, response_generator: AsyncGenerator) -> "AgentlyResultGenerator":
        meta: dict[str, Any] = {}
        content_buffer = ""
        reasoning_buffer = ""
        response_record: dict[str, Any] = {}
        completed_output_items: dict[int, dict[str, Any]] = {}
        tool_call_states: dict[str, dict[str, Any]] = {}
        completed = False
        saw_any_event = False

        async for event, message in response_generator:
            if event == "error":
                yield "error", message
                continue
            if event == "status":
                if isinstance(message, dict) and message.get("status") == "failed" and message.get("retry") is True:
                    meta = {}
                    content_buffer = ""
                    reasoning_buffer = ""
                    response_record = {}
                    completed_output_items = {}
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

            if payload_type in {"response.created", "response.in_progress"}:
                base_response = loaded_message.get("response", loaded_message)
                if isinstance(base_response, dict):
                    response_record.update(base_response)
                    if "id" in base_response:
                        meta["id"] = base_response["id"]
                    if "model" in base_response:
                        meta["model"] = base_response["model"]
                    if "status" in base_response:
                        meta["status"] = base_response["status"]
                continue

            if payload_type == "response.output_text.delta":
                delta = str(loaded_message.get("delta", ""))
                content_buffer += delta
                yield "delta", delta
                continue

            if payload_type == "response.output_text.done":
                final_text = str(loaded_message.get("text", ""))
                if final_text and not content_buffer:
                    content_buffer = final_text
                continue

            if payload_type == "response.reasoning_summary_text.delta":
                delta = str(loaded_message.get("delta", ""))
                reasoning_buffer += delta
                yield "reasoning_delta", delta
                continue

            if payload_type == "response.reasoning_summary_text.done":
                final_reasoning = str(loaded_message.get("text", ""))
                if final_reasoning and not reasoning_buffer:
                    reasoning_buffer = final_reasoning
                continue

            if payload_type == "response.function_call_arguments.delta":
                call_id = str(loaded_message.get("call_id", ""))
                if call_id == "":
                    continue
                delta = str(loaded_message.get("delta", ""))
                output_index = loaded_message.get("output_index", len(tool_call_states))
                index = int(output_index) if isinstance(output_index, int) else len(tool_call_states)
                tool_state = tool_call_states.setdefault(call_id, self._create_tool_call_state(call_id, index))
                tool_state["arguments"] = str(tool_state.get("arguments", "")) + delta
                yield "tool_calls", self._emit_tool_call_chunk(tool_state, arguments_delta=delta)
                continue

            if payload_type == "response.function_call_arguments.done":
                call_id = str(loaded_message.get("call_id", ""))
                if call_id and call_id in tool_call_states and isinstance(loaded_message.get("arguments"), str):
                    tool_call_states[call_id]["arguments"] = loaded_message["arguments"]
                continue

            if payload_type in {"response.output_item.added", "response.output_item.done"}:
                item = loaded_message.get("item")
                output_index = loaded_message.get("output_index", 0)
                if not isinstance(item, dict) or not isinstance(output_index, int):
                    continue
                item_type = item.get("type")
                if payload_type == "response.output_item.done":
                    completed_output_items[output_index] = item

                if item_type == "function_call":
                    call_id = str(item.get("call_id", ""))
                    if call_id == "":
                        continue
                    tool_state = tool_call_states.setdefault(call_id, self._create_tool_call_state(call_id, output_index))
                    tool_state["index"] = output_index
                    if isinstance(item.get("name"), str):
                        tool_state["name"] = item["name"]
                    if isinstance(item.get("arguments"), str):
                        tool_state["arguments"] = item["arguments"]
                    if payload_type == "response.output_item.done":
                        if not tool_state.get("any_argument_delta_emitted"):
                            yield "tool_calls", self._emit_tool_call_chunk(
                                tool_state,
                                arguments_delta=str(tool_state.get("arguments", "")),
                            )
                        elif not tool_state.get("name_emitted") and tool_state.get("name"):
                            yield "tool_calls", self._emit_tool_call_chunk(
                                tool_state,
                                arguments_delta=None,
                                name_only=True,
                            )
                continue

            if payload_type in {"response.completed", "response.incomplete"}:
                response_payload = loaded_message.get("response", loaded_message)
                if isinstance(response_payload, dict):
                    response_record.update(response_payload)
                completed = True
                continue

        if not saw_any_event:
            return

        if not completed:
            output_items = self._collect_output_items(completed_output_items)
            if len(output_items) > 0:
                response_record["output"] = output_items
            response_record.setdefault("status", "completed")
            response_record.setdefault("object", "response")
            if "id" in meta:
                response_record.setdefault("id", meta["id"])
            if "model" in meta:
                response_record.setdefault("model", meta["model"])

        response_output_items = response_record.get("output")
        if isinstance(response_output_items, list) and len(response_output_items) == 0 and len(completed_output_items) > 0:
            response_record["output"] = self._collect_output_items(completed_output_items)
            response_output_items = response_record["output"]

        done_content = self._collect_assistant_text(response_output_items)
        if done_content == "":
            done_content = content_buffer
        reasoning_done = self._extract_reasoning_summary(response_record)
        if reasoning_done == "":
            reasoning_done = reasoning_buffer

        yield "done", done_content
        yield "reasoning_done", reasoning_done
        yield "original_done", response_record

        if "id" in response_record:
            meta["id"] = response_record["id"]
        if "model" in response_record:
            meta["model"] = response_record["model"]
        if "status" in response_record:
            meta["status"] = response_record["status"]
        if "usage" in response_record:
            meta["usage"] = response_record["usage"]
        meta["finish_reason"] = self._build_finish_reason(response_record)
        yield "meta", meta
