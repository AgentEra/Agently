from __future__ import annotations

import os
from pprint import pprint
from typing import Literal

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.core import Action


ProviderName = Literal["deepseek", "ollama"]


def _select_provider() -> ProviderName:
    configured = os.getenv("COOKBOOK_MODEL_PROVIDER", "").strip().lower()
    if configured in {"deepseek", "ollama"}:
        return configured  # type: ignore[return-value]
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "ollama"


def configure_model(*, temperature: float = 0.0) -> ProviderName:
    load_dotenv(find_dotenv())
    provider = _select_provider()

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Missing DEEPSEEK_API_KEY. Set it or run with COOKBOOK_MODEL_PROVIDER=ollama."
            )
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat"),
                "model_type": "chat",
                "auth": api_key,
                "request_options": {"temperature": temperature},
            },
        )
    else:
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                "api_key": os.getenv("OLLAMA_API_KEY", "ollama"),
                "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
                "model_type": "chat",
                "request_options": {"temperature": temperature},
            },
        )

    Agently.set_settings("debug", False)
    return provider


def create_model_agent(system_prompt: str, *, temperature: float = 0.0, max_rounds: int = 4):
    provider = configure_model(temperature=temperature)
    agent = Agently.create_agent()
    agent.set_agent_prompt("system", system_prompt)
    agent.set_action_loop(max_rounds=max_rounds)
    return agent, provider


def print_model_provider(provider: ProviderName):
    print("[MODEL_PROVIDER]")
    print(provider)


def print_action_results(records):
    print("[ACTION_RECORDS]")
    pprint(records)
    print("[ACTION_RESULTS_INJECTED_TO_REPLY]")
    pprint(Action.to_action_results(records))


def print_model_reply(response):
    print("[MODEL_REPLY]")
    print(response.result.get_text())

    extra = response.result.full_result_data.get("extra") or {}
    action_logs = extra.get("action_logs", extra.get("tool_logs", [])) if isinstance(extra, dict) else []
    print("[ACTION_LOGS_FROM_RESPONSE_RESULT]")
    pprint(action_logs)
