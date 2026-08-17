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

import os
from typing import Any, Literal, TYPE_CHECKING, cast

from agently.core.model import _UNSET, _resolve_quick_prompt_input
from agently.core.model.AttachmentInput import ImageDetail, build_image_attachment
from agently.utils.LanguagePolicy import apply_language_policy_to_prompt, resolve_language_policy

if TYPE_CHECKING:
    from agently.core.Agent import BaseAgent
    from agently.core.model import ModelRequest
    from agently.types.data import OutputValidateHandler, PromptStandardSlot, SkillRuntimeStreamHandler


class AgentExecutionPromptDraft:
    """Execution-local prompt draft backed by one isolated ModelRequest."""

    def __init__(self, agent: "BaseAgent", request: "ModelRequest"):
        self.agent = agent
        self.request = request
        self.request_prompt = request.prompt
        self.prompt = self.request_prompt

    def snapshot(self) -> dict[str, Any]:
        prompt_snapshot = self.request.prompt.get()
        return dict(prompt_snapshot) if isinstance(prompt_snapshot, dict) else {}

    def set_execution_prompt(
        self,
        key: "PromptStandardSlot | str",
        value: Any,
        *,
        mappings: dict[str, Any] | None = None,
    ):
        self.request.prompt.set(key, value, mappings=mappings)
        return self

    def remove_execution_prompt(self, key: "PromptStandardSlot | str"):
        self.request.prompt.set(key, None)
        return self

    def validate(self, handler: "OutputValidateHandler"):
        self.request.validate(handler)
        return self

    def system(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False):
        if always:
            self.agent.system(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("system", prompt, mappings=mappings)
        return self

    def rule(self, prompt: Any, *, mappings: dict[str, Any] | None = None, always: bool = False):
        if always:
            self.agent.rule(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.append("instruct", "{system.rule} ARE IMPORTANT RULES YOU SHALL FOLLOW!")
            self.request.prompt.set("system.rule", prompt, mappings=mappings)
        return self

    def role(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent.role(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.append("instruct", "YOU MUST REACT AND RESPOND AS {system.role}!")
            self.request.prompt.set("system.your_role", prompt, mappings=mappings)
        return self

    def user_info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent.user_info(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.append("instruct", "{system.user_info} IS IMPORTANT INFORMATION ABOUT USER!")
            self.request.prompt.set("system.user_info", prompt, mappings=mappings)
        return self

    def input(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent.input(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("input", prompt, mappings=mappings)
        return self

    def info(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent.info(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("info", prompt, mappings=mappings)
        return self

    def instruct(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent.instruct(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("instruct", prompt, mappings=mappings)
        return self

    def examples(
        self,
        prompt: Any = _UNSET,
        value: Any = _UNSET,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        **kwargs: Any,
    ):
        prompt, mappings = _resolve_quick_prompt_input(prompt, value, mappings, kwargs)
        if always:
            self.agent.examples(prompt, mappings=mappings, always=True)
        else:
            self.request.prompt.set("examples", prompt, mappings=mappings)
        return self

    def output(
        self,
        prompt: Any,
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
        format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
    ):
        if always:
            self.agent.output(prompt, mappings=mappings, always=True, format=format)
        else:
            self.request.prompt.set("output", prompt, mappings=mappings)
            if format is not None:
                self.request.prompt.set("output_format", format)
        return self

    def attachment(
        self,
        prompt: list[dict[str, Any]],
        *,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        if always:
            self.agent.attachment(prompt, mappings=mappings, always=True)
        else:
            self.request_prompt.set("attachment", prompt, mappings=mappings)
        return self

    def image(
        self,
        *,
        question: str,
        file: str | os.PathLike[str] | None = None,
        url: str | None = None,
        files: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
        urls: list[str] | tuple[str, ...] | None = None,
        detail: ImageDetail | None = None,
        mappings: dict[str, Any] | None = None,
        always: bool = False,
    ):
        attachment = build_image_attachment(
            question=question,
            file=file,
            url=url,
            files=files,
            urls=urls,
            detail=detail,
        )
        if always:
            self.agent.image(
                question=question,
                file=file,
                url=url,
                files=files,
                urls=urls,
                detail=detail,
                mappings=mappings,
                always=True,
            )
        else:
            self.request_prompt.set("attachment", attachment, mappings=mappings)
        return self

    def set_prompt_options(self, options: dict[str, Any], *, always: bool = False):
        if always:
            self.agent.options(options, always=True)
        else:
            self.request.prompt.set("options", options)
        return self

    def language(
        self,
        language: Any = "auto",
        *,
        output: Any = None,
        process: Any = None,
        progress: Any = None,
        accept_language: Any = None,
        always: bool = False,
    ):
        if always:
            self.agent.language(
                language,
                output=output,
                process=process,
                progress=progress,
                accept_language=accept_language,
            )
        else:
            policy = resolve_language_policy(
                language,
                output_language=output,
                process_language=process,
                progress_language=progress,
                accept_language=accept_language,
            )
            apply_language_policy_to_prompt(self.request.prompt, policy)
        return self

    def use_dynamic_task(self, *args: Any, **kwargs: Any):
        self.agent.use_dynamic_task(*args, **kwargs)
        return self

    def _skills_prompt_defaults(
        self,
        task: str | None,
        *,
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        _settings_overrides: dict[str, Any] | None = None,
    ):
        if output is not None and semantic_outputs is not None:
            raise ValueError("Use either output= or semantic_outputs= for Skills execution, not both.")
        prompt_defaults = self.agent._dynamic_task_prompt_defaults(
            task,
            prompt_snapshot=self.snapshot(),
        )
        resolved_task = task if task is not None and prompt_defaults["target"] is None else prompt_defaults["target"]
        if not resolved_task:
            raise ValueError("Skills execution requires task=... or a configured agent.input(...).")
        explicit_output = output if output is not None else semantic_outputs
        resolved_output = explicit_output if explicit_output is not None else prompt_defaults["output_schema"]
        resolved_format = output_format or cast(Any, prompt_defaults["output_format"]) or "auto"
        return str(resolved_task), resolved_output, resolved_format

    async def async_resolve_skills_plan(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: Any = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        _settings_overrides: dict[str, Any] | None = None,
    ):
        task, output, output_format = self._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        return await cast(Any, self.agent).async_resolve_skills_plan(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            output=output,
            output_format=output_format,
            _settings_overrides=_settings_overrides,
        )

    def resolve_skills_plan(self, *args: Any, **kwargs: Any):
        return default_stage_call_bridge.as_sync(self.async_resolve_skills_plan)(*args, **kwargs)

    async def async_run_skills_task(
        self,
        task: str | None = None,
        *,
        skills: Any = None,
        skills_packs: Any = None,
        mode: Any = "model_decision",
        output: Any = None,
        semantic_outputs: Any = None,
        output_format: Literal["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal", "auto"] | None = None,
        stream_handler: "SkillRuntimeStreamHandler | None" = None,
        effort: str | None = None,
        _settings_overrides: dict[str, Any] | None = None,
    ):
        task, output, output_format = self._skills_prompt_defaults(
            task,
            output=output,
            semantic_outputs=semantic_outputs,
            output_format=output_format,
        )
        return await cast(Any, self.agent).async_run_skills_task(
            task,
            skills=skills,
            skills_packs=skills_packs,
            mode=mode,
            output=output,
            output_format=output_format,
            stream_handler=stream_handler,
            effort=effort,
            _settings_overrides=_settings_overrides,
        )

    def run_skills_task(self, *args: Any, **kwargs: Any):
        return default_stage_call_bridge.as_sync(self.async_run_skills_task)(*args, **kwargs)

    def create_dynamic_task(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("_prompt_snapshot", self.snapshot())
        return self.agent.create_dynamic_task(*args, **kwargs)

    def get_prompt_text(self):
        return self.request_prompt.to_text()[6:][:-11]
