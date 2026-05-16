import asyncio
import os

from agently.builtins.actions import Browse

TARGET_URL = os.getenv("PLAYWRIGHT_TARGET_URL", "https://github.com/AgentEra/Agently")
SCREENSHOT_PATH = os.getenv("PLAYWRIGHT_SCREENSHOT_PATH")

playwright_tool = Browse(
    enable_pyautogui=False,
    enable_playwright=True,
    enable_bs4=True,
    playwright_headless=True,
    response_mode="markdown",
    max_content_length=8000,
    playwright_include_links=False,
    playwright_screenshot_path=SCREENSHOT_PATH,
)


async def main():
    result = await playwright_tool.browse(url=TARGET_URL)
    print("[BROWSE]")
    print(result)


asyncio.run(main())
