from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager, SkillLibrary, TaskBoardGraph, TaskBoardRevision, TaskBoardValidator, build_task_board_evidence_view
from agently.core.application.AgentExecution import AgentExecutionLimitExceeded, AgentExecutionResult
from agently.core.application.AgentExecution.Stream import project_agent_execution_text_delta
from agently.core.application.AgentTask import AgentTask
from agently.builtins.plugins.AgentExecution.long_task.BlockCarrier import WorkUnitResult
from agently.builtins.plugins.AgentExecution.long_task.TaskShared import _AgentTaskDeadlineExceeded
from agently.core.application.SkillLibrary import SkillBinding, SkillContextSource
from agently.types.data import AgentExecutionStreamData, AgentlyRequestData
from agently.types.options import ExecutionOptions
from agently.utils import DataFormatter
from agently.utils import Settings
from agently.builtins.plugins.AgentExecution.modules.result_views import (
    get_async_generator as agent_execution_get_async_generator,
)


def _action_terminal_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the default Action-or-Response terminal decision fixture."""

    return {
        "next_action": "response",
        "execution_commands": [],
        "response": json.dumps(payload, ensure_ascii=False),
    }


@pytest.mark.parametrize(
    "relative_path",
    [
        "examples/agent_task_experiments/06_auto_mixed_travel_planning.py",
        "examples/agent_task_experiments/07_auto_mixed_equity_analysis.py",
        "examples/agent_task_experiments/08_auto_mixed_business_analysis.py",
    ],
)
def test_auto_mixed_examples_forward_task_options_through_auto_strategy(relative_path: str) -> None:
    source = (Path(__file__).parents[1] / relative_path).read_text(encoding="utf-8")

    assert '.strategy("auto", options=options)' in source
    assert ".create_execution(options=options)" not in source


class MockAgentExecutionRequester:
    name = "MockAgentExecutionRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}
    requests: list[str] = []

    def __init_subclass__(cls, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("request_model")
        if original is None:
            return

        async def request_model_with_semantic_contract(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" not in text:
                async for event in original(self, request_data):
                    yield event
                return
            events = [event async for event in original(self, request_data)]
            response_text = "".join(str(data) for event, data in events if event == "message")
            try:
                payload = json.loads(response_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                for event in events:
                    yield event
                return
            if isinstance(payload, dict):
                payload = self._complete_semantic_verifier_fixture(text, payload)
            yield "message", json.dumps(payload, ensure_ascii=False)

        cls.request_model = request_model_with_semantic_contract

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
            request_options={"stream": True},
            request_url="mock://agent-execution",
        )

    @staticmethod
    def _complete_semantic_verifier_fixture(
        text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        criterion_ids = list(dict.fromkeys(re.findall(r"criterion:\d+", text)))
        satisfied = payload.get("is_complete") is True and not payload.get("missing_criteria")
        payload["criterion_checks"] = [
            {
                "criterion_id": criterion_id,
                "satisfied": satisfied,
                "summary": str(payload.get("reason") or "Synthetic lifecycle verifier fixture."),
                "gaps": list(payload.get("missing_criteria") or []) if not satisfied else [],
                "evidence_ids": [],
            }
            for criterion_id in criterion_ids
        ]
        payload.setdefault("material_claim_coverage_complete", True)
        payload.setdefault("material_claim_checks", [])
        return payload

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "Synthetic lifecycle verifier accepted the fixture.",
                "missing_criteria": [],
                "final_result_required": True,
                "final_result": f"ok-{len(MockAgentExecutionRequester.requests)}",
            }
            payload = self._complete_semantic_verifier_fixture(text, payload)
        else:
            payload = {
                "answer": f"ok-{ len(MockAgentExecutionRequester.requests) }",
                "status": "ready",
            }
        yield "message", json.dumps(payload, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text


class MockLateStructuredRequester(MockAgentExecutionRequester):
    name = "MockLateStructuredRequester"
    final_payload: dict[str, Any] = {
        "answer": "Drafted the support escalation reply.",
        "artifact_manifest": {"path": "support_reply.md"},
        "file_refs": ["support_reply.md"],
        "evidence": [{"id": "ev-1", "summary": "Duplicate webhook deliveries confirmed."}],
        "evidence_use": [{"evidence_id": "ev-1", "claim": "No known data loss."}],
        "acceptance_points": [{"criterion": "Reply and checklist are present.", "satisfied": True}],
        "remaining_work": [],
        "self_check": "All requested sections are present.",
        "short_summary": "Support reply drafted and checked.",
        "progress_message": "Draft and validation complete.",
        "diagnostics": [],
    }
    natural_terminal_emitted = False
    cancelled_before_terminal = False

    @staticmethod
    def _on_register() -> None:
        MockAgentExecutionRequester.requests = []
        MockLateStructuredRequester.natural_terminal_emitted = False
        MockLateStructuredRequester.cancelled_before_terminal = False

    async def request_model(self, request_data: AgentlyRequestData):
        MockAgentExecutionRequester.requests.append(
            json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        )
        response_text = json.dumps(self.final_payload, ensure_ascii=False, separators=(",", ":"))
        split_marker = ',"file_refs"'
        first_part, final_part = response_text.split(split_marker, 1)
        yield "message", first_part
        await asyncio.sleep(0)
        yield "message", split_marker + final_part

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        try:
            async for event, data in response_generator:
                if event == "message":
                    response_text += str(data)
                    yield "delta", str(data)
                    await asyncio.sleep(0)
            type(self).natural_terminal_emitted = True
            yield "done", response_text
            yield "meta", {
                "provider": "mock-late-structured",
                "usage": {"total_tokens": 321},
            }
        except asyncio.CancelledError:
            type(self).cancelled_before_terminal = True
            raise


class MockAgentExecutionRetryStatusRequester(MockAgentExecutionRequester):
    name = "MockAgentExecutionRetryStatusRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        MockAgentExecutionRequester.requests.append(
            json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        )
        yield "message", "partial attempt"
        yield "status", {
            "status": "failed",
            "attempt_index": 1,
            "retry": True,
            "next_attempt_index": 2,
            "reason": "transient provider disconnect",
        }
        yield "message", "replacement"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
            elif event == "status":
                yield "status", data
        yield "done", response_text


class MockFailingAgentExecutionRequester(MockAgentExecutionRequester):
    name = "MockFailingAgentExecutionRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        _ = request_data
        raise RuntimeError("real-route-provider-failure:" + "故障" * 5000)
        yield  # pragma: no cover


class _FakeStreamForGeneratorCancel:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.queues: list[asyncio.Queue[Any]] = []


class _FakeExecutionForGeneratorCancel:
    def __init__(self) -> None:
        self._completed = False
        self.stream = _FakeStreamForGeneratorCancel()

    async def async_start(self) -> None:
        await asyncio.sleep(0.01)
        for queue in list(self.stream.queues):
            await queue.put(None)
        raise RuntimeError("synthetic stream start failure")


class MockAgentExecutionActionRequester(MockAgentExecutionRequester):
    name = "MockAgentExecutionActionRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockAgentExecutionActionRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "next_action" in text and "execution_commands" in text:
            MockAgentExecutionActionRequester.action_planning_calls += 1
            if "answer directly without using the required action" in text:
                payload = _action_terminal_response({"answer": "direct answer", "status": "ready"})
            elif MockAgentExecutionActionRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "response": None,
                    "execution_commands": [
                        {
                            "purpose": "Run allowlisted echo command",
                            "action_id": "echo_cli",
                            "action_input": {"cmd": "echo allowed action-output"},
                            "todo_suggestion": "Respond after echo completes.",
                        }
                    ],
                }
            else:
                payload = _action_terminal_response({"answer": "used-action", "status": "ready"})
        elif "[ACTION RESULTS]" in text:
            payload = {"answer": "used-action", "status": "ready"}
        else:
            payload = {"answer": "plain-text-delta", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockScopedActionRequester(MockAgentExecutionRequester):
    name = "MockScopedActionRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "next_action" in text and "execution_commands" in text:
            action_id = "blocked_action" if "blocked_action" in text else "allowed_action"
            payload = {
                "next_action": "execute",
                "response": None,
                "execution_commands": [
                    {
                        "purpose": f"Run {action_id}",
                        "action_id": action_id,
                        "action_input": {},
                    }
                ],
            }
        elif "[ACTION RESULTS]" in text:
            payload = {"answer": "used scoped action", "status": "ready"}
        else:
            payload = {"answer": "plain-text-delta", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_agent_execution_stream_cancel_retrieves_start_exception():
    event_loop = asyncio.get_running_loop()
    captured: list[dict[str, Any]] = []
    previous_handler = event_loop.get_exception_handler()
    event_loop.set_exception_handler(lambda _, context: captured.append(context))
    try:
        owner = _FakeExecutionForGeneratorCancel()
        generator = agent_execution_get_async_generator(cast(Any, owner))
        pending = asyncio.create_task(generator.__anext__())
        await asyncio.sleep(0)
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0.03)
    finally:
        event_loop.set_exception_handler(previous_handler)

    assert captured == []


@pytest.mark.asyncio
async def test_agent_execution_instant_adds_synthetic_delta_projection_without_polluting_all():
    phase_item = AgentExecutionStreamData(
        path="agent_task.phase.planned",
        value={"phase": "planned", "iteration": 1},
        event_type="done",
        is_complete=True,
        source="agent_task",
        route="task",
        task_id="task-1",
        meta={
            "stream_kind": "phase",
            "execution_id": "exec-1",
            "lineage": {"task_id": "task-1"},
        },
    )
    owner = SimpleNamespace(
        _completed=True,
        stream=SimpleNamespace(items=[phase_item]),
    )

    instant_items = [
        item async for item in agent_execution_get_async_generator(cast(Any, owner), type="instant")
    ]
    delta_chunks = [
        item async for item in agent_execution_get_async_generator(cast(Any, owner), type="delta")
    ]
    all_items = [
        item async for item in agent_execution_get_async_generator(cast(Any, owner), type="all")
    ]

    assert instant_items[0] is phase_item
    synthetic_delta = instant_items[1]
    assert isinstance(synthetic_delta, AgentExecutionStreamData)
    assert synthetic_delta.path == "$delta"
    assert synthetic_delta.delta == "Iteration 1: plan accepted.\n\n"
    assert synthetic_delta.value == synthetic_delta.delta
    assert synthetic_delta.event_type == "delta"
    assert synthetic_delta.is_complete is False
    assert synthetic_delta.source == "agent_execution"
    assert synthetic_delta.route == "task"
    assert synthetic_delta.task_id == "task-1"
    assert synthetic_delta.meta == {
        "stream_kind": "text_projection",
        "projection_source_path": "agent_task.phase.planned",
        "projection_source_stream_kind": "phase",
        "execution_id": "exec-1",
        "lineage": {"task_id": "task-1"},
        "projection_source_event_type": "done",
        "projection_source": "agent_task",
    }
    assert delta_chunks == ["Iteration 1: plan accepted.\n\n"]
    assert all_items == [("agent_execution", phase_item)]


@pytest.mark.asyncio
async def test_agent_execution_instant_keeps_heartbeat_structured_without_synthetic_delta():
    heartbeat_item = AgentExecutionStreamData(
        path="agent_task.heartbeat",
        value={"stage": "plan", "status": "running"},
        event_type="done",
        is_complete=True,
        source="agent_task",
        route="task",
        task_id="task-1",
        meta={
            "stream_kind": "heartbeat",
            "execution_id": "exec-1",
            "lineage": {"task_id": "task-1"},
        },
    )
    owner = SimpleNamespace(
        _completed=True,
        stream=SimpleNamespace(items=[heartbeat_item]),
    )

    instant_items = [
        item async for item in agent_execution_get_async_generator(cast(Any, owner), type="instant")
    ]
    delta_chunks = [
        item async for item in agent_execution_get_async_generator(cast(Any, owner), type="delta")
    ]
    all_items = [
        item async for item in agent_execution_get_async_generator(cast(Any, owner), type="all")
    ]

    assert instant_items == [heartbeat_item]
    assert delta_chunks == []
    assert all_items == [("agent_execution", heartbeat_item)]


@pytest.mark.asyncio
async def test_agent_execution_instant_uses_stateful_flat_plan_projection():
    context_item = AgentExecutionStreamData(
        path="agent_task.iteration.1.snapshot.context",
        value={
            "iteration": 1,
            "stage": "context",
            "snapshot": {"context_item_count": 2},
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        route="task",
        task_id="task-1",
        meta={"stream_kind": "snapshot", "stage": "context", "iteration": 1, "task_id": "task-1"},
    )
    plan_item = AgentExecutionStreamData(
        path="agent_task.iteration.1.snapshot.plan",
        value={
            "iteration": 1,
            "stage": "plan",
            "snapshot": {"step_instruction": "Read the source file and draft the final answer."},
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        route="task",
        task_id="task-1",
        meta={"stream_kind": "snapshot", "stage": "plan", "iteration": 1, "task_id": "task-1"},
    )
    owner = SimpleNamespace(
        _completed=True,
        stream=SimpleNamespace(items=[context_item, plan_item]),
    )

    instant_items = [
        item async for item in agent_execution_get_async_generator(cast(Any, owner), type="instant")
    ]

    delta_items = [item for item in instant_items if isinstance(item, AgentExecutionStreamData) and item.path == "$delta"]
    assert len(delta_items) == 2
    assert str(delta_items[0].delta) == "Iteration 1: context is ready with 2 item(s).\n\n"
    assert str(delta_items[1].delta).endswith("\n\n")
    assert "🧭 Iteration 1 — Plan ready" in str(delta_items[1].delta)
    assert "Previous completed action: prepared the working context with 2 item(s)." in str(delta_items[1].delta)
    assert "Current action plan: Read the source file and draft the final answer." in str(delta_items[1].delta)
    assert "| State | Step | Detail |" not in str(delta_items[1].delta)


class MockAgentExecutionIsolationRequester(MockAgentExecutionRequester):
    name = "MockAgentExecutionIsolationRequester"
    active_requests = 0
    max_active_requests = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockAgentExecutionIsolationRequester.active_requests = 0
        MockAgentExecutionIsolationRequester.max_active_requests = 0

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={
                "input": self.prompt.get("input"),
                "system": self.prompt.get("system"),
                "output": self.prompt.get("output"),
            },
            request_options={"stream": True},
            request_url="mock://agent-execution-isolation",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        MockAgentExecutionIsolationRequester.active_requests += 1
        MockAgentExecutionIsolationRequester.max_active_requests = max(
            MockAgentExecutionIsolationRequester.max_active_requests,
            MockAgentExecutionIsolationRequester.active_requests,
        )
        try:
            await asyncio.sleep(0.1)
            payload = DataFormatter.sanitize(request_data.data)
            prompt_input = payload.get("input")
            persistent_system = payload.get("system")
            MockAgentExecutionRequester.requests.append(json.dumps(payload, ensure_ascii=False))
            yield "message", json.dumps(
                {
                    "answer": prompt_input,
                    "system": persistent_system,
                },
                ensure_ascii=False,
            )
        finally:
            MockAgentExecutionIsolationRequester.active_requests -= 1


def _create_agent(name: str = "agent-execution-step-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_late_structured_agent(name: str = "agent-execution-late-structured-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockLateStructuredRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_retry_status_agent(name: str = "agent-execution-retry-status-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionRetryStatusRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_failing_agent(name: str = "agent-execution-failing-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFailingAgentExecutionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_execution_isolation_agent(name: str = "agent-execution-isolation-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionIsolationRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_action_agent(name: str = "agent-execution-action-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockAgentExecutionActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_scoped_action_agent(name: str = "agent-execution-scoped-action-test"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockScopedActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def test_task_context_contract_is_ref_backed_and_cap_free(tmp_path):
    agent = _create_agent("execution-task-context-contract").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Prepare a current source-grounded report.",
        success_criteria=["Use current source evidence without hot-loading large resources."],
        execution="flat",
        max_iterations=None,
    )

    contract = task._task_context_contract()
    work_unit = task._build_flat_work_unit_intent(
        1,
        {
            "step_instruction": "Collect latest evidence as refs and read only scoped snippets.",
            "deliverable_mode": "task_workspace_artifact",
        },
        {
            "goal": task.goal,
            "items": [],
            "profile": "test",
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert contract["schema_version"] == "agent_task_context_contract/v1"
    assert contract["current_time"]["utc"]
    assert "run_date_utc" not in contract
    intermediate_policy = contract["intermediate_resource_policy"]
    assert set(intermediate_policy["cold_resource_kinds"]) == {
        "download",
        "webpage_snapshot",
        "search_note",
        "generated_code",
        "large_extraction",
        "task_workspace_note",
    }
    assert intermediate_policy["default_state"] == "ref_only"
    assert "compact refs" in intermediate_policy["hot_path"]
    assert "TaskWorkspace or Action artifacts" in intermediate_policy["hot_path"]
    assert "max_bytes" in intermediate_policy["readback"]
    assert "offsets" in intermediate_policy["readback"]
    assert "discovery or materialization only" in intermediate_policy["evidence_boundary"]
    assert "hard_execution_caps" in contract["resource_policy"]
    assert "max_iterations" not in json.dumps(contract, ensure_ascii=False)
    assert work_unit.input_payload["task_context_contract"]["schema_version"] == contract["schema_version"]
    assert work_unit.delivery_contract["task_context_contract"]["schema_version"] == contract["schema_version"]
    prompt_contract = task._task_context_contract_for_model_prompt()
    assert prompt_contract["current_time"]["visibility"] == "omitted_from_default_model_hot_path"
    assert "utc" not in prompt_contract["current_time"]
    assert "current_time.utc" not in json.dumps(prompt_contract, ensure_ascii=False)
    assert work_unit.input_payload["task_context_contract"]["current_time"]["visibility"] == (
        "omitted_from_default_model_hot_path"
    )


@pytest.mark.asyncio
async def test_taskboard_work_units_receive_task_context_contract(tmp_path):
    agent = _create_agent("execution-taskboard-context-contract").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Prepare a current source-grounded report.",
        success_criteria=["Use current source evidence without hot-loading large resources."],
        execution="taskboard",
        max_iterations=None,
    )
    captured_work_units: list[dict[str, Any]] = []

    async def fake_run_work_unit_through_blocks(**kwargs: Any) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        work_unit = cast(Any, kwargs["work_unit"])
        captured_work_units.append(work_unit.to_dict())
        output: dict[str, Any] = {
            "status": "completed",
            "answer": "contract captured",
            "evidence": [],
            "remaining_work": [],
        }
        if str(work_unit.id).endswith(":control"):
            output["sufficient"] = True
        return (
            output,
            {"status": "completed", "logs": {"action_logs": {}, "route_logs": {}, "errors": []}},
            WorkUnitResult(id=str(work_unit.id), status="completed"),
        )

    async def fake_dependency_readbacks(*_args: Any, **_kwargs: Any):
        return {"schema_version": "agent_task_taskboard_dependency_readbacks/v1", "readbacks": []}

    cast(Any, task)._run_work_unit_through_blocks = fake_run_work_unit_through_blocks
    cast(Any, task)._taskboard_dependency_action_artifact_readbacks = fake_dependency_readbacks
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "profile": "test",
        "omitted": [],
        "diagnostics": {},
    }
    revision = TaskBoardRevision.create(
        board_id="taskboard-context-contract",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "taskboard-context-contract-graph",
                "cards": [
                    {
                        "id": "collect",
                        "objective": "Collect current source evidence as refs and notes.",
                        "allowed_execution_shape": "actions",
                    },
                    {
                        "id": "synthesize",
                        "objective": "Synthesize from bounded readback and source refs.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                    },
                ],
            }
        ),
    )
    cards = revision.graph.card_by_id()

    await task._run_taskboard_agent_card(
        SimpleNamespace(revision=revision, card=cards["collect"], dependency_results={}, planning_policy=None),
        context_pack,
    )
    await task._run_taskboard_control_card(
        SimpleNamespace(revision=revision, card=cards["synthesize"], dependency_results={}, planning_policy=None),
        context_pack,
    )

    assert len(captured_work_units) == 2
    for work_unit in captured_work_units:
        payload_contract = work_unit["input_payload"]["task_context_contract"]
        assert payload_contract["schema_version"] == "agent_task_context_contract/v1"
        assert payload_contract["current_time"]["visibility"] == "omitted_from_default_model_hot_path"
        assert "utc" not in payload_contract["current_time"]
        assert payload_contract["intermediate_resource_policy"]["default_state"] == "ref_only"
        assert work_unit["delivery_contract"]["task_context_contract"]["schema_version"] == (
            "agent_task_context_contract/v1"
        )


@pytest.mark.asyncio
async def test_taskboard_readback_card_does_not_promote_skill_procedure_as_evidence(tmp_path):
    agent = _create_agent("execution-taskboard-skill-context-readback").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Summarize agently-request setup guidance.",
        success_criteria=["The summary uses the required Skill context."],
        execution="taskboard",
    )
    captured_work_units: list[dict[str, Any]] = []

    async def fake_run_work_unit_through_blocks(**kwargs: Any) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        work_unit = cast(Any, kwargs["work_unit"])
        captured_work_units.append(work_unit.to_dict())
        raw = await kwargs["handler"]({})
        return (
            raw["execution_result"],
            raw["execution_meta"],
            WorkUnitResult(id=str(work_unit.id), status=str(raw["execution_result"]["status"])),
        )

    cast(Any, task)._run_work_unit_through_blocks = fake_run_work_unit_through_blocks
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "profile": "test",
        "omitted": [],
        "diagnostics": {},
        "skill_projection": {
            "schema_version": "agently.skills.context_pack.v1",
            "skills": [
                {
                    "skill_id": "agently-request",
                    "guidance": {
                        "path": "SKILL.md",
                        "excerpt": "Use Agently model settings and output control for request setup.",
                        "citation": "skills/agently-request/SKILL.md",
                    },
                    "selected_resources": [
                        {
                            "path": "references/model-setup.md",
                            "kind": "reference",
                            "content": "Configure a provider model key before creating a request.",
                            "citation": "skills/agently-request/references/model-setup.md",
                            "truncated": False,
                        }
                    ],
                }
            ],
        },
    }
    revision = TaskBoardRevision.create(
        board_id="taskboard-skill-context-readback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "taskboard-skill-context-readback-graph",
                "cards": [
                    {
                        "id": "read-skill",
                        "objective": "Read the required Skill references.",
                        "allowed_execution_shape": "readback",
                        "input_refs": [
                            "skills/agently-request/SKILL.md",
                            "skills/agently-request/references/model-setup.md",
                        ],
                        "evidence_contract": {
                            "evidence_to_use": [
                                "skills/agently-request/SKILL.md",
                                "skills/agently-request/references/model-setup.md",
                            ]
                        },
                    }
                ],
            }
        ),
    )
    card = revision.graph.card_by_id()["read-skill"]

    result = await task._run_taskboard_readback_card(
        SimpleNamespace(revision=revision, card=card, dependency_results={}, planning_policy=None),
        context_pack,
    )

    assert result.status == "blocked"
    assert "skill_context_readbacks" not in result.preview
    assert result.preview["remaining_work"]
    evidence_items = result.metadata["evidence_ledger"]["items"]
    assert evidence_items == []
    assert captured_work_units[0]["input_refs"] == []


def _write_skill(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        """---
name: Task Step Skill
description: Use for task step contract smoke tests.
---

# Task Step Skill

Return a short structured answer for the task step contract.
""",
        encoding="utf-8",
    )


def _taskboard_verification_payload(final_result: str = "taskboard accepted result") -> dict[str, Any]:
    return {
        "is_complete": True,
        "requires_block": False,
        "reason": "TaskBoard final evidence satisfies the success criteria.",
        "failure_analysis": "",
        "acceptance_delta": [],
        "missing_criteria": [],
        "repair_constraints": [],
        "next_step_requirements": [],
        "replan_instruction": "",
        "final_result_required": True,
        "final_result": final_result,
    }


def _install_site_skill(tmp_path: Path) -> Path:
    skill_root = tmp_path / "skill-pack" / "skills" / "website-builder"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: Website Builder
description: Use to plan and verify small website deliverables.
---

# Website Builder

Help build a small product website from supplied facts.
""",
        encoding="utf-8",
    )
    return tmp_path / "skill-pack"


