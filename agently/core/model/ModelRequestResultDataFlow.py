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
import inspect
import warnings

from collections.abc import Mapping, Sequence
from typing import Any, Awaitable, Literal, TYPE_CHECKING, cast

from pydantic import BaseModel

from agently.core.application.AgentExecution import RuntimeStageStallError
from agently.core.runtime import bind_runtime_context, get_current_agent_execution_context
from agently.utils import DataFormatter, DataLocator, DataPathBuilder

from .OutputObservationPolicy import OutputObservationPolicy

if TYPE_CHECKING:
    from agently.types.data import OutputValidateHandler, OutputValidateResult
    from .ModelRequestResult import ModelRequestResult


class ModelRequestResultDataFlow:
    def __init__(self, result: "ModelRequestResult"):
        self._result = result

    def get_auto_ensure_policies(
        self, *, key_style: Literal["dot", "slash"] = "dot"
    ) -> dict[str, Literal["presence", "not_null"]]:
        result = self._result
        cache_key = key_style
        if cache_key in result._auto_ensure_policies_cache:
            return cast(dict[str, Literal["presence", "not_null"]], result._auto_ensure_policies_cache[cache_key])

        try:
            prompt_output = result.prompt.to_prompt_object().output
        except Exception:
            prompt_output = None

        if not isinstance(prompt_output, (Mapping, Sequence)) or isinstance(prompt_output, str):
            result._auto_ensure_policies_cache[cache_key] = {}
            result._auto_ensure_keys_cache[cache_key] = []
            return {}

        try:
            ensure_policies = DataPathBuilder.extract_ensure_path_policies(prompt_output, style=key_style)
        except Exception:
            ensure_policies = {}

        result._auto_ensure_policies_cache[cache_key] = ensure_policies
        result._auto_ensure_keys_cache[cache_key] = list(ensure_policies.keys())
        return ensure_policies

    def get_declared_pydantic_output_model(self) -> type[BaseModel] | None:
        declared_output = self._result.prompt.get("output")
        if isinstance(declared_output, type) and issubclass(declared_output, BaseModel):
            return declared_output
        return None

    def get_output_validation_feedback(self) -> list[str]:
        if self.get_declared_pydantic_output_model() is None:
            return []
        getter = getattr(self._result._response_parser, "get_output_validation_feedback", None)
        if not callable(getter):
            return []
        feedback = getter()
        if not isinstance(feedback, list):
            return []
        return [str(item)[:300] for item in feedback[:8] if str(item).strip()]

    @staticmethod
    def get_validate_output_correction(validation_outcome: Mapping[str, Any]) -> list[str]:
        if validation_outcome.get("kind") != "failed":
            return []
        reason = validation_outcome.get("reason")
        if reason is None:
            return []
        reason_text = str(reason).strip()
        return [reason_text[:300]] if reason_text else []

    def get_auto_ensure_keys(self, *, key_style: Literal["dot", "slash"] = "dot") -> list[str]:
        result = self._result
        cache_key = key_style
        if cache_key in result._auto_ensure_keys_cache:
            return result._auto_ensure_keys_cache[cache_key]
        return list(self.get_auto_ensure_policies(key_style=key_style).keys())

    @staticmethod
    def merge_ensure_keys(auto_keys: list[str], explicit_keys: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for key in [*auto_keys, *explicit_keys]:
            if key not in seen:
                seen.add(key)
                merged.append(key)
        return merged

    @staticmethod
    def resolve_ensure_policies(
        active_ensure_keys: list[str],
        auto_ensure_policies: dict[str, Literal["presence", "not_null"]],
    ) -> dict[str, Literal["presence", "not_null"]]:
        return {key: auto_ensure_policies.get(key, "presence") for key in active_ensure_keys}

    @staticmethod
    def ensure_value_is_present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return bool(value) and all(ModelRequestResultDataFlow.ensure_value_is_present(item) for item in value)
        return True

    def is_strict_output_enabled(self) -> bool:
        try:
            prompt_object = self._result.prompt.to_prompt_object()
            return bool(getattr(prompt_object, "ensure_all_keys", False)) and prompt_object.output_format in {
                "json",
                "flat_markdown",
                "hybrid",
                "xml_field",
                "yaml_literal",
            }
        except Exception:
            return False

    @staticmethod
    def handler_name(handler: Any, index: int) -> str:
        name = getattr(handler, "__name__", None)
        if isinstance(name, str) and name:
            return name
        return f"validate_handler_{ index + 1 }"

    def resolve_validate_handlers(
        self,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
    ) -> list["OutputValidateHandler"]:
        handlers = self._result._extension_handlers.get("validate_handlers", [])
        resolved: list["OutputValidateHandler"] = list(handlers) if isinstance(handlers, list) else []
        if validate_handler is None:
            return resolved
        if isinstance(validate_handler, list):
            resolved.extend(validate_handler)
        else:
            resolved.append(validate_handler)
        return resolved

    async def build_validate_result_snapshot(self) -> dict[str, Any]:
        result = self._result
        parsed_result = result._response_parser.full_result_data.get("parsed_result")
        if isinstance(parsed_result, dict):
            return parsed_result.copy()
        if isinstance(parsed_result, Mapping):
            return dict(parsed_result)
        result_object = result._response_parser.full_result_data.get("result_object")
        model_dump = getattr(result_object, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
            if isinstance(dumped, Mapping):
                return dict(dumped)
        return {"value": parsed_result}

    async def build_validate_context(
        self,
        *,
        value: dict[str, Any],
        retry_count: int,
        max_retries: int,
        meta: dict[str, Any] | None = None,
    ):
        from agently.types.data import OutputValidateContext

        result = self._result
        response_text = await result._response_parser.async_get_text()
        return OutputValidateContext(
            value=value,
            agent_name=result.agent_name,
            response_id=result._response_id,
            attempt_index=result.attempt_index,
            retry_count=retry_count,
            max_retries=max_retries,
            prompt=result.prompt,
            settings=result.settings,
            request_run_context=result.request_run_context,
            model_run_context=result.model_run_context,
            response_text=response_text,
            parsed_result=result._response_parser.full_result_data.get("parsed_result"),
            result_object=result._response_parser.full_result_data.get("result_object"),
            meta=meta,
        )

    @staticmethod
    def exception_to_raise(error: BaseException | str | None):
        if error is None:
            return None
        if isinstance(error, BaseException):
            return error
        return ValueError(str(error))

    def materialization_idle_timeout(self) -> float | None:
        raw_timeout = self._result.settings.get("response.materialization_idle_timeout", None)
        if raw_timeout is None or raw_timeout == -1 or raw_timeout == "-1":
            return None
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float, str)):
            return None
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return None
        return timeout if timeout >= 0 else None

    def record_materialization_progress(self, *, stage: str, status: str, event_type: str) -> None:
        context = get_current_agent_execution_context()
        record_progress = getattr(context, "record_progress", None)
        if not callable(record_progress):
            return
        result = self._result
        record_progress(
            stage=stage,
            status=status,
            event_type=event_type,
            response_id=result._response_id,
            run_id=result.model_run_context.run_id if result.model_run_context is not None else None,
            meta={
                "agent_name": result.agent_name,
                "attempt_index": result.attempt_index,
                "request_run_id": result.request_run_context.run_id if result.request_run_context is not None else None,
                "model_run_id": result.model_run_context.run_id if result.model_run_context is not None else None,
            },
            notify=False,
        )

    async def await_materialization(self, awaitable: Awaitable[Any], *, stage: str):
        result = self._result
        timeout = self.materialization_idle_timeout()
        self.record_materialization_progress(
            stage=stage,
            status="started",
            event_type=f"{stage}.started",
        )
        try:
            if timeout is None:
                materialized = await awaitable
            else:
                materialized = await asyncio.wait_for(awaitable, timeout=timeout)
            self.record_materialization_progress(
                stage=stage,
                status="completed",
                event_type=f"{stage}.completed",
            )
            return materialized
        except asyncio.TimeoutError as error:
            stall_error = RuntimeStageStallError(
                f"Response materialization idle timeout after { timeout } seconds.",
                stage=stage,
                status="stalled",
                response_id=result._response_id,
                run_id=result.model_run_context.run_id if result.model_run_context is not None else None,
                agent_name=result.agent_name,
                idle_seconds=timeout,
                timeout_seconds=timeout,
            )
            self.record_materialization_progress(
                stage=stage,
                status="stalled",
                event_type=f"{stage}.stalled",
            )
            await self.emit_materialization_stall(stall_error)
            raise stall_error from error

    async def emit_materialization_stall(self, error: RuntimeStageStallError):
        from agently.base import async_emit_runtime

        result = self._result
        await async_emit_runtime(
            {
                "event_type": "model.response_materialization_stalled",
                "source": "ModelRequestResult",
                "level": "ERROR",
                "message": str(error),
                "payload": error.to_diagnostic(),
                "error": error,
                "run": result.model_run_context or result.request_run_context,
            }
        )

    @staticmethod
    def normalize_validate_result(
        raw_result: "OutputValidateResult",
        *,
        validator_name: str,
    ) -> dict[str, Any]:
        if isinstance(raw_result, bool):
            return {
                "ok": raw_result,
                "kind": "passed" if raw_result else "failed",
                "reason": None if raw_result else f"Validation failed in { validator_name }.",
                "payload": None,
                "validator_name": validator_name,
                "retryable": not raw_result,
                "no_retry": False,
                "stop": False,
                "raise_value": None,
                "error": None,
            }

        if isinstance(raw_result, dict):
            ok = bool(raw_result.get("ok", False))
            reason_value = raw_result.get("reason")
            reason = str(reason_value) if reason_value is not None else None
            payload = raw_result.get("payload")
            validation_payload = payload if isinstance(payload, dict) else None
            no_retry = bool(raw_result.get("no_retry", False))
            stop = bool(raw_result.get("stop", False))
            explicit_raise = raw_result.get("raise", raw_result.get("error", raw_result.get("exception")))
            validator_name_value = raw_result.get("validator_name")
            return {
                "ok": ok,
                "kind": "passed" if ok else "failed",
                "reason": (
                    reason if reason is not None else (None if ok else f"Validation failed in { validator_name }.")
                ),
                "payload": validation_payload,
                "validator_name": str(validator_name_value) if validator_name_value is not None else validator_name,
                "retryable": False if ok else not (no_retry or stop or explicit_raise is not None),
                "no_retry": no_retry,
                "stop": stop,
                "raise_value": None if ok else explicit_raise,
                "error": None,
            }

        return {
            "ok": False,
            "kind": "error",
            "reason": f"Unsupported validation result from { validator_name }: { type(raw_result).__name__ }",
            "payload": {"return_type": type(raw_result).__name__},
            "validator_name": validator_name,
            "retryable": True,
            "no_retry": False,
            "stop": False,
            "raise_value": None,
            "error": None,
        }

    @staticmethod
    def normalize_validate_error(
        error: BaseException,
        *,
        validator_name: str,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "kind": "error",
            "reason": str(error) if str(error) else f"Validation handler { validator_name } raised an exception.",
            "payload": None,
            "validator_name": validator_name,
            "retryable": True,
            "no_retry": False,
            "stop": False,
            "raise_value": None,
            "error": error,
        }

    async def emit_validation_runtime_event(
        self,
        event_type: str,
        *,
        level: str,
        message: str,
        outcome: dict[str, Any],
        retry_count: int,
        max_retries: int,
        response_text: str,
    ):
        from agently.base import async_emit_runtime

        result = self._result
        error = outcome.get("error")
        output_observation_policy = OutputObservationPolicy.from_settings(result.settings)
        payload = output_observation_policy.project_event_payload(
            {
                "agent_name": result.agent_name,
                "response_id": result._response_id,
                "attempt_index": result.attempt_index,
                "retry_count": retry_count,
                "max_retries": max_retries,
                "validator_name": outcome.get("validator_name"),
                "reason": outcome.get("reason"),
                "stop": outcome.get("stop", False),
                "no_retry": outcome.get("no_retry", False),
                "error_kind": type(error).__name__ if isinstance(error, BaseException) else None,
                "validation_payload": DataFormatter.sanitize(outcome.get("payload")),
                "response_text": response_text,
            },
            raw_text_fields=("response_text", "reason"),
            opaque_fields=("validation_payload",),
        )
        await async_emit_runtime(
            {
                "event_type": event_type,
                "source": "ModelRequestResult",
                "level": level,
                "message": message,
                "payload": payload,
                "error": (
                    None if output_observation_policy.active else error if isinstance(error, BaseException) else None
                ),
                "run": result.request_run_context,
            }
        )

    async def run_validate_handlers_once(
        self,
        handlers: list["OutputValidateHandler"],
        *,
        retry_count: int,
        max_retries: int,
    ) -> dict[str, Any] | None:
        result = self._result
        if result._validate_outcome is not None:
            signature = tuple(id(handler) for handler in handlers)
            if (
                len(handlers) > 0
                and result._validate_handler_signature is not None
                and signature != result._validate_handler_signature
            ):
                warnings.warn(
                    "Validation already finalized for this response result. New validate handlers are ignored.",
                    stacklevel=2,
                )
            return result._validate_outcome

        if len(handlers) == 0:
            return None

        signature = tuple(id(handler) for handler in handlers)
        async with result._validate_lock:
            if result._validate_outcome is not None:
                if result._validate_handler_signature is not None and signature != result._validate_handler_signature:
                    warnings.warn(
                        "Validation already finalized for this response result. New validate handlers are ignored.",
                        stacklevel=2,
                    )
                return result._validate_outcome

            validate_value = await self.build_validate_result_snapshot()
            response_text = await result._response_parser.async_get_text()
            for index, handler in enumerate(handlers):
                validator_name = self.handler_name(handler, index)
                context = await self.build_validate_context(
                    value=validate_value,
                    retry_count=retry_count,
                    max_retries=max_retries,
                    meta={"validator_name": validator_name},
                )
                try:
                    raw_result = handler(validate_value, context)
                    if inspect.isawaitable(raw_result):
                        raw_result = await raw_result
                    outcome = self.normalize_validate_result(
                        cast("OutputValidateResult", raw_result),
                        validator_name=validator_name,
                    )
                except BaseException as error:
                    outcome = self.normalize_validate_error(error, validator_name=validator_name)

                if not outcome["ok"]:
                    event_type = "model.validation_error" if outcome["kind"] == "error" else "model.validation_failed"
                    level = "ERROR" if outcome["kind"] == "error" else "WARNING"
                    await self.emit_validation_runtime_event(
                        event_type,
                        level=level,
                        message=f"Output validation failed in { outcome['validator_name'] }.",
                        outcome=outcome,
                        retry_count=retry_count,
                        max_retries=max_retries,
                        response_text=response_text,
                    )
                    result._validate_outcome = outcome
                    result._validate_handler_signature = signature
                    return outcome

            result._validate_outcome = {
                "ok": True,
                "kind": "passed",
                "reason": None,
                "payload": None,
                "validator_name": None,
                "retryable": False,
                "no_retry": False,
                "stop": False,
                "raise_value": None,
                "error": None,
            }
            result._validate_handler_signature = signature
            return result._validate_outcome

    async def emit_retrying_event(
        self,
        *,
        retry_count: int,
        response_text: str,
        active_ensure_keys: list[str],
        strict_output: bool,
        key_style: Literal["dot", "slash"],
        validation_outcome: dict[str, Any] | None = None,
        retry_reason: str = "output_constraints",
        message: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ):
        from agently.base import async_emit_runtime

        result = self._result
        payload = {
            "agent_name": result.agent_name,
            "response_id": result._response_id,
            "retry_count": retry_count,
            "attempt_index": result.attempt_index,
            "next_attempt_index": result.attempt_index + 1,
            "model_run_id": result.model_run_context.run_id if result.model_run_context is not None else None,
            "response_text": response_text,
            "ensure_keys": active_ensure_keys,
            "strict_output": strict_output,
            "key_style": key_style,
            "retry_reason": retry_reason,
        }
        if validation_outcome is not None:
            payload.update(
                {
                    "validator_name": validation_outcome.get("validator_name"),
                    "validation_reason": validation_outcome.get("reason"),
                    "validation_stop": validation_outcome.get("stop", False),
                    "validation_no_retry": validation_outcome.get("no_retry", False),
                    "validation_payload": DataFormatter.sanitize(validation_outcome.get("payload")),
                }
            )
        if extra_payload:
            payload.update(DataFormatter.sanitize(extra_payload))
        payload = OutputObservationPolicy.from_settings(result.settings).project_event_payload(
            payload,
            raw_text_fields=("response_text", "validation_reason"),
            opaque_fields=("validation_payload",),
        )
        with bind_runtime_context(
            parent_run_context=result.request_run_context,
            request_run_context=result.request_run_context,
            model_run_context=result.model_run_context,
            settings=result.settings,
        ):
            await async_emit_runtime(
                {
                    "event_type": "model.retrying",
                    "source": "ModelRequestResult",
                    "level": "WARNING",
                    "message": (
                        message
                        if message is not None
                        else (
                            "Output validation failed. Preparing retry."
                            if validation_outcome is not None
                            else "No target data in response. Preparing retry."
                        )
                    ),
                    "payload": payload,
                    "run": result.request_run_context,
                }
            )

    async def try_auto_degradation(
        self,
        *,
        type: Literal["original", "parsed", "all"],
        data: Any,
        active_ensure_keys: list[str],
        validate_handler: Any,
        key_style: Literal["dot", "slash"],
        max_retries: int,
        raise_ensure_failure: bool,
        retry_count: int,
    ) -> Any | None:
        """If auto-resolved format failed, degrade to json and retry.

        Returns the retried data on success, ``None`` if no degradation needed.
        """
        result = self._result
        try:
            prompt_object = result.prompt.to_prompt_object()
            if not (
                getattr(prompt_object, "output_format_resolved_from_auto", False)
                and prompt_object.output_format in ("flat_markdown", "hybrid", "xml_field", "yaml_literal")
            ):
                return None
            parsed_result = result._response_parser.full_result_data.get("parsed_result")
            if parsed_result is not None:
                return None

            original_format = prompt_object.output_format
            parse_error = result._response_parser.full_result_data.get("extra", {}).get("parse_error")
            result.prompt.set("output_format", "json")
            await self.emit_retrying_event(
                retry_count=retry_count,
                response_text=await result._response_parser.async_get_text(),
                active_ensure_keys=active_ensure_keys,
                strict_output=False,
                key_style=key_style,
                retry_reason="format_degradation",
                message=(f"Auto-format '{original_format}' parse failed. " f"Degrading to json."),
                extra_payload={
                    "auto_degradation_reason": "auto_resolved_format_parse_failed",
                    "from_output_format": original_format,
                    "to_output_format": "json",
                    "parse_error": parse_error,
                },
            )
            return await self.retry_get_data(
                type=type,
                ensure_keys=active_ensure_keys,
                validate_handler=validate_handler,
                key_style=key_style,
                max_retries=max_retries,
                raise_ensure_failure=raise_ensure_failure,
                retry_count=retry_count + 1,
            )
        except Exception:
            return None

    @staticmethod
    def build_validation_failure_exception(outcome: dict[str, Any]):
        explicit_error = ModelRequestResultDataFlow.exception_to_raise(cast(BaseException | str | None, outcome.get("raise_value")))
        if explicit_error is not None:
            return explicit_error
        validator_name = outcome.get("validator_name")
        reason = outcome.get("reason")
        if validator_name:
            return ValueError(f"Output validation failed in { validator_name }: { reason or 'unknown reason' }")
        return ValueError(reason or "Output validation failed.")

    @staticmethod
    def build_output_retry_failure_exception(
        *,
        active_ensure_keys: list[str],
        strict_output: bool,
        max_retries: int,
    ) -> ValueError:
        constraints: list[str] = []
        if strict_output:
            constraints.append("strict output")
        if active_ensure_keys:
            constraints.append(f"ensure keys { active_ensure_keys }")
        constraint_text = " and ".join(constraints) if constraints else "output constraints"
        return ValueError(f"Can not satisfy { constraint_text } within { max_retries } retries.")

    async def _apply_retry_backoff(self, retry_count: int) -> None:
        """Sleep an exponential backoff before re-issuing a model request.

        Opt-in: with no ``model_request.retry_backoff_base`` configured, retries
        re-issue immediately (current behavior). When set, retries back off with
        clamped exponential jitter to avoid amplifying provider error storms.
        """
        settings = self._result.settings
        base_raw: Any = settings.get("model_request.retry_backoff_base", None)
        if base_raw is None:
            return
        try:
            base = float(cast(Any, base_raw))
        except (TypeError, ValueError):
            return
        if base <= 0:
            return
        from agently.utils.RequestScheduler import RequestScheduler

        cap_raw: Any = settings.get("model_request.retry_backoff_max", 30.0)
        try:
            cap = float(cast(Any, cap_raw))
        except (TypeError, ValueError):
            cap = 30.0
        await asyncio.sleep(RequestScheduler.backoff_delay(max(1, retry_count), base=base, cap=cap))

    async def retry_get_data(
        self,
        *,
        type: Literal["original", "parsed", "all"],
        ensure_keys: list[str],
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None",
        key_style: Literal["dot", "slash"],
        max_retries: int,
        raise_ensure_failure: bool,
        retry_count: int,
        output_correction: list[str] | None = None,
    ) -> Any:
        from agently.core.model.ModelRequestRunner import ModelRequestRunner
        from agently.core.model.Prompt import Prompt

        result = self._result
        await self._apply_retry_backoff(retry_count)
        retry_prompt = result.prompt
        if output_correction:
            prompt_snapshot = result.prompt.get()
            retry_prompt = Prompt(
                result.plugin_manager,
                result.settings,
                prompt_dict=prompt_snapshot if isinstance(prompt_snapshot, dict) else {},
            )
            retry_prompt.update(
                {
                    "OUTPUT CORRECTION": [
                        "The previous response did not satisfy the declared output contract.",
                        "Return a complete replacement output that fixes every violation below.",
                        *output_correction,
                    ]
                }
            )
        retry_result = ModelRequestRunner(
            result.agent_name,
            result.plugin_manager,
            result.settings,
            retry_prompt,
            result._extension_handlers,
            run_context=result.request_run_context,
            attempt_index=result.attempt_index + 1,
        ).result
        data = await retry_result.async_get_data(
            type=type,
            ensure_keys=ensure_keys,
            validate_handler=validate_handler,
            key_style=key_style,
            max_retries=max_retries,
            raise_ensure_failure=raise_ensure_failure,
            _retry_count=retry_count,
        )
        result._accepted_retry_result = retry_result._accepted_retry_result or retry_result
        return data

    async def async_get_data(
        self,
        *,
        type: Literal['original', 'parsed', 'all'] = "parsed",
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
        retry_count: int = 0,
    ) -> Any:
        result = self._result
        if result._accepted_retry_result is not None:
            return await result._accepted_retry_result.async_get_data(
                type=type,
                ensure_keys=ensure_keys,
                validate_handler=validate_handler,
                key_style=key_style,
                max_retries=max_retries,
                raise_ensure_failure=raise_ensure_failure,
                _retry_count=retry_count,
            )
        auto_ensure_policies = self.get_auto_ensure_policies(key_style=key_style)
        auto_ensure_keys = list(auto_ensure_policies.keys())
        strict_output = self.is_strict_output_enabled()
        active_validate_handlers = self.resolve_validate_handlers(validate_handler)
        if ensure_keys is None:
            active_ensure_keys = auto_ensure_keys
        elif len(ensure_keys) == 0:
            active_ensure_keys = []
        else:
            active_ensure_keys = self.merge_ensure_keys(auto_ensure_keys, ensure_keys)
        active_ensure_policies = self.resolve_ensure_policies(active_ensure_keys, auto_ensure_policies)
        should_validate = bool(active_validate_handlers) or result._validate_outcome is not None
        has_pydantic_output = self.get_declared_pydantic_output_model() is not None
        needs_constraint_flow = (
            type in ("parsed", "all") and (active_ensure_keys or strict_output or has_pydantic_output)
        ) or should_validate
        if not needs_constraint_flow:
            try:
                data = await self.await_materialization(
                    result._response_parser.async_get_data(type=type),
                    stage="response_materialization",
                )
                await result._drain_response_parser_observations()
                if type in ("parsed", "all") and retry_count < max_retries:
                    degraded_data = await self.try_auto_degradation(
                        type=type,
                        data=data,
                        active_ensure_keys=active_ensure_keys,
                        validate_handler=validate_handler,
                        key_style=key_style,
                        max_retries=max_retries,
                        raise_ensure_failure=raise_ensure_failure,
                        retry_count=retry_count,
                    )
                    if degraded_data is not None:
                        return degraded_data
                return data
            finally:
                await result._drain_response_parser_observations()
                await result._run_finally_handlers_once()

        try:
            data = await self.await_materialization(
                result._response_parser.async_get_data(type=type),
                stage="response_materialization",
            )
            await result._drain_response_parser_observations()

            if type in ("parsed", "all") and retry_count < max_retries:
                degraded_data = await self.try_auto_degradation(
                    type=type,
                    data=data,
                    active_ensure_keys=active_ensure_keys,
                    validate_handler=validate_handler,
                    key_style=key_style,
                    max_retries=max_retries,
                    raise_ensure_failure=raise_ensure_failure,
                    retry_count=retry_count,
                )
                if degraded_data is not None:
                    return degraded_data

            try:
                constraint_data = data
                if type == "all" and isinstance(data, Mapping):
                    constraint_data = data.get("parsed_result")
                output_validation_feedback = self.get_output_validation_feedback()
                if output_validation_feedback:
                    raise ValueError("Pydantic output validation failed: " + "; ".join(output_validation_feedback))
                if has_pydantic_output and result._response_parser.full_result_data.get("result_object") is None:
                    raise ValueError("Pydantic output validation failed: no validated result object is available.")
                if strict_output:
                    parsed_result = result._response_parser.full_result_data.get("parsed_result")
                    result_object = result._response_parser.full_result_data.get("result_object")
                    if parsed_result is None or result_object is None:
                        raise ValueError(
                            "Strict output validation failed: parsed result or strict result object is missing."
                        )
                if active_ensure_keys:
                    for ensure_key in active_ensure_keys:
                        empty = object()
                        if not isinstance(constraint_data, (Mapping, Sequence)) or isinstance(constraint_data, str):
                            raise ValueError(f"Missing ensure key: { ensure_key }")
                        located_value = DataLocator.locate_path_in_dict(
                            constraint_data,
                            ensure_key,
                            key_style,
                            default=empty,
                        )
                        if located_value is empty:
                            raise ValueError(f"Missing ensure key: { ensure_key }")
                        if active_ensure_policies.get(
                            ensure_key, "presence"
                        ) == "not_null" and not self.ensure_value_is_present(located_value):
                            raise ValueError(f"Missing ensure key: { ensure_key }")
            except Exception as constraint_error:
                output_validation_feedback = self.get_output_validation_feedback()
                output_correction = (
                    output_validation_feedback if output_validation_feedback else [str(constraint_error)[:300]]
                )
                await self.emit_retrying_event(
                    retry_count=retry_count,
                    response_text=await result._response_parser.async_get_text(),
                    active_ensure_keys=active_ensure_keys,
                    strict_output=strict_output,
                    key_style=key_style,
                    retry_reason="output_constraints",
                )

                if retry_count < max_retries:
                    return await self.retry_get_data(
                        type=type,
                        ensure_keys=active_ensure_keys,
                        validate_handler=validate_handler,
                        key_style=key_style,
                        max_retries=max_retries,
                        raise_ensure_failure=raise_ensure_failure,
                        retry_count=retry_count + 1,
                        output_correction=output_correction,
                    )
                if output_validation_feedback:
                    retry_label = "retry" if max_retries == 1 else "retries"
                    raise ValueError(
                        "Pydantic output validation failed after "
                        f"{max_retries} {retry_label}: " + "; ".join(output_validation_feedback)
                    )
                if raise_ensure_failure:
                    raise self.build_output_retry_failure_exception(
                        active_ensure_keys=active_ensure_keys,
                        strict_output=strict_output,
                        max_retries=max_retries,
                    )
                return await self.await_materialization(
                    result._response_parser.async_get_data(type=type),
                    stage="response_materialization",
                )

            validation_outcome = await self.run_validate_handlers_once(
                active_validate_handlers,
                retry_count=retry_count,
                max_retries=max_retries,
            )
            if validation_outcome is not None and not validation_outcome["ok"]:
                if validation_outcome.get("retryable", True) and retry_count < max_retries:
                    await self.emit_retrying_event(
                        retry_count=retry_count,
                        response_text=await result._response_parser.async_get_text(),
                        active_ensure_keys=active_ensure_keys,
                        strict_output=strict_output,
                        key_style=key_style,
                        validation_outcome=validation_outcome,
                        retry_reason="validate",
                    )
                    return await self.retry_get_data(
                        type=type,
                        ensure_keys=active_ensure_keys,
                        validate_handler=validate_handler,
                        key_style=key_style,
                        max_retries=max_retries,
                        raise_ensure_failure=raise_ensure_failure,
                        retry_count=retry_count + 1,
                        output_correction=self.get_validate_output_correction(validation_outcome),
                    )
                if validation_outcome.get("raise_value") is not None or raise_ensure_failure:
                    raise self.build_validation_failure_exception(validation_outcome)
                return await self.await_materialization(
                    result._response_parser.async_get_data(type=type),
                    stage="response_materialization",
                )
            return data
        finally:
            await result._drain_response_parser_observations()
            await result._run_finally_handlers_once()

    async def async_get_data_object(
        self,
        *,
        ensure_keys: list[str] | None = None,
        validate_handler: "OutputValidateHandler | list[OutputValidateHandler] | None" = None,
        key_style: Literal["dot", "slash"] = "dot",
        max_retries: int = 3,
        raise_ensure_failure: bool = True,
    ):
        result = self._result
        if result._accepted_retry_result is not None:
            return await self.await_materialization(
                result._accepted_retry_result._response_parser.async_get_data_object(),
                stage="response_materialization",
            )
        auto_ensure_keys = self.get_auto_ensure_keys(key_style=key_style)
        strict_output = self.is_strict_output_enabled()
        active_validate_handlers = self.resolve_validate_handlers(validate_handler)
        if ensure_keys is None:
            active_ensure_keys = auto_ensure_keys
        elif len(ensure_keys) == 0:
            active_ensure_keys = []
        else:
            active_ensure_keys = self.merge_ensure_keys(auto_ensure_keys, ensure_keys)
        try:
            if (
                active_ensure_keys
                or strict_output
                or self.get_declared_pydantic_output_model() is not None
                or bool(active_validate_handlers)
                or result._validate_outcome is not None
            ):
                await result.async_get_data(
                    ensure_keys=active_ensure_keys,
                    validate_handler=validate_handler,
                    key_style=key_style,
                    max_retries=max_retries,
                    _retry_count=0,
                    raise_ensure_failure=raise_ensure_failure,
                )
                if result._accepted_retry_result is not None:
                    return await self.await_materialization(
                        result._accepted_retry_result._response_parser.async_get_data_object(),
                        stage="response_materialization",
                    )
                return await self.await_materialization(
                    result._response_parser.async_get_data_object(),
                    stage="response_materialization",
                )
            return await self.await_materialization(
                result._response_parser.async_get_data_object(),
                stage="response_materialization",
            )
        finally:
            await result._drain_response_parser_observations()
            await result._run_finally_handlers_once()
