import asyncio
import logging
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import agently
import pytest
import toml
import yaml
from typing_extensions import assert_type

from agently import Agent, Agently, TaskWorkspace, TriggerFlow
from agently.compatibility import (
    get_current_release_manifest,
    get_devtools_compatibility_manifest,
    get_skills_compatibility_manifest,
)
from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.application.AgentExecution import AgentExecutionContext
from agently.core.extension.ExtensionHandlers import ExtensionHandlers
from agently.core.model.ModelRequestResult import ModelRequestResult
from agently.core.model.ModelResponse import ModelResponse
from agently.core.model.Prompt import Prompt
from agently.core.runtime.RuntimeContext import bind_runtime_context
from agently.utils import Settings, SettingsNamespace
from agently.types.data import StreamingData
from agently.core.application.AgentExecution import AgentExecutionStream
from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.routing import HybridRoutePlanner
from agently.builtins.plugins.ActionFlow.TriggerFlowActionFlow import TriggerFlowActionFlow
from agently.builtins.plugins.ModelRequester.OpenAICompatible import OpenAICompatible


_RUNTIME_LOG_KEYS = (
    "debug",
    "runtime.show_model_logs",
    "runtime.show_action_logs",
    "runtime.show_tool_logs",
    "runtime.show_trigger_flow_logs",
    "runtime.show_runtime_logs",
    "runtime.httpx_log_level",
)


def test_package_exposes_release_version() -> None:
    pyproject = toml.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    expected_version = pyproject["project"]["version"]

    assert agently.version == expected_version
    assert Agently.version == expected_version


def test_public_core_instance_creation_styles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys.modules["__main__"],
        "__file__",
        str(tmp_path / "main.py"),
        raising=False,
    )
    anonymous_agent = Agent()
    direct_agent = Agent("direct-agent")
    factory_agent = Agently.create_agent("factory-agent")
    direct_flow = TriggerFlow(name="direct-flow")
    factory_flow = Agently.create_trigger_flow("factory-flow")
    direct_task_workspace = TaskWorkspace(tmp_path / "direct-task-workspace")
    direct_flow_execution = direct_flow.create_execution(record_store=False)

    assert isinstance(anonymous_agent.name, str)
    assert anonymous_agent.name
    assert direct_agent.name == "direct-agent"
    assert factory_agent.name == "factory-agent"
    assert anonymous_agent.record_store._backend is None
    assert direct_agent.record_store._backend is None
    assert factory_agent.record_store._backend is None
    for agent in (anonymous_agent, direct_agent, factory_agent):
        assert agent.task_workspace.root == (
            tmp_path / ".agently" / "task_workspaces" / agent.id
        ).resolve()
    assert direct_flow.name == "direct-flow"
    assert factory_flow.name == "factory-flow"
    assert direct_task_workspace.root == (tmp_path / "direct-task-workspace").resolve()
    assert "record_store" not in direct_flow_execution.get_runtime_resources()


def _snapshot_runtime_log_settings():
    return {key: Agently.settings.get(key, None) for key in _RUNTIME_LOG_KEYS}


def _restore_runtime_log_settings(snapshot):
    for key, value in snapshot.items():
        Agently.settings.set(key, value)
    level_name = Agently.settings.get("runtime.httpx_log_level", "WARNING")
    level = getattr(logging, str(level_name).upper(), logging.WARNING)
    logging.getLogger("httpx").setLevel(level)
    logging.getLogger("httpcore").setLevel(level)


@pytest.mark.asyncio
async def test_settings():
    Agently.set_settings("test", "test")
    assert Agently.settings["test"] == "test"


def test_agent_set_settings_preserves_fluent_agent_contract():
    agent = Agently.create_agent("settings-chain")

    configured_agent = agent.set_settings("custom.enabled", True)

    assert_type(configured_agent, Agent)
    assert configured_agent is agent
    assert configured_agent.settings.get("custom.enabled") is True
    assert callable(configured_agent.input)


def test_agently_set_api_key_and_alias_mapping():
    original_api_key = Agently.settings.get("agently.api_key", None)
    try:
        Agently.set_api_key("official-key")
        assert Agently.settings["agently.api_key"] == "official-key"

        Agently.set_settings("agently_api_key", "official-key-alias")
        assert Agently.settings["agently.api_key"] == "official-key-alias"
    finally:
        Agently.set_settings("agently.api_key", original_api_key)


def test_agent_activate_model_sets_default_model_key_for_requests():
    agent = Agently.create_agent("model-switcher")

    assert agent.activate_model("ollama-qwen2.5") is agent
    assert getattr(agent.request, "_model_key") == "ollama-qwen2.5"
    assert getattr(agent.create_request(), "_model_key") == "ollama-qwen2.5"
    assert getattr(agent.create_temp_request(), "_model_key") == "ollama-qwen2.5"

    assert getattr(agent.create_request(model_key="deepseek-v4"), "_model_key") == "deepseek-v4"

    agent.activate_model(None)
    assert getattr(agent.request, "_model_key") is None
    assert getattr(agent.create_request(), "_model_key") is None

    with pytest.raises(ValueError, match="non-empty model_key"):
        agent.activate_model("")


def test_action_executor_plugins_registered():
    plugin_list = Agently.plugin_manager.get_plugin_list("ActionExecutor")
    assert "LocalFunctionActionExecutor" in plugin_list
    assert "MCPActionExecutor" in plugin_list
    assert "CodeExecutionActionExecutor" in plugin_list
    assert "BashSandboxActionExecutor" in plugin_list


def test_action_runtime_and_flow_plugins_registered():
    runtime_plugins = Agently.plugin_manager.get_plugin_list("ActionRuntime")
    flow_plugins = Agently.plugin_manager.get_plugin_list("ActionFlow")
    plugin_map = Agently.plugin_manager.get_plugin_list()

    assert "AgentlyActionRuntime" in runtime_plugins
    assert "TriggerFlowActionFlow" in flow_plugins
    assert getattr(Agently.action_runtime, "name", "") == "AgentlyActionRuntime"
    assert getattr(Agently.action_flow, "name", "") == "TriggerFlowActionFlow"
    assert "ToolManager" not in plugin_map


