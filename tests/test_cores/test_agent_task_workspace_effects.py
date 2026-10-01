from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.core.application.AgentTask import AgentTask
from agently.core.orchestration import resolve_task_board_planning_policy
from agently.types.data import AgentlyRequestData, TaskBoardCard, TaskBoardCardResult, TaskBoardRevision
from agently.utils import DataFormatter, Settings


def _agent(name: str):
    settings = Settings(name=f"{name}-settings", parent=Agently.settings)
    plugins = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-plugins")
    return Agently.AgentType(plugins, parent_settings=settings, name=name)


def _private_paths(root: Path) -> list[str]:
    private = root / ".agently"
    if not private.exists():
        return []
    return sorted(str(path.relative_to(root)) for path in private.rglob("*") if path.is_file())


class _FlatArtifactRequester:
    name = "FlatArtifactRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}

    def __init__(self, prompt: Any, settings: Settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
            request_options={"stream": True},
            request_url="mock://agent-task-workspace-effects",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        if "Verify the task against every success criterion" in text:
            payload = {
                "is_complete": True,
                "requires_block": False,
                "reason": "trusted file readback is present",
                "failure_analysis": "",
                "acceptance_delta": [],
                "missing_criteria": [],
                "replan_instruction": "",
                "repair_constraints": [],
                "next_step_requirements": [],
                "final_result_required": True,
                "final_result": "The report is available through the trusted file ref.",
                "criterion_checks": [
                    {
                        "criterion_id": "criterion:1",
                        "satisfied": True,
                        "summary": "The trusted TaskWorkspace readback contains the report.",
                        "gaps": [],
                        "evidence_ids": [],
                    }
                ],
                "material_claim_coverage_complete": True,
                "material_claim_checks": [],
            }
        elif "Plan the next bounded AgentExecution step" in text:
            payload = {
                "execution_shape": "direct",
                "step_instruction": "write the final report",
                "expected_evidence": "trusted file readback",
                "rationale": "one bounded step is enough",
                "deliverable_mode": "task_workspace_artifact",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "report prepared",
                "artifact_markdown": "# Final report\n\nWorkspace storage stays minimal.\n",
                "artifact_manifest": {"path": "reports/final.md"},
                "evidence": ["report body prepared"],
                "remaining_work": [],
            }
        else:
            payload = {"answer": "ok"}
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


def _flat_agent(name: str):
    settings = Settings(name=f"{name}-settings", parent=Agently.settings)
    plugins = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-plugins")
    plugins.register("ModelRequester", _FlatArtifactRequester, activate=True)
    return Agently.AgentType(plugins, parent_settings=settings, name=name)


@pytest.mark.asyncio
async def test_agent_task_process_state_stays_in_memory_by_default(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    task = AgentTask(
        _agent("agent-task-memory-only").use_task_workspace(root),
        task_id="agent-task-memory-only",
        goal="Return one bounded answer.",
        success_criteria=["The answer is returned."],
        execution="flat",
    )

    assert task.task_workspace.root == root.resolve()
    assert task.task_workspace.execution_id == task.id
    assert task.task_workspace.mode == "read_only"

    context = {"items": [], "diagnostics": {}}
    plan = {"step_instruction": "Answer once.", "execution_shape": "direct"}
    decision = await task._record_decision(1, plan, cast(Any, context))
    observation, checkpoint = await task._record_observation(
        1,
        plan=plan,
        decision_ref=decision,
        execution_result={"answer": "done"},
        execution_meta={"status": "completed", "execution_id": "bounded-1"},
    )
    verification = await task._record_verification(
        1,
        {"is_complete": True, "reason": "done"},
        observation,
    )
    reflection = await task._record_reflection(
        1,
        phase="major_node",
        subject_ref=verification,
        summary={"assessment": "done", "status": "accepted"},
    )
    await task._write_resume_snapshot(1, {"is_complete": True, "reason": "done"})

    assert cast(dict[str, Any], decision)["storage"] == "memory"
    assert cast(dict[str, Any], observation)["storage"] == "memory"
    assert cast(dict[str, Any], verification)["storage"] == "memory"
    assert reflection and cast(dict[str, Any], reflection)["storage"] == "memory"
    assert checkpoint is None
    assert _private_paths(root) == []


@pytest.mark.asyncio
async def test_taskboard_tick_does_not_materialize_record_store_storage(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    task = AgentTask(
        _agent("taskboard-memory-only").use_task_workspace(root),
        task_id="taskboard-memory-only",
        goal="Coordinate one card.",
        success_criteria=["The card completes."],
        execution="taskboard",
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-1",
            "graph": {
                "graph_id": f"{task.id}.graph",
                "cards": [
                    {
                        "id": "answer",
                        "objective": "Return the answer.",
                        "required_outputs": ["answer"],
                    }
                ],
            },
        }
    )

    revision_ref, checkpoint_ref = await task._record_taskboard_checkpoint(
        stage="tick",
        tick_index=1,
        revision=revision,
        runtime_topology={"ready": ["answer"]},
    )

    assert revision_ref is None
    assert checkpoint_ref is None
    assert task._latest_taskboard_acceptance_index
    assert _private_paths(root) == []


@pytest.mark.asyncio
async def test_agent_task_terminal_file_retention_uses_identity_state_without_database(
    tmp_path: Path,
):
    root = tmp_path / "project"
    root.mkdir()
    task = AgentTask(
        _agent("agent-task-file-retention").use_task_workspace(root, mode="read_only"),
        task_id="agent-task-file-retention",
        goal="Write a final report.",
        success_criteria=["The report is retained."],
        execution="flat",
    )
    draft = await task.task_workspace.write_file("report/draft.md", "draft")
    final = await task.task_workspace.write_file("report/final.md", "final")
    final_ref = cast(dict[str, Any], final["file_refs"][0])

    retained = await task._register_terminal_deliverables([cast(Any, final_ref)])
    task.result = {"status": "completed", "artifact_refs": retained}
    result = await task._apply_terminal_task_workspace_retention(status="completed")

    assert result and result["status"] == "applied"
    assert len(retained) == 1
    assert str(retained[0].get("locator_id") or "").startswith("loc_")
    assert str(retained[0].get("content_version_id") or "").startswith("cv_")
    assert retained[0]["sha256"] == final_ref["sha256"]
    assert not (root / cast(str, draft["path"])).exists()
    assert (root / cast(str, final["path"])).read_text(encoding="utf-8") == "final"
    private_paths = _private_paths(root)
    assert str(final["path"]) in private_paths
    assert ".agently/identity/state.json" in private_paths
    assert ".agently/identity/state.lock" in private_paths
    assert ".agently/workspace.db" not in private_paths


@pytest.mark.asyncio
async def test_terminal_artifact_resolves_stable_reference_tokens_to_source_cards(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    task = AgentTask(
        _agent("agent-task-reference-token").use_task_workspace(root),
        task_id="agent-task-reference-token",
        goal="Write a cited report.",
        success_criteria=["The report citation resolves."],
        execution="flat",
    )
    source = task._task_reference_catalog.add_evidence(
        {
            "id": "action.source",
            "kind": "agent_task.action.result",
            "action_call_id": "call-source",
            "source_url": "https://example.com/source",
            "status": "ok",
            "body_state": "bounded",
            "body": "source body",
        }
    )
    reference_id = str(source["reference_id"])
    final = await task.task_workspace.write_file(
        "report/final.md",
        f"# Final\n\nSupported claim [[ref:{reference_id}]].\n",
    )

    retained = await task._register_terminal_deliverables([cast(Any, final["file_refs"][0])])

    assert len(retained) == 1
    token_diagnostic = task.diagnostics["reference_tokens"][str(final["path"])]
    assert token_diagnostic["status"] == "validated"
    assert token_diagnostic["reference_ids"] == [reference_id]
    assert token_diagnostic["source_cards"] == [
        {
            "reference_id": reference_id,
            "kind": "agent_task.action.result",
            "source_role": "action",
            "source_url": "https://example.com/source",
        }
    ]
    await task._apply_terminal_task_workspace_retention(status="completed")
    manifest_path = next((root / ".agently" / "identity" / "tasks").glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert reference_id in manifest["task_reference_catalog"]["references"]
    persisted_evidence = next(iter(manifest["task_reference_catalog"]["evidence"].values()))
    assert "body" not in persisted_evidence["target"]


@pytest.mark.asyncio
async def test_terminal_artifact_unknown_reference_token_fails_closed_and_legacy_alias_is_marked(
    tmp_path: Path,
):
    unknown_root = tmp_path / "unknown"
    unknown_root.mkdir()
    unknown_task = AgentTask(
        _agent("agent-task-reference-token-unknown").use_task_workspace(unknown_root),
        task_id="agent-task-reference-token-unknown",
        goal="Write a cited report.",
        success_criteria=["The report citation resolves."],
        execution="flat",
    )
    unknown = await unknown_task.task_workspace.write_file("report.md", "Unknown [[ref:ref_Z]].")

    retained = await unknown_task._register_terminal_deliverables([cast(Any, unknown["file_refs"][0])])

    assert retained == []
    assert unknown_task._terminal_retention_deferred is True
    assert unknown_task.diagnostics["task_workspace_retention"]["diagnostics"][-1]["code"] == (
        "agent_task.retention.reference_token_invalid"
    )

    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    legacy_task = AgentTask(
        _agent("agent-task-reference-token-legacy").use_task_workspace(legacy_root),
        task_id="agent-task-reference-token-legacy",
        goal="Retain a legacy report for explicit re-verification.",
        success_criteria=["The report is retained without guessing aliases."],
        execution="flat",
    )
    legacy = await legacy_task.task_workspace.write_file("legacy.md", "Legacy citation (e1).")

    legacy_retained = await legacy_task._register_terminal_deliverables([cast(Any, legacy["file_refs"][0])])

    assert len(legacy_retained) == 1
    assert legacy_task.diagnostics["reference_tokens"][str(legacy["path"])]["status"] == (
        "legacy_reference_unverified"
    )


@pytest.mark.asyncio
async def test_flat_agent_task_completes_with_final_file_and_identity_manifest(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    execution = _flat_agent("flat-agent-task-effects").create_task(
        task_id="flat-agent-task-effects",
        goal="Write a final report.",
        success_criteria=["A final report is written and read back."],
        execution="flat",
        task_workspace=root,
        max_iterations=1,
    )

    result = await execution.run()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert len(result["artifact_refs"]) == 1
    ref = result["artifact_refs"][0]
    assert ref["type"] == "file"
    assert ref["execution_id"] == execution.id
    assert ref["locator_id"].startswith("loc_")
    assert ref["content_version_id"].startswith("cv_")
    assert (root / ref["path"]).read_text(encoding="utf-8").startswith("# Final report")
    private_paths = _private_paths(root)
    assert ref["path"] in private_paths
    assert ".agently/workspace.db" not in private_paths
    assert all(path == ref["path"] or path.startswith(".agently/identity/") for path in private_paths)


@pytest.mark.asyncio
async def test_taskboard_full_run_completes_with_identity_manifest_without_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "project"
    root.mkdir()
    task = AgentTask(
        _agent("taskboard-full-effects").use_task_workspace(root),
        task_id="taskboard-full-effects",
        goal="Write a TaskBoard report.",
        success_criteria=["The report is written and read back."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value({
        "id": "write", "objective": "Write the report.", "required_outputs": ["report"],
        "metadata": {"final_task_workspace_deliverables": ["reports/final.md"]},
    })
    revision = TaskBoardRevision.from_value(
        {
            "board_id": task.id,
            "revision_id": "rev-0",
            "graph": {"graph_id": f"{task.id}.graph", "cards": [card.to_dict()]},
        }
    )
    planning_policy = resolve_task_board_planning_policy(
        "medium", metadata={"execution_strategy": "taskboard", "task_id": task.id}
    )

    async def build_context() -> dict[str, Any]:
        return {"goal": task.goal, "profile": "none", "items": [], "omitted": [], "diagnostics": {}}

    async def request_plan(_context: Mapping[str, Any], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(revision=revision, planning_policy=planning_policy)

    async def run_card(context: Any, _context: Mapping[str, Any]) -> TaskBoardCardResult:
        write = await task.task_workspace.write_file("reports/final.md", "# TaskBoard final\n")
        ref = cast(dict[str, Any], write["file_refs"][0])
        ref["role"] = "task_workspace_artifact"
        ref["source"] = "agent_task.task_workspace_artifact.taskboard_test"
        return TaskBoardCardResult(
            card_id=context.card.id,
            status="completed",
            preview={"status": "completed", "final_result": "report ready", "remaining_work": []},
            file_refs=(cast(Any, ref),),
        )

    async def verify(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "trusted TaskBoard file ref is present",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": "report ready",
            "criterion_checks": [
                {
                    "criterion_id": "criterion:1",
                    "satisfied": True,
                    "summary": "The trusted TaskBoard TaskWorkspace readback contains the report.",
                    "gaps": [],
                    "evidence_ids": [],
                }
            ],
            "material_claim_coverage_complete": True,
            "material_claim_checks": [],
        }

    async def no_finalizer(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("completed terminal card should not need another finalizer")

    monkeypatch.setattr(cast(Any, task), "_build_context", build_context)
    monkeypatch.setattr(cast(Any, task), "_request_taskboard_plan", request_plan)
    monkeypatch.setattr(cast(Any, task), "_run_taskboard_card", run_card)
    monkeypatch.setattr(cast(Any, task), "_request_verification", verify)
    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", no_finalizer)

    result = await task.async_run()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert len(result["artifact_refs"]) == 1
    ref = result["artifact_refs"][0]
    assert ref["locator_id"].startswith("loc_")
    assert ref["content_version_id"].startswith("cv_")
    assert (root / ref["path"]).read_text(encoding="utf-8") == "# TaskBoard final\n"
    private_paths = _private_paths(root)
    assert ref["path"] in private_paths
    assert ".agently/workspace.db" not in private_paths
    assert all(path == ref["path"] or path.startswith(".agently/identity/") for path in private_paths)
