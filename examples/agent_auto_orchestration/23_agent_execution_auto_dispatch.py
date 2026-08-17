"""AgentExecution automatic route dispatch for GitHub issue triage.

Run:
    python examples/agent_auto_orchestration/23_agent_execution_auto_dispatch.py

Environment:
    DEEPSEEK_API_KEY in the shell or .env file.
    Set DYNAMIC_TASK_MODEL_PROVIDER=ollama for local Ollama instead.

This example focuses on dispatch judgement with a real external task:

    quick prompt only
        -> AgentExecution route: model_request

    goal + success_criteria on the execution draft
        -> AgentExecution route: agent_task
        -> task step calls fetch_github_open_issues as a GitHub data Action
        -> task refs are exposed through AgentExecutionResult/meta

GitHub issue data is fetched from the live AgentEra/Agently repository. Model
calls are real; issue grouping, pending-work judgement, and the final summary
are model-owned. The host only provides a data-fetching Action for the external
GitHub system. The task intentionally covers the first visible issue page rather
than a full repository audit so it remains a focused AgentExecution dispatch
example.

Expected key output from one real DeepSeek run on 2026-07-08:
    GitHub issue triage for AgentEra/Agently
    ========================================
    ## Overview
    The first page of open issues for AgentEra/Agently contains 8 issues...
    ## Still-Pending Issues
    All 8 fetched issues are open and require maintainer follow-up:
    ...
    ## Recommended Maintainer Actions
    - #312 - Immediate security review and patch...
    Execution evidence
    ------------------
    Provider: deepseek
    Quick prompt route: model_request (category: issue_triage)
    Auto-dispatched route: agent_task (selected by execution_strategy, strategy: task)
    Task status: completed
    GitHub fetch action called: yes
    TaskWorkspace observations recorded: yes
    TaskWorkspace artifact readback: yes
    Task refs include task id: yes
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently
from examples.dynamic_task._shared import configure_model


RUNTIME_ROOT = ROOT / ".example_runtime" / "agent_auto_orchestration" / "agent_execution_auto_dispatch"
AUTO_TASK_ID = "auto-github-issue-triage"
GITHUB_REPO = "AgentEra/Agently"


def configure_leaf_model_settings(provider: str) -> None:
    Agently.set_settings("OpenAICompatible.stream", False)
    if provider == "deepseek":
        Agently.set_settings("OpenAICompatible.base_url", os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
        Agently.set_settings("OpenAICompatible.auth", os.getenv("DEEPSEEK_API_KEY"))
        Agently.set_settings("OpenAICompatible.model_type", "chat")
        Agently.set_settings("OpenAICompatible.model", os.getenv("AGENT_EXECUTION_EXAMPLE_MODEL", "deepseek-v4-flash"))
        return
    Agently.set_settings("OpenAICompatible.base_url", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    Agently.set_settings("OpenAICompatible.auth", os.getenv("OLLAMA_API_KEY", "ollama"))
    Agently.set_settings("OpenAICompatible.model_type", "chat")
    Agently.set_settings("OpenAICompatible.model", os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"))


def register_github_issue_action(agent: Any) -> None:
    @agent.action_func
    def fetch_github_open_issues(repo: str, limit: int = 20) -> dict[str, Any]:
        """Fetch recent open GitHub issues from a public repository's GitHub issues page."""
        normalized_repo = repo.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", normalized_repo):
            return {"status": "invalid_repo", "repo": repo, "issues": []}
        safe_limit = max(1, min(int(limit or 8), 8))
        page_url = f"https://github.com/{normalized_repo}/issues"
        page_result = _fetch_github_issue_page(normalized_repo, page_url, safe_limit)
        if page_result.get("status") == "ok" and page_result.get("issues"):
            return page_result

        api_result = _fetch_github_issue_api(normalized_repo, safe_limit)
        if api_result.get("status") == "ok" and api_result.get("issues"):
            return api_result
        return {
            "status": "unavailable",
            "repo": normalized_repo,
            "source_url": page_url,
            "page_result": page_result,
            "api_result": api_result,
            "issues": [],
        }

    agent.use_actions(fetch_github_open_issues)
    agent.set_action_loop(max_rounds=1, timeout=120)


