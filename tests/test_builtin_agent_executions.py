import asyncio
import json
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.plugins.AgentExecution import AgentExecution, ProductionOptions
from agently.core import PluginManager
from agently.types.data import AgentlyRequestData, ExecutionExchangeView, OutputValidateResultDict
from agently.utils import Settings


class ScriptedExecutionRequester:
    name = "ScriptedExecutionRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    responses: list[Any] = []
    requests: list[dict[str, Any]] = []
    model_dispatches = 0

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls, responses: list[Any]) -> None:
        cls.responses = list(responses)
        cls.requests = []
        cls.model_dispatches = 0

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        index = len(type(self).requests)
        if index >= len(type(self).responses):
            raise AssertionError(f"Unexpected Execution ModelRequest at index {index}.")
        prompt_snapshot = self.prompt.get()
        assert isinstance(prompt_snapshot, dict)
        type(self).requests.append(prompt_snapshot)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"response": type(self).responses[index]},
            request_options={"stream": True},
            request_url="mock://builtin-agent-execution",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        type(self).model_dispatches += 1
        response = request_data.data["response"]
        content = response if isinstance(response, str) else json.dumps(response)
        yield "message", content

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {"provider": "mock-builtin-agent-execution", "status": "completed"}


class ClarificationProvider:
    def __init__(self, response: Any):
        self.response = response
        self.published: list[dict[str, Any]] = []
        self.awaited: list[dict[str, Any]] = []

    def publish_request(self, execution_id, request, *, interrupt):
        self.published.append(
            {
                "execution_id": execution_id,
                "request": dict(request),
                "interrupt": dict(interrupt),
            }
        )
        return {"exchange_id": f"clarification-{len(self.published)}"}

    async def await_response(self, request):
        self.awaited.append(dict(request))
        return self.response


def create_execution_agent(
    tmp_path: Path,
    name: str,
    responses: list[Any],
    *,
    settings_values: dict[str, Any] | None = None,
):
    ScriptedExecutionRequester.reset(responses)
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    for key, value in (settings_values or {}).items():
        settings.set(key, value)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name=f"{name}-PluginManager",
    )
    plugin_manager.register("ModelRequester", ScriptedExecutionRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    ).use_task_workspace(tmp_path / name)


def ready_payload() -> dict[str, Any]:
    return {
        "plan_ready": True,
        "readiness_summary": "Scope and acceptance are available.",
        "questions": [],
    }


def not_ready_payload() -> dict[str, Any]:
    return {
        "plan_ready": False,
        "readiness_summary": "The target environment is missing.",
        "questions": [
            {
                "question": "Which environment is the release targeting?",
                "why_needed": "The rollout and validation steps differ by environment.",
            }
        ],
    }


def test_default_plugins_register_bundled_agent_executions():
    names = Agently.plugin_manager.get_plugin_list("AgentExecution")

    assert "plan" in names
    assert "long_content" in names
    assert Agently.settings.get("plugins.AgentExecution.plan.max_questions_per_round") == 3
    assert Agently.settings.get("plugins.AgentExecution.long_content.max_sections") == 12


@pytest.mark.asyncio
async def test_plan_execution_runs_readiness_then_terminal_plan(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "builtin-plan",
        [ready_payload(), "# Release plan\n\n1. Validate.\n2. Ship."],
    )
    execution = agent.create_execution("plan").input("Plan the release.")

    result = await execution.async_get_data()

    assert result.startswith("# Release plan")
    assert len(ScriptedExecutionRequester.requests) == 2
    assert execution.name == "plan"
    assert execution.status == "success"
    assert execution.route_info["selected_route"] == "plan"
    assert execution.diagnostics["execution_run"] == {
        "name": "plan",
        "model_request_count": 2,
        "stages": ["readiness_1", "final_plan"],
        "clarification_rounds": 0,
    }
    assert len(execution.logs["model_response_ids"]) == 2
    paths = [item.path for item in execution.stream.items]
    assert paths.index("route.selected") < paths.index("execution.stage.started")
    assert paths.index("execution.stage.completed") < paths.index("result")


@pytest.mark.asyncio
async def test_application_can_override_bundled_execution_without_builtin_identity(tmp_path):
    class ApplicationPlanExecution(AgentExecution):
        producer_route = "plan"
        name = "plan"
        DEFAULT_SETTINGS: dict[str, Any] = {}

        @staticmethod
        def _on_register() -> None:
            pass

        @staticmethod
        def _on_unregister() -> None:
            pass

        async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
            return "plan", "application plan"

    agent = create_execution_agent(tmp_path, "overridden-plan", [])
    agent.plugin_manager.register(
        "AgentExecution",
        ApplicationPlanExecution,
        activate=False,
    )
    execution = agent.create_execution("plan").input("Plan the release.")

    assert await execution.async_get_data() == "application plan"
    assert type(execution) is ApplicationPlanExecution
    assert ScriptedExecutionRequester.requests == []


