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

from typing import TYPE_CHECKING, cast

from httpx import AsyncClient as AsyncClient

from agently.types.plugins import ModelRequester
from agently.types.settings import OpenAICompatibleSettings
from agently.utils import SettingsNamespace

from .modules.credential import OpenAICompatibleCredentialMixin
from .modules.handlers import OpenAICompatibleHandlersMixin
from .modules.request_builder import OpenAICompatibleRequestBuilderMixin
from .modules.response_adapter import OpenAICompatibleResponseAdapterMixin
from .modules.transport import OpenAICompatibleTransportMixin

if TYPE_CHECKING:
    from agently.core.model.Prompt import Prompt
    from agently.utils import Settings


class OpenAICompatible(
    OpenAICompatibleHandlersMixin,
    OpenAICompatibleRequestBuilderMixin,
    OpenAICompatibleTransportMixin,
    OpenAICompatibleCredentialMixin,
    OpenAICompatibleResponseAdapterMixin,
    ModelRequester,
):
    name = "OpenAICompatible"
    SETTINGS_SCHEMAS = {
        "plugins.ModelRequester.OpenAICompatible": OpenAICompatibleSettings,
    }

    DEFAULT_SETTINGS = {
        "$mappings": {
            "path_mappings": {
                "OpenAICompatible": "plugins.ModelRequester.OpenAICompatible",
                "OpenAI": "plugins.ModelRequester.OpenAICompatible",
                "OAIClient": "plugins.ModelRequester.OpenAICompatible",
            },
        },
        "model_type": "chat",
        "model": None,
        "default_model": {
            "chat": "gpt-4.1",
            "completions": "gpt-3.5-turbo-instruct",
            "embeddings": "text-embedding-ada-002",
            # Rerank has no vendor-neutral default model id; it must be configured.
            "rerank": None,
        },
        "timeout_mode": "first_token",
        "stream_idle_timeout": None,
        "request_retry": {
            "max_attempts": 2,
            "after_output": True,
        },
        "client_options": {},
        "headers": {},
        "proxy": None,
        "request_options": {},
        "base_url": "https://api.openai.com/v1",
        "full_url": None,
        "path_mapping": {
            "chat": "/chat/completions",
            "completions": "/completions",
            "embeddings": "/embeddings",
            "rerank": "/rerank",
        },
        "auth": None,
        "stream": True,
        "rich_content": False,
        "strict_role_orders": True,
        "content_mapping": {
            "id": "id",
            "role": "choices[0].delta.role",
            "reasoning": "choices[0].delta.reasoning_content",
            "delta": "choices[0].delta.content",
            "tool_calls": "choices[0].delta.tool_calls",
            "done": None,
            "usage": "usage",
            "finish_reason": "choices[0].finish_reason",
            "extra_delta": {
                "function_call": "choices[0].delta.function_call",
            },
            "extra_done": None,
        },
        "yield_extra_content_separately": True,
        "content_mapping_style": "dot",
        "timeout": {
            "connect": 30.0,
            "read": 600.0,
            "write": 30.0,
            "pool": 30.0,
        },
    }

    def __init__(
        self,
        prompt: "Prompt",
        settings: "Settings",
    ):
        self.prompt = prompt
        self.settings = settings
        self.plugin_settings = SettingsNamespace(self.settings, f"plugins.ModelRequester.{ self.name }")
        self.model_type = cast(str, self.plugin_settings.get("model_type"))

        # check if has attachment prompt
        if self.prompt["attachment"]:
            self.plugin_settings["rich_content"] = True

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass
