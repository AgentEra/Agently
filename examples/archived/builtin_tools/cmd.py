import asyncio
from pathlib import Path
from agently.builtins.actions import Cmd

cmd = Cmd(
    allowed_cmd_prefixes=["ls", "rg", "cat", "pwd"],
    allowed_workdir_roots=["/Users/moxin/dev/public/Agently"],
    timeout=20,
)


async def main():
    result = await cmd.run(f"ls { Path(__file__).parent.resolve() }")
    print(result)
    return result


asyncio.run(main())
