import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from agently import Agently
from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.model.Prompt import Prompt
from agently.utils import Settings
from agently.builtins.plugins.ModelRequester.AnthropicCompatible import (
    AnthropicCompatible,
)
from agently.types.plugins import ModelRequester
import agently.builtins.plugins.ModelRequester.AnthropicCompatible as anthropic_module


def build_plugin(config: dict, prompt_values: dict | None = None):
    settings = Settings(parent=Agently.settings)
    settings.update({"plugins": {"ModelRequester": {"AnthropicCompatible": config}}})
    prompt = Prompt(plugin_manager=Agently.plugin_manager, parent_settings=settings)
    for key, value in (prompt_values or {}).items():
        prompt.set(key, value)
    return AnthropicCompatible(prompt, settings)


def generate_request(config: dict, prompt_values: dict | None = None):
    return build_plugin(config, prompt_values).generate_request_data().model_dump()


async def capture_request_headers(monkeypatch: pytest.MonkeyPatch, config: dict, prompt_values: dict | None = None):
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        content = b'{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet-4-20250514","content":[{"type":"text","text":"hello"}],"stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1}}'
        text = content.decode()
        headers = {"Content-Type": "application/json"}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.headers = {}
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = dict(self.headers if headers is None else headers)
            return FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(anthropic_module, "AsyncClient", FakeAsyncClient)
    plugin = build_plugin(config, prompt_values)
    request_data = plugin.generate_request_data()
    async for _event, _payload in plugin.request_model(request_data):
        pass
    return captured


def collect_events(plugin: AnthropicCompatible, request_events: list[tuple[str, Any]]):
    async def _run():
        async def generator():
            for event, payload in request_events:
                yield event, payload

        collected = []
        async for event, data in plugin.broadcast_response(generator()):
            collected.append((event, data))
        return collected

    return asyncio.run(_run())


@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        ("https://api.anthropic.example", "https://api.anthropic.example/messages"),
        ("https://api.anthropic.example/v1", "https://api.anthropic.example/v1/messages"),
    ],
)
def test_generate_request_uses_messages_path_and_default_model(base_url: str, expected_url: str):
    request = generate_request(
        {
            "base_url": base_url,
        },
        {"input": "hello"},
    )

    assert request["request_url"] == expected_url
    assert request["request_options"]["model"] == "claude-sonnet-4-20250514"
    assert request["request_options"]["stream"] is True
    assert request["request_options"]["max_tokens"] == 8192
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    assert request["data"]["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize(
    ("base_url", "full_url"),
    [
        ("https://api.minimax.io/anthropic", "https://api.minimax.io/anthropic/v1/messages"),
        ("https://api.minimaxi.com/anthropic", "https://api.minimaxi.com/anthropic/v1/messages"),
    ],
)
def test_generate_request_uses_minimax_full_url(base_url: str, full_url: str):
    request = generate_request(
        {
            "base_url": base_url,
            "full_url": full_url,
        },
        {"input": "hello"},
    )

    assert request["request_url"] == full_url


def test_client_options_disable_environment_proxy_by_default():
    request = generate_request({"base_url": "https://api.anthropic.example/v1"}, {"input": "hello"})

    assert request["client_options"]["trust_env"] is False


def test_client_options_can_enable_environment_proxy_explicitly():
    request = generate_request(
        {
            "base_url": "https://api.anthropic.example/v1",
            "client_options": {"trust_env": True},
        },
        {"input": "hello"},
    )

    assert request["client_options"]["trust_env"] is True


def test_inherits_model_requester_protocol_instead_of_responses_plugin():
    assert ModelRequester in AnthropicCompatible.__mro__


def test_generate_request_maps_system_and_rich_content():
    request = generate_request(
        {
            "base_url": "https://api.anthropic.example/v1",
        },
        {
            "instruct": "Be concise.",
            "input": "Describe this image.",
            "attachment": [{"type": "image_url", "image_url": "https://example.com/cat.png"}],
        },
    )

    assert "system" not in request["data"]
    assert request["data"]["messages"][0]["role"] == "user"
    content = request["data"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "[INSTRUCT]:" in content[0]["text"]
    assert "Be concise." in content[0]["text"]
    assert "Describe this image." in content[0]["text"]
    assert content[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.com/cat.png"},
    }


def test_generate_request_maps_data_url_image_to_base64_source():
    request = generate_request(
        {
            "base_url": "https://api.anthropic.example/v1",
        },
        {
            "attachment": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                }
            ],
        },
    )

    content = request["data"]["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "aGVsbG8=",
        },
    }


