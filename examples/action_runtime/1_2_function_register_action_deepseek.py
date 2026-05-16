from _shared import create_deepseek_agent, print_action_results, print_response


def normalize_title(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def count_words(text: str) -> int:
    return len(text.split())


agent = create_deepseek_agent(
    "Prefer exact string-processing actions over freehand rewriting when they are available."
)

agent.action.register_action(
    action_id="normalize_title",
    desc="Normalize whitespace, trim the edges, and convert the title to lowercase.",
    kwargs={"text": (str, "Title text to normalize.")},
    func=normalize_title,
    expose_to_model=True,
)

agent.action.register_action(
    action_id="count_words",
    desc="Count how many words are in the given text.",
    kwargs={"text": (str, "Text to count.")},
    func=count_words,
    expose_to_model=True,
)


if __name__ == "__main__":
    agent.use_actions(["normalize_title", "count_words"])
    agent.input(
        "Use the available actions on this title: `  Action   Runtime   Plugin   Refactor  `. "
        "Return the normalized title and the exact word count."
    )
    records = agent.get_action_result()
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes successful normalize_title and count_words calls.
# The normalized title is "action runtime plugin refactor".
# The exact word count is 4.
