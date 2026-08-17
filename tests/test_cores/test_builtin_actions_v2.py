import copy
import json
import sqlite3
from typing import Any, cast
import importlib

import pytest

from agently import Agently
from agently.builtins.actions import ACP, Browse, Cmd, RuntimePreflight, Search
from agently.builtins.actions.ACP import LocalACPProvider
from agently.builtins.tools import Browse as LegacyBrowse
from agently.builtins.tools import Cmd as LegacyCmd
from agently.builtins.tools import Search as LegacySearch
from agently.core.application.AgentExecution.Context import AgentExecutionContext
from agently.core import TaskWorkspace
from agently.core.runtime.RuntimeContext import bind_runtime_context


def test_builtins_actions_is_preferred_import_path_and_tools_is_facade():
    assert issubclass(LegacySearch, Search)
    assert issubclass(LegacyBrowse, Browse)
    assert issubclass(LegacyCmd, Cmd)
    assert not hasattr(Search(timeout=1), "tool_info_list")
    assert hasattr(LegacySearch(timeout=1), "tool_info_list")
    assert not hasattr(Browse(), "tool_info_list")
    assert hasattr(LegacyBrowse(), "tool_info_list")
    assert not hasattr(Cmd(), "tool_info_list")
    assert hasattr(LegacyCmd(), "tool_info_list")


def test_runtime_preflight_registers_structured_code_runtime_action():
    agent = Agently.create_agent()
    RuntimePreflight().register_actions(agent.action, action_id="inspect_runtimes")

    spec = agent.action.action_registry.get_spec("inspect_runtimes")
    assert spec is not None
    assert spec.get("side_effect_level") == "read"
    assert "without installing software" in str(spec.get("desc", ""))

    result = agent.action.execute_action("inspect_runtimes", {})
    assert result.get("status") == "success"
    assert result.get("schema_version") == "code_runtime_environment/v1"
    assert result.get("data", {}).get("schema_version") == "code_runtime_environment/v1"
    assert result.get("result", {}).get("schema_version") == "code_runtime_environment/v1"
    assert result.get("install_policy") == "not_allowed"
    assert result.get("package_manager_policy") == "not_allowed"
    assert "python_current" in result.get("candidate_order", [])
    assert "python_current" in result.get("available_runtime_ids", [])
    current = next(item for item in result.get("candidates", []) if item.get("runtime_id") == "python_current")
    assert current.get("available") is True
    assert current.get("source_file") == "reconcile.py"
    assert current.get("run_commands")


def test_runtime_preflight_can_hide_unavailable_candidates():
    agent = Agently.create_agent()
    RuntimePreflight(
        candidates=[
            {
                "runtime_id": "missing_runtime",
                "language": "NopeLang",
                "commands": ["definitely-missing-runtime-for-agently-test"],
                "source_file": "main.nope",
                "run_commands": ["definitely-missing-runtime-for-agently-test main.nope"],
            }
        ]
    ).register_actions(agent.action, action_id="inspect_custom_runtimes")

    with_unavailable = agent.action.execute_action("inspect_custom_runtimes", {"include_unavailable": True})
    assert with_unavailable.get("available_runtime_ids") == []
    assert with_unavailable.get("selected_runtime_hint") == ""
    assert with_unavailable.get("candidates", [])[0].get("available") is False

    without_unavailable = agent.action.execute_action("inspect_custom_runtimes", {"include_unavailable": False})
    assert without_unavailable.get("candidates") == []


def test_mcp_and_acp_optional_dependencies_wait_for_explicit_use(monkeypatch):
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def record_lazy_import(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        raise AssertionError("optional dependency should not be loaded without explicit use")

    registrar_module = importlib.import_module("agently.core.operation.Action.ActionResourceRegistrar")
    acp_module = importlib.import_module("agently.builtins.actions.ACP")
    monkeypatch.setattr(registrar_module.LazyImport, "import_package", record_lazy_import)
    monkeypatch.setattr(acp_module.LazyImport, "import_package", record_lazy_import)

    agent = Agently.create_agent()
    agent.use_actions(Cmd(allowed_cmd_prefixes=["echo"]))

    assert calls == []


@pytest.mark.asyncio
async def test_cmd_without_task_workspace_boundary_fails_closed_no_cwd_fallback(tmp_path):
    # Without a TaskWorkspace-issued working directory, Cmd must refuse instead of
    # silently running in the process cwd (spec sections 8.6 / 9).
    cmd = Cmd(allowed_cmd_prefixes=["pwd"])
    assert cmd.allowed_workdir_roots == []
    result = await cmd.run("pwd")
    assert result["ok"] is False
    assert result["reason"] == "task_workspace_boundary_required"


@pytest.mark.asyncio
async def test_cmd_with_task_workspace_boundary_runs_in_injected_root(tmp_path):
    cmd = Cmd(allowed_cmd_prefixes=["pwd"], allowed_workdir_roots=[str(tmp_path)])
    result = await cmd.run("pwd")
    assert result["ok"] is True
    assert str(tmp_path) in result["stdout"]


@pytest.mark.asyncio
async def test_cmd_resolves_relative_workdir_from_injected_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    cmd = Cmd(allowed_cmd_prefixes=["pwd"], allowed_workdir_roots=[str(tmp_path)])

    root_result = await cmd.run("pwd", workdir=".")
    child_result = await cmd.run("pwd", workdir="project")

    assert root_result["ok"] is True
    assert child_result["ok"] is True
    assert root_result["stdout"].strip() == str(tmp_path.resolve())
    assert child_result["stdout"].strip() == str(project.resolve())


@pytest.mark.asyncio
async def test_cmd_collapses_task_workspace_root_prefixed_relative_workdir(tmp_path):
    execution_root = tmp_path / ".agently" / "files" / "execution-1"
    execution_root.mkdir(parents=True)
    child = execution_root / "reports"
    child.mkdir()
    cmd = Cmd(
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(execution_root)],
    )

    root_result = await cmd.run(
        "pwd",
        workdir=".agently/files/execution-1",
    )
    child_result = await cmd.run(
        "pwd",
        workdir=".agently/files/execution-1/reports",
    )

    assert root_result["ok"] is True
    assert child_result["ok"] is True
    assert root_result["stdout"].strip() == str(execution_root.resolve())
    assert child_result["stdout"].strip() == str(child.resolve())


def test_v2_default_plugins_are_registered():
    action_executors = set(Agently.plugin_manager.get_plugin_list("ActionExecutor"))
    environment_providers = set(Agently.plugin_manager.get_plugin_list("ExecutionResourceProvider"))

    assert {
        "SearchActionExecutor",
        "BrowseActionExecutor",
        "CodeExecutionActionExecutor",
        "DockerActionExecutor",
        "SQLiteActionExecutor",
    }.issubset(action_executors)
    assert {
        "ACPExecutionResourceProvider",
        "TrustedLocalExecutionResourceProvider",
        "DockerExecutionResourceProvider",
        "GVisorDockerExecutionResourceProvider",
        "SeatbeltExecutionResourceProvider",
        "BrowserExecutionResourceProvider",
        "SQLiteExecutionResourceProvider",
    }.issubset(environment_providers)


