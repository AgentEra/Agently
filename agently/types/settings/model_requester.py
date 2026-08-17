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

from typing import Any, Literal

from pydantic import Field

from agently.types.config import AgentlyConfigModel


class _HTTPTimeoutSettings(AgentlyConfigModel):
    connect: float | None = None
    read: float | None = None
    write: float | None = None
    pool: float | None = None


class _RequestRetrySettings(AgentlyConfigModel):
    max_attempts: int | None = None
    after_output: bool | None = None


class _OpenAIContentMapping(AgentlyConfigModel):
    id: str | None = None
    role: str | None = None
    reasoning: str | None = None
    delta: str | None = None
    tool_calls: str | None = None
    done: str | None = None
    usage: str | None = None
    finish_reason: str | None = None
    extra_delta: dict[str, str] | None = None
    extra_done: dict[str, str] | None = None


class _ModelPathMapping(AgentlyConfigModel):
    chat: str | None = None
    completions: str | None = None
    embeddings: str | None = None


class OpenAICompatibleSettings(AgentlyConfigModel):
    __settings_namespace__ = "plugins.ModelRequester.OpenAICompatible"
    __secret_fields__ = {"api_key", "auth"}

    model: str | None = None
    model_type: Literal["chat", "completions", "embeddings"] | None = None
    timeout_mode: Literal["http", "first_token"] | None = None
    stream_idle_timeout: float | None = None
    request_retry: _RequestRetrySettings | dict[str, int | bool | None] | bool | None = None
    client_options: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    proxy: str | None = None
    request_options: dict[str, Any] | None = None
    base_url: str | None = None
    full_url: str | None = None
    path_mapping: _ModelPathMapping | dict[str, str] | None = None
    default_model: _ModelPathMapping | dict[str, str] | None = None
    auth: Any = None
    api_key: str | None = Field(default=None, description="Compatibility alias for auth.api_key.")
    stream: bool | None = None
    rich_content: bool | None = None
    strict_role_orders: bool | None = None
    content_mapping: _OpenAIContentMapping | dict[str, Any] | None = None
    content_mapping_style: Literal["dot", "slash"] | None = None
    yield_extra_content_separately: bool | None = None
    timeout: _HTTPTimeoutSettings | dict[str, float | None] | None = None


class OpenAIResponsesCompatibleSettings(AgentlyConfigModel):
    __settings_namespace__ = "plugins.ModelRequester.OpenAIResponsesCompatible"
    __secret_fields__ = {"api_key", "auth"}

    model: str | None = None
    default_model: str | None = None
    timeout_mode: Literal["http", "first_token"] | None = None
    stream_idle_timeout: float | None = None
    request_retry: _RequestRetrySettings | dict[str, int | bool | None] | bool | None = None
    client_options: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    proxy: str | None = None
    request_options: dict[str, Any] | None = None
    base_url: str | None = None
    full_url: str | None = None
    auth: Any = None
    api_key: str | None = Field(default=None, description="Compatibility alias for auth.api_key.")
    stream: bool | None = None
    rich_content: bool | None = None
    strict_role_orders: bool | None = None
    timeout: _HTTPTimeoutSettings | dict[str, float | None] | None = None


class AnthropicCompatibleSettings(AgentlyConfigModel):
    __settings_namespace__ = "plugins.ModelRequester.AnthropicCompatible"
    __secret_fields__ = {"api_key", "auth"}

    model: str | None = None
    default_model: str | None = None
    timeout_mode: Literal["http", "first_token"] | None = None
    stream_idle_timeout: float | None = None
    request_retry: _RequestRetrySettings | dict[str, int | bool | None] | bool | None = None
    client_options: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    proxy: str | None = None
    request_options: dict[str, Any] | None = None
    base_url: str | None = None
    full_url: str | None = None
    auth: Any = None
    api_key: str | None = Field(default=None, description="Compatibility alias for auth.api_key.")
    stream: bool | None = None
    rich_content: bool | None = None
    strict_role_orders: bool | None = None
    anthropic_version: str | None = None
    anthropic_beta: str | list[str] | None = None
    max_tokens: int | None = None
    timeout: _HTTPTimeoutSettings | dict[str, float | None] | None = None
