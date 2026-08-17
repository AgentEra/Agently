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

from agently_stage import default_stage_call_bridge

import asyncio
import contextlib
import html
import re
import warnings

from typing import TYPE_CHECKING, Any, AsyncGenerator, Generator, Literal, Mapping, cast, overload
from pydantic import BaseModel, ValidationError

from agently.types.plugins import ResponseParser
from agently.types.data import StreamingData
from agently.utils import (
    DataPathBuilder,
    DataFormatter,
    StateDataNamespace,
    GeneratorConsumer,
    StreamingJSONParser,
    DeprecationWarnings,
)

from agently.builtins.plugins.ResponseParser.modules.json_output import (
    parse_json_output,
)
from agently.builtins.plugins.ResponseParser.modules.flat_markdown import (
    FlatMarkdownStreamingParser,
    parse_flat_markdown_output,
)
from agently.builtins.plugins.ResponseParser.modules.hybrid import (
    HybridStreamingParser,
    parse_hybrid_output,
)
from agently.builtins.plugins.ResponseParser.modules.xml_field import (
    XmlFieldStreamingParser,
    extract_xml_field_target,
    parse_xml_field_output,
)
from agently.builtins.plugins.ResponseParser.modules.yaml_literal import (
    YamlLiteralStreamingParser,
    extract_yaml_literal_target,
    parse_yaml_literal_output,
)

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.types.data import (
        AgentlyModelResultMessage,
        AgentlyModelResult,
        AgentlyOriginalResultPayload,
        AgentlyResultGenerator,
        AgentlySpecificResultMessage,
        InstantStreamingContentType,
        ResultContentType,
        RunContext,
        SerializableMapping,
        SpecificEvents,
    )
    from agently.utils import Settings

DEFAULT_SPECIFIC_EVENTS = cast(
    "SpecificEvents",
    ["reasoning_delta", "delta", "reasoning_done", "done", "tool_calls"],
)

STRUCTURED_OUTPUT_FORMATS = {"json", "flat_markdown", "hybrid", "xml_field", "yaml_literal"}


class LeadingThinkEventNormalizer:
    """Normalize leading ``<think>...</think>`` content into reasoning events."""

    _OPEN = "<think>"
    _CLOSE_RE = re.compile(r"</think>", flags=re.IGNORECASE)

    def __init__(self):
        self._mode: Literal["unknown", "reasoning", "answer"] = "unknown"
        self._pending = ""
        self._reasoning_buffer = ""
        self._reasoning_done_emitted = False

    def feed_delta(self, chunk: str) -> list[tuple[str, str]]:
        if self._mode == "answer":
            return [("delta", chunk)] if chunk else []

        self._pending += chunk
        if self._mode == "unknown":
            leading = self._pending.lstrip()
            if not leading:
                return []
            lowered = leading.lower()
            if self._OPEN.startswith(lowered) and len(lowered) < len(self._OPEN):
                return []
            if lowered.startswith(self._OPEN):
                self._mode = "reasoning"
                self._pending = leading[len(self._OPEN):]
                return self._drain_reasoning_pending()
            self._mode = "answer"
            answer = self._pending
            self._pending = ""
            return [("delta", answer)] if answer else []

        return self._drain_reasoning_pending()

    def feed_done(self, content: str) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        answer = self._strip_leading_think(content)
        if self._reasoning_buffer and not self._reasoning_done_emitted:
            events.append(("reasoning_done", self._reasoning_buffer))
            self._reasoning_done_emitted = True
        events.append(("done", answer))
        return events

    def _drain_reasoning_pending(self) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        close = self._CLOSE_RE.search(self._pending)
        if close is None:
            if self._pending:
                self._reasoning_buffer += self._pending
                events.append(("reasoning_delta", self._pending))
                self._pending = ""
            return events

        reasoning = self._pending[:close.start()]
        if reasoning:
            self._reasoning_buffer += reasoning
            events.append(("reasoning_delta", reasoning))
        if not self._reasoning_done_emitted:
            events.append(("reasoning_done", self._reasoning_buffer))
            self._reasoning_done_emitted = True
        rest = self._pending[close.end():].lstrip()
        self._pending = ""
        self._mode = "answer"
        if rest:
            events.append(("delta", rest))
        return events

    def _strip_leading_think(self, content: str) -> str:
        leading = content.lstrip()
        if not leading.lower().startswith(self._OPEN):
            return content
        close = self._CLOSE_RE.search(leading)
        if close is None:
            return content
        reasoning = leading[len(self._OPEN):close.start()]
        if reasoning and not self._reasoning_buffer:
            self._reasoning_buffer = reasoning
        answer = leading[close.end():].lstrip()
        if not answer:
            return content
        return answer


