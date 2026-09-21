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


def test_current_candidate_includes_accepted_development_contracts() -> None:
    """A same-version release candidate must not silently ship stale contracts."""
    development = _development_manifest()
    if development["target_version"] != CURRENT_FRAMEWORK_VERSION:
        return  # A later development train intentionally differs from a release.
    current = get_current_release_manifest()
    identity_fields = {"target_version", "release_train", "notes"}
    for key, value in development.items():
        if key not in identity_fields:
            assert current[key] == value, f"Candidate contract is stale: {key}"


def test_4_1_4_8_skills_catalog_is_v3_without_rewriting_archives() -> None:
    for manifest in (get_current_release_manifest(), _development_manifest()):
        skills = manifest["companions"]["skills"]
        assert skills["catalog_generation"] == "v3"
        assert skills["authoring_protocol"] == "agently-skills.authoring.v3"
        assert skills["recommended_bundle"] == "app"
        archives = {item["generation"]: item for item in skills["archived_catalog_generations"]}
        assert archives["v2"]["branch"] == "update/archive-v2-catalog"
        assert archives["v2"]["last_supported_framework_version"] == "4.1.4.7"
        assert archives["v1"]["last_supported_framework_version"] == "4.1.1"
    previous = json.loads((ROOT / "compatibility/releases/4.1.4.7.json").read_text(encoding="utf-8"))
    assert previous["companions"]["skills"]["catalog_generation"] == "v2"


def test_4_1_4_8_release_manifest_pins_stage_native_runtime_contract() -> None:
    manifest = get_current_release_manifest()

    assert CURRENT_FRAMEWORK_VERSION == "4.1.4.8"
    assert CURRENT_RELEASE_TRAIN == "2026-09-4.1.4.8"
    stage_support = manifest["runtime_support"]["agently_stage"]
    assert stage_support["version_specifier"] == ">=0.3.8,<0.4.0"
    assert stage_support["role"] == "required_runtime_dependency"
    assert stage_support["release_order"] == "publish_and_verify_stage_before_raising_agently_minimum"
    assert stage_support["task_mechanism_owners"] == ["TriggerFlowExecution"]
    assert stage_support["public_runtime_surface"] is False
    assert "EventCenter background task settlement" in stage_support["rejected_mechanism_replacements"]
    assert stage_support["semantic_owners_unchanged"] is True


def test_companion_views_still_derive_from_released_manifest() -> None:
    current = get_current_release_manifest()
    devtools = get_devtools_compatibility_manifest()
    skills = get_skills_compatibility_manifest()

    assert devtools["framework_version"] == CURRENT_FRAMEWORK_VERSION
    assert devtools["release_train"] == CURRENT_RELEASE_TRAIN
    assert devtools["runtime_protocol"] == current["companions"]["devtools"]["runtime_protocol"]
    assert skills["authoring_protocol"] == current["companions"]["skills"]["authoring_protocol"]


