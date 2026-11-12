"""Real-model, structured long-task example (4.1.4.9 development line / 4.2).

Configure OMLX_BASE_URL, OMLX_MODEL and OMLX_API_KEY in the environment or .env.
The local CSV is synthetic input; reading, model decisions and output parsing are real.

Flow: decide -> read Action -> decide -> original typed result.
Expected key output from a local Qwen3.8-27B-4bit run:
    confirmed=['安', '白', '邓', '郭'], pending=['方']
No per-card evaluator, final judge or application-local orchestration is needed.
"""
import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import load_dotenv
from pydantic import BaseModel

from agently import Agently


class Roster(BaseModel):
    confirmed: list[str]
    pending: list[str]


async def main() -> None:
    load_dotenv()
    agent = Agently.create_agent()
    agent.set_settings("plugins.ModelRequester.OpenAICompatible", {
        "base_url": os.environ["OMLX_BASE_URL"], "model": os.environ["OMLX_MODEL"],
        "auth": os.environ["OMLX_API_KEY"], "stream": False,
        "request_options": {"temperature": 0, "max_tokens": 8192,
                            "chat_template_kwargs": {"enable_thinking": False}},
    })
    with TemporaryDirectory(prefix="agently-roster-") as directory:
        root = Path(directory)
        (root / "roster.csv").write_text(
            "name,attendance\n安,confirmed\n白,confirmed\n陈,declined\n邓,confirmed\n方,pending\n郭,confirmed\n",
            encoding="utf-8",
        )
        agent.use_task_workspace(root, mode="read_only")

        @agent.action_func
        async def read_roster() -> str:
            """Read the current roster.csv source, including its attendance status."""
            return (root / "roster.csv").read_text(encoding="utf-8")

        execution = (
            agent.create_execution("long_task", limits={"max_model_requests": 6})
            .input("读取 roster.csv，返回已确认参加者和待确认者的姓名列表。未参加者不放入上述列表。")
            .use_actions("read_roster")
            .output(Roster)
        )
        # Source content is supplied by the Action, without a second optional source selector.
        execution.options["context_budget"] = {"optional_selection": "none"}
        print(await execution.async_get_data_object())
        await execution.async_close()


if __name__ == "__main__":
    asyncio.run(main())