def test_dynamic_task_plugin_registered():
    planner_plugins = Agently.plugin_manager.get_plugin_list("TaskDAGPlanner")
    task = Agently.create_dynamic_task(
        "demo",
        plan={
            "graph_id": "registered",
            "tasks": [{"id": "a", "kind": "local", "binding": "local_handler"}],
        },
        handlers={"local_handler": lambda context: context.task.id},
    )

    assert "AgentlyTaskDAGPlanner" in planner_plugins
    assert task.planner.name == "AgentlyTaskDAGPlanner"
    assert "local_handler" in task.resolver.keys()


def test_streaming_data_uses_is_complete_completion_field():
    item = StreamingData(path="reply", value="ok", is_complete=True)

    assert item.is_complete is True


def test_response_parser_records_complete_streaming_snapshot_fields():
    from agently.builtins.plugins.ResponseParser.AgentlyResponseParser import AgentlyResponseParser

    class FakePromptObject:
        output_format = "flat_markdown"
        output = {
            "step_result": (str, "status", True),
            "artifact_manifest": {
                "path": (str, "path", True),
                "sections": ([str], "sections", False),
            },
        }

    class FakePrompt:
        def to_prompt_object(self):
            return FakePromptObject()

        def to_output_model(self):
            return None

    async def empty_response_generator():
        if False:
            yield ("done", "")

    parser = AgentlyResponseParser(
        "snapshot-agent",
        "response-snapshot",
        cast(Any, FakePrompt()),
        empty_response_generator(),
        Settings({}),
    )

    step_item = parser._prepare_streaming_data_for_yield(
        StreamingData(path="step_result", value="wrote final.md", is_complete=True),
        "dot",
    )
    manifest_path_item = parser._prepare_streaming_data_for_yield(
        StreamingData(path="artifact_manifest.path", value="final.md", is_complete=True),
        "slash",
    )
    parser._prepare_streaming_data_for_yield(
        StreamingData(path="artifact_manifest.sections[0]", value="Data Boundary", is_complete=True),
        "dot",
    )

    assert step_item.path == "step_result"
    assert manifest_path_item.path == "/artifact_manifest/path"
    assert parser.full_result_data["parsed_result"] == {
        "step_result": "wrote final.md",
        "artifact_manifest": {
            "path": "final.md",
            "sections": ["Data Boundary"],
        },
    }
    assert parser.full_result_data["extra"]["streaming_snapshot"] is True
    assert parser.full_result_data["extra"]["parse_success"] is True


def test_model_response_direct_construction_warns_but_get_result_does_not():
    from agently.core import ModelRequest
    from agently.utils import DeprecationWarnings

    DeprecationWarnings.reset_registry()
    settings = Settings(name="DeprecatedModelResponseSettings", parent=Agently.settings)

    with pytest.warns(DeprecationWarning, match="ModelResponse is deprecated"):
        ModelResponse(
            "deprecated-response",
            Agently.plugin_manager,
            settings,
            Prompt(Agently.plugin_manager, settings),
            ExtensionHandlers(),
        )

    DeprecationWarnings.reset_registry()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = ModelRequest(Agently.plugin_manager, parent_settings=Agently.settings).input("hello").get_result()

    assert isinstance(result, ModelRequestResult)
    assert not any("ModelResponse is deprecated" in str(item.message) for item in caught)


def test_skills_executor_is_a_thin_application_facade_not_a_plugin():
    assert "SkillsExecutor" not in Agently.plugin_manager.get_plugin_list()
    assert not hasattr(Agently.skills_executor, "execute")
    assert not hasattr(Agently.skills_executor, "resolve_strategy")
    assert hasattr(Agently.skills_executor, "install_skills")
    assert hasattr(Agently.skills_executor, "build_context_pack")


def test_skills_facade_and_agents_share_the_canonical_skill_library():
    agent = Agently.create_agent("canonical-skill-library-agent")

    assert Agently.skills_executor.library is Agently.skill_library
    assert agent.skill_library is Agently.skill_library


def test_agent_can_create_dynamic_task():
    agent = Agently.create_agent("graph-agent")
    task = agent.create_dynamic_task(
        "demo",
        plan={
            "graph_id": "agent-task",
            "tasks": [{"id": "a", "kind": "local", "binding": "local_handler"}],
        },
        handlers={"local_handler": lambda context: context.task.id},
    )

    assert task.name == "graph-agent-DynamicTask"
    assert task.settings.parent is agent.settings


def test_agent_use_dynamic_task_fails_fast_with_migration_guidance():
    agent = Agently.create_agent("execution-timeout-agent")

    with pytest.raises(ValueError, match=r"Agent\.use_dynamic_task.*Agently\.create_dynamic_task.*TaskDAGExecutor"):
        agent.use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-execution-timeout",
                "tasks": [{"id": "a", "kind": "local", "binding": "local_handler"}],
            },
            handlers={"local_handler": lambda context: context.task.id},
        )

    assert not hasattr(agent, "_dynamic_task_candidates")


def test_agent_execution_use_dynamic_task_fails_fast_with_migration_guidance():
    agent = Agently.create_agent("execution-idle-stall-agent")
    execution = agent.create_execution()

    with pytest.raises(ValueError, match=r"AgentExecution\.use_dynamic_task.*Agently\.create_dynamic_task.*TaskDAGExecutor"):
        execution.use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-execution-idle-stall",
                "tasks": [{"id": "a", "kind": "local", "binding": "local_handler"}],
            },
            handlers={"local_handler": lambda context: context.task.id},
        )

    assert not hasattr(execution, "dynamic_task_candidates")


