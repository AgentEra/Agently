import hashlib
import json
import sys
import warnings
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Annotated, Any

import pytest
from agently_stage import Stage
from pydantic import BaseModel, Field

from agently import Agently
from agently.core import PluginManager, TaskWorkspace
from agently.types.data import (
    AgentlyRequestData,
    ChatMessageDict,
    OutputValidateResultDict,
)
from agently.utils import Settings


class MockAgentExecutionCompatibilityRequester:
    name = "MockAgentExecutionCompatibilityRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    attempts = 0
    requests: list[dict[str, Any]] = []

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @classmethod
    def reset(cls):
        cls.attempts = 0
        cls.requests = []

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        type(self).attempts += 1
        chat_history = self.prompt.get("chat_history", []) or []
        output_prompt = self.prompt.get("output", {}) or {}
        output_keys = list(output_prompt) if isinstance(output_prompt, dict) else []
        payload = {
            "attempt": type(self).attempts,
            "input": self.prompt.get("input"),
            "system": self.prompt.get("system"),
            "chat_history_count": len(chat_history) if isinstance(chat_history, list) else 1,
            "output_key": output_keys[0] if output_keys else "reply",
        }
        type(self).requests.append(payload)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data=payload,
            request_options={"stream": True},
            request_url="mock://agent-execution-compatibility",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        yield "message", json.dumps(
            {
                request_data.data["output_key"]: (
                    f"attempt={ request_data.data['attempt'] }; "
                    f"input={ request_data.data['input'] }; "
                    f"history={ request_data.data['chat_history_count'] }"
                )
            },
            ensure_ascii=False,
        )

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "done", response_text
        yield "meta", {
            "provider": "mock-agent-execution-compatibility",
            "status": "completed",
            "finish_reason": "stop",
        }


class MockAgentExecutionSpecificStreamRequester(MockAgentExecutionCompatibilityRequester):
    name = "MockAgentExecutionSpecificStreamRequester"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        async for _event, _data in response_generator:
            pass
        yield "reasoning_delta", "thinking"
        yield "delta", "answer"
        yield "tool_calls", [{"id": "call-1", "name": "lookup_policy"}]
        yield "done", "answer"
        yield "meta", {"provider": "mock-agent-execution-specific-stream"}


class MockAgentExecutionOriginalDeltaRequester(MockAgentExecutionCompatibilityRequester):
    name = "MockAgentExecutionOriginalDeltaRequester"

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        async for _event, _data in response_generator:
            pass
        yield "original_delta", '{"provider":"raw"}'
        yield "delta", "answer"
        yield "done", "answer"


class MockAgentExecutionLongOutputRequester(MockAgentExecutionCompatibilityRequester):
    name = "MockAgentExecutionLongOutputRequester"
    initial_text = "alpha-"

    def generate_request_data(self):
        type(self).attempts += 1
        continuation_input = self.prompt.get("input")
        payload = {
            "attempt": type(self).attempts,
            "tools": self.prompt.get("tools"),
            "input_keys": (
                list(continuation_input)
                if isinstance(continuation_input, dict)
                else []
            ),
            "instruct": self.prompt.get("instruct"),
            "continuation": (
                continuation_input.get("long_output_continuation")
                if isinstance(continuation_input, dict)
                else None
            ),
        }
        type(self).requests.append(payload)
        return AgentlyRequestData(
            client_options={},
            headers={},
            data=payload,
            request_options={"stream": True},
            request_url="mock://agent-execution-long-output",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slots = continuation["assembly_slots"]
        if slots[0]["path_key"] == "$text":
            updates = [
                {
                    "path_key": "$text",
                    "operation": "append_text",
                    "unit_index": slots[0]["next_unit_index"],
                    "value": "omega",
                }
            ]
        else:
            updates = [
                {
                    "path_key": slots[0]["path_key"],
                    "operation": "append_item",
                    "unit_index": slots[0]["next_unit_index"],
                    "value": {"name": "b"},
                },
                {
                    "path_key": slots[0]["path_key"],
                    "operation": "append_item",
                    "unit_index": slots[0]["next_unit_index"] + 1,
                    "value": {"name": "c"},
                },
            ]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": "all requested output units are complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "delta", response_text
        yield "done", response_text
        if type(self).attempts == 1:
            yield "meta", {
                "provider": "mock-agent-execution-long-output",
                "status": "incomplete",
                "finish_reason": "length",
                "incomplete_details": {"reason": "max_output_tokens"},
            }
        else:
            yield "meta", {
                "provider": "mock-agent-execution-long-output",
                "status": "completed",
                "finish_reason": "stop",
            }


class MockAgentExecutionStructuredLongOutputRequester(MockAgentExecutionLongOutputRequester):
    name = "MockAgentExecutionStructuredLongOutputRequester"
    initial_text = '{"components":[{"name":"a"},{"name":"b"'


class ExactListItem(BaseModel):
    name: str


class ExactThreeItemOutput(BaseModel):
    items: Annotated[
        list[ExactListItem],
        Field(min_length=3, max_length=3),
    ]


class ExactThreeItemWithSummaryOutput(ExactThreeItemOutput):
    summary: str


class NestedBoundItem(BaseModel):
    name: str
    sinks: Annotated[
        list[str],
        Field(min_length=1, max_length=2),
    ]


class ExactTwoNestedBoundOutput(BaseModel):
    items: Annotated[
        list[NestedBoundItem],
        Field(min_length=2, max_length=2),
    ]


class MockAgentExecutionExactListLimitRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionExactListLimitRequester"
    initial_text = '{"items":[{"name":"a"},{"name":"open"'

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slots = continuation["assembly_slots"]
        if type(self).attempts == 2:
            slot = slots[0]
            next_index = slot["next_unit_index"]
            updates = [
                {
                    "path_key": slot["path_key"],
                    "operation": "append_item",
                    "unit_index": next_index + offset,
                    "value": {"name": name},
                }
                for offset, name in enumerate(("b", "c", "must-not-commit"))
            ]
        else:
            updates = []
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": "exactly three items are complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionNestedConstraintRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionNestedConstraintRequester"
    initial_text = (
        '{"items":[{"name":"must-not-commit","sinks":[]},'
        '{"name":"open"'
    )

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        next_index = slot["next_unit_index"]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_item",
                        "unit_index": next_index,
                        "value": {"name": "a", "sinks": ["x"]},
                    },
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_item",
                        "unit_index": next_index + 1,
                        "value": {"name": "b", "sinks": ["y", "z"]},
                    },
                ],
                "state_summary": "both valid items are complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionOrderedExactListRequester(
    MockAgentExecutionExactListLimitRequester
):
    name = "MockAgentExecutionOrderedExactListRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slots = continuation["assembly_slots"]
        if type(self).attempts == 2:
            slot = slots[0]
            updates = [
                {
                    "path_key": slot["path_key"],
                    "operation": "append_item",
                    "unit_index": slot["next_unit_index"] + offset,
                    "value": {"name": name},
                }
                for offset, name in enumerate(("b", "c"))
            ]
            is_final = False
        else:
            slot = slots[0]
            updates = [
                {
                    "path_key": slot["path_key"],
                    "operation": "append_text",
                    "unit_index": slot["next_unit_index"],
                    "value": "three ordered items",
                }
            ]
            is_final = True
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": "exact items precede their summary",
                "is_final": is_final,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionRootListLongOutputRequester(MockAgentExecutionLongOutputRequester):
    name = "MockAgentExecutionRootListLongOutputRequester"
    initial_text = '[{"name":"a"},{"name":"b"'


