from agently import Agently

from _shared import create_agent, print_action_results, print_response


agent = create_agent(
    "ollama",
    (
        "You are an exact calculation assistant. "
        "Call the available Python action for exact list statistics before replying. "
        "Do not include URLs or source links in the reply."
    ),
)

agent.enable_python(
    desc="Use for exact arithmetic and list statistics. Assign the final answer to `result`.",
    expose_to_model=True,
)


if __name__ == "__main__":
    agent.input(
        "Use Python for this list: [15, 23, 42, 8, 12]. "
        "Calculate average, count, and max-minus-min gap. "
        "The Python code must assign a dict to `result` with keys average, count, and max_minus_min_gap. "
        "Then reply with those values."
    )

    records = agent.get_action_result()
    print_action_results(records)

    response = agent.get_response()
    print_response(response)

    print("[ACTION_CALL_HANDLES_AFTER_RELEASE]")
    print(Agently.execution_environment.list(scope="action_call"))

# Expected key output with Ollama running:
# [ACTION_RECORDS] includes a successful run_python call with model_digest and artifact_refs.
# [ACTION_RESULTS_INJECTED_TO_REPLY] contains average=20.0, count=5, and max_minus_min_gap=34.
# [ACTION_CALL_HANDLES_AFTER_RELEASE] prints [].
