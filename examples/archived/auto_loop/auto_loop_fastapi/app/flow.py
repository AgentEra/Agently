import asyncio
import json
from pathlib import Path
from typing import Any

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently.builtins.actions import Browse, Search

from .config import OLLAMA_API_KEY, OLLAMA_BASE_URL, OLLAMA_MODEL, SEARCH_PROXY


kb_collection = None


async def _emit(data: TriggerFlowRuntimeData, event_type: str, payload: Any):
    await data.async_put_into_stream(json.dumps({"type": event_type, "data": payload}))


async def _build_kb_collection():
    try:
        from agently.integrations.chromadb import ChromaCollection

        embedding = Agently.create_agent()
        embedding.set_settings(
            "OpenAICompatible",
            {
                "model": "qwen3-embedding:0.6b",
                "base_url": "http://127.0.0.1:11434/v1/",
                "auth": "nothing",
                "model_type": "embeddings",
            },
        )
        collection = ChromaCollection(
            collection_name="agently_examples",
            embedding_agent=embedding,
        )
        docs = []
        for path in Path("examples").rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            docs.append(
                {
                    "document": f"[FILE] {path}\n{content}",
                    "metadata": {"path": str(path)},
                }
            )
        if docs:
            collection.add(docs)
        return collection
    except Exception:
        return None


