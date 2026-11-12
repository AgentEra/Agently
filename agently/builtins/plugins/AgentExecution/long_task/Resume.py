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

from agently.core.TaskWorkspace import TaskWorkspace, TaskWorkspaceContextSource
from agently.core.application.SkillLibrary import (
    SkillBinding,
    SkillContextSource,
    SkillLibrary,
)
from agently.core.context import TaskContext
from agently.core.storage import RecordStore, RecordStoreContextSource
from agently.types.data import (
    ContextBlock,
    ContextConsumption,
    ContextDiagnostic,
    ContextOmission,
    ContextPackage,
)

from .LifecycleState import AgentTaskLifecycleState
from .TaskShared import (
    AgentTaskEffectiveExecutionStrategy,
    AgentTaskMixinBase,
    Any,
    BaseAgent,
    cast,
    DataFormatter,
    Mapping,
    os,
    Sequence,
    TaskBoardRevision,
    TaskReferenceCatalog,
    TerminalConvergenceState,
    _AgentTaskT,
    _compact_agent_task_error_message,
    _normalize_agent_task_max_iterations,
)
from .TaskEvidenceContextSource import TaskEvidenceContextSource


def _context_package_from_dict(value: Mapping[str, Any]) -> ContextPackage:
    raw_blocks = value.get("blocks")
    blocks = tuple(
        ContextBlock(
            block_id=str(item.get("block_id") or ""),
            block_key=str(item.get("block_key") or ""),
            source_id=str(item.get("source_id") or ""),
            source_revision=str(item.get("source_revision") or ""),
            source_ref=str(item.get("source_ref") or ""),
            binding_id=str(item.get("binding_id") or ""),
            role=cast(Any, item.get("role")),
            content=item.get("content"),
            completeness=cast(Any, item.get("completeness")),
            content_chars=int(item.get("content_chars") or 0),
            required=bool(item.get("required")),
            refs=tuple(str(ref) for ref in item.get("refs") or ()),
            metadata=cast(Mapping[str, Any], item.get("metadata") or {}),
        )
        for item in raw_blocks or ()
        if isinstance(item, Mapping)
    )
    raw_omissions = value.get("omissions")
    omissions = tuple(
        ContextOmission(
            block_key=str(item.get("block_key") or ""),
            reason=str(item.get("reason") or ""),
            required=bool(item.get("required")),
            source_ref=(
                str(item.get("source_ref"))
                if item.get("source_ref") is not None
                else None
            ),
            details=cast(Mapping[str, Any], item.get("details") or {}),
        )
        for item in raw_omissions or ()
        if isinstance(item, Mapping)
    )
    raw_diagnostics = value.get("diagnostics")
    diagnostics = tuple(
        ContextDiagnostic(
            code=str(item.get("code") or ""),
            message=str(item.get("message") or ""),
            details=cast(Mapping[str, Any], item.get("details") or {}),
        )
        for item in raw_diagnostics or ()
        if isinstance(item, Mapping)
    )
    return ContextPackage(
        package_id=str(value.get("package_id") or ""),
        task_context_id=str(value.get("task_context_id") or ""),
        context_revision=int(value.get("context_revision") or 0),
        consumer_id=str(value.get("consumer_id") or ""),
        phase=str(value.get("phase") or ""),
        source_revisions=cast(Mapping[str, str], value.get("source_revisions") or {}),
        source_coverage=cast(
            Mapping[str, Mapping[str, Any]],
            value.get("source_coverage") or {},
        ),
        blocks=blocks,
        omissions=omissions,
        diagnostics=diagnostics,
    )


def _context_consumption_from_dict(value: Mapping[str, Any]) -> ContextConsumption:
    return ContextConsumption(
        consumption_id=str(value.get("consumption_id") or ""),
        package_id=str(value.get("package_id") or ""),
        request_id=str(value.get("request_id") or ""),
        consumer_id=str(value.get("consumer_id") or ""),
        phase=str(value.get("phase") or ""),
        block_ids=tuple(str(item) for item in value.get("block_ids") or ()),
    )