def test_prompt_tools_are_converted_and_explicit_tools_override_by_name():
    request = generate_request(
        {
            "base_url": "https://api.anthropic.example/v1",
            "request_options": {
                "tools": [
                    {
                        "name": "search_docs",
                        "description": "explicit override",
                        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
                    }
                ]
            },
        },
        {
            "input": "Find docs.",
            "tools": [
                {
                    "name": "search_docs",
                    "desc": "Search the documentation.",
                    "kwargs": {"query": (str, "query text"), "limit": (int, "max result count")},
                },
                {
                    "name": "lookup_issue",
                    "desc": "Lookup a GitHub issue.",
                    "kwargs": {"issue_id": (str, "Issue id")},
                },
            ],
        },
    )

    tools = request["request_options"]["tools"]
    search_docs = next(tool for tool in tools if tool.get("name") == "search_docs")
    lookup_issue = next(tool for tool in tools if tool.get("name") == "lookup_issue")

    assert search_docs["description"] == "explicit override"
    assert lookup_issue["input_schema"]["properties"]["issue_id"]["type"] == "string"
    assert lookup_issue["input_schema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_auth_headers_are_preserved_in_outgoing_request(monkeypatch: pytest.MonkeyPatch):
    captured = await capture_request_headers(
        monkeypatch,
        {
            "base_url": "https://api.anthropic.example",
            "model": "claude-sonnet-4-20250514",
            "stream": False,
            "auth": {"headers": {"X-Test": "1"}, "api_key": "claude-secret"},
            "anthropic_beta": ["tools-2024-04-04"],
        },
        {"input": "hello"},
    )

    assert captured["url"] == "https://api.anthropic.example/messages"
    assert captured["headers"]["x-api-key"] == "claude-secret"
    assert captured["headers"]["X-Test"] == "1"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["anthropic-beta"] == "tools-2024-04-04"


@pytest.mark.asyncio
async def test_stream_idle_timeout_returns_timeout_error_event(monkeypatch: pytest.MonkeyPatch):
    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aclose(self):
            return None

    async def fake_aiter_sse_with_retry(self, client, method, url, *, headers, json):
        del self, client, method, url, headers, json

        async def generator():
            yield SimpleNamespace(
                event="content_block_delta",
                data='{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}',
            )
            await asyncio.sleep(0.05)
            yield SimpleNamespace(
                event="content_block_delta",
                data='{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"late"}}',
            )

        return generator()

    monkeypatch.setattr(anthropic_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(AnthropicCompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.anthropic.example/v1",
            "model": "claude-sonnet-4-20250514",
            "stream": True,
            "stream_idle_timeout": 0.01,
            "timeout": {"connect": 1.0, "read": 9.0, "write": 2.0, "pool": 3.0},
        },
        {"input": "hello"},
    )

    events = []
    async for event, payload in plugin.request_model(plugin.generate_request_data()):
        events.append((event, payload))

    assert len(events) == 3
    assert events[0][0] == "content_block_delta"
    assert events[1][0] == "status"
    assert events[1][1]["status"] == "failed"
    assert events[1][1]["retry"] is False
    assert events[1][1]["reason"] == "Stream idle timeout after 0.01 seconds."
    assert events[2][0] == "error"
    assert isinstance(events[2][1], RuntimeStageStallError)
    assert events[2][1].stage == "response_stream"


@pytest.mark.asyncio
async def test_non_stream_response_idle_timeout_returns_stall_error(monkeypatch: pytest.MonkeyPatch):
    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None):
            del url, json, headers
            await asyncio.sleep(10)
            raise AssertionError("post should have been cancelled by the idle deadline")

    monkeypatch.setattr(anthropic_module, "AsyncClient", FakeAsyncClient)

    plugin = build_plugin(
        {
            "base_url": "https://api.anthropic.example/v1",
            "model": "claude-sonnet-4-20250514",
            "stream": False,
            "stream_idle_timeout": 0.01,
        },
        {"input": "hello"},
    )

    events = []
    async for event, payload in plugin.request_model(plugin.generate_request_data()):
        events.append((event, payload))

    statuses = [payload for event, payload in events if event == "status"]
    assert statuses
    assert statuses[-1]["status"] == "failed"
    assert any(
        "Non-streaming response made no progress before idle deadline" in str(payload.get("reason") or "")
        for payload in statuses
    )
    assert events[-1][0] == "error"
    assert isinstance(events[-1][1], RuntimeStageStallError)
    assert events[-1][1].stage == "response_materialization"
    assert events[-1][1].timeout_seconds == 0.01
    assert "stream_idle_timeout=0.01" in str(events[-1][1])


