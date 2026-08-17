from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.core.application.AgentTask import AgentTask


def _canonical_action_ref(manager: Any, ref: dict[str, Any], scope: dict[str, str]) -> dict[str, Any]:
    transfer = manager.read_selection_transfer(ref["selection_key"], expected_scope=scope)
    assert transfer is not None
    return transfer[0]


def test_action_artifact_scope_is_private_and_returned_values_are_defensive() -> None:
    agent: Any = Agently.create_agent("action-artifact-private-scope")
    manager = agent.action._artifact_manager
    scope_a = {"kind": "agent_execution", "id": "execution-a"}
    scope_b = {"kind": "agent_execution", "id": "execution-b"}
    artifact = manager.register_execution_artifact(
        action_call_id="private-scope-call",
        artifact_type="action_output",
        label="Private scope artifact",
        value={"nested": {"value": "original"}},
        artifact_scope=scope_a,
    )
    artifact_id = artifact["artifact_id"]

    artifact["meta"]["artifact_scope"] = scope_b
    artifact["meta"]["external"] = True
    first_read = manager.get_artifact(artifact_id)
    assert first_read is not None
    assert first_read["meta"]["artifact_scope"] == scope_a
    assert "external" not in first_read["meta"]

    first_read["meta"]["artifact_scope"] = scope_b
    first_read["value"]["nested"]["value"] = "mutated"
    first_value = manager.get_artifact_value(artifact_id)
    first_value["nested"]["value"] = "mutated again"
    assert manager.get_artifact_scope(artifact_id) == scope_a
    assert manager.get_artifact_value(artifact_id) == {"nested": {"value": "original"}}
    assert manager.release_scope(scope_b) == 0
    assert manager.get_artifact_value(artifact_id) is not None
    assert manager.release_scope(scope_a) == 1
    assert manager.get_artifact_value(artifact_id) is None


def test_action_artifact_keeps_exact_private_value_and_redacts_preview_only() -> None:
    agent: Any = Agently.create_agent("action-artifact-exact-private-value")
    exact_value = {"token": "exact-secret", "nested": {"body": "full-value"}}
    artifact = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="exact-private-call",
        artifact_type="action_output",
        label="Exact private value",
        value=exact_value,
    )

    assert artifact["preview"]["token"] == "[REDACTED]"
    assert agent.action._artifact_manager.get_artifact_value(artifact["artifact_id"]) == exact_value


