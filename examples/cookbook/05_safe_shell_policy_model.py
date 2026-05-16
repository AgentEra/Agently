from pathlib import Path
from pprint import pprint

from _shared_model import (
    create_model_agent,
    print_action_results,
    print_model_provider,
    print_model_reply,
)


def main():
    workspace = Path(__file__).resolve().parent
    agent, provider = create_model_agent(
        (
            "You are a shell-policy demonstration assistant. You must call the "
            "safe_shell action for shell work. First run pwd. Then intentionally "
            "try `ls` to demonstrate policy blocking. In the final answer, only "
            "describe commands that appear in the action records. Do not claim a "
            "fallback command was executed unless it appears in action results."
        ),
        temperature=0.0,
        max_rounds=4,
    )
    agent.enable_shell(
        root=workspace,
        commands=["pwd", "echo"],
        action_id="safe_shell",
        expose_to_model=True,
        timeout=5,
    )

    print_model_provider(provider)
    agent.input(
        "Demonstrate shell policy: run pwd, then try ls. Explain whether each command was allowed or blocked."
    )
    records = agent.get_action_result()
    print_action_results(records)

    response = agent.get_response()
    print_model_reply(response)

    statuses = [record.get("status") for record in records]
    errors = [record.get("error") for record in records]
    assert "success" in statuses
    assert "approval_required" in statuses
    assert "cmd_not_allowed" in errors


if __name__ == "__main__":
    main()

# Expected key output with DeepSeek or local Ollama configured:
# [MODEL_PROVIDER] prints deepseek or ollama.
# [ACTION_RECORDS] includes a successful pwd call and an approval_required ls call.
# The blocked ls record has error "cmd_not_allowed"; the command is not executed.