class MockGoalPursuitRequester(MockAgentExecutionRequester):
    """Model-driven mock for the goal-pursuit (AgentTask) execution path.

    Drives plan -> bounded step -> verify through real model output rather than
    patching production AgentTask methods, so the execution->AgentTask wiring is
    exercised end to end.
    """

    name = "MockGoalPursuitRequester"
    final_result = "accepted result"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "run one bounded step",
                "expected_evidence": "final output evidence",
                "rationale": "one bounded step is enough",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": MockGoalPursuitRequester.final_result,
                "evidence": ["evidence recorded"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            final_result = MockGoalPursuitRequester.final_result
            if "summary" in text:
                final_result = json.dumps({"summary": MockGoalPursuitRequester.final_result}, ensure_ascii=False)
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "model verifier accepted the evidence",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": final_result,
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatReplanRequester(MockAgentExecutionRequester):
    name = "MockFlatReplanRequester"
    verify_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatReplanRequester.verify_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "dynamic_task",
                "step_instruction": "collect one piece of evidence",
                "expected_evidence": "evidence exists",
                "rationale": "the mock intentionally asks for DAG to prove flat host policy wins",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "flat bounded step result",
                "evidence": ["flat evidence"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            MockFlatReplanRequester.verify_calls += 1
            if MockFlatReplanRequester.verify_calls == 1:
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "first pass lacks enough evidence",
                    "missing_criteria": ["needs one more bounded pass"],
                    "replan_instruction": "Run one more flat bounded step.",
                    "final_result_required": True,
                    "final_result": "",
                }
            else:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "second pass is enough",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "flat accepted result",
                }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatRepairConstraintRequester(MockAgentExecutionRequester):
    name = "MockFlatRepairConstraintRequester"
    plan_calls = 0
    verify_calls = 0
    second_plan_prompt = ""
    second_execution_prompt = ""
    latest_step_instruction = ""

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatRepairConstraintRequester.plan_calls = 0
        MockFlatRepairConstraintRequester.verify_calls = 0
        MockFlatRepairConstraintRequester.second_plan_prompt = ""
        MockFlatRepairConstraintRequester.second_execution_prompt = ""
        MockFlatRepairConstraintRequester.latest_step_instruction = ""

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            MockFlatRepairConstraintRequester.plan_calls += 1
            if MockFlatRepairConstraintRequester.plan_calls == 1:
                step_instruction = "Draft a broad weekly report with all collected items."
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": step_instruction,
                    "expected_evidence": "Candidate report.",
                    "rationale": "Start with an initial report draft.",
                }
            else:
                MockFlatRepairConstraintRequester.second_plan_prompt = text
                if "repair_context" in text and "Reduce the report to 5-8 news items." in text:
                    step_instruction = (
                        "Revise the candidate report to contain 5-8 news items and keep existing grounded evidence."
                    )
                else:
                    step_instruction = "Repeat the broad weekly report draft without repair context."
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": step_instruction,
                    "expected_evidence": "Repaired report that satisfies the 5-8 item constraint.",
                    "rationale": "The latest verifier repair_context requires reducing the report to 5-8 items.",
                }
            MockFlatRepairConstraintRequester.latest_step_instruction = step_instruction
        elif "Execute exactly one bounded step" in text:
            if "Revise the candidate report to contain 5-8 news items" in text:
                MockFlatRepairConstraintRequester.second_execution_prompt = text
                payload = {
                    "step_result": "Repaired report produced.",
                    "candidate_final_result": "# Weekly\n\n" + "\n".join(f"- Item {index}" for index in range(1, 7)),
                    "evidence": ["repaired to 6 items"],
                    "remaining_work": [],
                }
            else:
                payload = {
                    "step_result": "Initial oversized report produced.",
                    "candidate_final_result": "# Weekly\n\n" + "\n".join(f"- Item {index}" for index in range(1, 16)),
                    "evidence": ["initial report has 15 items"],
                    "remaining_work": [],
                }
        elif "Verify the task against every success criterion" in text:
            MockFlatRepairConstraintRequester.verify_calls += 1
            if MockFlatRepairConstraintRequester.verify_calls == 1:
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "The draft includes 15 items but the task asks for 5-8.",
                    "failure_analysis": "The candidate artifact overshoots the accepted item count.",
                    "acceptance_delta": ["The report must include 5-8 news items, not 15."],
                    "missing_criteria": ["The report must include 5-8 news items, not 15."],
                    "repair_constraints": ["Reduce the report to 5-8 news items."],
                    "next_step_requirements": ["Revise the candidate report; do not restart evidence gathering."],
                    "replan_instruction": "Revise the report to satisfy the item-count constraint.",
                    "final_result_required": True,
                    "final_result": "",
                }
            else:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "The repaired report now has 6 items.",
                    "failure_analysis": "",
                    "acceptance_delta": [],
                    "missing_criteria": [],
                    "repair_constraints": [],
                    "next_step_requirements": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "repaired accepted result",
                }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatActionRequester(MockAgentExecutionRequester):
    name = "MockFlatActionRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatActionRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Call the probe_action framework Action.",
                "expected_evidence": "probe_action result",
                "rationale": "The task needs action evidence.",
            }
        elif "next_action" in text and "execution_commands" in text:
            MockFlatActionRequester.action_planning_calls += 1
            if MockFlatActionRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "response": None,
                    "execution_commands": [
                        {
                            "purpose": "Collect probe action evidence.",
                            "action_id": "probe_action",
                            "action_input": {},
                        }
                    ],
                }
            else:
                payload = _action_terminal_response(
                    {
                        "step_result": "action evidence collected",
                        "evidence": ["probe_action executed"],
                        "remaining_work": [],
                    }
                )
        elif "[ACTION RESULTS]" in text:
            payload = {
                "step_result": "action evidence collected",
                "evidence": ["probe_action executed"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "action evidence is present",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "flat action accepted result",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatActionPlanningStallRequester(MockAgentExecutionRequester):
    name = "MockFlatActionPlanningStallRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Use the probe action.",
                "expected_evidence": "probe action result",
                "rationale": "The task needs action evidence.",
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": False,
                "requires_block": True,
                "reason": "execution timed out before action evidence was collected",
                "missing_criteria": ["Action planning timed out."],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "",
            }
        elif "next_action" in text and "execution_commands" in text:
            await asyncio.sleep(5)
            payload = {"next_action": "response", "execution_commands": []}
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatActionPostExecutionPlanningStallRequester(MockAgentExecutionRequester):
    name = "MockFlatActionPostExecutionPlanningStallRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatActionPostExecutionPlanningStallRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Use the probe action.",
                "expected_evidence": "probe action result",
                "rationale": "The task needs action evidence.",
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": False,
                "requires_block": True,
                "reason": "post-action planning timed out",
                "missing_criteria": ["Action loop did not finish."],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "",
            }
        elif "next_action" in text and "execution_commands" in text:
            MockFlatActionPostExecutionPlanningStallRequester.action_planning_calls += 1
            if MockFlatActionPostExecutionPlanningStallRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "response": None,
                    "execution_commands": [
                        {
                            "purpose": "Collect probe action evidence.",
                            "action_id": "probe_action",
                            "action_input": {},
                        }
                    ],
                }
            else:
                await asyncio.sleep(5)
                payload = {"next_action": "response", "execution_commands": []}
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatActionPlanningSlowRequester(MockAgentExecutionRequester):
    name = "MockFlatActionPlanningSlowRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatActionPlanningSlowRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Use the probe action.",
                "expected_evidence": "probe action result",
                "rationale": "The task needs action evidence.",
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "action evidence is present",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "flat action accepted result",
            }
        elif "[ACTION RESULTS]" in text:
            payload = {
                "step_result": "action evidence collected",
                "evidence": ["probe_action executed"],
                "remaining_work": [],
            }
        elif "next_action" in text and "execution_commands" in text:
            MockFlatActionPlanningSlowRequester.action_planning_calls += 1
            await asyncio.sleep(0.35)
            if MockFlatActionPlanningSlowRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "response": None,
                    "execution_commands": [
                        {
                            "purpose": "Collect probe action evidence.",
                            "action_id": "probe_action",
                            "action_input": {},
                        }
                    ],
                }
            else:
                payload = _action_terminal_response(
                    {
                        "step_result": "action evidence collected",
                        "evidence": ["probe_action executed"],
                        "remaining_work": [],
                    }
                )
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatSlowPlanRequester(MockAgentExecutionRequester):
    name = "MockFlatSlowPlanRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            await asyncio.sleep(0.2)
            payload = {
                "execution_shape": "direct",
                "step_instruction": "Return the final answer from known evidence.",
                "expected_evidence": "final answer",
                "rationale": "The task is small after planning.",
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "final answer is present",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "slow plan final answer",
            }
        else:
            payload = {
                "step_result": "slow plan evidence",
                "evidence": ["slow plan evidence"],
                "remaining_work": [],
                "ready_for_final_verification": True,
                "candidate_final_result": "slow plan final answer",
            }
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockWorkspaceArtifactDraftStallRequester(MockAgentExecutionRequester):
    name = "MockWorkspaceArtifactDraftStallRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Write only the final Markdown artifact body" in text:
            await asyncio.sleep(5)
            yield "message", "# Late artifact body"
            return
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "Prepare a TaskWorkspace-backed final artifact.",
                "expected_evidence": "final.md is written and read back.",
                "rationale": "The task requests a deliverable file.",
                "deliverable_mode": "task_workspace_artifact",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "Control result ready; framework should draft the TaskWorkspace artifact.",
                "artifact_manifest": {"path": "final.md", "sections": [{"id": "summary", "title": "Summary"}]},
                "evidence": ["artifact source evidence"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": False,
                "requires_block": True,
                "reason": "The TaskWorkspace artifact was not delivered.",
                "missing_criteria": ["final.md readback is missing."],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockWorkspaceArtifactDraftRetryRequester(MockAgentExecutionRequester):
    name = "MockWorkspaceArtifactDraftRetryRequester"
    draft_calls = 0
    draft_outputs: list[Any] = []

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockWorkspaceArtifactDraftRetryRequester.draft_calls = 0
        MockWorkspaceArtifactDraftRetryRequester.draft_outputs = []

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Write only the final Markdown artifact body" in text:
            MockWorkspaceArtifactDraftRetryRequester.draft_calls += 1
            MockWorkspaceArtifactDraftRetryRequester.draft_outputs.append(request_data.data.get("output"))
            yield "message", "# Partial\n\nThis attempt must be discarded."
            yield "status", {
                "status": "failed",
                "attempt_index": 1,
                "retry": True,
                "next_attempt_index": 2,
                "reason": "transient provider disconnect",
            }
            return
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "Prepare a TaskWorkspace-backed final artifact.",
                "expected_evidence": "final.md is written and read back.",
                "rationale": "The task requests a deliverable file.",
                "deliverable_mode": "task_workspace_artifact",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "Control result ready; framework should draft the TaskWorkspace artifact.",
                "artifact_manifest": {"path": "final.md", "sections": [{"id": "summary", "title": "Summary"}]},
                "evidence": ["artifact source evidence"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "The TaskWorkspace artifact was delivered after retry.",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "final.md",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
            elif event == "status":
                yield "status", data
        yield "done", response_text


class MockWorkspaceArtifactDraftNaturalTextRequester(MockAgentExecutionRequester):
    name = "MockWorkspaceArtifactDraftNaturalTextRequester"
    draft_outputs: list[Any] = []

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockWorkspaceArtifactDraftNaturalTextRequester.draft_outputs = []

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Write only the final Markdown artifact body" in text:
            MockWorkspaceArtifactDraftNaturalTextRequester.draft_outputs.append(request_data.data.get("output"))
            yield "message", "# Partial attempt that must be discarded\n\n"
            yield "message", "<$retry>transient provider disconnect</$retry>"
            yield "message", "# Final\n\nNatural-text artifact body."
            return
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "Prepare a TaskWorkspace-backed final artifact.",
                "expected_evidence": "final.md is written and read back.",
                "rationale": "The task requests a deliverable file.",
                "deliverable_mode": "task_workspace_artifact",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "Control result ready; framework should draft the TaskWorkspace artifact.",
                "artifact_manifest": {"path": "final.md", "sections": [{"id": "summary", "title": "Summary"}]},
                "evidence": ["artifact source evidence"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "The TaskWorkspace artifact was delivered.",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "final.md",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatParallelActionRequester(MockAgentExecutionRequester):
    name = "MockFlatParallelActionRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockFlatParallelActionRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "actions",
                "step_instruction": "Collect independent evidence from two framework Actions.",
                "expected_evidence": "Both independent action results are present.",
                "rationale": "The actions are independent and can run in the same bounded step.",
            }
        elif "next_action" in text and "execution_commands" in text:
            MockFlatParallelActionRequester.action_planning_calls += 1
            if MockFlatParallelActionRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "response": None,
                    "execution_commands": [
                        {
                            "purpose": "Collect evidence A.",
                            "action_id": "slow_a",
                            "action_input": {},
                        },
                        {
                            "purpose": "Collect evidence B.",
                            "action_id": "slow_b",
                            "action_input": {},
                        },
                    ],
                }
            else:
                payload = _action_terminal_response(
                    {
                        "step_result": "parallel action evidence collected",
                        "evidence": ["slow_a executed", "slow_b executed"],
                        "remaining_work": [],
                    }
                )
        elif "[ACTION RESULTS]" in text:
            payload = {
                "step_result": "parallel action evidence collected",
                "evidence": ["slow_a executed", "slow_b executed"],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "both independent action results are present",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "flat parallel action accepted result",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockFlatEvidenceCandidateRequester(MockAgentExecutionRequester):
    name = "MockFlatEvidenceCandidateRequester"
    report = "# Weekly Report\n\n" + ("Detailed section with grounded evidence.\n" * 80)

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "Produce the requested report.",
                "expected_evidence": "Complete report text and supporting evidence.",
                "rationale": "One bounded step can produce the report.",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "Report written; see evidence for the full Markdown.",
                "evidence": [self.report],
                "remaining_work": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": "candidate_final_result" in text and "Weekly Report" in text,
                "requires_block": False,
                "reason": "candidate final report is visible to verifier",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "",
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


def _is_taskboard_plan_request(text: str) -> bool:
    return "Plan a TaskBoard for this submitted task" in text or "Plan a card board for this submitted task" in text


def _is_taskboard_final_request(text: str) -> bool:
    return (
        "Synthesize the final result for this TaskBoard task" in text
        or "Assemble a verifier-ready final result for this TaskBoard task" in text
        or "Assemble the final result for this task from completed card evidence" in text
    )


def _is_taskboard_control_request(text: str) -> bool:
    return "Complete one TaskBoard control card" in text


class MockTaskBoardRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Complete the task through a board.",
                "cards": [
                    {
                        "id": "collect",
                        "action_block": "Collect and summarize evidence.",
                        "objective": "Collect one fact and summarize it.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The fact is summarized.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": ["Check the collected fact before finalizing."],
                "completion_gate": "All cards completed and final answer synthesized.",
                "why_this_effort_shape": "One card is enough for this mock task.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "completed",
                "answer": "taskboard card result",
                "evidence": ["taskboard card evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "completed card evidence satisfies the criterion",
                "final_result": "taskboard accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardControlRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardControlRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Complete the task through a control card.",
                "cards": [
                    {
                        "id": "synthesize",
                        "action_block": "Synthesize and verify the final deliverable.",
                        "objective": "Produce the complete final Markdown deliverable and decide whether it is enough.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The final deliverable is complete and sufficient.",
                        "allowed_execution_shape": "control",
                    }
                ],
                "reflection_points": ["Check sufficiency in the control-card output."],
                "completion_gate": "The control card returns a complete accepted deliverable.",
                "why_this_effort_shape": "One control card can synthesize and self-check this task.",
                "risk_notes": [],
            }
        elif _is_taskboard_control_request(text):
            payload = {
                "status": "completed",
                "answer": "control card synthesized the final deliverable",
                "artifact_markdown": "# Control Result\n\nComplete deliverable body.",
                "sufficient": True,
                "next_board_action": "finalize",
                "gaps": [],
                "evidence": ["control evidence summary"],
                "remaining_work": [],
                "diagnostics": [{"kind": "control", "message": "single request handled synthesis and decision"}],
            }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "failed",
                "answer": "legacy child execution should not run for control cards",
                "remaining_work": ["unexpected legacy path"],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "control-card deliverable satisfies the criterion",
                "final_result": "# Control Result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("# Control Result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardConsumerDrivenRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardConsumerDrivenRequester"
    seen_dependency_evidence = False

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardConsumerDrivenRequester.seen_dependency_evidence = False

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if "Verify the task against every success criterion" in text:
            raise AssertionError("TaskBoard intermediate consumer should not call terminal verifier")
        if _is_taskboard_final_request(text):
            raise AssertionError("TaskBoard intermediate consumer should not call terminal synthesis")
        if _is_taskboard_control_request(text):
            MockTaskBoardConsumerDrivenRequester.seen_dependency_evidence = (
                "collect" in text and "sources/source.md" in text and "ref_only" in text
            )
            payload = {
                "status": "blocked",
                "answer": "Dependency evidence is only a ref; request bounded readback before continuing.",
                "sufficient": False,
                "next_board_action": "readback",
                "target_refs": [
                    {"owner": "task_workspace", "locator": "sources/source.md"}
                ],
                "gaps": ["Need bounded TaskWorkspace readback for sources/source.md."],
                "evidence": ["collect card produced a ref-only source pointer"],
                "remaining_work": ["Continue the control card after readback."],
                "diagnostics": [{"kind": "consumer_driven_sufficiency"}],
            }
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardSectionedArtifactRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardSectionedArtifactRequester"
    tail_marker = "SECTIONED-ARTIFACT-END-MARKER"
    first_section = (
        "# Sectioned Report\n\n"
        "This report is intentionally long enough to require a TaskWorkspace-backed sectioned artifact.\n\n"
        + ("Source-grounded analysis paragraph with bounded evidence refs.\n" * 120)
    )
    second_section = (
        "The second section carries the complete body that should not be streamed back through "
        "TaskBoard tick payloads or finalizer hot input.\n\n"
        + ("Detailed finding with supporting source boundary.\n" * 120)
        + tail_marker
    )
    full_report = f"{first_section}\n\n## Details\n\n{second_section}"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Write a sectioned report through a control card.",
                "cards": [
                    {
                        "id": "synthesize",
                        "action_block": "Synthesize the final sectioned deliverable.",
                        "objective": "Produce the complete sectioned Markdown deliverable.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The complete sectioned report is available in TaskWorkspace.",
                        "allowed_execution_shape": "control",
                    }
                ],
                "reflection_points": ["Ensure the artifact is complete and backed by TaskWorkspace readback."],
                "completion_gate": "The sectioned artifact is written and accepted.",
                "why_this_effort_shape": "One control card can write the final sectioned report.",
                "risk_notes": [],
            }
        elif _is_taskboard_control_request(text):
            payload = {
                "status": "completed",
                "answer": "sectioned report manifest prepared",
                "artifact_manifest": {
                    "path": "final.md",
                    "sections": [
                        {"id": "summary", "title": "Summary", "content": self.first_section},
                        {"id": "details", "title": "Details", "content": self.second_section},
                    ],
                },
                "sufficient": True,
                "next_board_action": "finalize",
                "gaps": [],
                "evidence": ["sectioned evidence summary"],
                "remaining_work": [],
                "diagnostics": [{"kind": "control", "message": "sectioned manifest returned"}],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "sectioned TaskWorkspace artifact satisfies the criterion",
                "final_result": "# Sectioned Report",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("# Sectioned Report")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardFinalCandidateRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardFinalCandidateRequester"
    full_report = (
        "# Repository Report\n\n"
        "## Repository Snapshot\n"
        "- Source: cloned repository files.\n\n"
        "## Purpose\n"
        "This project trains reusable agent skills from source-grounded examples.\n\n"
        "## Core Ideas\n"
        "- Treat the skill document as trainable state.\n"
        "- Validate edits against held-out tasks.\n\n"
        "## Evidence Table\n"
        "| Claim | Evidence |\n"
        "| --- | --- |\n"
        "| Skill state is edited | README.md and package entry point |\n"
    )

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Write a source-grounded repository report.",
                "cards": [
                    {
                        "id": "final_report",
                        "action_block": "Draft the final repository report.",
                        "objective": "Produce the complete final Markdown deliverable.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The complete final report is available.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The final report is complete.",
                "why_this_effort_shape": "One card is enough for this regression.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "completed",
                "answer": self.full_report,
                "evidence": ["README.md supports the report."],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "all required sections are present",
                "final_result": self.full_report[:120],
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload(self.full_report)
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardSlowCardRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardSlowCardRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Exercise a bounded card timeout.",
                "cards": [
                    {
                        "id": "slow",
                        "action_block": "Run a slow evidence collection step.",
                        "objective": "Collect evidence with a slow model request.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The slow evidence is collected.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The slow card completes.",
                "why_this_effort_shape": "One card is enough for this timeout probe.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            await asyncio.sleep(0.6)
            payload = {
                "status": "completed",
                "answer": "slow card eventually completed",
                "evidence": ["late evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "should not finalize after a card timeout",
                "final_result": "unexpected final",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("unexpected final")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardControlStallRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardControlStallRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Exercise a TaskBoard control-card idle guard.",
                "cards": [
                    {
                        "id": "control-stall",
                        "action_block": "Run a stalled control card.",
                        "objective": "Synthesize a control result, but the model request stalls.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The control result is available.",
                        "allowed_execution_shape": "control",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The control card completes.",
                "why_this_effort_shape": "One control card is enough for this liveness probe.",
                "risk_notes": [],
            }
        elif _is_taskboard_control_request(text):
            await asyncio.sleep(5)
            payload = {
                "status": "completed",
                "answer": "unexpected late control result",
                "sufficient": True,
                "remaining_work": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "should not finalize after a control timeout",
                "final_result": "unexpected final",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("unexpected final")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardRetryCardRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardRetryCardRequester"
    card_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardRetryCardRequester.card_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Exercise card retry.",
                "cards": [
                    {
                        "id": "retry",
                        "action_block": "Collect evidence with one transient timeout.",
                        "objective": "Collect retry evidence.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "Retry evidence is collected.",
                        "allowed_execution_shape": "model",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The retry card completes.",
                "why_this_effort_shape": "One retried card is enough for this regression.",
                "risk_notes": [],
            }
        elif "Execute exactly one TaskBoard card" in text:
            MockTaskBoardRetryCardRequester.card_calls += 1
            if MockTaskBoardRetryCardRequester.card_calls == 1:
                await asyncio.sleep(1.5)
            payload = {
                "status": "completed",
                "answer": "retried card completed",
                "evidence": ["retry evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "retry card evidence satisfies the criterion",
                "final_result": "taskboard retry accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard retry accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardActionPostExecutionPlanningStallRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardActionPostExecutionPlanningStallRequester"
    action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardActionPostExecutionPlanningStallRequester.action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Exercise partial evidence preservation after a card stall.",
                "cards": [
                    {
                        "id": "partial",
                        "action_block": "Run one action, then continue planning.",
                        "objective": "Collect action evidence before a later planning stall.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "Action evidence is captured.",
                        "allowed_execution_shape": "actions",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "Partial evidence is available for analysis.",
                "why_this_effort_shape": "One action card is enough for this regression.",
                "risk_notes": [],
            }
        elif "next_action" in text and "execution_commands" in text:
            MockTaskBoardActionPostExecutionPlanningStallRequester.action_planning_calls += 1
            if MockTaskBoardActionPostExecutionPlanningStallRequester.action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "response": None,
                    "execution_commands": [
                        {
                            "purpose": "Collect probe evidence before the stall.",
                            "action_id": "probe_action",
                            "action_input": {},
                        }
                    ],
                }
            else:
                await asyncio.sleep(5)
                payload = {"next_action": "response", "execution_commands": []}
        elif "[ACTION RESULTS]" in text:
            payload = {
                "status": "completed",
                "answer": "probe action evidence collected",
                "evidence": ["probe_action executed"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": False,
                "reason": "the card should not finalize after a stall",
                "final_result": "",
                "missing_criteria": ["card stalled"],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("unexpected final")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardReadbackRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardReadbackRequester"
    last_action_id = ""
    readback_planning_seen = False
    review_planning_prompt = ""
    collect_action_planning_calls = 0
    review_action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardReadbackRequester.last_action_id = ""
        MockTaskBoardReadbackRequester.readback_planning_seen = False
        MockTaskBoardReadbackRequester.review_planning_prompt = ""
        MockTaskBoardReadbackRequester.collect_action_planning_calls = 0
        MockTaskBoardReadbackRequester.review_action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Complete the task through evidence readback.",
                "cards": [
                    {
                        "id": "collect",
                        "action_block": "Collect opaque evidence.",
                        "objective": "Collect one opaque evidence artifact.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The opaque evidence is available as a cold artifact ref.",
                        "allowed_execution_shape": "model",
                    },
                    {
                        "id": "review",
                        "action_block": "Review evidence.",
                        "objective": "Review evidence by reading cold artifact refs when previews are insufficient.",
                        "depends_on": ["collect"],
                        "evidence_to_use": ["collect"],
                        "done_when": "The cold artifact ref has been read back.",
                        "allowed_execution_shape": "readback",
                    },
                ],
                "reflection_points": ["Read cold refs when dependency previews are insufficient."],
                "completion_gate": "Both cards completed and final answer synthesized.",
                "why_this_effort_shape": "The second card depends on the first card evidence.",
                "risk_notes": [],
            }
        elif "next_action" in text and "execution_commands" in text:
            selection_key_match = re.search(r"sel_[0-9a-f]+", text)
            if (
                "Review evidence by reading cold artifact refs" in text
                and "read_action_artifact" in text
                and selection_key_match is not None
            ):
                MockTaskBoardReadbackRequester.review_planning_prompt = text
                MockTaskBoardReadbackRequester.readback_planning_seen = True
                MockTaskBoardReadbackRequester.last_action_id = "read_action_artifact"
                MockTaskBoardReadbackRequester.review_action_planning_calls += 1
                if MockTaskBoardReadbackRequester.review_action_planning_calls == 1:
                    payload = {
                        "next_action": "execute",
                        "response": None,
                        "execution_commands": [
                            {
                                "purpose": "Read dependency cold artifact.",
                                "action_id": "read_action_artifact",
                                "action_input": {
                                    "selection_key": (
                                        selection_key_match.group(0) if selection_key_match else "missing"
                                    ),
                                },
                            }
                        ],
                    }
                else:
                    payload = _action_terminal_response(
                        {
                            "status": "completed",
                            "answer": "readback confirmed",
                            "evidence": ["read_action_artifact returned the dependency artifact"],
                            "remaining_work": [],
                            "diagnostics": [],
                        }
                    )
            else:
                MockTaskBoardReadbackRequester.last_action_id = "produce_large_evidence"
                MockTaskBoardReadbackRequester.collect_action_planning_calls += 1
                if MockTaskBoardReadbackRequester.collect_action_planning_calls == 1:
                    payload = {
                        "next_action": "execute",
                        "response": None,
                        "execution_commands": [
                            {
                                "purpose": "Produce an opaque artifact.",
                                "action_id": "produce_large_evidence",
                                "action_input": {},
                            }
                        ],
                    }
                else:
                    payload = _action_terminal_response(
                        {
                            "status": "completed",
                            "answer": "cold artifact produced",
                            "evidence": ["produce_large_evidence produced a cold artifact ref"],
                            "remaining_work": [],
                            "diagnostics": [],
                        }
                    )
        elif "[ACTION RESULTS]" in text:
            if MockTaskBoardReadbackRequester.last_action_id == "read_action_artifact":
                payload = {
                    "status": "completed",
                    "answer": "readback confirmed",
                    "evidence": ["read_action_artifact returned the dependency artifact"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
            else:
                payload = {
                    "status": "completed",
                    "answer": "cold artifact produced",
                    "evidence": ["produce_large_evidence produced a cold artifact ref"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "completed",
                "answer": "card completed without action",
                "evidence": ["card evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "dependency evidence was read back",
                "final_result": "taskboard readback accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard readback accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardDependencyReadbackRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardDependencyReadbackRequester"
    last_action_id = ""
    dependency_readback_seen = False
    source_refs_seen = False
    collect_action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardDependencyReadbackRequester.last_action_id = ""
        MockTaskBoardDependencyReadbackRequester.dependency_readback_seen = False
        MockTaskBoardDependencyReadbackRequester.source_refs_seen = False
        MockTaskBoardDependencyReadbackRequester.collect_action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Complete the task through automatic dependency readback.",
                "cards": [
                    {
                        "id": "collect",
                        "action_block": "Collect opaque evidence.",
                        "objective": "Collect one opaque evidence artifact.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The opaque evidence is available as a cold artifact ref.",
                        "allowed_execution_shape": "model",
                    },
                    {
                        "id": "synthesize",
                        "action_block": "Use dependency evidence.",
                        "objective": "Use the dependency evidence without a dedicated readback card.",
                        "depends_on": ["collect"],
                        "evidence_to_use": ["collect"],
                        "done_when": "The hidden evidence detail is used.",
                        "allowed_execution_shape": "model",
                    },
                ],
                "reflection_points": ["Use cold refs when dependency previews are insufficient."],
                "completion_gate": "Both cards completed and final answer synthesized.",
                "why_this_effort_shape": "The second card depends on the first card evidence.",
                "risk_notes": [],
            }
        elif "next_action" in text and "execution_commands" in text:
            if "dependency_readbacks" in text and "Hidden evidence" in text:
                MockTaskBoardDependencyReadbackRequester.dependency_readback_seen = True
                if "source_refs" in text and "https://example.test/evidence" in text:
                    MockTaskBoardDependencyReadbackRequester.source_refs_seen = True
                payload = _action_terminal_response(
                    {
                        "status": "completed",
                        "answer": "dependency readback evidence used",
                        "evidence": ["dependency_readbacks included Hidden evidence"],
                        "remaining_work": [],
                        "diagnostics": [],
                    }
                )
            else:
                MockTaskBoardDependencyReadbackRequester.last_action_id = "produce_large_evidence"
                MockTaskBoardDependencyReadbackRequester.collect_action_planning_calls += 1
                if MockTaskBoardDependencyReadbackRequester.collect_action_planning_calls == 1:
                    payload = {
                        "next_action": "execute",
                        "response": None,
                        "execution_commands": [
                            {
                                "purpose": "Produce an opaque artifact.",
                                "action_id": "produce_large_evidence",
                                "action_input": {},
                            }
                        ],
                    }
                else:
                    payload = _action_terminal_response(
                        {
                            "status": "completed",
                            "answer": "cold artifact produced",
                            "evidence": ["produce_large_evidence produced a cold artifact ref"],
                            "remaining_work": [],
                            "diagnostics": [],
                        }
                    )
        elif "[ACTION RESULTS]" in text:
            payload = {
                "status": "completed",
                "answer": "cold artifact produced",
                "evidence": ["produce_large_evidence produced a cold artifact ref"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif (
            "Execute exactly one TaskBoard card" in text
            and "Use dependency evidence without a dedicated readback card" in text
        ):
            if "dependency_readbacks" in text and "Hidden evidence" in text:
                MockTaskBoardDependencyReadbackRequester.dependency_readback_seen = True
                if "source_refs" in text and "https://example.test/evidence" in text:
                    MockTaskBoardDependencyReadbackRequester.source_refs_seen = True
                payload = {
                    "status": "completed",
                    "answer": "dependency readback evidence used",
                    "evidence": ["dependency_readbacks included Hidden evidence"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
            else:
                payload = {
                    "status": "blocked",
                    "answer": "dependency readback missing",
                    "evidence": [],
                    "remaining_work": ["Need dependency artifact readback."],
                    "diagnostics": [],
                }
        elif "Execute exactly one TaskBoard card" in text:
            payload = {
                "status": "completed",
                "answer": "card completed without action",
                "evidence": ["card evidence"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "dependency evidence was read back before the downstream card.",
                "final_result": "taskboard dependency readback accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard dependency readback accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


class MockTaskBoardControlDependencyReadbackRequester(MockAgentExecutionRequester):
    name = "MockTaskBoardControlDependencyReadbackRequester"
    dependency_readback_seen = False
    source_refs_seen = False
    collect_action_planning_calls = 0

    @staticmethod
    def _on_register():
        MockAgentExecutionRequester.requests = []
        MockTaskBoardControlDependencyReadbackRequester.dependency_readback_seen = False
        MockTaskBoardControlDependencyReadbackRequester.source_refs_seen = False
        MockTaskBoardControlDependencyReadbackRequester.collect_action_planning_calls = 0

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentExecutionRequester.requests.append(text)
        if _is_taskboard_plan_request(text):
            payload = {
                "board_goal": "Complete the task through control-card dependency readback.",
                "cards": [
                    {
                        "id": "collect",
                        "action_block": "Collect opaque evidence.",
                        "objective": "Collect one opaque evidence artifact.",
                        "depends_on": [],
                        "evidence_to_use": [],
                        "done_when": "The opaque evidence is available as a cold artifact ref.",
                        "allowed_execution_shape": "model",
                    },
                    {
                        "id": "synthesize",
                        "action_block": "Synthesize from dependency evidence.",
                        "objective": "Synthesize after reading dependency evidence.",
                        "depends_on": ["collect"],
                        "evidence_to_use": ["collect"],
                        "done_when": "The hidden evidence detail is included in the synthesis.",
                        "allowed_execution_shape": "control",
                    },
                ],
                "reflection_points": ["Use cold refs when dependency previews are insufficient."],
                "completion_gate": "Both cards completed and final answer synthesized.",
                "why_this_effort_shape": "The control card depends on collected evidence.",
                "risk_notes": [],
            }
        elif "next_action" in text and "execution_commands" in text:
            MockTaskBoardControlDependencyReadbackRequester.collect_action_planning_calls += 1
            if MockTaskBoardControlDependencyReadbackRequester.collect_action_planning_calls == 1:
                payload = {
                    "next_action": "execute",
                    "response": None,
                    "execution_commands": [
                        {
                            "purpose": "Produce an opaque artifact.",
                            "action_id": "produce_large_evidence",
                            "action_input": {},
                        }
                    ],
                }
            else:
                payload = _action_terminal_response(
                    {
                        "status": "completed",
                        "answer": "cold artifact produced",
                        "evidence": ["produce_large_evidence produced a cold artifact ref"],
                        "remaining_work": [],
                        "diagnostics": [],
                    }
                )
        elif "[ACTION RESULTS]" in text:
            payload = {
                "status": "completed",
                "answer": "cold artifact produced",
                "evidence": ["produce_large_evidence produced a cold artifact ref"],
                "remaining_work": [],
                "diagnostics": [],
            }
        elif _is_taskboard_control_request(text):
            if "evidence_ledger" in text and "Hidden evidence" in text:
                MockTaskBoardControlDependencyReadbackRequester.dependency_readback_seen = True
                if "source_refs" in text and "https://example.test/evidence" in text:
                    MockTaskBoardControlDependencyReadbackRequester.source_refs_seen = True
                payload = {
                    "status": "completed",
                    "answer": "control dependency readback evidence used",
                    "candidate_final_result": "control dependency readback accepted result",
                    "sufficient": True,
                    "next_board_action": "finalize",
                    "gaps": [],
                    "evidence": ["evidence_ledger included Hidden evidence"],
                    "remaining_work": [],
                    "diagnostics": [],
                }
            else:
                payload = {
                    "status": "blocked",
                    "answer": "dependency readback missing",
                    "sufficient": False,
                    "next_board_action": "readback",
                    "gaps": ["Need dependency artifact readback."],
                    "evidence": [],
                    "remaining_work": ["Need dependency artifact readback."],
                    "diagnostics": [],
                }
        elif _is_taskboard_final_request(text):
            payload = {
                "accepted": True,
                "reason": "control-card dependency evidence was read back.",
                "final_result": "taskboard control dependency readback accepted result",
                "missing_criteria": [],
            }
        elif "Verify the task against every success criterion" in text:
            payload = _taskboard_verification_payload("taskboard control dependency readback accepted result")
        else:
            payload = {"answer": "ok", "status": "ready"}
        yield "message", json.dumps(payload, ensure_ascii=False)


def _create_goal_pursuit_agent(name: str = "agent-execution-goal-pursuit"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockGoalPursuitRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_replan_agent(name: str = "agent-execution-flat-replan"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatReplanRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_repair_constraint_agent(name: str = "agent-execution-flat-repair-constraint"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatRepairConstraintRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_agent(name: str = "agent-execution-flat-action"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_planning_stall_agent(name: str = "agent-execution-flat-action-planning-stall"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionPlanningStallRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_post_execution_planning_stall_agent(
    name: str = "agent-execution-flat-action-post-execution-planning-stall",
):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionPostExecutionPlanningStallRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_action_planning_slow_agent(name: str = "agent-execution-flat-action-planning-slow"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatActionPlanningSlowRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_slow_plan_agent(name: str = "agent-execution-flat-slow-plan"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatSlowPlanRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_task_workspace_artifact_draft_stall_agent(name: str = "agent-execution-artifact-draft-stall"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockWorkspaceArtifactDraftStallRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_task_workspace_artifact_draft_retry_agent(name: str = "agent-execution-artifact-draft-retry"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockWorkspaceArtifactDraftRetryRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_task_workspace_artifact_draft_natural_text_agent(name: str = "agent-execution-artifact-draft-natural-text"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockWorkspaceArtifactDraftNaturalTextRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_parallel_action_agent(name: str = "agent-execution-flat-parallel-action"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatParallelActionRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_flat_evidence_candidate_agent(name: str = "agent-execution-flat-evidence-candidate"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockFlatEvidenceCandidateRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_agent(name: str = "agent-execution-taskboard"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_control_agent(name: str = "agent-execution-taskboard-control"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardControlRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_consumer_driven_agent(name: str = "agent-execution-taskboard-consumer-driven"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardConsumerDrivenRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_sectioned_artifact_agent(name: str = "agent-execution-taskboard-sectioned-artifact"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardSectionedArtifactRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_final_candidate_agent(name: str = "agent-execution-taskboard-final-candidate"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardFinalCandidateRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_slow_card_agent(name: str = "agent-execution-taskboard-slow-card"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardSlowCardRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_control_stall_agent(name: str = "agent-execution-taskboard-control-stall"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardControlStallRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_retry_card_agent(name: str = "agent-execution-taskboard-retry-card"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardRetryCardRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_action_post_execution_planning_stall_agent(
    name: str = "agent-execution-taskboard-action-post-execution-planning-stall",
):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardActionPostExecutionPlanningStallRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_readback_agent(name: str = "agent-execution-taskboard-readback"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardReadbackRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_dependency_readback_agent(name: str = "agent-execution-taskboard-dependency-readback"):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardDependencyReadbackRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def _create_taskboard_control_dependency_readback_agent(
    name: str = "agent-execution-taskboard-control-dependency-readback",
):
    settings = Settings(name=f"{ name }-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-plugins")
    plugin_manager.register("ModelRequester", MockTaskBoardControlDependencyReadbackRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


def test_taskboard_blocked_scoped_retrieval_card_adds_continuation_patch(tmp_path):
    agent = _create_agent("taskboard-scoped-retrieval-continuation").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-scoped-retrieval-continuation",
        goal="Use scoped retrieval evidence before final synthesis.",
        success_criteria=["The downstream card continues when bounded evidence is insufficient."],
    )
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="scoped-retrieval-continuation",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "scoped-retrieval-continuation-graph",
                "cards": [
                    {
                        "id": "review",
                        "objective": "Review scoped retrieval evidence and request more if the snippet is incomplete.",
                        "allowed_execution_shape": "actions",
                        "metadata": {
                            "scoped_retrieval": {
                                "query_groups": [
                                    {
                                        "query": "Project Atlas blocker",
                                        "expected_role": "evidence_snippet",
                                        "source_kinds": ["record_store"],
                                        "filters": {"collection": "retained-notes"},
                                        "snippet_limit": 200,
                                    }
                                ]
                            }
                        },
                    }
                ],
            }
        ),
    )
    card = revision.graph.card_by_id()["review"]
    diagnostics: list[dict[str, Any]] = []
    patch = task._taskboard_scoped_retrieval_continuation_patch(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "sufficient": False,
            "gaps": ["The bounded snippet ended before the blocker sentence was complete."],
            "remaining_work": ["Rerun scoped retrieval with enough bounded context before synthesis."],
        },
        diagnostics,
    )

    assert patch is not None
    assert diagnostics[0]["code"] == "taskboard.scoped_retrieval.auto_continuation_patch"
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["review"].failure_policy == "degradable"
    assert cards["review"].status == "skipped"
    assert "review.evidence" in cards
    assert "review.continue" in cards
    assert cards["review.evidence"].allowed_execution_shape == "auto"
    scoped_plan = cards["review.evidence"].metadata["scoped_retrieval"]
    assert scoped_plan["query_groups"][0]["snippet_limit"] == 1200
    assert cards["review.evidence"].metadata["generated_by"] == "agent_task.taskboard.scoped_retrieval_continuation"
    assert cards["review.continue"].depends_on == ("review", "review.evidence")

    repeated_patch = task._taskboard_scoped_retrieval_continuation_patch(
        SimpleNamespace(revision=next_revision, card=cards["review.continue"]),
        {
            "status": "blocked",
            "sufficient": False,
            "remaining_work": ["The expanded scoped retrieval evidence is still insufficient."],
        },
        [],
    )

    assert repeated_patch is None

    nonfatal_gap_patch = task._taskboard_scoped_retrieval_continuation_patch(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "completed",
            "sufficient": True,
            "gaps": ["Optional caveat that does not block this card."],
        },
        [],
    )

    assert nonfatal_gap_patch is None


def test_taskboard_control_readback_action_auto_patch_adds_continuation():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="auto-readback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "auto-readback-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect source evidence."},
                    {
                        "id": "final",
                        "objective": "Write final answer after full source readback.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": {"card_id": "collect", "status": "completed"}}],
        },
    )
    card = revision.graph.card_by_id()["final"]
    patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "gaps": ["Need complete source page."],
            "remaining_work": ["Generate final.md after readback."],
        },
    )

    assert patch is not None
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["final"].failure_policy == "degradable"
    assert cards["final"].status == "skipped"
    assert "final.readback" in cards
    assert "final.continue" in cards
    assert cards["final.readback"].allowed_execution_shape == "readback"
    assert cards["final.continue"].depends_on == ("final", "collect", "final.readback")
    schedule = validator.schedule(next_revision)
    assert "final.readback" in schedule.runnable_card_ids

    repeated_patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=next_revision, card=cards["final.continue"]),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "gaps": ["The same readback was still insufficient."],
            "remaining_work": ["Do not create another continue/readback chain."],
        },
    )

    assert repeated_patch is None

    next_target_patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=next_revision, card=cards["final.continue"]),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {"owner": "task_workspace", "locator": "final.md"}
            ],
            "gaps": ["Need exact final artifact wording before patching."],
            "remaining_work": ["Read final.md, then apply the attribution correction."],
        },
    )

    assert next_target_patch is not None
    continued_revision = validator.apply_patch(next_revision, next_target_patch)
    continued_cards = continued_revision.graph.card_by_id()
    assert "final.continue.readback" in continued_cards
    assert "final.continue.continue" in continued_cards
    assert continued_cards["final.continue.continue"].metadata["target_refs"] == [
        {"owner": "task_workspace", "locator": "final.md"}
    ]


def test_taskboard_control_auto_readback_scope_includes_upstream_evidence_cards():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="auto-readback-upstream",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "auto-readback-upstream-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect source evidence."},
                    {
                        "id": "analyze",
                        "objective": "Analyze source evidence.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                    },
                    {
                        "id": "final",
                        "objective": "Write final answer after full source readback.",
                        "depends_on": ["analyze"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "artifact_refs": [
                            {
                                "artifact_id": "source-artifact",
                                "action_call_id": "source-call",
                                "role": "output",
                            }
                        ],
                    },
                },
                {"op": "record_card_result", "result": {"card_id": "analyze", "status": "completed"}},
            ],
        },
    )
    card = revision.graph.card_by_id()["final"]
    patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "gaps": ["Need upstream source artifact."],
            "remaining_work": ["Generate final.md after readback."],
        },
    )

    assert patch is not None
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()

    assert cards["final.readback"].depends_on == ("analyze", "collect")
    assert cards["final.readback"].metadata["evidence_scope"] == ["analyze", "collect"]
    assert cards["final.continue"].depends_on == ("final", "analyze", "final.readback")
    schedule = validator.schedule(next_revision)
    assert "final.readback" in schedule.runnable_card_ids

    scoped_view = build_task_board_evidence_view(next_revision, card_ids=cards["final.readback"].depends_on).to_dict()
    assert scoped_view["artifact_refs"][0]["artifact_id"] == "source-artifact"


def test_taskboard_control_invalid_readback_patch_proposal_becomes_framework_patch():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="invalid-readback-patch",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "invalid-readback-patch-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect source evidence."},
                    {
                        "id": "final",
                        "objective": "Write final answer after source readback.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "artifact_refs": [
                            {
                                "artifact_id": "source-artifact",
                                "action_call_id": "source-call",
                                "role": "output",
                            }
                        ],
                    },
                }
            ],
        },
    )
    card = revision.graph.card_by_id()["final"]
    diagnostics: list[dict[str, Any]] = []
    patch = AgentTask._taskboard_control_patch_proposal(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "patch_proposal": {
                "action": "readback",
                "target_refs": [
                    {"owner": "external", "locator": "https://example.test/source"}
                ],
                "reason": "Need scoped source readback.",
            },
            "gaps": ["Need source details."],
            "remaining_work": ["Continue after readback."],
        },
        diagnostics,
    )

    assert patch is not None
    assert diagnostics[0]["code"] == "taskboard.control.invalid_model_patch_proposal"
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["final.evidence"].allowed_execution_shape == "actions"
    assert cards["final.evidence"].depends_on == ("collect",)
    assert cards["final.evidence"].metadata["target_refs"] == [
        {"owner": "external", "locator": "https://example.test/source"}
    ]
    assert cards["final.continue"].depends_on == ("final", "collect", "final.evidence")


def test_taskboard_control_direct_target_refs_become_action_evidence_patch():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="direct-target-refs",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "direct-target-refs-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect initial source links."},
                    {
                        "id": "final",
                        "objective": "Write final answer after missing target refs are materialized.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": {"card_id": "collect", "status": "completed"}}],
        },
    )
    card = revision.graph.card_by_id()["final"]
    diagnostics: list[dict[str, Any]] = []
    patch = AgentTask._taskboard_control_patch_proposal(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {"owner": "external", "locator": "https://example.test/source.pdf"},
                {"owner": "external", "locator": "https://example.test/examples.html"},
            ],
            "gaps": ["Need full PDF text and example page content."],
            "remaining_work": ["Download or snapshot target refs, then continue final synthesis."],
        },
        diagnostics,
    )

    assert patch is not None
    assert diagnostics == []
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["final.evidence"].allowed_execution_shape == "actions"
    assert cards["final.evidence"].metadata["target_refs"] == [
        {"owner": "external", "locator": "https://example.test/source.pdf"},
        {"owner": "external", "locator": "https://example.test/examples.html"},
    ]
    assert "https://example.test/source.pdf" in cards["final.evidence"].objective
    assert cards["final.continue"].depends_on == ("final", "collect", "final.evidence")


def test_taskboard_control_empty_model_patch_does_not_swallow_readback_intent():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="empty-patch-readback-intent",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "empty-patch-readback-intent-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect initial source links."},
                    {
                        "id": "final",
                        "objective": "Write final answer after target refs are materialized.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": {"card_id": "collect", "status": "completed"}}],
        },
    )
    card = revision.graph.card_by_id()["final"]
    diagnostics: list[dict[str, Any]] = []
    patch = AgentTask._taskboard_control_patch_proposal(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {"owner": "external", "locator": "https://example.test/official.html"}
            ],
            "patch_proposal": {},
            "gaps": ["Need official source body."],
            "remaining_work": ["Read official source, then continue final synthesis."],
        },
        diagnostics,
    )

    assert patch is not None
    assert diagnostics[0]["code"] == "taskboard.control.invalid_model_patch_proposal"
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["final.evidence"].allowed_execution_shape == "actions"
    assert cards["final.evidence"].metadata["target_refs"] == [
        {"owner": "external", "locator": "https://example.test/official.html"}
    ]
    assert cards["final.continue"].depends_on == ("final", "collect", "final.evidence")


def test_taskboard_control_stream_display_meta_uses_locale_keys_not_hardcoded_labels():
    answer_meta = AgentTask._taskboard_control_stream_display_meta("answer")
    action_meta = AgentTask._taskboard_control_stream_display_meta("target_refs[0]")
    nested_meta = AgentTask._taskboard_control_stream_display_meta("evidence_use[0].claim")

    assert answer_meta["display_title_key"] == "agent_task.taskboard.control.answer"
    assert answer_meta["display_title_default"] == "Repair answer"
    assert answer_meta["display_category"] == "model_natural_language"
    assert answer_meta["display_is_intermediate"] is False
    assert action_meta["display_title_key"] == "agent_task.taskboard.control.target_refs"
    assert action_meta["display_title_default"] == "[Action: Target refs]"
    assert action_meta["display_category"] == "action"
    assert action_meta["display_is_intermediate"] is True
    assert nested_meta["display_title_key"] == "agent_task.taskboard.control.evidence_use"
    assert nested_meta["display_category"] == "evidence"


def test_taskboard_control_task_workspace_target_refs_become_readback_patch():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="workspace-target-refs",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-target-refs-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect retained TaskWorkspace refs."},
                    {
                        "id": "final",
                        "objective": "Write final answer after retained notes are read.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": {"card_id": "collect", "status": "completed"}}],
        },
    )
    card = revision.graph.card_by_id()["final"]
    diagnostics: list[dict[str, Any]] = []
    patch = AgentTask._taskboard_control_patch_proposal(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {
                    "owner": "record_store",
                    "locator": "retained-notes/rec_abc-operations-note.txt",
                }
            ],
            "gaps": ["Need the retained note body before synthesis."],
            "remaining_work": ["Read the retained note, then continue final synthesis."],
        },
        diagnostics,
    )

    assert patch is not None
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["final.readback"].allowed_execution_shape == "readback"
    assert cards["final.readback"].metadata["target_refs"] == [
        {
            "owner": "record_store",
            "locator": "retained-notes/rec_abc-operations-note.txt",
        }
    ]
    assert cards["final.readback"].metadata["task_workspace_target_refs"] == [
        {
            "owner": "record_store",
            "locator": "retained-notes/rec_abc-operations-note.txt",
        }
    ]
    assert "external_target_refs" not in cards["final.readback"].metadata
    assert cards["final.continue"].depends_on == ("final", "collect", "final.readback")
    assert cards["final.continue"].metadata["readback_card_id"] == "final.readback"
    assert cards["final.continue"].metadata["evidence_card_id"] == ""


def test_taskboard_target_refs_require_explicit_owner_and_preserve_read_identity():
    refs = AgentTask._normalize_taskboard_target_refs(
        [
            {
                "owner": "task_workspace",
                "locator": "sources/report.md",
                "content_version": "cv_report_v1",
                "range": {"offset": 4096, "max_bytes": 2048},
            },
            {
                "owner": "action_artifact",
                "locator": "sel_search_result",
                "content_version": "sha256:search-v1",
                "range": {"offset": 0, "max_bytes": 4096},
            },
            {
                "owner": "external",
                "locator": "https://example.test/source.pdf",
            },
            "selection-key-without-owner",
            {"locator": "path-without-owner.md"},
        ]
    )

    assert refs == [
        {
            "owner": "task_workspace",
            "locator": "sources/report.md",
            "content_version": "cv_report_v1",
            "range": {"offset": 4096, "max_bytes": 2048},
        },
        {
            "owner": "action_artifact",
            "locator": "sel_search_result",
            "content_version": "sha256:search-v1",
            "range": {"offset": 0, "max_bytes": 4096},
        },
        {
            "owner": "external",
            "locator": "https://example.test/source.pdf",
        },
    ]
    local_refs, action_refs = AgentTask._split_taskboard_target_refs(refs)
    assert [ref["owner"] for ref in local_refs] == ["task_workspace", "action_artifact"]
    assert [ref["owner"] for ref in action_refs] == ["external"]


def test_taskboard_mixed_target_owners_create_separate_action_and_readback_cards():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="mixed-target-owners",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "mixed-target-owners-graph",
                "cards": [
                    {
                        "id": "final",
                        "objective": "Finish after local and external evidence are both read.",
                        "allowed_execution_shape": "control",
                    }
                ],
            }
        ),
    )
    patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=revision, card=revision.graph.card_by_id()["final"]),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {"owner": "task_workspace", "locator": "sources/local.md"},
                {"owner": "external", "locator": "https://example.test/official"},
            ],
            "remaining_work": ["Read both targets."],
        },
    )

    assert patch is not None
    updated = validator.apply_patch(revision, patch)
    cards = updated.graph.card_by_id()
    assert cards["final.evidence"].allowed_execution_shape == "actions"
    assert cards["final.evidence"].metadata["external_target_refs"] == [
        {"owner": "external", "locator": "https://example.test/official"}
    ]
    assert cards["final.readback"].allowed_execution_shape == "readback"
    assert cards["final.readback"].metadata["task_workspace_target_refs"] == [
        {"owner": "task_workspace", "locator": "sources/local.md"}
    ]
    assert cards["final.continue"].depends_on == (
        "final",
        "final.evidence",
        "final.readback",
    )


