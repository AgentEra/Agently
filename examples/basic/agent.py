"""Configure an Agently agent with one of three model provider styles.

Agently ships three protocol-layer Request plugins. Pick the one that matches
the API protocol your endpoint exposes:

  - OpenAICompatible           OpenAI Chat Completions style   (POST /chat/completions)
  - OpenAIResponsesCompatible  OpenAI Responses API style      (POST /responses)
  - AnthropicCompatible        Anthropic / Claude Messages API (POST /messages)

No real API keys live in this file. Each setup reads credentials from
environment variables through Agently's "${ENV.NAME}" placeholders
(auto_load_env=True resolves them against os.environ), so copy the block you
need and set the matching variables (e.g. in a .env file loaded by dotenv).
"""

import asyncio
import os

from dotenv import find_dotenv, load_dotenv

from agently import Agently

# Pick which provider style to run: "chat" | "responses" | "anthropic"
PROVIDER = os.getenv("AGENTLY_EXAMPLE_PROVIDER", "chat")

# Load a local .env so the ${ENV.*} placeholders below can resolve.
load_dotenv(find_dotenv())

Agently.set_settings("response.streaming_parse", True)


def setup_chat_completions() -> None:
    """OpenAI Chat Completions compatible endpoints (OpenAI, DeepSeek, Ollama, ...).

    Settings aliases: "OpenAICompatible" / "OpenAI" / "OAIClient".
    """
    request_options = {}
    if os.getenv("AGENTLY_EXAMPLE_CHAT_BASE_URL"):
        base_url = os.getenv("AGENTLY_EXAMPLE_CHAT_BASE_URL", "")
        model = os.getenv("AGENTLY_EXAMPLE_CHAT_MODEL", "")
        auth = os.getenv("AGENTLY_EXAMPLE_CHAT_API_KEY", "")
    elif os.getenv("DEEPSEEK_API_KEY"):
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        model = os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")
        auth = os.getenv("DEEPSEEK_API_KEY", "")
        request_options = {"thinking": {"type": "disabled"}}
    else:
        base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
        model = os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b")
        auth = ""
    Agently.set_settings("plugins.ModelRequester.activate", "OpenAICompatible")
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": base_url,
            "model": model,
            "model_type": "chat",
            "auth": auth,
            "request_options": request_options,
        },
    )


def setup_responses() -> None:
    """OpenAI Responses API style endpoints.

    Settings aliases: "OpenAIResponsesCompatible" / "OpenAIResponses" / "Responses".
    """
    Agently.set_settings("plugins.ModelRequester.activate", "OpenAIResponsesCompatible")
    Agently.set_settings(
        "Responses",
        {
            "base_url": "${ENV.OPENAI_BASE_URL}",  # e.g. https://api.openai.com/v1
            "model": "${ENV.OPENAI_MODEL}",        # e.g. gpt-5.5
            "api_key": "${ENV.OPENAI_API_KEY}",
        },
        auto_load_env=True,
    )


def setup_anthropic() -> None:
    """Anthropic / Claude Messages API.

    Settings aliases: "AnthropicCompatible" / "Anthropic" / "Claude".
    """
    Agently.set_settings("plugins.ModelRequester.activate", "AnthropicCompatible")
    Agently.set_settings(
        "Anthropic",
        {
            "base_url": "${ENV.ANTHROPIC_BASE_URL}",  # e.g. https://api.anthropic.com
            "model": "${ENV.ANTHROPIC_MODEL}",        # e.g. claude-sonnet-4-20250514
            "api_key": "${ENV.ANTHROPIC_API_KEY}",
            "max_tokens": 4096,
        },
        auto_load_env=True,
    )


PROVIDER_SETUPS = {
    "chat": setup_chat_completions,
    "responses": setup_responses,
    "anthropic": setup_anthropic,
}
PROVIDER_SETUPS[PROVIDER]()

agent = Agently.create_agent()

agent.set_agent_prompt("system", "You're the cutest cat in the world")

agent.request.set_prompt("input", "Hi~")


async def run():
    streaming_parse_generator = (
        agent.instruct("Return all output fields. Include at least one short cat-like action.")
        .output(
            {
                "thinking": ("str",),
                "actions": [("str",)],
                "say": ("str",),
            }
        )
        .get_async_generator("instant")
    )

    thinking_status = False
    actions_status = False
    last_actions_index = ""
    current_actions_index = ""
    say_status = False

    async for data in streaming_parse_generator:
        if data.path == "thinking":
            if not thinking_status:
                print("[Think]:")
                thinking_status = True
            if data.delta:
                print(data.delta, end="", flush=True)
        if data.path.startswith("actions["):
            if not actions_status:
                print()
                print("[Actions]:")
                actions_status = True
            current_actions_index = data.path[8:-1]
            if current_actions_index != last_actions_index:
                print()
                print("- ", end="", flush=True)
                last_actions_index = current_actions_index
            if data.delta:
                print(data.delta, end="", flush=True)
        if data.path == "say":
            if not say_status:
                print("\n\n")
                print("[Say]:")
                say_status = True
            if data.delta:
                print(data.delta, end="", flush=True)


asyncio.run(run())

# Expected key output (PROVIDER="chat", one real DeepSeek or local Ollama run;
# the section keys [Think]/[Actions]/[Say] and the list shape are stable, the
# wording varies):
# [Think]:
# A human greeted me! I should respond in a cute cat way. ...
# [Actions]:
# - Perks up ears
# - Wags tail playfully
# - Paws at the air
# [Say]:
# Mewo~ Hi there, human!
#
# How it works:
# - PROVIDER selects one of three protocol-layer Request plugins; only one is
#   activated via plugins.ModelRequester.activate. The three setup_* functions
#   show the equivalent set_settings(...) shape for each API protocol, using
#   "${ENV.*}" placeholders so no real key is hard-coded.
# - get_async_generator("instant") yields streaming_parse nodes as the model
#   generates. Each node has a .path (e.g. "thinking", "actions[0]", "say") and
#   a .delta token. The loop dispatches on data.path to print section headers
#   once, then streams tokens inline. actions[*] elements are separated by
#   detecting index changes in data.path[8:-1]. The whole three-field structured
#   output renders progressively before the model finishes.
