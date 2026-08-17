import asyncio
import json
from typing import Any

import pytest
from httpx import ReadError
from httpx_sse import SSEError

from types import SimpleNamespace

from agently import Agently
from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.model.Prompt import Prompt
from agently.utils import Settings
from agently.builtins.plugins.ModelRequester.OpenAIResponsesCompatible import (
    OpenAIResponsesCompatible,
)
from agently.types.plugins import ModelRequester
import agently.builtins.plugins.ModelRequester.OpenAIResponsesCompatible as responses_module


def build_plugin(config: dict, prompt_values: dict | None = None):
    settings = Settings(parent=Agently.settings)
    settings.update({"plugins": {"ModelRequester": {"OpenAIResponsesCompatible": config}}})
    prompt = Prompt(plugin_manager=Agently.plugin_manager, parent_settings=settings)
    for key, value in (prompt_values or {}).items():
        prompt.set(key, value)
    return OpenAIResponsesCompatible(prompt, settings)


def generate_request(config: dict, prompt_values: dict | None = None):
    return build_plugin(config, prompt_values).generate_request_data().model_dump()


@pytest.mark.asyncio
async def test_request_retry_uses_public_attempt_boundary(monkeypatch):
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "request_retry": {"max_attempts": 2, "after_output": True},
        },
        {"input": "hello"},
    )
    calls = 0

    async def fake_legacy(_request_data):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "response.output_text.delta", '{"delta":"partial"}'
            yield "error", ReadError("responses SSE disconnected")
            return
        yield "response.completed", '{"response":{"status":"completed"}}'

    monkeypatch.setattr(plugin, "_request_model_legacy", fake_legacy)
    events = [
        item
        async for item in plugin.request_model(plugin.generate_request_data())
    ]

    assert calls == 2
    assert events[1] == (
        "status",
        {
            "status": "failed",
            "attempt_index": 1,
            "retry": True,
            "next_attempt_index": 2,
            "reason": "responses SSE disconnected",
            "error_type": "ReadError",
        },
    )
    assert events[-1] == (
        "status",
        {"status": "completed", "attempt_index": 2, "retry": False},
    )


async def capture_request_headers(monkeypatch: pytest.MonkeyPatch, config: dict, prompt_values: dict | None = None):
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        content = b'{"id":"resp_1","object":"response","status":"completed","output":[]}'
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

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)
    plugin = build_plugin(config, prompt_values)
    request_data = plugin.generate_request_data()
    async for _event, _payload in plugin.request_model(request_data):
        pass
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_http_error_message_excludes_request_body(monkeypatch: pytest.MonkeyPatch, stream: bool):
    class FakeResponse:
        status_code = 500
        content = b'{"error":{"message":"upstream failed"}}'
        text = content.decode()
        headers = {"Content-Type": "application/json"}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json=None, headers=None):
            return FakeResponse()

    async def fail_sse(*args, **kwargs):
        raise SSEError("provider returned non-SSE response")

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)
    if stream:
        monkeypatch.setattr(OpenAIResponsesCompatible, "_aiter_sse_with_retry", fail_sse)
    request_marker = f"private-responses-{'stream-' if stream else ''}request-marker"
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": stream,
            "request_retry": {"max_attempts": 1},
        },
        {"input": request_marker},
    )

    events = [item async for item in plugin.request_model(plugin.generate_request_data())]
    error = next(payload for event, payload in events if event == "error")
    message = str(error)

    assert "Status Code: 500" in message
    assert "upstream failed" in message
    assert "Request Data:" not in message
    assert request_marker not in message


def collect_events(plugin: OpenAIResponsesCompatible, request_events: list[tuple[str, Any]]):
    async def _run():
        async def generator():
            for event, payload in request_events:
                yield event, payload

        collected = []
        async for event, data in plugin.broadcast_response(generator()):
            collected.append((event, data))
        return collected

    return asyncio.run(_run())


def test_friendly_settings_path_aliases_are_declared():
    # Parity with OpenAICompatible (OpenAI / OAIClient) and AnthropicCompatible
    # (Anthropic / Claude): the Responses requester exposes short settings aliases.
    mappings = OpenAIResponsesCompatible.DEFAULT_SETTINGS["$mappings"]["path_mappings"]
    target = "plugins.ModelRequester.OpenAIResponsesCompatible"
    assert mappings["OpenAIResponsesCompatible"] == target
    assert mappings["OpenAIResponses"] == target
    assert mappings["Responses"] == target


