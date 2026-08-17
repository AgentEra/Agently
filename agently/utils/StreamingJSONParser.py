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
import json5
from typing import Any, AsyncGenerator, List, Literal, TYPE_CHECKING
import copy

from agently.utils import DataLocator, DataPathBuilder, StreamingJSONCompleter
from agently.types.data import StreamingData

if TYPE_CHECKING:
    from agently.types.data import PromptOutputStructure


class StreamingJSONParser:
    """
    AsyncStreamingJSONParser parses streamed JSON data chunk by chunk asynchronously, maintaining parsing state and emitting
    incremental ("delta") and completion ("done") events for each field as the structure is built up.

    This class is designed to process partial JSON data (e.g., from an LLM or network stream) and
    emit structured events as fields become available or finalized, allowing for responsive UI updates
    or downstream processing.

    Attributes:
        schema (PromptOutputStructure): The schema describing the expected JSON structure.
    """

    DEFAULT_MAX_INCOMPLETE_PARSE_CHARS = 1024

    def __init__(
        self,
        schema: "PromptOutputStructure",
        *,
        max_incomplete_parse_chars: int | None = DEFAULT_MAX_INCOMPLETE_PARSE_CHARS,
    ):
        """
        Initialize an AsyncStreamingJSONParser instance.

        Args:
            schema (PromptOutputStructure): The schema describing the expected JSON structure.
        """
        self.schema = schema
        self.max_incomplete_parse_chars = (
            self.DEFAULT_MAX_INCOMPLETE_PARSE_CHARS
            if max_incomplete_parse_chars is None
            else max_incomplete_parse_chars
        )
        self.completer = StreamingJSONCompleter()
        self.previous_data = {}
        self.current_data = {}
        self.field_completion_status = set()  # Tracks completed field paths
        self.string_values = {}  # Tracks current string values for fields
        self.last_complete_structure = {}  # Last complete structure for completion checks
        self._large_incremental_parse_deferred = False

        # Get the expected field parsing order and all possible paths
        self.expected_field_order = DataPathBuilder.extract_parsing_key_orders(schema, style="dot")
        self.all_possible_paths = DataPathBuilder.extract_possible_paths(schema, style="dot")

        self.current_parsing_position = 0  # Current position in parsing order

    @staticmethod
    def _looks_structurally_complete(text: str) -> bool:
        stack: list[str] = []
        started = False
        in_string = False
        escape = False
        comment: str | None = None
        string_char: str | None = None

        for index, char in enumerate(text):
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if comment is not None:
                if comment == "//" and char in "\r\n":
                    comment = None
                elif comment == "/*" and char == "*" and next_char == "/":
                    comment = None
                continue
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == string_char:
                    in_string = False
                    string_char = None
                continue
            if char == "/" and next_char == "/":
                comment = "//"
                continue
            if char == "/" and next_char == "*":
                comment = "/*"
                continue
            if char in "\"'":
                if started:
                    in_string = True
                    string_char = char
                continue
            if char in "{[":
                started = True
                stack.append("}" if char == "{" else "]")
                continue
            if char in "}]":
                if not stack or stack[-1] != char:
                    return False
                stack.pop()
                if started and not stack:
                    tail = text[index + 1 :].strip()
                    return not tail or tail.startswith("```")
        return False

    def _should_skip_large_incremental_parse(self) -> bool:
        if self.max_incomplete_parse_chars <= 0:
            return False
        raw_buffer = str(getattr(self.completer, "_buffer", "") or "")
        return len(raw_buffer) > self.max_incomplete_parse_chars

    def _large_incremental_parse_status(self) -> StreamingData:
        raw_buffer = str(getattr(self.completer, "_buffer", "") or "")
        self._large_incremental_parse_deferred = True
        return StreamingData(
            path="$status",
            value={
                "status": "streaming_parse_deferred",
                "reason": "large_json_incremental_parse",
                "buffer_chars": len(raw_buffer),
                "threshold": self.max_incomplete_parse_chars,
            },
            delta=None,
            is_complete=False,
            event_type="delta",
        )

    def _parse_buffer_once(self) -> bool:
        completed_json = self.completer.complete()
        located_json = DataLocator.locate_output_json(
            completed_json,
            self.schema,
        )
        if not located_json:
            return False
        try:
            parsed_data = json5.loads(located_json)
        except (json.JSONDecodeError, ValueError):
            return False
        self.previous_data = copy.deepcopy(self.current_data)
        self.current_data = parsed_data
        return True

    @staticmethod
    def _observed_complete_values(text: str) -> list[tuple[str, Any]]:
        """Parse only values whose closing token is present in the raw stream.

        The ordinary incremental parser intentionally repairs the open suffix so
        callers can render provisional data. This bounded recursive descent is
        the independent evidence path used for completion provenance: it never
        inserts a quote, delimiter, or container closer.
        """

        decoder = json.JSONDecoder()
        length = len(text)
        completed: list[tuple[str, Any]] = []

        def skip_space(index: int) -> int:
            while index < length and text[index].isspace():
                index += 1
            return index

        def dot_path(parts: list[str | int]) -> str:
            return DataPathBuilder.build_dot_path(parts)

        def parse_value(index: int, parts: list[str | int]) -> tuple[Any, int, bool]:
            index = skip_space(index)
            if index >= length:
                return None, index, False
            char = text[index]
            if char == "{":
                value: dict[str, Any] = {}
                cursor = skip_space(index + 1)
                if cursor < length and text[cursor] == "}":
                    path = dot_path(parts)
                    if path:
                        completed.append((path, value))
                    return value, cursor + 1, True
                while cursor < length:
                    try:
                        key, key_end = decoder.raw_decode(text, cursor)
                    except (json.JSONDecodeError, ValueError):
                        return value, cursor, False
                    if not isinstance(key, str):
                        return value, cursor, False
                    cursor = skip_space(key_end)
                    if cursor >= length or text[cursor] != ":":
                        return value, cursor, False
                    child, cursor, child_complete = parse_value(
                        cursor + 1,
                        [*parts, key],
                    )
                    if not child_complete:
                        return value, cursor, False
                    value[key] = child
                    cursor = skip_space(cursor)
                    if cursor < length and text[cursor] == ",":
                        cursor = skip_space(cursor + 1)
                        continue
                    if cursor < length and text[cursor] == "}":
                        path = dot_path(parts)
                        if path:
                            completed.append((path, value))
                        return value, cursor + 1, True
                    return value, cursor, False
                return value, cursor, False
            if char == "[":
                value_list: list[Any] = []
                cursor = skip_space(index + 1)
                if cursor < length and text[cursor] == "]":
                    path = dot_path(parts)
                    if path:
                        completed.append((path, value_list))
                    return value_list, cursor + 1, True
                item_index = 0
                while cursor < length:
                    child, cursor, child_complete = parse_value(
                        cursor,
                        [*parts, item_index],
                    )
                    if not child_complete:
                        return value_list, cursor, False
                    value_list.append(child)
                    cursor = skip_space(cursor)
                    if cursor < length and text[cursor] == ",":
                        item_index += 1
                        cursor = skip_space(cursor + 1)
                        continue
                    if cursor < length and text[cursor] == "]":
                        path = dot_path(parts)
                        if path:
                            completed.append((path, value_list))
                        return value_list, cursor + 1, True
                    return value_list, cursor, False
                return value_list, cursor, False
            try:
                value, end = decoder.raw_decode(text, index)
            except (json.JSONDecodeError, ValueError):
                return None, index, False
            boundary = skip_space(end)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and boundary >= length
            ):
                # A number at the current transport boundary may still grow
                # when the next chunk arrives (for example, `2` -> `25`).
                return None, index, False
            if boundary < length and text[boundary] not in {",", "]", "}"}:
                return None, index, False
            path = dot_path(parts)
            if path:
                completed.append((path, value))
            return value, end, True

        start_candidates = [
            index
            for index in (text.find("{"), text.find("["))
            if index >= 0
        ]
        if not start_candidates:
            return completed
        parse_value(min(start_candidates), [])
        return completed

    async def _emit_observed_boundary_events(
        self,
    ) -> AsyncGenerator[StreamingData, None]:
        raw_buffer = str(getattr(self.completer, "_buffer", "") or "")
        for path, value in self._observed_complete_values(raw_buffer):
            if path in self.field_completion_status:
                continue
            self.field_completion_status.add(path)
            yield StreamingData(
                path=path,
                value=value,
                delta=None,
                is_complete=True,
                event_type="done",
                full_data=self.current_data,
                completion_source="observed_boundary",
            )

    async def _get_value_at_path(self, data: dict, path_keys: List[str | int]) -> Any:
        """
        Retrieve the value at the specified path from a nested dictionary/list structure.
        Args:
            data (dict): The data to search within.
            path_keys (List[str | int]): Path as a list of keys and indices.
        Returns:
            Any: The value at the path, or None if not found.
        """
        current = data
        for key in path_keys:
            if isinstance(key, int):
                if isinstance(current, list) and len(current) > key:
                    current = current[key]
                else:
                    return None
            elif isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current

    async def _set_value_at_path(self, data: dict, path_keys: List[str | int], value: Any):
        """
        Set the value at the specified path in a nested dictionary/list structure.
        Args:
            data (dict): The data to modify.
            path_keys (List[str | int]): Path as a list of keys/indices.
            value (Any): The value to set.
        """
        current = data
        for i, key in enumerate(path_keys[:-1]):
            if isinstance(key, int):
                if not isinstance(current, list):
                    return
                while len(current) <= key:
                    current.append({})
                current = current[key]
            else:
                if not isinstance(current, dict):
                    return
                if key not in current:
                    # Decide whether to create dict or list based on next key
                    next_key = path_keys[i + 1]
                    current[key] = [] if isinstance(next_key, int) else {}
                current = current[key]

        # Set final value at the path
        final_key = path_keys[-1]
        if isinstance(final_key, int):
            if isinstance(current, list):
                while len(current) <= final_key:
                    current.append(None)
                current[final_key] = value
        else:
            if isinstance(current, dict):
                current[final_key] = value

    async def _parse_path_keys(self, path: str) -> List[str | int]:
        """
        Parse a dot-style path string into a list of keys and indices.
        Args:
            path (str): The dot-style path (e.g., 'foo.bar[0].baz').
        Returns:
            List[str|int]: List of keys and indices.
        """
        if not path:
            return []

        keys = []
        parts = path.split('.')

        for part in parts:
            if '[' in part and ']' in part:
                # Handle array index
                base_part = part.split('[')[0]
                if base_part:
                    keys.append(base_part)

                # Extract all array indices
                import re

                indices = re.findall(r'\[(\d+)\]', part)
                for idx in indices:
                    keys.append(int(idx))
            else:
                keys.append(part)

        return keys

    async def _get_current_parsing_paths(self) -> set[str]:
        """
        Get the set of all currently parsed paths in the current_data structure.
        Returns:
            set[str]: Set of dot-style paths currently present in current_data.
        """
        current_paths = set()

        async def collect_paths(data: Any, path_keys: List[str | int] = []):
            path = DataPathBuilder.build_dot_path(path_keys)
            if path:
                current_paths.add(path)

            if isinstance(data, dict):
                for key, value in data.items():
                    await collect_paths(value, path_keys + [key])
            elif isinstance(data, list):
                for i, item in enumerate(data):
                    await collect_paths(item, path_keys + [i])

        await collect_paths(self.current_data)
        return current_paths

    async def _should_mark_field_complete(self, path: str, current_value: Any, previous_value: Any) -> bool:
        """
        Determine whether a field/path should be marked as complete.
        Args:
            path (str): The field path.
            current_value (Any): The current value at this path.
            previous_value (Any): The previous value at this path.
        Returns:
            bool: True if the field should be marked as complete, False otherwise.
        """
        if path in self.field_completion_status:
            return False

        # Get all paths currently present in parsing
        current_parsing_paths = await self._get_current_parsing_paths()

        # Core logic: check the furthest path currently being parsed
        # If this path is before the furthest and value is stable, can mark as complete
        furthest_parsing_path = await self._get_furthest_parsing_path(current_parsing_paths)

        if furthest_parsing_path and await self._is_path_before(path, furthest_parsing_path):
            # This path is before the furthest one and value is stable
            if current_value == previous_value and current_value is not None:
                return True

        # A later array sibling cannot begin until the previous item delimiter
        # has actually appeared in the provider text. This is a trustworthy
        # boundary even though the streaming completer also closes the
        # currently open outer list/object for incremental parsing.
        if current_value == previous_value and current_value is not None:
            import re

            indexed_path = re.match(r"^(.*)\[(\d+)\](?:\.|$)", path)
            if indexed_path is not None:
                prefix = indexed_path.group(1)
                index = int(indexed_path.group(2))
                sibling_pattern = re.compile(
                    rf"^{re.escape(prefix)}\[(\d+)\](?:\.|$)"
                )
                for parsing_path in current_parsing_paths:
                    sibling = sibling_pattern.match(parsing_path)
                    if sibling is not None and int(sibling.group(1)) > index:
                        return True

        # Special case: leaf fields with stable value and newer paths
        if not isinstance(current_value, (dict, list)) and current_value is not None:
            for parsing_path in current_parsing_paths:
                if await self._is_path_before(path, parsing_path):
                    if current_value == previous_value:
                        return True
                    break

        return False

    async def _get_furthest_parsing_path(self, current_parsing_paths: set[str]) -> str | None:
        """
        Get the furthest (most advanced) path currently being parsed, according to expected order.
        Args:
            current_parsing_paths (set[str]): Set of paths currently present in the data.
        Returns:
            Optional[str]: The furthest path, or None if not found.
        """
        if not current_parsing_paths:
            return None

        # Find the last path being parsed according to expected order
        furthest_path = None
        furthest_index = -1

        for path in current_parsing_paths:
            try:
                index = self.expected_field_order.index(path)
                if index > furthest_index:
                    furthest_index = index
                    furthest_path = path
            except ValueError:
                # Path not in expected order (likely dynamic array)
                continue

        return furthest_path

    async def _is_path_before(self, path1: str, path2: str) -> bool:
        """
        Determine if path1 comes before path2 in the expected field order.
        Args:
            path1 (str): First path.
            path2 (str): Second path.
        Returns:
            bool: True if path1 is before path2, else False.
        """
        if not path2:
            return False

        try:
            index1 = self.expected_field_order.index(path1)
            index2 = self.expected_field_order.index(path2)
            return index1 < index2
        except ValueError:
            # special case: array path
            return await self._is_array_path_before(path1, path2)

    async def _is_array_path_before(self, path1: str, path2: str) -> bool:
        """
        Compare two array-type paths for order.
        Args:
            path1 (str): First path.
            path2 (str): Second path.
        Returns:
            bool: True if path1 is considered before path2.
        """
        # Simple string/length-based comparison; can be improved for more complex cases
        return len(path1) < len(path2) or path1 < path2

    async def _compare_and_generate_events(
        self,
        *,
        completion_source: Literal[
            "observed_boundary",
            "final_reconciliation",
            "synthetic_repair",
        ] = "observed_boundary",
    ) -> AsyncGenerator[StreamingData, None]:
        """
        Compare the current and previous data, and yield StreamingData events for
        incremental ("delta") updates and completions ("done").
        Yields:
            StreamingData: The event for each updated or completed field.
        """

        async def traverse_and_compare(current_data: Any, previous_data: Any, path_keys: List[str | int] = []):
            path = DataPathBuilder.build_dot_path(path_keys)

            # Handle incremental updates for string fields
            if isinstance(current_data, str):
                previous_str = previous_data if isinstance(previous_data, str) else ""

                # Emit delta if changed and computable
                if current_data != previous_str:
                    # Prefer suffix delta if previous_str is a prefix, else full current_data as delta
                    if previous_str and current_data.startswith(previous_str):
                        delta = current_data[len(previous_str) :]
                    else:
                        delta = current_data if previous_str == "" else current_data
                    if delta:
                        yield StreamingData(
                            path=path,
                            value=current_data,
                            delta=delta,
                            is_complete=False,
                            event_type="delta",
                            full_data=self.current_data,  # Pass the full current_data here
                        )
                        self.string_values[path] = current_data

                # Check if string field should be immediately marked complete
                if path and await self._should_mark_field_complete(path, current_data, previous_data):
                    self.field_completion_status.add(path)
                    yield StreamingData(
                        path=path,
                        value=current_data,
                        delta=None,
                        is_complete=True,
                        event_type="done",
                        full_data=self.current_data,  # Pass the full current_data here
                        completion_source=completion_source,
                    )

            # Handle primitive (non-string) types
            elif not isinstance(current_data, (dict, list)) and current_data is not None:
                # Emit delta if value changed and previous_data exists
                if current_data != previous_data:
                    yield StreamingData(
                        path=path,
                        value=current_data,
                        delta=str(current_data),
                        is_complete=False,
                        event_type="delta",
                        full_data=self.current_data,  # Pass the full current_data here
                    )
                # Check if primitive field should be immediately marked complete
                if path and await self._should_mark_field_complete(path, current_data, previous_data):
                    self.field_completion_status.add(path)
                    yield StreamingData(
                        path=path,
                        value=current_data,
                        delta=None,
                        is_complete=True,
                        event_type="done",
                        full_data=self.current_data,  # Pass the full current_data here
                        completion_source=completion_source,
                    )

            # Handle dictionary/object types
            elif isinstance(current_data, dict):
                previous_dict = previous_data if isinstance(previous_data, dict) else {}

                # Recursively process subfields
                for key, value in current_data.items():
                    child_path_keys = path_keys + [key]
                    previous_value = previous_dict.get(key)
                    async for event in traverse_and_compare(value, previous_value, child_path_keys):
                        yield event

                # Check if object should be marked complete
                if path and await self._should_mark_field_complete(path, current_data, previous_data):
                    self.field_completion_status.add(path)
                    yield StreamingData(
                        path=path,
                        value=current_data,
                        delta=None,
                        is_complete=True,
                        event_type="done",
                        full_data=self.current_data,  # Pass the full current_data here
                        completion_source=completion_source,
                    )

            # Handle list/array types
            elif isinstance(current_data, list):
                previous_list = previous_data if isinstance(previous_data, list) else []

                # Recursively process elements
                for i, value in enumerate(current_data):
                    child_path_keys = path_keys + [i]
                    previous_value = previous_list[i] if i < len(previous_list) else None
                    async for event in traverse_and_compare(value, previous_value, child_path_keys):
                        yield event

                # Check if array should be marked complete
                if path and await self._should_mark_field_complete(path, current_data, previous_data):
                    self.field_completion_status.add(path)
                    yield StreamingData(
                        path=path,
                        value=current_data,
                        delta=None,
                        is_complete=True,
                        event_type="done",
                        full_data=self.current_data,  # Pass the full current_data here
                        completion_source=completion_source,
                    )

        async for event in traverse_and_compare(self.current_data, self.previous_data):
            yield event

    async def _extract_array_index(self, path: str) -> int:
        """
        Extract the first array index from a dot-style path string.
        Args:
            path (str): The path string (e.g., 'foo.bar[2].baz').
        Returns:
            int: The first index found, or 0 if none.
        """
        import re

        match = re.search(r'\[(\d+)\]', path)
        return int(match.group(1)) if match else 0

    async def flush_final_data(
        self,
        final_data: Any,
        *,
        completion_source: Literal[
            "observed_boundary",
            "final_reconciliation",
            "synthetic_repair",
        ] | None = None,
    ) -> AsyncGenerator[StreamingData, None]:
        """
        Reconcile the parser state with a trusted final parsed result and emit any missing
        delta/done events needed to bring streaming consumers to the final state.

        Args:
            final_data (Any): The final parsed JSON-compatible data.

        Yields:
            StreamingData: Missing incremental or completion events required to catch up.
        """
        if final_data is None:
            return

        if completion_source is None:
            raw_buffer = str(getattr(self.completer, "_buffer", "") or "")
            completion_source = (
                "final_reconciliation"
                if self._looks_structurally_complete(raw_buffer)
                else "synthetic_repair"
            )

        if self.current_data != final_data:
            self.previous_data = copy.deepcopy(self.current_data)
            self.current_data = final_data
            async for event in self._compare_and_generate_events(
                completion_source=completion_source,
            ):
                yield event

        async for event in self.finalize(
            completion_source=completion_source,
        ):
            yield event

    async def finalize(
        self,
        *,
        completion_source: Literal[
            "observed_boundary",
            "final_reconciliation",
            "synthetic_repair",
        ] | None = None,
    ) -> AsyncGenerator[StreamingData, None]:
        """
        Mark all remaining fields as complete and yield "done" events for every incomplete path.
        This should be called at the end of the stream to ensure all fields are finalized.
        Yields:
            StreamingData: The completion event for each remaining field.
        """
        raw_buffer = str(getattr(self.completer, "_buffer", "") or "")
        structurally_complete = self._looks_structurally_complete(raw_buffer)
        if completion_source is None:
            completion_source = (
                "final_reconciliation"
                if structurally_complete
                else "synthetic_repair"
            )
        if raw_buffer and (not self.current_data or structurally_complete):
            self._parse_buffer_once()

        async for event in self._emit_observed_boundary_events():
            yield event

        async def mark_all_complete(data: Any, path_keys: List[str | int] = []):
            path = DataPathBuilder.build_dot_path(path_keys)

            if isinstance(data, dict):
                # Recursively mark all subfields
                for key, value in data.items():
                    child_path_keys = path_keys + [key]
                    async for event in mark_all_complete(value, child_path_keys):
                        yield event

                # Mark this object field as complete if not already done
                if path and path not in self.field_completion_status:
                    self.field_completion_status.add(path)
                    yield StreamingData(
                        path=path,
                        value=data,
                        delta=None,
                        is_complete=True,
                        event_type="done",
                        full_data=self.current_data,  # Pass the full current_data here
                        completion_source=completion_source,
                    )

            elif isinstance(data, list):
                # Recursively mark all elements
                for i, item in enumerate(data):
                    child_path_keys = path_keys + [i]
                    async for event in mark_all_complete(item, child_path_keys):
                        yield event

                # Mark this array field as complete if not already done
                if path and path not in self.field_completion_status:
                    self.field_completion_status.add(path)
                    yield StreamingData(
                        path=path,
                        value=data,
                        delta=None,
                        is_complete=True,
                        event_type="done",
                        full_data=self.current_data,  # Pass the full current_data here
                        completion_source=completion_source,
                    )

            else:
                # Mark primitive field as complete if not already done
                if path and path not in self.field_completion_status:
                    self.field_completion_status.add(path)
                    yield StreamingData(
                        path=path,
                        value=data,
                        delta=None,
                        is_complete=True,
                        event_type="done",
                        full_data=self.current_data,  # Pass the full current_data here
                        completion_source=completion_source,
                    )

        async for event in mark_all_complete(self.current_data):
            yield event

    async def flush_observed_boundaries(
        self,
    ) -> AsyncGenerator[StreamingData, None]:
        """Emit only values closed by delimiters already present in raw bytes."""
        async for event in self._emit_observed_boundary_events():
            yield event

    async def parse_chunk(self, chunk: str) -> AsyncGenerator[StreamingData, None]:
        """
        Parse a single chunk of streamed JSON data and yield StreamingData events for any
        detected incremental or completion updates.
        Args:
            chunk (str): A chunk of JSON text (possibly incomplete).
        Yields:
            StreamingData: The event for each detected update or completion.
        """
        self.completer.append(chunk)
        if self._should_skip_large_incremental_parse():
            if not self._large_incremental_parse_deferred:
                yield self._large_incremental_parse_status()
            return
        parsed = self._parse_buffer_once()
        if parsed:
            async for event in self._compare_and_generate_events():
                yield event
        async for event in self._emit_observed_boundary_events():
            yield event

    async def parse_stream(self, chunk_stream: AsyncGenerator[str, None]) -> AsyncGenerator[StreamingData, None]:
        """
        Parse a stream of JSON chunks and yield StreamingData events.
        Args:
            chunk_stream (AsyncGenerator[str, None]): An async generator that yields JSON chunks.
        Yields:
            StreamingData: The event for each detected update or completion.
        """
        async for chunk in chunk_stream:
            async for event in self.parse_chunk(chunk):
                yield event

        # Finalize at the end of the stream
        async for event in self.finalize():
            yield event