@pytest.mark.asyncio
async def test_direct_action_entry_releases_success_and_failure_scopes(tmp_path, monkeypatch) -> None:
    agent: Any = Agently.create_agent("action-artifact-direct-scope-release")
    action_id = "direct_scope_release"
    agent.action.register_action(
        action_id=action_id,
        desc="Return a large direct Action value.",
        kwargs={},
        func=lambda: {"body": "d" * (1024 * 1024)},
    )

    record = await agent.action.async_execute_action(action_id, {})
    serialized_record = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")
    assert len(serialized_record) <= 16000, len(serialized_record)
    assert ("d" * 100).encode("utf-8") not in serialized_record
    assert record["artifact_refs"]
    returned_ref = next(
        ref for ref in record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    assert returned_ref["available"] is False
    assert returned_ref["full_value_available"] is False
    assert returned_ref.get("preview") or returned_ref.get("preview_omitted") is True
    assert returned_ref["selection_key"]
    assert returned_ref["artifact_id"]
    assert len(returned_ref["sha256"]) == 64
    model_visible = next(iter(agent.action.to_action_results([record]).values()))
    assert set(model_visible["artifact_refs"][0]).isdisjoint(
        {"artifact_id", "action_call_id", "sha256", "size", "bytes", "meta"}
    )
    assert record["model_digest"]
    readback = agent.action.read_action_artifact(
        selection_key=returned_ref["selection_key"],
    )
    assert readback["ok"] is False
    assert readback["status"] == "not_found"
    assert agent.action._artifact_manager._artifacts == {}

    captured_id = ""
    original_finalize = agent.action._finalize_action_result

    def fail_after_registration(result: Any, *, artifact_scope: dict[str, str] | None = None) -> Any:
        nonlocal captured_id
        finalized = original_finalize(result, artifact_scope=artifact_scope)
        captured_id = agent.action._artifact_manager.get_artifact_id_for_selection(
            finalized["artifact_refs"][0]["selection_key"]
        ) or ""
        raise RuntimeError("finalization failed after artifact registration")

    monkeypatch.setattr(agent.action, "_finalize_action_result", fail_after_registration)
    with pytest.raises(RuntimeError, match="finalization failed"):
        await agent.action.async_execute_action(action_id, {})
    assert captured_id
    assert agent.action._artifact_manager.get_artifact_value(captured_id) is None


@pytest.mark.asyncio
async def test_direct_action_entry_bounds_complete_large_instruction_record() -> None:
    agent: Any = Agently.create_agent("action-artifact-direct-large-instruction")
    action_id = "direct_large_instruction"
    marker = "DIRECT_LARGE_INSTRUCTION_MUST_NOT_RETURN_RAW"
    agent.action.register_action(
        action_id=action_id,
        desc="Consume a large instruction and return a compact fact.",
        kwargs={"code": (str, "Instruction body.")},
        func=lambda code: {"received_bytes": len(code.encode("utf-8"))},
    )

    record = await agent.action.async_execute_action(
        action_id,
        {"code": ("q" * (1024 * 1024)) + marker},
    )

    serialized_record = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")
    assert len(serialized_record) <= 16000, len(serialized_record)
    assert marker.encode("utf-8") not in serialized_record
    assert record["artifact_refs"]
    assert all(ref.get("available") is False for ref in record["artifact_refs"])
    assert agent.action._artifact_manager._artifacts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
async def test_standalone_action_flows_release_success_scope(
    flow_name: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    agent: Any = Agently.create_agent(f"action-artifact-standalone-{flow_name}")
    action_id = f"standalone_{flow_name}"
    output_marker = f"{flow_name}_RAW_OUTPUT_TAIL_MUST_STAY_COLD"
    agent.action.register_action(
        action_id=action_id,
        desc="Return a large standalone flow value.",
        kwargs={},
        func=lambda: {"body": ("f" * (1024 * 1024)) + output_marker},
    )
    flow = agent.action._flow_controller.create_named_action_flow(flow_name)
    from agently import TriggerFlow

    captured_executions: list[Any] = []
    original_create_execution = TriggerFlow.create_execution

    def capture_execution(trigger_flow: Any, **kwargs: Any) -> Any:
        execution = original_create_execution(trigger_flow, **kwargs)
        captured_executions.append(execution)
        return execution

    monkeypatch.setattr(TriggerFlow, "create_execution", capture_execution)

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [{"action_id": action_id, "action_input": {}, "purpose": "produce output"}],
        }

    async def execution_handler(context: dict[str, Any], request: dict[str, Any]) -> list[Any]:
        return [
            await agent.action.async_execute_action(
                str(command["action_id"]),
                dict(command.get("action_input") or {}),
                purpose=str(command.get("purpose") or ""),
                artifact_scope=context["artifact_scope"],
            )
            for command in request["action_calls"]
        ]

    records = await flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(),
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=2,
    )

    assert records
    returned_refs: list[dict[str, Any]] = []

    def collect_refs(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("artifact_type") == "action_output" and value.get("artifact_id"):
                returned_refs.append(value)
            for nested in value.values():
                collect_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_refs(nested)

    collect_refs(records)
    serialized_records = json.dumps(records, ensure_ascii=False)
    assert len(serialized_records.encode("utf-8")) <= 16000
    assert output_marker not in serialized_records
    assert all(ref["available"] is False for ref in returned_refs)
    assert all(ref["full_value_available"] is False for ref in returned_refs)
    assert all(ref.get("preview") or ref.get("preview_omitted") is True for ref in returned_refs)
    assert all(ref.get("sha256") for ref in returned_refs)
    assert agent.action._artifact_manager._artifacts == {}
    action_loop_executions = [
        execution
        for execution in captured_executions
        if execution.get_state("round_index", None) is not None
        and isinstance(execution.get_state("done_plans", None), list)
    ]
    assert len(action_loop_executions) == 1
    execution = action_loop_executions[0]
    internal_workspace = execution._get_runtime_resource("task_workspace", None)
    stored_content_bytes = 0
    stored_records: list[dict[str, Any]] = []
    if internal_workspace is not None:
        stored_records = await internal_workspace.search()
        for stored_ref in stored_records:
            stored_content_bytes += len(
                json.dumps(stored_ref, ensure_ascii=False, default=str).encode("utf-8")
            )
            if stored_ref.get("path"):
                stored_value = await internal_workspace.get_data(stored_ref)
                stored_content_bytes += len(
                    json.dumps(stored_value, ensure_ascii=False, default=str).encode("utf-8")
                )
    for state_key in ("done_plans", "last_round_records", "action_loop_result"):
        state_bytes = json.dumps(
            execution.get_state(state_key, []),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        assert len(state_bytes) <= 16000, {
            "state_key": state_key,
            "state_bytes": len(state_bytes),
            "task_workspace_record_kinds": [record.get("kind") for record in stored_records],
            "task_workspace_terminal_bytes": stored_content_bytes,
        }
        assert output_marker.encode("utf-8") not in state_bytes
    assert internal_workspace is None, {
        "record_count": len(stored_records),
        "stored_content_bytes": stored_content_bytes,
    }
    assert stored_content_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
async def test_action_flow_single_action_id_cohort_preserves_cross_round_value_dependency(
    flow_name: str,
) -> None:
    agent: Any = Agently.create_agent(f"action-round-cohort-{flow_name}")
    search_action_id = f"repository_search_{flow_name}"
    read_action_id = f"repository_read_{flow_name}"
    executed_calls: list[tuple[str, dict[str, Any]]] = []
    observations: list[dict[str, Any]] = []

    def search_repository(query: str) -> dict[str, Any]:
        executed_calls.append((search_action_id, {"query": query}))
        return {"matches": [f"src/{query}.py"]}

    def read_repository(path: str) -> dict[str, Any]:
        executed_calls.append((read_action_id, {"path": path}))
        return {"path": path, "content": "bounded source"}

    agent.action.register_action(
        action_id=search_action_id,
        desc="Search repository files before selecting a path to read.",
        kwargs={"query": (str, "Search query.")},
        func=search_repository,
    )
    agent.action.register_action(
        action_id=read_action_id,
        desc="Read a path selected from repository search results.",
        kwargs={"path": (str, "Selected repository path.")},
        func=read_repository,
    )
    agent.settings.set("action.loop.round_dispatch_policy", "single_action_id_cohort")
    flow = agent.action._flow_controller.create_named_action_flow(flow_name)

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        round_index = context.get("round_index")
        if round_index == 0:
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "action_id": search_action_id,
                        "action_input": {"query": "selected"},
                        "purpose": "find the selected source",
                    },
                    {
                        "action_id": read_action_id,
                        "action_input": {"path": "guessed/path.py"},
                        "purpose": "stale read that must be deferred and replanned",
                    },
                    {
                        "action_id": search_action_id,
                        "action_input": {"query": "secondary"},
                        "purpose": "parallel search in the same capability cohort",
                    },
                ],
            }
        if round_index == 1:
            last_round_records = context.get("last_round_records")
            assert isinstance(last_round_records, list)
            assert [record.get("action_id") for record in last_round_records] == [
                search_action_id,
                search_action_id,
            ]
            search_value = last_round_records[0].get("data") or last_round_records[0].get("result")
            assert isinstance(search_value, dict)
            result_preview = search_value.get("result_preview", search_value)
            assert isinstance(result_preview, dict), json.dumps(
                last_round_records[0], ensure_ascii=False, default=str
            )
            selected_path = result_preview["matches"][0]
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "action_id": read_action_id,
                        "action_input": {"path": selected_path},
                        "purpose": "read the path supplied by the previous round",
                    }
                ],
            }
        return {"next_action": "response", "action_calls": []}

    async def execution_handler(context: dict[str, Any], request: dict[str, Any]) -> list[Any]:
        records: list[Any] = []
        for command in request["action_calls"]:
            action_id = str(command["action_id"])
            action_input = dict(command.get("action_input") or {})
            records.append(
                await agent.action.async_execute_action(
                    action_id,
                    action_input,
                    purpose=str(command.get("purpose") or ""),
                    artifact_scope=context["artifact_scope"],
                )
            )
        return records

    records = await flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(),
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=4,
        runtime_observation_handler=lambda observation: observations.append(copy.deepcopy(observation)),
    )

    assert [action_id for action_id, _ in executed_calls] == [
        search_action_id,
        search_action_id,
        read_action_id,
    ]
    assert executed_calls[-1][1] == {"path": "src/selected.py"}
    assert all(action_input.get("path") != "guessed/path.py" for _, action_input in executed_calls)
    assert [record.get("action_id") for record in records] == [
        search_action_id,
        search_action_id,
        read_action_id,
    ]
    first_plan = next(
        observation
        for observation in observations
        if observation.get("kind") == "plan_ready"
        and observation.get("payload", {}).get("round_index") == 0
    )
    first_decision = first_plan["payload"]["decision"]
    assert [call["action_id"] for call in first_decision["action_calls"]] == [
        search_action_id,
        search_action_id,
    ]
    assert first_decision["diagnostics"][-1]["code"] == (
        "action_loop.round_dispatch.deferred_distinct_actions"
    )
    raw_decision = agent.action._apply_action_decision_round_dispatch_policy(
        agent.action._normalize_action_decision(
            {
                "next_action": "execute",
                "action_calls": [
                    {"action_id": search_action_id, "action_input": {"query": "one"}},
                    {"action_id": read_action_id, "action_input": {"path": "stale"}},
                    {"action_id": search_action_id, "action_input": {"query": "two"}},
                ],
            }
        ),
        agent.settings,
    )
    assert raw_decision["diagnostics"][-1]["meta"] == {
        "policy": "single_action_id_cohort",
        "selected_action_id": search_action_id,
        "selected_call_count": 2,
        "deferred_action_ids": [read_action_id],
        "deferred_call_count": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
async def test_standalone_action_flows_bound_large_instruction_before_state_and_storage(
    flow_name: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    agent: Any = Agently.create_agent(f"action-artifact-large-instruction-{flow_name}")
    action_id = f"large_instruction_{flow_name}"
    marker = f"{flow_name}_LARGE_INSTRUCTION_MUST_STAY_COLD"
    large_code = ("i" * (1024 * 1024)) + marker
    agent.action.register_action(
        action_id=action_id,
        desc="Consume a large instruction and return a compact fact.",
        kwargs={"code": (str, "Instruction body.")},
        func=lambda code: {"received_bytes": len(code.encode("utf-8"))},
    )
    flow = agent.action._flow_controller.create_named_action_flow(flow_name)
    from agently import TriggerFlow

    captured_executions: list[Any] = []
    original_create_execution = TriggerFlow.create_execution

    def capture_execution(trigger_flow: Any, **kwargs: Any) -> Any:
        execution = original_create_execution(trigger_flow, **kwargs)
        captured_executions.append(execution)
        return execution

    monkeypatch.setattr(TriggerFlow, "create_execution", capture_execution)

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "action_id": action_id,
                    "action_input": {"code": large_code},
                    "purpose": "consume instruction",
                }
            ],
        }

    async def execution_handler(context: dict[str, Any], request: dict[str, Any]) -> list[Any]:
        return [
            await agent.action.async_execute_action(
                str(command["action_id"]),
                dict(command.get("action_input") or {}),
                purpose=str(command.get("purpose") or ""),
                artifact_scope=context["artifact_scope"],
            )
            for command in request["action_calls"]
        ]

    records = await flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(),
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=2,
    )

    action_loop_executions = [
        execution
        for execution in captured_executions
        if execution.get_state("round_index", None) is not None
        and isinstance(execution.get_state("done_plans", None), list)
    ]
    assert len(action_loop_executions) == 1
    execution = action_loop_executions[0]
    carriers: dict[str, Any] = {
        "return": records,
        "done_plans": execution.get_state("done_plans", []),
        "last_round_records": execution.get_state("last_round_records", []),
        "action_loop_result": execution.get_state("action_loop_result", []),
    }
    for name, carrier in carriers.items():
        carrier_bytes = json.dumps(carrier, ensure_ascii=False, default=str).encode("utf-8")
        assert len(carrier_bytes) <= 16000, (name, len(carrier_bytes))
        assert marker.encode("utf-8") not in carrier_bytes
    internal_workspace = execution._get_runtime_resource("task_workspace", None)
    stored_content_bytes = 0
    if internal_workspace is not None:
        for stored_ref in await internal_workspace.search():
            stored_content_bytes += len(
                json.dumps(stored_ref, ensure_ascii=False, default=str).encode("utf-8")
            )
            if stored_ref.get("path"):
                stored_value = await internal_workspace.get_data(stored_ref)
                stored_content_bytes += len(
                    json.dumps(stored_value, ensure_ascii=False, default=str).encode("utf-8")
                )
    assert internal_workspace is None, {"stored_content_bytes": stored_content_bytes}
    assert stored_content_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_mode", ["failure", "cancellation"])
