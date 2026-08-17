from typing import Any, cast

import pytest
from pydantic import BaseModel

from agently import TriggerFlow
from agently.types.trigger_flow import TriggerFlowRuntimeData


class AskInput(BaseModel):
    question: str


class StreamItem(BaseModel):
    stage: str
    question: str


class FinalResult(BaseModel):
    answer: str


def _make_contract_flow():
    flow = TriggerFlow().set_contract(
        initial_input=AskInput,
        stream=StreamItem,
        result=FinalResult,
        meta={"kind": "demo"},
    )

    async def worker(data: TriggerFlowRuntimeData[Any, StreamItem, FinalResult]):
        assert isinstance(data.value, AskInput)
        await data.async_put(StreamItem(stage="start", question=data.value.question))
        await data.async_stop_stream()
        data.set_result(FinalResult(answer=data.value.question.upper()))

    flow.to(worker).end()
    return flow


def test_trigger_flow_contract_validates_input_stream_and_result():
    flow = _make_contract_flow()

    contract = flow.get_contract()
    assert contract.initial_input is AskInput
    assert contract.stream is StreamItem
    assert contract.result is FinalResult
    assert contract.meta == {"kind": "demo"}

    events = list(flow.get_runtime_stream(AskInput(question="hello")))
    assert len(events) == 1
    assert isinstance(events[0], StreamItem)
    assert events[0].stage == "start"
    assert events[0].question == "hello"

    result = flow.start(AskInput(question="world"))
    assert result == {"$final_result": FinalResult(answer="WORLD")}


def test_trigger_flow_contract_rejects_invalid_initial_input():
    flow = _make_contract_flow()

    with pytest.raises(ValueError, match="initial_input"):
        flow.start(cast(Any, {"wrong": "value"}))


def test_trigger_flow_contract_rejects_invalid_stream_item():
    flow = TriggerFlow().set_contract(stream=StreamItem)

    async def worker(data: TriggerFlowRuntimeData[Any, StreamItem, Any]):
        await data.async_put(cast(Any, {"stage": "missing-question"}))

    flow.to(worker).end()

    with pytest.raises(ValueError, match="stream"):
        flow.start(None)


def test_trigger_flow_contract_rejects_invalid_result():
    flow = TriggerFlow().set_contract(result=FinalResult)

    async def worker(data: TriggerFlowRuntimeData[Any, Any, FinalResult]):
        data.set_result(cast(Any, {"wrong": "shape"}))

    flow.to(worker).end()

    with pytest.raises(ValueError, match="result"):
        flow.start(None)