@pytest.mark.asyncio
async def test_plan_execution_preserves_structured_output_and_request_validator(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "structured-plan",
        [ready_payload(), {"steps": ["validate", "ship"]}],
    )
    validated: list[dict[str, Any]] = []

    def validator(value, _context):
        validated.append(dict(value))
        return bool(value["steps"])

    execution = (
        agent.create_execution("plan").input("Plan the release.")
        .output({"steps": ([str], "Ordered plan steps")}, format="json")
        .validate(validator)

    )

    assert await execution.async_get_data() == {"steps": ["validate", "ship"]}
    assert validated == [{"steps": ["validate", "ship"]}]
    first_output = ScriptedExecutionRequester.requests[0]["output"]
    final_output = ScriptedExecutionRequester.requests[1]["output"]
    assert "plan_ready" in first_output.model_fields
    assert "steps" in final_output


@pytest.mark.asyncio
async def test_plan_execution_uses_execution_exchange_for_clarification(tmp_path):
    from agently.base import execution_exchange

    provider_id = "test-plan-clarification"
    provider = ClarificationProvider({"environment": "staging"})
    execution_exchange.register_provider(provider_id, provider, replace=True)
    try:
        agent = create_execution_agent(
            tmp_path,
            "clarified-plan",
            [
                not_ready_payload(),
                ready_payload(),
                "# Staging release plan\n\n1. Validate staging.\n2. Ship.",
            ],
            settings_values={
                "interaction.mode": "hot",
                "interaction.exchange_provider": provider_id,
                "interaction.hot_wait_timeout": 1,
            },
        )
        execution = agent.create_execution("plan").input("Plan the release.")

        result = await execution.async_get_data()

        assert result.startswith("# Staging release plan")
        assert len(provider.published) == 1
        assert len(provider.awaited) == 1
        second_input = ScriptedExecutionRequester.requests[1]["input"]
        assert second_input["execution_stage_input"]["clarifications"][0]["response"] == {
            "environment": "staging"
        }
        assert execution.diagnostics["execution_run"]["clarification_rounds"] == 1
        assert cast(list[dict[str, Any]], execution._review_contract["clarifications"])[0]["response"] == {"environment": "staging"}
        assert "not execution" in str(execution._review_contract["deliverable_role"])
        paths = [item.path for item in execution.stream.items]
        assert "exchange.pending" in paths
        assert "exchange.resolved" in paths
    finally:
        execution_exchange.unregister_provider(provider_id)


@pytest.mark.asyncio
async def test_plan_execution_uses_standard_agent_interaction_handler(tmp_path):
    from agently.base import execution_exchange

    seen: list[ExecutionExchangeView] = []
    registered_before = execution_exchange.list_providers()
    agent = create_execution_agent(
        tmp_path,
        "agent-interaction-plan",
        [
            not_ready_payload(),
            ready_payload(),
            "# Staging release plan\n\n1. Validate staging.\n2. Ship.",
        ],
        settings_values={"interaction.mode": "durable"},
    )

    def handle_exchange(exchange: ExecutionExchangeView) -> dict[str, str]:
        seen.append(exchange)
        return {"environment": "staging"}

    execution = (
        agent.create_execution("plan").input("Plan the release.")
        .interact(handle_exchange)

    )

    result = await execution.async_get_data()

    assert result.startswith("# Staging release plan")
    assert agent.settings.get("interaction.mode") == "durable"
    assert execution.request.settings.get("interaction.mode") == "hot"
    assert execution_exchange.list_providers() == registered_before
    assert len(seen) == 1
    assert seen[0]["kind"] == "clarification"
    assert seen[0]["status"] == "pending"
    assert seen[0]["subject"] == "Plan clarification"
    assert seen[0]["payload"]["questions"][0]["question"].startswith("Which environment")
    assert seen[0]["request"].get("provider_metadata", {}).get("provider") == "agent_interaction"
    second_input = ScriptedExecutionRequester.requests[1]["input"]
    assert second_input["execution_stage_input"]["clarifications"][0]["response"] == {
        "environment": "staging"
    }


@pytest.mark.asyncio
async def test_plan_execution_fails_closed_for_durable_clarification(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "durable-plan",
        [not_ready_payload()],
        settings_values={"interaction.mode": "durable"},
    )
    execution = agent.create_execution("plan").input("Plan the release.")

    with pytest.raises(RuntimeError, match="cannot yet return a resumable Execution handle"):
        await execution.async_get_data()

    assert len(ScriptedExecutionRequester.requests) == 1
    assert execution.status in {"error", "blocked"}
    assert "exchange.pending" in [item.path for item in execution.stream.items]


