"""Mistral OCR image extraction, without a conversational-model fallback."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from typing import Any

import httpx

from agently.core.model.AttemptRunner import AttemptRunner, core_attempt_runner_entrypoint
from agently.types.data import AgentlyRequestData, AttemptDecision, AttemptHandlers, AttemptState
from agently.utils import SettingsNamespace


class MistralOCR:
    name = "MistralOCR"
    DEFAULT_SETTINGS = {"base_url": "https://api.mistral.ai/v1", "request_options": {}, "client_options": {}}

    def __init__(self, prompt: Any, settings: Any):
        self.prompt = prompt
        self.plugin_settings = SettingsNamespace(settings, f"plugins.ModelRequester.{self.name}")

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        images = [item["image_url"]["url"] for item in (self.prompt.get("attachment") or []) if item.get("type") == "image_url"]
        if len(images) != 1:
            raise ValueError("MistralOCR accepts one image per atomic request; Execution groups multiple images.")
        if self.prompt.get("output") is not None:
            raise ValueError("MistralOCR extracts text; use a following LLM for an output schema.")
        config: Any = self.plugin_settings
        if not config.get("model"):
            raise ValueError("MistralOCR requires an explicit model.")
        options = dict(config.get("request_options", {}) or {})
        if {"model", "document", "document_annotation_format", "document_annotation_prompt"}.intersection(options):
            raise ValueError("OCR request_options cannot override the model, document or output contract.")
        headers = dict(config.get("headers", {}) or {})
        if config.get("api_key"):
            headers["Authorization"] = f"Bearer {config.get('api_key')}"
        clients = {"timeout": 120.0, **dict(config.get("client_options", {}) or {})}
        if config.get("timeout"):
            clients["timeout"] = httpx.Timeout(120.0, **config.get("timeout"))
        return AgentlyRequestData(
            request_url=config.get("full_url") or f"{str(config.get('base_url')).rstrip('/')}/ocr",
            client_options=clients, headers=headers, request_options={"stream": False},
            data={**options, "model": config.get("model"), "document": {"type": "image_url", "image_url": images[0]}},
        )

    def build_request_handlers(self, request_data: AgentlyRequestData) -> AttemptHandlers:
        async def execute(_state: AttemptState) -> AsyncGenerator[tuple[str, Any], None]:
            async with httpx.AsyncClient(**request_data.client_options) as client:
                response = await client.post(request_data.request_url, headers=request_data.headers, json=request_data.data)
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
            pages = payload.get("pages") if isinstance(payload, Mapping) else None
            if not isinstance(pages, list) or any(not isinstance(page, Mapping) or not isinstance(page.get("markdown"), str) for page in pages):
                raise ValueError("MistralOCR returned invalid pages.")
            yield "original_done", payload
            yield "meta", {"provider": self.name, "model": payload.get("model"), "usage": payload.get("usage_info", {})}
            yield "done", "\n\n".join(page["markdown"] for page in pages)
