from __future__ import annotations

import asyncio
import os

from dotenv import find_dotenv, load_dotenv

from agently import Agently


load_dotenv(find_dotenv(usecwd=True))
if os.getenv("DEEPSEEK_API_KEY"):
    PROVIDER = "deepseek"
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "auth": os.environ["DEEPSEEK_API_KEY"],
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash"),
            "model_type": "chat",
            "request_options": {
                "thinking": {"type": "disabled"},
                "temperature": 0,
            },
        },
    )
else:
    PROVIDER = "ollama"
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            "api_key": os.getenv("OLLAMA_API_KEY", "ollama"),
            "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
            "model_type": "chat",
            "request_options": {"temperature": 0},
        },
    )


GOAL = (
    "Set up a new open-source project: create the repository, add CI, write "
    "the README, configure linting, and define branch protection."
)

DELIVERY_SCHEMA = {
    "delivery_summary": (str, "summary of the project setup plan", "not_null"),
    "parallel_workstreams": ([str], "workstreams that can proceed independently", True),
    "ordered_followups": (str, "follow-up work that depends on earlier tasks", "not_null"),
}

SUBMITTED_PLAN = {
    "graph_id": "open-source-project-setup",
    "task_schema_version": "task_dag/v1",
    "tasks": [
        {
            "id": "analyze_repository_setup",
            "kind": "model",
            "title": "Analyze repository setup",
            "purpose": "Analyze repository, README, and branch-protection needs in the goal.",
        },
        {
            "id": "analyze_automation_setup",
            "kind": "model",
            "title": "Analyze automation setup",
            "purpose": "Independently analyze CI and linting workstreams in the goal.",
        },
        {
            "id": "synthesize_delivery_plan",
            "kind": "model",
            "title": "Synthesize delivery plan",
            "purpose": (
                "Use both dependency_results to combine parallel workstreams and "
                "their ordering constraints into one practical setup plan."
            ),
            "depends_on": ["analyze_repository_setup", "analyze_automation_setup"],
        },
        {
            "id": "write_delivery_summary",
            "kind": "model",
            "title": "Write delivery summary",
            "purpose": "Return dependency_results as the requested structured delivery summary.",
            "depends_on": ["synthesize_delivery_plan"],
        },
    ],
    "semantic_outputs": {"final_summary": "write_delivery_summary"},
}


async def main():
    task = Agently.create_dynamic_task(
        target=GOAL,
        plan=SUBMITTED_PLAN,
        output_schema=DELIVERY_SCHEMA,
        ensure_keys=["delivery_summary", "parallel_workstreams", "ordered_followups"],
    )
    validation = task.validate(SUBMITTED_PLAN, strict_schema_version=True)
    snapshot = await task.async_run(
        graph_input={"goal": GOAL},
        timeout=180,
        concurrency=3,
    )
    semantic_output = snapshot["semantic_outputs"]["final_summary"]
    final_result = semantic_output["result"]
    task_ids = list(validation.topological_task_ids)
    root_task_ids = list(validation.root_task_ids)
    join_task_ids = [
        item.id for item in validation.graph.tasks if len(item.depends_on) >= 2
    ]

    print(f"provider={PROVIDER}")
    print(f"task_ids={','.join(task_ids)}")
    print(f"root_task_ids={','.join(root_task_ids)}")
    print(f"join_task_ids={','.join(join_task_ids)}")
    print(f"semantic_final_task={semantic_output['task_id']}")
    print("[DELIVERY_SUMMARY]")
    print(final_result)

    assert task_ids == [
        "analyze_repository_setup",
        "analyze_automation_setup",
        "synthesize_delivery_plan",
        "write_delivery_summary",
    ]
    assert root_task_ids == ["analyze_repository_setup", "analyze_automation_setup"]
    assert join_task_ids == ["synthesize_delivery_plan"]
    assert semantic_output["task_id"] == "write_delivery_summary"
    assert str(final_result["delivery_summary"]).strip()
    assert isinstance(final_result["parallel_workstreams"], list)
    assert str(final_result["ordered_followups"]).strip()


if __name__ == "__main__":
    asyncio.run(main())


# Expected key output from a real DeepSeek run on 2026-07-11:
# provider=deepseek
# task_ids=analyze_repository_setup,analyze_automation_setup,synthesize_delivery_plan,write_delivery_summary
# root_task_ids=analyze_repository_setup,analyze_automation_setup
# join_task_ids=synthesize_delivery_plan
# semantic_final_task=write_delivery_summary
# parallel_workstreams contains four items and ordered_followups is non-empty.
#
# How it works:
# The application submits a stable TaskDAG whose tasks use real model requests.
# Dynamic Task validates the graph, TaskDAGExecutor runs the two roots concurrently
# under concurrency=3, and the declared dependency edges control the join and final
# output. No local pending/completed scheduler or runtime TriggerFlow compilation is
# needed; TriggerFlow remains the execution substrate under TaskDAG.
