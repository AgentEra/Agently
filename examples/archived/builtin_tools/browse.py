import asyncio
from agently.builtins.actions import Browse

browse = Browse()


async def directly_browse():
    result = await browse.browse("https://arxiv.org/abs/1706.03762")
    print("[BROWSE]:")
    print(result)


asyncio.run(directly_browse())