def test_generate_request_uses_responses_path_and_latest_default_model():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
        },
        {"input": "hello"},
    )

    assert request["request_url"] == "https://api.example.com/v1/responses"
    assert request["request_options"]["model"] == "gpt-5.5"
    assert request["request_options"]["stream"] is True
    assert request["data"]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]


def test_client_options_disable_environment_proxy_by_default():
    request = generate_request({"base_url": "https://api.example.com/v1"}, {"input": "hello"})

    assert request["client_options"]["trust_env"] is False


def test_client_options_can_enable_environment_proxy_explicitly():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
            "client_options": {"trust_env": True},
        },
        {"input": "hello"},
    )

    assert request["client_options"]["trust_env"] is True


def test_inherits_model_requester_protocol_instead_of_openai_plugin():
    assert ModelRequester in OpenAIResponsesCompatible.__mro__


def test_generate_request_maps_rich_content_and_preserves_instructions():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
            "request_options": {"instructions": "Be concise."},
        },
        {
            "input": "Describe this file.",
            "attachment": [
                {"type": "text", "text": "extra text"},
                {"type": "image_url", "image_url": "https://example.com/cat.png"},
                {"type": "input_file", "file_id": "file_123"},
            ],
        },
    )

    assert request["request_options"]["instructions"] == "Be concise."
    content = request["data"]["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert "Describe this file." in content[0]["text"]
    assert content[1] == {"type": "input_text", "text": "extra text"}
    assert content[2] == {"type": "input_image", "image_url": "https://example.com/cat.png"}
    assert content[3] == {"type": "input_file", "file_id": "file_123"}


def test_generate_request_preserves_responses_reasoning_options():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
            "request_options": {
                "reasoning": {
                    "effort": "low",
                    "summary": "auto",
                },
            },
        },
        {"input": "hello"},
    )

    assert request["request_options"]["reasoning"] == {
        "effort": "low",
        "summary": "auto",
    }


def test_prompt_tools_are_converted_and_explicit_tools_override_by_name():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
            "request_options": {
                "tools": [
                    {
                        "type": "function",
                        "name": "search_docs",
                        "description": "explicit override",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                        "strict": True,
                    },
                    {
                        "type": "web_search_preview",
                    },
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
                    "required_input_keys": ["issue_id"],
                },
            ],
        },
    )

    tools = request["request_options"]["tools"]
    search_docs = next(tool for tool in tools if tool.get("name") == "search_docs")
    lookup_issue = next(tool for tool in tools if tool.get("name") == "lookup_issue")
    web_search = next(tool for tool in tools if tool.get("type") == "web_search_preview")

    assert search_docs["description"] == "explicit override"
    assert search_docs["strict"] is True
    assert lookup_issue["strict"] is False
    assert lookup_issue["parameters"]["properties"]["issue_id"]["type"] == "string"
    assert lookup_issue["parameters"]["required"] == ["issue_id"]
    assert lookup_issue["parameters"]["additionalProperties"] is False
    assert web_search == {"type": "web_search_preview"}


@pytest.mark.asyncio
async def test_auth_headers_are_preserved_in_outgoing_request(monkeypatch: pytest.MonkeyPatch):
    captured = await capture_request_headers(
        monkeypatch,
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": False,
            "api_key": "KEY1",
            "auth": {"headers": {"Authorization": "Custom ABC", "X-Test": "1"}},
        },
        {"input": "hello"},
    )

    assert captured["headers"]["Authorization"] == "Custom ABC"
    assert captured["headers"]["X-Test"] == "1"


def test_broadcast_response_maps_text_stream_and_meta():
    plugin = build_plugin({"base_url": "https://api.example.com/v1"}, {"input": "hello"})
    final_response = {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "model": "gpt-5.5",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        "incomplete_details": None,
    }
    events = collect_events(
        plugin,
        [
            ("response.created", json.dumps({"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5", "status": "in_progress"}})),
            ("response.output_text.delta", json.dumps({"type": "response.output_text.delta", "delta": "Hello "})),
            ("response.output_text.delta", json.dumps({"type": "response.output_text.delta", "delta": "world"})),
            (
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": final_response["output"][0],
                    }
                ),
            ),
            ("response.completed", json.dumps({"type": "response.completed", "response": final_response})),
        ],
    )

    assert [data for event, data in events if event == "delta"] == ["Hello ", "world"]
    assert ("done", "Hello world") in events
    assert ("reasoning_done", "") in events
    assert ("original_done", final_response) in events
    assert ("meta", {"id": "resp_1", "model": "gpt-5.5", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}, "finish_reason": "stop"}) in events


