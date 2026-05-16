import asyncio
from typing import AsyncGenerator

from fastapi import APIRouter, WebSocket
from fastapi.responses import StreamingResponse

from .flow import build_flow
from .schemas import AskRequest, AskResponse

router = APIRouter()

flow = build_flow()


async def run_flow(payload: dict):
    execution = flow.create_execution(auto_close=False)
    await execution.async_start(payload)
    close_task = asyncio.create_task(execution.async_close())
    events = []
    async for event in execution.get_async_runtime_stream(timeout=None):
        events.append(event)
    state = await close_task
    return state, events


@router.get("/sse")
async def sse(question: str):
    async def event_stream() -> AsyncGenerator[str, None]:
        execution = flow.create_execution(auto_close=False)
        await execution.async_start({"question": question})
        close_task = asyncio.create_task(execution.async_close())
        async for event in execution.get_async_runtime_stream(timeout=None):
            yield f"data: {event}\n\n"
            await asyncio.sleep(0)
        await close_task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    while True:
        question = await ws.receive_text()
        execution = flow.create_execution(auto_close=False)
        await execution.async_start({"question": question})
        close_task = asyncio.create_task(execution.async_close())
        async for event in execution.get_async_runtime_stream(timeout=None):
            await ws.send_text(str(event))
            await asyncio.sleep(0)
        await close_task


@router.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest):
    state, _ = await run_flow({"question": body.question})
    final = state.get("final") if isinstance(state, dict) else {}
    reply = final.get("reply") if isinstance(final, dict) else str(final)
    return AskResponse(reply=str(reply))