class FakeACPProvider:
    def __init__(self, agents: list[dict[str, Any]]):
        self.agents = agents
        self.runs: list[dict[str, Any]] = []

    def discover_agents(self, *, root: str, agent_ids=None, timeout_seconds=None):
        return {
            "agents": self.agents,
            "diagnostics": [{"code": "fake.discovery", "root": root, "agent_ids": agent_ids}],
        }

    async def async_run_task(
        self,
        *,
        agent_id: str,
        task: str,
        root: str,
        working_dir: str,
        timeout_seconds=None,
        context=None,
    ):
        record = {
            "agent_id": agent_id,
            "task": task,
            "root": root,
            "working_dir": working_dir,
            "context": dict(context or {}),
        }
        self.runs.append(record)
        return {"ok": True, "status": "success", "output": f"{agent_id}: {task}", "record": record}


def test_agent_use_acp_registers_handshake_verified_actions(tmp_path):
    agent = Agently.create_agent().use_task_workspace(tmp_path / "task_workspace")
    provider = FakeACPProvider(
        [
            {"agent_id": "codex", "name": "Codex", "status": "ready", "endpoint": "local"},
            {"agent_id": "broken", "name": "Broken", "status": "failed"},
        ]
    )

    agent.use_acp(provider=provider)

    action_ids = {item.get("action_id") for item in agent.action.get_action_list(tags=[f"agent-{ agent.name }"])}
    assert {"acp_list_agents", "acp_run_task"}.issubset(action_ids)

    list_result = agent.action.execute_action("acp_list_agents", {})
    assert list_result.get("status") == "success"
    assert [item["agent_id"] for item in list_result.get("agents", [])] == ["codex"]
    assert [item["agent_id"] for item in list_result.get("data", {}).get("agents", [])] == ["codex"]

    run_result = agent.action.execute_action(
        "acp_run_task",
        {"agent_id": "codex", "task": "inspect the current branch", "working_subdir": "."},
    )

    assert run_result.get("status") == "success"
    assert run_result.get("agent_id") == "codex"
    assert run_result.get("data", {}).get("output") == "codex: inspect the current branch"
    assert provider.runs[0]["root"] == str(agent.task_workspace.root)
    assert provider.runs[0]["working_dir"] == str(agent.task_workspace.root)
    assert not (agent.task_workspace.root / ".agently").exists()


def test_agent_use_acp_skips_missing_or_failed_agents(tmp_path):
    agent = Agently.create_agent()
    provider = FakeACPProvider([{"agent_id": "broken", "status": "failed"}])

    agent.use_acp(root=tmp_path, provider=provider)

    action_ids = {item.get("action_id") for item in agent.action.get_action_list(tags=[f"agent-{ agent.name }"])}
    assert "acp_list_agents" in action_ids
    assert "acp_run_task" not in action_ids
    list_result = agent.action.execute_action("acp_list_agents", {})
    assert list_result.get("status") == "skipped"
    assert list_result.get("agents") == []
    diagnostics = list_result.get("diagnostics", [])
    assert isinstance(diagnostics, list)
    assert diagnostics[0].get("code") == "fake.discovery"
    assert diagnostics[-1].get("code") == "acp.adapter_hints"

    adapter_hints = cast(list[dict[str, Any]], list_result.get("adapter_hints", []))
    adapter_aliases = {alias for item in adapter_hints for alias in item.get("aliases", [])}
    assert {
        "codex",
        "claude code",
        "cc",
        "openclaw",
        "hermes",
        "hermes agent",
        "gemini",
    }.issubset(adapter_aliases)
    assert list_result.get("data", {}).get("adapter_hints") == adapter_hints

    action_info = agent.action.get_action_info(tags=[f"agent-{ agent.name }"])["acp_list_agents"]
    assert "claude code/cc" in action_info["desc"]
    assert action_info.get("meta", {}).get("adapter_hints") == adapter_hints


def test_agent_use_acp_error_on_missing_agents(tmp_path):
    agent = Agently.create_agent()
    provider = FakeACPProvider([])

    with pytest.raises(RuntimeError, match="No handshake-verified Agent Client Protocol"):
        agent.use_acp(root=tmp_path, provider=provider, on_missing="error")


def test_agent_use_acp_registers_local_cli_adapter(tmp_path):
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'fake codex 1.0'; exit 0; fi\n"
        "printf 'FAKE_ACP_RUN:'\n"
        "printf '%s|' \"$@\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    provider = LocalACPProvider(command_paths={"codex": [str(fake_codex)]})
    agent = Agently.create_agent()

    agent.use_acp(root=tmp_path, agent_ids=["codex"], provider=provider)

    action_ids = {item.get("action_id") for item in agent.action.get_action_list(tags=[f"agent-{ agent.name }"])}
    assert {"acp_list_agents", "acp_run_task"}.issubset(action_ids)
    list_result = agent.action.execute_action("acp_list_agents", {})
    agents = cast(list[dict[str, Any]], list_result.get("agents", []))
    assert agents[0]["agent_id"] == "codex"
    assert agents[0]["transport"] == "cli_adapter"
    assert agents[0]["meta"]["health"]["output"] == "fake codex 1.0"

    run_result = agent.action.execute_action(
        "acp_run_task",
        {"agent_id": "codex", "task": "inspect files", "working_subdir": "."},
    )

    assert run_result.get("status") == "success"
    assert run_result.get("transport") == "cli_adapter"
    assert run_result.get("acp_session", {}).get("persistence") == "stateless_cli"
    assert "inspect files" in str(run_result.get("output", ""))


@pytest.mark.asyncio
async def test_acp_package_reuses_execution_session_scope(tmp_path):
    provider = FakeACPProvider([{"agent_id": "codex", "status": "ready"}])
    acp = ACP(root=tmp_path, provider=provider)
    execution_context = AgentExecutionContext(
        execution_id="exec-acp-session",
        lineage={},
        limits={},
    )

    with bind_runtime_context(agent_execution_context=execution_context):
        first = await acp.async_run_task("codex", "inspect one", ".")
        second = await acp.async_run_task("codex", "inspect two", ".")

    first_session = cast(dict[str, Any], first.get("acp_session", {}))
    second_session = cast(dict[str, Any], second.get("acp_session", {}))
    assert first_session["scope"] == "execution"
    assert first_session["execution_id"] == "exec-acp-session"
    assert first_session["session_id"] == second_session["session_id"]
    assert provider.runs[0]["context"]["_agently_acp_session"]["session_id"] == first_session["session_id"]
    assert provider.runs[1]["context"]["_agently_acp_session"]["session_id"] == first_session["session_id"]


@pytest.mark.asyncio
async def test_acp_run_task_rejects_unknown_agent_and_outside_root(tmp_path):
    provider = FakeACPProvider([{"agent_id": "codex", "status": "ready"}])
    acp = ACP(root=tmp_path, provider=provider)

    unknown = await acp.async_run_task("unknown", "do work")
    outside = await acp.async_run_task("codex", "do work", working_subdir="../outside")

    assert unknown["status"] == "error"
    assert unknown["diagnostics"][0]["code"] == "acp.agent_unknown"
    assert outside["status"] == "error"
    assert outside["diagnostics"][0]["code"] == "acp.root_boundary"


def test_agent_use_actions_accepts_search_package():
    agent = Agently.create_agent()
    search = Search(timeout=1)

    async def fake_search(query: str, timelimit=None, max_results: int | None = 10):
        return [{"title": query, "href": "https://example.com", "body": f"limit={max_results}"}]

    search.search = fake_search
    agent.use_actions(search)

    action_ids = {item.get("action_id") for item in agent.action.get_action_list(tags=[f"agent-{ agent.name }"])}
    assert {"search", "search_news", "search_wikipedia", "search_arxiv"}.issubset(action_ids)

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})
    assert result.get("status") == "success"
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "limit=2"}]


