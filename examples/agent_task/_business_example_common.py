from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import find_dotenv, load_dotenv


ProviderName = Literal["deepseek", "ollama"]
TASK_MODEL_KEY = "task-main"


def configure_agent_model_pool(agent: Any, *, temperature: float = 0.0) -> ProviderName:
    load_dotenv(find_dotenv(usecwd=True))
    configured = os.getenv("AGENT_TASK_MODEL_PROVIDER", "").strip().lower()
    if configured in {"deepseek", "ollama"}:
        provider = cast(ProviderName, configured)
    elif os.getenv("DEEPSEEK_API_KEY"):
        provider = "deepseek"
    else:
        provider = "ollama"

    configured_model_pool = agent.settings.get("model_pool", {}) or {}
    configured_profiles = agent.settings.get("model_profiles", {}) or {}
    configured_key_pools = agent.settings.get("api_key_pools", {}) or {}
    model_pool = cast(dict[str, Any], dict(configured_model_pool) if isinstance(configured_model_pool, dict) else {})
    model_profiles = cast(dict[str, Any], dict(configured_profiles) if isinstance(configured_profiles, dict) else {})
    api_key_pools = cast(dict[str, Any], dict(configured_key_pools) if isinstance(configured_key_pools, dict) else {})

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY. Set it or run with AGENT_TASK_MODEL_PROVIDER=ollama.")
        model_pool[TASK_MODEL_KEY] = "agent-task-deepseek-main"
        model_profiles["agent-task-deepseek-main"] = {
            "provider": "OpenAICompatible",
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash"),
            "model_type": "chat",
            "api_key_pool": "agent-task-deepseek",
            "request_options": {
                "thinking": {"type": "disabled"},
                "temperature": temperature,
            },
        }
        api_key_pools["agent-task-deepseek"] = {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "primary", "value": api_key}],
        }
    else:
        model_pool[TASK_MODEL_KEY] = "agent-task-ollama-main"
        model_profiles["agent-task-ollama-main"] = {
            "provider": "OpenAICompatible",
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
            "model_type": "chat",
            "api_key_pool": "agent-task-ollama",
            "request_options": {"temperature": temperature},
        }
        api_key_pools["agent-task-ollama"] = {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "local", "value": os.getenv("OLLAMA_API_KEY", "ollama")}],
        }

    agent.settings.set("model_pool", model_pool)
    agent.settings.set("model_profiles", model_profiles)
    agent.settings.set("api_key_pools", api_key_pools)
    agent.settings.set("action.planning_model_key", TASK_MODEL_KEY)
    agent.activate_model(TASK_MODEL_KEY)
    return provider


def default_workspace(prefix: str) -> Path:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    return Path(os.getenv("AGENT_TASK_WORKSPACE", "") or f"agent-task-workspaces/{prefix}-{run_id}").resolve()


def resolve_result_artifact_path(task_workspace: Any, result: dict[str, Any], requested_path: str) -> Path:
    """Resolve one trusted task file ref without assuming a physical file layout."""

    root = Path(str(task_workspace.root)).resolve()
    direct = (root / requested_path).resolve()
    if direct.is_file():
        return direct
    raw_refs = result.get("artifact_refs", [])
    refs = raw_refs if isinstance(raw_refs, list) else []
    requested = Path(requested_path)
    for raw_ref in refs:
        if not isinstance(raw_ref, dict) or raw_ref.get("type") != "file":
            continue
        path_text = str(raw_ref.get("path") or "")
        candidate = Path(path_text)
        target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target.is_file() and (
            candidate == requested
            or candidate.parts[-len(requested.parts) :] == requested.parts
        ):
            return target
    return direct


def print_stream_item(item: Any) -> None:
    value = item.value if isinstance(item.value, dict) else {}
    message = str(value.get("message") or "")
    stream_kind = (item.meta or {}).get("stream_kind")
    if stream_kind == "progress" and message:
        print(f"[PROGRESS] {message}", flush=True)
    elif stream_kind == "snapshot":
        stage = value.get("stage") or item.path.rsplit(".", 1)[-1]
        print(f"[SNAPSHOT] {stage}: {message}", flush=True)


async def judge_business_artifact(
    agent: Any,
    *,
    scenario: str,
    artifact_text: str,
    business_context: dict[str, Any],
    rules: list[str],
) -> dict[str, Any]:
    if not artifact_text.strip():
        return {
            "accepted": False,
            "reason": "No artifact text was available for model judging.",
            "rule_results": [],
        }
    try:
        timeout_seconds = float(os.getenv("AGENT_TASK_JUDGE_TIMEOUT_SECONDS", "90"))
        request = (
            agent.create_request(model_key=TASK_MODEL_KEY)
            .input(
                {
                    "scenario": scenario,
                    "candidate_artifact": artifact_text,
                    "business_context": business_context,
                    "rules": rules,
                }
            )
            .instruct(
                "Judge the candidate artifact using only the supplied business context and rules. "
                "The business context may contain incomplete or conflicting facts; decide whether the artifact handles them responsibly. "
                "Return JSON only with per-rule evidence. Do not rely on keyword counting as the primary signal."
            )
            .output(
                {
                    "accepted": (bool, "True only when every rule is satisfied.", True),
                    "reason": (str, "Concise overall reason.", True),
                    "rule_results": [
                        {
                            "rule": (str, "Rule text or short name.", True),
                            "ok": (bool, "Whether the candidate satisfies this rule.", True),
                            "evidence": (str, "Concrete evidence from the candidate artifact.", True),
                        }
                    ],
                },
                format="json",
            )
            .async_start(max_retries=2, raise_ensure_failure=False)
        )
        judged = await asyncio.wait_for(request, timeout=timeout_seconds)
    except TimeoutError:
        return {
            "accepted": False,
            "reason": "Model judge timed out before returning a verdict.",
            "rule_results": [],
        }
    except Exception as error:
        return {
            "accepted": False,
            "reason": f"Model judge failed: {error.__class__.__name__}: {error}",
            "rule_results": [],
        }
    if not isinstance(judged, dict):
        return {
            "accepted": False,
            "reason": f"Model judge returned non-dict output: {judged!r}",
            "rule_results": [],
        }
    return {
        "accepted": bool(judged.get("accepted")),
        "reason": str(judged.get("reason") or ""),
        "rule_results": judged.get("rule_results") if isinstance(judged.get("rule_results"), list) else [],
    }


def write_summary(summary: dict[str, Any]) -> None:
    print("[RESULT] Run summary JSON:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
