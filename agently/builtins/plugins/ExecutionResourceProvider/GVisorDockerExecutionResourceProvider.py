# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import uuid
from typing import TYPE_CHECKING, Any, Sequence

from agently.core import ExecutionResourceError
from agently.types.data import TaskWorkspaceAccessGrant

from .DockerExecutionResourceProvider import (
    DockerExecutionResource,
    DockerExecutionResourceProvider,
)

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceProviderProbe,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


class GVisorDockerExecutionResource(DockerExecutionResource):
    """Docker code resource whose runtime is the host-configured gVisor runsc."""

    runtime_name = "runsc"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._runtime_verified = False

    @staticmethod
    def _reject_runtime_default_args(default_args: Sequence[str]) -> None:
        for item in default_args:
            value = str(item).strip()
            if value == "--runtime" or value.startswith("--runtime="):
                raise ValueError(
                    "gVisor provider owns Docker --runtime; remove --runtime from default_args."
                )

    def _container_base_args(
        self,
        *,
        profile: dict[str, Any],
        workdir: str = "/sandbox",
        extra_mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        self._reject_runtime_default_args(self.default_args)
        args = super()._container_base_args(
            profile=profile,
            workdir=workdir,
            extra_mounts=extra_mounts,
            env=env,
        )
        args[2:2] = ["--runtime", self.runtime_name]
        return args

    def _docker_runtime_registry(self) -> dict[str, Any] | None:
        try:
            result = subprocess.run(
                [self.docker_binary, "info", "--format", "{{json .Runtimes}}"],
                capture_output=True,
                text=True,
                timeout=min(max(self.timeout, 1), 10),
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        if result.returncode != 0:
            return None
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def inspect_availability(self) -> dict[str, Any]:
        availability = super().inspect_availability()
        if not availability.get("available"):
            return availability
        try:
            self._reject_runtime_default_args(self.default_args)
        except ValueError as error:
            return {
                **availability,
                "available": False,
                "reason": "runsc_runtime_arguments_invalid",
                "container_runtime": self.runtime_name,
                "error": str(error),
            }
        registry = self._docker_runtime_registry()
        if registry is None:
            return {
                **availability,
                "available": False,
                "reason": "runsc_runtime_registry_invalid",
                "container_runtime": self.runtime_name,
            }
        runtime = registry.get(self.runtime_name)
        if not isinstance(runtime, dict):
            return {
                **availability,
                "available": False,
                "reason": "runsc_runtime_unregistered",
                "container_runtime": self.runtime_name,
                "registered_runtimes": sorted(str(item) for item in registry),
            }
        return {
            **availability,
            "container_runtime": self.runtime_name,
            "runtime_registration": "registered",
        }

    async def async_verify_runtime(
        self,
        *,
        image: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self._run_container(
            image=image,
            cmd=["true"],
            profile=profile,
            timeout=min(self.timeout, 10),
        )
        self._runtime_verified = bool(result.get("ok"))
        return {"verified": self._runtime_verified, "result": result}

    async def async_execute_code(self, **kwargs: Any) -> dict[str, Any]:
        result = await super().async_execute_code(**kwargs)
        if self._runtime_verified:
            result["meta"] = {
                **(result.get("meta") if isinstance(result.get("meta"), dict) else {}),
                "active_runtime": self.runtime_name,
            }
        return result

    async def run(
        self,
        *,
        image: str,
        cmd: str | list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        if not image:
            return {"ok": False, "error": "Docker image is required."}
        if not self.is_binary_available():
            return {"ok": False, "error": f"Docker binary not found: {self.docker_binary}"}
        self._reject_runtime_default_args(self.default_args)
        args = [
            self.docker_binary,
            "run",
            "--rm",
            "--runtime",
            self.runtime_name,
            *self.default_args,
        ]
        if workdir:
            args.extend(["-w", str(workdir)])
        if isinstance(env, dict):
            for key, value in env.items():
                args.extend(["-e", f"{key}={value}"])
        args.append(image)
        args.extend(shlex.split(cmd) if isinstance(cmd, str) else [str(item) for item in cmd])
        result = await asyncio.to_thread(
            subprocess.run,
            args,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


class GVisorDockerExecutionResourceProvider(DockerExecutionResourceProvider):
    """Optional gVisor implementation of the existing Docker provider contract."""

    name = "GVisorDockerExecutionResourceProvider"

    @property
    def provider_id(self) -> str:
        return "gvisor"

    def create_resource(
        self,
        *,
        docker_binary: str,
        timeout: int,
        default_args: Sequence[str] = (),
        runtime_profile: dict[str, Any] | None = None,
        workspace_grant: TaskWorkspaceAccessGrant | None = None,
        max_output_bytes: int = 20000,
    ) -> GVisorDockerExecutionResource:
        return GVisorDockerExecutionResource(
            docker_binary=docker_binary,
            timeout=timeout,
            default_args=list(default_args),
            runtime_profile=runtime_profile,
            workspace_grant=workspace_grant,
            max_output_bytes=max_output_bytes,
        )

    async def async_probe(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
    ) -> "ExecutionResourceProviderProbe":
        probe = await super().async_probe(requirement=requirement, policy=policy)
        capabilities = probe.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
            probe["capabilities"] = capabilities
        capabilities["container_runtime"] = GVisorDockerExecutionResource.runtime_name
        if probe.get("available"):
            isolation = capabilities.get("isolation")
            if isinstance(isolation, dict):
                isolation["mechanism"] = "gvisor_container"
                isolation["container_runtime"] = GVisorDockerExecutionResource.runtime_name
        return probe

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        handle = await super().async_ensure(
            requirement=requirement,
            policy=policy,
            existing_handle=existing_handle,
        )
        resource = handle.get("resource")
        if not isinstance(resource, GVisorDockerExecutionResource):
            raise ExecutionResourceError(
                "gVisor provider did not create a gVisor Docker resource.",
                code="execution_resource.gvisor_runtime_unavailable",
                payload={"reason": "runsc_resource_construction_failed"},
            )
        try:
            profile = resource._profile()
            image = str(profile.get("image", ""))
            if not image:
                raise ExecutionResourceError(
                    "gVisor provider has no runtime image to verify.",
                    code="execution_resource.gvisor_runtime_unavailable",
                    payload={"reason": "runsc_runtime_image_missing"},
                )
            verification = await resource.async_verify_runtime(image=image, profile=profile)
            if not verification.get("verified"):
                raise ExecutionResourceError(
                    "Docker registered runsc but could not execute the selected runtime.",
                    code="execution_resource.gvisor_runtime_unavailable",
                    payload={
                        "reason": "runsc_runtime_execution_failed",
                        "container_runtime": resource.runtime_name,
                        "runtime_verification": verification,
                    },
                )
        except BaseException:
            await resource.async_close()
            raise
        meta = handle.get("meta")
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta.update(
            {
                "active_runtime": resource.runtime_name,
                "runtime_verification": verification,
            }
        )
        handle["handle_id"] = f"gvisor:{uuid.uuid4().hex}"
        handle["provider_id"] = self.provider_id
        handle["meta"] = meta
        return handle

    async def async_health_check(
        self,
        handle: "ExecutionResourceHandle",
    ) -> "ExecutionResourceStatus":
        resource = handle.get("resource")
        if not isinstance(resource, GVisorDockerExecutionResource):
            return "unhealthy"
        meta = handle.get("meta")
        if not isinstance(meta, dict) or meta.get("active_runtime") != resource.runtime_name:
            return "unhealthy"
        previous_verification = meta.get("runtime_verification")
        if not isinstance(previous_verification, dict) or not previous_verification.get("verified"):
            return "unhealthy"
        availability = await asyncio.to_thread(resource.inspect_availability)
        if not availability.get("available"):
            return "unhealthy"
        profile = resource._profile()
        image = str(profile.get("image", ""))
        if not image:
            return "unhealthy"
        verification = await resource.async_verify_runtime(image=image, profile=profile)
        return "ready" if verification.get("verified") else "unhealthy"


__all__ = [
    "GVisorDockerExecutionResource",
    "GVisorDockerExecutionResourceProvider",
]
