"""Read an image with an independent VLM and reuse its structured result.

Set VLM_BASE_URL, VLM_MODEL, VLM_API_KEY (optional), and IMAGE_FILE.
Expected key output from a real note-image run:
location='greenhouse', evidence='Hammond is in the greenhouse.'
The note was a generated input fixture; the response came from the model.
With both vlm and llm configured, get_data() uses VLM -> LLM;
to_text() explicitly keeps the image operation on the VLM.
"""
import asyncio
import os

from dotenv import find_dotenv, load_dotenv
from agently import Agently


async def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    agent = Agently.create_agent()
    agent.set_settings("vlm", {
        "provider": "OpenAICompatible",
        "base_url": os.environ["VLM_BASE_URL"],
        "api_key": os.getenv("VLM_API_KEY", ""),
        "model": os.environ["VLM_MODEL"],
    })
    execution = (
        agent.image(os.environ["IMAGE_FILE"], question="Read the note carefully.")
        .input("Where is Hammond?")
        .output({"location": str, "evidence": str})
    )
    print(await execution.async_to_text())
    print(await execution.async_get_data())  # Same run, no second model request.


if __name__ == "__main__":
    asyncio.run(main())
