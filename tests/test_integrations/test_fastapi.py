import json

import httpx
import pytest
from pydantic import BaseModel

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently.integrations.fastapi import FastAPIHelper


class AskInput(BaseModel):
    question: str


class StreamItem(BaseModel):
    stage: str


class FinalResult(BaseModel):
    answer: str


def _create_contract_flow():
    flow = TriggerFlow(name="fastapi-contract-flow").set_contract(
        initial_input=AskInput,
        stream=StreamItem,
        result=FinalResult,
        meta={"domain": "qa"},
    )

    async def worker(data: TriggerFlowRuntimeData[AskInput, StreamItem, FinalResult]):
        await data.async_put(StreamItem(stage="start"))
        await data.async_stop_stream()
        data.set_result(FinalResult(answer=data.value.question.upper()))

    flow.to(worker).end()
    return flow


@pytest.mark.asyncio
async def test_fastapi_helper_post_uses_triggerflow_contract_for_runtime_and_openapi():
    app = FastAPIHelper(response_provider=_create_contract_flow())
    app.use_post("/run")
    app.use_sse("/run/stream")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/run", json={"data": {"question": "hello"}, "options": {}})
        assert response.status_code == 200
        assert response.json() == {
            "status": 200,
            "data": {"$final_result": {"answer": "HELLO"}},
            "msg": None,
        }

        invalid_response = await client.post("/run", json={"data": {"wrong": "value"}, "options": {}})
        invalid_payload = invalid_response.json()
        assert invalid_response.status_code == 422
        assert invalid_payload["detail"][0]["loc"] == ["body", "data", "question"]
        assert invalid_payload["detail"][0]["type"] == "missing"

    openapi = app.openapi()
    post_schema = openapi["paths"]["/run"]["post"]
    sse_schema = openapi["paths"]["/run/stream"]["get"]

    assert post_schema["x-agently-triggerflow-contract"]["initial_input"]["label"] == "AskInput"
    assert post_schema["x-agently-triggerflow-contract"]["result"]["label"] == "FinalResult"
    assert post_schema["x-agently-triggerflow-contract"]["system_stream"]["interrupt"]["label"] == "TriggerFlowInterruptEvent"
    assert sse_schema["x-agently-triggerflow-contract"]["stream"]["label"] == "StreamItem"
    assert "AskInput" in openapi["components"]["schemas"]


@pytest.mark.asyncio
async def test_fastapi_helper_sse_keeps_triggerflow_contract_runtime_validation():
    app = FastAPIHelper(response_provider=_create_contract_flow()).use_sse("/stream")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        async with client.stream(
            "GET",
            "/stream",
            params={"payload": json.dumps({"data": {"question": "hello"}, "options": {}})},
        ) as response:
            assert response.status_code == 200
            chunks = [line async for line in response.aiter_lines() if line]

    payloads = [json.loads(line.removeprefix("data: ")) for line in chunks if line.startswith("data: ")]
    assert any(payload["data"] == {"stage": "start"} for payload in payloads)
