# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared runtime helpers for the Blocks business examples.

This module intentionally contains execution glue only: model/provider setup,
runtime stream printing, Blocks case execution, and model-judge utilities. Each
business example keeps its own facts, prompts, handlers, and validation rules.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, TypedDict

from dotenv import find_dotenv, load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently


MODEL_TIMEOUT_SECONDS = float(os.getenv("BLOCKS_COMPLEXITY_MODEL_TIMEOUT_SECONDS", "120"))
ARTIFACTS_DIR = Path(__file__).resolve().parent / "_artifacts"
SUMMARY_PATH = ARTIFACTS_DIR / "blocks_business_complexity_ladder_summary.json"


class RequiredBusinessCase(TypedDict):
    case_id: str
    title: str
    graph: Any
    handlers: Mapping[str, Callable[..., Any]]


class BusinessCase(RequiredBusinessCase, total=False):
    runtime_resources: Mapping[str, Any]
    needs_model: bool


def configure_model() -> str:
    load_dotenv(find_dotenv(usecwd=True))
    configured = os.getenv("BLOCKS_COMPLEXITY_MODEL_PROVIDER", "").strip().lower()
    if configured in {"deepseek", "ollama"}:
        provider = configured
    elif os.getenv("DEEPSEEK_API_KEY"):
        provider = "deepseek"
    else:
        provider = "ollama"

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY for BLOCKS_COMPLEXITY_MODEL_PROVIDER=deepseek.")
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash"),
                "model_type": "chat",
                "auth": api_key,
                "request_options": {
                    "thinking": {"type": "disabled"},
                    "temperature": 0.2,
                },
            },
        )
    else:
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                "api_key": os.getenv("OLLAMA_API_KEY", "ollama"),
                "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
                "model_type": "chat",
                "request_options": {"temperature": 0.2},
            },
        )

    Agently.set_settings("agent_task.progress.language", os.getenv("AGENTLY_PROGRESS_LANGUAGE", "auto"))
    return provider


async def emit(context: Mapping[str, Any], item: Mapping[str, Any]) -> None:
    runtime_data = context["runtime_data"]
    await runtime_data.execution.async_put_into_stream(dict(item), _skip_contract_validation=True)


def output_for(context: Mapping[str, Any], plan_block_id: str) -> Any:
    state = context.get("state")
    if not isinstance(state, Mapping):
        return None
    results = state.get("execution_block_results", [])
    if not isinstance(results, list):
        return None
    for item in reversed(results):
        if not isinstance(item, Mapping):
            continue
        if item.get("source_plan_block_id") == plan_block_id:
            return item.get("output")
    return None


def all_outputs(context: Mapping[str, Any]) -> dict[str, Any]:
    state = context.get("state")
    results = state.get("execution_block_results", []) if isinstance(state, Mapping) else []
    collected: dict[str, Any] = {}
    if isinstance(results, list):
        for item in results:
            if isinstance(item, Mapping) and item.get("source_plan_block_id"):
                collected[str(item["source_plan_block_id"])] = item.get("output")
    return collected


def require_number(value: Any, label: str) -> float:
    if value is None:
        raise RuntimeError(f"{label} returned no numeric value.")
    return float(value)


async def generate_model_artifact(
    context: Mapping[str, Any],
    *,
    artifact: str,
    business_context: Mapping[str, Any],
    instructions: list[str],
    output_schema: Mapping[str, Any],
) -> dict[str, Any]:
    await emit(context, {"type": "business.progress", "message": f"Starting model artifact: {artifact}."})
    request = (
        Agently.create_request(f"blocks-{artifact}")
        .input({"artifact": artifact, "business_context": dict(business_context)})
        .instruct(
            [
                *instructions,
                "Use only the supplied business context and caller-provided TaskContext guidance.",
                "Do not invent hidden external facts, approvals, receipts, metrics, or tool results.",
            ]
        )
        .output(dict(output_schema), format="json")
    )
    result = request.get_result()
    async for item in result.get_async_generator(type="instant"):
        delta = getattr(item, "delta", None)
        if isinstance(delta, str) and delta:
            await emit(
                context,
                {
                    "type": "business.model_delta",
                    "artifact": artifact,
                    "field": str(getattr(item, "path", "model") or "model"),
                    "delta": delta,
                },
            )
    data = await asyncio.wait_for(result.async_get_data(max_retries=2), timeout=MODEL_TIMEOUT_SECONDS)
    if not isinstance(data, dict):
        raise RuntimeError(f"Model artifact {artifact} returned non-dict data: {data!r}")
    await emit(context, {"type": "business.progress", "message": f"Model artifact ready: {artifact}."})
    return {"artifact": artifact, "content": data}


async def model_judge(
    *,
    scenario: str,
    candidate: Mapping[str, Any],
    business_context: Mapping[str, Any],
    rules: list[str],
) -> dict[str, Any]:
    request = (
        Agently.create_request(f"blocks-judge-{scenario}")
        .input(
            {
                "scenario": scenario,
                "candidate": dict(candidate),
                "business_context": dict(business_context),
                "rules": rules,
            }
        )
        .instruct(
            [
                "Judge semantic compliance with each rule.",
                "Do not use keyword overlap as the primary signal.",
                "Use only the supplied candidate and business context.",
                "List any unsupported claim that is not grounded in the supplied business context.",
                "Set accepted true only when every required rule passes and unsupported_claims is empty.",
            ]
        )
        .output(
            {
                "accepted": (bool, "True only when all required rules pass.", True),
                "reason": (str, "Concise overall reason.", True),
                "unsupported_claims": ([str], "Claims not supported by the business context.", True),
                "rule_results": [
                    {
                        "rule": (str, "Rule being judged.", True),
                        "passed": (bool, "Whether the rule passed.", True),
                        "evidence": (str, "Specific evidence from candidate or context.", True),
                    }
                ],
            },
            format="json",
        )
    )
    judged = await asyncio.wait_for(request.get_result().async_get_data(max_retries=2), timeout=MODEL_TIMEOUT_SECONDS)
    if not isinstance(judged, dict):
        raise RuntimeError(f"Model judge returned non-dict data: {judged!r}")
    return judged