def _fetch_github_issue_page(repo: str, page_url: str, limit: int) -> dict[str, Any]:
    request = urllib.request.Request(
        page_url,
        headers={
            "Accept": "text/html",
            "User-Agent": "Mozilla/5.0 Agently-AgentExecution-example",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            html_text = response.read().decode("utf-8", errors="replace")
    except Exception as error:
        return {
            "status": "request_error",
            "repo": repo,
            "source": "github_html",
            "source_url": page_url,
            "error_type": error.__class__.__name__,
            "error": str(error),
            "issues": [],
        }
    pattern = rf'href="(/{re.escape(repo)}/issues/(\d+))"[^>]*>(.*?)</a>'
    issues: list[dict[str, Any]] = []
    seen_numbers: set[str] = set()
    for href, number, raw_title in re.findall(pattern, html_text, re.S):
        if number in seen_numbers:
            continue
        title = re.sub(r"<.*?>", "", raw_title)
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        seen_numbers.add(number)
        issues.append(
            {
                "number": int(number),
                "title": title,
                "url": f"https://github.com{href}",
                "state": "open",
            }
        )
        if len(issues) >= limit:
            break
    return {
        "status": "ok" if issues else "empty",
        "repo": repo,
        "source": "github_html",
        "source_url": page_url,
        "issue_count": len(issues),
        "issues": issues,
    }


def _fetch_github_issue_api(repo: str, limit: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
            "per_page": limit,
        }
    )
    api_url = f"https://api.github.com/repos/{repo}/issues?{query}"
    page_url = f"https://github.com/{repo}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Agently-AgentExecution-example",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return {
            "status": "http_error",
            "repo": repo,
            "source": "github_api",
            "source_url": page_url,
            "error_code": error.code,
            "issues": [],
        }
    except Exception as error:
        return {
            "status": "request_error",
            "repo": repo,
            "source": "github_api",
            "source_url": page_url,
            "error_type": error.__class__.__name__,
            "error": str(error),
            "issues": [],
        }
    issues = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        labels = item.get("labels", [])
        issues.append(
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "url": item.get("html_url"),
                "state": item.get("state"),
                "labels": [
                    label.get("name")
                    for label in labels
                    if isinstance(label, dict) and label.get("name")
                ],
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "comments": item.get("comments"),
            }
        )
    return {
        "status": "ok",
        "repo": repo,
        "source": "github_api",
        "source_url": page_url,
        "issue_count": len(issues),
        "issues": issues,
    }


def task_action_ids(meta: dict[str, Any]) -> list[str]:
    task_meta = meta.get("logs", {}).get("route_logs", {}).get("agent_task", {})
    if not isinstance(task_meta, dict):
        return []
    ids: list[str] = []
    for iteration in task_meta.get("iterations", []):
        if not isinstance(iteration, dict):
            continue
        logs = iteration.get("execution_meta", {}).get("logs", {})
        if not isinstance(logs, dict):
            continue
        for action_log in logs.get("action_logs", []):
            if isinstance(action_log, dict):
                action_id = str(action_log.get("action_id") or action_log.get("id") or "")
                if action_id:
                    ids.append(action_id)
    return ids


def task_workspace_refs(meta: dict[str, Any]) -> dict[str, Any]:
    task_refs = meta.get("task_refs", {})
    if not isinstance(task_refs, dict):
        return {}
    workspace_refs = task_refs.get("workspace_refs", {})
    return workspace_refs if isinstance(workspace_refs, dict) else {}


def final_result_text(data: dict[str, Any]) -> str:
    final_result = data.get("final_result")
    if final_result is None and isinstance(data.get("verification"), dict):
        final_result = data["verification"].get("final_result")
    if final_result is None and "raw" in data:
        final_result = data["raw"]
    return str(final_result or "").strip()


