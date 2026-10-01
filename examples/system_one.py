"""Run output templates with a dedicated SystemOne model through local oMLX.

Set OMLX_BASE_URL, OMLX_API_KEY and LLM_MODEL in dotenv. SYSTEM_ONE_MODEL
can override the default Qwen3.5-9B-MLX-4bit API model ID. Check /v1/models first.
No Jev credentials or calls are involved. Use --disabled for the ordinary LLM.

Expected key output from real local runs (estimates may vary):
  pure: urgent=1, team="billing", urgency=2
  dynamic: Ada urgent=1.0; Ben urgent=0.0; messages preserved
  custom: answer="Friday"
A mixed run exposed a lost score-rubric handoff; after preserving the template
contract, summary correctly described urgency=2 as maximum urgency. The limited
on/off comparison did not establish a total latency improvement.

Flow: pure/custom -> SystemOne; mixed -> SystemOne -> ordinary LLM;
dynamic -> ordinary LLM prerequisites -> SystemOne -> ordinary LLM summary.
The custom template shows that SystemOne output is not limited to three primitives.
"""

import argparse
import asyncio
import json
import os
from time import monotonic
from typing import Annotated

from dotenv import find_dotenv, load_dotenv
from pydantic import Field

from agently import Agently, Choice, OutputTemplate, Probability, Score


class ShortAnswer(OutputTemplate):
    def to_schema(self):
        return (Annotated[str, Field(min_length=1, max_length=160)], self.question, True, {"judgment": True})


async def run_case(mode: str, *, enabled: bool = True) -> dict:
    load_dotenv(find_dotenv(usecwd=True))
    connection = {"base_url": os.environ["OMLX_BASE_URL"], "api_key": os.environ["OMLX_API_KEY"]}
    agent = Agently.create_agent(f"system-one-{mode}")
    agent.set_settings(
        "OpenAICompatible",
        {
            **connection,
            "model": os.environ["LLM_MODEL"],
            "request_options": {
                "temperature": 0,
                "max_tokens": 1500,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        },
    )
    agent.set_settings(
        "system_one",
        {
            **connection,
            "provider": "OpenAICompatible",
            "model": os.getenv("SYSTEM_ONE_MODEL", "Qwen3.5-9B-MLX-4bit"),
            "request_options": {
                "temperature": 0,
                "max_tokens": 800,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        },
    )
    execution = agent.use_system_one(enabled)
    if mode == "dynamic":
        execution.input(
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
                            "Copy the source sentence supporting the record, consumed when forming the message and retained for audit.",
                        ),
                        "name": (str, "Copy the supplied name."),
                        "message": (str, "Copy the matching supplied message verbatim using evidence."),
                        "urgent": Probability(
                            "Does the message explicitly request a same-day response?",
                            from_output=["items[].name", "items[].message"],
                            after_output="items[].evidence",
                        ),
                    }
                ],
                "summary": (str, "Summarize both messages and their accepted urgency judgments in one sentence."),
            }
        )
    elif mode == "custom":
        execution.input("Delivery is confirmed for Friday; the customer asks for Thursday.").output(
            {
                "answer": ShortAnswer("State the confirmed date without promising the requested earlier delivery."),
            }
        )
    else:
        execution.input("All my payouts have failed for three days. Please fix this today.").output(
            {
                "urgent": Probability("Does the customer explicitly request resolution today?"),
                "team": Choice(
                    "Which department fits the reported issue?",
                    {
                        "billing": "Payments, invoices, refunds",
                        "sales": "Purchases and upgrades",
                        "technical": "Other software defects",
                    },
                ),
                "urgency": Score(
                    "How urgent is the request?",
                    ["No urgency expressed", "Moderately time-sensitive", "Explicit same-day request"],
                ),
                **(
                    {"summary": (str, "Summarize the supplied facts and accepted judgments in one sentence.")}
                    if mode == "mixed"
                    else {}
                ),
            }
        )
    started = monotonic()
    value = await execution.async_get_data(max_retries=0)
    return {
        "case": mode,
        "enabled": enabled,
        "result": value,
        "elapsed_seconds": monotonic() - started,
        "judgment": (await execution.async_get_meta()).get("judgment", {}),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["pure", "mixed", "dynamic", "custom"], default="mixed")
    parser.add_argument("--disabled", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_case(args.mode, enabled=not args.disabled)), ensure_ascii=False, indent=2))
