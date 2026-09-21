"""Execution-owned media preparation; atomic requests retain their existing owners."""

from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from typing import Any

import httpx

from agently.core.orchestration import TriggerFlow
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils.ModelPool import apply_role_profile, resolve_role_profile, resolve_model_pool_settings

from .production import ProductionOptions

_DESCRIPTION = (
    "Describe the image(s). If there is readable text, explain its contents. "
    "Identify unreadable or uncertain details explicitly; do not invent missing text."
)
_EVIDENCE = {
    "observations": (str, "Visible facts relevant to the task, including readable text, image numbers, identities and spatial relationships. Ground every claim in the images."),
    "uncertainties": (list[str], "Unreadable, ambiguous or missing visual evidence; do not guess."),
}


def _legacy_llm(request: Any) -> bool:
    provider = request.settings.get("plugins.ModelRequester.activate", "OpenAICompatible")
    return bool(request._model_key or request.settings.get(f"plugins.ModelRequester.{provider}.model")
                or provider != "OpenAICompatible")


def _select(request: Any, role: str) -> None:
    if role == "llm" and request._model_key:
        resolve_model_pool_settings(request._model_key, request.settings)
        request._model_key = None
        request._model_role = None
    elif not apply_role_profile(request, role):
        if role != "llm" or not _legacy_llm(request):
            raise ValueError(f"Configure {role}.provider/model before using this capability.")
    provider = request.settings.get("plugins.ModelRequester.activate", "OpenAICompatible")
    if request.settings.get(f"plugins.ModelRequester.{provider}.vision") is False:
        raise ValueError(f"The selected {role} profile explicitly disables vision.")


def _child(owner: Any, role: str):
    request = owner.agent.create_request(inherit_agent_prompt=False, inherit_extension_handlers=False)
    request.settings.update(deepcopy(owner.request.settings.get()))
    request.settings.parent = None
    request._model_key = None
    if not apply_role_profile(request, role):
        raise ValueError(f"Configure {role}.provider/model before using this capability.")
    provider = request.settings.get("plugins.ModelRequester.activate")
    if role == "vlm" and request.settings.get(f"plugins.ModelRequester.{provider}.vision") is False:
        raise ValueError("The vlm profile explicitly disables vision.")
    request.settings.set(f"plugins.ModelRequester.{provider}.request_retry", False)
    request.settings.set(f"plugins.ModelRequester.{provider}._api_key_pool_runtime", None)
    return request