def test_broadcast_response_maps_reasoning_summary_and_preserves_core_status():
    plugin = build_plugin({"base_url": "https://api.example.com/v1"}, {"input": "hello"})
    final_response = {
        "id": "resp_reasoning",
        "object": "response",
        "status": "completed",
        "model": "gpt-5.5",
        "output": [
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "First, inspect the request. "},
                    {"type": "summary_text", "text": "Then answer it."},
                ],
            },
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            },
        ],
        "reasoning": {"effort": "low", "summary": "auto"},
    }
    completed_status = {"status": "completed", "attempt_index": 1, "retry": False}

    events = collect_events(
        plugin,
        [
            (
                "response.reasoning_summary_text.delta",
                json.dumps(
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "First, inspect the request. ",
                    }
                ),
            ),
            (
                "response.reasoning_summary_text.delta",
                json.dumps(
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "summary_index": 1,
                        "delta": "Then answer it.",
                    }
                ),
            ),
            (
                "response.reasoning_summary_text.done",
                json.dumps(
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "summary_index": 1,
                        "text": "Then answer it.",
                    }
                ),
            ),
            ("response.completed", json.dumps({"type": "response.completed", "response": final_response})),
            ("status", completed_status),
        ],
    )

    assert [data for event, data in events if event == "reasoning_delta"] == [
        "First, inspect the request. ",
        "Then answer it.",
    ]
    assert ("reasoning_done", "First, inspect the request. Then answer it.") in events
    assert ("status", completed_status) in events
    assert ("original_done", final_response) in events


def test_broadcast_response_does_not_expose_reasoning_summary_configuration_as_content():
    plugin = build_plugin({"base_url": "https://api.example.com/v1"}, {"input": "hello"})
    final_response = {
        "id": "resp_no_reasoning",
        "object": "response",
        "status": "completed",
        "output": [],
        "reasoning": {"effort": "low", "summary": "auto"},
    }

    events = collect_events(
        plugin,
        [("response.completed", json.dumps({"type": "response.completed", "response": final_response}))],
    )

    assert ("reasoning_done", "") in events


def test_broadcast_response_maps_incomplete_terminal_to_length():
    plugin = build_plugin({"base_url": "https://api.example.com/v1"}, {"input": "hello"})
    incomplete_response = {
        "id": "resp_incomplete",
        "object": "response",
        "status": "incomplete",
        "model": "gpt-5.5",
        "output": [
            {
                "id": "msg_incomplete",
                "type": "message",
                "status": "incomplete",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "partial output"}],
            }
        ],
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    events = collect_events(
        plugin,
        [
            (
                "response.output_text.delta",
                json.dumps({"type": "response.output_text.delta", "delta": "partial output"}),
            ),
            (
                "response.incomplete",
                json.dumps(
                    {
                        "type": "response.incomplete",
                        "response": incomplete_response,
                    }
                ),
            ),
        ],
    )

    assert ("done", "partial output") in events
    assert ("original_done", incomplete_response) in events
    assert (
        "meta",
        {
            "id": "resp_incomplete",
            "model": "gpt-5.5",
            "status": "incomplete",
            "finish_reason": "length",
        },
    ) in events


def test_broadcast_response_preserves_core_status_record():
    plugin = build_plugin({"base_url": "https://api.example.com/v1"}, {"input": "hello"})

    events = collect_events(
        plugin,
        [("status", {"status": "failed", "attempt_index": 1, "retry": True})],
    )

    assert events == [("status", {"status": "failed", "attempt_index": 1, "retry": True})]


