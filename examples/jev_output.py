"""Real Jev output judgments; install this development build before running.

Set JEV_API_KEY in your dotenv file. Pure mode needs no LLM configuration.
For --dynamic, also set LLM_BASE_URL, LLM_MODEL and optionally LLM_API_KEY.

Expected key output from one real run (probabilities may vary):
  pure: urgent=0.98, team='billing', urgency=2.0
  dynamic: Ada urgent=0.94; Ben urgent=0.05; names/messages unchanged.

Pure: output leaves -> one Jev batch -> Host output.
Dynamic: LLM evidence/records -> Jev batch -> LLM summary -> Host merge.
No generated interpretation is substituted with a canned answer.
"""

import argparse
import asyncio
import json
import os

from dotenv import find_dotenv, load_dotenv

from agently import Agently, Choice, Probability, Score


async def main(dynamic: bool) -> None:
    load_dotenv(find_dotenv(usecwd=True))
    agent = Agently.create_agent("jev-output-example")
    agent.set_settings("Jev", {"api_key": os.environ["JEV_API_KEY"]})
    agent.set_settings("system_one", {"provider": "Jev"})
    if dynamic:
        agent.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.environ["LLM_BASE_URL"],
                "model": os.environ["LLM_MODEL"],
                "api_key": os.getenv("LLM_API_KEY"),
            },
        )
        execution = agent.input(
            [
                {"name": "Ada", "message": "Please fix my failed payout today."},
                {"name": "Ben", "message": "Please send the product brochure; there is no deadline."},
            ]
        ).output(
            {
                "items": [
                    {
                        "evidence": (
                            str,
                            "Copy the source sentence supporting this record; visible audit evidence consumed when copying the message.",
                        ),
                        "name": (str, "Copy each supplied name."),
                        "message": (str, "Copy the same supplied message verbatim."),
                        "urgent": Probability(
                            "Does this message explicitly request a same-day response?",
                            from_output=["items[].name", "items[].message"],
                            after_output="items[].evidence",
                        ),
                    }
                ],
                "summary": (str, "Summarize the fixed records and their accepted urgent judgments in one sentence."),
            }
        )
    else:
        execution = agent.input(
            "Customer report: All my payouts have failed for three days. Please fix this today."
        ).output(
            {
                "urgent": Probability("Does the customer explicitly request resolution today?"),
                "team": Choice(
                    "Which department fits the reported problem?",
                    {
                        "billing": "Payments, invoices, refunds",
                        "sales": "Purchases and upgrades",
                        "technical": "Other software defects",
                    },
                ),
                "urgency": Score(
                    "How urgent is the request?",
                    [
                        "No urgency expressed",
                        "Moderately time-sensitive",
                        "Explicit same-day request",
                    ],
                ),
            }
        )
    value = await execution.async_get_data(max_retries=0)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    print(json.dumps((await execution.async_get_meta()).get("judgment", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dynamic", action="store_true")
    asyncio.run(main(parser.parse_args().dynamic))
