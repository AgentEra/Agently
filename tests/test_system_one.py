"""Synthetic orchestration evidence; real-model effects are in the experiment report."""

from copy import deepcopy

import pytest
from test_builtin_agent_executions import ScriptedExecutionRequester, create_execution_agent

from agently import Probability
from agently.builtins.plugins.AgentExecution.modules.system_one import SystemOne
from agently.types.settings import SystemOneSettings
from agently.utils import Settings


class SmallRequester(ScriptedExecutionRequester):
    name = "SmallRequester"
    configurations = []

    def generate_request_data(self):
        type(self).configurations.append(deepcopy(self.settings.get(f"plugins.ModelRequester.{self.name}")))
        return super().generate_request_data()


def agent_with_models(tmp_path, ordinary, small):
    agent = create_execution_agent(tmp_path, "system-one", ordinary)
    SmallRequester.reset(small)
    SmallRequester.configurations = []
    agent.plugin_manager.register("ModelRequester", SmallRequester, activate=False)
    agent.set_settings("system_one", {"provider": "SmallRequester", "model": "small-model"})
    agent.set_settings("plugins.ModelRequester.ScriptedExecutionRequester.model", "ordinary-model")
    return agent


def test_configured_system_one_routes_only_template_stages(tmp_path):
    agent = agent_with_models(tmp_path, [{"field_0": "Summary"}], [{"field_0": 0.7}])
    execution = agent.input("Evidence").output({"p": Probability("P?"), "summary": str})
    assert execution.get_data() == {"p": 0.7, "summary": "Summary"}
    meta = execution.get_meta().get("judgment", {})
    assert [(stage["model"], stage["system_one"]) for stage in meta["stages"]] == [
        ("small-model", True),
        ("ordinary-model", False),
    ]
    assert not meta["native"]
    assert "answer" not in meta["fields"]["p"]
    assert "0.7" in str(ScriptedExecutionRequester.requests[-1]["info"])


def test_explicit_off_combines_ordinary_output_and_does_not_mutate_agent(tmp_path):
    agent = agent_with_models(tmp_path, [{"p": 0.4, "summary": "S"}], [])
    execution = agent.use_system_one(False).input("Evidence").output({"p": Probability("P?"), "summary": str})
    assert execution.get_data() == {"p": 0.4, "summary": "S"}
    assert SmallRequester.model_dispatches == 0
    assert SystemOne(agent.settings).enabled
    assert not execution.get_meta().get("judgment", {})["system_one"]["enabled"]


def test_unconfigured_is_off_even_with_jev_credentials(tmp_path):
    agent = create_execution_agent(tmp_path, "off", [{"p": 0.2}])
    agent.set_settings("Jev", {"api_key": "unused"})
    execution = agent.input("Evidence").output({"p": Probability("P?")})
    assert execution.get_data() == {"p": 0.2}
    assert not execution.get_meta().get("judgment", {})["system_one"]["enabled"]


def test_explicit_on_without_model_fails_before_dispatch(tmp_path):
    agent = create_execution_agent(tmp_path, "missing", [])
    with pytest.raises(ValueError, match="without a model"):
        agent.input("E").use_system_one(True).output({"p": Probability("P?")}).get_data()
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_dynamic_before_and_after_stay_on_ordinary_model(tmp_path):
    agent = agent_with_models(
        tmp_path, [{"notes": "Not bound", "items": [{"name": "A"}]}, {"field_0": "Summary"}], [{"field_0": 0.9}]
    )
    execution = agent.input("Evidence").output(
        {
            "notes": str,
            "items": [{"name": str, "p": Probability("P?", from_output="items[].name", after_output="notes")}],
            "summary": str,
        }
    )
    assert execution.get_data()["summary"] == "Summary"
    assert [stage["model"] for stage in execution.get_meta().get("judgment", {})["stages"]] == [
        "ordinary-model",
        "small-model",
        "ordinary-model",
    ]
    assert "Not bound" not in str(SmallRequester.requests[0].get("info"))


def test_same_provider_settings_and_prompt_options_are_isolated(tmp_path):
    agent = create_execution_agent(tmp_path, "same-provider", [{"field_0": 0.8}, {"field_0": "S"}])
    agent.set_settings(
        "plugins.ModelRequester.ScriptedExecutionRequester",
        {
            "model": "ordinary",
            "api_key": "ordinary-secret",
            "request_options": {"reasoning": True},
            "headers": {"x-ordinary": "value"},
        },
    )
    agent.set_settings(
        "system_one",
        {"provider": "ScriptedExecutionRequester", "model": "fast", "request_options": {"enable_thinking": False}},
    )
    captured = []
    original = ScriptedExecutionRequester.generate_request_data

    def capture(self):
        captured.append(self.settings.get("plugins.ModelRequester.ScriptedExecutionRequester"))
        return original(self)

    from unittest.mock import patch

    with patch.object(ScriptedExecutionRequester, "generate_request_data", capture):
        execution = agent.input("Evidence").output({"p": Probability("P?"), "summary": str})
        execution.request.prompt.set("options", {"reasoning": True})
        assert execution.get_data()["p"] == 0.8
    assert captured[0]["model"] == "fast"
    assert captured[0].get("api_key") is None
    assert captured[0].get("headers") is None
    assert captured[0]["request_options"] == {"enable_thinking": False}
    assert "options" not in ScriptedExecutionRequester.requests[0]
    assert captured[1]["api_key"] == "ordinary-secret"
    assert ScriptedExecutionRequester.requests[1]["options"] == {"reasoning": True}


