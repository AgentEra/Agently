from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from dotenv import find_dotenv, load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agently import Agently


ProviderName = Literal["deepseek", "ollama"]


def configure_model(*, temperature: float = 0.0) -> ProviderName:
    load_dotenv(find_dotenv(usecwd=True))
    configured = os.getenv("DYNAMIC_TASK_MODEL_PROVIDER", "").strip().lower()
    if configured in {"deepseek", "ollama"}:
        provider = configured
    elif os.getenv("DEEPSEEK_API_KEY"):
        provider = "deepseek"
    else:
        provider = "ollama"

    if provider == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY. Set it or run with DYNAMIC_TASK_MODEL_PROVIDER=ollama.")
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
        return "deepseek"

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
    return "ollama"
