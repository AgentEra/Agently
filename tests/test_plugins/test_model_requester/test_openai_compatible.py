import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import stamina

from httpx import ReadError, RemoteProtocolError
from httpx_sse import SSEError

from typing import Any, cast
from agently import Agently
from agently.core import ModelRequest, PluginManager
from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.model.Prompt import Prompt
from agently.utils import Settings
from agently.builtins.plugins.ModelRequester.OpenAICompatible import (
    OpenAICompatible,
)
import agently.builtins.plugins.ModelRequester.OpenAICompatible.plugin as openai_module
import agently.builtins.plugins.ModelRequester.OpenAICompatible.modules.transport as openai_transport_module
import agently.builtins.plugins.ModelRequester.AnthropicCompatible.modules.transport as anthropic_transport_module
import agently.builtins.plugins.ModelRequester.OpenAIResponsesCompatible.modules.transport as responses_transport_module
from collections import Counter
from types import SimpleNamespace

def build_plugin(config: dict, prompt_values: dict | None = None):
    settings = Settings(parent=Agently.settings)
    settings.update({"plugins": {"ModelRequester": {"OpenAICompatible": config}}})
    prompt = Prompt(plugin_manager=Agently.plugin_manager, parent_settings=settings)
    for key, value in (prompt_values or {}).items():
        prompt.set(key, value)
    return OpenAICompatible(prompt, settings)


class _FakeSSE:
    def __init__(self, data: str, *, event: str = "message"):
        self.event = event
        self.data = data
        self.id = None
        self.retry = None


class _FakeStreamingAsyncClient:
    def __init__(self, **kwargs):
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aclose(self):
        return None


def _install_sse_source(monkeypatch: pytest.MonkeyPatch, source_factory):
    async def fake_aiter_sse_with_retry(
        self,
        client,
        method,
        url,
        *,
        headers,
        json,
    ):
        return source_factory()

    monkeypatch.setattr(openai_module, "AsyncClient", _FakeStreamingAsyncClient)
    monkeypatch.setattr(
        OpenAICompatible,
        "_aiter_sse_with_retry",
        fake_aiter_sse_with_retry,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_retry",
    [False, {"max_attempts": 1}],
)
async def test_request_retry_disabled_owns_physical_sse_attempts(
    monkeypatch: pytest.MonkeyPatch,
    request_retry,
):
    connections = 0

    class FailingEventSource:
        async def aiter_sse(self):
            raise ReadError("dropped SSE connection")
            yield

    @asynccontextmanager
    async def fake_aconnect_sse(*_args, **_kwargs):
        nonlocal connections
        connections += 1
        yield FailingEventSource()

    monkeypatch.setattr(openai_module, "AsyncClient", _FakeStreamingAsyncClient)
    monkeypatch.setattr(
        openai_transport_module,
        "aconnect_sse",
        fake_aconnect_sse,
    )
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "request_retry": request_retry,
        },
        {"input": "hello"},
    )

    with stamina.set_testing(True, attempts=2):
        events = [
            item
            async for item in plugin.request_model(
                plugin.generate_request_data()
            )
        ]

    assert connections == 1
    assert any(
        event == "error" and isinstance(payload, ReadError)
        for event, payload in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport_module", "mixin_type"),
    [
        (
            openai_transport_module,
            openai_transport_module.OpenAICompatibleTransportMixin,
        ),
        (
            anthropic_transport_module,
            anthropic_transport_module.AnthropicCompatibleTransportMixin,
        ),
        (
            responses_transport_module,
            responses_transport_module.OpenAIResponsesCompatibleTransportMixin,
        ),
    ],
)
async def test_compatible_sse_transports_do_not_hide_physical_reconnects(
    monkeypatch: pytest.MonkeyPatch,
    transport_module,
    mixin_type,
):
    connections = 0

    class FailingEventSource:
        async def aiter_sse(self):
            raise ReadError("dropped SSE connection")
            yield

    @asynccontextmanager
    async def fake_aconnect_sse(*_args, **_kwargs):
        nonlocal connections
        connections += 1
        yield FailingEventSource()

    monkeypatch.setattr(transport_module, "aconnect_sse", fake_aconnect_sse)
    generator = await mixin_type()._aiter_sse_with_retry(
        object(),
        "POST",
        "https://example.invalid/v1/stream",
        headers={},
        json={},
    )

    with stamina.set_testing(True, attempts=2):
        with pytest.raises(ReadError, match="dropped SSE connection"):
            await anext(generator)

    assert connections == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_output", "expected_connections", "expected_retry"),
    [(False, 1, False), (True, 2, True)],
)
async def test_request_retry_after_output_owns_physical_sse_attempts(
    monkeypatch: pytest.MonkeyPatch,
    after_output: bool,
    expected_connections: int,
    expected_retry: bool,
):
    connections = 0

    class EventSource:
        def __init__(self, connection_index: int):
            self.connection_index = connection_index

        async def aiter_sse(self):
            if self.connection_index == 1:
                yield _FakeSSE(
                    '{"choices":[{"delta":{"content":"partial"}}]}'
                )
                raise ReadError("dropped SSE connection after output")
            yield _FakeSSE(
                '{"choices":[{"delta":{"content":"replacement"}}]}'
            )
            yield _FakeSSE("[DONE]")

    @asynccontextmanager
    async def fake_aconnect_sse(*_args, **_kwargs):
        nonlocal connections
        connections += 1
        yield EventSource(connections)

    monkeypatch.setattr(openai_module, "AsyncClient", _FakeStreamingAsyncClient)
    monkeypatch.setattr(
        openai_transport_module,
        "aconnect_sse",
        fake_aconnect_sse,
    )
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "request_retry": {
                "max_attempts": 2,
                "after_output": after_output,
            },
        },
        {"input": "hello"},
    )

    events = [
        item
        async for item in plugin.request_model(
            plugin.generate_request_data()
        )
    ]

    retry_statuses = [
        payload
        for event, payload in events
        if event == "status" and payload.get("retry") is True
    ]
    assert connections == expected_connections
    assert bool(retry_statuses) is expected_retry
    if expected_retry:
        assert retry_statuses == [
            {
                "status": "failed",
                "attempt_index": 1,
                "retry": True,
                "next_attempt_index": 2,
                "reason": "dropped SSE connection after output",
                "error_type": "ReadError",
            }
        ]
        assert events[-1] == (
            "status",
            {"status": "completed", "attempt_index": 2, "retry": False},
        )