def test_search_batch_registration_rolls_back_and_restores_host_action_on_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="search",
        desc="Host-owned search Action.",
        kwargs={"query": (str, "query")},
        func=lambda query: {"host": query},
    )
    original_register = agent.action.register_action
    registration_count = 0

    def fail_second_registration(**kwargs: Any):
        nonlocal registration_count
        registration_count += 1
        if registration_count == 2:
            raise RuntimeError("search batch registration failed")
        return original_register(**kwargs)

    monkeypatch.setattr(agent.action, "register_action", fail_second_registration)

    with pytest.raises(RuntimeError, match="search batch registration failed"):
        agent.use_actions(Search(timeout=1))

    restored = agent.action.action_registry.get_spec("search")
    assert restored is not None
    assert restored.get("desc") == "Host-owned search Action."
    assert not agent.action.action_registry.has("search_news")


def test_agent_language_policy_updates_builtin_search_default_region():
    agent = Agently.create_agent()
    agent.language("中文")
    search = Search(timeout=1)

    agent.use_actions(search, always=True)

    assert search.region == "cn-zh"


def test_agent_language_policy_does_not_override_explicit_builtin_search_region():
    agent = Agently.create_agent()
    agent.language("中文")
    search = Search(timeout=1, region="us-en")

    agent.use_actions(search, always=True)

    assert search.region == "us-en"


def test_agent_use_actions_accepts_browse_package():
    agent = Agently.create_agent()
    browse = Browse(enable_playwright=False, enable_bs4=False)

    async def fake_browse(url: str):
        return f"content from {url}"

    browse.browse = fake_browse
    agent.use_actions(browse)

    spec = agent.action.action_registry.get_spec("browse")
    assert spec is not None
    assert spec.get("executor_type") == "browse"
    assert spec.get("meta", {}).get("component") == "builtins.actions.Browse"

    result = agent.action.execute_action("browse", {"url": "https://example.com"})
    assert result.get("status") == "success"
    assert result.get("data") == "content from https://example.com"


def test_agent_language_policy_updates_builtin_browse_accept_language():
    agent = Agently.create_agent()
    agent.language("zh-CN")
    browse = Browse(enable_playwright=False, enable_bs4=False)

    agent.use_actions(browse, always=True)

    assert browse.headers["Accept-Language"].startswith("zh-CN")


def test_browse_default_fallback_prefers_reader_then_local_backends():
    browse = Browse()

    assert browse.fallback_order == ("jina_reader", "playwright", "bs4", "curl")
    assert browse.enable_curl is True
    assert browse.enable_jina_reader is True
    assert browse.jina_reader_fallback_endpoints == ("https://r.jinaai.cn/",)
    assert browse.jina_reader_timeout == 10
    assert browse.playwright_include_links is True


def test_browse_jina_reader_url_uses_raw_prepend_format():
    browse = Browse()

    reader_url = browse._jina_reader_url("https://example.com/path?q=1#section")

    assert reader_url == "https://r.jina.ai/https://example.com/path?q=1#section"


def test_browse_jina_reader_url_encodes_only_unsafe_target_characters():
    browse = Browse()

    reader_url = browse._jina_reader_url("https://example.com/路径?q=hello world")

    assert reader_url == "https://r.jina.ai/https://example.com/%E8%B7%AF%E5%BE%84?q=hello%20world"


def test_browse_jina_reader_url_uses_configured_endpoint():
    browse = Browse(jina_reader_endpoint="https://r.jinaai.cn/")

    reader_url = browse._jina_reader_url("https://example.com/path?q=1")

    assert reader_url == "https://r.jinaai.cn/https://example.com/path?q=1"


@pytest.mark.asyncio
async def test_browse_jina_reader_uses_post_for_fragment_urls(monkeypatch):
    browse_module = importlib.import_module("agently.builtins.actions.Browse")
    calls: list[tuple[str, Any]] = []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/markdown"}
        text = (
            "URL Source: https://example.com/#/route\n\n"
            "# Fragment route\n\n"
            "Reader returned a concrete body from a hash-routed page."
        )

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            calls.append(("post", (url, kwargs)))
            return FakeResponse()

        async def get(self, url, **kwargs):
            calls.append(("get", (url, kwargs)))
            raise AssertionError("fragment URLs should use Jina Reader POST form")

    class FakeHttpx:
        AsyncClient = FakeAsyncClient

    def fake_import_package(package_name: str, **kwargs):
        calls.append(("import", (package_name, kwargs)))
        assert package_name == "httpx"
        assert kwargs == {"auto_install": False}
        return FakeHttpx

    monkeypatch.setattr(browse_module.LazyImport, "import_package", fake_import_package)

    browse = Browse(enable_jina_reader=True, proxy="http://127.0.0.1:7890", timeout=30, jina_reader_timeout=9)
    result = await browse._jina_reader_browse("https://example.com/#/route")

    assert isinstance(result, dict)
    assert result["content_kind"] == "reader_text"
    assert result["reader_url"] == "https://r.jina.ai/https://example.com/#/route"
    assert ("init", {"proxy": "http://127.0.0.1:7890", "timeout": 9}) in calls
    assert any(
        kind == "post"
        and payload[0] == "https://r.jina.ai/"
        and payload[1]["data"] == {"url": "https://example.com/#/route"}
        for kind, payload in calls
    )
    assert not any(kind == "get" for kind, _ in calls)


@pytest.mark.asyncio
async def test_browse_jina_reader_tries_alternate_endpoint_on_transport_error(monkeypatch):
    browse_module = importlib.import_module("agently.builtins.actions.Browse")
    calls: list[tuple[str, Any]] = []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/markdown"}
        text = "URL Source: https://example.com/\n\n# Example\n\nReader alternate endpoint recovered content."

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            calls.append(("get", (url, kwargs)))
            if str(url).startswith("https://r.jina.ai/"):
                raise RuntimeError("tls eof")
            return FakeResponse()

    class FakeHttpx:
        AsyncClient = FakeAsyncClient

    monkeypatch.setattr(
        browse_module.LazyImport,
        "import_package",
        lambda package_name, **kwargs: FakeHttpx if package_name == "httpx" else None,
    )

    browse = Browse(enable_jina_reader=True, timeout=30, jina_reader_timeout=7)
    result = await browse._jina_reader_browse("https://example.com/")

    assert isinstance(result, dict)
    assert result["reader_endpoint"] == "https://r.jinaai.cn/"
    assert result["reader_url"] == "https://r.jinaai.cn/https://example.com/"
    assert ("init", {"proxy": None, "timeout": 7}) in calls
    assert [payload[0] for kind, payload in calls if kind == "get"] == [
        "https://r.jina.ai/https://example.com/",
        "https://r.jinaai.cn/https://example.com/",
    ]


@pytest.mark.asyncio
async def test_browse_falls_back_from_jina_block_page_to_local_backend():
    browse = Browse(
        fallback_order=("jina_reader", "bs4"),
        enable_playwright=False,
        enable_curl=False,
        enable_jina_reader=True,
        enable_bs4=True,
        min_content_length=20,
        max_attempts=1,
    )

    async def fake_jina(url: str):
        return {
            "ok": True,
            "content_kind": "reader_text",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/markdown",
            "content": "You've been blocked by network security. Log in to your Reddit account.",
            "links": [],
            "canonical_links": [],
        }

    async def fake_bs4(url: str):
        return {
            "ok": True,
            "content_kind": "html",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/html",
            "content": "Local backend recovered a readable article body with enough content.",
            "links": [],
            "canonical_links": [],
        }

    browse._jina_reader_browse = fake_jina  # type: ignore[method-assign]
    browse._bs4_browse = fake_bs4  # type: ignore[method-assign]

    result = await browse._execute_action_method("browse", url="https://example.com")

    assert result["status"] == "partial_success"
    assert result["meta"]["backend"] == "bs4"
    assert result["diagnostics"][0]["backend"] == "jina_reader"
    assert "blocked by network security" in result["diagnostics"][0]["message"]