class MockAgentExecutionInvalidUnitLongOutputRequester(
    MockAgentExecutionStructuredLongOutputRequester
):
    name = "MockAgentExecutionInvalidUnitLongOutputRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        next_index = slot["next_unit_index"]
        if type(self).attempts == 2:
            updates = [
                {
                    "path_key": slot["path_key"],
                    "operation": "append_item",
                    "unit_index": next_index,
                    "value": {"name": "b"},
                },
                {
                    "path_key": slot["path_key"],
                    "operation": "append_item",
                    "unit_index": next_index + 1,
                    "value": {"name": 2},
                },
                {
                    "path_key": slot["path_key"],
                    "operation": "append_item",
                    "unit_index": next_index + 2,
                    "value": {"name": "must-not-skip-invalid-prefix"},
                },
            ]
        else:
            updates = [
                {
                    "path_key": slot["path_key"],
                    "operation": "append_item",
                    "unit_index": next_index,
                    "value": {"name": "c"},
                }
            ]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": "invalid tails must be regenerated",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionFinalValidationRepairRequester(
    MockAgentExecutionStructuredLongOutputRequester
):
    name = "MockAgentExecutionFinalValidationRepairRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slots = continuation["assembly_slots"]
        if type(self).attempts == 2:
            component_slot = slots[0]
            updates = [
                {
                    "path_key": component_slot["path_key"],
                    "operation": "append_item",
                    "unit_index": component_slot["next_unit_index"],
                    "value": {"name": "b"},
                },
                {
                    "path_key": component_slot["path_key"],
                    "operation": "append_item",
                    "unit_index": component_slot["next_unit_index"] + 1,
                    "value": {"name": "c"},
                },
            ]
        else:
            summary_slot = slots[1]
            updates = [
                {
                    "path_key": summary_slot["path_key"],
                    "operation": "append_text",
                    "unit_index": summary_slot["next_unit_index"],
                    "value": "three components generated",
                }
            ]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": "all declared fields are now complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionUnrepairableFinalRequester(
    MockAgentExecutionFinalValidationRepairRequester
):
    name = "MockAgentExecutionUnrepairableFinalRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        if type(self).attempts <= 2:
            async for event in super().request_model(request_data):
                yield event
            return
        continuation = request_data.data["continuation"]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [],
                "state_summary": "incorrectly claiming completion",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionMultiSegmentLongOutputRequester(MockAgentExecutionLongOutputRequester):
    name = "MockAgentExecutionMultiSegmentLongOutputRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        update = {
            "path_key": "$text",
            "operation": "append_text",
            "unit_index": slot["next_unit_index"],
            "value": "beta-" if type(self).attempts == 2 else "gamma",
        }
        envelope = {
            "base_revision": continuation["base_revision"],
            "base_digest": continuation["base_digest"],
            "anchor": continuation["anchor"],
            "updates": [update],
            "state_summary": "one more block remains",
            "is_final": type(self).attempts >= 3,
        }
        text = json.dumps(envelope, ensure_ascii=False)
        if type(self).attempts == 2:
            text = (
                text[:-2]
                + ',{"path_key":"$text","operation":"append_text",'
                '"unit_index":2,"value":"open'
            )
        yield "message", text

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "delta", response_text
        yield "done", response_text
        yield "meta", {
            "provider": "mock-agent-execution-long-output",
            "status": "incomplete" if type(self).attempts <= 2 else "completed",
            "finish_reason": "length" if type(self).attempts <= 2 else "stop",
            "incomplete_details": (
                {"reason": "max_output_tokens"}
                if type(self).attempts <= 2
                else None
            ),
        }


class MockAgentExecutionMultiUpdateTextRequester(MockAgentExecutionLongOutputRequester):
    name = "MockAgentExecutionMultiUpdateTextRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        next_index = slot["next_unit_index"]
        updates = [
            {
                "path_key": "$text",
                "operation": "append_text",
                "unit_index": next_index,
                "value": "beta-" if type(self).attempts == 2 else "gamma",
            }
        ]
        if type(self).attempts == 2:
            updates.append(
                {
                    "path_key": "$text",
                    "operation": "append_text",
                    "unit_index": next_index + 1,
                    "value": "gamma",
                }
            )
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": "the final text block remains",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionStructuredContinuationLengthRequester(
    MockAgentExecutionStructuredLongOutputRequester
):
    name = "MockAgentExecutionStructuredContinuationLengthRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        next_index = slot["next_unit_index"]
        if type(self).attempts == 2:
            yield "message", json.dumps(
                {
                    "base_revision": continuation["base_revision"],
                    "base_digest": continuation["base_digest"],
                    "anchor": continuation["anchor"],
                    "updates": [
                        {
                            "path_key": slot["path_key"],
                            "operation": "append_item",
                            "unit_index": next_index,
                            "value": {"name": "b"},
                        }
                    ],
                    "state_summary": "one component remains",
                },
                ensure_ascii=False,
            )[:-1] + ',"is_final":'
            return
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_item",
                        "unit_index": next_index,
                        "value": {"name": "c"},
                    }
                ],
                "state_summary": "all components are complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "delta", response_text
        yield "done", response_text
        yield "meta", {
            "provider": "mock-agent-execution-long-output",
            "status": "incomplete" if type(self).attempts <= 2 else "completed",
            "finish_reason": "length" if type(self).attempts <= 2 else "stop",
            "incomplete_details": (
                {"reason": "max_output_tokens"}
                if type(self).attempts <= 2
                else None
            ),
        }


class MockAgentExecutionLargeStructuredContinuationLengthRequester(
    MockAgentExecutionStructuredContinuationLengthRequester
):
    name = "MockAgentExecutionLargeStructuredContinuationLengthRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        if type(self).attempts != 2:
            async for event, data in super().request_model(request_data):
                yield event, data
            return
        slot = continuation["assembly_slots"][0]
        envelope_prefix = json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_item",
                        "unit_index": slot["next_unit_index"],
                        "value": {"name": "b" * 1500},
                    }
                ],
            },
            ensure_ascii=False,
        )
        yield "message", (
            envelope_prefix[:-2]
            + ',{"path_key":"p0","operation":"append_item",'
            '"unit_index":2,"value":{"name":"open'
        )


class MockAgentExecutionIncompleteHeaderRecoveryRequester(
    MockAgentExecutionStructuredContinuationLengthRequester
):
    name = "MockAgentExecutionIncompleteHeaderRecoveryRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        if type(self).attempts == 2:
            yield "message", '{"base_revision":'
            return
        slot = continuation["assembly_slots"][0]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_item",
                        "unit_index": slot["next_unit_index"],
                        "value": {"name": "b"},
                    }
                ],
                "state_summary": "header recovery completed",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionIncompleteHeaderNoProgressRequester(
    MockAgentExecutionStructuredContinuationLengthRequester
):
    name = "MockAgentExecutionIncompleteHeaderNoProgressRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        yield "message", '{"base_revision":'

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, Any], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
        yield "delta", response_text
        yield "done", response_text
        yield "meta", {
            "provider": "mock-agent-execution-long-output",
            "status": "incomplete",
            "finish_reason": "length",
            "incomplete_details": {"reason": "max_output_tokens"},
        }


class MockAgentExecutionNestedSlotRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionNestedSlotRequester"
    initial_text = (
        '{"groups":[{"title":"g1","options":'
        '[{"name":"a","enabled":true}]},{"title":"open"'
    )

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_item",
                        "unit_index": slot["next_unit_index"],
                        "value": {
                            "title": "g2",
                            "options": [{"name": "b", "enabled": False}],
                        },
                    }
                ],
                "state_summary": "nested groups complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionStructuredTextSlotRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionStructuredTextSlotRequester"
    initial_text = '{"title":"alpha","components":[{"name":"open"'

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = _slot_for_label(continuation, "components")
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_item",
                        "unit_index": slot["next_unit_index"],
                        "value": {"name": "a"},
                    }
                ],
                "state_summary": "structured text and list are complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionRepeatedStructuredTextRequester(
    MockAgentExecutionStructuredTextSlotRequester
):
    name = "MockAgentExecutionRepeatedStructuredTextRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        if type(self).attempts == 2:
            updates = [
                {
                    "path_key": "p0:title",
                    "operation": "append_text",
                    "unit_index": 1,
                    "value": "-must-not-append",
                }
            ]
        else:
            slot = _slot_for_label(continuation, "components")
            updates = [
                {
                    "path_key": slot["path_key"],
                    "operation": "append_item",
                    "unit_index": slot["next_unit_index"],
                    "value": {"name": "a"},
                }
            ]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": "completed text must remain immutable",
                "is_final": type(self).attempts >= 3,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionExplicitEmptyListRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionExplicitEmptyListRequester"
    initial_text = '{"items":[],"tail":"open'

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = _slot_for_label(continuation, "tail")
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "append_text",
                        "unit_index": slot["next_unit_index"],
                        "value": "done",
                    }
                ],
                "state_summary": "empty items and tail are complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionContinuationEmptyListRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionContinuationEmptyListRequester"
    initial_text = '{"head":"alpha","items":'

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = _slot_for_label(continuation, "items")
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "declare_empty_list",
                        "unit_index": 0,
                        "value": [],
                    }
                ],
                "state_summary": "items is intentionally empty",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionStaleEmptyListRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionStaleEmptyListRequester"
    initial_text = '{"items":[{"name":"a"}],"tail":"open'

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = _slot_for_label(continuation, "items")
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": slot["path_key"],
                        "operation": "declare_empty_list",
                        "unit_index": 0,
                        "value": [],
                    }
                ],
                "state_summary": "invalid empty reset",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionRequiredSlotBarrierRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionRequiredSlotBarrierRequester"
    initial_text = '{"head":"alpha","summary":'

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        path = (
            "summary"
            if type(self).attempts == 2
            else "direct_answer"
        )
        slot = _slot_for_label(continuation, path)
        update = (
            {
                "path_key": slot["path_key"],
                "operation": "append_text",
                "unit_index": slot["next_unit_index"],
                "value": "ready",
            }
            if path == "summary"
            else {
                "path_key": slot["path_key"],
                "operation": "declare_empty_text",
                "unit_index": 0,
                "value": "",
            }
        )
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [update],
                "state_summary": "all required paths are complete",
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionManySegmentLongOutputRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionManySegmentLongOutputRequester"
    stack_depths: list[int] = []

    @classmethod
    def reset(cls):
        super().reset()
        cls.stack_depths = []

    @staticmethod
    def _stack_depth() -> int:
        depth = 0
        frame = sys._getframe()
        while frame is not None:
            depth += 1
            frame = frame.f_back
        return depth

    def generate_request_data(self):
        type(self).stack_depths.append(self._stack_depth())
        return super().generate_request_data()

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": "$text",
                        "operation": "append_text",
                        "unit_index": slot["next_unit_index"],
                        "value": f"segment-{type(self).attempts};",
                    }
                ],
                "state_summary": "continue until the declared final segment",
                "is_final": type(self).attempts >= 13,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionNoProgressLongOutputRequester(MockAgentExecutionLongOutputRequester):
    name = "MockAgentExecutionNoProgressLongOutputRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": [],
                "state_summary": "no progress",
                # A terminal assertion without a durable update must not be
                # trusted as proof that the truncated result is complete.
                "is_final": True,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionInvalidCompleteEnvelopeRequester(
    MockAgentExecutionLongOutputRequester
):
    name = "MockAgentExecutionInvalidCompleteEnvelopeRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        if type(self).attempts in {2, 3}:
            yield "message", '{"not":"a continuation envelope"}'
            return
        async for event, data in super().request_model(request_data):
            yield event, data


class MockAgentExecutionLargeStructuredLongOutputRequester(
    MockAgentExecutionStructuredLongOutputRequester
):
    name = "MockAgentExecutionLargeStructuredLongOutputRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        slot = continuation["assembly_slots"][0]
        start = slot["next_unit_index"]
        stop = 36 if type(self).attempts == 2 else 75
        updates = [
            {
                "path_key": slot["path_key"],
                "operation": "append_item",
                "unit_index": index,
                "value": {"name": f"c{index:02d}"},
            }
            for index in range(start, stop)
        ]
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": continuation["base_digest"],
                "anchor": continuation["anchor"],
                "updates": updates,
                "state_summary": f"generated through component {stop - 1}",
                "is_final": stop == 75,
            },
            ensure_ascii=False,
        )


class MockAgentExecutionStaleLongOutputRequester(MockAgentExecutionLongOutputRequester):
    name = "MockAgentExecutionStaleLongOutputRequester"

    async def request_model(self, request_data: AgentlyRequestData):
        continuation = request_data.data.get("continuation")
        if not isinstance(continuation, dict):
            yield "message", self.initial_text
            return
        yield "message", json.dumps(
            {
                "base_revision": continuation["base_revision"],
                "base_digest": "0" * 64,
                "anchor": continuation["anchor"],
                "updates": [
                    {
                        "path_key": "$text",
                        "operation": "append_text",
                        "unit_index": 1,
                        "value": "must-not-commit",
                    }
                ],
                "state_summary": "",
                "is_final": True,
            },
            ensure_ascii=False,
        )


def _create_test_agent(name: str = "agent-execution-compatibility"):
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", MockAgentExecutionCompatibilityRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


def _create_specific_stream_test_agent(name: str = "agent-execution-specific-stream"):
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", MockAgentExecutionSpecificStreamRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


def _create_original_delta_test_agent(name: str = "agent-execution-original-delta"):
    settings = Settings(name=f"{ name }-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{ name }-PluginManager")
    plugin_manager.register("ModelRequester", MockAgentExecutionOriginalDeltaRequester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


def _create_long_output_test_agent(
    requester: type[MockAgentExecutionLongOutputRequester],
    name: str,
):
    settings = Settings(name=f"{name}-Settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name=f"{name}-PluginManager",
    )
    plugin_manager.register("ModelRequester", requester, activate=True)
    return Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name=name,
    )


def _get_long_output_meta(execution) -> dict[str, Any]:
    long_output = execution.get_meta().get("long_output")
    assert isinstance(long_output, dict)
    return long_output


def _slot_for_label(
    continuation: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    suffix = f":{label}"
    return next(
        item
        for item in continuation["assembly_slots"]
        if item["path_key"].endswith(suffix)
    )


def test_agent_quick_prompt_start_creates_isolated_execution_each_turn():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("quick-prompt-isolation")
    agent.system("Reply briefly.", always=True)

    replies = [
        agent.input(f"turn-{index}").output({"reply": (str,)}, format="json").start()["reply"]
        for index in range(3)
    ]

    assert replies == [
        "attempt=1; input=turn-0; history=0",
        "attempt=2; input=turn-1; history=0",
        "attempt=3; input=turn-2; history=0",
    ]
    assert [request["input"] for request in MockAgentExecutionCompatibilityRequester.requests] == [
        "turn-0",
        "turn-1",
        "turn-2",
    ]


def test_completed_agent_execution_reconfiguration_requires_new_execution():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("completed-execution-one-run")
    agent.system("Reply briefly.", always=True)

    execution = agent.input("first").output({"reply": (str,)}, format="json")
    first = execution.start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    with pytest.raises(RuntimeError, match="one independent run"):
        execution.input("second")

    second = agent.input("second").output({"reply": (str,)}, format="json").start()["reply"]
    assert second == "attempt=2; input=second; history=0"
    assert [request["input"] for request in MockAgentExecutionCompatibilityRequester.requests] == [
        "first",
        "second",
    ]


def test_agent_execution_ensure_long_output_is_opt_in_and_fluent():
    agent = _create_test_agent("ensure-long-output-policy")
    execution = agent.input("long answer")

    assert execution._ensure_long_output_enabled is False
    assert execution.ensure_long_output() is execution
    assert execution._ensure_long_output_enabled is True
    assert execution.ensure_long_output(False) is execution
    assert execution._ensure_long_output_enabled is False


def test_agent_execution_ensure_long_output_rejects_reconfiguration_after_start():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("ensure-long-output-lifecycle")
    execution = (
        agent.input("long answer")
        .output({"reply": (str,)}, format="json")
        .ensure_long_output()
    )

    execution.start()

    with pytest.raises(RuntimeError, match="one independent run"):
        execution.ensure_long_output(False)


def test_agent_execution_ensure_long_output_short_path_reports_guarantee_level():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("ensure-long-output-short-meta")
    execution = (
        agent.input("short answer")
        .output({"reply": (str,)}, format="json")
        .validate(lambda value, _context: bool(value["reply"]))
        .ensure_long_output()
    )

    result = execution.get_data()
    long_output = _get_long_output_meta(execution)

    assert result["reply"].startswith("attempt=1")
    assert MockAgentExecutionCompatibilityRequester.attempts == 1
    assert long_output["declared_coverage_complete"] is True
    assert long_output["semantic_exhaustiveness"] == "not_claimed"
    assert long_output["guarantee_level"] == "single_request_with_declared_coverage"


def test_agent_execution_ensure_long_output_continues_plain_text_losslessly(tmp_path):
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester,
        "ensure-long-output-plain",
    ).use_task_workspace(tmp_path)

    execution = (
        agent.input("write a long document")
        .ensure_long_output()
    )
    result = execution.get_text()
    long_output = _get_long_output_meta(execution)

    assert result == "alpha-omega"
    assert MockAgentExecutionLongOutputRequester.attempts == 2
    assert long_output["transport_complete"] is True
    assert long_output["replayed_unit_count"] == 2
    assert long_output["manifest_ref"]["sha256"]
    assert long_output["final_digest"]
    assert long_output["final_ref_retention"] == "execution_private_staging"


