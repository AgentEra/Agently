from pprint import pprint

from agently import Agently

from _shared_model import configure_model, print_model_provider


def plan_next_step(history: list[dict]) -> dict:
    response = (
        Agently.create_agent()
        .input({
            "task": "Calculate (12 + 7) * 3.",
            "history": history,
            "available_actions": [
                {"action_id": "add", "kwargs": {"a": "int", "b": "int"}},
                {"action_id": "multiply", "kwargs": {"a": "int", "b": "int"}},
            ],
        })
        .instruct([
            "You are the planner in a plan -> action -> observe loop.",
            "If history is empty, choose action add with a=12 and b=7.",
            "If history contains add result 19 but not multiply result 57, choose action multiply with a=19 and b=3.",
            "If history already contains multiply result 57, choose final and answer '(12 + 7) * 3 = 57'.",
            "Do not skip action execution.",
        ])
        .output({
            "type": ("'action' | 'final'", "next step type"),
            "action_id": ("'add' | 'multiply' | null", "action to execute when type is action"),
            "purpose": ("str | null", "why this action is needed"),
            "action_input": ("dict | null", "kwargs for the action"),
            "answer": ("str | null", "final answer when type is final"),
        })
        .get_response()
    )
    return response.get_data(ensure_keys=["type"])


def main():
    provider = configure_model(temperature=0.0)
    print_model_provider(provider)

    agent = Agently.create_agent()

    @agent.action_func
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @agent.action_func
    def multiply(a: int, b: int) -> int:
        """Multiply two integers."""
        return a * b

    agent.use_actions([add, multiply])

    history: list[dict] = []
    records: list[dict] = []
    answer = ""

    for round_no in range(1, 5):
        decision = plan_next_step(history)
        print(f"[MODEL_DECISION_ROUND_{ round_no }]")
        pprint(decision)

        if decision["type"] == "final":
            answer = decision.get("answer") or ""
            break

        action_id = decision["action_id"]
        action_input = decision["action_input"] or {}
        record = agent.action.execute_action(
            action_id,
            action_input,
            purpose=decision.get("purpose") or f"Use { action_id }",
        )
        records.append(record)
        history.append({
            "action_id": record["action_id"],
            "kwargs": record["kwargs"],
            "result": record["result"],
            "status": record["status"],
        })

    print("[ACTION_HISTORY]")
    pprint(history)
    print("[FINAL_ANSWER]")
    print(answer)

    results = [record.get("result") for record in records]
    assert 19 in results
    assert 57 in results
    assert "57" in answer


if __name__ == "__main__":
    main()

# Expected key output with DeepSeek or local Ollama configured:
# [MODEL_PROVIDER] prints deepseek or ollama.
# [MODEL_DECISION_ROUND_*] prints real model planner decisions.
# One action result is 19 and one action result is 57.
# [FINAL_ANSWER] states the final result is 57.
