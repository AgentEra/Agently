# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Model pool + key pool resolution.

Resolves a caller-provided ``model_key`` into concrete provider settings. The
current layered shape is::

    model_key -> model_pool -> model_profile -> api_key_pool -> key

Legacy ``model_pool``/``key_pool_strategy``/``key_pool`` settings remain
supported for compatibility.
"""

from __future__ import annotations

import inspect
import os
import random
import re
import threading
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from agently.types.data import (
    APIKeyFailoverContext,
    APIKeySelectionContext,
    ModelProfileResolution,
)

if TYPE_CHECKING:
    from agently.utils.Settings import Settings

_ENV_PLACEHOLDER = re.compile(r"\$\{\s*(?:ENV\.)?([^}]+?)\s*\}")
_DEFAULT_FAILOVER_STATUS_CODES = {401, 403, 429}
_FAILOVER_ACTION_ALIASES = {
    "retry_next": "try_next",
    "next": "try_next",
    "raise_error": "raise",
    "error": "raise",
}

# Module-level state for key selection strategies (process-scoped).
_round_robin_counters: dict[str, int] = {}
_usage_counters: dict[str, int] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class APIKeyFailoverDecision:
    retry: bool
    action: str
    key_id: str | None = None
    api_key: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _ResolvedModelRoute:
    model_key: str
    model_name: str | None
    profile: dict[str, Any]
    provider: str


def _resolve_env(value: str) -> str:
    """Resolve ``${ENV.VAR_NAME}`` placeholders against ``os.environ``."""

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, "")

    return _ENV_PLACEHOLDER.sub(_replace, value)


def _select_key(mode: str, pool: list[str], model_name: str, key_pool: dict[str, str]) -> str:
    """Apply a key selection strategy and return a *key_id* from *pool*."""
    if not pool:
        raise ValueError("key_pool_strategy pool must not be empty")

    if mode == "fixed":
        return pool[0]

    if mode == "random":
        return random.choice(pool)

    with _lock:
        if mode == "round_robin":
            idx = _round_robin_counters.get(model_name, 0)
            selected = pool[idx % len(pool)]
            _round_robin_counters[model_name] = idx + 1
            return selected

        if mode == "least_used":
            selected = min(pool, key=lambda kid: _usage_counters.get(kid, 0))
            _usage_counters[selected] = _usage_counters.get(selected, 0) + 1
            return selected

    raise ValueError(f"Unknown key_pool_strategy mode: {mode!r}")


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return dict(value) if isinstance(value, dict) else {}


def _as_callable(value: Any) -> Callable[..., Any] | None:
    return value if callable(value) else None


def _call_handler(handler: Callable[..., Any], context: Any) -> Any:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return handler(context)
    required_positional_count = 0
    positional_count = 0
    accepts_varargs = False
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            accepts_varargs = True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_count += 1
            if parameter.default is inspect.Parameter.empty:
                required_positional_count += 1
    if isinstance(context, APIKeyFailoverContext) and (accepts_varargs or positional_count >= 2):
        return handler(context.error, context)
    if required_positional_count == 0 and positional_count == 0 and not accepts_varargs:
        return handler()
    return handler(context)


def _get_strategy_config(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    if callable(value):
        return {"handler": value}
    if isinstance(value, str):
        return {"strategy": value}
    return _as_dict(value)


def _build_key_entry(
    *,
    pool_id: str,
    index: int,
    raw_entry: Any,
    legacy_key_pool: dict[str, str],
) -> dict[str, Any] | None:
    if isinstance(raw_entry, str):
        key_id = raw_entry
        raw_key = legacy_key_pool.get(raw_entry, raw_entry)
        return {
            "id": key_id,
            "value": _resolve_env(str(raw_key)),
            "index": index,
        }
    entry = _as_dict(raw_entry)
    if not entry:
        return None
    key_id = str(entry.get("id") or entry.get("key_id") or entry.get("name") or f"{ pool_id }[{ index }]")
    raw_key = entry.get("value")
    if raw_key is None:
        raw_key = entry.get("api_key")
    if raw_key is None:
        raw_key = entry.get("auth")
    if raw_key is None:
        raw_key = legacy_key_pool.get(key_id)
    if raw_key is None:
        return None
    key_entry: dict[str, Any] = {
        "id": key_id,
        "value": _resolve_env(str(raw_key)),
        "index": index,
    }
    if "weight" in entry:
        key_entry["weight"] = entry["weight"]
    if "tags" in entry:
        key_entry["tags"] = entry["tags"]
    return key_entry


def _coerce_key_entry(choice: Any, key_entries: list[dict[str, Any]], *, default_id: str) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, int):
        if 0 <= choice < len(key_entries):
            return key_entries[choice]
        return None
    if isinstance(choice, str):
        for entry in key_entries:
            if entry.get("id") == choice:
                return entry
        return None
    if isinstance(choice, Mapping):
        choice_dict = dict(choice)
        choice_id = choice_dict.get("id") or choice_dict.get("key_id") or choice_dict.get("name")
        if choice_id is not None:
            for entry in key_entries:
                if entry.get("id") == str(choice_id):
                    return entry
        raw_key = choice_dict.get("value")
        if raw_key is None:
            raw_key = choice_dict.get("api_key")
        if raw_key is None:
            raw_key = choice_dict.get("auth")
        if raw_key is None:
            return None
        return {
            "id": str(choice_id or default_id),
            "value": _resolve_env(str(raw_key)),
            "index": len(key_entries),
        }
    return None


def _select_key_entry(
    *,
    pool_id: str,
    selection_config: dict[str, Any],
    key_entries: list[dict[str, Any]],
    legacy_mode: str,
) -> dict[str, Any]:
    handler = _as_callable(selection_config.get("handler"))
    strategy = str(selection_config.get("strategy") or selection_config.get("mode") or legacy_mode or "fixed")
    if handler is not None:
        context = APIKeySelectionContext(
            pool_id=pool_id,
            keys=[entry.copy() for entry in key_entries],
            strategy=strategy,
        )
        selected = _coerce_key_entry(
            _call_handler(handler, context),
            key_entries,
            default_id=f"{ pool_id }.selected",
        )
        if selected is not None:
            return selected
    key_values = {str(entry["id"]): str(entry["value"]) for entry in key_entries}
    selected_key_id = _select_key(
        strategy, [str(entry["id"]) for entry in key_entries], f"api_key_pool:{ pool_id }", key_values
    )
    selected_entry = _coerce_key_entry(selected_key_id, key_entries, default_id=f"{ pool_id }.selected")
    if selected_entry is None:
        raise ValueError(f"API key pool selection returned unknown key id: { selected_key_id }")
    return selected_entry


def _resolve_api_key_pool(
    *,
    pool_id: str,
    api_key_pools: dict[str, Any],
    legacy_key_pool: dict[str, str],
) -> tuple[str | None, dict[str, Any] | None]:
    pool_config = _as_dict(api_key_pools.get(pool_id))
    if not pool_config:
        return None, None
    selection_config = _get_strategy_config(pool_config, "selection")
    failover_config = _get_strategy_config(pool_config, "failover")
    mode = str(pool_config.get("strategy") or pool_config.get("mode") or "fixed")
    raw_entries = pool_config.get("keys")
    if raw_entries is None:
        raw_entries = pool_config.get("pool", [])
    if not isinstance(raw_entries, list):
        return None, None

    key_entries: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(raw_entries):
        key_entry = _build_key_entry(
            pool_id=pool_id,
            index=index,
            raw_entry=raw_entry,
            legacy_key_pool=legacy_key_pool,
        )
        if key_entry is not None:
            key_entries.append(key_entry)
    if not key_entries:
        return None, None
    selected = _select_key_entry(
        pool_id=pool_id,
        selection_config=selection_config,
        key_entries=key_entries,
        legacy_mode=mode,
    )
    runtime = {
        "pool_id": pool_id,
        "selection": selection_config,
        "failover": failover_config,
        "keys": key_entries,
        "selected_key_id": selected["id"],
        "attempts": [{"key_id": selected["id"], "action": "initial"}],
    }
    return str(selected["value"]), runtime


def _resolve_legacy_api_key(
    *,
    model_name: str,
    key_pool: dict[str, str],
    key_pool_strategy: dict[str, dict[str, Any]],
) -> str | None:
    strategy = key_pool_strategy.get(model_name)
    if not strategy:
        return None
    mode = strategy.get("mode", "fixed")
    pool_ids: list[str] = strategy.get("pool", [])
    if not pool_ids:
        return None
    selected_key_id = _select_key(str(mode), pool_ids, model_name, key_pool)
    raw_key = key_pool.get(selected_key_id, "")
    return _resolve_env(raw_key) if raw_key else None


def _normalize_failover_action(action: Any) -> str:
    normalized = str(action or "raise")
    return _FAILOVER_ACTION_ALIASES.get(normalized, normalized)


def _failover_max_attempts(failover_config: dict[str, Any], key_count: int) -> int:
    raw_value = failover_config.get("max_attempts")
    if isinstance(raw_value, int) and raw_value > 0:
        return raw_value
    return max(1, key_count)


def _status_is_retryable(failover_config: dict[str, Any], status_code: int | None) -> bool:
    status_codes = failover_config.get("retry_status_codes")
    if status_codes is None:
        status_codes = failover_config.get("status_codes")
    if status_codes is None:
        retry_status_codes = _DEFAULT_FAILOVER_STATUS_CODES
    else:
        retry_status_codes = {int(code) for code in status_codes if isinstance(code, int) or str(code).isdigit()}
    return status_code in retry_status_codes


def _next_key_entry(runtime: dict[str, Any]) -> dict[str, Any] | None:
    key_entries = [entry for entry in runtime.get("keys", []) if isinstance(entry, dict)]
    if not key_entries:
        return None
    current_key_id = runtime.get("selected_key_id")
    attempted_ids = {
        attempt.get("key_id")
        for attempt in runtime.get("attempts", [])
        if isinstance(attempt, dict) and attempt.get("key_id") is not None
    }
    start_index = 0
    for index, entry in enumerate(key_entries):
        if entry.get("id") == current_key_id:
            start_index = index + 1
            break
    ordered = key_entries[start_index:] + key_entries[:start_index]
    for entry in ordered:
        if entry.get("id") not in attempted_ids:
            return entry
    return None


def _runtime_key_entry(runtime: dict[str, Any], choice: Any) -> dict[str, Any] | None:
    key_entries = [entry for entry in runtime.get("keys", []) if isinstance(entry, dict)]
    return _coerce_key_entry(choice, key_entries, default_id=f"{ runtime.get('pool_id', 'api_key_pool') }.failover")


def _parse_failover_choice(choice: Any, runtime: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    if choice is True:
        return "try_next", None
    if choice is False or choice is None:
        return "raise", None
    if isinstance(choice, str):
        action = _normalize_failover_action(choice)
        if action in {"try_next", "retry_same", "raise", "stop"}:
            return action, None
        entry = _runtime_key_entry(runtime, choice)
        if entry is not None:
            return "retry_key", entry
        return "raise", None
    if isinstance(choice, Mapping):
        choice_dict = dict(choice)
        action_value = choice_dict.get("action") or choice_dict.get("decision") or choice_dict.get("strategy")
        key_choice = choice_dict.get(
            "key_id",
            choice_dict.get("key_entry", choice_dict.get("key", choice_dict.get("entry"))),
        )
        if action_value is not None:
            action = _normalize_failover_action(action_value)
            if key_choice is not None:
                return action, _runtime_key_entry(runtime, key_choice)
            return action, None
        if key_choice is not None:
            entry = _runtime_key_entry(runtime, key_choice)
            if entry is not None:
                return "retry_key", entry
        entry = _runtime_key_entry(runtime, choice_dict)
        if entry is not None:
            return "retry_key", entry
    return "raise", None


def resolve_api_key_failover(
    plugin_settings: Any,
    *,
    error: Any,
    status_code: int | None = None,
    response_text: str | None = None,
    request_data: dict[str, Any] | None = None,
    provider: str | None = None,
    stream_started: bool = False,
) -> APIKeyFailoverDecision:
    """Resolve provider-error handling for the selected API-key pool.

    The returned decision mutates the request-local plugin settings when it
    selects a retry key, so provider plugins can rebuild their auth headers from
    the same settings namespace.
    """

    runtime = _as_dict(plugin_settings.get("_api_key_pool_runtime", None))
    if not runtime:
        return APIKeyFailoverDecision(False, "raise", reason="no_api_key_pool_runtime")
    failover_config = _as_dict(runtime.get("failover"))
    if not failover_config:
        return APIKeyFailoverDecision(False, "raise", reason="no_failover_policy")
    key_entries = [entry for entry in runtime.get("keys", []) if isinstance(entry, dict)]
    if not key_entries:
        return APIKeyFailoverDecision(False, "raise", reason="empty_api_key_pool")

    max_attempts = _failover_max_attempts(failover_config, len(key_entries))
    attempts = runtime.setdefault("attempts", [])
    if len(attempts) >= max_attempts:
        return APIKeyFailoverDecision(False, "raise", reason="max_attempts_exhausted")

    allow_stream_retry = bool(failover_config.get("allow_stream_retry_after_output", False))
    if stream_started and not allow_stream_retry:
        return APIKeyFailoverDecision(False, "raise", reason="stream_already_started")

    handler = _as_callable(failover_config.get("handler"))
    context = APIKeyFailoverContext(
        pool_id=str(runtime.get("pool_id", "")),
        keys=[entry.copy() for entry in key_entries],
        current_key_id=runtime.get("selected_key_id"),
        attempt_index=len(attempts),
        max_attempts=max_attempts,
        error=error,
        status_code=status_code,
        response_text=response_text,
        request_data=request_data,
        provider=provider,
        stream_started=stream_started,
    )
    if handler is not None:
        raw_choice = _call_handler(handler, context)
    else:
        strategy = _normalize_failover_action(failover_config.get("strategy", "raise"))
        raw_choice = strategy if _status_is_retryable(failover_config, status_code) else "raise"

    action, entry = _parse_failover_choice(raw_choice, runtime)
    if action in {"raise", "stop"}:
        return APIKeyFailoverDecision(False, "raise", reason="handler_or_policy_raised")
    if action in {"try_next"}:
        entry = _next_key_entry(runtime)
    elif action == "retry_same":
        entry = _runtime_key_entry(runtime, runtime.get("selected_key_id"))
    if entry is None:
        return APIKeyFailoverDecision(False, "raise", reason="no_retry_key_available")

    runtime["selected_key_id"] = entry["id"]
    attempts.append({"key_id": entry["id"], "action": action})
    plugin_settings.set("_api_key_pool_runtime", runtime)
    plugin_settings.set("api_key", str(entry["value"]))
    return APIKeyFailoverDecision(
        True,
        action,
        key_id=str(entry["id"]),
        api_key=str(entry["value"]),
    )


def _profile_from_model_pool(
    model_key: str,
    model_pool: dict[str, Any],
    model_profiles: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    model_ref = model_pool.get(model_key)
    if not model_ref:
        return None, {}
    if isinstance(model_ref, dict):
        inline_profile = dict(model_ref)
        return str(inline_profile.get("model") or model_key), inline_profile
    if isinstance(model_ref, str) and model_ref in model_profiles:
        profile = _as_dict(model_profiles.get(model_ref))
        return str(profile.get("model") or model_ref), profile
    return str(model_ref), {}


def _available_model_keys(model_pool: dict[str, Any]) -> str:
    keys = sorted(str(key) for key in model_pool)
    if len(keys) > 10:
        return f"{', '.join(keys[:10])}, ... ({len(keys)} total)"
    return ", ".join(keys)


def _resolve_model_route(model_key: str, settings: "Settings") -> _ResolvedModelRoute:
    model_pool = _as_dict(settings.get("model_pool", {}) or {})
    model_profiles = _as_dict(settings.get("model_profiles", {}) or {})
    if model_pool and model_key not in model_pool:
        available = _available_model_keys(model_pool)
        raise ValueError(
            f"Unknown model_key {model_key!r}; configure it in model_pool or omit model_key "
            f"to use inherited model settings. Available model keys: {available}."
        )

    model_name, profile = _profile_from_model_pool(model_key, model_pool, model_profiles)
    if model_key in model_pool and not model_name:
        raise ValueError(f"model_pool[{model_key!r}] must resolve to a non-empty model or model profile.")

    active_plugin = str(settings.get("plugins.ModelRequester.activate", "OpenAICompatible"))
    provider = str(profile.get("provider") or active_plugin)
    return _ResolvedModelRoute(
        model_key=model_key,
        model_name=model_name,
        profile=profile,
        provider=provider,
    )


def _auth_value_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_resolve_env(value).strip())
    if isinstance(value, Mapping):
        return any(_auth_value_present(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_auth_value_present(item) for item in value)
    return value is not None and value is not False


def _api_key_pool_auth_state(
    pool_id: str,
    api_key_pools: dict[str, Any],
    legacy_key_pool: dict[str, str],
) -> tuple[bool, bool]:
    pool_config = _as_dict(api_key_pools.get(pool_id))
    raw_entries = pool_config.get("keys")
    if raw_entries is None:
        raw_entries = pool_config.get("pool", [])
    if not isinstance(raw_entries, list):
        return False, False
    key_entries: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(raw_entries):
        key_entry = _build_key_entry(
            pool_id=pool_id,
            index=index,
            raw_entry=raw_entry,
            legacy_key_pool=legacy_key_pool,
        )
        if key_entry is not None:
            key_entries.append(key_entry)
    return bool(key_entries), any(_auth_value_present(entry.get("value")) for entry in key_entries)


def _legacy_strategy_has_auth(
    model_name: str | None,
    key_pool: dict[str, str],
    key_pool_strategy: dict[str, dict[str, Any]],
) -> bool:
    if model_name is None:
        return False
    strategy = _as_dict(key_pool_strategy.get(model_name))
    pool_ids = strategy.get("pool", [])
    if not isinstance(pool_ids, list):
        return False
    return any(_auth_value_present(key_pool.get(str(key_id))) for key_id in pool_ids)


def _profile_value(profile: dict[str, Any], key: str, fallback: Any) -> Any:
    value = profile.get(key)
    return fallback if value is None else value


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def resolve_model_profile(model_key: str, settings: "Settings") -> ModelProfileResolution:
    """Return a non-secret model-profile projection without mutating settings.

    When a non-empty ``model_pool`` is configured, *model_key* must be one of
    its aliases. With no model pool, the inherited single-model settings remain
    valid for backward compatibility.
    """
    if not model_key:
        raise ValueError("resolve_model_profile(...) requires a non-empty model_key.")

    route = _resolve_model_route(model_key, settings)
    profile = route.profile
    ns = f"plugins.ModelRequester.{route.provider}"
    api_key_pools = _as_dict(settings.get("api_key_pools", {}) or {})
    key_pool = _as_dict(settings.get("key_pool", {}) or {})
    key_pool_strategy = _as_dict(settings.get("key_pool_strategy", {}) or {})

    auth_present = False
    auth_resolved = False
    api_key_pool = profile.get("api_key_pool")
    if api_key_pool:
        auth_resolved, auth_present = _api_key_pool_auth_state(str(api_key_pool), api_key_pools, key_pool)
    if not auth_resolved:
        raw_profile_key = profile.get("api_key")
        if raw_profile_key is None and isinstance(profile.get("auth"), str):
            raw_profile_key = profile["auth"]
        if raw_profile_key is not None:
            auth_present = _auth_value_present(raw_profile_key)
            auth_resolved = True
        elif _auth_value_present(profile.get("auth")):
            auth_present = True
            auth_resolved = True
    if not auth_resolved:
        auth_present = _legacy_strategy_has_auth(route.model_name, key_pool, key_pool_strategy)
        auth_resolved = auth_present
    if not auth_resolved:
        auth_present = _auth_value_present(settings.get(f"{ns}.api_key", None)) or _auth_value_present(
            settings.get(f"{ns}.auth", None)
        )

    return {
        "model_key": route.model_key,
        "provider": route.provider,
        "model": _optional_str(route.model_name if route.model_name is not None else settings.get(f"{ns}.model", None)),
        "base_url": _optional_str(_profile_value(profile, "base_url", settings.get(f"{ns}.base_url", None))),
        "full_url": _optional_str(_profile_value(profile, "full_url", settings.get(f"{ns}.full_url", None))),
        "auth_present": auth_present,
    }


def resolve_model_pool_settings(model_key: str, settings: "Settings") -> None:
    """Resolve *model_key* into provider, model, profile fields, and API key.

    Reads the current layered ``model_pool``/``model_profiles``/
    ``api_key_pools`` shape, with fallback support for legacy ``key_pool`` and
    ``key_pool_strategy``. Injected values are written into the ModelRequester
    settings namespace that the selected provider already reads.

    This is a no-op when *model_key* is ``None``/empty or no model pool is
    configured. A non-empty pool rejects unknown aliases before any provider
    request can be constructed.
    """
    if not model_key:
        return

    api_key_pools = _as_dict(settings.get("api_key_pools", {}) or {})
    key_pool = cast(dict[str, str], _as_dict(settings.get("key_pool", {}) or {}))
    key_pool_strategy = cast(
        dict[str, dict[str, Any]],
        _as_dict(settings.get("key_pool_strategy", {}) or {}),
    )

    route = _resolve_model_route(model_key, settings)
    model_name = route.model_name
    profile = route.profile
    if model_name is None:
        return

    active_plugin = str(settings.get("plugins.ModelRequester.activate", "OpenAICompatible"))
    provider = route.provider
    if provider != active_plugin:
        settings.set("plugins.ModelRequester.activate", provider)
    ns = f"plugins.ModelRequester.{provider}"
    settings.set(f"{ns}._api_key_pool_runtime", None)

    strategy = key_pool_strategy.get(model_name)
    api_key: str | None = None
    if profile.get("api_key_pool"):
        api_key, api_key_pool_runtime = _resolve_api_key_pool(
            pool_id=str(profile["api_key_pool"]),
            api_key_pools=api_key_pools,
            legacy_key_pool=key_pool,
        )
        if api_key_pool_runtime is not None:
            settings.set(f"{ns}._api_key_pool_runtime", api_key_pool_runtime)
    if api_key is None:
        raw_profile_key = profile.get("api_key")
        if raw_profile_key is None:
            raw_profile_key = profile.get("auth") if isinstance(profile.get("auth"), str) else None
        if raw_profile_key is not None:
            api_key = _resolve_env(str(raw_profile_key))
    if api_key is None and strategy:
        api_key = _resolve_legacy_api_key(
            model_name=model_name,
            key_pool=key_pool,
            key_pool_strategy=key_pool_strategy,
        )

    settings.set(f"{ns}.model", model_name)
    for key, value in profile.items():
        if key in {"provider", "model", "api_key_pool", "api_key"}:
            continue
        if value is not None:
            settings.set(f"{ ns }.{ key }", value)
    if api_key is not None:
        settings.set(f"{ns}.api_key", api_key)


def resolve_role_profile(role: str, settings: "Settings", *, select_credentials: bool = True) -> tuple[str, dict[str, Any]] | None:
    """Resolve an independent use profile through the existing model/key pools.

    Provider namespaces are deliberately not inherited: two uses of the same
    provider must not share credentials or request options accidentally.
    """
    from copy import deepcopy

    from agently.types.settings import ModelUseSettings
    from agently.utils.Settings import Settings

    raw = settings.get(role, None)
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(f"{role} must be a model configuration object.")
    config = ModelUseSettings.model_validate(dict(raw), strict=True).to_dict()
    key = config.pop("model_key", None)
    selected = Settings()
    for name in ("model_pool", "model_profiles", "api_key_pools", "key_pool", "key_pool_strategy"):
        selected.set(name, deepcopy(settings.get(name, {})))
    if key:
        if config:
            raise ValueError(f"{role}.model_key cannot be combined with an inline model profile.")
        pool = selected.get("model_pool", {})
        if not isinstance(pool, Mapping) or key not in pool:
            raise ValueError(f"{role}.model_key {key!r} must exist in model_pool.")
    else:
        if not config.get("provider") or not config.get("model"):
            raise ValueError(f"Configure {role}.provider and {role}.model, or {role}.model_key.")
        key = "__agently_model_use__"
        selected.set("model_pool", {key: config})
    if not select_credentials:
        route = _resolve_model_route(key, selected)
        if not route.model_name:
            raise ValueError(f"{role} did not resolve a valid model profile.")
        return route.provider, {**deepcopy(route.profile), "model": route.model_name}
    resolve_model_pool_settings(key, selected)
    provider = selected.get("plugins.ModelRequester.activate", "OpenAICompatible")
    profile = selected.get(f"plugins.ModelRequester.{provider}", {})
    if not isinstance(provider, str) or not provider.strip() or not isinstance(profile, Mapping) or not profile.get("model"):
        raise ValueError(f"{role} did not resolve a valid model profile.")
    return provider, deepcopy(dict(profile))


def apply_role_profile(request: Any, role: str) -> bool:
    """Select one role on a request-local snapshot; return False when absent."""
    from copy import deepcopy

    selected = resolve_role_profile(role, request.settings)
    if selected is None:
        return False
    provider, profile = selected
    plugin = request.plugin_manager.get_plugin("ModelRequester", provider)
    defaults = {key: deepcopy(value) for key, value in getattr(plugin, "DEFAULT_SETTINGS", {}).items() if key != "$mappings"}
    snapshot = deepcopy(request.settings.get())
    request.settings.parent = None
    request.settings.update(snapshot)
    request.settings.set(f"plugins.ModelRequester.{provider}", {**defaults, **profile})
    request.settings.set("plugins.ModelRequester.activate", provider)
    request._model_key = None
    request._model_role = None
    return True
