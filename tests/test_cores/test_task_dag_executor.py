import asyncio
import time

import pytest

from agently import Agently
from agently.builtins.plugins import AgentlyTaskDAGPlanner
from agently.core import (
    TaskDAG,
    TaskDAGExecutor,
    TaskDAGValidator,
    TriggerFlow,
)


def _operators(flow):
    return flow._blue_print.definition.operators


def test_task_dag_validation_rejects_duplicate_ids():
    graph = {
        "graph_id": "duplicate-demo",
        "tasks": [
            {"id": "a", "kind": "local"},
            {"id": "a", "kind": "local"},
        ],
    }

    with pytest.raises(ValueError, match="Duplicate dynamic task id"):
        TaskDAGValidator({"local": lambda context: context.task.id}).validate(graph)


def test_task_dag_validation_rejects_missing_dependency():
    graph = {
        "graph_id": "missing-dependency-demo",
        "tasks": [
            {"id": "a", "kind": "local", "depends_on": ["missing"]},
        ],
    }

    with pytest.raises(ValueError, match="depends on missing task"):
        TaskDAGValidator({"local": lambda context: context.task.id}).validate(graph)


def test_task_dag_validation_rejects_cycles():
    graph = {
        "graph_id": "cycle-demo",
        "tasks": [
            {"id": "a", "kind": "local", "depends_on": ["b"]},
            {"id": "b", "kind": "local", "depends_on": ["a"]},
        ],
    }

    with pytest.raises(ValueError, match="at least one root task|dependency cycle"):
        TaskDAGValidator({"local": lambda context: context.task.id}).validate(graph)


def test_task_dag_planner_exposes_output_contract_and_constraints():
    planner = AgentlyTaskDAGPlanner({"local": lambda context: context.task.id})
    schema = planner.output_schema()

    assert schema["graph_id"][2] is True
    assert schema["task_schema_version"][2] is True
    assert schema["tasks"][0]["id"][2] is True
    assert schema["tasks"][0]["kind"][2] is True
    assert schema["tasks"][0]["depends_on"][2] is True
    assert planner.ensure_keys() == [
        "graph_id",
        "task_schema_version",
        "tasks[*].id",
        "tasks[*].kind",
        "tasks[*].depends_on",
        "semantic_outputs",
    ]
    constraints = planner.plugin_constraints()
    assert constraints["schema_version"] == "task_dag/v1"
    assert constraints["available_bindings"] == ["emit", "local", "validate"]
    assert "dependency_cycles" in constraints["validation"]
    assert "task_kinds_or_bindings_without_resolver_entries" in constraints["forbidden"]
    instructions = "\n".join(planner.instructions())
    assert "ordinary model tasks as network side effects" in instructions
    assert "Keep approval empty for read-only model analysis" in instructions
    assert "task.inputs.output_format to json" in instructions
    assert constraints["request_contract"]["output_format"] == "json"


def test_task_dag_planner_contract_returns_mutation_safe_copies():
    planner = AgentlyTaskDAGPlanner({"local": lambda context: context.task.id})

    schema = planner.output_schema()
    schema["graph_id"] = "mutated"
    schema["tasks"][0]["id"] = "mutated"
    assert planner.output_schema()["graph_id"][0] is str
    assert planner.output_schema()["tasks"][0]["id"][0] is str

    ensure_keys = planner.ensure_keys()
    ensure_keys.append("mutated")
    assert planner.ensure_keys() == [
        "graph_id",
        "task_schema_version",
        "tasks[*].id",
        "tasks[*].kind",
        "tasks[*].depends_on",
        "semantic_outputs",
    ]


