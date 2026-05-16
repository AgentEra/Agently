import os
import re
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

from agently import Agently
from agently.builtins.actions import Browse

ISSUES_URL = "https://github.com/AgentEra/Agently/issues?q=is%3Aissue%20state%3Aopen"
URL_PATTERN = re.compile(r"https?://[^\s\]\)\"'>]+")


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


def with_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(query_pairs)
    query["page"] = str(page)
    new_query = urlencode(query, doseq=True)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )


def extract_issue_links_from_action_logs(action_logs: list[dict]) -> list[str]:
    issue_links: list[str] = []
    seen: set[str] = set()
    for action_log in action_logs:
        if not isinstance(action_log, dict):
            continue
        result = action_log.get("result")
        if not isinstance(result, dict):
            continue

        urls: list[str] = []
        content = result.get("content")
        if isinstance(content, str) and content:
            urls.extend(URL_PATTERN.findall(content))

        links = result.get("links", [])
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict):
                    link_url = str(link.get("url", "")).strip()
                    if link_url:
                        urls.append(link_url)

        for link_url in urls:
            parsed = urlparse(link_url)
            path_parts = [segment for segment in parsed.path.split("/") if segment]
            if (
                parsed.netloc.endswith("github.com")
                and len(path_parts) >= 4
                and path_parts[-2] == "issues"
                and path_parts[-1].isdigit()
            ):
                canonical_issue_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
                if canonical_issue_url not in seen:
                    seen.add(canonical_issue_url)
                    issue_links.append(canonical_issue_url)
    return issue_links


def has_next_page_from_action_logs(action_logs: list[dict], current_page: int) -> bool:
    target_page = str(current_page + 1)
    for action_log in action_logs:
        if not isinstance(action_log, dict):
            continue
        result = action_log.get("result")
        if not isinstance(result, dict):
            continue
        urls: list[str] = []
        content = result.get("content")
        if isinstance(content, str) and content:
            urls.extend(URL_PATTERN.findall(content))

        links = result.get("links", [])
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict):
                    link_url = str(link.get("url", "")).strip()
                    if link_url:
                        urls.append(link_url)

        for link_url in urls:
            parsed = urlparse(link_url)
            if not parsed.netloc.endswith("github.com"):
                continue
            if not parsed.path.endswith("/issues"):
                continue
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if query.get("page") == target_page:
                return True
    return False


def analyze_one_page(scan_agent, page_url: str, page: int):
    response = (
        scan_agent.input(
            {
                "target_repo": "AgentEra/Agently",
                "issues_url": page_url,
                "page": page,
            }
        )
        .instruct(
            """
            You MUST use the available tool to open {issues_url} first.
            Extract issues from this page only.
            Prefer the content field from tool result because it preserves link-text relationship.
            Prefer real issue URL if present; otherwise return empty string.
            If no issue is listed on this page, return an empty list.
            """
        )
        .output(
            {
                "page": (int, "Current page number"),
                "issues": [
                    {
                        "title": (str, "Issue title"),
                        "url": (str, "Issue URL"),
                        "risk_hint": (str, "A short risk hint"),
                    }
                ],
                "has_next_page_guess": (bool, "Whether another issues page likely exists"),
                "note": (str, "Short note for this page"),
            }
        )
        .get_response()
    )
    result = response.result.get_data()
    extra = response.result.full_result_data.get("extra", {})
    return result, extra.get("action_logs", extra.get("tool_logs", []))


def analyze_issue_detail(scan_agent, issue_url: str):
    response = (
        scan_agent.input(
            {
                "issue_url": issue_url,
            }
        )
        .instruct(
            """
            You MUST use the available tool to open {issue_url} first.
            Prefer the content field from tool result because it preserves link-text relationship.
            Then extract key information from this issue page.
            """
        )
        .output(
            {
                "url": (str, "Issue URL"),
                "title": (str, "Issue title"),
                "state": (str, "open / closed / unknown"),
                "labels": [(str, "Issue labels")],
                "risk_level_hint": (str, "low / medium / high"),
                "summary": (str, "1-3 sentence issue summary"),
            }
        )
        .get_response()
    )
    result = response.result.get_data()
    extra = response.result.full_result_data.get("extra", {})
    return result, extra.get("action_logs", extra.get("tool_logs", []))