@pytest.mark.asyncio
async def test_agent_execution_stream_preserves_delta_event_structure():
    stream = AgentExecutionStream(
        execution_id="exec-output-policy",
        lineage={"task_id": "issue-intake", "step_id": "collect"},
    )

    await stream.emit(
        "model.text",
        "A",
        delta="A",
        event_type="delta",
        is_complete=False,
        source="model_request",
        meta={"response_id": "response-1", "field_path": "text"},
    )
    await stream.emit(
        "model.text",
        "AB",
        delta="B",
        event_type="delta",
        is_complete=False,
        source="model_request",
        meta={"response_id": "response-1", "field_path": "text"},
    )

    await stream.emit(
        "model.text",
        "ABC",
        delta="C",
        event_type="delta",
        is_complete=False,
        source="model_request",
        meta={"response_id": "response-1", "field_path": "text"},
    )

    assert len(stream.items) == 3
    item = stream.items[0]
    assert [item.delta for item in stream.items] == ["A", "B", "C"]
    assert [item.value for item in stream.items] == ["A", "AB", "ABC"]
    assert item.meta is not None
    assert "coalesced" not in item.meta
    assert item.meta["execution_id"] == "exec-output-policy"
    assert item.meta["lineage"]["task_id"] == "issue-intake"


@pytest.mark.asyncio
async def test_agent_execution_stream_default_delta_path_remains_uncoalesced():
    stream = AgentExecutionStream(execution_id="exec-output-default")

    await stream.emit("model.text", "A", delta="A", event_type="delta", is_complete=False)
    await stream.emit("model.text", "AB", delta="B", event_type="delta", is_complete=False)

    assert [item.delta for item in stream.items] == ["A", "B"]


@pytest.mark.asyncio
async def test_agent_execution_progress_is_visible_with_raw_stream_delivery():
    agent = Agently.create_agent("execution-output-policy-progress-agent")
    execution = agent.input("stream").create_execution()

    await execution.emit_stream(
        "model.text",
        "partial",
        delta="partial",
        event_type="delta",
        is_complete=False,
        route="model_request",
    )

    assert len(execution.stream.items) == 1
    assert execution.execution_context.last_progress_event is not None
    assert execution.execution_context.last_progress_event["stage"] == "model.text"

    await execution.close_streams()

    assert len(execution.stream.items) == 1
    assert "coalesced" not in (execution.stream.items[0].meta or {})


@pytest.mark.asyncio
async def test_agent_execution_structured_model_bridge_refreshes_progress_clock():
    class StructuredStreamItem:
        path = "artifact_manifest.sections[0]"
        wildcard_path = "artifact_manifest.sections[*]"
        indexes = [0]
        value = "data boundary"
        delta = None
        event_type = "done"
        is_complete = True

    agent = Agently.create_agent("execution-structured-bridge-progress-agent")
    execution = agent.input("structured stream").create_execution()

    assert execution.execution_context.last_progress_event is None

    await execution.bridge_model_stream_item(
        StructuredStreamItem(),
        route="model_request",
        meta={
            "response_id": "response-1",
            "request_run_id": "request-run-1",
            "model_run_id": "model-run-1",
        },
    )

    progress = execution.execution_context.last_progress_event
    assert progress is not None
    assert progress["stage"] == "artifact_manifest.sections[0]"
    assert progress["status"] == "completed"
    assert progress["event_type"] == "artifact_manifest.sections[0]"
    assert progress["response_id"] == "response-1"
    assert progress["run_id"] == "model-run-1"
    assert progress["meta"]["field_path"] == "artifact_manifest.sections[0]"
    assert progress["meta"]["wildcard_path"] == "artifact_manifest.sections[*]"
    assert any(item.path == "artifact_manifest.sections[0]" for item in execution.stream.items)
    assert not any(item.path.startswith("runtime.progress.") for item in execution.stream.items)


@pytest.mark.asyncio
async def test_openai_compatible_first_event_timeout_is_typed_stall():
    async def slow_generator():
        await asyncio.sleep(0.05)
        yield {"delta": "late"}

    requester = OpenAICompatible.__new__(OpenAICompatible)
    requester.plugin_settings = SettingsNamespace(
        Settings({"plugins": {"ModelRequester": {"OpenAICompatible": {"model": "deepseek-chat"}}}}),
        "plugins.ModelRequester.OpenAICompatible",
    )

    with pytest.raises(RuntimeStageStallError) as raised:
        async for _ in requester._aiter_with_first_token_timeout(slow_generator(), timeout_seconds=0.001):
            pass

    assert raised.value.stage == "response_first_event"
    assert raised.value.status == "stalled"
    assert raised.value.provider == "OpenAICompatible"
    assert raised.value.model == "deepseek-chat"


@pytest.mark.asyncio
async def test_openai_compatible_first_event_timeout_ignores_empty_sse_heartbeats():
    async def heartbeat_generator():
        while True:
            await asyncio.sleep(0.005)
            yield SimpleNamespace(event="message", data="  \n")

    requester = OpenAICompatible.__new__(OpenAICompatible)
    requester.plugin_settings = SettingsNamespace(
        Settings({"plugins": {"ModelRequester": {"OpenAICompatible": {"model": "deepseek-chat"}}}}),
        "plugins.ModelRequester.OpenAICompatible",
    )

    async def consume():
        async for _ in requester._aiter_with_first_token_timeout(
            heartbeat_generator(),
            timeout_seconds=0.05,
        ):
            pass

    with pytest.raises(RuntimeStageStallError) as raised:
        await asyncio.wait_for(consume(), timeout=0.5)

    assert raised.value.stage == "response_first_event"


