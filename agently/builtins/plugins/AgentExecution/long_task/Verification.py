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

import hashlib
import json
import re
from pathlib import PurePosixPath

from .TaskShared import (
    acceptance_locator_view_from_ledger,
    AgentTaskMixinBase,
    Any,
    cast,
    collect_evidence_use,
    DataFormatter,
    evidence_ledger_view,
    Literal,
    Mapping,
    parse_output_contract_dict,
    relevant_state_digest,
    ReplanSignal,
    Sequence,
    task_workspace_artifacts_from_ledger,
    TaskContextView,
    TerminalIssue,
    validate_evidence_use,
    value_with_normalized_evidence_use,
    _compact_agent_task_error_message,
    _omit_agent_task_request_payloads_from_hot_path,
    _TASKBOARD_DEPENDENCY_READBACK_MAX_REFS,
    _TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
    _VERIFIER_LEDGER_BODY_CHARS,
    _VERIFIER_LEDGER_MAX_ITEMS,
    _VERIFIER_LEDGER_MAX_OVERFLOW_REFS,
    _VERIFIER_PROMPT_ITEM_CHARS,
    _VERIFIER_PROMPT_TARGET_CHARS,
    _VERIFIER_PROMPT_VALUE_CHARS,
    _WORKSPACE_ARTIFACT_PREVIEW_BYTES,
)


_VERIFIER_RESULT_EMBEDDED_EVIDENCE_KEYS = frozenset(
    {
        "evidence_ledger",
        "evidence_use",
        "taskboard_evidence_view",
        "taskboard_scoped_evidence_view",
    }
)


