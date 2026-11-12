"""Run a two-round Action loop with a Chat Completions model that supports tools."""

import os

from agently import Agently


def main() -> None:
    agent = Agently.create_agent()
    agent.set_settings("plugins.ModelRequester.activate", "OpenAICompatible")
    agent.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.environ["NATIVE_TOOLS_BASE_URL"],
            "model": os.environ["NATIVE_TOOLS_MODEL"],
            "auth": os.environ["NATIVE_TOOLS_API_KEY"],
            "stream": os.getenv("NATIVE_TOOLS_STREAM", "1") != "0",
            "request_retry": False,
        },
    )
    agent.set_action_loop(enabled=True, planning_protocol="native_tool_calls", max_rounds=2, timeout=90)

    calls: list[tuple[str, str]] = []
    invoice_amounts = {"INV-14": 17850, "INV-27": 42675}

    @agent.action_func
    def add_invoice_amounts(first_invoice: str, second_invoice: str) -> int:
        """Read two invoices by ID and add their amounts."""
        calls.append((first_invoice, second_invoice))
        return invoice_amounts[first_invoice] + invoice_amounts[second_invoice]

    agent.use_actions(add_invoice_amounts)
    turn = agent.input(
        "Use add_invoice_amounts once to calculate the total of invoices INV-14 and INV-27. "
        "After receiving the tool result, give the total in one short sentence."
    )
    records = agent.get_action_result(prompt=turn.prompt)
    assert any(record.get("status") == "success" for record in records)
    result = turn.get_result()
    print("Action calls:", calls)
    print("Final reply:", result.get_text())
    assert len(calls) == 1 and sorted(calls[0]) == ["INV-14", "INV-27"]
    assert "60525" in result.get_text().replace(",", "")


if __name__ == "__main__":
    main()

# Expected key output from oMLX with Qwen3.8-27B-4bit, streaming on or off:
# Action calls: [('INV-14', 'INV-27')]
# Final reply contains 60525 (possibly formatted as 60,525).