def generate_request(config: dict, prompt_values: dict | None = None):
    return build_plugin(config, prompt_values).generate_request_data().model_dump()


async def capture_request_headers(monkeypatch: pytest.MonkeyPatch, config: dict, prompt_values: dict | None = None):
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        content = b'{"ok": true}'
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

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
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

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
    if stream:
        monkeypatch.setattr(OpenAICompatible, "_aiter_sse_with_retry", fail_sse)
    request_marker = f"private-openai-{'stream-' if stream else ''}request-marker"
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


@pytest.mark.asyncio
async def test_non_stream_request_retries_next_api_key_on_failover(monkeypatch):
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
            calls.append(dict(self.headers if headers is None else headers))
            if len(calls) == 1:
                return FakeResponse(429, b'{"error":{"message":"rate limited"}}')
            return FakeResponse(200, b'{"choices":[{"delta":{"content":"ok"}}]}')

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "api_key": "key-a",
            "stream": False,
            "_api_key_pool_runtime": {
                "pool_id": "example",
                "failover": {"strategy": "try_next", "max_attempts": 2, "retry_status_codes": [429]},
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
        ("message", '{"choices":[{"delta":{"content":"ok"}}]}'),
        ("message", "[DONE]"),
        ("status", {"status": "completed", "attempt_index": 1, "retry": False}),
    ]


@pytest.mark.asyncio
async def test_request_model_retries_transient_provider_error_before_output_started(monkeypatch):
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "request_retry": {"max_attempts": 2},
        },
        {"input": "hello"},
    )
    calls = 0

    async def fake_legacy(_request_data):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "error", RemoteProtocolError("server disconnected")
            return
        yield "message", '{"choices":[{"delta":{"content":"ok"}}]}'
        yield "message", "[DONE]"

    monkeypatch.setattr(plugin, "_request_model_legacy", fake_legacy)

    events = []
    async for event, payload in plugin.request_model(plugin.generate_request_data()):
        events.append((event, payload))

    assert calls == 2
    assert events == [
        (
            "status",
            {
                "status": "failed",
                "attempt_index": 1,
                "retry": True,
                "next_attempt_index": 2,
                "reason": "server disconnected",
                "error_type": "RemoteProtocolError",
            },
        ),
        ("message", '{"choices":[{"delta":{"content":"ok"}}]}'),
        ("message", "[DONE]"),
        ("status", {"status": "completed", "attempt_index": 2, "retry": False}),
    ]


