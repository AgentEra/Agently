from __future__ import annotations

from typing import Any

import pytest

from agently.core.context import ModelRequestContextSelector
from agently.types.data import ContextCandidate, ContextConsumer, ContextReadIntent


class FakeModelRequest:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.slots: dict[str, Any] = {}
        self.output_format: str | None = None

    def input(self, value: Any) -> "FakeModelRequest":
        self.slots["input"] = value
        return self

    def info(self, value: Any) -> "FakeModelRequest":
        self.slots["info"] = value
        return self

    def instruct(self, value: Any) -> "FakeModelRequest":
        self.slots["instruct"] = value
        return self

    def output(self, value: Any, *, format: str | None = None) -> "FakeModelRequest":
        self.slots["output"] = value
        self.output_format = format
        return self

    async def async_get_data(self) -> Any:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _candidates() -> tuple[ContextCandidate, ...]:
    return (
        ContextCandidate(
            block_key="context-block:1",
            source_id="source:private-a",
            source_revision="rev:secret-a",
            source_ref="private/path/a.md",
            binding_id="binding:private-a",
            role="information",
            summary="Release evidence and risk notes",
            estimated_chars=900,
        ),
        ContextCandidate(
            block_key="context-block:2",
            source_id="source:private-b",
            source_revision="rev:secret-b",
            source_ref="private/path/b.md",
            binding_id="binding:private-b",
            role="example",
            summary="Example acceptance report",
            estimated_chars=500,
        ),
    )


@pytest.mark.asyncio
async def test_model_request_selector_uses_prompt_lanes_and_host_keys_only() -> None:
    request = FakeModelRequest(
        {"selected_keys": ["context-block:2"], "priorities": {"context-block:2": 1}}
    )
    selector = ModelRequestContextSelector(lambda: request)

    result = await selector.async_select(
        intent=ContextReadIntent(
            query="Prepare the acceptance report",
            metadata={
                "selection_budget": {
                    "available_chars": 1200,
                    "available_blocks": 2,
                    "max_block_chars": 900,
                }
            },
        ),
        candidates=_candidates(),
        consumer=ContextConsumer("planner", model="test-model"),
        phase="planning",
    )

    assert result.selected_keys == ("context-block:2",)
    assert request.slots["input"] == {
        "intent": "Prepare the acceptance report",
        "phase": "planning",
    }
    cards = request.slots["info"]["offered_context_blocks"]
    assert [card["block_key"] for card in cards] == ["context-block:1", "context-block:2"]
    assert cards[0]["summary"] == "Release evidence and risk notes"
    assert "source_id" not in cards[0]
    assert "source_revision" not in cards[0]
    assert "source_ref" not in cards[0]
    assert "binding_id" not in cards[0]
    assert set(request.slots["info"]) == {"offered_context_blocks"}
    assert "selection_guidance" not in request.slots["instruct"]
    assert "Return only offered block_key values" in request.slots["instruct"]
    assert "descending task relevance" in request.slots["instruct"]
    assert request.slots["output"] == {
        "selected_keys": ([str], "Ordered subset of offered block_key values.", True),
    }
    assert request.output_format == "json"


@pytest.mark.asyncio
async def test_model_request_selector_does_not_locally_validate_semantics_or_join_identity() -> None:
    request = FakeModelRequest({"selected_keys": ["unknown-key"]})
    selector = ModelRequestContextSelector(lambda: request)

    result = await selector.async_select(
        intent=ContextReadIntent(query="Select evidence"),
        candidates=_candidates(),
        consumer=ContextConsumer("worker"),
        phase="execution",
    )

    # ContextReader, not the model adapter, validates offered-key membership and
    # reconstructs canonical source identity.
    assert result.selected_keys == ("unknown-key",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {},
        {"selected_keys": "context-block:1"},
        {"selected_keys": [1]},
    ],
)
async def test_model_request_selector_rejects_invalid_output_shape(result: Any) -> None:
    selector = ModelRequestContextSelector(lambda: FakeModelRequest(result))

    with pytest.raises(ValueError, match="selected_keys"):
        await selector.async_select(
            intent=ContextReadIntent(query="Select evidence"),
            candidates=_candidates(),
            consumer=ContextConsumer("worker"),
            phase="execution",
        )


@pytest.mark.asyncio
async def test_model_request_selector_propagates_request_failure() -> None:
    selector = ModelRequestContextSelector(
        lambda: FakeModelRequest(RuntimeError("provider unavailable"))
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await selector.async_select(
            intent=ContextReadIntent(query="Select evidence"),
            candidates=_candidates(),
            consumer=ContextConsumer("worker"),
            phase="execution",
        )


@pytest.mark.asyncio
async def test_selector_projects_read_guidance_without_identity_or_other_metadata() -> None:
    request = FakeModelRequest({"selected_keys": []})
    await ModelRequestContextSelector(lambda: request).async_select(
        intent=ContextReadIntent(
            query="Prepare handoff",
            metadata={
                "selection_guidance": [
                    {
                        "content": "Read references/detail.md before handoff.",
                        "completeness": "lossy",
                        "source_id": "private-identity",
                    }
                ],
                "unrelated": "PRIVATE_METADATA",
            },
        ),
        candidates=_candidates(),
        consumer=ContextConsumer("worker"),
        phase="handoff",
    )
    assert request.slots["info"]["selection_guidance"] == [
        {
            "content": "Read references/detail.md before handoff.",
            "completeness": "lossy",
        }
    ]
    assert "PRIVATE_METADATA" not in str(request.slots)
    assert "private-identity" not in str(request.slots)
    assert "[info.selection_guidance]" in request.slots["instruct"]
    assert "Read references/detail.md before handoff." not in request.slots["instruct"]
