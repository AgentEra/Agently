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


class OpenAIResponsesCompatibleSettings(TypedDict, total=False):
    model: str
    timeout_mode: Literal["http", "first_token"]
    stream_idle_timeout: float | None
    request_retry: dict[str, int | bool] | bool
    client_options: dict[str, SerializableValue]
    headers: dict[str, SerializableValue]
    proxy: str
    request_options: dict[str, SerializableValue]
    base_url: str
    full_url: str
    auth: SerializableValue
    stream: bool
    rich_content: bool
    strict_role_orders: bool
