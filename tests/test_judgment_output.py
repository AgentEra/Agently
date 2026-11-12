"""Synthetic protocol and orchestration evidence, not semantic model evaluation."""

import json

import httpx
import pytest
from test_builtin_agent_executions import ScriptedExecutionRequester, create_execution_agent

from agently import Agently, Choice, Probability, Score
from agently.builtins.plugins.AgentExecution.modules.judgment_schema import JudgmentSchema
from agently.builtins.plugins.ModelRequester.Jev import Jev


@pytest.fixture
def wire(monkeypatch):
    calls = []
    replies = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, headers, json):
            calls.append(json)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr("agently.builtins.plugins.ModelRequester.Jev.httpx.AsyncClient", Client)
    return calls, replies


def native(*values):
    return {
        "model": "jev-test",
        "answers": {f"q{i}": {"type": "noul", "noul": value} for i, value in enumerate(values)},
        "usage": {"input_tokens": 8, "output_tokens": 4},
    }


def enabled(tmp_path, scripts):
    agent = create_execution_agent(tmp_path, "judgment", scripts)
    agent.set_settings("Jev", {"api_key": "test-key"})
    agent.set_settings("system_one", {"provider": "Jev"})
    return agent


def test_pure_jev_batch_no_llm(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.7, 0.3))
    execution = (
        enabled(tmp_path, []).input("Evidence").output({"a": Probability("A?"), "nested": {"b": Probability("B?")}})
    )
    assert execution.get_data() == {"a": 0.7, "nested": {"b": 0.3}}
    assert len(calls) == 1
    assert ScriptedExecutionRequester.model_dispatches == 0
    meta = execution.get_meta().get("judgment", {})
    assert meta["stages"][0]["jev"]["usage"]["input_tokens"] == 8
    assert meta["fields"]["a"]["provider"] == "Jev"
    typed = execution.get_data_object()
    assert typed is not None
    assert typed.model_dump()["a"] == 0.7


def test_mixed_jev_before_llm_host_merge(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.7))
    execution = (
        enabled(tmp_path, [{"field_0": "Synthetic summary"}])
        .input("Evidence")
        .output({"p": Probability("P?"), "summary": str})
    )
    assert execution.get_data() == {"p": 0.7, "summary": "Synthetic summary"}
    assert len(calls) == 1
    assert ScriptedExecutionRequester.requests[-1]["output"] == {"field_0": str}
    assert "0.7" in str(ScriptedExecutionRequester.requests[-1]["info"])


def test_dynamic_current_item_binding(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.6, 0.2))
    execution = (
        enabled(tmp_path, [{"items": [{"name": "First"}, {"name": "Second"}]}])
        .input("Evidence")
        .output({"items": [{"name": str, "p": Probability("P?", from_output="items[].name")}]})
    )
    assert execution.get_data() == {"items": [{"name": "First", "p": 0.6}, {"name": "Second", "p": 0.2}]}
    assert [q["instructions"]["target"] for q in calls[0]["questions"].values()] == ["First", "Second"]


def test_empty_list_no_jev(tmp_path, wire):
    execution = (
        enabled(tmp_path, [{"items": []}])
        .input("Evidence")
        .output({"items": [{"name": str, "p": Probability("P?", from_output="items[].name")}]})
    )
    assert execution.get_data() == {"items": []}
    assert not wire[0]


def test_jev_dependency_chain(tmp_path, wire):
    calls, replies = wire
    replies.extend([native(0.8), native(0.9)])
    execution = (
        enabled(tmp_path, [])
        .input("Evidence")
        .output({"a": Probability("A?"), "b": Probability("B?", from_output="a")})
    )
    assert execution.get_data() == {"a": 0.8, "b": 0.9}
    assert calls[1]["questions"]["q0"]["instructions"]["target"] == 0.8


def test_fallback_retains_constraints(tmp_path):
    execution = (
        create_execution_agent(tmp_path, "fallback", [{"c": "yes", "s": 0.25}])
        .input("Evidence")
        .output({"c": Choice("Choose", {"yes": "Matches", "no": "Different"}), "s": Score("Grade", ["low", "high"])})
    )
    assert execution.get_data() == {"c": "yes", "s": 0.25}
    assert "Matches" in str(ScriptedExecutionRequester.requests[-1]["output"])
    assert not execution.get_meta().get("judgment", {})["native"]