def test_taskboard_control_continuations_inherit_one_terminal_convergence_subject():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="stable-convergence-subject",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "stable-convergence-subject-graph",
                "cards": [
                    {
                        "id": "final",
                        "objective": "Complete the source-grounded final answer.",
                        "allowed_execution_shape": "control",
                        "metadata": {"terminal_convergence_subject": "task:final-grounding"},
                    }
                ],
            }
        ),
    )
    first_patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=revision, card=revision.graph.card_by_id()["final"]),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {"owner": "external", "locator": "https://example.test/source-a"}
            ],
            "remaining_work": ["Read source A."],
        },
    )
    assert first_patch is not None
    first_revision = validator.apply_patch(revision, first_patch)
    first_cards = first_revision.graph.card_by_id()
    assert first_cards["final.evidence"].metadata["terminal_convergence_subject"] == (
        "task:final-grounding"
    )
    assert first_cards["final.continue"].metadata["terminal_convergence_subject"] == (
        "task:final-grounding"
    )

    second_patch = AgentTask._taskboard_control_auto_patch(
        SimpleNamespace(revision=first_revision, card=first_cards["final.continue"]),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {"owner": "external", "locator": "https://example.test/source-b"}
            ],
            "remaining_work": ["Read source B."],
        },
    )
    assert second_patch is not None
    second_revision = validator.apply_patch(first_revision, second_patch)
    second_cards = second_revision.graph.card_by_id()
    assert second_cards["final.continue.evidence"].metadata[
        "terminal_convergence_subject"
    ] == "task:final-grounding"
    assert second_cards["final.continue.continue"].metadata[
        "terminal_convergence_subject"
    ] == "task:final-grounding"


def test_taskboard_source_refs_mark_unread_intermediate_refs_before_target_readback(
    tmp_path,
):
    discovered_refs = AgentTask._collect_taskboard_source_refs(
        {
            "url": "https://example.test/source.pdf",
            "title": "Discovered source document",
            "media_type": "application/pdf",
            "sha256": "0" * 64,
            "bytes": 4096,
        }
    )
    read_refs = AgentTask._collect_taskboard_source_refs(
        {
            "url": "https://example.test/source.pdf",
            "title": "Read source document",
            "content_preview": "Bounded extracted source content.",
        }
    )
    full_value_refs = AgentTask._collect_taskboard_source_refs(
        {
            "path": "retained/source.md",
            "artifact_id": "artifact-1",
            "full_value_available": True,
            "bytes": 4096,
            "sha256": "0" * 64,
        }
    )
    task = AgentTask(
        _create_agent("taskboard-source-ref-policy").use_task_workspace(
            tmp_path / "task_workspace"
        ),
        goal="Inspect source refs.",
        success_criteria=["Source-ref states are explicit."],
        execution="taskboard",
    )
    policy = task._taskboard_source_ref_policy()

    assert discovered_refs[0]["content_state"] == "ref_only"
    assert read_refs[0]["content_state"] == "bounded_readback_available"
    assert full_value_refs[0]["content_state"] == "ref_only"
    assert "ref_only" in policy["content_states"]
    assert "sha256" not in discovered_refs[0]
    assert "bytes" not in discovered_refs[0]
    assert "media_type" not in discovered_refs[0]

    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="ref-only-target-readback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "ref-only-target-readback-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect discovered intermediate source refs."},
                    {
                        "id": "final",
                        "objective": "Synthesize only after unread source refs are materialized.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                        "required_outputs": ["final.md"],
                        "metadata": {"final_task_workspace_deliverables": ["final.md"]},
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": {"card_id": "collect", "status": "completed"}}],
        },
    )
    card = revision.graph.card_by_id()["final"]
    diagnostics: list[dict[str, Any]] = []
    patch = AgentTask._taskboard_control_patch_proposal(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "next_board_action": "readback",
            "target_refs": [
                {"owner": "external", "locator": discovered_refs[0]["url"]}
            ],
            "gaps": ["The source ref is discovered-only; scoped source content is still needed."],
            "remaining_work": ["Materialize the ref before final synthesis."],
        },
        diagnostics,
    )

    assert patch is not None
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["final.evidence"].allowed_execution_shape == "actions"
    assert cards["final.evidence"].metadata["target_refs"] == [
        {"owner": "external", "locator": "https://example.test/source.pdf"}
    ]
    assert cards["final.continue"].depends_on == ("final", "collect", "final.evidence")
    assert cards["final.continue"].metadata["final_task_workspace_deliverables"] == ["final.md"]
    assert "final.md" in cards["final.continue"].objective


def test_scoped_retrieval_results_expose_model_hot_view_without_provenance_noise():
    raw_locator = {
        "role": "locator_ref",
        "content_state": "ref_only",
        "source": "workspace.search_files",
        "path": "retained/source.md",
        "record_id": "record-1",
        "bytes": 4096,
        "sha256": "1" * 64,
        "media_type": "text/markdown",
        "content_kind": "text",
        "search_engine": "task_workspace_file_grep",
        "grep_tool": "rg",
        "scope": {"max_file_bytes": 200000},
        "file_ref": {"path": "retained/source.md", "sha256": "1" * 64},
        "ref": {
            "id": "record-1",
            "path": "retained/source.md",
            "collection": "retained",
            "kind": "note",
            "size": 4096,
            "sha256": "1" * 64,
            "meta": {"internal": "cold"},
        },
    }
    block_context = {
        "state": {
            "execution_block_results": [
                {
                    "kind": "context_read",
                    "execution_block_id": "block-1",
                    "source_plan_block_id": "plan-block-1",
                    "output": {
                        "operation": "read",
                        "query": "needle",
                            "filters": {"collection": "retained"},
                            "bounded": {
                                "package_id": "context_package:example",
                                "task_context_id": "task_context:example",
                                "context_revision": 3,
                                "returned_results": 1,
                                "used_chars": 15,
                                "omitted_count": 0,
                        },
                        "locator_refs": [raw_locator],
                        "evidence_snippets": [
                            {
                                "role": "evidence_snippet",
                                "content_state": "bounded_readback_available",
                                "source": "workspace.search_files",
                                "path": "retained/source.md",
                                "line_start": 7,
                                "line_end": 9,
                                "content": "needle evidence",
                                "snippet_bytes": 15,
                                "bytes": 4096,
                                "sha256": "1" * 64,
                                "media_type": "text/markdown",
                                "content_kind": "text",
                                "search_engine": "task_workspace_file_grep",
                                "grep_tool": "rg",
                                "scope": {"max_file_bytes": 200000},
                                "file_ref": {"path": "retained/source.md", "sha256": "1" * 64},
                                "locator_ref": raw_locator,
                            }
                        ],
                        "diagnostics": [
                            {
                                "code": "example.diagnostic",
                                "message": "visible diagnostic",
                                "sha256": "1" * 64,
                                "raw": {"bytes": 4096},
                            }
                        ],
                    },
                }
            ]
        }
    }

    hot_results = AgentTask._scoped_retrieval_results_from_block_context(block_context)
    hot_text = json.dumps(hot_results, ensure_ascii=False)

    assert hot_results[0]["locator_refs"][0]["path"] == "retained/source.md"
    assert hot_results[0]["locator_refs"][0]["ref"]["id"] == "record-1"
    assert hot_results[0]["evidence_snippets"][0]["content"] == "needle evidence"
    assert hot_results[0]["bounded"]["task_context_id"] == "task_context:example"
    assert '"sha256"' not in hot_text
    assert '"bytes"' not in hot_text
    assert '"media_type"' not in hot_text
    assert '"content_kind"' not in hot_text
    assert '"search_engine"' not in hot_text
    assert '"grep_tool"' not in hot_text
    assert '"scope"' not in hot_text
    assert '"file_ref"' not in hot_text
    assert '"execution_block_id"' not in hot_text
    assert '"source_plan_block_id"' not in hot_text
    assert block_context["state"]["execution_block_results"][0]["output"]["locator_refs"][0]["sha256"]


def test_taskboard_context_read_prompt_view_omits_reconstructable_provenance_noise():
    compact = AgentTask._compact_taskboard_context_read(
        {
            "id": "op-1",
            "plan_block_id": "plan-1",
            "source_plan_block_id": "source-plan-1",
            "execution_block_id": "exec-block-1",
            "kind": "context_read",
            "status": "completed",
            "output": {
                "diagnostics": [{"code": "diag", "message": "visible", "sha256": "1" * 64, "raw": {"bytes": 4096}}],
                "bounded": {
                    "returned_results": 1,
                    "continuation_available": True,
                    "source_coverage": {
                        "binding:repo": {
                            "scope": {"path": ".", "query": "owner"},
                            "returned_candidates": 8,
                            "exhaustive": False,
                            "continuation_available": True,
                        }
                    },
                    "diagnostics": [
                        {"code": "bounded", "message": "bounded visible", "sha256": "1" * 64, "raw": {"bytes": 4096}}
                    ],
                    "locator_refs": [
                        {
                            "path": "retained/source.md",
                            "record_id": "record-1",
                            "bytes": 4096,
                            "sha256": "1" * 64,
                            "media_type": "text/markdown",
                            "search_engine": "task_workspace_file_grep",
                            "grep_tool": "rg",
                            "content_state": "ref_only",
                        }
                    ],
                    "evidence_snippets": [
                        {
                            "path": "retained/source.md",
                            "content": "bounded source detail",
                            "bytes": 4096,
                            "sha256": "1" * 64,
                            "search_engine": "task_workspace_file_grep",
                            "grep_tool": "rg",
                        }
                    ],
                },
                "evidence_snippets": [
                    {
                        "path": "retained/source.md",
                        "content": "bounded source detail",
                        "bytes": 4096,
                        "sha256": "1" * 64,
                        "search_engine": "task_workspace_file_grep",
                        "grep_tool": "rg",
                    }
                ],
            },
        }
    )

    prompt_text = json.dumps(compact, ensure_ascii=False)
    assert "bounded source detail" in prompt_text
    assert compact["output"]["bounded"]["continuation_available"] is True
    assert compact["output"]["bounded"]["source_coverage"]["binding:repo"][
        "exhaustive"
    ] is False
    assert '"cursor"' not in prompt_text
    assert '"sha256"' not in prompt_text
    assert '"bytes"' not in prompt_text
    assert '"media_type"' not in prompt_text
    assert '"search_engine"' not in prompt_text
    assert '"grep_tool"' not in prompt_text
    assert '"plan_block_id"' not in prompt_text
    assert '"source_plan_block_id"' not in prompt_text
    assert '"execution_block_id"' not in prompt_text


def test_block_carrier_task_workspace_ref_meta_omits_reconstructable_provenance_noise():
    compact = AgentTask._compact_task_workspace_ref_or_snippet_for_meta(
        {
            "path": "retained/source.md",
            "line_start": 7,
            "line_end": 9,
            "content_state": "bounded_readback_available",
            "source": "workspace.search_files",
            "query": "needle",
            "content": "needle evidence",
            "bytes": 4096,
            "sha256": "1" * 64,
            "media_type": "text/markdown",
            "search_engine": "task_workspace_file_grep",
            "grep_tool": "rg",
        },
        max_chars=1000,
    )

    prompt_text = json.dumps(compact, ensure_ascii=False)
    assert compact["path"] == "retained/source.md"
    assert compact["content"] == "needle evidence"
    assert '"sha256"' not in prompt_text
    assert '"bytes"' not in prompt_text
    assert '"media_type"' not in prompt_text
    assert '"search_engine"' not in prompt_text
    assert '"grep_tool"' not in prompt_text


def test_model_hot_artifact_refs_omit_programmatic_provenance_noise():
    compact = AgentTask._compact_artifact_ref_for_verifier(
        {
            "artifact_id": "artifact-1",
            "action_call_id": "call-1",
            "path": "reports/final.md",
            "label": "final report",
            "media_type": "text/markdown",
            "bytes": 4096,
            "size": 4096,
            "sha256": "1" * 64,
            "content_kind": "text",
            "handler_id": "task_workspace_text",
            "source": "task_workspace",
            "truncated": True,
            "available": True,
        }
    )

    hot_text = json.dumps(compact, ensure_ascii=False)
    assert compact["artifact_id"] == "artifact-1"
    assert compact["action_call_id"] == "call-1"
    assert compact["path"] == "reports/final.md"
    assert compact["truncated"] is True
    assert '"sha256"' not in hot_text
    assert '"bytes"' not in hot_text
    assert '"size"' not in hot_text
    assert '"media_type"' not in hot_text
    assert '"content_kind"' not in hot_text
    assert '"handler_id"' not in hot_text


def test_action_source_refs_do_not_treat_sha_as_source_evidence():
    refs = AgentTask._collect_source_refs_from_action_records(
        [
            {
                "id": "collect",
                "action_call_id": "call-1",
                "artifact_refs": [
                    {
                        "path": "retained/source.md",
                        "sha256": "1" * 64,
                        "source_url": "https://example.test/source",
                    }
                ],
            }
        ]
    )

    fields = {ref["field"] for ref in refs}
    assert "source_url" in fields
    assert "path" in fields
    assert "sha256" not in fields


def test_action_source_refs_keep_full_value_artifacts_ref_only_until_readback():
    refs = AgentTask._collect_source_refs_from_action_records(
        [
            {
                "id": "collect",
                "action_call_id": "call-1",
                "artifact_refs": [
                    {
                        "artifact_id": "artifact-1",
                        "action_call_id": "call-1",
                        "path": "retained/source.md",
                        "full_value_available": True,
                        "bytes": 4096,
                        "sha256": "1" * 64,
                    }
                ],
            }
        ]
    )

    path_ref = next(ref for ref in refs if ref["field"] == "path")
    assert path_ref["value"] == "retained/source.md"
    assert path_ref["content_state"] == "ref_only"
    assert path_ref["evidence_boundary"] == "discovery_or_materialization_only"


def test_taskboard_available_readback_omits_programmatic_provenance_noise():
    evidence_view = {
        "artifact_refs": [
            {
                "selection_key": "selection-1",
                "role": "output",
                "label": "search note",
                "media_type": "text/markdown",
                "bytes": 4096,
                "sha256": "1" * 64,
                "full_value_available": True,
            }
        ],
        "file_refs": [
            {
                "path": "retained/source.md",
                "role": "evidence",
                "media_type": "text/markdown",
                "bytes": 4096,
                "sha256": "2" * 64,
            }
        ],
    }

    available = AgentTask._taskboard_available_readback(evidence_view)
    hot_text = json.dumps(available, ensure_ascii=False)

    assert available["action_artifact_readback"]["artifact_refs"][0]["selection_key"] == "selection-1"
    assert available["task_workspace_file_readback"]["file_refs"][0]["path"] == "retained/source.md"
    assert '"sha256"' not in hot_text
    assert '"bytes"' not in hot_text
    assert '"media_type"' not in hot_text
    assert '"full_value_available"' not in hot_text


def test_taskboard_available_readback_separates_recall_capability_from_prior_refs():
    available = AgentTask._taskboard_available_readback({})

    readback_shape = available["taskboard_readback_shape"]
    assert readback_shape["prior_refs_available"] is False
    assert "available" not in readback_shape
    action_readback = available["action_artifact_readback"]
    assert action_readback["capability_available"] is True
    assert action_readback["prior_refs_available"] is False
    assert "available" not in action_readback
    assert action_readback["artifact_refs"] == []
    file_readback = available["task_workspace_file_readback"]
    assert file_readback["prior_file_refs_available"] is False
    assert "available" not in file_readback
    assert "current ActionLoop records" in available["policy"]


@pytest.mark.asyncio
async def test_taskboard_readback_work_unit_hot_payload_omits_programmatic_provenance_noise(tmp_path):
    agent = _create_agent("execution-taskboard-readback-hot-provenance").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Read dependency evidence without hot-loading provenance fields.",
        success_criteria=["Readback work units keep cold refs separate from model-hot payloads."],
        execution="taskboard",
        max_iterations=None,
    )
    revision = TaskBoardRevision.create(
        board_id="readback-hot-provenance",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "readback-hot-provenance-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect cold refs."},
                    {
                        "id": "readback",
                        "objective": "Read the collected cold refs.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "readback",
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "artifact_refs": [
                            {
                                "selection_key": "selection-1",
                                "label": "cold action output",
                                "media_type": "text/markdown",
                                "bytes": 4096,
                                "sha256": "1" * 64,
                                "full_value_available": True,
                            }
                        ],
                        "file_refs": [
                            {
                                "path": "sources/source.md",
                                "role": "evidence",
                                "media_type": "text/markdown",
                                "bytes": 2048,
                                "sha256": "2" * 64,
                            }
                        ],
                    },
                }
            ],
        },
    )
    captured_work_units: list[dict[str, Any]] = []

    async def fake_run_work_unit_through_blocks(**kwargs: Any) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        work_unit = cast(Any, kwargs["work_unit"])
        work_unit_dict = work_unit.to_dict()
        captured_work_units.append(work_unit_dict)
        file_refs = [
            ref
            for ref in work_unit_dict["input_refs"]
            if isinstance(ref, dict) and str(ref.get("path") or "").strip()
        ]
        return (
            {
                "status": "completed",
                "answer": "readback captured",
                "readbacks": [{"ok": True, "selection_key": "selection-1"}],
                "file_readbacks": [{"ok": True, "path": "sources/source.md"}],
                "file_refs": file_refs,
                "remaining_work": [],
                "diagnostics": [],
            },
            {"execution_id": "readback-hot-provenance", "status": "completed", "logs": {}},
            WorkUnitResult(id=str(work_unit.id), status="completed"),
        )

    cast(Any, task)._run_work_unit_through_blocks = fake_run_work_unit_through_blocks
    card = revision.graph.card_by_id()["readback"]

    result = await task._run_taskboard_readback_card(
        SimpleNamespace(card=card, revision=revision),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    work_unit = captured_work_units[0]
    hot_text = json.dumps(work_unit["input_payload"], ensure_ascii=False)
    cold_text = json.dumps(work_unit["input_refs"], ensure_ascii=False)
    assert work_unit["input_payload"]["artifact_refs"][0]["selection_key"] == "selection-1"
    assert work_unit["input_payload"]["file_refs"][0]["path"] == "sources/source.md"
    assert '"sha256"' not in hot_text
    assert '"bytes"' not in hot_text
    assert '"media_type"' not in hot_text
    assert '"full_value_available"' not in hot_text
    assert '"sha256"' in cold_text
    assert '"bytes"' in cold_text
    assert '"media_type"' in cold_text


@pytest.mark.asyncio
async def test_taskboard_dependency_readback_work_unit_hot_payload_omits_programmatic_provenance_noise(tmp_path):
    agent = _create_agent("execution-taskboard-dependency-readback-hot-provenance").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Prefetch dependency artifact evidence without hot provenance noise.",
        success_criteria=["Dependency readback keeps full provenance out of model-hot payloads."],
        execution="taskboard",
        max_iterations=None,
    )
    captured_work_units: list[dict[str, Any]] = []

    async def fake_run_work_unit_through_blocks(**kwargs: Any) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        work_unit = cast(Any, kwargs["work_unit"])
        captured_work_units.append(work_unit.to_dict())
        return (
            {
                "schema_version": "agent_task_taskboard_dependency_readbacks/v1",
                "card_id": "synthesize",
                "ref_count": 1,
                "readbacks": [{"ok": True, "selection_key": "selection-1"}],
                "diagnostics": [],
            },
            {"execution_id": "dependency-readback-hot-provenance", "status": "completed", "logs": {}},
            WorkUnitResult(id=str(work_unit.id), status="completed"),
        )

    cast(Any, task)._run_work_unit_through_blocks = fake_run_work_unit_through_blocks
    output = await task._taskboard_dependency_action_artifact_readbacks(
        {
            "artifact_refs": [
                {
                    "selection_key": "selection-1",
                    "label": "large dependency output",
                    "media_type": "text/markdown",
                    "bytes": 999999,
                    "sha256": "1" * 64,
                    "full_value_available": True,
                }
            ]
        },
        card_id="synthesize",
        context_pack={"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert output["readbacks"][0]["selection_key"] == "selection-1"
    work_unit = captured_work_units[0]
    hot_text = json.dumps(work_unit["input_payload"], ensure_ascii=False)
    cold_text = json.dumps(work_unit["input_refs"], ensure_ascii=False)
    assert work_unit["input_payload"]["artifact_refs"][0]["selection_key"] == "selection-1"
    assert '"sha256"' not in hot_text
    assert '"bytes"' not in hot_text
    assert '"media_type"' not in hot_text
    assert '"full_value_available"' not in hot_text
    assert '"sha256"' not in cold_text
    assert '"bytes"' in cold_text
    assert '"media_type"' in cold_text


def test_taskboard_action_artifact_readback_preview_omits_ref_provenance_noise():
    compact = AgentTask._compact_taskboard_action_artifact_readback(
        {
            "ok": True,
            "status": "read",
            "selection_key": "selection-1",
            "media_type": "text/markdown",
            "value": {
                "summary": "bounded source note",
                "file_refs": [
                    {
                        "path": "retained/source.md",
                        "role": "evidence",
                        "media_type": "text/markdown",
                        "bytes": 4096,
                        "sha256": "1" * 64,
                    }
                ],
            },
            "meta": {"sha256": "2" * 64, "bytes": 8192},
        },
        {
            "selection_key": "selection-1",
            "media_type": "text/markdown",
            "bytes": 4096,
            "sha256": "1" * 64,
        },
    )

    hot_text = json.dumps(compact, ensure_ascii=False)
    assert compact["selection_key"] == "selection-1"
    assert compact["value_preview"]["summary"] == "bounded source note"
    assert compact["value_preview"]["file_refs"][0]["path"] == "retained/source.md"
    assert '"sha256"' not in hot_text
    assert '"bytes"' not in hot_text
    assert '"media_type"' not in hot_text


def test_taskboard_readback_evidence_dedupes_by_owner_version_and_range():
    ref = {
        "selection_key": "selection-1",
        "owner": "action_artifact",
        "locator": "selection-1",
        "content_version": "sha256:version-1",
        "role": "output",
    }
    readback = {
        "ok": True,
        "status": "success",
        "selection_key": "selection-1",
        "owner": "action_artifact",
        "locator": "selection-1",
        "content_version": "sha256:version-1",
        "range": {"offset": 0, "end": 128, "read_bytes": 128},
        "value_preview": "bounded source segment",
        "value_preview_meta": {"truncated": True},
        "ref": ref,
    }
    first = AgentTask._taskboard_action_artifact_readback_evidence_items(
        [readback],
        source="taskboard_dependency_readback",
        card_id="first-card",
    )[0]
    second = AgentTask._taskboard_action_artifact_readback_evidence_items(
        [readback],
        source="taskboard_dependency_readback",
        card_id="second-card",
    )[0]

    deduped = AgentTask._dedupe_taskboard_readback_evidence_items([first, second])

    assert len(deduped) == 1
    assert deduped[0]["read_identity"] == {
        "owner": "action_artifact",
        "locator": "selection-1",
        "content_version": "sha256:version-1",
        "range": {"offset": 0, "end": 128},
    }
    assert deduped[0]["id"] == second["id"] == first["id"]


@pytest.mark.asyncio
async def test_taskboard_dependency_readback_advances_past_prior_range(tmp_path):
    agent = _create_agent("execution-taskboard-progressive-action-readback").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-progressive-action-readback",
        goal="Read the next unseen Action artifact segment.",
        success_criteria=["Repeated readback advances its byte range."],
        execution="taskboard",
    )
    scope = {"kind": "agent_task", "id": task.id}
    artifact = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="progressive-readback-call",
        artifact_type="action_output",
        label="Large repository search result",
        value="0123456789" * 2000,
        artifact_scope=scope,
    )
    output = await task._taskboard_dependency_action_artifact_readbacks(
        {
            "artifact_refs": [artifact],
            "evidence_items": [
                    {
                        "id": "prior-segment",
                        "kind": "taskboard_action_artifact.readback",
                        "status": "ok",
                        "body_state": "truncated",
                        "body": "0123456789" * 6 + "0123",
                    "read_identity": {
                        "owner": "action_artifact",
                        "locator": artifact.get("selection_key"),
                        "content_version": artifact.get("sha256"),
                        "range": {"offset": 0, "end": 64},
                    },
                }
            ],
        },
        card_id="dependent-card",
        context_pack={"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert output["readbacks"][0]["range"]["offset"] == 64
    assert output["readbacks"][0]["range"]["end"] > 64


@pytest.mark.asyncio
async def test_taskboard_dependency_readback_skips_exhausted_artifact(tmp_path):
    agent = _create_agent("execution-taskboard-exhausted-action-readback").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-exhausted-action-readback",
        goal="Do not reread an exhausted Action artifact.",
        success_criteria=["Exhausted refs do not trigger another Action read."],
        execution="taskboard",
    )
    scope = {"kind": "agent_task", "id": task.id}
    artifact = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="exhausted-readback-call",
        artifact_type="action_output",
        label="Complete repository search result",
        value="0123456789" * 2000,
        artifact_scope=scope,
    )
    output = await task._taskboard_dependency_action_artifact_readbacks(
        {
            "artifact_refs": [artifact],
            "evidence_items": [
                    {
                        "id": "complete-segment",
                        "kind": "taskboard_action_artifact.readback",
                        "status": "ok",
                        "body_state": "full",
                        "body": "complete Action artifact body",
                    "total_bytes": artifact.get("bytes"),
                    "read_identity": {
                        "owner": "action_artifact",
                        "locator": artifact.get("selection_key"),
                        "content_version": artifact.get("sha256"),
                        "range": {"offset": 0, "end": artifact.get("bytes")},
                    },
                }
            ],
        },
        card_id="dependent-card",
        context_pack={"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert output["ref_count"] == 0
    assert output["exhausted_ref_count"] == 1
    assert output["readbacks"] == []
    assert output["diagnostics"][0]["code"] == "taskboard.dependency_readback.no_unread_ranges"
    assert output["frontier"]["integrity_complete"] is True
    assert output["frontier"]["exhausted"] is True
    assert output["frontier"]["pending_ref_count"] == 0


def test_taskboard_task_level_read_progress_tracks_versioned_contiguous_ranges(tmp_path):
    task = AgentTask(
        _create_agent("execution-taskboard-task-read-progress").use_task_workspace(
            tmp_path / "task_workspace"
        ),
        task_id="taskboard-task-read-progress",
        goal="Track bounded reads across TaskBoard evidence projections.",
        success_criteria=["Read progress remains task-owned and version-safe."],
        execution="taskboard",
    )
    evidence_view = {"evidence_items": []}
    ref = {
        "owner": "action_artifact",
        "selection_key": "sel-progress",
        "locator": "sel-progress",
        "content_version": "version-a",
        "bytes": 30_000,
    }

    for offset, end in ((0, 12_000), (12_000, 24_000)):
        task._record_taskboard_read_progress(
            {
                "ok": True,
                **ref,
                "total_bytes": 30_000,
                "range": {"offset": offset, "end": end},
            },
            card_id=f"read-{offset}",
        )

    assert task._taskboard_requested_read_range(
        ref,
        evidence_view,
        default_max_bytes=12_000,
    ) == (24_000, 12_000)
    assert task._taskboard_read_target_exhausted(ref, evidence_view) is False
    assert "taskboard_read_progress" not in task.diagnostics

    task._record_taskboard_read_progress(
        {
            "ok": True,
            **ref,
            "total_bytes": 30_000,
            "range": {"offset": 24_000, "end": 30_000},
        },
        card_id="read-final",
    )

    assert task._taskboard_read_target_exhausted(ref, evidence_view) is True
    assert task._taskboard_requested_read_range(
        {**ref, "content_version": "version-b"},
        evidence_view,
        default_max_bytes=12_000,
    ) == (0, 12_000)
    assert task._taskboard_requested_read_range(
        {
            "owner": "task_workspace",
            "locator": "sources/mutable.md",
            "path": "sources/mutable.md",
        },
        evidence_view,
        default_max_bytes=12_000,
    ) == (0, 12_000)


def test_taskboard_control_readback_required_patch_type_becomes_readback_patch():
    validator = TaskBoardValidator()
    revision = TaskBoardRevision.create(
        board_id="readback-required-patch-type",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "readback-required-patch-type-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect source evidence."},
                    {
                        "id": "analyze",
                        "objective": "Analyze source evidence after cold readback.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                    },
                ],
            }
        ),
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "artifact_refs": [
                            {
                                "artifact_id": "source-artifact",
                                "action_call_id": "source-call",
                                "role": "output",
                            }
                        ],
                    },
                }
            ],
        },
    )
    card = revision.graph.card_by_id()["analyze"]
    diagnostics: list[dict[str, Any]] = []
    patch = AgentTask._taskboard_control_patch_proposal(
        SimpleNamespace(revision=revision, card=card),
        {
            "status": "blocked",
            "patch_proposal": {
                "patch_type": "readback_required",
                "readback_targets": [
                    {
                        "artifact_id": "source-artifact",
                        "action_call_id": "source-call",
                    }
                ],
            },
            "gaps": ["Need the cold artifact body."],
            "remaining_work": ["Continue after readback."],
        },
        diagnostics,
    )

    assert patch is not None
    assert diagnostics[0]["code"] == "taskboard.control.invalid_model_patch_proposal"
    next_revision = validator.apply_patch(revision, patch)
    cards = next_revision.graph.card_by_id()
    assert cards["analyze.readback"].allowed_execution_shape == "readback"
    assert cards["analyze.readback"].depends_on == ("collect",)
    assert cards["analyze.continue"].depends_on == ("analyze", "collect", "analyze.readback")


def test_taskboard_control_blocked_output_does_not_allow_task_workspace_delivery():
    assert AgentTask._taskboard_control_output_allows_task_workspace_delivery(
        {
            "status": "blocked",
            "sufficient": False,
            "next_board_action": "readback",
            "artifact_manifest": {"path": "final.md", "sections": [{"id": "deliverable"}]},
            "remaining_work": ["Read scoped evidence before generating the deliverable."],
        }
    ) is False
    assert AgentTask._taskboard_control_output_allows_task_workspace_delivery(
        {
            "status": "completed",
            "sufficient": True,
            "artifact_manifest": {"path": "final.md", "sections": [{"id": "deliverable"}]},
            "remaining_work": [],
            "gaps": [],
        }
    ) is True
    assert AgentTask._taskboard_control_output_allows_task_workspace_delivery(
        {
            "status": "completed",
            "sufficient": True,
            "next_board_action": "finalize",
            "artifact_manifest": {"path": "final.md", "sections": [{"id": "deliverable"}]},
            "remaining_work": [],
            "gaps": ["Non-fatal evidence limitation disclosed for verifier review."],
        }
    ) is True
    assert AgentTask._taskboard_control_output_allows_task_workspace_delivery(
        {
            "status": "completed",
            "sufficient": True,
            "artifact_manifest": {"path": "final.md", "sections": [{"id": "deliverable"}]},
            "remaining_work": ["Write the actual deliverable body."],
            "gaps": [],
        }
    ) is False


@pytest.mark.asyncio
async def test_task_workspace_artifact_delivery_blocks_empty_body_without_trusted_readback(tmp_path):
    agent = _create_agent("execution-workspace-artifact-empty-body").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Produce a trusted TaskWorkspace artifact.",
        success_criteria=["final.md is written and read back."],
        execution="taskboard",
        max_iterations=None,
    )
    execution_meta: dict[str, Any] = {"logs": {"action_logs": {}, "route_logs": {}, "errors": []}}

    delivered = await task._deliver_task_workspace_artifact(
        {"status": "completed", "artifact_manifest": {"path": "final.md"}},
        plan={"deliverable_mode": "task_workspace_artifact"},
        execution_meta=execution_meta,
        source="test.task_workspace_artifact.empty_body",
        allow_stream_draft=False,
    )

    assert delivered["status"] == "blocked"
    assert delivered["task_workspace_artifact_delivery"]["status"] == "failed"
    assert delivered["task_workspace_artifact_delivery"]["error"]["type"] == "EmptyWorkspaceArtifactBody"
    assert delivered["diagnostics"][0]["code"] == "agent_task.task_workspace_artifact.empty_body"
    assert not (task.task_workspace.root / "final.md").exists()
    evidence_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    assert evidence_items[0]["status"] == "failed"
    assert evidence_items[0]["supports"]["unavailability"] is True


@pytest.mark.asyncio
async def test_task_workspace_artifact_delivery_blocks_structured_wrapper_body(tmp_path):
    agent = _create_agent("execution-workspace-artifact-wrapper-body").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Produce a natural Markdown TaskWorkspace artifact.",
        success_criteria=["final.md is written as natural text."],
        execution="taskboard",
        max_iterations=None,
    )
    execution_meta: dict[str, Any] = {"logs": {"action_logs": {}, "route_logs": {}, "errors": []}}

    delivered = await task._deliver_task_workspace_artifact(
        {
            "status": "completed",
            "artifact_manifest": {"path": "final.md"},
            "artifact_markdown": json.dumps({"answer": "# Final\n\nWrapped body."}),
        },
        plan={"deliverable_mode": "task_workspace_artifact"},
        execution_meta=execution_meta,
        source="test.task_workspace_artifact.wrapper_body",
        allow_stream_draft=False,
    )

    assert delivered["status"] == "blocked"
    assert delivered["task_workspace_artifact_delivery"]["status"] == "failed"
    assert delivered["task_workspace_artifact_delivery"]["error"]["type"] == "StructuredWorkspaceArtifactBody"
    assert delivered["diagnostics"][0]["code"] == "agent_task.task_workspace_artifact.structured_wrapper_body"
    assert not (task.task_workspace.root / "final.md").exists()


