"""Unified AgentExecution quick prompt and task-loop strategy.

Run:
    python examples/agent_auto_orchestration/22_unified_agent_execution_result.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

This example demonstrates the unified AgentExecution shape:

    Agent definition state:
        agent.define(...)

    One quick prompt execution:
        execution = agent.input(...).output(...)
        result = execution.get_result()

    One long-task strategy execution:
        execution = agent.create_task_loop(...)
        execution = execution.input(...)
        result = execution.get_result()

The business data is mocked. The model owns classification, drafting,
verification, and task-loop judgement.

Expected key output from one real DeepSeek run on 2026-07-08:
    provider=deepseek
    quick_result_type=AgentExecutionResult
    quick_category=renewal_risk
    quick_meta_has_execution_id=True
    task_strategy=task_loop
    task_result_status=completed
    task_refs_have_task_id=True
    task_snapshot_count=5
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from agently.core import AgentExecutionResult
from agently.types.data import AgentExecutionMeta
from examples.dynamic_task._shared import configure_model


RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "unified_agent_execution_result"

ACCOUNT_SIGNAL: dict[str, Any] = {
    "account_id": "acct-nova-27",
    "plan": "enterprise",
    "renewal_days": 21,
    "usage_trend": "down 34% over 30 days",
    "support_sentiment": "two unresolved admin-seat complaints",
    "champion_status": "primary champion left the company",
    "commercial_note": "procurement requested a month-to-month fallback quote",
}


async def collect_stream_paths(execution: Any) -> list[str]:
    paths: list[str] = []
    async for item in execution.get_async_generator(type="instant"):
        path = str(getattr(item, "path", ""))
        if path:
            paths.append(path)
    return paths


async def run_quick_prompt(agent: Any) -> tuple[dict[str, Any], AgentExecutionMeta]:
    execution = (
        agent
        .input({"account_signal": ACCOUNT_SIGNAL})
        .instruct(
            "Classify the renewal situation. Use only the provided account signal. "
            "Return concise fields for an account manager."
        )
        .output(
            {
                "category": (str, "Use exactly one of: renewal_risk, expansion, stable.", True),
                "reason": (str, "One sentence grounded in the account signal.", True),
                "next_action": (str, "One concrete account-manager action.", True),
            },
            format="json",
        )
    )
    result = execution.get_result()
    if not isinstance(result, AgentExecutionResult):
        raise TypeError(f"Expected AgentExecutionResult, got {type(result).__name__}")
    data = await result.async_get_data()
    meta = await result.async_get_meta()
    return data if isinstance(data, dict) else {"raw": data}, meta


async def run_task_strategy(agent: Any) -> tuple[dict[str, Any], AgentExecutionMeta, list[str]]:
    workspace = getattr(agent, "workspace", None)
    if workspace is None:
        raise RuntimeError("TaskWorkspace is required for task-loop strategy examples.")
    await workspace.put(
        content=ACCOUNT_SIGNAL,
        collection="observations",
        kind="account_signal",
        summary="acct-nova-27 renewal risk signal: usage down, unresolved complaints, champion left, procurement fallback quote",
        scope={"task_id": "renewal-risk-brief"},
        source={"type": "mock_business_system", "name": "account_health_signal"},
    )

    execution = (
        agent.create_task_loop(
            task_id="renewal-risk-brief",
            goal=(
                "Create a concise renewal-risk brief for acct-nova-27. Use the provided account signal "
                "as the only business evidence. The final result should name the risk, the evidence, "
                "and one next action for the account manager."
            ),
            success_criteria=[
                "The final result identifies the renewal risk clearly.",
                "The result cites at least two facts from the account signal.",
                "The result gives one concrete account-manager next action.",
            ],
            task_workspace=RUNTIME_ROOT,
            max_iterations=2,
            limits={"max_model_requests": 8, "max_seconds": 240, "max_no_progress_seconds": 120},
            options={"agent_task": {"stream_snapshots": True, "request_timeout_seconds": 90}},
        )
        .input({"account_signal": ACCOUNT_SIGNAL})
    )

    stream_paths = await collect_stream_paths(execution)
    result = execution.get_result()
    data = await result.async_get_full_data()
    meta = await result.async_get_meta()
    return data if isinstance(data, dict) else {"raw": data}, meta, stream_paths


async def main() -> None:
    provider = configure_model(temperature=0.0)
    Agently.set_settings("OpenAICompatible.stream", False)
    if provider == "deepseek":
        Agently.set_settings("OpenAICompatible.model", os.getenv("AGENT_EXECUTION_EXAMPLE_MODEL", "deepseek-v4-flash"))
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)

    agent = Agently.create_agent("unified-agent-execution-result").use_task_workspace(RUNTIME_ROOT)
    agent.define(
        prompt={
            "rule": (
                "Keep every output grounded in the provided business facts. "
                "Do not invent account history or commercial commitments."
            )
        }
    )
    agent.define().role("You write concise operator-facing recommendations.")

    quick_data, quick_meta = await run_quick_prompt(agent)
    task_data, task_meta, task_stream_paths = await run_task_strategy(agent)

    task_refs = task_meta.get("task_refs", {})
    snapshot_count = sum(1 for path in task_stream_paths if ".snapshot" in path or path.endswith(".context"))

    print(f"provider={provider}")
    print("quick_result_type=AgentExecutionResult")
    print(f"quick_category={quick_data.get('category')}")
    print(f"quick_meta_has_execution_id={bool(quick_meta.get('execution_id'))}")
    print(f"task_strategy={task_refs.get('strategy')}")
    print(f"task_result_status={task_data.get('status')}")
    print(f"task_refs_have_task_id={bool(task_refs.get('task_id'))}")
    print(f"task_snapshot_count={snapshot_count}")


if __name__ == "__main__":
    asyncio.run(main())
