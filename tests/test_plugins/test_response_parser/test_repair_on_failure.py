import asyncio
import importlib
from typing import Any, cast

import pytest

import agently.base

from agently.builtins.plugins.ResponseParser.AgentlyResponseParser import AgentlyResponseParser
from agently.types.data.prompt import PromptModel
from agently.utils import DataLocator, Settings


class DummyPrompt:
    def __init__(self, output):
        self._prompt_object = PromptModel(output=output, output_format="json")

    def to_prompt_object(self):
        return self._prompt_object

    def to_output_model(self):
        return None


async def _noop_async_emit_runtime(_event):
    return None


def create_response_parser(events, output_schema):
    async def response_generator():
        for event, data in events:
            yield event, data

    return AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-1",
        prompt=cast(Any, DummyPrompt(output_schema)),
        response_generator=response_generator(),
        settings=Settings(),
    )


@pytest.mark.asyncio
async def test_response_parser_only_repairs_after_parse_failure(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    repair_calls = 0
    original_repair = DataLocator.repair_json_fragment

    def track_repair_calls(text: str) -> str:
        nonlocal repair_calls
        repair_calls += 1
        return original_repair(text)

    monkeypatch.setattr(DataLocator, "repair_json_fragment", track_repair_calls)

    parser = create_response_parser(
        [("done", '{"name": "Alice", "quote": "她说：“你好”"}')],
        {"name": None, "quote": None},
    )

    parsed = await parser.async_get_data()

    assert parsed["name"] == "Alice"
    assert parsed["quote"] == "她说：“你好”"
    assert repair_calls == 0


@pytest.mark.asyncio
async def test_response_parser_repairs_structural_quotes_and_preserves_value_quotes(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    parser = create_response_parser(
        [("done", '{“quote”: "她说：“你好”", “name”: "Alice"}')],
        {"quote": None, "name": None},
    )

    parsed = await parser.async_get_data()
    result = await parser.async_get_data(type="all")

    assert parsed["quote"] == "她说：“你好”"
    assert parsed["name"] == "Alice"
    assert result["cleaned_result"] is not None
    assert '“你好”' in result["cleaned_result"]
    assert '"quote"' in result["cleaned_result"]
    assert '"name"' in result["cleaned_result"]


@pytest.mark.asyncio
async def test_response_parser_error_event_fails_text_materialization(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    provider_error = ValueError("provider stream failed")
    parser = create_response_parser(
        [
            ("delta", "partial text"),
            ("error", provider_error),
        ],
        {"name": None},
    )

    with pytest.raises(ValueError, match="provider stream failed") as raised:
        await asyncio.wait_for(parser.async_get_text(), timeout=1)

    assert raised.value is provider_error
    assert parser.full_result_data["errors"] == [provider_error]
    observations = parser.drain_runtime_observations()
    failed_observations = [item for item in observations if item["kind"] == "failed"]
    assert failed_observations
    assert failed_observations[0]["error"] is provider_error


@pytest.mark.asyncio
async def test_response_parser_string_error_event_fails_text_materialization(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    parser = create_response_parser(
        [
            ("delta", "partial text"),
            ("error", "upstream action loop failed"),
        ],
        {"name": None},
    )

    with pytest.raises(RuntimeError, match="upstream action loop failed") as raised:
        await asyncio.wait_for(parser.async_get_text(), timeout=1)

    assert parser.full_result_data["errors"] == [raised.value]


@pytest.mark.asyncio
async def test_instant_async_generator_streams_valid_json_before_done(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    release_done = asyncio.Event()

    async def response_generator():
        yield "delta", '{"name": "Ali'
        await release_done.wait()
        yield "done", '{"name": "Alice"}'

    parser = AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-2",
        prompt=cast(Any, DummyPrompt({"name": None})),
        response_generator=response_generator(),
        settings=Settings(),
    )

    generator = parser.get_async_generator(type="instant")
    first_event = await asyncio.wait_for(anext(generator), timeout=1)

    assert first_event.path == "name"
    assert first_event.event_type == "delta"
    assert first_event.value == "Ali"
    assert first_event.delta == "Ali"

    release_done.set()

    second_event = await asyncio.wait_for(anext(generator), timeout=1)
    third_event = await asyncio.wait_for(anext(generator), timeout=1)

    assert second_event.path == "name"
    assert second_event.event_type == "delta"
    assert second_event.value == "Alice"
    assert second_event.delta == "ce"

    assert third_event.path == "name"
    assert third_event.event_type == "done"
    assert third_event.value == "Alice"

    with pytest.raises(StopAsyncIteration):
        await anext(generator)


@pytest.mark.asyncio
async def test_instant_async_generator_defers_large_json_delta_by_default(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    parser_module = importlib.import_module("agently.utils.StreamingJSONParser")

    def fail_if_called(_value):
        raise AssertionError("large JSON deltas should not be parsed incrementally by default")

    monkeypatch.setattr(parser_module.json5, "loads", fail_if_called)

    async def response_generator():
        yield "delta", '{"report": "' + ("x" * 20000)

    parser = AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-large-json-delta",
        prompt=cast(Any, DummyPrompt({"report": None})),
        response_generator=response_generator(),
        settings=Settings(),
    )

    generator = parser.get_async_generator(type="instant")
    first_event = await asyncio.wait_for(anext(generator), timeout=1)

    assert first_event.path == "$status"
    assert first_event.value["status"] == "streaming_parse_deferred"
    assert first_event.value["reason"] == "large_json_incremental_parse"

    await generator.aclose()


@pytest.mark.asyncio
async def test_instant_async_generator_defers_large_json_delta_when_setting_is_none(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    parser_module = importlib.import_module("agently.utils.StreamingJSONParser")

    def fail_if_called(_value):
        raise AssertionError("None settings should keep the default large-delta guard")

    monkeypatch.setattr(parser_module.json5, "loads", fail_if_called)

    async def response_generator():
        yield "delta", '{"report": "' + ("x" * 20000)

    settings = Settings()
    settings.set("response.streaming_parse_max_incomplete_chars", None)
    parser = AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-large-json-delta-none-setting",
        prompt=cast(Any, DummyPrompt({"report": None})),
        response_generator=response_generator(),
        settings=settings,
    )

    generator = parser.get_async_generator(type="instant")
    first_event = await asyncio.wait_for(anext(generator), timeout=1)

    assert first_event.path == "$status"
    assert first_event.value["status"] == "streaming_parse_deferred"

    await generator.aclose()


@pytest.mark.asyncio
async def test_instant_large_complete_json_keeps_authoritative_trailing_false(
    monkeypatch,
):
    monkeypatch.setattr(
        agently.base,
        "async_emit_runtime",
        _noop_async_emit_runtime,
    )
    payload = "x" * 1600
    first_chunk = '{"payload":"' + payload[:500]
    second_chunk = payload[500:] + '","is_final":false}'
    complete_text = first_chunk + second_chunk

    parser = create_response_parser(
        [
            ("delta", first_chunk),
            ("delta", second_chunk),
            ("done", complete_text),
        ],
        {"payload": None, "is_final": None},
    )

    events = []
    async for event in parser.get_async_generator(type="instant"):
        events.append(event)
    parsed = await parser.async_get_data()

    assert any(
        event.path == "$status"
        and event.value["status"] == "streaming_parse_deferred"
        for event in events
    )
    assert parsed == {"payload": payload, "is_final": False}


@pytest.mark.asyncio
async def test_instant_async_generator_waits_until_done_before_repair(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    release_done = asyncio.Event()

    async def response_generator():
        yield "delta", '{“name”: “Ali'
        await release_done.wait()
        yield "done", '{“name”: “Alice”}'

    parser = AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-3",
        prompt=cast(Any, DummyPrompt({"name": None})),
        response_generator=response_generator(),
        settings=Settings(),
    )

    generator = parser.get_async_generator(type="instant")
    first_event_task = asyncio.create_task(anext(generator))

    await asyncio.sleep(0.05)
    assert not first_event_task.done()

    release_done.set()

    first_event = await asyncio.wait_for(first_event_task, timeout=1)
    second_event = await asyncio.wait_for(anext(generator), timeout=1)

    assert first_event.path == "name"
    assert first_event.event_type == "delta"
    assert first_event.value == "Alice"
    assert first_event.delta == "Alice"

    assert second_event.path == "name"
    assert second_event.event_type == "done"
    assert second_event.value == "Alice"

    with pytest.raises(StopAsyncIteration):
        await anext(generator)


@pytest.mark.asyncio
async def test_instant_async_generator_does_not_emit_done_when_final_parse_fails(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    async def response_generator():
        yield "delta", '{"name": "Ali'
        yield "done", '{"name": "Alice" "age": 1}'

    parser = AgentlyResponseParser(
        agent_name="test-agent",
        response_id="resp-4",
        prompt=cast(Any, DummyPrompt({"name": None, "age": None})),
        response_generator=response_generator(),
        settings=Settings(),
    )

    events = []
    async for event in parser.get_async_generator(type="instant"):
        events.append((event.path, event.event_type, event.value, event.delta))

    assert events == [("name", "delta", "Ali", "Ali")]
    assert await parser.async_get_data() is None


@pytest.mark.asyncio
async def test_instant_multiple_consumers_share_single_final_parse(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    parser = create_response_parser(
        [
            ("delta", '{"name": "Ali'),
            ("done", '{"name": "Alice"}'),
        ],
        {"name": None},
    )

    parse_calls = 0
    original_parse = parser._parse_json_output

    def track_parse_calls(text: str):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(text)

    monkeypatch.setattr(parser, "_parse_json_output", track_parse_calls)

    first_events = []
    second_events = []

    async for event in parser.get_async_generator(type="instant"):
        first_events.append((event.path, event.event_type, event.value, event.delta))

    async for event in parser.get_async_generator(type="instant"):
        second_events.append((event.path, event.event_type, event.value, event.delta))

    assert parse_calls == 1
    assert first_events == second_events


@pytest.mark.asyncio
async def test_instant_primitive_delta_is_string(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    parser = create_response_parser(
        [
            ("delta", '{"ready": tru'),
            ("done", '{"ready": true}'),
        ],
        {"ready": None},
    )

    events = []
    async for event in parser.get_async_generator(type="instant"):
        events.append((event.path, event.event_type, event.value, event.delta))

    assert events[0] == ("ready", "delta", True, "True")


def test_instant_sync_generator_replays_final_result(monkeypatch):
    monkeypatch.setattr(agently.base, "async_emit_runtime", _noop_async_emit_runtime)

    parser = create_response_parser(
        [
            ("delta", '{"name": "Ali'),
            ("done", '{"name": "Alice"}'),
        ],
        {"name": None},
    )

    events = list(parser.get_generator(type="instant"))

    assert [event.event_type for event in events] == ["delta", "delta", "done"]
    assert [event.path for event in events] == ["name", "name", "name"]
    assert events[0].value == "Ali"
    assert events[1].value == "Alice"
    assert events[1].delta == "ce"
    assert events[2].value == "Alice"