@pytest.mark.asyncio
async def test_agent_execution_long_output_settles_stage_owned_continuation_tasks(
    tmp_path,
    monkeypatch,
):
    stage_origins: dict[int, list[str]] = {}
    closed_snapshots: dict[int, Any] = {}
    original_create_task = Stage.create_task
    original_async_close = Stage.async_close

    def record_create_task(
        self,
        coroutine,
        *,
        origin,
        name=None,
    ):
        stage_origins.setdefault(id(self), []).append(origin)
        return original_create_task(
            self,
            coroutine,
            origin=origin,
            name=name,
        )

    async def record_async_close(self, timeout=None):
        await original_async_close(self, timeout=timeout)
        closed_snapshots[id(self)] = self.snapshot()

    monkeypatch.setattr(Stage, "create_task", record_create_task)
    monkeypatch.setattr(Stage, "async_close", record_async_close)
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester,
        "ensure-long-output-stage-settlement",
    ).use_task_workspace(tmp_path)

    execution = agent.input("write a long document").ensure_long_output()
    result = await execution.async_get_text()

    continuation_stage_ids = [
        stage_id
        for stage_id, origins in stage_origins.items()
        if "emit_nowait:event:VALIDATE" in origins
    ]
    assert result == "alpha-omega"
    assert len(continuation_stage_ids) == 1
    continuation_stage_id = continuation_stage_ids[0]
    assert any(
        origin.startswith("handler:")
        for origin in stage_origins[continuation_stage_id]
    )
    assert continuation_stage_id in closed_snapshots
    snapshot = closed_snapshots[continuation_stage_id]
    assert snapshot.state == "closed"
    assert snapshot.active_count == 0
    assert snapshot.pending_root_count == 0
    assert snapshot.unresolved_origins == ()


def test_agent_execution_ensure_long_output_separates_text_anchor_from_continuity_context(
    tmp_path,
    monkeypatch,
):
    initial_text = "alpha-" + ("中" * 2300)
    monkeypatch.setattr(
        MockAgentExecutionLongOutputRequester,
        "initial_text",
        initial_text,
    )
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester,
        "ensure-long-output-text-anchor",
    ).use_task_workspace(tmp_path)

    result = (
        agent.input("write the complete document")
        .ensure_long_output()
        .get_text()
    )

    assert result == initial_text + "omega"
    continuation = MockAgentExecutionLongOutputRequester.requests[1][
        "continuation"
    ]
    assert continuation["anchor"] == hashlib.sha256(
        initial_text.encode("utf-8")
    ).hexdigest()
    assert continuation["continuity_context"] == {
        "document_start": initial_text[:1000],
        "accepted_tail": initial_text[-2000:],
        "accepted_character_count": len(initial_text),
    }
    assert continuation["assembly_slots"] == [
        {
            "path_key": "$text",
            "operation": "append_text",
            "next_unit_index": 1,
            "is_set": True,
            "value_contract": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
            },
        }
    ]


def test_agent_execution_ensure_long_output_stream_hides_private_envelopes(tmp_path):
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester,
        "ensure-long-output-stream",
    ).use_task_workspace(tmp_path)
    execution = agent.input("write a long document").ensure_long_output()

    deltas = list(execution.get_generator(type="delta"))

    assert "".join(deltas) == "alpha-omega"
    assert all("base_digest" not in delta for delta in deltas)
    assert execution.get_text() == "alpha-omega"


def test_agent_execution_ensure_long_output_preserves_complete_structured_units(tmp_path):
    MockAgentExecutionStructuredLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionStructuredLongOutputRequester,
        "ensure-long-output-structured",
    ).use_task_workspace(tmp_path)

    execution = (
        agent.input("generate every component")
        .output(
            {
                "components": [
                    {
                        "name": (str, "component name"),
                    }
                ]
            },
            format="json",
        )
        .ensure_long_output()
    )
    result = execution.get_data()
    result_object = execution.get_data_object()
    long_output = _get_long_output_meta(execution)

    assert result == {
        "components": [
            {"name": "a"},
            {"name": "b"},
            {"name": "c"},
        ]
    }
    assert result_object is not None
    assert result_object.model_dump() == result
    assert MockAgentExecutionStructuredLongOutputRequester.attempts == 2
    assert long_output["accepted_unit_count"] == 3
    assert long_output["replayed_unit_count"] == 3
    assert long_output["schema_complete"] is True


def test_agent_execution_ensure_long_output_validates_only_final_assembled_value(tmp_path):
    MockAgentExecutionStructuredLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionStructuredLongOutputRequester,
        "ensure-long-output-final-validator",
    ).use_task_workspace(tmp_path)
    seen: list[dict[str, Any]] = []

    def require_three_components(value, _context) -> bool:
        seen.append(value)
        return len(value["components"]) == 3

    result = (
        agent.input("generate exactly three components")
        .output({"components": [{"name": (str,)}]}, format="json")
        .validate(require_three_components)
        .ensure_long_output()
        .get_data()
    )

    assert len(result["components"]) == 3
    assert seen == [result]


def test_agent_execution_ensure_long_output_replays_root_list(tmp_path):
    MockAgentExecutionRootListLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionRootListLongOutputRequester,
        "ensure-long-output-root-list",
    ).use_task_workspace(tmp_path)

    execution = (
        agent.input("generate every component")
        .output([{"name": (str, "component name", True)}], format="json")
        .ensure_long_output()
    )
    result = execution.get_data()
    result_object = execution.get_data_object()

    assert result == [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    assert result_object is not None
    assert result_object.model_dump()["list"] == result
    assert _get_long_output_meta(execution)["replayed_unit_count"] == 3


def test_agent_execution_ensure_long_output_keeps_valid_prefix_and_regenerates_invalid_unit(
    tmp_path,
):
    MockAgentExecutionInvalidUnitLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionInvalidUnitLongOutputRequester,
        "ensure-long-output-invalid-unit",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("generate every component")
        .output({"components": [{"name": (str, "component name", True)}]}, format="json")
        .ensure_long_output()
    )

    result = execution.get_data()

    assert result == {
        "components": [
            {"name": "a"},
            {"name": "b"},
            {"name": "c"},
        ]
    }
    assert MockAgentExecutionInvalidUnitLongOutputRequester.attempts == 3
    rejected = execution.diagnostics["long_output_rejected_updates"]
    assert rejected[0]["accepted_prefix_count"] == 1
    assert "declared output schema" in rejected[0]["reason"]
    repaired_request = MockAgentExecutionInvalidUnitLongOutputRequester.requests[2]
    repair_feedback = repaired_request["continuation"]["repair_feedback"]
    assert repair_feedback["accepted_prefix_count"] == 1
    assert "next_unit_index" in repair_feedback["action"]
    value_contract = repaired_request["continuation"]["assembly_slots"][0][
        "value_contract"
    ]
    assert value_contract["type"] == "object"
    assert value_contract["required"] == ["name"]
    assert value_contract["properties"]["name"]["type"] == "string"
    assert '"name"' in json.dumps(value_contract)
    assert _get_long_output_meta(execution)["accepted_unit_count"] == 3