@pytest.mark.asyncio
async def test_plan_execution_rejects_transport_continuation_mix(tmp_path):
    agent = create_execution_agent(tmp_path, "continued-plan", [])
    execution = (
        agent.create_execution("plan").input("Plan the release.")
        .auto_continue()

    )

    with pytest.raises(ValueError, match="cannot be combined with ensure_long_output"):
        await execution.async_get_data()

    assert ScriptedExecutionRequester.requests == []

@pytest.fixture
def stub_long_task_production(monkeypatch):
    """Only contract plumbing; this fixture is not model-quality evidence."""
    from agently.builtins.plugins.AgentExecution.long_task import AgentTask

    async def stream(task, *args, **kwargs):
        task.result = {"final_result": "fixture candidate", "status": "completed", "accepted": True}
        task.status = "completed"
        task._completed = True
        if False:
            yield None

    monkeypatch.setattr(AgentTask, "get_async_generator", stream)


@pytest.mark.asyncio
async def test_long_task_terminal_policies_share_structured_business_value(tmp_path, monkeypatch):
    from agently.builtins.plugins.AgentExecution.long_task import AgentTask

    business = {"answer": "fixture answer"}
    envelope = {"status": "completed", "accepted": True, "final_result": json.dumps(business)}

    async def stream(task, *args, **kwargs):
        task.result = dict(envelope)
        task.status = "completed"
        task._completed = True
        if False:
            yield None

    monkeypatch.setattr(AgentTask, "get_async_generator", stream)
    agent = create_execution_agent(tmp_path, "business-subject", [])
    observed = []

    def validate(result, context):
        observed.append(("validate", result))
        return True

    def render(result, context):
        observed.append(("artifact", result))
        return json.dumps(result)

    def review(result, context):
        observed.append(("review", result))
        assert context.artifact_refs
        return True

    execution = (
        agent.create_execution("long_task").strategy("flat").goal("Declared goal", ["Declared criterion"])
        .output({"answer": (str, "Final answer", True)})
        .validate(validate).artifact("answer.json", render).review(review)
    )
    captured = execution.get_result()
    assert await execution.async_run() == envelope
    assert await captured.async_get_data() == business
    assert await execution.async_get_full_data() == envelope
    assert observed == [(name, business) for name in ("validate", "artifact", "review")]
    artifact_path = execution.task_workspace.resolve_file_path(execution.artifact_results[0]["path"])
    assert json.loads(artifact_path.read_text()) == business
    assert ScriptedExecutionRequester.model_dispatches == 0


@pytest.mark.asyncio
async def test_retained_goal_contract_and_original_request_reach_review_without_regeneration(tmp_path):
    from agently.builtins.plugins.AgentExecution.long_task import AgentTask
    from agently.builtins.plugins.AgentExecution.modules.goal_preparation import PreparedGoal

    agent = create_execution_agent(tmp_path, "retained-goal", [])
    agent.use_record_store(tmp_path / "records", mode="read_write")
    provenance = PreparedGoal((), ("Derived criterion",), "original-preparation-id").to_record()
    original = {"input": "Original request facts", "goal": ["Explicit retained goal"]}
    task = AgentTask(
        agent, goal="Explicit retained goal", success_criteria=["Derived criterion"], execution="flat",
        options={
            "agent_task": {"record_store_recovery": True},
            "execution_prompt_snapshot": original, "goal_preparation": provenance,
        },
    )
    await task._write_resume_snapshot(1, {"is_complete": True, "final_result": "retained result"})
    assert not task.diagnostics.get("resume_snapshot_errors")
    reviewed = []

    def review(result, context):
        reviewed.append(context)
        return True

    execution = cast(AgentExecution, await agent.async_resume(task.id))
    execution.review(review)
    assert await execution.async_get_data() == "retained result"
    assert execution.goal_items == ["Explicit retained goal"]
    assert execution.success_criteria_items == ["Derived criterion"]
    assert reviewed[0].prompt["input"] == original["input"]
    assert reviewed[0].goals == ("Explicit retained goal",)
    origin = execution._review_contract["goal_preparation"]
    assert isinstance(origin, dict) and origin["request_id"] == "original-preparation-id"
    assert ScriptedExecutionRequester.model_dispatches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_goal,declared_criteria", [
    (None, None), ("Explicit goal", None), (None, ["Explicit criterion"]),
])
async def test_missing_long_task_contract_is_prepared_once_and_preserves_declarations(
    tmp_path, stub_long_task_production, declared_goal, declared_criteria,
):
    response = {"status": "ready", "missing_information": []}
    if declared_goal is None:
        response["goals"] = ["Inferred goal"]
    if declared_criteria is None:
        response["success_criteria"] = ["Inferred criterion"]
    agent = create_execution_agent(tmp_path, "prepare-goal", [response])
    execution = cast(AgentExecution, agent.create_execution("long_task").strategy("flat").input("Original source request"))
    if declared_goal:
        execution.goal(declared_goal, turn_on_long_task=False)
    if declared_criteria:
        execution.set_execution_prompt("success_criteria", declared_criteria)
    original = dict(execution.prompt_snapshot)
    validated = []
    execution.validate(lambda result, context: validated.append(result) or True)
    result = await execution.async_get_data()
    assert result == "fixture candidate"
    assert (await execution.async_get_full_data())["status"] == "completed"
    assert validated == [{"value": result}]
    assert execution.prompt_snapshot == original
    assert execution.goal_items == [declared_goal or "Inferred goal"]
    assert execution.success_criteria_items == (declared_criteria or ["Inferred criterion"])
    assert len(ScriptedExecutionRequester.requests) == 1
    task = execution.task_record
    assert task is not None
    assert task.goal == "\n".join(execution.goal_items)
    assert task.success_criteria == execution.success_criteria_items
    record = task.options["goal_preparation"]
    assert record["source"] == "model"
    assert record["request_id"] in execution.logs["model_response_ids"]
    origin = execution._review_contract["goal_preparation"]
    assert isinstance(origin, dict) and origin["source"] == "model"
    await execution.async_get_data()
    assert len(ScriptedExecutionRequester.requests) == 1


