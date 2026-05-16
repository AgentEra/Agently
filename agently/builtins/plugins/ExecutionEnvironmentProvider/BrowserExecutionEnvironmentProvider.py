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

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from agently.utils import LazyImport

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionEnvironmentHandle,
        ExecutionEnvironmentPolicy,
        ExecutionEnvironmentRequirement,
        ExecutionEnvironmentStatus,
    )


class BrowserExecutionResource:
    def __init__(
        self,
        *,
        page: Any = None,
        context: Any = None,
        browser: Any = None,
        session: Any = None,
        headless: bool = True,
        timeout: int = 30000,
        proxy: str | None = None,
        user_agent: str | None = None,
    ):
        self.page = page
        self.context = context
        self.browser = browser
        self.session = session
        self.headless = headless
        self.timeout = timeout
        self.proxy = proxy
        self.user_agent = user_agent
        self._playwright = None
        self._owns_playwright = False
        self._owns_browser = False
        self._owns_context = False
        self._owns_page = False

    async def _ensure_page(self):
        if self.page is not None:
            return self.page
        if self.context is not None:
            self.page = await self.context.new_page()
            self._owns_page = True
            return self.page
        if self.browser is not None:
            context_kwargs = {}
            if self.user_agent:
                context_kwargs["user_agent"] = self.user_agent
            self.context = await self.browser.new_context(**context_kwargs)
            self._owns_context = True
            self.page = await self.context.new_page()
            self._owns_page = True
            return self.page

        LazyImport.import_package("playwright", auto_install=False)
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._owns_playwright = True
        launch_kwargs: dict[str, Any] = {"headless": self.headless}
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        self.browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._owns_browser = True
        context_kwargs = {}
        if self.user_agent:
            context_kwargs["user_agent"] = self.user_agent
        self.context = await self.browser.new_context(**context_kwargs)
        self._owns_context = True
        self.page = await self.context.new_page()
        self._owns_page = True
        return self.page

    async def browse(self, *, browse_tool, url: str):
        page = await self._ensure_page()
        requested_url = str(url or "")
        normalized_url = browse_tool._normalize_url(requested_url)
        response = await page.goto(normalized_url, wait_until="domcontentloaded", timeout=self.timeout)
        title = await page.title()

        if browse_tool.response_mode == "text":
            content = await page.locator("body").inner_text(timeout=self.timeout)
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
        content = browse_tool._normalize_content(content)

        links = None
        if browse_tool.playwright_include_links:
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
                if browse_tool.playwright_max_links <= 0 or len(links) < browse_tool.playwright_max_links:
                    links.append({"url": absolute_url, "text": link_text})

        screenshot_output = None
        if browse_tool.playwright_screenshot_path:
            screenshot_output = Path(browse_tool.playwright_screenshot_path).expanduser().resolve()
            screenshot_output.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot_output), full_page=True)

        if len(content) < browse_tool.min_content_length:
            return f"Can not browse '{ normalized_url }'.\tManaged browser content was empty or too short."

        result = {
            "ok": True,
            "requested_url": requested_url,
            "normalized_url": normalized_url,
            "url": page.url,
            "status": response.status if response else None,
            "title": title,
            "content_format": browse_tool.response_mode,
            "content": content,
            "screenshot_path": str(screenshot_output) if screenshot_output else None,
        }
        if links is not None:
            result["links"] = links
        return result["content"]

    def is_ready(self):
        return self.page is not None or self.context is not None or self.browser is not None or self.session is not None

    async def close(self):
        if self._owns_page and self.page is not None:
            await self.page.close()
        if self._owns_context and self.context is not None:
            await self.context.close()
        if self._owns_browser and self.browser is not None:
            await self.browser.close()
        if self._owns_playwright and self._playwright is not None:
            await self._playwright.stop()


class BrowserExecutionEnvironmentProvider:
    name = "BrowserExecutionEnvironmentProvider"
    DEFAULT_SETTINGS = {}
    kind = "browser"

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionEnvironmentRequirement",
        policy: "ExecutionEnvironmentPolicy",
        existing_handle: "ExecutionEnvironmentHandle | None" = None,
    ) -> "ExecutionEnvironmentHandle":
        _ = (policy, existing_handle)
        config = requirement.get("config", {})
        resource = BrowserExecutionResource(
            page=config.get("page"),
            context=config.get("context"),
            browser=config.get("browser"),
            session=config.get("session"),
            headless=bool(config.get("headless", True)),
            timeout=int(config.get("timeout", 30000)),
            proxy=config.get("proxy"),
            user_agent=config.get("user_agent"),
        )
        return {
            "handle_id": f"browser:{ uuid.uuid4().hex }",
            "resource": resource,
            "status": "ready",
            "meta": {"provider": self.name, "managed": True},
        }

    async def async_health_check(self, handle: "ExecutionEnvironmentHandle") -> "ExecutionEnvironmentStatus":
        resource = handle.get("resource")
        return "ready" if resource is not None and hasattr(resource, "browse") else "unhealthy"

    async def async_release(self, handle: "ExecutionEnvironmentHandle") -> None:
        resource = handle.get("resource")
        if resource is not None and hasattr(resource, "close"):
            await resource.close()
        return None
