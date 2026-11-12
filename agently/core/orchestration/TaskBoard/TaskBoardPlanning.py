# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agently.types.data import TaskBoardCard, TaskBoardGraph, TaskBoardRevision

from .TaskBoardValidation import TaskBoardValidator


_FORBIDDEN_PLANNING_KEYS = {
    "allowed_action",
    "allowed_actions",
    "allowed_action_ids",
    "action_allowlist",
    "action_options",
    "fixed_action_options",
    "mandatory_actions",
    "max_cards",
    "max_card_count",
    "max_model_calls",
    "max_model_requests",
    "max_nodes",
    "max_steps",
    "model_call_limit",
    "node_count",
    "required_action",
    "required_actions",
    "required_action_ids",
    "step_count",
}


@dataclass(frozen=True)
class TaskBoardEffortProfile:
    name: str
    complexity_definition: str
    orchestration_guidance: tuple[str, ...]
    reflection_density: str
    evidence_depth: str
    repair_tendency: str
    forbidden_constraints: tuple[str, ...] = field(default_factory=tuple)

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "complexity_definition": self.complexity_definition,
            "orchestration_guidance": list(self.orchestration_guidance),
            "reflection_density": self.reflection_density,
            "evidence_depth": self.evidence_depth,
            "repair_tendency": self.repair_tendency,
            "forbidden_constraints": list(self.forbidden_constraints),
        }


@dataclass(frozen=True)
class TaskBoardPlanningPolicy:
    effort_profile: TaskBoardEffortProfile
    action_block_meaning: str
    task_complexity_basis: tuple[str, ...]
    owner_boundaries: tuple[str, ...]
    control_card_guidance: tuple[str, ...]
    evidence_reuse_guidance: tuple[str, ...]
    repair_orchestration_guidance: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "effort_profile": self.effort_profile.to_prompt_payload(),
            "action_block_meaning": self.action_block_meaning,
            "task_complexity_basis": list(self.task_complexity_basis),
            "owner_boundaries": list(self.owner_boundaries),
            "control_card_guidance": list(self.control_card_guidance),
            "evidence_reuse_guidance": list(self.evidence_reuse_guidance),
            "repair_orchestration_guidance": list(self.repair_orchestration_guidance),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TaskBoardPlanningResult:
    revision: TaskBoardRevision
    planning_policy: TaskBoardPlanningPolicy
    raw_result: Mapping[str, Any]
    diagnostics: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision.to_dict(),
            "planning_policy": self.planning_policy.to_prompt_payload(),
            "raw_result": dict(self.raw_result),
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


def resolve_task_board_effort_profile(effort: Any = "medium") -> TaskBoardEffortProfile:
    name, overrides = _normalize_effort(effort)
    profile = _profile_presets().get(name) or _profile_presets()["medium"]
    if not overrides:
        return profile
    return TaskBoardEffortProfile(
        name=str(overrides.get("name") or profile.name),
        complexity_definition=str(overrides.get("complexity_definition") or profile.complexity_definition),
        orchestration_guidance=_str_tuple(overrides.get("orchestration_guidance")) or profile.orchestration_guidance,
        reflection_density=str(overrides.get("reflection_density") or profile.reflection_density),
        evidence_depth=str(overrides.get("evidence_depth") or profile.evidence_depth),
        repair_tendency=str(overrides.get("repair_tendency") or profile.repair_tendency),
        forbidden_constraints=profile.forbidden_constraints,
    )