def test_agent_execution_ensure_long_output_enforces_constrained_list_bounds(
    tmp_path,
):
    MockAgentExecutionExactListLimitRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionExactListLimitRequester,
        "ensure-long-output-exact-list-limit",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("generate exactly three items")
        .output(ExactThreeItemOutput, format="json")
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    }
    assert MockAgentExecutionExactListLimitRequester.attempts == 3
    first_continuation = MockAgentExecutionExactListLimitRequester.requests[1][
        "continuation"
    ]
    assert first_continuation["assembly_slots"][0]["min_items"] == 3
    assert first_continuation["assembly_slots"][0]["max_items"] == 3
    assert MockAgentExecutionExactListLimitRequester.requests[2][
        "continuation"
    ]["assembly_slots"] == []
    rejected = execution.diagnostics["long_output_rejected_updates"]
    assert rejected[0]["accepted_prefix_count"] == 2
    assert "maximum item count 3" in rejected[0]["reason"]


def test_agent_execution_ensure_long_output_rejects_nested_constraint_violation_before_manifest(
    tmp_path,
):
    MockAgentExecutionNestedConstraintRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionNestedConstraintRequester,
        "ensure-long-output-nested-constraint",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("generate exactly two valid connected items")
        .output(ExactTwoNestedBoundOutput, format="json")
        .ensure_long_output()
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert execution.get_data() == {
            "items": [
                {"name": "a", "sinks": ["x"]},
                {"name": "b", "sinks": ["y", "z"]},
            ]
        }

    assert MockAgentExecutionNestedConstraintRequester.attempts == 2
    first_continuation = MockAgentExecutionNestedConstraintRequester.requests[
        1
    ]["continuation"]
    assert first_continuation["assembly_slots"][0]["next_unit_index"] == 0
    rejected = execution.diagnostics["long_output_rejected_updates"]
    assert rejected[0]["accepted_prefix_count"] == 0
    assert "declared output schema" in rejected[0]["reason"]
    assert "at least 1 item" in rejected[0]["reason"]
    long_output_meta = _get_long_output_meta(execution)
    assert long_output_meta["accepted_unit_count"] == 2
    assert long_output_meta["replayed_unit_count"] == 2
    assert not [
        item
        for item in caught
        if "PydanticSerializationUnexpectedValue" in str(item.message)
    ]


def test_agent_execution_ensure_long_output_orders_exact_lists_and_exposes_context(
    tmp_path,
):
    MockAgentExecutionOrderedExactListRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionOrderedExactListRequester,
        "ensure-long-output-ordered-exact-list",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("generate exactly three items followed by their summary")
        .output(ExactThreeItemWithSummaryOutput, format="json")
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "items": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
        "summary": "three ordered items",
    }
    assert MockAgentExecutionOrderedExactListRequester.attempts == 3
    first_continuation = MockAgentExecutionOrderedExactListRequester.requests[
        1
    ]["continuation"]
    assert first_continuation["assembly_slots"][0]["path_key"] == "p0:items"
    assert "path" not in first_continuation["assembly_slots"][0]
    assert first_continuation["continuity_context"] == {
        "accepted_json": '{"items":[{"name":"a"}]}',
        "accepted_serialized_character_count": len(
            '{"items":[{"name":"a"}]}'
        ),
        "complete_snapshot": True,
    }
    second_continuation = MockAgentExecutionOrderedExactListRequester.requests[
        2
    ]["continuation"]
    assert second_continuation["assembly_slots"][0]["path_key"] == (
        "p1:summary"
    )
    assert "path" not in second_continuation["assembly_slots"][0]
    assert second_continuation["continuity_context"]["accepted_json"] == (
        '{"items":[{"name":"a"},{"name":"b"},{"name":"c"}]}'
    )


def test_agent_execution_ensure_long_output_repairs_final_validation_without_losing_units(
    tmp_path,
):
    def require_summary(value, _context) -> OutputValidateResultDict:
        return {
            "ok": value.get("summary")
            == "three components generated",
            "reason": "summary must describe the three components",
        }

    MockAgentExecutionFinalValidationRepairRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionFinalValidationRepairRequester,
        "ensure-long-output-final-repair",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("generate every component and a summary")
        .output(
                {
                    "components": [{"name": (str, "component name", True)}],
                    "summary": (str, "required summary"),
                },
                format="json",
            )
            .validate(require_summary)
            .ensure_long_output()
        )

    result = execution.get_data(max_retries=1)

    assert result == {
        "components": [
            {"name": "a"},
            {"name": "b"},
            {"name": "c"},
        ],
        "summary": "three components generated",
    }
    assert MockAgentExecutionFinalValidationRepairRequester.attempts == 3
    repair_request = MockAgentExecutionFinalValidationRepairRequester.requests[2]
    assert "Final validation failed" in repair_request["continuation"][
        "repair_feedback"
    ]["action"]
    assert len(execution.diagnostics["long_output_validation_repairs"]) == 1
    long_output = _get_long_output_meta(execution)
    assert long_output["accepted_unit_count"] == 4
    assert long_output["replayed_unit_count"] == 4
    assert long_output["validation_repair_count"] == 1


def test_agent_execution_ensure_long_output_bounds_final_validation_repairs(tmp_path):
    def require_summary(value, _context) -> OutputValidateResultDict:
        return {
            "ok": value.get("summary")
            == "three components generated",
            "reason": "summary must describe the three components",
        }

    MockAgentExecutionUnrepairableFinalRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionUnrepairableFinalRequester,
        "ensure-long-output-bounded-final-repair",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("generate every component and a summary")
        .output(
                {
                    "components": [{"name": (str, "component name", True)}],
                    "summary": (str, "required summary"),
                },
                format="json",
            )
            .validate(require_summary)
            .ensure_long_output()
        )

    with pytest.raises(Exception, match="summary"):
        execution.get_data(max_retries=1)

    assert MockAgentExecutionUnrepairableFinalRequester.attempts == 3
    assert len(execution.diagnostics["long_output_validation_repairs"]) == 1


def test_agent_execution_ensure_long_output_assembles_more_than_seventy_units(tmp_path):
    MockAgentExecutionLargeStructuredLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLargeStructuredLongOutputRequester,
        "ensure-long-output-large-structured",
    ).use_task_workspace(tmp_path)

    execution = (
        agent.input("generate 75 components")
        .output({"components": [{"name": (str,)}]}, format="json")
        .validate(
            lambda value, _context: {
                "ok": len(value["components"]) == 75,
                "reason": "all 75 components are required",
            }
        )
        .ensure_long_output()
    )
    result = execution.get_data()

    assert len(result["components"]) == 75
    assert result["components"][0] == {"name": "a"}
    assert result["components"][-1] == {"name": "c74"}
    assert MockAgentExecutionLargeStructuredLongOutputRequester.attempts == 3
    assert _get_long_output_meta(execution)["accepted_unit_count"] == 75


def test_agent_execution_stream_preserves_structured_completion_provenance(tmp_path):
    MockAgentExecutionStructuredLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionStructuredLongOutputRequester,
        "ensure-long-output-provenance",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("generate every component")
        .output({"components": [{"name": (str,)}]}, format="json")
        .ensure_long_output()
    )

    items = list(execution.get_generator(type="instant"))
    first_item = next(item for item in items if item.path == "components[0]" and item.is_complete)
    partial_item = next(item for item in items if item.path == "components[1]" and item.is_complete)

    assert first_item.completion_source == "observed_boundary"
    assert partial_item.completion_source == "synthetic_repair"
    assert execution.get_data()["components"][-1]["name"] == "c"