@pytest.mark.asyncio
async def test_openai_compatible_first_event_timeout_does_not_wait_for_slow_cancellation_cleanup():
    async def cancellation_resistant_generator():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            raise
        yield SimpleNamespace(event="message", data="late")

    requester = OpenAICompatible.__new__(OpenAICompatible)
    requester.plugin_settings = SettingsNamespace(
        Settings({"plugins": {"ModelRequester": {"OpenAICompatible": {"model": "deepseek-chat"}}}}),
        "plugins.ModelRequester.OpenAICompatible",
    )
    started_at = asyncio.get_running_loop().time()

    with pytest.raises(RuntimeStageStallError) as raised:
        async for _ in requester._aiter_with_first_token_timeout(
            cancellation_resistant_generator(),
            timeout_seconds=0.05,
        ):
            pass

    elapsed_seconds = asyncio.get_running_loop().time() - started_at
    assert elapsed_seconds < 0.15
    assert raised.value.stage == "response_first_event"
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_openai_compatible_first_event_timeout_yields_first_meaningful_sse_frame():
    async def heartbeat_then_data_generator():
        yield SimpleNamespace(event="message", data="")
        await asyncio.sleep(0.005)
        yield SimpleNamespace(event="message", data='{"choices": [{"delta": {"content": "ready"}}]}')

    requester = OpenAICompatible.__new__(OpenAICompatible)
    items = [
        item
        async for item in requester._aiter_with_first_token_timeout(
            heartbeat_then_data_generator(),
            timeout_seconds=0.05,
        )
    ]

    assert [item.data for item in items] == ['{"choices": [{"delta": {"content": "ready"}}]}']


@pytest.mark.asyncio
async def test_openai_compatible_stream_idle_timeout_is_typed_stall():
    async def idle_generator():
        yield {"delta": "first"}
        await asyncio.sleep(0.05)
        yield {"delta": "late"}

    requester = OpenAICompatible.__new__(OpenAICompatible)
    requester.plugin_settings = SettingsNamespace(
        Settings({"plugins": {"ModelRequester": {"OpenAICompatible": {"model": "deepseek-chat"}}}}),
        "plugins.ModelRequester.OpenAICompatible",
    )

    with pytest.raises(RuntimeStageStallError) as raised:
        async for _ in requester._aiter_with_stream_idle_timeout(idle_generator(), timeout_seconds=0.001):
            pass

    assert raised.value.stage == "response_stream"
    assert raised.value.status == "stalled"
    assert raised.value.provider == "OpenAICompatible"


@pytest.mark.asyncio
async def test_openai_compatible_stream_idle_timeout_ignores_empty_sse_heartbeats():
    async def heartbeat_generator():
        yield SimpleNamespace(event="message", data='{"choices": [{"delta": {"content": "ready"}}]}')
        while True:
            await asyncio.sleep(0.005)
            yield SimpleNamespace(event="message", data="\t")

    requester = OpenAICompatible.__new__(OpenAICompatible)
    requester.plugin_settings = SettingsNamespace(
        Settings({"plugins": {"ModelRequester": {"OpenAICompatible": {"model": "deepseek-chat"}}}}),
        "plugins.ModelRequester.OpenAICompatible",
    )

    yielded = []

    async def consume():
        async for item in requester._aiter_with_stream_idle_timeout(
            heartbeat_generator(),
            timeout_seconds=0.05,
        ):
            yielded.append(item.data)

    with pytest.raises(RuntimeStageStallError) as raised:
        await asyncio.wait_for(consume(), timeout=0.5)

    assert yielded == ['{"choices": [{"delta": {"content": "ready"}}]}']
    assert raised.value.stage == "response_stream"


@pytest.mark.asyncio
async def test_action_runtime_planning_handler_timeout_is_typed_stage_stall():
    async def slow_planning_handler(_context, _request):
        await asyncio.sleep(0.05)
        return {"next_action": "response", "execution_commands": []}

    agent = Agently.create_agent("action-runtime-stage-stall-agent")
    runtime = agent.action.action_runtime
    context = AgentExecutionContext(
        execution_id="action-runtime-stall",
        lineage={"task_id": "issue-intake", "step_id": "plan"},
        limits={"max_model_requests": None, "max_nested_agent_steps": 0, "max_no_progress_seconds": 0.001},
    )

    with bind_runtime_context(agent_execution_context=context):
        with pytest.raises(RuntimeStageStallError) as raised:
            await runtime.async_generate_action_call(
                prompt=agent.request.prompt,
                settings=agent.settings,
                action_list=[{"name": "search", "desc": "Search issues.", "kwargs": {}}],
                planning_handler=slow_planning_handler,
                planning_protocol="native_tool_calls",
            )

    assert raised.value.stage == "tool_call_selection"
    assert raised.value.status == "stalled"
    assert raised.value.planning_protocol == "native_tool_calls"


@pytest.mark.asyncio
async def test_action_runtime_structured_planning_timeout_is_typed_stage_stall():
    async def slow_planning_handler(_context, _request):
        await asyncio.sleep(0.05)
        return {"next_action": "response", "execution_commands": []}

    agent = Agently.create_agent("action-runtime-structured-stage-stall-agent")
    runtime = agent.action.action_runtime
    context = AgentExecutionContext(
        execution_id="action-runtime-structured-stall",
        lineage={"task_id": "issue-intake", "step_id": "plan"},
        limits={"max_model_requests": None, "max_nested_agent_steps": 0, "max_no_progress_seconds": 0.001},
    )

    with bind_runtime_context(agent_execution_context=context):
        with pytest.raises(RuntimeStageStallError) as raised:
            await runtime.async_generate_action_call(
                prompt=agent.request.prompt,
                settings=agent.settings,
                action_list=[{"name": "search", "desc": "Search issues.", "kwargs": {}}],
                planning_handler=slow_planning_handler,
                planning_protocol="structured_plan",
            )

    assert raised.value.stage == "action_planning"
    assert raised.value.status == "stalled"
    assert raised.value.planning_protocol == "structured_plan"


