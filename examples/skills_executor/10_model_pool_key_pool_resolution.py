"""Model pool + key pool resolution through public Skills request paths.

Run:
    python examples/skills_executor/10_model_pool_key_pool_resolution.py

This demo validates the public three-layer resolution path:
  1. model_key → model_pool → model_name
  2. model_name → key_pool_strategy → key_id → key_pool → api_key
  3. Backward compat: absent model_key uses global model + key

It makes real DeepSeek calls to verify resolution works end-to-end. It uses
public request and Skills APIs only; deterministic internals belong in tests.

Expected key output from a real DeepSeek run on 2026-06-08:
  Demo 1 resolves model_key 'deepseek-v4' to model 'deepseek-v4-pro'
    and key_pool auth.
  Demo 2 response is 'OK' through the global model fallback.
  Demo 3 response is 'OK' after agent.activate_model("deepseek-v4").
  Demo 4 Skills finalizer response is {'status': 'ok'}.
  Demo 5 direct structured request response is {'status': 'ok'}.
  The script prints "Model pool + key pool resolution validated end-to-end".

Environment:
    DEEPSEEK_API_KEY must be available in the shell or a .env file.
    Optional:
      DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
      DEEPSEEK_DEFAULT_MODEL=deepseek-v4-flash
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

from agently import Agently
from agently.utils.ModelPool import resolve_model_pool_settings


def _check_env():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing DEEPSEEK_API_KEY. Put it in your shell or .env before running this example."
        )
    return api_key


async def main():
    api_key = _check_env()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    default_model = os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")

    # ═══════════════════════════════════════════════════════════
    #  Set up global model (fallback / backward compat)
    # ═══════════════════════════════════════════════════════════
    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": base_url,
            "model": default_model,
            "model_type": "chat",
            "auth": api_key,
            "request_options": {"thinking": {"type": "disabled"}},
        },
    )
    Agently.set_settings("debug", False)

    # ═══════════════════════════════════════════════════════════
    #  Create agent with model pool + key pool
    # ═══════════════════════════════════════════════════════════
    agent = Agently.create_agent("model-pool-demo")

    agent.set_settings(
        "model_pool",
        {
            "deepseek-v4": default_model,
        },
    )
    agent.set_settings(
        "key_pool",
        {
            "primary": api_key,
        },
    )
    agent.set_settings(
        "key_pool_strategy",
        {
            default_model: {
                "mode": "fixed",
                "pool": ["primary"],
            },
        },
    )
    agent.set_settings("skills.runtime.stage_model_keys", {"finalizer": "deepseek-v4"})

    # ═══════════════════════════════════════════════════════════
    #  Demo 1: unit-level resolution tests (no model call)
    # ═══════════════════════════════════════════════════════════
    print("═" * 60)
    print("Demo 1: public resolver smoke (no model call)")
    print("═" * 60)

    from agently.utils.Settings import Settings
    test_settings = Settings(name="Test-Settings")
    test_settings.set("model_pool", {"deepseek-v4": "deepseek-v4-pro"})
    test_settings.set("key_pool", {"prod-1": "sk-key1"})
    test_settings.set(
        "key_pool_strategy",
        {
            "deepseek-v4-pro": {"mode": "fixed", "pool": ["prod-1"]},
        },
    )
    test_settings.set("plugins.ModelRequester.activate", "OpenAICompatible")

    resolve_model_pool_settings("deepseek-v4", test_settings)
    assert test_settings.get("plugins.ModelRequester.OpenAICompatible.model") == "deepseek-v4-pro"
    assert test_settings.get("plugins.ModelRequester.OpenAICompatible.api_key") == "sk-key1"
    print("  model_key 'deepseek-v4' → model 'deepseek-v4-pro' + key_pool auth: OK")

    # ═══════════════════════════════════════════════════════════
    #  Demo 2: backward compat — no model_key → global model
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 2: backward compat (no model_key)")
    print("═" * 60)

    result = await agent.input("Reply with exactly 'OK' and nothing else.").async_get_text()
    print(f"  Response (global model, no model_key): '{result.strip()}'")
    assert result.strip() == "OK", f"Expected 'OK', got '{result.strip()}'"
    print("  backward compat: OK")

    # ═══════════════════════════════════════════════════════════
    #  Demo 3: activate_model → real call
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 3: activate_model")
    print("═" * 60)

    agent.activate_model("deepseek-v4")
    result = await agent.input("Reply with exactly 'OK' and nothing else.").async_get_text()
    print(f"  Response (active model='deepseek-v4'): '{result.strip()}'")
    assert result.strip() == "OK", f"Expected 'OK', got '{result.strip()}'"
    print("  activate_model: OK")

    # ═══════════════════════════════════════════════════════════
    #  Demo 4: model pool through public Skills execution
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 4: model pool via public Skills execution")
    print("═" * 60)
    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        skill_src = Path(__file__).resolve().parent / "skills" / "ok-status-skill"
        Agently.skills_executor.configure(
            registry_root=str(temp_path / "registry"),
            allowed_trust_levels=["local"],
        )
        contract = Agently.skills_executor.install_skills(skill_src, trust_level="local", update=True)
        skill_id = str(contract["skill_id"])
        execution = await agent.async_run_skills_task(
            "Return a JSON object with a single key 'status' set to 'ok'.",
            skills=[skill_id],
            mode="required",
            effort="fast",
            output={"status": (str, "ok or error", True)},
            output_format="json",
        )
    result = execution.output or {}
    print(f"  Response (Skills finalizer model_key): {result}")
    assert result.get("status") == "ok", f"Expected status 'ok', got {result}"
    print("  model pool via Skills execution: OK")

    # ═══════════════════════════════════════════════════════════
    #  Demo 5: model_key with direct structured request
    # ═══════════════════════════════════════════════════════════
    print()
    print("═" * 60)
    print("Demo 5: model_key with direct structured request")
    print("═" * 60)

    request = agent.create_temp_request(model_key="deepseek-v4")
    result = (
        request
        .input("Return a JSON object with a single key 'status' set to 'ok'.")
        .output({"status": (str, "ok or error", True)}, format="json")
        .get_result()
    )
    data = await result.async_get_data()
    print(f"  Response: {data}")
    assert data.get("status") == "ok", f"Expected status 'ok', got {data}"
    print("  model_key + structured output: OK")

    print()
    print("✅ Model pool + key pool resolution validated end-to-end")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
