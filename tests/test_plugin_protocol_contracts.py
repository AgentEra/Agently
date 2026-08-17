from __future__ import annotations

import inspect
import importlib
from pathlib import Path

import agently.types.plugins as plugin_types

from agently.builtins.plugins.ActionExecutor import (
    BashSandboxActionExecutor,
    BrowseActionExecutor,
    CodeExecutionActionExecutor,
    DockerActionExecutor,
    LocalFunctionActionExecutor,
    MCPActionExecutor,
    SearchActionExecutor,
    SQLiteActionExecutor,
)
from agently.builtins.plugins.ActionFlow import TriggerFlowActionFlow
from agently.builtins.plugins.ActionRuntime import AgentlyActionRuntime
from agently.builtins.plugins.AgentOrchestrator import AgentlyAgentOrchestrator
from agently.builtins.plugins.ExecutionResourceProvider import (
    BashExecutionResourceProvider,
    BrowserExecutionResourceProvider,
    DockerExecutionResourceProvider,
    GVisorDockerExecutionResourceProvider,
    SeatbeltExecutionResourceProvider,
    MCPExecutionResourceProvider,
    SQLiteExecutionResourceProvider,
    TrustedLocalExecutionResourceProvider,
)
from agently.builtins.plugins.ModelRequester.AnthropicCompatible import AnthropicCompatible
from agently.builtins.plugins.ModelRequester.OpenAICompatible import OpenAICompatible
from agently.builtins.plugins.ModelRequester.OpenAIResponsesCompatible import OpenAIResponsesCompatible
from agently.types.plugins import (
    ActionExecutor,
    ActionFlow,
    ActionRuntime,
    AgentExecution,
    AgentOrchestrator,
    ContextSource,
    ExecutionResourceProvider,
    SessionMemory,
)
from agently.utils.Settings import Settings


def _method_names(protocol: type) -> list[str]:
    return [
        name
        for name, value in protocol.__dict__.items()
        if not name.startswith("_") and inspect.isfunction(value)
    ]


def test_context_source_protocol_is_descriptor_and_exact_read_only():
    change_feed = getattr(plugin_types, "ContextSourceChangeFeed", None)

    assert not hasattr(plugin_types, "ContextSourceCandidateWindow")
    assert not hasattr(ContextSource, "async_list_candidates")
    assert not hasattr(ContextSource, "async_read")
    assert hasattr(ContextSource, "async_enumerate_descriptors")
    assert hasattr(ContextSource, "async_read_exact")
    assert {"source_id", "source_kind"}.issubset(ContextSource.__annotations__)
    assert change_feed is not None
    assert _method_names(change_feed) == ["async_changes"]


def test_session_memory_protocol_exposes_context_source_instead_of_retrieval_policy():
    methods = _method_names(SessionMemory)

    assert "create_context_source" in methods
    assert "prepare_request" in methods
    assert "retrieve" not in methods
    assert "rerank" not in methods


def test_builtin_execution_resource_providers_match_protocol():
    providers = [
        BashExecutionResourceProvider(),
        BrowserExecutionResourceProvider(),
        DockerExecutionResourceProvider(),
        GVisorDockerExecutionResourceProvider(),
        SeatbeltExecutionResourceProvider(),
        MCPExecutionResourceProvider(),
        SQLiteExecutionResourceProvider(),
        TrustedLocalExecutionResourceProvider(),
    ]

    for provider in providers:
        assert isinstance(provider, ExecutionResourceProvider)
        assert provider.provider_id
        assert provider.supported_kinds
        for method_name in _method_names(ExecutionResourceProvider):
            assert callable(getattr(provider, method_name))


def test_builtin_action_executors_match_protocol():
    class FakeSearch:
        async def search(self, *_args, **_kwargs):  # pragma: no cover - protocol shape only
            return {}

    class FakeBrowse:
        async def browse(self, *_args, **_kwargs):  # pragma: no cover - protocol shape only
            return {}

    executors = [
        LocalFunctionActionExecutor(lambda: None),
        MCPActionExecutor(action_id="protocol_mcp", transport={"type": "direct", "tools": []}),
        BashSandboxActionExecutor(timeout=1),
        SearchActionExecutor(search=FakeSearch(), method_name="search"),
        BrowseActionExecutor(browse=FakeBrowse()),
        CodeExecutionActionExecutor(language="python", timeout=1),
        DockerActionExecutor(timeout=1),
        SQLiteActionExecutor(),
    ]

    for executor in executors:
        assert isinstance(executor, ActionExecutor)
        assert executor.kind
        for method_name in _method_names(ActionExecutor):
            assert callable(getattr(executor, method_name))


