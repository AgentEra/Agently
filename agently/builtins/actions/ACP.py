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

from __future__ import annotations

from agently_stage import default_stage_call_bridge

import asyncio
import inspect
import shutil
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from agently.core.runtime.RuntimeContext import get_current_agent_execution_context
from agently.types.data import ExecutionResourceRequirement
from agently.utils import LazyImport


async def _await_value(value: Awaitable[Any]) -> Any:
    return await value


def _resolve_sync(value: Any) -> Any:
    if inspect.isawaitable(value):
        return default_stage_call_bridge.as_sync(_await_value)(cast(Awaitable[Any], value))
    return value


COMMON_ACP_ADAPTER_HINTS: tuple[dict[str, Any], ...] = (
    {
        "name": "codex",
        "label": "Codex",
        "aliases": ("codex",),
    },
    {
        "name": "claude code",
        "label": "Claude Code",
        "aliases": ("claude code", "cc", "claude"),
    },
    {
        "name": "openclaw",
        "label": "OpenClaw",
        "aliases": ("openclaw",),
    },
    {
        "name": "hermes",
        "label": "Hermes Agent",
        "aliases": ("hermes", "hermes agent"),
    },
    {
        "name": "gemini",
        "label": "Gemini",
        "aliases": ("gemini",),
    },
)

COMMON_ACP_ADAPTER_HINT_MESSAGE = (
    "Common ACP adapter names/aliases include codex, claude code/cc, "
    "openclaw, hermes/hermes agent, and gemini. These are hints only; "
    "acp_run_task is registered only after local discovery verifies a runnable agent."
)


def common_acp_adapter_hints() -> list[dict[str, Any]]:
    return [
        {
            "name": str(item["name"]),
            "label": str(item["label"]),
            "aliases": [str(alias) for alias in item["aliases"]],
        }
        for item in COMMON_ACP_ADAPTER_HINTS
    ]