@pytest.mark.asyncio
async def test_request_model_retries_transient_provider_error_after_output_started_by_default(monkeypatch):
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "request_retry": {"max_attempts": 2},
        },
        {"input": "hello"},
    )
    calls = 0

    async def fake_legacy(_request_data):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "message", '{"choices":[{"delta":{"content":"partial"}}]}'
            yield "error", RemoteProtocolError("server disconnected")
            return
        yield "message", '{"choices":[{"delta":{"content":"replacement"}}]}'
        yield "message", "[DONE]"

    monkeypatch.setattr(plugin, "_request_model_legacy", fake_legacy)

    events = []
    async for event, payload in plugin.request_model(plugin.generate_request_data()):
        events.append((event, payload))

    assert calls == 2
    assert events[0] == ("message", '{"choices":[{"delta":{"content":"partial"}}]}')
    assert events[1][0] == "status"
    assert events[1][1]["status"] == "failed"
    assert events[1][1]["retry"] is True
    assert events[1][1]["next_attempt_index"] == 2
    assert events[1][1]["reason"] == "server disconnected"
    assert events[2] == ("message", '{"choices":[{"delta":{"content":"replacement"}}]}')
    assert events[-1] == ("status", {"status": "completed", "attempt_index": 2, "retry": False})


@pytest.mark.asyncio
async def test_request_model_can_disable_retry_after_output_started(monkeypatch):
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "request_retry": {"max_attempts": 2, "after_output": False},
        },
        {"input": "hello"},
    )
    calls = 0

    async def fake_legacy(_request_data):
        nonlocal calls
        calls += 1
        yield "message", '{"choices":[{"delta":{"content":"partial"}}]}'
        yield "error", RemoteProtocolError("server disconnected")

    monkeypatch.setattr(plugin, "_request_model_legacy", fake_legacy)

    events = []
    async for event, payload in plugin.request_model(plugin.generate_request_data()):
        events.append((event, payload))

    assert calls == 1
    assert events[0] == ("message", '{"choices":[{"delta":{"content":"partial"}}]}')
    assert events[1][0] == "status"
    assert events[1][1]["status"] == "failed"
    assert events[1][1]["retry"] is False
    assert events[1][1]["reason"] == "server disconnected"
    assert events[2][0] == "error"
    assert isinstance(events[2][1], RemoteProtocolError)


@pytest.mark.asyncio
async def test_request_model_retries_transient_provider_error_after_output_when_enabled(monkeypatch):
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
            yield "message", '{"choices":[{"delta":{"content":"partial"}}]}'
            yield "error", RemoteProtocolError("peer closed connection")
            return
        yield "message", '{"choices":[{"delta":{"content":"replacement"}}]}'
        yield "message", "[DONE]"

    monkeypatch.setattr(plugin, "_request_model_legacy", fake_legacy)

    events = [event async for event in plugin.request_model(plugin.generate_request_data())]

    assert calls == 2
    assert events[1] == (
        "status",
        {
            "status": "failed",
            "attempt_index": 1,
            "retry": True,
            "next_attempt_index": 2,
            "reason": "peer closed connection",
            "error_type": "RemoteProtocolError",
        },
    )
    assert events[-1] == ("status", {"status": "completed", "attempt_index": 2, "retry": False})


@pytest.mark.asyncio
async def test_broadcast_response_maps_non_stream_chat_message_content():
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": False,
        },
        {"input": "hello"},
    )

    async def response_generator():
        yield "message", '{"choices":[{"message":{"role":"assistant","content":"done text"},"finish_reason":"stop"}]}'
        yield "message", "[DONE]"

    events = []
    async for event, payload in plugin.broadcast_response(response_generator()):
        events.append((event, payload))

    assert ("done", "done text") in events
    original_done = next(payload for event, payload in events if event == "original_done")
    assert original_done["choices"][0]["message"]["content"] == "done text"