async def test_standalone_triggerflow_action_scope_releases_on_failure_and_cancellation(
    terminal_mode: str,
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    agent: Any = Agently.create_agent(f"action-artifact-standalone-{terminal_mode}")
    action_id = f"standalone_{terminal_mode}"
    agent.action.register_action(
        action_id=action_id,
        desc="Create a large standalone value before terminal interruption.",
        kwargs={},
        func=lambda: {"body": terminal_mode * (1024 * 1024)},
    )
    flow = agent.action._flow_controller.create_named_action_flow("TriggerFlowActionFlow")
    artifact_registered = asyncio.Event()
    wait_forever = asyncio.Event()

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [{"action_id": action_id, "action_input": {}, "purpose": "interrupt"}],
        }

    async def execution_handler(_context: dict[str, Any], request: dict[str, Any]) -> list[Any]:
        await request["async_call_action"](action_id, {})
        assert agent.action._artifact_manager._artifacts
        artifact_registered.set()
        if terminal_mode == "failure":
            raise RuntimeError("standalone flow failure")
        await wait_forever.wait()
        return []

    run = asyncio.create_task(
        flow.async_run(
            action=agent.action,
            prompt=agent.request.prompt,
            settings=agent.settings,
            action_list=agent.action.get_action_list(),
            planning_handler=planning_handler,
            execution_handler=execution_handler,
            max_rounds=2,
        )
    )
    await artifact_registered.wait()
    if terminal_mode == "cancellation":
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    else:
        with pytest.raises(RuntimeError, match="standalone flow failure"):
            await run
    assert agent.action._artifact_manager._artifacts == {}


@pytest.mark.asyncio
async def test_large_action_output_stays_in_memory_and_runtime_event_is_bounded() -> None:
    large_body = "x" * (1024 * 1024)
    agent: Any = Agently.create_agent("action-artifact-retention-large-output")
    record = agent.action._finalize_action_result(
        {
            "action_call_id": "large-action-call",
            "action_id": "large_action_output",
            "status": "success",
            "success": True,
            "ok": True,
            "result": {"body": large_body},
            "data": {"body": large_body},
        }
    )
    artifact_refs = record["artifact_refs"]
    assert len(artifact_refs) == 1
    selection_key = artifact_refs[0]["selection_key"]
    assert artifact_refs[0]["artifact_id"]
    assert artifact_refs[0]["action_call_id"] == "large-action-call"
    assert len(agent.action._artifact_manager._artifacts) == 1
    transfer = agent.action._artifact_manager.read_selection_transfer(
        selection_key,
        expected_scope={"kind": "action_call", "id": "large-action-call"},
    )
    assert transfer is not None
    canonical_ref, exact_value = transfer
    assert canonical_ref["artifact_id"] == artifact_refs[0]["artifact_id"]
    assert exact_value == {"body": large_body}
    assert record["result"]["body"] == large_body
    assert record["data"]["body"] == large_body

    visible_record = agent.action._to_model_visible_record(record)
    assert large_body not in json.dumps(visible_record, ensure_ascii=False)
    assert visible_record["artifact_refs"][0]["selection_key"] == selection_key
    assert set(visible_record["artifact_refs"][0]).isdisjoint(
        {"artifact_id", "action_call_id", "sha256", "size", "bytes", "meta"}
    )
    assert visible_record["result"]["result_preview_meta"]["truncated"] is True

    captured: list[Any] = []
    hook_name = "test_action_artifact_retention.large_action_output"
    Agently.event_center.register_hook(
        lambda event: captured.append(event),
        event_types="action.completed",
        hook_name=hook_name,
    )
    try:
        await agent.action._flow_controller.async_emit_action_flow_observation(
            {
                "kind": "action_completed",
                "source": "ActionFlow",
                "payload": {"record": record},
            }
        )
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert len(captured) == 1
    event_payload = captured[0].payload
    event_record = event_payload["record"]
    assert large_body not in json.dumps(event_payload, ensure_ascii=False)
    assert event_record["artifact_refs"] == visible_record["artifact_refs"]
    assert event_record["result"]["result_preview_meta"]["truncated"] is True


def test_external_artifact_ids_are_scope_local_and_preserved_only_as_provenance() -> None:
    agent: Any = Agently.create_agent("action-artifact-external-id-scope")
    manager = agent.action._artifact_manager
    scope_a = {"kind": "agent_execution", "id": "execution-a"}
    scope_b = {"kind": "agent_execution", "id": "execution-b"}
    provider_ref_a = {"artifact_id": "provider-shared-id", "path": "a/report.json", "bytes": 11}
    provider_ref_b = {"artifact_id": "provider-shared-id", "path": "b/report.json", "bytes": 13}

    ref_a = manager.register_external_artifact_ref(
        action_call_id="call-a",
        artifact_type="external_ref",
        label="A",
        ref=provider_ref_a,
        artifact_scope=scope_a,
    )
    ref_b = manager.register_external_artifact_ref(
        action_call_id="call-b",
        artifact_type="external_ref",
        label="B",
        ref=provider_ref_b,
        artifact_scope=scope_b,
    )

    assert ref_a["artifact_id"] != ref_b["artifact_id"]
    assert ref_a["artifact_id"] != "provider-shared-id"
    assert ref_b["artifact_id"] != "provider-shared-id"
    assert ref_a["meta"]["external_artifact_id"] == "provider-shared-id"
    assert ref_b["meta"]["external_artifact_id"] == "provider-shared-id"
    assert manager.get_artifact_value(ref_a["artifact_id"]) == provider_ref_a
    assert manager.get_artifact_value(ref_b["artifact_id"]) == provider_ref_b
    assert manager.release_scope(scope_a) == 1
    assert manager.get_artifact_value(ref_a["artifact_id"]) is None
    assert manager.get_artifact_value(ref_b["artifact_id"]) == provider_ref_b


def test_small_explicit_artifact_model_projection_hides_canonical_identity() -> None:
    agent: Any = Agently.create_agent("action-artifact-small-model-projection")
    scope = {"kind": "agent_execution", "id": "small-projection"}
    canonical_ref = agent.action._artifact_manager.register_external_artifact_ref(
        action_call_id="small-call",
        artifact_type="external_ref",
        label="Small external artifact",
        ref={"artifact_id": "provider-id", "path": "small/report.json", "bytes": 3},
        artifact_scope=scope,
    )
    record = {
        "action_call_id": "small-call",
        "action_id": "small_action",
        "status": "success",
        "success": True,
        "result": {"ok": True},
        "artifact_refs": [],
        "artifacts": [canonical_ref],
    }

    visible = agent.action._to_model_visible_record(record)

    assert visible["artifact_refs"][0]["selection_key"] == canonical_ref["selection_key"]
    assert set(visible["artifact_refs"][0]).isdisjoint(
        {"artifact_id", "action_call_id", "sha256", "size", "bytes", "meta"}
    )
    assert visible["artifacts"] == visible["artifact_refs"]


def test_public_action_artifact_readback_selector_is_selection_key_only() -> None:
    signature = inspect.signature(Agently.create_agent("action-readback-signature").action.async_read_action_artifact)

    assert list(signature.parameters) == ["selection_key", "offset", "max_bytes"]


@pytest.mark.asyncio
async def test_action_artifact_readback_returns_stable_bounded_ranges() -> None:
    agent: Any = Agently.create_agent("action-readback-bounded-ranges")
    scope = {"kind": "agent_task", "id": "bounded-range-task"}
    artifact = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="bounded-range-call",
        artifact_type="action_output",
        label="Large search result",
        value="0123456789" * 100,
        artifact_scope=scope,
    )

    with agent.action._artifact_manager.bind_artifact_scope(scope):
        first = await agent.action.async_read_action_artifact(
            selection_key=artifact["selection_key"],
            offset=0,
            max_bytes=64,
        )
        second = await agent.action.async_read_action_artifact(
            selection_key=artifact["selection_key"],
            offset=first["range"]["end"],
            max_bytes=64,
        )

    assert first["ok"] is True
    assert first["owner"] == "action_artifact"
    assert first["locator"] == artifact["selection_key"]
    assert first["content_version"] == artifact["sha256"]
    assert first["range"] == {"offset": 0, "end": 64, "read_bytes": 64}
    assert first["truncated"] is True
    assert second["range"] == {"offset": 64, "end": 128, "read_bytes": 64}
    assert second["value"] != first["value"] or second["range"] != first["range"]


