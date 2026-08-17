from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal, cast

from dotenv import find_dotenv, load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agently import Agently


ProviderName = Literal["longcat", "deepseek", "ollama"]
TASK_MODEL_KEY = "task-main"


def _select_provider() -> ProviderName:
    configured = os.getenv("AGENT_TASK_EXAMPLE_MODEL_PROVIDER", "").strip().lower()
    if configured in {"longcat", "deepseek", "ollama"}:
        return cast(ProviderName, configured)
    if os.getenv("LONGCAT_API_KEY"):
        return "longcat"
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "ollama"


def configure_agent_model_pool(agent: Any, *, temperature: float = 0.0) -> ProviderName:
    load_dotenv(find_dotenv(usecwd=True))
    provider = _select_provider()

    model_pool = dict(agent.settings.get("model_pool", {}) or {})
    model_profiles = dict(agent.settings.get("model_profiles", {}) or {})
    api_key_pools = dict(agent.settings.get("api_key_pools", {}) or {})

    if provider == "longcat":
        api_key = os.getenv("LONGCAT_API_KEY")
        if not api_key:
            raise RuntimeError("Missing LONGCAT_API_KEY. Set it or choose another provider.")
        model_pool[TASK_MODEL_KEY] = "agent-task-example-longcat"
        model_profiles["agent-task-example-longcat"] = {
            "provider": "OpenAICompatible",
            "base_url": os.getenv("LONGCAT_BASE_URL", "https://api.longcat.chat/openai/v1"),
            "model": os.getenv("LONGCAT_MODEL", "LongCat-Flash-Chat"),
            "model_type": "chat",
            "api_key_pool": "agent-task-example-longcat",
            "request_options": {"temperature": temperature},
        }
        api_key_pools["agent-task-example-longcat"] = {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "primary", "value": api_key}],
        }
    elif provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY. Set it or choose another provider.")
        model_pool[TASK_MODEL_KEY] = "agent-task-example-deepseek"
        model_profiles["agent-task-example-deepseek"] = {
            "provider": "OpenAICompatible",
            "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash"),
            "model_type": "chat",
            "api_key_pool": "agent-task-example-deepseek",
            "request_options": {
                "thinking": {"type": "disabled"},
                "temperature": temperature,
            },
        }
        api_key_pools["agent-task-example-deepseek"] = {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "primary", "value": api_key}],
        }
    else:
        model_pool[TASK_MODEL_KEY] = "agent-task-example-ollama"
        model_profiles["agent-task-example-ollama"] = {
            "provider": "OpenAICompatible",
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
            "model_type": "chat",
            "api_key_pool": "agent-task-example-ollama",
            "request_options": {"temperature": temperature},
        }
        api_key_pools["agent-task-example-ollama"] = {
            "selection": {"strategy": "fixed"},
            "keys": [{"id": "local", "value": os.getenv("OLLAMA_API_KEY", "ollama")}],
        }

    agent.settings.set("model_pool", model_pool)
    agent.settings.set("model_profiles", model_profiles)
    agent.settings.set("api_key_pools", api_key_pools)
    agent.settings.set("action.planning_model_key", TASK_MODEL_KEY)
    agent.settings.set(
        "skills.runtime.stage_model_keys",
        {
            "planner": TASK_MODEL_KEY,
            "research": TASK_MODEL_KEY,
            "reason": TASK_MODEL_KEY,
            "reason_fast": TASK_MODEL_KEY,
            "executor": TASK_MODEL_KEY,
            "verifier": TASK_MODEL_KEY,
            "reflector": TASK_MODEL_KEY,
            "finalizer": TASK_MODEL_KEY,
        },
    )
    agent.activate_model(TASK_MODEL_KEY)
    return provider


def default_workspace(prefix: str) -> Path:
    base = Path(os.getenv("AGENT_TASK_EXAMPLE_WORKSPACE", ".agently/examples/agent_task_experiments"))
    return (base / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}").resolve()


def create_task_agent(
    name: str,
    *,
    workspace_prefix: str,
    language: str = "auto",
    temperature: float = 0.0,
) -> tuple[Any, ProviderName, Path]:
    workspace = default_workspace(workspace_prefix)
    workspace.mkdir(parents=True, exist_ok=True)
    agent = Agently.create_agent(name).use_task_workspace(workspace, mode="read_write")
    provider = configure_agent_model_pool(agent, temperature=temperature)
    if language != "auto":
        agent.language(language)
    return agent, provider, workspace