@pytest.mark.asyncio
async def test_taskboard_control_task_workspace_patch_materializes_file_without_graph_patch(tmp_path):
    agent = _create_agent("execution-taskboard-workspace-patch").use_task_workspace(
        tmp_path / "task_workspace",
        mode="read_write",
    )
    task = AgentTask(
        agent,
        goal="Repair a TaskWorkspace-backed final deliverable.",
        success_criteria=["The corrected TaskWorkspace file is available through trusted readback refs."],
        execution="taskboard",
        max_iterations=None,
    )
    await task.task_workspace.write_file(
        "final.md",
        "# Final\n\nCoverage: stale label\n\nKeep this line.\n",
        append=False,
    )

    async def fake_run_work_unit_through_blocks(**kwargs: Any) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        work_unit = cast(Any, kwargs["work_unit"])
        output = {
            "status": "completed",
            "sufficient": True,
            "answer": "Patch the final TaskWorkspace artifact.",
            "next_board_action": "patch",
            "remaining_work": [],
            "gaps": [],
            "patch_proposal": {
                "file": "final.md",
                "operations": [
                    {
                        "type": "replace",
                        "old": "Coverage: stale label",
                        "new": "Coverage: corrected label",
                    }
                ],
            },
        }
        return (
            output,
            {"status": "completed", "logs": {"action_logs": {}, "route_logs": {}, "errors": []}},
            WorkUnitResult(id=str(work_unit.id), status="completed"),
        )

    cast(Any, task)._run_work_unit_through_blocks = fake_run_work_unit_through_blocks
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "profile": "test",
        "omitted": [],
        "diagnostics": {},
    }
    revision = TaskBoardRevision.create(
        board_id="workspace-patch",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-patch-graph",
                "cards": [
                    {
                        "id": "repair",
                        "objective": "Apply a precise correction to final.md.",
                        "allowed_execution_shape": "control",
                        "metadata": {"final_task_workspace_deliverables": ["final.md"]},
                    }
                ],
            }
        ),
    )
    card = revision.graph.card_by_id()["repair"]

    result = await task._run_taskboard_control_card(
        SimpleNamespace(revision=revision, card=card, dependency_results={}, planning_policy=None),
        context_pack,
    )
    readback = await task.task_workspace.read_file("final.md", max_bytes=4000)

    assert result.status == "completed"
    assert result.patch_proposal is None
    assert result.file_refs
    assert task._task_workspace_artifact_display_path(result.file_refs[0]["path"]) == "final.md"
    assert result.file_refs[0]["role"] == "task_workspace_artifact"
    assert "Coverage: corrected label" in readback["content"]
    assert "Coverage: stale label" not in readback["content"]
    assert any(
        diagnostic.get("code") == "taskboard.control.task_workspace_patch_applied"
        for diagnostic in result.diagnostics
    )
    assert result.preview["task_workspace_patch_delivery"]["status"] == "completed"
    assert "task_workspace_patch_proposal" in result.preview
    assert "patch_proposal" not in result.preview


@pytest.mark.asyncio
async def test_taskboard_control_task_workspace_text_patch_writes_complete_file(tmp_path):
    agent = _create_agent("execution-taskboard-workspace-text-patch").use_task_workspace(
        tmp_path / "task_workspace",
        mode="read_write",
    )
    task = AgentTask(
        agent,
        goal="Repair a TaskWorkspace-backed final deliverable by replacing the file body.",
        success_criteria=["The corrected TaskWorkspace file is available through trusted readback refs."],
        execution="taskboard",
        max_iterations=None,
    )
    await task.task_workspace.write_file("final.md", "# Old\n\nUnsupported claim.", append=False)
    corrected = "# Final\n\nCorrected source-grounded deliverable."

    async def fake_run_work_unit_through_blocks(**kwargs: Any) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        work_unit = cast(Any, kwargs["work_unit"])
        output = {
            "status": "completed",
            "sufficient": True,
            "answer": "Replace the final TaskWorkspace artifact with the corrected body.",
            "next_board_action": "patch",
            "remaining_work": [],
            "gaps": [],
            "patch_proposal": {
                "type": "task_workspace_text_patch",
                "path": "final.md",
                "content": corrected,
            },
        }
        return (
            output,
            {"status": "completed", "logs": {"action_logs": {}, "route_logs": {}, "errors": []}},
            WorkUnitResult(id=str(work_unit.id), status="completed"),
        )

    cast(Any, task)._run_work_unit_through_blocks = fake_run_work_unit_through_blocks
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "profile": "test",
        "omitted": [],
        "diagnostics": {},
    }
    revision = TaskBoardRevision.create(
        board_id="workspace-text-patch",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-text-patch-graph",
                "cards": [
                    {
                        "id": "repair",
                        "objective": "Apply a complete corrected final.md body.",
                        "allowed_execution_shape": "control",
                        "metadata": {"final_task_workspace_deliverables": ["final.md"]},
                    }
                ],
            }
        ),
    )
    card = revision.graph.card_by_id()["repair"]

    result = await task._run_taskboard_control_card(
        SimpleNamespace(revision=revision, card=card, dependency_results={}, planning_policy=None),
        context_pack,
    )
    readback = await task.task_workspace.read_file("final.md", max_bytes=4000)

    assert result.status == "completed"
    assert result.patch_proposal is None
    assert result.file_refs
    assert task._task_workspace_artifact_display_path(result.file_refs[0]["path"]) == "final.md"
    assert readback["content"] == corrected
    assert result.preview["task_workspace_patch_delivery"]["operation_count"] == 1
    assert result.preview["task_workspace_patch_delivery"]["operations"][0]["type"] == "write"


@pytest.mark.asyncio
async def test_taskboard_control_task_workspace_text_patch_patches_alias_applies_replace(tmp_path):
    agent = _create_agent("execution-taskboard-workspace-text-patch-patches-alias").use_task_workspace(
        tmp_path / "task_workspace",
        mode="read_write",
    )
    task = AgentTask(
        agent,
        goal="Repair a TaskWorkspace-backed final deliverable by applying a targeted patch.",
        success_criteria=["The corrected TaskWorkspace file is available through trusted readback refs."],
        execution="taskboard",
        max_iterations=None,
    )
    await task.task_workspace.write_file(
        "final.md",
        "# Final\n\n**Covered window:** 2026-02-12 - 2026-06-30\n\n## Source Methodology\n",
        append=False,
    )

    async def fake_run_work_unit_through_blocks(**kwargs: Any) -> tuple[Any, dict[str, Any], WorkUnitResult]:
        work_unit = cast(Any, kwargs["work_unit"])
        output = {
            "status": "completed",
            "sufficient": True,
            "answer": "Apply the targeted Date Window section patch.",
            "next_board_action": "patch",
            "remaining_work": [],
            "gaps": [],
            "patch_proposal": {
                "kind": "task_workspace_text_patch",
                "path": "final.md",
                "patches": [
                    {
                        "old_text": "**Covered window:** 2026-02-12 - 2026-06-30",
                        "new_text": "## Date Window\n\n2026-02-12 - 2026-06-30",
                    }
                ],
            },
        }
        return (
            output,
            {"status": "completed", "logs": {"action_logs": {}, "route_logs": {}, "errors": []}},
            WorkUnitResult(id=str(work_unit.id), status="completed"),
        )

    cast(Any, task)._run_work_unit_through_blocks = fake_run_work_unit_through_blocks
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "profile": "test",
        "omitted": [],
        "diagnostics": {},
    }
    revision = TaskBoardRevision.create(
        board_id="workspace-text-patch-patches-alias",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-text-patch-patches-alias-graph",
                "cards": [
                    {
                        "id": "repair",
                        "objective": "Apply a targeted final.md patch.",
                        "allowed_execution_shape": "control",
                        "metadata": {"final_task_workspace_deliverables": ["final.md"]},
                    }
                ],
            }
        ),
    )
    card = revision.graph.card_by_id()["repair"]

    result = await task._run_taskboard_control_card(
        SimpleNamespace(revision=revision, card=card, dependency_results={}, planning_policy=None),
        context_pack,
    )
    readback = await task.task_workspace.read_file("final.md", max_bytes=4000)

    assert result.status == "completed"
    assert result.patch_proposal is None
    assert result.file_refs
    assert task._task_workspace_artifact_display_path(result.file_refs[0]["path"]) == "final.md"
    assert "## Date Window" in readback["content"]
    assert "**Covered window:**" not in readback["content"]
    assert result.preview["task_workspace_patch_delivery"]["status"] == "completed"
    assert result.preview["task_workspace_patch_delivery"]["operations"][0]["type"] == "replace"


@pytest.mark.asyncio
async def test_taskboard_readback_card_reads_task_workspace_file_refs(tmp_path):
    agent = _create_agent("execution-taskboard-workspace-file-readback").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Read cold TaskWorkspace file evidence.",
        success_criteria=["The readback card reads the TaskWorkspace file ref."],
        execution="taskboard",
        max_iterations=None,
    )
    write_result = await task.task_workspace.write_file(
        "sources/source.md",
        "# Official Evidence\n\nWorkspace-only detail that is not in the hot preview.",
    )
    file_ref = dict(write_result["file_refs"][0])
    revision = TaskBoardRevision.create(
        board_id="workspace-file-readback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-file-readback-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect TaskWorkspace file evidence."},
                    {
                        "id": "readback",
                        "objective": "Read scoped TaskWorkspace file evidence.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "readback",
                        "required_outputs": ["TaskWorkspace file content preview."],
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "file ref only",
                        "file_refs": [file_ref],
                    },
                }
            ],
        },
    )
    card = revision.graph.card_by_id()["readback"]
    result = await task._run_taskboard_readback_card(
        SimpleNamespace(
            card=card,
            revision=revision,
        ),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    payload = result.preview
    assert payload["file_readbacks"][0]["ok"] is True
    assert payload["file_readbacks"][0]["path"] == file_ref["path"]
    assert "Workspace-only detail" in payload["file_readbacks"][0]["content_preview"]
    assert result.metadata["execution_kind"] == "taskboard_artifact_readback"
    assert result.metadata["file_ref_count"] == 1
    ledger_items = result.metadata["evidence_ledger"]["items"]
    readback_items = [item for item in ledger_items if item.get("kind") == "task_workspace_file.readback"]
    assert readback_items
    assert readback_items[0]["path"] == file_ref["path"]
    assert readback_items[0]["status"] == "ok"
    assert readback_items[0]["supports"]["content"] is True
    assert "Workspace-only detail" in readback_items[0]["body"]

    updated_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": result.to_dict()}],
        },
    )
    evidence_view = build_task_board_evidence_view(updated_revision).to_dict()
    projected_items = [
        item for item in evidence_view["evidence_items"] if item.get("kind") == "task_workspace_file.readback"
    ]
    assert projected_items
    assert "Workspace-only detail" in projected_items[0]["body"]


@pytest.mark.asyncio
async def test_taskboard_readback_card_reads_task_workspace_target_refs_from_content_store(tmp_path):
    agent = _create_agent("execution-taskboard-content-target-readback").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Read retained TaskWorkspace content before synthesis.",
        success_criteria=["The readback card reads retained TaskWorkspace content refs."],
        execution="taskboard",
        max_iterations=None,
    )
    retained_ref = await task.record_store.put(
        "The only active blocker is the data processing addendum waiting on legal review.",
        collection="retained-notes",
        kind="operations_note",
        summary="Operations note with blocker.",
    )
    revision = TaskBoardRevision.create(
        board_id="workspace-target-ref-readback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-target-ref-readback-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect retained note refs."},
                    {
                        "id": "readback",
                        "objective": "Read the retained operations note.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "readback",
                        "required_outputs": ["TaskWorkspace content readback preview."],
                        "metadata": {
                            "target_refs": [
                                {"owner": "record_store", "locator": retained_ref["id"]}
                            ]
                        },
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "retained note path collected",
                    },
                }
            ],
        },
    )
    card = revision.graph.card_by_id()["readback"]
    result = await task._run_taskboard_readback_card(
        SimpleNamespace(
            card=card,
            revision=revision,
        ),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    payload = result.preview
    assert payload["file_readbacks"][0]["ok"] is True
    assert payload["file_readbacks"][0]["path"] == retained_ref["id"]
    assert "data processing addendum waiting on legal review" in payload["file_readbacks"][0]["content_preview"]
    hot_text = json.dumps(payload["file_readbacks"][0], ensure_ascii=False)
    assert '"sha256"' not in hot_text
    assert '"bytes"' not in hot_text
    assert '"media_type"' not in hot_text
    assert result.metadata["file_ref_count"] == 1


@pytest.mark.asyncio
async def test_taskboard_task_workspace_readback_advances_past_prior_range(tmp_path):
    agent = _create_agent("execution-taskboard-progressive-file-readback").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        task_id="taskboard-progressive-file-readback",
        goal="Read consecutive unseen segments from one TaskWorkspace file.",
        success_criteria=["The second read starts after the first bounded range."],
        execution="taskboard",
    )
    write_result = await task.task_workspace.write_file(
        "sources/large.md",
        "0123456789" * 3000,
    )
    content_version = str(write_result["sha256"])
    target = {
        "owner": "task_workspace",
        "locator": "sources/large.md",
        "content_version": content_version,
    }
    revision = TaskBoardRevision.create(
        board_id=task.id,
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {"id": "collect", "objective": "Identify the large source file."},
                    {
                        "id": "read-first",
                        "objective": "Read the first bounded source segment.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "readback",
                        "metadata": {"target_refs": [target]},
                    },
                    {
                        "id": "read-second",
                        "objective": "Read the next unseen bounded source segment.",
                        "depends_on": ["read-first"],
                        "allowed_execution_shape": "readback",
                        "metadata": {"target_refs": [target]},
                    },
                    {
                        "id": "read-third",
                        "objective": "Read the final unseen source segment.",
                        "depends_on": ["read-second"],
                        "allowed_execution_shape": "readback",
                        "metadata": {"target_refs": [target]},
                    },
                    {
                        "id": "read-fourth",
                        "objective": "Do not reread an exhausted source.",
                        "depends_on": ["read-third"],
                        "allowed_execution_shape": "readback",
                        "metadata": {"target_refs": [target]},
                    },
                ],
            }
        ),
    )
    validator = TaskBoardValidator()
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {"op": "record_card_result", "result": {"card_id": "collect", "status": "completed"}}
            ],
        },
    )
    first = await task._run_taskboard_readback_card(
        SimpleNamespace(card=revision.graph.card_by_id()["read-first"], revision=revision),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": first.to_dict()}],
        },
    )
    second = await task._run_taskboard_readback_card(
        SimpleNamespace(card=revision.graph.card_by_id()["read-second"], revision=revision),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": second.to_dict()}],
        },
    )
    third = await task._run_taskboard_readback_card(
        SimpleNamespace(card=revision.graph.card_by_id()["read-third"], revision=revision),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )
    revision = validator.apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": third.to_dict()}],
        },
    )
    fourth = await task._run_taskboard_readback_card(
        SimpleNamespace(card=revision.graph.card_by_id()["read-fourth"], revision=revision),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    first_readback = first.preview["file_readbacks"][0]
    second_readback = second.preview["file_readbacks"][0]
    assert first_readback["offset"] == 0
    assert second_readback["offset"] == first_readback["read_bytes"]
    assert second_readback["content_preview"] != first_readback["content_preview"] or (
        second_readback["offset"] != first_readback["offset"]
    )
    assert third.preview["file_readbacks"][0]["offset"] == (
        second_readback["offset"] + second_readback["read_bytes"]
    )
    assert fourth.status == "completed"
    assert fourth.preview["file_readbacks"] == []
    fourth_diagnostic = fourth.preview["diagnostics"][0]
    assert fourth_diagnostic["code"] == "taskboard.readback.no_unread_ranges"
    assert fourth_diagnostic["card_id"] == "read-fourth"
    assert fourth_diagnostic["exhausted_ref_count"] == 1


@pytest.mark.asyncio
async def test_taskboard_readback_card_reads_task_workspace_file_refs_from_artifact_refs(tmp_path):
    agent = _create_agent("execution-taskboard-workspace-file-readback-legacy").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Read cold TaskWorkspace file evidence from an upstream card result.",
        success_criteria=["The readback card reads the TaskWorkspace file ref."],
        execution="taskboard",
        max_iterations=None,
    )
    write_result = await task.task_workspace.write_file(
        "deliverables/final.md",
        "# Final Deliverable\n\nThe complete TaskWorkspace-backed deliverable body is here.",
    )
    file_ref = dict(write_result["file_refs"][0])
    revision = TaskBoardRevision.create(
        board_id="workspace-file-readback-artifact-ref",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-file-readback-artifact-ref-graph",
                "cards": [
                    {"id": "draft", "objective": "Write the TaskWorkspace-backed deliverable."},
                    {
                        "id": "readback",
                        "objective": "Read the upstream TaskWorkspace-backed deliverable.",
                        "depends_on": ["draft"],
                        "allowed_execution_shape": "readback",
                        "required_outputs": ["TaskWorkspace file content preview."],
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": "TaskWorkspace artifact delivered at deliverables/final.md",
                        "artifact_refs": [file_ref],
                    },
                }
            ],
        },
    )
    card = revision.graph.card_by_id()["readback"]
    result = await task._run_taskboard_readback_card(
        SimpleNamespace(
            card=card,
            revision=revision,
        ),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    assert result.metadata["file_ref_count"] == 1
    assert result.file_refs[0]["path"] == file_ref["path"]
    assert result.preview["file_readbacks"][0]["ok"] is True
    assert "complete TaskWorkspace-backed deliverable body" in result.preview["file_readbacks"][0]["content_preview"]


@pytest.mark.asyncio
async def test_taskboard_readback_card_projects_required_final_deliverable_as_artifact_readback(tmp_path):
    agent = _create_agent("execution-taskboard-final-artifact-readback-evidence").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Verify a final TaskWorkspace deliverable through readback evidence.",
        success_criteria=["The final deliverable body is verifier-visible."],
        execution="taskboard",
        max_iterations=None,
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    write_result = await task.task_workspace.write_file(
        "final.md",
        "# Final Report\n\nThe final TaskWorkspace artifact body is verifier-visible.",
    )
    file_ref = dict(write_result["file_refs"][0])
    revision = TaskBoardRevision.create(
        board_id="workspace-final-readback-artifact-evidence",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "workspace-final-readback-artifact-evidence-graph",
                "cards": [
                    {"id": "draft", "objective": "Write final.md."},
                    {
                        "id": "readback",
                        "objective": "Read the final TaskWorkspace artifact.",
                        "depends_on": ["draft"],
                        "allowed_execution_shape": "readback",
                        "required_outputs": ["TaskWorkspace artifact body readback evidence."],
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": "TaskWorkspace artifact delivered at final.md",
                        "artifact_refs": [file_ref],
                    },
                }
            ],
        },
    )

    card = revision.graph.card_by_id()["readback"]
    result = await task._run_taskboard_readback_card(
        SimpleNamespace(card=card, revision=revision),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    ledger_items = result.metadata["evidence_ledger"]["items"]
    artifact_items = [item for item in ledger_items if item.get("kind") == "task_workspace_artifact.readback"]
    assert artifact_items
    assert artifact_items[0]["path"] == file_ref["path"]
    assert artifact_items[0]["supports"]["content"] is True
    assert "final TaskWorkspace artifact body" in artifact_items[0]["body"]

    updated_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": result.to_dict()}],
        },
    )
    evidence_view = build_task_board_evidence_view(updated_revision).to_dict()
    projected_items = [
        item for item in evidence_view["evidence_items"] if item.get("kind") == "task_workspace_artifact.readback"
    ]
    assert projected_items
    assert "final TaskWorkspace artifact body" in projected_items[0]["body"]


@pytest.mark.asyncio
async def test_taskboard_readback_card_promotes_nested_task_workspace_file_refs_from_action_readback(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("execution-taskboard-nested-workspace-file-readback").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Read a downloaded file ref discovered inside Action artifact readback.",
        success_criteria=["The readback card promotes nested TaskWorkspace file refs."],
        execution="taskboard",
        max_iterations=None,
    )
    write_result = await task.task_workspace.write_file(
        "downloads/source.md",
        "# Downloaded Source\n\nWorkspace-only PDF text extracted after download.",
    )
    file_ref = dict(write_result["file_refs"][0])
    file_ref["role"] = "download"

    async def fake_read_action_artifact(
        selection_key: str,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "success",
            "selection_key": selection_key,
            "owner": "action_artifact",
            "locator": selection_key,
            "content_version": "nested-file-ref-v1",
            "range": {"offset": offset, "end": offset + int(max_bytes or 0), "read_bytes": int(max_bytes or 0)},
            "artifact_type": "action_output",
            "value": {
                "kind": "remote_file",
                "file_refs": [file_ref],
                "read_preview": {
                    "content": "Only the first page preview is here.",
                    "file_refs": [file_ref],
                    "truncated": True,
                },
            },
        }

    monkeypatch.setattr(agent.action, "async_read_action_artifact", fake_read_action_artifact, raising=False)
    revision = TaskBoardRevision.create(
        board_id="nested-workspace-file-readback",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "nested-workspace-file-readback-graph",
                "cards": [
                    {"id": "collect", "objective": "Download source material."},
                    {
                        "id": "readback",
                        "objective": "Read scoped downloaded TaskWorkspace file evidence.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "readback",
                        "required_outputs": ["Downloaded file content preview."],
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "Action artifact ref only; file refs are nested in readback.",
                        "artifact_refs": [
                            {
                                "selection_key": "sel-download-output",
                                "artifact_type": "action_output",
                                "available": True,
                                "full_value_available": True,
                                "role": "output",
                            }
                        ],
                    },
                }
            ],
        },
    )
    card = revision.graph.card_by_id()["readback"]
    result = await task._run_taskboard_readback_card(
        SimpleNamespace(
            card=card,
            revision=revision,
        ),
        {"goal": task.goal, "profile": "", "items": [], "omitted": [], "diagnostics": {}},
    )

    assert result.status == "completed"
    assert result.file_refs[0]["path"] == file_ref["path"]
    assert result.file_refs[0]["requested_path"] == "downloads/source.md"
    assert result.metadata["file_ref_count"] == 1
    assert result.metadata["file_success_count"] == 1
    payload = result.preview
    assert payload["file_refs"][0]["path"] == file_ref["path"]
    assert payload["file_readbacks"][0]["ok"] is True
    assert "Workspace-only PDF text" in payload["file_readbacks"][0]["content_preview"]
    assert any(
        item.get("code") == "taskboard.readback.task_workspace_file_refs_discovered"
        for item in payload["diagnostics"]
        if isinstance(item, dict)
    )
    ledger_items = result.metadata["evidence_ledger"]["items"]
    readback_item = next(
        item for item in ledger_items if item.get("kind") == "taskboard_action_artifact.readback"
    )
    assert readback_item["status"] == "ok"
    assert readback_item["body_state"] in {"bounded", "truncated"}
    assert readback_item["selection_key"] == "sel-download-output"
    assert "Only the first page preview" in readback_item["body"]
    assert "sel-download-output" in readback_item["aliases"]

    next_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [{"op": "record_card_result", "result": result.to_dict()}],
        },
    )
    evidence_view = build_task_board_evidence_view(next_revision).to_dict()
    assert any(
        item.get("id") == readback_item["id"]
        and item.get("body_state") in {"bounded", "truncated"}
        and "Only the first page preview" in item.get("body", "")
        for item in evidence_view["evidence_items"]
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_taskboard_intermediate_card_relocates_required_final_deliverable_path(tmp_path):
    agent = _create_agent("execution-taskboard-intermediate-final-path").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Use an intermediate card artifact before the final deliverable.",
        success_criteria=["Intermediate artifacts do not occupy final.md."],
        execution="taskboard",
        max_iterations=None,
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    revision = TaskBoardRevision.create(
        board_id="intermediate-final-path",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "intermediate-final-path-graph",
                "cards": [
                    {"id": "extract", "objective": "Write an intermediate source summary."},
                    {
                        "id": "synthesize",
                        "objective": "Write final.md.",
                        "depends_on": ["extract"],
                    },
                ],
            }
        ),
    )
    context = SimpleNamespace(
        card=revision.graph.card_by_id()["extract"],
        revision=revision,
    )
    prepared, plan = task._prepare_taskboard_task_workspace_artifact_delivery(
        {
            "artifact_markdown": "# Source Summary\n\nIntermediate evidence only.",
            "artifact_manifest": {"path": "final.md"},
        },
        context,
        deliverable_mode="task_workspace_artifact",
    )

    delivered = await task._deliver_task_workspace_artifact(
        prepared,
        plan=plan,
        execution_meta={"logs": {}},
        source="test.taskboard.intermediate.task_workspace_artifact",
        card_context=context,
    )

    assert task._task_workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == (
        "working/taskboard/extract/final.md"
    )
    assert task.task_workspace.resolve_file_path(delivered["file_refs"][0]["path"]).is_file()
    assert not (task.task_workspace.root / "final.md").exists()
    assert delivered["diagnostics"][0]["code"] == (
        "taskboard.task_workspace_artifact.final_path_relocated_for_intermediate_card"
    )


def test_empty_artifact_manifest_does_not_request_task_workspace_delivery():
    assert AgentTask._task_workspace_artifact_delivery_mode({"artifact_manifest": {}}) == ""


@pytest.mark.asyncio
async def test_taskboard_intermediate_card_relocates_explicit_artifact_without_task_output_contract(tmp_path):
    agent = _create_agent("execution-taskboard-intermediate-uncontracted-path").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Use an intermediate card artifact before the final deliverable.",
        success_criteria=["Intermediate artifacts do not occupy final.md."],
        execution="taskboard",
        max_iterations=None,
    )
    revision = TaskBoardRevision.create(
        board_id="intermediate-uncontracted-path",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "intermediate-uncontracted-path-graph",
                "cards": [
                    {"id": "extract", "objective": "Write an intermediate source summary."},
                    {
                        "id": "synthesize",
                        "objective": "Write final.md.",
                        "depends_on": ["extract"],
                    },
                ],
            }
        ),
    )
    context = SimpleNamespace(
        card=revision.graph.card_by_id()["extract"],
        revision=revision,
    )
    prepared, plan = task._prepare_taskboard_task_workspace_artifact_delivery(
        {
            "artifact_markdown": "# Source Summary\n\nIntermediate evidence only.",
            "artifact_manifest": {"path": "final.md"},
        },
        context,
        deliverable_mode="task_workspace_artifact",
    )

    delivered = await task._deliver_task_workspace_artifact(
        prepared,
        plan=plan,
        execution_meta={"logs": {}},
        source="test.taskboard.intermediate_uncontracted.task_workspace_artifact",
        card_context=context,
    )

    assert task._task_workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == (
        "working/taskboard/extract/final.md"
    )
    assert task.task_workspace.resolve_file_path(delivered["file_refs"][0]["path"]).is_file()
    assert not (task.task_workspace.root / "final.md").exists()


@pytest.mark.asyncio
async def test_taskboard_remaining_work_leaf_card_relocates_required_final_deliverable_path(tmp_path):
    agent = _create_agent("execution-taskboard-leaf-remaining-work-final-path").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Use evidence before the final deliverable.",
        success_criteria=["Intermediate continuation artifacts do not occupy final.md."],
        execution="taskboard",
        max_iterations=None,
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    revision = TaskBoardRevision.create(
        board_id="leaf-remaining-work-final-path",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "leaf-remaining-work-final-path-graph",
                "cards": [
                    {
                        "id": "continue-evidence",
                        "objective": "Continue evidence gathering before final synthesis.",
                    }
                ],
            }
        ),
    )
    context = SimpleNamespace(
        card=revision.graph.card_by_id()["continue-evidence"],
        revision=revision,
    )
    prepared, plan = task._prepare_taskboard_task_workspace_artifact_delivery(
        {
            "artifact_markdown": "# Evidence Continuation\n\nThis is not the final deliverable.",
            "artifact_manifest": {"path": "final.md"},
            "remaining_work": ["Use this evidence in the final synthesis card."],
        },
        context,
        deliverable_mode="task_workspace_artifact",
    )

    delivered = await task._deliver_task_workspace_artifact(
        prepared,
        plan=plan,
        execution_meta={"logs": {}},
        source="test.taskboard.leaf_remaining_work.task_workspace_artifact",
        card_context=context,
    )

    assert task._task_workspace_artifact_display_path(delivered["file_refs"][0]["path"]) == (
        "working/taskboard/continue-evidence/final.md"
    )
    assert task.task_workspace.resolve_file_path(delivered["file_refs"][0]["path"]).is_file()
    assert not (task.task_workspace.root / "final.md").exists()
    assert any(
        item.get("code") == "taskboard.task_workspace_artifact.final_path_relocated_for_intermediate_card"
        and item.get("remaining_work_present") is True
        for item in delivered["diagnostics"]
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_taskboard_final_repair_card_stages_required_deliverable_candidate(
    tmp_path,
):
    agent = _create_agent("execution-taskboard-final-repair-final-path").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Repair and materialize the final deliverable.",
        success_criteria=["The final deliverable file exists."],
        execution="taskboard",
        max_iterations=None,
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    revision = TaskBoardRevision.create(
        board_id="final-repair-final-path",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "final-repair-final-path-graph",
                "cards": [
                    {
                        "id": "repair",
                        "objective": "Repair the final deliverable.",
                        "metadata": {"final_task_workspace_deliverables": ["final.md"]},
                    },
                    {
                        "id": "audit",
                        "objective": "Audit after repair.",
                        "depends_on": ["repair"],
                    },
                ],
            }
        ),
    )
    context = SimpleNamespace(
        card=revision.graph.card_by_id()["repair"],
        revision=revision,
    )
    prepared, plan = task._prepare_taskboard_task_workspace_artifact_delivery(
        {
            "artifact_markdown": "# Final Repair\n\nMaterialized at the required final path.",
            "artifact_manifest": {"path": "working/taskboard/repair/final.md"},
        },
        context,
        deliverable_mode="task_workspace_artifact",
    )

    delivered = await task._deliver_task_workspace_artifact(
        prepared,
        plan=plan,
        execution_meta={"logs": {}},
        source="test.taskboard.final_repair.task_workspace_artifact",
        card_context=context,
    )

    assert task._task_workspace_artifact_display_path(
        delivered["file_refs"][0]["path"]
    ) == "working/taskboard/repair/terminal-candidates/final.md"
    assert task.task_workspace.resolve_file_path(delivered["file_refs"][0]["path"]).is_file()
    assert not (task.task_workspace.root / "final.md").exists()
    assert not (task.task_workspace.root / "working/taskboard/repair/final.md").exists()
    assert any(
        item.get("code")
        == "taskboard.task_workspace_artifact.terminal_candidate_staged"
        for item in delivered["diagnostics"]
        if isinstance(item, dict)
    )


def test_execution_options_validate_known_route_schema():
    with pytest.raises(ValueError, match="routes.skills was removed"):
        ExecutionOptions.model_validate({"routes": {"skills": {"unknown": True}}})


def test_create_task_execution_parameter_normalizes_and_rejects(tmp_path):
    agent = _create_agent("execution-strategy-normalization").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Do the task.",
        success_criteria=["The task is done."],
        execution="flat_react",
    )
    assert execution.task_options["execution"] == "flat"
    assert "max_iterations" not in execution.task_options

    taskboard_execution = agent.create_task(
        goal="Do the board task.",
        success_criteria=["The task is done."],
        execution="task_board",
    )
    assert taskboard_execution.task_options["execution"] == "taskboard"
    assert "max_iterations" not in taskboard_execution.task_options

    explicit_limit_execution = agent.create_task(
        goal="Do the explicitly bounded task.",
        success_criteria=["The task is done."],
        execution="flat",
        max_iterations=2,
    )
    assert explicit_limit_execution.task_options["max_iterations"] == 2

    with pytest.raises(ValueError, match="execution must be one of"):
        agent.create_task(
            goal="Do the task.",
            success_criteria=["The task is done."],
            execution="unknown",  # pyright: ignore[reportArgumentType] - runtime rejection probe
        )


def test_guidance_delta_projects_readable_status_text():
    received = AgentExecutionStreamData(
        path="agent_task.guidance.received",
        value={"guidance_id": "guidance-1", "status": "received", "target": "task"},
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "guidance", "guidance_status": "received"},
    )
    applied = AgentExecutionStreamData(
        path="agent_task.guidance.applied",
        value={"guidance_ids": ["guidance-1"], "status": "applied", "boundary": "flat_context"},
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "guidance", "guidance_status": "applied"},
    )

    assert project_agent_execution_text_delta(received) == (
        "Guidance received; it will be applied at the next safe task boundary.\n\n"
    )
    assert project_agent_execution_text_delta(applied) == (
        "Guidance applied at the next safe task boundary.\n\n"
    )