@pytest.mark.asyncio
async def test_action_artifact_readback_fails_closed_without_scope_or_across_execution_scope() -> None:
    from agently.core.runtime import bind_runtime_context

    agent: Any = Agently.create_agent("action-readback-scope-bound")
    scope_a = {"kind": "agent_execution", "id": "execution-a"}
    artifact = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="scope-a-call",
        artifact_type="action_output",
        label="Execution A private output",
        value={"private": "execution-a-only"},
        artifact_scope=scope_a,
    )

    missing_scope = await agent.action.async_read_action_artifact(
        selection_key=artifact["selection_key"],
    )
    with bind_runtime_context(
        agent_execution_context=SimpleNamespace(execution_id="execution-b"),
    ):
        cross_scope = await agent.action.async_read_action_artifact(
            selection_key=artifact["selection_key"],
        )
    with bind_runtime_context(
        agent_execution_context=SimpleNamespace(execution_id="execution-a"),
    ):
        own_scope = await agent.action.async_read_action_artifact(
            selection_key=artifact["selection_key"],
        )

    assert missing_scope["ok"] is False
    assert missing_scope["status"] in {"forbidden", "not_found"}
    assert "artifact_id" not in missing_scope
    assert "value" not in missing_scope
    assert cross_scope["ok"] is False
    assert cross_scope["status"] in {"forbidden", "not_found"}
    assert "artifact_id" not in cross_scope
    assert "value" not in cross_scope
    assert own_scope["ok"] is True
    assert own_scope["value"] == {"private": "execution-a-only"}


@pytest.mark.asyncio
async def test_explicit_task_run_context_owns_bounded_action_artifacts_over_ambient_execution() -> None:
    from agently.core.runtime import bind_runtime_context
    from agently.types.data import RunContext

    agent: Any = Agently.create_agent("action-artifact-explicit-task-owner")

    @agent.action_func
    def produce_task_evidence() -> dict[str, Any]:
        return {"records": [f"record-{index}" for index in range(2000)]}

    task_scope = {"kind": "agent_task", "id": "taskboard-task-owner"}
    parent_run_context = RunContext.create(
        run_kind="agent_execution",
        execution_id="taskboard-bounded-action",
        meta={"task_id": task_scope["id"]},
    )
    ambient_context = SimpleNamespace(
        execution_id="outer-agent-execution",
        lineage={},
    )

    with bind_runtime_context(agent_execution_context=ambient_context):
        records = await agent.action._async_execute_action_calls(
            action_calls=[
                {
                    "action_id": "produce_task_evidence",
                    "action_input": {},
                    "purpose": "Produce exact evidence for a later TaskBoard card.",
                }
            ],
            settings=agent.settings,
            agent_name=agent.name,
            parent_run_context=parent_run_context,
        )

    output_ref = next(
        ref
        for ref in records[0]["artifact_refs"]
        if ref.get("role") == "output"
    )
    artifact_id = agent.action._artifact_manager.get_artifact_id_for_selection(
        output_ref["selection_key"]
    )
    assert artifact_id is not None
    assert agent.action._artifact_manager.get_artifact_scope(artifact_id) == task_scope
    with agent.action._artifact_manager.bind_artifact_scope(task_scope):
        readback = await agent.action.async_read_action_artifact(
            selection_key=output_ref["selection_key"],
            max_bytes=128,
        )
    assert readback["ok"] is True


@pytest.mark.asyncio
async def test_explicit_task_run_context_owns_action_loop_artifacts_over_ambient_execution() -> None:
    from agently.core.runtime import bind_runtime_context
    from agently.types.data import RunContext

    agent: Any = Agently.create_agent("action-loop-artifact-explicit-task-owner")

    @agent.action_func
    def search_task_evidence(query: str) -> dict[str, Any]:
        return {
            "query": query,
            "matches": [f"src/module_{index}.py" for index in range(2000)],
        }

    async def planning_handler(
        context: dict[str, Any],
        _request: dict[str, Any],
    ) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "action_id": "search_task_evidence",
                    "action_input": {"query": "task context"},
                    "purpose": "Search exact evidence for a later TaskBoard card.",
                }
            ],
        }

    task_scope = {"kind": "agent_task", "id": "taskboard-action-loop-owner"}
    parent_run_context = RunContext.create(
        run_kind="agent_execution",
        execution_id="taskboard-action-loop",
        meta={"task_id": task_scope["id"]},
    )
    ambient_context = SimpleNamespace(
        execution_id="outer-agent-execution",
        lineage={},
    )

    with bind_runtime_context(agent_execution_context=ambient_context):
        records = await agent.action.async_plan_and_execute(
            prompt=agent.request.prompt,
            settings=agent.settings,
            action_list=agent.action.get_action_list(),
            agent_name=agent.name,
            parent_run_context=parent_run_context,
            planning_handler=planning_handler,
            max_rounds=2,
        )

    output_ref = next(
        ref
        for ref in records[0]["artifact_refs"]
        if ref.get("role") == "output"
    )
    artifact_id = agent.action._artifact_manager.get_artifact_id_for_selection(
        output_ref["selection_key"]
    )
    assert artifact_id is not None
    assert agent.action._artifact_manager.get_artifact_scope(artifact_id) == task_scope
    with agent.action._artifact_manager.bind_artifact_scope(task_scope):
        readback = await agent.action.async_read_action_artifact(
            selection_key=output_ref["selection_key"],
            max_bytes=128,
        )
    assert readback["ok"] is True