@pytest.mark.asyncio
async def test_complete_or_restored_contract_needs_no_preparation_request(tmp_path, stub_long_task_production):
    from agently.builtins.plugins.AgentExecution.modules.goal_preparation import (
        PreparedGoal, prepare_missing_goal, retain_prepared_goal,
    )
    agent = create_execution_agent(tmp_path, "complete-goal", [])
    complete = agent.create_execution("long_task").strategy("flat").goal("Declared", ["Declared criterion"])
    assert (await complete.async_get_full_data())["status"] == "completed"
    restored = cast(AgentExecution, agent.create_execution("long_task").strategy("flat").input("Original request"))
    retain_prepared_goal(restored, PreparedGoal.from_record(
        PreparedGoal(("Retained goal",), ("Retained criterion",), "retained-request").to_record(),
    ))
    assert await prepare_missing_goal(restored) is None
    assert restored.goal_items == ["Retained goal"]
    assert restored.prompt_snapshot["input"] == "Original request"
    assert ScriptedExecutionRequester.requests == []


@pytest.mark.asyncio
async def test_missing_goal_information_blocks_without_task_or_final_policy(tmp_path):
    response = {"status": "missing_information", "goals": [], "success_criteria": [],
                "missing_information": ["Which supplied record is the requested subject?"]}
    agent = create_execution_agent(tmp_path, "missing-goal-info", [response])
    execution = agent.create_execution("long_task").strategy("flat").input("Handle it.")
    seen = []
    execution.validate(lambda result, context: seen.append(result) or True).review(
        lambda result, context: seen.append(result) or True,
    )
    result = await execution.async_get_data()
    assert result["status"] == "blocked"
    assert result["missing_information"] == response["missing_information"]
    assert execution.task_record is None
    assert execution.status == "blocked"
    assert seen == []


@pytest.mark.asyncio
async def test_goal_preparation_is_counted_against_outer_budget(tmp_path):
    from agently.core import AgentExecutionLimitExceeded
    agent = create_execution_agent(tmp_path, "goal-budget", [{"status": "ready"}])
    execution = agent.create_execution("long_task", limits={"max_model_requests": 0}).input("Write the report.")
    with pytest.raises(AgentExecutionLimitExceeded):
        await execution.async_get_data()
    assert ScriptedExecutionRequester.model_dispatches == 0
    assert execution.task_record is None


@pytest.mark.asyncio
async def test_expired_goal_preparation_dispatches_nothing(tmp_path):
    from agently.core.application.AgentExecution import RuntimeStageStallError

    agent = create_execution_agent(tmp_path, "expired-preparation", [])
    execution = agent.create_execution("long_task", limits={"max_seconds": 0}).input("Write a report.").strategy("flat")

    with pytest.raises(RuntimeStageStallError) as caught:
        await execution.async_get_data()

    assert caught.value.status == "timed_out"
    assert execution.status == "timed_out"
    assert execution.task_record is None
    assert ScriptedExecutionRequester.model_dispatches == 0
    assert ScriptedExecutionRequester.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["deadline", "cancel"])
