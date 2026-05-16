import asyncio
import os

from agently.builtins.actions import Browse

TARGET_URL = os.getenv("PYAUTOGUI_TARGET_URL", "https://github.com/AgentEra/Agently")
RUN_REAL = os.getenv("RUN_PYAUTOGUI", "1") == "1"
OPEN_MODE = os.getenv("PYAUTOGUI_OPEN_MODE", "hotkey").strip().lower()
if OPEN_MODE not in ("hotkey", "system"):
    OPEN_MODE = "hotkey"
BROWSER_APP = os.getenv("PYAUTOGUI_BROWSER_APP", "Google Chrome").strip() or None
ACTIVATE_BROWSER = os.getenv("PYAUTOGUI_ACTIVATE_BROWSER", "1" if OPEN_MODE == "hotkey" else "0") == "1"
ACTION = os.getenv("PYAUTOGUI_ACTION", "browse").strip().lower()
if ACTION not in ("browse",):
    ACTION = "browse"

pyautogui_tool = Browse(
    enable_pyautogui=True,
    enable_playwright=True,
    enable_bs4=True,
    pyautogui_new_tab=True,
    pyautogui_wait_seconds=1.5,
    pyautogui_dry_run=not RUN_REAL,
    pyautogui_open_mode=OPEN_MODE,
    pyautogui_activate_browser=ACTIVATE_BROWSER,
    pyautogui_browser_app=BROWSER_APP,
)


async def main():
    print(
        "[PYAUTOGUI_CONFIG]",
        {
            "run_real": RUN_REAL,
            "open_mode": OPEN_MODE,
            "activate_browser": ACTIVATE_BROWSER,
            "browser_app": BROWSER_APP,
            "action": ACTION,
            "target_url": TARGET_URL,
        },
    )
    result = await pyautogui_tool.browse(TARGET_URL)
    print("[BROWSE]")
    print(result)


asyncio.run(main())