@pytest.mark.asyncio
async def test_agent_execution_add_guidance_during_flat_task_does_not_wait_for_completion(tmp_path):
    agent = _create_agent("execution-live-guidance").use_task_workspace(tmp_path / "task_workspace")
    execution = agent.create_task(
        goal="Prepare the incident response summary.",
        success_criteria=["The final answer uses the operator's latest guidance."],
        execution="flat",
        max_iterations=2,
    )
    plan_started = asyncio.Event()
    release_first_plan = asyncio.Event()
    context_packs: list[dict[str, Any]] = []

    async def request_plan(iteration_index, context_pack):
        context_packs.append(dict(context_pack))
        if iteration_index == 1:
            plan_started.set()
            await release_first_plan.wait()
        return {
            "execution_shape": "direct",
            "step_instruction": f"Run bounded work iteration {iteration_index}.",
            "expected_evidence": "operator guidance is considered",
            "rationale": "The task should consume runtime guidance at a safe boundary.",
        }

    async def execute_step(iteration_index, _plan, _context_pack):
        remaining = ["Use the new operator guidance."] if iteration_index == 1 else []
        return (
            {
                "step_result": f"iteration {iteration_index} result",
                "evidence": [f"iteration {iteration_index} evidence"],
                "remaining_work": remaining,
                "ready_for_final_verification": iteration_index == 2,
            },
            {
                "execution_id": f"exec-guidance-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def request_verification(iteration_index, **_kwargs):
        if iteration_index == 1:
            return {
                "is_complete": False,
                "requires_block": False,
                "reason": "runtime guidance must be consumed by the next step",
                "missing_criteria": ["operator guidance not consumed yet"],
                "replan_instruction": "Use the latest runtime guidance.",
                "final_result": "",
            }
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "runtime guidance was considered",
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result": "Final answer that uses the runtime operator guidance.",
        }

    cast(Any, execution)._agent_task_step_overrides = {
        "_request_plan": request_plan,
        "_execute_step": execute_step,
        "_request_verification": request_verification,
    }

    run_task = asyncio.create_task(execution.async_get_full_data())
    await asyncio.wait_for(plan_started.wait(), timeout=3)

    guidance = await asyncio.wait_for(
        execution.async_add_guidance(
            "Use the operator's newly uploaded incident note as the primary context.",
            author="operator",
            target="task",
        ),
        timeout=1,
    )
    assert guidance["kind"] == "guidance"
    assert guidance["status"] == "received"
    assert not run_task.done()

    release_first_plan.set()
    result = await asyncio.wait_for(run_task, timeout=5)
    meta = await execution.async_get_meta()
    meta_view = cast(dict[str, Any], meta)
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert len(context_packs) == 2
    assert context_packs[1]["guidance"][0]["id"] == guidance["id"]
    assert "operator's newly uploaded incident note" in context_packs[1]["guidance"][0]["content_preview"]
    assert task_meta["guidance_items"][0]["status"] == "applied"
    assert task_meta["guidance_refs"] == task_meta["record_refs"]["guidance"]
    assert meta_view["record_refs"]["agent_task"]["guidance"] == task_meta["guidance_refs"]


@pytest.mark.asyncio
async def test_agent_execution_add_guidance_before_task_creation_is_queued_then_applied(tmp_path):
    agent = _create_agent("execution-queued-guidance").use_task_workspace(tmp_path / "task_workspace")
    execution = agent.create_task(
        goal="Prepare the queued guidance summary.",
        success_criteria=["The queued guidance is reflected in the final answer."],
        execution="flat",
        max_iterations=1,
    )
    context_packs: list[dict[str, Any]] = []

    async def request_plan(_iteration_index, context_pack):
        context_packs.append(dict(context_pack))
        return {
            "execution_shape": "direct",
            "step_instruction": "Use the queued guidance.",
            "expected_evidence": "queued guidance was available",
            "rationale": "Guidance arrived before the task record existed.",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {
                "step_result": "queued guidance used",
                "evidence": ["queued guidance was present"],
                "remaining_work": [],
                "ready_for_final_verification": True,
            },
            {
                "execution_id": "exec-queued-guidance",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def request_verification(_iteration_index, **_kwargs):
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "queued guidance was consumed",
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result": "Final answer with queued guidance.",
        }

    cast(Any, execution)._agent_task_step_overrides = {
        "_request_plan": request_plan,
        "_execute_step": execute_step,
        "_request_verification": request_verification,
    }

    guidance = await execution.async_add_guidance(
        "Use the queued operator context before planning the first step.",
        author="operator",
    )
    assert guidance["status"] == "queued"

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert context_packs[0]["guidance"][0]["id"] == guidance["id"]
    assert task_meta["guidance_items"][0]["status"] == "applied"
    assert task_meta["guidance_items"][0]["storage"] == "memory"
    assert "task_workspace_ref" not in task_meta["guidance_items"][0]


@pytest.mark.asyncio
async def test_agent_task_does_not_call_legacy_skills_context_pack_builder(tmp_path, monkeypatch):
    agent = _create_agent("execution-required-skill-context-pack").use_task_workspace(tmp_path / "task_workspace")
    calls: list[dict[str, Any]] = []

    async def fake_build_skills_context_pack(**kwargs: Any) -> dict[str, Any]:
        calls.append(DataFormatter.sanitize(kwargs))
        return {
            "schema_version": "agently.skills.context_pack.v1",
            "task": kwargs.get("task", ""),
            "used_chars": 128,
            "skills": [
                {
                    "skill_id": "agently-request",
                    "source": {"installed_path": "/tmp/registry/agently-request"},
                    "guidance": {
                        "path": "SKILL.md",
                        "excerpt": "Use Agently request output control.",
                        "citation": "skills/agently-request/SKILL.md",
                    },
                    "selected_resources": [],
                }
            ],
            "citations": ["skills/agently-request/SKILL.md"],
            "diagnostics": [],
        }

    monkeypatch.setattr(agent, "async_build_skills_context_pack", fake_build_skills_context_pack, raising=False)
    task = AgentTask(
        agent,
        goal="Build a structured Agently request.",
        success_criteria=["The answer follows Agently request guidance."],
        execution="flat",
        options={"capability_constraints": {"skills": {"required": ["agently-request"]}}},
    )

    context_pack = await task._build_context()

    assert calls == []
    assert context_pack["skill_projection"]["skills"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["flat", "taskboard"])
async def test_agent_task_context_prepare_blocks_when_required_skill_guidance_is_empty(
    tmp_path,
    monkeypatch,
    execution,
):
    agent = _create_agent(f"execution-required-skill-context-blocked-{execution}").use_task_workspace(tmp_path / execution)

    async def fake_build_skills_context_pack(**kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": "agently.skills.context_pack.v1",
            "task": kwargs.get("task", ""),
            "usable": False,
            "used_chars": 0,
            "skills": [
                {
                    "skill_id": "agently-request",
                    "mode": "required",
                    "selected_resources": [],
                    "allocation": {
                        "guidance_chars": 0,
                        "resource_chars": 0,
                    },
                }
            ],
            "citations": [],
            "diagnostics": [
                {
                    "level": "error",
                    "code": "skill_context_pack.required_guidance_empty",
                    "skill_id": "agently-request",
                }
            ],
        }

    monkeypatch.setattr(
        agent,
        "async_build_skills_context_pack",
        fake_build_skills_context_pack,
        raising=False,
    )
    task = AgentTask(
        agent,
        goal="Build a structured Agently request.",
        success_criteria=["The answer follows Agently request guidance."],
        execution=execution,
        options={"capability_constraints": {"skills": {"required": ["agently-request"]}}},
    )

    if execution == "flat":
        frame = await task._flat_context_prepare_stage({"iteration": 1})
    else:
        frame = await task._taskboard_context_prepare_stage({"iteration": 1})

    result = cast(dict[str, Any], frame.get("iteration_result"))
    assert result["terminal"] is True
    assert result["status"] == "blocked"
    assert result["reason_code"] == "required_skill_context_unavailable"
    assert result["missing_criteria"] == ["required_skill_context:agently-request"]
    assert task.status == "blocked"


@pytest.mark.asyncio
async def test_agent_task_context_prepare_ignores_legacy_skill_pack_builder(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("execution-required-skill-context-builder-failed").use_task_workspace(tmp_path / "task_workspace")

    async def fail_build_skills_context_pack(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated context build failure")

    monkeypatch.setattr(
        agent,
        "async_build_skills_context_pack",
        fail_build_skills_context_pack,
        raising=False,
    )
    task = AgentTask(
        agent,
        goal="Build a structured Agently request.",
        success_criteria=["The answer follows Agently request guidance."],
        execution="flat",
        options={"capability_constraints": {"skills": {"required": ["agently-request"]}}},
    )

    frame = await task._flat_context_prepare_stage({"iteration": 1})

    result = cast(dict[str, Any], frame.get("iteration_result"))
    assert result["terminal"] is True
    assert result["status"] == "blocked"
    assert task.result["required_skill_context"]["missing_skill_ids"] == [
        "agently-request"
    ]


def test_flat_context_pack_availability_needs_response_bound_skill_consumption(tmp_path):
    agent = _create_agent("execution-flat-required-skill-binding").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Build a structured Agently request.",
        success_criteria=["The answer follows Agently request guidance."],
        execution="flat",
        options={"capability_constraints": {"skills": {"required": ["agently-request"]}}},
    )
    context_pack: dict[str, Any] = {
        "package_id": "context-package-1",
        "goal": task.goal,
        "items": [],
        "profile": "test",
        "omitted": [],
        "diagnostics": {},
        "skill_projection": {
            "schema_version": "agently.context_package.skill_projection.v2",
            "skills": [
                {
                    "skill_id": "agently-request",
                    "binding_id": "skill-binding-1",
                    "revision_ref": "skill:agently-request@sha256:abc",
                }
            ],
        },
    }
    execution_meta = {
        "status": "success",
        "logs": {},
        "effective_options": {
            "capability_constraints": {"skills": {"required": ["agently-request"]}},
        },
    }

    enriched_meta = task._flat_execution_meta_with_skill_context_consumptions(
        execution_meta,
        context_pack=context_pack,
    )
    summary = task._execution_log_summary(enriched_meta)

    assert summary["consumed_skill_ids"] == []
    assert summary["missing_required_skill_context"] == ["agently-request"]
    assert "agently-request" not in summary["capabilities_used"]
    assert summary["context_evidence"]["skills"]["consumed"] == []
    assert summary["capability_evidence_requirements"] == []

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "done",
            "missing_criteria": [],
            "final_result": "done",
        },
        execution_evidence_summary=summary,
        candidate_final_result="done",
    )

    assert verification["is_complete"] is False
    assert verification["missing_required_capabilities"] == ["agently-request"]

    execution_meta["logs"] = {"model_response_ids": ["response-1"]}
    consumed_meta = task._flat_execution_meta_with_skill_context_consumptions(
        execution_meta,
        context_pack=context_pack,
    )
    consumed_summary = task._execution_log_summary(consumed_meta)

    assert consumed_summary["consumed_skill_ids"] == ["agently-request"]
    assert consumed_summary["missing_required_skill_context"] == []
    assert consumed_summary["context_evidence"]["skills"]["consumptions"] == [
        {
            "skill_id": "agently-request",
            "binding_id": "skill-binding-1",
            "revision_ref": "skill:agently-request@sha256:abc",
            "package_id": "context-package-1",
            "request_id": "response-1",
            "phase": "work.execute",
        }
    ]


@pytest.mark.asyncio
async def test_agent_execution_guidance_on_non_task_route_is_not_applied_or_prompt_injected():
    agent = _create_agent("execution-guidance-non-task")
    execution = (
        agent.input("Return a compact answer.")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(limits={"max_model_requests": 1})
    )

    guidance = await execution.async_add_guidance("Do not inject this into a non-task prompt.")
    result = await execution.async_get_data()
    meta = await execution.async_get_meta()
    meta_view = cast(dict[str, Any], meta)
    request_text = "\n".join(MockAgentExecutionRequester.requests)

    assert result["answer"]
    assert guidance["status"] == "queued"
    assert meta_view["guidance_items"][0]["status"] == "not_applied"
    assert meta_view["guidance_items"][0]["not_applied_reason"] == "route:model_request:not_agent_task"
    assert "Do not inject this into a non-task prompt." not in request_text


def test_taskboard_final_normalization_preserves_complete_task_workspace_candidate():
    candidate = "# Complete Deliverable\n\n" + ("complete section body\n" * 120)
    final = {
        "accepted": True,
        "reason": "The deliverable is complete.",
        "final_result": "The complete deliverable has been written to final.md.",
        "missing_criteria": [],
    }

    normalized = AgentTask._normalize_taskboard_final_result(final, candidate)

    assert normalized["final_result"] == candidate.strip()


def test_taskboard_final_normalization_prefers_task_workspace_ref_fallback_for_file_deliverable():
    candidate = "# Complete File Deliverable\n\n" + ("file body section\n" * 120)
    final = {
        "accepted": True,
        "reason": "Trusted TaskWorkspace artifact refs identify the deliverable.",
        "missing_criteria": [],
    }

    normalized = AgentTask._normalize_taskboard_final_result(
        final,
        candidate,
        fallback_final_result=(
            "TaskWorkspace artifact delivered at final.md; full content is available through file_refs/readback."
        ),
    )

    assert normalized["final_result"] == (
        "TaskWorkspace artifact delivered at final.md; full content is available through file_refs/readback."
    )
    assert "file body section" not in normalized["final_result"]


def test_taskboard_final_response_explains_partial_artifact_without_acceptance():
    final_response = AgentTask._taskboard_user_final_response(
        final={
            "final_result": "TaskWorkspace artifact delivered at final.md; full content is available through file_refs/readback.",
        },
        accepted=False,
        artifact_status="partial",
        reason="Official PDF source was unavailable, so source-grounded claims remain weak.",
        missing_criteria=["Ground every official-source claim in verifier-visible evidence."],
        final_refs=[{"path": "final.md", "role": "task_workspace_artifact"}],
        board_status="completed",
        degraded_finalization_attempted=False,
    )

    assert "Partial result available" in final_response
    assert "final.md" in final_response
    assert "Official PDF source was unavailable" in final_response
    assert "Ground every official-source claim" in final_response


def test_taskboard_final_response_replaces_overstrong_model_response_with_host_projection():
    final_response = AgentTask._taskboard_user_final_response(
        final={"final_response": "Completed. All success criteria are satisfied."},
        accepted=True,
        artifact_status="degraded",
        reason="Verifier accepted under disclosed source limits.",
        missing_criteria=[],
        final_refs=[{"path": "final.md", "role": "task_workspace_artifact"}],
        board_status="completed",
        degraded_finalization_attempted=False,
        completion_notes={
            "authority": "projection_only",
            "known_limits": ["Official PDF content was not read; only the download link was available."],
            "quality_notes": ["Final artifact disclosed the source limitation."],
        },
    )

    assert "All success criteria are satisfied" not in final_response
    assert "Completed with disclosed limitations" in final_response
    assert "Deliverable artifact: final.md" in final_response
    assert "Official PDF content was not read" in final_response


def test_agent_task_final_response_ignores_overstrong_unaccepted_model_response():
    final_response = AgentTask._agent_task_user_final_response(
        final={"final_response": "Completed. All success criteria are satisfied."},
        accepted=False,
        artifact_status="partial",
        reason="Official source evidence is still missing.",
        missing_criteria=["Ground every official-source claim."],
        final_refs=[{"path": "final.md", "role": "task_workspace_artifact"}],
    )

    assert "All success criteria are satisfied" not in final_response
    assert "Partial result available" in final_response
    assert "final.md" in final_response
    assert "Official source evidence is still missing" in final_response
    assert "Ground every official-source claim" in final_response


def test_taskboard_final_response_omits_process_notes_for_plain_acceptance():
    final_response = AgentTask._taskboard_user_final_response(
        final={"final_response": "Completed. Deliverable artifact: final.md."},
        accepted=True,
        artifact_status="accepted",
        reason="All criteria are satisfied.",
        missing_criteria=[],
        final_refs=[{"path": "final.md"}],
        board_status="completed",
        degraded_finalization_attempted=False,
        completion_notes={
            "known_limits": [],
            "quality_notes": ["Earlier repair note was resolved."],
            "process_notes": ["Readback happened before final verification."],
        },
    )

    assert final_response == (
        "Completed. Deliverable artifact: final.md. Summary: All criteria are satisfied."
    )
    assert "Process notes" not in final_response
    assert "Earlier repair note" not in final_response


def test_taskboard_completion_notes_do_not_degrade_resolved_repair_history():
    revision = TaskBoardRevision.create(
        board_id="resolved-repair-history",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "resolved-repair-history-graph",
                "cards": [
                    {"id": "draft", "objective": "Write the report."},
                    {"id": "final-verification-repair", "objective": "Repair verifier evidence."},
                    {"id": "final-verification-repair-2", "objective": "Repair final artifact path."},
                    {"id": "final-verification-repair.readback", "objective": "Read back the artifact."},
                    {"id": "final-verification-repair.continue", "objective": "Continue after readback."},
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": "rev-0",
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": {"final_result": "Report body."},
                    },
                },
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final-verification-repair",
                        "status": "setback",
                        "preview": {
                            "remaining_work": ["Need artifact readback before final verification."],
                            "self_check": "Readback is needed, not a final quality failure.",
                        },
                    },
                },
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final-verification-repair.readback",
                        "status": "completed",
                        "preview": {"short_summary": "Artifact readback completed."},
                    },
                },
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final-verification-repair-2",
                        "status": "completed",
                        "preview": {
                            "remaining_work": ["Write the confirmed content to the required artifact path."],
                            "self_check": "The content is verified; only materialization was pending.",
                        },
                    },
                },
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final-verification-repair.continue",
                        "status": "completed",
                        "preview": {"short_summary": "Verifier evidence repaired."},
                    },
                },
            ],
        },
    )
    notes = AgentTask._taskboard_completion_notes(
        revision,
        final={"accepted": True, "reason": "All criteria are satisfied."},
        final_verification={
            "is_complete": True,
            "reason": "All criteria are satisfied.",
            "failure_analysis": "All criteria are satisfied.",
            "acceptance_delta": ["All criteria are satisfied after readback."],
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion": "The report is delivered.",
                    "satisfied": True,
                    "summary": "The artifact readback confirms delivery.",
                }
            ],
        },
        acceptance_verification_plan={
            "all_satisfied": True,
            "status_counts": {"satisfied": 1},
            "metadata": {"dirty_count": 0, "green_count": 1, "total_items": 1},
        },
    )

    assert not notes["known_limits"]
    assert any("resolved earlier setback" in note for note in notes["process_notes"])
    assert any("Write the confirmed content" in note for note in notes["process_notes"])
    assert any("All criteria are satisfied." in note for note in notes["quality_notes"])
    assert any("All criteria are satisfied after readback" in note for note in notes["quality_notes"])
    assert (
        AgentTask._taskboard_final_is_degraded(
            {"accepted": True},
            board_status="completed",
            degraded_finalization_attempted=False,
            completion_notes=notes,
        )
        is False
    )


@pytest.mark.asyncio
async def test_taskboard_finalizer_rejection_stops_without_redundant_terminal_verifier(tmp_path, monkeypatch):
    agent = _create_agent("execution-taskboard-finalizer-rejection-verifier").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Produce a final answer.",
        success_criteria=["The final answer is verified."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.create(
        board_id="finalizer-rejection",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "finalizer-rejection-graph",
                "cards": [{"id": "final", "objective": "Produce the final answer."}],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": "rev-0",
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final",
                        "status": "completed",
                        "preview": {"answer": "Complete candidate final answer."},
                    },
                }
            ],
        },
    )
    verification_calls: list[dict[str, Any]] = []

    async def fake_taskboard_final(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "accepted": False,
            "reason": "Finalizer could not bind evidence.",
            "final_result": "",
            "missing_criteria": ["Terminal verifier must inspect candidate evidence."],
        }

    async def fake_request_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        verification_calls.append(DataFormatter.sanitize(kwargs))
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Terminal verifier accepted the candidate.",
            "final_result": "Verified final answer.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion": "The final answer is verified.",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "Verifier accepted the terminal candidate.",
                }
            ],
        }

    async def no_missing_deliverables() -> list[dict[str, Any]]:
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_promote_taskboard_final_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(task, "_request_taskboard_final", fake_taskboard_final)
    monkeypatch.setattr(task, "_request_verification", fake_request_verification)
    monkeypatch.setattr(task, "_missing_required_task_workspace_deliverables", no_missing_deliverables)
    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_emit", noop)

    terminal = await task._finalize_taskboard(completed_revision, context_pack=cast(dict[str, Any], {}))

    assert verification_calls == []
    assert terminal == {"terminal": True, "status": "blocked"}
    assert task.result["status"] == "blocked"
    assert task.result["accepted"] is False
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    assert terminal_state["final_verification"] is None
    assert terminal_state["taskboard_acceptance_index"]["metadata"]["green_count"] == 0
    assert terminal_state["acceptance_verification_plan"]["all_satisfied"] is False
    assert "taskboard" not in task.result


@pytest.mark.asyncio
async def test_taskboard_finalization_requires_required_skill_context_evidence(tmp_path, monkeypatch):
    agent = _create_agent("execution-taskboard-required-skill-missing").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Produce an Agently request checklist.",
        success_criteria=["The checklist uses required Agently request guidance."],
        execution="taskboard",
        options={
            "capability_constraints": {"skills": {"required": ["agently-request"]}},
            "planner_capabilities": [
                {
                    "id": "agently-request",
                    "kind": "skill",
                    "route": "model_request",
                    "guidance_access": "context_pack",
                    "mode": "required",
                }
            ],
        },
    )
    revision = TaskBoardRevision.create(
        board_id="required-skill-missing",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "required-skill-missing-graph",
                "cards": [{"id": "final", "objective": "Produce the checklist."}],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": "rev-0",
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final",
                        "status": "completed",
                        "preview": {"final_result": "Checklist without Skill context evidence."},
                    },
                }
            ],
        },
    )

    async def fake_taskboard_final(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "accepted": True,
            "reason": "Finalizer accepted the candidate.",
            "final_result": "Checklist without Skill context evidence.",
            "missing_criteria": [],
        }

    async def fake_request_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Verifier accepted the candidate.",
            "final_result": "Checklist without Skill context evidence.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion": "The checklist uses required Agently request guidance.",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "The candidate looks complete.",
                }
            ],
        }

    async def no_missing_deliverables() -> list[dict[str, Any]]:
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_promote_taskboard_final_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(task, "_request_taskboard_final", fake_taskboard_final)
    monkeypatch.setattr(task, "_request_verification", fake_request_verification)
    monkeypatch.setattr(task, "_missing_required_task_workspace_deliverables", no_missing_deliverables)
    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_emit", noop)

    terminal = await task._finalize_taskboard(completed_revision, context_pack=cast(dict[str, Any], {}))

    assert terminal["terminal"] is False
    assert terminal["status"] == "repair_requested"
    assert "agently-request" in terminal["final_verification"]["missing_required_capabilities"]
    assert any(
        "agently-request" in item
        for item in terminal["final_verification"]["missing_criteria"]
    )


@pytest.mark.asyncio
async def test_taskboard_finalization_accepts_library_bound_required_skill_context(tmp_path, monkeypatch):
    agent = _create_agent("execution-taskboard-required-skill-loaded").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Produce an Agently request checklist.",
        success_criteria=["The checklist uses required Agently request guidance."],
        execution="taskboard",
        options={
            "capability_constraints": {"skills": {"required": ["agently-request"]}},
            "planner_capabilities": [
                {
                    "id": "agently-request",
                    "kind": "skill",
                    "route": "model_request",
                    "guidance_access": "context_pack",
                    "mode": "required",
                }
            ],
        },
    )
    revision = TaskBoardRevision.create(
        board_id="required-skill-loaded",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "required-skill-loaded-graph",
                "cards": [{"id": "final", "objective": "Produce the checklist."}],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": "rev-0",
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final",
                        "status": "completed",
                        "preview": {"final_result": "Checklist grounded in agently-request guidance."},
                        "diagnostics": [
                            {
                                "evidence_summary": {
                                    "consumed_skill_ids": ["agently-request"],
                                    "context_evidence": {
                                        "skills": {
                                            "consumed": ["agently-request"],
                                            "consumptions": [
                                                {
                                                    "skill_id": "agently-request",
                                                    "binding_id": "skill-binding-1",
                                                    "revision_ref": "skill:agently-request@sha256:abc",
                                                    "package_id": "context-package-1",
                                                    "request_id": "response-1",
                                                    "phase": "work.execute.taskboard",
                                                }
                                            ],
                                        }
                                    },
                                }
                            }
                        ],
                    },
                }
            ],
        },
    )
    verification_calls: list[dict[str, Any]] = []

    async def fake_taskboard_final(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "accepted": True,
            "reason": "Finalizer accepted the candidate.",
            "final_result": "Checklist grounded in agently-request guidance.",
            "missing_criteria": [],
        }

    async def fake_request_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        verification_calls.append(DataFormatter.sanitize(kwargs))
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Verifier accepted the candidate.",
            "final_result": "Checklist grounded in agently-request guidance.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion": "The checklist uses required Agently request guidance.",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "The required Skill context is visible.",
                }
            ],
        }

    async def no_missing_deliverables() -> list[dict[str, Any]]:
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_promote_taskboard_final_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(task, "_request_taskboard_final", fake_taskboard_final)
    monkeypatch.setattr(task, "_request_verification", fake_request_verification)
    monkeypatch.setattr(task, "_missing_required_task_workspace_deliverables", no_missing_deliverables)
    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_emit", noop)

    terminal = await task._finalize_taskboard(
        completed_revision,
        context_pack=cast(
            dict[str, Any],
            {
                "skill_projection": {
                    "schema_version": "agently.skills.context_pack.v1",
                    "skills": [{"skill_id": "agently-request"}],
                }
            },
        ),
    )

    assert terminal == {"terminal": True, "status": "completed"}
    assert task.result["accepted"] is True
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    assert terminal_state["final_verification"]["missing_required_capabilities"] == []
    consumptions = verification_calls[0]["execution_meta"]["logs"]["skill_context_consumptions"]
    assert consumptions == [
        {
            "skill_id": "agently-request",
            "binding_id": "skill-binding-1",
            "revision_ref": "skill:agently-request@sha256:abc",
            "package_id": "context-package-1",
            "request_id": "response-1",
            "phase": "work.execute.taskboard",
        }
    ]


@pytest.mark.asyncio
async def test_taskboard_degraded_final_success_is_not_plain_accepted(tmp_path, monkeypatch):
    agent = _create_agent("execution-taskboard-degraded-final-success").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Produce the best available source-grounded brief.",
        success_criteria=["Deliver a usable brief and disclose unavailable optional sources."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.create(
        board_id="degraded-final-success",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "degraded-final-success-graph",
                "cards": [
                    {"id": "draft", "objective": "Write the usable brief."},
                    {"id": "optional_audit", "objective": "Try optional source audit.", "depends_on": ["draft"]},
                ],
            }
        ),
    )
    degraded_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": "rev-0",
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": {"candidate_final_result": "Usable brief with disclosed source limits."},
                    },
                },
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "optional_audit",
                        "status": "blocked",
                        "preview": {"reason": "Optional source audit was unavailable."},
                    },
                },
            ],
        },
    )
    finalizer_seen: dict[str, Any] = {}

    async def fake_taskboard_final(*args: Any, **kwargs: Any) -> dict[str, Any]:
        finalizer_seen["allow_degraded_final"] = kwargs.get("allow_degraded_final")
        return {
            "accepted": True,
            "degraded": True,
            "degradation_reason": "The optional source audit was unavailable and disclosed.",
            "reason": "The usable brief satisfies the goal under disclosed source limits.",
            "final_result": "Usable brief with disclosed source limits.",
            "final_response": "Completed with disclosed degradation: optional source audit unavailable.",
            "missing_criteria": [],
        }

    async def fake_request_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Accepted under disclosed degradation.",
            "final_result": "Usable brief with disclosed source limits.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion": "Deliver a usable brief and disclose unavailable optional sources.",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "The brief discloses the unavailable audit.",
                }
            ],
        }

    async def no_missing_deliverables() -> list[dict[str, Any]]:
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_promote_taskboard_final_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(task, "_request_taskboard_final", fake_taskboard_final)
    monkeypatch.setattr(task, "_request_verification", fake_request_verification)
    monkeypatch.setattr(task, "_missing_required_task_workspace_deliverables", no_missing_deliverables)
    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_emit", noop)

    terminal = await task._finalize_taskboard(degraded_revision, context_pack=cast(dict[str, Any], {}))

    assert terminal == {"terminal": True, "status": "completed"}
    assert finalizer_seen["allow_degraded_final"] is True
    assert task.result["accepted"] is True
    assert task.result["artifact_status"] == "degraded"
    assert "degraded" not in task.result
    assert "optional source audit unavailable" in task.result["final_response"]
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    assert terminal_state["degraded_finalization_attempted"] is True


@pytest.mark.asyncio
async def test_taskboard_completion_notes_disclose_card_gaps_in_final_response(tmp_path, monkeypatch):
    agent = _create_agent("execution-taskboard-completion-notes-final-response").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Produce the best available source-grounded report.",
        success_criteria=["Deliver a usable report and disclose source limits."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.create(
        board_id="completion-notes-final-response",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "completion-notes-final-response-graph",
                "cards": [{"id": "draft", "objective": "Write the usable report."}],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": "rev-0",
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "draft",
                        "status": "completed",
                        "preview": {
                            "final_result": "Usable report with source boundary disclosed.",
                            "short_summary": "A usable report was produced.",
                            "gaps": [
                                "Official PDF content was not read; only the download link was available."
                            ],
                        },
                    },
                }
            ],
        },
    )

    async def fake_taskboard_final(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "accepted": True,
            "reason": "All requested content was delivered.",
            "final_result": "Usable report with source boundary disclosed.",
            "final_response": "Completed. All success criteria are satisfied.",
            "missing_criteria": [],
        }

    async def fake_request_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Verifier accepted the report because the source limitation is disclosed.",
            "final_result": "Usable report with source boundary disclosed.",
            "missing_criteria": [],
            "criterion_checks": [
                {
                    "criterion": "Deliver a usable report and disclose source limits.",
                    "satisfied": True,
                    "status": "satisfied",
                    "summary": "The report discloses that the official PDF content was not read.",
                }
            ],
        }

    async def no_missing_deliverables() -> list[dict[str, Any]]:
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_promote_taskboard_final_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(task, "_request_taskboard_final", fake_taskboard_final)
    monkeypatch.setattr(task, "_request_verification", fake_request_verification)
    monkeypatch.setattr(task, "_missing_required_task_workspace_deliverables", no_missing_deliverables)
    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_emit", noop)

    terminal = await task._finalize_taskboard(completed_revision, context_pack=cast(dict[str, Any], {}))

    assert terminal == {"terminal": True, "status": "completed"}
    assert task.result["accepted"] is True
    assert task.result["artifact_status"] == "degraded"
    assert "degraded" not in task.result
    assert "All success criteria are satisfied" in task.result["final_response"]
    assert "Official PDF content was not read" in task.result["final_response"]
    terminal_state = cast(dict[str, Any], task._terminal_taskboard_state)
    notes = terminal_state["completion_notes"]
    assert notes["authority"] == "projection_only"
    assert notes["metadata"]["not_evidence"] is True
    assert any("Official PDF content was not read" in note for note in notes["known_limits"])
    assert not any(
        item.get("schema_version") == "task_board_completion_notes/v1"
        for item in terminal_state["evidence_view"].get("evidence_items", [])
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_taskboard_finalization_materializes_final_artifact_evidence_before_verifier(
    tmp_path,
    monkeypatch,
):
    agent = _create_agent("execution-taskboard-final-artifact-verifier-evidence").use_task_workspace(
        tmp_path / "task_workspace",
        mode="read_write",
    )
    task = AgentTask(
        agent,
        goal="Produce a final TaskWorkspace report.",
        success_criteria=["The final report contains the verifier-visible required evidence section."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    await task.task_workspace.write_file(
        "final.md",
        "# Final Report\n\n## Required Evidence Section\n\nThe final artifact has verifier-visible evidence.",
    )
    revision = TaskBoardRevision.create(
        board_id="final-artifact-verifier-evidence",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "final-artifact-verifier-evidence-graph",
                "cards": [{"id": "final", "objective": "Write final.md."}],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "final",
                        "status": "completed",
                        "preview": {
                            "artifact_manifest": {"path": "final.md"},
                            "acceptance_points": [
                                {
                                    "criterion": "Required evidence section is present.",
                                    "expected_anchor": "Required Evidence Section",
                                    "artifact_path": "final.md",
                                }
                            ],
                            "remaining_work": [],
                        },
                        "artifact_refs": [{"path": "final.md", "role": "task_workspace_artifact"}],
                    },
                }
            ],
        },
    )
    verification_calls: list[dict[str, Any]] = []

    async def fake_request_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        verification_calls.append(DataFormatter.sanitize(kwargs))
        evidence_items = kwargs["execution_meta"]["blocks"]["evidence"]["evidence_items"]
        assert any(item.get("kind") == "task_workspace_artifact.readback" for item in evidence_items)
        assert any(item.get("kind") == "task_workspace_artifact.acceptance_locator" for item in evidence_items)
        assert any("final artifact has verifier-visible evidence" in str(item.get("body") or "") for item in evidence_items)
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Final artifact evidence is verifier-visible.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": "",
        }

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_request_verification", fake_request_verification)
    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_emit", noop)

    terminal = await task._finalize_taskboard(completed_revision, context_pack=cast(dict[str, Any], {}))

    assert verification_calls
    assert terminal == {"terminal": True, "status": "completed"}
    assert task.result["accepted"] is True
    assert "final.md" in task.result["final_result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_mode", ["repeated_valid", "unknown"])
async def test_taskboard_finalization_promotes_working_artifact_to_required_deliverable(
    tmp_path,
    monkeypatch,
    reference_mode,
):
    agent = _create_agent("execution-taskboard-final-artifact-promotion").use_task_workspace(
        tmp_path / "task_workspace",
        mode="read_only",
    )
    task = AgentTask(
        agent,
        goal="Produce a final TaskWorkspace report.",
        success_criteria=["The final report contains a verifier-visible required evidence section."],
        execution="taskboard",
        options={
            "agent_task": {
                "output_contract": {
                    "deliverables": [{"path": "final.md", "media_type": "text/markdown"}],
                    "sections": ["Required Evidence Section"],
                }
            }
        },
    )
    source_evidence = task._task_reference_catalog.add_evidence(
        {
            "id": "source.action.result",
            "kind": "agent_task.action.result",
            "action_call_id": "call-source",
            "action_id": "source.read",
            "status": "ok",
            "body_state": "bounded",
            "body": "The source supports the report.",
        }
    )
    source_token = (
        f"[[ref:{source_evidence['reference_id']}]]"
        if reference_mode == "repeated_valid"
        else "[[ref:ref_unknown]]"
    )
    source_path = "working/taskboard/synthesize/terminal-candidates/final.md"
    source_content = (
        "# Final Report\n\n"
        "## Required Evidence Section\n\n"
        f"The report is complete. {source_token}\n\n"
        + ("long body line\n" * 500)
        + f"\nRepeated source citation: {source_token}\n"
        + "TAIL-MARKER-PROMOTED-FROM-WORKING-ARTIFACT"
    )
    await task.task_workspace.write_file(source_path, source_content)
    revision = TaskBoardRevision.create(
        board_id="final-artifact-promotion",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "final-artifact-promotion-graph",
                "cards": [{"id": "synthesize", "objective": "Write the final report."}],
            }
        ),
    )
    completed_revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "synthesize",
                        "status": "completed",
                        "preview": {
                            "artifact_manifest": {"path": source_path},
                            "acceptance_points": [
                                {
                                    "criterion": "Required evidence section is present.",
                                    "expected_anchor": "Required Evidence Section",
                                    "artifact_path": "final.md",
                                }
                            ],
                            "remaining_work": [],
                        },
                        "artifact_refs": [{"path": source_path, "role": "task_workspace_artifact"}],
                    },
                }
            ],
        },
    )
    verification_calls: list[dict[str, Any]] = []

    async def fake_request_verification(*args: Any, **kwargs: Any) -> dict[str, Any]:
        verification_calls.append(DataFormatter.sanitize(kwargs))
        file_refs = kwargs["execution_result"]["file_refs"]
        assert task._task_workspace_artifact_display_path(file_refs[0]["path"]) == source_path
        assert task._task_workspace_artifact_display_path(
            file_refs[0]["staged_target_path"]
        ) == "final.md"
        evidence_items = kwargs["execution_meta"]["blocks"]["evidence"]["evidence_items"]
        assert any(
            item.get("kind") == "task_workspace_artifact.readback"
            and task._task_workspace_artifact_display_path(item.get("path"))
            == source_path
            for item in evidence_items
        )
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "Final artifact evidence is verifier-visible.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": "",
        }

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_request_verification", fake_request_verification)
    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_emit", noop)

    terminal = await task._finalize_taskboard(completed_revision, context_pack=cast(dict[str, Any], {}))

    assert verification_calls
    if reference_mode == "unknown":
        assert terminal["terminal"] is False
        assert terminal["status"] == "repair_requested"
        assert terminal["final_verification"]["is_complete"] is False
        assert "taskboard_terminal_reference_token_invalid" in terminal[
            "final_verification"
        ]["guard_reasons"]
        assert not task.task_workspace.resolve_file_path("final.md").exists()
        return
    final_target = task.task_workspace.resolve_file_path("final.md")
    readback = await task.task_workspace.read_file("final.md", max_bytes=int(final_target.stat().st_size) + 1)
    assert terminal == {"terminal": True, "status": "completed"}
    assert task.result["accepted"] is True
    assert len(task.result["artifact_refs"]) == 1
    assert task._task_workspace_artifact_display_path(
        task.result["artifact_refs"][0]["path"]
    ) == "final.md"
    assert task.result["artifact_refs"][0]["sha256"] == readback["sha256"]
    assert task.result["artifact_refs"][0]["bytes"] == len(source_content.encode("utf-8"))
    assert readback["content"] == source_content
    assert "TAIL-MARKER-PROMOTED-FROM-WORKING-ARTIFACT" in str(readback["content"])