def test_browse_exception_detail_includes_cause():
    error = RuntimeError("connect failed")
    error.__cause__ = OSError("tls eof")

    detail = Browse._exception_detail(error)

    assert detail == "RuntimeError: connect failed: caused by OSError: tls eof"


def test_browse_blocked_page_reason_detects_weibo_visitor_shell():
    reason = Browse._blocked_page_reason(
        '--- title: "Sina Visitor System" url: "https://passport.weibo.com/visitor/visitor?entry=miniblog" ---'
    )

    assert reason == "blocked_or_error_page: sina visitor system"


def test_browse_text_extraction_tolerates_missing_node_attrs():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<html><body><main><p>Readable official page content.</p></main></body></html>", "html.parser")
    assert soup.main is not None
    soup.main.attrs = None  # type: ignore[assignment]

    content = Browse._extract_text_from_soup(soup, min_length=10)

    assert "Readable official page content" in content


@pytest.mark.asyncio
async def test_browse_curl_backend_extracts_html_content():
    browse = Browse(
        fallback_order=("curl",),
        enable_playwright=False,
        enable_bs4=False,
        min_content_length=10,
        max_attempts=1,
    )

    async def fake_curl(url: str):
        return {
            "ok": True,
            "content_kind": "html",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/html",
            "content": "Curl backend official page with enough readable content.",
            "links": [{"url": "https://example.com/next", "text": "Next"}],
            "canonical_links": ["https://example.com/"],
        }

    browse._curl_browse = fake_curl  # type: ignore[method-assign]

    result = await browse._execute_action_method("browse", url="https://example.com")

    assert result["status"] == "success"
    assert result["meta"]["backend"] == "curl"
    assert result["data"]["content"].startswith("Curl backend official page")
    assert result["data"]["links"][0]["url"] == "https://example.com/next"


@pytest.mark.asyncio
async def test_browse_jina_reader_backend_extracts_markdown_content_and_links():
    browse = Browse(
        fallback_order=("jina_reader",),
        enable_playwright=False,
        enable_curl=False,
        enable_jina_reader=True,
        enable_bs4=False,
        min_content_length=10,
        max_attempts=1,
    )

    async def fake_jina(url: str):
        return {
            "ok": True,
            "content_kind": "reader_text",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/markdown",
            "content": "Reader backend official page with enough readable content.",
            "links": [{"url": "https://example.com/docs", "text": "Docs"}],
            "canonical_links": ["https://example.com/"],
        }

    browse._jina_reader_browse = fake_jina  # type: ignore[method-assign]

    result = await browse._execute_action_method("browse", url="https://example.com")

    assert result["status"] == "success"
    assert result["meta"]["backend"] == "jina_reader"
    assert result["data"]["content"].startswith("Reader backend official page")
    assert result["data"]["links"][0]["url"] == "https://example.com/docs"


@pytest.mark.asyncio
async def test_browse_falls_back_from_curl_to_jina_reader_with_diagnostics():
    browse = Browse(
        fallback_order=("curl", "jina_reader"),
        enable_playwright=False,
        enable_curl=True,
        enable_jina_reader=True,
        enable_bs4=False,
        min_content_length=20,
        max_attempts=1,
    )

    async def fake_curl(url: str):
        return {
            "ok": True,
            "content_kind": "html",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/html",
            "content": "short",
        }

    async def fake_jina(url: str):
        return {
            "ok": True,
            "content_kind": "reader_text",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/markdown",
            "content": "Reader recovered the article body with enough useful content.",
            "links": [],
            "canonical_links": [],
        }

    browse._curl_browse = fake_curl  # type: ignore[method-assign]
    browse._jina_reader_browse = fake_jina  # type: ignore[method-assign]

    result = await browse._execute_action_method("browse", url="https://example.com")

    assert result["status"] == "partial_success"
    assert result["success"] is True
    assert result["meta"]["backend"] == "jina_reader"
    assert result["diagnostics"][0]["backend"] == "curl"
    assert result["diagnostics"][0]["message"] == "HTTP status 200 produced no readable content"


@pytest.mark.asyncio
async def test_browse_curl_backend_materializes_remote_file_to_workspace(tmp_path):
    task_workspace = TaskWorkspace(tmp_path / "browse-curl-workspace", mode="read_write")
    browse = Browse(
        fallback_order=("curl",),
        enable_playwright=False,
        enable_bs4=False,
        max_attempts=1,
    )
    pdf_bytes = b"%PDF-1.4\ncurl pdf bytes\n%%EOF"

    async def fake_curl(url: str):
        return {
            "ok": True,
            "content_kind": "remote_file",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "application/pdf",
            "headers": {"content-type": "application/pdf"},
            "content_bytes": pdf_bytes,
        }

    browse._curl_browse = fake_curl  # type: ignore[method-assign]

    result = await browse._execute_action_method(
        "browse",
        url="https://example.com/syllabus.pdf",
        task_workspace=task_workspace,
    )

    assert result["status"] in {"success", "partial_success"}
    assert result["data"]["kind"] == "remote_file"
    assert "downloads/syllabus-" in result["file_refs"][0]["path"]
    assert (task_workspace.root / result["file_refs"][0]["path"]).is_file()


def test_browse_action_failure_is_structured_error_not_success_text():
    agent = Agently.create_agent()
    browse = Browse(enable_pyautogui=False, enable_playwright=False, enable_curl=False, enable_bs4=False)
    agent.use_actions(browse)

    result = agent.action.execute_action("browse", {"url": "https://example.com/nope"})

    assert result.get("status") == "error"
    assert result.get("success") is False
    assert result.get("ok") is False
    assert result.get("data") is None
    assert "Can not browse" in str(result.get("error", ""))
    diagnostics = result.get("diagnostics")
    assert isinstance(diagnostics, list)
    assert diagnostics
    assert diagnostics[0].get("code") == "browse_backend_failed"
    assert result.get("meta", {}).get("provider") == "builtins.actions.Browse"


def test_browse_raw_body_fallback_strips_data_uri_hot_content():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(
        '<html><body><img src="data:image/png;base64,AAAA'
        + ("B" * 2000)
        + '"><div data-payload="'
        + ("C" * 800)
        + '">tiny</div></body></html>',
        "html.parser",
    )

    content = Browse._extract_text_from_soup(soup, min_length=50)

    assert "data:image" not in content
    assert "BBBB" not in content
    assert "CCCC" not in content


def test_browse_action_retries_transient_backend_error():
    agent = Agently.create_agent()
    browse = Browse(
        fallback_order=("bs4",),
        enable_playwright=False,
        enable_bs4=True,
        min_content_length=10,
        max_attempts=2,
        retry_backoff_seconds=0,
    )
    calls: list[str] = []

    async def fake_bs4(url: str):
        calls.append(url)
        if len(calls) == 1:
            return "Can not browse 'https://example.com'.\tError: incomplete chunked read"
        return "Recovered page content with enough text."

    browse._bs4_browse = fake_bs4  # type: ignore[method-assign]
    agent.use_actions(browse)

    result = agent.action.execute_action("browse", {"url": "https://example.com"})

    assert len(calls) == 2
    assert result.get("status") == "partial_success"
    assert result.get("success") is True
    assert result.get("data") == "Recovered page content with enough text."
    diagnostic = result.get("diagnostics", [])[0]
    assert diagnostic.get("code") == "browse_backend_failed"
    assert diagnostic.get("retryable") is True