def artifact_preview_from_task_envelope(data: Any) -> str:
    """Return the first full text artifact preview carried by a task envelope."""
    if isinstance(data, dict):
        for key in ("artifact_preview", "artifact_markdown", "preview", "body"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("file_refs",):
            refs = data.get(key)
            if isinstance(refs, list):
                for ref in refs:
                    preview = artifact_preview_from_task_envelope(ref)
                    if preview:
                        return preview
        for key in (
            "final_result",
            "verification",
            "artifact_manifest",
            "workspace_artifact_delivery",
            "result",
            "execution_result",
        ):
            preview = artifact_preview_from_task_envelope(data.get(key))
            if preview:
                return preview
    elif isinstance(data, list):
        for item in data:
            preview = artifact_preview_from_task_envelope(item)
            if preview:
                return preview
    return ""


def task_artifact_text(data: dict[str, Any], runtime_root: Path, task_id: str, pointer: str) -> str:
    _ = task_id
    preview = artifact_preview_from_task_envelope(data)
    if preview:
        return preview
    artifact_paths: list[str] = []

    def collect_file_refs(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "file" and isinstance(value.get("path"), str):
                artifact_paths.append(str(value["path"]))
            for nested in value.values():
                collect_file_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_file_refs(nested)

    collect_file_refs(data.get("artifact_refs", []))
    match = re.search(r"TaskWorkspace artifact delivered at ([^;]+)", pointer)
    if match:
        artifact_paths.append(match.group(1).strip())
    artifact_paths.append("final.md")
    for relative_path in artifact_paths:
        candidate = (runtime_root / relative_path).resolve()
        try:
            candidate.relative_to(runtime_root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return ""


async def run_direct_model_request(agent: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    result = (
        agent
        .input(
            {
                "repo": GITHUB_REPO,
                "request": "Summarize currently open GitHub issues and identify maintainer follow-up items.",
            }
        )
        .instruct("Classify the request. Do not fetch GitHub data in this quick prompt.")
        .output(
            {
                "category": (str, "Use exactly one of: issue_triage, documentation, release, other.", True),
                "reason": (str, "One concise reason.", True),
            },
            format="json",
        )
        .get_result()
    )
    data = await result.async_get_data()
    meta = await result.async_get_meta()
    return data if isinstance(data, dict) else {"raw": data}, meta


async def run_auto_task_dispatch(agent: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    execution = (
        agent
        .create_execution(
            lineage={"task_id": AUTO_TASK_ID, "repo": GITHUB_REPO},
            limits={"max_model_requests": 8, "max_seconds": 420, "max_no_progress_seconds": 300},
            options={
                "task": {
                    "max_iterations": 3,
                    "options": {
                        "agent_task": {
                            "request_timeout_seconds": 180,
                            "stream_progress": False,
                            "stream_snapshots": True,
                        },
                    },
                },
            },
        )
        .goal(
            f"Review the first GitHub issues page for currently open issues in {GITHUB_REPO}. First call the "
            "fetch_github_open_issues Action for that repository with a limit of 8. Summarize the fetched open "
            "work and list which fetched issues still need maintainer follow-up, including issue numbers and titles. "
            "When producing the final summary, copy issue titles exactly from the fetched Action result; do not say "
            "titles are unavailable if they appear in the fetched evidence. "
            "Return the final result as readable Markdown with sections for overview, still-pending issues, and "
            "recommended maintainer actions.",
            [
                f"The execution evidence includes a fetch_github_open_issues Action call for {GITHUB_REPO}.",
                "The final result summarizes the fetched open issue set from the GitHub page.",
                "The final result names specific fetched still-pending issue numbers and titles.",
                "The final result calls out at least one recommended next handling action for maintainers.",
                "The final result is readable by a maintainer without inspecting execution metadata.",
            ]
        )
    )
    result = execution.get_result()
    data = await result.async_get_data()
    full_data = await result.async_get_full_data()
    meta = await result.async_get_meta()
    return (
        data if isinstance(data, dict) else {"raw": data},
        full_data if isinstance(full_data, dict) else {"raw": full_data},
        meta,
    )


async def main() -> None:
    provider = configure_model(temperature=0.0)
    configure_leaf_model_settings(provider)
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)

    agent = Agently.create_agent("agent-execution-auto-dispatch").use_task_workspace(RUNTIME_ROOT)
    agent.define(
        prompt={
            "rule": (
                "Use only fetched GitHub issue facts for issue summaries. "
                "Do not invent issue numbers, labels, states, or maintainer decisions."
            )
        }
    )

    direct_data, direct_meta = await run_direct_model_request(agent)
    register_github_issue_action(agent)
    auto_data, auto_full_data, auto_meta = await run_auto_task_dispatch(agent)

    auto_route = auto_meta.get("route", {})
    auto_route_options = auto_route.get("options", {}) if isinstance(auto_route, dict) else {}
    auto_task_refs = auto_meta.get("task_refs", {})
    auto_action_ids = task_action_ids(auto_meta)
    auto_workspace_refs = task_workspace_refs(auto_meta)
    pointer_text = final_result_text(auto_data)
    artifact_text = task_artifact_text(auto_full_data, RUNTIME_ROOT, AUTO_TASK_ID, pointer_text)
    summary = artifact_text or pointer_text
    selected_route = auto_route.get("selected_route") if isinstance(auto_route, dict) else None
    selected_by = auto_route.get("selected_by") if isinstance(auto_route, dict) else None
    task_strategy = auto_route_options.get("strategy") if isinstance(auto_route_options, dict) else None
    fetch_called = "fetch_github_open_issues" in auto_action_ids
    observations_recorded = bool(auto_workspace_refs.get("observations"))
    refs_include_task_id = bool(auto_task_refs.get("task_id")) if isinstance(auto_task_refs, dict) else False

    print(f"GitHub issue triage for {GITHUB_REPO}")
    print("=" * (len("GitHub issue triage for ") + len(GITHUB_REPO)))
    print(summary or "(No final issue summary was produced.)")
    print()
    print("Execution evidence")
    print("------------------")
    print(f"Provider: {provider}")
    print(
        "Quick prompt route: "
        f"{direct_meta.get('route', {}).get('selected_route')} "
        f"(category: {direct_data.get('category')})"
    )
    print(
        "Auto-dispatched route: "
        f"{selected_route} (selected by {selected_by}, strategy: {task_strategy})"
    )
    print(f"Task status: {auto_full_data.get('status')}")
    print(f"GitHub fetch action called: {'yes' if fetch_called else 'no'}")
    print(f"TaskWorkspace observations recorded: {'yes' if observations_recorded else 'no'}")
    print(f"TaskWorkspace artifact readback: {'yes' if artifact_text else 'no'}")
    print(f"Task refs include task id: {'yes' if refs_include_task_id else 'no'}")


if __name__ == "__main__":
    asyncio.run(main())
