"""Custom output template using an ordinary LLM, with no Jev dependency.

Set LLM_BASE_URL, LLM_MODEL and optionally LLM_API_KEY in dotenv.
The ShortAnswer template is an extension example, not a built-in Agently type.
The source facts, intermediate evidence and summary are model-produced outputs.
Expected key output from a real run:
  answer="The delivery is confirmed for Friday. We cannot promise an earlier arrival on Thursday."
Expected contract: answer is a non-empty string of at most 160 characters;
review_notes are produced before answer but are not its explicit bound target.
Configure a supported no-reasoning option through normal provider settings;
this example does not guess a vendor-specific parameter or promise latency.
"""

import os
from typing import Annotated

from dotenv import find_dotenv, load_dotenv
from pydantic import Field

from agently import Agently, OutputTemplate


class ShortAnswer(OutputTemplate):
    def to_schema(self):
        return (Annotated[str, Field(min_length=1, max_length=160)], self.question, True, {"judgment": True})


def main():
    load_dotenv(find_dotenv(usecwd=True))
    agent = Agently.create_agent("output-template-example")
    agent.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.environ["LLM_BASE_URL"],
            "model": os.environ["LLM_MODEL"],
            "api_key": os.getenv("LLM_API_KEY"),
        },
    )
    agent.set_settings("system_one.enabled", False)
    execution = agent.input(
        "The delivery is scheduled for Friday. The customer asks whether it can arrive Thursday."
    ).output(
        {
            "review_notes": (
                str,
                "State which delivery date is confirmed and which is only requested; used to form facts and retained for audit.",
            ),
            "facts": (str, "Summarize the confirmed date and customer request, using review_notes."),
            "answer": ShortAnswer(
                "Briefly state the confirmed date; do not promise an earlier arrival.",
                from_output=["facts"],
                after_output=["review_notes"],
            ),
        }
    )
    print(execution.get_data(max_retries=0))


if __name__ == "__main__":
    main()