def test_shared_retry_does_not_repeat_success(tmp_path, wire):
    calls, replies = wire
    replies.extend([native(0.8), httpx.ConnectError("synthetic"), native(0.9)])
    execution = (
        enabled(tmp_path, [])
        .input("Evidence")
        .output({"a": Probability("A?"), "b": Probability("B?", from_output="a")})
    )
    assert execution.get_data(max_retries=1) == {"a": 0.8, "b": 0.9}
    assert len(calls) == 3
    assert execution.get_meta().get("judgment", {})["retries_used"] == 1


@pytest.mark.parametrize(
    "schema",
    [
        {"a": Probability("A?", from_output="a")},
        {"a": Probability("A?", from_output="b"), "b": Probability("B?", from_output="a")},
        {"a": Probability("A?", from_output="missing")},
        {"a": [{"p": Probability("P?", from_output="b[].name")}], "b": [{"name": str}]},
    ],
)
def test_bad_dependencies_fail_before_network(schema):
    with pytest.raises(ValueError):
        JudgmentSchema(schema)


def test_adapter_projects_choice_and_score():
    request = Agently.create_request()
    request.input("Evidence").output(
        {"c": Choice("Choose", {"a": None, "b": "B"}), "s": Score("Grade", ["low", "high"])}
    )
    request.set_settings("Jev", {"api_key": "fake"})
    data = Jev(request.prompt, request.settings).generate_request_data()
    assert data.request_url == "https://api.typesafe.ai/v1/systemone"
    assert data.data["questions"]["q1"]["criteria"] == ["low", "high"]
    assert "fake" not in json.dumps(data.data)


def test_root_scalar_and_standalone_request(tmp_path, wire):
    _, replies = wire
    replies.extend([native(0.4), native(0.6)])
    execution = enabled(tmp_path, []).input("Evidence").output(Probability("P?"))
    assert execution.get_data() == 0.4
    request = Agently.create_request().input("Evidence").output(Probability("P?"))
    request.set_settings("Jev", {"api_key": "fake"})
    request.set_settings("plugins.ModelRequester.activate", "Jev")
    assert request.get_data(max_retries=0) == 0.6


def test_root_list_binding(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.3))
    execution = (
        enabled(tmp_path, [[{"name": "Item"}]])
        .input("Evidence")
        .output([{"name": str, "p": Probability("P?", from_output="[].name")}])
    )
    assert execution.get_data() == [{"name": "Item", "p": 0.3}]
    assert calls[0]["questions"]["q0"]["instructions"]["target"] == "Item"


def test_nested_lists_binding(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.1, 0.9))
    base = {"groups": [{"items": [{"name": "A"}, {"name": "B"}]}]}
    execution = (
        enabled(tmp_path, [base])
        .input("Evidence")
        .output({"groups": [{"items": [{"name": str, "p": Probability("P?", from_output="groups[].items[].name")}]}]})
    )
    result = execution.get_data()
    assert [item["p"] for item in result["groups"][0]["items"]] == [0.1, 0.9]
    assert [q["instructions"]["target"] for q in calls[0]["questions"].values()] == ["A", "B"]


def test_external_list_reference_collects_values(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.9))
    execution = (
        enabled(tmp_path, [{"items": [{"name": "A"}, {"name": "B"}]}])
        .input("Evidence")
        .output({"items": [{"name": str}], "p": Probability("P?", from_output="items[].name")})
    )
    assert execution.get_data()["p"] == 0.9
    assert calls[0]["questions"]["q0"]["instructions"]["target"] == ["A", "B"]