@pytest.mark.asyncio
async def test_action_runtime_default_planning_uses_configured_model_key(monkeypatch):
    import agently.core as agently_core

    agent = Agently.create_agent()
    agent.settings.set("action.planning_model_key", "task-main")
    seen_model_keys: list[str | None] = []

    class FakeResult:
        async def async_get_data(self):
            return {"next_action": "response", "execution_commands": []}

    class FakeResponse:
        result = FakeResult()

        async def get_async_generator(self, *_, **__):
            if False:
                yield None

    class FakeModelRequest:
        def __init__(self, *_, model_key=None, **__):
            seen_model_keys.append(model_key)

        def input(self, *_args, **_kwargs):
            return self

        def info(self, *_args, **_kwargs):
            return self

        def instruct(self, *_args, **_kwargs):
            return self

        def output(self, *_args, **_kwargs):
            return self

        def get_response(self, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(agently_core, "ModelRequest", FakeModelRequest)
    prompt = Agently.create_prompt()
    prompt.set("input", "collect evidence")

    decision = await agent.action.action_runtime._default_structured_planning_handler(
        {
            "prompt": prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "round_index": 0,
            "max_rounds": 1,
            "done_plans": [],
            "last_round_records": [],
        },
        {"action_list": [{"name": "search"}]},
    )

    assert seen_model_keys == ["task-main"]
    assert decision["next_action"] == "response"


@pytest.mark.asyncio
async def test_action_runtime_default_planning_discloses_single_action_id_round_policy(monkeypatch):
    import agently.core as agently_core

    agent = Agently.create_agent()
    agent.settings.set("action.loop.round_dispatch_policy", "single_action_id_cohort")
    captured_info: list[dict[str, Any]] = []
    captured_instructions: list[list[str]] = []

    class FakeResult:
        async def async_get_data(self):
            return {"next_action": "response", "execution_commands": []}

    class FakeResponse:
        result = FakeResult()

    class FakeModelRequest:
        def __init__(self, *_, **__):
            pass

        def input(self, *_args, **_kwargs):
            return self

        def info(self, value):
            captured_info.append(value)
            return self

        def instruct(self, value):
            captured_instructions.append(value)
            return self

        def output(self, *_args, **_kwargs):
            return self

        def get_response(self, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(agently_core, "ModelRequest", FakeModelRequest)
    prompt = Agently.create_prompt()
    prompt.set("input", "search, then read a selected match")

    await agent.action.action_runtime._default_structured_planning_handler(
        {
            "prompt": prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "round_index": 0,
            "max_rounds": None,
            "done_plans": [],
            "last_round_records": [],
        },
        {"action_list": [{"name": "search"}, {"name": "read"}]},
    )

    assert captured_info[0]["round_dispatch_policy"] == "single_action_id_cohort"
    assert any(
        "same action_id" in instruction and "next round" in instruction
        for instruction in captured_instructions[0]
    )


@pytest.mark.asyncio
async def test_browse_recovers_http_to_https_and_returns_link_diagnostics():
    browse = Browse(
        fallback_order=("bs4",),
        enable_playwright=False,
        enable_bs4=True,
        min_content_length=10,
        max_attempts=1,
        retry_backoff_seconds=0,
    )
    calls: list[str] = []

    async def fake_bs4(url: str):
        calls.append(url)
        if url.startswith("http://"):
            return "Can not browse 'http://example.com'.\tError: connection refused"
        return {
            "ok": True,
            "content_kind": "html",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/html",
            "content": "Recovered official page with enough readable content.",
            "links": [{"url": "https://example.com/101/1010/10261.html", "text": "Official syllabus"}],
            "canonical_links": ["https://example.com/"],
        }

    browse._bs4_browse = fake_bs4  # type: ignore[method-assign]

    result = await browse._execute_action_method("browse", url="http://example.com")

    assert calls[:2] == ["http://example.com", "https://example.com"]
    assert result["status"] == "partial_success"
    assert result["success"] is True
    assert result["data"]["selected_url"] == "https://example.com"
    assert result["data"]["links"][0]["text"] == "Official syllabus"
    assert result["meta"]["retry_candidates"][1]["reason"] == "same_host_https"
    assert result["diagnostics"][0]["retryable"] is True


@pytest.mark.asyncio
async def test_browse_rejects_waf_shell_and_continues_protocol_candidates():
    browse = Browse(
        fallback_order=("bs4",),
        enable_playwright=False,
        enable_bs4=True,
        min_content_length=10,
        max_attempts=1,
        retry_backoff_seconds=0,
    )
    calls: list[str] = []

    async def fake_bs4(url: str):
        calls.append(url)
        if url.startswith("http://"):
            return {
                "ok": True,
                "content_kind": "html",
                "requested_url": url,
                "url": url,
                "status": 200,
                "media_type": "text/html",
                "content": (
                    '<body><div id="errorCodeTitle"></div><div id="errorCodeInfo"></div>'
                    '<a href="https://yundun.console.aliyun.com/?p=waf#/waf/cn/dashboard/index" '
                    'id="waf"></a></body>'
                ),
                "links": [],
                "canonical_links": [],
            }
        return {
            "ok": True,
            "content_kind": "html",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "text/html",
            "content": "Recovered official page with enough readable content.",
            "links": [{"url": "https://example.com/official.html", "text": "Official page"}],
            "canonical_links": ["https://example.com/"],
        }

    browse._bs4_browse = fake_bs4  # type: ignore[method-assign]

    result = await browse._execute_action_method("browse", url="http://example.com")

    assert calls[:2] == ["http://example.com", "https://example.com"]
    assert result["success"] is True
    assert result["data"]["selected_url"] == "https://example.com"
    assert result["data"]["content"] == "Recovered official page with enough readable content."
    assert result["diagnostics"][0]["message"].startswith("blocked_or_error_page")


@pytest.mark.asyncio
async def test_browse_materializes_remote_file_to_workspace(tmp_path):
    task_workspace = TaskWorkspace(tmp_path / "browse-workspace", mode="read_write")
    browse = Browse(
        fallback_order=("bs4",),
        enable_playwright=False,
        enable_bs4=True,
        min_content_length=10,
        max_attempts=1,
    )
    pdf_bytes = b"%PDF-1.4\nfake pdf bytes\n%%EOF"

    async def fake_bs4(url: str):
        return {
            "ok": True,
            "content_kind": "remote_file",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "application/pdf",
            "headers": {"content-type": "application/pdf"},
            "content_bytes": pdf_bytes,
        }

    browse._bs4_browse = fake_bs4  # type: ignore[method-assign]

    result = await browse._execute_action_method(
        "browse",
        url="https://example.com/syllabus.pdf",
        task_workspace=task_workspace,
    )

    assert result["success"] is True
    assert result["status"] in {"success", "partial_success"}
    assert result["data"]["kind"] == "remote_file"
    assert result["data"]["media_type"] == "application/pdf"
    assert "downloads/syllabus-" in result["file_refs"][0]["path"]
    assert (task_workspace.root / result["file_refs"][0]["path"]).is_file()
    assert result["data"]["read_preview"]["path"] == result["file_refs"][0]["path"]
    assert "content_bytes" not in str(result)


def test_browse_action_executor_uses_settings_task_workspace_for_remote_file(tmp_path):
    task_workspace = TaskWorkspace(tmp_path / "browse-action-workspace", mode="read_write")
    agent = Agently.create_agent()
    cast(Any, agent.settings).set("action.task_workspace", task_workspace)
    browse = Browse(
        fallback_order=("bs4",),
        enable_playwright=False,
        enable_bs4=True,
        min_content_length=10,
        max_attempts=1,
    )

    async def fake_bs4(url: str):
        return {
            "ok": True,
            "content_kind": "remote_file",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "application/pdf",
            "headers": {"content-type": "application/pdf"},
            "content_bytes": b"%PDF-1.4\nexecutor fake\n%%EOF",
        }

    browse._bs4_browse = fake_bs4  # type: ignore[method-assign]
    agent.use_actions(browse)

    result = agent.action.execute_action("browse", {"url": "https://example.com/syllabus.pdf"})

    assert result.get("success") is True
    file_refs = result.get("file_refs", [])
    assert isinstance(file_refs, list)
    assert "downloads/syllabus-" in file_refs[0]["path"]
    assert (task_workspace.root / file_refs[0]["path"]).is_file()


@pytest.mark.asyncio
async def test_browse_remote_file_without_task_workspace_fails_closed():
    browse = Browse(
        fallback_order=("bs4",),
        enable_playwright=False,
        enable_bs4=True,
        min_content_length=10,
        max_attempts=1,
    )

    async def fake_bs4(url: str):
        return {
            "ok": True,
            "content_kind": "remote_file",
            "requested_url": url,
            "url": url,
            "status": 200,
            "media_type": "application/pdf",
            "headers": {"content-type": "application/pdf"},
            "content_bytes": b"%PDF-1.4\nfake\n%%EOF",
        }

    browse._bs4_browse = fake_bs4  # type: ignore[method-assign]

    result = await browse._execute_action_method("browse", url="https://example.com/syllabus.pdf")

    assert result["success"] is False
    assert result["status"] == "blocked"
    assert result["diagnostics"][0]["code"] == "browse.remote_file.task_workspace_required"
    assert "content_bytes" not in str(result)


@pytest.mark.asyncio
async def test_action_runtime_native_tool_planning_uses_configured_model_key(monkeypatch):
    import agently.core as agently_core

    agent = Agently.create_agent()
    agent.settings.set("action.planning_model_key", "task-main")
    seen_model_keys: list[str | None] = []

    class FakePrompt:
        def set(self, *_args, **_kwargs):
            return None

    class FakeResponse:
        def get_async_generator(self, *_, **__):
            async def generate():
                yield "done", None

            return generate()

    class FakeModelRequest:
        prompt = FakePrompt()

        def __init__(self, *_, model_key=None, **__):
            seen_model_keys.append(model_key)

        def input(self, *_args, **_kwargs):
            return self

        def info(self, *_args, **_kwargs):
            return self

        def instruct(self, *_args, **_kwargs):
            return self

        def get_response(self, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(agently_core, "ModelRequest", FakeModelRequest)
    prompt = Agently.create_prompt()
    prompt.set("input", "collect evidence")

    decision = await agent.action.action_runtime._default_native_tool_call_planning_handler(
        {
            "prompt": prompt,
            "settings": agent.settings,
            "agent_name": agent.name,
            "round_index": 0,
            "max_rounds": 1,
            "done_plans": [],
            "last_round_records": [],
        },
        {"action_list": [{"name": "search"}]},
    )

    assert seen_model_keys == ["task-main"]
    assert decision["next_action"] == "response"


def test_agent_enable_sqlite_registers_managed_sqlite_action(tmp_path):
    db_path = tmp_path / "items.db"
    connection = sqlite3.connect(db_path)
    connection.execute("create table items (id integer primary key, name text)")
    connection.execute("insert into items (name) values (?)", ("Agently",))
    connection.commit()
    connection.close()

    agent = Agently.create_agent()
    agent.enable_sqlite(database=str(db_path), action_id="test_query_sqlite")

    spec = agent.action.action_registry.get_spec("test_query_sqlite")
    assert spec is not None
    environments = spec.get("execution_resources", [])
    assert environments and environments[0].get("kind") == "sqlite"

    result = agent.action.execute_action(
        "test_query_sqlite",
        {"query": "select name from items where id = ?", "params": [1]},
    )
    assert result.get("status") == "success"
    data = result.get("data")
    assert isinstance(data, dict)
    assert data.get("rows") == [{"name": "Agently"}]


def test_instruction_heavy_direct_action_releases_historical_artifact_refs(tmp_path):
    agent = Agently.create_agent()
    agent.enable_shell(root=tmp_path, commands=["pwd"], action_id="test_recall_bash", sandbox="trusted_local")

    result = agent.action.execute_action(
        "test_recall_bash",
        {"cmd": "pwd", "workdir": str(tmp_path), "api_token": "secret-value"},
        purpose="Inspect cwd",
    )

    assert result.get("status") == "success"
    assert str(tmp_path) in str(result.get("data", {}).get("stdout", ""))

    digest = result.get("model_digest")
    assert isinstance(digest, dict)
    assert digest.get("action_id") == "test_recall_bash"
    assert digest.get("instruction", {}).get("kind") == "cmd"
    assert "artifact_refs" in digest

    artifact_refs = result.get("artifact_refs")
    assert isinstance(artifact_refs, list)
    assert len(artifact_refs) >= 2
    assert "api_token" in result.get("redaction_report", [])
    input_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_input")
    output_ref = next(ref for ref in artifact_refs if ref.get("artifact_type") == "action_output")
    input_artifact_id = input_ref.get("artifact_id")
    input_action_call_id = input_ref.get("action_call_id")
    output_artifact_id = output_ref.get("artifact_id")
    output_action_call_id = output_ref.get("action_call_id")
    assert isinstance(input_artifact_id, str)
    assert isinstance(input_action_call_id, str)
    assert isinstance(output_artifact_id, str)
    assert isinstance(output_action_call_id, str)
    assert all(ref.get("available") is False for ref in artifact_refs)
    assert all(ref.get("full_value_available") is False for ref in artifact_refs)

    recalled_input = agent.action.read_action_artifact(
        selection_key=str(input_ref.get("selection_key", "")),
    )
    assert recalled_input.get("ok") is False
    assert recalled_input.get("status") == "not_found"

    recalled = agent.action.read_action_artifact(
        selection_key=str(output_ref.get("selection_key", "")),
    )
    assert recalled.get("ok") is False
    assert recalled.get("status") == "not_found"

    prompt_results = agent.action.to_action_results([result])
    visible = prompt_results["Inspect cwd"]
    assert isinstance(visible, dict)
    assert visible.get("action_call_id") == result.get("action_call_id")
    visible_refs = visible.get("artifact_refs")
    assert isinstance(visible_refs, list)
    assert [ref.get("selection_key") for ref in visible_refs] == [
        ref.get("selection_key") for ref in artifact_refs
    ]
    assert all(
        set(ref).isdisjoint({"artifact_id", "action_call_id", "sha256", "size", "bytes", "meta"})
        for ref in visible_refs
    )


@pytest.mark.asyncio
async def test_action_loop_exposes_live_recall_then_returns_historical_refs(tmp_path):
    agent = Agently.create_agent()
    tag = f"recall-loop-{agent.name}"
    action_id = "test_loop_recall_bash"
    agent.action.register_bash_sandbox_action(
        action_id=action_id,
        tags=[tag],
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(tmp_path)],
        expose_to_model=True,
    )
    spec = agent.action.action_registry.get_spec(action_id)
    assert spec is not None
    spec_desc = str(spec.get("desc", ""))
    assert "Allowed command prefixes: pwd." in spec_desc
    assert f"Allowed working directory roots: {tmp_path}" in spec_desc
    assert "Timeout: 20 seconds." in spec_desc
    prompt = Agently.create_prompt()
    prompt.set("input", "inspect cwd")
    seen_rounds: list[dict[str, Any]] = []

    async def planning_handler(context, request):
        seen_rounds.append(
            {
                "round_index": context.get("round_index"),
                "last_round_records": context.get("last_round_records", []),
                "action_ids": [item.get("action_id") for item in request.get("action_list", [])],
            }
        )
        if context.get("round_index") == 0:
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "Inspect cwd",
                        "action_id": action_id,
                        "action_input": {"cmd": "pwd", "workdir": str(tmp_path)},
                        "todo_suggestion": "respond",
                    }
                ],
            }
        return {
            "next_action": "response",
            "action_calls": [],
        }

    records = await agent.action.async_plan_and_execute(
        prompt=prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(tags=[tag]),
        agent_name=agent.name,
        planning_handler=planning_handler,
        max_rounds=3,
    )

    assert len(records) == 1
    assert str(tmp_path) in str(
        records[0].get("data", {}).get("result_preview", {}).get("stdout", "")
    )
    assert len(seen_rounds) >= 2
    second_round = seen_rounds[1]
    assert "read_action_artifact" in second_round["action_ids"]
    visible_record = second_round["last_round_records"][0]
    live_digest = visible_record.get("data")
    returned_digest = records[0].get("model_digest")
    assert isinstance(live_digest, dict)
    assert isinstance(returned_digest, dict)
    assert {
        key: value for key, value in live_digest.items() if key != "artifact_refs"
    } == {
        key: value for key, value in returned_digest.items() if key != "artifact_refs"
    }
    assert visible_record.get("result") == live_digest
    assert all(ref.get("available") is True for ref in live_digest.get("artifact_refs", []))
    assert all(ref.get("available") is False for ref in returned_digest.get("artifact_refs", []))


