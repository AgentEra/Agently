from agently import Agently

from _shared import create_agent, print_action_results, print_response


ISSUE_PAYLOAD = {
    "title": "TriggerFlow stream stops after human approval",
    "body": (
        "After upgrading to 4.1, a TriggerFlow execution pauses for approval correctly, "
        "but after resume the runtime stream stops emitting chunk status updates. "
        "The workflow result is correct, but devtools cannot show the rest of the run."
    ),
    "labels": ["bug", "triggerflow", "devtools"],
    "comments": [
        "Reproduced with a two-step flow and async_continue_with.",
        "No issue when runtime stream is disabled.",
        "Expected stream events after approval resume.",
    ],
}


agent = create_agent(
    "deepseek",
    (
        "You are an issue processor for Agently. "
        "Use the available Python action for deterministic counting and scoring before replying. "
        "Do not invent metrics without calling the action."
    ),
    temperature=0.1,
)

agent.enable_python(
    desc="Calculate deterministic GitHub issue metrics from provided issue text and labels. Assign metrics to `result`.",
    expose_to_model=True,
)


if __name__ == "__main__":
    agent.input(
        {
            "task": (
                "Process this GitHub issue. First call the Python action to compute: "
                "label_count, comment_count, whether TriggerFlow is involved, whether DevTools is involved, "
                "and a severity score where bug=3, triggerflow=2, devtools=1. "
                "The Python code must assign a dict to `result` with keys label_count, comment_count, "
                "triggerflow_involved, devtools_involved, and severity_score. "
                "Then reply with a triage summary, suggested owner, and next debugging step."
            ),
            "issue": ISSUE_PAYLOAD,
        }
    )

    records = agent.get_action_result()
    print_action_results(records)

    response = agent.get_response()
    print_response(response)

    print("[ACTION_CALL_HANDLES_AFTER_RELEASE]")
    print(Agently.execution_environment.list(scope="action_call"))

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes a successful run_python call with deterministic issue metrics.
# The metrics include label_count=3, comment_count=3, triggerflow_involved=True,
# devtools_involved=True, and severity_score=6.
# [ACTION_CALL_HANDLES_AFTER_RELEASE] prints [].