def test_task_dag_loads_and_exports_yaml_json_config(tmp_path):
    graph = TaskDAG.from_value(
        {
            "graph_id": "config-demo",
            "task_schema_version": "task_dag/v1",
            "tasks": [
                {"id": "extract", "kind": "local", "binding": "local_handler"},
                {
                    "id": "final",
                    "kind": "local",
                    "binding": "local_handler",
                    "depends_on": ["extract"],
                },
            ],
            "semantic_outputs": {"final": "final"},
        }
    )

    yaml_path = tmp_path / "task_dag.yaml"
    json_path = tmp_path / "task_dag.json"
    graph.get_yaml(yaml_path)
    graph.get_json(json_path)

    assert TaskDAG.from_yaml(yaml_path).to_dict() == graph.to_dict()
    assert TaskDAG.from_json(json_path).to_dict() == graph.to_dict()

    indented_yaml = "    " + graph.get_yaml().replace("\n", "\n    ")
    wrapped_yaml = f"""
plans:
  review:
{ indented_yaml }
"""
    wrapped_json = """
{
  "plans": {
    "review": %s
  }
}
""" % graph.get_json()
    assert TaskDAG.from_yaml(wrapped_yaml, task_dag_key_path="plans.review").to_dict() == graph.to_dict()
    assert TaskDAG.from_json(wrapped_json, task_dag_key_path="plans.review").to_dict() == graph.to_dict()


def test_task_dag_planner_validate_output_returns_retry_payload():
    planner = AgentlyTaskDAGPlanner({"local": lambda context: context.task.id})

    assert planner.validate_output(
        {
            "graph_id": "valid",
            "task_schema_version": "task_dag/v1",
            "tasks": [{"id": "a", "kind": "local", "depends_on": []}],
            "semantic_outputs": {"final": "a"},
        }
    ) is True

    missing_dependency = planner.validate_output(
        {
            "graph_id": "invalid",
            "task_schema_version": "task_dag/v1",
            "tasks": [{"id": "a", "kind": "local", "depends_on": ["missing"]}],
            "semantic_outputs": {"final": "a"},
        }
    )
    assert isinstance(missing_dependency, dict)
    assert missing_dependency["ok"] is False
    assert missing_dependency["validator_name"] == "task_dag"
    assert "missing task" in missing_dependency["reason"]

    wrong_version = planner.validate_output(
        {
            "graph_id": "wrong-version",
            "task_schema_version": "future",
            "tasks": [{"id": "a", "kind": "local", "depends_on": []}],
            "semantic_outputs": {"final": "a"},
        }
    )
    assert isinstance(wrong_version, dict)
    assert wrong_version["ok"] is False
    assert wrong_version["validator_name"] == "task_dag.schema_version"


@pytest.mark.asyncio
async def test_task_dag_planner_prepares_agently_request_in_stages():
    graph = {
        "graph_id": "prepared",
        "task_schema_version": "task_dag/v1",
        "tasks": [{"id": "a", "kind": "local", "depends_on": []}],
        "semantic_outputs": {"final": "a"},
    }

    class FakeRequest:
        def __init__(self):
            self.calls = []

        def input(self, value):
            self.calls.append(("input", value))
            return self

        def instruct(self, value):
            self.calls.append(("instruct", value))
            return self

        def output(self, value, *, format="auto"):
            self.calls.append(("output", value, format))
            return self

        def validate(self, value):
            self.calls.append(("validate", value))
            return self

        async def async_start(self, **kwargs):
            self.calls.append(("async_start", kwargs))
            return graph

    planner = AgentlyTaskDAGPlanner({"local": lambda context: context.task.id})
    request = FakeRequest()

    planned = await planner.async_plan(request, {"goal": "demo"}, max_retries=2)

    assert planned == graph
    assert request.calls[0] == ("input", {"goal": "demo"})
    assert request.calls[2][0] == "output"
    assert request.calls[2][2] == "json"
    assert request.calls[-1][0] == "async_start"
    assert request.calls[-1][1]["ensure_keys"] == planner.ensure_keys()
    assert request.calls[-1][1]["validate_handler"] == planner.validate_output
    assert request.calls[-1][1]["max_retries"] == 2


