from _shared import create_deepseek_agent, print_action_results, print_response


agent = create_deepseek_agent(
    "Use the python sandbox action whenever exact computation or small data processing is needed."
)

agent.action.register_python_sandbox_action(
    action_id="python_table_stats",
    desc=(
        "Execute Python code inside a sandbox. "
        "Always store the final answer in a variable named `result`."
    ),
    expose_to_model=True,
)


if __name__ == "__main__":
    agent.use_actions("python_table_stats")
    agent.input(
        "Use the python sandbox action to compute the average and the max-minus-min gap "
        "for [15, 23, 42, 8, 12]. Return both numbers."
    )
    records = agent.get_action_result()
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes a successful python_table_stats call.
# The ActionResult has model_digest and artifact_refs for the Python code/output.
# The calculated values are average=20.0 and max-minus-min gap=34.