@pytest.mark.asyncio
async def test_action_loop_preserves_blocked_executor_reason_for_model_repair(tmp_path):
    agent = Agently.create_agent()
    tag = f"blocked-reason-{agent.name}"
    action_id = "test_blocked_reason_bash"
    agent.action.register_bash_sandbox_action(
        action_id=action_id,
        tags=[tag],
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(tmp_path)],
        expose_to_model=True,
    )
    prompt = Agently.create_prompt()
    prompt.set("input", "try one blocked command")
    seen_records: list[list[dict[str, Any]]] = []

    async def planning_handler(context, request):
        _ = request
        seen_records.append(context.get("last_round_records", []))
        if context.get("round_index") == 0:
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "Run a disallowed command",
                        "action_id": action_id,
                        "action_input": {
                            "cmd": ["python -c 'print(1)'"],
                            "workdir": str(tmp_path),
                        },
                    }
                ],
            }
        return {"next_action": "response", "action_calls": []}

    await agent.action.async_plan_and_execute(
        prompt=prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(tags=[tag]),
        agent_name=agent.name,
        planning_handler=planning_handler,
        max_rounds=2,
    )

    blocked = seen_records[1][0]
    digest = blocked.get("data", {})
    assert blocked.get("status") == "blocked"
    assert blocked.get("error") == "cmd_not_allowed"
    assert digest.get("error") == "cmd_not_allowed"
    assert digest.get("result_preview", {}).get("reason") == "cmd_not_allowed"
    assert digest.get("diagnostics", [{}])[0].get("code") == "shell.cmd_not_allowed"