def summarize_all(summary_agent, page_results: list[dict], issues: list[dict], issue_details: list[dict]):
    response = (
        summary_agent.input(
            {
                "target_repo": "AgentEra/Agently",
                "page_results": page_results,
                "issues": issues,
                "issue_details": issue_details,
            }
        )
        .instruct(
            """
            Summarize the paged issue scan result.
            Focus on risk level and actionable follow-ups.
            Prefer `issue_details` over title-only records when conflict exists.
            """
        )
        .output(
            {
                "repo": (str, "Repository"),
                "total_pages_checked": (int, "How many pages were checked"),
                "total_unique_issues": (int, "How many unique issues were found"),
                "deep_dive_issues_checked": (int, "How many issue detail pages were checked"),
                "summary": (str, "3-6 sentence summary"),
                "high_priority_count_estimate": (int, "Estimated number of high-priority issues"),
                "issues": [
                    {
                        "title": (str, "Issue title"),
                        "url": (str, "Issue URL"),
                        "risk_level": (str, "low / medium / high"),
                        "why_it_matters": (str, "Why this matters"),
                    }
                ],
                "next_actions": [(str, "Concrete action item")],
            }
        )
        .get_response()
    )
    return response.result.get_data()


def main():
    configure_model()
    scan_agent = Agently.create_agent("issue-checker-loop")
    summary_agent = Agently.create_agent("issue-checker-summary")
    max_pages = int(os.getenv("ISSUE_SCAN_MAX_PAGES", "3"))
    issue_detail_limit = int(os.getenv("ISSUE_DETAIL_LIMIT", "5"))
    playwright = Browse(
        enable_pyautogui=False,
        enable_playwright=True,
        enable_bs4=False,
        playwright_headless=True,
        playwright_timeout=45000,
        response_mode="markdown",
        max_content_length=16000,
        playwright_include_links=False,
    )

    async def open_issues_page(url: str) -> dict:
        """
        Open GitHub issues page and return rendered content.
        """
        content = await playwright.browse(url=url)
        ok = isinstance(content, str) and not content.startswith("Can not browse")
        return {
            "ok": ok,
            "requested_url": url,
            "url": url,
            "content": content if isinstance(content, str) else str(content),
        }

    scan_agent.use_actions(open_issues_page)

    page_results: list[dict] = []
    all_action_logs: list[dict] = []
    all_issues: list[dict] = []
    issue_details: list[dict] = []
    seen_issue_urls: set[str] = set()
    seen_title_only: set[str] = set()

    for page in range(1, max_pages + 1):
        page_url = with_page(ISSUES_URL, page)
        page_result, action_logs = analyze_one_page(scan_agent, page_url, page)
        page_results.append(
            {
                "page": page,
                "url": page_url,
                "result": page_result,
            }
        )
        all_action_logs.extend(action_logs)

        extracted_issue_links = extract_issue_links_from_action_logs(action_logs)
        for issue_url in extracted_issue_links:
            if issue_url in seen_issue_urls:
                continue
            seen_issue_urls.add(issue_url)
            all_issues.append(
                {
                    "title": "",
                    "url": issue_url,
                    "risk_hint": "extracted from issue_links",
                    "page": page,
                }
            )

        issues = page_result.get("issues", []) if isinstance(page_result, dict) else []
        new_count = 0
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            title = str(issue.get("title", "")).strip()
            url = str(issue.get("url", "")).strip()
            if url:
                if url in seen_issue_urls:
                    continue
                seen_issue_urls.add(url)
            elif title:
                if title in seen_title_only:
                    continue
                seen_title_only.add(title)
            else:
                continue
            all_issues.append(
                {
                    "title": title,
                    "url": url,
                    "risk_hint": str(issue.get("risk_hint", "")).strip(),
                    "page": page,
                }
            )
            new_count += 1

        print(
            f"[PAGE {page}] url={page_url} "
            f"issues={len(issues)} extracted_issue_links={len(extracted_issue_links)} new={new_count}"
        )

        has_next_from_links = has_next_page_from_action_logs(action_logs, page)
        has_next_from_model = page_result.get("has_next_page_guess", False) if isinstance(page_result, dict) else False
        has_next = has_next_from_links or has_next_from_model
        if not has_next:
            print(f"[STOP] no next page detected at page {page}.")
            break
        if new_count == 0 and not extracted_issue_links:
            print(f"[STOP] no new issues found at page {page}.")
            break

    issue_urls_for_detail = [issue["url"] for issue in all_issues if isinstance(issue.get("url"), str) and issue["url"]]
    issue_urls_for_detail = issue_urls_for_detail[:issue_detail_limit]
    for index, issue_url in enumerate(issue_urls_for_detail, start=1):
        detail_result, detail_action_logs = analyze_issue_detail(scan_agent, issue_url)
        issue_details.append(detail_result if isinstance(detail_result, dict) else {"url": issue_url})
        all_action_logs.extend(detail_action_logs)
        print(f"[DETAIL {index}] url={issue_url}")

    final_report = summarize_all(summary_agent, page_results, all_issues, issue_details)

    print("[ISSUE_CHECK_RESULT]")
    print(final_report)
    print("[ACTION_LOGS]")
    print(all_action_logs)


if __name__ == "__main__":
    main()