@pytest.mark.asyncio
async def test_broadcast_response_maps_non_stream_chat_reasoning_content():
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "reasoning-model",
            "stream": False,
        },
        {"input": "hello"},
    )

    async def response_generator():
        yield "message", json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "model reasoning",
                            "content": "done text",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        yield "message", "[DONE]"

    events = [item async for item in plugin.broadcast_response(response_generator())]

    assert ("reasoning_done", "model reasoning") in events
    original_done = next(payload for event, payload in events if event == "original_done")
    assert original_done["choices"][0]["message"]["reasoning_content"] == "model reasoning"


@pytest.mark.asyncio
async def test_broadcast_response_preserves_streamed_reasoning_in_original_done():
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "reasoning-model",
            "stream": True,
        },
        {"input": "hello"},
    )

    async def response_generator():
        yield "message", '{"choices":[{"delta":{"role":"assistant","reasoning_content":"model "}}]}'
        yield "message", '{"choices":[{"delta":{"reasoning_content":"reasoning"}}]}'
        yield "message", '{"choices":[{"delta":{"content":"done text"}}]}'
        yield "message", '{"choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield "message", "[DONE]"

    events = [item async for item in plugin.broadcast_response(response_generator())]

    assert ("reasoning_done", "model reasoning") in events
    original_done = next(payload for event, payload in events if event == "original_done")
    assert original_done["choices"][0]["message"] == {
        "role": "assistant",
        "content": "done text",
        "reasoning_content": "model reasoning",
    }


@pytest.mark.asyncio
async def test_broadcast_response_preserves_core_status_record():
    plugin = build_plugin({"base_url": "https://api.example.com/v1", "model": "m1"}, {"input": "hello"})

    async def response_generator():
        yield "status", {"status": "failed", "attempt_index": 1, "retry": True}

    events = [item async for item in plugin.broadcast_response(response_generator())]

    assert events == [("status", {"status": "failed", "attempt_index": 1, "retry": True})]


@pytest.mark.asyncio
async def test_broadcast_response_resets_attempt_text_after_retry_status():
    plugin = build_plugin({"base_url": "https://api.example.com/v1", "model": "m1"}, {"input": "hello"})

    async def response_generator():
        yield "message", '{"choices":[{"delta":{"content":"partial"}}]}'
        yield "status", {
            "status": "failed",
            "attempt_index": 1,
            "retry": True,
            "next_attempt_index": 2,
            "reason": "server disconnected",
        }
        yield "message", '{"choices":[{"delta":{"content":"replacement"}}]}'
        yield "message", "[DONE]"

    events = [item async for item in plugin.broadcast_response(response_generator())]

    assert [payload for event, payload in events if event == "delta"] == ["partial", "replacement"]
    assert ("done", "replacement") in events
    assert ("done", "partialreplacement") not in events


@pytest.mark.asyncio
async def test_broadcast_response_handles_usage_only_final_chunk_without_choices():
    # Regression for #287: some OpenAI-compatible gateways (YuDing, MiMo) send a
    # usage-only final chunk with an empty "choices" array before [DONE]. The done
    # handler must not raise IndexError and must preserve the accumulated content.
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
        },
        {"input": "hello"},
    )

    async def response_generator():
        yield "message", '{"id":"1","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}'
        yield "message", '{"id":"1","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}'
        yield "message", '{"id":"1","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}'
        yield "message", '{"id":"1","choices":[{"index":0,"delta":{"content":""},"finish_reason":"stop"}]}'
        yield "message", '{"id":"1","choices":[],"usage":{"prompt_tokens":27,"completion_tokens":17,"total_tokens":44}}'
        yield "message", "[DONE]"

    events = []
    async for event, payload in plugin.broadcast_response(response_generator()):
        events.append((event, payload))

    assert ("done", "hello world") in events
    original_done = next(payload for event, payload in events if event == "original_done")
    assert original_done["choices"][0]["message"]["content"] == "hello world"
    meta = next(payload for event, payload in events if event == "meta")
    assert meta["usage"]["total_tokens"] == 44
def test_plugin_root_options_are_treated_as_request_options():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "options": {"temperature": 0.7, "top_p": 0.9},
        },
        {"input": "hello"},
    )

    assert request["request_options"] == {"temperature": 0.7, "top_p": 0.9, "model": "m1", "stream": True}


def test_request_options_override_legacy_plugin_root_options():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "options": {"temperature": 0.7, "top_p": 0.9},
            "request_options": {"temperature": 0.2},
        },
        {"input": "hello", "options": {"top_p": 0.5}},
    )

    assert request["request_options"] == {"temperature": 0.2, "top_p": 0.5, "model": "m1", "stream": True}


