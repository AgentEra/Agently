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

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field, StrictBool

from agently.types.config import AgentlyConfigModel

KeyPoolStrategy = Literal["fixed", "random", "round_robin", "least_used"]
KeyPoolFailoverStrategy = Literal["none", "raise", "try_next", "retry_next", "retry_same"]


class APIKeyPoolKey(AgentlyConfigModel):
    id: str | None = None
    value: str | None = None
    api_key: str | None = None
    auth: Any = None
    weight: int | None = None
    tags: dict[str, Any] | None = None


class APIKeyPoolSelectionPolicy(AgentlyConfigModel):
    strategy: KeyPoolStrategy | None = None
    mode: KeyPoolStrategy | None = None
    handler: Callable[..., Any] | None = None


class APIKeyPoolFailoverPolicy(AgentlyConfigModel):
    strategy: KeyPoolFailoverStrategy | None = None
    handler: Callable[..., Any] | None = None
    max_attempts: int | None = None
    retry_status_codes: list[int] | None = None
    status_codes: list[int] | None = None
    allow_stream_retry_after_output: bool | None = None


class APIKeyPoolSettings(AgentlyConfigModel):
    strategy: KeyPoolStrategy | None = None
    mode: KeyPoolStrategy | None = None
    selection: KeyPoolStrategy | APIKeyPoolSelectionPolicy | Callable[..., Any] | dict[str, Any] | None = None
    failover: KeyPoolFailoverStrategy | APIKeyPoolFailoverPolicy | Callable[..., Any] | dict[str, Any] | None = None
    key_entries: list[str | APIKeyPoolKey | dict[str, Any]] | None = Field(
        default=None,
        alias="keys",
        serialization_alias="keys",
    )
    pool: list[str | APIKeyPoolKey | dict[str, Any]] | None = None


class ModelProfileSettings(AgentlyConfigModel):
    provider: str | None = None
    model: str | None = None
    api_key_pool: str | None = None
    api_key: str | None = None
    auth: Any = None
    base_url: str | None = None
    full_url: str | None = None
    request_options: dict[str, Any] | None = None
    client_options: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    timeout: dict[str, Any] | None = None
    stream_idle_timeout: float | None = None


class ModelUseSettings(ModelProfileSettings):
    """Independent model use: an inline profile or a model-pool reference."""

    __secret_fields__ = {"api_key", "auth"}
    model_key: str | None = None
    vision: StrictBool | None = None


class SystemOneSettings(ModelProfileSettings):
    """Dedicated model profile for SystemOne output-template stages."""

    __settings_namespace__ = "system_one"
    __secret_fields__ = {"api_key", "auth"}
    enabled: StrictBool | None = None
    model_key: str | None = None
    batch_size: int = Field(default=64, ge=1, strict=True)
