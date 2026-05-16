from _shared import create_deepseek_agent, print_action_results, print_response


agent = create_deepseek_agent(
    "Use available actions whenever they can produce an exact answer. "
    "For arithmetic, call the action before you reply."
)


@agent.action_func
def add_invoice_amounts(first_amount: int, second_amount: int) -> int:
    """Add two invoice amounts and return the exact integer total."""
    return first_amount + second_amount


if __name__ == "__main__":
    agent.use_actions(add_invoice_amounts)
    agent.input(
        "Use the action to calculate 17850 + 42675, then answer with the total and one short sentence."
    )
    records = agent.get_action_result()
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes a successful add_invoice_amounts call with result 60525.
# [ACTION_RESULTS_INJECTED_TO_REPLY] contains {"Add two invoice amounts...": 60525}.
# [MODEL_REPLY] mentions the total 60525.
