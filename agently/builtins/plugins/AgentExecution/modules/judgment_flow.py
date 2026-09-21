"""Judgment output production under the existing AgentExecution lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter

from agently.builtins.plugins.ModelRequester.Jev import jev_enabled, supports_template
from agently.core.orchestration import TriggerFlow
from agently.types.trigger_flow import TriggerFlowRuntimeData
from agently.utils import DataFormatter
from agently.utils.ModelPool import resolve_model_pool_settings

from .judgment_schema import JudgmentSchema, Path, has_judgment, merge_output, path_text
from .long_output import LongOutputDelivery, _set_path
from .model_stage import _record_action_logs
from .production import ProductionOptions
from .routes import finish_model_request_route

_RESOURCE = "judgment_output"


class _JudgmentOutput:
    def __init__(self, execution: Any, options: ProductionOptions):
        self.execution = execution
        self.options = options
        self.plan = JudgmentSchema(execution.request.prompt.get("output"))
        self.native_paths = {path for path, field in self.plan.fields.items() if supports_template(field.declaration)}
        self.native = bool(self.native_paths) and jev_enabled(
            execution.request.settings, path=path_text(next(iter(self.plan.fields))) or "output"
        )
        if not self.native and all(
            not field.sources and not field.prerequisites for field in self.plan.fields.values()
        ):
            self.plan.ordinary = self.plan.schema
            self.plan.stages = [None]
        if execution.request.prompt.to_prompt_object().output_format != "json":
            raise ValueError("Judgment output currently requires JSON output format.")
        self.batch_size = execution.request.settings.get("plugins.ModelRequester.Jev.batch_size", 64)
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int) or self.batch_size < 1:
            raise ValueError("Jev.batch_size must be a positive integer.")
        if isinstance(options.max_retries, bool) or not isinstance(options.max_retries, int) or options.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer.")
        self.meta: dict[str, Any] = {"native": self.native, "stages": [], "fields": {}, "retries_used": 0}
        self.execution._producer_state = {"kind": "judgment", "judgment": self.meta}
        if execution._ensure_long_output_enabled:
            raise ValueError("Judgment composition cannot currently combine with auto_continue.")

    def request(
        self,
        schema: Any,
        *,
        native: bool,
        targets: dict[str, Any] | None = None,
        evidence: Any = None,
        feedback: str | None = None,
    ):
        owner = self.execution
        request = owner.agent.create_request(inherit_agent_prompt=False, inherit_extension_handlers=not native)
        request.settings.update(deepcopy(owner.request.settings.get()))
        request._model_key = None
        if native:
            request.settings.set("plugins.ModelRequester.activate", "Jev")
        else:
            key = getattr(owner.request, "_model_key", None)
            if key:
                resolve_model_pool_settings(key, request.settings)
            if request.settings.get("plugins.ModelRequester.activate") == "Jev":
                raise ValueError(
                    "Mixed output needs an LLM ModelRequester in addition to the independent Jev settings."
                )
        provider = str(request.settings.get("plugins.ModelRequester.activate"))
        request.settings.set(f"plugins.ModelRequester.{provider}.request_retry", False)
        request.settings.set(f"plugins.ModelRequester.{provider}._api_key_pool_runtime", None)
        snapshot = deepcopy(dict(owner.request.prompt))
        for name in ("output", "output_format", "ensure_all_keys"):
            snapshot.pop(name, None)
        request.prompt.update(snapshot)
        request.output(schema, format="json")
        request.prompt.set("ensure_all_keys", True)
        request.extension_handlers.set("validate_handlers", None)
        if targets:
            if native:
                request.prompt.set("jev_bound_output", targets)
            else:
                request.prompt.append("info", {"judgment_targets": targets})
        if evidence:
            request.prompt.append("info", {"accepted_judgments": evidence})
        if feedback:
            request.prompt.append("info", {"output_validation_feedback": feedback})
        if not native:
            instruction = "Return only the declared output fields. Accepted judgments are read-only evidence."
            if targets:
                instruction += " For judgment_targets, evaluate each declared question on its matching target using the original context."
            if targets or has_judgment(schema):
                instruction += " Probabilities and scores you produce are model estimates; do not invent native probability distributions."
            request.prompt.append("instruct", {"judgment_output": instruction})
        return request

    def preflight(self) -> None:
        # Check provider construction/configuration without executing transport.
        schemas = [(self.plan.ordinary, False)] if None in self.plan.stages else []
        if self.plan.remaining:
            schemas.append(
                ({f"field_{index}": schema for index, schema in enumerate(self.plan.remaining.values())}, False)
            )
        if any(path not in self.native_paths for path in self.plan.fields):
            schemas.append(
                (
                    {
                        "value": next(
                            field.declaration
                            for path, field in self.plan.fields.items()
                            if path not in self.native_paths
                        )
                    },
                    False,
                )
            )
        if self.native_paths:
            schemas.append(
                (
                    {
                        "value": replace(
                            self.plan.fields[next(iter(self.native_paths))].declaration,
                            from_output=None,
                            after_output=None,
                        )
                    },
                    self.native,
                )
            )
        for schema, native in schemas:
            request = self.request(schema, native=native)
            name = str(request.settings.get("plugins.ModelRequester.activate"))
            plugin = request.plugin_manager.get_plugin("ModelRequester", name)
            instance = plugin(request.prompt, request.settings)
            if plugin.__module__.startswith("agently.builtins.plugins.ModelRequester."):
                data = instance.generate_request_data()
                if not native and urlsplit(data.request_url).hostname in {"api.openai.com", "api.anthropic.com"}:
                    config = request.settings.get(f"plugins.ModelRequester.{name}", {})
                    if isinstance(config, Mapping) and not (
                        config.get("api_key")
                        or config.get("auth")
                        or config.get("headers", {}).get("Authorization")
                        or config.get("headers", {}).get("x-api-key")
                    ):
                        raise ValueError(
                            "Mixed judgment output requires LLM credentials before Jev dispatch; configure the ordinary ModelRequester as well as Jev."
                        )

    async def dispatch(
        self, stage: int, batch: list[tuple[Path, Path]] | None, output: Any, feedback: str | None
    ) -> Any:
        owner = self.execution
        judgment_batch = batch is not None and all(path in self.plan.fields for path, _ in batch)
        native = (
            self.native and judgment_batch and batch is not None and all(path in self.native_paths for path, _ in batch)
        )
        targets: dict[str, Any] = {}
        schema: Any = self.plan.ordinary
        if batch is not None:
            schema = {}
            for index, (abstract, concrete) in enumerate(batch):
                name = f"field_{index}"
                if abstract not in self.plan.fields:
                    schema[name] = self.plan.remaining[abstract]
                    continue
                field = self.plan.fields[abstract]
                for source in field.prerequisites:
                    self.plan._bind_source(field, source, concrete, output)
                if field.sources:
                    targets[name] = self.plan.bind(field, concrete, output)
                declaration = replace(
                    field.declaration, from_output=name if field.sources and native else None, after_output=None
                )
                schema[name] = declaration if native else declaration.to_schema()
        request = self.request(
            schema, native=native, targets=targets, evidence=None if native else self.meta["fields"], feedback=feedback
        )
        if batch is not None and not judgment_batch:
            request.prompt.append(
                "info",
                {
                    "output_targets": {
                        f"field_{index}": path_text(concrete) for index, (_, concrete) in enumerate(batch)
                    },
                    "accepted_output": output,
                },
            )
            request.prompt.append(
                "instruct",
                "Generate each field for its original output_targets path, using accepted_output as fixed context. Do not regenerate containers or accepted fields.",
            )
        package = await owner.async_read_task_context(consumer_id=f"judgment:{owner.id}:{stage}", phase="judgment")
        for block in package.blocks:
            lane = "instruct" if block.role == "instruction" else "examples" if block.role == "example" else "info"
            request.prompt.append(
                lane,
                {
                    "task_context_blocks": [
                        {
                            "content": DataFormatter.sanitize(block.content),
                            "role": block.role,
                            "ref": block.source_ref,
                            "completeness": block.completeness,
                        }
                    ]
                },
            )
        await owner.emit_stream(
            "execution.stage.started", {"plugin": owner.name, "stage": f"judgment_{stage}"}, route="model_request"
        )
        provider = str(request.settings.get("plugins.ModelRequester.activate"))
        result = request.get_result(parent_run_context=owner.agent_execution_run_context)
        owner.record_model_response_id(result.id)
        record: dict[str, Any] = {"stage": stage, "provider": provider, "request_id": result.id, "status": "running"}
        self.meta["stages"].append(record)
        started = monotonic()
        try:
            value = await result.async_get_data(max_retries=0, raise_ensure_failure=True)
            if batch is not None:
                if not isinstance(value, dict) or set(value) != set(schema):
                    raise ValueError("Judgment stage returned incomplete or extra fields.")
                if judgment_batch:
                    for index, (abstract, _) in enumerate(batch):
                        declaration = self.plan.fields[abstract].declaration
                        TypeAdapter(declaration.to_schema()[0]).validate_python(value[f"field_{index}"])
            else:
                self.plan.validate_sources(merge_output(deepcopy(output), deepcopy(value)))
            await _record_action_logs(owner, result)
            record.update({"status": "completed", "meta": await result.async_get_meta()})
            raw = result.full_result_data.get("original_done") if native else None
            if raw is not None:
                record["jev"] = deepcopy(raw)
            if judgment_batch and batch is not None:
                for index, (abstract, concrete) in enumerate(batch):
                    field = self.plan.fields[abstract]
                    detail: dict[str, Any] = {
                        "question": field.declaration.question,
                        "value": value[f"field_{index}"],
                        "provider": provider,
                        "request_id": result.id,
                        "revision": owner.revision,
                    }
                    if isinstance(raw, Mapping):
                        detail["answer"] = deepcopy(raw["answers"][f"q{index}"])
                        detail["model"] = raw.get("model")
                    self.meta["fields"][path_text(concrete)] = detail
            await owner.emit_stream(
                "execution.stage.completed",
                {"plugin": owner.name, "stage": f"judgment_{stage}", "response_id": result.id},
                route="model_request",
            )
            return value
        except BaseException as error:
            record.update({"status": "failed", "error_type": type(error).__name__})
            raise
        finally:
            record["elapsed_seconds"] = monotonic() - started
            owner.record_context_consumption(package, request_id=str(result.response_id or result.id))


def _runtime(data: TriggerFlowRuntimeData) -> _JudgmentOutput:
    runtime = data.require_resource(_RESOURCE)
    if not isinstance(runtime, _JudgmentOutput):
        raise RuntimeError("Missing judgment output execution resource.")
    return runtime


async def _prepare(data: TriggerFlowRuntimeData) -> None:
    runtime = _runtime(data)
    stage = data.get_state("stage", 0)
    if stage >= len(runtime.plan.stages):
        await data.async_emit_nowait("ASSEMBLE", None)
        return
    paths = runtime.plan.stages[stage]
    if paths is None:
        batches = [None]
    else:
        output = data.get_state("output")
        expanded = [(path, concrete) for path in paths for concrete in runtime.plan.expand(path, output)]
        groups = [
            [item for item in expanded if runtime.native and item[0] in runtime.native_paths],
            [item for item in expanded if not (runtime.native and item[0] in runtime.native_paths)],
        ]
        batches = [
            group[index : index + runtime.batch_size]
            for group in groups
            for index in range(0, len(group), runtime.batch_size)
        ]
    await data.async_set_state("batches", batches, emit=False)
    await data.async_set_state("batch_index", 0, emit=False)
    await data.async_emit_nowait("REQUEST", None)


async def _request(data: TriggerFlowRuntimeData) -> None:
    runtime = _runtime(data)
    stage = data.get_state("stage", 0)
    batches = data.get_state("batches", [])
    index = data.get_state("batch_index", 0)
    if index >= len(batches):
        await data.async_set_state("stage", stage + 1, emit=False)
        await data.async_emit_nowait("PREPARE", None)
        return
    batch = batches[index]
    output = deepcopy(data.get_state("output"))
    try:
        value = await runtime.dispatch(stage, batch, output, data.get_state("feedback"))
    except Exception as error:
        retries = data.get_state("retries", 0)
        retryable = isinstance(error, (ValueError, httpx.RequestError, TimeoutError))
        if isinstance(error, httpx.HTTPStatusError):
            retryable = error.response.status_code in {408, 429} or error.response.status_code >= 500
        if not retryable or retries >= runtime.options.max_retries:
            raise
        await data.async_set_state("retries", retries + 1, emit=False)
        runtime.meta["retries_used"] = retries + 1
        await data.async_set_state("feedback", str(error)[:1500], emit=False)
        await data.async_emit_nowait("RETRY", None)
        return
    if batch is None:
        output = merge_output(output, value)
    else:
        for offset, (_, concrete) in enumerate(batch):
            if output is None and concrete:
                output = [] if isinstance(concrete[0], int) else {}
            output = _set_path(output, concrete, value[f"field_{offset}"])
    await data.async_set_state("output", output, emit=False)
    await data.async_set_state("feedback", None, emit=False)
    await data.async_set_state("batch_index", index + 1, emit=False)
    await data.async_emit_nowait("REQUEST", None)


async def _assemble(data: TriggerFlowRuntimeData) -> None:
    runtime = _runtime(data)
    output = data.get_state("output")
    runtime.plan.validate_values(output)
    owner = runtime.execution
    model = owner.request.prompt.to_output_model(strict_output=True)
    owner._producer_result_object = model.model_validate(output)
    if not runtime.native and not runtime.meta["fields"]:
        record = runtime.meta["stages"][-1]
        from agently.utils import DataLocator

        for field in runtime.plan.fields.values():
            for concrete in runtime.plan.expand(field.path, output):
                value = DataLocator.locate_path_in_dict(output, path_text(concrete)) if concrete else output
                runtime.meta["fields"][path_text(concrete)] = {
                    "question": field.declaration.question,
                    "value": value,
                    "provider": record["provider"],
                    "request_id": record["request_id"],
                    "revision": owner.revision,
                }
    delivery = LongOutputDelivery(
        owner,
        ensure_keys=runtime.options.ensure_keys,
        ensure_all_keys=True,
        validate_handler=None,
        key_style=runtime.options.key_style,
        max_retries=0,
        raise_ensure_failure=True,
    )
    delivery._validate_ensure_keys(output)
    await data.async_set_state("result", output, emit=False)


@lru_cache(maxsize=1)
def _flow() -> TriggerFlow:
    flow = TriggerFlow(name="agent-execution-judgment-output")
    flow.to(_prepare)
    flow.when("PREPARE").to(_prepare)
    flow.when("REQUEST").to(_request)
    flow.when("RETRY").to(_request)
    flow.when("ASSEMBLE").to(_assemble)
    return flow


async def run_judgment_output(execution: Any, options: ProductionOptions) -> Any:
    runtime = _JudgmentOutput(execution, options)
    runtime.preflight()
    flow = _flow().create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={_RESOURCE: runtime},
        parent_run_context=execution.agent_execution_run_context,
        intervention_mode=None,
    )
    try:
        await flow.async_start(None)
        snapshot = await flow.async_close(reason="judgment_completed")
    except BaseException:
        if not flow.is_closed():
            with suppress(BaseException):
                await flow.async_close(reason="judgment_failed", pending_interrupts="cancel")
        raise
    if not isinstance(snapshot, Mapping) or "result" not in snapshot:
        raise RuntimeError("Judgment flow did not produce a complete output.")
    return await finish_model_request_route(execution, snapshot["result"])