def test_agent_execution_ensure_long_output_commits_closed_updates_from_truncated_continuation(tmp_path):
    MockAgentExecutionMultiSegmentLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionMultiSegmentLongOutputRequester,
        "ensure-long-output-multi-segment",
    ).use_task_workspace(tmp_path)

    execution = agent.input("write three blocks").ensure_long_output()

    assert execution.get_text() == "alpha-beta-gamma"
    assert MockAgentExecutionMultiSegmentLongOutputRequester.attempts == 3
    assert _get_long_output_meta(execution)["accepted_unit_count"] == 3


def test_agent_execution_ensure_long_output_commits_one_text_update_per_continuation(
    tmp_path,
):
    MockAgentExecutionMultiUpdateTextRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionMultiUpdateTextRequester,
        "ensure-long-output-one-text-update",
    ).use_task_workspace(tmp_path)

    execution = agent.input("write three blocks").ensure_long_output()

    assert execution.get_text() == "alpha-beta-gamma"
    assert MockAgentExecutionMultiUpdateTextRequester.attempts == 3
    rejected = execution.diagnostics["long_output_rejected_updates"]
    assert rejected[0]["accepted_prefix_count"] == 1
    assert "exactly one append_text update" in rejected[0]["reason"]
    repaired_request = MockAgentExecutionMultiUpdateTextRequester.requests[2]
    assert repaired_request["continuation"]["assembly_slots"][0][
        "next_unit_index"
    ] == 2
    assert _get_long_output_meta(execution)["accepted_unit_count"] == 3


def test_agent_execution_ensure_long_output_commits_structured_prefix_from_truncated_continuation(
    tmp_path,
):
    MockAgentExecutionStructuredContinuationLengthRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionStructuredContinuationLengthRequester,
        "ensure-long-output-structured-continuation-length",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write all components")
        .output({"components": [{"name": (str,)}]}, format="json")
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "components": [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    }
    long_output = _get_long_output_meta(execution)
    assert MockAgentExecutionStructuredContinuationLengthRequester.attempts == 3
    assert long_output["accepted_unit_count"] == 3
    assert long_output["replayed_unit_count"] == 3
    assert long_output["request_count"] == 3


def test_agent_execution_ensure_long_output_commits_large_deferred_prefix(
    tmp_path,
):
    MockAgentExecutionLargeStructuredContinuationLengthRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLargeStructuredContinuationLengthRequester,
        "ensure-long-output-large-deferred-prefix",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write all components")
        .output({"components": [{"name": (str,)}]}, format="json")
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "components": [
            {"name": "a"},
            {"name": "b" * 1500},
            {"name": "c"},
        ]
    }
    assert (
        MockAgentExecutionLargeStructuredContinuationLengthRequester.attempts
        == 3
    )
    assert _get_long_output_meta(execution)["accepted_unit_count"] == 3


def test_agent_execution_ensure_long_output_recovers_after_length_before_header_close(
    tmp_path,
):
    MockAgentExecutionIncompleteHeaderRecoveryRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionIncompleteHeaderRecoveryRequester,
        "ensure-long-output-header-recovery",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write all components")
        .output({"components": [{"name": (str,)}]}, format="json")
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "components": [{"name": "a"}, {"name": "b"}]
    }
    long_output = _get_long_output_meta(execution)
    no_progress = execution.diagnostics["long_output_no_progress"]
    assert MockAgentExecutionIncompleteHeaderRecoveryRequester.attempts == 3
    assert long_output["manifest_revision"] == 2
    assert long_output["accepted_unit_count"] == 2
    assert long_output["replayed_unit_count"] == 2
    assert no_progress == [
        {
            "segment_index": 1,
            "response_id": no_progress[0]["response_id"],
            "reason_code": "continuation_header_incomplete",
            "reason": (
                "Length-terminated continuation did not close required header "
                "fields: base_revision, base_digest, anchor."
            ),
            "observed_header_fields": [],
            "missing_header_fields": [
                "base_revision",
                "base_digest",
                "anchor",
            ],
            "observed_complete_paths": [],
            "manifest_revision": 1,
            "manifest_digest": no_progress[0]["manifest_digest"],
            "accepted_unit_count": 1,
            "no_progress_count": 1,
        }
    ]
    recovery_input = (
        MockAgentExecutionIncompleteHeaderRecoveryRequester.requests[2][
            "continuation"
        ]
    )
    assert recovery_input["repair_feedback"]["reason_code"] == (
        "continuation_header_incomplete"
    )
    assert (
        MockAgentExecutionIncompleteHeaderRecoveryRequester.requests[1][
            "input_keys"
        ][0]
        == "long_output_continuation"
    )
    continuation_instruct = (
        MockAgentExecutionIncompleteHeaderRecoveryRequester.requests[1][
            "instruct"
        ]
    )
    assert list(continuation_instruct)[0] == "long_output_delivery_protocol"
    delivery_protocol = " ".join(
        continuation_instruct["long_output_delivery_protocol"]
    )
    assert "a nested schema with type array must be a JSON array" in (
        delivery_protocol
    )
    assert "emit at most one corrected update" in delivery_protocol


def test_agent_execution_ensure_long_output_owns_complete_envelope_recovery(
    tmp_path,
):
    MockAgentExecutionInvalidCompleteEnvelopeRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionInvalidCompleteEnvelopeRequester,
        "ensure-long-output-complete-envelope-recovery",
    ).use_task_workspace(tmp_path)
    execution = agent.input("write the complete text").ensure_long_output()

    assert execution.get_text() == "alpha-omega"
    assert MockAgentExecutionInvalidCompleteEnvelopeRequester.attempts == 4
    no_progress = execution.diagnostics["long_output_no_progress"]
    assert [item["reason_code"] for item in no_progress] == [
        "continuation_envelope_invalid",
        "continuation_envelope_invalid",
    ]
    assert [item["no_progress_count"] for item in no_progress] == [1, 2]
    second_recovery = (
        MockAgentExecutionInvalidCompleteEnvelopeRequester.requests[2][
            "continuation"
        ]["repair_feedback"]
    )
    assert second_recovery["reason_code"] == (
        "continuation_envelope_invalid"
    )
    assert second_recovery["recovery_attempt"] == 1
    long_output = _get_long_output_meta(execution)
    assert long_output["request_count"] == 4
    assert long_output["accepted_unit_count"] == 2
    assert long_output["replayed_unit_count"] == 2


def test_agent_execution_ensure_long_output_bounds_incomplete_header_no_progress(
    tmp_path,
):
    MockAgentExecutionIncompleteHeaderNoProgressRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionIncompleteHeaderNoProgressRequester,
        "ensure-long-output-header-no-progress",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write all components")
        .output({"components": [{"name": (str,)}]}, format="json")
        .ensure_long_output()
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "no durable progress.*continuation_header_incomplete.*"
            "base_revision, base_digest, anchor"
        ),
    ):
        execution.get_data()

    assert MockAgentExecutionIncompleteHeaderNoProgressRequester.attempts == 4
    no_progress = execution.diagnostics["long_output_no_progress"]
    assert len(no_progress) == 3
    assert [item["no_progress_count"] for item in no_progress] == [1, 2, 3]
    assert all(
        item["reason_code"] == "continuation_header_incomplete"
        for item in no_progress
    )
    assert execution.diagnostics["long_output"]["manifest_revision"] == 1
    assert execution.diagnostics["long_output"]["accepted_unit_count"] == 1