def test_in_development_manifest_retains_released_owner_boundaries() -> None:
    manifest = _development_manifest()

    assert manifest["target_version"] == "4.1.4.9"
    assert manifest["release_train"] == "2026-09-4.1.4.9-dev"
    assert "carries forward the 4.1.4.7 contract" in manifest["notes"]
    assert "Agently-Stage >=0.3.8,<0.4.0" in manifest["notes"]
    assert "Python 3.14 task-factory keyword arguments" in manifest["notes"]
    assert "physically safe carrier" in manifest["notes"]
    assert "provider-owned sync wrapper" in manifest["notes"]
    assert "deprecated syncify/asyncify adapters" in manifest["notes"]
    assert "Stage.as_sync/as_async" in manifest["notes"]
    assert "default_stage_call_bridge usage remains unchanged" in manifest["notes"]
    assert "TriggerFlowExecution remains the semantic lifecycle owner" in manifest["notes"]

    stage_support = manifest["runtime_support"]["agently_stage"]
    assert stage_support["repository"] == "Agently-Stage"
    assert stage_support["package"] == "agently-stage"
    assert stage_support["role"] == "required_runtime_dependency"
    assert stage_support["version_specifier"] == ">=0.3.8,<0.4.0"
    assert stage_support["release_order"] == ("publish_and_verify_stage_before_raising_agently_minimum")
    assert stage_support["skills_guidance_required"] is True
    assert stage_support["public_runtime_surface"] is False
    assert stage_support["task_mechanism_owners"] == ["TriggerFlowExecution"]
    assert "EventCenter background task settlement" in stage_support["rejected_mechanism_replacements"]
    assert stage_support["semantic_owners_unchanged"] is True

    companions = manifest["companions"]
    assert companions["task_context"]["reader"] == "ContextReader"
    assert companions["task_context"]["derived_index_owner"] == ("TaskContext internal ContextIndex")
    assert "async_enumerate_descriptors" in companions["task_context"]["source_protocol"]
    assert "async_read_exact" in companions["task_context"]["source_protocol"]
    assert companions["task_context"]["source_kinds"] == "open adapter vocabulary"
    assert companions["task_workspace"]["default_root"].endswith(".agently/task_workspaces/<agent-id>")
    assert "verifier acceptance" in companions["task_workspace"]["terminal_artifact_contract"]
    assert companions["record_store"]["local_state"].endswith(".agently/records/records.db")
    assert companions["session_memory"]["storage_owner"] == "RecordStore"

    execution_contract = manifest["request_input"]["agent_execution_request_scope"]
    assert "AgentExecution.ensure_long_output" in execution_contract["surface"]
    assert "AgentExecution.auto_continue" in execution_contract["surface"]
    assert execution_contract["preferred_continuation_surface"] == "AgentExecution.auto_continue"
    assert "first request keeps its original contract" in execution_contract["contract"]
    assert "cannot be combined with an explicit AgentTask strategy" in execution_contract["contract"]


def test_in_development_skill_contract_reconnects_to_agent_execution() -> None:
    manifest = _development_manifest()
    stage_support = manifest["runtime_support"]["agently_stage"]
    skills = manifest["companions"]["skills"]
    contract = skills["runtime_contract"]

    assert contract["installed_truth_owner"].startswith("SkillLibrary")
    assert contract["selection_and_binding_owner"].startswith("AgentExecution")
    assert "same agent.use_*" in contract["composition_contract"]
    assert "no second public Skill collection manager" in contract["composition_contract"]
    assert "empty declarations do not expose" in contract["execution_scope_contract"]
    assert "TaskContext" in contract["disclosure_owner"]
    assert "complete SKILL.md root" in contract["root_disclosure_contract"]
    assert "Agently.skills_executor" in contract["compatibility_facade"]
    assert "not executor-ready" in contract["compatibility_facade"]
    assert "TaskDAGContext" in contract["compatibility_facade"]
    assert "No Skills route" in contract["execution_policy"]
    assert "per-script Action discovery" in contract["execution_policy"]
    assert "one stable ordinary Workspace-backed" in contract["execution_policy"]
    assert "binds permission only in the current execution" in contract["execution_policy"]
    assert "each later user request creates a fresh execution" in contract["execution_scope_contract"]
    assert "released explicit binder remains compatible" in contract["execution_policy"]
    assert "SkillSourceProvider" in contract["remote_source_policy"]
    assert "immutable local snapshots" in contract["remote_source_policy"]
    request_contract = manifest["request_input"]["skills"]
    assert "AgentExecution.use_skills" in request_contract["surface"]
    assert "Agent.enable_skill_script_exec" in request_contract["surface"]
    assert "Agent.bind_skill_script_action" in request_contract["surface"]
    assert "Agent.run_skills_task" in request_contract["surface"]
    assert "result-shaped adapter" in request_contract["contract"]
    assert "agent.use_skills(..., always=True)" in request_contract["contract"]

    stage_guidance = skills["runtime_dependency_guidance"]["agently_stage"]
    assert stage_guidance["skill"] == "agently-stage"
    assert stage_guidance["version_specifier"] == stage_support["version_specifier"]

    assert skills["catalog_generation"] == "v3"
    archived_catalogs = {
        entry["generation"]: entry
        for entry in skills["archived_catalog_generations"]
    }
    assert archived_catalogs["v2"] == {
        "generation": "v2",
        "branch": "update/archive-v2-catalog",
        "last_supported_framework_version": "4.1.4.7",
        "status": "frozen",
    }
    assert archived_catalogs["v1"] == {
        "generation": "v1",
        "branch": "update/archive-legacy-v1-catalog",
        "last_supported_framework_version": "4.1.1",
        "status": "frozen",
    }