def test_disabled_jev_ignores_bad_credentials_and_combines_llm(tmp_path, wire):
    agent = enabled(tmp_path, [{"p": 0.5, "summary": "S"}])
    agent.set_settings("Jev", {"enabled": False, "api_key": None, "base_url": "bad"})
    execution = agent.input("Evidence").output({"p": Probability("P?"), "summary": str})
    assert execution.get_data() == {"p": 0.5, "summary": "S"}
    assert ScriptedExecutionRequester.model_dispatches == 1
    assert not wire[0]
    assert "answer" not in execution.get_meta().get("judgment", {})["fields"]["p"]


def test_explicit_enabled_missing_key_preflight(tmp_path, wire):
    agent = create_execution_agent(tmp_path, "missing-key", [])
    agent.set_settings("Jev.enabled", True)
    agent.set_settings("system_one", {"provider": "Jev"})
    execution = agent.input("Evidence").output({"probability": Probability("P?"), "summary": str})
    with pytest.raises(ValueError, match="probability.*API key"):
        execution.get_data()
    assert not wire[0]
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_failures_share_budget_across_stages(tmp_path, wire):
    calls, replies = wire
    replies.extend([httpx.ConnectError("first"), native(0.8), httpx.ConnectError("second")])
    execution = (
        enabled(tmp_path, [])
        .input("Evidence")
        .output({"a": Probability("A?"), "b": Probability("B?", from_output="a")})
    )
    with pytest.raises(httpx.ConnectError):
        execution.get_data(max_retries=1)
    assert len(calls) == 3
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_llm_output_validation_retries_after_observed_field(tmp_path):
    execution = (
        create_execution_agent(tmp_path, "llm-retry", [{"p": None}, {"p": 0.5}])
        .input("Evidence")
        .output({"p": Probability("P?")})
    )
    assert execution.get_data(max_retries=1) == {"p": 0.5}
    assert ScriptedExecutionRequester.model_dispatches == 2


@pytest.mark.parametrize("value", [None, True, -0.1, 1.1, float("nan")])
def test_probability_constraints_reject_invalid_values(value):
    request = Agently.create_request().output({"p": Probability("P?")})
    with pytest.raises(ValueError):
        request.prompt.to_output_model(strict_output=True).model_validate({"p": value})


def test_bounded_batches(tmp_path, wire):
    calls, replies = wire
    replies.extend([native(0.1), native(0.2)])
    agent = enabled(tmp_path, [])
    agent.set_settings("Jev.batch_size", 1)
    assert agent.input("Evidence").output({"a": Probability("A?"), "b": Probability("B?")}).get_data() == {
        "a": 0.1,
        "b": 0.2,
    }
    assert len(calls) == 2


def test_namespace_child_mapping_and_exact_kv_precedence():
    from agently.utils import Settings

    parent = Settings()
    parent.register_path_mappings("Provider", "plugins.Provider")
    parent.register_kv_mappings("Provider.enabled", False, {"feature.enabled": False})
    child = Settings(parent=parent)
    child.set_settings("Provider.model", "child-model")
    assert child.get("plugins.Provider.model") == "child-model"
    assert parent.get("plugins.Provider.model") is None
    child.set_settings("Provider.enabled", False)
    assert child.get("feature.enabled") is False
    assert child.get("plugins.Provider.enabled") is None
    child.load("json", '{"Provider.model": "loaded"}')
    assert child.get("plugins.Provider.model") == "loaded"
    assert child.get("Provider.model") == "loaded"


@pytest.mark.asyncio
async def test_cancel_settles_provider_and_stops_downstream(tmp_path, monkeypatch):
    import asyncio
    from agently.types.data import AttemptHandlers, AttemptDecision

    started = asyncio.Event()
    settled = asyncio.Event()

    def handlers(self, request_data):
        async def execute(state):
            started.set()
            try:
                await asyncio.Event().wait()
                yield "response", native(0.7)
            finally:
                settled.set()

        return AttemptHandlers(execute=execute, handle_error=lambda error, state: AttemptDecision.raise_error(error))

    monkeypatch.setattr(Jev, "build_request_handlers", handlers)
    execution = enabled(tmp_path, []).input("Evidence").output({"p": Probability("P?"), "summary": str})
    task = asyncio.create_task(execution.async_get_data())
    await asyncio.wait_for(started.wait(), 2)
    await execution.async_cancel(timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set()
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_extra_llm_fields_cannot_overwrite_native(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.7))
    execution = (
        enabled(tmp_path, [{"p": 0.1, "summary": "bad"}, {"field_0": "fixed"}])
        .input("Evidence")
        .output({"p": Probability("P?"), "summary": str})
    )
    assert execution.get_data(max_retries=1) == {"p": 0.7, "summary": "fixed"}
    assert len(calls) == 1