@pytest.mark.asyncio
async def test_task_dag_executor_runs_roots_concurrently_and_joins_dependencies():
    started_at = {}
    finished_at = {}

    async def local_task(context):
        task_id = context.task.id
        started_at[task_id] = time.perf_counter()
        await asyncio.sleep(context.task.inputs.get("delay", 0))
        finished_at[task_id] = time.perf_counter()
        if context.dependency_results:
            deps = ",".join(
                f"{ dep }={ value }" for dep, value in sorted(context.dependency_results.items())
            )
            return f"{ task_id }({ deps })"
        return f"{ task_id }:{ context.graph_input['doc'] }"

    graph = {
        "graph_id": "join-demo",
        "tasks": [
            {"id": "extract_terms", "kind": "local", "inputs": {"delay": 0.05}},
            {"id": "extract_dates", "kind": "local", "inputs": {"delay": 0.05}},
            {
                "id": "final_review",
                "kind": "local",
                "depends_on": ["extract_terms", "extract_dates"],
            },
        ],
        "semantic_outputs": {"final": "final_review"},
    }

    compiled = TaskDAGExecutor({"local": local_task}).compile(graph)
    snapshot = await compiled.async_run({"doc": "policy"}, timeout=1)

    assert snapshot["task_results"]["extract_terms"] == "extract_terms:policy"
    assert snapshot["task_results"]["extract_dates"] == "extract_dates:policy"
    assert snapshot["task_results"]["final_review"] == (
        "final_review(extract_dates=extract_dates:policy,extract_terms=extract_terms:policy)"
    )
    assert snapshot["semantic_outputs"]["final"]["task_id"] == "final_review"
    assert started_at["extract_dates"] < finished_at["extract_terms"]
    assert started_at["extract_terms"] < finished_at["extract_dates"]


@pytest.mark.asyncio
async def test_task_dag_executor_preserves_artifact_refs():
    async def artifact_task(context):
        return {
            "summary": "created",
            "artifact_refs": [{"kind": "file", "path": "reports/summary.md"}],
        }

    graph = {
        "graph_id": "artifact-demo",
        "tasks": [
            {
                "id": "make_report",
                "kind": "artifact",
                "produces": [{"role": "report"}],
            },
        ],
    }

    snapshot = await TaskDAGExecutor({"artifact": artifact_task}).async_run(graph, timeout=1)

    assert snapshot["artifact_refs"]["make_report"] == [{"kind": "file", "path": "reports/summary.md"}]
    assert snapshot["semantic_outputs"]["report"]["artifact_refs"] == [
        {"kind": "file", "path": "reports/summary.md"}
    ]


@pytest.mark.asyncio
async def test_task_dag_executor_approval_task_resumes_to_downstream_tasks():
    async def consume_approval(context):
        return f"approved={ context.dependency_results['approve_write']['approved'] }"

    graph = {
        "graph_id": "approval-demo",
        "tasks": [
            {"id": "approve_write", "kind": "approval", "approval": {"type": "human_approval"}},
            {"id": "write_report", "kind": "local", "depends_on": ["approve_write"]},
        ],
    }

    execution = TaskDAGExecutor({"local": consume_approval}).compile(graph).create_execution(
        auto_close=False
    )
    Agently.configure_policy_approval(handler="fail_closed")
    try:
        await execution.async_start({"request": "publish"})

        for _ in range(20):
            interrupts = execution.get_pending_interrupts()
            if interrupts:
                break
            await asyncio.sleep(0.01)
        assert len(interrupts) == 1
        interrupt_id = next(iter(interrupts))
        await execution.async_continue_with(interrupt_id, {"approved": True})
        snapshot = await execution.async_close(timeout=1)
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")

    assert snapshot["task_results"]["approve_write"] == {"approved": True}
    assert snapshot["task_results"]["write_report"] == "approved=True"


@pytest.mark.asyncio
async def test_task_dag_executor_checkpoint_rehydrates_approval_dag_and_continues_downstream():
    async def consume_approval(context):
        return f"approved={ context.dependency_results['approve_write']['approved'] }"

    graph = {
        "graph_id": "approval-checkpoint-demo",
        "tasks": [
            {"id": "approve_write", "kind": "approval", "approval": {"type": "human_approval"}},
            {"id": "write_report", "kind": "local", "depends_on": ["approve_write"]},
        ],
    }

    compiled = TaskDAGExecutor({"local": consume_approval}).compile(graph)
    execution = compiled.create_execution(auto_close=False)
    Agently.configure_policy_approval(handler="fail_closed")
    try:
        await execution.async_start({"request": "publish"})

        interrupts = {}
        for _ in range(20):
            interrupts = execution.get_pending_interrupts()
            if interrupts:
                break
            await asyncio.sleep(0.01)
        assert len(interrupts) == 1
        saved_state = execution.save()
        await execution.async_close(pending_interrupts="cancel")

        restored = compiled.create_execution(auto_close=False)
        report = restored.inspect_rehydration(saved_state)
        assert report["ready"] is True
        assert report["status"] == "ready"
        await restored.async_rehydrate(saved_state)

        interrupt_id = next(iter(restored.get_pending_interrupts()))
        await restored.async_continue_with(
            interrupt_id,
            {"approved": True},
            resume_request_id="approval-callback-1",
        )
        snapshot = await restored.async_close(timeout=1)
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")

    assert snapshot["task_results"]["approve_write"] == {"approved": True}
    assert snapshot["task_results"]["write_report"] == "approved=True"


