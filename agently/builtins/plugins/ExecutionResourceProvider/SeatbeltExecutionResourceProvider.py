# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Grant-bound macOS Seatbelt code-execution provider."""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceExecutionManifest,
    resolve_code_execution_workspace_uri,
)
from agently.types.data.code_execution import extract_code_toolchain_version

from ._bounded_process import run_bounded_process

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceProviderProbe,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


def is_macos() -> bool:
    return platform.system() == "Darwin"


def _probe_profile() -> str:
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-exec*)",
            "(allow file-read*)",
            "(allow sysctl-read)",
            "(allow mach*)",
        ]
    )


def inspect_seatbelt_availability() -> dict[str, Any]:
    """Run a real bounded sandbox-exec probe."""

    if not is_macos():
        return {"available": False, "reason": "not_macos"}
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return {"available": False, "reason": "sandbox_exec_missing"}
    try:
        completed = subprocess.run(
            [binary, "-p", _probe_profile(), "/usr/bin/true"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "available": False,
            "reason": "sandbox_exec_failed",
            "binary": binary,
            "error": str(error)[:300],
        }
    if completed.returncode != 0:
        return {
            "available": False,
            "reason": "sandbox_exec_failed",
            "binary": binary,
            "returncode": completed.returncode,
            "stdout": completed.stdout[:300],
            "stderr": completed.stderr[:300],
        }
    return {
        "available": True,
        "reason": "ready",
        "binary": binary,
        "platform": "macos",
    }


def _sbpl_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_sbpl_profile(
    *,
    grant: TaskWorkspaceAccessGrant,
    network: bool = False,
) -> str:
    """Build a profile whose write rules derive only from the Workspace grant."""

    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec* process-fork signal)",
        "(allow sysctl-read)",
        "(allow mach*)",
        "(allow ipc-posix*)",
        # Toolchains and their dynamic libraries may live outside system roots.
        # This limitation is reported as host_filesystem_restricted=False.
        "(allow file-read*)",
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-write* (literal "/dev/ptmx"))',
        "(allow pseudo-tty)",
    ]
    for root in grant.roots:
        path = _sbpl_literal(str(Path(root.host_path).resolve()))
        if root.access_mode == "read_write":
            lines.append(f'(allow file-write* (subpath "{path}"))')
        else:
            lines.append(f'(deny file-write* (subpath "{path}"))')
    lines.append("(allow network-outbound)" if network else "(deny network-outbound)")
    return "\n".join(lines) + "\n"