@pytest.mark.asyncio
async def test_action_runtime_action_completion_refreshes_execution_progress(monkeypatch):
    agent = Agently.create_agent("action-runtime-action-progress-agent")
    runtime = agent.action.action_runtime
    execution_record_store_args = []
    original_create_execution = TriggerFlow.create_execution

    def capture_internal_execution_record_store(flow, *args, **kwargs):
        execution_record_store_args.append(kwargs.get("record_store"))
        return original_create_execution(flow, *args, **kwargs)

    monkeypatch.setattr(TriggerFlow, "create_execution", capture_internal_execution_record_store)

    @agent.action_func
    async def slow_first_action():
        await asyncio.sleep(0.08)
        return "first"

    @agent.action_func
    async def slow_second_action():
        await asyncio.sleep(0.08)
        return "second"

    context = AgentExecutionContext(
        execution_id="action-runtime-action-progress",
        lineage={"task_id": "issue-intake", "step_id": "execute"},
        limits={"max_model_requests": None, "max_nested_agent_steps": 0, "max_no_progress_seconds": 0.3},
    )
    handler = runtime.resolve_execution_handler()

    with bind_runtime_context(agent_execution_context=context):
        results = await handler(
            {"settings": agent.settings},
            {
                "action_calls": [
                    {"action_id": "slow_first_action", "action_input": {}},
                    {"action_id": "slow_second_action", "action_input": {}},
                ],
                "concurrency": 1,
                "planning_protocol": "structured_plan",
            },
        )

    assert [record["result"] for record in results] == ["first", "second"]
    completed_actions = [
        event
        for event in context.stage_events
        if event.get("event_type") == "action.completed"
    ]
    assert [event["meta"]["action_id"] for event in completed_actions] == [
        "slow_first_action",
        "slow_second_action",
    ]
    assert execution_record_store_args == [False]


@pytest.mark.asyncio
async def test_action_flow_close_timeout_is_typed_stage_stall():
    flow = TriggerFlowActionFlow(plugin_manager=Agently.plugin_manager, settings=Agently.settings)
    context = AgentExecutionContext(
        execution_id="action-flow-close-stall",
        lineage={"task_id": "issue-intake", "step_id": "execute"},
        limits={"max_model_requests": None, "max_nested_agent_steps": 0},
    )

    with bind_runtime_context(agent_execution_context=context):
        flow._record_agent_execution_progress("action_loop_close", "started", "structured_plan")
        error = flow._build_action_loop_close_stall(0.001, "structured_plan")

    assert error.stage == "action_loop_close"
    assert error.status == "stalled"
    assert error.timeout_seconds == 0.001
    assert error.planning_protocol == "structured_plan"


def test_prompt_draft_use_dynamic_task_fails_fast_with_execution_guidance():
    agent = Agently.create_agent("execution-sync-idle-stall-agent")

    with pytest.raises(ValueError, match=r"AgentExecution\.use_dynamic_task.*Agently\.create_dynamic_task"):
        agent.input("run graph").use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-execution-sync-idle-stall",
                "tasks": [{"id": "a", "kind": "local", "binding": "local_handler"}],
            },
            handlers={"local_handler": lambda context: context.task.id},
        )


@pytest.mark.asyncio
async def test_model_response_result_materialization_idle_timeout():
    class SlowResponseParser:
        def __init__(self, *_args, **_kwargs):
            self.full_result_data = {}

        async def async_get_text(self):
            await asyncio.sleep(0.05)
            return "late"

        async def async_get_data(self, *, type="parsed"):
            await asyncio.sleep(0.05)
            return {"type": type}

        async def async_get_data_object(self):
            await asyncio.sleep(0.05)
            return None

        async def async_get_meta(self):
            await asyncio.sleep(0.05)
            return {}

        def get_generator(self, **_kwargs):
            return iter(())

        async def get_async_generator(self, **_kwargs):
            if False:
                yield None

    class MinimalPromptGenerator:
        def __init__(self, *_args, **_kwargs):
            pass

        def to_text(self, *_args, **_kwargs):
            return ""

        def to_messages(self, *_args, **_kwargs):
            return []

        def to_prompt_object(self, *_args, **_kwargs):
            return {}

        def to_output_model(self, *_args, **_kwargs):
            return None

        def to_serializable_prompt_data(self, *_args, **_kwargs):
            return {}

        def to_json_prompt(self, *_args, **_kwargs):
            return "{}"

        def to_yaml_prompt(self, *_args, **_kwargs):
            return ""

    class FakePluginManager:
        def get_plugin(self, category, *_args):
            if category == "PromptGenerator":
                return MinimalPromptGenerator
            return SlowResponseParser

    async def empty_response_generator():
        if False:
            yield ("done", "")

    settings = Settings(
        {
            "plugins": {"ResponseParser": {"activate": "slow"}},
            "response": {"materialization_idle_timeout": 0.001},
        }
    )
    result = ModelRequestResult(
        "timeout-agent",
        "response-timeout",
        Prompt(cast(Any, FakePluginManager()), settings),
        empty_response_generator(),
        cast(Any, FakePluginManager()),
        settings,
        ExtensionHandlers(),
    )

    with pytest.raises(RuntimeStageStallError) as raised:
        await result.async_get_text()

    assert raised.value.stage == "final_response_text_materialization"
    assert raised.value.status == "stalled"


@pytest.mark.asyncio
async def test_model_response_materialization_refreshes_progress_clock_without_notify():
    class FastResponseParser:
        def __init__(self, *_args, **_kwargs):
            self.full_result_data = {}

        async def async_get_text(self):
            await asyncio.sleep(0)
            return "ready"

        async def async_get_data(self, *, type="parsed"):
            await asyncio.sleep(0)
            return {"type": type}

        async def async_get_data_object(self):
            await asyncio.sleep(0)
            return None

        async def async_get_meta(self):
            await asyncio.sleep(0)
            return {}

        def drain_runtime_observations(self):
            return []

    class MinimalPromptGenerator:
        def __init__(self, *_args, **_kwargs):
            pass

        def to_text(self, *_args, **_kwargs):
            return ""

        def to_messages(self, *_args, **_kwargs):
            return []

        def to_prompt_object(self, *_args, **_kwargs):
            return {}

        def to_output_model(self, *_args, **_kwargs):
            return None

        def to_serializable_prompt_data(self, *_args, **_kwargs):
            return {}

        def to_json_prompt(self, *_args, **_kwargs):
            return "{}"

        def to_yaml_prompt(self, *_args, **_kwargs):
            return ""

    class FakePluginManager:
        def get_plugin(self, category, *_args):
            if category == "PromptGenerator":
                return MinimalPromptGenerator
            return FastResponseParser

    async def empty_response_generator():
        if False:
            yield ("done", "")

    settings = Settings({"plugins": {"ResponseParser": {"activate": "fast"}}})
    result = ModelRequestResult(
        "progress-agent",
        "response-progress",
        Prompt(cast(Any, FakePluginManager()), settings),
        empty_response_generator(),
        cast(Any, FakePluginManager()),
        settings,
        ExtensionHandlers(),
    )
    context = AgentExecutionContext(
        execution_id="materialization-progress",
        lineage={"task_id": "task", "step_id": "execute"},
        limits={"max_model_requests": None, "max_nested_agent_steps": 0, "max_no_progress_seconds": 90},
    )
    notified_events: list[dict[str, Any]] = []
    context.set_progress_callback(lambda event: notified_events.append(event))
    context.record_progress(stage="action_loop_close", status="completed", event_type="action_loop_close.completed")

    with bind_runtime_context(agent_execution_context=context):
        text = await result.async_get_text()

    assert text == "ready"
    progress = context.last_progress_event
    assert progress is not None
    assert progress["stage"] == "final_response_text_materialization"
    assert progress["status"] == "completed"
    assert progress["event_type"] == "final_response_text_materialization.completed"
    assert progress["response_id"] == "response-progress"
    assert progress["meta"]["agent_name"] == "progress-agent"
    assert notified_events == [
        {
            "stage": "action_loop_close",
            "status": "completed",
            "event_type": "action_loop_close.completed",
            "run_id": None,
            "response_id": None,
            "monotonic_time": notified_events[0]["monotonic_time"],
            "meta": {},
        }
    ]