def test_task_dag_executor_repeated_compilation_is_idempotent():
    async def local_task(context):
        return context.task.id

    graph = {
        "graph_id": "idempotent-demo",
        "tasks": [
            {"id": "a", "kind": "local"},
            {"id": "b", "kind": "local", "depends_on": ["a"]},
        ],
    }
    flow = TriggerFlow(name="dynamic-idempotent")

    TaskDAGExecutor({"local": local_task}, flow=flow).compile(graph)
    operator_count = len(_operators(flow))
    TaskDAGExecutor({"local": local_task}, flow=flow).compile(graph)

    assert len(_operators(flow)) == operator_count

    with pytest.raises(ValueError, match="different definition"):
        TaskDAGExecutor({"local": local_task}, flow=flow).compile(
            {
                "graph_id": "idempotent-demo",
                "tasks": [
                    {"id": "a", "kind": "local"},
                    {"id": "b", "kind": "local"},
                ],
            },
        )


@pytest.mark.asyncio
async def test_task_dag_executor_resolver_factory_can_bind_by_task():
    @TaskDAGExecutor.resolver_factory
    def handler_for_task(task):
        async def run(context):
            return f"{ task.id }:{ context.task.inputs['value'] }"

        return run

    graph = {
        "graph_id": "factory-demo",
        "tasks": [
            {"id": "one", "kind": "local", "inputs": {"value": 1}},
            {"id": "two", "kind": "local", "depends_on": ["one"], "inputs": {"value": 2}},
        ],
    }

    snapshot = await TaskDAGExecutor({"local": handler_for_task}).async_run(graph, timeout=1)

    assert snapshot["task_results"] == {"one": "one:1", "two": "two:2"}


@pytest.mark.asyncio
async def test_task_dag_executor_resolves_runtime_placeholders():
    async def local_task(context):
        if context.task.id == "lookup":
            return {
                "ticket": {"id": "T-42", "priority": "high"},
                "message": "lookup complete",
            }
        return {
            "task_inputs": context.task.inputs,
            "task_input_inputs": context.task_input["inputs"],
        }

    graph = {
        "graph_id": "placeholder-demo",
        "tasks": [
            {"id": "lookup", "kind": "local"},
            {
                "id": "draft",
                "kind": "local",
                "depends_on": ["lookup"],
                "inputs": {
                    "ticket": "${DEPS.lookup.ticket}",
                    "summary": "Account ${INIT.account} ticket ${STATE.task_results.lookup.ticket.id}",
                    "upstream_ticket": "${TRIGGER.result.ticket}",
                    "raw_init": "${INIT}",
                },
            },
        ],
    }

    snapshot = await TaskDAGExecutor({"local": local_task}).async_run(
        graph,
        graph_input={"account": "Acme"},
        timeout=1,
    )

    draft = snapshot["task_results"]["draft"]
    assert draft["task_inputs"] == draft["task_input_inputs"]
    assert draft["task_inputs"]["ticket"] == {"id": "T-42", "priority": "high"}
    assert draft["task_inputs"]["summary"] == "Account Acme ticket T-42"
    assert draft["task_inputs"]["upstream_ticket"] == {"id": "T-42", "priority": "high"}
    assert draft["task_inputs"]["raw_init"] == {"account": "Acme"}


@pytest.mark.asyncio
async def test_task_dag_executor_fails_closed_for_missing_runtime_placeholder():
    async def local_task(context):
        return context.task.inputs

    graph = {
        "graph_id": "missing-placeholder-demo",
        "tasks": [
            {
                "id": "draft",
                "kind": "local",
                "inputs": {"summary": "Ticket ${INIT.ticket_id}"},
            },
        ],
    }

    with pytest.raises(ValueError, match="runtime placeholder"):
        await TaskDAGExecutor({"local": local_task}).async_run(
            graph,
            graph_input={"account": "Acme"},
            timeout=1,
        )