def test_agent_execution_ensure_long_output_normalizes_nested_slot_values_without_serializer_warning(
    tmp_path,
):
    MockAgentExecutionNestedSlotRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionNestedSlotRequester,
        "ensure-long-output-nested-slot-normalization",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write nested groups")
        .output(
            {
                "groups": [
                    {
                        "title": (str,),
                        "options": [
                            {
                                "name": (str,),
                                "enabled": (bool,),
                            }
                        ],
                    }
                ]
            },
            format="json",
        )
        .ensure_long_output()
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = execution.get_data()

    assert result == {
        "groups": [
            {
                "title": "g1",
                "options": [{"name": "a", "enabled": True}],
            },
            {
                "title": "g2",
                "options": [{"name": "b", "enabled": False}],
            },
        ]
    }
    assert not [
        item
        for item in caught
        if "PydanticSerializationUnexpectedValue" in str(item.message)
    ]
    value_contract = (
        MockAgentExecutionNestedSlotRequester.requests[1]["continuation"][
            "assembly_slots"
        ][0]["value_contract"]
    )
    assert value_contract["type"] == "object"
    assert value_contract["properties"]["options"]["type"] == "array"
    assert (
        value_contract["properties"]["options"]["items"]["properties"][
            "enabled"
        ]["type"]
        == "boolean"
    )


def test_agent_execution_ensure_long_output_replays_initial_structured_text_slot(
    tmp_path,
):
    MockAgentExecutionStructuredTextSlotRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionStructuredTextSlotRequester,
        "ensure-long-output-structured-text-slot",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write title and components")
        .output(
            {
                "title": (str,),
                "components": [{"name": (str,)}],
            },
            format="json",
        )
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "title": "alpha",
        "components": [{"name": "a"}],
    }
    continuation_slots = MockAgentExecutionStructuredTextSlotRequester.requests[
        1
    ]["continuation"]["assembly_slots"]
    assert [slot["path_key"] for slot in continuation_slots] == [
        "p1:components"
    ]
    assert _get_long_output_meta(execution)["replayed_unit_count"] == 2


def test_agent_execution_ensure_long_output_rejects_append_to_completed_structured_text(
    tmp_path,
):
    MockAgentExecutionRepeatedStructuredTextRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionRepeatedStructuredTextRequester,
        "ensure-long-output-immutable-structured-text",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write title and components")
        .output(
            {
                "title": (str,),
                "components": [{"name": (str,)}],
            },
            format="json",
        )
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "title": "alpha",
        "components": [{"name": "a"}],
    }
    assert MockAgentExecutionRepeatedStructuredTextRequester.attempts == 3
    rejected = execution.diagnostics["long_output_rejected_updates"]
    assert "already committed" in rejected[0]["reason"]
    assert rejected[0]["accepted_prefix_count"] == 0


def test_agent_execution_ensure_long_output_preserves_observed_empty_list(
    tmp_path,
):
    MockAgentExecutionExplicitEmptyListRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionExplicitEmptyListRequester,
        "ensure-long-output-explicit-empty-list",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write an explicit empty item list and tail")
        .output(
            {
                "items": [{"name": (str, "item name", True)}],
                "tail": (str, "tail text", True),
            },
            format="json",
        )
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "items": [],
        "tail": "done",
    }
    assert _get_long_output_meta(execution)["replayed_unit_count"] == 2


def test_agent_execution_ensure_long_output_continuation_declares_empty_list(
    tmp_path,
):
    MockAgentExecutionContinuationEmptyListRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionContinuationEmptyListRequester,
        "ensure-long-output-continuation-empty-list",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("write head and an explicit empty item list")
        .output(
            {
                "head": (str, "head text", True),
                "items": [{"name": (str, "item name", True)}],
            },
            format="json",
        )
        .ensure_long_output()
    )

    assert execution.get_data() == {
        "head": "alpha",
        "items": [],
    }
    continuation_slot = _slot_for_label(
        MockAgentExecutionContinuationEmptyListRequester.requests[1][
            "continuation"
        ],
        "items",
    )
    assert continuation_slot["empty_operation"] == "declare_empty_list"
    assert continuation_slot["empty_is_declared"] is False
    assert _get_long_output_meta(execution)["replayed_unit_count"] == 2


def test_agent_execution_ensure_long_output_rejects_stale_empty_list_declaration(
    tmp_path,
):
    MockAgentExecutionStaleEmptyListRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionStaleEmptyListRequester,
        "ensure-long-output-stale-empty-list",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("do not erase accepted items")
        .output(
            {
                "items": [{"name": (str, "item name", True)}],
                "tail": (str, "tail text", True),
            },
            format="json",
        )
        .ensure_long_output()
    )

    with pytest.raises(
        RuntimeError,
        match="no durable progress.*continuation_update_rejected",
    ):
        execution.get_data()

    assert MockAgentExecutionStaleEmptyListRequester.attempts == 4
    assert execution.diagnostics["long_output"]["accepted_unit_count"] == 1
    assert all(
        "Empty-list declaration" in item["reason"]
        for item in execution.diagnostics["long_output_rejected_updates"]
    )
    repair_feedback = MockAgentExecutionStaleEmptyListRequester.requests[2][
        "continuation"
    ]["repair_feedback"]
    assert "already complete" in repair_feedback["action"]


def test_agent_execution_ensure_long_output_requires_manifest_fact_before_final(
    tmp_path,
):
    MockAgentExecutionRequiredSlotBarrierRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionRequiredSlotBarrierRequester,
        "ensure-long-output-required-slot-barrier",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.input("complete all required fields")
        .output(
            {
                "head": (str, "head text", True),
                "summary": (str, "summary text", True),
                "direct_answer": (str, "blank when not applicable", True),
            },
            format="json",
        )
        .ensure_long_output()
    )

    assert execution.get_data(max_retries=0) == {
        "head": "alpha",
        "summary": "ready",
        "direct_answer": "",
    }
    assert MockAgentExecutionRequiredSlotBarrierRequester.attempts == 3
    recovery_feedback = (
        MockAgentExecutionRequiredSlotBarrierRequester.requests[2][
            "continuation"
        ]["repair_feedback"]
    )
    assert recovery_feedback["reason_code"] == (
        "continuation_required_slots_missing"
    )
    assert recovery_feedback["missing_paths"] == ["direct_answer"]
    assert _get_long_output_meta(execution)["replayed_unit_count"] == 3


def test_agent_execution_ensure_long_output_many_segments_do_not_grow_call_stack(
    tmp_path,
):
    MockAgentExecutionManySegmentLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionManySegmentLongOutputRequester,
        "ensure-long-output-many-segments",
    ).use_task_workspace(tmp_path)

    result = agent.input("write every segment").ensure_long_output().get_text()

    assert result.endswith("segment-13;")
    assert MockAgentExecutionManySegmentLongOutputRequester.attempts == 13
    assert (
        max(MockAgentExecutionManySegmentLongOutputRequester.stack_depths)
        - min(MockAgentExecutionManySegmentLongOutputRequester.stack_depths)
        < 10
    )


def test_agent_execution_ensure_long_output_fails_closed_after_repeated_no_progress(tmp_path):
    MockAgentExecutionNoProgressLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionNoProgressLongOutputRequester,
        "ensure-long-output-no-progress",
    ).use_task_workspace(tmp_path)

    with pytest.raises(RuntimeError, match="no durable progress"):
        agent.input("write a long result").ensure_long_output().get_text()

    assert MockAgentExecutionNoProgressLongOutputRequester.attempts == 4


def test_agent_execution_ensure_long_output_rejects_stale_digest_without_committing(tmp_path):
    MockAgentExecutionStaleLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionStaleLongOutputRequester,
        "ensure-long-output-stale-digest",
    ).use_task_workspace(tmp_path)

    execution = agent.input("write a long result").ensure_long_output()
    with pytest.raises(RuntimeError, match="manifest revision, digest, and anchor"):
        execution.get_text()

    assert MockAgentExecutionStaleLongOutputRequester.attempts == 2
    assert all(
        request.get("continuation") is None or request["tools"] is None
        for request in MockAgentExecutionStaleLongOutputRequester.requests
    )


