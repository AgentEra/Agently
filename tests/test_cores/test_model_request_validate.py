import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import BaseModel, Field

from agently import Agently
from agently.core import ModelRequest, PluginManager
from agently.types.data import AgentlyRequestData, OutputValidateResult
from agently.utils import Settings


class MockValidateJSONRequester:
    name = "MockValidateJSONRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    attempts = 0
    responses: list[Any] = []
    prompt_texts: list[str] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls, responses: list[Any]):
        cls.attempts = 0
        cls.responses = list(responses)
        cls.prompt_texts = []

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        type(self).attempts += 1
        prompt_object = self.prompt.to_prompt_object()
        type(self).prompt_texts.append(self.prompt.to_text())
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "attempt": type(self).attempts,
                "output_format": prompt_object.output_format,
            },
            request_options={"stream": True},
            request_url="mock://validate-json-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        attempt = int(request_data.data.get("attempt", 1))
        index = min(attempt - 1, len(type(self).responses) - 1)
        response = type(self).responses[index]
        if isinstance(response, dict) and "__chunks__" in response:
            for chunk in response["__chunks__"]:
                yield "message", str(chunk)
            return
        yield "message", json.dumps(response, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {"provider": "mock-validate", "model": "json"}


class MockValidateScalarRequester:
    name = "MockValidateScalarRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    attempts = 0
    responses: list[Any] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls, responses: list[Any]):
        cls.attempts = 0
        cls.responses = list(responses)

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        type(self).attempts += 1
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"attempt": type(self).attempts},
            request_options={"stream": True},
            request_url="mock://validate-scalar-requester",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        attempt = int(request_data.data.get("attempt", 1))
        index = min(attempt - 1, len(type(self).responses) - 1)
        yield "payload", type(self).responses[index]

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        payload = None
        async for event, data in response_generator:
            if event == "payload":
                payload = data
        yield "done", payload
        yield "meta", {"provider": "mock-validate", "model": "scalar"}


def _create_request(requester_cls: type[Any], name: str) -> ModelRequest:
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", requester_cls, activate=True)
    return ModelRequest(
        plugin_manager,
        agent_name=name,
        agent_id=f"{ name }-id",
        parent_settings=settings,
    )


