import os
import re
import unicodedata
from datetime import date
from urllib.parse import urlparse

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

from agently import Agently
from agently.builtins.actions import Browse

TRAVEL_DATE = date.today().isoformat()
TRAVEL_DATE_OBJ = date.fromisoformat(TRAVEL_DATE)
TRAVEL_YEAR = TRAVEL_DATE_OBJ.strftime("%Y")
TRAVEL_MONTH_FULL = TRAVEL_DATE_OBJ.strftime("%B").lower()
TRAVEL_MONTH_SHORT = TRAVEL_DATE_OBJ.strftime("%b").lower()
TRAVEL_DAY_NUM = str(TRAVEL_DATE_OBJ.day)
TRAVEL_DATE_COMPACT_SHORT = TRAVEL_DATE_OBJ.strftime("%y%m%d")
ROUTE_FROM = "Beijing"
ROUTE_TO = "Los Angeles"
ROUTE_FROM_TOKENS = ["beijing", "pek", "pkx"]
ROUTE_TO_TOKENS = ["los angeles", "los-angeles", "los%20angeles", "los+angeles", "lax"]
BLOCKED_KEYWORDS = [
    "bot",
    "captcha",
    "access denied",
    "unusual traffic",
    "forbidden",
    "blocked",
    "verify you are human",
    "cloudflare",
]
ROUTE_NEAR_PATTERN = re.compile(
    r"(beijing|pek|pkx).{0,80}(to|->|→|-).{0,80}(los angeles|los-angeles|lax)"
    r"|"
    r"(los angeles|los-angeles|lax).{0,80}(to|->|→|-).{0,80}(beijing|pek|pkx)",
    re.IGNORECASE,
)
PRICE_PATTERN = re.compile(
    r"\b(USD|SGD|CNY|RMB|HKD|EUR|GBP|JPY)\s?([0-9]{2,6}(?:[.,][0-9]{1,2})?)\b|([$€£¥])\s?([0-9]{2,6}(?:[.,][0-9]{1,2})?)",
    re.IGNORECASE,
)
SYMBOL_TO_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "CNY"}
URL_PUNCT_TRANSLATION = str.maketrans(
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


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def normalize_url_text(url: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(url or "")).strip()
    normalized = normalized.translate(URL_PUNCT_TRANSLATION)
    normalized = normalized.replace("\u3000", " ")
    normalized = re.sub(r"[\r\n\t]+", "", normalized)
    normalized = normalized.strip(' "\'`')
    normalized = re.sub(r"[,;:!?]+$", "", normalized)
    return normalized


def configure_model():
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.openai.com/v1")
    model = os.getenv("DEEPSEEK_DEFAULT_MODEL", "gpt-4o-mini")
    auth = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not auth:
        raise RuntimeError(
            "Missing API key. Please set DEEPSEEK_API_KEY (or OPENAI_API_KEY) in your environment."
        )

    Agently.set_settings(
        "OpenAICompatible",
        {
            "base_url": base_url,
            "model": model,
            "model_type": "chat",
            "auth": auth,
        },
    )


def url_is_query_ready(url: str) -> bool:
    url = normalize_url_text(url)
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    lowered = url.lower()
    has_from = any(token in lowered for token in ROUTE_FROM_TOKENS)
    has_to = any(token in lowered for token in ROUTE_TO_TOKENS)
    has_iata_pair = bool(re.search(r"(pek|pkx)[/_:\-.]+lax|lax[/_:\-.]+(pek|pkx)", lowered))
    has_iso_date = TRAVEL_DATE in lowered
    has_compact_date = TRAVEL_DATE.replace("-", "") in lowered
    has_short_compact_date = TRAVEL_DATE_COMPACT_SHORT in lowered
    natural_sep = r"(?:%20|\s|[-_/+,.])+"
    has_natural_date = (
        re.search(
            rf"(?:{TRAVEL_MONTH_FULL}|{TRAVEL_MONTH_SHORT}){natural_sep}0?{TRAVEL_DAY_NUM}(?:st|nd|rd|th)?{natural_sep}{TRAVEL_YEAR}",
            lowered,
        )
        is not None
        or re.search(
            rf"{TRAVEL_YEAR}{natural_sep}(?:{TRAVEL_MONTH_FULL}|{TRAVEL_MONTH_SHORT}){natural_sep}0?{TRAVEL_DAY_NUM}(?:st|nd|rd|th)?",
            lowered,
        )
        is not None
    )
    has_google_tfs_date_hint = "/travel/flights/search" in lowered and "tfs=" in lowered
    has_date = has_iso_date or has_compact_date or has_short_compact_date or has_natural_date or has_google_tfs_date_hint
    has_flight_query_hint = any(
        marker in lowered
        for marker in ("tfs=", "/flights/", "/travel/flights/search", "origin=", "destination=", "departure=")
    )
    return has_date and ((has_from and has_to) or has_iata_pair or has_flight_query_hint)


def extract_last_opened_url(action_logs: list[dict]) -> str:
    for action_log in reversed(action_logs):
        if not isinstance(action_log, dict):
            continue
        kwargs = action_log.get("kwargs")
        if not isinstance(kwargs, dict):
            continue
        url = kwargs.get("url")
        if isinstance(url, str) and url.strip():
            return normalize_url_text(url)
    return ""


def extract_last_open_result(action_logs: list[dict]) -> dict:
    for action_log in reversed(action_logs):
        if not isinstance(action_log, dict):
            continue
        result = action_log.get("result")
        if isinstance(result, dict) and "ok" in result:
            return result
    return {}


def extract_last_read_page(action_logs: list[dict]) -> tuple[str, str, int | None, str]:
    for action_log in reversed(action_logs):
        if not isinstance(action_log, dict):
            continue
        result = action_log.get("result")
        if not isinstance(result, dict):
            continue
        content = str(result.get("content", "") or "")
        title = str(result.get("title", "") or "")
        status = result.get("status")
        if not isinstance(status, int):
            status = None
        final_url = str(result.get("url", "") or "")
        if content or title or status is not None or final_url:
            return content, title, status, final_url
    return "", "", None, ""


def extract_price_quotes_from_text(content: str, source_url: str) -> list[dict]:
    if not content:
        return []
    quotes: list[dict] = []
    seen: set[tuple[str, float]] = set()
    domain = urlparse(source_url).netloc or source_url
    for match in PRICE_PATTERN.finditer(content):
        if match.group(1) and match.group(2):
            currency = match.group(1).upper()
            amount_text = match.group(2)
            price_text = f"{currency} {amount_text}"
        elif match.group(3) and match.group(4):
            currency = SYMBOL_TO_CURRENCY.get(match.group(3), "UNKNOWN")
            amount_text = match.group(4)
            price_text = f"{match.group(3)}{amount_text}"
        else:
            continue

        normalized = amount_text.replace(",", "")
        try:
            amount = float(normalized)
        except ValueError:
            continue
        key = (currency, amount)
        if key in seen:
            continue
        seen.add(key)
        quotes.append(
            {
                "source": domain,
                "price_text": price_text,
                "currency": currency,
                "amount": amount,
                "note": "Regex fallback extraction from page content",
            }
        )
        if len(quotes) >= 8:
            break
    return quotes


def normalize_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().strip()
    except Exception:
        return ""


def add_failure_feedback(feedbacks: list[str], url: str, reason: str):
    domain = normalize_domain(url) or url
    item = f"{domain}: {reason}"
    if item not in feedbacks:
        feedbacks.append(item)


def infer_route_match(content: str, source_url: str = "") -> bool:
    lowered = content.lower()
    from_match = any(token in lowered for token in ROUTE_FROM_TOKENS)
    to_match = any(token in lowered for token in ROUTE_TO_TOKENS)
    near_match = bool(ROUTE_NEAR_PATTERN.search(lowered))
    has_date = TRAVEL_DATE in lowered or TRAVEL_DATE.replace("-", "/") in lowered
    url_ready = url_is_query_ready(source_url)
    if near_match:
        return True
    if from_match and to_match and has_date:
        return True
    if url_ready and from_match and to_match:
        return True
    return False


def infer_page_status(
    content: str,
    title: str,
    route_match: bool,
    quote_count: int,
    status_code: int | None = None,
    final_url: str = "",
) -> str:
    merged = f"{title}\n{content}\n{final_url}".lower()
    if status_code in (401, 403, 406, 409, 429, 503):
        return "blocked"
    if status_code == 202 and not content.strip():
        return "blocked"
    if "/help/bots" in merged or "bot or not" in merged:
        return "blocked"
    if any(keyword in merged for keyword in BLOCKED_KEYWORDS):
        return "blocked"
    if not route_match:
        return "not_relevant"
    if quote_count <= 0:
        return "no_price"
    return "valid"


def pick_programmatic_best_offer(read_step_results: list[dict]) -> dict | None:
    candidates: list[dict] = []
    for item in read_step_results:
        if not isinstance(item, dict):
            continue
        if item.get("page_status") != "valid":
            continue
        quotes = item.get("price_quotes", [])
        if not isinstance(quotes, list):
            continue
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            amount = quote.get("amount")
            currency = quote.get("currency")
            if not isinstance(amount, (int, float)) or not isinstance(currency, str):
                continue
            candidates.append(
                {
                    "source": str(quote.get("source", "")),
                    "price_text": str(quote.get("price_text", f"{currency} {amount}")),
                    "currency": currency.upper(),
                    "amount": float(amount),
                    "url": str(item.get("url", "")),
                    "confidence": str(item.get("confidence", "medium")),
                }
            )
    if not candidates:
        return None

    currency_counts: dict[str, int] = {}
    for candidate in candidates:
        currency = candidate["currency"]
        currency_counts[currency] = currency_counts.get(currency, 0) + 1
    dominant_currency = max(currency_counts.items(), key=lambda item: item[1])[0]
    dominant_candidates = [candidate for candidate in candidates if candidate["currency"] == dominant_currency]
    best = min(dominant_candidates, key=lambda candidate: candidate["amount"])
    return best


def run_one_open_step(
    scan_agent,
    opened_urls: list[str],
    rejected_urls: list[str],
    blocked_domains: list[str],
    recent_failures: list[str],
    step: int,
    max_steps: int,
):
    trimmed_opened_urls = opened_urls[-8:]
    trimmed_rejected_urls = rejected_urls[-8:]
    trimmed_recent_failures = recent_failures[-8:]
    trimmed_blocked_domains = blocked_domains[-8:]
    response = (
        scan_agent.input(
            {
                "route_from": ROUTE_FROM,
                "route_to": ROUTE_TO,
                "travel_date": TRAVEL_DATE,
                "opened_urls": trimmed_opened_urls,
                "rejected_urls": trimmed_rejected_urls,
                "blocked_domains": trimmed_blocked_domains,
                "recent_failures": trimmed_recent_failures,
                "step": step,
                "max_steps": max_steps,
            }
        )
        .instruct(
            """
            Goal: find airfare result pages for a one-way flight from {route_from} to {route_to} on {travel_date}.
            You MUST use the available tool first to open exactly one URL that you choose.
            Hard constraints:
            1) Avoid URL duplicates from {opened_urls} and {rejected_urls}.
            2) Avoid domains from {blocked_domains} if possible.
            3) Choose a deep-link search result URL, not a generic homepage.
            4) URL should include both route and date hints.
            5) Use {recent_failures} to avoid repeating known bad patterns.
            Do not fabricate ticket prices in this step.
            """
        )
        .output(
            {
                "step": (int, "Current step"),
                "selected_url": (str, "URL selected by the model"),
                "opened": (bool, "Whether open action succeeded"),
                "note": (str, "Short note for this step"),
            }
        )
        .get_response()
    )
    result = response.result.get_data()
    extra = response.result.full_result_data.get("extra", {})
    return result, extra.get("action_logs", extra.get("tool_logs", []))


def run_one_read_step(read_agent, target_url: str):
    response = (
        read_agent.input(
            {
                "route_from": ROUTE_FROM,
                "route_to": ROUTE_TO,
                "travel_date": TRAVEL_DATE,
                "target_url": target_url,
            }
        )
        .instruct(
            """
            You MUST use the available tool first to read page content for {target_url}.
            The open step already navigated a page; do not invent another unrelated URL here.
            Extract only explicit airfare evidence visible in page content.
            Focus on one-way flights from {route_from} to {route_to} on {travel_date}.
            Return:
            - route_match: whether content clearly matches the target route/date context
            - page_status: valid / blocked / not_relevant / no_price
            If no clear price is visible, return empty quotes and explain why.
            """
        )
        .output(
            {
                "url": (str, "Analyzed URL"),
                "route_match": (bool, "Whether route/date appears to match"),
                "page_status": (str, "valid / blocked / not_relevant / no_price"),
                "price_quotes": [
                    {
                        "source": (str, "Site or provider"),
                        "price_text": (str, "Raw price text"),
                        "currency": (str, "Currency code/symbol"),
                        "amount": (float, "Numeric amount"),
                        "note": (str, "Context for the quote"),
                    }
                ],
                "lowest_price_text": (str, "Lowest visible price text on this page"),
                "confidence": (str, "low / medium / high"),
                "note": (str, "Extraction note"),
            }
        )
        .get_response()
    )
    result = response.result.get_data()
    extra = response.result.full_result_data.get("extra", {})
    return result, extra.get("action_logs", extra.get("tool_logs", []))


def post_process_read_result(read_result: dict, read_logs: list[dict], target_url: str) -> dict:
    result = read_result if isinstance(read_result, dict) else {}
    content, title, status_code, final_url = extract_last_read_page(read_logs)
    actual_url = normalize_url_text(final_url or target_url)
    route_match = (
        bool(result.get("route_match"))
        if isinstance(result.get("route_match"), bool)
        else infer_route_match(content, actual_url)
    )

    price_quotes = result.get("price_quotes", [])
    if not isinstance(price_quotes, list):
        price_quotes = []
    if not price_quotes and route_match:
        price_quotes = extract_price_quotes_from_text(content, actual_url)

    page_status = str(result.get("page_status", "")).strip().lower()
    inferred_status = infer_page_status(
        content=content,
        title=title,
        route_match=route_match,
        quote_count=len(price_quotes),
        status_code=status_code,
        final_url=final_url,
    )
    if inferred_status == "blocked":
        page_status = "blocked"
    elif page_status not in ("valid", "blocked", "not_relevant", "no_price"):
        page_status = inferred_status
    if page_status == "valid" and len(price_quotes) == 0:
        page_status = "no_price"

    if not result.get("lowest_price_text") and price_quotes:
        lowest_quote = min(
            [quote for quote in price_quotes if isinstance(quote, dict) and isinstance(quote.get("amount"), (int, float))],
            key=lambda quote: float(quote["amount"]),
            default=None,
        )
        if lowest_quote:
            result["lowest_price_text"] = str(lowest_quote.get("price_text", ""))

    confidence = str(result.get("confidence", "")).lower()
    if confidence not in ("low", "medium", "high"):
        if page_status == "valid" and price_quotes:
            confidence = "high"
        elif page_status in ("blocked", "not_relevant"):
            confidence = "low"
        else:
            confidence = "medium"

    result.update(
        {
            "url": actual_url,
            "expected_url": normalize_url_text(target_url),
            "final_url": final_url,
            "status_code": status_code,
            "route_match": route_match,
            "page_status": page_status,
            "price_quotes": price_quotes,
            "confidence": confidence,
        }
    )
    return result


def summarize_execution(
    summary_agent,
    mode: str,
    opened_urls: list[str],
    open_step_results: list[dict],
    read_step_results: list[dict],
    programmatic_best_offer: dict | None,
):
    response = (
        summary_agent.input(
            {
                "route_from": ROUTE_FROM,
                "route_to": ROUTE_TO,
                "travel_date": TRAVEL_DATE,
                "mode": mode,
                "opened_urls": opened_urls,
                "open_step_results": open_step_results,
                "read_step_results": read_step_results,
                "programmatic_best_offer": programmatic_best_offer,
            }
        )
        .instruct(
            """
            Produce a final airfare conclusion and purchase recommendation.
            Requirements:
            1) Always provide `final_price_summary` (use "manual_required" only when evidence is truly insufficient).
            2) Always provide `buying_recommendation` with actionable guidance.
            3) Never fabricate prices; rely on read_step_results and programmatic_best_offer.
            4) If programmatic_best_offer is present, do not return manual_required.
            """
        )
        .output(
            {
                "route": (str, "Route summary"),
                "travel_date": (str, "Date"),
                "mode": (str, "dry_run or real"),
                "opened_url_count": (int, "Number of opened URLs"),
                "analyzed_url_count": (int, "Number of content-analyzed URLs"),
                "final_price_summary": (str, "Final price conclusion or manual_required"),
                "best_offer": {
                    "source": (str, "Best source"),
                    "price_text": (str, "Best price text"),
                    "url": (str, "URL of best offer"),
                    "confidence": (str, "Confidence level"),
                },
                "buying_recommendation": (str, "Whether to buy now or wait, with reason"),
                "manual_review_checklist": [(str, "Manual verification checklist")],
            }
        )
        .get_response()
    )
    result = response.result.get_data()
    if not isinstance(result, dict):
        result = {}

    if not result.get("route"):
        result["route"] = f"{ROUTE_FROM} to {ROUTE_TO}"
    if not result.get("travel_date"):
        result["travel_date"] = TRAVEL_DATE
    if not isinstance(result.get("opened_url_count"), int):
        result["opened_url_count"] = len(opened_urls)
    if not isinstance(result.get("analyzed_url_count"), int):
        result["analyzed_url_count"] = len(read_step_results)

    if programmatic_best_offer:
        final_summary = str(result.get("final_price_summary", "")).strip().lower()
        if not final_summary or final_summary == "manual_required":
            result["final_price_summary"] = programmatic_best_offer.get("price_text", "manual_required")
        best_offer = result.get("best_offer")
        if not isinstance(best_offer, dict) or not best_offer.get("price_text"):
            result["best_offer"] = {
                "source": programmatic_best_offer.get("source", ""),
                "price_text": programmatic_best_offer.get("price_text", ""),
                "url": programmatic_best_offer.get("url", ""),
                "confidence": programmatic_best_offer.get("confidence", "medium"),
            }
        if not result.get("buying_recommendation"):
            result["buying_recommendation"] = (
                f"Current best observed fare is {programmatic_best_offer.get('price_text', '')}. "
                "If your schedule is fixed, consider booking now after checking baggage and refund rules."
            )
    else:
        if not result.get("final_price_summary"):
            result["final_price_summary"] = "manual_required"
        best_offer = result.get("best_offer")
        if not isinstance(best_offer, dict):
            result["best_offer"] = {
                "source": "",
                "price_text": "",
                "url": "",
                "confidence": "low",
            }
        if not result.get("buying_recommendation"):
            result["buying_recommendation"] = (
                "No reliable fare quote was extracted. Use at least two different flight search sites manually, "
                "then compare fare rules (refund/change/baggage) before purchase."
            )
    return result


def main():
    configure_model()
    scan_agent = Agently.create_agent("flight-open-pyautogui")
    read_agent = Agently.create_agent("flight-read-page")
    summary_agent = Agently.create_agent("flight-summary")

    run_real = env_bool("RUN_PYAUTOGUI", True)
    max_steps = int(os.getenv("FLIGHT_SCAN_MAX_STEPS", "6"))
    read_limit = int(os.getenv("FLIGHT_PRICE_READ_LIMIT", "4"))
    min_valid_urls = int(os.getenv("FLIGHT_MIN_VALID_URLS", "1"))
    pyautogui_open_mode = os.getenv("PYAUTOGUI_OPEN_MODE", "hotkey").strip().lower()
    if pyautogui_open_mode not in ("hotkey", "system"):
        pyautogui_open_mode = "hotkey"
    pyautogui_browser_app = os.getenv("PYAUTOGUI_BROWSER_APP", "Google Chrome").strip() or None
    pyautogui_activate_browser = env_bool(
        "PYAUTOGUI_ACTIVATE_BROWSER",
        pyautogui_open_mode == "hotkey",
    )
    pyautogui_wait_seconds = env_float("PYAUTOGUI_WAIT_SECONDS", 1.2)
    pyautogui_type_interval = env_float("PYAUTOGUI_TYPE_INTERVAL", 0.01)
    pyautogui_new_tab = env_bool("PYAUTOGUI_NEW_TAB", True)

    print(
        "[PYAUTOGUI_CONFIG] "
        f"run_real={run_real} open_mode={pyautogui_open_mode} "
        f"activate_browser={pyautogui_activate_browser} browser_app={pyautogui_browser_app} "
        f"new_tab={pyautogui_new_tab} read_source=browse_auto_fallback"
    )

    browse_tool = Browse(
        fallback_order=("pyautogui", "playwright", "bs4"),
        enable_pyautogui=run_real,
        enable_playwright=True,
        enable_bs4=True,
        pyautogui_new_tab=pyautogui_new_tab,
        pyautogui_wait_seconds=pyautogui_wait_seconds,
        pyautogui_dry_run=not run_real,
        pyautogui_type_interval=pyautogui_type_interval,
        pyautogui_open_mode=pyautogui_open_mode,
        pyautogui_activate_browser=pyautogui_activate_browser,
        pyautogui_browser_app=pyautogui_browser_app,
        response_mode="markdown",
        max_content_length=18000,
    )

    async def open_flight_site(url: str) -> dict:
        """
        Open one URL in browser and extract page content via Browse fallback chain.
        """
        normalized_url = normalize_url_text(url)
        content = await browse_tool.browse(url=normalized_url)
        ok = isinstance(content, str) and not content.startswith("Can not browse")
        return {
            "ok": ok,
            "requested_url": normalized_url,
            "url": normalized_url,
            "content": content if isinstance(content, str) else str(content),
            "read_source": "browse_auto_fallback",
        }

    async def read_flight_page(url: str) -> dict:
        """
        Read one page content for fare extraction.
        """
        normalized_url = normalize_url_text(url)
        content = await browse_tool.browse(url=normalized_url)
        ok = isinstance(content, str) and not content.startswith("Can not browse")
        return {
            "ok": ok,
            "requested_url": normalized_url,
            "url": normalized_url,
            "content": content if isinstance(content, str) else str(content),
            "read_source": "browse_auto_fallback",
            "expected_url": normalized_url,
        }

    scan_agent.use_actions(open_flight_site)
    read_agent.use_actions(read_flight_page)

    opened_urls: list[str] = []
    valid_urls: list[str] = []
    rejected_urls: list[str] = []
    blocked_domains: list[str] = []
    recent_failures: list[str] = []
    analyzed_urls: set[str] = set()
    open_step_results: list[dict] = []
    read_step_results: list[dict] = []
    all_action_logs: list[dict] = []

    for step in range(1, max_steps + 1):
        result, action_logs = run_one_open_step(
            scan_agent=scan_agent,
            opened_urls=opened_urls,
            rejected_urls=rejected_urls,
            blocked_domains=blocked_domains,
            recent_failures=recent_failures,
            step=step,
            max_steps=max_steps,
        )
        open_step_results.append(result if isinstance(result, dict) else {"step": step, "opened": False})
        all_action_logs.extend(action_logs)

        opened_url = extract_last_opened_url(action_logs)
        if not opened_url and isinstance(result, dict):
            selected_url = result.get("selected_url")
            if isinstance(selected_url, str) and selected_url.strip():
                opened_url = normalize_url_text(selected_url)
        if opened_url and opened_url not in opened_urls:
            opened_urls.append(opened_url)

        quality_ok = url_is_query_ready(opened_url)
        open_exec_result = extract_last_open_result(action_logs)
        opened = bool(open_exec_result.get("ok")) if isinstance(open_exec_result, dict) else False
        if not open_exec_result and isinstance(result, dict):
            opened = bool(result.get("opened"))
        read_status = "skipped"
        if not opened:
            if opened_url and opened_url not in rejected_urls:
                rejected_urls.append(opened_url)
            add_failure_feedback(recent_failures, opened_url or "unknown", "open_url failed")
            read_status = "open_failed"
        elif not opened_url:
            read_status = "no_url"
        elif not quality_ok:
            if opened_url not in rejected_urls:
                rejected_urls.append(opened_url)
            add_failure_feedback(
                recent_failures,
                opened_url,
                "URL is not a deep-link query page with route/date hints",
            )
            read_status = "not_query_ready"
        elif opened_url in analyzed_urls:
            read_status = "duplicate_url"
        elif len(read_step_results) >= read_limit:
            read_status = "read_limit_reached"
        else:
            analyzed_urls.add(opened_url)
            read_result, read_logs = run_one_read_step(read_agent, opened_url)
            processed = post_process_read_result(read_result, read_logs, opened_url)
            read_step_results.append(processed)
            all_action_logs.extend(read_logs)
            read_status = str(processed.get("page_status", "unknown"))
            if read_status == "valid":
                if opened_url not in valid_urls:
                    valid_urls.append(opened_url)
            else:
                if opened_url not in rejected_urls:
                    rejected_urls.append(opened_url)
                add_failure_feedback(
                    recent_failures,
                    opened_url,
                    f"page_status={read_status}",
                )
                domain = normalize_domain(opened_url)
                if read_status == "blocked" and domain and domain not in blocked_domains:
                    blocked_domains.append(domain)
            print(
                f"[READ STEP {len(read_step_results)}/{read_limit}] "
                f"status={processed.get('page_status')} "
                f"route_match={processed.get('route_match')} url={opened_url}"
            )

        print(
            f"[OPEN STEP {step}/{max_steps}] opened={opened} "
            f"quality_ok={quality_ok} read_status={read_status} url={opened_url}"
        )
        if isinstance(open_exec_result, dict) and open_exec_result:
            open_mode = open_exec_result.get("open_mode")
            hint = open_exec_result.get("hint")
            error = open_exec_result.get("error")
            if error:
                print(f"[OPEN ERROR] mode={open_mode} error={error}")
            if hint:
                print(f"[OPEN HINT] {hint}")

        if len(valid_urls) >= min_valid_urls:
            print(f"[STOP] reached min_valid_urls={min_valid_urls}.")
            break
        if step == max_steps:
            print(f"[STOP] reached max steps={max_steps}.")
            break
        if len(read_step_results) >= read_limit and len(valid_urls) > 0:
            print(f"[STOP] reached read limit={read_limit} with valid result(s).")
            break

    programmatic_best_offer = pick_programmatic_best_offer(read_step_results)

    mode = "real" if run_real else "dry_run"
    final_report = summarize_execution(
        summary_agent=summary_agent,
        mode=mode,
        opened_urls=opened_urls,
        open_step_results=open_step_results,
        read_step_results=read_step_results,
        programmatic_best_offer=programmatic_best_offer,
    )

    print("[FLIGHT_CHECK_RESULT]")
    print(final_report)
    print("[ACTION_LOGS]")
    print(all_action_logs)


if __name__ == "__main__":
    main()