def test_fixed_cross_list_index_is_unambiguous():
    plan = JudgmentSchema({"a": [{"p": Probability("P?", from_output="b[0].name")}], "b": [{"name": str}]})
    field = plan.fields[("a", "*", "p")]
    assert plan.bind(field, ("a", 0, "p"), {"b": [{"name": "B"}]}) == "B"


def test_invalid_observed_dependency_is_repaired_before_jev(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.5))
    execution = (
        enabled(tmp_path, [{"name": None}, {"name": "Valid"}])
        .input("Evidence")
        .output({"name": str, "p": Probability("P?", from_output="name")})
    )
    assert execution.get_data(max_retries=1) == {"name": "Valid", "p": 0.5}
    assert len(calls) == 1
    assert ScriptedExecutionRequester.model_dispatches == 2
    assert calls[0]["questions"]["q0"]["instructions"]["target"] == "Valid"


def test_mixed_missing_llm_config_fails_before_jev(wire):
    agent = Agently.create_agent("no-llm")
    agent.set_settings("Jev", {"api_key": "fake"})
    agent.set_settings("system_one", {"provider": "Jev"})
    execution = agent.input("Evidence").output({"p": Probability("P?"), "summary": str})
    with pytest.raises(ValueError, match="LLM credentials"):
        execution.get_data()
    assert not wire[0]


def test_multiple_sources_bind_per_item_and_preserve_names(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.8, 0.2))
    execution = (
        enabled(tmp_path, [{"items": [{"name": "A", "message": "First"}, {"name": "B", "message": "Second"}]}])
        .input("Evidence")
        .output(
            {
                "items": [
                    {
                        "name": str,
                        "message": str,
                        "p": Probability("P?", from_output=["items[].name", "items[].message"]),
                    }
                ]
            }
        )
    )
    assert [item["p"] for item in execution.get_data()["items"]] == [0.8, 0.2]
    assert [q["instructions"]["target"] for q in calls[0]["questions"].values()] == [
        {"items[].name": "A", "items[].message": "First"},
        {"items[].name": "B", "items[].message": "Second"},
    ]


def test_multiple_sources_wait_for_all_producers(tmp_path, wire):
    calls, replies = wire
    replies.extend([native(0.8), native(0.4)])
    execution = (
        enabled(tmp_path, [{"name": "A"}])
        .input("Evidence")
        .output({"a": Probability("A?"), "name": str, "b": Probability("B?", from_output=["a", "name"])})
    )
    assert execution.get_data()["b"] == 0.4
    assert calls[1]["questions"]["q0"]["instructions"]["target"] == {"a": 0.8, "name": "A"}


def test_dynamic_remaining_fields_use_judgment_evidence(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.7))
    execution = (
        enabled(tmp_path, [{"items": [{"message": "M"}]}, {"field_0": "Summary"}])
        .input("Evidence")
        .output({"items": [{"message": str, "p": Probability("P?", from_output="items[].message")}], "summary": str})
    )
    assert execution.get_data() == {"items": [{"message": "M", "p": 0.7}], "summary": "Summary"}
    assert [stage["provider"] for stage in execution.get_meta().get("judgment", {})["stages"]] == [
        "ScriptedExecutionRequester",
        "Jev",
        "ScriptedExecutionRequester",
    ]
    assert "0.7" in str(ScriptedExecutionRequester.requests[-1]["info"])


@pytest.mark.parametrize("paths", [[], ["a", "a"], ["a", ""], ["a", 1]])
def test_invalid_source_arrays(paths):
    with pytest.raises(ValueError, match="from_output"):
        Probability("P?", from_output=paths)