@pytest.mark.asyncio
async def test_artifacts_only_terminal_carrier_discards_transient_selection_without_alias_leak(tmp_path) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.terminal_retention import (
        prepare_agent_execution_terminal_retention,
    )

    agent: Any = Agently.create_agent("artifacts-only-terminal-carrier").use_task_workspace(
        tmp_path / "task_workspace"
    )
    execution = agent.input("Return the selected artifact.").create_execution().strategy("direct")
    execution.status = "success"
    execution.route_info["selected_route"] = "model_request"
    scope = {"kind": "agent_execution", "id": execution.id}
    canonical_ref = agent.action._artifact_manager.register_execution_artifact(
        action_call_id="artifacts-only-call",
        artifact_type="action_output",
        label="Artifacts-only output",
        value={"body": "canonical terminal value"},
        artifact_scope=scope,
    )
    execution.logs["artifact_refs"] = [canonical_ref]
    execution.result = {
        "status": "success",
        "artifacts": [canonical_ref],
    }

    terminal_result, retained_refs = await prepare_agent_execution_terminal_retention(execution)

    assert retained_refs == []
    assert terminal_result["artifacts"] == []
    assert "artifact_refs" not in terminal_result
    terminal_json = json.dumps(terminal_result, ensure_ascii=False, default=str)
    assert canonical_ref["artifact_id"] not in terminal_json
    assert canonical_ref["action_call_id"] not in terminal_json
    assert "artifact_scope" not in terminal_json
    assert not (tmp_path / "task_workspace" / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_selected_action_artifact_run_scope_release_is_concurrent_and_small_carrier_is_durable(
    tmp_path,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.route_execution import (
        _finalize_terminal_execution,
    )

    agent: Any = Agently.create_agent("action-artifact-retention-run-scope").use_task_workspace(tmp_path / "run")
    action_id = "produce_scoped_large_action_output"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a scope-owned large Action artifact.",
        kwargs={"marker": (str, "Marker used to identify the execution-owned value.")},
        func=lambda marker: {"marker": marker, "body": marker * (1024 * 1024)},
        tags=[f"agent-{agent.name}"],
    )
    first_execution = agent.input("Run the first scoped Action set.").create_execution().strategy("direct")
    second_execution = agent.input("Run the concurrent scoped Action set.").create_execution().strategy("direct")
    async def run_scoped_actions(execution: Any, markers: list[str]) -> list[dict[str, Any]]:
        return await asyncio.gather(
            *[
                agent.action.async_execute_action(
                    action_id,
                    {"marker": marker},
                    artifact_scope={"kind": "agent_execution", "id": execution.id},
                )
                for marker in markers
            ]
        )

    first_records, second_records = await asyncio.gather(
        run_scoped_actions(first_execution, ["s", "u"]),
        run_scoped_actions(second_execution, ["c"]),
    )
    first_refs = [
        next(ref for ref in record["artifact_refs"] if ref.get("artifact_type") == "action_output")
        for record in first_records
    ]
    second_ref = next(
        ref
        for ref in second_records[0]["artifact_refs"]
        if ref.get("artifact_type") == "action_output"
    )
    expected_first_scope = {"kind": "agent_execution", "id": first_execution.id}
    expected_second_scope = {"kind": "agent_execution", "id": second_execution.id}
    first_canonical_refs = [
        _canonical_action_ref(agent.action._artifact_manager, ref, expected_first_scope)
        for ref in first_refs
    ]
    second_canonical_ref = _canonical_action_ref(
        agent.action._artifact_manager, second_ref, expected_second_scope
    )
    assert [ref["meta"]["artifact_scope"] for ref in first_canonical_refs] == [
        expected_first_scope,
        expected_first_scope,
    ]
    assert second_canonical_ref["meta"]["artifact_scope"] == expected_second_scope

    selected_ref, unselected_ref = first_refs
    first_execution.logs["artifact_refs"] = first_refs
    first_execution.result = {
        "accepted": True,
        "artifact_refs": [{"selection_key": selected_ref["selection_key"]}],
        "reply": "small accepted wrapper",
    }
    first_execution.status = "success"

    await _finalize_terminal_execution(first_execution, terminal_status="completed")

    assert agent.action._artifact_manager.get_artifact_value(first_canonical_refs[0]["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(first_canonical_refs[1]["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(second_canonical_ref["artifact_id"]) is not None
    assert first_execution.diagnostics["action_artifact_release"]["released_count"] == 4
    terminal_result = first_execution.close_snapshot["terminal_result"]
    assert first_canonical_refs[0]["artifact_id"] not in json.dumps(terminal_result, ensure_ascii=False)
    assert terminal_result["artifact_refs"] == []
    assert terminal_result["reply"] == "small accepted wrapper"
    assert not (tmp_path / "run" / ".agently" / "workspace.db").exists()


@pytest.mark.asyncio
async def test_custom_action_execution_handler_callback_binds_agent_execution_artifact_scope(
    tmp_path,
    monkeypatch,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.route_execution import (
        _finalize_terminal_execution,
    )

    agent: Any = Agently.create_agent("action-artifact-custom-handler-scope").use_task_workspace(tmp_path / "run")
    action_id = "produce_custom_handler_large_output"
    action_tag = f"agent-{agent.name}"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a large Action artifact through a custom execution handler callback.",
        kwargs={"marker": (str, "Execution scope marker.")},
        func=lambda marker: {"marker": marker, "body": marker * (1024 * 1024)},
        tags=[action_tag],
    )
    owner = agent.input("Run the custom Action execution handler.").create_execution().strategy("direct")
    concurrent = agent.input("Keep a concurrent Action scope alive.").create_execution().strategy("direct")
    callback_results: list[dict[str, Any]] = []
    registered_refs: list[dict[str, Any]] = []
    original_register_execution_artifact = agent.action._artifact_manager.register_execution_artifact

    def capture_registered_artifact(*args: Any, **kwargs: Any) -> dict[str, Any]:
        artifact_ref = original_register_execution_artifact(*args, **kwargs)
        registered_refs.append(copy.deepcopy(artifact_ref))
        return artifact_ref

    monkeypatch.setattr(
        agent.action._artifact_manager,
        "register_execution_artifact",
        capture_registered_artifact,
    )

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "purpose": "produce scoped output",
                    "action_id": action_id,
                    "action_input": {"marker": "o"},
                    "todo_suggestion": "finish",
                }
            ],
        }

    async def execution_handler(
        _context: dict[str, Any],
        request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        callback_result = await request["async_call_action"](action_id, {"marker": "o"})
        assert isinstance(callback_result, dict)
        callback_results.append(callback_result)
        return [
            {
                "action_call_id": "custom-handler-result",
                "action_id": action_id,
                "purpose": "produce scoped output",
                "status": "success",
                "success": True,
                "ok": True,
                "result": callback_result,
                "data": callback_result,
                "artifact_refs": callback_result.get("artifact_refs", []),
            }
        ]

    records = await agent.action.async_plan_and_execute(
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(tags=[action_tag]),
        agent_name=agent.name,
        parent_run_context=owner._ensure_agent_execution_run_context(),
        planning_handler=planning_handler,
        action_execution_handler=execution_handler,
        max_rounds=2,
    )
    assert callback_results
    callback_ref = next(
        ref
        for ref in registered_refs
        if ref.get("artifact_type") == "action_output"
    )
    stored = agent.action._artifact_manager.get_artifact(callback_ref["artifact_id"])
    assert stored is not None
    assert stored["meta"]["artifact_scope"] == {"kind": "agent_execution", "id": owner.id}

    concurrent_record = await agent.action.async_execute_action(
        action_id,
        {"marker": "c"},
        artifact_scope={"kind": "agent_execution", "id": concurrent.id},
    )
    concurrent_ref = next(
        ref
        for ref in concurrent_record["artifact_refs"]
        if ref.get("artifact_type") == "action_output"
    )
    owner.logs["artifact_refs"] = [callback_ref]
    owner.result = {
        "accepted": True,
        "artifact_refs": [{"selection_key": callback_ref["selection_key"]}],
        "records": records,
        "reply": "bounded",
    }
    owner.status = "success"

    await _finalize_terminal_execution(owner, terminal_status="completed")

    assert agent.action._artifact_manager.get_artifact_value(callback_ref["artifact_id"]) is None
    concurrent_canonical_ref = _canonical_action_ref(
        agent.action._artifact_manager,
        concurrent_ref,
        {"kind": "agent_execution", "id": concurrent.id},
    )
    assert agent.action._artifact_manager.get_artifact_value(concurrent_canonical_ref["artifact_id"]) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_mode", ["success", "failure", "cancellation", "timeout"])
async def test_standalone_agent_task_releases_exact_action_artifact_scope_at_terminal(
    tmp_path,
    monkeypatch,
    terminal_mode: str,
) -> None:
    agent: Any = Agently.create_agent(f"standalone-task-artifact-{terminal_mode}").use_task_workspace(
        tmp_path / terminal_mode
    )
    action_id = f"standalone_task_large_output_{terminal_mode}"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a real large Action value owned by one standalone AgentTask.",
        kwargs={},
        func=lambda: {"body": terminal_mode * (1024 * 1024)},
    )
    task = AgentTask(
        agent,
        task_id=f"standalone-task-{terminal_mode}",
        goal="Exercise standalone AgentTask terminal Action cleanup.",
        success_criteria=["The exact task scope is released."],
        execution="flat",
    )
    task_scope = {"kind": "agent_task", "id": task.id}
    action_record = await agent.action.async_execute_action(
        action_id,
        {},
        artifact_scope=task_scope,
    )
    output_ref = next(
        ref for ref in action_record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    canonical_ref = _canonical_action_ref(agent.action._artifact_manager, output_ref, task_scope)
    assert agent.action._artifact_manager.get_artifact_value(canonical_ref["artifact_id"]) is not None

    class _TerminalExecution:
        async def async_start(self, _value: Any) -> None:
            if terminal_mode == "success":
                task.status = "completed"
                task.result = {
                    "status": "completed",
                    "accepted": True,
                    "artifact_status": "accepted",
                    "final_result": "done",
                    "artifact_refs": [],
                }
                return
            if terminal_mode == "failure":
                raise RuntimeError("standalone AgentTask failed")
            if terminal_mode == "cancellation":
                raise asyncio.CancelledError()
            raise TimeoutError("standalone AgentTask timed out")

        async def async_close(self, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(task._flow, "create_execution", lambda **_kwargs: _TerminalExecution())

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(task, "_record_phase", noop)
    monkeypatch.setattr(task, "_ensure_final_reflection", noop)
    monkeypatch.setattr(task, "_emit", noop)

    if terminal_mode == "success":
        result = await task.async_run()
        assert result["status"] == "completed"
    elif terminal_mode == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await task.async_run()
        assert task.status == "cancelled"
    elif terminal_mode == "timeout":
        with pytest.raises(TimeoutError, match="timed out"):
            await task.async_run()
        assert task.status == "timed_out"
    else:
        with pytest.raises(RuntimeError, match="failed"):
            await task.async_run()
        assert task.status == "error"

    assert agent.action._artifact_manager.get_artifact_value(canonical_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.release_scope(task_scope) == 0


@pytest.mark.asyncio
async def test_custom_triggerflow_handler_bounds_every_agent_execution_consumer(
    tmp_path,
    monkeypatch,
) -> None:
    from agently import TriggerFlow
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.diagnostics import (
        build_execution_meta,
    )
    from agently.core.runtime import bind_runtime_context

    marker = "CUSTOM_HANDLER_RAW_TAIL_MUST_STAY_PRIVATE"
    agent: Any = Agently.create_agent("custom-handler-all-consumers").use_task_workspace(tmp_path / "task_workspace")
    action_id = "custom_handler_large_output"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce one large value through a custom TriggerFlow execution handler.",
        kwargs={},
        func=lambda: {"body": ("z" * (1024 * 1024)) + marker},
    )
    owner = agent.input("Run one custom Action handler.").create_execution().strategy("direct")
    captured_executions: list[Any] = []
    original_create_execution = TriggerFlow.create_execution

    def capture_execution(trigger_flow: Any, **kwargs: Any) -> Any:
        execution = original_create_execution(trigger_flow, **kwargs)
        captured_executions.append(execution)
        return execution

    monkeypatch.setattr(TriggerFlow, "create_execution", capture_execution)
    callback_records: list[dict[str, Any]] = []
    captured_events: list[Any] = []
    hook_name = "test.custom_handler_all_consumers"
    Agently.event_center.register_hook(
        lambda event: captured_events.append(event),
        event_types="action.completed",
        hook_name=hook_name,
    )

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [{"action_id": action_id, "action_input": {}, "purpose": "produce large output"}],
        }

    async def execution_handler(
        _context: dict[str, Any],
        request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        action_result = await request["async_call_action"](action_id, {})
        callback_records.append(action_result)
        return [
            {
                "action_call_id": "custom-handler-composite",
                "action_id": action_id,
                "purpose": "produce large output",
                "status": "success",
                "success": True,
                "ok": True,
                "result": action_result,
                "data": action_result,
                "model_digest": action_result,
                "raw": action_result,
                "artifact_refs": action_result.get("artifact_refs", []),
            }
        ]

    try:
        with bind_runtime_context(agent_execution_context=owner.execution_context):
            records = await agent.action.async_plan_and_execute(
                prompt=agent.request.prompt,
                settings=agent.settings,
                action_list=agent.action.get_action_list(),
                agent_name=agent.name,
                parent_run_context=owner._ensure_agent_execution_run_context(),
                planning_handler=planning_handler,
                action_execution_handler=execution_handler,
                max_rounds=2,
            )
        owner._refresh_diagnostics()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    scope = {"kind": "agent_execution", "id": owner.id}
    exact_values = [
        agent.action._artifact_manager.get_artifact_value(artifact_id)
        for artifact_id, artifact_scope in agent.action._artifact_manager._artifact_scopes.items()
        if artifact_scope == (scope["kind"], scope["id"])
    ]
    assert any(isinstance(value, dict) and marker in json.dumps(value, ensure_ascii=False) for value in exact_values)

    action_loop_execution = next(
        execution
        for execution in captured_executions
        if execution.get_state("round_index", None) is not None
    )
    meta = build_execution_meta(owner)
    carriers: dict[str, tuple[Any, int]] = {
        "context_records": (owner.execution_context.action_records, 16000),
        "owner_logs": (owner.logs, 16000),
        "owner_diagnostics": (owner.diagnostics, 16000),
        "owner_meta": (meta, 32000),
        "runtime_events": ([event.payload for event in captured_events], 16000),
        "public_return": (records, 16000),
        "done_plans": (action_loop_execution.get_state("done_plans", []), 16000),
        "last_round_records": (action_loop_execution.get_state("last_round_records", []), 16000),
        "action_loop_result": (action_loop_execution.get_state("action_loop_result", []), 16000),
    }
    measurements: dict[str, dict[str, Any]] = {}
    violations: list[str] = []
    for carrier_name, (carrier, max_bytes) in carriers.items():
        carrier_bytes = json.dumps(carrier, ensure_ascii=False, default=str).encode("utf-8")
        marker_present = marker.encode("utf-8") in carrier_bytes
        measurements[carrier_name] = {
            "bytes": len(carrier_bytes),
            "max_bytes": max_bytes,
            "marker_present": marker_present,
        }
        if len(carrier_bytes) > max_bytes or marker_present:
            violations.append(carrier_name)
    assert violations == [], measurements

    action_logs = owner.logs["action_logs"]
    assert len(action_logs) == 1
    semantic_log = action_logs[0]
    assert "raw" not in semantic_log, json.dumps(measurements, sort_keys=True)
    assert sum(
        key in semantic_log and semantic_log[key] not in (None, {}, [])
        for key in ("data", "result", "model_digest")
    ) <= 1

    internal_workspace = action_loop_execution._get_runtime_resource("task_workspace", None)
    stored_content_bytes = 0
    if internal_workspace is not None:
        for stored_ref in await internal_workspace.search():
            stored_content_bytes += len(
                json.dumps(stored_ref, ensure_ascii=False, default=str).encode("utf-8")
            )
            if stored_ref.get("path"):
                stored_value = await internal_workspace.get_data(stored_ref)
                stored_content_bytes += len(
                    json.dumps(stored_value, ensure_ascii=False, default=str).encode("utf-8")
                )
    assert internal_workspace is None
    assert stored_content_bytes == 0
    agent.action._release_artifact_scope(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
async def test_action_flow_observations_bound_and_redact_every_delivery_path(
    tmp_path,
    flow_name: str,
) -> None:
    input_marker = f"{flow_name}_OBSERVATION_INPUT_TAIL"
    output_marker = f"{flow_name}_OBSERVATION_OUTPUT_TAIL"
    secret_value = f"{flow_name}-secret-value"
    agent: Any = Agently.create_agent(f"observation-bounds-{flow_name}").use_task_workspace(
        tmp_path / flow_name
    )
    action_id = f"observation_bounds_{flow_name}"
    agent.action.register_action(
        action_id=action_id,
        desc="Exercise every Action observation delivery path.",
        kwargs={
            "code": (str, "Large instruction body."),
            "api_key": (str, "Sensitive credential."),
        },
        func=lambda code, api_key: {
            "body": ("o" * (1024 * 1024)) + output_marker,
            "api_key": api_key,
            "received_bytes": len(code.encode("utf-8")),
        },
    )
    flow = agent.action._flow_controller.create_named_action_flow(flow_name)
    large_input = ("i" * (1024 * 1024)) + input_marker
    direct_observations: list[dict[str, Any]] = []
    runtime_events: list[Any] = []
    hook_name = f"test.observation_bounds.{flow_name}"
    Agently.event_center.register_hook(
        lambda event: runtime_events.append(event),
        event_types=[
            "action.plan_ready",
            "tool.plan_ready",
            "action.started",
            "action.completed",
        ],
        hook_name=hook_name,
    )

    async def observation_handler(observation: dict[str, Any]) -> None:
        direct_observations.append(copy.deepcopy(observation))
        await agent.action._flow_controller.async_emit_action_flow_observation(observation)

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if context.get("done_plans"):
            return {"next_action": "response", "action_calls": []}
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "purpose": "exercise observation projection",
                    "action_id": action_id,
                    "action_input": {"code": large_input, "api_key": secret_value},
                }
            ],
        }

    async def execution_handler(
        context: dict[str, Any],
        request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [
            await agent.action.async_execute_action(
                str(command["action_id"]),
                dict(command.get("action_input") or {}),
                purpose=str(command.get("purpose") or ""),
                artifact_scope=context["artifact_scope"],
            )
            for command in request["action_calls"]
        ]

    try:
        records = await flow.async_run(
            action=agent.action,
            prompt=agent.request.prompt,
            settings=agent.settings,
            action_list=agent.action.get_action_list(),
            planning_handler=planning_handler,
            execution_handler=execution_handler,
            max_rounds=2,
            runtime_observation_handler=observation_handler,
        )
    finally:
        Agently.event_center.unregister_hook(hook_name)

    assert records
    expected_event_types = {
        "action.plan_ready",
        "tool.plan_ready",
        "action.started",
        "action.completed",
    }
    assert expected_event_types.issubset({event.event_type for event in runtime_events})
    measurements: dict[str, dict[str, Any]] = {}
    violations: list[str] = []
    for index, observation in enumerate(direct_observations):
        serialized = json.dumps(observation, ensure_ascii=False, default=str).encode("utf-8")
        key = f"direct:{index}:{observation.get('kind')}"
        measurements[key] = {
            "bytes": len(serialized),
            "input_marker": input_marker.encode("utf-8") in serialized,
            "output_marker": output_marker.encode("utf-8") in serialized,
            "secret": secret_value.encode("utf-8") in serialized,
        }
        if len(serialized) > 16000 or any(measurements[key][name] for name in ("input_marker", "output_marker", "secret")):
            violations.append(key)
    for index, event in enumerate(runtime_events):
        serialized = json.dumps(event.payload, ensure_ascii=False, default=str).encode("utf-8")
        key = f"event:{index}:{event.event_type}"
        measurements[key] = {
            "bytes": len(serialized),
            "input_marker": input_marker.encode("utf-8") in serialized,
            "output_marker": output_marker.encode("utf-8") in serialized,
            "secret": secret_value.encode("utf-8") in serialized,
        }
        if len(serialized) > 16000 or any(measurements[key][name] for name in ("input_marker", "output_marker", "secret")):
            violations.append(key)
    assert violations == [], json.dumps(measurements, sort_keys=True)

    plan_event = next(event for event in runtime_events if event.event_type == "action.plan_ready")
    decision = plan_event.payload["decision"]
    assert {
        key
        for key in ("execution_actions", "action_calls", "execution_commands", "tool_commands")
        if key in decision
    } == {"action_calls"}


@pytest.mark.asyncio
async def test_triggerflow_failure_convergence_callback_receives_bounded_records(tmp_path) -> None:
    marker = "FAILURE_CONVERGENCE_RAW_RECORD_TAIL"
    agent: Any = Agently.create_agent("failure-convergence-bounded").use_task_workspace(tmp_path / "task_workspace")
    action_id = "failure_convergence_action"
    flow = agent.action._flow_controller.create_named_action_flow("TriggerFlowActionFlow")
    observations: list[dict[str, Any]] = []

    async def planning_handler(_context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        return {
            "next_action": "execute",
            "action_calls": [{"action_id": action_id, "action_input": {}, "purpose": "fail repeatedly"}],
        }

    async def execution_handler(
        _context: dict[str, Any],
        _request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        payload = {"body": ("f" * (1024 * 1024)) + marker}
        return [
            {
                "action_call_id": "failure-convergence-call",
                "action_id": action_id,
                "purpose": "fail repeatedly",
                "status": "failed",
                "success": False,
                "ok": False,
                "result": payload,
                "data": payload,
                "error": "expected failure",
            }
        ]

    async def observation_handler(observation: dict[str, Any]) -> None:
        observations.append(copy.deepcopy(observation))

    records = await flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=[{"action_id": action_id, "desc": "fail", "kwargs": {}}],
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=4,
        runtime_observation_handler=observation_handler,
    )

    assert records
    convergence = next(
        observation
        for observation in observations
        if observation.get("kind") == "loop_failed_action_converged"
    )
    serialized = json.dumps(convergence, ensure_ascii=False, default=str).encode("utf-8")
    assert len(serialized) <= 16000, len(serialized)
    assert marker.encode("utf-8") not in serialized


@pytest.mark.asyncio
async def test_triggerflow_unchanged_typed_evidence_page_converges_without_round_cap(tmp_path) -> None:
    agent: Any = Agently.create_agent("unchanged-evidence-convergence").use_task_workspace(
        tmp_path / "task_workspace"
    )
    flow = agent.action._flow_controller.create_named_action_flow("TriggerFlowActionFlow")
    observations: list[dict[str, Any]] = []
    planning_rounds: list[int] = []
    content_version = hashlib.sha256(b"immutable artifact page").hexdigest()

    async def planning_handler(context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        planning_rounds.append(int(context["round_index"]))
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "action_id": "read_action_artifact",
                    "action_input": {
                        "selection_key": "sel-stable-page",
                        "offset": 0,
                        "max_bytes": 64,
                    },
                    "purpose": "Read the same immutable evidence page",
                }
            ],
        }

    async def execution_handler(
        _context: dict[str, Any],
        _request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        transfer = {
            "ok": True,
            "status": "success",
            "owner": "action_artifact",
            "locator": "sel-stable-page",
            "content_version": content_version,
            "range": {"offset": 0, "end": 64, "read_bytes": 64},
            "value": "immutable artifact page",
        }
        return [
            {
                "action_call_id": f"recall-{len(planning_rounds)}",
                "action_id": "read_action_artifact",
                "purpose": "Read the same immutable evidence page",
                "status": "success",
                "success": True,
                "ok": True,
                "result": transfer,
                "data": transfer,
            }
        ]

    async def observation_handler(observation: dict[str, Any]) -> None:
        observations.append(copy.deepcopy(observation))

    records = await flow.async_run(
        action=agent.action,
        prompt=agent.request.prompt,
        settings=agent.settings,
        action_list=[
            {
                "action_id": "read_action_artifact",
                "desc": "Read one immutable artifact page.",
                "kwargs": {},
            }
        ],
        planning_handler=planning_handler,
        execution_handler=execution_handler,
        max_rounds=None,
        runtime_observation_handler=observation_handler,
    )

    assert len(planning_rounds) == 3
    assert len(records) == 3
    convergence = next(
        observation
        for observation in observations
        if observation.get("kind") == "loop_unchanged_evidence_converged"
    )
    assert convergence["payload"]["occurrence_count"] == 3
    assert convergence["payload"]["evidence_identities"] == [
        {
            "owner": "action_artifact",
            "locator": "sel-stable-page",
            "content_version": content_version,
            "range": {"offset": 0, "end": 64, "read_bytes": 64},
        }
    ]


@pytest.mark.asyncio
async def test_unchanged_evidence_counter_resets_when_same_round_has_other_information_progress() -> None:
    from agently.builtins.plugins.ActionFlow.TriggerFlowActionFlow import (
        TriggerFlowActionFlow,
    )

    class State:
        def __init__(self):
            self.values = {}

        def get_state(self, key, default=None):
            return self.values.get(key, default)

        async def async_set_state(self, key, value):
            self.values[key] = value

    state = State()
    page = {
        "status": "success",
        "result": {
            "owner": "action_artifact",
            "locator": "sel-stable-page",
            "content_version": "sha256:stable",
            "range": {"offset": 0, "end": 64, "read_bytes": 64},
        },
    }
    fresh = {
        "status": "success",
        "action_id": "repo_search",
        "result": {"matches": ["new/source.py:12"]},
    }

    assert (
        await TriggerFlowActionFlow._update_unchanged_evidence_page_state(
            state,
            [page],
            max_consecutive_unchanged_evidence_rounds=3,
        )
    )[:2] == (False, 1)
    assert (
        await TriggerFlowActionFlow._update_unchanged_evidence_page_state(
            state,
            [page],
            max_consecutive_unchanged_evidence_rounds=3,
        )
    )[:2] == (False, 2)
    assert await TriggerFlowActionFlow._update_unchanged_evidence_page_state(
        state, [page, fresh], max_consecutive_unchanged_evidence_rounds=3
    ) == (False, 0, [])
    assert (
        await TriggerFlowActionFlow._update_unchanged_evidence_page_state(
            state,
            [page],
            max_consecutive_unchanged_evidence_rounds=3,
        )
    )[:2] == (False, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_name", ["TriggerFlowActionFlow", "DAGActionFlow"])
@pytest.mark.parametrize("error_shape", ["structured", "opaque_string"])
async def test_action_flow_failure_bounds_complete_error_observation_carriers(
    tmp_path,
    flow_name: str,
    error_shape: str,
) -> None:
    message_tail = f"{flow_name}_ERROR_MESSAGE_TAIL"
    traceback_tail = f"{flow_name}_ERROR_TRACEBACK_TAIL"
    secret_value = f"{flow_name}-error-secret"
    opaque_message = secret_value + ("u" * (1024 * 1024)) + message_tail
    body_probe = "u" * 128 if error_shape == "opaque_string" else "m" * 128
    agent: Any = Agently.create_agent(f"error-observation-{flow_name}-{error_shape}").use_task_workspace(
        tmp_path / flow_name / error_shape
    )
    flow = agent.action._flow_controller.create_named_action_flow(flow_name)
    direct_observations: list[dict[str, Any]] = []
    runtime_events: list[Any] = []
    hook_name = f"test.error_observation.{flow_name}.{error_shape}"
    Agently.event_center.register_hook(
        lambda event: runtime_events.append(event),
        event_types=["action.loop_failed", "tool.loop_failed"],
        hook_name=hook_name,
    )

    async def observation_handler(observation: dict[str, Any]) -> None:
        direct_observations.append(observation)
        await agent.action._flow_controller.async_emit_action_flow_observation(observation)

    async def planning_handler(_context: dict[str, Any], _request: dict[str, Any]) -> dict[str, Any]:
        if error_shape == "opaque_string":
            raise RuntimeError(opaque_message)
        try:
            raise ValueError(
                {
                    "body": ("t" * (1024 * 1024)) + traceback_tail,
                    "api_key": secret_value,
                }
            )
        except ValueError as cause:
            raise RuntimeError(
                {
                    "body": ("m" * (1024 * 1024)) + message_tail,
                    "api_key": secret_value,
                }
            ) from cause

    async def execution_handler(
        _context: dict[str, Any],
        _request: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return []

    try:
        with pytest.raises(RuntimeError):
            await flow.async_run(
                action=agent.action,
                prompt=agent.request.prompt,
                settings=agent.settings,
                action_list=[{"action_id": "never_runs", "desc": "never runs", "kwargs": {}}],
                planning_handler=planning_handler,
                execution_handler=execution_handler,
                max_rounds=1,
                runtime_observation_handler=observation_handler,
            )
    finally:
        Agently.event_center.unregister_hook(hook_name)

    direct_failure = next(
        observation
        for observation in direct_observations
        if observation.get("kind") == "loop_failed"
    )
    action_event = next(event for event in runtime_events if event.event_type == "action.loop_failed")
    tool_event = next(event for event in runtime_events if event.event_type == "tool.loop_failed")
    carriers = {
        "direct": direct_failure,
        "action": action_event.model_dump(mode="json"),
        "tool": tool_event.model_dump(mode="json"),
    }
    measurements: dict[str, dict[str, Any]] = {}
    violations: list[str] = []
    projected_errors: dict[str, dict[str, Any]] = {}
    for name, carrier in carriers.items():
        serialized = json.dumps(carrier, ensure_ascii=False, default=str).encode("utf-8")
        projected_error = carrier.get("error")
        error_is_mapping = isinstance(projected_error, dict)
        measurements[name] = {
            "bytes": len(serialized),
            "message_tail": message_tail.encode("utf-8") in serialized,
            "traceback_tail": traceback_tail.encode("utf-8") in serialized,
            "secret": secret_value.encode("utf-8") in serialized,
            "body": body_probe.encode("utf-8") in serialized,
            "error_is_mapping": error_is_mapping,
        }
        if (
            len(serialized) > 16000
            or any(
                measurements[name][key]
                for key in ("message_tail", "traceback_tail", "secret", "body")
            )
            or not error_is_mapping
        ):
            violations.append(name)
        if error_is_mapping:
            projected_errors[name] = projected_error
    assert violations == [], json.dumps(measurements, sort_keys=True)
    assert projected_errors["direct"] == projected_errors["action"] == projected_errors["tool"]
    assert len(projected_errors["direct"]["message"].encode("utf-8")) <= 2400
    assert len(projected_errors["direct"]["traceback"].encode("utf-8")) <= 6000
    probes = (message_tail, traceback_tail, secret_value, body_probe)
    for projected_error in projected_errors.values():
        assert all(probe not in projected_error["message"] for probe in probes)
        assert all(probe not in projected_error["traceback"] for probe in probes)
    if error_shape == "opaque_string":
        details = projected_errors["direct"]["details"]
        assert projected_errors["direct"]["message"] == "Opaque exception message redacted."
        assert details["message_bytes"] == len(opaque_message.encode("utf-8"))
        assert details["message_sha256"] == hashlib.sha256(opaque_message.encode("utf-8")).hexdigest()
    for carrier in carriers.values():
        serialized = json.dumps(carrier, ensure_ascii=False, default=str)
        assert serialized.count('"error":') == 1
        assert "error" not in carrier.get("payload", {})
        payload = json.dumps(carrier.get("payload", {}), ensure_ascii=False, default=str)
        assert all(probe not in payload for probe in probes)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["promotion", "terminal_event"])
async def test_action_artifact_terminal_failure_still_releases_only_owner_scope(
    tmp_path,
    monkeypatch,
    failure_stage: str,
) -> None:
    from agently.builtins.plugins.AgentOrchestrator.AgentlyAgentOrchestrator.modules.route_execution import (
        _finalize_terminal_execution,
    )

    agent: Any = Agently.create_agent(f"action-artifact-release-{failure_stage}").use_task_workspace(tmp_path / failure_stage)
    action_id = f"produce_release_failure_{failure_stage}"
    agent.action.register_action(
        action_id=action_id,
        desc="Produce a large value for terminal failure release coverage.",
        kwargs={"marker": (str, "Scope marker.")},
        func=lambda marker: {"marker": marker, "body": marker * (1024 * 1024)},
    )
    owner = agent.input("Finalize the owner scope.").create_execution().strategy("direct")
    concurrent = agent.input("Keep the concurrent scope alive.").create_execution().strategy("direct")

    async def execute_for(execution: Any, marker: str) -> dict[str, Any]:
        return await agent.action.async_execute_action(
            action_id,
            {"marker": marker},
            artifact_scope={"kind": "agent_execution", "id": execution.id},
        )

    owner_record, concurrent_record = await asyncio.gather(
        execute_for(owner, "o"),
        execute_for(concurrent, "c"),
    )
    owner_ref = next(
        ref for ref in owner_record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    concurrent_ref = next(
        ref for ref in concurrent_record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    unselected_record = await execute_for(owner, "u")
    unselected_ref = next(
        ref for ref in unselected_record["artifact_refs"] if ref.get("artifact_type") == "action_output"
    )
    owner.logs["artifact_refs"] = [owner_ref, unselected_ref]
    owner.result = {
        "accepted": True,
        "artifact_refs": [{"selection_key": owner_ref["selection_key"]}],
        "reply": "bounded",
    }
    owner.status = "success"
    owner_canonical_ref = _canonical_action_ref(
        agent.action._artifact_manager,
        owner_ref,
        {"kind": "agent_execution", "id": owner.id},
    )
    concurrent_canonical_ref = _canonical_action_ref(
        agent.action._artifact_manager,
        concurrent_ref,
        {"kind": "agent_execution", "id": concurrent.id},
    )
    unselected_canonical_ref = _canonical_action_ref(
        agent.action._artifact_manager,
        unselected_ref,
        {"kind": "agent_execution", "id": owner.id},
    )

    if failure_stage == "promotion":
        async def fail_promotion(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("promotion unavailable " + "p" * 500)

        monkeypatch.setattr(owner.record_store, "put_artifact_ref", fail_promotion)
    else:
        owner._ensure_agent_execution_run_context()

        async def fail_terminal_event(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("terminal event unavailable " + "e" * 500)

        monkeypatch.setattr(agent, "_async_emit_agent_execution_terminal_event", fail_terminal_event)

    await _finalize_terminal_execution(owner, terminal_status="completed")

    assert agent.action._artifact_manager.get_artifact_value(owner_canonical_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(unselected_canonical_ref["artifact_id"]) is None
    assert agent.action._artifact_manager.get_artifact_value(concurrent_canonical_ref["artifact_id"]) is not None
    release_diagnostic = owner.diagnostics["action_artifact_release"]
    assert release_diagnostic["status"] == "released"
    assert release_diagnostic["scope"] == {"kind": "agent_execution", "id": owner.id}
    assert release_diagnostic["preserved_artifact_ids"] == []
    retention_diagnostics = owner.diagnostics["task_workspace_retention"]["diagnostics"]
    if failure_stage == "terminal_event":
        assert retention_diagnostics
        assert retention_diagnostics[0]["code"] == "agent_execution.retention.terminal_event_delivery_failed"
    else:
        assert retention_diagnostics == []
    assert all(len(str(item.get("message", ""))) <= 360 for item in retention_diagnostics)