def test_builtin_action_runtime_and_flow_match_protocols():
    settings = Settings(name="protocol-action-runtime")

    class FakeAction:
        pass

    runtime = AgentlyActionRuntime(action=FakeAction(), plugin_manager=None, settings=settings)
    flow = TriggerFlowActionFlow(plugin_manager=None, settings=settings)

    assert isinstance(runtime, ActionRuntime)
    assert isinstance(flow, ActionFlow)
    for method_name in _method_names(ActionRuntime):
        assert callable(getattr(runtime, method_name))
    for method_name in _method_names(ActionFlow):
        assert callable(getattr(flow, method_name))


def test_builtin_agent_orchestrator_matches_protocol():
    orchestrator = AgentlyAgentOrchestrator(plugin_manager=None, settings=Settings(name="protocol-orchestrator"))

    assert isinstance(orchestrator, AgentOrchestrator)
    for method_name in _method_names(AgentOrchestrator):
        assert callable(getattr(orchestrator, method_name))


def test_builtin_agent_execution_matches_protocol_without_core_builtin_dependency():
    from agently import Agently
    from agently.core.application.AgentExecution import AgentExecutionStream
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.stream import (
        AgentExecutionStream as CompatAgentExecutionStream,
    )

    agent = Agently.create_agent("protocol-agent-execution")
    execution = agent.input("protocol smoke").create_execution()

    assert isinstance(execution, AgentExecution)
    assert CompatAgentExecutionStream is AgentExecutionStream
    for method_name in _method_names(AgentExecution):
        assert callable(getattr(execution, method_name))

    protocol_source = (
        Path(__file__).resolve().parents[1] / "agently" / "types" / "plugins" / "AgentExecution.py"
    ).read_text(encoding="utf-8")
    assert "builtins" not in protocol_source
    assert "AgentlyAgentOrchestrator" not in protocol_source


def test_model_requester_runtime_handler_contract_imports_and_ownership():
    model_requester_root = Path(__file__).resolve().parents[1] / "agently" / "builtins" / "plugins" / "ModelRequester"
    requesters = {
        "OpenAICompatible": OpenAICompatible,
        "OpenAIResponsesCompatible": OpenAIResponsesCompatible,
        "AnthropicCompatible": AnthropicCompatible,
    }

    for requester_name, requester in requesters.items():
        requester_package = model_requester_root / requester_name
        requester_module = importlib.import_module(f"agently.builtins.plugins.ModelRequester.{requester_name}")
        assert getattr(requester_module, requester_name) is requester
        assert requester_package.is_dir()
        assert not (model_requester_root / f"{requester_name}.py").exists()
        assert (requester_package / "plugin.py").exists()
        for module_name in [
            "request_builder.py",
            "credential.py",
            "transport.py",
            "handlers.py",
            "response_adapter.py",
        ]:
            assert (requester_package / "modules" / module_name).exists()
        assert len((requester_package / "plugin.py").read_text(encoding="utf-8").splitlines()) < 180
        assert callable(getattr(requester, "build_request_handlers"))

    builtin_root = Path(__file__).resolve().parents[1] / "agently" / "builtins"
    source_files = list(builtin_root.rglob("*.py"))
    forbidden_terms = [
        "from agently.base import async_emit_runtime",
        "from agently.base import emit_runtime",
        "from agently.core.runtime import async_emit_action_flow_observation",
        "from agently.core.runtime import async_emit_model_requester_error",
        "from agently.core.runtime import async_emit_response_parser_observation",
        "from agently.core.runtime import async_emit_session_observation",
        "from agently.core.runtime import emit_session_observation",
        "await async_emit_runtime(",
        "emit_runtime(",
        "event_center.create_emitter(",
        "_emitter.async_error(",
    ]
    for source_file in source_files:
        source = source_file.read_text(encoding="utf-8")
        assert [term for term in forbidden_terms if term in source] == []
