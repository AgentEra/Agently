import asyncio
from pathlib import Path

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData
from agently.builtins.actions import Browse, Search


## Auto Loop: plan -> tool -> plan -> reply (TriggerFlow + chat history + KB)
async def auto_loop_demo():
    # Idea: a planning loop that keeps asking/using tools until ready to reply,
    # then continues with the next user turn.
    # Flow: input -> plan -> tool -> plan -> reply -> loop
    # Expect: prints a readable "plan/tool/result" process and final replies.
    agent = Agently.create_agent()
    import os

    agent.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
            "api_key": os.environ.get("OLLAMA_API_KEY", "ollama"),
            "model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
            "model_type": "chat",
            "request_options": {"temperature": 0.3},
        },
    )

    # Built-in action packages (Search/Browse). Configure proxy if needed.
    search = Search(
        proxy="http://127.0.0.1:55758",
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
    kb_collection = None

    async def start_loop(data: TriggerFlowRuntimeData):
        nonlocal kb_collection
        if kb_collection is None:
            print("[📚] Preparing knowledge base from examples...")
            kb_collection = await build_kb_collection()
            if kb_collection is None:
                print("[📚] Knowledge base disabled (init failed).")
            else:
                print("[📚] Knowledge base ready.")
        await data.async_emit("Loop", None)
        return None

    async def get_input(data: TriggerFlowRuntimeData):
        try:
            question = input("Question (type 'exit' to stop): ").strip()
        except EOFError:
            question = "exit"
        if question.lower() == "exit":
            await data.async_set_state("closed_by_user", True)
            await data.async_put_into_stream("[status] user exit\n")
            await data.execution.async_close(reason="user_exit")
            return "exit"
        await data.async_emit("UserInput", question)
        return question

    async def prepare_context(data: TriggerFlowRuntimeData):
        question = data.input
        chat_history = data.get_state("chat_history") or []
        agent.set_chat_history(chat_history)
        await data.async_set_state("question", question)
        await data.async_set_state("done_plans", [])
        await data.async_set_state("step", 0)
        await data.async_set_state("print_process", True)
        if data.get_state("memo") is None:
            await data.async_set_state("memo", [])
        await data.async_put_into_stream("[status] planning started\n")
        return question

    async def ensure_kb(data: TriggerFlowRuntimeData):
        # Build a knowledge base from all example files once per process.
        nonlocal kb_collection
        if kb_collection is None:
            kb_collection = await build_kb_collection()

        if kb_collection is None:
            await data.async_set_state("kb_results", [])
            return []
        results = kb_collection.query(data.get_state("question", ""))
        await data.async_set_state("kb_results", results)
        return results

    async def build_kb_collection():
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
            .info(
                {
                    "tools": tools_list,
                    "done": done_plans,
                    "kb_results": kb_results,
                    "memo": memo,
                }
            )
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
        next_action = None
        thinking_started = False
        async for stream in response.get_async_generator(type="instant"):
            if stream.wildcard_path == "next_step_thinking" and stream.delta:
                if not thinking_started:
                    await data.async_put_into_stream("[thinking] ")
                    thinking_started = True
                await data.async_put_into_stream(stream.delta)
            if stream.wildcard_path == "next_step_thinking" and stream.is_complete:
                await data.async_put_into_stream("\n")
            if stream.wildcard_path == "next_step_action.type" and stream.is_complete:
                await data.async_put_into_stream(f"[plan] next_action: {stream.value}\n")
            if stream.wildcard_path == "next_step_action.tool_using.tool_name" and stream.is_complete:
                await data.async_put_into_stream(f"[plan] tool: {stream.value}\n")
        result = await response.result.async_get_data()
        next_action = result["next_step_action"]
        await data.async_put_into_stream("[status] planning done\n")
        await data.async_set_state("step", step + 1)
        await data.async_emit("Plan", next_action)
        return next_action

    async def use_tool(data: TriggerFlowRuntimeData):
        tool_using_info = data.input["tool_using"]
        tool_name = tool_using_info["tool_name"].lower()
        tool = tools_info.get(tool_name)
        if tool is None:
            return {"type": "final", "reply": f"Unknown tool: {tool_name}"}

        await data.async_put_into_stream(f"[status] tool running: {tool_name}\n")
        if data.get_state("print_process"):
            print("[🪛 I should use a tool]")
            print("🤔 Purpose:", tool_using_info["purpose"])
            print("🤔 Tool:", tool_using_info["tool_name"])

        tool_func = tool["func"]
        if asyncio.iscoroutinefunction(tool_func):
            tool_result = await tool_func(**tool_using_info["kwargs"])
        else:
            tool_result = tool_func(**tool_using_info["kwargs"])

        if data.get_state("print_process"):
            print("🎉 Result:", str(tool_result)[:200], "...")
        await data.async_put_into_stream(f"[status] tool done: {tool_name}\n")

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
        if data.get_state("print_process"):
            print("[💬 Ready to answer]")
            print("✅ Final answer:", reply_text)
        await data.async_put_into_stream("[status] reply ready\n")
        chat_history = data.get_state("chat_history") or []
        question = data.get_state("question")
        chat_history.append({"role": "user", "content": question})
        chat_history.append({"role": "assistant", "content": reply_text})
        await data.async_set_state("chat_history", chat_history)
        await data.async_emit("Loop", None)
        return reply_text

    async def update_memo(data: TriggerFlowRuntimeData):
        # Keep a runtime memo across turns for preferences, constraints, or facts.
        memo = data.get_state("memo") or []
        question = data.get_state("question")
        reply_text = data.input.get("reply", "")
        result = (
            agent.input({"question": question, "reply": reply_text, "memo": memo})
            .instruct(
                [
                    "Extract any stable preferences, constraints, or facts to remember.",
                    "Only keep items that are useful for future turns.",
                    "Return an updated memo list.",
                ]
            )
            .output({"memo": [(str, "Short memo item")]})
            .async_start()
        )
        result = await result
        new_memo = result.get("memo", []) if isinstance(result, dict) else []
        if new_memo:
            await data.async_set_state("memo", new_memo)
            await data.async_put_into_stream(f"[memo] {new_memo}\n")
        return {"type": "final", "reply": reply_text}

    flow.to(start_loop)
    flow.when("Loop").to(get_input)
    flow.when("UserInput").to(prepare_context).to(ensure_kb).to(make_next_plan)
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

    execution = flow.create_execution(auto_close=False)
    await execution.async_start("start")
    async for event in execution.get_async_runtime_stream(timeout=None):
        print(event, end="", flush=True)
    state = await execution.async_close()

    return state


if __name__ == "__main__":
    asyncio.run(auto_loop_demo())