def compile_case(case_id: str, plan_blocks: list[dict[str, Any]], edges: list[dict[str, str]], **extra: Any) -> Any:
    return Agently.blocks.compile(
        {
            "plan_id": case_id,
            "plan_blocks": plan_blocks,
            "edges": edges,
            **extra,
        }
    )


async def close_after_start(execution: Any, start_task: asyncio.Task[Any]) -> dict[str, Any]:
    await start_task
    return await execution.async_close(timeout=30)


def print_stream_item(item: Any) -> None:
    if not isinstance(item, dict):
        print(f"[stream] {item}", flush=True)
        return
    item_type = str(item.get("type") or "")
    if item_type == "business.model_delta":
        artifact = str(item.get("artifact") or "artifact")
        field = str(item.get("field") or "field")
        print(f"\n[delta:{artifact}.{field}] {item.get('delta') or ''}", end="", flush=True)
        return
    if item_type == "business.progress":
        print(f"\n[progress] {item.get('message')}", flush=True)
        return
    if item_type == "business.validation":
        print(f"\n[validation] {item.get('scenario')} accepted={item.get('accepted')} {item.get('reason', '')}", flush=True)
        return
    if item_type.startswith("block."):
        block_id = item.get("execution_block_id")
        print(f"\n[{item_type}] {block_id}", flush=True)
        return
    if item_type == "blocks.graph.completed":
        print(f"\n[blocks.graph.completed] {item.get('source_plan_id')}", flush=True)


async def run_case(
    case_id: str,
    title: str,
    graph: Any,
    *,
    handlers: Mapping[str, Callable[..., Any]],
    extra_runtime_resources: Mapping[str, Any] | None = None,
    input_payload: Any | None = None,
) -> dict[str, Any]:
    print("\n" + "=" * 72)
    print(f"{case_id}: {title}")
    print("=" * 72)
    flow = Agently.blocks.bind_runtime(graph)
    runtime_resources: dict[str, Any] = {"blocks.handlers": dict(handlers)}
    if extra_runtime_resources:
        runtime_resources.update(dict(extra_runtime_resources))
    execution = flow.create_execution(
        auto_close=False,
        record_store=False,
        concurrency=4,
        runtime_resources=runtime_resources,
    )
    started_at = time.monotonic()
    start_task = asyncio.create_task(execution.async_start(input_payload or {"case_id": case_id}))
    close_task = asyncio.create_task(close_after_start(execution, start_task))
    stream_items: list[Any] = []
    async for item in execution.get_async_runtime_stream(timeout=None):
        stream_items.append(item)
        print_stream_item(item)
    snapshot = await close_task
    result = Agently.blocks.map_result(graph, snapshot)
    evidence = Agently.blocks.map_evidence(graph, snapshot)
    semantic_outputs = result.get("semantic_outputs", {})
    terminal_outputs = list(semantic_outputs.values())
    ok = bool(terminal_outputs and isinstance(terminal_outputs[-1], dict) and terminal_outputs[-1].get("ok"))
    summary = {
        "case_id": case_id,
        "ok": ok,
        "elapsed_seconds": round(time.monotonic() - started_at, 2),
        "stream_item_count": len(stream_items),
        "action_evidence_count": len(evidence.action_evidence),
        "skill_evidence_count": len(evidence.skill_evidence),
        "terminal_output": terminal_outputs[-1] if terminal_outputs else None,
    }
    print(f"\n[case.summary] {json.dumps(summary, ensure_ascii=False, indent=2)}")
    if not ok:
        raise AssertionError(f"{case_id} failed semantic or deterministic validation: {summary}")
    return summary


async def run_business_case(case: BusinessCase) -> dict[str, Any]:
    return await run_case(
        case["case_id"],
        case["title"],
        case["graph"],
        handlers=case["handlers"],
        extra_runtime_resources=case.get("runtime_resources"),
    )


def selected_case_ids() -> set[str]:
    return {item.strip() for item in os.getenv("BLOCKS_COMPLEXITY_CASES", "").split(",") if item.strip()}


async def run_business_cases(cases: list[BusinessCase], *, write_artifact: bool = False) -> list[dict[str, Any]]:
    selected = selected_case_ids()
    active_cases = [case for case in cases if not selected or case["case_id"] in selected]
    unknown = selected - {case["case_id"] for case in cases}
    if unknown:
        raise RuntimeError(f"Unknown BLOCKS_COMPLEXITY_CASES values: {sorted(unknown)}")

    provider = configure_model() if any(case.get("needs_model") for case in active_cases) else "not-required"
    print(f"[setup] model_provider={provider}")
    summaries = [await run_business_case(case) for case in active_cases]

    if write_artifact:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        SUMMARY_PATH.write_text(
            json.dumps({"provider": provider, "cases": summaries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("\n" + "=" * 72)
        print(f"[all.cases.accepted] wrote {SUMMARY_PATH}")
    return summaries