class SeatbeltCodeExecutionResource:
    """Execute immutable code bundles under a grant-derived SBPL profile."""

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
        network: bool = False,
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self.network = bool(network)
        self._active_executions: set[asyncio.Task[Any]] = set()
        self._profile_paths: set[Path] = set()
        self._closed = False

    @staticmethod
    def _sha256(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    def _build_profile(self) -> str:
        return _build_sbpl_profile(grant=self.grant, network=self.network)

    def _validate_materialization(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
    ) -> Path:
        if grant != self.grant:
            raise PermissionError("Seatbelt resource is bound to another Workspace grant.")
        if (
            manifest.grant_id != grant.grant_id
            or manifest.bundle_id != bundle.bundle_id
            or manifest.bundle_digest != bundle.bundle_digest
        ):
            raise PermissionError("Code execution manifest does not match the bound bundle and grant.")
        area = Path(grant.execution_area).resolve()
        manifest_files = {Path(item.host_path).resolve(): item for item in manifest.files}
        for item in bundle.files:
            target = (area / "source" / Path(item.path)).resolve()
            if area not in target.parents or target.is_symlink() or not target.is_file():
                raise PermissionError("Materialized bundle file escaped or is unavailable.")
            recorded = manifest_files.get(target)
            if recorded is None or recorded.sha256 != item.sha256:
                raise PermissionError("Materialized bundle file is absent from the Workspace manifest.")
            if self._sha256(target) != item.sha256:
                raise PermissionError("Materialized bundle file digest changed before execution.")
        return area

    def _root_map(self) -> dict[str, str]:
        return {
            root.role: root.host_path
            for root in self.grant.roots
            if root.role in {"source", "build", "output", "logs"}
        }

    def _profile_path(self, *, area: Path, index: int) -> Path:
        path = area / "logs" / f".seatbelt-{uuid.uuid4().hex}-{index}.sb"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._build_profile(), encoding="utf-8")
        self._profile_paths.add(path)
        return path

    async def _run(
        self,
        *,
        bundle: CodeExecutionBundle,
        area: Path,
        timeout: int,
    ) -> dict[str, Any]:
        logs_root = area / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        roots = self._root_map()
        temp_root = roots.get("build") or roots.get("logs")
        steps = (*bundle.build_steps, bundle.run_step)
        final_stdout = b""
        final_stderr = b""
        returncode = 0
        stdout_truncated = False
        stderr_truncated = False
        log_refs: list[str] = []
        for index, step in enumerate(steps):
            cwd = (area / Path(step.cwd)).resolve()
            if area not in cwd.parents or not cwd.is_dir() or cwd.is_symlink():
                raise PermissionError("Execution step cwd escaped its Workspace grant.")
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            if temp_root:
                environment.update(TMPDIR=temp_root, TMP=temp_root, TEMP=temp_root)
            environment.update(
                {
                    key: resolve_code_execution_workspace_uri(value, roots=roots)
                    for key, value in step.env.items()
                }
            )
            profile_path = self._profile_path(area=area, index=index)
            try:
                completed = await run_bounded_process(
                    ["sandbox-exec", "-f", str(profile_path), *list(step.argv)],
                    cwd=str(cwd),
                    env=environment,
                    timeout=max(1, timeout),
                    max_output_bytes=self.max_output_bytes,
                )
            finally:
                try:
                    profile_path.unlink(missing_ok=True)
                finally:
                    self._profile_paths.discard(profile_path)
            returncode = completed.returncode
            final_stdout = completed.stdout
            final_stderr = completed.stderr
            stdout_truncated = completed.stdout_truncated
            stderr_truncated = completed.stderr_truncated or completed.timed_out
            if completed.timed_out:
                message = f"execution timed out after {timeout} seconds\n".encode()
                final_stderr += message[: max(0, self.max_output_bytes - len(final_stderr))]
            stdout_path = logs_root / f"{index:02d}-{step.role}.stdout.log"
            stderr_path = logs_root / f"{index:02d}-{step.role}.stderr.log"
            stdout_path.write_bytes(final_stdout)
            stderr_path.write_bytes(final_stderr)
            log_refs.extend([f"logs/{stdout_path.name}", f"logs/{stderr_path.name}"])
            if returncode != 0:
                break
        outputs = [
            path
            for path in bundle.expected_outputs
            if (area / Path(path)).is_file() and not (area / Path(path)).is_symlink()
        ]
        return {
            "ok": returncode == 0,
            "status": "success" if returncode == 0 else "error",
            "returncode": returncode,
            "stdout": final_stdout.decode("utf-8", errors="replace"),
            "stderr": final_stderr.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "outputs": outputs,
            "log_refs": log_refs,
            "meta": {
                "mechanism": "seatbelt",
                "host_filesystem_restricted": False,
                "workspace_write_restricted": True,
            },
        }

    async def async_execute_code(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
        timeout: int,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Seatbelt execution resource is closed.")
        area = self._validate_materialization(bundle=bundle, manifest=manifest, grant=grant)
        task = asyncio.current_task()
        if task is not None:
            self._active_executions.add(task)
        try:
            return await self._run(bundle=bundle, area=area, timeout=timeout)
        finally:
            if task is not None:
                self._active_executions.discard(task)

    async def async_close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        active = [task for task in self._active_executions if task is not current]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for path in tuple(self._profile_paths):
            path.unlink(missing_ok=True)
            self._profile_paths.discard(path)


class SeatbeltExecutionResourceProvider:
    name = "SeatbeltExecutionResourceProvider"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    provider_id = "seatbelt"
    supported_kinds = ("code_execution",)
    _allowed_config = {"dependency_policy", "network"}

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    def _tool_facts(self) -> dict[str, dict[str, Any]]:
        commands = {
            "python": ("python3", ("--version",)),
            "nodejs": ("node", ("--version",)),
            "go": ("go", ("version",)),
            "cpp": ("c++", ("--version",)),
        }
        facts: dict[str, dict[str, Any]] = {}
        for language, (tool, command_args) in commands.items():
            binary = shutil.which(tool)
            fact: dict[str, Any] = {
                "tool": {"nodejs": "node", "cpp": "c++"}.get(language, language),
                "available": binary is not None,
                "binary": binary or "",
                "version": "",
                "raw_version": "",
            }
            if binary is not None:
                try:
                    completed = subprocess.run(
                        [binary, *command_args],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    raw = str(completed.stdout or completed.stderr).strip()[:300]
                    fact.update(
                        available=completed.returncode == 0,
                        raw_version=raw,
                        version=extract_code_toolchain_version(raw),
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    fact.update(available=False, error=str(error)[:300])
            facts[language] = fact
        return facts

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> None:
        unknown = sorted(set(config).difference(SeatbeltExecutionResourceProvider._allowed_config))
        if unknown:
            from agently.core import ExecutionResourceError

            raise ExecutionResourceError(
                "Seatbelt provider configuration contains unsupported policy fields.",
                code="execution_resource.seatbelt_config_invalid",
                payload={"unsupported_fields": unknown},
            )

    def create_resource(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int,
        network: bool,
    ) -> SeatbeltCodeExecutionResource:
        return SeatbeltCodeExecutionResource(
            grant=grant,
            max_output_bytes=max_output_bytes,
            network=network,
        )

    async def async_probe(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
    ) -> "ExecutionResourceProviderProbe":
        _ = requirement, policy
        availability = await asyncio.to_thread(inspect_seatbelt_availability)
        available = bool(availability.get("available"))
        facts = await asyncio.to_thread(self._tool_facts) if available else {}
        languages = [language for language, fact in facts.items() if fact["available"]]
        toolchains = {
            str(fact["tool"]): {
                "available": bool(fact["available"]),
                "version": str(fact.get("version", "")),
                "raw_version": str(fact.get("raw_version", "")),
                "binary": str(fact.get("binary", "")),
            }
            for fact in facts.values()
        }
        return {
            "provider_id": self.provider_id,
            "available": available and bool(languages),
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": languages,
                "toolchains": toolchains,
                "isolation": {
                    "process_contained": True,
                    "host_filesystem_restricted": False,
                    "workspace_write_restricted": True,
                    "privilege_escalation_blocked": True,
                    "syscalls_restricted": True,
                    "mechanism": "seatbelt",
                    "network_mode": "configurable",
                },
                "workspace_access_modes": ["snapshot", "read_only", "read_write"],
                "network": "configurable",
                "safety_class": "host_policy",
            },
            "reason": "ready" if available and languages else str(availability.get("reason", "toolchain_unavailable")),
            "meta": {"availability": availability, "toolchains": facts},
        }

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        _ = existing_handle
        from agently.core import ExecutionResourceError

        config = requirement.get("config", {})
        config = dict(config) if isinstance(config, dict) else {}
        self._validate_config(config)
        grant = requirement.get("task_workspace_access_grant")
        if not isinstance(grant, TaskWorkspaceAccessGrant):
            raise ExecutionResourceError(
                "Seatbelt code execution requires a TaskWorkspace access grant.",
                code="execution_resource.workspace_grant_required",
                payload={"provider_id": self.provider_id},
            )
        availability = await asyncio.to_thread(inspect_seatbelt_availability)
        if not availability.get("available"):
            raise ExecutionResourceError(
                f"Seatbelt is unavailable: {availability.get('reason', 'unknown')}",
                code="execution_resource.seatbelt_unavailable",
                payload={"provider_id": self.provider_id, "availability": availability},
            )
        resource = self.create_resource(
            grant=grant,
            max_output_bytes=int(policy.get("max_output_bytes", 20000)),
            network=bool(config.get("network", False)),
        )
        try:
            verified = await asyncio.to_thread(inspect_seatbelt_availability)
            if not verified.get("available"):
                raise ExecutionResourceError(
                    "Seatbelt mechanism verification failed before handle readiness.",
                    code="execution_resource.seatbelt_unavailable",
                    payload={"provider_id": self.provider_id, "availability": verified},
                )
        except BaseException:
            await resource.async_close()
            raise
        return {
            "handle_id": f"seatbelt:{uuid.uuid4().hex}",
            "provider_id": self.provider_id,
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "mechanism_verified": True,
                "availability": verified,
                "grant_id": grant.grant_id,
            },
        }

    async def async_health_check(
        self,
        handle: "ExecutionResourceHandle",
    ) -> "ExecutionResourceStatus":
        resource = handle.get("resource")
        meta = handle.get("meta")
        if (
            not isinstance(resource, SeatbeltCodeExecutionResource)
            or not isinstance(meta, dict)
            or not meta.get("mechanism_verified")
            or resource._closed
        ):
            return "unhealthy"
        availability = await asyncio.to_thread(inspect_seatbelt_availability)
        return "ready" if availability.get("available") else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        resource = handle.get("resource")
        if isinstance(resource, SeatbeltCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "SeatbeltCodeExecutionResource",
    "SeatbeltExecutionResourceProvider",
    "inspect_seatbelt_availability",
]