def enable_coding_workspace(agent: Any) -> None:
    from agently.builtins.actions import RuntimePreflight

    task_workspace = getattr(agent, "task_workspace", None)
    if task_workspace is None:
        raise RuntimeError("Coding examples require a TaskWorkspace-bound agent.")
    root = Path(str(task_workspace.root)).resolve()
    RuntimePreflight().register_actions(agent.action, action_id="inspect_code_runtimes")
    agent.enable_task_workspace_file_actions(
        root=root,
        read=True,
        write=True,
        search=True,
        list_files=True,
        coding_agent=True,
        expose_to_model=True,
    )
    agent.enable_shell(
        root=root,
        commands=[
            "pwd",
            "ls",
            f"{Path(sys.executable).expanduser().resolve()} reconcile.py",
            "python3 reconcile.py",
            "python reconcile.py",
            "node reconcile.js",
            "go run reconcile.go",
            "g++ reconcile.cpp",
            "clang++ reconcile.cpp",
            "./reconcile",
        ],
        action_id="run_bash",
        desc=(
            "Only execute or compile the generated reconcile program. Runtime discovery "
            "must use inspect_code_runtimes, not shell version commands."
        ),
        timeout=60,
    )


def require_mcp_runtime() -> None:
    missing = [
        package
        for package in ("fastmcp", "mcp")
        if importlib.util.find_spec(package) is None
    ]
    if missing:
        raise RuntimeError(
            "The mixed MCP examples require optional MCP runtime packages: "
            f"{', '.join(missing)}. Install them in the active environment before "
            "running examples 06-08; the examples intentionally do not install "
            "runtime dependencies for you."
        )


def amap_mcp_transport() -> str:
    load_dotenv(find_dotenv(usecwd=True))
    amap_key = os.getenv("AMAP_API_KEY")
    if not amap_key:
        raise RuntimeError("AMAP_API_KEY is required for the real AMap MCP travel example.")
    return f"https://mcp.amap.com/mcp?key={amap_key}"


@contextmanager
def sanitized_proxy_env() -> Iterator[None]:
    names = ("NO_PROXY", "no_proxy")
    original = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            value = os.environ.get(name)
            if value:
                os.environ[name] = ",".join(part for part in value.split(",") if part.strip() != "::1/128")
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def install_local_skill(skill_dir: Path, *, registry_root: Path) -> str:
    Agently.skills_executor.configure(
        registry_root=str(registry_root),
        allowed_trust_levels=["local"],
    )
    contract = Agently.skills_executor.install_skills(
        skill_dir,
        trust_level="local",
        update=True,
    )
    skill_id = str(contract.get("skill_id") or "").strip()
    if not skill_id:
        raise RuntimeError(f"Local Skill did not return a skill_id: {skill_dir}")
    return skill_id


def enable_workspace_report_actions(agent: Any) -> None:
    task_workspace = getattr(agent, "task_workspace", None)
    if task_workspace is None:
        raise RuntimeError("Report examples require a TaskWorkspace-bound agent.")
    root = Path(str(task_workspace.root)).resolve()
    agent.enable_task_workspace_file_actions(
        root=root,
        read=True,
        write=True,
        search=True,
        list_files=True,
        expose_to_model=True,
    )


def compact_text(value: Any, *, max_chars: int = 900) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f" ... [truncated from {len(text)} chars]"


def stream_options() -> dict[str, Any]:
    return {"agent_task": {"stream_progress": True}}


def run_and_print(execution: Any, *, provider: ProviderName, task_workspace: Path) -> dict[str, Any]:
    return asyncio.run(
        async_run_and_print(execution, provider=provider, task_workspace=task_workspace)
    )


async def async_run_and_print(
    execution: Any,
    *,
    provider: ProviderName,
    task_workspace: Path,
) -> dict[str, Any]:
    print("[DELTA_STREAM]")
    delta_text = ""
    async for chunk in execution.get_async_generator(type="delta"):
        if not chunk:
            continue
        delta_text += str(chunk)
        print(chunk, end="", flush=True)
    if delta_text and not delta_text.endswith("\n"):
        print()

    result = execution.get_result()
    data = await result.async_get_data()
    full_data = await result.async_get_full_data()
    meta = await result.async_get_meta()
    terminal_data: Mapping[str, Any] = (
        full_data if isinstance(full_data, Mapping) else {}
    )
    nested_result = terminal_data.get("result")
    if not terminal_data.get("status") and isinstance(nested_result, Mapping):
        terminal_data = nested_result
    task_refs = meta.get("task_refs", {}) if isinstance(meta, dict) else {}
    route_logs = meta.get("logs", {}).get("route_logs", {}) if isinstance(meta, dict) else {}
    task_log = route_logs.get("agent_task", {}) if isinstance(route_logs, dict) else {}
    summary = {
        "provider": provider,
        "status": terminal_data.get("status", ""),
        "accepted": terminal_data.get("accepted"),
        "artifact_status": terminal_data.get("artifact_status"),
        "missing_criteria": terminal_data.get("missing_criteria"),
        "execution_strategy": (
            terminal_data.get("execution_strategy")
        )
        or (task_refs.get("execution_strategy") if isinstance(task_refs, dict) else None)
        or (task_log.get("execution_strategy") if isinstance(task_log, dict) else None),
        "task_workspace": str(task_workspace),
        "delta_chars": len(delta_text),
        "final_preview": compact_text(data.get("final_result") if isinstance(data, dict) else data),
    }
    print("[SUMMARY]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
