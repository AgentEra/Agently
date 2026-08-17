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

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any, AsyncGenerator, Literal, cast

from httpx import AsyncClient, HTTPStatusError, RequestError, Timeout
from httpx_sse import SSEError, aconnect_sse

from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.types.data import AgentlyRequestData, SerializableValue
from agently.utils import DataFormatter


class OpenAICompatibleTransportMixin:
    name: str
    model_type: str
    plugin_settings: Any
    _stream_cleanup_tasks: set[asyncio.Task[None]]

    if TYPE_CHECKING:
        def _build_headers_with_auth(self, request_data: "AgentlyRequestData") -> dict[str, Any]: ...

        def _build_failover_headers(
            self,
            request_data: "AgentlyRequestData",
            *,
            error: Any,
            status_code: int | None,
            response_text: str | None,
            full_request_data: dict[str, Any],
            stream_started: bool,
        ) -> dict[str, Any] | None: ...

    def _create_async_client(self, **client_options: Any):
        from .. import plugin as plugin_module

        package_module = sys.modules.get(plugin_module.__package__ or "")
        package_client = getattr(package_module, "AsyncClient", AsyncClient)
        plugin_client = getattr(plugin_module, "AsyncClient", AsyncClient)
        client_factory = package_client if package_client is not AsyncClient else plugin_client
        return client_factory(**client_options)

    def _get_timeout_mode(self) -> Literal["http", "first_token"]:
        timeout_mode = self.plugin_settings.get("timeout_mode", "first_token")
        if timeout_mode == "http":
            return "http"
        return "first_token"

    def _get_timeout_configs(self) -> dict[str, Any]:
        return DataFormatter.to_str_key_dict(
            self.plugin_settings.get(
                "timeout",
                {
                    "connect": 30.0,
                    "read": 120.0,
                    "write": 30.0,
                    "pool": 30.0,
                },
            ),
            default_value={},
        )

    def _get_http_timeout(self, *, disable_read: bool = False) -> Timeout:
        timeout_configs = self._get_timeout_configs().copy()
        if disable_read:
            timeout_configs["read"] = None
        return Timeout(**timeout_configs)

    def _get_first_token_timeout_seconds(self) -> float | None:
        read_timeout = self._get_timeout_configs().get("read")
        if isinstance(read_timeout, (int, float)) and read_timeout > 0:
            return float(read_timeout)
        return None

    def _get_stream_idle_timeout_seconds(self) -> float | None:
        raw_timeout = self.plugin_settings.get("stream_idle_timeout", None)
        if raw_timeout is None or raw_timeout == -1 or raw_timeout == "-1":
            return None
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float, str)):
            return None
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return None
        return timeout if timeout > 0 else None

    def _get_non_streaming_response_timeout_seconds(self) -> float | None:
        # Non-streaming requests await one blocking response, so there is no
        # inter-chunk idle to measure and the streaming first_token/stream_idle
        # guards never apply. Reuse the configured stream_idle_timeout as a
        # whole-response liveness deadline: if the single response does not return
        # within it, treat the request as stalled so the framework can capture
        # liveness evidence and fall back, instead of hanging until the coarse
        # task-level no-progress budget (or a human) stops it. Returns None when
        # unset, preserving the previous unbounded behavior for callers that did
        # not opt into a liveness cutoff.
        return self._get_stream_idle_timeout_seconds()

    async def _await_non_streaming_response(self, post_coroutine: Any, *, timeout_seconds: float | None) -> Any:
        if timeout_seconds is None:
            return await post_coroutine
        try:
            return await asyncio.wait_for(post_coroutine, timeout=timeout_seconds)
        except asyncio.TimeoutError as e:
            raise self._build_stream_stall_error(
                stage="response_materialization",
                timeout_seconds=timeout_seconds,
                message=(
                    f"Non-streaming response made no progress before idle deadline: "
                    f"stream_idle_timeout={ timeout_seconds } seconds."
                ),
            ) from e

    def _should_use_first_token_timeout(self, request_data: "AgentlyRequestData") -> bool:
        return (
            self._get_timeout_mode() == "first_token"
            and self.model_type in ("chat", "completions")
            and bool(request_data.stream)
        )

    def _build_stream_stall_error(
        self,
        *,
        stage: str,
        timeout_seconds: float,
        message: str,
        diagnostic_context: dict[str, Any] | None = None,
    ) -> RuntimeStageStallError:
        return RuntimeStageStallError(
            message,
            stage=stage,
            status="stalled",
            idle_seconds=timeout_seconds,
            timeout_seconds=timeout_seconds,
            provider=self.name,
            model=cast(str | None, self.plugin_settings.get("model", None)),
            diagnostic_context=diagnostic_context,
        )

    @staticmethod
    def _provider_stream_timeout_context() -> dict[str, str]:
        return {
            "owner": "model_request",
            "progress_basis": "meaningful_provider_response_data",
            "transport_cleanup": "asynchronous",
        }

    @staticmethod
    def _stream_item_has_meaningful_data(item: Any) -> bool:
        """Return whether a transport item can advance model-response parsing.

        ``httpx-sse`` can expose heartbeat traffic as SSE objects whose ``data``
        is empty. Those frames prove that the socket is alive, but they
        cannot produce a model delta and therefore must not refresh response
        liveness deadlines. Non-SSE iterators remain meaningful by default so
        the timeout helpers retain their generic test and extension contract.
        """

        missing = object()
        data = getattr(item, "data", missing)
        if data is missing:
            return True
        if data is None:
            return False
        if isinstance(data, (bytes, bytearray)):
            return bool(bytes(data).strip())
        if isinstance(data, str):
            return bool(data.strip())
        return True

    def _schedule_stream_generator_cleanup(
        self,
        generator: AsyncGenerator[Any, None],
        *,
        pending_anext: asyncio.Task[Any] | None = None,
    ) -> None:
        """Close a timed-out stream without delaying the caller's deadline."""

        async def cleanup() -> None:
            if pending_anext is not None:
                try:
                    await pending_anext
                except BaseException:
                    pass
            try:
                await generator.aclose()
            except BaseException:
                pass

        cleanup_task = asyncio.create_task(cleanup())
        cleanup_tasks = getattr(self, "_stream_cleanup_tasks", None)
        if cleanup_tasks is None:
            cleanup_tasks = set()
            self._stream_cleanup_tasks = cleanup_tasks
        cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(cleanup_tasks.discard)

    async def _anext_with_strict_timeout(
        self,
        generator: AsyncGenerator[Any, None],
        *,
        timeout_seconds: float,
    ) -> Any:
        """Await one stream item without waiting for slow cancellation cleanup."""

        pending_anext = asyncio.create_task(anext(generator))
        try:
            done, _ = await asyncio.wait({pending_anext}, timeout=timeout_seconds)
        except BaseException:
            pending_anext.cancel()
            self._schedule_stream_generator_cleanup(
                generator,
                pending_anext=pending_anext,
            )
            raise
        if not done:
            pending_anext.cancel()
            self._schedule_stream_generator_cleanup(
                generator,
                pending_anext=pending_anext,
            )
            raise asyncio.TimeoutError
        return pending_anext.result()

    async def _aiter_with_first_token_timeout(
        self,
        generator: AsyncGenerator[Any, None],
        *,
        timeout_seconds: float | None,
    ) -> AsyncGenerator[Any, None]:
        if timeout_seconds is None:
            async for item in generator:
                yield item
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                self._schedule_stream_generator_cleanup(generator)
                raise self._build_stream_stall_error(
                    stage="response_first_event",
                    timeout_seconds=timeout_seconds,
                    message=f"First token timeout after { timeout_seconds } seconds.",
                    diagnostic_context=self._provider_stream_timeout_context(),
                )
            try:
                first_item = await self._anext_with_strict_timeout(
                    generator,
                    timeout_seconds=remaining_seconds,
                )
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as e:
                raise self._build_stream_stall_error(
                    stage="response_first_event",
                    timeout_seconds=timeout_seconds,
                    message=f"First token timeout after { timeout_seconds } seconds.",
                    diagnostic_context=self._provider_stream_timeout_context(),
                ) from e
            if self._stream_item_has_meaningful_data(first_item):
                break

        yield first_item
        async for item in generator:
            yield item

    async def _aiter_with_stream_idle_timeout(
        self,
        generator: AsyncGenerator[Any, None],
        *,
        timeout_seconds: float | None,
    ) -> AsyncGenerator[Any, None]:
        if timeout_seconds is None:
            async for item in generator:
                yield item
            return

        while True:
            try:
                first_item = await anext(generator)
            except StopAsyncIteration:
                return
            if self._stream_item_has_meaningful_data(first_item):
                break
        yield first_item

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                self._schedule_stream_generator_cleanup(generator)
                raise self._build_stream_stall_error(
                    stage="response_stream",
                    timeout_seconds=timeout_seconds,
                    message=f"Stream idle timeout after { timeout_seconds } seconds.",
                    diagnostic_context=self._provider_stream_timeout_context(),
                )
            try:
                item = await self._anext_with_strict_timeout(
                    generator,
                    timeout_seconds=remaining_seconds,
                )
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as e:
                raise self._build_stream_stall_error(
                    stage="response_stream",
                    timeout_seconds=timeout_seconds,
                    message=f"Stream idle timeout after { timeout_seconds } seconds.",
                    diagnostic_context=self._provider_stream_timeout_context(),
                ) from e
            if not self._stream_item_has_meaningful_data(item):
                continue
            yield item
            deadline = loop.time() + timeout_seconds

    async def _aiter_sse_with_retry(
        self,
        client: AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict[str, Any],
        json: "SerializableValue",
    ):
        """Open one SSE connection for the current public request attempt.

        The legacy method name is retained for private-extension compatibility,
        but reconnection is intentionally owned by ``AttemptRunner`` so every
        physical request consumes and reports one public attempt.
        """

        async def _aiter_sse():
            request_headers = dict(headers)
            request_headers["Accept"] = "text/event-stream"
            async with aconnect_sse(
                client,
                method,
                url,
                headers=request_headers,
                json=json,
            ) as event_source:
                try:
                    async for sse in event_source.aiter_sse():
                        yield sse
                except GeneratorExit:
                    pass

        return _aiter_sse()

    async def _request_model_legacy(self, request_data: "AgentlyRequestData") -> AsyncGenerator[tuple[str, Any], None]:
        headers_with_auth = self._build_headers_with_auth(request_data)

        # request
        # stream request
        if self.model_type in ("chat", "completions") and request_data.stream:
            client_options = request_data.client_options.copy()
            if self._should_use_first_token_timeout(request_data):
                client_options.update({"timeout": self._get_http_timeout(disable_read=True)})

            async with self._create_async_client(**client_options) as client:
                client.headers.update(headers_with_auth)
                full_request_data = DataFormatter.to_str_key_dict(
                    request_data.data,
                    value_format="serializable",
                    default_value={},
                )
                full_request_data.update(request_data.request_options)
                stream_started = False
                while True:
                    try:
                        has_done = False
                        sse_generator = await self._aiter_sse_with_retry(
                            client,
                            "POST",
                            request_data.request_url,
                            json=full_request_data,
                            headers=headers_with_auth,
                        )
                        if self._should_use_first_token_timeout(request_data):
                            sse_generator = self._aiter_with_first_token_timeout(
                                sse_generator,
                                timeout_seconds=self._get_first_token_timeout_seconds(),
                            )
                        sse_generator = self._aiter_with_stream_idle_timeout(
                            sse_generator,
                            timeout_seconds=self._get_stream_idle_timeout_seconds(),
                        )
                        async for sse in sse_generator:
                            stream_started = True
                            yield sse.event, sse.data
                            if sse.data.strip() == "[DONE]":
                                has_done = True
                                break
                        if not has_done:
                            yield "message", "[DONE]"
                        break
                    except SSEError as sse_error:
                        response = await client.post(
                            request_data.request_url,
                            json=full_request_data,
                            headers=headers_with_auth,
                        )
                        if response.status_code >= 400:
                            request_error = RequestError(
                                f"Status Code: { response.status_code }\n"
                                f"Detail: { response.text }"
                            )
                            failover_headers = self._build_failover_headers(
                                request_data,
                                error=request_error,
                                status_code=response.status_code,
                                response_text=response.text,
                                full_request_data=full_request_data,
                                stream_started=stream_started,
                            )
                            if failover_headers is not None:
                                headers_with_auth = failover_headers
                                client.headers.update(headers_with_auth)
                                continue
                            yield "error", request_error
                        else:
                            content_type = response.headers.get("Content-Type", "")
                            if content_type.startswith("application/json"):
                                try:
                                    error_json = response.json()
                                except Exception:
                                    error_json = await response.aread()
                                    error_json = json.loads(error_json.decode())
                                error = error_json["error"]
                                error_detail = error["message"] if "message" in error else ""
                                yield "error", error_detail
                            else:
                                yield "error", sse_error
                        break
                    except HTTPStatusError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=e.response.status_code,
                            response_text=e.response.text,
                            full_request_data=full_request_data,
                            stream_started=stream_started,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except TimeoutError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=None,
                            response_text=None,
                            full_request_data=full_request_data,
                            stream_started=stream_started,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except RequestError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=None,
                            response_text=None,
                            full_request_data=full_request_data,
                            stream_started=stream_started,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except Exception as e:
                        yield "error", e
                        break
        # normal request
        else:
            async with self._create_async_client(**request_data.client_options) as client:
                client.headers.update(headers_with_auth)
                full_request_data = DataFormatter.to_str_key_dict(
                    request_data.data,
                    value_format="serializable",
                    default_value={},
                )
                full_request_data.update(request_data.request_options)
                response_timeout = self._get_non_streaming_response_timeout_seconds()
                while True:
                    try:
                        post_coroutine = client.post(
                            request_data.request_url,
                            json=full_request_data,
                            headers=headers_with_auth,
                        )
                        response = await self._await_non_streaming_response(
                            post_coroutine,
                            timeout_seconds=response_timeout,
                        )
                        if response.status_code >= 400:
                            e = RequestError(
                                f"Status Code: { response.status_code }\n"
                                f"Detail: { response.text }"
                            )
                            failover_headers = self._build_failover_headers(
                                request_data,
                                error=e,
                                status_code=response.status_code,
                                response_text=response.text,
                                full_request_data=full_request_data,
                                stream_started=False,
                            )
                            if failover_headers is not None:
                                headers_with_auth = failover_headers
                                client.headers.update(headers_with_auth)
                                continue
                            yield "error", e
                        else:
                            yield "message", response.content.decode()
                            yield "message", "[DONE]"
                        break
                    except RuntimeStageStallError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=None,
                            response_text=None,
                            full_request_data=full_request_data,
                            stream_started=False,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        # Liveness stall must propagate so the framework records it as
                        # model-request liveness evidence and can fall back; do not let
                        # the generic handler below swallow it into an untyped error event.
                        raise
                    except HTTPStatusError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=e.response.status_code,
                            response_text=e.response.text,
                            full_request_data=full_request_data,
                            stream_started=False,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except RequestError as e:
                        failover_headers = self._build_failover_headers(
                            request_data,
                            error=e,
                            status_code=None,
                            response_text=None,
                            full_request_data=full_request_data,
                            stream_started=False,
                        )
                        if failover_headers is not None:
                            headers_with_auth = failover_headers
                            client.headers.update(headers_with_auth)
                            continue
                        yield "error", e
                        break
                    except Exception as e:
                        yield "error", e
                        break
