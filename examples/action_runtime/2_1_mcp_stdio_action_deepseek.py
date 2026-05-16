from pathlib import Path

from _shared import create_deepseek_agent, print_action_results, print_response


agent = create_deepseek_agent(
    "Use MCP actions for exact calculations. Prefer calling the tools over mental math."
)


if __name__ == "__main__":
    server_script = Path(__file__).with_name("_calculator_mcp_server.py")
    agent.use_mcp(str(server_script))

    agent.input(
        "Use the MCP actions to compute (12.5 + 7.25) * 3, then answer with the exact result."
    )
    records = agent.get_action_result()
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes MCP calculator action calls.
# The final computed value for (12.5 + 7.25) * 3 is 59.25.
# [MODEL_REPLY] reports 59.25.