@pytest.mark.asyncio
async def test_dynamic_task_default_structured_contract_overrides_planner_flat_markdown():
    class FakeModelRequest:
        def __init__(self):
            self.output_format = None
            self.output_schema = None
            self.start_kwargs = None

        def input(self, _value):
            return self

        def instruct(self, _value):
            return self

        def output(self, value, *, format="auto"):
            self.output_schema = value
            self.output_format = format
            return self

        async def async_start(self, **kwargs):
            self.start_kwargs = kwargs
            return {"summary": "ok", "risk": "low"}

    model = FakeModelRequest()
    task = Agently.create_dynamic_task(
        "review contract",
        plan={
            "graph_id": "structured-contract-format",
            "task_schema_version": "task_dag/v1",
            "tasks": [
                {
                    "id": "final",
                    "kind": "model",
                    "inputs": {"output_format": "flat_markdown"},
                }
            ],
            "semantic_outputs": {"final": "final"},
        },
        model=model,
        output_schema={
            "summary": (str, "summary", True),
            "risk": (str, "risk", True),
        },
        ensure_keys=["summary", "risk"],
    )

    snapshot = await task.async_run(timeout=1)

    assert model.output_format == "auto"
    assert model.start_kwargs == {"ensure_keys": ["summary", "risk"]}
    assert snapshot["semantic_outputs"]["final"]["result"] == {"summary": "ok", "risk": "low"}


def test_agent_create_dynamic_task_consumes_prompt_snapshot_for_target_and_output_contract():
    agent = Agently.create_agent("dynamic-task-prompt-agent")
    agent.set_agent_prompt("info", {"customer": "Acme"})
    agent.set_request_prompt("instruct", "Focus on renewal risk.")
    task = (
        agent
        .input({"account": "Acme", "ticket": "T-42"})
        .output({"summary": (str, "risk summary", True)}, format="json")
        .create_dynamic_task()
    )

    assert "[INFO]:" in task.target
    assert "customer: Acme" in task.target
    assert "[INSTRUCT]:" in task.target
    assert "Focus on renewal risk." in task.target
    assert "[INPUT]:" in task.target
    assert "ticket: T-42" in task.target
    assert "[OUTPUT REQUIREMENT]" not in task.target
    assert task.output_schema == {"summary": (str, "risk summary", True)}
    assert task.output_format == "json"
    assert task._graph_input() == {"account": "Acme", "ticket": "T-42"}
    assert agent.request.prompt.get(inherit=False) == {}
    assert agent.agent_prompt.get("info") == {"customer": "Acme"}


def test_agent_create_dynamic_task_explicit_arguments_override_prompt_output_contract():
    agent = Agently.create_agent("dynamic-task-explicit-agent")
    task = (
        agent
        .input("prompt target")
        .output({"prompt_summary": (str, "prompt summary", True)}, format="json")
        .create_dynamic_task(
            "explicit target",
            output_schema={"explicit_summary": (str, "explicit summary", True)},
            output_format="hybrid",
        )
    )

    assert task.target == "explicit target"
    assert task.output_schema == {"explicit_summary": (str, "explicit summary", True)}
    assert task.output_format == "hybrid"
    assert task._graph_input() == "explicit target"


def test_task_dag_validator_prunes_unknown_optional_handler():
    graph = {
        "graph_id": "optional-prune",
        "tasks": [
            {"id": "core", "kind": "local"},
            {
                "id": "extra",
                "kind": "local",
                "binding": "missing_handler",
                "fallback": {"on_error": "skip"},
            },
        ],
        "semantic_outputs": {"final": "core"},
    }

    validation = TaskDAGValidator({"local": lambda context: context.task.id}).validate(graph)

    assert validation.task_ids == ("core",)
    assert validation.diagnostics[-1]["code"] == "dynamic_task.unknown_optional_binding_skipped"


def test_task_dag_validator_rejects_unknown_required_handler():
    graph = {
        "graph_id": "required-handler",
        "tasks": [
            {"id": "core", "kind": "local", "binding": "missing_handler"},
        ],
        "semantic_outputs": {"final": "core"},
    }

    with pytest.raises(ValueError, match="no executor resolver entry"):
        TaskDAGValidator({"local": lambda context: context.task.id}).validate(graph)