def test_broadcast_response_resets_attempt_text_after_retry_status():
    plugin = build_plugin({"base_url": "https://api.example.com/v1"}, {"input": "hello"})

    events = collect_events(
        plugin,
        [
            ("response.output_text.delta", json.dumps({"type": "response.output_text.delta", "delta": "partial"})),
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
            ("response.output_text.delta", json.dumps({"type": "response.output_text.delta", "delta": "replacement"})),
        ],
    )

    assert [payload for event, payload in events if event == "delta"] == ["partial", "replacement"]
    assert ("done", "replacement") in events
    assert ("done", "partialreplacement") not in events


def test_broadcast_response_synthesizes_tool_call_done_without_completed_event():
    plugin = build_plugin({"base_url": "https://api.example.com/v1"}, {"input": "hello"})
    events = collect_events(
        plugin,
        [
            ("response.created", json.dumps({"type": "response.created", "response": {"id": "resp_tool", "model": "gpt-5.5", "status": "in_progress"}})),
            (
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "search_docs",
                            "arguments": '{"query":"Agently"}',
                            "status": "completed",
                        },
                    }
                ),
            ),
        ],
    )

    tool_chunk = next(data for event, data in events if event == "tool_calls")
    meta = next(data for event, data in events if event == "meta")
    original_done = next(data for event, data in events if event == "original_done")

    assert tool_chunk["id"] == "call_1"
    assert tool_chunk["function"]["name"] == "search_docs"
    assert tool_chunk["function"]["arguments"] == '{"query":"Agently"}'
    assert ("done", "") in events
    assert meta["finish_reason"] == "tool_calls"
    assert original_done["output"][0]["type"] == "function_call"


def test_function_call_argument_deltas_can_be_consumed_by_action_normalizer():
    plugin = build_plugin(
        {"base_url": "https://api.example.com/v1"},
        {
            "input": "Find docs.",
            "tools": [{"name": "search_docs", "desc": "Search docs.", "kwargs": {"query": (str, "")}}],
        },
    )
    events = collect_events(
        plugin,
        [
            (
                "response.output_item.added",
                json.dumps(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "call_id": "call_2",
                            "name": "search_docs",
                            "arguments": "",
                            "status": "in_progress",
                        },
                    }
                ),
            ),
            (
                "response.function_call_arguments.delta",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "call_id": "call_2",
                        "delta": '{"query":"Agently',
                    }
                ),
            ),
            (
                "response.function_call_arguments.delta",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "call_id": "call_2",
                        "delta": ' TriggerFlow"}',
                    }
                ),
            ),
            (
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "call_id": "call_2",
                            "name": "search_docs",
                            "arguments": '{"query":"Agently TriggerFlow"}',
                            "status": "completed",
                        },
                    }
                ),
            ),
        ],
    )

    tool_call_chunks = [data for event, data in events if event == "tool_calls"]
    action_calls = Agently.action._normalize_native_action_calls(tool_call_chunks)

    assert action_calls == [
        {
            "purpose": "Use search_docs",
            "action_id": "search_docs",
            "action_input": {"query": "Agently TriggerFlow"},
            "policy_override": {},
            "source_protocol": "native_tool_calls",
            "todo_suggestion": "",
            "next": "",
            "tool_name": "search_docs",
            "tool_kwargs": {"query": "Agently TriggerFlow"},
        }
    ]


@pytest.mark.asyncio
async def test_streaming_uses_first_token_timeout_mode_by_default(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.headers = {}
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aclose(self):
            return None

    async def fake_aiter_sse_with_retry(self, client, method, url, *, headers, json):
        del self, client, method, url, headers, json

        async def generator():
            yield SimpleNamespace(event="response.completed", data='{"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[]}}')

        return generator()

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAIResponsesCompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "timeout": {"connect": 1.0, "read": 9.0, "write": 2.0, "pool": 3.0},
        },
        {"input": "hello"},
    )
    request_data = plugin.generate_request_data()

    async for _event, _payload in plugin.request_model(request_data):
        pass

    timeout = captured["client_kwargs"]["timeout"]
    assert timeout.connect == 1.0
    assert timeout.read is None
    assert timeout.write == 2.0
    assert timeout.pool == 3.0