@pytest.mark.parametrize("dispatch_delay", [0, 0.2])
async def test_goal_preparation_stops_and_joins_before_terminal(tmp_path, monkeypatch, stop, dispatch_delay):
    from importlib import import_module
    from types import SimpleNamespace

    from agently.core.application.AgentExecution import RuntimeStageStallError

    started = asyncio.Event()
    settled = asyncio.Event()
    origin = time.monotonic()
    dispatched_at = None

    def monotonic():
        return origin if dispatched_at is None else origin + time.monotonic() - dispatched_at

    # This test owns in-flight cancellation, not startup timing. Begin its
    # logical clock at provider dispatch, then let the real timer expire.
    clock = SimpleNamespace(monotonic=monotonic)
    for module_name in (
        "agently.core.application.AgentExecution.Context",
        "agently.builtins.plugins.AgentExecution.modules.limits",
    ):
        monkeypatch.setattr(import_module(module_name), "time", clock)

    async def pending_model(self, request_data):
        nonlocal dispatched_at
        await asyncio.sleep(dispatch_delay)
        dispatched_at = time.monotonic()
        ScriptedExecutionRequester.model_dispatches += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            settled.set()
        if False:
            yield "message", ""

    monkeypatch.setattr(ScriptedExecutionRequester, "request_model", pending_model)
    agent = create_execution_agent(tmp_path, f"preparation-{stop}", [{"status": "ready"}])
    execution = agent.create_execution(
        "long_task", limits={"max_seconds": 0.15 if stop == "deadline" else None},
    ).input("Write a report.").strategy("flat")
    policies = []
    execution.validate(lambda result, context: policies.append(result) or True)
    work = asyncio.create_task(execution.async_get_data())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        if stop == "cancel":
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
        else:
            with pytest.raises(RuntimeStageStallError) as caught:
                await asyncio.wait_for(work, timeout=2)
            assert caught.value.status == "timed_out"
        assert settled.is_set()
        assert execution.task_record is None
        assert policies == []
        assert ScriptedExecutionRequester.model_dispatches == 1
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_preparation_and_task_share_absolute_execution_deadline(tmp_path, monkeypatch):
    from importlib import import_module
    from inspect import CORO_CLOSED, getcoroutinestate
    from types import SimpleNamespace

    from agently.builtins.plugins.AgentExecution.long_task import AgentTask

    # Advance only the owners' logical clocks. Global time and asyncio's clock
    # stay real; a busy host cannot consume this test's budget before handoff.
    monotonic_origin, wall_origin = time.monotonic(), time.time()
    logical_elapsed = 0.0
    budget, preparation_elapsed = 30.0, 10.0
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

    original_request = ScriptedExecutionRequester.request_model

    async def delayed_preparation(self, request_data):
        nonlocal logical_elapsed
        logical_elapsed = preparation_elapsed
        async for item in original_request(self, request_data):
            yield item

    monkeypatch.setattr(ScriptedExecutionRequester, "request_model", delayed_preparation)
    agent = create_execution_agent(tmp_path, "shared-task-deadline", [{
        "status": "ready", "goals": ["Write the requested report"],
        "success_criteria": ["Return the requested report"], "missing_information": [],
    }])
    execution = cast(AgentExecution, agent.create_execution(
        "long_task", limits={"max_seconds": budget},
    ).input("Write a report.").strategy("flat"))
    observed = []
    plan_coroutines = []
    planner_started = False

    async def pending_plan():
        nonlocal planner_started
        planner_started = True
        await asyncio.Event().wait()

    def prepare_plan(iteration_index, context_pack):
        nonlocal logical_elapsed
        task = cast(AgentTask, execution.task_record)
        elapsed = clock.monotonic() - execution.execution_context.started_at
        observed.append((elapsed, task._task_deadline_remaining()))
        # Expire at the actual Flat plan dispatch boundary. The real guard must
        # close the unstarted coroutine, not dispatch a new planner request.
        logical_elapsed = budget
        plan_coro = pending_plan()
        plan_coroutines.append(plan_coro)
        return plan_coro

    execution._agent_task_step_overrides = {"_request_plan": prepare_plan}
    result = await asyncio.wait_for(execution.async_get_full_data(), timeout=2)

    assert result["status"] == "timed_out"
    assert result["accepted"] is False
    assert "plan stage" in result["reason"]
    assert execution.status == "timed_out"
    assert len(observed) == 1
    elapsed, remaining = observed[0]
    assert elapsed == pytest.approx(preparation_elapsed)
    assert remaining is not None
    assert remaining == pytest.approx(budget - preparation_elapsed)
    assert elapsed + remaining == pytest.approx(budget)
    task = cast(AgentTask, execution.task_record)
    assert task._execution_deadline_monotonic == execution.execution_context.started_at + budget
    assert task.limits["max_seconds"] == execution.limits["max_seconds"] == budget
    assert "_execution_deadline_monotonic" not in task.options
    assert ScriptedExecutionRequester.model_dispatches == 1
    assert planner_started is False
    assert len(plan_coroutines) == 1
    assert getcoroutinestate(plan_coroutines[0]) == CORO_CLOSED


