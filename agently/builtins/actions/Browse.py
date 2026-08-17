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

from agently_stage import default_stage_call_bridge

from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal, cast
import asyncio
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import time
import unicodedata
import webbrowser
from urllib.parse import quote, urljoin, urlparse

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
    REMOTE_FILE_EXTENSIONS = {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
    }
    REMOTE_FILE_MEDIA_TYPES = {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }

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
    JINA_READER_BACKEND_ALIASES = {"jina", "jina-reader", "jina_reader", "reader"}

    BLOCKED_PAGE_MARKERS = (
        "web application firewall",
        "website is temporarily inaccessible",
        "protocol and port for the website are not added",
        "yundun.console.aliyun.com",
        "errorcodetitle",
        "errorcodeinfo",
        'id="waf"',
        "access denied",
        "request blocked",
        "target url returned error",
        "blocked by network security",
        "log in to your reddit account",
        "developer token",
        "login required",
        "sina visitor system",
        "passport.weibo.com/visitor",
        "captcha",
        "errorcode:",
    )

    def __init__(
        self,
        proxy: str | None = None,
        timeout: int | None = None,
        headers: dict[str, str] | None = None,
        *,
        fallback_order: tuple[str, ...] = ("jina_reader", "playwright", "bs4", "curl"),
        enable_pyautogui: bool = False,
        enable_playwright: bool = True,
        enable_curl: bool = True,
        enable_jina_reader: bool = True,
        enable_bs4: bool = True,
        response_mode: Literal["markdown", "text"] = "markdown",
        max_content_length: int = 12000,
        min_content_length: int = 40,
        jina_reader_endpoint: str = "https://r.jina.ai/",
        jina_reader_fallback_endpoints: tuple[str, ...] | list[str] | str | None = ("https://r.jinaai.cn/",),
        jina_reader_headers: dict[str, str] | None = None,
        jina_reader_engine: Literal["auto", "browser", "curl"] | None = "auto",
        jina_reader_timeout: int | None = 10,
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
        playwright_include_links: bool = True,
        playwright_max_links: int = 120,
        playwright_screenshot_path: str | None = None,
        use_browser_environment: bool = False,
        browser_environment_config: dict[str, Any] | None = None,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 0.25,
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
        self.enable_curl = enable_curl
        self.enable_jina_reader = enable_jina_reader
        self.enable_bs4 = enable_bs4
        self.response_mode = response_mode
        self.max_content_length = max_content_length
        self.min_content_length = max(1, int(min_content_length))
        self.jina_reader_endpoint = jina_reader_endpoint
        self.jina_reader_fallback_endpoints = self._normalize_endpoint_list(jina_reader_fallback_endpoints)
        self.jina_reader_headers = dict(jina_reader_headers or {})
        self.jina_reader_engine = jina_reader_engine
        self.jina_reader_timeout = jina_reader_timeout

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
        self.max_attempts = max(1, int(max_attempts)) if isinstance(max_attempts, int) else 2
        self.retry_backoff_seconds = (
            max(0.0, float(retry_backoff_seconds)) if isinstance(retry_backoff_seconds, (int, float)) else 0.25
        )

    def apply_language_policy(self, policy: Mapping[str, Any]) -> None:
        accept_language = policy.get("accept_language") if isinstance(policy, Mapping) else None
        if accept_language is not None and str(accept_language).strip() and "Accept-Language" not in self.headers:
            self.headers["Accept-Language"] = str(accept_language).strip()

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
        execution_resources = []
        if managed_browser:
            execution_resources.append(
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
        browse_desc = (
            "Browse an accessible web page and return its main readable content. "
            "The default strategy tries Jina Reader for LLM-friendly markdown first, "
            "then falls back to Playwright, BS4, and restricted curl when Reader transport "
            "fails or returns an obvious block/error shell. "
            "Protocol guidance: when an http URL, bare domain, root path, or guessed path returns "
            "a blocked/WAF/error page, short content, status such as 410, or an empty shell, try the "
            "same host through https and canonical entry pages before concluding the whole domain is "
            "unreachable. When the returned content includes same-site navigation links, prefer "
            "following those links over inventing path names."
        )
        action.register_action(
            action_id=action_id,
            desc=browse_desc,
            kwargs={"url": (str, "Accessible URL.")},
            executor=action.create_action_executor("BrowseActionExecutor", browse=self),
            tags=tags,
            default_policy=default_policy,
            side_effect_level="read",
            expose_to_model=expose_to_model,
            execution_resources=execution_resources,
            meta={
                "component": "builtins.actions.Browse",
                "legacy_tool_facade": "agently.builtins.tools.Browse",
                "fallback_order": list(self.fallback_order),
                "enable_jina_reader": self.enable_jina_reader,
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

    def _candidate_urls(self, url: str) -> list[dict[str, Any]]:
        normalized = self._normalize_url(url)
        if not normalized:
            return []
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            normalized = f"https://{normalized.lstrip('/')}"
            parsed = urlparse(normalized)
        candidates: list[dict[str, Any]] = []

        def add(candidate: str, *, reason: str, downgraded: bool = False):
            if not candidate:
                return
            if candidate not in [item["url"] for item in candidates]:
                candidates.append({"url": candidate, "reason": reason, "security_downgrade": downgraded})

        add(normalized, reason="requested")
        if parsed.scheme == "http":
            add(parsed._replace(scheme="https").geturl(), reason="same_host_https")
        elif parsed.scheme == "https":
            add(parsed._replace(scheme="http").geturl(), reason="same_host_http", downgraded=True)
        if not parsed.path or parsed.path == "/":
            for scheme in ("https", "http"):
                base = parsed._replace(scheme=scheme, path="/", params="", query="", fragment="").geturl()
                add(base, reason="canonical_root", downgraded=scheme == "http")
        return candidates

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

    @staticmethod
    def _safe_node_get(node: Any, key: str, default: Any = None) -> Any:
        try:
            if hasattr(node, "get"):
                return node.get(key, default)
        except Exception:
            return default
        return default

    @classmethod
    def _is_noise_node(cls, node) -> bool:
        class_values = cls._safe_node_get(node, "class", [])
        if isinstance(class_values, str):
            class_text = class_values.lower()
        elif isinstance(class_values, (list, tuple)):
            class_text = " ".join([str(item).lower() for item in class_values])
        else:
            class_text = ""

        node_id = str(cls._safe_node_get(node, "id", "") or "").lower()
        merged = f"{class_text} {node_id}".strip()
        return any(keyword in merged for keyword in cls.NOISE_KEYWORDS)

    @classmethod
    def _pick_main_root(cls, soup):
        Tag = LazyImport.from_import("bs4", "Tag", install_name="beautifulsoup4", auto_install=False)

        best_node = None
        best_length = 0
        for selector in cls.PRIMARY_CONTENT_SELECTORS:
            try:
                nodes = soup.select(selector)
            except Exception:
                continue
            for node in nodes:
                if not isinstance(node, Tag):
                    continue
                text_length = len(node.get_text(" ", strip=True))
                if text_length > best_length:
                    best_node = node
                    best_length = text_length
        return best_node

    @classmethod
    def _pick_body_root(cls, soup):
        Tag = LazyImport.from_import("bs4", "Tag", install_name="beautifulsoup4", auto_install=False)

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
    def _blocked_page_reason(cls, content: str) -> str:
        text = re.sub(r"\s+", " ", str(content or "")).strip().lower()
        if not text:
            return ""
        for marker in cls.BLOCKED_PAGE_MARKERS:
            if marker in text:
                return f"blocked_or_error_page: {marker}"
        return ""

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
    def _sanitize_raw_body_fallback(cls, root) -> str:
        for node in root.find_all(("svg", "canvas", "video", "audio", "iframe")):
            node.decompose()
        for node in root.find_all(True):
            node_attrs = getattr(node, "attrs", None) or {}
            for attr, value in list(node_attrs.items()):
                values = value if isinstance(value, list) else [value]
                text = " ".join(str(item) for item in values)
                if "data:" in text or len(text) > 512:
                    del node.attrs[attr]
        raw_body = str(root)
        raw_body = re.sub(r"data:[^\\s\"']{128,}", "data:[omitted]", raw_body)
        return raw_body

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
        raw_body = cls._sanitize_raw_body_fallback(body)
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

    @staticmethod
    def _media_type_from_content_type(content_type: str) -> str | None:
        media_type = str(content_type or "").split(";", 1)[0].strip().lower()
        return media_type or None

    @classmethod
    def _looks_like_remote_file(cls, *, url: str, media_type: str | None, content: bytes) -> bool:
        extension = Path(urlparse(str(url or "")).path).suffix.lower()
        if extension in cls.REMOTE_FILE_EXTENSIONS:
            return True
        if media_type in cls.REMOTE_FILE_MEDIA_TYPES:
            return True
        return content.startswith(b"%PDF-") or content.startswith(b"PK\x03\x04")

    @staticmethod
    def _filename_from_response(url: str, headers: Any) -> str:
        try:
            disposition = str(headers.get("content-disposition", "") or headers.get("Content-Disposition", ""))
        except Exception:
            disposition = ""
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, flags=re.I)
        if match:
            name = match.group(1).strip()
        else:
            name = Path(urlparse(url).path).name
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
        return name or "download.bin"

    @staticmethod
    def _download_task_workspace_path(url: str, content: bytes, headers: Any) -> str:
        filename = Browse._filename_from_response(url, headers)
        digest = hashlib.sha256(content).hexdigest()[:16]
        stem = Path(filename).stem or "download"
        suffix = Path(filename).suffix or ".bin"
        return f"downloads/{stem}-{digest}{suffix}"

    @staticmethod
    def _extract_links_from_soup(soup: Any, *, base_url: str, max_links: int) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"}:
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            text = " ".join(str(anchor.get_text(" ", strip=True) or "").split())
            links.append({"url": absolute, "text": text})
            if max_links > 0 and len(links) >= max_links:
                break
        return links

    @staticmethod
    def _extract_links_from_markdown(content: str, *, base_url: str, max_links: int) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for match in re.finditer(r"(?<!!)\[([^\]\n]{0,300})\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", str(content or "")):
            href = match.group(2).strip()
            if not href or href.startswith("#"):
                continue
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"}:
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            text = " ".join(match.group(1).split())
            links.append({"url": absolute, "text": text})
            if max_links > 0 and len(links) >= max_links:
                break
        return links

    def _jina_reader_url(self, url: str) -> str:
        endpoint = self._normalize_endpoint(str(self.jina_reader_endpoint or "").strip() or "https://r.jina.ai/")
        return self._jina_reader_url_for_endpoint(endpoint, url)

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        endpoint_text = str(endpoint or "").strip() or "https://r.jina.ai/"
        return endpoint_text.rstrip("/") + "/"

    @classmethod
    def _normalize_endpoint_list(cls, value: tuple[str, ...] | list[str] | str | None) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",")]
        else:
            items = [str(item).strip() for item in value]
        endpoints: list[str] = []
        for item in items:
            if not item:
                continue
            endpoint = cls._normalize_endpoint(item)
            if endpoint not in endpoints:
                endpoints.append(endpoint)
        return tuple(endpoints)

    def _jina_reader_endpoints(self) -> list[str]:
        endpoints: list[str] = []
        for endpoint in (
            self._normalize_endpoint(str(self.jina_reader_endpoint or "").strip() or "https://r.jina.ai/"),
            *self.jina_reader_fallback_endpoints,
        ):
            endpoint = self._normalize_endpoint(endpoint)
            if endpoint not in endpoints:
                endpoints.append(endpoint)
        return endpoints

    def _jina_reader_url_for_endpoint(self, endpoint: str, url: str) -> str:
        endpoint = self._normalize_endpoint(endpoint)
        safe_url_chars = ":/?#[]@!$&'()*+,;=%"
        return f"{endpoint.rstrip('/')}/{quote(self._normalize_url(url), safe=safe_url_chars)}"

    def _jina_reader_request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "text/markdown, text/plain;q=0.9, application/json;q=0.8, */*;q=0.5",
            "X-Preset": "agent",
            "X-Retain-Links": "all" if self.playwright_include_links else "text",
            "X-Retain-Images": "alt",
            "X-Retain-Media": "text",
        }
        if self.response_mode == "text":
            headers["X-Respond-With"] = "text"
        if self.jina_reader_engine:
            headers["X-Engine"] = str(self.jina_reader_engine)
        timeout = self.jina_reader_timeout if self.jina_reader_timeout is not None else self.timeout
        if isinstance(timeout, (int, float)) and timeout:
            headers["X-Timeout"] = str(min(180, max(1, int(timeout))))
        accept_language = self.headers.get("Accept-Language")
        if accept_language:
            headers["Accept-Language"] = accept_language
        headers.update(self.jina_reader_headers)
        return headers

    @staticmethod
    def _exception_detail(error: BaseException) -> str:
        def describe(item: BaseException) -> str:
            name = item.__class__.__name__
            message = str(item).strip()
            if message:
                return f"{name}: {message}"
            arg_text = ", ".join(
                repr(arg) for arg in getattr(item, "args", ()) if str(arg).strip() or repr(arg) not in {"''", '""'}
            )
            if arg_text:
                return f"{name}: {arg_text}"
            item_repr = repr(item)
            if item_repr and item_repr not in {f"{name}()", f"{name}('')", f'{name}("")'}:
                return f"{name}: {item_repr}"
            return name

        parts = [describe(error)]
        cause = error.__cause__ or error.__context__
        if cause is not None:
            parts.append(f"caused by {describe(cause)}")
        return ": ".join(parts)

    @staticmethod
    def _extract_canonical_links(soup: Any, *, base_url: str) -> list[str]:
        links: list[str] = []
        for selector in ('link[rel="canonical"]', 'meta[property="og:url"]'):
            try:
                nodes = soup.select(selector)
            except Exception:
                continue
            for node in nodes:
                value = str(Browse._safe_node_get(node, "href") or Browse._safe_node_get(node, "content") or "").strip()
                if not value:
                    continue
                absolute = urljoin(base_url, value)
                if absolute not in links:
                    links.append(absolute)
        return links

    @staticmethod
    def _attempt_diagnostics(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for item in attempts:
            if not isinstance(item, dict):
                continue
            backend = str(item.get("backend", "unknown"))
            reason = str(item.get("reason", "failed"))
            if item.get("ok") is True and item.get("security_downgrade") is True:
                diagnostics.append(
                    {
                        "code": "browse_security_downgrade",
                        "backend": backend,
                        "attempt_index": int(item.get("attempt_index", 0) or 0),
                        "retryable": False,
                        "message": reason,
                        "detail": {
                            key: item.get(key)
                            for key in ("url", "candidate_reason", "security_downgrade", "status")
                            if key in item
                        },
                    }
                )
                continue
            diagnostics.append(
                {
                    "code": "browse_backend_failed",
                    "backend": backend,
                    "attempt_index": int(item.get("attempt_index", 0) or 0),
                    "retryable": bool(item.get("retryable", False)),
                    "message": reason,
                    "detail": {
                        key: item.get(key)
                        for key in ("url", "candidate_reason", "security_downgrade", "status")
                        if key in item
                    },
                }
            )
        return diagnostics

    @staticmethod
    def _reason_text(attempts: list[dict[str, Any]]) -> str:
        reasons: list[str] = []
        for item in attempts:
            if not isinstance(item, dict):
                continue
            backend = str(item.get("backend", "unknown"))
            reason = str(item.get("reason", "failed"))
            reasons.append(f"{backend}: {reason}")
        return " | ".join(reasons) if reasons else "unknown error"

    @staticmethod
    def _is_transient_error(error: Exception | str) -> bool:
        message = str(error).lower()
        error_name = error.__class__.__name__.lower() if isinstance(error, Exception) else ""
        transient_markers = (
            "timeout",
            "timed out",
            "connection",
            "connect",
            "network",
            "reset",
            "disconnect",
            "incomplete chunked read",
            "chunked",
            "broken pipe",
            "temporarily unavailable",
            "temporary failure",
            "proxy",
            "ssl",
            "tls",
            "429",
            "rate limit",
            "too many requests",
            "502",
            "503",
            "504",
            "gateway",
            "service unavailable",
        )
        return any(marker in message or marker in error_name for marker in transient_markers)

    async def _materialize_remote_file_trace(
        self,
        trace: dict[str, Any],
        task_workspace: Any,
    ) -> dict[str, Any]:
        content = trace.get("content_bytes")
        content = content if isinstance(content, (bytes, bytearray)) else b""
        selected_url = str(trace.get("url") or trace.get("normalized_url") or "")
        media_type = str(trace.get("media_type") or "") or None
        if not content:
            return {
                "ok": False,
                "success": False,
                "status": "error",
                "data": None,
                "result": None,
                "error": "Remote file response did not contain bytes to materialize.",
                "diagnostics": [
                    {
                        "code": "browse.remote_file.empty",
                        "message": "Remote file response did not contain bytes to materialize.",
                    }
                ],
                "meta": {
                    "provider": "builtins.actions.Browse",
                    "method": "browse",
                    "selected_url": selected_url,
                    "content_kind": "remote_file",
                },
            }
        if task_workspace is None or not callable(getattr(task_workspace, "materialize_file", None)):
            return {
                "ok": False,
                "success": False,
                "status": "blocked",
                "data": None,
                "result": None,
                "error": "Remote file response requires a TaskWorkspace binding before it can be read.",
                "diagnostics": [
                    {
                        "code": "browse.remote_file.task_workspace_required",
                        "message": "Remote file response requires a TaskWorkspace binding before it can be materialized and read.",
                        "detail": {
                            "selected_url": selected_url,
                            "media_type": media_type,
                            "bytes": len(content),
                        },
                    }
                ],
                "meta": {
                    "provider": "builtins.actions.Browse",
                    "method": "browse",
                    "selected_url": selected_url,
                    "content_kind": "remote_file",
                },
            }
        headers = trace.get("headers") if isinstance(trace.get("headers"), dict) else {}
        path = self._download_task_workspace_path(selected_url, bytes(content), headers)
        materialized = await task_workspace.materialize_file(
            path,
            bytes(content),
            source={
                "kind": "remote_browse_download",
                "url": selected_url,
                "requested_url": trace.get("requested_url"),
                "backend": trace.get("backend"),
            },
            media_type=media_type,
            overwrite=False,
        )
        read_preview: dict[str, Any] = {}
        try:
            read_result = await task_workspace.read_file(materialized["path"], max_bytes=4000)
            read_preview = {
                "ok": read_result.get("ok"),
                "readable": read_result.get("readable"),
                "path": read_result.get("path"),
                "content": read_result.get("content", ""),
                "truncated": read_result.get("truncated"),
                "bytes": read_result.get("bytes"),
                "read_bytes": read_result.get("read_bytes"),
                "sha256": read_result.get("sha256"),
                "media_type": read_result.get("media_type"),
                "content_kind": read_result.get("content_kind"),
                "handler_id": read_result.get("handler_id"),
                "diagnostics": read_result.get("diagnostics", []),
            }
        except Exception as error:
            read_preview = {
                "ok": False,
                "readable": False,
                "path": materialized["path"],
                "content": "",
                "diagnostics": [
                    {
                        "code": "browse.remote_file.read_preview_failed",
                        "message": str(error),
                    }
                ],
            }
        file_refs = list(materialized.get("file_refs", []))
        data = {
            "kind": "remote_file",
            "source_url": selected_url,
            "selected_url": selected_url,
            "requested_url": trace.get("requested_url"),
            "media_type": materialized.get("media_type") or media_type,
            "bytes": materialized.get("bytes", len(content)),
            "sha256": materialized.get("sha256"),
            "path": materialized.get("path"),
            "file_refs": file_refs,
            "read_preview": read_preview,
        }
        return {
            "ok": True,
            "success": True,
            "status": "success" if read_preview.get("ok") else "partial_success",
            "data": data,
            "result": data,
            "file_refs": file_refs,
            "diagnostics": [
                *list(materialized.get("diagnostics", [])),
                *list(read_preview.get("diagnostics", [])),
            ],
            "meta": {
                "provider": "builtins.actions.Browse",
                "method": "browse",
                "selected_url": selected_url,
                "content_kind": "remote_file",
                "attempts": [
                    {
                        key: item.get(key)
                        for key in ("backend", "attempt_index", "url", "candidate_reason", "ok", "status")
                        if isinstance(item, dict) and key in item
                    }
                    for item in trace.get("attempts", [])
                    if isinstance(item, dict)
                ],
            },
        }

    async def _execute_action_method(self, method_name: str = "browse", **kwargs: Any) -> dict[str, Any]:
        task_workspace = kwargs.pop("task_workspace", None)
        custom_method = self.__dict__.get(method_name)
        if callable(custom_method):

            output = await default_stage_call_bridge.as_async(custom_method)(**kwargs)
            if isinstance(output, dict) and "status" in output:
                return output
            return {
                "ok": True,
                "success": True,
                "status": "success",
                "data": output,
                "result": output,
                "diagnostics": [],
                "meta": {
                    "provider": "custom",
                    "method": method_name,
                },
            }
        if method_name != "browse":
            method = getattr(self, method_name)
            output = await method(**kwargs)
            if isinstance(output, dict) and "status" in output:
                return output
            return {
                "ok": True,
                "success": True,
                "status": "success",
                "data": output,
                "result": output,
                "diagnostics": [],
                "meta": {
                    "provider": "builtins.actions.Browse",
                    "method": method_name,
                },
            }

        url = str(kwargs.get("url", ""))
        trace = await self._browse_with_trace(url)
        attempts = trace.get("attempts", [])
        attempts = attempts if isinstance(attempts, list) else []
        diagnostics = self._attempt_diagnostics(attempts)
        meta = {
            "provider": "builtins.actions.Browse",
            "method": "browse",
            "requested_url": trace.get("requested_url", url),
            "normalized_url": trace.get("normalized_url", self._normalize_url(url)),
            "backend": trace.get("backend"),
            "attempted_backends": list(self.fallback_order),
            "max_attempts": self.max_attempts,
            "failed_backends": [
                str(item.get("backend", "unknown"))
                for item in attempts
                if isinstance(item, dict) and item.get("ok") is not True
            ],
            "content_format": self.response_mode,
            "selected_url": trace.get("url", trace.get("normalized_url")),
            "retry_candidates": trace.get("retry_candidates", []),
            "canonical_links": trace.get("canonical_links", []),
            "links": trace.get("links", []),
        }
        if trace.get("content_kind") == "remote_file":
            return await self._materialize_remote_file_trace(trace, task_workspace)
        if trace.get("ok"):
            content = str(trace.get("content", "") or "")
            status = "partial_success" if diagnostics else "success"
            data: Any = content
            if trace.get("links") or trace.get("canonical_links"):
                data = {
                    "content": content,
                    "selected_url": trace.get("url", trace.get("normalized_url")),
                    "canonical_links": trace.get("canonical_links", []),
                    "links": trace.get("links", []),
                }
            return {
                "ok": True,
                "success": True,
                "status": status,
                "data": data,
                "result": data,
                "diagnostics": diagnostics,
                "meta": meta,
            }

        reason_text = self._reason_text(attempts)
        error = f"Can not browse '{self._normalize_url(url)}'. Fallback failed: {reason_text}"
        return {
            "ok": False,
            "success": False,
            "status": "error",
            "data": None,
            "result": None,
            "error": error,
            "diagnostics": diagnostics
            or [
                {
                    "code": "browse_backend_failed",
                    "backend": "all",
                    "message": reason_text,
                }
            ],
            "meta": meta,
        }

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

            pyautogui = cast(Any, LazyImport.import_package("pyautogui", auto_install=False))

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
        playwright_async = LazyImport.import_package(
            "playwright.async_api",
            install_name="playwright",
            auto_install=False,
        )
        async_playwright = getattr(playwright_async, "async_playwright")

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

    async def _bs4_browse(self, url: str) -> str | dict[str, Any]:
        httpx = LazyImport.import_package("httpx", auto_install=False)
        bs4 = LazyImport.import_package("bs4", install_name="beautifulsoup4", auto_install=False)
        AsyncClient = getattr(httpx, "AsyncClient")
        BeautifulSoup = getattr(bs4, "BeautifulSoup")

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
                final_url = str(page.url)
                media_type = self._media_type_from_content_type(str(page.headers.get("content-type", "")))
                if self._looks_like_remote_file(url=final_url, media_type=media_type, content=page.content):
                    return {
                        "ok": True,
                        "content_kind": "remote_file",
                        "requested_url": target_url,
                        "url": final_url,
                        "status": page.status_code,
                        "media_type": media_type,
                        "headers": dict(page.headers),
                        "content_bytes": bytes(page.content),
                    }
                soup = BeautifulSoup(page.content, "html.parser")
                content = self._extract_text_from_soup(soup, min_length=self.min_content_length)
                content = self._normalize_content(content)
                links = self._extract_links_from_soup(
                    soup,
                    base_url=final_url,
                    max_links=self.playwright_max_links,
                )
                canonical_links = self._extract_canonical_links(soup, base_url=final_url)
                if content:
                    return {
                        "ok": True,
                        "content_kind": "html",
                        "requested_url": target_url,
                        "url": final_url,
                        "status": page.status_code,
                        "media_type": media_type,
                        "content": content,
                        "links": links,
                        "canonical_links": canonical_links,
                    }
                return f"Can not fetch any content from {target_url}!"
        except Exception as e:
            return f"Can not browse '{target_url}'.\tError: {str(e)}"

    @staticmethod
    def _parse_curl_header_dump(header_bytes: bytes) -> dict[str, Any]:
        text = header_bytes.decode("iso-8859-1", errors="replace")
        blocks = [block.strip() for block in re.split(r"\r?\n\r?\n", text) if block.strip()]
        status = None
        headers: dict[str, str] = {}
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines or not lines[0].lower().startswith("http/"):
                continue
            status_match = re.search(r"\s(\d{3})(?:\s|$)", lines[0])
            if status_match:
                status = int(status_match.group(1))
            headers = {}
            for line in lines[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        return {"status": status, "headers": headers}

    async def _curl_browse(self, url: str) -> str | dict[str, Any]:
        target_url = self._normalize_url(url)
        timeout = int(self.timeout or 30)

        def run_curl() -> dict[str, Any] | str:
            with tempfile.NamedTemporaryFile() as header_file, tempfile.NamedTemporaryFile() as body_file:
                cmd = [
                    "curl",
                    "-L",
                    "--silent",
                    "--show-error",
                    "--compressed",
                    "--max-time",
                    str(max(1, timeout)),
                    "--dump-header",
                    header_file.name,
                    "--output",
                    body_file.name,
                    "--write-out",
                    "%{url_effective}",
                ]
                if self.proxy:
                    cmd.extend(["--proxy", self.proxy])
                for key, value in self.headers.items():
                    cmd.extend(["-H", f"{key}: {value}"])
                cmd.append(target_url)
                completed = subprocess.run(
                    cmd,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if completed.returncode != 0:
                    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
                    return f"Can not browse '{target_url}'.\tError: curl exited {completed.returncode}: {stderr}"
                header_file.seek(0)
                body_file.seek(0)
                header_info = self._parse_curl_header_dump(header_file.read())
                content = body_file.read()
                effective_url = completed.stdout.decode("utf-8", errors="replace").strip() or target_url
                headers = dict(header_info.get("headers", {}))
                media_type = self._media_type_from_content_type(str(headers.get("content-type", "")))
                if self._looks_like_remote_file(url=effective_url, media_type=media_type, content=content):
                    return {
                        "ok": True,
                        "content_kind": "remote_file",
                        "requested_url": target_url,
                        "url": effective_url,
                        "status": header_info.get("status"),
                        "media_type": media_type,
                        "headers": headers,
                        "content_bytes": bytes(content),
                    }
                return {
                    "ok": True,
                    "content_kind": "html",
                    "requested_url": target_url,
                    "url": effective_url,
                    "status": header_info.get("status"),
                    "media_type": media_type,
                    "headers": headers,
                    "content_bytes": bytes(content),
                }

        result = await asyncio.to_thread(run_curl)
        if isinstance(result, str):
            return result
        if result.get("content_kind") == "remote_file":
            return result

        bs4 = LazyImport.import_package("bs4", install_name="beautifulsoup4", auto_install=False)
        BeautifulSoup = getattr(bs4, "BeautifulSoup")

        content_bytes = result.pop("content_bytes", b"")
        soup = BeautifulSoup(content_bytes, "html.parser")
        content = self._extract_text_from_soup(soup, min_length=self.min_content_length)
        content = self._normalize_content(content)
        final_url = str(result.get("url") or target_url)
        links = self._extract_links_from_soup(soup, base_url=final_url, max_links=self.playwright_max_links)
        canonical_links = self._extract_canonical_links(soup, base_url=final_url)
        if not content:
            return f"Can not fetch any content from {target_url}!"
        return {
            **result,
            "content": content,
            "links": links,
            "canonical_links": canonical_links,
        }

    async def _jina_reader_browse(self, url: str) -> str | dict[str, Any]:
        httpx = LazyImport.import_package("httpx", auto_install=False)
        AsyncClient = getattr(httpx, "AsyncClient")

        target_url = self._normalize_url(url)
        parsed = urlparse(target_url)
        if parsed.scheme not in {"http", "https"}:
            return f"Can not browse '{target_url}'.\tError: Jina Reader requires an http or https URL."

        extension = Path(parsed.path).suffix.lower()
        if extension in self.REMOTE_FILE_EXTENSIONS:
            return (
                f"Can not browse '{target_url}'.\tError: Jina Reader backend skips remote-file URLs; "
                "use a local Browse backend with TaskWorkspace materialization."
            )

        timeout = self.jina_reader_timeout if self.jina_reader_timeout is not None else self.timeout
        request_timeout = max(1, int(timeout or 30))
        headers = self._jina_reader_request_headers()
        endpoint_errors: list[str] = []
        for endpoint in self._jina_reader_endpoints():
            reader_url = self._jina_reader_url_for_endpoint(endpoint, target_url)
            try:
                async with AsyncClient(proxy=self.proxy, timeout=request_timeout) as client:
                    if parsed.fragment:
                        response = await client.post(endpoint, headers=headers, data={"url": target_url})
                    else:
                        response = await client.get(reader_url, headers=headers)
            except Exception as error:
                error_text = self._exception_detail(error)
                endpoint_errors.append(f"{endpoint} request failed: {error_text}")
                continue

            media_type = self._media_type_from_content_type(str(response.headers.get("content-type", "")))
            if response.status_code >= 500 or response.status_code == 429:
                preview = " ".join(str(response.text or "").split())[:300]
                detail = f": {preview}" if preview else ""
                endpoint_errors.append(f"{endpoint} returned HTTP {response.status_code}{detail}")
                continue
            if response.status_code >= 400:
                preview = " ".join(str(response.text or "").split())[:300]
                detail = f": {preview}" if preview else ""
                return f"Can not browse '{target_url}'.\tError: Jina Reader returned HTTP {response.status_code}{detail}"

            title = ""
            final_url = target_url
            content = str(response.text or "")
            if media_type == "application/json":
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    content = str(payload.get("content") or payload.get("text") or "")
                    title = str(payload.get("title") or "")
                    final_url = self._normalize_url(str(payload.get("url") or payload.get("source_url") or target_url))

            if final_url == target_url:
                for line in content.splitlines()[:8]:
                    if line.lower().startswith("url source:"):
                        final_url = self._normalize_url(line.split(":", 1)[1])
                        break

            content = self._normalize_content(content)
            if not content:
                return f"Can not fetch any content from {target_url}!"

            links = self._extract_links_from_markdown(
                content,
                base_url=final_url or target_url,
                max_links=self.playwright_max_links,
            )
            return {
                "ok": True,
                "content_kind": "reader_text",
                "requested_url": target_url,
                "url": final_url or target_url,
                "status": response.status_code,
                "media_type": media_type or "text/markdown",
                "headers": {"content-type": str(response.headers.get("content-type", ""))},
                "title": title,
                "content": content,
                "links": links,
                "canonical_links": [final_url] if final_url and final_url != target_url else [],
                "reader_url": reader_url,
                "reader_endpoint": endpoint,
                "reader_endpoint_attempts": self._jina_reader_endpoints(),
            }

        error_text = " | ".join(endpoint_errors) if endpoint_errors else "no Jina Reader endpoint configured"
        return f"Can not browse '{target_url}'.\tError: Jina Reader request failed: {error_text}"

    async def _browse_with_trace(self, url: str) -> dict[str, Any]:
        requested_url = str(url or "")
        normalized_url = self._normalize_url(requested_url)
        candidates = self._candidate_urls(normalized_url)
        attempts: list[dict[str, Any]] = []

        for backend in self.fallback_order:
            if backend == "pyautogui" and not self.enable_pyautogui:
                continue
            if backend == "playwright" and not self.enable_playwright:
                continue
            if backend == "curl" and not self.enable_curl:
                continue
            if backend in self.JINA_READER_BACKEND_ALIASES and not self.enable_jina_reader:
                continue
            if backend == "bs4" and not self.enable_bs4:
                continue

            backend_candidates = candidates or [
                {"url": normalized_url, "reason": "requested", "security_downgrade": False}
            ]
            if backend in self.JINA_READER_BACKEND_ALIASES and backend_candidates:
                backend_candidates = [backend_candidates[0]]

            for candidate in backend_candidates:
                candidate_url = str(candidate.get("url") or normalized_url)
                candidate_reason = str(candidate.get("reason") or "requested")
                security_downgrade = bool(candidate.get("security_downgrade"))
                for attempt_index in range(self.max_attempts):
                    try:
                        if backend == "pyautogui":
                            result = await self._pyautogui_open_and_read_url(candidate_url)
                        elif backend == "playwright":
                            result = await self._playwright_open(candidate_url)
                        elif backend == "curl":
                            result = await self._curl_browse(candidate_url)
                        elif backend in self.JINA_READER_BACKEND_ALIASES:
                            result = await self._jina_reader_browse(candidate_url)
                        elif backend == "bs4":
                            result = await self._bs4_browse(candidate_url)
                        else:
                            attempts.append(
                                {
                                    "backend": backend,
                                    "attempt_index": attempt_index,
                                    "url": candidate_url,
                                    "candidate_reason": candidate_reason,
                                    "security_downgrade": security_downgrade,
                                    "ok": False,
                                    "retryable": False,
                                    "reason": "Unknown backend",
                                }
                            )
                            break

                        content = self._extract_content_from_result(result)
                        is_remote_file = isinstance(result, dict) and result.get("content_kind") == "remote_file"
                        blocked_reason = "" if is_remote_file else self._blocked_page_reason(content)
                        ok = is_remote_file or (
                            bool(content) and len(content) >= self.min_content_length and not blocked_reason
                        )

                        if not ok:
                            reason = blocked_reason or "content_empty_or_too_short"
                            if isinstance(result, dict):
                                error = str(result.get("error", "") or "").strip()
                                if error:
                                    reason = error
                                elif result.get("status") and not blocked_reason:
                                    reason = f"HTTP status { result.get('status') } produced no readable content"
                            elif isinstance(result, str) and result.startswith("Can not "):
                                reason = result
                            retryable = self._is_transient_error(reason)
                            attempts.append(
                                {
                                    "backend": backend,
                                    "attempt_index": attempt_index,
                                    "url": candidate_url,
                                    "candidate_reason": candidate_reason,
                                    "security_downgrade": security_downgrade,
                                    "ok": False,
                                    "retryable": retryable,
                                    "reason": reason,
                                }
                            )
                            if retryable and attempt_index + 1 < self.max_attempts:
                                if self.retry_backoff_seconds > 0:
                                    time.sleep(self.retry_backoff_seconds)
                                continue
                            break

                        trace = {
                            "ok": True,
                            "backend": backend,
                            "requested_url": requested_url,
                            "normalized_url": normalized_url,
                            "candidate_url": candidate_url,
                            "candidate_reason": candidate_reason,
                            "security_downgrade": security_downgrade,
                            "content_format": self.response_mode,
                            "content": content,
                            "attempts": attempts,
                            "retry_candidates": candidates,
                        }
                        if isinstance(result, dict):
                            trace.update(
                                {
                                    "url": result.get("url") or candidate_url,
                                    "title": result.get("title", ""),
                                    "status": result.get("status"),
                                    "content_kind": result.get("content_kind", "html"),
                                    "media_type": result.get("media_type"),
                                    "headers": result.get("headers", {}),
                                    "content_bytes": result.get("content_bytes"),
                                    "links": result.get("links", []),
                                    "canonical_links": result.get("canonical_links", []),
                                    "raw_result": result,
                                }
                            )
                        else:
                            trace.update(
                                {
                                    "url": candidate_url,
                                    "title": "",
                                    "status": None,
                                    "content_kind": "html",
                                    "raw_result": result,
                                }
                            )
                        if security_downgrade:
                            attempts.append(
                                {
                                    "backend": backend,
                                    "attempt_index": attempt_index,
                                    "url": candidate_url,
                                    "candidate_reason": candidate_reason,
                                    "security_downgrade": True,
                                    "ok": True,
                                    "retryable": False,
                                    "reason": "Browse succeeded through an http fallback after https was unavailable.",
                                }
                            )
                        return trace
                    except ImportError as e:
                        attempts.append(
                            {
                                "backend": backend,
                                "attempt_index": attempt_index,
                                "url": candidate_url,
                                "candidate_reason": candidate_reason,
                                "security_downgrade": security_downgrade,
                                "ok": False,
                                "retryable": False,
                                "reason": f"ImportError: {str(e)}",
                            }
                        )
                        break
                    except Exception as e:
                        retryable = self._is_transient_error(e)
                        attempts.append(
                            {
                                "backend": backend,
                                "attempt_index": attempt_index,
                                "url": candidate_url,
                                "candidate_reason": candidate_reason,
                                "security_downgrade": security_downgrade,
                                "ok": False,
                                "retryable": retryable,
                                "reason": str(e),
                            }
                        )
                        if retryable and attempt_index + 1 < self.max_attempts:
                            if self.retry_backoff_seconds > 0:
                                time.sleep(self.retry_backoff_seconds)
                            continue
                        break

        return {
            "ok": False,
            "requested_url": requested_url,
            "normalized_url": normalized_url,
            "content": "",
            "attempts": attempts,
            "retry_candidates": candidates,
            "error": "All browse backends failed.",
        }

    async def browse(self, url: str):
        trace = await self._browse_with_trace(url)
        if trace.get("ok"):
            return trace.get("content", "")

        attempts = trace.get("attempts", [])
        attempts = attempts if isinstance(attempts, list) else []
        reason_text = self._reason_text(attempts)
        return f"Can not browse '{self._normalize_url(url)}'.\tFallback failed: {reason_text}"
