from __future__ import annotations

import os
from pprint import pprint

from dotenv import find_dotenv, load_dotenv

from agently import Agently
from agently.core import Action


def configure_deepseek(*, temperature: float = 0.1):
    load_dotenv(find_dotenv())

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
    Agently.set_settings("debug", False)
    return Agently


def create_deepseek_agent(system_prompt: str):
    configure_deepseek()
    agent = Agently.create_agent()
    agent.set_agent_prompt("system", system_prompt)
    agent.set_action_loop(max_rounds=4)
    return agent


def print_action_results(records):
    print("[INTERMEDIATE_ACTION_RESULTS]")
    pprint(records)
    print("[INTERMEDIATE_ACTION_RESULTS_FOR_REPLY]")
    pprint(Action.to_action_results(records))


def print_response(result):
    print("[REPLY]")
    print(result.get_text())

    extra = result.full_result_data.get("extra") or {}
    action_logs = extra.get("action_logs", extra.get("tool_logs", [])) if isinstance(extra, dict) else []

    print("[ACTION_LOGS_FROM_RESPONSE_RESULT]")
    pprint(action_logs)

# Helper module — no standalone terminal output.
# Imported by action_runtime examples to provide:
#   create_deepseek_agent(system_prompt) — configures DeepSeek via env vars and returns an agent
#   configure_deepseek(temperature=0.1) — configures DeepSeek settings globally
#   print_action_results(records) — prints INTERMEDIATE_ACTION_RESULTS and INTERMEDIATE_ACTION_RESULTS_FOR_REPLY
#   print_response(result) — prints REPLY and ACTION_LOGS_FROM_RESPONSE_RESULT
#
# How it works:
# configure_deepseek() reads DEEPSEEK_API_KEY and DEEPSEEK_BASE_URL from the environment
# (or .env file via python-dotenv) and writes them to Agently.set_settings("OpenAICompatible").
# create_deepseek_agent() calls configure_deepseek() then creates an agent with the given
# system prompt and sets max action loop rounds to 4.
