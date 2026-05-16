import asyncio
from pprint import pprint

from agently import TriggerFlow

from _shared_model import configure_model, print_model_provider


def classify_intent(user_input: str) -> dict:
    from agently import Agently

    response = (
        Agently.create_agent()
        .input(user_input)
        .instruct([
            "Classify the user request into exactly one route.",
            "Use weather for weather questions.",
            "Use exchange for currency and exchange-rate questions.",
            "Use travel_plan for itinerary or travel planning questions.",
            "Use general for everything else.",
        ])
        .output({
            "route": ("'weather' | 'exchange' | 'travel_plan' | 'general'", "selected route"),
            "confidence": ("float", "0 to 1 classification confidence"),
            "reason": ("str", "short reason"),
        })
        .get_response()
    )
    return response.get_data(ensure_keys=["route", "confidence", "reason"])


async def model_answer(question: str, role: str) -> str:
    from agently import Agently

    return (
        await Agently.create_agent()
        .input(question)
        .instruct([
            f"Answer as a focused {role} assistant.",
            "Keep the answer to one concise sentence.",
            "Do not claim live data access.",
        ])
        .async_get_text()
    )


async def handle_weather(data):
    question = data.get_state("question", "")
    await data.async_set_state("answer", await model_answer(question, "weather"))
    await data.async_set_state("route_label", "weather")


async def handle_exchange(data):
    question = data.get_state("question", "")
    await data.async_set_state("answer", await model_answer(question, "exchange-rate"))
    await data.async_set_state("route_label", "exchange")


async def handle_travel(data):
    question = data.get_state("question", "")
    await data.async_set_state("answer", await model_answer(question, "travel planning"))
    await data.async_set_state("route_label", "travel_plan")


async def handle_general(data):
    question = data.get_state("question", "")
    await data.async_set_state("answer", await model_answer(question, "general"))
    await data.async_set_state("route_label", "general")


async def handle_low_confidence(data):
    question = data.get_state("question", "")
    await data.async_set_state("answer", await model_answer(question, "clarifying"))
    await data.async_set_state("route_label", "low_confidence")


def build_flow():
    flow = TriggerFlow(name="cookbook-router-model")

    async def route_step(data):
        intent = classify_intent(data.input)
        await data.async_set_state("question", data.input, emit=False)
        await data.async_set_state("intent", intent, emit=False)

    async def dispatch_step(data):
        intent = data.get_state("intent", {})
        if intent.get("confidence", 0) < 0.6:
            await handle_low_confidence(data)
            return

        handlers = {
            "weather": handle_weather,
            "exchange": handle_exchange,
            "travel_plan": handle_travel,
            "general": handle_general,
        }
        await handlers.get(intent.get("route"), handle_general)(data)

    flow.to(route_step).to(dispatch_step)
    return flow


async def main_async():
    provider = configure_model(temperature=0.0)
    print_model_provider(provider)

    flow = build_flow()
    results = []
    for question in [
        "Will it rain in Tokyo tomorrow?",
        "What does USD to CNY exchange rate mean for a 100 USD budget?",
        "Plan a three day travel route for Kyoto.",
        "Will AI change software engineering?",
    ]:
        execution = flow.create_execution(auto_close_timeout=0.0)
        await execution.async_start(question)
        state = await execution.async_close()
        results.append({
            "question": question,
            "intent": state["intent"],
            "route": state["route_label"],
            "answer": state["answer"],
        })

    print("[ROUTE_RESULTS]")
    pprint(results)
    assert [item["route"] for item in results] == [
        "weather",
        "exchange",
        "travel_plan",
        "general",
    ]


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

# Expected key output with DeepSeek or local Ollama configured:
# [MODEL_PROVIDER] prints deepseek or ollama.
# [ROUTE_RESULTS] contains model-generated intent objects and model-generated branch answers.
# The route labels are weather, exchange, travel_plan, and general.