@pytest.mark.asyncio
async def test_taskboard_final_artifact_evidence_supports_targeted_readback(tmp_path):
    from agently.builtins.plugins.AgentExecution.long_task.EvidenceLedger import evidence_ledger_view

    agent = _create_agent("execution-taskboard-final-targeted-readback").use_task_workspace(
        tmp_path / "task_workspace",
        mode="read_write",
    )
    task = AgentTask(
        agent,
        goal="Produce a sectioned final TaskWorkspace report.",
        success_criteria=["The final report includes a required section."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    await task.task_workspace.write_file(
        "final.md",
        "# Final Report\n\n## Required Evidence Section\n\nTARGETED-READBACK-MARKER proves scoped verification.",
    )

    evidence_items = await task._taskboard_final_artifact_verification_evidence_items(
        [{"path": "final.md", "role": "task_workspace_artifact"}],
        final={
            "accepted": True,
            "acceptance_points": [
                {
                    "criterion": "Required evidence section is present.",
                    "expected_anchor": "Required Evidence Section",
                    "artifact_path": "final.md",
                }
            ],
        },
    )
    execution_meta = {"blocks": {"evidence": {"evidence_items": evidence_items}}}
    ledger = evidence_ledger_view({"evidence_items": evidence_items}, max_items=120, body_chars=2400)

    await task._ensure_task_workspace_artifact_targeted_readback_evidence(
        execution_meta,
        ledger,
        evidence_use=[],
    )

    final_ledger = task._evidence_ledger_from_execution_meta(execution_meta)
    targeted = [
        item for item in final_ledger["items"] if item.get("kind") == "task_workspace_artifact.targeted_readback"
    ]
    assert targeted
    assert any("TARGETED-READBACK-MARKER" in str(item.get("body") or "") for item in targeted)


def test_taskboard_final_artifact_evidence_survives_taskboard_view_cap():
    from agently.builtins.plugins.AgentExecution.long_task.EvidenceLedger import evidence_ledger_view

    existing_items = [
        {
            "id": f"action-evidence-{index}",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "body": f"previous evidence {index}",
        }
        for index in range(140)
    ]
    final_item = {
        "id": "task_workspace_artifact_readback:agent_task.taskboard.final:final.md",
        "kind": "task_workspace_artifact.readback",
        "status": "ok",
        "body_state": "bounded",
        "path": "final.md",
        "body": "# Final\n\nVerifier-visible final artifact body.",
    }

    evidence_view = AgentTask._taskboard_evidence_view_with_additional_items(
        {"evidence_items": existing_items},
        [final_item],
    )
    ledger = evidence_ledger_view(evidence_view, max_items=120, body_chars=2400)
    ids = {str(item.get("id") or "") for item in ledger["items"]}

    assert final_item["id"] in ids
    assert "action-evidence-139" not in ids


def test_verifier_evidence_ledger_prioritizes_task_workspace_artifact_readback_when_capped():
    generic_items = [
        {
            "id": f"generic-evidence-{index}",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "bounded",
            "body": f"generic evidence {index}",
        }
        for index in range(100)
    ]
    targeted_item = {
        "id": "task_workspace_artifact_targeted_readback:final.md:source-list",
        "kind": "task_workspace_artifact.targeted_readback",
        "status": "ok",
        "body_state": "bounded",
        "path": "final.md",
        "body": "## Source List\n\nVerifier-visible source list.",
    }
    readback_item = {
        "id": "task_workspace_artifact_readback:agent_task.taskboard.final:final.md",
        "kind": "task_workspace_artifact.readback",
        "status": "ok",
        "body_state": "truncated",
        "path": "final.md",
        "body": "# Final\n\nInitial bounded body.",
    }
    locator_item = {
        "id": "task_workspace_artifact_acceptance_locator:final.md:source-list",
        "kind": "task_workspace_artifact.acceptance_locator",
        "status": "ok",
        "body_state": "ref_only",
        "path": "final.md",
        "heading": "Source List",
    }

    ledger = AgentTask._evidence_ledger_from_execution_meta(
        {
            "blocks": {
                "evidence": {
                    "evidence_items": [
                        *generic_items,
                        readback_item,
                        locator_item,
                        targeted_item,
                    ]
                }
            }
        }
    )
    ids = [str(item.get("id") or "") for item in ledger["items"]]

    assert ids[:3] == [targeted_item["id"], readback_item["id"], locator_item["id"]]
    assert targeted_item["id"] in ids
    assert readback_item["id"] in ids
    assert locator_item["id"] in ids
    assert "generic-evidence-99" not in ids


@pytest.mark.asyncio
async def test_flat_execution_strategy_forces_linear_steps_and_keeps_replan_gate(tmp_path):
    agent = _create_flat_replan_agent("execution-flat-strategy").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Produce a checked answer.",
        success_criteria=["The answer is accepted by verifier."],
        execution="flat",
        max_iterations=2,
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "flat accepted result"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["execution_strategy"] == "flat"
    assert task_meta["execution_strategy"] == "flat"
    assert len(task_meta["iterations"]) == 2
    assert task_meta["iterations"][0]["plan"]["execution_shape"] == "dynamic_task"
    assert task_meta["iterations"][0]["plan"]["effective_execution_shape"] == "direct"
    assert task_meta["iterations"][0]["plan"]["step_execution"]["policy"]["execution_strategy"] == "flat"
    assert task_meta["iterations"][0]["plan"]["step_execution"]["policy"]["allow_dag_steps"] is False
    assert task_meta["iterations"][0]["verification"]["is_complete"] is False
    assert task_meta["iterations"][1]["verification"]["is_complete"] is True


@pytest.mark.asyncio
async def test_flat_strategy_does_not_receive_taskboard_harness_state(tmp_path):
    agent = _create_flat_replan_agent("execution-flat-no-taskboard-harness").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Produce a checked answer.",
        success_criteria=["The answer is accepted by verifier."],
        execution="flat",
        max_iterations=2,
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    request_text = "\n".join(MockAgentExecutionRequester.requests)
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert task_meta["execution_strategy"] == "flat"
    assert "task_board_acceptance_index" not in request_text
    assert "task_board_handoff_projection" not in request_text
    assert "handoff_projection" not in str(task_meta)
    assert "acceptance_index" not in str(task_meta)


@pytest.mark.asyncio
async def test_structured_deliverable_contract_requires_task_workspace_readback(tmp_path):
    agent = _create_goal_pursuit_agent("execution-deliverable-contract-guard").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Produce the requested final file.",
        success_criteria=["The final deliverable file exists."],
        execution="flat",
        max_iterations=1,
    )
    execution.input(
        {
            "output_contract": {
                "deliverables": [{"path": "final.md", "media_type": "text/markdown"}],
            }
        }
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    verification = task_meta["iterations"][0]["verification"]

    assert result["accepted"] is False
    assert result["status"] == "max_iterations"
    assert verification["is_complete"] is False
    assert "required_task_workspace_deliverable_missing" in verification["guard_reasons"]
    assert "Missing required TaskWorkspace deliverable(s): final.md" in verification["missing_criteria"]


@pytest.mark.asyncio
async def test_flat_verifier_repair_constraints_feed_next_planner(tmp_path):
    agent = _create_flat_repair_constraint_agent("execution-flat-repair-constraints").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Produce an Agent engineering weekly report with 5-8 news items.",
        success_criteria=["The final report contains 5-8 news items."],
        execution="flat",
        max_iterations=2,
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    first_verification = task_meta["iterations"][0]["verification"]
    second_plan = task_meta["iterations"][1]["plan"]
    second_plan_prompt = MockFlatRepairConstraintRequester.second_plan_prompt
    second_execution_prompt = MockFlatRepairConstraintRequester.second_execution_prompt

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == (
        "TaskWorkspace artifact delivered at final.md; full content is available through file_refs/readback."
    )
    assert first_verification["is_complete"] is False
    assert first_verification["failure_analysis"] == "The candidate artifact overshoots the accepted item count."
    assert "The report must include 5-8 news items, not 15." in first_verification["acceptance_delta"]
    assert "Reduce the report to 5-8 news items." in first_verification["repair_constraints"]
    assert "The report must include 5-8 news items, not 15." in first_verification["repair_constraints"]
    assert (
        "Revise the candidate report; do not restart evidence gathering."
        in first_verification["next_step_requirements"]
    )
    assert "Revise the report to satisfy the item-count constraint." in first_verification["next_step_requirements"]
    assert second_plan["step_instruction"].startswith("Revise the candidate report")
    assert "repair_context" in second_plan_prompt
    assert "advisory_repair_constraints" in second_plan_prompt
    assert "acceptance_delta" in second_plan_prompt
    assert "Reduce the report to 5-8 news items." in second_plan_prompt
    assert "Revise the candidate report; do not restart evidence gathering." in second_plan_prompt
    assert "repair_context" in second_execution_prompt
    assert (
        "[input.repair_context.verification_source] distinguishes intermediate observations from verification feedback"
        in second_execution_prompt
    )
    assert "Actual guard and repair findings still apply." in second_execution_prompt
    assert "Reduce the report to 5-8 news items." in second_execution_prompt


@pytest.mark.asyncio
async def test_flat_actions_shape_activates_framework_actions_from_capabilities(tmp_path):
    agent = _create_flat_action_agent("execution-flat-action-strategy").use_task_workspace(tmp_path / "task_workspace")

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok", "evidence": "framework action executed"}

    execution = agent.create_task(
        goal="Collect action evidence.",
        success_criteria=["The probe action executes."],
        execution="flat",
        max_iterations=1,
    ).use_actions(["probe_action"])

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    delta_text = "".join([chunk async for chunk in execution.get_async_generator(type="delta")])
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    iteration = task_meta["iterations"][0]
    step_execution = iteration["plan"]["step_execution"]
    action_logs = iteration["execution_meta"]["logs"]["action_logs"]
    if isinstance(action_logs, dict):
        action_ids = list(action_logs.keys())
    else:
        action_ids = [item.get("action_id") for item in action_logs]
    action_planning_requests = [
        json.loads(request)
        for request in MockAgentExecutionRequester.requests
        if "next_action" in request and "execution_commands" in request
    ]

    assert result["accepted"] is True
    assert step_execution["effective_shape"] == "actions"
    assert step_execution["action_scope_source"] == "planner_capabilities"
    assert set(action_ids) == {"probe_action"}
    assert action_ids
    assert action_planning_requests
    assert set(action_planning_requests[0]["output"]) == {
        "next_action",
        "execution_commands",
        "response",
    }
    delta_paragraphs = [item for item in delta_text.split("\n\n") if item.strip()]
    assert len(delta_paragraphs) >= 3
    assert "🔄 `probe_action` — Running" in delta_text
    assert "✅ `probe_action` — Completed" in delta_text
    assert "Result:" in delta_text
    assert "flat action accepted result" in delta_text
    assert delta_text.index("🔄 `probe_action` — Running") < delta_text.index("✅ `probe_action` — Completed")


@pytest.mark.asyncio
async def test_flat_request_timeout_does_not_cancel_progressing_child_execution(tmp_path):
    agent = _create_flat_action_planning_slow_agent("execution-flat-action-planning-slow").use_task_workspace(
        tmp_path / "task_workspace"
    )

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok"}

    execution = agent.create_task(
        goal="Collect action evidence.",
        success_criteria=["The probe action executes."],
        execution="flat",
        max_iterations=1,
        options={
            "request_timeout_seconds": 0.6,
            "agent_task": {"request_timeout_seconds": 2.0},
        },
    ).use_actions(["probe_action"])

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "flat action accepted result"
    assert not task_meta["diagnostics"].get("execution_errors")


@pytest.mark.asyncio
async def test_flat_action_planning_stall_returns_structured_child_failure(tmp_path):
    agent = _create_flat_action_planning_stall_agent("execution-flat-action-planning-timeout").use_task_workspace(
        tmp_path / "task_workspace"
    )

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok"}

    execution = agent.create_task(
        goal="Collect action evidence.",
        success_criteria=["The probe action executes."],
        execution="flat",
        max_iterations=1,
        limits={"max_no_progress_seconds": 0.5},
    ).use_actions(["probe_action"])

    started_at = asyncio.get_running_loop().time()
    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    elapsed = asyncio.get_running_loop().time() - started_at
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    execution_error = task_meta["diagnostics"]["execution_errors"][0]

    assert elapsed < 8.0
    assert result["status"] in {"max_iterations", "timed_out"}
    assert result["accepted"] is False
    assert execution_error["type"] != "_AgentTaskDeadlineExceeded"
    assert (
        "max_no_progress_seconds" in execution_error["message"]
        or "no progress" in execution_error["message"]
        or "stalled" in execution_error["message"]
    )
    assert task_meta["iterations"][0]["execution_meta"]["status"] == "failed"
    assert task_meta["iterations"][0]["verification"]["is_complete"] is False


@pytest.mark.asyncio
async def test_flat_plan_no_progress_timeout_is_reported_as_idle_guard(tmp_path):
    agent = _create_flat_slow_plan_agent("execution-flat-plan-no-progress-timeout").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Answer after a slow plan.",
        success_criteria=["The final answer is present."],
        execution="flat",
        max_iterations=1,
        limits={"max_no_progress_seconds": 0.05},
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    terminal_phase = task_meta["diagnostics"]["phases"][-1]

    assert result["status"] == "timed_out"
    assert terminal_phase["phase"] == "terminal"
    assert terminal_phase["diagnostics"]["stage"] == "plan"
    assert terminal_phase["diagnostics"]["limit_name"] == "max_no_progress_seconds"
    assert "no progress" in terminal_phase["diagnostics"]["reason"]


@pytest.mark.asyncio
async def test_agent_task_request_progress_resets_only_idle_deadline(tmp_path):
    agent = _create_agent("request-progress-resets-idle").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Complete a progressing request.",
        success_criteria=["The request finishes after several progress events."],
        execution="flat",
        limits={"max_no_progress_seconds": 0.06},
        options={"heartbeat_interval_seconds": 0.01},
    )

    async def progressing_request() -> str:
        for sequence in range(5):
            await asyncio.sleep(0.03)
            await task._emit(
                "agent_task.test.request_progress",
                {"sequence": sequence},
                meta={
                    "task_id": task.id,
                    "status": task.status,
                    "stream_kind": "progress",
                },
            )
        return "completed"

    started = time.monotonic()
    result = await task._await_task_request(progressing_request(), stage="plan")

    assert result == "completed"
    assert time.monotonic() - started > 0.12


@pytest.mark.asyncio
async def test_agent_task_request_heartbeat_does_not_reset_idle_deadline(tmp_path):
    agent = _create_agent("request-heartbeat-idle").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Detect an idle request.",
        success_criteria=["Heartbeat remains observational."],
        execution="flat",
        limits={"max_no_progress_seconds": 0.05},
        options={"heartbeat_interval_seconds": 0.01},
    )

    with pytest.raises(_AgentTaskDeadlineExceeded) as caught:
        await task._await_task_request(asyncio.sleep(1), stage="plan")

    assert caught.value.limit_name == "max_no_progress_seconds"
    assert any(item.path == "agent_task.heartbeat" for item in task._stream_items)


@pytest.mark.asyncio
async def test_agent_task_request_timeout_stays_fixed_while_progress_continues(tmp_path):
    agent = _create_agent("request-fixed-timeout").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Respect a fixed request timeout.",
        success_criteria=["Progress cannot extend the request deadline."],
        execution="flat",
        limits={"max_no_progress_seconds": 0.05},
        options={"agent_task": {"request_timeout_seconds": 0.13}},
    )
    cancellations = 0

    async def progressing_request() -> None:
        nonlocal cancellations
        try:
            while True:
                await asyncio.sleep(0.025)
                await task._emit(
                    "agent_task.test.request_progress",
                    {"status": "running"},
                    meta={
                        "task_id": task.id,
                        "status": task.status,
                        "stream_kind": "progress",
                    },
                )
        except asyncio.CancelledError:
            cancellations += 1
            raise

    with pytest.raises(_AgentTaskDeadlineExceeded) as caught:
        await task._await_task_request(progressing_request(), stage="verify")

    assert caught.value.limit_name == "request_timeout_seconds"
    assert cancellations == 1


@pytest.mark.asyncio
async def test_agent_task_max_seconds_stays_fixed_while_progress_continues(tmp_path):
    agent = _create_agent("request-fixed-task-deadline").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Respect the task deadline.",
        success_criteria=["Progress cannot extend max_seconds."],
        execution="flat",
        limits={"max_seconds": 0.13, "max_no_progress_seconds": 0.05},
        options={"agent_task": {"request_timeout_seconds": 1.0}},
    )
    task.started_at = time.time()

    async def progressing_request() -> None:
        while True:
            await asyncio.sleep(0.025)
            await task._emit(
                "agent_task.test.request_progress",
                {"status": "running"},
                meta={
                    "task_id": task.id,
                    "status": task.status,
                    "stream_kind": "progress",
                },
            )

    with pytest.raises(_AgentTaskDeadlineExceeded) as caught:
        await task._await_task_request(progressing_request(), stage="finalize")

    assert caught.value.limit_name == "max_seconds"


@pytest.mark.asyncio
async def test_flat_action_planning_stall_preserves_completed_action_logs(tmp_path):
    agent = _create_flat_action_post_execution_planning_stall_agent(
        "execution-flat-action-post-action-planning-timeout"
    ).use_task_workspace(tmp_path / "task_workspace")

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok", "evidence": "framework action executed"}

    execution = agent.create_task(
        goal="Collect action evidence.",
        success_criteria=["The probe action executes."],
        execution="flat",
        max_iterations=1,
        limits={"max_no_progress_seconds": 1.5},
    ).use_actions(["probe_action"])

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    iteration = task_meta["iterations"][0]
    action_logs = iteration["execution_meta"]["logs"]["action_logs"]
    action_log_list = list(action_logs.values()) if isinstance(action_logs, dict) else action_logs

    assert result["accepted"] is False
    assert iteration["execution_meta"]["status"] == "failed"
    assert [item.get("action_id") for item in action_log_list] == ["probe_action"], (
        iteration["execution_meta"].get("logs"),
        iteration["execution_meta"].get("diagnostics"),
    )
    assert action_log_list[0]["status"] in {"success", "succeeded"}
    assert action_log_list[0]["route"] == "model_request"


@pytest.mark.asyncio
async def test_agent_task_heartbeat_does_not_reset_progress_clock(tmp_path):
    agent = _create_agent("execution-heartbeat-progress-clock").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Track progress clock.",
        success_criteria=["Heartbeat remains observational."],
        execution="flat",
    )
    previous_progress_at = task._last_stream_emit_monotonic - 3.0
    task._last_stream_emit_monotonic = previous_progress_at

    heartbeat_item = await task._emit(
        "agent_task.heartbeat",
        {"stage": "execute", "status": "running"},
        meta={"task_id": task.id, "status": task.status, "stream_kind": "heartbeat"},
    )

    assert task._last_stream_emit_monotonic == previous_progress_at
    execution = agent.create_execution(limits={"max_no_progress_seconds": 1.0})
    parent_progress_at = execution.execution_context.last_progress_at
    await execution.bridge_agent_task_stream_item(heartbeat_item)
    assert execution.execution_context.last_progress_at == parent_progress_at

    progress_item = await task._emit(
        "agent_task.progress",
        {"stage": "execute", "status": "running"},
        meta={"task_id": task.id, "status": task.status, "stream_kind": "progress"},
    )

    assert task._last_stream_emit_monotonic > previous_progress_at
    await execution.bridge_agent_task_stream_item(progress_item)
    assert execution.execution_context.last_progress_at > parent_progress_at
    assert execution.execution_context.last_progress_event is not None
    assert execution.execution_context.last_progress_event["stage"] == "agent_task.progress"


@pytest.mark.asyncio
async def test_routed_agent_task_material_progress_refreshes_parent_idle_clock(tmp_path):
    agent = _create_agent("execution-routed-task-material-progress").use_task_workspace(
        tmp_path / "task_workspace"
    )
    idle_timeout_seconds = 0.08
    execution = (
        agent.create_execution(limits={"max_no_progress_seconds": idle_timeout_seconds})
        .goal(
            "Finish a routed task while reporting material child progress.",
            success_criteria=["The parent remains live while the child advances."],
        )
        .strategy(
            "flat",
            max_iterations=1,
            options={"heartbeat_interval_seconds": 0.01},
        )
    )

    async def request_plan(_iteration_index, _context_pack):
        task = cast(AgentTask, execution.task_record)
        for sequence in range(5):
            await asyncio.sleep(0.04)
            await task._emit(
                "agent_task.test.material_progress",
                {"sequence": sequence},
                meta={
                    "task_id": task.id,
                    "status": task.status,
                    "stream_kind": "progress",
                },
            )
        return {
            "execution_shape": "direct",
            "step_instruction": "Finish after reporting bounded progress.",
            "expected_evidence": "The routed child stayed live.",
            "rationale": "The child has advanced through five observable units.",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {
                "step_result": "bounded child work completed",
                "evidence": ["five material progress events were emitted"],
                "remaining_work": [],
                "ready_for_final_verification": True,
            },
            {
                "execution_id": "routed-task-material-progress-child",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def request_verification(_iteration_index, **_kwargs):
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "The routed child completed after observable progress.",
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result": "Routed task completed after material child progress.",
        }

    cast(Any, execution)._agent_task_step_overrides = {
        "_request_plan": request_plan,
        "_execute_step": execute_step,
        "_request_verification": request_verification,
    }
    assert execution.limits["max_no_progress_seconds"] == idle_timeout_seconds

    started_at = asyncio.get_running_loop().time()
    result = await execution.async_get_full_data()
    elapsed = asyncio.get_running_loop().time() - started_at

    assert elapsed > idle_timeout_seconds * 2
    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert execution.status == "success"
    assert len(
        [
            item
            for item in execution.stream.items
            if item.path == "agent_task.test.material_progress"
        ]
    ) == 5


@pytest.mark.asyncio
async def test_agent_task_concurrent_heartbeat_loops_emit_once_per_interval(tmp_path):
    agent = _create_agent("execution-heartbeat-concurrent-dedupe").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Track quiet progress.",
        success_criteria=["Heartbeat remains observational."],
        execution="flat",
        options={"heartbeat_interval_seconds": 0.02},
    )
    task._last_stream_emit_monotonic = time.monotonic() - 1.0
    first = task._start_heartbeat(stage="plan")
    second = task._start_heartbeat(stage="plan")
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            heartbeat_items = [
                item for item in task._stream_items if getattr(item, "path", "") == "agent_task.heartbeat"
            ]
            if heartbeat_items:
                await asyncio.sleep(0)
                break
            await asyncio.sleep(0.005)
        heartbeat_items = [
            item for item in task._stream_items if getattr(item, "path", "") == "agent_task.heartbeat"
        ]
        assert len(heartbeat_items) == 1
        assert heartbeat_items[0].value["stage"] == "plan"
    finally:
        await task._stop_heartbeat(first)
        await task._stop_heartbeat(second)


@pytest.mark.asyncio
async def test_task_workspace_artifact_draft_timeout_emits_heartbeat_and_diagnostics(tmp_path):
    agent = _create_task_workspace_artifact_draft_stall_agent("execution-workspace-artifact-draft-timeout").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Produce final.md.",
        success_criteria=["final.md is written and read back."],
        execution="flat",
        max_iterations=1,
        options={
            "request_timeout_seconds": 1.0,
            "agent_task": {
                "request_timeout_seconds": 1.0,
                "heartbeat_interval_seconds": 0.1,
            },
        },
    )

    async def collect_stream() -> list[Any]:
        return [item async for item in execution.get_async_generator(type="instant")]

    started_at = asyncio.get_running_loop().time()
    stream_task = asyncio.create_task(collect_stream())
    result = await execution.async_get_full_data()
    stream_items = await stream_task
    meta = await execution.async_get_meta()
    delta_text = "".join([chunk async for chunk in execution.get_async_generator(type="delta")])
    elapsed = asyncio.get_running_loop().time() - started_at
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    deliveries = task_meta["diagnostics"]["task_workspace_artifact_delivery"]
    failed_delivery = deliveries[-1]
    heartbeat_items = [item for item in stream_items if getattr(item, "path", "") == "agent_task.heartbeat"]

    assert elapsed < 4
    assert result["accepted"] is False
    assert failed_delivery["status"] == "failed"
    assert failed_delivery["error"]["type"] == "_AgentTaskDeadlineExceeded"
    assert "task_workspace_artifact_draft stream produced no event" in failed_delivery["error"]["message"]
    assert heartbeat_items
    assert any(
        getattr(item, "value", {}).get("stage") == "task_workspace_artifact_draft"
        for item in heartbeat_items
        if isinstance(getattr(item, "value", None), dict)
    )
    assert "Still working" not in delta_text
    assert not (tmp_path / "task_workspace" / "final.md").exists()


@pytest.mark.asyncio
async def test_task_workspace_artifact_draft_retry_status_resets_partial_without_public_delta_marker(tmp_path):
    agent = _create_task_workspace_artifact_draft_retry_agent("execution-workspace-artifact-draft-retry").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Produce final.md.",
        success_criteria=["final.md is written and read back."],
        execution="flat",
        max_iterations=1,
    )

    await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    deliveries = task_meta["diagnostics"]["task_workspace_artifact_delivery"]
    failed_delivery = deliveries[-1]

    assert MockWorkspaceArtifactDraftRetryRequester.draft_calls == 1
    assert MockWorkspaceArtifactDraftRetryRequester.draft_outputs
    assert all(output is None for output in MockWorkspaceArtifactDraftRetryRequester.draft_outputs)
    assert failed_delivery["status"] == "failed"
    assert failed_delivery["error"]["type"] == "EmptyWorkspaceArtifactDraft"
    assert failed_delivery["bytes_written"] == 0
    assert failed_delivery["retry_boundaries"] == [
        {
            "status": "retrying",
            "attempt_index": 1,
            "next_attempt_index": 2,
            "reason": "transient provider disconnect",
            "source": "structured_status",
        }
    ]
    assert not (tmp_path / "task_workspace" / "files" / "final.md").exists()


@pytest.mark.asyncio
async def test_task_workspace_artifact_draft_writes_natural_text_without_output_contract(tmp_path):
    agent = _create_task_workspace_artifact_draft_natural_text_agent(
        "execution-workspace-artifact-draft-natural-text"
    ).use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Produce final.md.",
        success_criteria=["final.md is written and read back."],
        execution="flat",
        max_iterations=1,
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    deliveries = task_meta["diagnostics"]["task_workspace_artifact_delivery"]
    delivered = deliveries[-1]
    file_ref = delivered["file_refs"][0]

    assert result["accepted"] is True
    assert MockWorkspaceArtifactDraftNaturalTextRequester.draft_outputs == [None]
    assert delivered["status"] == "delivered"
    assert delivered.get("retry_boundaries", []) == []
    assert delivered["public_replay_markers"][0]["source"] == "delta_replay_marker"
    assert delivered["public_replay_markers"][0]["reason"] == "transient provider disconnect"
    assert file_ref["preview"] == "# Final\n\nNatural-text artifact body."
    assert "<$retry>" not in file_ref["preview"]
    assert "Partial attempt" not in file_ref["preview"]
    assert delivered["readback"]["bytes"] == len("# Final\n\nNatural-text artifact body.".encode("utf-8"))


@pytest.mark.asyncio
async def test_flat_actions_shape_fans_out_multiple_commands_in_one_step(tmp_path):
    agent = _create_flat_parallel_action_agent("execution-flat-action-fanout").use_task_workspace(tmp_path / "task_workspace")
    active = 0
    max_active = 0

    async def run_probe(label: str) -> dict[str, str]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"status": "ok", "label": label}

    @agent.action_func
    async def slow_a() -> dict[str, str]:
        return await run_probe("a")

    @agent.action_func
    async def slow_b() -> dict[str, str]:
        return await run_probe("b")

    execution = agent.create_task(
        goal="Collect two independent action evidence records.",
        success_criteria=["Both independent action results are collected."],
        execution="flat",
        max_iterations=1,
    ).use_actions(["slow_a", "slow_b"])

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    all_items: list[AgentExecutionStreamData] = []
    async for record in execution.get_async_generator(type="all"):
        item = record[1] if isinstance(record, tuple) and len(record) == 2 else record
        if isinstance(item, AgentExecutionStreamData):
            all_items.append(item)
    delta_text = "".join([chunk async for chunk in execution.get_async_generator(type="delta")])
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    action_logs = task_meta["iterations"][0]["execution_meta"]["logs"]["action_logs"]
    if isinstance(action_logs, dict):
        action_ids = list(action_logs.keys())
    else:
        action_ids = [item.get("action_id") for item in action_logs]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert max_active == 2
    assert set(action_ids) == {"slow_a", "slow_b"}
    planned_index = next(
        index
        for index, item in enumerate(all_items)
        if getattr(item, "path", "") == "agent_task.action.batch.planned"
    )
    started_indexes = [
        index
        for index, item in enumerate(all_items)
        if getattr(item, "path", "") == "agent_task.action.started"
    ]
    assert started_indexes and planned_index < min(started_indexes)
    planned_item = all_items[planned_index]
    assert planned_item.value["parallel"] is True
    assert {item["action_id"] for item in planned_item.value["actions"]} == {"slow_a", "slow_b"}
    assert "🚀 Next parallel Action batch — 2 tasks" in delta_text
    assert "🎯 Task completed" in delta_text


@pytest.mark.asyncio
async def test_flat_verifier_uses_bounded_action_evidence_prompt(tmp_path):
    agent = _create_flat_action_agent("execution-flat-verifier-evidence-bounds").use_task_workspace(tmp_path / "task_workspace")

    hidden_tail = "VERIFIER_SHOULD_NOT_SEE_FULL_ACTION_OUTPUT"

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {
            "status": "ok",
            "payload": ("x" * 8000) + hidden_tail + ("z" * 8000),
        }

    execution = agent.create_task(
        goal="Collect action evidence without flooding the verifier.",
        success_criteria=["The probe action executes."],
        execution="flat",
        max_iterations=1,
    ).use_actions(["probe_action"])

    result = await execution.async_get_full_data()
    verify_requests = [
        request
        for request in MockAgentExecutionRequester.requests
        if "Verify the task against every success criterion" in request
    ]

    assert result["accepted"] is True
    assert verify_requests
    verify_prompt = verify_requests[-1]
    assert "probe_action" in verify_prompt
    assert "artifact_refs" in verify_prompt
    assert hidden_tail not in verify_prompt
    assert len(verify_prompt) < 80000


@pytest.mark.asyncio
async def test_flat_promotes_report_like_evidence_to_candidate_final_result(tmp_path):
    agent = _create_flat_evidence_candidate_agent("execution-flat-evidence-candidate").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Produce a Markdown weekly report.",
        success_criteria=["The final report is returned."],
        execution="flat",
        max_iterations=1,
    )

    result = await execution.async_get_full_data()
    verify_requests = [
        request
        for request in MockAgentExecutionRequester.requests
        if "Verify the task against every success criterion" in request
    ]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"]["preview"].startswith("# Weekly Report")
    assert result["final_result"]["chars"] == len(MockFlatEvidenceCandidateRequester.report.strip())
    assert result["final_result"]["truncated"] is True
    assert MockFlatEvidenceCandidateRequester.report not in str(result["final_result"])
    assert verify_requests
    assert "candidate_final_result" in verify_requests[-1]
    assert "Weekly Report" in verify_requests[-1]


@pytest.mark.asyncio
async def test_taskboard_execution_strategy_runs_framework_owned_board(tmp_path):
    agent = _create_taskboard_agent("execution-taskboard-strategy").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Produce a board-managed answer.",
        success_criteria=["The board final answer is accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    phases = task_meta["diagnostics"]["phases"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["execution_strategy"] == "taskboard"
    assert result["final_result"] == "taskboard accepted result"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["execution_strategy"] == "taskboard"
    assert task_meta["execution_strategy"] == "taskboard"
    lifecycle_phase = next(phase for phase in phases if phase["phase"] == "taskboard_lifecycle_started")
    tick_phase = next(phase for phase in phases if phase["phase"] == "taskboard_tick")
    assert lifecycle_phase["diagnostics"]["runtime_topology"]["driver"] == "triggerflow_taskboard_lifecycle"
    assert tick_phase["diagnostics"]["runtime_topology"]["driver"] == "triggerflow_taskboard_lifecycle"
    assert tick_phase["diagnostics"]["runtime_topology"]["tick"]["fanout"] == "signal_net_frontier_overlay"
    collect_result = taskboard["revision"]["card_results"]["collect"]
    assert collect_result["status"] == "completed"
    block_carrier = collect_result["metadata"]["block_carrier"]
    assert block_carrier["work_unit"]["origin"] == "taskboard_card"
    assert block_carrier["work_unit_result"]["id"] == block_carrier["work_unit"]["id"]
    assert block_carrier["output_policy"]["body_transport"] == "structured_control"
    assert taskboard["evidence_view"]["cards"][0]["card_id"] == "collect"
    assert "content" not in taskboard["evidence_view"]["cards"][0]["artifact_refs"]
    assert any(item.path == "agent_task.taskboard.card.collect.execution.started" for item in stream_items)
    assert any(
        item.path == "agent_task.taskboard.card.collect.execution.route.selected"
        and (item.meta or {}).get("stream_kind") == "child_execution"
        and (item.meta or {}).get("stage") == "taskboard_card"
        and (item.meta or {}).get("card_id") == "collect"
        for item in stream_items
    )


@pytest.mark.asyncio
async def test_taskboard_checkpoint_and_resume_snapshot_include_handoff_projection(tmp_path):
    task_id = "taskboard-resume-checkpoint"
    task_workspace_dir = tmp_path / "task_workspace"
    agent = _create_taskboard_agent("execution-taskboard-checkpoint").use_task_workspace(task_workspace_dir)

    execution = agent.create_task(
        task_id=task_id,
        goal="Produce a board-managed answer.",
        success_criteria=["The board final answer is accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]

    assert result["status"] == "completed"
    assert task_meta["record_refs"]["checkpoints"] == []
    checkpoint_history = await agent.record_store.checkpoint_history(task_id)
    assert checkpoint_history == []

    snapshot = await agent.record_store.get_snapshot(f"{task_id}::resume")
    assert snapshot is None
    terminal_state = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    assert terminal_state["revision"]["revision_id"]
    assert task_meta["diagnostics"]["task_workspace_retention"]["status"] in {"applied", "noop"}


@pytest.mark.asyncio
async def test_taskboard_recovery_snapshot_persists_task_level_read_progress(tmp_path):
    task_id = "taskboard-read-progress-resume"
    task_workspace_dir = tmp_path / "task_workspace"
    agent = _create_taskboard_agent(
        "execution-taskboard-read-progress-resume-seed"
    ).use_task_workspace(task_workspace_dir)
    task = AgentTask(
        agent,
        task_id=task_id,
        goal="Resume a board without rereading completed evidence ranges.",
        success_criteria=["Read progress is restored with the TaskBoard revision."],
        execution="taskboard",
        options={"agent_task": {"record_store_recovery": True}},
    )
    task._record_taskboard_read_progress(
        {
            "ok": True,
            "owner": "action_artifact",
            "locator": "sel-resume-progress",
            "selection_key": "sel-resume-progress",
            "content_version": "version-resume",
            "total_bytes": 20_000,
            "range": {"offset": 0, "end": 12_000},
        },
        card_id="read-first",
    )
    revision = TaskBoardRevision.create(
        board_id=task_id,
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": f"{task_id}.graph",
                "cards": [
                    {
                        "id": "continue-read",
                        "objective": "Read only the remaining evidence range.",
                    }
                ],
            }
        ),
    )

    await task._record_taskboard_checkpoint(
        stage="tick",
        tick_index=1,
        revision=revision,
        runtime_topology={"driver": "test"},
    )

    snapshot = await agent.record_store.get_snapshot(f"{task_id}::resume")
    assert snapshot is not None
    read_progress = snapshot["taskboard_state"]["read_progress"]
    assert read_progress["schema_version"] == "agent_task_taskboard_read_progress/v1"
    assert next(iter(read_progress["items"].values()))["next_offset"] == 12_000

    agent2 = _create_taskboard_agent(
        "execution-taskboard-read-progress-resume-restore"
    ).use_task_workspace(task_workspace_dir)
    resumed = await agent2.async_resume(task_id, task_workspace=task_workspace_dir)
    resumed_task = cast(AgentTask, resumed.task_record)
    assert resumed_task._taskboard_requested_read_range(
        {
            "owner": "action_artifact",
            "locator": "sel-resume-progress",
            "selection_key": "sel-resume-progress",
            "content_version": "version-resume",
            "bytes": 20_000,
        },
        {"evidence_items": []},
        default_max_bytes=12_000,
    ) == (12_000, 12_000)


@pytest.mark.asyncio
async def test_taskboard_resume_terminal_snapshot_without_reexecuting_cards(tmp_path):
    task_id = "taskboard-terminal-resume"
    task_workspace_dir = tmp_path / "task_workspace"
    agent = _create_taskboard_agent("execution-taskboard-terminal-resume-1").use_task_workspace(task_workspace_dir)

    execution = agent.create_task(
        task_id=task_id,
        goal="Produce a board-managed answer.",
        success_criteria=["The board final answer is accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    result = await execution.async_get_full_data()
    assert result["status"] == "completed"
    assert result["final_result"] == "taskboard accepted result"

    agent2 = _create_taskboard_agent("execution-taskboard-terminal-resume-2").use_task_workspace(task_workspace_dir)
    MockAgentExecutionRequester.requests = []
    with pytest.raises(ValueError, match="No resumable AgentTask snapshot"):
        await agent2.async_resume(task_id, task_workspace=task_workspace_dir)
    assert MockAgentExecutionRequester.requests == []


@pytest.mark.asyncio
async def test_taskboard_resume_blocked_snapshot_retries_finalization_without_replanning(tmp_path):
    task_id = "taskboard-blocked-resume"
    task_workspace_dir = tmp_path / "task_workspace"
    agent = _create_taskboard_agent("execution-taskboard-blocked-resume-seed").use_task_workspace(task_workspace_dir)
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task_id,
            "revision_id": "rev-blocked",
            "graph": {
                "graph_id": f"{task_id}.graph",
                "cards": [
                    {
                        "id": "collect",
                        "objective": "Collect one fact and summarize it.",
                        "allowed_execution_shape": "model",
                    }
                ],
            },
            "card_results": {
                "collect": {
                    "card_id": "collect",
                    "status": "completed",
                    "preview": "taskboard card result",
                    "metadata": {"note": "completed before resume"},
                }
            },
            }
        )
    seed_task = AgentTask(
        agent,
        task_id=task_id,
        goal="Produce a board-managed answer.",
        success_criteria=["The board final answer is accepted."],
        execution="taskboard",
    )
    await agent.record_store.put_snapshot(
        f"{task_id}::resume",
        {
            "resume_version": 3,
            "task_id": task_id,
            "iteration": 1,
            "context_state": seed_task._context_resume_state(),
            "manifest": {
                "goal": "Produce a board-managed answer.",
                "success_criteria": ["The board final answer is accepted."],
                "execution_strategy": "taskboard",
                "effective_execution_strategy": "taskboard",
                "task_shape_analysis": {},
                "max_iterations": None,
                "verify": "before_done",
                "context_profile": "auto",
                "context_budget": {"chars": 6000},
                "limits": {},
                "options": {},
            },
            "iterations_summary": [],
            "reflection_summaries": [],
            "satisfied_required_actions": [],
            "satisfied_required_skills": [],
            "satisfied_capabilities": [],
            "satisfied_succeeded_actions": [],
            "failed_execution_shapes": [],
            "taskboard_state": {
                "schema_version": "agent_task_taskboard_resume/v1",
                "stage": "finalize",
                "tick_index": 1,
                "status": "blocked",
                "terminal_reason": "final_verification_failed",
                "revision": revision.to_dict(),
                "evidence_view": build_task_board_evidence_view(revision).to_dict(),
                "runtime_topology": {"driver": "triggerflow_taskboard_lifecycle"},
                "task_workspace_refs": {},
                "final_result": {
                    "status": "blocked",
                    "accepted": False,
                    "artifact_status": "partial",
                    "reason": "final verification failed",
                },
            },
            "last_verification": {
                "is_complete": False,
                "requires_block": True,
                "status": "blocked",
                "accepted": False,
                "artifact_status": "partial",
                "reason": "final verification failed",
                "final_result": "",
            },
        },
    )

    agent2 = _create_taskboard_agent("execution-taskboard-blocked-resume").use_task_workspace(task_workspace_dir)
    MockAgentExecutionRequester.requests = []
    resumed = await agent2.async_resume(task_id, task_workspace=task_workspace_dir)
    resumed_result = await resumed.async_start()

    request_text = "\n".join(MockAgentExecutionRequester.requests)
    assert resumed_result["status"] == "completed"
    assert resumed_result.get("resumed") is not True
    assert not _is_taskboard_plan_request(request_text)
    assert "Execute exactly one TaskBoard card" not in request_text
    assert _is_taskboard_final_request(request_text)
    assert "Verify the task against every success criterion" not in request_text


@pytest.mark.asyncio
async def test_taskboard_control_card_runs_single_model_request_through_block_carrier(tmp_path):
    agent = _create_taskboard_control_agent("execution-taskboard-control-card").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Produce a control-card deliverable.",
        success_criteria=["The control card final answer is accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    card_result = taskboard["revision"]["card_results"]["synthesize"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == (
        "TaskWorkspace artifact delivered at final.md; full content is available through file_refs/readback."
    )
    assert taskboard["finalization_source"] == "candidate_promotion"
    assert card_result["status"] == "completed"
    assert card_result["preview"]["task_workspace_artifact_delivery"]["status"] == "delivered"
    assert "Complete deliverable body." in card_result["preview"]["task_workspace_artifact_delivery"]["file_refs"][0]["preview"]
    assert card_result["metadata"]["execution_kind"] == "taskboard_control_request"
    block_carrier = card_result["metadata"]["block_carrier"]
    assert block_carrier["work_unit"]["origin"] == "taskboard_card"
    assert block_carrier["work_unit"]["runtime_preferences"]["handler"] == "agent_task_control_request"
    assert block_carrier["work_unit_result"]["id"] == block_carrier["work_unit"]["id"]
    assert block_carrier["output_policy"]["control_format"] == "json"
    assert block_carrier["block_graph"]["execution_block_kinds"] == ["agent_step"]
    assert block_carrier["block_graph"]["evidence_present"] is True
    assert block_carrier["block_graph"]["execution_block_result_kinds"] == ["agent_step"]
    assert any(item.path == "agent_task.taskboard.card.synthesize.control.started" for item in stream_items)
    assert any(
        (item.meta or {}).get("stream_kind") == "taskboard_control_request"
        and (item.meta or {}).get("card_id") == "synthesize"
        for item in stream_items
    )
    assert not any(item.path == "agent_task.taskboard.card.synthesize.execution.started" for item in stream_items)
    assert not any("Execute exactly one TaskBoard card" in request for request in MockAgentExecutionRequester.requests)
    planning_requests = [
        request for request in MockAgentExecutionRequester.requests if _is_taskboard_plan_request(request)
    ]
    assert planning_requests
    assert "serial chain of control-only cards" in planning_requests[-1]
    assert "control_card_guidance" in planning_requests[-1]


@pytest.mark.asyncio
async def test_taskboard_control_consumer_requests_readback_without_intermediate_verifier(tmp_path):
    agent = _create_taskboard_consumer_driven_agent("execution-taskboard-consumer-driven").use_task_workspace(
        tmp_path / "task_workspace"
    )
    skill_root = tmp_path / "consumer-control-skill"
    _write_skill(skill_root)
    skill_library = SkillLibrary(tmp_path / "skill-library")
    installed_skill = skill_library.install(skill_root, trust="trusted")
    agent.skill_library = skill_library
    skill_binding = SkillBinding.create(
        installed_skill,
        task_id="taskboard-control-consumer",
        mode="required",
        binding_id="binding_consumer_control",
    )
    task = AgentTask(
        agent,
        task_id="taskboard-control-consumer",
        goal="Use downstream control-card consumption to decide whether upstream evidence is enough.",
        success_criteria=["The control card requests readback when dependency evidence is only a ref."],
        execution="taskboard",
        max_iterations=None,
        options={
            "capability_constraints": {
                "skills": {"required": [installed_skill.skill_id]},
            },
            "skill_bindings": [
                {
                    "binding_id": "binding_consumer_control",
                    "canonical_skill_id": installed_skill.skill_id,
                    "mode": "required",
                }
            ],
        },
    )
    task.task_context.attach(
        SkillContextSource(skill_library, bindings=(skill_binding,)),
        binding_id="skill_context_binding:taskboard-control-consumer",
    )
    revision = TaskBoardRevision.create(
        board_id="consumer-driven-sufficiency",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "consumer-driven-sufficiency-graph",
                "cards": [
                    {
                        "id": "collect",
                        "objective": "Collect a source pointer.",
                        "allowed_execution_shape": "model",
                    },
                    {
                        "id": "review",
                        "objective": "Use dependency evidence and decide whether it is enough.",
                        "depends_on": ["collect"],
                        "allowed_execution_shape": "control",
                    },
                ],
            }
        ),
    )
    revision = TaskBoardValidator().apply_patch(
        revision,
        {
            "base_revision": revision.revision_id,
            "operations": [
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": {
                            "answer": "Collected a source ref only; no bounded readback yet.",
                            "source_refs": [
                                {
                                    "path": "sources/source.md",
                                    "field": "path",
                                    "content_state": "ref_only",
                                }
                            ],
                        },
                        "file_refs": [
                            {
                                "path": "sources/source.md",
                                "role": "evidence",
                                "content_state": "ref_only",
                            }
                        ],
                        "diagnostics": [{"code": "test.collect.ref_only"}],
                    },
                }
            ],
        },
    )

    context_pack = {
        "goal": task.goal,
        "profile": "",
        "items": [],
        "omitted": [],
        "diagnostics": {},
    }
    result = await task._run_taskboard_control_card(
        SimpleNamespace(
            card=revision.graph.card_by_id()["review"],
            revision=revision,
            dependency_results=revision.card_results,
            planning_policy=None,
        ),
        cast(Any, context_pack),
    )

    request_text = "\n".join(MockAgentExecutionRequester.requests)
    assert MockTaskBoardConsumerDrivenRequester.seen_dependency_evidence is True
    assert "Verify the task against every success criterion" not in request_text
    assert not _is_taskboard_final_request(request_text)
    assert result.status == "setback"
    assert result.patch_proposal is not None
    assert result.metadata["next_board_action"] == "readback"
    assert result.preview["sufficient"] is False
    assert result.preview["next_board_action"] == "readback"
    contexts = task._lifecycle_state.skill_bindings["binding_consumer_control"]["contexts"]
    assert len(contexts) == 1
    context_without_id = dict(contexts[0])
    control_request_id = context_without_id.pop("request_id")
    assert context_without_id == {
        "phase": "work.execute.taskboard",
        "guidance_chars": len(installed_skill.instruction_body),
        "resource_chars": 0,
        "selected_resource_keys": [],
        "truncated": False,
    }
    assert len(control_request_id) == 32
    assert not control_request_id.startswith(f"{task.id}:")

    next_revision = TaskBoardValidator().apply_patch(revision, result.patch_proposal)
    cards = next_revision.graph.card_by_id()
    assert cards["review.readback"].allowed_execution_shape == "readback"
    assert cards["review.readback"].depends_on == ("collect",)
    assert cards["review.continue"].allowed_execution_shape == "control"
    assert cards["review.continue"].depends_on == ("review", "collect", "review.readback")
    assert cards["review"].status == "skipped"
    assert cards["review"].metadata["superseded_by"] == "review.continue"