@pytest.mark.asyncio
async def test_streaming_http_timeout_mode_preserves_http_read_timeout(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.headers = {}
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aclose(self):
            return None

    async def fake_aiter_sse_with_retry(self, client, method, url, *, headers, json):
        del self, client, method, url, headers, json

        async def generator():
            yield SimpleNamespace(event="response.completed", data='{"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[]}}')

        return generator()

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAIResponsesCompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "timeout_mode": "http",
            "timeout": {"connect": 1.0, "read": 9.0, "write": 2.0, "pool": 3.0},
        },
        {"input": "hello"},
    )
    request_data = plugin.generate_request_data()

    async for _event, _payload in plugin.request_model(request_data):
        pass

    timeout = captured["client_kwargs"]["timeout"]
    assert timeout.connect == 1.0
    assert timeout.read == 9.0
    assert timeout.write == 2.0
    assert timeout.pool == 3.0


@pytest.mark.asyncio
async def test_first_token_timeout_returns_timeout_error_event(monkeypatch: pytest.MonkeyPatch):
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
            await asyncio.sleep(0.05)
            yield SimpleNamespace(event="response.completed", data='{"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[]}}')

        return generator()

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAIResponsesCompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "request_retry": {"max_attempts": 1},
            "timeout": {"connect": 1.0, "read": 0.01, "write": 2.0, "pool": 3.0},
        },
        {"input": "hello"},
    )

    request_data = plugin.generate_request_data()

    events = []
    async for event, payload in plugin.request_model(request_data):
        events.append((event, payload))

    assert len(events) == 2
    assert events[0][0] == "status"
    assert events[0][1]["status"] == "failed"
    assert events[0][1]["retry"] is False
    assert events[0][1]["reason"] == "First token timeout after 0.01 seconds."
    assert events[1][0] == "error"
    assert isinstance(events[1][1], TimeoutError)
    assert "First token timeout after 0.01 seconds." in str(events[1][1])


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_event", ["response.completed", "response.incomplete"])
async def test_streaming_stops_transport_after_terminal_response(
    monkeypatch: pytest.MonkeyPatch,
    terminal_event: str,
):
    late_event_reached = False

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
            nonlocal late_event_reached
            status = terminal_event.removeprefix("response.")
            yield SimpleNamespace(
                event=terminal_event,
                data=(
                    f'{{"type":"{terminal_event}","response":'
                    f'{{"id":"resp_1","status":"{status}","output":[]}}}}'
                ),
            )
            late_event_reached = True
            yield SimpleNamespace(
                event="response.output_text.delta",
                data='{"type":"response.output_text.delta","delta":"late"}',
            )

        return generator()

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAIResponsesCompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
        },
        {"input": "hello"},
    )

    events = [item async for item in plugin.request_model(plugin.generate_request_data())]

    assert late_event_reached is False
    assert [event for event, _payload in events] == [terminal_event, "status"]
    assert events[-1][1]["status"] == "completed"


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
                event="response.output_text.delta",
                data='{"type":"response.output_text.delta","delta":"hello"}',
            )
            await asyncio.sleep(0.05)
            yield SimpleNamespace(
                event="response.output_text.delta",
                data='{"type":"response.output_text.delta","delta":"late"}',
            )

        return generator()

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAIResponsesCompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "stream_idle_timeout": 0.01,
            "timeout": {"connect": 1.0, "read": 9.0, "write": 2.0, "pool": 3.0},
        },
        {"input": "hello"},
    )

    request_data = plugin.generate_request_data()

    events = []
    async for event, payload in plugin.request_model(request_data):
        events.append((event, payload))

    assert len(events) == 3
    assert events[0][0] == "response.output_text.delta"
    assert events[1][0] == "status"
    assert events[1][1]["status"] == "failed"
    assert events[1][1]["retry"] is False
    assert events[1][1]["reason"] == "Stream idle timeout after 0.01 seconds."
    assert events[2][0] == "error"
    assert isinstance(events[2][1], TimeoutError)
    assert "Stream idle timeout after 0.01 seconds." in str(events[2][1])


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

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
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
                b'{"id":"resp_1","object":"response","status":"completed","output":[]}',
            )

    monkeypatch.setattr(responses_module, "AsyncClient", FakeAsyncClient)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
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

    assert [headers.get("Authorization") for headers in calls] == ["Bearer key-a", "Bearer key-b"]
    assert events == [
        ("response.completed", '{"id":"resp_1","object":"response","status":"completed","output":[]}'),
        ("status", {"status": "completed", "attempt_index": 1, "retry": False}),
    ]
