import shutil
from pprint import pprint

from agently import Agently


ACTION_ID = "run_nodejs_example"


def build_agent():
    agent = Agently.create_agent()
    agent.enable_nodejs(action_id=ACTION_ID, expose_to_model=False)
    return agent


def main():
    if shutil.which("node") is None:
        print("[SKIP] Node.js is not installed or not on PATH.")
        return

    agent = build_agent()
    result = agent.action.execute_action(
        ACTION_ID,
        {
            "js_code": [
                "const numbers = [4, 8, 15, 16, 23, 42];",
                "const sum = numbers.reduce((total, item) => total + item, 0);",
                "console.log(JSON.stringify({ count: numbers.length, sum }));",
            ]
        },
    )

    print("[ACTION_RESULT]")
    pprint(result)
    assert result.get("status") == "success"
    assert '"sum":108' in str(result.get("data", {}).get("stdout", "")).replace(" ", "")
    assert Agently.execution_environment.list(scope="action_call") == []


if __name__ == "__main__":
    main()

# Expected key output with Node.js installed:
# [ACTION_RESULT] has status="success".
# stdout contains {"count":6,"sum":108}.
# Action-call execution environment handles are released after the call.