@pytest.mark.asyncio
async def test_task_plan_deadline_cancels_and_joins_pending_work(tmp_path, monkeypatch):
    from importlib import import_module
    from types import SimpleNamespace

    from agently.builtins.plugins.AgentExecution.long_task import AgentTask
    from agently.builtins.plugins.AgentExecution.long_task.TaskShared import _AgentTaskDeadlineExceeded

    agent = create_execution_agent(tmp_path, "pending-plan-deadline", [])
    task = AgentTask(
        agent, goal="Clock fixture", success_criteria=["Clock invariant"],
        limits={"max_seconds": 0.05},
    )
    monotonic_origin, wall_origin = time.monotonic(), time.time()
    clock = SimpleNamespace(monotonic=lambda: monotonic_origin, time=lambda: wall_origin)
    monkeypatch.setattr(
        import_module("agently.builtins.plugins.AgentExecution.long_task.RuntimeControl"),
        "time", clock,
    )
    task.started_at = wall_origin
    task._execution_deadline_monotonic = monotonic_origin + 0.05
    assert task._task_deadline_remaining() == pytest.approx(0.05)

    started, cleanup_started, release_cleanup, settled = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    )

    async def pending_plan():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            settled.set()

    plan_work = asyncio.create_task(pending_plan())
    deadline_work = None
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # Only the arithmetic clock is fixed; asyncio performs a real timed
        # wait on already-running work and the owner must cancel and join it.
        deadline_work = asyncio.create_task(task._await_task_deadline(plan_work, stage="plan"))
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        assert not deadline_work.done()
        assert not settled.is_set()
        release_cleanup.set()
        with pytest.raises(_AgentTaskDeadlineExceeded) as caught:
            await asyncio.wait_for(deadline_work, timeout=2)
        assert caught.value.stage == "plan"
        assert caught.value.limit_name == "max_seconds"
        assert caught.value.timeout_seconds == 0.05
        assert plan_work.done() and settled.is_set()
        assert ScriptedExecutionRequester.model_dispatches == 0
    finally:
        release_cleanup.set()
        owned_work = [plan_work] + ([deadline_work] if deadline_work is not None else [])
        for work in owned_work:
            if not work.done():
                work.cancel()
        await asyncio.gather(*owned_work, return_exceptions=True)


@pytest.mark.parametrize("task_seconds,parent_seconds,expected", [
    (None, None, None), (10, None, 10), (None, 2, 2),
    (10, 2, 2), (2, 10, 2), (2, -1, -1),
])
def test_task_deadline_respects_both_owners_without_mutating_limits(
    tmp_path, task_seconds, parent_seconds, expected,
):
    from agently.builtins.plugins.AgentExecution.long_task import AgentTask

    agent = create_execution_agent(tmp_path, "task-clock-boundary", [])
    task = AgentTask(agent, goal="Clock fixture", success_criteria=["Clock invariant"],
                     limits={"max_seconds": task_seconds})
    task.started_at = time.time()
    task._execution_deadline_monotonic = (
        time.monotonic() + parent_seconds if parent_seconds is not None else None
    )
    remaining = task._task_deadline_remaining()
    if expected is None:
        assert remaining is None
    else:
        assert remaining == pytest.approx(expected, abs=0.02)
    assert task.limits == {"max_seconds": task_seconds}