def test_nested_thinking_request_option_is_preserved():
    request = generate_request(
        {
            "base_url": "https://api.example.com/v1",
            "model": "reasoning-model",
            "request_options": {"thinking": {"type": "enabled"}},
        },
        {"input": "hello"},
    )

    assert request["request_options"]["thinking"] == {"type": "enabled"}


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


@pytest.mark.asyncio
async def test_auth_headers_are_kept_when_api_key_sets_authorization(monkeypatch: pytest.MonkeyPatch):
    captured = await capture_request_headers(
        monkeypatch,
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": False,
            "api_key": "KEY2",
            "auth": {"headers": {"X-Test": "1"}},
        },
        {"input": "hello"},
    )

    assert captured["headers"]["Authorization"] == "Bearer KEY2"
    assert captured["headers"]["X-Test"] == "1"


@pytest.mark.asyncio
async def test_streaming_done_is_not_emitted_twice(monkeypatch: pytest.MonkeyPatch):
    class FakeSSE:
        def __init__(self, event: str, data: str):
            self.event = event
            self.data = data
            self.id = None
            self.retry = None

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
        async def generator():
            yield FakeSSE(
                "message",
                '{"id":"1","choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}],"usage":{"total_tokens":1}}',
            )
            yield FakeSSE("message", "[DONE]")

        return generator()

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAICompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
        },
        {"input": "hello"},
    )
    request_data = plugin.generate_request_data()
    response = plugin.broadcast_response(plugin.request_model(request_data))

    events = []
    async for event, data in response:
        events.append((event, data))

    counts = Counter(event for event, _ in events)
    assert counts["done"] == 1
    assert counts["reasoning_done"] == 1
    assert counts["meta"] == 1


@pytest.mark.asyncio
async def test_streaming_done_stops_before_physical_tail_disconnect(
    monkeypatch: pytest.MonkeyPatch,
):
    tail_requested = False

    async def source():
        nonlocal tail_requested
        yield _FakeSSE(
            '{"id":"1","choices":[{"index":0,"delta":{"role":"assistant","content":"hello"},"finish_reason":null}]}'
        )
        yield _FakeSSE(
            '{"id":"1","choices":[{"index":0,"delta":{"content":""},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}'
        )
        yield _FakeSSE("[DONE]")
        tail_requested = True
        raise RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

    _install_sse_source(monkeypatch, source)
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "request_retry": {"max_attempts": 1},
        },
        {"input": "hello"},
    )

    events = [
        item
        async for item in plugin.broadcast_response(
            plugin.request_model(plugin.generate_request_data())
        )
    ]

    assert tail_requested is False
    assert [payload for event, payload in events if event == "delta"] == [
        "hello"
    ]
    assert [payload for event, payload in events if event == "done"] == [
        "hello"
    ]
    assert not [payload for event, payload in events if event == "error"]
    original_done = next(
        payload for event, payload in events if event == "original_done"
    )
    assert original_done["choices"][0]["message"]["content"] == "hello"
    assert original_done["choices"][0]["finish_reason"] == "stop"
    assert original_done["usage"]["total_tokens"] == 3
    meta = next(payload for event, payload in events if event == "meta")
    assert meta["finish_reason"] == "stop"
    assert meta["usage"]["completion_tokens"] == 2


@pytest.mark.asyncio
async def test_streaming_disconnect_before_done_remains_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    async def source():
        yield _FakeSSE(
            '{"id":"1","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}'
        )
        raise RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )

    _install_sse_source(monkeypatch, source)
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "request_retry": {"max_attempts": 1},
        },
        {"input": "hello"},
    )

    events = [
        item
        async for item in plugin.request_model(plugin.generate_request_data())
    ]

    assert [payload for event, payload in events if event == "message"] == [
        '{"id":"1","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}'
    ]
    errors = [payload for event, payload in events if event == "error"]
    assert len(errors) == 1
    assert isinstance(errors[0], RemoteProtocolError)
    assert not [
        payload
        for event, payload in events
        if event == "message" and payload == "[DONE]"
    ]
    assert events[-2][0] == "status"
    assert events[-2][1]["status"] == "failed"
    assert events[-2][1]["retry"] is False