def test_agent_execution_ensure_long_output_fails_when_manifest_replay_changes(
    tmp_path,
    monkeypatch,
):
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester,
        "ensure-long-output-replay-digest",
    ).use_task_workspace(tmp_path)
    original_read_file = TaskWorkspace.read_file
    manifest_reads: dict[str, int] = {}

    async def read_file_with_changed_replay(self, path, *, max_bytes=20000, offset=0):
        readback = await original_read_file(
            self,
            path,
            max_bytes=max_bytes,
            offset=offset,
        )
        path_text = str(path)
        if "manifests/" not in path_text:
            return readback
        manifest_reads[path_text] = manifest_reads.get(path_text, 0) + 1
        if manifest_reads[path_text] == 2:
            return replace(
                readback,
                content=f"{readback.content} ",
                data=f"{readback.content} ".encode("utf-8"),
            )
        return readback

    monkeypatch.setattr(TaskWorkspace, "read_file", read_file_with_changed_replay)

    with pytest.raises(RuntimeError, match="TaskWorkspace replay mismatch"):
        agent.input("write a long result").ensure_long_output().get_text()


def test_agent_execution_ensure_long_output_respects_execution_model_request_budget(tmp_path):
    MockAgentExecutionMultiSegmentLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionMultiSegmentLongOutputRequester,
        "ensure-long-output-model-budget",
    ).use_task_workspace(tmp_path)
    execution = (
        agent.create_execution(limits={"max_model_requests": 2})
        .input("write three blocks")
        .ensure_long_output()
    )

    with pytest.raises(RuntimeError, match="max_model_requests"):
        execution.get_text()

    # The third request object is prepared before the shared execution budget
    # rejects dispatch; only two provider requests are consumed.
    assert MockAgentExecutionMultiSegmentLongOutputRequester.attempts == 3
    assert execution.execution_context.model_request_count == 2


def test_agent_execution_ensure_long_output_rejects_unsupported_format_before_dispatch(tmp_path):
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester,
        "ensure-long-output-unsupported-format",
    ).use_task_workspace(tmp_path)

    with pytest.raises(RuntimeError, match="plain text and JSON"):
        (
            agent.input("write a long result")
            .output({"result": (str,)}, format="yaml_literal")
            .ensure_long_output()
            .get_data()
        )

    assert MockAgentExecutionLongOutputRequester.attempts == 0


def test_agent_execution_ensure_long_output_does_not_mix_with_agent_task(tmp_path):
    MockAgentExecutionLongOutputRequester.reset()
    agent = _create_long_output_test_agent(
        MockAgentExecutionLongOutputRequester,
        "ensure-long-output-agent-task-boundary",
    ).use_task_workspace(tmp_path)

    with pytest.raises(RuntimeError, match="cannot be mixed"):
        (
            agent.input("plan and execute a complex task")
            .strategy("task")
            .ensure_long_output()
            .get_data()
        )

    assert MockAgentExecutionLongOutputRequester.attempts == 0


def test_completed_agent_execution_create_execution_returns_clean_fresh_execution():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("completed-execution-create-new")
    agent.system("Reply briefly.", always=True)

    execution = agent.input("first").output({"reply": (str,)}, format="json")
    first = execution.start()["reply"]
    fresh_execution = execution.create_execution()
    second = fresh_execution.input("second").output({"reply": (str,)}, format="json").start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    assert second == "attempt=2; input=second; history=0"


def test_agent_quick_prompt_uses_current_agent_chat_history():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("quick-prompt-history")
    agent.system("Reply with history.", always=True)
    chat_history: list[ChatMessageDict] = []

    agent.set_chat_history(chat_history)
    first = agent.input("first").output({"reply": (str,)}, format="json").start()["reply"]
    chat_history.extend(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": first},
        ]
    )
    agent.set_chat_history(chat_history)
    second = agent.input("second").output({"reply": (str,)}, format="json").start()["reply"]

    assert first == "attempt=1; input=first; history=0"
    assert second == "attempt=2; input=second; history=2"


def test_agent_execution_get_data_object_returns_model_object():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-data-object")

    result_object = (
        agent.input("object")
        .output({"reply": (str,)}, format="json")
        .get_data_object(ensure_keys=["reply"])
    )

    assert result_object is not None
    assert result_object.model_dump()["reply"] == "attempt=1; input=object; history=0"


def test_agent_execution_result_get_data_object_returns_model_object():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-result-data-object")
    execution_result = agent.input("object").output({"reply": (str,)}, format="json").get_result()

    result_object = execution_result.get_data_object(ensure_keys=["reply"])

    assert result_object is not None
    assert result_object.model_dump()["reply"] == "attempt=1; input=object; history=0"


def test_agent_execution_key_waiter_facade_uses_execution_prompt():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-key-waiter")

    execution = agent.input("key").output({"reply": (str,)}, format="json")

    assert execution.get_key_result("reply") == "attempt=1; input=key; history=0"

    waiter_execution = agent.input("wait").output({"reply": (str,)}, format="json")
    assert list(waiter_execution.wait_keys(["reply"])) == [
        ("reply", "attempt=2; input=wait; history=0")
    ]

    handler_execution = agent.input("handler").output({"reply": (str,)}, format="json")
    handled = handler_execution.when_key("reply", lambda value: str(value).upper()).start_waiter()
    assert handled == [
        (
            "reply",
            "attempt=3; input=handler; history=0",
            "ATTEMPT=3; INPUT=HANDLER; HISTORY=0",
        )
    ]


def test_agent_execution_key_waiter_result_key_ignores_terminal_execution_result():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-key-waiter-result-key")

    handler_execution = agent.input("handler").output({"result": (str,)}, format="json")
    handled = handler_execution.when_key("result", lambda value: value).start_waiter()

    assert handled == [
        (
            "result",
            "attempt=1; input=handler; history=0",
            "attempt=1; input=handler; history=0",
        )
    ]


def test_agent_execution_prompt_text_is_available_before_and_after_start():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-prompt-text")
    agent.system("Reply briefly.", always=True)

    execution = agent.input("inspect prompt").output({"reply": (str,)}, format="json")
    before = execution.get_prompt_text()
    execution.start()
    after = execution.get_prompt_text()

    assert "inspect prompt" in before
    assert "inspect prompt" in after


def test_agent_get_prompt_text_reports_execution_prompt_boundary():
    agent = _create_test_agent("agent-prompt-boundary")

    agent.input("discarded execution")

    with pytest.raises(RuntimeError, match="AgentExecution"):
        agent.get_prompt_text()


def test_agent_execution_specific_stream_yields_event_tuples():
    MockAgentExecutionSpecificStreamRequester.reset()
    agent = _create_specific_stream_test_agent()

    events = list(agent.input("stream").get_generator(type="specific"))

    assert events == [
        ("reasoning_delta", "thinking"),
        ("delta", "answer"),
        ("tool_calls", [{"id": "call-1", "name": "lookup_policy"}]),
        ("done", "answer"),
    ]


def test_agent_execution_streaming_print_uses_execution_delta_stream(capsys):
    MockAgentExecutionSpecificStreamRequester.reset()
    agent = _create_specific_stream_test_agent("execution-streaming-print")

    agent.input("stream").streaming_print()

    output = capsys.readouterr().out
    assert "answer" in output


def test_agent_execution_delta_stream_filters_original_provider_delta():
    MockAgentExecutionOriginalDeltaRequester.reset()
    agent = _create_original_delta_test_agent()

    deltas = list(agent.input("stream").get_generator(type="delta"))
    original_events = list(
        agent.input("stream").get_generator(
            type="specific",
            specific=["original_delta"],
        )
    )

    assert deltas == ["answer"]
    assert original_events == [("original_delta", '{"provider":"raw"}')]


def test_agent_execution_instant_stream_preserves_full_data_snapshot():
    MockAgentExecutionCompatibilityRequester.reset()
    agent = _create_test_agent("execution-instant-full-data")

    items = list(agent.input("snapshot").output({"reply": (str,)}, format="json").get_generator(type="instant"))
    completed_reply_items = [
        item
        for item in items
        if item.path == "reply" and item.is_complete
    ]

    assert completed_reply_items
    assert completed_reply_items[-1].full_data
