"""API key pool selection and failover policy smoke.

Run:
    python examples/model_configures/api_key_pool_failover.py

This is an infrastructure-focused example. It does not call a live model; it
shows how the request-local key selection/failover contract resolves before a
ModelRequester provider sends a request.
"""

from agently.utils import Settings, SettingsNamespace
from agently.utils.ModelPool import resolve_api_key_failover, resolve_model_pool_settings
from typing import Any, cast


settings = Settings()
settings.set("plugins.ModelRequester.activate", "OpenAICompatible")
settings.set("model_pool", {"support-chat": "deepseek-v4-flash-prod"})
settings.set(
    "model_profiles",
    {
        "deepseek-v4-flash-prod": {
            "provider": "OpenAICompatible",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "api_key_pool": "deepseek-prod",
            "request_options": {"thinking": {"type": "disabled"}},
        }
    },
)


def failover(error, context):
    if context.status_code == 429:
        return "try_next"
    if context.status_code in {405, 422}:
        return "raise"
    return "raise"


settings.set(
    "api_key_pools",
    cast(Any, {
        "deepseek-prod": {
            "selection": {"strategy": "fixed"},
            "failover": {"handler": failover, "max_attempts": 2},
            "keys": [
                {"id": "primary", "value": "example-key-primary"},
                {"id": "backup", "value": "example-key-backup"},
            ],
        }
    }),
)

resolve_model_pool_settings("support-chat", settings)
plugin_settings = SettingsNamespace(settings, "plugins.ModelRequester.OpenAICompatible")
runtime = cast(dict[str, Any], settings.get("plugins.ModelRequester.OpenAICompatible._api_key_pool_runtime"))

print("initial_key", runtime["selected_key_id"])
print("initial_api_key", settings.get("plugins.ModelRequester.OpenAICompatible.api_key"))

quota_decision = resolve_api_key_failover(
    plugin_settings,
    error=RuntimeError("provider quota limit"),
    status_code=429,
    response_text="quota limit",
    provider="OpenAICompatible",
)
print("quota_retry", quota_decision.retry)
print("quota_key", quota_decision.key_id)
print("quota_api_key", settings.get("plugins.ModelRequester.OpenAICompatible.api_key"))

schema_decision = resolve_api_key_failover(
    plugin_settings,
    error=RuntimeError("payload schema mismatch"),
    status_code=422,
    response_text="schema mismatch",
    provider="OpenAICompatible",
)
print("schema_retry", schema_decision.retry)

# Expected key output from a real run:
# initial_key primary
# initial_api_key example-key-primary
# quota_retry True
# quota_key backup
# quota_api_key example-key-backup
# schema_retry False