@pytest.mark.asyncio
async def test_action_loop_keeps_large_outputs_cold_then_releases_standalone_scope():
    agent = Agently.create_agent()
    marker = "RAW_OUTPUT_SHOULD_STAY_COLD"

    @agent.action_func
    def produce_large_action_output() -> dict[str, Any]:
        return {
            "summary": "small visible prefix",
            "body": "x" * 12000 + marker,
        }

    prompt = Agently.create_prompt()
    prompt.set("input", "collect and then decide")
    seen_rounds: list[dict[str, Any]] = []

    async def planning_handler(context, request):
        seen_rounds.append(
            {
                "round_index": context.get("round_index"),
                "done_plans": context.get("done_plans", []),
                "last_round_records": context.get("last_round_records", []),
                "action_ids": [item.get("action_id") for item in request.get("action_list", [])],
            }
        )
        if context.get("round_index") == 0:
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "Collect large output",
                        "action_id": "produce_large_action_output",
                        "action_input": {},
                        "todo_suggestion": "inspect bounded digest",
                    }
                ],
            }
        return {
            "next_action": "response",
            "action_calls": [],
        }

    records = await agent.action.async_plan_and_execute(
        prompt=prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(tags=[f"agent-{agent.name}"]),
        agent_name=agent.name,
        planning_handler=planning_handler,
    )

    assert marker not in json.dumps(records, ensure_ascii=False)
    output_ref = next(
        ref
        for ref in records[0].get("artifact_refs", [])
        if ref.get("artifact_type") == "action_output"
    )
    assert output_ref.get("selection_key")
    assert set(output_ref).isdisjoint(
        {"artifact_id", "action_call_id", "sha256", "size", "bytes", "meta"}
    )
    assert output_ref.get("available") is False
    assert output_ref.get("full_value_available") is False
    assert agent.action._artifact_manager._artifacts == {}
    assert len(seen_rounds) >= 2
    second_round = seen_rounds[1]
    hot_context = json.dumps(
        {
            "done_plans": second_round["done_plans"],
            "last_round_records": second_round["last_round_records"],
        },
        ensure_ascii=False,
    )
    assert marker not in hot_context
    assert len(hot_context) < 10000
    assert "read_action_artifact" in second_round["action_ids"]
    visible_record = second_round["last_round_records"][0]
    assert visible_record["data"]["same_as"] == "result"
    visible_digest = visible_record["result"]
    assert visible_digest["result_preview_meta"]["hot_path_compacted"] is True
    assert visible_digest["artifact_refs"]
    assert "preview" not in visible_digest["artifact_refs"][0]
    assert visible_record["artifacts"] == visible_record["artifact_refs"]


