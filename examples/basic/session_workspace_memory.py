from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from agently import Agently


# Expected key output (recorded from a real DeepSeek run on 2026-07-03):
# [MODEL_PROVIDER]
# deepseek
# [MEMORY_RECORD_COUNT]
# 3
# [MEMORY_SCOPES]
# ['GLOBAL_MEMORY']
# [RECALL_KEY_FACTS]
# Atlas Renewals; concise bullet points


def configure_model() -> str:
    load_dotenv(find_dotenv())
    if os.getenv("DEEPSEEK_API_KEY"):
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash"),
                "model_type": "chat",
                "auth": os.getenv("DEEPSEEK_API_KEY"),
                "request_options": {
                    "thinking": {"type": "disabled"},
                    "temperature": 0.0,
                },
            },
        )
        return "deepseek"
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            "api_key": os.getenv("OLLAMA_API_KEY", "ollama"),
            "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
            "model_type": "chat",
            "request_options": {"temperature": 0.0},
        },
    )
    return "ollama"


async def main() -> None:
    provider = configure_model()
    Agently.set_settings("debug", False)

    record_store_root = Path(".example_runtime/session_record_store_memory").resolve()
    if record_store_root.exists():
        shutil.rmtree(record_store_root)

    agent = Agently.create_agent("session-memory-demo").use_record_store(
        record_store_root,
        mode="read_write",
    )
    agent.set_agent_prompt(
        "system",
        "You are a concise support assistant. Use retrieved memory when it is relevant.",
    )
    agent.set_settings(
        "session.memory.AgentlyMemory.body_schema",
        {
            "project": "string",
            "preference": "string",
            "evidence": "short string",
        },
    )
    agent.set_settings("session.memory.AgentlyMemory.extract.max_memories", 2)
    agent.set_settings(
        "session.memory.AgentlyMemory.retrieve.budget",
        {"chars": 2000, "item_chars": 800, "rerank_candidates": 3},
    )

    agent.activate_session(session_id="session-memory-demo")
    assert agent.activated_session is not None
    agent.activated_session.use_memory(mode="AgentlyMemory")

    agent.set_settings("session.memory.AgentlyMemory.retrieve.enabled", False)
    await agent.input(
        "Please remember these two durable facts: my project is Atlas Renewals, "
        "and I prefer concise bullet-point updates."
    ).async_get_text()

    agent.set_settings("session.memory.AgentlyMemory.retrieve.enabled", True)
    recall = await agent.input(
        "What project name and presentation style should you remember for my updates?"
    ).async_get_text()

    refs = await agent.record_store.grep(None, filters={"collection": "memory"})
    scopes = sorted({
        str(ref["scope"]["memory_scope"])
        for ref in refs
        if ref.get("scope") and ref["scope"].get("memory_scope") is not None
    })

    print("[MODEL_PROVIDER]")
    print(provider)
    print("[MEMORY_RECORD_COUNT]")
    print(len(refs))
    print("[MEMORY_SCOPES]")
    print(scopes)
    print("[RECALL_REPLY]")
    print(recall)


if __name__ == "__main__":
    asyncio.run(main())
