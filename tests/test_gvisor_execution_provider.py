"""Contract tests for the dedicated, fail-closed gVisor Docker provider."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import DockerExecutionResource
from agently.builtins.plugins.ExecutionResourceProvider.GVisorDockerExecutionResourceProvider import (
    GVisorDockerExecutionResource,
    GVisorDockerExecutionResourceProvider,
)
from agently.core import ExecutionResourceError
from agently.core.operation.Action.ActionResourceRegistrar import ActionResourceRegistrar


def _ready_docker() -> dict[str, Any]:
    return {"available": True, "reason": "ready", "docker_binary": "docker", "server_version": "29.0.0"}


def _gvisor_requirement() -> dict[str, Any]:
    return {"kind": "docker", "config": {"runtime_profile": {"image": "python:3.12-slim"}}}


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


def test_gvisor_requires_runsc_in_docker_runtime_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerExecutionResource, "inspect_availability", lambda _self: _ready_docker())
    resource = GVisorDockerExecutionResource()
    monkeypatch.setattr(resource, "_docker_runtime_registry", lambda: {"runc": {}})

    availability = resource.inspect_availability()

    assert availability["available"] is False
    assert availability["reason"] == "runsc_runtime_unregistered"


def test_gvisor_rejects_malformed_runtime_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DockerExecutionResource, "inspect_availability", lambda _self: _ready_docker())
    resource = GVisorDockerExecutionResource()
    monkeypatch.setattr(resource, "_docker_runtime_registry", lambda: None)

    assert resource.inspect_availability()["reason"] == "runsc_runtime_registry_invalid"


def test_gvisor_rejects_conflicting_default_runtime_args() -> None:
    resource = GVisorDockerExecutionResource(default_args=["--runtime", "runc"])

    with pytest.raises(ValueError, match="--runtime"):
        resource._container_base_args(profile={})


def test_gvisor_resource_emits_one_fixed_runtime_argv() -> None:
    args = GVisorDockerExecutionResource()._container_base_args(profile={})

    assert args.count("--runtime") == 1
    assert args[args.index("--runtime") + 1] == "runsc"


@pytest.mark.asyncio
async def test_gvisor_direct_docker_run_emits_one_fixed_runtime_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    async def to_thread(_function: Any, *args: Any, **_kwargs: Any) -> _Completed:
        calls.append(args)
        return _Completed()

    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )
    monkeypatch.setattr(docker_module.asyncio, "to_thread", to_thread)

    result = await GVisorDockerExecutionResource().run(
        image="alpine:3.20",
        cmd=["true"],
    )

    assert result["ok"] is True
    args = list(calls[0][0])
    assert args.count("--runtime") == 1
    assert args[args.index("--runtime") + 1] == "runsc"


@pytest.mark.asyncio
async def test_gvisor_ensure_records_active_runtime_only_after_execution_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_available", lambda _self: _ready_docker())
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_image_ready", lambda _self, _image, *, profile: {"ready": True})

    async def verify(_self: Any, *, image: str, profile: dict[str, Any]) -> dict[str, Any]:
        assert image == profile["image"] == "python:3.12-slim"
        return {"verified": True, "result": {"ok": True}}

    monkeypatch.setattr(GVisorDockerExecutionResource, "async_verify_runtime", verify)
    handle = await GVisorDockerExecutionResourceProvider().async_ensure(requirement=_gvisor_requirement(), policy={})

    assert handle["provider_id"] == "gvisor"
    assert handle["meta"]["active_runtime"] == "runsc"
    assert handle["meta"]["runtime_verification"]["verified"] is True


@pytest.mark.asyncio
async def test_gvisor_ensure_rejects_registered_but_non_executable_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_available", lambda _self: _ready_docker())
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_image_ready", lambda _self, _image, *, profile: {"ready": True})

    async def fail(_self: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"verified": False, "result": {"ok": False, "stderr": "runtime failed"}}

    monkeypatch.setattr(GVisorDockerExecutionResource, "async_verify_runtime", fail)
    with pytest.raises(ExecutionResourceError) as raised:
        await GVisorDockerExecutionResourceProvider().async_ensure(requirement=_gvisor_requirement(), policy={})

    assert raised.value.code == "execution_resource.gvisor_runtime_unavailable"
    assert raised.value.payload["reason"] == "runsc_runtime_execution_failed"


@pytest.mark.asyncio
async def test_gvisor_ensure_closes_unowned_resource_after_runtime_verification_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TrackingProvider(GVisorDockerExecutionResourceProvider):
        created: GVisorDockerExecutionResource | None = None

        def create_resource(self, **kwargs: Any) -> GVisorDockerExecutionResource:
            self.created = super().create_resource(**kwargs)
            return self.created

    provider = _TrackingProvider()
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_available", lambda _self: _ready_docker())
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_image_ready", lambda _self, _image, *, profile: {"ready": True})

    async def fail(_self: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"verified": False, "result": {"ok": False}}

    monkeypatch.setattr(GVisorDockerExecutionResource, "async_verify_runtime", fail)

    with pytest.raises(ExecutionResourceError):
        await provider.async_ensure(requirement=_gvisor_requirement(), policy={})

    assert provider.created is not None
    assert provider.created._closed is True


@pytest.mark.asyncio
async def test_gvisor_ensure_closes_unowned_resource_when_runtime_verification_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TrackingProvider(GVisorDockerExecutionResourceProvider):
        created: GVisorDockerExecutionResource | None = None

        def create_resource(self, **kwargs: Any) -> GVisorDockerExecutionResource:
            self.created = super().create_resource(**kwargs)
            return self.created

    provider = _TrackingProvider()
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_available", lambda _self: _ready_docker())
    monkeypatch.setattr(GVisorDockerExecutionResource, "ensure_image_ready", lambda _self, _image, *, profile: {"ready": True})

    async def cancel(_self: Any, **_kwargs: Any) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr(GVisorDockerExecutionResource, "async_verify_runtime", cancel)

    with pytest.raises(asyncio.CancelledError):
        await provider.async_ensure(requirement=_gvisor_requirement(), policy={})

    assert provider.created is not None
    assert provider.created._closed is True


def test_gvisor_sandbox_uses_only_the_gvisor_provider() -> None:
    action = _Action()
    ActionResourceRegistrar(action).register_python_sandbox_action(sandbox="gvisor")

    requirement = action.registered["execution_resources"][0]
    assert requirement["kind"] == "code_execution"
    assert [item["provider_id"] for item in requirement["provider_candidates"]] == ["gvisor"]


def test_default_docker_resource_has_no_runtime_argument() -> None:
    assert "--runtime" not in DockerExecutionResource()._container_base_args(profile={})


@pytest.mark.asyncio
async def test_gvisor_health_rejects_an_unregistered_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = GVisorDockerExecutionResource()
    monkeypatch.setattr(DockerExecutionResource, "inspect_availability", lambda _self: _ready_docker())
    monkeypatch.setattr(resource, "_docker_runtime_registry", lambda: {"runc": {}})

    assert await GVisorDockerExecutionResourceProvider().async_health_check({"resource": resource}) == "unhealthy"


@pytest.mark.asyncio
async def test_gvisor_health_rejects_registered_but_non_executable_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = GVisorDockerExecutionResource(runtime_profile={"image": "python:3.12-slim"})
    monkeypatch.setattr(resource, "inspect_availability", lambda: _ready_docker())

    async def fail(**_kwargs: Any) -> dict[str, Any]:
        return {"verified": False, "result": {"ok": False}}

    monkeypatch.setattr(resource, "async_verify_runtime", fail)

    status = await GVisorDockerExecutionResourceProvider().async_health_check(
        {
            "resource": resource,
            "meta": {
                "active_runtime": "runsc",
                "runtime_verification": {"verified": True},
            },
        }
    )

    assert status == "unhealthy"


def test_gvisor_probe_rejects_conflicting_runtime_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(DockerExecutionResource, "inspect_availability", lambda _self: _ready_docker())
    resource = GVisorDockerExecutionResource(default_args=["--runtime=runc"])
    monkeypatch.setattr(resource, "_docker_runtime_registry", lambda: {"runsc": {}})

    availability = resource.inspect_availability()

    assert availability["available"] is False
    assert availability["reason"] == "runsc_runtime_arguments_invalid"


@pytest.mark.asyncio
async def test_gvisor_cancelled_runtime_probe_leaves_no_active_container(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = GVisorDockerExecutionResource()

    async def cancelled_run(**_kwargs: Any) -> dict[str, Any]:
        raise asyncio.CancelledError

    monkeypatch.setattr(resource, "_run_container", cancelled_run)
    with pytest.raises(asyncio.CancelledError):
        await resource.async_verify_runtime(image="python:3.12-slim", profile={})

    assert resource._active_containers == set()