def test_model_key_uses_existing_profiles_without_ordinary_settings(tmp_path):
    agent = agent_with_models(tmp_path, [], [{"field_0": 0.5}])
    agent.settings.set("system_one", {"model_key": "quick"})
    agent.set_settings("model_pool", {"quick": "fast-profile"})
    agent.set_settings(
        "model_profiles",
        {
            "fast-profile": {
                "provider": "SmallRequester",
                "model": "profile-small",
                "request_options": {"enable_thinking": False},
            }
        },
    )
    execution = agent.input("E").output({"p": Probability("P?")})
    assert execution.get_data() == {"p": 0.5}
    assert SmallRequester.configurations[0]["model"] == "profile-small"


@pytest.mark.parametrize(
    "config",
    [
        {"enabled": True},
        {"base_url": "http://localhost"},
        {"provider": "OpenAICompatible"},
        {"model_key": "unknown"},
        {"model_key": "key", "model": "ambiguous"},
    ],
)
def test_invalid_profiles_fail_preflight(config):
    settings = Settings()
    settings.set("system_one", config)
    with pytest.raises(ValueError):
        SystemOne(settings)


def test_disabled_invalid_config_is_unused():
    settings = Settings()
    settings.set("system_one", {"enabled": False, "model": 123, "base_url": "bad"})
    assert not SystemOne(settings).enabled


def test_no_templates_does_not_add_system_one_call(tmp_path):
    agent = agent_with_models(tmp_path, [{"summary": "S"}], [])
    assert agent.input("E").output({"summary": str}).get_data() == {"summary": "S"}
    assert SmallRequester.model_dispatches == 0


def test_typed_settings_namespace_and_local_override(tmp_path):
    agent = agent_with_models(tmp_path, [], [{"field_0": 0.1}])
    agent.set_settings(SystemOneSettings(provider="SmallRequester", model="typed", enabled=False))
    execution = agent.input("E").output({"p": Probability("P?")}).use_system_one(True)
    assert execution.get_data() == {"p": 0.1}
    assert SmallRequester.configurations[0]["model"] == "typed"
    assert not SystemOne(agent.settings).enabled


def test_started_execution_rejects_switch_and_preserves_result(tmp_path):
    agent = agent_with_models(tmp_path, [], [{"field_0": 0.8}])
    original = agent.input("Evidence").output({"p": Probability("P?")})
    assert original.get_data() == {"p": 0.8}
    with pytest.raises(RuntimeError, match="already started"):
        original.use_system_one(False)
    assert original.get_data() == {"p": 0.8}


def test_ordinary_profile_survives_failed_fast_stage(tmp_path):
    agent = agent_with_models(tmp_path, [], [{"field_0": None}])
    ordinary_before = deepcopy(agent.settings.get("plugins.ModelRequester.ScriptedExecutionRequester"))
    with pytest.raises(ValueError):
        agent.input("E").output({"p": Probability("P?"), "summary": str}).get_data(max_retries=0)
    assert ScriptedExecutionRequester.model_dispatches == 0
    assert agent.settings.get("plugins.ModelRequester.ScriptedExecutionRequester") == ordinary_before


def test_following_llm_receives_score_scale_and_choice_meanings(tmp_path):
    from agently import Choice, Score

    agent = agent_with_models(tmp_path, [{"field_0": "S"}], [{"field_0": 2.0, "field_1": "billing"}])
    execution = agent.input("Evidence").output(
        {
            "urgency": Score("How urgent?", ["None", "Moderate", "Same-day"]),
            "team": Choice("Which team?", {"billing": "Payments and refunds", "sales": "Purchases"}),
            "summary": str,
        }
    )
    assert execution.get_data()["urgency"] == 2.0
    evidence = str(ScriptedExecutionRequester.requests[-1]["info"])
    assert "Same-day" in evidence and "Payments and refunds" in evidence
    meta = execution.get_meta().get("judgment", {})
    assert meta["fields"]["urgency"]["contract"]["schema"]["maximum"] == 2


def test_explicit_system_one_batch_size_retains_precedence(tmp_path):
    from agently.builtins.plugins.AgentExecution.modules.judgment_flow import _JudgmentOutput
    from agently.builtins.plugins.AgentExecution.modules.production import ProductionOptions
    agent = create_execution_agent(tmp_path, "batch-precedence", [])
    agent.set_settings("Jev", {"api_key": "synthetic", "batch_size": 1})
    agent.set_settings("system_one", {"provider": "Jev", "batch_size": 64})
    execution = agent.input("E").output({"p": Probability("P?")})
    runtime = _JudgmentOutput(execution, ProductionOptions())
    assert runtime.batch_size == 64


def test_system_one_can_retry_before_any_complete_instant_field(tmp_path):
    agent = agent_with_models(tmp_path, [], [{"field_0": 2.0}, {"field_0": 0.5}])
    execution = agent.input("Evidence").output({"p": Probability("P?")})
    assert execution.get_data(max_retries=1) == {"p": 0.5}
    assert SmallRequester.model_dispatches == 2
