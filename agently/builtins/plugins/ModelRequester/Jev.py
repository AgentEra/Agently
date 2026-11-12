"""TypeSafe System One protocol adapter. Execution owns composition and retries."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncGenerator, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from agently.core.model.AttemptRunner import AttemptRunner, core_attempt_runner_entrypoint
from agently.types.data import AgentlyRequestData, AttemptDecision, AttemptHandlers, AttemptState
from agently.types.data.judgment import Choice, OutputTemplate, Probability, Score
from agently.types.plugins import ModelRequester
from agently.types.settings import JevSettings
from agently.utils import SettingsNamespace, StateData


def supports_template(template: OutputTemplate) -> bool:
    return type(template) in (Probability, Choice, Score)


def to_question(template: OutputTemplate) -> dict[str, Any]:
    if not supports_template(template):
        raise ValueError(f"Jev does not support output template {type(template).__name__}; use an LLM provider.")
    if isinstance(template, Choice):
        return {"type": "choice", "instructions": template.question, "criteria": dict(template.options)}
    if isinstance(template, Score):
        return {"type": "score", "instructions": template.question, "criteria": list(template.options)}
    return {"type": "noul", "instructions": template.question}


def jev_enabled(settings: Any, *, path: str = "output", required: bool = False) -> bool:
    config = SettingsNamespace(settings, "plugins.ModelRequester.Jev")
    enabled = config.get("enabled")
    if enabled is False and not required:
        return False
    key = config.get("api_key")
    base = config.get("base_url")
    if enabled is None and not key and not base and not required:
        return False
    if enabled is False or not isinstance(key, str) or not key.strip():
        raise ValueError(
            f"{path}: Jev requires an API key and must not be disabled. "
            'Use Agently.set_settings("Jev", {"api_key": ..., "base_url": "https://api.typesafe.ai/v1"}), '
            "or set Jev.enabled=False to use the LLM output schema."
        )
    if base is not None and not isinstance(base, str):
        raise ValueError(f"{path}: Jev.base_url must be a string.")
    url = urlsplit(base or "https://api.typesafe.ai/v1")
    if (
        url.scheme not in {"http", "https"}
        or not url.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError(
            f"{path}: Jev.base_url must be an HTTP(S) API base URL without credentials, query or fragment."
        )
    model = config.get("model", "jev-latest")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{path}: Jev.model must be a non-empty model id.")
    timeout = config.get("timeout", 60.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{path}: Jev.timeout must be a positive finite number of seconds.")
    return True


class Jev(ModelRequester):
    name = "Jev"
    SETTINGS_SCHEMAS = {"plugins.ModelRequester.Jev": JevSettings}
    DEFAULT_SETTINGS = {
        "$mappings": {"path_mappings": {"Jev": "plugins.ModelRequester.Jev"}},
        "enabled": None,
        "api_key": None,
        "base_url": None,
        "model": "jev-latest",
        "timeout": 60.0,
        "batch_size": 64,
    }

    def __init__(self, prompt: Any, settings: Any):
        self.prompt = prompt
        self.settings = settings
        self.plugin_settings = SettingsNamespace(settings, "plugins.ModelRequester.Jev")
        self.leaves: dict[str, tuple[tuple[str, ...], OutputTemplate]] = {}

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        jev_enabled(self.settings, required=True)
        questions: dict[str, Any] = {}
        bound = self.prompt.get("jev_bound_output", {})

        def visit(value: Any, path: tuple[str, ...]) -> None:
            if isinstance(value, OutputTemplate):
                name = f"q{len(questions)}"
                question = to_question(value)
                if value.after_output is not None:
                    raise ValueError("after_output needs Agent Execution to resolve its dependencies.")
                if value.from_output is not None:
                    if (
                        not isinstance(value.from_output, str)
                        or not isinstance(bound, Mapping)
                        or value.from_output not in bound
                    ):
                        raise ValueError("from_output needs Agent Execution to resolve its dependencies.")
                    question["instructions"] = {
                        "question": value.question,
                        "target": bound[value.from_output],
                        "scope": "Evaluate target using the shared state as context.",
                    }
                questions[name] = question
                self.leaves[name] = (path, value)
            elif isinstance(value, Mapping):
                for key, child in value.items():
                    visit(child, (*path, str(key)))
            else:
                raise ValueError(
                    "A Jev ModelRequest requires only static judgment leaves. Use Agent Execution for mixed or dynamic output."
                )

        visit(self.prompt.get("output"), ())
        if not questions:
            raise ValueError("Jev output must contain at least one judgment.")
        if self.prompt.get("attachment") or self.prompt.get("tools"):
            raise ValueError("Jev supports text state and judgments, not attachments or Action calls.")
        # Reuse the ordinary prompt renderer, excluding the output contract and
        # host-resolved per-question targets. Credentials never enter this copy.
        state_prompt = {
            key: value
            for key, value in dict(self.prompt).items()
            if key not in {"output", "output_format", "ensure_all_keys", "jev_bound_output"}
        }
        state = (
            type(self.prompt.prompt_generator)(cast(Any, StateData(state_prompt)), self.settings).to_text()
            if state_prompt
            else ""
        )
        base = str(self.plugin_settings.get("base_url") or "https://api.typesafe.ai/v1")
        return AgentlyRequestData(
            request_url=f"{base.rstrip('/')}/systemone",
            headers={
                "Authorization": f"Bearer {self.plugin_settings.get('api_key')}",
                "Content-Type": "application/json",
            },
            client_options={"timeout": self.plugin_settings.get("timeout", 60.0), "trust_env": False},
            request_options={"stream": False},
            data={"model": self.plugin_settings.get("model", "jev-latest"), "state": state, "questions": questions},
        )

    def build_request_handlers(self, request_data: AgentlyRequestData) -> AttemptHandlers:
        async def execute(_state: AttemptState) -> AsyncGenerator[tuple[str, Any], None]:
            async with httpx.AsyncClient(**request_data.client_options) as client:
                response = await client.post(
                    request_data.request_url, headers=request_data.headers, json=request_data.data
                )
                response.raise_for_status()
                yield "response", response.json()

        return AttemptHandlers(execute=execute, handle_error=lambda error, _: AttemptDecision.raise_error(error))

    @core_attempt_runner_entrypoint
    async def request_model(self, request_data: AgentlyRequestData) -> AsyncGenerator[tuple[str, Any], None]:
        async for item in AttemptRunner(self.build_request_handlers(request_data)).run_stream():
            yield item

    async def broadcast_response(self, response_generator: AsyncGenerator) -> AsyncGenerator[tuple[str, Any], None]:
        async for event, payload in response_generator:
            if event in {"status", "error"}:
                yield event, payload
                continue
            result: Any = {}
            if not isinstance(payload, Mapping) or not isinstance(payload.get("answers"), Mapping):
                raise ValueError("Jev returned no answers map.")
            answers = payload["answers"]
            if set(answers) != set(self.leaves):
                raise ValueError("Jev answer ids do not match the requested questions.")
            for name, (path, declaration) in self.leaves.items():
                answer = answers[name]
                value = validate_answer(declaration, answer)
                if not path:
                    result = value
                else:
                    target = result
                    for key in path[:-1]:
                        target = target.setdefault(key, {})
                    target[path[-1]] = value
            content = json.dumps(result, ensure_ascii=False, allow_nan=False)
            yield "original_done", dict(payload)
            yield "extra", {"jev": dict(payload)}
            yield "meta", {"provider": "Jev", "model": payload.get("model"), "usage": payload.get("usage", {})}
            yield "delta", content
            yield "done", content


def _number(value: Any, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= maximum
    ):
        raise ValueError("Jev returned an invalid numeric judgment.")
    return float(value)


def validate_answer(declaration: OutputTemplate, answer: Any) -> str | float:
    kind = to_question(declaration)["type"]
    if not isinstance(answer, Mapping) or answer.get("type") != kind:
        raise ValueError("Jev answer type does not match its question.")
    value = answer.get(kind)
    if isinstance(declaration, Choice):
        if not isinstance(value, str) or value not in declaration.options:
            raise ValueError("Jev returned an unknown Choice candidate.")
        keys = set(declaration.options)
    elif isinstance(declaration, Score):
        value = _number(value, len(declaration.options) - 1)
        keys = {str(index) for index in range(len(declaration.options))}
        if not isinstance(answer.get("legend"), Mapping) or set(answer["legend"]) != keys:
            raise ValueError("Jev Score legend does not match its grades.")
    else:
        return _number(value, 1)
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, Mapping) or set(probabilities) != keys:
        raise ValueError("Jev returned an incomplete probability distribution.")
    total = sum(_number(probability, 1) for probability in probabilities.values())
    if not math.isclose(total, 1, abs_tol=0.001):
        raise ValueError("Jev probabilities must sum to one.")
    _number(answer.get("confidence"), 1)
    return value
