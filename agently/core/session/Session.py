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

from agently_stage import default_stage_call_bridge


from pathlib import Path
from uuid import uuid4
from warnings import warn

from json import JSONDecodeError
from typing import Callable, Sequence, Any, TYPE_CHECKING, cast

import json
import json5
import yaml

from agently.types.data import ChatMessage, ChatMessageDict
from agently.utils import DeprecationWarnings, Settings, SettingsNamespace, DataLocator

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.core.model import ModelRequestResult
    from agently.types.data import SerializableValue
    from agently.types.plugins import (
        AnalysisHandler,
        ExecutionHandler,
        ResizeHandler,
        StandardAnalysisHandler,
        StandardExecutionHandler,
        StandardResizeHandler,
    )
    from agently.utils import Settings


class Session:
    def __init__(
        self,
        id: str | None = None,
        *,
        auto_resize: bool = True,
        settings: dict[str, Any] | Settings = {},
        plugin_manager: Any = None,
        memory_store: Any = None,
        memory_store_provider: Callable[[], Any] | None = None,
    ):
        self.id = id if id is not None else uuid4().hex
        self._auto_resize = auto_resize
        self.plugin_manager = plugin_manager
        self._memory_store = memory_store
        self._memory_store_provider = memory_store_provider
        if isinstance(settings, dict):
            from agently.base import settings as global_settings

            self.settings = Settings(settings, parent=global_settings)
        else:
            self.settings = Settings(parent=settings)
        self.session_settings = SettingsNamespace(self.settings, "session")
        self.session_settings.setdefault("max_length", None)
        self._analysis_handler: "StandardAnalysisHandler" = self._default_analysis_handler
        self._resize_handlers: "dict[str, StandardResizeHandler]" = {
            "simple_cut": self._simple_cut_resize_handler,
        }
        self._execution_handlers: "dict[str, StandardExecutionHandler]" = self._resize_handlers
        self._full_context: list[ChatMessage] = []
        self._context_window: list[ChatMessage] = []
        self._memo = None
        self._memory_plugin: Any = None
        self._memory_mode: str | None = None

        self.reset_chat_history = default_stage_call_bridge.as_sync(self.async_reset_chat_history)
        self.set_chat_history = default_stage_call_bridge.as_sync(self.async_set_chat_history)
        self.clean_context_window = default_stage_call_bridge.as_sync(self.async_clean_context_window)
        self.clean_window_context = self.clean_context_window
        self.add_chat_history = default_stage_call_bridge.as_sync(self.async_add_chat_history)
        self.analyze_context = default_stage_call_bridge.as_sync(self.async_analyze_context)
        self.run_resize_strategy = default_stage_call_bridge.as_sync(self.async_run_resize_strategy)
        self.execute_strategy = default_stage_call_bridge.as_sync(self.async_execute_strategy)
        self.resize = default_stage_call_bridge.as_sync(self.async_resize)
        self.to_json = self.get_json_session
        self.to_yaml = self.get_yaml_session
        self.load_json = self.load_json_session
        self.load_yaml = self.load_yaml_session

    @property
    def memory(self):
        return self._memory_plugin

    @property
    def memory_mode(self) -> str | None:
        return self._memory_mode

    def _resolve_memory_store(self):
        if self._memory_store is not None:
            return self._memory_store
        if callable(self._memory_store_provider):
            memory_store = self._memory_store_provider()
            if memory_store is not None:
                self._memory_store = memory_store
        return self._memory_store

    def bind_memory_store(self, memory_store: Any):
        self._memory_store = memory_store
        if self._memory_plugin is not None and hasattr(self._memory_plugin, "bind_memory_store"):
            self._memory_plugin.bind_memory_store(memory_store)
        return self

    def use_memory(self, *, mode: str = "AgentlyMemory", memory_store: Any = None):
        plugin_manager = self.plugin_manager
        if plugin_manager is None:
            from agently.base import plugin_manager as global_plugin_manager

            plugin_manager = global_plugin_manager
            self.plugin_manager = plugin_manager
        if memory_store is not None:
            self._memory_store = memory_store
        else:
            memory_store = self._resolve_memory_store()
        try:
            session_memory_class = plugin_manager.get_plugin("SessionMemory", mode)
        except Exception as exc:
            raise ValueError(f"SessionMemory mode '{ mode }' is not registered.") from exc
        self._memory_plugin = cast(Any, session_memory_class)(
            session=self,
            memory_store=memory_store,
            plugin_manager=plugin_manager,
            settings=self.settings,
        )
        self._memory_mode = mode
        return self

    def create_memory_context_source(self, settings: "Settings | None" = None):
        if self._memory_plugin is None:
            return None
        if getattr(self._memory_plugin, "memory_store", None) is None:
            memory_store = self._resolve_memory_store()
            if memory_store is not None:
                self._memory_plugin.bind_memory_store(memory_store)
        factory = getattr(self._memory_plugin, "create_context_source", None)
        if not callable(factory):
            raise TypeError(
                "The active SessionMemory plugin must provide create_context_source(...)."
            )
        return factory(
            session=self,
            settings=settings or self.settings,
        )

    async def async_prepare_memory(self, prompt: "Prompt", settings: "Settings"):
        if self._memory_plugin is None:
            return {}
        if getattr(self._memory_plugin, "memory_store", None) is None:
            memory_store = self._resolve_memory_store()
            if memory_store is not None:
                self._memory_plugin.bind_memory_store(memory_store)
        return await self._memory_plugin.prepare_request(
            prompt=prompt,
            session=self,
            settings=settings,
        )

    async def async_after_memory_turn(
        self,
        *,
        user_content: str | None,
        assistant_content: str | None,
        result: "ModelRequestResult",
        settings: "Settings",
    ):
        if self._memory_plugin is None:
            return {}
        if getattr(self._memory_plugin, "memory_store", None) is None:
            memory_store = self._resolve_memory_store()
            if memory_store is not None:
                self._memory_plugin.bind_memory_store(memory_store)
        return await self._memory_plugin.after_turn(
            session=self,
            user_content=user_content,
            assistant_content=assistant_content,
            result=result,
            settings=settings,
        )

    async def _default_analysis_handler(
        self,
        full_context: Sequence[ChatMessage],
        context_window: Sequence[ChatMessage],
        memo: "SerializableValue",
        session_settings: SettingsNamespace,
    ):
        max_length = session_settings.get("max_length", None)
        context_window_length = self._calculate_context_length(context_window)
        if isinstance(max_length, int) and context_window_length > max_length:
            return "simple_cut"
        return None

    async def _simple_cut_resize_handler(
        self,
        full_context: Sequence[ChatMessage],
        context_window: Sequence[ChatMessage],
        memo: "SerializableValue",
        session_settings: SettingsNamespace,
    ):
        max_length = session_settings.get("max_length", None)
        if isinstance(max_length, int):
            new_context_window: list[ChatMessage] = []
            total_length = 0
            for message in reversed(context_window):
                message_length = len(str(message.model_dump()))
                if total_length + message_length > max_length:
                    break
                new_context_window.append(message)
                total_length += message_length
            new_context_window.reverse()

            if len(new_context_window) == 0 and context_window:
                new_content = str(context_window[-1].content)
                new_content = new_content[len(new_content) - max_length :]
                return (
                    None,
                    [
                        ChatMessage(
                            role=context_window[-1].role,
                            content=new_content,
                        )
                    ],
                    None,
                )

            if self._calculate_context_length(new_context_window) <= max_length:
                return (
                    None,
                    self._to_standard_chat_messages(new_context_window),
                    None,
                )
        return None, None, None

    def _calculate_context_length(self, context_window: Sequence[ChatMessage]):
        length = 0
        for message in context_window:
            length += len(str(message.model_dump()))
        return length

    def register_analysis_handler(self, analysis_handler: "AnalysisHandler | None"):
        if analysis_handler is None:
            self._analysis_handler = self._default_analysis_handler
        else:
            self._analysis_handler = default_stage_call_bridge.as_async(analysis_handler)
        return self

    def register_resize_handler(self, strategy_name: str, resize_handler: "ResizeHandler"):
        self._resize_handlers[strategy_name] = default_stage_call_bridge.as_async(resize_handler)
        return self

    def register_execution_handlers(self, strategy_name: str, execution_handler: "ExecutionHandler"):
        DeprecationWarnings.warn_deprecated_once(
            "Session.register_execution_handlers",
            "Session.register_execution_handlers() is deprecated and will be removed in a future version; "
            "use Session.register_resize_handler() instead.",
            stacklevel=2,
        )
        return self.register_resize_handler(strategy_name, execution_handler)

    def _to_standard_chat_messages(self, chat_messages: Sequence[ChatMessage | ChatMessageDict]):
        return [
            (
                ChatMessage(
                    role=message["role"],
                    content=message["content"],
                )
                if isinstance(message, dict)
                else message
            )
            for message in chat_messages
        ]

    async def async_reset_chat_history(
        self,
    ):
        self._full_context = []
        self._context_window = []
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_clean_context_window(self):
        self._context_window = []
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_clean_window_context(self):
        return await self.async_clean_context_window()

    async def async_set_chat_history(
        self,
        chat_history: Sequence[ChatMessage | ChatMessageDict] | ChatMessage | ChatMessageDict,
    ):
        if isinstance(chat_history, Sequence):
            messages = self._to_standard_chat_messages(chat_history)
            self._full_context = messages.copy()
            self._context_window = messages.copy()
        else:
            if isinstance(chat_history, dict):
                chat_history = ChatMessage(
                    role=chat_history["role"],
                    content=chat_history["content"],
                )
            self._full_context = [chat_history]
            self._context_window = [chat_history]
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_add_chat_history(
        self,
        chat_history: Sequence[ChatMessage | ChatMessageDict] | ChatMessage | ChatMessageDict,
    ):
        if isinstance(chat_history, Sequence):
            messages = self._to_standard_chat_messages(chat_history)
            self._full_context.extend(messages)
            self._context_window.extend(messages)
        else:
            if isinstance(chat_history, dict):
                chat_history = ChatMessage(
                    role=chat_history["role"],
                    content=chat_history["content"],
                )
            self._full_context.append(chat_history)
            self._context_window.append(chat_history)
        if self._auto_resize:
            await self.async_resize()
        return self

    async def async_analyze_context(self):
        return await self._analysis_handler(
            self._full_context,
            self._context_window,
            self._memo,
            self.session_settings,
        )

    async def async_run_resize_strategy(self, strategy_name: str):
        if strategy_name in self._resize_handlers:
            new_full_context, new_context_window, new_memo = await self._resize_handlers[strategy_name](
                self._full_context,
                self._context_window,
                self._memo,
                self.session_settings,
            )
            if new_full_context is not None:
                self._full_context = self._to_standard_chat_messages(new_full_context)
            if new_context_window is not None:
                self._context_window = self._to_standard_chat_messages(new_context_window)
            if new_memo is not None:
                self._memo = new_memo
        else:
            warn(f"Can not find strategy '{ strategy_name }' in resize handlers dictionary in Session <{ self.id }>.")

    async def async_execute_strategy(self, strategy_name: str):
        DeprecationWarnings.warn_deprecated_once(
            "Session.async_execute_strategy",
            "Session.async_execute_strategy() is deprecated and will be removed in a future version; "
            "use Session.async_run_resize_strategy() instead.",
            stacklevel=2,
        )
        await self.async_run_resize_strategy(strategy_name)

    async def async_resize(self):
        strategy_name = await self.async_analyze_context()
        if strategy_name is not None:
            await self.async_run_resize_strategy(strategy_name)

    @property
    def full_context(self):
        return self._full_context.copy()

    @property
    def context_window(self):
        return self._context_window.copy()

    @property
    def memo(self):
        try:
            return self._memo.copy()  # type: ignore
        except:
            return self._memo

    @staticmethod
    def _normalize_session_keys(keys: Any):
        if keys is None:
            return None
        if isinstance(keys, str):
            return [keys]
        if isinstance(keys, Sequence):
            return [str(key) for key in keys if key is not None]
        return []

    @staticmethod
    def _extract_by_path(data: Any, key: str):
        sentinel = object()
        if isinstance(data, dict):
            if key in data:
                return True, data[key]
            style = "slash" if "/" in key else "dot"
            value = DataLocator.locate_path_in_dict(data, key, style=style, default=sentinel)
            if value is not sentinel:
                return True, value
        return False, None

    @staticmethod
    def _extract_input_value(
        prompt_data: dict[str, Any],
        key: str,
        agent_prompt_data: dict[str, Any],
    ):
        key = key.strip()
        if key == "":
            return False, None

        if key == ".execution":
            return True, prompt_data

        if key.startswith(".execution."):
            return Session._extract_by_path(prompt_data, key[len(".execution.") :])

        if key == ".agent":
            return True, agent_prompt_data

        if key.startswith(".agent."):
            return Session._extract_by_path(
                agent_prompt_data,
                key[len(".agent.") :],
            )

        found, value = Session._extract_by_path(prompt_data, key)
        if found:
            return found, value

        input_data = prompt_data.get("input")
        if isinstance(input_data, dict):
            if key.startswith("input."):
                return Session._extract_by_path(input_data, key[len("input.") :])
            return Session._extract_by_path(input_data, key)

        return False, None

    @staticmethod
    def _format_session_value(value: Any):
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)) or value is None:
            return str(value)
        try:
            return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).rstrip("\n")
        except Exception:
            return str(value)

    @staticmethod
    def _format_session_keyed_content(items: list[tuple[str, Any]]):
        lines: list[str] = []
        for key, value in items:
            lines.append(f"[{ key }]:")
            lines.append(Session._format_session_value(value))
        return "\n".join(lines).strip()

    @staticmethod
    def apply_request_prefix(
        prompt: "Prompt",
        activated_session: "Session | None",
    ):
        if activated_session is None:
            return
        if "chat_history" in prompt:
            del prompt["chat_history"]
        prompt.set("chat_history", activated_session.context_window)
        if activated_session.memo is not None:
            prompt.set("CHAT SESSION MEMO", activated_session.memo)

    @staticmethod
    async def resolve_finally_contents(
        result: "ModelRequestResult",
        settings: "Settings",
        agent_prompt: "Prompt",
    ) -> tuple[str | None, str | None]:
        input_keys = Session._normalize_session_keys(settings.get("session.input_keys", None))
        reply_keys = Session._normalize_session_keys(settings.get("session.reply_keys", None))

        user_content: str | None = None
        if input_keys is None:
            user_content = str(result.prompt.to_text())
        else:
            prompt_data = result.prompt.to_serializable_prompt_data()
            agent_prompt_data = agent_prompt.to_serializable_prompt_data()
            user_items: list[tuple[str, Any]] = []
            for input_key in input_keys:
                found, value = Session._extract_input_value(dict(prompt_data), input_key, dict(agent_prompt_data))
                if found:
                    user_items.append((input_key, value))
            if user_items:
                user_content = Session._format_session_keyed_content(user_items)

        assistant_content: str | None = None
        result_data = await result.async_get_data()
        if reply_keys is None:
            assistant_content = Session._format_session_value(result_data)
        else:
            assistant_items: list[tuple[str, Any]] = []
            for reply_key in reply_keys:
                found, value = Session._extract_by_path(result_data, reply_key)
                if found:
                    assistant_items.append((reply_key, value))
            if assistant_items:
                assistant_content = Session._format_session_keyed_content(assistant_items)

        return user_content, assistant_content

    def _to_serializable_chat_messages(
        self,
        chat_messages: Sequence[ChatMessage | ChatMessageDict],
    ):
        return [
            message.model_dump() if isinstance(message, ChatMessage) else dict(message) for message in chat_messages
        ]

    def to_serializable_session_data(self):
        return {
            "id": self.id,
            "auto_resize": self._auto_resize,
            "full_context": self._to_serializable_chat_messages(self._full_context),
            "context_window": self._to_serializable_chat_messages(self._context_window),
            "memo": self._memo,
            "session_settings": self.session_settings.data,
        }

    def get_json_session(self):
        return json.dumps(
            self.to_serializable_session_data(),
            indent=2,
            ensure_ascii=False,
        )

    def get_yaml_session(self):
        return yaml.safe_dump(
            self.to_serializable_session_data(),
            indent=2,
            allow_unicode=True,
            sort_keys=False,
        )

    def _load_session_data(
        self,
        session_data: dict[str, Any],
    ):
        if "id" in session_data:
            self.id = str(session_data["id"])

        if "auto_resize" in session_data:
            self._auto_resize = bool(session_data["auto_resize"])

        full_context_data = session_data.get("full_context", [])
        if isinstance(full_context_data, (str, bytes)) or not isinstance(full_context_data, Sequence):
            raise TypeError("Cannot load Session data, expect key 'full_context' as a sequence of chat messages.")

        context_window_data = session_data.get("context_window", session_data.get("window_context", full_context_data))
        if isinstance(context_window_data, (str, bytes)) or not isinstance(context_window_data, Sequence):
            raise TypeError("Cannot load Session data, expect key 'context_window' as a sequence of chat messages.")

        self._full_context = self._to_standard_chat_messages(full_context_data)
        self._context_window = self._to_standard_chat_messages(context_window_data)

        if "memo" in session_data:
            self._memo = session_data["memo"]

        session_settings_data = session_data.get("session_settings", None)
        if session_settings_data is not None:
            if not isinstance(session_settings_data, dict):
                raise TypeError("Cannot load Session data, expect key 'session_settings' as a dictionary.")
            self.session_settings.update(session_settings_data)

        return self

    def load_yaml_session(
        self,
        path_or_content: str | Path,
        *,
        session_key_path: str | None = None,
        encoding: str | None = "utf-8",
    ):
        path = Path(path_or_content)
        is_yaml_file = False
        try:
            is_yaml_file = path.exists() and path.is_file()
        except (OSError, ValueError):
            is_yaml_file = False

        if is_yaml_file:
            try:
                with path.open("r", encoding=encoding) as file:
                    session_data = yaml.safe_load(file)
            except yaml.YAMLError as e:
                raise ValueError(f"Cannot load YAML file '{ path_or_content }'.\nError: { e }")
        else:
            try:
                session_data = yaml.safe_load(str(path_or_content))
            except yaml.YAMLError as e:
                raise ValueError(f"Cannot load YAML content or file path not existed.\nError: { e }")

        if not isinstance(session_data, dict):
            raise TypeError(f"Cannot load YAML Session data, expect dictionary data but got: { session_data }")

        if session_key_path is not None:
            session_data = DataLocator.locate_path_in_dict(session_data, session_key_path)

        if not isinstance(session_data, dict):
            raise TypeError(
                f"Cannot load YAML Session data, expect Session data{ ' from [' + session_key_path + '] ' if session_key_path is not None else ' ' }as dictionary but got: { session_data }"
            )

        return self._load_session_data(session_data)

    def load_json_session(
        self,
        path_or_content: str | Path,
        *,
        session_key_path: str | None = None,
        encoding: str | None = "utf-8",
    ):
        path = Path(path_or_content)
        is_json_file = False
        try:
            is_json_file = path.exists() and path.is_file()
        except (OSError, ValueError):
            is_json_file = False

        if is_json_file:
            try:
                with path.open("r", encoding=encoding) as file:
                    session_data = json5.load(file)
            except (JSONDecodeError, ValueError) as e:
                raise ValueError(f"Cannot load JSON file '{ path_or_content }'.\nError: { e }")
        else:
            try:
                session_data = json5.loads(str(path_or_content))
            except (JSONDecodeError, ValueError) as e:
                raise ValueError(f"Cannot load JSON content or file path not existed.\nError: { e }")

        if not isinstance(session_data, dict):
            raise TypeError(f"Cannot load JSON Session data, expect dictionary data but got: { session_data }")

        if session_key_path is not None:
            session_data = DataLocator.locate_path_in_dict(session_data, session_key_path)

        if not isinstance(session_data, dict):
            raise TypeError(
                f"Cannot load JSON Session data, expect Session data{ ' from [' + session_key_path + '] ' if session_key_path is not None else ' ' }as dictionary but got: { session_data }"
            )

        return self._load_session_data(session_data)
