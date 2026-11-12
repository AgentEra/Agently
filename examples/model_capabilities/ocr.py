"""Dedicated OCR through an OpenAI-compatible service, optionally followed by LLM.

Set IMAGE_FILE, OCR_BASE_URL, OCR_MODEL and optional OCR_API_KEY.
Set LLM_BASE_URL/LLM_MODEL/LLM_API_KEY to also run the structured answer path.
OCR_MODEL must be the ID returned by your service's /v1/models endpoint.
The OCR reads the image; only the LLM receives the overall question/output schema.
This example disables repair retries to keep its model-call count explicit:
one OCR call for extraction; one OCR plus one LLM for the separate answer task.

Expected key output (local GLM-OCR-bf16 / PaddleOCR-VL-1.6 run):
For a note containing "Hammond is in the greenhouse.", extraction preserves
that sentence; with Qwen3.8-27B-4bit the answer field is "in the greenhouse".
These are observed fixture results, not instructions supplied to the models.
"""
import asyncio
import os

from dotenv import find_dotenv, load_dotenv
from agently import Agently


async def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    agent = Agently.create_agent()
    agent.set_settings("ocr", {
        "provider": "OpenAICompatible",
        "base_url": os.environ["OCR_BASE_URL"],
        "api_key": os.getenv("OCR_API_KEY", ""),
        "model": os.environ["OCR_MODEL"],
        "request_options": {"temperature": 0, "max_tokens": 2048},
    })
    extraction = agent.image(os.environ["IMAGE_FILE"], mode="ocr")
    # Select direct extraction, then consume its one cached result.
    print(await extraction.async_to_text(max_retries=0))
    if os.getenv("LLM_MODEL"):
        agent.set_settings("llm", {
            "provider": "OpenAICompatible",
            "base_url": os.environ["LLM_BASE_URL"],
            "api_key": os.getenv("LLM_API_KEY", ""),
            "model": os.environ["LLM_MODEL"],
            "request_options": {"temperature": 0, "max_tokens": 1500},
        })
        answer = await (
            agent.image(os.environ["IMAGE_FILE"], mode="ocr")
            .input(os.getenv("OCR_QUESTION", "Where is Hammond?"))
            .output({"answer": str, "evidence": str})
            .async_get_data(max_retries=0)
        )
        print(answer)


if __name__ == "__main__":
    asyncio.run(main())