@runtime_checkable
class ACPProvider(Protocol):
    def discover_agents(
        self,
        *,
        root: str,
        agent_ids: list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any] | Iterable[Mapping[str, Any]]: ...

    async def async_run_task(
        self,
        *,
        agent_id: str,
        task: str,
        root: str,
        working_dir: str,
        timeout_seconds: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | str: ...


class LocalACPProvider:
    COMMON_AGENT_COMMANDS = ("codex", "claude", "gemini")
    DEFAULT_COMMAND_PATHS: dict[str, tuple[str, ...]] = {
        "codex": (
            "codex",
            "/Applications/Codex.app/Contents/Resources/codex",
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
        ),
        "claude": (
            "claude",
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
        ),
        "gemini": (
            "gemini",
            "/opt/homebrew/bin/gemini",
            "/usr/local/bin/gemini",
        ),
    }

    def __init__(self, command_paths: Mapping[str, Iterable[str]] | None = None):
        self.command_paths = {
            agent_id: tuple(str(item) for item in paths)
            for agent_id, paths in (command_paths or self.DEFAULT_COMMAND_PATHS).items()
        }
        self._agents_by_id: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _command_exists(command: str) -> str | None:
        if not command:
            return None
        resolved = shutil.which(command)
        if resolved:
            return resolved
        path = Path(command).expanduser()
        if path.exists() and path.is_file():
            return str(path)
        return None

    @staticmethod
    def _health_args(agent_id: str) -> tuple[str, ...]:
        return ("--version",)

    @staticmethod
    def _run_args(agent_id: str, *, task: str, root: str, working_dir: str) -> tuple[str, ...]:
        if agent_id == "codex":
            return (
                "exec",
                "-C",
                working_dir,
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "--skip-git-repo-check",
                task,
            )
        if agent_id == "claude":
            return (
                "-p",
                "--permission-mode",
                "dontAsk",
                "--add-dir",
                root,
                task,
            )
        return (task,)

    async def _async_command_health(
        self,
        *,
        agent_id: str,
        command: str,
        cwd: str,
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        args = self._health_args(agent_id)
        try:
            process = await asyncio.create_subprocess_exec(
                command,
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=min(float(timeout_seconds or 10), 10.0),
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return {
                    "ok": False,
                    "status": "failed",
                    "error": "ACP command health check timed out.",
                }
            output = stdout.decode("utf-8", "replace").strip()
            error = stderr.decode("utf-8", "replace").strip()
            return {
                "ok": process.returncode == 0,
                "status": "ready" if process.returncode == 0 else "failed",
                "exit_code": process.returncode,
                "output": output[:1200],
                "stderr": error[:1200],
            }
        except Exception as error:
            return {
                "ok": False,
                "status": "failed",
                "error": str(error) or error.__class__.__name__,
                "exception_type": error.__class__.__name__,
            }

    def _command_health(
        self,
        *,
        agent_id: str,
        command: str,
        cwd: str,
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        return default_stage_call_bridge.as_sync(self._async_command_health)(
            agent_id=agent_id,
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    def _discover_cli_agents(
        self,
        *,
        root: str,
        requested: list[str],
        timeout_seconds: float | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        agents: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        for agent_id in requested:
            paths = list(self.command_paths.get(agent_id, (agent_id,)))
            path_candidates = [
                {"command": path, "resolved": self._command_exists(path)}
                for path in paths
            ]
            selected_command = ""
            selected_health: dict[str, Any] | None = None
            failures: list[dict[str, Any]] = []
            for item in path_candidates:
                resolved = item.get("resolved")
                if not resolved:
                    continue
                health = self._command_health(
                    agent_id=agent_id,
                    command=str(resolved),
                    cwd=root,
                    timeout_seconds=timeout_seconds,
                )
                if health.get("ok"):
                    selected_command = str(resolved)
                    selected_health = health
                    break
                failures.append({"command": resolved, "health": health})
            if selected_command:
                agent = {
                    "agent_id": agent_id,
                    "name": {"codex": "Codex", "claude": "Claude Code", "gemini": "Gemini"}.get(agent_id, agent_id),
                    "status": "ready",
                    "endpoint": selected_command,
                    "command": selected_command,
                    "transport": "cli_adapter",
                    "handshake_kind": "command_health",
                    "meta": {
                        "health": selected_health or {},
                        "path_candidates": path_candidates,
                    },
                }
                agents.append(agent)
                continue
            diagnostics.append(
                {
                    "code": "acp.command_unavailable",
                    "agent_id": agent_id,
                    "message": "No runnable local coding-agent command passed ACP CLI adapter health checks.",
                    "path_candidates": path_candidates,
                    "health_failures": failures,
                }
            )
        return agents, diagnostics

    def discover_agents(
        self,
        *,
        root: str,
        agent_ids: list[str] | None = None,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        requested = [str(item).strip() for item in (agent_ids or self.COMMON_AGENT_COMMANDS) if str(item).strip()]
        command_candidates = [
            {
                "agent_id": item,
                "paths": [
                    {"command": path, "available": self._command_exists(path) is not None}
                    for path in self.command_paths.get(item, (item,))
                ],
            }
            for item in requested
        ]
        cli_agents, cli_diagnostics = self._discover_cli_agents(
            root=root,
            requested=requested,
            timeout_seconds=timeout_seconds,
        )
        try:
            acp_module = LazyImport.import_package("acp", auto_install=False)
        except ImportError as error:
            self._agents_by_id = {agent["agent_id"]: agent for agent in cli_agents}
            return {
                "agents": cli_agents,
                "diagnostics": [
                    {
                        "code": "acp.dependency_missing",
                        "message": str(error),
                        "requested_agent_ids": requested,
                        "command_candidates": command_candidates,
                    }
                ] + cli_diagnostics,
            }

        discover = getattr(acp_module, "discover_agents", None)
        if not callable(discover):
            self._agents_by_id = {agent["agent_id"]: agent for agent in cli_agents}
            return {
                "agents": cli_agents,
                "diagnostics": [
                    {
                        "code": "acp.discovery_api_missing",
                        "message": "Installed acp package does not expose discover_agents(...).",
                        "requested_agent_ids": requested,
                        "command_candidates": command_candidates,
                    }
                ] + cli_diagnostics,
            }
        result = _resolve_sync(discover(root=root, agent_ids=requested or None, timeout_seconds=timeout_seconds))
        if isinstance(result, Mapping):
            agents = list(result.get("agents", []) or [])
            diagnostics = list(result.get("diagnostics", []) or [])
            combined_agents = [
                *(agent for agent in agents if isinstance(agent, Mapping)),
                *cli_agents,
            ]
            self._agents_by_id = {
                str(agent.get("agent_id")): dict(agent)
                for agent in combined_agents
                if isinstance(agent, Mapping) and agent.get("agent_id")
            }
            return {
                **dict(result),
                "agents": combined_agents,
                "diagnostics": diagnostics + cli_diagnostics,
            }
        if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
            agents = [*(item for item in list(result) if isinstance(item, Mapping)), *cli_agents]
            self._agents_by_id = {
                str(agent.get("agent_id")): dict(agent)
                for agent in agents
                if isinstance(agent, Mapping) and agent.get("agent_id")
            }
            return {"agents": agents, "diagnostics": cli_diagnostics}
        self._agents_by_id = {agent["agent_id"]: agent for agent in cli_agents}
        return {"agents": cli_agents, "diagnostics": [{"code": "acp.discovery_invalid_result"}, *cli_diagnostics]}

    async def async_run_task(
        self,
        *,
        agent_id: str,
        task: str,
        root: str,
        working_dir: str,
        timeout_seconds: float | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        agent = self._agents_by_id.get(agent_id)
        if isinstance(agent, Mapping) and agent.get("transport") == "cli_adapter":
            command = str(agent.get("command") or "")
            if not command:
                return {
                    "ok": False,
                    "status": "error",
                    "error": "ACP CLI adapter has no command.",
                    "diagnostics": [{"code": "acp.cli_command_missing"}],
                }
            args = self._run_args(agent_id, task=task, root=root, working_dir=working_dir)
            try:
                process = await asyncio.create_subprocess_exec(
                    command,
                    *args,
                    cwd=working_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    return {
                        "ok": False,
                        "status": "timed_out",
                        "error": "ACP CLI task timed out.",
                        "agent_id": agent_id,
                        "diagnostics": [{"code": "acp.cli_timeout"}],
                    }
                output = stdout.decode("utf-8", "replace")
                error = stderr.decode("utf-8", "replace")
                return {
                    "ok": process.returncode == 0,
                    "status": "success" if process.returncode == 0 else "error",
                    "agent_id": agent_id,
                    "output": output,
                    "stderr": error,
                    "exit_code": process.returncode,
                    "command": command,
                    "transport": "cli_adapter",
                    "acp_session": (
                        dict(context.get("_agently_acp_session", {}))
                        if isinstance(context, Mapping)
                        and isinstance(context.get("_agently_acp_session", {}), Mapping)
                        else {}
                    ),
                    "diagnostics": [] if process.returncode == 0 else [{"code": "acp.cli_failed"}],
                }
            except Exception as error:
                return {
                    "ok": False,
                    "status": "error",
                    "error": str(error) or error.__class__.__name__,
                    "agent_id": agent_id,
                    "diagnostics": [{"code": "acp.cli_exception", "type": error.__class__.__name__}],
                }
        acp_module = LazyImport.import_package("acp", auto_install=False)
        run_task = getattr(acp_module, "run_task", None)
        if not callable(run_task):
            return {
                "ok": False,
                "status": "error",
                "error": "Installed acp package does not expose run_task(...).",
                "diagnostics": [{"code": "acp.run_api_missing"}],
            }
        result = run_task(
            agent_id=agent_id,
            task=task,
            root=root,
            working_dir=working_dir,
            timeout_seconds=timeout_seconds,
            context=dict(context or {}),
        )
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, Mapping) else {"output": str(result)}


class ACP:
    def __init__(
        self,
        *,
        root: str | Path | None = None,
        agent_ids: list[str] | tuple[str, ...] | str | None = None,
        provider: ACPProvider | Any | None = None,
        on_missing: Literal["skip", "error"] = "skip",
        timeout_seconds: float | None = 600,
        action_prefix: str = "",
        session_scope: Literal["action_call", "execution"] = "execution",
    ):
        self.root = Path(root or Path.cwd()).resolve()
        self.agent_ids = self._normalize_agent_ids(agent_ids)
        self.provider = provider if provider is not None else LocalACPProvider()
        self.on_missing = on_missing
        self.timeout_seconds = timeout_seconds
        self.action_prefix = action_prefix.strip()
        self.session_scope = session_scope if session_scope in {"action_call", "execution"} else "execution"
        self._agents: list[dict[str, Any]] | None = None
        self._diagnostics: list[dict[str, Any]] = []

    @staticmethod
    def _normalize_agent_ids(value: list[str] | tuple[str, ...] | str | None) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            values = [value]
        else:
            values = list(value)
        normalized = [str(item).strip() for item in values if str(item).strip()]
        return normalized or None

    @staticmethod
    def _normalize_agent(agent: Mapping[str, Any]) -> dict[str, Any]:
        agent_id = str(agent.get("agent_id") or agent.get("id") or agent.get("name") or "").strip()
        return {
            "agent_id": agent_id,
            "name": str(agent.get("name") or agent_id),
            "status": str(agent.get("status") or "ready"),
            "endpoint": str(agent.get("endpoint") or ""),
            "command": str(agent.get("command") or ""),
            "transport": str(agent.get("transport") or ""),
            "handshake_kind": str(agent.get("handshake_kind") or ""),
            "meta": dict(agent.get("meta", {})) if isinstance(agent.get("meta"), Mapping) else {},
        }

    def _discover(self) -> None:
        if self._agents is not None:
            return
        self._agents = []
        self._diagnostics = []
        discover = getattr(self.provider, "discover_agents", None)
        if not callable(discover):
            self._diagnostics.append(
                {
                    "code": "acp.provider_invalid",
                    "message": "ACP provider must expose discover_agents(...).",
                }
            )
        else:
            try:
                result = discover(
                    root=str(self.root),
                    agent_ids=self.agent_ids,
                    timeout_seconds=self.timeout_seconds,
                )
                result = _resolve_sync(result)
                if isinstance(result, Mapping):
                    agents = result.get("agents", [])
                    diagnostics = result.get("diagnostics", [])
                elif isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
                    agents = list(result or [])
                    diagnostics = []
                else:
                    agents = []
                    diagnostics = [{"code": "acp.discovery_invalid_result"}]
                if isinstance(diagnostics, list):
                    self._diagnostics.extend(
                        dict(item) if isinstance(item, Mapping) else {"message": str(item)}
                        for item in diagnostics
                    )
                for agent in agents if isinstance(agents, Iterable) else []:
                    if not isinstance(agent, Mapping):
                        continue
                    normalized = self._normalize_agent(agent)
                    if normalized["agent_id"] and normalized["status"] in {"ready", "success", "ok"}:
                        self._agents.append(normalized)
                    else:
                        self._diagnostics.append(
                            {
                                "code": "acp.handshake_failed",
                                "message": "ACP agent handshake did not produce a ready agent.",
                                "agent": dict(agent),
                            }
                        )
            except Exception as error:
                self._diagnostics.append(
                    {
                        "code": "acp.discovery_failed",
                        "message": str(error),
                        "exception_type": type(error).__name__,
                    }
                )

        if not self._agents and self.on_missing == "error":
            raise RuntimeError(
                "No handshake-verified Agent Client Protocol coding agent is available. "
                f"Diagnostics: { self._diagnostics }"
            )

    def _action_name(self, name: str) -> str:
        return f"{ self.action_prefix }{ name }" if self.action_prefix else name

    def _agents_by_id(self) -> dict[str, dict[str, Any]]:
        self._discover()
        return {str(agent.get("agent_id")): agent for agent in self._agents or []}

    def _resource_requirement(self) -> ExecutionResourceRequirement:
        return {
            "kind": "acp",
            "scope": "action_call",
            "resource_key": f"acp:{ self.root }",
            "config": {
                "root": str(self.root),
                "agents": list(self._agents or []),
            },
            "policy": {
                "task_workspace_roots": [str(self.root)],
                "timeout_seconds": float(self.timeout_seconds or 0),
            },
            "meta": {"component": "builtins.actions.ACP", "session_scope": self.session_scope},
        }

    def register_actions(
        self,
        action,
        *,
        tags: str | list[str] | None = None,
        action_prefix: str = "",
        expose_to_model: bool = True,
        default_policy: dict[str, Any] | None = None,
    ) -> list[str]:
        if action_prefix and not self.action_prefix:
            self.action_prefix = action_prefix.strip()
        self._discover()
        action_ids: list[str] = []
        list_action_id = self._action_name("acp_list_agents")
        action.register_action(
            action_id=list_action_id,
            desc=(
                "List handshake-verified local Agent Client Protocol coding agents available to this Agent. "
                + COMMON_ACP_ADAPTER_HINT_MESSAGE
            ),
            kwargs={},
            func=self.list_agents,
            tags=tags,
            default_policy=default_policy,
            side_effect_level="read",
            expose_to_model=expose_to_model,
            meta={
                "component": "builtins.actions.ACP",
                "kind": "acp",
                "root": str(self.root),
                "session_scope": self.session_scope,
                "diagnostics": list(self._diagnostics),
                "adapter_hints": common_acp_adapter_hints(),
            },
        )
        action_ids.append(list_action_id)
        if self._agents:
            run_action_id = self._action_name("acp_run_task")
            action.register_action(
                action_id=run_action_id,
                desc=(
                    "Run a bounded coding task through one handshake-verified local Agent Client Protocol "
                    "coding agent. The agent_id must come from acp_list_agents."
                ),
                kwargs={
                    "agent_id": (str, "Handshake-verified ACP agent id from acp_list_agents."),
                    "task": (str, "Bounded coding-agent task to run inside the configured root."),
                    "working_subdir": (str, "Optional relative subdirectory under the configured root."),
                    "context": (dict, "Optional structured context for the ACP agent."),
                },
                func=self.async_run_task,
                tags=tags,
                default_policy=default_policy,
                side_effect_level="exec",
                replay_safe=False,
                expose_to_model=expose_to_model,
                execution_resources=[self._resource_requirement()],
                meta={
                    "component": "builtins.actions.ACP",
                    "kind": "acp",
                    "root": str(self.root),
                    "session_scope": self.session_scope,
                    "agent_ids": [agent["agent_id"] for agent in self._agents],
                    "adapter_hints": common_acp_adapter_hints(),
                },
            )
            action_ids.append(run_action_id)
        return action_ids

    def list_agents(self) -> dict[str, Any]:
        self._discover()
        diagnostics = list(self._diagnostics)
        adapter_hints = common_acp_adapter_hints()
        if not self._agents:
            diagnostics.append(
                {
                    "code": "acp.adapter_hints",
                    "message": COMMON_ACP_ADAPTER_HINT_MESSAGE,
                    "adapter_hints": adapter_hints,
                }
            )
        data = {
            "agents": list(self._agents or []),
            "diagnostics": diagnostics,
            "root": str(self.root),
            "adapter_hints": adapter_hints,
        }
        return {
            "ok": bool(self._agents),
            "status": "success" if self._agents else "skipped",
            "data": data,
            "result": data,
            **data,
        }

    def _resolve_working_dir(self, working_subdir: str | None = None) -> Path:
        if working_subdir is None or not str(working_subdir).strip():
            return self.root
        candidate = (self.root / str(working_subdir)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("ACP working_subdir must stay inside the configured root.")
        return candidate

    @staticmethod
    def _selected_session_persistence(agent: Mapping[str, Any]) -> str:
        if str(agent.get("transport") or "") == "cli_adapter":
            return "stateless_cli"
        return "protocol_session"

    def _resolve_session_descriptor(
        self,
        *,
        selected: Mapping[str, Any],
        working_dir: Path,
    ) -> dict[str, Any]:
        agent_id = str(selected.get("agent_id") or "")
        persistence = self._selected_session_persistence(selected)
        if self.session_scope == "action_call":
            session_id = f"acp:{ uuid.uuid4().hex }"
            return {
                "scope": "action_call",
                "session_id": session_id,
                "agent_id": agent_id,
                "root": str(self.root),
                "working_dir": str(working_dir),
                "persistence": persistence,
            }

        execution_context = get_current_agent_execution_context()
        execution_id = str(getattr(execution_context, "execution_id", "") or "")
        if execution_context is None or not execution_id:
            session_id = f"acp:{ uuid.uuid4().hex }"
            return {
                "scope": "action_call",
                "requested_scope": "execution",
                "session_id": session_id,
                "agent_id": agent_id,
                "root": str(self.root),
                "working_dir": str(working_dir),
                "persistence": persistence,
                "diagnostics": [{"code": "acp.session.execution_context_missing"}],
            }

        action_scope = getattr(execution_context, "action_scope", None)
        if not isinstance(action_scope, dict):
            action_scope = {}
            try:
                setattr(execution_context, "action_scope", action_scope)
            except Exception:
                pass
        sessions = action_scope.setdefault("acp_sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
            action_scope["acp_sessions"] = sessions
        session_key = f"{ agent_id }|{ self.root }|{ execution_id }"
        session = sessions.get(session_key)
        if not isinstance(session, dict):
            session = {
                "scope": "execution",
                "session_id": f"acp:{ execution_id }:{ agent_id }:{ uuid.uuid4().hex[:8] }",
                "agent_id": agent_id,
                "root": str(self.root),
                "working_dir": str(working_dir),
                "execution_id": execution_id,
                "persistence": persistence,
            }
            sessions[session_key] = session
        else:
            session.setdefault("scope", "execution")
            session.setdefault("agent_id", agent_id)
            session.setdefault("root", str(self.root))
            session.setdefault("execution_id", execution_id)
            session["working_dir"] = str(working_dir)
            session["persistence"] = persistence
        return dict(session)

    async def async_run_task(
        self,
        agent_id: str,
        task: str,
        working_subdir: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        agents = self._agents_by_id()
        selected = agents.get(str(agent_id).strip())
        if selected is None:
            return {
                "ok": False,
                "status": "error",
                "error": f"Unknown or unavailable ACP agent_id: { agent_id }.",
                "agent_id": str(agent_id),
                "available_agent_ids": sorted(agents),
                "diagnostics": [{"code": "acp.agent_unknown"}],
            }
        if not isinstance(task, str) or not task.strip():
            return {
                "ok": False,
                "status": "error",
                "error": "ACP task must be a non-empty string.",
                "agent_id": selected["agent_id"],
                "diagnostics": [{"code": "acp.task_empty"}],
            }
        try:
            working_dir = self._resolve_working_dir(working_subdir)
        except ValueError as error:
            return {
                "ok": False,
                "status": "error",
                "error": str(error),
                "agent_id": selected["agent_id"],
                "diagnostics": [{"code": "acp.root_boundary"}],
            }
        run_task = getattr(self.provider, "async_run_task", None)
        if not callable(run_task):
            return {
                "ok": False,
                "status": "error",
                "error": "ACP provider must expose async_run_task(...).",
                "agent_id": selected["agent_id"],
                "diagnostics": [{"code": "acp.provider_run_missing"}],
            }
        run_task_func = cast(
            Callable[..., Awaitable[Mapping[str, Any] | str] | Mapping[str, Any] | str],
            run_task,
        )
        acp_session = self._resolve_session_descriptor(selected=selected, working_dir=working_dir)
        provider_context = dict(context or {})
        provider_context["_agently_acp_session"] = dict(acp_session)
        result = run_task_func(
            agent_id=selected["agent_id"],
            task=task,
            root=str(self.root),
            working_dir=str(working_dir),
            timeout_seconds=self.timeout_seconds,
            context=provider_context,
        )
        if inspect.isawaitable(result):
            result = await cast(Awaitable[Mapping[str, Any] | str], result)
        payload: dict[str, Any] = dict(result) if isinstance(result, Mapping) else {"output": str(result)}
        payload.setdefault("ok", payload.get("status", "success") in {"success", "partial_success"})
        payload.setdefault("status", "success" if payload.get("ok") else "error")
        payload.setdefault("agent_id", selected["agent_id"])
        payload.setdefault("root", str(self.root))
        payload.setdefault("working_dir", str(working_dir))
        payload.setdefault("diagnostics", [])
        payload.setdefault("acp_session", acp_session)
        payload_data = {
            "agent_id": payload.get("agent_id", selected["agent_id"]),
            "output": payload.get("output", payload.get("result", payload.get("data"))),
            "root": payload.get("root", str(self.root)),
            "working_dir": payload.get("working_dir", str(working_dir)),
            "diagnostics": payload.get("diagnostics", []),
            "acp_session": payload.get("acp_session", acp_session),
        }
        payload.setdefault("data", payload_data)
        payload.setdefault("result", payload.get("data"))
        return payload