@pytest.mark.asyncio
async def test_taskboard_sectioned_artifact_uses_task_workspace_and_bounded_stream(tmp_path):
    agent = _create_taskboard_sectioned_artifact_agent("execution-taskboard-sectioned-artifact").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Produce a sectioned final report.",
        success_criteria=["The complete sectioned final report is written and accepted."],
        execution="taskboard",
        max_iterations=2,
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    deliveries = task_meta["diagnostics"]["task_workspace_artifact_delivery"]
    delivered = next(item for item in deliveries if item.get("status") == "delivered")
    tick_completed_items = [
        item
        for item in stream_items
        if str(getattr(item, "path", "")).endswith(".completed")
        and ".taskboard.tick." in str(getattr(item, "path", ""))
    ]
    final_requests = [
        request
        for request in MockAgentExecutionRequester.requests
        if _is_taskboard_final_request(request)
    ]
    marker = MockTaskBoardSectionedArtifactRequester.tail_marker
    expected_task_workspace_body = (
        f"{MockTaskBoardSectionedArtifactRequester.first_section.strip()}\n\n"
        f"## Details\n\n{MockTaskBoardSectionedArtifactRequester.second_section.strip()}"
    )

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == (
        "TaskWorkspace artifact delivered at final.md; full content is available through file_refs/readback."
    )
    assert marker not in result["final_result"]
    assert delivered["mode"] == "sectioned_task_workspace_artifact"
    assert delivered["readback"]["bytes"] == len(expected_task_workspace_body.encode("utf-8"))
    assert delivered["readback"]["sha256"]
    assert taskboard["revision"]["card_results"]["synthesize"]["preview"]["task_workspace_artifact_delivery"]["mode"] == (
        "sectioned_task_workspace_artifact"
    )
    assert not final_requests
    assert all(marker not in request for request in final_requests)
    assert tick_completed_items
    for item in tick_completed_items:
        value_text = json.dumps(DataFormatter.sanitize(getattr(item, "value", None)), ensure_ascii=False)
        assert marker not in value_text
        assert len(value_text) < 20000
    assert taskboard["finalization_source"] == "candidate_promotion"


@pytest.mark.asyncio
async def test_taskboard_final_preserves_complete_candidate_from_terminal_card(tmp_path):
    agent = _create_taskboard_final_candidate_agent("execution-taskboard-final-candidate").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Write the complete repository report.",
        success_criteria=["The final result preserves the complete Markdown deliverable."],
        execution="taskboard",
        max_iterations=2,
    )

    result = await execution.async_get_full_data()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == MockTaskBoardFinalCandidateRequester.full_report.strip()
    assert len(result["final_result"]) > len(MockTaskBoardFinalCandidateRequester.full_report[:120])


def test_taskboard_final_refs_prioritize_required_task_workspace_deliverable(tmp_path):
    agent = _create_agent("execution-taskboard-final-ref-priority").use_task_workspace(tmp_path / "task_workspace")
    task = AgentTask(
        agent,
        goal="Write a final TaskWorkspace report.",
        success_criteria=["final.md is the trusted final deliverable."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    refs = [
        {"path": "working/taskboard/clone_and_read_evidence/final.md", "role": "task_workspace_artifact"},
        {"path": "configs/_base_/default.yaml", "source": "taskboard_target_ref", "content_state": "ref_only"},
        {"path": "final.md", "role": "task_workspace_artifact", "bytes": 12000},
        {"artifact_id": "act_art_123", "role": "output"},
    ]

    ordered = task._prioritize_taskboard_final_refs(refs)

    assert ordered[0]["path"] == "final.md"
    assert ordered[1]["path"] == "working/taskboard/clone_and_read_evidence/final.md"
    assert ordered[-1]["artifact_id"] == "act_art_123"


@pytest.mark.asyncio
async def test_taskboard_card_can_read_dependency_action_artifact_refs(tmp_path):
    agent = _create_taskboard_readback_agent("execution-taskboard-readback").use_task_workspace(tmp_path / "task_workspace")

    @agent.action_func
    def produce_large_evidence() -> dict[str, Any]:
        return {
            "records": [
                {
                    "title": "Hidden evidence",
                    "url": "https://example.test/evidence",
                    "snippet": "detail available only through artifact readback",
                }
            ],
            "padding": "x" * 9000,
        }

    execution = agent.create_task(
        goal="Use a dependency cold artifact to finish the board.",
        success_criteria=["The review card reads back dependency evidence."],
        execution="taskboard",
        max_iterations=3,
    ).use_actions(["produce_large_evidence"])

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_record = cast(AgentTask, execution.task_record)
    taskboard = cast(dict[str, Any], task_record._terminal_taskboard_state)
    review_result = taskboard["revision"]["card_results"]["review"]
    readbacks = review_result["preview"]["readbacks"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard readback accepted result"
    assert review_result["status"] == "completed"
    assert task_record._action_artifact_scope_transferred_to_execution_id == execution.id
    assert task_record.diagnostics["action_artifact_release"] == {
        "status": "transferred",
        "scope": {"kind": "agent_task", "id": task_record.id},
        "owner": {"kind": "agent_execution", "id": execution.id},
    }
    assert meta["diagnostics"].get("action_artifact_release", {}).get("scope") == {
        "kind": "agent_task",
        "id": task_record.id,
    }
    assert agent.action._artifact_manager._artifacts == {}
    assert review_result["metadata"]["execution_kind"] == "taskboard_artifact_readback"
    block_carrier = review_result["metadata"]["block_carrier"]
    assert block_carrier["work_unit"]["origin"] == "taskboard_card"
    assert block_carrier["work_unit"]["runtime_preferences"]["handler"] == "agent_task_artifact_readback"
    assert block_carrier["work_unit"]["runtime_preferences"]["plan_block_kind"] == "action_call"
    assert block_carrier["block_graph"]["execution_block_kinds"] == ["action_call"]
    assert block_carrier["block_graph"]["evidence_present"] is True
    assert block_carrier["block_graph"]["execution_block_result_kinds"] == ["action_call"]
    assert readbacks[0]["ok"] is True
    assert "Hidden evidence" in json.dumps(readbacks[0]["value_preview"], ensure_ascii=False)
    assert any(item.path == "agent_task.taskboard.card.review.readback.started" for item in stream_items)
    assert any(item.path == "agent_task.taskboard.card.review.readback.completed" for item in stream_items)
    assert not any(item.path == "agent_task.taskboard.card.review.execution.started" for item in stream_items)


@pytest.mark.asyncio
async def test_taskboard_agent_card_prefetches_dependency_action_artifact_refs(tmp_path):
    agent = _create_taskboard_dependency_readback_agent("execution-taskboard-dependency-readback").use_task_workspace(
        tmp_path / "task_workspace"
    )

    @agent.action_func
    def produce_large_evidence() -> dict[str, Any]:
        return {
            "records": [
                {
                    "title": "Hidden evidence",
                    "url": "https://example.test/evidence",
                    "snippet": "detail available only through automatic dependency readback",
                }
            ],
            "padding": "x" * 9000,
        }

    execution = agent.create_task(
        goal="Use a dependency cold artifact without a dedicated readback card.",
        success_criteria=["The downstream card uses dependency readback evidence."],
        execution="taskboard",
        max_iterations=3,
    ).use_actions(["produce_large_evidence"])

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    synthesize_result = taskboard["revision"]["card_results"]["synthesize"]
    dependency_carrier = task_meta["diagnostics"]["taskboard_dependency_readback_block_carriers"][0][
        "block_carrier"
    ]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard dependency readback accepted result"
    assert synthesize_result["status"] == "completed"
    assert dependency_carrier["work_unit"]["runtime_preferences"]["handler"] == (
        "agent_task_dependency_artifact_readback"
    )
    assert dependency_carrier["work_unit"]["runtime_preferences"]["plan_block_kind"] == "action_call"
    assert dependency_carrier["block_graph"]["execution_block_kinds"] == ["action_call"]
    assert MockTaskBoardDependencyReadbackRequester.dependency_readback_seen is True
    assert MockTaskBoardDependencyReadbackRequester.source_refs_seen is True
    # Inspect content independently of JSON escaping and YAML line wrapping.
    request_text = " ".join(
        " ".join(message["content"].split())
        for request in MockTaskBoardDependencyReadbackRequester.requests
        for message in json.loads(request).get("messages", [])
    )
    assert "Action success or a selection_key proves only execution/ref availability" in request_text
    assert "do not read a recall Action''s output as a new artifact" in request_text
    assert any(item.path == "agent_task.taskboard.card.synthesize.dependency_readback.started" for item in stream_items)
    assert any(
        item.path == "agent_task.taskboard.card.synthesize.dependency_readback.completed" for item in stream_items
    )


@pytest.mark.asyncio
async def test_taskboard_control_card_prefetches_dependency_action_artifact_refs(tmp_path):
    agent = _create_taskboard_control_dependency_readback_agent(
        "execution-taskboard-control-dependency-readback"
    ).use_task_workspace(tmp_path / "task_workspace")

    @agent.action_func
    def produce_large_evidence() -> dict[str, Any]:
        return {
            "records": [
                {
                    "title": "Hidden evidence",
                    "url": "https://example.test/evidence",
                    "snippet": "detail available only through automatic control-card dependency readback",
                }
            ],
            "padding": "x" * 9000,
        }

    execution = agent.create_task(
        goal="Use a dependency cold artifact in a control synthesis card.",
        success_criteria=["The control card uses dependency readback evidence."],
        execution="taskboard",
        max_iterations=3,
    ).use_actions(["produce_large_evidence"])

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    synthesize_result = taskboard["revision"]["card_results"]["synthesize"]
    dependency_carrier = task_meta["diagnostics"]["taskboard_dependency_readback_block_carriers"][0][
        "block_carrier"
    ]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == (
        "TaskWorkspace artifact delivered at final.md; full content is available through file_refs/readback."
    )
    assert taskboard["finalization_source"] == "candidate_promotion"
    assert synthesize_result["status"] == "completed"
    assert dependency_carrier["work_unit"]["runtime_preferences"]["handler"] == (
        "agent_task_dependency_artifact_readback"
    )
    assert dependency_carrier["work_unit"]["runtime_preferences"]["plan_block_kind"] == "action_call"
    assert dependency_carrier["block_graph"]["execution_block_kinds"] == ["action_call"]
    block_carrier = synthesize_result["metadata"]["block_carrier"]
    assert block_carrier["work_unit"]["origin"] == "taskboard_card"
    assert block_carrier["work_unit"]["runtime_preferences"]["handler"] == "agent_task_control_request"
    assert block_carrier["block_graph"]["execution_block_kinds"] == ["agent_step"]
    assert block_carrier["block_graph"]["evidence_present"] is True
    assert MockTaskBoardControlDependencyReadbackRequester.dependency_readback_seen is True
    assert MockTaskBoardControlDependencyReadbackRequester.source_refs_seen is True
    assert any(item.path == "agent_task.taskboard.card.synthesize.dependency_readback.started" for item in stream_items)
    assert any(
        item.path == "agent_task.taskboard.card.synthesize.dependency_readback.completed" for item in stream_items
    )


@pytest.mark.asyncio
async def test_taskboard_request_timeout_does_not_cancel_progressing_card_by_default(tmp_path):
    agent = _create_taskboard_slow_card_agent("execution-taskboard-card-request-timeout-isolated").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Run a slow TaskBoard card without default card cancellation.",
        success_criteria=["The slow card is allowed to finish without an explicit card timeout."],
        execution="taskboard",
        max_iterations=1,
        options={
            "request_timeout_seconds": 0.2,
            "agent_task": {"request_timeout_seconds": 1.0},
        },
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    assert "taskboard" not in task_meta["result"], task_meta["result"]
    revision = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)["revision"]
    slow_result = revision["card_results"]["slow"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert slow_result["status"] == "completed"
    assert slow_result["preview"]["answer"] == "slow card eventually completed"
    assert not task_meta["diagnostics"].get("taskboard_card_errors")


@pytest.mark.asyncio
async def test_taskboard_card_timeout_returns_structured_card_failure(tmp_path):
    agent = _create_taskboard_slow_card_agent("execution-taskboard-card-timeout").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Run a board card that should time out.",
        success_criteria=["The board records a structured card timeout."],
        execution="taskboard",
        max_iterations=1,
        options={
            "request_timeout_seconds": 5.0,
            "agent_task": {"taskboard_card_timeout_seconds": 0.25},
        },
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    slow_result = taskboard["revision"]["card_results"]["slow"]
    diagnostic = slow_result["diagnostics"][0]

    assert result["status"] == "error"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert "taskboard" not in result
    assert taskboard["revision"]["card_results"]["slow"]["status"] == "failed"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["execution_strategy"] == "taskboard"
    assert slow_result["status"] == "failed"
    assert diagnostic["code"] == "taskboard.card.timeout"
    assert diagnostic["card_id"] == "slow"
    assert diagnostic["timeout_seconds"] == 0.25
    assert "timed out" in diagnostic["message"]
    assert task_meta["diagnostics"]["taskboard_card_errors"][0]["code"] == "taskboard.card.timeout"


@pytest.mark.asyncio
async def test_taskboard_tick_timeout_does_not_cancel_running_cards(tmp_path):
    agent = _create_taskboard_slow_card_agent("execution-taskboard-tick-timeout-does-not-cancel").use_task_workspace(
        tmp_path / "task_workspace"
    )

    execution = agent.create_task(
        goal="Run a slow TaskBoard card without tick-level cancellation.",
        success_criteria=["The slow card is allowed to finish under its card timeout."],
        execution="taskboard",
        max_iterations=1,
        options={
            "request_timeout_seconds": 5.0,
            "agent_task": {
                "taskboard_tick_timeout_seconds": 0.05,
                "taskboard_card_timeout_seconds": 1.5,
            },
        },
    )

    result = await execution.async_get_full_data()
    revision = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)["revision"]
    slow_result = revision["card_results"]["slow"]

    assert result["status"] == "completed"
    assert slow_result["status"] == "completed"
    assert slow_result["preview"]["answer"] == "slow card eventually completed"
    assert not any(
        isinstance(diagnostic, dict) and diagnostic.get("code") == "taskboard.tick.card_interrupted"
        for diagnostic in revision.get("diagnostics", [])
    )


@pytest.mark.asyncio
async def test_taskboard_control_no_progress_timeout_returns_structured_card_failure(tmp_path):
    agent = _create_taskboard_control_stall_agent("execution-taskboard-control-no-progress-timeout").use_task_workspace(
        tmp_path / "task_workspace"
    )
    idle_timeout_seconds = 0.8

    execution = agent.create_task(
        goal="Run a stalled TaskBoard control card.",
        success_criteria=["The control card reports a no-progress failure instead of heartbeating forever."],
        execution="taskboard",
        max_iterations=1,
        limits={"max_no_progress_seconds": idle_timeout_seconds},
        options={"request_timeout_seconds": 5.0, "agent_task": {"taskboard_card_max_attempts": 1}},
    )

    started_at = asyncio.get_running_loop().time()
    result = await execution.async_get_full_data()
    elapsed = asyncio.get_running_loop().time() - started_at
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    card_result = taskboard["revision"]["card_results"]["control-stall"]
    diagnostic = card_result["diagnostics"][0]

    assert elapsed < 2.5
    assert result["status"] == "error"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert card_result["status"] == "failed"
    assert diagnostic["code"] == "taskboard.card.timeout"
    assert diagnostic["card_id"] == "control-stall"
    assert f"max_no_progress_seconds={idle_timeout_seconds}" in diagnostic["message"]


@pytest.mark.asyncio
async def test_taskboard_agent_card_data_wait_uses_no_progress_timeout(tmp_path):
    agent = _create_agent("execution-taskboard-agent-card-data-no-progress").use_task_workspace(
        tmp_path / "task_workspace"
    )
    task = AgentTask(
        agent,
        goal="Run a stalled TaskBoard agent card data wait.",
        success_criteria=["The data wait reports no progress instead of waiting forever."],
        execution="taskboard",
        limits={"max_no_progress_seconds": 0.05},
    )

    async def never_finishes() -> None:
        await asyncio.sleep(5)

    started_at = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError, match="TaskBoard card 'agent-card' data request made no progress"):
        await task._await_taskboard_card_execution(
            never_finishes(),
            card_id="agent-card",
            stage="data",
        )
    elapsed = asyncio.get_running_loop().time() - started_at

    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_taskboard_card_transient_timeout_retries_and_completes(tmp_path):
    agent = _create_taskboard_retry_card_agent("execution-taskboard-card-retry").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.create_task(
        goal="Run a board card that should recover after one transient timeout.",
        success_criteria=["The board records retry evidence and still returns a final result."],
        execution="taskboard",
        max_iterations=2,
        options={
            "request_timeout_seconds": 5.0,
            "agent_task": {
                "taskboard_card_timeout_seconds": 1.0,
                "taskboard_card_max_attempts": 2,
            },
        },
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()
    task_meta = meta["logs"]["route_logs"]["agent_task"]
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    retry_result = taskboard["revision"]["card_results"]["retry"]

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["final_result"] == "taskboard retry accepted result"
    assert retry_result["status"] == "completed"
    assert retry_result["metadata"]["attempt_index"] == 2
    assert retry_result["metadata"]["max_attempts"] == 2
    assert task_meta["diagnostics"]["taskboard_card_retries"][0]["code"] == "taskboard.card.timeout"
    assert any(item.path == "agent_task.taskboard.card.retry.execution.retry" for item in stream_items)


@pytest.mark.asyncio
async def test_taskboard_action_card_stops_after_stall_without_semantic_override(tmp_path):
    agent = _create_taskboard_action_post_execution_planning_stall_agent(
        "execution-taskboard-card-partial-evidence-stall"
    ).use_task_workspace(tmp_path / "task_workspace")

    @agent.action_func
    def probe_action() -> dict[str, str]:
        return {"status": "ok"}

    execution = agent.create_task(
        goal="Run a board card that stalls after one action.",
        success_criteria=["Partial action evidence remains inspectable."],
        execution="taskboard",
        max_iterations=1,
        limits={"max_no_progress_seconds": 0.5},
        options={"agent_task": {"taskboard_card_max_attempts": 1}},
    ).use_actions(["probe_action"])

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    result = await execution.async_get_full_data()
    taskboard = cast(dict[str, Any], cast(AgentTask, execution.task_record)._terminal_taskboard_state)
    partial_result = taskboard["revision"]["card_results"]["partial"]
    diagnostics = partial_result["diagnostics"]
    evidence_summaries = [
        item.get("evidence_summary") for item in diagnostics if isinstance(item, dict) and item.get("evidence_summary")
    ]

    assert result["status"] == "blocked", json.dumps(result, ensure_ascii=False, default=str)
    assert partial_result["status"] == "completed"
    assert MockTaskBoardActionPostExecutionPlanningStallRequester.action_planning_calls == 1
    assert evidence_summaries
    first_evidence_summary = evidence_summaries[0]
    assert isinstance(first_evidence_summary, dict)
    assert first_evidence_summary["action_ids"] == ["probe_action"]
    assert first_evidence_summary["action_statuses"]["probe_action"] in {"success", "succeeded"}
    action_events = [item for item in stream_items if item.path.startswith("agent_task.action.")]
    assert {item.path for item in action_events} >= {
        "agent_task.action.started",
        "agent_task.action.completed",
    }
    completed = next(item for item in action_events if item.path == "agent_task.action.completed")
    assert completed.value["action_id"] == "probe_action"
    assert completed.value["origin"] == "taskboard_card"
    assert completed.value["card_id"] == "partial"
    assert (completed.meta or {})["stream_kind"] == "action_observation"
    assert (completed.meta or {})["card_id"] == "partial"


@pytest.mark.asyncio
async def test_execution_first_chain_from_goal_accepts_skills_input_and_stream(tmp_path):
    skill_pack = _install_site_skill(tmp_path)
    agent = _create_goal_pursuit_agent("execution-first-goal-chain").use_task_workspace(tmp_path / "task_workspace")

    execution = (
        agent.goal("Build the site.", success_criteria=["The runnable page exists."])
        .use_skills(str(skill_pack), auto_allow=True)
        .input("Use the supplied product facts.")
        .effort("low")
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    meta = await execution.async_get_meta()

    assert type(execution).__name__ == "AgentExecution"
    assert execution.goal_items == ["Build the site."]
    assert execution.success_criteria_items == ["The runnable page exists."]
    assert execution.prompt_snapshot["input"] == "Use the supplied product facts."
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["effective_options"]["effort_strategy"]["reflection_density"] == "final"
    assert "max_iterations" not in meta["effective_options"]["effort_strategy"]
    assert any(item.path == "agent_task.phase.configured" for item in stream_items)
    assert any(item.path == "agent_task.phase.terminal" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "child_execution" for item in stream_items)


def test_goal_alias_and_detailed_effort_strategy_are_normalized(tmp_path):
    agent = _create_agent("execution-goals-effort-alias").use_task_workspace(tmp_path / "task_workspace")

    execution = agent.goals(
        ["Build the site.", "Publish a launch checklist."],
        success_criteria=["The runnable page exists."],
    ).effort(
        "high",
        budget={
            "iteration_limit": 4,
            "model_call_limit": 8,
            "wall_time_seconds": 90,
            "no_progress_seconds": 30,
        },
        planning={"depth": "expanded", "max_plan_items": 8},
        verification={"strictness": "strict"},
        replan={"policy": "on_verification_failure", "limit": 2},
        progress={"detail": "phase"},
    )

    effort_strategy = execution.effective_options["effort_strategy"]
    assert execution.goal_items == ["Build the site.", "Publish a launch checklist."]
    assert execution.success_criteria_items == ["The runnable page exists."]
    assert effort_strategy["name"] == "high"
    assert effort_strategy["budget"]["iteration_limit"] == 4
    assert effort_strategy["budget"]["model_call_limit"] == 8
    assert effort_strategy["budget"]["wall_time_seconds"] == 90.0
    assert effort_strategy["budget"]["no_progress_seconds"] == 30.0
    assert "max_iterations" not in effort_strategy
    assert "max_model_requests" not in effort_strategy
    assert "max_seconds" not in effort_strategy
    assert effort_strategy["planning_depth"] == "expanded"
    assert effort_strategy["verifier_strength"] == "strict"
    assert effort_strategy["planning"]["max_plan_items"] == 8
    assert effort_strategy["replan"]["limit"] == 2
    assert effort_strategy["progress"]["detail"] == "phase"
    assert execution.limits["max_model_requests"] is None
    assert execution.limits["max_seconds"] is None
    assert execution.limits["max_no_progress_seconds"] is None
    assert execution.effective_options["execution"]["limits"]["max_model_requests"] is None

    explicit_limits = agent.create_execution(limits={"max_model_requests": 2}).effort(
        "high", budget={"model_call_limit": 8}
    )
    assert explicit_limits.limits["max_model_requests"] == 2

    mutable_effort_limits = agent.create_execution().effort("medium", budget={"model_call_limit": 4})
    mutable_effort_limits.effort("high", budget={"model_call_limit": 6})
    assert mutable_effort_limits.limits["max_model_requests"] is None
    assert mutable_effort_limits.effective_options["effort_strategy"]["budget"]["model_call_limit"] == 6
    mutable_effort_limits.effort("low")
    assert mutable_effort_limits.limits["max_model_requests"] is None

    legacy_aliases = agent.create_execution().effort(
        "high",
        max_iterations=4,
        max_model_requests=8,
        max_seconds=90,
        max_no_progress_seconds=30,
    )
    legacy_strategy = legacy_aliases.effective_options["effort_strategy"]
    assert legacy_strategy["budget"]["iteration_limit"] == 4
    assert legacy_strategy["budget"]["model_call_limit"] == 8
    assert legacy_strategy["budget"]["wall_time_seconds"] == 90.0
    assert legacy_strategy["budget"]["no_progress_seconds"] == 30.0
    assert "max_iterations" not in legacy_strategy
    assert "max_model_requests" not in legacy_strategy
    assert legacy_aliases.limits["max_model_requests"] is None


@pytest.mark.asyncio
async def test_goal_pursuit_effort_iteration_limit_is_soft_strategy_metadata(tmp_path):
    agent = _create_goal_pursuit_agent("execution-detailed-effort-task").use_task_workspace(tmp_path / "task_workspace")
    execution = agent.goal("Build the site.", success_criteria=["The runnable page exists."]).effort(
        "low", budget={"iteration_limit": 2}
    )

    await execution.async_start()
    meta = await execution.async_get_meta()

    assert meta["logs"]["route_logs"]["agent_task"]["max_iterations"] is None
    assert meta["effective_options"]["effort_strategy"]["budget"]["iteration_limit"] == 2
    assert "effort.max_iterations" not in meta["consumed_options"]


@pytest.mark.asyncio
async def test_goal_pursuit_wall_clock_budget_is_owned_by_agent_task(tmp_path, monkeypatch):
    from importlib import import_module

    # Arrange the tested deadline at the plan boundary, not during unrelated
    # context setup. The actual asyncio timeout and Task owner remain real.
    monotonic_origin, wall_origin = time.monotonic(), time.time()
    logical_elapsed = 0.0
    budget = 30.0
    clock = SimpleNamespace(
        monotonic=lambda: monotonic_origin + logical_elapsed,
        time=lambda: wall_origin + logical_elapsed,
    )
    for module_name in (
        "agently.core.application.AgentExecution.Context",
        "agently.builtins.plugins.AgentExecution.modules.limits",
        "agently.builtins.plugins.AgentExecution.long_task.RuntimeControl",
    ):
        monkeypatch.setattr(import_module(module_name), "time", clock)
    agent = _create_goal_pursuit_agent("execution-task-route-deadline-owner").use_task_workspace(tmp_path / "task_workspace")
    execution = (
        agent.create_execution(limits={"max_seconds": budget, "max_no_progress_seconds": 5})
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .strategy("flat")
    )

    started, settled = asyncio.Event(), asyncio.Event()
    remaining_at_plan = []

    async def pending_plan():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    def slow_request_plan(_iteration_index, _context_pack):
        nonlocal logical_elapsed
        logical_elapsed = budget - 0.2
        task = cast(AgentTask, execution.task_record)
        remaining_at_plan.append(task._task_deadline_remaining())
        return pending_plan()

    cast(Any, execution)._agent_task_step_overrides = {"_request_plan": slow_request_plan}

    result = await asyncio.wait_for(execution.async_get_full_data(), timeout=5)
    meta = await execution.async_get_meta()

    assert result["status"] == "timed_out"
    assert execution.status == "timed_out"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["close_snapshot"]["task"]["status"] == "timed_out"
    assert "plan stage" in result["reason"]
    assert remaining_at_plan == [pytest.approx(0.2)]
    assert started.is_set()
    assert settled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_exit", ["cancellation", "timeout"])
async def test_routed_parent_joins_agent_task_before_releasing_transferred_action_scope(
    tmp_path,
    monkeypatch,
    parent_exit: str,
):
    agent = _create_agent(f"execution-routed-child-join-{parent_exit}").use_task_workspace(
        tmp_path / parent_exit
    )
    action_id = f"late_child_action_{parent_exit}"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a late large artifact only if the routed child outlives its parent.",
        kwargs={},
        func=lambda: {"body": "l" * (1024 * 1024)},
    )
    execution = agent.create_task(
        goal="Prove routed child shutdown is joined before parent terminal cleanup.",
        success_criteria=["No child work runs after parent terminal cleanup."],
        execution="flat",
        max_iterations=1,
    )
    child_started = asyncio.Event()
    allow_late_work = asyncio.Event()
    late_work_created = asyncio.Event()
    release_scopes: list[dict[str, str]] = []
    original_release_scope_except = agent.action._release_artifact_scope_except

    def capture_release_scope(
        artifact_scope: dict[str, str],
        *,
        retained_artifact_ids: set[str],
    ) -> int:
        release_scopes.append(dict(artifact_scope))
        return original_release_scope_except(
            artifact_scope,
            retained_artifact_ids=retained_artifact_ids,
        )

    monkeypatch.setattr(agent.action, "_release_artifact_scope_except", capture_release_scope)

    async def delayed_plan(_iteration_index: int, _context_pack: Any) -> dict[str, Any]:
        child_started.set()
        await allow_late_work.wait()
        return {
            "execution_shape": "direct",
            "step_instruction": "Attempt late child work.",
            "expected_evidence": "No late work is created.",
            "rationale": "This continuation must be cancelled with the routed parent.",
        }

    async def create_late_work(
        _iteration_index: int,
        _plan: dict[str, Any],
        _context_pack: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        task = cast(AgentTask, execution.task_record)
        await agent.action.async_execute_action(
            action_id,
            {},
            artifact_scope={"kind": "agent_task", "id": task.id},
        )
        await task.record_store.put(
            {"late": True},
            collection="observations",
            kind="late_child_process",
        )
        late_work_created.set()
        return (
            {
                "step_result": "late work created",
                "evidence": ["late work"],
                "remaining_work": [],
                "ready_for_final_verification": True,
            },
            {
                "execution_id": f"late-child-{parent_exit}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def complete_verification(_iteration_index: int, **_kwargs: Any) -> dict[str, Any]:
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "late child work completed",
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result": "late child result",
        }

    cast(Any, execution)._agent_task_step_overrides = {
        "_request_plan": delayed_plan,
        "_execute_step": create_late_work,
        "_request_verification": complete_verification,
    }

    run = asyncio.create_task(execution.async_get_full_data())
    await asyncio.wait_for(child_started.wait(), timeout=3)
    if parent_exit == "cancellation":
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    else:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(run, timeout=0.1)

    task = cast(AgentTask, execution.task_record)
    task_scope = {"kind": "agent_task", "id": task.id}
    assert execution.status == "cancelled"
    assert task._action_artifact_scope_transferred_to_execution_id == execution.id
    assert release_scopes.count(task_scope) == 1

    allow_late_work.set()
    await asyncio.sleep(0.2)

    assert late_work_created.is_set() is False
    assert task._completed is True
    assert all(
        scope != ("agent_task", task.id)
        for scope in agent.action._artifact_manager._artifact_scopes.values()
    )
    assert await task.record_store.search(filters={"kind": "late_child_process"}) == []


def test_execution_first_chain_allows_capabilities_before_goal(tmp_path):
    skill_pack = _install_site_skill(tmp_path)
    agent = _create_agent("execution-first-skill-first").use_task_workspace(tmp_path / "task_workspace")

    execution = (
        agent.use_skills(str(skill_pack), auto_allow=True)
        .goal("Build the site.", success_criteria=["The runnable page exists."])
        .input("Use the supplied product facts.")
    )

    assert type(execution).__name__ == "AgentExecution"
    assert execution.goal_items == ["Build the site."]
    assert execution.success_criteria_items == ["The runnable page exists."]
    assert execution.prompt_snapshot["input"] == "Use the supplied product facts."
    assert agent.request.prompt.get(inherit=False) == {}
    assert agent._collect_skill_selectors(skills=None, mode="model_decision") == []
    assert execution.local_skill_selectors


def test_goal_accepts_multiple_goals_and_optional_success_criteria():
    agent = _create_agent("execution-first-multiple-goals")

    execution = agent.goal(
        ["Build the site.", "Include pricing and contact sections."],
        ["The final site includes both sections."],
    )

    assert type(execution).__name__ == "AgentExecution"
    assert execution.goal_items == ["Build the site.", "Include pricing and contact sections."]
    assert execution.success_criteria_items == ["The final site includes both sections."]


@pytest.mark.asyncio
async def test_execution_first_chain_allows_goal_after_prompt_output(tmp_path):
    agent = _create_goal_pursuit_agent("execution-first-goal-after-prompt").use_task_workspace(tmp_path / "task_workspace")
    execution = (
        agent.input("Use these facts.")
        .output({"summary": (str, "summary", True)}, format="json")
        .goal("Write the final summary.", success_criteria=["The final summary is returned."])
    )

    data = await execution.async_get_full_data()
    meta = await execution.async_get_meta()

    assert data["accepted"] is True
    assert meta["route"]["selected_route"] == "agent_task"
    assert execution.prompt_snapshot["input"] == "Use these facts."
    assert execution.prompt_snapshot["output"]["summary"][0] is str
    assert execution.goal_items == ["Write the final summary."]


@pytest.mark.asyncio
async def test_allow_create_task_false_blocks_goal_pursuit(tmp_path):
    """ISSUE-007: allow_create_task=False is an enforced limit, not a no-op."""
    agent = _create_goal_pursuit_agent("execution-no-create-task").use_task_workspace(tmp_path / "task_workspace")
    execution = agent.goal("Build the site.", success_criteria=["The runnable page exists."]).create_execution(
        limits={"allow_create_task": False}
    )

    result = await execution.async_get_full_data()
    meta = await execution.async_get_meta()

    assert result["status"] in {"blocked", "max_iterations"}
    assert result["accepted"] is False
    assert "final_response" in result
    assert "No complete final deliverable was accepted" in result["final_response"]
    assert await execution.async_get_text() == result["final_response"]
    assert meta["route"]["selected_route"] == "agent_task"


@pytest.mark.asyncio
async def test_route_policy_block_and_deterministic_fallback():
    """ISSUE-017: on_violation='block' surfaces a blocked route; fallback is deterministic."""
    from types import SimpleNamespace
    from agently.builtins.plugins.AgentExecution.modules.routing import (
        HybridRoutePlanner,
    )

    blocked_exec = SimpleNamespace(
        options={"route_policy": {"allowed_routes": ["removed_route"], "on_violation": "block"}},
        effective_options={},
        local_skill_selectors=[],
        local_skills_pack_selectors=[],
        local_action_ids=[],
    )
    planner = HybridRoutePlanner(cast(Any, None), execution=blocked_exec)
    assert planner.allowed_routes() == {"removed_route"}
    assert planner.route_allowed("model_request") is False
    assert planner.on_violation() == "block"
    route, meta = await planner.select_route()
    assert route == "route_policy_blocked"
    assert meta["selected_by"] == "route_policy_violation"

    # Default on_violation is a fallback to model_request (backward compatible).
    fallback_exec = SimpleNamespace(
        options={"route_policy": {"allowed_routes": ["removed_route"]}},
        effective_options={},
        local_skill_selectors=[],
        local_skills_pack_selectors=[],
        local_action_ids=[],
    )
    fallback_planner = HybridRoutePlanner(cast(Any, None), execution=fallback_exec)
    route, meta = await fallback_planner.select_route()
    assert route == "model_request"
    assert meta["selected_by"] == "route_policy_fallback"


def test_agent_execution_context_enforces_nesting_budget():
    """ISSUE-007: max_nested_agent_steps is an enforced recursion control."""
    from agently.core.application.AgentExecution import AgentExecutionContext, AgentExecutionLimitExceeded

    root = AgentExecutionContext(
        execution_id="root",
        lineage={},
        limits={"max_nested_agent_steps": 1},
        nesting_depth=0,
        nesting_budget=1,
    )
    root.raise_if_nesting_exceeded()  # depth 0 within budget 1

    nested_ok = AgentExecutionContext(execution_id="nested-1", lineage={}, limits={}, nesting_depth=1, nesting_budget=1)
    nested_ok.raise_if_nesting_exceeded()  # depth 1 within budget 1

    nested_over = AgentExecutionContext(
        execution_id="nested-2", lineage={}, limits={}, nesting_depth=2, nesting_budget=1
    )
    with pytest.raises(AgentExecutionLimitExceeded) as raised:
        nested_over.raise_if_nesting_exceeded()
    assert raised.value.limit_name == "max_nested_agent_steps"


def test_nested_agent_executions_share_root_model_request_budget(tmp_path):
    from agently.core.application.AgentExecution import (
        AgentExecutionContext,
        AgentExecutionLimitExceeded,
    )
    from agently.core.runtime.RuntimeContext import bind_runtime_context

    root = AgentExecutionContext(
        execution_id="root-budget",
        lineage={},
        limits={"max_model_requests": 2},
    )
    agent = _create_agent("nested-shared-root-budget").use_task_workspace(
        tmp_path / "workspace"
    )
    with bind_runtime_context(agent_execution_context=root):
        first = agent.create_execution(limits={"max_model_requests": 2})
        second = agent.create_execution(limits={"max_model_requests": 2})

    first.execution_context.consume_model_request(response_id="first")
    second.execution_context.consume_model_request(response_id="second")
    with pytest.raises(AgentExecutionLimitExceeded) as raised:
        second.execution_context.consume_model_request(response_id="third")

    assert raised.value.limit_name == "max_model_requests"
    assert raised.value.limit_value == 2
    assert raised.value.used == 2
    assert root.diagnostics()["budget"]["model_requests_used"] == 2
    assert first.execution_context.diagnostics()["budget"]["model_requests_used"] == 1
    assert second.execution_context.diagnostics()["budget"]["model_requests_used"] == 1
    assert root.diagnostics()["limit_events"][0]["response_id"] == "third"


def test_nested_agent_execution_local_budget_does_not_constrain_sibling(tmp_path):
    from agently.core.application.AgentExecution import (
        AgentExecutionContext,
        AgentExecutionLimitExceeded,
    )
    from agently.core.runtime.RuntimeContext import bind_runtime_context

    root = AgentExecutionContext(
        execution_id="root-budget",
        lineage={},
        limits={"max_model_requests": 3},
    )
    agent = _create_agent("nested-independent-child-budget").use_task_workspace(
        tmp_path / "workspace"
    )
    with bind_runtime_context(agent_execution_context=root):
        constrained = agent.create_execution(limits={"max_model_requests": 1})
        sibling = agent.create_execution(limits={"max_model_requests": 2})

    constrained.execution_context.consume_model_request(response_id="child-one")
    with pytest.raises(AgentExecutionLimitExceeded) as raised:
        constrained.execution_context.consume_model_request(response_id="child-two")
    assert raised.value.limit_value == 1

    sibling.execution_context.consume_model_request(response_id="sibling-one")
    sibling.execution_context.consume_model_request(response_id="sibling-two")
    assert root.diagnostics()["budget"]["model_requests_used"] == 3
    assert sibling.execution_context.diagnostics()["budget"]["model_requests_used"] == 2


@pytest.mark.asyncio
async def test_agent_quick_prompt_returns_execution_and_result_facade():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("quick-prompt-execution-result")

    execution = agent.input("quick prompt").output({"answer": (str, "answer", True)}, format="json")
    result = execution.get_result()
    data = await result.async_get_data()
    meta = await result.async_get_meta()

    assert type(execution).__name__ == "AgentExecution"
    assert isinstance(result, AgentExecutionResult)
    assert data["answer"] == "ok-1"
    assert meta["execution_id"] == execution.id
    assert agent.request.prompt.get(inherit=False) == {}


def test_agent_define_parameter_and_builder_forms_write_definition_state():
    agent = _create_agent("agent-define-contract")

    builder = agent.define(
        model="mock-model",
        prompt={"system": "Base policy"},
        settings={"runtime.session_id": "define-session"},
        policy={"handler": "default"},
    )
    assert builder.info({"tenant": "demo"}) is builder

    prompt_snapshot = agent.agent_prompt.get(inherit=False)
    assert isinstance(prompt_snapshot, dict)
    assert prompt_snapshot["system"] == "Base policy"
    assert prompt_snapshot["info"] == {"tenant": "demo"}
    assert agent.request.prompt.get(inherit=False) == {}
    assert agent.settings.get("runtime.session_id") == "define-session"
    assert agent.settings.get("policy_approval.handler") == "default"

    builder_agent = _create_agent("agent-define-builder-contract")
    builder_2 = builder_agent.define()
    assert builder_2.role("Support assistant") is builder_2
    assert builder_agent.agent_prompt.get("system.your_role", inherit=False) == "Support assistant"
    assert builder_agent.request.prompt.get(inherit=False) == {}


def test_create_task_and_task_loop_return_strategy_execution_drafts(tmp_path):
    agent = _create_agent("task-strategy-drafts")

    task_execution = agent.create_task(
        task_id="task-draft",
        goal="Draft a task",
        success_criteria=["The task is drafted."],
        task_workspace=tmp_path / "task",
    )
    loop_execution = agent.create_task_loop(
        task_id="loop-draft",
        goal="Loop a task",
        success_criteria=["The loop is drafted."],
        task_workspace=tmp_path / "loop",
    )

    assert type(task_execution).__name__ == "AgentExecution"
    assert task_execution.strategy_name == "task"
    assert task_execution.task_options["task_id"] == "task-draft"
    assert type(loop_execution).__name__ == "AgentExecution"
    assert loop_execution.strategy_name == "task_loop"


@pytest.mark.asyncio
async def test_agent_execution_direct_strategy_forces_model_request_route(tmp_path):
    agent = _create_action_agent("direct-strategy-route").use_task_workspace(
        tmp_path / "task_workspace", mode="read_write"
    )
    agent.set_action_loop(max_rounds=1, timeout=5)
    agent.enable_shell(commands=["echo allowed"], action_id="echo_cli", sandbox="trusted_local")

    execution = (
        agent.goal("Use the echo action without creating an AgentTask.", ["The echo action result is returned."])
        .input("use echo action")
        .output({"answer": (str, "answer", True)}, format="json")
        .strategy("direct")
    )

    route, route_meta = await execution.select_route()

    assert route == "model_request"
    assert route_meta["selected_by"] == "execution_strategy"
    assert route_meta["strategy"] == "direct"
    assert route_meta["with_actions"] is True
    assert execution.strategy_name == "direct"
    assert execution.is_task_strategy() is False
    assert "execution" not in execution.task_options

    configured = (
        agent.create_execution(options={"strategy": "direct"})
        .input("answer directly")
        .output({"answer": (str, "answer", True)}, format="json")
    )
    configured_route, configured_meta = await configured.select_route()
    assert configured_route == "model_request"
    assert configured_meta["strategy"] == "direct"
    assert configured.strategy_name == "direct"


@pytest.mark.asyncio
async def test_agent_execution_auto_strategy_does_not_force_agent_task_without_task_signals():
    agent = _create_agent("auto-strategy-direct-route")
    execution = (
        agent.input("answer directly")
        .output({"answer": (str, "answer", True)}, format="json")
        .strategy("auto")
    )

    route, _route_meta = await execution.select_route()

    assert route == "model_request"
    assert execution.strategy_name == "auto"
    assert execution.is_task_strategy() is False
    assert "execution" not in execution.task_options


def test_agent_execution_flat_and_taskboard_strategies_force_agent_task_execution_shape():
    agent = _create_agent("execution-shape-strategy")

    for strategy_name in ("flat", "taskboard"):
        execution = agent.create_execution().strategy(strategy_name)

        assert execution.strategy_name == strategy_name
        assert execution.is_task_strategy() is True
        assert execution.task_options["execution"] == strategy_name
        assert execution.task_strategy_options()["execution"] == strategy_name

    auto_flat = agent.create_execution().strategy("auto", execution="flat")
    assert auto_flat.strategy_name == "auto"
    assert auto_flat.is_task_strategy() is True
    assert auto_flat.task_options["execution"] == "flat"


def test_removed_transitional_surfaces_are_not_available():
    agent = _create_agent("removed-transitional-surfaces")
    execution = agent.create_execution()

    assert not hasattr(agent, "create_turn")
    assert not hasattr(agent, "set_turn_prompt")
    assert not hasattr(agent, "set_request_prompt")
    assert not hasattr(agent, "remove_request_prompt")
    assert not hasattr(agent, "success_criteria")
    assert not hasattr(agent, "success_standards")
    assert hasattr(agent, "goals")
    assert hasattr(agent, "remove_execution_prompt")
    assert not hasattr(execution, "success_criteria")
    assert not hasattr(execution, "success_standards")
    assert hasattr(execution, "goals")
    assert not hasattr(execution, "remove_request_prompt")
    assert hasattr(execution, "remove_execution_prompt")


@pytest.mark.asyncio
async def test_same_agent_quick_prompt_executions_are_request_scoped():
    MockAgentExecutionRequester.requests = []
    agent = _create_execution_isolation_agent("same-agent-execution-isolation")
    agent.system("shared policy", always=True)

    results = await asyncio.gather(
        agent.input("request-A").output({"answer": (str, "answer", True)}, format="json").async_start(),
        agent.input("request-B").output({"answer": (str, "answer", True)}, format="json").async_start(),
        agent.input("request-C").output({"answer": (str, "answer", True)}, format="json").async_start(),
    )

    assert [result["answer"] for result in results] == ["request-A", "request-B", "request-C"]
    assert [result["system"] for result in results] == ["shared policy", "shared policy", "shared policy"]
    assert MockAgentExecutionIsolationRequester.max_active_requests == 3
    assert agent.request.prompt.get(inherit=False) == {}
    request_payloads = [json.loads(item) for item in MockAgentExecutionRequester.requests]
    assert [payload["input"] for payload in request_payloads] == ["request-A", "request-B", "request-C"]


@pytest.mark.asyncio
async def test_agent_execution_default_stream_meta_uses_execution_id_and_lineage():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("default-execution-stream")
    execution = (
        agent.input("classify this ticket").output({"answer": (str, "answer", True)}, format="json").create_execution()
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant") if item.is_complete]
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["answer"] == "ok-1"
    assert meta["limits"]["max_model_requests"] is None
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 1
    assert any(item.path == "route.selected" for item in stream_items)
    assert all(item.meta["execution_id"] == meta["execution_id"] for item in stream_items if item.meta)
    assert all("execution_mode" not in item.meta for item in stream_items if item.meta)


@pytest.mark.asyncio
async def test_agent_execution_context_progress_is_published_to_stream():
    agent = _create_agent("execution-progress-stream")
    execution = agent.input("progress probe").create_execution()

    execution.execution_context.record_progress(
        stage="action_runtime",
        status="started",
        event_type="action_runtime.started",
        meta={"action_id": "web_search"},
    )
    await asyncio.sleep(0)

    progress_items = [item for item in execution.stream.items if item.path == "runtime.progress.action_runtime.started"]
    assert progress_items
    item = progress_items[-1]
    assert item.source == "agent_execution"
    assert (item.meta or {})["stream_kind"] == "runtime_progress"
    assert item.value["stage"] == "action_runtime"
    assert item.value["status"] == "started"
    assert item.value["meta"]["action_id"] == "web_search"


@pytest.mark.asyncio
async def test_agent_execution_rejects_removed_mode_argument():
    agent = _create_agent("removed-mode-argument")
    removed_mode_kwargs: dict[str, Any] = {"mode": "removed"}

    with pytest.raises(TypeError):
        (
            agent.input("legacy mode argument")
            .output({"answer": (str, "answer", True)}, format="json")
            .create_execution(**removed_mode_kwargs)
        )


@pytest.mark.asyncio
async def test_agent_execution_select_route_is_reused_by_start():
    agent = _create_agent("route-reuse")
    execution = agent.input("route reuse").output({"answer": (str, "answer", True)}, format="json").create_execution()

    first_route = await execution.select_route()
    second_route = await execution.select_route()
    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert first_route == second_route
    assert data["answer"] == "ok-1"
    assert meta["route"]["selected_route"] == first_route[0]
    assert meta["route"]["reusable"] is True


@pytest.mark.asyncio
async def test_agent_execution_model_request_exposes_action_logs_and_artifacts(tmp_path):
    agent = _create_action_agent("action-log-exposure").use_task_workspace(
        tmp_path / "task_workspace", mode="read_write"
    )
    agent.set_action_loop(max_rounds=2, timeout=5)
    agent.enable_shell(commands=["echo allowed"], action_id="echo_cli", sandbox="trusted_local")
    execution = (
        agent.input("use echo action")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={"task_id": "action-task", "iteration_id": "iter-1", "step_id": "echo"},
            limits={"max_model_requests": None},
        )
    )

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    data = await execution.async_get_full_data()
    meta = await execution.async_get_meta()

    action_items = [item for item in stream_items if item.path == "actions.echo_cli"]
    assert data["answer"] == "used-action"
    assert meta["logs"]["model_response_ids"]
    assert meta["logs"]["action_logs"][0]["action_id"] == "echo_cli"
    assert meta["logs"]["action_logs"][0]["route"] == "model_request"
    assert meta["logs"]["artifact_refs"]
    assert action_items
    assert action_items[0].meta is not None
    assert action_items[0].meta["lineage"]["task_id"] == "action-task"


@pytest.mark.asyncio
async def test_required_skill_create_execution_routes_to_agent_task(tmp_path):
    skill_root = tmp_path / "skill-pack" / "skills" / "task-step"
    _write_skill(skill_root)
    Agently.skills_executor.configure(
        registry_root=tmp_path / "skills-registry",
        allowed_trust_levels=["local"],
    )
    contract = Agently.skills_executor.install_skills(skill_root, trust_level="local", update=True)
    skill_id = str(contract["skill_id"])

    agent = _create_action_agent("required-skill-with-actions").use_task_workspace(
        tmp_path / "task_workspace", mode="read_write"
    )
    agent.set_action_loop(max_rounds=2, timeout=5)
    agent.enable_shell(commands=["echo allowed"], action_id="echo_cli", sandbox="trusted_local")
    agent.require_skills([skill_id], always=True)
    execution = (
        agent.input("Use the required Skill guidance and the echo action.")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(limits={"max_model_requests": None})
    )

    route, route_meta = await execution.select_route()

    assert route == "agent_task"
    assert route_meta["selected_by"] == "execution_strategy"
    assert execution.effective_options["capability_constraints"]["skills"]["required"] == [skill_id]


@pytest.mark.asyncio
async def test_agent_execution_action_scope_filters_action_runtime_boundary():
    agent = _create_scoped_action_agent("action-scope-runtime-boundary")
    agent.set_action_loop(max_rounds=1)

    @agent.action_func
    def allowed_action() -> dict[str, str]:
        return {"status": "allowed"}

    @agent.action_func
    def blocked_action() -> dict[str, str]:
        return {"status": "blocked"}

    execution = (
        agent.create_execution()
        .use_actions(["allowed_action"])
        .input("use the scoped action")
        .output({"answer": (str, "answer", True)}, format="json")
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()
    planning_calls = [
        call for call in MockAgentExecutionRequester.requests if "next_action" in call and "execution_commands" in call
    ]
    action_ids = [item.get("action_id") for item in meta["logs"]["action_logs"]]

    assert data["answer"] == "used scoped action"
    assert planning_calls
    assert "allowed_action" in planning_calls[0]
    assert "blocked_action" not in planning_calls[0]
    # Only real executed actions enter action_logs; the action-loop boundary signal
    # (max_rounds) is a framework diagnostic and must not leak in as an executed action.
    assert action_ids == ["allowed_action"]
    assert "action_loop" not in action_ids
    boundary_diagnostics = [
        entry.get("action_id") for entry in meta["logs"].get("action_loop_diagnostics", [])
    ]
    assert "action_loop" in boundary_diagnostics
    assert meta["diagnostics"].get("action_scope", {}).get("allowed_action_ids") == ["allowed_action"]


@pytest.mark.asyncio
async def test_required_action_blocks_when_model_skips_required_evidence():
    agent = _create_action_agent("required-action-missing")

    @agent.action_func
    def required_lookup() -> dict[str, str]:
        return {"status": "looked-up"}

    execution = (
        agent.require_actions("required_lookup")
        .input("answer directly without using the required action")
        .output({"answer": (str, "answer", True)}, format="json")
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["status"] == "blocked"
    assert data["accepted"] is False
    assert meta["status"] == "blocked"
    assert meta["route"]["selected_by"] == "required_capability"
    assert meta["effective_options"]["capability_constraints"]["actions"]["required"] == ["required_lookup"]
    required_diagnostics = meta["diagnostics"]["required_capabilities"][0]
    assert required_diagnostics["missing_actions"] == ["required_lookup"]


@pytest.mark.asyncio
async def test_agent_execution_plain_text_model_request_streams_model_delta():
    agent = _create_action_agent("plain-text-stream")
    execution = agent.input("plain text route").create_execution()

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    meta = await execution.async_get_meta()

    assert any(item.path == "model.delta" and item.event_type == "delta" for item in stream_items)
    assert any(item.path == "model.text" for item in stream_items)
    assert meta["route"]["selected_route"] == "model_request"

    delta_chunks = [chunk async for chunk in execution.get_async_generator(type="delta")]
    assert delta_chunks
    assert all(isinstance(chunk, str) for chunk in delta_chunks)

    default_chunks = [chunk async for chunk in execution.get_async_generator()]
    assert default_chunks == delta_chunks


@pytest.mark.asyncio
async def test_agent_execution_delta_generator_projects_retry_status_boundary():
    agent = _create_retry_status_agent("plain-text-retry-status-stream")
    execution = agent.input("plain text route with transient retry").create_execution()

    stream_items = [item async for item in execution.get_async_generator(type="instant")]
    delta_text = "".join([chunk async for chunk in execution.get_async_generator(type="delta")])

    status_items = [item for item in stream_items if item.path == "$status"]
    assert status_items
    assert status_items[0].value["retry"] is True
    assert status_items[0].value["attempt_index"] == 1
    assert "partial attempt" in delta_text
    assert "<$retry>transient provider disconnect</$retry>" in delta_text
    assert delta_text.index("partial attempt") < delta_text.index("<$retry>")
    assert delta_text.index("<$retry>") < delta_text.index("replacement")


@pytest.mark.asyncio
async def test_agent_execution_bounded_step_meta_lineage_and_limit_success():
    MockAgentExecutionRequester.requests = []
    agent = _create_agent("task-step-success")
    execution = (
        agent.input("produce one bounded answer")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={"task_id": "task-1", "iteration_id": "iter-1", "step_id": "draft"},
            limits={"max_model_requests": 1},
        )
    )

    data = await execution.async_get_data()
    meta = await execution.async_get_meta()

    assert data["answer"] == "ok-1"
    assert meta["lineage"]["task_id"] == "task-1"
    assert meta["limits"]["max_model_requests"] == 1
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 1


@pytest.mark.asyncio
async def test_agent_execution_bounded_step_blocks_when_direct_model_budget_exceeded():
    agent = _create_agent("task-step-direct-budget")
    execution = (
        agent.input("this should exceed budget before provider call")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={"task_id": "budget-task", "iteration_id": "iter-1", "step_id": "direct"},
            limits={"max_model_requests": 0},
        )
    )

    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()

    meta = await execution.async_get_meta()
    assert meta["status"] == "blocked"
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 0
    assert meta["diagnostics"]["limit_events"][0]["limit_name"] == "max_model_requests"


def test_agent_execution_bounded_step_rejects_dynamic_task_route():
    agent = _create_agent("task-step-dynamic-budget")

    with pytest.raises(ValueError, match=r"Agent\.use_dynamic_task.*independent DAG workflows"):
        agent.use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "task-step-budget-dag",
                "task_schema_version": "task_dag/v1",
                "tasks": [
                    {
                        "id": "first",
                        "kind": "model",
                        "inputs": {"output_schema": {"answer": (str, "answer", True)}},
                    },
                    {
                        "id": "second",
                        "kind": "model",
                        "depends_on": ["first"],
                        "inputs": {"output_schema": {"answer": (str, "answer", True)}},
                    },
                ],
                "semantic_outputs": {"final": "second"},
            },
            timeout=3,
        )

    assert not hasattr(agent, "_dynamic_task_candidates")


@pytest.mark.asyncio
async def test_agent_execution_bounded_step_budget_covers_skill_bound_model_route(tmp_path):
    MockAgentExecutionRequester.requests = []
    skill_root = tmp_path / "skill-pack" / "skills" / "task-step"
    _write_skill(skill_root)
    library = SkillLibrary(tmp_path / "library")
    skill = library.install(skill_root, trust="trusted")
    agent = _create_agent("task-step-skills-budget")
    agent.skill_library = library
    execution = agent.create_execution(
        lineage={"task_id": "budget-task", "iteration_id": "iter-1", "step_id": "skills"},
        limits={"max_model_requests": 0},
    ).input("use the task step skill").use_skills(skill.skill_id, mode="required")

    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()

    meta = await execution.async_get_meta()
    assert meta["status"] == "blocked"
    assert meta["route_plan"]["selected_route"] != "skills"
    assert meta["diagnostics"]["budget"]["model_requests_used"] == 0


@pytest.mark.asyncio
async def test_two_bounded_step_executions_can_be_correlated_as_developer_loop():
    agent = _create_agent("task-step-loop")
    first = (
        agent.input("first step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={"task_id": "loop-task", "iteration_id": "iter-1", "step_id": "first"},
            limits={"max_model_requests": 1},
        )
    )
    first_data = await first.async_get_data()
    first_meta = await first.async_get_meta()

    second = (
        agent.input({"previous": first_data})
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={
                "task_id": "loop-task",
                "iteration_id": "iter-2",
                "step_id": "second",
                "parent_execution_id": first_meta["execution_id"],
            },
            limits={"max_model_requests": 1},
        )
    )

    second_stream = [item async for item in second.get_async_generator(type="instant") if item.is_complete]
    second_meta = await second.async_get_meta()

    assert second_meta["lineage"]["parent_execution_id"] == first_meta["execution_id"]
    assert second_meta["lineage"]["iteration_id"] == "iter-2"
    assert all(item.meta["execution_id"] == second_meta["execution_id"] for item in second_stream if item.meta)
    assert all(item.meta["lineage"]["task_id"] == "loop-task" for item in second_stream if item.meta)


@pytest.mark.asyncio
async def test_fresh_non_active_agent_execution_rejects_process_checkpoint_without_starting(tmp_path):
    agent = _create_agent("task-step-workspace-binding").use_task_workspace(tmp_path / "run")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution = (
        agent.input("workspace-bound step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={
                "task_id": "workspace-task",
                "iteration_id": "iter-1",
                "step_id": "record",
                "scope": {"area": "agent-execution"},
            },
            limits={"max_model_requests": 1},
        )
    )

    with pytest.raises(RuntimeError, match="non-active.*process|process.*non-active"):
        await execution.async_record_data(
            content={"stage": "requested-before-terminal"},
            summary="workspace-bound task-step record",
            checkpoint=True,
        )

    assert execution.status == "created"
    assert execution.record_refs["observations"] == []
    assert execution.record_refs["checkpoints"] == []


