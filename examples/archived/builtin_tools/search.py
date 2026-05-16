import asyncio
from agently.builtins.actions import Search

search = Search(
    proxy="http://127.0.0.1:7890",
    region="us-en",
    options={"safesearch": "on"},
)


async def directly_search():
    results = await search.search("attention is all you need")
    print("[SEARCH]:")
    print(results)

    results = await search.search_news("attention is all you need")
    print("[NEWS]:")
    print(results)

    results = await search.search_wikipedia("attention is all you need")
    print("[WIKIPEDIA]:")
    print(results)

    results = await search.search_arxiv("attention is all you need")
    print("[ARXIV]:")
    print(results)


asyncio.run(directly_search())

import os
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": os.environ["DEEPSEEK_BASE_URL"],
        "model": os.environ["DEEPSEEK_DEFAULT_MODEL"],
        "model_type": "chat",
        "auth": os.environ["DEEPSEEK_API_KEY"],
    },
)

agent = Agently.create_agent()

agent.use_actions(search)

response = agent.input("Search news about language model applications.").get_response()
print(response.result.get_data())
extra = response.result.full_result_data.get("extra") or {}
print(extra.get("action_logs", extra.get("tool_logs", [])))
