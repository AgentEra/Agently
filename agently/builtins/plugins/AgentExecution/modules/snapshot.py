"""Data-only snapshots of settled execution boundaries with explicit rebinding."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any, cast, get_origin

from pydantic import BaseModel, TypeAdapter

from agently.core.application.AgentExecution import AgentExecutionStream
from agently.core.storage import RecordStoreContextSource
from agently.core.TaskWorkspace import TaskWorkspaceContextSource
from agently.types.data import OutputValidateHandler, RunContext
from agently.types.plugins import ContextSource

from ..long_task.Rework import export_task_state, restore_task_state
from .goal_preparation import PreparedGoal, retain_prepared_goal
from .lifecycle import pause_flow
from .production import ProductionOptions
from .revisions import _HISTORY_FIELDS, export_history, restore_history
from .route_execution import finish_production, prepare_production

if TYPE_CHECKING:
    from .execution import AgentExecution


_FORMAT = "agently.execution.snapshot.v1"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _contract_value(value: object) -> object:
    if get_origin(value) is Annotated:
        return {"annotation_schema": TypeAdapter(value).json_schema()}
    if isinstance(value, type):
        if issubclass(value, BaseModel):
            return {"model_schema": value.model_json_schema()}
        return {"type": f"{value.__module__}.{value.__qualname__}"}
    if isinstance(value, Mapping):
        return {str(key): _contract_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_contract_value(item) for item in value]
    # Fail closed on live objects instead of serializing repr(), pickle or code.
    _json(value)
    return value


def _handler_key(value: object) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None)
    if not callable(value) or not isinstance(module, str) or not isinstance(name, str):
        raise TypeError("Snapshot callbacks need an explicitly rebound named callable.")
    return f"{module}.{name}"


def _validators(owner: AgentExecution) -> list[OutputValidateHandler]:
    declared = owner.request.extension_handlers.get("validate_handlers", [])
    handlers = list(declared) if isinstance(declared, list) else []
    options = owner._production_options
    if options is not None and options.validate_handler is not None:
        extra = options.validate_handler
        handlers.extend(extra if isinstance(extra, list) else [extra])
    return handlers


def _requirements(owner: AgentExecution) -> dict[str, object]:
    custom_bindings = []
    standard = {owner._task_workspace_context_binding_id, owner._record_store_context_binding_id,
                owner._skill_context_binding_id, owner._session_memory_context_binding_id}
    if owner.task_record is not None:
        standard.add(f"task_evidence_binding:{owner.task_record.id}")
    context = owner.task_context.snapshot()
    guidance_ids = {item.get("context_entry_id") for item in owner.guidance_items}
    for binding in context.bindings:
        if binding.binding_id not in standard:
            custom_bindings.append(binding.to_dict())
    library = owner.skill_library
    skill_catalog = [] if library is None else [
        {"revision_ref": package.revision_ref, "content_digest": _hash(package.to_dict())}
        for package in sorted(library.list(), key=lambda item: item.revision_ref)
    ]
    session = getattr(owner.agent, "activated_session", None)
    memory_factory = getattr(session, "create_memory_context_source", None)
    memory_source = cast(ContextSource | None,
                         memory_factory(settings=owner.request.settings) if callable(memory_factory) else None)
    requirements = {
        "workspace": owner.task_workspace.task_workspace_id,
        "workspace_mode": owner.task_workspace.mode,
        "record_store_root": str(getattr(owner.record_store, "root", "")),
        "actions": owner.required_action_ids(),
        "action_scope": list(owner.local_action_ids),
        "action_candidates": _contract_value(owner.action_candidates()),
        "skill_scope": _contract_value(owner.skill_candidate_summary()),
        "skill_catalog": skill_catalog,
        "session_memory": None if memory_source is None else {
            "source_id": memory_source.source_id, "source_revision": memory_source.source_revision,
        },
        "skills": _contract_value(owner.local_skill_selectors),
        "skill_packs": _contract_value(owner.local_skills_pack_selectors),
        "context_sources": custom_bindings,
        "context_entries": [_contract_value(entry.to_dict()) for entry in context.entries
                            if entry.entry_id not in owner._task_context_prompt_entry_ids
                            and entry.entry_id not in guidance_ids
                            and entry.entry_id not in {f"execution_rework:{owner.id}:{revision}"
                                                       for revision in range(1, owner.revision + 1)}],
        "validators": [_handler_key(handler) for handler in _validators(owner)],
        "reviews": [_contract_value({**item, "handler": _handler_key(item.get("handler"))})
                    for item in owner.review_declarations],
        "artifacts": [_contract_value({**item, "handler": _handler_key(item.get("handler"))})
                      for item in owner.artifact_declarations],
        "interaction": _handler_key(owner._interaction_handler),
    }
    return cast(dict[str, object], json.loads(_json(requirements)))


def _fingerprint(owner: AgentExecution) -> str:
    prompt = owner.prompt_snapshot if owner._started else owner._snapshot_prompt()
    contract = {
        "prompt": prompt,
        "options": dict(owner.options),
        "limits": dict(owner.limits),
        "strategy": owner.strategy_name,
        "goal_switch": owner._goal_turn_on_long_task,
        "ensure_long_output": owner._ensure_long_output_enabled,
        "model_key": getattr(owner.request, "_model_key", None),
    }
    media = {name: value for name in ("image_groups", "image_direct", "vlm_only")
             if (value := owner.request.settings.get(f"execution.{name}"))}
    if media:
        contract["image_routing"] = media
    return _hash(_contract_value(contract))


def save(owner: AgentExecution) -> dict[str, object]:
    if owner._bound_agent_capabilities or owner._audio_inputs:
        raise NotImplementedError("Snapshots with extra Agent capability bindings require a custom rebinding contract.")
    if owner.status != "paused" or owner._pause_flow is None:
        raise RuntimeError("Execution save requires a settled safe pause.")
    if owner._run_completion is not None and not owner._run_completion.done():
        raise RuntimeError("Execution pause has not settled; await its current run before save.")
    if owner._parent_model_request_budget is not None:
        raise RuntimeError("Nested execution snapshots require parent-budget rebinding and are unsupported.")
    if owner._production_options is None:
        raise RuntimeError("Execution has no retained production contract.")
    from agently.base import execution_resource
    resources = [execution_resource.inspect(key) for key in owner.execution_context._resource_handle_ids]
    if any(handle is not None and handle.get("status") != "released" for handle in resources):
        raise RuntimeError("Execution snapshot cannot restore live ExecutionResource state; settle or release it first.")
    requirements = _requirements(owner)
    candidate = owner.result if owner._pause_boundary == "candidate_ready" else None
    _json(candidate)
    options = asdict(owner._production_options)
    options.pop("validate_handler", None)
    snapshot: dict[str, object] = {
        "format": _FORMAT, "plugin": owner.name, "plugin_version": 1,
        "execution_id": owner.id, "lineage": dict(owner.lineage),
        "revisions": {
            "current": owner.revision, "history": export_history(owner),
            "limit": owner._rework_limit, "feedback": owner._rework_feedback,
            "allow_replay": owner._rework_allow_replay, "producer_state": owner._producer_state,
            "dispatched_actions": sorted(owner.execution_context._dispatched_action_ids),
            "blocked_actions": sorted(owner.execution_context._rework_blocked_action_ids),
            "task_state": export_task_state(owner),
        },
        "boundary": owner._pause_boundary, "contract_fingerprint": _fingerprint(owner),
        "requirements": requirements, "production_options": options,
        "route": owner.route_info.get("selected_route"),
        "route_info": dict(owner.route_info), "task_refs": dict(owner.task_refs),
        "guidance": list(owner.guidance_items),
        "review_contract": dict(owner._review_contract),
        "prepared_goal": owner._prepared_goal.to_record() if owner._prepared_goal is not None else None,
        "terminal_task_handoff_refs": list(owner._terminal_task_handoff_refs),
        "candidate": candidate, "candidate_digest": _hash(candidate),
        "request_validated": owner._candidate_request_validated,
        "workspace_digest": owner._task_workspace_context_source.source_revision,
        "run_context": (owner.agent_execution_run_context.model_dump(exclude={"meta"})
                        if owner.agent_execution_run_context is not None else None),
        "started_event_emitted": owner._agent_execution_started_emitted,
        "model_requests_used": owner.execution_context.model_request_count,
        "elapsed_seconds": max(0.0, time.monotonic() - owner.execution_context.started_at),
        "saved_at": time.time(),
        "flow": owner._pause_flow.save(require_idle=True),
    }
    snapshot["revisions_digest"] = _hash(snapshot["revisions"])
    # Roundtrip is an isolation barrier: no aliases to live result/state objects.
    return cast(dict[str, object], json.loads(_json(snapshot)))


def load(owner: AgentExecution, snapshot: Mapping[str, object]) -> None:
    if owner._bound_agent_capabilities or owner._audio_inputs:
        raise NotImplementedError("Snapshots with extra Agent capability bindings require a custom rebinding contract.")
    if owner._started or owner._run_completion is not None or owner._closed:
        raise RuntimeError("Load requires a fresh, explicitly configured execution.")
    state = cast(dict[str, Any], json.loads(_json(dict(snapshot))))
    if state.get("format") != _FORMAT or state.get("plugin_version") != 1:
        raise ValueError("Unsupported execution snapshot version.")
    if state.get("plugin") != owner.name:
        raise ValueError("Execution snapshot belongs to a different plugin.")
    boundary = state.get("boundary")
    if boundary not in {"before_production", "candidate_ready", "long_task_step"}:
        raise ValueError("Execution snapshot has no supported safe boundary.")
    identity = state.get("execution_id")
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Execution snapshot identity is invalid.")
    if state.get("contract_fingerprint") != _fingerprint(owner):
        raise ValueError("Rebound execution draft or limits do not match the saved contract.")
    if state.get("requirements") != _requirements(owner):
        raise ValueError("Execution snapshot has missing or changed resource/policy bindings.")
    if owner._parent_model_request_budget is not None:
        raise RuntimeError("Nested execution snapshot load requires parent-budget rebinding.")
    revisions = state.get("revisions")
    if not isinstance(revisions, dict) or state.get("revisions_digest") != _hash(revisions):
        raise ValueError("Execution snapshot revision identity changed.")
    revision = revisions.get("current")
    history = revisions.get("history")
    if (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
        or not isinstance(history, list) or len(history) != revision
        or any(not isinstance(item, dict) or not isinstance(item.get("fields"), dict)
               or set(item["fields"]) != set(_HISTORY_FIELDS)
               or item["fields"]["revision"] != index or not isinstance(item.get("meta"), dict)
               or not isinstance(item.get("stream"), list)
               for index, item in enumerate(history))):
        raise ValueError("Invalid execution revision history.")
    limit = revisions.get("limit")
    if ((revision and (isinstance(limit, bool) or not isinstance(limit, int) or limit < revision))
        or not isinstance(revisions.get("allow_replay"), bool)
        or not isinstance(revisions.get("producer_state"), dict)
        or (revision and not isinstance(revisions.get("feedback"), str))):
        raise ValueError("Invalid execution revision contract.")
    for key in ("dispatched_actions", "blocked_actions"):
        items = revisions.get(key)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError("Invalid execution replay protection.")
    count = state.get("model_requests_used")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("Invalid saved model request count.")
    for key in ("elapsed_seconds", "saved_at"):
        value = state.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid execution snapshot {key}.")
    if state.get("candidate_digest") != _hash(state.get("candidate")):
        raise ValueError("Execution snapshot candidate content identity changed.")
    for key in ("lineage", "route_info", "task_refs", "review_contract"):
        if not isinstance(state.get(key), dict):
            raise ValueError(f"Invalid execution snapshot {key}.")
    if not isinstance(state.get("terminal_task_handoff_refs"), list):
        raise ValueError("Invalid execution snapshot artifact handoff references.")
    if boundary == "candidate_ready" and not isinstance(state.get("route"), str):
        raise ValueError("Saved candidate is missing its producer route.")
    prepared_goal = (PreparedGoal.from_record(state["prepared_goal"])
                     if state.get("prepared_goal") is not None else None)
    rebound_workspace = owner.task_workspace._derive(execution_id=identity)
    if state.get("workspace_digest") != TaskWorkspaceContextSource(rebound_workspace).source_revision:
        raise ValueError("Execution snapshot workspace content identity changed.")
    raw_context = state.get("run_context")
    run_context = RunContext.model_validate(raw_context) if raw_context is not None else None
    if run_context is not None and run_context.execution_id != identity:
        raise ValueError("Execution snapshot run lineage belongs to a different execution.")
    raw_options = state.get("production_options")
    if not isinstance(raw_options, dict) or "validate_handler" in raw_options:
        raise ValueError("Invalid or executable production options in snapshot.")
    options = ProductionOptions(**raw_options)
    if (options.type not in {"original", "parsed", "all"}
        or options.key_style not in {"dot", "slash"}
        or isinstance(options.max_retries, bool) or not isinstance(options.max_retries, int)
        or options.max_retries < 0
        or not isinstance(options.raise_ensure_failure, bool)
        or (options.ensure_all_keys is not None and not isinstance(options.ensure_all_keys, bool))
        or (options.ensure_keys is not None and (
            not isinstance(options.ensure_keys, list)
            or any(not isinstance(key, str) for key in options.ensure_keys)
        ))):
        raise ValueError("Invalid execution snapshot production options.")
    guidance = state.get("guidance", [])
    if not isinstance(guidance, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str)
        or (item.get("context_entry_id") is not None and not isinstance(item.get("content"), str))
        for item in guidance
    ):
        raise ValueError("Invalid saved execution guidance.")
    raw_flow = state.get("flow")
    if not isinstance(raw_flow, dict):
        raise ValueError("Execution snapshot has no TriggerFlow continuation.")
    # This is a fixed trusted definition. The snapshot cannot select imports,
    # graph code, provider classes, or resource paths.
    flow = pause_flow().create_execution(
        auto_close=False, record_store=owner.record_store,
        runtime_resources={"agent_execution": owner, "record_store": owner.record_store},
        intervention_mode=None,
    )
    flow.load(raw_flow, runtime_resources={"agent_execution": owner, "record_store": owner.record_store},
              validate_resources=True)
    pending = flow.get_pending_interrupts()
    if set(pending) != {"execution-pause"} or pending["execution-pause"].get("payload") != {
        "execution_id": identity, "boundary": boundary,
    }:
        raise ValueError("Execution snapshot pause identity does not match its continuation.")

    owner._refresh_prompt_snapshot()
    owner.id = identity
    owner.lineage = state["lineage"]
    owner.task_workspace = rebound_workspace
    owner.record_store = owner.record_store._bind_execution(identity, scope={"execution_id": identity},
                                                            search_scope={"execution_id": identity})
    owner.task_context.remove(owner._task_workspace_context_binding_id)
    if owner._record_store_context_binding_id is not None:
        owner.task_context.remove(owner._record_store_context_binding_id)
    owner.task_context.task_id = identity
    owner.task_context.context_id = f"agent_execution:{identity}:context"
    owner._task_workspace_context_source = TaskWorkspaceContextSource(owner.task_workspace)
    owner._task_workspace_context_binding_id = owner.task_context.attach(
        owner._task_workspace_context_source, binding_id=f"task_workspace_binding:{identity}", scope="execution",
    )
    owner._record_store_context_binding_id = owner.task_context.attach(
        RecordStoreContextSource(owner.record_store), binding_id=f"record_store_binding:{identity}", scope="execution",
    )
    owner._replace_runtime_context()
    owner.execution_context.model_request_budget.count = count
    owner.execution_context._dispatched_action_ids = set(revisions["dispatched_actions"])
    owner.execution_context._rework_blocked_action_ids = set(revisions["blocked_actions"])
    owner.revision = revision
    owner._rework_limit = limit
    owner._rework_feedback = revisions["feedback"]
    owner._rework_allow_replay = revisions["allow_replay"]
    owner._producer_state = revisions["producer_state"]
    elapsed = state["elapsed_seconds"] + max(0.0, time.time() - state["saved_at"])
    owner.execution_context.started_at = time.monotonic() - elapsed
    owner.stream = AgentExecutionStream(execution_id=identity, lineage=owner.lineage).bind_execution(owner)
    owner._production_options = options
    owner.agent_execution_run_context = run_context
    owner._agent_execution_started_emitted = bool(state.get("started_event_emitted"))
    owner.result = state.get("candidate")
    owner._restored_result_pending = boundary == "candidate_ready"
    owner.route_info = state["route_info"]
    owner.task_refs = state["task_refs"]
    owner._review_contract = state["review_contract"]
    owner._terminal_task_handoff_refs = state["terminal_task_handoff_refs"]
    if prepared_goal is not None:
        retain_prepared_goal(owner, prepared_goal)
    owner._candidate_request_validated = bool(state.get("request_validated"))
    owner._candidate_validation_handlers = _validators(owner)
    owner.guidance_items = guidance
    for item in guidance:
        entry_id = item.get("context_entry_id")
        if entry_id is not None:
            owner.task_context.put(
                role="information", content=item["content"], entry_id=entry_id,
                source_ref=entry_id, required=True,
                metadata={"source": "execution_interrupt", "author": item.get("author")},
            )
    owner._pending_guidance = [item for item in guidance if item.get("status") == "inserted"]
    owner._pause_flow = flow
    owner._pause_boundary = boundary
    owner._pause_requested = True
    owner._started = True
    owner.status = "paused"

    async def continue_saved() -> tuple[str, object]:
        if boundary in {"before_production", "long_task_step"}:
            return await prepare_production(owner, options)
        route = state.get("route")
        if not isinstance(route, str):
            raise ValueError("Saved candidate is missing its producer route.")
        return await finish_production(owner, route, owner.result,
                                       owner._candidate_validation_handlers, owner._candidate_request_validated)

    owner._paused_continuation = continue_saved
    restore_task_state(owner, revisions.get("task_state"))
    restore_history(owner, history)