def build_flow() -> TriggerFlow:
    agent = Agently.create_agent()
    agent.set_settings(
        "OpenAICompatible",
        {
            "base_url": OLLAMA_BASE_URL,
            "api_key": OLLAMA_API_KEY,
            "model": OLLAMA_MODEL,
            "model_type": "chat",
            "request_options": {"temperature": 0.3},
        },
    )

    search = Search(
        proxy=SEARCH_PROXY,
        region="us-en",
        backend="google",
    )
    browse = Browse()

    tools_info = {
        "search": {
            "desc": "Search the web with {keywords}",
            "kwargs": {"keywords": [("str", "key word")]},
            "func": search.search,
        },
        "search_news": {
            "desc": "Search news with {keywords}",
            "kwargs": {"keywords": [("str", "key word")]},
            "func": search.search_news,
        },
        "browse": {
            "desc": "Browse the page at {url}",
            "kwargs": {"url": ("str", "Accessible URL")},
            "func": browse.browse,
        },
    }

    flow = TriggerFlow()

    async def start_request(data: TriggerFlowRuntimeData):
        global kb_collection
        if kb_collection is None:
            await _emit(data, "status", "kb preparing")
            kb_collection = await _build_kb_collection()
            if kb_collection is None:
                await _emit(data, "status", "kb disabled")
            else:
                await _emit(data, "status", "kb ready")
        return data.input

    async def prepare_context(data: TriggerFlowRuntimeData):
        payload = data.input
        question = payload.get("question", "")
        chat_history = payload.get("chat_history", [])
        memo = payload.get("memo", [])
        agent.set_chat_history(chat_history)
        await data.async_set_state("question", question)
        await data.async_set_state("done_plans", [])
        await data.async_set_state("step", 0)
        await data.async_set_state("memo", memo)
        await _emit(data, "status", "planning started")
        return question

    async def ensure_kb(data: TriggerFlowRuntimeData):
        global kb_collection
        if kb_collection is None:
            kb_collection = await _build_kb_collection()
        if kb_collection is None:
            await data.async_set_state("kb_results", [])
            return []
        results = kb_collection.query(data.get_state("question", ""))
        await data.async_set_state("kb_results", results)
        return results

    async def make_next_plan(data: TriggerFlowRuntimeData):
        question = data.get_state("question")
        done_plans = data.get_state("done_plans", [])
        step = data.get_state("step") or 0
        kb_results = data.get_state("kb_results") or []
        memo = data.get_state("memo") or []
        if step >= 5:
            final_action = {
                "type": "final",
                "reply": "Max steps reached. Please simplify your question and retry.",
            }
            await data.async_emit("Plan", final_action)
            return final_action

        tools_list = []
        for key, value in tools_info.items():
            tools_list.append(
                {
                    "tool_name": key,
                    "tool_desc": value["desc"],
                    "tool_args": value["kwargs"],
                }
            )

        request = (
            agent.input(question)
            .info({"tools": tools_list, "done": done_plans, "kb_results": kb_results, "memo": memo})
            .instruct(
                [
                    "Decide the next step based on {input}, {done}, and {tools}.",
                    "If {memo} contains constraints or preferences, follow them.",
                    "If an action keeps failing in {done}, choose 'final' and explain why.",
                    "If no tool is needed, choose 'final' and answer directly.",
                ]
            )
            .output(
                {
                    "next_step_thinking": ("str",),
                    "next_step_action": {
                        "type": ("'tool' | 'final'", "MUST IN values provided."),
                        "reply": ("str", "if type=='final' return the final answer, else ''"),
                        "tool_using": (
                            {
                                "tool_name": ("str from {tools.tool_name}", "Pick a tool from {tools}."),
                                "purpose": ("str", "Describe what you want to solve with the tool."),
                                "kwargs": ("dict", "Follow {tools.tool_args}."),
                            },
                            "if type=='tool' provide the tool plan, else null",
                        ),
                    },
                }
            )
        )
        response = request.get_response()
        thinking_started = False
        async for stream in response.get_async_generator(type="instant"):
            if stream.wildcard_path == "next_step_thinking" and stream.delta:
                if not thinking_started:
                    await _emit(data, "thinking_delta", "")
                    thinking_started = True
                await data.async_put_into_stream(json.dumps({"type": "thinking_delta", "data": stream.delta}))
            if stream.wildcard_path == "next_step_action.type" and stream.is_complete:
                await _emit(data, "plan", {"next_action": stream.value})
            if stream.wildcard_path == "next_step_action.tool_using.tool_name" and stream.is_complete:
                await _emit(data, "plan", {"tool": stream.value})
        result = await response.result.async_get_data()
        next_action = result["next_step_action"]
        await _emit(data, "status", "planning done")
        await data.async_set_state("step", step + 1)
        await data.async_emit("Plan", next_action)
        return next_action

    async def use_tool(data: TriggerFlowRuntimeData):
        tool_using_info = data.input["tool_using"]
        tool_name = tool_using_info["tool_name"].lower()
        tool = tools_info.get(tool_name)
        if tool is None:
            return {"type": "final", "reply": f"Unknown tool: {tool_name}"}

        await _emit(data, "status", f"tool running: {tool_name}")
        tool_func = tool["func"]
        if asyncio.iscoroutinefunction(tool_func):
            tool_result = await tool_func(**tool_using_info["kwargs"])
        else:
            tool_result = tool_func(**tool_using_info["kwargs"])
        await _emit(data, "status", f"tool done: {tool_name}")

        done_plans = data.get_state("done_plans", [])
        done_plans.append(
            {
                "purpose": tool_using_info["purpose"],
                "tool_name": tool_using_info["tool_name"],
                "result": tool_result,
            }
        )
        await data.async_set_state("done_plans", done_plans)
        return {"type": "tool"}

    async def reply(data: TriggerFlowRuntimeData):
        reply_text = data.input["reply"]
        await _emit(data, "reply", reply_text)
        await data.async_set_state("reply", reply_text)
        return data.input

    async def update_memo(data: TriggerFlowRuntimeData):
        memo = data.get_state("memo") or []
        question = data.get_state("question")
        reply_text = data.get_state("reply") or data.input.get("reply", "")
        result = (
            agent.input({"question": question, "reply": reply_text, "memo": memo})
            .instruct(
                [
                    "Extract any stable preferences, constraints, or facts to remember.",
                    "Only keep items that are useful for future turns.",
                    "Return an updated memo list.",
                ]
            )
            .output({"memo": [("str", "Short memo item")]})
            .async_start()
        )
        result = await result
        new_memo = result.get("memo", []) if isinstance(result, dict) else []
        if new_memo:
            await data.async_set_state("memo", new_memo)
            await _emit(data, "memo", new_memo)
        await data.async_set_state(
            "final",
            {
                "reply": reply_text,
                "memo": new_memo,
                "done_plans": data.get_state("done_plans", []),
            },
        )
        return {"type": "final", "reply": reply_text, "memo": new_memo}

    flow.to(start_request).to(prepare_context).to(ensure_kb).to(make_next_plan)
    (
        flow.when("Plan")
        .if_condition(lambda d: d.input.get("type") == "final")
        .to(reply)
        .to(update_memo)
        .else_condition()
        .to(use_tool)
        .to(make_next_plan)
        .end_condition()
    )

    return flow