@pytest.mark.asyncio
async def test_action_loop_explicit_recall_returns_bounded_hot_page_without_recursive_ref():
    agent = Agently.create_agent()
    expected_path = "ultralytics/nn/modules/moe/routers.py"

    @agent.action_func
    def produce_large_repository_listing() -> dict[str, Any]:
        return {
            "a_path": expected_path,
            "body": "x" * 12000,
        }

    prompt = Agently.create_prompt()
    prompt.set("input", "discover and read the exact repository path")
    seen_rounds: list[dict[str, Any]] = []

    async def planning_handler(context, _request):
        round_index = int(context.get("round_index", 0))
        last_round_records = context.get("last_round_records", [])
        seen_rounds.append(
            {
                "round_index": round_index,
                "last_round_records": copy.deepcopy(last_round_records),
            }
        )
        if round_index == 0:
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "List repository paths",
                        "action_id": "produce_large_repository_listing",
                        "action_input": {},
                    }
                ],
            }
        if round_index == 1:
            output_ref = next(
                ref
                for ref in last_round_records[0].get("artifact_refs", [])
                if ref.get("artifact_type") == "action_output"
            )
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "Read the bounded repository listing page",
                        "action_id": "read_action_artifact",
                        "action_input": {
                            "selection_key": output_ref["selection_key"],
                            "offset": 0,
                            "max_bytes": 4096,
                        },
                    }
                ],
            }
        return {"next_action": "response", "action_calls": []}

    records = await agent.action.async_plan_and_execute(
        prompt=prompt,
        settings=agent.settings,
        action_list=agent.action.get_action_list(tags=[f"agent-{agent.name}"]),
        agent_name=agent.name,
        planning_handler=planning_handler,
    )

    assert len(seen_rounds) == 3
    recalled_record = seen_rounds[2]["last_round_records"][0]
    recall_digest = recalled_record["result"]
    assert recall_digest["transfer_kind"] == "explicit_action_artifact_readback"
    assert expected_path in recall_digest["result_preview"]["value"]
    assert recalled_record["artifact_refs"] == []
    assert recalled_record["artifacts"] == []
    assert sum(
        1
        for record in records
        for ref in record.get("artifact_refs", [])
        if ref.get("artifact_type") == "action_output"
    ) == 1
    assert agent.action._artifact_manager._artifacts == {}


def test_search_package_does_not_load_backend_during_registration(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def fake_import_package(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Search backend should be loaded lazily.")

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", fake_import_package)
    agent = Agently.create_agent()
    agent.use_actions(Search(timeout=1))

    assert calls == []


def test_search_package_falls_back_when_ddgs_backend_has_no_results(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            backend = kwargs.get("backend")
            if backend == "yahoo":
                raise RuntimeError("No results found.")
            return [{"title": query, "href": "https://example.com", "body": f"backend={backend}"}]

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(backend="yahoo", search_fallback_backends=["brave"]))

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})

    assert result.get("status") == "partial_success"
    assert result.get("success") is True
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "backend=brave"}]


def test_search_package_reports_partial_success_when_fallback_recovers(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            backend = kwargs.get("backend")
            if backend == "yahoo":
                raise RuntimeError("HTTP 429")
            return [{"title": query, "href": "https://example.com", "body": f"backend={backend}"}]

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(backend="yahoo", search_fallback_backends=["google"]))

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})

    assert result.get("status") == "partial_success"
    assert result.get("success") is True
    assert result.get("ok") is True
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "backend=google"}]
    assert result.get("meta", {}).get("backend") == "google"
    assert result.get("meta", {}).get("failed_backends") == ["yahoo"]
    assert result.get("diagnostics", [])[0].get("code") == "search_backend_failed"


def test_search_package_retries_transient_backend_error(monkeypatch):
    calls: list[str] = []

    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            calls.append(str(kwargs.get("backend")))
            if len(calls) == 1:
                raise RuntimeError("incomplete chunked read")
            return [{"title": query, "href": "https://example.com", "body": "retried"}]

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(
        Search(backend="yahoo", max_attempts=2, retry_backoff_seconds=0)
    )

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})

    assert calls == ["yahoo", "yahoo"]
    assert result.get("status") == "partial_success"
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "retried"}]
    diagnostic = result.get("diagnostics", [])[0]
    assert diagnostic.get("code") == "search_backend_failed"
    assert diagnostic.get("retryable") is True


def test_search_package_continues_after_empty_backend(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            backend = kwargs.get("backend")
            if backend == "yahoo":
                return []
            return [{"title": query, "href": "https://example.com", "body": f"backend={backend}"}]

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(backend="yahoo", search_fallback_backends=["google"]))

    result = agent.action.execute_action("search", {"query": "Agently", "max_results": 2})

    assert result.get("status") == "partial_success"
    assert result.get("data") == [{"title": "Agently", "href": "https://example.com", "body": "backend=google"}]
    assert result.get("meta", {}).get("empty_backends") == ["yahoo"]


def test_search_package_treats_no_results_as_empty_success(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def news(self, query: str, **kwargs):
            raise RuntimeError("No results found.")

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(news_backend="yahoo", news_fallback_backends=["duckduckgo"]))

    result = agent.action.execute_action("search_news", {"query": "Agently Moxin", "max_results": 2})

    assert result.get("status") == "success"
    assert result.get("data") == []


def test_search_package_treats_mixed_backend_failure_and_empty_as_partial_success(monkeypatch):
    class FakeDDGS:
        def __init__(self, *args, **kwargs):
            pass

        def text(self, query: str, **kwargs):
            backend = kwargs.get("backend")
            if backend == "yahoo":
                raise RuntimeError("tls handshake eof")
            raise RuntimeError("No results found.")

    class FakeDDGSModule:
        DDGS = FakeDDGS

    search_module = importlib.import_module("agently.builtins.actions.Search")
    monkeypatch.setattr(search_module.LazyImport, "import_package", lambda *args, **kwargs: FakeDDGSModule)

    agent = Agently.create_agent().use_actions(Search(backend="yahoo", search_fallback_backends=["google"]))

    result = agent.action.execute_action("search", {"query": "Agently Moxin", "max_results": 2})

    assert result.get("status") == "partial_success"
    assert result.get("success") is True
    assert result.get("ok") is True
    assert result.get("data") == []
    assert result.get("meta", {}).get("failed_backends") == ["yahoo"]
    assert result.get("meta", {}).get("empty_backends") == ["google"]
    diagnostic_codes = [item.get("code") for item in result.get("diagnostics", [])]
    assert diagnostic_codes == ["search_backend_failed", "search_backend_failed", "search_backend_empty"]


def test_search_action_executor_awaits_async_fallback_methods():
    agent = Agently.create_agent()
    search = Search(timeout=1)

    async def fake_search_arxiv(query: str, max_results: int | None = 10):
        return {"query": query, "max_results": max_results}

    search.search_arxiv = fake_search_arxiv
    agent.use_actions(search)

    result = agent.action.execute_action("search_arxiv", {"query": "agents", "max_results": 3})

    assert result.get("status") == "success"
    assert result.get("data") == {"query": "agents", "max_results": 3}


def test_search_package_disables_lazy_import_install_prompt(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    search_module = importlib.import_module("agently.builtins.actions.Search")

    def missing_package(package_name: str, **kwargs):
        calls.append((package_name, kwargs))
        raise ImportError("missing dependency")

    monkeypatch.setattr(search_module.LazyImport, "import_package", missing_package)

    with pytest.raises(ImportError):
        Search(timeout=1)._get_ddgs()

    assert calls == [("ddgs", {"version_constraint": ">=9.10.0", "auto_install": False})]


@pytest.mark.asyncio
async def test_browse_bs4_disables_lazy_import_install_prompt(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    browse_module = importlib.import_module("agently.builtins.actions.Browse")

    def missing_bs4(package_name: str, **kwargs):
        calls.append((package_name, kwargs))
        if package_name == "httpx":
            return object()
        raise ImportError("missing dependency")

    monkeypatch.setattr(browse_module.LazyImport, "import_package", missing_bs4)

    with pytest.raises(ImportError):
        await Browse(fallback_order=("bs4",), enable_playwright=False, enable_curl=False)._bs4_browse("https://example.com")

    assert calls == [
        ("httpx", {"auto_install": False}),
        ("bs4", {"install_name": "beautifulsoup4", "auto_install": False}),
    ]