@pytest.mark.asyncio
async def test_agent_execution_record_purpose_defaults_to_process(tmp_path):
    agent = _create_agent("task-step-workspace-process-purpose").use_task_workspace(tmp_path / "run")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution = agent.input("record process workspace data").create_execution().strategy("direct")

    with pytest.raises(RuntimeError, match="non-active.*process|process.*non-active"):
        await execution.async_record_data(
            content={"stage": "working"},
            meta={"record_purpose": "audit"},
        )

    assert execution.status == "created"


@pytest.mark.asyncio
async def test_agent_execution_record_purpose_deliverable_is_an_explicit_record_write(tmp_path):
    agent = _create_agent("task-step-workspace-deliverable-purpose").use_task_workspace(tmp_path / "run")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution = agent.input("record deliverable workspace data").create_execution().strategy("direct")
    assert execution.record_store is not None
    record_write = await execution.async_record_data(
        content={"report": "accepted body"},
        purpose="deliverable",
    )

    assert record_write["record"]["meta"]["record_purpose"] == "deliverable"
    assert await execution.record_store.get_data(record_write["record"]) == {"report": "accepted body"}
    record_ref = record_write["record"]
    assert record_ref["source"]["type"] == "agent_execution"
    assert record_ref["meta"]["execution_id"] == execution.id


@pytest.mark.asyncio
async def test_agent_execution_record_purpose_rejects_deliverable_checkpoint_role(tmp_path):
    agent = _create_agent("task-step-workspace-purpose-role-conflict").use_task_workspace(tmp_path / "run")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution = agent.input("reject conflated workspace roles").create_execution().strategy("direct")

    with pytest.raises(ValueError, match="deliverable.*checkpoint|checkpoint.*deliverable"):
        await execution.async_record_data(
            content={"report": "must not become recovery state"},
            purpose="deliverable",
            checkpoint=True,
        )


@pytest.mark.asyncio
async def test_agent_execution_record_purpose_audit_is_explicitly_requested_by_the_call(tmp_path):
    agent = _create_agent("task-step-workspace-audit-purpose").use_task_workspace(tmp_path / "run")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution = agent.input("record explicitly requested audit data").create_execution().strategy("direct")
    record_write = await execution.async_record_data(
        content={"decision": "reviewed"},
        purpose="audit",
    )

    assert record_write["record"]["meta"]["record_purpose"] == "audit"
    assert await execution.record_store.get_data(record_write["record"]) == {"decision": "reviewed"}


@pytest.mark.asyncio
async def test_agent_execution_post_terminal_record_writes_are_purpose_governed(tmp_path):
    agent = _create_agent("task-step-post-terminal-workspace-purpose").use_task_workspace(tmp_path / "run")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution = agent.input("Finish before post-terminal writes.").create_execution().strategy("direct")

    async def fake_route(**_kwargs: Any) -> tuple[str, dict[str, str]]:
        execution.route_info["selected_route"] = "model_request"
        return "model_request", {"reply": "done"}

    execution._async_execute_route = fake_route  # type: ignore[method-assign]
    assert await execution.async_get_data() == {"reply": "done"}

    with pytest.raises(RuntimeError, match="post-terminal.*process|process.*post-terminal"):
        await execution.async_record_data(content={"late": "process"}, purpose="process")
    with pytest.raises(RuntimeError, match="post-terminal.*recovery|recovery.*post-terminal"):
        await execution.async_record_data(content={"late": "recovery"}, purpose="recovery")

    deliverable = await execution.async_record_data(
        content={"late": "deliverable"},
        purpose="deliverable",
    )
    assert await execution.record_store.get_data(deliverable["record"]) == {"late": "deliverable"}


@pytest.mark.asyncio
async def test_agent_execution_record_write_uses_execution_scoped_lazy_default_record_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _create_agent("task-step-workspace-missing")
    record_store = agent.record_store
    assert record_store._backend is None
    execution = (
        agent.input("missing workspace")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(limits={"max_model_requests": 1})
    )

    with pytest.raises(RuntimeError, match="non-active.*process|process.*non-active"):
        await execution.async_record_data()

    assert record_store._backend is None
    assert execution.record_store._backend is None
    assert execution.task_workspace.root == (
        tmp_path / ".agently" / "task_workspaces" / agent.id
    ).resolve()
    assert not (tmp_path / ".agently" / "records").exists()


def test_agent_execution_record_data_sync_wrapper_rejects_non_active_process(tmp_path):
    agent = _create_agent("task-step-workspace-sync").use_task_workspace(tmp_path / "run")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution = (
        agent.input("workspace-bound sync step")
        .output({"answer": (str, "answer", True)}, format="json")
        .create_execution(
            lineage={"task_id": "workspace-sync-task", "step_id": "record-sync"},
            limits={"max_model_requests": 1},
        )
    )

    with pytest.raises(RuntimeError, match="non-active.*process|process.*non-active"):
        execution.record_data(content={"sync": True}, checkpoint=True)


@pytest.mark.asyncio
async def test_real_direct_route_emits_one_canonical_agent_execution_terminal_event(tmp_path):
    events: list[Any] = []
    agent = _create_agent("real-route-single-terminal-success").use_task_workspace(tmp_path / "success")
    execution: Any = agent.input("Return one result.").create_execution().strategy("direct")

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id:
            if event.event_type in {"agent_execution.completed", "agent_execution.failed"}:
                events.append(event)

    hook_name = "test.real_route_single_terminal_success"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        result = await execution.async_get_data()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert "ok-" in str(result)
    assert [event.event_type for event in events] == ["agent_execution.completed"]
    assert [event.source for event in events] == ["BaseAgent"]
    assert events[0].payload["close_snapshot"]["terminal_result"] == result


@pytest.mark.asyncio
async def test_bounded_structured_agent_execution_reaches_request_terminal_with_late_fields():
    events: list[Any] = []
    agent = _create_late_structured_agent()
    execution = (
        agent.input("Produce one support reply and all downstream task evidence fields.")
        .output(
            {
                "answer": (str, "Completed support reply.", True),
                "artifact_manifest": (dict, "TaskWorkspace artifact manifest."),
                "file_refs": ([str], "Produced file references."),
                "evidence": ([dict], "Evidence records."),
                "evidence_use": ([dict], "Claim-to-evidence bindings."),
                "acceptance_points": ([dict], "Acceptance checks."),
                "remaining_work": ([str], "Outstanding work."),
                "self_check": (str, "Final self-check."),
                "short_summary": (str, "Short task summary."),
                "progress_message": (str, "Terminal progress message."),
                "diagnostics": ([str], "Diagnostics."),
            },
            format="json",
        )
        .create_execution(
            lineage={
                "task_id": "support-escalation-reply",
                "iteration_id": "taskboard:synthesize-and-write:attempt:1",
                "step_id": "synthesize_and_write",
            },
            limits={"max_model_requests": 1},
        )
        .strategy("direct")
    )

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id:
            events.append(event)

    hook_name = "test.bounded_structured_request_natural_terminal"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        stream_items = [item async for item in execution.get_async_generator(type="instant")]
        data = await execution.async_get_data()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert data == MockLateStructuredRequester.final_payload
    assert MockLateStructuredRequester.natural_terminal_emitted is True
    assert MockLateStructuredRequester.cancelled_before_terminal is False
    assert [
        event.event_type for event in events if event.event_type.startswith("request.")
    ] == ["request.started", "request.completed"]
    assert not any(
        event.event_type == "model.status" and event.payload.get("status") == "cancelled"
        for event in events
    )
    model_meta = [event for event in events if event.event_type == "model.meta"]
    assert len(model_meta) == 1
    assert model_meta[0].payload["meta"]["usage"]["total_tokens"] == 321
    streamed_paths = {item.path for item in stream_items}
    assert {"answer", "artifact_manifest.path"} <= streamed_paths


@pytest.mark.asyncio
async def test_real_direct_route_failure_emits_one_bounded_canonical_terminal_event(tmp_path):
    events: list[Any] = []
    agent = _create_failing_agent("real-route-single-terminal-failure").use_task_workspace(tmp_path / "failure")
    execution: Any = agent.input("Fail through the real requester.").create_execution().strategy("direct")

    async def capture(event: Any) -> None:
        if event.run is not None and event.run.execution_id == execution.id:
            if event.event_type in {"agent_execution.completed", "agent_execution.failed"}:
                events.append(event)

    hook_name = "test.real_route_single_terminal_failure"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        with pytest.raises(RuntimeError, match="real-route-provider-failure"):
            await execution.async_get_data(max_retries=0)
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert [event.event_type for event in events] == ["agent_execution.failed"]
    assert [event.source for event in events] == ["BaseAgent"]
    close_snapshot = events[0].payload["close_snapshot"]
    assert close_snapshot["terminal_result"]["status"] == "error"
    assert len(str(close_snapshot).encode("utf-8")) <= 4096


@pytest.mark.asyncio
async def test_active_agent_execution_process_and_recovery_writes_use_execution_lifecycle_id(tmp_path):
    agent = _create_agent("active-workspace-write").use_task_workspace(tmp_path / "task_workspace")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    execution: Any = agent.input("Wait for active writes.").create_execution(
        lineage={"task_id": "different-task-id", "step_id": "active-write"}
    ).strategy("direct")
    started = asyncio.Event()
    finish = asyncio.Event()

    async def active_route(**_kwargs: Any) -> tuple[str, dict[str, str]]:
        execution.route_info["selected_route"] = "model_request"
        started.set()
        await finish.wait()
        return "model_request", {"answer": "done"}

    execution._async_execute_route = active_route  # type: ignore[method-assign]
    run = asyncio.create_task(execution.async_get_data())
    await started.wait()
    process_record = await asyncio.wait_for(
        execution.async_record_data(purpose="process", content={"stage": "active"}),
        timeout=1,
    )
    recovery_record = await asyncio.wait_for(
        execution.async_record_data(
            purpose="recovery",
            content={"stage": "checkpoint"},
            checkpoint=True,
            checkpoint_state={"state_version": 11, "interrupts": {}, "intervention": {"ledger": {}}},
        ),
        timeout=1,
    )

    assert process_record["record"]["scope"]["execution_id"] == execution.id
    assert recovery_record["checkpoint"] is not None
    history = await execution.record_store.checkpoint_history(execution.id, step_id="active-write")
    assert [item["id"] for item in history] == [recovery_record["checkpoint"]["id"]]
    assert await execution.record_store.checkpoint_history("different-task-id") == []

    finish.set()
    assert await run == {"answer": "done"}
    with pytest.raises(RuntimeError, match="post-terminal.*process|process.*post-terminal"):
        await execution.async_record_data(purpose="process", content={"late": True})
