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

from typing import TYPE_CHECKING

from httpx import AsyncClient

from agently.types.plugins import ModelRequester
from agently.types.settings import AnthropicCompatibleSettings as TypedAnthropicCompatibleSettings
from agently.utils import SettingsNamespace

from .modules.credential import AnthropicCompatibleCredentialMixin
from .modules.handlers import AnthropicCompatibleHandlersMixin
from .modules.request_builder import AnthropicCompatibleRequestBuilderMixin
from .modules.response_adapter import AnthropicCompatibleResponseAdapterMixin
from .modules.transport import AnthropicCompatibleTransportMixin
from .modules.types import AnthropicCompatibleSettings

if TYPE_CHECKING:
    from agently.core.model.Prompt import Prompt
    from agently.utils import Settings


class AnthropicCompatible(
    AnthropicCompatibleHandlersMixin,
    AnthropicCompatibleRequestBuilderMixin,
    AnthropicCompatibleTransportMixin,
    AnthropicCompatibleCredentialMixin,
    AnthropicCompatibleResponseAdapterMixin,
    ModelRequester,
):
    name = "AnthropicCompatible"
    SETTINGS_SCHEMAS = {
        "plugins.ModelRequester.AnthropicCompatible": TypedAnthropicCompatibleSettings,
    }

    DEFAULT_SETTINGS = {
        "$mappings": {
            "path_mappings": {
                "AnthropicCompatible": "plugins.ModelRequester.AnthropicCompatible",
                "Anthropic": "plugins.ModelRequester.AnthropicCompatible",
                "Claude": "plugins.ModelRequester.AnthropicCompatible",
            },
        },
        "model": None,
        "default_model": "claude-sonnet-4-20250514",
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
        "base_url": "https://api.anthropic.com/v1",
        "full_url": None,
        "auth": None,
        "stream": True,
        "rich_content": True,
        "strict_role_orders": False,
        "anthropic_version": "2023-06-01",
        "anthropic_beta": None,
        "max_tokens": 8192,
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

        if self.prompt["attachment"]:
            self.plugin_settings["rich_content"] = True

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass
