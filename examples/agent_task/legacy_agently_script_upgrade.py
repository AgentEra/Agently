from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import find_dotenv, load_dotenv

from agently import Agently


TASK_MODEL_KEY = "task-main"

LEGACY_SCRIPT = """\
from agently import AgentFactory

agent = AgentFactory.create_agent()
print({"api": "legacy", "status": "created"})
"""

FORCED_GAP_LEGACY_SCRIPT = """\
from agently import AgentFactory
import json

agent = AgentFactory.create_agent()
print(json.dumps({"api": "legacy"}))
"""

CURRENT_API_GUIDANCE = """\
Current 4.1.x migration guidance for this example:

- Do not import or use AgentFactory.
- Use the package facade:

  from agently import Agently
  agent = Agently.create_agent("legacy-script-upgraded")

- The script must not call a model provider. It only needs to prove the current
  API can be imported and used to create an Agent object.
- Print strict JSON using json.dumps(...), not a Python dict repr.
- The expected final stdout is JSON containing api="4.1.x" and status="ok".
"""


ProviderName = Literal["deepseek", "ollama"]


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use AgentTaskLoop to repair a failing legacy Agently script and verify the fixed script."
    )
    parser.add_argument(
        "--workspace",
        default=os.getenv("AGENT_TASK_WORKSPACE", ""),
        help="TaskWorkspace directory. Defaults to agent-task-workspaces/legacy-script-upgrade-<run-id>.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=int(os.getenv("AGENT_TASK_MAX_ITERATIONS", "4")),
        help="Maximum AgentTaskLoop iterations.",
    )
    parser.add_argument(
        "--forced-gap",
        action="store_true",
        default=_env_bool("AGENT_TASK_LEGACY_FORCED_GAP"),
        help=(
            "Use a two-gap legacy fixture intended to prove verification-failed -> replan -> pass. "
            "This mode also limits each bounded step to one action so verification must drive the next iteration. "
            "The example still reports the actual model behavior instead of forcing a failed first pass."
        ),
    )
    return parser.parse_args(argv)


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


def _run_script(script_path: Path, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script_path.name)],
        cwd=script_path.parent,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _run_env() -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo_root) if not existing else f"{repo_root}{os.pathsep}{existing}"
    return env


def _validate_fixed_script(script_path: Path, final_run: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    script_text = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
    parsed_stdout: dict[str, Any] | None = None
    parse_error = ""
    try:
        raw = json.loads(final_run.stdout.strip())
        if isinstance(raw, dict):
            parsed_stdout = raw
    except Exception as error:
        parse_error = f"{error.__class__.__name__}: {error}"
    return {
        "script_exists": script_path.is_file(),
        "returncode": final_run.returncode,
        "stdout": final_run.stdout.strip(),
        "stderr": final_run.stderr.strip(),
        "stdout_json": parsed_stdout,
        "stdout_parse_error": parse_error,
        "does_not_use_agent_factory": "AgentFactory" not in script_text,
        "uses_current_facade": "Agently" in script_text and "create_agent" in script_text,
        "stdout_contract_ok": bool(
            parsed_stdout
            and parsed_stdout.get("api") == "4.1.x"
            and parsed_stdout.get("status") == "ok"
        ),
    }


def _print_stream_item(item: Any) -> None:
    value = item.value if isinstance(item.value, dict) else {}
    message = str(value.get("message") or "")
    stream_kind = (item.meta or {}).get("stream_kind")
    if stream_kind == "progress" and message:
        print(f"[PROGRESS] {message}", flush=True)
    elif stream_kind == "snapshot":
        stage = value.get("stage") or item.path.rsplit(".", 1)[-1]
        print(f"[SNAPSHOT] {stage}: {message}", flush=True)


async def main(argv: list[str] | None = None):
    args = parse_args(argv)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    workspace_dir = Path(
        args.workspace or f"agent-task-workspaces/legacy-script-upgrade-{run_id}"
    ).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    task_files_dir = workspace_dir / "files"
    task_files_dir.mkdir(parents=True, exist_ok=True)
    script_path = task_files_dir / "legacy_script.py"
    legacy_script = FORCED_GAP_LEGACY_SCRIPT if args.forced_gap else LEGACY_SCRIPT
    script_path.write_text(legacy_script, encoding="utf-8")

    run_env = _run_env()
    initial_run = _run_script(script_path, env=run_env)
    initial_failure_recorded = initial_run.returncode != 0

    agent = Agently.create_agent("agent-task-legacy-upgrade").use_task_workspace(workspace_dir).use_record_store(workspace_dir, mode="read_write")
    provider = configure_agent_model_pool(agent, temperature=0.0)
    task_workspace = agent.task_workspace
    record_store = agent.record_store

    agent.enable_task_workspace_file_actions(read=True, write=True, expose_to_model=True)
    agent.enable_shell(
        commands=["python legacy_script.py", f"{sys.executable} legacy_script.py"],
        action_id="run_task_command",
        desc=(
            "Run only the legacy_script.py validation command inside the task workspace. "
            "Use cmd='python legacy_script.py'. Omit workdir so the managed workspace file root is used."
        ),
        expose_to_model=True,
        timeout=20,
        env=run_env,
    )

    await record_store.put(
        content={
            "path": "legacy_script.py",
            "legacy_script_content": legacy_script,
            "current_api_guidance": CURRENT_API_GUIDANCE,
            "forced_gap": bool(args.forced_gap),
            "forced_gap_note": (
                "This fixture has two independent gaps: incompatible AgentFactory usage and an incomplete stdout JSON contract."
                if args.forced_gap
                else ""
            ),
            "initial_returncode": initial_run.returncode,
            "initial_stdout": initial_run.stdout,
            "initial_stderr": initial_run.stderr,
        },
        collection="observations",
        kind="legacy_script_initial_failure",
        summary="legacy_script.py fails before migration because AgentFactory is not available",
        scope={
            "task_id": "legacy_agently_script_upgrade",
            "execution_id": "legacy_agently_script_upgrade",
        },
        source={"type": "example_fixture", "phase": "initial_failure"},
    )

    print("[SETUP] Legacy script upgrade")
    print(f"[SETUP] TaskWorkspace: {workspace_dir}")
    print(f"[SETUP] Script: {script_path}")
    print(f"[SETUP] Provider: {provider}, model_key={TASK_MODEL_KEY}")
    print(f"[SETUP] Forced gap mode: {bool(args.forced_gap)}")
    print(f"[SETUP] Initial returncode: {initial_run.returncode}")
    print(f"[SETUP] Initial stderr: {initial_run.stderr.strip()[:240]}")

    execution = agent.create_task(
        task_id="legacy_agently_script_upgrade",
        goal=(
            "Upgrade legacy_script.py from an incompatible legacy Agently API to a current 4.1.x-compatible "
            "script. First use the recorded failure, legacy_script_content, and current_api_guidance from "
            "TaskWorkspace. Modify exactly the workspace-relative file path `legacy_script.py` through write_file. "
            "Verify by calling run_task_command with cmd='python legacy_script.py' and no workdir. The fixed "
            "script must use `from agently import Agently`, call `Agently.create_agent(...)`, avoid model "
            "provider calls, and print strict JSON with api='4.1.x' and status='ok'. "
            + (
                "In forced-gap mode, do not treat import migration alone as success; the stdout JSON contract and command validation are separate required evidence. "
                "Each bounded execution step is limited to one action, so use later iterations to close missing evidence."
                if args.forced_gap
                else ""
            )
        ),
        success_criteria=[
            "The original failure from legacy_script.py is recorded and used.",
            "legacy_script.py no longer imports or uses AgentFactory.",
            "legacy_script.py imports Agently and calls Agently.create_agent(...).",
            "Running python legacy_script.py succeeds.",
            "The final script stdout is strict JSON containing api='4.1.x' and status='ok'.",
        ],
        task_workspace=workspace_dir,
        max_iterations=max(1, int(args.max_iterations)),
        limits={"max_model_requests": 14, "max_seconds": 240, "max_no_progress_seconds": 90},
        options={
            "agent_task": {
                "request_timeout_seconds": 45,
                "stream_progress": True,
                "stream_snapshots": True,
            },
            "routes": {"model_request": {"action_loop": {"max_rounds": 1 if args.forced_gap else 6}}},
        },
    )

    stream_items = []
    stream_trace_path = workspace_dir / "outputs" / "legacy_script_upgrade_stream.jsonl"
    stream_trace_path.parent.mkdir(parents=True, exist_ok=True)
    with stream_trace_path.open("w", encoding="utf-8") as trace_file:
        async for item in execution.get_async_generator(type="instant"):
            stream_items.append(item)
            trace_file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
            trace_file.flush()
            _print_stream_item(item)

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    final_run = _run_script(script_path, env=run_env)
    validation = _validate_fixed_script(script_path, final_run)
    deterministic_validation_passed = bool(
        initial_failure_recorded
        and validation["returncode"] == 0
        and validation["does_not_use_agent_factory"]
        and validation["uses_current_facade"]
        and validation["stdout_contract_ok"]
    )
    replan_count = sum(1 for item in stream_items if item.path.endswith(".replan"))
    first_verification_failed = any(
        item.path == "agent_task.iteration.1.verification"
        and isinstance(item.value, dict)
        and isinstance(item.value.get("verification"), dict)
        and item.value["verification"].get("is_complete") is False
        for item in stream_items
    )
    summary = {
        "provider": provider,
        "forced_gap": bool(args.forced_gap),
        "task_status": result["status"],
        "accepted": bool(result.get("accepted", result.get("status") == "completed")),
        "artifact_status": str(result.get("artifact_status") or ("accepted" if result.get("status") == "completed" else "partial")),
        "deterministic_validation_passed": deterministic_validation_passed,
        "initial_failure_recorded": initial_failure_recorded,
        "first_verification_failed": first_verification_failed,
        "forced_gap_replan_proof": bool(args.forced_gap and first_verification_failed and replan_count >= 1 and result["status"] == "completed"),
        "replan_count": replan_count,
        "final_script_runs": validation["returncode"] == 0,
        "verification_passed": result["status"] == "completed",
        "record_store_recovery_ref_count": len(meta.get("record_refs", {}).get("checkpoints", [])),
        "workspace_process_record_count": len(meta.get("record_refs", {}).get("decisions", [])),
        "stream_trace_file": str(stream_trace_path),
        "script_path": str(script_path),
        "validation": validation,
    }
    print("[RESULT] Legacy script upgrade accepted" if deterministic_validation_passed else "[RESULT] Legacy script upgrade produced a partial artifact")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

# Expected key output from a real DeepSeek run on 2026-06-03:
# command:
#   AGENT_TASK_WORKSPACE=agent-task-workspaces/legacy-script-upgrade-cleanup-final python examples/agent_task/legacy_agently_script_upgrade.py
# task_status="completed"
# accepted=True
# artifact_status="accepted"
# deterministic_validation_passed=True
# initial_failure_recorded=True
# replan_count=0
# final_script_runs=True
# verification_passed=True
# record_store_recovery_ref_count=0 (recovery is opt-in)
# validation.stdout_json.api="4.1.x"
# validation.stdout_json.status="ok"
# stream_trace_file points to a JSONL stream trace under the TaskWorkspace
#
# Forced-gap validation command (requires a real DeepSeek or local Ollama run):
#   AGENT_TASK_WORKSPACE=agent-task-workspaces/legacy-forced-gap \
#   AGENT_TASK_LEGACY_FORCED_GAP=1 \
#   python examples/agent_task/legacy_agently_script_upgrade.py
# The forced-gap run reports forced_gap=True, first_verification_failed, and
# forced_gap_replan_proof from the actual stream trace. Do not update expected
# key output for this mode unless the command is re-run with a real model. This
# mode limits each bounded step to one action so missing validation evidence
# should be closed by the verifier-driven next iteration, not by hidden local
# control flow.
