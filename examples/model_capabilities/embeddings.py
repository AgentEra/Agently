"""Explicit embeddings without changing the main LLM configuration.

Set EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_API_KEY (optional).
Expected key output observed with bge-m3-mlx-fp16: 2 vectors, dimensions [1024, 1024].
Other models may use different dimensions. Existing indexes must retain their
own matching embedding-model identity; this operation does not migrate them.
"""
import asyncio
import os

from dotenv import find_dotenv, load_dotenv
from agently import Agently


async def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    agent = Agently.create_agent()
    agent.set_settings("embeddings", {
        "provider": "OpenAICompatible", "base_url": os.environ["EMBEDDING_BASE_URL"],
        "api_key": os.getenv("EMBEDDING_API_KEY", ""), "model": os.environ["EMBEDDING_MODEL"],
    })
    vectors = await agent.async_embed(["A document about gardening.", "A document about libraries."])
    print(len(vectors), "vectors, dimensions", [len(vector) for vector in vectors])


if __name__ == "__main__":
    asyncio.run(main())
