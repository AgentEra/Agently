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

from agently_stage import default_stage_call_bridge

from agently.types.data import ContextConsumption, ContextPackage

from .TaskShared import *


class AgentTaskObservationMixin(AgentTaskMixinBase):
    def _memory_process_ref(
        self,
        kind: str,
        *,
        iteration: int | None = None,
        phase: str | None = None,
    ) -> "RecordRef":
        """Return a compact run-local locator without writing TaskWorkspace or RecordStore state."""

        sequence = len(getattr(self, "_stream_items", []))
        suffix = phase or kind
        return cast(
            RecordRef,
            {
                "id": f"{self.id}:{suffix}:{iteration if iteration is not None else sequence}",
                "kind": kind,
                "storage": "memory",
                "task_id": self.id,
                "iteration": iteration,
            },
        )

    async def _record_decision(
        self,
        iteration_index: int,
        plan: dict[str, Any],
        context_pack: "TaskContextView",
    ) -> "RecordRef":
        _ = plan, context_pack
        return self._memory_process_ref("agent_task_decision", iteration=iteration_index, phase="decision")

    async def _record_observation(
        self,
        iteration_index: int,
        *,
        plan: dict[str, Any],
        decision_ref: "RecordRef",
        execution_result: Any,
        execution_meta: dict[str, Any],
    ) -> tuple["RecordRef", "RecordRef | None"]:
        _ = plan, decision_ref, execution_result
        record_ref = self._memory_process_ref(
            "agent_task_observation", iteration=iteration_index, phase="observation"
        )
        await self._emit_action_observation_events(iteration_index, execution_meta=execution_meta)
        return record_ref, None

    async def _record_taskboard_checkpoint(
        self,
        *,
        stage: str,
        tick_index: int,
        revision: Any,
        runtime_topology: Mapping[str, Any] | None = None,
        terminal_reason: str | None = None,
        final_result: Mapping[str, Any] | None = None,
    ) -> tuple["RecordRef | None", "RecordRef | None"]:
        try:
            effective_revision = TaskBoardRevision.from_value(revision)
            evidence_view = build_task_board_evidence_view(effective_revision).to_dict()
            explicit_state_facts = task_board_explicit_state_facts(effective_revision, evidence_view=evidence_view)
            previous_acceptance_index = getattr(self, "_latest_taskboard_acceptance_index", None)
            if not isinstance(previous_acceptance_index, Mapping):
                revision_metadata = getattr(effective_revision, "metadata", {})
                previous_acceptance_index = (
                    revision_metadata.get("taskboard_acceptance_index")
                    if isinstance(revision_metadata, Mapping)
                    and isinstance(revision_metadata.get("taskboard_acceptance_index"), Mapping)
                    else None
                )
            verification_payload = {}
            if isinstance(final_result, Mapping):
                taskboard_result = final_result.get("taskboard")
                if isinstance(taskboard_result, Mapping) and isinstance(taskboard_result.get("final_verification"), Mapping):
                    verification_payload = taskboard_result["final_verification"]
                else:
                    verification_payload = final_result
            acceptance_index = build_task_board_acceptance_index(
                effective_revision,
                success_criteria=self.success_criteria,
                verification=verification_payload,
                evidence_view=evidence_view,
                explicit_state_facts=explicit_state_facts,
                previous_acceptance_index=previous_acceptance_index,
            )
            self._latest_taskboard_acceptance_index = DataFormatter.sanitize(acceptance_index)
            revision_id = str(effective_revision.revision_id)
            await self._write_taskboard_resume_snapshot(
                stage=stage,
                tick_index=tick_index,
                revision=effective_revision,
                evidence_view=evidence_view,
                acceptance_index=acceptance_index,
                runtime_topology=runtime_topology or {},
                terminal_reason=terminal_reason,
                final_result=final_result,
            )
            await self._emit(
                "agent_task.taskboard.tick_recorded",
                {
                    "stage": stage,
                    "tick_index": tick_index,
                    "revision_id": revision_id,
                    "terminal_reason": terminal_reason,
                    "status": str((final_result or {}).get("status") or self.status),
                },
            )
            return None, None
        except Exception as error:
            self.diagnostics.setdefault("taskboard_checkpoint_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "stage": stage,
                    "tick_index": tick_index,
                }
            )
            return None, None

    async def _record_verification(
        self,
        iteration_index: int,
        verification: dict[str, Any],
        observation_ref: "RecordRef",
    ) -> "RecordRef":
        _ = verification, observation_ref
        return self._memory_process_ref(
            "agent_task_verification", iteration=iteration_index, phase="verification"
        )

    def _reflection_density(self) -> str:
        agent_task_options = self.options.get("agent_task")
        effort = agent_task_options.get("effort") if isinstance(agent_task_options, Mapping) else None
        effort = effort if isinstance(effort, Mapping) else {}
        density = str(effort.get("reflection_density") or "").strip().lower()
        if density in {"final", "major_node", "action"}:
            return density
        name = str(effort.get("name") or self._taskboard_effort() or "medium").strip().lower()
        if name in {"minimal", "low", "fast"}:
            return "final"
        if name in {"high", "max"}:
            return "action"
        return "major_node"

    def _should_record_process_reflection(self, phase: str, *, plan: dict[str, Any]) -> bool:
        density = self._reflection_density()
        if density == "action":
            return phase in {"bounded_step", "major_node", "taskboard_card", "acp_call"}
        if density == "major_node":
            return phase in {"major_node", "taskboard_card"}
        if phase != "major_node":
            return False
        marker = plan.get("important") or plan.get("importance") or plan.get("reflection_required")
        return bool(marker is True or str(marker).strip().lower() in {"important", "high", "required"})

    def _bounded_step_reflection_summary(
        self,
        *,
        plan: dict[str, Any],
        execution_meta: dict[str, Any],
        execution_failed: bool,
    ) -> dict[str, Any]:
        return {
            "assessment": (
                "bounded step failed and requires repair" if execution_failed else "bounded step produced evidence"
            ),
            "status": "failed" if execution_failed else "observed",
            "execution_shape": plan.get("effective_execution_shape", plan.get("execution_shape", "")),
            "execution_id": execution_meta.get("execution_id"),
            "route": execution_meta.get("route", {}),
            "completion_evidence": False,
        }

    @staticmethod
    def _major_node_reflection_summary(*, verification: dict[str, Any]) -> dict[str, Any]:
        return {
            "assessment": str(verification.get("reason") or ""),
            "status": "accepted" if verification.get("is_complete") else "needs_replan",
            "missing_criteria": DataFormatter.sanitize(verification.get("missing_criteria", [])),
            "acceptance_delta": DataFormatter.sanitize(verification.get("acceptance_delta", [])),
            "completion_evidence": False,
        }

    async def _record_reflection(
        self,
        iteration_index: int,
        *,
        phase: str,
        subject_ref: "RecordRef | None",
        summary: dict[str, Any],
    ) -> "RecordRef | None":
        content = {
            "task_id": self.id,
            "iteration": iteration_index,
            "phase": phase,
            "reflection_density": self._reflection_density(),
            "summary": DataFormatter.sanitize(summary),
            "subject_ref": DataFormatter.sanitize(subject_ref),
            "completion_evidence": False,
        }
        _ = content, subject_ref
        record_ref = self._memory_process_ref(
            "agent_task_reflection", iteration=iteration_index, phase=f"reflection:{phase}"
        )
        reflection_summary = {
            "iteration": iteration_index,
            "phase": phase,
            "record_ref": record_ref,
            "summary": DataFormatter.sanitize(summary),
            "completion_evidence": False,
        }
        self.reflections.append(reflection_summary)
        await self._emit(
            f"agent_task.iteration.{iteration_index}.reflection.{phase}",
            {"record": record_ref, "summary": reflection_summary["summary"]},
        )
        return record_ref

    async def _ensure_final_reflection(self) -> None:
        if any(item.get("phase") == "final" for item in self.reflections if isinstance(item, dict)):
            return
        await self._record_reflection(
            max(0, len(self.iterations)),
            phase="final",
            subject_ref=None,
            summary={
                "assessment": str((self.result or {}).get("reason") or self.status),
                "status": self.status,
                "accepted": bool((self.result or {}).get("accepted")),
                "artifact_status": (self.result or {}).get("artifact_status"),
                "completion_evidence": False,
            },
        )

    def _append_record_ref(self, collection: str, ref: dict[str, Any] | None):
        if not ref:
            return
        bucket = self.record_refs.setdefault(collection, [])
        ref_id = str(ref.get("id") or "")
        if ref_id and ref_id not in bucket:
            bucket.append(ref_id)

    async def async_meta(self) -> dict[str, Any]:
        if not self._completed:
            await self.async_run()
        return {
            "task_id": self.id,
            "status": self.status,
            "goal": self.goal,
            "success_criteria": DataFormatter.sanitize(self.success_criteria),
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "task_shape_analysis": DataFormatter.sanitize(self.task_shape_analysis),
            "max_iterations": self.max_iterations,
            "iterations": DataFormatter.sanitize(self.iterations),
            "reflections": DataFormatter.sanitize(self.reflections),
            "guidance_items": DataFormatter.sanitize(getattr(self, "guidance_items", [])),
            "guidance_refs": DataFormatter.sanitize(self.record_refs.get("guidance", [])),
            "context_packages": DataFormatter.sanitize(
                [
                    package.to_dict()
                    for package in self.context_packages
                    if isinstance(package, ContextPackage)
                ]
            ),
            "context_consumptions": DataFormatter.sanitize(
                [
                    consumption.to_dict()
                    for consumption in self.context_consumptions
                    if isinstance(consumption, ContextConsumption)
                ]
            ),
            "resumed_from_iteration": self._resumed_from_iteration,
            "resumed_iteration_summaries": DataFormatter.sanitize(self._resumed_iteration_summaries),
            "result": DataFormatter.sanitize(self.result),
            "diagnostics": DataFormatter.sanitize(self.diagnostics),
            "record_refs": DataFormatter.sanitize(self.record_refs),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def _meta(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.async_meta())
        return self.async_meta()

    async def get_async_generator(
        self,
        type: Literal["delta", "instant", "streaming_parse", "all"] | str | None = "delta",
        content: Any = None,
        **__,
    ) -> AsyncGenerator[Any, None]:
        if content is not None and type is None:
            type = content
        text_projector = AgentExecutionTextDeltaProjector() if type == "delta" else None
        if self._completed:
            for item in self._stream_items:
                projected = self._project_stream_item(item, type, text_projector=text_projector)
                if projected is not None:
                    yield projected
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in self._stream_items:
            await queue.put(item)
        self._stream_queues.append(queue)
        start_task = asyncio.create_task(self.async_run())
        start_task_joined = False
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                projected = self._project_stream_item(item, type, text_projector=text_projector)
                if projected is not None:
                    yield projected
            await start_task
            start_task_joined = True
        finally:
            if not start_task_joined:
                if not start_task.done():
                    start_task.cancel()
                try:
                    await start_task
                except BaseException:
                    # Joining is cleanup: the generator's original cancellation,
                    # close, or child failure remains the caller-visible error.
                    pass
            if queue in self._stream_queues:
                self._stream_queues.remove(queue)

    def _get_generator(self, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        return default_stage_call_bridge.iter_sync(self.get_async_generator(*args, **kwargs))

    @staticmethod
    def _project_stream_item(
        item: Any,
        type: Any,
        *,
        text_projector: AgentExecutionTextDeltaProjector | None = None,
    ) -> Any:
        if type == "all":
            return ("agent_task", item)
        if type == "delta":
            return text_projector.project(item) if text_projector is not None else project_agent_execution_text_delta(item)
        return item

    async def _emit_progress(
        self,
        iteration: int | None,
        stage: str,
        message: str,
    ) -> AgentExecutionStreamData | None:
        if not self._stream_progress_enabled():
            return None
        if self._progress_model_key() is not None:
            return None
        emit_coro = self._emit(
            (
                f"agent_task.progress.{stage}"
                if iteration is None
                else f"agent_task.iteration.{iteration}.progress.{stage}"
            ),
            {
                "message": message,
                "iteration": iteration,
                "stage": stage,
                "status": self.status,
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "progress",
                "progress_source": "template",
            },
        )
        if self._stream_progress_background_enabled():
            task = asyncio.create_task(emit_coro)
            self._track_background_stream_task(task)
            return None
        return await emit_coro

    async def _emit_snapshot(
        self,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
        *,
        message: str,
    ) -> AgentExecutionStreamData | None:
        if not self._stream_snapshots_enabled():
            return None
        item = await self._emit(
            f"agent_task.iteration.{iteration}.snapshot.{stage}",
            {
                "message": message,
                "iteration": iteration,
                "stage": stage,
                "snapshot": snapshot,
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "snapshot",
            },
        )
        self._schedule_model_progress_from_snapshot(
            iteration=iteration,
            stage=stage,
            snapshot=snapshot,
        )
        return item

    def _agent_task_option(self, key: str, default: Any = None) -> Any:
        agent_task_options = self.options.get("agent_task")
        if isinstance(agent_task_options, dict) and key in agent_task_options:
            return agent_task_options.get(key)
        return self.options.get(key, default)

    def _stream_progress_enabled(self) -> bool:
        return self._normalize_bool(self._agent_task_option("stream_progress", False), default=False)

    def _stream_progress_background_enabled(self) -> bool:
        return self._normalize_bool(
            self._agent_task_option("stream_progress_background", True),
            default=True,
        )

    def _stream_snapshots_enabled(self) -> bool:
        return self._normalize_bool(self._agent_task_option("stream_snapshots", True), default=True)

    def _progress_model_key(self) -> str | None:
        model_key = self._agent_task_option("progress_model_key", None) or self._agent_task_option(
            "stream_progress_model_key", None
        )
        if model_key is None:
            return None
        normalized = str(model_key).strip()
        return normalized or None

    def _progress_timeout_seconds(self) -> float:
        timeout = self._agent_task_option("progress_timeout_seconds", 20)
        normalized = self._normalize_timeout(timeout)
        return 20.0 if normalized is None else normalized

    def _schedule_model_progress_from_snapshot(
        self,
        *,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
    ) -> None:
        if not self._stream_progress_enabled():
            return
        model_key = self._progress_model_key()
        if model_key is None:
            return
        task = asyncio.create_task(
            self._emit_model_progress_from_snapshot(
                iteration=iteration,
                stage=stage,
                snapshot=self._operator_safe_progress_snapshot(stage, DataFormatter.sanitize(snapshot)),
                model_key=model_key,
            )
        )
        self._track_background_stream_task(task)

    async def _emit_model_progress_from_snapshot(
        self,
        *,
        iteration: int,
        stage: str,
        snapshot: dict[str, Any],
        model_key: str,
    ) -> AgentExecutionStreamData | None:
        try:
            request = self.agent.create_temp_request(model_key=model_key)
            progress_language = self._progress_language()
            request.set_settings("runtime.side_channel", True)
            request.set_settings("model_request.side_channel", True)
            request.input(
                {
                    "task_id": self.id,
                    "goal": self.goal,
                    "success_criteria": self.success_criteria,
                    "iteration": iteration,
                    "stage": stage,
                    "status": self.status,
                    "progress_language": progress_language,
                    "snapshot": snapshot,
                }
            )
            request.instruct(
                "Summarize AgentTask progress for a human operator using only the provided snapshot and task metadata. "
                "Do not add new facts, do not infer hidden results, and keep the message concise. "
                f"Write the message in this language: { progress_language }."
            )
            request.output(
                {
                    "message": (str, "One concise natural-language progress update.", True),
                },
                format="json",
            )
            result = request.get_result()
            streamed_message = ""
            final_stream_value = ""
            async for item in result.get_async_generator(type="instant"):
                raw_path = str(getattr(item, "path", "") or getattr(item, "wildcard_path", "") or "")
                if raw_path != "message" and not raw_path.endswith(".message"):
                    continue
                delta = getattr(item, "delta", None)
                value = getattr(item, "value", None)
                event_type = getattr(item, "event_type", None)
                if isinstance(delta, str) and delta:
                    streamed_message += delta
                    await self._emit_progress_delta(
                        iteration=iteration,
                        stage=stage,
                        delta=delta,
                        message_so_far=streamed_message,
                        model_key=model_key,
                        language=progress_language,
                    )
                elif event_type == "delta" and isinstance(value, str) and value:
                    suffix = value[len(streamed_message) :] if value.startswith(streamed_message) else value
                    if suffix:
                        streamed_message += suffix
                        await self._emit_progress_delta(
                            iteration=iteration,
                            stage=stage,
                            delta=suffix,
                            message_so_far=streamed_message,
                            model_key=model_key,
                            language=progress_language,
                        )
                elif bool(getattr(item, "is_complete", False)) and isinstance(value, str):
                    final_stream_value = value
            raw = await asyncio.wait_for(result.async_get_data(), timeout=self._progress_timeout_seconds())
            message = ""
            if isinstance(raw, dict):
                message = str(raw.get("message") or "")
            else:
                message = str(raw or "")
            if not message.strip() and final_stream_value.strip():
                message = final_stream_value
            if not message.strip() and streamed_message.strip():
                message = streamed_message
            if not message.strip():
                return None
            return await self._emit(
                f"agent_task.iteration.{iteration}.progress.{stage}",
                {
                    "message": message.strip(),
                    "iteration": iteration,
                    "stage": stage,
                    "status": self.status,
                    "language": progress_language,
                },
                meta={
                    "task_id": self.id,
                    "status": self.status,
                    "iteration": iteration,
                    "stage": stage,
                    "stream_kind": "progress",
                    "progress_source": "model",
                    "progress_model_key": model_key,
                    "progress_language": progress_language,
                },
            )
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            self.diagnostics.setdefault("progress_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": message,
                    "iteration": iteration,
                    "stage": stage,
                    "model_key": model_key,
                }
            )
            return None

    async def _emit_progress_delta(
        self,
        *,
        iteration: int,
        stage: str,
        delta: str,
        message_so_far: str,
        model_key: str,
        language: str,
    ) -> AgentExecutionStreamData:
        return await self._emit(
            f"agent_task.iteration.{iteration}.progress.{stage}.message",
            {
                "message": message_so_far,
                "iteration": iteration,
                "stage": stage,
                "status": self.status,
                "language": language,
            },
            event_type="delta",
            delta=delta,
            is_complete=False,
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "stage": stage,
                "stream_kind": "progress_delta",
                "progress_source": "model",
                "progress_model_key": model_key,
                "progress_language": language,
            },
        )

    def _progress_language(self) -> str:
        language = self._language_policy().get("progress_language")
        if language in (None, "", "auto"):
            getter = getattr(getattr(self.agent, "settings", None), "get", None)
            if callable(getter):
                language = getter("agent_task.progress.language", "auto")
        normalized = str(language or "auto").strip()
        return normalized or "auto"

    @staticmethod
    def _normalize_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on", "enabled"}:
                return True
            if lowered in {"0", "false", "no", "off", "disabled"}:
                return False
        return bool(value)

    def _track_background_stream_task(self, task: asyncio.Task[Any]) -> None:
        self._background_stream_tasks.add(task)

        def discard(done_task: asyncio.Task[Any]) -> None:
            self._background_stream_tasks.discard(done_task)
            try:
                error = done_task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self.diagnostics.setdefault("stream_errors", []).append(
                    {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    }
                )

        task.add_done_callback(discard)

    async def _cancel_background_stream_tasks(self) -> None:
        if not self._background_stream_tasks:
            return
        tasks = list(self._background_stream_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        _done, pending = await asyncio.wait(tasks, timeout=0.1)
        if pending:
            for task in pending:
                self._background_stream_tasks.discard(task)
            self.diagnostics.setdefault("stream_warnings", []).append(
                {
                    "type": "BackgroundStreamTaskCancelTimeout",
                    "message": "Background stream task cancellation did not finish before AgentTask stream close.",
                    "pending_count": len(pending),
                }
            )

    @classmethod
    def _operator_safe_progress_snapshot(cls, stage: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], cls._strip_developer_diagnostics({"stage": stage, **snapshot}))

    _DEVELOPER_DIAGNOSTIC_KEYS = frozenset(
        {
            "diagnostics",
            "context_pack_diagnostics",
            "fallback_reason",
            "errors",
            "progress_errors",
            "stream_errors",
        }
    )

    @classmethod
    def _strip_developer_diagnostics(cls, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key)
                if normalized_key in cls._DEVELOPER_DIAGNOSTIC_KEYS or "diagnostic" in normalized_key:
                    cleaned[normalized_key] = {"omitted": "developer_diagnostics"}
                    continue
                cleaned[normalized_key] = cls._strip_developer_diagnostics(item)
            return cleaned
        if isinstance(value, list):
            return [cls._strip_developer_diagnostics(item) for item in value]
        return value

    @classmethod
    def _execution_log_summary(cls, execution_meta: dict[str, Any]) -> dict[str, Any]:
        logs = execution_meta.get("logs", {})
        if not isinstance(logs, dict):
            logs = {}
        action_records = cls._collect_execution_action_records(execution_meta)
        action_ids = [record["id"] for record in action_records if record.get("id")]
        action_statuses = {record["id"]: record.get("status", "") for record in action_records if record.get("id")}
        source_refs = cls._collect_source_refs_from_action_records(action_records)
        raw_explicit_source_refs = logs.get("source_refs")
        if isinstance(raw_explicit_source_refs, Sequence) and not isinstance(
            raw_explicit_source_refs, str | bytes | bytearray
        ):
            source_refs = cast(
                list[dict[str, Any]],
                cls._dedupe_ref_records([*source_refs, *raw_explicit_source_refs]),
            )[:32]
        # Risk status is judged by the final status per action id (last record
        # wins), so an action that failed and then succeeded within the same step
        # is treated as recovered and does not block verification.
        failed_actions = cls._action_ids_by_final_status(action_statuses, {"failed", "failure", "error"})
        blocked_actions = cls._action_ids_by_final_status(action_statuses, {"blocked"})
        approval_required_actions = cls._action_ids_by_final_status(action_statuses, {"approval_required"})
        required_actions, required_skills = cls._required_capability_constraints(execution_meta)
        missing_required_actions = [action_id for action_id in required_actions if action_id not in action_ids]
        skill_context_consumptions = cls._skill_context_consumptions(logs)
        consumed_skill_ids = list(
            dict.fromkeys(
                str(item.get("skill_id") or "").strip()
                for item in skill_context_consumptions
                if str(item.get("skill_id") or "").strip()
                and str(item.get("request_id") or "").strip()
            )
        )
        missing_required_skill_context = [
            skill_id for skill_id in required_skills if skill_id not in consumed_skill_ids
        ]
        succeeded_actions = list(
            dict.fromkeys(
                cls._action_ids_by_status(
                    action_records, {"success", "succeeded", "partial_success"}
                )
            )
        )
        route = execution_meta.get("route", {})
        artifact_refs = logs.get("artifact_refs", [])
        file_refs = logs.get("file_refs", [])
        artifact_readbacks = cls._artifact_readback_evidence_ids(artifact_refs)
        record_refs = execution_meta.get("record_refs") or logs.get("record_refs", {})
        task_workspace_refs = execution_meta.get("task_workspace_refs") or logs.get(
            "task_workspace_refs", {}
        )
        raw_errors = logs.get("errors", [])
        execution_errors: list[Any]
        if isinstance(raw_errors, list):
            execution_errors = [_compact_agent_task_error_info(item) for item in raw_errors]
        elif raw_errors:
            execution_errors = [_compact_agent_task_error_info(raw_errors)]
        else:
            execution_errors = []
        diagnostics = execution_meta.get("diagnostics", {})
        if isinstance(diagnostics, dict) and diagnostics.get("execution_error"):
            execution_errors.append(_compact_agent_task_error_info(diagnostics["execution_error"]))
        replan_signals = cls._collect_replan_signals(execution_meta)
        collect_evidence_requirements = cast(
            Callable[[Mapping[str, Any] | None], list[dict[str, Any]]] | None,
            getattr(cls, "_capability_evidence_requirements_from_mapping", None),
        )
        capability_evidence_requirements: list[dict[str, Any]] = []
        if callable(collect_evidence_requirements):
            for source in (execution_meta.get("effective_options"), execution_meta.get("options")):
                if isinstance(source, Mapping):
                    capability_evidence_requirements.extend(collect_evidence_requirements(source))
            merge_evidence_requirements = cast(
                Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None,
                getattr(cls, "_merge_capability_evidence_requirements", None),
            )
            if callable(merge_evidence_requirements):
                capability_evidence_requirements = merge_evidence_requirements(capability_evidence_requirements)
        capabilities_used = list(dict.fromkeys(action_ids))
        return {
            "model_response_count": (
                len(logs.get("model_responses", [])) if isinstance(logs.get("model_responses", []), list) else 0
            ),
            "action_log_count": len(action_ids),
            "action_ids": action_ids,
            "action_statuses": action_statuses,
            "actions": action_records,
            "source_refs": source_refs,
            "failed_actions": failed_actions,
            "blocked_actions": blocked_actions,
            "approval_required_actions": approval_required_actions,
            "required_actions": required_actions,
            "capability_evidence_requirements": capability_evidence_requirements,
            "missing_required_actions": missing_required_actions,
            "consumed_skill_ids": consumed_skill_ids,
            "required_skills": required_skills,
            "missing_required_skill_context": missing_required_skill_context,
            "capabilities_used": capabilities_used,
            "capability_evidence": {
                "actions": {"succeeded": succeeded_actions, "failed": failed_actions},
                "artifacts": {"readback": artifact_readbacks},
                "validations": {"passed": [], "failed": []},
            },
            "context_evidence": {
                "skills": {
                    "consumed": consumed_skill_ids,
                    "consumptions": skill_context_consumptions,
                }
            },
            "artifact_refs": DataFormatter.sanitize(artifact_refs),
            "file_refs": DataFormatter.sanitize(file_refs),
            "record_refs": DataFormatter.sanitize(record_refs),
            "task_workspace_refs": DataFormatter.sanitize(task_workspace_refs),
            "route": DataFormatter.sanitize(route),
            "status": str(execution_meta.get("status") or ""),
            "errors": DataFormatter.sanitize(execution_errors),
            "replan_signals": DataFormatter.sanitize(replan_signals),
        }

    @staticmethod
    def _collect_replan_signals(execution_meta: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_values: list[Any] = []
        direct_signal = execution_meta.get("replan_signal")
        if direct_signal is not None:
            raw_values.append(direct_signal)
        direct_signals = execution_meta.get("replan_signals")
        if isinstance(direct_signals, (list, tuple)):
            raw_values.extend(direct_signals)
        blocks = execution_meta.get("blocks")
        if isinstance(blocks, Mapping):
            evidence = blocks.get("evidence")
            if isinstance(evidence, Mapping):
                diagnostics = evidence.get("diagnostics")
                if isinstance(diagnostics, (list, tuple)):
                    raw_values.extend(
                        item
                        for item in diagnostics
                        if isinstance(item, Mapping) and item.get("kind") == "replan_signal"
                    )
            snapshot = blocks.get("snapshot")
            snapshot_blocks = snapshot.get("blocks") if isinstance(snapshot, Mapping) else None
            if isinstance(snapshot_blocks, Mapping):
                replan_signals = snapshot_blocks.get("replan_signals")
                if isinstance(replan_signals, (list, tuple)):
                    raw_values.extend(replan_signals)

        signals: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for value in raw_values:
            if not isinstance(value, Mapping):
                continue
            candidate = dict(value)
            if candidate.get("kind") == "replan_signal":
                candidate.pop("kind", None)
            try:
                normalized = ReplanSignal.from_value(candidate).to_dict()
            except Exception as error:
                normalized = {
                    "status": "blocked",
                    "reason": "Invalid ReplanSignal payload: "
                    + _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "diagnostics": [
                        {
                            "type": error.__class__.__name__,
                            "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                        }
                    ],
                }
            key = (str(normalized.get("status") or ""), str(normalized.get("reason") or ""))
            if key not in seen:
                seen.add(key)
                signals.append(normalized)
        return signals

    @classmethod
    def _required_capability_constraints(cls, execution_meta: dict[str, Any]) -> tuple[list[str], list[str]]:
        constraints = execution_meta.get("effective_options", {})
        if not isinstance(constraints, dict):
            constraints = execution_meta.get("options", {})
        if not isinstance(constraints, dict):
            return [], []
        capability_constraints = constraints.get("capability_constraints", {})
        if not isinstance(capability_constraints, dict):
            return [], []
        actions = capability_constraints.get("actions", {})
        skills = capability_constraints.get("skills", {})
        required_actions = actions.get("required", []) if isinstance(actions, dict) else []
        required_skills = skills.get("required", []) if isinstance(skills, dict) else []
        return (
            cls._normalize_string_list(required_actions),
            cls._normalize_string_list(required_skills),
        )

    @staticmethod
    def _skill_context_consumptions(logs: dict[str, Any]) -> list[dict[str, Any]]:
        raw = logs.get("skill_context_consumptions", [])
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes | bytearray):
            return []
        return [
            dict(DataFormatter.sanitize(item))
            for item in raw
            if isinstance(item, Mapping)
            and str(item.get("skill_id") or "").strip()
            and str(item.get("request_id") or "").strip()
        ]

    @classmethod
    def _collect_execution_action_records(
        cls,
        execution_meta: Mapping[str, Any],
        *,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        logs = execution_meta.get("logs", {})
        records = cls._collect_action_records(logs if isinstance(logs, dict) else {})
        if depth >= 3:
            return cls._dedupe_action_records(records)

        blocks = execution_meta.get("blocks")
        if not isinstance(blocks, Mapping):
            return cls._dedupe_action_records(records)
        evidence = blocks.get("evidence")
        if not isinstance(evidence, Mapping):
            return cls._dedupe_action_records(records)
        for key in ("execution_block_results", "plan_block_results"):
            block_results = evidence.get(key)
            if not isinstance(block_results, Sequence) or isinstance(block_results, (str, bytes, bytearray)):
                continue
            for block_result in block_results:
                if not isinstance(block_result, Mapping):
                    continue
                output = block_result.get("output")
                if not isinstance(output, Mapping):
                    continue
                nested_meta = output.get("execution_meta")
                if isinstance(nested_meta, Mapping):
                    records.extend(cls._collect_execution_action_records(nested_meta, depth=depth + 1))
                nested_result = output.get("execution_result")
                if isinstance(nested_result, Mapping):
                    nested_result_meta = nested_result.get("execution_meta")
                    if isinstance(nested_result_meta, Mapping):
                        records.extend(cls._collect_execution_action_records(nested_result_meta, depth=depth + 1))
        return cls._dedupe_action_records(records)

    @staticmethod
    def _dedupe_action_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        for record in records:
            action_id = str(record.get("action_id") or record.get("id") or record.get("name") or "")
            call_id = str(record.get("action_call_id") or "")
            round_index = str(record.get("round_index") if record.get("round_index") is not None else "")
            command_index = str(record.get("command_index") if record.get("command_index") is not None else "")
            preview_sha = str(record.get("result_preview_sha256") or "")
            preview = str(record.get("result_preview") or "")
            if len(preview) > 120:
                preview = preview[:120]
            key = (action_id, call_id, round_index, command_index, preview_sha, preview)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(dict(record))
        return deduped

    async def _emit_action_observation_events(
        self,
        iteration_index: int | None,
        *,
        execution_meta: Mapping[str, Any],
        owner_context: Mapping[str, Any] | None = None,
    ) -> None:
        records = self._collect_execution_action_records(execution_meta)
        if not records:
            return
        if owner_context is None:
            owner_context = self._action_event_owner_context(iteration_index, execution_meta)
        for record in records:
            indexed_record = dict(record)
            action_id = str(record.get("action_id") or record.get("id") or record.get("name") or "").strip()
            if not action_id:
                continue
            await self._emit_normalized_action_event(
                "started",
                indexed_record,
                execution_meta=execution_meta,
                owner_context=owner_context,
            )
            if self._action_record_failed(indexed_record):
                await self._emit_normalized_action_event(
                    "failed",
                    indexed_record,
                    execution_meta=execution_meta,
                    owner_context=owner_context,
                )
            else:
                await self._emit_normalized_action_event(
                    "completed",
                    indexed_record,
                    execution_meta=execution_meta,
                    owner_context=owner_context,
                )

    async def _emit_planned_action_batch(
        self,
        *,
        iteration_index: int | None,
        commands: Sequence[Mapping[str, Any]],
        execution_id: str,
        round_index: int | None,
        concurrency: int | None,
        parallel: bool | None,
        projection_source: str,
    ) -> None:
        actions: list[dict[str, Any]] = []
        for position, command in enumerate(commands):
            if not isinstance(command, Mapping):
                continue
            action_id = " ".join(str(command.get("action_id") or "").split())[:80]
            if not action_id:
                continue
            purpose = " ".join(str(command.get("purpose") or f"Use {action_id}").split())[:180]
            action_call_id = " ".join(str(command.get("action_call_id") or "").split())[:120]
            actions.append(
                {
                    "position": position,
                    "action_id": action_id,
                    "purpose": purpose,
                    "action_call_id": action_call_id or None,
                }
            )
        if not actions:
            return
        normalized_concurrency = concurrency if isinstance(concurrency, int) and concurrency > 0 else None
        normalized_parallel = parallel if isinstance(parallel, bool) else None
        source = " ".join(str(projection_source or "").split())[:80]
        await self._emit(
            "agent_task.action.batch.planned",
            {
                "iteration": iteration_index,
                "round_index": round_index,
                "command_count": len(actions),
                "concurrency": normalized_concurrency,
                "parallel": normalized_parallel,
                "actions": actions,
                "projection_source": source,
            },
            meta={
                "task_id": self.id,
                "status": self.status,
                "stream_kind": "action_observation",
                "phase": "planned",
                "strategy": "flat",
                "iteration": iteration_index,
                "round_index": round_index,
                "execution_id": execution_id,
                "projection_source": source,
            },
        )

    async def _project_live_action_observation(
        self,
        iteration_index: int,
        observation: Mapping[str, Any],
        *,
        child_execution_id: str,
    ) -> None:
        kind = str(observation.get("kind") or "").strip().lower()
        payload = observation.get("payload")
        if not isinstance(payload, Mapping):
            return
        if kind == "plan_ready":
            decision = payload.get("decision")
            if not isinstance(decision, Mapping):
                return
            commands = decision.get("action_calls")
            if not isinstance(commands, Sequence) or isinstance(commands, str | bytes | bytearray):
                return
            stream_projection = observation.get("stream_projection")
            stream_projection = stream_projection if isinstance(stream_projection, Mapping) else {}
            if stream_projection.get("dispatch_confirmed") is not True:
                return
            concurrency = stream_projection.get("concurrency")
            parallel = stream_projection.get("parallel")
            round_index = payload.get("round_index")
            await self._emit_planned_action_batch(
                iteration_index=iteration_index,
                commands=[command for command in commands if isinstance(command, Mapping)],
                execution_id=child_execution_id,
                round_index=(
                    round_index
                    if isinstance(round_index, int) and not isinstance(round_index, bool)
                    else None
                ),
                concurrency=(
                    concurrency
                    if isinstance(concurrency, int) and not isinstance(concurrency, bool)
                    else None
                ),
                parallel=parallel if isinstance(parallel, bool) else None,
                projection_source="action.plan_ready",
            )
            return

        phase_by_kind = {
            "action_started": "started",
            "action_completed": "completed",
            "action_failed": "failed",
            "action_blocked": "failed",
            "action_approval_required": "failed",
        }
        phase = phase_by_kind.get(kind)
        if phase is None:
            return
        raw_record = payload.get("command") if phase == "started" else payload.get("record")
        record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
        action_id = str(
            record.get("action_id")
            or record.get("id")
            or payload.get("action_name")
            or ""
        ).strip()
        if not action_id:
            return
        record["id"] = action_id
        record["status"] = "started" if phase == "started" else record.get("status", phase)
        command_index = payload.get("command_index") if phase == "started" else payload.get("record_index")
        if isinstance(command_index, int) and not isinstance(command_index, bool):
            record["command_index"] = command_index
        round_index = payload.get("round_index")
        if isinstance(round_index, int) and not isinstance(round_index, bool):
            record["round_index"] = round_index
        await self._emit_normalized_action_event(
            cast(Literal["started", "completed", "failed"], phase),
            record,
            execution_meta={"execution_id": child_execution_id, "route": {}},
            owner_context={"iteration": iteration_index, "strategy": "flat"},
            projection_source=f"action.{phase}",
            posthoc_projection=False,
        )

    @staticmethod
    def _action_event_owner_context(iteration_index: int | None, execution_meta: Mapping[str, Any]) -> dict[str, Any]:
        block_carrier = execution_meta.get("block_carrier")
        work_unit: Mapping[str, Any] = {}
        if isinstance(block_carrier, Mapping):
            raw_work_unit = block_carrier.get("work_unit")
            if isinstance(raw_work_unit, Mapping):
                work_unit = raw_work_unit
        runtime_preferences = work_unit.get("runtime_preferences")
        if not isinstance(runtime_preferences, Mapping):
            runtime_preferences = {}
        return {
            "iteration": iteration_index,
            "origin": work_unit.get("origin"),
            "work_unit_id": work_unit.get("id"),
            "strategy": runtime_preferences.get("strategy"),
            "card_id": runtime_preferences.get("card_id"),
        }

    async def _emit_normalized_action_event(
        self,
        phase: Literal["started", "completed", "failed"],
        record: Mapping[str, Any],
        *,
        execution_meta: Mapping[str, Any],
        owner_context: Mapping[str, Any],
        projection_source: str = "execution_meta.action_logs",
        posthoc_projection: bool = True,
    ) -> None:
        if self._normalized_action_event_already_emitted(phase, record, execution_meta=execution_meta):
            return
        action_id = str(record.get("action_id") or record.get("id") or record.get("name") or "").strip()
        action_call_id = str(record.get("action_call_id") or "").strip()
        command_index = record.get("command_index")
        round_index = record.get("round_index")
        status = str(record.get("status") or "").strip() or ("started" if phase == "started" else phase)
        payload: dict[str, Any] = {
            "action_id": action_id,
            "action_call_id": action_call_id or None,
            "status": "started" if phase == "started" else status,
            "action_type": str(record.get("action_type") or "").strip() or None,
            "kind": str(record.get("kind") or "").strip() or None,
            "execution_id": execution_meta.get("execution_id"),
            "route": DataFormatter.sanitize(execution_meta.get("route", {})),
            "projection_source": projection_source,
            "posthoc_projection": posthoc_projection,
            "command_index": command_index if isinstance(command_index, int) and not isinstance(command_index, bool) else None,
            "round_index": round_index if isinstance(round_index, int) and not isinstance(round_index, bool) else None,
            **{key: value for key, value in owner_context.items() if value is not None},
        }
        if "input_preview" in record:
            payload["input_summary"] = record.get("input_preview")
        if phase in {"completed", "failed"}:
            payload["success"] = phase == "completed"
            if "result_preview" in record:
                payload["output_summary"] = record.get("result_preview")
            if "result_preview_meta" in record:
                payload["result_preview_meta"] = record.get("result_preview_meta")
            for key in (
                "artifact_refs",
                "file_refs",
                "usage",
                "estimated_input_chars",
                "estimated_output_chars",
                "elapsed_ms",
                "duration_ms",
                "warnings",
            ):
                if key in record:
                    if key in {"artifact_refs", "file_refs"}:
                        payload[key] = await self._public_action_file_refs(record.get(key))
                    else:
                        payload[key] = record.get(key)
            source_refs = self._collect_source_refs_from_action_records([record])
            if source_refs:
                payload["source_refs"] = source_refs
        if phase == "failed":
            error = record.get("error")
            if error is not None:
                payload["error"] = self._compact_verifier_prompt_value(error, max_chars=600)
            if "retryable" in record:
                payload["retryable"] = bool(record.get("retryable"))
            payload["failure_category"] = self._action_failure_category(record, status=status)
        await self._emit(
            f"agent_task.action.{phase}",
            {key: value for key, value in payload.items() if value is not None},
            meta={
                "task_id": self.id,
                "status": self.status,
                "stream_kind": "action_observation",
                "action_id": action_id,
                "action_call_id": action_call_id or None,
                "command_index": payload.get("command_index"),
                "round_index": payload.get("round_index"),
                "phase": phase,
                "iteration": owner_context.get("iteration"),
                "origin": owner_context.get("origin"),
                "work_unit_id": owner_context.get("work_unit_id"),
                "strategy": owner_context.get("strategy"),
                "card_id": owner_context.get("card_id"),
                "projection_source": projection_source,
                "trusted_ref_projection": True,
            },
        )

    async def _public_action_file_refs(self, refs: Any) -> list[dict[str, Any]]:
        if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
            return []
        task_workspace = getattr(self, "task_workspace", None)
        projected: list[dict[str, Any]] = []
        for raw_ref in refs[:8]:
            if not isinstance(raw_ref, Mapping):
                continue
            ref = dict(DataFormatter.sanitize(dict(raw_ref)))
            ref.pop("open_path", None)
            ref.pop("open_path_verified", None)
            ref.pop("display_path", None)
            projected.append(ref)
            path = str(ref.get("path") or "").strip()
            expected_sha256 = str(ref.get("sha256") or "").strip()
            expected_bytes = ref.get("bytes")
            if (
                task_workspace is None
                or not path
                or not expected_sha256
                or not isinstance(expected_bytes, int)
                or isinstance(expected_bytes, bool)
                or expected_bytes < 0
                or ref.get("available") is False
            ):
                continue
            try:
                target = task_workspace.resolve_file_path(path)
                if not target.is_file():
                    continue
                readback = await task_workspace.read_file(path, max_bytes=1)
                if (
                    readback.get("ok") is not True
                    or str(readback.get("sha256") or "") != expected_sha256
                    or int(readback.get("bytes", -1)) != expected_bytes
                ):
                    continue
                ref["open_path"] = str(target.resolve())
                ref["open_path_verified"] = True
                ref["display_path"] = self._task_workspace_artifact_display_path(path)
            except Exception:
                continue
        return projected

    def _normalized_action_event_already_emitted(
        self,
        phase: str,
        record: Mapping[str, Any],
        *,
        execution_meta: Mapping[str, Any],
    ) -> bool:
        emitted = getattr(self, "_emitted_action_event_keys", None)
        if not isinstance(emitted, set):
            emitted = set()
            setattr(self, "_emitted_action_event_keys", emitted)
        action_id = str(record.get("action_id") or record.get("id") or record.get("name") or "").strip()
        action_call_id = str(record.get("action_call_id") or "").strip()
        command_index = record.get("command_index")
        round_index = record.get("round_index")
        preview_key = str(record.get("result_preview_sha256") or "")
        if not preview_key:
            try:
                preview_key = json.dumps(
                    DataFormatter.sanitize(
                        {
                            "input_preview": record.get("input_preview"),
                            "result_preview": record.get("result_preview"),
                        }
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )[:160]
            except Exception:
                preview_key = f"{record.get('input_preview') or ''}|{record.get('result_preview') or ''}"[:160]
        execution_id = str(execution_meta.get("execution_id") or "")
        keys: list[tuple[Any, ...]] = []
        if action_call_id:
            keys.append((str(phase), execution_id, "call", action_call_id))
        if isinstance(command_index, int) and not isinstance(command_index, bool):
            keys.append(
                (
                    str(phase),
                    execution_id,
                    "position",
                    round_index if isinstance(round_index, int) and not isinstance(round_index, bool) else None,
                    command_index,
                    action_id,
                )
            )
        if not keys:
            keys.append(
                (
                    str(phase),
                    execution_id,
                    "fingerprint",
                    action_id,
                    preview_key or str(record.get("status") or ""),
                )
            )
        if any(key in emitted for key in keys):
            return True
        emitted.update(keys)
        return False

    @staticmethod
    def _action_record_failed(record: Mapping[str, Any]) -> bool:
        status = str(record.get("status") or "").strip().lower()
        if status in {"success", "succeeded", "completed", "complete", "partial_success", "ok"}:
            return False
        return status in {"failed", "error", "timed_out", "timeout", "blocked"} or record.get("error") is not None

    @classmethod
    def _action_failure_category(cls, record: Mapping[str, Any], *, status: str) -> str:
        status_text = str(status or "").strip().lower()
        error_text = str(record.get("error") or "").strip().lower()
        combined = f"{status_text}\n{error_text}"
        if "timeout" in combined or "timed out" in combined or "idle" in combined or "no progress" in combined:
            return "liveness"
        if "capability" in combined or "not allowed" in combined or "not permitted" in combined:
            return "capability"
        if "connection" in combined or "network" in combined or "provider" in combined or "service" in combined:
            return "infra"
        return "execution"

    @staticmethod
    def _dedupe_ref_records(records: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for record in records:
            if isinstance(record, Mapping):
                key = "|".join(
                    str(record.get(field) or "")
                    for field in ("selection_key", "artifact_id", "action_call_id", "path", "sha256", "source_url")
                )
                if not key.strip("|"):
                    key = json.dumps(DataFormatter.sanitize(record), ensure_ascii=False, sort_keys=True)
            else:
                key = str(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    @classmethod
    def _collect_source_refs_from_action_records(cls, records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for record in records:
            action_id = str(record.get("action_id") or record.get("id") or record.get("name") or "").strip()
            action_call_id = str(record.get("action_call_id") or "").strip()

            def collect(value: Any, *, path: str = "") -> None:
                if isinstance(value, Mapping):
                    for key, item in value.items():
                        key_text = str(key)
                        next_path = f"{path}.{key_text}" if path else key_text
                        if key_text in {"source_url", "selected_url", "requested_url", "url", "href", "path"}:
                            ref_value = str(item or "").strip()
                            if ref_value:
                                content_state = cls._source_ref_content_state(value, field=key_text)
                                refs.append(
                                    {
                                        "source": "action_result",
                                        "field": key_text,
                                        "path": next_path,
                                        "value": ref_value,
                                        "action_id": action_id,
                                        "action_call_id": action_call_id,
                                        "content_state": content_state,
                                        "evidence_boundary": (
                                            "content_preview_available"
                                            if content_state == "bounded_readback_available"
                                            else "discovery_or_materialization_only"
                                        ),
                                    }
                                )
                        collect(item, path=next_path)
                    return
                if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
                    for index, item in enumerate(value[:24]):
                        collect(item, path=f"{path}[{index}]" if path else f"[{index}]")

            collect(record.get("result_preview"))
            collect(record.get("artifact_refs"))
            collect(record.get("file_refs"))
        return cast(list[dict[str, Any]], cls._dedupe_ref_records(refs))[:32]

    @staticmethod
    def _source_ref_content_state(container: Any, *, field: str) -> str:
        if not isinstance(container, Mapping):
            return "ref_only"
        for key in (
            "content",
            "content_preview",
            "content_snippet",
            "evidence_snippet",
            "excerpt",
            "snippet",
            "text",
        ):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return "bounded_readback_available"
        preview = container.get("preview")
        if isinstance(preview, str) and preview.strip() and any(
            key in container for key in ("bytes", "read_bytes", "sha256", "handler_id", "role", "source")
        ):
            return "bounded_readback_available"
        return "ref_only"

    @staticmethod
    def _dedupe_jsonable_records(records: Sequence[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for record in records:
            try:
                key = json.dumps(DataFormatter.sanitize(record), ensure_ascii=False, sort_keys=True)
            except Exception:
                key = str(record)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    @classmethod
    def _collect_action_records(cls, logs: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        def add_entries(entries: Any) -> None:
            if isinstance(entries, dict):
                for action_id, record in entries.items():
                    if isinstance(record, dict):
                        records.append(cls._compact_action_record(action_id, record))
                    else:
                        records.append({"id": str(action_id), "name": str(action_id), "status": str(record or "")})
            elif isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict):
                        action_id = item.get("action_id") or item.get("id") or item.get("name") or ""
                        records.append(cls._compact_action_record(action_id, item))

        add_entries(logs.get("action_logs", {}))
        route_logs = logs.get("route_logs", {})
        if isinstance(route_logs, dict):
            add_entries(route_logs.get("action_logs", {}))
            route_output = route_logs.get("output", {})
            if isinstance(route_output, dict):
                add_entries(route_output.get("history", []))
        return records

    @classmethod
    def _compact_action_record(cls, action_id: Any, record: dict[str, Any]) -> dict[str, Any]:
        normalized_id = str(action_id or record.get("action_id") or record.get("id") or record.get("name") or "")
        status = str(record.get("status") or "").strip()
        if not status:
            result_value = record.get("result", record.get("data"))
            nested_status = result_value.get("status") if isinstance(result_value, Mapping) else None
            if record.get("error"):
                status = "failed"
            elif isinstance(nested_status, str) and nested_status.strip():
                status = nested_status.strip()
            elif "result" in record or "artifact" in record:
                status = "success"
        compact: dict[str, Any] = {
            "id": normalized_id,
            "name": str(record.get("name") or normalized_id),
            "status": status,
            "action_type": str(record.get("action_type") or record.get("type") or ""),
            "kind": str(record.get("kind") or ""),
        }
        for identity_key in ("round_index", "command_index"):
            identity_value = record.get(identity_key)
            if isinstance(identity_value, int) and not isinstance(identity_value, bool):
                compact[identity_key] = identity_value
        action_call_id = str(record.get("action_call_id") or record.get("call_id") or "").strip()
        if action_call_id:
            compact["action_call_id"] = action_call_id

        raw = record.get("raw")
        if not isinstance(raw, Mapping):
            raw = {}
        model_digest = record.get("model_digest")
        if (
            isinstance(model_digest, Mapping)
            and model_digest.get("same_as") == "result"
            and isinstance(record.get("result"), Mapping)
        ):
            model_digest = record.get("result")
        if not isinstance(model_digest, Mapping) or not model_digest:
            raw_model_digest = raw.get("model_digest")
            if (
                isinstance(raw_model_digest, Mapping)
                and raw_model_digest.get("same_as") == "result"
                and isinstance(raw.get("result"), Mapping)
            ):
                raw_model_digest = raw.get("result")
            if isinstance(raw_model_digest, Mapping) and raw_model_digest:
                model_digest = raw_model_digest
        digest = model_digest if isinstance(model_digest, Mapping) and model_digest else (raw or record)

        result_preview = digest.get("result_preview") if isinstance(digest, Mapping) else None
        if result_preview is None and isinstance(record.get("result_preview"), (Mapping, Sequence, str)):
            result_preview = record.get("result_preview")
        if result_preview is None and isinstance(digest, Mapping):
            for key in ("data", "result", "output"):
                fallback_preview = digest.get(key)
                if fallback_preview is not None:
                    result_preview = fallback_preview
                    break
        if result_preview is not None:
            compact_result_preview = cls._compact_action_preview_value(result_preview, max_chars=5200)
            if isinstance(result_preview, Mapping) and isinstance(compact_result_preview, dict):
                result_path = result_preview.get("path") or result_preview.get("output_path") or result_preview.get("file_path")
                if result_path:
                    for key in ("filename", "file_name", "size"):
                        if key in result_preview and result_preview.get(key) not in (None, "", [], {}):
                            compact_result_preview[key] = DataFormatter.sanitize(result_preview.get(key))
            compact["result_preview"] = compact_result_preview
        result_preview_meta = digest.get("result_preview_meta") if isinstance(digest, Mapping) else None
        if result_preview_meta is None:
            result_preview_meta = record.get("result_preview_meta")
        if result_preview_meta is None and isinstance(result_preview, Mapping):
            result_preview_meta = {
                key: result_preview.get(key)
                for key in ("chars", "bytes", "sha256", "truncated", "read_bytes", "size")
                if key in result_preview
            }
        if result_preview_meta is not None:
            compact["result_preview_meta"] = cls._compact_verifier_prompt_value(result_preview_meta, max_chars=500)

        input_preview = record.get("input") or record.get("kwargs") or raw.get("input") or raw.get("kwargs")
        if input_preview:
            compact["input_preview"] = cls._compact_verifier_prompt_value(input_preview, max_chars=500)

        for key in ("artifact_refs", "file_refs"):
            value = digest.get(key) if isinstance(digest, Mapping) else None
            if value is None:
                value = record.get(key)
            if value is None and raw:
                value = raw.get(key)
            if key == "artifact_refs" and isinstance(value, list):
                compact[key] = [cls._compact_artifact_ref_for_verifier(ref) for ref in value[:8]]
                if len(value) > 8:
                    compact[key].append({"omitted": len(value) - 8, "reason": "prompt_budget"})
            elif key == "file_refs" and value:
                compact[key] = cls._compact_verifier_prompt_value(value, max_chars=1200)

        for key in (
            "usage",
            "estimated_input_chars",
            "estimated_output_chars",
            "elapsed_ms",
            "duration_ms",
            "retryable",
            "warnings",
            "error",
        ):
            value = record.get(key)
            if value is None and isinstance(digest, Mapping):
                value = digest.get(key)
            if value is None and raw:
                value = raw.get(key)
            if value is not None:
                compact[key] = cls._compact_verifier_prompt_value(value, max_chars=1200)

        preview_meta = compact.get("result_preview_meta")
        if isinstance(preview_meta, Mapping):
            sha = preview_meta.get("sha256") or preview_meta.get("result_sha256")
            if sha:
                compact["result_preview_sha256"] = str(sha)
        return compact

    @staticmethod
    def _action_ids_by_status(records: list[dict[str, str]], statuses: set[str]) -> list[str]:
        result: list[str] = []
        for record in records:
            status = record.get("status", "").strip().lower()
            if status in statuses and record.get("id"):
                result.append(record["id"])
        return result

    @staticmethod
    def _action_ids_by_final_status(action_statuses: dict[str, str], statuses: set[str]) -> list[str]:
        return [
            action_id
            for action_id, status in action_statuses.items()
            if action_id and str(status).strip().lower() in statuses
        ]

    @staticmethod
    def _stream_path_token(value: Any) -> str:
        token = str(value or "").strip().replace("/", ".")
        return token or "item"

    async def _record_phase(
        self,
        phase: str,
        *,
        iteration: int | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        record = {
            "phase": phase,
            "iteration": iteration,
            "status": self.status,
            "diagnostics": DataFormatter.sanitize(diagnostics or {}),
        }
        self.diagnostics.setdefault("phases", []).append(record)
        return await self._emit(
            f"agent_task.phase.{phase}",
            record,
            meta={
                "task_id": self.id,
                "status": self.status,
                "iteration": iteration,
                "phase": phase,
                "stream_kind": "phase",
            },
        )

    async def _emit(
        self,
        path: str,
        value: Any,
        *,
        event_type: Literal["delta", "done"] = "done",
        delta: str | None = None,
        is_complete: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AgentExecutionStreamData:
        completed = event_type == "done"
        if is_complete is not None:
            completed = is_complete
        stream_kind = meta.get("stream_kind") if isinstance(meta, dict) else None
        if stream_kind != "heartbeat":
            self._last_stream_emit_monotonic = time.monotonic()
        item = AgentExecutionStreamData(
            path=path,
            value=DataFormatter.sanitize(value),
            delta=delta,
            is_complete=completed,
            event_type=event_type,
            source="agent_task",
            task_id=self.id,
            meta=meta or {"task_id": self.id, "status": self.status},
        )
        self._stream_items.append(item)
        # Bound the replay buffer so a very long task does not grow it without
        # limit. Late subscribers replay at most the most recent window.
        if len(self._stream_items) > _STREAM_REPLAY_LIMIT:
            del self._stream_items[: len(self._stream_items) - _STREAM_REPLAY_LIMIT]
        for queue in list(self._stream_queues):
            await queue.put(item)
        return item

    async def _close_streams(self):
        await self._cancel_background_stream_tasks()
        for queue in list(self._stream_queues):
            await queue.put(None)

    def _task_summary(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "execution_strategy": self.execution_strategy,
            "effective_execution_strategy": self.effective_execution_strategy,
            "max_iterations": self.max_iterations,
            "guidance_count": len(getattr(self, "guidance_items", []) or []),
            "verify": self.verify,
        }


__all__ = ["AgentTaskObservationMixin"]