@pytest.mark.asyncio
async def test_hybrid_route_planner_treats_optional_skills_as_model_request_context():
    class FakeRequest:
        def __init__(self):
            self.payload: dict[str, Any] = {}
            self.output_format = None

        def input(self, payload):
            self.payload = payload
            return self

        def output(self, _schema, *, format="auto"):
            self.output_format = format
            return self

        async def async_start(self, **_kwargs):
            raise AssertionError("Skill context must not create a second route-selection request")

    class FakeAction:
        def get_action_list(self, tags=None):
            return [{"name": "lookup_release"}]

    class FakePrompt:
        def get(self, _key, default=None):
            return "prepare release notes"

    class FakeAgent:
        name = "fake-route-agent"
        action = FakeAction()

        def __init__(self):
            self.request = type("Request", (), {"prompt": FakePrompt()})()

        def _collect_skill_selectors(self, *, skills, mode):
            return ["release-checklist"] if mode == "model_decision" else []

        def _collect_skills_pack_selectors(self, *, skills_packs, mode):
            return []

        def create_temp_request(self):
            return FakeRequest()

    route, meta = await HybridRoutePlanner(cast(Any, FakeAgent())).select_route()

    assert route == "model_request"
    assert meta["skill_context"] is True
    assert meta["with_actions"] is True


@pytest.mark.asyncio
async def test_hybrid_route_planner_respects_allowed_routes_policy():
    class FakeRequest:
        def input(self, _payload):
            raise AssertionError("route policy should avoid ambiguous route model selection")

    class FakeAction:
        def get_action_list(self, tags=None):
            return [{"name": "lookup_release"}]

    class FakePrompt:
        def get(self, _key, default=None):
            return "prepare release notes"

    class FakeAgent:
        name = "fake-route-policy-agent"
        action = FakeAction()

        def __init__(self):
            self.request = type("Request", (), {"prompt": FakePrompt()})()

        def _collect_skill_selectors(self, *, skills, mode):
            return ["release-checklist"] if mode == "required" else []

        def _collect_skills_pack_selectors(self, *, skills_packs, mode):
            return []

        def create_temp_request(self):
            return FakeRequest()

    execution = type(
        "FakeExecution",
        (),
        {"options": {"route_policy": {"allowed_routes": ["model_request"]}}, "effective_options": {}},
    )()

    route, meta = await HybridRoutePlanner(cast(Any, FakeAgent()), execution=execution).select_route()

    assert route == "model_request"
    assert meta["with_actions"] is True
    assert meta["selected_by"] == "single_candidate"


@pytest.mark.asyncio
async def test_hybrid_route_planner_keeps_required_skills_on_model_request_route():
    class FakePrompt:
        def get(self, _key, default=None):
            return "prepare release notes"

    class FakeAgent:
        name = "fake-route-agent"
        action = None

        def __init__(self):
            self.request = type("Request", (), {"prompt": FakePrompt()})()

        def _collect_skill_selectors(self, *, skills, mode):
            return ["release-checklist"] if mode == "required" else []

        def _collect_skills_pack_selectors(self, *, skills_packs, mode):
            return []

    route, meta = await HybridRoutePlanner(cast(Any, FakeAgent())).select_route()

    assert route == "model_request"
    assert meta["skill_context"] is True


@pytest.mark.asyncio
async def test_dynamic_task_runs_submitted_plan():
    async def run_task(context):
        if context.dependency_results:
            return f"{ context.task.id }:{ context.dependency_results['a'] }"
        return f"{ context.task.id }:{ context.graph_input['value'] }"

    graph = {
        "graph_id": "main-package-workflow",
        "tasks": [
            {"id": "a", "kind": "local", "binding": "local_handler"},
            {"id": "b", "kind": "local", "binding": "local_handler", "depends_on": ["a"]},
        ],
        "semantic_outputs": {"final": "b"},
    }
    task = Agently.create_dynamic_task(
        "run planned graph",
        plan=graph,
        handlers={"local_handler": run_task},
    )

    snapshot = await task.async_run(graph_input={"value": "ok"}, timeout=1)

    assert snapshot["task_results"] == {"a": "a:ok", "b": "b:a:ok"}
    assert snapshot["semantic_outputs"]["final"]["task_id"] == "b"