@pytest.mark.asyncio
async def test_review_receives_inferred_contract_and_host_provenance(tmp_path, stub_long_task_production):
    response = {"status": "ready", "goals": ["Inferred goal"],
                "success_criteria": ["Inferred criterion"], "missing_information": []}
    review = {"passed": True, "quality_level": "adequate", "checks": [],
              "summary": "Fixture judgment.", "issues": [], "overall_suggestions": []}
    agent = create_execution_agent(tmp_path, "prepared-review", [response, review])
    execution = agent.create_execution("long_task").strategy("flat").input("Original requirement").review()
    await execution.async_get_data()
    request = ScriptedExecutionRequester.requests[1]
    contract = request["info"]["request_contract"]
    assert contract["goals"] == response["goals"]
    assert contract["success_criteria"] == response["success_criteria"]
    assert contract["generated_criterion_indices"] == [0]
    assert contract["goal_preparation"]["inferred_fields"] == ["goals", "success_criteria"]
    assert contract["goal_preparation"]["request_id"] in execution.logs["model_response_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"status": "unknown", "success_criteria": [], "missing_information": []},
    {"status": "ready", "success_criteria": [], "missing_information": []},
    {"status": "ready", "success_criteria": [" "], "missing_information": []},
    {"status": "ready", "success_criteria": "not a list", "missing_information": []},
    {"status": "ready", "success_criteria": ["Criterion"], "missing_information": ["Still missing"]},
    {"status": "missing_information", "success_criteria": [], "missing_information": []},
    {"status": "ready", "goals": ["Overwrite"], "success_criteria": ["Criterion"], "missing_information": []},
])
async def test_goal_preparation_rejects_invalid_contributions(tmp_path, monkeypatch, response):
    from agently.builtins.plugins.AgentExecution.modules import goal_preparation
    from agently.builtins.plugins.AgentExecution.modules.model_stage import ModelStageResult
    agent = create_execution_agent(tmp_path, "invalid-goal-contribution", [])
    execution = cast(AgentExecution, agent.create_execution("long_task").goal("Keep explicit goal"))
    async def stage(*args, **kwargs):
        return ModelStageResult(response, "fixture-request")
    monkeypatch.setattr(goal_preparation, "run_model_stage", stage)
    with pytest.raises(ValueError):
        await goal_preparation.prepare_missing_goal(execution)
    assert execution.goal_items == ["Keep explicit goal"]
    assert execution._prepared_goal is None


@pytest.mark.asyncio
async def test_builtin_execution_model_stages_share_agent_execution_budget(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "bounded-plan-requests",
        [ready_payload(), "This second provider response must not be consumed."],
    )
    execution = (
        agent.create_execution("plan", limits={"max_model_requests": 1})
        .input("Plan the release.")

    )

    with pytest.raises(RuntimeError, match="max_model_requests"):
        await execution.async_get_data()

    # The second request object is rendered before the shared runtime budget
    # rejects provider dispatch, matching the ordinary ModelRequest contract.
    assert len(ScriptedExecutionRequester.requests) == 2
    assert execution.execution_context.model_request_count == 1
    assert execution.status in {"error", "blocked"}


@pytest.mark.asyncio
async def test_long_content_execution_plans_writes_and_host_assembles(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "long-content",
        [
            {
                "document_title": "Architecture Guide",
                "part_plan": [
                    {"part_title": "Context", "part_brief": "Set the context."},
                    {"part_title": "Design", "part_brief": "Explain the design."},
                    {"part_title": "Checks", "part_brief": "List acceptance checks."},
                ],
            },
            {"body": "Context body unique text."},
            {"summary": "Use term Execution Carrier."},
            {"body": "Design body."},
            {"summary": "Validation precedes release."},
            {"body": "Checks body."},
        ],
    )
    execution = agent.create_execution("long_content").input("Write an architecture guide.")

    result = await execution.async_get_data()

    assert result == (
        "# Architecture Guide\n\n"
        "## Context\n\nContext body unique text.\n\n"
        "## Design\n\nDesign body.\n\n"
        "## Checks\n\nChecks body."
    )
    assert len(ScriptedExecutionRequester.requests) == 6
    second_writer_prompt = json.dumps(
        ScriptedExecutionRequester.requests[3],
        ensure_ascii=False,
        default=str,
    )
    assert "Use term Execution Carrier." in second_writer_prompt
    assert "Context body unique text." not in second_writer_prompt
    assert execution.diagnostics["execution_run"] == {
        "name": "long_content",
        "model_request_count": 6,
        "stages": ["section_plan", "section_1", "section_1_summary", "section_2", "section_2_summary", "section_3"],
        "section_count": 3,
        "assembled_chars": len(result),
        "assembly": "host_ordered",
    }
    assert len(execution.logs["model_response_ids"]) == 6


@pytest.mark.asyncio
async def test_long_content_execution_composes_with_artifact_and_review(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "long-content-artifact",
        [
            {
                "document_title": "Report",
                "part_plan": [
                    {"part_title": "Summary", "part_brief": "Summarize."}
                ],
            },
            {"body": "Accepted report body.", "continuity_note": ""},
        ],
    )
    reviewed: list[str] = []

    def reviewer(value, _context):
        reviewed.append(value)
        return True

    execution = (
        agent.create_execution("long_content").input("Write a report.")

        .artifact("report.md")
        .review(reviewer)
    )

    result = await execution.async_get_data()

    assert reviewed == [result]
    artifact_path = execution.task_workspace.resolve_file_path(
        execution.artifact_results[0]["path"]
    )
    assert artifact_path.read_text() == result
    paths = [item.path for item in execution.stream.items]
    assert paths.index("execution.stage.completed") < paths.index("artifact.completed")
    assert paths.index("artifact.completed") < paths.index("review.started")


