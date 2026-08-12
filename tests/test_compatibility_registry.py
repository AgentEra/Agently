from __future__ import annotations

import json
from pathlib import Path

from agently.compatibility import (
    CURRENT_FRAMEWORK_VERSION,
    CURRENT_RELEASE_TRAIN,
    get_current_release_manifest,
    get_devtools_compatibility_manifest,
    get_skills_compatibility_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "compatibility" / "index.json"
IN_DEVELOPMENT_PATH = ROOT / "compatibility" / "in-development.json"


def _development_manifest() -> dict:
    return json.loads(IN_DEVELOPMENT_PATH.read_text(encoding="utf-8"))


def test_current_release_manifest_matches_registry_release_file() -> None:
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    release_path = ROOT / index["release_files"][CURRENT_FRAMEWORK_VERSION]
    release_manifest = json.loads(release_path.read_text(encoding="utf-8"))
    current_manifest = get_current_release_manifest()

    assert index["latest_release"] == CURRENT_FRAMEWORK_VERSION
    assert current_manifest == release_manifest


def test_4_1_4_6_release_manifest_pins_stage_native_runtime_contract() -> None:
    manifest = get_current_release_manifest()

    assert CURRENT_FRAMEWORK_VERSION == "4.1.4.6"
    assert CURRENT_RELEASE_TRAIN == "2026-07-4.1.4.6"
    stage_support = manifest["runtime_support"]["agently_stage"]
    assert stage_support["version_specifier"] == ">=0.3.5,<0.4.0"
    assert stage_support["task_mechanism_owners"] == ["TriggerFlowExecution"]
    assert stage_support["public_runtime_surface"] is False
    assert "EventCenter background task settlement" in stage_support[
        "rejected_mechanism_replacements"
    ]
    assert stage_support["semantic_owners_unchanged"] is True


def test_companion_views_still_derive_from_released_manifest() -> None:
    current = get_current_release_manifest()
    devtools = get_devtools_compatibility_manifest()
    skills = get_skills_compatibility_manifest()

    assert devtools["framework_version"] == CURRENT_FRAMEWORK_VERSION
    assert devtools["release_train"] == CURRENT_RELEASE_TRAIN
    assert devtools["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]
    assert skills["authoring_protocol"] == current["companions"]["skills"]["authoring_protocol"]


def test_in_development_manifest_declares_4_1_4_7_owner_boundaries() -> None:
    manifest = _development_manifest()

    assert manifest["target_version"] == "4.1.4.7"
    assert manifest["release_train"] == "2026-08-4.1.4.7-dev"
    assert "Agently-Stage >=0.3.6,<0.4.0" in manifest["notes"]
    assert "physically safe carrier" in manifest["notes"]
    assert "provider-owned sync wrapper" in manifest["notes"]
    assert "FunctionShifter.syncify/asyncify" in manifest["notes"]
    assert "Stage.as_sync/as_async" in manifest["notes"]
    assert "default_stage_call_bridge usage remains unchanged" in manifest["notes"]
    assert "TriggerFlowExecution remains the semantic lifecycle owner" in manifest["notes"]

    stage_support = manifest["runtime_support"]["agently_stage"]
    assert stage_support["version_specifier"] == ">=0.3.6,<0.4.0"
    assert stage_support["public_runtime_surface"] is False
    assert stage_support["task_mechanism_owners"] == ["TriggerFlowExecution"]
    assert "EventCenter background task settlement" in stage_support[
        "rejected_mechanism_replacements"
    ]
    assert stage_support["semantic_owners_unchanged"] is True

    companions = manifest["companions"]
    assert companions["task_context"]["reader"] == "ContextReader"
    assert companions["task_context"]["derived_index_owner"] == (
        "TaskContext internal ContextIndex"
    )
    assert "async_enumerate_descriptors" in companions["task_context"][
        "source_protocol"
    ]
    assert "async_read_exact" in companions["task_context"]["source_protocol"]
    assert companions["task_context"]["source_kinds"] == "open adapter vocabulary"
    assert companions["task_workspace"]["default_root"].endswith(
        ".agently/task_workspaces/<agent-id>"
    )
    assert "verifier acceptance" in companions["task_workspace"][
        "terminal_artifact_contract"
    ]
    assert companions["record_store"]["local_state"].endswith(
        ".agently/records/records.db"
    )
    assert companions["session_memory"]["storage_owner"] == "RecordStore"

    execution_contract = manifest["request_input"]["agent_execution_request_scope"]
    assert "AgentExecution.ensure_long_output" in execution_contract["surface"]
    assert "first request keeps its original contract" in execution_contract["contract"]
    assert "cannot be combined with an explicit AgentTask strategy" in execution_contract[
        "contract"
    ]


def test_in_development_skill_contract_reconnects_to_agent_execution() -> None:
    manifest = _development_manifest()
    skills = manifest["companions"]["skills"]
    contract = skills["runtime_contract"]

    assert contract["installed_truth_owner"].startswith("SkillLibrary")
    assert contract["selection_and_binding_owner"].startswith("AgentExecution")
    assert "TaskContext" in contract["disclosure_owner"]
    assert "Agently.skills_executor" in contract["compatibility_facade"]
    assert "No Skills route" in contract["execution_policy"]
    assert "SkillSourceProvider" in contract["remote_source_policy"]
    assert "immutable local snapshots" in contract["remote_source_policy"]

    request_contract = manifest["request_input"]["skills"]
    assert "AgentExecution.use_skills" in request_contract["surface"]
    assert "Agent.run_skills_task" in request_contract["surface"]
    assert "result-shaped adapter" in request_contract["contract"]


def test_in_development_blocks_and_devtools_keep_owner_boundaries() -> None:
    manifest = _development_manifest()
    blocks = manifest["companions"]["blocks"]
    devtools = manifest["companions"]["devtools"]

    assert blocks["removed_block_kinds"] == ["skill_activation", "workspace_operation"]
    assert "caller-bound ContextReader" in blocks["context_read_contract"]
    assert devtools["runtime_protocol"] == "agently-devtools.observation-runtime.v1"
    assert "TaskWorkspace is never an event store" in (
        devtools["runtime_control"]["record_store_contract"]
    )


def test_in_development_triggerflow_snapshot_projection_contract() -> None:
    triggerflow = _development_manifest()["companions"]["triggerflow"]
    contract = triggerflow["snapshot_projection_contract"]

    assert "schema v2" in contract
    assert "schema-v1" in contract
    assert "set_snapshot_projection_policy" in contract
    assert "pending recovery state remains complete" in contract
    assert "whole-snapshot byte limit" in contract


def test_in_development_code_execution_and_evidence_replan_contracts() -> None:
    manifest = _development_manifest()
    action_runtime = manifest["companions"]["action_runtime"]
    task_loop = manifest["request_input"]["agent_execution_task_loop"]

    assert action_runtime["code_execution_languages"] == [
        "python>=3.10",
        "nodejs>=18",
        "go>=1.25",
        "cpp20",
    ]
    assert "toolchain-version" in action_runtime["provider_selection_contract"]
    assert "Action result metadata" in action_runtime["provider_selection_contract"]
    assert "evidence-reacquisition card" in task_loop["evidence_replan_contract"]
    assert "final-artifact self-readback" in task_loop["evidence_replan_contract"]


def test_public_typing_contract_remains_explicit() -> None:
    public_typing = _development_manifest()["public_typing"]

    assert public_typing["status"] == "required"
    assert "compatibility/public-typing-allowlist.json" in public_typing["surface"]
    assert "typed parameters and returns" in public_typing["contract"]
    assert "not a public-method allowlist" in public_typing["compatibility_policy"]
