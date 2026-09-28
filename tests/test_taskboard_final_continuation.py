"""Synthetic protocol regressions; not model-quality evidence."""
from __future__ import annotations

import pytest

from agently import Agently
from agently.core import AgentTask
from agently.types.data import TaskBoardRevision


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    task = AgentTask(Agently.create_agent().use_task_workspace(tmp_path),
                     goal="Complete the report using the supplied facts.",
                     success_criteria=["Include both sections."], execution="taskboard")
    revision = TaskBoardRevision.from_value({
        "board_id": task.id, "revision_id": "rev-1", "status": "completed",
        "graph": {"graph_id": "report", "cards": [{"id": "draft", "objective": "Write report"}]},
        "card_results": {"draft": {"card_id": "draft", "status": "completed",
                                   "preview": {"final_result": "Partial report", "remaining_work": []}}},
    })
    response = {"accepted": False, "reason": "Second section missing.",
                "missing_criteria": ["Second section"], "final_result": "Partial report",
                "replan_signal": {"status": "repair", "reason": "Use existing facts", "evidence_refs": []}}
    calls = {"finalizer": 0, "verifier": 0, "retrieval": 0}

    async def finalizer(*args, **kwargs):
        calls["finalizer"] += 1
        return dict(response)

    async def verifier(*args, **kwargs):
        calls["verifier"] += 1
        raise AssertionError("Ordinary continuation must not request a second semantic verdict")

    async def retrieval(*args, **kwargs):
        calls["retrieval"] += 1
        return {"query_groups": [{"query": "missing facts", "source_kind": "task_workspace", "max_results": 2}]}

    monkeypatch.setattr(task, "_request_taskboard_final", finalizer)
    monkeypatch.setattr(task, "_request_verification", verifier)
    monkeypatch.setattr(task, "_request_taskboard_final_evidence_retrieval_plan", retrieval)
    return task, revision, response, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["repair", "replan_segment", "blocked", "clarify"])
async def test_finalizer_routes_its_own_continuation(boundary, status):
    task, revision, response, calls = boundary
    response["replan_signal"]["status"] = status
    result = await task._finalize_taskboard(revision, context_pack={})
    assert calls["finalizer"] == 1 and calls["verifier"] == 0
    assert calls["retrieval"] == (status == "replan_segment")
    if status in {"repair", "replan_segment"}:
        assert result["terminal"] is False and result["status"] == "repair_requested"
        repaired = TaskBoardRevision.from_value(result["revision"])
        assert repaired.card_results["draft"].preview["final_result"] == "Partial report"
        assert any(card.id.startswith("final-verification-repair") for card in repaired.graph.cards)
        assert any(card.id.startswith("final-verification-evidence") for card in repaired.graph.cards) == (status == "replan_segment")
    else:
        assert result == {"terminal": True, "status": "blocked"}
        assert task.result["final_result"] == "Partial report"


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted,status,refs", [
    (True, "repair", []), (False, "continue", []),
    (False, "invented", []), (False, "replan_goal", []),
    (False, "repair", ["unoffered"]), (True, "continue", ["unoffered"]),
])
async def test_invalid_finalizer_signal_cannot_accept_or_schedule_work(boundary, accepted, status, refs):
    task, revision, response, calls = boundary
    response["accepted"] = accepted
    response["replan_signal"].update(status=status, evidence_refs=refs)
    result = await task._finalize_taskboard(revision, context_pack={})
    assert result == {"terminal": True, "status": "blocked"}
    assert task.result["accepted"] is False
    assert calls == {"finalizer": 1, "verifier": 0, "retrieval": 0}


@pytest.mark.asyncio
async def test_unchanged_finalizer_repair_converges(boundary):
    task, revision, response, calls = boundary
    results = [await task._finalize_taskboard(revision, context_pack={}) for _ in range(3)]
    assert results[0]["status"] == "repair_requested"
    assert results[-1] == {"terminal": True, "status": "blocked"}
    assert calls["verifier"] == 0


