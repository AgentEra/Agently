"""Provider selection for output templates; Execution owns scheduling and retries."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from agently.builtins.plugins.ModelRequester.Jev import jev_enabled
from agently.types.settings import SystemOneSettings
from agently.utils import Settings
from agently.utils.ModelPool import resolve_model_pool_settings


class SystemOne:
    def __init__(self, settings: Settings, *, path: str = "output"):
        raw = settings.get("system_one", {})
        self.enabled = False
        self.provider: str | None = None
        self.profile: dict[str, Any] = {}
        self.batch_size = 64
        self.batch_size_explicit = False
        if raw is None or raw == {}:
            return
        if not isinstance(raw, Mapping):
            raise ValueError("system_one must be a model configuration object.")
        if raw.get("enabled") is False:
            return
        config = SystemOneSettings.model_validate(dict(raw), strict=True).to_dict()
        enabled = config.pop("enabled", None)
        self.batch_size_explicit = "batch_size" in config
        self.batch_size = config.pop("batch_size", 64)
        key = config.pop("model_key", None)
        if not config and not key:
            if enabled:
                raise ValueError(
                    "SystemOne is enabled without a model; configure system_one.provider/model or system_one.model_key."
                )
            return
        if key:
            if config:
                raise ValueError("system_one.model_key cannot be combined with an inline model profile.")
            pool = settings.get("model_pool", {})
            if not isinstance(pool, Mapping) or key not in pool:
                raise ValueError(f"system_one.model_key {key!r} must exist in model_pool.")
            selected = Settings()
            for name in ("model_pool", "model_profiles", "api_key_pools", "key_pool", "key_pool_strategy"):
                selected.set(name, deepcopy(settings.get(name, {})))
            selected.set(
                "plugins.ModelRequester.activate", settings.get("plugins.ModelRequester.activate", "OpenAICompatible")
            )
            resolve_model_pool_settings(key, selected)
            provider = selected.get("plugins.ModelRequester.activate")
            profile = selected.get(f"plugins.ModelRequester.{provider}", {})
            if not isinstance(provider, str) or not isinstance(profile, Mapping):
                raise ValueError("system_one.model_key did not resolve a model profile.")
            self.provider = provider
            self.profile = dict(profile)
        else:
            self.provider = config.pop("provider", None) or "OpenAICompatible"
            self.profile = config
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("system_one.provider must be a non-empty ModelRequester name.")
        if self.provider == "Jev":
            configured = settings.get("plugins.ModelRequester.Jev", {})
            if not isinstance(configured, Mapping):
                raise ValueError("Jev configuration must be an object.")
            self.profile = {**deepcopy(dict(configured)), **self.profile}
            selected = Settings()
            selected.set("plugins.ModelRequester.Jev", self.profile)
            if self.profile.get("enabled") is False:
                return
            jev_enabled(selected, required=True, path=path)
        elif not isinstance(self.profile.get("model"), str) or not self.profile["model"].strip():
            raise ValueError("SystemOne requires an explicit system_one.model for its LLM provider.")
        self.enabled = True

    def apply(self, request: Any) -> None:
        if not self.enabled or self.provider is None:
            raise RuntimeError("Cannot select a disabled SystemOne provider.")
        plugin = request.plugin_manager.get_plugin("ModelRequester", self.provider)
        defaults = {
            key: deepcopy(value) for key, value in getattr(plugin, "DEFAULT_SETTINGS", {}).items() if key != "$mappings"
        }
        request.settings.set(f"plugins.ModelRequester.{self.provider}", {**defaults, **deepcopy(self.profile)})
        request.settings.set("plugins.ModelRequester.activate", self.provider)
        request._model_key = None