def test_in_development_blocks_and_devtools_keep_owner_boundaries() -> None:
    manifest = _development_manifest()
    blocks = manifest["companions"]["blocks"]
    devtools = manifest["companions"]["devtools"]

    assert blocks["removed_block_kinds"] == ["skill_activation", "workspace_operation"]
    assert "caller-bound ContextReader" in blocks["context_read_contract"]
    assert devtools["runtime_protocol"] == "agently-devtools.observation-runtime.v1"
    assert devtools["recommended_version_specifier"] == ">=0.2.0,<0.3.0"
    assert "TaskWorkspace is never an event store" in (devtools["runtime_control"]["record_store_contract"])
    assert "model.reasoning.delta" in devtools["runtime_control"]["model_reasoning_observation_contract"]
    assert "model.validation_failed" in devtools["runtime_control"]["model_validation_diagnostics_contract"]
    console_contract = devtools["runtime_control"]["local_console_streaming_contract"]
    assert "ordered raw delivery" in console_contract
    assert "model.streaming" in console_contract
    assert "source=model_request" in console_contract
    assert "null-delta controls remain Process notifications" in console_contract
    assert "xml_field" in console_contract
    assert "progress_delta" in console_contract
    assert "not printed again" in console_contract
    assert "ExecutionResource environment self-check" in console_contract
    assert "product-language labels" in console_contract
    assert "compact translated lines" in console_contract
    concurrent_console_contract = devtools["runtime_control"][
        "local_console_concurrent_stream_display_contract"
    ]
    assert "first response that emits a delta" in concurrent_console_contract
    assert "first-delta FIFO order" in concurrent_console_contract
    assert "labeled deferred section" in concurrent_console_contract
    assert "ordinary Prompt, provider request, process" in concurrent_console_contract
    assert "approval-required facts remain immediately visible" in concurrent_console_contract
    assert "never blocks, throttles, cancels, retries, serializes" in concurrent_console_contract
    planning_contract = devtools["runtime_control"]["action_planning_projection_contract"]
    assert "execution_resources" in planning_contract
    assert "corrected call" in planning_contract
    assert "never a fabricated execution result" in planning_contract
    response_console_contract = devtools["runtime_control"]["action_planning_console_projection_contract"]
    assert "model_request_role=action_planning" in response_console_contract
    assert "displayed once" in response_console_contract
    assert "detail and EventCenter retain" in response_console_contract


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
    provider_candidates = action_runtime["builtin_provider_candidates"]
    assert {candidate["provider_id"] for candidate in provider_candidates} == {
        "gvisor",
        "seatbelt",
        "landlock",
    }
    assert {candidate["provider_id"]: candidate["isolation_policy"] for candidate in provider_candidates} == {
        "gvisor": "required",
        "seatbelt": "preferred",
        "landlock": "preferred",
    }
    assert all(candidate["fallback"] == "fail_closed" for candidate in provider_candidates)
    assert "contributor-owned" not in action_runtime["builtin_provider_contract"]
    assert "without another provider request" in action_runtime["response_delivery_contract"]
    assert "ensure_long_output" in action_runtime["response_delivery_contract"]
    assert "input(...).info(...).use_action(...)" in action_runtime["fluent_capability_chain_contract"]
    assert "evidence-reacquisition card" in task_loop["evidence_replan_contract"]
    assert "final-artifact self-readback" in task_loop["evidence_replan_contract"]


