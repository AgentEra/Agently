# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
from typing import Any, Literal
import json
import os
import platform
import re
import subprocess
import time
import unicodedata
import webbrowser
from urllib.parse import urljoin, urlparse

from agently.utils import LazyImport

_URL_PUNCT_TRANSLATION = str.maketrans(
    {
        "。": ".",
        "，": ",",
        "；": ";",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "《": "<",
        "》": ">",
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "、": "/",
    }
)


class Browse:
    PRIMARY_CONTENT_SELECTORS = (
        "[data-agently-main]",
        '[data-testid="markdown-body"]',
        '[data-testid="issue-body"]',
        '[data-testid="issue-viewer-issue-container"]',
        ".markdown-body",
        ".repository-content .markdown-body",
        ".repository-content .Box-body",
        ".js-issue-title + div",
        ".entry-content",
        ".post-content",
        ".article-content",
        ".article__content",
        ".article-body",
        ".story-body",
        ".news-article-body",
        ".caas-body",
        ".rich_media_content",
        ".theme-doc-markdown",
        ".theme-doc-markdown.markdown",
        ".docMainContainer",
        ".content__article-body",
        ".article-main",
        ".main-content",
        "main .vp-doc",
        "article .vp-doc",
        ".vp-doc",
        ".markdown",
        "main article",
        "article",
        "main",
        '[role="main"]',
        "#content",
        ".content",
        ".markdown-body",
    )

    CONTENT_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "td", "th", "blockquote")

    REMOVE_TAGS_STRICT = ("script", "style", "noscript", "svg", "nav", "aside", "footer", "header", "form")

    REMOVE_TAGS_RELAXED = ("script", "style", "noscript", "svg")

    NOISE_KEYWORDS = (
        "sidebar",
        "toc",
        "table-of-contents",
        "breadcrumb",
        "pagination",
        "pager",
        "navbar",
        "menu",
        "nav",
        "footer",
        "header",
        "ads",
        "advert",
    )

    BS4_STRATEGY_MIN_LENGTH = 20

    def __init__(
        self,
        proxy: str | None = None,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
        *,
        fallback_order: tuple[str, ...] = ("pyautogui", "playwright", "bs4"),
        enable_pyautogui: bool = False,
        enable_playwright: bool = True,
        enable_bs4: bool = True,
        response_mode: Literal["markdown", "text"] = "markdown",
        max_content_length: int = 12000,
        min_content_length: int = 40,
        pyautogui_pause: float = 0.05,
        pyautogui_fail_safe: bool = True,
        pyautogui_new_tab: bool = True,
        pyautogui_wait_seconds: float = 1.5,
        pyautogui_dry_run: bool = False,
        pyautogui_type_interval: float = 0.01,
        pyautogui_open_mode: Literal["hotkey", "system"] = "hotkey",
        pyautogui_activate_browser: bool = False,
        pyautogui_browser_app: str | None = None,
        pyautogui_activate_wait_seconds: float = 0.4,
        pyautogui_read_wait_seconds: float = 0.4,
        playwright_headless: bool = True,
        playwright_timeout: int = 30000,
        playwright_user_agent: str | None = None,
        playwright_include_links: bool = False,
        playwright_max_links: int = 120,
        playwright_screenshot_path: str | None = None,
        use_browser_environment: bool = False,
        browser_environment_config: dict[str, Any] | None = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = (
            headers
            if headers is not None
            else {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            }
        )

        self.fallback_order = tuple(item.strip().lower() for item in fallback_order if str(item).strip())
        self.enable_pyautogui = enable_pyautogui
        self.enable_playwright = enable_playwright
        self.enable_bs4 = enable_bs4
        self.response_mode = response_mode
        self.max_content_length = max_content_length
        self.min_content_length = max(1, int(min_content_length))

        self.pyautogui_pause = pyautogui_pause
        self.pyautogui_fail_safe = pyautogui_fail_safe
        self.pyautogui_new_tab = pyautogui_new_tab
        self.pyautogui_wait_seconds = pyautogui_wait_seconds
        self.pyautogui_dry_run = pyautogui_dry_run
        self.pyautogui_type_interval = pyautogui_type_interval
        self.pyautogui_open_mode = pyautogui_open_mode
        self.pyautogui_activate_browser = pyautogui_activate_browser
        self.pyautogui_browser_app = pyautogui_browser_app
        self.pyautogui_activate_wait_seconds = pyautogui_activate_wait_seconds
        self.pyautogui_read_wait_seconds = pyautogui_read_wait_seconds

        self.playwright_headless = playwright_headless
        self.playwright_timeout = playwright_timeout
        self.playwright_user_agent = playwright_user_agent
        self.playwright_include_links = playwright_include_links
        self.playwright_max_links = playwright_max_links
        self.playwright_screenshot_path = playwright_screenshot_path
        self.use_browser_environment = use_browser_environment
        self.browser_environment_config = dict(browser_environment_config or {})

    def register_actions(
        self,
        action,
        *,
        tags: str | list[str] | None = None,
        action_prefix: str = "",
        expose_to_model: bool = True,
        default_policy: dict[str, Any] | None = None,
        use_browser_environment: bool | None = None,
        browser_environment_config: dict[str, Any] | None = None,
    ) -> list[str]:
        prefix = action_prefix.strip()
        action_id = f"{ prefix }browse" if prefix else "browse"
        managed_browser = self.use_browser_environment if use_browser_environment is None else use_browser_environment
        environment_config = dict(self.browser_environment_config)
        if browser_environment_config:
            environment_config.update(browser_environment_config)
        execution_environments = []
        if managed_browser:
            execution_environments.append(
                {
                    "kind": "browser",
                    "scope": "action_call",
                    "resource_key": action_id,
                    "config": {
                        "headless": self.playwright_headless,
                        "timeout": self.playwright_timeout,
                        "proxy": self.proxy,
                        "user_agent": self.playwright_user_agent,
                        **environment_config,
                    },
                }
            )
        action.register_action(
            action_id=action_id,
            desc="Browse an accessible web page and return its main readable content.",
            kwargs={"url": (str, "Accessible URL.")},
            executor=action.create_action_executor("BrowseActionExecutor", browse=self),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="read",
            expose_to_model=expose_to_model,
            execution_environments=execution_environments,
            meta={
                "component": "builtins.actions.Browse",
                "legacy_tool_facade": "agently.builtins.tools.Browse",
                "fallback_order": list(self.fallback_order),
                "use_browser_environment": managed_browser,
            },
        )
        return [action_id]

    def _normalize_url(self, url: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(url or "")).strip()
        normalized = normalized.translate(_URL_PUNCT_TRANSLATION)
        normalized = normalized.replace("\u3000", " ")
        normalized = re.sub(r"[\r\n\t]+", "", normalized)
        normalized = normalized.strip(' "\'`')
        normalized = re.sub(r"[,;:!?]+$", "", normalized)
        return normalized

    def _normalize_content(self, content: str) -> str:
        normalized = str(content or "").replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"[ \t]+\n", "\n", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        if self.max_content_length > 0 and len(normalized) > self.max_content_length:
            return f"{normalized[:self.max_content_length]}..."
        return normalized

    @staticmethod
    def _build_header_line(level: str, text: str):
        if not level.startswith("h") or len(level) != 2 or not level[1].isdigit():
            return text
        return "#" * int(level[1]) + " " + text

    @classmethod
    def _is_noise_node(cls, node) -> bool:
        class_values = node.get("class", []) if hasattr(node, "get") else []
        if isinstance(class_values, str):
            class_text = class_values.lower()
        elif isinstance(class_values, (list, tuple)):
            class_text = " ".join([str(item).lower() for item in class_values])
        else:
            class_text = ""

        node_id = str(node.get("id", "")).lower() if hasattr(node, "get") else ""
        merged = f"{class_text} {node_id}".strip()
        return any(keyword in merged for keyword in cls.NOISE_KEYWORDS)

    @classmethod
    def _pick_main_root(cls, soup):
        from bs4 import Tag

        best_node = None
        best_length = 0
        for selector in cls.PRIMARY_CONTENT_SELECTORS:
            for node in soup.select(selector):
                if not isinstance(node, Tag):
                    continue
                text_length = len(node.get_text(" ", strip=True))
                if text_length > best_length:
                    best_node = node
                    best_length = text_length
        return best_node

    @classmethod
    def _pick_body_root(cls, soup):
        from bs4 import Tag

        if isinstance(soup.body, Tag):
            return soup.body
        html_node = soup.find("html")
        if isinstance(html_node, Tag):
            return html_node
        return soup

    @staticmethod
    def _content_is_enough(content: str, min_length: int) -> bool:
        return len(re.sub(r"\s+", "", str(content or ""))) >= max(1, int(min_length))

    @classmethod
    def _collect_text(cls, root, *, remove_tags: tuple[str, ...], filter_noise: bool):
        for removable in root.find_all(remove_tags):
            removable.decompose()

        if filter_noise:
            for node in root.find_all(True):
                if cls._is_noise_node(node):
                    node.decompose()

        content_lines = []
        for chunk in root.find_all(cls.CONTENT_TAGS):
            text = chunk.get_text(" ", strip=True)
            if text == "":
                continue
            if filter_noise and cls._is_noise_node(chunk):
                continue
            if chunk.name and chunk.name.startswith("h"):
                content_lines.append(cls._build_header_line(chunk.name, text))
            else:
                content_lines.append(text)

        normalized_lines: list[str] = []
        prev_line = ""
        for line in content_lines:
            line = re.sub(r"\s+", " ", line).strip()
            if line == "" or line == prev_line:
                continue
            normalized_lines.append(line)
            prev_line = line

        return "\n".join(normalized_lines).strip()

    @classmethod
    def _extract_text_from_soup(cls, soup, min_length: int | None = None) -> str:
        threshold = cls.BS4_STRATEGY_MIN_LENGTH if min_length is None else max(1, int(min_length))

        # Strategy 1 (whitelist): prefer known primary-content containers from docs/news/GitHub pages.
        root = cls._pick_main_root(soup)
        strict = ""
        if root is not None:
            strict = cls._collect_text(root, remove_tags=cls.REMOVE_TAGS_STRICT, filter_noise=True)
        if cls._content_is_enough(strict, threshold):
            return strict

        # Strategy 2 (blacklist): parse the whole body and remove common noise blocks.
        relaxed_root = cls._pick_body_root(soup)
        relaxed = cls._collect_text(relaxed_root, remove_tags=cls.REMOVE_TAGS_STRICT, filter_noise=True)
        if cls._content_is_enough(relaxed, threshold):
            return relaxed

        # Strategy 3 (body fallback): return raw body html when structured extraction is still too thin.
        body = cls._pick_body_root(soup)
        raw_body = str(body)
        if raw_body:
            return raw_body
        return ""

    def _extract_content_from_result(self, result: Any) -> str:
        if isinstance(result, str):
            text = result.strip()
            if text.startswith("Can not "):
                return ""
            return self._normalize_content(text)

        if isinstance(result, dict):
            content = self._normalize_content(str(result.get("content", "") or ""))
            if content:
                return content
            if isinstance(result.get("html_body"), str):
                return self._normalize_content(result["html_body"])
        return ""

    def _resolve_browser_app(self, os_name: str) -> str:
        if self.pyautogui_browser_app and self.pyautogui_browser_app.strip():
            return self.pyautogui_browser_app.strip()
        if os_name == "Darwin":
            return "Google Chrome"
        return ""

    def _activate_browser(self, os_name: str) -> str | None:
        if not self.pyautogui_activate_browser:
            return None
        app = (self.pyautogui_browser_app or "").strip()
        try:
            if os_name == "Darwin":
                target = app or "Google Chrome"
                subprocess.run(
                    ["osascript", "-e", f'tell application "{target}" to activate'],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if self.pyautogui_activate_wait_seconds > 0:
                    time.sleep(self.pyautogui_activate_wait_seconds)
                return target
            if os_name == "Windows" and app:
                subprocess.run(
                    f'start "" "{app}"',
                    shell=True,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if self.pyautogui_activate_wait_seconds > 0:
                    time.sleep(self.pyautogui_activate_wait_seconds)
                return app
            if os_name == "Linux" and app:
                subprocess.Popen(
                    [app],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if self.pyautogui_activate_wait_seconds > 0:
                    time.sleep(self.pyautogui_activate_wait_seconds)
                return app
        except Exception:
            return None
        return None

    def _build_read_javascript(self) -> str:
        if self.response_mode == "text":
            return (
                "JSON.stringify({"
                "url: location.href || '',"
                "title: document.title || '',"
                "content: ((document.body && document.body.innerText) || '')"
                "})"
            )
        return (
            "(() => {"
            "const root = document.body ? document.body.cloneNode(true) : null;"
            "if (root) {"
            "root.querySelectorAll('script,style,noscript,svg').forEach((el) => el.remove());"
            "root.querySelectorAll('a[href]').forEach((a) => {"
            "const href = a.href || '';"
            "const text = (a.textContent || '').trim().replace(/\\s+/g, ' ');"
            "const markdownLink = text ? '[' + text + '](' + href + ')' : href;"
            "a.replaceWith(document.createTextNode(markdownLink));"
            "});"
            "}"
            "const content = root ? (root.innerText || '') : '';"
            "return JSON.stringify({"
            "url: location.href || '',"
            "title: document.title || '',"
            "content: content"
            "});"
            "})()"
        )

    def _build_darwin_read_script(self, browser_app: str) -> list[str]:
        javascript = self._build_read_javascript()
        javascript_escaped = javascript.replace("\\", "\\\\").replace('"', '\\"')
        return [
            f'tell application "{browser_app}"',
            '    if (count of windows) is 0 then return ""',
            "    set _tab to active tab of front window",
            f'    set _json to execute _tab javascript "{javascript_escaped}"',
            "    return _json",
            "end tell",
        ]

    def _run_osascript(self, script_lines: list[str]) -> tuple[int, str, str]:
        cmd = ["osascript"]
        for line in script_lines:
            cmd.extend(["-e", line])
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    async def _pyautogui_open_url(self, url: str) -> dict[str, Any]:
        requested_url = str(url or "")
        url = self._normalize_url(requested_url)
        os_name = platform.system()
        actions = []
        if self.pyautogui_activate_browser:
            actions.append(f"activate:{self.pyautogui_browser_app or 'default-browser'}")
        if self.pyautogui_open_mode == "system":
            actions.append(f"system_open:{url}")
        else:
            modifier = "command" if os_name == "Darwin" else "ctrl"
            if self.pyautogui_new_tab:
                actions.append(f"{modifier}+t")
            actions.append(f"{modifier}+l")
            actions.extend([f"type:{url}", "press:enter"])

        if self.pyautogui_open_mode not in ("hotkey", "system"):
            return {
                "ok": False,
                "requested_url": requested_url,
                "url": url,
                "error": "Unsupported pyautogui_open_mode. Use 'hotkey' or 'system'.",
            }

        if self.pyautogui_dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "platform": os_name,
                "open_mode": self.pyautogui_open_mode,
                "requested_url": requested_url,
                "url": url,
                "actions": actions,
            }

        if self.pyautogui_open_mode == "hotkey":
            LazyImport.import_package("pyautogui", auto_install=False)

        if self.pyautogui_open_mode == "hotkey" and os_name == "Linux" and not os.environ.get("DISPLAY"):
            return {
                "ok": False,
                "requested_url": requested_url,
                "url": url,
                "error": "DISPLAY is not set. GUI session is required for PyAutoGUI.",
            }

        activated = self._activate_browser(os_name)
        try:
            if self.pyautogui_open_mode == "system":
                opened = bool(webbrowser.open_new_tab(url))
                if self.pyautogui_wait_seconds > 0:
                    time.sleep(self.pyautogui_wait_seconds)
                return {
                    "ok": opened,
                    "platform": os_name,
                    "open_mode": self.pyautogui_open_mode,
                    "activated_browser": activated,
                    "requested_url": requested_url,
                    "url": url,
                }

            import pyautogui

            modifier = "command" if os_name == "Darwin" else "ctrl"
            pyautogui.PAUSE = self.pyautogui_pause
            pyautogui.FAILSAFE = self.pyautogui_fail_safe
            if self.pyautogui_new_tab:
                pyautogui.hotkey(modifier, "t")
            pyautogui.hotkey(modifier, "l")
            pyautogui.typewrite(url, interval=self.pyautogui_type_interval)
            pyautogui.press("enter")
            if self.pyautogui_wait_seconds > 0:
                time.sleep(self.pyautogui_wait_seconds)
            return {
                "ok": True,
                "platform": os_name,
                "open_mode": self.pyautogui_open_mode,
                "activated_browser": activated,
                "requested_url": requested_url,
                "url": url,
            }
        except Exception as e:
            return {
                "ok": False,
                "platform": os_name,
                "open_mode": self.pyautogui_open_mode,
                "activated_browser": activated,
                "requested_url": requested_url,
                "url": url,
                "error": str(e),
            }

    async def _pyautogui_read_active_tab(self) -> dict[str, Any]:
        os_name = platform.system()
        browser_app = self._resolve_browser_app(os_name)

        if self.pyautogui_dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "platform": os_name,
                "browser_app": browser_app,
                "action": "read_active_tab",
            }

        if self.pyautogui_read_wait_seconds > 0:
            time.sleep(self.pyautogui_read_wait_seconds)

        if os_name != "Darwin":
            return {
                "ok": False,
                "platform": os_name,
                "browser_app": browser_app,
                "error": "read_active_tab currently supports macOS (Darwin) only.",
            }

        try:
            returncode, stdout, stderr = self._run_osascript(self._build_darwin_read_script(browser_app))
            if returncode != 0:
                return {
                    "ok": False,
                    "platform": os_name,
                    "browser_app": browser_app,
                    "error": (stderr or stdout or "AppleScript read failed").strip(),
                }
            if not stdout:
                return {
                    "ok": False,
                    "platform": os_name,
                    "browser_app": browser_app,
                    "error": "No active browser tab content returned.",
                }

            payload = json.loads(stdout)
            tab_url = self._normalize_url(payload.get("url", ""))
            title = str(payload.get("title", "") or "").strip()
            content = self._normalize_content(str(payload.get("content", "") or ""))
            return {
                "ok": True,
                "platform": os_name,
                "browser_app": browser_app,
                "content_format": self.response_mode,
                "url": tab_url,
                "title": title,
                "content": content,
                "status": None,
            }
        except Exception as e:
            return {
                "ok": False,
                "platform": os_name,
                "browser_app": browser_app,
                "error": str(e),
            }

    async def _pyautogui_open_and_read_url(self, url: str) -> dict[str, Any]:
        open_result = await self._pyautogui_open_url(url=url)
        if not isinstance(open_result, dict) or not open_result.get("ok"):
            return {
                "ok": False,
                "step": "pyautogui_open_url",
                "open_result": open_result,
            }
        read_result = await self._pyautogui_read_active_tab()
        if not isinstance(read_result, dict):
            return {
                "ok": False,
                "step": "pyautogui_read_active_tab",
                "open_result": open_result,
                "read_result": read_result,
            }
        read_result["requested_url"] = str(url or "")
        read_result["normalized_requested_url"] = self._normalize_url(str(url or ""))
        read_result["open_result"] = open_result
        return read_result

    async def _playwright_open(self, url: str) -> dict[str, Any]:
        LazyImport.import_package("playwright", auto_install=False)
        from playwright.async_api import async_playwright

        requested_url = str(url or "")
        url = self._normalize_url(requested_url)
        page_timeout = self.playwright_timeout
        screenshot_output = None

        try:
            async with async_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {
                    "headless": self.playwright_headless,
                }
                if self.proxy:
                    launch_kwargs["proxy"] = {"server": self.proxy}
                browser = await playwright.chromium.launch(**launch_kwargs)
                try:
                    context_kwargs = {}
                    if self.playwright_user_agent:
                        context_kwargs["user_agent"] = self.playwright_user_agent
                    context = await browser.new_context(**context_kwargs)
                    page = await context.new_page()
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
                    title = await page.title()

                    if self.response_mode == "text":
                        content = await page.locator("body").inner_text(timeout=page_timeout)
                        content = " ".join(content.split())
                    else:
                        content = await page.evaluate(
                            """
                            () => {
                                const root = document.body.cloneNode(true);
                                root.querySelectorAll("script,style,noscript,svg").forEach((el) => el.remove());
                                root.querySelectorAll("a[href]").forEach((a) => {
                                    const href = a.href || "";
                                    const text = (a.textContent || "").trim().replace(/\\s+/g, " ");
                                    const markdownLink = text ? `[${text}](${href})` : href;
                                    a.replaceWith(document.createTextNode(markdownLink));
                                });
                                return (root.innerText || "")
                                    .replace(/\\u00a0/g, " ")
                                    .replace(/[ \\t]+\\n/g, "\\n")
                                    .replace(/\\n{3,}/g, "\\n\\n")
                                    .trim();
                            }
                            """
                        )
                        content = " ".join(str(content).split())
                    content = self._normalize_content(content)

                    links = None
                    if self.playwright_include_links:
                        raw_links = await page.eval_on_selector_all(
                            "a[href]",
                            """
                            (elements) => elements.map((item) => ({
                                href: item.getAttribute("href") || "",
                                text: (item.textContent || "").trim(),
                            }))
                            """,
                        )
                        links = []
                        seen_links: set[str] = set()
                        for item in raw_links:
                            if not isinstance(item, dict):
                                continue
                            href = str(item.get("href", "")).strip()
                            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                                continue
                            absolute_url = urljoin(page.url, href)
                            parsed = urlparse(absolute_url)
                            if parsed.scheme not in ("http", "https"):
                                continue
                            if absolute_url in seen_links:
                                continue
                            seen_links.add(absolute_url)
                            link_text = " ".join(str(item.get("text", "")).split())
                            if self.playwright_max_links <= 0 or len(links) < self.playwright_max_links:
                                links.append({"url": absolute_url, "text": link_text})

                    if self.playwright_screenshot_path:
                        screenshot_output = Path(self.playwright_screenshot_path).expanduser().resolve()
                        screenshot_output.parent.mkdir(parents=True, exist_ok=True)
                        await page.screenshot(path=str(screenshot_output), full_page=True)

                    result = {
                        "ok": True,
                        "requested_url": requested_url,
                        "normalized_url": url,
                        "url": page.url,
                        "status": response.status if response else None,
                        "title": title,
                        "content_format": self.response_mode,
                        "content": content,
                        "screenshot_path": str(screenshot_output) if screenshot_output else None,
                    }
                    if links is not None:
                        result["links"] = links
                    return result
                finally:
                    await browser.close()
        except Exception as e:
            return {
                "ok": False,
                "requested_url": requested_url,
                "normalized_url": url,
                "error": str(e),
            }

    async def _bs4_browse(self, url: str) -> str:
        LazyImport.import_package("httpx")
        LazyImport.import_package("bs4", install_name="beautifulsoup4")

        from bs4 import BeautifulSoup
        from httpx import AsyncClient

        target_url = self._normalize_url(url)
        try:
            async with AsyncClient(
                proxy=self.proxy,
                timeout=self.timeout,
            ) as client:
                page = await client.get(target_url, headers=self.headers)
                if page.status_code == 301 and target_url.startswith("http:"):
                    target_url = target_url.replace("http:", "https:")
                    page = await client.get(target_url, headers=self.headers)
                soup = BeautifulSoup(page.content, "html.parser")
                content = self._extract_text_from_soup(soup, min_length=self.min_content_length)
                content = self._normalize_content(content)
                if content:
                    return content
                return f"Can not fetch any content from {target_url}!"
        except Exception as e:
            return f"Can not browse '{target_url}'.\tError: {str(e)}"

    async def _browse_with_trace(self, url: str) -> dict[str, Any]:
        requested_url = str(url or "")
        normalized_url = self._normalize_url(requested_url)
        attempts: list[dict[str, Any]] = []

        for backend in self.fallback_order:
            if backend == "pyautogui" and not self.enable_pyautogui:
                continue
            if backend == "playwright" and not self.enable_playwright:
                continue
            if backend == "bs4" and not self.enable_bs4:
                continue

            try:
                if backend == "pyautogui":
                    result = await self._pyautogui_open_and_read_url(normalized_url)
                elif backend == "playwright":
                    result = await self._playwright_open(normalized_url)
                elif backend == "bs4":
                    result = await self._bs4_browse(normalized_url)
                else:
                    attempts.append({"backend": backend, "ok": False, "reason": "Unknown backend"})
                    continue

                content = self._extract_content_from_result(result)
                ok = bool(content) and len(content) >= self.min_content_length

                if not ok:
                    reason = "content_empty_or_too_short"
                    if isinstance(result, dict):
                        error = str(result.get("error", "") or "").strip()
                        if error:
                            reason = error
                    elif isinstance(result, str) and result.startswith("Can not "):
                        reason = result
                    attempts.append({"backend": backend, "ok": False, "reason": reason})
                    continue

                trace = {
                    "ok": True,
                    "backend": backend,
                    "requested_url": requested_url,
                    "normalized_url": normalized_url,
                    "content_format": self.response_mode,
                    "content": content,
                    "attempts": attempts,
                }
                if isinstance(result, dict):
                    trace.update(
                        {
                            "url": result.get("url") or normalized_url,
                            "title": result.get("title", ""),
                            "status": result.get("status"),
                            "raw_result": result,
                        }
                    )
                else:
                    trace.update({"url": normalized_url, "title": "", "status": None, "raw_result": result})
                return trace
            except ImportError as e:
                attempts.append({"backend": backend, "ok": False, "reason": f"ImportError: {str(e)}"})
            except Exception as e:
                attempts.append({"backend": backend, "ok": False, "reason": str(e)})

        return {
            "ok": False,
            "requested_url": requested_url,
            "normalized_url": normalized_url,
            "content": "",
            "attempts": attempts,
            "error": "All browse backends failed.",
        }

    async def browse(self, url: str):
        trace = await self._browse_with_trace(url)
        if trace.get("ok"):
            return trace.get("content", "")

        reasons = []
        for item in trace.get("attempts", []):
            if not isinstance(item, dict):
                continue
            backend = str(item.get("backend", "unknown"))
            reason = str(item.get("reason", "failed"))
            reasons.append(f"{backend}: {reason}")
        reason_text = " | ".join(reasons) if reasons else "unknown error"
        return f"Can not browse '{self._normalize_url(url)}'.\tFallback failed: {reason_text}"