def resolve_task_board_planning_policy(
    effort: Any = "medium",
    *,
    task_complexity_basis: Sequence[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> TaskBoardPlanningPolicy:
    profile = resolve_task_board_effort_profile(effort)
    return TaskBoardPlanningPolicy(
        effort_profile=profile,
        action_block_meaning=(
            "An action block is a TaskBoard card-level unit of intended work. "
            "It may involve reading evidence, comparing facts, drafting, reviewing, repairing, "
            "deciding, waiting for input, finalizing, or other work that fits the task. "
            "These examples are vocabulary guidance only: they are not required actions, "
            "not an allowlist, not a target count, and not a scoring rule."
        ),
        task_complexity_basis=tuple(task_complexity_basis or _default_task_complexity_basis()),
        owner_boundaries=(
            "Plan only the submitted board shape; do not discuss route selection.",
            "Use the provided task facts, success criteria, evidence, and capability summary.",
            "TaskBoard policy may shape orchestration complexity, reflection density, evidence depth, and repair tendency.",
            "TaskBoard policy must not grant permissions, hide capabilities, or define hard budgets.",
        ),
        control_card_guidance=(
            "Use allowed_execution_shape='control' for synthesis, verification, finalization, or board-continuation decisions that do not need tools.",
            "Use allowed_execution_shape='readback' when the card's only work is bounded cold artifact readback from existing dependency refs.",
            "A control card returns the deliverable or card-local synthesis plus sufficient, gaps, "
            "next_board_action, diagnostics, and optional patch_proposal in one structured payload. "
            "patch_proposal may be a TaskBoardPatch for board-state changes or a TaskWorkspace text patch "
            "for precise artifact repair.",
            "After evidence fan-in, prefer one terminal control card that combines synthesis, verification, and next-step decision instead of a serial chain of synthesis -> risk -> finalization -> review control cards.",
            "Create multiple dependent control cards only when each one produces a distinct user-visible artifact, waits for different upstream evidence, or represents a materially separate decision that cannot be verified in the same request.",
            "Do not use a downstream control card only to review, summarize, or repackage the immediately previous control card; put that review or finalization into the earlier control card fields.",
        ),
        evidence_reuse_guidance=(
            "Use existing TaskBoard card results, artifact refs, file refs, and scoped readback before planning or executing another evidence-gathering action.",
            "When dependency evidence already contains the needed facts or cold refs, prefer synthesis, comparison, or local repair over re-gathering the same external evidence.",
            "Re-gather evidence only when the current card objective requires fresh evidence, the existing evidence is missing, stale, contradictory, or readback diagnostics show it cannot be used.",
            "Make card completion conditions outcome-based. Do not make a provider, endpoint, file format, or auxiliary guidance source a hard dependency unless the user goal explicitly requires that exact source or artifact.",
            "Mark auxiliary evidence, style guidance, optional cross-checks, or replaceable source attempts as optional or degradable so downstream synthesis can continue with diagnostics when core evidence is sufficient.",
        ),
        repair_orchestration_guidance=(
            "When review localizes a defect, prefer the smallest repair that can fix the affected card output or artifact while preserving valid evidence.",
            "Do not turn a localized defect into broad repair work that repeats completed evidence-gathering cards.",
            "Use board results, diagnostics, and refs to carry localized repair context forward.",
            "When a card fails but enough alternative evidence exists, prefer a bounded partial result with explicit diagnostics over blocking the whole board.",
        ),
        metadata=dict(metadata or {}),
    )


def task_board_planning_output_schema() -> dict[str, Any]:
    return {
        "board_goal": (str, "Goal represented by the TaskBoard.", True),
        "cards": [
            {
                "id": (
                    str,
                    "Optional short readable card id hint.",
                    False,
                ),
                "action_block": (str, "Card-level work block in the model's own words.", True),
                "objective": (str, "Objective for this card.", True),
                "depends_on": (
                    [str],
                    "Upstream card ids or id hints this card depends on.",
                    True,
                ),
                "evidence_to_use": ([str], "Evidence sources or upstream refs this card expects to use.", False),
                "done_when": (str, "Completion condition for this card.", True),
                "allowed_execution_shape": (
                    str,
                    "Optional handler-defined execution shape. Use control for synthesis/finalization/verification "
                    "cards that should run as one structured model request; use readback for scoped cold artifact "
                    "readback; keep auto for cards that use scoped_retrieval without Actions; use actions only for "
                    "mounted Actions, TaskWorkspace side effects, or mixed Action/readback cards. "
                    "Avoid serial control-only chains when one control card can synthesize, verify, and decide continuation.",
                    False,
                ),
                "failure_policy": (
                    str,
                    "required, optional, or degradable. Required failure blocks dependents. Optional/degradable failure may unblock dependents with diagnostics when enough core evidence exists.",
                    False,
                ),
                "scoped_retrieval": (
                    dict,
                    "Optional bounded TaskContext retrieval plan: {query_groups: [{query, expected_role, source_kinds?: [exact values offered by input.retrieval_policy.source_kinds], path?, pattern?, filters?, max_results?, snippet_limit?}]}. query is content text or an exact phrase to search, not a list/read/search Action command. Do not invent or hardcode source kinds. For record_store records, put collection names in filters.collection, do not put collection names in path, and use filters.kind only when the exact record kind is provided; never infer a generic kind such as note. For task_workspace files, path is the directory/file scope and pattern is one file glob such as *.md, * or **. Other source kinds define their descriptor/ref scope through the offered policy. Use filters.content_contains only for explicit content keyword lists. ContextReader returns factual locator_ref/evidence_snippet records only.",
                    False,
                ),
                "preflight_kind": (
                    str,
                    "Optional readiness check kind such as resource_health, readback, or capability_check. Preflight cards may only use mounted capabilities or existing TaskWorkspace refs.",
                    False,
                ),
                "requires_capability_ids": (
                    [str],
                    "Optional mounted capability ids required before this card can run. This is a structural requirement, not a permission grant.",
                    False,
                ),
                "action_commands": (
                    [
                        {
                            "purpose": (str, "Bounded purpose of this exact Action call.", False),
                            "action_id": (
                                str,
                                "Exact id from the offered planner_capabilities Action set.",
                                False,
                            ),
                            "action_input": (
                                dict,
                                "Complete kwargs object matching that Action's offered input contract.",
                                False,
                            ),
                        }
                    ],
                    "Complete exhaustive Action call batch for this card when every argument is already known at board-planning time. The card completes after this exact batch. Omit when an argument depends on a future card result, and put later synthesis or final delivery on a dependent control card.",
                    False,
                ),
                "requires_task_workspace_refs": (
                    [str],
                    "Optional TaskWorkspace/readback refs that must already exist for this preflight card.",
                    False,
                ),
                "final_task_workspace_deliverables": (
                    [str],
                    "TaskWorkspace-relative final file paths required by the submitted task and owned by this card. Preserve an exact requested path; when a file is requested without a path, choose a suitable relative path. Omit for intermediate artifacts and for tasks requesting only a returned answer body.",
                    False,
                ),
                "focus_item_ids": (
                    [str],
                    "Optional acceptance-index item ids this card is meant to advance. These ids are orientation hints only.",
                    False,
                ),
            }
        ],
        "reflection_points": ([str], "Review, correction, or decision points judged necessary by the model.", False),
        "completion_gate": (str, "How TaskBoard knows the submitted task is complete.", True),
        "why_this_effort_shape": (str, "Why this board shape matches the effort complexity definition.", True),
        "risk_notes": ([str], "Risks or limitations in this plan.", False),
    }


def coerce_task_board_planning_result(
    value: Mapping[str, Any],
    *,
    board_id: str,
    graph_id: str | None = None,
    effort: Any = "medium",
    planning_policy: TaskBoardPlanningPolicy | None = None,
    metadata: Mapping[str, Any] | None = None,
    validator: TaskBoardValidator | None = None,
) -> TaskBoardPlanningResult:
    if not isinstance(value, Mapping):
        raise TypeError(f"TaskBoard planning result must be a mapping, got: { type(value) }.")
    _reject_forbidden_planning_keys(value)
    policy = planning_policy or resolve_task_board_planning_policy(effort)
    raw_cards = value.get("cards")
    if not isinstance(raw_cards, Sequence) or isinstance(raw_cards, str | bytes | bytearray):
        raise TypeError("TaskBoard planning result requires 'cards' as a sequence.")
    cards, card_diagnostics = _cards_from_planning_items(raw_cards)
    board_metadata = {
        "board_goal": value.get("board_goal"),
        "reflection_points": _str_list(value.get("reflection_points")),
        "completion_gate": value.get("completion_gate"),
        "why_this_effort_shape": value.get("why_this_effort_shape"),
        "risk_notes": _str_list(value.get("risk_notes")),
        "planning_policy": policy.to_prompt_payload(),
        **dict(metadata or {}),
    }
    revision = TaskBoardRevision.create(
        board_id=board_id,
        graph=TaskBoardGraph(
            graph_id=graph_id or f"{ board_id }.graph",
            cards=cards,
            metadata={"planning_policy": policy.to_prompt_payload()},
        ),
        metadata=board_metadata,
    )
    effective_validator = validator or TaskBoardValidator()
    effective_validator.validate(revision)
    return TaskBoardPlanningResult(
        revision=revision,
        planning_policy=policy,
        raw_result=dict(value),
        diagnostics=(*_mapping_tuple(value.get("diagnostics")), *card_diagnostics),
    )


def _profile_presets() -> dict[str, TaskBoardEffortProfile]:
    forbidden = (
        "Do not use hard budgets as effort semantics.",
        "Do not use fixed card counts, node counts, model-call counts, or tool-call counts.",
        "Do not use mandatory action option sets or action allowlists as effort semantics.",
        "Do not grant permissions, hide configured capabilities, or change data visibility through effort.",
    )
    return {
        "low": TaskBoardEffortProfile(
            name="low",
            complexity_definition=(
                "Minimum viable orchestration: choose the simplest board shape that can complete the "
                "submitted task honestly. Necessary evidence, repair, and completion checks remain allowed."
            ),
            orchestration_guidance=(
                "Prefer compact card structure when the task risk allows it.",
                "Keep reflection focused on necessary completion and evidence checks.",
                "Escalate decomposition only when the task cannot be completed honestly without it.",
            ),
            reflection_density="minimum necessary",
            evidence_depth="enough to support the final claim without optional exploration",
            repair_tendency="local repair when a card fails or evidence contradicts the result",
            forbidden_constraints=forbidden,
        ),
        "medium": TaskBoardEffortProfile(
            name="medium",
            complexity_definition=(
                "Balanced orchestration: use enough decomposition and review density to reduce likely "
                "mistakes while avoiding exhaustive process."
            ),
            orchestration_guidance=(
                "Separate evidence gathering, synthesis, and final decision when that improves reliability.",
                "Use review points where dependency or evidence mistakes are likely.",
                "Keep repair opportunities available for failed or contradicted cards.",
            ),
            reflection_density="balanced",
            evidence_depth="cover primary sources and material dependencies",
            repair_tendency="repair or revise when evidence, dependency, or readback quality is insufficient",
            forbidden_constraints=forbidden,
        ),
        "high": TaskBoardEffortProfile(
            name="high",
            complexity_definition=(
                "Thorough orchestration: use deeper decomposition, evidence exploration, and reflection "
                "where risk, side effects, or artifact quality justify it, while avoiding symbolic process."
            ),
            orchestration_guidance=(
                "Make important evidence chains explicit.",
                "Add review or consistency cards where final quality depends on multiple sources or artifacts.",
                "Use repair and replan opportunities when direction, evidence, or produced artifacts are wrong.",
            ),
            reflection_density="thorough when it materially reduces risk",
            evidence_depth="deeper source, artifact, and readback evidence for risky or multi-capability work",
            repair_tendency="prefer explicit repair or board patch when progress is blocked, wrong, or under-evidenced",
            forbidden_constraints=forbidden,
        ),
    }


def _default_task_complexity_basis() -> tuple[str, ...]:
    return (
        "number and diversity of required capabilities",
        "dependency depth and fanout/fanin shape",
        "external evidence, file, or artifact requirements",
        "side-effect or approval risk",
        "need for readback, repair, replan, or final quality judgment",
    )


def _normalize_effort(effort: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(effort, Mapping):
        source = dict(effort)
        name = source.pop("name", source.pop("preset", source.pop("level", "medium")))
        return _effort_alias(str(name or "medium")), source
    return _effort_alias(str(effort or "medium")), {}


def _effort_alias(name: str) -> str:
    normalized = name.strip().lower() or "medium"
    if normalized in {"minimal", "fast"}:
        return "low"
    if normalized == "normal":
        return "medium"
    if normalized == "max":
        return "high"
    return normalized


def _cards_from_planning_items(raw_cards: Sequence[Any]) -> tuple[tuple[TaskBoardCard, ...], tuple[Mapping[str, Any], ...]]:
    raw_items: list[Mapping[str, Any]] = []
    for item in raw_cards:
        if not isinstance(item, Mapping):
            raise TypeError(f"TaskBoard planning card must be a mapping, got: { type(item) }.")
        raw_items.append(item)

    used_ids: set[str] = set()
    raw_id_counts: dict[str, int] = {}
    prepared: list[tuple[Mapping[str, Any], str, str]] = []
    diagnostics: list[Mapping[str, Any]] = []
    for index, item in enumerate(raw_items):
        raw_id = str(item.get("id") or item.get("card_id") or "").strip()
        if raw_id:
            raw_id_counts[raw_id] = raw_id_counts.get(raw_id, 0) + 1
        canonical_id = _canonical_taskboard_card_id(item, index=index, used_ids=used_ids)
        used_ids.add(canonical_id)
        prepared.append((item, raw_id, canonical_id))
        if raw_id != canonical_id:
            diagnostics.append(
                {
                    "code": "taskboard.planning_card_id_canonicalized",
                    "card_index": index,
                    "id_hint": raw_id,
                    "canonical_id": canonical_id,
                }
            )

    unique_raw_id_map = {
        raw_id: canonical_id
        for _, raw_id, canonical_id in prepared
        if raw_id and raw_id_counts.get(raw_id) == 1
    }
    duplicate_raw_ids = sorted(raw_id for raw_id, count in raw_id_counts.items() if count > 1)
    if duplicate_raw_ids:
        diagnostics.append(
            {
                "code": "taskboard.planning_card_id_hint_ambiguous",
                "id_hints": duplicate_raw_ids,
                "message": "Duplicate raw card id hints were not used for dependency remapping.",
            }
        )

    canonical_ids = {canonical_id for _, _, canonical_id in prepared}
    slug_targets: dict[str, set[str]] = {}
    for _, raw_id, canonical_id in prepared:
        for alias in (raw_id, canonical_id):
            slug = _slugify_taskboard_card_id(alias)
            if not slug:
                continue
            slug_targets.setdefault(slug, set()).add(canonical_id)
    unique_slug_id_map = {
        slug: next(iter(targets))
        for slug, targets in slug_targets.items()
        if len(targets) == 1
    }

    cards = tuple(
        _card_from_planning_item(
            item,
            card_id=canonical_id,
            card_index=index,
            dependency_id_map=unique_raw_id_map,
            canonical_ids=canonical_ids,
            slug_id_map=unique_slug_id_map,
            ambiguous_dependency_ids=set(duplicate_raw_ids),
        )
        for index, (item, _, canonical_id) in enumerate(prepared)
    )
    return cards, tuple(diagnostics)


def _card_from_planning_item(
    value: Any,
    *,
    card_id: str,
    card_index: int,
    dependency_id_map: Mapping[str, str],
    canonical_ids: set[str],
    slug_id_map: Mapping[str, str],
    ambiguous_dependency_ids: set[str],
) -> TaskBoardCard:
    if not isinstance(value, Mapping):
        raise TypeError(f"TaskBoard planning card must be a mapping, got: { type(value) }.")
    objective = str(value.get("objective") or value.get("goal") or "").strip()
    if not objective:
        raise ValueError(f"TaskBoard planning card '{ card_id }' requires non-empty objective.")
    evidence_to_use = _str_list(value.get("evidence_to_use"))
    action_block = str(value.get("action_block") or "").strip()
    done_when = str(value.get("done_when") or "").strip()
    failure_policy = _failure_policy(value.get("failure_policy"))
    scoped_retrieval = value.get("scoped_retrieval")
    preflight_kind = str(value.get("preflight_kind") or "").strip()
    requires_capability_ids = _str_list(value.get("requires_capability_ids"))
    action_commands = _planning_action_commands(value.get("action_commands"), card_id=card_id)
    declared_execution_shape = str(
        value.get("allowed_execution_shape") or "auto"
    ).strip()
    resolved_execution_shape = declared_execution_shape
    requires_task_workspace_refs = _str_list(value.get("requires_task_workspace_refs"))
    final_task_workspace_deliverables = _str_list(value.get("final_task_workspace_deliverables"))
    focus_item_ids = _str_list(value.get("focus_item_ids"))
    metadata: dict[str, Any] = {
        "action_block": action_block,
        "done_when": done_when,
        "failure_policy": failure_policy,
        "planning_card_index": card_index,
    }
    raw_id = str(value.get("id") or value.get("card_id") or "").strip()
    if raw_id and raw_id != card_id:
        metadata["planning_id_hint"] = raw_id
    evidence_contract: dict[str, Any] = {
        "action_block": action_block,
        "evidence_to_use": evidence_to_use,
        "done_when": done_when,
        "failure_policy": failure_policy,
    }
    if preflight_kind:
        metadata["preflight_kind"] = preflight_kind
        evidence_contract["preflight_kind"] = preflight_kind
    if requires_capability_ids:
        metadata["requires_capability_ids"] = requires_capability_ids
        evidence_contract["requires_capability_ids"] = requires_capability_ids
    if action_commands:
        metadata["action_commands"] = action_commands
        evidence_contract["action_commands"] = action_commands
        if declared_execution_shape.lower().replace("-", "_") != "actions":
            metadata["declared_execution_shape"] = declared_execution_shape
            metadata["execution_shape_normalization"] = (
                "exact_action_commands_override_generic_shape_hint"
            )
            resolved_execution_shape = "actions"
    if requires_task_workspace_refs:
        metadata["requires_task_workspace_refs"] = requires_task_workspace_refs
        evidence_contract["requires_task_workspace_refs"] = requires_task_workspace_refs
    if final_task_workspace_deliverables:
        metadata["final_task_workspace_deliverables"] = final_task_workspace_deliverables
        evidence_contract["final_task_workspace_deliverables"] = final_task_workspace_deliverables
    if focus_item_ids:
        metadata["focus_item_ids"] = focus_item_ids
        evidence_contract["focus_item_ids"] = focus_item_ids
    if isinstance(scoped_retrieval, Mapping):
        evidence_contract["scoped_retrieval"] = dict(scoped_retrieval)
        metadata["scoped_retrieval"] = dict(scoped_retrieval)
    return TaskBoardCard(
        id=card_id,
        objective=objective,
        depends_on=tuple(
            _canonical_taskboard_dependencies(
                value.get("depends_on"),
                dependency_id_map=dependency_id_map,
                canonical_ids=canonical_ids,
                slug_id_map=slug_id_map,
                ambiguous_dependency_ids=ambiguous_dependency_ids,
            )
        ),
        input_refs=tuple(evidence_to_use),
        required_outputs=(done_when,) if done_when else (),
        allowed_execution_shape=resolved_execution_shape,
        evidence_contract=evidence_contract,
        failure_policy=failure_policy,
        metadata=metadata,
    )


def _planning_action_commands(value: Any, *, card_id: str) -> list[dict[str, Any]]:
    if value in (None, [], ()):
        return []
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError(f"TaskBoard planning card '{card_id}' action_commands must be a sequence.")
    commands: list[dict[str, Any]] = []
    for index, raw_command in enumerate(value):
        if not isinstance(raw_command, Mapping):
            raise TypeError(
                f"TaskBoard planning card '{card_id}' action_commands[{index}] must be a mapping."
            )
        action_id = str(raw_command.get("action_id") or "").strip()
        action_input = raw_command.get("action_input")
        if not action_id:
            raise ValueError(
                f"TaskBoard planning card '{card_id}' action_commands[{index}] requires action_id."
            )
        if not isinstance(action_input, Mapping):
            raise TypeError(
                f"TaskBoard planning card '{card_id}' action_commands[{index}] action_input must be a mapping."
            )
        commands.append(
            {
                "purpose": str(raw_command.get("purpose") or f"Use {action_id}").strip(),
                "action_id": action_id,
                "action_input": dict(action_input),
            }
        )
    return commands


def _canonical_taskboard_card_id(
    value: Mapping[str, Any],
    *,
    index: int,
    used_ids: set[str],
) -> str:
    raw_id = str(value.get("id") or value.get("card_id") or "").strip()
    if raw_id:
        base = _slugify_taskboard_card_id(raw_id)
    else:
        basis = (
            str(value.get("objective") or "").strip()
            or str(value.get("action_block") or "").strip()
            or str(value.get("done_when") or "").strip()
            or "card"
        )
        slug = _slugify_taskboard_card_id(basis) or "card"
        base = f"card_{index + 1}_{slug}"
    base = (base or f"card_{index + 1}")[:72].strip("._-") or f"card_{index + 1}"
    candidate = base
    suffix = 2
    while candidate in used_ids:
        suffix_text = f"_{suffix}"
        candidate = f"{base[:72 - len(suffix_text)]}{suffix_text}".strip("._-") or f"card_{index + 1}_{suffix}"
        suffix += 1
    return candidate


def _canonical_taskboard_dependencies(
    value: Any,
    *,
    dependency_id_map: Mapping[str, str],
    canonical_ids: set[str],
    slug_id_map: Mapping[str, str],
    ambiguous_dependency_ids: set[str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for dependency in _str_list(value):
        if dependency in ambiguous_dependency_ids:
            raise ValueError(
                f"TaskBoard planning dependency '{ dependency }' is ambiguous because multiple cards used that id hint."
            )
        canonical = dependency_id_map.get(dependency)
        if canonical is None and dependency in canonical_ids:
            canonical = dependency
        if canonical is None:
            canonical = slug_id_map.get(_slugify_taskboard_card_id(dependency))
        if canonical is None:
            canonical = dependency
        if canonical and canonical not in seen:
            result.append(canonical)
            seen.add(canonical)
    return result


def _slugify_taskboard_card_id(value: Any) -> str:
    text = str(value or "").strip()
    chars: list[str] = []
    last_separator = False
    for char in text:
        if char.isascii() and char.isalnum():
            chars.append(char.lower())
            last_separator = False
        elif char in {"_", "-", "."}:
            if chars and not last_separator:
                chars.append(char)
                last_separator = True
        else:
            if chars and not last_separator:
                chars.append("_")
                last_separator = True
    return "".join(chars).strip("._-")


def _reject_forbidden_planning_keys(value: Any, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.strip().lower()
            if normalized in _FORBIDDEN_PLANNING_KEYS:
                location = f"{ path }.{ key_text }" if path else key_text
                raise ValueError(f"TaskBoard planning payload contains forbidden effort-control key: { location }.")
            next_path = f"{ path }.{ key_text }" if path else key_text
            _reject_forbidden_planning_keys(item, path=next_path)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(value):
            _reject_forbidden_planning_keys(item, path=f"{ path }[{ index }]")


def _str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(_str_list(value))


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if text and text not in seen:
                result.append(text)
                seen.add(text)
        return result
    text = str(value).strip()
    return [text] if text else []


def _failure_policy(value: Any) -> str:
    text = str(value or "required").strip().lower().replace("-", "_")
    aliases = {
        "must": "required",
        "mandatory": "required",
        "critical": "required",
        "nice_to_have": "optional",
        "best_effort": "optional",
        "non_blocking": "optional",
        "nonblocking": "optional",
        "soft": "degradable",
        "fallback": "degradable",
        "degrade": "degradable",
    }
    normalized = aliases.get(text, text)
    if normalized not in {"required", "optional", "degradable"}:
        return "required"
    return normalized


def _mapping_tuple(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (dict(value),)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(dict(item) if isinstance(item, Mapping) else {"value": item} for item in value)
    return ({"value": value},)