def test_singleton_array_keeps_object_shape(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.4))
    execution = (
        enabled(tmp_path, [{"name": "A"}])
        .input("Evidence")
        .output({"name": str, "p": Probability("P?", from_output=["name"])})
    )
    assert execution.get_data()["p"] == 0.4
    assert calls[0]["questions"]["q0"]["instructions"]["target"] == {"name": "A"}


def test_after_output_shares_llm_request_but_is_not_sent_to_jev(tmp_path, wire):
    calls, replies = wire
    replies.append(native(0.8))
    execution = (
        enabled(tmp_path, [{"notes": "UNBOUND_OUTPUT_SENTINEL", "conclusion": "Bound conclusion"}])
        .input("Evidence")
        .output(
            {
                "notes": (str, "Visible supporting notes"),
                "conclusion": (str, "Conclusion based on notes"),
                "p": Probability("P?", from_output=["conclusion"], after_output=["notes"]),
            }
        )
    )
    assert execution.get_data()["p"] == 0.8
    assert ScriptedExecutionRequester.model_dispatches == 1
    assert list(ScriptedExecutionRequester.requests[0]["output"]) == ["notes", "conclusion"]
    assert "UNBOUND_OUTPUT_SENTINEL" not in json.dumps(calls)
    assert calls[0]["questions"]["q0"]["instructions"]["target"] == {"conclusion": "Bound conclusion"}


def test_after_judgment_waits_without_injecting_answer(tmp_path, wire):
    calls, replies = wire
    replies.extend([native(0.12345), native(0.8)])
    execution = (
        enabled(tmp_path, [])
        .input("Evidence")
        .output({"a": Probability("A?"), "b": Probability("B?", after_output="a")})
    )
    assert execution.get_data()["b"] == 0.8
    assert len(calls) == 2
    assert "0.12345" not in json.dumps(calls[1])


@pytest.mark.parametrize(
    "schema",
    [
        {"p": Probability("P?", after_output="p")},
        {"p": Probability("P?", after_output=["missing"])},
        {"a": Probability("A?", from_output="b"), "b": Probability("B?", after_output="a")},
    ],
)
def test_after_output_dependency_errors(schema):
    with pytest.raises(ValueError):
        JudgmentSchema(schema)


def test_provider_independent_template_uses_llm_alongside_jev(tmp_path, wire):
    from agently import OutputTemplate

    class ShortText(OutputTemplate):
        def to_schema(self):
            return (str, self.question, True, {"judgment": True})

    calls, replies = wire
    replies.append(native(0.7))
    execution = (
        enabled(tmp_path, [{"field_0": "Short answer"}])
        .input("Evidence")
        .output({"p": Probability("P?"), "answer": ShortText("Give a short answer", from_output="p")})
    )
    assert execution.get_data() == {"p": 0.7, "answer": "Short answer"}
    assert len(calls) == 1
    assert len(calls[0]["questions"]) == 1
    assert "0.7" in str(ScriptedExecutionRequester.requests[-1]["info"])


def test_after_output_fallback_keeps_dependencies(tmp_path):
    execution = (
        create_execution_agent(tmp_path, "fallback-deps", [{"notes": "N", "answer": "A"}, {"field_0": 0.4}])
        .input("Evidence")
        .output({"notes": str, "answer": str, "p": Probability("P?", from_output="answer", after_output="notes")})
    )
    assert execution.get_data() == {"notes": "N", "answer": "A", "p": 0.4}
    assert ScriptedExecutionRequester.model_dispatches == 2


def test_custom_template_can_return_structured_value(tmp_path):
    from agently import OutputTemplate
    from pydantic import BaseModel

    class Answer(BaseModel):
        label: str
        count: int

    class StructuredAnswer(OutputTemplate):
        def to_schema(self):
            return (Answer, self.question, True, {"judgment": True})

    execution = create_execution_agent(tmp_path, "structured-template", [{"answer": {"label": "A", "count": 2}}]).input("Evidence").output({"answer": StructuredAnswer("Return a label and count")})
    assert execution.get_data() == {"answer": {"label": "A", "count": 2}}
