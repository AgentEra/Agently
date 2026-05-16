from pathlib import Path

from _shared import create_deepseek_agent, print_action_results, print_response


repo_root = Path(__file__).resolve().parents[2]
agent = create_deepseek_agent(
    "Use the bash sandbox action for filesystem inspection tasks. "
    "Prefer safe shell commands over guessed directory contents."
)

agent.action.register_bash_sandbox_action(
    action_id="repo_bash_inspector",
    desc=(
        "Run a shell command inside a constrained sandbox. "
        "Allowed commands are: pwd, ls, echo."
    ),
    expose_to_model=True,
    allowed_cmd_prefixes=["pwd", "ls", "echo"],
    allowed_workdir_roots=[str(repo_root)],
    default_policy={
        "workspace_roots": [str(repo_root)],
        "allowed_cmd_prefixes": ["pwd", "ls", "echo"],
        "timeout_seconds": 10,
    },
)


if __name__ == "__main__":
    agent.use_actions("repo_bash_inspector")
    agent.input(
        f"The repository root is `{repo_root}`. "
        "Use the bash sandbox action to run `pwd` in that directory, then list `examples/action_runtime`. "
        "Tell me the working directory and the example file names you found."
    )
    records = agent.get_action_result()
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes successful repo_bash_inspector calls.
# Each shell ActionResult includes model_digest and artifact_refs.
# The reply mentions the repository root and files under examples/action_runtime.