@pytest.mark.asyncio
async def test_streaming_does_not_consume_events_after_done(
    monkeypatch: pytest.MonkeyPatch,
):
    extra_requested = False

    async def source():
        nonlocal extra_requested
        yield _FakeSSE(
            '{"id":"1","choices":[{"index":0,"delta":{"content":"accepted"},"finish_reason":"stop"}]}'
        )
        yield _FakeSSE("[DONE]")
        extra_requested = True
        yield _FakeSSE(
            '{"id":"1","choices":[{"index":0,"delta":{"content":"must-not-be-consumed"},"finish_reason":null}]}'
        )

    _install_sse_source(monkeypatch, source)
    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "request_retry": {"max_attempts": 1},
        },
        {"input": "hello"},
    )

    events = [
        item
        async for item in plugin.request_model(plugin.generate_request_data())
    ]

    assert extra_requested is False
    assert [payload for event, payload in events if event == "message"] == [
        '{"id":"1","choices":[{"index":0,"delta":{"content":"accepted"},"finish_reason":"stop"}]}',
        "[DONE]",
    ]


@pytest.mark.asyncio
async def test_structured_validation_retry_materializes_after_done_tail_disconnect(
    monkeypatch: pytest.MonkeyPatch,
):
    attempt_contents = [
        '{"status":"draft"}',
        '{"status":"ready"}',
    ]
    source_count = 0

    async def source():
        nonlocal source_count
        attempt_index = source_count
        source_count += 1
        content = attempt_contents[attempt_index]
        yield _FakeSSE(
            json.dumps(
                {
                    "id": f"attempt-{ attempt_index + 1 }",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": content,
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
        )
        yield _FakeSSE(
            json.dumps(
                {
                    "id": f"attempt-{ attempt_index + 1 }",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": ""},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 3,
                    },
                }
            )
        )
        yield _FakeSSE("[DONE]")
        if attempt_index == 1:
            raise RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )

    _install_sse_source(monkeypatch, source)
    settings = Settings(
        name="openai-done-validation-retry-Settings",
        parent=Agently.settings,
    )
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="openai-done-validation-retry-PluginManager",
    )
    plugin_manager.register(
        "ModelRequester",
        OpenAICompatible,
        activate=True,
    )
    settings.update(
        {
            "plugins": {
                "ModelRequester": {
                    "OpenAICompatible": {
                        "base_url": "https://api.example.com/v1",
                        "model": "m1",
                        "stream": True,
                        "request_retry": False,
                    }
                }
            }
        }
    )
    request = ModelRequest(
        plugin_manager,
        agent_name="openai-done-validation-retry",
        agent_id="openai-done-validation-retry-id",
        parent_settings=settings,
    )
    request.output({"status": (str,)}, format="json")
    request.validate(
        lambda result, _context: result["status"] == "ready"
    )

    result = await request.async_start(max_retries=1)

    assert result == {"status": "ready"}
    assert source_count == 2


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
            yield SimpleNamespace(event="message", data='{"choices":[{"delta":{"content":"hello"}}]}')
            yield SimpleNamespace(event="message", data="[DONE]")

        return generator()

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAICompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

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
            yield SimpleNamespace(event="message", data='{"choices":[{"delta":{"content":"hello"}}]}')
            yield SimpleNamespace(event="message", data="[DONE]")

        return generator()

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAICompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

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

    calls = 0

    async def fake_aiter_sse_with_retry(self, client, method, url, *, headers, json):
        nonlocal calls
        del self, client, method, url, headers, json
        calls += 1

        async def generator():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0.1)
                raise
            yield SimpleNamespace(event="message", data='{"choices":[{"delta":{"content":"hello"}}]}')

        return generator()

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAICompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

    plugin = build_plugin(
        {
            "base_url": "https://api.example.com/v1",
            "model": "m1",
            "stream": True,
            "timeout": {"connect": 1.0, "read": 0.01, "write": 2.0, "pool": 3.0},
        },
        {"input": "hello"},
    )
    request_data = plugin.generate_request_data()

    events = []
    started_at = asyncio.get_running_loop().time()
    async for event, payload in plugin.request_model(request_data):
        events.append((event, payload))
    elapsed_seconds = asyncio.get_running_loop().time() - started_at
    cleanup_tasks = cast(
        tuple[asyncio.Task[Any], ...],
        tuple(getattr(plugin, "_stream_cleanup_tasks", ())),
    )
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    statuses = [payload for event, payload in events if event == "status"]
    assert calls == 2
    assert elapsed_seconds < 0.15
    assert len(statuses) == 2
    assert statuses[0]["status"] == "failed"
    assert statuses[0]["attempt_index"] == 1
    assert statuses[0]["retry"] is True
    assert statuses[0]["next_attempt_index"] == 2
    assert statuses[0]["reason"] == "First token timeout after 0.01 seconds."
    assert statuses[0]["error_diagnostic"] == {
        "error_type": "RuntimeStageStallError",
        "stage": "response_first_event",
        "status": "stalled",
        "message": "First token timeout after 0.01 seconds.",
        "response_id": None,
        "run_id": None,
        "agent_name": None,
        "elapsed_seconds": None,
        "idle_seconds": 0.01,
        "timeout_seconds": 0.01,
        "last_progress_event": None,
        "provider": "OpenAICompatible",
        "model": "m1",
        "planning_protocol": None,
        "diagnostic_context": {
            "owner": "model_request",
            "progress_basis": "meaningful_provider_response_data",
            "transport_cleanup": "asynchronous",
        },
    }
    assert statuses[-1]["status"] == "failed"
    assert statuses[-1]["attempt_index"] == 2
    assert statuses[-1]["retry"] is False
    assert statuses[-1]["error_diagnostic"]["stage"] == "response_first_event"
    assert statuses[-1]["error_diagnostic"]["diagnostic_context"]["owner"] == "model_request"
    assert events[-1][0] == "error"
    assert isinstance(events[-1][1], TimeoutError)
    assert "First token timeout after 0.01 seconds." in str(events[-1][1])


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
            yield SimpleNamespace(event="message", data='{"choices":[{"delta":{"content":"hello"}}]}')
            await asyncio.sleep(0.05)
            yield SimpleNamespace(event="message", data='{"choices":[{"delta":{"content":"late"}}]}')

        return generator()

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(OpenAICompatible, "_aiter_sse_with_retry", fake_aiter_sse_with_retry)

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
    assert events[0][0] == "message"
    assert events[1][0] == "status"
    assert events[1][1]["status"] == "failed"
    assert events[1][1]["retry"] is False
    assert events[1][1]["reason"] == "Stream idle timeout after 0.01 seconds."
    assert events[2][0] == "error"
    assert isinstance(events[2][1], TimeoutError)
    assert "Stream idle timeout after 0.01 seconds." in str(events[2][1])