class _Media:
    def __init__(self, owner: Any, options: ProductionOptions):
        self.owner, self.options = owner, options
        self.stages: list[tuple[str, Any]] = []
        self.meta: dict[str, Any] = {"stages": [], "retries_used": 0}
        self.direct_ocr = False
        self.ocr_text: list[str] = []
        self.original = deepcopy(owner.request.prompt.get())
        sources = getattr(owner, "_audio_inputs", [])
        if sources:
            from agently.core.model.AudioConfig import resolve_audio
            from agently.types.data.audio import AudioInput
            from pathlib import Path
            import mimetypes

            self.audio = resolve_audio(owner, "stt")
            for source in sources:
                if not isinstance(source, AudioInput):
                    path = Path(source)
                    source = AudioInput(path.read_bytes(), path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                if not source.data:
                    raise ValueError("STT requires non-empty audio bytes.")
                self.stages.append(("stt", source))
        self.prepare()
        # Resolve every stage before the first network operation. Invalid later
        # capabilities must not consume earlier audio/model calls.
        for role in {role for role, _ in self.stages} - {"stt"}:
            selected = resolve_role_profile(role, owner.request.settings, select_credentials=False)
            if selected is None:
                raise ValueError(f"Configure {role}.provider/model before using this capability.")
            provider, profile = selected
            owner.plugin_manager.get_plugin("ModelRequester", provider)
            if role == "vlm" and profile.get("vision") is False:
                raise ValueError("The vlm profile explicitly disables vision.")
        owner._media_meta = self.meta

    def prepare(self) -> None:
        request = self.owner.request
        groups = request.settings.get("execution.image_groups", []) or []
        attachment = self.original.get("attachment", []) or []
        direct = bool(request.settings.get("execution.image_direct", False))
        only = bool(request.settings.get("execution.vlm_only", False))
        vlm = resolve_role_profile("vlm", request.settings, select_credentials=False)
        llm = resolve_role_profile("llm", request.settings, select_credentials=False)
        has_llm = llm is not None or _legacy_llm(request)
        if only and vlm is None:
            raise ValueError("vlm_only(True) requires a configured vlm profile.")
        if direct and not groups:
            raise ValueError("to_text() requires image input.")
        if not groups:
            if only or (vlm is not None and not has_llm):
                apply_role_profile(request, "vlm")
            return
        modes = {group["mode"] for group in groups}
        if only and modes != {"vlm"}:
            raise ValueError("vlm_only conflicts with image mode=llm or mode=ocr.")
        if direct and len(modes) != 1:
            raise ValueError("to_text() requires a single image processing mode.")
        if not self.original.get("input") and not any(stage[0] == "stt" for stage in self.stages) and not any(item.get("type") == "text" for item in attachment):
            request.prompt.append("instruct", _DESCRIPTION)
            self.original = deepcopy(request.prompt.get())
        if only or (modes == {"vlm"} and (direct or not has_llm)):
            _select(request, "vlm" if vlm is not None else "llm")
            return
        if modes == {"llm"} or (modes == {"vlm"} and vlm is None):
            _select(request, "llm")
            return
        if "ocr" in modes:
            if resolve_role_profile("ocr", request.settings, select_credentials=False) is None:
                raise ValueError("Configure ocr.provider/model before mode=ocr.")
            self.direct_ocr = direct
            if direct and (self.original.get("output") is not None or self.original.get("input") or any(item.get("type") == "text" for item in attachment)):
                raise ValueError("Direct OCR extracts text only; use a configured llm for question/input/output processing.")
        if not has_llm and not self.direct_ocr:
            raise ValueError("Mixed image processing requires a configured llm profile.")
        if request._model_key:
            resolve_model_pool_settings(request._model_key, request.settings)
            request._model_key = None
            request._model_role = None
        elif llm is not None:
            apply_role_profile(request, "llm")
        visual: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        covered: set[int] = set()
        image_number = 0
        for group in groups:
            parts = attachment[group["start"]:group["end"]]
            numbered = []
            numbered_images = []
            for part in parts:
                if part.get("type") == "image_url":
                    image_number += 1
                    numbered.append({"type": "text", "text": f"Image {image_number}"})
                    numbered_images.append((image_number, part))
                numbered.append(part)
            if group["mode"] == "ocr":
                questions = [part["text"] for part in parts if part.get("type") == "text"]
                for number, part in numbered_images:
                    self.stages.append(("ocr", {"attachment": [part], "image": number, "questions": questions}))
            else:
                (visual if group["mode"] == "vlm" else retained).extend(numbered)
            covered.update(range(group["start"], group["end"]))
        retained.extend(part for index, part in enumerate(attachment) if index not in covered)
        if retained:
            provider = request.settings.get("plugins.ModelRequester.activate")
            if request.settings.get(f"plugins.ModelRequester.{provider}.vision") is False:
                raise ValueError("The llm profile explicitly disables vision but mode=llm images remain.")
        request.prompt.set("attachment", retained)
        request.settings.set("execution.image_groups", [])
        if visual:
            self.stages.append(("vlm", visual))

    async def dispatch(self, role: str, attachment: Any) -> None:
        owner = self.owner
        if role == "stt":
            await owner.emit_stream("execution.stage.started", {"stage": role}, source="agent_execution")
            record = {"stage": role, "status": "running"}
            self.meta["stages"].append(record)
            try:
                transcript = await self.audio.async_stt(attachment)
                content = {"audio_transcript": {"text": transcript.text, "model": transcript.model, "source": attachment.filename}}
                owner.request.prompt.append("info", content)
                owner.request.prompt.append("instruct", "Use the supplied audio transcript as the user's spoken task/input, together with the explicit overall task when provided.")
                self.original = deepcopy(owner.request.prompt.get())
                record.update(status="completed", model=transcript.model)
                await owner.emit_stream("execution.stage.completed", {"stage": role}, source="agent_execution")
                return
            except BaseException:
                record["status"] = "failed"
                raise
        request = _child(owner, role)
        if role == "ocr":
            request.attachment(attachment["attachment"])
        snapshot = deepcopy(self.original)
        for key in ("output", "output_format", "options", "_image_groups", "attachment", "ensure_all_keys", "_audio_inputs"):
            snapshot.pop(key, None)
        if role != "ocr":
            request.prompt.update(snapshot)
            request.attachment(attachment)
            request.info({"requested_final_output": self.original.get("output")})
            request.instruct("Extract visual evidence needed by the overall task and each image-local question. Preserve image numbers and uncertainty. Return evidence, not the final task answer.")
            request.output(_EVIDENCE)
        package = None
        if role == "vlm":
            from agently.utils import DataFormatter
            package = await owner.async_read_task_context(consumer_id=f"media:{owner.id}:vlm", phase="production")
            for block in package.blocks:
                lane = "instruct" if block.role == "instruction" else "examples" if block.role == "example" else "info"
                request.prompt.append(lane, {"task_context_blocks": [{
                    "content": DataFormatter.sanitize(block.content), "role": block.role,
                    "ref": block.source_ref, "completeness": block.completeness,
                }]})
        await owner.emit_stream("execution.stage.started", {"stage": role}, source="agent_execution")
        result = request.get_result(parent_run_context=owner.agent_execution_run_context)
        owner.record_model_response_id(result.id)
        record = {"stage": role, "request_id": result.id, "status": "running"}
        self.meta["stages"].append(record)
        try:
            evidence = await result.async_get_data(max_retries=0, raise_ensure_failure=True)
            if role == "ocr":
                self.ocr_text.append(str(evidence))
                owner.request.prompt.append("info", {"ocr_evidence": {"image": attachment["image"], "questions": attachment["questions"], "text": evidence}})
            else:
                owner.request.prompt.append("info", {"image_evidence": evidence})
            owner.request.prompt.append("instruct", "Use the supplied visual evidence for the task. Preserve uncertainty; do not promote ambiguous observations to established facts.")
            record["status"] = "completed"
            record["meta"] = await result.async_get_meta()
            await owner.emit_stream("execution.stage.completed", {"stage": role, "request_id": result.id}, source="agent_execution")
        except BaseException:
            record["status"] = "failed"
            raise
        finally:
            if package is not None:
                owner.record_context_consumption(package, request_id=str(result.response_id or result.id))


async def _step(data: TriggerFlowRuntimeData) -> None:
    runtime: _Media = data.require_resource("media")
    index = data.get_state("index", 0)
    if index >= len(runtime.stages):
        return
    try:
        await runtime.dispatch(*runtime.stages[index])
    except Exception as error:
        retries = runtime.meta["retries_used"]
        retryable = isinstance(error, (ValueError, httpx.RequestError, TimeoutError))
        if isinstance(error, httpx.HTTPStatusError):
            retryable = error.response.status_code in {408, 429} or error.response.status_code >= 500
        if not retryable or retries >= runtime.options.max_retries:
            raise
        runtime.meta["retries_used"] += 1
        await data.async_emit_nowait("RETRY", None)
        return
    await data.async_set_state("index", index + 1, emit=False)
    await data.async_emit_nowait("NEXT", None)


@lru_cache(maxsize=1)
def _flow() -> TriggerFlow:
    flow = TriggerFlow(name="agent-media-input")
    flow.to(_step)
    flow.when("NEXT").to(_step)
    flow.when("RETRY").to(_step)
    return flow


async def prepare_media(owner: Any, options: ProductionOptions) -> ProductionOptions:
    if owner.revision and getattr(owner, "_media_meta", None):
        # The retained request already contains successful evidence. Rework
        # revises the answer; it does not implicitly re-read source media.
        return options
    snapshot = deepcopy(owner.request.settings.get())
    owner.request.settings.parent = None
    owner.request.settings.update(snapshot)
    runtime = _Media(owner, options)
    if runtime.stages:
        # Resolve the final role before disabling nested provider retries.
        if owner.request._model_key:
            resolve_model_pool_settings(owner.request._model_key, owner.request.settings)
            owner.request._model_key = None
            owner.request._model_role = None
        elif owner.request._model_role:
            apply_role_profile(owner.request, owner.request._model_role)
        provider = owner.request.settings.get("plugins.ModelRequester.activate", "OpenAICompatible")
        owner.request.settings.set(f"plugins.ModelRequester.{provider}.request_retry", False)
        owner.request.settings.set(f"plugins.ModelRequester.{provider}._api_key_pool_runtime", None)
        flow = _flow().create_execution(auto_close=False, record_store=False, runtime_resources={"media": runtime},
                                        parent_run_context=owner.agent_execution_run_context, intervention_mode=None)
        try:
            await flow.async_start(None)
            await flow.async_close(reason="media_ready")
        except BaseException:
            if not flow.is_closed():
                with suppress(BaseException):
                    await flow.async_close(reason="media_failed", pending_interrupts="cancel")
            raise
    if runtime.direct_ocr:
        owner._media_result = "\n\n".join(runtime.ocr_text)
    owner._refresh_prompt_snapshot()
    return replace(options, max_retries=options.max_retries - runtime.meta["retries_used"])