@pytest.mark.asyncio
async def test_final_validator_checks_assembled_document_once_without_replay(tmp_path):
    agent = create_execution_agent(tmp_path, "document-validator", [
        {"document_title": "Report", "part_plan": [
            {"part_title": "Summary", "part_brief": "Summarize."},
        ]},
        {"body": "Document body.", "continuity_note": ""},
    ])
    checked = []
    agent.validate(lambda value, context: checked.append((value, context)) or False)
    execution = agent.create_execution("long_content").input("Write a report.").artifact("rejected.md")
    with pytest.raises(ValueError, match="Output validation failed"):
        await execution.async_get_data(max_retries=3)
    assert len(checked) == 1
    value, context = checked[0]
    assert value["value"] == "# Report\n\n## Summary\n\nDocument body."
    assert context.parsed_result == value["value"]
    assert context.max_retries == 0
    assert context.model_run_context is None
    assert context.response_id == ""
    assert len(ScriptedExecutionRequester.requests) == 2
    assert execution.artifact_results == []


@pytest.mark.asyncio
async def test_wrapper_validator_only_sees_transformed_default_result(tmp_path):
    agent = create_execution_agent(tmp_path, "wrapper-validator", ["raw"])
    checked = []

    class WrapperExecution(AgentExecution):
        name = "wrapper"
        async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
            route, value = await super()._async_produce(options)
            return route, {"wrapped": value}
    agent.plugin_manager.register("AgentExecution", WrapperExecution, activate=False)

    def validator(value, _context):
        checked.append(value)
        return value == {"wrapped": "raw"}

    agent.validate(validator)
    execution = agent.create_execution("wrapper").input("Produce text.")
    assert await execution.async_get_data() == {"wrapped": "raw"}
    assert checked == [{"wrapped": "raw"}]
    assert len(ScriptedExecutionRequester.requests) == 1


@pytest.mark.asyncio
async def test_agent_task_final_validation_does_not_replay_task(tmp_path, monkeypatch):
    from agently.builtins.plugins.AgentExecution.modules import route_execution
    agent = create_execution_agent(tmp_path, "task-validator", [])
    calls, checked = [], []

    async def task(_execution, _route_meta):
        calls.append("task")
        return {"final_response": "done"}

    async def validator(value, _context) -> OutputValidateResultDict:
        checked.append(value)
        return {"ok": False, "no_retry": True, "reason": "Final business gate rejected."}

    monkeypatch.setattr(route_execution, "run_agent_task_route", task)
    execution = agent.goal("Complete a task.").strategy("flat")
    with pytest.raises(ValueError, match="Final business gate"):
        await execution.async_get_data(validate_handler=validator, max_retries=5)
    assert calls == ["task"]
    assert checked == [{"final_response": "done"}]


@pytest.mark.asyncio
async def test_long_content_execution_rejects_over_limit_plan_before_writing(tmp_path):
    agent = create_execution_agent(
        tmp_path,
        "bounded-long-content",
        [
            {
                "document_title": "Too many sections",
                "part_plan": [
                    {"part_title": "One", "part_brief": "First."},
                    {"part_title": "Two", "part_brief": "Second."},
                ],
            }
        ],
        settings_values={"plugins.AgentExecution.long_content.max_sections": 1},
    )
    execution = agent.create_execution("long_content").input("Write a report.")

    # Supply invalid replies for every bounded parser attempt; an exhausted
    # fixture must not masquerade as proof that the plan contract was enforced.
    ScriptedExecutionRequester.responses *= 4
    with pytest.raises(ValueError, match="part_plan|validation|validat"):
        await execution.async_get_data()

    # Invalid schema can use ordinary bounded parser repair, but no writer starts.
    assert len(ScriptedExecutionRequester.requests) == 4
    assert execution.status in {"error", "blocked"}


@pytest.mark.asyncio
async def test_long_content_execution_rejects_structured_output_before_model_call(tmp_path):
    agent = create_execution_agent(tmp_path, "structured-long-content", [])
    execution = (
        agent.create_execution("long_content").input("Write a report.")
        .output({"report": (str, "report")})

    )

    with pytest.raises(ValueError, match="cannot be combined with a structured .output"):
        await execution.async_get_data()

    assert ScriptedExecutionRequester.requests == []


@pytest.mark.asyncio
async def test_long_content_execution_uses_request_local_continuation_only(tmp_path):
    agent = create_execution_agent(tmp_path, "continued-long-content", [
        {"document_title": "Report", "part_plan": [{"part_title": "Only", "part_brief": "Body"}]},
        {"body": "Short completed body."},
    ])
    execution = (
        agent.create_execution("long_content").input("Write a report.")
        .auto_continue()

    )

    assert (await execution.async_get_data()).endswith("Short completed body.")
    assert len(ScriptedExecutionRequester.requests) == 2
