"""Acceptance tests for the grant-bound Bubblewrap provider."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.BubblewrapExecutionResourceProvider import (
    BubblewrapCodeExecutionResource,
    BubblewrapExecutionResourceProvider,
)
from agently.core import ExecutionResourceError
from agently.core.operation.Action.ActionResourceRegistrar import ActionResourceRegistrar
from agently.types.data import TaskWorkspaceAccessGrant, TaskWorkspaceAccessRoot


bubblewrap_module = importlib.import_module(
    "agently.builtins.plugins.ExecutionResourceProvider.BubblewrapExecutionResourceProvider"
)


def _grant(tmp_path: Path) -> TaskWorkspaceAccessGrant:
    area = tmp_path / "execution"
    roots = []
    for role, mode in (
        ("source", "read_only"),
        ("build", "read_write"),
        ("output", "read_write"),
        ("logs", "read_write"),
    ):
        path = area / role
        path.mkdir(parents=True, exist_ok=True)
        roots.append(TaskWorkspaceAccessRoot(role=role, host_path=str(path), access_mode=mode))
    return TaskWorkspaceAccessGrant(
        grant_id="bubblewrap-grant",
        task_workspace_id="workspace",
        execution_id="execution",
        action_call_id="run",
        mode="snapshot",
        execution_area=str(area),
        roots=tuple(roots),
        issued_at="2026-08-17T00:00:00Z",
    )


def _root(grant: TaskWorkspaceAccessGrant, role: str) -> str:
    return next(item.host_path for item in grant.roots if item.role == role)


def _contains_slice(values: list[str], expected: list[str]) -> bool:
    width = len(expected)
    return any(values[index:index + width] == expected for index in range(len(values) - width + 1))


class _Settings:
    def get(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Action:
    def __init__(self) -> None:
        self.settings = _Settings()
        self.registered: dict[str, Any] = {}

    def _normalize_tags(self, _tags: Any) -> list[str]:
        return []

    def _create_executor(self, *_args: Any, **_kwargs: Any) -> object:
        return object()

    def register_action(self, **kwargs: Any) -> None:
        self.registered = kwargs


def test_bubblewrap_argv_mounts_only_provider_roots_and_workspace_grants(tmp_path: Path) -> None:
    grant = _grant(tmp_path)
    resource = BubblewrapCodeExecutionResource(grant=grant)
    argv = resource._build_bwrap_argv(["python3", "--version"], area=Path(grant.execution_area))

    assert _contains_slice(argv, ["--ro-bind", _root(grant, "source"), _root(grant, "source")])
    for role in ("build", "output", "logs"):
        path = _root(grant, role)
        assert _contains_slice(argv, ["--bind", path, path])
    assert not _contains_slice(argv, ["--bind", "/", "/"])
    assert "--unshare-all" in argv
    assert "--share-net" not in argv


def test_bubblewrap_resource_accepts_no_arbitrary_mount_or_argv_configuration(tmp_path: Path) -> None:
    grant = _grant(tmp_path)

    with pytest.raises(TypeError):
        BubblewrapCodeExecutionResource(grant=grant, bind_rw=["/"])  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_bubblewrap_probe_reports_no_syscall_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bubblewrap_module,
        "inspect_bubblewrap_availability",
        lambda: {"available": True, "reason": "ready", "binary": "/usr/bin/bwrap"},
    )
    monkeypatch.setattr(
        BubblewrapExecutionResourceProvider,
        "_tool_facts",
        lambda _self: {"python": {"tool": "python", "available": True, "binary": "/usr/bin/python3", "version": "3.10", "raw_version": "Python 3.10"}},
    )

    probe = await BubblewrapExecutionResourceProvider().async_probe(
        requirement={"kind": "code_execution"},
        policy={},
    )

    isolation = probe["capabilities"]["isolation"]
    assert isolation["syscalls_restricted"] is False
    assert isolation["host_filesystem_restricted"] is True
    assert probe["capabilities"]["safety_class"] == "namespace"


@pytest.mark.asyncio
async def test_bubblewrap_health_rechecks_real_mechanism(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = BubblewrapCodeExecutionResource(grant=_grant(tmp_path))
    monkeypatch.setattr(
        bubblewrap_module,
        "inspect_bubblewrap_availability",
        lambda: {"available": False, "reason": "bwrap_user_namespace_blocked"},
    )

    status = await BubblewrapExecutionResourceProvider().async_health_check(
        {"resource": resource, "meta": {"mechanism_verified": True}}
    )

    assert status == "unhealthy"


@pytest.mark.asyncio
async def test_bubblewrap_rejects_arbitrary_provider_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bubblewrap_module,
        "inspect_bubblewrap_availability",
        lambda: {"available": True, "reason": "ready", "binary": "/usr/bin/bwrap"},
    )

    with pytest.raises(ExecutionResourceError) as raised:
        await BubblewrapExecutionResourceProvider().async_ensure(
            requirement={
                "kind": "code_execution",
                "task_workspace_access_grant": _grant(tmp_path),
                "config": {"extra_bwrap_args": ["--bind", "/", "/host"]},
            },
            policy={},
        )

    assert raised.value.code == "execution_resource.bubblewrap_config_invalid"


def test_bubblewrap_sandbox_uses_only_the_bubblewrap_provider() -> None:
    action = _Action()
    ActionResourceRegistrar(action).register_python_sandbox_action(sandbox="bubblewrap")

    requirement = action.registered["execution_resources"][0]
    assert [item["provider_id"] for item in requirement["provider_candidates"]] == ["bubblewrap"]
    assert requirement["meta"]["isolation_preference"] == "preferred"
