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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agently.types.data import ContextCandidate, ContextConsumer, ContextReadIntent


@dataclass(frozen=True)
class ContextSelection:
    """Model/selector response containing request-local offered keys only."""

    selected_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_keys", tuple(str(item) for item in self.selected_keys))


@runtime_checkable
class ContextSemanticSelector(Protocol):
    async def async_select(
        self,
        *,
        intent: ContextReadIntent,
        candidates: Sequence[ContextCandidate],
        consumer: ContextConsumer,
        phase: str,
    ) -> ContextSelection: ...


class ModelRequestContextSelector:
    """Structured semantic-selector adapter over an injected ModelRequest factory."""

    def __init__(self, request_factory: Callable[[], Any]) -> None:
        if not callable(request_factory):
            raise TypeError("request_factory must be callable.")
        self.request_factory = request_factory

    async def async_select(
        self,
        *,
        intent: ContextReadIntent,
        candidates: Sequence[ContextCandidate],
        consumer: ContextConsumer,
        phase: str,
    ) -> ContextSelection:
        request = self.request_factory()
        cards = [
            {
                "block_key": candidate.block_key,
                "role": candidate.role,
                "summary": candidate.summary,
                "completeness": candidate.completeness,
            }
            for candidate in candidates
        ]
        request_input: dict[str, Any] = {
            "intent": intent.query,
            "phase": str(phase),
        }
        task = intent.metadata.get("task")
        if isinstance(task, Mapping):
            request_input["task"] = dict(task)
        request.input(request_input)
        request_info: dict[str, Any] = {"offered_context_blocks": cards}
        guidance = intent.metadata.get("selection_guidance")
        if isinstance(guidance, Sequence) and not isinstance(guidance, (str, bytes)):
            projected_guidance = [
                {"content": item["content"], "completeness": item["completeness"]}
                for item in guidance
                if isinstance(item, Mapping)
                and "content" in item
                and item.get("completeness") in {"complete", "truncated", "lossy"}
            ]
            if projected_guidance:
                request_info["selection_guidance"] = projected_guidance
        request.info(request_info)
        instruction = (
            "Select only optional Context blocks that are semantically useful for the current "
            "[input.intent] and [input.phase]. Use [input.task] when present to interpret this scope, "
            "without expanding it to other task stages. "
            "Return them in descending task relevance, putting exact API, schema, "
            "integration-contract, or directly requested evidence before general background. "
            "Return only offered block_key values. Do not perform the downstream task."
        )
        if "selection_guidance" in request_info:
            instruction += (
                " Use [info.selection_guidance] to interpret resource-reading conditions for this "
                "intent and phase, not to perform the downstream task. Respect each item's "
                "completeness; missing text in an incomplete item is not evidence that no rule exists."
            )
        request.instruct(instruction)
        request.output(
            {
                "selected_keys": (
                    [str],
                    "Ordered subset of offered block_key values.",
                    True,
                ),
            },
            format="json",
        )
        result = await request.async_get_data()
        if not isinstance(result, Mapping):
            raise ValueError("Context selection output must contain selected_keys as a list of strings.")
        raw_keys = result.get("selected_keys")
        if not isinstance(raw_keys, list):
            raise ValueError("Context selection output selected_keys must be a list of strings.")
        if any(not isinstance(item, str) or not item.strip() for item in raw_keys):
            raise ValueError("Context selection output selected_keys must contain non-empty strings.")
        return ContextSelection(selected_keys=tuple(item.strip() for item in raw_keys))


__all__ = [
    "ContextSelection",
    "ContextSemanticSelector",
    "ModelRequestContextSelector",
]