@pytest.mark.asyncio
async def test_non_stream_response_idle_timeout_allows_api_key_failover(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code: int, content: bytes):
            self.status_code = status_code
            self.content = content
            self.text = content.decode()
            self.headers = {"Content-Type": "application/json"}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None):
            del url, json
            calls.append(dict(self.headers if headers is None else headers))
            if len(calls) == 1:
                await asyncio.sleep(10)
                raise AssertionError("first post should have been cancelled by the idle deadline")
            return FakeResponse(
                200,
                b'{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet-4-20250514","content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1}}',
            )

    monkeypatch.setattr(anthropic_module, "AsyncClient", FakeAsyncClient)

    plugin = build_plugin(
        {
            "base_url": "https://api.anthropic.example/v1",
            "model": "claude-sonnet-4-20250514",
            "api_key": "key-a",
            "stream": False,
            "stream_idle_timeout": 0.01,
            "_api_key_pool_runtime": {
                "pool_id": "example",
                "failover": {"handler": lambda _context: "try_next", "max_attempts": 2},
                "keys": [
                    {"id": "a", "value": "key-a", "index": 0},
                    {"id": "b", "value": "key-b", "index": 1},
                ],
                "selected_key_id": "a",
                "attempts": [{"key_id": "a", "action": "initial"}],
            },
        },
        {"input": "hello"},
    )

    events = []
    async for event, payload in plugin.request_model(plugin.generate_request_data()):
        events.append((event, payload))

    assert [headers.get("x-api-key") for headers in calls] == ["key-a", "key-b"]
    assert events == [
        (
            "message",
            '{"id":"msg_1","type":"message","role":"assistant","model":"claude-sonnet-4-20250514","content":[{"type":"text","text":"ok"}],"stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1}}',
        ),
        ("status", {"status": "completed", "attempt_index": 1, "retry": False}),
    ]


def test_broadcast_response_maps_text_stream_and_meta():
    plugin = build_plugin({"base_url": "https://api.anthropic.example/v1"}, {"input": "hello"})
    events = collect_events(
        plugin,
        [
            (
                "message_start",
                json.dumps(
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_1",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-sonnet-4-20250514",
                            "content": [],
                            "usage": {"input_tokens": 1, "output_tokens": 0},
                        },
                    }
                ),
            ),
            ("content_block_start", json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})),
            ("content_block_delta", json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}})),
            ("content_block_delta", json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world"}})),
            ("content_block_stop", json.dumps({"type": "content_block_stop", "index": 0})),
            (
                "message_delta",
                json.dumps(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                    }
                ),
            ),
            ("message_stop", json.dumps({"type": "message_stop"})),
        ],
    )

    assert ("delta", "Hello ") in events
    assert ("delta", "world") in events
    assert ("done", "Hello world") in events
    assert ("reasoning_done", "") in events
    meta = next(data for event, data in events if event == "meta")
    assert meta["id"] == "msg_1"
    assert meta["finish_reason"] == "stop"
    assert meta["usage"]["output_tokens"] == 2


def test_broadcast_response_preserves_core_status_record():
    plugin = build_plugin({"base_url": "https://api.anthropic.example/v1"}, {"input": "hello"})

    events = collect_events(
        plugin,
        [("status", {"status": "failed", "attempt_index": 1, "retry": True})],
    )

    assert events == [("status", {"status": "failed", "attempt_index": 1, "retry": True})]


def test_broadcast_response_resets_attempt_text_after_retry_status():
    plugin = build_plugin({"base_url": "https://api.anthropic.example/v1"}, {"input": "hello"})

    events = collect_events(
        plugin,
        [
            (
                "content_block_start",
                json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ),
            (
                "content_block_delta",
                json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}}),
            ),
            (
                "status",
                {
                    "status": "failed",
                    "attempt_index": 1,
                    "retry": True,
                    "next_attempt_index": 2,
                    "reason": "server disconnected",
                },
            ),
            (
                "content_block_start",
                json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ),
            (
                "content_block_delta",
                json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "replacement"}}),
            ),
            ("content_block_stop", json.dumps({"type": "content_block_stop", "index": 0})),
            ("message_stop", json.dumps({"type": "message_stop"})),
        ],
    )

    assert [payload for event, payload in events if event == "delta"] == ["partial", "replacement"]
    assert ("done", "replacement") in events
    assert ("done", "partialreplacement") not in events


def test_broadcast_response_maps_tool_use_to_tool_calls():
    plugin = build_plugin({"base_url": "https://api.anthropic.example/v1"}, {"input": "Find docs"})
    events = collect_events(
        plugin,
        [
            (
                "message_start",
                json.dumps(
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_2",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-sonnet-4-20250514",
                            "content": [],
                            "usage": {"input_tokens": 3, "output_tokens": 0},
                        },
                    }
                ),
            ),
            (
                "content_block_start",
                json.dumps(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search_docs", "input": {}},
                    }
                ),
            ),
            (
                "content_block_delta",
                json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": "{\"query\":\"anthropic\"}"},
                    }
                ),
            ),
            ("content_block_stop", json.dumps({"type": "content_block_stop", "index": 0})),
            (
                "message_delta",
                json.dumps(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"input_tokens": 3, "output_tokens": 4},
                    }
                ),
            ),
            ("message_stop", json.dumps({"type": "message_stop"})),
        ],
    )

    tool_call_event = next(data for event, data in events if event == "tool_calls")
    assert tool_call_event["id"] == "toolu_1"
    assert tool_call_event["function"]["name"] == "search_docs"
    assert tool_call_event["function"]["arguments"] == "{\"query\":\"anthropic\"}"
    meta = next(data for event, data in events if event == "meta")
    assert meta["finish_reason"] == "tool_calls"