def test_in_development_programmatic_action_calling_contract() -> None:
    contract = _development_manifest()["companions"]["action_runtime"]["programmatic_action_calling"]

    assert "Agent.set_action_loop(planning_protocol=programmatic)" in contract["surface"]
    assert contract["default_protocol"].startswith("structured_plan")
    assert "ephemeral read-only Action micro-DAG" in contract["scope_contract"]
    assert "lossless-JSON returns contract" in contract["eligibility_contract"]
    assert "host_async_bindings" in contract["execution_contract"]
    assert "concurrency_mode=parallel" in contract["execution_contract"]
    assert "exclusive Actions form ordering barriers" in contract["execution_contract"]
    assert "re-enters ActionDispatcher" in contract["execution_contract"]
    assert "Agent.release_programmatic_action_calls" in contract["catalog_lifecycle_contract"]
    assert "redaction digest/byte facts" in contract["observation_contract"]
    assert "fail-open for DevTools" in contract["observation_contract"]
    assert "planning_observation" in contract["observation_contract"]
    assert "programmatic_observation" in contract["observation_contract"]
    assert "sdk_bytes" in contract["observation_contract"]
    assert "peak_active_binding_calls" in contract["observation_contract"]
    assert "independent observed effects" in contract["performance_contract"]
    assert "renderer v3" in contract["performance_contract"]
    assert "does not promise universal cost or latency" in contract["performance_contract"]


def test_public_typing_contract_remains_explicit() -> None:
    public_typing = _development_manifest()["public_typing"]

    assert public_typing["status"] == "required"
    assert "compatibility/public-typing-allowlist.json" in public_typing["surface"]
    assert "typed parameters and returns" in public_typing["contract"]
    assert "not a public-method allowlist" in public_typing["compatibility_policy"]


def test_agent_execution_plugins_and_terminal_policies_share_one_owner() -> None:
    contract = _development_manifest()["request_input"][
        "agent_execution_plugins_and_terminal_policies"
    ]

    assert "Agent.interact" in contract["surface"]
    assert "AgentExecution.interact" in contract["surface"]
    assert "stable AgentExecution methods" in contract["standard_methods_stability"]
    assert contract["builtin_plugins"] == ["auto", "request", "long_task", "plan", "long_content"]
    assert "Agent.create_execution" in contract["surface"]
    assert "Agent.pattern" not in contract["surface"]
    assert "Agent.verify" not in contract["surface"]
    assert "final_result projection" in contract["contract"]
    assert "only missing fields" in contract["goal_contract"]
    assert "same execution revision" in contract["control_contract"]
    assert "nested parent-budget restoration" in contract["pending_control_contract"]
    assert _development_manifest()["companions"]["skills"]["authoring_protocol"] == "agently-skills.authoring.v3"
    assert _development_manifest()["companions"]["docs"]["public_surface_protocol"] == "agently-docs.public-surface.v2"
    assert "normalized ExecutionExchangeView" in contract["interaction_contract"]
    assert "TriggerFlow remains the only pause/resume owner" in contract[
        "interaction_contract"
    ]


def test_stt_preprocessing_development_does_not_rewrite_released_audio_contract() -> None:
    development = _development_manifest()["audio_capability"]
    assert development["owner"] == "AudioModelRequest"
    assert development["input_preprocessing"]["since"] == "4.1.4.9"
    assert development["observation_protocol_changed"] is False
    assert CURRENT_FRAMEWORK_VERSION == "4.1.4.8"
    assert "input_preprocessing" not in get_current_release_manifest().get("audio_capability", {})