@pytest.mark.asyncio
async def test_continuation_returns_to_existing_lifecycle_and_completes(boundary, monkeypatch):
    from agently.types.data import TaskBoardCardResult

    task, revision, response, calls = boundary
    task._resumed_taskboard_state = {"revision": revision.to_dict()}
    executed = []

    async def repair_card(context, context_pack):
        executed.append(context.card.id)
        assert "Preserve the current inline answer format" in context.card.objective
        response.update(accepted=True, final_result="Complete report", missing_criteria=[])
        response["replan_signal"].update(status="continue", reason="Both sections present")
        return TaskBoardCardResult(card_id=context.card.id, status="completed",
                                   preview={"final_result": "Complete report", "remaining_work": []})

    monkeypatch.setattr(task, "_run_taskboard_card", repair_card)
    result = await task.async_run()
    assert result["accepted"] is True and result["status"] == "completed"
    assert len(executed) == 1
    assert calls == {"finalizer": 2, "verifier": 0, "retrieval": 0}


@pytest.mark.asyncio
async def test_missing_signal_keeps_partial_result_without_guessing_repair(boundary):
    task, revision, response, calls = boundary
    response.pop("replan_signal")
    result = await task._finalize_taskboard(revision, context_pack={})
    assert result == {"terminal": True, "status": "blocked"}
    assert task.result["final_result"] == "Partial report"
    assert calls == {"finalizer": 1, "verifier": 0, "retrieval": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("required_path", [False, True])
async def test_inline_repair_carrier_preserves_text_but_honors_delivery_paths(boundary, monkeypatch, required_path):
    from types import SimpleNamespace
    from agently.builtins.plugins.AgentExecution.long_task.BlockCarrier import WorkUnitResult

    task, revision, response, calls = boundary
    transition = await task._finalize_taskboard(revision, context_pack={})
    repaired = TaskBoardRevision.from_value(transition["revision"])
    card = next(card for card in repaired.graph.cards if card.id.startswith("final-verification-repair"))
    assert card.evidence_contract["deliverable_mode"] == "inline_final"
    if required_path:
        task._taskboard_planned_task_workspace_deliverables = ["final.md"]
    context = SimpleNamespace(card=card, revision=repaired,
                              dependency_results=dict(revision.card_results), planning_policy=None)

    async def work(**kwargs):
        return ({"status": "completed", "sufficient": True, "next_board_action": "finalize",
                 "candidate_final_result": "Complete report", "final_result": "Complete report",
                 "artifact_markdown": "", "artifact_manifest": {}, "remaining_work": []},
                {"execution_id": "synthetic-control", "status": "completed",
                 "logs": {"action_logs": [], "route_logs": {}, "errors": []}},
                WorkUnitResult(id=str(kwargs["work_unit"].id), status="completed"))

    monkeypatch.setattr(task, "_run_work_unit_through_blocks", work)
    result = await task._run_taskboard_control_card(context, {})
    assert result.status == "completed"
    if required_path:
        assert result.file_refs
        assert task.task_workspace.resolve_file_path(
            f"working/taskboard/{card.id}/terminal-candidates/final.md"
        ).read_text() == "Complete report"
    else:
        assert not result.file_refs and not result.artifact_refs
        assert result.preview["candidate_final_result"] == "Complete report"
        assert not task.task_workspace.resolve_file_path("final.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [True, False])
async def test_finalizer_references_must_belong_to_current_evidence(boundary, current):
    task, revision, response, calls = boundary
    evidence = task._task_reference_catalog.add_evidence({
        "id": "source-data", "kind": "action_evidence", "action_call_id": "read-data",
        "body_state": "full", "status": "ok", "body": "Supplied report facts",
    })
    response["replan_signal"]["evidence_refs"] = [evidence["reference_id"]]
    result = await task._finalize_taskboard(
        revision, context_pack={},
        prepared_outputs={"evidence_ledger": {"items": [evidence] if current else []}},
    )
    assert result["status"] == ("repair_requested" if current else "blocked")
    assert calls["verifier"] == 0
