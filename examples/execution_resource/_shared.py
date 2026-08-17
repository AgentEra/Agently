from __future__ import annotations

import os
from pprint import pprint
from typing import Literal

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.core import Action


ProviderName = Literal["ollama", "deepseek"]


def print_section(title: str, value=None):
    print(f"\n[{ title }]")
    if value is None:
        return
    if isinstance(value, str):
        print(value)
    else:
        pprint(value)


def configure_model(provider: ProviderName, *, temperature: float = 0.0):
    load_dotenv(find_dotenv())

    if provider == "ollama":
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
                "request_options": {
                    "temperature": temperature,
                },
            },
        )
    elif provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY. Put it in your environment or .env before running this example.")
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash"),
                "model_type": "chat",
                "auth": api_key,
                "request_options": {
                    "thinking": {"type": "disabled"},
                    "temperature": temperature,
                },
            },
        )
    else:
        raise ValueError(f"Unsupported model provider: {provider}")

    Agently.set_settings("debug", False)
    return Agently


def create_agent(provider: ProviderName, system_prompt: str, *, temperature: float = 0.0):
    configure_model(provider, temperature=temperature)
    agent = Agently.create_agent()
    agent.set_agent_prompt("system", system_prompt)
    agent.set_action_loop(max_rounds=4)
    return agent


def print_action_results(records):
    print_section("ACTION_RECORDS", records)
    print_section("ACTION_RESULTS_INJECTED_TO_REPLY", Action.to_action_results(records))


def print_response(result):
    print_section("MODEL_REPLY", result.get_text())

    extra = result.full_result_data.get("extra") or {}
    action_logs = extra.get("action_logs", extra.get("tool_logs", [])) if isinstance(extra, dict) else []

    print_section("ACTION_LOGS_FROM_RESPONSE_RESULT", action_logs)

# Helper module — no standalone terminal output.
# Imported by execution_resource examples to provide:
#   create_agent(provider, system_prompt, temperature=0.7) — configures Ollama or DeepSeek
#   print_action_results(records) — prints ACTION_RECORDS and ACTION_RESULTS_INJECTED_TO_REPLY
#   print_response(result) — prints MODEL_REPLY and ACTION_LOGS_FROM_RESPONSE_RESULT
#
# How it works:
# Callers supply provider name ("ollama" or "deepseek"); create_agent() reads the matching
# env vars (OLLAMA_BASE_URL or DEEPSEEK_BASE_URL + DEEPSEEK_API_KEY) and configures the
# OpenAICompatible settings before returning a new agent instance.
