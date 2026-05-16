import asyncio
from pprint import pprint

from agently import TriggerFlow

from _shared_model import configure_model, print_model_provider


def generate_draft(task: str) -> str:
    from agently import Agently

    data = (
        Agently.create_agent()
        .input(task)
        .instruct("Write the first draft. Keep it concise.")
        .output({"result": ("str", "first draft")})
        .get_response()
        .get_data(ensure_keys=["result"])
    )
    return data["result"]


def evaluate_draft(task: str, draft: str, criteria: list[str]) -> dict:
    from agently import Agently

    return (
        Agently.create_agent()
        .input({"task": task, "draft": draft})
        .info({"criteria": criteria})
        .instruct("Evaluate the draft against every criterion. Be strict but fair.")
        .output({
            "passed": ("bool", "whether the draft satisfies all criteria"),
            "score": ("float", "0 to 1 quality score"),
            "issues": (["str"], "concrete issues; empty if passed"),
            "suggestions": (["str"], "specific revision suggestions; empty if passed"),
        })
        .get_response()
        .get_data(ensure_keys=["passed", "score", "issues", "suggestions"])
    )


def revise_draft(task: str, draft: str, suggestions: list[str]) -> str:
    from agently import Agently

    data = (
        Agently.create_agent()
        .input({"task": task, "draft": draft})
        .info({"suggestions": suggestions})
        .instruct([
            "Revise the draft according to the suggestions.",
            "Preserve what already works.",
            "Keep the result under 80 English words.",
        ])
        .output({"result": ("str", "revised draft")})
        .get_response()
        .get_data(ensure_keys=["result"])
    )
    return data["result"]


def build_flow(max_rounds: int = 3):
    flow = TriggerFlow(name="cookbook-reflection-loop-model")

    async def generate(data):
        state = data.input
        draft = generate_draft(state["task"])
        data.emit_nowait("Evaluate", {**state, "draft": draft, "round": 0})

    async def evaluate(data):
        state = data.input
        round_no = state["round"] + 1
        critique = evaluate_draft(state["task"], state["draft"], state["criteria"])
        history = state.get("history", []) + [{"round": round_no, **critique}]

        if critique["passed"] or round_no >= max_rounds:
            await data.async_set_state("final_result", state["draft"], emit=False)
            await data.async_set_state("total_rounds", round_no, emit=False)
            await data.async_set_state("history", history, emit=False)
            return

        data.emit_nowait(
            "Revise",
            {**state, "round": round_no, "critique": critique, "history": history},
        )

    async def revise(data):
        state = data.input
        draft = revise_draft(state["task"], state["draft"], state["critique"]["suggestions"])
        data.emit_nowait("Evaluate", {**state, "draft": draft})

    flow.to(generate)
    flow.when("Evaluate").to(evaluate)
    flow.when("Revise").to(revise)
    return flow


async def main_async():
    provider = configure_model(temperature=0.0)
    print_model_provider(provider)

    flow = build_flow(max_rounds=3)
    execution = flow.create_execution(auto_close=False)
    await execution.async_start({
        "task": "Write a course intro for engineers learning AI app development.",
        "criteria": [
            "Clear audience: engineers",
            "Mentions AI app development workflows",
            "States one concrete learner benefit",
            "Avoids exaggerated marketing claims",
        ],
    })
    state = await execution.async_close(timeout=30)

    print("[REFLECTION_RESULT]")
    pprint({
        "total_rounds": state["total_rounds"],
        "final_result": state["final_result"],
        "history": state["history"],
    })

    assert state["total_rounds"] >= 1
    assert state["final_result"]
    assert state["history"]


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

# Expected key output with DeepSeek or local Ollama configured:
# [MODEL_PROVIDER] prints deepseek or ollama.
# [REFLECTION_RESULT] contains model-generated critique history.
# final_result is generated or revised by the model, not by a hand-written fallback.