class AgentTaskResumeMixin(AgentTaskMixinBase):
    @staticmethod
    def _resume_run_id(task_id: str) -> str:
        # Namespaced so resume snapshots never mix with the task's per-step
        # observation checkpoints under the bare task_id.
        return f"{ task_id }::resume"

    def _resume_manifest(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "success_criteria": list(self.success_criteria),
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "task_shape_analysis": DataFormatter.sanitize(self.task_shape_analysis),
            "max_iterations": self.max_iterations,
            "verify": self.verify,
            "context_profile": self.context_profile,
            "context_budget": DataFormatter.sanitize(self.context_budget),
            "limits": DataFormatter.sanitize(self.limits),
            "options": DataFormatter.sanitize(self.options),
        }

    def _context_resume_state(self) -> dict[str, Any]:
        snapshot = self.task_context.snapshot()
        sources: list[dict[str, Any]] = []
        for binding in snapshot.bindings:
            source = self.task_context._binding_source(binding.binding_id)
            descriptor: dict[str, Any] = {
                "binding": binding.to_dict(),
                "source_type": f"{source.__class__.__module__}.{source.__class__.__qualname__}",
            }
            if isinstance(source, TaskWorkspaceContextSource):
                descriptor.update(
                    {
                        "kind": "task_workspace",
                        "execution_id": source.task_workspace.execution_id,
                    }
                )
            elif isinstance(source, RecordStoreContextSource):
                descriptor["kind"] = "record_store"
            elif isinstance(source, TaskEvidenceContextSource):
                descriptor["kind"] = "task_evidence"
            elif isinstance(source, SkillContextSource):
                descriptor.update(
                    {
                        "kind": "skill_library",
                        "skill_bindings": [
                            {
                                "binding_id": item.binding_id,
                                "task_id": item.task_id,
                                "canonical_ref": item.canonical_ref,
                                "revision": item.revision,
                                "revision_ref": item.revision_ref,
                                "mode": item.mode,
                                "scope": item.scope,
                            }
                            for item in source.bindings
                        ],
                    }
                )
            else:
                descriptor["kind"] = "unsupported"
            sources.append(descriptor)
        return {
            "schema_version": "agent_task_context_resume/v1",
            "task_context": snapshot.to_dict(),
            "sources": sources,
            "readers": [
                reader._export_state()
                for reader in self.context_readers.values()
            ],
            "packages": [
                package.to_dict()
                for package in self.context_packages
                if isinstance(package, ContextPackage)
            ],
            "consumptions": [
                consumption.to_dict()
                for consumption in self.context_consumptions
                if isinstance(consumption, ContextConsumption)
            ],
        }

    @staticmethod
    def _resume_task_workspace(
        task_workspace: TaskWorkspace,
        context_state: Mapping[str, Any],
    ) -> TaskWorkspace:
        raw_sources = context_state.get("sources")
        if not isinstance(raw_sources, Sequence) or isinstance(
            raw_sources,
            str | bytes | bytearray,
        ):
            return task_workspace
        execution_id = ""
        for descriptor in raw_sources:
            if not isinstance(descriptor, Mapping):
                continue
            if descriptor.get("kind") == "task_workspace":
                execution_id = str(descriptor.get("execution_id") or "").strip()
                break
        if not execution_id or execution_id == task_workspace.execution_id:
            return task_workspace
        return TaskWorkspace(
            task_workspace.root,
            mode=task_workspace.mode,
            create=True,
            execution_id=execution_id,
        )

    @classmethod
    def _restore_task_context(
        cls,
        agent: "BaseAgent",
        context_state: Mapping[str, Any],
        *,
        task_workspace: TaskWorkspace,
        record_store: RecordStore,
        task_references: TaskReferenceCatalog,
    ) -> TaskContext:
        raw_snapshot = context_state.get("task_context")
        raw_sources = context_state.get("sources")
        if not isinstance(raw_snapshot, Mapping) or not isinstance(raw_sources, Sequence) or isinstance(
            raw_sources,
            str | bytes | bytearray,
        ):
            raise ValueError("AgentTask resume snapshot has no restorable TaskContext contract.")
        task_context = TaskContext(
            task_id=str(raw_snapshot.get("task_id") or ""),
            context_id=str(raw_snapshot.get("context_id") or ""),
        )
        source_by_binding: dict[str, Mapping[str, Any]] = {}
        for descriptor in raw_sources:
            if not isinstance(descriptor, Mapping):
                continue
            binding = descriptor.get("binding")
            if isinstance(binding, Mapping):
                source_by_binding[str(binding.get("binding_id") or "")] = descriptor
        raw_bindings = raw_snapshot.get("bindings")
        for binding in raw_bindings or ():
            if not isinstance(binding, Mapping):
                continue
            binding_id = str(binding.get("binding_id") or "")
            descriptor = source_by_binding.get(binding_id)
            if descriptor is None:
                raise ValueError(f"TaskContext source descriptor is missing for {binding_id!r}.")
            kind = str(descriptor.get("kind") or "")
            if kind == "task_workspace":
                source: Any = TaskWorkspaceContextSource(task_workspace)
            elif kind == "record_store":
                source = RecordStoreContextSource(record_store)
            elif kind == "task_evidence":
                source = TaskEvidenceContextSource(task_references)
            elif kind == "skill_library":
                library = getattr(agent, "skill_library", None)
                if not isinstance(library, SkillLibrary):
                    raise ValueError("AgentTask Skill Context resume requires the original SkillLibrary.")
                raw_skill_bindings = descriptor.get("skill_bindings")
                if not isinstance(raw_skill_bindings, Sequence) or isinstance(
                    raw_skill_bindings,
                    str | bytes | bytearray,
                ):
                    raise ValueError("AgentTask Skill Context resume has no exact Skill bindings.")
                skill_bindings = tuple(
                    SkillBinding(
                        binding_id=str(item.get("binding_id") or ""),
                        task_id=str(item.get("task_id") or ""),
                        canonical_ref=str(item.get("canonical_ref") or ""),
                        revision=str(item.get("revision") or ""),
                        revision_ref=str(item.get("revision_ref") or ""),
                        mode=cast(Any, item.get("mode")),
                        scope=str(item.get("scope") or "task"),
                    )
                    for item in raw_skill_bindings
                    if isinstance(item, Mapping)
                )
                source = SkillContextSource(library, bindings=skill_bindings)
            else:
                source_type = str(descriptor.get("source_type") or "unknown")
                raise ValueError(
                    f"AgentTask cannot durably restore ContextSource {source_type!r}."
                )
            expected_source_id = str(binding.get("source_id") or "")
            if str(source.source_id) != expected_source_id:
                raise ValueError(
                    f"Restored ContextSource identity changed for {binding_id!r}."
                )
            if kind == "skill_library" and str(source.source_revision) != str(
                binding.get("source_revision") or ""
            ):
                raise ValueError(
                    f"Restored Skill Context revision changed for {binding_id!r}."
                )
            task_context.attach(
                source,
                binding_id=binding_id,
                required=bool(binding.get("required")),
                priority=int(binding.get("priority") or 0),
                scope=str(binding.get("scope") or "task"),
                metadata=cast(Mapping[str, Any], binding.get("metadata") or {}),
            )
        raw_entries = raw_snapshot.get("entries")
        for entry in raw_entries or ():
            if not isinstance(entry, Mapping):
                continue
            task_context.put(
                role=cast(Any, entry.get("role")),
                content=entry.get("content"),
                entry_id=str(entry.get("entry_id") or ""),
                required=bool(entry.get("required")),
                source_ref=(
                    str(entry.get("source_ref"))
                    if entry.get("source_ref") is not None
                    else None
                ),
                priority=int(entry.get("priority") or 0),
                metadata=cast(Mapping[str, Any], entry.get("metadata") or {}),
            )
        if task_context.revision != int(raw_snapshot.get("revision") or 0):
            raise ValueError("Restored TaskContext revision does not match its durable snapshot.")
        return task_context

    def _restore_context_history(self, context_state: Mapping[str, Any]) -> None:
        raw_packages = context_state.get("packages")
        self.context_packages = [
            _context_package_from_dict(item)
            for item in raw_packages or ()
            if isinstance(item, Mapping)
        ]
        raw_consumptions = context_state.get("consumptions")
        self.context_consumptions = [
            _context_consumption_from_dict(item)
            for item in raw_consumptions or ()
            if isinstance(item, Mapping)
        ]
        raw_readers = context_state.get("readers")
        if not isinstance(raw_readers, Sequence) or isinstance(
            raw_readers,
            str | bytes | bytearray,
        ):
            return
        for state in raw_readers:
            if not isinstance(state, Mapping):
                continue
            raw_consumer = state.get("consumer")
            if not isinstance(raw_consumer, Mapping):
                continue
            consumer_id = str(raw_consumer.get("consumer_id") or "")
            phase = str(state.get("phase") or "")
            reader = self.task_context.restore_reader(
                state,
                packages=self.context_packages,
                semantic_selector=self._task_context_semantic_selector(),
            )
            self.context_readers[(consumer_id, phase)] = reader

    async def _write_resume_snapshot(self, iteration_index: int, verification: dict[str, Any]) -> None:
        """Persist a resumable snapshot keyed by task_id after an iteration.

        Stores the task manifest, the last completed iteration, the bounded
        iteration summaries, the cumulative satisfied-capability sets, and the
        last verification outcome so a crashed task can continue (or report its
        terminal result) from a fresh process.
        """
        # AgentTask process state is run-local by default. Hosts explicitly opt
        # into durable RecordStore recovery when cross-process resume is needed.
        if not bool(self._agent_task_option("record_store_recovery", False)):
            return
        try:
            if self.record_store is None:
                return
            await self.record_store.put_snapshot(
                self._resume_run_id(self.id),
                DataFormatter.sanitize(
                    {
                        "resume_version": 3,
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "manifest": self._resume_manifest(),
                        "context_state": self._context_resume_state(),
                        "iterations_summary": self._iteration_prompt_summaries(),
                        "reflection_summaries": self._reflection_prompt_summaries(),
                        "satisfied_required_actions": sorted(self._satisfied_required_actions),
                        "satisfied_required_skills": sorted(self._satisfied_required_skills),
                        "satisfied_capabilities": sorted(self._satisfied_capabilities),
                        "satisfied_succeeded_actions": sorted(self._satisfied_succeeded_actions),
                        "failed_execution_shapes": sorted(self._failed_execution_shapes),
                        "task_reference_catalog": self._task_reference_catalog.snapshot(),
                        "terminal_convergence": self._terminal_convergence_state.snapshot(),
                        "lifecycle_state": self._lifecycle_state.to_dict(),
                        "last_verification": {
                            "is_complete": bool(verification.get("is_complete")),
                            "requires_block": bool(verification.get("requires_block")),
                            "status": "completed" if bool(verification.get("is_complete")) else "",
                            "accepted": bool(verification.get("is_complete")),
                            "artifact_status": "accepted" if bool(verification.get("is_complete")) else "",
                            "reason": str(verification.get("reason") or ""),
                            "final_result": str(verification.get("final_result") or ""),
                            "final_response": str(verification.get("final_response") or ""),
                            "missing_criteria": self._normalize_string_list(verification.get("missing_criteria")),
                        },
                    }
                ),
                step_id=f"iteration-{ iteration_index }",
            )
        except Exception as error:
            # Snapshot persistence must never break the task loop.
            self.diagnostics.setdefault("resume_snapshot_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                }
            )

    async def _write_taskboard_resume_snapshot(
        self,
        *,
        stage: str,
        tick_index: int,
        iteration_index: int | None = None,
        revision: Any,
        evidence_view: Mapping[str, Any],
        runtime_topology: Mapping[str, Any],
        acceptance_index: Mapping[str, Any] | None = None,
        handoff_projection: Mapping[str, Any] | None = None,
        terminal_reason: str | None = None,
        final_result: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a TaskBoard resumable snapshot keyed by task_id.

        The public resume surface stays AgentExecution/AgentTask-owned; this
        snapshot gives the TaskBoard coordinator enough cold state to expose a
        terminal result or continue from a board revision without repeating
        completed cards.
        """
        # See _write_resume_snapshot: TaskBoard ticks persist only when the host
        # explicitly requests cross-process recovery.
        if not bool(self._agent_task_option("record_store_recovery", False)):
            return
        final_result = final_result if isinstance(final_result, Mapping) else {}
        try:
            resume_iteration = int(
                self._lifecycle_state.iteration
                if iteration_index is None
                else iteration_index
            )
        except (TypeError, ValueError):
            resume_iteration = int(self._lifecycle_state.iteration or 0)
        status = str(final_result.get("status") or self.status or "").strip().lower()
        accepted = bool(final_result.get("accepted"))
        reason = str(final_result.get("reason") or terminal_reason or "")
        final_result_text = str(final_result.get("final_result") or "")
        is_complete = accepted and status in {"completed", "success", "accepted"}
        requires_block = (not is_complete) and status in {
            "blocked",
            "failed",
            "error",
            "timed_out",
            "max_iterations",
            "partial",
        }
        try:
            effective_revision = TaskBoardRevision.from_value(revision)
            if self.record_store is None:
                return
            await self.record_store.put_snapshot(
                self._resume_run_id(self.id),
                DataFormatter.sanitize(
                    {
                        "resume_version": 3,
                        "task_id": self.id,
                        # Keep the outer AgentTask iteration separate from the
                        # TaskBoard's internal tick counter.  A single outer
                        # iteration may contain many board ticks.
                        "iteration": resume_iteration,
                        "manifest": self._resume_manifest(),
                        "context_state": self._context_resume_state(),
                        "iterations_summary": self._iteration_prompt_summaries(),
                        "reflection_summaries": self._reflection_prompt_summaries(),
                        "satisfied_required_actions": sorted(self._satisfied_required_actions),
                        "satisfied_required_skills": sorted(self._satisfied_required_skills),
                        "satisfied_capabilities": sorted(self._satisfied_capabilities),
                        "satisfied_succeeded_actions": sorted(self._satisfied_succeeded_actions),
                        "failed_execution_shapes": sorted(self._failed_execution_shapes),
                        "task_reference_catalog": self._task_reference_catalog.snapshot(),
                        "terminal_convergence": self._terminal_convergence_state.snapshot(),
                        "lifecycle_state": self._lifecycle_state.to_dict(),
                        "taskboard_state": {
                            "schema_version": "agent_task_taskboard_resume/v1",
                            "stage": stage,
                            "tick_index": int(tick_index),
                            "outer_iteration": resume_iteration,
                            "status": status or self.status,
                            "terminal_reason": terminal_reason,
                            "revision": effective_revision.to_dict(),
                            "evidence_view": evidence_view,
                            "acceptance_index": dict(acceptance_index or {}),
                            "handoff_projection": dict(handoff_projection or {}),
                            "runtime_topology": dict(runtime_topology),
                            "read_progress": DataFormatter.sanitize(
                                self._taskboard_read_progress
                            ),
                            "record_refs": DataFormatter.sanitize(self.record_refs),
                            "final_result": dict(final_result),
                        },
                        "last_verification": {
                            "is_complete": is_complete,
                            "requires_block": requires_block,
                            "status": status,
                            "accepted": accepted,
                            "artifact_status": final_result.get("artifact_status"),
                            "reason": reason,
                            "final_result": final_result_text,
                            "final_response": str(final_result.get("final_response") or ""),
                            "missing_criteria": self._normalize_string_list(final_result.get("missing_criteria")),
                        },
                    }
                ),
                step_id=f"taskboard-{stage}-{tick_index}",
            )
        except Exception as error:
            self.diagnostics.setdefault("resume_snapshot_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "strategy": "taskboard",
                    "stage": stage,
                    "tick_index": tick_index,
                }
            )

    @classmethod
    async def async_resume(
        cls: type[_AgentTaskT],
        agent: "BaseAgent",
        task_id: str,
        *,
        task_workspace: str | os.PathLike[str] | TaskWorkspace | None = None,
        record_store: RecordStore | None = None,
    ) -> _AgentTaskT:
        """Rebuild an AgentTask from its latest durable snapshot.

        The returned task continues from the iteration after the last completed
        one (or, when the last snapshot was already terminal, exposes that
        terminal result without re-running). Completed iterations are not
        re-executed, so their side effects are not repeated; an iteration that
        was in flight at crash time is re-planned.
        """
        agent_any = cast(Any, agent)
        if isinstance(task_workspace, (str, os.PathLike)):
            agent_any.use_task_workspace(task_workspace)
        bound_task_workspace = (
            task_workspace
            if isinstance(task_workspace, TaskWorkspace)
            else getattr(agent, "task_workspace", None)
        )
        bound_record_store = record_store or getattr(agent, "record_store", None)
        if not isinstance(bound_record_store, RecordStore):
            raise RuntimeError(
                "AgentTask.async_resume requires a record-store binding."
            )
        state = await bound_record_store.get_snapshot(cls._resume_run_id(str(task_id)))
        if not isinstance(state, dict):
            raise ValueError(f"No resumable AgentTask snapshot was found for task_id '{ task_id }'.")
        return cls._from_resume_state(
            agent, task_id, state, task_workspace=bound_task_workspace, record_store=bound_record_store,
        )

    @classmethod
    def _from_resume_state(
        cls: type[_AgentTaskT], agent: "BaseAgent", task_id: str, state: dict[str, Any], *,
        task_workspace: TaskWorkspace | None, record_store: RecordStore,
    ) -> _AgentTaskT:
        """Rebind trusted built-in task state without loading executable code."""
        bound_task_workspace = task_workspace
        bound_record_store = record_store
        manifest = state.get("manifest")
        if not isinstance(manifest, dict) or not manifest.get("goal"):
            raise ValueError(f"No resumable AgentTask snapshot was found for task_id '{ task_id }'.")
        context_state = state.get("context_state")
        if not isinstance(context_state, Mapping):
            raise ValueError(
                "AgentTask snapshot predates the TaskContext durable contract and cannot be resumed."
            )
        if not isinstance(bound_task_workspace, TaskWorkspace):
            raise RuntimeError("AgentTask.async_resume requires a TaskWorkspace binding.")
        restored_task_workspace = cls._resume_task_workspace(
            bound_task_workspace,
            context_state,
        )
        restored_record_store = bound_record_store._bind_execution(
            str(task_id),
            scope={"task_id": str(task_id), "execution_id": str(task_id)},
            search_scope={"task_id": str(task_id), "execution_id": str(task_id)},
        )
        raw_task_reference_catalog = state.get("task_reference_catalog")
        restored_task_references = (
            TaskReferenceCatalog.from_snapshot(
                str(task_id),
                raw_task_reference_catalog,
            )
            if isinstance(raw_task_reference_catalog, Mapping)
            else TaskReferenceCatalog(str(task_id))
        )
        restored_task_context = cls._restore_task_context(
            agent,
            context_state,
            task_workspace=restored_task_workspace,
            record_store=restored_record_store,
            task_references=restored_task_references,
        )
        task = cast(
            _AgentTaskT,
            cast(Any, cls)(
                agent,
                goal=str(manifest.get("goal") or ""),
                success_criteria=list(manifest.get("success_criteria") or []),
                execution=cast(Any, manifest.get("execution_strategy", "auto")),
                record_store=restored_record_store,
                task_context=restored_task_context,
                task_workspace=restored_task_workspace,
                max_iterations=_normalize_agent_task_max_iterations(manifest.get("max_iterations")),
                verify=cast(Any, manifest.get("verify", "before_done")),
                context_profile=str(manifest.get("context_profile", "auto")),
                context_budget=cast(Any, manifest.get("context_budget")),
                limits=cast(Any, manifest.get("limits")),
                options=cast(Any, manifest.get("options")),
                task_id=str(task_id),
            ),
        )
        task_any = cast(Any, task)
        # The restored TaskEvidenceContextSource already points at this exact
        # catalog object.  Keep the task identity owner and retrieval adapter
        # joined instead of replacing either side after Task construction.
        task_any._task_reference_catalog = restored_task_references
        task_any._restore_context_history(context_state)
        terminal_convergence = state.get("terminal_convergence")
        if isinstance(terminal_convergence, Mapping):
            task_any._terminal_convergence_state = TerminalConvergenceState.from_snapshot(
                str(task_id),
                terminal_convergence,
            )
        lifecycle_state = state.get("lifecycle_state")
        if isinstance(lifecycle_state, Mapping):
            restored_lifecycle_state = AgentTaskLifecycleState.from_dict(lifecycle_state)
            if restored_lifecycle_state.task_id != str(task_id):
                raise ValueError("AgentTask lifecycle snapshot task_id does not match the resume target.")
            task_any._lifecycle_state = restored_lifecycle_state
        task_any._resumed_from_iteration = int(state.get("iteration") or 0)
        effective_execution_strategy = manifest.get("effective_execution_strategy")
        if effective_execution_strategy in {"flat", "taskboard"}:
            task_any.effective_execution_strategy = cast(
                AgentTaskEffectiveExecutionStrategy, effective_execution_strategy
            )
        task_shape_analysis = manifest.get("task_shape_analysis")
        if isinstance(task_shape_analysis, dict):
            task_any.task_shape_analysis = DataFormatter.sanitize(task_shape_analysis)
        taskboard_state = state.get("taskboard_state")
        if isinstance(taskboard_state, dict):
            task_any._resumed_taskboard_state = DataFormatter.sanitize(taskboard_state)
            read_progress = taskboard_state.get("read_progress")
            if (
                isinstance(read_progress, Mapping)
                and read_progress.get("schema_version")
                == "agent_task_taskboard_read_progress/v1"
                and isinstance(read_progress.get("items"), Mapping)
            ):
                task_any._taskboard_read_progress = DataFormatter.sanitize(
                    read_progress
                )
            # New TaskBoard snapshots carry the outer AgentTask iteration
            # explicitly.  Older snapshots only had the tick counter in the
            # top-level field; keep their recorded value for compatibility.
            if "outer_iteration" in taskboard_state:
                try:
                    task_any._resumed_from_iteration = int(
                        taskboard_state.get("outer_iteration")
                        or task_any._resumed_from_iteration
                        or 0
                    )
                except (TypeError, ValueError):
                    pass
        summaries = state.get("iterations_summary")
        task_any._resumed_iteration_summaries = list(summaries) if isinstance(summaries, list) else []
        reflection_summaries = state.get("reflection_summaries")
        if isinstance(reflection_summaries, list):
            task_any.reflections = [
                DataFormatter.sanitize(item) for item in reflection_summaries if isinstance(item, dict)
            ]
        task_any._satisfied_required_actions = set(cls._normalize_string_list(state.get("satisfied_required_actions")))
        task_any._satisfied_required_skills = set(cls._normalize_string_list(state.get("satisfied_required_skills")))
        task_any._satisfied_capabilities = set(cls._normalize_string_list(state.get("satisfied_capabilities")))
        task_any._satisfied_succeeded_actions = set(
            cls._normalize_string_list(state.get("satisfied_succeeded_actions"))
        )
        task_any._failed_execution_shapes = set(cls._normalize_string_list(state.get("failed_execution_shapes")))
        last_verification = state.get("last_verification")
        if isinstance(last_verification, dict):
            taskboard_should_retry = (
                isinstance(taskboard_state, dict)
                and manifest.get("effective_execution_strategy") == "taskboard"
                and bool(last_verification.get("requires_block"))
            )
            if not taskboard_should_retry:
                task_any._resumed_prior_result = cls._terminal_result_from_resume(
                    task_id=str(task_id),
                    resumed_from_iteration=task_any._resumed_from_iteration,
                    last_verification=last_verification,
                )
        return task

    def resume(self, *args: Any, **kwargs: Any):
        raise TypeError("Use the async classmethod AgentTask.async_resume(agent, task_id, ...).")

    @classmethod
    def _terminal_result_from_resume(
        cls,
        *,
        task_id: str,
        resumed_from_iteration: int,
        last_verification: dict[str, Any],
    ) -> dict[str, Any] | None:
        if bool(last_verification.get("is_complete")):
            final_result = last_verification.get("final_result") or ""
            artifact_status = last_verification.get("artifact_status") or "accepted"
            return {
                "status": str(last_verification.get("status") or "completed") or "completed",
                "accepted": bool(last_verification.get("accepted", True)),
                "artifact_status": artifact_status,
                "task_id": task_id,
                "final_result": final_result,
                "final_response": cls._agent_task_user_final_response(
                    final=last_verification,
                    accepted=True,
                    artifact_status=str(artifact_status),
                    status=str(last_verification.get("status") or "completed") or "completed",
                    reason=str(last_verification.get("reason") or ""),
                    missing_criteria=last_verification.get("missing_criteria", []),
                    final_result=final_result,
                ),
                "iterations": resumed_from_iteration,
                "resumed": True,
            }
        if bool(last_verification.get("requires_block")):
            artifact_status = last_verification.get("artifact_status") or "blocked"
            reason = last_verification.get("reason") or "Verifier blocked the task."
            return {
                "status": str(last_verification.get("status") or "blocked") or "blocked",
                "accepted": False,
                "artifact_status": artifact_status,
                "task_id": task_id,
                "reason": reason,
                "final_response": cls._agent_task_user_final_response(
                    final=last_verification,
                    accepted=False,
                    artifact_status=str(artifact_status),
                    status=str(last_verification.get("status") or "blocked") or "blocked",
                    reason=str(reason),
                    missing_criteria=last_verification.get("missing_criteria", []),
                    final_result=last_verification.get("final_result") or "",
                ),
                "iterations": resumed_from_iteration,
                "resumed": True,
            }
        return None


__all__ = ["AgentTaskResumeMixin"]