class AgentlyResponseParser(ResponseParser):
    name = "AgentlyResponseParser"
    DEFAULT_SETTINGS = {
        "$global": {
            "response": {
                "streaming_parse": False,
                "streaming_parse_path_style": "dot",
            },
        },
    }

    def __init__(
        self,
        agent_name: str,
        response_id: str,
        prompt: "Prompt",
        response_generator: "AgentlyResultGenerator",
        settings: "Settings",
        run_context: "RunContext | None" = None,
    ):
        self.agent_name = agent_name
        self.response_id = response_id
        self.response_generator = response_generator
        self.settings = settings
        self.run_context = run_context
        self.plugin_settings = StateDataNamespace(self.settings, f"plugins.ResponseParser.{ self.name }")
        self.full_result_data: AgentlyModelResult = {
            "result_consumer": None,
            "meta": {},
            "original_delta": [],
            "original_done": {},
            "text_result": "",
            "cleaned_result": "",
            "parsed_result": None,
            "result_object": None,
            "errors": [],
            "extra": {},
        }
        self._prompt_object = prompt.to_prompt_object()
        self._OutputModel = prompt.to_output_model() if self._prompt_object.output_format in STRUCTURED_OUTPUT_FORMATS else None
        self._response_consumer: GeneratorConsumer | None = None
        self._consumer_lock = asyncio.Lock()
        self._final_json_parse_result: tuple[str | None, Any, BaseModel | None, bool] | None = None
        self._runtime_observations: list[dict[str, Any]] = []
        self._output_validation_feedback: list[str] = []

        self._streaming_canceled = False

        self.get_meta = default_stage_call_bridge.as_sync(self.async_get_meta)
        self.get_text = default_stage_call_bridge.as_sync(self.async_get_text)
        self.get_data = default_stage_call_bridge.as_sync(self.async_get_data)
        self.get_data_object = default_stage_call_bridge.as_sync(self.async_get_data_object)

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    @staticmethod
    def _format_validation_location(location: Any) -> str:
        if not isinstance(location, (list, tuple)):
            return str(location) if location else "<root>"
        parts: list[str] = []
        for item in location:
            if isinstance(item, int):
                if parts:
                    parts[-1] = f"{parts[-1]}[{item}]"
                else:
                    parts.append(f"[{item}]")
            else:
                parts.append(str(item))
        return ".".join(parts) if parts else "<root>"

    @classmethod
    def _validation_feedback_from_error(cls, error: BaseException) -> list[str]:
        if isinstance(error, ValidationError):
            feedback: list[str] = []
            for detail in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )[:8]:
                location = cls._format_validation_location(detail.get("loc"))
                message = str(detail.get("msg") or detail.get("type") or "Invalid value")
                feedback.append(f"{location}: {message}"[:300])
            if feedback:
                return feedback
        return [f"<root>: {str(error)}"[:300]]

    def _build_result_object(self, parsed: Any) -> BaseModel | None:
        self._output_validation_feedback = []
        try:
            if self._OutputModel:
                return self._OutputModel.model_validate(parsed)
        except Exception as exc:
            self._output_validation_feedback = self._validation_feedback_from_error(exc)
            return None
        return None

    def get_output_validation_feedback(self) -> list[str]:
        return list(self._output_validation_feedback)

    def _parse_json_output(self, text: str) -> tuple[str | None, Any, BaseModel | None, bool]:
        return parse_json_output(
            text,
            self._prompt_object.output,
            self._build_result_object,
        )

    def _known_field_section_seen(self, text: str) -> bool:
        output_schema = self._prompt_object.output
        if not isinstance(output_schema, Mapping) or not output_schema:
            return False
        escaped_names = "|".join(re.escape(str(name)) for name in output_schema.keys())
        if not escaped_names:
            return False
        pattern = rf"^###\s+({escaped_names})\s*(?:\[(?:text|JSON)\])?\s*$"
        return re.search(pattern, text, flags=re.MULTILINE) is not None

    def _parse_structured_text_output(self, text: str) -> tuple[Any | None, str | None, bool]:
        payload_extracted = False
        try:
            match self._prompt_object.output_format:
                case "flat_markdown":
                    payload_extracted = self._known_field_section_seen(text)
                    parsed = parse_flat_markdown_output(text, self._prompt_object.output or {})
                case "hybrid":
                    payload_extracted = self._known_field_section_seen(text)
                    parsed = parse_hybrid_output(text, self._prompt_object.output or {})
                case "xml_field":
                    payload_extracted = extract_xml_field_target(text) is not None
                    parsed = parse_xml_field_output(text, self._prompt_object.output or {})
                case "yaml_literal":
                    payload_extracted = extract_yaml_literal_target(text) is not None
                    parsed = parse_yaml_literal_output(text, self._prompt_object.output or {})
                case _:
                    return None, f"Unsupported structured output format: {self._prompt_object.output_format}", False
        except Exception as exc:
            return None, f"{exc.__class__.__name__}: {exc}", payload_extracted
        if parsed is None:
            if payload_extracted:
                return None, "Structured payload was found, but parser could not materialize it.", True
            return None, "Parser returned no structured payload.", False
        return parsed, None, payload_extracted

    def _parse_json_fallback_output(
        self,
        text: str,
    ) -> tuple[dict[str, Any] | None, str | None, bool, BaseModel | None, bool, str | None]:
        completed, parsed, result_object, repaired = self._parse_json_output(text)
        payload_extracted = completed is not None
        if parsed is None:
            parse_error = (
                "No JSON payload could be located."
                if completed is None
                else "Located JSON payload could not be parsed."
            )
            return None, parse_error, payload_extracted, None, repaired, completed
        if not isinstance(parsed, Mapping):
            return None, "JSON fallback parsed a non-dict payload.", payload_extracted, None, repaired, completed
        return dict(parsed), None, payload_extracted, result_object, repaired, completed

    def _record_runtime_observation(
        self,
        kind: str,
        *,
        message: str,
        level: str = "INFO",
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._runtime_observations.append(
            {
                "kind": kind,
                "level": level,
                "message": message,
                "payload": payload or {},
                "error": error,
            }
        )

    def drain_runtime_observations(self) -> list[dict[str, Any]]:
        observations = self._runtime_observations
        self._runtime_observations = []
        return observations

    @staticmethod
    def _should_reset_for_retry_status(data: Any) -> bool:
        return isinstance(data, Mapping) and data.get("status") == "failed" and data.get("retry") is True

    @staticmethod
    def _format_delta_retry_marker(data: Any) -> str:
        """Format the plain-delta replay boundary without trusting provider text."""

        reason = data.get("reason") if isinstance(data, Mapping) else None
        text = str(reason).strip() if reason is not None else ""
        if not text:
            text = "Retrying model request."
        return f"<$retry>{ html.escape(text, quote=False) }</$retry>"

    def _reset_retried_attempt_result(self) -> None:
        """Discard parser-owned state from an attempt replaced by a replay."""

        self.full_result_data["meta"].clear()
        self.full_result_data["original_delta"].clear()
        self.full_result_data["original_done"] = {}
        self.full_result_data["text_result"] = ""
        self.full_result_data["cleaned_result"] = ""
        self.full_result_data["parsed_result"] = None
        self.full_result_data["result_object"] = None
        self.full_result_data["errors"].clear()
        self.full_result_data["extra"].clear()
        self._final_json_parse_result = None
        self._output_validation_feedback = []
        self._streaming_canceled = False

    async def _handle_done_event(self, data: Any, buffer: str) -> None:
        self.full_result_data["text_result"] = str(data)
        if self._prompt_object.output_format == "json":
            self._final_json_parse_result = self._parse_json_output(str(data))
            completed, parsed, result_object, repaired = self._final_json_parse_result
            if parsed is not None:
                self.full_result_data["cleaned_result"] = completed
                self.full_result_data["parsed_result"] = parsed
                self.full_result_data["result_object"] = result_object
                self.full_result_data["extra"]["output_format"] = "json"
                self.full_result_data["extra"]["parse_error"] = None
                self.full_result_data["extra"]["payload_extracted"] = completed is not None
                self.full_result_data["extra"]["parse_success"] = True
                self._record_runtime_observation(
                    "completed",
                    message="Model response parsed as JSON output.",
                    payload={
                        "result": DataFormatter.sanitize(parsed),
                        "raw_text": str(data),
                        "cleaned_text": completed,
                        "repaired": repaired,
                        "streamed_text": buffer,
                        "format": "json",
                        "resolved_format": "json",
                        "payload_extracted": completed is not None,
                        "parse_success": True,
                        "parse_error": None,
                    },
                )
            else:
                parse_error = (
                    "No JSON payload could be located."
                    if completed is None
                    else "Located JSON payload could not be parsed."
                )
                self.full_result_data["cleaned_result"] = completed
                self.full_result_data["parsed_result"] = None
                self.full_result_data["result_object"] = None
                self.full_result_data["extra"]["output_format"] = "json"
                self.full_result_data["extra"]["parse_error"] = parse_error
                self.full_result_data["extra"]["payload_extracted"] = completed is not None
                self.full_result_data["extra"]["parse_success"] = False
                self._record_runtime_observation(
                    "parse_failed",
                    level="WARNING",
                    message="Can not parse JSON output from model response.",
                    payload={
                        "result": str(data),
                        "cleaned_text": completed,
                        "streamed_text": buffer,
                        "format": "json",
                        "resolved_format": "json",
                        "payload_extracted": completed is not None,
                        "parse_success": False,
                        "parse_error": parse_error,
                    },
                )
            return

        if self._prompt_object.output_format in ("flat_markdown", "hybrid", "xml_field", "yaml_literal"):
            parsed, parse_error, payload_extracted = self._parse_structured_text_output(str(data))
            self.full_result_data["extra"]["parse_error"] = parse_error
            self.full_result_data["extra"]["output_format"] = self._prompt_object.output_format
            self.full_result_data["extra"]["resolved_output_format"] = self._prompt_object.output_format
            self.full_result_data["extra"]["payload_extracted"] = payload_extracted
            self.full_result_data["extra"]["format_fallback"] = None
            if parsed is not None:
                result_object = self._build_result_object(parsed)
                self.full_result_data["parsed_result"] = parsed
                self.full_result_data["result_object"] = result_object
                self.full_result_data["text_result"] = str(data)
                self.full_result_data["extra"]["parse_success"] = True
                self._record_runtime_observation(
                    "completed",
                    message=f"Model response parsed as {self._prompt_object.output_format} output.",
                    payload={
                        "result": DataFormatter.sanitize(parsed),
                        "raw_text": str(data),
                        "streamed_text": buffer,
                        "format": self._prompt_object.output_format,
                        "resolved_format": self._prompt_object.output_format,
                        "payload_extracted": payload_extracted,
                        "parse_success": True,
                        "parse_error": None,
                    },
                )
            else:
                (
                    fallback_parsed,
                    fallback_error,
                    fallback_payload_extracted,
                    fallback_result_object,
                    fallback_repaired,
                    fallback_cleaned,
                ) = self._parse_json_fallback_output(str(data))
                if fallback_parsed is not None:
                    self.full_result_data["cleaned_result"] = fallback_cleaned
                    self.full_result_data["parsed_result"] = fallback_parsed
                    self.full_result_data["result_object"] = fallback_result_object
                    self.full_result_data["text_result"] = str(data)
                    self.full_result_data["extra"]["parse_error"] = None
                    self.full_result_data["extra"]["parse_success"] = True
                    self.full_result_data["extra"]["resolved_output_format"] = "json"
                    self.full_result_data["extra"]["payload_extracted"] = fallback_payload_extracted
                    self.full_result_data["extra"]["format_fallback"] = {
                        "from": self._prompt_object.output_format,
                        "to": "json",
                        "reason": parse_error,
                        "repaired": fallback_repaired,
                    }
                    self._record_runtime_observation(
                        "completed",
                        message=(
                            f"Model response parsed as JSON fallback after "
                            f"{self._prompt_object.output_format} output parsing failed."
                        ),
                        payload={
                            "result": DataFormatter.sanitize(fallback_parsed),
                            "raw_text": str(data),
                            "cleaned_text": fallback_cleaned,
                            "streamed_text": buffer,
                            "format": self._prompt_object.output_format,
                            "resolved_format": "json",
                            "payload_extracted": fallback_payload_extracted,
                            "parse_success": True,
                            "parse_error": None,
                            "format_fallback": {
                                "from": self._prompt_object.output_format,
                                "to": "json",
                                "reason": parse_error,
                                "repaired": fallback_repaired,
                            },
                        },
                    )
                else:
                    combined_error = parse_error
                    if fallback_error:
                        combined_error = f"{parse_error}; JSON fallback: {fallback_error}"
                    self.full_result_data["parsed_result"] = None
                    self.full_result_data["result_object"] = None
                    self.full_result_data["text_result"] = str(data)
                    self.full_result_data["extra"]["parse_error"] = combined_error
                    self.full_result_data["extra"]["parse_success"] = False
                    self._record_runtime_observation(
                        "parse_failed",
                        level="WARNING",
                        message=f"Can not parse {self._prompt_object.output_format} output from model response.",
                        payload={
                            "result": str(data),
                            "streamed_text": buffer,
                            "format": self._prompt_object.output_format,
                            "resolved_format": self._prompt_object.output_format,
                            "payload_extracted": payload_extracted or fallback_payload_extracted,
                            "parse_success": False,
                            "parse_error": combined_error,
                            "format_fallback": {
                                "from": self._prompt_object.output_format,
                                "to": "json",
                                "reason": parse_error,
                                "error": fallback_error,
                            },
                        },
                    )
            return

        if (
            isinstance(data, list)
            and isinstance(data[0], dict)
            and "object" in data[0]
            and data[0]["object"] == "embedding"
        ):
            data = [item["embedding"] for item in data]
        self.full_result_data["parsed_result"] = data
        if self.settings.get("$log.cancel_logs") is not True:
            self._record_runtime_observation(
                "completed",
                message="Model response parsing completed.",
                payload={
                    "result": DataFormatter.sanitize(data),
                    "raw_text": str(data),
                    "streamed_text": buffer,
                    "format": self._prompt_object.output_format,
                    "resolved_format": self._prompt_object.output_format,
                },
            )

    async def _flush_streaming_json_events(self, streaming_json_parser: StreamingJSONParser) -> AsyncGenerator[StreamingData, None]:
        if self._prompt_object.output_format != "json":
            return

        parsed_result = self.full_result_data["parsed_result"]
        if parsed_result is None:
            return

        async for streaming_data in streaming_json_parser.flush_final_data(parsed_result):
            yield streaming_data

    def _new_streaming_json_parser(self) -> StreamingJSONParser:
        max_chars: Any = self.settings.get("response.streaming_parse_max_incomplete_chars", None)
        if max_chars is None:
            max_chars = StreamingJSONParser.DEFAULT_MAX_INCOMPLETE_PARSE_CHARS
        else:
            try:
                max_chars = int(max_chars)
            except (TypeError, ValueError):
                max_chars = StreamingJSONParser.DEFAULT_MAX_INCOMPLETE_PARSE_CHARS
        return StreamingJSONParser(
            self._prompt_object.output,
            max_incomplete_parse_chars=max_chars,
        )

    @staticmethod
    def _streaming_path_parts(path: str) -> list[str | int]:
        if not path or path.startswith("$"):
            return []
        parts: list[str | int] = []
        buffer = ""
        index = 0
        while index < len(path):
            char = path[index]
            if char == ".":
                if buffer:
                    parts.append(buffer)
                    buffer = ""
                index += 1
                continue
            if char == "[":
                if buffer:
                    parts.append(buffer)
                    buffer = ""
                end = path.find("]", index)
                if end < 0:
                    return []
                raw_index = path[index + 1 : end]
                if not raw_index.isdigit():
                    return []
                parts.append(int(raw_index))
                index = end + 1
                continue
            buffer += char
            index += 1
        if buffer:
            parts.append(buffer)
        return parts

    @staticmethod
    def _streaming_child_container(next_part: str | int) -> list[Any] | dict[str, Any]:
        return [] if isinstance(next_part, int) else {}

    @classmethod
    def _set_streaming_snapshot_path(cls, root: dict[str, Any], path: str, value: Any) -> None:
        parts = cls._streaming_path_parts(path)
        if not parts:
            return
        current: Any = root
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            next_part = parts[index + 1] if not is_last else None
            if isinstance(part, int):
                if not isinstance(current, list):
                    return
                while len(current) <= part:
                    current.append(None)
                if is_last:
                    current[part] = value
                    return
                child = current[part]
                if not isinstance(child, (dict, list)):
                    child = cls._streaming_child_container(cast(str | int, next_part))
                    current[part] = child
                current = child
                continue
            if not isinstance(current, dict):
                return
            if is_last:
                current[part] = value
                return
            child = current.get(part)
            if not isinstance(child, (dict, list)):
                child = cls._streaming_child_container(cast(str | int, next_part))
                current[part] = child
            current = child

    def _record_streaming_snapshot(self, streaming_data: StreamingData) -> None:
        if not streaming_data.is_complete or streaming_data.path.startswith("$"):
            return
        if (
            streaming_data.completion_source == "observed_boundary"
            and self._final_json_parse_result is not None
            and self._final_json_parse_result[1] is not None
        ):
            # The provider-complete payload has already been parsed in full.
            # Observed-boundary events may carry an older incremental
            # ``full_data`` snapshot when large-JSON parsing was deferred; do
            # not let that provisional snapshot replace the authoritative
            # final value.
            return
        snapshot = self.full_result_data.get("parsed_result")
        if not isinstance(snapshot, dict):
            snapshot = {}
            self.full_result_data["parsed_result"] = snapshot
        if streaming_data.full_data not in (None, "", [], {}):
            self.full_result_data["parsed_result"] = DataFormatter.sanitize(streaming_data.full_data)
        else:
            self._set_streaming_snapshot_path(
                snapshot,
                streaming_data.path,
                DataFormatter.sanitize(streaming_data.value),
            )
        parsed_result = self.full_result_data.get("parsed_result")
        self.full_result_data["result_object"] = self._build_result_object(parsed_result)
        self.full_result_data["extra"]["streaming_snapshot"] = True
        self.full_result_data["extra"]["parse_success"] = parsed_result is not None

    def _prepare_streaming_data_for_yield(self, streaming_data: StreamingData, path_style: str) -> StreamingData:
        self._record_streaming_snapshot(streaming_data)
        if path_style == "slash":
            streaming_data.path = DataPathBuilder.convert_dot_to_slash(streaming_data.path)
        return streaming_data

    async def async_close(self) -> None:
        if self._response_consumer is not None:
            await self._response_consumer.close()

    async def _ensure_consumer(self):
        if self._response_consumer is None:
            async with self._consumer_lock:
                if self._response_consumer is None:
                    self._response_consumer = GeneratorConsumer(self._extract())

    async def _wait_for_consumer_result(self):
        await self._ensure_consumer()
        consumer = cast(GeneratorConsumer, self._response_consumer)
        try:
            await consumer.get_result()
        except asyncio.CancelledError:
            await consumer.close()
            raise

    @staticmethod
    def _normalize_error_event(data: Any) -> Exception:
        if isinstance(data, Exception):
            return data
        return RuntimeError(str(data) if data is not None else "Model response stream emitted an error.")

    async def _extract(self):
        buffer = ""
        stream_chunk_index = 0
        think_normalizer = LeadingThinkEventNormalizer()
        try:
            async for item in self.response_generator:
                try:
                    event, data = item
                except (TypeError, ValueError):
                    warnings.warn(f"\n⚠️ Incorrect response data from Agently Response Generator: { item }")
                    continue
                if event == "status":
                    if self._should_reset_for_retry_status(data):
                        buffer = ""
                        stream_chunk_index = 0
                        think_normalizer = LeadingThinkEventNormalizer()
                        self._reset_retried_attempt_result()
                    yield event, data
                    continue
                if event == "delta":
                    for normalized_event, normalized_data in think_normalizer.feed_delta(str(data)):
                        yield normalized_event, normalized_data
                        if normalized_event == "delta":
                            buffer += str(normalized_data)
                            stream_chunk_index += 1
                            if self.settings.get("$log.cancel_logs") is not True:
                                self._record_runtime_observation(
                                    "streaming",
                                    level="DEBUG",
                                    message=str(normalized_data),
                                    payload={
                                        "delta": str(normalized_data),
                                        "chunk_index": stream_chunk_index,
                                    },
                                )
                            elif self._streaming_canceled is False:
                                self._record_runtime_observation(
                                    "streaming_canceled",
                                    level="INFO",
                                    message=f"Streaming logs canceled for response '{ self.response_id }'.",
                                )
                                self._streaming_canceled = True
                    continue
                if event == "done":
                    if isinstance(data, str):
                        normalized_done_events = think_normalizer.feed_done(data)
                    else:
                        normalized_done_events = [("done", data)]
                    for normalized_event, normalized_data in normalized_done_events:
                        if normalized_event == "done":
                            await self._handle_done_event(normalized_data, buffer)
                        yield normalized_event, normalized_data
                    continue
                yield event, data
                match event:
                    case "original_delta":
                        self.full_result_data["original_delta"].append(data)
                    case "original_done":
                        self.full_result_data["original_done"] = data
                    case "meta":
                        if isinstance(data, Mapping):
                            self.full_result_data["meta"].update(dict(data))
                            self._record_runtime_observation(
                                "meta",
                                message="Model response meta updated.",
                                payload={"meta": dict(data)},
                            )
                    case "error":
                        error = self._normalize_error_event(data)
                        self.full_result_data["errors"].append(error)
                        self._record_runtime_observation(
                            "failed",
                            level="ERROR",
                            message="Model response stream emitted an error.",
                            error=error,
                        )
                        raise error
        finally:
            if hasattr(self.response_generator, "aclose"):
                with contextlib.suppress(RuntimeError):
                    await self.response_generator.aclose()

    async def async_get_meta(self) -> "SerializableMapping":
        await self._wait_for_consumer_result()
        return self.full_result_data["meta"]

    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', "all"] | None = "parsed",
        content: Literal['original', 'parsed', "all"] | None = "parsed",
    ) -> Any:
        await self._wait_for_consumer_result()
        if type is None and content is not None:
            DeprecationWarnings.warn_deprecated_once(
                "AgentlyResponseParser.async_get_data.content",
                "Parameter `content` in method .async_get_data() is  deprecated and will be removed in future version, please use parameter `type` instead.",
                stacklevel=2,
            )
            type = content
        match type:
            case "original":
                return self.full_result_data["original_done"].copy()
            case "parsed":
                parsed = self.full_result_data["parsed_result"]
                return parsed.copy() if hasattr(parsed, "copy") else parsed  # type: ignore
            case "all":
                return self.full_result_data.copy()

    async def async_get_data_object(self) -> BaseModel | None:
        if self._prompt_object.output_format not in STRUCTURED_OUTPUT_FORMATS:
            raise TypeError(
                "Error: Cannot build an output model for a non-structure output.\n"
                f"Output Format: { self._prompt_object.output_format }\n"
                f"Output Prompt: { self._prompt_object.output }"
            )
        await self._wait_for_consumer_result()
        return self.full_result_data["result_object"]

    async def async_get_text(self) -> str:
        await self._wait_for_consumer_result()
        return self.full_result_data["text_result"]

    @overload
    def get_async_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator[StreamingData, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyModelResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlySpecificResultMessage", None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator[str, None]: ...

    @overload
    def get_async_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator["AgentlyOriginalResultPayload", None]: ...

    @overload
    def get_async_generator(
        self,
        type: "ResultContentType | None" = "delta",
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator: ...

    async def get_async_generator(
        self,
        type: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        content: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> AsyncGenerator:
        await self._ensure_consumer()
        consumer = cast(GeneratorConsumer, self._response_consumer)
        parsed_generator = consumer.get_async_generator()
        _streaming_parse_path_style = str(self.settings.get("response.streaming_parse_path_style", "dot") or "dot")
        streaming_json_parser = None
        streaming_flat_markdown_parser = None
        streaming_hybrid_parser = None
        streaming_xml_field_parser = None
        streaming_yaml_literal_parser = None
        if type in ("instant", "streaming_parse"):
            if self._prompt_object.output_format == "json":
                streaming_json_parser = self._new_streaming_json_parser()
            elif self._prompt_object.output_format == "flat_markdown":
                streaming_flat_markdown_parser = FlatMarkdownStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "hybrid":
                streaming_hybrid_parser = HybridStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "xml_field":
                streaming_xml_field_parser = XmlFieldStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "yaml_literal":
                streaming_yaml_literal_parser = YamlLiteralStreamingParser(self._prompt_object.output or {})
        if type is None and content is not None:
            DeprecationWarnings.warn_deprecated_once(
                "AgentlyResponseParser.get_async_generator.content",
                "Parameter `content` in method .get_async_generator() is  deprecated and will be removed in future version, please use parameter `type` instead.",
                stacklevel=2,
            )
            type = content
        try:
            async for event, data in parsed_generator:
                match type:
                    case "all":
                        yield event, data
                    case "delta":
                        if event == "status" and self._should_reset_for_retry_status(data):
                            yield self._format_delta_retry_marker(data)
                        elif event == "delta":
                            yield data
                    case "specific":
                        if specific is None:
                            specific = ["delta"]
                        elif isinstance(specific, str):
                            specific = [specific]
                        if event in specific:
                            yield event, data
                    case "instant" | "streaming_parse":
                        if event == "status":
                            if self._should_reset_for_retry_status(data):
                                if streaming_json_parser is not None:
                                    streaming_json_parser = self._new_streaming_json_parser()
                                if streaming_flat_markdown_parser is not None:
                                    streaming_flat_markdown_parser = FlatMarkdownStreamingParser(self._prompt_object.output or {})
                                if streaming_hybrid_parser is not None:
                                    streaming_hybrid_parser = HybridStreamingParser(self._prompt_object.output or {})
                                if streaming_xml_field_parser is not None:
                                    streaming_xml_field_parser = XmlFieldStreamingParser(self._prompt_object.output or {})
                                if streaming_yaml_literal_parser is not None:
                                    streaming_yaml_literal_parser = YamlLiteralStreamingParser(self._prompt_object.output or {})
                            yield StreamingData(path="$status", value=data)
                            continue
                        if streaming_json_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_json_parser.parse_chunk(str(data)):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                            if event == "tool_calls":
                                yield StreamingData(path="$tool_calls", value=data)
                            elif event == "done":
                                async for streaming_data in (
                                    streaming_json_parser.flush_observed_boundaries()
                                ):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                                async for streaming_data in self._flush_streaming_json_events(streaming_json_parser):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                        if streaming_flat_markdown_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_flat_markdown_parser.parse_chunk(str(data)):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                            elif event == "done":
                                async for streaming_data in streaming_flat_markdown_parser.flush_final_data(
                                    self.full_result_data.get("parsed_result")
                                ):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                        if streaming_hybrid_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_hybrid_parser.parse_chunk(str(data)):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                            elif event == "done":
                                async for streaming_data in streaming_hybrid_parser.flush():
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                        if streaming_xml_field_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_xml_field_parser.parse_chunk(str(data)):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                            elif event == "done":
                                async for streaming_data in streaming_xml_field_parser.flush():
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                        if streaming_yaml_literal_parser is not None:
                            if event == "delta":
                                async for streaming_data in streaming_yaml_literal_parser.parse_chunk(str(data)):
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                            elif event == "done":
                                async for streaming_data in streaming_yaml_literal_parser.flush():
                                    yield self._prepare_streaming_data_for_yield(
                                        streaming_data,
                                        _streaming_parse_path_style,
                                    )
                    case "original":
                        if event.startswith("original"):
                            yield data
        except asyncio.CancelledError:
            await consumer.close()
            raise

    @overload
    def get_generator(
        self,
        type: "InstantStreamingContentType",
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator[StreamingData, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["all"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyModelResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["specific"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlySpecificResultMessage", None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["delta"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator[str, None, None]: ...

    @overload
    def get_generator(
        self,
        type: Literal["original"],
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator["AgentlyOriginalResultPayload", None, None]: ...

    @overload
    def get_generator(
        self,
        type: "ResultContentType | None" = "delta",
        content: "ResultContentType | None" = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator: ...

    def get_generator(
        self,
        type: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        content: Literal['all', 'delta', 'specific', 'original', 'instant', 'streaming_parse'] | None = "delta",
        *,
        specific: "SpecificEvents" = DEFAULT_SPECIFIC_EVENTS,
    ) -> Generator:
        asyncio.run(self._ensure_consumer())
        parsed_generator = cast(GeneratorConsumer, self._response_consumer).get_generator()
        _streaming_parse_path_style = str(self.settings.get("response.streaming_parse_path_style", "dot") or "dot")
        streaming_json_parser = None
        streaming_flat_markdown_parser = None
        streaming_hybrid_parser = None
        streaming_xml_field_parser = None
        streaming_yaml_literal_parser = None
        if type in ("instant", "streaming_parse"):
            if self._prompt_object.output_format == "json":
                streaming_json_parser = self._new_streaming_json_parser()
            elif self._prompt_object.output_format == "flat_markdown":
                streaming_flat_markdown_parser = FlatMarkdownStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "hybrid":
                streaming_hybrid_parser = HybridStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "xml_field":
                streaming_xml_field_parser = XmlFieldStreamingParser(self._prompt_object.output or {})
            elif self._prompt_object.output_format == "yaml_literal":
                streaming_yaml_literal_parser = YamlLiteralStreamingParser(self._prompt_object.output or {})
        if type is None and content is not None:
            DeprecationWarnings.warn_deprecated_once(
                "AgentlyResponseParser.get_generator.content",
                "Parameter `content` in method .get_generator() is  deprecated and will be removed in future version, please use parameter `type` instead.",
                stacklevel=2,
            )
            type = content
        for event, data in parsed_generator:
            match type:
                case "all":
                    yield event, data
                case "delta":
                    if event == "status" and self._should_reset_for_retry_status(data):
                        yield self._format_delta_retry_marker(data)
                    elif event == "delta":
                        yield data
                case "specific":
                    if specific is None:
                        specific = ["delta"]
                    elif isinstance(specific, str):
                        specific = [specific]
                    if event in specific:
                        yield event, data
                case "instant" | "streaming_parse":
                    if event == "status":
                        if self._should_reset_for_retry_status(data):
                            if streaming_json_parser is not None:
                                streaming_json_parser = self._new_streaming_json_parser()
                            if streaming_flat_markdown_parser is not None:
                                streaming_flat_markdown_parser = FlatMarkdownStreamingParser(self._prompt_object.output or {})
                            if streaming_hybrid_parser is not None:
                                streaming_hybrid_parser = HybridStreamingParser(self._prompt_object.output or {})
                            if streaming_xml_field_parser is not None:
                                streaming_xml_field_parser = XmlFieldStreamingParser(self._prompt_object.output or {})
                            if streaming_yaml_literal_parser is not None:
                                streaming_yaml_literal_parser = YamlLiteralStreamingParser(self._prompt_object.output or {})
                        yield StreamingData(path="$status", value=data)
                        continue
                    if streaming_json_parser is not None:
                        if event == "delta":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_json_parser.parse_chunk(str(data))
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                        if event == "tool_calls":
                            yield StreamingData(path="$tool_calls", value=data)
                        elif event == "done":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_json_parser.flush_observed_boundaries()
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                self._flush_streaming_json_events(streaming_json_parser)
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                    if streaming_flat_markdown_parser is not None:
                        if event == "delta":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_flat_markdown_parser.parse_chunk(str(data))
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                        elif event == "done":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_flat_markdown_parser.flush_final_data(
                                    self.full_result_data.get("parsed_result")
                                )
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                    if streaming_hybrid_parser is not None:
                        if event == "delta":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_hybrid_parser.parse_chunk(str(data))
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                        elif event == "done":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_hybrid_parser.flush()
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                    if streaming_xml_field_parser is not None:
                        if event == "delta":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_xml_field_parser.parse_chunk(str(data))
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                        elif event == "done":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_xml_field_parser.flush()
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                    if streaming_yaml_literal_parser is not None:
                        if event == "delta":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_yaml_literal_parser.parse_chunk(str(data))
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                        elif event == "done":
                            for streaming_data in default_stage_call_bridge.iter_sync(
                                streaming_yaml_literal_parser.flush()
                            ):
                                yield self._prepare_streaming_data_for_yield(
                                    streaming_data,
                                    _streaming_parse_path_style,
                                )
                case "original":
                    if event.startswith("original"):
                        yield data
