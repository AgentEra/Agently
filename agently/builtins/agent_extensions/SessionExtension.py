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

from uuid import uuid4

from typing import Any, TYPE_CHECKING, Sequence
from typing_extensions import Self

from agently.core import BaseAgent, Session
from agently.core.runtime.RuntimeContext import (
    get_current_agent_execution_run_context,
    get_current_request_run_context,
)

if TYPE_CHECKING:
    from agently.core import Prompt
    from agently.core.model import ModelRequestResult
    from agently.types.data import ChatMessage, ChatMessageDict
    from agently.utils import Settings
    from agently.types.plugins import SessionAnalysisHandler, SessionResizeHandler


class SessionExtension(BaseAgent):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sessions: dict[str, Session] = {}
        self.activated_session: Session | None = None
        self.settings.setdefault("session.input_keys", None, inherit=True)
        self.settings.setdefault("session.reply_keys", None, inherit=True)

        self.__session_analysis_handler: "SessionAnalysisHandler | None" = None
        self.__session_resize_handlers: dict[str, "SessionResizeHandler"] = {}

        self.extension_handlers.append("request_prefixes", self._session_request_prefix)
        self.extension_handlers.append("finally", self._session_finally)

    def register_session_analysis_handler(self, handler: "SessionAnalysisHandler") -> Self:
        """
        Register analysis handler for session resize planning.

        Signature:
            `(full_context, context_window, memo, session_settings) -> str | None`.
        """
        self.__session_analysis_handler = handler
        for session in self.sessions.values():
            session.register_analysis_handler(handler)
        return self

    def register_session_resize_handler(self, strategy_name: str, handler: "SessionResizeHandler") -> Self:
        """
        Register resize handler for a strategy.

        Signature:
            `(full_context, context_window, memo, session_settings) -> (new_full_context, new_context_window, new_memo)`.
        """
        if not isinstance(strategy_name, str) or strategy_name.strip() == "":
            raise ValueError("strategy_name must be a non-empty string.")
        self.__session_resize_handlers[strategy_name] = handler
        for session in self.sessions.values():
            session.register_resize_handler(strategy_name, handler)
        return self

    def __bind_session_resize_pipeline(self, session: Session):
        session.register_analysis_handler(self.__session_analysis_handler)
        for strategy_name, handler in self.__session_resize_handlers.items():
            session.register_resize_handler(strategy_name, handler)

    @staticmethod
    def _runtime_size(value) -> int:
        return len(value) if hasattr(value, "__len__") else 0

    @staticmethod
    def _get_runtime_request_run_context(settings: "Settings | None" = None):
        run_context = get_current_request_run_context()
        if run_context is not None:
            return run_context
        if settings is not None:
            return getattr(settings, "_runtime_request_run_context", None)
        return None

    def _refill_agent_chat_history_with_session(self):
        if self.activated_session is None:
            return self
        if "chat_history" in self.agent_prompt:
            del self.agent_prompt["chat_history"]
        self.agent_prompt.set("chat_history", self.activated_session.context_window)
        return self

    def activate_session(self, *, session_id: str | None = None) -> Self:
        if session_id is not None and session_id in self.sessions:
            self.activated_session = self.sessions[session_id]
            self.activated_session.plugin_manager = self.plugin_manager
            self.activated_session._memory_store_provider = lambda: getattr(self, "record_store", None)
        else:
            if session_id is None:
                session_id = uuid4().hex
            self.activated_session = Session(
                id=session_id,
                auto_resize=True,
                settings=self.settings,
                plugin_manager=self.plugin_manager,
                memory_store_provider=lambda: getattr(self, "record_store", None),
            )
            self.sessions[session_id] = self.activated_session

        self.__bind_session_resize_pipeline(self.activated_session)
        self.settings.set("runtime.session_id", self.activated_session.id)
        refresh_store = getattr(self, "_refresh_default_record_store_binding", None)
        if callable(refresh_store):
            refresh_store()
        self._bind_activated_session_memory_store()
        self._emit_session_runtime_observation(
            "activated",
            message=f"Session '{ self.activated_session.id }' activated.",
            payload={
                "session_id": self.activated_session.id,
                "agent_name": self.name,
            },
        )
        return self._refill_agent_chat_history_with_session()

    def _bind_activated_session_memory_store(self) -> None:
        if self.activated_session is None or self.activated_session.memory is None:
            return
        memory_store = getattr(self, "record_store", None)
        if memory_store is not None:
            self.activated_session.bind_memory_store(memory_store)

    def deactivate_session(self) -> Self:
        previous_session_id = self.activated_session.id if self.activated_session is not None else None
        self.activated_session = None
        self.settings.set("runtime.session_id", None)
        refresh_store = getattr(self, "_refresh_default_record_store_binding", None)
        if callable(refresh_store):
            refresh_store()
        if "chat_history" in self.agent_prompt:
            del self.agent_prompt["chat_history"]
        self.agent_prompt.set("chat_history", [])
        if previous_session_id is not None:
            self._emit_session_runtime_observation(
                "deactivated",
                message=f"Session '{ previous_session_id }' deactivated.",
                payload={
                    "session_id": previous_session_id,
                    "agent_name": self.name,
                },
            )
        return self

    def reset_chat_history(self) -> Self:
        if self.activated_session is None:
            return super().reset_chat_history()
        self.activated_session.reset_chat_history()
        return self._refill_agent_chat_history_with_session()

    async def async_reset_chat_history(self) -> Self:
        if self.activated_session is None:
            return super().reset_chat_history()
        await self.activated_session.async_reset_chat_history()
        return self._refill_agent_chat_history_with_session()

    def set_chat_history(
        self,
        chat_history: "Sequence[ChatMessage | ChatMessageDict]",
    ) -> Self:
        if self.activated_session is None:
            return super().set_chat_history(chat_history)
        self.activated_session.set_chat_history(chat_history)
        return self._refill_agent_chat_history_with_session()

    async def async_set_chat_history(
        self,
        chat_history: "Sequence[ChatMessage | ChatMessageDict]",
    ) -> Self:
        if self.activated_session is None:
            return super().set_chat_history(chat_history)
        await self.activated_session.async_set_chat_history(chat_history)
        return self._refill_agent_chat_history_with_session()

    def add_chat_history(
        self,
        chat_history: "Sequence[ChatMessage | ChatMessageDict] | ChatMessage | ChatMessageDict",
    ) -> Self:
        if self.activated_session is None:
            return super().add_chat_history(chat_history)
        self.activated_session.add_chat_history(chat_history)
        return self._refill_agent_chat_history_with_session()

    async def async_add_chat_history(
        self,
        chat_history: "Sequence[ChatMessage | ChatMessageDict] | ChatMessage | ChatMessageDict",
    ) -> Self:
        if self.activated_session is None:
            return super().add_chat_history(chat_history)
        await self.activated_session.async_add_chat_history(chat_history)
        return self._refill_agent_chat_history_with_session()

    def clean_context_window(self) -> Self:
        if self.activated_session is None:
            return super().reset_chat_history()
        self.activated_session.clean_context_window()
        return self._refill_agent_chat_history_with_session()

    async def async_clean_context_window(self) -> Self:
        if self.activated_session is None:
            return super().reset_chat_history()
        await self.activated_session.async_clean_context_window()
        return self._refill_agent_chat_history_with_session()

    async def _session_request_prefix(self, prompt: "Prompt", _settings: "Settings"):
        if self.activated_session is not None:
            _settings.set("runtime.session_id", self.activated_session.id)
        Session.apply_request_prefix(prompt, self.activated_session)
        if (
            self.activated_session is not None
            and get_current_agent_execution_run_context() is None
        ):
            await self.activated_session.async_prepare_memory(prompt, _settings)
        if self.activated_session is not None:
            memo = self.activated_session.memo
            memo_size = self._runtime_size(memo)
            await self._async_emit_session_runtime_observation(
                "applied_to_request",
                message=f"Session '{ self.activated_session.id }' applied to request.",
                payload={
                    "session_id": self.activated_session.id,
                    "context_window_size": self._runtime_size(self.activated_session.context_window),
                    "memo_size": memo_size,
                },
                run=self._get_runtime_request_run_context(_settings),
            )

    async def _session_finally(self, result: "ModelRequestResult", settings: "Settings"):
        if self.activated_session is None:
            return

        user_content, assistant_content = await Session.resolve_finally_contents(
            result,
            settings,
            self.agent_prompt,
        )

        history_changed = False
        if user_content is not None and user_content != "":
            await self.activated_session.async_add_chat_history(
                {"role": "user", "content": user_content}
            )
            history_changed = True
        if assistant_content is not None and assistant_content != "":
            await self.activated_session.async_add_chat_history(
                {"role": "assistant", "content": assistant_content}
            )
            history_changed = True
        if history_changed:
            self._refill_agent_chat_history_with_session()
        await self.activated_session.async_after_memory_turn(
            user_content=user_content,
            assistant_content=assistant_content,
            result=result,
            settings=settings,
        )
        if history_changed:
            await self._async_emit_session_runtime_observation(
                "context_appended",
                message=f"Session '{ self.activated_session.id }' appended new context.",
                payload={
                    "session_id": self.activated_session.id,
                    "has_user_content": bool(user_content),
                    "has_assistant_content": bool(assistant_content),
                    "context_window_size": self._runtime_size(self.activated_session.context_window),
                },
                run=self._get_runtime_request_run_context(settings),
            )