def _create_agent(requester_cls: type[Any], name: str):
    settings = Settings(name=f"{ name }-AgentSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-AgentPluginManager")
    plugin_manager.register("ModelRequester", requester_cls, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


@pytest.mark.asyncio
async def test_request_validate_chain_and_runtime_handler_order():
    MockValidateJSONRequester.reset([{"answer": "ok"}])
    request = _create_request(MockValidateJSONRequester, "validate-order")
    request.output({"answer": (str,)}, format="json")

    seen: list[tuple[str, dict[str, Any], int]] = []

    def first(result, context):
        seen.append(("first", dict(result), context.attempt_index))
        return True

    async def second(result, context):
        seen.append(("second", dict(result), context.attempt_index))
        return True

    def third(result, context):
        seen.append(("third", dict(result), context.attempt_index))
        return True

    data = await request.validate(first).validate(second).async_start(validate_handler=third)

    assert data == {"answer": "ok"}
    assert [name for name, _, _ in seen] == ["first", "second", "third"]
    assert all(result == {"answer": "ok"} for _, result, _ in seen)
    assert all(attempt_index == 1 for _, _, attempt_index in seen)


@pytest.mark.asyncio
async def test_agent_structured_output_does_not_retry_after_instant_field():
    MockValidateJSONRequester.reset([{"status": "draft"}, {"status": "ready"}])
    agent = _create_agent(MockValidateJSONRequester, "validate-agent")
    turn = agent.output({"status": (str,)}, format="json")
    turn.validate(lambda result, context: result["status"] == "ready")

    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_validate.agent_retry"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        with pytest.raises(ValueError, match="Validation failed"):
            await turn.async_start(max_retries=1)
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert MockValidateJSONRequester.attempts == 1

    validation_event = next(event for event in captured if event.event_type == "model.validation_failed")
    assert validation_event.payload["validator_name"] == "<lambda>"
    assert validation_event.payload["reason"] == "Validation failed in <lambda>."
    assert validation_event.payload["retry_count"] == 0
    assert validation_event.payload["max_retries"] == 1
    assert validation_event.payload["response_text"] == '{"status": "draft"}'

    assert not any(event.event_type == "model.retrying" for event in captured)


@pytest.mark.asyncio
async def test_instant_stream_suppresses_validation_retry_and_replays_same_attempt():
    MockValidateJSONRequester.reset([{"status": "draft"}, {"status": "ready"}])
    request = _create_request(MockValidateJSONRequester, "validate-retry-instant-stream")
    request.output({"status": (str,)}, format="json")
    response = request.validate(lambda result, context: result["status"] == "ready").get_response()

    first_attempt_items = [item async for item in response.get_async_generator(type="instant")]
    with pytest.raises(ValueError, match="Validation failed"):
        await response.async_get_data(max_retries=1)
    accepted_attempt_items = [item async for item in response.get_async_generator(type="instant")]

    assert MockValidateJSONRequester.attempts == 1
    assert [item.value for item in first_attempt_items if item.path == "status" and item.is_complete] == ["draft"]
    assert [item.value for item in accepted_attempt_items if item.path == "status" and item.is_complete] == ["draft"]


@pytest.mark.asyncio
async def test_complete_instant_field_suppresses_retry_after_final_carrier_failure():
    MockValidateJSONRequester.reset([{"status": "draft", "title": "x"}])
    request = _create_request(MockValidateJSONRequester, "instant-final-carrier-failure")
    request.output({"status": (str,), "title": (str,)}, format="json")
    request.validate(lambda result, context: False)
    response = request.get_response()

    instant_items = [item async for item in response.get_async_generator(type="instant")]
    with pytest.raises(ValueError):
        await response.async_get_data(max_retries=2)

    assert MockValidateJSONRequester.attempts == 1
    assert [item.value for item in instant_items if item.path == "status" and item.is_complete] == ["draft"]
    meta = await response.async_get_meta()
    assert meta["instant_retry_suppressed"] is True
    assert "status" in meta["instant_complete_paths"]


def test_sync_instant_stream_suppresses_validation_retry():
    MockValidateJSONRequester.reset([{"status": "draft"}, {"status": "ready"}])
    request = _create_request(MockValidateJSONRequester, "validate-retry-sync-instant-stream")
    request.output({"status": (str,)}, format="json")
    response = request.validate(lambda result, context: result["status"] == "ready").get_response()

    list(response.get_generator(type="instant"))
    with pytest.raises(ValueError, match="Validation failed"):
        response.get_data(max_retries=1)
    accepted_attempt_items = list(response.get_generator(type="instant"))

    assert MockValidateJSONRequester.attempts == 1
    assert [item.value for item in accepted_attempt_items if item.path == "status" and item.is_complete] == ["draft"]


@pytest.mark.asyncio
async def test_agent_execution_instant_stream_suppresses_validation_retry():
    MockValidateJSONRequester.reset([{"status": "draft"}, {"status": "ready"}])
    agent = _create_agent(MockValidateJSONRequester, "validate-agent-execution-retry-stream")
    execution = agent.output({"status": (str,)}, format="json")
    execution.validate(lambda result, context: result["status"] == "ready")

    with pytest.raises(ValueError, match="Validation failed"):
        [item async for item in execution.get_async_generator(type="instant")]
    assert MockValidateJSONRequester.attempts == 1


@pytest.mark.asyncio
async def test_validate_failure_reason_is_added_to_retry_prompt_without_payload():
    MockValidateJSONRequester.reset([{"status": "draft"}, {"status": "ready"}])
    request = _create_request(MockValidateJSONRequester, "validate-retry-feedback")
    request.output({"status": (str,)}, format="json")
    validation_reason = "status must be ready before publication" + ("." * 300) + "UNBOUNDED_TAIL"

    def require_ready(result, context) -> OutputValidateResult:
        del context
        if result["status"] == "ready":
            return True
        return {
            "ok": False,
            "reason": validation_reason,
            "payload": {"internal_policy": "do-not-send-to-model"},
        }

    data = await request.validate(require_ready).async_start(max_retries=1)

    assert data == {"status": "ready"}
    assert MockValidateJSONRequester.attempts == 2
    assert "[OUTPUT CORRECTION]:" in MockValidateJSONRequester.prompt_texts[1]
    assert "status must be ready before publication" in MockValidateJSONRequester.prompt_texts[1]
    assert "UNBOUNDED_TAIL" not in MockValidateJSONRequester.prompt_texts[1]
    assert "do-not-send-to-model" not in MockValidateJSONRequester.prompt_texts[1]


@pytest.mark.asyncio
async def test_validate_no_retry_can_return_last_result_when_raise_disabled():
    MockValidateJSONRequester.reset([{"status": "draft"}])
    request = _create_request(MockValidateJSONRequester, "validate-no-retry")
    request.output({"status": (str,)}, format="json")

    data = await request.async_start(
        validate_handler=lambda result, context: {
            "ok": False,
            "reason": "draft is not publishable",
            "no_retry": True,
        },
        max_retries=3,
        raise_ensure_failure=False,
    )

    assert data == {"status": "draft"}
    assert MockValidateJSONRequester.attempts == 1


@pytest.mark.asyncio
async def test_validate_stop_raises_without_retry():
    MockValidateJSONRequester.reset([{"status": "draft"}])
    request = _create_request(MockValidateJSONRequester, "validate-stop")
    request.output({"status": (str,)}, format="json")

    with pytest.raises(ValueError, match="stop immediately"):
        await request.async_start(
            validate_handler=lambda result, context: {
                "ok": False,
                "reason": "stop immediately",
                "stop": True,
            },
            max_retries=3,
        )

    assert MockValidateJSONRequester.attempts == 1


@pytest.mark.asyncio
async def test_validate_raise_value_stops_with_explicit_exception():
    MockValidateJSONRequester.reset([{"status": "draft"}])
    request = _create_request(MockValidateJSONRequester, "validate-raise")
    request.output({"status": (str,)}, format="json")

    with pytest.raises(RuntimeError, match="fatal validation"):
        await request.async_start(
            validate_handler=lambda result, context: {
                "ok": False,
                "raise": RuntimeError("fatal validation"),
            },
            max_retries=3,
        )

    assert MockValidateJSONRequester.attempts == 1


@pytest.mark.asyncio
async def test_validate_handler_exception_retries_and_emits_validation_error():
    MockValidateJSONRequester.reset([{"answer": "ok"}, {"answer": "ok"}])
    request = _create_request(MockValidateJSONRequester, "validate-error")
    request.output({"answer": (str,)}, format="json")

    attempts: list[int] = []

    def flaky_validator(result, context):
        del result
        attempts.append(context.attempt_index)
        if context.attempt_index == 1:
            raise RuntimeError("validator boom")
        return True

    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_model_request_validate.validation_error"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        data = await request.async_start(validate_handler=flaky_validator, max_retries=1)
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert data == {"answer": "ok"}
    assert attempts == [1, 2]

    validation_error_event = next(event for event in captured if event.event_type == "model.validation_error")
    assert validation_error_event.payload["validator_name"] == "flaky_validator"
    assert validation_error_event.payload["error_kind"] == "RuntimeError"
    assert validation_error_event.payload["reason"] == "validator boom"
    assert validation_error_event.payload["response_text"] == '{"answer": "ok"}'
    assert "validator boom" not in MockValidateJSONRequester.prompt_texts[1]


@pytest.mark.asyncio
async def test_validate_returns_latest_result_when_retries_exhausted_and_raise_disabled():
    MockValidateJSONRequester.reset([{"status": "draft"}, {"status": "still-draft"}])
    request = _create_request(MockValidateJSONRequester, "validate-latest")
    request.output({"status": (str,)}, format="json")

    data = await request.async_start(
        validate_handler=lambda result, context: False,
        max_retries=1,
        raise_ensure_failure=False,
    )

    assert data == {"status": "still-draft"}
    assert MockValidateJSONRequester.attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "expected_validate_result", "expected_return"),
    [
        ([7], {"value": 7}, 7),
        ([["a", "b"]], {"value": ["a", "b"]}, ["a", "b"]),
    ],
)
async def test_validate_receives_canonical_scalar_and_list_snapshots(
    responses: list[Any],
    expected_validate_result: dict[str, Any],
    expected_return: Any,
):
    MockValidateScalarRequester.reset(responses)
    request = _create_request(MockValidateScalarRequester, "validate-canonical")
    request.input("Return the prepared payload.")

    seen: list[dict[str, Any]] = []

    def validator(result, context):
        del context
        seen.append(dict(result))
        return True

    data = await request.async_start(validate_handler=validator)

    assert data == expected_return
    assert seen == [expected_validate_result]


@pytest.mark.asyncio
async def test_validate_runs_once_per_response_and_context_exposes_result_object():
    MockValidateJSONRequester.reset([{"status": "OPEN", "priority": 1}])
    request = _create_request(MockValidateJSONRequester, "validate-response-cache")
    request.output(
        {
            "status": (str,),
            "priority": (int,),
        },
        format="json",
    )

    calls: list[dict[str, Any]] = []
    contexts = []

    def validator(result, context):
        calls.append(dict(result))
        contexts.append(context)
        return True

    response = request.validate(validator).get_response()
    result_object = await response.async_get_data_object()
    parsed_result = await response.async_get_data()

    assert result_object is not None
    assert parsed_result == {"status": "OPEN", "priority": 1}
    assert calls == [{"status": "OPEN", "priority": 1}]
    assert contexts[0].result_object is not None
    assert contexts[0].typed is result_object


@pytest.mark.asyncio
async def test_pydantic_output_returns_declared_model_instance():
    class Ticket(BaseModel):
        status: str
        priority: int = Field(ge=1, le=3)

    MockValidateJSONRequester.reset([{"status": "OPEN", "priority": 1}])
    request = _create_request(MockValidateJSONRequester, "pydantic-output-model")
    request.output(Ticket, format="json")

    response = request.get_response()
    result_object = await response.async_get_data_object()
    parsed_result = await response.async_get_data()

    assert isinstance(result_object, Ticket)
    assert result_object == Ticket(status="OPEN", priority=1)
    assert parsed_result == {"status": "OPEN", "priority": 1}


@pytest.mark.asyncio
async def test_pydantic_constraint_failure_retries_with_feedback_and_caches_accepted_result():
    class Ticket(BaseModel):
        title: str = Field(max_length=3)

    MockValidateJSONRequester.reset(
        [
            {"title": "too long"},
            {"title": "ok"},
        ]
    )
    request = _create_request(MockValidateJSONRequester, "pydantic-output-constraint-retry")
    request.output(Ticket, format="json")

    response = request.get_response()
    result_object = await response.async_get_data_object(max_retries=1)
    parsed_result = await response.async_get_data(max_retries=1)
    accepted_text = await response.async_get_text()

    assert MockValidateJSONRequester.attempts == 2
    assert result_object == Ticket(title="ok")
    assert parsed_result == {"title": "ok"}
    assert json.loads(accepted_text) == {"title": "ok"}
    assert "[OUTPUT CORRECTION]:" in MockValidateJSONRequester.prompt_texts[1]
    assert "title: String should have at most 3 characters" in MockValidateJSONRequester.prompt_texts[1]


@pytest.mark.asyncio
async def test_pydantic_constraint_failure_is_not_returned_after_retries_are_exhausted():
    class Ticket(BaseModel):
        title: str = Field(max_length=3)

    MockValidateJSONRequester.reset(
        [
            {"title": "too long"},
            {"title": "still too long"},
        ]
    )
    request = _create_request(MockValidateJSONRequester, "pydantic-output-constraint-exhausted")
    request.output(Ticket, format="json")

    with pytest.raises(ValueError, match="Pydantic output validation failed.*title"):
        await request.async_start(max_retries=1)

    assert MockValidateJSONRequester.attempts == 2


@pytest.mark.asyncio
async def test_pydantic_constraint_failure_cannot_be_accepted_by_disabling_ensure_errors():
    class Ticket(BaseModel):
        title: str = Field(default="ok", max_length=3)

    MockValidateJSONRequester.reset([{"title": "too long"}])
    request = _create_request(MockValidateJSONRequester, "pydantic-output-constraint-no-ensure-raise")
    request.output(Ticket, format="json")

    with pytest.raises(ValueError, match="Pydantic output validation failed.*title"):
        await request.async_start(
            max_retries=0,
            raise_ensure_failure=False,
        )

    assert MockValidateJSONRequester.attempts == 1


@pytest.mark.asyncio
async def test_optional_pydantic_constraint_failure_retries_for_typed_object_read():
    class Ticket(BaseModel):
        title: str = Field(default="ok", max_length=3)

    MockValidateJSONRequester.reset(
        [
            {"title": "too long"},
            {"title": "ok"},
        ]
    )
    request = _create_request(MockValidateJSONRequester, "optional-pydantic-output-constraint-retry")
    request.output(Ticket, format="json")

    result_object = await request.get_response().async_get_data_object(max_retries=1)

    assert MockValidateJSONRequester.attempts == 2
    assert result_object == Ticket(title="ok")


@pytest.mark.asyncio
async def test_agent_execution_pydantic_output_preserves_nested_models():
    class Evidence(BaseModel):
        fact: str
        source_excerpt: str

    class MeetingMinutes(BaseModel):
        evidence: list[Evidence] = Field(min_length=1)
        summary: str

    expected = {
        "evidence": [
            {
                "fact": "The team selected plan B.",
                "source_excerpt": "selected plan B",
            }
        ],
        "summary": "Plan B was selected.",
    }
    MockValidateJSONRequester.reset([expected])
    agent = _create_agent(MockValidateJSONRequester, "agent-pydantic-output-model")
    execution = agent.input("Summarize the meeting.").output(MeetingMinutes, format="json")

    result_object = await execution.async_get_data_object()

    assert isinstance(result_object, MeetingMinutes)
    assert isinstance(result_object.evidence[0], Evidence)
    assert result_object.model_dump() == expected