@pytest.mark.asyncio
async def test_dynamic_task_model_output_schema_uses_agently_request_pipeline():
    schema = {
        "brief": (str, "customer-facing briefing", True),
        "next_update": (str, "next update timing", True),
    }

    class FakeModelRequest:
        def __init__(self):
            self.output_schema = None
            self.output_format = None
            self.start_kwargs = None

        def input(self, value):
            return self

        def instruct(self, value):
            return self

        def output(self, value, *, format="auto"):
            self.output_schema = value
            self.output_format = format
            return self

        async def async_start(self, **kwargs):
            self.start_kwargs = kwargs
            return {"brief": "Latency is resolved.", "next_update": "After duplicate checks finish."}

    request = FakeModelRequest()
    task = Agently.create_dynamic_task(
        "brief an incident",
        plan={
            "graph_id": "model-output-contract",
            "task_schema_version": "task_dag/v1",
            "tasks": [{"id": "write_brief", "kind": "model"}],
            "semantic_outputs": {"frontstage": "write_brief"},
        },
        model=request,
        output_schema=schema,
        ensure_keys=["brief", "next_update"],
    )

    snapshot = await task.async_run(timeout=1)

    assert request.output_schema == schema
    assert request.output_format is None
    assert request.start_kwargs == {"ensure_keys": ["brief", "next_update"]}
    assert snapshot["semantic_outputs"]["frontstage"]["result"]["brief"] == "Latency is resolved."


@pytest.mark.asyncio
async def test_dynamic_task_model_task_can_select_output_format():
    schema = {"html": (str, "render-ready HTML", True)}

    class FakeModelRequest:
        def __init__(self):
            self.output_schema = None
            self.output_format = None

        def input(self, _value):
            return self

        def instruct(self, _value):
            return self

        def output(self, value, *, format="auto"):
            self.output_schema = value
            self.output_format = format
            return self

        async def async_start(self, **_kwargs):
            return {"html": "<section>OK</section>"}

    request = FakeModelRequest()
    task = Agently.create_dynamic_task(
        "render a fragment",
        plan={
            "graph_id": "model-output-format",
            "task_schema_version": "task_dag/v1",
            "tasks": [
                {
                    "id": "render_html",
                    "kind": "model",
                    "inputs": {
                        "output_schema": schema,
                        "output_format": "flat_markdown",
                    },
                }
            ],
            "semantic_outputs": {"fragment": "render_html"},
        },
        model=request,
    )

    snapshot = await task.async_run(timeout=1)

    assert request.output_schema == schema
    assert request.output_format == "flat_markdown"
    assert snapshot["semantic_outputs"]["fragment"]["result"]["html"] == "<section>OK</section>"


@pytest.mark.parametrize("output_format", ["xml_field", "yaml_literal"])
@pytest.mark.asyncio
async def test_dynamic_task_model_task_accepts_new_structured_output_formats(output_format):
    schema = {"html": (str, "render-ready HTML", True)}

    class FakeModelRequest:
        def __init__(self):
            self.output_schema = None
            self.output_format = None

        def input(self, _value):
            return self

        def instruct(self, _value):
            return self

        def output(self, value, *, format="auto"):
            self.output_schema = value
            self.output_format = format
            return self

        async def async_start(self, **_kwargs):
            return {"html": "<section>OK</section>"}

    request = FakeModelRequest()
    task = Agently.create_dynamic_task(
        "render a fragment",
        plan={
            "graph_id": f"model-output-format-{output_format}",
            "task_schema_version": "task_dag/v1",
            "tasks": [
                {
                    "id": "render_html",
                    "kind": "model",
                    "inputs": {
                        "output_schema": schema,
                        "output_format": output_format,
                    },
                }
            ],
            "semantic_outputs": {"fragment": "render_html"},
        },
        model=request,
    )

    snapshot = await task.async_run(timeout=1)

    assert request.output_schema == schema
    assert request.output_format == output_format
    assert snapshot["semantic_outputs"]["fragment"]["result"]["html"] == "<section>OK</section>"


def test_dynamic_task_can_be_created_without_explicit_model_source():
    task = Agently.create_dynamic_task("needs planning")

    assert "model" in task.resolver.keys()
    assert "action" not in task.resolver.keys()
    assert task.planner.available_bindings == ("model",)


def test_dynamic_task_exposes_actions_only_when_explicit():
    task = Agently.create_dynamic_task("needs action", actions=Agently.action)

    assert "action" in task.resolver.keys()
    assert task.planner.available_bindings == ("model", "action")


def test_agent_execution_dynamic_task_route_is_removed_from_public_execution():
    agent = Agently.create_agent("execution-dag-agent")
    execution = agent.input("run submitted graph").create_execution()

    with pytest.raises(ValueError, match=r"AgentExecution\.use_dynamic_task.*no longer an AgentExecution route"):
        execution.use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "agent-execution-dag",
                "task_schema_version": "task_dag/v1",
                "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
            },
            handlers={"local_handler": lambda context: context.task.id},
        )

    assert not hasattr(execution, "dynamic_task_candidates")


def test_deprecated_action_manager_aliases_warn():
    with pytest.warns(DeprecationWarning):
        assert Agently.action.tool_manager is not None
    with pytest.warns(DeprecationWarning):
        assert Agently.action.action_manager is not None