class AgentTaskVerificationMixin(AgentTaskMixinBase):
    def _iteration_prompt_summaries(self) -> list[dict[str, Any]]:
        """Bounded, low-noise iteration history for plan/verify prompts.

        Full iteration records (including execution_meta) stay in self.iterations
        and the TaskWorkspace; prompts receive only step intent, the verification
        outcome, and TaskWorkspace refs so context does not grow unboundedly with the
        full execution metadata of every prior step.
        """
        limit = self._iterations_prompt_limit()
        records = self.iterations[-limit:] if isinstance(limit, int) and limit > 0 else self.iterations
        summaries: list[dict[str, Any]] = []
        for record in records:
            plan = record.get("plan")
            if not isinstance(plan, dict):
                plan = {}
            verification = record.get("verification")
            if not isinstance(verification, dict):
                verification = {}
            execution_meta = record.get("execution_meta")
            evidence_anchors = (
                self._planner_evidence_anchors_from_execution_meta(execution_meta)
                if isinstance(execution_meta, Mapping)
                else {}
            )
            material_claim_audit = verification.get("material_claim_audit")
            material_claim_projection = {
                "valid": (
                    material_claim_audit.get("valid")
                    if isinstance(material_claim_audit, Mapping)
                    else None
                ),
                "repair_contract": DataFormatter.sanitize(
                    verification.get(
                        "material_claim_repair_contract",
                        material_claim_audit.get("repair_contract", {})
                        if isinstance(material_claim_audit, Mapping)
                        else {},
                    )
                ),
            }
            terminal_convergence = verification.get("terminal_convergence")
            verification_source = record.get("verification_source") or verification.get("verification_source")
            progress_projection = {
                key: DataFormatter.sanitize(verification[key])
                for key in (
                    "guard_reasons", "replan_signal", "missing_required_capabilities",
                    "missing_capability_evidence", "unenforced_evidence_requirements",
                )
                if verification_source and verification.get(key)
            }
            if verification_source:
                progress_projection["verification_source"] = verification_source
            if verification_source == "consumer_driven_continuation" and verification.get("replan_signals"):
                progress_projection["replan_signals"] = DataFormatter.sanitize(verification["replan_signals"])
            summaries.append(
                {
                    "iteration": record.get("iteration"),
                    "step_instruction": plan.get("step_instruction", ""),
                    "effective_execution_shape": plan.get("effective_execution_shape", plan.get("execution_shape", "")),
                    "process_summary": DataFormatter.sanitize(record.get("process_summary", {})),
                    "verification": {
                        **progress_projection,
                        "is_complete": verification.get("is_complete"),
                        "reason": verification.get("reason", ""),
                        "failure_analysis": verification.get("failure_analysis", ""),
                        "acceptance_delta": verification.get("acceptance_delta", []),
                        "missing_criteria": verification.get("missing_criteria", []),
                        "replan_instruction": verification.get("replan_instruction", ""),
                        "repair_constraints": verification.get("repair_constraints", []),
                        "next_step_requirements": verification.get("next_step_requirements", []),
                        "criterion_repair_contract": DataFormatter.sanitize(
                            verification.get("criterion_repair_contract", {})
                        ),
                        "material_claim_audit": material_claim_projection,
                        "terminal_convergence": (
                            DataFormatter.sanitize(terminal_convergence)
                            if isinstance(terminal_convergence, Mapping)
                            else {}
                        ),
                    },
                    "observation_ref": record.get("observation_ref"),
                    "verification_ref": record.get("verification_ref"),
                    "reflection_refs": DataFormatter.sanitize(record.get("reflection_refs", [])),
                    "evidence_anchors": evidence_anchors,
                }
            )
        # Prior-run summaries (from a resumed snapshot) come first so the model
        # sees the full task history; the recent-window limit applies to the
        # combined sequence.
        combined = [*self._resumed_iteration_summaries, *summaries]
        if isinstance(limit, int) and limit > 0 and len(combined) > limit:
            combined = combined[-limit:]
        return combined

    def _reflection_prompt_summaries(self) -> list[dict[str, Any]]:
        limit = self._iterations_prompt_limit()
        records = self.reflections[-limit:] if isinstance(limit, int) and limit > 0 else self.reflections
        summaries: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            summaries.append(
                {
                    "iteration": record.get("iteration"),
                    "phase": record.get("phase"),
                    "record_ref": record.get("record_ref"),
                    "summary": DataFormatter.sanitize(record.get("summary", {})),
                    "completion_evidence": False,
                }
            )
        return summaries

    def _strict_terminal_relevant_state(
        self,
        *,
        candidate: Mapping[str, Any],
        execution_evidence_summary: Mapping[str, Any],
        repair_contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw_carriers = candidate.get("carriers")
        carriers = (
            [item for item in raw_carriers if isinstance(item, Mapping)]
            if isinstance(raw_carriers, Sequence)
            and not isinstance(raw_carriers, str | bytes | bytearray)
            else [candidate]
        )
        source_targets: list[dict[str, Any]] = []
        for reference_id in self._task_reference_catalog.offered_references():
            resolved = self._task_reference_catalog.resolve(reference_id)
            target = resolved.get("target")
            if not isinstance(target, Mapping):
                continue
            body = ""
            for field in ("body", "content", "text", "snippet", "preview", "result", "output", "value"):
                value = target.get(field)
                if isinstance(value, str) and value:
                    body = value
                    break
            source_targets.append(
                {
                    "reference_id": reference_id,
                    "content_version_id": str(target.get("content_version_id") or ""),
                    "action_call_id": str(target.get("action_call_id") or ""),
                    "status": str(target.get("status") or resolved.get("status") or ""),
                    "body_state": str(target.get("body_state") or resolved.get("body_state") or ""),
                    "content_digest": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "",
                }
            )
        capability_facts = {
            "required_actions": self._normalize_string_list(execution_evidence_summary.get("required_actions")),
            "required_skills": self._normalize_string_list(execution_evidence_summary.get("required_skills")),
            "current_action_ids": self._normalize_string_list(execution_evidence_summary.get("action_ids")),
            "current_consumed_skill_ids": self._normalize_string_list(
                execution_evidence_summary.get("consumed_skill_ids")
            ),
            "current_capabilities_used": self._normalize_string_list(
                execution_evidence_summary.get("capabilities_used")
            ),
            "current_capability_evidence": execution_evidence_summary.get("capability_evidence", {}),
            "current_actions": execution_evidence_summary.get("actions", []),
            "current_replan_signals": execution_evidence_summary.get(
                "current_replan_signals",
                execution_evidence_summary.get("replan_signals", []),
            ),
            "current_failed_actions": self._normalize_string_list(
                execution_evidence_summary.get("failed_actions")
            ),
            "current_blocked_actions": self._normalize_string_list(
                execution_evidence_summary.get("blocked_actions")
            ),
            "current_approval_required_actions": self._normalize_string_list(
                execution_evidence_summary.get("approval_required_actions")
            ),
            "satisfied_required_actions": sorted(self._satisfied_required_actions),
            "satisfied_required_skills": sorted(self._satisfied_required_skills),
            "satisfied_capabilities": sorted(self._satisfied_capabilities),
            "satisfied_succeeded_actions": sorted(self._satisfied_succeeded_actions),
            "requirements": self._capability_evidence_requirements(execution_evidence_summary),
        }
        return {
            "candidate_content_version_ids": [
                str(item.get("content_version_id") or "")
                for item in carriers
                if str(item.get("content_version_id") or "")
            ],
            "source_reference_targets": source_targets,
            "capability_facts": capability_facts,
            "criterion_subjects": [
                f"criterion:{index}" for index, _criterion in enumerate(self.success_criteria, start=1)
            ],
            "output_subjects": ["output:final_result"],
            "repair_contract": dict(repair_contract),
        }

    def _terminal_convergence_preflight(
        self,
        *,
        candidate: Mapping[str, Any],
        execution_evidence_summary: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        for record in self._terminal_convergence_state.active_records():
            repair_contract = record.get("repair_contract")
            issue_value = record.get("issue")
            if not isinstance(repair_contract, Mapping) or not isinstance(issue_value, Mapping):
                continue
            if (
                str(issue_value.get("gate_kind") or "") == "output_contract"
                and str(issue_value.get("issue_code") or "")
                == "terminal_verifier_output_invalid"
            ):
                # The candidate state is intentionally unchanged for a verifier
                # response-contract retry. Only another response can repair this
                # host/model join, so do not suppress that request as redundant.
                continue
            state_digest = relevant_state_digest(
                self._strict_terminal_relevant_state(
                    candidate=candidate,
                    execution_evidence_summary=execution_evidence_summary,
                    repair_contract=repair_contract,
                )
            )
            if state_digest != str(record.get("last_state_digest") or ""):
                continue
            issue = TerminalIssue(
                str(issue_value.get("gate_kind") or ""),
                str(issue_value.get("issue_code") or ""),
                str(issue_value.get("contract_subject") or ""),
            )
            decision = self._terminal_convergence_state.record_detection(
                issue,
                state_digest,
                repair_contract=repair_contract,
                verifier_called=False,
            )
            return self._terminal_convergence_verification(
                issue=issue,
                repair_contract=repair_contract,
                decision=decision,
                verifier_called=False,
                candidate_final_result=str(candidate.get("text") or ""),
            )
        return None

    async def _terminal_capability_evidence_preflight(
        self,
        *,
        candidate: Mapping[str, Any],
        execution_evidence_summary: Mapping[str, Any],
        candidate_final_result: str,
    ) -> dict[str, Any] | None:
        """Fail or repair deterministic capability gaps before semantic review.

        A semantic verifier cannot create Action execution evidence. Running it
        while an authored ``action_succeeded`` requirement is missing both
        wastes a request and lets an output-contract problem mask the actionable
        capability gap. The host therefore evaluates the structured evidence
        contract first and sends any missing capability directly to the normal
        terminal convergence/repair owner.
        """

        self._accumulate_capability_evidence(execution_evidence_summary)
        missing, unenforced = self._evaluate_capability_evidence(
            execution_evidence_summary
        )
        if unenforced:
            self.diagnostics.setdefault(
                "unenforced_evidence_requirements",
                [],
            ).extend(unenforced)
        if not missing:
            return None
        missing_message = (
            "Missing required capability evidence: " + ", ".join(missing)
        )
        preflight = {
            "is_complete": False,
            "requires_block": False,
            "reason": missing_message,
            "failure_analysis": missing_message,
            "acceptance_delta": [missing_message],
            "missing_criteria": [missing_message],
            "replan_instruction": (
                "Produce the authored structured capability evidence before semantic verification."
            ),
            "repair_constraints": [missing_message],
            "next_step_requirements": [missing_message],
            "final_result_required": bool(candidate_final_result.strip()),
            "final_result": candidate_final_result,
            "missing_required_capabilities": list(missing),
            "missing_capability_evidence": list(missing),
            "unenforced_evidence_requirements": DataFormatter.sanitize(
                unenforced
            ),
            "guard_reasons": ["capability_evidence_missing"],
        }
        return await self._apply_strict_terminal_gates(
            preflight,
            candidate=candidate,
            execution_evidence_summary=execution_evidence_summary,
            verifier_called=False,
        )

    def _active_terminal_verification_protocol_repair(
        self,
        *,
        current_offered_reference_ids: set[str],
        current_offered_claim_keys: Sequence[str],
    ) -> dict[str, Any]:
        """Project the latest verifier response-contract failure into its retry.

        The host forwards the structured repair contract verbatim; it does not
        interpret verifier prose or alter business evidence. Current offered
        identity sets are supplied separately so stale response-local ids can
        never become authoritative on the retry.
        """

        records = list(self._terminal_convergence_state.active_records())
        for record in reversed(records):
            issue = record.get("issue")
            repair_contract = record.get("repair_contract")
            if not isinstance(issue, Mapping) or not isinstance(
                repair_contract,
                Mapping,
            ):
                continue
            if (
                str(issue.get("gate_kind") or "") != "output_contract"
                or str(issue.get("issue_code") or "")
                != "terminal_verifier_output_invalid"
            ):
                continue
            try:
                occurrence = int(record.get("occurrence") or 0)
            except (TypeError, ValueError):
                occurrence = 0
            return DataFormatter.sanitize(
                {
                    "occurrence": occurrence,
                    "repair_contract": dict(repair_contract),
                    "current_offered_reference_ids": sorted(
                        current_offered_reference_ids
                    ),
                    "current_offered_claim_keys": [
                        str(value)
                        for value in current_offered_claim_keys
                        if str(value).strip()
                    ],
                }
            )
        return {}

    @classmethod
    def _planner_repair_context(cls, previous_iterations: list[dict[str, Any]]) -> dict[str, Any]:
        if not previous_iterations:
            return {}
        latest = previous_iterations[-1]
        if not isinstance(latest, Mapping):
            return {}
        verification = latest.get("verification")
        if not isinstance(verification, Mapping):
            return {}
        if verification.get("is_complete") is True:
            return {}

        def normalize_list(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, tuple):
                return [str(item) for item in value if str(item).strip()]
            if isinstance(value, str) and value.strip():
                return [value.strip()]
            return []

        missing_criteria = normalize_list(verification.get("missing_criteria"))
        acceptance_delta = normalize_list(verification.get("acceptance_delta"))
        repair_constraints = normalize_list(verification.get("repair_constraints"))
        next_step_requirements = normalize_list(verification.get("next_step_requirements"))
        replan_instruction = str(verification.get("replan_instruction") or "").strip()
        failure_analysis = str(verification.get("failure_analysis") or "").strip()
        material_claim_repair_contract = verification.get("material_claim_repair_contract")
        if "material_claim_repair_contract" not in verification:
            # Summary-only snapshots retain material repair under the audit view.
            # Explicit empty/invalid top-level values must shadow stale nested data.
            material_claim_audit = verification.get("material_claim_audit")
            material_claim_repair_contract = (
                material_claim_audit.get("repair_contract")
                if isinstance(material_claim_audit, Mapping)
                else None
            )
        if not isinstance(material_claim_repair_contract, Mapping):
            material_claim_repair_contract = {}
        else:
            material_claim_repair_contract = dict(
                DataFormatter.sanitize(material_claim_repair_contract)
            )
        criterion_repair_contract = verification.get("criterion_repair_contract")
        if not isinstance(criterion_repair_contract, Mapping):
            criterion_repair_contract = {}
        else:
            criterion_repair_contract = dict(DataFormatter.sanitize(criterion_repair_contract))
        terminal_convergence = verification.get("terminal_convergence")
        progress_projection = {
            key: DataFormatter.sanitize(verification[key])
            for key in (
                "verification_source", "guard_reasons", "replan_signal", "missing_required_capabilities",
                "missing_capability_evidence", "unenforced_evidence_requirements",
            )
            if verification.get("verification_source") and verification.get(key)
        }
        if verification.get("verification_source") == "consumer_driven_continuation" and verification.get("replan_signals"):
            progress_projection["replan_signals"] = DataFormatter.sanitize(verification["replan_signals"])
        if not any(
            [
                missing_criteria,
                acceptance_delta,
                repair_constraints,
                next_step_requirements,
                replan_instruction,
                failure_analysis,
                material_claim_repair_contract,
                criterion_repair_contract,
                verification.get("verification_source") == "consumer_driven_continuation",
            ]
        ):
            return {}
        repair_context = {
            **progress_projection,
            "source_iteration": latest.get("iteration"),
            "verification_ref": latest.get("verification_ref"),
            "reason": str(verification.get("reason") or ""),
            "failure_analysis": failure_analysis,
            "acceptance_delta": acceptance_delta,
            "missing_criteria": missing_criteria,
            "advisory_repair_constraints": repair_constraints,
            "advisory_next_step_requirements": next_step_requirements,
            "replan_instruction": replan_instruction,
        }
        if material_claim_repair_contract:
            repair_context["material_claim_repair_contract"] = material_claim_repair_contract
        if criterion_repair_contract:
            repair_context["criterion_repair_contract"] = criterion_repair_contract
        if isinstance(terminal_convergence, Mapping):
            repair_context["terminal_convergence"] = DataFormatter.sanitize(terminal_convergence)
        process_summary = latest.get("process_summary")
        if isinstance(process_summary, Mapping):
            compact_process = DataFormatter.sanitize(dict(process_summary))
            if compact_process:
                repair_context["process_summary"] = compact_process
        cumulative_anchors = cls._cumulative_planner_evidence_anchors(previous_iterations)
        if cumulative_anchors:
            repair_context["available_evidence_anchors"] = cumulative_anchors
        return repair_context

    def _active_repair_context(self) -> dict[str, Any]:
        """Latest verifier feedback for the next consumer work unit.

        Planner prompts already receive this shape. Execution and artifact
        carrier prompts need the same compact contract so a repair pass does not
        depend on the planner restating every verifier finding in prose.
        """

        return self._planner_repair_context(self._iteration_prompt_summaries())

    @classmethod
    def _planner_evidence_anchors_from_execution_meta(cls, execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        """Compact exact refs and previews that later planning can safely reuse.

        The planner should not receive full execution metadata on every turn, but
        repair steps need stable URLs, paths, and bounded action previews so they
        do not reconstruct source refs from prose verification feedback.
        """

        summary = cls._execution_log_summary(dict(execution_meta))
        return cls._planner_evidence_anchors_from_summary(summary)

    @classmethod
    def _planner_evidence_anchors_from_summary(cls, summary: Mapping[str, Any]) -> dict[str, Any]:
        source_refs = cls._compact_planner_source_refs(summary.get("source_refs", []), max_refs=24)
        action_previews = cls._compact_planner_action_previews(summary.get("actions", []), max_actions=8)
        artifact_refs = summary.get("artifact_refs")
        compact_artifact_refs: list[Any] = []
        if isinstance(artifact_refs, list):
            compact_artifact_refs = [cls._compact_artifact_ref_for_verifier(ref) for ref in artifact_refs[:8]]
        anchors: dict[str, Any] = {}
        if source_refs:
            anchors["source_refs"] = source_refs
        if action_previews:
            anchors["action_result_previews"] = action_previews
        if compact_artifact_refs:
            anchors["artifact_refs"] = compact_artifact_refs
        if summary.get("action_ids"):
            anchors["action_ids"] = DataFormatter.sanitize(summary.get("action_ids", []))
        status = str(summary.get("status") or "").strip()
        if status:
            anchors["status"] = status
        return anchors

    @classmethod
    def _cumulative_planner_evidence_anchors(cls, previous_iterations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        source_refs: list[Any] = []
        action_previews: list[Any] = []
        artifact_refs: list[Any] = []
        action_ids: list[str] = []
        for iteration in previous_iterations:
            anchors = iteration.get("evidence_anchors")
            if not isinstance(anchors, Mapping):
                continue
            raw_source_refs = anchors.get("source_refs")
            if isinstance(raw_source_refs, list):
                source_refs.extend(raw_source_refs)
            raw_action_previews = anchors.get("action_result_previews")
            if isinstance(raw_action_previews, list):
                action_previews.extend(raw_action_previews)
            raw_artifact_refs = anchors.get("artifact_refs")
            if isinstance(raw_artifact_refs, list):
                artifact_refs.extend(raw_artifact_refs)
            for action_id in cls._normalize_string_list(anchors.get("action_ids")):
                if action_id not in action_ids:
                    action_ids.append(action_id)

        cumulative: dict[str, Any] = {}
        compact_source_refs = cls._compact_planner_source_refs(source_refs, max_refs=32)
        if compact_source_refs:
            cumulative["source_refs"] = compact_source_refs
        compact_action_previews = cls._dedupe_planner_action_previews(action_previews)[:10]
        if compact_action_previews:
            cumulative["action_result_previews"] = compact_action_previews
        compact_artifact_refs = cls._dedupe_ref_records(artifact_refs)[:12]
        if compact_artifact_refs:
            cumulative["artifact_refs"] = compact_artifact_refs
        if action_ids:
            cumulative["action_ids"] = action_ids
        return DataFormatter.sanitize(cumulative)

    @classmethod
    def _compact_planner_source_refs(cls, refs: Any, *, max_refs: int) -> list[dict[str, Any]]:
        if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
            return []
        allowed_fields = {"source_url", "selected_url", "requested_url", "url", "href", "path"}
        compact: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            field = str(ref.get("field") or "").strip()
            value = str(ref.get("value") or "").strip()
            if not field and not value:
                path_value = str(ref.get("path") or "").strip()
                if path_value:
                    field = "path"
                    value = path_value
            action_call_id = str(ref.get("action_call_id") or "").strip()
            if field not in allowed_fields or not value:
                continue
            key = (field, value, action_call_id)
            if key in seen:
                continue
            seen.add(key)
            compact_ref = {
                "field": field,
                "value": value,
                "action_id": str(ref.get("action_id") or ""),
                "action_call_id": action_call_id,
                "path": str(ref.get("path") or ""),
            }
            content_state = str(ref.get("content_state") or "").strip()
            if content_state:
                compact_ref["content_state"] = content_state
            evidence_boundary = str(ref.get("evidence_boundary") or "").strip()
            if evidence_boundary:
                compact_ref["evidence_boundary"] = evidence_boundary
            compact.append(compact_ref)
            if len(compact) >= max_refs:
                break
        return compact

    @classmethod
    def _compact_planner_action_previews(cls, actions: Any, *, max_actions: int) -> list[dict[str, Any]]:
        if not isinstance(actions, Sequence) or isinstance(actions, str | bytes | bytearray):
            return []
        compact: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, Mapping):
                continue
            preview = action.get("result_preview")
            refs = cls._compact_planner_source_refs(cls._collect_source_refs_from_action_records([action]), max_refs=12)
            if preview is None and not refs:
                continue
            compact_action: dict[str, Any] = {
                "id": str(action.get("id") or action.get("name") or ""),
                "status": str(action.get("status") or ""),
            }
            action_call_id = str(action.get("action_call_id") or "").strip()
            if action_call_id:
                compact_action["action_call_id"] = action_call_id
            if action.get("input_preview"):
                compact_action["input_preview"] = cls._compact_verifier_prompt_value(
                    action.get("input_preview"),
                    max_chars=500,
                )
            if preview is not None:
                compact_action["result_preview"] = cls._compact_action_preview_value(preview, max_chars=1800)
            if refs:
                compact_action["source_refs"] = refs
            compact.append(compact_action)
            if len(compact) >= max_actions:
                break
        return cls._dedupe_planner_action_previews(compact)

    @staticmethod
    def _dedupe_planner_action_previews(actions: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for action in actions:
            if isinstance(action, Mapping):
                key = "|".join(
                    [
                        str(action.get("id") or ""),
                        str(action.get("action_call_id") or ""),
                        str(action.get("status") or ""),
                    ]
                )
            else:
                key = str(action)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(action)
        return deduped

    def _iterations_prompt_limit(self) -> int | None:
        configured = self._agent_task_option("iterations_prompt_limit", None)
        if configured is None:
            return None
        try:
            limit = int(configured)
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    @staticmethod
    def _shape_for_route(route: str) -> str:
        return {
            "model_request": "direct",
            "dynamic_task": "dynamic_task",
            "agent_task": "dynamic_task",
        }.get(str(route or "").strip(), "direct")

    def _reconcile_effective_shape(self, plan: dict[str, Any], execution_meta: dict[str, Any]) -> None:
        """Write the route actually taken back into the plan's effective shape.

        Keeps effective_execution_shape consistent with the executed route so
        diagnostics never report a shape the run did not actually take.
        """
        route_info = execution_meta.get("route")
        if not isinstance(route_info, dict):
            route_info = {}
        actual_route = str(route_info.get("selected_route") or "")
        if not actual_route:
            return
        requested_shape = str(plan.get("effective_execution_shape") or plan.get("execution_shape") or "direct")
        # "actions" and "direct" both run the model_request route; treat them as
        # consistent so a normal actions step is not flagged as a mismatch.
        consistent = {"direct", "actions"} if actual_route == "model_request" else {self._shape_for_route(actual_route)}
        if requested_shape in consistent:
            return
        plan["effective_execution_shape"] = self._shape_for_route(actual_route)
        plan["route_shape_reconciled"] = {
            "requested_shape": requested_shape,
            "actual_route": actual_route,
            "effective_shape": plan["effective_execution_shape"],
        }
        self.diagnostics.setdefault("route_shape_reconciliations", []).append(plan["route_shape_reconciled"])

    def _terminal_issue_from_verification(
        self,
        verification: Mapping[str, Any],
        *,
        execution_evidence_summary: Mapping[str, Any],
    ) -> tuple[TerminalIssue, dict[str, Any], bool] | None:
        material_claim_audit = verification.get("material_claim_audit")
        material_repair_contract = verification.get(
            "material_claim_repair_contract"
        )
        criterion_audit = verification.get("criterion_audit")
        criterion_repair_contract = verification.get(
            "criterion_repair_contract"
        )
        protocol_contracts: list[tuple[str, Mapping[str, Any]]] = []
        for section, audit, repair_contract in (
            (
                "criterion_checks",
                criterion_audit,
                criterion_repair_contract,
            ),
            (
                "material_claim_checks",
                material_claim_audit,
                material_repair_contract,
            ),
        ):
            if (
                isinstance(audit, Mapping)
                and audit.get("valid") is False
                and isinstance(repair_contract, Mapping)
                and str(repair_contract.get("gate_kind") or "")
                == "output_contract"
            ):
                protocol_contracts.append((section, repair_contract))
        if protocol_contracts:
            merged_requirements: list[dict[str, Any]] = []
            for section, repair_contract in protocol_contracts:
                raw_requirements = repair_contract.get("requirements")
                if not isinstance(raw_requirements, Sequence) or isinstance(
                    raw_requirements,
                    str | bytes | bytearray,
                ):
                    continue
                for requirement in raw_requirements:
                    if not isinstance(requirement, Mapping):
                        continue
                    merged_requirements.append(
                        {
                            **dict(DataFormatter.sanitize(requirement)),
                            "protocol_section": section,
                        }
                    )
            repair_contract = {
                "gate_kind": "output_contract",
                "issue_code": "terminal_verifier_output_invalid",
                "contract_subject": "verification:response",
                "protocol_sections": [
                    section for section, _contract in protocol_contracts
                ],
                "requirements": merged_requirements,
            }
            return (
                TerminalIssue(
                    "output_contract",
                    "terminal_verifier_output_invalid",
                    "verification:response",
                ),
                repair_contract,
                False,
            )
        if isinstance(material_claim_audit, Mapping) and material_claim_audit.get("valid") is False:
            repair_contract = material_repair_contract
            if isinstance(repair_contract, Mapping):
                issue = TerminalIssue(
                    str(repair_contract.get("gate_kind") or "factual_integrity"),
                    str(repair_contract.get("issue_code") or "material_claim_audit_invalid"),
                    str(repair_contract.get("contract_subject") or "artifact:factual_integrity"),
                )
                return issue, dict(DataFormatter.sanitize(repair_contract)), False
        if isinstance(criterion_audit, Mapping) and criterion_audit.get("valid") is False:
            repair_contract = criterion_repair_contract
            if isinstance(repair_contract, Mapping):
                issue = TerminalIssue(
                    str(repair_contract.get("gate_kind") or "criterion"),
                    str(repair_contract.get("issue_code") or "criterion_audit_invalid"),
                    str(
                        repair_contract.get("contract_subject")
                        or "verification:criterion_checks"
                    ),
                )
                return issue, dict(DataFormatter.sanitize(repair_contract)), False
        missing_capability_ids = self._normalize_string_list(verification.get("missing_capability_evidence"))
        missing_capability_ids = self._merge_string_lists(
            missing_capability_ids,
            verification.get("missing_required_capabilities"),
        )
        if missing_capability_ids:
            capability_id = missing_capability_ids[0]
            requirements = [
                dict(requirement)
                for requirement in self._capability_evidence_requirements(execution_evidence_summary)
                if str(requirement.get("capability_id") or "") == capability_id
            ]
            requirement = requirements[0] if requirements else {}
            capability_kind = str(requirement.get("capability_kind") or "capability")
            evidence_kind = str(requirement.get("kind") or "capability_used")
            issue_code = "action_succeeded_missing" if evidence_kind == "action_succeeded" else "capability_missing"
            repair_contract = {
                "gate_kind": "capability",
                "issue_code": issue_code,
                "contract_subject": f"{capability_kind}:{capability_id}",
                "capability_id": capability_id,
                "capability_kind": capability_kind,
                "evidence_kind": evidence_kind,
                "requirements": requirements,
            }
            available = {
                str(capability.get("id") or "")
                for capability in self._planner_capabilities()
                if isinstance(capability, Mapping)
                and str(capability.get("kind") or "") == "action"
            }
            unrecoverable = bool(
                evidence_kind == "action_succeeded"
                and capability_kind == "action"
                and capability_id
                and capability_id not in available
            )
            blocked = set(self._normalize_string_list(execution_evidence_summary.get("blocked_actions")))
            denied = set(self._normalize_string_list(execution_evidence_summary.get("approval_required_actions")))
            unrecoverable = unrecoverable or capability_id in blocked or capability_id in denied
            return (
                TerminalIssue("capability", issue_code, f"{capability_kind}:{capability_id}"),
                repair_contract,
                unrecoverable,
            )

        blocked_actions = self._normalize_string_list(execution_evidence_summary.get("blocked_actions"))
        if blocked_actions:
            action_id = blocked_actions[0]
            repair_contract = {
                "gate_kind": "lifecycle",
                "issue_code": "action_policy_blocked",
                "contract_subject": f"action:{action_id}",
                "blocked_action_ids": blocked_actions,
                "requirements": [],
            }
            return (
                TerminalIssue("lifecycle", "action_policy_blocked", f"action:{action_id}"),
                repair_contract,
                True,
            )

        raw_signals = execution_evidence_summary.get(
            "current_replan_signals",
            execution_evidence_summary.get("replan_signals", []),
        )
        blocking_signals = [
            signal
            for signal in raw_signals
            if isinstance(signal, Mapping) and str(signal.get("status") or "") == "blocked"
        ] if isinstance(raw_signals, Sequence) and not isinstance(raw_signals, str | bytes | bytearray) else []
        if blocking_signals:
            repair_contract = {
                "gate_kind": "lifecycle",
                "issue_code": "structured_execution_blocked",
                "contract_subject": "lifecycle:task",
                "signals": blocking_signals,
                "requirements": [],
            }
            return (
                TerminalIssue("lifecycle", "structured_execution_blocked", "lifecycle:task"),
                repair_contract,
                True,
            )

        guard_reasons = self._normalize_string_list(verification.get("guard_reasons"))
        if "final_result_output_parse_failed" in guard_reasons:
            repair_contract = {
                "gate_kind": "output_contract",
                "issue_code": "final_result_output_parse_failed",
                "contract_subject": "output:final_result",
                "requirements": self._normalize_string_list(verification.get("missing_criteria")),
            }
            return (
                TerminalIssue("output_contract", "final_result_output_parse_failed", "output:final_result"),
                repair_contract,
                False,
            )

        if verification.get("is_complete") is False:
            issue_code = guard_reasons[0] if guard_reasons else "criterion_unsatisfied"
            repair_contract = {
                "gate_kind": "criterion",
                "issue_code": issue_code,
                "contract_subject": "criterion:task",
                "requirements": self._normalize_string_list(verification.get("missing_criteria")),
                "next_step_requirements": self._normalize_string_list(verification.get("next_step_requirements")),
            }
            return TerminalIssue("criterion", issue_code, "criterion:task"), repair_contract, False
        return None

    def _terminal_convergence_verification(
        self,
        *,
        issue: TerminalIssue,
        repair_contract: Mapping[str, Any],
        decision: Mapping[str, Any],
        verifier_called: bool,
        candidate_final_result: str = "",
    ) -> dict[str, Any]:
        terminal = decision.get("terminal") is True
        occurrence = int(decision.get("occurrence") or 1)
        requirements = repair_contract.get("requirements")
        missing: list[str] = []
        if isinstance(requirements, Sequence) and not isinstance(requirements, str | bytes | bytearray):
            for requirement in requirements:
                if isinstance(requirement, Mapping):
                    text = str(
                        requirement.get("claim")
                        or requirement.get("reason")
                        or requirement.get("capability_id")
                        or requirement.get("subject_key")
                        or ""
                    ).strip()
                else:
                    text = str(requirement or "").strip()
                if text and text not in missing:
                    missing.append(text)
        if not missing:
            missing = [f"Resolve {issue.issue_code} for {issue.contract_subject}."]
        if terminal:
            reason = (
                f"Stopped after the same terminal issue '{issue.issue_code}' was detected three times; "
                "two repairs did not change the terminal outcome."
            )
        else:
            reason = (
                f"The relevant task state is unchanged for terminal issue '{issue.issue_code}' "
                f"(occurrence {occurrence}/3); skipped a redundant verifier request."
            )
        return {
            "is_complete": False,
            "requires_block": terminal,
            "reason": reason,
            "failure_analysis": reason,
            "acceptance_delta": missing,
            "missing_criteria": missing,
            "replan_instruction": "" if terminal else "Apply the structured repair contract before verification.",
            "repair_constraints": missing,
            "next_step_requirements": [] if terminal else missing,
            "final_result_required": bool(candidate_final_result),
            "final_result": candidate_final_result,
            "terminal_convergence": {
                **dict(DataFormatter.sanitize(decision)),
                "issue": {
                    "gate_kind": issue.gate_kind,
                    "issue_code": issue.issue_code,
                    "contract_subject": issue.contract_subject,
                },
                "repair_contract": dict(DataFormatter.sanitize(repair_contract)),
                "verifier_called": verifier_called,
                "stopped_after_third_occurrence": terminal,
            },
            "guard_reasons": ["terminal_convergence_stopped" if terminal else "terminal_state_unchanged"],
            "strict_terminal_gates_applied": True,
        }

    async def _apply_strict_terminal_gates(
        self,
        verification: dict[str, Any],
        *,
        candidate: Mapping[str, Any],
        execution_evidence_summary: Mapping[str, Any],
        verifier_called: bool,
    ) -> dict[str, Any]:
        normalized = dict(verification)
        issue_data = self._terminal_issue_from_verification(
            normalized,
            execution_evidence_summary=execution_evidence_summary,
        )
        if issue_data is None:
            self._terminal_convergence_state.mark_all_resolved()
            self.diagnostics["terminal_convergence"] = (
                self._terminal_convergence_state.snapshot()
            )
            normalized["terminal_convergence"] = {
                "resolved": True,
                "verifier_called": verifier_called,
            }
            normalized["strict_terminal_gates_applied"] = True
            return normalized
        issue, repair_contract, unrecoverable = issue_data
        state_digest = relevant_state_digest(
            self._strict_terminal_relevant_state(
                candidate=candidate,
                execution_evidence_summary=execution_evidence_summary,
                repair_contract=repair_contract,
            )
        )
        decision = self._terminal_convergence_state.record_detection(
            issue,
            state_digest,
            repair_contract=repair_contract,
            verifier_called=verifier_called,
            unrecoverable=unrecoverable,
        )
        normalized["terminal_convergence"] = {
            **dict(DataFormatter.sanitize(decision)),
            "issue": {
                "gate_kind": issue.gate_kind,
                "issue_code": issue.issue_code,
                "contract_subject": issue.contract_subject,
            },
            "repair_contract": repair_contract,
            "relevant_state_digest": state_digest,
            "stopped_after_third_occurrence": decision.get("terminal") is True,
        }
        self.diagnostics["terminal_convergence"] = self._terminal_convergence_state.snapshot()
        if decision.get("terminal") is True:
            stopped = self._terminal_convergence_verification(
                issue=issue,
                repair_contract=repair_contract,
                decision=decision,
                verifier_called=verifier_called,
                candidate_final_result=str(candidate.get("text") or normalized.get("final_result") or ""),
            )
            stopped["guard_reasons"] = self._merge_string_lists(
                normalized.get("guard_reasons"),
                stopped.get("guard_reasons"),
            )
            for key in (
                "missing_required_capabilities",
                "missing_capability_evidence",
                "unenforced_evidence_requirements",
            ):
                if key in normalized:
                    stopped[key] = DataFormatter.sanitize(normalized[key])
            return stopped
        normalized["strict_terminal_gates_applied"] = True
        return normalized

    def _terminal_delivery_contract_for_verifier(
        self,
        execution_result: Any,
        terminal_candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project a verified staged-delivery phase without exposing promotion mechanics."""

        if not isinstance(execution_result, Mapping):
            return {}
        raw_promotions = execution_result.get("staged_promotions")
        if not isinstance(raw_promotions, Sequence) or isinstance(
            raw_promotions,
            str | bytes | bytearray,
        ):
            return {}
        raw_carriers = terminal_candidate.get("carriers")
        carriers = (
            [item for item in raw_carriers if isinstance(item, Mapping)]
            if isinstance(raw_carriers, Sequence)
            and not isinstance(raw_carriers, str | bytes | bytearray)
            else []
        )
        carrier_versions = {
            (
                self._task_workspace_artifact_display_path(carrier.get("path")),
                str(carrier.get("content_version_id") or "").strip(),
            )
            for carrier in carriers
            if str(carrier.get("kind") or "") == "task_workspace_artifact"
            and str(carrier.get("status") or "") == "materialized"
        }
        required_targets = {
            self._task_workspace_artifact_display_path(path)
            for path in self._required_task_workspace_deliverables()
            if self._task_workspace_artifact_display_path(path)
        }
        candidate_mappings: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_promotion in raw_promotions:
            if not isinstance(raw_promotion, Mapping):
                continue
            candidate_path = self._task_workspace_artifact_display_path(
                raw_promotion.get("source_path")
            )
            required_target_path = self._task_workspace_artifact_display_path(
                raw_promotion.get("target_path")
            )
            content_version_id = str(
                raw_promotion.get("source_content_version_id") or ""
            ).strip()
            mapping_key = (candidate_path, required_target_path)
            if (
                not candidate_path
                or not required_target_path
                or required_target_path not in required_targets
                or (candidate_path, content_version_id) not in carrier_versions
                or mapping_key in seen
            ):
                continue
            seen.add(mapping_key)
            candidate_mappings.append(
                {
                    "candidate_path": candidate_path,
                    "required_target_path": required_target_path,
                    "candidate_state": "complete_readback_verified",
                    "target_state": "deferred_until_semantic_acceptance",
                }
            )
        if not candidate_mappings:
            return {}
        return {
            "phase": "pre_promotion_candidate_verification",
            "candidate_mappings": candidate_mappings,
            "semantic_acceptance_scope": (
                "Judge the staged candidate bytes against every semantic success criterion."
            ),
            "post_acceptance_host_guards": [
                "atomically promote the exact verifier-accepted candidate bytes",
                "completely read back every required target",
                "verify target digest and byte count before terminal completion",
            ],
        }

    async def _request_verification(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        execution_result: Any,
        execution_meta: dict[str, Any],
        context_pack: "TaskContextView",
    ) -> dict[str, Any]:
        language_policy = self._language_policy()
        initial_evidence_use = collect_evidence_use(execution_result)
        candidate_required_reference_ids = {
            evidence_id
            for use in initial_evidence_use
            for evidence_id in self._normalize_string_list(use.get("evidence_ids"))
        }
        raw_execution_evidence_summary = self._execution_log_summary(execution_meta)
        raw_cumulative_evidence_summary = self._cumulative_execution_evidence_summary(execution_meta)
        evidence_ledger = self._cumulative_evidence_ledger(
            execution_meta,
            required_evidence_ids=candidate_required_reference_ids,
        )
        initial_guard = validate_evidence_use(initial_evidence_use, evidence_ledger)
        await self._ensure_task_workspace_artifact_targeted_readback_evidence(
            execution_meta,
            evidence_ledger,
            evidence_use=initial_guard.get("normalized_evidence_use"),
        )
        raw_execution_evidence_summary = self._execution_log_summary(execution_meta)
        raw_cumulative_evidence_summary = self._cumulative_execution_evidence_summary(execution_meta)
        evidence_ledger = self._cumulative_evidence_ledger(
            execution_meta,
            required_evidence_ids=candidate_required_reference_ids,
        )
        grounding_guard = validate_evidence_use(initial_evidence_use, evidence_ledger)
        binding_reference_ids = set(
            self._task_reference_catalog.offered_references()
        )
        normalized_execution_result = value_with_normalized_evidence_use(
            execution_result,
            grounding_guard.get("normalized_evidence_use"),
        )
        repaired_evidence_use: list[dict[str, Any]] = []
        if grounding_guard.get("blocking_count"):
            repaired_evidence_use = self._deterministic_evidence_binding_repair(
                grounding_guard,
                evidence_ledger,
                offered_reference_ids=binding_reference_ids,
            )
        if repaired_evidence_use:
            self.diagnostics.setdefault("evidence_binding_repair", []).append(
                DataFormatter.sanitize(
                    {
                        "source": "deterministic_alias_resolver",
                        "repaired_count": len(repaired_evidence_use),
                    }
                )
            )
        elif self._should_attempt_evidence_binding_repair(grounding_guard):
            if self._can_attempt_model_evidence_binding_repair():
                try:
                    binding_repair_count = int(
                        self.diagnostics.get("evidence_binding_repair_attempt_count") or 0
                    )
                except (TypeError, ValueError):
                    binding_repair_count = 0
                self.diagnostics["evidence_binding_repair_attempt_count"] = binding_repair_count + 1
                repaired_evidence_use = await self._request_evidence_binding_repair(
                    grounding_guard,
                    evidence_ledger,
                    language_policy=language_policy,
                    offered_reference_ids=binding_reference_ids,
                )
            else:
                self.diagnostics.setdefault("evidence_binding_repair", []).append(
                    DataFormatter.sanitize(
                        {
                            "source": "model_repair_attempt_gate",
                            "skipped": True,
                            "attempt_count": self.diagnostics.get("evidence_binding_repair_attempt_count"),
                            "reason": "model evidence binding repair attempt limit reached; deterministic repair had no unique candidate",
                        }
                    )
                )
        if repaired_evidence_use:
            merged_evidence_use = self._merge_repaired_evidence_use(
                grounding_guard.get("normalized_evidence_use"),
                repaired_evidence_use,
            )
            candidate_execution_result = value_with_normalized_evidence_use(
                normalized_execution_result,
                merged_evidence_use,
            )
            candidate_guard = validate_evidence_use(collect_evidence_use(candidate_execution_result), evidence_ledger)
            self.diagnostics.setdefault("evidence_binding_repair", []).append(
                DataFormatter.sanitize(
                    {
                        "accepted": candidate_guard.get("valid") is True,
                        "blocking_count": candidate_guard.get("blocking_count"),
                        "diagnostics": candidate_guard.get("diagnostics", []),
                    }
                )
            )
            if candidate_guard.get("valid") is True:
                normalized_execution_result = candidate_execution_result
                grounding_guard = candidate_guard
        trusted_task_workspace_artifacts = self._task_workspace_artifact_index_for_verifier(evidence_ledger)
        evidence_summary = self._compact_verifier_evidence_summary(
            raw_execution_evidence_summary,
            include_body_previews=False,
        )
        cumulative_evidence_summary = self._compact_verifier_evidence_summary(
            raw_cumulative_evidence_summary,
            include_body_previews=False,
        )
        capability_evidence_requirements = self._capability_evidence_requirements(raw_cumulative_evidence_summary)
        verifier_execution_result = self._task_workspace_artifact_execution_result_for_verifier(normalized_execution_result)
        candidate_final_result = self._candidate_final_result_from_execution_result(normalized_execution_result)
        await self._replace_terminal_carriers(
            execution_result=normalized_execution_result,
            execution_evidence_summary=raw_cumulative_evidence_summary,
            source_work_result_id=str(
                execution_meta.get("execution_id") or f"iteration:{iteration_index}"
            ),
        )
        inventory = self._lifecycle_state.carrier_inventory
        verification_phase = (
            "pre_promotion_verifying"
            if any(
                carrier.kind == "task_workspace_artifact"
                and carrier.path != carrier.target_path
                for carrier in inventory.carriers
            )
            else "root_verifying"
            if any(carrier.kind == "task_workspace_artifact" for carrier in inventory.carriers)
            else "candidate_verifying"
        )
        self._lifecycle_state.advance(
            verification_phase,
            expected_version=self._lifecycle_state.state_version,
            iteration=iteration_index,
        )
        strict_candidate = await self._current_terminal_candidate()
        terminal_delivery_contract = self._terminal_delivery_contract_for_verifier(
            normalized_execution_result,
            strict_candidate,
        )
        capability_preflight = await self._terminal_capability_evidence_preflight(
            candidate=strict_candidate,
            execution_evidence_summary=raw_cumulative_evidence_summary,
            candidate_final_result=candidate_final_result,
        )
        if capability_preflight is not None:
            self.diagnostics["terminal_convergence"] = (
                self._terminal_convergence_state.snapshot()
            )
            await self._emit_process_progress_from_output(
                capability_preflight,
                stage="verification",
                iteration=iteration_index,
            )
            return capability_preflight
        convergence_preflight = self._terminal_convergence_preflight(
            candidate=strict_candidate,
            execution_evidence_summary=raw_cumulative_evidence_summary,
        )
        if convergence_preflight is not None:
            self.diagnostics["terminal_convergence"] = self._terminal_convergence_state.snapshot()
            await self._emit_process_progress_from_output(
                convergence_preflight,
                stage="verification",
                iteration=iteration_index,
            )
            return convergence_preflight
        verification_context_pack, context_package = await self._read_task_context_view(
            phase="verification",
            consumer_id=f"agent_task:{self.id}:verifier:iteration:{iteration_index}",
            intent=f"Verify iteration {iteration_index}: {self.goal}",
        )
        request = self.agent.create_temp_request()
        self._bind_task_context_attachments(request, context_package)
        self._apply_language_policy_to_request(request, language_policy)
        model_evidence_ledger = self._model_evidence_ledger_projection(
            evidence_ledger,
            max_items=_VERIFIER_LEDGER_MAX_ITEMS + 8,
            offered_reference_ids=set(
                self._task_reference_catalog.offered_references()
            ),
            required_reference_ids=candidate_required_reference_ids,
        )
        offered_reference_snapshot = {
            str(item.get("reference_id") or "").strip()
            for item in model_evidence_ledger.get("items", [])
            if isinstance(item, Mapping)
            and str(item.get("reference_id") or "").strip()
        }
        material_claim_candidates = (
            self._material_claim_candidates_for_verifier(strict_candidate)
        )
        protocol_repair = self._active_terminal_verification_protocol_repair(
            current_offered_reference_ids=offered_reference_snapshot,
            current_offered_claim_keys=[
                str(claim.get("claim_key") or "")
                for claim in material_claim_candidates
            ],
        )
        verifier_input = {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": [
                {"criterion_id": f"criterion:{index}", "text": str(criterion)}
                for index, criterion in enumerate(self.success_criteria, start=1)
            ],
            "material_claim_policy": {
                "required": True,
                "summary": (
                    "Ground material factual claims in task evidence. Distinguish derived analysis from reported facts "
                    "and preserve scope, uncertainty, and pending states. Runtime date/time is execution context, not "
                    "task evidence by itself. "
                ),
            },
            "iteration": iteration_index,
            "plan": self._compact_verifier_prompt_value(plan, max_chars=_VERIFIER_PROMPT_ITEM_CHARS),
            "candidate_final_result": self._compact_verifier_prompt_value(
                candidate_final_result,
                max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
            ),
            "material_claim_candidates": material_claim_candidates,
            "execution_result": self._compact_verifier_prompt_value(
                verifier_execution_result,
                max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
            ),
            "execution_meta": self._verification_execution_meta_summary(execution_meta),
            "execution_evidence_summary": evidence_summary,
            "cumulative_execution_evidence_summary": cumulative_evidence_summary,
            "evidence_ledger": model_evidence_ledger,
            "acceptance_locator_view": self._model_acceptance_locator_view(
                evidence_ledger
            ),
            "grounding_guard": self._compact_grounding_guard_for_verifier(grounding_guard),
            "trusted_task_workspace_artifacts": trusted_task_workspace_artifacts,
            "terminal_delivery_contract": terminal_delivery_contract,
            "capability_evidence_requirements": capability_evidence_requirements,
            "context_pack": self._compact_context_pack_for_verifier(
                cast("TaskContextView", verification_context_pack)
            ),
            "execution_prompt": self._execution_prompt_context(),
            "previous_iterations": self._iteration_prompt_summaries(),
            "reflection_summaries": self._reflection_prompt_summaries(),
            "language_policy": language_policy,
        }
        if protocol_repair:
            verifier_input["verification_protocol_repair"] = protocol_repair
        serialized_input_characters = len(
            json.dumps(
                DataFormatter.sanitize(verifier_input),
                ensure_ascii=False,
                default=str,
            )
        )
        self.diagnostics.setdefault("verifier_prompt_projection", []).append(
            {
                "serialized_input_characters": serialized_input_characters,
                "target_characters": _VERIFIER_PROMPT_TARGET_CHARS,
                "over_target": serialized_input_characters > _VERIFIER_PROMPT_TARGET_CHARS,
            }
        )
        request.input(verifier_input)
        request.instruct(
            "Verify the task against every success criterion. "
            "Also consider caller-provided execution_prompt constraints when they are present. "
            "For criterion_checks, return each offered success_criteria[].criterion_id exactly once; do not copy "
            "criterion text as identity and do not invent, omit, or duplicate criterion ids. "
            "When verification_protocol_repair is present, the previous verifier response violated the declared "
            "output contract. Correct every listed repair_contract requirement in this response, use only the "
            "current offered criterion, claim, or evidence keys in that field, and do not rerun or rewrite business work merely "
            "to repair the verifier response structure. "
            "Treat numeric criteria such as 'at least N' as exact counting rules and fail verification when the "
            "evidence does not meet the count. "
            "Require source/evidence references when the criteria ask for evidence. "
            "Treat evidence_ledger as the single authoritative model-visible grounding ledger. Each item exposes exactly "
            "one host-issued reference_id; use only exact offered items[].reference_id values in criterion_checks.evidence_ids. "
            "Canonical ids and aliases remain host-side. Other summaries and indexes are body-light projections. "
            "Use acceptance_locator_view as a body-light verifier readback index for TaskWorkspace artifacts: locator items show "
            "where to inspect an artifact, not whether the content is semantically correct. It deliberately exposes no "
            "selection ids. Prefer bounded readback "
            "items produced from those locators when checking long artifact sections. A locator with "
            "requirement_level='required' comes from an output contract or success criterion; status=empty on that "
            "locator is a structural gap only when no equivalent localized/renamed section, targeted readback, or "
            "trusted artifact snippet covers that required content area. Output-contract section labels are content "
            "areas, not exact heading-text mandates, unless the task explicitly requires exact headings. A locator "
            "with requirement_level='advisory' comes from a model-suggested "
            "acceptance point; status=empty means that proposed anchor was not found, but it is not by itself proof that "
            "the deliverable lacks the required content when other required locators or targeted readbacks cover it. "
            "evidence_ledger includes current and prior iteration evidence that is verifier-visible. Do not perform or "
            "assume extra readback outside this ledger. failed/empty ledger "
            "items are facts of unavailability only; ref_only items prove only a URL/path/ref was found; bounded or "
            "truncated content supports only the visible body. overflow_item_refs are key evidence points whose body "
            "did not fit the view budget: an overflow item with status=ok and body_state full/bounded/truncated is a "
            "record that the readback HAPPENED — never conclude a source was unread or unviewed while such an item "
            "exists for it. When its content is material to the judgment, use its existing path/ref to request scoped "
            "readback rather than treating the omitted body as visible. grounding_guard contains deterministic id/status/body "
            "state diagnostics that identify evidence-binding gaps. Treat blocking_count as a reason to request "
            "binding repair or additional scoped evidence when a required claim remains unsupported; do not reject a "
            "long artifact solely because an exact locator label missed while equivalent verifier-visible readback "
            "covers the required content area. "
            "Use both execution_evidence_summary and cumulative_execution_evidence_summary; the final verification "
            "must account for evidence gathered in earlier iterations, not only the current write/finalize step. "
            "Use reflection_summaries as evaluator notes linked to evidence and verification; reflection records are not "
            "completion evidence by themselves. "
            "Check material factual claims against verifier-visible evidence, including additions beyond the "
            "success criteria. The referenced content must support the claim at its stated specificity, scope, "
            "and certainty; a citation or locator alone is not support. Use evidence that directly supports the "
            "claim and account for relevant contradictions. Distinguish evidence-derived analysis from reported "
            "facts; preserve uncertainty and pending states. Runtime date/time describes execution context; it "
            "establishes a task fact only when task evidence connects them. Judge completeness against the "
            "content required by the task. Unresolved required content remains a gap; templates or intentionally "
            "open fields are valid when the task requests them. "
            "If bounded previews are enough to contradict the candidate, set is_complete=false. If the previews are "
            "too truncated to verify a material claim, set is_complete=false and ask for scoped evidence readback. "
            "In the same response, complete material_claim_checks for material external facts, bounded derived analysis, "
            "recommendations, and preserved uncertainty in material_claim_candidates. Each candidate is one exact "
            "host-owned span selected only by claim_key. This is one semantic audit, not a demand "
            "that every sentence reproduce source wording. Use state='reasonable_derived' when an analytical conclusion "
            "is a proportionate inference from the offered evidence even though no source states the conclusion verbatim. "
            "Classify any claim of non-existence, absence, not-found, missing, or no-public-artifact as "
            "claim_kind='absence_claim', even when it is phrased as an analytical conclusion. Partial directory lists, "
            "bounded searches, failed reads, empty previews, and evidence from a different subtree cannot establish global "
            "absence; an absence claim is supported only by verifier-visible evidence whose scope exhaustively covers the "
            "claim. Never use state='reasonable_derived' for external_fact, absence_claim, or uncertainty. "
            "Use state='supported' for direct external facts and exhaustively evidenced absence claims, and "
            "unsupported/contradicted/unverifiable only when the "
            "offered evidence actually fails to support the material statement. Return only the exact offered claim_key "
            "and exact offered evidence reference_id values; the host reconstructs carrier, path, version, and exact quote. "
            "The exact carrier text is already host-validated by material_claim_candidates and does not need to appear in evidence_ledger "
            "to prove that the quote occurs in the final carrier. Use evidence_ledger only to judge whether an external or derived claim "
            "inside that carrier span is supported. syntax_role describes lexical Markdown framing only. Pure Markdown headings, "
            "separators, table headers, and table separators are excluded from material_claim_candidates by the host because their document/criterion "
            "role is checked through artifact readback and acceptance locators, not as standalone factual claims. "
            "Set material_claim_coverage_complete=true only after checking all material external facts and material "
            "evidence-derived conclusions in the offered material_claim_candidates. It is valid to return an empty check list "
            "for ordinary transformation, formatting, code, or writing output that makes no material external factual claim. "
            "If execution metadata, action records, diagnostics, command output, or verifier-visible evidence shows "
            "a failed required action or failed validation command, do not mark complete. "
            "For every material_claim_checks item, return required_for_criterion_ids as the exact offered criterion ids whose "
            "satisfaction semantically requires that claim; return an empty list when the claim is optional or extraneous. "
            "Do not invent, copy criterion text into, omit duplicates from, or duplicate ids in this relationship. A required "
            "claim that lacks supporting evidence needs status='replan_segment', not a carrier-only repair or deletion. "
            "If a criterion requires a script, command, test, or external validation to pass, require explicit "
            "successful evidence for that validation before completion. "
            "Decide final_result_required from the goal and success criteria: set it true when the task demands a "
            "concrete final deliverable (answer, file, report, artifact, or similar) and false when the work is "
            "purely an action or side effect with no expected returned deliverable. "
            "For non-file deliverables, final_result should contain the returned answer body. For trusted TaskWorkspace "
            "artifact deliverables, the body remains in TaskWorkspace and trusted_task_workspace_artifacts plus file_refs/readback "
            "are the completion evidence; final_result may be a concise path/ref summary and must not copy the full "
            "artifact body only to satisfy a structured field. trusted_task_workspace_artifacts is a body-light TaskWorkspace "
            "location/status index with no selection identity, and the index itself does not prove artifact content. "
            "When terminal_delivery_contract.phase='pre_promotion_candidate_verification', each candidate_mappings item is a "
            "host-validated complete readback of the exact bytes proposed for required_target_path. Judge those candidate bytes "
            "against the semantic success criteria and treat them as the provisional carrier for that required target. The target "
            "path is intentionally absent until semantic acceptance: the host will then atomically promote the accepted bytes, "
            "completely read back the target, and verify its digest and byte count. Therefore criterion_checks and is_complete "
            "must not reject the candidate merely because the target path is absent during "
            "pre_promotion_candidate_verification. Reject it for content, evidence, or other semantic gaps when warranted; "
            "post-acceptance promotion/readback failures remain host-owned terminal failures. "
            "For source-grounded TaskWorkspace artifacts, verify the artifact "
            "body in evidence_ledger readback and targeted-readback items "
            "against visible source_refs, Action evidence, "
            "URLs, paths, and refs; a final_result path pointer alone is not enough to satisfy citation or provenance "
            "requirements. For long artifacts, also inspect task_workspace_artifact.targeted_readback ledger items for bounded "
            "section, tail, source-list, risk, reference, or coverage snippets before concluding a required section is "
            "missing. Treat long TaskWorkspace artifact verification as an acceptance-point evidence review, not a "
            "whole-document editorial review: judge whether verifier-visible bounded snippets, required locators, "
            "targeted readbacks, and cited source/action evidence cover the required structure and material claims. "
            "Do not require risk, uncertainty, limitation, or caveat sections unless the user task, output contract, "
            "success criteria, or verifier-visible evidence limitations explicitly require them. "
            "Do not require full-artifact reading merely to assert general overall quality unless the success criteria "
            "explicitly require whole-document proofreading, style review, or exhaustive line-by-line audit. If all "
            "required acceptance points and material claims are supported by verifier-visible evidence, accept without "
            "requesting broader readback. Do not ask a later step to read a TaskWorkspace artifact solely to paste its full content into "
            "final_result. If trusted artifact refs or readback are missing or too scoped to verify a material claim, "
            "keep is_complete=false and ask for scoped artifact readback or repair. "
            "When candidate_final_result contains a complete answer/report/artifact body that satisfies the criteria, "
            "use it as final_result. When the plan or success criteria require a TaskWorkspace artifact, accept only "
            "trusted TaskWorkspace write/readback refs from execution evidence; model-declared file_refs are diagnostics. "
            "If evidence is incomplete, set is_complete=false and explain failure_analysis and acceptance_delta: "
            "why the task is not accepted, which acceptance facts are missing or weak, and what evidence boundary "
            "blocked verification. Return one structured replan_signal. Use status='continue' only when complete; "
            "status='repair' when current verifier-visible evidence is sufficient and only the existing carrier or "
            "binding must change; status='replan_segment' when additional scoped evidence, readback, capability work, "
            "or a changed current-board path is required; status='replan_goal' only when the current whole-task path "
            "cannot satisfy the goal; and blocked or clarify only when continuation needs new authority, input, "
            "capability, or external state. The verifier does not choose tools, routes, execution shapes, or exact methods. "
            "repair_constraints and next_step_requirements are advisory compatibility fields only; keep them factual "
            "and do not turn them into a narrow tool script. Also include a short human-readable replan_instruction. "
            "After the judgment fields, include compact criterion_checks, verification_summary, and progress_message "
            "for downstream repair context and human progress. These fields are process summaries only; they are not "
            "completion evidence and must not contain raw chain-of-thought or long evidence bodies. "
            "When returning a terminal judgment, also include final_response as a concise user-facing status or answer "
            "note based only on the same visible evidence and structured judgment. For file-backed deliverables, "
            "mention the artifact path/ref and any known limitation instead of copying the whole file body. "
            "final_response is display context only and must not be used as completion evidence. "
            "Set requires_block=true only when the task cannot continue."
        )
        literal_factory = cast(Any, Literal)
        criterion_id_type = literal_factory.__getitem__(
            tuple(
                str(item["criterion_id"])
                for item in verifier_input["success_criteria"]
            )
        )
        claim_keys = tuple(
            str(item.get("claim_key") or "")
            for item in material_claim_candidates
            if str(item.get("claim_key") or "")
        )
        claim_key_type = (
            literal_factory.__getitem__(claim_keys) if claim_keys else str
        )
        evidence_reference_ids = tuple(sorted(offered_reference_snapshot))
        evidence_reference_type = (
            literal_factory.__getitem__(evidence_reference_ids)
            if evidence_reference_ids
            else str
        )
        request.output(
            {
                "is_complete": (bool, "True only when all success criteria are satisfied", True),
                "requires_block": (bool, "True only when the task cannot continue", True),
                "reason": (str, "Concise verification reason", True),
                "failure_analysis": (
                    str,
                    "Why the current result is incomplete or unacceptable; empty when complete",
                    False,
                ),
                "acceptance_delta": (
                    [str],
                    "Specific acceptance facts or evidence gaps that remain unsatisfied",
                    False,
                ),
                "missing_criteria": ([str], "Unmet or weak criteria, empty when none"),
                "replan_instruction": (str, "Instruction for the next planning round when incomplete"),
                "repair_constraints": (
                    [str],
                    "Advisory factual constraints; not a tool, route, or execution-shape plan",
                ),
                "next_step_requirements": (
                    [str],
                    "Advisory observable requirements for future evidence; not a hard method script",
                ),
                "replan_signal": (
                    {
                        "status": (
                            Literal[
                                "continue",
                                "repair",
                                "replan_segment",
                                "replan_goal",
                                "blocked",
                                "clarify",
                            ],
                            "Semantic next transition. Use repair only when current evidence is sufficient; use replan_segment when additional evidence or a changed current-board path is required.",
                            False,
                        ),
                        "reason": (str, "Concise evidence-based reason for the transition.", False),
                        "evidence_refs": (
                            [evidence_reference_type],
                            "Only exact offered evidence reference ids relevant to this transition.",
                            False,
                        ),
                    },
                    "Structured ReplanSignal validated and consumed by AgentTask; omitted legacy/model responses normalize to repair.",
                    False,
                ),
                "final_result_required": (bool, "True when the goal expects a concrete returned final deliverable"),
                "final_result": (str, "Final business result when complete"),
                "final_response": (
                    str,
                    "Concise user-facing terminal answer/status note; display context only, not completion evidence.",
                    False,
                ),
                "criterion_checks": (
                    [
                        {
                            "criterion_id": (
                                criterion_id_type,
                                "One exact offered success_criteria[].criterion_id.",
                                True,
                            ),
                            "satisfied": (bool, "True only when this criterion is satisfied.", True),
                            "summary": (str, "Concise criterion judgment.", True),
                            "gaps": ([str], "Specific gaps for this criterion.", False),
                            "evidence_ids": (
                                [evidence_reference_type],
                                "Only exact offered evidence_ledger.items[].reference_id values.",
                                False,
                            ),
                        }
                    ],
                    "Exactly one compact structured check for every offered success criterion.",
                    True,
                ),
                "material_claim_coverage_complete": (
                    bool,
                    "True only after every material external fact or evidence-derived conclusion in material_claim_candidates was audited.",
                    True,
                ),
                "material_claim_checks": (
                    [
                        {
                            "claim_key": (
                                claim_key_type,
                                "One exact offered material_claim_candidates[].claim_key; the host reconstructs all canonical carrier and quote fields.",
                                True,
                            ),
                            "claim_kind": (
                                Literal[
                                    "external_fact",
                                    "absence_claim",
                                    "derived_analysis",
                                    "recommendation",
                                    "uncertainty",
                                ],
                                "external_fact, absence_claim, derived_analysis, recommendation, or uncertainty.",
                                True,
                            ),
                            "state": (
                                Literal[
                                    "supported",
                                    "reasonable_derived",
                                    "unsupported",
                                    "contradicted",
                                    "unverifiable",
                                ],
                                "supported, reasonable_derived, unsupported, contradicted, or unverifiable.",
                                True,
                            ),
                            "evidence_ids": (
                                [evidence_reference_type],
                                "Only exact offered evidence_ledger.items[].reference_id values.",
                                False,
                            ),
                            "required_for_criterion_ids": (
                                [criterion_id_type],
                                "Exact offered criterion ids whose satisfaction requires this claim; empty when optional.",
                                True,
                            ),
                            "reason": (str, "Concise support or failure reason.", True),
                        }
                    ],
                    "Material factual and derived-claim audit across all visible terminal carriers; empty when none exist.",
                    True,
                ),
                "verification_summary": (
                    str,
                    "Short verifier summary for repair context; no raw chain-of-thought.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable verification progress sentence.",
                    False,
                ),
            },
            format="json",
        )
        result_handle = request.get_result()
        verification = await self._await_task_request(
            result_handle.async_get_data(),
            stage="verify",
        )
        self._record_task_context_consumption(
            context_package,
            request_id=result_handle.id,
        )
        await self._emit_required_skill_context_bound(
            verification_context_pack,
            request_id=result_handle.id,
            phase="terminal.verify",
        )
        if not isinstance(verification, dict):
            verification = {
                "is_complete": False,
                "requires_block": False,
                "reason": str(verification),
                "failure_analysis": str(verification),
                "acceptance_delta": self.success_criteria,
                "missing_criteria": self.success_criteria,
                "replan_instruction": "Run another bounded step with stronger evidence.",
                "repair_constraints": self.success_criteria,
                "next_step_requirements": ["Run another bounded step with stronger evidence."],
                "final_result_required": False,
                "final_result": "",
            }
        normalized = self._normalize_verification(
            verification,
            execution_evidence_summary=raw_cumulative_evidence_summary,
            candidate_final_result=candidate_final_result,
            grounding_guard=grounding_guard,
            terminal_candidate=strict_candidate,
            offered_reference_ids=offered_reference_snapshot,
        )
        normalized = await self._apply_strict_terminal_gates(
            normalized,
            candidate=strict_candidate,
            execution_evidence_summary=raw_cumulative_evidence_summary,
            verifier_called=True,
        )
        terminal_evidence_projection = (
            self._terminal_evidence_projection_for_observers(
                model_evidence_ledger,
                normalized,
            )
        )
        if terminal_evidence_projection:
            self.diagnostics["terminal_evidence_projection"] = (
                terminal_evidence_projection
            )
        await self._emit_process_progress_from_output(
            normalized,
            stage="verification",
            iteration=iteration_index,
        )
        return normalized

    async def _ensure_task_workspace_artifact_targeted_readback_evidence(
        self,
        execution_meta: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any],
        *,
        evidence_use: Any = None,
    ) -> None:
        if not isinstance(execution_meta, dict):
            return
        existing_generic_paths: set[str] = set()
        existing_locator_readbacks: set[str] = set()
        for item in evidence_ledger.get("items", []) if isinstance(evidence_ledger, Mapping) else []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("kind") or "") != "task_workspace_artifact.targeted_readback":
                continue
            path = str(item.get("path") or "").strip()
            provenance = item.get("provenance")
            source_evidence_id = (
                str(provenance.get("source_evidence_id") or "").strip() if isinstance(provenance, Mapping) else ""
            )
            if source_evidence_id:
                existing_locator_readbacks.add(source_evidence_id)
            elif path:
                existing_generic_paths.add(path)

        evidence_items: list[dict[str, Any]] = []
        for locator in acceptance_locator_view_from_ledger(evidence_ledger).get("items", []):
            if not isinstance(locator, Mapping):
                continue
            locator_id = str(locator.get("id") or "").strip()
            if not locator_id or locator_id in existing_locator_readbacks:
                continue
            if str(locator.get("status") or "") != "ok":
                continue
            readback = await self._task_workspace_artifact_acceptance_locator_readback(locator)
            if readback is not None:
                evidence_items.append(self._task_workspace_artifact_targeted_readback_evidence_item(locator, readback))
                existing_locator_readbacks.add(locator_id)

        claim_queries = self._evidence_use_verifier_target_queries(evidence_use)
        for artifact in task_workspace_artifacts_from_ledger(evidence_ledger):
            path = str(artifact.get("path") or "").strip()
            if not path or path in existing_generic_paths:
                continue
            if str(artifact.get("status") or "") != "ok":
                continue
            if str(artifact.get("body_state") or "") != "truncated":
                continue
            try:
                read_result = await self.task_workspace.read_file(
                    path,
                    max_bytes=self._task_workspace_artifact_verifier_readback_bytes(artifact),
                )
            except Exception as error:
                evidence_items.append(
                    self._task_workspace_artifact_targeted_readback_evidence_item(
                        artifact,
                        {
                            "kind": "verifier_readback",
                            "path": path,
                            "status": "failed",
                            "error": {
                                "type": error.__class__.__name__,
                                "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                            },
                        },
                    )
                )
                continue
            for readback in await self._trusted_task_workspace_artifact_targeted_readbacks(
                artifact,
                read_result,
                queries=claim_queries,
            ):
                evidence_items.append(self._task_workspace_artifact_targeted_readback_evidence_item(artifact, readback))
        self._append_execution_meta_evidence_items(execution_meta, evidence_items)

    @classmethod
    def _task_workspace_artifact_targeted_readback_evidence_item(
        cls,
        artifact: Mapping[str, Any],
        readback: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = str(readback.get("path") or artifact.get("path") or "").strip()
        raw_status = str(readback.get("status") or "read").strip()
        status = "failed" if raw_status == "failed" else "ok"
        kind = str(readback.get("kind") or "targeted_readback").strip()
        content = str(readback.get("content") or "")
        query = str(readback.get("query") or readback.get("matched_query") or "")
        locator = query or str(readback.get("offset") or readback.get("line_start") or kind)
        snapshot_key = str(
            readback.get("content_version_id")
            or artifact.get("content_version_id")
            or readback.get("snapshot_id")
            or artifact.get("snapshot_id")
            or readback.get("source_evidence_id")
            or artifact.get("id")
            or "unversioned"
        ).strip()
        evidence_id = cls._task_workspace_artifact_evidence_id(
            "task_workspace_artifact_targeted_readback",
            path,
            f"{snapshot_key}:{kind}:{locator}",
        )
        item: dict[str, Any] = {
            "id": evidence_id,
            "kind": "task_workspace_artifact.targeted_readback",
            "status": status,
            "raw_status": raw_status,
            "body_state": "ref_only" if status == "failed" else ("truncated" if readback.get("truncated") else "bounded"),
            "path": path,
            "aliases": cls._task_workspace_artifact_targeted_readback_aliases(
                path=path,
                query=query,
                readback=readback,
                artifact=artifact,
            ),
            "source": "agent_task.task_workspace_artifact.targeted_readback",
            "provenance": {
                "source": "agent_task.task_workspace_artifact.targeted_readback",
                "source_evidence_id": readback.get("source_evidence_id") or artifact.get("id"),
                "path": path,
                "kind": kind,
                "query": query,
                "matched_query": readback.get("matched_query"),
                "offset": readback.get("offset"),
                "line_start": readback.get("line_start"),
                "line_end": readback.get("line_end"),
            },
            "supports": {
                "content": status == "ok",
                "unavailability": status == "failed",
                "ref_pointer": False,
            },
        }
        for field in ("locator_id", "content_version_id", "snapshot_id", "sha256"):
            value = readback.get(field) if readback.get(field) not in (None, "", [], {}) else artifact.get(field)
            if value not in (None, "", [], {}):
                item[field] = DataFormatter.sanitize(value)
                item["provenance"][field] = DataFormatter.sanitize(value)
        for field in ("criterion_id", "heading", "anchor_text", "claim", "topic", "requirement_level", "point_source"):
            value = readback.get(field) if readback.get(field) not in (None, "", [], {}) else artifact.get(field)
            if value not in (None, "", [], {}):
                item[field] = DataFormatter.sanitize(value)
        if content:
            item["body"] = content
        if status == "failed":
            item["diagnostics"] = [
                {
                    "code": "agent_task.task_workspace_artifact.targeted_readback_failed",
                    "message": "TaskWorkspace artifact targeted readback failed before verifier request.",
                    "error": DataFormatter.sanitize(readback.get("error") or {}),
                }
            ]
        return DataFormatter.sanitize(item)

    @classmethod
    def _task_workspace_artifact_targeted_readback_aliases(
        cls,
        *,
        path: str,
        query: str,
        readback: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)
            slug = cls._task_workspace_artifact_readback_alias_slug(text)
            if slug and slug not in aliases:
                aliases.append(slug)

        add(path)
        add(PurePosixPath(path.replace("\\", "/")).name if path else "")
        add(query)
        add(readback.get("matched_query"))
        add(readback.get("source_evidence_id"))
        add(artifact.get("id"))
        for field in ("criterion_id", "heading", "anchor_text", "claim", "topic"):
            add(readback.get(field))
            add(artifact.get(field))
        return aliases[:24]

    @staticmethod
    def _task_workspace_artifact_readback_alias_slug(value: str) -> str:
        text = str(value or "").strip().lower().replace("_", " ")
        if not text:
            return ""
        slug = "-".join(re.findall(r"[a-z0-9]+", text))
        return slug[:160]

    async def _task_workspace_artifact_acceptance_locator_readback(
        self,
        locator: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        path = str(locator.get("path") or locator.get("artifact_path") or "").strip()
        locator_id = str(locator.get("id") or "").strip()
        if not path or not locator_id:
            return None
        offset = self._coerce_non_negative_int(locator.get("byte_offset"))
        byte_end = self._coerce_non_negative_int(locator.get("byte_end"))
        max_bytes = min(_VERIFIER_PROMPT_ITEM_CHARS, 2400)
        if byte_end > offset:
            max_bytes = min(max(byte_end - offset, 800), max_bytes)
        if offset > 0 or byte_end > 0:
            try:
                read_result = await self.task_workspace.read_file(path, max_bytes=max_bytes, offset=offset)
            except Exception as error:
                return {
                    "kind": "acceptance_locator_readback",
                    "path": path,
                    "status": "failed",
                    "source_evidence_id": locator_id,
                    "offset": offset,
                    "line_start": locator.get("line_start"),
                    "line_end": locator.get("line_end"),
                    "error": {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    },
                }
            return {
                "kind": "acceptance_locator_readback",
                "path": str(read_result.get("path") or path),
                "status": "read",
                "source_evidence_id": locator_id,
                "offset": int(read_result.get("offset") or offset),
                "line_start": locator.get("line_start"),
                "line_end": locator.get("line_end"),
                "truncated": bool(read_result.get("truncated")),
                "query": locator.get("heading") or locator.get("anchor_text") or locator.get("claim"),
                "content": self._truncate_prompt_text(str(read_result.get("content") or ""), max_bytes),
            }
        for query in self._acceptance_locator_search_queries(locator):
            match = await self._task_workspace_artifact_search_readback(
                path,
                query,
                max_file_bytes=5_000_000,
            )
            if match is not None:
                match["kind"] = "acceptance_locator_search"
                match["source_evidence_id"] = locator_id
                return match
        return {
            "kind": "acceptance_locator_readback",
            "path": path,
            "status": "failed",
            "source_evidence_id": locator_id,
            "error": {
                "type": "AcceptanceLocatorUnavailable",
                "message": "Acceptance locator did not include a byte offset and no anchor search matched.",
            },
        }

    @classmethod
    def _acceptance_locator_search_queries(cls, locator: Mapping[str, Any]) -> list[str]:
        queries: list[str] = []
        for key in ("heading", "anchor_text", "claim", "topic", "criterion_id"):
            text = str(locator.get(key) or "").strip()
            if text and len(text) <= 160 and text not in queries:
                queries.append(text)
        return queries[:4]

    def _should_attempt_evidence_binding_repair(self, grounding_guard: Mapping[str, Any]) -> bool:
        if not isinstance(grounding_guard, Mapping):
            return False
        if not grounding_guard.get("blocking_count"):
            return False
        blocking_codes = {
            str(item.get("code") or "")
            for item in grounding_guard.get("diagnostics", [])
            if isinstance(item, Mapping) and item.get("blocking") is True
        }
        if not blocking_codes:
            return False
        binding_codes = {
            "evidence_ledger.invalid_evidence_id",
            "evidence_ledger.ambiguous_evidence_alias",
            "evidence_ledger.missing_evidence_id",
            "evidence_ledger.ref_only_item_used_as_content_support",
        }
        if not blocking_codes.issubset(binding_codes):
            return False
        return True

    def _can_attempt_model_evidence_binding_repair(self) -> bool:
        try:
            count = int(self.diagnostics.get("evidence_binding_repair_attempt_count") or 0)
        except (TypeError, ValueError):
            count = 0
        return count < 2

    @staticmethod
    def _evidence_body_prompt_value(value: Any) -> Any:
        """Recover a complete structured Action body before hot projection.

        Action artifacts cross the canonical evidence boundary as text so they
        can also be indexed and read through TaskContext.  When that text is a
        complete JSON container, treating it as undifferentiated prose makes a
        character cut hide later sibling records even though the canonical
        body is complete.  Recovering only valid JSON preserves structure for
        the existing bounded projector; genuinely cut JSON remains text and
        therefore retains its visible truncation boundary.
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped.startswith(("{", "[")):
            return value
        try:
            structured = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
        if isinstance(structured, Mapping) or (
            isinstance(structured, Sequence)
            and not isinstance(structured, str | bytes | bytearray)
        ):
            return structured
        return value

    @classmethod
    def _preserve_required_structured_evidence_bodies(
        cls,
        items: Sequence[Mapping[str, Any]],
        *,
        required_evidence_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Keep complete JSON siblings for evidence the candidate actually used.

        The cumulative verifier ledger deliberately keeps a small prose body
        budget per item.  Applying that head/tail budget to a complete JSON
        Action body can hide later sibling records before the model projection
        has a chance to preserve their structure.  Only candidate-required
        evidence receives this structured projection; unrelated ledger bodies
        retain the ordinary bounded text budget.
        """
        projected: list[dict[str, Any]] = []
        for raw_item in items:
            item = dict(raw_item)
            identities = {
                str(item.get(field) or "").strip()
                for field in (
                    "id",
                    "evidence_id",
                    "reference_id",
                    "locator_id",
                )
                if str(item.get(field) or "").strip()
            }
            aliases = item.get("aliases")
            if isinstance(aliases, Sequence) and not isinstance(
                aliases,
                str | bytes | bytearray,
            ):
                identities.update(
                    str(alias or "").strip()
                    for alias in aliases
                    if str(alias or "").strip()
                )
            if identities.isdisjoint(required_evidence_ids):
                projected.append(item)
                continue
            body = item.get("body")
            structured = cls._evidence_body_prompt_value(body)
            if not isinstance(structured, Mapping) and not (
                isinstance(structured, Sequence)
                and not isinstance(structured, str | bytes | bytearray)
            ):
                projected.append(item)
                continue
            item.pop("body", None)
            item["preview"] = DataFormatter.sanitize(structured)
            projected.append(item)
        return projected

    @classmethod
    def _evidence_item_projection_quality(
        cls,
        item: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Rank duplicate projections of one immutable evidence identity."""
        value = item.get("body")
        if value in (None, "", [], {}):
            value = item.get("preview")
        structured = cls._evidence_body_prompt_value(value)
        if isinstance(structured, Mapping) or (
            isinstance(structured, Sequence)
            and not isinstance(structured, str | bytes | bytearray)
        ):
            return (
                3,
                len(
                    json.dumps(
                        DataFormatter.sanitize(structured),
                        ensure_ascii=False,
                        default=str,
                    )
                ),
            )
        if isinstance(value, str) and value:
            if "[...body truncated for evidence ledger view...]" in value:
                return (1, len(value))
            return (2, len(value))
        return (0, 0)

    def _canonical_structured_evidence_ledger_for_model(
        self,
        evidence_ledger: Mapping[str, Any],
        *,
        max_hydrated_items: int = 8,
    ) -> dict[str, Any]:
        """Rejoin lossy JSON views to their task-canonical evidence target.

        Canonical evidence stays owned by TaskReferenceCatalog; model-facing
        ledgers receive a bounded structured view so sibling fields are not
        lost to an earlier head/tail text cut. This applies equally to
        TaskBoard card execution and terminal verification.
        """
        hydrated = dict(DataFormatter.sanitize(evidence_ledger))
        raw_items = hydrated.get("items")
        if not isinstance(raw_items, Sequence) or isinstance(
            raw_items,
            str | bytes | bytearray,
        ):
            return hydrated
        output_items: list[Any] = []
        hydrated_count = 0
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                output_items.append(raw_item)
                continue
            item = dict(raw_item)
            body = item.get("body")
            lossy = item.get("body_truncated_for_view") is True or (
                isinstance(body, str)
                and "[...body truncated for evidence ledger view...]" in body
            )
            reference_id = str(item.get("reference_id") or "").strip()
            if (
                not lossy
                or not reference_id
                or hydrated_count >= max_hydrated_items
            ):
                output_items.append(item)
                continue
            try:
                resolved = self._task_references().resolve(reference_id)
            except (KeyError, ValueError):
                output_items.append(item)
                continue
            target = resolved.get("target")
            if not isinstance(target, Mapping):
                output_items.append(item)
                continue
            canonical_body: Any = None
            for field in ("body", "content", "text", "snippet", "preview"):
                value = target.get(field)
                if value not in (None, "", [], {}):
                    canonical_body = value
                    break
            structured = self._evidence_body_prompt_value(canonical_body)
            if not isinstance(structured, Mapping) and not (
                isinstance(structured, Sequence)
                and not isinstance(structured, str | bytes | bytearray)
            ):
                output_items.append(item)
                continue
            item.pop("body", None)
            item.pop("body_truncated_for_view", None)
            item.pop("body_chars", None)
            item["preview"] = DataFormatter.sanitize(structured)
            output_items.append(item)
            hydrated_count += 1
        hydrated["items"] = output_items
        return hydrated

    @classmethod
    def _terminal_evidence_projection_for_observers(
        cls,
        model_evidence_ledger: Mapping[str, Any],
        verification: Mapping[str, Any],
        *,
        max_items: int = 24,
    ) -> dict[str, Any]:
        """Publish the exact accepted evidence frontier for cold observers.

        The terminal verifier already receives a bounded, identity-safe ledger.
        Re-project only references that its accepted criterion/claim checks used;
        this keeps complete structured Action siblings available to experiments
        and DevTools without putting a second evidence owner in diagnostics.
        """
        if verification.get("is_complete") is not True:
            return {}

        used_reference_ids: list[str] = []

        def collect(value: Any) -> None:
            for reference_id in cls._normalize_string_list(value):
                if reference_id not in used_reference_ids:
                    used_reference_ids.append(reference_id)

        for field in ("criterion_checks", "material_claim_checks"):
            checks = verification.get(field)
            if not isinstance(checks, Sequence) or isinstance(
                checks,
                str | bytes | bytearray,
            ):
                continue
            for check in checks:
                if isinstance(check, Mapping):
                    collect(check.get("evidence_ids"))
        replan_signal = verification.get("replan_signal")
        if isinstance(replan_signal, Mapping):
            collect(replan_signal.get("evidence_refs"))
        if not used_reference_ids:
            return {}

        raw_items = model_evidence_ledger.get("items")
        if not isinstance(raw_items, Sequence) or isinstance(
            raw_items,
            str | bytes | bytearray,
        ):
            return {}
        items_by_reference = {
            str(item.get("reference_id") or "").strip(): item
            for item in raw_items
            if isinstance(item, Mapping)
            and str(item.get("reference_id") or "").strip()
        }
        retained_ids: list[str] = []
        projected_items: list[dict[str, Any]] = []
        for reference_id in used_reference_ids:
            item = items_by_reference.get(reference_id)
            if item is None:
                continue
            projected = {
                key: DataFormatter.sanitize(item.get(key))
                for key in (
                    "reference_id",
                    "kind",
                    "status",
                    "source_role",
                    "action_id",
                    "owner",
                    "locator",
                    "source_id",
                    "source_revision",
                    "source_ref",
                    "content_version",
                    "path",
                    "body_state",
                    "body_preview",
                )
                if item.get(key) not in (None, "", [], {})
            }
            retained_ids.append(reference_id)
            projected_items.append(projected)
            if len(projected_items) >= max(1, max_items):
                break
        if not projected_items:
            return {}
        return {
            "schema_version": "agent_task_terminal_evidence_projection/v1",
            "verification_state": "accepted",
            "reference_ids": retained_ids,
            "items": projected_items,
            "item_count": len(projected_items),
            "omitted_used_reference_ids": [
                reference_id
                for reference_id in used_reference_ids
                if reference_id not in retained_ids
            ],
        }

    @classmethod
    def _evidence_binding_repair_candidate_refs(
        cls,
        evidence_ledger: Mapping[str, Any],
        *,
        max_items: int = 80,
        offered_reference_ids: set[str] | None = None,
        preferred_reference_ids: set[str] | None = None,
        required_reference_ids: set[str] | None = None,
        include_host_identity: bool = False,
    ) -> list[dict[str, Any]]:
        """Project stable evidence choices with only facts needed for binding.

        The model selects one host-issued reference_id. Raw ledger ids, action-call
        ids, request-local aliases, and full provenance remain host-side so the
        model cannot be asked to reproduce several opaque identity fields.
        """
        if not isinstance(evidence_ledger, Mapping):
            return []
        raw_items: list[Any] = []
        for key in ("items", "overflow_item_refs"):
            value = evidence_ledger.get(key)
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                raw_items.extend(value)
        preferred = preferred_reference_ids or set()
        required = required_reference_ids or set()
        if preferred or required:
            # A downstream card must retain the exact refs that its dependency
            # structurally cited even when the cumulative ledger is larger
            # than the model-visible identity budget.  This is a projection
            # priority only; canonical identity and validation remain host-owned.
            raw_items.sort(
                key=lambda item: (
                    0
                    if isinstance(item, Mapping)
                    and str(item.get("reference_id") or "").strip() in preferred
                    else 1
                    if isinstance(item, Mapping)
                    and str(item.get("reference_id") or "").strip() in required
                    else 2
                )
            )
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        expanded_bounded_readbacks = 0
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            reference_id = str(item.get("reference_id") or "").strip()
            if not reference_id or reference_id in seen:
                continue
            if (
                offered_reference_ids is not None
                and reference_id not in offered_reference_ids
            ):
                continue
            seen.add(reference_id)
            candidate: dict[str, Any] = {
                "reference_id": reference_id,
                "kind": str(item.get("kind") or ""),
                "status": str(item.get("status") or ""),
                "body_state": str(item.get("body_state") or ""),
            }
            for field in (
                "action_id",
                "path",
                "url",
                "source_ref",
                "source_revision",
                "query",
                "range_start",
                "query_match",
                "criterion_id",
                "claim",
                "topic",
                "heading",
                "anchor_text",
                "line_start",
                "line_end",
                "byte_offset",
                "byte_end",
            ):
                value = item.get(field)
                if value not in (None, "", [], {}):
                    candidate[field] = DataFormatter.sanitize(value)
            if include_host_identity:
                for field in (
                    "execution_block_id",
                    "block_id",
                    "source_id",
                    "binding_id",
                ):
                    value = item.get(field)
                    if value not in (None, "", [], {}):
                        candidate[field] = DataFormatter.sanitize(value)
            provenance = item.get("provenance")
            if isinstance(provenance, Mapping):
                for field in (
                    "source_revision",
                    "source_ref",
                    "path",
                ):
                    if candidate.get(field) not in (None, "", [], {}):
                        continue
                    value = provenance.get(field)
                    if value not in (None, "", [], {}):
                        candidate[field] = DataFormatter.sanitize(value)
                if include_host_identity:
                    for field in (
                        "execution_block_id",
                        "block_id",
                        "source_id",
                        "binding_id",
                    ):
                        if candidate.get(field) not in (None, "", [], {}):
                            continue
                        value = provenance.get(field)
                        if value not in (None, "", [], {}):
                            candidate[field] = DataFormatter.sanitize(value)
            input_preview = item.get("input_preview")
            if input_preview not in (None, "", [], {}):
                candidate["input_preview"] = cls._compact_verifier_prompt_value(input_preview, max_chars=600)
            body = item.get("body")
            if body in (None, "", [], {}):
                body = item.get("preview")
            if body not in (None, "", [], {}):
                body_value = cls._evidence_body_prompt_value(body)
                preserve_complete_readback = False
                if (
                    str(item.get("kind") or "")
                    == "taskboard_action_artifact.readback"
                    and str(item.get("body_state") or "")
                    in {"full", "bounded", "truncated"}
                    and expanded_bounded_readbacks
                    < _TASKBOARD_DEPENDENCY_READBACK_MAX_REFS
                    and isinstance(body_value, Mapping | Sequence)
                    and not isinstance(body_value, str | bytes | bytearray)
                ):
                    serialized_body = json.dumps(
                        DataFormatter.sanitize(body_value),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )
                    preserve_complete_readback = (
                        len(serialized_body)
                        <= _TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS
                    )
                if preserve_complete_readback:
                    candidate["body_preview"] = DataFormatter.sanitize(body_value)
                    expanded_bounded_readbacks += 1
                else:
                    candidate["body_preview"] = cls._compact_verifier_prompt_value(
                        body_value,
                        max_chars=1200,
                    )
            candidates.append(DataFormatter.sanitize(candidate))
            if len(candidates) >= max_items:
                break
        return candidates

    def _model_evidence_ledger_projection(
        self,
        evidence_ledger: Mapping[str, Any],
        *,
        max_items: int = 80,
        offered_reference_ids: set[str] | None = None,
        preferred_reference_ids: set[str] | None = None,
        required_reference_ids: set[str] | None = None,
        include_host_identity: bool = False,
    ) -> dict[str, Any]:
        """Expose one host-issued identity per model-visible evidence item."""
        canonical_evidence_ledger = self._canonical_structured_evidence_ledger_for_model(
            evidence_ledger
        )
        candidates = self._evidence_binding_repair_candidate_refs(
            canonical_evidence_ledger,
            max_items=max_items,
            offered_reference_ids=offered_reference_ids,
            preferred_reference_ids=preferred_reference_ids,
            required_reference_ids=required_reference_ids,
            include_host_identity=include_host_identity,
        )
        eligible_reference_ids: set[str] = set()
        for key in ("items", "overflow_item_refs"):
            value = (
                canonical_evidence_ledger.get(key)
                if isinstance(canonical_evidence_ledger, Mapping)
                else None
            )
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    if not isinstance(item, Mapping):
                        continue
                    reference_id = str(item.get("reference_id") or "").strip()
                    if not reference_id:
                        continue
                    if (
                        offered_reference_ids is not None
                        and reference_id not in offered_reference_ids
                    ):
                        continue
                    eligible_reference_ids.add(reference_id)
        return {
            "items": candidates,
            "item_count": len(candidates),
            "omitted_count": max(len(eligible_reference_ids) - len(candidates), 0),
            "selection_policy": (
                "Use only an exact offered items[].reference_id in evidence_use.evidence_ids. "
                "reference_id is the only model-selection identity; host-owned canonical ids and aliases are omitted."
            ),
        }

    async def _request_evidence_binding_repair(
        self,
        grounding_guard: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any],
        *,
        language_policy: Mapping[str, Any],
        offered_reference_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        candidates = self._evidence_binding_repair_candidate_refs(
            evidence_ledger,
            offered_reference_ids=offered_reference_ids,
        )
        # No selectable identity means no model repair is possible. Returning
        # no repair preserves the caller's existing evidence errors.
        if not candidates:
            return []
        request = self.agent.create_temp_request()
        self._apply_language_policy_to_request(request, language_policy)
        request.input(
            {
                "task_id": self.id,
                "blocking_evidence_use_diagnostics": self._evidence_binding_repair_diagnostics(grounding_guard),
                "current_evidence_use": grounding_guard.get("normalized_evidence_use", []),
                "available_evidence_refs": candidates,
                "grounding_rules": evidence_ledger.get("grounding_rules", {}) if isinstance(evidence_ledger, Mapping) else {},
            }
        )
        request.instruct(
            "Repair only the structured evidence_use bindings that failed deterministic id binding. "
            "Do not rewrite, summarize, or regenerate the candidate final result. "
            "Choose evidence_ids only from available_evidence_refs.reference_id after matching each claim against the "
            "candidate's bounded input_preview, body_preview, action_id, and locator facts. "
            "If no listed evidence item can support a claim under the rules, return that claim with an empty evidence_ids "
            "list so the host can block precisely. Preserve each claim text and support_type."
        )
        request.output(
            {
                "evidence_use": (
                    [
                        {
                            "claim_index": (int, "Index of the claim in current_evidence_use when available", False),
                            "claim": (str, "Original claim text", True),
                            "evidence_ids": ([str], "Corrected stable reference_id values from available_evidence_refs", True),
                            "support_type": (
                                str,
                                "content, unavailability, or ref_pointer; keep the original type unless it was structurally wrong",
                                True,
                            ),
                        }
                    ],
                    "Only corrected evidence_use entries for claims with binding diagnostics",
                    True,
                ),
                "repair_summary": (
                    str,
                    "Short summary of the evidence binding repair result; no candidate rewrite.",
                    False,
                ),
                "progress_message": (
                    str,
                    "One safe human-readable repair progress sentence.",
                    False,
                ),
            },
            format="json",
        )
        repaired = await self._await_task_request(request.async_get_data(), stage="evidence_binding_repair")
        if not isinstance(repaired, Mapping):
            return []
        await self._emit_process_progress_from_output(
            repaired,
            stage="evidence_binding_repair",
        )
        evidence_use = repaired.get("evidence_use")
        if not isinstance(evidence_use, Sequence) or isinstance(evidence_use, str | bytes | bytearray):
            return []
        return [dict(DataFormatter.sanitize(item)) for item in evidence_use if isinstance(item, Mapping)]

    @classmethod
    def _deterministic_evidence_binding_repair(
        cls,
        grounding_guard: Mapping[str, Any],
        evidence_ledger: Mapping[str, Any] | None = None,
        *,
        offered_reference_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        current = grounding_guard.get("normalized_evidence_use", [])
        if not isinstance(current, Sequence) or isinstance(current, str | bytes | bytearray):
            return []
        current_items = [dict(item) for item in current if isinstance(item, Mapping)]
        if not current_items:
            return []
        available_ref_records = cls._evidence_binding_available_ref_records(
            grounding_guard.get("available_evidence_refs", [])
        )
        if evidence_ledger is not None:
            available_ref_records = cls._merge_evidence_binding_ref_records(
                available_ref_records,
                cls._evidence_binding_available_ref_records_from_ledger(evidence_ledger),
            )
        if offered_reference_ids is not None:
            available_ref_records = [
                ref
                for ref in available_ref_records
                if str(ref.get("reference_id") or ref.get("id") or "").strip()
                in offered_reference_ids
            ]
        available_refs = cls._evidence_binding_available_ref_index(available_ref_records)
        diagnostics = cls._evidence_binding_repair_diagnostics(grounding_guard)
        repaired: list[dict[str, Any]] = []
        seen_indexes: set[int] = set()
        raw_guard_diagnostics = [
            dict(item)
            for item in grounding_guard.get("diagnostics", [])
            if isinstance(item, Mapping) and item.get("blocking") is True
        ]
        blocking_diagnostics_by_claim: dict[int, list[dict[str, Any]]] = {}
        for diagnostic in raw_guard_diagnostics:
            try:
                diagnostic_index = int(diagnostic.get("index", -1))
            except (TypeError, ValueError):
                continue
            if diagnostic_index >= 0:
                blocking_diagnostics_by_claim.setdefault(
                    diagnostic_index,
                    [],
                ).append(diagnostic)
        support_incompatibility_codes = {
            "evidence_ledger.unavailable_item_used_as_positive_support",
            "evidence_ledger.ok_item_used_as_unavailability_support",
            "evidence_ledger.ref_only_item_used_as_content_support",
        }
        binding_repair_codes = {
            "evidence_ledger.invalid_evidence_id",
            "evidence_ledger.ambiguous_evidence_alias",
            "evidence_ledger.missing_evidence_id",
            "evidence_ledger.ref_only_item_used_as_content_support",
        }
        if evidence_ledger is not None:
            for claim_index, item in enumerate(current_items):
                claim_blocking_diagnostics = blocking_diagnostics_by_claim.get(
                    claim_index,
                    [],
                )
                if not claim_blocking_diagnostics or any(
                    str(diagnostic.get("code") or "")
                    not in support_incompatibility_codes
                    for diagnostic in claim_blocking_diagnostics
                ):
                    continue
                current_ids = cls._normalize_string_list(item.get("evidence_ids"))
                if not current_ids:
                    continue
                incompatible_ids: set[str] = set()
                for diagnostic in claim_blocking_diagnostics:
                    evidence_id = str(diagnostic.get("evidence_id") or "").strip()
                    if evidence_id in current_ids:
                        incompatible_ids.add(evidence_id)
                    resolved_ids = cls._normalize_string_list(
                        available_refs.get(evidence_id, [])
                    )
                    if len(resolved_ids) == 1:
                        incompatible_ids.add(resolved_ids[0])
                retained_ids = [
                    evidence_id
                    for evidence_id in current_ids
                    if evidence_id not in incompatible_ids
                ]
                if not retained_ids or len(retained_ids) == len(current_ids):
                    continue
                candidate = DataFormatter.sanitize(
                    {
                        "claim_index": claim_index,
                        "claim": item.get("claim", ""),
                        "evidence_ids": retained_ids,
                        "support_type": item.get("support_type", ""),
                    }
                )
                candidate_guard = validate_evidence_use([candidate], evidence_ledger)
                if candidate_guard.get("valid") is not True:
                    continue
                repaired.append(candidate)
                seen_indexes.add(claim_index)
        for diagnostic in diagnostics:
            raw_index = diagnostic.get("claim_index")
            try:
                claim_index = int(raw_index) if raw_index is not None else -1
            except (TypeError, ValueError):
                claim_index = -1
            if claim_index < 0 or claim_index >= len(current_items) or claim_index in seen_indexes:
                continue
            claim_blocking_diagnostics = blocking_diagnostics_by_claim.get(
                claim_index,
                [],
            )
            if not claim_blocking_diagnostics or any(
                str(item.get("code") or "") not in binding_repair_codes
                for item in claim_blocking_diagnostics
            ):
                continue
            item = current_items[claim_index]
            candidate_ids = cls._deterministic_evidence_id_candidates(
                diagnostic,
                available_refs,
                available_ref_records,
            )
            if len(candidate_ids) != 1:
                continue
            support_type = cls._deterministic_repaired_support_type(
                item,
                diagnostic,
                candidate_ids,
                available_ref_records,
            )
            repaired.append(
                DataFormatter.sanitize(
                    {
                        "claim_index": claim_index,
                        "claim": item.get("claim", diagnostic.get("claim", "")),
                        "evidence_ids": candidate_ids,
                        "support_type": support_type,
                    }
                )
            )
            seen_indexes.add(claim_index)
        return repaired

    @staticmethod
    def _evidence_binding_available_ref_records(value: Any) -> list[dict[str, Any]]:
        refs = value if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray) else []
        return [dict(DataFormatter.sanitize(ref)) for ref in refs if isinstance(ref, Mapping)]

    @staticmethod
    def _evidence_binding_available_ref_records_from_ledger(value: Any) -> list[dict[str, Any]]:
        ledger = value if isinstance(value, Mapping) and isinstance(value.get("items"), Sequence) else {}
        if not ledger and value not in (None, "", [], {}):
            ledger = evidence_ledger_view(value)
        raw_items = ledger.get("items") if isinstance(ledger, Mapping) else ()
        items = raw_items if isinstance(raw_items, Sequence) and not isinstance(raw_items, str | bytes | bytearray) else ()
        return [dict(DataFormatter.sanitize(item)) for item in items if isinstance(item, Mapping)]

    @staticmethod
    def _merge_evidence_binding_ref_records(
        refs: Sequence[Mapping[str, Any]],
        ledger_refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        merged_by_id: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        def merge(ref: Mapping[str, Any]) -> None:
            evidence_id = str(ref.get("reference_id") or ref.get("id") or "").strip()
            if not evidence_id:
                return
            if evidence_id not in merged_by_id:
                merged_by_id[evidence_id] = {}
                order.append(evidence_id)
            existing = merged_by_id[evidence_id]
            aliases: list[str] = []
            for source in (existing.get("aliases"), ref.get("aliases")):
                if isinstance(source, Sequence) and not isinstance(source, str | bytes | bytearray):
                    for alias in source:
                        text = str(alias or "").strip()
                        if text and text not in aliases:
                            aliases.append(text)
            existing.update(dict(ref))
            if aliases:
                existing["aliases"] = aliases[:24]

        for ref in ledger_refs:
            merge(ref)
        for ref in refs:
            merge(ref)
        return [merged_by_id[evidence_id] for evidence_id in order]

    @staticmethod
    def _evidence_binding_available_ref_index(value: Any) -> dict[str, list[str]]:
        refs = value if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray) else []
        index: dict[str, list[str]] = {}
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("reference_id") or ref.get("id") or "").strip()
            if not evidence_id:
                continue
            aliases: set[str] = {evidence_id}
            for field in (
                "cite_as",
                "path",
                "record_id",
                "source_url",
                "selected_url",
                "requested_url",
                "canonical_url",
                "url",
                "href",
                "artifact_id",
                "action_call_id",
            ):
                raw = ref.get(field)
                if raw not in (None, "", [], {}):
                    aliases.add(str(raw).strip())
            raw_aliases = ref.get("aliases")
            if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
                aliases.update(str(alias).strip() for alias in raw_aliases if str(alias or "").strip())
            for alias in aliases:
                if alias:
                    index.setdefault(alias, []).append(evidence_id)
        return index

    @staticmethod
    def _deterministic_repaired_support_type(
        item: Mapping[str, Any],
        diagnostic: Mapping[str, Any],
        candidate_ids: Sequence[str],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> str:
        support_type = str(item.get("support_type", diagnostic.get("support_type", "")) or "").strip()
        if support_type != "unavailability":
            return support_type
        refs_by_id = {
            str(ref.get("reference_id") or ref.get("id") or "").strip(): ref
            for ref in available_refs
            if isinstance(ref, Mapping) and str(ref.get("reference_id") or ref.get("id") or "").strip()
        }
        candidate_refs = [refs_by_id.get(str(candidate_id or "").strip()) for candidate_id in candidate_ids]
        if not candidate_refs or any(ref is None for ref in candidate_refs):
            return support_type
        content_backed = all(
            str(ref.get("status") or "").strip().lower() == "ok"
            and str(ref.get("body_state") or "").strip().lower() in {"full", "bounded", "truncated"}
            for ref in candidate_refs
            if isinstance(ref, Mapping)
        )
        return "content" if content_backed else support_type

    @classmethod
    def _deterministic_evidence_id_candidates(
        cls,
        diagnostic: Mapping[str, Any],
        available_ref_index: Mapping[str, list[str]],
        available_refs: Sequence[Mapping[str, Any]] = (),
    ) -> list[str]:
        raw_candidates = diagnostic.get("candidates")
        if isinstance(raw_candidates, Sequence) and not isinstance(raw_candidates, str | bytes | bytearray):
            candidates = [str(candidate).strip() for candidate in raw_candidates if str(candidate or "").strip()]
            unique = sorted(set(candidates))
            if len(unique) == 1:
                return unique
        evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        unique_matches: list[str] = []
        requires_content_replacement = False
        if evidence_id:
            matches = [
                str(item).strip()
                for item in available_ref_index.get(evidence_id, [])
                if str(item or "").strip()
            ]
            unique_matches = sorted(set(matches))
            requires_content_replacement = (
                str(diagnostic.get("code") or "") == "evidence_ledger.ref_only_item_used_as_content_support"
                and str(diagnostic.get("support_type") or "").strip().lower() == "content"
            )
            if len(unique_matches) == 1 and not requires_content_replacement:
                return unique_matches
            coverage_matches = cls._deterministic_acceptance_coverage_candidates(diagnostic, available_refs)
            if len(coverage_matches) == 1:
                return coverage_matches
            artifact_ref_matches = cls._deterministic_artifact_ref_candidates(diagnostic, available_refs)
            if len(artifact_ref_matches) == 1:
                return artifact_ref_matches
            action_result_matches = cls._deterministic_action_result_candidates(diagnostic, available_refs)
            if len(action_result_matches) == 1:
                return action_result_matches
        coverage_matches = cls._deterministic_acceptance_coverage_candidates(diagnostic, available_refs)
        if len(coverage_matches) == 1:
            return coverage_matches
        body_text_matches = cls._deterministic_body_text_candidates(diagnostic, available_refs)
        if len(body_text_matches) == 1:
            return body_text_matches
        if evidence_id:
            readback_matches = cls._deterministic_content_readback_candidates(diagnostic, available_refs)
            if len(readback_matches) == 1:
                return readback_matches
        if len(unique_matches) == 1:
            ref_by_id = {
                str(ref.get("id") or "").strip(): ref
                for ref in available_refs
                if isinstance(ref, Mapping) and str(ref.get("id") or "").strip()
            }
            match = ref_by_id.get(unique_matches[0], {})
            if not requires_content_replacement or str(match.get("body_state") or "").strip().lower() != "ref_only":
                return unique_matches
        return []

    @staticmethod
    def _deterministic_artifact_ref_candidates(
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        if not evidence_id:
            return []
        raw_candidates = diagnostic.get("candidates")
        candidates = {
            str(candidate).strip()
            for candidate in raw_candidates
            if str(candidate or "").strip()
        } if isinstance(raw_candidates, Sequence) and not isinstance(raw_candidates, str | bytes | bytearray) else set()
        matches: list[str] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            ref_id = str(ref.get("id") or "").strip()
            if not ref_id or candidates and ref_id not in candidates:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            kind = str(ref.get("kind") or "").strip().lower()
            artifact_id = str(ref.get("artifact_id") or "").strip()
            if status != "ok" or body_state not in {"full", "bounded", "truncated"}:
                continue
            if kind == "artifact_ref" and (artifact_id == evidence_id or evidence_id in ref_id):
                matches.append(ref_id)
        return sorted(set(matches))

    @staticmethod
    def _deterministic_action_result_candidates(
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        support_type = str(diagnostic.get("support_type") or "").strip().lower()
        if support_type not in {"content", "unavailability"}:
            return []
        text = " ".join(str(diagnostic.get(key) or "") for key in ("claim", "evidence_id")).lower()
        action_refs: list[Mapping[str, Any]] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("reference_id") or ref.get("id") or "").strip()
            if not evidence_id:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            kind = str(ref.get("kind") or "").strip().lower()
            if kind != "agent_task.action.result" or status != "ok":
                continue
            if body_state not in {"full", "bounded", "truncated"}:
                continue
            action_refs.append(ref)
        if not action_refs:
            return []
        diagnostic_evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        alias_matches: list[str] = []
        action_matches: list[str] = []
        for ref in action_refs:
            evidence_id = str(ref.get("reference_id") or ref.get("id") or "").strip()
            aliases = {
                evidence_id,
                str(ref.get("cite_as") or "").strip(),
                str(ref.get("action_id") or "").strip(),
                str(ref.get("action_call_id") or "").strip(),
            }
            raw_aliases = ref.get("aliases")
            if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
                aliases.update(str(alias or "").strip() for alias in raw_aliases)
            aliases.discard("")
            if diagnostic_evidence_id and diagnostic_evidence_id in aliases:
                alias_matches.append(evidence_id)
            action_id = str(ref.get("action_id") or "").strip().lower()
            action_tokens = {action_id, action_id.replace("_", " ")} if action_id else set()
            if action_tokens and any(token and token in text for token in action_tokens):
                action_matches.append(evidence_id)
        unique_alias_matches = sorted(set(alias_matches))
        if len(unique_alias_matches) == 1:
            return unique_alias_matches
        unique_action_matches = sorted(set(action_matches))
        if len(unique_action_matches) == 1:
            return unique_action_matches
        unique_action_refs = sorted({str(ref.get("id") or "").strip() for ref in action_refs if ref.get("id")})
        if len(unique_action_refs) != 1:
            return []
        evidence_id = str(diagnostic.get("evidence_id") or "").strip().lower()
        if evidence_id.startswith(("action result", "action_result", "action-result")):
            return unique_action_refs
        if support_type == "unavailability":
            return unique_action_refs
        return []

    @staticmethod
    def _deterministic_acceptance_coverage_candidates(
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        if str(diagnostic.get("support_type") or "").strip().lower() != "content":
            return []
        text = " ".join(str(diagnostic.get(key) or "") for key in ("claim", "evidence_id")).lower()
        raw_candidates = diagnostic.get("candidates")
        candidate_ids = {
            str(candidate).strip()
            for candidate in raw_candidates
            if str(candidate or "").strip()
        } if isinstance(raw_candidates, Sequence) and not isinstance(raw_candidates, str | bytes | bytearray) else set()
        matches: list[str] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("reference_id") or ref.get("id") or "").strip()
            if not evidence_id or (candidate_ids and evidence_id not in candidate_ids):
                continue
            if str(ref.get("kind") or "").strip().lower() != "task_workspace_artifact.acceptance_coverage":
                continue
            if str(ref.get("status") or "").strip().lower() != "ok":
                continue
            if str(ref.get("body_state") or "").strip().lower() not in {"full", "bounded", "truncated"}:
                continue
            path = str(ref.get("path") or "").strip().lower()
            basename = PurePosixPath(path.replace("\\", "/")).name if path else ""
            aliases = {path, basename, evidence_id.lower()}
            raw_aliases = ref.get("aliases")
            if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
                aliases.update(str(alias or "").strip().lower() for alias in raw_aliases)
            aliases.discard("")
            if any(alias and alias in text for alias in aliases):
                matches.append(evidence_id)
        return sorted(set(matches))

    @classmethod
    def _deterministic_body_text_candidates(
        cls,
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        support_type = str(diagnostic.get("support_type") or "").strip().lower()
        if support_type not in {"content", "unavailability"}:
            return []
        evidence_id_text = str(diagnostic.get("evidence_id") or "").strip()
        query_specs = [(query, False) for query in cls._evidence_binding_body_match_queries(evidence_id_text)]
        if not evidence_id_text or cls._evidence_binding_id_looks_like_task_workspace_locator(evidence_id_text):
            allow_partial_claim_match = not evidence_id_text
            query_specs.extend(
                (query, allow_partial_claim_match)
                for query in cls._evidence_binding_body_match_queries(str(diagnostic.get("claim") or ""))
            )
        query_specs = cls._dedupe_evidence_binding_query_specs(query_specs)
        if not query_specs:
            return []
        matches: list[Mapping[str, Any]] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            ref_id = str(ref.get("id") or "").strip()
            if not ref_id:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            status_supported = status == "ok" or (support_type == "unavailability" and status in {"failed", "empty"})
            if not status_supported or body_state not in {"full", "bounded", "truncated"}:
                continue
            body = cls._evidence_binding_ref_body(ref)
            if not body:
                continue
            if any(
                cls._evidence_binding_body_query_matches(query, body, allow_partial=allow_partial)
                for query, allow_partial in query_specs
            ):
                matches.append(ref)
        evidence_id_text = str(diagnostic.get("evidence_id") or "").strip()
        if cls._evidence_binding_id_looks_like_file_locator(evidence_id_text):
            # A file/path/locator reference may bind only to a ref whose own
            # path/anchor agrees. A body-text coincidence in another file must never
            # bind -- not even when it is the single body match. Anchor-gate every
            # file-locator case so cross-file binding is impossible; if no ref agrees,
            # return nothing and let the path-aware readback tier (or repair) decide.
            preferred_file_match = cls._preferred_file_locator_body_text_candidate(diagnostic, matches)
            return [preferred_file_match] if preferred_file_match else []
        unique_matches = cls._unique_evidence_binding_ref_ids(matches)
        if len(unique_matches) <= 1:
            return unique_matches
        coalesced_task_workspace_match = cls._coalesced_task_workspace_body_text_candidate(matches)
        if coalesced_task_workspace_match:
            return [coalesced_task_workspace_match]
        return unique_matches

    @staticmethod
    def _evidence_binding_ref_body(ref: Mapping[str, Any]) -> str:
        for key in ("body", "content", "text", "snippet", "preview", "result", "output", "value"):
            value = ref.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, str):
                return value
            try:
                return json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, sort_keys=True, default=str)
            except Exception:
                return str(value)
        return ""

    @classmethod
    def _evidence_binding_body_match_queries(cls, value: str) -> list[str]:
        raw = value.strip()
        if not raw:
            return []
        queries = [raw]
        lowered = raw.lower()
        for prefix in ("search result:", "search:", "result:", "source:", "evidence:"):
            if lowered.startswith(prefix):
                stripped = raw[len(prefix) :].strip()
                if stripped and stripped not in queries:
                    queries.append(stripped)
        for separator in (":", "/", "#"):
            if separator in raw:
                tail = raw.rsplit(separator, 1)[-1].strip()
                if tail and tail not in queries:
                    queries.append(tail)
        return [query for query in queries if cls._evidence_binding_body_query_is_informative(query)]

    @classmethod
    def _evidence_binding_body_query_matches(cls, query: str, body: str, *, allow_partial: bool = False) -> bool:
        normalized_query = cls._normalize_evidence_binding_text(query)
        if len(normalized_query) < 12:
            return False
        normalized_body = cls._normalize_evidence_binding_text(body)
        if normalized_query in normalized_body:
            return True
        query_tokens = cls._evidence_binding_body_match_tokens(query)
        if len(query_tokens) < 4:
            return False
        body_tokens = set(cls._evidence_binding_body_match_tokens(body))
        if not body_tokens:
            return False
        if all(token in body_tokens for token in query_tokens):
            return True
        if not allow_partial:
            return False
        overlap_count = sum(1 for token in query_tokens if token in body_tokens)
        required_overlap = min(len(query_tokens), max(3, (len(query_tokens) * 3 + 4) // 5))
        return overlap_count >= required_overlap

    @classmethod
    def _evidence_binding_body_query_is_informative(cls, query: str) -> bool:
        return len(cls._evidence_binding_body_match_tokens(query)) >= 3

    @staticmethod
    def _dedupe_evidence_binding_queries(queries: Sequence[str]) -> list[str]:
        deduped: list[str] = []
        for query in queries:
            text = str(query or "").strip()
            if text and text not in deduped:
                deduped.append(text)
        return deduped

    @staticmethod
    def _dedupe_evidence_binding_query_specs(queries: Sequence[tuple[str, bool]]) -> list[tuple[str, bool]]:
        deduped: list[tuple[str, bool]] = []
        seen: set[tuple[str, bool]] = set()
        for query, allow_partial in queries:
            text = str(query or "").strip()
            key = (text, bool(allow_partial))
            if text and key not in seen:
                seen.add(key)
                deduped.append(key)
        return deduped

    @classmethod
    def _evidence_binding_id_looks_like_task_workspace_locator(cls, evidence_id: str) -> bool:
        text = str(evidence_id or "").strip().lower()
        return (
            not text
            or "task_workspace_artifact" in text
            or "artifact_locator" in text
            or "acceptance_locator" in text
            or "readback" in text
            or cls._evidence_binding_id_looks_like_file_locator(text)
        )

    @staticmethod
    def _evidence_binding_id_looks_like_file_locator(evidence_id: str) -> bool:
        text = str(evidence_id or "").strip().lower().replace("\\", "/")
        if not text:
            return False
        if re.search(r"(?:^|[/\s'\"`])[\w.-]+\.[a-z0-9]{1,12}(?:\b|[:#/'\"`])", text):
            return True
        locator_terms = (" line ", " lines ", " row ", " rows ", " table ", " section ")
        padded = f" {text} "
        return any(term in padded for term in locator_terms)

    @classmethod
    def _normalize_evidence_binding_text(cls, value: str) -> str:
        return " ".join(cls._evidence_binding_body_match_tokens(value))

    @staticmethod
    def _evidence_binding_body_match_tokens(value: str) -> list[str]:
        tokens = re.findall(r"[a-z0-9]+", value.lower().replace("_", " "))
        ignored = {
            "a",
            "an",
            "and",
            "artifact",
            "claim",
            "content",
            "evidence",
            "final",
            "id",
            "locator",
            "md",
            "read",
            "readback",
            "section",
            "source",
            "targeted",
            "the",
            "task_workspace",
        }
        seen: set[str] = set()
        result: list[str] = []
        for token in tokens:
            if token in ignored:
                continue
            if not token.isdigit() and len(token) < 2:
                continue
            if token not in seen:
                seen.add(token)
                result.append(token)
        return result

    @staticmethod
    def _unique_evidence_binding_ref_ids(refs: Sequence[Mapping[str, Any]]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for ref in refs:
            evidence_id = str(ref.get("id") or "").strip()
            if evidence_id and evidence_id not in seen:
                seen.add(evidence_id)
                ordered.append(evidence_id)
        return ordered

    @classmethod
    def _preferred_file_locator_body_text_candidate(
        cls,
        diagnostic: Mapping[str, Any],
        refs: Sequence[Mapping[str, Any]],
    ) -> str:
        evidence_id_text = str(diagnostic.get("evidence_id") or "").strip()
        if not cls._evidence_binding_id_looks_like_file_locator(evidence_id_text):
            return ""
        locator_text = evidence_id_text.lower().replace("\\", "/")
        locator_matches = [
            ref
            for ref in refs
            if cls._evidence_binding_ref_matches_locator_text(ref, locator_text)
        ]
        unique_locator_matches = cls._unique_evidence_binding_ref_ids(locator_matches)
        if len(unique_locator_matches) == 1:
            return unique_locator_matches[0]
        readable_matches = [
            ref
            for ref in locator_matches
            if cls._evidence_binding_ref_is_readback_like(ref)
        ]
        unique_readable_matches = cls._unique_evidence_binding_ref_ids(readable_matches)
        if len(unique_readable_matches) == 1:
            return unique_readable_matches[0]
        coalesced_task_workspace_match = cls._coalesced_task_workspace_body_text_candidate(readable_matches)
        if coalesced_task_workspace_match:
            return coalesced_task_workspace_match
        return ""

    @classmethod
    def _evidence_binding_ref_matches_locator_text(cls, ref: Mapping[str, Any], locator_text: str) -> bool:
        for alias in cls._evidence_binding_ref_locator_aliases(ref):
            normalized_alias = alias.lower().replace("\\", "/")
            if len(normalized_alias) < 3:
                continue
            if normalized_alias in locator_text:
                return True
        return False

    @staticmethod
    def _evidence_binding_ref_locator_aliases(ref: Mapping[str, Any]) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)
            if text:
                basename = PurePosixPath(text.replace("\\", "/")).name
                if basename and basename not in aliases:
                    aliases.append(basename)

        for key in ("reference_id", "id", "cite_as", "path", "selection_key", "artifact_id", "action_call_id"):
            add(ref.get(key))
        raw_aliases = ref.get("aliases")
        if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
            for alias in raw_aliases:
                add(alias)
        return aliases

    @staticmethod
    def _evidence_binding_ref_is_readback_like(ref: Mapping[str, Any]) -> bool:
        kind = str(ref.get("kind") or "").strip().lower()
        action_id = str(ref.get("action_id") or "").strip().lower()
        return (
            "readback" in kind
            or kind == "task_workspace_artifact.readback"
            or kind == "task_workspace_artifact.targeted_readback"
            or action_id in {"read_file", "grep_files", "search_files"}
        )

    @classmethod
    def _coalesced_task_workspace_body_text_candidate(cls, refs: Sequence[Mapping[str, Any]]) -> str:
        task_workspace_refs: list[Mapping[str, Any]] = []
        for ref in refs:
            kind = str(ref.get("kind") or "").strip().lower()
            if kind not in {"task_workspace_artifact.readback", "task_workspace_artifact.targeted_readback"}:
                continue
            path = str(ref.get("path") or "").strip()
            evidence_id = str(ref.get("id") or "").strip()
            if path and evidence_id:
                task_workspace_refs.append(ref)
        if not task_workspace_refs:
            return ""
        paths = {str(ref.get("path") or "").strip() for ref in task_workspace_refs}
        if len(paths) != 1:
            return ""
        targeted_refs = [
            ref for ref in task_workspace_refs if str(ref.get("kind") or "").strip().lower() == "task_workspace_artifact.targeted_readback"
        ]
        selected_pool = targeted_refs or task_workspace_refs
        return str(selected_pool[-1].get("id") or "").strip()

    @classmethod
    def _deterministic_content_readback_candidates(
        cls,
        diagnostic: Mapping[str, Any],
        available_refs: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        if str(diagnostic.get("support_type") or "").strip() != "content":
            return []
        text = " ".join(
            str(diagnostic.get(key) or "")
            for key in ("claim", "evidence_id")
        ).lower()
        content_refs: list[Mapping[str, Any]] = []
        for ref in available_refs:
            if not isinstance(ref, Mapping):
                continue
            evidence_id = str(ref.get("id") or "").strip()
            if not evidence_id:
                continue
            status = str(ref.get("status") or "").strip().lower()
            body_state = str(ref.get("body_state") or "").strip().lower()
            kind = str(ref.get("kind") or "").strip().lower()
            if status != "ok" or body_state not in {"full", "bounded", "truncated"}:
                continue
            if (
                "readback" not in kind
                and "task_workspace_artifact" not in kind
                and kind != "agent_task.action.result"
            ):
                continue
            content_refs.append(ref)
        if not content_refs:
            return []

        diagnostic_evidence_id = str(diagnostic.get("evidence_id") or "").strip()
        alias_matches: list[str] = []
        path_matches: list[str] = []
        for ref in content_refs:
            evidence_id = str(ref.get("id") or "").strip()
            path = str(ref.get("path") or "").strip()
            basename = PurePosixPath(path.replace("\\", "/")).name if path else ""
            aliases = {
                evidence_id,
                str(ref.get("cite_as") or "").strip(),
                str(ref.get("artifact_id") or "").strip(),
                str(ref.get("action_id") or "").strip(),
                str(ref.get("action_call_id") or "").strip(),
                path,
                basename,
            }
            raw_aliases = ref.get("aliases")
            if isinstance(raw_aliases, Sequence) and not isinstance(raw_aliases, str | bytes | bytearray):
                aliases.update(str(alias or "").strip() for alias in raw_aliases)
            aliases.discard("")
            if diagnostic_evidence_id and diagnostic_evidence_id in aliases:
                alias_matches.append(evidence_id)
            path_tokens = [token.lower() for token in (path, basename) if token]
            if path_tokens and any(token in text for token in path_tokens):
                path_matches.append(evidence_id)
        unique_alias_matches = sorted(set(alias_matches))
        if len(unique_alias_matches) == 1:
            return unique_alias_matches
        unique_path_matches = sorted(set(path_matches))
        if len(unique_path_matches) == 1:
            return unique_path_matches

        unique_content_refs = sorted({str(ref.get("id") or "").strip() for ref in content_refs if ref.get("id")})
        if len(unique_content_refs) == 1:
            # "Only one content-bearing ref" must not cross files: a file/path/locator
            # reference may take this shortcut only when that ref's own path/anchor
            # agrees. Otherwise leave it unbound rather than binding to the wrong file.
            if cls._evidence_binding_id_looks_like_file_locator(diagnostic_evidence_id):
                only_ref = next(
                    (ref for ref in content_refs if str(ref.get("id") or "").strip() == unique_content_refs[0]),
                    None,
                )
                if only_ref is None or not cls._evidence_binding_ref_matches_locator_text(
                    only_ref, diagnostic_evidence_id.lower().replace("\\", "/")
                ):
                    return []
            return unique_content_refs
        return []

    @classmethod
    def _evidence_binding_repair_diagnostics(cls, grounding_guard: Mapping[str, Any]) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for item in grounding_guard.get("diagnostics", []) if isinstance(grounding_guard, Mapping) else []:
            if not isinstance(item, Mapping) or item.get("blocking") is not True:
                continue
            if str(item.get("code") or "") not in {
                "evidence_ledger.invalid_evidence_id",
                "evidence_ledger.ambiguous_evidence_alias",
                "evidence_ledger.missing_evidence_id",
                "evidence_ledger.ref_only_item_used_as_content_support",
            }:
                continue
            diagnostics.append(
                {
                    "code": item.get("code"),
                    "claim_index": item.get("index"),
                    "claim": item.get("claim"),
                    "evidence_id": item.get("evidence_id"),
                    "candidates": item.get("candidates"),
                    "support_type": item.get("support_type"),
                }
            )
        return DataFormatter.sanitize(diagnostics)

    @classmethod
    def _merge_repaired_evidence_use(cls, current_evidence_use: Any, repaired_evidence_use: Any) -> list[dict[str, Any]]:
        current = [dict(item) for item in current_evidence_use if isinstance(item, Mapping)] if isinstance(current_evidence_use, Sequence) and not isinstance(current_evidence_use, str | bytes | bytearray) else []
        repaired = [dict(item) for item in repaired_evidence_use if isinstance(item, Mapping)] if isinstance(repaired_evidence_use, Sequence) and not isinstance(repaired_evidence_use, str | bytes | bytearray) else []
        by_index: dict[int, dict[str, Any]] = {}
        by_claim: dict[str, dict[str, Any]] = {}
        for item in repaired:
            raw_claim_index = item.get("claim_index")
            try:
                claim_index = int(raw_claim_index) if raw_claim_index is not None else -1
            except (TypeError, ValueError):
                claim_index = -1
            if claim_index >= 0:
                by_index[claim_index] = item
            claim = str(item.get("claim") or "").strip()
            if claim:
                by_claim[claim] = item
        merged: list[dict[str, Any]] = []
        for index, item in enumerate(current):
            replacement = by_index.get(index)
            if replacement is None:
                replacement = by_claim.get(str(item.get("claim") or "").strip())
            merged.append(DataFormatter.sanitize(replacement if replacement is not None else item))
        return merged

    async def _trusted_task_workspace_artifacts_for_verifier(
        self,
        evidence_summary: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for ref in self._trusted_task_workspace_artifact_refs_from_summary(evidence_summary):
            artifact = self._trusted_task_workspace_artifact_ref_summary(ref)
            path = str(ref.get("path") or "").strip()
            if path:
                try:
                    read_result = await self.task_workspace.read_file(
                        path,
                        max_bytes=self._task_workspace_artifact_verifier_readback_bytes(ref),
                    )
                except Exception as error:
                    artifact["readback"] = {
                        "status": "failed",
                        "error": {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        },
                    }
                else:
                    content = read_result.get("content")
                    artifact["readback"] = {
                        "status": "read",
                        "path": str(read_result.get("path") or path),
                        "truncated": bool(read_result.get("truncated")),
                        "content": (
                            self._truncate_prompt_text(content, _VERIFIER_PROMPT_VALUE_CHARS)
                            if isinstance(content, str)
                            else ""
                        ),
                    }
                    targeted_readbacks = await self._trusted_task_workspace_artifact_targeted_readbacks(ref, read_result)
                    if targeted_readbacks:
                        artifact["targeted_readbacks"] = targeted_readbacks
            artifacts.append(artifact)
            if len(artifacts) >= 4:
                break
        return DataFormatter.sanitize(artifacts)

    async def _trusted_task_workspace_artifact_targeted_readbacks(
        self,
        ref: Mapping[str, Any],
        read_result: Mapping[str, Any],
        *,
        queries: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        path = str(ref.get("path") or read_result.get("path") or "").strip()
        if not path:
            return []
        byte_count = self._coerce_non_negative_int(ref.get("bytes") or read_result.get("bytes"))
        read_bytes = self._coerce_non_negative_int(read_result.get("read_bytes"))
        is_scoped_readback = bool(read_result.get("truncated")) or byte_count > read_bytes > 0
        if not is_scoped_readback:
            return []
        max_snippet_bytes = min(_VERIFIER_PROMPT_ITEM_CHARS, 2400)
        readbacks: list[dict[str, Any]] = []

        if byte_count > read_bytes > 0:
            offset = max(0, byte_count - max_snippet_bytes)
            try:
                tail = await self.task_workspace.read_file(path, max_bytes=max_snippet_bytes, offset=offset)
            except Exception as error:
                readbacks.append(
                    {
                        "kind": "tail_window",
                        "path": path,
                        "status": "failed",
                        "offset": offset,
                        "error": {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        },
                    }
                )
            else:
                readbacks.append(
                    {
                        "kind": "tail_window",
                        "path": str(tail.get("path") or path),
                        "status": "read",
                        "offset": int(tail.get("offset") or offset),
                        "truncated": bool(tail.get("truncated")),
                        "content": self._truncate_prompt_text(str(tail.get("content") or ""), max_snippet_bytes),
                    }
                )

        max_file_bytes = max(byte_count, _VERIFIER_PROMPT_VALUE_CHARS)
        for query in [*list(queries or ()), *self._task_workspace_artifact_verifier_target_queries()]:
            if len(readbacks) >= 8:
                break
            match = await self._task_workspace_artifact_search_readback(path, query, max_file_bytes=max_file_bytes)
            if match is not None:
                readbacks.append(match)
        return DataFormatter.sanitize(readbacks)

    async def _task_workspace_artifact_search_readback(
        self,
        path: str,
        query: str,
        *,
        max_file_bytes: int,
    ) -> dict[str, Any] | None:
        for search_query in self._task_workspace_artifact_query_variants(query):
            try:
                matches = await self.task_workspace.search_files(
                    search_query,
                    path=path,
                    max_results=1,
                    context_lines=4,
                    max_snippet_bytes=min(_VERIFIER_PROMPT_ITEM_CHARS, 2400),
                    max_file_bytes=max_file_bytes,
                )
            except Exception:
                continue
            if not matches:
                continue
            match = matches[0]
            return {
                "kind": "section_search",
                "path": str(match.get("path") or path),
                "query": query,
                "matched_query": search_query,
                "line_start": match.get("line_start"),
                "line_end": match.get("line_end"),
                "truncated": bool(match.get("truncated")),
                "content": self._truncate_prompt_text(str(match.get("snippet") or ""), _VERIFIER_PROMPT_ITEM_CHARS),
            }
        return None

    def _task_workspace_artifact_verifier_target_queries(self) -> list[str]:
        queries: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if not text:
                return
            text = " ".join(text.split())
            if len(text) > 120:
                return
            key = text.casefold()
            if key not in {item.casefold() for item in queries}:
                queries.append(text)

        def collect_section(value: Any) -> None:
            if isinstance(value, str):
                add(value)
                return
            if isinstance(value, Mapping):
                for key in ("title", "name", "heading", "id"):
                    add(value.get(key))
                return

        def collect_contract(value: Any) -> None:
            if not isinstance(value, Mapping):
                return
            sections = value.get("sections")
            if isinstance(sections, Sequence) and not isinstance(sections, str | bytes | bytearray):
                for section in sections:
                    collect_section(section)

        collect_contract(self._agent_task_option("output_contract", None))
        execution_prompt = self._execution_prompt_context()
        collect_contract(execution_prompt.get("output_contract"))
        prompt_input = execution_prompt.get("input")
        if isinstance(prompt_input, Mapping):
            collect_contract(prompt_input.get("output_contract"))
            case = prompt_input.get("case")
            if isinstance(case, Mapping):
                collect_contract(case.get("output_contract"))

        for query in (
            "source",
            "sources",
            "source list",
            "references",
            "citations",
            "risk",
            "risks",
            "uncertainty",
            "coverage",
        ):
            add(query)
        return queries[:12]

    @classmethod
    def _evidence_use_verifier_target_queries(cls, evidence_use: Any) -> list[str]:
        queries: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if not text:
                return
            text = " ".join(text.split())
            if len(text) > 160:
                return
            key = text.casefold()
            if key not in {item.casefold() for item in queries}:
                queries.append(text)

        for use in collect_evidence_use({"evidence_use": evidence_use}):
            if not isinstance(use, Mapping):
                continue
            add(use.get("claim"))
            for evidence_id in cls._normalize_string_list(use.get("evidence_ids")):
                if "/" in evidence_id or "." in evidence_id:
                    add(PurePosixPath(evidence_id).name)
        return queries[:12]

    @staticmethod
    def _task_workspace_artifact_query_variants(query: str) -> list[str]:
        variants: list[str] = []
        for value in (query, query.title(), query.lower(), query.upper()):
            text = str(value or "").strip()
            if text and text not in variants:
                variants.append(text)
        return variants

    @classmethod
    def _task_workspace_artifact_verifier_readback_bytes(cls, ref: Mapping[str, Any]) -> int:
        declared_bytes = cls._coerce_non_negative_int(ref.get("bytes"))
        if declared_bytes > 0 and declared_bytes < _VERIFIER_PROMPT_VALUE_CHARS:
            return declared_bytes + 1
        return max(_WORKSPACE_ARTIFACT_PREVIEW_BYTES, _VERIFIER_PROMPT_VALUE_CHARS)

    @classmethod
    def _trusted_task_workspace_artifact_refs_from_summary(cls, evidence_summary: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                if cls._is_trusted_task_workspace_artifact_ref(value) and str(value.get("path") or "").strip():
                    refs.append(dict(value))
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    collect(item)

        collect(evidence_summary.get("artifact_refs"))
        task_workspace_refs = evidence_summary.get("task_workspace_refs")
        if isinstance(task_workspace_refs, Mapping):
            collect(task_workspace_refs)

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (str(ref.get("path") or ""), str(ref.get("sha256") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)
        return deduped

    @classmethod
    def _trusted_task_workspace_artifact_refs_have_readback(cls, refs: Sequence[Mapping[str, Any]]) -> bool:
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            readback = ref.get("readback")
            if isinstance(readback, Mapping):
                content = str(readback.get("content") or readback.get("preview") or readback.get("text") or "").strip()
                if content:
                    return True
                if cls._coerce_non_negative_int(readback.get("bytes")) > 0:
                    return True
                if cls._coerce_non_negative_int(readback.get("read_bytes")) > 0:
                    return True
            if cls._coerce_non_negative_int(ref.get("bytes")) > 0 and str(ref.get("sha256") or "").strip():
                return True
            file_refs = ref.get("file_refs")
            if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
                if cls._trusted_task_workspace_artifact_refs_have_readback(
                    [item for item in file_refs if isinstance(item, Mapping)]
                ):
                    return True
        return False

    @classmethod
    def _execution_status_liveness_diagnostic(
        cls,
        *,
        execution_status: str,
        execution_evidence_summary: Mapping[str, Any],
        verification: Mapping[str, Any],
        normalized: Mapping[str, Any],
        trusted_task_workspace_artifact_refs: Sequence[Mapping[str, Any]],
        grounding_guard: Mapping[str, Any] | None,
        final_result_required: bool,
        non_blocking_action_ids: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        if execution_status not in {"failed", "error", "timed_out", "blocked"}:
            return None
        non_blocking_actions = set(cls._normalize_string_list(non_blocking_action_ids))

        def action_id_from_record(action: Mapping[str, Any]) -> str:
            return str(action.get("action_id") or action.get("id") or action.get("name") or "").strip()

        def status_is_terminal_issue(status_value: Any) -> bool:
            return str(status_value or "").strip().lower() in {
                "failed",
                "failure",
                "error",
                "timed_out",
                "timeout",
                "blocked",
            }

        def error_record_is_nonblocking(error: Mapping[str, Any]) -> bool:
            action_id = str(error.get("action_id") or error.get("id") or error.get("name") or "").strip()
            if action_id and action_id in non_blocking_actions:
                return True
            diagnostic_code = str(error.get("code") or "").strip()
            return diagnostic_code == "action_loop.max_rounds_reached" and "action_loop" in non_blocking_actions

        if not final_result_required or not trusted_task_workspace_artifact_refs:
            return None
        if not cls._trusted_task_workspace_artifact_refs_have_readback(trusted_task_workspace_artifact_refs):
            return None
        if normalized.get("is_complete") is not True or normalized.get("requires_block") is True:
            return None
        if cls._normalize_string_list(normalized.get("missing_criteria")):
            return None
        if not isinstance(grounding_guard, Mapping):
            return None
        if grounding_guard.get("valid") is not True or int(grounding_guard.get("blocking_count") or 0) > 0:
            return None
        if not cls._verification_criteria_are_satisfied(verification.get("criterion_checks")):
            return None
        if [
            action_id
            for action_id in cls._normalize_string_list(execution_evidence_summary.get("failed_actions"))
            if action_id not in non_blocking_actions
        ]:
            return None
        if [
            action_id
            for action_id in cls._normalize_string_list(execution_evidence_summary.get("blocked_actions"))
            if action_id not in non_blocking_actions
        ]:
            return None
        if cls._normalize_string_list(execution_evidence_summary.get("approval_required_actions")):
            return None
        action_statuses = execution_evidence_summary.get("action_statuses")
        if isinstance(action_statuses, Mapping):
            for action_id, value in action_statuses.items():
                if status_is_terminal_issue(value) and str(action_id or "").strip() not in non_blocking_actions:
                    return None
        for action in execution_evidence_summary.get("actions", []) or []:
            if not isinstance(action, Mapping):
                continue
            status = str(action.get("status") or "").strip().lower()
            if status_is_terminal_issue(status) and action_id_from_record(action) not in non_blocking_actions:
                return None

        errors = execution_evidence_summary.get("errors")
        error_records = [dict(error) for error in errors if isinstance(error, Mapping)] if isinstance(errors, list) else []
        if not error_records:
            if not non_blocking_actions:
                return None
            return {
                "status": execution_status,
                "error_type": "",
                "stage": "",
                "message": "Execution status came only from non-blocking action diagnostics.",
                "last_progress_event": None,
                "idle_seconds": None,
                "elapsed_seconds": None,
                "non_blocking_action_ids": sorted(non_blocking_actions),
                "diagnostic_only": True,
            }
        first_error = error_records[0]
        if not cls._is_liveness_stall_error(first_error) and not all(
            error_record_is_nonblocking(error) for error in error_records
        ):
            return None
        return {
            "status": execution_status,
            "error_type": str(first_error.get("error_type") or first_error.get("type") or ""),
            "stage": str(first_error.get("stage") or ""),
            "message": str(first_error.get("message") or ""),
            "last_progress_event": first_error.get("last_progress_event"),
            "idle_seconds": first_error.get("idle_seconds"),
            "elapsed_seconds": first_error.get("elapsed_seconds"),
            "non_blocking_action_ids": sorted(non_blocking_actions),
            "diagnostic_only": True,
        }

    @classmethod
    def _verification_criteria_are_satisfied(cls, criterion_checks: Any) -> bool:
        if not isinstance(criterion_checks, Sequence) or isinstance(criterion_checks, str | bytes | bytearray):
            return False
        checked = False
        for check in criterion_checks:
            if not isinstance(check, Mapping):
                continue
            if check.get("satisfied") is not True:
                return False
            checked = True
        return checked

    @classmethod
    def _material_claim_candidate_index(
        cls,
        candidate: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Create request-local claim selections over exact carrier spans.

        Segmentation is structural only. It does not decide whether a block is
        factual or supported; the semantic verifier owns that judgment. The
        host keeps carrier/version identity private and offers one short key per
        exact text block so the response never has to transcribe canonical ids
        or quotes.
        """

        raw_carriers = candidate.get("carriers")
        carriers = (
            [item for item in raw_carriers if isinstance(item, Mapping)]
            if isinstance(raw_carriers, Sequence)
            and not isinstance(raw_carriers, str | bytes | bytearray)
            else [candidate]
        )
        segments: list[dict[str, Any]] = []
        for carrier_index, carrier in enumerate(carriers[:8]):
            text = str(carrier.get("text") or "")
            if not text.strip():
                continue
            lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip()
            ]
            line_syntax_roles = [
                cls._material_claim_syntax_role(line)
                for line in lines
            ]
            markdown_table_header_indexes = {
                line_index
                for line_index in range(len(lines) - 1)
                if line_syntax_roles[line_index] == "markdown_table_row"
                and line_syntax_roles[line_index + 1]
                == "markdown_table_separator"
            }
            for line_index, line in enumerate(lines):
                if line_index in markdown_table_header_indexes:
                    # A pipe row immediately followed by the Markdown table
                    # separator is deterministic table scaffolding. Its labels
                    # remain verifier-visible through artifact readback, while
                    # factual body rows remain independently auditable below.
                    continue
                # Keep individual model-visible selections bounded while every
                # non-empty physical line remains independently selectable and
                # every long-line chunk is an exact carrier substring. This is
                # structural framing only; it performs no semantic sentence or
                # claim classification.
                for chunk_index, start in enumerate(range(0, len(line), 8_000)):
                    exact_text = line[start : start + 8_000]
                    if not exact_text:
                        continue
                    syntax_role = cls._material_claim_syntax_role(exact_text)
                    if syntax_role in {
                        "markdown_heading",
                        "markdown_separator",
                        "markdown_table_separator",
                    }:
                        # These spans are deterministic document scaffolding,
                        # not independently selectable material-claim bodies.
                        # Their document/criterion role remains visible through
                        # artifact readback and acceptance locators.
                        continue
                    segments.append(
                        {
                            "_order": (
                                carrier_index,
                                line_index,
                                chunk_index,
                            ),
                            "text": exact_text,
                            "syntax_role": syntax_role,
                            "delivery_kind": str(
                                carrier.get("kind") or "terminal_candidate"
                            ),
                            "path": str(carrier.get("path") or ""),
                            "carrier_id": str(
                                carrier.get("carrier_id")
                                or carrier.get("content_version_id")
                                or ""
                            ),
                            "content_version_id": str(
                                carrier.get("content_version_id") or ""
                            ),
                        }
                    )

        total = sum(len(item["text"]) for item in segments)
        if total > 96_000:
            selected_indexes: set[int] = set()
            head_size = 0
            for index, item in enumerate(segments):
                if head_size and head_size + len(item["text"]) > 48_000:
                    break
                selected_indexes.add(index)
                head_size += len(item["text"])
            tail_size = 0
            for index in range(len(segments) - 1, -1, -1):
                if index in selected_indexes:
                    break
                item = segments[index]
                if tail_size and tail_size + len(item["text"]) > 48_000:
                    break
                selected_indexes.add(index)
                tail_size += len(item["text"])
            segments = [
                item
                for index, item in enumerate(segments)
                if index in selected_indexes
            ]

        indexed: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(segments, start=1):
            claim_key = f"claim_{index}"
            indexed[claim_key] = {
                key: value
                for key, value in item.items()
                if key != "_order"
            }
        return indexed

    @staticmethod
    def _material_claim_syntax_role(text: str) -> str:
        """Describe lexical Markdown framing without deciding claim semantics."""

        value = str(text or "").strip()
        if value.startswith("#") and value.lstrip("#").startswith(" "):
            return "markdown_heading"
        if len(value) >= 3 and not value.strip("-_* "):
            return "markdown_separator"
        if value.startswith("|") and value.endswith("|"):
            cells = [cell.strip() for cell in value.strip("|").split("|")]
            if cells and all(
                cell and not cell.strip(":").replace("-", "")
                for cell in cells
            ):
                return "markdown_table_separator"
            return "markdown_table_row"
        return "prose"

    @classmethod
    def _material_claim_candidates_for_verifier(
        cls,
        candidate: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        return [
            {
                "claim_key": claim_key,
                "text": item["text"],
                "delivery_kind": item["delivery_kind"],
                "syntax_role": item["syntax_role"],
                **({"path": item["path"]} if item.get("path") else {}),
            }
            for claim_key, item in cls._material_claim_candidate_index(
                candidate
            ).items()
        ]

    @staticmethod
    def _model_acceptance_locator_view(
        evidence_ledger: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project locator facts without creating another selection domain."""

        view = acceptance_locator_view_from_ledger(evidence_ledger)
        raw_items = view.get("items")
        items = (
            [item for item in raw_items if isinstance(item, Mapping)]
            if isinstance(raw_items, Sequence)
            and not isinstance(raw_items, str | bytes | bytearray)
            else []
        )
        identity_fields = {
            "reference_id",
            "id",
            "evidence_id",
            "cite_as",
            "criterion_id",
            "content_fingerprint",
            "source_evidence_ids",
        }
        projected_items = [
            {
                key: DataFormatter.sanitize(value)
                for key, value in item.items()
                if key not in identity_fields
            }
            for item in items
        ]
        return {
            "schema_version": "acceptance_locator_view/v1",
            "items": projected_items,
            "item_count": len(projected_items),
            "rules": DataFormatter.sanitize(view.get("rules", {})),
        }

    def _validate_criterion_audit(
        self,
        verification: Mapping[str, Any],
        *,
        offered_reference_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        offered = {
            f"criterion:{index}": str(criterion)
            for index, criterion in enumerate(self.success_criteria, start=1)
        }
        effective_offered_reference_ids = (
            set(offered_reference_ids)
            if offered_reference_ids is not None
            else set(self._task_reference_catalog.offered_references())
        )
        raw_checks = verification.get("criterion_checks")
        structural_errors: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        failed_checks: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        if not isinstance(raw_checks, Sequence) or isinstance(
            raw_checks,
            str | bytes | bytearray,
        ):
            structural_errors.append(
                {
                    "code": "criterion_checks_invalid",
                    "message": "criterion_checks must be a structured list.",
                }
            )
            raw_checks = []
        for index, raw_check in enumerate(raw_checks):
            if not isinstance(raw_check, Mapping):
                structural_errors.append(
                    {
                        "code": "criterion_check_invalid",
                        "index": index,
                        "message": "A criterion check is not an object.",
                    }
                )
                continue
            criterion_id = str(raw_check.get("criterion_id") or "").strip()
            satisfied = raw_check.get("satisfied")
            summary = str(raw_check.get("summary") or "").strip()
            gaps = self._normalize_string_list(raw_check.get("gaps"))
            raw_evidence_ids = raw_check.get("evidence_ids")
            evidence_ids = (
                [str(value or "").strip() for value in raw_evidence_ids]
                if isinstance(raw_evidence_ids, Sequence)
                and not isinstance(raw_evidence_ids, str | bytes | bytearray)
                else []
            )
            if criterion_id not in offered:
                structural_errors.append(
                    {
                        "code": "criterion_id_unknown",
                        "index": index,
                        "criterion_id": criterion_id,
                        "message": "criterion_id is not one of the current offered success criteria.",
                    }
                )
            elif criterion_id in seen_ids:
                structural_errors.append(
                    {
                        "code": "criterion_id_duplicate",
                        "index": index,
                        "criterion_id": criterion_id,
                        "message": "criterion_id was returned more than once.",
                    }
                )
            else:
                seen_ids.add(criterion_id)
            if not isinstance(satisfied, bool):
                structural_errors.append(
                    {
                        "code": "criterion_satisfied_invalid",
                        "index": index,
                        "criterion_id": criterion_id,
                        "message": "satisfied must be a boolean.",
                    }
                )
            if not summary:
                structural_errors.append(
                    {
                        "code": "criterion_summary_missing",
                        "index": index,
                        "criterion_id": criterion_id,
                        "message": "summary is required.",
                    }
                )
            if len(evidence_ids) != len(set(evidence_ids)):
                structural_errors.append(
                    {
                        "code": "criterion_evidence_duplicate",
                        "index": index,
                        "criterion_id": criterion_id,
                        "message": "evidence_ids contains duplicates.",
                    }
                )
            unknown_evidence_ids = [
                evidence_id
                for evidence_id in evidence_ids
                if not evidence_id
                or evidence_id not in effective_offered_reference_ids
            ]
            valid_evidence_ids = [
                evidence_id
                for evidence_id in evidence_ids
                if evidence_id in effective_offered_reference_ids
            ]
            if unknown_evidence_ids:
                # Reject every unknown join key before lookup, but do not turn a
                # known criterion into a repeated response-shape loop. The
                # criterion remains unproven and is repaired through the normal
                # criterion path with only host-offered bindings retained.
                satisfied = False
                gaps.append(
                    "Host validation discarded evidence_ids outside the offered set."
                )
            normalized_check = {
                "criterion_id": criterion_id,
                "criterion": offered.get(criterion_id, ""),
                "satisfied": satisfied if isinstance(satisfied, bool) else False,
                "summary": summary,
                "gaps": gaps,
                "evidence_ids": valid_evidence_ids,
                **(
                    {"discarded_evidence_ids": unknown_evidence_ids}
                    if unknown_evidence_ids
                    else {}
                ),
            }
            checks.append(normalized_check)
            if criterion_id in offered and normalized_check["satisfied"] is False:
                failed_checks.append(normalized_check)
        missing_ids = [criterion_id for criterion_id in offered if criterion_id not in seen_ids]
        if missing_ids:
            structural_errors.append(
                {
                    "code": "criterion_checks_missing",
                    "criterion_ids": missing_ids,
                    "message": "criterion_checks omitted one or more offered success criteria.",
                }
            )
        valid = not structural_errors and not failed_checks
        if structural_errors:
            issue_code = "terminal_verifier_output_invalid"
            contract_subject = "verification:response"
        elif failed_checks:
            issue_code = "criterion_unsatisfied"
            failed_ids = [str(item.get("criterion_id") or "") for item in failed_checks]
            contract_subject = (
                failed_ids[0]
                if len(failed_ids) == 1
                else "criteria:"
                + hashlib.sha256("\n".join(sorted(failed_ids)).encode("utf-8")).hexdigest()[:12]
            )
        else:
            issue_code = ""
            contract_subject = ""
        requirements = [
            {
                "criterion_id": item.get("criterion_id"),
                "criterion": item.get("criterion"),
                "summary": item.get("summary"),
                "gaps": item.get("gaps", []),
                "evidence_ids": item.get("evidence_ids", []),
                **(
                    {
                        "discarded_evidence_ids": item.get(
                            "discarded_evidence_ids", []
                        )
                    }
                    if item.get("discarded_evidence_ids")
                    else {}
                ),
            }
            for item in failed_checks
        ]
        requirements.extend(
            {
                "code": error.get("code"),
                "reason": error.get("message"),
                **(
                    {"field": error.get("field")}
                    if error.get("field")
                    else {}
                ),
                **(
                    {
                        "invalid_reference_ids": error.get(
                            "invalid_reference_ids"
                        )
                    }
                    if error.get("invalid_reference_ids")
                    else {}
                ),
                "offered_reference_ids": error.get(
                    "offered_reference_ids", []
                ),
                "offered_reference_count": int(
                    error.get("offered_reference_count") or 0
                ),
            }
            for error in structural_errors
        )
        return {
            "valid": valid,
            "checks": DataFormatter.sanitize(checks),
            "failed_checks": DataFormatter.sanitize(failed_checks),
            "structural_errors": DataFormatter.sanitize(structural_errors),
            "repair_contract": (
                {
                    "gate_kind": (
                        "output_contract" if structural_errors else "criterion"
                    ),
                    "issue_code": issue_code,
                    "contract_subject": contract_subject,
                    **(
                        {"protocol_section": "criterion_checks"}
                        if structural_errors
                        else {}
                    ),
                    "requirements": DataFormatter.sanitize(requirements),
                }
                if not valid
                else {}
            ),
        }

    def _validate_material_claim_audit(
        self,
        verification: Mapping[str, Any],
        *,
        terminal_candidate: Mapping[str, Any],
        offered_reference_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        claim_candidates = self._material_claim_candidate_index(
            terminal_candidate
        )
        effective_offered_reference_ids = (
            set(offered_reference_ids)
            if offered_reference_ids is not None
            else set(self._task_reference_catalog.offered_references())
        )
        coverage_complete = verification.get("material_claim_coverage_complete") is True
        raw_checks = verification.get("material_claim_checks")
        structural_errors: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        failed_checks: list[dict[str, Any]] = []
        if not coverage_complete:
            structural_errors.append(
                {
                    "code": "material_claim_coverage_incomplete",
                    "message": "The semantic verifier did not confirm complete material-claim coverage.",
                }
            )
        if not isinstance(raw_checks, Sequence) or isinstance(raw_checks, str | bytes | bytearray):
            structural_errors.append(
                {
                    "code": "material_claim_checks_invalid",
                    "message": "material_claim_checks must be a structured list.",
                }
            )
            raw_checks = []
        allowed_kinds = {
            "external_fact",
            "absence_claim",
            "derived_analysis",
            "recommendation",
            "uncertainty",
        }
        allowed_states = {
            "supported",
            "reasonable_derived",
            "unsupported",
            "contradicted",
            "unverifiable",
        }
        offered_criterion_ids = {
            f"criterion:{index}"
            for index, _criterion in enumerate(self.success_criteria, start=1)
        }
        seen_claim_keys: set[str] = set()
        for index, raw_check in enumerate(raw_checks):
            if not isinstance(raw_check, Mapping):
                structural_errors.append(
                    {
                        "code": "material_claim_check_invalid",
                        "index": index,
                        "message": "A material claim check is not an object.",
                    }
                )
                continue
            claim_key = str(raw_check.get("claim_key") or "").strip()
            claim_kind = str(raw_check.get("claim_kind") or "").strip()
            state = str(raw_check.get("state") or "").strip()
            reason = str(raw_check.get("reason") or "").strip()
            raw_evidence_ids = raw_check.get("evidence_ids")
            evidence_ids = (
                [str(value or "").strip() for value in raw_evidence_ids]
                if isinstance(raw_evidence_ids, Sequence)
                and not isinstance(raw_evidence_ids, str | bytes | bytearray)
                else []
            )
            raw_required_criterion_ids = raw_check.get(
                "required_for_criterion_ids"
            )
            required_for_criterion_ids = (
                [str(value or "").strip() for value in raw_required_criterion_ids]
                if isinstance(raw_required_criterion_ids, Sequence)
                and not isinstance(
                    raw_required_criterion_ids,
                    str | bytes | bytearray,
                )
                else []
            )
            check_errors: list[str] = []
            claim_candidate = claim_candidates.get(claim_key)
            if claim_candidate is None:
                check_errors.append(
                    "claim_key is not one of the current offered material claim candidates"
                )
            if claim_key in seen_claim_keys:
                check_errors.append("claim_key is duplicated")
            elif claim_key:
                seen_claim_keys.add(claim_key)
            if claim_kind not in allowed_kinds:
                check_errors.append("claim_kind is invalid")
            if state not in allowed_states:
                check_errors.append("state is invalid")
            if not reason:
                check_errors.append("reason is required")
            if len(evidence_ids) != len(set(evidence_ids)):
                check_errors.append("evidence_ids contains duplicates")
            unknown_evidence_ids = [
                evidence_id
                for evidence_id in evidence_ids
                if not evidence_id or evidence_id not in effective_offered_reference_ids
            ]
            if unknown_evidence_ids:
                check_errors.append("evidence_ids contains a reference outside the offered set")
            if "required_for_criterion_ids" not in raw_check:
                check_errors.append("required_for_criterion_ids is required")
            if len(required_for_criterion_ids) != len(
                set(required_for_criterion_ids)
            ):
                check_errors.append("required_for_criterion_ids contains duplicates")
            unknown_required_criterion_ids = [
                criterion_id
                for criterion_id in required_for_criterion_ids
                if not criterion_id or criterion_id not in offered_criterion_ids
            ]
            if unknown_required_criterion_ids:
                check_errors.append(
                    "required_for_criterion_ids contains a criterion outside the offered set"
                )
            # An authored recommendation or safety/scope boundary is evaluated
            # from the host-validated carrier text itself.  It is not an
            # external fact and must not be forced to invent a source identity
            # merely to state what the document does or does not recommend.
            supported_without_evidence = (
                state == "supported"
                and not evidence_ids
                and claim_kind != "recommendation"
            )
            if supported_without_evidence:
                check_errors.append("supported material claims require at least one offered evidence reference")
            invalid_reasonable_derived_fact = state == "reasonable_derived" and claim_kind not in {
                "derived_analysis",
                "recommendation",
            }
            if invalid_reasonable_derived_fact:
                check_errors.append("reasonable_derived is valid only for derived analysis or recommendation")
            normalized_check = {
                "claim_key": claim_key,
                "carrier_id": (
                    str(claim_candidate.get("carrier_id") or "")
                    if claim_candidate is not None
                    else ""
                ),
                "path": (
                    str(claim_candidate.get("path") or "")
                    if claim_candidate is not None
                    else ""
                ),
                "content_version_id": (
                    str(claim_candidate.get("content_version_id") or "")
                    if claim_candidate is not None
                    else ""
                ),
                "artifact_quote": (
                    str(claim_candidate.get("text") or "")
                    if claim_candidate is not None
                    else ""
                ),
                "claim_kind": claim_kind,
                "state": state,
                "evidence_ids": evidence_ids,
                "required_for_criterion_ids": required_for_criterion_ids,
                "reason": reason,
            }
            checks.append(normalized_check)
            targetable_support_failure = ""
            targetable_state = ""
            if supported_without_evidence:
                targetable_support_failure = (
                    "supported material claims require at least one offered evidence reference"
                )
                targetable_state = "unsupported"
            elif invalid_reasonable_derived_fact:
                targetable_support_failure = (
                    "reasonable_derived is valid only for derived analysis or recommendation"
                )
                targetable_state = "unverifiable"
            if (
                targetable_support_failure
                and claim_candidate is not None
                and check_errors == [targetable_support_failure]
            ):
                # A known material claim with a semantically insufficient
                # support state is a factual-integrity failure, not merely a
                # response-shape retry. Conservatively retain the host-owned
                # artifact target and require deletion rather than retrying an
                # unchanged verifier protocol indefinitely.
                normalized_check["reported_state"] = state
                normalized_check["state"] = targetable_state
                normalized_check["reason"] = (
                    f"{reason} Host validation: {targetable_support_failure}"
                )
                normalized_check["repair_policy"] = (
                    "evidence_reacquisition_required"
                    if required_for_criterion_ids
                    else "delete_only"
                )
                failed_checks.append(normalized_check)
            elif check_errors:
                structural_errors.append(
                    {
                        "code": "material_claim_check_untrusted",
                        "index": index,
                        "claim_key": claim_key,
                        "carrier_id": normalized_check["carrier_id"],
                        "path": normalized_check["path"],
                        "content_version_id": normalized_check[
                            "content_version_id"
                        ],
                        "artifact_quote": normalized_check["artifact_quote"],
                        "claim_kind": normalized_check["claim_kind"],
                        "state": normalized_check["state"],
                        "evidence_ids": normalized_check["evidence_ids"],
                        "reason": normalized_check["reason"],
                        "field": (
                            "material_claim_checks.evidence_ids"
                            if unknown_evidence_ids
                            else "material_claim_checks.claim_key"
                        ),
                        "invalid_reference_ids": unknown_evidence_ids,
                        "offered_reference_ids": sorted(
                            effective_offered_reference_ids
                        )[:24],
                        "offered_reference_count": len(
                            effective_offered_reference_ids
                        ),
                        "messages": check_errors,
                    }
                )
            elif state in {"unsupported", "contradicted", "unverifiable"}:
                normalized_check["repair_policy"] = (
                    "evidence_reacquisition_required"
                    if required_for_criterion_ids
                    else "delete_only"
                )
                failed_checks.append(normalized_check)
        issue_code = ""
        if structural_errors:
            issue_code = "terminal_verifier_output_invalid"
        elif any(
            item.get("repair_policy") == "evidence_reacquisition_required"
            for item in failed_checks
        ):
            issue_code = "required_material_claim_evidence_missing"
        elif any(item.get("state") == "contradicted" for item in failed_checks):
            issue_code = "contradicted_material_claim"
        elif failed_checks:
            issue_code = "unsupported_material_claim"
        valid = coverage_complete and not structural_errors and not failed_checks
        failed_carrier_ids = list(
            dict.fromkeys(
                str(item.get("carrier_id") or "")
                for item in failed_checks
                if str(item.get("carrier_id") or "")
            )
        )
        requirements = [
            {
                "claim_key": item.get("claim_key"),
                "carrier_id": item.get("carrier_id"),
                "path": item.get("path"),
                "content_version_id": item.get("content_version_id"),
                "artifact_quote": item.get("artifact_quote"),
                "claim_kind": item.get("claim_kind"),
                "state": item.get("state"),
                "evidence_ids": item.get("evidence_ids", []),
                "required_for_criterion_ids": item.get(
                    "required_for_criterion_ids", []
                ),
                "reason": item.get("reason"),
                "repair_policy": item.get("repair_policy", "delete_only"),
                **(
                    {"reported_state": item.get("reported_state")}
                    if item.get("reported_state")
                    else {}
                ),
            }
            for item in failed_checks
        ]
        requirements.extend(
            {
                "claim_key": error.get("claim_key"),
                "carrier_id": error.get("carrier_id"),
                "path": error.get("path"),
                "content_version_id": error.get("content_version_id"),
                "artifact_quote": error.get("artifact_quote"),
                "claim_kind": error.get("claim_kind"),
                "state": error.get("state"),
                "evidence_ids": error.get("evidence_ids", []),
                "reason": str(error.get("message") or "; ".join(error.get("messages") or [])),
                "code": error.get("code"),
                **(
                    {"field": error.get("field")}
                    if error.get("field")
                    else {}
                ),
                **(
                    {
                        "invalid_reference_ids": error.get(
                            "invalid_reference_ids"
                        )
                    }
                    if error.get("invalid_reference_ids")
                    else {}
                ),
                "offered_reference_ids": error.get(
                    "offered_reference_ids", []
                ),
                "offered_reference_count": int(
                    error.get("offered_reference_count") or 0
                ),
            }
            for error in structural_errors
        )
        if len(failed_carrier_ids) == 1:
            contract_subject = f"carrier:{failed_carrier_ids[0]}"
        elif failed_carrier_ids:
            carrier_set_digest = hashlib.sha256(
                "\n".join(sorted(failed_carrier_ids)).encode("utf-8")
            ).hexdigest()[:12]
            contract_subject = f"carriers:{carrier_set_digest}"
        else:
            contract_subject = (
                "verification:response"
                if structural_errors
                else "verification:material_claim_audit"
            )
        return {
            "valid": valid,
            "coverage_complete": coverage_complete,
            "checks": DataFormatter.sanitize(checks),
            "failed_checks": DataFormatter.sanitize(failed_checks),
            "failed_carrier_ids": failed_carrier_ids,
            "structural_errors": DataFormatter.sanitize(structural_errors),
            "repair_contract": (
                {
                    "gate_kind": (
                        "output_contract"
                        if structural_errors
                        else "factual_integrity"
                    ),
                    "issue_code": issue_code,
                    "contract_subject": contract_subject,
                    **(
                        {"protocol_section": "material_claim_checks"}
                        if structural_errors
                        else {}
                    ),
                    "requirements": DataFormatter.sanitize(requirements),
                }
                if not valid
                else {}
            ),
        }

    @classmethod
    def _is_liveness_stall_error(cls, error: Mapping[str, Any]) -> bool:
        error_type = str(error.get("error_type") or error.get("type") or "")
        status = str(error.get("status") or "")
        message = str(error.get("message") or "")
        combined = f"{error_type} {status} {message}".lower()
        return (
            "runtimestagestallerror" in combined
            or "no progress" in combined
            or "idle deadline" in combined
            or "stalled" in combined
        )

    @classmethod
    def _align_guarded_verification_fields(
        cls,
        normalized: dict[str, Any],
        guard_reasons: Sequence[str],
        raw_verification: Mapping[str, Any] | None = None,
        *,
        terminal: bool = True,
    ) -> None:
        if normalized.get("is_complete") is True or not guard_reasons:
            return
        raw_verification = raw_verification or {}
        if terminal and raw_verification.get("is_complete") is not True:
            return
        missing = cls._normalize_string_list(normalized.get("missing_criteria"))
        guard_label = ", ".join(str(reason) for reason in guard_reasons if str(reason).strip()) or "verification_guard"
        summary = missing[0] if missing else f"Verification needs more evidence for {guard_label}."
        guarded_reason = f"Verification needs another step: {summary}"
        normalized["reason"] = guarded_reason
        normalized["failure_analysis"] = guarded_reason
        if normalized.get("progress_message") not in (None, "", [], {}) or raw_verification.get("progress_message") not in (None, "", [], {}):
            normalized["progress_message"] = guarded_reason
        if terminal or not normalized.get("replan_instruction"):
            normalized["replan_instruction"] = (
                "Run another bounded step and produce explicit evidence for the guarded criteria."
            )
        normalized["next_step_requirements"] = (
            [normalized["replan_instruction"]]
            if terminal
            else cls._merge_string_lists(normalized.get("next_step_requirements"), [normalized["replan_instruction"]])
        )

    @classmethod
    def _trusted_task_workspace_artifact_ref_summary(cls, ref: Mapping[str, Any]) -> dict[str, Any]:
        summary = {
            "path": str(ref.get("path") or ""),
            "role": str(ref.get("role") or ""),
            "source": str(ref.get("source") or ""),
            "truncated": bool(ref.get("truncated")),
        }
        file_refs = ref.get("file_refs")
        if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
            summary["file_refs"] = [
                cls._compact_artifact_ref_for_verifier(file_ref)
                for file_ref in list(file_refs)[:8]
                if isinstance(file_ref, Mapping)
            ]
        preview = ref.get("preview")
        if isinstance(preview, str) and preview:
            summary["preview"] = cls._truncate_prompt_text(preview, _WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        return DataFormatter.sanitize(summary)

    @classmethod
    def _task_workspace_artifact_execution_result_for_verifier(cls, execution_result: Any) -> Any:
        if not isinstance(execution_result, Mapping):
            return execution_result
        return cls._task_workspace_artifact_hot_value(
            execution_result,
            omit_embedded_evidence=True,
        )

    @classmethod
    def _task_workspace_artifact_hot_value(
        cls,
        value: Any,
        *,
        key_context: str = "",
        omit_embedded_evidence: bool = False,
    ) -> Any:
        if isinstance(value, Mapping):
            if key_context in {"file_refs", "artifact_refs"}:
                return cls._compact_artifact_ref_for_verifier(value)
            compact: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if omit_embedded_evidence and key_text in _VERIFIER_RESULT_EMBEDDED_EVIDENCE_KEYS:
                    continue
                if key_text in {"sha256", "bytes", "read_bytes", "size", "media_type", "content_kind", "handler_id"}:
                    continue
                if key_text == "artifact_manifest" and isinstance(item, Mapping):
                    compact[key_text] = cls._task_workspace_artifact_manifest_for_verifier(
                        item,
                        omit_embedded_evidence=omit_embedded_evidence,
                    )
                    continue
                if key_text == "task_workspace_artifact_delivery" and isinstance(item, Mapping):
                    compact[key_text] = cls._task_workspace_artifact_delivery_for_verifier(
                        item,
                        omit_embedded_evidence=omit_embedded_evidence,
                    )
                    continue
                compact[key_text] = cls._task_workspace_artifact_hot_value(
                    item,
                    key_context=key_text,
                    omit_embedded_evidence=omit_embedded_evidence,
                )
            return compact
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return [
                cls._task_workspace_artifact_hot_value(
                    item,
                    key_context=key_context,
                    omit_embedded_evidence=omit_embedded_evidence,
                )
                for item in value
            ]
        return value

    @classmethod
    def _task_workspace_artifact_index_for_verifier(
        cls,
        evidence_ledger: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        index: list[dict[str, Any]] = []
        for artifact in task_workspace_artifacts_from_ledger(evidence_ledger):
            if not isinstance(artifact, Mapping):
                continue
            item: dict[str, Any] = {
                key: artifact.get(key)
                for key in ("path", "status", "body_state")
                if key in artifact
            }
            readback = artifact.get("readback")
            if isinstance(readback, Mapping):
                item["readback"] = {
                    key: readback.get(key)
                    for key in ("status", "path", "truncated")
                    if key in readback
                }
                item["readback"]["available"] = str(readback.get("status") or "") == "ok"
            index.append(DataFormatter.sanitize(item))
        return index

    @classmethod
    def _task_workspace_artifact_manifest_for_verifier(
        cls,
        manifest: Mapping[str, Any],
        *,
        omit_embedded_evidence: bool = False,
    ) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, item in manifest.items():
            key_text = str(key)
            if omit_embedded_evidence and key_text in _VERIFIER_RESULT_EMBEDDED_EVIDENCE_KEYS:
                continue
            if key_text in {"sha256", "bytes", "read_bytes", "size", "media_type", "content_kind", "handler_id"}:
                continue
            if key_text == "file_refs":
                compact[key_text] = cls._task_workspace_artifact_hot_value(
                    item,
                    key_context=key_text,
                    omit_embedded_evidence=omit_embedded_evidence,
                )
                continue
            compact[key_text] = cls._task_workspace_artifact_hot_value(
                item,
                key_context=key_text,
                omit_embedded_evidence=omit_embedded_evidence,
            )
        return compact

    @classmethod
    def _task_workspace_artifact_delivery_for_verifier(
        cls,
        delivery: Mapping[str, Any],
        *,
        omit_embedded_evidence: bool = False,
    ) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key in ("source", "path", "status", "mode", "content_key"):
            if key in delivery:
                compact[key] = delivery.get(key)
        readback = delivery.get("readback")
        if isinstance(readback, Mapping):
            compact["readback"] = {
                key: readback.get(key)
                for key in ("path", "truncated")
                if key in readback
            }
        file_refs = delivery.get("file_refs")
        if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
            compact["file_refs"] = [
                cls._compact_artifact_ref_for_verifier(ref)
                for ref in list(file_refs)[:8]
                if isinstance(ref, Mapping)
            ]
            if len(file_refs) > 8:
                compact["file_refs"].append({"omitted": len(file_refs) - 8, "reason": "prompt_budget"})
        diagnostics = delivery.get("diagnostics")
        if diagnostics:
            compact["diagnostics"] = cls._task_workspace_artifact_hot_value(
                diagnostics,
                key_context="diagnostics",
                omit_embedded_evidence=omit_embedded_evidence,
            )
        draft_meta = delivery.get("draft_meta")
        if isinstance(draft_meta, Mapping):
            compact["draft_meta"] = {
                key: draft_meta.get(key)
                for key in ("status", "route")
                if key in draft_meta
            }
        return compact

    @classmethod
    def _task_workspace_artifact_display_path(cls, path: Any) -> str:
        """Project a private fallback carrier back to its requested logical path."""

        normalized = str(path or "").strip()
        parts = PurePosixPath(normalized).parts
        if len(parts) >= 4 and parts[:2] == (".agently", "files"):
            return PurePosixPath(*parts[3:]).as_posix()
        return normalized

    @classmethod
    def _task_workspace_artifact_final_result_from_refs(cls, refs: Sequence[Mapping[str, Any]]) -> str:
        paths = [
            cls._task_workspace_artifact_display_path(ref.get("path"))
            for ref in refs
            if str(ref.get("path") or "").strip()
        ]
        if not paths:
            return ""
        if len(paths) == 1:
            return f"TaskWorkspace artifact delivered at {paths[0]}; full content is available through file_refs/readback."
        return (
            "TaskWorkspace artifacts delivered at "
            + ", ".join(paths)
            + "; full content is available through file_refs/readback."
        )

    @classmethod
    def _final_result_is_task_workspace_artifact_pointer(
        cls,
        final_result: str,
        refs: Sequence[Mapping[str, Any]],
    ) -> bool:
        text = str(final_result or "").strip()
        if not text:
            return False
        if cls._looks_like_task_workspace_artifact_placeholder(text):
            return True
        for ref in refs:
            path = str(ref.get("path") or "").strip()
            if path and path in text:
                return True
        return False

    @classmethod
    def _candidate_final_result_from_execution_result(
        cls,
        execution_result: Any,
        *,
        include_answer: bool = True,
    ) -> str:
        if isinstance(execution_result, Mapping):
            candidates: list[str] = []
            for key in ("candidate_final_result", "final_result"):
                value = execution_result.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
            manifest = execution_result.get("artifact_manifest")
            if isinstance(manifest, Mapping):
                manifest_content = cls._task_workspace_artifact_manifest_content(manifest)
                if manifest_content.strip():
                    candidates.append(manifest_content.strip())
            keys: tuple[str, ...] = ("artifact_markdown", "artifact_html")
            if include_answer:
                keys = keys + ("answer", "result")
            for key in keys:
                value = execution_result.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
            if candidates:
                return max(candidates, key=len)
            if not include_answer:
                return ""
            step_result = execution_result.get("step_result")
            if isinstance(step_result, str) and len(step_result.strip()) > 200:
                return step_result.strip()
            remaining_work = execution_result.get("remaining_work")
            evidence = execution_result.get("evidence")
            if (
                not cls._has_remaining_work(remaining_work)
                and isinstance(evidence, Sequence)
                and not isinstance(evidence, str | bytes | bytearray)
            ):
                text_items = [item.strip() for item in evidence if isinstance(item, str) and item.strip()]
                if text_items:
                    longest = max(text_items, key=len)
                    if len(longest) > max(800, len(str(step_result or "")) * 3):
                        return longest
        if isinstance(execution_result, str) and execution_result.strip():
            return execution_result.strip()
        return ""

    @staticmethod
    def _has_remaining_work(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return any(bool(str(item).strip()) for item in value)
        return bool(value)

    @classmethod
    def _verification_execution_meta_summary(
        cls,
        execution_meta: Mapping[str, Any],
    ) -> dict[str, Any]:
        route = execution_meta.get("route")
        diagnostics = execution_meta.get("diagnostics")
        return {
            "status": str(execution_meta.get("status") or ""),
            "route": cls._compact_verifier_prompt_value(route, max_chars=1200),
            "diagnostics": cls._compact_verifier_prompt_value(diagnostics, max_chars=1200),
        }

    @classmethod
    def _evidence_ledger_from_execution_meta(
        cls,
        execution_meta: Mapping[str, Any],
        *,
        required_evidence_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        evidence_items: list[dict[str, Any]] = []
        pinned_evidence_ids = cls._pinned_evidence_ids_from_execution_meta(execution_meta)
        pinned_evidence_ids.update(required_evidence_ids or set())
        blocks = execution_meta.get("blocks")
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                block_evidence = dict(evidence)
                raw_block_items = block_evidence.get("evidence_items")
                if isinstance(raw_block_items, Sequence) and not isinstance(
                    raw_block_items, str | bytes | bytearray
                ):
                    block_evidence["evidence_items"] = cls._prioritized_verifier_evidence_items(
                        raw_block_items,
                        pinned_evidence_ids=pinned_evidence_ids,
                    )
                block_ledger = evidence_ledger_view(block_evidence, max_items=80, body_chars=2400)
                evidence_items.extend(
                    dict(item)
                    for item in block_ledger.get("items", [])
                    if isinstance(item, Mapping)
                )
        evidence_items.extend(cls._action_result_evidence_items_from_execution_meta(execution_meta))
        return evidence_ledger_view(
            {
                "evidence_items": cls._prioritized_verifier_evidence_items(
                    evidence_items,
                    pinned_evidence_ids=pinned_evidence_ids,
                )
            },
            max_items=120,
            body_chars=2400,
        )

    @classmethod
    def _pinned_evidence_ids_from_execution_meta(cls, execution_meta: Mapping[str, Any]) -> set[str]:
        pinned: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text:
                    pinned.add(text)
                return
            if isinstance(value, Mapping):
                for key in ("id", "evidence_id"):
                    text = str(value.get(key) or "").strip()
                    if text:
                        pinned.add(text)
                return
            if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                for item in value:
                    collect(item)

        collect(execution_meta.get("pinned_evidence_ids"))
        blocks = execution_meta.get("blocks")
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                collect(evidence.get("pinned_evidence_ids"))
        return pinned

    @staticmethod
    def _task_workspace_artifact_hot_path(item: Mapping[str, Any]) -> str:
        path = str(item.get("path") or "").strip().replace("\\", "/")
        return PurePosixPath(path).as_posix() if path else ""

    @staticmethod
    def _task_workspace_artifact_hot_source(item: Mapping[str, Any]) -> str:
        provenance = item.get("provenance")
        if isinstance(provenance, Mapping):
            source = str(provenance.get("source") or "").strip()
            if source:
                return source
        return str(item.get("source") or "").strip()

    @classmethod
    def _is_task_workspace_artifact_hot_item(cls, item: Mapping[str, Any]) -> bool:
        kind = str(item.get("kind") or "").strip()
        if kind.startswith("task_workspace_artifact."):
            return True
        if kind != "artifact_ref":
            return False
        role = str(item.get("role") or "").strip()
        source = cls._task_workspace_artifact_hot_source(item)
        return role == "task_workspace_artifact" or source.startswith("agent_task.")

    @staticmethod
    def _task_workspace_artifact_hot_identity(item: Mapping[str, Any]) -> tuple[str, str]:
        provenance = item.get("provenance")

        def value(field: str) -> str:
            direct = str(item.get(field) or "").strip()
            if direct:
                return direct
            if isinstance(provenance, Mapping):
                return str(provenance.get(field) or "").strip()
            return ""

        version = value("content_version_id") or value("snapshot_id")
        return version, value("sha256")

    def _current_task_workspace_artifact_hot_items(
        self,
        items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep historical artifact snapshots cold while projecting one current file state."""

        current_by_path: dict[str, tuple[str, str]] = {}
        for item in items:
            if not self._is_task_workspace_artifact_hot_item(item):
                continue
            kind = str(item.get("kind") or "").strip()
            if kind not in {"artifact_ref", "task_workspace_artifact.readback"}:
                continue
            path = self._task_workspace_artifact_hot_path(item)
            version, digest = self._task_workspace_artifact_hot_identity(item)
            if path and (version or digest) and path not in current_by_path:
                current_by_path[path] = (version, digest)

        projected: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        for item in items:
            candidate = dict(item)
            if not self._is_task_workspace_artifact_hot_item(candidate):
                projected.append(candidate)
                continue
            path = self._task_workspace_artifact_hot_path(candidate)
            current = current_by_path.get(path)
            if current is None:
                projected.append(candidate)
                continue
            version, digest = self._task_workspace_artifact_hot_identity(candidate)
            current_version, current_digest = current
            matches = bool(
                (version and current_version and version == current_version)
                or (digest and current_digest and digest == current_digest)
            )
            if matches:
                projected.append(candidate)
            else:
                filtered.append(
                    {
                        "id": candidate.get("id"),
                        "kind": candidate.get("kind"),
                        "path": path,
                        "content_version_id": version,
                        "sha256": digest,
                        "current_content_version_id": current_version,
                        "current_sha256": current_digest,
                    }
                )
        self.diagnostics["verifier_task_workspace_artifact_projection"] = {
            "current": {
                path: {"content_version_id": identity[0], "sha256": identity[1]}
                for path, identity in current_by_path.items()
            },
            "filtered_stale_count": len(filtered),
            "filtered_stale": DataFormatter.sanitize(filtered[:32]),
        }
        return projected

    @classmethod
    def _prioritized_verifier_evidence_items(
        cls,
        items: Sequence[Any],
        *,
        pinned_evidence_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        pinned = pinned_evidence_ids or set()
        ordered: list[tuple[int, int, int, dict[str, Any]]] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            sanitized = dict(DataFormatter.sanitize(item))
            identities = {
                str(sanitized.get(field) or "").strip()
                for field in ("id", "evidence_id", "reference_id", "cite_as")
                if str(sanitized.get(field) or "").strip()
            }
            pin_priority = -1 if identities.intersection(pinned) else 0
            ordered.append((pin_priority, cls._verifier_evidence_item_priority(sanitized), index, sanitized))
        ordered.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
        return [item for _, _, _, item in ordered]

    @staticmethod
    def _verifier_evidence_item_priority(item: Mapping[str, Any]) -> int:
        kind = str(item.get("kind") or "").strip().lower()
        if kind == "task_workspace_artifact.targeted_readback":
            return 0
        if kind == "task_workspace_artifact.acceptance_coverage":
            return 0
        if kind == "task_workspace_artifact.readback":
            return 1
        if kind == "task_workspace_artifact.acceptance_locator":
            return 2
        if kind.startswith("task_workspace_artifact."):
            return 3
        return 10

    @classmethod
    def _action_result_evidence_items_from_execution_meta(
        cls,
        execution_meta: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, record in enumerate(cls._collect_execution_action_records(execution_meta)):
            if not isinstance(record, Mapping):
                continue
            action_id = str(record.get("id") or record.get("name") or "").strip()
            if not action_id:
                continue
            result_preview = record.get("result_preview")
            error = record.get("error")
            progressive_transfer = (
                result_preview
                if (
                    isinstance(result_preview, Mapping)
                    and str(result_preview.get("owner") or "").strip()
                    and str(result_preview.get("locator") or "").strip()
                    and str(result_preview.get("content_version") or "").strip()
                    and "value" in result_preview
                )
                else None
            )
            # Progressive-disclosure metadata is typed evidence identity, not
            # part of the source body.  Keeping it beside the body avoids
            # spending the verifier's bounded content window on wrappers while
            # preserving the exact owner/version/range needed for no-progress
            # and provenance checks.
            body_value = (
                progressive_transfer.get("value")
                if isinstance(progressive_transfer, Mapping)
                else result_preview if result_preview not in (None, "", [], {}) else error
            )
            body = cls._action_result_evidence_body(body_value)
            access_blocked = cls._action_result_preview_access_blocked(body)
            status = cls._action_result_evidence_status(record, body=body)
            body_state = cls._action_result_evidence_body_state(record, status=status, body=body)
            action_call_id = str(record.get("action_call_id") or "").strip()
            evidence_id = cls._action_result_evidence_id(
                action_id=action_id,
                action_call_id=action_call_id,
                index=index,
                preview_sha=str(record.get("result_preview_sha256") or ""),
            )
            item: dict[str, Any] = {
                "id": evidence_id,
                "kind": "agent_task.action.result",
                "status": status,
                "raw_status": record.get("status", status),
                "body_state": body_state,
                "action_id": action_id,
                "action_call_id": action_call_id,
                "aliases": cls._action_result_evidence_aliases(record),
                "provenance": {
                    "source": "agent_task.execution_meta.action_logs",
                    "action_id": action_id,
                    "action_call_id": action_call_id,
                },
                "supports": {
                    "content": status == "ok" and body_state in {"full", "bounded", "truncated"},
                    "unavailability": status in {"failed", "empty"},
                    "ref_pointer": bool(action_call_id),
                },
            }
            input_preview = record.get("input_preview")
            if input_preview not in (None, "", [], {}):
                item["input_preview"] = DataFormatter.sanitize(input_preview)
            if isinstance(progressive_transfer, Mapping):
                for key in (
                    "owner",
                    "locator",
                    "content_version",
                    "range",
                    "total_bytes",
                    "next_offset",
                    "serialized_media_type",
                ):
                    value = progressive_transfer.get(key)
                    if value not in (None, "", [], {}):
                        item[key] = DataFormatter.sanitize(value)
                item["provenance"]["owner"] = str(progressive_transfer.get("owner") or "")
                item["provenance"]["locator"] = str(progressive_transfer.get("locator") or "")
                item["provenance"]["content_version"] = str(
                    progressive_transfer.get("content_version") or ""
                )
            if body:
                item["body"] = body
            if access_blocked:
                item["diagnostics"] = [
                    {
                        "code": "agent_task.action_result.access_blocked_preview",
                        "message": (
                            "Action preview appears to be an access-verification or anti-bot page; "
                            "it can support unavailability diagnostics only."
                        ),
                    }
                ]
            for ref in cls._collect_source_refs_from_action_records([record]):
                if not isinstance(ref, Mapping):
                    continue
                field = str(ref.get("field") or "").strip()
                value = str(ref.get("value") or "").strip()
                if field and value and item.get(field) in (None, "", [], {}):
                    item[field] = value
            items.append(DataFormatter.sanitize(item))
        return items

    @classmethod
    def _action_result_evidence_status(cls, record: Mapping[str, Any], *, body: str = "") -> str:
        status = str(record.get("status") or "").strip().lower()
        if status in {"failed", "failure", "error", "timed_out", "timeout", "blocked"} or record.get("error"):
            return "failed"
        if record.get("result_preview") in (None, "", [], {}):
            return "empty"
        if body and cls._action_result_preview_access_blocked(body):
            return "failed"
        return "ok"

    @staticmethod
    def _action_result_preview_access_blocked(body: str) -> bool:
        text = str(body or "").strip().lower()
        if not text:
            return False
        markers = (
            "cf_app_waf",
            "为了更好的访问体验",
            "请进行验证",
            "verify you are human",
            "human verification",
            "are you a human",
            "captcha challenge",
            "captcha verification",
            "complete the captcha",
            "access denied",
            "403 forbidden",
            "request blocked",
            "blocked by cloudflare",
            "blocked by security",
            "checking your browser",
            "please enable javascript",
            "enable javascript to continue",
            "security check to access",
            "cloudflare ray id",
        )
        return any(marker in text for marker in markers)

    @classmethod
    def _action_result_evidence_body_state(
        cls,
        record: Mapping[str, Any],
        *,
        status: str,
        body: str,
    ) -> str:
        if not body:
            return "ref_only" if status == "failed" else "ref_only"
        preview_meta = record.get("result_preview_meta")
        if isinstance(preview_meta, Mapping) and preview_meta.get("truncated") is True:
            return "truncated"
        return "bounded"

    @staticmethod
    def _action_result_evidence_body(value: Any) -> str:
        if value in (None, "", [], {}):
            return ""
        sanitized = DataFormatter.sanitize(value)
        if isinstance(sanitized, str):
            return sanitized
        try:
            return json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(sanitized)

    @staticmethod
    def _action_result_evidence_id(
        *,
        action_id: str,
        action_call_id: str,
        index: int,
        preview_sha: str,
    ) -> str:
        suffix = action_call_id or preview_sha or str(index)
        raw = f"agent_task_action_result:{action_id}:{suffix}"
        return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in raw)[:240]

    @staticmethod
    def _action_result_evidence_aliases(record: Mapping[str, Any]) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)

        action_id = str(record.get("id") or record.get("name") or "").strip()
        action_call_id = str(record.get("action_call_id") or "").strip()
        add(action_id)
        add(f"action_{action_id}")
        add(f"action_result_{action_id}")
        add(action_call_id)
        if action_call_id:
            add(f"action_{action_call_id}")
            add(f"action_result_{action_call_id}")
        for ref_key in ("artifact_refs", "file_refs"):
            refs = record.get(ref_key)
            if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
                continue
            for ref in refs:
                if not isinstance(ref, Mapping):
                    continue
                for field in ("artifact_id", "path", "record_id", "source_url", "selected_url", "url", "href"):
                    add(ref.get(field))
        return aliases[:24]

    def _cumulative_evidence_ledger(
        self,
        current_execution_meta: Mapping[str, Any],
        *,
        required_evidence_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        canonical_required_items: list[dict[str, Any]] = []
        expanded_required_ids = set(required_evidence_ids or set())
        for required_id in tuple(expanded_required_ids):
            try:
                resolved = self._task_references().resolve(required_id)
            except (KeyError, ValueError):
                continue
            for field in ("evidence_id", "reference_id"):
                identity = str(resolved.get(field) or "").strip()
                if identity:
                    expanded_required_ids.add(identity)
            target = resolved.get("target")
            if isinstance(target, Mapping):
                target_id = str(target.get("id") or "").strip()
                if target_id:
                    expanded_required_ids.add(target_id)
                canonical_target = dict(DataFormatter.sanitize(target))
                for field in ("evidence_id", "reference_id"):
                    identity = str(resolved.get(field) or "").strip()
                    if identity:
                        canonical_target[field] = identity
                canonical_required_items.append(canonical_target)
        pinned_evidence_ids = self._pinned_evidence_ids_from_execution_meta(current_execution_meta)
        pinned_evidence_ids.update(expanded_required_ids)
        current_ledger = self._evidence_ledger_from_execution_meta(
            current_execution_meta,
            required_evidence_ids=expanded_required_ids,
        )
        for item in current_ledger.get("items", []):
            if isinstance(item, Mapping):
                items.append(dict(item))
        for iteration in reversed(self.iterations):
            if not isinstance(iteration, Mapping):
                continue
            previous_meta = iteration.get("execution_meta")
            if not isinstance(previous_meta, Mapping):
                continue
            pinned_evidence_ids.update(self._pinned_evidence_ids_from_execution_meta(previous_meta))
            previous_ledger = self._evidence_ledger_from_execution_meta(
                previous_meta,
                required_evidence_ids=expanded_required_ids,
            )
            for item in previous_ledger.get("items", []):
                if isinstance(item, Mapping):
                    items.append(dict(item))
        # Candidate-used references resolve through the canonical task identity
        # owner.  Add that immutable target beside lossy cross-revision views so
        # duplicate selection below can keep the richest representation of the
        # same evidence rather than whichever revision happened to be visited
        # first.
        items.extend(canonical_required_items)
        deduped: list[dict[str, Any]] = []
        evidence_indexes: dict[str, int] = {}
        for item in items:
            evidence_id = str(item.get("id") or "").strip()
            existing_index = evidence_indexes.get(evidence_id) if evidence_id else None
            if existing_index is None:
                if evidence_id:
                    evidence_indexes[evidence_id] = len(deduped)
                deduped.append(item)
                continue
            existing = deduped[existing_index]
            if self._evidence_item_projection_quality(
                item
            ) > self._evidence_item_projection_quality(existing):
                deduped[existing_index] = item
        structured_required_items = self._preserve_required_structured_evidence_bodies(
            deduped,
            required_evidence_ids=expanded_required_ids,
        )
        hot_items = self._current_task_workspace_artifact_hot_items(
            structured_required_items
        )
        ledger = self._stable_evidence_ledger_view(
            {
                "evidence_items": self._prioritized_verifier_evidence_items(
                    hot_items,
                    pinned_evidence_ids=pinned_evidence_ids,
                )
            },
            max_items=_VERIFIER_LEDGER_MAX_ITEMS,
            body_chars=_VERIFIER_LEDGER_BODY_CHARS,
            max_overflow_refs=_VERIFIER_LEDGER_MAX_OVERFLOW_REFS,
        )
        # acceptance_locator_view is projected once beside the ledger in the
        # verifier request. Keeping another copy nested here duplicates its
        # locator metadata without adding evidence.
        ledger.pop("acceptance_locator_view", None)
        return ledger

    def _cumulative_execution_evidence_summary(self, current_execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        for iteration in self.iterations:
            if not isinstance(iteration, Mapping):
                continue
            previous_meta = iteration.get("execution_meta")
            if isinstance(previous_meta, Mapping):
                summaries.append(self._execution_log_summary(dict(previous_meta)))
        current_summary = self._execution_log_summary(dict(current_execution_meta))
        summaries.append(current_summary)

        combined: dict[str, Any] = {
            "model_response_count": 0,
            "action_log_count": 0,
            "action_ids": [],
            "action_statuses": {},
            "actions": [],
            "source_refs": [],
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "required_actions": sorted(self._task_contract_required_action_ids()),
            "capability_evidence_requirements": [],
            "missing_required_actions": [],
            "consumed_skill_ids": [],
            "required_skills": [],
            "missing_required_skill_context": [],
            "capabilities_used": [],
            "capability_evidence": {
                "actions": {"succeeded": [], "failed": []},
                "skills": {"selected": []},
                "artifacts": {"readback": []},
                "validations": {"passed": [], "failed": []},
            },
            "artifact_refs": [],
            "task_workspace_refs": {},
            "errors": [],
            "replan_signals": [],
            "current_replan_signals": [],
            "status": str(current_execution_meta.get("status") or ""),
        }

        for summary in summaries:
            combined["model_response_count"] += int(summary.get("model_response_count") or 0)
            actions = summary.get("actions")
            if isinstance(actions, list):
                combined["actions"].extend(actions)
            artifact_refs = summary.get("artifact_refs")
            if isinstance(artifact_refs, list):
                combined["artifact_refs"].extend(artifact_refs)
            source_refs = summary.get("source_refs")
            if isinstance(source_refs, list):
                combined["source_refs"].extend(source_refs)
            errors = summary.get("errors")
            if isinstance(errors, list):
                combined["errors"].extend(errors)
            replan_signals = summary.get("replan_signals")
            if isinstance(replan_signals, list):
                combined["replan_signals"].extend(replan_signals)
            task_workspace_refs = summary.get("task_workspace_refs")
            if isinstance(task_workspace_refs, Mapping):
                self._merge_task_workspace_ref_summary(combined["task_workspace_refs"], task_workspace_refs)
            for key in (
                "action_ids",
                "failed_actions",
                "blocked_actions",
                "approval_required_actions",
                "required_actions",
                "capability_evidence_requirements",
                "missing_required_actions",
                "consumed_skill_ids",
                "required_skills",
                "missing_required_skill_context",
                "capabilities_used",
            ):
                if key == "capability_evidence_requirements":
                    combined[key] = self._merge_capability_evidence_requirements(
                        combined.get(key),
                        summary.get(key),
                    )
                else:
                    combined[key] = self._merge_string_lists(combined.get(key), summary.get(key))
            action_statuses = summary.get("action_statuses")
            if isinstance(action_statuses, Mapping):
                combined["action_statuses"].update(DataFormatter.sanitize(action_statuses))
            capability_evidence = summary.get("capability_evidence")
            if isinstance(capability_evidence, Mapping):
                self._merge_capability_evidence_summary(combined["capability_evidence"], capability_evidence)

        combined["actions"] = self._dedupe_action_records(combined["actions"])
        combined["source_refs"] = self._dedupe_ref_records(combined["source_refs"])
        combined["action_log_count"] = len(combined["actions"])
        final_action_statuses = {
            str(action_id): str(status)
            for action_id, status in dict(combined.get("action_statuses") or {}).items()
            if str(action_id).strip()
        }
        combined["failed_actions"] = self._action_ids_by_final_status(
            final_action_statuses,
            {"failed", "failure", "error"},
        )
        combined["blocked_actions"] = self._action_ids_by_final_status(final_action_statuses, {"blocked"})
        combined["approval_required_actions"] = self._action_ids_by_final_status(
            final_action_statuses,
            {"approval_required"},
        )
        capability_evidence_summary = combined.get("capability_evidence")
        capability_actions = (
            capability_evidence_summary.get("actions") if isinstance(capability_evidence_summary, dict) else None
        )
        if isinstance(capability_actions, dict):
            capability_actions["failed"] = combined["failed_actions"]
        combined["artifact_refs"] = self._dedupe_ref_records(combined["artifact_refs"])
        combined["errors"] = self._dedupe_jsonable_records(combined["errors"])
        combined["replan_signals"] = self._dedupe_jsonable_records(combined["replan_signals"])
        current_replan_signals = current_summary.get("replan_signals")
        if isinstance(current_replan_signals, list):
            combined["current_replan_signals"] = self._dedupe_jsonable_records(current_replan_signals)
        return DataFormatter.sanitize(combined)

    @staticmethod
    def _merge_task_workspace_ref_summary(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            key_text = str(key)
            if isinstance(value, list):
                bucket = target.setdefault(key_text, [])
                if isinstance(bucket, list):
                    for item in value:
                        if item not in bucket:
                            bucket.append(item)
                continue
            if key_text not in target:
                target[key_text] = value

    @classmethod
    def _merge_capability_evidence_summary(cls, target: dict[str, Any], source: Mapping[str, Any]) -> None:
        for kind, value in source.items():
            if not isinstance(value, Mapping):
                continue
            bucket = target.setdefault(str(kind), {})
            if not isinstance(bucket, dict):
                continue
            for field, items in value.items():
                bucket[str(field)] = cls._merge_string_lists(bucket.get(str(field)), items)

    @classmethod
    def _compact_context_pack_for_verifier(cls, context_pack: "TaskContextView") -> dict[str, Any]:
        compact = cls._compact_verifier_prompt_value(
            cls._context_pack_for_verifier_hot_path(context_pack),
            max_chars=_VERIFIER_PROMPT_VALUE_CHARS,
        )
        return compact if isinstance(compact, dict) else {}

    @classmethod
    def _context_pack_for_verifier_hot_path(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            compact: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in {
                    "sha256",
                    "digest",
                    "bytes",
                    "read_bytes",
                    "size",
                    "media_type",
                    "content_kind",
                    "handler_id",
                }:
                    continue
                compact[key_text] = cls._context_pack_for_verifier_hot_path(item)
            return compact
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return [cls._context_pack_for_verifier_hot_path(item) for item in value]
        return value

    @classmethod
    def _compact_verifier_evidence_summary(
        cls,
        summary: Mapping[str, Any],
        *,
        include_body_previews: bool = False,
    ) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, value in summary.items():
            if key == "actions" and isinstance(value, list):
                if include_body_previews:
                    compact[key] = [cls._compact_action_record_for_verifier(ref) for ref in value[:16]]
                else:
                    compact[key] = [
                        {
                            "id": str(ref.get("id") or ref.get("name") or ""),
                            "action_call_id": str(ref.get("action_call_id") or ""),
                            "status": str(ref.get("status") or ""),
                        }
                        for ref in value[:16]
                        if isinstance(ref, Mapping)
                    ]
                if len(value) > 16:
                    compact[key].append({"omitted": len(value) - 16, "reason": "prompt_budget"})
                continue
            if key == "source_refs" and isinstance(value, list):
                compact[key] = [
                    cls._verifier_inspection_value_without_selection_ids(item)
                    for item in value[:24]
                ]
                if len(value) > 24:
                    compact[key].append(
                        {"omitted": len(value) - 24, "reason": "prompt_budget"}
                    )
                continue
            if not include_body_previews and key in {"artifact_refs", "task_workspace_refs"}:
                continue
            if key == "artifact_refs" and isinstance(value, list):
                compact[key] = [cls._compact_artifact_ref_for_verifier(ref) for ref in value[:24]]
                if len(value) > 24:
                    compact[key].append({"omitted": len(value) - 24, "reason": "prompt_budget"})
                continue
            if key == "task_workspace_refs" and isinstance(value, Mapping):
                compact[key] = cls._compact_verifier_prompt_value(
                    cls._task_workspace_artifact_hot_value(value),
                    max_chars=2400,
                )
                continue
            compact[key] = cls._compact_verifier_prompt_value(value, max_chars=_VERIFIER_PROMPT_ITEM_CHARS)
        return compact

    @classmethod
    def _verifier_inspection_value_without_selection_ids(cls, value: Any) -> Any:
        """Keep inspection facts while removing competing model selection ids."""

        identity_fields = {
            "id",
            "reference_id",
            "evidence_id",
            "cite_as",
            "selection_key",
            "criterion_id",
            "carrier_id",
            "content_version_id",
            "locator_id",
            "source_evidence_ids",
            "aliases",
            "artifact_id",
            "binding_id",
            "snapshot_id",
            "resource_id",
        }
        if isinstance(value, Mapping):
            return {
                str(key): cls._verifier_inspection_value_without_selection_ids(item)
                for key, item in value.items()
                if str(key) not in identity_fields
            }
        if isinstance(value, Sequence) and not isinstance(
            value,
            str | bytes | bytearray,
        ):
            return [
                cls._verifier_inspection_value_without_selection_ids(item)
                for item in value
            ]
        return cls._compact_verifier_prompt_value(
            value,
            max_chars=_VERIFIER_PROMPT_ITEM_CHARS,
        )

    @classmethod
    def _compact_artifact_ref_for_verifier(cls, ref: Any) -> Any:
        if not isinstance(ref, Mapping):
            return cls._compact_verifier_prompt_value(ref, max_chars=600)
        keep_keys = (
            "selection_key",
            "path",
            "requested_path",
            "role",
            "label",
            "artifact_type",
            "source",
            "source_url",
            "selected_url",
            "requested_url",
            "canonical_url",
            "url",
            "href",
            "record_id",
            "collection",
            "kind",
            "content_state",
            "readback_mode",
            "truncated",
            "available",
        )
        compact = {key: ref.get(key) for key in keep_keys if key in ref}
        if (
            str(ref.get("requested_path") or "").strip()
            and str(ref.get("task_workspace_id") or "").strip()
        ):
            compact["path"] = ref.get("requested_path")
        if not ref.get("selection_key"):
            for key in ("artifact_id", "action_call_id"):
                if key in ref:
                    compact[key] = ref.get(key)
        if "preview" in ref:
            compact["preview"] = cls._compact_verifier_prompt_value(ref.get("preview"), max_chars=600)
        if "content_preview" in ref:
            compact["content_preview"] = cls._compact_verifier_prompt_value(
                ref.get("content_preview"),
                max_chars=600,
            )
        return compact

    @classmethod
    def _compact_grounding_guard_for_verifier(cls, grounding_guard: Mapping[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "schema_version": grounding_guard.get("schema_version"),
            "valid": grounding_guard.get("valid"),
            "blocking_count": grounding_guard.get("blocking_count"),
            "checked_claims": grounding_guard.get("checked_claims"),
        }
        diagnostics = grounding_guard.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
            compact["diagnostics"] = [
                {
                    key: cls._compact_verifier_prompt_value(
                        item.get(key),
                        max_chars=800,
                    )
                    for key in (
                        "code",
                        "message",
                        "blocking",
                        "field",
                        "status",
                        "support_type",
                    )
                    if key in item
                }
                for item in list(diagnostics)[:12]
                if isinstance(item, Mapping)
            ]
            if len(diagnostics) > 12:
                compact["diagnostics"].append({"omitted": len(diagnostics) - 12, "reason": "prompt_budget"})
        available_ids = grounding_guard.get("available_evidence_ids")
        if isinstance(available_ids, Sequence) and not isinstance(available_ids, str | bytes | bytearray):
            compact["available_evidence_count"] = len(available_ids)
        return DataFormatter.sanitize(compact)

    @classmethod
    def _compact_action_record_for_verifier(cls, record: Any) -> Any:
        if not isinstance(record, Mapping):
            return cls._compact_verifier_prompt_value(record, max_chars=900)
        keep_keys = (
            "id",
            "name",
            "status",
            "action_type",
            "kind",
            "action_call_id",
            "input_preview",
            "result_preview_meta",
        )
        compact: dict[str, Any] = {key: record.get(key) for key in keep_keys if key in record}
        result_preview_meta = compact.get("result_preview_meta")
        if isinstance(result_preview_meta, Mapping):
            compact["result_preview_meta"] = {
                key: result_preview_meta.get(key)
                for key in ("truncated", "omitted", "reason")
                if key in result_preview_meta
            }
        if "result_preview" in record:
            compact["result_preview"] = cls._compact_action_preview_value(record.get("result_preview"), max_chars=5200)
        if isinstance(record.get("artifact_refs"), list):
            refs = record.get("artifact_refs") or []
            compact["artifact_refs"] = [cls._compact_artifact_ref_for_verifier(ref) for ref in refs[:4]]
            if len(refs) > 4:
                compact["artifact_refs"].append({"omitted": len(refs) - 4, "reason": "prompt_budget"})
        if record.get("file_refs"):
            file_refs = record.get("file_refs")
            if isinstance(file_refs, Sequence) and not isinstance(file_refs, str | bytes | bytearray):
                compact["file_refs"] = [
                    cls._compact_artifact_ref_for_verifier(ref)
                    for ref in list(file_refs)[:4]
                    if isinstance(ref, Mapping)
                ]
                if len(file_refs) > 4:
                    compact["file_refs"].append({"omitted": len(file_refs) - 4, "reason": "prompt_budget"})
            else:
                compact["file_refs"] = cls._compact_verifier_prompt_value(file_refs, max_chars=1000)
        return compact

    @classmethod
    def _compact_action_preview_value(
        cls,
        value: Any,
        *,
        max_chars: int,
        depth: int = 0,
    ) -> Any:
        value = _omit_agent_task_request_payloads_from_hot_path(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls._truncate_prompt_text_middle(value, max_chars)
        if depth >= 4:
            return cls._truncate_prompt_text_middle(value, max_chars)
        if isinstance(value, list):
            limit = 12
            return [
                cls._compact_action_preview_value(item, max_chars=max(360, max_chars // 2), depth=depth + 1)
                for item in value[:limit]
            ] + ([{"omitted": len(value) - limit, "reason": "prompt_budget"}] if len(value) > limit else [])
        if isinstance(value, dict):
            compacted: dict[str, Any] = {}
            logical_task_workspace_path = (
                value.get("requested_path")
                if str(value.get("requested_path") or "").strip()
                and str(value.get("task_workspace_id") or "").strip()
                else None
            )
            for index, (key, item) in enumerate(value.items()):
                if index >= 36:
                    compacted["omitted"] = {"count": len(value) - 36, "reason": "prompt_budget"}
                    break
                key_text = str(key)
                if key_text == "path" and logical_task_workspace_path is not None:
                    item = logical_task_workspace_path
                item_chars = max_chars
                if key_text in {"content", "raw", "text", "output", "result", "data", "body", "preview"}:
                    item_chars = max(1600, max_chars)
                compacted[key_text] = cls._compact_action_preview_value(
                    item,
                    max_chars=item_chars,
                    depth=depth + 1,
                )
            return compacted
        return cls._truncate_prompt_text_middle(value, max_chars)

    @classmethod
    def _compact_verifier_prompt_value(
        cls,
        value: Any,
        *,
        max_chars: int = _VERIFIER_PROMPT_ITEM_CHARS,
        depth: int = 0,
    ) -> Any:
        value = _omit_agent_task_request_payloads_from_hot_path(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, bytes):
            text = value[:max_chars].decode("utf-8", "replace")
            return {"bytes": len(value), "preview": cls._truncate_prompt_text(text, max_chars)}
        if isinstance(value, str):
            return cls._truncate_prompt_text(value, max_chars)
        if depth >= 5:
            return cls._truncate_prompt_text(value, max_chars)
        if isinstance(value, list):
            limit = 24
            items = [
                cls._compact_verifier_prompt_value(
                    item,
                    max_chars=max(240, max_chars // 2),
                    depth=depth + 1,
                )
                for item in value[:limit]
            ]
            if len(value) > limit:
                items.append({"omitted": len(value) - limit, "reason": "prompt_budget"})
            return items
        if isinstance(value, dict):
            limit = 48
            compacted: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= limit:
                    compacted["omitted"] = {"count": len(value) - limit, "reason": "prompt_budget"}
                    break
                key_text = str(key)
                item_chars = max_chars
                if key_text in {"content", "raw", "text", "output", "result", "data", "body", "preview"}:
                    item_chars = max(240, max_chars // 3)
                compacted[key_text] = cls._compact_verifier_prompt_value(
                    item,
                    max_chars=item_chars,
                    depth=depth + 1,
                )
            return compacted
        return cls._truncate_prompt_text(value, max_chars)

    @staticmethod
    def _truncate_prompt_text(value: Any, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 32)].rstrip() + "\n[truncated for verifier prompt]"

    @staticmethod
    def _truncate_prompt_text_middle(value: Any, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        marker = "\n[truncated middle for verifier prompt]\n"
        available = max(0, max_chars - len(marker))
        head = max(1, available // 2)
        tail = max(1, available - head)
        return text[:head].rstrip() + marker + text[-tail:].lstrip()

    def _normalize_verification(
        self,
        verification: dict[str, Any],
        *,
        execution_evidence_summary: dict[str, Any],
        candidate_final_result: str = "",
        grounding_guard: Mapping[str, Any] | None = None,
        terminal_candidate: Mapping[str, Any] | None = None,
        offered_reference_ids: set[str] | None = None,
        terminal: bool = True,
    ) -> dict[str, Any]:
        normalized: dict[str, Any] = {
            "is_complete": terminal and self._normalize_bool(verification.get("is_complete"), default=False),
            "requires_block": self._normalize_bool(verification.get("requires_block"), default=False),
            "reason": str(verification.get("reason") or ""),
            "failure_analysis": str(
                verification.get("failure_analysis") or (verification.get("reason") if terminal else "") or ""
            ),
            "acceptance_delta": self._normalize_string_list(verification.get("acceptance_delta")),
            "missing_criteria": self._normalize_string_list(verification.get("missing_criteria")),
            "replan_instruction": str(verification.get("replan_instruction") or ""),
            "repair_constraints": self._normalize_string_list(verification.get("repair_constraints")),
            "next_step_requirements": self._normalize_string_list(verification.get("next_step_requirements")),
            "final_result": str(verification.get("final_result") or ""),
        }
        guard_reasons: list[str] = []
        if isinstance(grounding_guard, Mapping):
            normalized["grounding_guard"] = DataFormatter.sanitize(dict(grounding_guard))
            if int(grounding_guard.get("blocking_count") or 0) > 0 or grounding_guard.get("valid") is False:
                normalized["is_complete"] = False
                guard_reasons.append("evidence_ledger_grounding_guard_failed")
                guard_messages = [
                    str(item.get("message") or item.get("code") or "")
                    for item in grounding_guard.get("diagnostics", [])
                    if isinstance(item, Mapping) and item.get("blocking") is True
                ]
                if not guard_messages:
                    guard_messages = ["Evidence ledger grounding guard found invalid claim-to-evidence bindings."]
                normalized["missing_criteria"] = self._merge_string_lists(
                    normalized.get("missing_criteria"),
                    guard_messages,
                )
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    guard_messages,
                )
        if normalized["requires_block"]:
            normalized["is_complete"] = False
            guard_reasons.append("requires_block_true")
        if normalized["missing_criteria"]:
            normalized["is_complete"] = False
            guard_reasons.append("missing_criteria_present")
        if isinstance(terminal_candidate, Mapping):
            criterion_audit = self._validate_criterion_audit(
                verification,
                offered_reference_ids=offered_reference_ids,
            )
            normalized["criterion_audit"] = criterion_audit
            normalized["criterion_checks"] = criterion_audit.get("checks", [])
            if criterion_audit.get("valid") is not True:
                normalized["is_complete"] = False
                criterion_repair_contract = criterion_audit.get("repair_contract")
                normalized["criterion_repair_contract"] = DataFormatter.sanitize(
                    criterion_repair_contract
                    if isinstance(criterion_repair_contract, Mapping)
                    else {}
                )
                criterion_guard = (
                    "criterion_audit_invalid"
                    if criterion_audit.get("structural_errors")
                    else "criterion_unsatisfied"
                )
                guard_reasons.append(criterion_guard)
                criterion_messages = [
                    str(
                        item.get("summary")
                        or "; ".join(self._normalize_string_list(item.get("gaps")))
                        or item.get("criterion")
                        or ""
                    ).strip()
                    for item in criterion_audit.get("failed_checks", [])
                    if isinstance(item, Mapping)
                ]
                criterion_messages.extend(
                    str(error.get("message") or "").strip()
                    for error in criterion_audit.get("structural_errors", [])
                    if isinstance(error, Mapping)
                )
                criterion_messages = [message for message in criterion_messages if message]
                normalized["missing_criteria"] = self._merge_string_lists(
                    normalized.get("missing_criteria"),
                    criterion_messages,
                )
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    criterion_messages,
                )
            material_claim_audit = self._validate_material_claim_audit(
                verification,
                terminal_candidate=terminal_candidate,
                offered_reference_ids=offered_reference_ids,
            )
            normalized["material_claim_audit"] = material_claim_audit
            normalized["material_claim_coverage_complete"] = material_claim_audit.get(
                "coverage_complete"
            )
            normalized["material_claim_checks"] = material_claim_audit.get("checks", [])
            if material_claim_audit.get("valid") is not True:
                normalized["is_complete"] = False
                repair_contract = material_claim_audit.get("repair_contract")
                normalized["material_claim_repair_contract"] = DataFormatter.sanitize(
                    repair_contract if isinstance(repair_contract, Mapping) else {}
                )
                audit_guard = (
                    "material_claim_audit_invalid"
                    if material_claim_audit.get("structural_errors")
                    else "material_claim_audit_failed"
                )
                guard_reasons.append(audit_guard)
                messages = [
                    str(item.get("artifact_quote") or item.get("reason") or "").strip()
                    for item in material_claim_audit.get("failed_checks", [])
                    if isinstance(item, Mapping)
                ]
                messages.extend(
                    str(error.get("message") or "; ".join(error.get("messages") or [])).strip()
                    for error in material_claim_audit.get("structural_errors", [])
                    if isinstance(error, Mapping)
                )
                messages = [message for message in messages if message]
                if not messages:
                    messages = ["Material factual claims did not pass the terminal semantic audit."]
                normalized["missing_criteria"] = self._merge_string_lists(
                    normalized.get("missing_criteria"),
                    messages,
                )
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    messages,
                )
        final_result_required = self._normalize_bool(verification.get("final_result_required"), default=False)
        trusted_task_workspace_artifact_refs = self._trusted_task_workspace_artifact_refs_from_summary(execution_evidence_summary)
        risky_actions, non_blocking_failed_actions = self._execution_risk_actions(execution_evidence_summary)
        if non_blocking_failed_actions:
            normalized["non_blocking_failed_actions"] = non_blocking_failed_actions
        execution_status = str(execution_evidence_summary.get("status") or "").strip().lower()
        if execution_status in {"failed", "error", "timed_out", "blocked"}:
            execution_errors = execution_evidence_summary.get("errors", [])
            error_message = ""
            if isinstance(execution_errors, list) and execution_errors:
                first_error = execution_errors[0]
                if isinstance(first_error, dict):
                    error_message = str(first_error.get("message") or first_error.get("type") or "")
                else:
                    error_message = str(first_error)
            detail = f": {error_message}" if error_message else ""
            liveness_diagnostic = self._execution_status_liveness_diagnostic(
                execution_status=execution_status,
                execution_evidence_summary=execution_evidence_summary,
                verification=verification,
                normalized=normalized,
                trusted_task_workspace_artifact_refs=trusted_task_workspace_artifact_refs,
                grounding_guard=grounding_guard,
                final_result_required=final_result_required,
                non_blocking_action_ids=non_blocking_failed_actions,
            )
            if liveness_diagnostic is not None:
                normalized["non_blocking_execution_status"] = liveness_diagnostic
                self.diagnostics.setdefault("non_blocking_execution_status", []).append(
                    {"task_id": self.id, **liveness_diagnostic}
                )
            else:
                normalized["is_complete"] = False
                guard_reasons.append("execution_status_failed")
                normalized["missing_criteria"] = [
                    *normalized["missing_criteria"],
                    f"Execution step status is {execution_status}{detail}.",
                ]
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    [f"Execution step status is {execution_status}{detail}."],
                )
        raw_current_replan_signals = execution_evidence_summary.get("current_replan_signals")
        raw_replan_signals = (
            raw_current_replan_signals
            if isinstance(raw_current_replan_signals, list)
            else execution_evidence_summary.get("replan_signals", [])
        )
        replan_signals = [dict(signal) for signal in raw_replan_signals if isinstance(signal, dict)]
        if replan_signals:
            normalized["replan_signals"] = replan_signals
        blocking_signal = next(
            (signal for signal in replan_signals if str(signal.get("status") or "") == "blocked"),
            None,
        )
        if blocking_signal is not None:
            normalized["is_complete"] = False
            normalized["requires_block"] = True
            guard_reasons.append("structured_replan_signal_blocked")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                str(blocking_signal.get("reason") or "Execution emitted a blocked ReplanSignal."),
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [str(blocking_signal.get("reason") or "Execution emitted a blocked ReplanSignal.")],
            )
        actionable_signals = [
            signal
            for signal in replan_signals
            if str(signal.get("status") or "") in {"repair", "replan_segment", "replan_goal", "clarify"}
        ]
        if actionable_signals:
            normalized["is_complete"] = False
            guard_reasons.append("structured_replan_signal")
            if not normalized["replan_instruction"]:
                reasons = [
                    str(signal.get("reason") or signal.get("status") or "")
                    for signal in actionable_signals
                    if str(signal.get("reason") or signal.get("status") or "").strip()
                ]
                normalized["replan_instruction"] = "Handle structured ReplanSignal before accepting completion" + (
                    f": {'; '.join(reasons)}." if reasons else "."
                )
        if risky_actions:
            normalized["is_complete"] = False
            guard_reasons.append("execution_risk_actions_present")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Unresolved execution risk actions: {', '.join(risky_actions)}",
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Unresolved execution risk actions: {', '.join(risky_actions)}"],
            )
        # Accumulate satisfied capabilities before evaluating the whole-task
        # evidence contract. Terminal preflight uses this same structural owner.
        self._accumulate_capability_evidence(execution_evidence_summary)
        required_actions = self._merge_string_lists(
            execution_evidence_summary.get("required_actions"),
            sorted(self._task_contract_required_action_ids()),
        )
        required_skills = self._normalize_string_list(execution_evidence_summary.get("required_skills"))
        missing_required = [
            *[action_id for action_id in required_actions if action_id not in self._satisfied_required_actions],
            *[skill_id for skill_id in required_skills if skill_id not in self._satisfied_required_skills],
        ]
        if terminal and missing_required:
            normalized["is_complete"] = False
            guard_reasons.append("required_capability_evidence_missing")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Missing required capability evidence: {', '.join(missing_required)}",
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Missing required capability evidence: {', '.join(missing_required)}"],
            )
        # Load-bearing structured capability-evidence gate
        # (AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC). A deterministic
        # correspondence check between authored, structured requirements (which
        # capabilities must appear in execution evidence) and the accumulated
        # capability/action evidence. It is independent of capability mode: a
        # model_decision skill (or any capability) the goal depends on can be
        # required as evidence here WITHOUT forcing routing. No prose or model
        # reading participates in the pass/fail decision. Only the kinds with a
        # deterministic check are enforced; reserved kinds are recorded as
        # unenforced diagnostics for the advisory model verifier.
        missing_capability_evidence, unenforced_requirements = self._evaluate_capability_evidence(
            execution_evidence_summary
        )
        if terminal and missing_capability_evidence:
            normalized["is_complete"] = False
            guard_reasons.append("capability_evidence_missing")
            normalized["missing_criteria"] = [
                *normalized["missing_criteria"],
                f"Missing required capability evidence: {', '.join(missing_capability_evidence)}",
            ]
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [f"Missing required capability evidence: {', '.join(missing_capability_evidence)}"],
            )
        missing_required = [*missing_required, *missing_capability_evidence]
        if unenforced_requirements:
            self.diagnostics.setdefault("unenforced_evidence_requirements", []).extend(unenforced_requirements)
        normalized["missing_required_capabilities"] = missing_required
        normalized["missing_capability_evidence"] = missing_capability_evidence
        # Surface unenforced requirements so a reserved or not-yet-wired evidence
        # kind is visible rather than a silent no-op (it does not block).
        normalized["unenforced_evidence_requirements"] = unenforced_requirements
        normalized["final_result_required"] = final_result_required
        if normalized["is_complete"] and final_result_required and not normalized["final_result"].strip():
            if candidate_final_result.strip():
                normalized["final_result"] = candidate_final_result.strip()
            elif trusted_task_workspace_artifact_refs:
                normalized["final_result"] = self._task_workspace_artifact_final_result_from_refs(
                    trusted_task_workspace_artifact_refs
                )
                normalized["final_result_via_task_workspace_artifact"] = True
            else:
                normalized["is_complete"] = False
                guard_reasons.append("final_result_missing")
                normalized["missing_criteria"] = [
                    *normalized["missing_criteria"],
                    "Final result is missing.",
                ]
                normalized["acceptance_delta"] = self._merge_string_lists(
                    normalized.get("acceptance_delta"),
                    ["Final result is missing."],
                )
        if normalized["is_complete"]:
            final_result_text = normalized["final_result"].strip()
            final_result_is_artifact_pointer = self._final_result_is_task_workspace_artifact_pointer(
                final_result_text,
                trusted_task_workspace_artifact_refs,
            )
            if final_result_text and not final_result_is_artifact_pointer:
                output_contract = self._parse_final_result_output_contract(final_result_text)
                if output_contract is not None and output_contract["parse_success"] is not True:
                    normalized["is_complete"] = False
                    guard_reasons.append("final_result_output_parse_failed")
                    attempts = output_contract.get("attempts", [])
                    attempted_formats = [
                        str(item.get("format"))
                        for item in attempts
                        if isinstance(item, Mapping) and str(item.get("format") or "").strip()
                    ]
                    format_note = (
                        " -> ".join(attempted_formats) if attempted_formats else output_contract["declared_format"]
                    )
                    message = (
                        "Final result must parse as a dict for the declared execution output contract "
                        f"(tried {format_note})."
                    )
                    last_error = str(output_contract.get("last_error") or "").strip()
                    if last_error:
                        message = f"{message} Last parser error: {last_error}."
                    normalized["missing_criteria"] = self._merge_string_lists(
                        normalized.get("missing_criteria"),
                        [message],
                    )
                    normalized["acceptance_delta"] = self._merge_string_lists(
                        normalized.get("acceptance_delta"),
                        [message],
                    )
                    if not normalized["replan_instruction"]:
                        normalized["replan_instruction"] = (
                            "Run another bounded step and produce a final_result that can be parsed as a dict "
                            "for the declared execution output contract; use JSON if the requested format fails."
                        )
                elif output_contract is not None and output_contract.get("resolved_format") != output_contract.get(
                    "declared_format"
                ):
                    self.diagnostics.setdefault("final_result_output_contract", []).append(
                        {
                            "task_id": self.id,
                            "declared_format": output_contract.get("declared_format"),
                            "resolved_format": output_contract.get("resolved_format"),
                            "fallback": output_contract.get("fallback"),
                        }
                    )
            elif final_result_is_artifact_pointer and trusted_task_workspace_artifact_refs:
                normalized["final_result_via_task_workspace_artifact"] = True
        continuation = self._untried_read_action_continuation(execution_evidence_summary)
        if normalized["requires_block"] and continuation:
            normalized["requires_block"] = False
            guard_reasons = [reason for reason in guard_reasons if reason != "requires_block_true"]
            guard_reasons.append("untried_read_action_available")
            normalized["continuation_opportunities"] = continuation
            untried = ", ".join(continuation.get("untried_action_ids") or [])
            message = "Verifier requested blocking, but read-only evidence capabilities remain untried" + (
                f": {untried}." if untried else "."
            )
            normalized["acceptance_delta"] = self._merge_string_lists(
                normalized.get("acceptance_delta"),
                [message],
            )
            normalized["missing_criteria"] = self._merge_string_lists(
                normalized.get("missing_criteria"),
                [message],
            )
            if not normalized["replan_instruction"]:
                normalized["replan_instruction"] = "Plan another bounded evidence-gathering step before blocking."
            self.diagnostics.setdefault("verification_continuations", []).append(
                {
                    "task_id": self.id,
                    "reason": continuation.get("reason"),
                    "untried_action_ids": continuation.get("untried_action_ids", []),
                    "failed_read_action_ids": continuation.get("failed_read_action_ids", []),
                }
            )
        if guard_reasons:
            self._align_guarded_verification_fields(normalized, guard_reasons, verification, terminal=terminal)
            normalized["guard_reasons"] = guard_reasons
            if not normalized["replan_instruction"]:
                normalized["replan_instruction"] = (
                    "Run another bounded step and produce explicit evidence for the guarded criteria."
                )
            self.diagnostics.setdefault("verification_guards", []).append(
                {
                    "task_id": self.id,
                    "guard_reasons": guard_reasons,
                    "missing_criteria": normalized["missing_criteria"],
                }
            )
        normalized["repair_constraints"] = self._merge_string_lists(
            normalized.get("repair_constraints"),
            normalized.get("missing_criteria"),
        )
        normalized["next_step_requirements"] = self._merge_string_lists(
            normalized.get("next_step_requirements"),
            [normalized.get("replan_instruction")] if normalized.get("replan_instruction") else [],
        )
        normalized["acceptance_delta"] = self._merge_string_lists(
            normalized.get("acceptance_delta"),
            normalized.get("missing_criteria"),
        )
        raw_replan_signal = verification.get("replan_signal")
        normal_progress = not terminal and not guard_reasons and not any(
            normalized.get(key)
            for key in ("failure_analysis", "missing_criteria", "acceptance_delta", "repair_constraints")
        )
        default_replan_status = (
            "continue"
            if normalized.get("is_complete") is True or normal_progress
            else ("blocked" if normalized.get("requires_block") is True else "repair")
        )
        signal_value: dict[str, Any]
        if isinstance(raw_replan_signal, Mapping):
            signal_value = dict(DataFormatter.sanitize(raw_replan_signal))
        else:
            signal_value = {
                "status": default_replan_status,
                "reason": str(normalized.get("reason") or ""),
            }
        raw_status = str(signal_value.get("status") or "").strip()
        material_claim_repair_contract = normalized.get(
            "material_claim_repair_contract"
        )
        raw_material_requirements = (
            material_claim_repair_contract.get("requirements")
            if isinstance(material_claim_repair_contract, Mapping)
            else None
        )
        required_claim_evidence_reacquisition = bool(
            isinstance(raw_material_requirements, Sequence)
            and not isinstance(
                raw_material_requirements,
                str | bytes | bytearray,
            )
            and any(
                isinstance(requirement, Mapping)
                and str(requirement.get("repair_policy") or "").strip()
                == "evidence_reacquisition_required"
                for requirement in raw_material_requirements
            )
        )
        if required_claim_evidence_reacquisition:
            normalized["required_claim_evidence_reacquisition"] = True
            if raw_status in {"", "continue", "repair"}:
                signal_value["status"] = "replan_segment"
                signal_value["reason"] = (
                    "A success-criterion-required material claim lacks bindable "
                    "evidence and cannot be repaired by deleting the claim."
                )
                raw_status = "replan_segment"
        if normalized.get("is_complete") is True:
            signal_value["status"] = "continue"
        elif normalized.get("requires_block") is True:
            if raw_status not in {"blocked", "clarify"}:
                signal_value["status"] = "blocked"
        elif not normal_progress and raw_status in {"", "continue", "blocked", "clarify"}:
            signal_value["status"] = "repair"
        try:
            replan_signal = ReplanSignal.from_value(signal_value)
        except (TypeError, ValueError):
            replan_signal = ReplanSignal(
                status=cast(Any, default_replan_status),
                reason=str(normalized.get("reason") or "") or None,
            )
        normalized_replan_signal = replan_signal.to_dict()
        if offered_reference_ids is not None:
            normalized_replan_signal["evidence_refs"] = [
                ref
                for ref in normalized_replan_signal.get("evidence_refs", [])
                if str(ref) in offered_reference_ids
            ]
        normalized["replan_signal"] = normalized_replan_signal
        for key, value in verification.items():
            normalized.setdefault(key, DataFormatter.sanitize(value))
        return normalized

    def _execution_risk_actions(
        self,
        execution_evidence_summary: Mapping[str, Any],
    ) -> tuple[list[str], list[str]]:
        failed_actions = self._normalize_string_list(execution_evidence_summary.get("failed_actions"))
        blocked_actions = self._normalize_string_list(execution_evidence_summary.get("blocked_actions"))
        approval_required_actions = self._normalize_string_list(
            execution_evidence_summary.get("approval_required_actions")
        )
        required_actions = {
            *self._normalize_string_list(execution_evidence_summary.get("required_actions")),
            *self._task_contract_required_action_ids(),
        }
        for requirement in self._capability_evidence_requirements(execution_evidence_summary):
            if not requirement.get("required", True):
                continue
            if str(requirement.get("kind") or "capability_used") != "action_succeeded":
                continue
            capability_id = str(requirement.get("capability_id") or "").strip()
            if capability_id:
                required_actions.add(capability_id)

        read_safe_actions = {
            str(item.get("id") or "").strip()
            for item in self._planner_capabilities()
            if isinstance(item, Mapping)
            and str(item.get("kind") or "").strip() == "action"
            and str(item.get("id") or "").strip()
            and (
                str(item.get("side_effect_level") or "").strip().lower() == "read"
                or bool(item.get("replay_safe")) is True
            )
        }
        framework_diagnostic_actions = {"action_loop", "action_planning"}
        risky_failed: list[str] = []
        non_blocking_failed: list[str] = []
        for action_id in failed_actions:
            if (
                action_id in framework_diagnostic_actions
                or action_id in read_safe_actions
            ) and action_id not in required_actions:
                non_blocking_failed.append(action_id)
            else:
                risky_failed.append(action_id)
        risky_blocked: list[str] = []
        for action_id in blocked_actions:
            if action_id in framework_diagnostic_actions and action_id not in required_actions:
                non_blocking_failed.append(action_id)
            else:
                risky_blocked.append(action_id)
        risky_actions = self._merge_string_lists(
            risky_failed,
            [*risky_blocked, *approval_required_actions],
        )
        return risky_actions, non_blocking_failed

    def _parse_final_result_output_contract(self, final_result_text: str) -> dict[str, Any] | None:
        execution_prompt = self._execution_prompt_context()
        output_schema = execution_prompt.get("output")
        if not isinstance(output_schema, Mapping) or not output_schema:
            return None
        output_format = str(execution_prompt.get("output_format") or "").strip().lower()
        if output_format in {"", "json_object", "application/json"}:
            output_format = "json"
        if output_format == "auto":
            output_format = "json"
        if output_format not in {"json", "flat_markdown", "hybrid", "xml_field", "yaml_literal"}:
            return None

        attempts: list[dict[str, Any]] = []
        for format_name in [output_format, *([] if output_format == "json" else ["json"])]:
            parsed, error = self._parse_final_result_by_format(
                final_result_text,
                output_schema=dict(output_schema),
                output_format=format_name,
            )
            attempt = {"format": format_name, "success": parsed is not None}
            if error:
                attempt["error"] = error
            attempts.append(attempt)
            if parsed is not None:
                return {
                    "parse_success": True,
                    "declared_format": output_format,
                    "resolved_format": format_name,
                    "attempts": attempts,
                    "fallback": (
                        {
                            "from": output_format,
                            "to": format_name,
                            "reason": attempts[0].get("error"),
                        }
                        if format_name != output_format
                        else None
                    ),
                }

        last_error = ""
        for attempt in reversed(attempts):
            if attempt.get("error"):
                last_error = str(attempt["error"])
                break
        return {
            "parse_success": False,
            "declared_format": output_format,
            "resolved_format": output_format,
            "attempts": attempts,
            "last_error": last_error,
        }

    @staticmethod
    def _parse_final_result_by_format(
        final_result_text: str,
        *,
        output_schema: dict[str, Any],
        output_format: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return parse_output_contract_dict(
            final_result_text,
            output_schema=output_schema,
            output_format=output_format,
        )

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, tuple):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @classmethod
    def _merge_string_lists(cls, *values: Any) -> list[str]:
        merged: list[str] = []
        for value in values:
            for item in cls._normalize_string_list(value):
                if item not in merged:
                    merged.append(item)
        return merged


__all__ = ["AgentTaskVerificationMixin"]