@pytest.mark.asyncio
async def test_non_stream_response_idle_timeout_returns_stall_error(monkeypatch: pytest.MonkeyPatch):
    # A non-streaming request awaits one blocking response, which the streaming
    # first_token/stream_idle guards never cover. With stream_idle_timeout set, a
    # provider that opens the request but never returns must surface as a liveness
    # stall so the framework can capture liveness evidence and fall back, instead
    # of hanging until the coarse task-level no-progress budget.
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

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)

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
            return FakeResponse(200, b'{"choices":[{"delta":{"content":"ok"}}]}')

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)

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
        ("message", '{"choices":[{"delta":{"content":"ok"}}]}'),
        ("message", "[DONE]"),
        ("status", {"status": "completed", "attempt_index": 1, "retry": False}),
    ]


@pytest.mark.asyncio
async def test_non_stream_response_without_idle_timeout_is_unbounded(monkeypatch: pytest.MonkeyPatch):
    # Without an explicit stream_idle_timeout the non-streaming path stays
    # unbounded (previous behavior): a normal response returns without any
    # liveness deadline being applied.
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
            del url, json, headers
            return FakeResponse(200, b'{"choices":[{"delta":{"content":"ok"}}]}')

    monkeypatch.setattr(openai_module, "AsyncClient", FakeAsyncClient)

    plugin = build_plugin(
        {"base_url": "https://api.example.com/v1", "model": "m1", "stream": False},
        {"input": "hello"},
    )

    events = []
    async for event, payload in plugin.request_model(plugin.generate_request_data()):
        events.append((event, payload))

    assert ("message", '{"choices":[{"delta":{"content":"ok"}}]}') in events
    assert not any(
        event == "error" and isinstance(payload, TimeoutError) for event, payload in events
    )
