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

from typing import Literal
from typing_extensions import TypedDict

from agently.types.data import SerializableValue


class ContentMapping(TypedDict):
    id: str | None
    role: str | None
    reasoning: str | None
    delta: str | None
    tool_calls: str | None
    done: str | None
    usage: str | None
    finish_reason: str | None
    extra_delta: dict[str, str] | None
    extra_done: dict[str, str] | None


class ModelSettingsMapping(TypedDict):
    chat: str
    completions: str
    embeddings: str
    rerank: str | None


class ModelRequesterSettings(TypedDict, total=False):
    model: str
    model_type: Literal["chat", "completions", "embeddings", "rerank"]
    timeout_mode: Literal["http", "first_token"]
    stream_idle_timeout: float | None
    client_options: dict[str, SerializableValue]
    headers: dict[str, SerializableValue]
    proxy: str
    request_options: dict[str, SerializableValue]
    base_url: str
    path_mapping: ModelSettingsMapping
    default_model: ModelSettingsMapping
    auth: SerializableValue
    stream: bool
    rich_content: bool
    strict_role_orders: bool
    content_mapping: ContentMapping
    content_mapping_style: Literal["dot", "slash"]