@pytest.mark.asyncio
async def test_ensure_keys_runs_before_validate_handlers():
    MockValidateJSONRequester.reset(
        [
            {"summary": "draft"},
            {"summary": "ready", "reply": "done"},
        ]
    )
    request = _create_request(MockValidateJSONRequester, "validate-after-ensure")
    request.output(
        {
            "summary": (str,),
            "reply": (str,),
        }
    )

    seen: list[dict[str, Any]] = []

    def validator(result, context):
        del context
        seen.append(dict(result))
        return True

    data = await request.async_start(
        ensure_keys=["reply"],
        validate_handler=validator,
        max_retries=1,
    )

    assert data == {"summary": "ready", "reply": "done"}
    assert seen == [{"summary": "ready", "reply": "done"}]


@pytest.mark.asyncio
async def test_tuple_not_null_retries_blank_string_in_wildcard_path():
    MockValidateJSONRequester.reset(
        [
            {
                "environment_checklist": [
                    {"item": "Python", "why": "Runtime check", "command": ""},
                ],
                "final_confirmation": "ready",
            },
            {
                "environment_checklist": [
                    {"item": "Python", "why": "Runtime check", "command": "python --version"},
                ],
                "final_confirmation": "ready",
            },
        ]
    )
    request = _create_request(MockValidateJSONRequester, "ensure-nonempty-command")
    request.output(
        {
            "environment_checklist": [
                {
                    "item": (str, "check item", True),
                    "why": (str, "check reason", True),
                    "command": (str, "bash command", "not_null"),
                }
            ],
            "final_confirmation": (str, "confirmation", True),
        }
    )

    data = await request.async_start(max_retries=1)

    assert MockValidateJSONRequester.attempts == 2
    assert data["environment_checklist"][0]["command"] == "python --version"


@pytest.mark.asyncio
async def test_tuple_ensure_presence_accepts_empty_values():
    MockValidateJSONRequester.reset(
        [
            {
                "ready": True,
                "reason": "",
                "questions": [],
            },
        ]
    )
    request = _create_request(MockValidateJSONRequester, "ensure-presence-empty-values")
    request.output(
        {
            "ready": (bool, "whether the plan can proceed", True),
            "reason": (str, "why the planner made this decision", True),
            "questions": (list, "clarification questions if needed", True),
        }
    )

    data = await request.async_start(max_retries=0)

    assert MockValidateJSONRequester.attempts == 1
    assert data == {"ready": True, "reason": "", "questions": []}


@pytest.mark.asyncio
async def test_runtime_ensure_keys_presence_accepts_empty_values():
    MockValidateJSONRequester.reset(
        [
            {
                "ready": True,
                "reason": "",
                "questions": [],
            },
        ]
    )
    request = _create_request(MockValidateJSONRequester, "runtime-ensure-presence-empty-values")
    request.output(
        {
            "ready": (bool, "whether the plan can proceed"),
            "reason": (str, "why the planner made this decision"),
            "questions": (list, "clarification questions if needed"),
        }
    )

    data = await request.async_start(
        ensure_keys=["ready", "reason", "questions"],
        max_retries=0,
    )

    assert MockValidateJSONRequester.attempts == 1
    assert data == {"ready": True, "reason": "", "questions": []}


@pytest.mark.asyncio
async def test_tuple_not_null_retries_empty_list_value():
    MockValidateJSONRequester.reset(
        [
            {
                "ready": True,
                "questions": [],
            },
            {
                "ready": True,
                "questions": ["确认预算上限"],
            },
        ]
    )
    request = _create_request(MockValidateJSONRequester, "ensure-not-null-list")
    request.output(
        {
            "ready": (bool, "whether the plan can proceed", True),
            "questions": (list, "clarification questions if needed", "not_null"),
        }
    )

    data = await request.async_start(max_retries=1)

    assert MockValidateJSONRequester.attempts == 2
    assert data == {"ready": True, "questions": ["确认预算上限"]}


@pytest.mark.asyncio
async def test_tuple_ensure_accepts_false_and_zero_values():
    MockValidateJSONRequester.reset(
        [
            {
                "ready": False,
                "count": 0,
            },
        ]
    )
    request = _create_request(MockValidateJSONRequester, "ensure-false-zero")
    request.output(
        {
            "ready": (bool, "whether ready", True),
            "count": (int, "number of items", True),
        }
    )

    data = await request.async_start(max_retries=0)

    assert MockValidateJSONRequester.attempts == 1
    assert data == {"ready": False, "count": 0}
