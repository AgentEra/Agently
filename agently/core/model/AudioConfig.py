"""Resolve role settings into the existing independent audio capability."""

from __future__ import annotations

from typing import Any, Literal, cast

from agently.types.data.audio import AudioConnection, SpeechOptions, TranscriptionOptions
from agently.types.plugins.AudioModelRequester import AudioCapability, AudioModelRequester
from agently.utils.ModelPool import resolve_role_profile

from .AudioModelRequest import AudioModelRequest


def resolve_audio(owner: Any, role: Literal["stt", "tts"]) -> AudioCapability:
    execution = hasattr(owner, "require_agent_capability")
    require = owner.require_agent_capability if execution else owner.require_capability
    try:
        return cast(AudioCapability, require("audio"))
    except RuntimeError:
        pass
    if execution and role in owner._bound_agent_capabilities:
        return cast(AudioCapability, owner._bound_agent_capabilities[role])
    settings = owner.request.settings if execution else owner.settings
    selected = resolve_role_profile(role, settings)
    if selected is None:
        raise RuntimeError(f"Audio capability is not bound. Configure {role}.provider/model or bind an AudioCapability with use_audio().")
    provider, profile = selected
    driver = cast(type[AudioModelRequester], owner.plugin_manager.get_plugin("AudioModelRequester", provider))
    if profile.get("stream_idle_timeout") is not None or (profile.get("auth") is not None and not isinstance(profile["auth"], str)):
        raise ValueError("Audio profiles support api_key/string auth and timeout.read; custom auth/stream_idle_timeout require an explicit audio driver.")
    if profile.get("full_url"):
        raise ValueError("Audio profiles use base_url, not full_url.")
    if (profile.get("_api_key_pool_runtime") or {}).get("failover"):
        raise ValueError("Audio profiles do not support API-key failover; audio is never implicitly replayed.")
    timeout = profile.get("timeout", {}) or {}
    unsupported_timeout = set(timeout) - {"read"}
    if unsupported_timeout:
        raise ValueError("Audio profile timeout accepts read (seconds); configure transport-specific timeouts through the audio driver.")
    connection = AudioConnection(
        base_url=profile.get("base_url") or "https://api.openai.com/v1",
        api_key=profile.get("api_key") or "", timeout=timeout.get("read", 120.0),
        headers=profile.get("headers") or {}, client_options=profile.get("client_options") or {},
    )
    options = dict(profile.get("request_options") or {})
    voice = options.pop("voice", None) if role == "tts" else None
    fields = {"response_format", "speed", "language", "instructions"} if role == "tts" else {"language", "prompt"}
    explicit = {key: options.pop(key) for key in list(options) if key in fields}
    speech = SpeechOptions(**explicit, extra=options) if role == "tts" else None
    transcription = TranscriptionOptions(**explicit, extra=options) if role == "stt" else None
    capability = AudioModelRequest(driver(connection), **{f"{role}_model": profile["model"]}, voice=voice,
                                   speech_options=speech, transcription_options=transcription)
    if role not in capability.supported_operations:
        raise ValueError(f"Audio provider {provider!r} does not support {role}.")
    if execution:
        owner._bound_agent_capabilities[role] = capability
    return capability