def test_tool_manager_plugin_registration_warns():
    from agently.builtins.plugins.ToolManager.AgentlyToolManager import AgentlyToolManager
    from agently.core import PluginManager
    from agently.utils import Settings

    settings = Settings(name="DeprecatedToolManagerSettings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="DeprecatedToolManagerPluginManager")

    with pytest.warns(DeprecationWarning):
        plugin_manager.register("ToolManager", AgentlyToolManager)


def test_action_plugin_protocols_exported_for_third_party_plugins():
    from agently.types.plugins import (
        ActionExecutionHandler,
        ActionExecutor,
        ActionFlow,
        ActionPlanningHandler,
        ActionRuntime,
        StandardActionExecutionHandler,
        StandardActionPlanningHandler,
    )

    assert ActionExecutor is not None
    assert ActionRuntime is not None
    assert ActionFlow is not None
    assert ActionPlanningHandler is not None
    assert ActionExecutionHandler is not None
    assert StandardActionPlanningHandler is not None
    assert StandardActionExecutionHandler is not None


def test_agently_load_settings_file(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"

    config_path.write_text(
        yaml.safe_dump(
            {
                "test_main_package": {
                    "base_url": "${ENV.TEST_MAIN_PACKAGE_BASE_URL}",
                }
            }
        ),
        encoding="utf-8",
    )
    env_path.write_text("TEST_MAIN_PACKAGE_BASE_URL=https://example.com\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_MAIN_PACKAGE_BASE_URL", raising=False)

    Agently.load_settings("yaml_file", str(config_path), auto_load_env=True)

    assert Agently.settings["test_main_package.base_url"] == "https://example.com"


def test_agently_load_settings_file_applies_model_requester_alias(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.yaml"
    env_path = tmp_path / ".env"
    previous_short = Agently.settings.get("OpenAICompatible", None)
    previous_openai = Agently.settings.get("plugins.ModelRequester.OpenAICompatible", None)

    config_path.write_text(
        yaml.safe_dump(
            {
                "OpenAICompatible": {
                    "base_url": "${ENV.OPENAI_BASE_URL}",
                    "api_key": "${ENV.OPENAI_API_KEY}",
                    "model": "${ENV.OPENAI_MODEL}",
                }
            }
        ),
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_BASE_URL=https://example.com/v1",
                "OPENAI_API_KEY=sk-test",
                "OPENAI_MODEL=deepseek-chat",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    try:
        Agently.load_settings("yaml_file", str(config_path), auto_load_env=True)

        assert Agently.settings["OpenAICompatible.model"] == "deepseek-chat"
        assert Agently.settings["plugins.ModelRequester.OpenAICompatible.base_url"] == "https://example.com/v1"
        assert Agently.settings["plugins.ModelRequester.OpenAICompatible.api_key"] == "sk-test"
        assert Agently.settings["plugins.ModelRequester.OpenAICompatible.model"] == "deepseek-chat"
    finally:
        Agently.settings.set("OpenAICompatible", previous_short)
        Agently.settings.set("plugins.ModelRequester.OpenAICompatible", previous_openai)


def test_agently_load_settings_refresh_httpx_log_level(tmp_path):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    "httpx_log_level": "INFO",
                }
            }
        ),
        encoding="utf-8",
    )

    Agently.load_settings("yaml_file", str(config_path))

    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("httpcore").level == logging.INFO


def test_agently_set_debug_mapping_profiles():
    snapshot = _snapshot_runtime_log_settings()
    try:
        Agently.set_settings("debug", True)
        assert Agently.settings["debug"] == "simple"
        assert Agently.settings["runtime.show_model_logs"] == "simple"
        assert Agently.settings["runtime.show_action_logs"] == "simple"
        assert Agently.settings["runtime.show_tool_logs"] == "simple"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "simple"
        assert Agently.settings["runtime.show_runtime_logs"] == "simple"
        assert logging.getLogger("httpx").level == logging.WARNING

        Agently.set_settings("debug", "detail")
        assert Agently.settings["debug"] == "detail"
        assert Agently.settings["runtime.show_model_logs"] == "detail"
        assert Agently.settings["runtime.show_action_logs"] == "detail"
        assert Agently.settings["runtime.show_runtime_logs"] == "detail"
        assert logging.getLogger("httpx").level == logging.INFO

        Agently.set_settings("debug", False)
        assert Agently.settings["debug"] == "off"
        assert Agently.settings["runtime.show_model_logs"] == "off"
        assert Agently.settings["runtime.show_action_logs"] == "off"
        assert Agently.settings["runtime.show_tool_logs"] == "off"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "off"
        assert Agently.settings["runtime.show_runtime_logs"] == "off"
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        _restore_runtime_log_settings(snapshot)


def test_agently_load_settings_applies_debug_mapping(tmp_path):
    snapshot = _snapshot_runtime_log_settings()
    try:
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.safe_dump({"debug": "detail"}), encoding="utf-8")

        Agently.load_settings("yaml_file", str(config_path))

        assert Agently.settings["debug"] == "detail"
        assert Agently.settings["runtime.show_model_logs"] == "detail"
        assert Agently.settings["runtime.show_action_logs"] == "detail"
        assert Agently.settings["runtime.show_tool_logs"] == "detail"
        assert Agently.settings["runtime.show_trigger_flow_logs"] == "detail"
        assert Agently.settings["runtime.show_runtime_logs"] == "detail"
        assert logging.getLogger("httpx").level == logging.INFO
    finally:
        _restore_runtime_log_settings(snapshot)


def test_request_quick_prompt_supports_key_value_and_kwargs():
    request = Agently.create_request()

    request.info("context", "Public-facing API handler", framework="FastAPI")

    assert request.prompt.to_prompt_object().info == {
        "context": "Public-facing API handler",
        "framework": "FastAPI",
    }


def test_request_quick_prompt_preserves_explicit_mappings():
    request = Agently.create_request()

    request.instruct("Hello ${name}", mappings={"name": "Alice"})

    assert request.prompt.to_prompt_object().instruct == "Hello Alice"


def test_devtools_compatibility_manifest_declares_runtime_protocol():
    manifest = get_devtools_compatibility_manifest()

    assert manifest["companion_package"] == "agently-devtools"
    assert manifest["runtime_protocol"].startswith("agently-devtools.observation-runtime.v")
    assert manifest["recommended_version_specifier"]
    assert manifest["framework_version"] == get_current_release_manifest()["framework_version"]


def test_skills_compatibility_manifest_declares_authoring_protocols():
    manifest = get_skills_compatibility_manifest()

    assert manifest["repository"] == "Agently-Skills"
    assert manifest["authoring_protocol"].startswith("agently-skills.authoring.v")
    assert manifest["devtools_guidance_protocol"].startswith(
        "agently-skills.devtools-guidance.v"
    )


def test_agent_quick_prompt_supports_key_value_and_kwargs():
    agent = Agently.create_agent()

    agent.info("context", "Public-facing API handler", framework="FastAPI", always=True)

    assert agent.agent_prompt.to_prompt_object().info == {
        "context": "Public-facing API handler",
        "framework": "FastAPI",
    }
