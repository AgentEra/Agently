"""Acceptance tests for the grant-bound Seatbelt execution provider."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.SeatbeltExecutionResourceProvider import (
    SeatbeltCodeExecutionResource,
    SeatbeltExecutionResourceProvider,
)
from agently.core import ExecutionResourceError
from agently.core.operation.Action.ActionResourceRegistrar import ActionResourceRegistrar
from agently.types.data import TaskWorkspaceAccessGrant, TaskWorkspaceAccessRoot


seatbelt_module = importlib.import_module(
    "agently.builtins.plugins.ExecutionResourceProvider.SeatbeltExecutionResourceProvider"
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
        grant_id="seatbelt-grant",
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


def test_seatbelt_profile_allows_writes_only_for_granted_read_write_roots(
    tmp_path: Path,
) -> None:
    grant = _grant(tmp_path)
    profile = SeatbeltCodeExecutionResource(grant=grant)._build_profile()

    assert f'(deny file-write* (subpath "{_root(grant, "source")}"))' in profile
    for role in ("build", "output", "logs"):
        assert f'(allow file-write* (subpath "{_root(grant, role)}"))' in profile
    assert f'(deny file-read* (subpath "{_root(grant, "source")}"))' not in profile
    assert '(allow file-write* (subpath "/private/tmp"))' not in profile


def test_seatbelt_resource_accepts_no_arbitrary_policy_paths(tmp_path: Path) -> None:
    grant = _grant(tmp_path)

    with pytest.raises(TypeError):
        SeatbeltCodeExecutionResource(grant=grant, writable_paths=["/"])  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_seatbelt_probe_reports_broad_host_reads_truthfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seatbelt_module,
        "inspect_seatbelt_availability",
        lambda: {"available": True, "reason": "ready", "binary": "/usr/bin/sandbox-exec"},
    )
    monkeypatch.setattr(
        SeatbeltExecutionResourceProvider,
        "_tool_facts",
        lambda _self: {"python": {"tool": "python", "available": True, "binary": "/usr/bin/python3", "version": "3.10", "raw_version": "Python 3.10"}},
    )

    probe = await SeatbeltExecutionResourceProvider().async_probe(
        requirement={"kind": "code_execution"},
        policy={},
    )

    isolation = probe["capabilities"]["isolation"]
    assert isolation["host_filesystem_restricted"] is False
    assert isolation["workspace_write_restricted"] is True
    assert probe["capabilities"]["safety_class"] == "host_policy"


@pytest.mark.asyncio
async def test_seatbelt_health_rechecks_mechanism_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = SeatbeltCodeExecutionResource(grant=_grant(tmp_path))
    monkeypatch.setattr(
        seatbelt_module,
        "inspect_seatbelt_availability",
        lambda: {"available": False, "reason": "sandbox_exec_failed"},
    )

    status = await SeatbeltExecutionResourceProvider().async_health_check(
        {"resource": resource, "meta": {"mechanism_verified": True}}
    )

    assert status == "unhealthy"


@pytest.mark.asyncio
async def test_seatbelt_rejects_arbitrary_policy_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seatbelt_module,
        "inspect_seatbelt_availability",
        lambda: {"available": True, "reason": "ready", "binary": "/usr/bin/sandbox-exec"},
    )

    with pytest.raises(ExecutionResourceError) as raised:
        await SeatbeltExecutionResourceProvider().async_ensure(
            requirement={
                "kind": "code_execution",
                "task_workspace_access_grant": _grant(tmp_path),
                "config": {"extra_sbpl_rules": "(allow default)"},
            },
            policy={},
        )

    assert raised.value.code == "execution_resource.seatbelt_config_invalid"


def test_seatbelt_sandbox_uses_only_the_seatbelt_provider() -> None:
    action = _Action()
    ActionResourceRegistrar(action).register_python_sandbox_action(sandbox="seatbelt")

    requirement = action.registered["execution_resources"][0]
    assert [item["provider_id"] for item in requirement["provider_candidates"]] == ["seatbelt"]
    assert requirement["meta"]["isolation_preference"] == "preferred"
