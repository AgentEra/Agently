"""Observed gVisor provider mechanism test; local Docker/runsc is optional."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from agently.builtins.plugins.ExecutionResourceProvider.GVisorDockerExecutionResourceProvider import (
    GVisorDockerExecutionResourceProvider,
)


@pytest.mark.asyncio
async def test_gvisor_provider_executes_with_registered_runsc() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")

    image = "alpine:3.20"
    image_fact = subprocess.run(
        [docker, "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if image_fact.returncode != 0:
        pytest.skip(f"required local gVisor probe image is unavailable: {image}")

    provider = GVisorDockerExecutionResourceProvider()
    requirement = {
        "kind": "docker",
        "config": {
            "docker_binary": docker,
            "runtime_profile": {"image": image, "image_pull_policy": "never"},
        },
    }
    probe = await provider.async_probe(requirement=requirement, policy={})
    if not probe["available"]:
        pytest.skip(f"gVisor provider prerequisites unavailable: {probe['reason']}")

    handle = await provider.async_ensure(requirement=requirement, policy={})

    assert handle["provider_id"] == "gvisor"
    assert handle["meta"]["active_runtime"] == "runsc"
    assert handle["meta"]["runtime_verification"]["verified"] is True
